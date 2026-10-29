"""Safe, body-blind connectivity probes for locally held provider credentials."""

from __future__ import annotations

import re
import ssl
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx

from .store import ProviderTestResult

_CONNECTION_ID = re.compile(r"^[a-z0-9][a-z0-9_:-]{2,79}$")
_MAX_CREDENTIAL_BYTES = 16_384


class OpenAICompatibleProviderTester:
    """Probe an OpenAI-compatible models endpoint without consuming its response body."""

    __slots__ = ("_models_url", "_monotonic", "_timeout_seconds", "_transport")

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        base = base_url.rstrip("/")
        parsed = urlsplit(base)
        if (
            len(base) > 2_048
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(ord(character) < 32 or ord(character) == 127 for character in base)
            or type(timeout_seconds) not in {int, float}
            or not 0.1 <= float(timeout_seconds) <= 30.0
        ):
            raise ValueError("local_secret.provider_target_invalid")
        self._models_url = f"{base}/models"
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport
        self._monotonic = monotonic

    def __call__(self, secret: memoryview) -> ProviderTestResult:
        started = self._monotonic()
        credential = bytearray()
        headers: httpx.Headers | None = None
        try:
            credential.extend(secret)
            if (
                not 1 <= len(credential) <= _MAX_CREDENTIAL_BYTES
                or any(value < 33 or value > 126 for value in credential)
            ):
                return self._result("authentication_failed", started)
            headers = httpx.Headers(
                {
                    b"Accept": b"application/json",
                    b"Authorization": b"Bearer " + bytes(credential),
                    b"Cache-Control": b"no-store",
                }
            )
            with httpx.Client(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                with client.stream("GET", self._models_url, headers=headers) as response:
                    outcome = _status_outcome(response.status_code, response.is_redirect)
            capabilities = ("models",) if outcome == "succeeded" else ()
            return self._result(outcome, started, capabilities=capabilities)
        except httpx.TimeoutException:
            return self._result("timeout", started)
        except httpx.ConnectError as exc:
            outcome = "tls_failed" if _caused_by_ssl(exc) else "network_unreachable"
            return self._result(outcome, started)
        except (httpx.NetworkError, httpx.ProtocolError, OSError):
            return self._result("network_unreachable", started)
        except Exception:
            return self._result("provider_unavailable", started)
        finally:
            if headers is not None:
                headers.clear()
            credential[:] = b"\x00" * len(credential)

    def _result(
        self,
        outcome: str,
        started: float,
        *,
        capabilities: tuple[str, ...] = (),
    ) -> ProviderTestResult:
        elapsed_ms = max(0.0, (self._monotonic() - started) * 1_000)
        return ProviderTestResult(  # type: ignore[arg-type]
            outcome,
            capabilities,  # type: ignore[arg-type]
            _latency_bucket(elapsed_ms),
        )

    def __repr__(self) -> str:
        return "OpenAICompatibleProviderTester(target=<redacted>)"


class LocalProviderTesterRegistry:
    """Resolve immutable deployment-owned connection IDs to safe provider testers."""

    __slots__ = ("_testers",)

    def __init__(self, testers: Mapping[str, Callable[[memoryview], ProviderTestResult]]) -> None:
        checked: dict[str, Callable[[memoryview], ProviderTestResult]] = {}
        for connection_id, tester in testers.items():
            if _CONNECTION_ID.fullmatch(connection_id) is None or not callable(tester):
                raise ValueError("local_secret.provider_registry_invalid")
            checked[connection_id] = tester
        self._testers = MappingProxyType(checked)

    @classmethod
    def from_base_urls(
        cls,
        values: Mapping[str, str],
        *,
        timeout_seconds: float = 5.0,
    ) -> LocalProviderTesterRegistry:
        return cls(
            {
                connection_id: OpenAICompatibleProviderTester(
                    base_url, timeout_seconds=timeout_seconds
                )
                for connection_id, base_url in values.items()
            }
        )

    def __call__(self, provider_connection_id: str, secret: memoryview) -> ProviderTestResult:
        tester = self._testers.get(provider_connection_id)
        if tester is None:
            return ProviderTestResult("unsupported", (), "lt_100ms")
        return tester(secret)

    def __repr__(self) -> str:
        return f"LocalProviderTesterRegistry(target_count={len(self._testers)})"


def _status_outcome(status_code: int, is_redirect: bool) -> str:
    if is_redirect:
        return "provider_unavailable"
    if 200 <= status_code <= 299:
        return "succeeded"
    if status_code == 401:
        return "authentication_failed"
    if status_code == 403:
        return "authorization_failed"
    if status_code == 429:
        return "rate_limited"
    if status_code in {408, 504}:
        return "timeout"
    if status_code in {404, 405, 501}:
        return "unsupported"
    return "provider_unavailable"


def _latency_bucket(elapsed_ms: float) -> str:
    if elapsed_ms < 100:
        return "lt_100ms"
    if elapsed_ms < 500:
        return "100_499ms"
    if elapsed_ms < 2_000:
        return "500_1999ms"
    return "gte_2000ms"


def _caused_by_ssl(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            return False
        if isinstance(current, ssl.SSLError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


__all__ = ("LocalProviderTesterRegistry", "OpenAICompatibleProviderTester")
