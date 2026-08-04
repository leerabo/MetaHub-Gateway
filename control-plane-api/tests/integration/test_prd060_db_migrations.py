"""PRD-060 0051 用户邀请流程与 0052 接受审计隐私迁移专项测试。

测试只在 pytest 独占 SQLite 文件中写入固定治理夹具，覆盖历史保真、严格 UTC 时间、
条件回滚和未知依赖。原始邀请 Token、密码、Provider Key、Prompt 与 Response 均不落库。
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
from metahub_shared.rbac import permission_matrix_sha256


# 0051/0052 必须建立在连续历史上；测试复制正式迁移而不改写任何已应用 checksum。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
EXPECTED_VERSIONS_0051 = [f"{version:04d}" for version in range(1, 52)]
EXPECTED_VERSIONS_0052 = [f"{version:04d}" for version in range(1, 53)]
NOW = "2026-07-31T12:00:00Z"
REDACTED_ACCEPTOR_EMAIL = "invitation-acceptor@redacted.invalid"
PRESERVED_TABLES = (
    "audit_logs",
    "audit_log_export_sequences",
    "audit_exports",
    "audit_export_events",
    "role_permission_events",
)


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占文件转换为迁移器支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不高于目标版本的正式 up/down 文件，冻结被测迁移栈顶。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """验证内容完整性和全部租户复合外键，避免只凭迁移版本判断成功。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _admin(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
    """按 tenant-qualified 条件读取管理员，测试夹具自身也不得跨租户选人。"""

    actor = connection.execute(
        """
        SELECT user_id,email,role FROM users
        WHERE tenant_id=? AND role='admin' AND status='active'
        ORDER BY user_id LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert actor is not None
    return actor


def _table_snapshot(
    connection: sqlite3.Connection,
    table_name: str,
) -> list[tuple[object, ...]]:
    """冻结允许保真的五张表全部列和 rowid，防止仅比较行数漏掉漂移。"""

    assert table_name in PRESERVED_TABLES
    return [
        tuple(row)
        for row in connection.execute(f"SELECT rowid,* FROM {table_name} ORDER BY rowid")
    ]


def _preserved_snapshot(connection: sqlite3.Connection) -> dict[str, object]:
    """冻结 0050 历史事实和稳定导出自增水位。"""

    return {
        **{table: _table_snapshot(connection, table) for table in PRESERVED_TABLES},
        "sequence": [
            tuple(row)
            for row in connection.execute(
                "SELECT name,seq FROM sqlite_sequence "
                "WHERE name='audit_log_export_sequences'"
            )
        ],
    }


def _database_dump(connection: sqlite3.Connection) -> tuple[str, ...]:
    """取得原子阻断前后的全库逻辑快照，不把数据库文件路径写入断言。"""

    return tuple(connection.iterdump())


def _insert_plain_audit(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
    audit_log_id: str,
    created_at: str = NOW,
) -> None:
    """插入一条不依赖新邀请对象的旧枚举审计，用于验证 rowid 和序列保真。"""

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
            "provider_prd060_preserved",
            None,
            '{"status":"inactive"}',
            '{"status":"active"}',
            '["status"]',
            None,
            "prd060-migration-test",
            f"req_{audit_log_id}",
            f"trc_{audit_log_id}",
            created_at,
        ),
    )


def _insert_export_graph(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
    suffix: str,
    filters: dict[str, object],
) -> None:
    """插入零行 AuditExport、source event 与审计，验证筛选证据和父子图保真。"""

    filters_json = json.dumps(filters, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    filter_fingerprint = hashlib.sha256(filters_json.encode()).hexdigest()
    content_sha256 = hashlib.sha256(f"prd060-{suffix}".encode()).hexdigest()
    export_id = f"aex_{suffix:0>32}"[-36:]
    audit_id = f"aud_prd060_export_{suffix}"
    event_id = f"aexevt_{suffix:0>32}"[-39:]
    request_id = f"req_prd060_export_{suffix}"
    trace_id = f"trc_prd060_export_{suffix}"
    payload = {
        "audit_export_id": export_id,
        "content_sha256": content_sha256,
        "content_size_bytes": 64,
        "filter_fingerprint": filter_fingerprint,
        "format": "csv",
        "row_count": 0,
        "schema_version": 1,
        "status": "completed",
    }
    payload_json = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        """
        INSERT INTO audit_exports(
          audit_export_id,tenant_id,requested_by,audit_log_id,status,format,schema_version,
          filters_json,filter_fingerprint,snapshot_sequence,snapshot_audit_log_id,
          snapshot_created_at,row_count,content_size_bytes,content_sha256,file_name,
          request_id,trace_id,event_id,event_payload_json,created_at,updated_at
        ) VALUES(?,?,?,?,'completed','csv',1,?,?,0,NULL,NULL,0,64,?,?,?, ?,?,?,?,?)
        """,
        (
            export_id,
            tenant_id,
            actor["user_id"],
            audit_id,
            filters_json,
            filter_fingerprint,
            content_sha256,
            f"metahub-audit-{suffix}.csv",
            request_id,
            trace_id,
            event_id,
            payload_json,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO audit_export_events(
          audit_export_event_id,tenant_id,audit_export_id,status,attempt_count,
          next_attempt_at,claim_token,claim_owner,lease_until,last_error_code,
          dispatched_at,created_at,updated_at
        ) VALUES(?,?,?,'pending',0,?,NULL,NULL,NULL,NULL,NULL,?,?)
        """,
        (event_id, tenant_id, export_id, NOW, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'audit.exported','audit_export',?,NULL,'{}',?,?,NULL,?,?,?,?)
        """,
        (
            audit_id,
            tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            export_id,
            payload_json,
            json.dumps(sorted(payload), ensure_ascii=True, separators=(",", ":")),
            "prd060-migration-test",
            request_id,
            trace_id,
            NOW,
        ),
    )


def _insert_role_permission_fact(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
) -> None:
    """生成一条真实 0050 RolePermission 变更、审计和 source event 历史事实。"""

    role = connection.execute(
        """
        SELECT role_id,code,name,permission_revision,matrix_sha256
        FROM roles WHERE tenant_id=? AND code='developer'
        """,
        (tenant_id,),
    ).fetchone()
    assert role is not None
    before_codes = sorted(
        row[0]
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
    )
    removed_code = "debug_console.use"
    assert removed_code in before_codes
    after_codes = sorted(set(before_codes) - {removed_code})
    after_sha256 = permission_matrix_sha256(after_codes)
    connection.execute(
        """
        DELETE FROM role_permissions
        WHERE tenant_id=? AND role_id=? AND permission_id=(
          SELECT permission_id FROM permissions WHERE tenant_id=? AND code=?
        )
        """,
        (tenant_id, role["role_id"], tenant_id, removed_code),
    )
    connection.execute(
        """
        UPDATE roles
        SET permission_revision=2,matrix_sha256=?,updated_at=?
        WHERE tenant_id=? AND role_id=? AND permission_revision=1
        """,
        (after_sha256, NOW, tenant_id, role["role_id"]),
    )
    before_summary = {
        "role_code": role["code"],
        "permission_revision": 1,
        "matrix_sha256": role["matrix_sha256"],
        "permission_count": len(before_codes),
    }
    after_summary = {
        "role_code": role["code"],
        "permission_revision": 2,
        "matrix_sha256": after_sha256,
        "permission_count": len(after_codes),
    }
    audit_id = "aud_prd060_role_permission"
    request_id = "req_prd060_role_permission"
    trace_id = "trc_prd060_role_permission"
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'role.permission_changed','role_permission',?,?,?,?,?,NULL,?,?,?,?)
        """,
        (
            audit_id,
            tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            role["role_id"],
            role["name"],
            json.dumps(before_summary, separators=(",", ":"), sort_keys=True),
            json.dumps(after_summary, separators=(",", ":"), sort_keys=True),
            '["matrix_sha256","permission_count","permission_revision"]',
            "prd060-migration-test",
            request_id,
            trace_id,
            NOW,
        ),
    )
    payload = {
        "schema_version": 1,
        "role_id": role["role_id"],
        "role_code": role["code"],
        "before_revision": 1,
        "after_revision": 2,
        "before_sha256": role["matrix_sha256"],
        "after_sha256": after_sha256,
        "added_permissions": [],
        "removed_permissions": [removed_code],
    }
    connection.execute(
        """
        INSERT INTO role_permission_events(
          event_id,tenant_id,role_id,actor_user_id,actor_role,request_id,
          before_revision,after_revision,before_sha256,after_sha256,
          added_permissions_json,removed_permissions_json,audit_log_id,trace_id,
          payload_json,delivery_status,last_error_code,created_at,updated_at,dispatched_at
        ) VALUES(?,?,?,?,?,?,1,2,?,?,'[]',?,?,?,?,'pending',NULL,?,?,NULL)
        """,
        (
            f"rpevt_{'6' * 32}",
            tenant_id,
            role["role_id"],
            actor["user_id"],
            actor["role"],
            request_id,
            role["matrix_sha256"],
            after_sha256,
            json.dumps([removed_code], separators=(",", ":")),
            audit_id,
            trace_id,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            NOW,
            NOW,
        ),
    )


def _insert_invitation(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
    marker: str,
    expires_at: str = "2026-08-01T12:00:00Z",
    created_at: str = NOW,
    updated_at: str = NOW,
) -> str:
    """插入不含原始 Token 的合法 pending Invitation，并返回稳定 ID。"""

    invitation_id = f"inv_{marker * 32}"
    connection.execute(
        """
        INSERT INTO invitations(
          invitation_id,tenant_id,email,email_fingerprint,role_code,token_hash,status,
          invited_by,accepted_user_id,expires_at,accepted_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'pending',?,NULL,?,NULL,?,?)
        """,
        (
            invitation_id,
            tenant_id,
            f"prd060-{marker}@example.com",
            marker * 64,
            "developer",
            hashlib.sha256(f"fixture-{marker}".encode()).hexdigest(),
            actor["user_id"],
            expires_at,
            created_at,
            updated_at,
        ),
    )
    return invitation_id


def _insert_invitation_graph(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
) -> None:
    """插入 Invitation、强审计与 source event 完整图，供 down 业务事实门禁验证。"""

    invitation_id = _insert_invitation(
        connection,
        tenant_id=tenant_id,
        actor=actor,
        marker="7",
    )
    fingerprint = "7" * 64
    audit_id = "aud_prd060_invited_guard"
    request_id = "req_prd060_invited_guard"
    trace_id = "trc_prd060_invited_guard"
    after_summary = {
        "email_fingerprint": fingerprint,
        "role_code": "developer",
        "status": "pending",
        "expires_at": "2026-08-01T12:00:00Z",
        "accepted_user_id": None,
    }
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'user.invited','invitation',?,NULL,'{}',?,?,NULL,NULL,?,?,?)
        """,
        (
            audit_id,
            tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            invitation_id,
            json.dumps(after_summary, separators=(",", ":"), sort_keys=True),
            '["email_fingerprint","expires_at","role_code","status"]',
            request_id,
            trace_id,
            NOW,
        ),
    )
    event_payload = {
        "schema_version": 1,
        "invitation_id": invitation_id,
        "email_fingerprint": fingerprint,
        "role_code": "developer",
        "expires_at": "2026-08-01T12:00:00Z",
        "invited_at": NOW,
    }
    connection.execute(
        """
        INSERT INTO invitation_events(
          invitation_event_id,tenant_id,invitation_id,event_type,actor_user_id,actor_role,
          audit_log_id,request_id,trace_id,payload_json,delivery_status,last_error_code,
          created_at,updated_at,dispatched_at
        ) VALUES(?,?,?,'user.invited',?,?,?,?,?,?,'pending',NULL,?,?,NULL)
        """,
        (
            f"ivevt_{'7' * 32}",
            tenant_id,
            invitation_id,
            actor["user_id"],
            actor["role"],
            audit_id,
            request_id,
            trace_id,
            json.dumps(event_payload, separators=(",", ":"), sort_keys=True),
            NOW,
            NOW,
        ),
    )


def _insert_webhook_graph(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
) -> None:
    """插入邀请订阅、失败 delivery、attempt 与自动生成 dead-letter 完整图。"""

    endpoint_id = "whe_prd060_down_guard"
    delivery_id = f"whd_{'8' * 32}"
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,max_attempts,
          secret_version,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?, 'active',5000,3,1,?,?,?,?)
        """,
        (
            endpoint_id,
            tenant_id,
            "PRD-060 down guard",
            "https://example.invalid/prd060",
            '["user.invited","invitation.accepted"]',
            "enc:v1:test-fixture",
            "test",
            "8" * 64,
            actor["user_id"],
            actor["user_id"],
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
          event_timestamp,payload_json,status,attempt_count,max_attempts,http_status,
          duration_ms,error_code,error_summary,next_retry_at,delivery_kind,
          replayed_from_delivery_id,replay_idempotency_key,replayed_by,replayed_at,
          secret_version,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'{}','failed',1,3,503,12,'webhook.delivery_unavailable',
                 'test-only',NULL,'original',NULL,NULL,NULL,NULL,1,?,?)
        """,
        (
            delivery_id,
            tenant_id,
            endpoint_id,
            "evt_prd060_down_guard",
            "user.invited",
            NOW,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO webhook_delivery_attempts(
          webhook_delivery_attempt_id,tenant_id,webhook_delivery_id,webhook_endpoint_id,
          attempt_no,status,http_status,duration_ms,error_code,error_summary,created_at
        ) VALUES(?,?,?,?,1,'failed',503,12,'webhook.delivery_unavailable','test-only',?)
        """,
        (f"wha_{'8' * 32}", tenant_id, delivery_id, endpoint_id, NOW),
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM webhook_dead_letter_events WHERE webhook_delivery_id=?",
        (delivery_id,),
    ).fetchone()[0] == 1


