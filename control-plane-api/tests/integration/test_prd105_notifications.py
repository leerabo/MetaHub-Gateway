"""PRD-105 notification ownership, preferences, templates and worker isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.routes import router as identity_router
from metahub_control.notifications import routes as notification_routes
from metahub_control.notifications import service as notification_service
from metahub_control.notifications.routes import router as notification_router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.workspace_access.constants import WORKSPACE_CSRF_COOKIE
from metahub_control.workspace_access.routes import router as workspace_access_router
from metahub_control.workspaces.routes import router as workspaces_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ADMIN_EMAIL = "admin@metahub.local"
ADMIN_PASSWORD = "LocalAdmin!2026"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"


def _platform_context(account_id: str) -> PlatformContext:
    return PlatformContext(
        platform_session_id="pts_" + "1" * 32,
        operator_id="pto_" + "1" * 32,
        account_id=account_id,
        global_session_id="gss_" + "1" * 32,
        factor_id="mfa_" + "1" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_communications",),
        permissions=(
            "platform.notification_template.read",
            "platform.notification_template.write",
        ),
        expires_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    database_url = f"sqlite:///{(tmp_path / 'notifications.sqlite3').as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_AUTH_TOKEN_SECRET": "test-prd105-auth-token-secret-2026",
        "METAHUB_IDENTITY_SESSION_SECRET": "test-prd105-global-session-secret-2026",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-prd105-challenge-secret-2026",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": "test-prd105-pii-secret-2026-long",
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-prd105-workspace-secret-2026-long",
        "METAHUB_PLATFORM_SESSION_SECRET": "test-prd105-platform-secret-2026-long",
        "METAHUB_SUPPORT_SESSION_SECRET": "test-prd105-support-secret-2026-long",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "test-prd105-mfa-secret-2026-long",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": "test-prd105-recovery-secret-2026-long",
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
        connection.execute(
            """INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
              VALUES(?,?,'active',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')""",
            ("pto_" + "1" * 32, account_id),
        )
        connection.execute(
            """INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
              VALUES(?,'platform_communications','active',NULL,'2026-08-23T00:00:00Z',NULL)""",
            ("pto_" + "1" * 32,),
        )
        connection.commit()
    finally:
        connection.close()
    context = _platform_context(account_id)
    monkeypatch.setattr(
        notification_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: context,
    )
    monkeypatch.setattr(
        notification_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: context,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-105 Notification Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag",),
    )
    app.include_router(identity_router)
    app.include_router(workspace_access_router)
    app.include_router(workspaces_router)
    app.include_router(notification_router)
    return database_url, app, account_id


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
    workspace = listed.json()["workspaces"][0]
    switched = await client.post(
        "/identity/workspace-switches",
        json={"workspace_id": workspace["workspace_id"]},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert switched.status_code == 200, switched.text
    return workspace["workspace_id"]


@pytest.mark.anyio
async def test_inbox_preferences_templates_and_worker_privacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_url, app, account_id = _prepare(monkeypatch, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as workspace:
        workspace_id = await _enter_workspace(workspace)
        connection = connect_database(database_url)
        try:
            tenant_id = connection.execute(
                "SELECT tenant_id FROM workspace_tenant_bindings WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0]
            connection.execute("BEGIN IMMEDIATE")
            assert notification_service.enqueue_notification_in_transaction(
                connection,
                app.state.config,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                recipient_account_id=account_id,
                event_type="invoice.issued",
                source_owner_type="test_invoice",
                source_owner_id="invoice-source-0001",
                parameters={
                    "currency": "CNY",
                    "invoice_number_masked": "****2048",
                    "issued_at": "2026-08-23T08:00:00Z",
                    "total": "106.00",
                },
            )
            assert not notification_service.enqueue_notification_in_transaction(
                connection,
                app.state.config,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                recipient_account_id=account_id,
                event_type="invoice.issued",
                source_owner_type="test_invoice",
                source_owner_id="invoice-source-0001",
                parameters={
                    "currency": "CNY",
                    "invoice_number_masked": "****2048",
                    "issued_at": "2026-08-23T08:00:00Z",
                    "total": "106.00",
                },
            )
            with pytest.raises(ApiError) as unknown:
                notification_service.enqueue_notification_in_transaction(
                    connection,
                    app.state.config,
                    workspace_id=workspace_id,
                    tenant_id=tenant_id,
                    recipient_account_id=account_id,
                    event_type="invoice.issued",
                    source_owner_type="test_invoice",
                    source_owner_id="invoice-source-0002",
                    parameters={
                        "currency": "CNY",
                        "invoice_number_masked": "****4096",
                        "issued_at": "2026-08-23T08:00:00Z",
                        "total": "1.00",
                        "token": "forbidden",
                    },
                )
            assert unknown.value.code == "notification.template_invalid"
            connection.commit()
        finally:
            connection.close()

        assert notification_service.process_notification_once(
            app.state.config, worker_id="prd105-test-worker"
        )
        inbox = await workspace.get("/notifications")
        assert inbox.status_code == 200, inbox.text
        assert inbox.json()["unread_count"] == 1
        notification = inbox.json()["notifications"][0]
        assert notification["subject"] == "Invoice issued"
        assert "****2048" in notification["body"]

        read = await workspace.patch(
            f"/notifications/{notification['notification_id']}/read",
            json={},
            headers=_csrf(workspace, WORKSPACE_CSRF_COOKIE),
        )
        assert read.status_code == 200, read.text
        repeated = await workspace.patch(
            f"/notifications/{notification['notification_id']}/read",
            json={},
            headers=_csrf(workspace, WORKSPACE_CSRF_COOKIE),
        )
        assert repeated.status_code == 200
        assert repeated.json()["notification"]["read"] is True

        preferences = await workspace.get("/notification-preferences")
        assert preferences.status_code == 200, preferences.text
        assert preferences.headers["etag"] == '"1"'
        mandatory = await workspace.patch(
            "/notification-preferences",
            json={"changes": [{"category": "security", "channel": "email", "enabled": False}]},
            headers=_csrf(workspace, WORKSPACE_CSRF_COOKIE, **{"If-Match": '"1"'}),
        )
        assert mandatory.status_code == 409
        updated = await workspace.patch(
            "/notification-preferences",
            json={"changes": [{"category": "billing", "channel": "email", "enabled": False}]},
            headers=_csrf(workspace, WORKSPACE_CSRF_COOKIE, **{"If-Match": '"1"'}),
        )
        assert updated.status_code == 200, updated.text
        assert updated.headers["etag"] == '"2"'
        stale = await workspace.patch(
            "/notification-preferences",
            json={"changes": [{"category": "billing", "channel": "email", "enabled": True}]},
            headers=_csrf(workspace, WORKSPACE_CSRF_COOKIE, **{"If-Match": '"1"'}),
        )
        assert stale.status_code == 412

    async with AsyncClient(transport=transport, base_url="https://testserver") as platform:
        platform.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        templates = await platform.get("/platform/notification-templates")
        assert templates.status_code == 200, templates.text
        assert len(templates.json()["templates"]) == 8
        created = await platform.post(
            "/platform/notification-templates",
            json={
                "template_code": "invoice_issued",
                "event_type": "invoice.issued",
                "channel": "email",
                "locale": "en",
                "parameter_allowlist": [
                    "currency",
                    "invoice_number_masked",
                    "issued_at",
                    "total",
                ],
                "subject_template": "Invoice {{invoice_number_masked}} issued",
                "body_template": (
                    "Invoice {{invoice_number_masked}} for {{total}} {{currency}} "
                    "was issued at {{issued_at}}."
                ),
                "status": "published",
            },
            headers=_csrf(
                platform,
                PLATFORM_CSRF_COOKIE,
                **{"If-Match": '"1"', "Idempotency-Key": "prd105-template-email-0001"},
            ),
        )
        assert created.status_code == 201, created.text
        replay = await platform.post(
            "/platform/notification-templates",
            json={
                "template_code": "invoice_issued",
                "event_type": "invoice.issued",
                "channel": "email",
                "locale": "en",
                "parameter_allowlist": [
                    "currency",
                    "invoice_number_masked",
                    "issued_at",
                    "total",
                ],
                "subject_template": "Invoice {{invoice_number_masked}} issued",
                "body_template": (
                    "Invoice {{invoice_number_masked}} for {{total}} {{currency}} "
                    "was issued at {{issued_at}}."
                ),
                "status": "published",
            },
            headers=_csrf(
                platform,
                PLATFORM_CSRF_COOKIE,
                **{"If-Match": '"1"', "Idempotency-Key": "prd105-template-email-0001"},
            ),
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        injection = await platform.post(
            "/platform/notification-templates",
            json={
                "template_code": "invoice_issued_bad",
                "event_type": "invoice.issued",
                "channel": "webhook",
                "locale": "en",
                "parameter_allowlist": [
                    "currency",
                    "invoice_number_masked",
                    "issued_at",
                    "total",
                ],
                "subject_template": "Invoice {{invoice_number_masked}}",
                "body_template": "<b>{{total}}</b> {{currency}} {{issued_at}}",
                "status": "published",
            },
            headers=_csrf(
                platform,
                PLATFORM_CSRF_COOKIE,
                **{"If-Match": '"2"', "Idempotency-Key": "prd105-template-bad-000001"},
            ),
        )
        assert injection.status_code == 422

    connection = connect_database(database_url)
    try:
        outbox = connection.execute("SELECT * FROM notification_outbox").fetchone()
        attempts = connection.execute(
            "SELECT channel,status,safe_error_code FROM delivery_attempts ORDER BY channel"
        ).fetchall()
        assert outbox["status"] == "succeeded"
        assert len(attempts) == 5
        assert {row["channel"]: row["status"] for row in attempts}["in_app"] == "sent"
        persisted = repr(dict(outbox)).lower() + repr([dict(row) for row in attempts]).lower()
        assert ADMIN_EMAIL not in persisted
        assert "@metahub.local" not in persisted
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_bad_template_is_isolated_and_next_notification_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_url, app, account_id = _prepare(monkeypatch, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        workspace_id = await _enter_workspace(client)

    connection = connect_database(database_url)
    try:
        tenant_id = connection.execute(
            "SELECT tenant_id FROM workspace_tenant_bindings WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()[0]
        started_at = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
        connection.execute("BEGIN IMMEDIATE")
        for offset, source_id in enumerate(("bad-template-source-0001", "good-source-0001")):
            assert notification_service.enqueue_notification_in_transaction(
                connection,
                app.state.config,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                recipient_account_id=account_id,
                event_type="invoice.issued",
                source_owner_type="test_invoice",
                source_owner_id=source_id,
                parameters={
                    "currency": "CNY",
                    "invoice_number_masked": "****2048",
                    "issued_at": "2026-08-23T09:00:00Z",
                    "total": "106.00",
                },
                now=started_at + timedelta(seconds=offset),
            )
        connection.commit()
    finally:
        connection.close()

    prepare_delivery = notification_service._prepare_delivery

    def isolate_bad_template(config, claim):
        if claim.source_owner_id == "bad-template-source-0001":
            raise ApiError("notification.template_invalid")
        return prepare_delivery(config, claim)

    monkeypatch.setattr(notification_service, "_prepare_delivery", isolate_bad_template)
    processed_at = started_at + timedelta(minutes=1)
    assert notification_service.process_notification_once(
        app.state.config, worker_id="prd105-isolation-worker", now=processed_at
    )
    assert notification_service.process_notification_once(
        app.state.config,
        worker_id="prd105-isolation-worker",
        now=processed_at + timedelta(seconds=1),
    )

    connection = connect_database(database_url)
    try:
        rows = connection.execute(
            """SELECT source_owner_id,status,safe_error_code FROM notification_outbox
               ORDER BY created_at"""
        ).fetchall()
        actual = [
            (row["source_owner_id"], row["status"], row["safe_error_code"])
            for row in rows
        ]
        assert actual == [
            ("bad-template-source-0001", "failed", "notification.template_invalid"),
            ("good-source-0001", "succeeded", None),
        ]
        assert connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.anyio
async def test_queue_head_fairness_skips_workspace_with_active_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_url, app, account_id = _prepare(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """INSERT INTO account_verifications(
              account_verification_id,account_id,verification_type,identifier_hmac,
              source_type,source_intent_id,verified_at)
              VALUES('avf_10500000000000000000000000000000',?,'email',?,
                'registration','rgi_10500000000000000000000000000000',
                '2026-08-23T00:00:00Z')""",
            (account_id, "1" * 64),
        )
        connection.commit()
    finally:
        connection.close()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        logged_in = await client.post(
            "/identity/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            headers=_origin_headers(),
        )
        assert logged_in.status_code == 200, logged_in.text
        first_workspace_id = (await client.get("/identity/workspaces")).json()["workspaces"][0][
            "workspace_id"
        ]
        created = await client.post(
            "/workspaces",
            json={
                "kind": "organization",
                "name": "Notification Fairness",
                "slug": "notification-fairness",
                "region": "cn-north-1",
                "service_mode": "internal_only",
            },
            headers=_csrf(
                client,
                GLOBAL_CSRF_COOKIE,
                **{"Idempotency-Key": "prd105-queue-fairness-0001"},
            ),
        )
        assert created.status_code == 201, created.text
        second_workspace_id = created.json()["workspace"]["workspace_id"]

    connection = connect_database(database_url)
    try:
        tenant_ids = {
            row["workspace_id"]: row["tenant_id"]
            for row in connection.execute(
                """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
                   WHERE workspace_id IN(?,?)""",
                (first_workspace_id, second_workspace_id),
            ).fetchall()
        }
        started_at = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
        connection.execute("BEGIN IMMEDIATE")
        for workspace_id, offset, source_id in (
            (first_workspace_id, 0, "queue-source-a-0001"),
            (first_workspace_id, 1, "queue-source-a-0002"),
            (second_workspace_id, 2, "queue-source-b-0001"),
        ):
            assert notification_service.enqueue_notification_in_transaction(
                connection,
                app.state.config,
                workspace_id=workspace_id,
                tenant_id=tenant_ids[workspace_id],
                recipient_account_id=account_id,
                event_type="invoice.issued",
                source_owner_type="test_invoice",
                source_owner_id=source_id,
                parameters={
                    "currency": "CNY",
                    "invoice_number_masked": "****4096",
                    "issued_at": "2026-08-23T10:00:00Z",
                    "total": "1.00",
                },
                now=started_at + timedelta(seconds=offset),
            )
        connection.commit()
    finally:
        connection.close()

    claim_time = started_at + timedelta(minutes=1)
    first = notification_service.claim_next_notification(
        app.state.config, worker_id="prd105-fairness-worker-a", now=claim_time
    )
    second = notification_service.claim_next_notification(
        app.state.config, worker_id="prd105-fairness-worker-b", now=claim_time
    )
    assert first is not None and first.source_owner_id == "queue-source-a-0001"
    assert second is not None and second.source_owner_id == "queue-source-b-0001"
