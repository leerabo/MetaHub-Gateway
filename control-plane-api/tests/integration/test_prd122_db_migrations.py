"""PRD-122 telemetry schema, immutable facts, ownership, and rollback guards."""

from __future__ import annotations

import hashlib
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
PRD122_TABLES = {
    "workspace_edge_telemetry_permission_extensions",
    "workspace_edge_telemetry_role_permissions",
    "edge_telemetry_policies",
    "edge_telemetry_policy_heads",
    "workspace_edge_telemetry_commands",
    "edge_telemetry_batches",
    "edge_usage_sync_cursors",
    "edge_telemetry_receipts",
    "edge_usage_reconciliations",
    "edge_failure_summaries",
    "edge_fleet_telemetry_projections",
    "edge_telemetry_events",
    "edge_telemetry_event_outbox",
    "tenant_deletion_manifest_extensions_prd122",
}
PRD122_TENANT_TABLES = {
    "edge_telemetry_policies",
    "edge_telemetry_policy_heads",
    "workspace_edge_telemetry_commands",
    "edge_telemetry_batches",
    "edge_usage_sync_cursors",
    "edge_telemetry_receipts",
    "edge_usage_reconciliations",
    "edge_failure_summaries",
    "edge_telemetry_events",
}
NOW = "2026-08-27T01:00:00.000Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0113(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd122-0113-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 113:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0113.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0113"
    for filename in (
        "0114_edge_telemetry_usage_failure_sync.sql",
        "0114_edge_telemetry_usage_failure_sync.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(_url(database))
    return database, migrations


@pytest.fixture
def migrated_0113(base_0113: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0113
    database = tmp_path / "prd122-0113.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def _owner(connection: sqlite3.Connection) -> tuple[str, str, str]:
    row = connection.execute(
        """
        SELECT binding.workspace_id,binding.tenant_id,membership.account_id
        FROM workspace_tenant_bindings binding
        JOIN workspace_memberships membership
          ON membership.workspace_id=binding.workspace_id
         AND membership.tenant_id=binding.tenant_id
        WHERE binding.status='active' AND membership.status='active'
          AND membership.role_code='owner'
        ORDER BY binding.workspace_id LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1]), str(row[2])


def _insert_deployment_policy(
    connection: sqlite3.Connection,
    *,
    suffix: str = "1",
    create_head: bool = True,
) -> tuple[str, str, str, str]:
    workspace_id, tenant_id, account_id = _owner(connection)
    deployment_id = "edp_" + suffix * 32
    policy_id = "etp_" + suffix * 32
    connection.execute(
        """
        INSERT INTO edge_deployments(
          edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,status,
          revision,revocation_generation,agent_version,health_code,last_connected_at,
          certificate_expires_at,created_by_account_id,revoked_by_account_id,
          revoked_reason_code,created_at,updated_at,revoked_at)
        VALUES(?,?,?,?, 'cn-east-1','connected','pending',
          1,1,NULL,'not_registered',NULL,NULL,?,NULL,NULL,?,?,NULL)
        """,
        (
            deployment_id,
            workspace_id,
            tenant_id,
            f"PRD-122 migration guard {suffix}",
            account_id,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO edge_telemetry_policies(
          edge_telemetry_policy_id,workspace_id,tenant_id,edge_deployment_id,revision,
          telemetry_level,source_code,actor_account_id,idempotency_sha256,
          request_sha256,created_at)
        VALUES(?,?,?,?,1,'minimal','system_default',?,NULL,?,?)
        """,
        (
            policy_id,
            workspace_id,
            tenant_id,
            deployment_id,
            account_id,
            hashlib.sha256(f"policy-{suffix}".encode()).hexdigest(),
            NOW,
        ),
    )
    if create_head:
        connection.execute(
            """
            INSERT INTO edge_telemetry_policy_heads(
              edge_deployment_id,workspace_id,tenant_id,edge_telemetry_policy_id,
              revision,telemetry_level,updated_at)
            VALUES(?,?,?,?,1,'minimal',?)
            """,
            (deployment_id, workspace_id, tenant_id, policy_id, NOW),
        )
    connection.commit()
    return deployment_id, policy_id, workspace_id, tenant_id


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _insert_device_chain(
    connection: sqlite3.Connection,
    *,
    deployment_id: str,
    workspace_id: str,
    tenant_id: str,
    suffix: str,
) -> tuple[str, str, str]:
    identity_id = "edi_" + suffix * 32
    certificate_id = "edc_" + suffix * 32
    session_id = "eds_" + suffix * 32
    public_key = _digest(f"public-key-{suffix}")
    connection.execute(
        """
        INSERT INTO edge_device_identities(
          edge_device_identity_id,workspace_id,tenant_id,edge_deployment_id,
          public_key_sha256,hardware_proof_sha256,capability_codes_json,
          agent_version,generation,status,created_at,updated_at,revoked_at)
        VALUES(?,?,?,?,?,NULL,'["telemetry"]','1.0.0',1,'active',?,?,NULL)
        """,
        (identity_id, workspace_id, tenant_id, deployment_id, public_key, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO edge_device_certificates(
          edge_device_certificate_id,workspace_id,tenant_id,edge_deployment_id,
          edge_device_identity_id,prior_certificate_id,serial_hex,certificate_sha256,
          public_key_sha256,csr_sha256,certificate_pem,issuer_key_id,generation,status,
          not_before,not_after,drain_until,created_at,updated_at,revoked_at)
        VALUES(?,?,?,?,?,NULL,?,?,?,?,?,'edge-device-ca-v1',1,'active',
          ?,'2036-08-27T01:00:00.000Z',NULL,?,?,NULL)
        """,
        (
            certificate_id,
            workspace_id,
            tenant_id,
            deployment_id,
            identity_id,
            suffix * 16,
            _digest(f"certificate-{suffix}"),
            public_key,
            _digest(f"csr-{suffix}"),
            "-----BEGIN CERTIFICATE-----\n"
            + "A" * 256
            + "\n-----END CERTIFICATE-----",
            NOW,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO edge_sessions(
          edge_session_id,workspace_id,tenant_id,edge_deployment_id,
          edge_device_identity_id,edge_device_certificate_id,session_token_hmac,
          peer_binding_sha256,handshake_nonce_hmac,identity_generation,
          revocation_generation,status,expires_at,revoked_reason_code,created_at,revoked_at)
        VALUES(?,?,?,?,?,?,?,?,?,1,1,'active','2036-08-27T01:00:00.000Z',NULL,?,NULL)
        """,
        (
            session_id,
            workspace_id,
            tenant_id,
            deployment_id,
            identity_id,
            certificate_id,
            _digest(f"session-token-{suffix}"),
            _digest(f"peer-{suffix}"),
            _digest(f"nonce-{suffix}"),
            NOW,
        ),
    )
    return identity_id, certificate_id, session_id


def _insert_batch(
    connection: sqlite3.Connection,
    *,
    suffix: str,
    deployment_id: str,
    workspace_id: str,
    tenant_id: str,
    identity_id: str,
    certificate_id: str,
    session_id: str,
    policy_id: str,
    policy_revision: int = 1,
) -> str:
    batch_id = "etb_" + suffix * 32
    payload = '{"kind":"health","padding":"' + "x" * 64 + '"}'
    connection.execute(
        """
        INSERT INTO edge_telemetry_batches(
          edge_telemetry_batch_id,workspace_id,tenant_id,edge_deployment_id,
          edge_device_identity_id,edge_device_certificate_id,edge_session_id,
          stream_code,epoch_id,previous_epoch_id,sequence,edge_telemetry_policy_id,
          policy_revision,schema_version,payload_rfc8785,payload_sha256,record_count,
          first_bucket_at,last_bucket_at,sent_at,received_at)
        VALUES(?,?,?,?,?,?,?,'health',?,NULL,1,?,?,'1',?,?,1,
          '2026-08-27T00:58:00.000Z','2026-08-27T00:59:00.000Z',?,?)
        """,
        (
            batch_id,
            workspace_id,
            tenant_id,
            deployment_id,
            identity_id,
            certificate_id,
            session_id,
            "ete_" + suffix * 32,
            policy_id,
            policy_revision,
            payload,
            _digest(payload),
            NOW,
            NOW,
        ),
    )
    return batch_id


def test_0113_to_0114_pristine_roundtrip_and_manifest(
    migrated_0113: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0113
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0114"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert PRD122_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd122"
            )
        } == PRD122_TENANT_TABLES
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_edge_telemetry_permission_extensions"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_edge_telemetry_role_permissions"
        ).fetchone()[0] == 9
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_telemetry_policies"
        ).fetchone()[0] == connection.execute(
            "SELECT COUNT(*) FROM edge_deployments"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_telemetry_policies "
            "WHERE revision!=1 OR telemetry_level!='minimal' "
            "OR source_code!='system_default'"
        ).fetchone()[0] == 0
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0114"
        ).rolled_back
        == "0114"
    )
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert PRD122_TABLES.isdisjoint(tables)
    _healthy(database_url)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0114"]
    _healthy(database_url)


