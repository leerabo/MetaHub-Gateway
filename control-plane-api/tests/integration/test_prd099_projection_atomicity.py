"""Canonical usage time and per-fact rollback on fully migrated databases."""

import shutil
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
import test_prd099_platform_billing as contracts
from httpx import ASGITransport, AsyncClient
from metahub_control.platform_billing import service
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError
from test_prd137_http_probes import migrated_baseline  # noqa: F401

ANCHOR = datetime(2026, 8, 25, tzinfo=UTC)


@pytest.fixture
def projection_database(migrated_baseline, tmp_path):  # noqa: F811
    database = tmp_path / "projection.sqlite3"
    shutil.copy2(migrated_baseline, database)
    return f"sqlite:///{database.as_posix()}"


@pytest.fixture
async def projection_subject(projection_database, monkeypatch, tmp_path):
    url, app = contracts._prepare(monkeypatch, tmp_path, database_url=projection_database)
    clock = [ANCHOR]
    monkeypatch.setattr(contracts.saas_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as platform:
        platform.cookies.set(contracts.PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(contracts.PLATFORM_CSRF_COOKIE, "c" * 43)
        version = await contracts._create_plan(platform)
    async with AsyncClient(transport=transport, base_url="https://testserver") as workspace:
        workspace_id = await contracts._enter_workspace(workspace)
        created = await workspace.post(
            "/subscriptions", json={"plan_version_id": version, "start_trial": True},
            headers=contracts._csrf(workspace, contracts.WORKSPACE_CSRF_COOKIE,
                                   **{"Idempotency-Key": "prd099-atomic-subscription"}),
        )
        assert created.status_code == 201, created.json().get("code")
        with closing(connect_database(url)) as connection:
            tenant_id = connection.execute(
                "SELECT tenant_id FROM workspace_tenant_bindings WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0]
        yield app.state.config, tenant_id, clock


def _usage(config, tenant_id, occurred_at, *, enqueued_at):
    usage_id = contracts._insert_usage(config.database_url, tenant_id, occurred_at)
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            "UPDATE platform_meter_outbox SET created_at=?,updated_at=? "
            "WHERE source_usage_ledger_id=?",
            (service._db_time(enqueued_at), service._db_time(enqueued_at), usage_id),
        )
        connection.commit()
    return usage_id


def _facts(config):
    with closing(connect_database(config.database_url)) as connection:
        tables = ("platform_billing_cycles", "platform_quota_allocations",
                  "platform_meter_readings", "platform_billing_summaries",
                  "platform_billing_adjustments", "platform_meter_checkpoints")
        return {table: sorted(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
                for table in tables}


def _assert_failed(config, usage_id, code):
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            "SELECT status,error_code,attempt_count,lease_owner_id,lease_token,lease_expires_at "
            "FROM platform_meter_outbox WHERE source_usage_ledger_id=?", (usage_id,),
        ).fetchone()
        assert tuple(row) == ("failed", code, 1, None, None, None)
    with pytest.raises(ApiError) as error:
        service.project_usage_once(config, worker_id="blocked-worker")
    assert error.value.code == "platform_metering.reconciliation_required"


@pytest.mark.anyio
async def test_projection_uses_usage_time_after_trial_ends(projection_subject):
    config, tenant_id, clock = projection_subject
    occurred_at = ANCHOR + timedelta(days=1)
    clock[0] = ANCHOR + timedelta(days=16)
    usage_id = _usage(config, tenant_id, occurred_at, enqueued_at=clock[0])
    assert service.project_usage_once(config, worker_id="delayed-worker")
    assert not service.project_usage_once(config, worker_id="replay-worker")
    with closing(connect_database(config.database_url)) as connection:
        readings = connection.execute(
            "SELECT occurred_at FROM platform_meter_readings WHERE source_fact_id=?", (usage_id,),
        ).fetchall()
        assert len(readings) == 4
        assert {row[0] for row in readings} == {service._db_time(occurred_at)}
        cycle = connection.execute("SELECT * FROM platform_billing_cycles").fetchone()
        assert cycle["period_started_at"] == service._db_time(ANCHOR)
        assert cycle["period_ends_at"] == service._db_time(ANCHOR + timedelta(days=14))


@pytest.mark.anyio
@pytest.mark.parametrize("offset", [-1, 14])
async def test_projection_does_not_borrow_entitlement_from_enqueue_time(projection_subject, offset):
    config, tenant_id, clock = projection_subject
    before = _facts(config)
    usage_id = _usage(config, tenant_id, ANCHOR + timedelta(days=offset),
                      enqueued_at=ANCHOR + timedelta(days=1))
    with pytest.raises(ApiError) as error:
        service.project_usage_once(config, worker_id="boundary-worker")
    assert error.value.code == "platform_metering.owner_invalid"
    assert _facts(config) == before
    _assert_failed(config, usage_id, "platform_metering.owner_invalid")


@pytest.mark.anyio
async def test_projection_failure_rolls_back_new_cycle_and_allocations(
    projection_subject, monkeypatch,
):
    config, tenant_id, clock = projection_subject
    before = _facts(config)
    usage_id = _usage(config, tenant_id, ANCHOR + timedelta(days=1), enqueued_at=ANCHOR)
    original = service._ensure_cycle

    def fail_after_cycle(*args, **kwargs):
        original(*args, **kwargs)
        raise ApiError("platform_metering.meter_unknown")

    monkeypatch.setattr(service, "_ensure_cycle", fail_after_cycle)
    with pytest.raises(ApiError) as error:
        service.project_usage_once(config, worker_id="cycle-fault-worker")
    assert error.value.code == "platform_metering.meter_unknown"
    assert _facts(config) == before
    _assert_failed(config, usage_id, "platform_metering.meter_unknown")


@pytest.mark.anyio
@pytest.mark.parametrize("database_error", [False, True], ids=["business-error", "database-error"])
async def test_late_projection_failure_keeps_frozen_facts(projection_subject, monkeypatch,
                                                         database_error):
    config, tenant_id, clock = projection_subject
    _usage(config, tenant_id, ANCHOR + timedelta(days=1), enqueued_at=ANCHOR)
    assert service.project_usage_once(config, worker_id="first-worker")
    clock[0] = ANCHOR + timedelta(days=15)
    assert service.close_due_cycles(config) == 1
    before = _facts(config)
    usage_id = _usage(config, tenant_id, ANCHOR + timedelta(days=2), enqueued_at=ANCHOR)
    original = service._late_adjustment
    calls = []

    def fail_after_second_adjustment(*args, **kwargs):
        original(*args, **kwargs)
        calls.append(kwargs["reading_id"])
        if len(calls) == 2:
            if database_error:
                args[0].execute("SELECT prd099_missing_column FROM platform_billing_cycles")
            raise ApiError("platform_billing.reconciliation_required")

    with monkeypatch.context() as patch:
        patch.setattr(service, "_late_adjustment", fail_after_second_adjustment)
        with pytest.raises(ApiError) as error:
            service.project_usage_once(config, worker_id="late-fault-worker")
    assert len(calls) == 2
    assert error.value.code == ("platform_metering.unavailable" if database_error
                                else "platform_billing.reconciliation_required")
    assert _facts(config) == before
    if not database_error:
        _assert_failed(config, usage_id, "platform_billing.reconciliation_required")
        return
    with closing(connect_database(config.database_url)) as connection:
        assert tuple(connection.execute(
            "SELECT status,attempt_count FROM platform_meter_outbox WHERE source_usage_ledger_id=?",
            (usage_id,),
        ).fetchone()) == ("pending", 0)
    assert service.project_usage_once(config, worker_id="database-retry-worker")
    assert not service.project_usage_once(config, worker_id="database-replay-worker")
    after = _facts(config)
    assert after["platform_billing_summaries"] == before["platform_billing_summaries"]
    assert len(after["platform_meter_readings"]) == 8
    assert len(after["platform_billing_adjustments"]) == 4
