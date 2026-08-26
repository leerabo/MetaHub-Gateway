from __future__ import annotations

import json
import sqlite3
import statistics
import time
from datetime import UTC, datetime

import pytest
from metahub_gateway.local_secrets import (
    LocalSecretStore,
    LocalSecretStoreError,
    ProviderTestResult,
)
from metahub_shared.private_gateway import LocalKek, LocalKekKeyring

DEPLOYMENT_ID = "edp_" + "1" * 32
NOW = datetime(2026, 8, 27, 3, 0, 0, tzinfo=UTC)


def _store(tmp_path) -> LocalSecretStore:
    return LocalSecretStore(
        tmp_path / "custody" / "local-secrets.sqlite3",
        deployment_id=DEPLOYMENT_ID,
        keyring=LocalKekKeyring(LocalKek(1, bytes(range(32)))),
        now=lambda: NOW,
    )


def _create(store: LocalSecretStore, value: bytes | bytearray | None = None):
    return store.create(
        tenant_id="ten_private_001",
        workspace_id="wsp_private_001",
        provider_connection_id="pcn_private_001",
        label="Primary provider credential",
        kind="api_key",
        plaintext=value if value is not None else bytearray(range(1, 129)),
    )


def _success(secret: memoryview) -> ProviderTestResult:
    assert len(secret) > 0
    return ProviderTestResult(
        "succeeded", ("chat_completions", "models"), "lt_100ms"
    )


def test_local_schema_is_independent_encrypted_and_append_guarded(tmp_path) -> None:
    store = _store(tmp_path)
    value = bytearray((index * 17) % 251 for index in range(4096))
    reference = _create(store, value)
    assert reference.active_version == 1
    assert reference.status == "active"
    assert store.path.parent.name == "custody"

    raw_database = store.path.read_bytes()
    assert bytes(value) not in raw_database

    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "local_secrets",
            "local_secret_versions",
            "local_secret_rotations",
            "local_provider_connection_tests",
            "local_secret_consumers",
            "local_secret_outbox",
        }.issubset(tables)
        with pytest.raises(sqlite3.IntegrityError, match="envelope_immutable"):
            connection.execute(
                "UPDATE local_secret_versions SET ciphertext=? WHERE reference_id=?",
                (b"changed", reference.reference_id),
            )
    finally:
        connection.close()


