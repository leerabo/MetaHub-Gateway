"""PRD-070 Gateway 会话风险持久事务与 ACK 重放专项测试。

测试使用最新迁移后的隔离 SQLite、内存 Store 和合成摘要；不连接 Provider、Control 或
外部 Redis，也不保存 Prompt、公开 Session Header、Virtual Key 明文或 Token。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.session_risk import (
    InMemorySessionRiskStore,
    SessionRiskAssessment,
    SessionRiskRequestIdentity,
    SessionRiskScope,
    preflight_session_risk,
    record_session_risk_turn,
)
from metahub_gateway.session_risk import service as session_risk_service
from metahub_gateway.session_risk.service import _persist_ack_and_dispatch
from metahub_gateway.usage.ledger import RequestLogContext, claim_gateway_request
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.errors import ApiError
from metahub_shared.session_risk import SessionRiskHmacKey


def _digest(marker: str) -> str:
    """生成固定 HMAC 形状测试摘要；marker 只使用十六进制字符。"""

    return f"srh_{marker * 64}"


def _prepare_database(tmp_path) -> SimpleNamespace:
    """创建 0062 隔离数据库并返回 tenant-qualified 测试 owner。"""

    database_url = f"sqlite:///{(tmp_path / 'session-risk.db').as_posix()}"
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT virtual_key_id FROM virtual_keys
            WHERE tenant_id=? AND status='active'
            ORDER BY virtual_key_id LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert row is not None
        virtual_key_id = str(row["virtual_key_id"])
    finally:
        connection.close()
    return SimpleNamespace(
        database_url=database_url,
        tenant_id=seed.tenant_id,
        virtual_key_id=virtual_key_id,
        session_risk_enabled=True,
        service_mode="public_service",
    )


def _scope(fixture: SimpleNamespace, marker: str) -> SessionRiskScope:
    """构造由同租户 Virtual Key 限定的 API session scope，摘要不含原始 Header。"""

    # owner 与 scope 都是合成 HMAC 形状，避免测试证据误收录真实 Session 或 Key。
    return SessionRiskScope(
        tenant_id=fixture.tenant_id,
        virtual_key_id=fixture.virtual_key_id,
        scope_type="api_session",
        scope_key=_digest(marker),
        owner_slot=_digest("f"),
        hmac_key_version=1,
    )


def _claim(fixture: SimpleNamespace, marker: str) -> tuple[str, str]:
    """先创建迁移 trigger 要求的不可变 Gateway request claim，冻结租户和 Key owner。"""

    request_id = f"req_prd070_service_{marker}"
    trace_id = f"trc_prd070_service_{marker}"
    claim_gateway_request(
        fixture,
        RequestLogContext(
            tenant_id=fixture.tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=fixture.virtual_key_id,
            endpoint="/v1/chat/completions",
            request_model="company-default",
        ),
    )
    # 返回的链路 ID 与 claim 完全一致，后续 observation 不允许自行生成另一套来源。
    return request_id, trace_id


def _assessment(turn: str, request_id: str, trace_id: str) -> SessionRiskAssessment:
    """构造一次 3600 分越狱风险摘要，三次恰好覆盖 high/freeze 主流程。"""

    # 每轮使用不同 HMAC 形状摘要，避免测试把重复轮次误算成新的风险累计。
    return SessionRiskAssessment(
        turn_key_digest=_digest(turn),
        turn_intent_digest=_digest(chr(ord(turn) + 1)),
        jailbreak_count_delta=1,
        sensitive_count_delta=0,
        tool_inducement_count_delta=0,
        turn_delta=3_600,
        input_truncated=False,
        detector_version="intent-rule-engine-v1",
        ruleset_version="session-risk-ruleset-v1",
        threshold_version="session-risk-threshold-v1",
        source_request_id=request_id,
        source_trace_id=trace_id,
        intent_risk_type="jailbreak",
        intent_risk_score=9_600,
    )


def _identity(
    scope: SessionRiskScope,
    assessment: SessionRiskAssessment,
) -> SessionRiskRequestIdentity:
    """将同一 turn 摘要绑定到进程内专用测试 HMAC key。"""

    # 专用测试密钥仅驻留对象私有字段且 repr 隐藏，用于验证生产密钥同样不进入证据。
    return SessionRiskRequestIdentity(
        scope=scope,
        turn_key_digest=assessment.turn_key_digest,
        hmac_key=SessionRiskHmacKey(1, "s" * 32),
    )


def _auth_context(fixture: SimpleNamespace) -> GatewayAuthContext:
    """构造只含持久 owner 的 Gateway 上下文；测试前缀不会作为真实 Token 使用。"""

    # owner 只从已 seed 的租户和 Key 构造，测试请求不能注入 tenant 或更换资源归属。
    return GatewayAuthContext(
        tenant_id=fixture.tenant_id,
        virtual_key_id=fixture.virtual_key_id,
        key_prefix="mh_prd070_test",
        environment="test",
    )


@pytest.mark.anyio
async def test_three_turns_commit_high_risk_and_freeze_as_single_transactions(tmp_path) -> None:
    """主流程：三轮风险写三条 observation、两次跃迁和一个独立 session overlay。"""

    fixture = _prepare_database(tmp_path)
    now = [1_700_000_000.0]
    store = InMemorySessionRiskStore(now=lambda: now[0])
    state = SimpleNamespace(
        session_risk_store=store,
        runtime_event_queue=asyncio.Queue(),
    )
    scope = _scope(fixture, "a")
    results = []
    for index, turn in enumerate(("1", "3", "5"), start=1):
        request_id, trace_id = _claim(fixture, f"main_{index}")
        assessment = _assessment(turn, request_id, trace_id)
        results.append(
            await record_session_risk_turn(
                fixture,
                state,
                _identity(scope, assessment),
                assessment,
            )
        )
        now[0] += 60

    assert [result.status for result in results] == ["monitoring", "high_risk", "frozen"]
    assert results[-1].freeze_transition is True
    connection = connect_database(fixture.database_url)
    try:
        score = connection.execute(
            """
            SELECT status,score,risky_turn_count,state_revision
            FROM session_risk_scores WHERE tenant_id=? AND scope_type='api_session'
              AND scope_key=?
            """,
            (fixture.tenant_id, scope.scope_key),
        ).fetchone()
        assert score is not None
        assert tuple(score) == ("frozen", 10_000, 3, 3)
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "session_risk_observations",
                "session_risk_actions",
                "session_risk_events",
                "session_risk_freezes",
            )
        )
    finally:
        connection.close()
    assert counts == (3, 2, 2, 1)
    assert state.runtime_event_queue.qsize() == 2
    queued = [state.runtime_event_queue.get_nowait() for _ in range(2)]
    assert [event.payload["status"] for event in queued] == ["high_risk", "frozen"]
    assert all(event.payload["scope_reference"] == "api_aaaaaaaaaaaaaaaa" for event in queued)


class _AckLossStore(InMemorySessionRiskStore):
    """只丢失下一次 ACK 响应，用于模拟 DB 已提交后的 Redis 网络不确定性。"""

    lose_next_ack: bool = False

    async def ack_pending(
        self,
        scope: SessionRiskScope,
        score_id: str,
        revision: int,
        turn_key_digest: str,
    ) -> bool:
        """故障开启时保留 pending 并返回不确定，之后恢复真实三元 CAS。"""

        if self.lose_next_ack:
            self.lose_next_ack = False
            return False
        return await super().ack_pending(scope, score_id, revision, turn_key_digest)


@pytest.mark.anyio
async def test_ack_loss_replays_existing_transition_without_duplicate_rows(tmp_path) -> None:
    """异常流程：ACK 丢失后重放原 event ID，不重复 observation/action/event。"""

    fixture = _prepare_database(tmp_path)
    now = [1_700_010_000.0]
    store = _AckLossStore(now=lambda: now[0])
    state = SimpleNamespace(
        session_risk_store=store,
        runtime_event_queue=asyncio.Queue(),
    )
    scope = _scope(fixture, "b")

    first_request, first_trace = _claim(fixture, "replay_1")
    first = _assessment("2", first_request, first_trace)
    await record_session_risk_turn(fixture, state, _identity(scope, first), first)

    now[0] += 60
    second_request, second_trace = _claim(fixture, "replay_2")
    second = _assessment("4", second_request, second_trace)
    store.lose_next_ack = True
    with pytest.raises(ApiError) as error:
        await record_session_risk_turn(fixture, state, _identity(scope, second), second)
    assert error.value.code == "gateway.session_risk_evidence_unavailable"

    pending = await store.load_pending(scope)
    assert pending is not None
    connection = connect_database(fixture.database_url)
    try:
        event_id = connection.execute(
            """
            SELECT session_risk_event_id FROM session_risk_events
            WHERE tenant_id=? AND session_risk_score_id=? AND state_revision=2
            """,
            (fixture.tenant_id, pending.snapshot.score_id),
        ).fetchone()[0]
    finally:
        connection.close()

    await _persist_ack_and_dispatch(fixture, state, store, pending)
    assert await store.load_pending(scope) is None
    connection = connect_database(fixture.database_url)
    try:
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "session_risk_observations",
                "session_risk_actions",
                "session_risk_events",
            )
        )
    finally:
        connection.close()
    assert counts == (2, 1, 1)
    replayed = state.runtime_event_queue.get_nowait()
    assert replayed.event_id == event_id


@pytest.mark.anyio
async def test_preflight_persists_pending_then_compares_new_durable_revision(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 首次失败留下的 pending 在预检清债后，必须按新 revision 对账并继续。"""

    fixture = _prepare_database(tmp_path)
    now = [1_700_020_000.0]
    monkeypatch.setattr(session_risk_service.time, "time", lambda: now[0])
    store = InMemorySessionRiskStore(now=lambda: now[0])
    state = SimpleNamespace(
        session_risk_store=store,
        runtime_event_queue=asyncio.Queue(),
    )
    scope = _scope(fixture, "c")
    first_request, first_trace = _claim(fixture, "pending_first")
    first = _assessment("1", first_request, first_trace)
    await record_session_risk_turn(fixture, state, _identity(scope, first), first)

    now[0] += 60
    second_request, second_trace = _claim(fixture, "pending_second")
    second = _assessment("3", second_request, second_trace)
    # 直接累计但暂不持久化，精确模拟 Redis 成功、SQLite 首次失败后的待清债状态。
    accumulated = await store.accumulate(scope, second)
    assert accumulated.pending is not None and accumulated.snapshot is not None
    result = await preflight_session_risk(
        fixture,
        state,
        _auth_context(fixture),
        (_identity(scope, second),),
    )

    assert result.status == "clean"
    assert result.snapshot == accumulated.snapshot
    assert result.snapshot.revision == 2
    assert await store.load_pending(scope) is None
    connection = connect_database(fixture.database_url)
    try:
        durable_revision = connection.execute(
            """
            SELECT state_revision FROM session_risk_scores
            WHERE tenant_id=? AND session_risk_score_id=?
            """,
            (fixture.tenant_id, accumulated.snapshot.score_id),
        ).fetchone()["state_revision"]
    finally:
        connection.close()
    assert durable_revision == 2


