"""PRD-067 0059 Prompt 模板库、RBAC/Audit 保真与安全回滚专项测试。"""

from __future__ import annotations

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

# 冻结迁移目录、RBAC 前后哈希、三项权限和时间戳，专项只验证 0059 精确状态而不依赖当前时钟。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
REVISION_3_HASHES = {
    "admin": "af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef",
    "developer": "ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3",
    "viewer": "77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228",
}
REVISION_4_HASHES = {
    "admin": "e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320",
    "developer": "2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c",
    "viewer": "3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086",
}
TEMPLATE_PERMISSION_CODES = (
    "prompt_template.read",
    "prompt_template.write",
    "prompt_template.use",
)
CREATED_AT = "2026-08-02T01:00:00Z"
PUBLISHED_AT = "2026-08-02T01:00:10Z"


def _url(path: Path) -> str:
    """返回当前用例独占 SQLite 文件的迁移器 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制指定栈顶前的正式 up/down SQL，测试不维护第二份内嵌迁移。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd067_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """构建 0058 历史与 0059 模板；seed 真实经历 revision 2、3、4 演进。"""

    root = tmp_path_factory.mktemp("prd067-migration-templates")
    migrations_0053 = _migration_subset(root / "migrations-0053", 53)
    migrations_0058 = _migration_subset(root / "migrations-0058", 58)
    migrations_0059 = _migration_subset(root / "migrations-0059", 59)
    database_0058 = root / "template-0058.db"
    database_0059 = root / "template-0059.db"
    database_url = _url(database_0058)

    assert apply_migrations(
        database_url, migrations_dir=migrations_0053
    ).applied[-1] == "0053"
    apply_seed_data(database_url)
    assert apply_migrations(
        database_url, migrations_dir=migrations_0058
    ).applied[-1] == "0058"
    shutil.copy2(database_0058, database_0059)
    assert apply_migrations(
        _url(database_0059), migrations_dir=migrations_0059
    ).applied == ["0059"]
    return {
        "database_0058": database_0058,
        "database_0059": database_0059,
        "migrations_0059": migrations_0059,
    }


def _clone(template: Path, destination: Path) -> str:
    """复制已关闭的模板数据库并返回当前用例独占 URL。"""

    shutil.copy2(template, destination)
    return _url(destination)


def _healthy(connection: sqlite3.Connection) -> None:
    """验证 SQLite 页面完整性和全部 tenant-qualified 外键。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _rows(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[Any, ...], ...]:
    """按 rowid 冻结表的全部字段，验证迁移往返不改写历史内部顺序。"""

    return tuple(
        tuple(row)
        for row in connection.execute(
            f'SELECT rowid,* FROM "{table}" ORDER BY rowid'
        ).fetchall()
    )


def _rbac_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """冻结目录、租户投影、角色 revision/hash 和真实关系。"""

    return {
        table: _rows(connection, table)
        for table in (
            "rbac_permission_templates",
            "permissions",
            "roles",
            "role_permissions",
        )
    }


def _schema_sql(
    connection: sqlite3.Connection,
    object_type: str,
    names: tuple[str, ...],
) -> dict[str, str]:
    """逐名冻结 sqlite_schema SQL，避免只比较对象数量掩盖约束漂移。"""

    placeholders = ",".join("?" for _ in names)
    return {
        str(row[0]): str(row[1])
        for row in connection.execute(
            f"""
            SELECT name,sql FROM sqlite_schema
            WHERE type=? AND name IN ({placeholders}) ORDER BY name
            """,
            (object_type, *names),
        )
    }


def _identity(connection: sqlite3.Connection) -> dict[str, str]:
    """返回 seed Admin 的完整 Audit 身份，所有样本只从可信数据库恢复 owner。"""

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


