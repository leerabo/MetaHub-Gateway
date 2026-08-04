"""PRD-071 0063 输出安全事实、流终态与可逆迁移专项测试。"""

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

# 专项测试始终复制正式迁移文件构造 0062 模板，避免手写测试 schema 与生产 SQL 漂移。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
FINGERPRINT = "a" * 64
EMPTY_RULESET_FINGERPRINT = hashlib.sha256(b"[]").hexdigest()
STARTED_AT = "2026-08-04T08:00:00Z"
COMPLETED_AT = "2026-08-04T08:00:01Z"


def _url(path: Path) -> str:
    """返回隔离 SQLite 文件的项目 URL。

    参数：pytest 专用数据库路径。返回：迁移运行器可解析的 URL。
    关键约束：不得回落到共享开发库，避免专项测试污染实际验收数据。
    """

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不高于指定版本的正式 up/down 迁移。

    参数：目标目录和最高版本。返回：只含目标版本范围的目录。
    关键约束：up/down 必须成对保留，确保回滚测试执行的就是交付 SQL。
    """

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd071_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """构造带真实 seed 的 0062 模板和 0063 正式迁移目录。

    参数：pytest 模块临时目录工厂。返回：关闭状态模板库与两个迁移目录。
    关键约束：seed 仍在仓库约定的 0053 节点执行，再逐条升级到 0062。
    """

    root = tmp_path_factory.mktemp("prd071-migration-templates")
    migrations_0053 = _migration_subset(root / "migrations-0053", 53)
    migrations_0062 = _migration_subset(root / "migrations-0062", 62)
    migrations_0063 = _migration_subset(root / "migrations-0063", 63)
    database_0062 = root / "template-0062.db"
    database_url = _url(database_0062)
    assert apply_migrations(
        database_url, migrations_dir=migrations_0053
    ).applied[-1] == "0053"
    apply_seed_data(database_url)
    assert apply_migrations(
        database_url, migrations_dir=migrations_0062
    ).applied[-1] == "0062"
    return {
        "database_0062": database_0062,
        "migrations_0063": migrations_0063,
    }


def _clone(template: Path, destination: Path) -> str:
    """复制已关闭的 0062 模板并返回用例专属 URL。

    参数：模板和目标文件。返回：目标 SQLite URL。
    关键约束：每个用例独占文件，失败事务不能影响其他权限或异常用例。
    """

    shutil.copy2(template, destination)
    return _url(destination)


def _apply_0063(database_url: str, migrations: Path) -> None:
    """只应用 0063 并断言迁移运行器没有越过目标版本。

    参数：数据库 URL 和包含 0063 的迁移目录。返回：无。
    关键约束：若意外应用多个版本，测试立即失败而不是掩盖迁移边界。
    """

    result = apply_migrations(database_url, migrations_dir=migrations)
    assert result.applied == ["0063"]


def _query_event_keys_trigger_sql(connection: sqlite3.Connection) -> str:
    """读取查询审计字段白名单触发器的规范 SQL。

    参数：当前迁移测试连接。返回：SQLite 保存的非空触发器 SQL。
    关键约束：触发器缺失必须立即失败，不能把应用层筛选通过误判为审计合同已迁移。
    """

    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' "
        "AND name='trg_request_log_query_events_keys_insert_prd055'"
    ).fetchone()
    assert row is not None and isinstance(row["sql"], str)
    return str(row["sql"])


def _identity(
    connection: sqlite3.Connection,
    *,
    excluded_tenant_id: str | None = None,
) -> dict[str, str]:
    """读取一个完整同租户用户、Key、环境、模型和 Provider 图。

    参数：数据库连接和可选排除租户。返回：仅含稳定 ID/code 的夹具字典。
    关键约束：每个 JOIN 均携带 tenant_id，测试数据本身不能制造横向拼接。
    """

    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,user.email,user.role,
               key.virtual_key_id,environment.environment_id,
               environment.code AS environment_code,
               model.model_id,provider.provider_id,provider.provider_code
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN virtual_keys key ON key.tenant_id=tenant.tenant_id
        JOIN environments environment
          ON environment.tenant_id=tenant.tenant_id AND environment.code='dev'
        JOIN models model ON model.tenant_id=tenant.tenant_id
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id
         AND provider.provider_id=model.provider_id
        WHERE (? IS NULL OR tenant.tenant_id!=?)
        ORDER BY tenant.slug='default' DESC,tenant.tenant_id,user.user_id,
                 key.virtual_key_id,model.model_id
        LIMIT 1
        """,
        (excluded_tenant_id, excluded_tenant_id),
    ).fetchone()
    assert row is not None
    return {column: str(row[column]) for column in row.keys()}


def _insert_claim(
    connection: sqlite3.Connection,
    identity: dict[str, str],
    suffix: str,
) -> tuple[str, str]:
    """插入一个同租户 Gateway claim 并返回 request/trace。

    参数：连接、同租户身份图和唯一后缀。返回：request_id 与 trace_id。
    关键约束：OutputSafetyScan 的 owner 必须来自已认证 claim，不能由调用方自报租户。
    """

    request_id = f"req_prd071_{suffix}"
    trace_id = f"trc_prd071_{suffix}"
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
    return request_id, trace_id


