"""PRD-070 Gateway 会话风险 Store 的原子合同测试。

测试只使用合成摘要，不包含真实 Prompt、Session Header、Virtual Key 或 Token。内存实现必须
与生产 Lua 接受相同边界，避免单元测试因宽松对象或串行执行掩盖生产故障。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from metahub_gateway.session_risk import (
    InMemorySessionRiskStore,
    RedisSessionRiskStore,
    SessionRiskAssessment,
    SessionRiskContractError,
    SessionRiskPendingEvidence,
    SessionRiskScope,
    SessionRiskSnapshot,
    SessionRiskStoreUnavailable,
    SessionRiskTurnConflict,
)
from metahub_gateway.session_risk import store as store_module


def _digest(marker: str) -> str:
    """生成不含业务原文的固定测试 HMAC 形状摘要。"""

    return f"srh_{marker * 64}"


def _scope(*, owner: str = "a", tenant_id: str = "tenant_a") -> SessionRiskScope:
    """构造带租户限定与 owner 槽位的 API 会话测试身份，防止跨租户复用状态。"""

    # tenant_id 与 owner_slot 均显式可变，用于逐项证明 Store key 的隔离维度完整。
    return SessionRiskScope(
        tenant_id=tenant_id,
        virtual_key_id="vk_test",
        scope_type="api_session",
        scope_key=_digest("b"),
        owner_slot=_digest(owner),
        hmac_key_version=1,
    )


def _assessment(
    marker: str,
    *,
    delta: int = 3_600,
) -> SessionRiskAssessment:
    """构造单次越狱类风险 assessment；每个 marker 表示不同可信 turn。"""

    # marker 同时区分轮次摘要与来源 ID，确保幂等测试不会意外共享证据。
    return SessionRiskAssessment(
        turn_key_digest=_digest(marker),
        turn_intent_digest=_digest(chr(ord(marker) + 1)),
        jailbreak_count_delta=1 if delta else 0,
        sensitive_count_delta=0,
        tool_inducement_count_delta=0,
        turn_delta=delta,
        input_truncated=False,
        detector_version="intent-rule-engine-v1",
        ruleset_version="intent-rules-v1",
        threshold_version="session-risk-threshold-v1",
        source_request_id=f"req_{marker}",
        source_trace_id=f"trace_{marker}",
        intent_risk_result_id=f"irr_{marker * 32}",
        intent_risk_type="jailbreak",
        intent_risk_score=9_600 if delta else 0,
    )


@pytest.mark.anyio
async def test_zero_delta_turn_is_deduplicated_without_score_or_ttl_refresh() -> None:
    """安全 turn 只建立 30 分钟 claim；重放不建 score，不产生 pending。"""

    now = 1_000.0
    store = InMemorySessionRiskStore(now=lambda: now)
    scope = _scope()
    assessment = _assessment("c", delta=0)

    first = await store.accumulate(scope, assessment)
    replay = await store.accumulate(scope, assessment)

    assert first.snapshot is None
    assert first.pending is None
    assert replay.duplicate is True
    assert await store.load(scope) is None


@pytest.mark.anyio
async def test_same_turn_with_different_intent_is_fixed_conflict() -> None:
    """同一 Turn ID 的正文意图发生变化时拒绝，不选择任一摘要继续累计。"""

    store = InMemorySessionRiskStore(now=lambda: 1_000.0)
    scope = _scope()
    assessment = _assessment("c", delta=0)
    await store.accumulate(scope, assessment)

    with pytest.raises(SessionRiskTurnConflict):
        await store.accumulate(
            scope,
            replace(assessment, turn_intent_digest=_digest("e")),
        )


@pytest.mark.anyio
async def test_three_risky_turns_cross_high_then_freeze_with_single_pending() -> None:
    """三次 3600 风险 turn 依次进入 monitoring/high_risk/frozen，且每次先清 pending。"""

    store = InMemorySessionRiskStore(now=lambda: 1_000.0)
    scope = _scope()

    first = await store.accumulate(scope, _assessment("c"))
    assert first.snapshot is not None and first.snapshot.state == "monitoring"
    assert first.snapshot.score == 3_600
    assert first.pending is not None and first.pending.high_risk_transition is False
    with pytest.raises(SessionRiskPendingEvidence):
        await store.accumulate(scope, _assessment("e"))
    assert await store.ack_pending(
        scope,
        first.snapshot.score_id,
        first.snapshot.revision,
        first.pending.assessment.turn_key_digest,
    )

    second = await store.accumulate(scope, _assessment("e"))
    assert second.snapshot is not None and second.snapshot.state == "high_risk"
    assert second.snapshot.score == 7_200
    assert second.pending is not None and second.pending.high_risk_transition is True
    assert second.pending.freeze_transition is False
    assert await store.ack_pending(
        scope,
        second.snapshot.score_id,
        second.snapshot.revision,
        second.pending.assessment.turn_key_digest,
    )

    third = await store.accumulate(scope, _assessment("1"))
    assert third.snapshot is not None and third.snapshot.state == "frozen"
    assert third.snapshot.score == 10_000
    assert third.snapshot.risky_turn_count == 3
    assert third.snapshot.expires_at_epoch is None
    assert third.pending is not None and third.pending.freeze_transition is True


@pytest.mark.anyio
async def test_ack_is_three_value_cas_and_safe_turn_does_not_refresh_risk_window() -> None:
    """错误 ACK 不清债；正确 ACK 后安全 turn 不能延长最后风险时刻的 TTL。"""

    now = 1_000.0
    store = InMemorySessionRiskStore(now=lambda: now)
    scope = _scope()
    result = await store.accumulate(scope, _assessment("c"))
    assert result.snapshot is not None and result.pending is not None
    original_expiry = result.snapshot.expires_at_epoch

    assert not await store.ack_pending(
        scope,
        result.snapshot.score_id,
        result.snapshot.revision + 1,
        result.pending.assessment.turn_key_digest,
    )
    assert await store.load_pending(scope) is not None
    assert await store.ack_pending(
        scope,
        result.snapshot.score_id,
        result.snapshot.revision,
        result.pending.assessment.turn_key_digest,
    )

    now = 1_500.0
    clean = await store.accumulate(scope, _assessment("e", delta=0))
    assert clean.snapshot is not None
    assert clean.snapshot.expires_at_epoch == original_expiry


@pytest.mark.anyio
async def test_restore_is_revision_monotonic_and_reset_is_cycle_cas() -> None:
    """恢复只接受更高 revision；reset 不能删除 revision 已变化的 cycle。"""

    store = InMemorySessionRiskStore(now=lambda: 1_000.0)
    scope = _scope()
    snapshot = SessionRiskSnapshot(
        score_id=f"srs_{'a' * 32}",
        score=7_200,
        risky_turn_count=2,
        jailbreak_count=2,
        sensitive_count=0,
        tool_inducement_count=0,
        state="high_risk",
        revision=2,
        window_started_at_epoch=1_000,
        last_risk_at_epoch=1_100,
        expires_at_epoch=2_900,
        hmac_key_version=1,
        threshold_version="session-risk-threshold-v1",
    )

    assert await store.restore(scope, snapshot)
    assert not await store.restore(scope, snapshot)
    assert not await store.reset(scope, snapshot.score_id, snapshot.revision + 1)
    assert await store.reset(scope, snapshot.score_id, snapshot.revision)
    assert await store.load(scope) is None


@pytest.mark.anyio
async def test_tenant_owner_and_store_failure_are_fail_closed() -> None:
    """同 owner slot 不能跨 HMAC 版本复用，Store 故障也不能返回空状态冒充可用。"""

    store = InMemorySessionRiskStore(now=lambda: 1_000.0)
    scope = _scope()
    snapshot = SessionRiskSnapshot(
        score_id=f"srs_{'a' * 32}",
        score=1_000,
        risky_turn_count=1,
        jailbreak_count=1,
        sensitive_count=0,
        tool_inducement_count=0,
        state="monitoring",
        revision=1,
        window_started_at_epoch=1_000,
        last_risk_at_epoch=1_000,
        expires_at_epoch=2_800,
        hmac_key_version=1,
        threshold_version="session-risk-threshold-v1",
    )
    assert await store.restore(scope, snapshot)
    with pytest.raises(SessionRiskStoreUnavailable):
        await store.load(replace(scope, hmac_key_version=2))

    store.set_available(False)
    with pytest.raises(SessionRiskStoreUnavailable):
        await store.ping()


def test_models_reject_raw_or_malformed_identifiers_and_invalid_combination() -> None:
    """Header 原文、非法摘要和零分带类别计数均在进入 Store 前被拒绝。"""

    with pytest.raises(SessionRiskContractError):
        replace(_scope(), owner_slot="mhs_client_header_value")
    with pytest.raises(SessionRiskContractError):
        replace(_assessment("c", delta=0), jailbreak_count_delta=1)


def test_accumulate_lua_validates_exact_contract_before_any_write() -> None:
    """生产 Lua 在 claim/score 写入前校验全部 23 个参数并先编码 pending。

    Redis Lua 的运行时错误不会自动回滚此前命令，因此本测试冻结参数索引和代码顺序，防止
    未来新增字段时再次出现“先 SET claim、后发现坏 delta/摘要”的不可重试残留。
    """

    script = store_module._ACCUMULATE_SCRIPT
    validation_boundary = script.index("if redis.call('HEXISTS'")
    assert "#ARGV ~= 23" in script[:validation_boundary]
    assert "BAD_DIGEST" in script[:validation_boundary]
    assert "BAD_VERSION" in script[:validation_boundary]
    assert "BAD_COMBINATION" in script[:validation_boundary]
    assert "ARGV[24]" not in script
    assert "or ARGV[18] ~= '1800'" in script[:validation_boundary]
    assert "local intent_type = ARGV[19]" in script[:validation_boundary]
    # 风险 turn 的 cjson 也必须在 claim 前生成；否则编码错误仍会留下重复 claim。
    assert script.index("local pending_payload = cjson.encode(pending)") < script.index(
        "local claim_ok = redis.call('SET'"
    )


@pytest.mark.anyio
async def test_redis_accumulate_emits_exact_23_argument_lua_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 Store 的 EVAL 参数位置必须与 Lua v1 合同逐项一致。"""

    store = RedisSessionRiskStore("redis://127.0.0.1:6379/0")
    calls: list[tuple[object, ...]] = []

    async def command(*parts: object) -> object:
        """截获 RESP 命令；零分 turn 返回 CLEAN，随后 HGETALL 返回空 score。"""

        calls.append(parts)
        return ["CLEAN"] if parts[0] == "EVAL" else []

    monkeypatch.setattr(store, "_command", command)
    result = await store.accumulate(_scope(), _assessment("c", delta=0))
    assert result.snapshot is None
    eval_call = calls[0]
    assert eval_call[:3] == ("EVAL", store_module._ACCUMULATE_SCRIPT, "2")
    argv = eval_call[5:]
    assert len(argv) == 23
    assert argv[16].startswith("srs_")
    assert argv[17] == "1800"
    assert argv[18] == "jailbreak"
    assert argv[19] == "0"
    assert argv[20:] == ("allow", "0", "0")
