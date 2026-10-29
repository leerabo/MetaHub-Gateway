"""Short-lived local administrator sessions and management network policy."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from time import time
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TOKEN = re.compile(r"^las1\.([A-Za-z0-9_-]{32,2048})\.([A-Za-z0-9_-]{43})$")
_CSRF = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ID = re.compile(r"^[a-z][a-z0-9_:-]{2,79}$")
_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class LocalAdminAuthError(RuntimeError):
    """Fixed local authentication failure without token or identity material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class _HandoffFailureState:
    failures: int
    window_started_at: int
    locked_until: int
    updated_at: int


class LocalAdminHandoffVerifier:
    """Digest-only verifier with bounded per-client failure state."""

    __slots__ = (
        "_credential_digest",
        "_failure_window_seconds",
        "_lock",
        "_lockout_seconds",
        "_max_clients",
        "_max_failures",
        "_now",
        "_states",
    )

    def __init__(
        self,
        credential: bytes,
        *,
        now: Callable[[], int] = lambda: int(time()),
        max_failures: int = 5,
        failure_window_seconds: int = 60,
        lockout_seconds: int = 300,
        max_clients: int = 256,
    ) -> None:
        if (
            type(credential) is not bytes
            or not _valid_handoff_credential(credential)
            or not callable(now)
            or type(max_failures) is not int
            or not 2 <= max_failures <= 20
            or type(failure_window_seconds) is not int
            or not 10 <= failure_window_seconds <= 600
            or type(lockout_seconds) is not int
            or not 30 <= lockout_seconds <= 3600
            or type(max_clients) is not int
            or not 16 <= max_clients <= 4096
        ):
            raise LocalAdminAuthError("local_admin.configuration_unavailable")
        self._credential_digest = hashlib.sha256(credential).digest()
        self._now = now
        self._max_failures = max_failures
        self._failure_window_seconds = failure_window_seconds
        self._lockout_seconds = lockout_seconds
        self._max_clients = max_clients
        self._states: OrderedDict[str, _HandoffFailureState] = OrderedDict()
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "LocalAdminHandoffVerifier()"

    def verify(self, candidate: bytearray, *, client_id: str) -> None:
        if type(candidate) is not bytearray:
            raise LocalAdminAuthError("local_admin.authentication_failed")
        try:
            normalized_client = str(ipaddress.ip_address(client_id))
        except ValueError:
            raise LocalAdminAuthError("local_admin.authentication_failed") from None
        now = self._now()
        if type(now) is not int:
            raise LocalAdminAuthError("local_admin.authentication_failed")

        supplied_digest = hashlib.sha256(candidate).digest()
        format_valid = _valid_handoff_credential(candidate)
        digest_matches = hmac.compare_digest(supplied_digest, self._credential_digest)
        accepted = format_valid and digest_matches
        with self._lock:
            state = self._states.get(normalized_client)
            if state is not None and state.locked_until > now:
                state.updated_at = now
                self._states.move_to_end(normalized_client)
                raise LocalAdminAuthError("local_admin.rate_limited")
            if accepted:
                self._states.pop(normalized_client, None)
                return
            self._record_failure(normalized_client, state, now)

    def _record_failure(
        self,
        client_id: str,
        state: _HandoffFailureState | None,
        now: int,
    ) -> None:
        if state is None or now - state.window_started_at >= self._failure_window_seconds:
            state = _HandoffFailureState(1, now, 0, now)
            self._states[client_id] = state
        else:
            state.failures += 1
            state.updated_at = now
        if state.failures >= self._max_failures:
            state.locked_until = now + self._lockout_seconds
        self._states.move_to_end(client_id)
        while len(self._states) > self._max_clients:
            self._states.popitem(last=False)
        code = (
            "local_admin.rate_limited"
            if state.locked_until > now
            else "local_admin.authentication_failed"
        )
        raise LocalAdminAuthError(code)


class LocalAdminClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["1"]
    session_id: str = Field(pattern=r"^las_[0-9a-f]{32}$")
    account_id: str = Field(min_length=3, max_length=80)
    tenant_id: str = Field(min_length=3, max_length=80)
    workspace_id: str = Field(min_length=3, max_length=80)
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    role: Literal["tenant_security_admin"]
    csrf_sha256: str = Field(pattern=_HEX_64.pattern, repr=False)
    issued_at: int = Field(ge=1)
    expires_at: int = Field(ge=1)
    step_up_expires_at: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_claims(self) -> LocalAdminClaims:
        if any(
            _ID.fullmatch(value) is None
            for value in (self.account_id, self.tenant_id, self.workspace_id)
        ):
            raise ValueError("local_admin.claims_invalid")
        if (
            self.expires_at <= self.issued_at
            or self.expires_at - self.issued_at > 900
            or not self.issued_at <= self.step_up_expires_at <= self.expires_at
        ):
            raise ValueError("local_admin.claims_invalid")
        return self


