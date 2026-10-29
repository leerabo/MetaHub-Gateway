"""Platform-only recovery preserves Usage and requires exact, audited source intent."""

from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sqlite3
from time import perf_counter
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from metahub_control.platform_billing import recovery
from metahub_control.platform_billing import service as billing
from metahub_control.platform_billing.models import MeterRecoveryRequest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.connection import (
    _close_idle_database_connections,
    close_database_pools,
    database_request_scope,
)
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.errors import ApiError
import test_prd099_platform_billing as original


@pytest.fixture(scope="module")
def recovery_baseline(tmp_path_factory):
    target = tmp_path_factory.mktemp("meter-schema") / "base.sqlite3"
    source = os.environ.get("METAHUB_TEST_METER_BASELINE")
    if source:
        source = Path(source).resolve(strict=True)
        assert sha256(source.read_bytes()).hexdigest() == (
            "062ae8e3a8024030efb712a5120793483203b86d552fcfd86a46e9bf804f4c2a"
        )
        shutil.copy2(source, target)
    apply_migrations("sqlite:///" + target.as_posix())
    yield target
    target.unlink()


@pytest.fixture
def subject(monkeypatch, tmp_path, recovery_baseline):
    target = tmp_path / "meter-recovery.sqlite3"
    shutil.copy2(recovery_baseline, target)
    database, app = original._prepare(monkeypatch, tmp_path, database_url="sqlite:///" + target.as_posix())
    with closing(connect_database(database)) as db:
        owner = db.execute("SELECT workspace_id,tenant_id FROM workspace_tenant_bindings LIMIT 1").fetchone()
        assert db.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 0
    yield database, app, owner["workspace_id"], owner["tenant_id"]
    close_database_pools()
    _close_idle_database_connections()
    for suffix in ("", "-wal", "-shm"):
        target.with_name(target.name + suffix).unlink(missing_ok=True)


@contextmanager
def api_error(code):
    with pytest.raises(ApiError) as caught:
        yield
    assert caught.value.code.endswith(code)


def state(app):
    return recovery.get_metering_state(app.state.config, session_token="test-session",
                                      request_id="req_meter_state", trace_id="trc_meter_state")


def request_for(app, action="internal_cost_only"):
    fact = state(app).blocked_fact
    assert fact is not None
    return MeterRecoveryRequest(meter_outbox_id=fact.meter_outbox_id,
        source_sequence=fact.source_sequence, expected_attempt_count=fact.attempt_count,
        expected_source_sha256=fact.source_sha256, action=action, confirmed=True)


def recover(app, payload, key="meter-recovery-test-0001"):
    return recovery.recover_meter_fact(app.state.config, session_token="test-session",
        csrf_token="test-csrf", payload=payload, idempotency_key=key,
        request_id="req_meter_recovery", trace_id="trc_meter_recovery")


def failed_head(subject, occurred_at=None):
    database, app, _workspace_id, tenant_id = subject
    usage_id = original._insert_usage(database, tenant_id, occurred_at or datetime(2020, 1, 1, tzinfo=UTC))
    with api_error("platform_metering.owner_invalid"):
        billing.project_usage_once(app.state.config, worker_id="meter-recovery-test")
    return usage_id


def counts(database):
    with closing(connect_database(database)) as db:
        return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in (
            "usage_ledger", "platform_meter_readings", "platform_billing_cycles",
            "platform_meter_recovery_actions", "platform_audit_logs",
        )} | {"checkpoint": db.execute("SELECT source_sequence FROM platform_meter_checkpoints "
                                      "WHERE projection_code='usage_ledger_v1'").fetchone()[0]}


