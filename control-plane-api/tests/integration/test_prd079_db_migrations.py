"""验证 PRD-079 0072 正反迁移、RBAC 保真和降级守卫。"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
import test_prd077_db_migrations as prd077
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

# 迁移目录、固定测试时间、权限集合和业务表清单用于约束 0072 正反向物理事实。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
SEED_PASSWORD = "Prd079MigrationOnly!2026"
NOW = "2026-08-14T00:00:00Z"
NEW_PERMISSIONS = (
    "billing.plan.read",
    "billing.plan.write",
    "billing.quota.read",
    "billing.quota.write",
)
QUOTA_TABLES = (
    "plans",
    "quota_buckets",
    "quota_adjustments",
    "quota_reservations",
    "quota_reservation_items",
    "quota_usage_settlements",
    "quota_usage_allocations",
    "quota_events",
    "quota_event_outbox",
)


def _url(path: Path) -> str:
    """把测试数据库路径转换为迁移器接受的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不高于指定版本的迁移对，为正反向测试冻结独立目录。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd079_migration_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """构造 seeded 0071 模板以及独立的 0071/0072 迁移目录。"""

    root = tmp_path_factory.mktemp("prd079-migration-templates")
    migrations_0071 = _migration_subset(root / "migrations-0071", 71)
    migrations_0072 = _migration_subset(root / "migrations-0072", 72)
    database = root / "template-0071.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations_0071).applied[-1] == "0071"
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = SEED_PASSWORD
    try:
        apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous
    return {
        "database_0071": database,
        "migrations_0071": migrations_0071,
        "migrations_0072": migrations_0072,
    }


def _clone(templates: dict[str, Path], destination: Path) -> str:
    """克隆只读模板数据库，保证每个测试拥有独立迁移事实。"""

    shutil.copy2(templates["database_0071"], destination)
    return _url(destination)


def _head(connection: sqlite3.Connection) -> str:
    """读取当前最高已应用迁移版本，失败时由测试断言直接阻断。"""

    row = connection.execute(
        """
        SELECT version FROM schema_migrations
        WHERE status='applied' ORDER BY version DESC LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return str(row[0])


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """断言数据库完整性通过且不存在任何复合外键违规。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _schema_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """冻结全部非内部 schema 物理定义，用于验证失败降级没有半执行 DDL。"""

    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type,name,tbl_name,sql FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name
            """
        )
    )


def _normalized_rbac_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    """忽略默认角色合法单调递增的 permission_revision，其余物理事实精确比较。"""

    normalized: list[tuple[object, ...]] = []
    for row in prd077._rbac_snapshot(connection):
        # 默认角色 revision 合法递增但其余列必须逐字保真，不能掩盖权限矩阵漂移。
        if row[0] == "roles":
            values = list(row)
            values[10] = None
            normalized.append(tuple(values))
        else:
            normalized.append(row)
    return tuple(normalized)


def _billing_grants(connection: sqlite3.Connection, role_code: str) -> tuple[str, ...]:
    """读取指定角色获得的 PRD-079 权限，结果按权限码稳定排序。"""

    return tuple(
        str(row[0])
        for row in connection.execute(
            """
            SELECT permission.code FROM roles role
            JOIN role_permissions relation
              ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE role.code=? AND permission.code IN(?,?,?,?)
            ORDER BY permission.code
            """,
            (role_code, *NEW_PERMISSIONS),
        )
    )


def test_prd079_upgrade_repeat_down_reup_preserves_rbac_and_audit(
    prd079_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证 0072 首次、重复、回滚和重升均保真 RBAC、审计行与数据库完整性。"""

    database_url = _clone(
        prd079_migration_templates,
        tmp_path / "roundtrip.sqlite3",
    )
    with connect_database(database_url) as connection:
        prd077._seed_legacy_audit(connection)
        prd077._customize_role_matrix(connection, "developer", "model.read")
        rbac_before = _normalized_rbac_snapshot(connection)
        audit_schema_before = prd077._audit_schema(connection)
        audit_row_before = tuple(
            connection.execute(
                "SELECT rowid,* FROM audit_logs WHERE audit_log_id='aud_prd077_legacy'"
            ).fetchone()
        )

    result = apply_migrations(
        database_url,
        migrations_dir=prd079_migration_templates["migrations_0072"],
    )
    assert result.applied == ["0072"]
    with connect_database(database_url) as connection:
        assert _head(connection) == "0072"
        assert tuple(
            connection.execute(
                """
                SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision)
                FROM rbac_permission_templates
                """
            ).fetchone()
        ) == (76, 15, 15)
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_role_permission_templates"
        ).fetchone()[0] == 202
        assert _billing_grants(connection, "admin") == tuple(sorted(NEW_PERMISSIONS))
        assert _billing_grants(connection, "finance_admin") == tuple(
            sorted(NEW_PERMISSIONS)
        )
        assert _billing_grants(connection, "developer") == ()
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'quota_%'"
            )
        } == set(QUOTA_TABLES) - {"plans"}
        assert tuple(
            connection.execute(
                "SELECT rowid,* FROM audit_logs WHERE audit_log_id='aud_prd077_legacy'"
            ).fetchone()
        ) == audit_row_before
        _assert_healthy(connection)
    assert apply_migrations(
        database_url,
        migrations_dir=prd079_migration_templates["migrations_0072"],
    ).applied == []

    rolled_back = rollback_last_migration(
        database_url,
        migrations_dir=prd079_migration_templates["migrations_0072"],
        expected_version="0072",
    )
    assert rolled_back.rolled_back == "0072"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0071"
        assert _normalized_rbac_snapshot(connection) == rbac_before
        assert prd077._audit_schema(connection) == audit_schema_before
        assert tuple(
            connection.execute(
                "SELECT rowid,* FROM audit_logs WHERE audit_log_id='aud_prd077_legacy'"
            ).fetchone()
        ) == audit_row_before
        assert not any(
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            for table in QUOTA_TABLES
        )
        _assert_healthy(connection)

    assert apply_migrations(
        database_url,
        migrations_dir=prd079_migration_templates["migrations_0072"],
    ).applied == ["0072"]
    with connect_database(database_url) as connection:
        assert _head(connection) == "0072"
        _assert_healthy(connection)


