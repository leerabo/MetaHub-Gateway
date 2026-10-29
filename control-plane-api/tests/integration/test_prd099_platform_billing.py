"""PRD-099 exactly-once metering, cycle closure, adjustment, and API contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.routes import router as identity_router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_billing import routes as billing_routes
from metahub_control.platform_billing import service as billing_service
from metahub_control.platform_billing.routes import router as billing_router
from metahub_control.saas_billing import routes as saas_routes
from metahub_control.saas_billing import service as saas_service
from metahub_control.saas_billing.routes import router as saas_router
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
)
from metahub_control.workspace_access.routes import router as workspace_access_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ADMIN_EMAIL = "admin@metahub.local"
ADMIN_PASSWORD = "LocalAdmin!2026"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, database_url: str | None = None):
    database_url = database_url or f"sqlite:///{(tmp_path / 'platform-billing.sqlite3').as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_AUTH_TOKEN_SECRET": "test-prd099-auth-token-secret-2026",
        "METAHUB_IDENTITY_SESSION_SECRET": "test-prd099-global-session-secret-2026",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-prd099-challenge-secret-2026",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": (
            "test-prd099-pii-secret-independent-2026"
        ),
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-prd099-workspace-secret-2026-long",
        "METAHUB_PLATFORM_SESSION_SECRET": "test-prd099-platform-secret-2026-long",
        "METAHUB_SUPPORT_SESSION_SECRET": "test-prd099-support-secret-2026-long",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "test-prd099-mfa-secret-2026-long-value",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": (
            "test-prd099-recovery-secret-2026-long"
        ),
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    apply_migrations(database_url)
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        account_id = connection.execute(
            "SELECT account_id FROM global_accounts WHERE email_normalized=?", (ADMIN_EMAIL,)
        ).fetchone()[0]
        operator_id = "pto_" + "8" * 32
        connection.execute(
            """
            INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,NULL,datetime('now'),datetime('now'))
            """,
            (operator_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
            VALUES(?,'platform_owner','active',NULL,datetime('now'),NULL)
            """,
            (operator_id,),
        )
        connection.commit()
    finally:
        connection.close()
    context = PlatformContext(
        platform_session_id="pts_test_prd099",
        operator_id=operator_id,
        account_id=account_id,
        global_session_id="gis_test_prd099",
        factor_id="mfa_test_prd099",
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=(
            "platform.billing.adjust",
            "platform.billing.read",
            "platform.catalog.read",
            "platform.catalog.write",
        ),
        expires_at=datetime(2026, 8, 23, tzinfo=UTC),
    )
    monkeypatch.setattr(
        saas_routes, "authenticate_platform_session_context", lambda *a, **k: context
    )
    monkeypatch.setattr(
        billing_routes, "authenticate_platform_session_context", lambda *a, **k: context
    )
    monkeypatch.setattr(
        saas_service, "authenticate_platform_session_in_transaction", lambda *a, **k: context
    )
    monkeypatch.setattr(
        billing_service,
        "authenticate_platform_session_in_transaction",
        lambda *a, **k: context,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-099 Platform Billing Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.include_router(identity_router)
    app.include_router(workspace_access_router)
    app.include_router(saas_router)
    app.include_router(billing_router)
    return database_url, app


def _origin_headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site", **extra}


def _csrf(client: AsyncClient, cookie_name: str, **extra: str) -> dict[str, str]:
    value = client.cookies.get(cookie_name)
    assert value
    return _origin_headers(**{"X-CSRF-Token": value, **extra})


async def _enter_workspace(client: AsyncClient) -> str:
    logged_in = await client.post(
        "/identity/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        headers=_origin_headers(),
    )
    assert logged_in.status_code == 200, logged_in.text
    listed = await client.get("/identity/workspaces")
    workspace_id = listed.json()["workspaces"][0]["workspace_id"]
    switched = await client.post(
        "/identity/workspace-switches",
        json={"workspace_id": workspace_id},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert switched.status_code == 200, switched.text
    return workspace_id


async def _create_plan(client: AsyncClient) -> str:
    created = await client.post(
        "/platform/plans",
        json={
            "plan_code": "metered-free",
            "display_name": "Metered Free",
            "display_order": 10,
            "billing_period": "none",
            "currency": "CNY",
            "unit_price": "0.00",
            "trial_days": 14,
            "entitlements": {
                "gateway_requests_per_month": 100,
                "workspace_seats": 5,
                "model_aliases": 10,
                "retention_days": 30,
                "priority_support": False,
            },
            "publish": True,
        },
        headers=_csrf(client, PLATFORM_CSRF_COOKIE),
    )
    assert created.status_code == 201, created.text
    return created.json()["plans"][0]["versions"][0]["plan_version_id"]


def _insert_usage(database_url: str, tenant_id: str, occurred_at: datetime) -> str:
    connection = connect_database(database_url)
    usage_id = "usg_" + uuid4().hex
    request_id = "req_prd099_" + uuid4().hex
    trace_id = "trc_prd099_" + uuid4().hex
    stamp = occurred_at.isoformat().replace("+00:00", "Z")
    try:
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
              policy_snapshot_status,claimed_at)
            VALUES(?,?,?,NULL,NULL,'disabled',?)
            """,
            (tenant_id, request_id, trace_id, stamp),
        )
        connection.execute(
            """
            INSERT INTO request_logs(request_id,tenant_id,trace_id,status,created_at,updated_at)
            VALUES(?,?,?,'success',?,?)
            """,
            (request_id, tenant_id, trace_id, stamp, stamp),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger(
              usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,total_tokens,
              cache_tokens,raw_usage,normalized_usage,cost_amount,cost_cny,cost_usd,currency,
              created_at,updated_at,status,estimated,usage_source)
            VALUES(?,?,?,10,5,15,0,'{}','{}',0,0,0,'CNY',?,?,'recorded',0,'upstream')
            """,
            (usage_id, tenant_id, request_id, stamp, stamp),
        )
        connection.commit()
    finally:
        connection.close()
    return usage_id


