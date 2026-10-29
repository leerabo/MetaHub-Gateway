"""PRD-070 0062 会话风险、RBAC revision 7 与可逆迁移专项测试。"""

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

# 所有用例都从真实迁移栈构建 0061 模板，避免测试 schema 与生产 SQL 各自演进。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
REVISION_6_HASHES = {
    "admin": "20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760",
    "security_admin": "eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94",
}
REVISION_7_HASHES = {
    "admin": "792e003e6c0a188c124ef71ce7ab31658e7ef43a6dadaf6b83c339dfff2caf7f",
    "security_admin": "de4701bc3ce4305c129704c69212d8a520ea5942a87c6119a36542e01784b78c",
}
SESSION_PERMISSION_CODES = ("session_risk.read", "session_risk.manage")


def _url(path: Path) -> str:
    """返回当前用例独占 SQLite 文件的数据库 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不高于指定版本的正式迁移，确保测试执行的就是交付 SQL。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd070_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """构建含真实 seed 的 0061 模板及只到 0062 的正式迁移目录。"""

    root = tmp_path_factory.mktemp("prd070-migration-templates")
    migrations_0053 = _migration_subset(root / "migrations-0053", 53)
    migrations_0061 = _migration_subset(root / "migrations-0061", 61)
    migrations_0062 = _migration_subset(root / "migrations-0062", 62)
    database_0061 = root / "template-0061.db"
    database_url = _url(database_0061)
    assert apply_migrations(
        database_url, migrations_dir=migrations_0053
    ).applied[-1] == "0053"
    apply_seed_data(database_url)
    assert apply_migrations(
        database_url, migrations_dir=migrations_0061
    ).applied[-1] == "0061"
    return {
        "database_0061": database_0061,
        "migrations_0062": migrations_0062,
    }


def _clone(template: Path, destination: Path) -> str:
    """复制已关闭的 0061 模板并返回当前用例的独占数据库 URL。"""

    shutil.copy2(template, destination)
    return _url(destination)


def _apply_0062(database_url: str, migrations: Path) -> None:
    """只应用 0062，使失败能够直接定位到本 PRD。"""

    result = apply_migrations(database_url, migrations_dir=migrations)
    assert result.applied == ["0062"]


def _matrix_hash(codes: list[str]) -> str:
    """按 Shared canonical JSON 合同计算租户自定义角色矩阵摘要。"""

    payload = json.dumps(
        sorted(codes), ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _rows_by_name(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[tuple[str, object], ...], ...]:
    """按字段名冻结整表，避免 ALTER COLUMN 的物理列序影响语义保真比较。"""

    columns = [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()
    ]
    rows = connection.execute(
        f'SELECT rowid,* FROM "{table}" ORDER BY rowid'
    ).fetchall()
    names = ["rowid", *columns]
    return tuple(
        tuple(sorted(zip(names, tuple(row), strict=True), key=lambda item: item[0]))
        for row in rows
    )


def _schema_objects(
    connection: sqlite3.Connection,
    table: str,
    *,
    include_table: bool = True,
) -> tuple[tuple[str, str, str], ...]:
    """冻结表及其显式索引/trigger SQL，用于核对旧治理约束没有漂移。"""

    return tuple(
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """
            SELECT type,name,sql FROM sqlite_schema
            WHERE ((? AND type='table' AND name=?)
                OR (type IN ('index','trigger') AND tbl_name=?))
              AND sql IS NOT NULL
            ORDER BY type,name
            """,
            (1 if include_table else 0, table, table),
        ).fetchall()
    )


def _identity(connection: sqlite3.Connection) -> dict[str, str]:
    """读取默认租户管理员、Virtual Key 与 Provider，供合法旧事实复用。"""

    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,user.email,user.role,
               key.virtual_key_id,provider.provider_id,provider.name
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN virtual_keys key ON key.tenant_id=tenant.tenant_id
        JOIN providers provider ON provider.tenant_id=tenant.tenant_id
        WHERE tenant.slug='default'
        ORDER BY key.virtual_key_id,provider.provider_id LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return {
        "tenant_id": str(row[0]),
        "user_id": str(row[1]),
        "email": str(row[2]),
        "role": str(row[3]),
        "virtual_key_id": str(row[4]),
        "provider_id": str(row[5]),
        "provider_name": str(row[6]),
    }


def _insert_0061_facts(connection: sqlite3.Connection) -> None:
    """插入 0061 可表达的 Intent、RequestLog 与 Audit 历史，验证升级/回滚保真。"""

    identity = _identity(connection)
    request_id = "req_prd070_roundtrip"
    trace_id = "trc_prd070_roundtrip"
    created_at = "2026-08-04T00:00:00Z"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled',?)
        """,
        (
            identity["tenant_id"],
            request_id,
            trace_id,
            identity["virtual_key_id"],
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO intent_risk_results(
          intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
          actor_type,actor_id,status,classification_mode,risk_type,risk_score,
          risk_score_scale,action,reason_code,detector_version,ruleset_version,
          input_code_points,input_truncated,duration_ms,error_code,created_at,completed_at
        ) VALUES('irr_prd070_roundtrip',?,'gateway',?,?,'virtual_key',?,'blocked',
                 'sync_strong','jailbreak',9800,10000,'block',
                 'explicit_instruction_override','intent-rule-engine-v1',
                 'intent-rules-v1',16,0,1,NULL,?,?)
        """,
        (
            identity["tenant_id"],
            request_id,
            trace_id,
            identity["virtual_key_id"],
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO intent_risk_events(
          intent_risk_event_id,tenant_id,intent_risk_result_id,event_type,
          source_request_id,source_trace_id,source_kind,classification_mode,
          risk_type,risk_score,risk_score_scale,action,reason_code,
          detector_version,ruleset_version,input_truncated,actor_type,actor_id,created_at
        ) VALUES('irevt_prd070_roundtrip',?,'irr_prd070_roundtrip',
                 'security.intent_detected',?,?,'gateway','sync_strong','jailbreak',
                 9800,10000,'block','explicit_instruction_override',
                 'intent-rule-engine-v1','intent-rules-v1',0,'virtual_key',?,?)
        """,
        (
            identity["tenant_id"],
            request_id,
            trace_id,
            identity["virtual_key_id"],
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,virtual_key_id,status,error_code,created_at,
          snapshot_version,policy_snapshot_status,intent_risk_result_id,
          intent_risk_schedule_status,intent_risk_mode,intent_risk_type,
          intent_risk_score,intent_risk_action,intent_risk_reason_code,
          intent_risk_detector_version,intent_risk_input_truncated
        ) VALUES(?,?,?,?,'failed','gateway.intent_blocked',?,NULL,'disabled',
                 'irr_prd070_roundtrip','blocked','sync_strong','jailbreak',9800,
                 'block','explicit_instruction_override','intent-rule-engine-v1',0)
        """,
        (
            request_id,
            identity["tenant_id"],
            trace_id,
            identity["virtual_key_id"],
            created_at,
        ),
    )
    # Provider 旧动作仅要求同租户有效 actor；用空安全摘要测试 Audit 换表的 NULL/rowid 保真。
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,
          object_type,object_id,object_name,before_summary_json,after_summary_json,
          changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES('aud_prd070_roundtrip',?,?,?,?,?,'provider',?,?,
                 '{}','{}','[]',NULL,NULL,'req_aud_prd070_roundtrip',
                 'trc_aud_prd070_roundtrip',?)
        """,
        (
            identity["tenant_id"],
            identity["user_id"],
            identity["email"],
            identity["role"],
            "provider.updated",
            identity["provider_id"],
            identity["provider_name"],
            created_at,
        ),
    )
    connection.commit()


