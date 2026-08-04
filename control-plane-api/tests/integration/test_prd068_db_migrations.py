"""PRD-068 0060 策略模板、RBAC、mask 证据和安全降级专项测试。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

# 迁移目录、前后角色哈希和模板/权限枚举共同冻结 0060 的产品数据合同与回滚预期。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
REVISION_4_HASHES = {
    "admin": "e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320",
    "developer": "2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c",
    "viewer": "3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086",
    "finance_admin": "181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be",
    "security_admin": "ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c",
    "ops_admin": "a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8",
}
REVISION_5_HASHES = {
    "admin": "64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655",
    "developer": REVISION_4_HASHES["developer"],
    "viewer": REVISION_4_HASHES["viewer"],
    "finance_admin": "96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114",
    "security_admin": "b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160",
    "ops_admin": "6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b",
}
TEMPLATE_CODES = (
    "budget_burst_guard",
    "customer_service_redaction",
    "domestic_models_only",
    "high_security_mode",
    "low_cost_mode",
)
NEW_PERMISSION_CODES = ("policy_template.apply", "policy_template.read")


def _url(path: Path) -> str:
    """返回当前用例独占 SQLite 文件的迁移 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制指定栈顶前的正式迁移，避免测试维护第二份 SQL。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd068_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """构建真实经历 seed 与 revision 演进的 0059 模板和 0060 迁移目录。"""

    root = tmp_path_factory.mktemp("prd068-migration-templates")
    migrations_0053 = _migration_subset(root / "migrations-0053", 53)
    migrations_0059 = _migration_subset(root / "migrations-0059", 59)
    migrations_0060 = _migration_subset(root / "migrations-0060", 60)
    database_0059 = root / "template-0059.db"
    database_url = _url(database_0059)

    assert apply_migrations(
        database_url, migrations_dir=migrations_0053
    ).applied[-1] == "0053"
    apply_seed_data(database_url)
    assert apply_migrations(
        database_url, migrations_dir=migrations_0059
    ).applied[-1] == "0059"
    return {
        "database_0059": database_0059,
        "migrations_0060": migrations_0060,
    }


def _clone(template: Path, destination: Path) -> str:
    """复制已关闭的模板数据库并返回独占 URL。"""

    shutil.copy2(template, destination)
    return _url(destination)


def _rows(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[Any, ...], ...]:
    """按 rowid 冻结表的全部字段，验证往返不改写历史顺序或 NULL。"""

    return tuple(
        tuple(row)
        for row in connection.execute(
            f'SELECT rowid,* FROM "{table}" ORDER BY rowid'
        ).fetchall()
    )


def _schema_objects(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, str], ...]:
    """冻结表及其本地 index/trigger SQL，防止恢复时静默丢约束。"""

    return tuple(
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """
            SELECT type,name,sql FROM sqlite_schema
            WHERE (type='table' AND name=?)
               OR (type IN ('index','trigger') AND tbl_name=? AND sql IS NOT NULL)
            ORDER BY type,name
            """,
            (table, table),
        ).fetchall()
    )


def _healthy(connection: sqlite3.Connection) -> None:
    """验证 SQLite 页面完整性与全部 tenant-qualified 外键。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _identity(connection: sqlite3.Connection) -> dict[str, str]:
    """返回 seed Admin 的租户、用户、邮箱和角色，供 owner 约束测试复用。"""

    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,user.email,user.role
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id
        WHERE tenant.slug='default' AND user.role='admin' AND user.status='active'
        """
    ).fetchone()
    assert row is not None
    return {
        "tenant_id": str(row[0]),
        "user_id": str(row[1]),
        "email": str(row[2]),
        "role": str(row[3]),
    }