@pytest.mark.anyio
async def test_meter_projection_cycle_close_late_usage_and_adjustment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path)
    clock = [datetime(2026, 8, 22, 2, 0, tzinfo=UTC)]
    monkeypatch.setattr(saas_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(billing_service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as platform:
        platform.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        plan_version = await _create_plan(platform)

    async with AsyncClient(transport=transport, base_url="https://testserver") as workspace:
        workspace_id = await _enter_workspace(workspace)
        connection = connect_database(database_url)
        try:
            tenant_id = str(
                connection.execute(
                    "SELECT tenant_id FROM workspace_tenant_bindings WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()[0]
            )
        finally:
            connection.close()
        created = await workspace.post(
            "/subscriptions",
            json={"plan_version_id": plan_version, "start_trial": True},
            headers=_csrf(
                workspace,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd099-subscription-create"},
            ),
        )
        assert created.status_code == 201, created.text

        first_usage = _insert_usage(database_url, tenant_id, clock[0] + timedelta(days=1))
        assert billing_service.project_usage_once(app.state.config, worker_id="test-worker")
        assert not billing_service.project_usage_once(app.state.config, worker_id="test-worker")

        current = await workspace.get("/billing/summary")
        assert current.status_code == 200, current.text
        cycle = current.json()["cycle"]
        assert cycle["status"] == "open"
        meters = {item["meter_code"]: item for item in cycle["meters"]}
        assert meters["api_requests"]["usage_quantity"] == 1
        assert meters["api_requests"]["allocated_quantity"] == 100
        assert meters["input_tokens"]["usage_quantity"] == 10
        assert meters["output_tokens"]["usage_quantity"] == 5
        assert meters["total_tokens"]["usage_quantity"] == 15
        cycle_id = cycle["billing_cycle_id"]

        clock[0] += timedelta(days=15)
        assert billing_service.close_due_cycles(app.state.config, limit=10) == 1
        assert billing_service.close_due_cycles(app.state.config, limit=10) == 0
        closed = await workspace.get(f"/billing/summary?cycle_id={cycle_id}")
        assert closed.status_code == 200, closed.text
        closed_cycle = closed.json()["cycle"]
        assert closed_cycle["status"] == "closed"
        closed_meters = {item["meter_code"]: item for item in closed_cycle["meters"]}
        summary_hash = closed_meters["api_requests"]["summary_sha256"]
        assert summary_hash

        late_usage = _insert_usage(
            database_url,
            tenant_id,
            datetime(2026, 8, 24, 2, 0, tzinfo=UTC),
        )
        assert billing_service.project_usage_once(app.state.config, worker_id="test-worker")
        reconciled = await workspace.get(f"/billing/usage?cycle_id={cycle_id}")
        assert reconciled.status_code == 200, reconciled.text
        reconciled_cycle = reconciled.json()["cycle"]
        assert reconciled_cycle["status"] == "reconciliation_required"
        reconciled_meters = {
            item["meter_code"]: item for item in reconciled_cycle["meters"]
        }
        assert reconciled_meters["api_requests"]["usage_quantity"] == 1
        assert reconciled_meters["api_requests"]["adjustment_quantity"] == 1

        exported = await workspace.get(
            f"/billing/reconciliation-export?cycle_id={cycle_id}"
        )
        assert exported.status_code == 200, exported.text
        assert exported.headers["x-content-type-options"] == "nosniff"
        assert exported.headers["cache-control"] == "no-store, private"
        assert exported.content.startswith(b"\xef\xbb\xbf")

        mixed = await workspace.get(
            "/billing/summary", headers={"Authorization": "Bearer forbidden"}
        )
        assert mixed.status_code == 400
        assert mixed.json()["code"] == "workspace_session.credential_conflict"

    async with AsyncClient(transport=transport, base_url="https://testserver") as platform:
        platform.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        listed = await platform.get("/platform/billing/summary?limit=1")
        assert listed.status_code == 200, listed.text
        assert listed.json()["cycles"][0]["workspace_id"] == workspace_id
        adjustment_payload = {
            "workspace_id": workspace_id,
            "billing_cycle_id": cycle_id,
            "meter_code": "api_requests",
            "reason_code": "manual_credit",
            "quantity_delta": 0,
            "amount_delta": "-1.00",
            "expected_summary_sha256": summary_hash,
        }
        headers = _csrf(
            platform,
            PLATFORM_CSRF_COOKIE,
            **{"Idempotency-Key": "prd099-manual-credit"},
        )
        adjusted = await platform.post(
            "/platform/billing/adjustments", json=adjustment_payload, headers=headers
        )
        assert adjusted.status_code == 200, adjusted.text
        replay = await platform.post(
            "/platform/billing/adjustments", json=adjustment_payload, headers=headers
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

    connection = connect_database(database_url)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM platform_meter_readings").fetchone()[0]
            == 8
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM platform_billing_summaries").fetchone()[0]
            == 4
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM platform_billing_adjustments").fetchone()[0]
            == 5
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_meter_outbox WHERE status='completed'"
        ).fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT source_usage_ledger_id FROM platform_meter_outbox ORDER BY created_at LIMIT 1"
        ).fetchone()[0] in {first_usage, late_usage}
    finally:
        connection.close()

    assert billing_service._csv_cell("=formula-canary") == "'=formula-canary"
