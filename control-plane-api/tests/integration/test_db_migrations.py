"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_db_migrations.py
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import metahub_shared.db.migrate as migration_module
import pytest
from metahub_shared.db import (
    apply_migrations as apply_current_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError


# 历史总迁移套件冻结在 0040，但 0041 由下方单独覆盖。新增正式迁移后仍要先
# 完整执行 up，再依据本次真实结果逐版回退，避免维护静态栈顶时漏掉新版本。
ROLLBACK_BOUNDARY_VERSION = "0041"
"""通用历史断言开始前保留并单独验证的最后一个正式迁移版本。"""

# PRD-042 起生产 Delivery ID 始终是 ``whd_`` 加 32 位小写十六进制；历史迁移
# 夹具也必须遵守该应用合同，0047 才能确定性派生同根 DeadLetter ID。


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"

def apply_migrations(
    database_url: str | None = None,
    *,
    migrations_dir: Path | None = None,
) -> migration_module.MigrationResult:
    """执行历史迁移套件，并在默认目录上把数据库稳定恢复到该套件冻结的 0040。"""

    result = apply_current_migrations(database_url, migrations_dir=migrations_dir)
    # 自定义目录用于迁移框架专项，版本号与主迁移链无关，必须原样返回真实结果。
    if migrations_dir is not None:
        return result
    # 只信任迁移器本次报告的实际版本，并按零填充版本号逆序回退；这样后续新增
    # 0059+ 时不会错误地把当前栈顶当成旧的固定版本。
    later_migrations = tuple(
        sorted(
            {
                version
                for version in (*result.applied, *result.skipped)
                if version > ROLLBACK_BOUNDARY_VERSION
            },
            reverse=True,
        )
    )
    for version in later_migrations:
        # 回滚结果必须逐项等于预期栈顶；错序会掩盖 migration history 漂移。
        rolled_back = rollback_last_migration(database_url)
        assert rolled_back.rolled_back == version
    if "0041" not in result.applied and "0041" not in result.skipped:
        excluded = set(later_migrations)
        return migration_module.MigrationResult(
            run_id=result.run_id,
            applied=[version for version in result.applied if version not in excluded],
            skipped=[version for version in result.skipped if version not in excluded],
            rolled_back=result.rolled_back,
        )

    # 本文件的历史断言冻结在 0040；0041 及更高版本均由各 PRD 专项覆盖后逐版回退。
    rollback = rollback_last_migration(database_url)
    assert rollback.rolled_back == "0041"
    excluded = set(later_migrations) | {"0041"}
    return migration_module.MigrationResult(
        run_id=result.run_id,
        applied=[version for version in result.applied if version not in excluded],
        skipped=[version for version in result.skipped if version not in excluded],
        rolled_back=result.rolled_back,
    )


def table_count(database_url: str, table: str) -> int:
    """读取指定表的行数，用于验证迁移和种子数据是否幂等。"""

    connection = connect_database(database_url)
    try:
        row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        return int(row["count"])
    finally:
        # 测试辅助函数也必须释放只读句柄，避免掩盖生产迁移入口的连接生命周期回归。
        connection.close()


def canonical_utc_after(raw_time: object, *, seconds: int) -> str:
    """基于数据库 UTC 时间生成确定的未来秒级 Z 文本，避免测试跨自然日失效。"""

    parsed = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # SQLite datetime('now') 返回无时区 UTC 文本，只在测试夹具层恢复其约定时区。
        parsed = parsed.replace(tzinfo=UTC)
    shifted = parsed.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=seconds)
    return shifted.isoformat().replace("+00:00", "Z")



def test_migration_entrypoints_close_connections_on_every_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """连接生命周期：apply/rollback 的成功、早退和异常分支都关闭真实 SQLite 连接。"""

    migration_dir = tmp_path / "connection-lifecycle-migrations"
    migration_dir.mkdir()
    (migration_dir / "0001_connection_probe.sql").write_text(
        "CREATE TABLE connection_probe(id TEXT PRIMARY KEY);",
        encoding="utf-8",
    )
    (migration_dir / "0001_connection_probe.down.sql").write_text(
        "DROP TABLE connection_probe;",
        encoding="utf-8",
    )
    broken_dir = tmp_path / "broken-connection-lifecycle-migrations"
    broken_dir.mkdir()
    (broken_dir / "0001_broken.sql").write_text(
        "CREATE TABLE broken(",
        encoding="utf-8",
    )
    missing_down_dir = tmp_path / "missing-down-connection-lifecycle-migrations"
    missing_down_dir.mkdir()
    (missing_down_dir / "0001_missing_down.sql").write_text(
        "CREATE TABLE missing_down_probe(id TEXT PRIMARY KEY);",
        encoding="utf-8",
    )
    broken_down_dir = tmp_path / "broken-down-connection-lifecycle-migrations"
    broken_down_dir.mkdir()
    (broken_down_dir / "0001_broken_down.sql").write_text(
        "CREATE TABLE broken_down_probe(id TEXT PRIMARY KEY);",
        encoding="utf-8",
    )
    (broken_down_dir / "0001_broken_down.down.sql").write_text(
        "DROP TABLE missing_broken_down_probe;",
        encoding="utf-8",
    )
    database_url = sqlite_url(tmp_path / "connection-lifecycle.db")
    broken_database_url = sqlite_url(tmp_path / "broken-connection-lifecycle.db")
    missing_down_database_url = sqlite_url(tmp_path / "missing-down-connection-lifecycle.db")
    broken_down_database_url = sqlite_url(tmp_path / "broken-down-connection-lifecycle.db")
    opened_connections: list[sqlite3.Connection] = []
    real_connect = migration_module.connect_database

    def tracked_connect(database_url_value: str | None) -> sqlite3.Connection:
        """登记迁移入口创建的真实连接，供调用结束后验证底层句柄已经失效。"""

        connection = real_connect(database_url_value)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(migration_module, "connect_database", tracked_connect)

    assert apply_migrations(database_url, migrations_dir=migration_dir).applied == ["0001"]
    # 已应用版本的幂等 skip 也会先创建连接，必须与真正执行 SQL 的成功分支一样及时释放。
    assert apply_migrations(database_url, migrations_dir=migration_dir).skipped == ["0001"]
    (migration_dir / "0001_connection_probe.sql").write_text(
        "CREATE TABLE connection_probe(id TEXT PRIMARY KEY);\n-- checksum changed",
        encoding="utf-8",
    )
    with pytest.raises(DbError) as checksum_changed:
        apply_migrations(database_url, migrations_dir=migration_dir)
    assert checksum_changed.value.code == "db.migration_checksum_changed"
    assert rollback_last_migration(
        database_url,
        migrations_dir=migration_dir,
    ).rolled_back == "0001"
    # 没有 applied 版本的提前返回也必须经过公开入口 finally。
    assert rollback_last_migration(
        database_url,
        migrations_dir=migration_dir,
    ).rolled_back is None
    with pytest.raises(DbError) as broken_sql:
        apply_migrations(broken_database_url, migrations_dir=broken_dir)
    assert broken_sql.value.code == "db.migration_failed"
    with pytest.raises(DbError) as missing_directory:
        apply_migrations(
            sqlite_url(tmp_path / "missing-directory.db"),
            migrations_dir=tmp_path / "does-not-exist",
        )
    assert missing_directory.value.code == "db.migration_failed"
    assert apply_migrations(
        missing_down_database_url,
        migrations_dir=missing_down_dir,
    ).applied == ["0001"]
    with pytest.raises(DbError) as missing_down:
        rollback_last_migration(
            missing_down_database_url,
            migrations_dir=missing_down_dir,
        )
    assert missing_down.value.code == "db.migration_failed"
    assert apply_migrations(
        broken_down_database_url,
        migrations_dir=broken_down_dir,
    ).applied == ["0001"]
    with pytest.raises(DbError) as broken_down:
        rollback_last_migration(
            broken_down_database_url,
            migrations_dir=broken_down_dir,
        )
    assert broken_down.value.code == "db.migration_failed"

    assert len(opened_connections) == 11
    for connection in opened_connections:
        # 对真实连接执行 SQL 必须报 closed，防止只验证 mock 调用而遗漏底层文件锁。
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

def rollback_request_feedback_migration(database_url: str) -> None:
    """先回滚空事实 PRD-050/049，使历史迁移专项恢复到真实 0037 结构。"""

    affinity_result = rollback_last_migration(database_url)
    assert affinity_result.rolled_back == "0040"
    hardening_result = rollback_last_migration(database_url)
    assert hardening_result.rolled_back == "0039"
    result = rollback_last_migration(database_url)
    assert result.rolled_back == "0038"


def rollback_quality_cost_migration(database_url: str) -> None:
    """依次回滚 PRD-049/048，使历史迁移专项在真实 0036 结构上执行。"""

    rollback_request_feedback_migration(database_url)
    result = rollback_last_migration(database_url)
    assert result.rolled_back == "0037"


def rollback_policy_engine_migration(database_url: str) -> None:
    """从当前最高版本连续回滚至 0032，让 PRD-044 往返从真实历史结构开始。"""

    rollback_quality_cost_migration(database_url)
    routing_result = rollback_last_migration(database_url)
    assert routing_result.rolled_back == "0036"
    dry_run_result = rollback_last_migration(database_url)
    assert dry_run_result.rolled_back == "0035"
    snapshot_result = rollback_last_migration(database_url)
    assert snapshot_result.rolled_back == "0034"
    result = rollback_last_migration(database_url)
    assert result.rolled_back == "0033"


def rollback_policy_snapshot_migration(database_url: str) -> None:
    """从当前最高版本连续回滚至 0033，供 PRD-045 测试构造真实历史数据。"""

    rollback_quality_cost_migration(database_url)
    routing_result = rollback_last_migration(database_url)
    assert routing_result.rolled_back == "0036"
    dry_run_result = rollback_last_migration(database_url)
    assert dry_run_result.rolled_back == "0035"
    result = rollback_last_migration(database_url)
    assert result.rolled_back == "0034"


def rollback_usage_export_migration(database_url: str) -> None:
    """回滚 PRD-043 迁移，让较早 PRD 的往返测试从其真实历史版本开始。"""

    result = rollback_last_migration(database_url)
    assert result.rolled_back == "0032"


def insert_test_stream_session(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    stream_session_id: str,
    request_id: str,
) -> None:
    """使用租户内种子元数据创建 starting 会话，供状态机和关联约束测试复用。

    测试夹具也只写安全字段，不引入 Endpoint、Credential、Prompt 或响应正文，
    从而与生产 StreamSession 的最小审计边界保持一致。
    """

    metadata = connection.execute(
        """
        SELECT
          vk.virtual_key_id, e.environment_id, e.code AS environment_code,
          m.model_id, p.provider_id, p.provider_code
        FROM virtual_keys vk
        JOIN environments e ON e.tenant_id=vk.tenant_id AND e.code='dev'
        JOIN models m ON m.tenant_id=vk.tenant_id
        JOIN providers p ON p.provider_id=m.provider_id AND p.tenant_id=m.tenant_id
        WHERE vk.tenant_id=?
        ORDER BY m.model_id
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert metadata is not None
    connection.execute(
        """
        INSERT INTO stream_sessions(
          stream_session_id, tenant_id, request_id, trace_id, virtual_key_id,
          environment_id, environment_code, model_id, provider_id, provider_code,
          status, inter_token_timeout_ms, started_at, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'starting', 30000, ?, ?, ?)
        """,
        (
            stream_session_id,
            tenant_id,
            request_id,
            f"trc_{request_id}",
            metadata["virtual_key_id"],
            metadata["environment_id"],
            metadata["environment_code"],
            metadata["model_id"],
            metadata["provider_id"],
            metadata["provider_code"],
            "2026-07-14T10:00:00Z",
            "2026-07-14T10:00:00Z",
            "2026-07-14T10:00:00Z",
        ),
    )


def insert_disabled_request_claim(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    request_id: str,
    trace_id: str,
    virtual_key_id: str | None = None,
) -> None:
    """为绕过 HTTP 编排的迁移测试写入 disabled claim。

    生产请求由 Gateway/Debug Console 在读取正文前认领；旧 PRD 的数据库专项直接插入
    request_logs，因此必须显式补齐相同前置事实，且不能伪造已执行策略快照。
    """

    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES(?, ?, ?, ?, NULL, 'disabled', '2026-07-17T00:00:00Z')
        """,
        (tenant_id, request_id, trace_id, virtual_key_id),
    )


def test_migrate_empty_database_and_repeat_is_idempotent(tmp_path: Path) -> None:
    """主流程：空库迁移成功，重复执行不重复应用。"""

    database_url = sqlite_url(tmp_path / "metahub.db")

    first = apply_current_migrations(database_url)
    second = apply_current_migrations(database_url)

    expected_versions = [
        "0001",
        "0002",
        "0003",
        "0004",
        "0005",
        "0006",
        "0007",
        "0008",
        "0009",
        "0010",
        "0011",
        "0012",
        "0013",
        "0014",
        "0015",
        "0016",
        "0017",
        "0018",
        "0019",
        "0020",
        "0021",
        "0022",
        "0023",
        "0024",
        "0025",
        "0026",
        "0027",
        "0028",
        "0029",
        "0030",
        "0031",
        "0032",
        "0033",
        "0034",
        "0035",
        "0036",
        "0037",
        "0038",
        "0039",
        "0040",
        "0041",
        "0042",
        "0043",
        "0044",
        "0045",
        "0046",
        "0047",
        "0048",
        "0049",
        "0050",
        "0051",
        "0052",
        "0053",
        "0054",
        "0055",
        "0056",
        "0057",
        "0058",
        "0059",
        "0060",
        "0061",
    ]
    assert first.applied == expected_versions
    assert second.applied == []
    assert second.skipped == expected_versions
    assert table_count(database_url, "tenants") == 0
    assert table_count(database_url, "roles") == 0
    assert table_count(database_url, "schema_migrations") == 61
    columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(virtual_keys)").fetchall()
    }
    assert {
        "environment",
        "environment_id",
        "description",
        "last_used_at",
        "disabled_at",
        "project_id",
        "application_id",
    }.issubset(columns)
    provider_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(providers)").fetchall()
    }
    assert {"provider_type", "auth_type", "health_status", "last_tested_at", "disabled_at"}.issubset(
        provider_columns
    )
    model_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(models)").fetchall()
    }
    assert {
        "provider_model_id",
        "context_window",
        "input_price_per_1k",
        "output_price_per_1k",
        "currency",
        "price_schema_version",
        "disabled_at",
    }.issubset(model_columns)
    provider_key_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(provider_credentials)").fetchall()
    }
    assert {
        "name",
        "key_prefix",
        "weight",
        "last_used_at",
        "last_error_at",
        "rate_limited_until",
        "disabled_at",
    }.issubset(provider_key_columns)
    model_alias_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(model_aliases)").fetchall()
    }
    assert {"tenant_id", "alias", "environment", "default_model_id", "fallback_model_id", "status"}.issubset(
        model_alias_columns
    )
    routing_decision_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(routing_decisions)").fetchall()
    }
    assert {
        "tenant_id",
        "request_id",
        "requested_model",
        "selected_model_id",
        "decision_reason",
        "error_code",
    }.issubset(routing_decision_columns)
    request_log_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(request_logs)").fetchall()
    }
    assert {
        "endpoint",
        "request_model",
        "resolved_model",
        "provider_code",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "estimated_cost_amount",
        "currency",
        "price_schema_version",
        "budget_status",
        "budget_rule_id",
        "budget_scope_type",
        "budget_scope_id",
        "budget_action",
        "budget_reason_code",
        "budget_current_cost_cny",
        "budget_limit_cny",
        "dlp_status",
        "dlp_rule_id",
        "dlp_action",
        "dlp_match_count",
        "dlp_reason_code",
        "project_id",
        "application_id",
        "fallback_triggered",
        "routing_attempt_count",
        "final_routing_attempt_id",
        "stream_session_id",
        "stream_status",
        "updated_at",
    }.issubset(request_log_columns)
    usage_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(usage_ledger)").fetchall()
    }
    assert {
        "total_tokens",
        "cache_tokens",
        "cost_cny",
        "cost_usd",
        "estimated",
        "usage_source",
        "price_snapshot_id",
        "model_id",
        "virtual_key_id",
        "project_id",
        "application_id",
        "routing_attempt_id",
        "updated_at",
    }.issubset(usage_columns)
    price_snapshot_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='price_snapshots'"
    ).fetchone()
    assert price_snapshot_table is not None
    price_plan_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='model_price_plans'"
    ).fetchone()
    price_event_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='model_price_events'"
    ).fetchone()
    price_snapshot_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(price_snapshots)").fetchall()
    }
    assert price_plan_table is not None
    assert price_event_table is not None
    assert {"price_plan_id", "effective_from"}.issubset(price_snapshot_columns)
    user_preferences_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_preferences'"
    ).fetchone()
    user_preference_events_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_preference_events'"
    ).fetchone()
    console_page_events_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='console_page_events'"
    ).fetchone()
    developer_portal_events_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='developer_portal_events'"
    ).fetchone()
    debug_console_events_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='debug_console_events'"
    ).fetchone()
    rate_limit_rules_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='virtual_key_rate_limit_rules'"
    ).fetchone()
    budget_rules_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_rules'"
    ).fetchone()
    budget_alert_events_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_alert_events'"
    ).fetchone()
    budget_evaluations_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_evaluations'"
    ).fetchone()
    budget_blocks_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_blocks'"
    ).fetchone()
    budget_block_events_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_block_events'"
    ).fetchone()
    budget_reservations_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_reservations'"
    ).fetchone()
    projects_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    applications_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='applications'"
    ).fetchone()
    project_events_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='project_events'"
    ).fetchone()
    dlp_rules_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dlp_rules'"
    ).fetchone()
    dlp_matches_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dlp_matches'"
    ).fetchone()
    webhook_endpoints_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_endpoints'"
    ).fetchone()
    webhook_deliveries_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_deliveries'"
    ).fetchone()
    webhook_attempts_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_delivery_attempts'"
    ).fetchone()
    fallback_policies_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fallback_policies'"
    ).fetchone()
    routing_attempts_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='routing_attempts'"
    ).fetchone()
    stream_sessions_table = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='stream_sessions'"
    ).fetchone()
    dashboard_latency_index = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_request_logs_dashboard_latency'"
    ).fetchone()
    dashboard_key_index = connect_database(database_url).execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_request_logs_dashboard_key_time'"
    ).fetchone()
    console_page_events_sql = connect_database(database_url).execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='console_page_events'"
    ).fetchone()
    user_preference_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(user_preferences)").fetchall()
    }
    console_event_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(console_page_events)").fetchall()
    }
    developer_portal_event_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(developer_portal_events)").fetchall()
    }
    debug_console_event_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(debug_console_events)").fetchall()
    }
    rate_limit_rule_columns = {
        row["name"]
        for row in connect_database(database_url).execute(
            "PRAGMA table_info(virtual_key_rate_limit_rules)"
        ).fetchall()
    }
    budget_rule_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(budget_rules)").fetchall()
    }
    budget_evaluation_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(budget_evaluations)").fetchall()
    }
    budget_block_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(budget_blocks)").fetchall()
    }
    budget_reservation_columns = {
        row["name"]
        for row in connect_database(database_url)
        .execute("PRAGMA table_info(budget_reservations)")
        .fetchall()
    }
    project_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(projects)").fetchall()
    }
    application_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(applications)").fetchall()
    }
    dlp_rule_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(dlp_rules)").fetchall()
    }
    dlp_match_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(dlp_matches)").fetchall()
    }
    webhook_endpoint_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(webhook_endpoints)").fetchall()
    }
    webhook_delivery_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(webhook_deliveries)").fetchall()
    }
    webhook_attempt_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(webhook_delivery_attempts)").fetchall()
    }
    fallback_policy_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(fallback_policies)").fetchall()
    }
    routing_attempt_columns = {
        row["name"]
        for row in connect_database(database_url).execute("PRAGMA table_info(routing_attempts)").fetchall()
    }
    stream_session_columns = {
        row["name"]
        for row in connect_database(database_url)
        .execute("PRAGMA table_info(stream_sessions)")
        .fetchall()
    }
    assert user_preferences_table is not None
    assert user_preference_events_table is not None
    assert console_page_events_table is not None
    assert developer_portal_events_table is not None
    assert debug_console_events_table is not None
    assert rate_limit_rules_table is not None
    assert budget_rules_table is not None
    assert budget_alert_events_table is not None
    assert budget_evaluations_table is not None
    assert budget_blocks_table is not None
    assert budget_block_events_table is not None
    assert budget_reservations_table is not None
    assert projects_table is not None
    assert applications_table is not None
    assert project_events_table is not None
    assert dlp_rules_table is not None
    assert dlp_matches_table is not None
    assert webhook_endpoints_table is not None
    assert webhook_deliveries_table is not None
    assert webhook_attempts_table is not None
    assert fallback_policies_table is not None
    assert routing_attempts_table is not None
    assert stream_sessions_table is not None
    assert dashboard_latency_index is not None
    assert dashboard_key_index is not None
    assert console_page_events_sql is not None
    assert "dashboard.viewed" in console_page_events_sql["sql"]
    assert {"tenant_id", "user_id", "active_navigation_item_id", "sidebar_collapsed", "locale"}.issubset(
        user_preference_columns
    )
    assert {"tenant_id", "user_id", "session_id", "event_type", "navigation_item_id"}.issubset(
        console_event_columns
    )
    assert {"tenant_id", "user_id", "session_id", "event_type", "object_id", "payload_json"}.issubset(
        developer_portal_event_columns
    )
    assert {
        "tenant_id",
        "user_id",
        "debug_request_id",
        "gateway_request_id",
        "virtual_key_id",
        "model_alias",
        "status",
        "error_code",
    }.issubset(debug_console_event_columns)
    assert {
        "tenant_id",
        "virtual_key_id",
        "rpm_limit",
        "tpm_limit",
        "concurrency_limit",
        "status",
    }.issubset(rate_limit_rule_columns)
    assert {
        "tenant_id",
        "scope_type",
        "scope_id",
        "daily_budget_cny",
        "warning_threshold_percent",
        "exhausted_action",
        "status",
    }.issubset(budget_rule_columns)
    assert {"tenant_id", "project_code", "name", "owner_user_id", "status"}.issubset(project_columns)
    assert {"tenant_id", "project_id", "application_code", "name", "status"}.issubset(application_columns)
    assert {
        "tenant_id",
        "request_id",
        "budget_rule_id",
        "projected_cost_cny",
        "budget_status",
        "action_taken",
        "reason_code",
    }.issubset(budget_evaluation_columns)
    assert {
        "tenant_id",
        "environment_id",
        "budget_rule_id",
        "budget_date",
        "status",
        "blocked_request_id",
        "release_reason",
        "released_at",
    }.issubset(budget_block_columns)
    assert {
        "tenant_id",
        "environment_id",
        "budget_rule_id",
        "budget_date",
        "request_id",
        "reserved_cost_cny",
    }.issubset(budget_reservation_columns)
    assert {
        "tenant_id",
        "name",
        "rule_type",
        "pattern",
        "pattern_hash",
        "action",
        "status",
        "priority",
    }.issubset(dlp_rule_columns)
    assert {
        "tenant_id",
        "request_id",
        "dlp_rule_id",
        "rule_type",
        "action",
        "match_text_hash",
        "match_length",
    }.issubset(dlp_match_columns)
    assert {
        "tenant_id",
        "endpoint_url",
        "event_types_json",
        "encrypted_secret",
        "secret_fingerprint",
        "last_delivery_status",
    }.issubset(webhook_endpoint_columns)
    assert {
        "tenant_id",
        "webhook_endpoint_id",
        "event_id",
        "event_type",
        "payload_json",
        "attempt_count",
    }.issubset(webhook_delivery_columns)
    assert {
        "tenant_id",
        "webhook_delivery_id",
        "webhook_endpoint_id",
        "attempt_no",
        "status",
        "error_code",
    }.issubset(webhook_attempt_columns)
    assert {
        "tenant_id",
        "model_alias_id",
        "status",
        "version",
        "condition_expr",
        "action",
    }.issubset(fallback_policy_columns)
    assert {
        "tenant_id",
        "request_id",
        "attempt_sequence",
        "attempt_role",
        "model_id",
        "provider_id",
        "status",
        "error_category",
        "usage_status",
    }.issubset(routing_attempt_columns)
    assert {
        "stream_session_id",
        "tenant_id",
        "request_id",
        "trace_id",
        "model_id",
        "provider_id",
        "status",
        "inter_token_timeout_ms",
        "chunk_count",
        "bytes_sent",
        "first_chunk_at",
        "last_chunk_at",
        "error_code",
        "started_at",
        "completed_at",
    }.issubset(stream_session_columns)


def test_seed_is_idempotent_and_does_not_store_plain_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程/安全：seed 幂等，密码、Provider Key、Virtual Key 不明文落库。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "AdminPasswordShouldNotLeak")
    monkeypatch.setenv("METAHUB_SEED_PROVIDER_API_KEY", "sk-provider-should-not-leak")
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", "mhv_virtual_key_should_not_leak")

    apply_migrations(database_url)
    first = apply_seed_data(database_url)
    second = apply_seed_data(database_url)

    assert first.tenant_id == second.tenant_id
    assert table_count(database_url, "tenants") == 1
    assert table_count(database_url, "users") == 1
    assert table_count(database_url, "roles") == 3
    assert table_count(database_url, "providers") == 1
    assert table_count(database_url, "models") == 1
    assert table_count(database_url, "virtual_keys") == 1

    connection = connect_database(database_url)
    dump = "\n".join(
        str(value)
        for table in [
            "users",
            "provider_credentials",
            "virtual_keys",
            "seed_runs",
        ]
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
    )
    assert "AdminPasswordShouldNotLeak" not in dump
    assert "sk-provider-should-not-leak" not in dump
    assert "mhv_virtual_key_should_not_leak" not in dump
    assert "local-placeholder:" in dump


def test_migration_checksum_change_is_rejected(tmp_path: Path) -> None:
    """异常流程：已经执行过的迁移文件不允许被静默修改。"""

    source = Path("infra/migrations/0001_core_mvp_tables.sql")
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    target = migration_dir / source.name
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url, migrations_dir=migration_dir)
    target.write_text(target.read_text(encoding="utf-8") + "\n-- changed\n", encoding="utf-8")

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url, migrations_dir=migration_dir)

    assert exc.value.code == "db.migration_checksum_changed"