def _matrix_hash(codes: list[str]) -> str:
    """按 Shared 规范计算自定义矩阵 hash，不依赖当前 revision 5 目录校验。"""

    payload = json.dumps(
        sorted(codes), ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _resource_graph(connection: sqlite3.Connection) -> dict[str, str]:
    """创建同租户 Project 与 Alias，供 application/依赖触发器专项复用。"""

    identity = _identity(connection)
    environment = connection.execute(
        """
        SELECT environment_id,code FROM environments
        WHERE tenant_id=? AND status='active' ORDER BY environment_id LIMIT 1
        """,
        (identity["tenant_id"],),
    ).fetchone()
    model = connection.execute(
        """
        SELECT model_id FROM models
        WHERE tenant_id=? AND status='active' ORDER BY model_id LIMIT 1
        """,
        (identity["tenant_id"],),
    ).fetchone()
    assert environment is not None and model is not None
    graph = {
        **identity,
        "environment_id": str(environment[0]),
        "environment_code": str(environment[1]),
        "model_id": str(model[0]),
        "project_id": "prj_prd068",
        "model_alias_id": "mal_prd068",
        "model_alias": "prd068-domestic-alias",
    }
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,
          status,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'PRD-068','迁移专项资源图',?,'active',?,?,?,?)
        """,
        (
            graph["project_id"],
            graph["tenant_id"],
            "prd068",
            graph["user_id"],
            graph["user_id"],
            graph["user_id"],
            "2026-08-03T03:30:00Z",
            "2026-08-03T03:30:00Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,default_model_id,
          fallback_model_id,status,description,created_by,disabled_at,
          disabled_by,created_at,updated_at,environment_id
        ) VALUES(?,?,?,?,?,NULL,'active','PRD-068 Alias',?,NULL,NULL,?,?,?)
        """,
        (
            graph["model_alias_id"],
            graph["tenant_id"],
            graph["model_alias"],
            graph["environment_code"],
            graph["model_id"],
            graph["user_id"],
            "2026-08-03T03:30:00Z",
            "2026-08-03T03:30:00Z",
            graph["environment_id"],
        ),
    )
    connection.commit()
    return graph


def _apply_0060(database_url: str, migrations: Path) -> None:
    """应用且只应用 0060，测试失败时直接显示错误 SQL。"""

    assert apply_migrations(
        database_url, migrations_dir=migrations
    ).applied == ["0060"]


