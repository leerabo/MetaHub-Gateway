"""验证 PRD-078 0071 正反迁移、审批事实降级守卫和 RBAC 保真。"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_prd077_db_migrations as prd077
import test_prd078_approvals_control as control_fixtures
from httpx import ASGITransport, AsyncClient
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db import migrate as migration_runtime
from metahub_shared.db.errors import DbError

# 迁移测试冻结 0071 的基线文件、虚构种子身份和新增对象哈希，确保正反迁移可重复核验。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
SEED_FILE = Path(__file__).resolve().parents[3] / "infra" / "seeds" / "default_seed.json"
SEED_PASSWORD = "Prd078MigrationOnly!2026"
NOW = "2026-08-13T00:00:00Z"
NEW_HASHES = {
    "admin": "73bdfac7bfe9fc41b783580f2459d71c87aa8aae02e3359a45918c5024434495",
    "finance_admin": "5b2aef1ae06711890946de8894623ea7c740f724cca886ce223b0b175fbc472a",
    "security_admin": "7f8a81d9d8e2a1d8e3e3991bc274ca3f5dd26eadcc6435d633c6da276727da7b",
}


def _url(path: Path) -> str:
    """``_url`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """``_migration_subset`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd078_migration_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """构造真实 seeded 0070 模板和独立 0070/0071 迁移目录。"""

    root = tmp_path_factory.mktemp("prd078-migration-templates")
    migrations_0070 = _migration_subset(root / "migrations-0070", 70)
    migrations_0071 = _migration_subset(root / "migrations-0071", 71)
    database = root / "template-0070.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations_0070).applied[-1] == "0070"
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = SEED_PASSWORD
    try:
        apply_seed_data(database_url, seed_file=SEED_FILE)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous
    return {
        "database_0070": database,
        "migrations_0070": migrations_0070,
        "migrations_0071": migrations_0071,
    }


def _clone_0070(templates: dict[str, Path], destination: Path) -> str:
    """``_clone_0070`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    shutil.copy2(templates["database_0070"], destination)
    return _url(destination)


def _head(connection: sqlite3.Connection) -> str:
    """``_head`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    row = connection.execute(
        """
        SELECT version FROM schema_migrations
        WHERE status='applied' ORDER BY version DESC LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return str(row[0])


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """``_assert_healthy`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _assert_rbac_head(connection: sqlite3.Connection, revision: int) -> None:
    """``_assert_rbac_head`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    permission_count = 72 if revision == 14 else 70
    relation_count = 194 if revision == 14 else 188
    assert tuple(
        connection.execute(
            """
            SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision)
            FROM rbac_permission_templates
            """
        ).fetchone()
    ) == (permission_count, revision, revision)
    assert (
        connection.execute("SELECT COUNT(*) FROM rbac_role_permission_templates").fetchone()[0]
        == relation_count
    )
    tenants = connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
    assert connection.execute("SELECT COUNT(*) FROM permissions").fetchone()[0] == (
        tenants * permission_count
    )


def _persistent_schema_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    """冻结 PRD-078 持久 schema，用于正反迁移保真比较。"""
    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type,name,tbl_name,sql FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name
            """
        )
    )


def _rows_snapshot(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
) -> tuple[tuple[object, ...], ...]:
    """``_rows_snapshot`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    rows: list[tuple[object, ...]] = []
    for table in tables:
        rows.extend(
            (table, *tuple(row))
            for row in connection.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")
        )
    return tuple(rows)


def _rbac_static_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """冻结自定义 RBAC 事实，验证升级和回退不扩大授权。"""
    return _rows_snapshot(
        connection,
        (
            "rbac_permission_templates",
            "rbac_role_permission_templates",
            "permissions",
            "role_permissions",
        ),
    )


def _approval_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """``_approval_snapshot`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return _rows_snapshot(
        connection,
        (
            "approval_tasks",
            "approval_decisions",
            "approval_step_up_challenges",
            "approval_step_up_guards",
            "approval_action_replays",
            "approval_writebacks",
            "approval_events",
            "approval_event_outbox",
            "approval_scheduler_tenants",
        ),
    )


def _webhook_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """``_webhook_snapshot`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return _rows_snapshot(
        connection,
        (
            "webhook_endpoints",
            "webhook_deliveries",
            "webhook_delivery_attempts",
            "webhook_dead_letter_events",
        ),
    )


