"""PRD-082 Shared RBAC, Schema, crypto, redaction, event, and runtime boundaries."""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from metahub_shared.mcp import (
    DEFAULT_MCP_ENCRYPTION_SECRET,
    DEFAULT_MCP_HMAC_SECRET,
    McpContractError,
    McpCryptoOwner,
    McpHmacOwner,
    McpSchemaLimits,
    canonical_mcp_permission_codes,
    canonical_redaction_pointers,
    canonicalize_and_harden_schemas,
    decrypt_mcp_value,
    encrypt_mcp_value,
    mcp_hmac,
    project_mcp_result,
    redact_result,
    validate_arguments,
    validate_mcp_endpoint_url,
    validate_mcp_tool_called_event,
)
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    ROLE_GRANTABLE_PERMISSIONS,
)
from metahub_shared.runtime.config import ConfigError, ConfigValue, _load_mcp_config
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.webhooks import build_webhook_event

JSONSCHEMA_AVAILABLE = importlib.util.find_spec("jsonschema") is not None
NOW = datetime(2026, 8, 16, 3, 4, 5, tzinfo=UTC)
TENANT_ID = "tenant_prd082"
ACTOR_ID = "user_prd082_developer"
INVOCATION_ID = "mcpi_" + "a" * 32
SERVER_ID = "mcps_" + "b" * 32
TOOL_ID = "mcpt_" + "c" * 32
EVENT_ID = "mcpevt_" + "d" * 32


def test_prd082_rbac_revision_17_has_exact_default_role_deltas() -> None:
    expected = {
        "mcp.read": ("read", {"admin", "developer", "security_admin", "ops_admin"}),
        "mcp.server.manage": ("critical", {"admin", "developer", "ops_admin"}),
        "mcp.policy.manage": ("critical", {"admin", "security_admin"}),
        "mcp.invoke": ("write", {"admin", "developer"}),
        "mcp.approve": ("critical", {"admin", "security_admin"}),
    }
    assert CATALOG_REVISION == 17
    for code, (risk, roles) in expected.items():
        definition = PERMISSION_BY_CODE[code]
        assert definition.category == "mcp"
        assert definition.risk_level == risk
        assert {
            role for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items() if code in permissions
        } == roles


def test_prd082_contract_helpers_reject_approval_permissions_and_noncanonical_endpoints() -> None:
    assert canonical_mcp_permission_codes(["file.read", "request_log.read"]) == (
        "file.read",
        "request_log.read",
    )
    for values in (["mcp.approve"], ["approval.decide"], ["file.read", "file.read"]):
        with pytest.raises(McpContractError, match="mcp.request_invalid"):
            canonical_mcp_permission_codes(values)
    assert (
        validate_mcp_endpoint_url(
            "https://mcp.example.test/v1",
            production=True,
            allow_test_loopback=False,
        )
        == "https://mcp.example.test/v1"
    )
    assert (
        validate_mcp_endpoint_url(
            "http://127.0.0.1:8765/mcp",
            production=False,
            allow_test_loopback=True,
        )
        == "http://127.0.0.1:8765/mcp"
    )
    for endpoint in (
        "https://user:secret@mcp.example.test/v1",
        "https://mcp.example.test/v1?token=secret",
        "http://localhost:8765/mcp",
        "https://mcp.example.test:443/v1",
    ):
        with pytest.raises(McpContractError, match="mcp.request_invalid"):
            validate_mcp_endpoint_url(
                endpoint,
                production=endpoint.startswith("https://"),
                allow_test_loopback=True,
            )


@pytest.mark.parametrize("kind", ["endpoint", "bearer", "arguments", "result"])
def test_prd082_crypto_is_domain_owner_and_version_bound(kind: str) -> None:
    plaintext = b'{"safe":"value"}'
    owner = McpCryptoOwner(TENANT_ID, INVOCATION_ID, kind, 7)  # type: ignore[arg-type]
    encrypted = encrypt_mcp_value(
        plaintext,
        secret=DEFAULT_MCP_ENCRYPTION_SECRET,
        owner=owner,
    )
    assert plaintext.decode() not in encrypted
    assert decrypt_mcp_value(
        encrypted,
        secret=DEFAULT_MCP_ENCRYPTION_SECRET,
        owner=owner,
    ) == plaintext
    for changed_owner in (
        McpCryptoOwner("tenant_other", INVOCATION_ID, kind, 7),  # type: ignore[arg-type]
        McpCryptoOwner(TENANT_ID, SERVER_ID, kind, 7),  # type: ignore[arg-type]
        McpCryptoOwner(TENANT_ID, INVOCATION_ID, kind, 8),  # type: ignore[arg-type]
    ):
        with pytest.raises(McpContractError, match="mcp.unavailable"):
            decrypt_mcp_value(
                encrypted,
                secret=DEFAULT_MCP_ENCRYPTION_SECRET,
                owner=changed_owner,
            )
    assert plaintext.decode() not in repr(owner)


