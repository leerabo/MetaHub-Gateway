"""PRD-066 0058 企业 Chat 元数据、RBAC 演进和安全回滚专项测试。"""

from __future__ import annotations

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

# 测试复制正式迁移文件，避免内嵌 SQL 与生产交付脚本发生漂移。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
OLD_HASHES = {
    "admin": "1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73",
    "developer": "220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f",
    "viewer": "b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a",
}
NEW_HASHES = {
    "admin": "af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef",
    "developer": "ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3",
    "viewer": "77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228",
}
CREATED_AT = "2026-08-02T01:00:00Z"


def _url(path: Path) -> str:
    """返回当前测试独占 SQLite 文件的迁移器 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制指定栈顶之前的正式 up/down SQL，构造真实迁移边界。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd066_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """构建历史 0057 与 fresh 0058 模板，供各用例复制后隔离验证。

    seed 特意在 0053 后、0054 前执行，使默认 Admin/Developer 真实经历
    revision 2，再由 0058 推进到 revision 3，避免只覆盖新租户 revision 1。
    """

    root = tmp_path_factory.mktemp("prd066-migration-templates")
    migrations_0053 = _migration_subset(root / "migrations-0053", 53)
    migrations_0057 = _migration_subset(root / "migrations-0057", 57)
    migrations_0058 = _migration_subset(root / "migrations-0058", 58)
    database_0057 = root / "template-0057.db"
    database_0058 = root / "template-0058.db"
    database_url = _url(database_0057)

    assert apply_migrations(
        database_url, migrations_dir=migrations_0053
    ).applied[-1] == "0053"
    apply_seed_data(database_url)
    assert apply_migrations(
        database_url, migrations_dir=migrations_0057
    ).applied[-1] == "0057"
    shutil.copy2(database_0057, database_0058)
    assert apply_migrations(
        _url(database_0058), migrations_dir=migrations_0058
    ).applied == ["0058"]
    return {
        "database_0057": database_0057,
        "database_0058": database_0058,
        "migrations_0058": migrations_0058,
    }


def _clone(template: Path, destination: Path) -> str:
    """复制已关闭连接的模板并返回当前用例独占 URL。"""

    shutil.copy2(template, destination)
    return _url(destination)


def _healthy(connection: sqlite3.Connection) -> None:
    """验证 SQLite 页面完整性和全部租户复合外键。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _rows(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[Any, ...], ...]:
    """按 rowid 冻结 RBAC 表全部字段，确保往返迁移不会改写历史行序和元数据。"""

    return tuple(
        tuple(row)
        for row in connection.execute(
            f'SELECT rowid,* FROM "{table}" ORDER BY rowid'
        ).fetchall()
    )


def _rbac_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """冻结目录、租户投影、角色 revision/hash 与真实关系。"""

    return {
        table: _rows(connection, table)
        for table in (
            "rbac_permission_templates",
            "permissions",
            "roles",
            "role_permissions",
        )
    }


