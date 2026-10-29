"""PRD-076 Response Cache 0069 正反迁移与回滚守卫专项测试。"""

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

# 测试只复制正式迁移构造隔离数据库，避免手写 schema 与生产 SQL 各自演进。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
EXPECTED_VERSIONS = [f"{version:04d}" for version in range(1, 70)]
STARTED_AT = "2026-08-10T00:00:00Z"
COMPLETED_AT = "2026-08-10T00:00:01Z"
ZERO_FINGERPRINT = "0" * 64

RESPONSE_CACHE_TABLES = {
    "response_cache_key_state",
    "response_cache_policies",
    "response_cache_policy_events",
    "response_cache_tag_generations",
    "response_cache_key_generations",
    "response_cache_entries",
    "response_cache_entry_tags",
    "response_cache_fill_leases",
    "response_cache_fill_tags",
    "response_cache_request_facts",
    "response_cache_hits",
    "response_cache_invalidations",
    "response_cache_events",
    "response_cache_event_outbox",
}
LEGACY_REBUILT_TABLES = (
    "usage_ledger",
    "finops_ledger_entries",
    "output_safety_scans",
    "output_safety_events",
    "audit_logs",
)
CACHE_PERMISSIONS = {
    "response_cache.read",
    "response_cache.write",
    "response_cache.invalidate",
}


def _url(path: Path) -> str:
    """返回用例独占 SQLite URL，禁止测试回落到共享开发数据库。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制指定版本以内的正式 up/down SQL，保证回滚也使用交付文件。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd076_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """构造真实 0068 seed 模板与 0069 迁移目录，供每个用例隔离复制。"""

    root = tmp_path_factory.mktemp("prd076-migration-templates")
    migrations_0068 = _migration_subset(root / "migrations-0068", 68)
    migrations_0069 = _migration_subset(root / "migrations-0069", 69)
    database_0068 = root / "template-0068.sqlite3"
    database_url = _url(database_0068)
    result = apply_migrations(database_url, migrations_dir=migrations_0068)
    assert result.applied[-1] == "0068"
    apply_seed_data(database_url)
    return {
        "database_0068": database_0068,
        "migrations_0069": migrations_0069,
    }


def _clone_and_upgrade(
    templates: dict[str, Path],
    destination: Path,
) -> tuple[str, sqlite3.Connection]:
    """复制关闭状态的 0068 模板，只应用 0069 并返回新连接。"""

    shutil.copy2(templates["database_0068"], destination)
    database_url = _url(destination)
    result = apply_migrations(
        database_url,
        migrations_dir=templates["migrations_0069"],
    )
    assert result.applied == ["0069"]
    return database_url, connect_database(database_url)


def _applied_head(connection: sqlite3.Connection) -> str:
    """读取当前最高 applied 版本，失败回滚不能错误推进迁移状态。"""

    row = connection.execute(
        """
        SELECT version FROM schema_migrations
        WHERE status='applied' ORDER BY version DESC LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return str(row[0])


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """同时固定 SQLite 完整性与外键零违规门禁。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _schema_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    """冻结全部持久 schema SQL，拒绝仅凭对象数量判断正反迁移等价。"""

    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type,name,tbl_name,sql FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name
            """
        )
    )


def _table_snapshot(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[object, ...], ...]:
    """连同 rowid 冻结重建表全列，防止迁移静默重排或丢失历史事实。"""

    assert table in LEGACY_REBUILT_TABLES
    return tuple(tuple(row) for row in connection.execute(f"SELECT rowid,* FROM {table}"))


def _identity(connection: sqlite3.Connection) -> dict[str, str]:
    """读取同租户用户、Key、环境、模型和 Provider 事实图。"""

    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,user.email,user.role,
               key.virtual_key_id,environment.environment_id,
               environment.code AS environment_code,model.model_id,
               provider.provider_id,provider.provider_code
        FROM tenants tenant
        JOIN users user
          ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN virtual_keys key ON key.tenant_id=tenant.tenant_id
        JOIN environments environment
          ON environment.tenant_id=tenant.tenant_id AND environment.code='dev'
        JOIN models model
          ON model.tenant_id=tenant.tenant_id AND model.status='active'
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id
         AND provider.provider_id=model.provider_id
        ORDER BY tenant.slug='default' DESC,tenant.tenant_id,user.user_id,
                 key.virtual_key_id,model.model_id
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return {column: str(row[column]) for column in row.keys()}


