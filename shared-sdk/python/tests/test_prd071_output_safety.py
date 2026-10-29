"""PRD-071 输出安全纯函数合同测试。

测试正文只使用固定虚构短语，断言异常与 repr 不会回显这些内容。测试不访问网络或开发数据库。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from metahub_shared.output_safety import (
    BoundedSseEventFramer,
    OutputFragment,
    OutputRule,
    OutputRuleSnapshot,
    OutputSafetyError,
    SlidingOutputScanner,
    scan_full_output,
)
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)


def _snapshot(*rules: OutputRule) -> OutputRuleSnapshot:
    """构造当前租户的固定规则快照，测试不把 pattern 写入持久层。"""

    return OutputRuleSnapshot(
        tenant_id="tenant_prd071",
        ruleset_fingerprint="a" * 64,
        detector_version="output-safety-dlp-v1",
        rules=rules,
    )


def _keyword(rule_id: str, pattern: str, action: str = "block") -> OutputRule:
    """构造固定关键词规则，并验证规则对象的 repr 始终隐藏敏感匹配正文。"""

    return OutputRule(
        dlp_rule_id=rule_id,
        rule_type="keyword",
        action=action,
        priority=10,
        pattern=pattern,
    )


def test_full_scan_covers_refusal_and_tool_arguments_without_leaking_text() -> None:
    """全文主流程：refusal/tool arguments 命中 block，决策和 repr 不含原文。"""

    secret = "prd071-forbidden-output"
    snapshot = _snapshot(_keyword("dlp_prd071_block", secret))
    payload = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "content": "safe",
                    "refusal": None,
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '{"value":"' + secret + '"}'}}
                    ],
                },
            }
        ]
    }

    decision = scan_full_output(snapshot, payload)

    assert decision.decision == "blocked"
    assert decision.dlp_rule_id == "dlp_prd071_block"
    assert secret not in repr(snapshot)
    assert secret not in repr(decision)


def test_full_scan_prioritizes_any_block_and_counts_distinct_rules() -> None:
    """判定语义：block 优先于更早 log_only，同一规则只计一次。"""

    snapshot = _snapshot(
        _keyword("dlp_log", "review", "log_only"),
        _keyword("dlp_block", "deny", "block"),
    )
    payload = {"choices": [{"message": {"content": "review review deny"}}]}

    decision = scan_full_output(snapshot, payload)

    assert decision.decision == "blocked"
    assert decision.dlp_rule_id == "dlp_block"
    assert decision.match_count == 2


def test_full_scan_rejects_known_field_shape_instead_of_claiming_clean() -> None:
    """异常流程：已知正文结构损坏必须失败关闭。"""

    with pytest.raises(OutputSafetyError, match="output_safety.response_shape_invalid"):
        scan_full_output(_snapshot(), {"choices": [{"message": {"content": {"bad": True}}}]})


def test_full_scan_honors_stricter_deployment_codepoint_limit() -> None:
    """部署收紧全文上限后，超限响应失败关闭且异常不回显正文。"""

    with pytest.raises(OutputSafetyError, match="output_safety.response_too_large"):
        scan_full_output(
            _snapshot(),
            {"choices": [{"message": {"content": "fixed-test-output"}}]},
            max_codepoints=8,
        )


def test_framer_preserves_cross_chunk_crlf_multidata_and_comment_bytes() -> None:
    """分帧主流程：跨 chunk、CRLF、多 data 行和 comment 均保留原始 bytes。"""

    framer = BoundedSseEventFramer()
    first = b": keepalive\r\ndata: {\"choices\": [{\"delta\":\r\n"
    second = b'data: {"content":"safe"}}]}\r\n\r\n'

    assert framer.feed(first) == ()
    frames = framer.feed(second)

    assert len(frames) == 1
    assert frames[0].raw_bytes == first + second
    assert frames[0].fragments[0].text == "safe"
    framer.finish()


def test_framer_returns_multiple_events_and_done_as_separate_release_units() -> None:
    """一个 raw chunk 可产生多个完整 frame，[DONE] 保持独立尾窗门禁。"""

    first = b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
    second = b'data: {"choices":[{"delta":{"refusal":"b"}}]}\n\n'
    done = b"data: [DONE]\n\n"

    frames = BoundedSseEventFramer().feed(first + second + done)

    assert [frame.raw_bytes for frame in frames] == [first, second, done]
    assert [frame.is_done for frame in frames] == [False, False, True]


def test_framer_stops_at_done_before_malformed_event_in_same_chunk() -> None:
    """终止边界：同批 `[DONE]` 后的畸形完整事件不得反向污染已完成帧。"""

    safe = b'data: {"choices":[{"delta":{"content":"safe"}}]}\n\n'
    done = b"data: [DONE]\n\n"
    malformed = b'data: {"choices": invalid}\n\n'
    framer = BoundedSseEventFramer()

    frames = framer.feed(safe + done + malformed)

    assert [frame.raw_bytes for frame in frames] == [safe, done]
    assert [frame.is_done for frame in frames] == [False, True]
    # 协议终止后即使 Provider 继续发送畸形或残缺字节，也应保持静默并允许正常收尾。
    assert framer.feed(b"data: not-json\n") == ()
    framer.finish()


def test_framer_rejects_duplicate_json_keys_without_echoing_payload() -> None:
    """歧义 JSON 失败关闭，异常只返回固定 reason code。"""

    sensitive = "prd071-duplicate-sensitive"
    framer = BoundedSseEventFramer()
    raw = (
        'data: {"choices":[],"choices":[{"delta":{"content":"'
        + sensitive
        + '"}}]}\n\n'
    ).encode()

    with pytest.raises(OutputSafetyError) as captured:
        framer.feed(raw)

    assert captured.value.reason_code == "output_safety.sse_json_duplicate_key"
    assert sensitive not in repr(captured.value)


def test_framer_rejects_incomplete_event_at_eof() -> None:
    """EOF 异常流程：未闭合 data event 不能被拼接标准 error 后透传。"""

    framer = BoundedSseEventFramer()
    assert framer.feed(b'data: {"choices":[]}\n') == ()

    with pytest.raises(OutputSafetyError, match="output_safety.sse_event_incomplete"):
        framer.finish()


def test_sliding_scanner_matches_same_lane_across_events() -> None:
    """滑窗主流程：同一 choice/content lane 跨 event 仍可命中。"""

    scanner = SlidingOutputScanner(
        _snapshot(_keyword("dlp_cross", "forbidden")),
        interval_tokens=8,
        window_tokens=32,
    )
    lane = ("choice:0", "delta.content")

    assert scanner.feed_sse_frame((OutputFragment(lane=lane, text="forbid"),)) is None
    blocked = scanner.feed_sse_frame(
        (OutputFragment(lane=lane, text="den plus padding text for checkpoint"),)
    )

    assert blocked is not None
    assert blocked.decision == "blocked"


def test_sliding_scanner_does_not_join_different_choice_or_tool_lanes() -> None:
    """lane 隔离：不同 choice/tool call 片段不能制造假命中。"""

    scanner = SlidingOutputScanner(
        _snapshot(_keyword("dlp_lane", "forbidden")),
        interval_tokens=8,
        window_tokens=32,
    )
    scanner.feed_sse_frame(
        (OutputFragment(lane=("choice:0", "delta.content"), text="forbid"),)
    )
    scanner.feed_sse_frame(
        (OutputFragment(lane=("choice:1", "delta.content"), text="den padding"),)
    )

    assert scanner.finish().decision == "clean"


def test_sliding_scanner_checks_intermediate_boundary_inside_large_frame() -> None:
    """单 frame 跨多个检查点时必须在早期窗口命中，不能被后续长文本挤出。"""

    scanner = SlidingOutputScanner(
        _snapshot(_keyword("dlp_early", "BLOCK")),
        interval_tokens=8,
        window_tokens=32,
    )

    decision = scanner.feed_sse_frame(
        (OutputFragment(lane=("choice:0", "delta.content"), text="BLOCK" + "x" * 40_000),)
    )

    assert decision is not None
    assert decision.decision == "blocked"
    assert decision.checkpoint_count == 1


def test_sliding_scanner_window_tokens_changes_retained_regex_history() -> None:
    """窗口合同：32 与 1024 Token 必须产生不同历史范围，同时允许固定 512 字符关键词重叠。"""

    long_history_rule = OutputRule(
        dlp_rule_id="dlp_window_history",
        rule_type="regex",
        action="log_only",
        priority=10,
        pattern=r"始中{600}终",
        compiled_regex=re.compile(r"始中{600}终"),
    )
    fragment = OutputFragment(
        lane=("choice:0", "delta.content"),
        text="始" + "中" * 600 + "终",
    )
    short_window = SlidingOutputScanner(
        _snapshot(long_history_rule),
        interval_tokens=8,
        window_tokens=32,
    )
    long_window = SlidingOutputScanner(
        _snapshot(long_history_rule),
        interval_tokens=8,
        window_tokens=1_024,
    )

    short_window.feed_sse_frame((fragment,))
    long_window.feed_sse_frame((fragment,))

    assert short_window.finish().decision == "clean"
    assert long_window.finish().decision == "matched"


def test_sliding_scanner_tail_scan_catches_short_output() -> None:
    """不足首检查点的短输出必须在 `[DONE]`/EOF 尾窗命中。"""

    scanner = SlidingOutputScanner(
        _snapshot(_keyword("dlp_tail", "bad")),
        interval_tokens=8,
        window_tokens=32,
    )
    assert scanner.feed_sse_frame(
        (OutputFragment(lane=("choice:0", "delta.content"), text="bad"),)
    ) is None

    decision = scanner.finish()

    assert decision.decision == "blocked"
    assert decision.checkpoint_count == 1


def test_sliding_scanner_scans_whitespace_tail_without_token_growth() -> None:
    """空白不计 Token 也必须有 code-point 边界且 finish 会执行尾窗检查。"""

    whitespace_rule = OutputRule(
        dlp_rule_id="dlp_space",
        rule_type="regex",
        action="log_only",
        priority=10,
        pattern=r"\s{16}",
        compiled_regex=re.compile(r"\s{16}"),
    )
    scanner = SlidingOutputScanner(
        _snapshot(whitespace_rule),
        interval_tokens=8,
        window_tokens=32,
    )
    scanner.feed_sse_frame(
        (OutputFragment(lane=("choice:0", "delta.content"), text=" " * 100_000),)
    )

    decision = scanner.finish()

    assert decision.decision == "matched"
    assert decision.scanned_token_count == 0


def test_sliding_scanner_blocks_before_single_event_codepoint_eviction() -> None:
    """单 event 淘汰边界：低 Token 命中词不能被大量空白挤出后伪造成 clean。"""

    scanner = SlidingOutputScanner(
        _snapshot(_keyword("dlp_space_evasion_single", "bad")),
        interval_tokens=8,
        window_tokens=32,
    )

    decision = scanner.feed_sse_frame(
        (OutputFragment(lane=("choice:0", "delta.content"), text="bad" + " " * 100_000),)
    )

    assert decision is not None
    assert decision.decision == "blocked"
    assert decision.scanned_token_count == 1


def test_sliding_scanner_blocks_before_multi_event_codepoint_eviction() -> None:
    """多 event 淘汰边界：跨 event 的空白累积也必须在回收命中前触发扫描。"""

    scanner = SlidingOutputScanner(
        _snapshot(_keyword("dlp_space_evasion_events", "bad")),
        interval_tokens=8,
        window_tokens=32,
    )
    lane = ("choice:0", "delta.content")
    assert scanner.feed_sse_frame((OutputFragment(lane=lane, text="bad"),)) is None

    blocked = None
    for _index in range(20):
        blocked = scanner.feed_sse_frame((OutputFragment(lane=lane, text=" " * 500),))
        if blocked is not None:
            break

    assert blocked is not None
    assert blocked.decision == "blocked"


def test_sliding_scanner_scans_old_lane_before_total_bound_eviction() -> None:
    """多 lane 总量边界：最久未触达 lane 在回收前仍须参与最后一次完整扫描。"""

    scanner = SlidingOutputScanner(
        _snapshot(_keyword("dlp_space_evasion_lanes", "bad")),
        interval_tokens=8,
        window_tokens=32,
    )
    first_lane = ("choice:0", "delta.content")
    assert scanner.feed_sse_frame(
        (OutputFragment(lane=first_lane, text="bad" + " " * 600),)
    ) is None

    blocked = None
    for index in range(1, 120):
        blocked = scanner.feed_sse_frame(
            (OutputFragment(lane=(f"choice:{index}", "delta.content"), text=" " * 600),)
        )
        if blocked is not None:
            break

    assert blocked is not None
    assert blocked.decision == "blocked"


def test_output_safety_runtime_config_is_bounded_and_cross_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署配置主流程与异常流程：合法窗口可加载，小于 interval 时启动失败关闭。"""

    monkeypatch.setenv("METAHUB_OUTPUT_SAFETY_INTERVAL_TOKENS", "64")
    monkeypatch.setenv("METAHUB_OUTPUT_SAFETY_WINDOW_TOKENS", "128")
    monkeypatch.setenv("METAHUB_OUTPUT_SAFETY_MAX_FULL_TEXT_CODE_POINTS", "131072")

    config = load_app_config("gateway-data-plane")

    assert config.output_safety_interval_tokens == 64
    assert config.output_safety_window_tokens == 128
    assert config.output_safety_max_full_text_code_points == 131_072

    monkeypatch.setenv("METAHUB_OUTPUT_SAFETY_WINDOW_TOKENS", "32")
    with pytest.raises(ConfigError, match="must be greater than or equal"):
        load_app_config("gateway-data-plane")