def _customize_security_admin(
    connection: sqlite3.Connection,
    *,
    removed_code: str = "dlp.write",
) -> tuple[str, str, int, str]:
    """合法移除一项关系并推进 CAS/hash，构造真实租户自定义安全角色。"""

    tenant_id, role_id, revision = connection.execute(
        "SELECT tenant_id,role_id,permission_revision FROM roles "
        "WHERE code='security_admin' ORDER BY tenant_id LIMIT 1"
    ).fetchone()
    relation_id = connection.execute(
        """
        SELECT relation.role_permission_id FROM role_permissions relation
        JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                   AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=? AND relation.role_id=? AND permission.code=?
        """,
        (tenant_id, role_id, removed_code),
    ).fetchone()[0]
    connection.execute(
        "DELETE FROM role_permissions WHERE role_permission_id=?", (relation_id,)
    )
    codes = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT permission.code FROM role_permissions relation
            JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                       AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=? ORDER BY permission.code
            """,
            (tenant_id, role_id),
        ).fetchall()
    ]
    custom_hash = _matrix_hash(codes)
    connection.execute(
        "UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=? "
        "WHERE role_id=?",
        (int(revision) + 1, custom_hash, "2026-08-04T00:05:00Z", role_id),
    )
    connection.commit()
    # 返回更新后的 revision/hash，调用方据此证明迁移没有覆盖租户自定义矩阵。
    return str(tenant_id), str(role_id), int(revision) + 1, custom_hash


def _insert_session_score(connection: sqlite3.Connection) -> str:
    """插入通过 0062 owner trigger 的最小 API 高危 score，供 down 门禁测试。"""

    identity = _identity(connection)
    request_id = "req_prd070_down_guard"
    trace_id = "trc_prd070_down_guard"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled','2026-08-04T00:10:00Z')
        """,
        (
            identity["tenant_id"],
            request_id,
            trace_id,
            identity["virtual_key_id"],
        ),
    )
    score_id = "srs_" + "7" * 32
    connection.execute(
        """
        INSERT INTO session_risk_scores(
          session_risk_score_id,tenant_id,scope_type,scope_key,hmac_key_version,
          virtual_key_id,chat_session_id,status,score,score_scale,risky_turn_count,
          jailbreak_count,sensitive_count,tool_inducement_count,state_revision,
          threshold_version,detector_version,ruleset_version,last_source_request_id,
          last_source_trace_id,window_started_at,last_risk_at,window_expires_at,
          first_high_risk_at,frozen_at,resolved_at,created_at,updated_at
        ) VALUES(?,?,'api_session',?,1,?,NULL,'high_risk',7500,10000,2,1,1,0,1,
                 'session-risk-threshold-v1','intent-risk-rules-v1',
                 'session-risk-ruleset-v1',?,?,
                 '2026-08-04T00:08:00Z','2026-08-04T00:10:00Z',
                 '2026-08-04T00:38:00Z','2026-08-04T00:10:00Z',NULL,NULL,
                 '2026-08-04T00:08:00Z','2026-08-04T00:10:00Z')
        """,
        (
            score_id,
            identity["tenant_id"],
            "srh_" + "8" * 64,
            identity["virtual_key_id"],
            request_id,
            trace_id,
        ),
    )
    connection.commit()
    return score_id