def test_prd082_hmac_is_tenant_owner_kind_and_version_separated() -> None:
    value = b"same-input"
    owners = (
        McpHmacOwner(TENANT_ID, INVOCATION_ID, "arguments", 3),
        McpHmacOwner(TENANT_ID, INVOCATION_ID, "result", 3),
        McpHmacOwner(TENANT_ID, INVOCATION_ID, "arguments", 4),
        McpHmacOwner("tenant_other", INVOCATION_ID, "arguments", 3),
    )
    digests = {
        mcp_hmac(value, secret=DEFAULT_MCP_HMAC_SECRET, owner=owner) for owner in owners
    }
    assert len(digests) == len(owners)
    assert all(len(digest) == 64 for digest in digests)


def test_prd082_redactor_uses_exact_normalized_keys_and_rfc6901_pointers() -> None:
    raw = {
        "access-token": "secret-access",
        "nested": {
            "provider_key": "secret-provider",
            "token_count": 17,
            "secretary": "safe",
            "optional": [{"value": "redact-by-pointer"}],
        },
    }
    result = redact_result(raw, ["/nested/optional/0/value", "/missing/path"])
    assert result.value == {
        "access-token": "[REDACTED]",
        "nested": {
            "provider_key": "[REDACTED]",
            "token_count": 17,
            "secretary": "safe",
            "optional": [{"value": "[REDACTED]"}],
        },
    }
    assert result.redaction_count == 3
    assert "secret-access" not in result.canonical_json
    assert raw["access-token"] == "secret-access"
    assert canonical_redaction_pointers(["/z", "/a~1b", "/a~0b"]) == (
        "/a~0b",
        "/a~1b",
        "/z",
    )
    with pytest.raises(McpContractError, match="mcp.result_invalid"):
        redact_result({"value": "scalar"}, ["/value/nested"])
    with pytest.raises(McpContractError, match="mcp.request_invalid"):
        canonical_redaction_pointers(["/bad~2escape"])
    projected = project_mcp_result(
        [{"type": "text", "text": "safe"}],
        {"api_key": "secret", "token_count": 3},
    )
    assert projected.value["structuredContent"] == {
        "api_key": "[REDACTED]",
        "token_count": 3,
    }
    with pytest.raises(McpContractError, match="mcp.result_invalid"):
        project_mcp_result([{"type": "image", "data": "secret"}])


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema dependency is not installed")
def test_prd082_schema_preserves_original_and_hardens_effective_validation() -> None:
    original = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "items": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["query"],
    }
    bundle = canonicalize_and_harden_schemas(original)
    assert "additionalProperties" not in bundle.input.original
    assert bundle.input.effective["additionalProperties"] is False
    assert bundle.input.effective["maxProperties"] == 256
    assert bundle.input.effective["properties"]["query"]["maxLength"] == 65_536
    assert bundle.input.original_sha256 != bundle.input.effective_sha256
    assert validate_arguments(
        {"query": "safe", "items": [1, 2]},
        bundle.input.effective,
    ) == b'{"items":[1,2],"query":"safe"}'
    with pytest.raises(McpContractError, match="mcp.arguments_invalid"):
        validate_arguments({"query": "safe", "unknown": True}, bundle.input.effective)
    assert original == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "items": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["query"],
    }


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema dependency is not installed")
@pytest.mark.parametrize(
    "schema",
    [
        {
            "type": "object",
            "oneOf": [
                {
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                {
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
            ],
        },
        {
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "anyOf": [
                        {"properties": {"query": {"type": "string"}}},
                        {"properties": {"id": {"type": "integer"}}},
                    ],
                }
            },
        },
    ],
)
def test_prd082_schema_rejects_composed_fields_without_local_carrier(
    schema: object,
) -> None:
    with pytest.raises(McpContractError, match="mcp.schema_invalid"):
        canonicalize_and_harden_schemas(schema)


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema dependency is not installed")
def test_prd082_schema_keeps_locally_declared_one_of_fields_valid() -> None:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "id": {"type": "integer"},
        },
        "oneOf": [
            {
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            {
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        ],
    }

    bundle = canonicalize_and_harden_schemas(schema)

    assert validate_arguments({"query": "safe"}, bundle.input.effective) == (
        b'{"query":"safe"}'
    )
    assert validate_arguments({"id": 7}, bundle.input.effective) == b'{"id":7}'


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema dependency is not installed")
def test_prd082_schema_enforces_declared_allowed_formats() -> None:
    bundle = canonicalize_and_harden_schemas(
        {
            "type": "object",
            "properties": {"resource": {"type": "string", "format": "uri"}},
            "required": ["resource"],
        }
    )

    assert validate_arguments(
        {"resource": "urn:metahub:resource"}, bundle.input.effective
    ) == b'{"resource":"urn:metahub:resource"}'
    with pytest.raises(McpContractError, match="mcp.arguments_invalid"):
        validate_arguments({"resource": "not a uri"}, bundle.input.effective)


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema dependency is not installed")
@pytest.mark.parametrize(
    "schema",
    [
        b'{"type":"object","type":"object"}',
        {"type": "object", "$ref": "#/$defs/value"},
        {"type": "object", "x-executable-validator": "unsafe"},
        {"type": "object", "properties": {"value": {"type": "string", "pattern": "(a+)+"}}},
        {"type": "object", "properties": {"value": {"type": "string", "format": "custom"}}},
    ],
)
def test_prd082_schema_rejects_duplicate_ref_custom_regex_and_format(schema: object) -> None:
    with pytest.raises(McpContractError, match="mcp.schema_invalid"):
        canonicalize_and_harden_schemas(schema)


def _event_payload(*, unknown: bool = False) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mcp_invocation_id": INVOCATION_ID,
        "mcp_server_id": SERVER_ID,
        "mcp_tool_id": TOOL_ID,
        "status": "outcome_unknown" if unknown else "succeeded",
        "risk_level": "standard",
        "approval_required": False,
        "network_attempted": True,
        "input_dlp_status": "clean",
        "input_dlp_match_count": 0,
        "output_dlp_status": "not_scanned" if unknown else "clean",
        "output_dlp_match_count": 0,
        "redaction_count": 0,
        "probe_attempt_count": 1,
        "call_attempt_count": 1,
        "duration_ms": 25,
        "request_bytes": 24,
        "response_bytes": 0 if unknown else 32,
        "execution_status": "unknown" if unknown else "called",
        "invocation_count": 1,
        "tool_call_count": None if unknown else 1,
        "cost_status": "unknown" if unknown else "not_reported",
        "error_code": "mcp.outcome_unknown" if unknown else None,
        "completed_at": "2026-08-16T03:04:05Z",
    }


