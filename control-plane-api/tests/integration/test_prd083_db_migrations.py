"""PRD-083 migration fidelity, drain, policy graph, and rollback guards."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import test_prd082_db_migrations as prd082
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
NOW = "2026-08-16T00:00:00Z"
NOW_1 = "2026-08-16T00:00:01Z"
NOW_2 = "2026-08-16T00:00:02Z"
NOW_3 = "2026-08-16T00:00:03Z"
FUTURE = "2026-08-16T01:00:00Z"
DEFAULT_FINGERPRINT = "1b0e1d1610dc98e12e09661d2ee481e0d8ee8961faec8a691f16fe6cc603c4f6"
NETWORK_TABLES = (
    "mcp_network_policies",
    "mcp_network_policy_revisions",
    "mcp_invocation_network_bindings",
    "mcp_network_decisions",
    "mcp_network_call_intents",
    "mcp_network_circuits",
    "mcp_network_events",
    "mcp_network_event_outbox",
)
STATE_TABLES = (
    "mcp_server_operations",
    "mcp_server_sync_jobs",
    "mcp_invocations",
    "mcp_invocation_attempts",
)


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd083_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("prd083-migration-templates")
    migrations_0075 = _migration_subset(root / "migrations-0075", 75)
    migrations_0076 = _migration_subset(root / "migrations-0076", 76)
    database = root / "seeded-0075.sqlite3"
    database_url = _url(database)
    result = apply_migrations(database_url, migrations_dir=migrations_0075)
    assert result.applied == [f"{version:04d}" for version in range(1, 76)]
    apply_seed_data(database_url)
    return {
        "database_0075": database,
        "migrations_0075": migrations_0075,
        "migrations_0076": migrations_0076,
    }


def _clone_0075(templates: dict[str, Path], destination: Path) -> str:
    shutil.copy2(templates["database_0075"], destination)
    return _url(destination)


def _head(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return str(row[0])


def _healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _assert_half_open_reclaim_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        """
        SELECT sql FROM sqlite_schema
        WHERE type='trigger' AND name='trg_mcp_network_circuits_transition_prd083'
        """
    ).fetchone()
    assert row is not None
    trigger_sql = str(row["sql"])
    assert "unixepoch(NEW.updated_at)<unixepoch(OLD.half_open_lease_expires_at)" in trigger_sql
    assert "unixepoch(NEW.updated_at)>=unixepoch(OLD.half_open_lease_expires_at)" not in trigger_sql


def _rows(
    connection: sqlite3.Connection, tables: tuple[str, ...]
) -> dict[str, tuple[tuple[object, ...], ...]]:
    return {
        table: tuple(
            tuple(row)
            for row in connection.execute(f'SELECT rowid,* FROM "{table}" ORDER BY rowid')
        )
        for table in tables
    }


def _schema(
    connection: sqlite3.Connection, tables: tuple[str, ...]
) -> tuple[tuple[object, ...], ...]:
    marks = ",".join("?" for _ in tables)
    rows: list[tuple[object, ...]] = []
    for row in connection.execute(
        f"SELECT type,name,tbl_name,sql FROM sqlite_schema "
        f"WHERE tbl_name IN({marks}) ORDER BY type,name",
        tables,
    ):
        values = list(row)
        if isinstance(values[3], str):
            for table in tables:
                values[3] = values[3].replace(f'CREATE TABLE "{table}"', f"CREATE TABLE {table}")
        rows.append(tuple(values))
    return tuple(rows)


def _apply_0076(templates: dict[str, Path], database_url: str) -> None:
    result = apply_migrations(database_url, migrations_dir=templates["migrations_0076"])
    assert result.applied == ["0076"]


def _insert_operation(
    connection: sqlite3.Connection,
    graph: dict[str, object],
    marker: str,
) -> tuple[str, str]:
    operation_id = prd082._id("mcpo_", f"prd083-operation-{marker}")
    job_id = prd082._id("mcpj_", f"prd083-job-{marker}")
    connection.execute(
        """
        INSERT INTO mcp_server_operations(
          mcp_server_operation_id,tenant_id,mcp_server_id,operation_type,
          idempotency_key_hmac,idempotency_key_hmac_version,intent_hmac,
          intent_hmac_version,actor_user_id,start_server_revision,
          start_credential_revision,candidate_credential_revision,status,error_code,
          request_id,trace_id,created_at,completed_at,updated_at
        ) VALUES(?,?,?,'refresh',?,1,?,1,?,2,1,NULL,'queued',NULL,?,?,?,NULL,?)
        """,
        (
            operation_id,
            graph["tenant_id"],
            graph["server_id"],
            prd082._digest(f"operation-key-{marker}"),
            prd082._digest(f"operation-intent-{marker}"),
            graph["user_id"],
            f"req_prd083_{marker}",
            f"trc_prd083_{marker}",
            NOW,
            NOW,
        ),
    )
    return operation_id, job_id


def _insert_terminal_operation_job(
    connection: sqlite3.Connection,
    graph: dict[str, object],
    marker: str,
) -> tuple[str, str]:
    operation_id, job_id = _insert_operation(connection, graph, marker)
    connection.execute(
        """
        INSERT INTO mcp_server_sync_jobs(
          mcp_server_sync_job_id,tenant_id,mcp_server_id,mcp_server_operation_id,
          candidate_credential_revision,job_type,status,attempt_count,lease_generation,
          lease_owner_id,lease_token,lease_expires_at,next_attempt_at,deadline_at,
          error_code,created_at,started_at,completed_at,updated_at
        ) VALUES(?,?,?,?,NULL,'discover','pending',0,0,NULL,NULL,NULL,?,?,NULL,?,
                 NULL,NULL,?)
        """,
        (
            job_id,
            graph["tenant_id"],
            graph["server_id"],
            operation_id,
            NOW,
            FUTURE,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        UPDATE mcp_server_sync_jobs
        SET status='processing',attempt_count=1,lease_generation=1,
          lease_owner_id='migration-test-worker',lease_token=?,lease_expires_at=?,
          next_attempt_at=NULL,started_at=?,updated_at=?
        WHERE mcp_server_sync_job_id=?
        """,
        (prd082._digest(f"job-lease-{marker}"), FUTURE, NOW_1, NOW_1, job_id),
    )
    connection.execute(
        """
        UPDATE mcp_server_sync_jobs
        SET status='succeeded',lease_generation=2,lease_owner_id=NULL,lease_token=NULL,
          lease_expires_at=NULL,completed_at=?,updated_at=?
        WHERE mcp_server_sync_job_id=?
        """,
        (NOW_2, NOW_2, job_id),
    )
    connection.execute(
        """
        UPDATE mcp_server_operations
        SET status='succeeded',completed_at=?,updated_at=?
        WHERE mcp_server_operation_id=?
        """,
        (NOW_3, NOW_3, operation_id),
    )
    return operation_id, job_id


