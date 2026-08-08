"""PRD-074 migration coverage for the Batch Scheduler and Worker schema."""

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
EXPECTED_VERSIONS = [f"{version:04d}" for version in range(1, 68)]
NOW = "2026-08-08T00:00:00Z"
DEADLINE = "2026-08-08T00:05:00Z"

BATCH_TABLES = {
    "batch_jobs",
    "batch_tasks",
    "batch_attempts",
    "batch_execution_grants",
    "batch_budget_holds",
    "batch_budget_allocations",
    "batch_scheduler_tenants",
    "batch_job_events",
    "batch_job_event_outbox",
    "batch_reconciliation_events",
}

REVISION_10_HASHES = {
    "admin": "ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262",
    "developer": "89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30",
    "ops_admin": "72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6",
}
REVISION_9_HASHES = {
    "admin": "e098913e46c2f1512960a9ff91f8aec5f8ccd6267d414404a07c9f591079a319",
    "developer": "a55457236abcd09c3d42d57f50e636494c5d8ec7ddd92a33f2f94fb95f7ed15f",
    "ops_admin": "6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b",
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
def prd074_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("prd074-migration-templates")
    migrations_0066 = _migration_subset(root / "migrations-0066", 66)
    migrations_0067 = _migration_subset(root / "migrations-0067", 67)
    database_0066 = root / "template-0066.sqlite3"
    database_url = _url(database_0066)
    assert apply_migrations(database_url, migrations_dir=migrations_0066).applied[-1] == "0066"
    apply_seed_data(database_url)
    return {"database_0066": database_0066, "migrations_0067": migrations_0067}


def _clone_and_upgrade(
    templates: dict[str, Path],
    destination: Path,
) -> tuple[str, sqlite3.Connection]:
    shutil.copy2(templates["database_0066"], destination)
    database_url = _url(destination)
    assert apply_migrations(
        database_url,
        migrations_dir=templates["migrations_0067"],
    ).applied == ["0067"]
    return database_url, connect_database(database_url)


def _assert_healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _applied_head(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        """
        SELECT version FROM schema_migrations
        WHERE status='applied' ORDER BY version DESC LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return str(row[0])


def _role_grants(
    connection: sqlite3.Connection,
    tenant_id: str,
    role_code: str,
) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT permission.code
            FROM roles role
            JOIN role_permissions relation
              ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE role.tenant_id=? AND role.code=?
            """,
            (tenant_id, role_code),
        )
    }


def _assert_revision_10(connection: sqlite3.Connection) -> str:
    assert connection.execute("SELECT COUNT(*) FROM rbac_permission_templates").fetchone()[0] == 64
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=10"
    ).fetchone()[0] == 64
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_role_permission_templates"
    ).fetchone()[0] == 173
    tenant_row = connection.execute(
        "SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1"
    ).fetchone()
    assert tenant_row is not None
    tenant_id = str(tenant_row[0])
    assert connection.execute(
        "SELECT COUNT(*) FROM permissions WHERE tenant_id=? AND catalog_revision=10",
        (tenant_id,),
    ).fetchone()[0] == 64
    for role_code in ("admin", "developer", "ops_admin"):
        grants = _role_grants(connection, tenant_id, role_code)
        assert {"batch.read", "batch.write"} <= grants
        role_hash = connection.execute(
            "SELECT matrix_sha256 FROM roles WHERE tenant_id=? AND code=?",
            (tenant_id, role_code),
        ).fetchone()[0]
        assert role_hash == REVISION_10_HASHES[role_code]
    assert "file.read" in _role_grants(connection, tenant_id, "ops_admin")
    return tenant_id