def _insert_synthetic_application(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor_user_id: str,
    project_id: str,
) -> str:
    """绕过服务写序仅插入约束完整的测试 application，供数据库 trigger 负例使用。

    该 helper 只在独占临时数据库中使用；关闭 FK 并删除 insert owner trigger 是为了避免
    构造完整 Policy/Snapshot/Audit 环，而 application 自身的 CHECK/UNIQUE 仍真实生效。
    """

    application_id = "ptapp_" + "a" * 32
    connection.execute(
        "DROP TRIGGER trg_policy_template_applications_owner_insert_prd068"
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        """
        INSERT INTO policy_template_applications(
          application_id,tenant_id,policy_template_id,template_version,
          template_definition_hash,project_id,actor_user_id,request_id,trace_id,
          request_fingerprint,preview_nonce,preview_as_of,applied_as_of,
          bindings_json,bindings_hash,impact_hash,applicability_fingerprint,
          before_candidate_hash,after_candidate_hash,previous_snapshot_id,
          previous_snapshot_version,result_snapshot_id,result_snapshot_version,
          generated_policy_id,generated_policy_version,status,
          policy_audit_log_id,policy_event_id,snapshot_audit_log_id,
          snapshot_event_id,apply_audit_log_id,template_event_id,created_at
        )
        SELECT
          ?,tenant_id,policy_template_id,version,definition_hash,?,?,?,
          'trace_prd068_application_guard',?,?,?,?,?,?,?,?,?,?,
          'psnap_prd068_previous',1,'psnap_prd068_result',2,
          'pol_prd068_generated',1,'applied',
          'aud_prd068_policy','pevt_prd068_policy','aud_prd068_snapshot',
          'psevt_prd068_snapshot','aud_prd068_apply','ptevt_prd068_apply',?
        FROM policy_templates
        WHERE tenant_id=? AND template_code='domestic_models_only'
        """,
        (
            application_id,
            project_id,
            actor_user_id,
            "request_prd068_application_guard",
            "a" * 64,
            "b" * 64,
            "2026-08-03T01:00:00Z",
            "2026-08-03T01:00:01Z",
            json.dumps({"project_id": project_id}, separators=(",", ":")),
            "c" * 64,
            "d" * 64,
            "e" * 64,
            "f" * 64,
            "1" * 64,
            "2026-08-03T01:00:01Z",
            tenant_id,
        ),
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")
    return application_id


def _synthetic_application_summary(
    application: sqlite3.Row,
    **overrides: object,
) -> dict[str, object]:
    """生成模板 apply 审计的固定安全摘要，并允许负例只篡改单个受保护字段。"""

    summary: dict[str, object] = {
        "status": "applied",
        "template_code": str(application["template_code"]),
        "template_version": int(application["template_version"]),
        "application_id": str(application["application_id"]),
        "generated_policy_id": str(application["generated_policy_id"]),
        "previous_snapshot_version": int(application["previous_snapshot_version"]),
        "result_snapshot_version": int(application["result_snapshot_version"]),
        "impact_hash": str(application["impact_hash"]),
        "bindings_hash": str(application["bindings_hash"]),
        "changed_resource_types": "policy,policy_snapshot",
    }
    summary.update(overrides)
    return summary


def test_prd068_event_payload_requires_exact_changed_resource_types(
    prd068_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """安全流程：事件数组必须精确为 Policy 与 Snapshot，不能用同长度伪造值。"""

    database_url = _clone(
        prd068_templates["database_0059"], tmp_path / "event-payload-guard.db"
    )
    _apply_0060(database_url, prd068_templates["migrations_0060"])
    with connect_database(database_url) as connection:
        graph = _resource_graph(connection)
        application_id = _insert_synthetic_application(
            connection,
            tenant_id=graph["tenant_id"],
            actor_user_id=graph["user_id"],
            project_id=graph["project_id"],
        )
        application = connection.execute(
            """
            SELECT application.*,template.template_code
            FROM policy_template_applications application
            JOIN policy_templates template
              ON template.tenant_id=application.tenant_id
             AND template.policy_template_id=application.policy_template_id
            WHERE application.tenant_id=? AND application.application_id=?
            """,
            (graph["tenant_id"], application_id),
        ).fetchone()
        assert application is not None
        payload = {
            "template_code": str(application["template_code"]),
            "template_version": int(application["template_version"]),
            "application_id": application_id,
            "generated_policy_id": str(application["generated_policy_id"]),
            "previous_snapshot_version": int(application["previous_snapshot_version"]),
            "result_snapshot_version": int(application["result_snapshot_version"]),
            "impact_hash": str(application["impact_hash"]),
            "bindings_hash": str(application["bindings_hash"]),
            "changed_resource_types": ["policy", "provider"],
        }
        # 本用例专测 payload CHECK；关闭 FK 只绕过尚未构造的 Audit/Snapshot 测试边，
        # 不影响当前表的 CHECK 与 owner trigger。
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        insert_sql = """
            INSERT INTO policy_template_events(
              policy_template_event_id,tenant_id,application_id,policy_template_id,
              template_version,result_snapshot_id,result_snapshot_version,event_type,
              actor_user_id,audit_log_id,request_id,trace_id,payload_json,created_at
            ) VALUES(?,?,?,?,?,?,?,'policy_template.applied',?,?,?,?,?,?)
        """
        values = (
            application["template_event_id"],
            application["tenant_id"],
            application_id,
            application["policy_template_id"],
            application["template_version"],
            application["result_snapshot_id"],
            application["result_snapshot_version"],
            application["actor_user_id"],
            application["apply_audit_log_id"],
            application["request_id"],
            application["trace_id"],
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            application["applied_as_of"],
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(insert_sql, values)

        # 同一 owner 图仅改回权威数组后必须可写，证明门禁没有误伤合法 source event。
        payload["changed_resource_types"] = ["policy", "policy_snapshot"]
        valid_values = (
            *values[:-2],
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            values[-1],
        )
        connection.execute(insert_sql, valid_values)
        assert connection.execute(
            "SELECT COUNT(*) FROM policy_template_events WHERE tenant_id=?",
            (graph["tenant_id"],),
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("status", "rolled_back"),
        ("template_version", 2),
        ("previous_snapshot_version", 99),
        ("changed_resource_types", "policy,provider"),
    ),
)
def test_prd068_template_audit_binds_exact_application_summary(
    prd068_templates: dict[str, Path],
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    """安全流程：模板审计关键值必须逐项来自同租户 application，不能只校验字段名。"""

    database_url = _clone(
        prd068_templates["database_0059"], tmp_path / f"audit-summary-{field}.db"
    )
    _apply_0060(database_url, prd068_templates["migrations_0060"])
    with connect_database(database_url) as connection:
        graph = _resource_graph(connection)
        application_id = _insert_synthetic_application(
            connection,
            tenant_id=graph["tenant_id"],
            actor_user_id=graph["user_id"],
            project_id=graph["project_id"],
        )
        application = connection.execute(
            """
            SELECT application.*,template.template_code
            FROM policy_template_applications application
            JOIN policy_templates template
              ON template.tenant_id=application.tenant_id
             AND template.policy_template_id=application.policy_template_id
            WHERE application.tenant_id=? AND application.application_id=?
            """,
            (graph["tenant_id"], application_id),
        ).fetchone()
        assert application is not None
        summary = _synthetic_application_summary(application, **{field: invalid_value})
        insert_sql = """
            INSERT INTO audit_logs(
              audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
              action,object_type,object_id,object_name,before_summary_json,
              after_summary_json,changed_fields_json,request_id,trace_id,created_at
            ) VALUES(
              ?,?,?,?,?,'policy_template.applied','policy_template_application',?,NULL,
              ?,?,?,?,?,?
            )
        """
        values = (
            application["apply_audit_log_id"],
            graph["tenant_id"],
            graph["user_id"],
            graph["email"],
            graph["role"],
            application_id,
            "{}",
            json.dumps(summary, sort_keys=True, separators=(",", ":")),
            json.dumps(sorted(summary), separators=(",", ":")),
            application["request_id"],
            application["trace_id"],
            application["applied_as_of"],
        )
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            connection.execute(insert_sql, values)

        valid_summary = _synthetic_application_summary(application)
        valid_values = (
            *values[:7],
            json.dumps(valid_summary, sort_keys=True, separators=(",", ":")),
            json.dumps(sorted(valid_summary), separators=(",", ":")),
            *values[9:],
        )
        connection.execute(insert_sql, valid_values)
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE tenant_id=? AND audit_log_id=?",
            (graph["tenant_id"], application["apply_audit_log_id"]),
        ).fetchone()[0] == 1


def test_prd068_upgrade_seeds_five_templates_and_revision5_rbac(
    prd068_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """主流程：现有/新 tenant 均获五模板，四类管理员获权且两低权角色不扩权。"""

    database_url = _clone(
        prd068_templates["database_0059"], tmp_path / "upgrade.db"
    )
    _apply_0060(database_url, prd068_templates["migrations_0060"])
    with connect_database(database_url) as connection:
        tenant_count = int(connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0])
        assert [
            tuple(row)
            for row in connection.execute(
            """
            SELECT template_code FROM policy_templates
            GROUP BY template_code ORDER BY template_code
            """
            ).fetchall()
        ] == [(code,) for code in TEMPLATE_CODES]
        assert connection.execute(
            "SELECT COUNT(*) FROM policy_templates"
        ).fetchone()[0] == tenant_count * 5
        assert connection.execute(
            """
            SELECT COUNT(*) FROM policy_templates
            WHERE (template_code='low_cost_mode' AND priority=1000)
               OR (template_code!='low_cost_mode' AND priority=10)
            """
        ).fetchone()[0] == tenant_count * 5
        assert [
            tuple(row)
            for row in connection.execute(
                """
                SELECT permission_code,catalog_revision
                FROM rbac_permission_templates
                WHERE permission_code LIKE 'policy_template.%'
                ORDER BY permission_code
                """
            ).fetchall()
        ] == [(code, 5) for code in NEW_PERMISSION_CODES]

        grants = {
            str(row[0]): {str(item[0]) for item in connection.execute(
                """
                SELECT permission.code
                FROM roles role
                JOIN role_permissions relation
                  ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE role.role_id=? AND permission.code LIKE 'policy_template.%'
                """,
                (row[1],),
            ).fetchall()}
            for row in connection.execute(
                "SELECT code,role_id FROM roles "
                "WHERE tenant_id=(SELECT tenant_id FROM tenants WHERE slug='default')"
            ).fetchall()
        }
        for role_code in ("admin", "finance_admin", "security_admin", "ops_admin"):
            assert grants[role_code] == set(NEW_PERMISSION_CODES)
        assert grants["developer"] == set()
        assert grants["viewer"] == set()

        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd068_future','prd068-future','PRD-068 Future',
                   'internal_only','active',?,?)
            """,
            ("2026-08-03T02:00:00Z", "2026-08-03T02:00:00Z"),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM policy_templates WHERE tenant_id='tenant_prd068_future'"
        ).fetchone()[0] == 5
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE tenant_id='tenant_prd068_future'"
        ).fetchone()[0] == 56
        for operation in (
            "UPDATE policy_templates SET name='tampered' WHERE tenant_id='tenant_prd068_future'",
            "DELETE FROM policy_templates WHERE tenant_id='tenant_prd068_future'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="policy_template_immutable"):
                connection.execute(operation)
        _healthy(connection)


def test_prd068_round_trip_preserves_rbac_audit_and_requestlog(
    prd068_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """兼容流程：无业务事实时 0059→0060→0059 逐行、rowid 与 schema 完整恢复。"""

    database_url = _clone(
        prd068_templates["database_0059"], tmp_path / "round-trip.db"
    )
    with connect_database(database_url) as connection:
        before_rows = {
            table: _rows(connection, table)
            for table in (
                "rbac_permission_templates",
                "rbac_role_permission_templates",
                "permissions",
                "roles",
                "role_permissions",
                "audit_logs",
                "request_logs",
            )
        }
        before_schema = {
            table: _schema_objects(connection, table)
            for table in ("audit_logs", "request_logs")
        }

    _apply_0060(database_url, prd068_templates["migrations_0060"])
    assert rollback_last_migration(
        database_url, migrations_dir=prd068_templates["migrations_0060"]
    ).rolled_back == "0060"

    with connect_database(database_url) as connection:
        for table, expected in before_rows.items():
            assert _rows(connection, table) == expected
        for table, expected in before_schema.items():
            assert _schema_objects(connection, table) == expected
        assert connection.execute(
            "SELECT COUNT(*) FROM pragma_table_info('request_logs')"
        ).fetchone()[0] == 50
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_schema
            WHERE name LIKE '%prd068'
               OR name IN (
                 'policy_templates','policy_template_applications',
                 'policy_template_application_items',
                 'policy_template_application_dependencies','policy_template_events'
               )
            """
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0060'"
        ).fetchone()[0] == "rolled_back"
        _healthy(connection)


