"""PRD-117 governance migration, ownership, round-trip, and durable-fact guards."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD117_TABLES = {
    "workspace_governance_permission_extensions",
    "workspace_governance_role_permissions",
    "platform_data_governance_permission_extensions",
    "platform_data_governance_role_permissions",
    "tenant_deletion_manifest_extensions",
    "data_classifications",
    "data_governance_fences",
    "residency_policies",
    "retention_policies",
    "legal_holds",
    "deletion_schedules",
    "residency_evidence",
    "data_governance_commands",
    "data_governance_events",
    "data_governance_event_outbox",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with connect_database(database_url) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        _assert_deletion_manifest_current(connection)


def _assert_deletion_manifest_current(connection) -> None:
    manifests = connection.execute(
        "SELECT source_table,table_sql_sha256,column_signature_sha256,trigger_set_sha256 "
        "FROM tenant_deletion_table_manifest ORDER BY source_table"
    ).fetchall()
    assert manifests
    actual_trigger_manifest: dict[str, tuple[str, str, str]] = {}
    for manifest in manifests:
        table = manifest["source_table"]
        schema = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        assert schema is not None
        table_hash = hashlib.sha256((schema["sql"] or "").encode()).hexdigest()
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        column_text = "\x1e".join(
            "\x1f".join(
                "" if value is None else str(value)
                for value in (
                    column["cid"],
                    column["name"],
                    column["type"],
                    column["notnull"],
                    column["dflt_value"],
                    column["pk"],
                )
            )
            for column in columns
        )
        trigger_rows = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=? "
            "ORDER BY name",
            (table,),
        ).fetchall()
        trigger_text = "\x1e".join(
            f"{trigger['name']}\x1f{hashlib.sha256((trigger['sql'] or '').encode()).hexdigest()}"
            for trigger in trigger_rows
        )
        assert (
            table_hash,
            hashlib.sha256(column_text.encode()).hexdigest(),
            hashlib.sha256(trigger_text.encode()).hexdigest(),
        ) == (
            manifest["table_sql_sha256"],
            manifest["column_signature_sha256"],
            manifest["trigger_set_sha256"],
        )
        actual_trigger_manifest.update(
            {
                trigger["name"]: (
                    table,
                    trigger["sql"],
                    hashlib.sha256((trigger["sql"] or "").encode()).hexdigest(),
                )
                for trigger in trigger_rows
            }
        )
    stored_trigger_manifest = {
        row["trigger_name"]: (
            row["source_table"],
            row["trigger_sql"],
            row["trigger_sha256"],
        )
        for row in connection.execute(
            "SELECT trigger_name,source_table,trigger_sql,trigger_sha256 "
            "FROM tenant_deletion_trigger_manifest ORDER BY trigger_name"
        )
    }
    assert stored_trigger_manifest == actual_trigger_manifest
    assert {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' "
            "AND name LIKE 'trg_tenant_deletion_%_manifest_%_prd089'"
        )
    } == {
        "trg_tenant_deletion_table_manifest_update_prd089",
        "trg_tenant_deletion_table_manifest_delete_prd089",
        "trg_tenant_deletion_trigger_manifest_update_prd089",
        "trg_tenant_deletion_trigger_manifest_delete_prd089",
    }
    extension_exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' "
        "AND name='tenant_deletion_manifest_extensions'"
    ).fetchone()
    if extension_exists:
        assert connection.execute(
            "SELECT tenant_key_column FROM tenant_deletion_manifest_extensions "
            "WHERE source_table='account_identity_links'"
        ).fetchone()[0] == "legacy_tenant_id"
        registered = {manifest["source_table"] for manifest in manifests}
        for table_row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ):
            table = table_row["name"]
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            if any(column["name"] == "tenant_id" for column in columns):
                assert table in registered
        assert {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' "
                "AND name LIKE 'trg_tenant_deletion_manifest_extensions_%_prd117'"
            )
        } == {
            "trg_tenant_deletion_manifest_extensions_insert_prd117",
            "trg_tenant_deletion_manifest_extensions_update_prd117",
            "trg_tenant_deletion_manifest_extensions_delete_prd117",
        }


@pytest.fixture(scope="module")
def migrated_0108(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd117-migrated-0108")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 108:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd117-0108.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0108"
    apply_seed_data(_url(database))
    for filename in (
        "0109_data_residency_retention_legal_hold.sql",
        "0109_data_residency_retention_legal_hold.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0108_to_0109_pristine_roundtrip(
    migrated_0108: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0108
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0109"]
    with connect_database(database_url) as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD117_TABLES <= tables
        workspace_count = connection.execute(
            "SELECT COUNT(*) FROM workspace_regions WHERE status='active'"
        ).fetchone()[0]
        assert connection.execute("SELECT COUNT(*) FROM data_classifications").fetchone()[0] == (
            workspace_count * 6
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM residency_policies WHERE status='active'"
        ).fetchone()[0] == workspace_count
        assert connection.execute(
            "SELECT COUNT(*) FROM retention_policies WHERE status='active'"
        ).fetchone()[0] == workspace_count * 6
        assert {
            tuple(row)
            for row in connection.execute(
                "SELECT role_code,permission_code FROM workspace_governance_role_permissions"
            )
        } >= {
            ("security_admin", "data_governance.manage"),
            ("legal_admin", "legal_hold.manage"),
            ("auditor", "data_governance.read"),
        }
        assert {
            tuple(row)
            for row in connection.execute(
                "SELECT role_code,permission_code FROM platform_data_governance_role_permissions"
            )
        } == {
            ("platform_owner", "platform.data_residency.read"),
            ("platform_compliance", "platform.data_residency.read"),
        }
        assert connection.execute(
            "SELECT status FROM platform_job_types WHERE type_code='data.retention.delete'"
        ).fetchone()[0] == "active"
        assert "governance_generation" in {
            row["name"]
            for row in connection.execute("PRAGMA table_info(workspace_gateway_placements)")
        }
        assert {"governance_generation", "residency_group"} <= {
            row["name"] for row in connection.execute("PRAGMA table_info(backup_sets)")
        }
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0109"
        ).rolled_back
        == "0109"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0109"]
    _healthy(database_url)


def test_owner_constraints_and_durable_evidence_block_down(
    migrated_0108: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0108
    database = tmp_path / "down-guard.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0109"]

    with connect_database(database_url) as connection:
        policy = connection.execute(
            "SELECT * FROM residency_policies WHERE status='active' ORDER BY workspace_id LIMIT 1"
        ).fetchone()
        assert policy is not None
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO residency_evidence(
                     residency_evidence_id,workspace_id,tenant_id,residency_policy_id,
                     policy_generation,component_code,region_code,storage_class,outcome,
                     violation_code,observed_watermark,manifest_sha256,observed_at,created_at)
                   VALUES(?,?,?,?,1,'gateway','cn-north-1','routing','compliant',NULL,1,?,?,?)""",
                (
                    "rse_" + "1" * 32,
                    policy["workspace_id"],
                    "ten_" + "f" * 32,
                    policy["residency_policy_id"],
                    "1" * 64,
                    "2026-08-26T00:00:00Z",
                    "2026-08-26T00:00:00Z",
                ),
            )
        connection.rollback()
        connection.execute(
            """INSERT INTO residency_evidence(
                 residency_evidence_id,workspace_id,tenant_id,residency_policy_id,
                 policy_generation,component_code,region_code,storage_class,outcome,
                 violation_code,observed_watermark,manifest_sha256,observed_at,created_at)
               VALUES(?,?,?,?,1,'gateway','cn-north-1','routing','compliant',NULL,1,?,?,?)""",
            (
                "rse_" + "2" * 32,
                policy["workspace_id"],
                policy["tenant_id"],
                policy["residency_policy_id"],
                "2" * 64,
                "2026-08-26T00:00:00Z",
                "2026-08-26T00:00:00Z",
            ),
        )
        connection.commit()

    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0109"
        )
    with connect_database(database_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM residency_evidence").fetchone()[0] == 1
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0109"
    _healthy(database_url)


def test_fresh_schema_installs_prd117_head(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 109:
            shutil.copy2(source, migrations / source.name)
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0109"
    _healthy(database_url)
