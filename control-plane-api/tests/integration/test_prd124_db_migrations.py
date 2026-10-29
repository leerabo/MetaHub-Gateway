"""PRD-124 installer release, lifecycle schema, permissions, and rollback guards."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from metahub_control.workspace_access.edge_permissions import (
    load_verified_platform_edge_permissions,
    load_verified_workspace_edge_permissions,
)
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.errors import ApiError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD124_TABLES = {
    "workspace_edge_installer_permission_extensions",
    "workspace_edge_installer_role_permissions",
    "platform_edge_installer_permission_extensions",
    "platform_edge_installer_role_permissions",
    "installer_trusted_release_keys",
    "installer_releases",
    "installer_release_artifacts",
    "installer_profiles",
    "platform_installer_release_commands",
    "edge_preflight_reports",
    "edge_installations",
    "edge_install_audits",
    "edge_install_outbox",
    "edge_install_commands",
    "tenant_deletion_manifest_extensions_prd124",
}
TENANT_TABLES = {
    "edge_preflight_reports",
    "edge_installations",
    "edge_install_audits",
    "edge_install_outbox",
    "edge_install_commands",
}
FIXED_RELEASE_ID = "irl_12400000000000000000000000000000"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0115(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd124-0115-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 115:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0115.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0115"
    for filename in (
        "0116_edge_installer_preflight_onboarding.sql",
        "0116_edge_installer_preflight_onboarding.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(_url(database))
    return database, migrations


@pytest.fixture
def migrated_0115(base_0115: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0115
    database = tmp_path / "prd124-0115.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0115_to_0116_pristine_roundtrip_and_seeded_catalog(
    migrated_0115: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0115
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0116"]
    apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD124_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd124"
            ).fetchall()
        } == TENANT_TABLES
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_edge_installer_permission_extensions"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_edge_installer_permission_extensions"
        ).fetchone()[0] == 3
        release = connection.execute(
            "SELECT status,revision FROM installer_releases WHERE installer_release_id=?",
            (FIXED_RELEASE_ID,),
        ).fetchone()
        assert release is not None and tuple(release) == ("released", 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM installer_release_artifacts"
        ).fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM installer_profiles").fetchone()[0] == 2
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0116"
        ).rolled_back
        == "0116"
    )
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD124_TABLES.isdisjoint(tables)
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0116"]
    _healthy(database_url)


def test_installer_facts_are_immutable_and_permission_drift_fails_closed(
    migrated_0115: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0115
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0116"]
    with closing(connect_database(database_url)) as connection:
        assert {
            "edge.installer.read",
            "edge.installation.manage",
        } <= set(
            load_verified_workspace_edge_permissions(
                connection,
                role_code="owner",
                unavailable_error_code="edge.unavailable",
            )
        )
        assert {
            "platform.edge_installer.read",
            "platform.edge_installer.manage",
            "platform.edge_installer.approve",
        } <= set(
            load_verified_platform_edge_permissions(
                connection,
                roles=("release_manager",),
                unavailable_error_code="platform.session_unavailable",
            )
        )

        with pytest.raises(sqlite3.IntegrityError, match="permission_immutable"):
            connection.execute(
                "DELETE FROM platform_edge_installer_role_permissions "
                "WHERE role_code='release_manager' "
                "AND permission_code='platform.edge_installer.manage'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="release_transition_invalid"):
            connection.execute(
                "UPDATE installer_releases SET status='approval_pending',revision=2 "
                "WHERE installer_release_id=?",
                (FIXED_RELEASE_ID,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="artifact_immutable"):
            connection.execute(
                """
                INSERT INTO installer_release_artifacts(
                  installer_artifact_id,installer_release_id,kind,filename,media_type,
                  content_sha256,sbom_sha256,size_bytes,executable,download_path,created_at)
                VALUES('ira_ffffffffffffffffffffffffffffffff',?,'cli','other.py',
                  'text/x-python',?,?,1,0,
                  '/edge/installers/irl_12400000000000000000000000000000/artifacts/ira_ffffffffffffffffffffffffffffffff',
                  '2026-08-27T00:00:00Z')
                """,
                (FIXED_RELEASE_ID, "a" * 64, "b" * 64),
            )
        connection.rollback()

        connection.execute("BEGIN")
        connection.execute("DROP TRIGGER trg_platform_edge_installer_roles_delete_prd124")
        connection.execute(
            "DELETE FROM platform_edge_installer_role_permissions "
            "WHERE role_code='release_manager' "
            "AND permission_code='platform.edge_installer.manage'"
        )
        with pytest.raises(ApiError) as drift:
            load_verified_platform_edge_permissions(
                connection,
                roles=("release_manager",),
                unavailable_error_code="platform.session_unavailable",
            )
        assert drift.value.code == "platform.session_unavailable"
        connection.rollback()
    _healthy(database_url)


def test_new_release_fact_blocks_0116_rollback(
    migrated_0115: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0115
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0116"]
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """
            INSERT INTO installer_releases(
              installer_release_id,version,channel,status,manifest_sha256,sbom_sha256,
              signature_algorithm,signature_key_id,signature_value,signature_purpose,
              supported_os_json,supported_arch_json,schema_min,schema_max,
              agent_min_version,agent_max_version,control_min_version,control_max_version,
              n_minus_one,revision,submitted_by_account_id,approved_by_account_id,
              submitted_at,approved_at,withdrawn_at)
            VALUES('irl_ffffffffffffffffffffffffffffffff','1.0.1','stable',
              'approval_pending',?,?,'Ed25519','metahub-installer-release-v1',?,
              'metahub.edge-installer.release/v1','["linux"]','["aarch64","x86_64"]',
              115,116,'1.0.0','1.1.99','1.0.0','1.1.99',1,1,
              'prd124-migration-test',NULL,'2026-08-27T00:00:00Z',NULL,NULL)
            """,
            ("c" * 64, "d" * 64, "A" * 86),
        )
        connection.commit()

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0116"
        )
    assert blocked.value.code == "db.migration_failed"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0116"
        assert connection.execute("SELECT COUNT(*) FROM installer_releases").fetchone()[0] == 2
    _healthy(database_url)