@pytest.mark.parametrize("unknown", [False, True])
def test_prd082_mcp_event_is_exact_and_bound_to_persisted_source(unknown: bool) -> None:
    payload = _event_payload(unknown=unknown)
    safe = validate_mcp_tool_called_event(
        payload,
        event_id=EVENT_ID,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        object_id=INVOCATION_ID,
        trace_id="trace_prd082",
        source_request_id="req_prd082",
        occurred_at=NOW,
    )
    assert safe == payload
    event = build_webhook_event(
        tenant_id=TENANT_ID,
        event_type="mcp.tool_called",
        payload=payload,
        actor_id=ACTOR_ID,
        object_id=INVOCATION_ID,
        trace_id="trace_prd082",
        source_request_id="req_prd082",
        event_id=EVENT_ID,
        timestamp=NOW,
    )
    assert event.payload["tool_call_count"] is (None if unknown else 1)
    changed = deepcopy(payload)
    changed["arguments"] = {"token": "must-not-escape"}
    with pytest.raises(McpContractError, match="mcp.unavailable"):
        validate_mcp_tool_called_event(
            changed,
            event_id=EVENT_ID,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            object_id=INVOCATION_ID,
            trace_id="trace_prd082",
            source_request_id="req_prd082",
            occurred_at=NOW,
        )


def _mcp_approval_created_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "approval_id": "apr_" + "e" * 32,
        "status": "pending",
        "target_type": "mcp_tool_invocation",
        "target_action": "invoke",
        "risk_level": "high",
        "requires_step_up": True,
        "expires_at": "2026-08-16T03:09:05Z",
        "entry": "approval-card",
    }


