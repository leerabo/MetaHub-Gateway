"""PRD-125 encrypted support-bundle streaming upload protocol tests."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from metahub_gateway.local_secrets import (
    CloudSupportBundleUploader,
    SupportBundleUploadError,
)
from metahub_shared.private_gateway import (
    EncryptedSupportBundle,
    SupportBundleEntry,
    SupportBundleManifest,
    canonical_edge_json_bytes,
    support_bundle_approval_proof_sha256,
    support_bundle_manifest_sha256,
)

BUNDLE_ID = "esb_" + "7" * 32
DEPLOYMENT_ID = "edp_" + "8" * 32
EDGE_SESSION = "egs_" + "s" * 43


def _bundle(tmp_path) -> EncryptedSupportBundle:
    ciphertext = b"MHSB1" + b"n" * 12 + b"ciphertext" + b"t" * 16
    path = tmp_path / f"{BUNDLE_ID}.enc"
    path.write_bytes(ciphertext)
    manifest = SupportBundleManifest(
        schema_version="metahub.support-bundle/v1",
        bundle_id=BUNDLE_ID,
        deployment_id=DEPLOYMENT_ID,
        scan_version="support-bundle-scan-v1",
        scan_outcome="clean",
        entries=(
            SupportBundleEntry(
                collector_code="runtime.health",
                path="health/summary.json",
                classification="health",
                media_type="application/json",
                size_bytes=10,
                sha256="a" * 64,
            ),
        ),
        total_uncompressed_bytes=10,
        archive_size_bytes=len(ciphertext) - 33,
        archive_sha256="b" * 64,
        created_at="2026-08-27T08:00:00.000Z",
        expires_at="2026-08-27T09:00:00.000Z",
    )
    return EncryptedSupportBundle(
        manifest=manifest,
        object_path=path,
        object_sha256=hashlib.sha256(ciphertext).hexdigest(),
        object_size_bytes=len(ciphertext),
    )


def _proof(bundle: EncryptedSupportBundle) -> str:
    return support_bundle_approval_proof_sha256(
        bundle_id=BUNDLE_ID,
        deployment_id=DEPLOYMENT_ID,
        tenant_id="ten_private_001",
        workspace_id="wsp_private_001",
        manifest_sha256=support_bundle_manifest_sha256(bundle.manifest),
        approved_by_account_id="acc_security_001",
        approved_at="2026-08-27T08:01:00.000Z",
        local_revision=2,
    )


def test_uploader_streams_canonical_metadata_then_ciphertext(tmp_path) -> None:
    bundle = _bundle(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-edge-session"] == EDGE_SESSION
        assert request.headers["x-edge-deployment-id"] == DEPLOYMENT_ID
        assert request.headers["content-type"] == (
            "application/vnd.metahub.edge-support-bundle"
        )
        body = request.read()
        metadata_length = int.from_bytes(body[:4], "big")
        metadata_bytes = body[4 : 4 + metadata_length]
        metadata = json.loads(metadata_bytes)
        assert metadata_bytes == canonical_edge_json_bytes(metadata)
        assert metadata["approval_proof_sha256"] == _proof(bundle)
        assert body[4 + metadata_length :] == bundle.object_path.read_bytes()
        receipt = {
            "bundle_id": BUNDLE_ID,
            "cloud_reference": BUNDLE_ID,
            "status": "ready",
            "revision": 1,
            "object_sha256": bundle.object_sha256,
            "idempotent_replay": False,
            "request_id": "req_prd125_upload",
            "trace_id": "trc_prd125_upload",
        }
        return httpx.Response(
            201,
            json=receipt,
            headers={"Cache-Control": "no-store", "Content-Type": "application/json"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        uploader = CloudSupportBundleUploader(
            client, "http://localhost", edge_session=lambda: EDGE_SESSION
        )
        reference = uploader(
            bundle,
            tenant_id="ten_private_001",
            workspace_id="wsp_private_001",
            approval_account_id="acc_security_001",
            approval_proof_sha256=_proof(bundle),
            approved_at="2026-08-27T08:01:00.000Z",
        )
    assert reference == BUNDLE_ID


def test_uploader_rejects_non_mtls_endpoint_shape_and_tampered_object(tmp_path) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(SupportBundleUploadError, match="endpoint_invalid"):
            CloudSupportBundleUploader(
                client, "http://control.example.test", edge_session=lambda: EDGE_SESSION
            )
        bundle = _bundle(tmp_path)
        bundle.object_path.write_bytes(bundle.object_path.read_bytes() + b"tampered")
        uploader = CloudSupportBundleUploader(
            client, "https://control.example.test", edge_session=lambda: EDGE_SESSION
        )
        with pytest.raises(SupportBundleUploadError, match="integrity_failed"):
            uploader(
                bundle,
                tenant_id="ten_private_001",
                workspace_id="wsp_private_001",
                approval_account_id="acc_security_001",
                approval_proof_sha256=_proof(bundle),
                approved_at="2026-08-27T08:01:00.000Z",
            )
