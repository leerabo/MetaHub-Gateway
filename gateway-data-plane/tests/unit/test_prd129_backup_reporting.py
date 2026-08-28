from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_gateway.local_secrets import (
    LocalBackupReportUploader,
    LocalBackupReportUploadError,
    LocalBackupStore,
    LocalDrillContext,
)
from metahub_gateway.local_secrets.keystore import TestKmsProtector as KmsProtector

DEPLOYMENT_ID = "edp_" + "7" * 32
TENANT_ID = "tenant_prd129"
WORKSPACE_ID = "workspace_prd129"
ACCOUNT_ID = "account_prd129"
EDGE_SESSION = "egs_" + "A" * 43
NOW = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)


def _store(tmp_path):
    clock = {"now": NOW}
    protector = KmsProtector("prd-129-report-uploader-kms-material", DEPLOYMENT_ID)
    store = LocalBackupStore(
        tmp_path / "journal.sqlite3",
        output_root=tmp_path / "artifacts",
        deployment_id=DEPLOYMENT_ID,
        schema_head="0121",
        signing_key=Ed25519PrivateKey.generate(),
        signing_key_id="edge-backup-report-v1",
        kms_key_id="test/customer/report-key",
        kms_key_version=1,
        wrap_data_key=lambda value: protector.protect(
            value, purpose="edge-backup-data-key"
        ),
        unwrap_data_key=lambda value: protector.unprotect(
            value, purpose="edge-backup-data-key"
        ),
        domain_provider=lambda _tenant, _workspace: {
            "config_policy": b'{"generation":1}',
            "ledger_watermark": b'{"cursor":1}',
            "audit_chain": b'{"head":"safe"}',
            "local_secrets": b'{"references":[]}',
        },
        restore_applier=lambda _domains, **_owners: None,
        now=lambda: clock["now"],
    )
    backup = store.create(
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        account_id=ACCOUNT_ID,
        source_generation=1,
        retention_seconds=86_400,
    )
    return store, backup, clock


def _response(request: httpx.Request, payload: dict[str, object], status: int = 200):
    return httpx.Response(
        status,
        request=request,
        headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        content=json.dumps(payload, separators=(",", ":")).encode("ascii"),
    )


def test_backup_report_retry_reuses_identical_body_and_acks_only_valid_receipt(
    tmp_path,
) -> None:
    store, backup, _ = _store(tmp_path)
    bodies: list[bytes] = []

    def transport(request: httpx.Request) -> httpx.Response:
        body = request.read()
        bodies.append(body)
        assert request.headers["x-edge-session"] == EDGE_SESSION
        if len(bodies) == 1:
            return _response(request, {"code": "edge_backup.unavailable"}, 503)
        return _response(
            request,
            {
                "deployment_id": DEPLOYMENT_ID,
                "backup": {"backup_id": backup.backup_id},
                "idempotent_replay": len(bodies) > 2,
                "request_id": "request-prd129",
                "trace_id": "trace-prd129",
            },
            201,
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        uploader = LocalBackupReportUploader(client, "http://localhost:8001")
        with pytest.raises(LocalBackupReportUploadError, match="receipt_invalid"):
            uploader.upload_next(store, edge_session=EDGE_SESSION)
        assert store.pending_cloud_report_count() == 1
        assert uploader.upload_next(store, edge_session=EDGE_SESSION) == 1
        assert uploader.upload_next(store, edge_session=EDGE_SESSION) == 0
    assert bodies[0] == bodies[1]
    assert store.pending_cloud_report_count() == 0


def test_restore_drill_report_uses_drill_endpoint_and_rejects_mismatched_receipt(
    tmp_path,
) -> None:
    store, backup, clock = _store(tmp_path)
    backup_report = store.next_cloud_report()
    assert backup_report is not None
    store.acknowledge_cloud_report(
        backup_report.report_id, backup_report.payload_sha256
    )
    clock["now"] = NOW + timedelta(minutes=20)
    drill_id = "pdd_" + "8" * 32
    store.restore(
        backup.backup_id,
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        account_id=ACCOUNT_ID,
        target_schema_head="0122",
        drill_context=LocalDrillContext(
            drill_id=drill_id,
            restore_job_id="rst_" + "9" * 32,
            expected_revision=2,
            source_backup_completed_at=NOW,
            device_reenrollment_evidence_sha256="a" * 64,
            minimal_request_evidence_sha256="b" * 64,
        ),
    )
    attempts = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.path.endswith(f"/restore-drills/{drill_id}/reports")
        received = json.loads(request.read())
        assert len(received["verifications"]) == 9
        selected_id = "pdd_" + "0" * 32 if attempts == 1 else drill_id
        return _response(
            request,
            {
                "deployment_id": DEPLOYMENT_ID,
                "drill": {
                    "drill_id": selected_id,
                    "status": "succeeded",
                },
                "idempotent_replay": False,
                "request_id": "request-prd129",
                "trace_id": "trace-prd129",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        uploader = LocalBackupReportUploader(client, "https://control.example.test")
        with pytest.raises(LocalBackupReportUploadError, match="receipt_invalid"):
            uploader.upload_next(store, edge_session=EDGE_SESSION)
        assert uploader.upload_next(store, edge_session=EDGE_SESSION) == 1
    assert attempts == 2
    assert store.pending_cloud_report_count() == 0
