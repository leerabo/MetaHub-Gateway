"""PRD-053 成本异常检测数据库迁移、owner 图和不可变状态机专项测试。

测试仅写入 pytest 独占 SQLite 文件，数据只包含安全 ID、整数微元和摘要；不保存
Provider Key、Token、Prompt、Response 或真实 Webhook 地址。跨租户用例验证数据库
第二道隔离防线，迁移失败用例同时验证持久 DDL 的原子性。
"""

from __future__ import annotations

import json
import sqlite3
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


# PRD-053 专项冻结在 0043；后续正式迁移必须先真实执行，再严格按栈序回退。
FROZEN_VERSION = "0043"
"""PRD-053 历史断言必须恢复到的迁移栈顶。"""


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库路径转换为项目 SQLite URL。

    参数：``tmp_path`` 下的文件路径。返回：迁移入口可解析的 URL。
    关键约束：不得回落到共享开发库，避免专项测试污染实际验收证据。
    """

    return f"sqlite:///{path.as_posix()}"


def apply_migrations(database_url: str) -> migration_module.MigrationResult:
    """执行当前迁移后逐版回退，使 PRD-053 专项稳定停在 0043 栈顶。

    参数：pytest 独占数据库 URL。返回：只包含截至 0043 的迁移结果。
    关键约束：必须真实执行并回退后续迁移，不能复制迁移目录或跳过 down guard。
    """

    result = apply_current_migrations(database_url)
    # 从真实迁移结果计算后续版本，防止新增迁移后静态列表落后于生产目录。
    later_migrations = tuple(
        sorted(
            {
                version
                for version in (*result.applied, *result.skipped)
                if version > FROZEN_VERSION
            },
            reverse=True,
        )
    )
    for version in later_migrations:
        # 严格核对每次真实栈顶，避免历史专项跳过新增迁移而产生假阳性。
        rolled_back = rollback_last_migration(database_url)
        assert rolled_back.rolled_back == version
    excluded = set(later_migrations)
    return migration_module.MigrationResult(
        run_id=result.run_id,
        applied=[version for version in result.applied if version not in excluded],
        skipped=[version for version in result.skipped if version not in excluded],
        rolled_back=result.rolled_back,
    )


def _metadata(connection: sqlite3.Connection, tenant_id: str) -> dict[str, str]:
    """读取同租户 admin、Provider、Model、dev Environment 和 Virtual Key 安全 ID。

    参数：数据库连接与可信 tenant_id。返回：构造账本和处置关系所需的 ID 字典。
    租户约束：每条 JOIN 都显式携带 tenant_id，测试夹具自身不能掩盖横向引用缺陷。
    """

    row = connection.execute(
        """
        SELECT actor.user_id, actor.email, actor.role,
               provider.provider_id, provider.provider_code,
               model.model_id, key.virtual_key_id,
               environment.environment_id, environment.code AS environment_code
        FROM users actor
        JOIN providers provider ON provider.tenant_id=actor.tenant_id
        JOIN models model
          ON model.tenant_id=provider.tenant_id
         AND model.provider_id=provider.provider_id
        JOIN virtual_keys key ON key.tenant_id=actor.tenant_id
        JOIN environments environment
          ON environment.tenant_id=actor.tenant_id AND environment.code='dev'
        WHERE actor.tenant_id=? AND actor.role='admin'
        ORDER BY actor.user_id, provider.provider_id, model.model_id, key.virtual_key_id
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None
    return {column: str(row[column]) for column in row.keys()}


def _insert_usage_charge(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    suffix: str,
    *,
    created_at: str = "2026-07-27T10:00:10Z",
    amount: str = "1.000000",
) -> dict[str, object]:
    """通过 Request -> PriceSnapshot -> Usage 正式链生成一条 FinOps Usage Charge。

    参数：连接、owner 元数据、唯一后缀、业务时间和 canonical CNY 金额。
    返回：PRD-053 run/anomaly 所需的正式账本行。
    关键约束：不直写投影表，确保 0041 owner、金额和来源触发器在测试中真实生效。
    """

    request_id = f"req_prd053_{suffix}"
    trace_id = f"trc_prd053_{suffix}"
    snapshot_id = f"prs_prd053_{suffix}"
    usage_id = f"usg_prd053_{suffix}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
        """,
        (tenant_id, request_id, trace_id, metadata["virtual_key_id"], created_at),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          status, created_at, updated_at, environment_id, environment_code
        ) VALUES(?, ?, ?, ?, ?, ?, 'success', ?, ?, ?, ?)
        """,
        (
            request_id,
            tenant_id,
            trace_id,
            metadata["virtual_key_id"],
            metadata["provider_id"],
            metadata["model_id"],
            created_at,
            created_at,
            metadata["environment_id"],
            metadata["environment_code"],
        ),
    )
    connection.execute(
        """
        INSERT INTO price_snapshots(
          price_snapshot_id, tenant_id, request_id, model_id, provider_id,
          provider_code, currency, input_price_per_1k, output_price_per_1k,
          price_schema_version, billing_dimensions, provider_raw_mapping,
          source, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, 'CNY', '0.001000', '0.002000',
                 'price-v1', '{}', '{}', 'model_catalog', 'active', ?, ?)
        """,
        (
            snapshot_id,
            tenant_id,
            request_id,
            metadata["model_id"],
            metadata["provider_id"],
            metadata["provider_code"],
            created_at,
            created_at,
        ),
    )
    normalized = {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "cache_tokens": 1,
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
          usage_ledger_id, tenant_id, request_id,
          input_tokens, output_tokens, total_tokens, cache_tokens,
          raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd,
          currency, price_schema_version, estimated, usage_source,
          price_snapshot_id, model_id, provider_id, virtual_key_id,
          environment_id, environment_code, provider_code, created_at, updated_at
        ) VALUES(?, ?, ?, 3, 2, 5, 1, '{}', ?, ?, ?, '0', 'CNY',
                 'price-v1', 0, 'upstream', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            usage_id,
            tenant_id,
            request_id,
            json.dumps(normalized, separators=(",", ":")),
            amount,
            amount,
            snapshot_id,
            metadata["model_id"],
            metadata["provider_id"],
            metadata["virtual_key_id"],
            metadata["environment_id"],
            metadata["environment_code"],
            metadata["provider_code"],
            created_at,
            created_at,
        ),
    )
    row = connection.execute(
        """
        SELECT * FROM finops_ledger_entries
        WHERE tenant_id=? AND usage_ledger_id=?
        """,
        (tenant_id, usage_id),
    ).fetchone()
    assert row is not None
    return dict(row)