def _insert_terminal_invocation_attempt(
    connection: sqlite3.Connection,
    graph: dict[str, object],
    marker: str,
) -> tuple[str, str]:
    invocation_id, attempt_id = prd082._claim_probe_attempt(
        connection, graph, marker, expires_at=FUTURE
    )
    connection.execute(
        """
        UPDATE mcp_invocation_attempts
        SET status='failed',error_code='mcp.server_unavailable',
          response_bytes=0,completed_at=?,duration_ms=100
        WHERE mcp_invocation_attempt_id=?
        """,
        (NOW_2, attempt_id),
    )
    connection.execute(
        """
        UPDATE mcp_invocations
        SET status='failed',state_revision=3,lease_generation=2,
          lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
          completed_at=?,duration_ms=100,error_code='mcp.server_unavailable',
          updated_at=?
        WHERE mcp_invocation_id=?
        """,
        (NOW_3, NOW_3, invocation_id),
    )
    return invocation_id, attempt_id


def _assert_catalog_18(connection: sqlite3.Connection) -> None:
    assert tuple(
        connection.execute(
            "SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision) "
            "FROM rbac_permission_templates"
        ).fetchone()
    ) == (85, 18, 18)
    assert (
        connection.execute("SELECT COUNT(*) FROM rbac_role_permission_templates").fetchone()[0]
        == 224
    )


