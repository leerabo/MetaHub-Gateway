"""PRD-084 Adapter、RBAC、错误与 ``local_model.called`` Shared 合同。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    PERMISSION_CATALOG,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.local_model_events import (
    LOCAL_MODEL_CALLED_EVENT_TYPE,
    validate_local_model_called_payload,
)
from metahub_shared.runtime.provider_adapter_spec import (
    CATALOG_HASH,
    CATALOG_VERSION,
    EXPECTED_PROVIDER_TYPES,
    LOCAL_COMPATIBLE,
    OPENAI_ADAPTER,
    OPENAI_PROTOCOL,
    provider_adapter_spec,
    provider_adapter_specs,
    provider_types,
    validate_provider_auth,
)
from metahub_shared.runtime.webhooks import (
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

NOW = datetime(2026, 8, 16, 0, 0, 0, tzinfo=UTC)
LOCAL_ENDPOINT_ID = "lme_" + "a" * 32
ROUTING_ATTEMPT_ID = "rta_" + "b" * 32
EVENT_ID = "evt_lmce_" + "c" * 32
REQUEST_ID = "req_prd084_local_call"
TRACE_ID = "trc_prd084_local_call"


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "local_endpoint_id": LOCAL_ENDPOINT_ID,
        "provider_id": "provider_prd084",
        "model_id": "model_prd084",
        "routing_attempt_id": ROUTING_ATTEMPT_ID,
        "request_id": REQUEST_ID,
        "attempt_sequence": 1,
        "attempt_role": "primary",
        "execution_status": "called",
        "outcome": "succeeded",
        "error_code": None,
        "latency_ms": 42,
        "usage_status": "pending",
        "completed_at": "2026-08-16T00:00:00Z",
    }
    payload.update(changes)
    return payload


def _build(payload: dict[str, object], **changes: object):
    values: dict[str, object] = {
        "tenant_id": "tenant_prd084",
        "event_type": LOCAL_MODEL_CALLED_EVENT_TYPE,
        "payload": payload,
        "actor_id": None,
        "object_id": LOCAL_ENDPOINT_ID,
        "trace_id": TRACE_ID,
        "source_request_id": REQUEST_ID,
        "event_id": EVENT_ID,
        "timestamp": NOW,
    }
    values.update(changes)
    return build_webhook_event(**values)


def test_prd084_local_endpoint_private_cidrs_accept_only_enterprise_private_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deployment CIDRs accept only RFC1918/ULA; loopback remains a separate switch."""

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    monkeypatch.setenv(
        "METAHUB_LOCAL_ENDPOINT_PRIVATE_CIDRS",
        "192.168.0.0/16,10.0.0.0/8,fd00::/8,172.16.0.0/12",
    )
    monkeypatch.setenv("METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK", "true")

    config = load_app_config("gateway-data-plane")

    assert config.local_endpoint_private_cidrs == [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fd00::/8",
    ]
    assert config.local_endpoint_allow_loopback is True


@pytest.mark.parametrize(
    "cidr",
    (
        "8.8.8.0/24",
        "0.0.0.0/0",
        "2001:4860::/32",
        "::/0",
        "127.0.0.0/8",
        "::1/128",
    ),
)
def test_prd084_local_endpoint_private_cidrs_reject_public_and_loopback_ranges(
    monkeypatch: pytest.MonkeyPatch,
    cidr: str,
) -> None:
    """Public/default CIDRs fail closed, and allow_loopback cannot admit loopback CIDRs."""

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    monkeypatch.setenv("METAHUB_LOCAL_ENDPOINT_PRIVATE_CIDRS", cidr)
    monkeypatch.setenv("METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK", "true")

    with pytest.raises(ConfigError) as captured:
        load_app_config("gateway-data-plane")

    assert captured.value.code == "infra.config_invalid"


def test_prd084_adapter_catalog_publishes_one_explicit_local_endpoint_spec() -> None:
    """v2 只新增 local_endpoint，旧九类和竞品边界保持关闭。"""

    assert CATALOG_VERSION == "adapter-spec-v2"
    assert CATALOG_HASH == "c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf"
    assert len(EXPECTED_PROVIDER_TYPES) == 10
    assert "local_endpoint" in EXPECTED_PROVIDER_TYPES
    assert {"openrouter", "siliconflow", "silicon_flow"}.isdisjoint(EXPECTED_PROVIDER_TYPES)
    assert provider_types(classification=LOCAL_COMPATIBLE) == frozenset(
        {"openai_compatible", "local_endpoint"}
    )

    spec = provider_adapter_spec("local_endpoint")
    assert spec.adapter_name == OPENAI_ADAPTER
    assert spec.protocol_family == OPENAI_PROTOCOL
    assert spec.allowed_auth_types == ("none", "bearer")
    assert spec.classification == LOCAL_COMPATIBLE
    assert spec.contract_case_id == "prd084_local_endpoint_minimum_contract"
    assert validate_provider_auth("local_endpoint", "none") is spec
    assert validate_provider_auth("local_endpoint", "bearer") is spec


