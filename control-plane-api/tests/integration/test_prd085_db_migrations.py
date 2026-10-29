"""验证产品事件迁移的来源映射、租户约束、权限哈希、完整性和安全回滚门禁。"""

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

# 冻结迁移目录、revision 20/19 哈希和统一时间，逐字验证升级与回滚后的权限事实。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
CATALOG_HASH_20 = "5f77eaca2da3b37026d9891ee7fab0f4aa121fbd2ac775bcc2b21d740aacca14"
ROLE_HASH_20 = "4602777ad3937aa0351a4b9949826756904a4ee3b485d65984cdbecdce127711"
CATALOG_HASH_19 = "f18c61ce9d657a22a0592abb0922407b7bda7cc1130d93fe5eea959ee1dbd3b0"
ROLE_HASH_19 = "0a448a84577e046654ed83d384f6cb59930477fcec78156f478a2aab66a5e239"
NOW = "2026-08-17T00:00:00Z"


def _url(path: Path) -> str:
    """把测试临时 SQLite 文件转换为迁移器接受的数据库连接地址。"""

    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def empty_0078(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """从空库连续应用 0001 至 0078，作为不含业务种子的迁移基准。"""

    root = tmp_path_factory.mktemp("prd085-empty")
    database = root / "empty.sqlite3"
    result = apply_migrations(_url(database))
    assert result.applied == [f"{version:04d}" for version in range(1, 79)]
    return database


@pytest.fixture(scope="module")
def seeded_0078(
    empty_0078: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """复制空迁移基准并应用默认租户种子，供来源 trigger 和回滚测试复用。"""

    root = tmp_path_factory.mktemp("prd085-seeded")
    database = root / "seeded.sqlite3"
    shutil.copy2(empty_0078, database)
    apply_seed_data(_url(database))
    return database


def _clone(source: Path, destination: Path) -> str:
    """为单个测试复制隔离数据库，避免失败事务污染模块级基准。"""

    shutil.copy2(source, destination)
    return _url(destination)


def _healthy(connection: sqlite3.Connection) -> None:
    """同时验证 SQLite 结构完整性与外键引用没有违规记录。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _hashes(connection: sqlite3.Connection) -> tuple[str, str]:
    """按迁移冻结顺序计算权限目录和角色模板的规范 SHA-256。"""

    catalog_records = [
        "\x1f".join(str(value) for value in row)
        for row in connection.execute(
            """
            SELECT permission_code,category,name,description,risk_level,catalog_revision
            FROM rbac_permission_templates ORDER BY permission_code
            """
        )
    ]
    role_records = [
        "\x1f".join(str(value) for value in row)
        for row in connection.execute(
            """
            SELECT role_code,permission_code,default_granted
            FROM rbac_role_permission_templates ORDER BY role_code,permission_code
            """
        )
    ]
    return (
        hashlib.sha256("\x1e".join(catalog_records).encode()).hexdigest(),
        hashlib.sha256("\x1e".join(role_records).encode()).hexdigest(),
    )


def _identity(connection: sqlite3.Connection) -> sqlite3.Row:
    """读取默认租户的管理员、Provider 和 Virtual Key 权威身份用于来源夹具。"""

    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,user.email,user.role,
               provider.provider_id,virtual_key.virtual_key_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN providers provider ON provider.tenant_id=tenant.tenant_id
        JOIN virtual_keys virtual_key ON virtual_key.tenant_id=tenant.tenant_id
        ORDER BY provider.provider_id,virtual_key.virtual_key_id LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return row


def _insert_audit(
    connection: sqlite3.Connection,
    *,
    audit_id: str,
    action: str,
    object_type: str,
    object_id: str,
    created_at: str,
) -> None:
    """插入同租户审计来源，验证 ProductEvent trigger 的动作映射与操作链路。"""

    owner = _identity(connection)
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
          action,object_type,object_id,object_name,before_summary_json,
          after_summary_json,changed_fields_json,client_ip,user_agent,
          request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,NULL,'{}','{}','[]',NULL,NULL,?,?,?)
        """,
        (
            audit_id,
            owner["tenant_id"],
            owner["user_id"],
            owner["email"],
            owner["role"],
            action,
            object_type,
            object_id,
            f"req_{audit_id}",
            f"trc_{audit_id}",
            created_at,
        ),
    )


def _insert_request_log(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    status: str,
    created_at: str,
) -> None:
    """插入最小 RequestLog 权威事实，用于验证首次成功调用的唯一投影。"""

    trace_id = f"trc_{request_id}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
          policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,NULL,NULL,'disabled',?)
        """,
        (tenant_id, request_id, trace_id, created_at),
    )
    connection.execute(
        """
        INSERT INTO request_logs(request_id,tenant_id,trace_id,status,created_at)
        VALUES(?,?,?,?,?)
        """,
        (request_id, tenant_id, trace_id, status, created_at),
    )


def _insert_config_import_audit(connection: sqlite3.Connection) -> str:
    """插入白名单配置导入摘要，验证 Provider 与 Virtual Key 双里程碑投影。"""

    owner = _identity(connection)
    config_import_id = "cimp_00000000000000000000000000000085"
    audit_id = "aud_prd085_config_import"
    after = {
        "schema_version": "metahub.config/v1",
        "manifest_sha256": "8" * 64,
        "providers_created_count": 1,
        "providers_unchanged_count": 0,
        "models_created_count": 0,
        "models_unchanged_count": 0,
        "aliases_created_count": 0,
        "aliases_unchanged_count": 0,
        "virtual_keys_created_count": 1,
        "virtual_keys_unchanged_count": 0,
        "generated_virtual_key_count": 1,
        "disabled_virtual_key_count": 0,
    }
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
        ) VALUES(?,?,?,?,'metahub.config/v1',1,0,0,0,0,0,1,0,1,0,?,?,?,?)
        """,
        (
            config_import_id,
            owner["tenant_id"],
            owner["user_id"],
            "8" * 64,
            "req_prd085_config_import",
            "trc_prd085_config_import",
            "cievt_00000000000000000000000000000085",
            "2026-08-17T03:00:00Z",
        ),
    )
    connection.executemany(
        """
        INSERT INTO config_import_rows(
          tenant_id,config_import_id,collection,row_number,object_type,
          action,object_id,business_key_sha256
        ) VALUES(?,?,?,?,?,'created',?,?)
        """,
        (
            (
                owner["tenant_id"],
                config_import_id,
                "providers",
                1,
                "provider",
                "provider_prd085_config_import",
                "9" * 64,
            ),
            (
                owner["tenant_id"],
                config_import_id,
                "virtual_keys",
                1,
                "virtual_key",
                "vk_prd085_config_import",
                "a" * 64,
            ),
        ),
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
          action,object_type,object_id,object_name,before_summary_json,
          after_summary_json,changed_fields_json,client_ip,user_agent,
          request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'config.imported','config_transfer',?,NULL,'{}',?,?,
                 NULL,NULL,?,?,?)
        """,
        (
            audit_id,
            owner["tenant_id"],
            owner["user_id"],
            owner["email"],
            owner["role"],
            config_import_id,
            json.dumps(after, sort_keys=True, separators=(",", ":")),
            json.dumps(sorted(after), separators=(",", ":")),
            "req_prd085_config_import",
            "trc_prd085_config_import",
            "2026-08-17T03:00:00Z",
        ),
    )
    return audit_id


def _insert_request_log_query(connection: sqlite3.Connection) -> str:
    """创建带 Session 操作者的日志查询事实，验证 actor、request 和 trace 精确继承。"""

    owner = _identity(connection)
    session_id = "ses_prd085_query"
    query_event_id = "lqe_" + "8" * 32
    connection.execute(
        """
        INSERT INTO sessions(
          session_id,tenant_id,user_id,token_jti,token_hash,status,expires_at,
          revoked_at,last_seen_at,user_agent,created_ip,created_at,updated_at
        ) VALUES(?,?,?,?,?,'active','2026-08-18T00:00:00Z',NULL,NULL,NULL,NULL,?,?)
        """,
        (
            session_id,
            owner["tenant_id"],
            owner["user_id"],
            "jti_prd085_query",
            "b" * 64,
            "2026-08-17T03:30:00Z",
            "2026-08-17T03:30:00Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO request_log_query_events(
          query_event_id,event_id,tenant_id,actor_user_id,actor_session_id,
          filter_version,filter_fingerprint,filter_keys_json,saved_filter_id,
          sort_by,sort_direction,query_limit,query_offset,matched_count,
          returned_count,has_more,query_duration_ms,operation_request_id,
          operation_trace_id,event_dispatch_status,event_dispatch_attempts,
          event_next_attempt_at,event_dispatched_at,event_claim_token,
          event_lease_owner_id,event_lease_expires_at,event_last_error_code,
          executed_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,1,?,'["status"]',NULL,'created_at','desc',10,0,2,2,0,3,
                 ?,?,'pending',0,?,NULL,NULL,NULL,NULL,NULL,?,?,?)
        """,
        (
            query_event_id,
            "lqevt_" + "8" * 32,
            owner["tenant_id"],
            owner["user_id"],
            session_id,
            "c" * 64,
            "req_prd085_query",
            "trc_prd085_query",
            "2026-08-17T03:30:01Z",
            "2026-08-17T03:30:01Z",
            "2026-08-17T03:30:01Z",
            "2026-08-17T03:30:01Z",
        ),
    )
    return query_event_id