def test_prd082_mcp_approval_created_webhook_accepts_high_risk_invoke() -> None:
    payload = _mcp_approval_created_payload()
    event = build_webhook_event(
        tenant_id=TENANT_ID,
        event_type="approval.created",
        payload=payload,
        actor_id=ACTOR_ID,
        object_id=payload["approval_id"],
        trace_id="trace_prd082_approval",
        source_request_id="req_prd082_approval",
        event_id="apevt_" + "f" * 32,
        timestamp=NOW,
    )
    assert event.payload == payload
    assert event.to_payload()["event_type"] == "approval.created"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_level", "standard"),
        ("requires_step_up", False),
        ("target_action", "unknown_action"),
    ],
)
def test_prd082_mcp_approval_created_webhook_fails_closed_on_drift(
    field: str,
    value: object,
) -> None:
    payload = _mcp_approval_created_payload()
    payload[field] = value
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id=TENANT_ID,
            event_type="approval.created",
            payload=payload,
            actor_id=ACTOR_ID,
            object_id=payload["approval_id"],
            trace_id="trace_prd082_approval",
            source_request_id="req_prd082_approval",
            event_id="apevt_" + "f" * 32,
            timestamp=NOW,
        )


def _mcp_config_values(**overrides: str) -> dict[str, ConfigValue]:
    defaults = {
        "METAHUB_MCP_ENABLED": "false",
        "METAHUB_MCP_ENCRYPTION_VERSION": "1",
        "METAHUB_MCP_ENCRYPTION_SECRET": "",
        "METAHUB_MCP_ENCRYPTION_PREVIOUS_VERSION": "",
        "METAHUB_MCP_ENCRYPTION_PREVIOUS_SECRET": "",
        "METAHUB_MCP_HMAC_VERSION": "1",
        "METAHUB_MCP_HMAC_SECRET": "",
        "METAHUB_MCP_HMAC_PREVIOUS_VERSION": "",
        "METAHUB_MCP_HMAC_PREVIOUS_SECRET": "",
        "METAHUB_MCP_EXECUTOR_MODE": "disabled",
        "METAHUB_MCP_TEST_LOOPBACK_ENABLED": "false",
        "METAHUB_MCP_SCHEDULER_ENABLED": "false",
        "METAHUB_MCP_SCHEDULER_POLL_INTERVAL_SECONDS": "2",
        "METAHUB_MCP_SCHEDULER_BATCH_SIZE": "20",
        "METAHUB_MCP_GLOBAL_CONCURRENCY": "4",
        "METAHUB_MCP_TENANT_CONCURRENCY": "1",
        "METAHUB_MCP_OPERATION_LEASE_SECONDS": "60",
        "METAHUB_MCP_INVOCATION_LEASE_SECONDS": "90",
        "METAHUB_MCP_PROBE_DEADLINE_SECONDS": "10",
        "METAHUB_MCP_CALL_DEADLINE_SECONDS": "30",
        "METAHUB_MCP_REGISTER_MAX_BYTES": "16384",
        "METAHUB_MCP_REFRESH_MAX_BYTES": "4096",
        "METAHUB_MCP_POLICY_MAX_BYTES": "32768",
        "METAHUB_MCP_INVOKE_MAX_BYTES": "131072",
        "METAHUB_MCP_ARGUMENTS_MAX_BYTES": "65536",
        "METAHUB_MCP_SCHEMA_MAX_BYTES": "65536",
        "METAHUB_MCP_SERVER_SCHEMA_MAX_BYTES": "1048576",
        "METAHUB_MCP_RESULT_MAX_BYTES": "1048576",
        "METAHUB_MCP_TREE_MAX_DEPTH": "12",
        "METAHUB_MCP_TREE_MAX_NODES": "1024",
        "METAHUB_MCP_SCHEMA_MAX_PROPERTIES": "256",
        "METAHUB_MCP_SCHEMA_MAX_KEYWORDS": "2048",
        "METAHUB_MCP_SCHEMA_MAX_COMBINATOR_BRANCHES": "64",
        "METAHUB_MCP_SCHEMA_MAX_ENUM_ITEMS": "256",
        "METAHUB_MCP_SCHEMA_MAX_ENUM_ITEM_BYTES": "4096",
        "METAHUB_MCP_SCHEMA_MAX_PATTERN_LENGTH": "512",
        "METAHUB_MCP_VALIDATOR_MAX_STEPS": "100000",
        "METAHUB_MCP_MAX_STRING_LENGTH": "65536",
        "METAHUB_MCP_MAX_ARRAY_ITEMS": "1024",
    }
    defaults.update(overrides)
    return {key: ConfigValue(value, "env") for key, value in defaults.items()}