def test_prd084_preserves_the_original_nine_spec_wire_contracts() -> None:
    """从 v2 排除新增项后，旧 v1 canonical bytes 必须逐字保持原发布身份。"""

    legacy_payload = {
        "catalog_version": "adapter-spec-v1",
        "specs": [
            spec.to_wire()
            for spec in provider_adapter_specs()
            if spec.provider_type != "local_endpoint"
        ],
    }
    legacy_bytes = json.dumps(
        legacy_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert len(legacy_bytes) == 13_633
    assert hashlib.sha256(legacy_bytes).hexdigest() == (
        "9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870"
    )


def test_prd084_rbac_revision_and_default_roles_are_exact() -> None:
    """developer 仅 read/test，manage 只授予 admin 与 ops_admin。"""

    codes = {
        "local_endpoint.read",
        "local_endpoint.manage",
        "local_endpoint.test",
    }
    assert CATALOG_REVISION == 19
    assert len(PERMISSION_CATALOG) == 88
    assert {code: PERMISSION_BY_CODE[code].risk_level for code in codes} == {
        "local_endpoint.read": "read",
        "local_endpoint.manage": "critical",
        "local_endpoint.test": "write",
    }
    assert codes <= ROLE_GRANTABLE_PERMISSIONS["admin"]
    assert codes <= ROLE_GRANTABLE_PERMISSIONS["ops_admin"]
    assert codes & ROLE_GRANTABLE_PERMISSIONS["developer"] == {
        "local_endpoint.read",
        "local_endpoint.test",
    }
    for role in ("viewer", "finance_admin", "security_admin"):
        assert codes.isdisjoint(ROLE_GRANTABLE_PERMISSIONS[role])
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["admin"]) == (
        "76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074"
    )
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["developer"]) == (
        "ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1"
    )
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["ops_admin"]) == (
        "aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba"
    )


def test_prd084_error_catalog_i18n_and_diagnosis_are_complete() -> None:
    """Control 与 Gateway 固定错误均可用统一信封和安全诊断消费。"""

    expected = {
        "provider.local_endpoint_requires_local_api": (422, False),
        "local_endpoint.not_found": (404, False),
        "local_endpoint.request_invalid": (422, False),
        "local_endpoint.engine_invalid": (422, False),
        "local_endpoint.url_invalid": (422, False),
        "local_endpoint.auth_invalid": (422, False),
        "local_endpoint.catalog_invalid": (422, False),
        "local_endpoint.conflict": (409, False),
        "local_endpoint.idempotency_conflict": (409, False),
        "local_endpoint.unavailable": (503, True),
        "gateway.local_endpoint_blocked": (502, False),
        "gateway.local_endpoint_unavailable": (503, True),
        "gateway.local_endpoint_outcome_unknown": (502, False),
    }
    assert {
        code: (ERROR_SPECS[code].http_status, ERROR_SPECS[code].retryable) for code in expected
    } == expected
    for code, (status, retryable) in expected.items():
        assert error_label(code, "zh-CN") != code
        assert error_label(code, "en-US") != code
        diagnosis = diagnose_error(code, http_status=status)
        assert diagnosis.matched_by == "error_code"
        assert diagnosis.retryable is retryable
        assert code in diagnosis.safe_summary


def test_local_model_called_accepts_exact_success_and_recovery_outcomes() -> None:
    """成功、失败和不确定终态都保留同一 attempt/request 追踪链。"""

    success = _build(_payload())
    assert success.payload == _payload()
    assert success.event_id == EVENT_ID
    assert LOCAL_MODEL_CALLED_EVENT_TYPE in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES

    failed = _build(
        _payload(
            outcome="failed",
            error_code="gateway.local_endpoint_unavailable",
            usage_status="unavailable",
        )
    )
    assert failed.payload["error_code"] == "gateway.local_endpoint_unavailable"

    recovered = _build(
        _payload(
            outcome="outcome_unknown",
            error_code="gateway.local_endpoint_outcome_unknown",
            usage_status="unavailable",
        )
    )
    assert recovered.payload["outcome"] == "outcome_unknown"


@pytest.mark.parametrize(
    "mutation",
    (
        {"prompt": "forbidden"},
        {"response": "forbidden"},
        {"token": "forbidden"},
        {"provider_key": "forbidden"},
        {"authorization": "forbidden"},
        {"endpoint_url": "https://private.invalid/v1"},
        {"raw_error": "private failure"},
        {"provider_id": "https://private.invalid/v1"},
        {"schema_version": 1},
        {"attempt_sequence": True},
        {"attempt_sequence": 2},
        {"execution_status": "not_called"},
        {"outcome": "succeeded", "error_code": "gateway.upstream_error"},
        {"outcome": "failed", "error_code": "raw failure", "usage_status": "unavailable"},
        {
            "outcome": "failed",
            "error_code": "gateway.private_error",
            "usage_status": "unavailable",
        },
        {"completed_at": "2026-13-16T00:00:00Z"},
    ),
)
def test_local_model_called_rejects_sensitive_extra_or_inconsistent_payload(
    mutation: dict[str, object],
) -> None:
    """未知字段、秘密、完整地址、自由错误和状态漂移均整体失败关闭。"""

    payload = _payload()
    payload.update(mutation)
    with pytest.raises(ValueError, match=r"^webhook\.payload_(?:invalid|sensitive)$"):
        _build(payload)


@pytest.mark.parametrize(
    "changes",
    (
        {"event_id": "lmce_" + "c" * 32},
        {"tenant_id": "tenant\nother"},
        {"actor_id": "usr_prd084"},
        {"object_id": "lme_" + "d" * 32},
        {"trace_id": "trace with spaces"},
        {"source_request_id": "req_other"},
        {"timestamp": NOW + timedelta(seconds=1)},
    ),
)
def test_local_model_called_rejects_envelope_owner_drift(changes: dict[str, object]) -> None:
    """恢复 worker 不能更换持久事件、租户、Endpoint、来源链路或完成秒。"""

    with pytest.raises(ValueError, match=r"^webhook\.payload_invalid$"):
        _build(_payload(), **changes)


def test_local_model_payload_validator_returns_detached_safe_copy() -> None:
    """Shared validator 不保留调用方可变 payload 引用。"""

    payload = _payload()
    validated = validate_local_model_called_payload(payload)
    payload["latency_ms"] = 999
    assert validated["latency_ms"] == 42
