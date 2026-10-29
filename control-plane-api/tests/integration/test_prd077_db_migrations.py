"""验证 PRD-077 0070 正反迁移、RBAC 默认矩阵兼容和既有审计行完整保真。"""

from __future__ import annotations

import hashlib
import os
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
from metahub_shared.rbac import permission_matrix_sha256

# 迁移目录、前后矩阵哈希和测试口令只用于隔离临时库的确定性 schema 验证。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
NEW_HASHES = {
    "admin": "0012b8538dcabe652d054716a350bd7abf5003c0bcca3a8021e830928afd295d",
    "finance_admin": "63691295710ed938c153dedd989d83412694b1b0b26b7fa83658d90a08a99ce8",
    "security_admin": "38148a7c165b4c2122a37209f88e3beee2c913d56a151b579d765f4f9c4a4656",
}
SEED_PASSWORD = "Prd077MigrationOnly!2026"
NOW = "2026-08-11T00:00:00Z"


def _url(path: Path) -> str:
    """把仓库测试临时目录中的 SQLite 文件转换成应用数据库 URL。"""

    return f"sqlite:///{path.as_posix()}"


def test_prd077_metahub_sha256_sql_contract(tmp_path: Path) -> None:
    """验证迁移哈希 UDF 只接受 TEXT/BLOB/NULL，并稳定返回小写 SHA-256。"""

    with connect_database(_url(tmp_path / "sha256-contract.sqlite3")) as connection:
        text_value = "MetaHub 管理助手"
        blob_value = b"\x00MetaHub\xff"
        assert (
            connection.execute("SELECT metahub_sha256(?)", (text_value,)).fetchone()[0]
            == hashlib.sha256(text_value.encode("utf-8")).hexdigest()
        )
        assert (
            connection.execute("SELECT metahub_sha256(?)", (blob_value,)).fetchone()[0]
            == hashlib.sha256(blob_value).hexdigest()
        )
        assert connection.execute("SELECT metahub_sha256(NULL)").fetchone()[0] is None
        function = connection.execute(
            """
            SELECT narg,flags FROM pragma_function_list
            WHERE name='metahub_sha256'
            """
        ).fetchone()
        assert function is not None
        assert int(function["narg"]) == 1
        assert int(function["flags"]) & 0x800
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("SELECT metahub_sha256(1)").fetchone()


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不超过指定版本的真实迁移，构造 0069 与 0070 独立历史视图。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd077_migration_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """一次构造真实 seeded 0069 模板和包含 0070 up/down 的独立目录。"""

    root = tmp_path_factory.mktemp("prd077-migration-templates")
    migrations_0069 = _migration_subset(root / "migrations-0069", 69)
    migrations_0070 = _migration_subset(root / "migrations-0070", 70)
    database_0069 = root / "template-0069.sqlite3"
    database_url = _url(database_0069)
    assert apply_migrations(database_url, migrations_dir=migrations_0069).applied[-1] == "0069"
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = SEED_PASSWORD
    try:
        apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous
    return {
        "database_0069": database_0069,
        "migrations_0070": migrations_0070,
    }


def _clone_0069(templates: dict[str, Path], destination: Path) -> str:
    """复制 seeded 0069 模板，确保每个升级或回滚用例拥有独立数据库。"""

    shutil.copy2(templates["database_0069"], destination)
    return _url(destination)


def _head(connection: sqlite3.Connection) -> str:
    """读取最高已应用迁移版本，避免仅依据新表是否存在推断数据库头。"""

    row = connection.execute(
        """
        SELECT version FROM schema_migrations
        WHERE status='applied' ORDER BY version DESC LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return str(row[0])


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """同时断言 SQLite 完整性检查通过且不存在任何外键违规记录。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _persistent_schema_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    """冻结完整持久 schema，证明迁移守卫失败未执行任何持久 DDL。"""

    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type,name,tbl_name,sql FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name
            """
        )
    )


def _rbac_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """连同 rowid 冻结 RBAC 目录、角色与关系，防止原子失败后静默重排。"""

    rows: list[tuple[object, ...]] = []
    for table in (
        "rbac_permission_templates",
        "rbac_role_permission_templates",
        "permissions",
        "roles",
        "role_permissions",
    ):
        rows.extend(
            (table, *tuple(row))
            for row in connection.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")
        )
    return tuple(rows)


def _role_relations(
    connection: sqlite3.Connection,
    role_id: str,
) -> tuple[tuple[object, ...], ...]:
    """冻结角色授权关系的物理 rowid、完整列与稳定 permission code。"""

    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT relation.rowid,relation.*,permission.code
            FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.role_id=? ORDER BY relation.rowid
            """,
            (role_id,),
        )
    )