def _assert_down_sql_blocked(connection: sqlite3.Connection) -> None:
    """直接执行正式 down 并验证 guard 失败后 schema 与全部事实原子不变。"""

    before = _database_dump(connection)
    down_sql = (MIGRATIONS_DIR / "0051_user_invitation_join_flow.down.sql").read_text(
        encoding="utf-8"
    )
    with pytest.raises(sqlite3.IntegrityError, match="prd060_down_blocked"):
        connection.executescript(down_sql)
    connection.rollback()
    assert _database_dump(connection) == before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0051'"
    ).fetchone()[0] == "applied"
    _assert_healthy(connection)


def _insert_accepted_audit(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    invitation_id: str,
    accepted_actor: sqlite3.Row,
    audit_log_id: str,
    actor_email: str,
) -> None:
    """插入一条接受审计；调用方可替换邮箱，专项验证 0051/0052 owner 差异。"""

    invitation = connection.execute(
        """
        SELECT email_fingerprint,role_code,expires_at,accepted_user_id
        FROM invitations WHERE tenant_id=? AND invitation_id=?
        """,
        (tenant_id, invitation_id),
    ).fetchone()
    assert invitation is not None
    before_summary = {
        "email_fingerprint": invitation["email_fingerprint"],
        "role_code": invitation["role_code"],
        "status": "pending",
        "expires_at": invitation["expires_at"],
        "accepted_user_id": None,
    }
    after_summary = {
        **before_summary,
        "status": "accepted",
        "accepted_user_id": invitation["accepted_user_id"],
    }
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'invitation.accepted','invitation',?,NULL,?,?,?,NULL,?,?,?,?)
        """,
        (
            audit_log_id,
            tenant_id,
            accepted_actor["user_id"],
            actor_email,
            accepted_actor["role"],
            invitation_id,
            json.dumps(before_summary, separators=(",", ":"), sort_keys=True),
            json.dumps(after_summary, separators=(",", ":"), sort_keys=True),
            '["accepted_user_id","status"]',
            "prd060-privacy-migration-test",
            f"req_{audit_log_id}",
            f"trc_{audit_log_id}",
            NOW,
        ),
    )


def _insert_legacy_accepted_graph(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    inviter: sqlite3.Row,
    marker: str = "a",
) -> tuple[str, sqlite3.Row, str]:
    """在 0051 上构造完整 User/Invitation/AuditLog 图，邮箱仍为旧版明文合同。"""

    invitation_id = _insert_invitation(
        connection,
        tenant_id=tenant_id,
        actor=inviter,
        marker=marker,
    )
    accepted_user_id = f"user_prd060_privacy_{marker}"
    accepted_email = f"prd060-{marker}@example.com"
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        (
            accepted_user_id,
            tenant_id,
            accepted_email,
            "PRD-060 privacy fixture",
            "test-only-password-hash",
            "developer",
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        UPDATE invitations
        SET status='accepted',accepted_user_id=?,accepted_at=?,updated_at=?
        WHERE tenant_id=? AND invitation_id=? AND status='pending'
        """,
        (accepted_user_id, NOW, NOW, tenant_id, invitation_id),
    )
    accepted_actor = connection.execute(
        """
        SELECT user_id,email,role FROM users
        WHERE tenant_id=? AND user_id=?
        """,
        (tenant_id, accepted_user_id),
    ).fetchone()
    assert accepted_actor is not None
    audit_log_id = f"aud_prd060_privacy_{marker}"
    _insert_accepted_audit(
        connection,
        tenant_id=tenant_id,
        invitation_id=invitation_id,
        accepted_actor=accepted_actor,
        audit_log_id=audit_log_id,
        actor_email=accepted_actor["email"],
    )
    return invitation_id, accepted_actor, audit_log_id