@pytest.mark.anyio
async def test_new_pending_archives_expired_cycle_before_creating_new_score(tmp_path) -> None:
    """请求跨过 TTL 后的新 pending 原子归档旧 cycle，不继承旧分数或阻塞唯一 scope。"""

    fixture = _prepare_database(tmp_path)
    now = [1_700_030_000.0]
    store = InMemorySessionRiskStore(now=lambda: now[0])
    state = SimpleNamespace(
        session_risk_store=store,
        runtime_event_queue=asyncio.Queue(),
    )
    scope = _scope(fixture, "d")
    first_request, first_trace = _claim(fixture, "expiry_first")
    first = _assessment("1", first_request, first_trace)
    first_result = await record_session_risk_turn(
        fixture,
        state,
        _identity(scope, first),
        first,
    )
    assert first_result.snapshot is not None

    now[0] += 1_801
    second_request, second_trace = _claim(fixture, "expiry_second")
    second = _assessment("3", second_request, second_trace)
    second_result = await record_session_risk_turn(
        fixture,
        state,
        _identity(scope, second),
        second,
    )

    assert second_result.snapshot is not None
    assert second_result.snapshot.revision == 1
    assert second_result.snapshot.score_id != first_result.snapshot.score_id
    connection = connect_database(fixture.database_url)
    try:
        scores = connection.execute(
            """
            SELECT session_risk_score_id,status,state_revision,window_expires_at,resolved_at
            FROM session_risk_scores WHERE tenant_id=? AND scope_key=?
            ORDER BY created_at,session_risk_score_id
            """,
            (fixture.tenant_id, scope.scope_key),
        ).fetchall()
        expire_action = connection.execute(
            """
            SELECT action,reason_code,status_before,status_after,state_revision
            FROM session_risk_actions
            WHERE tenant_id=? AND session_risk_score_id=? AND action='expire'
            """,
            (fixture.tenant_id, first_result.snapshot.score_id),
        ).fetchone()
    finally:
        connection.close()
    assert len(scores) == 2
    assert tuple(scores[0]) == (
        first_result.snapshot.score_id,
        "expired",
        2,
        None,
        scores[0]["resolved_at"],
    )
    assert scores[0]["resolved_at"] is not None
    assert tuple(scores[1])[:3] == (second_result.snapshot.score_id, "monitoring", 1)
    assert tuple(expire_action) == ("expire", "window_expired", "monitoring", "expired", 2)