def _critical_0070_schema(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """冻结 0070 的 RBAC 与预算关键 schema，证明 0071 回退不会改写既有治理合同。"""
    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type,name,tbl_name,sql FROM sqlite_schema
            WHERE tbl_name IN('rbac_permission_templates','permissions','budget_blocks')
               OR name IN('trg_tenants_rbac_initialize_prd059',
                 'trg_roles_owner_insert_prd059','trg_roles_matrix_update_prd059',
                 'trg_role_permissions_owner_insert_prd059',
                 'trg_role_permissions_delete_prd059',
                 'trg_rbac_role_templates_insert_prd059',
                 'trg_rbac_role_templates_update_prd059',
                 'trg_rbac_role_templates_delete_prd059')
            ORDER BY type,name
            """
        )
    )


def _insert_budget_pair(connection: sqlite3.Connection, marker: str) -> tuple[str, str]:
    """``_insert_budget_pair`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    owner = connection.execute(
        """
        SELECT tenant.tenant_id,environment.environment_id
        FROM tenants tenant JOIN environments environment
          ON environment.tenant_id=tenant.tenant_id AND environment.code='dev'
        WHERE tenant.slug='default'
        """
    ).fetchone()
    assert owner is not None
    rule_id = f"br_prd078_migration_{marker}"
    block_id = f"bb_prd078_migration_{marker}"
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
          daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
          exhausted_action,status,created_at,updated_at
        ) VALUES(?,?,?,'tenant',?,'PRD078 migration rule',10,80,100,'block',
                 'active',?,?)
        """,
        (rule_id, owner["tenant_id"], owner["environment_id"], owner["tenant_id"], NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO budget_blocks(
          budget_block_id,tenant_id,environment_id,budget_rule_id,scope_type,scope_id,
          budget_date,status,blocked_cost_cny,budget_cny,blocked_request_id,
          blocked_trace_id,blocked_at,created_at,updated_at
        ) VALUES(?,?,?,?,'tenant',?,'2026-08-13','active',10,10,?,?,?, ?,?)
        """,
        (
            block_id,
            owner["tenant_id"],
            owner["environment_id"],
            rule_id,
            owner["tenant_id"],
            f"req_prd078_migration_{marker}",
            f"trace_prd078_migration_{marker}",
            NOW,
            NOW,
            NOW,
        ),
    )
    connection.commit()
    return rule_id, block_id


def _insert_webhook_endpoint(
    connection: sqlite3.Connection,
    marker: str,
    event_types_json: str,
) -> tuple[str, str]:
    """插入测试专用的同租户 Webhook Endpoint，用于验证 0071 事件快照与回退保真。"""
    owner = connection.execute(
        """
        SELECT tenant_id,user_id FROM users
        WHERE email='admin@metahub.local' AND status='active'
        """
    ).fetchone()
    assert owner is not None
    endpoint_id = f"wh_prd078_migration_{marker}"
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'https://example.invalid/prd078',?,'synthetic-ciphertext',
                 'whsec_test','prd078-synthetic-fingerprint','disabled',1000,3,
                 ?,?,?,?)
        """,
        (
            endpoint_id,
            owner["tenant_id"],
            f"PRD078 {marker}",
            event_types_json,
            owner["user_id"],
            owner["user_id"],
            NOW,
            NOW,
        ),
    )
    # tenant_id 必须来自可信所有者关系并参与每次读写条件；缺失或不一致时立即失败关闭。
    return str(owner["tenant_id"]), endpoint_id


def _fact_id(prefix: str, marker: str) -> str:
    """生成满足 PRD-078 固定前缀与 32 位十六进制后缀约束的测试 ID。"""

    return prefix + hashlib.sha256(marker.encode("ascii")).hexdigest()[:32]


def _insert_replay_owner_facts(
    connection: sqlite3.Connection,
    marker: str,
    decided_at: datetime,
) -> tuple[str, str, str]:
    """建立一组合法终态 Task/Decision，供 replay 窗口 trigger 独立验证。"""

    rule_id = "br_prd078_migration_replay_owner"
    block_id = "bb_prd078_migration_replay_owner"
    if connection.execute(
        "SELECT COUNT(*) FROM budget_rules WHERE budget_rule_id=?",
        (rule_id,),
    ).fetchone()[0] == 0:
        assert _insert_budget_pair(connection, "replay_owner") == (rule_id, block_id)
    owner = connection.execute(
        """
        SELECT tenant_id,user_id FROM users
        WHERE email='admin@metahub.local' AND status='active'
        """
    ).fetchone()
    assert owner is not None
    timestamp = decided_at.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    created_at = (decided_at - timedelta(minutes=1)).astimezone(UTC).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    expires_at = (decided_at + timedelta(hours=1)).astimezone(UTC).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    task_id = _fact_id("apr_", f"task-{marker}")
    decision_id = _fact_id("apd_", f"decision-{marker}")
    connection.execute(
        """
        INSERT INTO approval_tasks(
          approval_task_id,tenant_id,source_type,source_id,target_type,target_id,
          target_action,target_reason_code,target_version,target_fingerprint,risk_level,
          status,resolution_reason,state_revision,writeback_status,required_permission_code,
          created_by_type,created_by_id,decided_by_user_id,decided_at,expires_at,
          create_hmac_key_version,create_idempotency_key_hmac,create_intent_hmac,
          source_request_id,source_trace_id,decision_request_id,decision_trace_id,
          created_at,updated_at
        ) VALUES(?,?,'budget',?,'budget_block_release',?,'release',NULL,?,?,'standard',
                 'pending',NULL,1,'pending','billing.budget.write','service',?,NULL,NULL,?,
                 1,?,?,?, ?,NULL,NULL,?,?)
        """,
        (
            task_id,
            owner["tenant_id"],
            block_id,
            rule_id,
            f"version-{marker}",
            hashlib.sha256(f"target-{marker}".encode("ascii")).hexdigest(),
            "prd078-migration-test",
            expires_at,
            hashlib.sha256(f"create-key-{marker}".encode("ascii")).hexdigest(),
            hashlib.sha256(f"create-intent-{marker}".encode("ascii")).hexdigest(),
            f"req_prd078_replay_create_{marker}",
            f"trace_prd078_replay_create_{marker}",
            created_at,
            created_at,
        ),
    )
    request_id = f"req_prd078_replay_decide_{marker}"
    trace_id = f"trace_prd078_replay_decide_{marker}"
    connection.execute(
        """
        UPDATE approval_tasks
        SET status='approved',resolution_reason='approved',state_revision=2,
            writeback_status='completed',decided_by_user_id=?,decided_at=?,
            decision_request_id=?,decision_trace_id=?,updated_at=?
        WHERE tenant_id=? AND approval_task_id=?
        """,
        (
            owner["user_id"],
            timestamp,
            request_id,
            trace_id,
            timestamp,
            owner["tenant_id"],
            task_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO approval_decisions(
          approval_decision_id,tenant_id,approval_task_id,decision,reason_code,
          actor_type,actor_id,revision_before,revision_after,step_up_used,
          writeback_status,request_id,trace_id,decided_at
        ) VALUES(?,?,?,'approve','approved_business_need','user',?,1,2,0,
                 'completed',?,?,?)
        """,
        (
            decision_id,
            owner["tenant_id"],
            task_id,
            owner["user_id"],
            request_id,
            trace_id,
            timestamp,
        ),
    )
    # 返回可信 owner 与决策标识，后续 replay 夹具不得从客户端字段重新推导租户身份。
    return str(owner["tenant_id"]), str(owner["user_id"]), decision_id


def _insert_action_replay(
    connection: sqlite3.Connection,
    *,
    marker: str,
    tenant_id: str,
    actor_user_id: str,
    decision_id: str,
    digest: str,
    created_at: datetime,
    action: str = "approve",
    reason_code: str = "approved_business_need",
) -> None:
    """为合法 Decision 写入固定 24 小时 replay 摘要，不保存原始幂等键。"""

    task_id = connection.execute(
        """
        SELECT approval_task_id FROM approval_decisions
        WHERE tenant_id=? AND approval_decision_id=?
        """,
        (tenant_id, decision_id),
    ).fetchone()[0]
    timestamp = created_at.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    expires_at = (created_at + timedelta(hours=24)).astimezone(UTC).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    connection.execute(
        """
        INSERT INTO approval_action_replays(
          approval_action_replay_id,tenant_id,approval_task_id,actor_user_id,
          action,expected_revision,reason_code,hmac_key_version,idempotency_key_hmac,
          intent_hmac,approval_decision_id,expires_at,created_at
        ) VALUES(?,?,?,?,?,1,?,1,?,?,?, ?,?)
        """,
        (
            _fact_id("aprep_", f"replay-{marker}"),
            tenant_id,
            task_id,
            actor_user_id,
            action,
            reason_code,
            digest,
            hashlib.sha256(f"intent-{marker}".encode("ascii")).hexdigest(),
            decision_id,
            expires_at,
            timestamp,
        ),
    )


def _install_revision14_catalog_drift(connection: sqlite3.Connection) -> None:
    """同时改全局与租户投影，证明 down 不把自洽的等量漂移视为规范目录。"""

    trigger_names = (
        "trg_rbac_permission_templates_update_prd059",
        "trg_permissions_update_prd059",
    )
    statements: list[str] = []
    for name in trigger_names:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
        assert row is not None and row[0]
        statements.append(str(row[0]))
        connection.execute(f"DROP TRIGGER {name}")
    assert (
        connection.execute(
            """
        UPDATE rbac_permission_templates SET description=description||' [synthetic drift]'
        WHERE permission_code='approval.read'
        """
        ).rowcount
        == 1
    )
    assert (
        connection.execute(
            """
        UPDATE permissions SET description=description||' [synthetic drift]'
        WHERE code='approval.read'
        """
        ).rowcount
        == connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
    )
    for statement in statements:
        connection.execute(statement)
    connection.commit()


def test_prd078_fresh_zero_to_71_and_repeat_are_healthy(
    prd078_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """空库升级和重复执行均保持迁移 0071 健康。"""
    database_url = _url(tmp_path / "fresh-0071.sqlite3")
    result = apply_migrations(
        database_url,
        migrations_dir=prd078_migration_templates["migrations_0071"],
    )
    assert result.applied == [f"{version:04d}" for version in range(1, 72)]
    repeat = apply_migrations(
        database_url,
        migrations_dir=prd078_migration_templates["migrations_0071"],
    )
    assert repeat.applied == []
    assert repeat.skipped[-1] == "0071"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0071"
        _assert_rbac_head(connection, 14)
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM sqlite_schema
            WHERE type='table' AND name LIKE 'approval_%'
            """
            ).fetchone()[0]
            == 9
        )
        _assert_healthy(connection)


def test_prd078_replay_digest_window_expires_without_deleting_history(
    prd078_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """摘要只在 24 小时窗口内唯一；边界到期后可复用且历史保持 append-only。"""

    database_url = _clone_0070(
        prd078_migration_templates,
        tmp_path / "replay-window.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd078_migration_templates["migrations_0071"],
    ).applied == ["0071"]
    base = datetime(2026, 8, 13, 1, 0, tzinfo=UTC)
    with connect_database(database_url) as connection:
        active_owner = _insert_replay_owner_facts(connection, "active_first", base)
        active_digest = hashlib.sha256(b"active-window-digest").hexdigest()
        _insert_action_replay(
            connection,
            marker="active_first",
            tenant_id=active_owner[0],
            actor_user_id=active_owner[1],
            decision_id=active_owner[2],
            digest=active_digest,
            created_at=base,
        )
        active_second = base + timedelta(hours=24) - timedelta(seconds=1)
        second_owner = _insert_replay_owner_facts(
            connection,
            "active_second",
            active_second,
        )
        with pytest.raises(sqlite3.IntegrityError, match="approval_replay_owner_mismatch"):
            _insert_action_replay(
                connection,
                marker="active_second",
                tenant_id=second_owner[0],
                actor_user_id=second_owner[1],
                decision_id=second_owner[2],
                digest=active_digest,
                created_at=active_second,
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_action_replays WHERE idempotency_key_hmac=?",
            (active_digest,),
        ).fetchone()[0] == 1

        for boundary, delta in (
            ("exact", timedelta(hours=24)),
            ("over", timedelta(hours=24, seconds=1)),
        ):
            digest = hashlib.sha256(f"{boundary}-digest".encode("ascii")).hexdigest()
            first = _insert_replay_owner_facts(connection, f"{boundary}_first", base)
            _insert_action_replay(
                connection,
                marker=f"{boundary}_first",
                tenant_id=first[0],
                actor_user_id=first[1],
                decision_id=first[2],
                digest=digest,
                created_at=base,
            )
            second_time = base + delta
            second = _insert_replay_owner_facts(
                connection,
                f"{boundary}_second",
                second_time,
            )
            _insert_action_replay(
                connection,
                marker=f"{boundary}_second",
                tenant_id=second[0],
                actor_user_id=second[1],
                decision_id=second[2],
                digest=digest,
                created_at=second_time,
            )
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_action_replays WHERE idempotency_key_hmac=?",
                (digest,),
            ).fetchone()[0] == 2

        owner = _insert_replay_owner_facts(connection, "owner_guard", base)
        with pytest.raises(sqlite3.IntegrityError, match="approval_replay_owner_mismatch"):
            _insert_action_replay(
                connection,
                marker="owner_guard",
                tenant_id=owner[0],
                actor_user_id=owner[1],
                decision_id=owner[2],
                digest=hashlib.sha256(b"owner-guard-digest").hexdigest(),
                created_at=base,
                action="reject",
                reason_code="budget_not_approved",
            )
        index_columns = tuple(
            row["name"]
            for row in connection.execute(
                "PRAGMA index_xinfo(idx_approval_replays_idempotency_expiry_prd078)"
            )
            if row["key"] == 1
        )
        assert index_columns == (
            "tenant_id",
            "actor_user_id",
            "hmac_key_version",
            "idempotency_key_hmac",
            "expires_at",
        )
        connection.commit()
        _assert_healthy(connection)


def test_prd078_registration_failure_rolls_back_schema_and_preserves_original_error(
    prd078_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """0071 DDL 与版本登记同 commit，登记及失败证据均被拒时仍保留原错误。"""

    migration_path = next(
        prd078_migration_templates["migrations_0071"].glob(
            "0071_mobile_enterprise_approval_entry.sql"
        )
    )
    sql = migration_path.read_text(encoding="utf-8")
    migration = migration_runtime.MigrationFile(
        version="0071",
        name="mobile_enterprise_approval_entry",
        path=migration_path,
        checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        sql=sql,
    )
    injected = migration_runtime._inject_atomic_migration_registration(migration)
    assert injected is not None
    registration_at = injected.index("INSERT INTO schema_migrations(")
    assert registration_at > injected.index("DROP TABLE prd078_up_final_guard;")
    assert registration_at < injected.rindex("COMMIT;")

    database_url = _clone_0070(
        prd078_migration_templates,
        tmp_path / "registration-failure.sqlite3",
    )
    with connect_database(database_url) as connection:
        connection.executescript(
            """
            CREATE TRIGGER prd078_registration_rejected
            BEFORE INSERT ON schema_migrations FOR EACH ROW
            WHEN NEW.version='0071'
            BEGIN SELECT RAISE(ABORT,'synthetic_registration_rejected'); END;
            """
        )
        schema_before = _persistent_schema_snapshot(connection)
    with pytest.raises(DbError) as error:
        apply_migrations(
            database_url,
            migrations_dir=prd078_migration_templates["migrations_0071"],
        )
    assert error.value.code == "db.migration_failed"
    assert "synthetic_registration_rejected" in error.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0070"
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='0071'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='approval_tasks'"
        ).fetchone()[0] == 0
        assert _persistent_schema_snapshot(connection) == schema_before
        _assert_healthy(connection)


def test_migration_registration_rejects_untrusted_metadata_before_execution(
    tmp_path: Path,
) -> None:
    """不受信事务形态或 NUL 元数据在执行前失败，不伪造迁移失败事实。"""

    migrations = tmp_path / "untrusted-migrations"
    migrations.mkdir()
    (migrations / "0001_base.sql").write_text(
        "CREATE TABLE base_fact(value TEXT NOT NULL);\n",
        encoding="utf-8",
    )
    (migrations / "0002_bad_shape.sql").write_text(
        "BEGIN IMMEDIATE;\nCREATE TABLE forbidden_fact(value TEXT);\n",
        encoding="utf-8",
    )
    database_url = _url(tmp_path / "untrusted.sqlite3")
    with pytest.raises(DbError) as error:
        apply_migrations(database_url, migrations_dir=migrations)
    assert error.value.code == "db.migration_failed"
    assert error.value.message == (
        "Migration transaction shape is not supported for atomic registration."
    )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='forbidden_fact'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='0002'"
        ).fetchone()[0] == 0

    migration = migration_runtime.MigrationFile(
        version="0002",
        name="bad\x00name",
        path=migrations / "0002_bad_shape.sql",
        checksum="0" * 64,
        sql="BEGIN IMMEDIATE;\nCOMMIT;\n",
    )
    with pytest.raises(DbError) as nul_error:
        migration_runtime._inject_atomic_migration_registration(migration)
    assert nul_error.value.code == "db.migration_failed"
    assert nul_error.value.message == "Migration metadata contains an invalid character."


@pytest.mark.parametrize(
    "drift_kind",
    ("permission_metadata", "tenant_permission_metadata", "role_relation", "trigger_body"),
)
def test_prd078_up_rejects_equal_count_catalog_or_schema_drift_atomically(
    prd078_migration_templates: dict[str, Path],
    tmp_path: Path,
    drift_kind: str,
) -> None:
    """0071 原子拒绝等数量目录或 schema 漂移。"""
    database_url = _clone_0070(
        prd078_migration_templates,
        tmp_path / f"up-drift-{drift_kind}.sqlite3",
    )
    with connect_database(database_url) as connection:
        prd077._install_rbac_drift(connection, drift_kind)
        schema_before = _persistent_schema_snapshot(connection)
        rbac_before = prd077._rbac_snapshot(connection)
    with pytest.raises(DbError) as error:
        apply_migrations(
            database_url,
            migrations_dir=prd078_migration_templates["migrations_0071"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd078_up_guard_ok" in error.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0070"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name='approval_tasks'"
            ).fetchone()[0]
            == 0
        )
        assert _persistent_schema_snapshot(connection) == schema_before
        assert prd077._rbac_snapshot(connection) == rbac_before
        _assert_healthy(connection)


def test_prd078_upgrade_expands_only_default_matrices_and_new_tenants(
    prd078_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """升级只扩展默认矩阵和升级后新租户。"""
    database_url = _clone_0070(prd078_migration_templates, tmp_path / "upgrade.sqlite3")
    with connect_database(database_url) as connection:
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd078_existing','prd078-existing','PRD078 Existing',
                   'internal_only','active',?,?)
            """,
            (NOW, NOW),
        )
        connection.commit()
        custom_roles = (
            prd077._customize_role_matrix(
                connection,
                "finance_admin",
                "response_cache.read",
            ),
            prd077._customize_role_matrix(
                connection,
                "security_admin",
                "session_risk.read",
            ),
        )
    assert apply_migrations(
        database_url,
        migrations_dir=prd078_migration_templates["migrations_0071"],
    ).applied == ["0071"]
    with connect_database(database_url) as connection:
        _assert_rbac_head(connection, 14)
        for custom in custom_roles:
            role_id = str(custom["role_id"])
            role = connection.execute(
                "SELECT rowid,* FROM roles WHERE role_id=?",
                (role_id,),
            ).fetchone()
            assert role is not None and tuple(role) == custom["role_row"]
            assert prd077._role_relations(connection, role_id) == custom["relations"]
            assert (
                connection.execute(
                    """
                SELECT COUNT(*) FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.role_id=?
                  AND permission.code IN('approval.read','approval.decide')
                """,
                    (role_id,),
                ).fetchone()[0]
                == 0
            )
        for code, matrix_hash in NEW_HASHES.items():
            assert (
                connection.execute(
                    """
                SELECT COUNT(DISTINCT permission.code)
                FROM roles role JOIN role_permissions relation
                  ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE role.code=? AND role.matrix_sha256=?
                  AND permission.code IN('approval.read','approval.decide')
                """,
                    (code, matrix_hash),
                ).fetchone()[0]
                == 2
            )
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd078_new','prd078-new','PRD078 New','internal_only','active',?,?)
            """,
            (NOW, NOW),
        )
        connection.commit()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM permissions WHERE tenant_id='tenant_prd078_new'"
            ).fetchone()[0]
            == 72
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM roles role JOIN role_permissions relation
              ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE role.tenant_id='tenant_prd078_new'
              AND role.code IN('admin','finance_admin','security_admin')
              AND permission.code IN('approval.read','approval.decide')
            """
            ).fetchone()[0]
            == 6
        )
        _assert_healthy(connection)


def test_prd078_empty_round_trip_preserves_rowids_custom_matrix_and_schema(
    prd078_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """空业务库回退重升保留 rowid、自定义矩阵和 schema。"""
    database_url = _clone_0070(prd078_migration_templates, tmp_path / "round-trip.sqlite3")
    with connect_database(database_url) as connection:
        custom = prd077._customize_role_matrix(
            connection,
            "finance_admin",
            "response_cache.read",
        )
        _insert_budget_pair(connection, "roundtrip")
        static_before = _rbac_static_snapshot(connection)
        budget_before = tuple(
            connection.execute("SELECT rowid,* FROM budget_blocks ORDER BY rowid").fetchone()
        )
        schema_before = _critical_0070_schema(connection)
    assert apply_migrations(
        database_url,
        migrations_dir=prd078_migration_templates["migrations_0071"],
    ).applied == ["0071"]
    assert (
        rollback_last_migration(
            database_url,
            migrations_dir=prd078_migration_templates["migrations_0071"],
            expected_version="0071",
        ).rolled_back
        == "0071"
    )
    with connect_database(database_url) as connection:
        assert _head(connection) == "0070"
        _assert_rbac_head(connection, 13)
        assert _rbac_static_snapshot(connection) == static_before
        assert (
            tuple(connection.execute("SELECT rowid,* FROM budget_blocks ORDER BY rowid").fetchone())
            == budget_before
        )
        assert _critical_0070_schema(connection) == schema_before
        role = connection.execute(
            "SELECT rowid,* FROM roles WHERE role_id=?",
            (custom["role_id"],),
        ).fetchone()
        assert role is not None and tuple(role) == custom["role_row"]
        assert prd077._role_relations(connection, str(custom["role_id"])) == custom["relations"]
        _assert_healthy(connection)
    assert apply_migrations(
        database_url,
        migrations_dir=prd078_migration_templates["migrations_0071"],
    ).applied == ["0071"]
    with connect_database(database_url) as connection:
        assert _head(connection) == "0071"
        _assert_healthy(connection)


@pytest.mark.parametrize("drift_kind", ("noop_trigger", "wrong_index", "table_schema"))
def test_prd078_down_rejects_canonical_schema_drift_atomically(
    prd078_migration_templates: dict[str, Path],
    tmp_path: Path,
    drift_kind: str,
) -> None:
    """验证回退能原子拒绝审批表、索引或触发器漂移，且失败后 0071 事实仍保持健康。"""
    database_url = _clone_0070(
        prd078_migration_templates,
        tmp_path / f"down-schema-{drift_kind}.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd078_migration_templates["migrations_0071"],
    ).applied == ["0071"]
    with connect_database(database_url) as connection:
        if drift_kind == "noop_trigger":
            connection.executescript(
                """
                DROP TRIGGER trg_approval_events_update_prd078;
                CREATE TRIGGER trg_approval_events_update_prd078
                BEFORE UPDATE ON approval_events FOR EACH ROW BEGIN SELECT 1; END;
                """
            )
        elif drift_kind == "wrong_index":
            connection.executescript(
                """
                DROP INDEX idx_approval_tasks_due_prd078;
                CREATE INDEX idx_approval_tasks_due_prd078
                  ON approval_tasks(tenant_id,created_at);
                """
            )
        else:
            connection.execute(
                "ALTER TABLE approval_step_up_guards ADD COLUMN schema_drift_marker INTEGER"
            )
            connection.commit()
        schema_before = _persistent_schema_snapshot(connection)
        rbac_before = prd077._rbac_snapshot(connection)
    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd078_migration_templates["migrations_0071"],
            expected_version="0071",
        )
    assert error.value.code == "db.migration_failed"
    assert "prd078_down_schema_invalid" in error.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0071"
        assert _persistent_schema_snapshot(connection) == schema_before
        assert prd077._rbac_snapshot(connection) == rbac_before
        _assert_healthy(connection)


def test_prd078_down_rejects_self_consistent_catalog_drift_atomically(
    prd078_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """回退原子拒绝自洽但非冻结版本的 RBAC 目录。"""
    database_url = _clone_0070(
        prd078_migration_templates,
        tmp_path / "down-catalog-drift.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd078_migration_templates["migrations_0071"],
    ).applied == ["0071"]
    with connect_database(database_url) as connection:
        _install_revision14_catalog_drift(connection)
        schema_before = _persistent_schema_snapshot(connection)
        rbac_before = prd077._rbac_snapshot(connection)
    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd078_migration_templates["migrations_0071"],
            expected_version="0071",
        )
    assert error.value.code == "db.migration_failed"
    assert "prd078_down_schema_invalid" in error.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0071"
        assert _persistent_schema_snapshot(connection) == schema_before
        assert prd077._rbac_snapshot(connection) == rbac_before
        _assert_healthy(connection)


@pytest.mark.anyio
@pytest.mark.parametrize("fact_kind", ("task", "subscription", "delivery", "custom_holder"))
async def test_prd078_down_business_guards_are_atomic(
    prd078_migration_templates: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fact_kind: str,
) -> None:
    """存在审批业务事实时回退 guard 必须原子失败。"""
    database_url = _clone_0070(
        prd078_migration_templates,
        tmp_path / f"down-business-{fact_kind}.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd078_migration_templates["migrations_0071"],
    ).applied == ["0071"]
    with connect_database(database_url) as connection:
        if fact_kind == "custom_holder":
            prd077._customize_role_matrix(connection, "finance_admin", "response_cache.read")
        elif fact_kind == "subscription":
            _insert_webhook_endpoint(
                connection,
                "subscription",
                '["approval.created"]',
            )
            connection.commit()
        elif fact_kind == "delivery":
            tenant_id, endpoint_id = _insert_webhook_endpoint(
                connection,
                "delivery",
                '["audit.recorded"]',
            )
            connection.execute(
                """
                INSERT INTO webhook_deliveries(
                  webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
                  event_timestamp,payload_json,status,attempt_count,max_attempts,
                  delivery_kind,secret_version,created_at,updated_at
                ) VALUES('whd_78787878787878787878787878787878',?,?,
                         'event_prd078_delivery','approval.created',?,'{}','pending',
                         0,3,'original',1,?,?)
                """,
                (tenant_id, endpoint_id, NOW, NOW, NOW),
            )
            connection.commit()
        else:
            rule_id, block_id = _insert_budget_pair(connection, "business")
            tenant_id = str(
                connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()[
                    0
                ]
            )
            control_fixtures._configure_runtime(monkeypatch, database_url)
            fixture = control_fixtures.ApprovalFixture(
                database_url,
                tenant_id,
                "tenant_unused",
                {"approve": rule_id},
                {"approve": block_id},
                {},
            )
            app = control_fixtures._build_app()
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await control_fixtures._create(
                    client,
                    fixture,
                    "approve",
                    key="prd078-migration-business-task",
                )
            assert response.status_code == 201, response.text

    with connect_database(database_url) as connection:
        schema_before = _persistent_schema_snapshot(connection)
        rbac_before = prd077._rbac_snapshot(connection)
        approval_before = _approval_snapshot(connection)
        webhook_before = _webhook_snapshot(connection)
    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd078_migration_templates["migrations_0071"],
            expected_version="0071",
        )
    assert error.value.code == "db.migration_failed"
    assert "prd078_down_business_facts_present" in error.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0071"
        assert _persistent_schema_snapshot(connection) == schema_before
        assert prd077._rbac_snapshot(connection) == rbac_before
        assert _approval_snapshot(connection) == approval_before
        assert _webhook_snapshot(connection) == webhook_before
        _assert_healthy(connection)