def test_prd083_empty_zero_to_76_repeat_down_reup(
    prd083_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _url(tmp_path / "fresh-0076.sqlite3")
    result = apply_migrations(database_url, migrations_dir=prd083_templates["migrations_0076"])
    assert result.applied == [f"{version:04d}" for version in range(1, 77)]
    repeat = apply_migrations(database_url, migrations_dir=prd083_templates["migrations_0076"])
    assert repeat.applied == [] and repeat.skipped[-1] == "0076"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0076"
        _assert_catalog_18(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' "
            f"AND name IN({','.join('?' for _ in NETWORK_TABLES)})",
            NETWORK_TABLES,
        ).fetchone()[0] == len(NETWORK_TABLES)
        _assert_half_open_reclaim_schema(connection)
        _healthy(connection)

    assert (
        rollback_last_migration(
            database_url,
            migrations_dir=prd083_templates["migrations_0076"],
            expected_version="0076",
        ).rolled_back
        == "0076"
    )
    with connect_database(database_url) as connection:
        assert _head(connection) == "0075"
        prd082._assert_catalog(connection, 17)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'mcp_network_%'"
            ).fetchone()[0]
            == 0
        )
        _healthy(connection)

    assert apply_migrations(
        database_url, migrations_dir=prd083_templates["migrations_0076"]
    ).applied == ["0076"]
    with connect_database(database_url) as connection:
        assert _head(connection) == "0076"
        _assert_catalog_18(connection)
        _assert_half_open_reclaim_schema(connection)
        _healthy(connection)


def test_prd083_seeded_roundtrip_preserves_rowids_values_and_0075_schema(
    prd083_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0075(prd083_templates, tmp_path / "seeded-roundtrip.sqlite3")
    with connect_database(database_url) as connection:
        graph = prd082._insert_server_tool(connection, "prd083-roundtrip")
        _insert_terminal_operation_job(connection, graph, "roundtrip")
        blocked = prd082._invocation_values(
            graph,
            "prd083-blocked",
            status="blocked",
            input_dlp_status="blocked",
            input_dlp_match_count=1,
        )
        prd082._insert_mapping(connection, "mcp_invocations", blocked)
        _insert_terminal_invocation_attempt(connection, graph, "prd083-attempt")
        prd082._insert_legacy_audit(connection)
        custom_role_id, custom_relations = prd082._customize_role_matrix(
            connection, "security_admin"
        )
        connection.commit()
        before_rows = _rows(connection, STATE_TABLES + ("audit_logs",))
        before_schema = _schema(connection, STATE_TABLES + ("audit_logs",))
        _healthy(connection)

    _apply_0076(prd083_templates, database_url)
    with connect_database(database_url) as connection:
        assert _rows(connection, STATE_TABLES + ("audit_logs",)) == before_rows
        assert connection.execute("SELECT COUNT(*) FROM mcp_network_policies").fetchone()[0] == 1
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM mcp_network_policy_revisions
            WHERE policy_revision=1 AND status='disabled' AND policy_fingerprint=?
            """,
                (DEFAULT_FINGERPRINT,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM mcp_invocation_network_bindings").fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.role_id=? AND permission.code='mcp.network.manage'
            """,
                (custom_role_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            tuple(
                tuple(row)
                for row in connection.execute(
                    """
                SELECT relation.rowid,relation.*,permission.code
                FROM role_permissions relation JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.role_id=? ORDER BY relation.rowid
                """,
                    (custom_role_id,),
                )
            )
            == custom_relations
        )
        _healthy(connection)

    assert (
        rollback_last_migration(
            database_url,
            migrations_dir=prd083_templates["migrations_0076"],
            expected_version="0076",
        ).rolled_back
        == "0076"
    )
    with connect_database(database_url) as connection:
        assert _rows(connection, STATE_TABLES + ("audit_logs",)) == before_rows
        assert _schema(connection, STATE_TABLES + ("audit_logs",)) == before_schema
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'mcp_network_%'"
            ).fetchone()[0]
            == 0
        )
        _healthy(connection)


