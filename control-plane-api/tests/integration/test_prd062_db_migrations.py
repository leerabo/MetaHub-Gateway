"""PRD-062 0054 配置迁移权限与不可变事实专项测试。

测试数据库只由 pytest 的受控临时根创建；执行命令必须把该根设置到
``D:/MetaHub/.artifacts/prd062-*``。夹具仅写固定治理 ID、哈希和计数，不保存配置正文、
Provider Credential、Virtual Key、Session Token、Prompt 或 Response。
"""

from __future__ import annotations

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
    CATALOG_REVISION,
    PERMISSION_CATALOG,
    ROLE_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)

# 生产迁移目录只读使用；历史升级夹具会复制明确版本，避免更高版本改变栈顶语义。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"

# fresh database 必须连续登记 0001..0054，缺号或越级都应立即让专项测试失败。
EXPECTED_VERSIONS = [f"{version:04d}" for version in range(1, 55)]

# 配置迁移使用独立读写权限，不能由 Provider、Model 等普通领域 CRUD 权限推导。
TRANSFER_CODES = frozenset({"config.transfer.read", "config.transfer.write"})
# PRD-062 的 0054 历史态固定为 revision 2；PRD-073 后共享目录已升到 revision 9，
# 因此专项显式排除 Chat、Prompt、策略、意图、会话风险、文件与安全扫描权限，不能改写历史迁移。
PRD062_LATER_PERMISSION_CODES = frozenset(
    {
        "chat.use",
        "prompt_template.read",
        "prompt_template.write",
        "prompt_template.use",
        "policy_template.read",
        "policy_template.apply",
        "intent_risk.classify",
        "session_risk.read",
        "session_risk.manage",
        "file.read",
        "file.write",
        "file.security.scan",
    }
)
PRD062_PERMISSION_CODES = frozenset(
    definition.code
    for definition in PERMISSION_CATALOG
    if definition.code not in PRD062_LATER_PERMISSION_CODES
)
PRD062_ROLE_GRANTABLE_PERMISSIONS = {
    role_code: frozenset(permission_codes - PRD062_LATER_PERMISSION_CODES)
    for role_code, permission_codes in ROLE_GRANTABLE_PERMISSIONS.items()
}

# 旧哈希精确表示 0050 revision 1 默认矩阵，只允许该模板参与无损自动升级。
OLD_HASHES = {
    "admin": "0a7305c09f739695c6255e3a21363c15bade0285074269ce26a98fccc4f44ee2",
    "developer": "9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e",
    "ops_admin": "b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9",
}
# 新哈希固定 revision 2 下三个默认角色的完整授权集，用于识别迁移后显式定制。
NEW_HASHES = {
    "admin": "1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73",
    "developer": "220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f",
    "ops_admin": "a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8",
}
# 固定 UTC 秒只服务约束夹具，避免迁移测试随机器时钟和本地时区产生漂移。
NOW = "2026-08-01T06:00:00Z"


