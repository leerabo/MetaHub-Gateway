"""Real subscription lifecycle and native order identity migration contracts."""

import sqlite3
from contextlib import closing
from uuid import uuid4

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd098_saas_billing as contracts
from httpx import ASGITransport, AsyncClient
from metahub_shared.db import DbError, apply_migrations, connect_database, rollback_last_migration
from test_prd043_postgresql_usage_sequence import _rollback_after

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


def _before_identity(database):
    later = _rollback_after(database, "0150")
    assert rollback_last_migration(database, expected_version="0150").rolled_back == "0150"
    return later


def _orders(database):
    with closing(connect_database(database)) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM subscription_order_records ORDER BY created_at,order_record_id"
            )
        ]


@pytest.mark.anyio
async def test_native_subscription_create_replay_same_time_change_and_expiry(
    native_business_database,
    monkeypatch,
    tmp_path,
    caplog,
):
    original = contracts._prepare
    monkeypatch.setattr(
        contracts,
        "_prepare",
        lambda patch, path: original(
            patch,
            path,
            database_url=native_business_database,
        ),
    )
    await contracts.test_catalog_subscription_idempotency_and_plan_change(
        monkeypatch, tmp_path, caplog
    )
    orders = _orders(native_business_database)
    assert len(orders) == 3
    assert len({row["rowid"] for row in orders}) == 3
    same_time = sorted(orders[:2], key=lambda row: row["rowid"])
    assert same_time[0]["created_at"] == same_time[1]["created_at"]
    assert [row["order_kind"] for row in same_time] == ["create", "upgrade"]
    with closing(connect_database(native_business_database)) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE subscription_order_records SET rowid=rowid+100")
        connection.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "INSERT INTO subscription_order_records "
                "SELECT * FROM subscription_order_records LIMIT 1"
            )
        connection.rollback()
    _rollback_after(native_business_database, "0150")
    with pytest.raises(DbError):
        rollback_last_migration(native_business_database, expected_version="0150")
    assert _orders(native_business_database) == orders


@pytest.fixture
async def historical_orders(native_business_database, monkeypatch, tmp_path):
    database, app = contracts._prepare(monkeypatch, tmp_path, database_url=native_business_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as platform:
        platform.cookies.set(contracts.PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(contracts.PLATFORM_CSRF_COOKIE, "c" * 43)
        version = await contracts._create_plan(platform, code="free", price="0.00")
    later = _before_identity(database)
    # These are legacy migration inputs, not substitutes for the lifecycle test above.
    with closing(connect_database(database)) as connection:
        owner = connection.execute(
            "SELECT binding.workspace_id,binding.tenant_id,member.workspace_membership_id "
            "FROM workspace_tenant_bindings binding JOIN workspace_memberships member "
            "ON member.workspace_id=binding.workspace_id "
            "ORDER BY member.workspace_membership_id LIMIT 1"
        ).fetchone()
        subscription = "sub_" + uuid4().hex
        connection.execute(
            "INSERT INTO workspace_subscriptions(subscription_id,workspace_id,tenant_id,"
            "plan_version_id,status,current_period_started_at,state_revision,"
            "created_by_membership_id,created_at,updated_at) "
            "VALUES(?,?,?,?,'active','2026-09-01T00:00:00Z',1,?,'2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')",
            (subscription, owner[0], owner[1], version, owner[2]),
        )
        for index in range(2):
            connection.execute(
                "INSERT INTO subscription_order_records(order_record_id,workspace_id,"
                "tenant_id,subscription_id,"
                "plan_version_id,order_kind,status,currency,amount_minor,idempotency_sha256,request_sha256,"
                "created_by_membership_id,created_at) "
                "VALUES(?,?,?,?,?,'create','not_required','CNY',0,?,?,?,?)",
                (
                    "sor_" + uuid4().hex,
                    owner[0],
                    owner[1],
                    subscription,
                    version,
                    str(index) * 64,
                    "2" * 64,
                    owner[2],
                    f"2026-09-0{index + 1}T00:00:00Z",
                ),
            )
        connection.commit()
    return database, later


@pytest.mark.anyio
async def test_native_order_migration_preserves_all_historical_fields(historical_orders):
    database, later = historical_orders
    before = _orders(database)
    assert apply_migrations(database).applied == ["0150", *reversed(later)]
    after = _orders(database)
    assert [{key: value for key, value in row.items() if key != "rowid"} for row in after] == before
    assert len({row["rowid"] for row in after}) == 2


@pytest.mark.anyio
async def test_native_order_migration_rejects_ambiguous_history(historical_orders):
    database, _ = historical_orders
    with closing(connect_database(database)) as connection:
        connection.execute(
            "UPDATE subscription_order_records SET created_at='2026-09-01T00:00:00Z'"
        )
        connection.commit()
    before = _orders(database)
    with pytest.raises(DbError):
        apply_migrations(database)
    assert _orders(database) == before


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("drift", ["constraint", "manifest"])
def test_native_order_migration_rejects_drift(native_business_database, direction, drift):
    database = native_business_database
    _rollback_after(database, "0150")
    if direction == "up":
        assert rollback_last_migration(database, expected_version="0150").rolled_back == "0150"
    with closing(connect_database(database)) as connection:
        if drift == "constraint":
            connection.execute(
                "ALTER TABLE subscription_order_records ADD CONSTRAINT "
                "unexpected_order_constraint CHECK(amount_minor=0) NOT VALID"
            )
        else:
            connection.execute(
                "ALTER TABLE tenant_deletion_table_manifest DISABLE TRIGGER "
                "trg_tenant_deletion_table_manifest_update_prd089"
            )
            connection.execute(
                "UPDATE tenant_deletion_table_manifest SET column_signature_sha256=? "
                "WHERE source_table='subscription_order_records'",
                ("0" * 64,),
            )
            connection.execute(
                "ALTER TABLE tenant_deletion_table_manifest ENABLE TRIGGER "
                "trg_tenant_deletion_table_manifest_update_prd089"
            )
        connection.commit()
    with pytest.raises(DbError):
        if direction == "up":
            apply_migrations(database)
        else:
            rollback_last_migration(database, expected_version="0150")
    assert _orders(database) == []
