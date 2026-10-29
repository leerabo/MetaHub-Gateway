"""PRD-057 0047 死信迁移与 0048 状态一致性加固专项测试。

测试只使用 pytest 独占 SQLite 文件和保留域占位信封，不访问网络，也不写入 Prompt、
Response、Token、Secret 或接收端正文；所有临时文件由调用方的 D:\\MetaHub\\.tmp basetemp 承载。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import metahub_shared.db.migrate as migration_module
import pytest
from metahub_shared.db import (
    apply_migrations as apply_current_migrations,
)
from metahub_shared.db import (
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

# 正式迁移版本序列用于证明空库升级未跳号，并锁定 PRD-057 的 0047/0048 位置。

EXPECTED_VERSIONS = [f"{version:04d}" for version in range(1, 49)]
"""空库必须按顺序登记的 48 个正式迁移版本。"""

# 历史专项必须真实执行新迁移再回退，不能复制旧迁移目录规避向前兼容检查。
FROZEN_VERSION = "0048"
"""PRD-057 历史断言必须恢复到的迁移栈顶。"""

# 固定 UTC 夹具时间保证升级、过期和 seek 排序断言不依赖测试机器当前时钟。
NOW = "2026-07-01T00:00:00Z"
"""迁移夹具使用的固定 UTC 时间，避免真实时钟导致排序和过期断言漂移。"""


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库文件转换为项目迁移器支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def apply_migrations(database_url: str) -> migration_module.MigrationResult:
    """真实执行当前完整迁移链，再回退全部后续版本以冻结 0048 栈顶。"""

    result = apply_current_migrations(database_url)
    # 根据实际结果生成逆序栈，不让新增迁移把历史专项的第一步回滚留在旧版本。
    later_migrations = tuple(
        sorted(
            {
                version
                for version in (*result.applied, *result.skipped)
                if version > FROZEN_VERSION
            },
            reverse=True,
        )
    )
    for version in later_migrations:
        rolled_back = rollback_last_migration(database_url)
        assert rolled_back.rolled_back == version
    excluded = set(later_migrations)
    return migration_module.MigrationResult(
        run_id=result.run_id,
        applied=[version for version in result.applied if version not in excluded],
        skipped=[version for version in result.skipped if version not in excluded],
        rolled_back=result.rolled_back,
    )


def _database_at_0046(path: Path) -> str:
    """先验证空库能到 0048，再依次回滚 0048/0047，供 0046 历史升级夹具写入。"""

    database_url = _sqlite_url(path)
    applied = apply_migrations(database_url)
    assert applied.applied == EXPECTED_VERSIONS
    hardening_rollback = rollback_last_migration(database_url)
    assert hardening_rollback.rolled_back == "0048"
    dead_letter_rollback = rollback_last_migration(database_url)
    assert dead_letter_rollback.rolled_back == "0047"
    return database_url


def _seed_webhook_owners(connection: sqlite3.Connection) -> None:
    """写入单租户 admin 与 Endpoint owner，所有 replay 只能引用该租户资源。"""

    connection.execute(
        "INSERT INTO tenants VALUES(?,?,?,?,?,?,?)",
        ("tenant_prd057", "tenant-prd057", "PRD057", "internal_only", "active", NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO users(user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            "usr_prd057",
            "tenant_prd057",
            "prd057@example.com",
            "PRD057 Admin",
            "test-hash",
            "admin",
            "active",
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,encrypted_secret,
          secret_prefix,secret_fingerprint,status,timeout_ms,max_attempts,created_by,updated_by,
          created_at,updated_at,secret_version
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "wh_prd057",
            "tenant_prd057",
            "PRD057 Receiver",
            "https://example.invalid/webhook",
            "[]",
            "test-ciphertext",
            "whsec_test",
            "test-fingerprint",
            "active",
            1000,
            3,
            "usr_prd057",
            "usr_prd057",
            NOW,
            NOW,
            1,
        ),
    )


def _insert_delivery(
    connection: sqlite3.Connection,
    index: int,
    *,
    kind: str,
    status: str,
    source_delivery_id: str | None = None,
) -> str:
    """写入 original 或 replay 历史行，replay 信封复用来源 event_id 以冻结幂等合同。"""

    delivery_id = f"whd_{index:032x}"
    event_index = index if source_delivery_id is None else int(source_delivery_id[4:], 16)
    created_at = f"2026-07-{index:02d}T00:00:00Z"
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,event_timestamp,
          payload_json,status,attempt_count,max_attempts,http_status,error_code,error_summary,
          delivery_kind,replayed_from_delivery_id,replay_idempotency_key,replayed_by,replayed_at,
          secret_version,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            delivery_id,
            "tenant_prd057",
            "wh_prd057",
            f"evt_prd057_{event_index}",
            "budget.threshold_reached",
            created_at,
            "{}",
            status,
            0 if status == "pending" else 1,
            3,
            500 if status == "failed" else (204 if status == "success" else None),
            "webhook.http_error" if status == "failed" else None,
            "Receiver failed." if status == "failed" else None,
            kind,
            source_delivery_id,
            f"replay_key_{index}" if source_delivery_id else None,
            "usr_prd057" if source_delivery_id else None,
            created_at if source_delivery_id else None,
            1,
            created_at,
            created_at,
        ),
    )
    return delivery_id


def _insert_legacy_audit(connection: sqlite3.Connection) -> tuple[object, ...]:
    """插入含 NULL 的 0046 审计行，验证 0047 重建不会改变 rowid 或列值。"""

    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "aud_prd057_legacy",
            "tenant_prd057",
            "usr_prd057",
            "prd057@example.com",
            "admin",
            "provider.updated",
            "provider",
            "provider_prd057_placeholder",
            None,
            "{}",
            '{"status":"active"}',
            '["status"]',
            None,
            "prd057-test-agent",
            "req_prd057_legacy",
            "trc_prd057_legacy",
            NOW,
        ),
    )
    row = connection.execute(
        "SELECT rowid,* FROM audit_logs WHERE audit_log_id='aud_prd057_legacy'"
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_prd057_fresh_up_down_up_preserves_schema_contract(tmp_path: Path) -> None:
    """空库 0001→0048、逐步回滚 0048/0047 与重新应用均保持 schema 合同。"""

    database_url = _sqlite_url(tmp_path / "prd057-fresh.db")
    first = apply_migrations(database_url)
    assert first.applied == EXPECTED_VERSIONS

    connection = connect_database(database_url)
    try:
        objects = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name LIKE '%dead_letter%_prd057' OR name='webhook_dead_letter_events'"
            ).fetchall()
        }
        assert "webhook_dead_letter_events" in objects
        assert "trg_webhook_dead_letters_delivery_failed_update_prd057" in objects
        assert "idx_webhook_dead_letters_replay_due_prd057" in objects
        audit_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'"
        ).fetchone()["sql"]
        assert "webhook_dead_letter.archived" in audit_sql
        assert "webhook_dead_letter" in audit_sql
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    hardening_rollback = rollback_last_migration(database_url)
    assert hardening_rollback.rolled_back == "0048"
    dead_letter_rollback = rollback_last_migration(database_url)
    assert dead_letter_rollback.rolled_back == "0047"
    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='webhook_dead_letter_events'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0047'"
        ).fetchone()["status"] == "rolled_back"
    finally:
        connection.close()

    reapplied = apply_migrations(database_url)
    assert reapplied.applied == ["0047", "0048"]


def test_prd057_history_mapping_root_identity_and_down_guard(tmp_path: Path) -> None:
    """历史 replay 被接管到根死信，失败不建子链，且有治理事实时回滚原子拒绝。"""

    database_url = _database_at_0046(tmp_path / "prd057-history.db")
    connection = connect_database(database_url)
    try:
        _seed_webhook_owners(connection)
        source_pending = _insert_delivery(connection, 1, kind="original", status="failed")
        source_success = _insert_delivery(connection, 2, kind="original", status="failed")
        source_failed = _insert_delivery(connection, 3, kind="original", status="failed")
        source_none = _insert_delivery(connection, 4, kind="original", status="failed")
        replay_pending = _insert_delivery(
            connection, 11, kind="replay", status="pending", source_delivery_id=source_pending
        )
        replay_success = _insert_delivery(
            connection, 12, kind="replay", status="success", source_delivery_id=source_success
        )
        replay_failed = _insert_delivery(
            connection, 13, kind="replay", status="failed", source_delivery_id=source_failed
        )
        legacy_audit = _insert_legacy_audit(connection)
        connection.commit()
    finally:
        connection.close()

    upgraded = apply_migrations(database_url)
    assert upgraded.applied == ["0047", "0048"]
    connection = connect_database(database_url)
    try:
        rows = connection.execute(
            """
            SELECT webhook_delivery_id,status,current_replay_delivery_id,replay_next_attempt_at,
                   last_replay_error_code,replayed_at
            FROM webhook_dead_letter_events ORDER BY webhook_delivery_id
            """
        ).fetchall()
        mapped = {str(row["webhook_delivery_id"]): row for row in rows}
        assert mapped[source_pending]["status"] == "replay_pending"
        assert mapped[source_pending]["current_replay_delivery_id"] == replay_pending
        assert mapped[source_pending]["replay_next_attempt_at"] is not None
        assert mapped[source_success]["status"] == "replayed"
        assert mapped[source_success]["current_replay_delivery_id"] == replay_success
        assert mapped[source_success]["replayed_at"] is not None
        assert mapped[source_failed]["status"] == "dead_letter"
        assert mapped[source_failed]["current_replay_delivery_id"] == replay_failed
        assert mapped[source_failed]["last_replay_error_code"] == "webhook.http_error"
        assert mapped[source_none]["status"] == "dead_letter"
        assert mapped[source_none]["current_replay_delivery_id"] is None

        copied_audit = connection.execute(
            "SELECT rowid,* FROM audit_logs WHERE audit_log_id='aud_prd057_legacy'"
        ).fetchone()
        assert copied_audit is not None and tuple(copied_audit) == legacy_audit

        # replayed→expired 必须保留最近 replay 与成功时间，过期只撤销重放资格。
        connection.execute(
            "UPDATE webhook_dead_letter_events SET status='expired',updated_at=? WHERE webhook_delivery_id=?",
            ("2026-08-10T00:00:00Z", source_success),
        )
        expired = connection.execute(
            "SELECT status,current_replay_delivery_id,replayed_at FROM webhook_dead_letter_events WHERE webhook_delivery_id=?",
            (source_success,),
        ).fetchone()
        assert expired is not None
        assert tuple(expired) == ("expired", replay_success, mapped[source_success]["replayed_at"])

        # replay delivery 失败只由 worker 回写根状态；0047 trigger 不得创建第二个死信对象。
        before_count = connection.execute("SELECT COUNT(*) FROM webhook_dead_letter_events").fetchone()[0]
        connection.execute(
            "UPDATE webhook_deliveries SET status='failed',error_code='webhook.http_error',updated_at=? WHERE webhook_delivery_id=?",
            ("2026-07-20T00:00:00Z", replay_pending),
        )
        connection.commit()
        after_count = connection.execute("SELECT COUNT(*) FROM webhook_dead_letter_events").fetchone()[0]
        assert before_count == after_count == 4

        # 重复根状态转换应暴露唯一约束错误，禁止 OR IGNORE 造成 failed delivery 无死信。
        connection.execute(
            "UPDATE webhook_deliveries SET status='pending' WHERE webhook_delivery_id=?",
            (source_none,),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE webhook_deliveries SET status='failed' WHERE webhook_delivery_id=?",
                (source_none,),
            )
        connection.rollback()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    # 任何死信生命周期事实存在时 down 必须原子拒绝，不能为降级静默删除证据。
    hardening_rollback = rollback_last_migration(database_url)
    assert hardening_rollback.rolled_back == "0048"

    with pytest.raises(DbError) as error:
        rollback_last_migration(database_url)
    assert error.value.code == "db.migration_failed"
    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0047'"
        ).fetchone()["status"] == "applied"
        assert connection.execute(
            "SELECT COUNT(*) FROM webhook_dead_letter_events"
        ).fetchone()[0] == 4
    finally:
        connection.close()


def test_prd057_replay_state_relation_rejects_mismatched_terminal_root(
    tmp_path: Path,
) -> None:
    """0048 拒绝稳定终态错配，同时允许崩溃窗口中的 terminal replay 被重新接管。"""

    database_url = _sqlite_url(tmp_path / "prd057-state-relation.db")
    applied = apply_migrations(database_url)
    assert applied.applied == EXPECTED_VERSIONS
    terminal_at = "2026-07-20T00:00:00Z"
    connection = connect_database(database_url)
    try:
        _seed_webhook_owners(connection)
        source_delivery_id = _insert_delivery(
            connection,
            5,
            kind="original",
            status="failed",
        )
        replay_delivery_id = _insert_delivery(
            connection,
            15,
            kind="replay",
            status="pending",
            source_delivery_id=source_delivery_id,
        )
        replay_requested_at = connection.execute(
            "SELECT replayed_at FROM webhook_deliveries WHERE webhook_delivery_id=?",
            (replay_delivery_id,),
        ).fetchone()["replayed_at"]
        # 先建立合法 pending 关系，证明 0048 没有阻断正常 replay intent。
        connection.execute(
            """
            UPDATE webhook_dead_letter_events
            SET status='replay_pending',current_replay_delivery_id=?,replay_requested_by=?,
                replay_requested_at=?,replay_next_attempt_at=?,updated_at=?
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (
                replay_delivery_id,
                "usr_prd057",
                replay_requested_at,
                replay_requested_at,
                replay_requested_at,
                "tenant_prd057",
                source_delivery_id,
            ),
        )
        connection.commit()

        # pending delivery 不能把根状态伪造为 replayed；失败必须保持事务前事实。
        with pytest.raises(sqlite3.IntegrityError, match="webhook_dead_letter_owner_mismatch"):
            connection.execute(
                """
                UPDATE webhook_dead_letter_events
                SET status='replayed',replayed_at=?,updated_at=?
                WHERE tenant_id=? AND webhook_delivery_id=?
                """,
                (terminal_at, terminal_at, "tenant_prd057", source_delivery_id),
            )
        connection.rollback()

        connection.execute(
            """
            UPDATE webhook_deliveries
            SET status='success',attempt_count=1,http_status=204,error_code=NULL,
                error_summary=NULL,updated_at=?
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (terminal_at, "tenant_prd057", replay_delivery_id),
        )

        # delivery 已终态而根仍 pending 是远端成功、本地根提交前崩溃的合法窗口；
        # scheduler 必须仍能写入并清除新 lease，随后才能把根状态收敛为 replayed。
        claim_expires_at = "2026-07-20T00:01:00Z"
        connection.execute(
            """
            UPDATE webhook_dead_letter_events
            SET replay_claim_token='claim_prd057_terminal',
                replay_lease_owner_id='worker-prd057-terminal',
                replay_lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (claim_expires_at, terminal_at, "tenant_prd057", source_delivery_id),
        )
        leased = connection.execute(
            """
            SELECT replay_claim_token,replay_lease_owner_id,replay_lease_expires_at
            FROM webhook_dead_letter_events
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            ("tenant_prd057", source_delivery_id),
        ).fetchone()
        assert leased is not None and tuple(leased) == (
            "claim_prd057_terminal",
            "worker-prd057-terminal",
            claim_expires_at,
        )
        connection.execute(
            """
            UPDATE webhook_dead_letter_events
            SET replay_claim_token=NULL,replay_lease_owner_id=NULL,replay_lease_expires_at=NULL,
                updated_at=?
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (terminal_at, "tenant_prd057", source_delivery_id),
        )
        # success delivery 也不能把根状态伪造为 dead_letter；只能推进为 replayed。
        with pytest.raises(sqlite3.IntegrityError, match="webhook_dead_letter_owner_mismatch"):
            connection.execute(
                """
                UPDATE webhook_dead_letter_events
                SET status='dead_letter',last_replay_error_code='webhook.http_error',updated_at=?
                WHERE tenant_id=? AND webhook_delivery_id=?
                """,
                (terminal_at, "tenant_prd057", source_delivery_id),
            )
        connection.rollback()

        # rollback 撤销了同一事务内的 delivery 终态，重新提交 success 后再推进合法根终态。
        connection.execute(
            """
            UPDATE webhook_deliveries
            SET status='success',attempt_count=1,http_status=204,error_code=NULL,
                error_summary=NULL,updated_at=?
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (terminal_at, "tenant_prd057", replay_delivery_id),
        )
        connection.execute(
            """
            UPDATE webhook_dead_letter_events
            SET status='replayed',last_replay_error_code=NULL,replayed_at=?,updated_at=?
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (terminal_at, terminal_at, "tenant_prd057", source_delivery_id),
        )
        connection.commit()
        relation = connection.execute(
            """
            SELECT dead.status,replay.status
            FROM webhook_dead_letter_events dead
            JOIN webhook_deliveries replay
              ON replay.tenant_id=dead.tenant_id
             AND replay.webhook_delivery_id=dead.current_replay_delivery_id
            WHERE dead.tenant_id=? AND dead.webhook_delivery_id=?
            """,
            ("tenant_prd057", source_delivery_id),
        ).fetchone()
        assert relation is not None and tuple(relation) == ("replayed", "success")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
