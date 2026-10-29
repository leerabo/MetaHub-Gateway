"""Minimal fail-closed configuration loader for the isolated MCP Sandbox Worker.

This module deliberately does not import or call ``load_app_config``.  The Worker
therefore has no path to database, Redis, Provider, Webhook, object-storage, or
host-directory settings even when those variables exist in the process environment.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from metahub_shared.mcp import (
    McpContractError,
    McpNetworkAcl,
    McpNetworkPolicyCaps,
    McpVersionedSecret,
    compile_mcp_network_acl,
    validate_mcp_keyring,
)


class SandboxConfigError(ValueError):
    """Fixed startup failure that never includes a submitted secret or network target."""

    def __init__(self, code: str, message: str, suggestion: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion


_ALLOWED_KEYS = frozenset(
    {
        "METAHUB_MCP_SANDBOX_WORKER_ENABLED",
        "METAHUB_MCP_ENCRYPTION_VERSION",
        "METAHUB_MCP_ENCRYPTION_SECRET",
        "METAHUB_MCP_ENCRYPTION_PREVIOUS_VERSION",
        "METAHUB_MCP_ENCRYPTION_PREVIOUS_SECRET",
        "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN",
        "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET",
        "METAHUB_MCP_SANDBOX_PERMIT_SECRET",
        "METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION",
        "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT",
        "METAHUB_MCP_NETWORK_CONNECT_TIMEOUT_MAX_SECONDS",
        "METAHUB_MCP_NETWORK_PROBE_TIMEOUT_MAX_SECONDS",
        "METAHUB_MCP_NETWORK_CALL_TIMEOUT_MAX_SECONDS",
        "METAHUB_MCP_NETWORK_FAILURE_THRESHOLD_MAX",
        "METAHUB_MCP_NETWORK_OPEN_SECONDS_MAX",
        "METAHUB_MCP_SANDBOX_PRIVATE_CIDRS",
        "METAHUB_MCP_SANDBOX_DENY_CIDRS",
        "METAHUB_MCP_SANDBOX_ALLOWED_PORTS",
        "METAHUB_MCP_SANDBOX_DENY_PORTS",
        "METAHUB_MCP_SANDBOX_DNS_TIMEOUT_SECONDS",
        "METAHUB_MCP_SANDBOX_TLS_TIMEOUT_SECONDS",
        "METAHUB_MCP_SANDBOX_HEADERS_TIMEOUT_SECONDS",
        "METAHUB_MCP_SANDBOX_BODY_IDLE_TIMEOUT_SECONDS",
        "METAHUB_MCP_SANDBOX_MAX_DNS_ANSWERS",
        "METAHUB_MCP_SANDBOX_MAX_ACTIVE_SESSIONS",
        "METAHUB_MCP_SANDBOX_CLEANUP_TIMEOUT_SECONDS",
    }
)
_DEFAULTS = {
    "METAHUB_MCP_SANDBOX_WORKER_ENABLED": "false",
    "METAHUB_MCP_ENCRYPTION_VERSION": "1",
    "METAHUB_MCP_ENCRYPTION_SECRET": "",
    "METAHUB_MCP_ENCRYPTION_PREVIOUS_VERSION": "",
    "METAHUB_MCP_ENCRYPTION_PREVIOUS_SECRET": "",
    "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN": "",
    "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET": "",
    "METAHUB_MCP_SANDBOX_PERMIT_SECRET": "",
    "METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION": "1",
    "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT": "",
    "METAHUB_MCP_NETWORK_CONNECT_TIMEOUT_MAX_SECONDS": "5",
    "METAHUB_MCP_NETWORK_PROBE_TIMEOUT_MAX_SECONDS": "10",
    "METAHUB_MCP_NETWORK_CALL_TIMEOUT_MAX_SECONDS": "30",
    "METAHUB_MCP_NETWORK_FAILURE_THRESHOLD_MAX": "10",
    "METAHUB_MCP_NETWORK_OPEN_SECONDS_MAX": "3600",
    "METAHUB_MCP_SANDBOX_PRIVATE_CIDRS": "",
    "METAHUB_MCP_SANDBOX_DENY_CIDRS": "",
    "METAHUB_MCP_SANDBOX_ALLOWED_PORTS": "443",
    "METAHUB_MCP_SANDBOX_DENY_PORTS": "",
    "METAHUB_MCP_SANDBOX_DNS_TIMEOUT_SECONDS": "2",
    "METAHUB_MCP_SANDBOX_TLS_TIMEOUT_SECONDS": "5",
    "METAHUB_MCP_SANDBOX_HEADERS_TIMEOUT_SECONDS": "10",
    "METAHUB_MCP_SANDBOX_BODY_IDLE_TIMEOUT_SECONDS": "10",
    "METAHUB_MCP_SANDBOX_MAX_DNS_ANSWERS": "8",
    "METAHUB_MCP_SANDBOX_MAX_ACTIVE_SESSIONS": "16",
    "METAHUB_MCP_SANDBOX_CLEANUP_TIMEOUT_SECONDS": "2",
}


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """The complete and intentionally small Sandbox Worker configuration."""

    worker_enabled: bool
    encryption_version: int
    encryption_secret: str = field(repr=False)
    encryption_previous_version: int | None = None
    encryption_previous_secret: str | None = field(default=None, repr=False)
    internal_token: str = field(default="", repr=False)
    envelope_secret: str = field(default="", repr=False)
    permit_secret: str = field(default="", repr=False)
    response_key_version: int = 1
    response_secret_current: str = field(default="", repr=False)
    network_policy_caps: McpNetworkPolicyCaps = McpNetworkPolicyCaps()
    network_acl: McpNetworkAcl = field(default_factory=McpNetworkAcl, repr=False)
    dns_timeout_seconds: int = 2
    tls_timeout_seconds: int = 5
    headers_timeout_seconds: int = 10
    body_idle_timeout_seconds: int = 10
    max_dns_answers: int = 8
    max_active_sessions: int = 16
    cleanup_timeout_seconds: int = 2

    @property
    def encryption_keyring(self) -> tuple[McpVersionedSecret, ...]:
        current = McpVersionedSecret(self.encryption_version, self.encryption_secret)
        if self.encryption_previous_version is None:
            return (current,)
        assert self.encryption_previous_secret is not None
        return (
            current,
            McpVersionedSecret(self.encryption_previous_version, self.encryption_previous_secret),
        )

    @property
    def caps_fingerprint(self) -> str:
        return self.network_policy_caps.fingerprint


def load_mcp_sandbox_config(
    environ: Mapping[str, str] | None = None,
) -> SandboxConfig:
    """Load only the Sandbox allowlist and reject every undeclared MetaHub variable."""

    source = os.environ if environ is None else environ
    unknown = sorted(
        key for key in source if key.startswith("METAHUB_") and key not in _ALLOWED_KEYS
    )
    if unknown:
        raise SandboxConfigError(
            code="infra.config_invalid",
            message="An unsupported MCP Sandbox configuration key is present.",
            suggestion="Remove Control, Scheduler, and undeclared MCP settings from the Worker.",
        )
    values = {key: source.get(key, default) for key, default in _DEFAULTS.items()}
    if _boolean(values, "METAHUB_MCP_SANDBOX_WORKER_ENABLED") is not True:
        raise SandboxConfigError(
            code="infra.config_invalid",
            message="The MCP Sandbox Worker role is not explicitly enabled.",
            suggestion="Enable the Worker only in its isolated deployment profile.",
        )

    encryption_version = _integer(
        values, "METAHUB_MCP_ENCRYPTION_VERSION", minimum=1, maximum=2_147_483_647
    )
    encryption_secret = _required(values, "METAHUB_MCP_ENCRYPTION_SECRET")
    previous_version = _optional_integer(values, "METAHUB_MCP_ENCRYPTION_PREVIOUS_VERSION")
    previous_secret = _optional(values, "METAHUB_MCP_ENCRYPTION_PREVIOUS_SECRET")
    if (previous_version is None) != (previous_secret is None) or (
        previous_version is not None and previous_version >= encryption_version
    ):
        raise SandboxConfigError(
            code="infra.config_invalid",
            message="MCP Sandbox encryption keyring is invalid.",
            suggestion="Use one current key and at most one complete older key pair.",
        )

    internal_token = _required(values, "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN")
    envelope_secret = _required(values, "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET")
    permit_secret = _required(values, "METAHUB_MCP_SANDBOX_PERMIT_SECRET")
    response_version = _integer(
        values,
        "METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION",
        minimum=1,
        maximum=2_147_483_647,
    )
    response_secret = _required(values, "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT")
    secrets = (
        encryption_secret,
        previous_secret,
        internal_token,
        envelope_secret,
        permit_secret,
        response_secret,
    )
    _validate_independent_secrets(secrets)
    keyring = [McpVersionedSecret(encryption_version, encryption_secret)]
    if previous_version is not None and previous_secret is not None:
        keyring.append(McpVersionedSecret(previous_version, previous_secret))
    try:
        validate_mcp_keyring(tuple(keyring))
    except McpContractError:
        raise SandboxConfigError(
            code="infra.config_invalid",
            message="MCP Sandbox encryption keyring is invalid.",
            suggestion="Use bounded, distinct current and previous encryption roots.",
        ) from None

    caps = McpNetworkPolicyCaps(
        connect_timeout_max_seconds=_integer(
            values, "METAHUB_MCP_NETWORK_CONNECT_TIMEOUT_MAX_SECONDS", minimum=1, maximum=10
        ),
        probe_timeout_max_seconds=_integer(
            values, "METAHUB_MCP_NETWORK_PROBE_TIMEOUT_MAX_SECONDS", minimum=1, maximum=30
        ),
        call_timeout_max_seconds=_integer(
            values, "METAHUB_MCP_NETWORK_CALL_TIMEOUT_MAX_SECONDS", minimum=1, maximum=120
        ),
        failure_threshold_max=_integer(
            values, "METAHUB_MCP_NETWORK_FAILURE_THRESHOLD_MAX", minimum=1, maximum=10
        ),
        open_seconds_max=_integer(
            values, "METAHUB_MCP_NETWORK_OPEN_SECONDS_MAX", minimum=10, maximum=3600
        ),
    )
    max_dns_answers = _integer(values, "METAHUB_MCP_SANDBOX_MAX_DNS_ANSWERS", minimum=1, maximum=16)
    try:
        acl = compile_mcp_network_acl(
            private_cidrs=_csv(values["METAHUB_MCP_SANDBOX_PRIVATE_CIDRS"]),
            deny_cidrs=_csv(values["METAHUB_MCP_SANDBOX_DENY_CIDRS"]),
            allowed_ports=_ports(values["METAHUB_MCP_SANDBOX_ALLOWED_PORTS"]),
            deny_ports=_ports(values["METAHUB_MCP_SANDBOX_DENY_PORTS"], allow_empty=True),
            max_dns_answers=max_dns_answers,
        )
    except McpContractError:
        raise SandboxConfigError(
            code="infra.config_invalid",
            message="MCP Sandbox ACL configuration is invalid.",
            suggestion="Use canonical exact private hosts, CIDRs, and non-overlapping ports.",
        ) from None

    dns_timeout = _integer(values, "METAHUB_MCP_SANDBOX_DNS_TIMEOUT_SECONDS", minimum=1, maximum=5)
    tls_timeout = _integer(values, "METAHUB_MCP_SANDBOX_TLS_TIMEOUT_SECONDS", minimum=1, maximum=10)
    headers_timeout = _integer(
        values, "METAHUB_MCP_SANDBOX_HEADERS_TIMEOUT_SECONDS", minimum=1, maximum=30
    )
    body_idle_timeout = _integer(
        values, "METAHUB_MCP_SANDBOX_BODY_IDLE_TIMEOUT_SECONDS", minimum=1, maximum=30
    )
    overall_minimum = min(caps.probe_timeout_max_seconds, caps.call_timeout_max_seconds)
    if (
        max(
            dns_timeout,
            caps.connect_timeout_max_seconds,
            tls_timeout,
            headers_timeout,
            body_idle_timeout,
        )
        > overall_minimum
    ):
        raise SandboxConfigError(
            code="infra.config_invalid",
            message="MCP Sandbox phase deadlines exceed the shared overall cap.",
            suggestion="Keep DNS, connect, TLS, headers, and body idle within both overalls.",
        )

    return SandboxConfig(
        worker_enabled=True,
        encryption_version=encryption_version,
        encryption_secret=encryption_secret,
        encryption_previous_version=previous_version,
        encryption_previous_secret=previous_secret,
        internal_token=internal_token,
        envelope_secret=envelope_secret,
        permit_secret=permit_secret,
        response_key_version=response_version,
        response_secret_current=response_secret,
        network_policy_caps=caps,
        network_acl=acl,
        dns_timeout_seconds=dns_timeout,
        tls_timeout_seconds=tls_timeout,
        headers_timeout_seconds=headers_timeout,
        body_idle_timeout_seconds=body_idle_timeout,
        max_dns_answers=max_dns_answers,
        max_active_sessions=_integer(
            values, "METAHUB_MCP_SANDBOX_MAX_ACTIVE_SESSIONS", minimum=1, maximum=64
        ),
        cleanup_timeout_seconds=_integer(
            values, "METAHUB_MCP_SANDBOX_CLEANUP_TIMEOUT_SECONDS", minimum=1, maximum=5
        ),
    )


def _validate_independent_secrets(values: tuple[str | None, ...]) -> None:
    encoded_values: list[bytes] = []
    for value in values:
        if value is None:
            continue
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise SandboxConfigError(
                code="infra.config_invalid",
                message="MCP Sandbox secret encoding is invalid.",
                suggestion="Use independent 32-512 byte UTF-8 secrets.",
            ) from None
        if (
            not 32 <= len(encoded) <= 512
            or value != value.strip()
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise SandboxConfigError(
                code="infra.config_invalid",
                message="MCP Sandbox secret does not meet security requirements.",
                suggestion="Use independent 32-512 byte secrets without whitespace.",
            )
        if any(hmac.compare_digest(encoded, prior) for prior in encoded_values):
            raise SandboxConfigError(
                code="infra.config_invalid",
                message="MCP Sandbox secrets must use independent key domains.",
                suggestion="Replace every reused Worker secret.",
            )
        encoded_values.append(encoded)


def _required(values: Mapping[str, str], key: str) -> str:
    value = _optional(values, key)
    if value is None:
        raise SandboxConfigError(
            code="infra.config_missing",
            message="Required MCP Sandbox configuration is missing.",
            suggestion="Configure the complete isolated Worker profile.",
        )
    return value


def _optional(values: Mapping[str, str], key: str) -> str | None:
    value = values[key]
    if type(value) is not str or value != value.strip():
        raise SandboxConfigError(
            code="infra.config_invalid",
            message="MCP Sandbox configuration contains a non-canonical value.",
            suggestion="Remove surrounding whitespace and use exact values.",
        )
    return value or None


def _boolean(values: Mapping[str, str], key: str) -> bool:
    value = values[key]
    if value not in {"true", "false"}:
        raise SandboxConfigError(
            code="infra.config_invalid",
            message="MCP Sandbox boolean configuration is invalid.",
            suggestion="Use the exact lower-case true or false value.",
        )
    return value == "true"


def _integer(values: Mapping[str, str], key: str, *, minimum: int, maximum: int) -> int:
    value = values[key]
    if not value.isascii() or not value.isdecimal() or (len(value) > 1 and value[0] == "0"):
        raise SandboxConfigError(
            code="infra.config_invalid",
            message="MCP Sandbox integer configuration is invalid.",
            suggestion="Use a canonical base-10 integer within the documented bounds.",
        )
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise SandboxConfigError(
            code="infra.config_invalid",
            message="MCP Sandbox integer configuration is out of range.",
            suggestion="Use the documented bounded value.",
        )
    return parsed


def _optional_integer(values: Mapping[str, str], key: str) -> int | None:
    if values[key] == "":
        return None
    return _integer(values, key, minimum=1, maximum=2_147_483_647)


def _csv(value: str) -> tuple[str, ...]:
    if value == "":
        return ()
    parts = tuple(value.split(","))
    if any(not part or part != part.strip() for part in parts):
        raise McpContractError("mcp.feature_disabled")
    return parts


def _ports(value: str, *, allow_empty: bool = False) -> tuple[int, ...]:
    parts = _csv(value)
    if not parts and allow_empty:
        return ()
    if not parts or any(
        not part.isascii() or not part.isdecimal() or part != str(int(part)) for part in parts
    ):
        raise McpContractError("mcp.feature_disabled")
    return tuple(int(part) for part in parts)