def _customize_role_matrix(
    connection: sqlite3.Connection,
    role_code: str,
    removed_permission: str,
) -> dict[str, object]:
    """删除一项真实授权并写入 canonical hash，返回升级前完整保真快照。"""

    role = connection.execute(
        "SELECT rowid,* FROM roles WHERE code=? ORDER BY tenant_id LIMIT 1",
        (role_code,),
    ).fetchone()
    assert role is not None
    granted_codes = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT permission.code FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
            ORDER BY permission.code
            """,
            (role["tenant_id"], role["role_id"]),
        )
    ]
    assert removed_permission in granted_codes
    changed = connection.execute(
        """
        DELETE FROM role_permissions
        WHERE tenant_id=? AND role_id=? AND permission_id=(
          SELECT permission_id FROM permissions WHERE tenant_id=? AND code=?
        )
        """,
        (role["tenant_id"], role["role_id"], role["tenant_id"], removed_permission),
    ).rowcount
    assert changed == 1
    custom_hash = permission_matrix_sha256(
        code for code in granted_codes if code != removed_permission
    )
    changed = connection.execute(
        """
        UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=?
        WHERE tenant_id=? AND role_id=?
        """,
        (custom_hash, role["tenant_id"], role["role_id"]),
    ).rowcount
    assert changed == 1
    connection.commit()
    current_role = connection.execute(
        "SELECT rowid,* FROM roles WHERE role_id=?", (role["role_id"],)
    ).fetchone()
    assert current_role is not None
    return {
        "role_id": str(role["role_id"]),
        "role_row": tuple(current_role),
        "relations": _role_relations(connection, str(role["role_id"])),
        "matrix_sha256": custom_hash,
    }


def _audit_schema(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """冻结 AuditLog 表、索引和所有 trigger SQL，防止只比较对象数量。"""

    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type,name,tbl_name,sql FROM sqlite_schema
            WHERE name='audit_logs' OR tbl_name='audit_logs'
            ORDER BY type,name
            """
        )
    )


def _seed_legacy_audit(connection: sqlite3.Connection) -> None:
    """在 0069 写入既有审计事实，用于证明 0070 往返不改 rowid 或触发器。"""

    owner = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,user.email,user.role,provider.provider_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN providers provider ON provider.tenant_id=tenant.tenant_id
        ORDER BY tenant.slug='default' DESC,provider.provider_id LIMIT 1
        """
    ).fetchone()
    assert owner is not None
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
          action,object_type,object_id,object_name,before_summary_json,
          after_summary_json,changed_fields_json,client_ip,user_agent,
          request_id,trace_id,created_at
        ) VALUES('aud_prd077_legacy',?,?,?,?,'provider.updated','provider',?,NULL,
                 '{}','{}','[]',NULL,NULL,'req_prd077_legacy','trc_prd077_legacy',
                 '2026-08-11T00:00:00Z')
        """,
        (
            owner["tenant_id"],
            owner["user_id"],
            owner["email"],
            owner["role"],
            owner["provider_id"],
        ),
    )
    connection.commit()