@dataclass(frozen=True, slots=True)
class LocalManagementPolicy:
    allowed_origins: frozenset[str]
    allowed_client_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    allow_loopback: bool = True

    def __post_init__(self) -> None:
        if not self.allowed_origins or any(
            not _valid_origin(origin) for origin in self.allowed_origins
        ):
            raise LocalAdminAuthError("local_admin.policy_invalid")
        if type(self.allow_loopback) is not bool or any(
            not isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network))
            for network in self.allowed_client_networks
        ):
            raise LocalAdminAuthError("local_admin.policy_invalid")

    @classmethod
    def build(
        cls,
        *,
        allowed_origins: tuple[str, ...],
        allowed_client_cidrs: tuple[str, ...] = (),
        allow_loopback: bool = True,
    ) -> LocalManagementPolicy:
        try:
            networks = tuple(
                ipaddress.ip_network(value, strict=True) for value in allowed_client_cidrs
            )
        except ValueError:
            raise LocalAdminAuthError("local_admin.policy_invalid") from None
        return cls(frozenset(allowed_origins), networks, allow_loopback)

    def permits_client(self, value: str) -> bool:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        return (self.allow_loopback and address.is_loopback) or any(
            address in network for network in self.allowed_client_networks
        )


@dataclass(frozen=True, slots=True)
class LocalAdminSessionSigner:
    key: bytes = field(repr=False)
    now: Callable[[], int] = field(default=lambda: int(time()), repr=False)

    def __post_init__(self) -> None:
        if type(self.key) is not bytes or len(self.key) != 32 or not callable(self.now):
            raise LocalAdminAuthError("local_admin.configuration_unavailable")

    def issue(self, claims: LocalAdminClaims) -> str:
        if type(claims) is not LocalAdminClaims:
            raise LocalAdminAuthError("local_admin.claims_invalid")
        payload = _canonical(claims.model_dump(mode="json"))
        encoded = _b64(payload)
        signature = hmac.digest(self.key, b"las1." + encoded.encode("ascii"), "sha256")
        return f"las1.{encoded}.{_b64(signature)}"

    def verify(
        self,
        token: object,
        *,
        csrf_token: object,
        deployment_id: str,
        require_step_up: bool = True,
    ) -> LocalAdminClaims:
        if type(token) is not str or type(csrf_token) is not str:
            raise LocalAdminAuthError("local_admin.authentication_failed")
        match = _TOKEN.fullmatch(token)
        if match is None or _CSRF.fullmatch(csrf_token) is None:
            raise LocalAdminAuthError("local_admin.authentication_failed")
        encoded, supplied_signature = match.groups()
        expected = hmac.digest(self.key, b"las1." + encoded.encode("ascii"), "sha256")
        try:
            signature = _unb64(supplied_signature)
        except ValueError:
            raise LocalAdminAuthError("local_admin.authentication_failed") from None
        if not hmac.compare_digest(signature, expected):
            raise LocalAdminAuthError("local_admin.authentication_failed")
        try:
            claims = LocalAdminClaims.model_validate_json(_unb64(encoded))
        except (ValueError, TypeError):
            raise LocalAdminAuthError("local_admin.authentication_failed") from None
        now = self.now()
        if (
            type(now) is not int
            or claims.deployment_id != deployment_id
            or now < claims.issued_at - 30
            or now >= claims.expires_at
            or (require_step_up and now >= claims.step_up_expires_at)
            or not hmac.compare_digest(
                claims.csrf_sha256,
                hashlib.sha256(csrf_token.encode("ascii")).hexdigest(),
            )
        ):
            raise LocalAdminAuthError("local_admin.authentication_failed")
        return claims


def csrf_sha256(value: str) -> str:
    if type(value) is not str or _CSRF.fullmatch(value) is None:
        raise LocalAdminAuthError("local_admin.csrf_invalid")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise LocalAdminAuthError("local_admin.claims_invalid") from None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if "=" in value:
        raise ValueError
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _valid_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and (port is None or port >= 1)
        and (parsed.scheme == "https" or loopback)
    )


def _valid_handoff_credential(value: bytes | bytearray) -> bool:
    return 32 <= len(value) <= 512 and all(33 <= character <= 126 for character in value)


__all__ = (
    "LocalAdminAuthError",
    "LocalAdminClaims",
    "LocalAdminHandoffVerifier",
    "LocalAdminSessionSigner",
    "LocalManagementPolicy",
    "csrf_sha256",
)