def test_budget_block_ownership_rejects_cross_environment_inserts(tmp_path: Path) -> None:
    """安全约束：block 和 event 的冗余 tenant/environment/rule 必须指向同一归属链。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    dev_environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (seed.tenant_id,),
    ).fetchone()["environment_id"]
    connection.execute(
        """
        INSERT INTO environments(
          environment_id, tenant_id, code, name, status, created_at, updated_at
        )
        VALUES('env_prd033_test', ?, 'test', 'PRD033 Test', 'active', ?, ?)
        """,
        (seed.tenant_id, "2026-07-09T00:00:00Z", "2026-07-09T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        )
        VALUES('br_prd033_owner', ?, ?, 'tenant', ?, 'Owner constraint',
          10, 80, 100, 'block', 'active', ?, ?)
        """,
        (
            seed.tenant_id,
            dev_environment_id,
            seed.tenant_id,
            "2026-07-09T00:00:00Z",
            "2026-07-09T00:00:00Z",
        ),
    )

    with pytest.raises(sqlite3.IntegrityError, match="budget_block_owner_mismatch"):
        connection.execute(
            """
            INSERT INTO budget_blocks(
              budget_block_id, tenant_id, environment_id, budget_rule_id, scope_type, scope_id,
              budget_date, status, blocked_cost_cny, budget_cny, blocked_request_id,
              blocked_trace_id, blocked_at, created_at, updated_at
            )
            VALUES('bb_prd033_cross_env', ?, 'env_prd033_test', 'br_prd033_owner',
              'tenant', ?, '2026-07-09', 'active', 10, 10, 'req_cross', 'trc_cross', ?, ?, ?)
            """,
            (
                seed.tenant_id,
                seed.tenant_id,
                "2026-07-09T00:00:00Z",
                "2026-07-09T00:00:00Z",
                "2026-07-09T00:00:00Z",
            ),
        )

    connection.execute(
        """
        INSERT INTO budget_blocks(
          budget_block_id, tenant_id, environment_id, budget_rule_id, scope_type, scope_id,
          budget_date, status, blocked_cost_cny, budget_cny, blocked_request_id,
          blocked_trace_id, blocked_at, created_at, updated_at
        )
        VALUES('bb_prd033_valid', ?, ?, 'br_prd033_owner', 'tenant', ?, '2026-07-09',
          'active', 10, 10, 'req_valid', 'trc_valid', ?, ?, ?)
        """,
        (
            seed.tenant_id,
            dev_environment_id,
            seed.tenant_id,
            "2026-07-09T00:00:00Z",
            "2026-07-09T00:00:00Z",
            "2026-07-09T00:00:00Z",
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="budget_block_event_owner_mismatch"):
        connection.execute(
            """
            INSERT INTO budget_block_events(
              budget_block_event_id, event_id, tenant_id, environment_id, budget_block_id,
              budget_rule_id, event_type, reason_code, current_cost_cny, budget_cny, created_at
            )
            VALUES(
              'bbe_prd033_cross_env', 'evt_prd033_cross_env', ?, 'env_prd033_test',
              'bb_prd033_valid', 'br_prd033_owner', 'budget.blocked',
              'budget.exhausted.block', 10, 10, ?
            )
            """,
            (seed.tenant_id, "2026-07-09T00:00:00Z"),
        )


def test_stream_session_tenant_state_and_request_log_constraints(tmp_path: Path) -> None:
    """PRD-037：数据库拒绝跨租户引用、非法状态回退和不一致的调用日志摘要。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    insert_test_stream_session(
        connection,
        seed.tenant_id,
        stream_session_id="str_prd037_timeout",
        request_id="req_prd037_timeout",
    )

    # 即使维护脚本准备了另一个有效租户，也不能把既有会话整体迁移过去。
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES(
          'tenant_prd037_other', 'prd037-other', 'PRD037 Other', 'internal_only', 'active',
          '2026-07-14T10:00:00Z', '2026-07-14T10:00:00Z'
        )
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="stream_session_tenant_immutable"):
        connection.execute(
            """
            UPDATE stream_sessions
            SET tenant_id='tenant_prd037_other'
            WHERE stream_session_id='str_prd037_timeout'
            """
        )

    # INSERT 触发器必须在单列外键之外校验租户，防止拼接其他租户的 Key/模型/Provider。
    with pytest.raises(sqlite3.IntegrityError, match="stream_session_virtual_key_tenant_mismatch"):
        connection.execute(
            """
            INSERT INTO stream_sessions(
              stream_session_id, tenant_id, request_id, trace_id, virtual_key_id,
              environment_id, environment_code, model_id, provider_id, provider_code,
              status, inter_token_timeout_ms, started_at, created_at, updated_at
            )
            SELECT
              'str_prd037_cross_tenant', 'tenant_prd037_other', 'req_prd037_cross_tenant',
              'trc_prd037_cross_tenant', virtual_key_id, environment_id, environment_code,
              model_id, provider_id, provider_code, 'starting', 30000,
              started_at, created_at, updated_at
            FROM stream_sessions
            WHERE stream_session_id='str_prd037_timeout'
            """
        )

    # 正常路径只能先 active 再进入终态；终态一旦提交，重复清理不能覆盖首个事实。
    connection.execute(
        """
        UPDATE stream_sessions
        SET status='active', updated_at='2026-07-14T10:00:01Z'
        WHERE stream_session_id='str_prd037_timeout'
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="stream_session_status_transition_invalid"):
        connection.execute(
            """
            UPDATE stream_sessions
            SET status='starting'
            WHERE stream_session_id='str_prd037_timeout'
            """
        )
    connection.execute(
        """
        UPDATE stream_sessions
        SET status='timed_out', chunk_count=5, bytes_sent=2048,
            first_chunk_at='2026-07-14T10:00:02Z', last_chunk_at='2026-07-14T10:00:03Z',
            error_code='gateway.upstream_stream_timeout', completed_at='2026-07-14T10:00:33Z',
            updated_at='2026-07-14T10:00:33Z'
        WHERE stream_session_id='str_prd037_timeout'
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="stream_session_status_transition_invalid"):
        connection.execute(
            """
            UPDATE stream_sessions
            SET status='completed'
            WHERE stream_session_id='str_prd037_timeout'
            """
        )
    insert_test_stream_session(
        connection,
        seed.tenant_id,
        stream_session_id="str_prd037_direct_terminal",
        request_id="req_prd037_direct_terminal",
    )
    with pytest.raises(sqlite3.IntegrityError, match="stream_session_status_transition_invalid"):
        connection.execute(
            """
            UPDATE stream_sessions
            SET status='completed'
            WHERE stream_session_id='str_prd037_direct_terminal'
            """
        )

    # timed_out 只能映射 failed，且会话必须与日志的 tenant_id/request_id 完全一致。
    insert_disabled_request_claim(
        connection,
        seed.tenant_id,
        request_id="req_prd037_timeout",
        trace_id="trc_req_prd037_timeout",
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, status, error_code, stream_session_id, stream_status,
          created_at, updated_at
        )
        VALUES(
          'req_prd037_timeout', ?, 'trc_req_prd037_timeout', 'failed',
          'gateway.upstream_stream_timeout', 'str_prd037_timeout', 'timed_out',
          '2026-07-14T10:00:33Z', '2026-07-14T10:00:33Z'
        )
        """,
        (seed.tenant_id,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="request_log_stream_status_mismatch"):
        connection.execute(
            "UPDATE request_logs SET status='success' WHERE request_id='req_prd037_timeout'"
        )
    insert_disabled_request_claim(
        connection,
        seed.tenant_id,
        request_id="req_prd037_non_stream",
        trace_id="trc_prd037_non_stream",
    )
    connection.execute(
        """
        INSERT INTO request_logs(request_id, tenant_id, trace_id, status, created_at, updated_at)
        VALUES(
          'req_prd037_non_stream', ?, 'trc_prd037_non_stream', 'success',
          '2026-07-14T10:01:00Z', '2026-07-14T10:01:00Z'
        )
        """,
        (seed.tenant_id,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="request_log_stream_fields_incomplete"):
        connection.execute(
            """
            UPDATE request_logs
            SET stream_session_id='str_prd037_timeout'
            WHERE request_id='req_prd037_non_stream'
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="request_log_stream_session_mismatch"):
        connection.execute(
            """
            UPDATE request_logs
            SET status='failed', stream_session_id='str_prd037_timeout', stream_status='timed_out'
            WHERE request_id='req_prd037_non_stream'
            """
        )

    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_python_sdk_event_migration_preserves_old_event_and_filters_new_on_rollback(
    tmp_path: Path,
) -> None:
    """PRD-038 迁移/回滚：升级保留 quickstart，回滚删除 SDK 事件并恢复旧 CHECK。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)

    # 先回滚 PRD-043、PRD-042、PRD-041 和 PRD-040，再回到 0027 模拟 SDK 事件存量升级。
    rollback_policy_engine_migration(database_url)
    rollback_usage_export_migration(database_url)
    webhook_rollback = rollback_last_migration(database_url)
    assert webhook_rollback.rolled_back == "0031"
    competitor_rollback = rollback_last_migration(database_url)
    assert competitor_rollback.rolled_back == "0030"
    guide_rollback = rollback_last_migration(database_url)
    assert guide_rollback.rolled_back == "0029"
    initial_rollback = rollback_last_migration(database_url)
    assert initial_rollback.rolled_back == "0028"
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]
    connection.execute(
        """
        INSERT INTO developer_portal_events(
          event_id, tenant_id, user_id, session_id, event_type, object_id,
          path, request_id, trace_id, payload_json, created_at
        )
        VALUES(
          'dpe_prd038_quickstart', ?, ?, NULL, 'developer.quickstart_viewed', 'quickstart',
          '/#quickstart', 'req_prd038_quickstart', 'trc_prd038_quickstart', '{}',
          '2026-07-15T02:00:00Z'
        )
        """,
        (seed.tenant_id, user_id),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        # 0027 的原始约束必须拒绝尚未迁移的新事件类型。
        connection.execute(
            """
            INSERT INTO developer_portal_events(
              event_id, tenant_id, user_id, event_type, object_id, path,
              request_id, payload_json, created_at
            )
            VALUES(
              'dpe_prd038_rejected', ?, ?, 'sdk.example_downloaded', 'python-openai-example',
              '/#sdk-examples', 'req_prd038_rejected', '{}', '2026-07-15T02:01:00Z'
            )
            """,
            (seed.tenant_id, user_id),
        )
    connection.rollback()

    upgraded = apply_migrations(database_url)
    assert upgraded.applied == [
        "0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040"
    ]
    assert connection.execute(
        "SELECT event_id FROM developer_portal_events WHERE event_id='dpe_prd038_quickstart'"
    ).fetchone() is not None
    connection.execute(
        """
        INSERT INTO developer_portal_events(
          event_id, tenant_id, user_id, session_id, event_type, object_id,
          path, request_id, trace_id, payload_json, created_at
        )
        VALUES(
          'dpe_prd038_download', ?, ?, NULL, 'sdk.example_downloaded', 'python-openai-example',
          '/#sdk-examples', 'req_prd038_download', 'trc_prd038_download',
          '{"language":"python"}', '2026-07-15T02:02:00Z'
        )
        """,
        (seed.tenant_id, user_id),
    )
    connection.commit()

    rollback_policy_engine_migration(database_url)
    rollback_usage_export_migration(database_url)
    webhook_rollback = rollback_last_migration(database_url)
    assert webhook_rollback.rolled_back == "0031"
    competitor_rollback = rollback_last_migration(database_url)
    assert competitor_rollback.rolled_back == "0030"
    guide_rollback = rollback_last_migration(database_url)
    assert guide_rollback.rolled_back == "0029"
    rollback = rollback_last_migration(database_url)
    assert rollback.rolled_back == "0028"
    event_rows = connection.execute(
        "SELECT event_id, event_type FROM developer_portal_events ORDER BY event_id"
    ).fetchall()
    assert [(row["event_id"], row["event_type"]) for row in event_rows] == [
        ("dpe_prd038_quickstart", "developer.quickstart_viewed")
    ]
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='developer_portal_events'"
    ).fetchone()["sql"]
    assert "developer.quickstart_viewed" in table_sql
    assert "sdk.example_downloaded" not in table_sql
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    reapplied = apply_migrations(database_url)
    assert reapplied.applied == [
        "0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040"
    ]
    assert connection.execute(
        "SELECT event_id FROM developer_portal_events WHERE event_id='dpe_prd038_quickstart'"
    ).fetchone() is not None


def test_stream_session_migration_rolls_back_to_0026_and_reapplies(tmp_path: Path) -> None:
    """PRD-037 回滚：0026 尝试数据保留，0027 字段可删除后再次完整上行。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = connection.execute(
        """
        SELECT m.model_id, m.model_alias, p.provider_id, p.provider_code
        FROM models m
        JOIN providers p ON p.provider_id=m.provider_id AND p.tenant_id=m.tenant_id
        WHERE m.tenant_id=?
        ORDER BY m.model_id
        LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert metadata is not None
    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id, attempt_sequence, attempt_role,
          trigger_type, model_id, internal_model_id, provider_id, provider_code, status,
          retryable, usage_status, created_at, updated_at
        )
        VALUES(
          'rta_prd037_preserved', ?, 'req_prd037_preserved', 'trc_prd037_preserved',
          1, 'primary', 'initial', ?, ?, ?, ?, 'started', 0, 'pending',
          '2026-07-14T11:00:00Z', '2026-07-14T11:00:00Z'
        )
        """,
        (
            seed.tenant_id,
            metadata["model_id"],
            metadata["model_alias"],
            metadata["provider_id"],
            metadata["provider_code"],
        ),
    )
    connection.commit()

    # 先回滚 PRD-043、PRD-042、PRD-041、PRD-040 与 SDK 事件约束，再验证流会话回滚。
    rollback_policy_engine_migration(database_url)
    rollback_usage_export_migration(database_url)
    webhook_rollback = rollback_last_migration(database_url)
    assert webhook_rollback.rolled_back == "0031"
    competitor_rollback = rollback_last_migration(database_url)
    assert competitor_rollback.rolled_back == "0030"
    guide_rollback = rollback_last_migration(database_url)
    assert guide_rollback.rolled_back == "0029"
    sdk_rollback = rollback_last_migration(database_url)
    assert sdk_rollback.rolled_back == "0028"
    rollback = rollback_last_migration(database_url)
    assert rollback.rolled_back == "0027"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='stream_sessions'"
    ).fetchone() is None
    request_log_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(request_logs)").fetchall()
    }
    assert "stream_session_id" not in request_log_columns
    assert "stream_status" not in request_log_columns
    assert connection.execute(
        """
        SELECT routing_attempt_id
        FROM routing_attempts
        WHERE routing_attempt_id='rta_prd037_preserved'
        """
    ).fetchone() is not None
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    reapplied = apply_migrations(database_url)
    assert reapplied.applied == [
        "0027", "0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040"
    ]
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='stream_sessions'"
    ).fetchone() is not None
    assert connection.execute(
        """
        SELECT routing_attempt_id
        FROM routing_attempts
        WHERE routing_attempt_id='rta_prd037_preserved'
        """
    ).fetchone() is not None
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_competitor_event_round_trip_preserves_history(tmp_path: Path) -> None:
    """PRD-041 正反迁移保留全部事件和索引，并只降低竞品帮助事件的名称精度。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]
    event_rows = [
        ("cpe_prd041_page", "console.page_viewed", "api-docs"),
        ("cpe_prd041_dashboard", "dashboard.viewed", "overview"),
        ("cpe_prd041_migration", "migration.guide_viewed", "migration-guide"),
        ("cpe_prd041_help", "help.competitor_viewed", "competitor-comparison"),
    ]
    connection.executemany(
        """
        INSERT INTO console_page_events(
          event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
          target_section, path, request_id, trace_id, payload_json, created_at
        )
        VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, '2026-07-15T04:00:00Z')
        """,
        [
            (
                event_id,
                seed.tenant_id,
                user_id,
                event_type,
                navigation_id,
                navigation_id,
                f"/#{navigation_id}",
                f"req_{event_id}",
                f"trc_{event_id}",
                f'{{"hash":"#{navigation_id}","source":"sidebar"}}',
            )
            for event_id, event_type, navigation_id in event_rows
        ],
    )
    connection.commit()

    rollback_policy_engine_migration(database_url)
    rollback_usage_export_migration(database_url)
    webhook_rollback = rollback_last_migration(database_url)
    assert webhook_rollback.rolled_back == "0031"
    rollback = rollback_last_migration(database_url)
    rows = connection.execute(
        """
        SELECT event_id, tenant_id, user_id, event_type, path, request_id, trace_id, payload_json
        FROM console_page_events ORDER BY event_id
        """
    ).fetchall()
    indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='console_page_events'"
        ).fetchall()
    }
    assert rollback.rolled_back == "0030"
    assert len(rows) == 4
    help_event = next(row for row in rows if row["event_id"] == "cpe_prd041_help")
    migration_event = next(row for row in rows if row["event_id"] == "cpe_prd041_migration")
    assert help_event["event_type"] == "console.page_viewed"
    assert migration_event["event_type"] == "migration.guide_viewed"
    assert help_event["tenant_id"] == seed.tenant_id
    assert help_event["user_id"] == user_id
    assert help_event["path"] == "/#competitor-comparison"
    assert help_event["request_id"] == "req_cpe_prd041_help"
    assert help_event["trace_id"] == "trc_cpe_prd041_help"
    assert "#competitor-comparison" in help_event["payload_json"]
    assert {
        "idx_console_page_events_tenant_time",
        "idx_console_page_events_user_time",
        "idx_console_page_events_item_time",
    }.issubset(indexes)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    reapplied = apply_migrations(database_url)
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='console_page_events'"
    ).fetchone()["sql"]
    assert reapplied.applied == [
        "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040"
    ]
    assert "help.competitor_viewed" in table_sql
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("mismatched_owner", ["virtual_key", "model_alias"])
def test_cost_routing_preflight_rejects_historical_environment_owner_drift(
    tmp_path: Path,
    mismatched_owner: str,
) -> None:
    """0035 历史 Decision 的 Key/Alias Environment 漂移必须在结构替换前阻断升级。"""

    database_url = sqlite_url(tmp_path / f"metahub-prd047-preflight-{mismatched_owner}.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0036"
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO environments(
          environment_id, tenant_id, code, name, status, created_at, updated_at
        )
        VALUES(
          'env_prd047_preflight_test', ?, 'test', 'PRD047 Preflight Test',
          'active', '2026-07-18T15:00:00Z', '2026-07-18T15:00:00Z'
        )
        """,
        (seed.tenant_id,),
    )
    virtual_key_id = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? AND environment='dev' LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()[0]
    dev_environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (seed.tenant_id,),
    ).fetchone()[0]
    default_model_id = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? ORDER BY model_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, status, created_at, updated_at
        )
        VALUES(
          'alias_prd047_preflight_dev', ?, 'prd047-preflight-dev', 'dev', ?, ?,
          'active', '2026-07-18T15:00:00Z', '2026-07-18T15:00:00Z'
        )
        """,
        (seed.tenant_id, dev_environment_id, default_model_id),
    )
    alias_id = "alias_prd047_preflight_dev"
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          model_alias_id, requested_model, environment, environment_id,
          decision_status, decision_reason, candidate_summary, created_at
        )
        VALUES(
          ?, ?, ?, ?, ?, ?, 'prd047-preflight', 'test',
          'env_prd047_preflight_test', 'failed', 'preflight.fixture', '{}',
          '2026-07-18T15:00:00Z'
        )
        """,
        (
            f"rtd_prd047_preflight_{mismatched_owner}",
            seed.tenant_id,
            f"req_prd047_preflight_{mismatched_owner}",
            f"trc_prd047_preflight_{mismatched_owner}",
            virtual_key_id if mismatched_owner == "virtual_key" else None,
            alias_id if mismatched_owner == "model_alias" else None,
        ),
    )
    connection.commit()

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url)
    assert exc.value.code == "db.migration_failed"
    # 迁移器保留 failed 状态用于运维诊断，但事务内业务结构必须完整回滚。
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0036'"
    ).fetchone()[0] == "failed"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='model_alias_routing_policies'"
    ).fetchone() is None


def test_cost_routing_preflight_accepts_legacy_key_and_preserves_duplicate_history_round_trip(
    tmp_path: Path,
) -> None:
    """0035 历史兼容：旧 Key 按同租户代码解析，重复 request 在 0036 往返中逐行保真。"""

    database_url = sqlite_url(tmp_path / "metahub-prd047-legacy-history.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0036"
    connection = connect_database(database_url)
    binding = connection.execute(
        """
        SELECT vk.virtual_key_id, e.environment_id
        FROM virtual_keys vk
        JOIN environments e ON e.tenant_id=vk.tenant_id AND e.code=vk.environment
        WHERE vk.tenant_id=? AND vk.environment='dev'
        ORDER BY vk.virtual_key_id
        LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert binding is not None
    # 真实旧库可能保留 environment 代码但缺少 environment_id；Gateway 鉴权会在同租户内按代码解析。
    connection.execute(
        "UPDATE virtual_keys SET environment_id=NULL WHERE tenant_id=? AND virtual_key_id=?",
        (seed.tenant_id, binding["virtual_key_id"]),
    )
    connection.executemany(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          requested_model, environment, environment_id, decision_status,
          decision_reason, candidate_summary, error_code, created_at
        )
        VALUES(?, ?, 'req_prd047_legacy_duplicate', ?, ?, 'legacy-missing-model',
               'dev', ?, 'failed', 'legacy.fixture', '{}', 'gateway.model_not_found', ?)
        """,
        [
            (
                f"rtd_prd047_legacy_duplicate_{sequence}",
                seed.tenant_id,
                f"trc_prd047_legacy_duplicate_{sequence}",
                binding["virtual_key_id"],
                binding["environment_id"],
                f"2026-07-18T15:1{sequence}:00Z",
            )
            for sequence in range(3)
        ],
    )
    connection.commit()
    public_columns = [
        "routing_decision_id",
        "tenant_id",
        "request_id",
        "trace_id",
        "virtual_key_id",
        "model_alias_id",
        "requested_model",
        "environment",
        "selected_model_id",
        "selected_internal_model_id",
        "provider_id",
        "provider_code",
        "decision_status",
        "decision_reason",
        "candidate_summary",
        "error_code",
        "created_at",
        "environment_id",
    ]

    def load_legacy_rows() -> list[tuple[object, ...]]:
        """读取 0035 公共列，确保 up/down 不借兼容修复重写任何历史事实。"""

        return [
            tuple(row)
            for row in connection.execute(
                f"SELECT {', '.join(public_columns)} FROM routing_decisions "
                "WHERE request_id='req_prd047_legacy_duplicate' ORDER BY routing_decision_id"
            ).fetchall()
        ]

    rows_before = load_legacy_rows()
    assert len(rows_before) == 3

    assert apply_migrations(database_url).applied == ["0036", "0037", "0038", "0039", "0040"]
    rollback_quality_cost_migration(database_url)
    assert load_legacy_rows() == rows_before
    assert connection.execute(
        """
        SELECT COUNT(*) FROM routing_decisions
        WHERE tenant_id=? AND request_id='req_prd047_legacy_duplicate'
          AND routing_policy_id IS NULL
        """,
        (seed.tenant_id,),
    ).fetchone()[0] == 3
    index_sql = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type='index' AND name='idx_routing_decisions_policy_request_unique'
        """
    ).fetchone()[0]
    assert "WHERE routing_policy_id IS NOT NULL" in index_sql

    assert rollback_last_migration(database_url).rolled_back == "0036"
    assert load_legacy_rows() == rows_before
    assert connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='index' AND name='idx_routing_decisions_policy_request_unique'
        """
    ).fetchone() is None

    assert apply_migrations(database_url).applied == ["0036", "0037", "0038", "0039", "0040"]
    rollback_quality_cost_migration(database_url)
    assert load_legacy_rows() == rows_before
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("mismatch_kind", ["legacy_code", "legacy_tenant"])
def test_cost_routing_preflight_rejects_legacy_key_code_or_tenant_mismatch(
    tmp_path: Path,
    mismatch_kind: str,
) -> None:
    """安全边界：legacy Key 只能按同 tenant、同环境代码解析，任一错配都必须拒绝升级。"""

    database_url = sqlite_url(tmp_path / f"metahub-prd047-legacy-{mismatch_kind}.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0036"
    connection = connect_database(database_url)
    dev_environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (seed.tenant_id,),
    ).fetchone()[0]
    if mismatch_kind == "legacy_code":
        virtual_key_id = connection.execute(
            """
            SELECT virtual_key_id FROM virtual_keys
            WHERE tenant_id=? AND environment='dev'
            ORDER BY virtual_key_id LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE virtual_keys SET environment='test', environment_id=NULL
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (seed.tenant_id, virtual_key_id),
        )
    else:
        virtual_key_id = "vk_prd047_legacy_other"
        connection.execute(
            """
            INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
            VALUES('tenant_prd047_legacy_other', 'prd047-legacy-other',
                   'PRD047 Legacy Other', 'internal_only', 'active',
                   '2026-07-18T15:20:00Z', '2026-07-18T15:20:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO virtual_keys(
              virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
              created_at, updated_at, environment, environment_id
            )
            VALUES(?, 'tenant_prd047_legacy_other', 'PRD047 Legacy Other Key',
                   'mhk_prd047_other', 'hash_prd047_legacy_other', 'active',
                   '2026-07-18T15:20:00Z', '2026-07-18T15:20:00Z', 'dev', NULL)
            """,
            (virtual_key_id,),
        )
    # 0035 的单列 Virtual Key 外键允许构造该历史坏图，0036 preflight 必须在换表前失败关闭。
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          requested_model, environment, environment_id, decision_status,
          decision_reason, candidate_summary, created_at
        )
        VALUES(?, ?, ?, ?, ?, 'legacy-mismatch', 'dev', ?, 'failed',
               'legacy.mismatch.fixture', '{}', '2026-07-18T15:21:00Z')
        """,
        (
            f"rtd_prd047_{mismatch_kind}",
            seed.tenant_id,
            f"req_prd047_{mismatch_kind}",
            f"trc_prd047_{mismatch_kind}",
            virtual_key_id,
            dev_environment_id,
        ),
    )
    connection.commit()

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url)
    assert exc.value.code == "db.migration_failed"
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0036'"
    ).fetchone()[0] == "failed"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='model_alias_routing_policies'"
    ).fetchone() is None


def test_cost_routing_policy_request_uniqueness_and_legacy_key_owner_triggers(
    tmp_path: Path,
) -> None:
    """0036 写入合同：legacy Key 需通过 owner 图，且仅 Policy-bound Decision 按 request 唯一。"""

    database_url = sqlite_url(tmp_path / "metahub-prd047-policy-request-unique.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    connection = connect_database(database_url)
    metadata = connection.execute(
        """
        SELECT
          vk.virtual_key_id, e.environment_id, m.model_id, u.user_id
        FROM virtual_keys vk
        JOIN environments e
          ON e.tenant_id=vk.tenant_id AND e.code=vk.environment
        JOIN models m ON m.tenant_id=vk.tenant_id
        JOIN users u ON u.tenant_id=vk.tenant_id
        WHERE vk.tenant_id=? AND vk.environment='dev'
        ORDER BY vk.virtual_key_id, m.model_id, u.user_id
        LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert metadata is not None
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, status, created_by, created_at, updated_at
        )
        VALUES('ma_prd047_policy_unique', ?, 'prd047-policy-unique', 'dev', ?, ?,
               'active', ?, '2026-07-18T15:29:00Z', '2026-07-18T15:29:00Z')
        """,
        (
            seed.tenant_id,
            metadata["environment_id"],
            metadata["model_id"],
            metadata["user_id"],
        ),
    )
    binding = connection.execute(
        """
        SELECT
          vk.virtual_key_id, a.model_alias_id, a.alias, a.environment,
          a.environment_id, rp.routing_policy_id, rp.version, rp.routing_mode
        FROM virtual_keys vk
        JOIN model_aliases a
          ON a.tenant_id=vk.tenant_id AND a.environment=vk.environment
        JOIN model_alias_routing_policies rp
          ON rp.tenant_id=a.tenant_id AND rp.model_alias_id=a.model_alias_id
        WHERE vk.tenant_id=? AND vk.environment='dev'
          AND a.model_alias_id='ma_prd047_policy_unique'
        ORDER BY vk.virtual_key_id, a.model_alias_id
        LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert binding is not None
    connection.execute(
        "UPDATE virtual_keys SET environment_id=NULL WHERE tenant_id=? AND virtual_key_id=?",
        (seed.tenant_id, binding["virtual_key_id"]),
    )
    policy_insert_sql = """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          model_alias_id, requested_model, environment, environment_id,
          decision_status, decision_reason, candidate_summary, error_code, created_at,
          routing_policy_id, routing_policy_version, routing_mode
        )
        VALUES(?, ?, 'req_prd047_policy_unique', ?, ?, ?, ?, ?, ?, 'failed',
               'routing.no_eligible_candidate', '{}', 'gateway.no_route_available', ?, ?, ?, ?)
    """
    policy_values = (
        seed.tenant_id,
        binding["virtual_key_id"],
        binding["model_alias_id"],
        binding["alias"],
        binding["environment"],
        binding["environment_id"],
        binding["routing_policy_id"],
        binding["version"],
        binding["routing_mode"],
    )
    connection.execute(
        policy_insert_sql,
        (
            "rtd_prd047_policy_unique_first",
            policy_values[0],
            "trc_prd047_policy_unique_first",
            *policy_values[1:6],
            "2026-07-18T15:30:00Z",
            *policy_values[6:],
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        connection.execute(
            policy_insert_sql,
            (
                "rtd_prd047_policy_unique_second",
                policy_values[0],
                "trc_prd047_policy_unique_second",
                *policy_values[1:6],
                "2026-07-18T15:30:01Z",
                *policy_values[6:],
            ),
        )
    # 同 request 的非 Policy 历史/降级事实仍可落库，不受部分唯一索引误伤。
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          requested_model, environment, environment_id, decision_status,
          decision_reason, candidate_summary, error_code, created_at
        )
        VALUES('rtd_prd047_unbound_duplicate', ?, 'req_prd047_policy_unique',
               'trc_prd047_unbound_duplicate', ?, 'legacy-unbound', 'dev', ?, 'failed',
               'legacy.unbound.fixture', '{}', 'gateway.model_not_found',
               '2026-07-18T15:30:02Z')
        """,
        (seed.tenant_id, binding["virtual_key_id"], binding["environment_id"]),
    )
    # UPDATE OF 会触发 owner 校验；同租户 legacy code 解析应通过，而改绑其他代码必须失败。
    connection.execute(
        """
        UPDATE routing_decisions SET environment=environment, environment_id=environment_id
        WHERE routing_decision_id='rtd_prd047_unbound_duplicate'
        """
    )
    connection.execute(
        """
        INSERT INTO environments(
          environment_id, tenant_id, code, name, status, created_at, updated_at
        )
        VALUES('env_prd047_policy_unique_test', ?, 'test', 'PRD047 Policy Unique Test',
               'active', '2026-07-18T15:30:03Z', '2026-07-18T15:30:03Z')
        """,
        (seed.tenant_id,),
    )
    test_environment_id = "env_prd047_policy_unique_test"
    with pytest.raises(sqlite3.IntegrityError, match="routing_decision_owner_graph_invalid"):
        connection.execute(
            """
            UPDATE routing_decisions SET environment='test', environment_id=?
            WHERE routing_decision_id='rtd_prd047_unbound_duplicate'
            """,
            (test_environment_id,),
        )
    assert connection.execute(
        """
        SELECT COUNT(*) FROM routing_decisions
        WHERE tenant_id=? AND request_id='req_prd047_policy_unique'
        """,
        (seed.tenant_id,),
    ).fetchone()[0] == 2
    connection.rollback()


def test_cost_availability_routing_migration_enforces_owner_and_candidate_graph(
    tmp_path: Path,
) -> None:
    """PRD-047 正向迁移：Policy 自动版本、复合归属与 Candidate 角色图均由 DB 兜底。"""

    database_url = sqlite_url(tmp_path / "metahub-prd047.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    connection = connect_database(database_url)
    actor = connection.execute(
        "SELECT user_id, email, role FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    metadata = connection.execute(
        """
        SELECT
          e.environment_id, p.provider_id, p.provider_code,
          m.model_id, vk.virtual_key_id
        FROM environments e
        JOIN providers p ON p.tenant_id=e.tenant_id
        JOIN models m ON m.tenant_id=p.tenant_id AND m.provider_id=p.provider_id
        JOIN virtual_keys vk ON vk.tenant_id=e.tenant_id AND vk.environment_id=e.environment_id
        WHERE e.tenant_id=? AND e.code='dev'
        ORDER BY m.model_id
        LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert actor is not None
    assert metadata is not None
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd047_fallback', ?, ?, 'prd047-fallback', 'prd047-fallback-upstream',
          '{}', 'active', '2026-07-18T13:00:00Z', '2026-07-18T13:00:00Z'
        )
        """,
        (seed.tenant_id, metadata["provider_id"]),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, status, created_by, created_at, updated_at
        )
        VALUES(
          'ma_prd047_contract', ?, 'prd047-contract', 'dev', ?, ?, 'active', ?,
          '2026-07-18T13:00:00Z', '2026-07-18T13:00:00Z'
        )
        """,
        (
            seed.tenant_id,
            metadata["environment_id"],
            metadata["model_id"],
            actor["user_id"],
        ),
    )
    policy = connection.execute(
        """
        SELECT routing_policy_id, routing_mode, version
        FROM model_alias_routing_policies
        WHERE tenant_id=? AND model_alias_id='ma_prd047_contract'
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert policy is not None
    assert (policy["routing_mode"], policy["version"]) == ("fixed", 1)

    # 一条多字段 Alias UPDATE 只能推进一次版本；同值 SQL 不得制造无意义版本。
    connection.execute(
        """
        UPDATE model_aliases
        SET alias='prd047-contract-v2', fallback_model_id='model_prd047_fallback',
            updated_at='2026-07-18T13:01:00Z'
        WHERE tenant_id=? AND model_alias_id='ma_prd047_contract'
        """,
        (seed.tenant_id,),
    )
    assert connection.execute(
        "SELECT version FROM model_alias_routing_policies WHERE routing_policy_id=?",
        (policy["routing_policy_id"],),
    ).fetchone()[0] == 2
    connection.execute(
        """
        UPDATE model_aliases
        SET alias=alias, fallback_model_id=fallback_model_id
        WHERE tenant_id=? AND model_alias_id='ma_prd047_contract'
        """,
        (seed.tenant_id,),
    )
    assert connection.execute(
        "SELECT version FROM model_alias_routing_policies WHERE routing_policy_id=?",
        (policy["routing_policy_id"],),
    ).fetchone()[0] == 2
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode='cost_first', version=3, updated_by=?,
            updated_at='2026-07-18T13:02:00Z'
        WHERE tenant_id=? AND routing_policy_id=?
        """,
        (actor["user_id"], seed.tenant_id, policy["routing_policy_id"]),
    )

    # 第二租户用于直接 SQL 反例；默认 Environment trigger 会自动建立该租户 dev。
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_prd047_other', 'prd047-other', 'PRD047 Other',
               'internal_only', 'active', '2026-07-18T13:00:00Z', '2026-07-18T13:00:00Z')
        """
    )
    other_environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id='tenant_prd047_other' AND code='dev'"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="models.provider_owner_invalid"):
        connection.execute(
            """
            INSERT INTO models(
              model_id, tenant_id, provider_id, model_alias, upstream_model_name,
              capabilities, status, created_at, updated_at
            )
            VALUES('model_prd047_cross', 'tenant_prd047_other', ?, 'cross', 'cross',
                   '{}', 'active', datetime('now'), datetime('now'))
            """,
            (metadata["provider_id"],),
        )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, created_at, updated_at
        )
        VALUES(
          'provider_prd047_other', 'tenant_prd047_other', 'prd047_other',
          'PRD047 Other Provider', 'http://127.0.0.1:9/v1', 'active',
          'openai_compatible', 'none', datetime('now'), datetime('now')
        )
        """
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd047_other', 'tenant_prd047_other', 'provider_prd047_other',
          'prd047-other', 'prd047-other', '{}', 'active', datetime('now'), datetime('now')
        )
        """
    )
    # Model UPDATE 也必须重新核对 Provider tenant，不能只保护新建路径。
    with pytest.raises(sqlite3.IntegrityError, match="models.provider_owner_invalid"):
        connection.execute(
            """
            UPDATE models SET provider_id='provider_prd047_other'
            WHERE tenant_id=? AND model_id='model_prd047_fallback'
            """,
            (seed.tenant_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="model_aliases.owner_graph_invalid"):
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id, tenant_id, alias, environment, environment_id,
              default_model_id, status, created_at, updated_at
            )
            VALUES('ma_prd047_cross', 'tenant_prd047_other', 'cross', 'dev', ?, ?,
                   'active', datetime('now'), datetime('now'))
            """,
            (other_environment_id, metadata["model_id"]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="model_aliases.owner_graph_invalid"):
        connection.execute(
            """
            UPDATE model_aliases
            SET fallback_model_id=default_model_id
            WHERE tenant_id=? AND model_alias_id='ma_prd047_contract'
            """,
            (seed.tenant_id,),
        )
    # Alias UPDATE 的 Environment 与 Model 两条 owner 边均需独立拒绝跨租户父对象。
    with pytest.raises(sqlite3.IntegrityError, match="model_aliases.owner_graph_invalid"):
        connection.execute(
            """
            UPDATE model_aliases SET environment_id=?
            WHERE tenant_id=? AND model_alias_id='ma_prd047_contract'
            """,
            (other_environment_id, seed.tenant_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="model_aliases.owner_graph_invalid"):
        connection.execute(
            """
            UPDATE model_aliases SET default_model_id='model_prd047_other'
            WHERE tenant_id=? AND model_alias_id='ma_prd047_contract'
            """,
            (seed.tenant_id,),
        )

    # Decision INSERT 不能把当前租户 VirtualKey 绑定到另一租户 Environment。
    with pytest.raises(sqlite3.IntegrityError, match="routing_decision_owner_graph_invalid"):
        connection.execute(
            """
            INSERT INTO routing_decisions(
              routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
              requested_model, environment, environment_id, decision_status,
              decision_reason, candidate_summary, created_at
            )
            VALUES(
              'rtd_prd047_cross_insert', ?, 'req_prd047_cross_insert',
              'trc_prd047_cross_insert', ?, 'cross', 'dev', ?, 'failed',
              'cross.fixture', '{}', '2026-07-18T13:02:00Z'
            )
            """,
            (seed.tenant_id, metadata["virtual_key_id"], other_environment_id),
        )

    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          model_alias_id, requested_model, environment, environment_id,
          selected_model_id, selected_internal_model_id, provider_id, provider_code,
          decision_status, decision_reason, candidate_summary, created_at,
          routing_policy_id, routing_policy_version, routing_mode, routing_policy_event_id
        )
        VALUES(
          'rtd_prd047_contract', ?, 'req_prd047_contract', 'trc_prd047_contract', ?,
          'ma_prd047_contract', 'prd047-contract-v2', 'dev', ?, ?, 'default-chat', ?, ?,
          'selected', 'routing.single_eligible_candidate', '{}', '2026-07-18T13:03:00Z',
          ?, 3, 'cost_first', 'rpe_22222222222222222222222222222222'
        )
        """,
        (
            seed.tenant_id,
            metadata["virtual_key_id"],
            metadata["environment_id"],
            metadata["model_id"],
            metadata["provider_id"],
            metadata["provider_code"],
            policy["routing_policy_id"],
        ),
    )
    # Candidate 尚未写入时 owner UPDATE trigger 仍需独立拒绝跨租户 Environment。
    with pytest.raises(sqlite3.IntegrityError, match="routing_decision_owner_graph_invalid"):
        connection.execute(
            """
            UPDATE routing_decisions
            SET environment_id=?
            WHERE routing_decision_id='rtd_prd047_contract'
            """,
            (other_environment_id,),
        )
    connection.execute(
        """
        INSERT INTO routing_candidates(
          routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
          model_alias_id, model_id, provider_id, candidate_role, configured_priority,
          rank, status, reason_code, health_state, health_fresh, keypool_state,
          price_available, price_source, created_at, updated_at
        )
        VALUES(
          'rcand_22222222222222222222222222222222', ?, 'rtd_prd047_contract', ?,
          'ma_prd047_contract', ?, ?, 'default', 0, 1, 'selected',
          'routing.single_eligible_candidate', 'healthy', 1, 'available',
          0, 'not_evaluated', '2026-07-18T13:03:00Z', '2026-07-18T13:03:00Z'
        )
        """,
        (
            seed.tenant_id,
            policy["routing_policy_id"],
            metadata["model_id"],
            metadata["provider_id"],
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            """
            INSERT INTO routing_candidates(
              routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
              model_alias_id, model_id, provider_id, candidate_role, configured_priority,
              status, reason_code, health_state, health_fresh, keypool_state,
              price_available, price_source, created_at, updated_at
            )
            VALUES(
              'rcand_33333333333333333333333333333333', ?, 'rtd_prd047_contract', ?,
              'ma_prd047_contract', 'model_prd047_fallback', ?, 'fallback', 0,
              'rejected', 'routing.candidate.provider_unhealthy', 'unhealthy', 1,
              'not_evaluated', 0, 'not_evaluated',
              '2026-07-18T13:03:00Z', '2026-07-18T13:03:00Z'
            )
            """,
            (seed.tenant_id, policy["routing_policy_id"], metadata["provider_id"]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="routing_candidate_graph_invalid"):
        connection.execute(
            """
            INSERT INTO routing_candidates(
              routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
              model_alias_id, model_id, provider_id, candidate_role, configured_priority,
              status, reason_code, health_state, health_fresh, keypool_state,
              price_available, price_source, created_at, updated_at
            )
            VALUES(
              'rcand_44444444444444444444444444444444', ?, 'rtd_prd047_contract', ?,
              'ma_prd047_contract', 'model_prd047_fallback', ?, 'default', 0,
              'rejected', 'routing.candidate.provider_unhealthy', 'unhealthy', 1,
              'not_evaluated', 0, 'not_evaluated',
              '2026-07-18T13:03:00Z', '2026-07-18T13:03:00Z'
            )
            """,
            (seed.tenant_id, policy["routing_policy_id"], metadata["provider_id"]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            """
            INSERT INTO routing_candidates(
              routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
              model_alias_id, model_id, provider_id, candidate_role, configured_priority,
              status, reason_code, health_state, health_fresh, keypool_state,
              price_available, price_source, created_at, updated_at
            )
            VALUES(
              'rcand_77777777777777777777777777777777', ?, 'rtd_prd047_contract', ?,
              'ma_prd047_contract', ?, ?, 'default', 1, 'rejected',
              'routing.candidate.provider_unhealthy', 'unhealthy', 1,
              'not_evaluated', 0, 'not_evaluated',
              '2026-07-18T13:03:00Z', '2026-07-18T13:03:00Z'
            )
            """,
            (
                seed.tenant_id,
                policy["routing_policy_id"],
                metadata["model_id"],
                metadata["provider_id"],
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="routing_candidate_graph_invalid"):
        connection.execute(
            """
            INSERT INTO routing_candidates(
              routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
              model_alias_id, model_id, provider_id, candidate_role, configured_priority,
              status, reason_code, health_state, health_fresh, keypool_state,
              price_available, price_source, created_at, updated_at
            )
            VALUES(
              'rcand_88888888888888888888888888888888', ?, 'rtd_prd047_contract', ?,
              'ma_prd047_contract', ?, ?, 'fallback', 1, 'rejected',
              'routing.candidate.provider_unhealthy', 'unhealthy', 1,
              'not_evaluated', 0, 'not_evaluated',
              '2026-07-18T13:03:00Z', '2026-07-18T13:03:00Z'
            )
            """,
            (
                seed.tenant_id,
                policy["routing_policy_id"],
                metadata["model_id"],
                metadata["provider_id"],
            ),
        )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, status, created_by, created_at, updated_at
        )
        VALUES(
          'ma_prd047_no_fallback', ?, 'prd047-no-fallback', 'dev', ?, ?,
          'active', ?, '2026-07-18T13:03:00Z', '2026-07-18T13:03:00Z'
        )
        """,
        (
            seed.tenant_id,
            metadata["environment_id"],
            metadata["model_id"],
            actor["user_id"],
        ),
    )
    no_fallback_policy_id = connection.execute(
        """
        SELECT routing_policy_id FROM model_alias_routing_policies
        WHERE tenant_id=? AND model_alias_id='ma_prd047_no_fallback'
        """,
        (seed.tenant_id,),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          model_alias_id, requested_model, environment, environment_id,
          selected_model_id, selected_internal_model_id, provider_id, provider_code,
          decision_status, decision_reason, candidate_summary, created_at,
          routing_policy_id, routing_policy_version, routing_mode, routing_policy_event_id
        )
        VALUES(
          'rtd_prd047_no_fallback', ?, 'req_prd047_no_fallback',
          'trc_prd047_no_fallback', ?, 'ma_prd047_no_fallback',
          'prd047-no-fallback', 'dev', ?, ?, 'default-chat', ?, ?,
          'selected', 'alias.default', '{}', '2026-07-18T13:03:00Z',
          ?, 1, 'fixed', 'rpe_99999999999999999999999999999999'
        )
        """,
        (
            seed.tenant_id,
            metadata["virtual_key_id"],
            metadata["environment_id"],
            metadata["model_id"],
            metadata["provider_id"],
            metadata["provider_code"],
            no_fallback_policy_id,
        ),
    )
    # Alias 没有 fallback 目标时，任何 fallback Candidate 都必须被图约束拒绝。
    with pytest.raises(sqlite3.IntegrityError, match="routing_candidate_graph_invalid"):
        connection.execute(
            """
            INSERT INTO routing_candidates(
              routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
              model_alias_id, model_id, provider_id, candidate_role, configured_priority,
              status, reason_code, health_state, health_fresh, keypool_state,
              price_available, price_source, created_at, updated_at
            )
            VALUES(
              'rcand_99999999999999999999999999999999', ?, 'rtd_prd047_no_fallback', ?,
              'ma_prd047_no_fallback', 'model_prd047_fallback', ?, 'fallback', 1,
              'rejected', 'routing.candidate.model_disabled', 'unknown', 0,
              'not_evaluated', 0, 'not_evaluated',
              '2026-07-18T13:03:00Z', '2026-07-18T13:03:00Z'
            )
            """,
            (seed.tenant_id, no_fallback_policy_id, metadata["provider_id"]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="routing_candidate_immutable"):
        connection.execute(
            "UPDATE routing_candidates SET reason_code='routing.fixed_priority' "
            "WHERE routing_candidate_id='rcand_22222222222222222222222222222222'"
        )
    # Decision/Candidate 是同一冻结解释；已有 Candidate 后不能把 selected 改成 failed，
    # 也不能改选同租户内另一模型后留下旧 selected Candidate。
    with pytest.raises(sqlite3.IntegrityError, match="routing_decision_candidate_bound"):
        connection.execute(
            """
            UPDATE routing_decisions
            SET decision_status='failed', selected_model_id=NULL, provider_id=NULL,
                routing_policy_event_id=NULL
            WHERE routing_decision_id='rtd_prd047_contract'
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="routing_decision_candidate_bound"):
        connection.execute(
            """
            UPDATE routing_decisions
            SET selected_model_id='model_prd047_fallback'
            WHERE routing_decision_id='rtd_prd047_contract'
            """
        )
    # reason_code 会由 RequestLog 强类型返回，任意 routing.* 文本也不能绕过精确白名单。
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            """
            INSERT INTO routing_candidates(
              routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
              model_alias_id, model_id, provider_id, candidate_role, configured_priority,
              rank, status, reason_code, health_state, health_fresh, keypool_state,
              price_available, price_source, created_at, updated_at
            )
            VALUES(
              'rcand_55555555555555555555555555555555', ?, 'rtd_prd047_contract', ?,
              'ma_prd047_contract', 'model_prd047_fallback', ?, 'fallback', 1,
              2, 'standby', 'routing.private free text', 'healthy', 1,
              'not_evaluated', 0, 'not_evaluated',
              '2026-07-18T13:03:00Z', '2026-07-18T13:03:00Z'
            )
            """,
            (seed.tenant_id, policy["routing_policy_id"], metadata["provider_id"]),
        )
    # 直接 SQL 若在首条 Candidate 后推进 Policy，不能再向旧版本 Decision 混入第二条候选。
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode='availability_first', version=4,
            updated_at='2026-07-18T13:04:00Z'
        WHERE tenant_id=? AND routing_policy_id=?
        """,
        (seed.tenant_id, policy["routing_policy_id"]),
    )
    with pytest.raises(sqlite3.IntegrityError, match="routing_candidate_graph_invalid"):
        connection.execute(
            """
            INSERT INTO routing_candidates(
              routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
              model_alias_id, model_id, provider_id, candidate_role, configured_priority,
              rank, status, reason_code, health_state, health_fresh, keypool_state,
              price_available, price_source, created_at, updated_at
            )
            VALUES(
              'rcand_66666666666666666666666666666666', ?, 'rtd_prd047_contract', ?,
              'ma_prd047_contract', 'model_prd047_fallback', ?, 'fallback', 1,
              2, 'standby', 'routing.candidate.standby', 'healthy', 1,
              'available', 0, 'not_evaluated',
              '2026-07-18T13:04:00Z', '2026-07-18T13:04:00Z'
            )
            """,
            (seed.tenant_id, policy["routing_policy_id"], metadata["provider_id"]),
        )
    connection.rollback()

    assert _foreign_key_column_pairs(
        connection.execute("PRAGMA foreign_key_list(routing_candidates)").fetchall(),
        "routing_decisions",
    ) == {(('tenant_id', 'tenant_id'), ('routing_decision_id', 'routing_decision_id'))}
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_routing_policy_event_payload_arrays_enforce_canonical_contract_and_rollback(
    tmp_path: Path,
) -> None:
    """PRD-047 事件数组只接受应用白名单与规范序，且新增触发器随 0036 完整回滚。"""

    database_url = sqlite_url(tmp_path / "metahub-prd047-event-payload.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    connection = connect_database(database_url)
    actor_user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()[0]
    environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (seed.tenant_id,),
    ).fetchone()[0]
    model_id = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status, description, created_by,
          created_at, updated_at
        ) VALUES(
          'ma_prd047_event_payload', ?, 'prd047-event-payload', 'dev', ?, ?, NULL,
          'active', 'PRD047 event payload contract', ?,
          '2026-07-18T15:00:00Z', '2026-07-18T15:00:00Z'
        )
        """,
        (seed.tenant_id, environment_id, model_id, actor_user_id),
    )
    policy = connection.execute(
        """
        SELECT routing_policy_id, version, routing_mode
        FROM model_alias_routing_policies
        WHERE tenant_id=? AND model_alias_id='ma_prd047_event_payload'
        """,
        (seed.tenant_id,),
    ).fetchone()
    connection.commit()

    def event_payload(
        request_id: str,
        *,
        version: int,
        change_kinds: list[object],
        changed_fields: list[object],
        previous_mode: str | None,
        routing_mode: str,
    ) -> dict[str, object]:
        """按应用九字段形状构造事件正文，调用方只替换本用例关注的数组向量。"""

        return {
            "trigger": "configuration_changed",
            "request_id": request_id,
            "model_alias_id": "ma_prd047_event_payload",
            "routing_policy_id": str(policy["routing_policy_id"]),
            "routing_policy_version": version,
            "change_kinds": change_kinds,
            "changed_routing_fields": changed_fields,
            "previous_mode": previous_mode,
            "routing_mode": routing_mode,
        }

    def insert_event(index: int, payload: dict[str, object], *, version: int) -> None:
        """写入一条同租户配置事件；ID、指纹和列值均从安全测试向量确定生成。"""

        connection.execute(
            """
            INSERT INTO routing_policy_events(
              routing_policy_event_id, tenant_id, routing_policy_id, model_alias_id,
              routing_policy_version, event_type, trigger_type, actor_user_id,
              request_id, trace_id, request_fingerprint, payload_json, created_at
            ) VALUES(
              ?, ?, ?, 'ma_prd047_event_payload', ?, 'route.policy_selected',
              'configuration_changed', ?, ?, ?, ?, ?, '2026-07-18T15:01:00Z'
            )
            """,
            (
                f"rpe_{index:032x}",
                seed.tenant_id,
                policy["routing_policy_id"],
                version,
                actor_user_id,
                payload["request_id"],
                f"trc_prd047_event_payload_{index}",
                f"{index:064x}",
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )

    initial_fields = [
        "alias",
        "environment",
        "environment_id",
        "status",
        "default_model_id",
        "fallback_model_id",
        "routing_mode",
    ]
    invalid_arrays: list[tuple[str, list[object]]] = [
        ("change_kinds", ["unknown"]),
        ("change_kinds", [1]),
        ("change_kinds", ["mode", "mode"]),
        ("change_kinds", ["alias_state", "mode"]),
        ("change_kinds", ["created", "mode"]),
        ("changed_routing_fields", ["unknown"]),
        ("changed_routing_fields", [1]),
        ("changed_routing_fields", ["alias", "alias"]),
        ("changed_routing_fields", ["routing_mode", "alias"]),
    ]
    for index, (payload_field, invalid_value) in enumerate(invalid_arrays, start=1):
        request_id = f"req_prd047_event_payload_invalid_{index}"
        payload = event_payload(
            request_id,
            version=1,
            change_kinds=["mode"],
            changed_fields=["routing_mode"],
            previous_mode="fixed",
            routing_mode="cost_first",
        )
        payload[payload_field] = invalid_value
        with pytest.raises(
            sqlite3.IntegrityError,
            match="routing_policy_event_payload_invalid",
        ):
            insert_event(index, payload, version=1)
        connection.rollback()

    invalid_semantics = [
        event_payload(
            "req_prd047_event_created_previous",
            version=1,
            change_kinds=["created"],
            changed_fields=initial_fields,
            previous_mode="fixed",
            routing_mode="fixed",
        ),
        event_payload(
            "req_prd047_event_created_fields",
            version=1,
            change_kinds=["created"],
            changed_fields=initial_fields[:-1],
            previous_mode=None,
            routing_mode="fixed",
        ),
        event_payload(
            "req_prd047_event_update_previous",
            version=1,
            change_kinds=["candidates"],
            changed_fields=["fallback_model_id"],
            previous_mode=None,
            routing_mode="fixed",
        ),
        event_payload(
            "req_prd047_event_mode_field_missing",
            version=1,
            change_kinds=["mode", "alias_state"],
            changed_fields=["status"],
            previous_mode="fixed",
            routing_mode="cost_first",
        ),
        event_payload(
            "req_prd047_event_mode_kind_missing",
            version=1,
            change_kinds=["alias_state"],
            changed_fields=["status", "routing_mode"],
            previous_mode="fixed",
            routing_mode="cost_first",
        ),
        event_payload(
            "req_prd047_event_mode_noop",
            version=1,
            change_kinds=["mode"],
            changed_fields=["routing_mode"],
            previous_mode="fixed",
            routing_mode="fixed",
        ),
        event_payload(
            "req_prd047_event_non_mode_drift",
            version=1,
            change_kinds=["candidates"],
            changed_fields=["fallback_model_id"],
            previous_mode="fixed",
            routing_mode="cost_first",
        ),
        event_payload(
            "req_prd047_event_candidates_missing",
            version=1,
            change_kinds=["alias_state"],
            changed_fields=["status", "fallback_model_id"],
            previous_mode="fixed",
            routing_mode="fixed",
        ),
        event_payload(
            "req_prd047_event_candidates_spurious",
            version=1,
            change_kinds=["candidates", "alias_state"],
            changed_fields=["status"],
            previous_mode="fixed",
            routing_mode="fixed",
        ),
        event_payload(
            "req_prd047_event_alias_state_missing",
            version=1,
            change_kinds=["candidates"],
            changed_fields=["status", "fallback_model_id"],
            previous_mode="fixed",
            routing_mode="fixed",
        ),
        event_payload(
            "req_prd047_event_alias_state_spurious",
            version=1,
            change_kinds=["candidates", "alias_state"],
            changed_fields=["fallback_model_id"],
            previous_mode="fixed",
            routing_mode="fixed",
        ),
    ]
    for index, payload in enumerate(invalid_semantics, start=20):
        with pytest.raises(
            sqlite3.IntegrityError,
            match="routing_policy_event_payload_invalid",
        ):
            insert_event(index, payload, version=1)
        connection.rollback()

    # created 即使其他字段完整，也不能伪造为当前 Policy 的第二版初始事实。
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET version=2, updated_at='2026-07-18T15:01:30Z'
        WHERE tenant_id=? AND routing_policy_id=?
        """,
        (seed.tenant_id, policy["routing_policy_id"]),
    )
    invalid_created_version = event_payload(
        "req_prd047_event_created_version",
        version=2,
        change_kinds=["created"],
        changed_fields=initial_fields,
        previous_mode=None,
        routing_mode="fixed",
    )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="routing_policy_event_payload_invalid",
    ):
        insert_event(40, invalid_created_version, version=2)
    connection.rollback()

    created_request_id = "req_prd047_event_payload_created"
    created_payload = event_payload(
        created_request_id,
        version=1,
        change_kinds=["created"],
        changed_fields=initial_fields,
        previous_mode=None,
        routing_mode="fixed",
    )
    insert_event(100, created_payload, version=1)
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode='cost_first', version=2, updated_at='2026-07-18T15:02:00Z'
        WHERE tenant_id=? AND routing_policy_id=?
        """,
        (seed.tenant_id, policy["routing_policy_id"]),
    )
    combined_request_id = "req_prd047_event_payload_combined"
    combined_payload = event_payload(
        combined_request_id,
        version=2,
        change_kinds=["mode", "candidates", "alias_state"],
        changed_fields=[
            "alias",
            "status",
            "fallback_model_id",
            "routing_mode",
        ],
        previous_mode="fixed",
        routing_mode="cost_first",
    )
    insert_event(101, combined_payload, version=2)
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET version=3, updated_at='2026-07-18T15:03:00Z'
        WHERE tenant_id=? AND routing_policy_id=?
        """,
        (seed.tenant_id, policy["routing_policy_id"]),
    )
    candidates_payload = event_payload(
        "req_prd047_event_payload_candidates",
        version=3,
        change_kinds=["candidates"],
        changed_fields=["fallback_model_id"],
        previous_mode="cost_first",
        routing_mode="cost_first",
    )
    insert_event(102, candidates_payload, version=3)
    connection.commit()

    stored_payloads = [
        json.loads(row["payload_json"])
        for row in connection.execute(
            """
            SELECT payload_json FROM routing_policy_events
            WHERE model_alias_id='ma_prd047_event_payload'
            ORDER BY routing_policy_version
            """
        ).fetchall()
    ]
    assert stored_payloads[0]["change_kinds"] == ["created"]
    assert stored_payloads[1]["change_kinds"] == [
        "mode",
        "candidates",
        "alias_state",
    ]
    assert stored_payloads[1]["changed_routing_fields"] == [
        "alias",
        "status",
        "fallback_model_id",
        "routing_mode",
    ]
    assert stored_payloads[2]["change_kinds"] == ["candidates"]
    assert stored_payloads[2]["previous_mode"] == stored_payloads[2]["routing_mode"]
    with pytest.raises(sqlite3.IntegrityError, match="routing_policy_event_immutable"):
        connection.execute(
            """
            UPDATE routing_policy_events SET payload_json=?
            WHERE request_id=?
            """,
            (
                json.dumps(
                    {**combined_payload, "change_kinds": ["mode", "mode"]},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                combined_request_id,
            ),
        )
    connection.rollback()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()

    rolled_back = rollback_last_migration(database_url)
    assert rolled_back.rolled_back == "0036"
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='routing_policy_events'"
    ).fetchone() is None
    assert connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='trigger' AND name='trg_routing_policy_events_prd047_payload_insert'
        """
    ).fetchone() is None