def _sqlite_url(path: Path) -> str:
    """把隔离数据库路径转换为项目迁移器支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不高于指定版本的真实 up/down SQL，构造可重复历史升级目录。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        prefix = source.name[:4]
        if prefix.isdigit() and int(prefix) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """验证 SQLite 内容、外键和迁移换表均保持健康。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _admin(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
    """按 tenant-qualified 条件读取活动管理员，避免夹具掩盖跨租户 owner 缺陷。"""

    row = connection.execute(
        """
        SELECT user_id,email,role FROM users
        WHERE tenant_id=? AND role='admin' AND status='active'
        ORDER BY user_id LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None
    return row


def _role_grants(
    connection: sqlite3.Connection,
    tenant_id: str,
    role_code: str,
) -> set[str]:
    """读取一个租户角色的真实数据库授权集合，不使用 Python 默认值回退。"""

    return {
        row["code"]
        for row in connection.execute(
            """
            SELECT permission.code
            FROM roles role
            JOIN role_permissions relation
              ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE role.tenant_id=? AND role.code=?
            """,
            (tenant_id, role_code),
        )
    }


def _rows(connection: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    """按 rowid 快照受控表；表名只来自测试内部白名单，不能接收外部输入。"""

    assert table in {
        "roles",
        "permissions",
        "role_permissions",
        "rbac_permission_templates",
        "rbac_role_permission_templates",
        "audit_logs",
        "audit_log_export_sequences",
    }
    return [
        tuple(row)
        for row in connection.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")
    ]


def _insert_tenant(connection: sqlite3.Connection, tenant_id: str, slug: str) -> None:
    """创建测试租户；RBAC 初始化 trigger 必须在同一事务生成 revision 2 目录。"""

    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id,slug,name,service_mode,status,created_at,updated_at
        ) VALUES(?,?,?,'internal_only','active',?,?)
        """,
        (tenant_id, slug, f"Tenant {slug}", NOW, NOW),
    )


def _insert_legacy_audit(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
) -> None:
    """写入一个 0053 合法旧动作，验证 AuditLog 换表及 sequence 逐 rowid 保真。"""

    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'provider.updated','provider',?,NULL,'{}','{}','[]',
                 NULL,NULL,?,?,?)
        """,
        (
            "aud_00000000000000000000000000000062",
            tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            "provider_prd062_legacy",
            "req_prd062_legacy_audit",
            "trc_prd062_legacy_audit",
            NOW,
        ),
    )


def test_prd062_fresh_repeat_and_new_tenant_default_matrix(tmp_path: Path) -> None:
    """主流程：fresh/repeat、revision 2 投影和新租户三角色默认授权均准确。"""

    migrations = _migration_subset(tmp_path / "migrations-through-0054", 54)
    database_url = _sqlite_url(tmp_path / "prd062-fresh.db")
    assert apply_migrations(database_url, migrations_dir=migrations).applied == EXPECTED_VERSIONS
    repeat = apply_migrations(database_url, migrations_dir=migrations)
    assert repeat.applied == [] and repeat.skipped == EXPECTED_VERSIONS
    seed = apply_seed_data(database_url)

    connection = connect_database(database_url)
    # 当前运行目录包含 PRD-066..072 的十一项后续权限；数据库子集仍冻结 0054 的 50 项快照。
    assert CATALOG_REVISION == 9
    assert len(PERMISSION_CATALOG) == 62
    assert len(PRD062_PERMISSION_CODES) == 50
    assert {
        definition.code
        for definition in PERMISSION_CATALOG
        if definition.code not in PRD062_PERMISSION_CODES
    } == PRD062_LATER_PERMISSION_CODES
    for tenant_id in (seed.tenant_id, "tenant_prd062_second"):
        # 第二租户必须由 0054 的初始化触发器创建，用于验证新租户也不会复用首租户关系。
        if tenant_id != seed.tenant_id:
            _insert_tenant(connection, tenant_id, "prd062-second")
            connection.commit()
        catalog = connection.execute(
            """
            SELECT COUNT(*) AS total,MIN(catalog_revision) AS minimum,
                   MAX(catalog_revision) AS maximum
            FROM permissions WHERE tenant_id=?
            """,
            (tenant_id,),
        ).fetchone()
        assert tuple(catalog) == (50, 2, 2)
        roles = connection.execute(
            """
            SELECT code,permission_revision,matrix_sha256
            FROM roles WHERE tenant_id=? ORDER BY code
            """,
            (tenant_id,),
        ).fetchall()
        assert {row["code"] for row in roles} == set(ROLE_CODES)
        # 新租户角色矩阵从自身 revision 1 开始；产品 catalog revision 独立为 2。
        assert {row["permission_revision"] for row in roles} == {1}
        for role in roles:
            grants = _role_grants(connection, tenant_id, role["code"])
            assert grants == set(PRD062_ROLE_GRANTABLE_PERMISSIONS[role["code"]])
            assert role["matrix_sha256"] == permission_matrix_sha256(grants)
        assert TRANSFER_CODES <= _role_grants(connection, tenant_id, "admin")
        assert TRANSFER_CODES <= _role_grants(connection, tenant_id, "developer")
        assert TRANSFER_CODES <= _role_grants(connection, tenant_id, "ops_admin")
        for role_code in {"viewer", "finance_admin", "security_admin"}:
            assert _role_grants(connection, tenant_id, role_code).isdisjoint(TRANSFER_CODES)

    plan = " ".join(
        row["detail"]
        for row in connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT config_import_id FROM config_imports
            WHERE tenant_id=? ORDER BY created_at DESC,config_import_id LIMIT 20
            """,
            (seed.tenant_id,),
        )
    )
    assert "idx_config_imports_tenant_created_prd062" in plan
    _assert_healthy(connection)
    connection.close()


