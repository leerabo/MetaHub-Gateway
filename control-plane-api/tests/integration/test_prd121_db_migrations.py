"""PRD-121 signed desired-state schema, ownership, and rollback guards."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
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
PRD121_TABLES = {
    "edge_snapshot_signing_roots",
    "edge_trust_manifests",
    "edge_trust_manifest_heads",
    "edge_desired_states",
    "edge_desired_state_approvals",
    "edge_snapshot_artifacts",
    "edge_signed_snapshots",
    "edge_desired_state_heads",
    "edge_apply_attempts",
    "edge_acks",
    "edge_lkg_observations",
    "workspace_edge_state_commands",
    "edge_desired_state_events",
    "edge_desired_state_event_outbox",
    "tenant_deletion_manifest_extensions_prd121",
}
PRD121_TENANT_TABLES = {
    "edge_desired_states",
    "edge_desired_state_approvals",
    "edge_snapshot_artifacts",
    "edge_signed_snapshots",
    "edge_desired_state_heads",
    "edge_apply_attempts",
    "edge_acks",
    "edge_lkg_observations",
    "workspace_edge_state_commands",
    "edge_desired_state_events",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture
def migrated_0112(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 112:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd121-0112.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0112"
    for filename in (
        "0113_edge_desired_state_signed_snapshot_lkg.sql",
        "0113_edge_desired_state_signed_snapshot_lkg.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    try:
        yield database, migrations
    finally:
        database.unlink(missing_ok=True)


def test_0112_to_0113_pristine_roundtrip_and_manifest(
    migrated_0112: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0112
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0113"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert PRD121_TABLES <= tables
        assert {
            row[0]
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd121"
            )
        } == PRD121_TENANT_TABLES
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0113"
        ).rolled_back
        == "0113"
    )
    with closing(connect_database(database_url)) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert PRD121_TABLES.isdisjoint(tables)
    _healthy(database_url)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0113"]
    _healthy(database_url)


def test_trust_chain_foreign_key_and_security_facts_are_immutable(
    migrated_0112: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0112
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0113"]

    manifest = json.dumps(
        {
            "domain": "metahub.edge-trust-manifest.v1",
            "generation": 1,
            "current_kid": "edge-ds-test-a",
            "padding": "x" * 128,
        },
        separators=(",", ":"),
    )
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """
            INSERT INTO edge_snapshot_signing_roots(
              kid,algorithm,public_key_b64,public_key_sha256,created_at)
            VALUES('edge-ds-test-a','Ed25519',?,?,?)
            """,
            ("A" * 43, "a" * 64, "2026-08-26T12:00:00.000Z"),
        )
        connection.execute(
            """
            INSERT INTO edge_trust_manifests(
              edge_trust_manifest_id,generation,signer_kid,current_kid,next_kid,
              manifest_rfc8785,manifest_sha256,issued_at,expires_at,created_at)
            VALUES(?,1,'edge-ds-test-a','edge-ds-test-a',NULL,?,?,?, ?,?)
            """,
            (
                "etmf_" + "1" * 32,
                manifest,
                "b" * 64,
                "2026-08-26T12:00:00.000Z",
                "2026-08-27T12:00:00.000Z",
                "2026-08-26T12:00:00.000Z",
            ),
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO edge_trust_manifest_heads(
                  edge_trust_manifest_head_id,singleton,generation,edge_trust_manifest_id,
                  manifest_sha256,revision,updated_at)
                VALUES(?,1,2,?,?,1,?)
                """,
                (
                    "etmh_" + "2" * 32,
                    "etmf_" + "1" * 32,
                    "b" * 64,
                    "2026-08-26T12:00:01.000Z",
                ),
            )
        connection.rollback()

        connection.execute(
            """
            INSERT INTO edge_trust_manifest_heads(
              edge_trust_manifest_head_id,singleton,generation,edge_trust_manifest_id,
              manifest_sha256,revision,updated_at)
            VALUES(?,1,1,?,?,1,?)
            """,
            (
                "etmh_" + "2" * 32,
                "etmf_" + "1" * 32,
                "b" * 64,
                "2026-08-26T12:00:01.000Z",
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="edge_signing_root_immutable"):
            connection.execute(
                "UPDATE edge_snapshot_signing_roots SET created_at=? WHERE kid=?",
                ("2026-08-26T12:00:02.000Z", "edge-ds-test-a"),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="edge_trust_manifest_immutable"):
            connection.execute(
                "DELETE FROM edge_trust_manifests WHERE generation=1"
            )
        connection.rollback()
    _healthy(database_url)


def test_published_desired_state_blocks_down(
    migrated_0112: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0112
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0113"]
    apply_seed_data(database_url)

    now = "2026-08-26T12:00:00.000Z"
    with closing(connect_database(database_url)) as connection:
        owner = connection.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings "
            "WHERE status='active' ORDER BY workspace_id LIMIT 1"
        ).fetchone()
        account = connection.execute(
            "SELECT account_id FROM global_accounts WHERE status='active' "
            "ORDER BY account_id LIMIT 1"
        ).fetchone()
        assert owner is not None
        assert account is not None
        workspace_id, tenant_id = owner
        account_id = account[0]
        deployment_id = "edp_" + "1" * 32
        snapshot_id = "csn_" + "1" * 32
        desired_state_id = "edst_" + "1" * 32
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,status,
              revision,revocation_generation,agent_version,health_code,last_connected_at,
              certificate_expires_at,created_by_account_id,revoked_by_account_id,
              revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'Guard deployment','cn-east-1','connected','pending',1,1,NULL,
              'not_registered',NULL,NULL,?,NULL,NULL,?,?,NULL)
            """,
            (deployment_id, workspace_id, tenant_id, account_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO config_snapshots(
              config_snapshot_id,workspace_id,tenant_id,generation,manifest_envelope,
              manifest_sha256,source_sha256,kms_key_version,signature_algorithm,
              signature_b64,revocation_watermark,published_at)
            VALUES(?,?,?,1,?,?,?,'test-v1','ed25519',?,1,?)
            """,
            (
                snapshot_id,
                workspace_id,
                tenant_id,
                "m" * 64,
                "a" * 64,
                "b" * 64,
                "c" * 86,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO edge_desired_states(
              edge_desired_state_id,workspace_id,tenant_id,edge_deployment_id,generation,
              base_generation,rollback_of_generation,source_config_snapshot_id,
              source_generation,source_manifest_sha256,source_revocation_watermark,
              change_class,status,requested_by_account_id,approved_by_account_id,revision,
              request_sha256,requested_at,decided_at,published_at,updated_at)
            VALUES(?,?,?,?,1,0,NULL,?,1,?,1,'standard','published',?,NULL,1,?,?,NULL,?,?)
            """,
            (
                desired_state_id,
                workspace_id,
                tenant_id,
                deployment_id,
                snapshot_id,
                "a" * 64,
                account_id,
                "d" * 64,
                now,
                now,
                now,
            ),
        )
        connection.commit()
    _healthy(database_url)

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0113"
        )
    assert blocked.value.code == "db.migration_failed"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0113"
        assert connection.execute("SELECT COUNT(*) FROM edge_desired_states").fetchone()[0] == 1
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database = tmp_path / "fresh.sqlite3"
    database_url = _url(database)
    try:
        assert apply_migrations(database_url).applied[-1] == "0156"
        _healthy(database_url)
    finally:
        database.unlink(missing_ok=True)
