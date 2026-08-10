"""验证 Response Cache candidate 的 Output Safety 不计费阻断合同。

该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from metahub_gateway.output_safety.models import (
    OutputSafetyStoreError,
    OutputSafetyTerminalState,
    PreparedOutputSafetyScan,
)
from metahub_gateway.output_safety.service import _validate_terminal
from metahub_shared.output_safety import OutputRule, OutputRuleSnapshot, OutputScanDecision


def _blocked_decision() -> OutputScanDecision:
    """构造不含命中文本的固定阻断摘要。"""

    return OutputScanDecision(
        decision="blocked",
        action="block",
        dlp_rule_id="dlp_prd076_cache_block",
        rule_type="keyword",
        match_count=1,
        checkpoint_count=1,
        scanned_token_count=5,
    )


def _scan(*, stream_session_id: str | None = None) -> PreparedOutputSafetyScan:
    """冻结单租户规则 owner；pattern 只驻留内存且不会进入断言输出。"""

    rule = OutputRule(
        dlp_rule_id="dlp_prd076_cache_block",
        rule_type="keyword",
        action="block",
        priority=1,
        pattern="synthetic-block-marker",
    )
    snapshot = OutputRuleSnapshot(
        tenant_id="tenant-prd076-output-safety",
        ruleset_fingerprint="a" * 64,
        detector_version="output-safety-dlp-v1",
        rules=(rule,),
    )
    return PreparedOutputSafetyScan(
        output_safety_scan_id="oss_" + "b" * 32,
        tenant_id=snapshot.tenant_id,
        request_id="req-prd076-output-safety",
        trace_id="trc-prd076-output-safety",
        stream_session_id=stream_session_id,
        scan_mode="non_stream_full",
        scan_status="pending",
        interval_tokens=None,
        window_tokens=None,
        snapshot=snapshot,
    )


def test_cache_candidate_block_is_non_billable_and_unexposed() -> None:
    """未交付且未访问 Provider 的 cache candidate 不能进入成本争议。"""

    scan = _scan()
    terminal = OutputSafetyTerminalState.from_response_cache_decision(
        _blocked_decision(),
        observed_output_token_count=5,
    )

    _validate_terminal(scan, terminal)
    assert terminal.decision == "blocked"
    assert terminal.exposure_state == "none"
    assert terminal.emitted_token_count == 0
    assert terminal.emitted_sse_event_count == 0
    assert terminal.emitted_bytes == 0
    assert terminal.billing_disposition == "not_billable_response_cache_candidate"


def test_provider_block_and_cache_block_keep_distinct_billing_dispositions() -> None:
    """普通 Provider 输出阻断必须继续标记 dispute_pending。"""

    provider_terminal = OutputSafetyTerminalState.from_scan_decision(
        _blocked_decision(),
        observed_output_token_count=5,
        emitted_token_count=0,
        emitted_sse_event_count=0,
        emitted_bytes=0,
        exposure_state="none",
    )

    _validate_terminal(_scan(), provider_terminal)
    assert provider_terminal.billing_disposition == "dispute_pending"


def test_cache_only_disposition_rejects_stream_owner_or_partial_exposure() -> None:
    """cache-only 不计费枚举不能被流式或已有暴露的响应借用。"""

    terminal = OutputSafetyTerminalState.from_response_cache_decision(
        _blocked_decision(),
        observed_output_token_count=5,
    )
    with pytest.raises(OutputSafetyStoreError):
        _validate_terminal(_scan(stream_session_id="strs_prd076_invalid"), terminal)
    with pytest.raises(OutputSafetyStoreError):
        _validate_terminal(_scan(), replace(terminal, exposure_state="partial"))