def _insert_invitation_audit(connection: sqlite3.Connection) -> str:
    """创建邀请对象和安全审计摘要，验证激活事件不复制邮箱或令牌。"""

    owner = _identity(connection)
    invitation_id = "inv_" + "8" * 32
    audit_id = "aud_prd085_invited"
    expires_at = "2026-08-18T04:00:00Z"
    fingerprint = "d" * 64
    connection.execute(
        """
        INSERT INTO invitations(
          invitation_id,tenant_id,email,email_fingerprint,role_code,token_hash,status,
          invited_by,accepted_user_id,expires_at,accepted_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'pending',?,NULL,?,NULL,?,?)
        """,
        (
            invitation_id,
            owner["tenant_id"],
            "prd085-invite@example.invalid",
            fingerprint,
            "developer",
            hashlib.sha256(b"prd085-invitation-fixture").hexdigest(),
            owner["user_id"],
            expires_at,
            "2026-08-17T04:00:00Z",
            "2026-08-17T04:00:00Z",
        ),
    )
    after = {
        "email_fingerprint": fingerprint,
        "role_code": "developer",
        "status": "pending",
        "expires_at": expires_at,
        "accepted_user_id": None,
    }
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
          action,object_type,object_id,object_name,before_summary_json,
          after_summary_json,changed_fields_json,client_ip,user_agent,
          request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'user.invited','invitation',?,NULL,'{}',?,?,
                 NULL,NULL,?,?,?)
        """,
        (
            audit_id,
            owner["tenant_id"],
            owner["user_id"],
            owner["email"],
            owner["role"],
            invitation_id,
            json.dumps(after, sort_keys=True, separators=(",", ":")),
            '["email_fingerprint","expires_at","role_code","status"]',
            "req_prd085_invited",
            "trc_prd085_invited",
            "2026-08-17T04:00:00Z",
        ),
    )
    return audit_id


def _insert_webhook_down_guard(
    connection: sqlite3.Connection,
    *,
    fact_kind: str,
) -> None:
    """按场景插入订阅、投递、尝试或死信依赖，验证 down 迁移失败关闭。"""

    owner = _identity(connection)
    endpoint_id = f"whe_prd085_{fact_kind}"
    event_types = (
        '["product_event.recorded"]'
        if fact_kind == "subscription"
        else '["user.invited"]'
    )
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,max_attempts,
          secret_version,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,'active',5000,3,1,?,?,?,?)
        """,
        (
            endpoint_id,
            owner["tenant_id"],
            f"PRD-085 {fact_kind}",
            "https://example.invalid/prd085",
            event_types,
            "enc:v1:test-fixture",
            "test",
            "e" * 64,
            owner["user_id"],
            owner["user_id"],
            NOW,
            NOW,
        ),
    )
    if fact_kind == "subscription":
        return

    delivery_id = "whd_" + hashlib.sha256(fact_kind.encode()).hexdigest()[:32]
    failed = fact_kind == "dead_letter"
    event_id = f"evt_prd085_{fact_kind}"
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
          event_timestamp,payload_json,status,attempt_count,max_attempts,http_status,
          duration_ms,error_code,error_summary,next_retry_at,delivery_kind,
          replayed_from_delivery_id,replay_idempotency_key,replayed_by,replayed_at,
          secret_version,created_at,updated_at
        ) VALUES(?,?,?,?,? ,?,'{}',?,?,3,?,?,?, ?,NULL,'original',
                 NULL,NULL,NULL,NULL,1,?,?)
        """,
        (
            delivery_id,
            owner["tenant_id"],
            endpoint_id,
            event_id,
            "product_event.recorded",
            NOW,
            "failed" if failed else "pending",
            1 if failed else 0,
            503 if failed else None,
            12 if failed else None,
            "webhook.delivery_unavailable" if failed else None,
            "test-only" if failed else None,
            NOW,
            NOW,
        ),
    )
    if fact_kind == "attempt":
        connection.execute(
            """
            INSERT INTO webhook_delivery_attempts(
              webhook_delivery_attempt_id,tenant_id,webhook_delivery_id,
              webhook_endpoint_id,attempt_no,status,http_status,duration_ms,
              error_code,error_summary,created_at
            ) VALUES(?,?,?,?,1,'failed',503,12,'webhook.delivery_unavailable','test-only',?)
            """,
            (
                "wha_" + "8" * 32,
                owner["tenant_id"],
                delivery_id,
                endpoint_id,
                NOW,
            ),
        )
    if fact_kind == "dead_letter":
        assert connection.execute(
            """
            SELECT COUNT(*) FROM webhook_dead_letter_events
            WHERE tenant_id=? AND webhook_delivery_id=?
              AND event_type='product_event.recorded' AND status='dead_letter'
            """,
            (owner["tenant_id"], delivery_id),
        ).fetchone()[0] == 1


def _insert_rbac_down_guard(connection: sqlite3.Connection) -> None:
    """制造租户自定义 ops_admin 矩阵漂移，证明回滚不会覆盖管理员变更。"""

    owner = _identity(connection)
    connection.execute(
        """
        UPDATE roles
        SET permission_revision=permission_revision+1,matrix_sha256=?,updated_at=?
        WHERE tenant_id=? AND code='ops_admin'
        """,
        ("f" * 64, "2099-01-01T00:00:00Z", owner["tenant_id"]),
    )


def _down_guard_snapshot(connection: sqlite3.Connection) -> dict[str, tuple[tuple, ...]]:
    """冻结所有回滚门禁相关表内容，用于验证失败后没有部分删除或改写。"""

    tables = (
        "webhook_endpoints",
        "webhook_deliveries",
        "webhook_delivery_attempts",
        "webhook_dead_letter_events",
        "roles",
        "role_permissions",
        "permissions",
    )
    return {
        table: tuple(tuple(row) for row in connection.execute(f"SELECT rowid,* FROM {table}"))
        for table in tables
    }


def test_empty_database_seed_and_revision_20_contract(
    empty_0078: Path,
    tmp_path: Path,
) -> None:
    """验证空库 0078 结构、默认种子回填、revision 20 权限哈希和数据库健康。"""

    database_url = _clone(empty_0078, tmp_path / "seed-contract.sqlite3")
    connection = connect_database(database_url)
    assert connection.execute("SELECT COUNT(*) FROM product_events").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM product_event_outbox").fetchone()[0] == 0
    _healthy(connection)
    connection.close()

    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    events = connection.execute(
        """
        SELECT tenant_id,event_type,source_kind,capture_mode,actor_user_id
        FROM product_events ORDER BY product_event_id
        """
    ).fetchall()
    assert [tuple(row) for row in events] == [
        (seed.tenant_id, "tenant.registered", "seed_run", "backfill", None)
    ]
    assert connection.execute("SELECT COUNT(*) FROM product_event_outbox").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM rbac_permission_templates").fetchone()[0] == 89
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_role_permission_templates"
    ).fetchone()[0] == 234
    assert _hashes(connection) == (CATALOG_HASH_20, ROLE_HASH_20)
    grants = {
        row[0]
        for row in connection.execute(
            """
            SELECT role.code FROM roles role
            JOIN role_permissions relation
              ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE permission.code='product_analytics.read'
            """
        )
    }
    assert grants == {"admin", "ops_admin"}
    _healthy(connection)
    connection.close()


def test_live_audit_and_first_success_sources_are_atomic_and_unique(
    seeded_0078: Path,
    tmp_path: Path,
) -> None:
    """验证实时审计与首次成功请求原子生成去重 ProductEvent 和 source outbox。"""

    database_url = _clone(seeded_0078, tmp_path / "live-sources.sqlite3")
    connection = connect_database(database_url)
    owner = _identity(connection)
    _insert_audit(
        connection,
        audit_id="aud_prd085_provider",
        action="provider.created",
        object_type="provider",
        object_id=owner["provider_id"],
        created_at="2026-08-17T01:00:00Z",
    )
    _insert_audit(
        connection,
        audit_id="aud_prd085_virtual_key",
        action="virtual_key.created",
        object_type="virtual_key",
        object_id=owner["virtual_key_id"],
        created_at="2026-08-17T01:00:01Z",
    )
    _insert_request_log(
        connection,
        tenant_id=owner["tenant_id"],
        request_id="req_prd085_failed",
        status="failed",
        created_at="2026-08-17T01:00:02Z",
    )
    _insert_request_log(
        connection,
        tenant_id=owner["tenant_id"],
        request_id="req_prd085_first_success",
        status="success",
        created_at="2026-08-17T01:00:03Z",
    )
    _insert_request_log(
        connection,
        tenant_id=owner["tenant_id"],
        request_id="req_prd085_second_success",
        status="success",
        created_at="2026-08-17T01:00:04Z",
    )
    connection.commit()

    rows = connection.execute(
        """
        SELECT event_type,source_id,capture_mode,operation_request_id
        FROM product_events WHERE capture_mode='live' ORDER BY occurred_at,event_type
        """
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("provider.created", "aud_prd085_provider", "live", "req_aud_prd085_provider"),
        (
            "virtual_key.created",
            "aud_prd085_virtual_key",
            "live",
            "req_aud_prd085_virtual_key",
        ),
        (
            "gateway.first_call_succeeded",
            "req_prd085_first_success",
            "live",
            "req_prd085_first_success",
        ),
    ]
    assert connection.execute("SELECT COUNT(*) FROM product_event_outbox").fetchone()[0] == 3
    assert connection.execute(
        "SELECT COUNT(*) FROM product_events WHERE source_id='req_prd085_failed'"
    ).fetchone()[0] == 0
    _healthy(connection)
    connection.close()


def test_config_import_projects_provider_and_virtual_key_events(
    seeded_0078: Path,
    tmp_path: Path,
) -> None:
    """验证配置导入只按安全计数投影 Provider 与 Virtual Key 两类事件。"""

    database_url = _clone(seeded_0078, tmp_path / "config-import.sqlite3")
    connection = connect_database(database_url)
    owner = _identity(connection)
    audit_id = _insert_config_import_audit(connection)
    connection.commit()

    rows = connection.execute(
        """
        SELECT event_type,actor_user_id,source_kind,source_id,
               operation_request_id,operation_trace_id,capture_mode
        FROM product_events WHERE source_id=? ORDER BY event_type
        """,
        (audit_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (
            "provider.created",
            owner["user_id"],
            "audit_log",
            audit_id,
            "req_prd085_config_import",
            "trc_prd085_config_import",
            "live",
        ),
        (
            "virtual_key.created",
            owner["user_id"],
            "audit_log",
            audit_id,
            "req_prd085_config_import",
            "trc_prd085_config_import",
            "live",
        ),
    ]
    assert connection.execute(
        "SELECT COUNT(*) FROM product_event_outbox WHERE tenant_id=?",
        (owner["tenant_id"],),
    ).fetchone()[0] == 2
    _healthy(connection)
    connection.close()


def test_failed_request_update_to_success_projects_first_call_once(
    seeded_0078: Path,
    tmp_path: Path,
) -> None:
    """验证失败请求转成功只生成一次租户级首次成功调用事实。"""

    database_url = _clone(seeded_0078, tmp_path / "request-update.sqlite3")
    connection = connect_database(database_url)
    owner = _identity(connection)
    request_id = "req_prd085_update_success"
    _insert_request_log(
        connection,
        tenant_id=owner["tenant_id"],
        request_id=request_id,
        status="failed",
        created_at="2026-08-17T03:15:00Z",
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM product_events WHERE source_id=?", (request_id,)
    ).fetchone()[0] == 0
    connection.execute(
        "UPDATE request_logs SET status='success',updated_at=? "
        "WHERE tenant_id=? AND request_id=?",
        ("2026-08-17T03:15:01Z", owner["tenant_id"], request_id),
    )
    connection.execute(
        "UPDATE request_logs SET status='success',updated_at=? "
        "WHERE tenant_id=? AND request_id=?",
        ("2026-08-17T03:15:02Z", owner["tenant_id"], request_id),
    )
    connection.commit()

    row = connection.execute(
        """
        SELECT event_type,actor_user_id,source_kind,source_id,
               operation_request_id,operation_trace_id,capture_mode,occurred_at
        FROM product_events WHERE source_id=?
        """,
        (request_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == (
        "gateway.first_call_succeeded",
        None,
        "request_log",
        request_id,
        request_id,
        f"trc_{request_id}",
        "live",
        "2026-08-17T03:15:01Z",
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM product_events WHERE source_id=?", (request_id,)
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM product_event_outbox"
    ).fetchone()[0] == 1
    _healthy(connection)
    connection.close()


def test_request_log_query_projects_exact_actor_request_and_trace(
    seeded_0078: Path,
    tmp_path: Path,
) -> None:
    """验证成功日志查询把认证操作者和原始请求追踪链精确投影到事件。"""

    database_url = _clone(seeded_0078, tmp_path / "request-query.sqlite3")
    connection = connect_database(database_url)
    owner = _identity(connection)
    query_event_id = _insert_request_log_query(connection)
    connection.commit()

    row = connection.execute(
        """
        SELECT event_type,actor_user_id,source_kind,source_id,
               operation_request_id,operation_trace_id,capture_mode,occurred_at
        FROM product_events WHERE source_id=?
        """,
        (query_event_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == (
        "request_logs.viewed",
        owner["user_id"],
        "request_log_query",
        query_event_id,
        "req_prd085_query",
        "trc_prd085_query",
        "live",
        "2026-08-17T03:30:01Z",
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM product_event_outbox"
    ).fetchone()[0] == 1
    _healthy(connection)
    connection.close()


def test_budget_and_invitation_audits_project_activation_events(
    seeded_0078: Path,
    tmp_path: Path,
) -> None:
    """验证预算创建更新和邀请发出映射到固定阶段且不携带敏感业务字段。"""

    database_url = _clone(seeded_0078, tmp_path / "budget-invitation.sqlite3")
    connection = connect_database(database_url)
    _insert_audit(
        connection,
        audit_id="aud_prd085_budget_created",
        action="budget_rule.created",
        object_type="budget_rule",
        object_id="br_prd085_created",
        created_at="2026-08-17T04:00:00Z",
    )
    _insert_audit(
        connection,
        audit_id="aud_prd085_budget_updated",
        action="budget_rule.updated",
        object_type="budget_rule",
        object_id="br_prd085_updated",
        created_at="2026-08-17T04:00:01Z",
    )
    invitation_audit_id = _insert_invitation_audit(connection)
    connection.commit()

    rows = connection.execute(
        """
        SELECT event_type,source_id,operation_request_id,operation_trace_id
        FROM product_events WHERE source_id IN(?,?,?) ORDER BY source_id
        """,
        (
            "aud_prd085_budget_created",
            "aud_prd085_budget_updated",
            invitation_audit_id,
        ),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (
            "budget.configured",
            "aud_prd085_budget_created",
            "req_aud_prd085_budget_created",
            "trc_aud_prd085_budget_created",
        ),
        (
            "budget.configured",
            "aud_prd085_budget_updated",
            "req_aud_prd085_budget_updated",
            "trc_aud_prd085_budget_updated",
        ),
        (
            "teammate.invited",
            invitation_audit_id,
            "req_prd085_invited",
            "trc_prd085_invited",
        ),
    ]
    assert connection.execute(
        "SELECT COUNT(*) FROM product_event_outbox"
    ).fetchone()[0] == 3
    _healthy(connection)
    connection.close()


def test_down_reup_is_stable_and_restores_revision_19(
    seeded_0078: Path,
    tmp_path: Path,
) -> None:
    """验证仅含可重建回填事实时 down/re-up 稳定，并精确恢复 revision 19。"""

    database_url = _clone(seeded_0078, tmp_path / "round-trip.sqlite3")
    connection = connect_database(database_url)
    before_ids = [row[0] for row in connection.execute(
        "SELECT product_event_id FROM product_events ORDER BY product_event_id"
    )]
    connection.close()

    result = rollback_last_migration(
        database_url,
        migrations_dir=MIGRATIONS_DIR,
        expected_version="0078",
    )
    assert result.rolled_back == "0078"
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version DESC LIMIT 1"
    ).fetchone()[0] == "0077"
    assert connection.execute("SELECT COUNT(*) FROM rbac_permission_templates").fetchone()[0] == 88
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_role_permission_templates"
    ).fetchone()[0] == 232
    assert _hashes(connection) == (CATALOG_HASH_19, ROLE_HASH_19)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%prd085%'"
    ).fetchone()[0] == 0
    _healthy(connection)
    connection.close()

    assert apply_migrations(database_url, migrations_dir=MIGRATIONS_DIR).applied == ["0078"]
    connection = connect_database(database_url)
    after_ids = [row[0] for row in connection.execute(
        "SELECT product_event_id FROM product_events ORDER BY product_event_id"
    )]
    assert after_ids == before_ids
    _healthy(connection)
    connection.close()


@pytest.mark.parametrize(
    "fact_kind",
    ("subscription", "delivery", "attempt", "dead_letter", "rbac_drift"),
)
def test_down_blocks_webhook_dependencies_and_rbac_drift_without_mutation(
    seeded_0078: Path,
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """验证外部可见 Webhook 依赖或租户 RBAC 漂移会阻断回滚且不产生部分变更。"""

    database_url = _clone(seeded_0078, tmp_path / f"down-{fact_kind}.sqlite3")
    connection = connect_database(database_url)
    if fact_kind == "rbac_drift":
        _insert_rbac_down_guard(connection)
    else:
        _insert_webhook_down_guard(connection, fact_kind=fact_kind)
    connection.commit()
    before = _down_guard_snapshot(connection)
    connection.close()

    with pytest.raises(DbError) as captured:
        rollback_last_migration(
            database_url,
            migrations_dir=MIGRATIONS_DIR,
            expected_version="0078",
        )
    assert captured.value.code == "db.migration_failed"

    connection = connect_database(database_url)
    assert _down_guard_snapshot(connection) == before
    assert connection.execute(
        "SELECT version FROM schema_migrations WHERE status='applied' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()[0] == "0078"
    _healthy(connection)
    connection.close()


def test_down_blocks_live_facts_without_partial_mutation(
    seeded_0078: Path,
    tmp_path: Path,
) -> None:
    """验证存在实时 ProductEvent 时回滚失败关闭，并保持全部表结构和业务事实。"""

    database_url = _clone(seeded_0078, tmp_path / "down-block.sqlite3")
    connection = connect_database(database_url)
    owner = _identity(connection)
    _insert_audit(
        connection,
        audit_id="aud_prd085_down_block",
        action="provider.created",
        object_type="provider",
        object_id=owner["provider_id"],
        created_at="2026-08-17T02:00:00Z",
    )
    connection.commit()
    before = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT rowid,* FROM product_events ORDER BY rowid"
        )
    )
    connection.close()

    with pytest.raises(DbError) as captured:
        rollback_last_migration(
            database_url,
            migrations_dir=MIGRATIONS_DIR,
            expected_version="0078",
        )
    assert captured.value.code == "db.migration_failed"
    connection = connect_database(database_url)
    after = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT rowid,* FROM product_events ORDER BY rowid"
        )
    )
    assert after == before
    assert connection.execute(
        "SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version DESC LIMIT 1"
    ).fetchone()[0] == "0078"
    _healthy(connection)
    connection.close()
