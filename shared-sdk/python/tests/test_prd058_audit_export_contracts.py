"""PRD-058 审计导出错误目录与 `audit.exported` Webhook 严格合同测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 错误目录固定 HTTP/retryable 语义，防止控制台和 SDK 对同一失败做出相反恢复决策。
AUDIT_EXPORT_ERRORS = {
    "audit_export.filter_invalid": (422, False),
    "audit_export.request_conflict": (409, False),
    "audit_export.too_large": (413, False),
    "audit_export.integrity_failed": (500, False),
    "audit_export.unavailable": (503, True),
}


def _payload(**overrides: object) -> dict[str, object]:
    """构造不含筛选原文、CSV 或操作者信息的固定导出摘要。"""

    value: dict[str, object] = {
        "audit_export_id": "aex_0123456789abcdef0123456789abcdef",
        "status": "completed",
        "format": "csv",
        "schema_version": 1,
        "row_count": 12,
        "content_size_bytes": 4096,
        "content_sha256": "a" * 64,
        "filter_fingerprint": "b" * 64,
    }
    value.update(overrides)
    return value


def _event(payload: dict[str, object] | None = None, **overrides: object):
    """用完整持久 source 身份构造审计导出事件。"""

    arguments: dict[str, object] = {
        "tenant_id": "tenant-prd058",
        "event_type": "audit.exported",
        "payload": payload or _payload(),
        "actor_id": "user-prd058-admin",
        "object_id": "aex_0123456789abcdef0123456789abcdef",
        "trace_id": "trace-prd058-export",
        "source_request_id": "request-prd058-export",
        "event_id": "aexevt_0123456789abcdef0123456789abcdef",
        "timestamp": datetime(2026, 7, 31, 1, 2, 3, tzinfo=UTC),
    }
    arguments.update(overrides)
    return build_webhook_event(**arguments)


def test_audit_export_errors_have_stable_status_retry_and_bilingual_labels() -> None:
    """错误目录必须区分输入、冲突、规模、完整性和暂时不可用。"""

    for code, (status, retryable) in AUDIT_EXPORT_ERRORS.items():
        spec = ERROR_SPECS[code]
        assert spec.http_status == status
        assert spec.retryable is retryable
        assert error_label(code, "zh-CN") != code
        assert error_label(code, "en-US") != code


def test_audit_exported_event_accepts_only_persistent_safe_contract() -> None:
    """主流程事件必须可订阅并绑定 aexevt、AuditExport、actor 和 trace。"""

    assert "audit.exported" in WEBHOOK_EVENT_TYPES
    assert "audit.exported" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES

    event = _event()

    assert event.event_id.startswith("aexevt_")
    assert event.object_id == event.payload["audit_export_id"]
    assert set(event.payload) == set(_payload())


@pytest.mark.parametrize(
    "payload",
    [
        _payload(filters_json="{}"),
        _payload(csv="sensitive"),
        _payload(audit_export_id="aex_invalid"),
        _payload(status="pending"),
        _payload(format="json"),
        _payload(schema_version=True),
        _payload(row_count=-1),
        _payload(row_count=10_001),
        _payload(content_size_bytes=0),
        _payload(content_size_bytes=16 * 1024 * 1024 + 1),
        _payload(content_sha256="A" * 64),
        _payload(filter_fingerprint="b" * 63),
    ],
)
def test_audit_exported_event_rejects_invalid_or_sensitive_payload(
    payload: dict[str, object],
) -> None:
    """异常 payload 不得扩展筛选、正文或类型混淆字段。"""

    with pytest.raises(ValueError, match="webhook.payload_(invalid|sensitive)"):
        _event(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "evt_0123456789abcdef0123456789abcdef"},
        {"tenant_id": ""},
        {"actor_id": None},
        {"object_id": "aex_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        {"trace_id": "bad trace"},
        {"source_request_id": "bad\nrequest"},
        {"timestamp": datetime(2026, 7, 31, 1, 2, 3)},
    ],
)
def test_audit_exported_event_rejects_unbound_envelope(overrides: dict[str, object]) -> None:
    """租户、actor、对象、链路、请求或时间不可信时必须失败关闭。"""

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _event(**overrides)
