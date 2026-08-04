"""PRD-054 请求解释查看事件与统一错误合同测试。

测试只使用固定虚构 ID、状态枚举和区块名称，不包含 Prompt、Response、Provider Key、
Token、Webhook 地址或任何真实租户数据。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    WebhookEvent,
    build_webhook_event,
)


# 稳定事件/对象/时间、区块顺序和错误表共同冻结最小披露 Webhook 合同。
EVENT_ID = "rxevt_" + "a" * 32
EXPLANATION_ID = "rxe_" + "b" * 32
OTHER_EXPLANATION_ID = "rxe_" + "c" * 32
VIEWED_AT = "2026-07-27T12:00:00Z"
SECTION_ORDER = (
    "authentication",
    "rate_limit",
    "budget",
    "dlp",
    "policy",
    "routing",
    "keypool",
    "provider_response",
    "usage_and_finops",
    "logs",
    "webhooks",
)
ERROR_CONTRACTS = {
    "request_explanation.not_found": (404, False, "请求解释不存在", "Request explanation not found"),
    "request_explanation.request_id_invalid": (
        422,
        False,
        "源请求标识不合法",
        "Invalid source request id",
    ),
    "request_explanation.request_invalid": (422, False, "解释请求不合法", "Invalid explanation request"),
    "request_explanation.unavailable": (503, True, "请求解释暂不可用", "Request explanation unavailable"),
    "request_explanation.audit_unavailable": (
        503,
        True,
        "解释访问审计暂不可用",
        "Explanation audit unavailable",
    ),
    "request_explanation.integrity_failed": (
        500,
        False,
        "解释证据完整性校验失败",
        "Explanation integrity failed",
    ),
    "request_explanation.event_unavailable": (
        503,
        True,
        "解释查看事件暂不可投递",
        "Explanation event unavailable",
    ),
}


def _payload(*, status: str = "complete") -> dict[str, object]:
    """构造精确的解释查看安全摘要，partial 时显式列出缺失证据。"""

    missing = [] if status == "complete" else ["webhooks"]
    return {
        "schema_version": 1,
        "request_explanation_id": EXPLANATION_ID,
        "source_request_id": "req_gateway_prd054",
        "source_trace_id": "trace_gateway_prd054",
        "source_status": "success",
        "explanation_status": status,
        "missing_sections": missing,
        "truncated_sections": [],
        "evidence_version": "request_explanation_v1",
        "viewed_at": VIEWED_AT,
    }


def _build(payload: dict[str, object], **overrides: object) -> WebhookEvent:
    """构造稳定查看事件，并允许单字段覆盖验证失败关闭路径。"""

    arguments = {
        "tenant_id": "tenant_prd054",
        "event_type": "request.explanation_viewed",
        "payload": payload,
        "actor_id": "user_prd054_developer",
        "object_id": EXPLANATION_ID,
        "trace_id": "trace_control_prd054",
        "event_id": EVENT_ID,
        "timestamp": datetime(2026, 7, 27, 12, tzinfo=UTC),
    }
    arguments.update(overrides)
    return build_webhook_event(**arguments)


def test_request_explanation_errors_have_stable_http_retry_and_labels() -> None:
    """公开错误必须保持 HTTP、重试语义、owner 与中英文短标签一致。"""

    actual = {code for code in ERROR_SPECS if code.startswith("request_explanation.")}
    assert actual == ERROR_CONTRACTS.keys()
    for code, (status, retryable, zh_label, en_label) in ERROR_CONTRACTS.items():
        spec = ERROR_SPECS[code]
        assert spec.http_status == status
        assert spec.retryable is retryable
        assert spec.owner == "request_explanation"
        assert error_label(code, "zh-CN") == zh_label
        assert error_label(code, "en-US") == en_label


@pytest.mark.parametrize("status", ["complete", "partial"])
def test_request_explanation_viewed_accepts_frozen_contract(status: str) -> None:
    """完整与部分解释都应生成可订阅、可重放的稳定安全事件。"""

    event = _build(_payload(status=status))

    assert "request.explanation_viewed" in WEBHOOK_EVENT_TYPES
    assert "request.explanation_viewed" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert event.event_id == EVENT_ID
    assert event.event_type == "request.explanation_viewed"
    assert event.timestamp == VIEWED_AT
    assert event.tenant_id == "tenant_prd054"
    assert event.actor_id == "user_prd054_developer"
    assert event.object_id == EXPLANATION_ID
    assert event.trace_id == "trace_control_prd054"
    assert event.payload == _payload(status=status)


@pytest.mark.parametrize("field", sorted(_payload()))
def test_request_explanation_viewed_rejects_each_missing_field(field: str) -> None:
    """精确 payload 任一字段缺失都必须失败，订阅方不能猜默认值。"""

    payload = _payload()
    payload.pop(field)
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("request_explanation_id", "rxe_" + "A" * 32),
        ("request_explanation_id", "rxe_" + "a" * 31),
        ("source_request_id", ""),
        ("source_request_id", "request with space"),
        ("source_trace_id", "x" * 129),
        ("source_status", "pending"),
        ("explanation_status", "unknown"),
        ("evidence_version", "request_explanation_v2"),
        ("viewed_at", "2026-07-27T12:00:00+00:00"),
        ("viewed_at", "2026-07-27T12:00:00.001Z"),
    ],
)
def test_request_explanation_viewed_rejects_invalid_identity_enum_or_time(
    field: str,
    value: object,
) -> None:
    """ID、强枚举、版本与秒级 UTC 时间必须保持 canonical 合同。"""

    payload = _payload()
    payload[field] = value
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


@pytest.mark.parametrize(
    ("missing", "truncated", "status"),
    [
        (["unknown"], [], "partial"),
        (["webhooks", "authentication"], [], "partial"),
        (["webhooks", "webhooks"], [], "partial"),
        (["webhooks"], ["webhooks"], "partial"),
        ([], [], "partial"),
        (["webhooks"], [], "complete"),
        ([], ["routing"], "complete"),
        ("webhooks", [], "partial"),
    ],
)
def test_request_explanation_viewed_rejects_invalid_section_sets(
    missing: object,
    truncated: object,
    status: str,
) -> None:
    """区块数组必须有序、唯一、互斥，且与完整度结论一致。"""

    payload = _payload(status=status)
    payload["missing_sections"] = missing
    payload["truncated_sections"] = truncated
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": None},
        {"event_id": "evt_" + "a" * 32},
        {"event_id": "rxevt_" + "A" * 32},
        {"actor_id": None},
        {"actor_id": "user with space"},
        {"object_id": OTHER_EXPLANATION_ID},
        {"trace_id": None},
        {"trace_id": "trace with space"},
        {"timestamp": datetime(2026, 7, 27, 12, 0, 1, tzinfo=UTC)},
    ],
)
def test_request_explanation_viewed_rejects_unbound_envelope(
    overrides: dict[str, object],
) -> None:
    """验证查看事件信封绑定稳定事件、对象、可信用户、操作链路与查看时间。

    任一覆盖值漂移都必须由共享构造器失败关闭，不能生成新的随机审计身份。
    """

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(_payload(), **overrides)


@pytest.mark.parametrize(
    "sensitive_key",
    ["Prompt", "response", "access-token", "signing_secret", "provider_api_key"],
)
def test_request_explanation_viewed_recursively_rejects_sensitive_keys(
    sensitive_key: str,
) -> None:
    """任意嵌套正文、Token、Secret 或 API Key 字段都必须整体拒绝。"""

    payload = _payload()
    payload["future"] = [{"nested": {sensitive_key: "redacted-test-value"}}]
    with pytest.raises(ValueError, match="webhook.payload_sensitive"):
        _build(payload)


def test_request_explanation_viewed_rejects_unreviewed_extra_field() -> None:
    """普通额外字段也不能绕过精确 payload 白名单。"""

    payload = _payload()
    payload["future_field"] = "not-reviewed"
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


def test_section_contract_contains_rate_limit_and_all_eleven_detail_blocks() -> None:
    """共享测试冻结 10 阶段所需的 11 个详情区块，防止再次漏掉限流证据。"""

    assert len(SECTION_ORDER) == 11
    assert SECTION_ORDER[1] == "rate_limit"
    assert SECTION_ORDER[-2:] == ("logs", "webhooks")
