"""PRD-114 browser API, federation callback, and enforced-login contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import shutil
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
import metahub_control.enterprise_identity.service as enterprise_service
import metahub_control.identity.service as identity_service
import metahub_control.workspace_access.service as workspace_service
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from metahub_control.enterprise_identity.routes import router as enterprise_identity_router
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
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "LocalAdmin!2026"
MEMBER_EMAIL = "federated.member@example.com"
MEMBER_PASSWORD = "FederatedMember!2026"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"
SSO_BROWSER_COOKIE = "__Host-metahub-sso-browser"
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd114-api-migrated") / "enterprise-current.sqlite3"
    assert apply_migrations(f"sqlite:///{database.as_posix()}").applied[-1] == "0111"
    return database


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
):
    database = tmp_path / "enterprise.sqlite3"
    shutil.copy2(migrated_database, database)
    database_url = f"sqlite:///{database.as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_AUTH_TOKEN_SECRET": "test-prd114-legacy-auth-secret-0000000001",
        "METAHUB_IDENTITY_SESSION_SECRET": "test-prd114-global-session-secret-000000001",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-prd114-challenge-secret-000000000001",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": "test-prd114-pii-secret-00000000000000001",
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-prd114-workspace-secret-0000000000001",
        "METAHUB_PLATFORM_SESSION_SECRET": "test-prd114-platform-secret-00000000000001",
        "METAHUB_SUPPORT_SESSION_SECRET": "test-prd114-support-secret-000000000000001",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "test-prd114-mfa-secret-00000000000000001",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": (
            "test-prd114-platform-recovery-secret-0000001"
        ),
        "METAHUB_ENTERPRISE_SSO_HMAC_SECRET": "test-prd114-sso-hmac-secret-00000000000001",
        "METAHUB_ENTERPRISE_SSO_ENCRYPTION_SECRET": (
            "test-prd114-sso-encryption-secret-00000001"
        ),
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET": "test-prd114-cache-secret-0000000000000001",
        "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "test_capture",
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
        "METAHUB_CONTROL_API_BASE_URL": "https://api.example.com",
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    apply_seed_data(database_url)
    clear_test_identity_challenges()
    with connect_database(database_url) as connection:
        connection.execute(
            "UPDATE global_accounts SET email_normalized=?,updated_at=datetime('now')",
            (ADMIN_EMAIL,),
        )
        connection.commit()
    clock = [NOW]
    monkeypatch.setattr(identity_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(enterprise_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(workspace_service, "_utcnow", lambda: clock[0])
    app = create_service_app(
        "control-plane-api",
        "PRD-114 Enterprise Identity Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.include_router(identity_router)
    app.include_router(enterprise_identity_router)
    app.include_router(workspaces_router)
    app.include_router(workspace_access_router)
    return database_url, app, clock


def _headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site", **extra}


def _csrf(client: AsyncClient, name: str, **extra: str) -> dict[str, str]:
    token = client.cookies.get(name)
    assert token
    return _headers(**{"X-CSRF-Token": token, **extra})


def _totp(secret: str, moment: datetime) -> str:
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(moment.timestamp()) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


async def _login(
    client: AsyncClient,
    *,
    email: str = ADMIN_EMAIL,
    password: str = ADMIN_PASSWORD,
    **extra: str,
) -> None:
    response = await client.post(
        "/identity/login",
        json={"email": email, "password": password, **extra},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text


async def _switch(client: AsyncClient, workspace_id: str):
    return await client.post(
        "/identity/workspace-switches",
        json={"workspace_id": workspace_id},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )


async def _register_with_invitation(
    client: AsyncClient,
    *,
    invitation_token: str,
    email: str = MEMBER_EMAIL,
    password: str = MEMBER_PASSWORD,
    display_name: str = "Federated Member",
) -> None:
    started = await client.post(
        "/identity/registrations",
        json={
            "email": email,
            "display_name": display_name,
            "password": password,
            "consent_version": "privacy-2026-08",
            "invitation_token": invitation_token,
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


async def _invite_and_register(
    owner: AsyncClient,
    transport: ASGITransport,
    *,
    email: str,
    role: str,
) -> None:
    invitation = await owner.post(
        "/membership-invitations",
        json={"email": email, "role": role, "expires_in_hours": 24},
        headers=_csrf(owner, WORKSPACE_CSRF_COOKIE),
    )
    assert invitation.status_code == 201, invitation.text
    async with AsyncClient(transport=transport, base_url="https://testserver") as invited:
        await _register_with_invitation(
            invited,
            invitation_token=invitation.json()["accept_token"],
            email=email,
            password=MEMBER_PASSWORD,
            display_name=f"PRD-114 {role}",
        )


def _oidc_material() -> tuple[rsa.RSAPrivateKey, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "prd114-api-key", "alg": "RS256", "use": "sig"})
    return private_key, jwk


@pytest.mark.anyio
async def test_enterprise_sso_oidc_recovery_and_dynamic_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app, clock = _prepare(monkeypatch, tmp_path, migrated_database)
    transport = ASGITransport(app=app)
    private_key, jwk = _oidc_material()

    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        await _login(owner)
        workspaces = await owner.get("/identity/workspaces")
        workspace_id = workspaces.json()["workspaces"][0]["workspace_id"]
        switched = await _switch(owner, workspace_id)
        assert switched.status_code == 200, switched.text

        enrollment = await owner.post(
            "/identity/mfa/totp/enrollments",
            json={"current_password": ADMIN_PASSWORD},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert enrollment.status_code == 200, enrollment.text
        mfa_secret = enrollment.json()["secret"]
        activated = await owner.post(
            "/identity/mfa/totp/verifications",
            json={
                "factor_id": enrollment.json()["factor_id"],
                "code": _totp(mfa_secret, clock[0]),
            },
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert activated.status_code == 200, activated.text

        invitation = await owner.post(
            "/membership-invitations",
            json={"email": MEMBER_EMAIL, "role": "developer", "expires_in_hours": 24},
            headers=_csrf(owner, WORKSPACE_CSRF_COOKIE),
        )
        assert invitation.status_code == 201, invitation.text
        async with AsyncClient(
            transport=transport, base_url="https://testserver"
        ) as invited:
            await _register_with_invitation(
                invited, invitation_token=invitation.json()["accept_token"]
            )

        summary = await owner.get(f"/workspaces/{workspace_id}/enterprise-identity")
        assert summary.status_code == 200 and summary.json()["policy"] is None
        management_headers = _csrf(
            owner,
            WORKSPACE_CSRF_COOKIE,
            **{"Idempotency-Key": "prd114-domain-create-0001"},
        )
        created = await owner.post(
            f"/workspaces/{workspace_id}/domains",
            json={"domain": "example.com"},
            headers=management_headers,
        )
        assert created.status_code == 201, created.text
        domain = created.json()["domain"]
        verification_record = created.json()["verification_record"]
        replay = await owner.post(
            f"/workspaces/{workspace_id}/domains",
            json={"domain": "example.com"},
            headers=management_headers,
        )
        assert replay.status_code == 201 and replay.json()["idempotent_replay"] is True
        assert replay.json()["domain"]["verified_domain_id"] == domain["verified_domain_id"]

        monkeypatch.setattr(
            enterprise_service,
            "_lookup_txt_records",
            lambda candidate: (
                frozenset({verification_record})
                if candidate == "example.com"
                else frozenset()
            ),
        )
        verified = await owner.post(
            f"/workspaces/{workspace_id}/domains/{domain['verified_domain_id']}/verify",
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{
                    "If-Match": '"1"',
                    "Idempotency-Key": "prd114-domain-verify-0001",
                },
            ),
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["domain"]["status"] == "verified"

        connection_payload = {
            "verified_domain_id": domain["verified_domain_id"],
            "connection_code": "corp-oidc",
            "display_name": "Corporate OIDC",
            "protocol": "oidc",
            "issuer": "https://idp.example.com",
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "client_id": "metahub-prd114-client",
            "client_secret": "synthetic-prd114-client-secret",
            "jwks": {"keys": [jwk]},
        }
        created_connection = await owner.post(
            f"/workspaces/{workspace_id}/sso-connections",
            json=connection_payload,
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd114-connection-create-0001"},
            ),
        )
        assert created_connection.status_code == 201, created_connection.text
        connection_id = created_connection.json()["connection"]["sso_connection_id"]
        tested = await owner.post(
            f"/workspaces/{workspace_id}/sso/test",
            json={"sso_connection_id": connection_id},
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{
                    "If-Match": '"1"',
                    "Idempotency-Key": "prd114-connection-test-0001",
                },
            ),
        )
        assert tested.status_code == 200, tested.text
        assert tested.json()["connection"]["status"] == "active"

        with connect_database(database_url) as database:
            account_id = database.execute(
                "SELECT account_id FROM global_accounts WHERE email_normalized=?",
                (ADMIN_EMAIL,),
            ).fetchone()[0]
            member = database.execute(
                """SELECT member.workspace_membership_id,member.account_id
                     FROM workspace_memberships member
                     JOIN global_accounts account ON account.account_id=member.account_id
                     WHERE member.workspace_id=? AND account.email_normalized=?""",
                (workspace_id, MEMBER_EMAIL),
            ).fetchone()
            assert member is not None
            member_membership_id = member["workspace_membership_id"]
            member_account_id = member["account_id"]
            persisted = database.execute(
                "SELECT public_config_json,client_secret_ciphertext FROM sso_connections"
            ).fetchone()
            assert connection_payload["client_secret"] not in persisted["public_config_json"]
            assert connection_payload["client_secret"] not in persisted["client_secret_ciphertext"]

        enforced = await owner.patch(
            f"/workspaces/{workspace_id}/sso-policy",
            json={
                "mode": "enforced",
                "active_connection_id": connection_id,
                "break_glass_account_id": account_id,
            },
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{
                    "If-Match": '"1"',
                    "Idempotency-Key": "prd114-policy-enforce-0001",
                },
            ),
        )
        assert enforced.status_code == 200, enforced.text
        assert enforced.json()["policy"]["mode"] == "enforced"

    async with AsyncClient(transport=transport, base_url="https://testserver") as password_only:
        await _login(password_only)
        blocked = await _switch(password_only, workspace_id)
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "sso.login_required"

    async with AsyncClient(transport=transport, base_url="https://testserver") as browser:
        discovery = await browser.get(f"/identity/login-discovery?email={MEMBER_EMAIL}")
        assert discovery.status_code == 200, discovery.text
        assert discovery.json()["mode"] == "sso"
        authorization = parse_qs(urlsplit(discovery.json()["authorization_url"]).query)
        state = discovery.json()["transaction_id"]
        assert authorization["state"] == [state]
        browser_token = browser.cookies.get(SSO_BROWSER_COOKIE)
        assert browser_token
        id_token = jwt.encode(
            {
                "iss": "https://idp.example.com",
                "sub": "subject-prd114-member",
                "aud": "metahub-prd114-client",
                "azp": "metahub-prd114-client",
                "exp": clock[0] + timedelta(minutes=5),
                "iat": clock[0],
                "nonce": authorization["nonce"][0],
                "email": MEMBER_EMAIL,
                "email_verified": True,
                "jti": "assertion-prd114-api-0001",
            },
            private_key,
            algorithm="RS256",
            headers={"kid": jwk["kid"]},
        )
        monkeypatch.setattr(enterprise_service, "exchange_oidc_code", lambda **kwargs: id_token)
        callback = await browser.post(
            "/identity/sso/callback",
            json={"protocol": "oidc", "state": state, "code": "code-prd114-valid"},
            headers=_headers(),
        )
        assert callback.status_code == 200, callback.text
        assert callback.json()["workspace_id"] == workspace_id
        assert (await browser.get("/workspace/session")).status_code == 200

        async with AsyncClient(
            transport=transport, base_url="https://testserver"
        ) as replay_browser:
            replay_browser.cookies.set(
                SSO_BROWSER_COOKIE,
                browser_token,
                domain="testserver.local",
                path="/",
            )
            replayed_callback = await replay_browser.post(
                "/identity/sso/callback",
                json={"protocol": "oidc", "state": state, "code": "code-prd114-valid"},
                headers=_headers(),
            )
            assert replayed_callback.status_code == 400
            assert replayed_callback.json()["code"] == "sso.state_invalid"

        with connect_database(database_url) as database:
            database.execute(
                """UPDATE workspace_memberships
                     SET status='disabled',revision=revision+1,
                         updated_at=datetime('now','+1 second')
                     WHERE workspace_membership_id=? AND account_id=?""",
                (member_membership_id, member_account_id),
            )
            database.commit()
        revoked = await browser.get("/workspace/session")
        assert revoked.status_code == 401
        assert revoked.json()["code"] == "workspace_session.revoked"

    clock[0] += timedelta(seconds=30)
    async with AsyncClient(transport=transport, base_url="https://testserver") as recovery:
        await _login(
            recovery,
            workspace_id=workspace_id,
            totp_code=_totp(mfa_secret, clock[0]),
            recovery_reason="idp_unavailable",
        )
        recovered = await _switch(recovery, workspace_id)
        assert recovered.status_code == 200, recovered.text
        consumed = await _switch(recovery, workspace_id)
        assert consumed.status_code == 403
        assert consumed.json()["code"] == "sso.login_required"

    with connect_database(database_url) as database:
        assert database.execute(
            """SELECT COUNT(*) FROM enterprise_identity_session_grants
                 WHERE status='consumed' AND workspace_session_id IS NOT NULL"""
        ).fetchone()[0] == 2
        assert database.execute(
            "SELECT COUNT(*) FROM account_federation_links WHERE status='active'"
        ).fetchone()[0] == 1
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
async def test_enterprise_identity_transport_isolation_idempotency_and_roles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    _database_url, app, _clock = _prepare(monkeypatch, tmp_path, migrated_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        await _login(owner)
        workspaces = await owner.get("/identity/workspaces")
        workspace_id = workspaces.json()["workspaces"][0]["workspace_id"]
        assert (await _switch(owner, workspace_id)).status_code == 200

        mixed_public = await owner.get(
            f"/identity/login-discovery?email={ADMIN_EMAIL}"
        )
        assert mixed_public.status_code == 422
        assert mixed_public.json()["code"] == "sso.request_invalid"

        unknown_field = await owner.post(
            f"/workspaces/{workspace_id}/domains",
            json={"domain": "transport.example.net", "unknown": True},
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd114-transport-unknown-0001"},
            ),
        )
        assert unknown_field.status_code == 422
        assert unknown_field.json()["code"] == "sso.request_invalid"

        duplicate_key = await owner.post(
            f"/workspaces/{workspace_id}/domains",
            json={"domain": "duplicate.example.net"},
            headers=[
                ("Origin", ORIGIN),
                ("Sec-Fetch-Site", "same-site"),
                ("X-CSRF-Token", owner.cookies.get(WORKSPACE_CSRF_COOKIE) or ""),
                ("Idempotency-Key", "prd114-duplicate-key-0001"),
                ("Idempotency-Key", "prd114-duplicate-key-0001"),
            ],
        )
        assert duplicate_key.status_code == 422
        assert duplicate_key.json()["code"] == "sso.idempotency_invalid"

        cross_origin = await owner.post(
            f"/workspaces/{workspace_id}/domains",
            json={"domain": "origin.example.net"},
            headers={
                "Origin": "https://attacker.invalid",
                "Sec-Fetch-Site": "cross-site",
                "X-CSRF-Token": owner.cookies.get(WORKSPACE_CSRF_COOKIE) or "",
                "Idempotency-Key": "prd114-cross-origin-0001",
            },
        )
        assert cross_origin.status_code == 403
        assert cross_origin.json()["code"] == "sso.origin_invalid"

        command_headers = _csrf(
            owner,
            WORKSPACE_CSRF_COOKIE,
            **{"Idempotency-Key": "prd114-idempotency-conflict-0001"},
        )
        created = await owner.post(
            f"/workspaces/{workspace_id}/domains",
            json={"domain": "first.example.net"},
            headers=command_headers,
        )
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "no-store, private"
        conflict = await owner.post(
            f"/workspaces/{workspace_id}/domains",
            json={"domain": "second.example.net"},
            headers=command_headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "sso.idempotency_conflict"

        verify_headers = _csrf(
            owner,
            WORKSPACE_CSRF_COOKIE,
            **{
                "If-Match": '"1"',
                "Idempotency-Key": "prd114-not-found-shape-0001",
            },
        )
        unknown = await owner.post(
            f"/workspaces/{workspace_id}/domains/vdm_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/verify",
            headers=verify_headers,
        )
        cross_workspace = await owner.post(
            "/workspaces/wsp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/domains/"
            f"{created.json()['domain']['verified_domain_id']}/verify",
            headers=verify_headers,
        )
        assert unknown.status_code == cross_workspace.status_code == 404
        assert unknown.json()["code"] == cross_workspace.json()["code"] == "sso.not_found"
        assert unknown.json()["message"] == cross_workspace.json()["message"]

        role_accounts = {
            "security_admin": "security.role@example.net",
            "auditor": "auditor.role@example.net",
            "developer": "developer.role@example.net",
        }
        for role, email in role_accounts.items():
            await _invite_and_register(owner, transport, email=email, role=role)

    async with AsyncClient(transport=transport, base_url="https://testserver") as security:
        await _login(security, email=role_accounts["security_admin"], password=MEMBER_PASSWORD)
        assert (await _switch(security, workspace_id)).status_code == 200
        managed = await security.post(
            f"/workspaces/{workspace_id}/domains",
            json={"domain": "security-admin.example.net"},
            headers=_csrf(
                security,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd114-security-admin-0001"},
            ),
        )
        assert managed.status_code == 201, managed.text

    async with AsyncClient(transport=transport, base_url="https://testserver") as auditor:
        await _login(auditor, email=role_accounts["auditor"], password=MEMBER_PASSWORD)
        assert (await _switch(auditor, workspace_id)).status_code == 200
        assert (
            await auditor.get(f"/workspaces/{workspace_id}/enterprise-identity")
        ).status_code == 200
        denied_write = await auditor.post(
            f"/workspaces/{workspace_id}/domains",
            json={"domain": "auditor.example.net"},
            headers=_csrf(
                auditor,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd114-auditor-denied-0001"},
            ),
        )
        assert denied_write.status_code == 403
        assert denied_write.json()["code"] == "workspace_session.permission_denied"

    async with AsyncClient(transport=transport, base_url="https://testserver") as developer:
        await _login(developer, email=role_accounts["developer"], password=MEMBER_PASSWORD)
        assert (await _switch(developer, workspace_id)).status_code == 200
        denied_read = await developer.get(
            f"/workspaces/{workspace_id}/enterprise-identity"
        )
        assert denied_read.status_code == 403
        assert denied_read.json()["code"] == "workspace_session.permission_denied"