def _resource_graph(connection: sqlite3.Connection) -> dict[str, str]:
    """创建一套同租户 Project/Alias/Key/Session 图，供 Chat 与模板绑定专项复用。"""

    identity = _identity(connection)
    environment = connection.execute(
        """
        SELECT environment_id,code FROM environments
        WHERE tenant_id=? AND code='dev' AND status='active'
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
        "project_id": "prj_prd067",
        "model_alias_id": "mal_prd067",
        "model_alias": "prompt-template-chat",
        "virtual_key_id": "vkey_prd067",
        "auth_session_id": "session_prd067",
    }
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,
          status,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'PRD-067','迁移专项资源图',?,'active',?,?,?,?)
        """,
        (
            graph["project_id"],
            graph["tenant_id"],
            "prd067",
            graph["user_id"],
            graph["user_id"],
            graph["user_id"],
            CREATED_AT,
            CREATED_AT,
        ),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,default_model_id,
          fallback_model_id,status,description,created_by,disabled_at,
          disabled_by,created_at,updated_at,environment_id
        ) VALUES(?,?,?,?,?,NULL,'active','PRD-067 Alias',?,NULL,NULL,?,?,?)
        """,
        (
            graph["model_alias_id"],
            graph["tenant_id"],
            graph["model_alias"],
            graph["environment_code"],
            str(model[0]),
            graph["user_id"],
            CREATED_AT,
            CREATED_AT,
            graph["environment_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id,tenant_id,name,key_prefix,key_hash,status,
          created_at,updated_at,environment,description,last_used_at,
          created_by,disabled_at,disabled_by,project_id,application_id,environment_id
        ) VALUES(?,?,?,'mhv_prd067',?,'active',?,?,?,'迁移专项 Key',
                 NULL,?,NULL,NULL,?,NULL,?)
        """,
        (
            graph["virtual_key_id"],
            graph["tenant_id"],
            "PRD-067 Key",
            "b" * 64,
            CREATED_AT,
            CREATED_AT,
            graph["environment_code"],
            graph["user_id"],
            graph["project_id"],
            graph["environment_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO sessions(
          session_id,tenant_id,user_id,token_jti,token_hash,status,expires_at,
          revoked_at,last_seen_at,user_agent,created_ip,created_at,updated_at
        ) VALUES(?,?,?,'jti_prd067',?,'active','2026-08-03T01:00:00Z',
                 NULL,NULL,'prd067-test','127.0.0.1',?,?)
        """,
        (
            graph["auth_session_id"],
            graph["tenant_id"],
            graph["user_id"],
            "c" * 64,
            CREATED_AT,
            CREATED_AT,
        ),
    )
    connection.commit()
    return graph


def _chat_facts(connection: sqlite3.Connection, graph: dict[str, str]) -> dict[str, str]:
    """写入不含正文的 PRD-066 Session/Message/grant，验证 0059 down 不删历史事实。"""

    session_id = "chs_" + "1" * 32
    message_id = "chm_" + "2" * 32
    connection.execute(
        """
        INSERT INTO chat_sessions(
          chat_session_id,tenant_id,owner_user_id,owner_auth_session_id,
          project_id,environment_id,model_alias_id,model_alias,virtual_key_id,
          status,message_sequence,state_revision,expires_at,cleared_at,
          created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'active',0,1,
                 '2026-08-02T02:00:00Z',NULL,?,?)
        """,
        (
            session_id,
            graph["tenant_id"],
            graph["user_id"],
            graph["auth_session_id"],
            graph["project_id"],
            graph["environment_id"],
            graph["model_alias_id"],
            graph["model_alias"],
            graph["virtual_key_id"],
            CREATED_AT,
            CREATED_AT,
        ),
    )
    connection.execute(
        """
        UPDATE chat_sessions
        SET message_sequence=1,state_revision=2,updated_at='2026-08-02T01:00:01Z'
        WHERE tenant_id=? AND chat_session_id=?
        """,
        (graph["tenant_id"], session_id),
    )
    connection.execute(
        """
        INSERT INTO chat_messages(
          chat_message_id,tenant_id,chat_session_id,owner_user_id,
          client_message_id,message_sequence,gateway_request_id,gateway_trace_id,
          status,webhook_event_id,started_at
        ) VALUES(?,?,?,?,?,1,'req_prd067_chat','trc_prd067_chat',
                 'in_flight',?,?)
        """,
        (
            message_id,
            graph["tenant_id"],
            session_id,
            graph["user_id"],
            "00000000-0000-4000-8000-000000000067",
            f"evt_{message_id}",
            "2026-08-02T01:00:02Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO chat_execution_grants(
          chat_execution_grant_id,tenant_id,grant_hash,chat_session_id,
          chat_message_id,owner_user_id,owner_auth_session_id,project_id,
          environment_id,model_alias_id,virtual_key_id,gateway_request_id,
          gateway_trace_id,status,expires_at,issued_at,consumed_at,revoked_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'req_prd067_chat','trc_prd067_chat',
                 'issued','2026-08-02T01:00:23Z','2026-08-02T01:00:03Z',NULL,NULL)
        """,
        (
            "chg_" + "3" * 32,
            graph["tenant_id"],
            "a" * 64,
            session_id,
            message_id,
            graph["user_id"],
            graph["auth_session_id"],
            graph["project_id"],
            graph["environment_id"],
            graph["model_alias_id"],
            graph["virtual_key_id"],
        ),
    )
    connection.commit()
    # 返回同租户 Session/Message owner，Invocation 测试不得自行拼接跨租户关系。
    return {"session_id": session_id, "message_id": message_id}