def _insert_model_alias(
    connection: sqlite3.Connection,
    identity: dict[str, str],
) -> str:
    """为策略守卫构造同租户 Alias；正式 seed 不承诺预置任意 Alias。"""

    model_alias_id = "mal_prd076_policy"
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,default_model_id,
          fallback_model_id,status,description,created_by,disabled_at,
          disabled_by,created_at,updated_at,environment_id
        ) VALUES(?,?,?,'dev',?,NULL,'active',NULL,?,NULL,NULL,?,?,?)
        """,
        (
            model_alias_id,
            identity["tenant_id"],
            "prd076-policy-guard",
            identity["model_id"],
            identity["user_id"],
            STARTED_AT,
            STARTED_AT,
            identity["environment_id"],
        ),
    )
    return model_alias_id


def _insert_legacy_facts(connection: sqlite3.Connection) -> None:
    """写入 0068 合法历史事实；固定值均为合成摘要，不含 Prompt/Response 或凭据。"""

    identity = _identity(connection)
    request_id = "req_prd076_legacy"
    trace_id = "trace_prd076_legacy"
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
            STARTED_AT,
        ),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,
          provider_code,status,error_code,created_at,updated_at,
          environment_id,environment_code
        ) VALUES(?,?,?,?,?,?,?,'failed','content_policy_violation',?,?,?,?)
        """,
        (
            request_id,
            identity["tenant_id"],
            trace_id,
            identity["virtual_key_id"],
            identity["provider_id"],
            identity["model_id"],
            identity["provider_code"],
            STARTED_AT,
            COMPLETED_AT,
            identity["environment_id"],
            identity["environment_code"],
        ),
    )

    # Output Safety 事实只使用合成规则摘要；阻断发生在输出交付前，不保存响应正文。
    dlp_rule_id = "dlp_prd076_legacy"
    pattern = "fixture-prd076"
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,
          action,status,priority,scan_scope,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'keyword',?,?,'block','active',10,'output',?,?,?,?)
        """,
        (
            dlp_rule_id,
            identity["tenant_id"],
            "PRD076 legacy fixture",
            pattern,
            hashlib.sha256(pattern.encode("ascii")).hexdigest(),
            identity["user_id"],
            identity["user_id"],
            STARTED_AT,
            STARTED_AT,
        ),
    )
    scan_id = "oss_" + "a" * 32
    connection.execute(
        """
        INSERT INTO output_safety_scans(
          output_safety_scan_id,tenant_id,request_id,trace_id,stream_session_id,
          scan_mode,scan_status,decision,action,dlp_rule_id,rule_type,
          match_count,ruleset_fingerprint,detector_version,
          interval_tokens,window_tokens,checkpoint_count,
          observed_output_token_count,scanned_token_count,emitted_token_count,
          emitted_sse_event_count,emitted_bytes,exposure_state,
          billing_disposition,failure_reason_code,started_at,completed_at
        ) VALUES(?,?,?,?,NULL,'non_stream_full','pending','none',NULL,NULL,NULL,
                 0,NULL,'output-safety-dlp-v1',NULL,NULL,0,0,0,0,0,0,
                 'unknown','not_required',NULL,?,NULL)
        """,
        (scan_id, identity["tenant_id"], request_id, trace_id, STARTED_AT),
    )
    connection.execute(
        """
        UPDATE output_safety_scans
        SET scan_status='completed',decision='blocked',action='block',
            dlp_rule_id=?,rule_type='keyword',match_count=1,
            ruleset_fingerprint=?,checkpoint_count=1,
            observed_output_token_count=3,scanned_token_count=3,
            exposure_state='none',billing_disposition='dispute_pending',
            completed_at=?
        WHERE output_safety_scan_id=?
        """,
        (dlp_rule_id, "a" * 64, COMPLETED_AT, scan_id),
    )
    connection.execute(
        """
        INSERT INTO output_safety_events(
          event_id,tenant_id,output_safety_scan_id,request_id,trace_id,event_type,
          schema_version,scan_mode,scan_status,decision,error_code,exposure_state,
          emitted_token_count,checkpoint_count,dlp_rule_id,detector_version,
          billing_disposition,occurred_at
        ) VALUES(?,?,?,?,?,'security.output_blocked',1,'non_stream_full',
                 'completed','blocked','content_policy_violation','none',0,1,?,
                 'output-safety-dlp-v1','dispute_pending',?)
        """,
        (
            "osevt_" + "b" * 32,
            identity["tenant_id"],
            scan_id,
            request_id,
            trace_id,
            dlp_rule_id,
            COMPLETED_AT,
        ),
    )

    # 普通 upstream Usage 生成既有 FinOps 事实，用于证明扩表不改变历史收费记录。
    price_snapshot_id = "prs_prd076_legacy"
    connection.execute(
        """
        INSERT INTO price_snapshots(
          price_snapshot_id,tenant_id,request_id,model_id,provider_id,
          provider_code,currency,input_price_per_1k,output_price_per_1k,
          price_schema_version,billing_dimensions,provider_raw_mapping,
          source,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'CNY','0.001000','0.002000','price-v1',
                 '{}','{}','model_catalog','active',?,?)
        """,
        (
            price_snapshot_id,
            identity["tenant_id"],
            request_id,
            identity["model_id"],
            identity["provider_id"],
            identity["provider_code"],
            STARTED_AT,
            STARTED_AT,
        ),
    )
    normalized_usage = {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "cache_tokens": 0,
        "cost_amount": "1.250000",
        "cost_cny": "1.250000",
        "cost_usd": "0",
        "currency": "CNY",
        "price_schema_version": "price-v1",
        "price_snapshot_id": price_snapshot_id,
        "posting_source": "direct",
    }
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,
          total_tokens,cache_tokens,raw_usage,normalized_usage,cost_amount,
          cost_cny,cost_usd,currency,price_schema_version,estimated,usage_source,
          price_snapshot_id,model_id,provider_id,virtual_key_id,
          environment_id,environment_code,provider_code,created_at,updated_at
        ) VALUES(?,?,?,3,2,5,0,'{}',?,?,?,'0','CNY','price-v1',0,'upstream',
                 ?,?,?,?,?,?,?,?,?)
        """,
        (
            "usg_prd076_legacy",
            identity["tenant_id"],
            request_id,
            json.dumps(normalized_usage, separators=(",", ":"), sort_keys=True),
            "1.250000",
            "1.250000",
            price_snapshot_id,
            identity["model_id"],
            identity["provider_id"],
            identity["virtual_key_id"],
            identity["environment_id"],
            identity["environment_code"],
            identity["provider_code"],
            COMPLETED_AT,
            COMPLETED_AT,
        ),
    )

    # 选择已有 Provider 的普通审计动作，避免把 PRD-076 业务事实带入可回滚夹具。
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
          action,object_type,object_id,object_name,before_summary_json,
          after_summary_json,changed_fields_json,client_ip,user_agent,
          request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'provider.updated','provider',?,NULL,
                 '{}','{}','[]',NULL,NULL,?,?,?)
        """,
        (
            "aud_prd076_legacy",
            identity["tenant_id"],
            identity["user_id"],
            identity["email"],
            identity["role"],
            identity["provider_id"],
            "req_prd076_audit",
            "trace_prd076_audit",
            COMPLETED_AT,
        ),
    )
    connection.commit()


def _assert_cache_rbac(connection: sqlite3.Connection) -> None:
    """验证 revision-12 exact 基数以及默认角色的后端授权投影。"""

    assert tuple(
        connection.execute(
            """
            SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision)
            FROM rbac_permission_templates
            """
        ).fetchone()
    ) == (69, 12, 12)
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_role_permission_templates"
    ).fetchone()[0] == 185
    assert connection.execute("SELECT COUNT(*) FROM permissions").fetchone()[0] == (
        connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] * 69
    )
    assert {
        str(row[0])
        for row in connection.execute(
            """
            SELECT permission_code FROM rbac_permission_templates
            WHERE permission_code LIKE 'response_cache.%'
            """
        )
    } == CACHE_PERMISSIONS
    grants = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """
            SELECT role.code,permission.code
            FROM roles role
            JOIN role_permissions relation
              ON relation.tenant_id=role.tenant_id
             AND relation.role_id=role.role_id
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE role.tenant_id=(
              SELECT tenant_id FROM tenants WHERE slug='default'
            ) AND permission.code LIKE 'response_cache.%'
            """
        )
    }
    assert grants == {
        (role_code, permission_code)
        for role_code in ("admin", "finance_admin")
        for permission_code in CACHE_PERMISSIONS
    }


def test_prd076_fresh_0069_repeat_schema_integrity_fk_and_rbac(
    tmp_path: Path,
    prd076_templates: dict[str, Path],
) -> None:
    """从空 SQLite 实际迁移到 0069，并验证幂等、结构、RBAC 与健康门禁。"""

    database_url = _url(tmp_path / "fresh-0069.sqlite3")
    result = apply_migrations(
        database_url,
        migrations_dir=prd076_templates["migrations_0069"],
    )
    assert result.applied == EXPECTED_VERSIONS
    repeat = apply_migrations(
        database_url,
        migrations_dir=prd076_templates["migrations_0069"],
    )
    assert repeat.applied == []
    assert repeat.skipped == EXPECTED_VERSIONS
    apply_seed_data(database_url)

    connection = connect_database(database_url)
    assert _applied_head(connection) == "0069"
    assert {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type='table' AND name LIKE 'response_cache_%'
            """
        )
    } == RESPONSE_CACHE_TABLES
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema "
        "WHERE type='index' AND name LIKE '%prd076%'"
    ).fetchone()[0] == 14
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema "
        "WHERE type='trigger' AND name LIKE '%prd076%'"
    ).fetchone()[0] == 45
    assert tuple(
        connection.execute(
            """
            SELECT key_state_id,key_schema_version,active_hmac_key_version,
                   active_encryption_key_version,hmac_secret_fingerprint,
                   encryption_secret_fingerprint,generation
            FROM response_cache_key_state
            """
        ).fetchone()
    ) == (1, 1, 1, 1, ZERO_FINGERPRINT, ZERO_FINGERPRINT, 1)
    _assert_cache_rbac(connection)
    _assert_healthy(connection)
    connection.close()


