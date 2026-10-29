"""Low-latency, privacy-bounded abuse counters shared by Control and Gateway."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import unquote, urlparse

from fastapi import FastAPI

from metahub_shared.runtime.config import AppConfig

RiskAction = Literal["registration", "login", "login_recovery", "resource_creation", "gateway_api"]
RiskOutcome = Literal["allow", "challenge", "restricted"]
SubjectKind = Literal["ip_prefix", "device", "account", "workspace", "virtual_key"]

_POLICIES: dict[RiskAction, tuple[int, int, RiskOutcome]] = {
    "registration": (300, 20, "challenge"),
    "login": (300, 50, "challenge"),
    "login_recovery": (300, 10, "challenge"),
    "resource_creation": (60, 30, "challenge"),
    "gateway_api": (60, 1000, "restricted"),
}
_ALLOWED_SUBJECTS: dict[RiskAction, frozenset[SubjectKind]] = {
    "registration": frozenset({"ip_prefix", "device", "account"}),
    "login": frozenset({"ip_prefix", "device", "account"}),
    "login_recovery": frozenset({"ip_prefix", "device", "account"}),
    "resource_creation": frozenset({"ip_prefix", "device", "account"}),
    "gateway_api": frozenset({"workspace", "virtual_key"}),
}


class RiskCounterUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CounterClaim:
    key: str
    subject_kind: SubjectKind
    subject_hmac: str
    window_started_at: datetime
    window_seconds: int
    ttl_seconds: int


@dataclass(frozen=True, slots=True)
class CounterFact:
    action: RiskAction
    subject_kind: SubjectKind
    subject_hmac: str
    window_started_at: datetime
    window_seconds: int
    count_after: int
    source_code: Literal["redis", "test_injected"]
    request_id: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class DegradedFact:
    kind: Literal["privilege_fail_closed", "gateway_fail_open"]


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    action: RiskAction
    outcome: RiskOutcome
    reason_family: Literal["within_policy", "unusual_volume", "counter_unavailable"]
    degraded: bool
    max_count: int
    limit: int


class RiskCounterStore(Protocol):
    async def increment_many(self, claims: tuple[CounterClaim, ...]) -> tuple[int, ...]: ...


class InMemoryRiskCounterStore:
    """Atomic local implementation for tests and non-production development."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._values: dict[str, tuple[int, float]] = {}
        self.available = True

    async def increment_many(self, claims: tuple[CounterClaim, ...]) -> tuple[int, ...]:
        if not self.available:
            raise RiskCounterUnavailable("counter unavailable")
        now = self._monotonic()
        with self._lock:
            values: list[int] = []
            for claim in claims:
                current, expires = self._values.get(claim.key, (0, 0.0))
                if expires <= now:
                    current = 0
                values.append(current + 1)
            for claim, value in zip(claims, values, strict=True):
                self._values[claim.key] = (value, now + claim.ttl_seconds)
            return tuple(values)


@dataclass(frozen=True, slots=True, repr=False)
class _RedisConfig:
    host: str
    port: int
    db: int
    username: str | None
    password: str | None
    use_tls: bool


class RedisRiskCounterStore:
    """Minimal RESP client using one Lua operation for all request dimensions."""

    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url
        self._config = _parse_redis_url(redis_url)

    async def increment_many(self, claims: tuple[CounterClaim, ...]) -> tuple[int, ...]:
        if not claims:
            return ()
        script = (
            "local out={};"
            "for i=1,#KEYS do "
            "local value=redis.call('INCRBY',KEYS[i],1);"
            "if value==1 then redis.call('EXPIRE',KEYS[i],ARGV[i]); end;"
            "out[i]=value;"
            "end;return out;"
        )
        result = await self._command(
            "EVAL",
            script,
            str(len(claims)),
            *(claim.key for claim in claims),
            *(str(claim.ttl_seconds) for claim in claims),
        )
        if (
            type(result) is not list
            or len(result) != len(claims)
            or any(type(value) is not int or value < 1 for value in result)
        ):
            raise RiskCounterUnavailable("redis response invalid")
        return tuple(result)

    async def _command(self, *parts: str) -> object:
        try:
            reader, writer = await asyncio.open_connection(
                self._config.host,
                self._config.port,
                ssl=self._config.use_tls,
                server_hostname=self._config.host if self._config.use_tls else None,
            )
            try:
                await self._authenticate(reader, writer)
                writer.write(_encode_command(parts))
                await writer.drain()
                return await _read_response(reader)
            finally:
                writer.close()
                await writer.wait_closed()
        except Exception as exc:
            raise RiskCounterUnavailable("redis command failed") from exc

    async def _authenticate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._config.password:
            auth = (
                ("AUTH", self._config.username, self._config.password)
                if self._config.username
                else ("AUTH", self._config.password)
            )
            writer.write(_encode_command(auth))
            await writer.drain()
            await _read_response(reader)
        if self._config.db:
            writer.write(_encode_command(("SELECT", str(self._config.db))))
            await writer.drain()
            await _read_response(reader)