def _install_rbac_drift(connection: sqlite3.Connection, drift_kind: str) -> None:
    """绕开不可变 trigger 注入等量目录或 trigger 漂移，供迁移 preflight 验证。"""

    trigger_names = {
        "permission_metadata": ("trg_rbac_permission_templates_update_prd059",),
        "tenant_permission_metadata": ("trg_permissions_update_prd059",),
        "role_relation": (
            "trg_rbac_role_templates_insert_prd059",
            "trg_rbac_role_templates_delete_prd059",
        ),
        "trigger_body": ("trg_permissions_update_prd059",),
    }[drift_kind]
    trigger_sql: list[str] = []
    for trigger_name in trigger_names:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
            (trigger_name,),
        ).fetchone()
        assert row is not None and row[0]
        trigger_sql.append(str(row[0]))
        connection.execute(f"DROP TRIGGER {trigger_name}")

    # 分别破坏全局权限元数据、租户权限元数据或角色关系，证明 down 不只核对行数。
    if drift_kind == "permission_metadata":
        assert (
            connection.execute(
                """
            UPDATE rbac_permission_templates
            SET description=description || ' [synthetic drift]'
            WHERE permission_code='response_cache.read'
            """
            ).rowcount
            == 1
        )
    # 租户权限漂移必须由 tenant-qualified 目录守卫独立识别，不能被全局模板掩盖。
    elif drift_kind == "tenant_permission_metadata":
        assert (
            connection.execute(
                """
            UPDATE permissions SET name=name || ' [synthetic drift]'
            WHERE tenant_id=(SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1)
              AND code='response_cache.read'
            """
            ).rowcount
            == 1
        )
    # 等量替换默认角色关系，验证迁移按精确矩阵而不是简单计数执行失败关闭。
    elif drift_kind == "role_relation":
        assert (
            connection.execute(
                """
            DELETE FROM rbac_role_permission_templates
            WHERE role_code='viewer' AND permission_code='file.read'
            """
            ).rowcount
            == 1
        )
        connection.execute(
            """
            INSERT INTO rbac_role_permission_templates(
              role_code,permission_code,default_granted
            ) VALUES('viewer','audit.export',1)
            """
        )
    else:
        connection.execute(
            """
            CREATE TRIGGER trg_permissions_update_prd059
            BEFORE UPDATE ON permissions FOR EACH ROW
            BEGIN SELECT 1; END
            """
        )

    if drift_kind != "trigger_body":
        for statement in trigger_sql:
            connection.execute(statement)
    connection.commit()


def _install_prd077_schema_drift(
    connection: sqlite3.Connection,
    drift_kind: str,
) -> None:
    """保持对象名称或数量不变，注入 Assistant schema SQL 漂移。"""

    if drift_kind == "noop_trigger":
        connection.executescript(
            """
            DROP TRIGGER trg_assistant_queries_update_prd077;
            CREATE TRIGGER trg_assistant_queries_update_prd077
            BEFORE UPDATE ON assistant_queries FOR EACH ROW
            BEGIN SELECT 1; END;
            """
        )
    elif drift_kind == "wrong_index":
        connection.executescript(
            """
            DROP INDEX idx_assistant_queries_tenant_time_prd077;
            CREATE INDEX idx_assistant_queries_tenant_time_prd077
              ON assistant_queries(tenant_id,query_type,created_at);
            """
        )
    else:
        connection.execute("ALTER TABLE assistant_queries ADD COLUMN schema_drift_marker INTEGER")
    connection.commit()


def _assert_rbac_head(connection: sqlite3.Connection, *, revision: int) -> None:
    """按 revision 冻结模板权限数、关系数及所有租户物化权限总量。"""

    expected_permissions = 70 if revision == 13 else 69
    expected_relations = 188 if revision == 13 else 185
    assert tuple(
        connection.execute(
            """
            SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision)
            FROM rbac_permission_templates
            """
        ).fetchone()
    ) == (expected_permissions, revision, revision)
    assert (
        connection.execute("SELECT COUNT(*) FROM rbac_role_permission_templates").fetchone()[0]
        == expected_relations
    )
    assert connection.execute("SELECT COUNT(*) FROM permissions").fetchone()[0] == (
        connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] * expected_permissions
    )


@pytest.mark.parametrize(
    "drift_kind",
    (
        "permission_metadata",
        "tenant_permission_metadata",
        "role_relation",
        "trigger_body",
    ),
)
def test_prd077_up_rejects_same_count_rbac_or_trigger_drift_atomically(
    prd077_migration_templates: dict[str, Path],
    tmp_path: Path,
    drift_kind: str,
) -> None:
    """等量 metadata、角色三元组或同名 trigger 漂移不得被标记为 0070。"""

    database_url = _clone_0069(
        prd077_migration_templates,
        tmp_path / f"up-drift-{drift_kind}.sqlite3",
    )
    with connect_database(database_url) as connection:
        _install_rbac_drift(connection, drift_kind)
        schema_before = _persistent_schema_snapshot(connection)
        rbac_before = _rbac_snapshot(connection)
    with pytest.raises(DbError) as error:
        apply_migrations(
            database_url,
            migrations_dir=prd077_migration_templates["migrations_0070"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd077_up_guard_ok" in error.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0069"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name='assistant_queries'"
            ).fetchone()[0]
            == 0
        )
        assert _persistent_schema_snapshot(connection) == schema_before
        assert _rbac_snapshot(connection) == rbac_before
        _assert_healthy(connection)


