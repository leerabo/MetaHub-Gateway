"""PRD-096 platform Workspace directory and lifecycle API contracts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import shutil
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.customer_health.service import SourceSnapshot
from metahub_control.identity import service as identity_service
from metahub_control.identity.delivery import (
    clear_test_identity_challenges,
    pop_test_identity_challenge,
)
from metahub_control.identity.routes import router as identity_router
from metahub_control.platform_access import service as platform_service
from metahub_control.platform_access import workspace_service
from metahub_control.platform_access.constants import PLATFORM_CSRF_COOKIE
from metahub_control.platform_access.routes import router as platform_router
from metahub_control.platform_customer_success import projection as customer_success_projection
from metahub_control.workspace_access.routes import router as workspace_access_router
from metahub_control.workspaces.routes import router as workspaces_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ADMIN_EMAIL = "admin@metahub.local"
ADMIN_PASSWORD = f"Aa9!{secrets.token_urlsafe(24)}"
RECOVERY_SECRET = secrets.token_urlsafe(32)
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd096-api-migrated") / "platform-workspaces.sqlite3"
    result = apply_migrations(f"sqlite:///{database.as_posix()}")
    assert result.applied[-1] == "0156"
    return database


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, migrated_database: Path):
    database = tmp_path / "platform-workspaces.sqlite3"
    shutil.copy2(migrated_database, database)
    database_url = f"sqlite:///{database.as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_AUTH_TOKEN_SECRET": "test-legacy-auth-token-secret",
        "METAHUB_IDENTITY_SESSION_SECRET": "test-global-session-secret-0123456789abcdef",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-challenge-secret-0123456789abcdef0",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": "test-pii-secret-0123456789abcdef012345",
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-workspace-session-secret-0123456789abcdef",
        "METAHUB_PLATFORM_SESSION_SECRET": "test-platform-session-secret-0123456789abcdef0",
        "METAHUB_SUPPORT_SESSION_SECRET": "test-support-session-secret-0123456789abcdef01",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "test-mfa-encryption-secret-0123456789abcdef0",
        "METAHUB_PLATFORM_BOOTSTRAP_ENABLED": "true",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": RECOVERY_SECRET,
        "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "test_capture",
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    apply_seed_data(database_url)
    clear_test_identity_challenges()
    app = create_service_app(
        "control-plane-api",
        "PRD-096 Platform Workspace Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.include_router(auth_router)
    app.include_router(identity_router)
    app.include_router(workspaces_router)
    app.include_router(workspace_access_router)
    app.include_router(platform_router)
    return database_url, app


def _headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site", **extra}


def _csrf(client: AsyncClient, cookie_name: str, **extra: str) -> dict[str, str]:
    value = client.cookies.get(cookie_name)
    assert value
    return _headers(**{"X-CSRF-Token": value, **extra})


def _totp(secret: str, moment: datetime) -> str:
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(moment.timestamp()) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


async def _bootstrap_owner(client: AsyncClient, clock: list[datetime]) -> str:
    logged_in = await client.post(
        "/identity/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        headers=_headers(),
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
    enrollment = await client.post(
        "/identity/mfa/totp/enrollments",
        json={"current_password": ADMIN_PASSWORD},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert enrollment.status_code == 200, enrollment.text
    secret = enrollment.json()["secret"]
    activated = await client.post(
        "/identity/mfa/totp/verifications",
        json={"factor_id": enrollment.json()["factor_id"], "code": _totp(secret, clock[0])},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert activated.status_code == 200, activated.text
    clock[0] += timedelta(seconds=30)
    bootstrap = await client.post(
        "/platform/bootstrap",
        json={
            "current_password": ADMIN_PASSWORD,
            "totp_code": _totp(secret, clock[0]),
            "recovery_secret": RECOVERY_SECRET,
            "reason_code": "initial_platform_owner",
            "confirmed": True,
        },
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert bootstrap.status_code == 200, bootstrap.text
    clock[0] += timedelta(seconds=30)
    session = await client.post(
        "/platform/sessions",
        json={"totp_code": _totp(secret, clock[0])},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert session.status_code == 200, session.text
    return workspace_id


async def _register_platform_operator(
    owner: AsyncClient,
    transport: ASGITransport,
    database_url: str,
    clock: list[datetime],
    *,
    email: str,
    role: str,
) -> AsyncClient:
    client = AsyncClient(transport=transport, base_url="https://testserver")
    password = f"Aa9!{secrets.token_urlsafe(24)}"
    started = await client.post(
        "/identity/registrations",
        json={
            "email": email,
            "display_name": role.replace("platform_", "").replace("_", " ").title(),
            "password": password,
            "consent_version": "privacy-2026-08",
        },
        headers=_headers(),
    )
    assert started.status_code == 200, started.text
    challenge = pop_test_identity_challenge("registration", started.json()["intent_id"])
    assert challenge is not None
    verified = await client.post(
        "/identity/verifications",
        json={"intent_id": started.json()["intent_id"], "code": challenge.code},
        headers=_headers(),
    )
    assert verified.status_code == 200, verified.text
    enrollment = await client.post(
        "/identity/mfa/totp/enrollments",
        json={"current_password": password},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert enrollment.status_code == 200, enrollment.text
    secret = enrollment.json()["secret"]
    activated = await client.post(
        "/identity/mfa/totp/verifications",
        json={"factor_id": enrollment.json()["factor_id"], "code": _totp(secret, clock[0])},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert activated.status_code == 200, activated.text
    connection = connect_database(database_url)
    try:
        account_id = connection.execute(
            "SELECT account_id FROM global_accounts WHERE email_normalized=?", (email,)
        ).fetchone()[0]
    finally:
        connection.close()
    created = await owner.post(
        "/platform/operators",
        json={"account_id": account_id, "roles": [role]},
        headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
    )
    assert created.status_code == 201, created.text
    clock[0] += timedelta(seconds=30)
    session = await client.post(
        "/platform/sessions",
        json={"totp_code": _totp(secret, clock[0])},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert session.status_code == 200, session.text
    return client


@pytest.mark.anyio
async def test_directory_suspend_restore_and_workspace_authority_overlay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(identity_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(platform_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(workspace_service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        unauthorized = await client.post(
            "/platform/workspaces/not-a-workspace/suspend",
            content=b"not-json",
            headers=_headers(**{"Content-Type": "application/json"}),
        )
        assert unauthorized.status_code == 401
        assert unauthorized.json()["code"] == "platform.session_required"

        workspace_id = await _bootstrap_owner(client, clock)
        directory = await client.get("/platform/workspaces?limit=1")
        assert directory.status_code == 200, directory.text
        assert len(directory.json()["items"]) == 1
        summary = directory.json()["items"][0]
        assert summary["workspace_id"] == workspace_id
        assert summary["operational_status"] == "active"
        assert summary["plan_status"] == "not_available"
        assert summary["deployment_status"] == "not_configured"
        assert "owner_email" not in directory.text

        headers = _csrf(
            client,
            PLATFORM_CSRF_COOKIE,
            **{"If-Match": '"1"', "Idempotency-Key": "suspend-workspace-0001"},
        )
        suspended = await client.post(
            f"/platform/workspaces/{workspace_id}/suspend",
            json={"reason_code": "security_containment", "confirmed": True},
            headers=headers,
        )
        assert suspended.status_code == 200, suspended.text
        assert suspended.json()["workspace"]["operational_status"] == "suspended"
        action_intent_id = suspended.json()["action_intent_id"]

        replay = await client.post(
            f"/platform/workspaces/{workspace_id}/suspend",
            json={"reason_code": "security_containment", "confirmed": True},
            headers=headers,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["action_intent_id"] == action_intent_id
        conflict = await client.post(
            f"/platform/workspaces/{workspace_id}/suspend",
            json={"reason_code": "terms_enforcement", "confirmed": True},
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "platform.idempotency_conflict"
        hidden = await client.get("/identity/workspaces")
        assert hidden.status_code == 200
        assert hidden.json()["total"] == 0

        restored = await client.post(
            f"/platform/workspaces/{workspace_id}/restore",
            json={"reason_code": "security_cleared", "confirmed": True},
            headers=_csrf(
                client,
                PLATFORM_CSRF_COOKIE,
                **{"If-Match": '"2"', "Idempotency-Key": "restore-workspace-0001"},
            ),
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["workspace"]["operational_status"] == "active"
        visible = await client.get("/identity/workspaces")
        assert visible.status_code == 200
        assert visible.json()["total"] == 1

    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_sessions WHERE status='active'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_operational_events WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_operational_event_outbox"
            ).fetchone()[0]
            == 2
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_risk_tags_owner_notification_events_and_safe_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(identity_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(platform_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(workspace_service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        workspace_id = await _bootstrap_owner(client, clock)
        added = await client.post(
            f"/platform/workspaces/{workspace_id}/risk-tags",
            json={"tag_code": "security_review", "reason_code": "security_review"},
            headers=_csrf(
                client,
                PLATFORM_CSRF_COOKIE,
                **{"If-Match": '"1"', "Idempotency-Key": "risk-add-workspace-0001"},
            ),
        )
        assert added.status_code == 200, added.text
        assert added.json()["workspace"]["platform_risk_tags"] == ["security_review"]
        removed = await client.request(
            "DELETE",
            f"/platform/workspaces/{workspace_id}/risk-tags/security_review",
            json={"reason_code": "security_review"},
            headers=_csrf(
                client,
                PLATFORM_CSRF_COOKIE,
                **{"If-Match": '"2"', "Idempotency-Key": "risk-remove-workspace-0001"},
            ),
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["workspace"]["platform_risk_tags"] == []
        notification = await client.post(
            f"/platform/workspaces/{workspace_id}/owner-notifications",
            json={
                "template_code": "workspace_operational_notice",
                "reason_code": "operational_notice",
            },
            headers=_csrf(
                client,
                PLATFORM_CSRF_COOKIE,
                **{"If-Match": '"1"', "Idempotency-Key": "owner-notify-workspace-0001"},
            ),
        )
        assert notification.status_code == 200, notification.text
        assert notification.json()["notification_status"] == "pending"
        events = await client.get(f"/platform/workspaces/{workspace_id}/events")
        assert events.status_code == 200, events.text
        assert [event["event_type"] for event in events.json()["events"]] == [
            "workspace.owner_notification_requested",
            "workspace.risk_tag_removed",
            "workspace.risk_tag_added",
        ]

    connection = connect_database(database_url)
    try:
        outbox = connection.execute(
            "SELECT payload_json,payload_sha256 FROM workspace_owner_notification_outbox"
        ).fetchone()
        payload = json.loads(outbox["payload_json"])
        assert set(payload) == {"action_intent_id", "schema_version", "template_code"}
        assert "email" not in outbox["payload_json"].lower()
        assert (
            hashlib.sha256(outbox["payload_json"].encode("ascii")).hexdigest()
            == outbox["payload_sha256"]
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_role_projection_cursor_scope_search_and_risk_responsibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(identity_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(platform_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(workspace_service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)
    role_clients: dict[str, AsyncClient] = {}
    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        workspace_id = await _bootstrap_owner(owner, clock)
        roles = (
            "platform_ops",
            "platform_support",
            "platform_billing",
            "platform_security",
            "platform_customer_success",
        )
        try:
            for index, role in enumerate(roles):
                role_clients[role] = await _register_platform_operator(
                    owner,
                    transport,
                    database_url,
                    clock,
                    email=f"prd096-role-{index}@example.test",
                    role=role,
                )

            workspace_creator = role_clients["platform_support"]
            created_workspace = await workspace_creator.post(
                "/workspaces",
                json={
                    "kind": "organization",
                    "name": "Operations Secondary",
                    "slug": "operations-secondary",
                    "region": "cn-north-1",
                    "service_mode": "internal_only",
                },
                headers=_csrf(
                    workspace_creator,
                    GLOBAL_CSRF_COOKIE,
                    **{"Idempotency-Key": "prd096-secondary-workspace"},
                ),
            )
            assert created_workspace.status_code == 201, created_workspace.text

            expected_fields = {
                "platform_ops": {"owner_contact_status"},
                "platform_support": set(),
                "platform_billing": {"plan_status"},
                "platform_security": {"platform_risk_tags"},
                "platform_customer_success": {
                    "platform_risk_tags",
                    "owner_contact_status",
                },
            }
            for role, client in role_clients.items():
                response = await client.get(f"/platform/workspaces/{workspace_id}")
                assert response.status_code == 200, response.text
                payload = response.json()["workspace"]
                projected = ("plan_status", "platform_risk_tags", "owner_contact_status")
                actual = {field for field in projected if field in payload}
                assert actual == expected_fields[role]
                assert "tenant_id" not in response.text
                assert "account_id" not in response.text

            first_page = await owner.get("/platform/workspaces?limit=1")
            assert first_page.status_code == 200, first_page.text
            assert first_page.json()["has_more"] is True
            cursor = first_page.json()["next_cursor"]
            assert cursor
            cross_role = await role_clients["platform_support"].get(
                "/platform/workspaces", params={"limit": 1, "cursor": cursor}
            )
            assert cross_role.status_code == 422
            assert cross_role.json()["code"] == "platform.request_invalid"
            tampered = ("A" if cursor[0] != "A" else "B") + cursor[1:]
            bad_cursor = await owner.get(
                "/platform/workspaces", params={"limit": 1, "cursor": tampered}
            )
            assert bad_cursor.status_code == 422
            assert bad_cursor.json()["code"] == "platform.request_invalid"

            exact_slug = await owner.get(
                "/platform/workspaces", params={"search": "operations-secondary"}
            )
            assert exact_slug.status_code == 200, exact_slug.text
            assert [item["workspace_id"] for item in exact_slug.json()["items"]] == [
                created_workspace.json()["workspace"]["workspace_id"]
            ]

            security = role_clients["platform_security"]
            customer_success = role_clients["platform_customer_success"]
            denied_commercial = await security.post(
                f"/platform/workspaces/{workspace_id}/risk-tags",
                json={"tag_code": "payment_attention", "reason_code": "payment_follow_up"},
                headers=_csrf(
                    security,
                    PLATFORM_CSRF_COOKIE,
                    **{"If-Match": '"1"', "Idempotency-Key": "security-commercial-denied"},
                ),
            )
            assert denied_commercial.status_code == 403
            denied_security = await customer_success.post(
                f"/platform/workspaces/{workspace_id}/risk-tags",
                json={"tag_code": "security_review", "reason_code": "security_review"},
                headers=_csrf(
                    customer_success,
                    PLATFORM_CSRF_COOKIE,
                    **{"If-Match": '"1"', "Idempotency-Key": "customer-security-denied"},
                ),
            )
            assert denied_security.status_code == 403
            security_added = await security.post(
                f"/platform/workspaces/{workspace_id}/risk-tags",
                json={"tag_code": "security_review", "reason_code": "security_review"},
                headers=_csrf(
                    security,
                    PLATFORM_CSRF_COOKIE,
                    **{"If-Match": '"1"', "Idempotency-Key": "security-risk-allowed-01"},
                ),
            )
            assert security_added.status_code == 200, security_added.text
            commercial_added = await customer_success.post(
                f"/platform/workspaces/{workspace_id}/risk-tags",
                json={"tag_code": "adoption_attention", "reason_code": "adoption_follow_up"},
                headers=_csrf(
                    customer_success,
                    PLATFORM_CSRF_COOKIE,
                    **{"If-Match": '"2"', "Idempotency-Key": "customer-risk-allowed-01"},
                ),
            )
            assert commercial_added.status_code == 200, commercial_added.text
            assert commercial_added.json()["workspace"]["platform_risk_tags"] == [
                "adoption_attention",
                "security_review",
            ]
        finally:
            await asyncio.gather(*(client.aclose() for client in role_clients.values()))


@pytest.mark.anyio
async def test_health_projection_fresh_stale_and_bad_source_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    auth_clock = [datetime.now(UTC).replace(microsecond=0)]
    health_clock = [auth_clock[0]]
    monkeypatch.setattr(identity_service, "_utcnow", lambda: auth_clock[0])
    monkeypatch.setattr(platform_service, "_utcnow", lambda: auth_clock[0])
    monkeypatch.setattr(workspace_service, "_utcnow", lambda: health_clock[0])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        workspace_id = await _bootstrap_owner(client, auth_clock)
        health_clock[0] = auth_clock[0]
        connection = connect_database(database_url)
        try:
            tenant_id = connection.execute(
                "SELECT tenant_id FROM workspace_tenant_bindings WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0]
            evaluated = health_clock[0]
            window_end = evaluated.replace(minute=0, second=0)
            source = SourceSnapshot(
                tenant_id=tenant_id,
                window_start=(window_end - timedelta(days=7)).isoformat().replace("+00:00", "Z"),
                window_end=window_end.isoformat().replace("+00:00", "Z"),
                comparison_start=(window_end - timedelta(days=14))
                .isoformat()
                .replace("+00:00", "Z"),
                comparison_end=(window_end - timedelta(days=7)).isoformat().replace("+00:00", "Z"),
                current_request_count=100,
                previous_request_count=80,
                successful_request_count=99,
                failed_request_count=1,
                unresolved_cost_anomaly_count=0,
                critical_cost_anomaly_count=0,
                active_budget_rule_count=1,
                eligible_user_count=4,
                recently_active_user_count=4,
                activation_stage_count=7,
                evaluated_at=evaluated.isoformat().replace("+00:00", "Z"),
            )
            monkeypatch.setattr(
                customer_success_projection,
                "collect_source_snapshot",
                lambda *_args, **_kwargs: source,
            )
            monkeypatch.setattr(
                customer_success_projection,
                "_support_counts",
                lambda *_args, **_kwargs: (0, 0, 0, 0, 0, 0),
            )
            connection.execute("BEGIN IMMEDIATE")
            assert customer_success_projection.reconcile_accounts(
                connection, now=evaluated
            ) >= 1
            account_id = connection.execute(
                """SELECT customer_account_id FROM platform_customer_account_projections
                    WHERE workspace_id=? AND tenant_id=?""",
                (workspace_id, tenant_id),
            ).fetchone()[0]
            score_id = customer_success_projection.materialize_health_v2(
                connection, account_id, now=evaluated
            )
            assert score_id is not None
            connection.commit()
        finally:
            connection.close()

        fresh = await client.get(f"/platform/workspaces/{workspace_id}/health")
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["health"]["status"] == "fresh"
        assert fresh.json()["health"]["score_band"] == "healthy"
        for forbidden in ("current_request_count", "score_bps", "source_hash", "tenant_id"):
            assert forbidden not in fresh.text

        health_clock[0] += timedelta(hours=3)
        stale = await client.get(f"/platform/workspaces/{workspace_id}/health")
        assert stale.status_code == 200, stale.text
        assert stale.json()["health"]["status"] == "stale"

        connection = connect_database(database_url)
        try:
            connection.execute("DROP TRIGGER trg_platform_customer_health_update_prd140")
            connection.execute(
                "UPDATE platform_customer_health_snapshots SET health_projection_sha256=? "
                "WHERE customer_health_snapshot_id=?",
                ("f" * 64, score_id),
            )
            connection.commit()
        finally:
            connection.close()
        unavailable = await client.get(f"/platform/workspaces/{workspace_id}/health")
        assert unavailable.status_code == 200, unavailable.text
        assert unavailable.json()["health"] == {"status": "unavailable"}


@pytest.mark.anyio
async def test_concurrent_suspend_commits_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(identity_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(platform_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(workspace_service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        workspace_id = await _bootstrap_owner(client, clock)
        csrf = client.cookies.get(PLATFORM_CSRF_COOKIE)
        assert csrf
        responses = await asyncio.gather(
            *(
                client.post(
                    f"/platform/workspaces/{workspace_id}/suspend",
                    json={"reason_code": "operational_protection", "confirmed": True},
                    headers=_headers(
                        **{
                            "X-CSRF-Token": csrf,
                            "If-Match": '"1"',
                            "Idempotency-Key": f"concurrent-suspend-{index:04d}",
                        }
                    ),
                )
                for index in range(20)
            )
        )
        committed = [response for response in responses if response.status_code == 200]
        rejected = [response for response in responses if response.status_code == 412]
        assert len(committed) == 1
        assert len(rejected) == 19
        assert {response.json()["code"] for response in rejected} <= {
            "platform.workspace_revision_conflict",
            "platform.workspace_lifecycle_conflict",
        }

    connection = connect_database(database_url)
    try:
        state = connection.execute(
            "SELECT status,revision FROM workspace_operational_states WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        assert tuple(state) == ("suspended", 2)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_operational_events WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()