RiskFact = CounterFact | DegradedFact
FactSink = Callable[[tuple[RiskFact, ...]], Awaitable[None]]


class RiskRuntime:
    """Hashes fixed subjects, evaluates atomic windows, and queues rebuild facts."""

    def __init__(
        self,
        config: AppConfig,
        *,
        store: RiskCounterStore | None = None,
        sink: FactSink | None = None,
        queue_size: int = 4096,
        timeout_seconds: float = 0.025,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._store = store or (
            RedisRiskCounterStore(config.redis_url)
            if config.redis_url
            else InMemoryRiskCounterStore()
        )
        self._source: Literal["redis", "test_injected"] = (
            "redis" if config.redis_url else "test_injected"
        )
        self._sink = sink
        self._queue: asyncio.Queue[RiskFact] = asyncio.Queue(maxsize=queue_size)
        self._timeout_seconds = timeout_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._stop = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._fixed_counters = {
            "backend_failure": 0,
            "queue_full": 0,
            "privilege_fail_closed": 0,
            "gateway_fail_open": 0,
        }

    @property
    def fixed_counters(self) -> Mapping[str, int]:
        return dict(self._fixed_counters)

    async def assess(
        self,
        action: RiskAction,
        subjects: Mapping[SubjectKind, str],
        *,
        request_id: str,
        trace_id: str,
    ) -> RiskAssessment:
        if action not in _POLICIES or not subjects:
            raise ValueError("risk assessment invalid")
        if not set(subjects) <= _ALLOWED_SUBJECTS[action]:
            raise ValueError("risk subject dimension invalid")
        window_seconds, limit, exceeded_outcome = _POLICIES[action]
        now = self._now().astimezone(UTC)
        epoch = int(now.timestamp())
        window_epoch = epoch - (epoch % window_seconds)
        started = datetime.fromtimestamp(window_epoch, UTC)
        claims = tuple(
            self._claim(action, kind, value, started, window_seconds)
            for kind, value in sorted(subjects.items())
            if value
        )
        if not claims:
            raise ValueError("risk assessment has no usable subjects")
        try:
            counts = await asyncio.wait_for(
                self._store.increment_many(claims), timeout=self._timeout_seconds
            )
        except (RiskCounterUnavailable, TimeoutError, OSError, ValueError):
            self._fixed_counters["backend_failure"] += 1
            if action == "gateway_api":
                self._fixed_counters["gateway_fail_open"] += 1
                self._enqueue(DegradedFact(kind="gateway_fail_open"))
                return RiskAssessment(
                    action=action,
                    outcome="allow",
                    reason_family="counter_unavailable",
                    degraded=True,
                    max_count=0,
                    limit=limit,
                )
            self._fixed_counters["privilege_fail_closed"] += 1
            self._enqueue(DegradedFact(kind="privilege_fail_closed"))
            return RiskAssessment(
                action=action,
                outcome="challenge",
                reason_family="counter_unavailable",
                degraded=True,
                max_count=0,
                limit=limit,
            )
        for claim, count in zip(claims, counts, strict=True):
            self._enqueue(
                CounterFact(
                    action=action,
                    subject_kind=claim.subject_kind,
                    subject_hmac=claim.subject_hmac,
                    window_started_at=claim.window_started_at,
                    window_seconds=claim.window_seconds,
                    count_after=count,
                    source_code=self._source,
                    request_id=_bounded_link(request_id),
                    trace_id=_bounded_link(trace_id),
                )
            )
        maximum = max(counts)
        return RiskAssessment(
            action=action,
            outcome=exceeded_outcome if maximum > limit else "allow",
            reason_family="unusual_volume" if maximum > limit else "within_policy",
            degraded=False,
            max_count=maximum,
            limit=limit,
        )

    def _claim(
        self,
        action: RiskAction,
        kind: SubjectKind,
        raw_value: str,
        started: datetime,
        window_seconds: int,
    ) -> CounterClaim:
        normalized = _normalized_subject(kind, raw_value)
        digest = hmac.new(
            self._config.cache_affinity_hmac_secret.encode(),
            f"prd110/{action}/{kind}/v1\0{normalized}".encode(),
            hashlib.sha256,
        ).hexdigest()
        window_epoch = int(started.timestamp())
        return CounterClaim(
            key=f"metahub:risk:v1:{action}:{kind}:{digest}:{window_epoch}",
            subject_kind=kind,
            subject_hmac=digest,
            window_started_at=started,
            window_seconds=window_seconds,
            ttl_seconds=window_seconds + 5,
        )

    def _enqueue(self, fact: RiskFact) -> None:
        try:
            self._queue.put_nowait(fact)
        except asyncio.QueueFull:
            self._fixed_counters["queue_full"] += 1

    async def start(self) -> None:
        if self._worker is None and self._sink is not None:
            self._worker = asyncio.create_task(self._run_sink(), name="risk-fact-sink")

    async def stop(self) -> None:
        self._stop.set()
        if self._worker is not None:
            await self._worker
            self._worker = None

    async def _run_sink(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            facts: list[RiskFact] = []
            try:
                fact = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                facts.append(fact)
            except TimeoutError:
                continue
            while len(facts) < 100:
                try:
                    facts.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._sink(tuple(facts))
            except Exception:
                self._fixed_counters["backend_failure"] += 1
            finally:
                for _fact in facts:
                    self._queue.task_done()


def runtime_for(state, config: AppConfig) -> RiskRuntime:
    runtime = getattr(state, "abuse_risk_runtime", None)
    if isinstance(runtime, RiskRuntime):
        return runtime
    runtime = RiskRuntime(config)
    state.abuse_risk_runtime = runtime
    return runtime


def install_risk_runtime(
    app: FastAPI,
    *,
    sink: FactSink | None = None,
) -> None:
    original_lifespan = app.router.lifespan_context
    runtime = RiskRuntime(app.state.config, sink=sink)
    app.state.abuse_risk_runtime = runtime

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            await runtime.start()
            try:
                yield
            finally:
                await runtime.stop()

    app.router.lifespan_context = combined_lifespan


def _normalized_subject(kind: SubjectKind, value: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 512 or "\x00" in text:
        raise ValueError("risk subject invalid")
    if kind == "ip_prefix":
        address = ipaddress.ip_address(text)
        prefix = 24 if address.version == 4 else 56
        return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
    return text.casefold() if kind == "account" else text


def _bounded_link(value: str) -> str:
    text = str(value)
    return text if 1 <= len(text) <= 128 else "risk-runtime"


def _parse_redis_url(redis_url: str) -> _RedisConfig:
    parsed = urlparse(redis_url)
    if parsed.scheme not in {"redis", "rediss"}:
        raise RiskCounterUnavailable("redis scheme invalid")
    try:
        db = int(parsed.path.lstrip("/") or "0")
    except ValueError as exc:
        raise RiskCounterUnavailable("redis database invalid") from exc
    if not 0 <= db <= 15:
        raise RiskCounterUnavailable("redis database invalid")
    return _RedisConfig(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        db=db,
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
        use_tls=parsed.scheme == "rediss",
    )


def _encode_command(parts: tuple[str, ...]) -> bytes:
    encoded = [part.encode() for part in parts]
    result = [f"*{len(encoded)}\r\n".encode("ascii")]
    for part in encoded:
        result.extend((f"${len(part)}\r\n".encode("ascii"), part, b"\r\n"))
    return b"".join(result)


async def _read_response(reader: asyncio.StreamReader) -> object:
    prefix = await reader.readexactly(1)
    line = await reader.readline()
    if prefix == b"+":
        return line.rstrip(b"\r\n").decode()
    if prefix == b":":
        return int(line.rstrip(b"\r\n"))
    if prefix == b"$":
        length = int(line.rstrip(b"\r\n"))
        if length < 0:
            return None
        data = await reader.readexactly(length)
        await reader.readexactly(2)
        return data.decode()
    if prefix == b"*":
        length = int(line.rstrip(b"\r\n"))
        return [await _read_response(reader) for _index in range(max(0, length))]
    raise RiskCounterUnavailable("redis response invalid")


__all__ = [
    "CounterFact",
    "DegradedFact",
    "InMemoryRiskCounterStore",
    "RedisRiskCounterStore",
    "RiskAssessment",
    "RiskCounterUnavailable",
    "RiskRuntime",
    "install_risk_runtime",
    "runtime_for",
]