def test_prd077_fresh_zero_to_70_and_repeat_are_healthy(
    prd077_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证空库真实执行 0 到 70、重复迁移幂等并保持完整性与外键健康。"""

    database = tmp_path / "fresh.sqlite3"
    database_url = _url(database)
    result = apply_migrations(
        database_url,
        migrations_dir=prd077_migration_templates["migrations_0070"],
    )
    assert result.applied == [f"{version:04d}" for version in range(1, 71)]
    assert (
        apply_migrations(
            database_url,
            migrations_dir=prd077_migration_templates["migrations_0070"],
        ).applied
        == []
    )
    with connect_database(database_url) as connection:
        assert _head(connection) == "0070"
        _assert_rbac_head(connection, revision=13)
        _assert_healthy(connection)


def test_prd077_upgrade_expands_only_default_matrices_and_new_tenants(
    prd077_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证真实自定义矩阵逐行保真，同时让默认角色和新租户安全扩权。"""

    database_url = _clone_0069(prd077_migration_templates, tmp_path / "upgrade.sqlite3")
    with connect_database(database_url) as connection:
        custom_roles = (
            _customize_role_matrix(connection, "finance_admin", "response_cache.read"),
            _customize_role_matrix(connection, "security_admin", "session_risk.read"),
        )
    assert apply_migrations(
        database_url,
        migrations_dir=prd077_migration_templates["migrations_0070"],
    ).applied == ["0070"]
    with connect_database(database_url) as connection:
        _assert_rbac_head(connection, revision=13)
        for custom in custom_roles:
            role_id = str(custom["role_id"])
            current_role = connection.execute(
                "SELECT rowid,* FROM roles WHERE role_id=?", (role_id,)
            ).fetchone()
            assert current_role is not None
            assert tuple(current_role) == custom["role_row"]
            assert _role_relations(connection, role_id) == custom["relations"]
            assert (
                connection.execute(
                    """
                SELECT COUNT(*) FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.role_id=? AND permission.code='admin_assistant.query'
                """,
                    (role_id,),
                ).fetchone()[0]
                == 0
            )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM roles role
            JOIN role_permissions relation
              ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE role.code='admin' AND role.matrix_sha256=?
              AND permission.code='admin_assistant.query'
            """,
                (NEW_HASHES["admin"],),
            ).fetchone()[0]
            >= 1
        )
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd077_new','prd077-new','PRD077 New','internal_only','active',
                   '2026-08-11T01:00:00Z','2026-08-11T01:00:00Z')
            """
        )
        connection.commit()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM permissions WHERE tenant_id='tenant_prd077_new'"
            ).fetchone()[0]
            == 70
        )
        for code, matrix_hash in NEW_HASHES.items():
            assert (
                connection.execute(
                    """
                SELECT COUNT(*) FROM roles role
                JOIN role_permissions relation
                  ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE role.tenant_id='tenant_prd077_new' AND role.code=?
                  AND role.matrix_sha256=? AND permission.code='admin_assistant.query'
                """,
                    (code, matrix_hash),
                ).fetchone()[0]
                == 1
            )
        _assert_healthy(connection)


