"""PRD-095 browser APIs for MFA, Platform IAM, and controlled support."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import math
import shutil
import struct
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.identity import service as identity_service
from metahub_control.identity.delivery import (
    clear_test_identity_challenges,
    pop_test_identity_challenge,
)
from metahub_control.identity.models import IdentityPlatformAccessResponse
from metahub_control.identity.routes import router as identity_router
from metahub_control.platform_access import service as platform_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.models import (
    PlatformNavigationItem,
    SupportDiagnosticsResponse,
)
from metahub_control.platform_access.routes import router as platform_router
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.routes import router as workspace_access_router
from metahub_control.workspaces.routes import router as workspaces_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ADMIN_EMAIL = "admin@metahub.local"
ADMIN_PASSWORD = "LocalAdmin!2026"
SECOND_EMAIL = "platform.security@example.test"
SECOND_PASSWORD = "PlatformSecurity!2026"
RECOVERY_SECRET = "offline-bootstrap-recovery-secret-0123456789abcdef"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd095-api-migrated") / "platform-current.sqlite3"
    result = apply_migrations(f"sqlite:///{database.as_posix()}")
    assert "0087" in result.applied
    return database


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, migrated_database: Path):
    database = tmp_path / "platform.sqlite3"
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
        "PRD-095 Platform IAM Test",
        extra_cors_allow_headers=("If-Match", "X-CSRF-Token"),
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


def _csrf(client: AsyncClient, cookie_name: str) -> dict[str, str]:
    value = client.cookies.get(cookie_name)
    assert value
    return _headers(**{"X-CSRF-Token": value})


def _totp(secret: str, moment: datetime) -> str:
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(moment.timestamp()) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


async def _login(client: AsyncClient, email: str, password: str) -> None:
    response = await client.post(
        "/identity/login", json={"email": email, "password": password}, headers=_headers()
    )
    assert response.status_code == 200, response.text


async def _enroll(client: AsyncClient, password: str, moment: datetime) -> str:
    enrollment = await client.post(
        "/identity/mfa/totp/enrollments",
        json={"current_password": password},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert enrollment.status_code == 200, enrollment.text
    secret = enrollment.json()["secret"]
    activated = await client.post(
        "/identity/mfa/totp/verifications",
        json={"factor_id": enrollment.json()["factor_id"], "code": _totp(secret, moment)},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert activated.status_code == 200, activated.text
    return secret


async def _register(client: AsyncClient) -> None:
    started = await client.post(
        "/identity/registrations",
        json={
            "email": SECOND_EMAIL,
            "display_name": "Platform Security",
            "password": SECOND_PASSWORD,
            "consent_version": "privacy-2026-08",
        },
        headers=_headers(),
    )
    assert started.status_code == 200, started.text
    intent_id = started.json()["intent_id"]
    challenge = pop_test_identity_challenge("registration", intent_id)
    assert challenge is not None
    verified = await client.post(
        "/identity/verifications",
        json={"intent_id": intent_id, "code": challenge.code},
        headers=_headers(),
    )
    assert verified.status_code == 200, verified.text


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def test_prd095_security_response_models_forbid_extra_fields() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        PlatformNavigationItem.model_validate(
            {
                "id": "audit",
                "label": "Audit",
                "href": "/platform.html#audit",
                "internal_scope": "must-not-leak",
            }
        )
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        IdentityPlatformAccessResponse.model_validate(
            {
                "available": True,
                "request_id": "req_test",
                "trace_id": "trc_test",
                "operator_role": "must-not-leak",
            }
        )
    with pytest.raises(ValueError, match="Input should be"):
        SupportDiagnosticsResponse.model_validate(
            {
                "workspace_id": "wsp_" + "1" * 32,
                "operational_status": "unknown",
                "region_code": None,
                "provisioning_status": "ready",
                "health_band": None,
                "active_memberships": 0,
                "request_id": "req_test",
                "trace_id": "trc_test",
            }
        )


@pytest.mark.anyio
async def test_platform_mfa_bootstrap_dual_approval_and_support_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(identity_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(platform_service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        await _login(owner, ADMIN_EMAIL, ADMIN_PASSWORD)
        workspace_list = await owner.get("/identity/workspaces")
        workspace_id = workspace_list.json()["workspaces"][0]["workspace_id"]
        enrollment = await owner.post(
            "/identity/mfa/totp/enrollments",
            json={"current_password": ADMIN_PASSWORD},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert enrollment.status_code == 200, enrollment.text
        owner_secret = enrollment.json()["secret"]
        factor_id = enrollment.json()["factor_id"]
        valid_codes = {
            _totp(owner_secret, clock[0] + timedelta(seconds=offset)) for offset in (-30, 0, 30)
        }
        invalid_code = next(
            f"{candidate:06d}"
            for candidate in range(1_000_000)
            if f"{candidate:06d}" not in valid_codes
        )
        for _ in range(5):
            rejected = await owner.post(
                "/identity/mfa/totp/verifications",
                json={"factor_id": factor_id, "code": invalid_code},
                headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
            )
            assert rejected.status_code == 400
            assert rejected.json()["code"] == "identity.mfa_verification_failed"
        locked = await owner.post(
            "/identity/mfa/totp/verifications",
            json={"factor_id": factor_id, "code": _totp(owner_secret, clock[0])},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert locked.status_code == 400
        assert locked.json()["code"] == "identity.mfa_verification_failed"
        clock[0] += timedelta(minutes=5, seconds=1)
        activated = await owner.post(
            "/identity/mfa/totp/verifications",
            json={"factor_id": factor_id, "code": _totp(owner_secret, clock[0])},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert activated.status_code == 200, activated.text
        me = await owner.get("/identity/me")
        assert me.json()["account"]["mfa_configured"] is True
        platform_access = await owner.get("/identity/platform-access")
        assert platform_access.status_code == 200
        assert platform_access.json()["available"] is False

        clock[0] += timedelta(seconds=30)
        bootstrapped = await owner.post(
            "/platform/bootstrap",
            json={
                "current_password": ADMIN_PASSWORD,
                "totp_code": _totp(owner_secret, clock[0]),
                "recovery_secret": RECOVERY_SECRET,
                "reason_code": "initial_platform_owner",
                "confirmed": True,
            },
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert bootstrapped.status_code == 200, bootstrapped.text
        assert bootstrapped.json()["operator"]["roles"] == ["platform_owner"]
        platform_access = await owner.get("/identity/platform-access")
        assert platform_access.status_code == 200
        assert platform_access.json()["available"] is True

        replayed = await owner.post(
            "/platform/sessions",
            json={"totp_code": _totp(owner_secret, clock[0])},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert replayed.status_code == 400
        assert replayed.json()["code"] == "identity.mfa_verification_failed"

        clock[0] += timedelta(seconds=30)
        exchanged = await owner.post(
            "/platform/sessions",
            json={"totp_code": _totp(owner_secret, clock[0])},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert exchanged.status_code == 200, exchanged.text
        assert owner.cookies.get(PLATFORM_SESSION_COOKIE)
        assert owner.cookies.get(PLATFORM_CSRF_COOKIE)
        assert owner.cookies.get(WORKSPACE_SESSION_COOKIE) is None
        navigation = await owner.get("/platform/navigation")
        assert navigation.status_code == 200
        assert len(navigation.json()["items"]) == 13
        rejected_reason = await owner.post(
            "/platform/support-grants",
            json={
                "workspace_id": workspace_id,
                "reason_code": "arbitrary_local_reason",
                "duration_minutes": 20,
            },
            headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
        )
        assert rejected_reason.status_code == 422
        assert rejected_reason.json()["code"] == "platform.request_invalid"
        malformed_grant = await owner.post(
            "/platform/support-grants/not-a-grant/sessions",
            json={},
            headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
        )
        assert malformed_grant.status_code == 404
        assert malformed_grant.json()["code"] == "support.grant_not_found"

        async with AsyncClient(transport=transport, base_url="https://testserver") as security:
            await _register(security)
            security_secret = await _enroll(security, SECOND_PASSWORD, clock[0])
            connection = connect_database(database_url)
            try:
                second_account_id = connection.execute(
                    "SELECT account_id FROM global_accounts WHERE email_normalized=?",
                    (SECOND_EMAIL,),
                ).fetchone()[0]
            finally:
                connection.close()
            created = await owner.post(
                "/platform/operators",
                json={"account_id": second_account_id, "roles": ["platform_security"]},
                headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
            )
            assert created.status_code == 201, created.text

            clock[0] += timedelta(seconds=30)
            security_session = await security.post(
                "/platform/sessions",
                json={"totp_code": _totp(security_secret, clock[0])},
                headers=_csrf(security, GLOBAL_CSRF_COOKIE),
            )
            assert security_session.status_code == 200, security_session.text
            malformed_break_glass = await security.post(
                "/platform/break-glass-sessions/not-break-glass/reviews",
                json={},
                headers=_csrf(security, PLATFORM_CSRF_COOKIE),
            )
            assert malformed_break_glass.status_code == 404
            assert malformed_break_glass.json()["code"] == "support.grant_not_found"

            grant = await owner.post(
                "/platform/support-grants",
                json={
                    "workspace_id": workspace_id,
                    "reason_code": "workspace_diagnostics",
                    "duration_minutes": 20,
                },
                headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
            )
            assert grant.status_code == 201, grant.text
            grant_id = grant.json()["grant"]["grant_id"]
            self_approval = await owner.post(
                f"/platform/support-grants/{grant_id}/approve",
                json={},
                headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
            )
            assert self_approval.status_code == 403

            platform_approval = await security.post(
                f"/platform/support-grants/{grant_id}/approve",
                json={},
                headers=_csrf(security, PLATFORM_CSRF_COOKIE),
            )
            assert platform_approval.status_code == 200, platform_approval.text
            assert platform_approval.json()["grant"]["missing_approvals"] == ["workspace"]

        switched = await owner.post(
            "/identity/workspace-switches",
            json={"workspace_id": workspace_id},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert switched.status_code == 200, switched.text
        workspace_approval = await owner.post(
            f"/workspace/support-grants/{grant_id}/approve",
            json={},
            headers=_csrf(owner, WORKSPACE_CSRF_COOKIE),
        )
        assert workspace_approval.status_code == 200, workspace_approval.text
        assert workspace_approval.json()["grant"]["status"] == "active"

        support = await owner.post(
            f"/platform/support-grants/{grant_id}/sessions",
            json={},
            headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
        )
        assert support.status_code == 200, support.text
        assert owner.cookies.get(SUPPORT_SESSION_COOKIE)
        support_tokens = [owner.cookies.get(SUPPORT_SESSION_COOKIE)]
        for _ in range(3):
            issued = await owner.post(
                f"/platform/support-grants/{grant_id}/sessions",
                json={},
                headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
            )
            assert issued.status_code == 200, issued.text
            support_tokens.append(owner.cookies.get(SUPPORT_SESSION_COOKIE))
        assert all(support_tokens)
        diagnostics = await owner.get("/support/workspace/diagnostics")
        assert diagnostics.status_code == 200, diagnostics.text
        assert set(diagnostics.json()) == {
            "workspace_id",
            "operational_status",
            "region_code",
            "provisioning_status",
            "health_band",
            "active_memberships",
            "request_id",
            "trace_id",
        }
        assert diagnostics.json()["workspace_id"] == workspace_id
        assert "email" not in diagnostics.text.lower()
        assert "token" not in diagnostics.text.lower()

        connection = connect_database(database_url)
        try:
            owner_account_id = connection.execute(
                "SELECT requester.account_id FROM support_grants grant_record "
                "JOIN platform_operators requester "
                "ON requester.operator_id=grant_record.requester_operator_id "
                "WHERE grant_record.grant_id=?",
                (grant_id,),
            ).fetchone()[0]
            security_factor = connection.execute(
                "SELECT factor.factor_id,factor.revision FROM account_mfa_factors factor "
                "JOIN global_accounts account ON account.account_id=factor.account_id "
                "WHERE account.email_normalized=? AND factor.status='active'",
                (SECOND_EMAIL,),
            ).fetchone()
            support_session_ids = [
                connection.execute(
                    "SELECT support_session_id FROM support_sessions WHERE token_hmac=?",
                    (platform_service._support_hmac(app.state.config, token),),
                ).fetchone()[0]
                for token in support_tokens
            ]

            connection.execute(
                "UPDATE global_accounts SET status='disabled' WHERE account_id=?",
                (owner_account_id,),
            )
            connection.commit()
            async with AsyncClient(transport=transport, base_url="https://testserver") as client:
                client.cookies.set(SUPPORT_SESSION_COOKIE, support_tokens[0])
                invalid_status = await client.get("/support/session")
            assert invalid_status.status_code == 401
            assert invalid_status.json()["code"] == "support.session_invalid"
            assert (
                connection.execute(
                    "SELECT status FROM support_sessions WHERE support_session_id=?",
                    (support_session_ids[0],),
                ).fetchone()[0]
                == "revoked"
            )
            connection.execute(
                "UPDATE global_accounts SET status='active' WHERE account_id=?",
                (owner_account_id,),
            )
            connection.commit()

            connection.execute(
                "UPDATE global_accounts SET login_enabled=0 WHERE account_id=?",
                (owner_account_id,),
            )
            connection.commit()
            async with AsyncClient(transport=transport, base_url="https://testserver") as client:
                client.cookies.set(SUPPORT_SESSION_COOKIE, support_tokens[1])
                invalid_login = await client.get("/support/session")
            assert invalid_login.status_code == 401
            assert invalid_login.json()["code"] == "support.session_invalid"
            assert (
                connection.execute(
                    "SELECT status FROM support_sessions WHERE support_session_id=?",
                    (support_session_ids[1],),
                ).fetchone()[0]
                == "revoked"
            )
            connection.execute(
                "UPDATE global_accounts SET login_enabled=1 WHERE account_id=?",
                (owner_account_id,),
            )
            connection.commit()

            connection.execute(
                "UPDATE support_sessions SET factor_id=?,factor_revision=? "
                "WHERE support_session_id=?",
                (security_factor["factor_id"], security_factor["revision"], support_session_ids[2]),
            )
            connection.commit()
            async with AsyncClient(transport=transport, base_url="https://testserver") as client:
                client.cookies.set(SUPPORT_SESSION_COOKIE, support_tokens[2])
                invalid_factor = await client.get("/support/session")
            assert invalid_factor.status_code == 401
            assert invalid_factor.json()["code"] == "support.session_invalid"
            assert (
                connection.execute(
                    "SELECT status FROM support_sessions WHERE support_session_id=?",
                    (support_session_ids[2],),
                ).fetchone()[0]
                == "revoked"
            )

            approval = connection.execute(
                "SELECT approval_id,authority_revision FROM support_grant_approvals "
                "WHERE grant_id=? AND approval_domain='platform'",
                (grant_id,),
            ).fetchone()
            connection.execute(
                "UPDATE support_grant_approvals SET authority_revision=? WHERE approval_id=?",
                (int(approval["authority_revision"]) + 1, approval["approval_id"]),
            )
            connection.commit()
            async with AsyncClient(transport=transport, base_url="https://testserver") as client:
                client.cookies.set(SUPPORT_SESSION_COOKIE, support_tokens[3])
                invalid_grant = await client.get("/support/session")
            assert invalid_grant.status_code == 401
            assert invalid_grant.json()["code"] == "support.session_invalid"
            assert (
                connection.execute(
                    "SELECT status FROM support_sessions WHERE support_session_id=?",
                    (support_session_ids[3],),
                ).fetchone()[0]
                == "revoked"
            )
            connection.execute(
                "UPDATE support_grant_approvals SET authority_revision=? WHERE approval_id=?",
                (approval["authority_revision"], approval["approval_id"]),
            )
            connection.commit()
        finally:
            connection.close()

        conflict = await owner.get(
            "/platform/session", headers={"Authorization": "Bearer local-test-value"}
        )
        assert conflict.status_code == 400
        assert conflict.json()["code"] == "platform.credential_conflict"

        revoked = await owner.post(
            f"/workspace/support-grants/{grant_id}/revoke",
            json={"reason_code": "diagnostics_complete"},
            headers=_csrf(owner, WORKSPACE_CSRF_COOKIE),
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["grant"]["status"] == "revoked"
        revoked_session = await owner.get("/support/session")
        assert revoked_session.status_code == 401

        clock[0] += timedelta(seconds=30)
        emergency = await owner.post(
            "/platform/break-glass-sessions",
            json={
                "workspace_id": workspace_id,
                "reason_code": "incident_containment",
                "totp_code": _totp(owner_secret, clock[0]),
                "confirmed": True,
            },
            headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
        )
        assert emergency.status_code == 201, emergency.text
        break_glass_id = emergency.json()["break_glass_id"]
        assert emergency.json()["session"]["source_kind"] == "break_glass"
        assert (await owner.get("/support/workspace/diagnostics")).status_code == 200

        clock[0] += timedelta(minutes=10, seconds=1)
        expired_emergency = await owner.get("/support/session")
        assert expired_emergency.status_code == 401
        assert expired_emergency.json()["code"] == "support.session_expired"

        async with AsyncClient(transport=transport, base_url="https://testserver") as reviewer:
            await _login(reviewer, SECOND_EMAIL, SECOND_PASSWORD)
            reviewer_session = await reviewer.post(
                "/platform/sessions",
                json={"totp_code": _totp(security_secret, clock[0])},
                headers=_csrf(reviewer, GLOBAL_CSRF_COOKIE),
            )
            assert reviewer_session.status_code == 200, reviewer_session.text
            platform_review = await reviewer.post(
                f"/platform/break-glass-sessions/{break_glass_id}/reviews",
                json={},
                headers=_csrf(reviewer, PLATFORM_CSRF_COOKIE),
            )
            assert platform_review.status_code == 200, platform_review.text
            assert platform_review.json()["missing_reviews"] == ["workspace"]

        workspace_review = await owner.post(
            f"/workspace/break-glass-sessions/{break_glass_id}/reviews",
            json={},
            headers=_csrf(owner, WORKSPACE_CSRF_COOKIE),
        )
        assert workspace_review.status_code == 200, workspace_review.text
        assert workspace_review.json()["status"] == "reviewed"
        assert workspace_review.json()["missing_reviews"] == []

    connection = connect_database(database_url)
    try:
        ciphertext = connection.execute(
            "SELECT secret_ciphertext FROM account_mfa_factors ORDER BY created_at LIMIT 1"
        ).fetchone()[0]
        assert owner_secret not in ciphertext
        audit_blob = "\n".join(
            str(tuple(row)) for row in connection.execute("SELECT * FROM platform_audit_logs")
        )
        assert owner_secret not in audit_blob
        assert RECOVERY_SECRET not in audit_blob
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM platform_audit_logs
                WHERE action_code='support.access_approved'
                """
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM platform_audit_logs WHERE action_code='break_glass.used'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM platform_audit_logs
                WHERE action_code='support.session_issued'
                """
            ).fetchone()[0]
            == 4
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM platform_event_outbox "
                "WHERE event_type='support.access_granted'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """
                SELECT priority FROM platform_event_outbox
                WHERE event_type='break_glass.used'
                """
            ).fetchone()[0]
            == "high"
        )
        assert connection.execute("SELECT COUNT(*) FROM break_glass_reviews").fetchone()[0] == 2
    finally:
        connection.close()


@pytest.mark.anyio
async def test_platform_concurrency_cursor_scope_and_warm_performance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(identity_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(platform_service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)

    async with (
        AsyncClient(transport=transport, base_url="https://testserver") as owner,
        AsyncClient(transport=transport, base_url="https://testserver") as security,
    ):
        await _login(owner, ADMIN_EMAIL, ADMIN_PASSWORD)
        workspace_id = (await owner.get("/identity/workspaces")).json()["workspaces"][0][
            "workspace_id"
        ]
        owner_secret = await _enroll(owner, ADMIN_PASSWORD, clock[0])
        clock[0] += timedelta(seconds=30)
        bootstrapped = await owner.post(
            "/platform/bootstrap",
            json={
                "current_password": ADMIN_PASSWORD,
                "totp_code": _totp(owner_secret, clock[0]),
                "recovery_secret": RECOVERY_SECRET,
                "reason_code": "initial_platform_owner",
                "confirmed": True,
            },
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert bootstrapped.status_code == 200
        clock[0] += timedelta(seconds=30)
        owner_session = await owner.post(
            "/platform/sessions",
            json={"totp_code": _totp(owner_secret, clock[0])},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert owner_session.status_code == 200

        await _register(security)
        security_secret = await _enroll(security, SECOND_PASSWORD, clock[0])
        connection = connect_database(database_url)
        try:
            security_account_id = connection.execute(
                "SELECT account_id FROM global_accounts WHERE email_normalized=?",
                (SECOND_EMAIL,),
            ).fetchone()[0]
        finally:
            connection.close()
        created_operator = await owner.post(
            "/platform/operators",
            json={"account_id": security_account_id, "roles": ["platform_security"]},
            headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
        )
        assert created_operator.status_code == 201
        security_operator_id = created_operator.json()["operator"]["operator_id"]
        clock[0] += timedelta(seconds=30)
        security_session = await security.post(
            "/platform/sessions",
            json={"totp_code": _totp(security_secret, clock[0])},
            headers=_csrf(security, GLOBAL_CSRF_COOKIE),
        )
        assert security_session.status_code == 200

        grant = await owner.post(
            "/platform/support-grants",
            json={
                "workspace_id": workspace_id,
                "reason_code": "workspace_diagnostics",
                "duration_minutes": 30,
            },
            headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
        )
        assert grant.status_code == 201
        grant_id = grant.json()["grant"]["grant_id"]

        platform_approvals = await asyncio.gather(
            *(
                security.post(
                    f"/platform/support-grants/{grant_id}/approve",
                    json={},
                    headers=_csrf(security, PLATFORM_CSRF_COOKIE),
                )
                for _ in range(100)
            )
        )
        assert [response.status_code for response in platform_approvals].count(200) == 1
        assert [response.status_code for response in platform_approvals].count(409) == 99

        switched = await owner.post(
            "/identity/workspace-switches",
            json={"workspace_id": workspace_id},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert switched.status_code == 200
        workspace_approvals = await asyncio.gather(
            *(
                owner.post(
                    f"/workspace/support-grants/{grant_id}/approve",
                    json={},
                    headers=_csrf(owner, WORKSPACE_CSRF_COOKIE),
                )
                for _ in range(100)
            )
        )
        assert [response.status_code for response in workspace_approvals].count(200) == 1
        assert [response.status_code for response in workspace_approvals].count(409) == 99

        session_durations: list[float] = []
        operator_durations: list[float] = []
        grant_durations: list[float] = []
        for _ in range(5):
            assert (await owner.get("/platform/session")).status_code == 200
            assert (await owner.get("/platform/operators?limit=100&offset=0")).status_code == 200
            assert (
                await owner.get("/platform/support-grants?limit=100&offset=0")
            ).status_code == 200
        for _ in range(40):
            started = time.perf_counter()
            assert (await owner.get("/platform/session")).status_code == 200
            session_durations.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            assert (await owner.get("/platform/operators?limit=100&offset=0")).status_code == 200
            operator_durations.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            assert (
                await owner.get("/platform/support-grants?limit=100&offset=0")
            ).status_code == 200
            grant_durations.append((time.perf_counter() - started) * 1000)
        assert _p95(session_durations) < 50
        assert _p95(operator_durations) < 300
        assert _p95(grant_durations) < 300

        issued_sessions = await asyncio.gather(
            *(
                owner.post(
                    f"/platform/support-grants/{grant_id}/sessions",
                    json={},
                    headers=_csrf(owner, PLATFORM_CSRF_COOKIE),
                )
                for _ in range(100)
            )
        )
        assert all(response.status_code == 200 for response in issued_sessions)
        connection = connect_database(database_url)
        try:
            assert (
                connection.execute(
                    """
                    SELECT COUNT(*) FROM support_sessions
                    WHERE source_kind='grant' AND source_id=?
                    """,
                    (grant_id,),
                ).fetchone()[0]
                == 100
            )
            assert (
                connection.execute(
                    "SELECT COUNT(DISTINCT token_hmac) FROM support_sessions WHERE source_id=?",
                    (grant_id,),
                ).fetchone()[0]
                == 100
            )
        finally:
            connection.close()

        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                INSERT INTO platform_audit_logs(
                  audit_id,actor_domain,actor_id,action_code,object_type,object_id,
                  reason_code,request_id,trace_id,before_sha256,after_sha256,occurred_at)
                VALUES(?, 'platform', ?, 'billing.subscription_changed', 'subscription', ?,
                  'billing_test', 'req_scope_test', 'trc_scope_test', NULL, NULL, ?)
                """,
                (
                    "pau_" + "f" * 32,
                    security_operator_id,
                    "sub_" + "f" * 32,
                    clock[0].isoformat().replace("+00:00", "Z"),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        security_audit = await security.get("/platform/audit-logs", params={"limit": 100})
        assert security_audit.status_code == 200
        assert all(
            entry["action"] != "billing.subscription_changed"
            for entry in security_audit.json()["entries"]
        )
        owner_audit = await owner.get("/platform/audit-logs", params={"limit": 100})
        assert owner_audit.status_code == 200
        assert any(
            entry["action"] == "billing.subscription_changed"
            for entry in owner_audit.json()["entries"]
        )

        changed_role = await owner.patch(
            f"/platform/operators/{security_operator_id}",
            json={"status": "active", "roles": ["platform_support"]},
            headers={**_csrf(owner, PLATFORM_CSRF_COOKIE), "If-Match": '"1"'},
        )
        assert changed_role.status_code == 200
        clock[0] += timedelta(seconds=30)
        reopened = await security.post(
            "/platform/sessions",
            json={"totp_code": _totp(security_secret, clock[0])},
            headers=_csrf(security, GLOBAL_CSRF_COOKIE),
        )
        assert reopened.status_code == 200
        own_grant_ids: set[str] = set()
        for reason_code in ("workspace_diagnostics", "incident_response"):
            own_grant = await security.post(
                "/platform/support-grants",
                json={
                    "workspace_id": workspace_id,
                    "reason_code": reason_code,
                    "duration_minutes": 15,
                },
                headers=_csrf(security, PLATFORM_CSRF_COOKIE),
            )
            assert own_grant.status_code == 201
            own_grant_ids.add(own_grant.json()["grant"]["grant_id"])

        first_page = await security.get("/platform/audit-logs", params={"limit": 1})
        assert first_page.status_code == 200
        first_body = first_page.json()
        assert first_body["total"] == 2
        assert first_body["entries"][0]["object_id"] in own_grant_ids
        cursor = first_body["next_cursor"]
        assert cursor
        second_page = await security.get(
            "/platform/audit-logs", params={"limit": 1, "cursor": cursor}
        )
        assert second_page.status_code == 200
        visible_ids = {
            first_body["entries"][0]["object_id"],
            second_page.json()["entries"][0]["object_id"],
        }
        assert visible_ids == own_grant_ids
        assert second_page.json()["next_cursor"] is None

        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        assert (
            await security.get("/platform/audit-logs", params={"limit": 1, "cursor": tampered})
        ).status_code == 422
        assert (
            await owner.get("/platform/audit-logs", params={"limit": 1, "cursor": cursor})
        ).status_code == 422
        assert (await owner.get("/platform/audit-logs?limit=1&offset=0")).status_code == 422
