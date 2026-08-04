"""PRD-059 0050 细粒度角色权限矩阵迁移专项测试。

测试数据库均由 pytest tmp_path 独占创建；夹具只写固定安全 ID 和治理元数据，
不得保存 Provider Key、Token、Prompt、Response、Secret 或真实 Endpoint。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.rbac import (
    PERMISSION_CATALOG,
    ROLE_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)

# 空库升级必须连续登记全部五十条迁移，缺号或乱序都属于不可发布状态。
EXPECTED_VERSIONS = [f"{version:04d}" for version in range(1, 51)]
"""PRD-059 fresh database 的连续迁移版本合同。"""

# 历史升级测试复制真实迁移文件到 pytest 临时目录，以精确停在 0049，不修改生产目录。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
"""仓库中的生产迁移目录；测试只读取并复制到 D:/MetaHub/.tmp 下的 tmp_path。"""

# 以下 PRD-058 父子事实必须在 0050 up/down 中逐 rowid 保真，避免只比较行数漏掉漂移。
PRESERVED_TABLES = (
    "audit_logs",
    "audit_log_export_sequences",
    "audit_exports",
    "audit_export_events",
)
"""0050 up/down 都必须逐 rowid 保真的 PRD-058 审计与导出事实表。"""

# 固定时间只用于构造迁移事实，避免机器时钟让 sequence、事件和审计投影不可复现。
NOW = "2026-07-31T10:00:00Z"
"""固定 UTC 时间，避免迁移证据依赖测试机器当前时钟。"""

# PRD-059 专项刻意停在 0050；共享目录已由 PRD-069 提升到 revision 6，因此这里必须显式冻结
# revision 1 的 48 项历史投影，不能让配置迁移、Chat、两类模板或敏感意图权限反向改写早期语义。
PRD059_LATER_PERMISSION_CODES = frozenset(
    {
        "chat.use",
        "config.transfer.read",
        "config.transfer.write",
        "intent_risk.classify",
        "prompt_template.read",
        "prompt_template.write",
        "prompt_template.use",
        "policy_template.read",
        "policy_template.apply",
    }
)
"""0050 尚不存在、必须从共享 revision 6 目录中排除的后续权限码。"""


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库路径转换为项目迁移器支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不高于指定版本的真实 up/down SQL，构造隔离的历史升级目录。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        prefix = source.name[:4]
        if prefix.isdigit() and int(prefix) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """验证 SQLite 内容、外键及迁移事务均保持健康。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _assert_tenant_matrix(connection: sqlite3.Connection, tenant_id: str) -> None:
    """逐角色复核当前租户 6/48/125 默认关系、revision 和规范哈希。"""

    catalog_codes = {
        definition.code
        for definition in PERMISSION_CATALOG
        if definition.code not in PRD059_LATER_PERMISSION_CODES
    }
    permission_codes = {
        row["code"]
        for row in connection.execute(
            "SELECT code FROM permissions WHERE tenant_id=?", (tenant_id,)
        )
    }
    assert permission_codes == catalog_codes
    roles = connection.execute(
        """
        SELECT role_id,code,permission_revision,matrix_sha256,is_permission_immutable
        FROM roles WHERE tenant_id=? ORDER BY code
        """,
        (tenant_id,),
    ).fetchall()
    assert {row["code"] for row in roles} == set(ROLE_CODES)
    for role in roles:
        granted = {
            row["code"]
            for row in connection.execute(
                """
                SELECT permission.code
                FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=?
                """,
                (tenant_id, role["role_id"]),
            )
        }
        expected = set(ROLE_GRANTABLE_PERMISSIONS[role["code"]]) - set(
            PRD059_LATER_PERMISSION_CODES
        )
        assert granted == expected
        assert role["permission_revision"] == 1
        assert role["matrix_sha256"] == permission_matrix_sha256(expected)
        assert role["is_permission_immutable"] == (1 if role["code"] == "admin" else 0)
    assert connection.execute(
        "SELECT COUNT(*) FROM role_permissions WHERE tenant_id=?", (tenant_id,)
    ).fetchone()[0] == 125