def _assert_privacy_down_sql_blocked(connection: sqlite3.Connection) -> None:
    """直接执行 0052 down，并证明阻断前后的持久 schema 与业务数据完全相同。"""

    before = _database_dump(connection)
    down_sql = (MIGRATIONS_DIR / "0052_invitation_audit_privacy.down.sql").read_text(
        encoding="utf-8"
    )
    with pytest.raises(sqlite3.IntegrityError, match="prd060_privacy_down_blocked"):
        connection.executescript(down_sql)
    connection.rollback()
    assert _database_dump(connection) == before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0052'"
    ).fetchone()[0] == "applied"
    _assert_healthy(connection)


def _assert_privacy_up_sql_blocked(connection: sqlite3.Connection) -> None:
    """执行正式 0052 up，证明缺失依赖在 trigger 与审计数据变化前原子阻断。"""

    before = _database_dump(connection)
    owner_before = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_audit_logs_owner_insert'"
    ).fetchone()[0]
    up_sql = (MIGRATIONS_DIR / "0052_invitation_audit_privacy.sql").read_text(
        encoding="utf-8"
    )
    with pytest.raises(sqlite3.IntegrityError, match="prd060_privacy_up_graph_invalid"):
        connection.executescript(up_sql)
    connection.rollback()
    assert _database_dump(connection) == before
    assert connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_audit_logs_owner_insert'"
    ).fetchone()[0] == owner_before


