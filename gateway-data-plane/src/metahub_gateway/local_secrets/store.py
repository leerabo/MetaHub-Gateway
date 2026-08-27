"""Independent local-only custody store and rotation state machine."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from metahub_shared.private_gateway import (
    EdgeProviderTestCompletedEvent,
    EdgeSecretReferenceChangedEvent,
    EdgeSecretRotationFailedEvent,
    EdgeSecretSyncBatch,
    EdgeSecretSyncReceipt,
    EncryptedLocalSecret,
    LocalKekKeyring,
    LocalSecretKind,
    LocalSecretOwner,
    MutableSecretLease,
    ProviderCapability,
    ProviderTestOutcome,
    SecretReference,
    decrypt_local_secret,
    encrypt_local_secret,
)

type ConsumerSlot = Literal["live", "staging", "lkg", "rollback", "backup"]
type LatencyBucket = Literal["lt_100ms", "100_499ms", "500_1999ms", "gte_2000ms"]

_SCHEMA_VERSION = 2
_EVENT_TYPES = frozenset(
    {
        "edge.secret_reference_changed",
        "edge.secret_rotation_failed",
        "edge.provider_test_completed",
    }
)
_CONSUMER_SLOTS = frozenset({"live", "staging", "lkg", "rollback", "backup"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_secrets(
  reference_id TEXT PRIMARY KEY,
  deployment_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  provider_connection_id TEXT NOT NULL,
  label TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN
    ('api_key','bearer_token','basic_password','oauth_client_secret')),
  status TEXT NOT NULL CHECK(status IN ('active','rotating','degraded','disabled')),
  revision INTEGER NOT NULL CHECK(revision >= 1),
  active_version INTEGER,
  candidate_version INTEGER,
  last_test_outcome TEXT,
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  rotated_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(deployment_id, tenant_id, workspace_id, provider_connection_id, label),
  CHECK((status='disabled' AND active_version IS NULL) OR status<>'disabled')
);
CREATE TABLE IF NOT EXISTS local_secret_versions(
  reference_id TEXT NOT NULL REFERENCES local_secrets(reference_id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK(version >= 1),
  binding_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('candidate','active','draining','retired','failed')),
  ciphertext_format TEXT NOT NULL,
  kek_version INTEGER NOT NULL CHECK(kek_version >= 1),
  wrap_nonce BLOB NOT NULL,
  wrapped_data_key BLOB NOT NULL,
  value_nonce BLOB NOT NULL,
  ciphertext BLOB NOT NULL,
  created_at TEXT NOT NULL,
  activated_at TEXT,
  drain_started_at TEXT,
  retired_at TEXT,
  PRIMARY KEY(reference_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_secret_one_candidate
  ON local_secret_versions(reference_id) WHERE status='candidate';
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_secret_one_active
  ON local_secret_versions(reference_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS local_secret_rotations(
  rotation_id TEXT PRIMARY KEY,
  reference_id TEXT NOT NULL REFERENCES local_secrets(reference_id) ON DELETE CASCADE,
  from_version INTEGER NOT NULL,
  to_version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('preparing','testing','activating','draining','completed','failed')),
  test_outcome TEXT,
  created_at TEXT NOT NULL,
  activated_at TEXT,
  completed_at TEXT,
  UNIQUE(reference_id, to_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_secret_one_rotation
  ON local_secret_rotations(reference_id)
  WHERE status IN ('preparing','testing','activating','draining');
CREATE TABLE IF NOT EXISTS local_provider_connection_tests(
  test_id TEXT PRIMARY KEY,
  reference_id TEXT NOT NULL REFERENCES local_secrets(reference_id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN
    ('succeeded','authentication_failed','authorization_failed','rate_limited','timeout',
     'tls_failed','network_unreachable','provider_unavailable','unsupported')),
  capabilities_json TEXT NOT NULL,
  latency_bucket TEXT NOT NULL CHECK(latency_bucket IN
    ('lt_100ms','100_499ms','500_1999ms','gte_2000ms')),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_secret_consumers(
  reference_id TEXT NOT NULL REFERENCES local_secrets(reference_id) ON DELETE CASCADE,
  slot TEXT NOT NULL CHECK(slot IN ('live','staging','lkg','rollback','backup')),
  consumer_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(reference_id, slot, consumer_id)
);
CREATE TABLE IF NOT EXISTS local_secret_outbox(
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL CHECK(event_type IN
    ('edge.secret_reference_changed','edge.secret_rotation_failed',
     'edge.provider_test_completed')),
  reference_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivery_batch_id TEXT CHECK(delivery_batch_id IS NULL OR(
    length(delivery_batch_id)=36 AND substr(delivery_batch_id,1,4)='lsb_')),
  delivery_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(delivery_attempt_count>=0),
  last_attempt_at TEXT,
  delivery_receipt_id TEXT CHECK(delivery_receipt_id IS NULL OR(
    length(delivery_receipt_id)=36 AND substr(delivery_receipt_id,1,4)='lsa_')),
  delivered_at TEXT,
  CHECK((delivered_at IS NULL AND delivery_receipt_id IS NULL)
    OR(delivered_at IS NOT NULL AND delivery_receipt_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_local_secret_outbox_delivery
  ON local_secret_outbox(delivered_at,delivery_batch_id,created_at,event_id);
CREATE TRIGGER IF NOT EXISTS local_secret_envelope_immutable
BEFORE UPDATE OF reference_id,version,binding_digest,ciphertext_format,kek_version,
  wrap_nonce,wrapped_data_key,value_nonce,ciphertext,created_at
ON local_secret_versions BEGIN SELECT RAISE(ABORT, 'local_secret_envelope_immutable'); END;
CREATE TRIGGER IF NOT EXISTS local_secret_version_no_delete
BEFORE DELETE ON local_secret_versions
WHEN EXISTS(SELECT 1 FROM local_secrets s
            WHERE s.reference_id=OLD.reference_id AND s.status<>'disabled')
BEGIN SELECT RAISE(ABORT, 'local_secret_version_in_use'); END;
"""

