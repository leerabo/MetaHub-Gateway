"""PRD-063 Adapter spec 错误目录与 Webhook 精确合同测试。

测试只构造冻结版本、hash 和随机样式资源 ID，不访问网络、数据库或真实租户 Endpoint，
也不把 Provider Key、Token、Prompt、Response 或 Authorization 写入测试证据。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.webhooks import (
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 三个常量只构造脱敏、可重复的事件归属和目录 hash，不对应真实租户或生产发布事实。
ACTIVATION_ID = "paca_" + "6" * 32
EVENT_ID = f"evt_{ACTIVATION_ID}"
CATALOG_HASH = "a" * 64


def _occurred_at() -> datetime:
    """返回不晚于当前时间的规范 UTC 秒，避免未来时间门禁造成时钟相关测试抖动。"""

    return (datetime.now(UTC) - timedelta(seconds=1)).replace(microsecond=0)


def _payload(occurred_at: datetime) -> dict[str, object]:
    """构造只含五个公开目录切换字段的合法 payload。"""

    return {
        "catalog_version": "adapter-spec-v2",
        "catalog_hash": CATALOG_HASH,
        "previous_catalog_version": "adapter-spec-v1",
        "spec_count": 10,
        "activated_at": occurred_at.isoformat().replace("+00:00", "Z"),
    }


def _build(payload: dict[str, object], *, occurred_at: datetime, **overrides: object):
    """用固定发布身份和 activation 归属构造事件，并允许测试只替换一个 envelope 字段。"""

    values: dict[str, object] = {
        "tenant_id": "tenant_prd063",
        "event_type": "adapter.spec_updated",
        "payload": payload,
        "actor_id": "adapter-spec-publisher",
        "object_id": ACTIVATION_ID,
        "trace_id": "trc_prd063_spec_update",
        "source_request_id": "req_prd063_spec_update",
        "event_id": EVENT_ID,
        "timestamp": occurred_at,
    }
    values.update(overrides)
    return build_webhook_event(**values)


def test_prd063_error_registry_has_exact_http_and_retry_contracts() -> None:
    """错误合同：管理面校验为 4xx，目录或 Gateway 制品不可用为可追踪 503。"""

    expected = {
        "provider.adapter_spec_missing": (422, False),
        "provider.auth_not_supported": (422, False),
        "adapter.spec_catalog_unavailable": (503, True),
        "adapter.spec_catalog_invalid": (503, True),
        "adapter.spec_filter_invalid": (422, False),
        "adapter.spec_tenant_not_found": (404, False),
        "adapter.spec_activation_conflict": (409, False),
        "adapter.spec_activation_invalid": (422, False),
        "gateway.provider_adapter_spec_missing": (503, True),
        "gateway.provider_adapter_spec_invalid": (503, True),
    }
    actual = {
        code: (ERROR_SPECS[code].http_status, ERROR_SPECS[code].retryable)
        for code in expected
    }

    assert actual == expected


def test_adapter_spec_updated_accepts_exact_persistent_safe_facts() -> None:
    """主流程：真实版本切换绑定固定发布 actor、activation、来源链路和同一 UTC 秒。"""

    occurred_at = _occurred_at()
    payload = _payload(occurred_at)
    event = _build(payload, occurred_at=occurred_at)

    assert event.event_id == EVENT_ID
    assert event.event_type == "adapter.spec_updated"
    assert event.object_id == ACTIVATION_ID
    assert event.actor_id == "adapter-spec-publisher"
    assert event.payload == payload
    assert "adapter.spec_updated" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES


@pytest.mark.parametrize(
    "mutation",
    (
        {"endpoint_url": "https://private.example"},
        {"authorization": "Bearer private"},
        {"catalog_version": "adapter-spec-v1"},
        {"catalog_hash": "A" * 64},
        {"catalog_hash": "a" * 63},
        {"spec_count": True},
        {"spec_count": 65},
        {"activated_at": "2026-08-01T00:00:00+00:00"},
    ),
)
def test_adapter_spec_updated_rejects_extra_sensitive_or_invalid_payload(
    mutation: dict[str, object],
) -> None:
    """安全流程：秘密类字段、同版本伪更新、非法 hash/count/time 都整体失败关闭。"""

    occurred_at = _occurred_at()
    payload = _payload(occurred_at)
    payload.update(mutation)

    with pytest.raises(ValueError, match=r"^webhook\.payload_(invalid|sensitive)$"):
        _build(payload, occurred_at=occurred_at)


@pytest.mark.parametrize(
    "overrides",
    (
        {"actor_id": "admin-user"},
        {"object_id": "paca_" + "g" * 32},
        {"event_id": "evt_unbound"},
        {"trace_id": ""},
        {"source_request_id": ""},
        {"tenant_id": "tenant\nother"},
    ),
)
def test_adapter_spec_updated_rejects_unbound_or_unsafe_envelope(
    overrides: dict[str, object],
) -> None:
    """权限与归属：非发布 actor、跨对象 ID 或缺失链路不得形成可签名事件。"""

    occurred_at = _occurred_at()

    with pytest.raises(ValueError, match=r"^webhook\.payload_invalid$"):
        _build(_payload(occurred_at), occurred_at=occurred_at, **overrides)
