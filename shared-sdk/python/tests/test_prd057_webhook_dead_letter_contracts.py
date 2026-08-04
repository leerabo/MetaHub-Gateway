"""PRD-057 Webhook 死信内部事件与统一错误码合同测试。

测试只构造不含业务正文的安全摘要，不访问网络和数据库；重点冻结内部事件不可订阅、
固定错误状态与敏感字段递归拒绝，避免死信治理反向制造递归 Webhook 或泄露正文。
"""

from __future__ import annotations

import pytest

from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)


def test_dead_lettered_event_is_registered_but_never_subscribable() -> None:
    """内部死信事实可以构造和追踪，但不能进入客户 Endpoint 订阅目录。"""

    assert "webhook.dead_lettered" in WEBHOOK_EVENT_TYPES
    assert "webhook.dead_lettered" not in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    event = build_webhook_event(
        tenant_id="tenant_prd057",
        event_type="webhook.dead_lettered",
        event_id="evt_prd057_dead_lettered",
        object_id="whdl_" + "1" * 32,
        payload={
            "dead_letter_event_id": "whdl_" + "1" * 32,
            "webhook_delivery_id": "whd_" + "2" * 32,
            "event_type": "budget.threshold_reached",
            "status": "dead_letter",
            "error_code": "webhook.http_error",
        },
    )
    assert event.event_id == "evt_prd057_dead_lettered"
    assert event.payload["status"] == "dead_letter"


def test_dead_lettered_event_still_rejects_sensitive_payload_recursively() -> None:
    """内部来源也不能绕过 Prompt、Token 与响应正文的通用安全过滤。"""

    with pytest.raises(ValueError, match="webhook.payload_sensitive"):
        build_webhook_event(
            tenant_id="tenant_prd057",
            event_type="webhook.dead_lettered",
            payload={"summary": {"prompt": "must-not-leak"}},
        )


def test_dead_letter_error_specs_have_stable_http_and_retry_contracts() -> None:
    """查询、状态冲突、筛选和存储失败分别保持稳定 HTTP 语义。"""

    expected = {
        "webhook.dead_letter_not_found": (404, False),
        "webhook.dead_letter_not_replayable": (409, False),
        "webhook.dead_letter_replay_pending": (409, False),
        "webhook.dead_letter_not_archivable": (409, False),
        "webhook.dead_letter_filter_invalid": (422, False),
        "webhook.dead_letter_unavailable": (503, True),
    }
    actual = {
        code: (ERROR_SPECS[code].http_status, ERROR_SPECS[code].retryable)
        for code in expected
    }
    assert actual == expected
    assert all(ERROR_SPECS[code].owner == "webhook" for code in expected)