def _insert_template(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    *,
    suffix: str = "1",
    selected: bool = False,
    recommended: bool = False,
) -> dict[str, Any]:
    """在一个 IMMEDIATE 事务中装配 building v1、绑定、head 与一次 published seal。"""

    template_id = "ptpl_" + suffix * 32
    version_id = "ptv_" + suffix * 32
    request_id = f"req_prd067_template_{suffix}"
    name = f"PRD-067 模板 {suffix}"
    scope = "selected" if selected else "all"
    model_count = 1 if recommended else 0
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """
        INSERT INTO prompt_templates(
          prompt_template_id,tenant_id,name,description,status,
          current_version_id,current_version_state,revision,created_by,updated_by,
          created_at,updated_at
        ) VALUES(?,?,?,'迁移专项模板','active',?,'published',1,?,?,?,?)
        """,
        (
            template_id,
            graph["tenant_id"],
            name,
            version_id,
            graph["user_id"],
            graph["user_id"],
            CREATED_AT,
            CREATED_AT,
        ),
    )
    connection.execute(
        """
        INSERT INTO prompt_template_versions(
          prompt_template_version_id,tenant_id,prompt_template_id,version_number,
          state,definition_schema_version,definition_ciphertext,
          definition_size_bytes,variable_count,project_scope,
          recommended_model_count,change_summary_hmac,created_by,created_at,
          published_at,request_id,trace_id
        ) VALUES(?,?,?,1,'building','prompt-template-v1',?,128,1,?,?,?, ?,?,NULL,?,?)
        """,
        (
            version_id,
            graph["tenant_id"],
            template_id,
            "ptpl-aesgcm-v1:v1:" + "a" * 16 + ":" + "b" * 32,
            scope,
            model_count,
            "d" * 64,
            graph["user_id"],
            CREATED_AT,
            request_id,
            f"trc_prd067_template_{suffix}",
        ),
    )
    if selected:
        connection.execute(
            """
            INSERT INTO prompt_template_version_projects(
              version_project_id,tenant_id,prompt_template_id,
              prompt_template_version_id,project_id,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                "ptvp_" + suffix * 32,
                graph["tenant_id"],
                template_id,
                version_id,
                graph["project_id"],
                CREATED_AT,
            ),
        )
    if recommended:
        connection.execute(
            """
            INSERT INTO prompt_template_version_models(
              version_model_id,tenant_id,prompt_template_id,
              prompt_template_version_id,model_alias_id,display_order,created_at
            ) VALUES(?,?,?,?,?,0,?)
            """,
            (
                "ptvm_" + suffix * 32,
                graph["tenant_id"],
                template_id,
                version_id,
                graph["model_alias_id"],
                CREATED_AT,
            ),
        )
    connection.execute(
        """
        UPDATE prompt_template_versions
        SET state='published',published_at=?
        WHERE tenant_id=? AND prompt_template_version_id=?
        """,
        (PUBLISHED_AT, graph["tenant_id"], version_id),
    )
    connection.commit()
    return {
        "template_id": template_id,
        "version_id": version_id,
        "version_number": 1,
        "request_id": request_id,
        "trace_id": f"trc_prd067_template_{suffix}",
        "name": name,
        "scope": scope,
        "project_count": 1 if selected else 0,
        "model_count": model_count,
    }


def _insert_audit(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    template: dict[str, Any],
    *,
    action: str,
    audit_log_id: str,
) -> None:
    """写入与当前模板/版本逐值一致的安全 Audit 摘要，不包含正文、变量或密文。"""

    summary = {
        "status": "active",
        "revision": 1,
        "version_number": 1,
        "project_scope": template["scope"],
        "project_count": template["project_count"],
        "recommended_model_count": template["model_count"],
        "definition_size_bytes": 128,
        "variable_count": 1,
        "change_summary_hmac": "d" * 64,
    }
    object_type = (
        "prompt_template_version"
        if action == "prompt_template.version_published"
        else "prompt_template"
    )
    object_id = (
        template["version_id"]
        if object_type == "prompt_template_version"
        else template["template_id"]
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
          action,object_type,object_id,object_name,before_summary_json,
          after_summary_json,changed_fields_json,client_ip,user_agent,
          request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'{}',?,?,NULL,NULL,?,?,?)
        """,
        (
            audit_log_id,
            graph["tenant_id"],
            graph["user_id"],
            graph["email"],
            graph["role"],
            action,
            object_type,
            object_id,
            template["name"],
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            json.dumps(sorted(summary), ensure_ascii=False, separators=(",", ":")),
            template["request_id"],
            template["trace_id"],
            PUBLISHED_AT,
        ),
    )