def test_prd070_round_trip_preserves_rows_schema_and_reapplies(
    prd070_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """主流程：0061→0062→0061→0062 保留旧事实 rowid 与全部旧治理对象。"""

    database_url = _clone(
        prd070_templates["database_0061"], tmp_path / "round-trip.db"
    )
    with connect_database(database_url) as connection:
        _insert_0061_facts(connection)
        tracked_tables = (
            "rbac_permission_templates",
            "rbac_role_permission_templates",
            "permissions",
            "roles",
            "role_permissions",
            "request_logs",
            "intent_risk_results",
            "intent_risk_events",
            "audit_logs",
        )
        before_rows = {
            table: _rows_by_name(connection, table) for table in tracked_tables
        }
        exact_schema_tables = (
            "rbac_permission_templates",
            "permissions",
            "intent_risk_results",
            "intent_risk_events",
            "audit_logs",
        )
        before_schema = {
            table: _schema_objects(connection, table)
            for table in exact_schema_tables
        }
        request_objects = _schema_objects(
            connection, "request_logs", include_table=False
        )

    _apply_0062(database_url, prd070_templates["migrations_0062"])
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_permission_templates"
        ).fetchone()[0] == 59
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_role_permission_templates"
        ).fetchone()[0] == 155
        assert connection.execute(
            "SELECT COUNT(*) FROM pragma_table_info('request_logs')"
        ).fetchone()[0] == 66
        for role_code, expected_hash in REVISION_7_HASHES.items():
            assert connection.execute(
                "SELECT matrix_sha256 FROM roles WHERE code=? ORDER BY tenant_id LIMIT 1",
                (role_code,),
            ).fetchone()[0] == expected_hash

    result = rollback_last_migration(
        database_url, migrations_dir=prd070_templates["migrations_0062"]
    )
    assert result.rolled_back == "0062"
    with connect_database(database_url) as connection:
        for table, expected in before_rows.items():
            assert _rows_by_name(connection, table) == expected
        for table, expected in before_schema.items():
            assert _schema_objects(connection, table) == expected
        assert _schema_objects(
            connection, "request_logs", include_table=False
        ) == request_objects
        assert connection.execute(
            "SELECT COUNT(*) FROM pragma_table_info('request_logs')"
        ).fetchone()[0] == 61
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%prd070%'"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for role_code, expected_hash in REVISION_6_HASHES.items():
            assert connection.execute(
                "SELECT matrix_sha256 FROM roles WHERE code=? ORDER BY tenant_id LIMIT 1",
                (role_code,),
            ).fetchone()[0] == expected_hash

    # 再次升级验证 down 恢复的表定义可被同一正式 up 接受，不只满足静态计数。
    _apply_0062(database_url, prd070_templates["migrations_0062"])
    with connect_database(database_url) as connection:
        for table in ("intent_risk_results", "intent_risk_events", "audit_logs"):
            old_rowids = {
                dict(row)["rowid"] for row in before_rows[table]
            }
            new_rowids = {
                dict(row)["rowid"] for row in _rows_by_name(connection, table)
            }
            assert old_rowids <= new_rowids
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_prd070_custom_security_admin_is_not_expanded(
    prd070_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """权限流程：0061 已定制 SecurityAdmin 不得被迁移静默授予两项新权限。"""

    database_url = _clone(
        prd070_templates["database_0061"], tmp_path / "custom-before-up.db"
    )
    with connect_database(database_url) as connection:
        tenant_id, role_id, revision, custom_hash = _customize_security_admin(
            connection
        )
    _apply_0062(database_url, prd070_templates["migrations_0062"])
    with connect_database(database_url) as connection:
        assert tuple(
            connection.execute(
                "SELECT permission_revision,matrix_sha256 FROM roles WHERE role_id=?",
                (role_id,),
            ).fetchone()
        ) == (revision, custom_hash)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM role_permissions relation
            JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                       AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
              AND permission.code IN ('session_risk.read','session_risk.manage')
            """,
            (tenant_id, role_id),
        ).fetchone()[0] == 0


def test_prd070_down_rejects_score_atomically(
    prd070_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """异常流程：任一 score 事实都在首项持久 DDL 前阻断回滚并完整保留 0062。"""

    database_url = _clone(
        prd070_templates["database_0061"], tmp_path / "score-guard.db"
    )
    _apply_0062(database_url, prd070_templates["migrations_0062"])
    with connect_database(database_url) as connection:
        score_id = _insert_session_score(connection)
    with pytest.raises(DbError, match="prd070_down_business_facts_present"):
        rollback_last_migration(
            database_url, migrations_dir=prd070_templates["migrations_0062"]
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0062'"
        ).fetchone()[0] == "applied"
        assert connection.execute(
            "SELECT COUNT(*) FROM session_risk_scores "
            "WHERE session_risk_score_id=?",
            (score_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM pragma_table_info('request_logs')"
        ).fetchone()[0] == 66


def test_prd070_down_rejects_custom_holder_atomically(
    prd070_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """权限异常：仍持新权限的租户自定义 SecurityAdmin 禁止被 down 静默删权。"""

    database_url = _clone(
        prd070_templates["database_0061"], tmp_path / "custom-holder-guard.db"
    )
    _apply_0062(database_url, prd070_templates["migrations_0062"])
    with connect_database(database_url) as connection:
        tenant_id, role_id, revision, custom_hash = _customize_security_admin(
            connection
        )
        assert connection.execute(
            """
            SELECT COUNT(*) FROM role_permissions relation
            JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                       AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
              AND permission.code IN ('session_risk.read','session_risk.manage')
            """,
            (tenant_id, role_id),
        ).fetchone()[0] == 2
    with pytest.raises(DbError, match="prd070_down_business_facts_present"):
        rollback_last_migration(
            database_url, migrations_dir=prd070_templates["migrations_0062"]
        )
    with connect_database(database_url) as connection:
        assert tuple(
            connection.execute(
                "SELECT permission_revision,matrix_sha256 FROM roles WHERE role_id=?",
                (role_id,),
            ).fetchone()
        ) == (revision, custom_hash)
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_permission_templates"
        ).fetchone()[0] == 59


def test_prd070_unknown_dependency_blocks_down_before_ddl(
    prd070_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """兼容流程：未知后续 view 引用会话表时，down 原子拒绝且 view/0062 均保留。"""

    database_url = _clone(
        prd070_templates["database_0061"], tmp_path / "future-view-guard.db"
    )
    _apply_0062(database_url, prd070_templates["migrations_0062"])
    with connect_database(database_url) as connection:
        connection.execute(
            "CREATE VIEW future_session_risk_projection AS "
            "SELECT tenant_id,session_risk_score_id FROM session_risk_scores"
        )
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, migrations_dir=prd070_templates["migrations_0062"]
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='view' "
            "AND name='future_session_risk_projection'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0062'"
        ).fetchone()[0] == "applied"


def test_prd070_audit_owner_rejects_missing_source_action(
    prd070_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """安全负例：没有同租户 action/score/event 的 SessionRisk Audit 必须在数据库层拒绝。"""

    database_url = _clone(
        prd070_templates["database_0061"], tmp_path / "audit-owner.db"
    )
    _apply_0062(database_url, prd070_templates["migrations_0062"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            connection.execute(
                """
                INSERT INTO audit_logs(
                  audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,
                  object_type,object_id,object_name,before_summary_json,
                  after_summary_json,changed_fields_json,client_ip,user_agent,
                  request_id,trace_id,created_at
                ) VALUES('aud_prd070_missing_source',?,?,?,?,
                         'session_risk.freeze_session','session_risk',
                         'srs_ffffffffffffffffffffffffffffffff','api_session',
                         '{}','{}','[]',NULL,NULL,
                         'req_prd070_missing_source','trc_prd070_missing_source',
                         '2026-08-04T00:20:00Z')
                """,
                (
                    identity["tenant_id"],
                    identity["user_id"],
                    identity["email"],
                    identity["role"],
                ),
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_logs "
            "WHERE audit_log_id='aud_prd070_missing_source'"
        ).fetchone()[0] == 0
