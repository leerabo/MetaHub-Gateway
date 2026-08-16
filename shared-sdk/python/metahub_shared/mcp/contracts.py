"""PRD-082 MCP shared value contracts.

The structures in this module never include credentials in ``repr``.  Tenant and
owner values must come from trusted server-side state; public request bodies must
not be converted directly into crypto owners or authorization snapshots.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from metahub_shared.rbac import PERMISSION_CODES, canonical_permission_codes

McpCryptoKind = Literal["endpoint", "bearer", "arguments", "result"]
McpHmacKind = Literal[
    "endpoint",
    "credential",
    "idempotency",
    "intent",
    "arguments",
    "result",
    "input_ruleset",
    "output_ruleset",
]

MCP_CRYPTO_KINDS: Final[frozenset[str]] = frozenset(
    {"endpoint", "bearer", "arguments", "result"}
)
MCP_HMAC_KINDS: Final[frozenset[str]] = frozenset(
    {
        "endpoint",
        "credential",
        "idempotency",
        "intent",
        "arguments",
        "result",
        "input_ruleset",
        "output_ruleset",
    }
)
MCP_FORBIDDEN_TOOL_PERMISSION_CODES: Final[frozenset[str]] = frozenset(
    {"approval.decide", "mcp.approve"}
)
MCP_SENSITIVE_RESULT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "cookie",
        "set_cookie",
        "access_token",
        "refresh_token",
        "id_token",
        "bearer_token",
        "api_key",
        "provider_key",
        "client_secret",
        "password",
    }
)

_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", re.ASCII)
_VISIBLE_ASCII_IDEMPOTENCY = re.compile(r"^[\x21-\x7e]{8,128}$", re.ASCII)


class McpContractError(ValueError):
    """A fixed MCP contract failure with no submitted value in its message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class McpSchemaLimits:
    """Deployment-owned Schema, JSON tree, and validation budgets."""

    max_schema_bytes: int = 65_536
    max_instance_bytes: int = 65_536
    max_result_bytes: int = 1_048_576
    max_depth: int = 12
    max_nodes: int = 1_024
    max_properties: int = 256
    max_string_length: int = 65_536
    max_array_items: int = 1_024
    max_keywords: int = 2_048
    max_combinator_branches: int = 64
    max_enum_items: int = 256
    max_enum_item_bytes: int = 4_096
    max_pattern_length: int = 512
    max_evaluation_steps: int = 100_000

    def __post_init__(self) -> None:
        values = tuple(getattr(self, item) for item in self.__dataclass_fields__)
        if any(type(value) is not int or value < 1 for value in values):
            raise McpContractError("mcp.schema_invalid")


@dataclass(frozen=True, slots=True)
class McpCanonicalJson:
    """A bounded canonical JSON value and its public SHA-256 contract digest."""

    value: Any = field(repr=False)
    canonical_json: str = field(repr=False)
    sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class McpSchemaArtifact:
    """Immutable original Schema and deterministic MetaHub effective Schema."""

    original: Mapping[str, Any] = field(repr=False)
    effective: Mapping[str, Any] = field(repr=False)
    original_json: str = field(repr=False)
    effective_json: str = field(repr=False)
    original_sha256: str
    effective_sha256: str
    original_byte_count: int
    effective_byte_count: int


@dataclass(frozen=True, slots=True)
class McpSchemaBundle:
    """A required input artifact and an optional output artifact."""

    input: McpSchemaArtifact
    output: McpSchemaArtifact | None

    @property
    def total_byte_count(self) -> int:
        output_bytes = 0
        if self.output is not None:
            output_bytes = self.output.original_byte_count + self.output.effective_byte_count
        return self.input.original_byte_count + self.input.effective_byte_count + output_bytes


@dataclass(frozen=True, slots=True)
class McpCryptoOwner:
    """Trusted owner dimensions used to build MCP AES-GCM AAD."""

    tenant_id: str
    owner_id: str
    kind: McpCryptoKind
    key_version: int


@dataclass(frozen=True, slots=True)
class McpHmacOwner:
    """Trusted owner dimensions used for tenant/domain keyed fingerprints."""

    tenant_id: str
    owner_id: str
    kind: McpHmacKind
    key_version: int


@dataclass(frozen=True, slots=True)
class McpVersionedSecret:
    """One current or retained MCP key; the secret is never represented."""

    version: int
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpRedactionResult:
    """A detached safe result; raw and canonical values stay out of ``repr``."""

    value: Any = field(repr=False)
    canonical_json: str = field(repr=False)
    redaction_count: int
    byte_count: int


def validate_mcp_tool_name(value: object) -> str:
    """Validate the bounded ASCII remote tool identity used for reconciliation."""

    if type(value) is not str or _TOOL_NAME.fullmatch(value) is None:
        raise McpContractError("mcp.schema_invalid")
    return value


def normalize_mcp_description(value: object) -> str | None:
    """Normalize an optional display-only description without accepting controls."""

    if value is None:
        return None
    if type(value) is not str:
        raise McpContractError("mcp.schema_invalid")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > 500
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        raise McpContractError("mcp.schema_invalid")
    return normalized