def _insert_run(
    connection: sqlite3.Connection,
    tenant_id: str,
    environment_id: str,
    ledger_watermark: int,
    hex_char: str,
) -> str:
    """插入一条 processing 检测运行并返回 ID。

    参数：owner、正式账本水位和十六进制 ID 字符。返回：固定形状 run ID。
    关键约束：窗口严格对齐五分钟，lease、claim 与 revision 同时形成可恢复状态。
    """

    run_id = f"car_{hex_char * 32}"
    connection.execute(
        """
        INSERT INTO cost_anomaly_detection_runs(
          detection_run_id, tenant_id, environment_id,
          window_start, window_end, baseline_start, baseline_end,
          ledger_watermark, evaluation_revision, status,
          claim_token, lease_owner_id, lease_expires_at,
          operation_request_id, operation_trace_id,
          started_at, created_at, updated_at
        ) VALUES(
          :run_id, :tenant_id, :environment_id,
          '2026-07-27T10:00:00Z', '2026-07-27T10:05:00Z',
          '2026-07-26T10:00:00Z', '2026-07-27T10:00:00Z',
          :watermark, 1, 'processing', :claim_token, :lease_owner,
          '2026-07-27T10:10:00Z', :request_id, :trace_id,
          '2026-07-27T10:00:20Z', '2026-07-27T10:00:20Z',
          '2026-07-27T10:00:20Z'
        )
        """,
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "environment_id": environment_id,
            "watermark": ledger_watermark,
            "claim_token": f"claim-{hex_char * 16}",
            "lease_owner": f"worker-prd053-{hex_char}",
            "request_id": f"req_prd053_run_{hex_char}",
            "trace_id": f"trc_prd053_run_{hex_char}",
        },
    )
    return run_id


def _insert_anomaly(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    run_id: str,
    ledger: dict[str, object],
    hex_char: str,
    *,
    virtual_key_id: str | None = None,
) -> str:
    """插入一条 Key spend spike 不可变证据并返回 anomaly ID。

    参数：同租户 owner 图、processing run、正式账本边界和可选 Key 覆盖。
    返回：固定形状 anomaly ID。
    关键约束：代表请求、Trace、Project/Application 快照均直接来自正式账本事实。
    """

    anomaly_id = f"can_{hex_char * 32}"
    key_id = virtual_key_id or metadata["virtual_key_id"]
    connection.execute(
        """
        INSERT INTO cost_anomalies(
          cost_anomaly_id, event_id, tenant_id, environment_id, detection_run_id,
          rule_type, severity, status, scope_type, scope_id, virtual_key_id,
          top_virtual_key_id, project_id_snapshot, application_id_snapshot,
          window_start, window_end, baseline_start, baseline_end,
          current_cost_micros, baseline_total_micros, observed_ratio_millis,
          threshold_multiplier_millis, minimum_cost_micros,
          source_entry_count, estimated_entry_count, legacy_entry_count,
          affected_virtual_key_count, representative_request_id,
          representative_trace_id, source_ledger_sequence_min,
          source_ledger_sequence_max, recommended_action, detected_at,
          mitigation_eligible_until, event_dispatch_status,
          event_dispatch_attempts, event_next_attempt_at,
          created_at, updated_at
        ) VALUES(
          :anomaly_id, :event_id, :tenant_id, :environment_id, :run_id,
          'virtual_key_spend_spike', 'critical', 'open', 'virtual_key',
          :key_id, :key_id, NULL, :project_id, :application_id,
          '2026-07-27T10:00:00Z', '2026-07-27T10:05:00Z',
          '2026-07-26T10:00:00Z', '2026-07-27T10:00:00Z',
          '1000000', '100000', '10000', 3000, 100000,
          1, 0, 0, 1, :request_id, :trace_id, :ledger_sequence,
          :ledger_sequence, 'apply_temporary_rate_limit',
          '2026-07-27T10:05:00Z', '2026-07-28T10:05:00Z',
          'pending', 0, '2026-07-27T10:05:00Z',
          '2026-07-27T10:05:00Z', '2026-07-27T10:05:00Z'
        )
        """,
        {
            "anomaly_id": anomaly_id,
            "event_id": f"caevt_{hex_char * 32}",
            "tenant_id": tenant_id,
            "environment_id": metadata["environment_id"],
            "run_id": run_id,
            "key_id": key_id,
            "project_id": ledger["project_id"],
            "application_id": ledger["application_id"],
            "request_id": ledger["source_request_id"],
            "trace_id": ledger["source_trace_id"],
            "ledger_sequence": ledger["ledger_sequence"],
        },
    )
    return anomaly_id