@pytest.mark.anyio
async def test_preflight_archives_expired_database_cycle_and_clears_old_hot_state(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 pending 的过期窗口在读取正文前归档，随后同 scope 可从零建立新 cycle。"""

    fixture = _prepare_database(tmp_path)
    now = [1_700_040_000.0]
    monkeypatch.setattr(session_risk_service.time, "time", lambda: now[0])
    store = InMemorySessionRiskStore(now=lambda: now[0])
    state = SimpleNamespace(
        session_risk_store=store,
        runtime_event_queue=asyncio.Queue(),
    )
    scope = _scope(fixture, "e")
    first_request, first_trace = _claim(fixture, "preflight_expiry_first")
    first = _assessment("1", first_request, first_trace)
    first_result = await record_session_risk_turn(
        fixture,
        state,
        _identity(scope, first),
        first,
    )
    assert first_result.snapshot is not None

    now[0] += 1_801
    next_request, next_trace = _claim(fixture, "preflight_expiry_next")
    next_assessment = _assessment("3", next_request, next_trace)
    result = await preflight_session_risk(
        fixture,
        state,
        _auth_context(fixture),
        (_identity(scope, next_assessment),),
    )

    assert result.status == "clean"
    assert result.identity is not None
    assert result.snapshot is None
    assert await store.load(scope) is None
    connection = connect_database(fixture.database_url)
    try:
        row = connection.execute(
            """
            SELECT status,state_revision,window_expires_at,resolved_at
            FROM session_risk_scores
            WHERE tenant_id=? AND session_risk_score_id=?
            """,
            (fixture.tenant_id, first_result.snapshot.score_id),
        ).fetchone()
        action_count = connection.execute(
            """
            SELECT COUNT(*) AS value FROM session_risk_actions
            WHERE tenant_id=? AND session_risk_score_id=?
              AND action='expire' AND reason_code='window_expired'
            """,
            (fixture.tenant_id, first_result.snapshot.score_id),
        ).fetchone()["value"]
    finally:
        connection.close()
    assert tuple(row)[:3] == ("expired", 2, None)
    assert row["resolved_at"] is not None
    assert action_count == 1


@pytest.mark.anyio
async def test_expired_high_risk_cycle_resolves_without_losing_threshold_history(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """达到 high-risk 但未冻结的窗口到期后转 resolved，并保留首次越阈时间。"""

    fixture = _prepare_database(tmp_path)
    now = [1_700_050_000.0]
    monkeypatch.setattr(session_risk_service.time, "time", lambda: now[0])
    store = InMemorySessionRiskStore(now=lambda: now[0])
    state = SimpleNamespace(
        session_risk_store=store,
        runtime_event_queue=asyncio.Queue(),
    )
    scope = _scope(fixture, "a")
    latest_assessment = None
    high_risk = None
    for index, turn in enumerate(("1", "3"), start=1):
        request_id, trace_id = _claim(fixture, f"high_expiry_{index}")
        latest_assessment = _assessment(turn, request_id, trace_id)
        high_risk = await record_session_risk_turn(
            fixture,
            state,
            _identity(scope, latest_assessment),
            latest_assessment,
        )
        now[0] += 60
    assert high_risk is not None and high_risk.snapshot is not None
    assert high_risk.snapshot.state == "high_risk"
    assert latest_assessment is not None

    now[0] += 1_801
    result = await preflight_session_risk(
        fixture,
        state,
        _auth_context(fixture),
        (_identity(scope, latest_assessment),),
    )
    assert result.status == "clean" and result.snapshot is None
    connection = connect_database(fixture.database_url)
    try:
        row = connection.execute(
            """
            SELECT status,state_revision,first_high_risk_at,window_expires_at,resolved_at
            FROM session_risk_scores
            WHERE tenant_id=? AND session_risk_score_id=?
            """,
            (fixture.tenant_id, high_risk.snapshot.score_id),
        ).fetchone()
        action = connection.execute(
            """
            SELECT action,reason_code,status_before,status_after,state_revision
            FROM session_risk_actions
            WHERE tenant_id=? AND session_risk_score_id=? AND action='expire'
            """,
            (fixture.tenant_id, high_risk.snapshot.score_id),
        ).fetchone()
    finally:
        connection.close()
    assert row["status"] == "resolved" and row["state_revision"] == 3
    assert row["first_high_risk_at"] is not None
    assert row["window_expires_at"] is None and row["resolved_at"] is not None
    assert tuple(action) == ("expire", "window_expired", "high_risk", "resolved", 3)