def test_prd077_empty_business_round_trip_preserves_audit_rowids_and_triggers(
    prd077_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证无 PRD-077 业务事实时 0070 正反往返完整保留审计 rowid、索引和触发器。"""

    database_url = _clone_0069(prd077_migration_templates, tmp_path / "round-trip.sqlite3")
    with connect_database(database_url) as connection:
        _seed_legacy_audit(connection)
        audit_schema = _audit_schema(connection)
        audit_rows = tuple(
            tuple(row) for row in connection.execute("SELECT rowid,* FROM audit_logs")
        )
        sequence_rows = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT rowid,* FROM audit_log_export_sequences ORDER BY rowid"
            )
        )
    assert apply_migrations(
        database_url,
        migrations_dir=prd077_migration_templates["migrations_0070"],
    ).applied == ["0070"]
    assert (
        rollback_last_migration(
            database_url,
            migrations_dir=prd077_migration_templates["migrations_0070"],
        ).rolled_back
        == "0070"
    )
    with connect_database(database_url) as connection:
        assert _head(connection) == "0069"
        _assert_rbac_head(connection, revision=12)
        assert _audit_schema(connection) == audit_schema
        assert (
            tuple(tuple(row) for row in connection.execute("SELECT rowid,* FROM audit_logs"))
            == audit_rows
        )
        assert (
            tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT rowid,* FROM audit_log_export_sequences ORDER BY rowid"
                )
            )
            == sequence_rows
        )
        _assert_healthy(connection)
    assert apply_migrations(
        database_url,
        migrations_dir=prd077_migration_templates["migrations_0070"],
    ).applied == ["0070"]
    with connect_database(database_url) as connection:
        assert _head(connection) == "0070"
        _assert_healthy(connection)


def _insert_webhook_endpoint(
    connection: sqlite3.Connection,
    marker: str,
    event_types_json: str,
) -> tuple[str, str]:
    """写入不含敏感明文的隔离 Webhook endpoint，返回租户与 endpoint 标识。"""

    tenant_id = str(
        connection.execute("SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1").fetchone()[0]
    )
    endpoint_id = f"wh_prd077_{marker}"
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_at,updated_at
        ) VALUES(?,?,?,'http://127.0.0.1:9/prd077',?,'enc_v1:fixture',
                 'fixture','fixture','disabled',1000,3,?,?)
        """,
        (endpoint_id, tenant_id, f"PRD077 {marker}", event_types_json, NOW, NOW),
    )
    # 返回同一租户 owner 与 endpoint，后续 delivery 写入不得自行推导或跨租户替换。
    return tenant_id, endpoint_id


def _insert_assistant_delivery(
    connection: sqlite3.Connection,
    tenant_id: str,
    endpoint_id: str,
    marker: str,
    *,
    failed: bool,
) -> str:
    """写入 assistant.queried 投递；failed 终态由既有 trigger 生成死信。"""

    delivery_id = "whd_" + marker * 32
    if failed:
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
              event_timestamp,payload_json,status,attempt_count,max_attempts,error_code,
              delivery_kind,secret_version,created_at,updated_at
            ) VALUES(?,?,?,?,'assistant.queried',?,'{}','failed',3,3,
                     'webhook.delivery_unavailable','original',1,?,?)
            """,
            (delivery_id, tenant_id, endpoint_id, f"event_prd077_{marker}", NOW, NOW, NOW),
        )
    else:
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
              event_timestamp,payload_json,status,attempt_count,max_attempts,
              delivery_kind,secret_version,created_at,updated_at
            ) VALUES(?,?,?,?,'assistant.queried',?,'{}','pending',0,3,
                     'original',1,?,?)
            """,
            (delivery_id, tenant_id, endpoint_id, f"event_prd077_{marker}", NOW, NOW, NOW),
        )
    return delivery_id


def _webhook_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """冻结回滚守卫覆盖的 endpoint、delivery、attempt 与 dead-letter 全部行。"""

    rows: list[tuple[object, ...]] = []
    for table in (
        "webhook_endpoints",
        "webhook_deliveries",
        "webhook_delivery_attempts",
        "webhook_dead_letter_events",
    ):
        rows.extend(
            (table, *tuple(row))
            for row in connection.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")
        )
    return tuple(rows)


