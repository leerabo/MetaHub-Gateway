from __future__ import annotations

from datetime import UTC, datetime
from secrets import token_urlsafe

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from metahub_gateway.local_secrets import (
    LocalAdminClaims,
    LocalAdminHandoffVerifier,
    LocalAdminSessionSigner,
    LocalBackupStore,
    LocalManagementPolicy,
    LocalSecretStore,
    csrf_sha256,
)
from metahub_gateway.local_secrets.keystore import TestKmsProtector as KmsProtector
from metahub_gateway.local_secrets.routes import install_local_api_error_handler, router
from metahub_shared.private_gateway import BACKUP_DOMAIN_CODES, LocalKek, LocalKekKeyring

DEPLOYMENT_ID = "edp_" + "6" * 32
NOW_SECONDS = 1_777_777_777
NOW = datetime(2026, 8, 28, 13, 0, tzinfo=UTC)
ORIGIN = "https://console.example.test"
TENANT_ID = "tenant_prd129"
WORKSPACE_ID = "workspace_prd129"


def _domains() -> dict[str, bytes]:
    return {code: (code + "-snapshot").encode("ascii") for code in BACKUP_DOMAIN_CODES}


def _app(tmp_path):
    app = FastAPI()
    app.state.local_secret_store = LocalSecretStore(
        tmp_path / "local-secrets.sqlite3",
        deployment_id=DEPLOYMENT_ID,
        keyring=LocalKekKeyring(LocalKek(1, bytes(range(32)))),
        now=lambda: NOW,
    )
    protector = KmsProtector(
        "prd-129-local-api-kms-master-secret-material",
        DEPLOYMENT_ID,
    )
    applied: list[dict[str, bytes]] = []
    app.state.local_backup_store = LocalBackupStore(
        tmp_path / "journal.sqlite3",
        output_root=tmp_path / "artifacts",
        deployment_id=DEPLOYMENT_ID,
        schema_head="0121",
        signing_key=Ed25519PrivateKey.generate(),
        signing_key_id="edge-backup-local-api-v1",
        kms_key_id="test/customer/local-api-key",
        kms_key_version=1,
        wrap_data_key=lambda value: protector.protect(
            value, purpose="edge-backup-data-key"
        ),
        unwrap_data_key=lambda value: protector.unprotect(
            value, purpose="edge-backup-data-key"
        ),
        domain_provider=lambda tenant_id, workspace_id: (
            _domains()
            if (tenant_id, workspace_id) == (TENANT_ID, WORKSPACE_ID)
            else {}
        ),
        restore_applier=lambda domains, **_owners: applied.append(dict(domains)),
        now=lambda: NOW,
    )
    signer = LocalAdminSessionSigner(bytes(reversed(range(32))), now=lambda: NOW_SECONDS)
    app.state.local_admin_session_signer = signer
    app.state.local_admin_handoff_verifier = LocalAdminHandoffVerifier(
        b"prd-129-handoff-secret-material-0001", now=lambda: NOW_SECONDS
    )
    app.state.local_management_policy = LocalManagementPolicy.build(
        allowed_origins=(ORIGIN,)
    )
    install_local_api_error_handler(app)
    app.include_router(router)
    return app, signer, applied


def _auth(signer: LocalAdminSessionSigner, *, tenant_id: str = TENANT_ID):
    csrf = token_urlsafe(32)
    claims = LocalAdminClaims.model_validate(
        {
            "schema_version": "1",
            "session_id": "las_" + "7" * 32,
            "account_id": "account_prd129",
            "tenant_id": tenant_id,
            "workspace_id": WORKSPACE_ID,
            "deployment_id": DEPLOYMENT_ID,
            "role": "tenant_security_admin",
            "csrf_sha256": csrf_sha256(csrf),
            "issued_at": NOW_SECONDS - 30,
            "expires_at": NOW_SECONDS + 600,
            "step_up_expires_at": NOW_SECONDS + 300,
        }
    )
    return (
        {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": csrf,
        },
        {
            "__Host-metahub-local-session": signer.issue(claims),
            "__Host-metahub-local-csrf": csrf,
        },
    )


