"""验证 PRD-086 客户健康评分的 RBAC、错误与 Webhook 公共合同。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    PERMISSION_CATALOG,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.webhooks import (
    CUSTOMER_HEALTH_CHANGED_EVENT_TYPE,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 稳定评分、事件和请求链后缀与窗口时间共同冻结 EVT-098 exact envelope 基线。
SUFFIX = "a" * 32
SCORE_ID = f"chs_{SUFFIX}"
EVENT_SUFFIX = hashlib.sha256(SCORE_ID.encode("ascii")).hexdigest()[:32]
OPERATION_SUFFIX = "c" * 32
WINDOW_END = datetime(2026, 8, 17, 8, 0, 0, tzinfo=UTC)
EVALUATED_AT = WINDOW_END + timedelta(minutes=3)


def _payload(**changes: object) -> dict[str, object]:
    """构造 exact v1 安全载荷，并允许用例覆盖单一字段。"""

    payload: dict[str, object] = {
        "schema_version": "1.0",
        "customer_health_score_id": SCORE_ID,
        "score_version": "customer-health-v1",
        "score_bps": 4200,
        "risk_level": "at_risk",
        "risk_tags": ["usage_declining", "high_error_rate"],
        "missing_signals": ["ticket_health"],
        "window_end": "2026-08-17T08:00:00Z",
    }
    payload.update(changes)
    return payload


def _build(payload: dict[str, object], **changes: object):
    """以稳定 Scheduler 身份构造客户健康变更事件。"""

    values: dict[str, object] = {
        "tenant_id": "tenant_prd086",
        "event_type": CUSTOMER_HEALTH_CHANGED_EVENT_TYPE,
        "payload": payload,
        "actor_id": "system:customer-health-scheduler",
        "object_id": SCORE_ID,
        "trace_id": f"trc_chs_{OPERATION_SUFFIX}",
        "source_request_id": f"req_chs_{OPERATION_SUFFIX}",
        "event_id": f"evt_chs_{EVENT_SUFFIX}",
        "timestamp": EVALUATED_AT,
    }
    values.update(changes)
    return build_webhook_event(**values)


def test_prd140_retires_prd086_tenant_permission() -> None:
    """PRD-140 切换后，企业目录和全部默认角色都不再暴露旧健康读取。"""

    assert CATALOG_REVISION >= 21
    assert len(PERMISSION_CATALOG) >= 89
    assert "customer_health.read" not in PERMISSION_BY_CODE
    for role in ROLE_GRANTABLE_PERMISSIONS:
        assert "customer_health.read" not in ROLE_GRANTABLE_PERMISSIONS[role]


def test_prd140_contracted_role_matrix_hashes_are_exact() -> None:
    """确认移除旧权限后的六个完整角色矩阵摘要没有静默漂移。"""

    assert {
        role: permission_matrix_sha256(permissions)
        for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items()
    } == {
        "admin": "9af89117d6e3eb365c9c90f61778d0fe7408d03a50a6cd32a0d6eb4e61e772fb",
        "developer": "888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686",
        "viewer": "be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af",
        "finance_admin": "843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc",
        "security_admin": "7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb",
        "ops_admin": "71c49cf7d47d0a5092eb50c3727cb93a1409f2f1fe3359d47e477c2f6eefdc6b",
    }


def test_customer_health_changed_accepts_exact_system_projection() -> None:
    """确认固定载荷、系统 actor、评分 owner 与评估时间窗口能够安全签名。"""

    event = _build(_payload())
    assert CUSTOMER_HEALTH_CHANGED_EVENT_TYPE in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert event.event_id == f"evt_chs_{EVENT_SUFFIX}"
    assert event.actor_id == "system:customer-health-scheduler"
    assert event.object_id == SCORE_ID
    assert event.timestamp == "2026-08-17T08:03:00Z"
    assert event.payload == _payload()


@pytest.mark.parametrize(
    "mutation",
    (
        {"schema_version": 1},
        {"customer_health_score_id": "chs_" + "A" * 32},
        {"score_version": "customer-health-v2"},
        {"score_bps": True},
        {"score_bps": 10001},
        {"risk_level": "low"},
        {"risk_level": "healthy"},
        {"risk_tags": ["high_error_rate", "usage_declining"]},
        {"risk_tags": ["usage_declining", "usage_declining"]},
        {"risk_tags": ["support_cases_open"]},
        {"missing_signals": []},
        {"missing_signals": ["ticket_health", "usage_trend"]},
        {"window_end": "2026-08-17T08:00:00+00:00"},
        {"window_end": "2026-08-17T08:00:00.000Z"},
        {"prompt": "private"},
        {"response": "private"},
        {"token": "private"},
        {"provider_key": "private"},
        {"endpoint": "https://private.invalid/v1"},
        {"request_ids": ["req_private"]},
    ),
)
def test_customer_health_changed_rejects_malformed_or_sensitive_payload(
    mutation: dict[str, object],
) -> None:
    """确认类型、顺序、额外字段、敏感正文和假风险均在签名前拒绝。"""

    payload = _payload()
    payload.update(mutation)
    with pytest.raises(ValueError, match=r"^webhook\.payload_(?:invalid|sensitive)$"):
        _build(payload)


@pytest.mark.parametrize(
    "changes",
    (
        {"event_id": "evt_chs_" + "b" * 32},
        {"tenant_id": "tenant\nother"},
        {"actor_id": None},
        {"actor_id": "user_prd086"},
        {"object_id": "chs_" + "b" * 32},
        {"trace_id": "trc_chs_" + "B" * 32},
        {"source_request_id": "req_chs_" + "b" * 31},
        {"timestamp": WINDOW_END - timedelta(seconds=1)},
        {"timestamp": WINDOW_END + timedelta(hours=1)},
        {"timestamp": WINDOW_END + timedelta(hours=1, seconds=1)},
        {"timestamp": datetime(2026, 8, 17, 8, 3, 0)},
    ),
)
def test_customer_health_changed_rejects_envelope_drift(changes: dict[str, object]) -> None:
    """确认恢复 worker 不能替换 Scheduler actor、链路、对象、租户或评估时间。"""

    with pytest.raises(ValueError, match=r"^webhook\.payload_invalid$"):
        _build(_payload(), **changes)


def test_customer_health_changed_payload_is_detached() -> None:
    """确认构造后的安全副本不受调用方后续修改影响。"""

    payload = _payload()
    event = _build(payload)
    payload["score_bps"] = 0
    assert event.payload["score_bps"] == 4200


def test_prd086_error_specs_are_exact_and_generic() -> None:
    """确认客户健康错误状态稳定且公开文本不泄露内部实现。"""

    assert {
        code: (spec.http_status, spec.retryable, spec.owner)
        for code, spec in ERROR_SPECS.items()
        if code.startswith("customer_health.")
    } == {
        "customer_health.request_invalid": (422, False, "customer_health"),
        "customer_health.not_ready": (503, True, "customer_health"),
        "customer_health.unavailable": (503, True, "customer_health"),
    }
    public_text = " ".join(
        f"{spec.message} {spec.suggestion}".lower()
        for code, spec in ERROR_SPECS.items()
        if code.startswith("customer_health.")
    )
    assert all(
        sensitive not in public_text
        for sensitive in ("sqlite", "table", "path", "token", "prompt", "response", "key")
    )
