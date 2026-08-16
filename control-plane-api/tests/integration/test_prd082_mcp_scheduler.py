"""PRD-082 MCP Scheduler audit and approval authorization boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import metahub_control.mcp_governance.scheduler as scheduler
import pytest
from metahub_control.approvals.service import mcp_approval_target_coordinates
from metahub_control.mcp_governance.executor import (
    McpCallResult,
    McpConnectionMaterial,
    McpDiscoveredTool,
    McpDiscoveryResult,
    McpExecutorError,
    McpProbeResult,
)
from metahub_control.mcp_governance.scheduler import (
    McpInvocationClaim,
    McpSyncJobClaim,
    _approval_authorization_failure,
    _finalize_sync_failure,
    _finalize_sync_success,
)
from metahub_control.mcp_governance.service import DlpDecision
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.mcp import (
    McpCanonicalJson,
    McpSchemaArtifact,
    McpSchemaBundle,
    McpSchemaLimits,
)

CREATED_AT = "2026-08-15T00:00:00Z"
ACTIVE_AT = "2026-08-15T00:00:01Z"
LEASE_OWNER = "prd082-audit-worker"
LEASE_TOKEN = hashlib.sha256(b"prd082-audit-lease").hexdigest()


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _digest(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def _id(prefix: str, marker: str) -> str:
    return prefix + _digest(marker)[:32]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _schema_artifact(marker: str) -> McpSchemaArtifact:
    value = {
        "additionalProperties": False,
        "properties": {"value": {"const": marker, "type": "string"}},
        "type": "object",
    }
    encoded = _canonical(value)
    digest = _digest(encoded)
    byte_count = len(encoded.encode("utf-8"))
    return McpSchemaArtifact(
        original=value,
        effective=value,
        original_json=encoded,
        effective_json=encoded,
        original_sha256=digest,
        effective_sha256=digest,
        original_byte_count=byte_count,
        effective_byte_count=byte_count,
    )


def _discovered_tool(name: str, marker: str) -> McpDiscoveredTool:
    annotations_json = "{}"
    return McpDiscoveredTool(
        name=name,
        description=f"Metadata {marker}",
        schema_bundle=McpSchemaBundle(input=_schema_artifact(marker), output=None),
        annotations=McpCanonicalJson(
            value={},
            canonical_json=annotations_json,
            sha256=_digest(annotations_json),
            byte_count=len(annotations_json),
        ),
    )


@pytest.fixture(scope="module")
def prd082_scheduler_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd082-scheduler-template") / "template.sqlite3"
    database_url = _url(database)
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = "Prd082SchedulerOnly!2026"
    try:
        applied = apply_migrations(database_url).applied
        assert applied and applied[-1] == "0076"
        apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous
    return database


@pytest.fixture
def scheduler_database(
    tmp_path: Path,
    prd082_scheduler_template: Path,
) -> Path:
    database = tmp_path / "scheduler.sqlite3"
    shutil.copy2(prd082_scheduler_template, database)
    return database


def _insert_existing_tool(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    server_id: str,
    actor_id: str,
    tool: McpDiscoveredTool,
) -> str:
    tool_id = _id("mcpt_", f"scheduler-{tool.name}")
    input_schema = tool.schema_bundle.input
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
        ) VALUES(?,?,?,?,NULL,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?,1,1,'standard',0,3600,
                 '[]','[]','review_required',?,?,?,?)
        """,
        (
            tool_id,
            tenant_id,
            server_id,
            tool.name,
            tool.description,
            input_schema.original_json,
            input_schema.original_sha256,
            input_schema.effective_json,
            input_schema.effective_sha256,
            tool.annotations.canonical_json,
            tool.annotations.sha256,
            actor_id,
            actor_id,
            ACTIVE_AT,
            ACTIVE_AT,
        ),
    )
    return tool_id


