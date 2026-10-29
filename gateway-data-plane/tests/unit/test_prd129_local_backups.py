from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_gateway.local_secrets.backups import (
    LocalBackupError,
    LocalBackupSigningKeyStore,
    LocalBackupStore,
    LocalDrillContext,
)
from metahub_gateway.local_secrets.keystore import TestKmsProtector as KmsProtector
from metahub_gateway.local_secrets.store import LocalSecretStore
from metahub_shared.private_gateway import (
    BACKUP_DOMAIN_CODES,
    LocalKek,
    LocalKekKeyring,
)

DEPLOYMENT_ID = "edp_" + "1" * 32
TENANT_ID = "tenant_prd129"
WORKSPACE_ID = "workspace_prd129"
ACCOUNT_ID = "account_prd129"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class RecordingApplier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, bytes]] = []

    def __call__(self, domains, **owners: str) -> None:
        assert owners["tenant_id"] == TENANT_ID
        assert owners["workspace_id"] == WORKSPACE_ID
        assert owners["source_deployment_id"] == DEPLOYMENT_ID
        assert owners["restore_id"].startswith("lrj_")
        if self.fail:
            raise OSError("staging replacement failed")
        self.calls.append(dict(domains))


def _domains() -> dict[str, bytes]:
    return {
        "config_policy": b'{"generation":11,"policy":"strict"}',
        "ledger_watermark": b'{"usage_cursor":901}',
        "audit_chain": b'{"head":"' + b"a" * 64 + b'"}',
        "local_secrets": b'{"values":["portable-secret-envelope"]}',
    }


def _store(
    tmp_path,
    *,
    applier=None,
    clock=None,
    schema_head="0121",
    customer_object_store_root=None,
):
    protector = KmsProtector(
        "prd-129-customer-kms-master-secret-material",
        DEPLOYMENT_ID,
    )
    signing_key = Ed25519PrivateKey.generate()
    selected = applier or RecordingApplier()
    selected_clock = clock or {"now": NOW}
    store = LocalBackupStore(
        tmp_path / "journal.sqlite3",
        output_root=tmp_path / "artifacts",
        deployment_id=DEPLOYMENT_ID,
        schema_head=schema_head,
        signing_key=signing_key,
        signing_key_id="edge-backup-test-v1",
        kms_key_id="test/customer/recovery-key",
        kms_key_version=7,
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
        restore_applier=selected,
        customer_object_store_root=customer_object_store_root,
        now=lambda: selected_clock["now"],
    )
    return store, selected, selected_clock, tmp_path / "artifacts", tmp_path / "journal.sqlite3"


def _create(store: LocalBackupStore):
    return store.create(
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        account_id=ACCOUNT_ID,
        source_generation=11,
        retention_seconds=86_400,
    )


def test_backup_artifacts_are_encrypted_and_restore_is_journaled(tmp_path) -> None:
    store, applier, _, root, database = _store(tmp_path)
    backup = _create(store)
    directory = root / backup.backup_id

    assert backup.chunk_count == len(BACKUP_DOMAIN_CODES)
    assert {path.name for path in directory.iterdir()} == {
        "manifest.json",
        "envelope.json",
        *(
            path.name
            for path in directory.iterdir()
            if path.name.startswith("bch_")
        ),
    }
    joined = b"".join(path.read_bytes() for path in directory.iterdir())
    assert b"portable-secret-envelope" not in joined
    assert b"customer/recovery-key" not in joined

    restored = store.restore(
        backup.backup_id,
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        account_id=ACCOUNT_ID,
        target_schema_head="0122",
    )
    assert restored.status == "succeeded"
    assert applier.calls == [_domains()]

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT status,revision,failure_code FROM local_restore_journals"
        ).fetchone() == ("succeeded", 4, None)
        assert connection.execute(
            "SELECT step_code,outcome FROM local_restore_steps ORDER BY sequence"
        ).fetchall() == [
            ("staging", "passed"),
            ("integrity", "passed"),
            ("apply", "passed"),
            ("complete", "passed"),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="local_restore_step_immutable"):
            connection.execute("DELETE FROM local_restore_steps")
    finally:
        connection.close()


def test_missing_or_corrupt_chunk_never_calls_restore_applier(tmp_path) -> None:
    store, applier, _, root, database = _store(tmp_path)
    backup = _create(store)
    chunk = next((root / backup.backup_id).glob("bch_*.bin"))
    chunk.write_bytes(bytes([chunk.read_bytes()[0] ^ 1]) + chunk.read_bytes()[1:])

    with pytest.raises(LocalBackupError, match="local_restore.chunk_invalid"):
        store.restore(
            backup.backup_id,
            tenant_id=TENANT_ID,
            workspace_id=WORKSPACE_ID,
            account_id=ACCOUNT_ID,
            target_schema_head="0121",
        )
    assert applier.calls == []
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT status,failure_code FROM local_restore_journals"
        ).fetchone() == ("failed", "local_restore.chunk_invalid")
    finally:
        connection.close()