def test_prd067_seeded_upgrade_expands_only_canonical_roles(
    tmp_path: Path,
    prd067_templates: dict[str, Path],
) -> None:
    """验证 revision 3 -> 4 精确补权，定制 Developer 与三职责角色均不被静默扩权。"""

    database_url = _clone(
        prd067_templates["database_0058"], tmp_path / "canonical-upgrade.db"
    )
    connection = connect_database(database_url)
    tenant_id = _identity(connection)["tenant_id"]
    developer = connection.execute(
        "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code='developer'",
        (tenant_id,),
    ).fetchone()
    assert developer is not None
    relation = connection.execute(
        """
        SELECT relation.role_permission_id
        FROM role_permissions relation
        JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=? AND relation.role_id=?
          AND permission.code='model.read'
        """,
        (tenant_id, developer[0]),
    ).fetchone()
    assert relation is not None
    connection.execute(
        "DELETE FROM role_permissions WHERE role_permission_id=?", (relation[0],)
    )
    connection.execute(
        """
        UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=?
        WHERE tenant_id=? AND role_id=?
        """,
        ("9" * 64, tenant_id, developer[0]),
    )
    connection.commit()
    old_revisions = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            """
            SELECT code,permission_revision FROM roles
            WHERE tenant_id=? AND code IN ('admin','viewer')
            """,
            (tenant_id,),
        )
    }
    connection.close()

    result = apply_migrations(
        database_url, migrations_dir=prd067_templates["migrations_0059"]
    )
    assert result.applied == ["0059"]
    assert apply_migrations(
        database_url, migrations_dir=prd067_templates["migrations_0059"]
    ).applied == []
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=4"
    ).fetchone()[0] == 54
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_role_permission_templates"
    ).fetchone()[0] == 141
    for code in ("admin", "viewer"):
        row = connection.execute(
            "SELECT permission_revision,matrix_sha256 FROM roles WHERE tenant_id=? AND code=?",
            (tenant_id, code),
        ).fetchone()
        assert row is not None
        assert tuple(row) == (
            old_revisions[code] + 1,
            REVISION_4_HASHES[code],
        )
    assert connection.execute(
        """
        SELECT COUNT(*) FROM role_permissions relation
        JOIN roles role ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
        JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE role.tenant_id=? AND role.code='developer'
          AND permission.code LIKE 'prompt_template.%'
        """,
        (tenant_id,),
    ).fetchone()[0] == 0
    assert connection.execute(
        """
        SELECT COUNT(*) FROM role_permissions relation
        JOIN roles role ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
        JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE role.tenant_id=? AND role.code IN (
          'finance_admin','security_admin','ops_admin'
        ) AND permission.code LIKE 'prompt_template.%'
        """,
        (tenant_id,),
    ).fetchone()[0] == 0
    _healthy(connection)
    connection.close()


