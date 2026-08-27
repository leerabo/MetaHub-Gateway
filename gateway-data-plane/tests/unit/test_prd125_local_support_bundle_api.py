"""PRD-125 local support-bundle API boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime
from secrets import token_urlsafe

import httpx
import pytest
from fastapi import FastAPI
from metahub_gateway.local_secrets import (
    LocalAdminClaims,
    LocalAdminHandoffVerifier,
    LocalAdminSessionSigner,
    LocalManagementPolicy,
    LocalSecretStore,
    LocalSupportBundleStore,
    csrf_sha256,
)
from metahub_gateway.local_secrets.routes import install_local_api_error_handler, router
from metahub_shared.private_gateway import (
    SUPPORT_BUNDLE_COLLECTOR_CODES,
    LocalKek,
    LocalKekKeyring,
)

DEPLOYMENT_ID = "edp_" + "5" * 32
NOW_SECONDS = 1_777_777_777
NOW = datetime(2026, 8, 27, 7, 0, tzinfo=UTC)
ORIGIN = "https://console.example.test"
TENANT_ID = "ten_private_001"
WORKSPACE_ID = "wsp_private_001"


class RecordingUploader:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, bundle, **_owners: str) -> str:
        assert bundle.object_path.read_bytes().startswith(b"MHSB1")
        assert len(_owners["approval_proof_sha256"]) == 64
        assert _owners["approved_at"] == "2026-08-27T07:00:00.000Z"
        self.calls += 1
        return bundle.manifest.bundle_id


def _app(tmp_path) -> tuple[FastAPI, LocalAdminSessionSigner, RecordingUploader]:
    app = FastAPI()
    secret_store = LocalSecretStore(
        tmp_path / "local.sqlite3",
        deployment_id=DEPLOYMENT_ID,
        keyring=LocalKekKeyring(LocalKek(1, bytes(range(32)))),
    )
    uploader = RecordingUploader()
    app.state.local_secret_store = secret_store
    app.state.local_support_bundle_store = LocalSupportBundleStore(
        secret_store.path,
        output_root=tmp_path / "support-bundles",
        deployment_id=DEPLOYMENT_ID,
        encryption_secret=b"prd-125-api-encryption-secret-material",
        input_provider=lambda tenant_id, workspace_id, scanner_key: (
            secret_store.support_bundle_inputs(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                scanner_hmac_key=scanner_key,
            )
        ),
        uploader=uploader,
        ttl_seconds=3600,
        now=lambda: NOW,
    )
    signer = LocalAdminSessionSigner(bytes(reversed(range(32))), now=lambda: NOW_SECONDS)
    app.state.local_admin_session_signer = signer
    app.state.local_admin_handoff_verifier = LocalAdminHandoffVerifier(
        b"prd-125-handoff-secret-material-0001", now=lambda: NOW_SECONDS
    )
    app.state.local_management_policy = LocalManagementPolicy.build(allowed_origins=(ORIGIN,))
    install_local_api_error_handler(app)
    app.include_router(router)
    return app, signer, uploader


def _auth(
    signer: LocalAdminSessionSigner, *, tenant_id: str = TENANT_ID
) -> tuple[dict[str, str], dict[str, str]]:
    csrf = token_urlsafe(32)
    claims = LocalAdminClaims.model_validate(
        {
            "schema_version": "1",
            "session_id": "las_" + "6" * 32,
            "account_id": "acc_private_001",
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
async def test_preview_approve_and_cancel_require_local_consent_cas(tmp_path) -> None:
    app, signer, uploader = _app(tmp_path)
    headers, cookies = _auth(signer)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost", cookies=cookies
    ) as client:
        preview_response = await client.post(
            "/local/support-bundles",
            json={"collector_codes": list(SUPPORT_BUNDLE_COLLECTOR_CODES)},
            headers=headers,
        )
        assert preview_response.status_code == 201
        assert preview_response.headers["cache-control"] == "no-store, private"
        preview = preview_response.json()
        assert preview["status"] == "preview_ready"
        assert [entry["collector_code"] for entry in preview["manifest"]["entries"]] == list(
            SUPPORT_BUNDLE_COLLECTOR_CODES
        )
        assert not {"content", "value", "secret", "path_input"}.intersection(preview)
        assert uploader.calls == 0

        approved_response = await client.post(
            f"/local/support-bundles/{preview['bundle_id']}/approve",
            headers={**headers, "If-Match": str(preview["revision"])},
        )
        assert approved_response.status_code == 200
        approved = approved_response.json()
        assert approved["status"] == "uploaded"
        assert approved["revision"] == 3
        assert uploader.calls == 1

        replay = await client.post(
            f"/local/support-bundles/{preview['bundle_id']}/approve",
            headers={**headers, "If-Match": str(approved["revision"])},
        )
        assert replay.status_code == 409
        assert replay.json() == {"code": "local_support_bundle.state_conflict"}

        cancellable_response = await client.post(
            "/local/support-bundles",
            json={"collector_codes": ["runtime.health"]},
            headers=headers,
        )
        cancellable = cancellable_response.json()
        cancelled = await client.delete(
            f"/local/support-bundles/{cancellable['bundle_id']}",
            headers={**headers, "If-Match": "1"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"


@pytest.mark.anyio
async def test_support_bundle_api_rejects_forgery_ambiguous_json_and_cross_owner(
    tmp_path,
) -> None:
    app, signer, _ = _app(tmp_path)
    headers, cookies = _auth(signer)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost", cookies=cookies
    ) as client:
        valid = await client.post(
            "/local/support-bundles",
            json={"collector_codes": ["runtime.health"]},
            headers=headers,
        )
        bundle_id = valid.json()["bundle_id"]

        duplicate_key = await client.post(
            "/local/support-bundles",
            content=b'{"collector_codes":["runtime.health"],"collector_codes":[]}',
            headers={**headers, "Content-Type": "application/json"},
        )
        assert duplicate_key.status_code == 422
        assert duplicate_key.json() == {"code": "local_support_bundle.request_invalid"}

        duplicate_collector = await client.post(
            "/local/support-bundles",
            json={"collector_codes": ["runtime.health", "runtime.health"]},
            headers=headers,
        )
        assert duplicate_collector.status_code == 422

        forged_csrf = await client.post(
            "/local/support-bundles",
            json={"collector_codes": ["runtime.health"]},
            headers={**headers, "X-CSRF-Token": token_urlsafe(32)},
        )
        assert forged_csrf.status_code == 403
        assert forged_csrf.json() == {"code": "local_admin.csrf_invalid"}

    other_headers, other_cookies = _auth(signer, tenant_id="ten_private_002")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost", cookies=other_cookies
    ) as other:
        hidden = await other.delete(
            f"/local/support-bundles/{bundle_id}",
            headers={**other_headers, "If-Match": "1", "X-Tenant-ID": TENANT_ID},
        )
        assert hidden.status_code == 404
        assert hidden.json() == {"code": "local_support_bundle.not_found"}
