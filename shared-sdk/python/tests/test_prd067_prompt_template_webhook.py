"""PRD-067 Prompt 发布 Webhook 的七字段 payload、信封和敏感键门禁测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 固定领域 ID 只验证 Webhook 信封与安全摘要，不对应真实 tenant、用户或生产资源。
_TEMPLATE_ID = "ptpl_" + "a" * 32
_VERSION_ID = "ptv_" + "b" * 32
_EVENT_ID = "ptev_" + "c" * 32


def _payload() -> dict[str, object]:
    """返回不含名称、正文、变量、绑定清单或变更说明的固定发布摘要。"""

    return {
        "template_id": _TEMPLATE_ID,
        "version_id": _VERSION_ID,
        "version_number": 2,
        "template_revision": 3,
        "project_scope": "selected",
        "project_count": 1,
        "recommended_model_count": 2,
    }


def _build(payload: dict[str, object] | None = None, **overrides: object):
    """构造一个完整持久发布信封，供 payload 与 owner 负例复用。"""

    values: dict[str, object] = {
        "tenant_id": "tenant_default",
        "event_type": "prompt_template.published",
        "event_id": _EVENT_ID,
        "timestamp": datetime(2026, 8, 2, 9, 0, tzinfo=UTC),
        "actor_id": "user_admin",
        "object_id": _VERSION_ID,
        "trace_id": "trace-prd067-publish",
        "source_request_id": "request-prd067-publish",
        "payload": _payload() if payload is None else payload,
    }
    values.update(overrides)
    return build_webhook_event(**values)


def test_prompt_template_published_is_subscribable_and_exact() -> None:
    """合法事件保留稳定 ptev_ 身份、精确版本对象和七字段安全 payload。"""

    assert "prompt_template.published" in WEBHOOK_EVENT_TYPES
    assert "prompt_template.published" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    event = _build()
    assert event.event_id == _EVENT_ID
    assert event.object_id == _VERSION_ID
    assert event.payload == _payload()
    assert set(event.payload) == {
        "template_id",
        "version_id",
        "version_number",
        "template_revision",
        "project_scope",
        "project_count",
        "recommended_model_count",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"name": "不得外发"}),
        lambda value: value.update({"variables": []}),
        lambda value: value.update({"project_scope": "all"}),
        lambda value: value.update({"project_count": 0}),
        lambda value: value.update({"recommended_model_count": 9}),
        lambda value: value.update({"template_revision": 1}),
        lambda value: value.update({"version_id": "ptv_" + "B" * 32}),
    ],
)
def test_prompt_template_published_rejects_payload_drift(mutation) -> None:
    """额外敏感键、跨字段矛盾、越界计数和非 canonical ID 均在签名前拒绝。"""

    payload = _payload()
    mutation(payload)
    with pytest.raises(ValueError, match="webhook.payload"):
        _build(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "evt_" + "c" * 32},
        {"actor_id": None},
        {"object_id": _TEMPLATE_ID},
        {"trace_id": None},
        {"source_request_id": None},
        {"tenant_id": "tenant with space"},
    ],
)
def test_prompt_template_published_rejects_envelope_owner_drift(
    overrides: dict[str, object],
) -> None:
    """source event、actor、版本对象、tenant 与 request/trace 任一漂移都失败关闭。"""

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(**overrides)