def _insert_request_log(
    connection: sqlite3.Connection,
    identity: dict[str, str],
    request_id: str,
    trace_id: str,
    *,
    status: str = "failed",
    stream_session_id: str | None = None,
    stream_status: str | None = None,
) -> None:
    """插入 Usage/RequestLog owner，并可携带真实流终态摘要。

    参数：连接、身份、请求、Trace、公开状态和可选流字段。返回：无。
    关键约束：流字段成对出现，数据库触发器再次核对同 tenant/request 会话。
    """

    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,
          provider_code,status,error_code,created_at,updated_at,
          environment_id,environment_code,stream_session_id,stream_status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            request_id,
            identity["tenant_id"],
            trace_id,
            identity["virtual_key_id"],
            identity["provider_id"],
            identity["model_id"],
            identity["provider_code"],
            status,
            "content_policy_violation" if status == "failed" else None,
            STARTED_AT,
            COMPLETED_AT,
            identity["environment_id"],
            identity["environment_code"],
            stream_session_id,
            stream_status,
        ),
    )


def _insert_dlp_rule(
    connection: sqlite3.Connection,
    identity: dict[str, str],
    suffix: str,
    *,
    scan_scope: str = "output",
    action: str = "block",
) -> str:
    """插入一条不含真实敏感文本的固定 DLP 规则并返回 ID。

    参数：连接、身份、唯一后缀、方向和动作。返回：DLP rule ID。
    关键约束：pattern 仅使用测试占位串；租户、创建人和更新人来自同一身份图。
    """

    rule_id = f"dlp_prd071_{suffix}"
    pattern = f"fixture-{suffix}"
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,
          action,status,priority,scan_scope,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'keyword',?,?,?,'active',10,?,?,?,?,?)
        """,
        (
            rule_id,
            identity["tenant_id"],
            f"PRD071 {suffix}",
            pattern,
            hashlib.sha256(pattern.encode("ascii")).hexdigest(),
            action,
            scan_scope,
            identity["user_id"],
            identity["user_id"],
            STARTED_AT,
            STARTED_AT,
        ),
    )
    return rule_id


def _insert_pending_scan(
    connection: sqlite3.Connection,
    identity: dict[str, str],
    request_id: str,
    trace_id: str,
    hex_char: str,
    *,
    stream_session_id: str | None = None,
) -> str:
    """插入 Provider 前 pending scan owner 并返回扫描 ID。

    参数：连接、身份、request/trace、十六进制 ID 字符和可选流会话。返回：scan ID。
    关键约束：pending 不携带规则快照、计数或终态推断；流模式必须先有已提交会话。
    """

    scan_id = f"oss_{hex_char * 32}"
    stream_mode = stream_session_id is not None
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
        ) VALUES(?,?,?,?,?,?, 'pending','none',NULL,NULL,NULL,0,NULL,
                 'output-safety-dlp-v1',?,?,0,0,0,0,0,0,'unknown',
                 'not_required',NULL,?,NULL)
        """,
        (
            scan_id,
            identity["tenant_id"],
            request_id,
            trace_id,
            stream_session_id,
            "stream_window" if stream_mode else "non_stream_full",
            32 if stream_mode else None,
            256 if stream_mode else None,
            STARTED_AT,
        ),
    )
    return scan_id


def _finalize_scan(
    connection: sqlite3.Connection,
    scan_id: str,
    *,
    scan_status: str,
    decision: str,
    action: str | None = None,
    dlp_rule_id: str | None = None,
    rule_type: str | None = None,
    match_count: int = 0,
    fingerprint: str | None = FINGERPRINT,
    checkpoint_count: int = 0,
    observed_tokens: int = 0,
    scanned_tokens: int = 0,
    emitted_tokens: int = 0,
    emitted_events: int = 0,
    emitted_bytes: int = 0,
    exposure_state: str = "unknown",
    billing_disposition: str = "not_required",
    failure_reason_code: str | None = None,
) -> None:
    """把 pending scan 以一条 UPDATE 推进到指定终态。

    参数：扫描 ID 和完整终态矩阵字段。返回：无。
    关键约束：函数不补默认业务推断；测试显式传入 exposure、账单与 failure 语义。
    """

    connection.execute(
        """
        UPDATE output_safety_scans
        SET scan_status=?,decision=?,action=?,dlp_rule_id=?,rule_type=?,
            match_count=?,ruleset_fingerprint=?,checkpoint_count=?,
            observed_output_token_count=?,scanned_token_count=?,
            emitted_token_count=?,emitted_sse_event_count=?,emitted_bytes=?,
            exposure_state=?,billing_disposition=?,failure_reason_code=?,completed_at=?
        WHERE output_safety_scan_id=?
        """,
        (
            scan_status,
            decision,
            action,
            dlp_rule_id,
            rule_type,
            match_count,
            fingerprint,
            checkpoint_count,
            observed_tokens,
            scanned_tokens,
            emitted_tokens,
            emitted_events,
            emitted_bytes,
            exposure_state,
            billing_disposition,
            failure_reason_code,
            COMPLETED_AT,
            scan_id,
        ),
    )