def test_prd060_fresh_repeat_and_invalid_calendar_times(tmp_path: Path) -> None:
    """主/异常流程：连续迁移幂等，七个时间分支拒绝 SQLite NULL 绕过。"""

    database_url = _sqlite_url(tmp_path / "prd060-time-constraints.db")
    migrations = _migration_subset(tmp_path / "migrations-through-0051-time", 51)
    assert (
        apply_migrations(database_url, migrations_dir=migrations).applied
        == EXPECTED_VERSIONS_0051
    )
    repeat = apply_migrations(database_url, migrations_dir=migrations)
    assert repeat.applied == [] and repeat.skipped == EXPECTED_VERSIONS_0051
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = _admin(connection, seed.tenant_id)

    # 非法日历值外形满足 20 字符和 UTC Z，但 strftime 返回 NULL；必须由表 CHECK 拒绝。
    invalid_time = "2026-99-99T99:99:99Z"
    invalid_rows = (
        {"marker": "1", "expires_at": invalid_time, "created_at": NOW, "updated_at": NOW},
        {
            "marker": "2",
            "expires_at": "2026-08-01T12:00:00Z",
            "created_at": invalid_time,
            "updated_at": invalid_time,
        },
        {
            # SQLite 3.50 会原样接受 24:59:59；`+0 seconds` 必须把它规范化后拒绝。
            "marker": "5",
            "expires_at": "2026-01-01T24:59:59Z",
            "created_at": "2026-01-01T23:00:00Z",
            "updated_at": "2026-01-01T23:00:00Z",
        },
        {
            # 年份 0000 可能通过 strftime 回放，产品合同明确只接受 0001..9999。
            "marker": "6",
            "expires_at": "0000-01-02T00:00:00Z",
            "created_at": "0000-01-01T00:00:00Z",
            "updated_at": "0000-01-01T00:00:00Z",
        },
    )
    for values in invalid_rows:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_invitation(connection, tenant_id=seed.tenant_id, actor=actor, **values)
        connection.rollback()

    invitation_id = _insert_invitation(
        connection,
        tenant_id=seed.tenant_id,
        actor=actor,
        marker="3",
    )
    _insert_plain_audit(
        connection,
        tenant_id=seed.tenant_id,
        actor=actor,
        audit_log_id="aud_prd060_event_time",
    )
    connection.commit()
    # 本用例只测表级时间 CHECK，临时移除业务 owner trigger，避免其他业务条件掩盖结果。
    connection.execute("DROP TRIGGER trg_invitation_events_owner_insert_prd060")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            """
            INSERT INTO invitation_events(
              invitation_event_id,tenant_id,invitation_id,event_type,actor_user_id,actor_role,
              audit_log_id,request_id,trace_id,payload_json,delivery_status,last_error_code,
              created_at,updated_at,dispatched_at
            ) VALUES(?,?,?,'user.invited',?,?,?,?,?,'{}','pending',NULL,?,?,NULL)
            """,
            (
                f"ivevt_{'3' * 32}",
                seed.tenant_id,
                invitation_id,
                actor["user_id"],
                actor["role"],
                "aud_prd060_event_time",
                "req_prd060_invalid_event_time",
                "trc_prd060_invalid_event_time",
                invalid_time,
                invalid_time,
            ),
        )
    connection.rollback()

    # dispatched_at 与 updated_at 使用同一规范合同，24 点小时不能作为投递终态落库。
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            """
            INSERT INTO invitation_events(
              invitation_event_id,tenant_id,invitation_id,event_type,actor_user_id,actor_role,
              audit_log_id,request_id,trace_id,payload_json,delivery_status,last_error_code,
              created_at,updated_at,dispatched_at
            ) VALUES(?,?,?,'user.invited',?,?,?,?,?,'{}','dispatched',NULL,?,?,?)
            """,
            (
                f"ivevt_{'5' * 32}",
                seed.tenant_id,
                invitation_id,
                actor["user_id"],
                actor["role"],
                "aud_prd060_event_time",
                "req_prd060_invalid_dispatched_time",
                "trc_prd060_invalid_dispatched_time",
                NOW,
                "2026-07-31T24:59:59Z",
                "2026-07-31T24:59:59Z",
            ),
        )
    connection.rollback()

    connection.execute("DROP TRIGGER trg_invitations_owner_insert_prd060")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            """
            INSERT INTO invitations(
              invitation_id,tenant_id,email,email_fingerprint,role_code,token_hash,status,
              invited_by,accepted_user_id,expires_at,accepted_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'accepted',?,?,?,?,?,?)
            """,
            (
                f"inv_{'4' * 32}",
                seed.tenant_id,
                actor["email"],
                "4" * 64,
                actor["role"],
                "4" * 64,
                actor["user_id"],
                actor["user_id"],
                "2026-08-01T12:00:00Z",
                invalid_time,
                NOW,
                NOW,
            ),
        )
    connection.rollback()
    _assert_healthy(connection)
    connection.close()