def _resource_graph(connection: sqlite3.Connection, marker: str) -> dict[str, str]:
    assert len(marker) == 1 and marker in "0123456789abcdef"
    identity = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,environment.environment_id,environment.code,
               model.model_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id
        JOIN environments environment ON environment.tenant_id=tenant.tenant_id
        JOIN models model ON model.tenant_id=tenant.tenant_id
        WHERE tenant.slug='default' AND user.role='admin'
          AND environment.code='dev' AND environment.status='active'
          AND model.status='active'
        ORDER BY user.user_id,model.model_id LIMIT 1
        """
    ).fetchone()
    assert identity is not None
    tenant_id, user_id, environment_id, environment_code, model_id = map(str, identity)
    graph = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "environment_id": environment_id,
        "environment_code": environment_code,
        "project_id": f"prj_prd074_{marker}",
        "model_alias_id": f"mal_prd074_{marker}",
        "virtual_key_id": f"vkey_prd074_{marker}",
        "file_id": "file_" + marker * 32,
    }
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,
          status,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'Batch migration fixture',NULL,?,'active',?,?,?,?)
        """,
        (
            graph["project_id"],
            tenant_id,
            f"prd074-{marker}",
            user_id,
            user_id,
            user_id,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,default_model_id,
          fallback_model_id,status,description,created_by,disabled_at,
          disabled_by,created_at,updated_at,environment_id
        ) VALUES(?,?,?,?,?,NULL,'active',NULL,?,NULL,NULL,?,?,?)
        """,
        (
            graph["model_alias_id"],
            tenant_id,
            f"batch-alias-{marker}",
            environment_code,
            model_id,
            user_id,
            NOW,
            NOW,
            environment_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at,
          environment,description,last_used_at,created_by,disabled_at,disabled_by,
          project_id,application_id,environment_id
        ) VALUES(?,?,?,'mhv_batch',?,'active',?,?,?,NULL,NULL,?,NULL,NULL,?,NULL,?)
        """,
        (
            graph["virtual_key_id"],
            tenant_id,
            f"Batch key {marker}",
            marker * 64,
            NOW,
            NOW,
            environment_code,
            user_id,
            graph["project_id"],
            environment_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO uploaded_files(
          file_id,tenant_id,uploaded_by,source_request_id,source_trace_id,
          original_name_ciphertext,file_extension,media_type,size_bytes,
          content_fingerprint,status,virus_scan_status,dlp_scan_status,dlp_action,
          effective_dlp_rule_id,match_count,ruleset_fingerprint,storage_backend,
          original_object_key,failure_error_code,created_at,updated_at,completed_at
        ) VALUES(?,?,?,?,?,?,'txt','text/plain',64,?,'processing','not_configured',
                 'pending',NULL,NULL,0,NULL,NULL,NULL,NULL,?,?,NULL)
        """,
        (
            graph["file_id"],
            tenant_id,
            user_id,
            f"req_prd074_file_{marker}",
            f"trc_prd074_file_{marker}",
            b"x" * 64,
            "hmac-sha256:" + marker * 64,
            NOW,
            NOW,
        ),
    )
    connection.commit()
    return graph


def _insert_job(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    marker: str,
    *,
    virtual_key_id: str | None = None,
    estimated_cost_cny: str = "0",
) -> str:
    job_id = "bjob_" + marker * 32
    connection.execute(
        """
        INSERT INTO batch_jobs(
          batch_job_id,tenant_id,submitted_by_user_id,idempotency_key_hash,
          request_fingerprint,project_id,environment_id,model_alias_id,virtual_key_id,
          route_plan_json,name_ciphertext,operation,operation_spec_ciphertext,
          operation_template_version,priority,max_duration_seconds,max_attempts,
          max_output_tokens,task_count,status,estimated_input_tokens,
          estimated_output_tokens,estimated_cost_cny,posted_cost_cny,possible_cost_cny,
          reconciliation_pending,deadline_at,source_request_id,source_trace_id,
          created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'{}',?,'summarize',?,
                 'batch-operation-templates-v1','normal',300,2,256,1,'queued',
                 64,32,?,'0','0',0,?,?,?,?,?)
        """,
        (
            job_id,
            graph["tenant_id"],
            graph["user_id"],
            marker * 64,
            marker * 63 + "0",
            graph["project_id"],
            graph["environment_id"],
            graph["model_alias_id"],
            virtual_key_id or graph["virtual_key_id"],
            "enc:v1:" + marker * 40,
            "enc:v1:" + marker * 40,
            estimated_cost_cny,
            DEADLINE,
            f"req_prd074_job_{marker}",
            f"trc_prd074_job_{marker}",
            NOW,
            NOW,
        ),
    )
    connection.commit()
    return job_id


def _insert_task(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    job_id: str,
    marker: str,
) -> str:
    task_id = "btsk_" + marker * 32
    connection.execute(
        """
        INSERT INTO batch_tasks(
          batch_task_id,tenant_id,batch_job_id,file_id,task_index,priority_rank,
          status,attempt_count,next_attempt_at,lease_generation,result_available,
          created_at,updated_at
        ) VALUES(?,?,?,?,0,2,'queued',0,?,0,0,?,?)
        """,
        (task_id, graph["tenant_id"], job_id, graph["file_id"], NOW, NOW, NOW),
    )
    connection.commit()
    return task_id


def _insert_attempt(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    job_id: str,
    task_id: str,
    marker: str,
) -> str:
    attempt_id = "batm_" + marker * 32
    connection.execute(
        """
        INSERT INTO batch_attempts(
          batch_attempt_id,tenant_id,batch_job_id,batch_task_id,attempt_no,
          request_id,trace_id,status,execution_outcome,retry_safe,
          request_payload_ciphertext,request_payload_key_version,request_payload_hmac,
          route_plan_json,result_state,created_at,updated_at
        ) VALUES(?,?,?,?,1,?,?,'prepared','not_invoked',0,?,1,?,'{}','none',?,?)
        """,
        (
            attempt_id,
            graph["tenant_id"],
            job_id,
            task_id,
            f"req_prd074_attempt_{marker}",
            f"trc_prd074_attempt_{marker}",
            "enc:v1:" + marker * 40,
            marker * 64,
            NOW,
            NOW,
        ),
    )
    connection.commit()
    return attempt_id


def test_prd074_fresh_repeat_schema_integrity_and_rbac(tmp_path: Path) -> None:
    migrations = _migration_subset(tmp_path / "migrations-0067", 67)
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url, migrations_dir=migrations).applied == EXPECTED_VERSIONS
    repeat = apply_migrations(database_url, migrations_dir=migrations)
    assert repeat.applied == [] and repeat.skipped == EXPECTED_VERSIONS
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'batch_%'"
        )
    }
    assert tables == BATCH_TABLES
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name LIKE '%prd074'"
    ).fetchone()[0] >= 11
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name LIKE '%prd074%'"
    ).fetchone()[0] >= 17
    _assert_revision_10(connection)
    _assert_healthy(connection)
    connection.close()


def test_prd074_0066_upgrade_empty_down_and_reapply(
    tmp_path: Path,
    prd074_templates: dict[str, Path],
) -> None:
    database = tmp_path / "empty-round-trip.sqlite3"
    shutil.copy2(prd074_templates["database_0066"], database)
    database_url = _url(database)
    before = connect_database(database_url)
    permission_snapshot = before.execute(
        "SELECT rowid,* FROM permissions ORDER BY rowid"
    ).fetchall()
    before.close()
    assert apply_migrations(
        database_url,
        migrations_dir=prd074_templates["migrations_0067"],
    ).applied == ["0067"]
    connection = connect_database(database_url)
    tenant_id = _assert_revision_10(connection)
    connection.close()

    assert rollback_last_migration(
        database_url,
        migrations_dir=prd074_templates["migrations_0067"],
    ).rolled_back == "0067"
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0066"
    assert connection.execute("SELECT COUNT(*) FROM rbac_permission_templates").fetchone()[0] == 62
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_role_permission_templates"
    ).fetchone()[0] == 166
    assert connection.execute(
        "SELECT COUNT(*) FROM permissions WHERE tenant_id=? AND catalog_revision=9",
        (tenant_id,),
    ).fetchone()[0] == 62
    for role_code, expected_hash in REVISION_9_HASHES.items():
        assert connection.execute(
            "SELECT matrix_sha256 FROM roles WHERE tenant_id=? AND code=?",
            (tenant_id, role_code),
        ).fetchone()[0] == expected_hash
    assert connection.execute(
        "SELECT rowid,* FROM permissions ORDER BY rowid"
    ).fetchall() == permission_snapshot
    assert not {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE 'batch_%'"
        )
    }
    _assert_healthy(connection)
    connection.close()

    assert apply_migrations(
        database_url,
        migrations_dir=prd074_templates["migrations_0067"],
    ).applied == ["0067"]
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    _assert_revision_10(connection)
    _assert_healthy(connection)
    connection.close()


def test_prd074_owner_immutability_and_source_outbox(
    tmp_path: Path,
    prd074_templates: dict[str, Path],
) -> None:
    _, connection = _clone_and_upgrade(
        prd074_templates,
        tmp_path / "constraints.sqlite3",
    )
    graph_a = _resource_graph(connection, "a")
    graph_b = _resource_graph(connection, "b")
    job_a = _insert_job(connection, graph_a, "a")
    job_b = _insert_job(connection, graph_b, "b")
    task_a = _insert_task(connection, graph_a, job_a, "a")
    task_b = _insert_task(connection, graph_b, job_b, "b")

    seed_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? AND project_id IS NULL LIMIT 1",
        (graph_a["tenant_id"],),
    ).fetchone()
    assert seed_key is not None
    with pytest.raises(sqlite3.IntegrityError, match="batch_job_owner_mismatch"):
        _insert_job(connection, graph_a, "c", virtual_key_id=str(seed_key[0]))
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_job(connection, graph_a, "c", estimated_cost_cny="-1")
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_job(connection, graph_a, "d", estimated_cost_cny="0.0000001")
    connection.rollback()

    with pytest.raises(sqlite3.IntegrityError, match="batch_job_immutable"):
        connection.execute(
            "UPDATE batch_jobs SET route_plan_json='{\"changed\":true}' WHERE batch_job_id=?",
            (job_a,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="batch_task_immutable"):
        connection.execute(
            "UPDATE batch_tasks SET file_id=? WHERE batch_task_id=?",
            (graph_b["file_id"], task_a),
        )
    connection.rollback()

    with pytest.raises(sqlite3.IntegrityError, match="batch_attempt_owner_mismatch"):
        _insert_attempt(connection, graph_a, job_a, task_b, "b")
    connection.rollback()
    attempt_a = _insert_attempt(connection, graph_a, job_a, task_a, "a")
    with pytest.raises(sqlite3.IntegrityError, match="batch_attempt_immutable"):
        connection.execute(
            "UPDATE batch_attempts SET request_payload_hmac=? WHERE batch_attempt_id=?",
            ("f" * 64, attempt_a),
        )
    connection.rollback()

    budget_rule_id = "budget_prd074_growth_guard"
    hold_id = "bbhd_" + "d" * 32
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
          daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
          exhausted_action,status,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'project',?,'PRD074 hold guard',100,80,100,'block','active',?,?,?,?)
        """,
        (
            budget_rule_id,
            graph_a["tenant_id"],
            graph_a["environment_id"],
            graph_a["project_id"],
            graph_a["user_id"],
            graph_a["user_id"],
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO batch_budget_holds(
          batch_budget_hold_id,tenant_id,batch_job_id,budget_rule_id,environment_id,
          scope_type,scope_id,budget_date,reserved_cost_cny,remaining_cost_cny,
          reconciliation_cost_cny,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'project',?,'2026-08-08','1.000000','1.000000','0',
                 'active',?,?)
        """,
        (
            hold_id,
            graph_a["tenant_id"],
            job_a,
            budget_rule_id,
            graph_a["environment_id"],
            graph_a["project_id"],
            NOW,
            NOW,
        ),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="batch_budget_amount_growth"):
        connection.execute(
            """
            UPDATE batch_budget_holds SET remaining_cost_cny='1.000001'
            WHERE batch_budget_hold_id=?
            """,
            (hold_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="batch_budget_amount_growth"):
        connection.execute(
            """
            UPDATE batch_budget_holds SET remaining_cost_cny='0.500000',
              reconciliation_cost_cny='0.500001' WHERE batch_budget_hold_id=?
            """,
            (hold_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO batch_budget_holds(
              batch_budget_hold_id,tenant_id,batch_job_id,budget_rule_id,environment_id,
              scope_type,scope_id,budget_date,reserved_cost_cny,remaining_cost_cny,
              reconciliation_cost_cny,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'project',?,'2026-08-09','1','1','0.000001','active',?,?)
            """,
            (
                "bbhd_" + "e" * 32,
                graph_a["tenant_id"],
                job_a,
                budget_rule_id,
                graph_a["environment_id"],
                graph_a["project_id"],
                NOW,
                NOW,
            ),
        )
    connection.rollback()

    event_id = "bjev_" + "a" * 32
    connection.execute(
        """
        INSERT INTO batch_job_events(
          batch_job_event_id,tenant_id,batch_job_id,event_type,actor_type,actor_id,
          source_request_id,source_trace_id,payload_json,occurred_at
        ) VALUES(?,?,?,'batch_job.created','user',?,?,?,'{\"status\":\"queued\"}',?)
        """,
        (
            event_id,
            graph_a["tenant_id"],
            job_a,
            graph_a["user_id"],
            "req_prd074_event_a",
            "trc_prd074_event_a",
            NOW,
        ),
    )
    connection.commit()
    outbox = connection.execute(
        """
        SELECT tenant_id,dispatch_status,dispatch_attempts,next_attempt_at,created_at
        FROM batch_job_event_outbox WHERE batch_job_event_id=?
        """,
        (event_id,),
    ).fetchone()
    assert tuple(outbox) == (graph_a["tenant_id"], "pending", 0, NOW, NOW)
    with pytest.raises(sqlite3.IntegrityError, match="batch_outbox_state_invalid"):
        connection.execute(
            """
            UPDATE batch_job_event_outbox
            SET dispatch_status='dispatched',dispatch_attempts=1,next_attempt_at=NULL,
                dispatched_at=?,delivery_snapshot_frozen_at=?,updated_at=?
            WHERE batch_job_event_id=?
            """,
            (NOW, NOW, NOW, event_id),
        )
    connection.rollback()
    connection.execute(
        """
        UPDATE batch_job_event_outbox
        SET dispatch_status='processing',dispatch_attempts=1,next_attempt_at=NULL,
            claim_token=?,lease_owner_id='worker-prd074',lease_expires_at=?,updated_at=?
        WHERE batch_job_event_id=?
        """,
        (
            "a" * 32,
            "2026-08-08T00:01:31Z",
            "2026-08-08T00:00:01Z",
            event_id,
        ),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="batch_event_immutable"):
        connection.execute(
            "UPDATE batch_job_events SET payload_json='{}' WHERE batch_job_event_id=?",
            (event_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="batch_outbox_immutable"):
        connection.execute(
            "DELETE FROM batch_job_event_outbox WHERE batch_job_event_id=?",
            (event_id,),
        )
    connection.rollback()
    _assert_healthy(connection)
    connection.close()


def test_prd074_business_fact_blocks_down_atomically(
    tmp_path: Path,
    prd074_templates: dict[str, Path],
) -> None:
    database_url, connection = _clone_and_upgrade(
        prd074_templates,
        tmp_path / "business-down-guard.sqlite3",
    )
    graph = _resource_graph(connection, "c")
    job_id = _insert_job(connection, graph, "c")
    connection.close()
    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd074_templates["migrations_0067"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd074_down_guard_ok" in error.value.message
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    assert connection.execute(
        "SELECT COUNT(*) FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
        (graph["tenant_id"], job_id),
    ).fetchone()[0] == 1
    _assert_healthy(connection)
    connection.close()


def test_prd074_webhook_reference_blocks_down_atomically(
    tmp_path: Path,
    prd074_templates: dict[str, Path],
) -> None:
    database_url, connection = _clone_and_upgrade(
        prd074_templates,
        tmp_path / "external-down-guard.sqlite3",
    )
    tenant_id, user_id = connection.execute(
        "SELECT tenant_id,user_id FROM users ORDER BY user_id LIMIT 1"
    ).fetchone()
    endpoint_id = "whe_prd074_batch_created"
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'https://example.invalid/prd074','[\"batch_job.created\"]',
                 'enc:v1:test-placeholder','whsec_test','fpr_prd074_fixture',
                 'active',1000,3,?,?,?,?)
        """,
        (endpoint_id, tenant_id, "Batch created", user_id, user_id, NOW, NOW),
    )
    connection.commit()
    connection.close()
    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd074_templates["migrations_0067"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd074_down_guard_ok" in error.value.message
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    assert connection.execute(
        "SELECT COUNT(*) FROM webhook_endpoints WHERE webhook_endpoint_id=?",
        (endpoint_id,),
    ).fetchone()[0] == 1
    _assert_healthy(connection)
    connection.close()