def _insert_action(
    connection: sqlite3.Connection,
    tenant_id: str,
    anomaly_id: str,
    actor_user_id: str,
    hex_char: str,
    action_type: str,
    *,
    target_override_id: str | None = None,
) -> dict[str, object]:
    """写入 acknowledge/apply/release append-only 动作并返回动作参数。

    参数：owner、异常、actor、唯一字符、动作类型和可选 release target。
    返回：后续 anomaly/override/Audit 同事务步骤所需的动作字段。
    关键约束：apply 至少收紧 RPM；release 只放宽或清空有效限制，不伪造再次收紧。
    """

    action_id = f"caa_{hex_char * 32}"
    before = "open"
    after = "acknowledged"
    created_at = "2026-07-27T10:06:00Z"
    duration: int | None = None
    rpm: int | None = None
    effective_rpm_before: int | None = None
    effective_rpm_after: int | None = None
    if action_type == "apply_temporary_rate_limit":
        after = "mitigated"
        duration = 30
        rpm = 10
        effective_rpm_before = 100
        effective_rpm_after = 10
    elif action_type == "release_temporary_rate_limit":
        before = "mitigated"
        after = "mitigated"
        created_at = "2026-07-27T10:10:00Z"
        effective_rpm_before = 10
        effective_rpm_after = 100
    connection.execute(
        """
        INSERT INTO cost_anomaly_actions(
          cost_anomaly_action_id, tenant_id, cost_anomaly_id, actor_user_id,
          action_type, status_before, status_after, dismiss_reason,
          duration_minutes, rpm_limit, tpm_limit, concurrency_limit,
          target_override_id, effective_rpm_before, effective_tpm_before,
          effective_concurrency_before, effective_rpm_after,
          effective_tpm_after, effective_concurrency_after,
          operation_request_id, operation_trace_id,
          idempotency_key_sha256, intent_sha256, created_at
        ) VALUES(
          :action_id, :tenant_id, :anomaly_id, :actor_id, :action_type,
          :before_status, :after_status, NULL, :duration, :rpm, NULL, NULL,
          :target_override_id, :effective_rpm_before, NULL, NULL,
          :effective_rpm_after, NULL, NULL, :request_id, :trace_id,
          :idempotency_hash, :intent_hash, :created_at
        )
        """,
        {
            "action_id": action_id,
            "tenant_id": tenant_id,
            "anomaly_id": anomaly_id,
            "actor_id": actor_user_id,
            "action_type": action_type,
            "before_status": before,
            "after_status": after,
            "duration": duration,
            "rpm": rpm,
            "target_override_id": target_override_id,
            "effective_rpm_before": effective_rpm_before,
            "effective_rpm_after": effective_rpm_after,
            "request_id": f"req_prd053_action_{hex_char}",
            "trace_id": f"trc_prd053_action_{hex_char}",
            "idempotency_hash": hex_char * 64,
            "intent_hash": ("f" if hex_char != "f" else "e") * 64,
            "created_at": created_at,
        },
    )
    return {
        "action_id": action_id,
        "action_type": action_type,
        "request_id": f"req_prd053_action_{hex_char}",
        "trace_id": f"trc_prd053_action_{hex_char}",
        "created_at": created_at,
        "duration": duration,
        "rpm": rpm,
    }


def _insert_override(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    anomaly_id: str,
    action: dict[str, object],
    hex_char: str,
    *,
    virtual_key_id: str | None = None,
) -> str:
    """用 apply action 建立同 tenant、同 Key 的临时 RPM 覆盖并返回 ID。

    参数：owner 图、异常、apply 动作和可选 Key。返回：覆盖 ID。
    关键约束：starts/created/动作时间及 operation request/trace 必须逐值一致。
    """

    override_id = f"trl_{hex_char * 32}"
    connection.execute(
        """
        INSERT INTO temporary_rate_limit_overrides(
          temporary_rate_limit_override_id, tenant_id, virtual_key_id,
          source_type, source_id, source_action_id, rpm_limit, status,
          starts_at, expires_at, created_by,
          operation_request_id, operation_trace_id, created_at, updated_at
        ) VALUES(
          ?, ?, ?, 'cost_anomaly', ?, ?, ?, 'active', ?,
          '2026-07-27T10:36:00Z', ?, ?, ?, ?, ?
        )
        """,
        (
            override_id,
            tenant_id,
            virtual_key_id or metadata["virtual_key_id"],
            anomaly_id,
            action["action_id"],
            action["rpm"],
            action["created_at"],
            metadata["user_id"],
            action["request_id"],
            action["trace_id"],
            action["created_at"],
            action["created_at"],
        ),
    )
    return override_id


