"""PRD-100 Platform infrastructure API, backup state, and credential isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_infrastructure import routes as infrastructure_routes
from metahub_control.platform_infrastructure import service as infrastructure_service
from metahub_control.platform_infrastructure.routes import router
from metahub_control.platform_infrastructure.service import (
    claim_backup,
    complete_backup,
    mark_backup_uploading,
    mark_backup_verifying,
)
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    database_url = f"sqlite:///{(tmp_path / 'prd100.sqlite3').as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": "LocalAdmin!2026",
        "METAHUB_AUTH_TOKEN_SECRET": "test-prd100-auth-token-secret-2026",
        "METAHUB_IDENTITY_SESSION_SECRET": "test-prd100-global-session-secret-2026",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-prd100-challenge-secret-2026",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": "test-prd100-pii-secret-independent-2026",
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-prd100-workspace-secret-2026-long",
        "METAHUB_PLATFORM_SESSION_SECRET": "test-prd100-platform-secret-2026-long",
        "METAHUB_SUPPORT_SESSION_SECRET": "test-prd100-support-secret-2026-long",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "test-prd100-mfa-secret-2026-long-value",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": "test-prd100-recovery-secret-2026-long",
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    apply_migrations(database_url)
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        account_id = str(
            connection.execute("SELECT account_id FROM global_accounts LIMIT 1").fetchone()[0]
        )
        operator_id = "pto_" + "1" * 32
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
        platform_session_id="pts_prd100_test",
        operator_id=operator_id,
        account_id=account_id,
        global_session_id="gis_prd100_test",
        factor_id="mfa_prd100_test",
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=(
            "platform.infrastructure.read",
            "platform.infrastructure.manage",
            "platform.backup.create",
            "platform.key_security.read",
        ),
        expires_at=datetime(2026, 8, 23, tzinfo=UTC),
    )
    monkeypatch.setattr(
        infrastructure_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: context,
    )
    monkeypatch.setattr(
        infrastructure_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: context,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-100 Test",
        extra_cors_allow_headers=("Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.include_router(router)
    return database_url, app


def _write_headers() -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": "c" * 43,
        "Idempotency-Key": "prd100-backup-request-0001",
    }


@pytest.mark.anyio
async def test_status_backup_idempotency_and_verified_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        client.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        status = await client.get("/platform/infrastructure/status")
        assert status.status_code == 200, status.text
        projection = status.json()
        assert len(projection["resources"]) == 4
        assert projection["resources"][0]["provider_code"] == "sqlite_local"
        assert all("object_key" not in item for item in projection["resources"])

        payload = {
            "backup_type": "full",
            "scope_kind": "platform",
            "workspace_id": None,
            "tenant_id": None,
            "retention_days": 30,
            "confirmed": True,
        }
        created = await client.post("/platform/backups", json=payload, headers=_write_headers())
        assert created.status_code == 201, created.text
        backup_id = created.json()["backup"]["backup_id"]
        assert created.json()["backup"]["status"] == "requested"
        assert "object_key" not in created.json()["backup"]
        replay = await client.post("/platform/backups", json=payload, headers=_write_headers())
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True

        claim = claim_backup(app.state.config, worker_id="prd100-worker-0001")
        assert claim is not None and claim.backup_id == backup_id
        content = "a" * 64
        manifest = "b" * 64
        mark_backup_uploading(
            app.state.config,
            backup_id=backup_id,
            lease_token=claim.lease_token,
            source_watermark="sqlite-data-version:1",
            size_bytes=1024,
            content_sha256=content,
            schema_head="0092",
        )
        mark_backup_verifying(
            app.state.config,
            backup_id=backup_id,
            lease_token=claim.lease_token,
            object_key=f"backups/v1/{backup_id[:8]}/{backup_id}.mhb",
            manifest_sha256=manifest,
            kms_key_version="test-v1",
        )
        complete_backup(
            app.state.config,
            backup_id=backup_id,
            lease_token=claim.lease_token,
            restored_schema_head="0092",
            manifest_sha256=manifest,
            tenant_owner_sample_count=1,
        )
        detail = await client.get(f"/platform/backups/{backup_id}")
        assert detail.status_code == 200
        assert detail.json()["backup"]["status"] == "ready"
        assert detail.json()["backup"]["verification"]["integrity_status"] == "ok"

        mixed = await client.get(
            "/platform/infrastructure/status",
            headers={"Authorization": "Bearer forbidden"},
        )
        assert mixed.status_code == 400
        assert mixed.json()["code"] == "platform.credential_conflict"

        for conflicting_cookie in (WORKSPACE_SESSION_COOKIE, SUPPORT_SESSION_COOKIE):
            client.cookies.set(conflicting_cookie, "x" * 43)
            conflicted = await client.get("/platform/infrastructure/status")
            assert conflicted.status_code == 400
            assert conflicted.json()["code"] == "platform.credential_conflict"
            client.cookies.delete(conflicting_cookie)

        security_context = PlatformContext(
            platform_session_id="pts_prd100_security",
            operator_id="pto_" + "2" * 32,
            account_id="gac_" + "3" * 32,
            global_session_id="gis_prd100_security",
            factor_id="mfa_prd100_security",
            factor_revision=1,
            operator_revision=1,
            roles=("platform_security",),
            permissions=("platform.infrastructure.read", "platform.key_security.read"),
            expires_at=datetime(2026, 8, 24, tzinfo=UTC),
        )
        monkeypatch.setattr(
            infrastructure_routes,
            "authenticate_platform_session_context",
            lambda *args, **kwargs: security_context,
        )
        denied = await client.post(
            "/platform/backups",
            json=payload,
            headers={**_write_headers(), "Idempotency-Key": "prd100-security-denied-01"},
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "platform.permission_denied"

    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM infrastructure_event_outbox WHERE event_type='backup.completed'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