def test_prd068_upgrade_preserves_custom_finance_matrix(
    prd068_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """权限流程：迁移只扩展 canonical Finance，租户已定制矩阵不得被静默恢复权限。"""

    database_url = _clone(
        prd068_templates["database_0059"], tmp_path / "custom-matrix.db"
    )
    with connect_database(database_url) as connection:
        tenant_id = str(connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default'"
        ).fetchone()[0])
        role_id, revision = connection.execute(
            "SELECT role_id,permission_revision FROM roles "
            "WHERE tenant_id=? AND code='finance_admin'",
            (tenant_id,),
        ).fetchone()
        relation_id = connection.execute(
            """
            SELECT relation.role_permission_id
            FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
              AND permission.code='billing.anomaly.write'
            """,
            (tenant_id, role_id),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM role_permissions WHERE role_permission_id=?", (relation_id,)
        )
        codes = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT permission.code
                FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=?
                ORDER BY permission.code
                """,
                (tenant_id, role_id),
            ).fetchall()
        ]
        custom_hash = _matrix_hash(codes)
        connection.execute(
            """
            UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            (
                int(revision) + 1,
                custom_hash,
                "2026-08-03T03:00:00Z",
                tenant_id,
                role_id,
            ),
        )

    _apply_0060(database_url, prd068_templates["migrations_0060"])
    with connect_database(database_url) as connection:
        role = connection.execute(
            """
            SELECT permission_revision,matrix_sha256 FROM roles
            WHERE tenant_id=? AND role_id=?
            """,
            (tenant_id, role_id),
        ).fetchone()
        assert tuple(role) == (int(revision) + 1, custom_hash)
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
              AND permission.code LIKE 'policy_template.%'
            """,
            (tenant_id, role_id),
        ).fetchone()[0] == 0
        _healthy(connection)

    assert rollback_last_migration(
        database_url, migrations_dir=prd068_templates["migrations_0060"]
    ).rolled_back == "0060"
    with connect_database(database_url) as connection:
        restored = connection.execute(
            "SELECT permission_revision,matrix_sha256 FROM roles WHERE role_id=?",
            (role_id,),
        ).fetchone()
        assert restored is not None
        assert tuple(restored) == (int(revision) + 1, custom_hash)
        _healthy(connection)


def test_prd068_alias_status_is_frozen_but_new_environment_is_allowed(
    prd068_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """交叉审查：Alias 任意状态变化失败关闭，而新 Environment 依靠 DSL 默认 deny。"""

    database_url = _clone(
        prd068_templates["database_0059"], tmp_path / "alias-environment.db"
    )
    _apply_0060(database_url, prd068_templates["migrations_0060"])
    with connect_database(database_url) as connection:
        graph = _resource_graph(connection)
        application_id = _insert_synthetic_application(
            connection,
            tenant_id=graph["tenant_id"],
            actor_user_id=graph["user_id"],
            project_id=graph["project_id"],
        )
        used_codes = {
            str(row[0])
            for row in connection.execute(
                "SELECT code FROM environments WHERE tenant_id=?",
                (graph["tenant_id"],),
            ).fetchall()
        }
        future_code = next(code for code in ("test", "prod") if code not in used_codes)
        connection.execute(
            """
            INSERT INTO policy_template_application_dependencies(
              application_dependency_id,tenant_id,application_id,resource_type,
              resource_id,relation,resource_name,version_or_hash,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "ptdep_" + "b" * 32,
                graph["tenant_id"],
                application_id,
                "model_alias",
                graph["model_alias_id"],
                "domestic_allowed_alias",
                graph["model_alias"],
                "alias-fingerprint-v1",
                "2026-08-03T04:00:00Z",
            ),
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="policy_template_dependency_immutable"
        ):
            connection.execute(
                "UPDATE model_aliases SET status=? WHERE tenant_id=? AND model_alias_id=?",
                ("disabled", graph["tenant_id"], graph["model_alias_id"]),
            )

        connection.execute(
            """
            INSERT INTO environments(
              environment_id,tenant_id,code,name,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "env_prd068_future",
                graph["tenant_id"],
                future_code,
                "PRD-068 Future",
                "active",
                "2026-08-03T04:01:00Z",
                "2026-08-03T04:01:00Z",
            ),
        )
        assert connection.execute(
            "SELECT status FROM environments WHERE environment_id='env_prd068_future'"
        ).fetchone()[0] == "active"


@pytest.mark.parametrize(
    "fact_kind",
    ("application", "audit", "webhook", "permission", "request_mask", "active_mask"),
)
def test_prd068_down_business_facts_fail_before_first_ddl(
    prd068_templates: dict[str, Path],
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """异常流程：任一治理事实都以稳定错误阻断 down，0060 schema 与事实保持原样。"""

    database_url = _clone(
        prd068_templates["database_0059"], tmp_path / f"guard-{fact_kind}.db"
    )
    _apply_0060(database_url, prd068_templates["migrations_0060"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        tenant_id = identity["tenant_id"]
        if fact_kind == "application":
            graph = _resource_graph(connection)
            _insert_synthetic_application(
                connection,
                tenant_id=tenant_id,
                actor_user_id=identity["user_id"],
                project_id=graph["project_id"],
            )
        elif fact_kind == "audit":
            connection.execute(
                "DROP TRIGGER trg_audit_logs_policy_template_owner_insert_prd068"
            )
            connection.execute(
                """
                INSERT INTO audit_logs(
                  audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
                  action,object_type,object_id,object_name,before_summary_json,
                  after_summary_json,changed_fields_json,request_id,trace_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,NULL,'{}','{}','[]',?,?,?)
                """,
                (
                    "aud_prd068_guard",
                    tenant_id,
                    identity["user_id"],
                    identity["email"],
                    identity["role"],
                    "policy_template.applied",
                    "policy_template_application",
                    "ptapp_prd068_guard",
                    "request_prd068_audit_guard",
                    "trace_prd068_audit_guard",
                    "2026-08-03T05:00:00Z",
                ),
            )
        elif fact_kind == "webhook":
            connection.execute(
                """
                INSERT INTO webhook_endpoints(
                  webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
                  encrypted_secret,secret_prefix,secret_fingerprint,status,
                  timeout_ms,max_attempts,created_by,updated_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "whep_prd068_guard",
                    tenant_id,
                    "PRD-068 Guard",
                    "https://example.invalid/prd068",
                    '["policy_template.applied"]',
                    "aesgcm-v1:guard",
                    "guard",
                    "f" * 64,
                    "active",
                    5000,
                    3,
                    identity["user_id"],
                    identity["user_id"],
                    "2026-08-03T05:00:00Z",
                    "2026-08-03T05:00:00Z",
                ),
            )
        # 自定义 Finance hash 会让其脱离可收缩集合，验证 down 不会删除显式授权矩阵。
        elif fact_kind == "permission":
            connection.execute(
                """
                UPDATE roles
                SET permission_revision=permission_revision+1,
                    matrix_sha256=?,updated_at=?
                WHERE tenant_id=? AND code='finance_admin'
                """,
                ("f" * 64, "2026-08-03T05:00:00Z", tenant_id),
            )
        elif fact_kind == "request_mask":
            connection.execute("DROP TRIGGER trg_request_logs_claim_insert")
            connection.execute(
                """
                INSERT INTO request_logs(
                  request_id,tenant_id,trace_id,status,created_at,
                  policy_mask_applied,policy_mask_policy_id
                ) VALUES(?,?,?,?,?,1,'pol_prd068_mask_guard')
                """,
                (
                    "request_prd068_mask_guard",
                    tenant_id,
                    "trace_prd068_mask_guard",
                    "failed",
                    "2026-08-03T05:00:00Z",
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO policies(
                  policy_id,tenant_id,name,description,status,priority,version,
                  dsl_version,created_by,updated_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,10,1,'policy-dsl-v1',?,?,?,?)
                """,
                (
                    "pol_prd068_mask_guard",
                    tenant_id,
                    "PRD-068 Mask Guard",
                    "down guard",
                    "active",
                    identity["user_id"],
                    identity["user_id"],
                    "2026-08-03T05:00:00Z",
                    "2026-08-03T05:00:00Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO policy_actions(
                  policy_action_id,tenant_id,policy_id,action_type,target_model_id,
                  created_at,updated_at
                ) VALUES(?,?,?,'mask',NULL,?,?)
                """,
                (
                    "pact_prd068_mask_guard",
                    tenant_id,
                    "pol_prd068_mask_guard",
                    "2026-08-03T05:00:00Z",
                    "2026-08-03T05:00:00Z",
                ),
            )
        connection.commit()

    with pytest.raises(DbError, match="prd068_down_business_facts_present"):
        rollback_last_migration(
            database_url, migrations_dir=prd068_templates["migrations_0060"]
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0060'"
        ).fetchone()[0] == "applied"
        assert connection.execute(
            "SELECT COUNT(*) FROM policy_templates"
        ).fetchone()[0] > 0


def test_prd068_unknown_dependency_blocks_down_atomically(
    prd068_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """兼容流程：未知后续 view 引用模板 schema 时必须原子拒绝降级。"""

    database_url = _clone(
        prd068_templates["database_0059"], tmp_path / "unknown-dependency.db"
    )
    _apply_0060(database_url, prd068_templates["migrations_0060"])
    with connect_database(database_url) as connection:
        connection.execute(
            """
            CREATE VIEW future_policy_template_projection AS
            SELECT tenant_id,policy_template_id FROM policy_templates
            """
        )
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, migrations_dir=prd068_templates["migrations_0060"]
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_schema
            WHERE type='view' AND name='future_policy_template_projection'
            """
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0060'"
        ).fetchone()[0] == "applied"


def test_prd068_application_schema_freezes_owner_and_deferred_edges(
    prd068_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """结构流程：唯一定位、active Project 与仅未来事实 deferred FK 均落入正式 DDL。"""

    database_url = _clone(
        prd068_templates["database_0059"], tmp_path / "application-contract.db"
    )
    _apply_0060(database_url, prd068_templates["migrations_0060"])
    with connect_database(database_url) as connection:
        sql = str(connection.execute(
            """
            SELECT sql FROM sqlite_schema
            WHERE type='table' AND name='policy_template_applications'
            """
        ).fetchone()[0])
        assert "UNIQUE (tenant_id,previous_snapshot_id)" in sql
        assert "UNIQUE (tenant_id,result_snapshot_id)" in sql
        assert "UNIQUE (tenant_id,generated_policy_id)" in sql
        assert "UNIQUE (tenant_id,rollback_snapshot_id)" in sql
        assert sql.count("DEFERRABLE INITIALLY DEFERRED") == 7
        generated_fk_columns = {
            str(row[3])
            for row in connection.execute(
                "PRAGMA foreign_key_list('policy_template_applications')"
            ).fetchall()
        }
        assert "generated_policy_id" in generated_fk_columns
        assert "generated_policy_version" not in generated_fk_columns
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_schema
            WHERE type='index'
              AND name='uq_policy_template_applications_active_project_prd068'
              AND sql LIKE '%WHERE status=''applied''%'
            """
        ).fetchone()[0] == 1
        _healthy(connection)