def test_local_schema_migrates_v1_outbox_delivery_state_in_place(tmp_path) -> None:
    path = tmp_path / "custody" / "local-secrets.sqlite3"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE local_secret_outbox(
              event_id TEXT PRIMARY KEY,
              event_type TEXT NOT NULL,
              reference_id TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              delivered_at TEXT
            );
            INSERT INTO local_secret_outbox(
              event_id,event_type,reference_id,payload_json,created_at,delivered_at
            ) VALUES(
              'lse_00000000000000000000000000000001',
              'edge.secret_reference_changed',
              'lsr_00000000000000000000000000000001',
              '{}',
              '2026-08-27T03:00:00.000Z',
              NULL
            );
            PRAGMA user_version=1;
            """
        )
        connection.commit()
    finally:
        connection.close()

    LocalSecretStore(
        path,
        deployment_id=DEPLOYMENT_ID,
        keyring=LocalKekKeyring(LocalKek(1, bytes(range(32)))),
        now=lambda: NOW,
    )

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(local_secret_outbox)")
        }
        assert {
            "delivery_batch_id",
            "delivery_attempt_count",
            "last_attempt_at",
            "delivery_receipt_id",
        }.issubset(columns)
        migrated = connection.execute(
            """
            SELECT delivery_batch_id,delivery_attempt_count,last_attempt_at,
                   delivery_receipt_id,delivered_at
            FROM local_secret_outbox
            """
        ).fetchone()
        assert migrated == (None, 0, None, None, None)
    finally:
        connection.close()


def test_rotation_activates_candidate_while_accepted_request_drains(tmp_path) -> None:
    store = _store(tmp_path)
    first_value = bytearray(range(1, 65))
    second_value = bytearray(reversed(range(1, 65)))
    reference = _create(store, first_value)
    accepted = store.acquire(
        reference.reference_id,
        tenant_id=reference.tenant_id,
        workspace_id=reference.workspace_id,
    )
    assert accepted.version == 1

    rotated = store.rotate(
        reference.reference_id,
        tenant_id=reference.tenant_id,
        workspace_id=reference.workspace_id,
        plaintext=second_value,
        tester=_success,
        expected_revision=reference.revision,
    )
    assert rotated.active_version == 2
    assert bytes(accepted.view()) == bytes(first_value)
    with store.acquire(
        reference.reference_id,
        tenant_id=reference.tenant_id,
        workspace_id=reference.workspace_id,
    ) as current:
        assert current.version == 2
        assert bytes(current.view()) == bytes(second_value)

    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT status FROM local_secret_versions WHERE reference_id=? AND version=1",
            (reference.reference_id,),
        ).fetchone()[0] == "draining"
        assert connection.execute(
            "SELECT status FROM local_secret_rotations WHERE reference_id=?",
            (reference.reference_id,),
        ).fetchone()[0] == "draining"
    finally:
        connection.close()

    accepted.close()
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT status FROM local_secret_versions WHERE reference_id=? AND version=1",
            (reference.reference_id,),
        ).fetchone()[0] == "retired"
        assert connection.execute(
            "SELECT status FROM local_secret_rotations WHERE reference_id=?",
            (reference.reference_id,),
        ).fetchone()[0] == "completed"
    finally:
        connection.close()


def test_failed_rotation_preserves_active_version_and_emits_only_fixed_facts(tmp_path) -> None:
    store = _store(tmp_path)
    reference = _create(store)

    def failed(_secret: memoryview) -> ProviderTestResult:
        return ProviderTestResult("authentication_failed", (), "100_499ms")

    degraded = store.rotate(
        reference.reference_id,
        tenant_id=reference.tenant_id,
        workspace_id=reference.workspace_id,
        plaintext=bytearray(reversed(range(1, 129))),
        tester=failed,
        expected_revision=reference.revision,
    )
    assert degraded.active_version == 1
    assert degraded.status == "degraded"
    assert degraded.last_test_outcome == "authentication_failed"
    with store.acquire(
        reference.reference_id,
        tenant_id=reference.tenant_id,
        workspace_id=reference.workspace_id,
    ) as lease:
        assert lease.version == 1

    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        events = connection.execute(
            "SELECT event_type,payload_json FROM local_secret_outbox ORDER BY rowid"
        ).fetchall()
        assert {event["event_type"] for event in events} == {
            "edge.secret_reference_changed",
            "edge.secret_rotation_failed",
            "edge.provider_test_completed",
        }
        for event in events:
            body = json.loads(event["payload_json"])
            assert not {
                "value",
                "secret",
                "ciphertext",
                "endpoint",
                "message",
            }.intersection(body)
    finally:
        connection.close()


@pytest.mark.parametrize("slot", ["live", "staging", "lkg", "rollback", "backup"])
def test_delete_checks_every_runtime_and_recovery_reference(tmp_path, slot) -> None:
    store = _store(tmp_path)
    reference = _create(store)
    store.bind_consumer(
        reference.reference_id,
        slot=slot,
        consumer_id=f"consumer_{slot}",
        version=1,
    )
    with pytest.raises(LocalSecretStoreError, match="local_secret.in_use"):
        store.delete(
            reference.reference_id,
            tenant_id=reference.tenant_id,
            workspace_id=reference.workspace_id,
            expected_revision=reference.revision,
        )
    store.unbind_consumer(
        reference.reference_id, slot=slot, consumer_id=f"consumer_{slot}"
    )
    disabled = store.delete(
        reference.reference_id,
        tenant_id=reference.tenant_id,
        workspace_id=reference.workspace_id,
        expected_revision=reference.revision,
    )
    assert disabled.status == "disabled"
    assert disabled.active_version is None
    with pytest.raises(LocalSecretStoreError, match="not_found"):
        store.acquire(
            reference.reference_id,
            tenant_id=reference.tenant_id,
            workspace_id=reference.workspace_id,
        )

    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_secret_versions WHERE reference_id=?",
            (reference.reference_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_cross_owner_access_cas_and_provider_exception_fail_closed(tmp_path) -> None:
    store = _store(tmp_path)
    reference = _create(store)
    with pytest.raises(LocalSecretStoreError, match="not_found"):
        store.acquire(
            reference.reference_id,
            tenant_id="ten_private_002",
            workspace_id=reference.workspace_id,
        )
    with pytest.raises(LocalSecretStoreError, match="revision_conflict"):
        store.rotate(
            reference.reference_id,
            tenant_id=reference.tenant_id,
            workspace_id=reference.workspace_id,
            plaintext=b"x" * 32,
            tester=_success,
            expected_revision=reference.revision + 1,
        )

    def unavailable(_secret: memoryview) -> ProviderTestResult:
        raise OSError

    result = store.test_connection(
        reference.reference_id,
        tenant_id=reference.tenant_id,
        workspace_id=reference.workspace_id,
        tester=unavailable,
    )
    assert result.outcome == "provider_unavailable"
    assert result.capabilities == ()
    assert result.latency_bucket == "gte_2000ms"


def test_local_unseal_p95_is_below_ten_milliseconds(tmp_path) -> None:
    store = _store(tmp_path)
    reference = _create(store, bytearray(range(1, 129)))
    elapsed: list[float] = []
    for _ in range(120):
        started = time.perf_counter()
        with store.acquire(
            reference.reference_id,
            tenant_id=reference.tenant_id,
            workspace_id=reference.workspace_id,
        ) as lease:
            assert lease.view()[0] == 1
        elapsed.append((time.perf_counter() - started) * 1000)
    p95 = statistics.quantiles(elapsed, n=100, method="inclusive")[94]
    assert p95 < 10.0