def test_prd060_0050_up_down_reapply_preserves_history(tmp_path: Path) -> None:
    """兼容流程：非空 0050 审计、导出和权限事件经 up/down/re-up 全列保真。"""

    database_url = _sqlite_url(tmp_path / "prd060-history-roundtrip.db")
    through_0050 = _migration_subset(tmp_path / "migrations-through-0050", 50)
    through_0051 = _migration_subset(tmp_path / "migrations-through-0051", 51)
    assert (
        apply_migrations(database_url, migrations_dir=through_0050).applied
        == EXPECTED_VERSIONS_0051[:-1]
    )
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = _admin(connection, seed.tenant_id)
    _insert_plain_audit(
        connection,
        tenant_id=seed.tenant_id,
        actor=actor,
        audit_log_id="aud_prd060_legacy",
    )
    _insert_export_graph(
        connection,
        tenant_id=seed.tenant_id,
        actor=actor,
        suffix="60",
        filters={},
    )
    _insert_role_permission_fact(connection, tenant_id=seed.tenant_id, actor=actor)
    connection.commit()
    before = _preserved_snapshot(connection)
    _assert_healthy(connection)
    connection.close()

    assert apply_migrations(database_url, migrations_dir=through_0051).applied == ["0051"]
    connection = connect_database(database_url)
    assert _preserved_snapshot(connection) == before
    _assert_healthy(connection)
    connection.close()

    assert rollback_last_migration(database_url, migrations_dir=through_0051).rolled_back == "0051"
    connection = connect_database(database_url)
    assert _preserved_snapshot(connection) == before
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name IN ('invitations','invitation_events')"
    ).fetchone()[0] == 0
    assert "user.invited" not in connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="audit_export_immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO audit_exports SELECT * FROM audit_exports LIMIT 1"
        )
    connection.rollback()
    _assert_healthy(connection)
    connection.close()

    assert apply_migrations(database_url, migrations_dir=through_0051).applied == ["0051"]
    connection = connect_database(database_url)
    assert _preserved_snapshot(connection) == before
    _assert_healthy(connection)
    connection.close()