def _insert_plan(connection: sqlite3.Connection) -> None:
    """写入最小套餐业务事实，用于触发 0072 降级前置守卫。"""

    tenant_id = str(
        connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO plans(
          plan_id,tenant_id,display_name,base_tier,status,trial_status,
          trial_started_at,trial_ends_at,trial_cycle,state_revision,
          quota_generation,created_at,updated_at
        ) VALUES(?,?,'Migration guard','free','active','not_started',
                 NULL,NULL,0,1,1,?,?)
        """,
        (f"pln_{uuid4().hex}", tenant_id, NOW, NOW),
    )


def _insert_settlement_owner_facts(
    connection: sqlite3.Connection,
    *,
    reservation_status: str,
    reservation_actual_micros: int | None = None,
    currency: str = "CNY",
) -> tuple[str, ...]:
    """写入不含正文的 request、Usage 与 FinOps owner 事实供结算约束负测。"""

    _insert_plan(connection)
    plan = connection.execute("SELECT tenant_id,plan_id FROM plans").fetchone()
    assert plan is not None
    tenant_id = str(plan["tenant_id"])
    plan_id = str(plan["plan_id"])
    request_id = f"req_prd079_{uuid4().hex}"
    trace_id = f"trc_prd079_{uuid4().hex}"
    reservation_id = f"qres_{uuid4().hex}"
    usage_ledger_id = f"usg_{uuid4().hex}"
    actual_micros = (
        None
        if reservation_status == "active"
        else (100 if reservation_actual_micros is None else reservation_actual_micros)
    )
    outstanding_micros = 100 if reservation_status == "active" else 0
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
          policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,NULL,NULL,'disabled',?)
        """,
        (tenant_id, request_id, trace_id, NOW),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,status,created_at,updated_at
        ) VALUES(?,?,?,'success',?,?)
        """,
        (request_id, tenant_id, trace_id, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO quota_reservations(
          quota_reservation_id,tenant_id,plan_id,request_id,trace_id,
          estimated_micros,outstanding_micros,actual_micros,unallocated_micros,
          order_version,status,expires_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,100,?,?,0,'trial_free_gift_paid_v1',?,?,?,?)
        """,
        (
            reservation_id,
            tenant_id,
            plan_id,
            request_id,
            trace_id,
            outstanding_micros,
            actual_micros,
            reservation_status,
            NOW,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id,tenant_id,request_id,raw_usage,normalized_usage,
          cost_amount,cost_cny,cost_usd,currency,created_at,updated_at
        ) VALUES(?,?,?,'{}','{}',?,?,?,?,?,?)
        """,
        (
            usage_ledger_id,
            tenant_id,
            request_id,
            0.000100,
            0.000100 if currency == "CNY" else 0,
            0.000100 if currency == "USD" else 0,
            currency,
            NOW,
            NOW,
        ),
    )
    finops = connection.execute(
        """
        SELECT finops_ledger_entry_id FROM finops_ledger_entries
        WHERE tenant_id=? AND usage_ledger_id=? AND entry_type='usage_charge'
        """,
        (tenant_id, usage_ledger_id),
    ).fetchone()
    assert finops is not None
    return (
        tenant_id,
        plan_id,
        reservation_id,
        usage_ledger_id,
        str(finops["finops_ledger_entry_id"]),
        request_id,
        trace_id,
    )


def _insert_usage_settlement(
    connection: sqlite3.Connection,
    owner_facts: tuple[str, ...],
    *,
    amount_micros: int = 100,
    amount_basis: str = "actual_cny",
) -> None:
    """直接写入一条金额为 100 微元的结算，便于精确触发 owner/唯一性守卫。"""

    connection.execute(
        """
        INSERT INTO quota_usage_settlements(
          quota_usage_settlement_id,tenant_id,plan_id,quota_reservation_id,
          usage_ledger_id,finops_ledger_entry_id,request_id,trace_id,
          amount_micros,exposure_outstanding_micros,allocated_micros,
          unallocated_micros,amount_basis,allocation_class,allocation_count,
          status,plan_revision,order_version,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,0,?,0,?,'paid',1,
                 'settled',1,'trial_free_gift_paid_v1',?)
        """,
        (
            f"qset_{uuid4().hex}",
            *owner_facts,
            amount_micros,
            amount_micros,
            amount_basis,
            NOW,
        ),
    )


@pytest.mark.parametrize(
    ("currency", "reservation_actual", "settlement_amount", "amount_basis", "accepted"),
    (
        ("CNY", 101, 101, "actual_cny", False),
        ("CNY", 100, 100, "admission_estimate", False),
        ("USD", 100, 100, "actual_cny", False),
        ("USD", 99, 99, "admission_estimate", False),
        ("USD", 100, 100, "admission_estimate", True),
    ),
)
def test_prd079_settlement_binds_basis_to_canonical_charge(
    prd079_migration_templates: dict[str, Path],
    tmp_path: Path,
    currency: str,
    reservation_actual: int,
    settlement_amount: int,
    amount_basis: str,
    accepted: bool,
) -> None:
    """直接 SQL 不能伪造 canonical CNY 金额或 estimate basis。"""

    database_url = _clone(
        prd079_migration_templates,
        tmp_path
        / f"settlement-charge-{currency}-{reservation_actual}-{amount_basis}.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd079_migration_templates["migrations_0072"],
    ).applied == ["0072"]
    with connect_database(database_url) as connection:
        owner_facts = _insert_settlement_owner_facts(
            connection,
            reservation_status="settled",
            reservation_actual_micros=reservation_actual,
            currency=currency,
        )
        if accepted:
            _insert_usage_settlement(
                connection,
                owner_facts,
                amount_micros=settlement_amount,
                amount_basis=amount_basis,
            )
        else:
            with pytest.raises(sqlite3.IntegrityError) as caught:
                _insert_usage_settlement(
                    connection,
                    owner_facts,
                    amount_micros=settlement_amount,
                    amount_basis=amount_basis,
                )
            assert str(caught.value) == "quota_settlement_owner_mismatch"
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_usage_settlements"
        ).fetchone()[0] == int(accepted)
        _assert_healthy(connection)


def test_prd079_reservation_rejects_unallocated_above_actual(
    prd079_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证 Reservation header 不接受 unallocated 大于 canonical actual。"""

    database_url = _clone(
        prd079_migration_templates,
        tmp_path / "reservation-unallocated-over-actual.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd079_migration_templates["migrations_0072"],
    ).applied == ["0072"]
    with connect_database(database_url) as connection:
        _insert_plan(connection)
        plan = connection.execute("SELECT tenant_id,plan_id FROM plans").fetchone()
        assert plan is not None
        with pytest.raises(sqlite3.IntegrityError) as caught:
            connection.execute(
                """
                INSERT INTO quota_reservations(
                  quota_reservation_id,tenant_id,plan_id,request_id,trace_id,
                  estimated_micros,outstanding_micros,actual_micros,
                  unallocated_micros,order_version,status,expires_at,
                  created_at,updated_at
                ) VALUES(?,?,?,?,?,100,0,100,101,'trial_free_gift_paid_v1',
                         'overage',?,?,?)
                """,
                (
                    f"qres_{uuid4().hex}",
                    plan["tenant_id"],
                    plan["plan_id"],
                    f"req_prd079_{uuid4().hex}",
                    f"trc_prd079_{uuid4().hex}",
                    NOW,
                    NOW,
                    NOW,
                ),
            )
        assert "unallocated_micros<=actual_micros" in str(caught.value)
        assert connection.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 0
        _assert_healthy(connection)


