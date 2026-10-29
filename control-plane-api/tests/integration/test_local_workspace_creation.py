"""Dev account admission is workspace-specific and never invents mailbox verification."""

import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity import registration_policy
from metahub_control.workspaces import service as workspace_service
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.security import hash_secret
from metahub_shared.runtime.errors import ApiError
from migration_lifecycle_support import rollback_later_migrations
from test_prd095_platform_iam import GLOBAL_CSRF_COOKIE, _csrf, _prepare
from test_prd095_platform_iam import migrated_database as migrated_database

ROOT = Path(__file__).resolve().parents[3]
ORIGIN = "http://localhost:5173"
BODY = {
    "kind": "organization",
    "name": "Local Organization",
    "slug": "local-organization",
    "region": "cn-north-1",
    "service_mode": "internal_only",
}


async def _signup(client, monkeypatch):
    monkeypatch.setattr(registration_policy.secrets, "choice", lambda choices: choices[0])
    challenge = (await client.get("/identity/registration-captcha")).json()
    result = await client.post(
        "/identity/registrations",
        headers={"Origin": ORIGIN},
        json={
            "email": "organization-owner@example.test",
            "display_name": "Trial Owner",
            "password": "OrganizationTrial!2026",
            "consent_version": "privacy-local-2026-09-09",
            "captcha_id": challenge["challenge_id"],
            "captcha_answer": "22222",
        },
    )
    assert result.status_code == 200, result.text
    return result.json()["account"]


def _dev_app(monkeypatch, tmp_path, migrated_database):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    app.state.config = app.state.config.model_copy(
        update={
            "environment": "dev",
            "identity_challenge_delivery_mode": "disabled",
        }
    )
    return database, app


