"""PRD-071 Gateway 输出安全持久化专项测试。

测试只使用仓库 ``.tmp`` 下的独立 SQLite 文件，不启动 Provider 或网络服务。规则正文均为合成值，
并验证它们不会进入 scan、event、异常消息或结构化日志。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import metahub_gateway.output_safety.service as persistence
from metahub_gateway.output_safety import (
    OutputSafetyStoreError,
    OutputSafetyTerminalState,
    finalize_output_safety_scan,
    finalize_output_safety_scan_in_transaction,
    output_safety_event_to_webhook,
    prepare_output_safety_scan,
)
from metahub_shared.db import apply_migrations, apply_seed_data
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.errors import ApiError


def _database_url(path: Path) -> str:
    """把沙箱内独立测试文件转换成 Shared 连接层可识别的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_schema(path: Path) -> SimpleNamespace:
    """创建与 0063 服务字段合同一致的最小 SQLite schema。

    参数：pytest 提供的沙箱内临时路径。
    返回：只含 ``database_url`` 的配置替身。
    关键约束：复合 claim FK 和唯一键保留，用来验证 tenant-first owner 条件。
    """

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE gateway_request_claims(
          tenant_id TEXT NOT NULL,
          request_id TEXT NOT NULL,
          trace_id TEXT NOT NULL,
          PRIMARY KEY(tenant_id,request_id)
        );
        CREATE TABLE dlp_rules(
          dlp_rule_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          name TEXT NOT NULL,
          rule_type TEXT NOT NULL,
          pattern TEXT NOT NULL,
          pattern_hash TEXT NOT NULL,
          action TEXT NOT NULL,
          status TEXT NOT NULL,
          priority INTEGER NOT NULL,
          scan_scope TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE stream_sessions(
          stream_session_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          request_id TEXT NOT NULL,
          trace_id TEXT NOT NULL,
          UNIQUE(tenant_id,request_id,stream_session_id),
          FOREIGN KEY(tenant_id,request_id)
            REFERENCES gateway_request_claims(tenant_id,request_id)
        );
        CREATE TABLE output_safety_scans(
          output_safety_scan_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          request_id TEXT NOT NULL,
          trace_id TEXT NOT NULL,
          stream_session_id TEXT,
          scan_mode TEXT NOT NULL,
          scan_status TEXT NOT NULL,
          decision TEXT NOT NULL,
          action TEXT,
          dlp_rule_id TEXT,
          rule_type TEXT,
          match_count INTEGER NOT NULL,
          ruleset_fingerprint TEXT,
          detector_version TEXT NOT NULL,
          interval_tokens INTEGER,
          window_tokens INTEGER,
          checkpoint_count INTEGER NOT NULL,
          observed_output_token_count INTEGER NOT NULL,
          scanned_token_count INTEGER NOT NULL,
          emitted_token_count INTEGER NOT NULL,
          emitted_sse_event_count INTEGER NOT NULL,
          emitted_bytes INTEGER NOT NULL,
          exposure_state TEXT NOT NULL,
          billing_disposition TEXT NOT NULL,
          failure_reason_code TEXT,
          started_at TEXT NOT NULL,
          completed_at TEXT,
          UNIQUE(tenant_id,request_id),
          UNIQUE(tenant_id,output_safety_scan_id),
          FOREIGN KEY(tenant_id,request_id)
            REFERENCES gateway_request_claims(tenant_id,request_id),
          FOREIGN KEY(tenant_id,request_id,stream_session_id)
            REFERENCES stream_sessions(tenant_id,request_id,stream_session_id)
        );
        CREATE TABLE output_safety_events(
          event_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          output_safety_scan_id TEXT NOT NULL,
          request_id TEXT NOT NULL,
          trace_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          schema_version INTEGER NOT NULL,
          scan_mode TEXT NOT NULL,
          scan_status TEXT NOT NULL,
          decision TEXT NOT NULL,
          error_code TEXT NOT NULL,
          exposure_state TEXT NOT NULL,
          emitted_token_count INTEGER NOT NULL,
          checkpoint_count INTEGER NOT NULL,
          dlp_rule_id TEXT NOT NULL,
          detector_version TEXT NOT NULL,
          billing_disposition TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          UNIQUE(tenant_id,output_safety_scan_id),
          FOREIGN KEY(tenant_id,output_safety_scan_id)
            REFERENCES output_safety_scans(tenant_id,output_safety_scan_id)
        );
        """
    )
    connection.commit()
    connection.close()
    return SimpleNamespace(database_url=_database_url(path))


def _claim(config: SimpleNamespace, tenant_id: str, request_id: str, trace_id: str) -> None:
    """插入已鉴权且已提交的最小 Gateway request claim。"""

    connection = connect_database(config.database_url)
    connection.execute(
        "INSERT INTO gateway_request_claims(tenant_id,request_id,trace_id) VALUES(?,?,?)",
        (tenant_id, request_id, trace_id),
    )
    connection.commit()
    connection.close()


def _rule(
    config: SimpleNamespace,
    *,
    tenant_id: str,
    rule_id: str,
    pattern: str,
    action: str,
    scan_scope: str,
    priority: int,
    updated_at: str,
    rule_type: str = "keyword",
) -> None:
    """插入合成 DLP 规则，并按控制面算法生成一致的 pattern_hash。"""

    digest = hashlib.sha256(f"{rule_type}:{pattern}".encode("utf-8")).hexdigest()
    connection = connect_database(config.database_url)
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,action,status,
          priority,scan_scope,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,'active',?,?,?,?)
        """,
        (
            rule_id,
            tenant_id,
            rule_id,
            rule_type,
            pattern,
            f"sha256:{digest}",
            action,
            priority,
            scan_scope,
            updated_at,
            updated_at,
        ),
    )
    connection.commit()
    connection.close()