def test_prd077_down_rejects_catalog_metadata_drift_atomically(
    prd077_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证 down 的 revision-13 精确目录守卫原子拒绝等量 metadata 漂移。"""

    database_url = _clone_0069(prd077_migration_templates, tmp_path / "down-catalog-drift.sqlite3")
    assert apply_migrations(
        database_url,
        migrations_dir=prd077_migration_templates["migrations_0070"],
    ).applied == ["0070"]
    with connect_database(database_url) as connection:
        _install_rbac_drift(connection, "permission_metadata")
        schema_before = _persistent_schema_snapshot(connection)
        rbac_before = _rbac_snapshot(connection)
    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd077_migration_templates["migrations_0070"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd077_down_schema_invalid" in error.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0070"
        assert _persistent_schema_snapshot(connection) == schema_before
        assert _rbac_snapshot(connection) == rbac_before
        _assert_healthy(connection)


@pytest.mark.parametrize(
    "drift_kind",
    ("noop_trigger", "wrong_index", "table_schema"),
)
def test_prd077_down_rejects_canonical_schema_drift_atomically(
    prd077_migration_templates: dict[str, Path],
    tmp_path: Path,
    drift_kind: str,
) -> None:
    """同名弱 trigger/index 或同名表 SQL 漂移必须在 down 首个 DDL 前失败。"""

    database_url = _clone_0069(
        prd077_migration_templates,
        tmp_path / f"down-schema-drift-{drift_kind}.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd077_migration_templates["migrations_0070"],
    ).applied == ["0070"]
    with connect_database(database_url) as connection:
        _install_prd077_schema_drift(connection, drift_kind)
        schema_before = _persistent_schema_snapshot(connection)
        rbac_before = _rbac_snapshot(connection)
    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd077_migration_templates["migrations_0070"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd077_down_schema_invalid" in error.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0070"
        assert _persistent_schema_snapshot(connection) == schema_before
        assert _rbac_snapshot(connection) == rbac_before
        _assert_healthy(connection)


@pytest.mark.parametrize(
    "fact_kind",
    ("subscription", "delivery", "attempt", "dead_letter", "custom_holder"),
)
def test_prd077_down_business_guards_are_atomic(
    prd077_migration_templates: dict[str, Path],
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """订阅、投递、尝试、死信和自定义 holder 均必须在首个 DDL 前阻断 down。"""

    marker = {
        "subscription": "s",
        "delivery": "d",
        "attempt": "a",
        "dead_letter": "e",
        "custom_holder": "h",
    }[fact_kind]
    database_url = _clone_0069(
        prd077_migration_templates, tmp_path / f"down-business-{fact_kind}.sqlite3"
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd077_migration_templates["migrations_0070"],
    ).applied == ["0070"]
    with connect_database(database_url) as connection:
        if fact_kind == "custom_holder":
            _customize_role_matrix(connection, "finance_admin", "response_cache.read")
        else:
            event_types = (
                '["assistant.queried"]' if fact_kind == "subscription" else '["audit.recorded"]'
            )
            tenant_id, endpoint_id = _insert_webhook_endpoint(connection, marker, event_types)
            delivery_id: str | None = None
            if fact_kind != "subscription":
                delivery_id = _insert_assistant_delivery(
                    connection,
                    tenant_id,
                    endpoint_id,
                    marker,
                    failed=fact_kind == "dead_letter",
                )
            if fact_kind == "attempt":
                connection.execute(
                    """
                    INSERT INTO webhook_delivery_attempts(
                      webhook_delivery_attempt_id,tenant_id,webhook_delivery_id,
                      webhook_endpoint_id,attempt_no,status,error_code,created_at
                    ) VALUES(?,?,?,?,1,'failed','webhook.delivery_unavailable',?)
                    """,
                    (f"wha_prd077_{marker}", tenant_id, delivery_id, endpoint_id, NOW),
                )
            connection.commit()
            if fact_kind == "dead_letter":
                assert (
                    connection.execute(
                        """
                    SELECT COUNT(*) FROM webhook_dead_letter_events
                    WHERE webhook_delivery_id=?
                    """,
                        (delivery_id,),
                    ).fetchone()[0]
                    == 1
                )
        schema_before = _persistent_schema_snapshot(connection)
        rbac_before = _rbac_snapshot(connection)
        webhook_before = _webhook_snapshot(connection)
    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd077_migration_templates["migrations_0070"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd077_down_business_facts_present" in error.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0070"
        assert _persistent_schema_snapshot(connection) == schema_before
        assert _rbac_snapshot(connection) == rbac_before
        assert _webhook_snapshot(connection) == webhook_before
        _assert_healthy(connection)