def _assert_role_permission_foreign_keys(connection: sqlite3.Connection) -> None:
    """冻结 RolePermission 到角色和权限的两条 tenant-qualified 复合外键。"""

    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in connection.execute("PRAGMA foreign_key_list('role_permissions')"):
        grouped.setdefault(row["id"], []).append(row)
    normalized = {
        (
            rows[0]["table"],
            tuple(
                (row["from"], row["to"])
                for row in sorted(rows, key=lambda item: item["seq"])
            ),
        )
        for rows in grouped.values()
    }
    assert normalized == {
        ("roles", (("tenant_id", "tenant_id"), ("role_id", "role_id"))),
        (
            "permissions",
            (("tenant_id", "tenant_id"), ("permission_id", "permission_id")),
        ),
    }


def _insert_tenant(connection: sqlite3.Connection, tenant_id: str, slug: str) -> None:
    """插入一个安全测试租户；0050 trigger 必须在同一事务生成完整默认矩阵。"""

    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id,slug,name,service_mode,status,created_at,updated_at
        ) VALUES(?,?,?,'internal_only','active',?,?)
        """,
        (tenant_id, slug, f"Tenant {slug}", NOW, NOW),
    )


def _admin(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
    """按 tenant-qualified 条件读取管理员，夹具本身不得掩盖跨租户 owner 缺陷。"""

    row = connection.execute(
        """
        SELECT user_id,email,role FROM users
        WHERE tenant_id=? AND role='admin' ORDER BY user_id LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None
    return row


def _insert_plain_audit(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
    audit_log_id: str,
    created_at: str = NOW,
) -> None:
    """写入不依赖具体业务表的旧枚举审计，用于验证 rowid 与稳定序列保真。"""

    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            audit_log_id,
            tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            "provider.updated",
            "provider",
            "provider_prd059_placeholder",
            None,
            '{"status":"inactive"}',
            '{"status":"active"}',
            '["status"]',
            None,
            "prd059-migration-test",
            f"req_{audit_log_id}",
            f"trc_{audit_log_id}",
            created_at,
        ),
    )


def _export_values(tenant_id: str, actor_id: str) -> dict[str, object]:
    """构造满足 0049 owner、摘要和 source event 合同的无正文导出字段。"""

    filters_json = "{}"
    fingerprint = hashlib.sha256(filters_json.encode()).hexdigest()
    content_sha = hashlib.sha256(b"prd059-empty-csv").hexdigest()
    export_id = "aex_00000000000000000000000000000059"
    audit_id = "aud_prd059_export"
    payload = {
        "audit_export_id": export_id,
        "content_sha256": content_sha,
        "content_size_bytes": 128,
        "filter_fingerprint": fingerprint,
        "format": "csv",
        "row_count": 0,
        "schema_version": 1,
        "status": "completed",
    }
    return {
        "audit_export_id": export_id,
        "tenant_id": tenant_id,
        "requested_by": actor_id,
        "audit_log_id": audit_id,
        "status": "completed",
        "format": "csv",
        "schema_version": 1,
        "filters_json": filters_json,
        "filter_fingerprint": fingerprint,
        "snapshot_sequence": 0,
        "snapshot_audit_log_id": None,
        "snapshot_created_at": None,
        "row_count": 0,
        "content_size_bytes": 128,
        "content_sha256": content_sha,
        "file_name": "metahub-audit-prd059.csv",
        "request_id": "req_prd059_export",
        "trace_id": "trc_prd059_export",
        "event_id": "aexevt_00000000000000000000000000000059",
        "event_payload_json": json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ),
        "created_at": NOW,
        "updated_at": NOW,
    }