def _identity(connection: sqlite3.Connection) -> tuple[str, str]:
    """返回 seed 的可信租户和 Admin 用户，保证全部外键样本归属同一租户。"""

    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id
        WHERE tenant.slug='default' AND user.role='admin' AND user.status='active'
        """
    ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


def _resource_graph(connection: sqlite3.Connection) -> dict[str, str]:
    """创建一套合法且只有一把 active Project Key 的 Chat 执行资源图。"""

    tenant_id, user_id = _identity(connection)
    environment = connection.execute(
        """
        SELECT environment_id,code FROM environments
        WHERE tenant_id=? AND code='dev' AND status='active'
        """,
        (tenant_id,),
    ).fetchone()
    model = connection.execute(
        """
        SELECT model_id FROM models
        WHERE tenant_id=? AND status='active' ORDER BY model_id LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert environment is not None and model is not None
    graph = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "environment_id": str(environment[0]),
        "environment_code": str(environment[1]),
        "project_id": "prj_prd066",
        "model_alias_id": "mal_prd066",
        "model_alias": "enterprise-chat",
        "virtual_key_id": "vkey_prd066",
        "auth_session_id": "session_prd066",
    }
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,
          status,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'PRD-066 Chat','迁移测试资源图',?,'active',?,?,?,?)
        """,
        (
            graph["project_id"],
            tenant_id,
            "prd066-chat",
            user_id,
            user_id,
            user_id,
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
        ) VALUES(?,?,?,?,?,NULL,'active','PRD-066 Alias',?,NULL,NULL,?,?,?)
        """,
        (
            graph["model_alias_id"],
            tenant_id,
            graph["model_alias"],
            graph["environment_code"],
            str(model[0]),
            user_id,
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
          created_by,disabled_at,disabled_by,project_id,application_id,
          environment_id
        ) VALUES(?,?,?,'mhv_prd066',?,'active',?,?,?,'迁移测试 Key',
                 NULL,?,NULL,NULL,?,NULL,?)
        """,
        (
            graph["virtual_key_id"],
            tenant_id,
            "PRD-066 Key",
            "b" * 64,
            CREATED_AT,
            CREATED_AT,
            graph["environment_code"],
            user_id,
            graph["project_id"],
            graph["environment_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO sessions(
          session_id,tenant_id,user_id,token_jti,token_hash,status,expires_at,
          revoked_at,last_seen_at,user_agent,created_ip,created_at,updated_at
        ) VALUES(?,?,?,'jti_prd066',?,'active','2026-08-03T01:00:00Z',
                 NULL,NULL,'prd066-test','127.0.0.1',?,?)
        """,
        (
            graph["auth_session_id"],
            tenant_id,
            user_id,
            "c" * 64,
            CREATED_AT,
            CREATED_AT,
        ),
    )
    connection.commit()
    return graph


def _chat_session(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    session_id: str = "chs_" + "1" * 32,
) -> str:
    """插入只含 owner 和资源快照的合法 active ChatSession。"""

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
    connection.commit()
    # Session 标识只在事务提交后返回，避免后续断言引用已回滚的孤立主键。
    return session_id


def _message(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    session_id: str,
) -> str:
    """占用 Session 序列 1 并插入不含正文的 in-flight Message。"""

    message_id = "chm_" + "2" * 32
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
        ) VALUES(?,?,?,?,?,1,'req_prd066_1','trc_prd066_1','in_flight',?,?)
        """,
        (
            message_id,
            graph["tenant_id"],
            session_id,
            graph["user_id"],
            "00000000-0000-4000-8000-000000000001",
            f"evt_{message_id}",
            "2026-08-02T01:00:02Z",
        ),
    )
    connection.commit()
    return message_id


def _grant(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    session_id: str,
    message_id: str,
    grant_id: str = "chg_" + "3" * 32,
    grant_hash: str = "a" * 64,
) -> str:
    """插入与单一 Message 和全部 owner 精确绑定的短时 grant。"""

    connection.execute(
        """
        INSERT INTO chat_execution_grants(
          chat_execution_grant_id,tenant_id,grant_hash,chat_session_id,
          chat_message_id,owner_user_id,owner_auth_session_id,project_id,
          environment_id,model_alias_id,virtual_key_id,gateway_request_id,
          gateway_trace_id,status,expires_at,issued_at,consumed_at,revoked_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'req_prd066_1','trc_prd066_1',
                 'issued','2026-08-02T01:00:23Z',
                 '2026-08-02T01:00:03Z',NULL,NULL)
        """,
        (
            grant_id,
            graph["tenant_id"],
            grant_hash,
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
    return grant_id


def test_prd066_empty_round_trip_restores_rbac(
    tmp_path: Path,
    prd066_templates: dict[str, Path],
) -> None:
    """验证历史默认矩阵 0057 -> 0058 -> 0057 -> 0058 无损往返。"""

    database_url = _clone(
        prd066_templates["database_0057"], tmp_path / "round-trip.db"
    )
    connection = connect_database(database_url)
    before = _rbac_snapshot(connection)
    old_revisions = {
        str(row["code"]): int(row["permission_revision"])
        for row in connection.execute(
            """
            SELECT code,permission_revision FROM roles
            WHERE tenant_id=(SELECT tenant_id FROM tenants WHERE slug='default')
              AND code IN ('admin','developer','viewer')
            """
        )
    }
    connection.close()

    assert apply_migrations(
        database_url, migrations_dir=prd066_templates["migrations_0058"]
    ).applied == ["0058"]
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_permission_templates"
    ).fetchone()[0] == 51
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_role_permission_templates"
    ).fetchone()[0] == 134
    for code, expected_hash in NEW_HASHES.items():
        row = connection.execute(
            """
            SELECT role.permission_revision,role.matrix_sha256,
                   COUNT(permission.permission_id)
            FROM roles role
            LEFT JOIN role_permissions relation
              ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
            LEFT JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
             AND permission.code='chat.use'
            WHERE role.tenant_id=(SELECT tenant_id FROM tenants WHERE slug='default')
              AND role.code=?
            GROUP BY role.role_id
            """,
            (code,),
        ).fetchone()
        assert tuple(row) == (old_revisions[code] + 1, expected_hash, 1)
    assert connection.execute(
        """
        SELECT COUNT(*) FROM role_permissions relation
        JOIN roles role
          ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
        JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE permission.code='chat.use'
          AND role.code IN ('finance_admin','security_admin','ops_admin')
        """
    ).fetchone()[0] == 0
    _healthy(connection)
    connection.close()

    assert rollback_last_migration(
        database_url, migrations_dir=prd066_templates["migrations_0058"]
    ).rolled_back == "0058"
    connection = connect_database(database_url)
    assert _rbac_snapshot(connection) == before
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_permission_templates"
    ).fetchone()[0] == 50
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_role_permission_templates"
    ).fetchone()[0] == 131
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_schema
        WHERE name IN ('chat_sessions','chat_messages','chat_execution_grants')
        """
    ).fetchone()[0] == 0
    _healthy(connection)
    connection.close()
    assert apply_migrations(
        database_url, migrations_dir=prd066_templates["migrations_0058"]
    ).applied == ["0058"]


