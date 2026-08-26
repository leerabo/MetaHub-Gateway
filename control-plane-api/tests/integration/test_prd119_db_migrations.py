"""PRD-119 migration, signature, immutability, and rollback guards."""

from __future__ import annotations

import base64
import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from metahub_control.provider_catalog.seed import SEED_MANIFEST
from metahub_control.provider_catalog.service import _verified_manifest
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.db import (
    apply_migrations,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD119_TABLES = {
    "workspace_provider_catalog_permission_extensions",
    "workspace_provider_catalog_role_permissions",
    "platform_provider_catalog_permission_extensions",
    "platform_provider_catalog_role_permissions",
    "provider_catalog_trusted_keys",
    "provider_catalog_versions",
    "provider_catalog_head",
    "provider_catalog_entries",
    "provider_capabilities",
    "regional_endpoint_templates",
    "compatibility_profiles",
    "provider_catalog_instances",
    "provider_preflight_runs",
    "workspace_provider_catalog_commands",
    "platform_provider_catalog_commands",
    "provider_catalog_events",
    "provider_catalog_event_outbox",
    "tenant_deletion_manifest_extensions_prd119",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def migrated_0110(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd119-migrated-0110")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 110:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd119-0110.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0110"
    for filename in (
        "0111_official_provider_catalog_regional_compliance.sql",
        "0111_official_provider_catalog_regional_compliance.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0110_to_0111_pristine_roundtrip(
    migrated_0110: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0110
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0111"]
    with connect_database(database_url) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert PRD119_TABLES <= tables
        assert tuple(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM provider_catalog_entries),
                  (SELECT COUNT(*) FROM provider_capabilities),
                  (SELECT COUNT(*) FROM regional_endpoint_templates),
                  (SELECT COUNT(*) FROM compatibility_profiles)
                """
            ).fetchone()
        ) == (10, 12, 7, 10)
        manifest, version = _verified_manifest(connection, "2026.08.26.1")
        assert manifest.model_dump(mode="json") == SEED_MANIFEST
        raw = canonical_json_bytes(SEED_MANIFEST)
        assert hashlib.sha256(raw).hexdigest() == version["manifest_sha256"]
        key = connection.execute(
            "SELECT public_key_b64 FROM provider_catalog_trusted_keys WHERE key_id=?",
            (version["key_id"],),
        ).fetchone()
        assert key is not None
        public = base64.urlsafe_b64decode(key["public_key_b64"] + "=")
        signature = base64.urlsafe_b64decode(version["signature_b64"] + "==")
        Ed25519PublicKey.from_public_bytes(public).verify(signature, raw)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0111"
        ).rolled_back
        == "0111"
    )
    with connect_database(database_url) as connection:
        remaining = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert not PRD119_TABLES & remaining
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0111"]


def test_catalog_rows_are_immutable_and_down_guard_preserves_schema(
    migrated_0110: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0110
    database = tmp_path / "guards.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0111"]
    with connect_database(database_url) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE provider_catalog_entries SET display_name='tampered' "
                "WHERE version='2026.08.26.1' AND entry_code='deepseek'"
            )
        connection.rollback()
        connection.execute(
            """
            INSERT INTO provider_catalog_trusted_keys(
              key_id,algorithm,public_key_b64,status,key_purpose,created_at)
            VALUES('deployment-active-v1','ed25519',?,'active','provider_catalog',?)
            """,
            ("A" * 43, "2026-08-26T01:00:00.000Z"),
        )
        connection.commit()
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0111"
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0111"
        assert PRD119_TABLES <= {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }


def test_tenant_manifest_covers_every_prd119_owned_table(
    migrated_0110: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0110
    database = tmp_path / "manifest.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0111"]
    with connect_database(database_url) as connection:
        rows = connection.execute(
            """
            SELECT extension.source_table,manifest.disposition,manifest.retention_class
            FROM tenant_deletion_manifest_extensions_prd119 extension
            JOIN tenant_deletion_table_manifest manifest
              ON manifest.source_table=extension.source_table
            ORDER BY extension.source_table
            """
        ).fetchall()
        assert {row["source_table"] for row in rows} == {
            "provider_catalog_instances",
            "provider_preflight_runs",
            "workspace_provider_catalog_commands",
            "provider_catalog_events",
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
