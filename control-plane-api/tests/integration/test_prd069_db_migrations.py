"""PRD-069 0061 意图安全结果、RequestLog 证据与 RBAC revision 6 专项测试。"""

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

# 测试使用正式迁移逐级构建 0060 模板，避免维护一份可能漂移的测试 schema。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
REVISION_5_HASHES = {
    "admin": "64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655",
    "security_admin": "b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160",
}
REVISION_6_HASHES = {
    "admin": "20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760",
    "security_admin": "eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94",
}


def _url(path: Path) -> str:
    """返回当前用例独占 SQLite 文件的迁移 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制到指定栈顶的正式迁移，确保测试与生产 SQL 完全一致。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd069_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """构建真实 seed、0060 数据库模板及只包含到 0061 的迁移目录。"""

    root = tmp_path_factory.mktemp("prd069-migration-templates")
    migrations_0053 = _migration_subset(root / "migrations-0053", 53)
    migrations_0060 = _migration_subset(root / "migrations-0060", 60)
    migrations_0061 = _migration_subset(root / "migrations-0061", 61)
    database_0060 = root / "template-0060.db"
    database_url = _url(database_0060)
    assert apply_migrations(
        database_url, migrations_dir=migrations_0053
    ).applied[-1] == "0053"
    apply_seed_data(database_url)
    assert apply_migrations(
        database_url, migrations_dir=migrations_0060
    ).applied[-1] == "0060"
    return {
        "database_0060": database_0060,
        "migrations_0061": migrations_0061,
    }


def _clone(template: Path, destination: Path) -> str:
    """复制已关闭的 0060 模板并返回独占 URL。"""

    shutil.copy2(template, destination)
    return _url(destination)


def _apply_0061(database_url: str, migrations: Path) -> None:
    """应用且只应用 0061，使 SQL 失败直接定位到本条迁移。"""

    result = apply_migrations(database_url, migrations_dir=migrations)
    assert result.applied == ["0061"]


def _identity(connection: sqlite3.Connection) -> dict[str, str]:
    """读取并认领 Gateway 测试身份。

    参数：当前用例数据库连接。返回：同租户用户、Virtual Key 和服务端链路 ID。
    约束：claim 必须先于 result 写入，模拟真实 Gateway owner 建立顺序。
    """

    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,key.virtual_key_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN virtual_keys key ON key.tenant_id=tenant.tenant_id
        WHERE tenant.slug='default'
        ORDER BY key.virtual_key_id LIMIT 1
        """
    ).fetchone()
    assert row is not None and row[2] is not None
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,'req_prd069_gateway','trc_prd069_gateway',?,NULL,'disabled',?)
        """,
        (str(row[0]), str(row[2]), "2026-08-03T11:59:00Z"),
    )
    return {
        "tenant_id": str(row[0]),
        "user_id": str(row[1]),
        "virtual_key_id": str(row[2]),
        "request_id": "req_prd069_gateway",
        "trace_id": "trc_prd069_gateway",
    }


def _matrix_hash(codes: list[str]) -> str:
    """按 Shared canonical JSON 合同计算租户自定义角色矩阵 hash。"""

    payload = json.dumps(
        sorted(codes), ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _rows(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[object, ...], ...]:
    """按 rowid 冻结整表，验证回滚不会改写历史实体或 NULL 分布。"""

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
    """冻结表和本地索引/触发器 SQL，确保 down 精确恢复 0060 结构。"""

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


def _insert_gateway_result(
    connection: sqlite3.Connection,
    identity: dict[str, str],
    *,
    result_id: str = "irr_prd069_blocked",
) -> None:
    """插入不含正文的合法 Gateway blocked 结果，供 owner/event/RequestLog 测试复用。"""

    connection.execute(
        """
        INSERT INTO intent_risk_results(
          intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
          actor_type,actor_id,status,classification_mode,risk_type,risk_score,
          risk_score_scale,action,reason_code,detector_version,ruleset_version,
          input_code_points,input_truncated,duration_ms,error_code,created_at,completed_at
        ) VALUES(?,?,'gateway',?,?,'virtual_key',?,'blocked','sync_strong',
                 'system_prompt_extraction',9800,10000,'block',
                 'explicit_system_prompt_request','intent-rule-engine-v1',
                 'intent-rules-2026-08-03-v1',18,0,1,NULL,?,?)
        """,
        (
            result_id,
            identity["tenant_id"],
            identity["request_id"],
            identity["trace_id"],
            identity["virtual_key_id"],
            "2026-08-03T12:00:00Z",
            "2026-08-03T12:00:00Z",
        ),
    )


def test_prd069_upgrade_schema_rbac_and_future_tenant(
    prd069_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """主流程：升级形成 57/151 目录、61 列日志和 revision 1 新租户矩阵。"""

    database_url = _clone(
        prd069_templates["database_0060"], tmp_path / "upgrade.db"
    )
    _apply_0061(database_url, prd069_templates["migrations_0061"])
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_permission_templates"
        ).fetchone()[0] == 57
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_role_permission_templates"
        ).fetchone()[0] == 151
        tenant_count = connection.execute(
            "SELECT COUNT(*) FROM tenants"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions"
        ).fetchone()[0] == tenant_count * 57
        assert connection.execute(
            "SELECT COUNT(*) FROM pragma_table_info('request_logs')"
        ).fetchone()[0] == 61
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name='request_logs'"
        ).fetchone()[0] == 14
        for role_code, expected_hash in REVISION_6_HASHES.items():
            role = connection.execute(
                "SELECT matrix_sha256 FROM roles WHERE code=?", (role_code,)
            ).fetchone()
            assert role is not None and role[0] == expected_hash
        grants = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT role.code FROM role_permissions relation
                JOIN roles role ON role.tenant_id=relation.tenant_id
                               AND role.role_id=relation.role_id
                JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                           AND permission.permission_id=relation.permission_id
                WHERE permission.code='intent_risk.classify'
                """
            ).fetchall()
        }
        assert grants == {"admin", "security_admin"}

        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd069_future','prd069-future','PRD-069 Future',
                   'internal_only','active',?,?)
            """,
            ("2026-08-03T13:00:00Z", "2026-08-03T13:00:00Z"),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE tenant_id='tenant_prd069_future'"
        ).fetchone()[0] == 57
        assert {
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT permission_revision FROM roles "
                "WHERE tenant_id='tenant_prd069_future'"
            ).fetchall()
        } == {1}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_prd069_result_owner_state_event_and_immutability(
    prd069_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """异常与租户流程：claim owner、状态矩阵、event 镜像和终态不可变均由 DB 拒绝。"""

    database_url = _clone(
        prd069_templates["database_0060"], tmp_path / "result-contract.db"
    )
    _apply_0061(database_url, prd069_templates["migrations_0061"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        _insert_gateway_result(connection, identity)
        connection.execute(
            """
            INSERT INTO intent_risk_events(
              intent_risk_event_id,tenant_id,intent_risk_result_id,event_type,
              source_request_id,source_trace_id,source_kind,classification_mode,
              risk_type,risk_score,risk_score_scale,action,reason_code,
              detector_version,ruleset_version,input_truncated,actor_type,actor_id,created_at
            ) VALUES('irevt_prd069',?,?,'security.intent_detected',?,?,'gateway',
                     'sync_strong','system_prompt_extraction',9800,10000,'block',
                     'explicit_system_prompt_request','intent-rule-engine-v1',
                     'intent-rules-2026-08-03-v1',0,'virtual_key',?,?)
            """,
            (
                identity["tenant_id"],
                "irr_prd069_blocked",
                identity["request_id"],
                identity["trace_id"],
                identity["virtual_key_id"],
                "2026-08-03T12:00:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="intent_risk_result_immutable"):
            connection.execute(
                "UPDATE intent_risk_results SET risk_score=9900 "
                "WHERE intent_risk_result_id='irr_prd069_blocked'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="intent_risk_event_immutable"):
            connection.execute(
                "DELETE FROM intent_risk_events WHERE intent_risk_event_id='irevt_prd069'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO intent_risk_results(
                  intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
                  actor_type,actor_id,status,classification_mode,risk_type,risk_score,
                  risk_score_scale,action,reason_code,detector_version,ruleset_version,
                  input_code_points,input_truncated,duration_ms,error_code,created_at,completed_at
                ) VALUES('irr_invalid_failed',?,'gateway','unknown','unknown','virtual_key',?,
                         'failed','sync_strong','none',0,10000,'allow','classifier_failed',
                         'intent-rule-engine-v1','intent-rules-2026-08-03-v1',0,0,1,
                         'intent_risk.classifier_failed',?,?)
                """,
                (
                    identity["tenant_id"],
                    identity["virtual_key_id"],
                    "2026-08-03T12:01:00Z",
                    "2026-08-03T12:01:00Z",
                ),
            )
        # Control 分类器异常且数据库仍可写时必须允许 manual failed/none，供 503 诊断追踪。
        connection.execute(
            """
            INSERT INTO intent_risk_results(
              intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
              actor_type,actor_id,status,classification_mode,risk_type,risk_score,
              risk_score_scale,action,reason_code,detector_version,ruleset_version,
              input_code_points,input_truncated,duration_ms,error_code,created_at,completed_at
            ) VALUES('irr_manual_failed',?,'manual','req_manual_failed','trc_manual_failed',
                     'user',?,'failed','manual_sync','none',0,10000,'none','classifier_failed',
                     'intent-rule-engine-v1','intent-rules-2026-08-03-v1',8,0,1,
                     'intent_risk.classifier_failed',?,?)
            """,
            (
                identity["tenant_id"],
                identity["user_id"],
                "2026-08-03T12:01:30Z",
                "2026-08-03T12:01:30Z",
            ),
        )
        # 合法 owner 也不能伪造 taxonomy 与原因码的组合，否则事件消费者会得到双义证据。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO intent_risk_results(
                  intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
                  actor_type,actor_id,status,classification_mode,risk_type,risk_score,
                  risk_score_scale,action,reason_code,detector_version,ruleset_version,
                  input_code_points,input_truncated,duration_ms,error_code,created_at,completed_at
                ) VALUES('irr_invalid_reason',?,'manual','req_invalid_reason','trc_invalid_reason',
                         'user',?,'detected','manual_sync','jailbreak',9600,10000,'review',
                         'explicit_policy_bypass','intent-rule-engine-v1',
                         'intent-rules-2026-08-03-v1',10,0,1,NULL,?,?)
                """,
                (
                    identity["tenant_id"],
                    identity["user_id"],
                    "2026-08-03T12:02:00Z",
                    "2026-08-03T12:02:01Z",
                ),
            )


def test_prd069_request_log_distinguishes_result_store_failure(
    prd069_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """异常流程：同步结果存储失败必须以独立原因和 none 动作冻结，且不得伪造 result。"""

    database_url = _clone(
        prd069_templates["database_0060"], tmp_path / "request-log-failed-closed.db"
    )
    _apply_0061(database_url, prd069_templates["migrations_0061"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        # result/event 未提交时只能保存失败关闭摘要；固定 reason 使排障能区分分类器与存储阶段。
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id,tenant_id,trace_id,virtual_key_id,status,error_code,
              snapshot_version,policy_snapshot_status,
              intent_risk_result_id,intent_risk_schedule_status,intent_risk_mode,
              intent_risk_type,intent_risk_score,intent_risk_action,intent_risk_reason_code,
              intent_risk_detector_version,intent_risk_input_truncated,created_at,updated_at
            ) VALUES(?,?,?,?,'failed','gateway.intent_result_store_unavailable',
                     NULL,'disabled',NULL,'failed_closed','sync_strong',NULL,NULL,'none',
                     'result_store_unavailable','intent-rule-engine-v1',0,?,?)
            """,
            (
                identity["request_id"],
                identity["tenant_id"],
                identity["trace_id"],
                identity["virtual_key_id"],
                "2026-08-03T12:00:00Z",
                "2026-08-03T12:00:00Z",
            ),
        )
        assert tuple(
            connection.execute(
                """
                SELECT intent_risk_schedule_status,intent_risk_action,
                       intent_risk_reason_code,intent_risk_result_id
                FROM request_logs WHERE tenant_id=? AND request_id=?
                """,
                (identity["tenant_id"], identity["request_id"]),
            ).fetchone()
        ) == ("failed_closed", "none", "result_store_unavailable", None)
        # 存储失败时不存在可信终态；即使提供格式合法的 ID，也不能让日志冒充已提交结果。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE request_logs SET intent_risk_result_id='irr_uncommitted_result'
                WHERE tenant_id=? AND request_id=?
                """,
                (identity["tenant_id"], identity["request_id"]),
            )
        # failed_closed 的动作是显式 none，NULL 会丢失“已执行失败关闭”的状态语义。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE request_logs SET intent_risk_action=NULL
                WHERE tenant_id=? AND request_id=?
                """,
                (identity["tenant_id"], identity["request_id"]),
            )
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id,request_id,trace_id,virtual_key_id,
              snapshot_version,policy_snapshot_status,claimed_at
            ) VALUES(?,'req_prd069_invalid_null','trc_prd069_invalid_null',?,NULL,'disabled',?)
            """,
            (
                identity["tenant_id"],
                identity["virtual_key_id"],
                "2026-08-03T12:01:00Z",
            ),
        )
        # INSERT trigger 同样必须把 UNKNOWN 当非法状态，不能让必填动作的 NULL 绕过矩阵。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO request_logs(
                  request_id,tenant_id,trace_id,virtual_key_id,status,error_code,
                  snapshot_version,policy_snapshot_status,intent_risk_schedule_status,
                  intent_risk_mode,intent_risk_action,intent_risk_reason_code,
                  intent_risk_detector_version,created_at
                ) VALUES('req_prd069_invalid_null',?,'trc_prd069_invalid_null',?,
                         'failed','gateway.intent_result_store_unavailable',NULL,'disabled',
                         'failed_closed','sync_strong',NULL,'result_store_unavailable',
                         'intent-rule-engine-v1',?)
                """,
                (
                    identity["tenant_id"],
                    identity["virtual_key_id"],
                    "2026-08-03T12:01:00Z",
                ),
            )