def test_prd062_0053_upgrade_custom_matrix_and_empty_roundtrip(tmp_path: Path) -> None:
    """兼容流程：旧默认角色补齐，自定义 Developer 不扩权，空事实 down/re-up 全表保真。"""

    through_0053 = _migration_subset(tmp_path / "migrations-through-0053", 53)
    through_0054 = _migration_subset(tmp_path / "migrations-through-0054", 54)
    database_url = _sqlite_url(tmp_path / "prd062-upgrade-roundtrip.db")
    assert apply_migrations(database_url, migrations_dir=through_0053).applied == EXPECTED_VERSIONS[:-1]
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = _admin(connection, seed.tenant_id)
    _insert_legacy_audit(connection, tenant_id=seed.tenant_id, actor=actor)

    developer = connection.execute(
        "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code='developer'",
        (seed.tenant_id,),
    ).fetchone()
    debug_relation = connection.execute(
        """
        SELECT relation.role_permission_id
        FROM role_permissions relation
        JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=? AND relation.role_id=?
          AND permission.code='debug_console.use'
        """,
        (seed.tenant_id, developer["role_id"]),
    ).fetchone()[0]
    connection.execute(
        "DELETE FROM role_permissions WHERE role_permission_id=?", (debug_relation,)
    )
    custom_codes = _role_grants(connection, seed.tenant_id, "developer")
    custom_hash = permission_matrix_sha256(custom_codes)
    connection.execute(
        """
        UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=?
        WHERE tenant_id=? AND role_id=?
        """,
        (custom_hash, seed.tenant_id, developer["role_id"]),
    )
    connection.commit()
    before = {
        table: _rows(connection, table)
        for table in (
            "roles",
            "permissions",
            "role_permissions",
            "rbac_permission_templates",
            "rbac_role_permission_templates",
            "audit_logs",
            "audit_log_export_sequences",
        )
    }
    connection.close()

    assert apply_migrations(database_url, migrations_dir=through_0054).applied == ["0054"]
    connection = connect_database(database_url)
    custom = connection.execute(
        """
        SELECT permission_revision,matrix_sha256 FROM roles
        WHERE tenant_id=? AND code='developer'
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert tuple(custom) == (2, custom_hash)
    assert _role_grants(connection, seed.tenant_id, "developer").isdisjoint(TRANSFER_CODES)
    for role_code in ("admin", "ops_admin"):
        role = connection.execute(
            """
            SELECT permission_revision,matrix_sha256 FROM roles
            WHERE tenant_id=? AND code=?
            """,
            (seed.tenant_id, role_code),
        ).fetchone()
        assert tuple(role) == (2, NEW_HASHES[role_code])
        assert TRANSFER_CODES <= _role_grants(connection, seed.tenant_id, role_code)
    assert _rows(connection, "audit_logs") == before["audit_logs"]
    assert _rows(connection, "audit_log_export_sequences") == before[
        "audit_log_export_sequences"
    ]
    _assert_healthy(connection)
    connection.close()

    assert rollback_last_migration(
        database_url, migrations_dir=through_0054
    ).rolled_back == "0054"
    connection = connect_database(database_url)
    for table, snapshot in before.items():
        assert _rows(connection, table) == snapshot
    assert not {
        row["name"]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type='table' AND name IN (
              'config_imports','config_import_rows','config_exports'
            )
            """
        )
    }
    _assert_healthy(connection)
    connection.close()

    assert apply_migrations(database_url, migrations_dir=through_0054).applied == ["0054"]
    connection = connect_database(database_url)
    assert _role_grants(connection, seed.tenant_id, "developer").isdisjoint(TRANSFER_CODES)
    assert TRANSFER_CODES <= _role_grants(connection, seed.tenant_id, "admin")
    assert TRANSFER_CODES <= _role_grants(connection, seed.tenant_id, "ops_admin")
    _assert_healthy(connection)
    connection.close()