def _insert_export_graph(
    connection: sqlite3.Connection,
    *,
    actor: sqlite3.Row,
    values: dict[str, object],
) -> None:
    """按 0049 冻结顺序写导出、source event 和其一一绑定的不可变 AuditLog。"""

    export_columns = tuple(values)
    connection.execute(
        f"INSERT INTO audit_exports({','.join(export_columns)}) "
        f"VALUES({','.join('?' for _ in export_columns)})",
        tuple(values[column] for column in export_columns),
    )
    connection.execute(
        """
        INSERT INTO audit_export_events(
          audit_export_event_id,tenant_id,audit_export_id,status,attempt_count,
          next_attempt_at,claim_token,claim_owner,lease_until,last_error_code,
          dispatched_at,created_at,updated_at
        ) VALUES(?,?,?,'pending',0,?,NULL,NULL,NULL,NULL,NULL,?,?)
        """,
        (
            values["event_id"],
            values["tenant_id"],
            values["audit_export_id"],
            values["created_at"],
            values["created_at"],
            values["created_at"],
        ),
    )
    summary = json.loads(values["event_payload_json"])
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            values["audit_log_id"],
            values["tenant_id"],
            values["requested_by"],
            actor["email"],
            actor["role"],
            "audit.exported",
            "audit_export",
            values["audit_export_id"],
            None,
            "{}",
            json.dumps(summary, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            json.dumps(sorted(summary), ensure_ascii=True, separators=(",", ":")),
            None,
            "prd059-migration-test",
            values["request_id"],
            values["trace_id"],
            values["created_at"],
        ),
    )


def _table_snapshot(connection: sqlite3.Connection, table_name: str) -> list[tuple[object, ...]]:
    """按 rowid 冻结白名单表全列快照，避免动态表名扩大测试 SQL 边界。"""

    assert table_name in PRESERVED_TABLES
    return [
        tuple(row)
        for row in connection.execute(f"SELECT rowid,* FROM {table_name} ORDER BY rowid")
    ]