def test_prd079_settlement_rejects_active_reservation(
    prd079_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证伪造 Settlement 不能越过仍处于 active 的 Reservation。"""

    database_url = _clone(
        prd079_migration_templates,
        tmp_path / "active-reservation-settlement.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd079_migration_templates["migrations_0072"],
    ).applied == ["0072"]
    with connect_database(database_url) as connection:
        owner_facts = _insert_settlement_owner_facts(
            connection,
            reservation_status="active",
        )
        with pytest.raises(sqlite3.IntegrityError) as caught:
            _insert_usage_settlement(connection, owner_facts)
        assert str(caught.value) == "quota_settlement_owner_mismatch"
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_usage_settlements"
        ).fetchone()[0] == 0
        _assert_healthy(connection)


def test_prd079_settlement_rejects_second_row_for_reservation(
    prd079_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证同一 Reservation 即使更换 Settlement ID 也只能结算一次。"""

    database_url = _clone(
        prd079_migration_templates,
        tmp_path / "duplicate-reservation-settlement.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd079_migration_templates["migrations_0072"],
    ).applied == ["0072"]
    with connect_database(database_url) as connection:
        owner_facts = _insert_settlement_owner_facts(
            connection,
            reservation_status="settled",
        )
        _insert_usage_settlement(connection, owner_facts)
        with pytest.raises(sqlite3.IntegrityError) as caught:
            _insert_usage_settlement(connection, owner_facts)
        assert str(caught.value) == "quota_settlement_owner_mismatch"
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_usage_settlements"
        ).fetchone()[0] == 1
        _assert_healthy(connection)


