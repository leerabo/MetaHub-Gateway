"""Bounded, fail-open telemetry buffering with immutable database batch watermarks."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import queue
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

logger = logging.getLogger("metahub.observability.runtime")
_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,63}$")
_REGION = re.compile(r"^[a-z0-9][a-z0-9.-]{1,39}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_STATUS_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx", "other"})
_REGIONS = frozenset(
    {
        "global",
        "cn-east-1",
        "cn-north-1",
        "cn-south-1",
        "us-east-1",
        "us-west-1",
        "eu-west-1",
        "ap-southeast-1",
    }
)
_DURATION_BUCKETS = (
    (10, "le_10ms"),
    (50, "le_50ms"),
    (100, "le_100ms"),
    (250, "le_250ms"),
    (500, "le_500ms"),
    (1000, "le_1000ms"),
    (2500, "le_2500ms"),
    (5000, "le_5000ms"),
)
_OPERATIONS = {
    "gateway": {
        ("POST", "/v1/chat/completions"): "chat.completions",
        ("POST", "/v1/embeddings"): "embeddings",
        ("POST", "/v1/adapter-contract-probes/identity"): "adapter.identity_probe",
        ("POST", "/internal/chat/completions"): "internal.chat",
        ("POST", "/internal/batch/chat/completions"): "internal.batch.chat",
        ("GET", "/health"): "health",
        ("GET", "/ready"): "readiness",
    },
    "control": {
        ("GET", "/platform/observability/services"): "observability.services",
        ("GET", "/platform/slos"): "observability.slos.read",
        ("POST", "/platform/slos"): "observability.slos.write",
        ("GET", "/platform/error-budgets"): "observability.budgets",
        ("POST", "/platform/alerts/{alert_id}/ack"): "observability.alert_ack",
        ("GET", "/health"): "health",
        ("GET", "/ready"): "readiness",
    },
}


@dataclass(frozen=True, slots=True, repr=False)
class TelemetryFact:
    service_code: str
    operation_code: str
    region_code: str
    status_class: str
    duration_bucket: str
    bucket_started_at: datetime
    request_link_sha256: str
    trace_link_sha256: str
    tenant_pseudonym: str = "none"

    def __post_init__(self) -> None:
        if (
            self.service_code not in _OPERATIONS
            or self.operation_code
            not in {*_OPERATIONS[self.service_code].values(), "other"}
            or self.region_code not in _REGIONS
            or self.status_class not in _STATUS_CLASSES
            or self.duration_bucket not in {item[1] for item in _DURATION_BUCKETS} | {
                "gt_5000ms"
            }
            or self.bucket_started_at.tzinfo is None
            or _HASH.fullmatch(self.request_link_sha256) is None
            or _HASH.fullmatch(self.trace_link_sha256) is None
            or (
                self.tenant_pseudonym != "none"
                and _HASH.fullmatch(self.tenant_pseudonym) is None
            )
        ):
            raise ValueError("telemetry.fact_invalid")


@dataclass(frozen=True, slots=True)
class TelemetrySnapshot:
    queued: int
    accepted: int
    dropped: int
    persisted: int
    backend_failures: int


class TelemetryBuffer:
    """Own a bounded queue; callers never wait for or observe telemetry persistence."""

    def __init__(
        self,
        config: AppConfig,
        *,
        service_code: str,
        region_code: str = "global",
        capacity: int = 4096,
        batch_size: int = 200,
        flush_interval_seconds: float = 0.25,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            _CODE.fullmatch(service_code) is None
            or region_code not in _REGIONS
            or not 16 <= capacity <= 65536
            or not 1 <= batch_size <= capacity
            or not 0.01 <= flush_interval_seconds <= 5.0
        ):
            raise ValueError("telemetry.policy_invalid")
        self._config = config
        self._service_code = service_code
        self._region_code = region_code
        self._queue: queue.Queue[TelemetryFact] = queue.Queue(maxsize=capacity)
        self._batch_size = batch_size
        self._flush_interval_seconds = flush_interval_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._accepted = 0
        self._dropped = 0
        self._pending_dropped = 0
        self._persisted = 0
        self._backend_failures = 0

    def enqueue_http(
        self,
        *,
        operation_code: str,
        status_code: int,
        duration_ms: int,
        request_id: str,
        trace_id: str,
        tenant_owner: str | None = None,
    ) -> bool:
        try:
            fact = TelemetryFact(
                service_code=self._service_code,
                operation_code=operation_code,
                region_code=self._region_code,
                status_class=_status_class(status_code),
                duration_bucket=_duration_bucket(duration_ms),
                bucket_started_at=_minute(self._now()),
                request_link_sha256=self._link("request", request_id),
                trace_link_sha256=self._link("trace", trace_id),
                tenant_pseudonym=(
                    "none" if tenant_owner is None else self._link("tenant", tenant_owner)
                ),
            )
            self._queue.put_nowait(fact)
        except (ValueError, queue.Full):
            self._record_drop(1)
            return False
        with self._lock:
            self._accepted += 1
        return True

    def flush_once(self) -> bool:
        facts: list[TelemetryFact] = []
        for _index in range(self._batch_size):
            try:
                facts.append(self._queue.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            pending_dropped = self._pending_dropped
            self._pending_dropped = 0
        if not facts and pending_dropped == 0:
            return False
        try:
            self._persist_batch(facts, pending_dropped)
        except (sqlite3.Error, DbError, TypeError, ValueError):
            with self._lock:
                self._backend_failures += 1
                self._pending_dropped += pending_dropped + len(facts)
                self._dropped += len(facts)
            return False
        finally:
            for _fact in facts:
                self._queue.task_done()
        with self._lock:
            self._persisted += len(facts)
        return True

    async def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            progressed = await asyncio.to_thread(self.flush_once)
            await _wait(stop_event, 0.01 if progressed else self._flush_interval_seconds)
        while await asyncio.to_thread(self.flush_once):
            pass

    def schema_available(self) -> bool:
        try:
            connection = connect_database(self._config.database_url)
            try:
                return connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='table' "
                    "AND name='telemetry_ingest_head'"
                ).fetchone() is not None
            finally:
                connection.close()
        except (sqlite3.Error, DbError):
            return False

    def snapshot(self) -> TelemetrySnapshot:
        with self._lock:
            return TelemetrySnapshot(
                queued=self._queue.qsize(),
                accepted=self._accepted,
                dropped=self._dropped,
                persisted=self._persisted,
                backend_failures=self._backend_failures,
            )

    def _persist_batch(self, facts: list[TelemetryFact], dropped: int) -> None:
        aggregates = Counter(
            (
                _db_time(fact.bucket_started_at),
                fact.service_code,
                fact.operation_code,
                fact.region_code,
                fact.status_class,
                fact.duration_bucket,
                fact.tenant_pseudonym,
            )
            for fact in facts
        )
        connection = connect_database(self._config.database_url)
        try:
            connection.execute("BEGIN IMMEDIATE")
            head = connection.execute("SELECT * FROM telemetry_ingest_head").fetchone()
            if head is None:
                raise ValueError("telemetry.unavailable")
            watermark = int(head["source_watermark"]) + (1 if aggregates else 0)
            now = _database_now(connection)
            changed_at = _changed_time(now, head["updated_at"])
            for dimensions, total_count in sorted(aggregates.items()):
                (
                    bucket_started_at,
                    service_code,
                    operation_code,
                    region_code,
                    status_class,
                    duration_bucket,
                    tenant_pseudonym,
                ) = dimensions
                good_count = total_count if status_class in {"2xx", "3xx"} else 0
                source = {
                    "bucket_started_at": bucket_started_at,
                    "duration_bucket": duration_bucket,
                    "good_count": good_count,
                    "operation_code": operation_code,
                    "region_code": region_code,
                    "service_code": service_code,
                    "source_watermark": watermark,
                    "status_class": status_class,
                    "tenant_pseudonym": tenant_pseudonym,
                    "total_count": total_count,
                }
                connection.execute(
                    """
                    INSERT INTO sli_time_buckets(
                      sli_time_bucket_id,source_watermark,bucket_started_at,service_code,
                      operation_code,region_code,status_class,duration_bucket,
                      tenant_pseudonym,good_count,total_count,source_sha256,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        _id("slb_"),
                        watermark,
                        bucket_started_at,
                        service_code,
                        operation_code,
                        region_code,
                        status_class,
                        duration_bucket,
                        tenant_pseudonym,
                        good_count,
                        total_count,
                        _digest(source),
                        _db_time(changed_at),
                    ),
                )
            connection.execute(
                """
                UPDATE telemetry_ingest_head
                SET source_watermark=?,dropped_fact_count=dropped_fact_count+?,
                  revision=revision+1,updated_at=?
                WHERE telemetry_ingest_head_id=?
                """,
                (
                    watermark,
                    dropped,
                    _db_time(changed_at),
                    head["telemetry_ingest_head_id"],
                ),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _record_drop(self, count: int) -> None:
        with self._lock:
            self._dropped += count
            self._pending_dropped += count

    def _link(self, purpose: str, value: str) -> str:
        normalized = value if isinstance(value, str) and value else "missing"
        return hmac.new(
            self._config.platform_session_secret.encode("utf-8"),
            f"prd109/telemetry-{purpose}/v1\0{normalized}".encode(),
            hashlib.sha256,
        ).hexdigest()


def install_telemetry_runtime(app: FastAPI, *, service_code: str) -> None:
    """Install one non-blocking middleware and one bounded lifespan worker."""

    config = app.state.config
    region = str(config.object_store_region or "global").lower()
    if _REGION.fullmatch(region) is None:
        region = "global"
    runtime = TelemetryBuffer(config, service_code=service_code, region_code=region)
    app.state.telemetry_runtime = runtime

    @app.middleware("http")
    async def telemetry_middleware(request: Request, call_next):  # type: ignore[type-arg]
        started = time.perf_counter()
        response = await call_next(request)
        try:
            route = request.scope.get("route")
            route_path = getattr(route, "path", request.url.path)
            operation = _OPERATIONS.get(service_code, {}).get(
                (request.method.upper(), route_path), "other"
            )
            runtime.enqueue_http(
                operation_code=operation,
                status_code=int(response.status_code),
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                request_id=str(getattr(request.state, "request_id", "missing")),
                trace_id=str(getattr(request.state, "trace_id", "missing")),
            )
        except Exception:
            runtime._record_drop(1)
        return response

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task: asyncio.Task[None] | None = None
            if await asyncio.to_thread(runtime.schema_available):
                task = asyncio.create_task(
                    runtime.run(stop_event), name=f"{service_code}-telemetry"
                )
            try:
                yield
            finally:
                stop_event.set()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


def _status_class(value: int) -> str:
    if type(value) is not int or not 100 <= value <= 599:
        return "other"
    return f"{value // 100}xx"


def _duration_bucket(value: int) -> str:
    duration = max(0, int(value))
    return next((name for limit, name in _DURATION_BUCKETS if duration <= limit), "gt_5000ms")


def _minute(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(second=0, microsecond=0)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _database_now(connection) -> datetime:
    return _time(connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0])


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _wait(stop_event: threading.Event, delay: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


__all__ = [
    "TelemetryBuffer",
    "TelemetryFact",
    "TelemetrySnapshot",
    "install_telemetry_runtime",
]