@pytest.mark.parametrize(
    "fact_kind",
    (
        "invitation_graph",
        "webhook_graph",
        "zero_row_export_filter",
        "later_migration",
        "unknown_schema_dependency",
    ),
)
def test_prd060_down_guard_blocks_dependencies_atomically(
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """回滚安全：任一邀请、投递、筛选或未知依赖都在首项持久 DDL 前原子阻断。"""

    database_url = _sqlite_url(tmp_path / f"prd060-down-{fact_kind}.db")
    migrations = _migration_subset(tmp_path / f"migrations-{fact_kind}", 51)
    apply_migrations(database_url, migrations_dir=migrations)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = _admin(connection, seed.tenant_id)
    if fact_kind == "invitation_graph":
        _insert_invitation_graph(connection, tenant_id=seed.tenant_id, actor=actor)
    elif fact_kind == "webhook_graph":
        _insert_webhook_graph(connection, tenant_id=seed.tenant_id, actor=actor)
    elif fact_kind == "zero_row_export_filter":
        _insert_export_graph(
            connection,
            tenant_id=seed.tenant_id,
            actor=actor,
            suffix="61",
            filters={"action": "user.invited"},
        )
    elif fact_kind == "later_migration":
        connection.execute(
            """
            INSERT INTO schema_migrations(
              version,name,checksum,status,applied_at,execution_time_ms,error_message
            ) VALUES('0052','prd060_future_dependency',?,'applied',?,0,NULL)
            """,
            ("9" * 64, NOW),
        )
    else:
        connection.execute(
            "CREATE VIEW prd060_unknown_dependency AS "
            "SELECT invitation_id FROM invitations"
        )
    connection.commit()
    _assert_down_sql_blocked(connection)
    connection.close()


def test_prd060_0052_fresh_stack_and_legacy_email_redaction(tmp_path: Path) -> None:
    """主流程：fresh 0001..0052 可运行，0051 合法接受图仅脱敏审计邮箱。"""

    fresh_url = _sqlite_url(tmp_path / "prd060-privacy-fresh.db")
    fresh_migrations = _migration_subset(tmp_path / "migrations-0052-fresh", 52)
    assert (
        apply_migrations(fresh_url, migrations_dir=fresh_migrations).applied
        == EXPECTED_VERSIONS_0052
    )
    fresh_connection = connect_database(fresh_url)
    owner_sql = fresh_connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_audit_logs_owner_insert'"
    ).fetchone()[0]
    assert REDACTED_ACCEPTOR_EMAIL in owner_sql
    _assert_healthy(fresh_connection)
    fresh_connection.close()

    legacy_url = _sqlite_url(tmp_path / "prd060-privacy-legacy.db")
    through_0051 = _migration_subset(tmp_path / "migrations-0051-legacy", 51)
    through_0052 = _migration_subset(tmp_path / "migrations-0052-legacy", 52)
    assert (
        apply_migrations(legacy_url, migrations_dir=through_0051).applied
        == EXPECTED_VERSIONS_0051
    )
    seed = apply_seed_data(legacy_url)
    connection = connect_database(legacy_url)
    inviter = _admin(connection, seed.tenant_id)
    invitation_id, accepted_actor, audit_log_id = _insert_legacy_accepted_graph(
        connection,
        tenant_id=seed.tenant_id,
        inviter=inviter,
    )
    connection.commit()
    before_non_email = tuple(
        connection.execute(
            """
            SELECT rowid,audit_log_id,tenant_id,actor_user_id,actor_role,action,object_type,
                   object_id,object_name,before_summary_json,after_summary_json,
                   changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at
            FROM audit_logs WHERE audit_log_id=?
            """,
            (audit_log_id,),
        ).fetchone()
    )
    assert connection.execute(
        "SELECT actor_email FROM audit_logs WHERE audit_log_id=?",
        (audit_log_id,),
    ).fetchone()[0] == accepted_actor["email"]
    # 接受后的用户角色/状态和租户允许合法变化；0052 必须仍能清理历史邮箱，
    # 不能把当前授权状态误当成不可变审计归属条件。
    connection.execute(
        "UPDATE users SET role='viewer',status='disabled' WHERE tenant_id=? AND user_id=?",
        (seed.tenant_id, accepted_actor["user_id"]),
    )
    connection.execute(
        "UPDATE tenants SET status='disabled' WHERE tenant_id=?",
        (seed.tenant_id,),
    )
    connection.commit()
    connection.close()


    assert apply_migrations(legacy_url, migrations_dir=through_0052).applied == ["0052"]
    connection = connect_database(legacy_url)
    after_non_email = tuple(
        connection.execute(
            """
            SELECT rowid,audit_log_id,tenant_id,actor_user_id,actor_role,action,object_type,
                   object_id,object_name,before_summary_json,after_summary_json,
                   changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at
            FROM audit_logs WHERE audit_log_id=?
            """,
            (audit_log_id,),
        ).fetchone()
    )
    assert after_non_email == before_non_email
    assert connection.execute(
        "SELECT actor_email FROM audit_logs WHERE audit_log_id=?",
        (audit_log_id,),
    ).fetchone()[0] == REDACTED_ACCEPTOR_EMAIL
    assert connection.execute(
        "SELECT email FROM invitations WHERE tenant_id=? AND invitation_id=?",
        (seed.tenant_id, invitation_id),
    ).fetchone()[0] == accepted_actor["email"]
    _assert_healthy(connection)
    connection.close()


