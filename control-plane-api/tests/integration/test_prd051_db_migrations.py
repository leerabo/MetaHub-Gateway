"""PRD-051 FinOps Ledger 数据库迁移与触发器专项测试。

本模块只构造安全 ID、整数微元和脱敏财务摘要；不写入 Prompt、Response、
Provider Key、Token、真实 Endpoint 或幂等键原文。所有 owner 用例都显式验证 tenant_id。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError


# 迁移源目录只用于构造截至 0042 的只读历史快照，禁止修改仓库中的正式迁移文件。
MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[3] / "infra" / "migrations"


@pytest.fixture(scope="module", autouse=True)
def prd051_migration_snapshot(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """冻结只含 0001..0042 的迁移目录，维持 FinOps 往返测试的历史版本边界。

    参数：pytest 模块级临时目录工厂。返回：无业务值，仅在 fixture 生命周期内切换迁移目录。
    关键约束：本专项要验证 0041 及其与 0042 的兼容性；0043 及以后版本不得改变既有
    applied 列表、down guard 目标或异常原子性断言。
    """

    snapshot_directory = tmp_path_factory.mktemp("prd051-migrations")
    copied_versions: set[str] = set()
    for source_file in MIGRATIONS_DIRECTORY.glob("*.sql"):
        version = source_file.name[:4]
        if not version.isdigit() or int(version) > 42:
            continue
        shutil.copy2(source_file, snapshot_directory / source_file.name)
        if not source_file.name.endswith(".down.sql"):
            copied_versions.add(version)
    assert copied_versions == {f"{version:04d}" for version in range(1, 43)}

    previous_directory = os.environ.get("METAHUB_MIGRATIONS_DIR")
    # 环境变量是迁移 runner 的默认目录契约；作用域必须严格限制在当前测试模块，
    # finally 恢复可防止后续 PRD 专项误用 0042 截止的历史快照。
    os.environ["METAHUB_MIGRATIONS_DIR"] = str(snapshot_directory)
    try:
        yield
    finally:
        if previous_directory is None:
            os.environ.pop("METAHUB_MIGRATIONS_DIR", None)
        else:
            os.environ["METAHUB_MIGRATIONS_DIR"] = previous_directory


def _sqlite_url(path: Path) -> str:
    """把隔离测试数据库转换为项目约定的 SQLite URL。

    参数：pytest 专用临时数据库路径。返回：迁移入口可解析的 URL。
    关键约束：不得回落到共享开发库，避免测试污染真实验收数据。
    """

    return f"sqlite:///{path.as_posix()}"


def _metadata(connection: sqlite3.Connection, tenant_id: str) -> dict[str, str]:
    """读取同租户管理员、模型、Provider、Key 与环境安全 ID。

    参数：数据库连接和可信 tenant_id。返回：只含稳定 ID/code 的夹具字典。
    关键约束：所有 JOIN 都携带 tenant_id，测试夹具自身也不能制造横向引用。
    """

    row = connection.execute(
        """
        SELECT
          user.user_id, user.email, user.role,
          model.model_id, model.model_alias,
          provider.provider_id, provider.provider_code,
          key.virtual_key_id,
          environment.environment_id, environment.code AS environment_code
        FROM users user
        JOIN models model ON model.tenant_id=user.tenant_id
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
        JOIN virtual_keys key ON key.tenant_id=model.tenant_id
        JOIN environments environment
          ON environment.tenant_id=model.tenant_id AND environment.code='dev'
        WHERE user.tenant_id=? AND user.role='admin'
        ORDER BY user.user_id, model.model_id, key.virtual_key_id
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None
    return {column: str(row[column]) for column in row.keys()}


def _insert_request(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    suffix: str,
    *,
    created_at: str = "2026-07-26T10:00:00Z",
) -> tuple[str, str]:
    """插入同租户 RequestClaim 与成功 RequestLog，并返回 request/trace ID。

    参数：连接、tenant、同租户元数据、唯一后缀和业务时间。返回：请求与 Trace ID。
    关键约束：先写 Claim 再写 Request，保持 PRD-045 的请求所有权门禁成立。
    """

    request_id = f"req_prd051_{suffix}"
    trace_id = f"trc_prd051_{suffix}"
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
    return request_id, trace_id