def test_prd067_round_trip_preserves_rbac_audit_and_prd066_chat(
    tmp_path: Path,
    prd067_templates: dict[str, Path],
) -> None:
    """验证 0058 -> 0059 -> 0058 对 RBAC、Audit NULL/rowid 与既有 Chat 事实逐值无损。"""

    database_url = _clone(
        prd067_templates["database_0058"], tmp_path / "round-trip.db"
    )
    connection = connect_database(database_url)
    graph = _resource_graph(connection)
    _chat_facts(connection, graph)
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
          action,object_type,object_id,object_name,before_summary_json,
          after_summary_json,changed_fields_json,client_ip,user_agent,
          request_id,trace_id,created_at
        ) VALUES('aud_prd067_baseline',?,?,?,?,
                 'provider.created','provider','prv_prd067',NULL,
                 '{}','{}','[]',NULL,NULL,
                 'req_prd067_baseline','trc_prd067_baseline',?)
        """,
        (
            graph["tenant_id"],
            graph["user_id"],
            graph["email"],
            graph["role"],
            CREATED_AT,
        ),
    )
    connection.commit()
    old_rbac = _rbac_snapshot(connection)
    old_audit = _rows(connection, "audit_logs")
    old_sequence = _rows(connection, "audit_log_export_sequences")
    old_chat = {
        table: _rows(connection, table)
        for table in ("chat_sessions", "chat_messages", "chat_execution_grants")
    }
    audit_indexes = _schema_sql(
        connection,
        "index",
        (
            "idx_audit_logs_tenant_time",
            "idx_audit_logs_tenant_actor_time",
            "idx_audit_logs_tenant_object_time",
            "idx_audit_logs_tenant_action_time",
            "idx_audit_logs_tenant_request",
            "idx_audit_logs_dry_run_request_unique",
            "idx_audit_logs_model_quality_request_unique",
            "idx_audit_logs_finops_adjustment_unique",
            "idx_audit_logs_role_permission_request_unique",
            "idx_audit_logs_invitation_request_unique",
            "idx_audit_logs_config_transfer_request_unique",
        ),
    )
    audit_triggers = _schema_sql(
        connection,
        "trigger",
        (
            "trg_audit_logs_owner_insert",
            "trg_audit_logs_immutable_update",
            "trg_audit_logs_immutable_delete",
            "trg_audit_logs_prd050_conflict_insert",
            "trg_audit_logs_prd050_rowid_insert",
            "trg_audit_logs_export_sequence_insert_prd058",
            "trg_audit_exports_owner_insert_prd058",
            "trg_audit_log_export_sequences_owner_insert_prd058",
            "trg_invitation_events_owner_insert_prd060",
            "trg_role_permission_events_owner_insert_prd059",
        ),
    )
    connection.close()

    assert apply_migrations(
        database_url, migrations_dir=prd067_templates["migrations_0059"]
    ).applied == ["0059"]
    assert rollback_last_migration(
        database_url, migrations_dir=prd067_templates["migrations_0059"]
    ).rolled_back == "0059"
    connection = connect_database(database_url)
    assert _rbac_snapshot(connection) == old_rbac
    assert _rows(connection, "audit_logs") == old_audit
    assert _rows(connection, "audit_log_export_sequences") == old_sequence
    assert {
        table: _rows(connection, table)
        for table in ("chat_sessions", "chat_messages", "chat_execution_grants")
    } == old_chat
    assert _schema_sql(connection, "index", tuple(audit_indexes)) == audit_indexes
    assert _schema_sql(connection, "trigger", tuple(audit_triggers)) == audit_triggers
    _healthy(connection)
    connection.close()
    assert apply_migrations(
        database_url, migrations_dir=prd067_templates["migrations_0059"]
    ).applied == ["0059"]


def test_prd067_new_tenant_receives_revision4_default_matrix(
    tmp_path: Path,
    prd067_templates: dict[str, Path],
) -> None:
    """验证 0059 后新 tenant 直接投影 54 权限和 141 条产品默认关系。"""

    database_url = _clone(
        prd067_templates["database_0059"], tmp_path / "new-tenant.db"
    )
    connection = connect_database(database_url)
    tenant_id = "tenant_prd067_new"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id,slug,name,service_mode,status,created_at,updated_at
        ) VALUES(?,'prd067-new','PRD-067 New','internal_only','active',?,?)
        """,
        (tenant_id, CREATED_AT, CREATED_AT),
    )
    connection.commit()
    assert connection.execute(
        "SELECT COUNT(*) FROM permissions WHERE tenant_id=? AND catalog_revision=4",
        (tenant_id,),
    ).fetchone()[0] == 54
    assert connection.execute(
        "SELECT COUNT(*) FROM role_permissions WHERE tenant_id=?", (tenant_id,)
    ).fetchone()[0] == 141
    for code, expected_hash in REVISION_4_HASHES.items():
        row = connection.execute(
            "SELECT permission_revision,matrix_sha256 FROM roles WHERE tenant_id=? AND code=?",
            (tenant_id, code),
        ).fetchone()
        assert row is not None
        assert tuple(row) == (1, expected_hash)
    _healthy(connection)
    connection.close()


