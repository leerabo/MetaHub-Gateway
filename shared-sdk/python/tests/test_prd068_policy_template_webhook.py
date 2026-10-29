"""PRD-068 策略模板应用 Webhook 的九字段 payload、信封和敏感键门禁测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 固定领域 ID 只验证 Webhook 合同，不对应真实 tenant、用户或生产资源。
_APPLICATION_ID = "ptapp_" + "a" * 32
_POLICY_ID = "pol_" + "b" * 32
_EVENT_ID = "ptevt_" + "c" * 32


def _payload() -> dict[str, object]:
    """返回不含 DSL、绑定值、价格、正文或命中内容的固定应用摘要。"""

    return {
        "template_code": "customer_service_redaction",
        "template_version": 1,
        "application_id": _APPLICATION_ID,
        "generated_policy_id": _POLICY_ID,
        "previous_snapshot_version": 7,
        "result_snapshot_version": 8,
        "impact_hash": "d" * 64,
        "bindings_hash": "e" * 64,
        "changed_resource_types": ["policy", "policy_snapshot"],
    }


def _build(payload: dict[str, object] | None = None, **overrides: object):
    """构造完整持久应用信封，供 payload 与 owner 负例复用。"""

    values: dict[str, object] = {
        "tenant_id": "tenant_default",
        "event_type": "policy_template.applied",
        "event_id": _EVENT_ID,
        "timestamp": datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        "actor_id": "user_admin",
        "object_id": _APPLICATION_ID,
        "trace_id": "trace-prd068-apply",
        "source_request_id": "request-prd068-apply",
        "payload": _payload() if payload is None else payload,
    }
    values.update(overrides)
    return build_webhook_event(**values)


def test_policy_template_applied_is_subscribable_and_exact() -> None:
    """合法事件保留稳定 ptevt_ 身份、应用 owner 和精确九字段安全 payload。"""

    assert "policy_template.applied" in WEBHOOK_EVENT_TYPES
    assert "policy_template.applied" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    event = _build()
    assert event.event_id == _EVENT_ID
    assert event.object_id == _APPLICATION_ID
    assert event.payload == _payload()
    assert set(event.payload) == {
        "template_code",
        "template_version",
        "application_id",
        "generated_policy_id",
        "previous_snapshot_version",
        "result_snapshot_version",
        "impact_hash",
        "bindings_hash",
        "changed_resource_types",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"dsl": "project == private"}),
        lambda value: value.update({"bindings": {"project_id": "private"}}),
        lambda value: value.update({"template_code": "custom"}),
        lambda value: value.update({"template_code": []}),
        lambda value: value.update({"template_version": True}),
        lambda value: value.update({"application_id": "ptapp_" + "A" * 32}),
        lambda value: value.update({"generated_policy_id": "policy_" + "b" * 32}),
        lambda value: value.update({"result_snapshot_version": 9}),
        lambda value: value.update({"impact_hash": "D" * 64}),
        lambda value: value.update({"impact_hash": 68}),
        lambda value: value.update({"bindings_hash": "e" * 63}),
        lambda value: value.update(
            {"changed_resource_types": ["policy_snapshot", "policy"]}
        ),
    ],
)
def test_policy_template_applied_rejects_payload_drift(mutation) -> None:
    """敏感键、未知模板、身份/版本/hash 漂移和资源顺序变化均在签名前拒绝。"""

    payload = _payload()
    mutation(payload)
    with pytest.raises(ValueError, match="webhook.payload"):
        _build(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "evt_" + "c" * 32},
        {"actor_id": None},
        {"object_id": _POLICY_ID},
        {"trace_id": None},
        {"source_request_id": None},
        {"tenant_id": "tenant with space"},
    ],
)
def test_policy_template_applied_rejects_envelope_owner_drift(
    overrides: dict[str, object],
) -> None:
    """source event、actor、应用对象、tenant 与 request/trace 任一漂移都失败关闭。"""

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(**overrides)