def _insert_snapshot(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    request_id: str,
    suffix: str,
    *,
    currency: str = "CNY",
    version: str | None = "price-v1",
    created_at: str = "2026-07-26T10:00:01Z",
) -> str:
    """插入一条同租户价格证据并返回 snapshot ID。

    参数：连接、tenant、元数据、请求、后缀、币种、版本和时间。返回：快照 ID。
    关键约束：价格证据只含测试单价与安全映射，不放置 Provider 原始密钥或 URL。
    """

    snapshot_id = f"prs_prd051_{suffix}"
    connection.execute(
        """
        INSERT INTO price_snapshots(
          price_snapshot_id, tenant_id, request_id, model_id, provider_id,
          provider_code, currency, input_price_per_1k, output_price_per_1k,
          price_schema_version, billing_dimensions, provider_raw_mapping,
          source, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, '0.001000', '0.002000', ?, '{}', '{}',
                 'model_catalog', 'active', ?, ?)
        """,
        (
            snapshot_id,
            tenant_id,
            request_id,
            metadata["model_id"],
            metadata["provider_id"],
            metadata["provider_code"],
            currency,
            version,
            created_at,
            created_at,
        ),
    )
    return snapshot_id


def _insert_usage(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    request_id: str,
    suffix: str,
    *,
    amount: str,
    currency: str = "CNY",
    snapshot_id: str | None = None,
    version: str | None = None,
    normalized: dict[str, object] | None = None,
    model_id: str | None = None,
    created_at: str = "2026-07-26T10:00:02Z",
) -> str:
    """插入一条 Usage，供历史回填或运行期自动投影测试。

    参数：连接、owner 元数据、金额/币种/快照及可选 normalized 载荷。返回：Usage ID。
    关键约束：canonical 金额始终是普通十进制字符串，USD 的 CNY 归一值明确为 null。
    """

    usage_id = f"usg_prd051_{suffix}"
    payload = normalized
    if payload is None:
        payload = {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "cache_tokens": 1,
            "cost_amount": amount,
            "cost_cny": amount if currency == "CNY" else None,
            "cost_usd": amount if currency == "USD" else "0",
            "currency": currency,
            "price_schema_version": version,
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
        ) VALUES(?, ?, ?, 3, 2, 5, 1, '{}', ?, ?, ?, ?, ?, ?, 0, 'upstream',
                 ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            usage_id,
            tenant_id,
            request_id,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            amount,
            amount if currency == "CNY" else "0",
            amount if currency == "USD" else "0",
            currency,
            version,
            snapshot_id,
            model_id or metadata["model_id"],
            metadata["provider_id"],
            metadata["virtual_key_id"],
            metadata["environment_id"],
            metadata["environment_code"],
            metadata["provider_code"],
            created_at,
            created_at,
        ),
    )
    return usage_id


def _insert_adjustment(
    connection: sqlite3.Connection,
    tenant_id: str,
    actor_user_id: str,
    suffix: str,
    *,
    adjustment_type: str,
    amount_micros: int,
    currency: str = "CNY",
    reversal_of_entry_id: str | None = None,
    related_request_id: str | None = None,
) -> str:
    """插入不可变 AdjustmentRecord 并返回公开 ID。

    参数：owner、类型、整数微元和可选冲正/请求关系。返回：Adjustment ID。
    关键约束：测试只保存 SHA-256 形状的固定摘要，不保存 reason 或幂等键之外的敏感原文。
    """

    hex_char = suffix[0]
    adjustment_id = f"fadj_{hex_char * 32}"
    connection.execute(
        """
        INSERT INTO finops_adjustments(
          finops_adjustment_id, tenant_id, adjustment_type, amount_micros,
          currency, reversal_of_entry_id, related_request_id, reason,
          reason_sha256, actor_user_id, operation_request_id, operation_trace_id,
          idempotency_key_sha256, payload_sha256, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, '财务调整测试', ?, ?, ?, ?, ?, ?, 'posted', ?, ?)
        """,
        (
            adjustment_id,
            tenant_id,
            adjustment_type,
            amount_micros,
            currency,
            reversal_of_entry_id,
            related_request_id,
            hex_char * 64,
            actor_user_id,
            f"req_prd051_adjust_{suffix}",
            f"trc_prd051_adjust_{suffix}",
            hex_char * 64,
            ("f" if hex_char != "f" else "e") * 64,
            "2026-07-26T11:00:00Z",
            "2026-07-26T11:00:00Z",
        ),
    )
    return adjustment_id