@pytest.mark.parametrize("active_kind", ["operation", "job", "invocation"])
def test_prd083_drain_guard_is_atomic(
    prd083_templates: dict[str, Path], tmp_path: Path, active_kind: str
) -> None:
    database_url = _clone_0075(prd083_templates, tmp_path / f"drain-{active_kind}.sqlite3")
    with connect_database(database_url) as connection:
        graph = prd082._insert_server_tool(connection, f"drain-{active_kind}")
        if active_kind in {"operation", "job"}:
            operation_id, job_id = _insert_operation(connection, graph, f"drain-{active_kind}")
            if active_kind == "job":
                connection.execute(
                    """
                    INSERT INTO mcp_server_sync_jobs(
                      mcp_server_sync_job_id,tenant_id,mcp_server_id,
                      mcp_server_operation_id,candidate_credential_revision,job_type,
                      status,attempt_count,lease_generation,lease_owner_id,lease_token,
                      lease_expires_at,next_attempt_at,deadline_at,error_code,created_at,
                      started_at,completed_at,updated_at
                    ) VALUES(?,?,?,?,NULL,'discover','pending',0,0,NULL,NULL,NULL,?,?,
                             NULL,?,NULL,NULL,?)
                    """,
                    (
                        job_id,
                        graph["tenant_id"],
                        graph["server_id"],
                        operation_id,
                        NOW,
                        FUTURE,
                        NOW,
                        NOW,
                    ),
                )
        else:
            prd082._insert_mapping(
                connection,
                "mcp_invocations",
                prd082._invocation_values(graph, "drain-invocation"),
            )
        connection.commit()
        before = _rows(connection, STATE_TABLES)
        catalog_before = _rows(
            connection,
            ("rbac_permission_templates", "rbac_role_permission_templates"),
        )

    with pytest.raises(DbError, match="prd083.mcp_drain_required"):
        apply_migrations(database_url, migrations_dir=prd083_templates["migrations_0076"])
    with connect_database(database_url) as connection:
        assert _head(connection) == "0075"
        assert _rows(connection, STATE_TABLES) == before
        assert (
            _rows(
                connection,
                ("rbac_permission_templates", "rbac_role_permission_templates"),
            )
            == catalog_before
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'mcp_network_%'"
            ).fetchone()[0]
            == 0
        )
        _healthy(connection)


