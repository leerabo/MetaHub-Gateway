"""PRD-064 Adapter 合同错误注册表与 Webhook 精确合同测试。

测试只构造固定 run、hash、计数和错误码，不访问网络、数据库或真实 Provider Endpoint，也不
使用 Provider Key、Token、Prompt、Response、Authorization 或上游错误正文。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.webhooks import (
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)


# 固定事件身份和目录哈希用于精确合同负例，测试不得替换为真实租户或运行数据。
RUN_ID = "acr_" + "6" * 32
EVENT_ID = f"evt_{RUN_ID}"
CATALOG_HASH = "a" * 64


def _occurred_at() -> datetime:
    """返回不晚于当前时间的规范 UTC 秒，避免未来时间门禁造成测试抖动。"""

    return (datetime.now(UTC) - timedelta(seconds=1)).replace(microsecond=0)


def _payload(occurred_at: datetime) -> dict[str, object]:
    """构造只含九个公开失败摘要字段的合法 payload。"""

    return {
        "run_id": RUN_ID,
        "catalog_version": "adapter-spec-v1",
        "catalog_hash": CATALOG_HASH,
        "suite_kind": "merge_gate",
        "mode": "mock",
        "total_cases": 92,
        "failed_cases": 1,
        "failure_codes": ["adapter.contract_usage_failed"],
        "completed_at": occurred_at.isoformat().replace("+00:00", "Z"),
    }


def _build(payload: dict[str, object], *, occurred_at: datetime, **overrides: object):
    """用固定 runner 身份构造事件，并允许负例只替换一个 envelope 字段。"""

    values: dict[str, object] = {
        "tenant_id": "tenant_prd064",
        "event_type": "adapter.contract_failed",
        "payload": payload,
        "actor_id": "adapter-contract-runner",
        "object_id": RUN_ID,
        "trace_id": "trc_prd064_contract_failed",
        "source_request_id": "req_prd064_contract_failed",
        "event_id": EVENT_ID,
        "timestamp": occurred_at,
    }
    values.update(overrides)
    return build_webhook_event(**values)


def test_prd064_error_registry_has_exact_http_and_retry_contracts() -> None:
    """错误合同：输入/冲突/租户不可见/存储不可用分别映射固定 HTTP。"""

    expected = {
        "adapter.contract_report_invalid": (422, False),
        "adapter.contract_catalog_mismatch": (409, False),
        "adapter.contract_run_conflict": (409, False),
        "adapter.contract_run_not_found": (404, False),
        "adapter.contract_storage_unavailable": (503, True),
    }
    assert {
        code: (ERROR_SPECS[code].http_status, ERROR_SPECS[code].retryable)
        for code in expected
    } == expected


def test_adapter_contract_failed_accepts_exact_persistent_safe_facts() -> None:
    """主流程：失败 run 绑定固定 actor、来源链路、目录和同一完成秒。"""

    occurred_at = _occurred_at()
    payload = _payload(occurred_at)
    event = _build(payload, occurred_at=occurred_at)

    assert event.event_id == EVENT_ID
    assert event.event_type == "adapter.contract_failed"
    assert event.object_id == RUN_ID
    assert event.actor_id == "adapter-contract-runner"
    assert event.payload == payload
    assert "adapter.contract_failed" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES


@pytest.mark.parametrize(
    "mutation",
    (
        {"endpoint_url": "https://private.example"},
        {"authorization": "Bearer private"},
        {"run_id": "acr_" + "G" * 32},
        {"catalog_hash": "A" * 64},
        {"suite_kind": "merge_gate", "mode": "gateway_probe"},
        {"total_cases": True},
        {"failed_cases": 0},
        {"failure_codes": []},
        {"failure_codes": ["adapter.contract_usage_failed", "adapter.contract_usage_failed"]},
        {"failure_codes": ["private.provider.error"]},
        {"completed_at": "2026-08-01T00:00:00+00:00"},
    ),
)
def test_adapter_contract_failed_rejects_extra_sensitive_or_invalid_payload(
    mutation: dict[str, object],
) -> None:
    """安全流程：秘密字段、身份/计数/失败码/时间漂移都在签名前失败关闭。"""

    occurred_at = _occurred_at()
    payload = _payload(occurred_at)
    payload.update(mutation)

    with pytest.raises(ValueError, match=r"^webhook\.payload_(invalid|sensitive)$"):
        _build(payload, occurred_at=occurred_at)


@pytest.mark.parametrize(
    "overrides",
    (
        {"actor_id": "admin-user"},
        {"object_id": "acr_" + "7" * 32},
        {"event_id": "evt_unbound"},
        {"trace_id": ""},
        {"source_request_id": ""},
        {"tenant_id": "tenant\nother"},
    ),
)
def test_adapter_contract_failed_rejects_unbound_or_unsafe_envelope(
    overrides: dict[str, object],
) -> None:
    """权限与归属：非 runner actor、跨对象或缺失链路不得形成可签名事件。"""

    occurred_at = _occurred_at()
    with pytest.raises(ValueError, match=r"^webhook\.payload_invalid$"):
        _build(_payload(occurred_at), occurred_at=occurred_at, **overrides)