def test_expiry_schema_and_apply_failures_preserve_active_callback_boundary(tmp_path) -> None:
    store, applier, clock, _, _ = _store(tmp_path / "expired")
    backup = _create(store)
    clock["now"] = NOW + timedelta(days=2)
    with pytest.raises(LocalBackupError, match="local_restore.expired"):
        store.restore(
            backup.backup_id,
            tenant_id=TENANT_ID,
            workspace_id=WORKSPACE_ID,
            account_id=ACCOUNT_ID,
            target_schema_head="0121",
        )
    assert applier.calls == []

    incompatible, incompatible_applier, _, _, _ = _store(tmp_path / "schema")
    incompatible_backup = _create(incompatible)
    with pytest.raises(LocalBackupError, match="schema_incompatible"):
        incompatible.restore(
            incompatible_backup.backup_id,
            tenant_id=TENANT_ID,
            workspace_id=WORKSPACE_ID,
            account_id=ACCOUNT_ID,
            target_schema_head="0120",
        )
    assert incompatible_applier.calls == []

    failing_applier = RecordingApplier(fail=True)
    failing, _, _, _, database = _store(tmp_path / "apply", applier=failing_applier)
    failing_backup = _create(failing)
    with pytest.raises(LocalBackupError, match="local_restore.apply_failed"):
        failing.restore(
            failing_backup.backup_id,
            tenant_id=TENANT_ID,
            workspace_id=WORKSPACE_ID,
            account_id=ACCOUNT_ID,
            target_schema_head="0121",
        )
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT status,failure_code,revision FROM local_restore_journals"
        ).fetchone() == ("failed", "local_restore.apply_failed", 4)
    finally:
        connection.close()


def test_backup_signing_key_is_independent_protected_and_reloadable(tmp_path) -> None:
    protector = KmsProtector(
        "prd-129-independent-signing-key-protector",
        DEPLOYMENT_ID,
    )
    path = tmp_path / "backup-signing-key.json"
    store = LocalBackupSigningKeyStore(path, protector)
    first = store.load_or_create().public_key().public_bytes_raw()
    second = store.load().public_key().public_bytes_raw()
    assert first == second
    assert first not in path.read_bytes()
    assert b"private_key" not in path.read_bytes()


def test_restore_rewraps_portable_secrets_with_replacement_node_kek(tmp_path) -> None:
    source_key = bytes(range(32))
    target_key = bytes(reversed(range(32)))
    source = LocalSecretStore(
        tmp_path / "source.sqlite3",
        deployment_id=DEPLOYMENT_ID,
        keyring=LocalKekKeyring(LocalKek(1, source_key)),
        now=lambda: NOW,
    )
    reference = source.create(
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        provider_connection_id="provider_prd129",
        label="Primary provider",
        kind="api_key",
        plaintext=b"source-portable-secret",
    )
    target = LocalSecretStore(
        tmp_path / "target.sqlite3",
        deployment_id=DEPLOYMENT_ID,
        keyring=LocalKekKeyring(LocalKek(7, target_key)),
        now=lambda: NOW + timedelta(minutes=1),
    )
    decoy = target.create(
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        provider_connection_id="provider_decoy",
        label="Target decoy",
        kind="api_key",
        plaintext=b"target-decoy-secret",
    )
    protector = KmsProtector(
        "prd-129-portable-archive-kms-material",
        DEPLOYMENT_ID,
    )
    clock = {"now": NOW + timedelta(minutes=2)}
    backups = LocalBackupStore(
        tmp_path / "portable-journal.sqlite3",
        output_root=tmp_path / "portable-artifacts",
        deployment_id=DEPLOYMENT_ID,
        schema_head="0121",
        signing_key=Ed25519PrivateKey.generate(),
        signing_key_id="edge-backup-rewrap-v1",
        kms_key_id="test/customer/portable-key",
        kms_key_version=3,
        wrap_data_key=lambda value: protector.protect(
            value, purpose="edge-backup-data-key"
        ),
        unwrap_data_key=lambda value: protector.unprotect(
            value, purpose="edge-backup-data-key"
        ),
        domain_provider=lambda tenant_id, workspace_id: source.backup_domains(
            tenant_id=tenant_id, workspace_id=workspace_id
        ),
        restore_applier=lambda domains, **owners: target.apply_backup_domains(
            domains, **owners
        ),
        now=lambda: clock["now"],
    )
    backup = _create(backups)
    restored = backups.restore(
        backup.backup_id,
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        account_id=ACCOUNT_ID,
        target_schema_head="0121",
    )
    assert restored.status == "succeeded"
    references = target.list_references(
        tenant_id=TENANT_ID, workspace_id=WORKSPACE_ID
    )
    assert [item.reference_id for item in references] == [reference.reference_id]
    assert decoy.reference_id not in {item.reference_id for item in references}
    with target.acquire(
        reference.reference_id,
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
    ) as lease:
        assert bytes(lease.view()) == b"source-portable-secret"

    connection = sqlite3.connect(target.path)
    try:
        assert connection.execute(
            "SELECT kek_version FROM local_secret_versions"
        ).fetchone()[0] == 7
    finally:
        connection.close()
    assert target.path.with_name(target.path.name + ".restore-lkg").is_file()
    artifacts = b"".join(
        path.read_bytes()
        for path in (tmp_path / "portable-artifacts" / backup.backup_id).iterdir()
    )
    assert b"source-portable-secret" not in artifacts
    assert source_key not in artifacts and target_key not in artifacts