def _insert_stream_session(
    connection: sqlite3.Connection,
    identity: dict[str, str],
    request_id: str,
    trace_id: str,
    hex_char: str,
) -> str:
    """插入 starting StreamSession 并返回稳定 ID。

    参数：连接、同租户身份、request/trace 和十六进制字符。返回：session ID。
    关键约束：会话 owner 与 claim 使用同一 tenant/request/trace，供流式 scan 复合 FK 验证。
    """

    session_id = f"ss_prd071_{hex_char * 24}"
    connection.execute(
        """
        INSERT INTO stream_sessions(
          stream_session_id,tenant_id,request_id,trace_id,virtual_key_id,
          environment_id,environment_code,model_id,provider_id,provider_code,
          status,inter_token_timeout_ms,started_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,'starting',30000,?,?,?)
        """,
        (
            session_id,
            identity["tenant_id"],
            request_id,
            trace_id,
            identity["virtual_key_id"],
            identity["environment_id"],
            identity["environment_code"],
            identity["model_id"],
            identity["provider_id"],
            identity["provider_code"],
            STARTED_AT,
            STARTED_AT,
            STARTED_AT,
        ),
    )
    # 返回值只暴露稳定会话 ID，测试不向日志输出任何请求正文或敏感配置。
    return session_id


def _object_names(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str], ...]:
    """返回表的显式索引与 trigger 名称集合。

    参数：连接与表名。返回：按类型、名称排序的元组。
    关键约束：忽略 SQLite 自动索引，只比较仓库显式治理对象是否完整恢复。
    """

    return tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """
            SELECT type,name FROM sqlite_schema
            WHERE tbl_name=? AND type IN ('index','trigger')
              AND name NOT LIKE 'sqlite_autoindex%'
            ORDER BY type,name
            """,
            (table,),
        ).fetchall()
    )


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """返回表的逻辑字段名顺序。

    参数：连接与表名。返回：PRAGMA table_info 中的字段名。
    关键约束：RequestLog 的 ALTER COLUMN 可改变物理位置，回滚只要求字段集合与业务合同保真。
    """

    return tuple(
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()
    )


