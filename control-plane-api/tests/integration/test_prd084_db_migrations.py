"""PRD-084 local Endpoint migration fidelity, ownership and rollback guards."""

from __future__ import annotations

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
NOW = "2026-08-16T00:00:00Z"
NOW_1 = "2026-08-16T00:00:01Z"
NOW_2 = "2026-08-16T00:00:02Z"
FUTURE = "2026-08-16T01:00:00Z"
V1_HASH = "9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870"
V2_HASH = "c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf"
LOCAL_TABLES = (
    "local_model_endpoints",
    "local_model_catalog_entries",
    "local_endpoint_create_receipts",
    "local_model_call_events",
    "local_model_call_event_outbox",
)
LEGACY_PROVIDER_TRIGGERS = {
    "trg_policy_template_provider_delete_prd068",
    "trg_policy_template_provider_update_prd068",
    "trg_providers_experiment_arm_delete_prd075",
    "trg_providers_experiment_arm_update_prd075",
}
MATRIX_19 = {
    "admin": "76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074",
    "developer": "ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1",
    "ops_admin": "aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba",
}


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
def prd084_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("prd084-migration-templates")
    migrations_0076 = _migration_subset(root / "migrations-0076", 76)
    migrations_0077 = _migration_subset(root / "migrations-0077", 77)
    database = root / "seeded-0076.sqlite3"
    database_url = _url(database)
    result = apply_migrations(database_url, migrations_dir=migrations_0076)
    assert result.applied == [f"{version:04d}" for version in range(1, 77)]
    apply_seed_data(database_url)
    return {
        "database_0076": database,
        "migrations_0076": migrations_0076,
        "migrations_0077": migrations_0077,
    }


def _clone_0076(templates: dict[str, Path], destination: Path) -> str:
    shutil.copy2(templates["database_0076"], destination)
    return _url(destination)


def _head(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE status='applied' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return str(row[0])


def _healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _rows(connection: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(f'SELECT rowid,* FROM "{table}" ORDER BY rowid')
    )


def _columns(connection: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info('{table}')"))


def _identity(connection: sqlite3.Connection) -> dict[str, str]:
    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,provider.provider_id,model.model_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN providers provider ON provider.tenant_id=tenant.tenant_id
        JOIN models model ON model.tenant_id=tenant.tenant_id
                         AND model.provider_id=provider.provider_id
        WHERE tenant.slug='default' LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return {
        "tenant_id": str(row[0]),
        "user_id": str(row[1]),
        "legacy_provider_id": str(row[2]),
        "legacy_model_id": str(row[3]),
    }


def _apply_0077(templates: dict[str, Path], database_url: str) -> None:
    assert apply_migrations(
        database_url, migrations_dir=templates["migrations_0077"]
    ).applied == ["0077"]


def _rollback_0077(templates: dict[str, Path], database_url: str) -> None:
    result = rollback_last_migration(
        database_url,
        migrations_dir=templates["migrations_0077"],
        expected_version="0077",
    )
    assert result.rolled_back == "0077"


def _assert_revision_19(connection: sqlite3.Connection) -> None:
    assert tuple(
        connection.execute(
            "SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision) "
            "FROM rbac_permission_templates"
        ).fetchone()
    ) == (88, 19, 19)
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_role_permission_templates"
    ).fetchone()[0] == 232
    role_matrices = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT code,matrix_sha256 FROM roles "
            "WHERE code IN('admin','developer','ops_admin')"
        )
    }
    if role_matrices:
        assert role_matrices == MATRIX_19
    grants = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """
            SELECT role.code,permission.code
            FROM role_permissions relation
            JOIN roles role ON role.tenant_id=relation.tenant_id
                           AND role.role_id=relation.role_id
            JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                           AND permission.permission_id=relation.permission_id
            WHERE permission.code LIKE 'local_endpoint.%'
            """
        )
    }
    expected_grants = {
        ("admin", "local_endpoint.read"),
        ("admin", "local_endpoint.manage"),
        ("admin", "local_endpoint.test"),
        ("developer", "local_endpoint.read"),
        ("developer", "local_endpoint.test"),
        ("ops_admin", "local_endpoint.read"),
        ("ops_admin", "local_endpoint.manage"),
        ("ops_admin", "local_endpoint.test"),
    }
    assert grants == (expected_grants if role_matrices else set())