def test_prd076_0068_0069_round_trip_exact_schema_and_data_preservation(
    tmp_path: Path,
    prd076_templates: dict[str, Path],
) -> None:
    """验证 0068→0069→0068→0069 的双基线 exact schema 与历史事实保真。"""

    database = tmp_path / "round-trip.sqlite3"
    shutil.copy2(prd076_templates["database_0068"], database)
    database_url = _url(database)
    connection = connect_database(database_url)
    _insert_legacy_facts(connection)
    schema_0068 = _schema_snapshot(connection)
    facts = {
        table: _table_snapshot(connection, table) for table in LEGACY_REBUILT_TABLES
    }
    assert all(facts.values())
    _assert_healthy(connection)
    connection.close()

    assert apply_migrations(
        database_url,
        migrations_dir=prd076_templates["migrations_0069"],
    ).applied == ["0069"]
    connection = connect_database(database_url)
    schema_0069 = _schema_snapshot(connection)
    for table, expected in facts.items():
        assert _table_snapshot(connection, table) == expected
    _assert_healthy(connection)
    connection.close()

    assert rollback_last_migration(
        database_url,
        migrations_dir=prd076_templates["migrations_0069"],
    ).rolled_back == "0069"
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0068"
    assert _schema_snapshot(connection) == schema_0068
    for table, expected in facts.items():
        assert _table_snapshot(connection, table) == expected
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_schema
        WHERE name LIKE 'response_cache_%'
        """
    ).fetchone()[0] == 0
    _assert_healthy(connection)
    connection.close()

    assert apply_migrations(
        database_url,
        migrations_dir=prd076_templates["migrations_0069"],
    ).applied == ["0069"]
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0069"
    assert _schema_snapshot(connection) == schema_0069
    for table, expected in facts.items():
        assert _table_snapshot(connection, table) == expected
    _assert_healthy(connection)
    connection.close()


def test_prd076_schema_drift_blocks_down_atomically(
    tmp_path: Path,
    prd076_templates: dict[str, Path],
) -> None:
    """缺少任一权威表时 down 必须在首个持久 DDL 前原子失败。"""

    database_url, connection = _clone_and_upgrade(
        prd076_templates,
        tmp_path / "schema-guard.sqlite3",
    )
    connection.execute("DROP TABLE response_cache_event_outbox")
    connection.commit()
    schema_before = _schema_snapshot(connection)
    connection.close()

    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd076_templates["migrations_0069"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd076_down_schema_invalid" in error.value.message

    connection = connect_database(database_url)
    assert _applied_head(connection) == "0069"
    assert _schema_snapshot(connection) == schema_before
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_schema
        WHERE type='table' AND name='response_cache_policies'
        """
    ).fetchone()[0] == 1
    _assert_healthy(connection)
    connection.close()


