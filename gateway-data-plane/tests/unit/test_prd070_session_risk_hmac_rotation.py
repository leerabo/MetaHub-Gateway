"""PRD-070 HMAC current/previous 轮换门禁与移除 verifier 专项测试。

测试使用 0062 隔离 SQLite 和内存 Store，不连接开发库、外部 Redis 或 Provider。所有会话、
turn 和 intent 标识都是规范合成 HMAC；断言只检查固定错误码、版本和安全计数。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.session_risk import (
    InMemorySessionRiskStore,
    SessionRiskAssessment,
    SessionRiskHmacRotationReport,
    SessionRiskHmacRotationVerifierUnavailable,
    SessionRiskRedisHmacReferenceReport,
    SessionRiskRequestIdentity,
    SessionRiskScope,
    preflight_session_risk,
    record_session_risk_turn,
    verify_session_risk_hmac_removal,
)
from metahub_gateway.session_risk import rotation as session_risk_rotation
from metahub_gateway.session_risk import service as session_risk_service
from metahub_gateway.usage.ledger import RequestLogContext, claim_gateway_request
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.errors import ApiError
from metahub_shared.session_risk import SessionRiskHmacKey


def _digest(marker: str) -> str:
    """生成固定长度测试 HMAC；marker 必须是单个小写十六进制字符。"""

    assert len(marker) == 1 and marker in "0123456789abcdef"
    return f"srh_{marker * 64}"


def _prepare_database(tmp_path) -> SimpleNamespace:
    """创建最新隔离数据库，并返回默认租户和 Virtual Key 的最小运行配置。"""

    database_url = f"sqlite:///{(tmp_path / 'hmac-rotation.db').as_posix()}"
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        key = connection.execute(
            """
            SELECT virtual_key_id FROM virtual_keys
            WHERE tenant_id=? AND status='active'
            ORDER BY virtual_key_id LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert key is not None
    finally:
        connection.close()
    return SimpleNamespace(
        database_url=database_url,
        tenant_id=seed.tenant_id,
        virtual_key_id=str(key["virtual_key_id"]),
        session_risk_enabled=True,
        service_mode="public_service",
    )


def _scope(
    fixture: SimpleNamespace,
    *,
    version: int,
    scope_marker: str,
    owner_marker: str,
) -> SessionRiskScope:
    """构造租户/Key 限定的 API scope；不同版本使用不同 HMAC 投影。"""

    # scope 和 owner 都只使用合成摘要，测试不会把原始 Header 写入失败证据。
    return SessionRiskScope(
        tenant_id=fixture.tenant_id,
        virtual_key_id=fixture.virtual_key_id,
        scope_type="api_session",
        scope_key=_digest(scope_marker),
        owner_slot=_digest(owner_marker),
        hmac_key_version=version,
    )


def _assessment(marker: str, request_id: str, trace_id: str) -> SessionRiskAssessment:
    """构造一轮 3600 分越狱摘要，供持久 score 和 high-risk 两轮测试复用。"""

    next_marker = format((int(marker, 16) + 1) % 16, "x")
    # 组合信号与生产计分合同一致，避免手写 delta 绕过模型约束。
    return SessionRiskAssessment(
        turn_key_digest=_digest(marker),
        turn_intent_digest=_digest(next_marker),
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
        dlp_action="allow",
        dlp_current_turn_match_count=0,
        tool_inducement_detected=False,
    )


def _identity(
    scope: SessionRiskScope,
    assessment: SessionRiskAssessment,
) -> SessionRiskRequestIdentity:
    """把测试 scope 和 turn 摘要绑定到同版本、repr 隐藏的专用 HMAC key。"""

    # key 版本必须与 scope 一致，才能真实覆盖 current/previous 精确选择。
    return SessionRiskRequestIdentity(
        scope=scope,
        turn_key_digest=assessment.turn_key_digest,
        hmac_key=SessionRiskHmacKey(scope.hmac_key_version, "h" * 32),
    )


def _auth(fixture: SimpleNamespace) -> GatewayAuthContext:
    """构造只含持久 tenant/Key owner 的 Gateway 鉴权上下文。"""

    # tenant 和 Key 均来自隔离库，调用方不能通过候选 scope 改写鉴权 owner。
    return GatewayAuthContext(
        tenant_id=fixture.tenant_id,
        virtual_key_id=fixture.virtual_key_id,
        key_prefix="mh_prd070_rotation",
        environment="test",
    )


