"""PRD-079 Gateway 额度预留、结算、并发与恢复专项测试。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from metahub_gateway import quota as quota_package
from metahub_gateway.quota import (
    enforce_quota_guard,
    finalize_request_quota_failure,
    reconcile_expired_quota_reservations,
    release_request_quota,
)
from metahub_gateway.quota import service as quota_service
from metahub_gateway.routing.attempts import create_routing_attempt
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan
from metahub_gateway.usage.ledger import (
    RequestLogContext,
    claim_gateway_request,
    record_request_log,
    replay_usage_spool_record,
    safe_record_usage_ledger,
)
from metahub_gateway.usage.spool import UsageSpool
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.routing_policy import TokenEstimate

# 两个冻结数据库时间用于构造超期与未超期恢复边界，避免依赖真实系统时钟。
_PAST_NOW = "2026-07-01T00:00:00Z"
_CURRENT_NOW = "2098-01-01T00:00:00Z"


@pytest.fixture(scope="module")
def quota_database_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """从空库迁移并 seed 一次，后续用例只复制关闭的 SQLite 文件。"""

    root = tmp_path_factory.mktemp("prd079-quota-template")
    database = root / "template.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    result = apply_migrations(database_url)
    # The current migration chain ends at 0164; this test exercises the same
    # quota contract against the complete schema used by the application.
    assert result.applied[-1] == "0164"
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
    return database


def _database(template: Path, target: Path) -> SimpleNamespace:
    """复制关闭的模板库并返回绑定默认租户的隔离测试配置。"""

    shutil.copy2(template, target)
    database_url = f"sqlite:///{target.as_posix()}"
    connection = connect_database(database_url)
    try:
        tenant_id = str(
            connection.execute(
                "SELECT tenant_id FROM tenants WHERE slug='default'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    return SimpleNamespace(
        database_url=database_url,
        tenant_id=tenant_id,
        finops_usage_spool_root=str(target.parent / f"{target.stem}-spool"),
        finops_usage_spool_claim_lease_seconds=60,
        auth_token_secret="prd079-gateway-test-auth-secret-with-sufficient-entropy",
    )


def _seed_plan(
    config: SimpleNamespace,
    grants: dict[str, int],
    *,
    status: str = "active",
    trial_active: bool = True,
    trial_ends_at: str = "2099-01-01T00:00:00Z",
) -> str:
    """为指定租户写入套餐和四类额度 Bucket，返回新套餐标识。"""

    plan_id = f"pln_{uuid4().hex}"
    now = "2026-08-14T00:00:00Z"
    connection = connect_database(config.database_url)
    try:
        connection.execute(
            """
            INSERT INTO plans(
              plan_id,tenant_id,display_name,base_tier,status,trial_status,
              trial_started_at,trial_ends_at,trial_cycle,state_revision,
              quota_generation,created_at,updated_at
            ) VALUES(?,?,?,'paid',?,?,?,?,?,1,1,?,?)
            """,
            (
                plan_id,
                config.tenant_id,
                "PRD-079 Test Plan",
                status,
                "active" if trial_active else "not_started",
                "2026-01-01T00:00:00Z" if trial_active else None,
                trial_ends_at if trial_active else None,
                1 if trial_active else 0,
                now,
                now,
            ),
        )
        for quota_type in ("trial", "free", "gift", "paid"):
            granted = grants.get(quota_type, 0)
            bucket_status = "suspended" if status == "suspended" else (
                "active" if granted else "exhausted"
            )
            connection.execute(
                """
                INSERT INTO quota_buckets(
                  quota_bucket_id,tenant_id,plan_id,quota_type,granted_micros,
                  consumed_micros,reserved_micros,status,expires_at,state_revision,
                  created_at,updated_at
                ) VALUES(?,?,?,?,?,0,0,?,?,1,?,?)
                """,
                (
                    f"qbk_{uuid4().hex}",
                    config.tenant_id,
                    plan_id,
                    quota_type,
                    granted,
                    bucket_status,
                    trial_ends_at if quota_type == "trial" and trial_active else None,
                    now,
                    now,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return plan_id


def _guard(
    config: SimpleNamespace,
    suffix: str,
    amount_micros: int,
):
    """使用可信微元成本调用正式额度准入，并冻结测试请求链路。"""

    return enforce_quota_guard(
        config,
        tenant_id=config.tenant_id,
        request_id=f"req-prd079-{suffix}",
        trace_id=f"trace-prd079-{suffix}",
        route=object(),
        payload={},
        request_kind="chat",
        trusted_cost_cny=Decimal(amount_micros) / Decimal(1_000_000),
    )


def _metadata(config: SimpleNamespace) -> dict[str, str]:
    """读取同租户真实路由元数据并写入测试所需的版本化价格。"""

    connection = connect_database(config.database_url)
    try:
        row = connection.execute(
            """
            SELECT model.model_id,model.model_alias,
                   COALESCE(model.provider_model_id,model.upstream_model_name)
                     AS upstream_model_name,
                   model.capabilities,provider.provider_id,provider.provider_code,
                   provider.provider_type,provider.auth_type,provider.name,provider.base_url,
                   key.virtual_key_id,environment.environment_id,
                   environment.code AS environment_code
            FROM models model
            JOIN providers provider
              ON provider.tenant_id=model.tenant_id
             AND provider.provider_id=model.provider_id
            JOIN virtual_keys key ON key.tenant_id=model.tenant_id
            JOIN environments environment ON environment.tenant_id=model.tenant_id
            WHERE model.tenant_id=? AND model.status='active' AND environment.code='dev'
              AND NOT EXISTS(SELECT 1 FROM model_price_plans price
                WHERE price.tenant_id=model.tenant_id AND price.model_id=model.model_id)
            ORDER BY model.model_id,key.virtual_key_id
            LIMIT 1
            """,
            (config.tenant_id,),
        ).fetchone()
        assert row is not None
        connection.execute(
            """
            UPDATE models SET input_price_per_1k='1.000000',output_price_per_1k='0',
              currency='CNY',price_schema_version='prd079-test-price-v1'
            WHERE tenant_id=? AND model_id=?
            """,
            (config.tenant_id, row["model_id"]),
        )
        # This legacy quota fixture exercises model-catalog pricing. Disable
        # the seeded workspace rate so the resolver intentionally falls back
        # to the model price it installs below.
        connection.execute(
            """
            UPDATE workspace_provider_cost_rate_heads
            SET adoption_state='disabled',head_revision=head_revision+1
            WHERE workspace_provider_cost_rate_id IN(
              SELECT workspace_provider_cost_rate_id FROM workspace_provider_cost_rates
              WHERE tenant_id=? AND model_id=?
            )
            """,
            (config.tenant_id, row["model_id"]),
        )
        connection.commit()
        return {name: str(row[name]) for name in row.keys()}
    finally:
        connection.close()


def _context(config: SimpleNamespace, suffix: str) -> RequestLogContext:
    """构造并持久化同租户请求上下文，供正式用量结算路径复用。"""

    metadata = _metadata(config)
    context = RequestLogContext(
        tenant_id=config.tenant_id,
        request_id=f"req-prd079-{suffix}",
        trace_id=f"trace-prd079-{suffix}",
        virtual_key_id=metadata["virtual_key_id"],
        environment_id=metadata["environment_id"],
        environment_code=metadata["environment_code"],
        provider_id=metadata["provider_id"],
        model_id=metadata["model_id"],
        endpoint="chat/completions",
        request_model=metadata["model_alias"],
        resolved_model=metadata["model_alias"],
        provider_code=metadata["provider_code"],
    )
    claim_gateway_request(config, context)
    record_request_log(
        config,
        context,
        status="success",
        error_code=None,
        latency_ms=1,
    )
    return context


def _route(config: SimpleNamespace) -> GatewayRoute:
    """从当前租户的真实目录行构造额度估价使用的冻结路由。"""

    metadata = _metadata(config)
    capabilities = json.loads(metadata["capabilities"])
    assert isinstance(capabilities, dict)
    return GatewayRoute(
        tenant_id=config.tenant_id,
        model_id=metadata["model_id"],
        model_alias=metadata["model_alias"],
        upstream_model_name=metadata["upstream_model_name"],
        capabilities=capabilities,
        provider_id=metadata["provider_id"],
        provider_code=metadata["provider_code"],
        provider_type=metadata["provider_type"],
        provider_auth_type=metadata["auth_type"],
        provider_name=metadata["name"],
        provider_base_url=metadata["base_url"],
        environment=metadata["environment_code"],
        environment_id=metadata["environment_id"],
    )


def _record_usage(config: SimpleNamespace, context: RequestLogContext, amount_micros: int):
    """把精确微元换算为测试 Token，并通过正式用量入口持久化。"""

    # Current prices are denominated per million tokens.  The fixture's model
    # rate is 1 CNY per million, so one token maps to one micro-CNY here.
    input_tokens = amount_micros
    # 用量必须走正式耐久写入入口，才能同时验证额度和财务原子结算。
    return safe_record_usage_ledger(
        config,
        context,
        {
            "prompt_tokens": input_tokens,
            "completion_tokens": 0,
            "total_tokens": input_tokens,
            "cache_tokens": 0,
            "estimated": False,
            "usage_source": "upstream",
            "raw_usage": {"prompt_tokens": input_tokens, "completion_tokens": 0},
        },
        require_durable=True,
    )


@pytest.mark.parametrize(
    ("actual_micros", "expected", "status", "unallocated"),
    [
        (5_000, {"trial": 2_000, "free": 3_000}, "settled", 0),
        (
            10_000,
            {"trial": 2_000, "free": 3_000, "gift": 4_000, "paid": 1_000},
            "settled",
            0,
        ),
        (
            13_000,
            {"trial": 2_000, "free": 3_000, "gift": 4_000, "paid": 4_000},
            "settled",
            0,
        ),
        (
            25_000,
            {"trial": 2_000, "free": 3_000, "gift": 4_000, "paid": 10_000},
            "overage",
            6_000,
        ),
    ],
)
def test_quota_order_and_actual_settlement_boundaries(
    quota_database_template: Path,
    tmp_path: Path,
    actual_micros: int,
    expected: dict[str, int],
    status: str,
    unallocated: int,
) -> None:
    """验证四类额度顺序、实际金额边界和超额结算事实保持一致。"""

    config = _database(quota_database_template, tmp_path / "settlement.sqlite3")
    _seed_plan(config, {"trial": 2_000, "free": 3_000, "gift": 4_000, "paid": 10_000})
    context = _context(config, "settlement")
    guard = _guard(config, "settlement", 10_000)
    assert guard.reservation_id is not None
    committed = _record_usage(
        config,
        replace(context, quota_reservation_outstanding_cny="0"),
        actual_micros,
    )
    assert committed is not None

    connection = connect_database(config.database_url)
    try:
        allocations = connection.execute(
            """
            SELECT quota_type,sequence,amount_micros FROM quota_usage_allocations
            WHERE tenant_id=? ORDER BY sequence
            """,
            (config.tenant_id,),
        ).fetchall()
        settlement = connection.execute(
            "SELECT * FROM quota_usage_settlements WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()
        reservation = connection.execute(
            "SELECT status,outstanding_micros FROM quota_reservations WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()
        buckets = connection.execute(
            """
            SELECT quota_type,consumed_micros,reserved_micros FROM quota_buckets
            WHERE tenant_id=?
            """,
            (config.tenant_id,),
        ).fetchall()
        assert [row["quota_type"] for row in allocations] == list(expected)
        assert [row["sequence"] for row in allocations] == list(
            range(1, len(expected) + 1)
        )
        assert {row["quota_type"]: row["amount_micros"] for row in allocations} == expected
        assert settlement["status"] == status
        assert settlement["unallocated_micros"] == unallocated
        assert tuple(reservation) == (status, 0)
        assert {row["quota_type"]: row["consumed_micros"] for row in buckets} == {
            quota_type: expected.get(quota_type, 0)
            for quota_type in ("trial", "free", "gift", "paid")
        }
        assert all(row["reserved_micros"] == 0 for row in buckets)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_exposure_settlement_consumes_actual_before_preserving_risk(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """Fallback 费用先消费 trial，主路由未决暴露再占后续余额，不能颠倒额度顺序。"""

    config = _database(quota_database_template, tmp_path / "exposure-order.sqlite3")
    _seed_plan(config, {"trial": 100_000, "free": 100_000})
    context = replace(
        _context(config, "exposure-order"),
        quota_reservation_outstanding_cny="0.100000",
    )
    assert _guard(config, "exposure-order", 200_000).reservation_id is not None

    committed = _record_usage(config, context, 50_000)
    assert committed is not None
    connection = connect_database(config.database_url)
    try:
        allocation = connection.execute(
            "SELECT quota_type,amount_micros FROM quota_usage_allocations"
        ).fetchone()
        reservation = connection.execute(
            """
            SELECT status,estimated_micros,actual_micros,outstanding_micros
            FROM quota_reservations
            """
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT amount_micros,exposure_outstanding_micros,allocated_micros,status
            FROM quota_usage_settlements
            """
        ).fetchone()
        items = connection.execute(
            """
            SELECT quota_type,consumed_micros,released_micros,status
            FROM quota_reservation_items ORDER BY priority
            """
        ).fetchall()
        buckets = connection.execute(
            """
            SELECT quota_type,consumed_micros,reserved_micros
            FROM quota_buckets WHERE quota_type IN('trial','free') ORDER BY quota_type
            """
        ).fetchall()
        assert tuple(allocation) == ("trial", 50_000)
        assert tuple(reservation) == ("exposure_pending", 200_000, 50_000, 100_000)
        assert tuple(settlement) == (50_000, 100_000, 50_000, "settled")
        assert [tuple(row) for row in items] == [
            ("trial", 50_000, 0, "exposure_pending"),
            ("free", 0, 50_000, "exposure_pending"),
        ]
        assert {row["quota_type"]: tuple(row)[1:] for row in buckets} == {
            "free": (0, 50_000),
            "trial": (50_000, 50_000),
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_exposure_actual_above_admission_without_spare_balance_is_unallocated(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """E=200k、O=100k、A=150k 时，额外 50k 必须形成可追踪 overage。"""

    config = _database(quota_database_template, tmp_path / "exposure-overage.sqlite3")
    _seed_plan(config, {"free": 200_000}, trial_active=False)
    context = replace(
        _context(config, "exposure-overage"),
        quota_reservation_outstanding_cny="0.100000",
    )
    assert _guard(config, "exposure-overage", 200_000).reservation_id is not None

    committed = _record_usage(config, context, 150_000)
    assert committed is not None
    connection = connect_database(config.database_url)
    try:
        reservation = connection.execute(
            """
            SELECT status,estimated_micros,actual_micros,outstanding_micros,
                   unallocated_micros
            FROM quota_reservations
            """
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT status,amount_micros,exposure_outstanding_micros,
                   allocated_micros,unallocated_micros
            FROM quota_usage_settlements
            """
        ).fetchone()
        allocation = connection.execute(
            "SELECT quota_type,amount_micros FROM quota_usage_allocations"
        ).fetchone()
        bucket = connection.execute(
            """
            SELECT consumed_micros,reserved_micros FROM quota_buckets
            WHERE quota_type='free'
            """
        ).fetchone()
        assert tuple(reservation) == (
            "exposure_pending",
            200_000,
            150_000,
            100_000,
            50_000,
        )
        assert tuple(settlement) == ("overage", 150_000, 100_000, 100_000, 50_000)
        assert tuple(allocation) == ("free", 100_000)
        assert tuple(bucket) == (100_000, 100_000)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_exposure_actual_above_admission_consumes_spare_gift_balance(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """E=200k、O=100k、A=150k 时，预留外的 50k 按顺序从 gift 补扣。"""

    config = _database(quota_database_template, tmp_path / "exposure-gift.sqlite3")
    _seed_plan(config, {"free": 200_000, "gift": 50_000}, trial_active=False)
    context = replace(
        _context(config, "exposure-gift"),
        quota_reservation_outstanding_cny="0.100000",
    )
    assert _guard(config, "exposure-gift", 200_000).reservation_id is not None

    committed = _record_usage(config, context, 150_000)
    assert committed is not None
    connection = connect_database(config.database_url)
    try:
        reservation = connection.execute(
            """
            SELECT status,estimated_micros,actual_micros,outstanding_micros,
                   unallocated_micros
            FROM quota_reservations
            """
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT status,amount_micros,exposure_outstanding_micros,
                   allocated_micros,unallocated_micros
            FROM quota_usage_settlements
            """
        ).fetchone()
        allocations = connection.execute(
            """
            SELECT quota_type,sequence,amount_micros FROM quota_usage_allocations
            ORDER BY sequence
            """
        ).fetchall()
        buckets = connection.execute(
            """
            SELECT quota_type,consumed_micros,reserved_micros FROM quota_buckets
            WHERE quota_type IN('free','gift')
            """
        ).fetchall()
        assert tuple(reservation) == (
            "exposure_pending",
            200_000,
            150_000,
            100_000,
            0,
        )
        assert tuple(settlement) == ("settled", 150_000, 100_000, 150_000, 0)
        assert [tuple(row) for row in allocations] == [
            ("free", 1, 100_000),
            ("gift", 2, 50_000),
        ]
        assert {row["quota_type"]: tuple(row)[1:] for row in buckets} == {
            "free": (100_000, 100_000),
            "gift": (50_000, 0),
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_unknown_actual_consumes_only_estimate_after_exposure(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """actual=None 时只消费 E-O，不能把同一 100k 风险重复算作 charge。"""

    config = _database(quota_database_template, tmp_path / "exposure-unknown.sqlite3")
    _seed_plan(config, {"free": 200_000}, trial_active=False)
    context = replace(
        _context(config, "exposure-unknown"),
        quota_reservation_outstanding_cny="0.100000",
    )
    assert _guard(config, "exposure-unknown", 200_000).reservation_id is not None
    connection = connect_database(config.database_url)
    try:
        connection.execute(
            """
            UPDATE models SET currency='USD',price_schema_version='prd079-test-price-usd-v1'
            WHERE tenant_id=? AND model_id=?
            """,
            (config.tenant_id, context.model_id),
        )
        connection.commit()
    finally:
        connection.close()

    committed = _record_usage(config, context, 100_000)
    assert committed is not None
    assert (committed.currency, committed.cny_normalization_status) == (
        "USD",
        "unavailable",
    )
    assert committed.normalized_cny is None
    connection = connect_database(config.database_url)
    try:
        reservation = connection.execute(
            """
            SELECT status,estimated_micros,actual_micros,outstanding_micros,
                   unallocated_micros
            FROM quota_reservations
            """
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT amount_basis,amount_micros,exposure_outstanding_micros,
                   allocated_micros,unallocated_micros
            FROM quota_usage_settlements
            """
        ).fetchone()
        bucket = connection.execute(
            """
            SELECT consumed_micros,reserved_micros FROM quota_buckets
            WHERE quota_type='free'
            """
        ).fetchone()
        assert tuple(reservation) == (
            "exposure_pending",
            200_000,
            100_000,
            100_000,
            0,
        )
        assert tuple(settlement) == (
            "admission_estimate",
            100_000,
            100_000,
            100_000,
            0,
        )
        assert tuple(bucket) == (100_000, 100_000)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_failure_exposure_can_shrink_twice_before_late_charge(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """正额失败覆盖可重复缩小，已释放 item 不阻断随后 canonical charge 结算。"""

    config = _database(quota_database_template, tmp_path / "exposure-resize.sqlite3")
    _seed_plan(config, {"trial": 100_000, "free": 100_000})
    context = _context(config, "exposure-resize")
    assert _guard(config, "exposure-resize", 200_000).reservation_id is not None

    assert finalize_request_quota_failure(
        config,
        tenant_id=config.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        outstanding_cost_cny=Decimal("0.100000"),
    ) is True
    assert finalize_request_quota_failure(
        config,
        tenant_id=config.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        outstanding_cost_cny=Decimal("0.050000"),
    ) is True

    committed = _record_usage(
        config,
        replace(context, quota_reservation_outstanding_cny="0"),
        75_000,
    )
    assert committed is not None
    connection = connect_database(config.database_url)
    try:
        reservation = connection.execute(
            "SELECT status,actual_micros,outstanding_micros FROM quota_reservations"
        ).fetchone()
        free_item = connection.execute(
            """
            SELECT consumed_micros,released_micros,status
            FROM quota_reservation_items WHERE quota_type='free'
            """
        ).fetchone()
        buckets = connection.execute(
            """
            SELECT quota_type,consumed_micros,reserved_micros
            FROM quota_buckets WHERE quota_type IN('trial','free')
            """
        ).fetchall()
        assert tuple(reservation) == ("settled", 75_000, 0)
        assert tuple(free_item) == (0, 100_000, "released")
        assert {row["quota_type"]: tuple(row)[1:] for row in buckets} == {
            "trial": (75_000, 0),
            "free": (0, 0),
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_quota_settlement_same_charge_replay_is_idempotent(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """验证同 owner、同金额和同依据的 canonical charge 重放只读取既有结算。"""

    config = _database(quota_database_template, tmp_path / "settlement-replay.sqlite3")
    plan_id = _seed_plan(config, {"free": 20_000}, trial_active=False)
    context = _context(config, "settlement-replay")
    guard = _guard(config, "settlement-replay", 10_000)
    assert guard.reservation_id is not None
    assert _record_usage(
        config,
        replace(context, quota_reservation_outstanding_cny="0"),
        10_000,
    ) is not None

    connection = connect_database(config.database_url)
    try:
        settlement = connection.execute(
            "SELECT * FROM quota_usage_settlements WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()
        assert settlement is not None
        connection.execute("BEGIN IMMEDIATE")
        replay = quota_service.settle_quota_usage(
            connection,
            tenant_id=config.tenant_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            usage_ledger_id=str(settlement["usage_ledger_id"]),
            finops_ledger_entry_id=str(settlement["finops_ledger_entry_id"]),
            actual_micros=10_000,
        )
        connection.commit()
        assert replay.allocation_class == "free"
        assert replay.allocation_count == 1
        assert settlement["plan_id"] == plan_id
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_usage_settlements WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()[0] == 1
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


@pytest.mark.parametrize(
    "replay_actual_micros",
    [
        pytest.param(11_000, id="different-amount"),
        pytest.param(None, id="same-amount-different-basis"),
    ],
)
def test_quota_settlement_replay_rejects_canonical_charge_drift(
    quota_database_template: Path,
    tmp_path: Path,
    replay_actual_micros: int | None,
) -> None:
    """验证金额或 amount_basis 漂移时关闭重放，且既有结算保持不变。"""

    config = _database(
        quota_database_template,
        tmp_path / f"settlement-conflict-{replay_actual_micros}.sqlite3",
    )
    _seed_plan(config, {"free": 20_000}, trial_active=False)
    context = _context(config, "settlement-conflict")
    guard = _guard(config, "settlement-conflict", 10_000)
    assert guard.reservation_id is not None
    assert _record_usage(
        config,
        replace(context, quota_reservation_outstanding_cny="0"),
        10_000,
    ) is not None

    connection = connect_database(config.database_url)
    try:
        settlement = connection.execute(
            "SELECT * FROM quota_usage_settlements WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()
        assert settlement is not None
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="quota_settlement_replay_conflict",
        ):
            quota_service.settle_quota_usage(
                connection,
                tenant_id=config.tenant_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                usage_ledger_id=str(settlement["usage_ledger_id"]),
                finops_ledger_entry_id=str(settlement["finops_ledger_entry_id"]),
                actual_micros=replay_actual_micros,
            )
        connection.rollback()
        persisted = connection.execute(
            "SELECT amount_micros,amount_basis FROM quota_usage_settlements WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()
        assert tuple(persisted) == (10_000, "actual_cny")
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def test_corrupted_reservation_item_order_fails_admission_and_settlement_closed(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """验证持久 item 顺序漂移后既不能重放准入，也不能提交部分 canonical charge。"""

    config = _database(quota_database_template, tmp_path / "item-order-corrupt.sqlite3")
    _seed_plan(config, {"trial": 5_000, "free": 5_000})
    context = _context(config, "item-order-corrupt")
    guard = _guard(config, "item-order-corrupt", 7_000)
    assert guard.reservation_id is not None

    connection = connect_database(config.database_url)
    try:
        # 模拟磁盘级历史损坏；正式 trigger 先被显式移除，避免测试伪称公开 API 可篡改顺序。
        connection.execute("DROP TRIGGER trg_quota_reservation_items_transition_prd079")
        connection.execute(
            """
            UPDATE quota_reservation_items SET priority=1
            WHERE tenant_id=? AND quota_reservation_id=? AND quota_type='free'
            """,
            (config.tenant_id, guard.reservation_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ApiError) as caught:
        _guard(config, "item-order-corrupt", 7_000)
    assert caught.value.code == "gateway.quota_unavailable"
    assert _record_usage(config, context, 3_000) is None

    connection = connect_database(config.database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM finops_ledger_entries").fetchone()[0] == 0
        reservation = connection.execute(
            "SELECT status,outstanding_micros FROM quota_reservations WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()
        assert tuple(reservation) == ("active", 7_000)
    finally:
        connection.close()
    claims = UsageSpool(config).claim_batch(limit=2)
    assert len(claims) == 1


def test_concurrent_admission_and_exhaustion_leave_no_partial_reservation(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """验证并发准入仅一方成功，耗尽方不留下任何部分预留。"""

    config = _database(quota_database_template, tmp_path / "concurrency.sqlite3")
    _seed_plan(config, {"free": 10_000}, trial_active=False)
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def reserve(suffix: str) -> None:
        """同步发起一次额度准入，并只收集稳定公开结果代码。"""

        barrier.wait()
        try:
            _guard(config, suffix, 7_000)
            outcome = "reserved"
        # 并发耗尽只能暴露统一错误码，测试不依赖底层异常文本。
        except ApiError as exc:
            outcome = exc.code
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=reserve, args=(str(index),)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert sorted(results) == ["gateway.quota_exhausted", "reserved"]
    connection = connect_database(config.database_url)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_reservations WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT reserved_micros FROM quota_buckets
            WHERE tenant_id=? AND quota_type='free'
            """,
            (config.tenant_id,),
        ).fetchone()[0] == 7_000
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_events WHERE tenant_id=? AND event_type='quota.exhausted'",
            (config.tenant_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_release_then_late_usage_is_traceable_overage(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """验证释放后的迟到用量形成可追踪超额且不会再次扣减余额。"""

    config = _database(quota_database_template, tmp_path / "late-usage.sqlite3")
    _seed_plan(config, {"free": 10_000}, trial_active=False)
    context = _context(config, "late")
    _guard(config, "late", 7_000)
    assert release_request_quota(
        config,
        tenant_id=config.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
    )
    _record_usage(config, context, 3_000)

    connection = connect_database(config.database_url)
    try:
        reservation = connection.execute(
            "SELECT status,actual_micros FROM quota_reservations WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT status,allocated_micros,unallocated_micros,allocation_class
            FROM quota_usage_settlements WHERE tenant_id=?
            """,
            (config.tenant_id,),
        ).fetchone()
        assert tuple(reservation) == ("released", 0)
        assert tuple(settlement) == ("overage", 0, 3_000, "unallocated")
    finally:
        connection.close()


def test_trial_expiry_falls_back_to_free_and_is_idempotent(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """验证试用到期后切换免费额度，并且到期事件只生成一次。"""

    config = _database(quota_database_template, tmp_path / "trial-expiry.sqlite3")
    _seed_plan(
        config,
        {"trial": 5_000, "free": 5_000},
        trial_ends_at="2026-02-01T00:00:00Z",
    )
    result = _guard(config, "trial-expiry", 3_000)
    assert result.reservation_id is not None

    connection = connect_database(config.database_url)
    try:
        plan = connection.execute(
            "SELECT trial_status FROM plans WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()
        items = connection.execute(
            "SELECT quota_type,reserved_micros FROM quota_reservation_items WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchall()
        assert plan["trial_status"] == "expired"
        assert [tuple(row) for row in items] == [("free", 3_000)]
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_events WHERE tenant_id=? AND event_type='trial.expired'",
            (config.tenant_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_no_plan_is_compatible_and_suspended_plan_is_blocked(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """验证无套餐保持兼容，而停用套餐由网关后端强制阻断。"""

    no_plan = _database(quota_database_template, tmp_path / "no-plan.sqlite3")
    result = _guard(no_plan, "no-plan", 3_000)
    assert result.reservation_id is None and result.plan_id is None

    suspended = _database(quota_database_template, tmp_path / "suspended.sqlite3")
    _seed_plan(suspended, {"paid": 10_000}, status="suspended", trial_active=False)
    with pytest.raises(ApiError) as caught:
        _guard(suspended, "suspended", 3_000)
    assert caught.value.code == "gateway.plan_suspended"


def test_reconciliation_settles_charge_and_marks_unknown_release(
    quota_database_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证恢复任务结算权威费用，并显式标记结果未知的释放路径。"""

    charged = _database(quota_database_template, tmp_path / "reconcile-charge.sqlite3")
    _seed_plan(charged, {"free": 10_000}, trial_active=False)
    context = _context(charged, "reconcile-charge")
    original_now = quota_service._database_now
    monkeypatch.setattr(quota_service, "_database_now", lambda _connection: _PAST_NOW)
    _guard(charged, "reconcile-charge", 7_000)
    monkeypatch.setattr(quota_service, "_database_now", original_now)
    original_settle = quota_package.settle_quota_usage
    monkeypatch.setattr(quota_package, "settle_quota_usage", lambda *_args, **_kwargs: None)
    _record_usage(charged, context, 3_000)
    monkeypatch.setattr(quota_package, "settle_quota_usage", original_settle)
    assert reconcile_expired_quota_reservations(charged) == 1

    connection = connect_database(charged.database_url)
    try:
        assert connection.execute(
            "SELECT status FROM quota_reservations WHERE tenant_id=?",
            (charged.tenant_id,),
        ).fetchone()[0] == "settled"
        assert connection.execute(
            "SELECT amount_micros FROM quota_usage_settlements WHERE tenant_id=?",
            (charged.tenant_id,),
        ).fetchone()[0] == 3_000
    finally:
        connection.close()

    unknown = _database(quota_database_template, tmp_path / "reconcile-unknown.sqlite3")
    _seed_plan(unknown, {"free": 10_000}, trial_active=False)
    unknown_context = _context(unknown, "reconcile-unknown")
    monkeypatch.setattr(quota_service, "_database_now", lambda _connection: _PAST_NOW)
    _guard(unknown, "reconcile-unknown", 4_000)
    monkeypatch.setattr(quota_service, "_database_now", lambda _connection: _CURRENT_NOW)
    assert reconcile_expired_quota_reservations(unknown) == 1
    connection = connect_database(unknown.database_url)
    try:
        assert connection.execute(
            "SELECT status FROM quota_reservations WHERE tenant_id=?",
            (unknown.tenant_id,),
        ).fetchone()[0] == "reconciliation_required"
        assert connection.execute(
            "SELECT SUM(reserved_micros) FROM quota_buckets WHERE tenant_id=?",
            (unknown.tenant_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()

    # 未知结果释放后余额可能已被复用；迟到 Usage 必须保留 canonical cost，
    # 并形成独立 overage 事实而不是重新扣减任一 Bucket。
    _record_usage(unknown, unknown_context, 3_000)
    connection = connect_database(unknown.database_url)
    try:
        assert tuple(
            connection.execute(
                """
                SELECT status,unallocated_micros FROM quota_usage_settlements
                WHERE tenant_id=?
                """,
                (unknown.tenant_id,),
            ).fetchone()
        ) == ("overage", 3_000)
        assert connection.execute(
            "SELECT SUM(consumed_micros) FROM quota_buckets WHERE tenant_id=?",
            (unknown.tenant_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_reconciliation_keeps_failed_request_with_provider_attempt_unknown(
    quota_database_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """failed log 后 finalizer 崩溃时，已有 Provider attempt 必须保持结果未知。"""

    config = _database(quota_database_template, tmp_path / "reconcile-attempt.sqlite3")
    _seed_plan(config, {"free": 10_000}, trial_active=False)
    context = _context(config, "reconcile-attempt")
    monkeypatch.setattr(quota_service, "_database_now", lambda _connection: _PAST_NOW)
    assert _guard(config, "reconcile-attempt", 4_000).reservation_id is not None
    route = _route(config)
    create_routing_attempt(
        config,
        GatewayRoutePlan(initial_route=route, initial_role="primary"),
        route,
        request_id=context.request_id,
        trace_id=context.trace_id,
        attempt_sequence=1,
        attempt_role="primary",
        estimated_cost_cny=Decimal("0.004000"),
    )
    record_request_log(
        config,
        context,
        status="failed",
        error_code="gateway.upstream_error",
        latency_ms=1,
    )

    monkeypatch.setattr(quota_service, "_database_now", lambda _connection: _CURRENT_NOW)
    assert reconcile_expired_quota_reservations(config) == 1
    connection = connect_database(config.database_url)
    try:
        request_log = connection.execute(
            "SELECT status,error_code FROM request_logs WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT status,attempt_sequence FROM routing_attempts WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()
        reservation = connection.execute(
            """
            SELECT status,actual_micros,outstanding_micros FROM quota_reservations
            WHERE tenant_id=?
            """,
            (config.tenant_id,),
        ).fetchone()
        assert tuple(request_log) == ("failed", "gateway.upstream_error")
        assert tuple(attempt) == ("started", 1)
        assert tuple(reservation) == ("reconciliation_required", 0, 0)
        assert connection.execute(
            "SELECT SUM(reserved_micros) FROM quota_buckets WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_usage_settlements WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_active_plan_with_missing_versioned_price_fails_closed(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """Active Plan 缺少可审计 CNY 价格时返回 unavailable，不能伪造零额预留。"""

    config = _database(quota_database_template, tmp_path / "missing-price.sqlite3")
    _seed_plan(config, {"free": 10_000}, trial_active=False)
    route = _route(config)
    connection = connect_database(config.database_url)
    try:
        connection.execute(
            """
            UPDATE models SET price_schema_version=NULL
            WHERE tenant_id=? AND model_id=?
            """,
            (config.tenant_id, route.model_id),
        )
        connection.execute(
            """
            UPDATE workspace_provider_cost_rate_heads
            SET adoption_state='disabled',head_revision=head_revision+1
            WHERE workspace_provider_cost_rate_id IN(
              SELECT workspace_provider_cost_rate_id
              FROM workspace_provider_cost_rates
              WHERE tenant_id=? AND model_id=?
            )
            """,
            (config.tenant_id, route.model_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ApiError) as captured:
        enforce_quota_guard(
            config,
            tenant_id=config.tenant_id,
            request_id="req-prd079-missing-price",
            trace_id="trace-prd079-missing-price",
            route=route,
            payload={"messages": [{"role": "user", "content": "bounded"}]},
            request_kind="chat",
            token_estimate=TokenEstimate(
                input_tokens=1,
                output_tokens=1,
                truncated=False,
            ),
        )
    assert captured.value.code == "gateway.quota_unavailable"
    connection = connect_database(config.database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 0
        assert connection.execute(
            "SELECT SUM(reserved_micros) FROM quota_buckets WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM quota_events").fetchone()[0] == 0
    finally:
        connection.close()


def test_same_request_id_isolated_across_tenants_and_release_owner(
    quota_database_template: Path,
    tmp_path: Path,
) -> None:
    """相同 request_id 在两个租户形成独立 Reservation，释放只能命中可信 tenant owner。"""

    config = _database(quota_database_template, tmp_path / "tenant-isolation.sqlite3")
    other_tenant = "tenant_prd079_gateway_other"
    connection = connect_database(config.database_url)
    try:
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,?,?,'internal_only','active',?,?)
            """,
            (
                other_tenant,
                "prd079-gateway-other",
                "PRD-079 Gateway Other",
                "2026-08-14T00:00:00Z",
                "2026-08-14T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    other_config = SimpleNamespace(**{**vars(config), "tenant_id": other_tenant})
    _seed_plan(config, {"free": 10_000}, trial_active=False)
    _seed_plan(other_config, {"free": 20_000}, trial_active=False)

    first = _guard(config, "shared-owner", 7_000)
    second = _guard(other_config, "shared-owner", 9_000)
    assert first.reservation_id is not None
    assert second.reservation_id is not None
    assert first.reservation_id != second.reservation_id
    assert release_request_quota(
        config,
        tenant_id=config.tenant_id,
        request_id="req-prd079-shared-owner",
        trace_id="trace-prd079-shared-owner",
    ) is True

    connection = connect_database(config.database_url)
    try:
        reservations = connection.execute(
            """
            SELECT tenant_id,status,estimated_micros,outstanding_micros
            FROM quota_reservations WHERE request_id='req-prd079-shared-owner'
            ORDER BY tenant_id
            """
        ).fetchall()
        assert [tuple(row) for row in reservations] == sorted(
            [
                (config.tenant_id, "released", 7_000, 0),
                (other_tenant, "active", 9_000, 9_000),
            ]
        )
        default_reserved = connection.execute(
            "SELECT SUM(reserved_micros) FROM quota_buckets WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()[0]
        other_reserved = connection.execute(
            "SELECT SUM(reserved_micros) FROM quota_buckets WHERE tenant_id=?",
            (other_tenant,),
        ).fetchone()[0]
        assert default_reserved == 0
        assert other_reserved == 9_000
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_usage_spool_replay_settles_quota_once(
    quota_database_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主库结算瞬时失败时由 v7 保存正额暴露，重放后只结算 canonical actual。"""

    config = _database(quota_database_template, tmp_path / "spool-replay.sqlite3")
    _seed_plan(config, {"free": 10_000}, trial_active=False)
    context = replace(
        _context(config, "spool-replay"),
        quota_reservation_outstanding_cny="0.002000",
    )
    guard = _guard(config, "spool-replay", 7_000)
    assert guard.reservation_id is not None
    original_settle = quota_package.settle_quota_usage

    def fail_quota_settlement(*_args: object, **_kwargs: object) -> None:
        """在已捕获价格证据后模拟 SQLite 瞬时故障，触发正式 spool 回退。"""

        # 仅抛固定测试异常，不携带租户、请求、凭据或数据库路径信息。
        raise sqlite3.OperationalError("quota settlement temporarily unavailable")

    monkeypatch.setattr(quota_package, "settle_quota_usage", fail_quota_settlement)
    assert _record_usage(config, context, 3_000) is None

    connection = connect_database(config.database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM finops_ledger_entries").fetchone()[0] == 0
        reservation = connection.execute(
            "SELECT status,outstanding_micros FROM quota_reservations"
        ).fetchone()
        assert tuple(reservation) == ("active", 7_000)
        assert connection.execute(
            "SELECT SUM(reserved_micros) FROM quota_buckets WHERE tenant_id=?",
            (config.tenant_id,),
        ).fetchone()[0] == 7_000
    finally:
        connection.close()

    spool = UsageSpool(config)
    claims = spool.claim_batch(limit=1)
    assert len(claims) == 1
    payload = claims[0].payload
    assert payload["schema_version"] == 8
    assert payload["context"]["quota_reservation_outstanding_cny"] == "0.002000"
    monkeypatch.setattr(quota_package, "settle_quota_usage", original_settle)
    committed = replay_usage_spool_record(config, payload)
    assert committed.created is True
    spool.complete(claims[0])
    replayed = replay_usage_spool_record(config, payload)
    assert replayed.created is False
    assert replayed.ledger_entry_id == committed.ledger_entry_id
    assert spool.claim_batch(limit=1) == []

    connection = connect_database(config.database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM finops_ledger_entries").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_usage_settlements"
        ).fetchone()[0] == 1
        reservation = connection.execute(
            "SELECT status,actual_micros,outstanding_micros FROM quota_reservations"
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT status,amount_micros,exposure_outstanding_micros,
                   allocated_micros,unallocated_micros
            FROM quota_usage_settlements
            """
        ).fetchone()
        bucket = connection.execute(
            """
            SELECT consumed_micros,reserved_micros FROM quota_buckets
            WHERE tenant_id=? AND quota_type='free'
            """,
            (config.tenant_id,),
        ).fetchone()
        assert tuple(reservation) == ("exposure_pending", 3_000, 2_000)
        assert tuple(settlement) == ("settled", 3_000, 2_000, 3_000, 0)
        assert tuple(bucket) == (3_000, 2_000)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