def test_policy_owner_and_permission_facts_are_immutable(
    migrated_0113: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0113
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0114"]
    apply_seed_data(database_url)

    with closing(connect_database(database_url)) as connection:
        deployment_id, policy_id, workspace_id, _tenant_id = _insert_deployment_policy(
            connection
        )
        with pytest.raises(sqlite3.IntegrityError, match="telemetry_policy_immutable"):
            connection.execute(
                "UPDATE edge_telemetry_policies SET telemetry_level='billing' "
                "WHERE edge_telemetry_policy_id=?",
                (policy_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="telemetry_policy_head_invalid"):
            connection.execute(
                "UPDATE edge_telemetry_policy_heads SET updated_at=? "
                "WHERE edge_deployment_id=?",
                ("2026-08-27T01:00:01.000Z", deployment_id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO edge_telemetry_policies(
                  edge_telemetry_policy_id,workspace_id,tenant_id,edge_deployment_id,
                  revision,telemetry_level,source_code,actor_account_id,
                  idempotency_sha256,request_sha256,created_at)
                SELECT ?,?, 'ten_missing_owner', ?,2,'billing','workspace_policy',
                  created_by_account_id,?,?,?
                FROM edge_deployments WHERE edge_deployment_id=?
                """,
                (
                    "etp_" + "2" * 32,
                    workspace_id,
                    deployment_id,
                    "b" * 64,
                    "c" * 64,
                    "2026-08-27T01:00:01.000Z",
                    deployment_id,
                ),
            )
        connection.rollback()
        with pytest.raises(
            sqlite3.IntegrityError, match="telemetry_permission_immutable"
        ):
            connection.execute(
                "DELETE FROM workspace_edge_telemetry_role_permissions "
                "WHERE role_code='finance_admin'"
            )
        connection.rollback()
    _healthy(database_url)


def test_telemetry_relations_reject_cross_deployment_references(
    migrated_0113: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0113
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0114"]
    apply_seed_data(database_url)

    with closing(connect_database(database_url)) as connection:
        deployment_1, policy_1, workspace_id, tenant_id = _insert_deployment_policy(
            connection, suffix="1"
        )
        deployment_2, policy_2, _, _ = _insert_deployment_policy(
            connection, suffix="2"
        )
        deployment_3, policy_3, _, _ = _insert_deployment_policy(
            connection, suffix="3", create_head=False
        )
        identity_1, certificate_1, session_1 = _insert_device_chain(
            connection,
            deployment_id=deployment_1,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            suffix="1",
        )
        identity_2, certificate_2, session_2 = _insert_device_chain(
            connection,
            deployment_id=deployment_2,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            suffix="2",
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE edge_telemetry_policy_heads
                SET revision=2,updated_at='2026-08-27T01:00:01.000Z'
                WHERE edge_deployment_id=?
                """,
                (deployment_1,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO edge_telemetry_policy_heads(
                  edge_deployment_id,workspace_id,tenant_id,edge_telemetry_policy_id,
                  revision,telemetry_level,updated_at)
                VALUES(?,?,?,?,1,'billing',?)
                """,
                (deployment_3, workspace_id, tenant_id, policy_3, NOW),
            )
        connection.rollback()

        account_id = _owner(connection)[2]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO workspace_edge_telemetry_commands(
                  workspace_edge_telemetry_command_id,workspace_id,tenant_id,
                  edge_deployment_id,actor_account_id,action_code,idempotency_sha256,
                  request_sha256,result_policy_id,created_at)
                VALUES(?,?,?,?,?,'telemetry_policy_patch',?,?,?,?)
                """,
                (
                    "etc_" + "9" * 32,
                    workspace_id,
                    tenant_id,
                    deployment_1,
                    account_id,
                    _digest("cross-policy-idempotency"),
                    _digest("cross-policy-request"),
                    policy_2,
                    NOW,
                ),
            )
        connection.rollback()

        invalid_batches = (
            ("a", identity_2, certificate_1, session_1, policy_1, 1),
            ("b", identity_1, certificate_2, session_1, policy_1, 1),
            ("c", identity_1, certificate_1, session_2, policy_1, 1),
            ("d", identity_1, certificate_1, session_1, policy_1, 2),
        )
        for suffix, identity_id, certificate_id, session_id, policy_id, revision in (
            invalid_batches
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_batch(
                    connection,
                    suffix=suffix,
                    deployment_id=deployment_1,
                    workspace_id=workspace_id,
                    tenant_id=tenant_id,
                    identity_id=identity_id,
                    certificate_id=certificate_id,
                    session_id=session_id,
                    policy_id=policy_id,
                    policy_revision=revision,
                )
            connection.rollback()

        batch_id = _insert_batch(
            connection,
            suffix="f",
            deployment_id=deployment_1,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            identity_id=identity_1,
            certificate_id=certificate_1,
            session_id=session_1,
            policy_id=policy_1,
        )
        cursor_1 = "euc_" + "1" * 32
        cursor_2 = "euc_" + "2" * 32
        for cursor_id, deployment_id, epoch_suffix in (
            (cursor_1, deployment_1, "1"),
            (cursor_2, deployment_2, "2"),
        ):
            connection.execute(
                """
                INSERT INTO edge_usage_sync_cursors(
                  edge_usage_sync_cursor_id,workspace_id,tenant_id,edge_deployment_id,
                  stream_code,epoch_id,previous_epoch_id,status,contiguous_sequence,
                  highest_seen_sequence,revision,opened_at,closed_at,updated_at)
                VALUES(?,?,?,?,'health',?,NULL,'active',1,1,1,?,NULL,?)
                """,
                (
                    cursor_id,
                    workspace_id,
                    tenant_id,
                    deployment_id,
                    "ete_" + epoch_suffix * 32,
                    NOW,
                    NOW,
                ),
            )
        connection.commit()

        receipt_values = (
            "etr_" + "9" * 32,
            workspace_id,
            tenant_id,
            deployment_1,
            batch_id,
            cursor_2,
            _digest("cross-cursor-receipt"),
            NOW,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO edge_telemetry_receipts(
                  edge_telemetry_receipt_id,workspace_id,tenant_id,edge_deployment_id,
                  edge_telemetry_batch_id,edge_usage_sync_cursor_id,contiguous_sequence,
                  expected_sequence,highest_seen_sequence,receipt_sha256,received_at)
                VALUES(?,?,?,?,?,?,1,2,1,?,?)
                """,
                receipt_values,
            )
        connection.rollback()
        connection.execute(
            """
            INSERT INTO edge_telemetry_receipts(
              edge_telemetry_receipt_id,workspace_id,tenant_id,edge_deployment_id,
              edge_telemetry_batch_id,edge_usage_sync_cursor_id,contiguous_sequence,
              expected_sequence,highest_seen_sequence,receipt_sha256,received_at)
            VALUES(?,?,?,?,?,?,1,2,1,?,?)
            """,
            (
                "etr_" + "8" * 32,
                workspace_id,
                tenant_id,
                deployment_1,
                batch_id,
                cursor_1,
                _digest("owned-cursor-receipt"),
                NOW,
            ),
        )
        connection.commit()
    _healthy(database_url)


def test_telemetry_command_blocks_down(migrated_0113: tuple[Path, Path]) -> None:
    database, migrations = migrated_0113
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0114"]
    apply_seed_data(database_url)

    with closing(connect_database(database_url)) as connection:
        deployment_id, policy_id, workspace_id, tenant_id = _insert_deployment_policy(
            connection
        )
        account_id = _owner(connection)[2]
        connection.execute(
            """
            INSERT INTO workspace_edge_telemetry_commands(
              workspace_edge_telemetry_command_id,workspace_id,tenant_id,
              edge_deployment_id,actor_account_id,action_code,idempotency_sha256,
              request_sha256,result_policy_id,created_at)
            VALUES(?,?,?,?,?,'telemetry_policy_patch',?,?,?,?)
            """,
            (
                "etc_" + "1" * 32,
                workspace_id,
                tenant_id,
                deployment_id,
                account_id,
                "d" * 64,
                "e" * 64,
                policy_id,
                NOW,
            ),
        )
        connection.commit()
    _healthy(database_url)

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0114"
        )
    assert blocked.value.code == "db.migration_failed"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0114"
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_edge_telemetry_commands"
        ).fetchone()[0] == 1
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database = tmp_path / "fresh.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0115"
    _healthy(database_url)