def test_prd060_0052_redacts_legacy_email_after_legal_identity_drift(
    tmp_path: Path,
) -> None:
    """兼容流程：租户停用及用户角色/邮箱漂移不能阻止旧接受邮箱被不可逆脱敏。"""

    database_url = _sqlite_url(tmp_path / "prd060-privacy-legacy-drift.db")
    through_0051 = _migration_subset(tmp_path / "migrations-0051-legacy-drift", 51)
    through_0052 = _migration_subset(tmp_path / "migrations-0052-legacy-drift", 52)
    apply_migrations(database_url, migrations_dir=through_0051)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    invitation_id, accepted_actor, audit_log_id = _insert_legacy_accepted_graph(
        connection,
        tenant_id=seed.tenant_id,
        inviter=_admin(connection, seed.tenant_id),
        marker="d",
    )
    connection.commit()

    # 这些都是接受事实之后允许发生的当前状态变化；历史归属应以 Invitation 快照复核。
    changed_email = "renamed-prd060-d@example.com"
    connection.execute(
        "UPDATE tenants SET status='disabled' WHERE tenant_id=?",
        (seed.tenant_id,),
    )
    connection.execute(
        "UPDATE users SET email=?,role='viewer' WHERE tenant_id=? AND user_id=?",
        (changed_email, seed.tenant_id, accepted_actor["user_id"]),
    )
    connection.commit()
    connection.close()

    assert apply_migrations(database_url, migrations_dir=through_0052).applied == ["0052"]
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT actor_email FROM audit_logs WHERE audit_log_id=?",
        (audit_log_id,),
    ).fetchone()[0] == REDACTED_ACCEPTOR_EMAIL
    assert tuple(
        connection.execute(
            "SELECT email,role FROM users WHERE tenant_id=? AND user_id=?",
            (seed.tenant_id, accepted_actor["user_id"]),
        ).fetchone()
    ) == (changed_email, "viewer")
    assert connection.execute(
        "SELECT email FROM invitations WHERE tenant_id=? AND invitation_id=?",
        (seed.tenant_id, invitation_id),
    ).fetchone()[0] == accepted_actor["email"]
    _assert_healthy(connection)
    connection.close()


@pytest.mark.parametrize(
    "missing_table",
    ("tenants", "roles", "permissions", "role_permissions", "invitation_events"),
)
def test_prd060_0052_up_guard_rejects_missing_rbac_dependencies(
    tmp_path: Path,
    missing_table: str,
) -> None:
    """异常流程：0051 的租户、RBAC 或邀请表缺失时，0052 在持久变更前统一阻断。"""

    database_url = _sqlite_url(tmp_path / f"prd060-privacy-missing-{missing_table}.db")
    migrations = _migration_subset(tmp_path / f"migrations-missing-{missing_table}", 51)
    apply_migrations(database_url, migrations_dir=migrations)
    connection = connect_database(database_url)
    # RENAME 会保留其他 schema 并同步 SQLite 内部引用，专门隔离“必需表名缺失”门禁。
    connection.execute(
        f"ALTER TABLE {missing_table} RENAME TO {missing_table}_missing_prd060"
    )
    connection.commit()
    _assert_privacy_up_sql_blocked(connection)
    connection.close()


@pytest.mark.parametrize(
    "missing_trigger",
    (
        "trg_invitations_owner_insert_prd060",
        "trg_invitations_state_update_prd060",
        "trg_invitation_events_owner_insert_prd060",
        "trg_invitation_events_state_update_prd060",
    ),
)
def test_prd060_0052_up_guard_rejects_missing_invitation_triggers(
    tmp_path: Path,
    missing_trigger: str,
) -> None:
    """异常流程：任一 Invitation 核心 owner/状态 trigger 缺失都在脱敏写入前阻断。"""

    database_url = _sqlite_url(tmp_path / f"prd060-privacy-missing-{missing_trigger}.db")
    migrations = _migration_subset(tmp_path / f"migrations-missing-{missing_trigger}", 51)
    apply_migrations(database_url, migrations_dir=migrations)
    connection = connect_database(database_url)
    # trigger 名来自固定参数列表，不拼接外部输入；删除后必须由 0052 前置对象门禁发现。
    connection.execute(f"DROP TRIGGER {missing_trigger}")
    connection.commit()
    _assert_privacy_up_sql_blocked(connection)
    connection.close()