def _insert_local_graph(connection: sqlite3.Connection) -> dict[str, str]:
    identity = _identity(connection)
    provider_id = "provider_prd084_local"
    model_id = "model_prd084_local"
    endpoint_id = "lme_" + "1" * 32
    connection.execute(
        """
        INSERT INTO providers(
          provider_id,tenant_id,provider_code,name,base_url,status,created_at,updated_at,
          provider_type,auth_type,created_by
        ) VALUES(?,?,'prd084_local','PRD084 local','https://local.invalid/v1',
                 'active',?,?,'local_endpoint','none',?)
        """,
        (provider_id, identity["tenant_id"], NOW, NOW, identity["user_id"]),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id,tenant_id,provider_id,model_alias,upstream_model_name,
          capabilities,status,created_at,updated_at,provider_model_id,display_name,
          context_window,input_price_per_1k,output_price_per_1k,currency,created_by
        ) VALUES(?,?,?,'prd084-local-chat','served-model','{"chat":true}',
                 'active',?,?,'served-model','PRD084 local chat',32768,0,0,'CNY',?)
        """,
        (model_id, identity["tenant_id"], provider_id, NOW, NOW, identity["user_id"]),
    )
    connection.execute(
        """
        INSERT INTO local_model_endpoints(
          local_endpoint_id,tenant_id,provider_id,initial_model_id,engine_kind,
          revision,catalog_revision,catalog_status,last_discovered_at,
          last_catalog_error_code,created_by,status,created_at,updated_at
        ) VALUES(?,?,?,?, 'vllm',1,0,'unknown',NULL,NULL,?,'active',?,?)
        """,
        (
            endpoint_id,
            identity["tenant_id"],
            provider_id,
            model_id,
            identity["user_id"],
            NOW,
            NOW,
        ),
    )
    return {
        **identity,
        "provider_id": provider_id,
        "model_id": model_id,
        "endpoint_id": endpoint_id,
    }


def _insert_attempt(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    marker: str,
) -> str:
    suffix = {"called": "3", "not_called": "4", "recovery": "5"}[marker]
    attempt_id = "rta_" + suffix * 32
    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id,tenant_id,request_id,trace_id,model_alias_id,
          fallback_policy_id,fallback_policy_version,attempt_sequence,attempt_role,
          trigger_type,trigger_error_code,model_id,internal_model_id,provider_id,
          provider_code,status,error_code,error_category,upstream_http_status,
          retryable,latency_ms,usage_status,usage_ledger_id,estimated_cost_cny,
          created_at,completed_at,updated_at,provider_call_status,
          provider_call_started_at
        ) VALUES(?,?,?,? ,NULL,NULL,NULL,1,'primary','initial',NULL,?,
                 'prd084-local-chat',?,'prd084_local','started',NULL,NULL,NULL,
                 0,NULL,'pending',NULL,NULL,?,NULL,?,'not_called',NULL)
        """,
        (
            attempt_id,
            graph["tenant_id"],
            f"req_prd084_{marker}",
            f"trc_prd084_{marker}",
            graph["model_id"],
            graph["provider_id"],
            NOW,
            NOW,
        ),
    )
    return attempt_id


def test_prd084_empty_zero_to_77_repeat_down_reup(
    prd084_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _url(tmp_path / "fresh-0077.sqlite3")
    result = apply_migrations(database_url, migrations_dir=prd084_templates["migrations_0077"])
    assert result.applied == [f"{version:04d}" for version in range(1, 78)]
    repeat = apply_migrations(database_url, migrations_dir=prd084_templates["migrations_0077"])
    assert repeat.applied == [] and repeat.skipped[-1] == "0077"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0077"
        _assert_revision_19(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' "
            f"AND name IN({','.join('?' for _ in LOCAL_TABLES)})",
            LOCAL_TABLES,
        ).fetchone()[0] == len(LOCAL_TABLES)
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_adapter_catalog_activations"
        ).fetchone()[0] == 0
        _healthy(connection)

    _rollback_0077(prd084_templates, database_url)
    with connect_database(database_url) as connection:
        assert _head(connection) == "0076"
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'local_model_%' "
            "OR name='local_endpoint_create_receipts'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_adapter_catalog_activations"
        ).fetchone()[0] == 0
        assert not {
            str(row[1]) for row in connection.execute("PRAGMA table_info('routing_attempts')")
        } & {"provider_call_status", "provider_call_started_at"}
        _healthy(connection)

    _apply_0077(prd084_templates, database_url)
    with connect_database(database_url) as connection:
        assert _head(connection) == "0077"
        _assert_revision_19(connection)
        _healthy(connection)