def test_prd059_fresh_repeat_new_tenant_and_owner_constraints(tmp_path: Path) -> None:
    """主流程：fresh/repeat、新租户矩阵、admin 不可变和跨租户关系均受数据库约束。"""

    database_url = _sqlite_url(tmp_path / "prd059-fresh-owner.db")
    prd059_dir = _migration_subset(tmp_path / "migrations-through-0050", 50)
    assert apply_migrations(database_url, migrations_dir=prd059_dir).applied == EXPECTED_VERSIONS
    repeat = apply_migrations(database_url, migrations_dir=prd059_dir)
    assert repeat.applied == [] and repeat.skipped == EXPECTED_VERSIONS
    seed = apply_seed_data(database_url)

    connection = connect_database(database_url)
    _assert_tenant_matrix(connection, seed.tenant_id)
    _assert_role_permission_foreign_keys(connection)
    second_tenant_id = "tenant_prd059_second"
    _insert_tenant(connection, second_tenant_id, "prd059-second")
    connection.commit()
    _assert_tenant_matrix(connection, second_tenant_id)

    admin_role_id = connection.execute(
        "SELECT role_id FROM roles WHERE tenant_id=? AND code='admin'",
        (seed.tenant_id,),
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="rbac_role_immutable"):
        connection.execute(
            """
            UPDATE roles
            SET permission_revision=permission_revision+1,
                matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            ("f" * 64, NOW, seed.tenant_id, admin_role_id),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="rbac_role_immutable"):
        connection.execute(
            "DELETE FROM roles WHERE tenant_id=? AND role_id=?",
            (seed.tenant_id, admin_role_id),
        )
    connection.rollback()

    admin_relation = connection.execute(
        """
        SELECT relation.role_permission_id
        FROM role_permissions relation
        JOIN roles role
          ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
        WHERE relation.tenant_id=? AND role.code='admin' LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="rbac_role_permission_immutable"):
        connection.execute(
            "DELETE FROM role_permissions WHERE role_permission_id=?", (admin_relation,)
        )
    connection.rollback()

    foreign_role_id = connection.execute(
        "SELECT role_id FROM roles WHERE tenant_id=? AND code='developer'",
        (seed.tenant_id,),
    ).fetchone()[0]
    local_permission_id = connection.execute(
        "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='console.basic.read'",
        (second_tenant_id,),
    ).fetchone()[0]
    local_role_id = connection.execute(
        "SELECT role_id FROM roles WHERE tenant_id=? AND code='developer'",
        (second_tenant_id,),
    ).fetchone()[0]
    foreign_permission_id = connection.execute(
        "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='console.basic.read'",
        (seed.tenant_id,),
    ).fetchone()[0]
    # 两个方向分别证明 role FK 与 permission FK 都不能把全局 ID 混入另一租户。
    cross_tenant_relations = (
        ("role", foreign_role_id, local_permission_id),
        ("permission", local_role_id, foreign_permission_id),
    )
    for suffix, role_id, permission_id in cross_tenant_relations:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="rbac_role_permission_owner_mismatch",
        ):
            connection.execute(
                """
                INSERT INTO role_permissions(
                  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    f"rperm_prd059_cross_{suffix}",
                    second_tenant_id,
                    role_id,
                    permission_id,
                    NOW,
                    NOW,
                ),
            )
        connection.rollback()
    _assert_healthy(connection)
    connection.close()


def test_prd059_0049_history_up_down_and_reapply_preserve_prd058(tmp_path: Path) -> None:
    """兼容流程：0049 审计/导出图经 up、down、re-up 全列和 rowid 保真。"""

    database_url = _sqlite_url(tmp_path / "prd059-history-roundtrip.db")
    legacy_dir = _migration_subset(tmp_path / "migrations-through-0049", 49)
    prd059_dir = _migration_subset(tmp_path / "migrations-through-0050", 50)
    assert (
        apply_migrations(database_url, migrations_dir=legacy_dir).applied
        == EXPECTED_VERSIONS[:-1]
    )
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = _admin(connection, seed.tenant_id)
    _insert_plain_audit(
        connection,
        tenant_id=seed.tenant_id,
        actor=actor,
        audit_log_id="aud_prd059_legacy",
    )
    _insert_export_graph(
        connection,
        actor=actor,
        values=_export_values(seed.tenant_id, actor["user_id"]),
    )
    connection.commit()
    role_snapshot = [
        tuple(row)
        for row in connection.execute(
            "SELECT rowid,* FROM roles WHERE tenant_id=? ORDER BY rowid", (seed.tenant_id,)
        )
    ]
    preserved = {table: _table_snapshot(connection, table) for table in PRESERVED_TABLES}
    connection.close()

    assert apply_migrations(database_url, migrations_dir=prd059_dir).applied == ["0050"]
    connection = connect_database(database_url)
    _assert_tenant_matrix(connection, seed.tenant_id)
    assert {table: _table_snapshot(connection, table) for table in PRESERVED_TABLES} == preserved
    # 0050 重建 owner 后必须保留 0049 的冲突优先级，重复导出不能误报 owner mismatch。
    with pytest.raises(sqlite3.IntegrityError, match="audit_export_immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO audit_exports SELECT * FROM audit_exports "
            "WHERE tenant_id=? LIMIT 1",
            (seed.tenant_id,),
        )
    connection.rollback()
    connection.close()

    assert rollback_last_migration(database_url, migrations_dir=prd059_dir).rolled_back == "0050"
    connection = connect_database(database_url)
    assert [
        tuple(row)
        for row in connection.execute(
            "SELECT rowid,* FROM roles WHERE tenant_id=? ORDER BY rowid", (seed.tenant_id,)
        )
    ] == role_snapshot
    assert {table: _table_snapshot(connection, table) for table in PRESERVED_TABLES} == preserved
    trigger_names = {
        row["name"]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type='trigger' AND name IN (
              'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
              'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
              'trg_audit_logs_prd050_rowid_insert',
              'trg_audit_logs_export_sequence_insert_prd058',
              'trg_audit_log_export_sequences_owner_insert_prd058',
              'trg_audit_exports_owner_insert_prd058'
            )
            """
        )
    }
    assert len(trigger_names) == 8
    assert connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='index' AND name='uq_users_tenant_user_prd044'"
    ).fetchone() is not None
    assert connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='index' AND name='idx_users_tenant_user_prd059'"
    ).fetchone() is None
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_immutable"):
        connection.execute(
            "UPDATE audit_logs SET user_agent=user_agent WHERE audit_log_id='aud_prd059_legacy'"
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="audit_export_immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO audit_exports SELECT * FROM audit_exports "
            "WHERE tenant_id=? LIMIT 1",
            (seed.tenant_id,),
        )
    connection.rollback()
    previous_sequence = connection.execute(
        "SELECT MAX(audit_log_sequence) FROM audit_log_export_sequences"
    ).fetchone()[0]
    _insert_plain_audit(
        connection,
        tenant_id=seed.tenant_id,
        actor=actor,
        audit_log_id="aud_prd059_after_down",
        created_at="2026-07-31T10:00:01Z",
    )
    connection.commit()
    assert connection.execute(
        """
        SELECT audit_log_sequence FROM audit_log_export_sequences
        WHERE audit_log_id='aud_prd059_after_down'
        """
    ).fetchone()[0] > previous_sequence
    reapply_preserved = {
        table: _table_snapshot(connection, table) for table in PRESERVED_TABLES
    }
    _assert_healthy(connection)
    connection.close()

    assert apply_migrations(database_url, migrations_dir=prd059_dir).applied == ["0050"]
    connection = connect_database(database_url)
    _assert_tenant_matrix(connection, seed.tenant_id)
    assert {
        table: _table_snapshot(connection, table) for table in PRESERVED_TABLES
    } == reapply_preserved
    _assert_healthy(connection)
    connection.close()