def test_output_blocked_webhook_accepts_exact_safe_contract() -> None:
    """Webhook 主流程：稳定 source event 与 blocked scan 信封逐字绑定。"""

    payload = _blocked_webhook_payload()
    event = build_webhook_event(
        tenant_id="tenant_prd071",
        event_type="security.output_blocked",
        payload=payload,
        actor_id=None,
        object_id=payload["output_safety_scan_id"],
        trace_id="trc_prd071",
        source_request_id=payload["request_id"],
        event_id="osevt_" + "c" * 32,
        timestamp=datetime(2026, 8, 4, 5, 0, tzinfo=UTC),
    )

    assert "security.output_blocked" in WEBHOOK_EVENT_TYPES
    assert "security.output_blocked" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert event.payload == payload


@pytest.mark.parametrize(
    "updates",
    [
        {"response": "forbidden-output"},
        {"reversal_eligible": True},
        {"decision": "matched"},
        {"exposure_state": "none", "emitted_token_count": 1},
        {"checkpoint_count": True},
        {"detector_version": "output-safety-dlp-v2"},
    ],
)
def test_output_blocked_webhook_rejects_sensitive_or_inconsistent_drift(
    updates: dict[str, object],
) -> None:
    """Webhook 异常流程：正文、未知财务字段和自相矛盾状态在签名前失败关闭。"""

    payload = _blocked_webhook_payload()
    payload.update(updates)
    with pytest.raises(ValueError, match=r"webhook\.payload_(?:invalid|sensitive)"):
        build_webhook_event(
            tenant_id="tenant_prd071",
            event_type="security.output_blocked",
            payload=payload,
            actor_id=None,
            object_id="oss_" + "a" * 32,
            trace_id="trc_prd071",
            source_request_id="req_prd071",
            event_id="osevt_" + "c" * 32,
            timestamp=datetime(2026, 8, 4, 5, 0, tzinfo=UTC),
        )


def _blocked_webhook_payload() -> dict[str, object]:
    """构造不含输出正文、pattern 或冲正资格的冻结输出阻断摘要。"""

    return {
        "schema_version": 1,
        "output_safety_scan_id": "oss_" + "a" * 32,
        "request_id": "req_prd071",
        "scan_mode": "stream_window",
        "scan_status": "completed",
        "decision": "blocked",
        "error_code": "content_policy_violation",
        "exposure_state": "partial",
        "emitted_token_count": 41,
        "checkpoint_count": 2,
        "dlp_rule_id": "dlp_" + "b" * 32,
        "detector_version": "output-safety-dlp-v1",
        "billing_disposition": "dispute_pending",
        "occurred_at": "2026-08-04T05:00:00Z",
    }