def _prepare_sync_job(
    database: Path,
    *,
    marker: str,
    with_tools: bool,
    tenant_slug: str = "default",
    claim_job: bool = True,
    deadline_seconds: int | None = None,
) -> tuple[SimpleNamespace, McpSyncJobClaim, dict[str, str]]:
    database_url = _url(database)
    connection = connect_database(database_url)
    identity = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,user.email,user.role
        FROM tenants tenant JOIN users user ON user.tenant_id=tenant.tenant_id
        WHERE tenant.slug=? AND user.role='admin' AND user.status='active'
        ORDER BY user.user_id LIMIT 1
        """,
        (tenant_slug,),
    ).fetchone()
    assert identity is not None
    tenant_id = str(identity["tenant_id"])
    actor_id = str(identity["user_id"])
    server_id = _id("mcps_", f"scheduler-server-{marker}")
    credential_id = _id("mcpc_", f"scheduler-credential-{marker}")
    operation_id = _id("mcpo_", f"scheduler-operation-{marker}")
    job_id = _id("mcpj_", f"scheduler-job-{marker}")
    request_id = f"req_prd082_scheduler_{marker}"
    trace_id = f"trc_prd082_scheduler_{marker}"
    database_now_row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    assert database_now_row is not None
    claimed_at = datetime.strptime(str(database_now_row[0]), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    lease_expires_at = claimed_at + timedelta(minutes=10)
    deadline_at = claimed_at + (
        timedelta(seconds=deadline_seconds) if deadline_seconds is not None else timedelta(hours=1)
    )

    connection.execute(
        """
        INSERT INTO mcp_servers(
          mcp_server_id,tenant_id,name,transport,endpoint_ciphertext,
          endpoint_encryption_key_version,endpoint_hmac,endpoint_hmac_key_version,
          endpoint_scheme,endpoint_host_hint,endpoint_has_path,
          active_credential_revision,protocol_version,capabilities_sha256,status,
          state_revision,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,'Scheduler audit fixture','streamable_http','opaque-endpoint',
                 1,?,1,'https','m***.invalid',0,NULL,NULL,NULL,'pending_sync',1,?,?,?,?)
        """,
        (
            server_id,
            tenant_id,
            _digest(f"scheduler-endpoint-{marker}"),
            actor_id,
            actor_id,
            CREATED_AT,
            CREATED_AT,
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
        (credential_id, tenant_id, server_id, actor_id, CREATED_AT),
    )
    connection.execute(
        """
        UPDATE mcp_server_credentials SET status='active',activated_at=?
        WHERE tenant_id=? AND mcp_server_credential_id=?
        """,
        (ACTIVE_AT, tenant_id, credential_id),
    )
    connection.execute(
        """
        UPDATE mcp_servers
        SET active_credential_revision=1,protocol_version='2025-03-26',
            capabilities_sha256=?,status='active',state_revision=2,
            updated_by=?,updated_at=?
        WHERE tenant_id=? AND mcp_server_id=?
        """,
        (_digest("old-capabilities"), actor_id, ACTIVE_AT, tenant_id, server_id),
    )
    tool_ids: dict[str, str] = {}
    if with_tools:
        for tool in (
            _discovered_tool("unchanged_tool", "unchanged"),
            _discovered_tool("changed_tool", "old"),
            _discovered_tool("removed_tool", "removed"),
        ):
            tool_ids[tool.name] = _insert_existing_tool(
                connection,
                tenant_id=tenant_id,
                server_id=server_id,
                actor_id=actor_id,
                tool=tool,
            )
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
            tenant_id,
            server_id,
            _digest(f"scheduler-key-{marker}"),
            _digest(f"scheduler-intent-{marker}"),
            actor_id,
            request_id,
            trace_id,
            ACTIVE_AT,
            ACTIVE_AT,
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
            tenant_id,
            server_id,
            operation_id,
            ACTIVE_AT,
            _timestamp(deadline_at),
            ACTIVE_AT,
            ACTIVE_AT,
        ),
    )
    if claim_job:
        connection.execute(
            """
            UPDATE mcp_server_sync_jobs
            SET status='processing',attempt_count=1,lease_generation=1,
                lease_owner_id=?,lease_token=?,lease_expires_at=?,next_attempt_at=NULL,
                started_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_server_sync_job_id=? AND status='pending'
            """,
            (
                LEASE_OWNER,
                LEASE_TOKEN,
                _timestamp(lease_expires_at),
                _timestamp(claimed_at),
                _timestamp(claimed_at),
                tenant_id,
                job_id,
            ),
        )
        connection.execute(
            """
            UPDATE mcp_server_operations SET status='processing',updated_at=?
            WHERE tenant_id=? AND mcp_server_operation_id=? AND status='queued'
            """,
            (_timestamp(claimed_at), tenant_id, operation_id),
        )
    connection.commit()
    connection.close()
    claim = McpSyncJobClaim(
        tenant_id=tenant_id,
        job_id=job_id,
        server_id=server_id,
        operation_id=operation_id,
        job_type="discover",
        candidate_credential_revision=None,
        attempt_count=1 if claim_job else 0,
        lease_generation=1 if claim_job else 0,
        lease_owner_id=LEASE_OWNER,
        lease_token=LEASE_TOKEN,
        lease_expires_at=lease_expires_at,
        deadline_at=deadline_at,
    )
    ids = {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "actor_email": str(identity["email"]),
        "actor_role": str(identity["role"]),
        "server_id": server_id,
        "operation_id": operation_id,
        "request_id": request_id,
        "trace_id": trace_id,
        **tool_ids,
    }
    return SimpleNamespace(database_url=database_url), claim, ids


def _audit_rows(database: Path, request_id: str) -> list[sqlite3.Row]:
    connection = connect_database(_url(database))
    try:
        return connection.execute(
            """
            SELECT * FROM audit_logs WHERE request_id=?
            ORDER BY action,object_id
            """,
            (request_id,),
        ).fetchall()
    finally:
        connection.close()


def test_scheduler_sync_success_audits_only_operation_and_changed_tools(
    scheduler_database: Path,
) -> None:
    config, claim, ids = _prepare_sync_job(
        scheduler_database,
        marker="success",
        with_tools=True,
    )
    result = McpDiscoveryResult(
        protocol_version="2025-06-18",
        capabilities_json=_canonical({"tools": True}),
        capabilities_sha256=_digest(_canonical({"tools": True})),
        tools=(
            _discovered_tool("unchanged_tool", "unchanged"),
            _discovered_tool("changed_tool", "new"),
            _discovered_tool("new_tool", "inserted"),
        ),
        request_bytes=32,
        response_bytes=64,
    )

    _finalize_sync_success(config, claim, result)  # type: ignore[arg-type]

    rows = _audit_rows(scheduler_database, ids["request_id"])
    assert [row["action"] for row in rows].count("mcp.server_operation_completed") == 1
    assert [row["action"] for row in rows].count("mcp.server_refreshed") == 1
    assert [row["action"] for row in rows].count("mcp.tool_refreshed") == 3
    connection = connect_database(_url(scheduler_database))
    try:
        operation = connection.execute(
            "SELECT * FROM mcp_server_operations WHERE mcp_server_operation_id=?",
            (ids["operation_id"],),
        ).fetchone()
        tools = {
            str(row["remote_name"]): row
            for row in connection.execute(
                "SELECT * FROM mcp_tools WHERE tenant_id=? AND mcp_server_id=?",
                (ids["tenant_id"], ids["server_id"]),
            ).fetchall()
        }
        server = connection.execute(
            "SELECT * FROM mcp_servers WHERE tenant_id=? AND mcp_server_id=?",
            (ids["tenant_id"], ids["server_id"]),
        ).fetchone()
    finally:
        connection.close()
    assert operation is not None
    assert server is not None
    assert operation["status"] == "succeeded"
    audited_tool_ids = {
        str(row["object_id"]) for row in rows if row["action"] == "mcp.tool_refreshed"
    }
    assert str(tools["unchanged_tool"]["mcp_tool_id"]) not in audited_tool_ids
    assert audited_tool_ids == {
        str(tools[name]["mcp_tool_id"]) for name in ("changed_tool", "removed_tool", "new_tool")
    }
    assert tools["changed_tool"]["schema_revision"] == 2
    assert tools["changed_tool"]["policy_revision"] == 2
    assert tools["removed_tool"]["status"] == "disabled"
    assert tools["new_tool"]["status"] == "review_required"
    assert tools["unchanged_tool"]["updated_at"] == operation["completed_at"]

    allowed_operation = {
        "mcp_server_operation_id",
        "mcp_server_id",
        "operation_type",
        "status",
        "start_server_revision",
        "candidate_credential_revision",
        "error_code",
    }
    allowed_tool = {
        "mcp_tool_id",
        "mcp_server_id",
        "status",
        "schema_revision",
        "policy_revision",
        "risk_level",
        "approval_required",
        "required_permission_count",
        "role_grant_count",
        "user_grant_count",
        "redaction_count",
    }
    allowed_server = {
        "mcp_server_id",
        "status",
        "state_revision",
        "active_credential_revision",
        "tool_count",
        "active_tool_count",
    }
    for row in rows:
        before = json.loads(row["before_summary_json"])
        after = json.loads(row["after_summary_json"])
        if row["object_type"] == "mcp_server_operation":
            allowed = allowed_operation
        elif row["object_type"] == "mcp_server":
            allowed = allowed_server
        else:
            allowed = allowed_tool
        assert set(before) <= allowed
        assert set(after) == allowed
        assert row["object_name"] is None
        assert row["client_ip"] is None
        assert row["user_agent"] is None
        assert row["actor_user_id"] == ids["actor_id"]
        assert row["actor_email"] == ids["actor_email"]
        assert row["actor_role"] == ids["actor_role"]
        assert row["request_id"] == ids["request_id"]
        assert row["trace_id"] == ids["trace_id"]
        if row["object_type"] == "mcp_server_operation":
            assert before["status"] == "processing"
            assert after["status"] == "succeeded"
            assert row["created_at"] == operation["completed_at"]
        elif row["object_type"] == "mcp_server":
            assert before["state_revision"] == 2
            assert after["state_revision"] == 3
            assert row["created_at"] == server["updated_at"]
        else:
            assert (
                row["created_at"]
                == tools[
                    next(
                        name
                        for name, tool in tools.items()
                        if tool["mcp_tool_id"] == row["object_id"]
                    )
                ]["updated_at"]
            )


def test_scheduler_sync_failure_audits_terminal_operation(
    scheduler_database: Path,
) -> None:
    config, claim, ids = _prepare_sync_job(
        scheduler_database,
        marker="failure",
        with_tools=False,
    )

    _finalize_sync_failure(  # type: ignore[arg-type]
        config,
        claim,
        error_code="mcp.sync_failed",
    )

    rows = _audit_rows(scheduler_database, ids["request_id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "mcp.server_operation_completed"
    before = json.loads(row["before_summary_json"])
    after = json.loads(row["after_summary_json"])
    assert before["status"] == "processing"
    assert before["error_code"] is None
    assert after["status"] == "failed"
    assert after["error_code"] == "mcp.sync_failed"
    assert row["actor_user_id"] == ids["actor_id"]
    assert row["actor_email"] == ids["actor_email"]
    assert row["actor_role"] == ids["actor_role"]
    assert row["trace_id"] == ids["trace_id"]
    connection = connect_database(_url(scheduler_database))
    try:
        operation = connection.execute(
            "SELECT completed_at FROM mcp_server_operations WHERE mcp_server_operation_id=?",
            (ids["operation_id"],),
        ).fetchone()
    finally:
        connection.close()
    assert operation is not None
    assert row["created_at"] == operation["completed_at"]


def test_scheduler_sync_network_budget_reserves_finalize_window() -> None:
    now = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
    job_deadline = now + timedelta(seconds=16)
    lease_deadline = now + timedelta(seconds=30)

    assert (
        scheduler._remaining_deadline_seconds(
            job_deadline,
            lease_expires_at=lease_deadline,
            configured=10,
            now=now,
        )
        == 10
    )
    assert (
        scheduler._remaining_deadline_seconds(
            job_deadline,
            lease_expires_at=now + timedelta(seconds=9),
            configured=10,
            now=now,
        )
        == 3
    )
    assert (
        scheduler._remaining_deadline_seconds(
            job_deadline,
            lease_expires_at=now + timedelta(seconds=6, milliseconds=999),
            configured=10,
            now=now,
        )
        is None
    )
    assert (
        scheduler._remaining_deadline_seconds(
            now + timedelta(seconds=200),
            lease_expires_at=now + timedelta(seconds=300),
            configured=500,
            now=now,
        )
        == 120
    )


def test_scheduler_sync_budget_is_computed_after_material_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    claim = McpSyncJobClaim(
        tenant_id="tenant_prd082_budget_order",
        job_id="mcpj_" + "1" * 32,
        server_id="mcps_" + "2" * 32,
        operation_id="mcpo_" + "3" * 32,
        job_type="discover",
        candidate_credential_revision=None,
        attempt_count=1,
        lease_generation=1,
        lease_owner_id="prd082-budget-order",
        lease_token="4" * 64,
        lease_expires_at=now + timedelta(seconds=30),
        deadline_at=now + timedelta(seconds=16),
    )
    config = SimpleNamespace(mcp_probe_deadline_seconds=10)
    material = McpConnectionMaterial(endpoint="opaque", bearer_token=None)
    result = McpDiscoveryResult(
        protocol_version="2025-03-26",
        capabilities_json="{}",
        capabilities_sha256=_digest("budget-order"),
        tools=(),
        request_bytes=11,
        response_bytes=19,
    )
    events: list[str] = []

    def load(*_args: object, **_kwargs: object) -> McpConnectionMaterial:
        events.append("load")
        return material

    budget_calls = 0

    def budget(
        deadline: datetime,
        *,
        lease_expires_at: datetime,
        configured: object,
        now: datetime | None = None,
    ) -> int:
        nonlocal budget_calls
        assert deadline == claim.deadline_at
        assert lease_expires_at == claim.lease_expires_at
        assert configured == 10
        assert now is None
        budget_calls += 1
        events.append("preflight" if budget_calls == 1 else "budget")
        return 7

    def discover(*_args: object, **_kwargs: object) -> McpDiscoveryResult:
        request = _args[1]
        assert isinstance(request, scheduler.McpDiscoverRequest)
        assert request.connection is material
        assert request.deadline_seconds == 7
        events.append("discover")
        return result

    def finalize(*_args: object, **_kwargs: object) -> None:
        events.append("finalize")

    monkeypatch.setattr(scheduler, "_load_sync_connection_material", load)
    monkeypatch.setattr(scheduler, "_remaining_deadline_seconds", budget)
    monkeypatch.setattr(scheduler, "discover_mcp_server", discover)
    monkeypatch.setattr(scheduler, "_finalize_sync_success", finalize)

    scheduler._process_sync_job(config, claim)  # type: ignore[arg-type]

    assert events == ["preflight", "load", "budget", "discover", "finalize"]


@pytest.mark.parametrize(
    ("job_type", "expected_error"),
    (
        ("discover", "mcp.sync_failed"),
        ("credential_probe", "mcp.credential_invalid"),
    ),
)
def test_scheduler_expired_sync_job_fails_without_loading_or_network(
    monkeypatch: pytest.MonkeyPatch,
    job_type: str,
    expected_error: str,
) -> None:
    now = datetime.now(UTC)
    claim = McpSyncJobClaim(
        tenant_id="tenant_prd082_expired_sync",
        job_id="mcpj_" + "5" * 32,
        server_id="mcps_" + "6" * 32,
        operation_id="mcpo_" + "7" * 32,
        job_type=job_type,
        candidate_credential_revision=1 if job_type == "credential_probe" else None,
        attempt_count=1,
        lease_generation=1,
        lease_owner_id="prd082-expired-sync",
        lease_token="8" * 64,
        lease_expires_at=now + timedelta(seconds=30),
        deadline_at=now - timedelta(seconds=1),
    )
    config = SimpleNamespace(mcp_probe_deadline_seconds=10)
    events: list[str] = []

    def unexpected(*_args: object, **_kwargs: object) -> None:
        events.append("unexpected")

    def finalize_failure(
        _config: object,
        actual_claim: McpSyncJobClaim,
        *,
        error_code: str,
    ) -> None:
        assert actual_claim is claim
        assert error_code == expected_error
        events.append("failure")

    monkeypatch.setattr(scheduler, "_load_sync_connection_material", unexpected)
    monkeypatch.setattr(scheduler, "discover_mcp_server", unexpected)
    monkeypatch.setattr(scheduler, "_finalize_sync_failure", finalize_failure)

    scheduler._process_sync_job(config, claim)  # type: ignore[arg-type]

    assert events == ["failure"]


def _scheduler_config(
    database: Path,
    *,
    tenant_concurrency: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        database_url=_url(database),
        mcp_enabled=True,
        mcp_scheduler_enabled=True,
        mcp_probe_deadline_seconds=30,
        mcp_operation_lease_seconds=120,
        mcp_invocation_lease_seconds=120,
        mcp_tenant_concurrency=tenant_concurrency,
        mcp_scheduler_poll_interval_seconds=1.0,
        mcp_schema_limits=McpSchemaLimits(),
    )


def _dlp(marker: str, *, status: str = "clean", match_count: int = 0) -> DlpDecision:
    return DlpDecision(
        status=status,
        match_count=match_count,
        rule_id=None,
        ruleset_hmac=_digest(f"dlp-rules-{marker}"),
        ruleset_hmac_key_version=7,
    )


def _prepare_runtime_invocation(
    database: Path,
    *,
    marker: str,
    redaction_pointers: tuple[str, ...] = (),
) -> tuple[
    SimpleNamespace,
    McpInvocationClaim,
    scheduler._InvocationMaterial,
    McpDiscoveredTool,
    dict[str, str],
]:
    sync_config, sync_claim, ids = _prepare_sync_job(
        database,
        marker=f"invocation-{marker}",
        with_tools=False,
    )
    _finalize_sync_failure(
        sync_config,  # type: ignore[arg-type]
        sync_claim,
        error_code="mcp.sync_failed",
    )
    connection = connect_database(_url(database))
    try:
        frozen_tool = _discovered_tool("runtime_tool", "frozen")
        tool_id = _insert_existing_tool(
            connection,
            tenant_id=ids["tenant_id"],
            server_id=ids["server_id"],
            actor_id=ids["actor_id"],
            tool=frozen_tool,
        )
        connection.execute(
            """
            INSERT INTO mcp_tool_user_grants(
              mcp_tool_user_grant_id,tenant_id,mcp_tool_id,user_id,created_by,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                _id("mcpug_", f"runtime-grant-{marker}"),
                ids["tenant_id"],
                tool_id,
                ids["actor_id"],
                ids["actor_id"],
                ACTIVE_AT,
            ),
        )
        pointers_json = _canonical(list(redaction_pointers))
        connection.execute(
            """
            UPDATE mcp_tools
            SET status='active',policy_revision=2,result_redaction_pointers_json=?,
                updated_by=?,updated_at=?
            WHERE tenant_id=? AND mcp_tool_id=?
            """,
            (
                pointers_json,
                ids["actor_id"],
                ACTIVE_AT,
                ids["tenant_id"],
                tool_id,
            ),
        )
        now_row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
        assert now_row is not None
        started_at = str(now_row[0])
        current = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        expires_at = _timestamp(current + timedelta(minutes=5))
        invocation_id = _id("mcpi_", f"runtime-invocation-{marker}")
        initial_dlp = _dlp(f"runtime-{marker}")
        invocation: dict[str, object] = {
            "mcp_invocation_id": invocation_id,
            "tenant_id": ids["tenant_id"],
            "mcp_server_id": ids["server_id"],
            "mcp_tool_id": tool_id,
            "requested_by": ids["actor_id"],
            "request_id": f"req_prd082_runtime_{marker}",
            "trace_id": f"trc_prd082_runtime_{marker}",
            "idempotency_key_hmac": _digest(f"runtime-idempotency-{marker}"),
            "idempotency_key_hmac_version": 1,
            "intent_hmac": _digest(f"runtime-intent-{marker}"),
            "intent_hmac_version": 1,
            "server_state_revision": 2,
            "credential_revision": 1,
            "schema_revision": 1,
            "policy_revision": 2,
            "policy_fingerprint": _digest(f"runtime-policy-{marker}"),
            "input_schema_original_sha256": frozen_tool.schema_bundle.input.original_sha256,
            "input_schema_effective_sha256": frozen_tool.schema_bundle.input.effective_sha256,
            "output_schema_original_sha256": None,
            "output_schema_effective_sha256": None,
            "risk_level": "standard",
            "approval_required": 0,
            "required_permissions_sha256": _digest(f"runtime-permissions-{marker}"),
            "grant_sha256": _digest(f"runtime-grants-{marker}"),
            "result_redaction_pointers_json": pointers_json,
            "arguments_ciphertext": "opaque-arguments",
            "arguments_encryption_key_version": 1,
            "arguments_bytes": 2,
            "arguments_hmac": _digest(f"runtime-arguments-{marker}"),
            "arguments_hmac_key_version": 1,
            "approval_task_id": None,
            "approval_deadline_at": None,
            "status": "queued",
            "state_revision": 1,
            "input_dlp_status": initial_dlp.status,
            "input_dlp_match_count": initial_dlp.match_count,
            "input_dlp_rule_id": initial_dlp.rule_id,
            "input_dlp_ruleset_hmac": initial_dlp.ruleset_hmac,
            "input_dlp_ruleset_hmac_key_version": initial_dlp.ruleset_hmac_key_version,
            "input_dlp_scanned_at": started_at,
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
            "duration_ms": None,
            "execution_status": "not_called",
            "invocation_count": 1,
            "tool_call_count": 0,
            "cost_status": "not_applicable",
            "cost_micros": None,
            "error_code": None,
            "lease_generation": 0,
            "lease_owner_id": None,
            "lease_token": None,
            "lease_expires_at": None,
            "next_attempt_at": started_at,
            "created_at": started_at,
            "queued_at": started_at,
            "started_at": None,
            "call_started_at": None,
            "completed_at": None,
            "updated_at": started_at,
        }
        columns = tuple(invocation)
        connection.execute(
            f"INSERT INTO mcp_invocations({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            tuple(invocation[column] for column in columns),
        )
        lease_token = _digest(f"runtime-probe-lease-{marker}")
        connection.execute(
            """
            UPDATE mcp_invocations
            SET status='probing',state_revision=2,probe_attempt_count=1,
                lease_generation=1,lease_owner_id=?,lease_token=?,lease_expires_at=?,
                next_attempt_at=NULL,started_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (
                LEASE_OWNER,
                lease_token,
                expires_at,
                started_at,
                started_at,
                ids["tenant_id"],
                invocation_id,
            ),
        )
        attempt_id = _id("mcpa_", f"runtime-probe-attempt-{marker}")
        connection.execute(
            """
            INSERT INTO mcp_invocation_attempts(
              mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
              attempt_number,status,lease_generation,error_code,request_bytes,
              response_bytes,started_at,call_started_at,completed_at,duration_ms
            ) VALUES(?,?,?,'probe',1,'processing',1,NULL,NULL,NULL,?,NULL,NULL,NULL)
            """,
            (attempt_id, ids["tenant_id"], invocation_id, started_at),
        )
        connection.commit()
    finally:
        connection.close()
    claim = McpInvocationClaim(
        tenant_id=ids["tenant_id"],
        invocation_id=invocation_id,
        server_id=ids["server_id"],
        tool_id=tool_id,
        attempt_id=attempt_id,
        attempt_number=1,
        lease_generation=1,
        lease_owner_id=LEASE_OWNER,
        lease_token=lease_token,
        lease_expires_at=datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC),
    )
    material = scheduler._InvocationMaterial(
        claim=claim,
        connection=McpConnectionMaterial(
            endpoint="opaque",
            bearer_token=None,
        ),
        arguments={"value": "opaque"},
        input_dlp=initial_dlp,
        remote_name=frozen_tool.name,
    )
    runtime_ids = {**ids, "tool_id": tool_id, "invocation_id": invocation_id}
    return _scheduler_config(database), claim, material, frozen_tool, runtime_ids


def _probe_result(tool: McpDiscoveredTool, *, drifted: bool = False) -> McpProbeResult:
    return McpProbeResult(
        protocol_version="2025-06-18" if drifted else "2025-03-26",
        capabilities_json="{}",
        capabilities_sha256=(
            _digest("runtime-drifted-capabilities") if drifted else _digest("old-capabilities")
        ),
        tool=_discovered_tool(tool.name, "drifted") if drifted else tool,
        request_bytes=13,
        response_bytes=17,
    )


def _ensure_scheduler_fact(database: Path, tenant_id: str) -> None:
    connection = connect_database(_url(database))
    try:
        row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
        assert row is not None
        timestamp = str(row[0])
        connection.execute(
            """
            INSERT OR IGNORE INTO mcp_scheduler_tenants(
              tenant_id,status,last_claimed_at,last_claim_sequence,lease_generation,
              lease_owner_id,lease_token,lease_expires_at,next_scan_at,created_at,updated_at
            ) VALUES(?,'idle',NULL,0,0,NULL,NULL,NULL,?,?,?)
            """,
            (tenant_id, timestamp, timestamp, timestamp),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_alternate_tenant(database: Path, marker: str) -> tuple[str, str]:
    tenant_id = f"tenant_prd082_{marker}"
    tenant_slug = f"prd082-{marker}"
    user_id = f"user_prd082_{marker}"
    connection = connect_database(_url(database))
    try:
        row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
        assert row is not None
        timestamp = str(row[0])
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,?,?,'internal_only','active',?,?)
            """,
            (tenant_id, tenant_slug, f"Scheduler {marker}", timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at,password_changed_at
            ) VALUES(?,?,?,'Scheduler actor','not-a-runtime-credential','admin','active',?,?,?)
            """,
            (
                user_id,
                tenant_id,
                f"scheduler-{marker}@example.invalid",
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    _ensure_scheduler_fact(database, tenant_id)
    return tenant_id, tenant_slug


@pytest.mark.parametrize("drift", ["actor", "server"])
def test_scheduler_sync_finalize_drift_is_one_network_attempt_and_state_conflict(
    scheduler_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    _unused, claim, ids = _prepare_sync_job(
        scheduler_database,
        marker=f"finalize-{drift}",
        with_tools=False,
    )
    config = _scheduler_config(scheduler_database)
    calls = 0

    monkeypatch.setattr(
        scheduler,
        "_load_sync_connection_material",
        lambda *_args, **_kwargs: McpConnectionMaterial(
            endpoint="opaque",
            bearer_token=None,
        ),
    )

    def discover(*_args: object, **_kwargs: object) -> McpDiscoveryResult:
        nonlocal calls
        calls += 1
        connection = connect_database(_url(scheduler_database))
        try:
            row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
            assert row is not None
            timestamp = str(row[0])
            if drift == "actor":
                connection.execute(
                    "UPDATE users SET status='disabled',updated_at=? "
                    "WHERE tenant_id=? AND user_id=?",
                    (timestamp, ids["tenant_id"], ids["actor_id"]),
                )
            else:
                connection.execute(
                    """
                    UPDATE mcp_servers
                    SET status='disabled',state_revision=state_revision+1,
                        updated_by=?,updated_at=?
                    WHERE tenant_id=? AND mcp_server_id=?
                    """,
                    (
                        ids["actor_id"],
                        timestamp,
                        ids["tenant_id"],
                        ids["server_id"],
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        return McpDiscoveryResult(
            protocol_version="2025-03-26",
            capabilities_json="{}",
            capabilities_sha256=_digest("old-capabilities"),
            tools=(),
            request_bytes=11,
            response_bytes=19,
        )

    monkeypatch.setattr(scheduler, "discover_mcp_server", discover)
    scheduler._process_sync_job(config, claim)  # type: ignore[arg-type]

    assert calls == 1
    connection = connect_database(_url(scheduler_database))
    try:
        job = connection.execute(
            "SELECT status,error_code FROM mcp_server_sync_jobs "
            "WHERE tenant_id=? AND mcp_server_sync_job_id=?",
            (ids["tenant_id"], claim.job_id),
        ).fetchone()
        operation = connection.execute(
            "SELECT status,error_code FROM mcp_server_operations "
            "WHERE tenant_id=? AND mcp_server_operation_id=?",
            (ids["tenant_id"], claim.operation_id),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(job) == ("failed", "mcp.state_conflict")
    assert tuple(operation) == ("failed", "mcp.state_conflict")


def test_scheduler_releases_fairness_lease_before_network_processing(
    scheduler_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unused, pending, ids = _prepare_sync_job(
        scheduler_database,
        marker="release-before-network",
        with_tools=False,
        claim_job=False,
    )
    _ensure_scheduler_fact(scheduler_database, ids["tenant_id"])
    config = _scheduler_config(scheduler_database)
    observed: list[str] = []

    def process(_config: object, _claim: McpSyncJobClaim, **_kwargs: object) -> None:
        connection = connect_database(_url(scheduler_database))
        try:
            row = connection.execute(
                "SELECT status FROM mcp_scheduler_tenants WHERE tenant_id=?",
                (ids["tenant_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        observed.append(str(row["status"]))

    monkeypatch.setattr(scheduler, "_process_sync_job", process)
    assert scheduler.run_mcp_scheduler_once(
        config,  # type: ignore[arg-type]
        worker_id="scheduler-release-proof",
    )
    assert observed == ["idle"]
    assert pending.job_id


def test_scheduler_tenant_concurrency_two_allows_two_active_claims(
    scheduler_database: Path,
) -> None:
    for marker in ("concurrency-two-a", "concurrency-two-b"):
        _unused, _pending, ids = _prepare_sync_job(
            scheduler_database,
            marker=marker,
            with_tools=False,
            claim_job=False,
        )
    _ensure_scheduler_fact(scheduler_database, ids["tenant_id"])
    config = _scheduler_config(scheduler_database, tenant_concurrency=2)

    first_lease = scheduler.claim_next_scheduler_tenant(config, worker_id="tenant-two-first")
    assert first_lease is not None
    first = scheduler._claim_next_sync_job(
        config,
        first_lease,
        worker_id="tenant-two-first",
        now=None,
    )
    assert first is not None
    scheduler._release_scheduler_tenant(config, first_lease, worked=True)

    second_lease = scheduler.claim_next_scheduler_tenant(config, worker_id="tenant-two-second")
    assert second_lease is not None
    second = scheduler._claim_next_sync_job(
        config,
        second_lease,
        worker_id="tenant-two-second",
        now=None,
    )
    assert second is not None
    assert first.tenant_id == second.tenant_id == ids["tenant_id"]
    assert first.job_id != second.job_id


def test_scheduler_tenant_concurrency_one_skips_capped_tenant_not_other_tenant(
    scheduler_database: Path,
) -> None:
    _unused, _pending, default_ids = _prepare_sync_job(
        scheduler_database,
        marker="concurrency-one-default-a",
        with_tools=False,
        claim_job=False,
    )
    _prepare_sync_job(
        scheduler_database,
        marker="concurrency-one-default-b",
        with_tools=False,
        claim_job=False,
    )
    _ensure_scheduler_fact(scheduler_database, default_ids["tenant_id"])
    other_tenant_id, other_slug = _insert_alternate_tenant(
        scheduler_database, "concurrency-one-other"
    )
    _prepare_sync_job(
        scheduler_database,
        marker="concurrency-one-other",
        with_tools=False,
        tenant_slug=other_slug,
        claim_job=False,
    )
    config = _scheduler_config(scheduler_database, tenant_concurrency=1)

    first_lease = scheduler.claim_next_scheduler_tenant(config, worker_id="tenant-one-first")
    assert first_lease is not None
    first = scheduler._claim_next_sync_job(
        config,
        first_lease,
        worker_id="tenant-one-first",
        now=None,
    )
    assert first is not None
    assert first.tenant_id == default_ids["tenant_id"]
    scheduler._release_scheduler_tenant(config, first_lease, worked=True)

    second_lease = scheduler.claim_next_scheduler_tenant(config, worker_id="tenant-one-second")
    assert second_lease is not None
    assert second_lease.tenant_id == other_tenant_id
    second = scheduler._claim_next_sync_job(
        config,
        second_lease,
        worker_id="tenant-one-second",
        now=None,
    )
    assert second is not None and second.tenant_id == other_tenant_id
    scheduler._release_scheduler_tenant(config, second_lease, worked=True)

    assert (
        scheduler.claim_next_scheduler_tenant(config, worker_id="tenant-one-capped-proof") is None
    )


def test_scheduler_probe_terminal_failure_persists_latest_input_dlp_facts(
    scheduler_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, claim, material, _tool, ids = _prepare_runtime_invocation(
        scheduler_database,
        marker="probe-terminal-dlp",
    )
    latest = _dlp("probe-terminal-latest", status="log_only", match_count=2)
    material = scheduler._InvocationMaterial(
        claim=material.claim,
        connection=material.connection,
        arguments=material.arguments,
        input_dlp=latest,
        remote_name=material.remote_name,
    )
    monkeypatch.setattr(scheduler, "_prepare_invocation", lambda *_args: material)

    def fail_probe(*_args: object, **_kwargs: object) -> McpProbeResult:
        raise McpExecutorError("mcp.executor_disabled")

    monkeypatch.setattr(scheduler, "probe_mcp_tool", fail_probe)
    scheduler._process_invocation(config, claim)  # type: ignore[arg-type]

    connection = connect_database(_url(scheduler_database))
    try:
        row = connection.execute(
            """
            SELECT status,error_code,input_dlp_status,input_dlp_match_count,
              input_dlp_rule_id,input_dlp_ruleset_hmac,
              input_dlp_ruleset_hmac_key_version,input_dlp_scanned_at
            FROM mcp_invocations WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert tuple(row)[:7] == (
        "failed",
        "mcp.executor_disabled",
        latest.status,
        latest.match_count,
        latest.rule_id,
        latest.ruleset_hmac,
        latest.ruleset_hmac_key_version,
    )
    assert row["input_dlp_scanned_at"]


def test_scheduler_pre_call_probe_drift_is_terminal_without_catalog_mutation(
    scheduler_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _claim, material, frozen_tool, ids = _prepare_runtime_invocation(
        scheduler_database,
        marker="probe-drift",
    )
    monkeypatch.setattr(scheduler, "_current_authorization_failure", lambda *_args: None)
    monkeypatch.setattr(
        scheduler, "require_actor_transaction_permissions", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(scheduler, "_scan_dlp", lambda *_args, **_kwargs: material.input_dlp)
    connection = connect_database(_url(scheduler_database))
    try:
        tool_before = tuple(
            connection.execute(
                "SELECT * FROM mcp_tools WHERE tenant_id=? AND mcp_tool_id=?",
                (ids["tenant_id"], ids["tool_id"]),
            ).fetchone()
        )
        server_before = tuple(
            connection.execute(
                "SELECT * FROM mcp_servers WHERE tenant_id=? AND mcp_server_id=?",
                (ids["tenant_id"], ids["server_id"]),
            ).fetchone()
        )
    finally:
        connection.close()

    assert (
        scheduler._begin_call_after_probe(
            config,  # type: ignore[arg-type]
            material,
            _probe_result(frozen_tool, drifted=True),
        )
        is None
    )

    connection = connect_database(_url(scheduler_database))
    try:
        invocation = connection.execute(
            "SELECT status,error_code,execution_status,tool_call_count "
            "FROM mcp_invocations WHERE tenant_id=? AND mcp_invocation_id=?",
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchone()
        tool_after = tuple(
            connection.execute(
                "SELECT * FROM mcp_tools WHERE tenant_id=? AND mcp_tool_id=?",
                (ids["tenant_id"], ids["tool_id"]),
            ).fetchone()
        )
        server_after = tuple(
            connection.execute(
                "SELECT * FROM mcp_servers WHERE tenant_id=? AND mcp_server_id=?",
                (ids["tenant_id"], ids["server_id"]),
            ).fetchone()
        )
    finally:
        connection.close()
    assert tuple(invocation) == ("failed", "mcp.state_conflict", "not_called", 0)
    assert tool_after == tool_before
    assert server_after == server_before


def test_scheduler_pre_call_rescan_can_advance_input_dlp_timestamp(
    scheduler_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, claim, material, frozen_tool, ids = _prepare_runtime_invocation(
        scheduler_database,
        marker="pre-call-dlp-rescan",
    )
    fixed_now = claim.lease_expires_at - timedelta(minutes=4, seconds=59)
    monkeypatch.setattr(
        scheduler,
        "_transaction_now",
        lambda _connection, override: override or fixed_now,
    )
    monkeypatch.setattr(scheduler, "_current_authorization_failure", lambda *_args: None)
    monkeypatch.setattr(
        scheduler, "require_actor_transaction_permissions", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(scheduler, "_scan_dlp", lambda *_args, **_kwargs: material.input_dlp)

    call_material = scheduler._begin_call_after_probe(
        config,  # type: ignore[arg-type]
        material,
        _probe_result(frozen_tool),
    )
    assert call_material is not None

    connection = connect_database(_url(scheduler_database))
    try:
        row = connection.execute(
            "SELECT status,input_dlp_scanned_at FROM mcp_invocations "
            "WHERE tenant_id=? AND mcp_invocation_id=?",
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == ("calling", _timestamp(fixed_now))


def test_scheduler_server_unavailable_after_call_intent_is_outcome_unknown(
    scheduler_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, claim, material, frozen_tool, ids = _prepare_runtime_invocation(
        scheduler_database,
        marker="call-server-unavailable",
    )
    fixed_now = claim.lease_expires_at - timedelta(minutes=5)
    monkeypatch.setattr(
        scheduler,
        "_transaction_now",
        lambda _connection, override: override or fixed_now,
    )
    monkeypatch.setattr(scheduler, "_prepare_invocation", lambda *_args: material)
    monkeypatch.setattr(scheduler, "_current_authorization_failure", lambda *_args: None)
    monkeypatch.setattr(
        scheduler, "require_actor_transaction_permissions", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(scheduler, "_scan_dlp", lambda *_args, **_kwargs: material.input_dlp)
    monkeypatch.setattr(
        scheduler,
        "probe_mcp_tool",
        lambda *_args, **_kwargs: _probe_result(frozen_tool),
    )

    def fail_call(*_args: object, **_kwargs: object) -> McpCallResult:
        raise McpExecutorError("mcp.server_unavailable")

    monkeypatch.setattr(scheduler, "call_mcp_tool", fail_call)
    scheduler._process_invocation(config, claim)  # type: ignore[arg-type]

    connection = connect_database(_url(scheduler_database))
    try:
        invocation = connection.execute(
            """
            SELECT status,error_code,execution_status,tool_call_count,cost_status
            FROM mcp_invocations WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchone()
        attempt = connection.execute(
            """
            SELECT status,error_code FROM mcp_invocation_attempts
            WHERE tenant_id=? AND mcp_invocation_id=? AND attempt_kind='call'
            """,
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchone()
        event = connection.execute(
            """
            SELECT execution_status,tool_call_count,error_code
            FROM mcp_invocation_events WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(invocation) == (
        "outcome_unknown",
        "mcp.outcome_unknown",
        "unknown",
        None,
        "unknown",
    )
    assert tuple(attempt) == ("outcome_unknown", "mcp.outcome_unknown")
    assert tuple(event) == ("unknown", None, "mcp.outcome_unknown")


def test_scheduler_fourth_probe_can_publish_evt094_outcome_unknown(
    scheduler_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, first_claim, material, frozen_tool, ids = _prepare_runtime_invocation(
        scheduler_database,
        marker="fourth-probe-event",
    )
    _ensure_scheduler_fact(scheduler_database, ids["tenant_id"])
    clock = [first_claim.lease_expires_at - timedelta(minutes=5)]

    def controlled_now(
        _connection: sqlite3.Connection,
        override: datetime | None,
    ) -> datetime:
        return (override or clock[0]).astimezone(UTC).replace(microsecond=0)

    def prepare(
        _config: object,
        active_claim: McpInvocationClaim,
    ) -> scheduler._InvocationMaterial:
        return scheduler._InvocationMaterial(
            claim=active_claim,
            connection=material.connection,
            arguments=material.arguments,
            input_dlp=material.input_dlp,
            remote_name=material.remote_name,
        )

    probe_times: list[datetime] = []

    def probe(*_args: object, **_kwargs: object) -> McpProbeResult:
        probe_times.append(clock[0])
        if len(probe_times) <= 3:
            raise McpExecutorError("mcp.server_unavailable")
        return _probe_result(frozen_tool)

    call_times: list[datetime] = []

    def fail_call(*_args: object, **_kwargs: object) -> McpCallResult:
        call_times.append(clock[0])
        raise McpExecutorError("mcp.server_unavailable")

    monkeypatch.setattr(scheduler, "_transaction_now", controlled_now)
    monkeypatch.setattr(scheduler, "_prepare_invocation", prepare)
    monkeypatch.setattr(scheduler, "_current_authorization_failure", lambda *_args: None)
    monkeypatch.setattr(
        scheduler, "require_actor_transaction_permissions", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(scheduler, "_scan_dlp", lambda *_args, **_kwargs: material.input_dlp)
    monkeypatch.setattr(scheduler, "probe_mcp_tool", probe)
    monkeypatch.setattr(scheduler, "call_mcp_tool", fail_call)

    scheduler._process_invocation(config, first_claim)  # type: ignore[arg-type]
    active_claim = first_claim
    for attempt_number, delay in enumerate((5, 30, 300), start=2):
        clock[0] += timedelta(seconds=delay)
        tenant_lease = scheduler.claim_next_scheduler_tenant(
            config,  # type: ignore[arg-type]
            worker_id=f"fourth-probe-tenant-{attempt_number}",
            now=clock[0],
        )
        assert tenant_lease is not None
        active_claim = scheduler._claim_next_invocation(
            config,  # type: ignore[arg-type]
            tenant_lease,
            worker_id=f"fourth-probe-worker-{attempt_number}",
            now=clock[0],
        )
        assert active_claim is not None
        assert active_claim.attempt_number == attempt_number
        scheduler._release_scheduler_tenant(
            config,  # type: ignore[arg-type]
            tenant_lease,
            worked=True,
        )
        scheduler._process_invocation(config, active_claim)  # type: ignore[arg-type]

    connection = connect_database(_url(scheduler_database))
    try:
        invocation = connection.execute(
            """
            SELECT status,error_code,execution_status,probe_attempt_count,
              call_attempt_count,tool_call_count,cost_status
            FROM mcp_invocations WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchone()
        attempts = connection.execute(
            """
            SELECT attempt_kind,attempt_number,status,error_code
            FROM mcp_invocation_attempts
            WHERE tenant_id=? AND mcp_invocation_id=?
            ORDER BY CASE attempt_kind WHEN 'probe' THEN 0 ELSE 1 END,attempt_number
            """,
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchall()
        event = connection.execute(
            """
            SELECT mcp_invocation_event_id,event_type,status,error_code,execution_status,
              probe_attempt_count,call_attempt_count,tool_call_count,cost_status
            FROM mcp_invocation_events
            WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchone()
        outbox = connection.execute(
            """
            SELECT mcp_invocation_event_id,status,attempt_count,lease_generation,
              lease_owner_id,lease_token,lease_expires_at,error_code
            FROM mcp_invocation_event_outbox WHERE tenant_id=?
            """,
            (ids["tenant_id"],),
        ).fetchone()
    finally:
        connection.close()

    assert probe_times == [
        first_claim.lease_expires_at - timedelta(minutes=5),
        first_claim.lease_expires_at - timedelta(minutes=5) + timedelta(seconds=5),
        first_claim.lease_expires_at - timedelta(minutes=5) + timedelta(seconds=35),
        first_claim.lease_expires_at - timedelta(minutes=5) + timedelta(seconds=335),
    ]
    assert call_times == [probe_times[-1]]
    assert tuple(invocation) == (
        "outcome_unknown",
        "mcp.outcome_unknown",
        "unknown",
        4,
        1,
        None,
        "unknown",
    )
    assert [tuple(row) for row in attempts] == [
        ("probe", 1, "failed", "mcp.server_unavailable"),
        ("probe", 2, "failed", "mcp.server_unavailable"),
        ("probe", 3, "failed", "mcp.server_unavailable"),
        ("probe", 4, "succeeded", None),
        ("call", 1, "outcome_unknown", "mcp.outcome_unknown"),
    ]
    assert event is not None
    assert tuple(event)[1:] == (
        "mcp.tool_called",
        "outcome_unknown",
        "mcp.outcome_unknown",
        "unknown",
        4,
        1,
        None,
        "unknown",
    )
    assert tuple(outbox) == (
        event["mcp_invocation_event_id"],
        "pending",
        0,
        0,
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("output_status", "is_error", "expected_status", "expected_error"),
    [
        ("blocked", False, "blocked", "mcp.result_blocked"),
        ("clean", True, "failed", "mcp.tool_failed"),
        ("unavailable", False, "failed", "mcp.output_dlp_unavailable"),
    ],
)
def test_scheduler_called_terminal_projection_keeps_redaction_count(
    scheduler_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_status: str,
    is_error: bool,
    expected_status: str,
    expected_error: str,
) -> None:
    marker = f"projection-{output_status}"
    config, claim, material, frozen_tool, ids = _prepare_runtime_invocation(
        scheduler_database,
        marker=marker,
    )
    fixed_now = claim.lease_expires_at - timedelta(minutes=5)
    monkeypatch.setattr(
        scheduler,
        "_transaction_now",
        lambda _connection, override: override or fixed_now,
    )
    input_dlp = material.input_dlp
    output_dlp = _dlp(
        f"{marker}-output",
        status=output_status,
        match_count=1 if output_status == "blocked" else 0,
    )
    monkeypatch.setattr(scheduler, "_current_authorization_failure", lambda *_args: None)
    monkeypatch.setattr(
        scheduler, "require_actor_transaction_permissions", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        scheduler,
        "_scan_dlp",
        lambda *_args, scope, **_kwargs: input_dlp if scope == "input" else output_dlp,
    )
    call_material = scheduler._begin_call_after_probe(
        config,  # type: ignore[arg-type]
        material,
        _probe_result(frozen_tool),
    )
    assert call_material is not None
    scheduler._finalize_call_result(
        config,  # type: ignore[arg-type]
        call_material,
        McpCallResult(
            is_error=is_error,
            content=[{"type": "text", "text": "opaque"}],
            structured_content={"api_key": "opaque"},
            request_bytes=23,
            response_bytes=29,
        ),
    )

    connection = connect_database(_url(scheduler_database))
    try:
        invocation = connection.execute(
            """
            SELECT status,error_code,result_redaction_count,result_ciphertext,
              execution_status,tool_call_count
            FROM mcp_invocations WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchone()
        event = connection.execute(
            """
            SELECT status,error_code,redaction_count,execution_status,tool_call_count
            FROM mcp_invocation_events WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (ids["tenant_id"], ids["invocation_id"]),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(invocation) == (
        expected_status,
        expected_error,
        1,
        None,
        "called",
        1,
    )
    assert tuple(event) == (expected_status, expected_error, 1, "called", 1)


def _approval_config(*, include_previous: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        approval_hmac_version=3,
        approval_hmac_secret="prd082-scheduler-approval-domain-" + "a" * 40,
        approval_previous_hmac_version=2 if include_previous else None,
        approval_previous_hmac_secret=(
            "prd082-scheduler-previous-domain-" + "b" * 40 if include_previous else None
        ),
    )


def _approval_connection(
    *,
    created_key_version: int = 3,
) -> tuple[sqlite3.Connection, SimpleNamespace, sqlite3.Row]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE invocation(
          approval_required INTEGER,approval_task_id TEXT,approval_deadline_at TEXT,
          mcp_invocation_id TEXT,tenant_id TEXT,mcp_server_id TEXT,mcp_tool_id TEXT,
          requested_by TEXT,server_state_revision INTEGER,credential_revision INTEGER,
          schema_revision INTEGER,policy_revision INTEGER,
          input_schema_original_sha256 TEXT,input_schema_effective_sha256 TEXT,
          output_schema_original_sha256 TEXT,output_schema_effective_sha256 TEXT,
          required_permissions_sha256 TEXT,grant_sha256 TEXT,arguments_hmac TEXT,
          arguments_hmac_key_version INTEGER,request_id TEXT,trace_id TEXT
        );
        CREATE TABLE approval_tasks(
          tenant_id TEXT,approval_task_id TEXT,source_type TEXT,source_id TEXT,
          target_type TEXT,target_id TEXT,target_action TEXT,target_reason_code TEXT,
          target_version TEXT,target_fingerprint TEXT,risk_level TEXT,
          required_permission_code TEXT,created_by_type TEXT,created_by_id TEXT,
          mcp_invocation_id TEXT,expires_at TEXT,source_request_id TEXT,
          source_trace_id TEXT,status TEXT,resolution_reason TEXT,state_revision INTEGER,
          writeback_status TEXT,decided_by_user_id TEXT,decided_at TEXT,updated_at TEXT,
          decision_request_id TEXT,decision_trace_id TEXT,
          create_hmac_key_version INTEGER
        );
        CREATE TABLE approval_decisions(
          tenant_id TEXT,approval_task_id TEXT,decision TEXT,reason_code TEXT,
          actor_type TEXT,actor_id TEXT,revision_before INTEGER,revision_after INTEGER,
          step_up_used INTEGER,writeback_status TEXT,request_id TEXT,trace_id TEXT,
          decided_at TEXT
        );
        CREATE TABLE approval_writebacks(
          tenant_id TEXT,approval_task_id TEXT,adapter_kind TEXT,target_fact_id TEXT,
          target_revision INTEGER,status TEXT,attempt_count INTEGER,
          lease_generation INTEGER,lease_owner_id TEXT,lease_token TEXT,
          lease_expires_at TEXT,next_attempt_at TEXT,last_error_code TEXT,
          completed_at TEXT,created_at TEXT,updated_at TEXT
        );
        """
    )
    values: dict[str, object] = {
        "approval_required": 1,
        "approval_task_id": "apr_" + "1" * 32,
        "approval_deadline_at": "2026-08-16T09:00:00Z",
        "mcp_invocation_id": "mcpi_" + "2" * 32,
        "tenant_id": "tenant_prd082_scheduler_approval",
        "mcp_server_id": "mcps_" + "3" * 32,
        "mcp_tool_id": "mcpt_" + "4" * 32,
        "requested_by": "user_prd082_requester",
        "server_state_revision": 7,
        "credential_revision": 3,
        "schema_revision": 5,
        "policy_revision": 4,
        "input_schema_original_sha256": "5" * 64,
        "input_schema_effective_sha256": "6" * 64,
        "output_schema_original_sha256": None,
        "output_schema_effective_sha256": None,
        "required_permissions_sha256": "7" * 64,
        "grant_sha256": "8" * 64,
        "arguments_hmac": "9" * 64,
        "arguments_hmac_key_version": 2,
        "request_id": "req_prd082_approval_source",
        "trace_id": "trc_prd082_approval_source",
    }
    columns = tuple(values)
    connection.execute(
        f"INSERT INTO invocation({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    invocation = connection.execute("SELECT * FROM invocation").fetchone()
    assert invocation is not None
    config = _approval_config()
    target_version, target_fingerprint = mcp_approval_target_coordinates(
        config,  # type: ignore[arg-type]
        fingerprint_key_version=created_key_version,
        invocation_id=values["mcp_invocation_id"],
        tenant_id=values["tenant_id"],
        server_id=values["mcp_server_id"],
        tool_id=values["mcp_tool_id"],
        requested_by=values["requested_by"],
        server_state_revision=values["server_state_revision"],
        credential_revision=values["credential_revision"],
        schema_revision=values["schema_revision"],
        policy_revision=values["policy_revision"],
        input_schema_original_sha256=values["input_schema_original_sha256"],
        input_schema_effective_sha256=values["input_schema_effective_sha256"],
        output_schema_original_sha256=values["output_schema_original_sha256"],
        output_schema_effective_sha256=values["output_schema_effective_sha256"],
        required_permissions_sha256=values["required_permissions_sha256"],
        grant_sha256=values["grant_sha256"],
        arguments_hmac=values["arguments_hmac"],
        arguments_hmac_key_version=values["arguments_hmac_key_version"],
        approval_deadline_at=values["approval_deadline_at"],
    )
    decided_at = "2026-08-16T08:30:00Z"
    decider = "user_prd082_approver"
    connection.execute(
        """
        INSERT INTO approval_tasks VALUES(
          ?,?,'mcp',?,'mcp_tool_invocation',?,'invoke',NULL,?,?,'high','mcp.approve',
          'user',?,?,? ,?,?,'approved','approved',2,'completed',?,?,?, ?,?,?)
        """,
        (
            values["tenant_id"],
            values["approval_task_id"],
            values["mcp_invocation_id"],
            values["mcp_invocation_id"],
            target_version,
            target_fingerprint,
            values["requested_by"],
            values["mcp_invocation_id"],
            values["approval_deadline_at"],
            values["request_id"],
            values["trace_id"],
            decider,
            decided_at,
            decided_at,
            "req_prd082_approval_decision",
            "trc_prd082_approval_decision",
            created_key_version,
        ),
    )
    connection.execute(
        """
        INSERT INTO approval_decisions VALUES(
          ?,?,'approve','approved_business_need','user',?,1,2,1,'completed',?,?,?)
        """,
        (
            values["tenant_id"],
            values["approval_task_id"],
            decider,
            "req_prd082_approval_decision",
            "trc_prd082_approval_decision",
            decided_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO approval_writebacks VALUES(
          ?,?,'mcp_invocation_enqueue',?,?,'completed',0,0,NULL,NULL,NULL,NULL,NULL,?,?,?)
        """,
        (
            values["tenant_id"],
            values["approval_task_id"],
            values["mcp_invocation_id"],
            values["policy_revision"],
            decided_at,
            decided_at,
            decided_at,
        ),
    )
    return connection, config, invocation


def test_scheduler_approval_authorization_accepts_complete_approved_chain() -> None:
    connection, config, invocation = _approval_connection()
    try:
        assert (
            _approval_authorization_failure(  # type: ignore[arg-type]
                connection,
                config,
                invocation,
            )
            is None
        )
    finally:
        connection.close()


def test_scheduler_approval_authorization_accepts_previous_hmac_task_after_rotation() -> None:
    connection, config, invocation = _approval_connection(created_key_version=2)
    try:
        assert (
            _approval_authorization_failure(  # type: ignore[arg-type]
                connection,
                config,
                invocation,
            )
            is None
        )
    finally:
        connection.close()


def test_scheduler_approval_authorization_fails_closed_after_previous_key_removal() -> None:
    connection, _config, invocation = _approval_connection(created_key_version=2)
    rotated_without_previous = _approval_config(include_previous=False)
    try:
        assert (
            _approval_authorization_failure(  # type: ignore[arg-type]
                connection,
                rotated_without_previous,
                invocation,
            )
            == "mcp.unavailable"
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "drift",
    [
        "pending",
        "rejected",
        "self_approver",
        "fingerprint",
        "version",
        "request",
        "trace",
        "writeback",
    ],
)
def test_scheduler_approval_authorization_fails_closed_on_drift(drift: str) -> None:
    connection, config, invocation = _approval_connection()
    try:
        if drift in {"pending", "rejected"}:
            connection.execute("UPDATE approval_tasks SET status=?", (drift,))
        elif drift == "self_approver":
            requester = invocation["requested_by"]
            connection.execute(
                "UPDATE approval_tasks SET decided_by_user_id=?",
                (requester,),
            )
            connection.execute("UPDATE approval_decisions SET actor_id=?", (requester,))
        elif drift == "fingerprint":
            connection.execute("UPDATE approval_tasks SET target_fingerprint=?", ("f" * 64,))
        elif drift == "version":
            connection.execute("UPDATE approval_tasks SET target_version='drifted-version'")
        elif drift == "request":
            connection.execute("UPDATE approval_tasks SET source_request_id='req_drifted'")
        elif drift == "trace":
            connection.execute("UPDATE approval_tasks SET source_trace_id='trc_drifted'")
        else:
            connection.execute("UPDATE approval_writebacks SET target_revision=target_revision+1")
        assert (
            _approval_authorization_failure(  # type: ignore[arg-type]
                connection,
                config,
                invocation,
            )
            == "mcp.authorization_revoked"
        )
    finally:
        connection.close()