@pytest.mark.parametrize(
    "fact_kind",
    ("new_role_user", "matrix_drift", "webhook_subscription", "quoted_dependency"),
)
def test_prd059_down_guard_is_atomic(tmp_path: Path, fact_kind: str) -> None:
    """异常流程：不可表达业务事实或未知依赖必须在首项持久 DDL 前原子阻断 down。"""

    database_url = _sqlite_url(tmp_path / f"prd059-down-guard-{fact_kind}.db")
    prd059_dir = _migration_subset(tmp_path / "migrations-through-0050", 50)
    apply_migrations(database_url, migrations_dir=prd059_dir)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    admin_user_id = _admin(connection, seed.tenant_id)["user_id"]
    # 四类事实分别冻结新角色用户、矩阵漂移、Webhook 订阅和未知引用的 down 前置 guard。
    if fact_kind == "new_role_user":
        connection.execute(
            "UPDATE users SET role='finance_admin' WHERE user_id=?", (admin_user_id,)
        )
    elif fact_kind == "matrix_drift":
        connection.execute(
            """
            DELETE FROM role_permissions
            WHERE role_permission_id=(
              SELECT relation.role_permission_id
              FROM role_permissions relation
              JOIN roles role
                ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
              WHERE relation.tenant_id=? AND role.code='developer' LIMIT 1
            )
            """,
            (seed.tenant_id,),
        )
    elif fact_kind == "webhook_subscription":
        # 订阅本身就是 0050 业务事实；夹具只保存无效域名和测试密文占位符。
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,
              timeout_ms,max_attempts,secret_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "whe_prd059_down_guard",
                seed.tenant_id,
                "PRD-059 down guard",
                "https://example.invalid/prd059",
                '["role.permission_changed"]',
                "enc:v1:test-fixture",
                "test",
                "a" * 64,
                "active",
                5000,
                3,
                1,
                NOW,
                NOW,
            ),
        )
    else:
        connection.execute(
            'CREATE VIEW prd059_unknown_roles AS\nSELECT role_id\nFROM\n"roles"'
        )
    connection.commit()
    connection.close()

    with pytest.raises(DbError) as captured:
        rollback_last_migration(database_url, migrations_dir=prd059_dir)
    assert captured.value.code == "db.migration_failed"
    assert "prd059_down_blocked" in captured.value.message

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0050'"
    ).fetchone()[0] == "applied"
    assert connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='permissions'"
    ).fetchone() is not None
    # down 被拒绝后原始阻断事实必须仍在，证明首项持久 DDL 之前已原子终止。
    if fact_kind == "new_role_user":
        assert connection.execute(
            "SELECT role FROM users WHERE user_id=?", (admin_user_id,)
        ).fetchone()[0] == "finance_admin"
    elif fact_kind == "matrix_drift":
        assert connection.execute(
            "SELECT COUNT(*) FROM role_permissions WHERE tenant_id=?", (seed.tenant_id,)
        ).fetchone()[0] == 124
    elif fact_kind == "webhook_subscription":
        assert connection.execute(
            """
            SELECT event_types_json FROM webhook_endpoints
            WHERE tenant_id=? AND webhook_endpoint_id='whe_prd059_down_guard'
            """,
            (seed.tenant_id,),
        ).fetchone()[0] == '["role.permission_changed"]'
    else:
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='view' AND name='prd059_unknown_roles'"
        ).fetchone() is not None
    _assert_healthy(connection)
    connection.close()