def test_cost_availability_routing_round_trip_cleans_events_and_preserves_0035(
    tmp_path: Path,
) -> None:
    """PRD-047 往返：down 仅删除新事件/审计/扩展字段，保留旧 Audit、Decision 与订阅顺序。"""

    database_url = sqlite_url(tmp_path / "metahub-prd047-roundtrip.db")
    apply_migrations(database_url)
    rollback_quality_cost_migration(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = connection.execute(
        "SELECT user_id, email, role FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (seed.tenant_id,),
    ).fetchone()[0]
    virtual_key_id = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? AND environment_id=? LIMIT 1",
        (seed.tenant_id, environment_id),
    ).fetchone()[0]
    connection.executemany(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, object_name,
          before_summary_json, after_summary_json, changed_fields_json,
          client_ip, user_agent, request_id, trace_id, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', '[]', ?, ?, ?, ?,
               '2026-07-18T14:00:00Z')
        """,
        [
            (
                "aud_prd047_old_preserved",
                seed.tenant_id,
                actor["user_id"],
                actor["email"],
                actor["role"],
                "policy.dry_run_executed",
                "policy_explanation",
                "pexp_prd047_preserved",
                "PRD047 preserved object",
                "127.0.0.47",
                "prd047-roundtrip-agent/1",
                "req_prd047_old_audit",
                "trc_prd047_old_audit",
            ),
            (
                "aud_prd047_removed",
                seed.tenant_id,
                actor["user_id"],
                actor["email"],
                actor["role"],
                "routing_policy.updated",
                "routing_policy",
                "rpol_prd047_removed",
                "PRD047 removed object",
                "127.0.0.48",
                "prd047-roundtrip-agent/2",
                "req_prd047_removed_audit",
                "trc_prd047_removed_audit",
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
          max_attempts, created_by, updated_by, created_at, updated_at
        )
        VALUES(
          'wh_prd047_roundtrip', ?, 'PRD047', 'https://example.invalid/hook',
          '["gateway.request_failed","route.policy_selected","audit.recorded"]',
          'ciphertext-only', 'whsec_safe', 'fingerprint-prd047', 'active', 5000, 3,
          ?, ?, '2026-07-18T14:00:00Z', '2026-07-18T14:00:00Z'
        )
        """,
        (seed.tenant_id, actor["user_id"], actor["user_id"]),
    )
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
          max_attempts, created_by, updated_by, created_at, updated_at
        )
        VALUES(
          'wh_prd047_only_route', ?, 'PRD047 only route',
          'https://example.invalid/only-route', '["route.policy_selected"]',
          'ciphertext-only', 'whsec_safe', 'fingerprint-prd047-only', 'active', 5000, 3,
          ?, ?, '2026-07-18T14:00:00Z', '2026-07-18T14:00:00Z'
        )
        """,
        (seed.tenant_id, actor["user_id"], actor["user_id"]),
    )
    connection.executemany(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
          event_timestamp, payload_json, status, attempt_count, max_attempts,
          delivery_kind, replayed_from_delivery_id, replay_idempotency_key,
          replayed_by, replayed_at, secret_version, created_at, updated_at
        )
        VALUES(?, ?, 'wh_prd047_roundtrip', ?, ?, '2026-07-18T14:00:00Z', '{}',
               'success', 1, 3, ?, ?, ?, ?, ?, 1,
               '2026-07-18T14:00:00Z', '2026-07-18T14:00:01Z')
        """,
        [
            (
                "whd_00000000000000000000000000470001",
                seed.tenant_id,
                "rpe_55555555555555555555555555555555",
                "route.policy_selected",
                "original",
                None,
                None,
                None,
                None,
            ),
            (
                "whd_00000000000000000000000000470002",
                seed.tenant_id,
                "rpe_55555555555555555555555555555555",
                "route.policy_selected",
                "replay",
                "whd_00000000000000000000000000470001",
                "idem-prd047",
                actor["user_id"],
                "2026-07-18T14:01:00Z",
            ),
            (
                "whd_00000000000000000000000000470003",
                seed.tenant_id,
                "evt_prd047_preserved",
                "gateway.request_failed",
                "original",
                None,
                None,
                None,
                None,
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO webhook_delivery_attempts(
          webhook_delivery_attempt_id, tenant_id, webhook_delivery_id,
          webhook_endpoint_id, attempt_no, status, http_status, duration_ms, created_at
        )
        VALUES('wha_prd047_removed', ?, 'whd_00000000000000000000000000470002',
               'wh_prd047_roundtrip', 1, 'success', 200, 4, '2026-07-18T14:01:01Z')
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          requested_model, environment, environment_id, decision_status,
          decision_reason, candidate_summary, error_code, created_at
        )
        VALUES(
          'rtd_prd047_roundtrip', ?, 'req_prd047_roundtrip', 'trc_prd047_roundtrip', ?,
          'missing-model', 'dev', ?, 'failed', 'direct_model.not_found', '{}',
          'gateway.model_not_found', '2026-07-18T14:02:00Z'
        )
        """,
        (seed.tenant_id, virtual_key_id, environment_id),
    )
    connection.commit()
    decision_columns = [
        "routing_decision_id", "tenant_id", "request_id", "trace_id", "virtual_key_id",
        "model_alias_id", "requested_model", "environment", "selected_model_id",
        "selected_internal_model_id", "provider_id", "provider_code", "decision_status",
        "decision_reason", "candidate_summary", "error_code", "created_at", "environment_id",
    ]
    decision_before = tuple(
        connection.execute(
            f"SELECT {', '.join(decision_columns)} FROM routing_decisions "
            "WHERE routing_decision_id='rtd_prd047_roundtrip'"
        ).fetchone()
    )
    audit_columns = [
        "audit_log_id", "tenant_id", "actor_user_id", "actor_email", "actor_role",
        "action", "object_type", "object_id", "object_name", "before_summary_json",
        "after_summary_json", "changed_fields_json", "client_ip", "user_agent",
        "request_id", "trace_id", "created_at",
    ]
    audit_before = tuple(
        connection.execute(
            f"SELECT {', '.join(audit_columns)} FROM audit_logs "
            "WHERE audit_log_id='aud_prd047_old_preserved'"
        ).fetchone()
    )

    assert rollback_last_migration(database_url).rolled_back == "0036"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='model_alias_routing_policies'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='routing_candidates'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT audit_log_id FROM audit_logs WHERE audit_log_id='aud_prd047_removed'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT audit_log_id FROM audit_logs WHERE audit_log_id='aud_prd047_old_preserved'"
    ).fetchone() is not None
    audit_after = tuple(
        connection.execute(
            f"SELECT {', '.join(audit_columns)} FROM audit_logs "
            "WHERE audit_log_id='aud_prd047_old_preserved'"
        ).fetchone()
    )
    assert audit_after == audit_before
    assert connection.execute(
        "SELECT event_types_json FROM webhook_endpoints "
        "WHERE webhook_endpoint_id='wh_prd047_roundtrip'"
    ).fetchone()[0] == '["gateway.request_failed","audit.recorded"]'
    assert connection.execute(
        "SELECT event_types_json FROM webhook_endpoints "
        "WHERE webhook_endpoint_id='wh_prd047_only_route'"
    ).fetchone()[0] == "[]"
    assert connection.execute(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE event_type='route.policy_selected'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT webhook_delivery_id FROM webhook_deliveries "
        "WHERE webhook_delivery_id='whd_00000000000000000000000000470003'"
    ).fetchone() is not None
    decision_after = tuple(
        connection.execute(
            f"SELECT {', '.join(decision_columns)} FROM routing_decisions "
            "WHERE routing_decision_id='rtd_prd047_roundtrip'"
        ).fetchone()
    )
    assert decision_after == decision_before
    prd047_objects = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE (type='trigger' AND name LIKE '%prd047%')
           OR name IN (
             'uq_environments_tenant_environment_prd047',
             'uq_model_aliases_tenant_id_prd047',
             'uq_providers_tenant_id_prd047'
           )
        """
    ).fetchall()
    assert prd047_objects == []
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert apply_migrations(database_url).applied == ["0036", "0037", "0038", "0039", "0040"]
    rollback_quality_cost_migration(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM model_alias_routing_policies WHERE routing_mode='fixed' AND version=1"
    ).fetchone()[0] == connection.execute("SELECT COUNT(*) FROM model_aliases").fetchone()[0]
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_migration_guide_event_round_trip_preserves_history(tmp_path: Path) -> None:
    """PRD-040 正反迁移保留事件内容、索引和外键，并将指南事件安全降级。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]
    connection.executemany(
        """
        INSERT INTO console_page_events(
          event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
          target_section, path, request_id, trace_id, payload_json, created_at
        )
        VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, '2026-07-15T03:00:00Z')
        """,
        [
            (
                "cpe_prd040_page",
                seed.tenant_id,
                user_id,
                "console.page_viewed",
                "api-docs",
                "api-docs",
                "/#api-docs",
                "req_prd040_page",
                "trc_prd040_page",
                '{"hash":"#api-docs"}',
            ),
            (
                "cpe_prd040_dashboard",
                seed.tenant_id,
                user_id,
                "dashboard.viewed",
                "overview",
                "overview",
                "/#overview",
                "req_prd040_dashboard",
                "trc_prd040_dashboard",
                '{"hash":"#overview"}',
            ),
            (
                "cpe_prd040_guide",
                seed.tenant_id,
                user_id,
                "migration.guide_viewed",
                "migration-guide",
                "migration-guide",
                "/#migration-guide",
                "req_prd040_guide",
                "trc_prd040_guide",
                '{"hash":"#migration-guide","source":"sidebar"}',
            ),
        ],
    )
    connection.commit()

    # 先回滚 0032/0031；0030 回滚只能移除竞品事件能力，PRD-040 事件必须保留。
    rollback_policy_engine_migration(database_url)
    rollback_usage_export_migration(database_url)
    webhook_rollback = rollback_last_migration(database_url)
    assert webhook_rollback.rolled_back == "0031"
    competitor_rollback = rollback_last_migration(database_url)
    assert competitor_rollback.rolled_back == "0030"
    rollback = rollback_last_migration(database_url)
    rows = connection.execute(
        """
        SELECT event_id, tenant_id, user_id, event_type, path, request_id, trace_id, payload_json
        FROM console_page_events ORDER BY event_id
        """
    ).fetchall()
    indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='console_page_events'"
        ).fetchall()
    }
    assert rollback.rolled_back == "0029"
    assert len(rows) == 3
    guide = next(row for row in rows if row["event_id"] == "cpe_prd040_guide")
    assert guide["event_type"] == "console.page_viewed"
    assert guide["tenant_id"] == seed.tenant_id
    assert guide["user_id"] == user_id
    assert guide["path"] == "/#migration-guide"
    assert guide["request_id"] == "req_prd040_guide"
    assert guide["trace_id"] == "trc_prd040_guide"
    assert "#migration-guide" in guide["payload_json"]
    assert {
        "idx_console_page_events_tenant_time",
        "idx_console_page_events_user_time",
        "idx_console_page_events_item_time",
    }.issubset(indexes)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    reapplied = apply_migrations(database_url)
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='console_page_events'"
    ).fetchone()["sql"]
    assert reapplied.applied == [
        "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040"
    ]
    assert "migration.guide_viewed" in table_sql
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_runner_rolls_back_partial_explicit_transaction(tmp_path: Path) -> None:
    """迁移脚本在重命名后失败时必须恢复原表和数据，再独立记录 failed 状态。"""

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_base.sql").write_text(
        "CREATE TABLE source_events(event_id TEXT PRIMARY KEY, payload TEXT NOT NULL);\n"
        "INSERT INTO source_events VALUES('evt_original', 'preserved');\n",
        encoding="utf-8",
    )
    database_url = sqlite_url(tmp_path / "runner.db")
    apply_migrations(database_url, migrations_dir=migrations_dir)
    (migrations_dir / "0002_failing_rebuild.sql").write_text(
        "BEGIN IMMEDIATE;\n"
        "ALTER TABLE source_events RENAME TO source_events_old;\n"
        "CREATE TABLE source_events(event_id TEXT PRIMARY KEY, payload TEXT NOT NULL);\n"
        "INSERT INTO source_events SELECT * FROM source_events_old;\n"
        "INSERT INTO missing_table VALUES('force_failure');\n"
        "COMMIT;\n",
        encoding="utf-8",
    )

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url, migrations_dir=migrations_dir)

    connection = connect_database(database_url)
    row = connection.execute("SELECT event_id, payload FROM source_events").fetchone()
    old_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source_events_old'"
    ).fetchone()
    failed = connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0002'"
    ).fetchone()
    assert exc.value.code == "db.migration_failed"
    assert row["event_id"] == "evt_original"
    assert row["payload"] == "preserved"
    assert old_table is None
    assert failed["status"] == "failed"


def test_missing_database_url_returns_db_config_missing() -> None:
    """异常流程：缺少数据库连接串时返回统一 db 错误码。"""

    with pytest.raises(DbError) as exc:
        apply_migrations(None)

    assert exc.value.code == "db.config_missing"


def test_rollback_last_migration_steps_back_one_version(tmp_path: Path) -> None:
    """异常/回滚流程：测试环境每次只回滚最近一个已应用迁移。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    connection = connect_database(database_url)

    affinity_rollback = rollback_last_migration(database_url)
    assert affinity_rollback.rolled_back == "0040"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cache_affinities'"
    ).fetchone() is None
    hardening_rollback = rollback_last_migration(database_url)
    assert hardening_rollback.rolled_back == "0039"
    feedback_rollback = rollback_last_migration(database_url)
    assert feedback_rollback.rolled_back == "0038"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='request_feedbacks'"
    ).fetchone() is None
    quality_rollback = rollback_last_migration(database_url)
    assert quality_rollback.rolled_back == "0037"
    routing_rollback = rollback_last_migration(database_url)
    assert routing_rollback.rolled_back == "0036"
    dry_run_rollback = rollback_last_migration(database_url)
    assert dry_run_rollback.rolled_back == "0035"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='policy_explanations'"
    ).fetchone() is None

    snapshot_rollback = rollback_last_migration(database_url)
    assert snapshot_rollback.rolled_back == "0034"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='policy_snapshots'"
    ).fetchone() is None

    policy_rollback = rollback_last_migration(database_url)
    assert policy_rollback.rolled_back == "0033"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='policies'"
    ).fetchone() is None

    usage_export_rollback = rollback_last_migration(database_url)
    assert usage_export_rollback.rolled_back == "0032"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_export_jobs'"
    ).fetchone() is None

    webhook_rollback = rollback_last_migration(database_url)
    assert webhook_rollback.rolled_back == "0031"
    assert "secret_version" not in {
        row["name"] for row in connection.execute("PRAGMA table_info(webhook_endpoints)").fetchall()
    }

    competitor_rollback = rollback_last_migration(database_url)
    competitor_event_table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='console_page_events'"
    ).fetchone()["sql"]
    assert competitor_rollback.rolled_back == "0030"
    assert "help.competitor_viewed" not in competitor_event_table_sql
    assert "migration.guide_viewed" in competitor_event_table_sql

    migration_guide_rollback = rollback_last_migration(database_url)
    console_event_table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='console_page_events'"
    ).fetchone()["sql"]
    assert migration_guide_rollback.rolled_back == "0029"
    assert "migration.guide_viewed" not in console_event_table_sql

    sdk_rollback = rollback_last_migration(database_url)
    developer_event_table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='developer_portal_events'"
    ).fetchone()["sql"]
    assert sdk_rollback.rolled_back == "0028"
    assert "sdk.example_downloaded" not in developer_event_table_sql

    stream_rollback = rollback_last_migration(database_url)
    stream_sessions_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='stream_sessions'"
    ).fetchone()
    request_log_columns_after_stream_rollback = {
        row["name"] for row in connection.execute("PRAGMA table_info(request_logs)").fetchall()
    }
    assert stream_rollback.rolled_back == "0027"
    assert stream_sessions_after_rollback is None
    assert "stream_session_id" not in request_log_columns_after_stream_rollback
    assert "stream_status" not in request_log_columns_after_stream_rollback

    fallback_rollback = rollback_last_migration(database_url)
    fallback_policies_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fallback_policies'"
    ).fetchone()
    routing_attempts_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='routing_attempts'"
    ).fetchone()
    request_log_columns_after_fallback_rollback = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(request_logs)").fetchall()
    }
    usage_columns_after_fallback_rollback = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(usage_ledger)").fetchall()
    }
    health_logs_before_health_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='provider_health_probe_logs'"
    ).fetchone()
    assert fallback_rollback.rolled_back == "0026"
    assert fallback_policies_after_rollback is None
    assert routing_attempts_after_rollback is None
    assert "fallback_triggered" not in request_log_columns_after_fallback_rollback
    assert "routing_attempt_count" not in request_log_columns_after_fallback_rollback
    assert "final_routing_attempt_id" not in request_log_columns_after_fallback_rollback
    assert "routing_attempt_id" not in usage_columns_after_fallback_rollback
    assert health_logs_before_health_rollback is not None

    health_rollback = rollback_last_migration(database_url)
    health_logs_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='provider_health_probe_logs'"
    ).fetchone()
    assert health_rollback.rolled_back == "0025"
    assert health_logs_after_rollback is None

    audit_rollback = rollback_last_migration(database_url)
    audit_logs_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_logs'"
    ).fetchone()
    assert audit_rollback.rolled_back == "0024"
    assert audit_logs_after_rollback is None

    budget_block_rollback = rollback_last_migration(database_url)
    budget_blocks_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_blocks'"
    ).fetchone()
    budget_block_events_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_block_events'"
    ).fetchone()
    budget_reservations_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_reservations'"
    ).fetchone()
    price_plan_before_price_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='model_price_plans'"
    ).fetchone()
    assert budget_block_rollback.rolled_back == "0023"
    assert budget_blocks_after_rollback is None
    assert budget_block_events_after_rollback is None
    assert budget_reservations_after_rollback is None
    assert price_plan_before_price_rollback is not None

    model_price_rollback = rollback_last_migration(database_url)
    model_price_table_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='model_price_plans'"
    ).fetchone()
    price_snapshot_columns_after_rollback = {
        row["name"] for row in connection.execute("PRAGMA table_info(price_snapshots)").fetchall()
    }
    assert model_price_rollback.rolled_back == "0022"
    assert model_price_table_after_rollback is None
    assert "price_plan_id" not in price_snapshot_columns_after_rollback
    assert "effective_from" not in price_snapshot_columns_after_rollback

    environment_rollback = rollback_last_migration(database_url)
    environment_table_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='environments'"
    ).fetchone()
    assert environment_rollback.rolled_back == "0021"
    assert environment_table_after_rollback is None

    project_rollback = rollback_last_migration(database_url)
    project_table_after_rollback = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    virtual_key_columns_after_rollback = {
        row["name"] for row in connection.execute("PRAGMA table_info(virtual_keys)").fetchall()
    }
    assert project_rollback.rolled_back == "0020"
    assert project_table_after_rollback is None
    assert "project_id" not in virtual_key_columns_after_rollback
    assert "application_id" not in virtual_key_columns_after_rollback

    first = rollback_last_migration(database_url)
    user_preference_events_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_preference_events'"
    ).fetchone()
    user_preference_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(user_preferences)").fetchall()
    }
    dashboard_latency_index = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_request_logs_dashboard_latency'"
    ).fetchone()
    console_page_events_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='console_page_events'"
    ).fetchone()
    assert first.rolled_back == "0019"
    assert user_preference_events_table is None
    assert "locale" not in user_preference_columns
    assert dashboard_latency_index is not None
    assert console_page_events_sql is not None
    assert "dashboard.viewed" in console_page_events_sql["sql"]

    dashboard_rollback = rollback_last_migration(database_url)
    dashboard_latency_index = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_request_logs_dashboard_latency'"
    ).fetchone()
    console_page_events_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='console_page_events'"
    ).fetchone()
    assert dashboard_rollback.rolled_back == "0018"
    assert dashboard_latency_index is None
    assert console_page_events_sql is not None
    assert "dashboard.viewed" not in console_page_events_sql["sql"]

    second = rollback_last_migration(database_url)
    webhook_endpoints_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_endpoints'"
    ).fetchone()
    webhook_deliveries_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_deliveries'"
    ).fetchone()
    webhook_attempts_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_delivery_attempts'"
    ).fetchone()
    dlp_rules_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dlp_rules'"
    ).fetchone()
    dlp_matches_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dlp_matches'"
    ).fetchone()
    budget_rules_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_rules'"
    ).fetchone()
    request_log_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(request_logs)").fetchall()
    }
    assert second.rolled_back == "0017"
    assert webhook_endpoints_table is None
    assert webhook_deliveries_table is None
    assert webhook_attempts_table is None
    assert dlp_rules_table is not None
    assert dlp_matches_table is not None
    assert budget_rules_table is not None
    assert "dlp_status" in request_log_columns
    assert "dlp_match_count" in request_log_columns

    third = rollback_last_migration(database_url)
    dlp_rules_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dlp_rules'"
    ).fetchone()
    dlp_matches_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dlp_matches'"
    ).fetchone()
    budget_rules_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_rules'"
    ).fetchone()
    request_log_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(request_logs)").fetchall()
    }
    assert third.rolled_back == "0016"
    assert dlp_rules_table is None
    assert dlp_matches_table is None
    assert budget_rules_table is not None
    assert "dlp_status" not in request_log_columns
    assert "dlp_match_count" not in request_log_columns

    fourth = rollback_last_migration(database_url)
    budget_rules_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_rules'"
    ).fetchone()
    rate_limit_rules_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='virtual_key_rate_limit_rules'"
    ).fetchone()
    request_log_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(request_logs)").fetchall()
    }
    assert fourth.rolled_back == "0015"
    assert budget_rules_table is None
    assert rate_limit_rules_table is not None
    assert "budget_status" not in request_log_columns
    assert "budget_limit_cny" not in request_log_columns

    fifth = rollback_last_migration(database_url)
    rate_limit_rules_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='virtual_key_rate_limit_rules'"
    ).fetchone()
    debug_console_events_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='debug_console_events'"
    ).fetchone()
    assert fifth.rolled_back == "0014"
    assert rate_limit_rules_table is None
    assert debug_console_events_table is not None

    sixth = rollback_last_migration(database_url)
    debug_console_events_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='debug_console_events'"
    ).fetchone()
    developer_portal_events_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='developer_portal_events'"
    ).fetchone()
    assert sixth.rolled_back == "0013"
    assert debug_console_events_table is None
    assert developer_portal_events_table is not None

    seventh = rollback_last_migration(database_url)
    developer_portal_events_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='developer_portal_events'"
    ).fetchone()
    user_preferences_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_preferences'"
    ).fetchone()
    assert seventh.rolled_back == "0012"
    assert developer_portal_events_table is None
    assert user_preferences_table is not None

    eighth = rollback_last_migration(database_url)
    user_preferences_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_preferences'"
    ).fetchone()
    console_page_events_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='console_page_events'"
    ).fetchone()
    usage_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(usage_ledger)").fetchall()
    }
    price_snapshot_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='price_snapshots'"
    ).fetchone()
    assert eighth.rolled_back == "0011"
    assert user_preferences_table is None
    assert console_page_events_table is None
    assert "cache_tokens" in usage_columns
    assert price_snapshot_table is not None

    ninth = rollback_last_migration(database_url)
    request_log_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(request_logs)").fetchall()
    }
    usage_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(usage_ledger)").fetchall()
    }
    price_snapshot_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='price_snapshots'"
    ).fetchone()
    assert ninth.rolled_back == "0010"
    assert "cache_tokens" not in usage_columns
    assert "cost_cny" not in usage_columns
    assert price_snapshot_table is None
    assert "endpoint" in request_log_columns
    assert "total_tokens" in request_log_columns

    expected_versions = ["0009", "0008", "0007", "0006", "0005", "0004", "0003", "0002"]
    for expected_version in expected_versions:
        result = rollback_last_migration(database_url)
        assert result.rolled_back == expected_version

    sessions_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()
    assert sessions_table is None
    assert table_count(database_url, "tenants") == 0


