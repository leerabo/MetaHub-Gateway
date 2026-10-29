"""Focused PRD-083 Shared contracts for MCP network isolation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from metahub_shared.mcp import (
    DEFAULT_MCP_NETWORK_DENY_PORTS,
    MCP_SANDBOX_RESPONSE_RESERVE_SECONDS,
    McpContractError,
    McpNetworkPolicyCaps,
    McpSandboxReplayCache,
    McpVersionedSecret,
    compile_mcp_network_acl,
    decrypt_mcp_sandbox_response,
    encrypt_mcp_sandbox_response,
    evaluate_mcp_network_destination,
    issue_mcp_sandbox_permit,
    sign_mcp_sandbox_envelope,
    validate_mcp_network_blocked_payload,
    validate_mcp_network_policy,
    verify_mcp_sandbox_envelope,
    verify_mcp_sandbox_permit,
)
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    ROLE_GRANTABLE_PERMISSIONS,
)
from metahub_shared.runtime.config import (
    DEFAULT_CONFIG,
    ConfigError,
    ConfigValue,
    _load_mcp_config,
)
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.mcp_sandbox_config import (
    SandboxConfigError,
    load_mcp_sandbox_config,
)
from metahub_shared.runtime.webhooks import build_webhook_event


def test_network_policy_caps_and_fingerprints_are_fixed_and_strict() -> None:
    assert MCP_SANDBOX_RESPONSE_RESERVE_SECONDS == 6
    caps = McpNetworkPolicyCaps()
    expected_caps = (
        '{"connect_timeout_max_seconds":5,"probe_timeout_max_seconds":10,'
        '"call_timeout_max_seconds":30,"failure_threshold_max":10,'
        '"open_seconds_max":3600}'
    )
    assert caps.canonical_json == expected_caps
    assert caps.fingerprint == hashlib.sha256(expected_caps.encode("ascii")).hexdigest()
    policy = validate_mcp_network_policy(
        {
            "expected_revision": 1,
            "status": "active",
            "egress_mode": "registered_endpoint_only",
            "connect_timeout_seconds": 5,
            "probe_timeout_seconds": 10,
            "call_timeout_seconds": 30,
            "failure_threshold": 3,
            "open_seconds": 60,
        },
        caps=caps,
    )
    assert policy.fingerprint == hashlib.sha256(policy.canonical_json.encode("ascii")).hexdigest()
    for mutation in (
        {**policy.to_dict(), "hostname": "internal.invalid"},
        {**policy.to_dict(), "call_timeout_seconds": 31},
        {
            **policy.to_dict(),
            "connect_timeout_seconds": 2,
            "call_timeout_seconds": 1,
        },
        {**policy.to_dict(), "expected_revision": True},
        {**policy.to_dict(), "egress_mode": "open"},
    ):
        with pytest.raises(McpContractError, match="mcp.network_policy_invalid"):
            validate_mcp_network_policy(mutation, caps=caps)


def test_acl_denies_permanent_mixed_and_ports_but_allows_exact_private_host() -> None:
    acl = compile_mcp_network_acl(private_cidrs=("10.0.0.8/32",))
    allowed = evaluate_mcp_network_destination(
        "https://mcp.example.test/v1", ["93.184.216.34"], acl=acl
    )
    assert allowed.allowed is True
    assert allowed.reason_code is None
    assert "93.184.216.34" not in repr(allowed)
    private = evaluate_mcp_network_destination(
        "https://mcp.internal.test/v1", ["10.0.0.8"], acl=acl
    )
    assert private.allowed is True
    mixed = evaluate_mcp_network_destination(
        "https://mcp.example.test/v1",
        ["93.184.216.34", "127.0.0.1"],
        acl=acl,
    )
    assert (mixed.allowed, mixed.reason_code, mixed.address_count) == (
        False,
        "mixed_dns_answers",
        2,
    )
    for address in ("169.254.169.254", "100.100.100.200", "::ffff:127.0.0.1"):
        denied = evaluate_mcp_network_destination("https://mcp.example.test/v1", [address], acl=acl)
        assert denied.reason_code == "address_denied"
    port = evaluate_mcp_network_destination(
        "https://mcp.example.test:5432/v1", ["93.184.216.34"], acl=acl
    )
    assert port.reason_code == "port_denied"
    redirect = evaluate_mcp_network_destination(
        "https://mcp.example.test/v1", ["93.184.216.34"], acl=acl, redirected=True
    )
    assert redirect.reason_code == "redirect_denied"
    with pytest.raises(McpContractError, match="mcp.feature_disabled"):
        compile_mcp_network_acl(private_cidrs=("10.0.0.0/24",))
    with pytest.raises(McpContractError, match="mcp.feature_disabled"):
        compile_mcp_network_acl(allowed_ports=(443, 5432))


def _network_event_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mcp_server_id": "mcps_" + "1" * 32,
        "operation_id": None,
        "mcp_invocation_id": "mcpi_" + "2" * 32,
        "phase": "call_preflight",
        "reason_code": "address_denied",
        "policy_revision": 2,
        "address_count": 1,
        "attempt_number": 1,
        "tool_call_count": 0,
        "cost_status": "not_applicable",
        "occurred_at": "2026-08-16T00:00:00Z",
    }


def test_network_blocked_event_is_exact_owner_bound_and_sensitive_safe() -> None:
    payload = _network_event_payload()
    assert validate_mcp_network_blocked_payload(payload) == payload
    assert validate_mcp_network_blocked_payload({**payload, "attempt_number": 10}) == {
        **payload,
        "attempt_number": 10,
    }
    with pytest.raises(McpContractError, match="mcp.unavailable"):
        validate_mcp_network_blocked_payload({**payload, "attempt_number": 11})
    event = build_webhook_event(
        tenant_id="tenant-a",
        event_type="mcp.network_blocked",
        payload=payload,
        actor_id=None,
        object_id=payload["mcp_invocation_id"],
        trace_id="trace-safe",
        source_request_id="req-safe",
        event_id="mcpne_" + "3" * 32,
        timestamp=datetime(2026, 8, 16, tzinfo=UTC),
    )
    assert event.payload == payload
    for mutation in (
        {**payload, "resolved_ip": "127.0.0.1"},
        {**payload, "operation_id": "mcpo_" + "4" * 32},
        {**payload, "tool_call_count": 1},
        {**payload, "reason_code": "permit_invalid"},
    ):
        with pytest.raises((McpContractError, ValueError)):
            build_webhook_event(
                tenant_id="tenant-a",
                event_type="mcp.network_blocked",
                payload=mutation,
                actor_id=None,
                object_id=payload["mcp_invocation_id"],
                trace_id="trace-safe",
                source_request_id="req-safe",
                event_id="mcpne_" + "3" * 32,
                timestamp=datetime(2026, 8, 16, tzinfo=UTC),
            )


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sandbox_envelope(now: datetime) -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": "probe",
        "dispatch_id": "mcpd_" + "1" * 32,
        "issued_at": _utc(now),
        "expires_at": _utc(now + timedelta(seconds=5)),
        "tenant_id": "tenant-a",
        "mcp_server_id": "mcps_" + "2" * 32,
        "operation_id": "mcpo_" + "3" * 32,
        "mcp_invocation_id": None,
        "lease_generation": 1,
        "lease_owner_id": "scheduler-1",
        "lease_token_hmac": "4" * 64,
        "network_policy_revision": 2,
        "network_policy_fingerprint": "5" * 64,
        "caps_fingerprint": "6" * 64,
        "deadline_at": _utc(now + timedelta(seconds=10)),
    }


def test_sandbox_envelope_is_short_lived_action_bound_and_replay_safe() -> None:
    now = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
    secret = "envelope-secret-" + "e" * 40
    payload = _sandbox_envelope(now)
    signed = sign_mcp_sandbox_envelope(payload, secret=secret, now=now)
    assert secret not in repr(signed)
    cache = McpSandboxReplayCache(maximum_entries=2)
    assert (
        verify_mcp_sandbox_envelope(
            signed.payload,
            signature=signed.signature,
            secret=secret,
            replay_cache=cache,
            expected_action="probe",
            now=now,
        )["dispatch_id"]
        == payload["dispatch_id"]
    )
    with pytest.raises(McpContractError, match="mcp.sandbox_unavailable"):
        verify_mcp_sandbox_envelope(
            signed.payload,
            signature=signed.signature,
            secret=secret,
            replay_cache=cache,
            now=now,
        )
    with pytest.raises(McpContractError, match="mcp.sandbox_unavailable"):
        sign_mcp_sandbox_envelope(
            {**payload, "expires_at": _utc(now + timedelta(seconds=11))},
            secret=secret,
            now=now,
        )


def _permit_claims(now: datetime) -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": "call",
        "dispatch_id": "mcpd_" + "7" * 32,
        "tenant_id": "tenant-a",
        "mcp_server_id": "mcps_" + "8" * 32,
        "mcp_tool_id": "mcpt_" + "f" * 32,
        "mcp_invocation_id": "mcpi_" + "9" * 32,
        "mcp_network_decision_id": "mcpnd_" + "0" * 32,
        "mcp_network_call_intent_id": "mcpni_" + "a" * 32,
        "lease_generation": 2,
        "lease_token_hmac": "b" * 64,
        "network_policy_revision": 3,
        "network_policy_fingerprint": "c" * 64,
        "endpoint_hmac": "d" * 64,
        "endpoint_hmac_key_version": 1,
        "credential_revision": 4,
        "credential_id": "mcpc_" + "1" * 32,
        "credential_ciphertext_sha256": "2" * 64,
        "auth_type": "bearer",
        "schema_revision": 5,
        "tool_policy_revision": 6,
        "tool_policy_fingerprint": "3" * 64,
        "tool_name_sha256": hashlib.sha256(b"safe_tool").hexdigest(),
        "arguments_hmac": "e" * 64,
        "arguments_hmac_key_version": 1,
        "expires_at": int((now + timedelta(seconds=10)).timestamp()),
        "scheme": "https",
        "hostname": "mcp.example.test",
        "port": 443,
        "pinned_ip": "93.184.216.34",
        "address_count": 1,
    }


def test_worker_only_permit_is_boot_bound_digestable_and_tamper_evident() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    secret = "permit-secret-" + "p" * 40
    permit = issue_mcp_sandbox_permit(
        _permit_claims(now), secret=secret, boot_id="sandbox-boot-1", now=now
    )
    assert secret not in repr(permit)
    assert permit.digest == hashlib.sha256(permit.value.encode("ascii")).hexdigest()
    replay_cache = McpSandboxReplayCache()
    expected = {
        key: _permit_claims(now)[key]
        for key in (
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
            "scheme",
            "hostname",
            "port",
            "pinned_ip",
            "address_count",
        )
    }
    claims = verify_mcp_sandbox_permit(
        permit.value,
        secret=secret,
        boot_id="sandbox-boot-1",
        replay_cache=replay_cache,
        expected_claims=expected,
        now=now,
    )
    assert claims["boot_id"] == "sandbox-boot-1"
    tampered_bindings = {
        "mcp_tool_id": "mcpt_" + "0" * 32,
        "mcp_network_decision_id": "mcpnd_" + "1" * 32,
        "mcp_network_call_intent_id": "mcpni_" + "2" * 32,
        "endpoint_hmac_key_version": 2,
        "credential_revision": 5,
        "credential_id": "mcpc_" + "4" * 32,
        "credential_ciphertext_sha256": "5" * 64,
        "schema_revision": 6,
        "tool_policy_revision": 7,
        "tool_policy_fingerprint": "6" * 64,
        "tool_name_sha256": hashlib.sha256(b"other_tool").hexdigest(),
        "arguments_hmac_key_version": 2,
        "hostname": "other.example.test",
        "port": 8443,
        "pinned_ip": "93.184.216.35",
        "address_count": 2,
    }
    for field, value in tampered_bindings.items():
        with pytest.raises(McpContractError, match="mcp.sandbox_unavailable"):
            verify_mcp_sandbox_permit(
                permit.value,
                secret=secret,
                boot_id="sandbox-boot-1",
                replay_cache=McpSandboxReplayCache(),
                expected_claims={**expected, field: value},
                now=now,
            )
    invalid_claims = (
        {**_permit_claims(now), "future_field": "not-reviewed"},
        {**_permit_claims(now), "scheme": "http"},
        {**_permit_claims(now), "hostname": "mcp.example.test\ninternal"},
        {**_permit_claims(now), "port": True},
        {**_permit_claims(now), "pinned_ip": "127.0.0.1%scope"},
        {**_permit_claims(now), "pinned_ip": "::ffff:127.0.0.1"},
        {**_permit_claims(now), "address_count": 17},
    )
    for invalid in invalid_claims:
        with pytest.raises(McpContractError, match="mcp.sandbox_unavailable"):
            issue_mcp_sandbox_permit(
                invalid,
                secret=secret,
                boot_id="sandbox-boot-1",
                now=now,
            )
    for boot_id, value in (
        ("sandbox-boot-2", permit.value),
        ("sandbox-boot-1", permit.value[:-2] + "AA"),
    ):
        with pytest.raises(McpContractError, match="mcp.sandbox_unavailable"):
            verify_mcp_sandbox_permit(
                value,
                secret=secret,
                boot_id=boot_id,
                replay_cache=McpSandboxReplayCache(),
                now=now,
            )
    with pytest.raises(McpContractError, match="mcp.sandbox_unavailable"):
        verify_mcp_sandbox_permit(
            permit.value,
            secret=secret,
            boot_id="sandbox-boot-1",
            replay_cache=replay_cache,
            now=now,
        )


def _response_metadata(now: datetime) -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": "request-safe",
        "dispatch_id": "mcpd_" + "1" * 32,
        "action": "call",
        "tenant_id": "tenant-a",
        "operation_id": None,
        "mcp_invocation_id": "mcpi_" + "2" * 32,
        "lease_generation": 1,
        "lease_owner_id": "scheduler-1",
        "network_policy_revision": 2,
        "network_policy_fingerprint": "3" * 64,
        "status": "succeeded",
        "failure_code": None,
        "deadline_at": _utc(now + timedelta(seconds=30)),
        "request_bytes": 123,
        "response_bytes": 456,
        "duration_ms": 50,
    }


def _response_binding(metadata: dict[str, object]) -> dict[str, object]:
    fields = {
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
    return {key: value for key, value in metadata.items() if key in fields}


def test_response_crypto_authenticates_metadata_payload_and_previous_rotation() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    current_secret = "response-current-" + "r" * 40
    previous_secret = "response-previous-" + "s" * 40
    metadata = _response_metadata(now)
    response = encrypt_mcp_sandbox_response(
        {"safe_count": 1},
        metadata=metadata,
        secret=current_secret,
        key_version=2,
    )
    assert current_secret not in repr(response)
    wire = response.to_wire()
    plaintext = decrypt_mcp_sandbox_response(
        wire,
        keyring=(
            McpVersionedSecret(2, current_secret),
            McpVersionedSecret(1, previous_secret),
        ),
        current_version=2,
        expected_metadata=_response_binding(metadata),
        now=now,
    )
    assert plaintext == b'{"safe_count":1}'
    old = encrypt_mcp_sandbox_response(
        b"old-worker-result",
        metadata=metadata,
        secret=previous_secret,
        key_version=1,
    ).to_wire()
    assert (
        decrypt_mcp_sandbox_response(
            old,
            keyring=(
                McpVersionedSecret(2, current_secret),
                McpVersionedSecret(1, previous_secret),
            ),
            current_version=2,
            expected_metadata=_response_binding(metadata),
            now=now,
        )
        == b"old-worker-result"
    )
    for tampered in (
        {**wire, "duration_ms": 51},
        {**wire, "payload_ciphertext": wire["payload_ciphertext"][:-2] + "AA"},
        {**wire, "response_key_version": 0},
    ):
        with pytest.raises(McpContractError, match="mcp.sandbox_unavailable"):
            decrypt_mcp_sandbox_response(
                tampered,
                keyring=(
                    McpVersionedSecret(2, current_secret),
                    McpVersionedSecret(1, previous_secret),
                ),
                current_version=2,
                expected_metadata=_response_binding(metadata),
                now=now,
            )
    with pytest.raises(McpContractError, match="mcp.sandbox_unavailable"):
        decrypt_mcp_sandbox_response(
            wire,
            keyring=(
                McpVersionedSecret(2, current_secret),
                McpVersionedSecret(1, previous_secret),
            ),
            current_version=2,
            expected_metadata=_response_binding(metadata),
            now=now + timedelta(seconds=31),
        )


def test_rbac_errors_and_i18n_publish_prd083_contracts() -> None:
    assert CATALOG_REVISION >= 18
    assert PERMISSION_BY_CODE["mcp.network.manage"].risk_level == "critical"
    assert "mcp.network.manage" in ROLE_GRANTABLE_PERMISSIONS["admin"]
    assert "mcp.network.manage" in ROLE_GRANTABLE_PERMISSIONS["security_admin"]
    assert "mcp.network.manage" in ROLE_GRANTABLE_PERMISSIONS["ops_admin"]
    for role in ("developer", "finance_admin", "viewer"):
        assert "mcp.network.manage" not in ROLE_GRANTABLE_PERMISSIONS[role]
    expected = {
        "mcp.network_policy_invalid": 422,
        "mcp.network_blocked": 403,
        "mcp.sandbox_unavailable": 503,
    }
    for code, status in expected.items():
        assert ERROR_SPECS[code].http_status == status
        assert error_label(code, "zh-CN") != code
        assert error_label(code, "en-US") != code


def _app_mcp_values(**overrides: str) -> dict[str, ConfigValue]:
    values = {key: value for key, value in DEFAULT_CONFIG.items() if key.startswith("METAHUB_MCP_")}
    values.update(overrides)
    return {key: ConfigValue(value, "env") for key, value in values.items()}


def test_scheduler_app_config_requires_closed_sandbox_profile_and_caps() -> None:
    values = _app_mcp_values(
        METAHUB_MCP_ENABLED="true",
        METAHUB_MCP_ENCRYPTION_SECRET="e" * 40,
        METAHUB_MCP_HMAC_SECRET="h" * 40,
        METAHUB_MCP_EXECUTOR_MODE="sandbox_http",
        METAHUB_MCP_SCHEDULER_ENABLED="true",
        METAHUB_MCP_SANDBOX_BASE_URL="http://mcp-sandbox-worker:8090",
        METAHUB_MCP_SANDBOX_INTERNAL_TOKEN="i" * 40,
        METAHUB_MCP_SANDBOX_ENVELOPE_SECRET="n" * 40,
        METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION="2",
        METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT="r" * 40,
        METAHUB_MCP_SANDBOX_RESPONSE_SECRET_PREVIOUS="p" * 40,
    )
    loaded = _load_mcp_config(
        values,
        environment="prod",
        service_name="mcp-scheduler-worker",
        forbidden_secrets=(),
    )
    assert loaded["mcp_executor_mode"] == "sandbox_http"
    assert loaded["mcp_network_call_timeout_max_seconds"] == 30
    with pytest.raises(ConfigError, match="cannot claim MCP jobs"):
        _load_mcp_config(
            values,
            environment="prod",
            service_name="control-plane-api",
            forbidden_secrets=(),
        )
    with pytest.raises(ConfigError, match="shared network policy caps"):
        _load_mcp_config(
            _app_mcp_values(
                METAHUB_MCP_OPERATION_LEASE_SECONDS="42",
                METAHUB_MCP_NETWORK_PROBE_TIMEOUT_MAX_SECONDS="30",
            ),
            environment="test",
            forbidden_secrets=(),
        )
    boundary = _load_mcp_config(
        _app_mcp_values(
            METAHUB_MCP_OPERATION_LEASE_SECONDS="43",
            METAHUB_MCP_NETWORK_PROBE_TIMEOUT_MAX_SECONDS="30",
        ),
        environment="test",
        forbidden_secrets=(),
    )
    assert boundary["mcp_operation_lease_seconds"] == 43


def _sandbox_values(**overrides: str) -> dict[str, str]:
    values = {
        "METAHUB_MCP_SANDBOX_WORKER_ENABLED": "true",
        "METAHUB_MCP_ENCRYPTION_VERSION": "1",
        "METAHUB_MCP_ENCRYPTION_SECRET": "e" * 40,
        "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN": "i" * 40,
        "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET": "n" * 40,
        "METAHUB_MCP_SANDBOX_PERMIT_SECRET": "p" * 40,
        "METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION": "1",
        "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT": "r" * 40,
        "METAHUB_MCP_SANDBOX_PRIVATE_CIDRS": "10.0.0.8/32",
    }
    values.update(overrides)
    return values


def test_sandbox_config_is_minimal_secret_safe_and_rejects_cross_role_keys() -> None:
    config = load_mcp_sandbox_config(
        {**_sandbox_values(), "PATH": "system-path", "TEMP": "system-temp"}
    )
    assert config.worker_enabled is True
    assert config.caps_fingerprint == config.network_policy_caps.fingerprint
    assert DEFAULT_MCP_NETWORK_DENY_PORTS <= config.network_acl.deny_ports
    assert config.encryption_keyring[0].version == 1
    rendered = repr(config)
    for secret in ("e" * 40, "i" * 40, "n" * 40, "p" * 40, "r" * 40):
        assert secret not in rendered
    for forbidden in (
        "METAHUB_MCP_EXECUTOR_MODE",
        "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_PREVIOUS",
        "METAHUB_MCP_HMAC_SECRET",
        "METAHUB_DATABASE_URL",
        "METAHUB_REDIS_URL",
        "METAHUB_PROVIDER_DEEPSEEK_API_KEY",
        "METAHUB_WEBHOOK_SECRET",
        "METAHUB_FILE_STORAGE_ROOT",
    ):
        with pytest.raises(SandboxConfigError, match="unsupported MCP Sandbox"):
            load_mcp_sandbox_config(_sandbox_values(**{forbidden: "forbidden"}))
    with pytest.raises(SandboxConfigError, match="phase deadlines"):
        load_mcp_sandbox_config(_sandbox_values(METAHUB_MCP_SANDBOX_HEADERS_TIMEOUT_SECONDS="11"))