def _insert_usage_charge(
    connection: sqlite3.Connection,
    identity: dict[str, str],
    request_id: str,
    suffix: str,
    *,
    amount: str = "1.250000",
) -> tuple[str, str, int]:
    """插入价格证据和 Usage，并返回自动投影的正式 charge。

    参数：连接、身份、请求、唯一后缀和十进制 CNY 金额。返回：Usage ID、Entry ID、微元金额。
    关键约束：沿用正式 Usage trigger 生成 usage_charge，不绕过既有 FinOps owner/金额校验。
    """

    snapshot_id = f"prs_prd071_{suffix}"
    usage_id = f"usg_prd071_{suffix}"
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
            snapshot_id,
            identity["tenant_id"],
            request_id,
            identity["model_id"],
            identity["provider_id"],
            identity["provider_code"],
            STARTED_AT,
            STARTED_AT,
        ),
    )
    normalized = {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "cache_tokens": 0,
        "cost_amount": amount,
        "cost_cny": amount,
        "cost_usd": "0",
        "currency": "CNY",
        "price_schema_version": "price-v1",
        "price_snapshot_id": snapshot_id,
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
            usage_id,
            identity["tenant_id"],
            request_id,
            json.dumps(normalized, ensure_ascii=True, separators=(",", ":")),
            amount,
            amount,
            snapshot_id,
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
    charge = connection.execute(
        """
        SELECT finops_ledger_entry_id,amount_micros
        FROM finops_ledger_entries
        WHERE tenant_id=? AND source_request_id=? AND usage_ledger_id=?
          AND entry_type='usage_charge'
        """,
        (identity["tenant_id"], request_id, usage_id),
    ).fetchone()
    assert charge is not None
    return usage_id, str(charge["finops_ledger_entry_id"]), int(charge["amount_micros"])


def test_prd071_round_trip_preserves_rows_objects_rbac_and_reapplies(
    prd071_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """主流程：0062→0063→0062→0063 保留旧行 rowid、治理对象与 revision 7。"""

    database_url = _clone(
        prd071_templates["database_0062"], tmp_path / "round-trip.db"
    )
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        request_id, trace_id = _insert_claim(connection, identity, "roundtrip")
        session_id = _insert_stream_session(
            connection, identity, request_id, trace_id, "1"
        )
        connection.execute(
            "UPDATE stream_sessions SET status='active',updated_at=? "
            "WHERE stream_session_id=?",
            (STARTED_AT, session_id),
        )
        connection.execute(
            "UPDATE stream_sessions SET status='completed',completed_at=?,updated_at=? "
            "WHERE stream_session_id=?",
            (COMPLETED_AT, COMPLETED_AT, session_id),
        )
        _insert_request_log(
            connection,
            identity,
            request_id,
            trace_id,
            status="success",
            stream_session_id=session_id,
            stream_status="completed",
        )
        pattern = "fixture-roundtrip"
        connection.execute(
            """
            INSERT INTO dlp_rules(
              dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,
              action,status,priority,created_by,updated_by,created_at,updated_at
            ) VALUES('dlp_prd071_roundtrip',?,?,'keyword',?,?,'block','active',10,?,?,?,?)
            """,
            (
                identity["tenant_id"],
                "PRD071 roundtrip",
                pattern,
                hashlib.sha256(pattern.encode("ascii")).hexdigest(),
                identity["user_id"],
                identity["user_id"],
                STARTED_AT,
                STARTED_AT,
            ),
        )
        tracked_rowids = {
            table: tuple(
                connection.execute(
                    f'SELECT rowid FROM "{table}" ORDER BY rowid'
                ).fetchall()
            )
            for table in ("dlp_rules", "stream_sessions", "request_logs")
        }
        old_columns = {
            table: _columns(connection, table)
            for table in ("dlp_rules", "stream_sessions", "request_logs")
        }
        old_objects = {
            table: _object_names(connection, table)
            for table in ("dlp_rules", "stream_sessions", "request_logs")
        }
        old_rbac = tuple(
            connection.execute(
                """
                SELECT role.tenant_id,role.code,role.permission_revision,role.matrix_sha256
                FROM roles role ORDER BY role.tenant_id,role.code
                """
            ).fetchall()
        )
        old_query_keys_trigger_sql = _query_event_keys_trigger_sql(connection)
        # 0062 的 0045 合同必须尚未认识任何 0063 输出安全筛选字段。
        assert "output_safety_status" not in old_query_keys_trigger_sql
        connection.commit()

    _apply_0063(database_url, prd071_templates["migrations_0063"])
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT scan_scope FROM dlp_rules WHERE dlp_rule_id='dlp_prd071_roundtrip'"
        ).fetchone()[0] == "input"
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=7"
        ).fetchone()[0] == 59
        assert tuple(
            connection.execute(
                """
                SELECT role.tenant_id,role.code,role.permission_revision,role.matrix_sha256
                FROM roles role ORDER BY role.tenant_id,role.code
                """
            ).fetchall()
        ) == old_rbac
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        migrated_trigger_sql = _query_event_keys_trigger_sql(connection)
        assert "))<=28" in migrated_trigger_sql.replace(" ", "").replace("\n", "")
        for field in (
            "output_safety_status",
            "output_safety_mode",
            "output_safety_exposure",
            "output_safety_billing",
        ):
            assert field in migrated_trigger_sql

    result = rollback_last_migration(
        database_url, migrations_dir=prd071_templates["migrations_0063"]
    )
    assert result.rolled_back == "0063"
    with connect_database(database_url) as connection:
        for table, expected in tracked_rowids.items():
            assert tuple(
                connection.execute(
                    f'SELECT rowid FROM "{table}" ORDER BY rowid'
                ).fetchall()
            ) == expected
        assert set(_columns(connection, "request_logs")) == set(
            old_columns["request_logs"]
        )
        assert _columns(connection, "stream_sessions") == old_columns["stream_sessions"]
        assert _columns(connection, "dlp_rules") == old_columns["dlp_rules"]
        for table, expected in old_objects.items():
            assert _object_names(connection, table) == expected
        assert tuple(
            connection.execute(
                """
                SELECT role.tenant_id,role.code,role.permission_revision,role.matrix_sha256
                FROM roles role ORDER BY role.tenant_id,role.code
                """
            ).fetchall()
        ) == old_rbac
        # down 必须逐字恢复原触发器，避免 0062 节点接受未来字段名。
        assert _query_event_keys_trigger_sql(connection) == old_query_keys_trigger_sql
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    _apply_0063(database_url, prd071_templates["migrations_0063"])
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()[0] == "0063"
        reapplied_trigger_sql = _query_event_keys_trigger_sql(connection)
        assert "))<=28" in reapplied_trigger_sql.replace(" ", "").replace("\n", "")
        assert all(
            field in reapplied_trigger_sql
            for field in (
                "output_safety_status",
                "output_safety_mode",
                "output_safety_exposure",
                "output_safety_billing",
            )
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_prd071_scan_terminal_matrix_and_immutability(
    prd071_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """状态流程：覆盖全部合法终态，并拒绝二次终结、DELETE、REPLACE 和伪 clean。"""

    database_url = _clone(
        prd071_templates["database_0062"], tmp_path / "scan-matrix.db"
    )
    _apply_0063(database_url, prd071_templates["migrations_0063"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        block_rule = _insert_dlp_rule(connection, identity, "matrix-block")
        allow_rule = _insert_dlp_rule(
            connection, identity, "matrix-allow", action="log_only"
        )
        scans: dict[str, str] = {}
        for suffix, hex_char in zip(
            (
                "not-configured",
                "not-applicable",
                "clean",
                "matched",
                "blocked",
                "unavailable-unknown",
                "unavailable-matched",
            ),
            "1234567",
            strict=True,
        ):
            request_id, trace_id = _insert_claim(connection, identity, suffix)
            scans[suffix] = _insert_pending_scan(
                connection, identity, request_id, trace_id, hex_char
            )

        _finalize_scan(
            connection,
            scans["not-configured"],
            scan_status="not_configured",
            decision="none",
            fingerprint=EMPTY_RULESET_FINGERPRINT,
        )
        _finalize_scan(
            connection,
            scans["not-applicable"],
            scan_status="not_applicable",
            decision="none",
        )
        _finalize_scan(
            connection,
            scans["clean"],
            scan_status="completed",
            decision="clean",
            checkpoint_count=1,
            observed_tokens=5,
            scanned_tokens=5,
            emitted_tokens=5,
            exposure_state="complete",
        )
        _finalize_scan(
            connection,
            scans["matched"],
            scan_status="completed",
            decision="matched",
            action="log_only",
            dlp_rule_id=allow_rule,
            rule_type="keyword",
            match_count=1,
            checkpoint_count=1,
            observed_tokens=5,
            scanned_tokens=5,
            emitted_tokens=5,
            exposure_state="complete",
        )
        _finalize_scan(
            connection,
            scans["blocked"],
            scan_status="completed",
            decision="blocked",
            action="block",
            dlp_rule_id=block_rule,
            rule_type="keyword",
            match_count=1,
            checkpoint_count=1,
            observed_tokens=8,
            scanned_tokens=8,
            emitted_tokens=0,
            exposure_state="none",
            billing_disposition="dispute_pending",
        )
        _finalize_scan(
            connection,
            scans["unavailable-unknown"],
            scan_status="unavailable",
            decision="unknown",
            fingerprint=None,
            exposure_state="unknown",
            failure_reason_code="output_safety.rule_snapshot_unavailable",
        )
        _finalize_scan(
            connection,
            scans["unavailable-matched"],
            scan_status="unavailable",
            decision="matched",
            action="log_only",
            dlp_rule_id=allow_rule,
            rule_type="keyword",
            match_count=1,
            observed_tokens=7,
            scanned_tokens=7,
            emitted_tokens=4,
            exposure_state="partial",
            failure_reason_code="output_safety.stream_incomplete",
        )

        rows = connection.execute(
            """
            SELECT scan_status,decision,COUNT(*) AS row_count
            FROM output_safety_scans GROUP BY scan_status,decision
            ORDER BY scan_status,decision
            """
        ).fetchall()
        assert sum(int(row["row_count"]) for row in rows) == 7

        # 全字段不变的幂等 UPDATE 可通过；任何终态字段漂移都必须失败关闭。
        connection.execute(
            "UPDATE output_safety_scans SET scan_status=scan_status "
            "WHERE output_safety_scan_id=?",
            (scans["clean"],),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE output_safety_scans SET exposure_state='none' "
                "WHERE output_safety_scan_id=?",
                (scans["clean"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="output_safety_scan_immutable"):
            connection.execute(
                "DELETE FROM output_safety_scans WHERE output_safety_scan_id=?",
                (scans["blocked"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT OR REPLACE INTO output_safety_scans "
                "SELECT * FROM output_safety_scans WHERE output_safety_scan_id=?",
                (scans["blocked"],),
            )

        request_id, trace_id = _insert_claim(connection, identity, "pending-mutation")
        pending_id = _insert_pending_scan(
            connection, identity, request_id, trace_id, "8"
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="output_safety_scan_transition_invalid"
        ):
            connection.execute(
                "UPDATE output_safety_scans SET ruleset_fingerprint=? "
                "WHERE output_safety_scan_id=?",
                (FINGERPRINT, pending_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            _finalize_scan(
                connection,
                pending_id,
                scan_status="completed",
                decision="clean",
                fingerprint=None,
                exposure_state="complete",
            )
        connection.rollback()


def test_prd071_owner_event_and_direct_terminal_guards(
    prd071_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """权限流程：拒绝跨 tenant/request/trace/rule，并冻结唯一 blocked source event。"""

    database_url = _clone(
        prd071_templates["database_0062"], tmp_path / "owner-event.db"
    )
    _apply_0063(database_url, prd071_templates["migrations_0063"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        rule_id = _insert_dlp_rule(connection, identity, "event-block")
        foreign_tenant_id = "tenant_prd071_foreign"
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id,slug,name,service_mode,status,created_at,updated_at
            ) VALUES(?,'prd071-foreign','PRD071 Foreign','internal_only','active',?,?)
            """,
            (foreign_tenant_id, STARTED_AT, STARTED_AT),
        )
        foreign_rule_id = "dlp_prd071_foreign_block"
        foreign_pattern = "fixture-foreign-block"
        connection.execute(
            """
            INSERT INTO dlp_rules(
              dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,
              action,status,priority,scan_scope,created_at,updated_at
            ) VALUES(?,?,'PRD071 foreign','keyword',?,?,'block','active',10,
                     'output',?,?)
            """,
            (
                foreign_rule_id,
                foreign_tenant_id,
                foreign_pattern,
                hashlib.sha256(foreign_pattern.encode("ascii")).hexdigest(),
                STARTED_AT,
                STARTED_AT,
            ),
        )

        request_id, trace_id = _insert_claim(connection, identity, "bad-trace")
        with pytest.raises(
            sqlite3.IntegrityError, match="output_safety_scan_owner_mismatch"
        ):
            _insert_pending_scan(
                connection, identity, request_id, "trc_prd071_not_owner", "9"
            )

        # 终态 INSERT 即使字段矩阵合法也必须被 pending 闸门拒绝。
        direct_request, direct_trace = _insert_claim(
            connection, identity, "direct-terminal"
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="output_safety_scan_initial_state_invalid"
        ):
            connection.execute(
                """
                INSERT INTO output_safety_scans(
                  output_safety_scan_id,tenant_id,request_id,trace_id,scan_mode,
                  scan_status,decision,ruleset_fingerprint,exposure_state,
                  billing_disposition,checkpoint_count,started_at,completed_at
                ) VALUES(?,?,?,?,'non_stream_full','completed','clean',?,
                         'complete','not_required',1,?,?)
                """,
                (
                    "oss_" + "a" * 32,
                    identity["tenant_id"],
                    direct_request,
                    direct_trace,
                    FINGERPRINT,
                    STARTED_AT,
                    COMPLETED_AT,
                ),
            )

        owner_request, owner_trace = _insert_claim(connection, identity, "rule-owner")
        owner_scan = _insert_pending_scan(
            connection, identity, owner_request, owner_trace, "b"
        )
        with pytest.raises(sqlite3.IntegrityError):
            _finalize_scan(
                connection,
                owner_scan,
                scan_status="completed",
                decision="blocked",
                action="block",
                dlp_rule_id=foreign_rule_id,
                rule_type="keyword",
                match_count=1,
                observed_tokens=1,
                scanned_tokens=1,
                exposure_state="none",
                billing_disposition="dispute_pending",
            )

        session_request, session_trace = _insert_claim(
            connection, identity, "session-owner"
        )
        session_id = _insert_stream_session(
            connection, identity, session_request, session_trace, "c"
        )
        wrong_request, wrong_trace = _insert_claim(
            connection, identity, "session-wrong-request"
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="output_safety_scan_owner_mismatch"
        ):
            _insert_pending_scan(
                connection,
                identity,
                wrong_request,
                wrong_trace,
                "d",
                stream_session_id=session_id,
            )

        blocked_request, blocked_trace = _insert_claim(
            connection, identity, "event-valid"
        )
        blocked_scan = _insert_pending_scan(
            connection, identity, blocked_request, blocked_trace, "e"
        )
        _finalize_scan(
            connection,
            blocked_scan,
            scan_status="completed",
            decision="blocked",
            action="block",
            dlp_rule_id=rule_id,
            rule_type="keyword",
            match_count=1,
            checkpoint_count=1,
            observed_tokens=9,
            scanned_tokens=9,
            emitted_tokens=0,
            exposure_state="none",
            billing_disposition="dispute_pending",
        )
        event_id = "osevt_" + "e" * 32
        connection.execute(
            """
            INSERT INTO output_safety_events(
              event_id,tenant_id,output_safety_scan_id,request_id,trace_id,
              event_type,schema_version,scan_mode,scan_status,decision,error_code,
              exposure_state,emitted_token_count,checkpoint_count,dlp_rule_id,
              detector_version,billing_disposition,occurred_at
            ) VALUES(?,?,?,?,?,'security.output_blocked',1,'non_stream_full',
                     'completed','blocked','content_policy_violation','none',
                     0,1,?,'output-safety-dlp-v1','dispute_pending',?)
            """,
            (
                event_id,
                identity["tenant_id"],
                blocked_scan,
                blocked_request,
                blocked_trace,
                rule_id,
                COMPLETED_AT,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="output_safety_event_immutable"):
            connection.execute(
                "UPDATE output_safety_events SET occurred_at=occurred_at WHERE event_id=?",
                (event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="output_safety_event_immutable"):
            connection.execute(
                "DELETE FROM output_safety_events WHERE event_id=?", (event_id,)
            )

        clean_request, clean_trace = _insert_claim(connection, identity, "event-clean")
        clean_scan = _insert_pending_scan(
            connection, identity, clean_request, clean_trace, "f"
        )
        _finalize_scan(
            connection,
            clean_scan,
            scan_status="completed",
            decision="clean",
            checkpoint_count=1,
            exposure_state="complete",
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="output_safety_event_owner_mismatch"
        ):
            connection.execute(
                """
                INSERT INTO output_safety_events(
                  event_id,tenant_id,output_safety_scan_id,request_id,trace_id,
                  event_type,schema_version,scan_mode,scan_status,decision,error_code,
                  exposure_state,emitted_token_count,checkpoint_count,dlp_rule_id,
                  detector_version,billing_disposition,occurred_at
                ) VALUES(?,?,?,?,?,'security.output_blocked',1,'non_stream_full',
                         'completed','blocked','content_policy_violation','none',
                         0,0,?,'output-safety-dlp-v1','dispute_pending',?)
                """,
                (
                    "osevt_" + "f" * 32,
                    identity["tenant_id"],
                    clean_scan,
                    clean_request,
                    clean_trace,
                    rule_id,
                    COMPLETED_AT,
                ),
            )
        connection.rollback()


def test_prd071_dispute_binds_blocked_scan_usage_charge_and_amount(
    prd071_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """账单流程：dispute 只能绑定同 owner blocked scan，且资格等于 charge 金额是否大于零。"""

    database_url = _clone(
        prd071_templates["database_0062"], tmp_path / "dispute-owner.db"
    )
    _apply_0063(database_url, prd071_templates["migrations_0063"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        rule_id = _insert_dlp_rule(connection, identity, "dispute-block")
        request_id, trace_id = _insert_claim(connection, identity, "dispute")
        _insert_request_log(connection, identity, request_id, trace_id)
        scan_id = _insert_pending_scan(
            connection, identity, request_id, trace_id, "1"
        )
        _finalize_scan(
            connection,
            scan_id,
            scan_status="completed",
            decision="blocked",
            action="block",
            dlp_rule_id=rule_id,
            rule_type="keyword",
            match_count=1,
            checkpoint_count=1,
            observed_tokens=2,
            scanned_tokens=2,
            exposure_state="none",
            billing_disposition="dispute_pending",
        )
        usage_id, entry_id, amount_micros = _insert_usage_charge(
            connection, identity, request_id, "positive"
        )
        assert amount_micros > 0
        dispute_id = "fld_" + "1" * 32
        with pytest.raises(
            sqlite3.IntegrityError, match="finops_ledger_dispute_owner_mismatch"
        ):
            connection.execute(
                """
                INSERT INTO finops_ledger_disputes(
                  finops_ledger_dispute_id,tenant_id,request_id,trace_id,
                  output_safety_scan_id,usage_ledger_id,finops_ledger_entry_id,
                  review_status,reversal_eligible,reason_code,created_at
                ) VALUES(?,?,?,?,?,?,?,'dispute_pending',0,
                         'output_safety.content_policy_violation',?)
                """,
                (
                    dispute_id,
                    identity["tenant_id"],
                    request_id,
                    trace_id,
                    scan_id,
                    usage_id,
                    entry_id,
                    COMPLETED_AT,
                ),
            )
        connection.execute(
            """
            INSERT INTO finops_ledger_disputes(
              finops_ledger_dispute_id,tenant_id,request_id,trace_id,
              output_safety_scan_id,usage_ledger_id,finops_ledger_entry_id,
              review_status,reversal_eligible,reason_code,created_at
            ) VALUES(?,?,?,?,?,?,?,'dispute_pending',1,
                     'output_safety.content_policy_violation',?)
            """,
            (
                dispute_id,
                identity["tenant_id"],
                request_id,
                trace_id,
                scan_id,
                usage_id,
                entry_id,
                COMPLETED_AT,
            ),
        )
        assert connection.execute(
            "SELECT reversal_eligible FROM finops_ledger_disputes "
            "WHERE finops_ledger_dispute_id=?",
            (dispute_id,),
        ).fetchone()[0] == 1
        with pytest.raises(
            sqlite3.IntegrityError, match="finops_ledger_dispute_immutable"
        ):
            connection.execute(
                "UPDATE finops_ledger_disputes SET review_status=review_status "
                "WHERE finops_ledger_dispute_id=?",
                (dispute_id,),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="finops_ledger_dispute_immutable"
        ):
            connection.execute(
                "DELETE FROM finops_ledger_disputes WHERE finops_ledger_dispute_id=?",
                (dispute_id,),
            )
        connection.rollback()


def test_prd071_stream_and_request_log_new_terminal_states(
    prd071_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """流状态流程：只允许 active 进入两个新终态，并要求固定错误码与 failed RequestLog。"""

    database_url = _clone(
        prd071_templates["database_0062"], tmp_path / "stream-states.db"
    )
    _apply_0063(database_url, prd071_templates["migrations_0063"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        request_id, trace_id = _insert_claim(connection, identity, "stream-block")
        session_id = _insert_stream_session(
            connection, identity, request_id, trace_id, "2"
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="stream_session_status_transition_invalid"
        ):
            connection.execute(
                """
                UPDATE stream_sessions
                SET status='output_blocked',error_code='content_policy_violation',
                    completed_at=?,updated_at=?
                WHERE stream_session_id=?
                """,
                (COMPLETED_AT, COMPLETED_AT, session_id),
            )
        connection.execute(
            "UPDATE stream_sessions SET status='active',updated_at=? "
            "WHERE stream_session_id=?",
            (STARTED_AT, session_id),
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="stream_session_output_safety_state_invalid"
        ):
            connection.execute(
                """
                UPDATE stream_sessions
                SET status='output_blocked',error_code='content_policy_violation',
                    completed_at=NULL,updated_at=? WHERE stream_session_id=?
                """,
                (COMPLETED_AT, session_id),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="stream_session_output_safety_state_invalid"
        ):
            connection.execute(
                """
                UPDATE stream_sessions
                SET status='output_blocked',error_code=NULL,
                    completed_at=?,updated_at=? WHERE stream_session_id=?
                """,
                (COMPLETED_AT, COMPLETED_AT, session_id),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="stream_session_output_safety_state_invalid"
        ):
            connection.execute(
                """
                UPDATE stream_sessions
                SET status='output_blocked',error_code='gateway.output_safety_unavailable',
                    completed_at=?,updated_at=?
                WHERE stream_session_id=?
                """,
                (COMPLETED_AT, COMPLETED_AT, session_id),
            )
        connection.execute(
            """
            UPDATE stream_sessions
            SET status='output_blocked',error_code='content_policy_violation',
                retryable=0,completed_at=?,updated_at=?
            WHERE stream_session_id=?
            """,
            (COMPLETED_AT, COMPLETED_AT, session_id),
        )
        _insert_request_log(
            connection,
            identity,
            request_id,
            trace_id,
            stream_session_id=session_id,
            stream_status="output_blocked",
        )
        assert tuple(
            connection.execute(
                "SELECT status,stream_status FROM request_logs WHERE tenant_id=? AND request_id=?",
                (identity["tenant_id"], request_id),
            ).fetchone()
        ) == ("failed", "output_blocked")

        unavailable_request, unavailable_trace = _insert_claim(
            connection, identity, "stream-unavailable"
        )
        unavailable_session = _insert_stream_session(
            connection,
            identity,
            unavailable_request,
            unavailable_trace,
            "3",
        )
        connection.execute(
            "UPDATE stream_sessions SET status='active' WHERE stream_session_id=?",
            (unavailable_session,),
        )
        connection.execute(
            """
            UPDATE stream_sessions
            SET status='output_safety_unavailable',
                error_code='gateway.output_safety_unavailable',retryable=0,
                completed_at=?,updated_at=? WHERE stream_session_id=?
            """,
            (COMPLETED_AT, COMPLETED_AT, unavailable_session),
        )
        _insert_request_log(
            connection,
            identity,
            unavailable_request,
            unavailable_trace,
            stream_session_id=unavailable_session,
            stream_status="output_safety_unavailable",
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="request_log_stream_status_mismatch"
        ):
            _insert_request_log(
                connection,
                identity,
                unavailable_request,
                unavailable_trace,
                status="success",
                stream_session_id=unavailable_session,
                stream_status="output_safety_unavailable",
            )
        connection.rollback()


@pytest.mark.parametrize(
    "guard_kind",
    ("scan", "scope", "webhook", "unknown_view"),
)
def test_prd071_down_guards_are_atomic(
    prd071_templates: dict[str, Path],
    tmp_path: Path,
    guard_kind: str,
) -> None:
    """回滚异常：业务事实、方向、Webhook 和未知依赖均在首项 DDL 前原子拒绝。"""

    database_url = _clone(
        prd071_templates["database_0062"], tmp_path / f"down-{guard_kind}.db"
    )
    _apply_0063(database_url, prd071_templates["migrations_0063"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        if guard_kind == "scan":
            request_id, trace_id = _insert_claim(connection, identity, "down-scan")
            _insert_pending_scan(connection, identity, request_id, trace_id, "4")
        elif guard_kind == "scope":
            _insert_dlp_rule(connection, identity, "down-scope", scan_scope="both")
        elif guard_kind == "webhook":
            connection.execute(
                """
                INSERT INTO webhook_endpoints(
                  webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
                  encrypted_secret,secret_prefix,secret_fingerprint,status,
                  timeout_ms,max_attempts,created_by,updated_by,created_at,updated_at
                ) VALUES('whe_prd071_down',?,'PRD071 down guard',
                         'https://example.invalid/prd071',?,
                         'enc:v1:fixture','whsec_test','fpr_prd071','active',
                         5000,3,?,?,?,?)
                """,
                (
                    identity["tenant_id"],
                    json.dumps(["security.output_blocked"]),
                    identity["user_id"],
                    identity["user_id"],
                    STARTED_AT,
                    STARTED_AT,
                ),
            )
        else:
            connection.execute(
                "CREATE VIEW future_output_safety_projection AS "
                "SELECT tenant_id,output_safety_scan_id FROM output_safety_scans"
            )
        connection.commit()

    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, migrations_dir=prd071_templates["migrations_0063"]
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0063'"
        ).fetchone()[0] == "applied"
        assert connection.execute(
            "SELECT COUNT(*) FROM pragma_table_info('output_safety_scans')"
        ).fetchone()[0] == 27
        if guard_kind == "unknown_view":
            assert connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema "
                "WHERE type='view' AND name='future_output_safety_projection'"
            ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
