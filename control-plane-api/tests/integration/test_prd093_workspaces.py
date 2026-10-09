"""PRD-093 Workspace API, isolation, idempotency, and projection contracts."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.delivery import (
    clear_test_identity_challenges,
    pop_test_identity_challenge,
)
from metahub_control.identity.routes import router as identity_router
from metahub_control.workspaces.routes import router as workspace_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ADMIN_PASSWORD = "LocalAdmin!2026"
OWNER_PASSWORD = "WorkspaceOwner!2026"
ROTATED_PASSWORD = "WorkspaceOwnerRotated!2026"
CSRF_COOKIE = "__Host-metahub-global-csrf"
SESSION_COOKIE = "__Host-metahub-global-session"


@pytest.fixture(scope="module")
def migrated_workspace_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd093-api-migrated")
    database = root / "workspace-current.sqlite3"
    result = apply_migrations(f"sqlite:///{database.as_posix()}")
    assert "0085" in result.applied
    return database


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_workspace_database: Path,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "workspace.sqlite3"
    shutil.copy2(migrated_workspace_database, database)
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
    monkeypatch.setenv("METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE", "test_capture")
    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("METAHUB_WORKSPACE_ALLOWED_REGIONS", "cn-north-1,cn-east-1")
    apply_seed_data(database_url)
    clear_test_identity_challenges()
    app = create_service_app(
        "control-plane-api",
        "PRD-093 Workspace Test",
        extra_cors_allow_headers=("Idempotency-Key", "If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.include_router(identity_router)
    app.include_router(workspace_router)
    return database_url, app


def _origin_headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site", **extra}


def _csrf_headers(client: AsyncClient, **extra: str) -> dict[str, str]:
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf
    return _origin_headers(**{"X-CSRF-Token": csrf, **extra})


async def _register(
    client: AsyncClient,
    email: str,
    password: str = OWNER_PASSWORD,
) -> tuple[str, str]:
    started = await client.post(
        "/identity/registrations",
        json={
            "email": email,
            "display_name": "Workspace Owner",
            "password": password,
            "consent_version": "privacy-2026-08",
        },
        headers=_origin_headers(),
    )
    assert started.status_code == 200
    intent_id = started.json()["intent_id"]
    challenge = pop_test_identity_challenge("registration", intent_id)
    assert challenge is not None
    verified = await client.post(
        "/identity/verifications",
        json={"intent_id": intent_id, "code": challenge.code},
        headers=_origin_headers(),
    )
    assert verified.status_code == 200
    return intent_id, challenge.code


def _organization(slug: str, name: str = "Acme Research") -> dict[str, str]:
    return {
        "kind": "organization",
        "name": name,
        "slug": slug,
        "region": "cn-north-1",
        "service_mode": "internal_only",
    }


async def _create(
    client: AsyncClient,
    body: dict[str, str],
    key: str,
):
    return await client.post(
        "/workspaces",
        json=body,
        headers=_csrf_headers(client, **{"Idempotency-Key": key}),
    )


def _database_artifacts(database_url: str) -> bytes:
    database = Path(database_url.removeprefix("sqlite:///"))
    artifacts = (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
    return b"".join(path.read_bytes() for path in artifacts if path.exists())


def _tables_containing_text(connection, value: str) -> set[str]:
    matches: set[str] = set()
    tables = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for table_row in tables:
        table = str(table_row[0])
        quoted_table = '"' + table.replace('"', '""') + '"'
        columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted_table})")]
        if not columns:
            continue
        clauses = []
        for column in columns:
            quoted_column = '"' + column.replace('"', '""') + '"'
            clauses.append(f"instr(CAST({quoted_column} AS TEXT),?)>0")
        query = f"SELECT 1 FROM {quoted_table} WHERE {' OR '.join(clauses)} LIMIT 1"
        if connection.execute(query, (value,) * len(clauses)).fetchone() is not None:
            matches.add(table)
    return matches


@pytest.mark.anyio
async def test_organization_create_is_atomic_idempotent_and_has_no_fake_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_workspace_database: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_workspace_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        _intent_id, challenge_code = await _register(client, "owner.atomic@example.com")
        session_token = client.cookies.get(SESSION_COOKIE)
        csrf_token = client.cookies.get(CSRF_COOKIE)
        assert session_token and csrf_token
        body = _organization("acme-research")
        created = await _create(client, body, "workspace-atomic-idempotency-0001")
        assert created.status_code == 201
        assert set(created.json()) == {"workspace", "request_id", "trace_id"}
        workspace = created.json()["workspace"]
        assert set(workspace) == {
            "environment",
            "kind",
            "name",
            "profile_state",
            "provisioning",
            "region",
            "revision",
            "service_mode",
            "slug",
            "status",
            "workspace_id",
        }
        assert workspace["kind"] == "organization"
        assert workspace["profile_state"] == "draft"
        assert workspace["environment"] == {
            "code": "dev",
            "origin": "system_created",
            "status": "active",
        }
        assert workspace["provisioning"] == {"status": "ready"}
        assert "tenant" not in created.text and "account_id" not in created.text
        assert created.headers["cache-control"] == "no-store, private"
        assert created.headers["referrer-policy"] == "no-referrer"
        workspace_id = workspace["workspace_id"]

        replay = await _create(client, body, "workspace-atomic-idempotency-0001")
        assert replay.status_code == 201
        assert replay.json()["workspace"]["workspace_id"] == workspace_id
        mismatch = await _create(
            client,
            _organization("acme-research-2"),
            "workspace-atomic-idempotency-0001",
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["code"] == "workspace.idempotency_conflict"
        listed = await client.get("/workspaces")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["workspaces"][0]["workspace_id"] == workspace_id
        detail = await client.get(f"/workspaces/{workspace_id}")
        assert detail.status_code == 200 and detail.headers["etag"] == '"1"'

    connection = connect_database(database_url)
    try:
        tenant_id = connection.execute(
            "SELECT tenant_id FROM workspace_tenant_bindings WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()[0]
        template_counts = tuple(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(DISTINCT role_code) FROM rbac_role_permission_templates),
                  (SELECT COUNT(*) FROM rbac_permission_templates),
                  (SELECT COUNT(*) FROM rbac_role_permission_templates)
                """
            ).fetchone()
        )
        actual_counts = tuple(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM roles WHERE tenant_id=?),
                  (SELECT COUNT(*) FROM permissions WHERE tenant_id=?),
                  (SELECT COUNT(*) FROM role_permissions WHERE tenant_id=?)
                """,
                (tenant_id, tenant_id, tenant_id),
            ).fetchone()
        )
        assert actual_counts == template_counts
        assert tuple(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM workspace_memberships WHERE workspace_id=?),
                  (SELECT COUNT(*) FROM environments WHERE tenant_id=? AND code='dev'),
                  (SELECT COUNT(*) FROM providers WHERE tenant_id=?),
                  (SELECT COUNT(*) FROM provider_credentials WHERE tenant_id=?),
                  (SELECT COUNT(*) FROM models WHERE tenant_id=?),
                  (SELECT COUNT(*) FROM virtual_keys WHERE tenant_id=?),
                  (SELECT COUNT(*) FROM projects WHERE tenant_id=?),
                  (SELECT COUNT(*) FROM workspace_events WHERE workspace_id=?),
                  (SELECT COUNT(*) FROM workspace_creation_requests WHERE workspace_id=?)
                """,
                (
                    workspace_id,
                    tenant_id,
                    tenant_id,
                    tenant_id,
                    tenant_id,
                    tenant_id,
                    tenant_id,
                    workspace_id,
                    workspace_id,
                ),
            ).fetchone()
        ) == (1, 1, 0, 0, 0, 0, 0, 1, 1)
        event = connection.execute(
            "SELECT payload_json FROM workspace_events WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()[0]
        assert set(json.loads(event)) == {
            "environment_origin",
            "kind",
            "profile_state",
            "region_code",
            "schema_version",
        }
        artifact = "\n".join(
            str(value)
            for table in ("workspace_events", "workspace_creation_requests")
            for row in connection.execute(f"SELECT * FROM {table}")
            for value in row
        )
        assert "Acme Research" not in artifact
        assert "owner.atomic@example.com" not in artifact
        assert "workspace-atomic-idempotency-0001" not in artifact
        assert _tables_containing_text(connection, "Acme Research") == {
            "organization_profiles",
            "tenants",
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    persisted = _database_artifacts(database_url)
    for secret in (
        OWNER_PASSWORD,
        challenge_code,
        session_token,
        csrf_token,
        "workspace-atomic-idempotency-0001",
    ):
        assert secret.encode("utf-8") not in persisted
        assert secret not in caplog.text
    assert "Acme Research" not in caplog.text


@pytest.mark.anyio
async def test_retired_personal_creation_region_and_cross_account_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_workspace_database: Path,
) -> None:
    _database_url, app = _prepare(monkeypatch, tmp_path, migrated_workspace_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as first:
        await _register(first, "personal.owner@example.com")
        personal_body = {
            "kind": "personal",
            "region": "cn-east-1",
            "service_mode": "internal_only",
        }
        created = await _create(first, personal_body, "workspace-personal-idempotency-01")
        assert created.status_code == 422
        assert created.json()["code"] == "workspace.personal_creation_disabled"
        assert (await first.get("/workspaces")).json()["workspaces"] == []
        organization = await _create(first, _organization("retired-personal-owner", "Team"),
                                     "workspace-organization-idempotency-02")
        assert organization.status_code == 201
        workspace_id = organization.json()["workspace"]["workspace_id"]
        invalid_region = await _create(
            first,
            {**personal_body, "region": "us-east-1"},
            "workspace-personal-idempotency-03",
        )
        assert invalid_region.status_code == 422
        assert invalid_region.json()["code"] == "workspace.region_invalid"
        injected = await _create(
            first,
            {**personal_body, "tenant_id": "tenant_default"},
            "workspace-personal-idempotency-04",
        )
        assert injected.status_code == 422

    async with AsyncClient(transport=transport, base_url="https://testserver") as second:
        await _register(second, "other.owner@example.com")
        hidden = await second.get(f"/workspaces/{workspace_id}")
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "workspace.not_found"


@pytest.mark.anyio
async def test_profile_cas_and_multi_workspace_password_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_workspace_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_workspace_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        await _register(client, "multi.workspace@example.com")
        first = await _create(
            client,
            _organization("multi-workspace-one", "Workspace One"),
            "workspace-multi-idempotency-0001",
        )
        second = await _create(
            client,
            _organization("multi-workspace-two", "Workspace Two"),
            "workspace-multi-idempotency-0002",
        )
        assert first.status_code == second.status_code == 201
        first_id = first.json()["workspace"]["workspace_id"]
        confirmed = await client.patch(
            f"/workspaces/{first_id}",
            json={"name": "Workspace One Confirmed", "profile_state": "confirmed"},
            headers=_csrf_headers(client, **{"If-Match": '"1"'}),
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["workspace"]["revision"] == 2
        assert confirmed.json()["workspace"]["profile_state"] == "confirmed"
        assert confirmed.headers["etag"] == '"2"'
        stale = await client.patch(
            f"/workspaces/{first_id}",
            json={"name": "Stale"},
            headers=_csrf_headers(client, **{"If-Match": '"1"'}),
        )
        assert stale.status_code == 412
        rotated = await client.post(
            "/identity/credentials/password/rotate",
            json={"current_password": OWNER_PASSWORD, "new_password": ROTATED_PASSWORD},
            headers=_csrf_headers(client),
        )
        assert rotated.status_code == 200

    connection = connect_database(database_url)
    try:
        account = connection.execute(
            "SELECT account_id,credential_revision FROM global_accounts WHERE email_normalized=?",
            ("multi.workspace@example.com",),
        ).fetchone()
        assert account is not None and account["credential_revision"] == 2
        links = connection.execute(
            """
            SELECT user.password_hash
            FROM account_identity_links link
            JOIN users user ON user.tenant_id=link.legacy_tenant_id
              AND user.user_id=link.legacy_user_id
            WHERE link.account_id=? ORDER BY user.tenant_id
            """,
            (account["account_id"],),
        ).fetchall()
        assert len(links) == 2 and len({row["password_hash"] for row in links}) == 1
        credential_hash = connection.execute(
            "SELECT secret_hash FROM account_credentials WHERE account_id=?",
            (account["account_id"],),
        ).fetchone()[0]
        assert links[0]["password_hash"] == credential_hash
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_events WHERE account_id=?",
            (account["account_id"],),
        ).fetchone()[0] == 3
    finally:
        connection.close()

    persisted = _database_artifacts(database_url)
    assert OWNER_PASSWORD.encode("utf-8") not in persisted
    assert ROTATED_PASSWORD.encode("utf-8") not in persisted


@pytest.mark.anyio
async def test_warm_workspace_core_creation_meets_p95_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_workspace_database: Path,
) -> None:
    _database_url, app = _prepare(monkeypatch, tmp_path, migrated_workspace_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        await _register(client, "workspace.performance@example.com")
        warm = await _create(
            client,
            _organization("workspace-perf-warm"),
            "workspace-performance-warm-0001",
        )
        assert warm.status_code == 201

        samples: list[float] = []
        for index in range(12):
            started_at = time.perf_counter()
            response = await _create(
                client,
                _organization(f"workspace-perf-{index:02d}"),
                f"workspace-performance-{index:04d}",
            )
            samples.append(time.perf_counter() - started_at)
            assert response.status_code == 201

    p95 = sorted(samples)[math.ceil(len(samples) * 0.95) - 1]
    assert p95 < 1.0, samples


@pytest.mark.anyio
@pytest.mark.parametrize("generated_slug", [False, True])
async def test_unverified_legacy_account_is_rejected_and_concurrent_replay_is_singleton(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_workspace_database: Path,
    generated_slug: bool,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_workspace_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as legacy:
        login = await legacy.post(
            "/identity/login",
            json={"email": "admin@metahub.local", "password": ADMIN_PASSWORD},
            headers=_origin_headers(),
        )
        assert login.status_code == 200
        rejected = await _create(
            legacy,
            _organization("legacy-unverified"),
            "workspace-legacy-unverified-0001",
        )
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "workspace.account_unverified"

    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        await _register(client, "concurrent.owner@example.com")
        body = _organization("concurrent-singleton")
        if generated_slug:
            body = {"kind": "organization", "name": "Concurrent Team"}
        responses = await asyncio.gather(
            *(
                _create(client, body, "workspace-concurrent-idempotency-0001")
                for _index in range(100)
            )
        )
        assert {response.status_code for response in responses} == {201}
        workspace_ids = {
            response.json()["workspace"]["workspace_id"] for response in responses
        }
        assert len(workspace_ids) == 1

    connection = connect_database(database_url)
    try:
        account_id = connection.execute(
            "SELECT account_id FROM global_accounts WHERE email_normalized=?",
            ("concurrent.owner@example.com",),
        ).fetchone()[0]
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_memberships WHERE account_id=?",
            (account_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_creation_requests WHERE account_id=?",
            (account_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()