def test_prd067_published_version_bindings_are_tenant_owned_and_immutable(
    tmp_path: Path,
    prd067_templates: dict[str, Path],
) -> None:
    """验证已发布版本的租户绑定、延迟 head、一次封印和全部不可变写门禁。"""

    database_url = _clone(
        prd067_templates["database_0059"], tmp_path / "version-guards.db"
    )
    connection = connect_database(database_url)
    graph = _resource_graph(connection)
    template = _insert_template(
        connection, graph, suffix="4", selected=True, recommended=True
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE prompt_template_versions SET definition_ciphertext=? "
            "WHERE prompt_template_version_id=?",
            ("ptpl-aesgcm-v1:v1:" + "e" * 16 + ":" + "f" * 32, template["version_id"]),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM prompt_template_version_projects WHERE prompt_template_version_id=?",
            (template["version_id"],),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM prompt_template_versions WHERE prompt_template_version_id=?",
            (template["version_id"],),
        )
    connection.rollback()
    # 同形跨租户 Project ID 不能作为当前 tenant 的版本绑定。
    other_tenant = "tenant_prd067_other"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id,slug,name,service_mode,status,created_at,updated_at
        ) VALUES(?,'prd067-other','Other','internal_only','active',?,?)
        """,
        (other_tenant, CREATED_AT, CREATED_AT),
    )
    other_user = "usr_prd067_other"
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,
          created_at,updated_at
        ) VALUES(?,?,'other-prd067@example.com','Other',?,'admin','active',?,?)
        """,
        (other_user, other_tenant, "x" * 60, CREATED_AT, CREATED_AT),
    )
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,
          status,created_by,updated_by,created_at,updated_at
        ) VALUES('prj_prd067_other',?,'other','Other','',?,'active',?,?,?,?)
        """,
        (other_tenant, other_user, other_user, other_user, CREATED_AT, CREATED_AT),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO prompt_template_version_projects(
              version_project_id,tenant_id,prompt_template_id,
              prompt_template_version_id,project_id,created_at
            ) VALUES('ptvp_55555555555555555555555555555555',?,?,?,?,?)
            """,
            (
                graph["tenant_id"],
                template["template_id"],
                template["version_id"],
                "prj_prd067_other",
                CREATED_AT,
            ),
        )
    connection.rollback()
    _healthy(connection)
    connection.close()


