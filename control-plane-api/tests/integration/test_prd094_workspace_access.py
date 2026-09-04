"""验证 PRD-094 WorkspaceSession、成员、邀请、owner 转移与跨凭据域清理合同。"""

from __future__ import annotations

import asyncio
import math
import shutil
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.console.routes import router as console_router
from metahub_control.identity.delivery import (
    clear_test_identity_challenges,
    pop_test_identity_challenge,
)
from metahub_control.identity.routes import router as identity_router
from metahub_control.invitations.routes import router as invitations_router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
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
MEMBER_PASSWORD = "MembershipUser!2026"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"
GLOBAL_SESSION_COOKIE = "__Host-metahub-global-session"


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd094-api-migrated")
    database = root / "workspace-current.sqlite3"
    result = apply_migrations(f"sqlite:///{database.as_posix()}")
    assert "0086" in result.applied
    return database


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
):
    database = tmp_path / "workspace.sqlite3"
    shutil.copy2(migrated_database, database)
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-legacy-auth-token-secret")
    monkeypatch.setenv(
        "METAHUB_IDENTITY_SESSION_SECRET",
        "test-independent-global-identity-session-secret-2026",
    )
    monkeypatch.setenv(
        "METAHUB_IDENTITY_CHALLENGE_SECRET",
        "test-independent-global-identity-challenge-secret-2026",
    )
    monkeypatch.setenv(
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET",
        "test-independent-global-identity-pii-encryption-secret-2026",
    )
    monkeypatch.setenv(
        "METAHUB_WORKSPACE_SESSION_SECRET",
        "test-independent-workspace-session-secret-2026",
    )
    monkeypatch.setenv("METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE", "test_capture")
    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", ORIGIN)
    apply_seed_data(database_url)
    clear_test_identity_challenges()
    app = create_service_app(
        "control-plane-api",
        "PRD-094 Workspace Access Test",
        extra_cors_allow_headers=("If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.include_router(auth_router)
    app.include_router(identity_router)
    app.include_router(invitations_router)
    app.include_router(workspace_access_router)
    app.include_router(workspaces_router)
    app.include_router(console_router)
    return database_url, app


def _origin_headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site", **extra}


def _csrf_headers(client: AsyncClient, cookie_name: str, **extra: str) -> dict[str, str]:
    csrf = client.cookies.get(cookie_name)
    assert csrf
    return _origin_headers(**{"X-CSRF-Token": csrf, **extra})


async def _global_login(client: AsyncClient, email: str, password: str) -> None:
    response = await client.post(
        "/identity/login",
        json={"email": email, "password": password},
        headers=_origin_headers(),
    )
    assert response.status_code == 200, response.text


async def _legacy_login(client: AsyncClient) -> str:
    response = await client.post(
        "/auth/login",
        json={"tenant_slug": None, "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def _switch(client: AsyncClient, workspace_id: str):
    response = await client.post(
        "/identity/workspace-switches",
        json={"workspace_id": workspace_id},
        headers=_csrf_headers(client, GLOBAL_CSRF_COOKIE),
    )
    assert response.status_code == 200, response.text
    return response


async def _register_with_invitation(
    client: AsyncClient,
    *,
    email: str,
    invitation_token: str,
) -> None:
    started = await client.post(
        "/identity/registrations",
        json={
            "email": email,
            "display_name": "Invited Member",
            "password": MEMBER_PASSWORD,
            "consent_version": "privacy-2026-08",
            "invitation_token": invitation_token,
        },
        headers=_origin_headers(),
    )
    assert started.status_code == 200, started.text
    intent_id = started.json()["intent_id"]
    challenge = pop_test_identity_challenge("registration", intent_id)
    assert challenge is not None
    verified = await client.post(
        "/identity/verifications",
        json={"intent_id": intent_id, "code": challenge.code},
        headers=_origin_headers(),
    )
    assert verified.status_code == 200, verified.text


async def _register_account(client: AsyncClient, *, email: str, display_name: str) -> None:
    started = await client.post(
        "/identity/registrations",
        json={
            "email": email,
            "display_name": display_name,
            "password": MEMBER_PASSWORD,
            "consent_version": "privacy-2026-08",
        },
        headers=_origin_headers(),
    )
    assert started.status_code == 200, started.text
    intent_id = started.json()["intent_id"]
    challenge = pop_test_identity_challenge("registration", intent_id)
    assert challenge is not None
    verified = await client.post(
        "/identity/verifications",
        json={"intent_id": intent_id, "code": challenge.code},
        headers=_origin_headers(),
    )
    assert verified.status_code == 200, verified.text


def _cookie_header(client: AsyncClient, *names: str) -> str:
    values = []
    for name in names:
        value = client.cookies.get(name)
        assert value
        values.append(f"{name}={value}")
    return "; ".join(values)


async def _legacy_invite_and_accept(
    app,
    *,
    email: str,
    role: str = "developer",
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as legacy:
        token = await _legacy_login(legacy)
        created = await legacy.post(
            "/invitations",
            json={"email": email, "role": role, "expires_in_hours": 24},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert created.status_code == 201, created.text
        accept_token = created.json()["accept_token"]
    async with AsyncClient(transport=transport, base_url="https://testserver") as anonymous:
        accepted = await anonymous.post(
            f"/invitations/{accept_token}/accept",
            json={"display_name": "Legacy Invited", "password": MEMBER_PASSWORD},
        )
        assert accepted.status_code == 201, accepted.text


@pytest.mark.anyio
async def test_workspace_switch_navigation_mixed_credentials_and_logout_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    _database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        await _global_login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        listed = await client.get("/identity/workspaces")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        workspace_id = listed.json()["workspaces"][0]["workspace_id"]

        client.cookies.set(
            PLATFORM_SESSION_COOKIE, "p" * 43, domain="testserver.local", path="/"
        )
        client.cookies.set(
            PLATFORM_CSRF_COOKIE, "c" * 43, domain="testserver.local", path="/"
        )
        client.cookies.set(
            SUPPORT_SESSION_COOKIE, "s" * 43, domain="testserver.local", path="/"
        )
        switched = await _switch(client, workspace_id)
        assert switched.json()["context"]["role"] == "owner"
        switch_cookies = switched.headers.get_list("set-cookie")
        assert any(
            PLATFORM_SESSION_COOKIE in value and "Max-Age=0" in value
            for value in switch_cookies
        )
        assert any(
            PLATFORM_CSRF_COOKIE in value and "Max-Age=0" in value
            for value in switch_cookies
        )
        assert any(
            SUPPORT_SESSION_COOKIE in value and "Max-Age=0" in value
            for value in switch_cookies
        )
        assert client.cookies.get(PLATFORM_SESSION_COOKIE) is None
        assert client.cookies.get(PLATFORM_CSRF_COOKIE) is None
        assert client.cookies.get(SUPPORT_SESSION_COOKIE) is None
        assert client.cookies.get(WORKSPACE_SESSION_COOKIE)
        assert client.cookies.get(WORKSPACE_CSRF_COOKIE)

        session = await client.get("/workspace/session")
        assert session.status_code == 200
        session_body = session.json()
        assert set(session_body["authorization"]) == {
            "permission_codes",
            "permission_revision",
            "permission_fingerprint",
            "membership_revision",
        }
        assert session_body["authorization"]["membership_revision"] == 1
        assert session_body["authorization"]["permission_revision"] >= 1
        assert len(session_body["authorization"]["permission_fingerprint"]) == 64
        assert "console.basic.read" in session_body["authorization"]["permission_codes"]
        members = await client.get("/memberships")
        assert members.status_code == 200
        assert members.json()["memberships"][0]["role"] == "owner"

        navigation = await client.get("/console/navigation")
        assert navigation.status_code == 200, navigation.text
        navigation_body = navigation.json()
        assert navigation_body["groups"]
        assert "navigation_items" not in navigation_body
        assert "permission_codes" not in navigation_body

        legacy_token = await _legacy_login(client)
        mixed = await client.get(
            "/console/navigation",
            headers={"Authorization": f"Bearer {legacy_token}"},
        )
        assert mixed.status_code == 400
        assert mixed.json()["code"] == "workspace_session.credential_conflict"

        logged_out = await client.post(
            "/identity/logout",
            headers=_csrf_headers(client, GLOBAL_CSRF_COOKIE),
        )
        assert logged_out.status_code == 200
        set_cookies = logged_out.headers.get_list("set-cookie")
        assert any(
            WORKSPACE_SESSION_COOKIE in value and "Max-Age=0" in value
            for value in set_cookies
        )
        assert any(WORKSPACE_CSRF_COOKIE in value and "Max-Age=0" in value for value in set_cookies)
        assert client.cookies.get(GLOBAL_SESSION_COOKIE) is None
        assert client.cookies.get(WORKSPACE_SESSION_COOKIE) is None


@pytest.mark.anyio
async def test_legacy_and_verified_invitation_paths_create_exact_memberships(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    await _legacy_invite_and_accept(app, email="legacy.member@example.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        await _global_login(owner, ADMIN_EMAIL, ADMIN_PASSWORD)
        listed = await owner.get("/identity/workspaces")
        workspace_id = listed.json()["workspaces"][0]["workspace_id"]
        await _switch(owner, workspace_id)
        created = await owner.post(
            "/membership-invitations",
            json={
                "email": "verified.member@example.com",
                "role": "auditor",
                "expires_in_hours": 24,
            },
            headers=_csrf_headers(owner, WORKSPACE_CSRF_COOKIE),
        )
        assert created.status_code == 201, created.text
        invitation_token = created.json()["accept_token"]

    async with AsyncClient(transport=transport, base_url="https://testserver") as invited:
        await _register_with_invitation(
            invited,
            email="verified.member@example.com",
            invitation_token=invitation_token,
        )

    connection = connect_database(database_url)
    try:
        rows = connection.execute(
            """
            SELECT account.email_normalized,member.role_code,member.source,member.status
            FROM workspace_memberships member
            JOIN global_accounts account ON account.account_id=member.account_id
            WHERE account.email_normalized IN(?,?) ORDER BY account.email_normalized
            """,
            ("legacy.member@example.com", "verified.member@example.com"),
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("legacy.member@example.com", "developer", "membership_invitation", "active"),
            ("verified.member@example.com", "auditor", "membership_invitation", "active"),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM membership_invitations WHERE status='accepted'"
        ).fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_workspace_credential_origin_csrf_and_cross_workspace_negative_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    await _legacy_invite_and_accept(app, email="cross.workspace.member@example.com")
    connection = connect_database(database_url)
    try:
        account_id = connection.execute(
            "SELECT account_id FROM global_accounts WHERE email_normalized=?",
            (ADMIN_EMAIL,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO account_verifications(
              account_verification_id,account_id,verification_type,identifier_hmac,
              source_type,source_intent_id,verified_at)
            VALUES('avf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',?,'email',?,
              'registration','rgi_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
              '2030-01-01T00:00:00Z')
            """,
            (account_id, "a" * 64),
        )
        connection.commit()
    finally:
        connection.close()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        await _global_login(owner, ADMIN_EMAIL, ADMIN_PASSWORD)
        workspace_one = (await owner.get("/identity/workspaces")).json()["workspaces"][0][
            "workspace_id"
        ]
        created = await owner.post(
            "/workspaces",
            json={
                "kind": "organization",
                "name": "Second Workspace",
                "slug": "second-workspace",
                "region": "cn-north-1",
                "service_mode": "internal_only",
            },
            headers=_csrf_headers(
                owner,
                GLOBAL_CSRF_COOKIE,
                **{"Idempotency-Key": "prd094-cross-workspace-0001"},
            ),
        )
        assert created.status_code == 201, created.text
        workspace_two = created.json()["workspace"]["workspace_id"]
        await _switch(owner, workspace_one)
        members = (await owner.get("/memberships")).json()["memberships"]
        target = next(member for member in members if member["role"] != "owner")

        cross_origin = await owner.patch(
            f"/memberships/{target['membership_id']}",
            json={"role": "viewer"},
            headers={
                "Origin": "https://attacker.invalid",
                "Sec-Fetch-Site": "cross-site",
                "X-CSRF-Token": owner.cookies.get(WORKSPACE_CSRF_COOKIE),
                "If-Match": f'"{target["revision"]}"',
            },
        )
        assert cross_origin.status_code == 403
        assert cross_origin.json()["code"] == "identity.origin_invalid"

        bad_csrf = await owner.patch(
            f"/memberships/{target['membership_id']}",
            json={"role": "viewer"},
            headers=_origin_headers(
                **{"X-CSRF-Token": "invalid-csrf", "If-Match": f'"{target["revision"]}"'}
            ),
        )
        assert bad_csrf.status_code == 403
        assert bad_csrf.json()["code"] == "workspace_session.csrf_invalid"

        legacy_token = await _legacy_login(owner)
        global_mixed = await owner.get(
            "/identity/workspaces",
            headers={"Authorization": f"Bearer {legacy_token}"},
        )
        assert global_mixed.status_code == 400
        assert global_mixed.json()["code"] == "identity.credential_invalid"

        workspace_cookie = owner.cookies.get(WORKSPACE_SESSION_COOKIE)
        assert workspace_cookie
        async with AsyncClient(transport=transport, base_url="https://testserver") as raw:
            duplicate = await raw.get(
                "/workspace/session",
                headers={
                    "Cookie": (
                        f"{WORKSPACE_SESSION_COOKIE}={workspace_cookie}; "
                        f"{WORKSPACE_SESSION_COOKIE}=duplicate_cookie_value"
                    )
                },
            )
            assert duplicate.status_code == 401
            assert duplicate.json()["code"] == "workspace_session.invalid"
            global_only = await raw.get(
                "/memberships",
                headers={"Cookie": _cookie_header(owner, GLOBAL_SESSION_COOKIE)},
            )
            assert global_only.status_code == 401
            assert global_only.json()["code"] == "workspace_session.required"

        await _switch(owner, workspace_two)
        cross_workspace = await owner.patch(
            f"/memberships/{target['membership_id']}",
            json={"role": "viewer"},
            headers=_csrf_headers(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{"If-Match": f'"{target["revision"]}"'},
            ),
        )
        unknown = await owner.patch(
            "/memberships/wmb_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            json={"role": "viewer"},
            headers=_csrf_headers(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{"If-Match": f'"{target["revision"]}"'},
            ),
        )
        assert cross_workspace.status_code == unknown.status_code == 404
        assert cross_workspace.json()["code"] == unknown.json()["code"] == "membership.not_found"
        assert cross_workspace.json()["message"] == unknown.json()["message"]


@pytest.mark.anyio
async def test_nine_role_delegation_matrix_and_service_subject_browser_denial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    member_email = "role.matrix.member@example.com"
    service_email = "service.subject@example.com"
    await _legacy_invite_and_accept(app, email=member_email)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="https://testserver") as owner,
        AsyncClient(transport=transport, base_url="https://testserver") as member,
        AsyncClient(transport=transport, base_url="https://testserver") as service_subject,
    ):
        await _global_login(owner, ADMIN_EMAIL, ADMIN_PASSWORD)
        workspace_id = (await owner.get("/identity/workspaces")).json()["workspaces"][0][
            "workspace_id"
        ]
        await _switch(owner, workspace_id)
        await _global_login(member, member_email, MEMBER_PASSWORD)

        current_role = "developer"
        for role in (
            "admin",
            "developer",
            "security_admin",
            "finance_admin",
            "legal_admin",
            "auditor",
            "viewer",
        ):
            owner_members = (await owner.get("/memberships")).json()["memberships"]
            target = next(item for item in owner_members if item["role"] != "owner")
            if current_role != role:
                changed = await owner.patch(
                    f"/memberships/{target['membership_id']}",
                    json={"role": role},
                    headers=_csrf_headers(
                        owner,
                        WORKSPACE_CSRF_COOKIE,
                        **{"If-Match": f'"{target["revision"]}"'},
                    ),
                )
                assert changed.status_code == 200, changed.text
                current_role = role

            await _switch(member, workspace_id)
            visible = await member.get("/memberships")
            assert visible.status_code == 200, visible.text
            assert visible.json()["total"] == (2 if role == "admin" else 1)
            invitation = await member.post(
                "/membership-invitations",
                json={
                    "email": f"matrix-{role}@example.com",
                    "role": "viewer",
                    "expires_in_hours": 24,
                },
                headers=_csrf_headers(member, WORKSPACE_CSRF_COOKIE),
            )
            if role == "admin":
                assert invitation.status_code == 201, invitation.text
            else:
                assert invitation.status_code == 403
                assert invitation.json()["code"] == "workspace_session.permission_denied"

        owner_members = (await owner.get("/memberships")).json()["memberships"]
        owner_membership = next(item for item in owner_members if item["role"] == "owner")
        ordinary_owner_change = await owner.patch(
            f"/memberships/{owner_membership['membership_id']}",
            json={"status": "disabled"},
            headers=_csrf_headers(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{"If-Match": f'"{owner_membership["revision"]}"'},
            ),
        )
        assert ordinary_owner_change.status_code == 409
        assert ordinary_owner_change.json()["code"] == "membership.update_invalid"

        await _register_account(
            service_subject,
            email=service_email,
            display_name="Automation Runner",
        )
        connection = connect_database(database_url)
        try:
            account_id = connection.execute(
                "SELECT account_id FROM global_accounts WHERE email_normalized=?",
                (service_email,),
            ).fetchone()[0]
            owner_row = connection.execute(
                """
                SELECT workspace_id,tenant_id,account_id
                FROM workspace_memberships WHERE role_code='owner'
                """
            ).fetchone()
            connection.execute(
                """
                INSERT INTO workspace_memberships(
                  workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
                  role_code,status,source,revision,created_by_account_id,
                  updated_by_account_id,created_at,updated_at)
                VALUES('wmb_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',?,?,?,NULL,
                  'service_account','pending','membership_invitation',1,?,?,
                  '2030-01-01T00:00:00Z','2030-01-01T00:00:00Z')
                """,
                (
                    owner_row["workspace_id"],
                    owner_row["tenant_id"],
                    account_id,
                    owner_row["account_id"],
                    owner_row["account_id"],
                ),
            )
            connection.commit()
        finally:
            connection.close()

        service_row = next(
            item
            for item in (await owner.get("/memberships")).json()["memberships"]
            if item["role"] == "service_account"
        )
        assert service_row["status"] == "pending"
        assert service_row["can_manage"] is False
        assert (await service_subject.get("/identity/workspaces")).json()["total"] == 0
        denied_switch = await service_subject.post(
            "/identity/workspace-switches",
            json={"workspace_id": workspace_id},
            headers=_csrf_headers(service_subject, GLOBAL_CSRF_COOKIE),
        )
        assert denied_switch.status_code == 404
        assert denied_switch.json()["code"] == "workspace.not_found"


@pytest.mark.anyio
async def test_membership_change_revokes_next_request_and_owner_transfer_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    target_email = "owner.target@example.com"
    await _legacy_invite_and_accept(app, email=target_email)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        await _global_login(owner, ADMIN_EMAIL, ADMIN_PASSWORD)
        listed = await owner.get("/identity/workspaces")
        workspace_id = listed.json()["workspaces"][0]["workspace_id"]
        await _switch(owner, workspace_id)
        members = await owner.get("/memberships")
        target = next(
            member
            for member in members.json()["memberships"]
            if member["display_name"] == "Legacy Invited"
        )

        async with AsyncClient(transport=transport, base_url="https://testserver") as target_client:
            await _global_login(target_client, target_email, MEMBER_PASSWORD)
            await _switch(target_client, workspace_id)
            assert (await target_client.get("/workspace/session")).status_code == 200

            updated = await owner.patch(
                f"/memberships/{target['membership_id']}",
                json={"role": "viewer"},
                headers=_csrf_headers(
                    owner,
                    WORKSPACE_CSRF_COOKIE,
                    **{"If-Match": f'"{target["revision"]}"'},
                ),
            )
            assert updated.status_code == 200, updated.text
            revoked = await target_client.get("/workspace/session")
            assert revoked.status_code == 401
            assert revoked.json()["code"] == "workspace_session.revoked"
            assert target_client.cookies.get(WORKSPACE_SESSION_COOKIE) is None
            await _switch(target_client, workspace_id)

            transferred = await owner.post(
                "/workspace-owner-transfers",
                json={
                    "target_membership_id": target["membership_id"],
                    "current_password": ADMIN_PASSWORD,
                },
                headers=_csrf_headers(owner, WORKSPACE_CSRF_COOKIE),
            )
            assert transferred.status_code == 200, transferred.text
            assert transferred.json()["source"]["role"] == "admin"
            assert transferred.json()["target"]["role"] == "owner"

            old_owner = await owner.get("/workspace/session")
            assert old_owner.status_code == 401
            target_revoked = await target_client.get("/workspace/session")
            assert target_revoked.status_code == 401
            await _switch(target_client, workspace_id)
            current = await target_client.get("/workspace/session")
            assert current.json()["context"]["role"] == "owner"

    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_memberships WHERE role_code='owner' AND status='active'"
        ).fetchone()[0] == 1
        projection = connection.execute(
            """
            SELECT member.role_code,user.role
            FROM workspace_memberships member
            JOIN global_accounts account ON account.account_id=member.account_id
            JOIN users user ON user.tenant_id=member.tenant_id
              AND user.user_id=member.legacy_user_id
            WHERE account.email_normalized=?
            """,
            (target_email,),
        ).fetchone()
        assert tuple(projection) == ("owner", "admin")
        assert connection.execute(
            "SELECT COUNT(*) FROM owner_transfers WHERE status='completed'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_workspace_access_sensitive_canaries_never_reach_database_or_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    transport = ASGITransport(app=app)
    caplog.set_level("INFO")
    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        await _global_login(owner, ADMIN_EMAIL, ADMIN_PASSWORD)
        global_session = owner.cookies.get(GLOBAL_SESSION_COOKIE)
        global_csrf = owner.cookies.get(GLOBAL_CSRF_COOKIE)
        workspace_id = (await owner.get("/identity/workspaces")).json()["workspaces"][0][
            "workspace_id"
        ]
        await _switch(owner, workspace_id)
        workspace_session = owner.cookies.get(WORKSPACE_SESSION_COOKIE)
        workspace_csrf = owner.cookies.get(WORKSPACE_CSRF_COOKIE)
        invited = await owner.post(
            "/membership-invitations",
            json={
                "email": "sensitive-audit.member@example.com",
                "role": "viewer",
                "expires_in_hours": 24,
            },
            headers=_csrf_headers(owner, WORKSPACE_CSRF_COOKIE),
        )
        assert invited.status_code == 201, invited.text
        invitation_token = invited.json()["accept_token"]

    sensitive_values = (
        ADMIN_PASSWORD,
        global_session,
        global_csrf,
        workspace_session,
        workspace_csrf,
        invitation_token,
    )
    assert all(isinstance(value, str) and value for value in sensitive_values)
    connection = connect_database(database_url)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    database = Path(database_url.removeprefix("sqlite:///"))
    artifacts = b"".join(
        path.read_bytes()
        for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
        if path.exists()
    )
    database_hits = sum(
        artifacts.count(value.encode("utf-8")) for value in sensitive_values
    )
    log_bytes = caplog.text.encode("utf-8")
    log_hits = sum(log_bytes.count(value.encode("utf-8")) for value in sensitive_values)
    assert database_hits == 0
    assert log_hits == 0


@pytest.mark.anyio
async def test_warm_workspace_switch_meets_p95_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as account:
        await _global_login(account, ADMIN_EMAIL, ADMIN_PASSWORD)
        workspace_id = (await account.get("/identity/workspaces")).json()["workspaces"][0][
            "workspace_id"
        ]
        await _switch(account, workspace_id)

        samples: list[float] = []
        for _index in range(20):
            started_at = time.perf_counter()
            response = await _switch(account, workspace_id)
            samples.append(time.perf_counter() - started_at)
            assert response.status_code == 200

    p95 = sorted(samples)[math.ceil(len(samples) * 0.95) - 1]
    assert p95 < 0.300, samples
    connection = connect_database(database_url)
    try:
        counts = connection.execute(
            """
            SELECT COUNT(*) total,
              SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) active_count
            FROM workspace_sessions
            """
        ).fetchone()
        assert tuple(counts) == (21, 1)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_one_hundred_concurrent_switches_leave_one_active_workspace_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as account:
        await _global_login(account, ADMIN_EMAIL, ADMIN_PASSWORD)
        workspace_id = (await account.get("/identity/workspaces")).json()["workspaces"][0][
            "workspace_id"
        ]
        cookie = _cookie_header(account, GLOBAL_SESSION_COOKIE, GLOBAL_CSRF_COOKIE)
        csrf = account.cookies.get(GLOBAL_CSRF_COOKIE)
        assert csrf

    async def switch_once() -> int:
        async with AsyncClient(transport=transport, base_url="https://testserver") as worker:
            response = await worker.post(
                "/identity/workspace-switches",
                json={"workspace_id": workspace_id},
                headers=_origin_headers(**{"Cookie": cookie, "X-CSRF-Token": csrf}),
            )
            return response.status_code

    statuses = await asyncio.gather(*(switch_once() for _index in range(100)))
    assert statuses == [200] * 100
    connection = connect_database(database_url)
    try:
        counts = connection.execute(
            """
            SELECT COUNT(*) total,
              SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) active_count
            FROM workspace_sessions
            """
        ).fetchone()
        assert tuple(counts) == (100, 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM membership_events WHERE event_type='workspace.switched'"
        ).fetchone()[0] == 100
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_one_hundred_concurrent_member_disables_commit_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    await _legacy_invite_and_accept(app, email="disable.race@example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        await _global_login(owner, ADMIN_EMAIL, ADMIN_PASSWORD)
        workspace_id = (await owner.get("/identity/workspaces")).json()["workspaces"][0][
            "workspace_id"
        ]
        await _switch(owner, workspace_id)
        target = next(
            member
            for member in (await owner.get("/memberships")).json()["memberships"]
            if member["role"] != "owner"
        )
        cookie = _cookie_header(owner, WORKSPACE_SESSION_COOKIE, WORKSPACE_CSRF_COOKIE)
        csrf = owner.cookies.get(WORKSPACE_CSRF_COOKIE)
        assert csrf

    async def disable_once() -> int:
        async with AsyncClient(transport=transport, base_url="https://testserver") as worker:
            response = await worker.patch(
                f"/memberships/{target['membership_id']}",
                json={"status": "disabled"},
                headers=_origin_headers(
                    **{
                        "Cookie": cookie,
                        "X-CSRF-Token": csrf,
                        "If-Match": f'"{target["revision"]}"',
                    }
                ),
            )
            return response.status_code

    statuses = await asyncio.gather(*(disable_once() for _index in range(100)))
    assert statuses.count(200) == 1
    assert statuses.count(412) == 99
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT status,revision FROM workspace_memberships
            WHERE workspace_membership_id=?
            """,
            (target["membership_id"],),
        ).fetchone()
        assert tuple(row) == ("disabled", target["revision"] + 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM membership_events WHERE event_type='membership.disabled'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_one_hundred_concurrent_owner_transfers_commit_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_database)
    await _legacy_invite_and_accept(app, email="owner.race@example.com")
    monkeypatch.setattr(
        "metahub_control.workspace_access.service.verify_password",
        lambda _password, _expected_hash: True,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        await _global_login(owner, ADMIN_EMAIL, ADMIN_PASSWORD)
        workspace_id = (await owner.get("/identity/workspaces")).json()["workspaces"][0][
            "workspace_id"
        ]
        await _switch(owner, workspace_id)
        target = next(
            member
            for member in (await owner.get("/memberships")).json()["memberships"]
            if member["role"] != "owner"
        )
        cookie = _cookie_header(owner, WORKSPACE_SESSION_COOKIE, WORKSPACE_CSRF_COOKIE)
        csrf = owner.cookies.get(WORKSPACE_CSRF_COOKIE)
        assert csrf

    async def transfer_once() -> int:
        async with AsyncClient(transport=transport, base_url="https://testserver") as worker:
            response = await worker.post(
                "/workspace-owner-transfers",
                json={
                    "target_membership_id": target["membership_id"],
                    "current_password": "concurrency-test-password",
                },
                headers=_origin_headers(**{"Cookie": cookie, "X-CSRF-Token": csrf}),
            )
            return response.status_code

    statuses = await asyncio.gather(*(transfer_once() for _index in range(100)))
    assert statuses.count(200) == 1
    assert statuses.count(401) + statuses.count(409) == 99
    connection = connect_database(database_url)
    try:
        owners = connection.execute(
            """
            SELECT workspace_membership_id FROM workspace_memberships
            WHERE role_code='owner' AND status='active'
            """
        ).fetchall()
        assert [row[0] for row in owners] == [target["membership_id"]]
        assert connection.execute(
            "SELECT COUNT(*) FROM owner_transfers WHERE status='completed'"
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT COUNT(*) FROM membership_events
            WHERE event_type='workspace.owner_transferred'
            """
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