def test_prd083_waiting_policy_resumes_with_new_deadline(
    prd083_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0075(prd083_templates, tmp_path / "waiting.sqlite3")
    _apply_0076(prd083_templates, database_url)
    with connect_database(database_url) as connection:
        graph = prd082._insert_server_tool(connection, "prd083-waiting")
        operation_id, job_id = _insert_operation(connection, graph, "waiting")
        connection.execute(
            """
            INSERT INTO mcp_server_sync_jobs(
              mcp_server_sync_job_id,tenant_id,mcp_server_id,mcp_server_operation_id,
              candidate_credential_revision,job_type,status,attempt_count,
              lease_generation,lease_owner_id,lease_token,lease_expires_at,
              next_attempt_at,deadline_at,error_code,created_at,started_at,
              completed_at,updated_at
            ) VALUES(?,?,?,?,NULL,'discover','waiting_policy',0,0,NULL,NULL,NULL,
                     NULL,NULL,NULL,?,NULL,NULL,?)
            """,
            (
                job_id,
                graph["tenant_id"],
                graph["server_id"],
                operation_id,
                NOW,
                NOW,
            ),
        )
        connection.commit()
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM mcp_server_sync_jobs
            WHERE status IN('pending','processing')
              AND (next_attempt_at<=? OR lease_expires_at<=?)
            """,
                (NOW_1, NOW_1),
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(sqlite3.IntegrityError):
            second_operation, second_job = _insert_operation(connection, graph, "waiting-duplicate")
            connection.execute(
                """
                INSERT INTO mcp_server_sync_jobs(
                  mcp_server_sync_job_id,tenant_id,mcp_server_id,
                  mcp_server_operation_id,candidate_credential_revision,job_type,
                  status,attempt_count,lease_generation,lease_owner_id,lease_token,
                  lease_expires_at,next_attempt_at,deadline_at,error_code,created_at,
                  started_at,completed_at,updated_at
                ) VALUES(?,?,?,?,NULL,'discover','waiting_policy',0,0,NULL,NULL,NULL,
                         NULL,NULL,NULL,?,NULL,NULL,?)
                """,
                (
                    second_job,
                    graph["tenant_id"],
                    graph["server_id"],
                    second_operation,
                    NOW,
                    NOW,
                ),
            )
        connection.rollback()

        policy = connection.execute(
            """
            SELECT * FROM mcp_network_policies
            WHERE tenant_id=? AND mcp_server_id=?
            """,
            (graph["tenant_id"], graph["server_id"]),
        ).fetchone()
        assert policy is not None
        active_fingerprint = prd082._digest("prd083-active-policy")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO mcp_network_policy_revisions(
              mcp_network_policy_id,tenant_id,mcp_server_id,policy_revision,
              policy_fingerprint,status,egress_mode,priority,condition_expr,action,
              connect_timeout_seconds,probe_timeout_seconds,call_timeout_seconds,
              failure_threshold,open_seconds,updated_by,request_id,trace_id,created_at
            ) VALUES(?,?,?,2,?,'active','registered_endpoint_only',1000,
                     'registered_endpoint_origin','allow',5,10,30,3,60,?,?,?,?)
            """,
            (
                policy["mcp_network_policy_id"],
                graph["tenant_id"],
                graph["server_id"],
                active_fingerprint,
                graph["user_id"],
                "req_prd083_waiting_active",
                "trc_prd083_waiting_active",
                NOW_2,
            ),
        )
        connection.execute(
            """
            UPDATE mcp_network_policies
            SET current_revision=2,current_fingerprint=?,status='active',updated_at=?
            WHERE tenant_id=? AND mcp_server_id=? AND current_revision=1
            """,
            (
                active_fingerprint,
                NOW_2,
                graph["tenant_id"],
                graph["server_id"],
            ),
        )
        assert (
            connection.execute(
                """
            UPDATE mcp_server_sync_jobs
            SET status='pending',next_attempt_at=?,deadline_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_server_id=? AND status='waiting_policy'
            """,
                (
                    NOW_2,
                    FUTURE,
                    NOW_2,
                    graph["tenant_id"],
                    graph["server_id"],
                ),
            ).rowcount
            == 1
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM mcp_server_sync_jobs WHERE mcp_server_sync_job_id=?",
            (job_id,),
        ).fetchone()
        assert row is not None
        assert (
            row["status"],
            row["attempt_count"],
            row["lease_generation"],
            row["next_attempt_at"],
            row["deadline_at"],
        ) == ("pending", 0, 0, NOW_2, FUTURE)
        _healthy(connection)


