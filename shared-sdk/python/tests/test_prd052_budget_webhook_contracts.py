"""PRD-052 三级预算越限 Webhook 的共享安全合同测试。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)


# 固定 ID 只用于验证稳定信封、Scope 绑定和十六进制格式，不承载真实租户或请求信息。
EVENT_ID = "bsevt_" + "a" * 32
SCOPE_EVENT_ID = "bse_" + "b" * 32
SCOPE_ID = "bsc_" + "c" * 32


def _payload() -> dict[str, object]:
    """返回满足整数微元恒等式的最小 canonical payload。"""

    return {
        "budget_scope_event_id": SCOPE_EVENT_ID,
        "budget_scope_id": SCOPE_ID,
        "budget_rule_id": "br_prd052_project",
        "environment_id": "env_prod",
        "scope_type": "project",
        "scope_id": "prj_billing",
        "budget_date": "2026-07-27",
        "posted_cost_cny": "480",
        "reserved_cost_cny": "10.000000",
        "estimated_request_cost_cny": "10",
        "projected_cost_cny": "500.000000",
        "budget_cny": "500",
        "action_taken": "block",
        "request_id": "req_prd052_budget",
        "conflict_policy": "all_applicable_caps_deny_wins_v1",
    }


def _build(payload: dict[str, object], **overrides: object):
    """使用稳定信封构造测试事件，允许单项覆盖以验证拒绝路径。"""

    arguments = {
        "tenant_id": "tenant_default",
        "event_type": "budget.scope_exceeded",
        "payload": payload,
        "actor_id": None,
        "object_id": SCOPE_ID,
        "trace_id": "trc_prd052_budget",
        "event_id": EVENT_ID,
    }
    arguments.update(overrides)
    return build_webhook_event(**arguments)


def test_budget_scope_exceeded_is_subscribable_and_keeps_stable_envelope() -> None:
    """合法事件应进入订阅目录，并原样绑定持久 ID、Scope 和首次调用链。"""

    event = _build(_payload())

    assert "budget.scope_exceeded" in WEBHOOK_EVENT_TYPES
    assert "budget.scope_exceeded" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert event.event_id == EVENT_ID
    assert event.actor_id is None
    assert event.object_id == SCOPE_ID
    assert event.trace_id == "trc_prd052_budget"
    assert event.payload == _payload()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("budget_scope_event_id", "bse_wrong"),
        ("budget_scope_id", "bsc_wrong"),
        ("scope_type", "department"),
        ("budget_date", "2026-02-30"),
        ("action_taken", "warn"),
        ("conflict_policy", "child_overrides_parent"),
        ("posted_cost_cny", "-1"),
        ("posted_cost_cny", "01"),
        ("posted_cost_cny", "1."),
        ("posted_cost_cny", "1.0000000"),
        ("posted_cost_cny", "1e3"),
    ],
)
def test_budget_scope_exceeded_rejects_invalid_identity_policy_and_money(
    field: str,
    value: object,
) -> None:
    """非法身份、业务日、冲突策略或非普通金额文本必须失败关闭。"""

    payload = _payload()
    payload[field] = value

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


def test_budget_scope_exceeded_rejects_amount_equation_mismatch() -> None:
    """projected 必须等于 posted、reserved 与 estimated 的整数微元之和。"""

    payload = _payload()
    payload["projected_cost_cny"] = "499.999999"

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


def test_budget_scope_exceeded_rejects_non_exceeded_fact() -> None:
    """投递事件时 projected 尚未达到额度不能伪造 scope exceeded 事实。"""

    payload = _payload()
    payload["budget_cny"] = "501"

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


def test_budget_scope_exceeded_rejects_extra_or_sensitive_fields() -> None:
    """精确字段合同拒绝普通扩展字段，通用过滤器还必须拒绝 Prompt 等敏感键。"""

    extra_payload = deepcopy(_payload())
    extra_payload["future_field"] = "not-allowed"
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(extra_payload)

    sensitive_payload = deepcopy(_payload())
    sensitive_payload["prompt"] = "sensitive"
    with pytest.raises(ValueError, match="webhook.payload_sensitive"):
        _build(sensitive_payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "evt_random"},
        {"actor_id": "user_admin"},
        {"object_id": "bsc_" + "d" * 32},
        {"trace_id": None},
    ],
)
def test_budget_scope_exceeded_rejects_unbound_envelope(overrides: dict[str, object]) -> None:
    """信封必须绑定 bsevt 稳定 ID、无 actor、同一 Scope 和非空 trace。"""

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(_payload(), **overrides)