def _create_second_tenant(
    connection: sqlite3.Connection,
    suffix: str,
) -> tuple[str, dict[str, str]]:
    """创建第二租户及其 admin、dev Environment 与 Key owner 图。

    参数：连接和唯一后缀。返回：tenant_id 与安全 ID 字典。
    用途：证明真实存在的其他租户 ID 也不能挂接到当前租户异常事实。
    """

    tenant_id = f"tenant_prd053_{suffix}"
    user_id = f"usr_prd053_{suffix}"
    key_id = f"vk_prd053_{suffix}"
    now = "2026-07-27T09:00:00Z"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        ) VALUES(?, ?, 'PRD053 隔离租户', 'internal_only', 'active', ?, ?)
        """,
        (tenant_id, f"prd053-{suffix}", now, now),
    )
    environment_id = str(
        connection.execute(
            "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
            (tenant_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash,
          role, status, created_at, updated_at
        ) VALUES(?, ?, ?, 'PRD053 Admin', 'test-hash-only',
                 'admin', 'active', ?, ?)
        """,
        (user_id, tenant_id, f"admin-{suffix}@example.invalid", now, now),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
          environment, environment_id, created_at, updated_at
        ) VALUES(?, ?, 'PRD053 Key', ?, ?, 'active', 'dev', ?, ?, ?)
        """,
        (
            key_id,
            tenant_id,
            f"mhv_prd053_{suffix}",
            f"hash-prd053-{suffix}",
            environment_id,
            now,
            now,
        ),
    )
    # 返回第二租户安全 ID，调用方只用于构造并验证横向引用攻击。
    return tenant_id, {
        "user_id": user_id,
        "virtual_key_id": key_id,
        "environment_id": environment_id,
    }


def _prepare_graph(
    tmp_path: Path,
    name: str,
) -> tuple[str, sqlite3.Connection, object, dict[str, str], dict[str, object], str, str]:
    """创建带正式 Usage、processing run 和 open Key anomaly 的独占数据库。

    参数：pytest 临时目录和数据库名。返回：URL、连接、seed、元数据、账本、run、anomaly。
    事务边界：调用者独占并负责关闭连接；测试失败也由 pytest 临时目录隔离清理。
    """

    database_url = _sqlite_url(tmp_path / f"{name}.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    ledger = _insert_usage_charge(connection, seed.tenant_id, metadata, "graph_a")
    run_id = _insert_run(
        connection,
        seed.tenant_id,
        metadata["environment_id"],
        int(ledger["ledger_sequence"]),
        "a",
    )
    anomaly_id = _insert_anomaly(
        connection,
        seed.tenant_id,
        metadata,
        run_id,
        ledger,
        "a",
    )
    return database_url, connection, seed, metadata, ledger, run_id, anomaly_id


def _insert_audit(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    anomaly_id: str,
    action: dict[str, object],
    suffix: str,
) -> None:
    """为处置动作写一条只含白名单摘要的 Audit。

    参数：owner、异常、动作和唯一后缀。返回：无。
    关键约束：request/trace/actor 必须与 action 一致，触发器据此阻止跨租户挂审计。
    """

    action_name = {
        "apply_temporary_rate_limit": "cost_anomaly.temporary_rate_limit_applied",
        "release_temporary_rate_limit": "cost_anomaly.temporary_rate_limit_released",
        "acknowledge": "cost_anomaly.acknowledged",
    }[str(action["action_type"])]
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, object_name,
          before_summary_json, after_summary_json, changed_fields_json,
          client_ip, user_agent, request_id, trace_id, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, 'cost_anomaly', ?, NULL,
                 '{}', '{}', '["status"]', NULL, NULL, ?, ?, ?)
        """,
        (
            f"aud_prd053_{suffix}",
            tenant_id,
            metadata["user_id"],
            metadata["email"],
            metadata["role"],
            action_name,
            anomaly_id,
            action["request_id"],
            action["trace_id"],
            action["created_at"],
        ),
    )