def test_prd083_network_outbox_recovery_and_event_exclusivity(
    prd083_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0075(prd083_templates, tmp_path / "network-outbox.sqlite3")
    _apply_0076(prd083_templates, database_url)
    with connect_database(database_url) as connection:
        graph = prd082._insert_server_tool(connection, "prd083-network-outbox")
        invocation = prd082._invocation_values(
            graph,
            "prd083-network-outbox",
            status="blocked",
            input_dlp_status="clean",
        )
        invocation["error_code"] = "mcp.network_blocked"
        prd082._insert_mapping(connection, "mcp_invocations", invocation)
        policy = connection.execute(
            "SELECT * FROM mcp_network_policies WHERE tenant_id=? AND mcp_server_id=?",
            (graph["tenant_id"], graph["server_id"]),
        ).fetchone()
        assert policy is not None
        decision_id = prd082._id("mcpnd_", "prd083-network-outbox-decision")
        event_id = prd082._id("mcpne_", "prd083-network-outbox-event")
        outbox_id = prd082._id("mcpno_", "prd083-network-outbox-owner")
        connection.execute(
            """
            INSERT INTO mcp_network_decisions(
              mcp_network_decision_id,tenant_id,mcp_server_id,
              mcp_server_operation_id,mcp_invocation_id,mcp_network_policy_id,
              phase,result,reason_code,policy_revision,policy_fingerprint,
              address_count,duration_ms,attempt_number,dispatch_id,permit_digest,
              observed_circuit_revision,half_open_owner_type,half_open_owner_id,
              half_open_lease_generation,request_id,trace_id,created_at
            ) VALUES(?,?,?,NULL,?,?,'probe','blocked','policy_disabled',1,?,
                     0,0,1,'dispatch_prd083_outbox',NULL,0,NULL,NULL,NULL,?,?,?)
            """,
            (
                decision_id,
                graph["tenant_id"],
                graph["server_id"],
                invocation["mcp_invocation_id"],
                policy["mcp_network_policy_id"],
                policy["current_fingerprint"],
                invocation["request_id"],
                invocation["trace_id"],
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO mcp_network_events(
              mcp_network_event_id,mcp_network_decision_id,tenant_id,mcp_server_id,
              mcp_server_operation_id,mcp_invocation_id,phase,reason_code,
              policy_revision,address_count,attempt_number,request_id,trace_id,occurred_at
            ) VALUES(?,?,?,?,NULL,?,'probe','policy_disabled',1,0,1,?,?,?)
            """,
            (
                event_id,
                decision_id,
                graph["tenant_id"],
                graph["server_id"],
                invocation["mcp_invocation_id"],
                invocation["request_id"],
                invocation["trace_id"],
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO mcp_network_event_outbox(
              mcp_network_event_outbox_id,tenant_id,mcp_network_event_id,status,
              attempt_count,lease_generation,lease_owner_id,lease_token,
              lease_expires_at,next_attempt_at,subscription_snapshot_json,
              snapshot_frozen_at,error_code,delivered_at,created_at,updated_at
            ) VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,?,NULL,NULL,NULL,NULL,?,?)
            """,
            (outbox_id, graph["tenant_id"], event_id, NOW, NOW, NOW),
        )
        connection.commit()

        table_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' "
                "AND name='mcp_network_event_outbox'"
            ).fetchone()[0]
        )
        assert "error_code TEXT" in table_sql
        assert "last_error_code" not in table_sql
        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND name IN(?,?)",
                (
                    "trg_mcp_network_call_intents_event_exclusion_prd083",
                    "trg_mcp_events_network_exclusion_prd083",
                ),
            )
        }
        assert trigger_names == {
            "trg_mcp_network_call_intents_event_exclusion_prd083",
            "trg_mcp_events_network_exclusion_prd083",
        }

        connection.execute(
            """
            UPDATE mcp_network_event_outbox
            SET status='processing',attempt_count=1,lease_generation=1,
                lease_owner_id='worker-prd083',lease_token=?,lease_expires_at=?,
                next_attempt_at=NULL,error_code=NULL,updated_at=?
            WHERE mcp_network_event_outbox_id=?
            """,
            ("1" * 64, "2026-08-16T00:03:00Z", "2026-08-16T00:01:00Z", outbox_id),
        )
        connection.execute(
            """
            UPDATE mcp_network_event_outbox
            SET lease_generation=2,lease_token=?,lease_expires_at=?,
                subscription_snapshot_json='[]',snapshot_frozen_at=?,updated_at=?
            WHERE mcp_network_event_outbox_id=?
            """,
            (
                "2" * 64,
                "2026-08-16T00:03:01Z",
                "2026-08-16T00:01:01Z",
                "2026-08-16T00:01:01Z",
                outbox_id,
            ),
        )
        connection.execute(
            """
            UPDATE mcp_network_event_outbox
            SET lease_generation=3,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE mcp_network_event_outbox_id=?
            """,
            ("3" * 64, "2026-08-16T00:03:02Z", "2026-08-16T00:01:02Z", outbox_id),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="mcp_network_outbox_transition_invalid"):
            connection.execute(
                """
                UPDATE mcp_network_event_outbox
                SET lease_generation=4,lease_token=?,lease_expires_at=?,
                    subscription_snapshot_json='[\"tampered\"]',updated_at=?
                WHERE mcp_network_event_outbox_id=?
                """,
                (
                    "4" * 64,
                    "2026-08-16T00:03:03Z",
                    "2026-08-16T00:01:03Z",
                    outbox_id,
                ),
            )
        connection.rollback()

        generation = 3
        for attempt in range(1, 10):
            release_at = f"2026-08-16T00:{attempt + 1:02d}:00Z"
            connection.execute(
                """
                UPDATE mcp_network_event_outbox
                SET status='pending',lease_generation=lease_generation+1,
                    lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                    next_attempt_at=?,error_code=NULL,delivered_at=NULL,updated_at=?
                WHERE mcp_network_event_outbox_id=?
                """,
                (release_at, release_at, outbox_id),
            )
            generation += 1
            next_attempt = attempt + 1
            claim_at = release_at
            expires_at = f"2026-08-16T00:{next_attempt + 2:02d}:00Z"
            token = format(generation + 1, "064x")
            connection.execute(
                """
                UPDATE mcp_network_event_outbox
                SET status='processing',attempt_count=?,
                    lease_generation=lease_generation+1,
                    lease_owner_id='worker-prd083',lease_token=?,lease_expires_at=?,
                    next_attempt_at=NULL,error_code=NULL,delivered_at=NULL,updated_at=?
                WHERE mcp_network_event_outbox_id=?
                """,
                (next_attempt, token, expires_at, claim_at, outbox_id),
            )
            generation += 1

        exhausted = connection.execute(
            "SELECT attempt_count,lease_generation,lease_expires_at "
            "FROM mcp_network_event_outbox WHERE mcp_network_event_outbox_id=?",
            (outbox_id,),
        ).fetchone()
        assert tuple(exhausted) == (10, generation, "2026-08-16T00:12:00Z")
        connection.execute(
            """
            UPDATE mcp_network_event_outbox
            SET status='failed',lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=NULL,error_code='webhook.delivery_unavailable',
                delivered_at=NULL,updated_at='2026-08-16T00:12:00Z'
            WHERE mcp_network_event_outbox_id=?
            """,
            (outbox_id,),
        )
        connection.commit()
        terminal = connection.execute(
            "SELECT status,attempt_count,error_code,subscription_snapshot_json "
            "FROM mcp_network_event_outbox WHERE mcp_network_event_outbox_id=?",
            (outbox_id,),
        ).fetchone()
        assert tuple(terminal) == (
            "failed",
            10,
            "webhook.delivery_unavailable",
            "[]",
        )
        _healthy(connection)


