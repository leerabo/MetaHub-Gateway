"""PRD-100 production infrastructure adapter and verified backup contracts."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest
from metahub_shared.db import close_database_pools, connect_database
from metahub_shared.db.connection import _DatabaseRow, _PostgresConnection
from metahub_shared.db.errors import DbError
from metahub_shared.infrastructure import (
    BackupArtifact,
    BackupRuntimeError,
    create_snapshot,
    restore_and_verify,
    store_backup,
)
from metahub_shared.infrastructure import backups as backup_runtime
from metahub_shared.runtime.config import (
    ConfigError,
    _validate_production_infrastructure_config,
    load_app_config,
)


class _ObjectBody(io.BytesIO):
    pass


class _MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_kwargs) -> None:
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, _ObjectBody]:
        return {"Body": _ObjectBody(self.objects[(Bucket, Key)])}


def _backup_config(monkeypatch: pytest.MonkeyPatch, database_url: str):
    values = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_OBJECT_STORE_ENDPOINT_URL": "http://object-store.test:9000",
        "METAHUB_OBJECT_STORE_REGION": "local-1",
        "METAHUB_OBJECT_STORE_BUCKET": "metahub-backups",
        "METAHUB_OBJECT_STORE_ACCESS_KEY_ID": "local-test-access",
        "METAHUB_OBJECT_STORE_SECRET_ACCESS_KEY": "local-test-secret-value",
        "METAHUB_KMS_PROVIDER": "test",
        "METAHUB_KMS_TEST_MASTER_KEY": "local-test-master-key-with-32-bytes-minimum",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return load_app_config("control-plane-api")


def _create_snapshot_source(database_url: str) -> None:
    connection = connect_database(database_url)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(
              version TEXT PRIMARY KEY,
              status TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES('0092','applied');
            CREATE TABLE workspaces(workspace_id TEXT PRIMARY KEY);
            CREATE TABLE tenants(tenant_id TEXT PRIMARY KEY);
            CREATE TABLE workspace_tenant_bindings(
              workspace_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL,
              PRIMARY KEY(workspace_id,tenant_id),
              FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id),
              FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
            );
            INSERT INTO workspaces VALUES('wsp_11111111111111111111111111111111');
            INSERT INTO tenants VALUES('ten_22222222222222222222222222222222');
            INSERT INTO workspace_tenant_bindings VALUES(
              'wsp_11111111111111111111111111111111',
              'ten_22222222222222222222222222222222'
            );
            """
        )
    finally:
        connection.close()


def test_sqlite_snapshot_encryption_upload_and_isolated_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'source.sqlite3').as_posix()}"
    _create_snapshot_source(database_url)
    config = _backup_config(monkeypatch, database_url)
    object_store = _MemoryObjectStore()
    monkeypatch.setattr(backup_runtime, "_boto_client", lambda _config, service: object_store)

    artifact = create_snapshot(config, backup_type="full")
    assert artifact.schema_head == "0092"
    assert artifact.tenant_owner_sample_count == 1
    assert b"wsp_11111111111111111111111111111111" in artifact.payload

    backup_id = "bkp_" + "a" * 32
    stored = store_backup(config, backup_id=backup_id, artifact=artifact)
    envelope = object_store.objects[("metahub-backups", stored.object_key)]
    assert artifact.payload not in envelope
    assert b"local-test-master-key" not in envelope

    verification = restore_and_verify(
        config,
        backup_id=backup_id,
        object_key=stored.object_key,
        expected_manifest_sha256=stored.manifest_sha256,
    )
    assert verification.schema_head == "0092"
    assert verification.tenant_owner_sample_count == 1
    assert verification.manifest_sha256 == stored.manifest_sha256


def test_restore_rejects_extra_envelope_fields_and_owner_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'tamper.sqlite3').as_posix()}"
    _create_snapshot_source(database_url)
    config = _backup_config(monkeypatch, database_url)
    object_store = _MemoryObjectStore()
    monkeypatch.setattr(backup_runtime, "_boto_client", lambda _config, service: object_store)
    artifact = create_snapshot(config, backup_type="full")

    mismatched = BackupArtifact(
        payload=artifact.payload,
        source_watermark=artifact.source_watermark,
        schema_head=artifact.schema_head,
        tenant_owner_sample_count=0,
    )
    backup_id = "bkp_" + "b" * 32
    stored = store_backup(config, backup_id=backup_id, artifact=mismatched)
    with pytest.raises(BackupRuntimeError) as owner_error:
        restore_and_verify(
            config,
            backup_id=backup_id,
            object_key=stored.object_key,
            expected_manifest_sha256=stored.manifest_sha256,
        )
    assert owner_error.value.code == "backup.restore_owner_failed"

    envelope = json.loads(object_store.objects[("metahub-backups", stored.object_key)])
    envelope["object_url"] = "must-not-be-accepted"
    object_store.objects[("metahub-backups", stored.object_key)] = json.dumps(envelope).encode()
    with pytest.raises(BackupRuntimeError) as envelope_error:
        restore_and_verify(
            config,
            backup_id=backup_id,
            object_key=stored.object_key,
            expected_manifest_sha256=stored.manifest_sha256,
        )
    assert envelope_error.value.code == "backup.envelope_invalid"