def test_prd069_custom_security_matrix_is_not_expanded(
    prd069_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """权限流程：迁移不得给已定制 SecurityAdmin 静默恢复或新增敏感分类权限。"""

    database_url = _clone(
        prd069_templates["database_0060"], tmp_path / "custom-security.db"
    )
    with connect_database(database_url) as connection:
        tenant_id, role_id, revision = connection.execute(
            "SELECT tenant_id,role_id,permission_revision FROM roles "
            "WHERE code='security_admin'"
        ).fetchone()
        relation_id = connection.execute(
            """
            SELECT relation.role_permission_id FROM role_permissions relation
            JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                       AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=? AND permission.code='dlp.write'
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
            (int(revision) + 1, custom_hash, "2026-08-03T14:00:00Z", role_id),
        )
    _apply_0061(database_url, prd069_templates["migrations_0061"])
    with connect_database(database_url) as connection:
        assert tuple(
            connection.execute(
                "SELECT permission_revision,matrix_sha256 FROM roles WHERE role_id=?",
                (role_id,),
            ).fetchone()
        ) == (int(revision) + 1, custom_hash)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM role_permissions relation
            JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                       AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
              AND permission.code='intent_risk.classify'
            """,
            (tenant_id, role_id),
        ).fetchone()[0] == 0


def test_prd069_down_business_fact_fails_before_ddl(
    prd069_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """回滚异常流程：任何 intent 结果存在时 down 原子拒绝且 0061 schema 保持原样。"""

    database_url = _clone(
        prd069_templates["database_0060"], tmp_path / "down-guard.db"
    )
    _apply_0061(database_url, prd069_templates["migrations_0061"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        _insert_gateway_result(connection, identity)
        connection.commit()
    with pytest.raises(DbError, match="prd069_down_business_facts_present"):
        rollback_last_migration(
            database_url, migrations_dir=prd069_templates["migrations_0061"]
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM intent_risk_results"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0061'"
        ).fetchone()[0] == "applied"


def test_prd069_round_trip_restores_0060_exactly(
    prd069_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """兼容流程：无业务事实时 0060→0061→0060 保留行、rowid 和 RequestLog schema。"""

    database_url = _clone(
        prd069_templates["database_0060"], tmp_path / "round-trip.db"
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
                "request_logs",
            )
        }
        before_request_schema = _schema_objects(connection, "request_logs")

    _apply_0061(database_url, prd069_templates["migrations_0061"])
    assert rollback_last_migration(
        database_url, migrations_dir=prd069_templates["migrations_0061"]
    ).rolled_back == "0061"
    with connect_database(database_url) as connection:
        for table, expected in before_rows.items():
            assert _rows(connection, table) == expected
        assert _schema_objects(connection, "request_logs") == before_request_schema
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%prd069'"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_prd069_unknown_dependency_blocks_down_atomically(
    prd069_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """兼容流程：未知后续 view 引用新表时 down 在首项持久 DDL 前原子拒绝。"""

    database_url = _clone(
        prd069_templates["database_0060"], tmp_path / "unknown-dependency.db"
    )
    _apply_0061(database_url, prd069_templates["migrations_0061"])
    with connect_database(database_url) as connection:
        connection.execute(
            "CREATE VIEW future_intent_projection AS "
            "SELECT tenant_id,intent_risk_result_id FROM intent_risk_results"
        )
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, migrations_dir=prd069_templates["migrations_0061"]
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='view' "
            "AND name='future_intent_projection'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0061'"
        ).fetchone()[0] == "applied"