def canonicalize_mcp_annotations(value: object) -> McpCanonicalJson:
    """Accept only the four MCP boolean safety hints and encode them deterministically."""

    if value is None:
        safe: dict[str, bool] = {}
    elif type(value) is dict:
        allowed = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        if set(value) - allowed or any(
            type(key) is not str or type(item) is not bool for key, item in value.items()
        ):
            raise McpContractError("mcp.schema_invalid")
        safe = dict(value)
    else:
        raise McpContractError("mcp.schema_invalid")
    encoded = _canonical_json_bytes(safe, code="mcp.schema_invalid", maximum=4_096)
    return McpCanonicalJson(
        value=safe,
        canonical_json=encoded.decode("utf-8"),
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_count=len(encoded),
    )


def canonical_mcp_permission_codes(values: object) -> tuple[str, ...]:
    """Validate an exact 0..8 permission conjunction for one Tool policy."""

    if type(values) not in {list, tuple} or len(values) > 8:
        raise McpContractError("mcp.request_invalid")
    if any(type(value) is not str for value in values) or len(values) != len(set(values)):
        raise McpContractError("mcp.request_invalid")
    try:
        canonical = canonical_permission_codes(values)
    except ValueError:
        raise McpContractError("mcp.request_invalid") from None
    if any(
        value not in PERMISSION_CODES or value in MCP_FORBIDDEN_TOOL_PERMISSION_CODES
        for value in canonical
    ):
        raise McpContractError("mcp.request_invalid")
    return canonical


def validate_mcp_idempotency_key(value: object) -> str:
    """Validate the single visible-ASCII idempotency key accepted by MCP writes."""

    if type(value) is not str or _VISIBLE_ASCII_IDEMPOTENCY.fullmatch(value) is None:
        raise McpContractError("mcp.idempotency_required")
    return value


def validate_mcp_endpoint_url(
    value: object,
    *,
    production: bool,
    allow_test_loopback: bool,
) -> str:
    """Validate the storage-only PRD-082 endpoint profile without resolving DNS."""

    if (
        type(value) is not str
        or not value
        or len(value) > 2_048
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise McpContractError("mcp.request_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise McpContractError("mcp.request_invalid") from None
    if (
        not parsed.scheme
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or parsed.hostname != parsed.hostname.lower()
    ):
        raise McpContractError("mcp.request_invalid")
    try:
        parsed_ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        parsed_ip = None
    is_literal_test_loopback = parsed.hostname in {"127.0.0.1", "::1"}
    canonical_host = (
        f"[{parsed.hostname}]"
        if parsed_ip is not None and parsed_ip.version == 6
        else parsed.hostname
    )
    canonical_netloc = canonical_host if port is None else f"{canonical_host}:{port}"
    if (
        parsed.netloc != canonical_netloc
        or "*" in parsed.hostname
        or "?" in value
        or "#" in value
        or port == 0
    ):
        raise McpContractError("mcp.request_invalid")
    if production:
        if parsed.scheme != "https" or allow_test_loopback:
            raise McpContractError("mcp.request_invalid")
    elif parsed.scheme == "http":
        if not allow_test_loopback or not is_literal_test_loopback:
            raise McpContractError("mcp.request_invalid")
    elif parsed.scheme != "https":
        raise McpContractError("mcp.request_invalid")
    if parsed.scheme == "https" and port == 443:
        raise McpContractError("mcp.request_invalid")
    if parsed.scheme == "http" and port == 80:
        raise McpContractError("mcp.request_invalid")
    if parsed.path and any(segment in {".", ".."} for segment in parsed.path.split("/")):
        raise McpContractError("mcp.request_invalid")
    return value


def canonical_json_bytes(value: object, *, maximum: int, code: str) -> bytes:
    """Public strict canonical JSON helper for bounded MCP intent material."""

    if type(maximum) is not int or maximum < 1 or type(code) is not str or not code:
        raise McpContractError("mcp.request_invalid")
    return _canonical_json_bytes(value, code=code, maximum=maximum)


def _canonical_json_bytes(value: object, *, code: str, maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise McpContractError(code) from None
    if len(encoded) > maximum:
        raise McpContractError(code)
    return encoded


def ensure_json_object(value: object, *, code: str) -> dict[str, Any]:
    """Return a detached exact JSON object, rejecting non-string keys and non-finite values."""

    if type(value) is not dict or any(type(key) is not str for key in value):
        raise McpContractError(code)
    encoded = _canonical_json_bytes(value, code=code, maximum=2**31 - 1)
    return json.loads(encoded)


def ensure_exact_string_sequence(values: object, *, maximum: int) -> tuple[str, ...]:
    """Internal helper for duplicate-free bounded policy strings."""

    if type(values) not in {list, tuple} or len(values) > maximum:
        raise McpContractError("mcp.request_invalid")
    if any(type(value) is not str for value in values) or len(values) != len(set(values)):
        raise McpContractError("mcp.request_invalid")
    return tuple(values)


def canonical_sha256(value: object, *, maximum: int, code: str) -> str:
    """Hash canonical public metadata such as Schema or annotations."""

    return hashlib.sha256(canonical_json_bytes(value, maximum=maximum, code=code)).hexdigest()


def iterable_strings(value: Iterable[object]) -> tuple[str, ...]:
    """Convert an already trusted iterable without stringifying submitted objects."""

    items = tuple(value)
    if any(type(item) is not str for item in items):
        raise McpContractError("mcp.request_invalid")
    return items