def test_prd060_0052_owner_rejects_acceptor_emails_and_preserves_conflict_priority(
    tmp_path: Path,
) -> None:
    """异常/权限流程：接受审计仅收固定标记，重复事实仍先返回不可变冲突。"""

    database_url = _sqlite_url(tmp_path / "prd060-privacy-owner.db")
    through_0051 = _migration_subset(tmp_path / "migrations-0051-owner", 51)
    through_0052 = _migration_subset(tmp_path / "migrations-0052-owner", 52)
    apply_migrations(database_url, migrations_dir=through_0051)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    inviter = _admin(connection, seed.tenant_id)
    invitation_id, accepted_actor, legacy_audit_id = _insert_legacy_accepted_graph(
        connection,
        tenant_id=seed.tenant_id,
        inviter=inviter,
        marker="b",
    )
    connection.commit()
    connection.close()
    assert apply_migrations(database_url, migrations_dir=through_0052).applied == ["0052"]

    connection = connect_database(database_url)
    for suffix, forbidden_email in (
        ("real", accepted_actor["email"]),
        ("arbitrary", "arbitrary-prd060@example.com"),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            _insert_accepted_audit(
                connection,
                tenant_id=seed.tenant_id,
                invitation_id=invitation_id,
                accepted_actor=accepted_actor,
                audit_log_id=f"aud_prd060_privacy_{suffix}",
                actor_email=forbidden_email,
            )
        connection.rollback()

    # 同一 ID 即使同时携带非法邮箱，也必须由后创建的 conflict trigger 先返回不可变错误。
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_immutable"):
        _insert_accepted_audit(
            connection,
            tenant_id=seed.tenant_id,
            invitation_id=invitation_id,
            accepted_actor=accepted_actor,
            audit_log_id=legacy_audit_id,
            actor_email="arbitrary-prd060@example.com",
        )
    connection.rollback()

    # 接受时继续实时校验租户和目标用户角色，不能只相信 Invitation 的历史终态。
    connection.execute(
        "UPDATE tenants SET status='disabled' WHERE tenant_id=?",
        (seed.tenant_id,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
        _insert_accepted_audit(
            connection,
            tenant_id=seed.tenant_id,
            invitation_id=invitation_id,
            accepted_actor=accepted_actor,
            audit_log_id="aud_prd060_privacy_disabled_tenant",
            actor_email=REDACTED_ACCEPTOR_EMAIL,
        )
    connection.rollback()

    connection.execute(
        "UPDATE users SET role='viewer' WHERE tenant_id=? AND user_id=?",
        (seed.tenant_id, accepted_actor["user_id"]),
    )
    drifted_actor = connection.execute(
        "SELECT user_id,email,role FROM users WHERE tenant_id=? AND user_id=?",
        (seed.tenant_id, accepted_actor["user_id"]),
    ).fetchone()
    assert drifted_actor is not None
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
        _insert_accepted_audit(
            connection,
            tenant_id=seed.tenant_id,
            invitation_id=invitation_id,
            accepted_actor=drifted_actor,
            audit_log_id="aud_prd060_privacy_role_drift",
            actor_email=REDACTED_ACCEPTOR_EMAIL,
        )
    connection.rollback()

    _insert_accepted_audit(
        connection,
        tenant_id=seed.tenant_id,
        invitation_id=invitation_id,
        accepted_actor=accepted_actor,
        audit_log_id="aud_prd060_privacy_redacted",
        actor_email=REDACTED_ACCEPTOR_EMAIL,
    )
    connection.commit()
    assert connection.execute(
        "SELECT actor_email FROM audit_logs WHERE audit_log_id='aud_prd060_privacy_redacted'"
    ).fetchone()[0] == REDACTED_ACCEPTOR_EMAIL
    _assert_healthy(connection)
    connection.close()


def test_prd060_0052_empty_down_restores_0051_owner_and_reapplies(tmp_path: Path) -> None:
    """兼容流程：空业务栈可 down/re-up，down 精确恢复 0051 owner 且不伪造邮箱。"""

    database_url = _sqlite_url(tmp_path / "prd060-privacy-empty-roundtrip.db")
    through_0051 = _migration_subset(tmp_path / "migrations-0051-empty", 51)
    through_0052 = _migration_subset(tmp_path / "migrations-0052-empty", 52)
    apply_migrations(database_url, migrations_dir=through_0051)
    connection = connect_database(database_url)
    owner_0051 = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_audit_logs_owner_insert'"
    ).fetchone()[0]
    connection.close()

    assert apply_migrations(database_url, migrations_dir=through_0052).applied == ["0052"]
    assert rollback_last_migration(
        database_url,
        migrations_dir=through_0052,
    ).rolled_back == "0052"
    connection = connect_database(database_url)
    restored_owner = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_audit_logs_owner_insert'"
    ).fetchone()[0]
    assert restored_owner == owner_0051
    assert REDACTED_ACCEPTOR_EMAIL not in restored_owner
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='invitation.accepted'"
    ).fetchone()[0] == 0
    _assert_healthy(connection)
    connection.close()

    assert apply_migrations(database_url, migrations_dir=through_0052).applied == ["0052"]
    connection = connect_database(database_url)
    reapplied_owner = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_audit_logs_owner_insert'"
    ).fetchone()[0]
    assert REDACTED_ACCEPTOR_EMAIL in reapplied_owner
    _assert_healthy(connection)
    connection.close()


@pytest.mark.parametrize(
    "dependency_kind",
    ("business_fact", "later_migration", "unknown_schema_dependency"),
)
def test_prd060_0052_down_guard_blocks_atomically(
    tmp_path: Path,
    dependency_kind: str,
) -> None:
    """回滚安全：事实、后续版本或未知隐私依赖均在首项持久 DDL 前原子阻断。"""

    database_url = _sqlite_url(tmp_path / f"prd060-privacy-down-{dependency_kind}.db")
    migrations = _migration_subset(tmp_path / f"migrations-0052-down-{dependency_kind}", 52)
    apply_migrations(database_url, migrations_dir=migrations)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    if dependency_kind == "business_fact":
        _insert_invitation(
            connection,
            tenant_id=seed.tenant_id,
            actor=_admin(connection, seed.tenant_id),
            marker="c",
        )
    elif dependency_kind == "later_migration":
        connection.execute(
            """
            INSERT INTO schema_migrations(
              version,name,checksum,status,applied_at,execution_time_ms,error_message
            ) VALUES('0053','prd060_privacy_future_dependency',?,'applied',?,0,NULL)
            """,
            ("8" * 64, NOW),
        )
    else:
        connection.execute(
            "CREATE VIEW prd060_privacy_unknown_dependency AS "
            "SELECT audit_log_id FROM audit_logs "
            f"WHERE actor_email='{REDACTED_ACCEPTOR_EMAIL}'"
        )
    connection.commit()
    _assert_privacy_down_sql_blocked(connection)
    connection.close()
