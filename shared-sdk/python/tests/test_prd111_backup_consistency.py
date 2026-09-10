"""Real SQLite restoration and data-derived S3 recovery evidence."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from dataclasses import replace

import pytest
from metahub_shared.infrastructure import backups as b
from metahub_shared.runtime.config import load_app_config

SCHEMA = """
CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,status TEXT NOT NULL);
INSERT INTO schema_migrations VALUES('0152','applied');
CREATE TABLE workspaces(workspace_id TEXT PRIMARY KEY);
CREATE TABLE tenants(tenant_id TEXT PRIMARY KEY);
CREATE TABLE workspace_tenant_bindings(
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  PRIMARY KEY(workspace_id,tenant_id));
INSERT INTO workspaces VALUES('wsp_test');
INSERT INTO tenants VALUES('ten_test');
INSERT INTO workspace_tenant_bindings VALUES('wsp_test','ten_test');
CREATE TABLE finops_ledger_entries(
  ledger_sequence INTEGER PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  amount_micros INTEGER NOT NULL,direction TEXT NOT NULL);
INSERT INTO finops_ledger_entries VALUES(1,'ten_test',300,'credit');
INSERT INTO finops_ledger_entries VALUES(2,'ten_test',100,'debit');
CREATE TABLE config_snapshot_heads(
  workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id),
  generation INTEGER NOT NULL,manifest_sha256 TEXT NOT NULL);
INSERT INTO config_snapshot_heads VALUES('wsp_test',7,'config-test-digest');
CREATE TABLE config_event_outbox(
  event_id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL);
