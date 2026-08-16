"""HTTPX transport that connects only to a previously authorized IP."""

from __future__ import annotations

import ssl
from typing import Any

import httpx

_AVAILABILITY_REASONS = frozenset(
    {
        "connect_unavailable",
        "tls_unavailable",
        "headers_timeout",
        "body_timeout",
        "overall_timeout",
    }
)


class NetworkStageUnavailable(RuntimeError):
    """A fixed availability stage without upstream exception text."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = (
            reason_code if reason_code in _AVAILABILITY_REASONS else "connect_unavailable"
        )
        super().__init__("mcp.server_unavailable")


class _ObservedResponseStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream) -> None:
        self._inner = inner

    async def __aiter__(self) -> Any:
        try:
            async for chunk in self._inner:
                yield chunk
        except httpx.ReadTimeout:
            raise NetworkStageUnavailable("body_timeout") from None
        except httpx.HTTPError:
            raise NetworkStageUnavailable("body_timeout") from None

    async def aclose(self) -> None:
        await self._inner.aclose()


def _has_tls_cause(error: BaseException) -> bool:
    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


class PinnedAsyncTransport(httpx.AsyncBaseTransport):
    """Rewrite the connection origin to one IP while retaining Host and TLS SNI."""

    def __init__(
        self,
        *,
        scheme: str,
        hostname: str,
        port: int,
        pinned_ip: str,
    ) -> None:
        self._scheme = scheme
        self._hostname = hostname
        self._port = port
        self._pinned_ip = pinned_ip
        self._inner = httpx.AsyncHTTPTransport(
            retries=0,
            http2=False,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=0,
                keepalive_expiry=0,
            ),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        default_port = 443 if self._scheme == "https" else 80
        if (
            request.url.scheme != self._scheme
            or request.url.host != self._hostname
            or (request.url.port or default_port) != self._port
        ):
            raise httpx.UnsupportedProtocol("mcp.network_blocked")
        pinned_url = request.url.copy_with(host=self._pinned_ip)
        host = self._hostname
        if self._port != default_port:
            host = f"{host}:{self._port}"
        headers = [(key, value) for key, value in request.headers.raw if key.lower() != b"host"]
        headers.append((b"host", host.encode("ascii", errors="strict")))
        extensions: dict[str, Any] = dict(request.extensions)
        extensions["sni_hostname"] = self._hostname
        pinned_request = httpx.Request(
            request.method,
            pinned_url,
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        try:
            response = await self._inner.handle_async_request(pinned_request)
        except httpx.ConnectTimeout:
            raise NetworkStageUnavailable("connect_unavailable") from None
        except httpx.ConnectError as exc:
            raise NetworkStageUnavailable(
                "tls_unavailable" if _has_tls_cause(exc) else "connect_unavailable"
            ) from None
        except httpx.ReadTimeout:
            raise NetworkStageUnavailable("headers_timeout") from None
        except httpx.HTTPError:
            raise NetworkStageUnavailable("connect_unavailable") from None
        response.stream = _ObservedResponseStream(response.stream)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


__all__ = ["NetworkStageUnavailable", "PinnedAsyncTransport"]