def rollback_after(database, target_version):
    """Expose a historical migration only after current later migrations roll back."""
    with closing(connect_database(database)) as db:
        original_versions = [row[0] for row in db.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version"
        )]
    assert target_version in original_versions
    rolled_back = []
    while True:
        with closing(connect_database(database)) as db:
            head = db.execute(
                "SELECT version FROM schema_migrations WHERE status='applied' "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()[0]
        if head == target_version:
            return original_versions, rolled_back
        rolled_back.append(rollback_last_migration(database).rolled_back)


def test_archive_preserves_source_and_advances_exactly_one_with_replay(subject):
    database, app, workspace_id, tenant_id = subject
    usage_id = failed_head(subject)
    original._insert_usage(database, tenant_id, datetime(2020, 1, 2, tzinfo=UTC))
    before = counts(database)
    payload = request_for(app)
    response = recover(app, payload)
    assert response.recovery.workspace_id == workspace_id
    assert not response.idempotent_replay
    assert recover(app, payload).idempotent_replay
    after = counts(database)
    assert after == before | {"checkpoint": 1,
        "platform_meter_recovery_actions": before["platform_meter_recovery_actions"] + 1,
        "platform_audit_logs": before["platform_audit_logs"] + 1}
    assert state(app).state == "pending"
    assert state(app).pending_count == 1
    with api_error("idempotency_conflict"):
        recover(app, payload.model_copy(update={"action": "retry"}))
    with api_error("revision_conflict"):
        recover(app, payload, "meter-recovery-test-other")
    with closing(connect_database(database)) as db:
        outbox = db.execute("SELECT status,error_code,attempt_count FROM platform_meter_outbox "
                            "WHERE source_usage_ledger_id=?", (usage_id,)).fetchone()
        assert tuple(outbox) == ("failed", "platform_metering.owner_invalid", 1)
        for sql in ("UPDATE platform_meter_recovery_actions SET action='retry'",
                    "DELETE FROM platform_meter_recovery_actions"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
            db.rollback()
    rollback_after(database, "0149")
    with pytest.raises(DbError) as caught:
        rollback_last_migration(database, expected_version="0149")
    assert caught.value.code == "db.migration_failed"
    assert "CHECK constraint failed" in caught.value.message
    assert counts(database) == after


def test_retry_preserves_attempt_and_old_attempt_cannot_archive_new_failure(subject):
    database, app, *_ = subject
    failed_head(subject)
    first = request_for(app, "retry")
    recover(app, first)
    assert state(app).state == "pending"
    assert counts(database)["checkpoint"] == 0
    with api_error("owner_invalid"):
        billing.project_usage_once(app.state.config, worker_id="meter-retry")
    assert state(app).blocked_fact.attempt_count == 2
    with api_error("revision_conflict"):
        recover(app, first.model_copy(update={"action": "internal_cost_only"}), "meter-recovery-stale-0001")
    assert recover(app, first).idempotent_replay
    recover(app, request_for(app), "meter-recovery-fresh-0001")
    assert state(app).state == "idle"
    assert counts(database)["platform_meter_recovery_actions"] == 2


@pytest.mark.parametrize("failure", ["audit", "reauthorization"])
def test_recovery_rolls_back_every_write_on_audit_or_final_permission_failure(subject, monkeypatch, failure):
    database, app, *_ = subject
    failed_head(subject)
    payload = request_for(app)
    before = counts(database)
    if failure == "audit":
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("injected audit failure")
        monkeypatch.setattr(recovery, "_record_recovery_audit", unavailable)
        expected = "unavailable"
    else:
        calls = 0
        auth = billing.authenticate_platform_session_in_transaction
        def revoked(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise ApiError("platform.permission_denied")
            return auth(*args, **kwargs)
        monkeypatch.setattr(billing, "authenticate_platform_session_in_transaction", revoked)
        expected = "permission_denied"
    with api_error(expected):
        recover(app, payload)
    assert counts(database) == before


def test_read_reauthorizes_after_snapshot_and_replay_reauthorizes(subject, monkeypatch):
    database, app, *_ = subject
    failed_head(subject)
    payload = request_for(app)
    recover(app, payload)
    auth = billing.authenticate_platform_session_in_transaction
    calls = []
    def revoked(connection, *args, **kwargs):
        calls.append(connection.in_transaction)
        if len(calls) % 2 == 0:
            raise ApiError("platform.permission_denied")
        return auth(connection, *args, **kwargs)
    monkeypatch.setattr(billing, "authenticate_platform_session_in_transaction", revoked)
    before = counts(database)
    with api_error("permission_denied"):
        state(app)
    with api_error("permission_denied"):
        recover(app, payload)
    assert calls == [True] * 4
    assert counts(database) == before


def test_existing_caller_transaction_is_not_rolled_back_or_closed(subject, monkeypatch):
    database, app, *_ = subject
    failed_head(subject)
    payload = request_for(app)
    with closing(connect_database(database)) as db:
        db.execute("BEGIN")
        monkeypatch.setattr(billing, "_connect", lambda config: db)
        for operation in (lambda: state(app), lambda: recover(app, payload)):
            with api_error("unavailable"):
                operation()
            assert db.in_transaction
            assert db.execute("SELECT 1").fetchone()[0] == 1
        db.rollback()


@pytest.mark.parametrize("confirmation", [False, 1, 0, "true", None])
def test_confirmation_requires_actual_boolean_true(confirmation):
    with pytest.raises(ValidationError):
        MeterRecoveryRequest(meter_outbox_id="pmo_" + "1" * 32, source_sequence=1,
            expected_attempt_count=1, expected_source_sha256="a" * 64,
            action="retry", confirmed=confirmation)


@pytest.mark.anyio
async def test_meter_routes_enforce_platform_origin_csrf_query_body_and_permissions(subject, monkeypatch):
    database, app, *_ = subject
    failed_head(subject)
    before = counts(database)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        assert (await client.get("/platform/billing/metering-state")).status_code == 401
        client.cookies.set(original.PLATFORM_SESSION_COOKIE, "p" * 43)
        client.cookies.set(original.PLATFORM_CSRF_COOKIE, "c" * 43)
        response = await client.get("/platform/billing/metering-state")
        assert response.status_code == 200, response.text
        assert "no-store" in response.headers["cache-control"]
        assert (await client.get("/platform/billing/metering-state?tenant_id=foreign")).status_code == 422
        assert (await client.get("/platform/billing/metering-state", headers={"Authorization": "Bearer test"})).status_code == 400
        payload = request_for(app).model_dump()
        path = "/platform/billing/meter-recoveries"
        headers = original._csrf(client, original.PLATFORM_CSRF_COOKIE,
                                **{"Idempotency-Key": "meter-route-test-0001"})
        for changed in ({"confirmed": 1}, {"prompt": "private"}, {"workspace_id": "foreign"}):
            invalid = await client.post(path, json=payload | changed, headers=headers)
            assert invalid.status_code == 422, invalid.text
        assert (await client.post(path, json=payload)).status_code == 403
        assert (await client.post(path, json=payload, headers=headers | {"Origin": "https://foreign.test"})).status_code == 403
        assert (await client.post(path, json=payload, headers=headers | {"X-CSRF-Token": "wrong"})).status_code == 403
        assert counts(database) == before
        auth = billing.authenticate_platform_session_in_transaction
        monkeypatch.setattr(billing, "authenticate_platform_session_in_transaction",
            lambda *args, **kwargs: replace(auth(*args, **kwargs), permissions=("platform.billing.read",)))
        denied = await client.post(path, json=payload, headers=headers)
        assert denied.status_code == 403, denied.text
        monkeypatch.setattr(billing, "authenticate_platform_session_in_transaction", auth)
        archived = await client.post(path, json=payload, headers=headers)
        assert archived.status_code == 201, archived.text
        replay = await client.post(path, json=payload, headers=headers)
        assert replay.status_code == 200 and replay.json()["idempotent_replay"]


@pytest.mark.anyio
async def test_subscription_era_gap_cannot_be_archived_even_by_direct_insert(subject, monkeypatch):
    database, app, _workspace_id, tenant_id = subject
    clock = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(original.saas_service, "_utcnow", lambda: clock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as platform:
        platform.cookies.set(original.PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(original.PLATFORM_CSRF_COOKIE, "c" * 43)
        plan = await original._create_plan(platform)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as workspace:
        await original._enter_workspace(workspace)
        subscribed = await workspace.post("/subscriptions", json={"plan_version_id": plan, "start_trial": True},
            headers=original._csrf(workspace, original.WORKSPACE_CSRF_COOKIE,
                                    **{"Idempotency-Key": "meter-subscription-test-0001"}))
        assert subscribed.status_code == 201, subscribed.text
    failed_head(subject, clock + timedelta(days=60))
    assert not state(app).blocked_fact.can_archive_internal_cost
    payload = request_for(app)
    with api_error("reconciliation_required"):
        recover(app, payload)
    # Bypass the service deliberately: schema must reject contract-era classification too.
    with closing(connect_database(database)) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO platform_meter_recovery_actions(
              recovery_id,meter_outbox_id,source_fact_id,source_sequence,tenant_id,workspace_id,
              operator_id,action,source_attempt_count,source_sha256,idempotency_sha256,
              request_sha256,request_id,trace_id,created_at)
              SELECT ?,o.meter_outbox_id,o.source_usage_ledger_id,s.usage_export_sequence,?,b.workspace_id,
                ?,'internal_cost_only',o.attempt_count,?,?,?,'req_raw_insert','trc_raw_insert',?
              FROM platform_meter_outbox o JOIN usage_export_ledger_sequences s
                ON s.usage_ledger_id=o.source_usage_ledger_id
              JOIN workspace_tenant_bindings b ON b.tenant_id=?""",
              ("pmx_" + uuid4().hex, tenant_id, "pto_" + "8" * 32, "a" * 64,
               "b" * 64, "c" * 64, clock.isoformat(), tenant_id))
        db.rollback()
    assert counts(database)["checkpoint"] == 0


def test_migration_pristine_down_and_up(subject):
    database, *_ = subject
    original_versions, rolled_back = rollback_after(database, "0149")
    assert rolled_back == list(reversed([version for version in original_versions if version > "0149"]))
    assert rollback_last_migration(database, expected_version="0149").rolled_back == "0149"
    with closing(connect_database(database)) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='platform_meter_recovery_actions'").fetchone() is None
        assert db.execute("SELECT COUNT(*) FROM tenant_deletion_table_manifest "
                          "WHERE source_table='platform_meter_recovery_actions'").fetchone()[0] == 0
    result = apply_migrations(database)
    assert result.applied == [version for version in original_versions if version >= "0149"]
    assert counts(database)["platform_meter_recovery_actions"] == 0


def test_metering_reads_are_bounded_and_preserve_all_facts(subject, record_property):
    database, app, *_ = subject
    failed_head(subject)
    before = counts(database)
    durations = []
    for _ in range(20):
        started = perf_counter()
        # Match the production Control GET middleware's request-owned warm handle.
        with database_request_scope(reuse_connections=True):
            result = state(app)
        durations.append((perf_counter() - started) * 1000)
        assert result.state == "blocked"
        assert len(result.recent_recoveries) <= 10
    p95 = sorted(durations)[18]
    record_property("metering_read_p95_ms", round(p95, 3))
    assert p95 < 300
    assert counts(database) == before


def test_original_metering_cycle_and_adjustment_flow_still_passes(subject, monkeypatch, tmp_path):
    import anyio

    database, app, *_ = subject
    monkeypatch.setattr(original, "_prepare", lambda *args, **kwargs: (database, app))
    anyio.run(original.test_meter_projection_cycle_close_late_usage_and_adjustment, monkeypatch, tmp_path)
