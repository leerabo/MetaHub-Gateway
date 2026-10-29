"""DNS resolution and fail-closed egress decisions for the MCP sandbox."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from metahub_shared.mcp import (
    McpContractError,
    McpNetworkAcl,
    evaluate_mcp_network_destination,
)

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_METADATA = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
    }
)
_BLOCK_REASONS = frozenset(
    {
        "policy_disabled",
        "address_denied",
        "mixed_dns_answers",
        "port_denied",
        "redirect_denied",
    }
)


class AclConfig(Protocol):
    mcp_sandbox_dns_timeout_seconds: int
    mcp_sandbox_max_dns_answers: int
    mcp_sandbox_private_cidrs: tuple[str, ...]
    mcp_sandbox_deny_cidrs: tuple[str, ...]
    mcp_sandbox_allowed_ports: tuple[int, ...]
    mcp_sandbox_deny_ports: tuple[int, ...]


class NetworkBlocked(RuntimeError):
    """A fixed ACL decision without the destination or resolved addresses."""

    def __init__(self, reason_code: str, *, address_count: int = 0) -> None:
        self.reason_code = reason_code if reason_code in _BLOCK_REASONS else "address_denied"
        self.address_count = max(0, address_count)
        super().__init__("mcp.network_blocked")


class NetworkUnavailable(RuntimeError):
    """A fixed DNS availability failure that contributes to the circuit breaker."""

    def __init__(self) -> None:
        self.reason_code = "dns_unavailable"
        super().__init__("mcp.server_unavailable")


@dataclass(frozen=True, slots=True)
class AclDecision:
    scheme: str
    hostname: str = field(repr=False)
    port: int
    pinned_ip: str = field(repr=False)
    address_count: int


async def authorize_endpoint(
    endpoint: str,
    *,
    policy_status: str,
    egress_mode: str,
    config: AclConfig,
) -> AclDecision:
    """Resolve once, validate every answer, and return one pinned destination."""

    if policy_status != "active":
        raise NetworkBlocked("policy_disabled")
    if egress_mode != "registered_endpoint_only":
        raise NetworkBlocked("address_denied")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError):
        raise NetworkBlocked("address_denied") from None
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise NetworkBlocked("address_denied")
    compiled_acl = getattr(config, "network_acl", None)
    if isinstance(compiled_acl, McpNetworkAcl):
        allowed_ports = compiled_acl.allowed_ports
        denied_ports = compiled_acl.deny_ports
    else:
        allowed_ports = frozenset(config.mcp_sandbox_allowed_ports)
        denied_ports = frozenset(config.mcp_sandbox_deny_ports)
    if port in denied_ports or port not in allowed_ports:
        raise NetworkBlocked("port_denied")

    addresses = await _resolve(hostname, port, config=config)
    if isinstance(compiled_acl, McpNetworkAcl):
        try:
            evaluated = evaluate_mcp_network_destination(
                endpoint,
                tuple(str(item) for item in addresses),
                acl=compiled_acl,
            )
        except McpContractError:
            raise NetworkBlocked("address_denied", address_count=len(addresses)) from None
        if not evaluated.allowed:
            raise NetworkBlocked(
                evaluated.reason_code or "address_denied",
                address_count=evaluated.address_count,
            )
        selected = sorted(addresses, key=lambda item: (item.version, item.packed))[0]
        return AclDecision(
            scheme=parsed.scheme,
            hostname=hostname,
            port=port,
            pinned_ip=str(selected),
            address_count=evaluated.address_count,
        )
    private_exceptions = _networks(config.mcp_sandbox_private_cidrs, exact=True)
    deny_networks = _networks(config.mcp_sandbox_deny_cidrs, exact=False)
    allowed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    denied = 0
    for address in addresses:
        if _address_denied(
            address,
            private_exceptions=private_exceptions,
            deny_networks=deny_networks,
        ):
            denied += 1
        else:
            allowed.append(address)
    if denied and allowed:
        raise NetworkBlocked("mixed_dns_answers", address_count=len(addresses))
    if denied or not allowed:
        raise NetworkBlocked("address_denied", address_count=len(addresses))
    selected = sorted(allowed, key=lambda item: (item.version, item.packed))[0]
    return AclDecision(
        scheme=parsed.scheme,
        hostname=hostname,
        port=port,
        pinned_ip=str(selected),
        address_count=len(addresses),
    )


async def _resolve(
    hostname: str, port: int, *, config: AclConfig
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return (_normalize_address(literal),)
    query = hostname if hostname.endswith(".") else hostname + "."
    try:
        async with asyncio.timeout(
            int(
                getattr(
                    config,
                    "dns_timeout_seconds",
                    getattr(config, "mcp_sandbox_dns_timeout_seconds", 2),
                )
            )
        ):
            answers = await asyncio.get_running_loop().getaddrinfo(
                query,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
                flags=socket.AI_ADDRCONFIG,
            )
    except (TimeoutError, OSError, UnicodeError):
        raise NetworkUnavailable from None
    result: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        for answer in answers:
            result.add(_normalize_address(ipaddress.ip_address(answer[4][0])))
    except (IndexError, TypeError, ValueError):
        raise NetworkUnavailable from None
    if not result:
        raise NetworkUnavailable
    compiled_acl = getattr(config, "network_acl", None)
    maximum = (
        compiled_acl.max_dns_answers
        if isinstance(compiled_acl, McpNetworkAcl)
        else int(getattr(config, "mcp_sandbox_max_dns_answers", 8))
    )
    if len(result) > maximum:
        raise NetworkBlocked("address_denied", address_count=maximum)
    return tuple(sorted(result, key=lambda item: (item.version, item.packed)))


def _normalize_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        raise NetworkBlocked("address_denied")
    return address


def _address_denied(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    private_exceptions: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    deny_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if address in _METADATA or address in _CGNAT:
        return True
    if any(address in network for network in deny_networks):
        return True
    if (
        address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        return True
    if address.is_private:
        return not any(address in network for network in private_exceptions)
    return not address.is_global


def _networks(
    values: tuple[str, ...], *, exact: bool
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    result: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    try:
        for value in values:
            network = ipaddress.ip_network(value, strict=True)
            if exact and network.prefixlen != network.max_prefixlen:
                raise ValueError
            result.append(network)
    except (TypeError, ValueError):
        raise NetworkUnavailable from None
    return tuple(result)
