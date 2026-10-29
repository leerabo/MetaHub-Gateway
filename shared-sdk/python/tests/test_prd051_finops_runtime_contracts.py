"""PRD-051 Shared 配置、错误码与账本 Webhook 的跨服务合同测试。

测试数据只使用随机业务 ID 和本地临时路径，不包含 Provider Key、Token、Prompt、
Response 或真实 Endpoint；账本事件字段使用固定安全摘要。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.webhooks import build_webhook_event


def test_finops_spool_config_is_bounded_and_masked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：加载有界 spool 配置，且宿主机路径不会进入摘要、repr 或 model_dump。"""

    spool_root = tmp_path / "private-finops-spool"
    monkeypatch.setenv("METAHUB_FINOPS_USAGE_SPOOL_ROOT", str(spool_root))
    monkeypatch.setenv("METAHUB_FINOPS_USAGE_SPOOL_POLL_INTERVAL_SECONDS", "3")
    monkeypatch.setenv("METAHUB_FINOPS_USAGE_SPOOL_CLAIM_LEASE_SECONDS", "120")

    config = load_app_config("gateway-data-plane")

    assert Path(config.finops_usage_spool_root) == spool_root.resolve()
    assert config.finops_usage_spool_poll_interval_seconds == 3
    assert config.finops_usage_spool_claim_lease_seconds == 120
    serialized = str(config.model_dump()) + repr(config)
    assert str(spool_root) not in serialized
    summary = next(
        item
        for item in config.config_items
        if item.key == "METAHUB_FINOPS_USAGE_SPOOL_ROOT"
    )
    assert summary.status == "masked"
    assert summary.value_preview == "***MASKED***"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("METAHUB_FINOPS_USAGE_SPOOL_ROOT", r"\\server\shared-spool"),
        ("METAHUB_FINOPS_USAGE_SPOOL_POLL_INTERVAL_SECONDS", "0"),
        ("METAHUB_FINOPS_USAGE_SPOOL_POLL_INTERVAL_SECONDS", "61"),
        ("METAHUB_FINOPS_USAGE_SPOOL_CLAIM_LEASE_SECONDS", "9"),
        ("METAHUB_FINOPS_USAGE_SPOOL_CLAIM_LEASE_SECONDS", "3601"),
    ],
)
def test_finops_spool_config_rejects_network_or_unbounded_values(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    """异常流程：网络目录和越界轮询/租约必须在启动时以统一配置码失败关闭。"""

    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError) as raised:
        load_app_config("gateway-data-plane")
    assert raised.value.code == "infra.config_invalid"
    assert key in raised.value.message
    assert value not in raised.value.message


def test_usage_record_unavailable_has_retryable_503_and_bilingual_labels() -> None:
    """错误合同：账本与 spool 双失败只暴露可重试 503，并具备中英文标签。"""

    spec = ERROR_SPECS["finops.usage_record_unavailable"]
    assert spec.http_status == 503
    assert spec.retryable is True
    assert spec.owner == "finops"
    assert error_label("finops.usage_record_unavailable", "zh-CN") == "用量账本记录暂不可用"
    assert error_label("finops.usage_record_unavailable", "en-US") == "Usage ledger recording unavailable"


def _ledger_payload(*, entry_type: str) -> dict[str, object]:
    """构造不含正文和调整原因的固定账本事件安全字段。"""

    is_reversal = entry_type == "reversal"
    return {
        "ledger_entry_id": "fle_" + "a" * 32,
        "entry_type": entry_type,
        "direction": "credit" if is_reversal else "debit",
        "amount": "1.000000",
        "currency": "CNY",
        "normalized_cny": "1.000000",
        "cny_normalization_status": "normalized",
        "quota_type": "paid",
        "quota_allocation_class": "not_applicable" if is_reversal else "unallocated",
        "quota_allocation_count": 0,
        "source_request_id": None if is_reversal else "req_prd051_usage",
        "price_snapshot_id": None,
        "price_schema_version": None,
        "price_snapshot_source": None,
        "price_evidence_status": "not_applicable",
        "reversal_of_entry_id": "fle_" + "b" * 32 if is_reversal else None,
    }


def test_reversal_may_inherit_not_applicable_price_evidence() -> None:
    """兼容流程：人工分录冲正可继承 not_applicable，事件仍绑定可信 actor 和对象。"""

    payload = _ledger_payload(entry_type="reversal")
    event = build_webhook_event(
        tenant_id="tenant-prd051",
        event_type="ledger.entry_created",
        event_id="flevt_" + "c" * 32,
        actor_id="user-prd051-admin",
        object_id=str(payload["ledger_entry_id"]),
        trace_id="trace-prd051-reversal",
        payload=payload,
    )
    assert event.payload["price_evidence_status"] == "not_applicable"


def test_usage_charge_rejects_not_applicable_price_evidence() -> None:
    """异常流程：Usage 分录必须给出 priced/unpriced 等真实证据，不能标记为不适用。"""

    payload = _ledger_payload(entry_type="usage_charge")
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant-prd051",
            event_type="ledger.entry_created",
            event_id="flevt_" + "d" * 32,
            actor_id=None,
            object_id=str(payload["ledger_entry_id"]),
            trace_id="trace-prd051-usage",
            payload=payload,
        )