def _insert_import_fact(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
) -> None:
    """插入一条 Provider created 批次及其行级事实，正文和业务键只保存不可逆哈希。"""

    connection.execute(
        """
        INSERT INTO config_imports(
          config_import_id,tenant_id,actor_user_id,manifest_sha256,schema_version,
          providers_created_count,providers_unchanged_count,
          models_created_count,models_unchanged_count,
          aliases_created_count,aliases_unchanged_count,
          virtual_keys_created_count,virtual_keys_unchanged_count,
          generated_virtual_key_count,disabled_virtual_key_count,
          request_id,trace_id,event_id,created_at
        ) VALUES(?,?,?,?,'metahub.config/v1',1,0,0,0,0,0,0,0,0,0,?,?,?,?)
        """,
        (
            "cimp_00000000000000000000000000000062",
            tenant_id,
            actor["user_id"],
            "a" * 64,
            "req_prd062_import",
            "trc_prd062_import",
            "cievt_00000000000000000000000000000062",
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO config_import_rows(
          tenant_id,config_import_id,collection,row_number,object_type,
          action,object_id,business_key_sha256
        ) VALUES(?,?,'providers',1,'provider','created',?,?)
        """,
        (
            tenant_id,
            "cimp_00000000000000000000000000000062",
            "provider_prd062_created",
            "b" * 64,
        ),
    )
    after = {
        "schema_version": "metahub.config/v1",
        "manifest_sha256": "a" * 64,
        "providers_created_count": 1,
        "providers_unchanged_count": 0,
        "models_created_count": 0,
        "models_unchanged_count": 0,
        "aliases_created_count": 0,
        "aliases_unchanged_count": 0,
        "virtual_keys_created_count": 0,
        "virtual_keys_unchanged_count": 0,
        "generated_virtual_key_count": 0,
        "disabled_virtual_key_count": 0,
    }
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'config.imported','config_transfer',?,NULL,'{}',?,?,
                 NULL,NULL,?,?,?)
        """,
        (
            "aud_00000000000000000000000000000620",
            tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            "cimp_00000000000000000000000000000062",
            json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(sorted(after), ensure_ascii=True, separators=(",", ":")),
            "req_prd062_import",
            "trc_prd062_import",
            NOW,
        ),
    )


def _insert_export_fact(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
) -> None:
    """插入一个零对象导出及精确 AuditLog；导出正文不进入任何持久字段。"""

    connection.execute(
        """
        INSERT INTO config_exports(
          config_export_id,tenant_id,actor_user_id,schema_version,manifest_sha256,
          providers_count,models_count,aliases_count,virtual_keys_count,
          request_id,trace_id,created_at
        ) VALUES(?,?,?,'metahub.config/v1',?,0,0,0,0,?,?,?)
        """,
        (
            "cexp_00000000000000000000000000000062",
            tenant_id,
            actor["user_id"],
            "c" * 64,
            "req_prd062_export",
            "trc_prd062_export",
            NOW,
        ),
    )
    after = {
        "schema_version": "metahub.config/v1",
        "manifest_sha256": "c" * 64,
        "providers_count": 0,
        "models_count": 0,
        "aliases_count": 0,
        "virtual_keys_count": 0,
    }
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'config.exported','config_transfer',?,NULL,'{}',?,?,
                 NULL,NULL,?,?,?)
        """,
        (
            "aud_00000000000000000000000000000621",
            tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            "cexp_00000000000000000000000000000062",
            json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(sorted(after), ensure_ascii=True, separators=(",", ":")),
            "req_prd062_export",
            "trc_prd062_export",
            NOW,
        ),
    )


def test_prd062_fact_owner_immutability_audit_and_down_block(tmp_path: Path) -> None:
    """安全流程：同租户事实与审计成功，跨租户/篡改失败，down 有证据时原子阻断。"""

    migrations = _migration_subset(tmp_path / "migrations-through-0054", 54)
    database_url = _sqlite_url(tmp_path / "prd062-facts.db")
    apply_migrations(database_url, migrations_dir=migrations)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = _admin(connection, seed.tenant_id)
    _insert_import_fact(connection, tenant_id=seed.tenant_id, actor=actor)
    _insert_export_fact(connection, tenant_id=seed.tenant_id, actor=actor)
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="config_import_immutable"):
        connection.execute(
            "UPDATE config_imports SET trace_id=? WHERE config_import_id=?",
            ("trc_tampered", "cimp_00000000000000000000000000000062"),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="config_import_row_immutable"):
        connection.execute(
            "DELETE FROM config_import_rows WHERE config_import_id=?",
            ("cimp_00000000000000000000000000000062",),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="config_export_immutable"):
        connection.execute(
            "DELETE FROM config_exports WHERE config_export_id=?",
            ("cexp_00000000000000000000000000000062",),
        )
    connection.rollback()

    _insert_tenant(connection, "tenant_prd062_foreign", "prd062-foreign")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO config_exports(
              config_export_id,tenant_id,actor_user_id,schema_version,manifest_sha256,
              providers_count,models_count,aliases_count,virtual_keys_count,
              request_id,trace_id,created_at
            ) VALUES(?,?,?,'metahub.config/v1',?,0,0,0,0,?,?,?)
            """,
            (
                "cexp_prd062_cross_tenant",
                "tenant_prd062_foreign",
                actor["user_id"],
                "d" * 64,
                "req_prd062_cross",
                "trc_prd062_cross",
                NOW,
            ),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO config_imports(
              config_import_id,tenant_id,actor_user_id,manifest_sha256,schema_version,
              providers_created_count,providers_unchanged_count,
              models_created_count,models_unchanged_count,
              aliases_created_count,aliases_unchanged_count,
              virtual_keys_created_count,virtual_keys_unchanged_count,
              generated_virtual_key_count,disabled_virtual_key_count,
              request_id,trace_id,event_id,created_at
            ) VALUES(?,?,?,?,'metahub.config/v1',-1,0,0,0,0,0,0,0,0,0,?,?,?,?)
            """,
            (
                "cimp_prd062_negative",
                seed.tenant_id,
                actor["user_id"],
                "e" * 64,
                "req_prd062_negative",
                "trc_prd062_negative",
                "cievt_prd062_negative",
                NOW,
            ),
        )
    connection.rollback()

    import_plan = " ".join(
        row["detail"]
        for row in connection.execute(
            """
            EXPLAIN QUERY PLAN SELECT config_import_id FROM config_imports
            WHERE tenant_id=? ORDER BY created_at DESC,config_import_id
            """,
            (seed.tenant_id,),
        )
    )
    export_plan = " ".join(
        row["detail"]
        for row in connection.execute(
            """
            EXPLAIN QUERY PLAN SELECT config_export_id FROM config_exports
            WHERE tenant_id=? ORDER BY created_at DESC,config_export_id
            """,
            (seed.tenant_id,),
        )
    )
    assert "idx_config_imports_tenant_created_prd062" in import_plan
    assert "idx_config_exports_tenant_created_prd062" in export_plan
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE object_type='config_transfer'"
    ).fetchone()[0] == 2
    _assert_healthy(connection)
    connection.close()

    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url, migrations_dir=migrations)
    assert exc.value.code == "db.migration_failed"
    assert "prd062_down_business_facts_present" in exc.value.message
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0054'"
    ).fetchone()[0] == "applied"
    assert connection.execute("SELECT COUNT(*) FROM config_imports").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM config_exports").fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE object_type='config_transfer'"
    ).fetchone()[0] == 2
    _assert_healthy(connection)
    connection.close()
