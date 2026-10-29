"""PRD-082 migration, tenant ownership, approval, and MCP state guards."""

from __future__ import annotations

import hashlib
import json
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
from metahub_shared.rbac import permission_matrix_sha256

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
NOW = "2026-08-16T00:00:00Z"
NOW_1 = "2026-08-16T00:00:01Z"
NOW_2 = "2026-08-16T00:00:02Z"
NOW_3 = "2026-08-16T00:00:03Z"
FUTURE = "2026-08-16T01:00:00Z"

MCP_TABLES = (
    "mcp_servers",
    "mcp_server_credentials",
    "mcp_server_operations",
    "mcp_server_sync_jobs",
    "mcp_tools",
    "mcp_tool_role_grants",
    "mcp_tool_user_grants",
    "mcp_invocations",
    "mcp_invocation_attempts",
    "mcp_invocation_events",
    "mcp_invocation_event_outbox",
    "mcp_scheduler_tenants",
)


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _id(prefix: str, marker: str) -> str:
    return prefix + _digest(marker)[:32]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd082_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("prd082-migration-templates")
    migrations_0074 = _migration_subset(root / "migrations-0074", 74)
    migrations_0075 = _migration_subset(root / "migrations-0075", 75)
    database = root / "seeded-0074.sqlite3"
    database_url = _url(database)
    result = apply_migrations(database_url, migrations_dir=migrations_0074)
    assert len(result.applied) == 74 and result.applied[-1] == "0074"
    apply_seed_data(database_url)
    return {
        "database_0074": database,
        "migrations_0074": migrations_0074,
        "migrations_0075": migrations_0075,
    }


def _clone_0074(templates: dict[str, Path], destination: Path) -> str:
    shutil.copy2(templates["database_0074"], destination)
    return _url(destination)


def _apply_0075(templates: dict[str, Path], database_url: str) -> None:
    result = apply_migrations(database_url, migrations_dir=templates["migrations_0075"])
    assert result.applied == ["0075"]


def _head(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE status='applied' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return str(row[0])


def _assert_healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _assert_catalog(connection: sqlite3.Connection, revision: int) -> None:
    permission_count = 84 if revision == 17 else 79
    relation_count = 221 if revision == 17 else 208
    assert tuple(
        connection.execute(
            "SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision) "
            "FROM rbac_permission_templates"
        ).fetchone()
    ) == (permission_count, revision, revision)
    assert (
        connection.execute("SELECT COUNT(*) FROM rbac_role_permission_templates").fetchone()[0]
        == relation_count
    )


def _rows_snapshot(
    connection: sqlite3.Connection, tables: tuple[str, ...]
) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    for table in tables:
        rows.extend(
            (table, *tuple(row))
            for row in connection.execute(f'SELECT rowid,* FROM "{table}" ORDER BY rowid')
        )
    return tuple(rows)


def _without_role_revisions(
    rows: tuple[tuple[object, ...], ...]
) -> tuple[tuple[object, ...], ...]:
    normalized: list[tuple[object, ...]] = []
    for row in rows:
        if row[0] == "roles":
            values = list(row)
            values[10] = None
            normalized.append(tuple(values))
        else:
            normalized.append(row)
    return tuple(normalized)


def _schema_snapshot(
    connection: sqlite3.Connection, tables: tuple[str, ...]
) -> tuple[tuple[object, ...], ...]:
    placeholders = ",".join("?" for _ in tables)
    rows: list[tuple[object, ...]] = []
    for row in connection.execute(
            f"SELECT type,name,tbl_name,sql FROM sqlite_schema "
            f"WHERE tbl_name IN({placeholders}) ORDER BY type,name",
            tables,
    ):
        values = list(row)
        if isinstance(values[3], str):
            for table in tables:
                values[3] = values[3].replace(
                    f'CREATE TABLE "{table}"', f"CREATE TABLE {table}"
                )
        rows.append(tuple(values))
    return tuple(rows)


def _identity(connection: sqlite3.Connection) -> dict[str, str]:
    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,user.email,user.role,
               developer.role_id,provider.provider_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN roles developer ON developer.tenant_id=tenant.tenant_id
                            AND developer.code='developer'
        JOIN providers provider ON provider.tenant_id=tenant.tenant_id
        WHERE tenant.slug='default' LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return {
        "tenant_id": str(row[0]),
        "user_id": str(row[1]),
        "email": str(row[2]),
        "role": str(row[3]),
        "developer_role_id": str(row[4]),
        "provider_id": str(row[5]),
    }


def _insert_second_user(connection: sqlite3.Connection, tenant_id: str) -> str:
    user_id = "user_prd082_decider"
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,
          created_at,updated_at,password_changed_at
        ) VALUES(?,?,'prd082-decider@example.invalid','PRD082 Decider',
                 'not-a-runtime-credential','security_admin','active',?,?,?)
        """,
        (user_id, tenant_id, NOW, NOW, NOW),
    )
    return user_id


def _insert_legacy_audit(connection: sqlite3.Connection) -> None:
    identity = _identity(connection)
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
          action,object_type,object_id,object_name,before_summary_json,
          after_summary_json,changed_fields_json,client_ip,user_agent,
          request_id,trace_id,created_at
        ) VALUES('aud_prd082_legacy',?,?,?,?,'provider.updated','provider',?,NULL,
                 '{}','{}','[]',NULL,NULL,'req_prd082_legacy','trc_prd082_legacy',?)
        """,
        (
            identity["tenant_id"],
            identity["user_id"],
            identity["email"],
            identity["role"],
            identity["provider_id"],
            NOW,
        ),
    )


def _customize_role_matrix(
    connection: sqlite3.Connection, role_code: str = "finance_admin"
) -> tuple[str, tuple[tuple[object, ...], ...]]:
    role = connection.execute(
        "SELECT role_id,tenant_id FROM roles WHERE code=? ORDER BY tenant_id LIMIT 1",
        (role_code,),
    ).fetchone()
    assert role is not None
    rows = connection.execute(
        """
        SELECT permission.code,relation.role_permission_id
        FROM role_permissions relation JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=? AND relation.role_id=? ORDER BY permission.code
        """,
        (role["tenant_id"], role["role_id"]),
    ).fetchall()
    assert rows
    removed_code = str(rows[0][0])
    assert connection.execute(
        "DELETE FROM role_permissions WHERE role_permission_id=?", (rows[0][1],)
    ).rowcount == 1
    remaining = tuple(str(row[0]) for row in rows[1:])
    assert connection.execute(
        "UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=? "
        "WHERE tenant_id=? AND role_id=?",
        (permission_matrix_sha256(remaining), role["tenant_id"], role["role_id"]),
    ).rowcount == 1
    connection.commit()
    relations = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT relation.rowid,relation.*,permission.code
            FROM role_permissions relation JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.role_id=? ORDER BY relation.rowid
            """,
            (role["role_id"],),
        )
    )
    assert removed_code not in {str(row[-1]) for row in relations}
    return str(role["role_id"]), relations


def _insert_mapping(
    connection: sqlite3.Connection, table: str, values: dict[str, object]
) -> None:
    columns = tuple(values)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )


def _audit_changed_fields(
    before: dict[str, object], after: dict[str, object]
) -> list[str]:
    return sorted(
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    )


def _insert_mcp_audit(
    connection: sqlite3.Connection,
    actor: dict[str, object],
    marker: str,
    *,
    action: str,
    object_type: str,
    object_id: object,
    before: dict[str, object],
    after: dict[str, object],
    request_id: str,
    trace_id: str,
    created_at: str,
    actor_user_id: object | None = None,
    actor_email: object | None = None,
    actor_role: object | None = None,
    changed_fields: list[str] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,
          object_type,object_id,object_name,before_summary_json,after_summary_json,
          changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?,NULL,NULL,?,?,?)
        """,
        (
            _id("aud_", marker),
            actor["tenant_id"],
            actor["user_id"] if actor_user_id is None else actor_user_id,
            actor["email"] if actor_email is None else actor_email,
            actor["role"] if actor_role is None else actor_role,
            action,
            object_type,
            object_id,
            _json(before),
            _json(after),
            _json(
                _audit_changed_fields(before, after)
                if changed_fields is None
                else changed_fields
            ),
            request_id,
            trace_id,
            created_at,
        ),
    )