def test_cloud_report_outbox_is_retry_stable_and_immutable(tmp_path) -> None:
    store, _, _, _, database = _store(tmp_path)
    backup = _create(store)

    first = store.next_cloud_report()
    second = store.next_cloud_report()
    assert first is not None and second == first
    assert first.aggregate_id == backup.backup_id
    payload = json.loads(first.payload_json)
    assert payload["status"] == "completed"
    assert payload["check_outcome"] == "verified"
    assert payload["manifest_sha256"] == backup.manifest_sha256
    assert not {
        "artifact_directory",
        "chunks",
        "envelope",
        "merkle_root",
        "path",
        "secret",
    }.intersection(payload)

    assert store.acknowledge_cloud_report(first.report_id, first.payload_sha256)
    assert not store.acknowledge_cloud_report(first.report_id, first.payload_sha256)
    assert store.next_cloud_report() is None
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="local_backup_report_immutable"):
            connection.execute("DELETE FROM local_backup_report_outbox")
    finally:
        connection.close()


def test_customer_object_store_uses_distinct_mount_and_missing_mount_reports_failure(
    tmp_path,
) -> None:
    customer_root = tmp_path / "customer-object-store"
    store, _, _, local_root, _ = _store(
        tmp_path / "configured", customer_object_store_root=customer_root
    )
    backup = store.create(
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        account_id=ACCOUNT_ID,
        source_generation=11,
        retention_seconds=86_400,
        storage_kind="customer_object_store",
    )
    assert not any(local_root.iterdir())
    assert (customer_root / backup.backup_id / "manifest.json").is_file()

    unavailable, _, _, _, _ = _store(tmp_path / "unavailable")
    with pytest.raises(LocalBackupError, match="customer_store_unavailable"):
        unavailable.create(
            tenant_id=TENANT_ID,
            workspace_id=WORKSPACE_ID,
            account_id=ACCOUNT_ID,
            source_generation=12,
            retention_seconds=86_400,
            storage_kind="customer_object_store",
        )
    report = unavailable.next_cloud_report()
    assert report is not None
    failure = json.loads(report.payload_json)
    assert failure["status"] == "failed"
    assert failure["failure_code"] == "local_backup.customer_store_unavailable"
    assert failure["manifest_sha256"] is None
    assert failure["kms_key_id_sha256"] is None


def test_restore_drill_queues_nine_safe_verifications_and_objective_timings(
    tmp_path,
) -> None:
    store, _, clock, _, _ = _store(tmp_path)
    backup = _create(store)
    backup_report = store.next_cloud_report()
    assert backup_report is not None
    store.acknowledge_cloud_report(
        backup_report.report_id, backup_report.payload_sha256
    )
    clock["now"] = NOW + timedelta(hours=2)
    restored = store.restore(
        backup.backup_id,
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        account_id=ACCOUNT_ID,
        target_schema_head="0122",
        drill_context=LocalDrillContext(
            drill_id="pdd_" + "2" * 32,
            restore_job_id="rst_" + "3" * 32,
            expected_revision=2,
            source_backup_completed_at=NOW,
            device_reenrollment_evidence_sha256="4" * 64,
            minimal_request_evidence_sha256="5" * 64,
        ),
    )
    assert restored.status == "succeeded"
    report = store.next_cloud_report()
    assert report is not None and report.report_kind == "drill"
    payload = json.loads(report.payload_json)
    assert payload["actual_rpo_seconds"] == 7200
    assert payload["actual_rto_seconds"] == 0
    assert payload["outcome"] == "succeeded"
    assert len(payload["verifications"]) == 9
    assert {item["verification_code"] for item in payload["verifications"]} == {
        "manifest_integrity",
        "owner_binding",
        "schema_compatibility",
        "config_consistency",
        "usage_cursor",
        "audit_chain",
        "secret_reference",
        "device_reenrollment",
        "minimal_request",
    }
    assert all(item["outcome"] == "passed" for item in payload["verifications"])
    first_report = report.payload_json
    serialized = first_report.lower()
    assert b"snapshot" not in serialized
    assert b"portable-secret" not in serialized
    assert b"artifact" not in serialized
    assert first_report == store.next_cloud_report().payload_json
