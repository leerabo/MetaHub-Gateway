"""PRD-055 Trace 高级查询事件、配置和统一错误标签合同测试。

测试只使用固定虚构标识、不可逆指纹和聚合计数，不包含真实 filters、Prompt、Response、
Provider Key、Token、SavedFilter 名称或 Webhook Endpoint。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    WebhookEvent,
    build_webhook_event,
)

# 固定事件、对象、时间和指纹让每个失败用例只改变一个合同字段，避免把随机值误当覆盖证据。
EVENT_ID = "lqevt_" + "a" * 32
QUERY_EVENT_ID = "lqe_" + "b" * 32
EXECUTED_AT = "2026-07-28T03:00:00Z"
REQUEST_LOG_ERROR_LABELS = {
    "request_log.not_found": ("调用日志不存在", "Request log not found"),
    "request_log.query_invalid": ("调用日志查询不合法", "Invalid request log query"),
    "request_log.status_invalid": ("调用日志状态不合法", "Invalid request log status"),
    "request_log.sort_invalid": ("调用日志排序不合法", "Invalid request log sort"),
    "request_log.pagination_invalid": ("调用日志分页不合法", "Invalid request log pagination"),
    "request_log.query_unavailable": ("调用日志查询暂不可用", "Request log query unavailable"),
    "request_log.query_audit_unavailable": (
        "调用日志查询审计暂不可用",
        "Request log query audit unavailable",
    ),
    "request_log.query_event_unavailable": (
        "调用日志查询事件暂不可投递",
        "Request log query event unavailable",
    ),
}
SAVED_FILTER_ERROR_LABELS = {
    "saved_filter.not_found": ("保存筛选不存在", "Saved filter not found"),
    "saved_filter.invalid": ("保存筛选不合法", "Invalid saved filter"),
    "saved_filter.name_conflict": ("保存筛选名称冲突", "Saved filter name conflict"),
    "saved_filter.limit_exceeded": ("保存筛选数量已达上限", "Saved filter limit reached"),
    "saved_filter.version_conflict": ("保存筛选版本冲突", "Saved filter version conflict"),
    "saved_filter.unavailable": ("保存筛选暂不可用", "Saved filters unavailable"),
}


def _payload() -> dict[str, object]:
    """构造带阶段筛选且计数关系自洽的最小披露查询事件 payload。"""

    return {
        "schema_version": 1,
        "query_event_id": QUERY_EVENT_ID,
        "filter_version": 1,
        "filter_keys": ["model", "status", "terminal_stage"],
        "filter_fingerprint": "c" * 64,
        "saved_filter_id": "sf_" + "d" * 32,
        "sort_by": "created_at",
        "sort_direction": "desc",
        "limit": 20,
        "offset": 20,
        "matched_count": 41,
        "returned_count": 20,
        "has_more": True,
        "query_duration_ms": 7,
        "executed_at": EXECUTED_AT,
    }


def _build(payload: dict[str, object], **overrides: object) -> WebhookEvent:
    """构造稳定查询事件，并允许单字段覆盖验证共享层失败关闭。"""

    arguments = {
        "tenant_id": "tenant_prd055",
        "event_type": "log.query_executed",
        "payload": payload,
        "actor_id": "user_prd055_developer",
        "object_id": QUERY_EVENT_ID,
        "trace_id": "trace_control_prd055",
        "event_id": EVENT_ID,
        "timestamp": datetime(2026, 7, 28, 3, tzinfo=UTC),
    }
    arguments.update(overrides)
    return build_webhook_event(**arguments)


def test_prd055_query_event_accepts_terminal_stage_and_frozen_envelope() -> None:
    """阶段字段、稳定身份和精确聚合计数应生成可订阅、可重放事件。"""

    event = _build(_payload())

    assert "log.query_executed" in WEBHOOK_EVENT_TYPES
    assert "log.query_executed" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert event.event_id == EVENT_ID
    assert event.object_id == QUERY_EVENT_ID
    assert event.actor_id == "user_prd055_developer"
    assert event.trace_id == "trace_control_prd055"
    assert event.timestamp == EXECUTED_AT
    assert event.payload == _payload()


@pytest.mark.parametrize(
    ("updates"),
    [
        {"filter_keys": ["status", "unknown"]},
        {"filter_keys": ["terminal_stage", "status"]},
        {"filter_keys": ["status", "status"]},
        {"has_more": False},
        {"offset": 40, "returned_count": 2, "has_more": False},
        {"returned_count": 21},
        {"returned_count": True},
    ],
)
def test_prd055_query_event_rejects_stage_or_pagination_drift(
    updates: dict[str, object],
) -> None:
    """字段名顺序、页大小和 has_more 推导任一漂移都必须拒绝签名。"""

    payload = _payload()
    payload.update(updates)
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "evt_" + "a" * 32},
        {"actor_id": None},
        {"object_id": "lqe_" + "e" * 32},
        {"trace_id": "trace with space"},
        {"timestamp": datetime(2026, 7, 28, 3, 0, 1, tzinfo=UTC)},
    ],
)
def test_prd055_query_event_rejects_unbound_envelope(
    overrides: dict[str, object],
) -> None:
    """信封必须绑定持久 event/query ID、可信 actor、operation trace 与执行时间。"""

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(_payload(), **overrides)


def test_prd055_error_codes_have_chinese_and_english_labels() -> None:
    """PRD-055 使用的公开错误必须具备稳定 owner、HTTP 语义和中英文短标签。"""

    expected = REQUEST_LOG_ERROR_LABELS | SAVED_FILTER_ERROR_LABELS
    actual = {
        code
        for code in ERROR_SPECS
        if code.startswith("request_log.") or code.startswith("saved_filter.")
    }
    assert actual == expected.keys()
    for code, (zh_label, en_label) in expected.items():
        spec = ERROR_SPECS[code]
        expected_owner = "request_log" if code.startswith("request_log.") else "saved_filter"
        assert spec.owner == expected_owner
        assert error_label(code, "zh-CN") == zh_label
        assert error_label(code, "en-US") == en_label


def test_prd055_query_event_config_is_independent_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """独立部署开关应严格解析，轮询间隔必须在 1..60 秒内。"""

    monkeypatch.setenv("METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED", "false")
    monkeypatch.setenv("METAHUB_REQUEST_LOG_QUERY_EVENT_POLL_INTERVAL_SECONDS", "3")
    config = load_app_config("control-plane-api")
    assert config.request_log_query_event_enabled is False
    assert config.request_log_query_event_poll_interval_seconds == 3
    assert config.features["request_log_query_event_enabled"] is False

    monkeypatch.setenv("METAHUB_REQUEST_LOG_QUERY_EVENT_POLL_INTERVAL_SECONDS", "0")
    with pytest.raises(ConfigError) as failure:
        load_app_config("control-plane-api")
    assert failure.value.code == "infra.config_invalid"