def _insert_server_tool(
    connection: sqlite3.Connection,
    marker: str,
    *,
    risk_level: str = "standard",
    add_grant: bool = True,
    activate: bool = True,
) -> dict[str, object]:
    identity = _identity(connection)
    server_id = _id("mcps_", f"server-{marker}")
    credential_id = _id("mcpc_", f"credential-{marker}-1")
    tool_id = _id("mcpt_", f"tool-{marker}")
    schema_hash = _digest(f"schema-{marker}")
    connection.execute(
        """
        INSERT INTO mcp_servers(
          mcp_server_id,tenant_id,name,transport,endpoint_ciphertext,
          endpoint_encryption_key_version,endpoint_hmac,endpoint_hmac_key_version,
          endpoint_scheme,endpoint_host_hint,endpoint_has_path,active_credential_revision,
          protocol_version,capabilities_sha256,status,state_revision,created_by,updated_by,
          created_at,updated_at
        ) VALUES(?,?,'safe-server','streamable_http','enc_endpoint',1,?,1,
                 'https','mcp.example.invalid',1,NULL,NULL,NULL,'pending_sync',1,?,?,?,?)
        """,
        (
            server_id,
            identity["tenant_id"],
            _digest(f"endpoint-{marker}"),
            identity["user_id"],
            identity["user_id"],
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO mcp_server_credentials(
          mcp_server_credential_id,tenant_id,mcp_server_id,credential_revision,
          auth_type,credential_ciphertext,credential_encryption_key_version,
          credential_hmac,credential_hmac_key_version,status,probe_operation_id,
          created_by,created_at,activated_at,rejected_at
        ) VALUES(?,?,?,1,'none',NULL,NULL,NULL,NULL,'candidate',NULL,?,?,NULL,NULL)
        """,
        (credential_id, identity["tenant_id"], server_id, identity["user_id"], NOW),
    )
    connection.execute(
        "UPDATE mcp_server_credentials SET status='active',activated_at=? "
        "WHERE mcp_server_credential_id=?",
        (NOW_1, credential_id),
    )
    connection.execute(
        """
        UPDATE mcp_servers SET active_credential_revision=1,protocol_version='2025-06-18',
          capabilities_sha256=?,status='active',state_revision=2,updated_at=?
        WHERE mcp_server_id=?
        """,
        (_digest(f"capabilities-{marker}"), NOW_1, server_id),
    )
    approval_required = 1 if risk_level == "high" else 0
    connection.execute(
        """
        INSERT INTO mcp_tools(
          mcp_tool_id,tenant_id,mcp_server_id,remote_name,title,description,
          input_schema_original_json,input_schema_original_sha256,
          input_schema_effective_json,input_schema_effective_sha256,
          output_schema_original_json,output_schema_original_sha256,
          output_schema_effective_json,output_schema_effective_sha256,
          annotations_json,annotations_sha256,schema_revision,policy_revision,
          risk_level,approval_required,approval_expires_seconds,
          required_permission_codes_json,result_redaction_pointers_json,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'safe_tool','Safe Tool',NULL,'{}',?,'{}',?,NULL,NULL,NULL,NULL,
                 '{}',?,1,1,?,?,900,'[]','[]','review_required',?,?,?,?)
        """,
        (
            tool_id,
            identity["tenant_id"],
            server_id,
            schema_hash,
            schema_hash,
            _digest(f"annotations-{marker}"),
            risk_level,
            approval_required,
            identity["user_id"],
            identity["user_id"],
            NOW,
            NOW,
        ),
    )
    grant_id = _id("mcprg_", f"role-grant-{marker}")
    if add_grant:
        connection.execute(
            """
            INSERT INTO mcp_tool_role_grants(
              mcp_tool_role_grant_id,tenant_id,mcp_tool_id,role_id,created_by,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                grant_id,
                identity["tenant_id"],
                tool_id,
                identity["developer_role_id"],
                identity["user_id"],
                NOW,
            ),
        )
    if activate:
        connection.execute(
            "UPDATE mcp_tools SET status='active',policy_revision=2,updated_at=? "
            "WHERE mcp_tool_id=?",
            (NOW_1, tool_id),
        )
    connection.commit()
    return {
        **identity,
        "server_id": server_id,
        "credential_id": credential_id,
        "tool_id": tool_id,
        "schema_hash": schema_hash,
        "grant_id": grant_id,
        "risk_level": risk_level,
        "approval_required": approval_required,
        "server_state_revision": 2,
        "credential_revision": 1,
        "schema_revision": 1,
        "policy_revision": 2 if activate else 1,
    }


def _invocation_values(
    graph: dict[str, object],
    marker: str,
    *,
    status: str = "queued",
    input_dlp_status: str = "clean",
    input_dlp_match_count: int = 0,
    approval_task_id: str | None = None,
) -> dict[str, object]:
    terminal = status in {"blocked", "failed"}
    error_code = None
    if status == "blocked":
        error_code = "mcp.arguments_blocked"
    elif status == "failed":
        error_code = "mcp.unavailable"
    approval_required = int(graph["approval_required"])
    return {
        "mcp_invocation_id": _id("mcpi_", f"invocation-{marker}"),
        "tenant_id": graph["tenant_id"],
        "mcp_server_id": graph["server_id"],
        "mcp_tool_id": graph["tool_id"],
        "requested_by": graph["user_id"],
        "request_id": f"req_prd082_{marker}",
        "trace_id": f"trc_prd082_{marker}",
        "idempotency_key_hmac": _digest(f"idempotency-{marker}"),
        "idempotency_key_hmac_version": 1,
        "intent_hmac": _digest(f"intent-{marker}"),
        "intent_hmac_version": 1,
        "server_state_revision": graph["server_state_revision"],
        "credential_revision": graph["credential_revision"],
        "schema_revision": graph["schema_revision"],
        "policy_revision": graph["policy_revision"],
        "policy_fingerprint": _digest(f"policy-{marker}"),
        "input_schema_original_sha256": graph["schema_hash"],
        "input_schema_effective_sha256": graph["schema_hash"],
        "output_schema_original_sha256": None,
        "output_schema_effective_sha256": None,
        "risk_level": graph["risk_level"],
        "approval_required": approval_required,
        "required_permissions_sha256": _digest(f"permissions-{marker}"),
        "grant_sha256": _digest(f"grants-{marker}"),
        "result_redaction_pointers_json": "[]",
        "arguments_ciphertext": "enc_arguments",
        "arguments_encryption_key_version": 1,
        "arguments_bytes": 2,
        "arguments_hmac": _digest(f"arguments-{marker}"),
        "arguments_hmac_key_version": 1,
        "approval_task_id": approval_task_id,
        "approval_deadline_at": FUTURE if approval_task_id is not None else None,
        "status": status,
        "state_revision": 1,
        "input_dlp_status": input_dlp_status,
        "input_dlp_match_count": input_dlp_match_count,
        "input_dlp_rule_id": None,
        "input_dlp_ruleset_hmac": _digest(f"input-rules-{marker}"),
        "input_dlp_ruleset_hmac_key_version": 1,
        "input_dlp_scanned_at": NOW,
        "output_dlp_status": None,
        "output_dlp_match_count": None,
        "output_dlp_rule_id": None,
        "output_dlp_ruleset_hmac": None,
        "output_dlp_ruleset_hmac_key_version": None,
        "output_dlp_scanned_at": None,
        "result_ciphertext": None,
        "result_encryption_key_version": None,
        "result_hmac": None,
        "result_hmac_key_version": None,
        "result_bytes": None,
        "result_redaction_count": None,
        "probe_attempt_count": 0,
        "call_attempt_count": 0,
        "request_bytes": 2,
        "response_bytes": 0,
        "duration_ms": 0 if terminal else None,
        "execution_status": "not_called",
        "invocation_count": 1,
        "tool_call_count": 0,
        "cost_status": "not_applicable",
        "cost_micros": None,
        "error_code": error_code,
        "lease_generation": 0,
        "lease_owner_id": None,
        "lease_token": None,
        "lease_expires_at": None,
        "next_attempt_at": NOW if status == "queued" else None,
        "created_at": NOW,
        "queued_at": NOW if status == "queued" else None,
        "started_at": None,
        "call_started_at": None,
        "completed_at": NOW if terminal else None,
        "updated_at": NOW,
    }


def _claim_probe_attempt(
    connection: sqlite3.Connection,
    graph: dict[str, object],
    marker: str,
    *,
    expires_at: str,
) -> tuple[str, str]:
    invocation = _invocation_values(graph, marker)
    _insert_mapping(connection, "mcp_invocations", invocation)
    invocation_id = str(invocation["mcp_invocation_id"])
    connection.execute(
        """
        UPDATE mcp_invocations SET status='probing',state_revision=2,
          probe_attempt_count=1,lease_generation=1,lease_owner_id=?,lease_token=?,
          lease_expires_at=?,next_attempt_at=NULL,started_at=?,updated_at=?
        WHERE mcp_invocation_id=?
        """,
        (
            f"probe-worker-{marker}",
            _digest(f"probe-token-{marker}"),
            expires_at,
            NOW_1,
            NOW_1,
            invocation_id,
        ),
    )
    attempt_id = _id("mcpa_", f"probe-attempt-{marker}")
    connection.execute(
        """
        INSERT INTO mcp_invocation_attempts(
          mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
          attempt_number,status,lease_generation,error_code,request_bytes,response_bytes,
          started_at,call_started_at,completed_at,duration_ms
        ) VALUES(?,?,?,'probe',1,'processing',1,NULL,NULL,NULL,?,NULL,NULL,NULL)
        """,
        (attempt_id, graph["tenant_id"], invocation_id, NOW_1),
    )
    return invocation_id, attempt_id


def _make_calling_invocation(
    connection: sqlite3.Connection,
    graph: dict[str, object],
    marker: str,
    *,
    lease_expires_at: str = "2026-08-16T00:10:02Z",
) -> str:
    invocation = _invocation_values(graph, marker)
    _insert_mapping(connection, "mcp_invocations", invocation)
    invocation_id = str(invocation["mcp_invocation_id"])
    connection.execute(
        """
        UPDATE mcp_invocations SET status='probing',state_revision=2,
          probe_attempt_count=1,lease_generation=1,lease_owner_id=?,lease_token=?,
          lease_expires_at='2026-08-16T00:10:01Z',next_attempt_at=NULL,
          started_at=?,updated_at=? WHERE mcp_invocation_id=?
        """,
        (
            f"call-worker-{marker}",
            _digest(f"call-probe-token-{marker}"),
            NOW_1,
            NOW_1,
            invocation_id,
        ),
    )
    connection.execute(
        """
        UPDATE mcp_invocations SET status='calling',state_revision=3,
          call_attempt_count=1,call_started_at=?,execution_status='unknown',
          tool_call_count=NULL,cost_status='unknown',lease_generation=2,
          lease_token=?,lease_expires_at=?,updated_at=? WHERE mcp_invocation_id=?
        """,
        (
            NOW_2,
            _digest(f"call-token-{marker}"),
            lease_expires_at,
            NOW_2,
            invocation_id,
        ),
    )
    return invocation_id


def _approval_values(
    invocation: dict[str, object], marker: str, *, created_by_id: str | None = None
) -> dict[str, object]:
    return {
        "approval_task_id": invocation["approval_task_id"],
        "tenant_id": invocation["tenant_id"],
        "source_type": "mcp",
        "source_id": invocation["mcp_invocation_id"],
        "target_type": "mcp_tool_invocation",
        "target_id": invocation["mcp_invocation_id"],
        "target_action": "invoke",
        "target_reason_code": None,
        "target_version": ":".join(
            str(invocation[key])
            for key in (
                "server_state_revision",
                "credential_revision",
                "schema_revision",
                "policy_revision",
            )
        ),
        "target_fingerprint": invocation["policy_fingerprint"],
        "risk_level": "high",
        "status": "pending",
        "resolution_reason": None,
        "state_revision": 1,
        "writeback_status": "pending",
        "required_permission_code": "mcp.approve",
        "created_by_type": "user",
        "created_by_id": created_by_id or invocation["requested_by"],
        "mcp_invocation_id": invocation["mcp_invocation_id"],
        "decided_by_user_id": None,
        "decided_at": None,
        "expires_at": FUTURE,
        "create_hmac_key_version": 1,
        "create_idempotency_key_hmac": _digest(f"approval-key-{marker}"),
        "create_intent_hmac": _digest(f"approval-intent-{marker}"),
        "source_request_id": invocation["request_id"],
        "source_trace_id": invocation["trace_id"],
        "decision_request_id": None,
        "decision_trace_id": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_prd082_empty_zero_to_75_repeat_down_reup(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _url(tmp_path / "fresh-0075.sqlite3")
    result = apply_migrations(
        database_url, migrations_dir=prd082_templates["migrations_0075"]
    )
    assert result.applied == [f"{version:04d}" for version in range(1, 76)]
    repeat = apply_migrations(
        database_url, migrations_dir=prd082_templates["migrations_0075"]
    )
    assert repeat.applied == [] and repeat.skipped[-1] == "0075"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0075"
        _assert_catalog(connection, 17)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' "
            f"AND name IN({','.join('?' for _ in MCP_TABLES)})",
            MCP_TABLES,
        ).fetchone()[0] == len(MCP_TABLES)
        assert connection.execute(
            "SELECT COUNT(*) FROM pragma_foreign_key_list('mcp_invocations') "
            "WHERE \"table\"='mcp_tools' "
            "AND \"from\" IN('tenant_id','mcp_server_id','mcp_tool_id')"
        ).fetchone()[0] == 3
        _assert_healthy(connection)

    down = rollback_last_migration(
        database_url,
        migrations_dir=prd082_templates["migrations_0075"],
        expected_version="0075",
    )
    assert down.rolled_back == "0075"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0074"
        _assert_catalog(connection, 16)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'mcp_%'"
        ).fetchone()[0] == 0
        _assert_healthy(connection)

    assert apply_migrations(
        database_url, migrations_dir=prd082_templates["migrations_0075"]
    ).applied == ["0075"]
    with connect_database(database_url) as connection:
        assert _head(connection) == "0075"
        _assert_catalog(connection, 17)
        _assert_healthy(connection)


def test_prd082_seeded_roundtrip_preserves_rows_custom_matrix_and_schema(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0074(prd082_templates, tmp_path / "seeded-roundtrip.sqlite3")
    preserved_tables = (
        "rbac_permission_templates",
        "rbac_role_permission_templates",
        "roles",
        "permissions",
        "role_permissions",
        "approval_tasks",
        "approval_decisions",
        "approval_writebacks",
        "audit_logs",
        "webhook_endpoints",
        "webhook_deliveries",
    )
    schema_tables = (
        "approval_tasks",
        "approval_decisions",
        "approval_writebacks",
        "audit_logs",
    )
    with connect_database(database_url) as connection:
        _insert_legacy_audit(connection)
        custom_role_id, custom_relations = _customize_role_matrix(connection)
        before_rows = _rows_snapshot(connection, preserved_tables)
        role_revisions_before = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT role_id,permission_revision FROM roles"
            )
        }
        before_schema = _schema_snapshot(connection, schema_tables)
        _assert_healthy(connection)

    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        _assert_catalog(connection, 17)
        assert tuple(
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
        ) == custom_relations
        assert connection.execute(
            """
            SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.role_id=? AND permission.code LIKE 'mcp.%'
            """,
            (custom_role_id,),
        ).fetchone()[0] == 0
        expected = {
            "admin": 5,
            "developer": 3,
            "viewer": 0,
            "finance_admin": 0,
            "security_admin": 3,
            "ops_admin": 2,
        }
        actual = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT role.code,COUNT(permission.code)
                FROM roles role LEFT JOIN role_permissions relation
                  ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
                LEFT JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                 AND permission.code LIKE 'mcp.%'
                GROUP BY role.code
                """,
            )
        }
        assert actual == expected
        assert tuple(
            tuple(row) for row in connection.execute("SELECT rowid,* FROM audit_logs")
        ) == tuple(
            row[1:] for row in before_rows if row[0] == "audit_logs"
        )
        _assert_healthy(connection)

    assert rollback_last_migration(
        database_url,
        migrations_dir=prd082_templates["migrations_0075"],
        expected_version="0075",
    ).rolled_back == "0075"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0074"
        after_rows = _rows_snapshot(connection, preserved_tables)
        assert _without_role_revisions(after_rows) == _without_role_revisions(before_rows)
        role_revisions_after = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT role_id,permission_revision FROM roles"
            )
        }
        for role_id, before_revision in role_revisions_before.items():
            expected_delta = 0 if role_id == custom_role_id else (
                2
                if connection.execute(
                    "SELECT code FROM roles WHERE role_id=?", (role_id,)
                ).fetchone()[0]
                in {"admin", "developer", "security_admin", "ops_admin"}
                else 0
            )
            assert role_revisions_after[role_id] == before_revision + expected_delta
        assert _schema_snapshot(connection, schema_tables) == before_schema
        _assert_catalog(connection, 16)
        _assert_healthy(connection)

    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        assert _head(connection) == "0075"
        _assert_healthy(connection)


def test_prd082_server_credential_rotation_and_operation_guards(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0074(prd082_templates, tmp_path / "server-guards.sqlite3")
    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        graph = _insert_server_tool(connection, "server")
        with pytest.raises(sqlite3.IntegrityError, match="mcp_server_transition_invalid"):
            connection.execute(
                "UPDATE mcp_servers SET endpoint_ciphertext='changed',state_revision=3,"
                "updated_at=? WHERE mcp_server_id=?",
                (NOW_2, graph["server_id"]),
            )
        connection.rollback()

        candidate_id = _id("mcpc_", "credential-server-2")
        connection.execute(
            """
            INSERT INTO mcp_server_credentials(
              mcp_server_credential_id,tenant_id,mcp_server_id,credential_revision,
              auth_type,credential_ciphertext,credential_encryption_key_version,
              credential_hmac,credential_hmac_key_version,status,probe_operation_id,
              created_by,created_at,activated_at,rejected_at
            ) VALUES(?,?,?,2,'none',NULL,NULL,NULL,NULL,'candidate',NULL,?,?,NULL,NULL)
            """,
            (
                candidate_id,
                graph["tenant_id"],
                graph["server_id"],
                graph["user_id"],
                NOW_1,
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="mcp_server_transition_invalid"):
            connection.execute(
                "UPDATE mcp_servers SET active_credential_revision=2,state_revision=3,"
                "updated_at=? WHERE mcp_server_id=?",
                (NOW_2, graph["server_id"]),
            )
        connection.rollback()
        connection.execute(
            "UPDATE mcp_server_credentials SET status='active',activated_at=? "
            "WHERE mcp_server_credential_id=?",
            (NOW_2, candidate_id),
        )
        connection.execute(
            "UPDATE mcp_servers SET active_credential_revision=2,state_revision=3,"
            "updated_at=? WHERE mcp_server_id=?",
            (NOW_2, graph["server_id"]),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM mcp_server_credentials WHERE mcp_server_id=? "
            "AND status='active'",
            (graph["server_id"],),
        ).fetchone()[0] == 2

        for marker, operation_type in (("update", "update"), ("enable", "enable")):
            connection.execute(
                """
                INSERT INTO mcp_server_operations(
                  mcp_server_operation_id,tenant_id,mcp_server_id,operation_type,
                  idempotency_key_hmac,idempotency_key_hmac_version,intent_hmac,
                  intent_hmac_version,actor_user_id,start_server_revision,
                  start_credential_revision,candidate_credential_revision,status,error_code,
                  request_id,trace_id,created_at,completed_at,updated_at
                ) VALUES(?,?,?,?,?,1,?,1,?,3,2,NULL,'queued',NULL,?,?,?,NULL,?)
                """,
                (
                    _id("mcpo_", f"operation-{marker}"),
                    graph["tenant_id"],
                    graph["server_id"],
                    operation_type,
                    _digest(f"operation-key-{marker}"),
                    _digest(f"operation-intent-{marker}"),
                    graph["user_id"],
                    f"req_prd082_operation_{marker}",
                    f"trc_prd082_operation_{marker}",
                    NOW_3,
                    NOW_3,
                ),
            )
        connection.commit()
        assert connection.execute(
            "SELECT group_concat(operation_type,',') FROM "
            "(SELECT operation_type FROM mcp_server_operations ORDER BY operation_type)"
        ).fetchone()[0] == "enable,update"

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT OR REPLACE INTO mcp_servers SELECT * FROM mcp_servers "
                "WHERE mcp_server_id=?",
                (graph["server_id"],),
            )
        connection.rollback()
        _assert_healthy(connection)


def test_prd082_sync_job_claim_takeover_and_terminal_guards(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0074(prd082_templates, tmp_path / "sync-job-guards.sqlite3")
    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        graph = _insert_server_tool(connection, "sync-job")
        operation_id = _id("mcpo_", "sync-job-operation")
        job_id = _id("mcpj_", "sync-job")
        connection.execute(
            """
            INSERT INTO mcp_server_operations(
              mcp_server_operation_id,tenant_id,mcp_server_id,operation_type,
              idempotency_key_hmac,idempotency_key_hmac_version,intent_hmac,
              intent_hmac_version,actor_user_id,start_server_revision,
              start_credential_revision,candidate_credential_revision,status,error_code,
              request_id,trace_id,created_at,completed_at,updated_at
            ) VALUES(?,?,?,'refresh',?,1,?,1,?,2,1,NULL,'queued',NULL,
                     'req_prd082_sync_job','trc_prd082_sync_job',?,NULL,?)
            """,
            (
                operation_id,
                graph["tenant_id"],
                graph["server_id"],
                _digest("sync-job-operation-key"),
                _digest("sync-job-operation-intent"),
                graph["user_id"],
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO mcp_server_sync_jobs(
              mcp_server_sync_job_id,tenant_id,mcp_server_id,mcp_server_operation_id,
              candidate_credential_revision,job_type,status,attempt_count,lease_generation,
              lease_owner_id,lease_token,lease_expires_at,next_attempt_at,deadline_at,
              error_code,created_at,started_at,completed_at,updated_at
            ) VALUES(?,?,?,?,NULL,'discover','pending',0,0,NULL,NULL,NULL,?,?,NULL,
                     ?,NULL,NULL,?)
            """,
            (job_id, graph["tenant_id"], graph["server_id"], operation_id, NOW, FUTURE, NOW, NOW),
        )
        first_token = _digest("sync-job-token-1")
        first_expiry = "2026-08-16T00:01:01Z"
        connection.execute(
            """
            UPDATE mcp_server_sync_jobs SET status='processing',attempt_count=1,
              lease_generation=1,lease_owner_id='sync-worker-1',lease_token=?,
              lease_expires_at=?,next_attempt_at=NULL,started_at=?,updated_at=?
            WHERE mcp_server_sync_job_id=?
            """,
            (first_token, first_expiry, NOW_1, NOW_1, job_id),
        )
        connection.execute(
            "UPDATE mcp_server_operations SET status='processing',updated_at=? "
            "WHERE mcp_server_operation_id=?",
            (NOW_1, operation_id),
        )

        takeover_at = "2026-08-16T00:01:02Z"
        second_expiry = "2026-08-16T00:02:02Z"
        second_token = _digest("sync-job-token-2")
        with pytest.raises(sqlite3.IntegrityError, match="mcp_sync_job_transition_invalid"):
            connection.execute(
                """
                UPDATE mcp_server_sync_jobs SET status='succeeded',lease_generation=2,
                  lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                  next_attempt_at=NULL,completed_at=?,updated_at=?
                WHERE mcp_server_sync_job_id=?
                """,
                (takeover_at, takeover_at, job_id),
            )
        connection.execute(
            """
            UPDATE mcp_server_sync_jobs SET status='processing',attempt_count=2,
              lease_generation=2,lease_owner_id='sync-worker-2',lease_token=?,
              lease_expires_at=?,updated_at=? WHERE mcp_server_sync_job_id=?
            """,
            (second_token, second_expiry, takeover_at, job_id),
        )
        assert connection.execute(
            """
            UPDATE mcp_server_sync_jobs SET updated_at=updated_at
            WHERE mcp_server_sync_job_id=? AND lease_generation=1 AND lease_token=?
            """,
            (job_id, first_token),
        ).rowcount == 0
        with pytest.raises(sqlite3.IntegrityError, match="mcp_sync_job_transition_invalid"):
            connection.execute(
                """
                UPDATE mcp_server_sync_jobs SET status='processing',attempt_count=3,
                  lease_generation=3,lease_owner_id='sync-worker-3',lease_token=?,
                  lease_expires_at='2026-08-16T00:02:03Z',updated_at='2026-08-16T00:01:03Z'
                WHERE mcp_server_sync_job_id=?
                """,
                (_digest("sync-job-early-token"), job_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="mcp_sync_job_transition_invalid"):
            connection.execute(
                """
                UPDATE mcp_server_sync_jobs SET status='succeeded',lease_generation=3,
                  lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                  next_attempt_at=NULL,completed_at=?,updated_at=?
                WHERE mcp_server_sync_job_id=?
                """,
                (second_expiry, second_expiry, job_id),
            )

        terminal_at = "2026-08-16T00:01:03Z"
        connection.execute("SAVEPOINT prd082_sync_terminal")
        connection.execute(
            """
            UPDATE mcp_server_sync_jobs SET status='succeeded',lease_generation=3,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=NULL,completed_at=?,updated_at=?
            WHERE mcp_server_sync_job_id=?
            """,
            (terminal_at, terminal_at, job_id),
        )
        connection.execute("ROLLBACK TO prd082_sync_terminal")
        connection.execute(
            """
            UPDATE mcp_server_sync_jobs SET status='failed',lease_generation=3,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=NULL,error_code='mcp.sync_failed',completed_at=?,updated_at=?
            WHERE mcp_server_sync_job_id=?
            """,
            (terminal_at, terminal_at, job_id),
        )
        connection.execute("ROLLBACK TO prd082_sync_terminal")
        connection.execute("RELEASE prd082_sync_terminal")

        connection.execute(
            """
            UPDATE mcp_server_sync_jobs SET status='pending',lease_generation=3,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=?,updated_at=? WHERE mcp_server_sync_job_id=?
            """,
            (terminal_at, terminal_at, job_id),
        )
        third_token = _digest("sync-job-token-3")
        third_expiry = "2026-08-16T00:02:03Z"
        connection.execute(
            """
            UPDATE mcp_server_sync_jobs SET status='processing',attempt_count=3,
              lease_generation=4,lease_owner_id='sync-worker-3',lease_token=?,
              lease_expires_at=?,next_attempt_at=NULL,updated_at=?
            WHERE mcp_server_sync_job_id=?
            """,
            (third_token, third_expiry, terminal_at, job_id),
        )
        completed_at = "2026-08-16T00:01:04Z"
        connection.execute(
            """
            UPDATE mcp_server_sync_jobs SET status='succeeded',lease_generation=5,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=NULL,completed_at=?,updated_at=?
            WHERE mcp_server_sync_job_id=?
            """,
            (completed_at, completed_at, job_id),
        )
        connection.execute(
            """
            UPDATE mcp_server_operations SET status='succeeded',completed_at=?,updated_at=?
            WHERE mcp_server_operation_id=?
            """,
            (completed_at, completed_at, operation_id),
        )
        connection.commit()
        assert tuple(
            connection.execute(
                """
                SELECT status,attempt_count,lease_generation,started_at,error_code
                FROM mcp_server_sync_jobs WHERE mcp_server_sync_job_id=?
                """,
                (job_id,),
            ).fetchone()
        ) == ("succeeded", 3, 5, NOW_1, None)
        _assert_healthy(connection)


def test_prd082_tool_refresh_touch_and_disabled_reappearance_are_owned(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0074(prd082_templates, tmp_path / "tool-refresh-owner.sqlite3")
    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        graph = _insert_server_tool(connection, "refresh-owner")
        with pytest.raises(sqlite3.IntegrityError, match="mcp_tool_transition_invalid"):
            connection.execute(
                "UPDATE mcp_tools SET updated_by=?,updated_at=? WHERE mcp_tool_id=?",
                (graph["user_id"], NOW_1, graph["tool_id"]),
            )
        connection.rollback()

        other_actor = _insert_second_user(connection, str(graph["tenant_id"]))
        operation_id = _id("mcpo_", "refresh-owner-operation")
        job_id = _id("mcpj_", "refresh-owner-job")
        request_id = "req_prd082_refresh_owner"
        trace_id = "trc_prd082_refresh_owner"
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
                _digest("refresh-owner-operation-key"),
                _digest("refresh-owner-operation-intent"),
                graph["user_id"],
                request_id,
                trace_id,
                NOW_1,
                NOW_1,
            ),
        )
        connection.execute(
            """
            INSERT INTO mcp_server_sync_jobs(
              mcp_server_sync_job_id,tenant_id,mcp_server_id,mcp_server_operation_id,
              candidate_credential_revision,job_type,status,attempt_count,lease_generation,
              lease_owner_id,lease_token,lease_expires_at,next_attempt_at,deadline_at,
              error_code,created_at,started_at,completed_at,updated_at
            ) VALUES(?,?,?,?,NULL,'discover','pending',0,0,NULL,NULL,NULL,?,?,NULL,
                     ?,NULL,NULL,?)
            """,
            (
                job_id,
                graph["tenant_id"],
                graph["server_id"],
                operation_id,
                NOW_1,
                FUTURE,
                NOW_1,
                NOW_1,
            ),
        )
        connection.execute(
            """
            UPDATE mcp_server_sync_jobs SET status='processing',attempt_count=1,
              lease_generation=1,lease_owner_id='refresh-owner-worker',lease_token=?,
              lease_expires_at='2026-08-16T00:10:01Z',next_attempt_at=NULL,
              started_at=?,updated_at=? WHERE mcp_server_sync_job_id=?
            """,
            (_digest("refresh-owner-job-token"), NOW_1, NOW_1, job_id),
        )
        connection.execute(
            "UPDATE mcp_server_operations SET status='processing',updated_at=? "
            "WHERE mcp_server_operation_id=?",
            (NOW_1, operation_id),
        )
        connection.commit()

        assert connection.execute(
            "UPDATE mcp_tools SET updated_by=?,updated_at=? WHERE mcp_tool_id=?",
            (graph["user_id"], NOW_1, graph["tool_id"]),
        ).rowcount == 1
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="mcp_tool_transition_invalid"):
            connection.execute(
                "UPDATE mcp_tools SET updated_by=?,updated_at=? WHERE mcp_tool_id=?",
                (other_actor, NOW_2, graph["tool_id"]),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="mcp_tool_transition_invalid"):
            connection.execute(
                "UPDATE mcp_tools SET description='drifted',updated_by=?,updated_at=? "
                "WHERE mcp_tool_id=?",
                (graph["user_id"], NOW_2, graph["tool_id"]),
            )
        connection.rollback()
        connection.execute(
            "UPDATE mcp_tools SET updated_by=?,updated_at=? WHERE mcp_tool_id=?",
            (graph["user_id"], NOW_2, graph["tool_id"]),
        )
        assert tuple(
            connection.execute(
                "SELECT status,schema_revision,policy_revision,updated_at "
                "FROM mcp_tools WHERE mcp_tool_id=?",
                (graph["tool_id"],),
            ).fetchone()
        ) == ("active", 1, 2, NOW_2)

        connection.execute(
            "UPDATE mcp_tools SET status='disabled',updated_by=?,updated_at=? "
            "WHERE mcp_tool_id=?",
            (graph["user_id"], NOW_3, graph["tool_id"]),
        )
        reappeared_at = "2026-08-16T00:00:04Z"
        reappeared_schema = _digest("refresh-owner-reappeared-schema")
        connection.execute(
            """
            UPDATE mcp_tools SET description='reappeared',
              input_schema_original_json='{"type":"object"}',
              input_schema_original_sha256=?,
              input_schema_effective_json='{"type":"object"}',
              input_schema_effective_sha256=?,schema_revision=2,policy_revision=3,
              status='review_required',updated_by=?,updated_at=? WHERE mcp_tool_id=?
            """,
            (
                reappeared_schema,
                reappeared_schema,
                graph["user_id"],
                reappeared_at,
                graph["tool_id"],
            ),
        )
        connection.execute(
            """
            UPDATE mcp_server_sync_jobs SET status='succeeded',lease_generation=2,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=NULL,completed_at=?,updated_at=?
            WHERE mcp_server_sync_job_id=?
            """,
            (reappeared_at, reappeared_at, job_id),
        )
        connection.execute(
            "UPDATE mcp_server_operations SET status='succeeded',completed_at=?,updated_at=? "
            "WHERE mcp_server_operation_id=?",
            (reappeared_at, reappeared_at, operation_id),
        )
        before = {
            "mcp_tool_id": graph["tool_id"],
            "mcp_server_id": graph["server_id"],
            "status": "disabled",
            "schema_revision": 1,
            "policy_revision": 2,
            "risk_level": "standard",
            "approval_required": False,
            "required_permission_count": 0,
            "role_grant_count": 1,
            "user_grant_count": 0,
            "redaction_count": 0,
        }
        after = {
            **before,
            "status": "review_required",
            "schema_revision": 2,
            "policy_revision": 3,
        }
        _insert_mcp_audit(
            connection,
            graph,
            "disabled-tool-reappeared",
            action="mcp.tool_refreshed",
            object_type="mcp_tool",
            object_id=graph["tool_id"],
            before=before,
            after=after,
            request_id=request_id,
            trace_id=trace_id,
            created_at=reappeared_at,
        )
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='mcp.tool_refreshed' "
            "AND object_id=?",
            (graph["tool_id"],),
        ).fetchone()[0] == 1
        _assert_healthy(connection)


