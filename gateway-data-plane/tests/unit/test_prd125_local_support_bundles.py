"""PRD-125 local preview, consent, encryption, upload, and recovery tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metahub_gateway.local_secrets.support_bundles import (
    LocalSupportBundleError,
    LocalSupportBundleStore,
)
from metahub_shared.private_gateway import SUPPORT_BUNDLE_COLLECTOR_CODES

DEPLOYMENT_ID = "edp_" + "4" * 32
TENANT_ID = "ten_private_001"
WORKSPACE_ID = "wsp_private_001"
ACCOUNT_ID = "usr_security_001"
NOW = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
ENCRYPTION_SECRET = b"prd-125-local-store-encryption-secret"


def _inputs(_tenant_id: str, _workspace_id: str, _scanner_key: bytes):
    return (
        {
            "runtime.version": {"agent_version": "1.0.0"},
            "runtime.watermarks": {"configuration_generation": 7},
            "runtime.health": {"status": "ready"},
            "runtime.errors": {"codes": ["provider.timeout"]},
            "runtime.logs": '{"code":"gateway.ready","count":1}\n',
        },
        (),
    )


class RecordingUploader:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[bytes, str, str, str, str, str]] = []

    def __call__(
        self,
        bundle,
        *,
        tenant_id: str,
        workspace_id: str,
        approval_account_id: str,
        approval_proof_sha256: str,
        approved_at: str,
    ) -> str:
        if self.fail:
            raise OSError("cloud unavailable")
        self.calls.append(
            (
                bundle.object_path.read_bytes(),
                tenant_id,
                workspace_id,
                approval_account_id,
                approval_proof_sha256,
                approved_at,
            )
        )
        return bundle.manifest.bundle_id


def _store(
    tmp_path: Path,
    *,
    uploader: RecordingUploader | None = None,
    now=lambda: NOW,
) -> tuple[LocalSupportBundleStore, RecordingUploader, Path, Path]:
    database = tmp_path / "custody" / "support-bundles.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "support-bundles"
    selected = uploader or RecordingUploader()
    return (
        LocalSupportBundleStore(
            database,
            output_root=output,
            deployment_id=DEPLOYMENT_ID,
            encryption_secret=ENCRYPTION_SECRET,
            input_provider=_inputs,
            uploader=selected,
            ttl_seconds=3600,
            now=now,
        ),
        selected,
        database,
        output,
    )


def _preview(store: LocalSupportBundleStore):
    return store.preview(
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        account_id=ACCOUNT_ID,
        collector_codes=SUPPORT_BUNDLE_COLLECTOR_CODES,
    )


def test_preview_requires_explicit_approval_then_uploads_only_encrypted_bytes(
    tmp_path: Path,
) -> None:
    store, uploader, database, output = _store(tmp_path)
    preview = _preview(store)
    archive = output / f"{preview.bundle_id}.zip"
    assert preview.status == "preview_ready"
    assert preview.revision == 1
    assert archive.is_file()
    assert uploader.calls == []

    uploaded = store.approve(
        preview.bundle_id,
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        account_id=ACCOUNT_ID,
        expected_revision=preview.revision,
    )
    assert uploaded.status == "uploaded"
    assert uploaded.revision == 3
    assert uploaded.cloud_object_reference == preview.bundle_id
    assert not archive.exists()
    assert not (output / f"{preview.bundle_id}.enc").exists()
    assert len(uploader.calls) == 1
    encrypted, tenant_id, workspace_id, account_id, approval_proof, approved_at = (
        uploader.calls[0]
    )
    assert encrypted.startswith(b"MHSB1")
    assert tenant_id == TENANT_ID
    assert workspace_id == WORKSPACE_ID
    assert account_id == ACCOUNT_ID
    assert len(approval_proof) == 64
    assert approved_at == "2026-08-27T06:00:00.000Z"

    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT approved_by_account_id,status,failure_code FROM local_support_bundles"
        ).fetchone()
        assert row == (ACCOUNT_ID, "uploaded", None)
        with pytest.raises(sqlite3.IntegrityError, match="local_support_bundle_immutable"):
            connection.execute("DELETE FROM local_support_bundles")
    finally:
        connection.close()


def test_cancel_and_cross_owner_access_are_terminal_and_non_disclosing(
    tmp_path: Path,
) -> None:
    store, _, _, output = _store(tmp_path)
    preview = _preview(store)
    with pytest.raises(LocalSupportBundleError, match="local_support_bundle.not_found"):
        store.cancel(
            preview.bundle_id,
            tenant_id="ten_private_002",
            workspace_id=WORKSPACE_ID,
            expected_revision=1,
        )
    cancelled = store.cancel(
        preview.bundle_id,
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        expected_revision=1,
    )
    assert cancelled.status == "cancelled"
    assert cancelled.revision == 2
    assert not (output / f"{preview.bundle_id}.zip").exists()
    with pytest.raises(LocalSupportBundleError, match="state_conflict"):
        store.approve(
            preview.bundle_id,
            tenant_id=TENANT_ID,
            workspace_id=WORKSPACE_ID,
            account_id=ACCOUNT_ID,
            expected_revision=2,
        )


def test_upload_and_integrity_failures_preserve_specific_fixed_codes(tmp_path: Path) -> None:
    failed_store, _, failed_database, _ = _store(
        tmp_path / "upload", uploader=RecordingUploader(fail=True)
    )
    failed = _preview(failed_store)
    with pytest.raises(LocalSupportBundleError, match="local_support_bundle.upload_failed"):
        failed_store.approve(
            failed.bundle_id,
            tenant_id=TENANT_ID,
            workspace_id=WORKSPACE_ID,
            account_id=ACCOUNT_ID,
            expected_revision=1,
        )
    connection = sqlite3.connect(failed_database)
    try:
        assert connection.execute(
            "SELECT status,failure_code FROM local_support_bundles"
        ).fetchone() == ("failed", "upload_failed")
    finally:
        connection.close()

    integrity_store, _, integrity_database, output = _store(tmp_path / "integrity")
    preview = _preview(integrity_store)
    (output / f"{preview.bundle_id}.zip").write_bytes(b"tampered")
    with pytest.raises(LocalSupportBundleError, match="local_support_bundle.integrity_failed"):
        integrity_store.approve(
            preview.bundle_id,
            tenant_id=TENANT_ID,
            workspace_id=WORKSPACE_ID,
            account_id=ACCOUNT_ID,
            expected_revision=1,
        )
    connection = sqlite3.connect(integrity_database)
    try:
        assert connection.execute(
            "SELECT status,failure_code FROM local_support_bundles"
        ).fetchone() == ("failed", "integrity_failed")
    finally:
        connection.close()


def test_restart_expires_previews_and_fails_interrupted_uploads(tmp_path: Path) -> None:
    clock = {"now": NOW}
    store, uploader, database, output = _store(tmp_path, now=lambda: clock["now"])
    expiring = _preview(store)
    clock["now"] = NOW + timedelta(minutes=30)
    interrupted = _preview(store)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            UPDATE local_support_bundles
            SET status='uploading',approved_by_account_id=?,approved_at=?,revision=2,updated_at=?
            WHERE bundle_id=?
            """,
            (
                ACCOUNT_ID,
                "2026-08-27T06:01:00.000Z",
                "2026-08-27T06:01:00.000Z",
                interrupted.bundle_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    LocalSupportBundleStore(
        database,
        output_root=output,
        deployment_id=DEPLOYMENT_ID,
        encryption_secret=ENCRYPTION_SECRET,
        input_provider=_inputs,
        uploader=uploader,
        ttl_seconds=3600,
        now=lambda: NOW + timedelta(hours=1, minutes=15),
    )
    connection = sqlite3.connect(database)
    try:
        rows = dict(
            connection.execute(
                "SELECT bundle_id,status || ':' || COALESCE(failure_code,'') "
                "FROM local_support_bundles"
            ).fetchall()
        )
        assert rows == {
            expiring.bundle_id: "expired:",
            interrupted.bundle_id: "failed:interrupted",
        }
    finally:
        connection.close()
    assert list(output.glob("esb_*")) == []