def test_webhook_replay_migration_preserves_original_and_drops_replay_on_rollback(tmp_path: Path) -> None:
    """PRD-042 正反迁移：original 历史完整保留，replay/Secret 版本元数据随 0031 安全回滚。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
          max_attempts, created_by, updated_by, secret_version, secret_rotated_at,
          secret_rotated_by, created_at, updated_at
        )
        VALUES(
          'wh_prd042_migration', ?, '迁移接收端', 'https://example.com/hook',
          '["gateway.request_failed"]', 'ciphertext', 'whsec_prefix', 'fingerprint',
          'active', 5000, 3, ?, ?, 2, '2026-07-16T01:00:00Z', ?,
          '2026-07-16T00:00:00Z', '2026-07-16T01:00:00Z'
        )
        """,
        (seed.tenant_id, user_id, user_id, user_id),
    )
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
          event_timestamp, payload_json, status, attempt_count, max_attempts,
          secret_version, created_at, updated_at
        )
        VALUES(
          'whd_00000000000000000000000000420001', ?, 'wh_prd042_migration', 'evt_prd042_original',
          'gateway.request_failed', '2026-07-16T00:10:00Z', '{"event_id":"evt_prd042_original","payload":{}}',
          'failed', 3, 3, 1, '2026-07-16T00:10:00Z', '2026-07-16T00:11:00Z'
        )
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
          event_timestamp, payload_json, status, attempt_count, max_attempts,
          delivery_kind, replayed_from_delivery_id, replay_idempotency_key,
          replayed_by, replayed_at, secret_version, created_at, updated_at
        )
        VALUES(
          'whd_00000000000000000000000000420002', ?, 'wh_prd042_migration', 'evt_prd042_original',
          'gateway.request_failed', '2026-07-16T00:10:00Z', '{"event_id":"evt_prd042_original","payload":{}}',
          'failed', 1, 3, 'replay', 'whd_00000000000000000000000000420001', 'migration_same_key',
          ?, '2026-07-16T01:00:00Z', 2, '2026-07-16T01:00:00Z', '2026-07-16T01:01:00Z'
        )
        """,
        (seed.tenant_id, user_id),
    )
    connection.execute(
        """
        INSERT INTO webhook_delivery_attempts(
          webhook_delivery_attempt_id, tenant_id, webhook_delivery_id, webhook_endpoint_id,
          attempt_no, status, error_code, created_at
        )
        VALUES(
          'wha_prd042_replay', ?, 'whd_00000000000000000000000000420002', 'wh_prd042_migration',
          1, 'failed', 'webhook.timeout', '2026-07-16T01:01:00Z'
        )
        """,
        (seed.tenant_id,),
    )
    connection.commit()

    rollback_policy_engine_migration(database_url)
    rollback_usage_export_migration(database_url)
    rollback = rollback_last_migration(database_url)
    assert rollback.rolled_back == "0031"
    assert connection.execute(
        "SELECT webhook_delivery_id FROM webhook_deliveries WHERE webhook_delivery_id='whd_00000000000000000000000000420001'"
    ).fetchone() is not None
    assert connection.execute(
        "SELECT webhook_delivery_id FROM webhook_deliveries WHERE webhook_delivery_id='whd_00000000000000000000000000420002'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT webhook_delivery_attempt_id FROM webhook_delivery_attempts WHERE webhook_delivery_attempt_id='wha_prd042_replay'"
    ).fetchone() is None
    endpoint_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(webhook_endpoints)").fetchall()
    }
    delivery_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(webhook_deliveries)").fetchall()
    }
    assert "secret_version" not in endpoint_columns
    assert "delivery_kind" not in delivery_columns
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # 0047 会为该 failed original 建立治理事实；完整升级后必须停在当前头，不能绕过 down guard。
    reapplied = apply_current_migrations(database_url)
    # 该用例从真实 0030 历史重新升级，必须走到当前正式迁移头 0061；
    # 固定完整连续区间可同时防止后续迁移被静默跳过或出现版本断档。
    assert reapplied.applied == [f"{version:04d}" for version in range(31, 62)]
    assert connection.execute(
        "SELECT delivery_kind, secret_version FROM webhook_deliveries WHERE webhook_delivery_id='whd_00000000000000000000000000420001'"
    ).fetchone()["delivery_kind"] == "original"


def test_webhook_replay_migration_upgrades_real_0030_history_and_enforces_ownership(tmp_path: Path) -> None:
    """PRD-042 正向升级：真实 0030 original/attempt 完整保留，0031 触发器阻止归属错配。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    # 先移除 0032，再回滚 0031 得到真实 0030 结构，避免用新表伪造历史升级数据。
    rollback_policy_engine_migration(database_url)
    rollback_usage_export_migration(database_url)
    rollback = rollback_last_migration(database_url)
    assert rollback.rolled_back == "0031"
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
          max_attempts, created_by, updated_by, created_at, updated_at
        )
        VALUES(
          'wh_prd042_0030', ?, '0030 历史 Endpoint', 'https://example.com/hook',
          '["gateway.request_failed"]', 'ciphertext', 'whsec_prefix', 'fingerprint',
          'active', 5000, 3, ?, ?, '2026-07-16T00:00:00Z', '2026-07-16T00:00:00Z'
        )
        """,
        (seed.tenant_id, user_id, user_id),
    )
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
          event_timestamp, payload_json, status, attempt_count, max_attempts,
          error_code, created_at, updated_at
        )
        VALUES(
          'whd_00000000000000000000000000420003', ?, 'wh_prd042_0030', 'evt_prd042_0030',
          'gateway.request_failed', '2026-07-16T00:10:00Z',
          '{"event_id":"evt_prd042_0030","event_type":"gateway.request_failed","timestamp":"2026-07-16T00:10:00Z","tenant_id":"placeholder","payload":{}}',
          'failed', 1, 3, 'webhook.timeout', '2026-07-16T00:10:00Z', '2026-07-16T00:11:00Z'
        )
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO webhook_delivery_attempts(
          webhook_delivery_attempt_id, tenant_id, webhook_delivery_id, webhook_endpoint_id,
          attempt_no, status, error_code, created_at
        )
        VALUES('wha_prd042_0030', ?, 'whd_00000000000000000000000000420003', 'wh_prd042_0030', 1, 'failed',
               'webhook.timeout', '2026-07-16T00:11:00Z')
        """,
        (seed.tenant_id,),
    )
    connection.commit()

    # 历史失败投递在 0047 被接管为死信后属于正式治理事实，测试应保留当前完整栈顶。
    upgraded = apply_current_migrations(database_url)
    # 升级结果必须覆盖 0031 到当前正式迁移头 0061，不能继续沿用 PRD-049 时的旧上界。
    assert upgraded.applied == [f"{version:04d}" for version in range(31, 62)]
    delivery = connection.execute(
        "SELECT delivery_kind, secret_version, status FROM webhook_deliveries WHERE webhook_delivery_id='whd_00000000000000000000000000420003'"
    ).fetchone()
    attempt = connection.execute(
        "SELECT error_code FROM webhook_delivery_attempts WHERE webhook_delivery_attempt_id='wha_prd042_0030'"
    ).fetchone()
    assert tuple(delivery) == ("original", 1, "failed")
    assert attempt["error_code"] == "webhook.timeout"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
          max_attempts, created_by, updated_by, created_at, updated_at
        )
        VALUES('wh_prd042_second', ?, '第二 Endpoint', 'https://example.com/second',
               '["gateway.request_failed"]', 'ciphertext', 'whsec_prefix2', 'fingerprint2',
               'active', 5000, 3, ?, ?, '2026-07-16T00:00:00Z', '2026-07-16T00:00:00Z')
        """,
        (seed.tenant_id, user_id, user_id),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="webhook_delivery_owner_mismatch"):
        # 来源属于第一个 Endpoint，数据库触发器必须拒绝把 replay 挂到第二个 Endpoint。
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
              event_timestamp, payload_json, status, attempt_count, max_attempts,
              delivery_kind, replayed_from_delivery_id, replay_idempotency_key,
              replayed_by, replayed_at, secret_version, created_at, updated_at
            )
            VALUES('whd_00000000000000000000000000420004', ?, 'wh_prd042_second', 'evt_prd042_0030',
                   'gateway.request_failed', '2026-07-16T00:10:00Z', '{}', 'pending', 0, 3,
                   'replay', 'whd_00000000000000000000000000420003', 'bad_owner_key', ?, '2026-07-16T01:00:00Z',
                   1, '2026-07-16T01:00:00Z', '2026-07-16T01:00:00Z')
            """,
            (seed.tenant_id, user_id),
        )
    connection.rollback()


def _insert_completed_usage_export_job(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    *,
    job_id: str,
    request_id: str,
) -> None:
    """插入满足 0032 完成态约束的同步任务，供迁移约束和往返测试复用。"""

    csv_content = b"\xef\xbb\xbfrequest_id,total_tokens\r\nreq_1,3\r\n"
    connection.execute(
        """
        INSERT INTO usage_export_jobs(
          usage_export_job_id, tenant_id, requested_by, request_id, trace_id,
          status, generation_mode, filters_json, filter_fingerprint, snapshot_at, snapshot_ledger_sequence,
          row_count, total_tokens, total_cost_cny, total_cost_usd,
          file_name, csv_content, content_size_bytes, content_sha256,
          requested_at, completed_at, expires_at, created_at, updated_at
        )
        VALUES(
          ?, ?, ?, ?, ?, 'completed', 'sync', '{}', ?, '2026-07-17T00:00:00Z', 42,
          1, 3, 0.25, 0, ?, ?, ?, ?,
          '2026-07-17T00:00:00Z', '2026-07-17T00:00:01Z', '2026-07-18T00:00:01Z',
          '2026-07-17T00:00:00Z', '2026-07-17T00:00:01Z'
        )
        """,
        (
            job_id,
            tenant_id,
            user_id,
            request_id,
            f"trc_{request_id}",
            "a" * 64,
            f"metahub-usage-20260717-20260717-{job_id}.csv",
            csv_content,
            len(csv_content),
            "b" * 64,
        ),
    )