def test_prd082_probe_attempt_four_is_the_only_expired_terminal(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0074(prd082_templates, tmp_path / "probe-limit.sqlite3")
    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        graph = _insert_server_tool(connection, "probe-limit")
        invocation = _invocation_values(graph, "probe-limit")
        _insert_mapping(connection, "mcp_invocations", invocation)
        invocation_id = invocation["mcp_invocation_id"]
        claims = (
            (1, NOW_1, "2026-08-16T00:01:01Z"),
            (2, "2026-08-16T00:01:02Z", "2026-08-16T00:02:02Z"),
            (3, "2026-08-16T00:02:03Z", "2026-08-16T00:03:03Z"),
        )
        for attempt, claimed_at, expires_at in claims:
            connection.execute(
                """
                UPDATE mcp_invocations SET status='probing',state_revision=?,
                  probe_attempt_count=?,lease_generation=?,lease_owner_id=?,
                  lease_token=?,lease_expires_at=?,next_attempt_at=NULL,
                  started_at=COALESCE(started_at,?),updated_at=?
                WHERE mcp_invocation_id=?
                """,
                (
                    attempt + 1,
                    attempt,
                    attempt,
                    f"probe-worker-{attempt}",
                    _digest(f"probe-limit-token-{attempt}"),
                    expires_at,
                    claimed_at,
                    claimed_at,
                    invocation_id,
                ),
            )

        attempt_three_expired = "2026-08-16T00:03:03Z"
        with pytest.raises(
            sqlite3.IntegrityError, match="mcp_invocation_transition_invalid"
        ):
            connection.execute(
                """
                UPDATE mcp_invocations SET status='failed',state_revision=5,
                  duration_ms=1,error_code='mcp.server_unavailable',lease_generation=4,
                  lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                  next_attempt_at=NULL,completed_at=?,updated_at=?
                WHERE mcp_invocation_id=?
                """,
                (attempt_three_expired, attempt_three_expired, invocation_id),
            )

        attempt_four_expiry = "2026-08-16T00:04:03Z"
        connection.execute(
            """
            UPDATE mcp_invocations SET status='probing',state_revision=5,
              probe_attempt_count=4,lease_generation=4,
              lease_owner_id='probe-worker-4',lease_token=?,lease_expires_at=?,
              next_attempt_at=NULL,updated_at=? WHERE mcp_invocation_id=?
            """,
            (
                _digest("probe-limit-token-4"),
                attempt_four_expiry,
                attempt_three_expired,
                invocation_id,
            ),
        )
        connection.execute(
            """
            UPDATE mcp_invocations SET status='failed',state_revision=6,
              duration_ms=1,error_code='mcp.server_unavailable',lease_generation=5,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=NULL,completed_at=?,updated_at=?
            WHERE mcp_invocation_id=?
            """,
            (attempt_four_expiry, attempt_four_expiry, invocation_id),
        )
        connection.commit()
        assert tuple(
            connection.execute(
                """
                SELECT status,probe_attempt_count,lease_generation,error_code,completed_at
                FROM mcp_invocations WHERE mcp_invocation_id=?
                """,
                (invocation_id,),
            ).fetchone()
        ) == ("failed", 4, 5, "mcp.server_unavailable", attempt_four_expiry)
        _assert_healthy(connection)


def test_prd082_attempt_completion_binds_parent_generation_status_and_lease(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0074(prd082_templates, tmp_path / "attempt-owner.sqlite3")
    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        graph = _insert_server_tool(connection, "attempt-owner")
        current_invocation, current_attempt = _claim_probe_attempt(
            connection, graph, "attempt-current", expires_at=FUTURE
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="mcp_attempt_owner_mismatch"):
            connection.execute(
                """
                INSERT INTO mcp_invocation_attempts(
                  mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
                  attempt_number,status,lease_generation,error_code,request_bytes,
                  response_bytes,started_at,call_started_at,completed_at,duration_ms
                ) VALUES(?,?,?,'probe',1,'processing',2,NULL,NULL,NULL,?,NULL,NULL,NULL)
                """,
                (
                    _id("mcpa_", "attempt-wrong-generation"),
                    graph["tenant_id"],
                    current_invocation,
                    NOW_1,
                ),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="mcp_attempt_owner_mismatch"):
            connection.execute(
                """
                INSERT INTO mcp_invocation_attempts(
                  mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
                  attempt_number,status,lease_generation,error_code,request_bytes,
                  response_bytes,started_at,call_started_at,completed_at,duration_ms
                ) VALUES(?,?,?,'call',1,'processing',1,NULL,2,NULL,?,?,NULL,NULL)
                """,
                (
                    _id("mcpa_", "attempt-wrong-parent-status"),
                    graph["tenant_id"],
                    current_invocation,
                    NOW_1,
                    NOW_1,
                ),
            )
        connection.rollback()
        with pytest.raises(
            sqlite3.IntegrityError, match="mcp_attempt_transition_invalid"
        ):
            connection.execute(
                "UPDATE mcp_invocation_attempts SET status='succeeded',completed_at=?,"
                "duration_ms=1 WHERE mcp_invocation_attempt_id=?",
                (NOW, current_attempt),
            )
        connection.rollback()
        connection.execute(
            "UPDATE mcp_invocation_attempts SET status='succeeded',request_bytes=0,"
            "response_bytes=0,completed_at=?,duration_ms=1 "
            "WHERE mcp_invocation_attempt_id=?",
            (NOW_2, current_attempt),
        )
        connection.commit()

        probe_expiry = "2026-08-16T00:01:01Z"
        expired_invocation, expired_attempt = _claim_probe_attempt(
            connection, graph, "attempt-expired-probe", expires_at=probe_expiry
        )
        connection.commit()
        with pytest.raises(
            sqlite3.IntegrityError, match="mcp_attempt_transition_invalid"
        ):
            connection.execute(
                "UPDATE mcp_invocation_attempts SET status='succeeded',request_bytes=0,"
                "response_bytes=0,completed_at=?,duration_ms=1 "
                "WHERE mcp_invocation_attempt_id=?",
                (probe_expiry, expired_attempt),
            )
        connection.rollback()
        connection.execute(
            "UPDATE mcp_invocation_attempts SET status='failed',"
            "error_code='mcp.server_unavailable',completed_at=?,duration_ms=1 "
            "WHERE mcp_invocation_attempt_id=?",
            (probe_expiry, expired_attempt),
        )
        connection.commit()

        call_expiry = "2026-08-16T00:01:02Z"
        calling_invocation = _make_calling_invocation(
            connection,
            graph,
            "attempt-expired-call",
            lease_expires_at=call_expiry,
        )
        call_attempt = _id("mcpa_", "attempt-expired-call")
        connection.execute(
            """
            INSERT INTO mcp_invocation_attempts(
              mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
              attempt_number,status,lease_generation,error_code,request_bytes,response_bytes,
              started_at,call_started_at,completed_at,duration_ms
            ) VALUES(?,?,?,'call',1,'processing',2,NULL,2,NULL,?,?,NULL,NULL)
            """,
            (
                call_attempt,
                graph["tenant_id"],
                calling_invocation,
                NOW_2,
                NOW_2,
            ),
        )
        connection.commit()
        with pytest.raises(
            sqlite3.IntegrityError, match="mcp_attempt_transition_invalid"
        ):
            connection.execute(
                "UPDATE mcp_invocation_attempts SET status='succeeded',response_bytes=0,"
                "completed_at=?,duration_ms=1 WHERE mcp_invocation_attempt_id=?",
                (call_expiry, call_attempt),
            )
        connection.rollback()
        connection.execute(
            "UPDATE mcp_invocation_attempts SET status='outcome_unknown',"
            "error_code='mcp.outcome_unknown',response_bytes=0,completed_at=?,duration_ms=1 "
            "WHERE mcp_invocation_attempt_id=?",
            (call_expiry, call_attempt),
        )
        connection.commit()

        assert tuple(
            connection.execute(
                "SELECT status,error_code FROM mcp_invocation_attempts "
                "WHERE mcp_invocation_attempt_id=?",
                (current_attempt,),
            ).fetchone()
        ) == ("succeeded", None)
        assert tuple(
            connection.execute(
                "SELECT status,error_code FROM mcp_invocation_attempts "
                "WHERE mcp_invocation_attempt_id=?",
                (expired_attempt,),
            ).fetchone()
        ) == ("failed", "mcp.server_unavailable")
        assert tuple(
            connection.execute(
                "SELECT status,error_code FROM mcp_invocation_attempts "
                "WHERE mcp_invocation_attempt_id=?",
                (call_attempt,),
            ).fetchone()
        ) == ("outcome_unknown", "mcp.outcome_unknown")
        assert connection.execute(
            "SELECT status FROM mcp_invocations WHERE mcp_invocation_id=?",
            (expired_invocation,),
        ).fetchone()[0] == "probing"
        _assert_healthy(connection)


def test_prd082_result_projection_matrix_is_closed(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0074(prd082_templates, tmp_path / "result-matrix.sqlite3")
    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        graph = _insert_server_tool(connection, "result-matrix")
        markers = (
            "success",
            "tool-error",
            "blocked",
            "dlp-unavailable",
            "partial-cipher",
            "result-bytes-only",
            "missing-count",
            "unexpected-count",
            "wrong-output-status",
        )
        invocation_ids = {
            marker: _make_calling_invocation(connection, graph, f"result-{marker}")
            for marker in markers
        }
        connection.commit()

        def finalize(
            marker: str,
            *,
            status: str,
            error_code: str | None,
            output_status: str | None,
            output_match_count: int | None,
            result_ciphertext: str | None = None,
            result_encryption_key_version: int | None = None,
            result_hmac: str | None = None,
            result_hmac_key_version: int | None = None,
            result_bytes: int | None = None,
            result_redaction_count: int | None = None,
        ) -> None:
            has_output_dlp = output_status is not None
            connection.execute(
                """
                UPDATE mcp_invocations SET status=?,state_revision=4,
                  output_dlp_status=?,output_dlp_match_count=?,output_dlp_rule_id=NULL,
                  output_dlp_ruleset_hmac=?,output_dlp_ruleset_hmac_key_version=?,
                  output_dlp_scanned_at=?,result_ciphertext=?,
                  result_encryption_key_version=?,result_hmac=?,result_hmac_key_version=?,
                  result_bytes=?,result_redaction_count=?,request_bytes=2,response_bytes=2,
                  duration_ms=2,execution_status='called',tool_call_count=1,
                  cost_status='not_reported',error_code=?,lease_generation=3,
                  lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                  next_attempt_at=NULL,completed_at=?,updated_at=?
                WHERE mcp_invocation_id=?
                """,
                (
                    status,
                    output_status,
                    output_match_count,
                    _digest(f"result-rules-{marker}") if has_output_dlp else None,
                    1 if has_output_dlp else None,
                    NOW_3 if has_output_dlp else None,
                    result_ciphertext,
                    result_encryption_key_version,
                    result_hmac,
                    result_hmac_key_version,
                    result_bytes,
                    result_redaction_count,
                    error_code,
                    NOW_3,
                    NOW_3,
                    invocation_ids[marker],
                ),
            )

        with pytest.raises(sqlite3.IntegrityError):
            finalize(
                "partial-cipher",
                status="succeeded",
                error_code=None,
                output_status="clean",
                output_match_count=0,
                result_ciphertext="enc_partial",
                result_redaction_count=1,
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            finalize(
                "result-bytes-only",
                status="failed",
                error_code="mcp.tool_failed",
                output_status="clean",
                output_match_count=0,
                result_bytes=2,
                result_redaction_count=1,
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            finalize(
                "missing-count",
                status="failed",
                error_code="mcp.tool_failed",
                output_status="clean",
                output_match_count=0,
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            finalize(
                "unexpected-count",
                status="failed",
                error_code="mcp.result_invalid",
                output_status=None,
                output_match_count=None,
                result_redaction_count=1,
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            finalize(
                "wrong-output-status",
                status="failed",
                error_code="mcp.tool_failed",
                output_status="unavailable",
                output_match_count=0,
                result_redaction_count=0,
            )
        connection.rollback()

        finalize(
            "success",
            status="succeeded",
            error_code=None,
            output_status="clean",
            output_match_count=0,
            result_ciphertext="enc_result",
            result_encryption_key_version=1,
            result_hmac=_digest("result-matrix-success"),
            result_hmac_key_version=1,
            result_bytes=2,
            result_redaction_count=1,
        )
        finalize(
            "tool-error",
            status="failed",
            error_code="mcp.tool_failed",
            output_status="clean",
            output_match_count=0,
            result_redaction_count=0,
        )
        finalize(
            "blocked",
            status="blocked",
            error_code="mcp.result_blocked",
            output_status="blocked",
            output_match_count=1,
            result_redaction_count=2,
        )
        finalize(
            "dlp-unavailable",
            status="failed",
            error_code="mcp.output_dlp_unavailable",
            output_status="unavailable",
            output_match_count=0,
            result_redaction_count=0,
        )
        connection.commit()

        assert tuple(
            tuple(row)
            for row in connection.execute(
                """
                SELECT status,error_code,result_ciphertext,result_bytes,
                       result_redaction_count
                FROM mcp_invocations
                WHERE mcp_invocation_id IN(?,?,?,?)
                ORDER BY status,error_code
                """,
                (
                    invocation_ids["success"],
                    invocation_ids["tool-error"],
                    invocation_ids["blocked"],
                    invocation_ids["dlp-unavailable"],
                ),
            )
        ) == (
            ("blocked", "mcp.result_blocked", None, None, 2),
            ("failed", "mcp.output_dlp_unavailable", None, None, 0),
            ("failed", "mcp.tool_failed", None, None, 0),
            ("succeeded", None, "enc_result", 2, 1),
        )
        _assert_healthy(connection)


def test_prd082_tool_grants_rowid_and_three_column_fk(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0074(prd082_templates, tmp_path / "tool-guards.sqlite3")
    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        graph = _insert_server_tool(
            connection, "tool-a", add_grant=False, activate=False
        )
        with pytest.raises(sqlite3.IntegrityError, match="mcp_tool_transition_invalid"):
            connection.execute(
                "UPDATE mcp_tools SET status='active',policy_revision=2,updated_at=? "
                "WHERE mcp_tool_id=?",
                (NOW_1, graph["tool_id"]),
            )
        connection.rollback()
        connection.execute(
            "UPDATE mcp_tools SET status='disabled',updated_at=? WHERE mcp_tool_id=?",
            (NOW_1, graph["tool_id"]),
        )
        connection.commit()
        connection.execute(
            """
            INSERT INTO mcp_tool_role_grants(
              mcp_tool_role_grant_id,tenant_id,mcp_tool_id,role_id,created_by,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                graph["grant_id"],
                graph["tenant_id"],
                graph["tool_id"],
                graph["developer_role_id"],
                graph["user_id"],
                NOW,
            ),
        )
        connection.execute(
            "UPDATE mcp_tools SET status='active',policy_revision=2,updated_at=? "
            "WHERE mcp_tool_id=?",
            (NOW_1, graph["tool_id"]),
        )
        user_grant_id = _id("mcpug_", "user-grant-tool-a")
        connection.execute(
            """
            INSERT INTO mcp_tool_user_grants(
              mcp_tool_user_grant_id,tenant_id,mcp_tool_id,user_id,created_by,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                user_grant_id,
                graph["tenant_id"],
                graph["tool_id"],
                graph["user_id"],
                graph["user_id"],
                NOW_1,
            ),
        )
        connection.execute(
            "DELETE FROM mcp_tool_role_grants WHERE mcp_tool_role_grant_id=?",
            (graph["grant_id"],),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="mcp_tool_last_grant_required"):
            connection.execute(
                "DELETE FROM mcp_tool_user_grants WHERE mcp_tool_user_grant_id=?",
                (user_grant_id,),
            )
        connection.rollback()

        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd082_other','prd082-other','PRD082 Other',
                   'internal_only','active',?,?)
            """,
            (NOW, NOW),
        )
        other_user = "user_prd082_other"
        connection.execute(
            """
            INSERT INTO users(user_id,tenant_id,email,display_name,password_hash,role,status,
                              created_at,updated_at,password_changed_at)
            VALUES(?,'tenant_prd082_other','other@example.invalid','Other',
                   'not-a-runtime-credential','developer','active',?,?,?)
            """,
            (other_user, NOW, NOW, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="mcp_user_grant_owner_mismatch"):
            connection.execute(
                """
                INSERT INTO mcp_tool_user_grants(
                  mcp_tool_user_grant_id,tenant_id,mcp_tool_id,user_id,created_by,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    _id("mcpug_", "cross-tenant"),
                    graph["tenant_id"],
                    graph["tool_id"],
                    other_user,
                    graph["user_id"],
                    NOW_2,
                ),
            )
        connection.rollback()

        graph_b = _insert_server_tool(connection, "tool-b")
        mismatched = _invocation_values(graph, "server-tool-mismatch")
        mismatched["mcp_tool_id"] = graph_b["tool_id"]
        mismatched["input_schema_original_sha256"] = graph_b["schema_hash"]
        mismatched["input_schema_effective_sha256"] = graph_b["schema_hash"]
        with pytest.raises(sqlite3.IntegrityError):
            _insert_mapping(connection, "mcp_invocations", mismatched)
        connection.rollback()
        assert connection.execute(
            "SELECT COUNT(*) FROM pragma_foreign_key_list('mcp_invocations') "
            "WHERE \"table\"='mcp_tools' "
            "AND \"from\" IN('tenant_id','mcp_server_id','mcp_tool_id')"
        ).fetchone()[0] == 3
        _assert_healthy(connection)


def test_prd082_deferred_approval_both_orders_missing_edges_and_self_review(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0074(prd082_templates, tmp_path / "approval-guards.sqlite3")
    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        graph = _insert_server_tool(connection, "approval", risk_level="high")
        decider_id = _insert_second_user(connection, str(graph["tenant_id"]))
        connection.commit()

        task_id = _id("apr_", "approval-task-invocation-first")
        invocation = _invocation_values(
            graph,
            "approval-invocation-first",
            status="approval_pending",
            approval_task_id=task_id,
        )
        task = _approval_values(invocation, "invocation-first")
        connection.execute("BEGIN")
        _insert_mapping(connection, "mcp_invocations", invocation)
        _insert_mapping(connection, "approval_tasks", task)
        connection.commit()

        task_first_id = _id("apr_", "approval-task-first")
        invocation_second = _invocation_values(
            graph,
            "approval-task-first",
            status="approval_pending",
            approval_task_id=task_first_id,
        )
        task_second = _approval_values(invocation_second, "task-first")
        connection.execute("BEGIN")
        _insert_mapping(connection, "approval_tasks", task_second)
        _insert_mapping(connection, "mcp_invocations", invocation_second)
        connection.commit()

        missing_task = _invocation_values(
            graph,
            "approval-missing-task",
            status="approval_pending",
            approval_task_id=_id("apr_", "missing-task"),
        )
        connection.execute("BEGIN")
        _insert_mapping(connection, "mcp_invocations", missing_task)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.commit()
        connection.rollback()

        missing_invocation = _invocation_values(
            graph,
            "approval-missing-invocation",
            status="approval_pending",
            approval_task_id=_id("apr_", "missing-invocation-task"),
        )
        orphan_task = _approval_values(missing_invocation, "missing-invocation")
        connection.execute("BEGIN")
        _insert_mapping(connection, "approval_tasks", orphan_task)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.commit()
        connection.rollback()

        mismatched_id = _id("apr_", "mismatched-owner-task")
        mismatched_invocation = _invocation_values(
            graph,
            "mismatched-owner",
            status="approval_pending",
            approval_task_id=mismatched_id,
        )
        mismatched_task = _approval_values(
            mismatched_invocation, "mismatched-owner", created_by_id=decider_id
        )
        connection.execute("BEGIN")
        _insert_mapping(connection, "approval_tasks", mismatched_task)
        with pytest.raises(sqlite3.IntegrityError, match="mcp_invocation_owner_mismatch"):
            _insert_mapping(connection, "mcp_invocations", mismatched_invocation)
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="approval_task_transition_invalid"):
            connection.execute(
                """
                UPDATE approval_tasks SET status='approved',resolution_reason='approved',
                  state_revision=2,writeback_status='completed',decided_by_user_id=?,
                  decided_at=?,decision_request_id='req_prd082_self',
                  decision_trace_id='trc_prd082_self',updated_at=?
                WHERE approval_task_id=?
                """,
                (graph["user_id"], NOW_1, NOW_1, task_id),
            )
        connection.rollback()

        connection.execute("BEGIN")
        connection.execute(
            """
            UPDATE mcp_invocations SET status='queued',state_revision=2,queued_at=?,
              next_attempt_at=?,updated_at=? WHERE mcp_invocation_id=?
            """,
            (NOW_1, NOW_1, NOW_1, invocation["mcp_invocation_id"]),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.commit()
        connection.rollback()

        connection.execute("BEGIN")
        connection.execute(
            """
            UPDATE mcp_invocations SET status='queued',state_revision=2,queued_at=?,
              next_attempt_at=?,updated_at=? WHERE mcp_invocation_id=?
            """,
            (NOW_1, NOW_1, NOW_1, invocation["mcp_invocation_id"]),
        )
        connection.execute(
            """
            UPDATE approval_tasks SET status='rejected',resolution_reason='user_rejected',
              state_revision=2,writeback_status='completed',decided_by_user_id=?,
              decided_at=?,decision_request_id='req_prd082_reject_drift',
              decision_trace_id='trc_prd082_reject_drift',updated_at=?
            WHERE approval_task_id=?
            """,
            (decider_id, NOW_1, NOW_1, task_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.commit()
        connection.rollback()

        gate_fk = {
            (row["from"], row["to"])
            for row in connection.execute("PRAGMA foreign_key_list('mcp_invocations')")
            if row["table"] == "approval_tasks"
        }
        assert gate_fk == {
            ("tenant_id", "tenant_id"),
            ("approval_task_id", "approval_task_id"),
            ("mcp_invocation_id", "mcp_invocation_id"),
            ("approval_gate_status", "status"),
            ("requested_by", "created_by_id"),
            ("approval_gate_target_version", "target_version"),
            ("policy_fingerprint", "target_fingerprint"),
            ("approval_deadline_at", "expires_at"),
        }

        connection.execute(
            """
            UPDATE mcp_invocations SET status='queued',state_revision=2,queued_at=?,
              next_attempt_at=?,updated_at=?
            WHERE mcp_invocation_id=?
            """,
            (NOW_1, NOW_1, NOW_1, invocation["mcp_invocation_id"]),
        )
        connection.execute(
            """
            UPDATE approval_tasks SET status='approved',resolution_reason='approved',
              state_revision=2,writeback_status='completed',decided_by_user_id=?,
              decided_at=?,decision_request_id='req_prd082_approve',
              decision_trace_id='trc_prd082_approve',updated_at=?
            WHERE approval_task_id=?
            """,
            (decider_id, NOW_1, NOW_1, task_id),
        )
        decision_id = _id("apd_", "approval-decision")
        connection.execute(
            """
            INSERT INTO approval_decisions(
              approval_decision_id,tenant_id,approval_task_id,decision,reason_code,
              actor_type,actor_id,revision_before,revision_after,step_up_used,
              writeback_status,request_id,trace_id,decided_at
            ) VALUES(?,?,?,'approve','approved_business_need','user',?,1,2,1,
                     'completed','req_prd082_approve','trc_prd082_approve',?)
            """,
            (decision_id, graph["tenant_id"], task_id, decider_id, NOW_1),
        )
        connection.execute(
            """
            INSERT INTO approval_writebacks(
              approval_writeback_id,tenant_id,approval_task_id,adapter_kind,
              target_fact_id,target_revision,status,attempt_count,lease_generation,
              lease_owner_id,lease_token,lease_expires_at,next_attempt_at,last_error_code,
              completed_at,created_at,updated_at
            ) VALUES(?,?,?,'mcp_invocation_enqueue',?,2,'completed',0,0,
                     NULL,NULL,NULL,NULL,NULL,?,?,?)
            """,
            (
                _id("apwb_", "approval-writeback"),
                graph["tenant_id"],
                task_id,
                invocation["mcp_invocation_id"],
                NOW_1,
                NOW_1,
                NOW_1,
            ),
        )
        connection.commit()
        assert tuple(
            connection.execute(
                "SELECT status,state_revision FROM mcp_invocations "
                "WHERE mcp_invocation_id=?",
                (invocation["mcp_invocation_id"],),
            ).fetchone()
        ) == ("queued", 2)
        _assert_healthy(connection)


def test_prd082_dlp_attempt_event_outbox_and_audit_contracts(
    prd082_templates: dict[str, Path], tmp_path: Path
) -> None:
    database_url = _clone_0074(prd082_templates, tmp_path / "event-audit.sqlite3")
    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        high = _insert_server_tool(connection, "high-dlp", risk_level="high")
        for marker, status, dlp_status, match_count in (
            ("high-blocked", "blocked", "blocked", 1),
            ("high-unavailable", "failed", "unavailable", 0),
        ):
            values = _invocation_values(
                high,
                marker,
                status=status,
                input_dlp_status=dlp_status,
                input_dlp_match_count=match_count,
            )
            _insert_mapping(connection, "mcp_invocations", values)
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM mcp_invocations WHERE approval_required=1 "
            "AND approval_task_id IS NULL AND status IN('blocked','failed')"
        ).fetchone()[0] == 2

        invalid = _invocation_values(
            high,
            "invalid-dlp-count",
            status="blocked",
            input_dlp_status="blocked",
            input_dlp_match_count=0,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_mapping(connection, "mcp_invocations", invalid)
        connection.rollback()

        graph = _insert_server_tool(connection, "event")
        rescanned = _invocation_values(graph, "rescan-blocked")
        _insert_mapping(connection, "mcp_invocations", rescanned)
        rescan_token = _digest("rescan-probe-lease")
        connection.execute(
            """
            UPDATE mcp_invocations SET status='probing',state_revision=2,
              probe_attempt_count=1,lease_generation=1,lease_owner_id='worker-prd082',
              lease_token=?,lease_expires_at=?,next_attempt_at=NULL,started_at=?,updated_at=?
            WHERE mcp_invocation_id=?
            """,
            (
                rescan_token,
                FUTURE,
                NOW_1,
                NOW_1,
                rescanned["mcp_invocation_id"],
            ),
        )
        rescan_attempt_id = _id("mcpa_", "rescan-probe-attempt")
        connection.execute(
            """
            INSERT INTO mcp_invocation_attempts(
              mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
              attempt_number,status,lease_generation,error_code,request_bytes,
              response_bytes,started_at,call_started_at,completed_at,duration_ms
            ) VALUES(?,?,?,'probe',1,'processing',1,NULL,NULL,NULL,?,NULL,NULL,NULL)
            """,
            (
                rescan_attempt_id,
                graph["tenant_id"],
                rescanned["mcp_invocation_id"],
                NOW_1,
            ),
        )
        connection.execute(
            """
            UPDATE mcp_invocation_attempts SET status='failed',
              error_code='mcp.arguments_blocked',completed_at=?,duration_ms=1
            WHERE mcp_invocation_attempt_id=?
            """,
            (NOW_2, rescan_attempt_id),
        )
        connection.execute(
            """
            UPDATE mcp_invocations SET status='blocked',state_revision=3,
              input_dlp_status='blocked',input_dlp_match_count=1,
              input_dlp_ruleset_hmac=?,input_dlp_ruleset_hmac_key_version=2,
              input_dlp_scanned_at=?,duration_ms=1,error_code='mcp.arguments_blocked',
              lease_generation=2,lease_owner_id=NULL,lease_token=NULL,
              lease_expires_at=NULL,next_attempt_at=NULL,completed_at=?,updated_at=?
            WHERE mcp_invocation_id=?
            """,
            (
                _digest("rescan-ruleset"),
                NOW_2,
                NOW_2,
                NOW_2,
                rescanned["mcp_invocation_id"],
            ),
        )
        assert tuple(
            connection.execute(
                """
                SELECT status,input_dlp_status,input_dlp_match_count,execution_status,
                       call_attempt_count,error_code
                FROM mcp_invocations WHERE mcp_invocation_id=?
                """,
                (rescanned["mcp_invocation_id"],),
            ).fetchone()
        ) == ("blocked", "blocked", 1, "not_called", 0, "mcp.arguments_blocked")
        assert tuple(
            connection.execute(
                "SELECT status,error_code FROM mcp_invocation_attempts "
                "WHERE mcp_invocation_attempt_id=?",
                (rescan_attempt_id,),
            ).fetchone()
        ) == ("failed", "mcp.arguments_blocked")
        assert connection.execute(
            "SELECT COUNT(*) FROM mcp_invocation_events WHERE mcp_invocation_id=?",
            (rescanned["mcp_invocation_id"],),
        ).fetchone()[0] == 0

        invocation = _invocation_values(graph, "event-success")
        _insert_mapping(connection, "mcp_invocations", invocation)
        connection.execute(
            """
            UPDATE mcp_invocations SET status='probing',state_revision=2,
              probe_attempt_count=1,lease_generation=1,lease_owner_id='worker-prd082',
              lease_token=?,lease_expires_at=?,next_attempt_at=NULL,started_at=?,updated_at=?
            WHERE mcp_invocation_id=?
            """,
            (
                _digest("probe-lease"),
                FUTURE,
                NOW_1,
                NOW_1,
                invocation["mcp_invocation_id"],
            ),
        )
        probe_attempt_id = _id("mcpa_", "probe-attempt")
        connection.execute(
            """
            INSERT INTO mcp_invocation_attempts(
              mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
              attempt_number,status,lease_generation,error_code,request_bytes,
              response_bytes,started_at,call_started_at,completed_at,duration_ms
            ) VALUES(?,?,?,'probe',1,'processing',1,NULL,NULL,NULL,?,NULL,NULL,NULL)
            """,
            (
                probe_attempt_id,
                graph["tenant_id"],
                invocation["mcp_invocation_id"],
                NOW_1,
            ),
        )
        connection.execute(
            "UPDATE mcp_invocation_attempts SET status='succeeded',completed_at=?,"
            "duration_ms=1 WHERE mcp_invocation_attempt_id=?",
            (NOW_2, probe_attempt_id),
        )
        call_token = _digest("call-lease")
        connection.execute(
            """
            UPDATE mcp_invocations SET status='calling',state_revision=3,
              call_attempt_count=1,call_started_at=?,execution_status='unknown',
              tool_call_count=NULL,cost_status='unknown',lease_generation=2,
              lease_token=?,updated_at=?
            WHERE mcp_invocation_id=?
            """,
            (NOW_2, call_token, NOW_2, invocation["mcp_invocation_id"]),
        )
        call_attempt_id = _id("mcpa_", "call-attempt")
        connection.execute(
            """
            INSERT INTO mcp_invocation_attempts(
              mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
              attempt_number,status,lease_generation,error_code,request_bytes,
              response_bytes,started_at,call_started_at,completed_at,duration_ms
            ) VALUES(?,?,?,'call',1,'processing',2,NULL,2,NULL,?,?,NULL,NULL)
            """,
            (
                call_attempt_id,
                graph["tenant_id"],
                invocation["mcp_invocation_id"],
                NOW_2,
                NOW_2,
            ),
        )
        connection.execute(
            "UPDATE mcp_invocation_attempts SET status='succeeded',request_bytes=11,"
            "response_bytes=2,"
            "completed_at=?,duration_ms=1 WHERE mcp_invocation_attempt_id=?",
            (NOW_3, call_attempt_id),
        )
        connection.execute(
            """
            UPDATE mcp_invocations SET status='succeeded',state_revision=4,
              output_dlp_status='clean',output_dlp_match_count=0,
              output_dlp_ruleset_hmac=?,output_dlp_ruleset_hmac_key_version=1,
              output_dlp_scanned_at=?,result_ciphertext='enc_result',
              result_encryption_key_version=1,result_hmac=?,result_hmac_key_version=1,
              result_bytes=2,result_redaction_count=0,request_bytes=11,
              response_bytes=2,duration_ms=2,
              execution_status='called',tool_call_count=1,cost_status='not_reported',
              lease_generation=3,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=NULL,completed_at=?,updated_at=?
            WHERE mcp_invocation_id=?
            """,
            (
                _digest("output-rules"),
                NOW_3,
                _digest("result"),
                NOW_3,
                NOW_3,
                invocation["mcp_invocation_id"],
            ),
        )
        event_id = _id("mcpevt_", "event-success")
        event_values = {
            "mcp_invocation_event_id": event_id,
            "tenant_id": graph["tenant_id"],
            "mcp_invocation_id": invocation["mcp_invocation_id"],
            "event_type": "mcp.tool_called",
            "schema_version": 1,
            "mcp_server_id": graph["server_id"],
            "mcp_tool_id": graph["tool_id"],
            "status": "succeeded",
            "risk_level": "standard",
            "approval_required": 0,
            "network_attempted": 1,
            "input_dlp_status": "clean",
            "input_dlp_match_count": 0,
            "output_dlp_status": "clean",
            "output_dlp_match_count": 0,
            "redaction_count": 0,
            "probe_attempt_count": 1,
            "call_attempt_count": 1,
            "duration_ms": 2,
            "request_bytes": 11,
            "response_bytes": 2,
            "execution_status": "called",
            "invocation_count": 1,
            "tool_call_count": 1,
            "cost_status": "not_reported",
            "error_code": None,
            "completed_at": NOW_3,
            "created_at": NOW_3,
        }
        _insert_mapping(connection, "mcp_invocation_events", event_values)
        outbox_id = _id("mcpob_", "event-outbox")
        outbox_insert = """
            INSERT INTO mcp_invocation_event_outbox(
              mcp_invocation_event_outbox_id,tenant_id,mcp_invocation_event_id,status,
              attempt_count,lease_generation,lease_owner_id,lease_token,lease_expires_at,
              next_attempt_at,subscription_snapshot_json,snapshot_frozen_at,error_code,
              delivered_at,created_at,updated_at
            ) VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,?,?,?,?,NULL,?,?)
        """
        connection.execute("SAVEPOINT prd082_legacy_snapshot")
        connection.execute(
            outbox_insert,
            (
                outbox_id,
                graph["tenant_id"],
                event_id,
                NOW_3,
                None,
                None,
                None,
                NOW_3,
                NOW_3,
            ),
        )
        first_token = _digest("event-outbox-first-token")
        second_token = _digest("event-outbox-second-token")
        connection.execute(
            """
            UPDATE mcp_invocation_event_outbox SET status='processing',attempt_count=1,
              lease_generation=1,lease_owner_id='worker-prd082',lease_token=?,
              lease_expires_at=?,next_attempt_at=NULL,updated_at=?
            WHERE mcp_invocation_event_outbox_id=?
            """,
            (first_token, FUTURE, NOW_3, outbox_id),
        )
        connection.execute(
            """
            UPDATE mcp_invocation_event_outbox SET lease_generation=2,lease_token=?,
              lease_expires_at=?,subscription_snapshot_json='[]',snapshot_frozen_at=?,
              updated_at=? WHERE mcp_invocation_event_outbox_id=?
            """,
            (second_token, FUTURE, NOW_3, NOW_3, outbox_id),
        )
        assert tuple(
            connection.execute(
                "SELECT subscription_snapshot_json,snapshot_frozen_at "
                "FROM mcp_invocation_event_outbox WHERE mcp_invocation_event_outbox_id=?",
                (outbox_id,),
            ).fetchone()
        ) == ("[]", NOW_3)
        connection.execute("ROLLBACK TO prd082_legacy_snapshot")
        connection.execute("RELEASE prd082_legacy_snapshot")

        connection.execute(
            outbox_insert,
            (
                outbox_id,
                graph["tenant_id"],
                event_id,
                NOW_3,
                "[]",
                NOW_3,
                None,
                NOW_3,
                NOW_3,
            ),
        )
        connection.execute(
            """
            UPDATE mcp_invocation_event_outbox SET status='processing',attempt_count=1,
              lease_generation=1,lease_owner_id='worker-prd082',lease_token=?,
              lease_expires_at=?,next_attempt_at=NULL,updated_at=?
            WHERE mcp_invocation_event_outbox_id=?
            """,
            (first_token, FUTURE, NOW_3, outbox_id),
        )
        connection.execute(
            """
            UPDATE mcp_invocation_event_outbox SET lease_generation=2,lease_token=?,
              lease_expires_at=?,updated_at=? WHERE mcp_invocation_event_outbox_id=?
            """,
            (second_token, FUTURE, NOW_3, outbox_id),
        )
        assert connection.execute(
            """
            UPDATE mcp_invocation_event_outbox SET updated_at=updated_at
            WHERE mcp_invocation_event_outbox_id=? AND lease_generation=1
              AND lease_token=?
            """,
            (outbox_id, first_token),
        ).rowcount == 0
        with pytest.raises(sqlite3.IntegrityError, match="mcp_outbox_transition_invalid"):
            connection.execute(
                """
                UPDATE mcp_invocation_event_outbox SET lease_generation=3,
                  lease_expires_at=?,updated_at=? WHERE mcp_invocation_event_outbox_id=?
                """,
                (FUTURE, NOW_3, outbox_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="mcp_outbox_transition_invalid"):
            connection.execute(
                """
                UPDATE mcp_invocation_event_outbox SET lease_generation=3,lease_token=?,
                  subscription_snapshot_json='["tampered"]',lease_expires_at=?,updated_at=?
                WHERE mcp_invocation_event_outbox_id=?
                """,
                (_digest("tampered-token"), FUTURE, NOW_3, outbox_id),
            )

        connection.execute("SAVEPOINT prd082_outbox_terminal")
        connection.execute(
            """
            UPDATE mcp_invocation_event_outbox SET status='succeeded',lease_generation=3,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=NULL,error_code=NULL,delivered_at=?,updated_at=?
            WHERE mcp_invocation_event_outbox_id=?
            """,
            (NOW_3, NOW_3, outbox_id),
        )
        connection.execute("ROLLBACK TO prd082_outbox_terminal")
        connection.execute(
            """
            UPDATE mcp_invocation_event_outbox SET status='failed',lease_generation=3,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=NULL,error_code='mcp.unavailable',delivered_at=NULL,
              updated_at=? WHERE mcp_invocation_event_outbox_id=?
            """,
            (NOW_3, outbox_id),
        )
        connection.execute("ROLLBACK TO prd082_outbox_terminal")
        connection.execute("RELEASE prd082_outbox_terminal")

        generation = 3
        connection.execute(
            """
            UPDATE mcp_invocation_event_outbox SET status='pending',lease_generation=?,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=?,error_code=NULL,delivered_at=NULL,updated_at=?
            WHERE mcp_invocation_event_outbox_id=?
            """,
            (generation, NOW_3, NOW_3, outbox_id),
        )
        for attempt in range(2, 11):
            generation += 1
            connection.execute(
                """
                UPDATE mcp_invocation_event_outbox SET status='processing',attempt_count=?,
                  lease_generation=?,lease_owner_id='worker-prd082',lease_token=?,
                  lease_expires_at=?,next_attempt_at=NULL,error_code=NULL,updated_at=?
                WHERE mcp_invocation_event_outbox_id=?
                """,
                (
                    attempt,
                    generation,
                    _digest(f"event-outbox-attempt-{attempt}"),
                    FUTURE,
                    NOW_3,
                    outbox_id,
                ),
            )
            if attempt < 10:
                generation += 1
                connection.execute(
                    """
                    UPDATE mcp_invocation_event_outbox SET status='pending',
                      lease_generation=?,lease_owner_id=NULL,lease_token=NULL,
                      lease_expires_at=NULL,next_attempt_at=?,error_code=NULL,updated_at=?
                    WHERE mcp_invocation_event_outbox_id=?
                    """,
                    (generation, NOW_3, NOW_3, outbox_id),
                )
        generation += 1
        connection.execute(
            """
            UPDATE mcp_invocation_event_outbox SET status='failed',lease_generation=?,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at=NULL,error_code='webhook.delivery_unavailable',updated_at=?
            WHERE mcp_invocation_event_outbox_id=?
            """,
            (generation, "2026-08-16T02:00:00Z", outbox_id),
        )
        connection.commit()
        drifted = dict(event_values)
        drifted["mcp_invocation_event_id"] = _id("mcpevt_", "event-drift")
        drifted["duration_ms"] = 3
        with pytest.raises(sqlite3.IntegrityError, match="mcp_event_owner_mismatch"):
            _insert_mapping(connection, "mcp_invocation_events", drifted)
        connection.rollback()

        operation_id = _id("mcpo_", "audit-refresh-operation")
        operation_request = "req_prd082_audit_refresh"
        operation_trace = "trc_prd082_audit_refresh"
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
                _digest("audit-refresh-operation-key"),
                _digest("audit-refresh-operation-intent"),
                graph["user_id"],
                operation_request,
                operation_trace,
                NOW_1,
                NOW_1,
            ),
        )
        connection.execute(
            "UPDATE mcp_server_operations SET status='processing',updated_at=? "
            "WHERE mcp_server_operation_id=?",
            (NOW_2, operation_id),
        )
        connection.execute(
            """
            UPDATE mcp_servers SET state_revision=3,updated_by=?,updated_at=?
            WHERE mcp_server_id=?
            """,
            (graph["user_id"], NOW_3, graph["server_id"]),
        )
        connection.execute(
            """
            UPDATE mcp_tools SET status='review_required',schema_revision=2,
              policy_revision=3,updated_by=?,updated_at=? WHERE mcp_tool_id=?
            """,
            (graph["user_id"], NOW_3, graph["tool_id"]),
        )
        connection.execute(
            """
            UPDATE mcp_server_operations SET status='succeeded',completed_at=?,updated_at=?
            WHERE mcp_server_operation_id=?
            """,
            (NOW_3, NOW_3, operation_id),
        )
        operation_before = {
            "mcp_server_operation_id": operation_id,
            "mcp_server_id": graph["server_id"],
            "operation_type": "refresh",
            "status": "processing",
            "start_server_revision": 2,
            "candidate_credential_revision": None,
            "error_code": None,
        }
        operation_after = {**operation_before, "status": "succeeded"}
        _insert_mcp_audit(
            connection,
            graph,
            "operation-completed",
            action="mcp.server_operation_completed",
            object_type="mcp_server_operation",
            object_id=operation_id,
            before=operation_before,
            after=operation_after,
            request_id=operation_request,
            trace_id=operation_trace,
            created_at=NOW_3,
        )
        server_before = {
            "mcp_server_id": graph["server_id"],
            "status": "active",
            "state_revision": 2,
            "active_credential_revision": 1,
            "tool_count": 1,
            "active_tool_count": 1,
        }
        server_after = {
            **server_before,
            "state_revision": 3,
            "active_tool_count": 0,
        }
        _insert_mcp_audit(
            connection,
            graph,
            "server-refreshed",
            action="mcp.server_refreshed",
            object_type="mcp_server",
            object_id=graph["server_id"],
            before=server_before,
            after=server_after,
            request_id=operation_request,
            trace_id=operation_trace,
            created_at=NOW_3,
        )
        tool_before = {
            "mcp_tool_id": graph["tool_id"],
            "mcp_server_id": graph["server_id"],
            "status": "active",
            "schema_revision": 1,
            "policy_revision": 2,
            "risk_level": "standard",
            "approval_required": False,
            "required_permission_count": 0,
            "role_grant_count": 1,
            "user_grant_count": 0,
            "redaction_count": 0,
        }
        tool_after = {
            **tool_before,
            "status": "review_required",
            "schema_revision": 2,
            "policy_revision": 3,
        }
        _insert_mcp_audit(
            connection,
            graph,
            "tool-refreshed",
            action="mcp.tool_refreshed",
            object_type="mcp_tool",
            object_id=graph["tool_id"],
            before=tool_before,
            after=tool_after,
            request_id=operation_request,
            trace_id=operation_trace,
            created_at=NOW_3,
        )

        policy_time = "2026-08-16T00:00:04Z"
        policy_before = {
            key: tool_after[key]
            for key in (
                "mcp_tool_id",
                "mcp_server_id",
                "status",
                "schema_revision",
                "policy_revision",
                "risk_level",
                "approval_required",
                "required_permission_count",
                "redaction_count",
            )
        }
        connection.execute(
            """
            UPDATE mcp_tools SET status='active',policy_revision=4,updated_by=?,updated_at=?
            WHERE mcp_tool_id=?
            """,
            (graph["user_id"], policy_time, graph["tool_id"]),
        )
        policy_after = {**policy_before, "status": "active", "policy_revision": 4}
        _insert_mcp_audit(
            connection,
            graph,
            "tool-policy",
            action="mcp.tool_policy_updated",
            object_type="mcp_tool",
            object_id=graph["tool_id"],
            before=policy_before,
            after=policy_after,
            request_id="req_prd082_audit_policy",
            trace_id="trc_prd082_audit_policy",
            created_at=policy_time,
        )

        grant_time = "2026-08-16T00:00:05Z"
        grants_before = {
            "mcp_tool_id": graph["tool_id"],
            "mcp_server_id": graph["server_id"],
            "policy_revision": 4,
            "role_grant_count": 1,
            "user_grant_count": 0,
        }
        connection.execute(
            """
            INSERT INTO mcp_tool_user_grants(
              mcp_tool_user_grant_id,tenant_id,mcp_tool_id,user_id,created_by,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                _id("mcpug_", "audit-user-grant"),
                graph["tenant_id"],
                graph["tool_id"],
                graph["user_id"],
                graph["user_id"],
                grant_time,
            ),
        )
        connection.execute(
            """
            UPDATE mcp_tools SET policy_revision=5,updated_by=?,updated_at=?
            WHERE mcp_tool_id=?
            """,
            (graph["user_id"], grant_time, graph["tool_id"]),
        )
        grants_after = {
            **grants_before,
            "policy_revision": 5,
            "user_grant_count": 1,
        }
        _insert_mcp_audit(
            connection,
            graph,
            "tool-grants",
            action="mcp.tool_grants_updated",
            object_type="mcp_tool",
            object_id=graph["tool_id"],
            before=grants_before,
            after=grants_after,
            request_id="req_prd082_audit_grants",
            trace_id="trc_prd082_audit_grants",
            created_at=grant_time,
        )

        registered_at = "2026-08-16T00:00:06Z"
        registered_server_id = _id("mcps_", "audit-register-server")
        registered_credential_id = _id("mcpc_", "audit-register-credential")
        registered_operation_id = _id("mcpo_", "audit-register-operation")
        connection.execute(
            """
            INSERT INTO mcp_servers(
              mcp_server_id,tenant_id,name,transport,endpoint_ciphertext,
              endpoint_encryption_key_version,endpoint_hmac,endpoint_hmac_key_version,
              endpoint_scheme,endpoint_host_hint,endpoint_has_path,active_credential_revision,
              protocol_version,capabilities_sha256,status,state_revision,created_by,updated_by,
              created_at,updated_at
            ) VALUES(?,?,'register-audit','streamable_http','enc_endpoint',1,?,1,
                     'https','register.example.invalid',0,NULL,NULL,NULL,'pending_sync',1,
                     ?,?,?,?)
            """,
            (
                registered_server_id,
                graph["tenant_id"],
                _digest("audit-register-endpoint"),
                graph["user_id"],
                graph["user_id"],
                registered_at,
                registered_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO mcp_server_credentials(
              mcp_server_credential_id,tenant_id,mcp_server_id,credential_revision,
              auth_type,credential_ciphertext,credential_encryption_key_version,
              credential_hmac,credential_hmac_key_version,status,probe_operation_id,
              created_by,created_at,activated_at,rejected_at
            ) VALUES(?,?,?,1,'none',NULL,NULL,NULL,NULL,'candidate',NULL,?,?,NULL,NULL)
            """,
            (
                registered_credential_id,
                graph["tenant_id"],
                registered_server_id,
                graph["user_id"],
                registered_at,
            ),
        )
        register_request = "req_prd082_audit_register"
        register_trace = "trc_prd082_audit_register"
        connection.execute(
            """
            INSERT INTO mcp_server_operations(
              mcp_server_operation_id,tenant_id,mcp_server_id,operation_type,
              idempotency_key_hmac,idempotency_key_hmac_version,intent_hmac,
              intent_hmac_version,actor_user_id,start_server_revision,
              start_credential_revision,candidate_credential_revision,status,error_code,
              request_id,trace_id,created_at,completed_at,updated_at
            ) VALUES(?,?,?,'register',?,1,?,1,?,1,NULL,1,'queued',NULL,?,?,?,NULL,?)
            """,
            (
                registered_operation_id,
                graph["tenant_id"],
                registered_server_id,
                _digest("audit-register-operation-key"),
                _digest("audit-register-operation-intent"),
                graph["user_id"],
                register_request,
                register_trace,
                registered_at,
                registered_at,
            ),
        )
        registered_after = {
            "mcp_server_id": registered_server_id,
            "status": "pending_sync",
            "state_revision": 1,
            "active_credential_revision": None,
            "tool_count": 0,
            "active_tool_count": 0,
        }
        _insert_mcp_audit(
            connection,
            graph,
            "server-registered",
            action="mcp.server_registered",
            object_type="mcp_server",
            object_id=registered_server_id,
            before={},
            after=registered_after,
            request_id=register_request,
            trace_id=register_trace,
            created_at=registered_at,
        )
        connection.commit()

        unsafe = dict(server_after)
        unsafe["endpoint_url"] = "not-evidence"
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            _insert_mcp_audit(
                connection,
                graph,
                "unsafe-server-field",
                action="mcp.server_refreshed",
                object_type="mcp_server",
                object_id=graph["server_id"],
                before=server_before,
                after=unsafe,
                request_id=operation_request,
                trace_id=operation_trace,
                created_at=NOW_3,
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            _insert_mcp_audit(
                connection,
                graph,
                "forged-operation-actor",
                action="mcp.server_operation_completed",
                object_type="mcp_server_operation",
                object_id=operation_id,
                before=operation_before,
                after=operation_after,
                request_id=operation_request,
                trace_id=operation_trace,
                created_at=NOW_3,
                actor_email="forged@example.invalid",
            )
        connection.rollback()
        forged_tool = dict(tool_after)
        forged_tool["required_permission_count"] = 1
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            _insert_mcp_audit(
                connection,
                graph,
                "forged-tool-count",
                action="mcp.tool_refreshed",
                object_type="mcp_tool",
                object_id=graph["tool_id"],
                before=tool_before,
                after=forged_tool,
                request_id=operation_request,
                trace_id=operation_trace,
                created_at=NOW_3,
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            _insert_mcp_audit(
                connection,
                graph,
                "forged-changed-fields",
                action="mcp.tool_grants_updated",
                object_type="mcp_tool",
                object_id=graph["tool_id"],
                before=grants_before,
                after=grants_after,
                request_id="req_prd082_audit_grants_forged",
                trace_id="trc_prd082_audit_grants_forged",
                created_at=grant_time,
                changed_fields=[],
            )
        connection.rollback()

        inactive_operations: dict[str, tuple[str, str, str]] = {}
        for outcome in ("failed", "succeeded"):
            inactive_operation_id = _id("mcpo_", f"inactive-actor-{outcome}")
            inactive_request = f"req_prd082_inactive_actor_{outcome}"
            inactive_trace = f"trc_prd082_inactive_actor_{outcome}"
            connection.execute(
                """
                INSERT INTO mcp_server_operations(
                  mcp_server_operation_id,tenant_id,mcp_server_id,operation_type,
                  idempotency_key_hmac,idempotency_key_hmac_version,intent_hmac,
                  intent_hmac_version,actor_user_id,start_server_revision,
                  start_credential_revision,candidate_credential_revision,status,error_code,
                  request_id,trace_id,created_at,completed_at,updated_at
                ) VALUES(?,?,?,'refresh',?,1,?,1,?,3,1,NULL,'queued',NULL,?,?,?,NULL,?)
                """,
                (
                    inactive_operation_id,
                    graph["tenant_id"],
                    graph["server_id"],
                    _digest(f"inactive-actor-{outcome}-key"),
                    _digest(f"inactive-actor-{outcome}-intent"),
                    graph["user_id"],
                    inactive_request,
                    inactive_trace,
                    "2026-08-16T00:00:07Z",
                    "2026-08-16T00:00:07Z",
                ),
            )
            connection.execute(
                "UPDATE mcp_server_operations SET status='processing',updated_at=? "
                "WHERE mcp_server_operation_id=?",
                ("2026-08-16T00:00:08Z", inactive_operation_id),
            )
            inactive_operations[outcome] = (
                inactive_operation_id,
                inactive_request,
                inactive_trace,
            )
        connection.execute(
            "UPDATE users SET status='disabled',updated_at=? "
            "WHERE tenant_id=? AND user_id=?",
            ("2026-08-16T00:00:09Z", graph["tenant_id"], graph["user_id"]),
        )
        failed_at = "2026-08-16T00:00:10Z"
        succeeded_at = "2026-08-16T00:00:11Z"
        connection.execute(
            """
            UPDATE mcp_server_operations SET status='failed',error_code='mcp.state_conflict',
              completed_at=?,updated_at=? WHERE mcp_server_operation_id=?
            """,
            (failed_at, failed_at, inactive_operations["failed"][0]),
        )
        connection.execute(
            """
            UPDATE mcp_server_operations SET status='succeeded',completed_at=?,updated_at=?
            WHERE mcp_server_operation_id=?
            """,
            (succeeded_at, succeeded_at, inactive_operations["succeeded"][0]),
        )
        connection.commit()

        inactive_failed_before = {
            "mcp_server_operation_id": inactive_operations["failed"][0],
            "mcp_server_id": graph["server_id"],
            "operation_type": "refresh",
            "status": "processing",
            "start_server_revision": 3,
            "candidate_credential_revision": None,
            "error_code": None,
        }
        inactive_failed_after = {
            **inactive_failed_before,
            "status": "failed",
            "error_code": "mcp.state_conflict",
        }
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            _insert_mcp_audit(
                connection,
                graph,
                "inactive-failed-forged-identity",
                action="mcp.server_operation_completed",
                object_type="mcp_server_operation",
                object_id=inactive_operations["failed"][0],
                before=inactive_failed_before,
                after=inactive_failed_after,
                request_id=inactive_operations["failed"][1],
                trace_id=inactive_operations["failed"][2],
                created_at=failed_at,
                actor_email="forged@example.invalid",
            )
        connection.rollback()
        _insert_mcp_audit(
            connection,
            graph,
            "inactive-failed-exact-owner",
            action="mcp.server_operation_completed",
            object_type="mcp_server_operation",
            object_id=inactive_operations["failed"][0],
            before=inactive_failed_before,
            after=inactive_failed_after,
            request_id=inactive_operations["failed"][1],
            trace_id=inactive_operations["failed"][2],
            created_at=failed_at,
        )
        connection.commit()

        inactive_succeeded_before = {
            **inactive_failed_before,
            "mcp_server_operation_id": inactive_operations["succeeded"][0],
        }
        inactive_succeeded_after = {
            **inactive_succeeded_before,
            "status": "succeeded",
        }
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            _insert_mcp_audit(
                connection,
                graph,
                "inactive-succeeded-rejected",
                action="mcp.server_operation_completed",
                object_type="mcp_server_operation",
                object_id=inactive_operations["succeeded"][0],
                before=inactive_succeeded_before,
                after=inactive_succeeded_after,
                request_id=inactive_operations["succeeded"][1],
                trace_id=inactive_operations["succeeded"][2],
                created_at=succeeded_at,
            )
        connection.rollback()
        assert connection.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action='mcp.server_operation_completed' AND object_id=?
            """,
            (inactive_operations["failed"][0],),
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action='mcp.server_operation_completed' AND object_id=?
            """,
            (inactive_operations["succeeded"][0],),
        ).fetchone()[0] == 0
        _assert_healthy(connection)


@pytest.mark.parametrize("fact_kind", ("server", "custom_mcp_grant"))
def test_prd082_down_guards_are_atomic(
    prd082_templates: dict[str, Path], tmp_path: Path, fact_kind: str
) -> None:
    database_url = _clone_0074(
        prd082_templates, tmp_path / f"down-guard-{fact_kind}.sqlite3"
    )
    _apply_0075(prd082_templates, database_url)
    with connect_database(database_url) as connection:
        if fact_kind == "server":
            graph = _insert_server_tool(connection, "down-guard")
            expected_id = graph["server_id"]
        else:
            identity = _identity(connection)
            role = connection.execute(
                "SELECT role_id FROM roles WHERE tenant_id=? AND code='finance_admin'",
                (identity["tenant_id"],),
            ).fetchone()
            permission = connection.execute(
                "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='mcp.read'",
                (identity["tenant_id"],),
            ).fetchone()
            assert role is not None and permission is not None
            existing = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT permission.code FROM role_permissions relation
                    JOIN permissions permission
                      ON permission.tenant_id=relation.tenant_id
                     AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND relation.role_id=?
                    ORDER BY permission.code
                    """,
                    (identity["tenant_id"], role[0]),
                )
            ]
            owner_trigger = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' "
                "AND name='trg_role_permissions_owner_insert_prd059'"
            ).fetchone()
            assert owner_trigger is not None and owner_trigger[0]
            connection.execute("DROP TRIGGER trg_role_permissions_owner_insert_prd059")
            connection.execute(
                """
                INSERT INTO role_permissions(
                  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    "rperm_prd082_custom_mcp",
                    identity["tenant_id"],
                    role[0],
                    permission[0],
                    NOW,
                    NOW,
                ),
            )
            connection.execute(str(owner_trigger[0]))
            connection.execute(
                "UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=? "
                "WHERE tenant_id=? AND role_id=?",
                (
                    permission_matrix_sha256((*existing, "mcp.read")),
                    identity["tenant_id"],
                    role[0],
                ),
            )
            connection.commit()
            expected_id = "rperm_prd082_custom_mcp"
        before = _schema_snapshot(connection, ("mcp_servers", "approval_tasks", "audit_logs"))
        _assert_healthy(connection)

    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd082_templates["migrations_0075"],
            expected_version="0075",
        )
    assert error.value.code == "db.migration_failed"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0075"
        assert _schema_snapshot(
            connection, ("mcp_servers", "approval_tasks", "audit_logs")
        ) == before
        if fact_kind == "server":
            assert connection.execute(
                "SELECT COUNT(*) FROM mcp_servers WHERE mcp_server_id=?", (expected_id,)
            ).fetchone()[0] == 1
        else:
            assert connection.execute(
                "SELECT COUNT(*) FROM role_permissions WHERE role_permission_id=?",
                (expected_id,),
            ).fetchone()[0] == 1
        _assert_healthy(connection)

    down_sql = (
        prd082_templates["migrations_0075"]
        / "0075_mcp_tool_registration_permissions.down.sql"
    ).read_text(encoding="utf-8")
    for table in MCP_TABLES:
        assert table in down_sql
    for marker in (
        "mcp_tool_invocation",
        "mcp_invocation_enqueue",
        "mcp.tool_called",
        "action LIKE 'mcp.%'",
    ):
        assert marker in down_sql