# 验证 Audit、正式账本和预算结构经过 0043 双向迁移后仍逐值保持。
def test_prd053_upgrade_and_down_preserve_audit_finops_budget_and_rowid(
    tmp_path: Path,
) -> None:
    """验证 0042 升级和回滚全过程，确保 Audit rowid/NULL、FinOps 正式分录及预算结构逐值不变。"""

    database_url = _sqlite_url(tmp_path / "prd053-roundtrip.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0043"
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    ledger = _insert_usage_charge(connection, seed.tenant_id, metadata, "roundtrip")
    connection.execute(
        """
        INSERT INTO audit_logs(
          rowid, audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, object_name,
          before_summary_json, after_summary_json, changed_fields_json,
          client_ip, user_agent, request_id, trace_id, created_at
        ) VALUES(53053, 'aud_prd053_legacy', ?, ?, ?, ?,
                 'provider.updated', 'provider', ?, NULL,
                 '{}', '{"status":"active"}', '["status"]',
                 NULL, NULL, 'req_prd053_legacy', 'trc_prd053_legacy',
                 '2026-07-27T09:30:00Z')
        """,
        (
            seed.tenant_id,
            metadata["user_id"],
            metadata["email"],
            metadata["role"],
            metadata["provider_id"],
        ),
    )
    connection.commit()
    audit_before = tuple(
        connection.execute(
            "SELECT rowid, * FROM audit_logs WHERE audit_log_id='aud_prd053_legacy'"
        ).fetchone()
    )
    ledger_before = tuple(
        connection.execute(
            "SELECT * FROM finops_ledger_entries WHERE ledger_sequence=?",
            (ledger["ledger_sequence"],),
        ).fetchone()
    )
    budget_sql_before = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='budget_scope_events'"
    ).fetchone()[0]
    connection.close()

    assert apply_migrations(database_url).applied == ["0043"]
    connection = connect_database(database_url)
    assert tuple(
        connection.execute(
            "SELECT rowid, * FROM audit_logs WHERE audit_log_id='aud_prd053_legacy'"
        ).fetchone()
    ) == audit_before
    assert tuple(
        connection.execute(
            "SELECT * FROM finops_ledger_entries WHERE ledger_sequence=?",
            (ledger["ledger_sequence"],),
        ).fetchone()
    ) == ledger_before
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='budget_scope_events'"
    ).fetchone()[0] == budget_sql_before
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='table' AND name IN (
          'cost_anomaly_detection_runs', 'cost_anomalies',
          'cost_anomaly_actions', 'temporary_rate_limit_overrides'
        )
        """
    ).fetchone()[0] == 4
    plan = connection.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT ledger_sequence FROM finops_ledger_entries
        WHERE tenant_id=? AND environment_id=?
          AND entry_type='usage_charge' AND direction='debit' AND status='posted'
          AND created_at>=? AND created_at<?
        """,
        (
            seed.tenant_id,
            metadata["environment_id"],
            "2026-07-27T10:00:00Z",
            "2026-07-27T10:05:00Z",
        ),
    ).fetchall()
    assert "idx_finops_budget_tenant_usage_prd052" in " ".join(str(row[3]) for row in plan)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()

    assert rollback_last_migration(database_url).rolled_back == "0043"
    connection = connect_database(database_url)
    assert tuple(
        connection.execute(
            "SELECT rowid, * FROM audit_logs WHERE audit_log_id='aud_prd053_legacy'"
        ).fetchone()
    ) == audit_before
    audit_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_logs'"
    ).fetchone()[0]
    assert "finops.adjustment_created" in audit_sql
    assert "cost_anomaly." not in audit_sql
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd053_unknown_audit_dependency_aborts_up_before_persistent_ddl(
    tmp_path: Path,
) -> None:
    """升级异常：未知 Audit 索引使 0043 原子失败，旧表和索引均保持。"""

    database_url = _sqlite_url(tmp_path / "prd053-unknown-audit.db")
    apply_migrations(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0043"
    connection = connect_database(database_url)
    connection.execute("CREATE INDEX idx_prd053_unknown_audit ON audit_logs(object_name)")
    connection.commit()
    connection.close()

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd053_prerequisite_missing" in exc.value.message
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='idx_prd053_unknown_audit'"
    ).fetchone()[0] == 1
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master WHERE name IN (
          'cost_anomaly_detection_runs', 'cost_anomalies',
          'cost_anomaly_actions', 'temporary_rate_limit_overrides'
        )
        """
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0043'"
    ).fetchone()[0] == "failed"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd053_owner_graph_rejects_cross_tenant_environment_key_actor_and_source(
    tmp_path: Path,
) -> None:
    """权限流程：run/anomaly/action/override 的跨租户 owner 攻击全部拒绝。"""

    _, connection, seed, metadata, ledger, run_id, anomaly_id = _prepare_graph(
        tmp_path,
        "prd053-owner",
    )
    _, other = _create_second_tenant(connection, "owner")
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_run_owner_mismatch"):
        _insert_run(
            connection,
            seed.tenant_id,
            other["environment_id"],
            int(ledger["ledger_sequence"]),
            "b",
        )
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_owner_mismatch"):
        _insert_anomaly(
            connection,
            seed.tenant_id,
            metadata,
            run_id,
            ledger,
            "b",
            virtual_key_id=other["virtual_key_id"],
        )
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_action_owner_mismatch"):
        _insert_action(
            connection,
            seed.tenant_id,
            anomaly_id,
            other["user_id"],
            "b",
            "acknowledge",
        )

    apply_action = _insert_action(
        connection,
        seed.tenant_id,
        anomaly_id,
        metadata["user_id"],
        "c",
        "apply_temporary_rate_limit",
    )
    connection.execute(
        """
        UPDATE cost_anomalies SET status='mitigated', updated_at='2026-07-27T10:06:00Z'
        WHERE tenant_id=? AND cost_anomaly_id=?
        """,
        (seed.tenant_id, anomaly_id),
    )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="temporary_rate_limit_override_owner_mismatch",
    ):
        _insert_override(
            connection,
            seed.tenant_id,
            metadata,
            anomaly_id,
            apply_action,
            "c",
            virtual_key_id=other["virtual_key_id"],
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd053_run_insert_requires_relevant_formal_max(tmp_path: Path) -> None:
    """水位所有权：首次 run 只能冻结相关检测区间正式账本的实际 MAX。"""

    database_url = _sqlite_url(tmp_path / "prd053-run-watermark-owner.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    ledger = _insert_usage_charge(
        connection,
        seed.tenant_id,
        metadata,
        "run_watermark_owner",
    )
    actual_watermark = int(ledger["ledger_sequence"])
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_run_owner_mismatch"):
        _insert_run(
            connection,
            seed.tenant_id,
            metadata["environment_id"],
            actual_watermark + 1_000_000,
            "e",
        )
    assert connection.execute(
        "SELECT COUNT(*) FROM cost_anomaly_detection_runs"
    ).fetchone()[0] == 0

    run_id = _insert_run(
        connection,
        seed.tenant_id,
        metadata["environment_id"],
        actual_watermark,
        "e",
    )
    row = connection.execute(
        "SELECT ledger_watermark, evaluation_revision, status "
        "FROM cost_anomaly_detection_runs WHERE detection_run_id=?",
        (run_id,),
    ).fetchone()
    assert tuple(row) == (actual_watermark, 1, "processing")
    connection.close()


def test_prd053_run_insert_rejects_larger_watermark_from_other_environment(
    tmp_path: Path,
) -> None:
    """验证同租户其他 Environment 的真实高序号不能作为 dev run 水位。

    参数：``tmp_path`` 为 pytest 提供的独占临时目录。返回值：无。
    隔离约束：水位必须属于 run 的 tenant_id 与 environment_id，不能只验证序号存在。
    """

    database_url = _sqlite_url(tmp_path / "prd053-run-watermark-environment.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    dev_ledger = _insert_usage_charge(
        connection,
        seed.tenant_id,
        metadata,
        "run_env_dev",
    )
    dev_watermark = int(dev_ledger["ledger_sequence"])

    test_environment_id = "env_prd053_watermark_test"
    test_virtual_key_id = "vk_prd053_watermark_test"
    now = "2026-07-27T09:00:00Z"
    connection.execute(
        """
        INSERT INTO environments(
          environment_id, tenant_id, code, name, status, created_at, updated_at
        ) VALUES(?, ?, 'test', 'PRD053 水位隔离测试环境', 'active', ?, ?)
        """,
        (test_environment_id, seed.tenant_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
          environment, environment_id, created_at, updated_at
        ) VALUES(?, ?, 'PRD053 水位隔离测试 Key', 'mhv_prd053_test',
                 'hash-prd053-watermark-test', 'active', 'test', ?, ?, ?)
        """,
        (
            test_virtual_key_id,
            seed.tenant_id,
            test_environment_id,
            now,
            now,
        ),
    )
    # 同租户复用 Provider/Model，只替换 Environment 与 Key，确保唯一变量是环境归属。
    test_metadata = {
        **metadata,
        "virtual_key_id": test_virtual_key_id,
        "environment_id": test_environment_id,
        "environment_code": "test",
    }
    test_ledger = _insert_usage_charge(
        connection,
        seed.tenant_id,
        test_metadata,
        "run_env_test",
    )
    test_watermark = int(test_ledger["ledger_sequence"])
    assert dev_ledger["environment_id"] == metadata["environment_id"]
    assert test_ledger["environment_id"] == test_environment_id
    assert test_watermark > dev_watermark

    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_run_owner_mismatch"):
        _insert_run(
            connection,
            seed.tenant_id,
            metadata["environment_id"],
            test_watermark,
            "f",
        )
    assert connection.execute(
        "SELECT COUNT(*) FROM cost_anomaly_detection_runs"
    ).fetchone()[0] == 0
    connection.close()


