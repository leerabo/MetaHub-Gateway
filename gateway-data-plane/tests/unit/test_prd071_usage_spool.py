"""PRD-071 Usage crash spool 条件式 v5 与输出安全状态矩阵测试。"""

from __future__ import annotations

import pytest
from metahub_gateway.usage.ledger import (
    RequestLogContext,
    _deserialize_usage_spool_record,
    _prepare_usage_ledger_intent,
    _serialize_usage_spool_record,
)


def _usage_intent():
    """构造不含 Provider 扩展正文的固定 Usage 入账意图，供版本往返测试复用。"""

    # 测试只复用正式归一入口，避免手工拼装意图时绕过 Usage 字段和敏感数据边界。
    return _prepare_usage_ledger_intent(
        {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "estimated": False,
            "usage_source": "upstream",
        }
    )


def _context(**updates: object) -> RequestLogContext:
    """构造安全最小请求上下文，并允许测试覆盖输出扫描四元组。"""

    values: dict[str, object] = {
        "tenant_id": "tenant_prd071",
        "request_id": "req_prd071_spool",
        "trace_id": "trc_prd071_spool",
    }
    values.update(updates)
    return RequestLogContext(**values)  # type: ignore[arg-type]


def test_spool_without_scan_stays_v4_and_round_trips_as_absent() -> None:
    """兼容主流程：无 scan 的 Embeddings/前置路径继续写 v4。"""

    payload = _serialize_usage_spool_record(_context(), _usage_intent())
    restored, _intent = _deserialize_usage_spool_record(payload)

    assert payload["schema_version"] == 4
    assert "output_safety_scan_id" not in payload["context"]
    assert restored.output_safety_scan_id is None


def test_spool_with_blocked_scan_uses_exact_v5_and_round_trips() -> None:
    """输出阻断主流程：v5 只新增 scan ID、二维终态和账单意图。"""

    context = _context(
        output_safety_scan_id="oss_" + "a" * 32,
        output_safety_scan_status="completed",
        output_safety_decision="blocked",
        billing_disposition="dispute_pending",
    )
    payload = _serialize_usage_spool_record(context, _usage_intent())
    restored, _intent = _deserialize_usage_spool_record(payload)

    assert payload["schema_version"] == 5
    assert {
        name: payload["context"][name]
        for name in (
            "output_safety_scan_id",
            "output_safety_scan_status",
            "output_safety_decision",
            "billing_disposition",
        )
    } == {
        "output_safety_scan_id": "oss_" + "a" * 32,
        "output_safety_scan_status": "completed",
        "output_safety_decision": "blocked",
        "billing_disposition": "dispute_pending",
    }
    assert restored.output_safety_decision == "blocked"


@pytest.mark.parametrize(
    "updates",
    [
        {"output_safety_scan_id": "oss_" + "a" * 32},
        {
            "output_safety_scan_id": "oss_" + "a" * 32,
            "output_safety_scan_status": "pending",
            "output_safety_decision": "none",
            "billing_disposition": "not_required",
        },
        {
            "output_safety_scan_id": "oss_" + "a" * 32,
            "output_safety_scan_status": "completed",
            "output_safety_decision": "clean",
            "billing_disposition": "dispute_pending",
        },
    ],
)
def test_spool_rejects_partial_or_inconsistent_output_safety_context(
    updates: dict[str, object],
) -> None:
    """异常流程：半组、pending 和冲突账单意图都不能落入 crash spool。"""

    with pytest.raises(ValueError, match="finops_spool"):
        _serialize_usage_spool_record(_context(**updates), _usage_intent())


def test_spool_reader_rejects_unknown_v6() -> None:
    """版本权限流程：未来 v6 不得被旧 reader 宽松解释。"""

    payload = _serialize_usage_spool_record(_context(), _usage_intent())
    payload["schema_version"] = 6

    with pytest.raises(ValueError, match="finops_spool_payload_version_invalid"):
        _deserialize_usage_spool_record(payload)
