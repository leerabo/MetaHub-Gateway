"""Deployment-owned network boundary for local model Endpoint calls."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from metahub_shared.runtime.config import LOCAL_ENDPOINT_BLOCKED_ADDRESSES


class LocalEndpointNetworkError(RuntimeError):
    """A fixed local Endpoint preflight failure without target or resolver text."""

    def __init__(self, code: str) -> None:
        self.code = (
            code
            if code in {"gateway.local_endpoint_blocked", "gateway.local_endpoint_unavailable"}
            else "gateway.local_endpoint_unavailable"
        )
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class LocalEndpointTarget:
    scheme: str
    hostname: str
    port: int
    pinned_ip: str


def _has_tls_cause(error: BaseException) -> bool:
    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


class PinnedLocalEndpointTransport(httpx.AsyncBaseTransport):
    """Connect to one approved IP while retaining the original Host and TLS SNI."""

    def __init__(self, target: LocalEndpointTarget) -> None:
        self._target = target
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
        target = self._target
        default_port = 443 if target.scheme == "https" else 80
        if (
            request.url.scheme != target.scheme
            or request.url.host.rstrip(".").lower() != target.hostname
            or (request.url.port or default_port) != target.port
        ):
            raise LocalEndpointNetworkError("gateway.local_endpoint_blocked")
        pinned_url = request.url.copy_with(host=target.pinned_ip)
        host = target.hostname
        if ":" in host:
            host = f"[{host}]"
        if target.port != default_port:
            host = f"{host}:{target.port}"
        headers = [
            (key, value)
            for key, value in request.headers.raw
            if key.lower() != b"host"
        ]
        headers.append((b"host", host.encode("idna")))
        extensions: dict[str, Any] = dict(request.extensions)
        extensions["sni_hostname"] = target.hostname
        pinned_request = httpx.Request(
            request.method,
            pinned_url,
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        try:
            return await self._inner.handle_async_request(pinned_request)
        except httpx.ConnectError as exc:
            # TLS and TCP remain the same public availability class; the distinction is only
            # retained as a fixed internal cause and never includes the target or exception text.
            code = (
                "gateway.local_endpoint_unavailable"
                if _has_tls_cause(exc)
                else "gateway.local_endpoint_unavailable"
            )
            raise LocalEndpointNetworkError(code) from None
        except httpx.HTTPError:
            raise LocalEndpointNetworkError("gateway.local_endpoint_unavailable") from None

    async def aclose(self) -> None:
        await self._inner.aclose()


async def resolve_local_endpoint_target(
    url: str,
    *,
    private_cidrs: list[str],
    allowed_ports: list[int],
    allow_loopback: bool,
) -> LocalEndpointTarget:
    """Resolve and validate all A/AAAA answers, then return one immutable pinned target."""

    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LocalEndpointNetworkError("gateway.local_endpoint_blocked")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise LocalEndpointNetworkError("gateway.local_endpoint_blocked") from None
    if port not in set(allowed_ports):
        raise LocalEndpointNetworkError("gateway.local_endpoint_blocked")
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            parsed.hostname,
            port,
            0,
            socket.SOCK_STREAM,
        )
    except OSError:
        raise LocalEndpointNetworkError("gateway.local_endpoint_unavailable") from None
    if not infos:
        raise LocalEndpointNetworkError("gateway.local_endpoint_unavailable")
    allow_networks = [ipaddress.ip_network(value, strict=False) for value in private_cidrs]
    addresses: list[str] = []
    for info in infos:
        resolved = ipaddress.ip_address(info[4][0])
        address = (
            resolved.ipv4_mapped
            if isinstance(resolved, ipaddress.IPv6Address)
            and resolved.ipv4_mapped is not None
            else resolved
        )
        if (
            address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or (address.is_loopback and not allow_loopback)
            or address in LOCAL_ENDPOINT_BLOCKED_ADDRESSES
        ):
            raise LocalEndpointNetworkError("gateway.local_endpoint_blocked")
        if not (allow_loopback and address.is_loopback) and not any(
            address in network for network in allow_networks
        ):
            raise LocalEndpointNetworkError("gateway.local_endpoint_blocked")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
        if len(addresses) > 8:
            raise LocalEndpointNetworkError("gateway.local_endpoint_blocked")
    addresses.sort(key=lambda value: (ipaddress.ip_address(value).version, value))
    return LocalEndpointTarget(
        scheme=parsed.scheme,
        hostname=parsed.hostname.rstrip(".").lower(),
        port=port,
        pinned_ip=addresses[0],
    )


__all__ = [
    "LocalEndpointNetworkError",
    "LocalEndpointTarget",
    "PinnedLocalEndpointTransport",
    "resolve_local_endpoint_target",
]