def test_prd053_run_cas_requires_new_watermark_revision_and_expired_takeover(
    tmp_path: Path,
) -> None:
    """运行状态机：重领水位须来自相关正式账本，且过期接管符合 CAS。"""

    _, connection, seed, metadata, ledger, run_id, _ = _prepare_graph(
        tmp_path,
        "prd053-run-cas",
    )
    connection.execute(
        """
        UPDATE cost_anomaly_detection_runs
        SET status='completed', environment_current_complete=1,
            environment_baseline_complete=1, source_entry_count=1,
            anomaly_count=1, claim_token=NULL, lease_owner_id=NULL,
            lease_expires_at=NULL, completed_at='2026-07-27T10:05:30Z',
            updated_at='2026-07-27T10:05:30Z'
        WHERE detection_run_id=?
        """,
        (run_id,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_run_cas_invalid"):
        connection.execute(
            """
            UPDATE cost_anomaly_detection_runs
            SET status='processing', evaluation_revision=2,
                environment_current_complete=NULL, environment_baseline_complete=NULL,
                source_entry_count=0, anomaly_count=0, completed_at=NULL,
                claim_token='claim-bbbbbbbbbbbbbbbb', lease_owner_id='worker-b',
                lease_expires_at='2026-07-27T10:20:00Z',
                operation_request_id='req_prd053_run_a',
                operation_trace_id='trc_prd053_run_a',
                started_at='2026-07-27T10:06:00Z', updated_at='2026-07-27T10:06:00Z'
            WHERE detection_run_id=?
            """,
            (run_id,),
        )

    # 即使序号真实存在，只要业务时间不在该 run 的 baseline/window 区间内，也不能
    # 冒充相关水位；不存在的高序号同样必须在数据库第二防线失败关闭。
    unrelated = _insert_usage_charge(
        connection,
        seed.tenant_id,
        metadata,
        "graph_unrelated_window",
        created_at="2026-07-25T10:00:10Z",
        amount="3.000000",
    )
    invalid_watermarks = (
        int(unrelated["ledger_sequence"]),
        int(unrelated["ledger_sequence"]) + 1_000_000,
    )
    for invalid_watermark in invalid_watermarks:
        with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_run_cas_invalid"):
            connection.execute(
                """
                UPDATE cost_anomaly_detection_runs
                SET status='processing', evaluation_revision=2, ledger_watermark=?,
                    environment_current_complete=NULL, environment_baseline_complete=NULL,
                    source_entry_count=0, anomaly_count=0, completed_at=NULL,
                    claim_token='claim-bbbbbbbbbbbbbbbb', lease_owner_id='worker-b',
                    lease_expires_at='2026-07-27T10:20:00Z',
                    operation_request_id='req_prd053_run_a',
                    operation_trace_id='trc_prd053_run_a',
                    started_at='2026-07-27T10:06:00Z', updated_at='2026-07-27T10:06:00Z'
                WHERE detection_run_id=?
                """,
                (invalid_watermark, run_id),
            )

    second = _insert_usage_charge(
        connection,
        seed.tenant_id,
        metadata,
        "graph_b",
        created_at="2026-07-27T10:01:10Z",
        amount="2.000000",
    )
    connection.execute(
        """
        UPDATE cost_anomaly_detection_runs
        SET status='processing', evaluation_revision=2, ledger_watermark=?,
            environment_current_complete=NULL, environment_baseline_complete=NULL,
            source_entry_count=0, anomaly_count=0, completed_at=NULL,
            claim_token='claim-bbbbbbbbbbbbbbbb', lease_owner_id='worker-b',
            lease_expires_at='2026-07-27T10:20:00Z',
            operation_request_id='req_prd053_run_a',
            operation_trace_id='trc_prd053_run_a',
            started_at='2026-07-27T10:06:00Z', updated_at='2026-07-27T10:06:00Z'
        WHERE detection_run_id=?
        """,
        (second["ledger_sequence"], run_id),
    )
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_run_cas_invalid"):
        connection.execute(
            """
            UPDATE cost_anomaly_detection_runs
            SET status='completed', evaluation_revision=1,
                environment_current_complete=1, environment_baseline_complete=1,
                source_entry_count=2, anomaly_count=1,
                claim_token=NULL, lease_owner_id=NULL, lease_expires_at=NULL,
                completed_at='2026-07-27T10:07:00Z',
                updated_at='2026-07-27T10:07:00Z'
            WHERE detection_run_id=?
            """,
            (run_id,),
        )
    # 过期接管的其他字段即使完全合法，也不能改写首次 run 的固定调用链。
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_run_cas_invalid"):
        connection.execute(
            """
            UPDATE cost_anomaly_detection_runs
            SET evaluation_revision=3,
                claim_token='claim-cccccccccccccccc', lease_owner_id='worker-c',
                lease_expires_at='2026-07-27T10:30:00Z',
                operation_request_id='req_prd053_run_c',
                operation_trace_id='trc_prd053_run_c',
                started_at='2026-07-27T10:20:00Z', updated_at='2026-07-27T10:20:00Z'
            WHERE detection_run_id=?
            """,
            (run_id,),
        )
    connection.execute(
        """
        UPDATE cost_anomaly_detection_runs
        SET evaluation_revision=3,
            claim_token='claim-cccccccccccccccc', lease_owner_id='worker-c',
            lease_expires_at='2026-07-27T10:30:00Z',
            operation_request_id='req_prd053_run_a',
            operation_trace_id='trc_prd053_run_a',
            started_at='2026-07-27T10:20:00Z', updated_at='2026-07-27T10:20:00Z'
        WHERE detection_run_id=?
        """,
        (run_id,),
    )
    row = connection.execute(
        """
        SELECT status, evaluation_revision, ledger_watermark, lease_owner_id,
               operation_request_id, operation_trace_id
        FROM cost_anomaly_detection_runs WHERE detection_run_id=?
        """,
        (run_id,),
    ).fetchone()
    assert tuple(row) == (
        "processing",
        3,
        second["ledger_sequence"],
        "worker-c",
        "req_prd053_run_a",
        "trc_prd053_run_a",
    )
    connection.close()


def test_prd053_apply_release_audit_and_immutability_contract(
    tmp_path: Path,
) -> None:
    """主流程：apply/release 合法顺序成功，Audit 绑定动作，四类事实不可篡改。"""

    _, connection, seed, metadata, _, run_id, anomaly_id = _prepare_graph(
        tmp_path,
        "prd053-actions",
    )
    apply_action = _insert_action(
        connection,
        seed.tenant_id,
        anomaly_id,
        metadata["user_id"],
        "b",
        "apply_temporary_rate_limit",
    )
    connection.execute(
        "UPDATE cost_anomalies SET status='mitigated', updated_at=? "
        "WHERE tenant_id=? AND cost_anomaly_id=?",
        (apply_action["created_at"], seed.tenant_id, anomaly_id),
    )
    override_id = _insert_override(
        connection,
        seed.tenant_id,
        metadata,
        anomaly_id,
        apply_action,
        "b",
    )
    _insert_audit(
        connection,
        seed.tenant_id,
        metadata,
        anomaly_id,
        apply_action,
        "apply",
    )

    release_action = _insert_action(
        connection,
        seed.tenant_id,
        anomaly_id,
        metadata["user_id"],
        "c",
        "release_temporary_rate_limit",
        target_override_id=override_id,
    )
    connection.execute(
        """
        UPDATE temporary_rate_limit_overrides
        SET status='released', release_action_id=?, released_by=?,
            released_at=?, updated_at=?
        WHERE tenant_id=? AND temporary_rate_limit_override_id=?
        """,
        (
            release_action["action_id"],
            metadata["user_id"],
            release_action["created_at"],
            release_action["created_at"],
            seed.tenant_id,
            override_id,
        ),
    )
    _insert_audit(
        connection,
        seed.tenant_id,
        metadata,
        anomaly_id,
        release_action,
        "release",
    )
    assert connection.execute(
        "SELECT status FROM temporary_rate_limit_overrides WHERE temporary_rate_limit_override_id=?",
        (override_id,),
    ).fetchone()[0] == "released"
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE tenant_id=? AND object_id=?",
        (seed.tenant_id, anomaly_id),
    ).fetchone()[0] == 2

    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_state_invalid"):
        connection.execute(
            "UPDATE cost_anomalies SET current_cost_micros='9999999', "
            "updated_at='2026-07-27T10:11:00Z' WHERE cost_anomaly_id=?",
            (anomaly_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_immutable"):
        connection.execute("DELETE FROM cost_anomalies WHERE cost_anomaly_id=?", (anomaly_id,))
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_action_immutable"):
        connection.execute(
            "UPDATE cost_anomaly_actions SET operation_trace_id='trc_rewritten' "
            "WHERE cost_anomaly_action_id=?",
            (apply_action["action_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="temporary_rate_limit_override_release_invalid"):
        connection.execute(
            "UPDATE temporary_rate_limit_overrides SET expires_at='2026-07-27T11:36:00Z' "
            "WHERE temporary_rate_limit_override_id=?",
            (override_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="temporary_rate_limit_override_immutable"):
        connection.execute(
            "DELETE FROM temporary_rate_limit_overrides "
            "WHERE temporary_rate_limit_override_id=?",
            (override_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_run_immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO cost_anomaly_detection_runs "
            "SELECT * FROM cost_anomaly_detection_runs WHERE detection_run_id=?",
            (run_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO cost_anomalies "
            "SELECT * FROM cost_anomalies WHERE cost_anomaly_id=?",
            (anomaly_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cost_anomaly_action_immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO cost_anomaly_actions "
            "SELECT * FROM cost_anomaly_actions WHERE cost_anomaly_action_id=?",
            (apply_action["action_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="temporary_rate_limit_override_immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO temporary_rate_limit_overrides "
            "SELECT * FROM temporary_rate_limit_overrides "
            "WHERE temporary_rate_limit_override_id=?",
            (override_id,),
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def _insert_webhook_fact(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    fact_kind: str,
) -> None:
    """插入成本异常订阅、delivery 或 attempt 外部事实。

    参数：同租户 owner 图和事实类型。返回：无。
    安全：Endpoint 使用 ``example.invalid``，Secret 字段只放测试占位密文与指纹。
    """

    endpoint_id = f"wh_prd053_{fact_kind}"
    event_types = '["cost.anomaly_detected"]' if fact_kind == "subscription" else "[]"
    now = "2026-07-27T11:00:00Z"
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status,
          created_by, updated_by, created_at, updated_at
        ) VALUES(?, ?, 'PRD053', 'https://example.invalid/webhook', ?,
                 'encrypted-test-only', 'whsec_test', ?, 'active', ?, ?, ?, ?)
        """,
        (
            endpoint_id,
            tenant_id,
            event_types,
            f"fingerprint-{fact_kind}",
            metadata["user_id"],
            metadata["user_id"],
            now,
            now,
        ),
    )
    if fact_kind == "subscription":
        return
    delivery_id = f"whd_prd053_{fact_kind}"
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id,
          event_type, event_timestamp, payload_json, status,
          attempt_count, max_attempts, created_at, updated_at
        ) VALUES(?, ?, ?, 'caevt_external_prd053', 'cost.anomaly_detected',
                 ?, '{}', 'pending', 0, 3, ?, ?)
        """,
        (delivery_id, tenant_id, endpoint_id, now, now, now),
    )
    if fact_kind == "attempt":
        connection.execute(
            """
            INSERT INTO webhook_delivery_attempts(
              webhook_delivery_attempt_id, tenant_id, webhook_delivery_id,
              webhook_endpoint_id, attempt_no, status, error_code,
              error_summary, created_at
            ) VALUES('wha_prd053_attempt', ?, ?, ?, 1, 'failed',
                     'webhook.delivery_unavailable', 'safe-test-summary', ?)
            """,
            (tenant_id, delivery_id, endpoint_id, now),
        )


@pytest.mark.parametrize("fact_kind", ["run", "subscription", "delivery", "attempt"])
def test_prd053_down_guard_rejects_business_and_webhook_facts_atomically(
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """异常流程：内部运行或三类外部 Webhook 合同均使 down 在 DDL 前失败。"""

    database_url = _sqlite_url(tmp_path / f"prd053-down-{fact_kind}.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    if fact_kind == "run":
        ledger = _insert_usage_charge(connection, seed.tenant_id, metadata, "down_run")
        _insert_run(
            connection,
            seed.tenant_id,
            metadata["environment_id"],
            int(ledger["ledger_sequence"]),
            "d",
        )
    else:
        _insert_webhook_fact(connection, seed.tenant_id, metadata, fact_kind)
    connection.commit()
    object_count_before = connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE name IN (
          'cost_anomaly_detection_runs', 'cost_anomalies',
          'cost_anomaly_actions', 'temporary_rate_limit_overrides'
        ) OR name LIKE '%_prd053'
        """
    ).fetchone()[0]

    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd053_down_business_facts_present" in exc.value.message
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE name IN (
          'cost_anomaly_detection_runs', 'cost_anomalies',
          'cost_anomaly_actions', 'temporary_rate_limit_overrides'
        ) OR name LIKE '%_prd053'
        """
    ).fetchone()[0] == object_count_before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0043'"
    ).fetchone()[0] == "applied"
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