# 每个风险 observation 都必须先有同 tenant/Key 的不可变 Gateway request claim。
def _claim(fixture: SimpleNamespace, marker: str) -> tuple[str, str]:
    """创建迁移触发器要求的租户与 Virtual Key 双重限定 Gateway 请求声明。"""

    request_id = f"req_prd070_rotation_{marker}"
    trace_id = f"trc_prd070_rotation_{marker}"
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
    return request_id, trace_id


async def _record(
    fixture: SimpleNamespace,
    state: SimpleNamespace,
    scope: SessionRiskScope,
    marker: str,
) -> SessionRiskRequestIdentity:
    """持久化一轮风险并返回同一可信 identity，确保 Redis pending 已被精确 ACK。"""

    request_id, trace_id = _claim(fixture, marker)
    assessment = _assessment(marker[0], request_id, trace_id)
    identity = _identity(scope, assessment)
    result = await record_session_risk_turn(fixture, state, identity, assessment)
    assert result.snapshot is not None
    return identity


def _state(now: list[float]) -> SimpleNamespace:
    """创建使用可控时钟的内存 Store 和事件队列。"""

    return SimpleNamespace(
        session_risk_store=InMemorySessionRiskStore(now=lambda: now[0]),
        runtime_event_queue=asyncio.Queue(),
    )


@pytest.mark.anyio
async def test_current_version_active_cycle_is_selected(tmp_path) -> None:
    """current 命中：活动 score 使用当前版本时预检继续同一 cycle。"""

    fixture = _prepare_database(tmp_path)
    now = [time.time()]
    state = _state(now)
    current_scope = _scope(fixture, version=2, scope_marker="2", owner_marker="3")
    current = await _record(fixture, state, current_scope, "4_current")

    result = await preflight_session_risk(fixture, state, _auth(fixture), (current,))

    assert result.status == "clean"
    assert result.identity is not None
    assert result.identity.scope.hmac_key_version == 2
    assert result.snapshot is not None and result.snapshot.hmac_key_version == 2


@pytest.mark.anyio
async def test_previous_version_active_cycle_continues_during_rotation(tmp_path) -> None:
    """保留 previous：双候选必须选择旧活动 cycle，而不是用 current 创建旁路。"""

    fixture = _prepare_database(tmp_path)
    now = [time.time()]
    state = _state(now)
    previous_scope = _scope(fixture, version=1, scope_marker="5", owner_marker="6")
    previous = await _record(fixture, state, previous_scope, "7_previous")
    current_scope = _scope(fixture, version=2, scope_marker="8", owner_marker="9")
    current = replace(previous, scope=current_scope, hmac_key=SessionRiskHmacKey(2, "i" * 32))

    result = await preflight_session_risk(
        fixture,
        state,
        _auth(fixture),
        (current, previous),
    )

    assert result.status == "clean"
    assert result.identity == previous
    assert result.snapshot is not None and result.snapshot.hmac_key_version == 1


@pytest.mark.anyio
async def test_removed_previous_with_same_key_active_cycle_fails_closed(tmp_path) -> None:
    """移除仍被引用的 previous：同租户/Key 在读取正文前固定返回版本不可用。"""

    fixture = _prepare_database(tmp_path)
    now = [time.time()]
    state = _state(now)
    previous_scope = _scope(fixture, version=1, scope_marker="a", owner_marker="b")
    previous = await _record(fixture, state, previous_scope, "c_previous_removed")
    current_scope = _scope(fixture, version=2, scope_marker="d", owner_marker="e")
    current = replace(previous, scope=current_scope, hmac_key=SessionRiskHmacKey(2, "j" * 32))

    with pytest.raises(ApiError) as error:
        await preflight_session_risk(fixture, state, _auth(fixture), (current,))

    assert error.value.code == "gateway.session_risk_hmac_version_unavailable"