@pytest.mark.anyio
async def test_local_backup_and_restore_api_keep_artifacts_out_of_http(tmp_path) -> None:
    app, signer, applied = _app(tmp_path)
    headers, cookies = _auth(signer)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost", cookies=cookies
    ) as client:
        created = await client.post(
            "/local/backups",
            headers=headers,
            json={
                "source_generation": 12,
                "retention_seconds": 86400,
                "storage_kind": "local",
            },
        )
        assert created.status_code == 201, created.text
        backup = created.json()
        assert backup["status"] == "ready"
        assert not {"chunks", "envelope", "path", "secret"}.intersection(backup)
        assert "config_policy-snapshot" not in created.text

        restored = await client.post(
            "/local/restores",
            headers=headers,
            json={"backup_id": backup["backup_id"], "target_schema_head": "0121"},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "succeeded"
        assert applied == [_domains()]


@pytest.mark.anyio
async def test_local_backup_api_rejects_forgery_ambiguous_json_and_cross_owner(
    tmp_path,
) -> None:
    app, signer, _ = _app(tmp_path)
    headers, cookies = _auth(signer)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost", cookies=cookies
    ) as client:
        created = await client.post(
            "/local/backups",
            headers=headers,
            json={
                "source_generation": 12,
                "retention_seconds": 86400,
                "storage_kind": "local",
            },
        )
        backup_id = created.json()["backup_id"]
        duplicate = await client.post(
            "/local/backups",
            headers={**headers, "Content-Type": "application/json"},
            content=(
                b'{"source_generation":12,"source_generation":13,'
                b'"retention_seconds":86400,"storage_kind":"local"}'
            ),
        )
        assert duplicate.status_code == 422
        assert duplicate.json() == {"code": "local_backup.request_invalid"}

        forged = await client.post(
            "/local/backups",
            headers={**headers, "X-CSRF-Token": token_urlsafe(32)},
            json={
                "source_generation": 13,
                "retention_seconds": 86400,
                "storage_kind": "local",
            },
        )
        assert forged.status_code == 403
        assert forged.json() == {"code": "local_admin.csrf_invalid"}

    other_headers, other_cookies = _auth(signer, tenant_id="tenant_other")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost", cookies=other_cookies
    ) as other:
        hidden = await other.post(
            "/local/restores",
            headers=other_headers,
            json={"backup_id": backup_id, "target_schema_head": "0121"},
        )
        assert hidden.status_code == 404
        assert hidden.json() == {"code": "local_backup.not_found"}


@pytest.mark.anyio
async def test_local_restore_api_accepts_only_exact_cloud_drill_binding(tmp_path) -> None:
    app, signer, _ = _app(tmp_path)
    headers, cookies = _auth(signer)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost", cookies=cookies
    ) as client:
        created = await client.post(
            "/local/backups",
            headers=headers,
            json={
                "source_generation": 12,
                "retention_seconds": 86400,
                "storage_kind": "local",
            },
        )
        backup = created.json()
        queued = app.state.local_backup_store.next_cloud_report()
        assert queued is not None
        app.state.local_backup_store.acknowledge_cloud_report(
            queued.report_id, queued.payload_sha256
        )
        restored = await client.post(
            "/local/restores",
            headers=headers,
            json={
                "backup_id": backup["backup_id"],
                "target_schema_head": "0121",
                "drill_context": {
                    "drill_id": "pdd_" + "1" * 32,
                    "restore_job_id": "rst_" + "2" * 32,
                    "expected_revision": 2,
                    "source_backup_completed_at": backup["created_at"],
                    "device_reenrollment_evidence_sha256": "3" * 64,
                    "minimal_request_evidence_sha256": "4" * 64,
                },
            },
        )
        assert restored.status_code == 200, restored.text
        report = app.state.local_backup_store.next_cloud_report()
        assert report is not None and report.report_kind == "drill"

        unknown = await client.post(
            "/local/restores",
            headers=headers,
            json={
                "backup_id": backup["backup_id"],
                "target_schema_head": "0121",
                "drill_context": {
                    "drill_id": "pdd_" + "5" * 32,
                    "restore_job_id": "rst_" + "6" * 32,
                    "expected_revision": 2,
                    "source_backup_completed_at": backup["created_at"],
                    "device_reenrollment_evidence_sha256": "7" * 64,
                    "minimal_request_evidence_sha256": "8" * 64,
                    "path": "forbidden",
                },
            },
        )
        assert unknown.status_code == 422
        assert unknown.json() == {"code": "local_restore.request_invalid"}