def test_prd067_invocation_uses_four_four_six_column_owner_graph(
    tmp_path: Path,
    prd067_templates: dict[str, Path],
) -> None:
    """验证 Invocation 以四/四/六列关系精确绑定版本、登录 owner、Message 和链路标识。"""

    database_url = _clone(
        prd067_templates["database_0059"], tmp_path / "invocation.db"
    )
    connection = connect_database(database_url)
    graph = _resource_graph(connection)
    chat = _chat_facts(connection, graph)
    template = _insert_template(connection, graph, suffix="6")
    connection.execute(
        """
        INSERT INTO chat_prompt_template_invocations(
          prompt_template_invocation_id,tenant_id,prompt_template_id,
          prompt_template_version_id,version_number,chat_session_id,
          chat_message_id,actor_user_id,actor_auth_session_id,
          gateway_request_id,gateway_trace_id,status,failure_code,
          created_at,completed_at
        ) VALUES('ptinv_66666666666666666666666666666666',?,?,?,?,?,?,?,?,?,?,
                 'in_flight',NULL,'2026-08-02T01:00:03Z',NULL)
        """,
        (
            graph["tenant_id"],
            template["template_id"],
            template["version_id"],
            1,
            chat["session_id"],
            chat["message_id"],
            graph["user_id"],
            graph["auth_session_id"],
            "req_prd067_chat",
            "trc_prd067_chat",
        ),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO chat_prompt_template_invocations(
              prompt_template_invocation_id,tenant_id,prompt_template_id,
              prompt_template_version_id,version_number,chat_session_id,
              chat_message_id,actor_user_id,actor_auth_session_id,
              gateway_request_id,gateway_trace_id,status,created_at
            ) VALUES('ptinv_77777777777777777777777777777777',?,?,?,?,?,?,?,?,?,?,
                     'in_flight','2026-08-02T01:00:03Z')
            """,
            (
                graph["tenant_id"],
                template["template_id"],
                template["version_id"],
                1,
                chat["session_id"],
                chat["message_id"],
                graph["user_id"],
                graph["auth_session_id"],
                "req_wrong",
                "trc_prd067_chat",
            ),
        )
    connection.rollback()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """
        UPDATE chat_messages
        SET status='succeeded',completed_at='2026-08-02T01:00:20Z'
        WHERE tenant_id=? AND chat_message_id=?
        """,
        (graph["tenant_id"], chat["message_id"]),
    )
    connection.execute(
        """
        UPDATE chat_prompt_template_invocations
        SET status='succeeded',completed_at='2026-08-02T01:00:20Z'
        WHERE tenant_id=? AND prompt_template_invocation_id=?
        """,
        (graph["tenant_id"], "ptinv_" + "6" * 32),
    )
    connection.commit()
    _healthy(connection)
    connection.close()


def test_prd067_audit_event_and_operation_are_exactly_bound(
    tmp_path: Path,
    prd067_templates: dict[str, Path],
) -> None:
    """验证 v1 双 Audit、event 与 create Operation 同 request 唯一且无敏感摘要。"""

    database_url = _clone(
        prd067_templates["database_0059"], tmp_path / "audit-event-operation.db"
    )
    connection = connect_database(database_url)
    graph = _resource_graph(connection)
    template = _insert_template(connection, graph, suffix="8")
    created_audit_id = "aud_prd067_template_created"
    version_audit_id = "aud_prd067_version_published"
    _insert_audit(
        connection,
        graph,
        template,
        action="prompt_template.created",
        audit_log_id=created_audit_id,
    )
    _insert_audit(
        connection,
        graph,
        template,
        action="prompt_template.version_published",
        audit_log_id=version_audit_id,
    )
    connection.execute(
        """
        INSERT INTO prompt_template_operations(
          prompt_template_operation_id,tenant_id,actor_user_id,request_id,
          trace_id,operation,intent_hmac,prompt_template_id,
          prompt_template_version_id,version_number,result_template_revision,
          result_audit_log_id,created_at
        ) VALUES('ptop_88888888888888888888888888888888',?,?,?,?,
                 'create',?,?,?,1,1,?,?)
        """,
        (
            graph["tenant_id"],
            graph["user_id"],
            template["request_id"],
            template["trace_id"],
            "e" * 64,
            template["template_id"],
            template["version_id"],
            created_audit_id,
            PUBLISHED_AT,
        ),
    )
    payload = {
        "template_id": template["template_id"],
        "version_id": template["version_id"],
        "version_number": 1,
        "template_revision": 1,
        "project_scope": "all",
        "project_count": 0,
        "recommended_model_count": 0,
    }
    connection.execute(
        """
        INSERT INTO prompt_template_events(
          prompt_template_event_id,tenant_id,prompt_template_id,
          prompt_template_version_id,version_number,event_type,actor_user_id,
          publication_audit_log_id,request_id,trace_id,payload_json,
          delivery_status,last_error_code,created_at,updated_at,dispatched_at
        ) VALUES('ptev_88888888888888888888888888888888',?,?,?,?,
                 'prompt_template.published',?,?,?,?,?,'pending',NULL,?,?,NULL)
        """,
        (
            graph["tenant_id"],
            template["template_id"],
            template["version_id"],
            1,
            graph["user_id"],
            version_audit_id,
            template["request_id"],
            template["trace_id"],
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            PUBLISHED_AT,
            PUBLISHED_AT,
        ),
    )
    connection.commit()
    assert connection.execute(
        """
        SELECT COUNT(*) FROM audit_logs
        WHERE tenant_id=? AND request_id=?
          AND action IN (
            'prompt_template.created','prompt_template.version_published'
          )
        """,
        (graph["tenant_id"], template["request_id"]),
    ).fetchone()[0] == 2
    serialized = "\n".join(
        str(value)
        for row in connection.execute(
            """
            SELECT before_summary_json,after_summary_json,changed_fields_json
            FROM audit_logs WHERE request_id=?
            """,
            (template["request_id"],),
        )
        for value in row
    )
    assert "ptpl-aesgcm" not in serialized
    assert "variables" not in serialized
    _healthy(connection)
    connection.close()


@pytest.mark.parametrize("fact_kind", ["template", "audit", "webhook", "permission"])
def test_prd067_down_business_facts_fail_before_first_ddl(
    tmp_path: Path,
    prd067_templates: dict[str, Path],
    fact_kind: str,
) -> None:
    """验证四类不可丢失事实均以稳定错误在首项 DDL 前原子拒绝 down。"""

    database_url = _clone(
        prd067_templates["database_0059"], tmp_path / f"down-{fact_kind}.db"
    )
    connection = connect_database(database_url)
    identity = _identity(connection)
    if fact_kind == "template":
        graph = _resource_graph(connection)
        _insert_template(connection, graph, suffix="9")
    elif fact_kind == "audit":
        # 故障注入只移除 owner trigger，以证明 action 枚举事实本身也足以阻止 down。
        connection.execute("DROP TRIGGER trg_audit_logs_owner_insert")
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
              action,object_type,object_id,object_name,before_summary_json,
              after_summary_json,changed_fields_json,client_ip,user_agent,
              request_id,trace_id,created_at
            ) VALUES('aud_prd067_down_guard',?,?,?,?,
                     'prompt_template.created','prompt_template','ptpl_guard',NULL,
                     '{}','{}','[]',NULL,NULL,'req_down_audit','trc_down_audit',?)
            """,
            (
                identity["tenant_id"],
                identity["user_id"],
                identity["email"],
                identity["role"],
                CREATED_AT,
            ),
        )
        connection.commit()
    elif fact_kind == "webhook":
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,
              timeout_ms,max_attempts,created_by,updated_by,created_at,updated_at
            ) VALUES('wh_prd067_guard',?,'PRD-067 Guard','https://example.invalid/hook',
                     '["prompt_template.published"]','cipher','sec_','fingerprint',
                     'active',5000,3,?,?,?,?)
            """,
            (
                identity["tenant_id"],
                identity["user_id"],
                identity["user_id"],
                CREATED_AT,
                CREATED_AT,
            ),
        )
        connection.commit()
    else:
        # 绕过运行期 owner trigger 注入非 canonical Finance grant，down 必须把它视为显式配置事实。
        connection.execute("DROP TRIGGER trg_role_permissions_owner_insert_prd059")
        role_id, permission_id = connection.execute(
            """
            SELECT role.role_id,permission.permission_id
            FROM roles role JOIN permissions permission
              ON permission.tenant_id=role.tenant_id
            WHERE role.tenant_id=? AND role.code='finance_admin'
              AND permission.code='prompt_template.use'
            """,
            (identity["tenant_id"],),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO role_permissions(
              role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
            ) VALUES('rperm_prd067_guard',?,?,?,?,?)
            """,
            (identity["tenant_id"], role_id, permission_id, CREATED_AT, CREATED_AT),
        )
        connection.commit()
    schema_before = connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema"
    ).fetchone()[0]
    connection.close()

    with pytest.raises(DbError, match="prd067_down_business_facts_present"):
        rollback_last_migration(
            database_url, migrations_dir=prd067_templates["migrations_0059"]
        )
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0059'"
    ).fetchone()[0] == "applied"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema"
    ).fetchone()[0] == schema_before
    connection.close()


def test_prd067_unknown_dependency_blocks_down_atomically(
    tmp_path: Path,
    prd067_templates: dict[str, Path],
) -> None:
    """验证未知后续 view 引用模板表时，结构 guard 在删除任一 0059 对象前拒绝。"""

    database_url = _clone(
        prd067_templates["database_0059"], tmp_path / "unknown-dependency.db"
    )
    connection = connect_database(database_url)
    connection.execute(
        "CREATE VIEW prd068_unknown_prompt_view AS SELECT tenant_id FROM prompt_templates"
    )
    connection.commit()
    connection.close()
    with pytest.raises(DbError, match="prd067_down_preflight_ok"):
        rollback_last_migration(
            database_url, migrations_dir=prd067_templates["migrations_0059"]
        )
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name='prompt_templates'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name='prd068_unknown_prompt_view'"
    ).fetchone()[0] == 1
    _healthy(connection)
    connection.close()