@pytest.mark.parametrize("business_fact", ("policy", "key_state"))
def test_prd076_business_facts_block_down_atomically(
    tmp_path: Path,
    prd076_templates: dict[str, Path],
    business_fact: str,
) -> None:
    """策略事实或 key-state 初始化后均不得被 down 静默删除。"""

    database_url, connection = _clone_and_upgrade(
        prd076_templates,
        tmp_path / f"business-guard-{business_fact}.sqlite3",
    )
    if business_fact == "policy":
        identity = _identity(connection)
        model_alias_id = _insert_model_alias(connection, identity)
        connection.execute(
            """
            INSERT INTO response_cache_policies(
              cache_policy_id,tenant_id,model_alias_id,status,data_class,
              ttl_seconds,charge_cache_hits,tags_json,
              single_data_class_confirmed,data_class_confirmed_by,
              data_class_confirmed_version,version,invalidation_generation,
              created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,?,'disabled',NULL,60,0,'[]',0,NULL,NULL,1,1,?,?,?,?)
            """,
            (
                "rcp_" + "c" * 32,
                identity["tenant_id"],
                model_alias_id,
                identity["user_id"],
                identity["user_id"],
                STARTED_AT,
                STARTED_AT,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE response_cache_key_state
            SET hmac_secret_fingerprint=?,generation=generation+1,updated_at=?
            WHERE key_state_id=1
            """,
            ("1" * 64, "2030-01-01T00:00:00Z"),
        )
    connection.commit()
    schema_before = _schema_snapshot(connection)
    fact_snapshot = tuple(
        connection.execute(
            "SELECT * FROM response_cache_key_state ORDER BY key_state_id"
        ).fetchone()
    )
    policy_count = connection.execute(
        "SELECT COUNT(*) FROM response_cache_policies"
    ).fetchone()[0]
    connection.close()

    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd076_templates["migrations_0069"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd076_down_business_facts_present" in error.value.message

    connection = connect_database(database_url)
    assert _applied_head(connection) == "0069"
    assert _schema_snapshot(connection) == schema_before
    assert tuple(
        connection.execute(
            "SELECT * FROM response_cache_key_state ORDER BY key_state_id"
        ).fetchone()
    ) == fact_snapshot
    assert connection.execute(
        "SELECT COUNT(*) FROM response_cache_policies"
    ).fetchone()[0] == policy_count
    _assert_healthy(connection)
    connection.close()