def test_prd079_money_columns_reject_values_above_shared_limit(
    prd079_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证数据库金额约束拒绝超过共享微元上限的额度桶。"""

    database_url = _clone(
        prd079_migration_templates,
        tmp_path / "money-upper-bound.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd079_migration_templates["migrations_0072"],
    ).applied == ["0072"]
    with connect_database(database_url) as connection:
        _insert_plan(connection)
        plan = connection.execute("SELECT tenant_id,plan_id FROM plans").fetchone()
        assert plan is not None
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO quota_buckets(
                  quota_bucket_id,tenant_id,plan_id,quota_type,granted_micros,
                  consumed_micros,reserved_micros,status,expires_at,state_revision,
                  created_at,updated_at
                ) VALUES(?,?,?,'paid',1000000000000001,0,0,'active',NULL,1,?,?)
                """,
                (f"qbk_{uuid4().hex}", plan["tenant_id"], plan["plan_id"], NOW, NOW),
            )
        connection.rollback()
        _assert_healthy(connection)


def _insert_custom_grant(connection: sqlite3.Connection) -> None:
    """写入自定义财务角色授权，用于验证降级不会静默丢失新权限。"""

    prd077._customize_role_matrix(connection, "finance_admin", "response_cache.read")


def _insert_webhook_subscription(connection: sqlite3.Connection) -> None:
    """写入订阅 PRD-079 事件的端点，用于验证降级订阅守卫。"""

    owner = connection.execute(
        """
        SELECT tenant_id,user_id FROM users
        WHERE role='admin' AND status='active' ORDER BY tenant_id LIMIT 1
        """
    ).fetchone()
    assert owner is not None
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'https://example.invalid/prd079','["quota.exhausted"]',
                 'synthetic-ciphertext','whsec_test','prd079-test-fingerprint',
                 'disabled',1000,3,?,?,?,?)
        """,
        (
            f"wh_prd079_{uuid4().hex}",
            owner["tenant_id"],
            "PRD-079 migration guard",
            owner["user_id"],
            owner["user_id"],
            NOW,
            NOW,
        ),
    )


@pytest.mark.parametrize("fact_kind", ("plan", "custom_grant", "subscription"))
def test_prd079_down_business_guards_are_atomic(
    prd079_migration_templates: dict[str, Path],
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """验证业务、授权或订阅存在时降级在首个 DDL 前原子失败。"""

    database_url = _clone(
        prd079_migration_templates,
        tmp_path / f"down-guard-{fact_kind}.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd079_migration_templates["migrations_0072"],
    ).applied == ["0072"]
    with connect_database(database_url) as connection:
        {
            "plan": _insert_plan,
            "custom_grant": _insert_custom_grant,
            "subscription": _insert_webhook_subscription,
        }[fact_kind](connection)
        connection.commit()
        schema_before = _schema_snapshot(connection)
        rbac_before = prd077._rbac_snapshot(connection)

    with pytest.raises(DbError) as caught:
        rollback_last_migration(
            database_url,
            migrations_dir=prd079_migration_templates["migrations_0072"],
            expected_version="0072",
        )
    assert caught.value.code == "db.migration_failed"
    assert "prd079_down_guard_failed" in caught.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0072"
        assert _schema_snapshot(connection) == schema_before
        assert prd077._rbac_snapshot(connection) == rbac_before
        _assert_healthy(connection)