def test_prd083_network_audit_contract_and_down_guard(
    prd083_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0075(prd083_templates, tmp_path / "audit-guard.sqlite3")
    _apply_0076(prd083_templates, database_url)
    with connect_database(database_url) as connection:
        graph = prd082._insert_server_tool(connection, "prd083-audit")
        policy = connection.execute(
            "SELECT * FROM mcp_network_policies WHERE tenant_id=? AND mcp_server_id=?",
            (graph["tenant_id"], graph["server_id"]),
        ).fetchone()
        assert policy is not None
        fingerprint = prd082._digest("prd083-audit-policy")
        connection.execute(
            """
            INSERT INTO mcp_network_policy_revisions(
              mcp_network_policy_id,tenant_id,mcp_server_id,policy_revision,
              policy_fingerprint,status,egress_mode,priority,condition_expr,action,
              connect_timeout_seconds,probe_timeout_seconds,call_timeout_seconds,
              failure_threshold,open_seconds,updated_by,request_id,trace_id,created_at
            ) VALUES(?,?,?,2,?,'active','registered_endpoint_only',1000,
                     'registered_endpoint_origin','allow',5,10,30,3,60,?,?,?,?)
            """,
            (
                policy["mcp_network_policy_id"],
                graph["tenant_id"],
                graph["server_id"],
                fingerprint,
                graph["user_id"],
                "req_prd083_audit",
                "trc_prd083_audit",
                NOW_2,
            ),
        )
        connection.execute(
            """
            UPDATE mcp_network_policies
            SET current_revision=2,current_fingerprint=?,status='active',updated_at=?
            WHERE tenant_id=? AND mcp_server_id=?
            """,
            (fingerprint, NOW_2, graph["tenant_id"], graph["server_id"]),
        )
        before = {
            "mcp_server_id": graph["server_id"],
            "status": "disabled",
            "revision": 1,
            "egress_mode": "registered_endpoint_only",
            "destination_count": 0,
            "connect_timeout_seconds": 5,
            "probe_timeout_seconds": 10,
            "call_timeout_seconds": 30,
            "failure_threshold": 3,
            "open_seconds": 60,
            "waiting_job_resume_count": 0,
        }
        after = {**before, "status": "active", "revision": 2, "destination_count": 1}
        changed = ["destination_count", "revision", "status"]
        identity = prd082._identity(connection)
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,
              object_type,object_id,object_name,before_summary_json,
              after_summary_json,changed_fields_json,client_ip,user_agent,
              request_id,trace_id,created_at
            ) VALUES('aud_prd083_network',?,?,?,?,?,
                     'mcp_network_policy',?,NULL,?,?,?,NULL,NULL,?,?,?)
            """,
            (
                graph["tenant_id"],
                identity["user_id"],
                identity["email"],
                identity["role"],
                "mcp.network_policy_updated",
                policy["mcp_network_policy_id"],
                json.dumps(before, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                json.dumps(after, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                json.dumps(changed, ensure_ascii=True, separators=(",", ":")),
                "req_prd083_audit",
                "trc_prd083_audit",
                NOW_2,
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            unsafe = {**after, "endpoint": "forbidden"}
            connection.execute(
                """
                INSERT INTO audit_logs(
                  audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,
                  object_type,object_id,object_name,before_summary_json,
                  after_summary_json,changed_fields_json,client_ip,user_agent,
                  request_id,trace_id,created_at
                ) VALUES('aud_prd083_unsafe',?,?,?,?,?,'mcp_network_policy',?,
                         NULL,?,?,?,NULL,NULL,?,?,?)
                """,
                (
                    graph["tenant_id"],
                    identity["user_id"],
                    identity["email"],
                    identity["role"],
                    "mcp.network_policy_updated",
                    policy["mcp_network_policy_id"],
                    json.dumps(before),
                    json.dumps(unsafe),
                    json.dumps(changed),
                    "req_prd083_audit",
                    "trc_prd083_audit",
                    NOW_2,
                ),
            )
        connection.rollback()
        _healthy(connection)

    with pytest.raises(DbError, match="prd083_down_guard_failed"):
        rollback_last_migration(
            database_url,
            migrations_dir=prd083_templates["migrations_0076"],
            expected_version="0076",
        )
    with connect_database(database_url) as connection:
        assert _head(connection) == "0076"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='mcp.network_policy_updated'"
            ).fetchone()[0]
            == 1
        )
        _healthy(connection)