def test_prd084_seeded_roundtrip_preserves_rows_columns_and_schema_objects(
    prd084_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0076(prd084_templates, tmp_path / "seeded-roundtrip.sqlite3")
    preserved_tables = (
        "providers",
        "models",
        "provider_credentials",
        "request_logs",
        "usage_ledger",
        "routing_attempts",
    )
    with connect_database(database_url) as connection:
        before_rows = {table: _rows(connection, table) for table in preserved_tables}
        before_columns = {table: _columns(connection, table) for table in preserved_tables}
        before_adapter = _rows(connection, "provider_adapter_catalog_activations")
        before_provider_objects = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT type,name FROM sqlite_schema WHERE tbl_name='providers'"
            )
        }

    _apply_0077(prd084_templates, database_url)
    with connect_database(database_url) as connection:
        for table in preserved_tables[:-1]:
            assert _rows(connection, table) == before_rows[table]
        old_routing_names = tuple(str(row[1]) for row in before_columns["routing_attempts"])
        after_routing = tuple(
            tuple(row)
            for row in connection.execute(
                f"SELECT rowid,{','.join(old_routing_names)} FROM routing_attempts ORDER BY rowid"
            )
        )
        assert after_routing == before_rows["routing_attempts"]
        assert connection.execute(
            "SELECT COUNT(*) FROM routing_attempts WHERE provider_call_status!='not_applicable' "
            "OR provider_call_started_at IS NOT NULL"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_adapter_catalog_activations "
            "WHERE catalog_version='adapter-spec-v2' AND status='active'"
        ).fetchone()[0] == 1
        _healthy(connection)

    _rollback_0077(prd084_templates, database_url)
    with connect_database(database_url) as connection:
        for table in preserved_tables:
            assert _rows(connection, table) == before_rows[table]
            assert _columns(connection, table) == before_columns[table]
        assert _rows(connection, "provider_adapter_catalog_activations") == before_adapter
        assert {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT type,name FROM sqlite_schema WHERE tbl_name='providers'"
            )
        } == before_provider_objects
        _healthy(connection)


