"""Shared PRD-083 MCP network policy, ACL, and sandbox protocol contracts.

The module is intentionally free of sockets and service state.  Control, the MCP
Scheduler, and the isolated Sandbox Worker use the same immutable policy caps and
canonical cryptographic formats without sharing network authority.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from secrets import token_bytes
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .contracts import McpContractError, McpVersionedSecret, canonical_json_bytes

McpNetworkPolicyStatus = Literal["disabled", "active"]
McpNetworkBlockReason = Literal[
    "policy_disabled",
    "address_denied",
    "mixed_dns_answers",
    "port_denied",
    "redirect_denied",
]
McpNetworkPhase = Literal["sync", "probe", "call_preflight"]

MCP_NETWORK_BLOCKED_EVENT_TYPE: Final[str] = "mcp.network_blocked"
MCP_SANDBOX_ENVELOPE_FORMAT: Final[str] = "metahub-mcp-sandbox-envelope-v1"
MCP_SANDBOX_PERMIT_FORMAT: Final[str] = "metahub-mcp-sandbox-permit-v1"
MCP_SANDBOX_RESPONSE_FORMAT: Final[str] = "metahub-mcp-sandbox-response-v1"
MCP_SANDBOX_ENVELOPE_MAX_TTL_SECONDS: Final[int] = 10
# Five seconds is the deployment cleanup maximum; one more second covers encryption and HTTP.
MCP_SANDBOX_RESPONSE_RESERVE_SECONDS: Final[int] = 6
MCP_NETWORK_BLOCK_REASONS: Final[frozenset[str]] = frozenset(
    {
        "policy_disabled",
        "address_denied",
        "mixed_dns_answers",
        "port_denied",
        "redirect_denied",
    }
)
DEFAULT_MCP_NETWORK_DENY_PORTS: Final[frozenset[int]] = frozenset(
    {
        1433,
        1521,
        2049,
        2375,
        2376,
        3306,
        5432,
        5984,
        6379,
        6443,
        8020,
        8500,
        9000,
        9042,
        9200,
        11211,
        27017,
    }
)

_METADATA_ADDRESSES: Final[frozenset[ipaddress.IPv4Address]] = frozenset(
    {ipaddress.IPv4Address("169.254.169.254"), ipaddress.IPv4Address("100.100.100.200")}
)
_POLICY_FIELDS: Final[tuple[str, ...]] = (
    "expected_revision",
    "status",
    "egress_mode",
    "connect_timeout_seconds",
    "probe_timeout_seconds",
    "call_timeout_seconds",
    "failure_threshold",
    "open_seconds",
)
_POLICY_SETTING_FIELDS: Final[tuple[str, ...]] = _POLICY_FIELDS[1:]
_CAP_FIELDS: Final[tuple[str, ...]] = (
    "connect_timeout_max_seconds",
    "probe_timeout_max_seconds",
    "call_timeout_max_seconds",
    "failure_threshold_max",
    "open_seconds_max",
)
_RESPONSE_CRYPTO_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "response_format",
        "response_key_version",
        "payload_nonce",
        "payload_ciphertext",
        "payload_hmac",
        "response_hmac",
    }
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$", re.ASCII)
_DISPATCH_ID = re.compile(r"^mcpd_[0-9a-f]{32}$")
_SERVER_ID = re.compile(r"^mcps_[0-9a-f]{32}$")
_OPERATION_ID = re.compile(r"^mcpo_[0-9a-f]{32}$")
_INVOCATION_ID = re.compile(r"^mcpi_[0-9a-f]{32}$")
_TOOL_ID = re.compile(r"^mcpt_[0-9a-f]{32}$")
_NETWORK_DECISION_ID = re.compile(r"^mcpnd_[0-9a-f]{32}$")
_NETWORK_CALL_INTENT_ID = re.compile(r"^mcpni_[0-9a-f]{32}$")
_NETWORK_EVENT_ID = re.compile(r"^mcpne_[0-9a-f]{32}$")
_UTC_SECOND = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PERMIT_CLAIM_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "action",
        "dispatch_id",
        "tenant_id",
        "mcp_server_id",
        "mcp_tool_id",
        "mcp_invocation_id",
        "mcp_network_decision_id",
        "mcp_network_call_intent_id",
        "lease_generation",
        "lease_token_hmac",
        "network_policy_revision",
        "network_policy_fingerprint",
        "endpoint_hmac",
        "endpoint_hmac_key_version",
        "credential_revision",
        "credential_id",
        "credential_ciphertext_sha256",
        "auth_type",
        "schema_revision",
        "tool_policy_revision",
        "tool_policy_fingerprint",
        "tool_name_sha256",
        "arguments_hmac",
        "arguments_hmac_key_version",
        "expires_at",
        "scheme",
        "hostname",
        "port",
        "pinned_ip",
        "address_count",
        "boot_id",
    }
)
_PERMIT_AEAD_INFO: Final[bytes] = b"metahub/mcp/sandbox/permit/aead/v1"
_PERMIT_HMAC_INFO: Final[bytes] = b"metahub/mcp/sandbox/permit/hmac/v1"
_RESPONSE_AEAD_INFO: Final[bytes] = b"metahub/mcp/response/aead"
_RESPONSE_HMAC_INFO: Final[bytes] = b"metahub/mcp/response/hmac"


@dataclass(frozen=True, slots=True)
class McpNetworkPolicyCaps:
    """Deployment-owned maxima shared byte-for-byte by all three MCP roles."""

    connect_timeout_max_seconds: int = 5
    probe_timeout_max_seconds: int = 10
    call_timeout_max_seconds: int = 30
    failure_threshold_max: int = 10
    open_seconds_max: int = 3600

    def __post_init__(self) -> None:
        bounds = ((1, 10), (1, 30), (1, 120), (1, 10), (10, 3600))
        if any(
            type(getattr(self, name)) is not int or not minimum <= getattr(self, name) <= maximum
            for name, (minimum, maximum) in zip(_CAP_FIELDS, bounds, strict=True)
        ):
            raise McpContractError("mcp.network_policy_invalid")

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in _CAP_FIELDS}

    @property
    def canonical_json(self) -> str:
        return _fixed_json_bytes(self.to_dict(), _CAP_FIELDS).decode("ascii")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class McpNetworkPolicy:
    """Exact public network-policy mutation after strict type and cap validation."""

    expected_revision: int
    status: McpNetworkPolicyStatus
    egress_mode: Literal["registered_endpoint_only"]
    connect_timeout_seconds: int
    probe_timeout_seconds: int
    call_timeout_seconds: int
    failure_threshold: int
    open_seconds: int

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in _POLICY_FIELDS}

    def settings_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in _POLICY_SETTING_FIELDS}

    @property
    def canonical_json(self) -> str:
        return _fixed_json_bytes(self.settings_dict(), _POLICY_SETTING_FIELDS).decode("ascii")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("ascii")).hexdigest()


def validate_mcp_network_policy(
    value: object,
    *,
    caps: McpNetworkPolicyCaps | None = None,
) -> McpNetworkPolicy:
    """Validate the exact PATCH body and apply the deployment maxima."""

    if type(value) is not dict or tuple(value) != _POLICY_FIELDS:
        # JSON member order is not semantically significant; normalize exact keys below.
        if type(value) is not dict or set(value) != set(_POLICY_FIELDS):
            raise McpContractError("mcp.network_policy_invalid")
    assert isinstance(value, dict)
    expected_revision = value["expected_revision"]
    status = value["status"]
    egress_mode = value["egress_mode"]
    integers = tuple(value[name] for name in _POLICY_FIELDS[3:])
    if (
        type(expected_revision) is not int
        or expected_revision < 1
        or status not in {"disabled", "active"}
        or type(status) is not str
        or egress_mode != "registered_endpoint_only"
        or type(egress_mode) is not str
        or any(type(item) is not int for item in integers)
    ):
        raise McpContractError("mcp.network_policy_invalid")
    active_caps = caps or McpNetworkPolicyCaps()
    ranges = (
        (1, active_caps.connect_timeout_max_seconds),
        (1, active_caps.probe_timeout_max_seconds),
        (1, active_caps.call_timeout_max_seconds),
        (1, active_caps.failure_threshold_max),
        (10, active_caps.open_seconds_max),
    )
    if (
        any(
            not minimum <= item <= maximum
            for item, (minimum, maximum) in zip(integers, ranges, strict=True)
        )
        or integers[0] > integers[2]
    ):
        raise McpContractError("mcp.network_policy_invalid")
    return McpNetworkPolicy(
        expected_revision=expected_revision,
        status=status,  # type: ignore[arg-type]
        egress_mode=egress_mode,
        connect_timeout_seconds=integers[0],
        probe_timeout_seconds=integers[1],
        call_timeout_seconds=integers[2],
        failure_threshold=integers[3],
        open_seconds=integers[4],
    )


@dataclass(frozen=True, slots=True)
class McpNetworkAcl:
    """Compiled deployment ACL; resolved addresses are deliberately absent from repr."""

    private_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(
        default=(), repr=False
    )
    deny_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(
        default=(), repr=False
    )
    allowed_ports: frozenset[int] = frozenset({443})
    deny_ports: frozenset[int] = DEFAULT_MCP_NETWORK_DENY_PORTS
    max_dns_answers: int = 8

    def __post_init__(self) -> None:
        networks = self.private_cidrs + self.deny_cidrs
        if (
            type(self.private_cidrs) is not tuple
            or type(self.deny_cidrs) is not tuple
            or any(
                not isinstance(item, (ipaddress.IPv4Network, ipaddress.IPv6Network))
                for item in networks
            )
            or type(self.allowed_ports) is not frozenset
            or type(self.deny_ports) is not frozenset
            or any(
                type(port) is not int or not 1 <= port <= 65535
                for port in self.allowed_ports | self.deny_ports
            )
            or not self.allowed_ports
            or self.allowed_ports & self.deny_ports
            or not DEFAULT_MCP_NETWORK_DENY_PORTS.issubset(self.deny_ports)
            or type(self.max_dns_answers) is not int
            or not 1 <= self.max_dns_answers <= 16
            or any(
                network.prefixlen != network.max_prefixlen
                or not network.network_address.is_private
                or _permanently_denied(network.network_address)
                for network in self.private_cidrs
            )
            or any(
                private.overlaps(denied)
                for private in self.private_cidrs
                for denied in self.deny_cidrs
            )
        ):
            raise McpContractError("mcp.feature_disabled")


@dataclass(frozen=True, slots=True)
class McpNetworkAclDecision:
    """Safe decision summary; address material remains request-local and hidden."""

    allowed: bool
    reason_code: McpNetworkBlockReason | None
    address_count: int
    normalized_addresses: tuple[str, ...] = field(default=(), repr=False)


def compile_mcp_network_acl(
    *,
    private_cidrs: Sequence[str] = (),
    deny_cidrs: Sequence[str] = (),
    allowed_ports: Sequence[int] = (443,),
    deny_ports: Sequence[int] = (),
    max_dns_answers: int = 8,
) -> McpNetworkAcl:
    """Compile canonical deployment rules and reject overlap instead of applying precedence."""

    private = _canonical_networks(private_cidrs, private_only=True)
    denied = _canonical_networks(deny_cidrs, private_only=False)
    allowed = _canonical_ports(allowed_ports, allow_empty=False)
    denied_ports = DEFAULT_MCP_NETWORK_DENY_PORTS | _canonical_ports(deny_ports, allow_empty=True)
    if (
        type(max_dns_answers) is not int
        or not 1 <= max_dns_answers <= 16
        or allowed & denied_ports
        or any(first.overlaps(second) for first in private for second in denied)
    ):
        raise McpContractError("mcp.feature_disabled")
    return McpNetworkAcl(
        private_cidrs=private,
        deny_cidrs=denied,
        allowed_ports=allowed,
        deny_ports=denied_ports,
        max_dns_answers=max_dns_answers,
    )


def evaluate_mcp_network_destination(
    endpoint_url: object,
    addresses: object,
    *,
    acl: McpNetworkAcl,
    redirected: bool = False,
) -> McpNetworkAclDecision:
    """Evaluate one registered endpoint and a complete single-resolution answer set."""

    if redirected:
        return McpNetworkAclDecision(False, "redirect_denied", 0)
    if type(endpoint_url) is not str or not isinstance(acl, McpNetworkAcl):
        raise McpContractError("mcp.network_blocked")
    try:
        parsed = urlsplit(endpoint_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise McpContractError("mcp.network_blocked") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.hostname != parsed.hostname.lower()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise McpContractError("mcp.network_blocked")
    if port in acl.deny_ports or port not in acl.allowed_ports:
        return McpNetworkAclDecision(False, "port_denied", 0)
    normalized = _normalized_addresses(addresses, maximum=acl.max_dns_answers)
    outcomes = tuple(_address_allowed(address, acl) for address in normalized)
    if all(outcomes):
        return McpNetworkAclDecision(
            True, None, len(normalized), tuple(str(item) for item in normalized)
        )
    reason: McpNetworkBlockReason = "mixed_dns_answers" if any(outcomes) else "address_denied"
    return McpNetworkAclDecision(
        False, reason, len(normalized), tuple(str(item) for item in normalized)
    )


_NETWORK_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "mcp_server_id",
        "operation_id",
        "mcp_invocation_id",
        "phase",
        "reason_code",
        "policy_revision",
        "address_count",
        "attempt_number",
        "tool_call_count",
        "cost_status",
        "occurred_at",
    }
)


def validate_mcp_network_blocked_payload(payload: object) -> dict[str, Any]:
    """Validate the exact non-sensitive EVT-095 payload."""

    if type(payload) is not dict or set(payload) != _NETWORK_EVENT_FIELDS:
        raise McpContractError("mcp.unavailable")
    operation_id = payload["operation_id"]
    invocation_id = payload["mcp_invocation_id"]
    phase = payload["phase"]
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or type(payload["mcp_server_id"]) is not str
        or _SERVER_ID.fullmatch(payload["mcp_server_id"]) is None
        or not _optional_id(operation_id, _OPERATION_ID)
        or not _optional_id(invocation_id, _INVOCATION_ID)
        or (operation_id is None) == (invocation_id is None)
        or phase not in {"sync", "probe", "call_preflight"}
        or type(phase) is not str
        or payload["reason_code"] not in MCP_NETWORK_BLOCK_REASONS
        or type(payload["reason_code"]) is not str
        or type(payload["policy_revision"]) is not int
        or payload["policy_revision"] < 1
        or type(payload["address_count"]) is not int
        or not 0 <= payload["address_count"] <= 16
        or type(payload["attempt_number"]) is not int
        or not 1 <= payload["attempt_number"] <= 10
        or type(payload["tool_call_count"]) is not int
        or payload["tool_call_count"] != 0
        or payload["cost_status"] != "not_applicable"
        or type(payload["cost_status"]) is not str
        or type(payload["occurred_at"]) is not str
        or _UTC_SECOND.fullmatch(payload["occurred_at"]) is None
        or (phase == "sync" and operation_id is None)
        or (phase == "call_preflight" and invocation_id is None)
    ):
        raise McpContractError("mcp.unavailable")
    if (payload["reason_code"] == "policy_disabled" and payload["address_count"] != 0) or (
        payload["reason_code"] in {"address_denied", "mixed_dns_answers"}
        and payload["address_count"] < 1
    ):
        raise McpContractError("mcp.unavailable")
    try:
        _parse_utc_second(payload["occurred_at"])
    except McpContractError:
        raise McpContractError("mcp.unavailable") from None
    return dict(payload)


def validate_mcp_network_blocked_event(
    payload: object,
    *,
    event_id: object,
    tenant_id: object,
    actor_id: object,
    object_id: object,
    trace_id: object,
    source_request_id: object,
    occurred_at: object,
) -> dict[str, Any]:
    """Bind EVT-095 to its stable source owner and occurrence second."""

    safe = validate_mcp_network_blocked_payload(payload)
    expected_object = safe["operation_id"] or safe["mcp_invocation_id"]
    if (
        type(event_id) is not str
        or _NETWORK_EVENT_ID.fullmatch(event_id) is None
        or any(
            type(item) is not str or _SAFE_ID.fullmatch(item) is None
            for item in (tenant_id, trace_id, source_request_id)
        )
        or actor_id is not None
        or object_id != expected_object
        or type(occurred_at) is not datetime
        or occurred_at.utcoffset() is None
        or _to_utc_second(occurred_at) != safe["occurred_at"]
    ):
        raise McpContractError("mcp.unavailable")
    return safe


@dataclass(frozen=True, slots=True)
class McpSignedSandboxEnvelope:
    """Canonical request body and detached signature, both excluded from repr."""

    payload: Mapping[str, Any] = field(repr=False)
    signature: str = field(repr=False)


class McpSandboxReplayCache:
    """Bounded, process-local dispatch replay cache with strict expiry."""

    def __init__(self, *, maximum_entries: int = 4096) -> None:
        if type(maximum_entries) is not int or not 1 <= maximum_entries <= 1_000_000:
            raise McpContractError("mcp.sandbox_unavailable")
        self._maximum_entries = maximum_entries
        self._entries: OrderedDict[str, int] = OrderedDict()

    @property
    def size(self) -> int:
        return len(self._entries)

    def consume(self, dispatch_id: object, *, expires_at: int, now: int) -> None:
        if (
            type(dispatch_id) is not str
            or _DISPATCH_ID.fullmatch(dispatch_id) is None
            or type(expires_at) is not int
            or type(now) is not int
            or expires_at <= now
        ):
            raise McpContractError("mcp.sandbox_unavailable")
        self._consume(dispatch_id, expires_at=expires_at, now=now)

    def consume_permit(self, permit: object, *, expires_at: int, now: int) -> None:
        """Consume an opaque permit without retaining the token or plaintext claims."""

        if type(permit) is not str or not permit:
            raise McpContractError("mcp.sandbox_unavailable")
        digest = hashlib.sha256(permit.encode("ascii", errors="strict")).hexdigest()
        self._consume("permit:" + digest, expires_at=expires_at, now=now)

    def _consume(self, key: str, *, expires_at: int, now: int) -> None:
        self._purge(now)
        if key in self._entries:
            raise McpContractError("mcp.sandbox_unavailable")
        if len(self._entries) >= self._maximum_entries:
            raise McpContractError("mcp.sandbox_unavailable")
        self._entries[key] = expires_at

    def _purge(self, now: int) -> None:
        expired = [key for key, expiry in self._entries.items() if expiry <= now]
        for key in expired:
            del self._entries[key]


def sign_mcp_sandbox_envelope(
    payload: object,
    *,
    secret: object,
    now: datetime | None = None,
) -> McpSignedSandboxEnvelope:
    """Validate and sign one short-lived Scheduler-to-Sandbox request."""

    safe, encoded, _, _ = _validate_sandbox_envelope(payload, now=now)
    signature = hmac.new(
        _secret_bytes(secret),
        encoded,
        hashlib.sha256,
    ).hexdigest()
    return McpSignedSandboxEnvelope(payload=safe, signature=signature)


def verify_mcp_sandbox_envelope(
    payload: object,
    *,
    signature: object,
    secret: object,
    replay_cache: McpSandboxReplayCache,
    expected_action: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authenticate, expiry-check, action-bind, and consume one dispatch ID."""

    safe, encoded, current, expires = _validate_sandbox_envelope(payload, now=now)
    if type(signature) is not str or len(signature) != 64:
        raise McpContractError("mcp.sandbox_unavailable")
    expected = hmac.new(
        _secret_bytes(secret),
        encoded,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise McpContractError("mcp.sandbox_unavailable")
    if expected_action is not None and safe["action"] != expected_action:
        raise McpContractError("mcp.sandbox_unavailable")
    replay_cache.consume(safe["dispatch_id"], expires_at=expires, now=current)
    return safe


@dataclass(frozen=True, slots=True)
class McpSandboxPermit:
    """Opaque worker-issued permit and its persistence-safe SHA-256 digest."""

    value: str = field(repr=False)
    digest: str
    expires_at: int


def issue_mcp_sandbox_permit(
    claims: object,
    *,
    secret: object,
    boot_id: object,
    now: datetime | None = None,
) -> McpSandboxPermit:
    """Encrypt and authenticate claims that only the current Worker boot can consume."""

    safe = _permit_claims(claims, boot_id=boot_id)
    expires = safe["expires_at"]
    if expires <= _epoch_second(now):
        raise McpContractError("mcp.sandbox_unavailable")
    encoded = canonical_json_bytes(safe, maximum=65_536, code="mcp.sandbox_unavailable")
    salt = hashlib.sha256((MCP_SANDBOX_PERMIT_FORMAT + ":" + str(boot_id)).encode()).digest()
    nonce = token_bytes(12)
    ciphertext = AESGCM(_hkdf_key(secret, salt=salt, info=_PERMIT_AEAD_INFO)).encrypt(
        nonce, encoded, MCP_SANDBOX_PERMIT_FORMAT.encode("ascii")
    )
    authenticated = b"\x00".join((MCP_SANDBOX_PERMIT_FORMAT.encode("ascii"), nonce, ciphertext))
    signature = hmac.new(
        _hkdf_key(secret, salt=salt, info=_PERMIT_HMAC_INFO),
        authenticated,
        hashlib.sha256,
    ).digest()
    value = ":".join((MCP_SANDBOX_PERMIT_FORMAT, _b64(nonce), _b64(ciphertext), _b64(signature)))
    return McpSandboxPermit(
        value=value,
        digest=hashlib.sha256(value.encode("ascii")).hexdigest(),
        expires_at=expires,
    )


def verify_mcp_sandbox_permit(
    permit: object,
    *,
    secret: object,
    boot_id: object,
    replay_cache: McpSandboxReplayCache,
    expected_claims: Mapping[str, object] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify and decrypt a permit, then bind all caller-supplied expected claims."""

    if type(permit) is not str:
        raise McpContractError("mcp.sandbox_unavailable")
    parts = permit.split(":")
    if len(parts) != 4 or parts[0] != MCP_SANDBOX_PERMIT_FORMAT:
        raise McpContractError("mcp.sandbox_unavailable")
    try:
        nonce, ciphertext, signature = (_unb64(item) for item in parts[1:])
        if len(nonce) != 12 or len(ciphertext) < 16 or len(signature) != 32:
            raise ValueError
        checked_boot = _safe_id(boot_id)
        salt = hashlib.sha256((MCP_SANDBOX_PERMIT_FORMAT + ":" + checked_boot).encode()).digest()
        authenticated = b"\x00".join((MCP_SANDBOX_PERMIT_FORMAT.encode("ascii"), nonce, ciphertext))
        expected_signature = hmac.new(
            _hkdf_key(secret, salt=salt, info=_PERMIT_HMAC_INFO),
            authenticated,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError
        plaintext = AESGCM(_hkdf_key(secret, salt=salt, info=_PERMIT_AEAD_INFO)).decrypt(
            nonce, ciphertext, MCP_SANDBOX_PERMIT_FORMAT.encode("ascii")
        )
        claims = json.loads(plaintext)
    except (InvalidTag, ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError):
        raise McpContractError("mcp.sandbox_unavailable") from None
    safe = _permit_claims(claims, boot_id=checked_boot)
    current = _epoch_second(now)
    if safe["expires_at"] <= current:
        raise McpContractError("mcp.sandbox_unavailable")
    if expected_claims is not None and any(
        key not in safe or safe[key] != value for key, value in expected_claims.items()
    ):
        raise McpContractError("mcp.sandbox_unavailable")
    replay_cache.consume_permit(permit, expires_at=safe["expires_at"], now=current)
    return safe


@dataclass(frozen=True, slots=True)
class McpSandboxResponseEnvelope:
    """Encrypted response wire; ciphertext and authenticators stay out of repr."""

    metadata: Mapping[str, Any] = field(repr=False)
    response_key_version: int
    payload_nonce: str = field(repr=False)
    payload_ciphertext: str = field(repr=False)
    payload_hmac: str = field(repr=False)
    response_hmac: str = field(repr=False)

    def to_wire(self) -> dict[str, Any]:
        return {
            **self.metadata,
            "response_format": MCP_SANDBOX_RESPONSE_FORMAT,
            "response_key_version": self.response_key_version,
            "payload_nonce": self.payload_nonce,
            "payload_ciphertext": self.payload_ciphertext,
            "payload_hmac": self.payload_hmac,
            "response_hmac": self.response_hmac,
        }


def encrypt_mcp_sandbox_response(
    payload: object,
    *,
    metadata: object,
    secret: object,
    key_version: int,
) -> McpSandboxResponseEnvelope:
    """AEAD-encrypt a response and add payload plus whole-envelope HMACs."""

    safe_metadata = _response_metadata(metadata)
    if type(key_version) is not int or not 1 <= key_version <= 2_147_483_647:
        raise McpContractError("mcp.sandbox_unavailable")
    plaintext = _response_payload(payload)
    aad = _response_aad(safe_metadata)
    aead_key, hmac_key = _response_keys(secret, key_version)
    nonce = token_bytes(12)
    ciphertext = AESGCM(aead_key).encrypt(nonce, plaintext, aad)
    payload_hmac = hmac.new(
        hmac_key, b"\x00".join((aad, nonce, ciphertext)), hashlib.sha256
    ).hexdigest()
    unsigned = {
        **safe_metadata,
        "response_format": MCP_SANDBOX_RESPONSE_FORMAT,
        "response_key_version": key_version,
        "payload_nonce": _b64(nonce),
        "payload_ciphertext": _b64(ciphertext),
        "payload_hmac": payload_hmac,
    }
    response_hmac = hmac.new(
        hmac_key,
        canonical_json_bytes(unsigned, maximum=2_097_152, code="mcp.sandbox_unavailable"),
        hashlib.sha256,
    ).hexdigest()
    return McpSandboxResponseEnvelope(
        metadata=safe_metadata,
        response_key_version=key_version,
        payload_nonce=unsigned["payload_nonce"],
        payload_ciphertext=unsigned["payload_ciphertext"],
        payload_hmac=payload_hmac,
        response_hmac=response_hmac,
    )


def decrypt_mcp_sandbox_response(
    envelope: object,
    *,
    keyring: tuple[McpVersionedSecret, ...],
    current_version: int,
    expected_metadata: Mapping[str, object],
    now: datetime | None = None,
) -> bytes:
    """Authenticate metadata and both HMAC layers before exposing plaintext."""

    if type(envelope) is not dict or not _RESPONSE_CRYPTO_FIELDS.issubset(envelope):
        raise McpContractError("mcp.sandbox_unavailable")
    if envelope["response_format"] != MCP_SANDBOX_RESPONSE_FORMAT:
        raise McpContractError("mcp.sandbox_unavailable")
    version = envelope["response_key_version"]
    if type(current_version) is not int or type(version) is not int:
        raise McpContractError("mcp.sandbox_unavailable")
    allowed_versions = {current_version, current_version - 1}
    if version not in allowed_versions or version < 1:
        raise McpContractError("mcp.sandbox_unavailable")
    keys = {item.version: item.secret for item in _response_keyring(keyring, current_version)}
    secret = keys.get(version)
    if secret is None:
        raise McpContractError("mcp.sandbox_unavailable")
    metadata = {key: value for key, value in envelope.items() if key not in _RESPONSE_CRYPTO_FIELDS}
    safe_metadata = _response_metadata(metadata)
    binding_fields = {
        "request_id",
        "dispatch_id",
        "action",
        "tenant_id",
        "operation_id",
        "mcp_invocation_id",
        "lease_generation",
        "lease_owner_id",
        "network_policy_revision",
        "network_policy_fingerprint",
        "deadline_at",
    }
    if type(expected_metadata) is not dict or not binding_fields.issubset(expected_metadata):
        raise McpContractError("mcp.sandbox_unavailable")
    if any(
        key not in safe_metadata or safe_metadata[key] != value
        for key, value in expected_metadata.items()
    ):
        raise McpContractError("mcp.sandbox_unavailable")
    if _parse_utc_second(safe_metadata["deadline_at"]) <= _epoch_second(now):
        raise McpContractError("mcp.sandbox_unavailable")
    try:
        nonce = _unb64(envelope["payload_nonce"])
        ciphertext = _unb64(envelope["payload_ciphertext"])
        if len(nonce) != 12 or len(ciphertext) < 16:
            raise ValueError
        aead_key, hmac_key = _response_keys(secret, version)
        unsigned = {key: value for key, value in envelope.items() if key != "response_hmac"}
        response_hmac = hmac.new(
            hmac_key,
            canonical_json_bytes(unsigned, maximum=2_097_152, code="mcp.sandbox_unavailable"),
            hashlib.sha256,
        ).hexdigest()
        if type(envelope["response_hmac"]) is not str or not hmac.compare_digest(
            envelope["response_hmac"], response_hmac
        ):
            raise ValueError
        aad = _response_aad(safe_metadata)
        payload_hmac = hmac.new(
            hmac_key, b"\x00".join((aad, nonce, ciphertext)), hashlib.sha256
        ).hexdigest()
        if type(envelope["payload_hmac"]) is not str or not hmac.compare_digest(
            envelope["payload_hmac"], payload_hmac
        ):
            raise ValueError
        return AESGCM(aead_key).decrypt(nonce, ciphertext, aad)
    except (InvalidTag, ValueError, TypeError, UnicodeError, binascii.Error):
        raise McpContractError("mcp.sandbox_unavailable") from None


def _validate_sandbox_envelope(
    payload: object,
    *,
    now: datetime | None,
) -> tuple[dict[str, Any], bytes, int, int]:
    if type(payload) is not dict:
        raise McpContractError("mcp.sandbox_unavailable")
    required = {
        "schema_version",
        "action",
        "dispatch_id",
        "issued_at",
        "expires_at",
        "tenant_id",
        "mcp_server_id",
        "operation_id",
        "mcp_invocation_id",
        "lease_generation",
        "lease_owner_id",
        "lease_token_hmac",
        "network_policy_revision",
        "network_policy_fingerprint",
        "caps_fingerprint",
        "deadline_at",
    }
    if not required.issubset(payload):
        raise McpContractError("mcp.sandbox_unavailable")
    safe = _detached_json_object(payload, maximum=131_072)
    issued = _parse_utc_second(safe["issued_at"])
    expires = _parse_utc_second(safe["expires_at"])
    deadline = _parse_utc_second(safe["deadline_at"])
    current = _epoch_second(now)
    operation_id = safe["operation_id"]
    invocation_id = safe["mcp_invocation_id"]
    if (
        safe["schema_version"] != 1
        or type(safe["schema_version"]) is not int
        or type(safe["action"]) is not str
        or safe["action"] not in {"discover", "probe", "authorize-call", "call"}
        or type(safe["dispatch_id"]) is not str
        or _DISPATCH_ID.fullmatch(safe["dispatch_id"]) is None
        or not _safe_id_or_false(safe["tenant_id"])
        or type(safe["mcp_server_id"]) is not str
        or _SERVER_ID.fullmatch(safe["mcp_server_id"]) is None
        or not _optional_id(operation_id, _OPERATION_ID)
        or not _optional_id(invocation_id, _INVOCATION_ID)
        or (operation_id is None) == (invocation_id is None)
        or type(safe["lease_generation"]) is not int
        or safe["lease_generation"] < 1
        or not _safe_id_or_false(safe["lease_owner_id"])
        or not _lower_hex(safe["lease_token_hmac"], 64)
        or type(safe["network_policy_revision"]) is not int
        or safe["network_policy_revision"] < 1
        or not _lower_hex(safe["network_policy_fingerprint"], 64)
        or not _lower_hex(safe["caps_fingerprint"], 64)
        or not issued <= current < expires <= deadline
        or expires - issued > MCP_SANDBOX_ENVELOPE_MAX_TTL_SECONDS
    ):
        raise McpContractError("mcp.sandbox_unavailable")
    encoded = canonical_json_bytes(safe, maximum=131_072, code="mcp.sandbox_unavailable")
    return safe, encoded, current, expires


def _permit_claims(claims: object, *, boot_id: object) -> dict[str, Any]:
    safe = _detached_json_object(claims, maximum=65_536)
    checked_boot = _safe_id(boot_id)
    supplied_fields = set(safe)
    if supplied_fields != _PERMIT_CLAIM_FIELDS and supplied_fields != _PERMIT_CLAIM_FIELDS - {
        "boot_id"
    }:
        raise McpContractError("mcp.sandbox_unavailable")
    supplied_boot = safe.get("boot_id")
    if supplied_boot is None:
        safe["boot_id"] = checked_boot
    elif supplied_boot != checked_boot:
        raise McpContractError("mcp.sandbox_unavailable")
    try:
        pinned_address = ipaddress.ip_address(safe["pinned_ip"])
    except (TypeError, ValueError):
        raise McpContractError("mcp.sandbox_unavailable") from None
    hostname = safe["hostname"]
    if (
        safe["schema_version"] != 1
        or type(safe["schema_version"]) is not int
        or safe["action"] not in {"authorize-call", "call"}
        or type(safe["action"]) is not str
        or type(safe["dispatch_id"]) is not str
        or _DISPATCH_ID.fullmatch(safe["dispatch_id"]) is None
        or not _safe_id_or_false(safe["tenant_id"])
        or type(safe["mcp_server_id"]) is not str
        or _SERVER_ID.fullmatch(safe["mcp_server_id"]) is None
        or type(safe["mcp_tool_id"]) is not str
        or _TOOL_ID.fullmatch(safe["mcp_tool_id"]) is None
        or type(safe["mcp_invocation_id"]) is not str
        or _INVOCATION_ID.fullmatch(safe["mcp_invocation_id"]) is None
        or type(safe["mcp_network_decision_id"]) is not str
        or _NETWORK_DECISION_ID.fullmatch(safe["mcp_network_decision_id"]) is None
        or type(safe["mcp_network_call_intent_id"]) is not str
        or _NETWORK_CALL_INTENT_ID.fullmatch(safe["mcp_network_call_intent_id"]) is None
        or type(safe["lease_generation"]) is not int
        or safe["lease_generation"] < 1
        or any(
            not _lower_hex(safe[name], 64)
            for name in (
                "lease_token_hmac",
                "network_policy_fingerprint",
                "endpoint_hmac",
                "credential_ciphertext_sha256",
                "tool_policy_fingerprint",
                "tool_name_sha256",
                "arguments_hmac",
            )
        )
        or any(
            type(safe[name]) is not int or safe[name] < 1
            for name in (
                "endpoint_hmac_key_version",
                "credential_revision",
                "schema_revision",
                "tool_policy_revision",
                "arguments_hmac_key_version",
            )
        )
        or safe["auth_type"] not in {"none", "bearer"}
        or type(safe["auth_type"]) is not str
        or (safe["credential_id"] is not None and not _safe_id_or_false(safe["credential_id"]))
        or (safe["auth_type"] == "none" and safe["credential_id"] is not None)
        or (safe["auth_type"] == "bearer" and safe["credential_id"] is None)
        or type(safe["network_policy_revision"]) is not int
        or safe["network_policy_revision"] < 1
        or type(safe["expires_at"]) is not int
        or safe["expires_at"] < 1
        or safe["scheme"] != "https"
        or type(safe["scheme"]) is not str
        or type(hostname) is not str
        or not 1 <= len(hostname) <= 253
        or any(ord(character) <= 32 or ord(character) == 127 for character in hostname)
        or type(safe["port"]) is not int
        or not 1 <= safe["port"] <= 65535
        or type(safe["pinned_ip"]) is not str
        or str(pinned_address) != safe["pinned_ip"]
        or (isinstance(pinned_address, ipaddress.IPv6Address) and pinned_address.ipv4_mapped)
        or type(safe["address_count"]) is not int
        or not 1 <= safe["address_count"] <= 16
    ):
        raise McpContractError("mcp.sandbox_unavailable")
    return safe


def _response_metadata(value: object) -> dict[str, Any]:
    safe = _detached_json_object(value, maximum=65_536)
    if set(safe) & _RESPONSE_CRYPTO_FIELDS:
        raise McpContractError("mcp.sandbox_unavailable")
    required = {
        "schema_version",
        "request_id",
        "dispatch_id",
        "action",
        "tenant_id",
        "operation_id",
        "mcp_invocation_id",
        "lease_generation",
        "lease_owner_id",
        "network_policy_revision",
        "network_policy_fingerprint",
        "status",
        "failure_code",
        "deadline_at",
    }
    if not required.issubset(safe):
        raise McpContractError("mcp.sandbox_unavailable")
    operation_id = safe["operation_id"]
    invocation_id = safe["mcp_invocation_id"]
    _parse_utc_second(safe["deadline_at"])
    if (
        safe["schema_version"] != 1
        or type(safe["schema_version"]) is not int
        or not _safe_id_or_false(safe["request_id"])
        or type(safe["dispatch_id"]) is not str
        or _DISPATCH_ID.fullmatch(safe["dispatch_id"]) is None
        or safe["action"] not in {"discover", "probe", "authorize-call", "call"}
        or type(safe["action"]) is not str
        or not _safe_id_or_false(safe["tenant_id"])
        or not _optional_id(operation_id, _OPERATION_ID)
        or not _optional_id(invocation_id, _INVOCATION_ID)
        or (operation_id is None) == (invocation_id is None)
        or type(safe["lease_generation"]) is not int
        or safe["lease_generation"] < 1
        or not _safe_id_or_false(safe["lease_owner_id"])
        or type(safe["network_policy_revision"]) is not int
        or safe["network_policy_revision"] < 1
        or not _lower_hex(safe["network_policy_fingerprint"], 64)
        or safe["status"] not in {"succeeded", "failed", "blocked"}
        or type(safe["status"]) is not str
        or (safe["failure_code"] is not None and not _safe_id_or_false(safe["failure_code"]))
        or (safe["status"] == "succeeded") != (safe["failure_code"] is None)
    ):
        raise McpContractError("mcp.sandbox_unavailable")
    return safe


def _response_aad(metadata: Mapping[str, Any]) -> bytes:
    fields = (
        "schema_version",
        "request_id",
        "dispatch_id",
        "action",
        "tenant_id",
        "operation_id",
        "mcp_invocation_id",
        "lease_generation",
        "lease_owner_id",
        "network_policy_revision",
        "network_policy_fingerprint",
        "status",
        "failure_code",
        "deadline_at",
    )
    return canonical_json_bytes(
        {name: metadata[name] for name in fields},
        maximum=65_536,
        code="mcp.sandbox_unavailable",
    )


def _response_payload(value: object) -> bytes:
    if type(value) is bytes:
        encoded = value
    elif type(value) is str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise McpContractError("mcp.sandbox_unavailable") from None
    else:
        encoded = canonical_json_bytes(value, maximum=1_048_576, code="mcp.sandbox_unavailable")
    if len(encoded) > 1_048_576:
        raise McpContractError("mcp.sandbox_unavailable")
    return encoded


def _response_keyring(keyring: object, current_version: int) -> tuple[McpVersionedSecret, ...]:
    if type(keyring) is not tuple or not 1 <= len(keyring) <= 2:
        raise McpContractError("mcp.sandbox_unavailable")
    if any(not isinstance(item, McpVersionedSecret) for item in keyring):
        raise McpContractError("mcp.sandbox_unavailable")
    versions = tuple(item.version for item in keyring)
    if versions[0] != current_version or (
        len(versions) == 2 and versions[1] != current_version - 1
    ):
        raise McpContractError("mcp.sandbox_unavailable")
    secrets = tuple(_secret_bytes(item.secret) for item in keyring)
    if len(secrets) == 2 and hmac.compare_digest(secrets[0], secrets[1]):
        raise McpContractError("mcp.sandbox_unavailable")
    return keyring


def _response_keys(secret: object, version: int) -> tuple[bytes, bytes]:
    salt = hashlib.sha256(
        ("metahub-mcp-sandbox-response/v1:" + str(version)).encode("ascii")
    ).digest()
    return (
        _hkdf_key(secret, salt=salt, info=_RESPONSE_AEAD_INFO),
        _hkdf_key(secret, salt=salt, info=_RESPONSE_HMAC_INFO),
    )


def _canonical_networks(
    values: Sequence[str], *, private_only: bool
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if type(values) not in {list, tuple}:
        raise McpContractError("mcp.feature_disabled")
    parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        if type(value) is not str:
            raise McpContractError("mcp.feature_disabled")
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError:
            raise McpContractError("mcp.feature_disabled") from None
        if value != str(network) or (
            private_only
            and (
                network.prefixlen != network.max_prefixlen
                or not network.network_address.is_private
                or _permanently_denied(network.network_address)
            )
        ):
            raise McpContractError("mcp.feature_disabled")
        parsed.append(network)
    if len(set(parsed)) != len(parsed) or tuple(parsed) != tuple(
        sorted(parsed, key=lambda item: (item.version, int(item.network_address), item.prefixlen))
    ):
        raise McpContractError("mcp.feature_disabled")
    return tuple(parsed)


def _canonical_ports(values: Sequence[int], *, allow_empty: bool) -> frozenset[int]:
    if type(values) not in {list, tuple} or (not allow_empty and not values):
        raise McpContractError("mcp.feature_disabled")
    if (
        any(type(value) is not int or not 1 <= value <= 65535 for value in values)
        or len(set(values)) != len(values)
        or tuple(values) != tuple(sorted(values))
    ):
        raise McpContractError("mcp.feature_disabled")
    return frozenset(values)


def _normalized_addresses(
    values: object, *, maximum: int
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    if type(values) not in {list, tuple} or not values or len(values) > maximum * 8:
        raise McpContractError("mcp.server_unavailable")
    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for value in values:
        if type(value) is not str:
            raise McpContractError("mcp.server_unavailable")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise McpContractError("mcp.server_unavailable") from None
        # getaddrinfo can repeat one address for several socket/protocol combinations.  The ACL
        # applies to the normalized address set and keeps IPv4-mapped IPv6 for permanent denial.
        if address not in parsed:
            parsed.append(address)
    if len(parsed) > maximum:
        raise McpContractError("mcp.server_unavailable")
    return tuple(sorted(parsed, key=lambda item: (item.version, int(item))))


def _address_allowed(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, acl: McpNetworkAcl
) -> bool:
    if _permanently_denied(address):
        return False
    if any(address in network for network in acl.deny_cidrs):
        return False
    if address.is_global:
        return True
    return any(address in network for network in acl.private_cidrs)


def _permanently_denied(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return True
    return (
        address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
        or address in _METADATA_ADDRESSES
    )


def _fixed_json_bytes(value: Mapping[str, object], fields: tuple[str, ...]) -> bytes:
    if tuple(value) != fields:
        raise McpContractError("mcp.network_policy_invalid")
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode(
            "ascii"
        )
    except (TypeError, ValueError, UnicodeError):
        raise McpContractError("mcp.network_policy_invalid") from None


def _detached_json_object(value: object, *, maximum: int) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise McpContractError("mcp.sandbox_unavailable")
    encoded = canonical_json_bytes(value, maximum=maximum, code="mcp.sandbox_unavailable")
    return json.loads(encoded)


def _hkdf_key(secret: object, *, salt: bytes, info: bytes) -> bytes:
    try:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(
            _secret_bytes(secret)
        )
    except McpContractError:
        raise
    except Exception:
        raise McpContractError("mcp.sandbox_unavailable") from None


def _secret_bytes(secret: object) -> bytes:
    if type(secret) is not str or secret != secret.strip():
        raise McpContractError("mcp.sandbox_unavailable")
    try:
        encoded = secret.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise McpContractError("mcp.sandbox_unavailable") from None
    if not 32 <= len(encoded) <= 4096:
        raise McpContractError("mcp.sandbox_unavailable")
    return encoded


def _safe_id(value: object) -> str:
    if not _safe_id_or_false(value):
        raise McpContractError("mcp.sandbox_unavailable")
    return value  # type: ignore[return-value]


def _safe_id_or_false(value: object) -> bool:
    return type(value) is str and _SAFE_ID.fullmatch(value) is not None


def _optional_id(value: object, pattern: re.Pattern[str]) -> bool:
    return value is None or (type(value) is str and pattern.fullmatch(value) is not None)


def _lower_hex(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _parse_utc_second(value: object) -> int:
    if type(value) is not str or _UTC_SECOND.fullmatch(value) is None:
        raise McpContractError("mcp.sandbox_unavailable")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise McpContractError("mcp.sandbox_unavailable") from None
    return int(parsed.timestamp())


def _epoch_second(value: datetime | None) -> int:
    current = value or datetime.now(UTC)
    if type(current) is not datetime or current.utcoffset() is None:
        raise McpContractError("mcp.sandbox_unavailable")
    return int(current.astimezone(UTC).timestamp())


def _to_utc_second(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: object) -> bytes:
    if type(value) is not str or not value:
        raise binascii.Error
    decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    if _b64(decoded) != value:
        raise binascii.Error
    return decoded
