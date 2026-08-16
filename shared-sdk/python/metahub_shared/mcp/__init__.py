"""Shared PRD-082 MCP contracts.

Schema entry points import ``jsonschema`` lazily so unrelated service startup does
not accidentally activate the optional MCP validation path when the feature is
disabled.  Deployment images still declare ``jsonschema`` as a required runtime
dependency.
"""

from __future__ import annotations

from typing import Any

from .contracts import (
    MCP_CRYPTO_KINDS,
    MCP_FORBIDDEN_TOOL_PERMISSION_CODES,
    MCP_HMAC_KINDS,
    MCP_SENSITIVE_RESULT_KEYS,
    McpCanonicalJson,
    McpContractError,
    McpCryptoKind,
    McpCryptoOwner,
    McpHmacKind,
    McpHmacOwner,
    McpRedactionResult,
    McpSchemaArtifact,
    McpSchemaBundle,
    McpSchemaLimits,
    McpVersionedSecret,
    canonical_json_bytes,
    canonical_mcp_permission_codes,
    canonicalize_mcp_annotations,
    normalize_mcp_description,
    validate_mcp_endpoint_url,
    validate_mcp_idempotency_key,
    validate_mcp_tool_name,
)
from .crypto import (
    DEFAULT_MCP_ENCRYPTION_SECRET,
    DEFAULT_MCP_HMAC_SECRET,
    MCP_CIPHERTEXT_FORMAT,
    decrypt_mcp_value,
    decrypt_mcp_value_from_keyring,
    encrypt_mcp_value,
    mcp_hmac,
    mcp_hmac_candidates,
    validate_mcp_keyring,
    validate_mcp_secret_domains,
)
from .events import (
    MCP_TOOL_CALLED_EVENT_TYPE,
    validate_mcp_tool_called_event,
    validate_mcp_tool_called_payload,
)
from .redaction import (
    MCP_REDACTED_VALUE,
    canonical_redaction_pointers,
    project_mcp_result,
    redact_result,
    validate_json_tree,
)

__all__ = [
    "DEFAULT_MCP_ENCRYPTION_SECRET",
    "DEFAULT_MCP_HMAC_SECRET",
    "MCP_CIPHERTEXT_FORMAT",
    "MCP_CRYPTO_KINDS",
    "MCP_FORBIDDEN_TOOL_PERMISSION_CODES",
    "MCP_HMAC_KINDS",
    "MCP_REDACTED_VALUE",
    "MCP_SENSITIVE_RESULT_KEYS",
    "MCP_TOOL_CALLED_EVENT_TYPE",
    "McpCanonicalJson",
    "McpContractError",
    "McpCryptoKind",
    "McpCryptoOwner",
    "McpHmacKind",
    "McpHmacOwner",
    "McpRedactionResult",
    "McpSchemaArtifact",
    "McpSchemaBundle",
    "McpSchemaLimits",
    "McpVersionedSecret",
    "canonical_json_bytes",
    "canonical_mcp_permission_codes",
    "canonical_redaction_pointers",
    "canonicalize_and_harden_schemas",
    "canonicalize_mcp_annotations",
    "decrypt",
    "decrypt_mcp_value",
    "decrypt_mcp_value_from_keyring",
    "encrypt",
    "encrypt_mcp_value",
    "hmac",
    "mcp_hmac",
    "mcp_hmac_candidates",
    "normalize_mcp_description",
    "project_mcp_result",
    "redact_result",
    "validate_arguments",
    "validate_event",
    "validate_json_tree",
    "validate_mcp_endpoint_url",
    "validate_mcp_idempotency_key",
    "validate_mcp_keyring",
    "validate_mcp_secret_domains",
    "validate_mcp_tool_called_event",
    "validate_mcp_tool_called_payload",
    "validate_mcp_tool_name",
    "validate_result",
    "validate_server_schema_budget",
]


def canonicalize_and_harden_schemas(*args: Any, **kwargs: Any) -> McpSchemaBundle:
    try:
        from .schema import canonicalize_and_harden_schemas as implementation
    except ModuleNotFoundError as exc:
        if exc.name == "jsonschema":
            raise McpContractError("mcp.feature_disabled") from None
        raise

    return implementation(*args, **kwargs)


def validate_arguments(*args: Any, **kwargs: Any) -> bytes:
    try:
        from .schema import validate_arguments as implementation
    except ModuleNotFoundError as exc:
        if exc.name == "jsonschema":
            raise McpContractError("mcp.feature_disabled") from None
        raise

    return implementation(*args, **kwargs)


def validate_result(*args: Any, **kwargs: Any) -> bytes:
    try:
        from .schema import validate_result as implementation
    except ModuleNotFoundError as exc:
        if exc.name == "jsonschema":
            raise McpContractError("mcp.feature_disabled") from None
        raise

    return implementation(*args, **kwargs)


def validate_server_schema_budget(*args: Any, **kwargs: Any) -> int:
    try:
        from .schema import validate_server_schema_budget as implementation
    except ModuleNotFoundError as exc:
        if exc.name == "jsonschema":
            raise McpContractError("mcp.feature_disabled") from None
        raise

    return implementation(*args, **kwargs)


# Compact aliases keep Control code readable while preserving explicit public names above.
encrypt = encrypt_mcp_value
decrypt = decrypt_mcp_value
hmac = mcp_hmac
validate_event = validate_mcp_tool_called_event