def _validate_prod(**overrides) -> None:
    values = {
        "environment": "prod",
        "database_url": "postgresql://app@db.test/metahub?sslmode=require",
        "database_pool_min_size": 1,
        "database_pool_max_size": 10,
        "redis_url": "rediss://cache.test:6380/0",
        "object_store_endpoint_url": "https://objects.test",
        "object_store_region": "cn-test-1",
        "object_store_bucket": "metahub-backups",
        "object_store_access_key_id": "access-id",
        "object_store_secret_access_key": "secret-value",
        "kms_provider": "aws",
        "kms_key_id": "kms-key-id",
        "kms_test_master_key": None,
        "backup_worker_enabled": False,
        "backup_restore_database_url": None,
    }
    values.update(overrides)
    _validate_production_infrastructure_config(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"database_url": "sqlite:///prod.db"}, "SQLite"),
        ({"database_url": "postgresql://app@db.test/metahub"}, "TLS"),
        ({"object_store_secret_access_key": None}, "incomplete"),
        (
            {
                "kms_provider": "test",
                "kms_key_id": None,
                "kms_test_master_key": "x" * 32,
            },
            "test provider",
        ),
    ],
)
def test_production_infrastructure_rejects_unsafe_configuration(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ConfigError) as exc:
        _validate_prod(**overrides)
    assert exc.value.code == "infra.config_invalid"
    assert message.lower() in exc.value.message.lower()


def test_infrastructure_secrets_and_topology_are_excluded_from_config_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_password = "private-database-password"
    object_secret = "private-object-secret"
    kms_secret = "private-kms-test-master-key-value"
    values = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": (
            f"postgresql://app:{database_password}@private-db.test/metahub?sslmode=require"
        ),
        "METAHUB_REDIS_URL": "redis://private-cache.test:6379/0",
        "METAHUB_OBJECT_STORE_ENDPOINT_URL": "http://private-objects.test:9000",
        "METAHUB_OBJECT_STORE_REGION": "local-1",
        "METAHUB_OBJECT_STORE_BUCKET": "private-backups",
        "METAHUB_OBJECT_STORE_ACCESS_KEY_ID": "private-object-access",
        "METAHUB_OBJECT_STORE_SECRET_ACCESS_KEY": object_secret,
        "METAHUB_KMS_PROVIDER": "test",
        "METAHUB_KMS_TEST_MASTER_KEY": kms_secret,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    config = load_app_config("control-plane-api")
    projection = repr(config) + config.model_dump_json()
    for forbidden in (
        database_password,
        object_secret,
        kms_secret,
        "private-db.test",
        "private-cache.test",
        "private-objects.test",
        "private-backups",
        "local-1",
        "private-object-access",
    ):
        assert forbidden not in projection


def _pool_module(pool_type: type, timeout_type: type[Exception]) -> ModuleType:
    module = ModuleType("psycopg_pool")
    module.ConnectionPool = pool_type
    module.PoolTimeout = timeout_type
    return module


def test_postgresql_pool_exhaustion_and_initialization_errors_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PoolTimeout(Exception):
        pass

    class ExhaustedPool:
        def __init__(self, **_kwargs) -> None:
            pass

        def getconn(self):
            raise PoolTimeout("private-dsn-value")

        def close(self) -> None:
            pass

    close_database_pools()
    monkeypatch.setitem(sys.modules, "psycopg_pool", _pool_module(ExhaustedPool, PoolTimeout))
    try:
        with pytest.raises(DbError) as exhausted:
            connect_database("postgresql://app:private-password@db.test/exhausted")
        assert exhausted.value.code == "db.pool_exhausted"
        assert exhausted.value.__cause__ is None
        assert "private" not in str(exhausted.value)
    finally:
        close_database_pools()

    class BrokenPool:
        def __init__(self, **_kwargs) -> None:
            raise RuntimeError("postgresql://app:private-password@db.test/broken")

    monkeypatch.setitem(sys.modules, "psycopg_pool", _pool_module(BrokenPool, PoolTimeout))
    with pytest.raises(DbError) as failed:
        connect_database("postgresql://app:private-password@db.test/broken")
    assert failed.value.code == "db.connection_failed"
    assert failed.value.__cause__ is None
    assert "private" not in str(failed.value)


def test_postgresql_row_preserves_sqlite_named_access_contract() -> None:
    row = _DatabaseRow(("Name", "Name", "count"), ("first", "second", 3))
    assert row[0] == "first"
    assert row["Name"] == "first"
    assert row["name"] == "first"
    assert row[-1] == 3
    assert row.keys() == ["Name", "Name", "count"]
    assert dict(row) == {"Name": "first", "count": 3}


def test_postgresql_driver_errors_are_mapped_without_provider_text() -> None:
    class ProviderError(Exception):
        def __init__(self, sqlstate: str) -> None:
            super().__init__("private SQL and credential-bearing provider text")
            self.sqlstate = sqlstate

    class BrokenCursor:
        description = None

        def execute(self, _statement, _parameters=None) -> None:
            raise ProviderError("23505")

        def close(self) -> None:
            pass

    class Info:
        transaction_status = "IDLE"

    class BrokenConnection:
        info = Info()

        def cursor(self) -> BrokenCursor:
            return BrokenCursor()

    class Pool:
        def putconn(self, _connection) -> None:
            pass

    connection = _PostgresConnection(BrokenConnection(), Pool())
    with pytest.raises(sqlite3.IntegrityError) as exc:
        connection.execute("INSERT INTO facts(value) VALUES(?)", ("value",))
    assert str(exc.value) == "Database integrity constraint failed."
    assert exc.value.__cause__ is None
