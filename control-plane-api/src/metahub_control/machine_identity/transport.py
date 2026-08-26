"""Trusted HTTP facts for PRD-118 machine authentication."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Mapping

from fastapi import Request
from metahub_shared.runtime.errors import ApiError

_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")


def direct_peer_ip(request: Request) -> str | None:
    """Return only the ASGI peer address; ordinary forwarding headers are untrusted."""

    if request.client is None or not request.client.host:
        return None
    try:
        return str(ipaddress.ip_address(request.client.host))
    except ValueError:
        return None


def verified_mtls_fingerprint(request: Request) -> str | None:
    """Restore a leaf-certificate fingerprint only from a verified ASGI TLS extension."""

    extensions = request.scope.get("extensions")
    if not isinstance(extensions, Mapping):
        return None
    tls = extensions.get("tls")
    if not isinstance(tls, Mapping) or tls.get("client_cert_error"):
        return None
    supplied = tls.get("client_cert_sha256")
    if isinstance(supplied, str):
        normalized = supplied.strip().lower()
        if _HEX_64_RE.fullmatch(normalized) is not None:
            return normalized
        return None
    chain = tls.get("client_cert_chain")
    if (
        isinstance(chain, (list, tuple))
        and chain
        and isinstance(chain[0], bytes)
        and chain[0]
    ):
        return hashlib.sha256(chain[0]).hexdigest()
    return None


def optional_machine_bearer(request: Request) -> str | None:
    """Return one machine Bearer or fail closed when its Authorization is ambiguous."""

    values = [
        value
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"authorization"
    ]
    decoded: list[str] = []
    for value in values:
        try:
            decoded.append(value.decode("ascii", errors="strict"))
        except UnicodeDecodeError:
            if b"mat_" in value.lower():
                raise ApiError("machine_auth.invalid") from None
            return None
    candidates = [value for value in decoded if re.match(r"(?i)^Bearer\s+mat_", value)]
    if not candidates:
        return None
    if len(values) != 1 or len(candidates) != 1:
        raise ApiError("machine_auth.invalid")
    match = re.fullmatch(r"(?i)Bearer (mat_[A-Za-z0-9_-]{32,80})", candidates[0])
    if match is None:
        raise ApiError("machine_auth.invalid")
    return match.group(1)


def reject_machine_cookies(request: Request) -> None:
    """Machine routes accept one Bearer domain and no ambient browser credential."""

    if any(
        name.lower() == b"cookie" and value.strip()
        for name, value in request.scope.get("headers", ())
    ):
        raise ApiError("machine_auth.invalid")


__all__ = [
    "direct_peer_ip",
    "optional_machine_bearer",
    "reject_machine_cookies",
    "verified_mtls_fingerprint",
]
