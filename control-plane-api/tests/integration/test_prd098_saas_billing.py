"""PRD-098 catalog, Workspace subscription, and entitlement API smoke tests."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.routes import router as identity_router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.saas_billing import routes as saas_routes
from metahub_control.saas_billing import scheduler as saas_scheduler
from metahub_control.saas_billing import service as saas_service
from metahub_control.saas_billing.routes import router as saas_router
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.routes import router as workspace_access_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ADMIN_EMAIL = "admin@metahub.local"
ADMIN_PASSWORD = "LocalAdmin!2026"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    database_url = f"sqlite:///{(tmp_path / 'saas-billing.sqlite3').as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_AUTH_TOKEN_SECRET": "test-prd098-auth-token-secret-2026",
        "METAHUB_IDENTITY_SESSION_SECRET": "test-prd098-global-session-secret-2026",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-prd098-challenge-secret-2026",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": (
            "test-prd098-pii-secret-independent-2026"
        ),
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-prd098-workspace-secret-2026",
        "METAHUB_PLATFORM_SESSION_SECRET": (
            "test-prd098-platform-session-secret-2026"
        ),
        "METAHUB_SUPPORT_SESSION_SECRET": (
            "test-prd098-support-session-secret-2026"
        ),
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": (
            "test-prd098-mfa-encryption-secret-2026"
        ),
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": (
            "test-prd098-bootstrap-recovery-secret-2026"
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
            "SELECT account_id FROM global_accounts WHERE email_normalized=?",
            (ADMIN_EMAIL,),
        ).fetchone()[0]
        operator_id = "pto_" + "9" * 32
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
        platform_session_id="pts_test_prd098",
        operator_id=operator_id,
        account_id=account_id,
        global_session_id="gis_test_prd098",
        factor_id="mfa_test_prd098",
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=("platform.catalog.read", "platform.catalog.write"),
        expires_at=saas_service._utcnow(),
    )
    monkeypatch.setattr(
        saas_routes, "authenticate_platform_session_context", lambda *a, **k: context
    )
    monkeypatch.setattr(
        saas_service, "authenticate_platform_session_in_transaction", lambda *a, **k: context
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-098 SaaS Billing Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag",),
    )
    app.include_router(identity_router)
    app.include_router(workspace_access_router)
    app.include_router(saas_router)
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
    assert listed.status_code == 200, listed.text
    workspace_id = listed.json()["workspaces"][0]["workspace_id"]
    switched = await client.post(
        "/identity/workspace-switches",
        json={"workspace_id": workspace_id},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert switched.status_code == 200, switched.text
    return workspace_id


async def _create_plan(
    client: AsyncClient, *, code: str, price: str, publish: bool = True
) -> str:
    created = await client.post(
        "/platform/plans",
        json={
            "plan_code": code,
            "display_name": code.title(),
            "display_order": 10 if code == "free" else 20,
            "billing_period": "none" if price == "0.00" else "monthly",
            "currency": "CNY",
            "unit_price": price,
            "trial_days": 14,
            "entitlements": {
                "gateway_requests_per_month": 10000,
                "workspace_seats": 5,
                "model_aliases": 10,
                "retention_days": 30,
                "priority_support": code != "free",
            },
            "publish": publish,
        },
        headers=_csrf(client, PLATFORM_CSRF_COOKIE),
    )
    assert created.status_code == 201, created.text
    return created.json()["plans"][0]["versions"][0]["plan_version_id"]


@pytest.mark.anyio
async def test_catalog_subscription_idempotency_and_plan_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path)
    clock = [datetime(2026, 8, 22, 2, 0, tzinfo=UTC)]
    monkeypatch.setattr(saas_service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as platform:
        platform.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        free_version = await _create_plan(platform, code="free", price="0.00")
        paid_version = await _create_plan(platform, code="team", price="99.00")
        all_plans = await platform.get("/platform/plans")
        assert all_plans.status_code == 200, all_plans.text
        assert {item["plan_code"] for item in all_plans.json()["plans"]} == {"free", "team"}

    async with AsyncClient(transport=transport, base_url="https://testserver") as public:
        catalog = await public.get("/catalog/plans")
        assert catalog.status_code == 200, catalog.text
        assert catalog.headers["cache-control"] == "public, max-age=60"
        assert len(catalog.json()["plans"]) == 2

    for foreign_cookie in (WORKSPACE_SESSION_COOKIE, SUPPORT_SESSION_COOKIE):
        async with AsyncClient(transport=transport, base_url="https://testserver") as foreign:
            foreign.cookies.set(foreign_cookie, "f" * 43)
            rejected = await foreign.get("/platform/plans")
            assert rejected.status_code == 400, rejected.text
            assert rejected.json()["code"] == "platform.credential_conflict"

    async with AsyncClient(transport=transport, base_url="https://testserver") as workspace:
        workspace_id = await _enter_workspace(workspace)
        empty = await workspace.get("/subscriptions/current")
        assert empty.status_code == 200, empty.text
        assert empty.json()["subscription"] is None

        key = "prd098-create-free-plan"
        created = await workspace.post(
            "/subscriptions",
            json={"plan_version_id": free_version, "start_trial": True},
            headers=_csrf(workspace, WORKSPACE_CSRF_COOKIE, **{"Idempotency-Key": key}),
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["subscription"]["workspace_id"] == workspace_id
        assert body["subscription"]["status"] == "trialing"
        assert body["trial"]["status"] == "active"
        assert body["entitlement"]["plan_version_id"] == free_version
        assert body["latest_order"]["status"] == "not_required"

        replay = await workspace.post(
            "/subscriptions",
            json={"plan_version_id": free_version, "start_trial": True},
            headers=_csrf(workspace, WORKSPACE_CSRF_COOKIE, **{"Idempotency-Key": key}),
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        changed_key = "prd098-change-to-paid"
        changed = await workspace.post(
            "/subscriptions/current/change-plan",
            json={"plan_version_id": paid_version},
            headers=_csrf(
                workspace,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": changed_key, "If-Match": '"1"'},
            ),
        )
        assert changed.status_code == 200, changed.text
        changed_body = changed.json()
        assert changed_body["subscription"]["pending_plan_version_id"] == paid_version
        assert changed_body["subscription"]["state_revision"] == 2
        assert changed_body["entitlement"]["plan_version_id"] == free_version
        assert changed_body["latest_order"]["status"] == "pending_payment"

        clock[0] += timedelta(days=15)
        assert saas_scheduler.run_saas_trial_expiry_once(app.state.config) is True
        assert saas_scheduler.run_saas_trial_expiry_once(app.state.config) is False
        expired = await workspace.get("/subscriptions/current")
        assert expired.status_code == 200, expired.text
        expired_body = expired.json()
        assert expired_body["subscription"]["plan_version_id"] == free_version
        assert expired_body["subscription"]["pending_plan_version_id"] is None
        assert expired_body["subscription"]["status"] == "active"
        assert expired_body["trial"]["status"] == "expired"
        assert expired_body["entitlement"]["snapshot_version"] == 2
        assert expired_body["entitlement"]["plan_version_id"] == free_version
        assert expired_body["latest_order"]["status"] == "not_required"

        mixed = await workspace.get(
            "/subscriptions/current", headers={"Authorization": "Bearer forbidden"}
        )
        assert mixed.status_code == 400
        assert mixed.json()["code"] == "workspace_session.credential_conflict"

    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM workspace_subscriptions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM subscription_order_records"
        ).fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM entitlement_snapshots").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM saas_subscription_events"
        ).fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()

    sensitive_exception = "sensitive-scheduler-exception-detail"

    def fail_once(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError(sensitive_exception)

    monkeypatch.setattr(saas_scheduler, "expire_due_trials", fail_once)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=saas_scheduler.logger.name):
        assert saas_scheduler.run_saas_trial_expiry_once(app.state.config) is False
    records = [record for record in caplog.records if record.name == saas_scheduler.logger.name]
    assert records
    assert sensitive_exception not in "\n".join(record.getMessage() for record in records)
    assert all(
        record.__dict__.get("error_code") == "subscription.unavailable"
        and record.__dict__.get("event") == "saas_billing.trial_expiry_failed"
        for record in records
    )