@pytest.mark.anyio
async def test_archived_previous_cycle_does_not_block_current(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已归档旧 cycle：窗口到期并形成历史后，current 可建立全新 scope。"""

    fixture = _prepare_database(tmp_path)
    now = [1_700_130_000.0]
    monkeypatch.setattr(session_risk_service.time, "time", lambda: now[0])
    state = _state(now)
    previous_scope = _scope(fixture, version=1, scope_marker="1", owner_marker="2")
    previous = await _record(fixture, state, previous_scope, "3_archive")
    now[0] += 1_801
    archived = await preflight_session_risk(fixture, state, _auth(fixture), (previous,))
    assert archived.status == "clean" and archived.snapshot is None

    current_scope = _scope(fixture, version=2, scope_marker="4", owner_marker="5")
    current = replace(previous, scope=current_scope, hmac_key=SessionRiskHmacKey(2, "k" * 32))
    result = await preflight_session_risk(fixture, state, _auth(fixture), (current,))

    assert result.status == "clean" and result.identity == current
    connection = connect_database(fixture.database_url)
    try:
        row = connection.execute(
            """
            SELECT status FROM session_risk_scores
            WHERE tenant_id=? AND scope_key=? AND hmac_key_version=1
            """,
            (fixture.tenant_id, previous_scope.scope_key),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None and row["status"] == "expired"


def _insert_isolated_key(
    fixture: SimpleNamespace,
    *,
    tenant_id: str,
    virtual_key_id: str,
    create_tenant: bool,
) -> SimpleNamespace:
    """为隔离测试创建合法 tenant/Key owner，不复制真实 Key 或管理员凭据。"""

    connection = connect_database(fixture.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 只有跨租户分支需要创建租户根；同租户跨 Key 必须复用既有 tenant_id。
        if create_tenant:
            # 跨租户用例创建合法根租户，由迁移 trigger 自动生成其默认 dev Environment。
            connection.execute(
                """
                INSERT INTO tenants(
                  tenant_id,slug,name,service_mode,status,created_at,updated_at
                ) VALUES(?,?,?,'public_service','active',datetime('now'),datetime('now'))
                """,
                (tenant_id, f"slug-{tenant_id}", "PRD-070 隔离租户"),
            )
        environment = connection.execute(
            """
            SELECT environment_id FROM environments
            WHERE tenant_id=? AND code='dev' AND status='active'
            """,
            (tenant_id,),
        ).fetchone()
        assert environment is not None
        hash_marker = "8" if create_tenant else "9"
        connection.execute(
            """
            INSERT INTO virtual_keys(
              virtual_key_id,tenant_id,name,key_prefix,key_hash,status,
              environment,environment_id,created_at,updated_at
            ) VALUES(?,?,?,'mh_iso',?,'active','dev',?,datetime('now'),datetime('now'))
            """,
            (
                virtual_key_id,
                tenant_id,
                "PRD-070 隔离 Key",
                hash_marker * 64,
                environment["environment_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return SimpleNamespace(
        database_url=fixture.database_url,
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        session_risk_enabled=True,
        service_mode="public_service",
    )


@pytest.mark.anyio
async def test_unknown_versions_in_other_tenant_and_key_do_not_cross_block(tmp_path) -> None:
    """隔离流程：其他租户或其他 Key 的旧版本活动 score 不得阻断当前 owner。"""

    fixture = _prepare_database(tmp_path)
    same_tenant_other_key = _insert_isolated_key(
        fixture,
        tenant_id=fixture.tenant_id,
        virtual_key_id="vk_prd070_other_key",
        create_tenant=False,
    )
    other_tenant = _insert_isolated_key(
        fixture,
        tenant_id="tenant_prd070_other",
        virtual_key_id="vk_prd070_other_tenant",
        create_tenant=True,
    )
    now = [time.time()]
    state = _state(now)
    await _record(
        same_tenant_other_key,
        state,
        _scope(same_tenant_other_key, version=1, scope_marker="6", owner_marker="7"),
        "8_other_key",
    )
    await _record(
        other_tenant,
        state,
        _scope(other_tenant, version=1, scope_marker="9", owner_marker="a"),
        "b_other_tenant",
    )
    current_scope = _scope(fixture, version=2, scope_marker="c", owner_marker="d")
    assessment = _assessment("e", "req_prd070_rotation_isolated", "trc_prd070_rotation_isolated")
    current = _identity(current_scope, assessment)

    result = await preflight_session_risk(fixture, state, _auth(fixture), (current,))

    assert result.status == "clean"
    assert result.identity == current
    assert result.snapshot is None


class _EmptyRedisInspector:
    """返回零引用 Redis 报告，用于单独验证数据库与 action digest 门禁。"""

    async def inspect_hmac_rotation_references(
        self,
        *,
        current_version: int,
        removing_version: int,
    ) -> SessionRiskRedisHmacReferenceReport:
        """复核调用版本后返回不可变零计数；不连接网络或共享状态。"""

        assert (current_version, removing_version) == (2, 1)
        # 零计数替身只隔离 Redis，数据库查询仍使用真实 0062 表和事务。
        return SessionRiskRedisHmacReferenceReport(0, 0, 0, 0, 0)


@pytest.mark.anyio
async def test_verifier_blocks_active_score_and_unexpired_action_digest(tmp_path) -> None:
    """verifier 主流程：活动 score 与 24 小时人工摘要分别阻断，过期后才 ready。"""

    fixture = _prepare_database(tmp_path)
    now = [1_700_150_000.0]
    state = _state(now)
    scope = _scope(fixture, version=1, scope_marker="e", owner_marker="f")
    await _record(fixture, state, scope, "1_action_first")
    await _record(fixture, state, scope, "3_action_second")

    active_report = await verify_session_risk_hmac_removal(
        fixture.database_url,
        _EmptyRedisInspector(),
        current_version=2,
        removing_version=1,
        now_epoch=int(now[0]),
    )
    assert isinstance(active_report, SessionRiskHmacRotationReport)
    assert active_report.active_score_references == 1
    assert active_report.ready is False

    connection = connect_database(fixture.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        score = connection.execute(
            """
            SELECT * FROM session_risk_scores
            WHERE tenant_id=? AND scope_key=? AND status='high_risk'
            """,
            (fixture.tenant_id, scope.scope_key),
        ).fetchone()
        actor = connection.execute(
            """
            SELECT user_id FROM users
            WHERE tenant_id=? AND status='active' ORDER BY user_id LIMIT 1
            """,
            (fixture.tenant_id,),
        ).fetchone()
        assert score is not None and actor is not None
        next_revision = int(score["state_revision"]) + 1
        connection.execute(
            """
            UPDATE session_risk_scores
            SET status='resolved',state_revision=?,resolved_at=datetime(?,'unixepoch'),
                window_expires_at=NULL,updated_at=datetime(?,'unixepoch')
            WHERE tenant_id=? AND session_risk_score_id=?
            """,
            (
                next_revision,
                int(now[0]),
                int(now[0]),
                fixture.tenant_id,
                score["session_risk_score_id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO session_risk_actions(
              session_risk_action_id,tenant_id,session_risk_score_id,session_risk_freeze_id,
              action,reason_code,actor_type,actor_id,status_before,status_after,score_snapshot,
              state_revision,source_request_id,source_trace_id,digest_key_version,
              idempotency_key_digest,intent_digest,idempotency_expires_at,created_at
            ) VALUES(
              ?,?,?,NULL,'dismiss','false_positive','user',?,'high_risk','resolved',?,?,
              'req_prd070_rotation_action','trc_prd070_rotation_action',1,?,?,
              datetime(?,'unixepoch'),datetime(?,'unixepoch')
            )
            """,
            (
                "sra_" + "1" * 32,
                fixture.tenant_id,
                score["session_risk_score_id"],
                actor["user_id"],
                score["score"],
                next_revision,
                _digest("2"),
                _digest("3"),
                int(now[0]) + 86_400,
                int(now[0]),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    action_report = await verify_session_risk_hmac_removal(
        fixture.database_url,
        _EmptyRedisInspector(),
        current_version=2,
        removing_version=1,
        now_epoch=int(now[0]),
    )
    assert action_report.active_score_references == 0
    assert action_report.unexpired_action_digest_references == 1
    assert action_report.ready is False

    expired_report = await verify_session_risk_hmac_removal(
        fixture.database_url,
        _EmptyRedisInspector(),
        current_version=2,
        removing_version=1,
        now_epoch=int(now[0]) + 86_401,
    )
    assert expired_report.unexpired_action_digest_references == 0
    assert expired_report.ready is True
    assert expired_report.as_safe_dict()["status"] == "ready"


@pytest.mark.anyio
async def test_verifier_folds_database_filesystem_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库父目录创建失败也只返回固定 verifier 异常，不泄露敏感路径。"""

    sensitive_marker = "synthetic-sensitive-database-path"

    def connect_failure(_database_url: str) -> None:
        """模拟共享连接器在 SQLite connect 前创建父目录失败。"""

        # PermissionError 属于真实 OSError 分支，消息刻意含敏感标记以验证异常脱敏。
        raise PermissionError(sensitive_marker)

    monkeypatch.setattr(session_risk_rotation, "connect_database", connect_failure)
    with pytest.raises(SessionRiskHmacRotationVerifierUnavailable) as caught:
        await verify_session_risk_hmac_removal(
            "sqlite:///synthetic-sensitive-database-path.db",
            _EmptyRedisInspector(),
            current_version=2,
            removing_version=1,
        )

    assert str(caught.value) == "session risk hmac rotation verifier unavailable"
    assert caught.value.__cause__ is None
    assert sensitive_marker not in repr(caught.value)