def _insert_manual_entry(
    connection: sqlite3.Connection,
    tenant_id: str,
    actor_user_id: str,
    adjustment_id: str,
    suffix: str,
    *,
    entry_type: str = "manual_credit",
    direction: str = "credit",
    amount_micros: int = 250000,
) -> str:
    """为非关联 Adjustment 写入 not_applicable 正式分录并返回 Entry ID。"""

    hex_char = suffix[0]
    entry_id = f"fle_{hex_char * 32}"
    operation = connection.execute(
        """
        SELECT operation_request_id, operation_trace_id
        FROM finops_adjustments
        WHERE tenant_id=? AND finops_adjustment_id=?
        """,
        (tenant_id, adjustment_id),
    ).fetchone()
    assert operation is not None
    connection.execute(
        """
        INSERT INTO finops_ledger_entries(
          finops_ledger_entry_id, event_id, tenant_id, entry_type, direction,
          amount_micros, currency, normalized_cny_micros,
          cny_normalization_status, price_evidence_status, price_snapshot_source,
          posting_source, quota_type, finops_adjustment_id,
          operation_request_id, operation_trace_id, actor_user_id,
          status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, 'CNY', ?, 'normalized', 'not_applicable',
                 NULL, 'direct', 'paid', ?, ?, ?, ?, 'posted', ?, ?)
        """,
        (
            entry_id,
            f"flevt_{hex_char * 32}",
            tenant_id,
            entry_type,
            direction,
            amount_micros,
            amount_micros,
            adjustment_id,
            operation["operation_request_id"],
            operation["operation_trace_id"],
            actor_user_id,
            "2026-07-26T11:00:00Z",
            "2026-07-26T11:00:00Z",
        ),
    )
    return entry_id