def test_prd082_config_defaults_keyrings_and_prod_executor_guard() -> None:
    loaded = _load_mcp_config(_mcp_config_values(), environment="test", forbidden_secrets=())
    assert loaded["mcp_enabled"] is False
    assert loaded["mcp_executor_mode"] == "disabled"
    assert loaded["mcp_encryption_secret"] == DEFAULT_MCP_ENCRYPTION_SECRET
    assert loaded["mcp_hmac_secret"] == DEFAULT_MCP_HMAC_SECRET
    with pytest.raises(ConfigError, match="Production MCP executor"):
        _load_mcp_config(
            _mcp_config_values(
                METAHUB_MCP_EXECUTOR_MODE="test_loopback",
                METAHUB_MCP_TEST_LOOPBACK_ENABLED="true",
                METAHUB_MCP_ENCRYPTION_SECRET="e" * 40,
                METAHUB_MCP_HMAC_SECRET="h" * 40,
            ),
            environment="prod",
            forbidden_secrets=(),
        )


def test_prd082_error_i18n_and_runtime_dependency_declarations_are_complete() -> None:
    expected = {
        "mcp.feature_disabled": 503,
        "mcp.executor_disabled": 503,
        "mcp.request_invalid": 422,
        "mcp.schema_invalid": 422,
        "mcp.arguments_blocked": 403,
        "mcp.result_blocked": 403,
        "mcp.outcome_unknown": 503,
        "mcp.unavailable": 503,
    }
    for code, status in expected.items():
        assert ERROR_SPECS[code].http_status == status
        assert error_label(code, "zh-CN") != code
        assert error_label(code, "en-US") != code
    requirements = Path("infra/docker/requirements.runtime.txt").read_text(encoding="utf-8")
    assert "mcp>=1.0,<2.0" in requirements
    assert "jsonschema[format-nongpl]>=4.23,<5.0" in requirements


def test_prd082_fastapi_outer_boundary_ignores_client_ids_and_hides_unhandled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_canary = "prd082-argument-canary-must-not-log"
    app = create_service_app("control-plane-api", "PRD-082 Shared Boundary")
    config = app.state.config
    assert "mcp_encryption_secret" not in config.model_dump()
    assert "mcp_hmac_secret" not in config.model_dump()
    assert DEFAULT_MCP_ENCRYPTION_SECRET not in repr(config)
    assert DEFAULT_MCP_HMAC_SECRET not in repr(config)

    @app.get("/mcp/boom")
    async def boom(request: Request) -> dict[str, str]:
        raise RuntimeError(secret_canary)

    @app.get("/mcp/ok")
    async def ok(request: Request) -> dict[str, str]:
        return {
            "request_id": request.state.request_id,
            "trace_id": request.state.trace_id,
        }

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/mcp/boom",
            headers={"X-Request-ID": secret_canary, "X-Trace-ID": secret_canary},
        )
        assert response.status_code == 500
        assert response.headers["Cache-Control"] == "no-store, private"
        assert response.headers["X-Request-ID"] != secret_canary
        assert response.headers["X-Trace-ID"] != secret_canary
        assert secret_canary not in response.text
        success = client.get(
            "/mcp/ok",
            headers={"X-Request-ID": secret_canary, "X-Trace-ID": secret_canary},
        )
        assert success.status_code == 200
        assert success.headers["Cache-Control"] == "no-store, private"
        assert success.json()["request_id"] != secret_canary
    assert secret_canary not in caplog.text


def test_prd082_schema_limit_projection_is_safe_for_direct_callers() -> None:
    limits = McpSchemaLimits()
    assert limits.max_schema_bytes == 65_536
    assert limits.max_result_bytes == 1_048_576
    with pytest.raises(McpContractError, match="mcp.schema_invalid"):
        McpSchemaLimits(max_nodes=0)
    if not JSONSCHEMA_AVAILABLE:
        with pytest.raises(McpContractError, match="mcp.feature_disabled"):
            canonicalize_and_harden_schemas({"type": "object"})