@pytest.mark.anyio
async def test_local_signup_creates_owner_and_switches_without_mailbox_proof(
    monkeypatch,
    tmp_path,
    migrated_database,
):
    database, app = _dev_app(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _signup(client, monkeypatch)
        initial = await client.get("/identity/workspaces")
        assert initial.status_code == 200 and initial.json()["workspaces"] == []
        headers = {
            **_csrf(client, GLOBAL_CSRF_COOKIE),
            "Idempotency-Key": "local-workspace-create-001",
        }
        no_csrf = await client.post("/workspaces", json=BODY, headers={
            "Origin": ORIGIN, "Idempotency-Key": "local-workspace-no-csrf-001",
        })
        assert no_csrf.status_code == 403
        retired = await client.post("/workspaces", json={"kind": "personal"}, headers={
            **headers, "Idempotency-Key": "retired-personal-workspace-001",
        })
        assert retired.status_code == 422, retired.text
        assert retired.json()["code"] == "workspace.kind_invalid"
        assert (await client.get("/identity/workspaces")).json()["workspaces"] == []
        name_only = {"kind": "organization", "name": "研发团队"}
        created = await client.post("/workspaces", json=name_only, headers=headers)
        assert created.status_code == 201, created.text
        workspace_id = created.json()["workspace"]["workspace_id"]
        workspace = created.json()["workspace"]
        assert workspace["name"] == "研发团队"
        assert workspace["slug"] == "org-" + workspace_id[4:]
        assert workspace["region"] == app.state.config.workspace_allowed_regions[0]
        assert workspace["service_mode"] == "internal_only"
        app.state.config = app.state.config.model_copy(update={
            "workspace_allowed_regions": tuple(
                reversed(app.state.config.workspace_allowed_regions)
            ),
        })
        replay = await client.post("/workspaces", json=name_only, headers=headers)
        assert replay.status_code == 201, replay.text
        assert replay.json()["workspace"]["workspace_id"] == workspace_id
        assert replay.json()["workspace"]["region"] == workspace["region"]
        conflict = await client.post(
            "/workspaces", json={**name_only, "name": "另一团队"}, headers=headers,
        )
        assert conflict.status_code == 409
        for invalid in (
            {"slug": ""}, {"slug": None}, {"slug": "组织"}, {"slug": "admin"},
            {"region": ""}, {"region": None}, {"region": "not-available"},
            {"service_mode": ""}, {"service_mode": None}, {"service_mode": "invalid"},
            {"owner": "platform_admin"},
        ):
            rejected = await client.post("/workspaces", json={**name_only, **invalid},
                headers={**headers, "Idempotency-Key": "invalid-name-only-request-001"})
            assert rejected.status_code == 422, (tuple(invalid), rejected.status_code)
        choices = await client.get("/identity/workspaces")
        assert choices.json()["workspaces"][0]["role"] == "owner"
        opened = await client.post(
            "/identity/workspace-switches",
            json={"workspace_id": workspace_id},
            headers=_csrf(client, GLOBAL_CSRF_COOKIE),
        )
        assert opened.status_code == 200, opened.text
        current = await client.get("/workspace/session")
        assert current.status_code == 200, current.text
        assert current.json()["context"]["role"] == "owner"
        assert (await client.get("/platform/registration-policy")).status_code in (400, 401, 403)
    connection = connect_database(database)
    try:
        admission = connection.execute(
            "SELECT * FROM local_workspace_creation_admissions"
        ).fetchone()
        assert (
            admission["workspace_id"] == workspace_id and admission["source_environment"] == "dev"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM account_verifications WHERE account_id=?",
                (admission["account_id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM local_workspace_creation_admissions"
            ).fetchone()[0]
            == 1
        )
        for table in ("providers", "models", "virtual_keys"):
            assert (
                connection.execute(
                    f"SELECT count(*) FROM {table} WHERE tenant_id=?", (admission["tenant_id"],)
                ).fetchone()[0]
                == 0
            )
        for statement in (
            "UPDATE local_workspace_creation_admissions SET policy_revision=2",
            "DELETE FROM local_workspace_creation_admissions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(statement)
            connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.executescript(
                (ROOT / "infra/migrations/0147_local_workspace_creation.down.sql").read_text()
            )
        connection.rollback()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        manifest = connection.execute(
            "SELECT disposition FROM tenant_deletion_table_manifest "
            "WHERE source_table='local_workspace_creation_admissions'"
        ).fetchone()
        assert manifest[0] == "archive_then_delete"
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='trg_users_projection_claim_validate_prd093'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT trigger_sha256 FROM tenant_deletion_trigger_manifest "
            "WHERE trigger_name='trg_users_projection_claim_validate_prd093'"
        ).fetchone()[0] == hash_secret(trigger)
    finally:
        connection.close()


@pytest.mark.anyio
@pytest.mark.parametrize("restriction", ["prod", "test", "email_enabled"])
async def test_local_consent_never_bypasses_current_environment_or_policy(
    monkeypatch,
    tmp_path,
    migrated_database,
    restriction,
):
    database, app = _dev_app(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _signup(client, monkeypatch)
        if restriction == "email_enabled":
            connection = connect_database(database)
            connection.execute(
                "UPDATE registration_policy SET email_verification=1,revision=revision+1 "
                "WHERE environment='dev'"
            )
            connection.commit()
            connection.close()
        else:
            app.state.config = app.state.config.model_copy(update={"environment": restriction})
        result = await client.post(
            "/workspaces",
            json={**BODY, "local_consent_version": workspace_service.LOCAL_CREATION_CONSENT_VERSION},
            headers={
                **_csrf(client, GLOBAL_CSRF_COOKIE),
                "Idempotency-Key": "local-workspace-rejected-001",
            },
        )
        assert result.status_code == 403, result.text
        assert result.json()["code"] == "workspace.account_unverified"
    connection = connect_database(database)
    try:
        assert (
            connection.execute(
                "SELECT count(*) FROM local_workspace_creation_admissions"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM workspaces WHERE creation_source='user_created'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_existing_unverified_account_can_explicitly_create_in_local_trial(
    monkeypatch, tmp_path, migrated_database,
):
    database, app = _dev_app(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        await _signup(client, monkeypatch)
        connection = connect_database(database)
        # Build a legacy-style account without fabricating or deleting proof.
        connection.execute("INSERT INTO global_accounts(account_id,email_normalized,display_name,status,"
            "login_enabled,credential_revision,session_revision,mfa_state,failed_login_count,created_at,updated_at) "
            "SELECT 'gac_1234567890abcdef1234567890abcdef','legacy-employee@example.test','Employee',status,"
            "login_enabled,credential_revision,session_revision,mfa_state,failed_login_count,created_at,updated_at "
            "FROM global_accounts WHERE email_normalized='organization-owner@example.test'")
        connection.execute("INSERT INTO account_credentials(credential_id,account_id,credential_type,algorithm,"
            "secret_hash,status,version,created_at,updated_at) "
            "SELECT 'acr_1234567890abcdef1234567890abcdef','gac_1234567890abcdef1234567890abcdef',"
            "credential_type,algorithm,secret_hash,status,version,created_at,updated_at FROM account_credentials "
            "WHERE account_id=(SELECT account_id FROM global_accounts WHERE email_normalized='organization-owner@example.test')")
        connection.commit()
        connection.close()
        login = await client.post('/identity/login', json={
            'email': 'legacy-employee@example.test', 'password': 'OrganizationTrial!2026',
        }, headers={'Origin': ORIGIN})
        assert login.status_code == 200, login.text
        headers = {**_csrf(client, GLOBAL_CSRF_COOKIE), "Idempotency-Key": "existing-local-create-001"}
        denied = await client.post("/workspaces", json=BODY, headers=headers)
        assert denied.status_code == 403 and denied.json()["code"] == "workspace.account_unverified"
        body = {**BODY, "local_consent_version": workspace_service.LOCAL_CREATION_CONSENT_VERSION}
        no_csrf = await client.post("/workspaces", json=body, headers={
            "Origin": ORIGIN, "Idempotency-Key": "existing-local-no-csrf-001",
        })
        assert no_csrf.status_code == 403
        bad_consent = await client.post("/workspaces", json={**body, "local_consent_version": "unknown"}, headers=headers)
        assert bad_consent.status_code == 422
        original_initialize = workspace_service._initialize_operational_state
        def fail_initialization(*args):
            raise ApiError("workspace.unavailable")
        monkeypatch.setattr(workspace_service, "_initialize_operational_state", fail_initialization)
        failed = await client.post("/workspaces", json=body, headers=headers)
        assert failed.status_code == 503
        connection = connect_database(database)
        assert connection.execute("SELECT count(*) FROM local_registration_consents WHERE "
            "account_id='gac_1234567890abcdef1234567890abcdef'").fetchone()[0] == 0
        connection.close()
        monkeypatch.setattr(workspace_service, "_initialize_operational_state", original_initialize)
        created = await client.post("/workspaces", json=body, headers=headers)
        assert created.status_code == 201, created.text
        replay = await client.post("/workspaces", json=body, headers=headers)
        assert replay.status_code == 201 and replay.json()["workspace"] == created.json()["workspace"]
        workspace_id = created.json()["workspace"]["workspace_id"]
        switched = await client.post("/identity/workspace-switches", json={"workspace_id": workspace_id},
                                    headers=_csrf(client, GLOBAL_CSRF_COOKIE))
        assert switched.status_code == 200
        assert (await client.get("/workspace/session")).json()["context"]["role"] == "owner"
    connection = connect_database(database)
    try:
        assert connection.execute("SELECT count(*) FROM local_registration_consents WHERE "
            "account_id='gac_1234567890abcdef1234567890abcdef'").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM local_workspace_creation_admissions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM account_verifications WHERE account_id IN "
            "(SELECT account_id FROM local_registration_consents)").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.anyio
async def test_local_creation_rollback_keeps_no_partial_owner_or_admission(
    monkeypatch,
    tmp_path,
    migrated_database,
):
    database, app = _dev_app(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _signup(client, monkeypatch)

        def fail(*args):
            raise ApiError("workspace.unavailable")

        monkeypatch.setattr(workspace_service, "_verify_core_projection", fail)
        result = await client.post(
            "/workspaces",
            json=BODY,
            headers={
                **_csrf(client, GLOBAL_CSRF_COOKIE),
                "Idempotency-Key": "local-workspace-rollback-001",
            },
        )
        assert result.status_code == 503, result.text
    connection = connect_database(database)
    try:
        for table in (
            "local_workspace_creation_admissions",
            "workspace_tenant_creation_claims",
            "workspace_user_projection_claims",
            "workspace_creation_requests",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM tenants WHERE slug=?", (BODY["slug"],)
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_local_workspace_pristine_down_up_and_unverified_without_consent(
    monkeypatch,
    tmp_path,
    migrated_database,
):
    database, app = _dev_app(monkeypatch, tmp_path, migrated_database)
    later = rollback_later_migrations(database, "0147")
    assert rollback_last_migration(database, expected_version="0147").rolled_back == "0147"
    assert apply_migrations(database).applied == ["0147", *later]
    connection = connect_database(database)
    try:
        with pytest.raises(ApiError, match="verified"):
            workspace_service._require_workspace_creation_identity(
                connection, app.state.config, "missing-account"
            )
    finally:
        connection.close()
