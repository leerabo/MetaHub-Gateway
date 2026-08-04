"""PRD-071 StreamSession 与输出安全证据原子终结专项测试。

本模块在真实 1→63 迁移库上验证 StreamSession、OutputSafetyScan 和 blocked source event
共享一个事务。所有规则正文均为合成占位值，不启动 Provider、网络服务或全量 Gateway 回归。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from metahub_gateway.output_safety import (
    OutputSafetyTerminalState,
    PreparedOutputSafetyScan,
    prepare_output_safety_scan,
)
from metahub_gateway.streaming.sessions import (
    StreamSession,
    mark_stream_session_active,
    safe_finish_stream_session,
)
from metahub_gateway.streaming.sessions import create_stream_session
from metahub_gateway.usage.ledger import RequestLogContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database


@dataclass(frozen=True, slots=True)
class _AtomicFixture:
    """保存真实 0063 测试库及同租户路由身份。

    ``identity`` 只包含稳定 ID/code，不包含 Virtual Key、Provider Key、Token 或请求正文。
    """

    database_url: str
    config: SimpleNamespace
    identity: dict[str, str]


@dataclass(frozen=True, slots=True)
class _StreamScanBundle:
    """保存同 tenant/request/trace 的内存 Session、pending scan 和决定性规则 ID。"""

    session: StreamSession
    scan: PreparedOutputSafetyScan
    rule_id: str


def _sqlite_url(path: Path) -> str:
    """把沙箱内独立测试文件转换成 Shared 连接层可解析的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def atomic_fixture(tmp_path_factory: pytest.TempPathFactory) -> _AtomicFixture:
    """一次构造带 seed 的真实 0063 数据库，供本模块的互不重名用例复用。

    参数：pytest 模块临时目录工厂。返回：数据库 URL、最小配置和可信路由身份。
    关键约束：所有文件只写入 ``D:\\MetaHub\\.tmp`` 下的 pytest basetemp。
    """

    database_path = tmp_path_factory.mktemp("prd071-stream-atomic") / "atomic.db"
    database_url = _sqlite_url(database_path)
    apply_migrations(database_url)
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,key.virtual_key_id,
               environment.environment_id,environment.code AS environment_code,
               model.model_id,provider.provider_id,provider.provider_code
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN virtual_keys key ON key.tenant_id=tenant.tenant_id
        JOIN environments environment
          ON environment.tenant_id=tenant.tenant_id AND environment.code='dev'
        JOIN models model ON model.tenant_id=tenant.tenant_id
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id
         AND provider.provider_id=model.provider_id
        ORDER BY tenant.slug='default' DESC,tenant.tenant_id,user.user_id,
                 key.virtual_key_id,model.model_id
        LIMIT 1
        """
    ).fetchone()
    connection.close()
    assert row is not None
    identity = {column: str(row[column]) for column in row.keys()}
    return _AtomicFixture(
        database_url=database_url,
        config=SimpleNamespace(database_url=database_url),
        identity=identity,
    )


def _create_bundle(fixture: _AtomicFixture, suffix: str) -> _StreamScanBundle:
    """创建 claim、output 规则、starting Session 和已提交 pending scan。

    参数：真实库夹具和用例唯一后缀。返回：同 owner 的 Session/scan/rule 组合。
    关键约束：pending scan 在 Session 创建后、active 握手前提交，符合真实主链路顺序。
    """

    identity = fixture.identity
    request_id = f"req_prd071_atomic_{suffix}"
    trace_id = f"trc_prd071_atomic_{suffix}"
    rule_id = "dlp_" + hashlib.sha256(suffix.encode("ascii")).hexdigest()[:32]
    pattern = f"fixture-output-{suffix}"
    pattern_hash = hashlib.sha256(f"keyword:{pattern}".encode("utf-8")).hexdigest()
    connection = connect_database(fixture.database_url)
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled',?)
        """,
        (
            identity["tenant_id"],
            request_id,
            trace_id,
            identity["virtual_key_id"],
            "2026-08-04T08:00:00Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,
          action,status,priority,scan_scope,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'keyword',?,?,'block','active',10,'output',?,?,?,?)
        """,
        (
            rule_id,
            identity["tenant_id"],
            f"PRD071 atomic {suffix}",
            pattern,
            f"sha256:{pattern_hash}",
            identity["user_id"],
            identity["user_id"],
            "2026-08-04T08:00:00Z",
            "2026-08-04T08:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    context = RequestLogContext(
        tenant_id=identity["tenant_id"],
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=identity["virtual_key_id"],
        environment_id=identity["environment_id"],
        environment_code=identity["environment_code"],
        model_id=identity["model_id"],
        provider_id=identity["provider_id"],
        provider_code=identity["provider_code"],
    )
    session = create_stream_session(
        fixture.config,
        context,
        inter_token_timeout_ms=30_000,
    )
    scan = prepare_output_safety_scan(
        fixture.config,
        tenant_id=identity["tenant_id"],
        request_id=request_id,
        trace_id=trace_id,
        scan_mode="stream_window",
        stream_session_id=session.stream_session_id,
    )
    assert scan.scan_status == "pending"
    # 会话和扫描句柄必须共享 tenant/request 绑定，后续原子终结测试禁止拼接其他租户上下文。
    return _StreamScanBundle(session=session, scan=scan, rule_id=rule_id)


def _blocked_terminal(bundle: _StreamScanBundle) -> OutputSafetyTerminalState:
    """构造包含部分暴露计数的固定 completed+blocked scan 终态。"""

    return OutputSafetyTerminalState(
        scan_status="completed",
        decision="blocked",
        action="block",
        dlp_rule_id=bundle.rule_id,
        rule_type="keyword",
        match_count=1,
        checkpoint_count=2,
        observed_output_token_count=8,
        scanned_token_count=8,
        emitted_token_count=2,
        emitted_sse_event_count=1,
        emitted_bytes=bundle.session.bytes_sent,
        exposure_state="partial",
        billing_disposition="dispute_pending",
    )


def _fact_counts(fixture: _AtomicFixture, bundle: _StreamScanBundle) -> tuple[object, ...]:
    """按租户绑定读取 session、scan 与 source event 的最小原子性事实。

    参数是迁移后的数据库夹具和同租户会话扫描组合；返回值保留三类事实的终态与事件数量，
    用于断言事务提交或回滚后不存在跨租户读取和半完成安全状态。
    """

    connection = connect_database(fixture.database_url)
    row = connection.execute(
        """
        SELECT session.status,session.error_code,session.completed_at,
               scan.scan_status,scan.decision,scan.failure_reason_code,scan.completed_at,
               (SELECT COUNT(*) FROM output_safety_events event
                 WHERE event.tenant_id=scan.tenant_id
                   AND event.output_safety_scan_id=scan.output_safety_scan_id) AS event_count
        FROM stream_sessions session
        JOIN output_safety_scans scan
          ON scan.tenant_id=session.tenant_id
         AND scan.request_id=session.request_id
         AND scan.stream_session_id=session.stream_session_id
        WHERE session.tenant_id=? AND session.stream_session_id=?
        """,
        (bundle.session.tenant_id, bundle.session.stream_session_id),
    ).fetchone()
    connection.close()
    assert row is not None
    return tuple(row)


def test_active_completed_commits_session_and_clean_scan_together(
    atomic_fixture: _AtomicFixture,
) -> None:
    """主流程：active→completed 与 completed+clean scan 使用同一完成时刻原子提交。"""

    bundle = _create_bundle(atomic_fixture, "completed")
    mark_stream_session_active(atomic_fixture.config, bundle.session)
    bundle.session.note_chunk(b"data: clean\n\n")
    terminal = OutputSafetyTerminalState(
        scan_status="completed",
        decision="clean",
        checkpoint_count=1,
        observed_output_token_count=4,
        scanned_token_count=4,
        emitted_token_count=4,
        emitted_sse_event_count=1,
        emitted_bytes=bundle.session.bytes_sent,
        exposure_state="complete",
    )

    assert safe_finish_stream_session(
        atomic_fixture.config,
        bundle.session,
        status="completed",
        error_code=None,
        quality_observation_eligible=True,
        output_safety_scan=bundle.scan,
        output_safety_terminal=terminal,
    ) is True
    assert safe_finish_stream_session(
        atomic_fixture.config,
        bundle.session,
        status="completed",
        error_code=None,
        quality_observation_eligible=True,
        output_safety_scan=bundle.scan,
        output_safety_terminal=terminal,
    ) is True
    facts = _fact_counts(atomic_fixture, bundle)
    assert facts[0:2] == ("completed", None)
    # Session 保留链路微秒精度，scan/event 使用 Webhook 秒级规范时间；同一事务共享同一 UTC 秒。
    assert facts[2] is not None and str(facts[2])[:19] == str(facts[6])[:19]
    assert facts[3:6] == ("completed", "clean", None)
    assert facts[7] == 0


def test_active_output_blocked_commits_scan_and_unique_source_event(
    atomic_fixture: _AtomicFixture,
) -> None:
    """验证阻断终结会原子提交会话、扫描和唯一来源事件。

    重复调用必须保持同一事件身份及同一租户事实数量，避免重试产生重复审计记录。
    """

    bundle = _create_bundle(atomic_fixture, "blocked")
    mark_stream_session_active(atomic_fixture.config, bundle.session)
    bundle.session.note_chunk(b"data: safe-prefix\n\n")
    terminal = _blocked_terminal(bundle)

    assert safe_finish_stream_session(
        atomic_fixture.config,
        bundle.session,
        status="output_blocked",
        error_code="content_policy_violation",
        quality_observation_eligible=False,
        output_safety_scan=bundle.scan,
        output_safety_terminal=terminal,
    ) is True
    first_event = bundle.session.output_safety_event
    assert first_event is not None
    assert safe_finish_stream_session(
        atomic_fixture.config,
        bundle.session,
        status="output_blocked",
        error_code="content_policy_violation",
        quality_observation_eligible=False,
        output_safety_scan=bundle.scan,
        output_safety_terminal=terminal,
    ) is True
    assert bundle.session.output_safety_event == first_event
    facts = _fact_counts(atomic_fixture, bundle)
    assert facts[0:2] == ("output_blocked", "content_policy_violation")
    # 两类表采用不同时间精度，但必须来自同一 finalizer 时刻而保持 UTC 秒一致。
    assert facts[2] is not None and str(facts[2])[:19] == str(facts[6])[:19]
    assert facts[3:6] == ("completed", "blocked", None)
    assert facts[7] == 1


def test_active_output_safety_unavailable_commits_without_block_event(
    atomic_fixture: _AtomicFixture,
) -> None:
    """异常流程：active→output_safety_unavailable 与 unavailable scan 原子且不伪造事件。"""

    bundle = _create_bundle(atomic_fixture, "unavailable")
    mark_stream_session_active(atomic_fixture.config, bundle.session)
    bundle.session.note_chunk(b"data: prior-safe\n\n")
    terminal = OutputSafetyTerminalState(
        scan_status="unavailable",
        decision="unknown",
        checkpoint_count=1,
        observed_output_token_count=5,
        scanned_token_count=5,
        emitted_token_count=2,
        emitted_sse_event_count=1,
        emitted_bytes=bundle.session.bytes_sent,
        exposure_state="partial",
        failure_reason_code="output_safety.sse_json_invalid",
    )

    assert safe_finish_stream_session(
        atomic_fixture.config,
        bundle.session,
        status="output_safety_unavailable",
        error_code="gateway.output_safety_unavailable",
        quality_observation_eligible=False,
        output_safety_scan=bundle.scan,
        output_safety_terminal=terminal,
    ) is True
    facts = _fact_counts(atomic_fixture, bundle)
    assert facts[0:2] == (
        "output_safety_unavailable",
        "gateway.output_safety_unavailable",
    )
    # unavailable 不生成事件，Session 与 scan 仍应记录同一事务时刻的 UTC 秒。
    assert facts[2] is not None and str(facts[2])[:19] == str(facts[6])[:19]
    assert facts[3:6] == (
        "unavailable",
        "unknown",
        "output_safety.sse_json_invalid",
    )
    assert facts[7] == 0
    assert bundle.session.output_safety_event is None


def test_non_active_output_terminal_is_rejected_without_touching_pending_scan(
    atomic_fixture: _AtomicFixture,
) -> None:
    """状态边界：starting 会话不能伪造输出阻断，scan 必须保持 pending 且无事件。"""

    bundle = _create_bundle(atomic_fixture, "starting-rejected")
    bundle.session.note_chunk(b"data: should-not-count\n\n")

    assert safe_finish_stream_session(
        atomic_fixture.config,
        bundle.session,
        status="output_blocked",
        error_code="content_policy_violation",
        quality_observation_eligible=False,
        output_safety_scan=bundle.scan,
        output_safety_terminal=_blocked_terminal(bundle),
    ) is False
    assert _fact_counts(atomic_fixture, bundle) == (
        "starting",
        None,
        None,
        "pending",
        "none",
        None,
        None,
        0,
    )
    assert bundle.session.status == "starting"
    assert bundle.session.output_safety_event is None


def test_starting_handshake_failure_commits_not_applicable_scan_atomically(
    atomic_fixture: _AtomicFixture,
) -> None:
    """握手异常：Provider 无输出时 upstream_failed 与 not_applicable scan 必须同事务收口。"""

    bundle = _create_bundle(atomic_fixture, "handshake-not-applicable")
    terminal = OutputSafetyTerminalState.not_applicable()

    assert safe_finish_stream_session(
        atomic_fixture.config,
        bundle.session,
        status="upstream_failed",
        error_code="provider_key.unavailable",
        quality_observation_eligible=False,
        output_safety_scan=bundle.scan,
        output_safety_terminal=terminal,
    ) is True
    facts = _fact_counts(atomic_fixture, bundle)
    assert facts[0:2] == ("upstream_failed", "provider_key.unavailable")
    assert facts[2] is not None and str(facts[2])[:19] == str(facts[6])[:19]
    assert facts[3:6] == ("not_applicable", "none", None)
    assert facts[7] == 0
    assert bundle.session.chunk_count == 0
    assert bundle.session.bytes_sent == 0
    assert bundle.session.output_safety_event is None


def test_cross_tenant_scan_is_rejected_before_transaction(
    atomic_fixture: _AtomicFixture,
) -> None:
    """租户隔离：拼接其他 tenant 的 scan 句柄必须在写会话前失败且不产生半成品。"""

    bundle = _create_bundle(atomic_fixture, "cross-tenant")
    mark_stream_session_active(atomic_fixture.config, bundle.session)
    foreign_scan = replace(bundle.scan, tenant_id="tenant_prd071_foreign")

    with pytest.raises(ValueError, match="stream_session_output_safety_owner_invalid"):
        safe_finish_stream_session(
            atomic_fixture.config,
            bundle.session,
            status="output_blocked",
            error_code="content_policy_violation",
            quality_observation_eligible=False,
            output_safety_scan=foreign_scan,
            output_safety_terminal=_blocked_terminal(bundle),
        )
    assert _fact_counts(atomic_fixture, bundle) == (
        "active",
        None,
        None,
        "pending",
        "none",
        None,
        None,
        0,
    )


def test_source_event_failure_rolls_back_session_and_scan(
    atomic_fixture: _AtomicFixture,
) -> None:
    """事务异常：source event 插入失败必须回滚 session 与 scan，不能留下任一半成品。"""

    bundle = _create_bundle(atomic_fixture, "event-rollback")
    mark_stream_session_active(atomic_fixture.config, bundle.session)
    bundle.session.note_chunk(b"data: safe-prefix\n\n")
    connection = connect_database(atomic_fixture.database_url)
    connection.execute(
        """
        CREATE TRIGGER trg_prd071_test_event_failure
        BEFORE INSERT ON output_safety_events FOR EACH ROW
        BEGIN SELECT RAISE(ABORT,'prd071_test_event_failure'); END
        """
    )
    connection.commit()
    connection.close()
    try:
        assert safe_finish_stream_session(
            atomic_fixture.config,
            bundle.session,
            status="output_blocked",
            error_code="content_policy_violation",
            quality_observation_eligible=False,
            output_safety_scan=bundle.scan,
            output_safety_terminal=_blocked_terminal(bundle),
        ) is False
    finally:
        # 测试专用故障触发器无论断言结果如何都移除，避免污染同模块后续事实读取。
        connection = connect_database(atomic_fixture.database_url)
        connection.execute("DROP TRIGGER IF EXISTS trg_prd071_test_event_failure")
        connection.commit()
        connection.close()
    assert _fact_counts(atomic_fixture, bundle) == (
        "active",
        None,
        None,
        "pending",
        "none",
        None,
        None,
        0,
    )
    assert bundle.session.status == "active"
    assert bundle.session.output_safety_event is None