_MIGRATE_V1_TO_V2 = """
ALTER TABLE local_secret_outbox ADD COLUMN delivery_batch_id TEXT
  CHECK(delivery_batch_id IS NULL OR(
    length(delivery_batch_id)=36 AND substr(delivery_batch_id,1,4)='lsb_'));
ALTER TABLE local_secret_outbox ADD COLUMN delivery_attempt_count INTEGER
  NOT NULL DEFAULT 0 CHECK(delivery_attempt_count>=0);
ALTER TABLE local_secret_outbox ADD COLUMN last_attempt_at TEXT;
ALTER TABLE local_secret_outbox ADD COLUMN delivery_receipt_id TEXT
  CHECK(delivery_receipt_id IS NULL OR(
    length(delivery_receipt_id)=36 AND substr(delivery_receipt_id,1,4)='lsa_'));
CREATE INDEX IF NOT EXISTS idx_local_secret_outbox_delivery
  ON local_secret_outbox(delivered_at,delivery_batch_id,created_at,event_id);
"""


class LocalSecretStoreError(RuntimeError):
    """Fixed local custody error without database or provider details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProviderTestResult:
    outcome: ProviderTestOutcome
    capabilities: tuple[ProviderCapability, ...]
    latency_bucket: LatencyBucket

    def __post_init__(self) -> None:
        if self.outcome not in {
            "succeeded",
            "authentication_failed",
            "authorization_failed",
            "rate_limited",
            "timeout",
            "tls_failed",
            "network_unreachable",
            "provider_unavailable",
            "unsupported",
        }:
            raise LocalSecretStoreError("local_secret.test_result_invalid")
        if (
            len(self.capabilities) > 3
            or len(self.capabilities) != len(set(self.capabilities))
            or tuple(sorted(self.capabilities)) != self.capabilities
            or any(
                item not in {"chat_completions", "embeddings", "models"}
                for item in self.capabilities
            )
            or (self.outcome != "succeeded" and self.capabilities)
        ):
            raise LocalSecretStoreError("local_secret.test_result_invalid")
        if self.latency_bucket not in {
            "lt_100ms",
            "100_499ms",
            "500_1999ms",
            "gte_2000ms",
        }:
            raise LocalSecretStoreError("local_secret.test_result_invalid")


@dataclass(frozen=True, slots=True)
class LocalProviderConnectionTest:
    test_id: str
    reference_id: str
    version: int
    outcome: ProviderTestOutcome
    capabilities: tuple[ProviderCapability, ...]
    latency_bucket: LatencyBucket
    created_at: str


class LocalVersionLease:
    """Pins one accepted request to its selected immutable secret version."""

    __slots__ = ("_closed", "_lease", "_release", "reference_id", "version")

    def __init__(
        self,
        *,
        reference_id: str,
        version: int,
        lease: MutableSecretLease,
        release: Callable[[str, int], None],
    ) -> None:
        self.reference_id = reference_id
        self.version = version
        self._lease = lease
        self._release = release
        self._closed = False

    def view(self) -> memoryview:
        return self._lease.view()

    def close(self) -> None:
        if self._closed:
            return
        self._lease.close()
        self._closed = True
        self._release(self.reference_id, self.version)

    def __enter__(self) -> Self:
        if self._closed:
            raise LocalSecretStoreError("local_secret.lease_closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"LocalVersionLease(reference_id={self.reference_id!r}, "
            f"version={self.version}, value=<redacted>)"
        )


class LocalSecretStore:
    """Single-owner local store with atomic rotations and in-process drain accounting."""

    def __init__(
        self,
        path: str | Path,
        *,
        deployment_id: str,
        keyring: LocalKekKeyring,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._path = Path(path).resolve()
        self._deployment_id = deployment_id
        self._keyring = keyring
        self._now = now
        self._lock = threading.RLock()
        self._leases: dict[tuple[str, int], int] = {}
        self._draining: set[tuple[str, int]] = set()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def deployment_id(self) -> str:
        return self._deployment_id

    def create(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        provider_connection_id: str,
        label: str,
        kind: LocalSecretKind,
        plaintext: bytes | bytearray,
    ) -> SecretReference:
        reference_id = _id("lsr_")
        now = self._time()
        binding_digest = _binding_digest(provider_connection_id)
        owner = self._owner(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            reference_id=reference_id,
            version=1,
            kind=kind,
            binding_digest=binding_digest,
        )
        encrypted = encrypt_local_secret(plaintext, owner=owner, keyring=self._keyring)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO local_secrets(
                      reference_id,deployment_id,tenant_id,workspace_id,
                      provider_connection_id,label,kind,status,revision,active_version,
                      candidate_version,last_test_outcome,capabilities_json,rotated_at,
                      created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,1,1,NULL,NULL,'[]',NULL,?,?)
                    """,
                    (
                        reference_id,
                        self._deployment_id,
                        tenant_id,
                        workspace_id,
                        provider_connection_id,
                        label,
                        kind,
                        "active",
                        now,
                        now,
                    ),
                )
                self._insert_version(
                    connection,
                    reference_id=reference_id,
                    version=1,
                    binding_digest=binding_digest,
                    status="active",
                    encrypted=encrypted,
                    now=now,
                    activated_at=now,
                )
                reference = self._reference(connection, reference_id)
                self._event(connection, "edge.secret_reference_changed", reference, now=now)
                connection.commit()
                return reference
            except LocalSecretStoreError:
                _rollback(connection)
                raise
            except sqlite3.IntegrityError:
                _rollback(connection)
                raise LocalSecretStoreError("local_secret.conflict") from None
            except sqlite3.Error:
                _rollback(connection)
                raise LocalSecretStoreError("local_secret.storage_unavailable") from None
            finally:
                connection.close()

    def acquire(
        self,
        reference_id: str,
        *,
        tenant_id: str,
        workspace_id: str,
    ) -> LocalVersionLease:
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT s.tenant_id,s.workspace_id,s.kind,s.provider_connection_id,
                           s.active_version,v.*
                    FROM local_secrets s JOIN local_secret_versions v
                      ON v.reference_id=s.reference_id AND v.version=s.active_version
                    WHERE s.reference_id=? AND s.deployment_id=? AND s.status<>'disabled'
                    """,
                    (reference_id, self._deployment_id),
                ).fetchone()
                if (
                    row is None
                    or row["tenant_id"] != tenant_id
                    or row["workspace_id"] != workspace_id
                    or row["status"] not in {"active", "draining"}
                ):
                    raise LocalSecretStoreError("local_secret.not_found")
                version = int(row["active_version"])
                key = (reference_id, version)
                self._leases[key] = self._leases.get(key, 0) + 1
                try:
                    lease = decrypt_local_secret(
                        self._envelope(row),
                        owner=self._owner_from_rows(row, reference_id, version),
                        keyring=self._keyring,
                    )
                except Exception:
                    self._leases[key] -= 1
                    if self._leases[key] == 0:
                        del self._leases[key]
                    raise LocalSecretStoreError("local_secret.unseal_failed") from None
                return LocalVersionLease(
                    reference_id=reference_id,
                    version=version,
                    lease=lease,
                    release=self._release,
                )
            except LocalSecretStoreError:
                raise
            except sqlite3.Error:
                raise LocalSecretStoreError("local_secret.storage_unavailable") from None
            finally:
                connection.close()

    def rotate(
        self,
        reference_id: str,
        *,
        tenant_id: str,
        workspace_id: str,
        plaintext: bytes | bytearray,
        tester: Callable[[memoryview], ProviderTestResult],
        expected_revision: int,
    ) -> SecretReference:
        if not callable(tester):
            raise LocalSecretStoreError("local_secret.tester_invalid")
        rotation_id = _id("lsn_")
        now = self._time()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                secret = self._owned_secret(
                    connection,
                    reference_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )
                if (
                    int(secret["revision"]) != expected_revision
                    or secret["status"] == "disabled"
                    or secret["candidate_version"] is not None
                ):
                    raise LocalSecretStoreError("local_secret.revision_conflict")
                from_version = int(secret["active_version"])
                to_version = from_version + 1
                binding_digest = _binding_digest(str(secret["provider_connection_id"]))
                owner = self._owner_from_rows(secret, reference_id, to_version)
                encrypted = encrypt_local_secret(plaintext, owner=owner, keyring=self._keyring)
                self._insert_version(
                    connection,
                    reference_id=reference_id,
                    version=to_version,
                    binding_digest=binding_digest,
                    status="candidate",
                    encrypted=encrypted,
                    now=now,
                    activated_at=None,
                )
                connection.execute(
                    """
                    INSERT INTO local_secret_rotations(
                      rotation_id,reference_id,from_version,to_version,status,test_outcome,
                      created_at,activated_at,completed_at
                    ) VALUES(?,?,?,?,? ,NULL,?,NULL,NULL)
                    """,
                    (rotation_id, reference_id, from_version, to_version, "testing", now),
                )
                connection.execute(
                    """
                    UPDATE local_secrets SET status='rotating',candidate_version=?,updated_at=?
                    WHERE reference_id=?
                    """,
                    (to_version, now, reference_id),
                )
                connection.commit()
            except LocalSecretStoreError:
                _rollback(connection)
                raise
            except sqlite3.IntegrityError:
                _rollback(connection)
                raise LocalSecretStoreError("local_secret.rotation_in_progress") from None
            except sqlite3.Error:
                _rollback(connection)
                raise LocalSecretStoreError("local_secret.storage_unavailable") from None
            finally:
                connection.close()

        result = self._test_candidate(reference_id, to_version, tester)
        return self._finish_rotation(
            reference_id,
            rotation_id=rotation_id,
            from_version=from_version,
            to_version=to_version,
            result=result,
        )

    def test_connection(
        self,
        reference_id: str,
        *,
        tenant_id: str,
        workspace_id: str,
        tester: Callable[[memoryview], ProviderTestResult],
    ) -> LocalProviderConnectionTest:
        if not callable(tester):
            raise LocalSecretStoreError("local_secret.tester_invalid")
        with self.acquire(reference_id, tenant_id=tenant_id, workspace_id=workspace_id) as lease:
            result = self._invoke_tester(tester, lease.view())
            version = lease.version
        return self._record_test(reference_id, version=version, result=result)

    def delete(
        self,
        reference_id: str,
        *,
        tenant_id: str,
        workspace_id: str,
        expected_revision: int,
    ) -> SecretReference:
        now = self._time()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                secret = self._owned_secret(
                    connection,
                    reference_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )
                if int(secret["revision"]) != expected_revision:
                    raise LocalSecretStoreError("local_secret.revision_conflict")
                if secret["candidate_version"] is not None or secret["status"] == "rotating":
                    raise LocalSecretStoreError("local_secret.rotation_in_progress")
                consumers = connection.execute(
                    "SELECT slot FROM local_secret_consumers WHERE reference_id=? LIMIT 1",
                    (reference_id,),
                ).fetchone()
                active_leases = any(
                    ref == reference_id and count > 0
                    for (ref, _version), count in self._leases.items()
                )
                if consumers is not None or active_leases:
                    raise LocalSecretStoreError("local_secret.in_use")
                connection.execute(
                    """
                    UPDATE local_secrets
                    SET status='disabled',revision=revision+1,active_version=NULL,
                        candidate_version=NULL,last_test_outcome=NULL,capabilities_json='[]',
                        updated_at=? WHERE reference_id=?
                    """,
                    (now, reference_id),
                )
                connection.execute(
                    "DELETE FROM local_secret_versions WHERE reference_id=?", (reference_id,)
                )
                reference = self._reference(connection, reference_id)
                self._event(connection, "edge.secret_reference_changed", reference, now=now)
                connection.commit()
                return reference
            except LocalSecretStoreError:
                _rollback(connection)
                raise
            except sqlite3.Error:
                _rollback(connection)
                raise LocalSecretStoreError("local_secret.storage_unavailable") from None
            finally:
                connection.close()

    def bind_consumer(
        self,
        reference_id: str,
        *,
        slot: ConsumerSlot,
        consumer_id: str,
        version: int,
    ) -> None:
        if slot not in _CONSUMER_SLOTS or not 1 <= len(consumer_id) <= 80:
            raise LocalSecretStoreError("local_secret.consumer_invalid")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                exists = connection.execute(
                    "SELECT 1 FROM local_secret_versions WHERE reference_id=? AND version=?",
                    (reference_id, version),
                ).fetchone()
                if exists is None:
                    raise LocalSecretStoreError("local_secret.not_found")
                connection.execute(
                    """
                    INSERT INTO local_secret_consumers(
                      reference_id,slot,consumer_id,version,created_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(reference_id,slot,consumer_id)
                    DO UPDATE SET version=excluded.version,created_at=excluded.created_at
                    """,
                    (reference_id, slot, consumer_id, version, self._time()),
                )
                connection.commit()
            except LocalSecretStoreError:
                _rollback(connection)
                raise
            except sqlite3.Error:
                _rollback(connection)
                raise LocalSecretStoreError("local_secret.storage_unavailable") from None
            finally:
                connection.close()

    def unbind_consumer(self, reference_id: str, *, slot: ConsumerSlot, consumer_id: str) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    DELETE FROM local_secret_consumers
                    WHERE reference_id=? AND slot=? AND consumer_id=?
                    """,
                    (reference_id, slot, consumer_id),
                )
                connection.commit()
            except sqlite3.Error:
                raise LocalSecretStoreError("local_secret.storage_unavailable") from None
            finally:
                connection.close()

    def get_reference(
        self, reference_id: str, *, tenant_id: str, workspace_id: str
    ) -> SecretReference:
        connection = self._connect()
        try:
            row = self._owned_secret(
                connection,
                reference_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            return self._reference_from_row(row)
        finally:
            connection.close()

    def list_references(self, *, tenant_id: str, workspace_id: str) -> tuple[SecretReference, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM local_secrets
                WHERE deployment_id=? AND tenant_id=? AND workspace_id=?
                ORDER BY label,reference_id
                """,
                (self._deployment_id, tenant_id, workspace_id),
            ).fetchall()
            return tuple(self._reference_from_row(row) for row in rows)
        except sqlite3.Error:
            raise LocalSecretStoreError("local_secret.storage_unavailable") from None
        finally:
            connection.close()

    def _test_candidate(
        self,
        reference_id: str,
        version: int,
        tester: Callable[[memoryview], ProviderTestResult],
    ) -> ProviderTestResult:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT s.tenant_id,s.workspace_id,s.kind,s.provider_connection_id,v.*
                FROM local_secrets s JOIN local_secret_versions v USING(reference_id)
                WHERE s.reference_id=? AND v.version=? AND v.status='candidate'
                """,
                (reference_id, version),
            ).fetchone()
            if row is None:
                raise LocalSecretStoreError("local_secret.rotation_conflict")
            try:
                lease = decrypt_local_secret(
                    self._envelope(row),
                    owner=self._owner_from_rows(row, reference_id, version),
                    keyring=self._keyring,
                )
            except Exception:
                return ProviderTestResult("provider_unavailable", (), "lt_100ms")
            with lease:
                return self._invoke_tester(tester, lease.view())
        finally:
            connection.close()

    def _finish_rotation(
        self,
        reference_id: str,
        *,
        rotation_id: str,
        from_version: int,
        to_version: int,
        result: ProviderTestResult,
    ) -> SecretReference:
        now = self._time()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                secret = connection.execute(
                    "SELECT * FROM local_secrets WHERE reference_id=?", (reference_id,)
                ).fetchone()
                if (
                    secret is None
                    or secret["status"] != "rotating"
                    or int(secret["candidate_version"]) != to_version
                    or int(secret["active_version"]) != from_version
                ):
                    raise LocalSecretStoreError("local_secret.rotation_conflict")
                self._insert_test(connection, reference_id, to_version, result, now)
                if result.outcome != "succeeded":
                    connection.execute(
                        """
                        UPDATE local_secret_versions SET status='failed'
                        WHERE reference_id=? AND version=?
                        """,
                        (reference_id, to_version),
                    )
                    connection.execute(
                        """
                        UPDATE local_secret_rotations
                        SET status='failed',test_outcome=?,completed_at=?
                        WHERE rotation_id=?
                        """,
                        (result.outcome, now, rotation_id),
                    )
                    connection.execute(
                        """
                        UPDATE local_secrets SET status='degraded',candidate_version=NULL,
                          last_test_outcome=?,capabilities_json='[]',revision=revision+1,updated_at=?
                        WHERE reference_id=?
                        """,
                        (result.outcome, now, reference_id),
                    )
                    reference = self._reference(connection, reference_id)
                    self._event(
                        connection,
                        "edge.secret_rotation_failed",
                        {
                            "reference_id": reference_id,
                            "revision": reference.revision,
                            "outcome": result.outcome,
                            "updated_at": now,
                        },
                        now=now,
                    )
                else:
                    draining = self._leases.get((reference_id, from_version), 0) > 0
                    old_status = "draining" if draining else "retired"
                    connection.execute(
                        """
                        UPDATE local_secret_versions
                        SET status=?,drain_started_at=?,retired_at=?
                        WHERE reference_id=? AND version=?
                        """,
                        (
                            old_status,
                            now,
                            None if draining else now,
                            reference_id,
                            from_version,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE local_secret_versions SET status='active',activated_at=?
                        WHERE reference_id=? AND version=?
                        """,
                        (now, reference_id, to_version),
                    )
                    capabilities = _capabilities_json(result.capabilities)
                    connection.execute(
                        """
                        UPDATE local_secrets
                        SET status='active',active_version=?,candidate_version=NULL,
                          last_test_outcome=?,capabilities_json=?,rotated_at=?,revision=revision+1,
                          updated_at=? WHERE reference_id=?
                        """,
                        (
                            to_version,
                            result.outcome,
                            capabilities,
                            now,
                            now,
                            reference_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE local_secret_rotations
                        SET status=?,test_outcome=?,activated_at=?,completed_at=?
                        WHERE rotation_id=?
                        """,
                        (
                            "draining" if draining else "completed",
                            result.outcome,
                            now,
                            None if draining else now,
                            rotation_id,
                        ),
                    )
                    reference = self._reference(connection, reference_id)
                    self._event(connection, "edge.secret_reference_changed", reference, now=now)
                self._event(
                    connection,
                    "edge.provider_test_completed",
                    {
                        "reference_id": reference_id,
                        "version": to_version,
                        "outcome": result.outcome,
                        "capabilities": list(result.capabilities),
                        "latency_bucket": result.latency_bucket,
                        "created_at": now,
                    },
                    now=now,
                )
                connection.commit()
                if result.outcome == "succeeded" and draining:
                    self._draining.add((reference_id, from_version))
                return reference
            except LocalSecretStoreError:
                _rollback(connection)
                raise
            except sqlite3.Error:
                _rollback(connection)
                raise LocalSecretStoreError("local_secret.storage_unavailable") from None
            finally:
                connection.close()

    def support_bundle_inputs(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        scanner_hmac_key: bytes,
    ) -> tuple[dict[str, object], tuple[str, ...]]:
        """Return an exact operational snapshot plus HMAC-only local-value scan material."""

        if not 32 <= len(scanner_hmac_key) <= 512:
            raise LocalSecretStoreError("local_secret.payload_invalid")
        references = self.list_references(tenant_id=tenant_id, workspace_id=workspace_id)
        fingerprints: list[str] = []
        for reference in references:
            if reference.status == "disabled":
                continue
            with self.acquire(
                reference.reference_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            ) as lease:
                fingerprints.append(
                    hmac.new(scanner_hmac_key, lease.view(), hashlib.sha256).hexdigest()
                )
        connection = self._connect()
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise LocalSecretStoreError("local_secret.storage_unavailable")
            counts = {
                str(row["status"]): int(row["entry_count"])
                for row in connection.execute(
                    """
                    SELECT status,COUNT(*) entry_count FROM local_secrets
                    WHERE deployment_id=? AND tenant_id=? AND workspace_id=?
                    GROUP BY status ORDER BY status
                    """,
                    (self._deployment_id, tenant_id, workspace_id),
                ).fetchall()
            }
            revision = int(
                connection.execute(
                    """
                    SELECT coalesce(MAX(revision),0) FROM local_secrets
                    WHERE deployment_id=? AND tenant_id=? AND workspace_id=?
                    """,
                    (self._deployment_id, tenant_id, workspace_id),
                ).fetchone()[0]
            )
            delivery = connection.execute(
                """
                SELECT COUNT(*) total_count,
                  coalesce(SUM(CASE WHEN delivered_at IS NULL THEN 1 ELSE 0 END),0) pending_count,
                  coalesce(MAX(outbox.created_at),'1970-01-01T00:00:00.000Z') latest_at
                FROM local_secret_outbox outbox
                JOIN local_secrets owner ON owner.reference_id=outbox.reference_id
                WHERE owner.deployment_id=? AND owner.tenant_id=? AND owner.workspace_id=?
                """,
                (self._deployment_id, tenant_id, workspace_id),
            ).fetchone()
            outcomes = [
                {"code": str(row["outcome"]), "count": int(row["outcome_count"])}
                for row in connection.execute(
                    """
                    SELECT test.outcome,COUNT(*) outcome_count
                    FROM local_provider_connection_tests test
                    JOIN local_secrets owner ON owner.reference_id=test.reference_id
                    WHERE owner.deployment_id=? AND owner.tenant_id=? AND owner.workspace_id=?
                    GROUP BY test.outcome ORDER BY test.outcome
                    """,
                    (self._deployment_id, tenant_id, workspace_id),
                ).fetchall()
            ]
            events = [
                json.dumps(
                    {
                        "event_code": str(row["event_type"]),
                        "occurred_at": str(row["created_at"]),
                        "delivery_state": (
                            "delivered" if row["delivered_at"] is not None else "pending"
                        ),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for row in connection.execute(
                    """
                    SELECT outbox.event_type,outbox.created_at,outbox.delivered_at
                    FROM local_secret_outbox outbox
                    JOIN local_secrets owner ON owner.reference_id=outbox.reference_id
                    WHERE owner.deployment_id=? AND owner.tenant_id=? AND owner.workspace_id=?
                    ORDER BY outbox.created_at DESC,outbox.event_id DESC LIMIT 100
                    """,
                    (self._deployment_id, tenant_id, workspace_id),
                ).fetchall()
            ]
            snapshot: dict[str, object] = {
                "runtime.version": {
                    "schema_version": "1",
                    "component_code": "gateway",
                    "deployment_sha256": hashlib.sha256(
                        self._deployment_id.encode("ascii")
                    ).hexdigest(),
                    "local_schema_version": _SCHEMA_VERSION,
                },
                "runtime.watermarks": {
                    "revision": revision,
                    "event_count": int(delivery["total_count"]),
                    "pending_event_count": int(delivery["pending_count"]),
                    "latest_event_at": str(delivery["latest_at"]),
                },
                "runtime.health": {
                    "storage_outcome": integrity,
                    "reference_count": len(references),
                    "active_count": counts.get("active", 0),
                    "rotating_count": counts.get("rotating", 0),
                    "degraded_count": counts.get("degraded", 0),
                    "disabled_count": counts.get("disabled", 0),
                },
                "runtime.errors": {"outcomes": outcomes},
                "runtime.logs": ("\n".join(reversed(events)) + "\n") if events else "{}\n",
            }
            return snapshot, tuple(sorted(fingerprints))
        except LocalSecretStoreError:
            raise
        except sqlite3.Error:
            raise LocalSecretStoreError("local_secret.storage_unavailable") from None
        finally:
            connection.close()

    def next_sync_batch(self, *, limit: int = 64) -> EdgeSecretSyncBatch | None:
        """Claim the oldest safe outbox facts under one retry-stable batch ID."""

        if type(limit) is not int or not 1 <= limit <= 64:
            raise LocalSecretStoreError("local_secret.sync_invalid")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                first = connection.execute(
                    """
                    SELECT * FROM local_secret_outbox WHERE delivered_at IS NULL
                    ORDER BY created_at,event_id LIMIT 1
                    """
                ).fetchone()
                if first is None:
                    connection.rollback()
                    return None
                batch_id = first["delivery_batch_id"]
                if batch_id is None:
                    batch_id = _id("lsb_")
                    rows = connection.execute(
                        """
                        SELECT * FROM local_secret_outbox
                        WHERE delivered_at IS NULL AND delivery_batch_id IS NULL
                        ORDER BY created_at,event_id LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT * FROM local_secret_outbox
                        WHERE delivered_at IS NULL AND delivery_batch_id=?
                        ORDER BY created_at,event_id
                        """,
                        (batch_id,),
                    ).fetchall()
                if not rows:
                    raise LocalSecretStoreError("local_secret.storage_invalid")
                events = [self._sync_event(row) for row in rows]
                now = self._time()
                batch = EdgeSecretSyncBatch(
                    batch_id=str(batch_id),
                    deployment_id=self._deployment_id,
                    events=events,
                    # The cloud hashes the whole batch for idempotency. Deriving sent_at
                    # from immutable events keeps the payload stable across receipt-loss retries.
                    sent_at=events[-1].occurred_at,
                )
                event_ids = [str(row["event_id"]) for row in rows]
                placeholders = ",".join("?" for _ in event_ids)
                connection.execute(
                    f"""
                    UPDATE local_secret_outbox SET delivery_batch_id=?,
                      delivery_attempt_count=delivery_attempt_count+1,last_attempt_at=?
                    WHERE event_id IN({placeholders}) AND delivered_at IS NULL
                    """,
                    (batch.batch_id, now, *event_ids),
                )
                connection.commit()
                return batch
            except LocalSecretStoreError:
                _rollback(connection)
                raise
            except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
                _rollback(connection)
                raise LocalSecretStoreError("local_secret.storage_invalid") from None
            finally:
                connection.close()

    def acknowledge_sync_receipt(self, receipt: EdgeSecretSyncReceipt) -> int:
        """Mark a complete batch delivered only after exact receipt validation."""

        if (
            type(receipt) is not EdgeSecretSyncReceipt
            or receipt.deployment_id != self._deployment_id
        ):
            raise LocalSecretStoreError("local_secret.receipt_invalid")
        expected = receipt.accepted_event_count + receipt.duplicate_event_count
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    """
                    SELECT event_id,delivery_receipt_id,delivered_at
                    FROM local_secret_outbox WHERE delivery_batch_id=?
                    ORDER BY created_at,event_id
                    """,
                    (receipt.batch_id,),
                ).fetchall()
                if len(rows) != expected or not rows:
                    raise LocalSecretStoreError("local_secret.receipt_invalid")
                delivered = [row for row in rows if row["delivered_at"] is not None]
                if delivered:
                    if len(delivered) != len(rows) or any(
                        row["delivery_receipt_id"] != receipt.receipt_id for row in rows
                    ):
                        raise LocalSecretStoreError("local_secret.receipt_invalid")
                    connection.rollback()
                    return 0
                now = self._time()
                changed = connection.execute(
                    """
                    UPDATE local_secret_outbox SET delivery_receipt_id=?,delivered_at=?
                    WHERE delivery_batch_id=? AND delivered_at IS NULL
                    """,
                    (receipt.receipt_id, now, receipt.batch_id),
                ).rowcount
                if changed != expected:
                    raise LocalSecretStoreError("local_secret.receipt_invalid")
                connection.commit()
                return changed
            except LocalSecretStoreError:
                _rollback(connection)
                raise
            except sqlite3.Error:
                _rollback(connection)
                raise LocalSecretStoreError("local_secret.storage_unavailable") from None
            finally:
                connection.close()

    def pending_sync_event_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM local_secret_outbox WHERE delivered_at IS NULL"
            ).fetchone()
            return int(row[0])
        except sqlite3.Error:
            raise LocalSecretStoreError("local_secret.storage_unavailable") from None
        finally:
            connection.close()

    def _record_test(
        self, reference_id: str, *, version: int, result: ProviderTestResult
    ) -> LocalProviderConnectionTest:
        now = self._time()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                test_id = self._insert_test(connection, reference_id, version, result, now)
                connection.execute(
                    """
                    UPDATE local_secrets SET status=?,last_test_outcome=?,capabilities_json=?,
                      revision=revision+1,updated_at=? WHERE reference_id=?
                    """,
                    (
                        "active" if result.outcome == "succeeded" else "degraded",
                        result.outcome,
                        _capabilities_json(result.capabilities),
                        now,
                        reference_id,
                    ),
                )
                reference = self._reference(connection, reference_id)
                self._event(connection, "edge.secret_reference_changed", reference, now=now)
                self._event(
                    connection,
                    "edge.provider_test_completed",
                    {
                        "reference_id": reference_id,
                        "version": version,
                        "outcome": result.outcome,
                        "capabilities": list(result.capabilities),
                        "latency_bucket": result.latency_bucket,
                        "created_at": now,
                    },
                    now=now,
                )
                connection.commit()
                return LocalProviderConnectionTest(
                    test_id=test_id,
                    reference_id=reference_id,
                    version=version,
                    outcome=result.outcome,
                    capabilities=result.capabilities,
                    latency_bucket=result.latency_bucket,
                    created_at=now,
                )
            except sqlite3.Error:
                _rollback(connection)
                raise LocalSecretStoreError("local_secret.storage_unavailable") from None
            finally:
                connection.close()

    def _release(self, reference_id: str, version: int) -> None:
        with self._lock:
            key = (reference_id, version)
            count = self._leases.get(key, 0)
            if count <= 0:
                return
            if count > 1:
                self._leases[key] = count - 1
                return
            del self._leases[key]
            if key not in self._draining:
                return
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                version_row = connection.execute(
                    "SELECT status FROM local_secret_versions WHERE reference_id=? AND version=?",
                    (reference_id, version),
                ).fetchone()
                if version_row is not None and version_row["status"] == "draining":
                    now = self._time()
                    connection.execute(
                        """
                        UPDATE local_secret_versions SET status='retired',retired_at=?
                        WHERE reference_id=? AND version=?
                        """,
                        (now, reference_id, version),
                    )
                    connection.execute(
                        """
                        UPDATE local_secret_rotations SET status='completed',completed_at=?
                        WHERE reference_id=? AND from_version=? AND status='draining'
                        """,
                        (now, reference_id, version),
                    )
                connection.commit()
                self._draining.discard(key)
            except sqlite3.Error:
                _rollback(connection)
            finally:
                connection.close()

    def _invoke_tester(
        self,
        tester: Callable[[memoryview], ProviderTestResult],
        secret: memoryview,
    ) -> ProviderTestResult:
        try:
            result = tester(secret)
        except Exception:
            return ProviderTestResult("provider_unavailable", (), "gte_2000ms")
        if type(result) is not ProviderTestResult:
            return ProviderTestResult("provider_unavailable", (), "gte_2000ms")
        return result

    def _insert_test(
        self,
        connection: sqlite3.Connection,
        reference_id: str,
        version: int,
        result: ProviderTestResult,
        now: str,
    ) -> str:
        test_id = _id("lpt_")
        connection.execute(
            """
            INSERT INTO local_provider_connection_tests(
              test_id,reference_id,version,outcome,capabilities_json,latency_bucket,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                test_id,
                reference_id,
                version,
                result.outcome,
                _capabilities_json(result.capabilities),
                result.latency_bucket,
                now,
            ),
        )
        return test_id

    def _insert_version(
        self,
        connection: sqlite3.Connection,
        *,
        reference_id: str,
        version: int,
        binding_digest: str,
        status: str,
        encrypted: EncryptedLocalSecret,
        now: str,
        activated_at: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO local_secret_versions(
              reference_id,version,binding_digest,status,ciphertext_format,kek_version,
              wrap_nonce,wrapped_data_key,value_nonce,ciphertext,created_at,activated_at,
              drain_started_at,retired_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)
            """,
            (
                reference_id,
                version,
                binding_digest,
                status,
                encrypted.ciphertext_format,
                encrypted.kek_version,
                encrypted.wrap_nonce,
                encrypted.wrapped_data_key,
                encrypted.value_nonce,
                encrypted.ciphertext,
                now,
                activated_at,
            ),
        )

    def _owned_secret(
        self,
        connection: sqlite3.Connection,
        reference_id: str,
        *,
        tenant_id: str,
        workspace_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM local_secrets WHERE reference_id=? AND deployment_id=?",
            (reference_id, self._deployment_id),
        ).fetchone()
        if row is None or row["tenant_id"] != tenant_id or row["workspace_id"] != workspace_id:
            raise LocalSecretStoreError("local_secret.not_found")
        return row

    def _reference(self, connection: sqlite3.Connection, reference_id: str) -> SecretReference:
        row = connection.execute(
            "SELECT * FROM local_secrets WHERE reference_id=?", (reference_id,)
        ).fetchone()
        if row is None:
            raise LocalSecretStoreError("local_secret.not_found")
        return self._reference_from_row(row)

    def _reference_from_row(self, row: sqlite3.Row) -> SecretReference:
        try:
            return SecretReference.model_validate(
                {
                    "schema_version": "1",
                    "reference_id": row["reference_id"],
                    "deployment_id": row["deployment_id"],
                    "tenant_id": row["tenant_id"],
                    "workspace_id": row["workspace_id"],
                    "provider_connection_id": row["provider_connection_id"],
                    "label": row["label"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "revision": int(row["revision"]),
                    "active_version": (
                        int(row["active_version"]) if row["active_version"] is not None else None
                    ),
                    "last_test_outcome": row["last_test_outcome"],
                    "capabilities": json.loads(row["capabilities_json"]),
                    "rotated_at": row["rotated_at"],
                    "updated_at": row["updated_at"],
                }
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            raise LocalSecretStoreError("local_secret.storage_invalid") from None

    def _owner_from_rows(
        self, row: sqlite3.Row, reference_id: str, version: int
    ) -> LocalSecretOwner:
        binding_digest = (
            str(row["binding_digest"])
            if "binding_digest" in row.keys()
            else _binding_digest(str(row["provider_connection_id"]))
        )
        return self._owner(
            tenant_id=str(row["tenant_id"]),
            workspace_id=str(row["workspace_id"]),
            reference_id=reference_id,
            version=version,
            kind=str(row["kind"]),  # type: ignore[arg-type]
            binding_digest=binding_digest,
        )

    def _owner(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        reference_id: str,
        version: int,
        kind: LocalSecretKind,
        binding_digest: str,
    ) -> LocalSecretOwner:
        return LocalSecretOwner(
            deployment_id=self._deployment_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            reference_id=reference_id,
            version=version,
            kind=kind,
            binding_digest=binding_digest,
        )

    def _envelope(self, row: sqlite3.Row) -> EncryptedLocalSecret:
        return EncryptedLocalSecret(
            ciphertext_format=str(row["ciphertext_format"]),
            kek_version=int(row["kek_version"]),
            wrap_nonce=bytes(row["wrap_nonce"]),
            wrapped_data_key=bytes(row["wrapped_data_key"]),
            value_nonce=bytes(row["value_nonce"]),
            ciphertext=bytes(row["ciphertext"]),
        )

    def _event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        payload: SecretReference | dict[str, object],
        *,
        now: str,
    ) -> None:
        if event_type not in _EVENT_TYPES:
            raise LocalSecretStoreError("local_secret.event_invalid")
        body = payload.model_dump(mode="json") if isinstance(payload, SecretReference) else payload
        connection.execute(
            """
            INSERT INTO local_secret_outbox(
              event_id,event_type,reference_id,payload_json,created_at,delivered_at
            ) VALUES(?,?,?,?,?,NULL)
            """,
            (
                _id("lse_"),
                event_type,
                body["reference_id"],
                json.dumps(body, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )

    def _sync_event(self, row: sqlite3.Row):
        body = json.loads(str(row["payload_json"]))
        if type(body) is not dict:
            raise LocalSecretStoreError("local_secret.storage_invalid")
        common = {
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "occurred_at": str(row["created_at"]),
        }
        if row["event_type"] == "edge.secret_reference_changed":
            reference = SecretReference.model_validate(body)
            return EdgeSecretReferenceChangedEvent(**common, reference=reference)
        if row["event_type"] == "edge.secret_rotation_failed":
            if set(body) != {"reference_id", "revision", "outcome", "updated_at"}:
                raise LocalSecretStoreError("local_secret.storage_invalid")
            return EdgeSecretRotationFailedEvent(
                **common,
                reference_id=body["reference_id"],
                revision=body["revision"],
                outcome=body["outcome"],
            )
        if row["event_type"] == "edge.provider_test_completed":
            if set(body) != {
                "reference_id",
                "version",
                "outcome",
                "capabilities",
                "latency_bucket",
                "created_at",
            }:
                raise LocalSecretStoreError("local_secret.storage_invalid")
            return EdgeProviderTestCompletedEvent(
                **common,
                reference_id=body["reference_id"],
                version=body["version"],
                outcome=body["outcome"],
                capabilities=body["capabilities"],
                latency_bucket=body["latency_bucket"],
            )
        raise LocalSecretStoreError("local_secret.storage_invalid")

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, _SCHEMA_VERSION}:
                raise LocalSecretStoreError("local_secret.schema_unsupported")
            if version == 1:
                connection.executescript(_MIGRATE_V1_TO_V2)
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            now = self._time()
            connection.execute(
                """
                UPDATE local_secret_versions SET status='retired',retired_at=?
                WHERE status='draining'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE local_secret_rotations SET status='completed',completed_at=?
                WHERE status='draining'
                """,
                (now,),
            )
            connection.commit()
        except LocalSecretStoreError:
            raise
        except sqlite3.Error:
            raise LocalSecretStoreError("local_secret.storage_unavailable") from None
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            return connection
        except sqlite3.Error:
            raise LocalSecretStoreError("local_secret.storage_unavailable") from None

    def _time(self) -> str:
        try:
            value = self._now()
            if value.tzinfo is None:
                raise ValueError
            return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except Exception:
            raise LocalSecretStoreError("local_secret.time_invalid") from None


def _binding_digest(provider_connection_id: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"provider_connection_id": provider_connection_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _capabilities_json(values: tuple[ProviderCapability, ...]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error:
        pass


__all__ = (
    "ConsumerSlot",
    "LatencyBucket",
    "LocalProviderConnectionTest",
    "LocalSecretStore",
    "LocalSecretStoreError",
    "LocalVersionLease",
    "ProviderTestResult",
)
