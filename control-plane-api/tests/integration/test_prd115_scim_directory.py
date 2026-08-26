"""PRD-115 Workspace administration and cookie-free SCIM protocol acceptance."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path

import metahub_control.enterprise_identity.scim_service as scim_service
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.enterprise_identity.scim_routes import router as scim_router
from metahub_control.identity.delivery import pop_test_identity_challenge
from metahub_control.identity.routes import router as identity_router
from metahub_control.workspace_access.constants import WORKSPACE_CSRF_COOKIE
from metahub_control.workspace_access.routes import router as workspace_access_router
from metahub_control.workspaces.routes import router as workspaces_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"
SCIM_CONTENT = "application/scim+json"
VERIFIED_MEMBER_EMAIL = "verified.directory.member@example.com"
SEED_PATH = Path(__file__).resolve().parents[3] / "infra" / "seeds" / "default_seed.json"
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"


def _local_seed_admin() -> tuple[str, str]:
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    admin = seed["admin"]
    return str(admin["email"]), str(admin["password"])


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd115-api-migrated")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 108:
            shutil.copy2(source, migrations / source.name)
    database = root / "scim-current.sqlite3"
    assert (
        apply_migrations(
            f"sqlite:///{database.as_posix()}", migrations_dir=migrations
        ).applied[-1]
        == "0108"
    )
    return database


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
):
    now = datetime.now(UTC).replace(microsecond=0)
    _, admin_password = _local_seed_admin()
    owner_email = f"owner-{secrets.token_hex(8)}@example.com"
    database = tmp_path / "scim.sqlite3"
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
    monkeypatch.setattr(scim_service, "_utcnow", lambda: now)
    app = create_service_app(
        "control-plane-api",
        "PRD-115 SCIM Directory Test",
        extra_cors_allow_methods=("PUT",),
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.include_router(identity_router)
    app.include_router(workspaces_router)
    app.include_router(workspace_access_router)
    app.include_router(scim_router)
    return database_url, app, owner_email


def _headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site", **extra}


def _csrf(client: AsyncClient, name: str, **extra: str) -> dict[str, str]:
    token = client.cookies.get(name)
    assert token
    return _headers(**{"X-CSRF-Token": token, **extra})


async def _login_and_switch(client: AsyncClient, owner_email: str) -> str:
    _, admin_password = _local_seed_admin()
    credentials = {"email": owner_email, "password": admin_password}
    logged_in = await client.post(
        "/identity/login",
        json=credentials,
        headers=_headers(),
    )
    credentials.clear()
    owner_email = admin_password = ""
    assert logged_in.status_code == 200, logged_in.text
    workspaces = await client.get("/identity/workspaces")
    workspace_id = workspaces.json()["workspaces"][0]["workspace_id"]
    switched = await client.post(
        "/identity/workspace-switches",
        json={"workspace_id": workspace_id},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert switched.status_code == 200, switched.text
    return workspace_id


async def _register_verified_account(client: AsyncClient, email: str) -> None:
    password = f"Aa1!{secrets.token_urlsafe(24)}"
    registration = {
        "email": email,
        "display_name": "Verified Directory Member",
        "password": password,
        "consent_version": "privacy-2026-08",
    }
    started = await client.post(
        "/identity/registrations",
        json=registration,
        headers=_headers(),
    )
    registration.clear()
    password = ""
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


def _install_verified_domain(database_url: str, workspace_id: str) -> None:
    with connect_database(database_url) as connection:
        owner = connection.execute(
            """SELECT member.tenant_id,member.account_id FROM workspace_memberships member
                 WHERE member.workspace_id=? AND member.role_code='owner'""",
            (workspace_id,),
        ).fetchone()
        assert owner is not None
        stamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        connection.execute(
            """INSERT INTO verified_domains(
                 verified_domain_id,workspace_id,tenant_id,domain_normalized,domain_hmac,
                 challenge_hash,status,revision,challenge_expires_at,verified_at,suspended_at,
                 created_by_account_id,updated_by_account_id,created_at,updated_at)
               VALUES(?,?,?,'example.com',?,?,'verified',2,?,?,NULL,?,?,?,?)""",
            (
                "vdm_" + "1" * 32,
                workspace_id,
                owner["tenant_id"],
                hashlib.sha256(b"domain").hexdigest(),
                hashlib.sha256(b"challenge").hexdigest(),
                stamp,
                stamp,
                owner["account_id"],
                owner["account_id"],
                stamp,
                stamp,
            ),
        )
        connection.commit()


async def _create_token(owner: AsyncClient, workspace_id: str) -> dict[str, object]:
    response = await owner.post(
        f"/workspaces/{workspace_id}/scim-tokens",
        json={"expires_in_days": 30},
        headers=_csrf(
            owner,
            WORKSPACE_CSRF_COOKIE,
            **{"Idempotency-Key": "prd115-token-create-0001"},
        ),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _machine_headers(token: str, **extra: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": SCIM_CONTENT,
        "Content-Type": SCIM_CONTENT,
        **extra,
    }


def _user_payload(user_name: str, external_id: str) -> dict[str, object]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": user_name,
        "externalId": external_id,
        "displayName": user_name.split("@", 1)[0].replace(".", " ").title(),
        "active": True,
        "meta": {"version": "1"},
    }


@pytest.mark.anyio
async def test_prd115_token_protocol_owner_protection_and_rotation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    database_url, app, admin_email = _prepare(monkeypatch, tmp_path, migrated_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        workspace_id = await _login_and_switch(owner, admin_email)
        _install_verified_domain(database_url, workspace_id)
        async with AsyncClient(transport=transport, base_url="https://testserver") as registration:
            await _register_verified_account(registration, VERIFIED_MEMBER_EMAIL)
        created_token = await _create_token(owner, workspace_id)
        bearer = created_token["bearer_token"]
        assert isinstance(bearer, str) and bearer.startswith("mhscim_")
        assert bearer not in str(created_token["token"])
        replay = await owner.post(
            f"/workspaces/{workspace_id}/scim-tokens",
            json={"expires_in_days": 30},
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd115-token-create-0001"},
            ),
        )
        assert replay.status_code == 201
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["bearer_token"] is None

        async with AsyncClient(transport=transport, base_url="https://testserver") as machine:
            wrong_media = await machine.post(
                "/scim/v2/Users",
                json=_user_payload(admin_email, "owner-001"),
                headers={"Authorization": f"Bearer {bearer}"},
            )
            assert wrong_media.status_code == 400
            assert wrong_media.headers["content-type"].startswith(SCIM_CONTENT)
            assert wrong_media.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]

            owner_user = await machine.post(
                "/scim/v2/Users",
                content=scim_service._canonical(_user_payload(admin_email, "owner-001")),
                headers=_machine_headers(bearer),
            )
            assert owner_user.status_code == 201, owner_user.text
            assert owner_user.headers["content-type"].startswith(SCIM_CONTENT)
            assert owner_user.headers["etag"] == 'W/"1"'
            owner_link_id = owner_user.json()["id"]

            blocked = await machine.patch(
                f"/scim/v2/Users/{owner_link_id}",
                content=scim_service._canonical(
                    {
                        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                        "Operations": [{"op": "replace", "path": "active", "value": False}],
                    }
                ),
                headers=_machine_headers(bearer, **{"If-Match": 'W/"1"'}),
            )
            assert blocked.status_code == 409
            assert blocked.json()["scimType"] == "mutability"

            pending = await machine.post(
                "/scim/v2/Users",
                content=scim_service._canonical(
                    _user_payload("directory.member@example.com", "member-001")
                ),
                headers=_machine_headers(bearer),
            )
            assert pending.status_code == 201, pending.text
            assert pending.json()["active"] is False
            pending_id = pending.json()["id"]

            verified_member = await machine.post(
                "/scim/v2/Users",
                content=scim_service._canonical(
                    _user_payload(VERIFIED_MEMBER_EMAIL, "verified-member-001")
                ),
                headers=_machine_headers(bearer),
            )
            assert verified_member.status_code == 201, verified_member.text
            assert verified_member.json()["active"] is True
            verified_member_id = verified_member.json()["id"]

            filtered = await machine.get(
                "/scim/v2/Users?filter=userName%20eq%20%22Directory.Member%40Example.COM%22"
                "&startIndex=1&count=10",
                headers={"Authorization": f"Bearer {bearer}", "Accept": SCIM_CONTENT},
            )
            assert filtered.status_code == 200, filtered.text
            assert filtered.json()["totalResults"] == 1
            assert filtered.json()["Resources"][0]["id"] == pending_id

            cookie_mix = await machine.get(
                "/scim/v2/Users",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Cookie": "browser_session=forbidden",
                },
            )
            assert cookie_mix.status_code == 400

            group = await machine.post(
                "/scim/v2/Groups",
                content=scim_service._canonical(
                    {
                        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                        "externalId": "group-001",
                        "displayName": "Engineering Directory",
                        "members": [
                            {"value": pending_id},
                            {"value": verified_member_id},
                        ],
                        "meta": {"version": 1},
                    }
                ),
                headers=_machine_headers(bearer),
            )
            assert group.status_code == 201, group.text
            group_id = group.json()["id"]

        with connect_database(database_url) as connection:
            conflict = connection.execute(
                """SELECT reason_code,status FROM scim_conflicts
                     WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1""",
                (workspace_id,),
            ).fetchone()
            assert conflict is not None
            assert tuple(conflict) == ("owner_protected", "open")

        mapping_preflight = await owner.options(
            f"/workspaces/{workspace_id}/scim-role-mappings",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": (
                    "content-type,if-match,idempotency-key,x-csrf-token"
                ),
            },
        )
        assert mapping_preflight.status_code == 200, mapping_preflight.text
        assert "PUT" in mapping_preflight.headers["access-control-allow-methods"]

        mapped = await owner.put(
            f"/workspaces/{workspace_id}/scim-role-mappings",
            json={"scim_group_link_id": group_id, "role_code": "developer"},
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{
                    "Idempotency-Key": "prd115-mapping-upsert-0001",
                    "If-Match": '"0"',
                },
            ),
        )
        assert mapped.status_code == 200, mapped.text
        assert mapped.json()["mapping"]["group_display_name"] == "Engineering Directory"
        with connect_database(database_url) as connection:
            verified_projection = connection.execute(
                """SELECT member.role_code,member.lifecycle_source
                     FROM scim_user_links link
                     JOIN workspace_memberships member
                       ON member.workspace_membership_id=link.membership_id
                    WHERE link.scim_user_link_id=?""",
                (verified_member_id,),
            ).fetchone()
            assert verified_projection is not None
            assert tuple(verified_projection) == ("developer", "scim")

        async with AsyncClient(transport=transport, base_url="https://testserver") as machine:
            removed = await machine.patch(
                f"/scim/v2/Groups/{group_id}",
                content=scim_service._canonical(
                    {
                        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                        "Operations": [
                            {
                                "op": "remove",
                                "path": "members",
                                "value": [{"value": verified_member_id}],
                            }
                        ],
                    }
                ),
                headers=_machine_headers(bearer, **{"If-Match": 'W/"1"'}),
            )
            assert removed.status_code == 200, removed.text
            assert removed.headers["etag"] == 'W/"2"'
        with connect_database(database_url) as connection:
            reverted_projection = connection.execute(
                """SELECT member.role_code,member.lifecycle_source
                     FROM scim_user_links link
                     JOIN workspace_memberships member
                       ON member.workspace_membership_id=link.membership_id
                    WHERE link.scim_user_link_id=?""",
                (verified_member_id,),
            ).fetchone()
            assert reverted_projection is not None
            assert tuple(reverted_projection) == ("viewer", "scim")

        summary = await owner.get(f"/workspaces/{workspace_id}/scim-directory")
        assert summary.status_code == 200, summary.text
        assert summary.headers["cache-control"] == "no-store, private"
        assert summary.json()["pending_invitation_count"] == 1
        assert summary.json()["linked_group_count"] == 1
        assert summary.json()["conflicts"][0]["reason_code"] == "owner_protected"
        assert bearer not in summary.text

        conflict_summary = summary.json()["conflicts"][0]
        accept_blocked = await owner.post(
            f"/workspaces/{workspace_id}/scim-conflicts/"
            f"{conflict_summary['scim_conflict_id']}/resolve",
            json={"resolution_code": "accept_directory"},
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{
                    "Idempotency-Key": "prd115-conflict-accept-0001",
                    "If-Match": '"1"',
                },
            ),
        )
        assert accept_blocked.status_code == 409
        assert accept_blocked.json()["code"] == "scim.conflict"

        kept_local = await owner.post(
            f"/workspaces/{workspace_id}/scim-conflicts/"
            f"{conflict_summary['scim_conflict_id']}/resolve",
            json={"resolution_code": "keep_local"},
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{
                    "Idempotency-Key": "prd115-conflict-local-0001",
                    "If-Match": '"1"',
                },
            ),
        )
        assert kept_local.status_code == 200, kept_local.text
        assert kept_local.json()["conflict"]["status"] == "resolved"
        assert kept_local.json()["conflict"]["revision"] == 2
        with connect_database(database_url) as connection:
            owner_projection = connection.execute(
                """SELECT link.state,member.status FROM scim_user_links link
                     JOIN workspace_memberships member
                       ON member.workspace_membership_id=link.membership_id
                    WHERE link.scim_user_link_id=?""",
                (owner_link_id,),
            ).fetchone()
            assert owner_projection is not None
            assert tuple(owner_projection) == ("active", "active")

        token_summary = created_token["token"]
        rotated = await owner.post(
            f"/workspaces/{workspace_id}/scim-tokens/{token_summary['scim_token_id']}/rotate",
            json={"expires_in_days": 60},
            headers=_csrf(
                owner,
                WORKSPACE_CSRF_COOKIE,
                **{
                    "Idempotency-Key": "prd115-token-rotate-0001",
                    "If-Match": '"1"',
                },
            ),
        )
        assert rotated.status_code == 201, rotated.text
        new_bearer = rotated.json()["bearer_token"]
        assert new_bearer != bearer
        async with AsyncClient(transport=transport, base_url="https://testserver") as machine:
            old_denied = await machine.get(
                "/scim/v2/Users", headers={"Authorization": f"Bearer {bearer}"}
            )
            assert old_denied.status_code == 401
            new_allowed = await machine.get(
                "/scim/v2/Users", headers={"Authorization": f"Bearer {new_bearer}"}
            )
            assert new_allowed.status_code == 200

        forbidden_values = {
            bearer,
            new_bearer,
            admin_email,
            "directory.member@example.com",
            VERIFIED_MEMBER_EMAIL,
        }
        with connect_database(database_url) as connection:
            events = connection.execute(
                "SELECT payload_json FROM scim_event_outbox ORDER BY occurred_at"
            ).fetchall()
            assert events
            serialized = "\n".join(row["payload_json"] for row in events)
            for value in forbidden_values:
                assert value not in serialized