INSERT INTO config_event_outbox VALUES('evt_test','sensitive-test-body','pending');
"""


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


@pytest.fixture
def config(monkeypatch, tmp_path):
    for key in os.environ:
        if key.startswith("METAHUB_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    values = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": f"sqlite:///{(tmp_path / 'source.db').as_posix()}",
        "METAHUB_KMS_PROVIDER": "test",
        "METAHUB_KMS_TEST_MASTER_KEY": "isolated-restore-master-key-test-only-12345",
        "METAHUB_OBJECT_STORE_BUCKET": "isolated-backups",
        "METAHUB_OBJECT_STORE_ENDPOINT_URL": "http://objects.invalid:9000",
        "METAHUB_OBJECT_STORE_REGION": "local-1",
        "METAHUB_OBJECT_STORE_ACCESS_KEY_ID": "isolated-test-access",
        "METAHUB_OBJECT_STORE_SECRET_ACCESS_KEY": "isolated-test-secret",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with sqlite3.connect(tmp_path / "source.db") as connection:
        connection.executescript(SCHEMA)
    return load_app_config("control-plane-api")


@pytest.fixture
def store(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(b, "_boto_client", lambda *args: store)
    return store


def restore(config, artifact):
    backup_id = "bkp_" + "a" * 32
    stored = b.store_backup(config, backup_id=backup_id, artifact=artifact)
    return b.restore_and_verify(
        config,
        backup_id=backup_id,
        object_key=stored.object_key,
        expected_manifest_sha256=stored.manifest_sha256,
    )


def test_real_restore_compares_all_critical_tables_and_exposes_no_row_values(config, store):
    artifact = b.create_snapshot(config, backup_type="full")
    proof = restore(config, artifact)
    assert proof.consistency == artifact.consistency
    assert {item.component_code for item in proof.consistency} == {
        "database",
        "ownership",
        "ledger",
        "outbox",
        "config",
    }
    assert proof.content_sha256 == hashlib.sha256(artifact.payload).hexdigest()
    assert proof.source_watermark == artifact.source_watermark
    assert proof.schema_head == "0152"
    text = repr(proof) + repr(list(store.objects.values()))
    for secret in ("sensitive-test-body", "ten_test", "wsp_test", "config-test-digest"):
        assert secret not in text


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE finops_ledger_entries SET amount_micros=900 WHERE ledger_sequence=1",
        "UPDATE config_snapshot_heads SET generation=8",
        "UPDATE config_event_outbox SET payload='changed-but-same-count'",
        "DELETE FROM config_event_outbox",
        "DROP TABLE config_event_outbox",
        "ALTER TABLE config_event_outbox ADD COLUMN omitted TEXT",
        "INSERT INTO tenants VALUES('ten_other'); "
        "UPDATE workspace_tenant_bindings SET tenant_id='ten_other'",
    ],
)
def test_restoration_rejects_data_changes_even_with_resealed_valid_envelope(
    config,
    store,
    tmp_path,
    change,
):
    original = b.create_snapshot(config, backup_type="full")
    path = tmp_path / "changed.db"
    path.write_bytes(original.payload)
    with sqlite3.connect(path) as connection:
        connection.executescript(change)
    modified = replace(original, payload=path.read_bytes())
    with pytest.raises(b.BackupRuntimeError, match="^backup.restore_consistency_failed$"):
        restore(config, modified)


def test_v1_remains_readable_without_claiming_v2_consistency(config, store):
    artifact = replace(b.create_snapshot(config, backup_type="full"), consistency=None)
    proof = restore(config, artifact)
    assert proof.consistency is None
    assert proof.schema_head == "0152"


def test_snapshot_proof_is_from_copy_not_source_after_backup(config, store, monkeypatch):
    original = b.fingerprint_database

    def observe(connection, *, backend):
        with sqlite3.connect("source.db") as writer:
            writer.execute("UPDATE config_snapshot_heads SET generation=9")
        return original(connection, backend=backend)

    monkeypatch.setattr(b, "fingerprint_database", observe)
    artifact = b.create_snapshot(config, backup_type="full")
    monkeypatch.setattr(b, "fingerprint_database", original)
    assert restore(config, artifact).consistency == artifact.consistency
    later = b.create_snapshot(config, backup_type="full")
    assert later.consistency != artifact.consistency


def test_keyset_paging_reads_every_row_with_composite_primary_key(config, store):
    with sqlite3.connect("source.db") as connection:
        connection.execute(
            "CREATE TABLE test_event_outbox(a INTEGER,b INTEGER,payload BLOB,PRIMARY KEY(a,b))"
        )
        connection.executemany(
            "INSERT INTO test_event_outbox VALUES(?,?,?)",
            [(index // 7, index % 7, bytes([index % 256])) for index in range(700)],
        )
    artifact = b.create_snapshot(config, backup_type="full")
    item = next(item for item in artifact.consistency if item.table_name == "test_event_outbox")
    assert item.row_count == 700
    assert restore(config, artifact).consistency == artifact.consistency


@pytest.mark.parametrize(
    "schema",
    [
        "CREATE TABLE test_outbox(payload TEXT)",
        "CREATE TABLE test_outbox(id TEXT PRIMARY KEY); INSERT INTO test_outbox VALUES(NULL)",
        'CREATE TABLE test_outbox("unsafe-column" TEXT PRIMARY KEY)',
    ],
)
def test_unsupported_critical_table_never_silently_disappears(config, schema):
    with sqlite3.connect("source.db") as connection:
        connection.executescript(schema)
    with pytest.raises(b.BackupRuntimeError, match="^backup.snapshot_failed$"):
        b.create_snapshot(config, backup_type="full")


def test_fingerprints_are_authenticated_by_manifest(config, store):
    artifact = b.create_snapshot(config, backup_type="full")
    backup_id = "bkp_" + "b" * 32
    stored = b.store_backup(config, backup_id=backup_id, artifact=artifact)
    key = (config.object_store_bucket, stored.object_key)
    envelope = json.loads(store.objects[key])
    envelope["manifest"]["consistency"][0]["row_count"] += 1
    store.objects[key] = json.dumps(envelope).encode()
    with pytest.raises(b.BackupRuntimeError, match="^backup.manifest_invalid$"):
        b.restore_and_verify(
            config,
            backup_id=backup_id,
            object_key=stored.object_key,
            expected_manifest_sha256=stored.manifest_sha256,
        )


@pytest.mark.parametrize(
    "query",
    [
        "host=source",
        "dbname=source",
        "user=admin",
        "service=production",
        "options=-csearch_path%3Dsecret",
        "sslmode=require&sslmode=disable",
        "broken",
    ],
)
def test_postgres_connection_identity_overrides_are_rejected(query):
    with pytest.raises(b.BackupRuntimeError, match="^backup.database_url_unsafe$"):
        b._validate_postgres_url("postgresql://user@localhost/isolated?" + query)


def test_postgres_subprocess_ignores_ambient_libpq_overrides(monkeypatch):
    for name in ("PGHOST", "PGSERVICE", "PGOPTIONS", "PGPASSFILE", "PGPASSWORD"):
        monkeypatch.setenv(name, "never-use-this-value")
    environment = b._postgres_environment("postgresql://user@localhost/isolated")
    assert set(key for key in environment if key.startswith("PG")) == {
        "PGDATABASE",
        "PGCONNECT_TIMEOUT",
        "PGHOST",
        "PGPORT",
        "PGUSER",
    }
    assert environment["PGDATABASE"] == "isolated"
    assert environment["PGHOST"] == "localhost"
    assert environment["PGUSER"] == "user"


@pytest.mark.parametrize(
    "name", ["dbname=x%20host=source", "postgresql:%2F%2Fsource", "bad%00name"]
)
def test_database_name_cannot_be_interpreted_as_a_connection_string(name):
    with pytest.raises(b.BackupRuntimeError, match="^backup.database_url_unsafe$"):
        b._postgres_environment("postgresql://user@localhost/" + name)


def test_failed_object_read_closes_body_without_exposing_provider_error(config, monkeypatch):
    class BrokenBody(io.BytesIO):
        def read(self, *args):
            raise OSError("sensitive-test-provider-error")

    body = BrokenBody()

    class Store:
        def get_object(self, **kwargs):
            return {"Body": body}

    monkeypatch.setattr(b, "_boto_client", lambda *args: Store())
    with pytest.raises(b.BackupRuntimeError, match="^backup.object_store_unavailable$"):
        b._s3_get(config, object_key="test-backup")
    assert body.closed