def test_usage_export_migration_enforces_job_event_audit_and_ownership(tmp_path: Path) -> None:
    """PRD-043 数据边界：任务状态、租户归属、事件不可变和审计白名单由数据库兜底。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]

    # 在 0031 状态写入乱序历史账本，再重放 0032，验证回填由业务时间和稳定 ID 决定而非插入 rowid。
    rollback_policy_engine_migration(database_url)
    rollback_usage_export_migration(database_url)
    connection.executemany(
        """
        INSERT INTO request_logs(request_id, tenant_id, trace_id, status, created_at, updated_at)
        VALUES(?, ?, ?, 'success', ?, ?)
        """,
        [
            (
                "req_prd043_backfill_late",
                seed.tenant_id,
                "trc_prd043_backfill_late",
                "2026-07-17T00:00:02Z",
                "2026-07-17T00:00:02Z",
            ),
            (
                "req_prd043_backfill_early",
                seed.tenant_id,
                "trc_prd043_backfill_early",
                "2026-07-17T00:00:01Z",
                "2026-07-17T00:00:01Z",
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
          raw_usage, normalized_usage, cost_amount, currency, created_at, updated_at
        )
        VALUES(?, ?, ?, 2, 1, '{}', '{}', 0.25, 'CNY', ?, ?)
        """,
        [
            (
                "usg_prd043_backfill_late",
                seed.tenant_id,
                "req_prd043_backfill_late",
                "2026-07-17T00:00:02Z",
                "2026-07-17T00:00:02Z",
            ),
            (
                "usg_prd043_backfill_early",
                seed.tenant_id,
                "req_prd043_backfill_early",
                "2026-07-17T00:00:01Z",
                "2026-07-17T00:00:01Z",
            ),
        ],
    )
    connection.commit()
    assert apply_migrations(database_url).applied == [
        "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040"
    ]

    job_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(usage_export_jobs)").fetchall()
    }
    assert {
        "tenant_id",
        "requested_by",
        "filters_json",
        "snapshot_at",
        "snapshot_ledger_sequence",
        "csv_content",
        "lease_token",
        "content_sha256",
        "download_count",
    }.issubset(job_columns)
    sequence_rows = connection.execute(
        """
        SELECT usage_export_sequence, usage_ledger_id
        FROM usage_export_ledger_sequences
        WHERE tenant_id=?
        ORDER BY usage_export_sequence
        """,
        (seed.tenant_id,),
    ).fetchall()
    assert [row["usage_ledger_id"] for row in sequence_rows] == [
        "usg_prd043_backfill_early",
        "usg_prd043_backfill_late",
    ]
    historical_max_sequence = sequence_rows[-1]["usage_export_sequence"]
    insert_disabled_request_claim(
        connection,
        seed.tenant_id,
        request_id="req_prd043_sequence_runtime",
        trace_id="trc_prd043_sequence_runtime",
    )
    connection.execute(
        """
        INSERT INTO request_logs(request_id, tenant_id, trace_id, status, created_at, updated_at)
        VALUES('req_prd043_sequence_runtime', ?, 'trc_prd043_sequence_runtime', 'success',
               '2026-07-17T00:00:03Z', '2026-07-17T00:00:03Z')
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
          raw_usage, normalized_usage, cost_amount, currency, created_at, updated_at
        )
        VALUES('usg_prd043_sequence_runtime', ?, 'req_prd043_sequence_runtime', 3, 1,
               '{}', '{}', 0.5, 'CNY', '2026-07-17T00:00:03Z', '2026-07-17T00:00:03Z')
        """,
        (seed.tenant_id,),
    )
    runtime_sequence = connection.execute(
        """
        SELECT usage_export_sequence
        FROM usage_export_ledger_sequences
        WHERE tenant_id=? AND usage_ledger_id='usg_prd043_sequence_runtime'
        """,
        (seed.tenant_id,),
    ).fetchone()["usage_export_sequence"]
    assert runtime_sequence > historical_max_sequence
    indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='usage_export_jobs'"
        ).fetchall()
    }
    sequence_indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='usage_export_ledger_sequences'"
        ).fetchall()
    }
    assert "idx_usage_export_ledger_sequences_tenant_sequence" in sequence_indexes
    triggers = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND (name LIKE 'trg_usage_export_%' OR name LIKE 'trg_usage_ledger_export_%')"
        ).fetchall()
    }
    assert {
        "idx_usage_export_jobs_tenant_time",
        "idx_usage_export_jobs_pending_lease",
        "idx_usage_export_jobs_expiry",
    }.issubset(indexes)
    assert {
        "trg_usage_export_jobs_owner_insert",
        "trg_usage_export_jobs_identity_immutable",
        "trg_usage_export_jobs_status_transition",
        "trg_usage_export_events_owner_insert",
        "trg_usage_export_events_immutable_update",
        "trg_usage_export_events_immutable_delete",
        "trg_usage_export_ledger_sequences_owner_insert",
        "trg_usage_export_ledger_sequences_immutable_update",
        "trg_usage_export_ledger_sequences_immutable_delete",
        "trg_usage_ledger_export_append_only_update",
        "trg_usage_ledger_export_append_only_delete",
        "trg_usage_ledger_export_sequence_insert",
    }.issubset(triggers)

    _insert_completed_usage_export_job(
        connection,
        seed.tenant_id,
        user_id,
        job_id="uex_prd043_valid",
        request_id="req_prd043_valid",
    )
    connection.execute(
        """
        INSERT INTO usage_export_events(
          usage_export_event_id, tenant_id, usage_export_job_id, event_type,
          actor_user_id, request_id, trace_id, payload_json, created_at
        )
        VALUES(
          'uee_prd043_valid', ?, 'uex_prd043_valid', 'usage.exported', ?,
          'req_prd043_valid', 'trc_req_prd043_valid',
          '{"status":"completed","row_count":1}', '2026-07-17T00:00:01Z'
        )
        """,
        (seed.tenant_id, user_id),
    )
    connection.executemany(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES(?, ?, ?, 'admin@metahub.local', 'admin', ?, 'usage_export_job',
               'uex_prd043_valid', ?, ?, ?, ?, ?, '2026-07-17T00:00:01Z')
        """,
        [
            (
                "aud_prd043_requested",
                seed.tenant_id,
                user_id,
                "usage_export.requested",
                "{}",
                '{"status":"completed","row_count":1}',
                '["row_count","status"]',
                "req_prd043_valid",
                "trc_req_prd043_valid",
            ),
            (
                "aud_prd043_downloaded",
                seed.tenant_id,
                user_id,
                "usage_export.downloaded",
                '{"download_count":0}',
                '{"download_count":1}',
                '["download_count"]',
                "req_prd043_download",
                "trc_req_prd043_download",
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_prd043_other', 'prd043-other', 'PRD043 其他租户', 'internal_only',
               'active', '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z')
        """
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="usage_export_ledger_sequence_owner_mismatch"):
        connection.execute(
            """
            INSERT INTO usage_export_ledger_sequences(tenant_id, usage_ledger_id)
            VALUES('tenant_prd043_other', 'usg_prd043_backfill_early')
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="usage_export_job_owner_mismatch"):
        _insert_completed_usage_export_job(
            connection,
            "tenant_prd043_other",
            user_id,
            job_id="uex_prd043_bad_owner",
            request_id="req_prd043_bad_owner",
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            """
            INSERT INTO usage_export_jobs(
              usage_export_job_id, tenant_id, requested_by, request_id, trace_id,
              status, generation_mode, filters_json, filter_fingerprint, snapshot_at, snapshot_ledger_sequence,
              row_count, file_name, requested_at, created_at, updated_at
            )
            VALUES('uex_prd043_bad_json', ?, ?, 'req_prd043_bad_json', 'trc_prd043_bad_json',
                   'pending', 'async', '[]', ?, '2026-07-17T00:00:00Z', 0,
                   0, 'metahub-usage-bad-json.csv', '2026-07-17T00:00:00Z',
                   '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z')
            """,
            (seed.tenant_id, user_id, "c" * 64),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        # 账本序列水位必须是创建时捕获的非负整数，负值不能进入异步快照合同。
        connection.execute(
            """
            INSERT INTO usage_export_jobs(
              usage_export_job_id, tenant_id, requested_by, request_id, trace_id,
              status, generation_mode, filters_json, filter_fingerprint,
              snapshot_at, snapshot_ledger_sequence, row_count, file_name,
              requested_at, created_at, updated_at
            )
            VALUES('uex_prd043_bad_sequence', ?, ?, 'req_prd043_bad_sequence', 'trc_prd043_bad_sequence',
                   'pending', 'async', '{}', ?, '2026-07-17T00:00:00Z', -1, 0,
                   'metahub-usage-bad-sequence.csv', '2026-07-17T00:00:00Z',
                   '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z')
            """,
            (seed.tenant_id, user_id, "d" * 64),
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_completed_usage_export_job(
            connection,
            seed.tenant_id,
            user_id,
            job_id="uex_prd043_duplicate_request",
            request_id="req_prd043_valid",
        )
    with pytest.raises(sqlite3.IntegrityError, match="usage_export_job_identity_immutable"):
        connection.execute(
            "UPDATE usage_export_jobs SET tenant_id='tenant_prd043_other' WHERE usage_export_job_id='uex_prd043_valid'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="usage_export_job_identity_immutable"):
        # 创建后的账本高水位不能后移，否则同一 job 的行数和制品范围会发生漂移。
        connection.execute(
            "UPDATE usage_export_jobs SET snapshot_ledger_sequence=43 WHERE usage_export_job_id='uex_prd043_valid'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="usage_export_ledger_sequence_immutable"):
        connection.execute(
            "UPDATE usage_export_ledger_sequences SET tenant_id='tenant_prd043_other' "
            "WHERE usage_ledger_id='usg_prd043_backfill_early'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="usage_export_ledger_sequence_immutable"):
        connection.execute(
            "DELETE FROM usage_export_ledger_sequences "
            "WHERE usage_ledger_id='usg_prd043_backfill_early'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="usage_ledger_append_only"):
        # 水位内财务事实不能原地改写；后续对账必须追加新的调整记录。
        connection.execute(
            "UPDATE usage_ledger SET cost_amount=0.5 "
            "WHERE usage_ledger_id='usg_prd043_backfill_early'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="usage_ledger_append_only"):
        connection.execute(
            "DELETE FROM usage_ledger WHERE usage_ledger_id='usg_prd043_backfill_early'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="usage_export_job_status_transition_invalid"):
        connection.execute(
            "UPDATE usage_export_jobs SET status='processing' WHERE usage_export_job_id='uex_prd043_valid'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="usage_export_event_owner_mismatch"):
        connection.execute(
            """
            INSERT INTO usage_export_events(
              usage_export_event_id, tenant_id, usage_export_job_id, event_type,
              actor_user_id, request_id, trace_id, payload_json, created_at
            )
            VALUES('uee_prd043_bad_owner', 'tenant_prd043_other', 'uex_prd043_valid',
                   'usage.exported', ?, 'req_bad', 'trc_bad', '{}', '2026-07-17T00:00:02Z')
            """,
            (user_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="usage_export_event_immutable"):
        connection.execute(
            "UPDATE usage_export_events SET payload_json='{}' WHERE usage_export_event_id='uee_prd043_valid'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
              action, object_type, object_id, before_summary_json, after_summary_json,
              changed_fields_json, request_id, trace_id, created_at
            )
            VALUES('aud_prd043_unknown', ?, ?, 'admin@metahub.local', 'admin',
                   'usage_export.unknown', 'usage_export_job', 'uex_prd043_valid',
                   '{}', '{}', '[]', 'req_unknown', 'trc_unknown', '2026-07-17T00:00:02Z')
            """,
            (seed.tenant_id, user_id),
        )
    connection.rollback()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_usage_export_migration_round_trip_preserves_usage_and_older_audit(tmp_path: Path) -> None:
    """PRD-043 往返迁移：down 删除导出制品/审计，保留 Usage 与 0031 之前管理事实。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]
    insert_disabled_request_claim(
        connection,
        seed.tenant_id,
        request_id="req_prd043_usage_preserved",
        trace_id="trc_prd043_usage_preserved",
    )
    connection.execute(
        """
        INSERT INTO request_logs(request_id, tenant_id, trace_id, status, created_at, updated_at)
        VALUES('req_prd043_usage_preserved', ?, 'trc_prd043_usage_preserved', 'success',
               '2026-07-17T01:00:00Z', '2026-07-17T01:00:00Z')
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
          raw_usage, normalized_usage, cost_amount, currency, created_at, updated_at
        )
        VALUES('usg_prd043_preserved', ?, 'req_prd043_usage_preserved', 2, 1,
               '{}', '{}', 0.25, 'CNY', '2026-07-17T01:00:00Z', '2026-07-17T01:00:00Z')
        """,
        (seed.tenant_id,),
    )
    _insert_completed_usage_export_job(
        connection,
        seed.tenant_id,
        user_id,
        job_id="uex_prd043_rollback",
        request_id="req_prd043_rollback",
    )
    connection.execute(
        """
        INSERT INTO usage_export_events(
          usage_export_event_id, tenant_id, usage_export_job_id, event_type,
          actor_user_id, request_id, trace_id, payload_json, created_at
        )
        VALUES('uee_prd043_rollback', ?, 'uex_prd043_rollback', 'usage.exported', ?,
               'req_prd043_rollback', 'trc_req_prd043_rollback',
               '{"status":"completed"}', '2026-07-17T01:00:01Z')
        """,
        (seed.tenant_id, user_id),
    )
    connection.executemany(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES(?, ?, ?, 'admin@metahub.local', 'admin', ?, ?, ?, '{}', ?, ?, ?, ?,
               '2026-07-17T01:00:01Z')
        """,
        [
            (
                "aud_prd043_old_preserved",
                seed.tenant_id,
                user_id,
                "provider.created",
                "provider",
                "provider_prd043_preserved",
                '{"name":"历史 Provider"}',
                '["name"]',
                "req_prd043_old_audit",
                "trc_prd043_old_audit",
            ),
            (
                "aud_prd043_new_removed",
                seed.tenant_id,
                user_id,
                "usage_export.requested",
                "usage_export_job",
                "uex_prd043_rollback",
                '{"status":"completed"}',
                '["status"]',
                "req_prd043_rollback",
                "trc_req_prd043_rollback",
            ),
        ],
    )
    connection.commit()

    rollback_request_feedback_migration(database_url)
    quality_rollback = rollback_last_migration(database_url)
    assert quality_rollback.rolled_back == "0037"
    routing_rollback = rollback_last_migration(database_url)
    assert routing_rollback.rolled_back == "0036"
    dry_run_rollback = rollback_last_migration(database_url)
    assert dry_run_rollback.rolled_back == "0035"
    snapshot_rollback = rollback_last_migration(database_url)
    assert snapshot_rollback.rolled_back == "0034"
    policy_rollback = rollback_last_migration(database_url)
    assert policy_rollback.rolled_back == "0033"
    rollback = rollback_last_migration(database_url)
    assert rollback.rolled_back == "0032"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_export_jobs'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_export_events'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='usage_export_ledger_sequences'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='trigger' AND name LIKE 'trg_usage_ledger_export_%'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT usage_ledger_id FROM usage_ledger WHERE usage_ledger_id='usg_prd043_preserved'"
    ).fetchone() is not None
    audit_rows = connection.execute(
        "SELECT audit_log_id, action FROM audit_logs ORDER BY audit_log_id"
    ).fetchall()
    assert [(row["audit_log_id"], row["action"]) for row in audit_rows] == [
        ("aud_prd043_old_preserved", "provider.created")
    ]
    audit_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_logs'"
    ).fetchone()["sql"]
    assert "webhook_delivery.replayed" in audit_sql
    assert "usage_export.requested" not in audit_sql
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    reapplied = apply_migrations(database_url)
    assert reapplied.applied == ["0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040"]
    assert connection.execute(
        "SELECT usage_ledger_id FROM usage_ledger WHERE usage_ledger_id='usg_prd043_preserved'"
    ).fetchone() is not None
    assert connection.execute(
        "SELECT audit_log_id FROM audit_logs WHERE audit_log_id='aud_prd043_old_preserved'"
    ).fetchone() is not None
    restored_sequence = connection.execute(
        """
        SELECT usage_export_sequence
        FROM usage_export_ledger_sequences
        WHERE tenant_id=? AND usage_ledger_id='usg_prd043_preserved'
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert restored_sequence is not None
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_policy_migration_bundle(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    *,
    suffix: str,
    action_type: str = "allow",
    target_model_id: str | None = None,
) -> tuple[str, str, str]:
    """插入一组满足 0033 约束的 Policy/Rule/Action，供迁移边界测试复用。

    参数只使用测试 ID 和 canonical 摘要，不写 Provider Key、Prompt、Response 或任意脚本；
    route_to 目标仍由调用方显式传入，以便测试数据库的租户与 active 双重约束。
    """

    policy_id = f"pol_{suffix}"
    rule_id = f"prule_{suffix}"
    action_id = f"pact_{suffix}"
    timestamp = "2026-07-17T08:00:00Z"
    connection.execute(
        """
        INSERT INTO policies(
          policy_id, tenant_id, name, description, status, priority, version,
          dsl_version, created_by, updated_by, created_at, updated_at
        )
        VALUES(?, ?, ?, '迁移专项安全描述', 'active', 100, 1,
               'policy-dsl-v1', ?, ?, ?, ?)
        """,
        (policy_id, tenant_id, f"策略-{suffix}", user_id, user_id, timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO policy_rules(
          policy_rule_id, tenant_id, policy_id, condition_expr,
          condition_ast_json, condition_hash, variables_json, created_at, updated_at
        )
        VALUES(?, ?, ?, 'env == "prod"',
               '{"kind":"comparison","operator":"eq","value":{"kind":"string","value":"prod"},"variable":"env"}',
               ?, '["env"]', ?, ?)
        """,
        (rule_id, tenant_id, policy_id, "a" * 64, timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO policy_actions(
          policy_action_id, tenant_id, policy_id, action_type,
          target_model_id, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (action_id, tenant_id, policy_id, action_type, target_model_id, timestamp, timestamp),
    )
    return policy_id, rule_id, action_id


def _insert_policy_migration_event(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    *,
    event_id: str,
    event_type: str,
    request_id: str,
    policy_id: str | None,
    reason_code: str,
    payload_json: str,
) -> None:
    """写入安全白名单策略事件，集中保持 request_id、trace_id 和租户归属字段一致。"""

    connection.execute(
        """
        INSERT INTO policy_events(
          policy_event_id, tenant_id, policy_id, event_type, actor_user_id,
          request_id, trace_id, reason_code, payload_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-07-17T08:01:00Z')
        """,
        (
            event_id,
            tenant_id,
            policy_id,
            event_type,
            user_id,
            request_id,
            f"trc_{request_id}",
            reason_code,
            payload_json,
        ),
    )


def _foreign_key_column_pairs(
    rows: list[sqlite3.Row],
    parent_table: str,
) -> set[tuple[tuple[str, str], ...]]:
    """按 SQLite foreign_key_list 的 id/seq 还原指定父表的复合外键列顺序。"""

    foreign_key_ids = {row["id"] for row in rows if row["table"] == parent_table}
    return {
        tuple(
            (row["from"], row["to"])
            for row in sorted(
                (candidate for candidate in rows if candidate["id"] == foreign_key_id),
                key=lambda candidate: candidate["seq"],
            )
        )
        for foreign_key_id in foreign_key_ids
    }


def test_policy_engine_migration_enforces_tenant_version_target_and_event_contract(
    tmp_path: Path,
) -> None:
    """PRD-044 约束专项：复合归属、版本、active target、partial unique 和不可变事件均生效。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? ORDER BY provider_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    active_model = connection.execute(
        "SELECT model_id FROM models "
        "WHERE tenant_id=? AND status='active' ORDER BY model_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    assert provider is not None
    assert active_model is not None

    # 第二租户与禁用模型专门用于证明全局 user_id/model_id 主键不能绕过复合 tenant 归属。
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_prd044_other', 'prd044-other', 'PRD044 其他租户', 'internal_only',
               'active', '2026-07-17T08:00:00Z', '2026-07-17T08:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash,
          role, status, created_at, updated_at
        )
        VALUES('user_prd044_other', 'tenant_prd044_other', 'other-prd044@example.invalid',
               '其他租户管理员', 'hash-only', 'admin', 'active',
               '2026-07-17T08:00:00Z', '2026-07-17T08:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, health_status, created_at, updated_at
        )
        VALUES('provider_prd044_other', 'tenant_prd044_other', 'prd044-other',
               '其他租户 Provider', 'https://example.invalid/v1', 'active',
               'openai_compatible', 'bearer', 'unknown',
               '2026-07-17T08:00:00Z', '2026-07-17T08:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, display_name, capabilities, status, created_at, updated_at
        )
        VALUES('model_prd044_other', 'tenant_prd044_other', 'provider_prd044_other',
               'prd044-other-model', 'prd044-other-model', 'prd044-other-model',
               '其他租户模型', '{}', 'active',
               '2026-07-17T08:00:00Z', '2026-07-17T08:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, display_name, capabilities, status, created_at, updated_at
        )
        VALUES('model_prd044_disabled', ?, ?, 'prd044-disabled-model',
               'prd044-disabled-model', 'prd044-disabled-model', '禁用目标模型', '{}',
               'disabled', '2026-07-17T08:00:00Z', '2026-07-17T08:00:00Z')
        """,
        (seed.tenant_id, provider["provider_id"]),
    )

    policy_id, rule_id, action_id = _insert_policy_migration_bundle(
        connection,
        seed.tenant_id,
        user_id,
        suffix="contract",
    )
    route_policy_id, _, _ = _insert_policy_migration_bundle(
        connection,
        seed.tenant_id,
        user_id,
        suffix="route",
        action_type="route_to",
        target_model_id=active_model["model_id"],
    )
    connection.commit()

    user_indexes = {
        row["name"]: row["unique"]
        for row in connection.execute("PRAGMA index_list(users)").fetchall()
    }
    model_indexes = {
        row["name"]: row["unique"]
        for row in connection.execute("PRAGMA index_list(models)").fetchall()
    }
    assert user_indexes["uq_users_tenant_user_prd044"] == 1
    assert model_indexes["uq_models_tenant_model_prd044"] == 1

    # 复合 FK 必须按 tenant_id 在第 0 列、业务 ID 在第 1 列指向对应 parent unique。
    policy_fks = connection.execute("PRAGMA foreign_key_list(policies)").fetchall()
    rule_fks = connection.execute("PRAGMA foreign_key_list(policy_rules)").fetchall()
    action_fks = connection.execute("PRAGMA foreign_key_list(policy_actions)").fetchall()
    event_fks = connection.execute("PRAGMA foreign_key_list(policy_events)").fetchall()
    assert _foreign_key_column_pairs(policy_fks, "users") == {
        (("tenant_id", "tenant_id"), ("created_by", "user_id")),
        (("tenant_id", "tenant_id"), ("updated_by", "user_id")),
    }
    assert _foreign_key_column_pairs(rule_fks, "policies") == {
        (("tenant_id", "tenant_id"), ("policy_id", "policy_id"))
    }
    assert _foreign_key_column_pairs(action_fks, "policies") == {
        (("tenant_id", "tenant_id"), ("policy_id", "policy_id"))
    }
    assert _foreign_key_column_pairs(action_fks, "models") == {
        (("tenant_id", "tenant_id"), ("target_model_id", "model_id"))
    }
    assert _foreign_key_column_pairs(event_fks, "users") == {
        (("tenant_id", "tenant_id"), ("actor_user_id", "user_id"))
    }
    assert _foreign_key_column_pairs(event_fks, "policies") == {
        (("tenant_id", "tenant_id"), ("policy_id", "policy_id"))
    }

    partial_index_sql = {
        row["name"]: row["sql"]
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='policy_events'"
        ).fetchall()
    }
    assert "WHERE event_type = 'policy.created'" in partial_index_sql[
        "idx_policy_events_created_unique"
    ]
    assert "WHERE event_type = 'policy.validation_failed'" in partial_index_sql[
        "idx_policy_events_validation_request_unique"
    ]
    trigger_names = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_polic%'"
        ).fetchall()
    }
    assert {
        "trg_policies_owner_insert",
        "trg_policies_owner_update",
        "trg_policies_identity_immutable",
        "trg_policies_version_increment",
        "trg_policy_rules_owner_insert",
        "trg_policy_rules_owner_update",
        "trg_policy_rules_identity_immutable",
        "trg_policy_actions_owner_insert",
        "trg_policy_actions_owner_update",
        "trg_policy_actions_target_active_insert",
        "trg_policy_actions_target_active_update",
        "trg_policy_actions_identity_immutable",
        "trg_policy_events_owner_insert",
        "trg_policy_events_immutable_update",
        "trg_policy_events_immutable_delete",
    }.issubset(trigger_names)

    with pytest.raises(sqlite3.IntegrityError, match="policy_owner_mismatch"):
        connection.execute(
            """
            INSERT INTO policies(
              policy_id, tenant_id, name, status, priority, version, dsl_version,
              created_by, updated_by, created_at, updated_at
            )
            VALUES('pol_bad_owner', ?, '错误 Owner', 'active', 1, 1, 'policy-dsl-v1',
                   'user_prd044_other', 'user_prd044_other', '2026-07-17T08:00:00Z',
                   '2026-07-17T08:00:00Z')
            """,
            (seed.tenant_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            """
            INSERT INTO policies(
              policy_id, tenant_id, name, status, priority, version, dsl_version,
              created_by, updated_by, created_at, updated_at
            )
            VALUES('pol_bad_priority', ?, '错误优先级', 'active', 0, 1, 'policy-dsl-v1',
                   ?, ?, '2026-07-17T08:00:00Z', '2026-07-17T08:00:00Z')
            """,
            (seed.tenant_id, user_id, user_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_identity_immutable"):
        connection.execute(
            "UPDATE policies SET tenant_id='tenant_prd044_other', version=2 WHERE policy_id=?",
            (policy_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_version_increment_invalid"):
        connection.execute(
            "UPDATE policies SET name='跳号策略', version=3 WHERE policy_id=?",
            (policy_id,),
        )
    connection.execute(
        "UPDATE policies SET name='合法版本推进', version=2, updated_by=?, updated_at=? "
        "WHERE policy_id=?",
        (user_id, "2026-07-17T08:02:00Z", policy_id),
    )
    assert connection.execute(
        "SELECT version FROM policies WHERE policy_id=?", (policy_id,)
    ).fetchone()["version"] == 2

    with pytest.raises(sqlite3.IntegrityError, match="policy_rule_owner_mismatch"):
        connection.execute(
            """
            INSERT INTO policy_rules(
              policy_rule_id, tenant_id, policy_id, condition_expr, condition_ast_json,
              condition_hash, variables_json, created_at, updated_at
            )
            VALUES('prule_cross_tenant', 'tenant_prd044_other', ?, 'env == "prod"',
                   '{}', ?, '["env"]', '2026-07-17T08:00:00Z', '2026-07-17T08:00:00Z')
            """,
            (policy_id, "b" * 64),
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        connection.execute(
            """
            INSERT INTO policy_rules(
              policy_rule_id, tenant_id, policy_id, condition_expr, condition_ast_json,
              condition_hash, variables_json, created_at, updated_at
            )
            VALUES('prule_duplicate', ?, ?, 'env == "test"', '{}', ?, '["env"]',
                   '2026-07-17T08:00:00Z', '2026-07-17T08:00:00Z')
            """,
            (seed.tenant_id, policy_id, "c" * 64),
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_rule_identity_immutable"):
        connection.execute(
            "UPDATE policy_rules SET policy_id=? WHERE policy_rule_id=?",
            (route_policy_id, rule_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            "UPDATE policy_rules SET condition_ast_json='[]' WHERE policy_rule_id=?",
            (rule_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            "UPDATE policy_rules SET condition_hash=? WHERE policy_rule_id=?",
            ("Z" * 64, rule_id),
        )

    with pytest.raises(sqlite3.IntegrityError, match="policy_action_target_invalid"):
        connection.execute(
            """
            UPDATE policy_actions
            SET action_type='route_to', target_model_id='model_prd044_other', updated_at=?
            WHERE policy_action_id=?
            """,
            ("2026-07-17T08:03:00Z", action_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        connection.execute(
            """
            INSERT INTO policy_actions(
              policy_action_id, tenant_id, policy_id, action_type,
              target_model_id, created_at, updated_at
            )
            VALUES('pact_duplicate', ?, ?, 'deny', NULL,
                   '2026-07-17T08:00:00Z', '2026-07-17T08:00:00Z')
            """,
            (seed.tenant_id, policy_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_action_target_invalid"):
        connection.execute(
            """
            UPDATE policy_actions
            SET action_type='route_to', target_model_id='model_prd044_disabled', updated_at=?
            WHERE policy_action_id=?
            """,
            ("2026-07-17T08:03:00Z", action_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            "UPDATE policy_actions SET target_model_id=? WHERE policy_action_id=?",
            (active_model["model_id"], action_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_action_identity_immutable"):
        connection.execute(
            "UPDATE policy_actions SET policy_id=? WHERE policy_action_id=?",
            (route_policy_id, action_id),
        )

    _insert_policy_migration_event(
        connection,
        seed.tenant_id,
        user_id,
        event_id="pevt_contract_created",
        event_type="policy.created",
        request_id="req_contract_created",
        policy_id=policy_id,
        reason_code="policy.created",
        payload_json='{"action_type":"allow","condition_hash":"safe"}',
    )
    _insert_policy_migration_event(
        connection,
        seed.tenant_id,
        user_id,
        event_id="pevt_contract_failed",
        event_type="policy.validation_failed",
        request_id="req_contract_failed",
        policy_id=None,
        reason_code="policy.expression_invalid",
        payload_json='{"diagnostic_codes":["policy.expression_invalid"]}',
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_policy_migration_event(
            connection,
            seed.tenant_id,
            user_id,
            event_id="pevt_contract_created_duplicate",
            event_type="policy.created",
            request_id="req_contract_created_duplicate",
            policy_id=policy_id,
            reason_code="policy.created",
            payload_json="{}",
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_policy_migration_event(
            connection,
            seed.tenant_id,
            user_id,
            event_id="pevt_contract_failed_duplicate",
            event_type="policy.validation_failed",
            request_id="req_contract_failed",
            policy_id=None,
            reason_code="policy.context_invalid",
            payload_json="{}",
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_event_owner_mismatch"):
        _insert_policy_migration_event(
            connection,
            seed.tenant_id,
            "user_prd044_other",
            event_id="pevt_contract_bad_actor",
            event_type="policy.validation_failed",
            request_id="req_contract_bad_actor",
            policy_id=None,
            reason_code="policy.action_invalid",
            payload_json="{}",
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_policy_migration_event(
            connection,
            seed.tenant_id,
            user_id,
            event_id="pevt_contract_bad_reason",
            event_type="policy.created",
            request_id="req_contract_bad_reason",
            policy_id=policy_id,
            reason_code="policy.expression_invalid",
            payload_json="{}",
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_policy_migration_event(
            connection,
            seed.tenant_id,
            user_id,
            event_id="pevt_contract_many_diagnostics",
            event_type="policy.validation_failed",
            request_id="req_contract_many_diagnostics",
            policy_id=None,
            reason_code="policy.expression_invalid",
            payload_json='{"diagnostic_codes":["1","2","3","4","5","6","7","8","9"]}',
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_event_immutable"):
        connection.execute(
            "UPDATE policy_events SET payload_json='{}' "
            "WHERE policy_event_id='pevt_contract_failed'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_event_immutable"):
        connection.execute(
            "DELETE FROM policy_events WHERE policy_event_id='pevt_contract_failed'"
        )

    connection.rollback()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_policy_engine_migration_round_trip_preserves_full_0032_history(tmp_path: Path) -> None:
    """PRD-044 往返迁移：0032 的审计、Usage 与 Webhook 逐字段保留，仅过滤 policy 专属事实。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    rollback_policy_engine_migration(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]

    # 同时保留真实 Usage job/event 与 Webhook delivery，证明 audit 重建没有破坏 0032 其他业务实体。
    _insert_completed_usage_export_job(
        connection,
        seed.tenant_id,
        user_id,
        job_id="uex_prd044_preserved",
        request_id="req_prd044_usage",
    )
    connection.execute(
        """
        INSERT INTO usage_export_events(
          usage_export_event_id, tenant_id, usage_export_job_id, event_type,
          actor_user_id, request_id, trace_id, payload_json, created_at
        )
        VALUES('uee_prd044_preserved', ?, 'uex_prd044_preserved', 'usage.exported', ?,
               'req_prd044_usage', 'trc_prd044_usage', '{"status":"completed"}',
               '2026-07-17T09:00:00Z')
        """,
        (seed.tenant_id, user_id),
    )
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
          max_attempts, created_by, updated_by, created_at, updated_at
        )
        VALUES('wh_prd044_preserved', ?, '迁移保留 Endpoint', 'https://example.invalid/hook',
               '["gateway.request_failed"]', 'ciphertext-only', 'whsec_safe', 'fingerprint-safe',
               'active', 5000, 3, ?, ?, '2026-07-17T09:00:00Z', '2026-07-17T09:00:00Z')
        """,
        (seed.tenant_id, user_id, user_id),
    )
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
          event_timestamp, payload_json, status, attempt_count, max_attempts,
          delivery_kind, secret_version, created_at, updated_at
        )
        VALUES('whd_00000000000000000000000000440001', ?, 'wh_prd044_preserved', 'evt_prd044_preserved',
               'gateway.request_failed', '2026-07-17T09:00:00Z', '{}', 'success', 1, 3,
               'original', 1, '2026-07-17T09:00:00Z', '2026-07-17T09:00:01Z')
        """,
        (seed.tenant_id,),
    )

    audit_pairs = [
        ("provider.created", "provider"),
        ("provider.updated", "provider"),
        ("provider_key.created", "provider_key"),
        ("provider_key.updated", "provider_key"),
        ("virtual_key.created", "virtual_key"),
        ("virtual_key.updated", "virtual_key"),
        ("budget_rule.created", "budget_rule"),
        ("budget_rule.updated", "budget_rule"),
        ("dlp_rule.created", "dlp_rule"),
        ("dlp_rule.updated", "dlp_rule"),
        ("webhook_delivery.replayed", "webhook_delivery"),
        ("webhook_endpoint.secret_rotated", "webhook_endpoint"),
        ("usage_export.requested", "usage_export_job"),
        ("usage_export.downloaded", "usage_export_job"),
    ]
    assert len(audit_pairs) == 14
    assert len({object_type for _, object_type in audit_pairs}) == 8
    connection.executemany(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, object_name, before_summary_json,
          after_summary_json, changed_fields_json, client_ip, user_agent,
          request_id, trace_id, created_at
        )
        VALUES(?, ?, ?, 'admin@metahub.local', 'admin', ?, ?, ?, ?,
               '{"before":"safe"}', '{"after":"safe"}', '["status"]',
               '127.0.0.1', 'migration-test', ?, ?, '2026-07-17T09:01:00Z')
        """,
        [
            (
                f"aud_prd044_old_{index:02d}",
                seed.tenant_id,
                user_id,
                action,
                object_type,
                f"obj_prd044_{index:02d}",
                f"对象-{index:02d}",
                f"req_prd044_audit_{index:02d}",
                f"trc_prd044_audit_{index:02d}",
            )
            for index, (action, object_type) in enumerate(audit_pairs)
        ],
    )
    connection.commit()
    columns = [row["name"] for row in connection.execute("PRAGMA table_info(audit_logs)")]
    before_rows = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {', '.join(columns)} FROM audit_logs ORDER BY audit_log_id"
        ).fetchall()
    ]

    upgraded = apply_migrations(database_url)
    assert upgraded.applied == ["0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040"]
    after_up_rows = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {', '.join(columns)} FROM audit_logs ORDER BY audit_log_id"
        ).fetchall()
    ]
    assert after_up_rows == before_rows

    policy_id, _, _ = _insert_policy_migration_bundle(
        connection,
        seed.tenant_id,
        user_id,
        suffix="roundtrip",
    )
    _insert_policy_migration_event(
        connection,
        seed.tenant_id,
        user_id,
        event_id="pevt_roundtrip_created",
        event_type="policy.created",
        request_id="req_roundtrip_created",
        policy_id=policy_id,
        reason_code="policy.created",
        payload_json='{"policy_id":"pol_roundtrip","version":1}',
    )
    _insert_policy_migration_event(
        connection,
        seed.tenant_id,
        user_id,
        event_id="pevt_roundtrip_failed",
        event_type="policy.validation_failed",
        request_id="req_roundtrip_failed",
        policy_id=None,
        reason_code="policy.expression_invalid",
        payload_json='{"diagnostic_codes":["policy.expression_invalid"]}',
    )
    connection.executemany(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES(?, ?, ?, 'admin@metahub.local', 'admin', ?, 'policy', ?, '{}',
               '{"condition_hash":"safe"}', '["condition_hash"]', ?, ?,
               '2026-07-17T09:02:00Z')
        """,
        [
            (
                "aud_prd044_policy_created",
                seed.tenant_id,
                user_id,
                "policy.created",
                policy_id,
                "req_prd044_policy_created",
                "trc_prd044_policy_created",
            ),
            (
                "aud_prd044_policy_updated",
                seed.tenant_id,
                user_id,
                "policy.updated",
                policy_id,
                "req_prd044_policy_updated",
                "trc_prd044_policy_updated",
            ),
        ],
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_immutable"):
        # down 必须通过受控重建过滤 policy audit；直接 DELETE 会被既有不可变边界拒绝。
        connection.execute(
            "DELETE FROM audit_logs WHERE audit_log_id='aud_prd044_policy_created'"
        )
    connection.rollback()

    rollback_request_feedback_migration(database_url)
    quality_rollback = rollback_last_migration(database_url)
    assert quality_rollback.rolled_back == "0037"
    routing_rollback = rollback_last_migration(database_url)
    assert routing_rollback.rolled_back == "0036"
    dry_run_rollback = rollback_last_migration(database_url)
    assert dry_run_rollback.rolled_back == "0035"
    snapshot_rollback = rollback_last_migration(database_url)
    assert snapshot_rollback.rolled_back == "0034"
    rollback = rollback_last_migration(database_url)
    assert rollback.rolled_back == "0033"
    for table_name in ("policy_events", "policy_actions", "policy_rules", "policies"):
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone() is None
    assert connection.execute(
        "SELECT usage_export_job_id FROM usage_export_jobs "
        "WHERE usage_export_job_id='uex_prd044_preserved'"
    ).fetchone() is not None
    assert connection.execute(
        "SELECT usage_export_event_id FROM usage_export_events "
        "WHERE usage_export_event_id='uee_prd044_preserved'"
    ).fetchone() is not None
    assert connection.execute(
        "SELECT webhook_delivery_id FROM webhook_deliveries "
        "WHERE webhook_delivery_id='whd_00000000000000000000000000440001'"
    ).fetchone() is not None
    after_down_rows = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {', '.join(columns)} FROM audit_logs ORDER BY audit_log_id"
        ).fetchall()
    ]
    assert after_down_rows == before_rows
    audit_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_logs'"
    ).fetchone()["sql"]
    assert "usage_export.requested" in audit_sql
    assert "usage_export.downloaded" in audit_sql
    assert "policy.created" not in audit_sql
    audit_indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_logs'"
        ).fetchall()
    }
    audit_triggers = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_logs'"
        ).fetchall()
    }
    assert {
        "idx_audit_logs_tenant_time",
        "idx_audit_logs_tenant_actor_time",
        "idx_audit_logs_tenant_object_time",
        "idx_audit_logs_tenant_action_time",
        "idx_audit_logs_tenant_request",
    }.issubset(audit_indexes)
    assert audit_triggers == {
        "trg_audit_logs_owner_insert",
        "trg_audit_logs_immutable_update",
        "trg_audit_logs_immutable_delete",
    }
    parent_indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "uq_users_tenant_user_prd044" not in parent_indexes
    assert "uq_models_tenant_model_prd044" not in parent_indexes
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    reapplied = apply_migrations(database_url)
    assert reapplied.applied == ["0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040"]
    after_reapply_rows = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {', '.join(columns)} FROM audit_logs ORDER BY audit_log_id"
        ).fetchall()
    ]
    assert after_reapply_rows == before_rows
    assert table_count(database_url, "policies") == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _insert_published_policy_snapshot(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    *,
    policy_id: str,
    policy_rule_id: str,
    policy_action_id: str,
    snapshot_id: str,
    request_id: str,
) -> None:
    """插入并封印一份 v1 快照，供 0034 数据库约束和往返测试复用。

    测试只写 canonical 表达式与不可逆 hash，不写 Prompt、Response、Key 或 Token；
    header、item、event 与 head 的顺序和真实发布事务保持一致。
    """

    created_at = "2026-07-17T12:00:00Z"
    connection.execute(
        """
        INSERT INTO policy_snapshots(
          policy_snapshot_id, tenant_id, snapshot_version, schema_version, state,
          operation, previous_snapshot_id, previous_snapshot_version,
          rollback_source_snapshot_id, rollback_source_snapshot_version,
          request_id, trace_id, request_fingerprint, content_hash, artifact_hash,
          policy_count, change_reason, approval_status, created_by, created_at, published_at
        )
        VALUES(?, ?, 1, 'policy-snapshot-v1', 'building', 'publish', NULL, NULL,
               NULL, NULL, ?, ?, ?, NULL, NULL, NULL, '迁移专项发布', 'not_required',
               ?, ?, NULL)
        """,
        (
            snapshot_id,
            tenant_id,
            request_id,
            f"trc_{request_id}",
            "b" * 64,
            user_id,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO policy_snapshot_items(
          policy_snapshot_item_id, tenant_id, policy_snapshot_id, evaluation_order,
          source_policy_id, source_policy_version, policy_rule_id, policy_action_id,
          priority, dsl_version, canonical_expression, condition_hash, variables_json,
          action_type, target_model_id, created_at
        )
        VALUES(?, ?, ?, 0, ?, 1, ?, ?, 100, 'policy-dsl-v1', 'env == "prod"',
               ?, '["env"]', 'allow', NULL, ?)
        """,
        (
            f"psitem_{snapshot_id.removeprefix('psnap_')}",
            tenant_id,
            snapshot_id,
            policy_id,
            policy_rule_id,
            policy_action_id,
            "a" * 64,
            created_at,
        ),
    )
    connection.execute(
        """
        UPDATE policy_snapshots
        SET state='published', content_hash=?, artifact_hash=?, policy_count=1,
            published_at='2026-07-17T12:00:01Z'
        WHERE tenant_id=? AND policy_snapshot_id=?
        """,
        ("c" * 64, "d" * 64, tenant_id, snapshot_id),
    )
    connection.execute(
        """
        INSERT INTO policy_snapshot_heads(
          tenant_id, current_snapshot_id, current_snapshot_version, updated_by, updated_at
        )
        VALUES(?, ?, 1, ?, '2026-07-17T12:00:01Z')
        """,
        (tenant_id, snapshot_id, user_id),
    )
    connection.execute(
        """
        INSERT INTO policy_snapshot_events(
          policy_snapshot_event_id, tenant_id, policy_snapshot_id, snapshot_version,
          event_type, operation, actor_user_id, request_id, trace_id, payload_json, created_at
        )
        VALUES(?, ?, ?, 1, 'policy_snapshot.published', 'publish', ?, ?, ?,
               '{"snapshot_version":1,"operation":"publish"}',
               '2026-07-17T12:00:01Z')
        """,
        (
            f"psevt_{snapshot_id.removeprefix('psnap_')}",
            tenant_id,
            snapshot_id,
            user_id,
            request_id,
            f"trc_{request_id}",
        ),
    )


def _insert_prd045_other_tenant(connection: sqlite3.Connection, tenant_id: str) -> None:
    """创建无敏感配置的第二租户，验证复合 request_id 主键不会产生跨租户耦合。"""

    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES(?, ?, 'PRD045 其他租户', 'internal_only', 'active',
               '2026-07-17T12:00:00Z', '2026-07-17T12:00:00Z')
        """,
        (tenant_id, tenant_id.replace("_", "-")),
    )


def test_policy_snapshot_migration_enforces_seal_claim_and_composite_request_contract(
    tmp_path: Path,
) -> None:
    """PRD-045 约束专项：复合租户 FK、封印、claim 与复合请求键均由 SQLite 执行。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]
    virtual_key_id = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY virtual_key_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["virtual_key_id"]
    policy_id, rule_id, action_id = _insert_policy_migration_bundle(
        connection,
        seed.tenant_id,
        user_id,
        suffix="snapshot_contract",
    )
    _insert_published_policy_snapshot(
        connection,
        seed.tenant_id,
        user_id,
        policy_id=policy_id,
        policy_rule_id=rule_id,
        policy_action_id=action_id,
        snapshot_id="psnap_contract",
        request_id="req_snapshot_publish",
    )
    connection.commit()

    snapshot_foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(policy_snapshot_heads)"
    ).fetchall()
    claim_foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(gateway_request_claims)"
    ).fetchall()
    assert _foreign_key_column_pairs(snapshot_foreign_keys, "policy_snapshots") == {
        (
            ("tenant_id", "tenant_id"),
            ("current_snapshot_id", "policy_snapshot_id"),
            ("current_snapshot_version", "snapshot_version"),
        )
    }
    assert _foreign_key_column_pairs(claim_foreign_keys, "virtual_keys") == {
        (("tenant_id", "tenant_id"), ("virtual_key_id", "virtual_key_id"))
    }
    assert tuple(
        connection.execute(
            "SELECT state, policy_count FROM policy_snapshots "
            "WHERE tenant_id=? AND policy_snapshot_id='psnap_contract'",
            (seed.tenant_id,),
        ).fetchone()
    ) == ("published", 1)

    with pytest.raises(sqlite3.IntegrityError, match="policy_snapshot_immutable"):
        connection.execute(
            "UPDATE policy_snapshots SET change_reason='禁止改写' "
            "WHERE tenant_id=? AND policy_snapshot_id='psnap_contract'",
            (seed.tenant_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_snapshot_item_immutable"):
        connection.execute(
            "DELETE FROM policy_snapshot_items "
            "WHERE tenant_id=? AND policy_snapshot_id='psnap_contract'",
            (seed.tenant_id,),
        )

    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES(?, 'req_shared_contract', 'trc_shared_contract', ?, 1, 'current',
               '2026-07-17T12:01:00Z')
        """,
        (seed.tenant_id, virtual_key_id),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          tenant_id, request_id, trace_id, virtual_key_id, status, created_at,
          snapshot_version, policy_snapshot_status, policy_decision, policy_reason_code
        )
        VALUES(?, 'req_shared_contract', 'trc_shared_contract', ?, 'success',
               '2026-07-17T12:01:00Z', 1, 'current', 'allow', 'policy.no_match_default_allow')
        """,
        (seed.tenant_id, virtual_key_id),
    )
    with pytest.raises(sqlite3.IntegrityError, match="request_log_claim_mismatch"):
        connection.execute(
            "UPDATE request_logs SET policy_snapshot_status='lkg' "
            "WHERE tenant_id=? AND request_id='req_shared_contract'",
            (seed.tenant_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="request_claim_immutable"):
        connection.execute(
            "UPDATE gateway_request_claims SET trace_id='trc_changed' "
            "WHERE tenant_id=? AND request_id='req_shared_contract'",
            (seed.tenant_id,),
        )

    _insert_prd045_other_tenant(connection, "tenant_prd045_other")
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES('tenant_prd045_other', 'req_shared_contract', 'trc_other', NULL,
               NULL, 'disabled', '2026-07-17T12:01:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          tenant_id, request_id, trace_id, status, created_at, policy_snapshot_status
        )
        VALUES('tenant_prd045_other', 'req_shared_contract', 'trc_other', 'failed',
               '2026-07-17T12:01:00Z', 'disabled')
        """
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM request_logs WHERE request_id='req_shared_contract'"
    ).fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError, match="request_claim_snapshot_mismatch"):
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id, request_id, trace_id, virtual_key_id,
              snapshot_version, policy_snapshot_status, claimed_at
            )
            VALUES('tenant_prd045_other', 'req_cross_snapshot', 'trc_cross_snapshot', NULL,
                   1, 'current', '2026-07-17T12:01:00Z')
            """
        )

    connection.rollback()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_policy_snapshot_migration_round_trip_preserves_logs_usage_audit_and_webhooks(
    tmp_path: Path,
) -> None:
    """PRD-045 往返：0033 历史逐字段保真，down 只清理快照专属审计与 Webhook。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_policy_snapshot_migration(database_url)
    connection = connect_database(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["user_id"]
    virtual_key_id = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY virtual_key_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["virtual_key_id"]
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, status, created_at,
          endpoint, request_model, input_tokens, output_tokens, total_tokens,
          estimated_cost_amount, currency, dlp_match_count, fallback_triggered,
          routing_attempt_count
        )
        VALUES('req_prd045_history', ?, 'trc_prd045_history', ?, 'success',
               '2026-07-17T12:10:00Z', '/v1/chat/completions', 'safe-model',
               3, 2, 5, 0.01, 'CNY', 0, 0, 0)
        """,
        (seed.tenant_id, virtual_key_id),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
          raw_usage, normalized_usage, cost_amount, currency, created_at,
          total_tokens, cache_tokens, cost_cny, cost_usd, estimated, usage_source,
          virtual_key_id, endpoint, request_model
        )
        VALUES('usg_prd045_history', ?, 'req_prd045_history', 3, 2, '{}',
               '{"total_tokens":5}', 0.01, 'CNY', '2026-07-17T12:10:01Z',
               5, 0, 0.01, 0, 0, 'upstream', ?, '/v1/chat/completions', 'safe-model')
        """,
        (seed.tenant_id, virtual_key_id),
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES('aud_prd045_history', ?, ?, 'admin@metahub.local', 'admin',
               'policy.created', 'policy', 'pol_history', '{}', '{"status":"active"}',
               '["status"]', 'req_prd045_audit_history', 'trc_prd045_audit_history',
               '2026-07-17T12:10:00Z')
        """,
        (seed.tenant_id, user_id),
    )
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
          max_attempts, created_by, updated_by, created_at, updated_at
        )
        VALUES('wh_prd045_roundtrip', ?, '往返 Endpoint', 'https://example.invalid/hook',
               '["gateway.request_failed"]', 'ciphertext-only', 'whsec_safe',
               'fingerprint-safe', 'active', 5000, 3, ?, ?,
               '2026-07-17T12:10:00Z', '2026-07-17T12:10:00Z')
        """,
        (seed.tenant_id, user_id, user_id),
    )
    connection.commit()

    request_columns = [row["name"] for row in connection.execute("PRAGMA table_info(request_logs)")]
    usage_columns = [row["name"] for row in connection.execute("PRAGMA table_info(usage_ledger)")]
    price_columns = [
        row["name"] for row in connection.execute("PRAGMA table_info(price_snapshots)")
    ]
    before_request = tuple(
        connection.execute(
            f"SELECT {', '.join(request_columns)} FROM request_logs "
            "WHERE request_id='req_prd045_history'"
        ).fetchone()
    )
    before_usage = tuple(
        connection.execute(
            f"SELECT {', '.join(usage_columns)} FROM usage_ledger "
            "WHERE usage_ledger_id='usg_prd045_history'"
        ).fetchone()
    )
    before_prices = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {', '.join(price_columns)} FROM price_snapshots ORDER BY price_snapshot_id"
        ).fetchall()
    ]
    before_request_indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='request_logs' "
            "AND sql IS NOT NULL"
        )
    }
    before_request_triggers = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='request_logs'"
        )
    }
    before_usage_indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='usage_ledger' "
            "AND sql IS NOT NULL"
        )
    }
    before_usage_triggers = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='usage_ledger'"
        )
    }

    assert apply_migrations(database_url).applied == ["0034", "0035", "0036", "0037", "0038", "0039", "0040"]
    assert _foreign_key_column_pairs(
        connection.execute("PRAGMA foreign_key_list(price_snapshots)").fetchall(),
        "request_logs",
    ) == {
        (("tenant_id", "tenant_id"), ("request_id", "request_id"))
    }
    migrated_log = connection.execute(
        "SELECT policy_snapshot_status FROM request_logs "
        "WHERE tenant_id=? AND request_id='req_prd045_history'",
        (seed.tenant_id,),
    ).fetchone()
    migrated_claim = connection.execute(
        "SELECT trace_id, snapshot_version, policy_snapshot_status "
        "FROM gateway_request_claims WHERE tenant_id=? AND request_id='req_prd045_history'",
        (seed.tenant_id,),
    ).fetchone()
    assert migrated_log["policy_snapshot_status"] == "disabled"
    assert tuple(migrated_claim) == ("trc_prd045_history", None, "disabled")

    policy_id, rule_id, action_id = _insert_policy_migration_bundle(
        connection,
        seed.tenant_id,
        user_id,
        suffix="snapshot_roundtrip",
    )
    _insert_published_policy_snapshot(
        connection,
        seed.tenant_id,
        user_id,
        policy_id=policy_id,
        policy_rule_id=rule_id,
        policy_action_id=action_id,
        snapshot_id="psnap_roundtrip",
        request_id="req_snapshot_roundtrip",
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES('aud_prd045_snapshot_removed', ?, ?, 'admin@metahub.local', 'admin',
               'policy_snapshot.published', 'policy_snapshot', 'psnap_roundtrip', '{}',
               '{"snapshot_version":1}', '["snapshot_version"]',
               'req_snapshot_roundtrip', 'trc_req_snapshot_roundtrip',
               '2026-07-17T12:11:00Z')
        """,
        (seed.tenant_id, user_id),
    )
    connection.execute(
        "UPDATE webhook_endpoints SET event_types_json="
        "'[\"gateway.request_failed\",\"policy_snapshot.published\"]' "
        "WHERE webhook_endpoint_id='wh_prd045_roundtrip'"
    )
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
          event_timestamp, payload_json, status, attempt_count, max_attempts,
          delivery_kind, secret_version, created_at, updated_at
        )
        VALUES('whd_00000000000000000000000000450001', ?, 'wh_prd045_roundtrip',
               'evt_prd045_snapshot', 'policy_snapshot.published',
               '2026-07-17T12:11:00Z', '{"snapshot_version":1}', 'success', 1, 3,
               'original', 1, '2026-07-17T12:11:00Z', '2026-07-17T12:11:01Z')
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO webhook_delivery_attempts(
          webhook_delivery_attempt_id, tenant_id, webhook_delivery_id,
          webhook_endpoint_id, attempt_no, status, http_status, duration_ms, created_at
        )
        VALUES('wha_prd045_snapshot', ?, 'whd_00000000000000000000000000450001',
               'wh_prd045_roundtrip', 1, 'success', 200, 5, '2026-07-17T12:11:01Z')
        """,
        (seed.tenant_id,),
    )
    connection.commit()

    rollback_request_feedback_migration(database_url)
    quality_rollback = rollback_last_migration(database_url)
    assert quality_rollback.rolled_back == "0037"
    routing_rollback = rollback_last_migration(database_url)
    assert routing_rollback.rolled_back == "0036"
    dry_run_rollback = rollback_last_migration(database_url)
    assert dry_run_rollback.rolled_back == "0035"
    rollback = rollback_last_migration(database_url)
    assert rollback.rolled_back == "0034"
    after_request = tuple(
        connection.execute(
            f"SELECT {', '.join(request_columns)} FROM request_logs "
            "WHERE request_id='req_prd045_history'"
        ).fetchone()
    )
    after_usage = tuple(
        connection.execute(
            f"SELECT {', '.join(usage_columns)} FROM usage_ledger "
            "WHERE usage_ledger_id='usg_prd045_history'"
        ).fetchone()
    )
    after_prices = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {', '.join(price_columns)} FROM price_snapshots ORDER BY price_snapshot_id"
        ).fetchall()
    ]
    assert after_request == before_request
    assert after_usage == before_usage
    assert after_prices == before_prices
    assert _foreign_key_column_pairs(
        connection.execute("PRAGMA foreign_key_list(price_snapshots)").fetchall(),
        "request_logs",
    ) == {(("request_id", "request_id"),)}
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='request_logs' "
            "AND sql IS NOT NULL"
        )
    } == before_request_indexes
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='request_logs'"
        )
    } == before_request_triggers
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='usage_ledger' "
            "AND sql IS NOT NULL"
        )
    } == before_usage_indexes
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='usage_ledger'"
        )
    } == before_usage_triggers
    assert connection.execute(
        "SELECT audit_log_id FROM audit_logs WHERE audit_log_id='aud_prd045_history'"
    ).fetchone() is not None
    assert connection.execute(
        "SELECT audit_log_id FROM audit_logs "
        "WHERE audit_log_id='aud_prd045_snapshot_removed'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT event_types_json FROM webhook_endpoints "
        "WHERE webhook_endpoint_id='wh_prd045_roundtrip'"
    ).fetchone()[0] == '["gateway.request_failed"]'
    assert connection.execute(
        "SELECT webhook_delivery_id FROM webhook_deliveries "
        "WHERE webhook_delivery_id='whd_00000000000000000000000000450001'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='policy_snapshots'"
    ).fetchone() is None
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    reapplied = apply_migrations(database_url)
    assert reapplied.applied == ["0034", "0035", "0036", "0037", "0038", "0039", "0040"]
    assert connection.execute(
        "SELECT policy_snapshot_status FROM gateway_request_claims "
        "WHERE tenant_id=? AND request_id='req_prd045_history'",
        (seed.tenant_id,),
    ).fetchone()[0] == "disabled"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_policy_snapshot_down_stops_before_loss_on_cross_tenant_request_collision(
    tmp_path: Path,
) -> None:
    """PRD-045 回滚异常：合法复合 ID 无法降级时应整体回滚，不得静默丢日志或 Usage。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    rollback_request_feedback_migration(database_url)
    quality_rollback = rollback_last_migration(database_url)
    assert quality_rollback.rolled_back == "0037"
    routing_rollback = rollback_last_migration(database_url)
    assert routing_rollback.rolled_back == "0036"
    dry_run_rollback = rollback_last_migration(database_url)
    assert dry_run_rollback.rolled_back == "0035"
    _insert_prd045_other_tenant(connection, "tenant_prd045_down_other")
    for tenant_id, trace_id in (
        (seed.tenant_id, "trc_down_default"),
        ("tenant_prd045_down_other", "trc_down_other"),
    ):
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id, request_id, trace_id, snapshot_version,
              policy_snapshot_status, claimed_at
            )
            VALUES(?, 'req_prd045_down_collision', ?, NULL, 'disabled',
                   '2026-07-17T12:20:00Z')
            """,
            (tenant_id, trace_id),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              tenant_id, request_id, trace_id, status, created_at, policy_snapshot_status
            )
            VALUES(?, 'req_prd045_down_collision', ?, 'failed',
                   '2026-07-17T12:20:00Z', 'disabled')
            """,
            (tenant_id, trace_id),
        )
    connection.commit()

    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url)
    assert exc.value.code == "db.migration_failed"
    assert table_count(database_url, "policy_snapshots") == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM request_logs WHERE request_id='req_prd045_down_collision'"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0034'"
    ).fetchone()[0] == "applied"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _policy_dry_run_event_payload(
    *,
    request_id: str,
    policy_id: str | None,
    explanation_id: str = "pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
) -> str:
    """构造迁移测试使用的精确安全事件摘要，不包含 Prompt、DSL 或预算余额。"""

    return json.dumps(
        {
            "policy_explanation_id": explanation_id,
            "request_id": request_id,
            "policy_set_fingerprint": "b" * 64,
            "decision": "allow",
            "decision_reason_code": "policy.rule_matched" if policy_id else "policy.default_allow",
            "matched_policy_id": policy_id,
            "route_status": "selected",
            "dlp_status": "clean",
            "budget_status": "within_limit",
            "would_error_code": None,
            "checked_policy_count": 1 if policy_id else 0,
            "runtime_compatible": True,
            "coverage_complete": False,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _insert_policy_explanation_fact(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    *,
    explanation_id: str = "pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    request_id: str = "req_prd046_contract",
    policy_id: str | None = None,
) -> None:
    """写入 PRD-046 的不可变摘要行，所有输入正文仍只存在于测试函数栈。"""

    connection.execute(
        """
        INSERT INTO policy_explanations(
          policy_explanation_id, tenant_id, actor_user_id, request_id, trace_id,
          status, version, policy_source, policy_set_fingerprint,
          published_snapshot_version, decision, reason_code,
          matched_policy_id, matched_priority, matched_action,
          route_status, dlp_status, budget_status, would_error_code,
          checked_policy_count, runtime_compatible, coverage_complete,
          explanation_truncated, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'completed', 1, 'active_candidate', ?,
               NULL, 'allow', ?, ?, ?, ?, 'selected', 'clean', 'within_limit', NULL,
               ?, 1, 0, 0, '2026-07-18T12:00:00Z', '2026-07-18T12:00:00Z')
        """,
        (
            explanation_id,
            tenant_id,
            user_id,
            request_id,
            f"trc_{request_id}",
            "b" * 64,
            "policy.rule_matched" if policy_id else "policy.default_allow",
            policy_id,
            100 if policy_id else None,
            "allow" if policy_id else None,
            1 if policy_id else 0,
        ),
    )


def _policy_dry_run_audit_after(policy_id: str | None) -> dict[str, object]:
    """返回审计层允许的完整固定字段集；可空字段也显式保留。"""

    return {
        "policy_explanation_id": "pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "policy_set_fingerprint": "b" * 64,
        "decision": "allow",
        "decision_reason_code": "policy.rule_matched" if policy_id else "policy.default_allow",
        "matched_policy_id": policy_id,
        "matched_priority": 100 if policy_id else None,
        "matched_action": "allow" if policy_id else None,
        "route_status": "selected",
        "dlp_status": "clean",
        "budget_status": "within_limit",
        "would_error_code": None,
        "checked_policy_count": 1 if policy_id else 0,
        "runtime_compatible": True,
        "coverage_complete": False,
    }


def test_policy_dry_run_migration_enforces_safe_fact_contract(tmp_path: Path) -> None:
    """PRD-046 正向迁移：三类安全事实的字段、语义、唯一键与不可变边界均由 DB 兜底。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = connection.execute(
        "SELECT user_id, email, role FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    policy_id, _, _ = _insert_policy_migration_bundle(
        connection,
        seed.tenant_id,
        actor["user_id"],
        suffix="dry_run_contract",
    )
    _insert_policy_explanation_fact(
        connection,
        seed.tenant_id,
        actor["user_id"],
        policy_id=policy_id,
    )
    _insert_policy_migration_event(
        connection,
        seed.tenant_id,
        actor["user_id"],
        event_id="pevt_prd046_contract",
        event_type="policy.dry_run_executed",
        request_id="req_prd046_contract",
        policy_id=policy_id,
        reason_code="policy.dry_run_executed",
        payload_json=_policy_dry_run_event_payload(
            request_id="req_prd046_contract", policy_id=policy_id
        ),
    )
    audit_after = _policy_dry_run_audit_after(policy_id)
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES('aud_prd046_contract', ?, ?, ?, ?, 'policy.dry_run_executed',
               'policy_explanation', 'pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '{}', ?, ?,
               'req_prd046_contract', 'trc_req_prd046_contract', '2026-07-18T12:00:00Z')
        """,
        (
            seed.tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            json.dumps(audit_after, sort_keys=True, separators=(",", ":")),
            json.dumps(sorted(audit_after), separators=(",", ":")),
        ),
    )
    connection.commit()

    assert [
        row["name"] for row in connection.execute("PRAGMA table_info(policy_explanations)")
    ] == [
        "policy_explanation_id",
        "tenant_id",
        "actor_user_id",
        "request_id",
        "trace_id",
        "status",
        "version",
        "policy_source",
        "policy_set_fingerprint",
        "published_snapshot_version",
        "decision",
        "reason_code",
        "matched_policy_id",
        "matched_priority",
        "matched_action",
        "route_status",
        "dlp_status",
        "budget_status",
        "would_error_code",
        "checked_policy_count",
        "runtime_compatible",
        "coverage_complete",
        "explanation_truncated",
        "created_at",
        "updated_at",
    ]
    assert _foreign_key_column_pairs(
        connection.execute("PRAGMA foreign_key_list(policy_explanations)").fetchall(),
        "users",
    ) == {(('tenant_id', 'tenant_id'), ('actor_user_id', 'user_id'))}
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='policy_explanations'"
        )
    } == {
        "trg_policy_explanations_immutable_update",
        "trg_policy_explanations_immutable_delete",
    }
    policy_index_sql = {
        row["name"]: row["sql"]
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='policy_events'"
        )
    }
    assert "idx_policy_events_created_unique" in policy_index_sql
    assert "idx_policy_events_validation_request_unique" in policy_index_sql
    assert "idx_policy_events_dry_run_request_unique" in policy_index_sql
    assert "WHERE event_type = 'policy.dry_run_executed'" in policy_index_sql[
        "idx_policy_events_dry_run_request_unique"
    ]
    audit_index_sql = {
        row["name"]: row["sql"]
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='audit_logs'"
        )
    }
    # sqlite_master 不保证运算符两侧空白，移除空白后仍精确校验部分唯一索引谓词。
    assert "WHEREaction='policy.dry_run_executed'" in "".join(
        audit_index_sql["idx_audit_logs_dry_run_request_unique"].split()
    )
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='policy_events'"
        )
    } == {
        "trg_policy_events_owner_insert",
        "trg_policy_events_immutable_update",
        "trg_policy_events_immutable_delete",
    }
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_logs'"
        )
    } == {
        "trg_audit_logs_owner_insert",
        "trg_audit_logs_immutable_update",
        "trg_audit_logs_immutable_delete",
        "trg_audit_logs_prd050_conflict_insert",
        "trg_audit_logs_prd050_rowid_insert",
    }

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_policy_explanation_fact(
            connection,
            seed.tenant_id,
            actor["user_id"],
            explanation_id="pexp_cccccccccccccccccccccccccccccccc",
            policy_id=policy_id,
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        bad_payload = json.loads(
            _policy_dry_run_event_payload(request_id="req_prd046_bad_payload", policy_id=None)
        )
        bad_payload["prompt_length"] = 10
        _insert_policy_migration_event(
            connection,
            seed.tenant_id,
            actor["user_id"],
            event_id="pevt_prd046_bad_payload",
            event_type="policy.dry_run_executed",
            request_id="req_prd046_bad_payload",
            policy_id=None,
            reason_code="policy.dry_run_executed",
            payload_json=json.dumps(bad_payload, separators=(",", ":")),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_policy_migration_event(
            connection,
            seed.tenant_id,
            actor["user_id"],
            event_id="pevt_prd046_bad_reason",
            event_type="policy.dry_run_executed",
            request_id="req_prd046_bad_reason",
            policy_id=None,
            reason_code="policy.default_allow",
            payload_json=_policy_dry_run_event_payload(
                request_id="req_prd046_bad_reason", policy_id=None
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_policy_migration_event(
            connection,
            seed.tenant_id,
            actor["user_id"],
            event_id="pevt_prd046_duplicate",
            event_type="policy.dry_run_executed",
            request_id="req_prd046_contract",
            policy_id=policy_id,
            reason_code="policy.dry_run_executed",
            payload_json=_policy_dry_run_event_payload(
                request_id="req_prd046_contract", policy_id=policy_id
            ),
        )
    # 0040 在部分唯一索引前统一阻断 Audit REPLACE，避免依赖 SQLite 冲突算法的隐式删除。
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_immutable"):
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
              action, object_type, object_id, before_summary_json, after_summary_json,
              changed_fields_json, request_id, trace_id, created_at
            )
            VALUES('aud_prd046_duplicate', ?, ?, ?, ?, 'policy.dry_run_executed',
                   'policy_explanation', 'pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '{}', ?, ?,
                   'req_prd046_contract', 'trc_req_prd046_duplicate', '2026-07-18T12:00:01Z')
            """,
            (
                seed.tenant_id,
                actor["user_id"],
                actor["email"],
                actor["role"],
                json.dumps(audit_after, sort_keys=True, separators=(",", ":")),
                json.dumps(sorted(audit_after), separators=(",", ":")),
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert_policy_explanation_fact(
            connection,
            seed.tenant_id,
            "user_prd046_missing",
            explanation_id="pexp_dddddddddddddddddddddddddddddddd",
            request_id="req_prd046_bad_actor",
            policy_id=None,
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_explanation_immutable"):
        connection.execute(
            "UPDATE policy_explanations SET decision='deny' "
            "WHERE policy_explanation_id='pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="policy_explanation_immutable"):
        connection.execute(
            "DELETE FROM policy_explanations "
            "WHERE policy_explanation_id='pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'"
        )
    connection.rollback()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_policy_dry_run_migration_round_trip_preserves_0034_and_cleans_json(
    tmp_path: Path,
) -> None:
    """PRD-046 往返：旧事实逐字段保真，down 仅清理 Dry-Run 事件、审计与订阅值。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    rollback_quality_cost_migration(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0036"
    assert rollback_last_migration(database_url).rolled_back == "0035"
    actor = connection.execute(
        "SELECT user_id, email, role FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    policy_id, _, _ = _insert_policy_migration_bundle(
        connection,
        seed.tenant_id,
        actor["user_id"],
        suffix="dry_run_roundtrip",
    )
    _insert_policy_migration_event(
        connection,
        seed.tenant_id,
        actor["user_id"],
        event_id="pevt_prd046_old_preserved",
        event_type="policy.created",
        request_id="req_prd046_old_event",
        policy_id=policy_id,
        reason_code="policy.created",
        payload_json='{"status":"active"}',
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES('aud_prd046_old_preserved', ?, ?, ?, ?, 'policy_snapshot.published',
               'policy_snapshot', 'psnap_prd046_history', '{}', '{"snapshot_version":1}',
               '["snapshot_version"]', 'req_prd046_old_audit', 'trc_prd046_old_audit',
               '2026-07-18T11:00:00Z')
        """,
        (seed.tenant_id, actor["user_id"], actor["email"], actor["role"]),
    )
    connection.executemany(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
          max_attempts, created_by, updated_by, created_at, updated_at
        )
        VALUES(?, ?, ?, 'https://example.invalid/hook', ?, 'ciphertext-only',
               'whsec_safe', ?, 'active', 5000, 3, ?, ?,
               '2026-07-18T11:00:00Z', '2026-07-18T11:00:00Z')
        """,
        [
            (
                "wh_prd046_mixed",
                seed.tenant_id,
                "混合订阅",
                '["gateway.request_failed","audit.recorded"]',
                "fingerprint-mixed",
                actor["user_id"],
                actor["user_id"],
            ),
            (
                "wh_prd046_only",
                seed.tenant_id,
                "单事件订阅",
                '["audit.recorded"]',
                "fingerprint-only",
                actor["user_id"],
                actor["user_id"],
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
          event_timestamp, payload_json, status, attempt_count, max_attempts,
          delivery_kind, secret_version, created_at, updated_at
        )
        VALUES('whd_00000000000000000000000000460001', ?, 'wh_prd046_mixed',
               'evt_prd046_old_preserved', 'gateway.request_failed',
               '2026-07-18T11:00:00Z', '{}', 'success', 1, 3, 'original', 1,
               '2026-07-18T11:00:00Z', '2026-07-18T11:00:01Z')
        """,
        (seed.tenant_id,),
    )
    connection.commit()
    event_columns = [row["name"] for row in connection.execute("PRAGMA table_info(policy_events)")]
    audit_columns = [row["name"] for row in connection.execute("PRAGMA table_info(audit_logs)")]
    old_event = tuple(
        connection.execute(
            f"SELECT {', '.join(event_columns)} FROM policy_events "
            "WHERE policy_event_id='pevt_prd046_old_preserved'"
        ).fetchone()
    )
    old_audit = tuple(
        connection.execute(
            f"SELECT {', '.join(audit_columns)} FROM audit_logs "
            "WHERE audit_log_id='aud_prd046_old_preserved'"
        ).fetchone()
    )

    assert apply_migrations(database_url).applied == ["0035", "0036", "0037", "0038", "0039", "0040"]
    _insert_policy_explanation_fact(
        connection,
        seed.tenant_id,
        actor["user_id"],
        policy_id=policy_id,
    )
    _insert_policy_migration_event(
        connection,
        seed.tenant_id,
        actor["user_id"],
        event_id="pevt_prd046_roundtrip_removed",
        event_type="policy.dry_run_executed",
        request_id="req_prd046_contract",
        policy_id=policy_id,
        reason_code="policy.dry_run_executed",
        payload_json=_policy_dry_run_event_payload(
            request_id="req_prd046_contract", policy_id=policy_id
        ),
    )
    audit_after = _policy_dry_run_audit_after(policy_id)
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES('aud_prd046_roundtrip_removed', ?, ?, ?, ?, 'policy.dry_run_executed',
               'policy_explanation', 'pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '{}', ?, ?,
               'req_prd046_contract', 'trc_req_prd046_contract', '2026-07-18T12:00:00Z')
        """,
        (
            seed.tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            json.dumps(audit_after, sort_keys=True, separators=(",", ":")),
            json.dumps(sorted(audit_after), separators=(",", ":")),
        ),
    )
    connection.execute(
        "UPDATE webhook_endpoints SET event_types_json="
        "'[\"gateway.request_failed\",\"policy.dry_run_executed\",\"audit.recorded\"]' "
        "WHERE webhook_endpoint_id='wh_prd046_mixed'"
    )
    connection.execute(
        "UPDATE webhook_endpoints SET event_types_json='[\"policy.dry_run_executed\"]' "
        "WHERE webhook_endpoint_id='wh_prd046_only'"
    )
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
          event_timestamp, payload_json, status, attempt_count, max_attempts,
          delivery_kind, secret_version, created_at, updated_at
        )
        VALUES('whd_00000000000000000000000000460002', ?, 'wh_prd046_mixed', 'evt_pevt_prd046_roundtrip_removed',
               'policy.dry_run_executed', '2026-07-18T12:00:00Z', '{}', 'success', 1, 3,
               'original', 1, '2026-07-18T12:00:00Z', '2026-07-18T12:00:01Z')
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO webhook_delivery_attempts(
          webhook_delivery_attempt_id, tenant_id, webhook_delivery_id,
          webhook_endpoint_id, attempt_no, status, http_status, duration_ms, created_at
        )
        VALUES('wha_prd046_removed', ?, 'whd_00000000000000000000000000460002', 'wh_prd046_mixed',
               1, 'success', 200, 3, '2026-07-18T12:00:01Z')
        """,
        (seed.tenant_id,),
    )
    connection.commit()

    rollback_quality_cost_migration(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0036"
    assert rollback_last_migration(database_url).rolled_back == "0035"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='policy_explanations'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT policy_event_id FROM policy_events "
        "WHERE policy_event_id='pevt_prd046_roundtrip_removed'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT audit_log_id FROM audit_logs "
        "WHERE audit_log_id='aud_prd046_roundtrip_removed'"
    ).fetchone() is None
    assert tuple(
        connection.execute(
            f"SELECT {', '.join(event_columns)} FROM policy_events "
            "WHERE policy_event_id='pevt_prd046_old_preserved'"
        ).fetchone()
    ) == old_event
    assert tuple(
        connection.execute(
            f"SELECT {', '.join(audit_columns)} FROM audit_logs "
            "WHERE audit_log_id='aud_prd046_old_preserved'"
        ).fetchone()
    ) == old_audit
    subscriptions = {
        row["webhook_endpoint_id"]: row["event_types_json"]
        for row in connection.execute(
            "SELECT webhook_endpoint_id, event_types_json FROM webhook_endpoints "
            "WHERE webhook_endpoint_id LIKE 'wh_prd046_%'"
        )
    }
    assert subscriptions == {
        "wh_prd046_mixed": '["gateway.request_failed","audit.recorded"]',
        "wh_prd046_only": "[]",
    }
    assert connection.execute(
        "SELECT webhook_delivery_id FROM webhook_deliveries "
        "WHERE webhook_delivery_id='whd_00000000000000000000000000460002'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT webhook_delivery_attempt_id FROM webhook_delivery_attempts "
        "WHERE webhook_delivery_attempt_id='wha_prd046_removed'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT webhook_delivery_id FROM webhook_deliveries "
        "WHERE webhook_delivery_id='whd_00000000000000000000000000460001'"
    ).fetchone() is not None
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='policy_events' "
            "AND sql IS NOT NULL"
        )
    } == {
        "idx_policy_events_created_unique",
        "idx_policy_events_validation_request_unique",
        "idx_policy_events_tenant_time",
        "idx_policy_events_tenant_request",
    }
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='policy_events'"
        )
    } == {
        "trg_policy_events_owner_insert",
        "trg_policy_events_immutable_update",
        "trg_policy_events_immutable_delete",
    }
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_logs' "
            "AND sql IS NOT NULL"
        )
    } == {
        "idx_audit_logs_tenant_time",
        "idx_audit_logs_tenant_actor_time",
        "idx_audit_logs_tenant_object_time",
        "idx_audit_logs_tenant_action_time",
        "idx_audit_logs_tenant_request",
    }
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_logs'"
        )
    } == {
        "trg_audit_logs_owner_insert",
        "trg_audit_logs_immutable_update",
        "trg_audit_logs_immutable_delete",
    }
    policy_event_table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='policy_events'"
    ).fetchone()[0]
    audit_table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_logs'"
    ).fetchone()[0]
    assert "policy.dry_run_executed" not in policy_event_table_sql
    assert "policy.validation_failed" in policy_event_table_sql
    assert "reason_code = 'policy.created'" in policy_event_table_sql
    assert "policy.dry_run_executed" not in audit_table_sql
    assert "policy_explanation" not in audit_table_sql
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert apply_migrations(database_url).applied == ["0035", "0036", "0037", "0038", "0039", "0040"]
    assert connection.execute("SELECT COUNT(*) FROM policy_explanations").fetchone()[0] == 0
    assert tuple(
        connection.execute(
            f"SELECT {', '.join(event_columns)} FROM policy_events "
            "WHERE policy_event_id='pevt_prd046_old_preserved'"
        ).fetchone()
    ) == old_event
    assert tuple(
        connection.execute(
            f"SELECT {', '.join(audit_columns)} FROM audit_logs "
            "WHERE audit_log_id='aud_prd046_old_preserved'"
        ).fetchone()
    ) == old_audit
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_prd047_budget_reservation_outstanding_column_roundtrip(tmp_path: Path) -> None:
    """0036 分项结算列 up/down 保留原始总预留和历史行，重升后恢复 NULL 兼容语义。"""

    database_url = sqlite_url(tmp_path / "metahub-prd047-budget-reservation-roundtrip.db")
    assert apply_migrations(database_url).applied[-1] == "0040"
    rollback_quality_cost_migration(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (seed.tenant_id,),
    ).fetchone()["environment_id"]
    # 0042 会在升级前校验历史预算作用域；这里必须建立同租户真实归属，不能伪造悬空 Application。
    connection.execute(
        """
        INSERT INTO projects(
          project_id, tenant_id, project_code, name, status, created_at, updated_at
        ) VALUES('project_prd047_reservation_roundtrip', ?, 'prd047-roundtrip',
                 'PRD047 roundtrip', 'active', '2026-07-19T00:00:00Z',
                 '2026-07-19T00:00:00Z')
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO applications(
          application_id, tenant_id, project_id, application_code, name,
          status, created_at, updated_at
        ) VALUES('app_prd047_reservation_roundtrip', ?,
                 'project_prd047_reservation_roundtrip', 'prd047-roundtrip',
                 'PRD047 roundtrip', 'active', '2026-07-19T00:00:00Z',
                 '2026-07-19T00:00:00Z')
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        )
        VALUES('br_prd047_reservation_roundtrip', ?, ?, 'application',
               'app_prd047_reservation_roundtrip', 'PRD047 reservation roundtrip',
               100, 80, 100, 'block', 'active',
               '2026-07-19T00:00:00Z', '2026-07-19T00:00:00Z')
        """,
        (seed.tenant_id, environment_id),
    )
    connection.execute(
        """
        INSERT INTO budget_reservations(
          budget_reservation_id, tenant_id, environment_id, budget_rule_id, scope_type,
          scope_id, budget_date, request_id, trace_id, reserved_cost_cny,
          outstanding_cost_cny, created_at
        )
        VALUES('bres_prd047_roundtrip', ?, ?, 'br_prd047_reservation_roundtrip',
               'application', 'app_prd047_reservation_roundtrip', '2026-07-19',
               'req_prd047_reservation_roundtrip', 'trc_prd047_reservation_roundtrip',
               12.5, 4.25, '2026-07-19T00:00:00Z')
        """,
        (seed.tenant_id, environment_id),
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE budget_reservations SET outstanding_cost_cny=-1 "
            "WHERE budget_reservation_id='bres_prd047_roundtrip'"
        )
    connection.rollback()
    original = tuple(
        connection.execute(
            """
            SELECT tenant_id, environment_id, budget_rule_id, scope_type, scope_id,
                   budget_date, request_id, trace_id, reserved_cost_cny, created_at
            FROM budget_reservations WHERE budget_reservation_id='bres_prd047_roundtrip'
            """
        ).fetchone()
    )

    assert rollback_last_migration(database_url).rolled_back == "0036"
    columns_after_down = {
        row["name"] for row in connection.execute("PRAGMA table_info(budget_reservations)")
    }
    assert "outstanding_cost_cny" not in columns_after_down
    after_down = tuple(
        connection.execute(
            """
            SELECT tenant_id, environment_id, budget_rule_id, scope_type, scope_id,
                   budget_date, request_id, trace_id, reserved_cost_cny, created_at
            FROM budget_reservations WHERE budget_reservation_id='bres_prd047_roundtrip'
            """
        ).fetchone()
    )
    assert after_down == original

    actor = connection.execute(
        "SELECT user_id, email, role FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    provider_id = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["provider_id"]
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES('aud_prd047_provider_url_scrub', ?, ?, ?, ?, 'provider.updated',
               'provider', ?,
               '{"base_url":"https://old.example/v1/provider-key/token-sentinel","status":"active"}',
               '{"base_url":"https://new.example/v1/access-token/token-sentinel","status":"active"}',
               '["base_url"]', 'req_prd047_provider_url_scrub',
               'trc_prd047_provider_url_scrub', '2026-07-19T00:00:00Z')
        """,
        (
            seed.tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            provider_id,
        ),
    )
    connection.commit()

    assert apply_migrations(database_url).applied == ["0036", "0037", "0038", "0039", "0040"]
    restored = connection.execute(
        """
        SELECT reserved_cost_cny, outstanding_cost_cny
        FROM budget_reservations WHERE budget_reservation_id='bres_prd047_roundtrip'
        """
    ).fetchone()
    assert str(restored["reserved_cost_cny"]) == "12.5"
    assert restored["outstanding_cost_cny"] is None
    scrubbed_audit = connection.execute(
        """
        SELECT before_summary_json, after_summary_json
        FROM audit_logs WHERE audit_log_id='aud_prd047_provider_url_scrub'
        """
    ).fetchone()
    assert "base_url" not in scrubbed_audit["before_summary_json"]
    assert "base_url" not in scrubbed_audit["after_summary_json"]
    assert "token-sentinel" not in (
        scrubbed_audit["before_summary_json"] + scrubbed_audit["after_summary_json"]
    )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _prd048_model_metadata(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> sqlite3.Row:
    """读取 PRD-048 迁移夹具所需的同租户 Model、Provider、Alias 和管理员身份。"""

    row = connection.execute(
        """
        SELECT
          m.model_id, m.model_alias AS internal_model_id,
          p.provider_id, p.provider_code,
          a.model_alias_id, a.alias, a.environment, a.environment_id,
          rp.routing_policy_id, rp.routing_mode, rp.version AS routing_policy_version,
          u.user_id, u.email, u.role
        FROM models m
        JOIN providers p
          ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        JOIN model_aliases a
          ON a.tenant_id=m.tenant_id AND a.default_model_id=m.model_id
        JOIN model_alias_routing_policies rp
          ON rp.tenant_id=a.tenant_id AND rp.model_alias_id=a.model_alias_id
        JOIN users u
          ON u.tenant_id=m.tenant_id AND u.role='admin'
        WHERE m.tenant_id=?
        ORDER BY m.model_id, a.model_alias_id, u.user_id
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    if row is None:
        # 基础 seed 只保证 Model/Provider/User/Environment；专项显式创建 Alias，避免依赖页面夹具。
        base = connection.execute(
            """
            SELECT
              m.model_id, e.environment_id, u.user_id
            FROM models m
            JOIN environments e ON e.tenant_id=m.tenant_id AND e.code='dev'
            JOIN users u ON u.tenant_id=m.tenant_id AND u.role='admin'
            WHERE m.tenant_id=?
            ORDER BY m.model_id, u.user_id
            LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()
        assert base is not None
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id, tenant_id, alias, environment, environment_id,
              default_model_id, status, created_by, created_at, updated_at
            )
            VALUES('ma_prd048_migration_fixture', ?, 'prd048-migration-fixture',
                   'dev', ?, ?, 'active', ?,
                   '2026-07-19T00:00:00Z', '2026-07-19T00:00:00Z')
            """,
            (tenant_id, base["environment_id"], base["model_id"], base["user_id"]),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT
              m.model_id, m.model_alias AS internal_model_id,
              p.provider_id, p.provider_code,
              a.model_alias_id, a.alias, a.environment, a.environment_id,
              rp.routing_policy_id, rp.routing_mode, rp.version AS routing_policy_version,
              u.user_id, u.email, u.role
            FROM models m
            JOIN providers p
              ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
            JOIN model_aliases a
              ON a.tenant_id=m.tenant_id AND a.default_model_id=m.model_id
            JOIN model_alias_routing_policies rp
              ON rp.tenant_id=a.tenant_id AND rp.model_alias_id=a.model_alias_id
            JOIN users u ON u.tenant_id=m.tenant_id AND u.role='admin'
            WHERE m.tenant_id=? AND a.model_alias_id='ma_prd048_migration_fixture'
            """,
            (tenant_id,),
        ).fetchone()
    assert row is not None
    return row


def _insert_prd048_attempt(
    connection: sqlite3.Connection,
    metadata: sqlite3.Row,
    tenant_id: str,
    *,
    suffix: str,
    status: str,
    usage_status: str,
    latency_ms: int | None,
    error_code: str | None = None,
    error_category: str | None = None,
    completed_at: str = "2026-07-19T01:00:01Z",
) -> None:
    """写入一条安全的 0036 Attempt 终态，供 0037 精确资格分类和 preflight 测试。

    参数中的错误码与类别必须由用例显式给出，避免把 ``usage_status=unavailable``
    误当成真实上游质量资格。返回值：无；所有 ID 均绑定当前租户内冻结路由元数据。
    """

    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id, model_alias_id,
          attempt_sequence, attempt_role, trigger_type,
          model_id, internal_model_id, provider_id, provider_code,
          status, error_code, error_category, retryable, latency_ms, usage_status,
          created_at, completed_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 1, 'primary', 'initial', ?, ?, ?, ?, ?, ?, ?, ?,
               ?, ?, '2026-07-19T01:00:00Z', ?,
               '2026-07-19T01:00:01Z')
        """,
        (
            f"rta_prd048_{suffix}",
            tenant_id,
            f"req_prd048_{suffix}",
            f"trc_prd048_{suffix}",
            metadata["model_alias_id"],
            metadata["model_id"],
            metadata["internal_model_id"],
            metadata["provider_id"],
            metadata["provider_code"],
            status,
            error_code,
            error_category,
            1 if status == "failed" else 0,
            latency_ms,
            usage_status,
            completed_at,
        ),
    )


def _insert_prd048_stream_history(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    suffix: str,
    status: str,
    error_code: str | None,
    started_at: str,
    completed_at: str,
) -> None:
    """写入一条 0036 StreamSession 历史终态并遵守既有状态机。

    参数：租户、稳定后缀、终态/错误码及原始起止时间。返回值：无。关键约束：只有
    ``upstream_failed`` 可从 starting 直接终止，其余终态必须先进入 active；时间文本
    故意不在夹具层修正，以便验证 0037 对可计与排除样本采用不同 preflight 边界。
    """

    stream_session_id = f"strm_prd048_{suffix}"
    insert_test_stream_session(
        connection,
        tenant_id,
        stream_session_id=stream_session_id,
        request_id=f"req_prd048_{suffix}",
    )
    connection.execute(
        """
        UPDATE stream_sessions
        SET started_at=?, created_at='2026-07-14T10:00:00Z',
            updated_at='2026-07-14T10:00:00Z'
        WHERE tenant_id=? AND stream_session_id=?
        """,
        (started_at, tenant_id, stream_session_id),
    )
    # 非上游失败必须先证明握手成功；测试不能绕过生产状态机伪造不可达终态。
    if status != "upstream_failed":
        connection.execute(
            """
            UPDATE stream_sessions
            SET status='active', updated_at='2026-07-14T10:00:00Z'
            WHERE tenant_id=? AND stream_session_id=?
            """,
            (tenant_id, stream_session_id),
        )
    connection.execute(
        """
        UPDATE stream_sessions
        SET status=?, error_code=?, completed_at=?, updated_at='2026-07-14T10:00:00Z'
        WHERE tenant_id=? AND stream_session_id=?
        """,
        (status, error_code, completed_at, tenant_id, stream_session_id),
    )


def _prd048_database_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[tuple[object, ...], ...], dict[str, tuple[tuple[object, ...], ...]]]:
    """读取主库全部正式结构和表数据，用于证明失败 down 原子且零改写。

    参数：当前测试数据库连接。返回：按名称排序的 sqlite_master 清单和逐表行快照。
    表名只来自 sqlite_master 并进行双引号转义；TEMP guard 不属于正式库，不进入比较。
    """

    schema = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()
    )
    table_names = [
        str(row["name"])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name
            """
        ).fetchall()
    ]
    data: dict[str, tuple[tuple[object, ...], ...]] = {}
    for table_name in table_names:
        # 标识符来自数据库结构而非外部输入；仍转义引号，避免测试辅助函数制造 SQL 歧义。
        quoted_name = table_name.replace('"', '""')
        data[table_name] = tuple(
            tuple(row) for row in connection.execute(f'SELECT * FROM "{quoted_name}"').fetchall()
        )
    return schema, data