def _stream_session(
    config: SimpleNamespace,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    stream_session_id: str,
) -> None:
    """插入已经提交的 starting StreamSession owner，供流式 pending scan 复合 FK 引用。"""

    connection = connect_database(config.database_url)
    connection.execute(
        """
        INSERT INTO stream_sessions(
          stream_session_id,tenant_id,request_id,trace_id
        ) VALUES(?,?,?,?)
        """,
        (stream_session_id, tenant_id, request_id, trace_id),
    )
    connection.commit()
    connection.close()


def _row(config: SimpleNamespace, query: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
    """读取一条 tenant-qualified 测试事实并立即释放连接。"""

    connection = connect_database(config.database_url)
    row = connection.execute(query, params).fetchone()
    connection.close()
    assert row is not None
    return row


def test_prepare_filters_tenant_and_scope_then_persists_clean_terminal(tmp_path: Path) -> None:
    """主流程：只冻结同租户 output/both 规则，并把 clean 终态写入唯一 owner。"""

    config = _prepare_schema(tmp_path / "output-safety-main.db")
    _claim(config, "tenant_a", "req_main", "trc_main")
    _rule(
        config,
        tenant_id="tenant_a",
        rule_id="dlp_input_only",
        pattern="input-only-secret",
        action="block",
        scan_scope="input",
        priority=1,
        updated_at="2026-08-04T01:00:00Z",
    )
    _rule(
        config,
        tenant_id="tenant_a",
        rule_id="dlp_both_old",
        pattern="both-secret",
        action="log_only",
        scan_scope="both",
        priority=10,
        updated_at="2026-08-04T01:00:00Z",
    )
    _rule(
        config,
        tenant_id="tenant_a",
        rule_id="dlp_output_new",
        pattern="output-secret",
        action="block",
        scan_scope="output",
        priority=10,
        updated_at="2026-08-04T02:00:00Z",
    )
    _rule(
        config,
        tenant_id="tenant_b",
        rule_id="dlp_cross_tenant",
        pattern="cross-tenant-secret",
        action="block",
        scan_scope="output",
        priority=1,
        updated_at="2026-08-04T03:00:00Z",
    )

    prepared = prepare_output_safety_scan(
        config,
        tenant_id="tenant_a",
        request_id="req_main",
        trace_id="trc_main",
        scan_mode="non_stream_full",
    )

    assert prepared.configured is True
    assert [rule.dlp_rule_id for rule in prepared.snapshot.rules] == [
        "dlp_output_new",
        "dlp_both_old",
    ]
    assert len(prepared.snapshot.ruleset_fingerprint) == 64
    assert prepared.snapshot.ruleset_fingerprint.islower()
    assert "output-secret" not in repr(prepared)
    assert "both-secret" not in repr(prepared)
    pending = _row(
        config,
        "SELECT scan_status,ruleset_fingerprint FROM output_safety_scans WHERE tenant_id=?",
        ("tenant_a",),
    )
    assert tuple(pending) == ("pending", None)

    terminal = OutputSafetyTerminalState(
        scan_status="completed",
        decision="clean",
        checkpoint_count=1,
        observed_output_token_count=5,
        scanned_token_count=5,
        emitted_token_count=5,
        exposure_state="complete",
    )
    result = finalize_output_safety_scan(config, prepared, terminal)

    assert result.idempotent_replay is False
    assert result.source_event is None
    completed = _row(
        config,
        """
        SELECT scan_status,decision,ruleset_fingerprint,detector_version,
               observed_output_token_count,scanned_token_count,emitted_token_count,
               exposure_state,billing_disposition
        FROM output_safety_scans WHERE tenant_id=? AND request_id=?
        """,
        ("tenant_a", "req_main"),
    )
    assert tuple(completed) == (
        "completed",
        "clean",
        prepared.snapshot.ruleset_fingerprint,
        "output-safety-dlp-v1",
        5,
        5,
        5,
        "complete",
        "not_required",
    )


def test_empty_output_rules_are_explicit_not_configured(tmp_path: Path) -> None:
    """兼容流程：历史 input-only 规则不得应用到输出，且必须留下 not_configured 事实。"""

    config = _prepare_schema(tmp_path / "output-safety-empty.db")
    _claim(config, "tenant_empty", "req_empty", "trc_empty")
    _rule(
        config,
        tenant_id="tenant_empty",
        rule_id="dlp_historical_input",
        pattern="historical-input-secret",
        action="block",
        scan_scope="input",
        priority=10,
        updated_at="2026-08-04T01:00:00Z",
    )

    prepared = prepare_output_safety_scan(
        config,
        tenant_id="tenant_empty",
        request_id="req_empty",
        trace_id="trc_empty",
        scan_mode="non_stream_full",
    )

    assert prepared.configured is False
    assert prepared.scan_status == "not_configured"
    assert prepared.snapshot.rules == ()
    fact = _row(
        config,
        """
        SELECT scan_status,decision,action,dlp_rule_id,match_count,
               length(ruleset_fingerprint),exposure_state,billing_disposition,
               failure_reason_code
        FROM output_safety_scans WHERE tenant_id=? AND request_id=?
        """,
        ("tenant_empty", "req_empty"),
    )
    assert tuple(fact) == (
        "not_configured",
        "none",
        None,
        None,
        0,
        64,
        "unknown",
        "not_required",
        None,
    )


def test_broken_regex_finalizes_pending_as_unavailable_without_plaintext(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """异常流程：规则快照损坏时 Provider 前失败关闭，并且日志/scan 不回显 pattern。"""

    config = _prepare_schema(tmp_path / "output-safety-bad-regex.db")
    _claim(config, "tenant_bad", "req_bad", "trc_bad")
    sensitive_pattern = "(sensitive-secret+)+"
    _rule(
        config,
        tenant_id="tenant_bad",
        rule_id="dlp_bad_regex",
        pattern=sensitive_pattern,
        action="block",
        scan_scope="output",
        priority=10,
        updated_at="2026-08-04T01:00:00Z",
        rule_type="regex",
    )

    with pytest.raises(ApiError) as raised:
        prepare_output_safety_scan(
            config,
            tenant_id="tenant_bad",
            request_id="req_bad",
            trace_id="trc_bad",
            scan_mode="non_stream_full",
        )

    assert raised.value.code == "gateway.output_safety_unavailable"
    assert sensitive_pattern not in str(raised.value)
    assert sensitive_pattern not in caplog.text
    fact = _row(
        config,
        """
        SELECT scan_status,decision,ruleset_fingerprint,failure_reason_code,
               observed_output_token_count,emitted_token_count
        FROM output_safety_scans WHERE tenant_id=? AND request_id=?
        """,
        ("tenant_bad", "req_bad"),
    )
    assert tuple(fact) == (
        "unavailable",
        "unknown",
        None,
        "output_safety.rule_snapshot_unavailable",
        0,
        0,
    )
    connection = connect_database(config.database_url)
    scan_dump = "\n".join(
        str(value)
        for row in connection.execute("SELECT * FROM output_safety_scans").fetchall()
        for value in tuple(row)
    )
    connection.close()
    assert sensitive_pattern not in scan_dump


def test_blocked_terminal_is_idempotent_and_creates_one_exact_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """阻断流程：合法单次终结写唯一 source event，相同重放成功而不同终态失败。"""

    config = _prepare_schema(tmp_path / "output-safety-block.db")
    _claim(config, "tenant_block", "req_block", "trc_block")
    pattern = "blocked-output-secret"
    _rule(
        config,
        tenant_id="tenant_block",
        rule_id="dlp_output_block",
        pattern=pattern,
        action="block",
        scan_scope="both",
        priority=1,
        updated_at="2026-08-04T01:00:00Z",
    )
    prepared = prepare_output_safety_scan(
        config,
        tenant_id="tenant_block",
        request_id="req_block",
        trace_id="trc_block",
        scan_mode="non_stream_full",
    )
    terminal = OutputSafetyTerminalState(
        scan_status="completed",
        decision="blocked",
        action="block",
        dlp_rule_id="dlp_output_block",
        rule_type="keyword",
        match_count=1,
        checkpoint_count=1,
        observed_output_token_count=8,
        scanned_token_count=8,
        exposure_state="none",
        billing_disposition="dispute_pending",
    )
    monkeypatch.setattr(
        persistence,
        "_utcnow",
        lambda: datetime(2026, 8, 4, 5, 0, tzinfo=UTC),
    )
    first = finalize_output_safety_scan(config, prepared, terminal)
    monkeypatch.setattr(
        persistence,
        "_utcnow",
        lambda: datetime(2026, 8, 4, 5, 1, tzinfo=UTC),
    )
    replay = finalize_output_safety_scan(config, prepared, terminal)

    assert first.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert first.source_event == replay.source_event
    assert first.source_event is not None
    assert first.source_event.event_id.startswith("osevt_")
    assert first.source_event.error_code == "content_policy_violation"
    assert first.source_event.occurred_at == "2026-08-04T05:00:00Z"
    with pytest.raises(ApiError) as conflict:
        finalize_output_safety_scan(
            config,
            prepared,
            OutputSafetyTerminalState.unavailable(
                "output_safety.stream_incomplete",
                exposure_state="none",
            ),
        )
    assert conflict.value.code == "gateway.output_safety_unavailable"

    event = _row(
        config,
        """
        SELECT event_type,schema_version,scan_mode,scan_status,decision,error_code,
               exposure_state,emitted_token_count,checkpoint_count,dlp_rule_id,
               detector_version,billing_disposition,occurred_at
        FROM output_safety_events WHERE tenant_id=? AND request_id=?
        """,
        ("tenant_block", "req_block"),
    )
    assert tuple(event) == (
        "security.output_blocked",
        1,
        "non_stream_full",
        "completed",
        "blocked",
        "content_policy_violation",
        "none",
        0,
        1,
        "dlp_output_block",
        "output-safety-dlp-v1",
        "dispute_pending",
        "2026-08-04T05:00:00Z",
    )
    connection = connect_database(config.database_url)
    assert connection.execute("SELECT COUNT(*) FROM output_safety_events").fetchone()[0] == 1
    safe_dump = "\n".join(
        str(value)
        for table in ("output_safety_scans", "output_safety_events")
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
    )
    connection.close()
    assert pattern not in safe_dump


def test_transaction_variant_never_commits_and_rejects_cross_tenant_snapshot(tmp_path: Path) -> None:
    """事务/权限流程：StreamSession 接口不自行提交，且篡改 tenant 不能更新真实 owner。"""

    config = _prepare_schema(tmp_path / "output-safety-transaction.db")
    _claim(config, "tenant_owner", "req_transaction", "trc_transaction")
    _rule(
        config,
        tenant_id="tenant_owner",
        rule_id="dlp_transaction",
        pattern="transaction-secret",
        action="log_only",
        scan_scope="output",
        priority=1,
        updated_at="2026-08-04T01:00:00Z",
    )
    with pytest.raises(ApiError) as missing_session:
        prepare_output_safety_scan(
            config,
            tenant_id="tenant_owner",
            request_id="req_transaction",
            trace_id="trc_transaction",
            scan_mode="stream_window",
            stream_session_id="str_missing",
        )
    assert missing_session.value.code == "gateway.output_safety_unavailable"
    _stream_session(
        config,
        tenant_id="tenant_owner",
        request_id="req_transaction",
        trace_id="trc_transaction",
        stream_session_id="str_transaction",
    )
    prepared = prepare_output_safety_scan(
        config,
        tenant_id="tenant_owner",
        request_id="req_transaction",
        trace_id="trc_transaction",
        scan_mode="stream_window",
        stream_session_id="str_transaction",
    )
    terminal = OutputSafetyTerminalState(
        scan_status="completed",
        decision="clean",
        checkpoint_count=2,
        observed_output_token_count=20,
        scanned_token_count=20,
        emitted_token_count=20,
        emitted_sse_event_count=3,
        emitted_bytes=180,
        exposure_state="complete",
    )
    connection = connect_database(config.database_url)
    connection.execute("BEGIN IMMEDIATE")
    result = finalize_output_safety_scan_in_transaction(
        connection,
        prepared,
        terminal,
        completed_at=datetime(2026, 8, 4, 6, 0, tzinfo=UTC),
    )
    assert result.idempotent_replay is False
    assert connection.execute(
        "SELECT scan_status FROM output_safety_scans WHERE output_safety_scan_id=?",
        (prepared.output_safety_scan_id,),
    ).fetchone()[0] == "completed"
    connection.rollback()
    connection.close()
    persisted = _row(
        config,
        "SELECT scan_status FROM output_safety_scans WHERE output_safety_scan_id=?",
        (prepared.output_safety_scan_id,),
    )
    assert persisted["scan_status"] == "pending"

    connection = connect_database(config.database_url)
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(OutputSafetyStoreError) as owner_error:
        finalize_output_safety_scan_in_transaction(
            connection,
            replace(prepared, tenant_id="tenant_other"),
            terminal,
        )
    connection.rollback()
    connection.close()
    assert owner_error.value.reason_code == "output_safety.snapshot_owner_mismatch"
    assert _row(
        config,
        "SELECT scan_status FROM output_safety_scans WHERE tenant_id=? AND request_id=?",
        ("tenant_owner", "req_transaction"),
    )["scan_status"] == "pending"


def test_real_0063_schema_accepts_pending_block_and_exact_source_event(tmp_path: Path) -> None:
    """迁移烟测：真实 1→63 schema 接受服务的 pending、blocked 和唯一 source event 合同。"""

    database_url = _database_url(tmp_path / "output-safety-real-migrations.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = SimpleNamespace(database_url=database_url)
    rule_id = "dlp_" + "a" * 32
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
          policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,NULL,NULL,'disabled',?)
        """,
        (
            seed.tenant_id,
            "req_real_0063",
            "trc_real_0063",
            "2026-08-04T08:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    _rule(
        config,
        tenant_id=seed.tenant_id,
        rule_id=rule_id,
        pattern="real-migration-secret",
        action="block",
        scan_scope="output",
        priority=1,
        updated_at="2026-08-04T08:00:00Z",
    )

    prepared = prepare_output_safety_scan(
        config,
        tenant_id=seed.tenant_id,
        request_id="req_real_0063",
        trace_id="trc_real_0063",
        scan_mode="non_stream_full",
    )
    finalized = finalize_output_safety_scan(
        config,
        prepared,
        OutputSafetyTerminalState(
            scan_status="completed",
            decision="blocked",
            action="block",
            dlp_rule_id=rule_id,
            rule_type="keyword",
            match_count=1,
            checkpoint_count=1,
            observed_output_token_count=4,
            scanned_token_count=4,
            exposure_state="none",
            billing_disposition="dispute_pending",
        ),
    )

    assert finalized.source_event is not None
    webhook = output_safety_event_to_webhook(finalized.source_event)
    assert webhook.event_id == finalized.source_event.event_id
    assert webhook.object_id == prepared.output_safety_scan_id
    assert webhook.actor_id is None
    assert webhook.trace_id == "trc_real_0063"
    assert webhook.payload["dlp_rule_id"] == rule_id
    assert "reversal_eligible" not in webhook.payload
    fact = _row(
        config,
        """
        SELECT scan.scan_status,scan.decision,scan.billing_disposition,event.event_type,
               event.error_code,event.occurred_at=scan.completed_at AS same_time
        FROM output_safety_scans scan
        JOIN output_safety_events event
          ON event.tenant_id=scan.tenant_id
         AND event.output_safety_scan_id=scan.output_safety_scan_id
        WHERE scan.tenant_id=? AND scan.request_id=?
        """,
        (seed.tenant_id, "req_real_0063"),
    )
    assert tuple(fact) == (
        "completed",
        "blocked",
        "dispute_pending",
        "security.output_blocked",
        "content_policy_violation",
        1,
    )
