"""Meter recovery through actual MFA, role changes and revocable platform sessions."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.platform_billing.routes import router as billing_router
from metahub_shared.db import connect_database
from metahub_shared.db.connection import (
    _close_idle_database_connections,
    close_database_pools,
)

import test_prd095_platform_iam as iam
import test_platform_meter_recovery as meter
from test_platform_meter_recovery import recovery_baseline as recovery_baseline


STATE = "/platform/billing/metering-state"
RECOVER = "/platform/billing/meter-recoveries"


@pytest.fixture
def real_identity_subject(monkeypatch, tmp_path, recovery_baseline):
    database, app = iam._prepare(monkeypatch, tmp_path, recovery_baseline)
    app.include_router(billing_router)
    with closing(connect_database(database)) as db:
        binding = db.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings LIMIT 1"
        ).fetchone()
        assert db.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 0
    try:
        yield database, app, binding["workspace_id"], binding["tenant_id"]
    finally:
        close_database_pools()
        _close_idle_database_connections()
        target = tmp_path / "platform.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)


async def _exchange(client, secret, clock):
    clock[0] += timedelta(seconds=30)
    response = await client.post(
        "/platform/sessions", json={"totp_code": iam._totp(secret, clock[0])},
        headers=iam._csrf(client, iam.GLOBAL_CSRF_COOKIE),
    )
    assert response.status_code == 200, response.text


async def _payload(client, action):
    response = await client.get(STATE)
    assert response.status_code == 200, response.text
    fact = response.json()["blocked_fact"]
    assert fact is not None
    return {
        "meter_outbox_id": fact["meter_outbox_id"],
        "source_sequence": fact["source_sequence"],
        "expected_attempt_count": fact["attempt_count"],
        "expected_source_sha256": fact["source_sha256"],
        "action": action,
        "confirmed": True,
    }


def _headers(client, key):
    return iam._csrf(client, iam.PLATFORM_CSRF_COOKIE) | {"Idempotency-Key": key}


@pytest.mark.anyio
async def test_real_mfa_meter_recovery_roles_replay_and_revocation(
    real_identity_subject, monkeypatch, record_property,
):
    database, app, workspace_id, _tenant_id = real_identity_subject
    meter.failed_head(real_identity_subject)
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(iam.identity_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(iam.platform_service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="https://testserver") as owner,
        AsyncClient(transport=transport, base_url="https://testserver") as staff,
        AsyncClient(transport=transport, base_url="https://testserver") as workspace,
        AsyncClient(transport=transport, base_url="https://testserver") as support,
    ):
        assert (await owner.get(STATE)).status_code == 401
        await iam._login(owner, iam.ADMIN_EMAIL, iam.ADMIN_PASSWORD)
        assert (await owner.get(STATE)).status_code == 401
        await iam._login(workspace, iam.ADMIN_EMAIL, iam.ADMIN_PASSWORD)
        switched = await workspace.post(
            "/identity/workspace-switches", json={"workspace_id": workspace_id},
            headers=iam._csrf(workspace, iam.GLOBAL_CSRF_COOKIE),
        )
        assert switched.status_code == 200, switched.text
        denied = await workspace.get(STATE)
        assert denied.status_code == 400
        assert denied.json()["code"] == "platform.credential_conflict"

        owner_secret = await iam._enroll(owner, iam.ADMIN_PASSWORD, clock[0])
        clock[0] += timedelta(seconds=30)
        bootstrap = await owner.post(
            "/platform/bootstrap",
            json={
                "current_password": iam.ADMIN_PASSWORD,
                "totp_code": iam._totp(owner_secret, clock[0]),
                "recovery_secret": iam.RECOVERY_SECRET,
                "reason_code": "initial_platform_owner", "confirmed": True,
            },
            headers=iam._csrf(owner, iam.GLOBAL_CSRF_COOKIE),
        )
        assert bootstrap.status_code == 200, bootstrap.text
        replay = await owner.post(
            "/platform/sessions", json={"totp_code": iam._totp(owner_secret, clock[0])},
            headers=iam._csrf(owner, iam.GLOBAL_CSRF_COOKIE),
        )
        assert replay.status_code == 400
        assert replay.json()["code"] == "identity.mfa_verification_failed"
        await _exchange(owner, owner_secret, clock)

        await iam._register(staff)
        staff_secret = await iam._enroll(staff, iam.SECOND_PASSWORD, clock[0])
        with closing(connect_database(database)) as db:
            account_id = db.execute(
                "SELECT account_id FROM global_accounts WHERE email_normalized=?",
                (iam.SECOND_EMAIL,),
            ).fetchone()[0]
        created = await owner.post(
            "/platform/operators",
            json={"account_id": account_id, "roles": ["platform_security"]},
            headers=iam._csrf(owner, iam.PLATFORM_CSRF_COOKIE),
        )
        assert created.status_code == 201, created.text
        operator_id = created.json()["operator"]["operator_id"]
        await _exchange(staff, staff_secret, clock)
        payload = await _payload(owner, "retry")
        before = meter.counts(database)
        for response in (
            await staff.get(STATE),
            await staff.post(RECOVER, json=payload, headers=_headers(staff, "real-denied-0001")),
        ):
            assert response.status_code == 403, response.text
            assert response.json()["code"] == "platform.permission_denied"
        assert meter.counts(database) == before

        grant = await owner.post(
            "/platform/support-grants",
            json={"workspace_id": workspace_id, "reason_code": "workspace_diagnostics",
                  "duration_minutes": 20},
            headers=iam._csrf(owner, iam.PLATFORM_CSRF_COOKIE),
        )
        assert grant.status_code == 201, grant.text
        grant_id = grant.json()["grant"]["grant_id"]
        for client, cookie, path in (
            (staff, iam.PLATFORM_CSRF_COOKIE, f"/platform/support-grants/{grant_id}/approve"),
            (workspace, iam.WORKSPACE_CSRF_COOKIE, f"/workspace/support-grants/{grant_id}/approve"),
        ):
            approved = await client.post(path, json={}, headers=iam._csrf(client, cookie))
            assert approved.status_code == 200, approved.text
        issued = await owner.post(
            f"/platform/support-grants/{grant_id}/sessions", json={},
            headers=iam._csrf(owner, iam.PLATFORM_CSRF_COOKIE),
        )
        assert issued.status_code == 200, issued.text
        support.cookies.set(iam.SUPPORT_SESSION_COOKIE, owner.cookies.get(iam.SUPPORT_SESSION_COOKIE))
        owner.cookies.delete(iam.SUPPORT_SESSION_COOKIE)
        assert (await support.get("/support/session")).status_code == 200
        for response in (
            await support.get(STATE),
            await support.post(RECOVER, json=payload, headers=iam._headers()),
        ):
            assert response.status_code == 400
            assert response.json()["code"] == "platform.credential_conflict"

        changed = await owner.patch(
            f"/platform/operators/{operator_id}",
            json={"status": "active", "roles": ["platform_billing"]},
            headers=iam._csrf(owner, iam.PLATFORM_CSRF_COOKIE) | {"If-Match": '"1"'},
        )
        assert changed.status_code == 200, changed.text
        assert (await staff.get(STATE)).status_code == 401
        await _exchange(staff, staff_secret, clock)
        durations = []
        for _ in range(20):
            started = perf_counter()
            response = await staff.get(STATE)
            durations.append((perf_counter() - started) * 1000)
            assert response.status_code == 200, response.text
            assert "no-store" in response.headers["cache-control"]
        p95 = iam._p95(durations)
        record_property("real_mfa_asgi_read_p95_ms", round(p95, 3))

        headers = _headers(staff, "real-meter-retry-0001")
        before = meter.counts(database)
        for bad_headers in (
            {}, headers | {"Origin": "https://foreign.test"},
            headers | {"X-CSRF-Token": "invalid"},
        ):
            denied = await staff.post(RECOVER, json=payload, headers=bad_headers)
            assert denied.status_code == 403, denied.text
        assert meter.counts(database) == before
        retry = await staff.post(RECOVER, json=payload, headers=headers)
        assert retry.status_code == 201, retry.text
        replay = await staff.post(RECOVER, json=payload, headers=headers)
        assert replay.status_code == 200 and replay.json()["idempotent_replay"]
        assert meter.counts(database)["checkpoint"] == 0
        with meter.api_error("owner_invalid"):
            meter.billing.project_usage_once(app.state.config, worker_id="real-meter-retry")
        stale = await staff.post(
            RECOVER, json=payload | {"action": "internal_cost_only"},
            headers=_headers(staff, "real-meter-stale-0001"),
        )
        assert stale.status_code == 412, stale.text
        assert stale.json()["code"] == "platform_billing.revision_conflict"
        payload = await _payload(staff, "internal_cost_only")
        headers = _headers(staff, "real-meter-archive-0001")
        before = meter.counts(database)
        archived = await staff.post(RECOVER, json=payload, headers=headers)
        assert archived.status_code == 201, archived.text
        replay = await staff.post(RECOVER, json=payload, headers=headers)
        assert replay.status_code == 200 and replay.json()["idempotent_replay"]
        assert meter.counts(database) == before | {
            "checkpoint": 1,
            "platform_meter_recovery_actions": before["platform_meter_recovery_actions"] + 1,
            "platform_audit_logs": before["platform_audit_logs"] + 1,
        }

        revoked = await owner.patch(
            f"/platform/operators/{operator_id}",
            json={"status": "disabled", "roles": ["platform_billing"]},
            headers=iam._csrf(owner, iam.PLATFORM_CSRF_COOKIE) | {"If-Match": '"2"'},
        )
        assert revoked.status_code == 200, revoked.text
        before = meter.counts(database)
        assert (await staff.get(STATE)).status_code == 401
        assert (await staff.post(RECOVER, json=payload, headers=headers)).status_code == 401
        assert meter.counts(database) == before
        final = await owner.get(STATE)
        assert final.status_code == 200 and final.json()["state"] == "idle"
        assert p95 < 300, f"authenticated ASGI p95={p95:.3f}ms"