def _insert_prd048_nondefault_created_history(
    connection: sqlite3.Connection,
    metadata: sqlite3.Row,
    tenant_id: str,
) -> None:
    """构造 v2 created 非默认历史，并把当前 fixed Policy 恢复为默认配置。

    参数：0037 数据库连接、同租户策略元数据和 tenant_id。返回值：无。为复现旧写入窗口，
    只在首次 version=1 配置快照前暂时移除版本 trigger，事件仍通过正式 payload/graph trigger；
    随后恢复 trigger 并按正常 +1 合同把当前配置改回默认，确保 down 只能依靠历史事件识别事实。
    """

    assert metadata["routing_policy_version"] == 1
    trigger_row = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type='trigger' AND name='trg_routing_policies_prd048_version_update'
        """
    ).fetchone()
    assert trigger_row is not None and trigger_row["sql"] is not None
    connection.execute("DROP TRIGGER trg_routing_policies_prd048_version_update")
    try:
        connection.execute(
            """
            UPDATE model_alias_routing_policies
            SET quality_weight_bps=4000, cost_weight_bps=4000
            WHERE tenant_id=? AND routing_policy_id=?
            """,
            (tenant_id, metadata["routing_policy_id"]),
        )
    finally:
        # 无论夹具更新是否成功都恢复正式 trigger，避免测试状态缺少生产约束。
        connection.execute(str(trigger_row["sql"]))

    payload = json.dumps(
        {
            "trigger": "configuration_changed",
            "request_id": "req_prd048_created_nondefault",
            "model_alias_id": metadata["model_alias_id"],
            "routing_policy_id": metadata["routing_policy_id"],
            "routing_policy_version": 1,
            "configuration_schema_version": 2,
            "change_kinds": ["created"],
            "changed_routing_fields": [
                "alias",
                "environment",
                "environment_id",
                "status",
                "default_model_id",
                "fallback_model_id",
                "routing_mode",
                "quality_cost_config",
            ],
            "previous_mode": None,
            "routing_mode": "fixed",
            "quality_cost_config": {
                "quality_weight_bps": 4000,
                "cost_weight_bps": 4000,
                "latency_weight_bps": 2000,
                "quality_floor_bps": 6000,
                "minimum_sample_count": 20,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO routing_policy_events(
          routing_policy_event_id, tenant_id, routing_policy_id, model_alias_id,
          routing_policy_version, event_type, trigger_type, actor_user_id,
          request_id, trace_id, request_fingerprint, payload_schema_version,
          payload_json, created_at
        )
        VALUES('rpe_22222222222222222222222222222222', ?, ?, ?, 1,
               'route.policy_selected', 'configuration_changed', ?,
               'req_prd048_created_nondefault', 'trc_prd048_created_nondefault', ?, 2, ?,
               '2026-07-19T02:59:00Z')
        """,
        (
            tenant_id,
            metadata["routing_policy_id"],
            metadata["model_alias_id"],
            metadata["user_id"],
            "2" * 64,
            payload,
        ),
    )
    # 当前行恢复默认且仍为 fixed；历史事件必须继续独立阻止 down，不可只看当前 Policy。
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET quality_weight_bps=5000, cost_weight_bps=3000,
            version=version+1, updated_by=?, updated_at='2026-07-19T03:00:00Z'
        WHERE tenant_id=? AND routing_policy_id=?
        """,
        (metadata["user_id"], tenant_id, metadata["routing_policy_id"]),
    )


def test_quality_cost_migration_backfills_attempt_stream_and_round_trips(
    tmp_path: Path,
) -> None:
    """0037 主流程：精确聚合真实上游样本，旧策略保真，派生统计允许条件往返。"""

    database_url = sqlite_url(tmp_path / "metahub-prd048-history-roundtrip.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    connection = connect_database(database_url)
    metadata = _prd048_model_metadata(connection, seed.tenant_id)
    # 模拟模型创建时钟晚于历史终态；Score.updated_at 必须取 model.created_at 与观察极值的 MAX。
    connection.execute(
        """
        UPDATE models
        SET created_at='2026-07-20T00:00:00Z', updated_at='2026-07-20T00:00:00Z'
        WHERE tenant_id=? AND model_id=?
        """,
        (seed.tenant_id, metadata["model_id"]),
    )
    policy_before = tuple(
        connection.execute(
            """
            SELECT routing_policy_id, routing_mode, version, created_by, updated_by,
                   created_at, updated_at
            FROM model_alias_routing_policies WHERE routing_policy_id=?
            """,
            (metadata["routing_policy_id"],),
        ).fetchone()
    )

    # 每个 tuple 依次为后缀、终态、usage、延迟、错误码、错误类别；仅后三类可靠上游类别
    # 和新 transport 固定码可计，401/402/404/400、InvalidURL 与本地失败全部排除。
    for suffix, status, usage_status, latency_ms, error_code, error_category in (
        ("success", "succeeded", "recorded", 100, None, None),
        ("timeout", "failed", "unavailable", 200, "gateway.upstream_timeout", "timeout"),
        (
            "rate_limited",
            "failed",
            "unavailable",
            201,
            "gateway.upstream_rate_limited",
            "rate_limited",
        ),
        ("upstream_5xx", "failed", "unavailable", 202, "gateway.upstream_error", "upstream_5xx"),
        (
            "transport",
            "failed",
            "unavailable",
            203,
            "gateway.upstream_transport_failed",
            "other",
        ),
        ("auth_401", "failed", "unavailable", 300, "gateway.upstream_auth_failed", "other"),
        ("quota_402", "failed", "unavailable", 301, "gateway.upstream_quota_exceeded", "other"),
        ("model_404", "failed", "unavailable", 302, "gateway.upstream_model_not_found", "other"),
        ("request_400", "failed", "unavailable", 303, "gateway.upstream_request_invalid", "other"),
        ("invalid_url", "failed", "unavailable", 304, "gateway.upstream_unavailable", "other"),
        ("local", "failed", "not_applicable", 305, "gateway.internal_error", "other"),
    ):
        _insert_prd048_attempt(
            connection,
            metadata,
            seed.tenant_id,
            suffix=suffix,
            status=status,
            usage_status=usage_status,
            latency_ms=latency_ms,
            error_code=error_code,
            error_category=error_category,
        )
    # 合法 offset 历史允许兼容输入，但 Score 观察时间必须规范化回同一 20 位 UTC Z。
    connection.execute(
        """
        UPDATE routing_attempts SET completed_at='2026-07-19T09:00:01+08:00'
        WHERE tenant_id=? AND routing_attempt_id='rta_prd048_timeout'
        """,
        (seed.tenant_id,),
    )

    # 可计流覆盖同秒 1.6ms、跨秒 1.6ms、秒级文本，以及全部无歧义 legacy/new 错误码。
    for suffix, status, error_code, started_at, completed_at in (
        (
            "stream_success",
            "completed",
            None,
            "2026-07-14T10:00:00.000100Z",
            "2026-07-14T10:00:00.001700Z",
        ),
        (
            "stream_timed_out",
            "timed_out",
            "gateway.upstream_stream_timeout",
            "2026-07-14T10:00:00Z",
            "2026-07-14T10:00:02Z",
        ),
        (
            "stream_rate_limited",
            "upstream_failed",
            "gateway.upstream_rate_limited",
            "2026-07-14T10:00:00.100000Z",
            "2026-07-14T10:00:00.101600Z",
        ),
        (
            "stream_timeout",
            "upstream_failed",
            "gateway.upstream_timeout",
            "2026-07-14T10:00:00.200000Z",
            "2026-07-14T10:00:00.201600Z",
        ),
        (
            "stream_legacy_timeout",
            "upstream_failed",
            "gateway.upstream_stream_timeout",
            "2026-07-14T10:00:00.300000Z",
            "2026-07-14T10:00:00.301600Z",
        ),
        (
            "stream_read_failure",
            "upstream_failed",
            "gateway.upstream_stream_failed",
            "2026-07-14T10:00:00.999100Z",
            "2026-07-14T10:00:01.000700Z",
        ),
    ):
        _insert_prd048_stream_history(
            connection,
            seed.tenant_id,
            suffix=suffix,
            status=status,
            error_code=error_code,
            started_at=started_at,
            completed_at=completed_at,
        )

    # 排除样本故意携带本地格式坏时间；若分类边界泄漏，它们会触发 preflight 并使主流程失败。
    for suffix, status, error_code in (
        ("excluded_disconnect", "client_disconnected", "gateway.client_disconnected"),
        ("excluded_unavailable", "upstream_failed", "gateway.upstream_unavailable"),
        ("excluded_upstream_error", "upstream_failed", "gateway.upstream_error"),
        ("excluded_transport", "upstream_failed", "gateway.upstream_transport_failed"),
        ("excluded_auth", "upstream_failed", "gateway.upstream_auth_failed"),
        ("excluded_quota", "upstream_failed", "gateway.upstream_quota_exceeded"),
        ("excluded_model", "upstream_failed", "gateway.upstream_model_not_found"),
        ("excluded_request", "upstream_failed", "gateway.upstream_request_invalid"),
        ("excluded_processing", "upstream_failed", "gateway.stream_processing_unavailable"),
        ("excluded_session", "upstream_failed", "gateway.stream_session_unavailable"),
        ("excluded_unknown", "upstream_failed", "gateway.unknown_stream_failure"),
        ("excluded_null", "upstream_failed", None),
    ):
        _insert_prd048_stream_history(
            connection,
            seed.tenant_id,
            suffix=suffix,
            status=status,
            error_code=error_code,
            started_at="not-a-timestamp",
            completed_at="2026-07-14 10:00:00",
        )

    # 0036 v1 配置事件原文必须在 up/down/up 中逐字节保留。
    payload = json.dumps(
        {
            "trigger": "configuration_changed",
            "request_id": "req_prd048_v1_history",
            "model_alias_id": metadata["model_alias_id"],
            "routing_policy_id": metadata["routing_policy_id"],
            "routing_policy_version": metadata["routing_policy_version"],
            "change_kinds": ["created"],
            "changed_routing_fields": [
                "alias",
                "environment",
                "environment_id",
                "status",
                "default_model_id",
                "fallback_model_id",
                "routing_mode",
            ],
            "previous_mode": None,
            "routing_mode": metadata["routing_mode"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO routing_policy_events(
          routing_policy_event_id, tenant_id, routing_policy_id, model_alias_id,
          routing_policy_version, event_type, trigger_type, actor_user_id,
          request_id, trace_id, request_fingerprint, payload_json, created_at
        )
        VALUES('rpe_11111111111111111111111111111111', ?, ?, ?, ?,
               'route.policy_selected', 'configuration_changed', ?,
               'req_prd048_v1_history', 'trc_prd048_v1_history', ?, ?,
               '2026-07-19T01:00:00Z')
        """,
        (
            seed.tenant_id,
            metadata["routing_policy_id"],
            metadata["model_alias_id"],
            metadata["routing_policy_version"],
            metadata["user_id"],
            "1" * 64,
            payload,
        ),
    )
    connection.commit()

    assert apply_migrations(database_url).applied == ["0037", "0038", "0039", "0040"]
    score = connection.execute(
        "SELECT * FROM model_quality_scores WHERE tenant_id=? AND model_id=?",
        (seed.tenant_id, metadata["model_id"]),
    ).fetchone()
    assert score is not None
    assert (
        score["sample_count"],
        score["success_count"],
        score["error_count"],
        score["latency_sample_count"],
        score["total_latency_ms"],
        score["statistics_version"],
    ) == (11, 2, 9, 11, 2911, 11)
    assert score["first_observed_at"] == "2026-07-14T10:00:00Z"
    assert score["last_observed_at"] == "2026-07-19T01:00:01Z"
    assert score["updated_at"] == "2026-07-20T00:00:00Z"
    # 随机内部 ID 不可重建；其余 Score 字段都必须在空业务事实 down/up 后逐项保持。
    rebuildable_score_columns = (
        "tenant_id",
        "model_id",
        "status",
        "sample_count",
        "success_count",
        "error_count",
        "latency_sample_count",
        "total_latency_ms",
        "manual_quality_bps",
        "statistics_version",
        "feedback_version",
        "first_observed_at",
        "last_observed_at",
        "feedback_updated_by",
        "feedback_updated_at",
        "created_at",
        "updated_at",
    )
    score_before_round_trip = tuple(score[column] for column in rebuildable_score_columns)
    assert table_count(database_url, "model_quality_scores") == table_count(database_url, "models")
    policy_after = tuple(
        connection.execute(
            """
            SELECT routing_policy_id, routing_mode, version, created_by, updated_by,
                   created_at, updated_at
            FROM model_alias_routing_policies WHERE routing_policy_id=?
            """,
            (metadata["routing_policy_id"],),
        ).fetchone()
    )
    assert policy_after == policy_before
    assert tuple(
        connection.execute(
            """
            SELECT quality_weight_bps, cost_weight_bps, latency_weight_bps,
                   quality_floor_bps, minimum_sample_count
            FROM model_alias_routing_policies WHERE routing_policy_id=?
            """,
            (metadata["routing_policy_id"],),
        ).fetchone()
    ) == (5000, 3000, 2000, 6000, 20)
    event = connection.execute(
        """
        SELECT payload_schema_version, payload_json FROM routing_policy_events
        WHERE routing_policy_event_id='rpe_11111111111111111111111111111111'
        """
    ).fetchone()
    assert event["payload_schema_version"] == 1
    assert event["payload_json"] == payload

    rollback_quality_cost_migration(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='model_quality_scores'"
    ).fetchone()[0] == 0
    assert "quality_weight_bps" not in {
        row["name"] for row in connection.execute("PRAGMA table_info(model_alias_routing_policies)")
    }
    assert connection.execute(
        """
        SELECT payload_json FROM routing_policy_events
        WHERE routing_policy_event_id='rpe_11111111111111111111111111111111'
        """
    ).fetchone()[0] == payload
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert apply_migrations(database_url).applied == ["0037", "0038", "0039", "0040"]
    rebuilt = connection.execute(
        "SELECT * FROM model_quality_scores WHERE tenant_id=? AND model_id=?",
        (seed.tenant_id, metadata["model_id"]),
    ).fetchone()
    assert rebuilt is not None
    assert tuple(rebuilt[column] for column in rebuildable_score_columns) == score_before_round_trip
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_quality_cost_migration_score_and_policy_hard_constraints(tmp_path: Path) -> None:
    """0037 硬约束：统计/人工更新互斥、版本单步推进，非法权重和直接删除失败。"""

    database_url = sqlite_url(tmp_path / "metahub-prd048-hard-constraints.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _prd048_model_metadata(connection, seed.tenant_id)
    score = connection.execute(
        "SELECT * FROM model_quality_scores WHERE tenant_id=? AND model_id=?",
        (seed.tenant_id, metadata["model_id"]),
    ).fetchone()
    earlier_observed_at = canonical_utc_after(score["updated_at"], seconds=60)
    later_observed_at = canonical_utc_after(score["updated_at"], seconds=120)
    feedback_updated_at = canonical_utc_after(score["updated_at"], seconds=180)

    connection.execute(
        """
        UPDATE model_quality_scores
        SET sample_count=sample_count+1, success_count=success_count+1,
            latency_sample_count=latency_sample_count+1,
            total_latency_ms=total_latency_ms+25,
            statistics_version=statistics_version+1,
            first_observed_at=?, last_observed_at=?, updated_at=?
        WHERE tenant_id=? AND model_quality_score_id=?
        """,
        (
            later_observed_at,
            later_observed_at,
            later_observed_at,
            seed.tenant_id,
            score["model_quality_score_id"],
        ),
    )
    connection.commit()
    # 第二个样本较早到达时只能向前扩展 first；last/updated_at 必须保持既有最大值。
    connection.execute(
        """
        UPDATE model_quality_scores
        SET sample_count=sample_count+1, error_count=error_count+1,
            latency_sample_count=latency_sample_count+1,
            total_latency_ms=total_latency_ms+30,
            statistics_version=statistics_version+1,
            first_observed_at=?, last_observed_at=?, updated_at=?
        WHERE tenant_id=? AND model_quality_score_id=?
        """,
        (
            earlier_observed_at,
            later_observed_at,
            later_observed_at,
            seed.tenant_id,
            score["model_quality_score_id"],
        ),
    )
    connection.commit()
    extrema = connection.execute(
        """
        SELECT first_observed_at, last_observed_at, updated_at
        FROM model_quality_scores
        WHERE tenant_id=? AND model_quality_score_id=?
        """,
        (seed.tenant_id, score["model_quality_score_id"]),
    ).fetchone()
    assert tuple(extrema) == (
        earlier_observed_at,
        later_observed_at,
        later_observed_at,
    )
    with pytest.raises(sqlite3.IntegrityError, match="model_quality_score_transition_invalid"):
        connection.execute(
            """
            UPDATE model_quality_scores
            SET sample_count=0, success_count=0, statistics_version=0
            WHERE tenant_id=? AND model_quality_score_id=?
            """,
            (seed.tenant_id, score["model_quality_score_id"]),
        )
    connection.rollback()

    connection.execute(
        """
        UPDATE model_quality_scores
        SET manual_quality_bps=8500, feedback_version=feedback_version+1,
            feedback_updated_by=?, feedback_updated_at=?, updated_at=?
        WHERE tenant_id=? AND model_quality_score_id=?
        """,
        (
            metadata["user_id"],
            feedback_updated_at,
            feedback_updated_at,
            seed.tenant_id,
            score["model_quality_score_id"],
        ),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            UPDATE model_alias_routing_policies
            SET quality_weight_bps=9000, version=version+1
            WHERE tenant_id=? AND routing_policy_id=?
            """,
            (seed.tenant_id, metadata["routing_policy_id"]),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="model_quality_score_delete_forbidden"):
        connection.execute(
            "DELETE FROM model_quality_scores WHERE model_quality_score_id=?",
            (score["model_quality_score_id"],),
        )
    connection.rollback()


@pytest.mark.parametrize(
    "fact_kind",
    [
        "active_mode",
        "historical_decision",
        "historical_created_nondefault",
        "fixed_nondefault",
        "manual",
        "cleared_feedback",
        "subscription",
    ],
)
def test_quality_cost_down_guard_fails_before_schema_changes(
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """0037 条件回滚：任一不可降级质量事实均以固定错误原子阻断，不改写业务结构。"""

    database_url = sqlite_url(tmp_path / f"metahub-prd048-down-guard-{fact_kind}.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_request_feedback_migration(database_url)
    connection = connect_database(database_url)
    metadata = _prd048_model_metadata(connection, seed.tenant_id)

    if fact_kind in {"active_mode", "historical_decision"}:
        connection.execute(
            """
            UPDATE model_alias_routing_policies
            SET routing_mode='quality_cost', version=version+1,
                updated_by=?, updated_at='2026-07-19T03:00:00Z'
            WHERE tenant_id=? AND routing_policy_id=?
            """,
            (metadata["user_id"], seed.tenant_id, metadata["routing_policy_id"]),
        )
        current_version = metadata["routing_policy_version"] + 1
        if fact_kind == "historical_decision":
            connection.execute(
                """
                INSERT INTO routing_decisions(
                  routing_decision_id, tenant_id, request_id, trace_id,
                  model_alias_id, requested_model, environment, environment_id,
                  decision_status, decision_reason, candidate_summary, error_code, created_at,
                  routing_policy_id, routing_policy_version, routing_mode,
                  quality_weight_bps, cost_weight_bps, latency_weight_bps,
                  quality_floor_bps, minimum_sample_count, quality_as_of,
                  quality_formula_version, quality_scoring_performed
                )
                VALUES('rdec_prd048_historical_quality', ?,
                       'req_prd048_historical_quality', 'trc_prd048_historical_quality',
                       ?, ?, ?, ?, 'failed', 'routing.no_available_candidate', '{}',
                       'gateway.no_route_available', '2026-07-19T03:00:00Z', ?, ?,
                       'quality_cost', 5000, 3000, 2000, 6000, 20,
                       '2026-07-19T03:00:00Z', 'quality_cost_v1', 0)
                """,
                (
                    seed.tenant_id,
                    metadata["model_alias_id"],
                    metadata["alias"],
                    metadata["environment"],
                    metadata["environment_id"],
                    metadata["routing_policy_id"],
                    current_version,
                ),
            )
            connection.execute(
                """
                UPDATE model_alias_routing_policies
                SET routing_mode='fixed', version=version+1,
                    updated_at='2026-07-19T03:01:00Z'
                WHERE tenant_id=? AND routing_policy_id=?
                """,
                (seed.tenant_id, metadata["routing_policy_id"]),
            )
    elif fact_kind == "historical_created_nondefault":
        _insert_prd048_nondefault_created_history(connection, metadata, seed.tenant_id)
    elif fact_kind == "fixed_nondefault":
        # fixed 模式下的休眠配置仍是管理员业务事实，不能因尚无 v2 event 就被 down 裁掉。
        connection.execute(
            """
            UPDATE model_alias_routing_policies
            SET quality_weight_bps=4000, cost_weight_bps=4000,
                version=version+1, updated_by=?, updated_at='2026-07-19T03:00:00Z'
            WHERE tenant_id=? AND routing_policy_id=?
            """,
            (metadata["user_id"], seed.tenant_id, metadata["routing_policy_id"]),
        )
    elif fact_kind in {"manual", "cleared_feedback"}:
        score = connection.execute(
            "SELECT updated_at FROM model_quality_scores WHERE tenant_id=? AND model_id=?",
            (seed.tenant_id, metadata["model_id"]),
        ).fetchone()
        manual_time = canonical_utc_after(score["updated_at"], seconds=60)
        cleared_time = canonical_utc_after(score["updated_at"], seconds=120)
        connection.execute(
            """
            UPDATE model_quality_scores
            SET manual_quality_bps=7000, feedback_version=feedback_version+1,
                feedback_updated_by=?, feedback_updated_at=?, updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (
                metadata["user_id"],
                manual_time,
                manual_time,
                seed.tenant_id,
                metadata["model_id"],
            ),
        )
        if fact_kind == "cleared_feedback":
            # 即使人工值已清空，推进过的版本和 actor/time 仍是不可降级业务事实。
            connection.execute(
                """
                UPDATE model_quality_scores
                SET manual_quality_bps=NULL, feedback_version=feedback_version+1,
                    feedback_updated_by=?, feedback_updated_at=?, updated_at=?
                WHERE tenant_id=? AND model_id=?
                """,
                (
                    metadata["user_id"],
                    cleared_time,
                    cleared_time,
                    seed.tenant_id,
                    metadata["model_id"],
                ),
            )
    else:
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
              encrypted_secret, secret_prefix, secret_fingerprint, status,
              created_by, updated_by, created_at, updated_at
            )
            VALUES('wh_prd048_feedback_guard', ?, 'PRD048 Feedback Guard',
                   'https://example.invalid/prd048-feedback', '["model.feedback_recorded"]',
                   'encrypted', 'whsec_guard', 'fingerprint_guard', 'active', ?, ?,
                   '2026-07-19T03:00:00Z', '2026-07-19T03:00:00Z')
            """,
            (seed.tenant_id, metadata["user_id"], metadata["user_id"]),
        )
    connection.commit()

    database_before = _prd048_database_snapshot(connection)
    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd048_down_business_facts_present" in exc.value.message
    assert _prd048_database_snapshot(connection) == database_before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0037'"
    ).fetchone()[0] == "applied"


def test_quality_cost_migration_preflight_rejects_bad_historical_latency(tmp_path: Path) -> None:
    """0037 异常流程：可计数历史缺少 latency 时在换表前整体失败，0036 业务结构不变。"""

    database_url = sqlite_url(tmp_path / "metahub-prd048-bad-history.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    connection = connect_database(database_url)
    metadata = _prd048_model_metadata(connection, seed.tenant_id)
    _insert_prd048_attempt(
        connection,
        metadata,
        seed.tenant_id,
        suffix="bad_latency",
        status="failed",
        usage_status="unavailable",
        latency_ms=None,
        error_code="gateway.upstream_timeout",
        error_category="timeout",
    )
    connection.commit()
    policy_sql_before = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='model_alias_routing_policies'"
    ).fetchone()[0]

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url)
    assert exc.value.code == "db.migration_failed"
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0037'"
    ).fetchone()[0] == "failed"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='model_quality_scores'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='model_alias_routing_policies'"
    ).fetchone()[0] == policy_sql_before


def test_quality_cost_migration_preflight_rejects_conflicting_attempt_usage(tmp_path: Path) -> None:
    """0037 Attempt 资格 preflight：白名单上游错误与本地 usage 语义冲突时失败关闭。

    参数：pytest 临时目录。返回值：无。timeout + not_applicable 不能被回填，也不能静默
    忽略后丢失损坏事实；迁移必须在正式业务结构变化前留下统一失败状态。
    """

    database_url = sqlite_url(tmp_path / "metahub-prd048-conflicting-attempt-usage.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    connection = connect_database(database_url)
    metadata = _prd048_model_metadata(connection, seed.tenant_id)
    _insert_prd048_attempt(
        connection,
        metadata,
        seed.tenant_id,
        suffix="conflicting_usage",
        status="failed",
        usage_status="not_applicable",
        latency_ms=10,
        error_code="gateway.upstream_timeout",
        error_category="timeout",
    )
    connection.commit()
    schema_before = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()
    )

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url)
    assert exc.value.code == "db.migration_failed"
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0037'"
    ).fetchone()[0] == "failed"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='model_quality_scores'"
    ).fetchone()[0] == 0
    assert schema_before == tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()
    )


def test_quality_cost_migration_preflight_rejects_unparseable_attempt_observed_time(
    tmp_path: Path,
) -> None:
    """0037 Attempt 时间 preflight：SQLite 特殊日期不得生成 Control API 无法解析的年份。

    参数：pytest 临时目录。返回值：无。``completed_at='0'`` 虽可被 SQLite 日期函数
    解释为儒略日，却无法规范化为年份 0001..9999 的 20 位 UTC Z，必须在业务 DDL 前失败。
    """

    database_url = sqlite_url(tmp_path / "metahub-prd048-bad-attempt-time.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    connection = connect_database(database_url)
    metadata = _prd048_model_metadata(connection, seed.tenant_id)
    _insert_prd048_attempt(
        connection,
        metadata,
        seed.tenant_id,
        suffix="bad_observed_time",
        status="succeeded",
        usage_status="recorded",
        latency_ms=10,
        completed_at="0",
    )
    connection.commit()
    schema_before = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()
    )

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url)
    assert exc.value.code == "db.migration_failed"
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0037'"
    ).fetchone()[0] == "failed"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='model_quality_scores'"
    ).fetchone()[0] == 0
    assert schema_before == tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()
    )


@pytest.mark.parametrize(
    ("started_at", "completed_at"),
    [
        ("2026-07-19 10:00:00", "2026-07-19T10:00:01Z"),
        ("2026-07-19T10:00:00Z", "2026-07-19T10:00:01+08:00"),
        ("2026-07-19T10:00:00.123Z", "2026-07-19T10:00:01.123456Z"),
        ("2026-02-30T10:00:00Z", "2026-03-03T10:00:00Z"),
        ("2026-07-19T23:59:59Z", "2026-07-19T24:00:00Z"),
        ("2026-07-19T10:00:01.000000Z", "2026-07-19T10:00:00.999999Z"),
    ],
    ids=("local", "offset", "fraction", "invalid-date", "invalid-time", "reverse"),
)
def test_quality_cost_migration_preflight_rejects_noncanonical_stream_time(
    tmp_path: Path,
    started_at: str,
    completed_at: str,
) -> None:
    """0037 时间 preflight：可计流只接受两种 UTC Z 文本且整数微秒差必须非负。

    参数由 pytest 覆盖本地格式、offset、非六位小数、非法日历/时间和规范逆序。
    返回值：无；失败必须发生在任何正式业务结构变化之前。
    """

    database_url = sqlite_url(tmp_path / "metahub-prd048-bad-stream-time.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_quality_cost_migration(database_url)
    connection = connect_database(database_url)
    _insert_prd048_stream_history(
        connection,
        seed.tenant_id,
        suffix="bad_timestamp",
        status="completed",
        error_code=None,
        started_at=started_at,
        completed_at=completed_at,
    )
    connection.commit()
    schema_before = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()
    )

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url)
    assert exc.value.code == "db.migration_failed"
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0037'"
    ).fetchone()[0] == "failed"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='model_quality_scores'"
    ).fetchone()[0] == 0
    assert schema_before == tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()
    )


@pytest.mark.parametrize(
    "unrebuildable_kind",
    ["score_without_source", "bad_stream_time", "bad_attempt_time", "bad_attempt_usage"],
)
def test_quality_cost_down_rejects_unrebuildable_automatic_statistics_atomically(
    tmp_path: Path,
    unrebuildable_kind: str,
) -> None:
    """0037 down：合法形态但无源增量的自动 Score 必须以固定错误原子拒绝。

    参数：pytest 临时目录。返回值：无。测试比较全部正式结构和逐表数据，证明 guard
    在任何业务 DDL/DML 前失败，且 0037 迁移状态、外键和数据库完整性均保持不变。
    """

    database_url = sqlite_url(tmp_path / "metahub-prd048-down-unrebuildable-score.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    rollback_request_feedback_migration(database_url)
    connection = connect_database(database_url)
    metadata = _prd048_model_metadata(connection, seed.tenant_id)
    if unrebuildable_kind == "score_without_source":
        score = connection.execute(
            "SELECT updated_at FROM model_quality_scores WHERE tenant_id=? AND model_id=?",
            (seed.tenant_id, metadata["model_id"]),
        ).fetchone()
        observed_at = canonical_utc_after(score["updated_at"], seconds=60)
        connection.execute(
            """
            UPDATE model_quality_scores
            SET sample_count=sample_count+1, success_count=success_count+1,
                latency_sample_count=latency_sample_count+1,
                total_latency_ms=total_latency_ms+25,
                statistics_version=statistics_version+1,
                first_observed_at=?, last_observed_at=?, updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (
                observed_at,
                observed_at,
                observed_at,
                seed.tenant_id,
                metadata["model_id"],
            ),
        )
    elif unrebuildable_kind == "bad_stream_time":
        # 0037 之后出现的可计坏源时间也必须走同一固定错误，不能在删除 Score 后才失败。
        _insert_prd048_stream_history(
            connection,
            seed.tenant_id,
            suffix="down_bad_time",
            status="completed",
            error_code=None,
            started_at="2026-07-19 04:00:00",
            completed_at="2026-07-19 04:00:01",
        )
    elif unrebuildable_kind == "bad_attempt_time":
        _insert_prd048_attempt(
            connection,
            metadata,
            seed.tenant_id,
            suffix="down_bad_time",
            status="succeeded",
            usage_status="recorded",
            latency_ms=10,
            completed_at="0",
        )
    else:
        _insert_prd048_attempt(
            connection,
            metadata,
            seed.tenant_id,
            suffix="down_bad_usage",
            status="failed",
            usage_status="not_applicable",
            latency_ms=10,
            error_code="gateway.upstream_timeout",
            error_category="timeout",
        )
    connection.commit()
    before = _prd048_database_snapshot(connection)

    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd048_down_automatic_statistics_unrebuildable" in exc.value.message
    assert _prd048_database_snapshot(connection) == before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0037'"
    ).fetchone()[0] == "applied"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