def test_prd084_schema_owner_fence_event_and_outbox_contract(
    prd084_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0076(prd084_templates, tmp_path / "owner-event.sqlite3")
    _apply_0077(prd084_templates, database_url)
    with connect_database(database_url) as connection:
        graph = _insert_local_graph(connection)
        connection.execute(
            """
            INSERT INTO local_endpoint_create_receipts(
              tenant_id,request_id,intent_hash,local_endpoint_id,created_at
            ) VALUES(?, 'req_prd084_create', ?, ?, ?)
            """,
            (graph["tenant_id"], "a" * 64, graph["endpoint_id"], NOW),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="local_endpoint_receipt_immutable"):
            connection.execute(
                "UPDATE local_endpoint_create_receipts SET intent_hash=? "
                "WHERE tenant_id=? AND request_id='req_prd084_create'",
                ("b" * 64, graph["tenant_id"]),
            )
        connection.rollback()

        with pytest.raises(
            sqlite3.IntegrityError, match="local_endpoint_provider_immutable"
        ):
            connection.execute(
                "UPDATE providers SET base_url='https://changed.invalid/v1' "
                "WHERE provider_id=?",
                (graph["provider_id"],),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="local_endpoint_model_immutable"):
            connection.execute("DELETE FROM models WHERE model_id=?", (graph["model_id"],))
        connection.rollback()

        connection.execute(
            """
            UPDATE local_model_endpoints
            SET catalog_revision=1,catalog_status='ready',last_discovered_at=?,
                last_catalog_error_code=NULL,updated_at=?
            WHERE local_endpoint_id=?
            """,
            (NOW_1, NOW_1, graph["endpoint_id"]),
        )
        connection.execute(
            """
            INSERT INTO local_model_catalog_entries(
              local_model_catalog_entry_id,tenant_id,local_endpoint_id,
              provider_model_id,availability_status,first_seen_revision,
              last_seen_revision,linked_model_id,created_at,updated_at
            ) VALUES(?,?,?,'served-model','available',1,1,?,?,?)
            """,
            (
                "lmcat_" + "2" * 32,
                graph["tenant_id"],
                graph["endpoint_id"],
                graph["model_id"],
                NOW_1,
                NOW_1,
            ),
        )

        called_id = _insert_attempt(connection, graph, "called")
        assert connection.execute(
            """
            UPDATE routing_attempts
            SET provider_call_status='called',provider_call_started_at=?,updated_at=?
            WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
              AND provider_call_status='not_called'
            """,
            (NOW_1, NOW_1, graph["tenant_id"], called_id),
        ).rowcount == 1
        connection.commit()
        with pytest.raises(
            sqlite3.IntegrityError, match="routing_attempt_local_fence_invalid"
        ):
            connection.execute(
                "UPDATE routing_attempts SET provider_call_status='not_called',"
                "provider_call_started_at=NULL WHERE routing_attempt_id=?",
                (called_id,),
            )
        connection.rollback()
        assert connection.execute(
            """
            UPDATE routing_attempts SET status='succeeded',latency_ms=42,
              completed_at=?,updated_at=? WHERE routing_attempt_id=? AND status='started'
            """,
            (NOW_2, NOW_2, called_id),
        ).rowcount == 1

        event = connection.execute(
            "SELECT routing_attempt_id,execution_status,outcome,latency_ms,usage_status "
            "FROM local_model_call_events WHERE routing_attempt_id=?",
            (called_id,),
        ).fetchone()
        assert tuple(event) == (called_id, "called", "succeeded", 42, "pending")
        outbox = connection.execute(
            "SELECT status,attempt_count,lease_generation,next_attempt_at "
            "FROM local_model_call_event_outbox"
        ).fetchone()
        assert tuple(outbox) == ("pending", 0, 0, NOW_2)

        not_called_id = _insert_attempt(connection, graph, "not_called")
        connection.execute(
            """
            UPDATE routing_attempts SET status='failed',error_code='gateway.local_endpoint_blocked',
              error_category='other',retryable=0,latency_ms=0,
              usage_status='not_applicable',completed_at=?,updated_at=?
            WHERE routing_attempt_id=? AND status='started'
            """,
            (NOW_2, NOW_2, not_called_id),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM local_model_call_events WHERE routing_attempt_id=?",
            (not_called_id,),
        ).fetchone()[0] == 0

        recovery_id = _insert_attempt(connection, graph, "recovery")
        assert connection.execute(
            """
            UPDATE routing_attempts
            SET provider_call_status='called',provider_call_started_at=?,updated_at=?
            WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
              AND provider_call_status='not_called'
            """,
            (NOW_1, NOW_1, graph["tenant_id"], recovery_id),
        ).rowcount == 1
        connection.commit()
        recovery_sql = """
            UPDATE routing_attempts
            SET status='failed',error_code='gateway.local_endpoint_outcome_unknown',
              error_category='other',upstream_http_status=NULL,retryable=0,
              latency_ms=1000,usage_status='unavailable',completed_at=?,updated_at=?
            WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
              AND provider_call_status='called' AND provider_call_started_at<=?
        """
        assert connection.execute(
            recovery_sql,
            (NOW_2, NOW_2, graph["tenant_id"], recovery_id, NOW_1),
        ).rowcount == 1
        assert connection.execute(
            recovery_sql,
            (NOW_2, NOW_2, graph["tenant_id"], recovery_id, NOW_1),
        ).rowcount == 0
        recovered_event = connection.execute(
            "SELECT outcome,error_code,usage_status FROM local_model_call_events "
            "WHERE routing_attempt_id=?",
            (recovery_id,),
        ).fetchone()
        assert tuple(recovered_event) == (
            "outcome_unknown",
            "gateway.local_endpoint_outcome_unknown",
            "unavailable",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM local_model_call_event_outbox outbox "
            "JOIN local_model_call_events event ON event.tenant_id=outbox.tenant_id "
            "AND event.local_model_call_event_id=outbox.local_model_call_event_id "
            "WHERE event.routing_attempt_id=?",
            (recovery_id,),
        ).fetchone()[0] == 1
        connection.commit()

        provider_trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='providers'"
            )
        }
        assert LEGACY_PROVIDER_TRIGGERS <= provider_trigger_names
        assert {
            "trg_local_endpoint_provider_update_prd084",
            "trg_local_endpoint_provider_delete_prd084",
        } <= provider_trigger_names
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name LIKE '%_prd084'"
        ).fetchone()[0] == 23
        forbidden = ("token", "prompt", "response", "header", "resolved_ip", "base_url")
        for table in LOCAL_TABLES:
            names = {
                str(row[1]).lower()
                for row in connection.execute(f"PRAGMA table_info('{table}')")
            }
            assert not names.intersection(forbidden)
        _healthy(connection)


