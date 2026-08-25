"""PRD-116 identity security, step-up, and conditional-access acceptance."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import shutil
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import metahub_control.identity.service as identity_service
import metahub_control.workspace_access.conditional_access as conditional_service
import metahub_control.workspace_access.service as workspace_service
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.delivery import (
    clear_test_identity_challenges,
    pop_test_identity_challenge,
)
from metahub_control.identity.routes import router as identity_router
from metahub_control.workspace_access.constants import WORKSPACE_CSRF_COOKIE
from metahub_control.workspace_access.routes import router as workspace_access_router
from metahub_control.workspaces.routes import router as workspaces_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"
GLOBAL_SESSION_COOKIE = "__Host-metahub-global-session"
SEED_PATH = Path(__file__).resolve().parents[3] / "infra" / "seeds" / "default_seed.json"


def _local_seed_admin() -> tuple[str, str]:
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    admin = seed["admin"]
    return str(admin["email"]), str(admin["password"])


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd116-api-migrated") / "identity-security.sqlite3"
    assert apply_migrations(f"sqlite:///{database.as_posix()}").applied[-1] == "0108"
    return database


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
):
    clear_test_identity_challenges()
    _, admin_password = _local_seed_admin()
    owner_email = f"owner-{secrets.token_hex(8)}@example.com"
    database = tmp_path / "identity-security.sqlite3"
    shutil.copy2(migrated_database, database)
    database_url = f"sqlite:///{database.as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": admin_password,
        "METAHUB_AUTH_TOKEN_SECRET": secrets.token_urlsafe(32),
        "METAHUB_IDENTITY_SESSION_SECRET": secrets.token_urlsafe(32),
        "METAHUB_IDENTITY_CHALLENGE_SECRET": secrets.token_urlsafe(32),
        "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "test_capture",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": secrets.token_urlsafe(32),
        "METAHUB_WORKSPACE_SESSION_SECRET": secrets.token_urlsafe(32),
        "METAHUB_PLATFORM_SESSION_SECRET": secrets.token_urlsafe(32),
        "METAHUB_SUPPORT_SESSION_SECRET": secrets.token_urlsafe(32),
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": secrets.token_urlsafe(32),
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": secrets.token_urlsafe(32),
        "METAHUB_ENTERPRISE_SSO_HMAC_SECRET": secrets.token_urlsafe(32),
        "METAHUB_ENTERPRISE_SSO_ENCRYPTION_SECRET": secrets.token_urlsafe(32),
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET": secrets.token_urlsafe(32),
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
        "METAHUB_CONTROL_API_BASE_URL": "https://api.example.com",
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    apply_seed_data(database_url)
    monkeypatch.delenv("METAHUB_SEED_ADMIN_PASSWORD")
    with connect_database(database_url) as connection:
        connection.execute(
            "UPDATE global_accounts SET email_normalized=?,updated_at=datetime('now')",
            (owner_email,),
        )
        connection.commit()
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(identity_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(workspace_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(conditional_service, "_utcnow", lambda: clock[0])
    app = create_service_app(
        "control-plane-api",
        "PRD-116 Identity Security Test",
        extra_cors_allow_headers=("X-CSRF-Token", "X-Step-Up-Grant"),
        cors_allow_credentials=True,
    )
    app.include_router(identity_router)
    app.include_router(workspaces_router)
    app.include_router(workspace_access_router)
    return database_url, app, owner_email, clock


def _headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site", **extra}


def _csrf(client: AsyncClient, name: str, **extra: str) -> dict[str, str]:
    token = client.cookies.get(name)
    assert token
    return _headers(**{"X-CSRF-Token": token, **extra})


async def _login(client: AsyncClient, email: str) -> None:
    _, password = _local_seed_admin()
    credentials = {"email": email, "password": password}
    response = await client.post("/identity/login", json=credentials, headers=_headers())
    credentials.clear()
    password = ""
    assert response.status_code == 200


def _totp(secret: str, moment: datetime) -> str:
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    digest = hmac.new(
        key,
        struct.pack(">Q", int(moment.timestamp()) // 30),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 15
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


@pytest.mark.anyio
async def test_prd116_production_cors_allows_bound_step_up_grant() -> None:
    from metahub_control.app import app as production_app

    async with AsyncClient(
        transport=ASGITransport(app=production_app),
        base_url="http://control.local",
    ) as client:
        response = await client.options(
            f"/workspaces/wsp_{'1' * 32}/conditional-access",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": (
                    "content-type,x-csrf-token,x-step-up-grant"
                ),
            },
        )

    assert response.status_code == 200
    allowed = {
        value.strip().lower()
        for value in response.headers["access-control-allow-headers"].split(",")
    }
    assert {"content-type", "x-csrf-token", "x-step-up-grant"}.issubset(allowed)
    assert response.headers["access-control-allow-credentials"] == "true"


async def _switch(client: AsyncClient, workspace_id: str):
    return await client.post(
        "/identity/workspace-switches",
        json={"workspace_id": workspace_id},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )


async def _workspace_step_up(
    client: AsyncClient,
    *,
    workspace_id: str,
    secret: str,
    clock: list[datetime],
) -> str:
    clock[0] += timedelta(seconds=30)
    response = await client.post(
        "/identity/step-up",
        json={
            "method": "totp",
            "workspace_id": workspace_id,
            "action_code": "conditional_access.manage",
            "resource_type": "conditional_access_policy",
            "resource_id": workspace_id,
            "code": _totp(secret, clock[0]),
        },
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert response.status_code == 200
    return str(response.json()["grant"])


@pytest.mark.anyio
async def test_identity_security_lifecycle_and_conditional_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app, owner_email, clock = _prepare(
        monkeypatch, tmp_path, migrated_database
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        await _login(owner, owner_email)
        _, password = _local_seed_admin()
        enrollment_request = {"method": "totp", "current_password": password}
        enrollment = await owner.post(
            "/identity/mfa/enrollments",
            json=enrollment_request,
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        enrollment_request.clear()
        password = ""
        assert enrollment.status_code == 200
        enrollment_body = enrollment.json()
        factor_id = enrollment_body["factor_id"]
        totp_secret = enrollment_body["secret"]
        activated = await owner.post(
            "/identity/mfa/totp/verifications",
            json={"factor_id": factor_id, "code": _totp(totp_secret, clock[0])},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert activated.status_code == 200

        _, password = _local_seed_admin()
        webauthn_request = {"method": "webauthn", "current_password": password}
        webauthn = await owner.post(
            "/identity/mfa/enrollments",
            json=webauthn_request,
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        webauthn_request.clear()
        password = ""
        assert webauthn.status_code == 200
        webauthn_body = webauthn.json()
        assert webauthn_body["public_key_options"]["rp"]["id"] == "localhost"
        authenticator_selection = webauthn_body["public_key_options"][
            "authenticatorSelection"
        ]
        assert authenticator_selection["residentKey"] == "required"
        assert authenticator_selection["requireResidentKey"] is True
        with connect_database(database_url) as connection:
            challenge = connection.execute(
                "SELECT * FROM mfa_challenges WHERE mfa_challenge_id=?",
                (webauthn_body["challenge_id"],),
            ).fetchone()
            assert challenge is not None
            assert len(challenge["challenge_hmac"]) == 64
            assert "challenge" not in challenge.keys()

        _, password = _local_seed_admin()
        recovery_request = {"method": "recovery_codes", "current_password": password}
        recovery = await owner.post(
            "/identity/mfa/enrollments",
            json=recovery_request,
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        recovery_request.clear()
        password = ""
        assert recovery.status_code == 200
        codes = tuple(recovery.json()["recovery_codes"])
        assert len(codes) == 10
        recovery_completion_code = codes[1]
        security = await owner.get("/identity/security")
        assert security.status_code == 200
        security_json = security.json()
        serialized = json.dumps(security_json, ensure_ascii=True)
        assert all(
            marker not in serialized
            for marker in ("code_hash", "device_hmac", "public_key")
        )
        assert security_json["recovery_code_set"]["remaining_count"] == 10
        assert security_json["sessions"][0]["assurance_level"] == "mfa"

        recovery_step_up = await owner.post(
            "/identity/step-up",
            json={
                "method": "recovery_code",
                "action_code": "identity.session.revoke",
                "resource_type": "global_session",
                "resource_id": security_json["sessions"][0]["session_id"],
                "code": codes[0],
            },
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert recovery_step_up.status_code == 200
        replay = await owner.post(
            "/identity/step-up",
            json={
                "method": "recovery_code",
                "action_code": "identity.session.revoke",
                "resource_type": "global_session",
                "resource_id": security_json["sessions"][0]["session_id"],
                "code": codes[0],
            },
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert replay.status_code == 400
        codes = ()

        clock[0] += timedelta(seconds=30)
        trusted = await owner.post(
            "/identity/trusted-devices",
            json={"label": "Work browser", "totp_code": _totp(totp_secret, clock[0])},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert trusted.status_code == 200
        trusted_id = trusted.json()["trusted_device"]["trusted_device_id"]

        workspaces = await owner.get("/identity/workspaces")
        workspace_id = workspaces.json()["workspaces"][0]["workspace_id"]
        switched = await _switch(owner, workspace_id)
        assert switched.status_code == 200
        first_grant = await _workspace_step_up(
            owner, workspace_id=workspace_id, secret=totp_secret, clock=clock
        )
        policy_body = {
            "expected_revision": 0,
            "mode": "audit",
            "require_mfa": True,
            "require_step_up_for_high_risk": True,
            "allow_trusted_devices": False,
            "region_rule": "any",
            "network_rule": "any",
            "allowed_cidrs": [],
            "exception_membership_ids": [],
        }
        created = await owner.patch(
            f"/workspaces/{workspace_id}/conditional-access",
            json=policy_body,
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{"X-Step-Up-Grant": first_grant},
            ),
        )
        first_grant = ""
        assert created.status_code == 200
        assert created.json()["policy"]["revision"] == 1
        stale_policy_read = await owner.get(
            f"/workspaces/{workspace_id}/conditional-access"
        )
        assert stale_policy_read.status_code == 401
        assert stale_policy_read.json()["code"] == "workspace_session.revoked"

        switched = await _switch(owner, workspace_id)
        assert switched.status_code == 200
        simulated = await owner.post(
            f"/workspaces/{workspace_id}/conditional-access/simulations",
            json={},
            headers=_csrf(owner, WORKSPACE_CSRF_COOKIE),
        )
        assert simulated.status_code == 200
        assert simulated.json()["simulation"]["outcome_code"] == "simulation.ready"
        second_grant = await _workspace_step_up(
            owner, workspace_id=workspace_id, secret=totp_secret, clock=clock
        )
        policy_body["expected_revision"] = 1
        policy_body["mode"] = "enforced"
        enforced = await owner.patch(
            f"/workspaces/{workspace_id}/conditional-access",
            json=policy_body,
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{"X-Step-Up-Grant": second_grant},
            ),
        )
        second_grant = ""
        assert enforced.status_code == 200
        assert enforced.json()["policy"]["mode"] == "enforced"

        switched = await _switch(owner, workspace_id)
        assert switched.status_code == 200
        revoked_device = await owner.delete(
            f"/identity/trusted-devices/{trusted_id}",
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert revoked_device.status_code == 200

        async with AsyncClient(transport=transport, base_url="https://testserver") as password_only:
            await _login(password_only, owner_email)
            denied = await _switch(password_only, workspace_id)
            assert denied.status_code == 403
            assert denied.json()["code"] == "conditional_access.denied"

            _, password = _local_seed_admin()
            for method in ("totp", "webauthn"):
                bypass_request = {"method": method, "current_password": password}
                bypass = await password_only.post(
                    "/identity/mfa/enrollments",
                    json=bypass_request,
                    headers=_csrf(password_only, GLOBAL_CSRF_COOKIE),
                )
                bypass_request.clear()
                assert bypass.status_code == 403
                assert bypass.json()["code"] == "identity.mfa_required"
            password = ""
            stale_session_token = password_only.cookies.get(GLOBAL_SESSION_COOKIE)
            assert stale_session_token

        with connect_database(database_url) as connection:
            assert connection.execute(
                "SELECT consumed_count FROM recovery_code_sets WHERE account_id=("
                "SELECT account_id FROM global_accounts WHERE email_normalized=?)",
                (owner_email,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM step_up_grants WHERE status='active'"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM identity_security_events"
            ).fetchone()[0] >= 7

        _, password = _local_seed_admin()
        recovery_begin_request = {
            "operation": "begin_recovery",
            "current_password": password,
        }
        recovery_begin = await owner.post(
            "/identity/mfa/challenges",
            json=recovery_begin_request,
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        recovery_begin_request.clear()
        password = ""
        assert recovery_begin.status_code == 200
        assert any(
            "Max-Age=172800" in value
            for value in recovery_begin.headers.get_list("set-cookie")
        )
        recovery_challenge = recovery_begin.json()
        assert recovery_challenge["purpose"] == "recovery"
        assert recovery_challenge["ready_at"] < recovery_challenge["expires_at"]
        with connect_database(database_url) as connection:
            extended = connection.execute(
                """SELECT session.expires_at,session.idle_expires_at
                     FROM global_sessions session JOIN mfa_challenges challenge
                       ON challenge.global_session_id=session.session_id
                    WHERE challenge.mfa_challenge_id=?""",
                (recovery_challenge["challenge_id"],),
            ).fetchone()
            assert extended is not None
            response_expiry = datetime.fromisoformat(
                recovery_challenge["expires_at"].replace("Z", "+00:00")
            )
            assert datetime.fromisoformat(
                extended["expires_at"].replace("Z", "+00:00")
            ) == response_expiry
            assert datetime.fromisoformat(
                extended["idle_expires_at"].replace("Z", "+00:00")
            ) == response_expiry
        delivered = pop_test_identity_challenge(
            "mfa_recovery", recovery_challenge["challenge_id"]
        )
        assert delivered is not None

        password_only_completion = await owner.post(
            "/identity/mfa/challenges",
            json={
                "operation": "complete_recovery",
                "challenge_id": recovery_challenge["challenge_id"],
                "current_password": "not-a-completion-proof",
            },
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert password_only_completion.status_code == 422
        assert password_only_completion.json()["code"] == "identity.request_invalid"

        recovery_proof = {
            "operation": "complete_recovery",
            "challenge_id": recovery_challenge["challenge_id"],
            "notification_code": delivered.code,
            "recovery_code": recovery_completion_code,
        }
        too_early = await owner.post(
            "/identity/mfa/challenges",
            json=recovery_proof,
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert too_early.status_code == 409
        assert too_early.json()["code"] == "identity.recovery_not_ready"

        clock[0] += timedelta(hours=24)
        recovered = await owner.post(
            "/identity/mfa/challenges",
            json=recovery_proof,
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        recovery_proof.clear()
        recovery_completion_code = ""
        assert recovered.status_code == 200
        assert recovered.json()["assurance_level"] == "recovery"
        assert owner.cookies.get(WORKSPACE_CSRF_COOKIE) is None

        security_after_recovery = await owner.get("/identity/security")
        assert security_after_recovery.status_code == 200
        recovered_security = security_after_recovery.json()
        assert recovered_security["recovery_challenge"] is None
        assert recovered_security["recovery_code_set"]["status"] == "revoked"
        assert not any(
            factor["status"] == "active" for factor in recovered_security["factors"]
        )
        assert recovered_security["sessions"][0]["assurance_level"] == "recovery"
        assert recovered_security["sessions"][0]["auth_method"] == "recovery_code"

        async with AsyncClient(
            transport=transport,
            base_url="https://testserver",
            cookies={GLOBAL_SESSION_COOKIE: stale_session_token},
        ) as stale:
            stale_read = await stale.get("/identity/security")
            assert stale_read.status_code == 401
            assert stale_read.json()["code"] == "identity.session_revoked"
        stale_session_token = ""

        _, password = _local_seed_admin()
        replacement_request = {"method": "totp", "current_password": password}
        replacement = await owner.post(
            "/identity/mfa/enrollments",
            json=replacement_request,
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        replacement_request.clear()
        password = ""
        assert replacement.status_code == 200
        replacement_body = replacement.json()
        replacement_secret = replacement_body["secret"]
        replacement_verified = await owner.post(
            "/identity/mfa/totp/verifications",
            json={
                "factor_id": replacement_body["factor_id"],
                "code": _totp(replacement_secret, clock[0]),
            },
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        replacement_secret = ""
        assert replacement_verified.status_code == 200

        with connect_database(database_url) as connection:
            account_id = connection.execute(
                "SELECT account_id FROM global_accounts WHERE email_normalized=?",
                (owner_email,),
            ).fetchone()[0]
            assert connection.execute(
                "SELECT COUNT(*) FROM global_sessions WHERE account_id=? AND status='active'",
                (account_id,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM workspace_sessions WHERE account_id=? AND status='active'",
                (account_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM trusted_devices WHERE account_id=? AND status='active'",
                (account_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM step_up_grants WHERE account_id=? AND status='active'",
                (account_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                """SELECT COUNT(*) FROM identity_security_events event
                     JOIN identity_security_event_outbox outbox
                       ON outbox.identity_security_event_id=event.identity_security_event_id
                    WHERE event.account_id=? AND event.reason_code='recovery_completed'
                      AND event.payload_json NOT LIKE '%notification%'
                      AND event.payload_json NOT LIKE '%recovery_code%'""",
                (account_id,),
            ).fetchone()[0] == 1
        totp_secret = ""