def test_prd051_historical_backfill_preserves_audit_and_roundtrips(tmp_path: Path) -> None:
    """0040→0041：priced/legacy/CNY/USD 精确回填，空业务事实 down 保真恢复。"""

    database_url = _sqlite_url(tmp_path / "prd051-history.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0042"
    assert rollback_last_migration(database_url).rolled_back == "0041"
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)

    request_priced, _ = _insert_request(connection, seed.tenant_id, metadata, "history_priced")
    snapshot_priced = _insert_snapshot(
        connection, seed.tenant_id, metadata, request_priced, "history_priced"
    )
    _insert_usage(
        connection,
        seed.tenant_id,
        metadata,
        request_priced,
        "history_priced",
        amount="12.345678",
        snapshot_id=snapshot_priced,
        version="price-v1",
    )

    request_missing, _ = _insert_request(connection, seed.tenant_id, metadata, "history_missing")
    _insert_usage(
        connection,
        seed.tenant_id,
        metadata,
        request_missing,
        "history_missing",
        amount="0.250000",
        normalized={},
    )

    request_unversioned, _ = _insert_request(
        connection, seed.tenant_id, metadata, "history_unversioned"
    )
    snapshot_unversioned = _insert_snapshot(
        connection,
        seed.tenant_id,
        metadata,
        request_unversioned,
        "history_unversioned",
        version=None,
    )
    _insert_usage(
        connection,
        seed.tenant_id,
        metadata,
        request_unversioned,
        "history_unversioned",
        amount="0.500000",
        snapshot_id=snapshot_unversioned,
        version=None,
    )

    request_usd, _ = _insert_request(connection, seed.tenant_id, metadata, "history_usd")
    snapshot_usd = _insert_snapshot(
        connection,
        seed.tenant_id,
        metadata,
        request_usd,
        "history_usd",
        currency="USD",
        version="usd-v1",
    )
    _insert_usage(
        connection,
        seed.tenant_id,
        metadata,
        request_usd,
        "history_usd",
        amount="0.123456",
        currency="USD",
        snapshot_id=snapshot_usd,
        version="usd-v1",
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          rowid, audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, object_name, before_summary_json,
          after_summary_json, changed_fields_json, client_ip, user_agent,
          request_id, trace_id, created_at
        ) VALUES(410, 'aud_prd051_history', ?, ?, ?, 'admin', 'policy.created',
                 'policy', 'pol_prd051_history', NULL, '{}', '{"status":"active"}',
                 '["status"]', NULL, NULL, 'req_prd051_audit', 'trc_prd051_audit',
                 '2026-07-26T10:10:00Z')
        """,
        (seed.tenant_id, metadata["user_id"], metadata["email"]),
    )
    connection.commit()
    audit_before = tuple(
        connection.execute(
            "SELECT rowid, * FROM audit_logs WHERE audit_log_id='aud_prd051_history'"
        ).fetchone()
    )
    connection.close()

    assert apply_migrations(database_url).applied == ["0041", "0042"]
    connection = connect_database(database_url)
    rows = connection.execute(
        """
        SELECT usage_ledger_id, amount_micros, currency, normalized_cny_micros,
               price_evidence_status, price_snapshot_source, price_schema_version
        FROM finops_ledger_entries
        WHERE entry_type='usage_charge'
        ORDER BY usage_ledger_id
        """
    ).fetchall()
    assert len(rows) == 4
    by_usage = {row["usage_ledger_id"]: row for row in rows}
    assert by_usage["usg_prd051_history_priced"]["amount_micros"] == 12_345_678
    assert by_usage["usg_prd051_history_priced"]["price_evidence_status"] == "priced"
    assert by_usage["usg_prd051_history_missing"]["amount_micros"] == 250_000
    assert by_usage["usg_prd051_history_missing"]["price_evidence_status"] == "legacy_amount_only"
    assert by_usage["usg_prd051_history_unversioned"]["price_snapshot_source"] == (
        "finops_legacy_backfill"
    )
    assert by_usage["usg_prd051_history_usd"]["normalized_cny_micros"] is None
    assert tuple(
        connection.execute(
            "SELECT rowid, * FROM audit_logs WHERE audit_log_id='aud_prd051_history'"
        ).fetchone()
    ) == audit_before
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()

    assert rollback_last_migration(database_url).rolled_back == "0042"
    assert rollback_last_migration(database_url).rolled_back == "0041"
    connection = connect_database(database_url)
    assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 4
    assert connection.execute(
        "SELECT COUNT(*) FROM price_snapshots WHERE source='finops_legacy_backfill'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'finops_%'"
    ).fetchone()[0] == 0
    assert tuple(
        connection.execute(
            "SELECT rowid, * FROM audit_logs WHERE audit_log_id='aud_prd051_history'"
        ).fetchone()
    ) == audit_before
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd051_runtime_projection_is_exact_and_tenant_owned(tmp_path: Path) -> None:
    """运行期：priced/unpriced Usage 同事务投影，跨租户 Model 引用在数据库层拒绝。"""

    database_url = _sqlite_url(tmp_path / "prd051-runtime.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)

    request_priced, _ = _insert_request(connection, seed.tenant_id, metadata, "runtime_priced")
    snapshot_id = _insert_snapshot(
        connection, seed.tenant_id, metadata, request_priced, "runtime_priced"
    )
    usage_priced = _insert_usage(
        connection,
        seed.tenant_id,
        metadata,
        request_priced,
        "runtime_priced",
        amount="1.234567",
        snapshot_id=snapshot_id,
        version="price-v1",
    )
    request_zero, _ = _insert_request(connection, seed.tenant_id, metadata, "runtime_zero")
    usage_zero = _insert_usage(
        connection,
        seed.tenant_id,
        metadata,
        request_zero,
        "runtime_zero",
        amount="0.000000",
    )
    connection.commit()

    priced = connection.execute(
        "SELECT * FROM finops_ledger_entries WHERE tenant_id=? AND usage_ledger_id=?",
        (seed.tenant_id, usage_priced),
    ).fetchone()
    assert priced["amount_micros"] == 1_234_567
    assert priced["price_evidence_status"] == "priced"
    assert priced["price_snapshot_source"] == "model_catalog"
    zero = connection.execute(
        "SELECT * FROM finops_ledger_entries WHERE tenant_id=? AND usage_ledger_id=?",
        (seed.tenant_id, usage_zero),
    ).fetchone()
    assert zero["amount_micros"] == 0
    assert zero["price_evidence_status"] == "unpriced"
    assert zero["price_schema_version"] == "unpriced-v1"
    assert connection.execute(
        """
        SELECT COUNT(*) FROM price_snapshots
        WHERE tenant_id=? AND source='finops_unpriced'
          AND json_extract(billing_dimensions, '$.finops_usage_ledger_id')=?
        """,
        (seed.tenant_id, usage_zero),
    ).fetchone()[0] == 1

    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_prd051_other', 'prd051-other', 'Other', 'internal_only', 'active',
               '2026-07-26T10:20:00Z', '2026-07-26T10:20:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status, created_at, updated_at
        ) VALUES('prv_prd051_other', 'tenant_prd051_other', 'other_safe', 'Other',
                 'https://example.invalid', 'active',
                 '2026-07-26T10:20:00Z', '2026-07-26T10:20:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        ) VALUES('mdl_prd051_other', 'tenant_prd051_other', 'prv_prd051_other',
                 'other-model', 'other-model', '{}', 'active',
                 '2026-07-26T10:20:00Z', '2026-07-26T10:20:00Z')
        """
    )
    request_bad, _ = _insert_request(connection, seed.tenant_id, metadata, "runtime_bad_owner")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="usage_finops_owner_mismatch"):
        _insert_usage(
            connection,
            seed.tenant_id,
            metadata,
            request_bad,
            "runtime_bad_owner",
            amount="0.000000",
            model_id="mdl_prd051_other",
        )
    connection.rollback()
    assert connection.execute(
        "SELECT COUNT(*) FROM usage_ledger WHERE usage_ledger_id='usg_prd051_runtime_bad_owner'"
    ).fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd051_append_only_replace_and_usage_reversal_contract(tmp_path: Path) -> None:
    """不可变性：阻断 UPDATE/DELETE/REPLACE，并允许 Usage 部分冲正且拒绝超额。"""

    database_url = _sqlite_url(tmp_path / "prd051-immutable.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    request_id, _ = _insert_request(connection, seed.tenant_id, metadata, "reversal_usage")
    snapshot_id = _insert_snapshot(
        connection, seed.tenant_id, metadata, request_id, "reversal_usage"
    )
    usage_id = _insert_usage(
        connection,
        seed.tenant_id,
        metadata,
        request_id,
        "reversal_usage",
        amount="1.000000",
        snapshot_id=snapshot_id,
        version="price-v1",
    )
    target = connection.execute(
        "SELECT * FROM finops_ledger_entries WHERE usage_ledger_id=?", (usage_id,)
    ).fetchone()

    manual_adjustment = _insert_adjustment(
        connection,
        seed.tenant_id,
        metadata["user_id"],
        "a_manual",
        adjustment_type="manual_credit",
        amount_micros=250_000,
    )
    manual_entry = _insert_manual_entry(
        connection,
        seed.tenant_id,
        metadata["user_id"],
        manual_adjustment,
        "b_manual",
    )
    reversal_adjustment = _insert_adjustment(
        connection,
        seed.tenant_id,
        metadata["user_id"],
        "c_reversal",
        adjustment_type="reversal",
        amount_micros=400_000,
        reversal_of_entry_id=target["finops_ledger_entry_id"],
    )
    connection.execute(
        """
        INSERT INTO finops_ledger_entries(
          finops_ledger_entry_id, event_id, tenant_id, entry_type, direction,
          amount_micros, currency, normalized_cny_micros,
          cny_normalization_status, price_evidence_status, price_snapshot_source,
          posting_source, quota_type, finops_adjustment_id,
          source_request_id, source_trace_id, operation_request_id, operation_trace_id,
          price_snapshot_id, price_schema_version, reversal_of_entry_id, actor_user_id,
          model_id, provider_id, virtual_key_id, environment_id, project_id,
          application_id, status, created_at, updated_at
        ) VALUES(?, ?, ?, 'reversal', 'credit', 400000, 'CNY', 400000,
                 'normalized', ?, ?, 'direct', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                 ?, ?, ?, ?, ?, ?, 'posted', ?, ?)
        """,
        (
            f"fle_{'c' * 32}",
            f"flevt_{'c' * 32}",
            seed.tenant_id,
            target["price_evidence_status"],
            target["price_snapshot_source"],
            target["quota_type"],
            reversal_adjustment,
            target["source_request_id"],
            target["source_trace_id"],
            "req_prd051_adjust_c_reversal",
            "trc_prd051_adjust_c_reversal",
            target["price_snapshot_id"],
            target["price_schema_version"],
            target["finops_ledger_entry_id"],
            metadata["user_id"],
            target["model_id"],
            target["provider_id"],
            target["virtual_key_id"],
            target["environment_id"],
            target["project_id"],
            target["application_id"],
            "2026-07-26T11:00:00Z",
            "2026-07-26T11:00:00Z",
        ),
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="finops_adjustment_owner_mismatch"):
        _insert_adjustment(
            connection,
            seed.tenant_id,
            metadata["user_id"],
            "d_overflow",
            adjustment_type="reversal",
            amount_micros=600_001,
            reversal_of_entry_id=target["finops_ledger_entry_id"],
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="finops_adjustment_append_only"):
        connection.execute(
            "UPDATE finops_adjustments SET reason='不可修改' WHERE finops_adjustment_id=?",
            (manual_adjustment,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="finops_ledger_append_only"):
        connection.execute(
            "DELETE FROM finops_ledger_entries WHERE finops_ledger_entry_id=?", (manual_entry,)
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="finops_ledger_append_only"):
        connection.execute(
            """
            INSERT OR REPLACE INTO finops_ledger_entries(
              finops_ledger_entry_id, event_id, tenant_id, entry_type, direction,
              amount_micros, currency, normalized_cny_micros,
              cny_normalization_status, price_evidence_status, posting_source,
              quota_type, finops_adjustment_id, operation_request_id,
              operation_trace_id, actor_user_id, status, created_at, updated_at
            ) SELECT ?, event_id, tenant_id, entry_type, direction, amount_micros,
                     currency, normalized_cny_micros, cny_normalization_status,
                     price_evidence_status, posting_source, quota_type,
                     finops_adjustment_id, operation_request_id, operation_trace_id,
                     actor_user_id, status, created_at, updated_at
              FROM finops_ledger_entries WHERE finops_ledger_entry_id=?
            """,
            (f"fle_{'e' * 32}", manual_entry),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="price_snapshot_append_only"):
        connection.execute(
            "UPDATE price_snapshots SET source='changed' WHERE price_snapshot_id=?",
            (snapshot_id,),
        )
    connection.rollback()
    assert connection.execute(
        """
        SELECT COALESCE(SUM(amount_micros), 0) FROM finops_ledger_entries
        WHERE tenant_id=? AND entry_type='reversal' AND reversal_of_entry_id=?
        """,
        (seed.tenant_id, target["finops_ledger_entry_id"]),
    ).fetchone()[0] == 400_000
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


@pytest.mark.parametrize(
    "fact_kind",
    ["adjustment", "canonical_subscription", "alias_subscription", "delivery", "attempt"],
)
def test_prd051_down_blocks_each_irreversible_fact_atomically(
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """条件 down：Adjustment、两种订阅名、Delivery/Attempt 任一存在都在 DDL 前拒绝。"""

    database_url = _sqlite_url(tmp_path / f"prd051-down-{fact_kind}.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)

    if fact_kind == "adjustment":
        _insert_adjustment(
            connection,
            seed.tenant_id,
            metadata["user_id"],
            "a_down",
            adjustment_type="manual_credit",
            amount_micros=1,
        )
    else:
        endpoint_id = f"wh_prd051_{fact_kind}"
        event_types = {
            "canonical_subscription": '["ledger.entry_created"]',
            "alias_subscription": '["billing.ledger_created"]',
        }.get(fact_kind, "[]")
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
              encrypted_secret, secret_prefix, secret_fingerprint, status,
              created_by, updated_by, created_at, updated_at
            ) VALUES(?, ?, 'PRD051', 'https://example.invalid/webhook', ?,
                     'encrypted-test-only', 'whsec_test', ?, 'active', ?, ?, ?, ?)
            """,
            (
                endpoint_id,
                seed.tenant_id,
                event_types,
                f"fingerprint-{fact_kind}",
                metadata["user_id"],
                metadata["user_id"],
                "2026-07-26T12:00:00Z",
                "2026-07-26T12:00:00Z",
            ),
        )
        if fact_kind in {"delivery", "attempt"}:
            connection.execute(
                """
                INSERT INTO webhook_deliveries(
                  webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id,
                  event_type, event_timestamp, payload_json, status,
                  attempt_count, max_attempts, created_at, updated_at
                ) VALUES('whd_prd051_down', ?, ?, ?, 'ledger.entry_created', ?, '{}',
                         'pending', 0, 3, ?, ?)
                """,
                (
                    seed.tenant_id,
                    endpoint_id,
                    f"flevt_{'9' * 32}",
                    "2026-07-26T12:00:00Z",
                    "2026-07-26T12:00:00Z",
                    "2026-07-26T12:00:00Z",
                ),
            )
        if fact_kind == "attempt":
            connection.execute(
                """
                INSERT INTO webhook_delivery_attempts(
                  webhook_delivery_attempt_id, tenant_id, webhook_delivery_id,
                  webhook_endpoint_id, attempt_no, status, created_at
                ) VALUES('wha_prd051_down', ?, 'whd_prd051_down', ?, 1, 'failed', ?)
                """,
                (seed.tenant_id, endpoint_id, "2026-07-26T12:00:01Z"),
            )
    connection.commit()
    schema_before = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('finops_adjustments','finops_ledger_entries')"
    ).fetchone()[0]

    assert rollback_last_migration(database_url).rolled_back == "0042"
    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd051_down_business_facts_present" in exc.value.message
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('finops_adjustments','finops_ledger_entries')"
    ).fetchone()[0] == schema_before == 2
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0041'"
    ).fetchone()[0] == "applied"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_prd051_invalid_historical_amount_aborts_before_persistent_ddl(tmp_path: Path) -> None:
    """异常流程：七位小数历史金额在 Audit/FinOps 持久 DDL 前拒绝并完整回滚。"""

    database_url = _sqlite_url(tmp_path / "prd051-invalid-history.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0042"
    assert rollback_last_migration(database_url).rolled_back == "0041"
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    request_id, _ = _insert_request(connection, seed.tenant_id, metadata, "invalid_precision")
    _insert_usage(
        connection,
        seed.tenant_id,
        metadata,
        request_id,
        "invalid_precision",
        amount="0.1234567",
        normalized={
            "cost_amount": "0.1234567",
            "cost_cny": "0.1234567",
            "currency": "CNY",
        },
    )
    connection.commit()
    audit_sql_before = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_logs'"
    ).fetchone()[0]
    connection.close()

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd051_usage_preflight_failed" in exc.value.message
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'finops_%'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_logs'"
    ).fetchone()[0] == audit_sql_before
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