def test_prd084_new_tenant_receives_v2_and_revision_19(
    prd084_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0076(prd084_templates, tmp_path / "new-tenant.sqlite3")
    _apply_0077(prd084_templates, database_url)
    tenant_id = "tenant_prd084_new"
    with connect_database(database_url) as connection:
        connection.execute(
            "INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at) "
            "VALUES(?,'prd084-new','PRD084 new','internal_only','active',?,?)",
            (tenant_id, NOW, NOW),
        )
        connection.commit()
        assert tuple(
            connection.execute(
                "SELECT catalog_version,catalog_hash,spec_count,status,changed "
                "FROM provider_adapter_catalog_activations WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()
        ) == ("adapter-spec-v2", V2_HASH, 10, "active", 0)
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE tenant_id=? AND catalog_revision=19",
            (tenant_id,),
        ).fetchone()[0] == 88
        assert {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT code,matrix_sha256 FROM roles WHERE tenant_id=? "
                "AND code IN('admin','developer','ops_admin')",
                (tenant_id,),
            )
        } == MATRIX_19
        _healthy(connection)

    _rollback_0077(prd084_templates, database_url)
    with connect_database(database_url) as connection:
        assert tuple(
            connection.execute(
                "SELECT catalog_version,catalog_hash,spec_count,status "
                "FROM provider_adapter_catalog_activations WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()
        ) == ("adapter-spec-v1", V1_HASH, 9, "active")
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE tenant_id=? AND catalog_revision=18",
            (tenant_id,),
        ).fetchone()[0] == 85
        _healthy(connection)


def test_prd084_down_guard_is_atomic_for_local_provider(
    prd084_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0076(prd084_templates, tmp_path / "down-guard.sqlite3")
    _apply_0077(prd084_templates, database_url)
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        connection.execute(
            """
            INSERT INTO providers(
              provider_id,tenant_id,provider_code,name,base_url,status,
              created_at,updated_at,provider_type,auth_type,created_by
            ) VALUES('provider_prd084_guard',?,'prd084_guard','PRD084 guard',
              'https://guard.invalid/v1','active',?,?,'local_endpoint','none',?)
            """,
            (identity["tenant_id"], NOW, NOW, identity["user_id"]),
        )
        connection.commit()

    with pytest.raises(DbError, match="prd084_down_guard_failed"):
        rollback_last_migration(
            database_url,
            migrations_dir=prd084_templates["migrations_0077"],
            expected_version="0077",
        )
    with connect_database(database_url) as connection:
        assert _head(connection) == "0077"
        assert connection.execute(
            "SELECT COUNT(*) FROM providers WHERE provider_id='provider_prd084_guard' "
            "AND provider_type='local_endpoint'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' "
            "AND name='local_model_endpoints'"
        ).fetchone()[0] == 1
        _healthy(connection)