def test_prd066_owner_duplicate_grant_and_state_guards(
    tmp_path: Path,
    prd066_templates: dict[str, Path],
) -> None:
    """验证跨租户 owner、重复 grant、并发 Message 和终态倒退均被拒绝。"""

    database_url = _clone(
        prd066_templates["database_0058"], tmp_path / "owner-state.db"
    )
    connection = connect_database(database_url)
    graph = _resource_graph(connection)
    session_id = _chat_session(connection, graph)
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id,slug,name,service_mode,status,created_at,updated_at
        ) VALUES('tenant_prd066_other','prd066-other','Other',
                 'internal_only','active',?,?)
        """,
        (CREATED_AT, CREATED_AT),
    )
    with pytest.raises(sqlite3.IntegrityError):
        _chat_session(
            connection,
            {**graph, "tenant_id": "tenant_prd066_other"},
            "chs_" + "9" * 32,
        )
    connection.rollback()

    message_id = _message(connection, graph, session_id)
    grant_id = _grant(connection, graph, session_id, message_id)
    with pytest.raises(sqlite3.IntegrityError):
        _grant(
            connection,
            graph,
            session_id,
            message_id,
            "chg_" + "4" * 32,
            "d" * 64,
        )
    connection.rollback()

    connection.execute(
        """
        UPDATE chat_sessions
        SET message_sequence=2,state_revision=3,updated_at='2026-08-02T01:00:04Z'
        WHERE tenant_id=? AND chat_session_id=?
        """,
        (graph["tenant_id"], session_id),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO chat_messages(
              chat_message_id,tenant_id,chat_session_id,owner_user_id,
              client_message_id,message_sequence,gateway_request_id,
              gateway_trace_id,status,webhook_event_id,started_at
            ) VALUES(?,?,?,?,?,2,'req_prd066_2','trc_prd066_2',
                     'in_flight',?,?)
            """,
            (
                "chm_" + "5" * 32,
                graph["tenant_id"],
                session_id,
                graph["user_id"],
                "00000000-0000-4000-8000-000000000002",
                "evt_chm_" + "5" * 32,
                "2026-08-02T01:00:05Z",
            ),
        )
    connection.rollback()

    connection.execute(
        """
        UPDATE chat_execution_grants
        SET status='consumed',consumed_at='2026-08-02T01:00:10Z'
        WHERE tenant_id=? AND chat_execution_grant_id=?
        """,
        (graph["tenant_id"], grant_id),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            UPDATE chat_execution_grants
            SET status='revoked',consumed_at=NULL,
                revoked_at='2026-08-02T01:00:11Z'
            WHERE tenant_id=? AND chat_execution_grant_id=?
            """,
            (graph["tenant_id"], grant_id),
        )
    connection.rollback()

    connection.execute(
        """
        UPDATE chat_messages
        SET status='succeeded',input_tokens=8,output_tokens=3,total_tokens=11,
            cache_tokens=0,cost_cny_micros=12,policy_decision='allow',
            policy_reason_code='policy.default_allow',fallback_triggered=0,
            cache_status='not_configured',completed_at='2026-08-02T01:00:12Z'
        WHERE tenant_id=? AND chat_message_id=?
        """,
        (graph["tenant_id"], message_id),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            UPDATE chat_messages
            SET status='failed',error_code='chat.execution_failed'
            WHERE tenant_id=? AND chat_message_id=?
            """,
            (graph["tenant_id"], message_id),
        )
    connection.rollback()

    connection.execute(
        """
        UPDATE chat_sessions
        SET status='cleared',state_revision=3,
            cleared_at='2026-08-02T01:00:13Z',
            updated_at='2026-08-02T01:00:13Z'
        WHERE tenant_id=? AND chat_session_id=?
        """,
        (graph["tenant_id"], session_id),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            UPDATE chat_sessions
            SET status='active',state_revision=4,cleared_at=NULL,
                updated_at='2026-08-02T01:00:14Z'
            WHERE tenant_id=? AND chat_session_id=?
            """,
            (graph["tenant_id"], session_id),
        )
    connection.rollback()
    _healthy(connection)
    connection.close()


def test_prd066_chat_fact_blocks_down_atomically(
    tmp_path: Path,
    prd066_templates: dict[str, Path],
) -> None:
    """验证 ChatSession 事实以稳定原因拒绝 down，且不产生部分回滚。"""

    database_url = _clone(
        prd066_templates["database_0058"], tmp_path / "down-guard.db"
    )
    connection = connect_database(database_url)
    graph = _resource_graph(connection)
    session_id = _chat_session(connection, graph)
    before = tuple(connection.iterdump())
    connection.close()

    with pytest.raises(DbError) as exc:
        rollback_last_migration(
            database_url, migrations_dir=prd066_templates["migrations_0058"]
        )
    assert exc.value.code == "db.migration_failed"
    assert "prd066_down_business_facts_present" in exc.value.message
    connection = connect_database(database_url)
    assert tuple(connection.iterdump()) == before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0058'"
    ).fetchone()[0] == "applied"
    assert connection.execute(
        """
        SELECT COUNT(*) FROM chat_sessions
        WHERE tenant_id=? AND chat_session_id=?
        """,
        (graph["tenant_id"], session_id),
    ).fetchone()[0] == 1
    _healthy(connection)
    connection.close()


def test_prd066_schema_has_no_sensitive_body_columns(
    tmp_path: Path,
    prd066_templates: dict[str, Path],
) -> None:
    """验证 Chat 三表只含安全执行元数据，不提供正文或凭据落库位置。"""

    database_url = _clone(
        prd066_templates["database_0058"], tmp_path / "privacy.db"
    )
    connection = connect_database(database_url)
    columns = {
        str(row["name"])
        for table in ("chat_sessions", "chat_messages", "chat_execution_grants")
        for row in connection.execute(f"PRAGMA table_info({table})")
    }
    assert {
        "content",
        "messages_json",
        "prompt",
        "prompt_hash",
        "response",
        "response_json",
        "endpoint_url",
        "key_prefix",
        "email",
        "error_message",
    }.isdisjoint(columns)
    assert {
        "tenant_id",
        "gateway_request_id",
        "gateway_trace_id",
        "cost_cny_micros",
        "error_code",
    }.issubset(columns)
    _healthy(connection)
    connection.close()
