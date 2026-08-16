"""Focused PRD-083 Scheduler/Sandbox HTTP trust-boundary tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import metahub_control.mcp_governance.scheduler as scheduler
import metahub_control.webhooks.service as webhook_service
import pytest
import test_prd082_mcp_scheduler as prd082_scheduler
from metahub_control.mcp_governance.executor import McpDiscoveryResult
from metahub_control.mcp_governance.sandbox_client import (
    McpSandboxClientError,
    McpSandboxDispatch,
    McpSandboxHttpClient,
    McpSandboxPermitResponse,
    McpSandboxResponse,
    parse_sandbox_discovery,
    parse_sandbox_permit,
)
from metahub_control.mcp_governance.scheduler import claim_next_scheduler_tenant
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import encrypt_secret, secret_fingerprint, secret_prefix
from metahub_shared.mcp import (
    McpNetworkPolicyCaps,
    McpSchemaLimits,
    encrypt_mcp_sandbox_response,
    validate_mcp_network_policy,
)
from metahub_shared.runtime.errors import ApiError

CURRENT_SECRET = "prd083-scheduler-response-current-" + "r" * 40
PREVIOUS_SECRET = "prd083-scheduler-response-previous-" + "p" * 40
ENVELOPE_SECRET = "prd083-scheduler-envelope-" + "e" * 40
INTERNAL_TOKEN = "prd083-scheduler-internal-" + "i" * 40
TENANT_ID = "tenant-prd083-scheduler"
SERVER_ID = "mcps_" + "1" * 32
INVOCATION_ID = "mcpi_" + "2" * 32


def _config(transport: httpx.BaseTransport | None = None) -> SimpleNamespace:
    del transport
    caps = McpNetworkPolicyCaps()
    return SimpleNamespace(
        mcp_executor_mode="sandbox_http",
        mcp_sandbox_base_url="http://sandbox.internal",
        mcp_sandbox_internal_token=INTERNAL_TOKEN,
        mcp_sandbox_envelope_secret=ENVELOPE_SECRET,
        mcp_sandbox_response_key_version=2,
        mcp_sandbox_response_secret_current=CURRENT_SECRET,
        mcp_sandbox_response_secret_previous=PREVIOUS_SECRET,
        mcp_result_max_bytes=1_048_576,
        mcp_server_schema_max_bytes=1_048_576,
        mcp_schema_limits=McpSchemaLimits(),
        mcp_network_policy_caps=lambda: caps,
    )


def _policy() -> Any:
    return validate_mcp_network_policy(
        {
            "expected_revision": 2,
            "status": "active",
            "egress_mode": "registered_endpoint_only",
            "connect_timeout_seconds": 5,
            "probe_timeout_seconds": 10,
            "call_timeout_seconds": 30,
            "failure_threshold": 3,
            "open_seconds": 60,
        },
        caps=McpNetworkPolicyCaps(),
    )


def _activate_policy(
    config: SimpleNamespace,
    claim: scheduler.McpInvocationClaim,
    *,
    actor_id: str,
    marker: str,
) -> tuple[Any, datetime, sqlite3.Row]:
    connection = connect_database(config.database_url)
    try:
        now_row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
        assert now_row is not None
        timestamp = str(now_row[0])
        current = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        policy = _policy()
        policy_row = connection.execute(
            """
            SELECT * FROM mcp_network_policies
            WHERE tenant_id=? AND mcp_server_id=?
            """,
            (claim.tenant_id, claim.server_id),
        ).fetchone()
        assert policy_row is not None
        policy_id = str(policy_row["mcp_network_policy_id"])
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
                policy_id,
                claim.tenant_id,
                claim.server_id,
                policy.fingerprint,
                actor_id,
                f"req_prd083_activate_{marker}",
                f"trc_prd083_activate_{marker}",
                timestamp,
            ),
        )
        changed = connection.execute(
            """
            UPDATE mcp_network_policies
            SET current_revision=2,current_fingerprint=?,status='active',updated_at=?
            WHERE tenant_id=? AND mcp_server_id=? AND current_revision=1
            """,
            (policy.fingerprint, timestamp, claim.tenant_id, claim.server_id),
        ).rowcount
        assert changed == 1
        current_policy = connection.execute(
            """
            SELECT policy.mcp_network_policy_id,policy.current_revision,
              policy.current_fingerprint,policy.status AS network_status,
              revision.egress_mode,revision.connect_timeout_seconds,
              revision.probe_timeout_seconds,revision.call_timeout_seconds,
              revision.failure_threshold,revision.open_seconds
            FROM mcp_network_policies policy
            JOIN mcp_network_policy_revisions revision
              ON revision.tenant_id=policy.tenant_id
             AND revision.mcp_network_policy_id=policy.mcp_network_policy_id
             AND revision.policy_revision=policy.current_revision
            WHERE policy.tenant_id=? AND policy.mcp_server_id=?
            """,
            (claim.tenant_id, claim.server_id),
        ).fetchone()
        assert current_policy is not None
        connection.commit()
        return policy, current, current_policy
    finally:
        connection.close()


def _dispatch(*, action: str = "probe") -> McpSandboxDispatch:
    policy = _policy()
    return McpSandboxDispatch(
        action=action,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        server_id=SERVER_ID,
        operation_id=None,
        invocation_id=INVOCATION_ID,
        lease_generation=3,
        lease_owner_id="scheduler-prd083",
        lease_token="3" * 64,
        policy=policy,
        policy_revision=2,
        policy_fingerprint=policy.fingerprint,
        deadline_at=datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=30),
        request_id="req_prd083_scheduler",
        endpoint_ciphertext="opaque-endpoint-ciphertext",
        endpoint_hmac="4" * 64,
        endpoint_hmac_key_version=1,
        auth_type="none",
        credential_id=None,
        tool_name="safe_tool",
        arguments_hmac="5" * 64,
        arguments_hmac_key_version=1,
        call_intent_id="mcpni_" + "6" * 32 if action == "authorize-call" else None,
    )


def _response_metadata(payload: dict[str, Any], **overrides: object) -> dict[str, object]:
    metadata = {
        "schema_version": 1,
        "request_id": payload["request_id"],
        "dispatch_id": payload["dispatch_id"],
        "action": payload["action"],
        "tenant_id": payload["tenant_id"],
        "operation_id": payload["operation_id"],
        "mcp_invocation_id": payload["mcp_invocation_id"],
        "lease_generation": payload["lease_generation"],
        "lease_owner_id": payload["lease_owner_id"],
        "network_policy_revision": payload["network_policy_revision"],
        "network_policy_fingerprint": payload["network_policy_fingerprint"],
        "status": "succeeded",
        "failure_code": None,
        "deadline_at": payload["deadline_at"],
        "address_count": 1,
        "duration_ms": 7,
    }
    metadata.update(overrides)
    return metadata


def _worker_transport(
    *,
    response_secret: str = CURRENT_SECRET,
    response_version: int = 2,
    tamper_metadata: bool = False,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {INTERNAL_TOKEN}"
        wire = json.loads(request.content)
        payload = wire["payload"]
        assert "opaque-endpoint-ciphertext" in request.content.decode()
        result = {
            "protocol_version": "2025-06-18",
            "capabilities": {"tools": {}},
            "tools": [
                {
                    "name": "safe_tool",
                    "inputSchema": {"type": "object"},
                }
            ],
            "requested_tool_name": "safe_tool",
        }
        encrypted = encrypt_mcp_sandbox_response(
            result,
            metadata=_response_metadata(payload),
            secret=response_secret,
            key_version=response_version,
        ).to_wire()
        if tamper_metadata:
            encrypted["duration_ms"] = 8
        return httpx.Response(200, json=encrypted)

    return httpx.MockTransport(handler)


def test_client_readiness_requires_exact_caps_fingerprint() -> None:
    caps = McpNetworkPolicyCaps()

    def ready(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/ready"
        return httpx.Response(
            200,
            json={"status": "ready", "caps_fingerprint": caps.fingerprint},
        )

    McpSandboxHttpClient(_config(), transport=httpx.MockTransport(ready)).readiness()

    def drifted(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ready", "caps_fingerprint": "0" * 64})

    with pytest.raises(McpSandboxClientError, match="mcp.sandbox_unavailable"):
        McpSandboxHttpClient(_config(), transport=httpx.MockTransport(drifted)).readiness()


def test_client_accepts_only_current_or_previous_authenticated_response() -> None:
    current = McpSandboxHttpClient(_config(), transport=_worker_transport()).dispatch(_dispatch())
    assert current.status == "succeeded"
    parsed = parse_sandbox_discovery(current, config=_config())
    assert parsed.protocol_version == "2025-06-18"
    assert [tool.name for tool in parsed.tools] == ["safe_tool"]

    previous = McpSandboxHttpClient(
        _config(),
        transport=_worker_transport(response_secret=PREVIOUS_SECRET, response_version=1),
    ).dispatch(_dispatch())
    assert previous.status == "succeeded"


def test_client_rejects_authenticated_metadata_tamper_without_sensitive_error() -> None:
    request = _dispatch()
    assert "opaque-endpoint-ciphertext" not in repr(request)
    with pytest.raises(McpSandboxClientError) as captured:
        McpSandboxHttpClient(
            _config(),
            transport=_worker_transport(tamper_metadata=True),
        ).dispatch(request)
    assert str(captured.value) == "mcp.sandbox_unavailable"
    assert "opaque-endpoint-ciphertext" not in repr(captured.value)


def test_permit_digest_is_verified_before_scheduler_can_persist_intent() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    permit = "metahub-mcp-sandbox-permit-v1:opaque"
    response = McpSandboxResponse(
        status="succeeded",
        failure_code=None,
        dispatch_id="mcpd_" + "7" * 32,
        address_count=1,
        duration_ms=1,
        request_bytes=1,
        response_bytes=1,
        payload={
            "permit": permit,
            "permit_digest": hashlib.sha256(permit.encode("ascii")).hexdigest(),
            "permit_expires_at": int((now + timedelta(seconds=20)).timestamp()),
        },
    )
    parsed = parse_sandbox_permit(
        response,
        deadline_at=now + timedelta(seconds=30),
    )
    assert parsed.permit_digest == response.payload["permit_digest"]
    tampered = McpSandboxResponse(
        status=response.status,
        failure_code=response.failure_code,
        dispatch_id=response.dispatch_id,
        address_count=response.address_count,
        duration_ms=response.duration_ms,
        request_bytes=response.request_bytes,
        response_bytes=response.response_bytes,
        payload={**response.payload, "permit_digest": "0" * 64},
    )
    with pytest.raises(McpSandboxClientError):
        parse_sandbox_permit(
            tampered,
            deadline_at=now + timedelta(seconds=30),
        )


@pytest.mark.parametrize(
    "reason",
    (
        "dns_unavailable",
        "connect_unavailable",
        "tls_unavailable",
        "headers_timeout",
        "body_timeout",
        "overall_timeout",
    ),
)
def test_network_failure_reason_accepts_only_fixed_authenticated_categories(
    reason: str,
) -> None:
    response = McpSandboxResponse(
        status="failed",
        failure_code="mcp.server_unavailable",
        dispatch_id="mcpd_" + "9" * 32,
        address_count=0,
        duration_ms=1,
        request_bytes=1,
        response_bytes=1,
        payload={"availability_reason": reason},
    )
    assert (
        scheduler._network_failure_reason(
            response,
            error_code="mcp.server_unavailable",
        )
        == reason
    )
    response.payload["availability_reason"] = "unsafe-upstream-detail"
    assert (
        scheduler._network_failure_reason(
            response,
            error_code="mcp.server_unavailable",
        )
        == "overall_timeout"
    )


def test_sandbox_sync_dispatch_deadline_reserves_durable_finalize_window() -> None:
    now = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
    claim = scheduler.McpSyncJobClaim(
        tenant_id="tenant_prd083_sync_budget",
        job_id="mcpj_" + "1" * 32,
        server_id="mcps_" + "2" * 32,
        operation_id="mcpo_" + "3" * 32,
        job_type="discover",
        candidate_credential_revision=None,
        attempt_count=1,
        lease_generation=1,
        lease_owner_id="prd083-sync-budget",
        lease_token="4" * 64,
        lease_expires_at=now + timedelta(seconds=60),
        deadline_at=now + timedelta(seconds=22),
    )
    assert scheduler._sandbox_sync_dispatch_deadline(claim, configured=10, now=now) == now + (
        timedelta(seconds=16)
    )
    assert (
        scheduler._sandbox_sync_dispatch_deadline(
            claim,
            configured=10,
            now=now + timedelta(seconds=16),
        )
        is None
    )


def test_expired_sandbox_sync_budget_fails_before_prepare_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    claim = scheduler.McpSyncJobClaim(
        tenant_id="tenant_prd083_expired_sandbox_sync",
        job_id="mcpj_" + "5" * 32,
        server_id="mcps_" + "6" * 32,
        operation_id="mcpo_" + "7" * 32,
        job_type="discover",
        candidate_credential_revision=None,
        attempt_count=1,
        lease_generation=1,
        lease_owner_id="prd083-expired-sandbox-sync",
        lease_token="8" * 64,
        lease_expires_at=now + timedelta(seconds=60),
        deadline_at=now + timedelta(seconds=22),
    )
    events: list[str] = []

    def no_budget(*_args: object, **_kwargs: object) -> None:
        events.append("budget")
        return None

    def unexpected(*_args: object, **_kwargs: object) -> None:
        events.append("unexpected")

    def finalize_failure(
        _config: object,
        actual_claim: scheduler.McpSyncJobClaim,
        *,
        error_code: str,
        **_kwargs: object,
    ) -> None:
        assert actual_claim is claim
        assert error_code == "mcp.sync_failed"
        events.append("failure")

    monkeypatch.setattr(scheduler, "_sandbox_sync_dispatch_deadline", no_budget)
    monkeypatch.setattr(scheduler, "_prepare_sandbox_sync", unexpected)
    monkeypatch.setattr(scheduler, "_finalize_sync_failure", finalize_failure)
    scheduler._process_sync_job_sandbox(
        SimpleNamespace(mcp_probe_deadline_seconds=10),  # type: ignore[arg-type]
        claim,
    )
    assert events == ["budget", "failure"]


def test_sandbox_sync_recomputes_budget_and_preserves_policy_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    first_deadline = now + timedelta(seconds=10)
    final_deadline = now + timedelta(seconds=7)
    claim = scheduler.McpSyncJobClaim(
        tenant_id="tenant_prd083_sandbox_budget_order",
        job_id="mcpj_" + "9" * 32,
        server_id="mcps_" + "a" * 32,
        operation_id="mcpo_" + "b" * 32,
        job_type="discover",
        candidate_credential_revision=None,
        attempt_count=1,
        lease_generation=1,
        lease_owner_id="prd083-sandbox-budget-order",
        lease_token="c" * 64,
        lease_expires_at=now + timedelta(seconds=60),
        deadline_at=now + timedelta(seconds=22),
    )
    policy = _policy()
    material = SimpleNamespace(
        policy=policy,
        fence=SimpleNamespace(
            policy_revision=2,
            policy_fingerprint=policy.fingerprint,
            request_id="req_prd083_sandbox_budget_order",
        ),
        endpoint_ciphertext="opaque-endpoint-ciphertext",
        endpoint_hmac="d" * 64,
        endpoint_hmac_key_version=1,
        auth_type="none",
        credential_id=None,
        credential_ciphertext=None,
        credential_revision=1,
    )
    response = McpSandboxResponse(
        status="succeeded",
        failure_code=None,
        dispatch_id="mcpd_" + "e" * 32,
        address_count=1,
        duration_ms=1,
        request_bytes=1,
        response_bytes=1,
        payload={},
    )
    result = McpDiscoveryResult(
        protocol_version="2025-06-18",
        capabilities_json="{}",
        capabilities_sha256=hashlib.sha256(b"sandbox-budget").hexdigest(),
        tools=(),
        request_bytes=1,
        response_bytes=1,
    )
    events: list[str] = []
    budgets = iter((first_deadline, final_deadline))

    def budget(*_args: object, **_kwargs: object) -> datetime:
        events.append("budget")
        return next(budgets)

    def prepare(*_args: object, **_kwargs: object) -> object:
        events.append("prepare")
        return material

    class Client:
        def __init__(self, _config: object) -> None:
            return

        def dispatch(self, request: McpSandboxDispatch) -> McpSandboxResponse:
            events.append("dispatch")
            assert request.deadline_at == final_deadline
            assert request.policy.fingerprint == policy.fingerprint
            assert request.policy_fingerprint == policy.fingerprint
            return response

    def parse(*_args: object, **_kwargs: object) -> McpDiscoveryResult:
        events.append("parse")
        return result

    def finalize(*_args: object, **_kwargs: object) -> None:
        events.append("finalize")

    monkeypatch.setattr(scheduler, "_sandbox_sync_dispatch_deadline", budget)
    monkeypatch.setattr(scheduler, "_prepare_sandbox_sync", prepare)
    monkeypatch.setattr(scheduler, "McpSandboxHttpClient", Client)
    monkeypatch.setattr(scheduler, "parse_sandbox_discovery", parse)
    monkeypatch.setattr(scheduler, "_finalize_sync_success", finalize)
    scheduler._process_sync_job_sandbox(
        SimpleNamespace(mcp_probe_deadline_seconds=10),  # type: ignore[arg-type]
        claim,
    )
    assert events == ["budget", "prepare", "budget", "dispatch", "parse", "finalize"]


def test_reserved_sandbox_sync_window_commits_on_migrated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "sandbox-sync-finalize-window.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "Prd083SchedulerOnly!2026")
    assert apply_migrations(database_url).applied[-1] == "0076"
    apply_seed_data(database_url)
    config, claim, _ids = prd082_scheduler._prepare_sync_job(
        database,
        marker="sandbox-finalize-window",
        with_tools=False,
        deadline_seconds=22,
    )
    result = McpDiscoveryResult(
        protocol_version="2025-06-18",
        capabilities_json="{}",
        capabilities_sha256=hashlib.sha256(b"sandbox-finalize-window").hexdigest(),
        tools=(),
        request_bytes=1,
        response_bytes=1,
    )
    claimed_at = claim.deadline_at - timedelta(seconds=22)
    dispatch_deadline = scheduler._sandbox_sync_dispatch_deadline(
        claim,
        configured=10,
        now=claimed_at,
    )
    assert dispatch_deadline == claim.deadline_at - timedelta(seconds=6)

    monkeypatch.setattr(
        scheduler,
        "_transaction_now",
        lambda *_args, **_kwargs: claim.deadline_at + timedelta(seconds=1),
    )
    with pytest.raises(ApiError) as late:
        scheduler._finalize_sync_success(config, claim, result)
    assert late.value.code == "mcp.state_conflict"

    monkeypatch.setattr(
        scheduler,
        "_transaction_now",
        lambda *_args, **_kwargs: dispatch_deadline + timedelta(seconds=1),
    )
    scheduler._finalize_sync_success(config, claim, result)
    connection = connect_database(config.database_url)
    try:
        job = connection.execute(
            "SELECT status FROM mcp_server_sync_jobs WHERE mcp_server_sync_job_id=?",
            (claim.job_id,),
        ).fetchone()
        operation = connection.execute(
            "SELECT status FROM mcp_server_operations WHERE mcp_server_operation_id=?",
            (claim.operation_id,),
        ).fetchone()
        assert tuple(job) == ("succeeded",)
        assert tuple(operation) == ("succeeded",)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_authorized_permit_commits_call_intent_on_migrated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "authorized-call-intent.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "Prd083SchedulerOnly!2026")
    assert apply_migrations(database_url).applied[-1] == "0076"
    apply_seed_data(database_url)
    config, claim, legacy, frozen_tool, ids = prd082_scheduler._prepare_runtime_invocation(
        database,
        marker="authorized-call-intent",
    )
    config.mcp_network_policy_caps = lambda: McpNetworkPolicyCaps()
    config.mcp_sandbox_envelope_secret = ENVELOPE_SECRET
    policy, current, current_policy = _activate_policy(
        config,
        claim,
        actor_id=ids["actor_id"],
        marker="authorized-call-intent",
    )
    call_generation = claim.lease_generation + 1
    call_token = "6" * 64
    connection = connect_database(config.database_url)
    try:
        call_fence = scheduler._start_network_fence(
            connection,
            tenant_id=claim.tenant_id,
            server_id=claim.server_id,
            policy_row=current_policy,
            phase="call_preflight",
            owner_type="invocation",
            owner_id=claim.invocation_id,
            lease_generation=call_generation,
            lease_owner_id=claim.lease_owner_id,
            lease_token=call_token,
            lease_expires_at=claim.lease_expires_at,
            attempt_number=claim.attempt_number,
            request_id="req_prd083_authorized_call_intent",
            trace_id="trc_prd083_authorized_call_intent",
            current=current,
        )
        tool_policy = connection.execute(
            """
            SELECT tool.schema_revision,tool.policy_revision,
              invocation.policy_fingerprint,server.endpoint_ciphertext,
              server.endpoint_hmac,server.endpoint_hmac_key_version
            FROM mcp_tools tool
            JOIN mcp_servers server ON server.tenant_id=tool.tenant_id
             AND server.mcp_server_id=tool.mcp_server_id
            JOIN mcp_invocations invocation ON invocation.tenant_id=tool.tenant_id
             AND invocation.mcp_tool_id=tool.mcp_tool_id
            WHERE tool.tenant_id=? AND tool.mcp_tool_id=?
            """,
            (claim.tenant_id, claim.tool_id),
        ).fetchone()
        assert tool_policy is not None
        connection.commit()
    finally:
        connection.close()
    material = scheduler._SandboxInvocationMaterial(
        claim=claim,
        policy=policy,
        fence=call_fence,
        endpoint_ciphertext=str(tool_policy["endpoint_ciphertext"]),
        endpoint_hmac=str(tool_policy["endpoint_hmac"]),
        endpoint_hmac_key_version=int(tool_policy["endpoint_hmac_key_version"]),
        auth_type="none",
        credential_id=None,
        credential_ciphertext=None,
        credential_revision=1,
        arguments=legacy.arguments,
        arguments_ciphertext="opaque-arguments",
        arguments_hmac="5" * 64,
        arguments_hmac_key_version=1,
        input_dlp=legacy.input_dlp,
        remote_name=legacy.remote_name,
    )
    probe_response = McpSandboxResponse(
        status="succeeded",
        failure_code=None,
        dispatch_id="mcpd_" + "d" * 32,
        address_count=1,
        duration_ms=1,
        request_bytes=17,
        response_bytes=19,
        payload={},
    )
    plan = scheduler._SandboxCallPlan(
        material=material,
        probe=prd082_scheduler._probe_result(frozen_tool),
        policy=policy,
        fence=call_fence,
        network_decision_id="mcpnd_" + "7" * 32,
        call_intent_id="mcpni_" + "8" * 32,
        schema_revision=int(tool_policy["schema_revision"]),
        tool_policy_revision=int(tool_policy["policy_revision"]),
        tool_policy_fingerprint=str(tool_policy["policy_fingerprint"]),
        call_generation=call_generation,
        call_owner_id=claim.lease_owner_id,
        call_token=call_token,
        call_expires_at=claim.lease_expires_at,
    )
    permit_value = "metahub-mcp-sandbox-permit-v1:authorized-call-intent"
    permit = McpSandboxPermitResponse(
        permit=permit_value,
        permit_digest=hashlib.sha256(permit_value.encode("ascii")).hexdigest(),
        permit_expires_at=int(plan.call_expires_at.timestamp()),
        dispatch=probe_response,
    )
    connection = connect_database(config.database_url)
    try:
        original_request_bytes = connection.execute(
            """
            SELECT request_bytes FROM mcp_invocations
            WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (claim.tenant_id, claim.invocation_id),
        ).fetchone()["request_bytes"]
    finally:
        connection.close()

    monkeypatch.setattr(scheduler, "_current_authorization_failure", lambda *_args: None)
    monkeypatch.setattr(
        scheduler,
        "require_actor_transaction_permissions",
        lambda *_args, **_kwargs: None,
    )
    call = scheduler._commit_sandbox_call_intent(config, plan, permit)
    assert call is not None
    connection = connect_database(config.database_url)
    try:
        invocation = connection.execute(
            """
            SELECT status,call_attempt_count,execution_status,tool_call_count,
              cost_status,request_bytes
            FROM mcp_invocations
            WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (claim.tenant_id, claim.invocation_id),
        ).fetchone()
        assert tuple(invocation) == (
            "calling",
            1,
            "unknown",
            None,
            "unknown",
            original_request_bytes,
        )
        decision = connection.execute(
            """
            SELECT result,reason_code,permit_digest FROM mcp_network_decisions
            WHERE tenant_id=? AND mcp_network_decision_id=?
            """,
            (claim.tenant_id, plan.network_decision_id),
        ).fetchone()
        assert tuple(decision) == ("allowed", "allowed", permit.permit_digest)
        intent = connection.execute(
            """
            SELECT permit_digest,lease_generation FROM mcp_network_call_intents
            WHERE tenant_id=? AND mcp_network_call_intent_id=?
            """,
            (claim.tenant_id, plan.call_intent_id),
        ).fetchone()
        assert tuple(intent) == (permit.permit_digest, plan.call_generation)
        attempt = connection.execute(
            """
            SELECT attempt_kind,attempt_number,status,lease_generation,request_bytes
            FROM mcp_invocation_attempts
            WHERE tenant_id=? AND mcp_invocation_id=? AND attempt_kind='call'
            """,
            (claim.tenant_id, claim.invocation_id),
        ).fetchone()
        assert tuple(attempt) == (
            "call",
            1,
            "processing",
            plan.call_generation,
            original_request_bytes,
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_waiting_policy_job_is_not_claimed_or_mutated(tmp_path: Path) -> None:
    database = tmp_path / "waiting-policy.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE tenants(tenant_id TEXT PRIMARY KEY,status TEXT NOT NULL);
            CREATE TABLE mcp_scheduler_tenants(
              tenant_id TEXT PRIMARY KEY,status TEXT,last_claim_sequence INTEGER,
              last_claimed_at TEXT,lease_generation INTEGER,lease_owner_id TEXT,
              lease_token TEXT,lease_expires_at TEXT,next_scan_at TEXT,
              created_at TEXT,updated_at TEXT
            );
            CREATE TABLE mcp_server_sync_jobs(
              tenant_id TEXT,status TEXT,next_attempt_at TEXT,lease_expires_at TEXT
            );
            CREATE TABLE mcp_invocations(
              tenant_id TEXT,status TEXT,next_attempt_at TEXT,lease_expires_at TEXT
            );
            """
        )
        timestamp = "2026-08-16T00:00:00Z"
        connection.execute("INSERT INTO tenants VALUES(?, 'active')", (TENANT_ID,))
        connection.execute(
            "INSERT INTO mcp_scheduler_tenants VALUES(?, 'idle',0,NULL,0,NULL,NULL,NULL,?,?,?)",
            (TENANT_ID, timestamp, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO mcp_server_sync_jobs VALUES(?, 'waiting_policy',NULL,NULL)",
            (TENANT_ID,),
        )
        connection.commit()
    finally:
        connection.close()
    config = SimpleNamespace(
        database_url=f"sqlite:///{database.as_posix()}",
        mcp_tenant_concurrency=1,
        mcp_operation_lease_seconds=120,
        mcp_invocation_lease_seconds=120,
    )
    assert (
        claim_next_scheduler_tenant(
            config,  # type: ignore[arg-type]
            worker_id="prd083-waiting-policy",
            now=datetime(2026, 8, 16, tzinfo=UTC),
        )
        is None
    )
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT status FROM mcp_server_sync_jobs").fetchone()
        scheduler = connection.execute(
            "SELECT status,lease_generation FROM mcp_scheduler_tenants"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("waiting_policy",)
    assert scheduler == ("idle", 0)


def test_blocked_invocation_commits_only_evt095_on_migrated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler_database = tmp_path / "scheduler.sqlite3"
    database_url = f"sqlite:///{scheduler_database.as_posix()}"
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "Prd083SchedulerOnly!2026")
    assert apply_migrations(database_url).applied[-1] == "0076"
    apply_seed_data(database_url)
    config, claim, legacy, _tool, ids = prd082_scheduler._prepare_runtime_invocation(
        scheduler_database,
        marker="network-blocked",
    )
    policy, current, current_policy = _activate_policy(
        config,
        claim,
        actor_id=ids["actor_id"],
        marker="blocked",
    )
    connection = connect_database(config.database_url)
    try:
        fence = scheduler._start_network_fence(
            connection,
            tenant_id=claim.tenant_id,
            server_id=claim.server_id,
            policy_row=current_policy,
            phase="probe",
            owner_type="invocation",
            owner_id=claim.invocation_id,
            lease_generation=claim.lease_generation,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
            lease_expires_at=claim.lease_expires_at,
            attempt_number=claim.attempt_number,
            request_id="req_prd083_runtime_network-blocked",
            trace_id="trc_prd083_runtime_network-blocked",
            current=current,
        )
        connection.commit()
    finally:
        connection.close()

    material = scheduler._SandboxInvocationMaterial(
        claim=claim,
        policy=policy,
        fence=fence,
        endpoint_ciphertext="opaque-endpoint",
        endpoint_hmac="4" * 64,
        endpoint_hmac_key_version=1,
        auth_type="none",
        credential_id=None,
        credential_ciphertext=None,
        credential_revision=1,
        arguments=legacy.arguments,
        arguments_ciphertext="opaque-arguments",
        arguments_hmac="5" * 64,
        arguments_hmac_key_version=1,
        input_dlp=legacy.input_dlp,
        remote_name=legacy.remote_name,
    )
    response = McpSandboxResponse(
        status="blocked",
        failure_code="mcp.network_blocked",
        dispatch_id="mcpd_" + "8" * 32,
        address_count=1,
        duration_ms=3,
        request_bytes=19,
        response_bytes=23,
        payload={"reason_code": "address_denied"},
    )
    scheduler._finalize_invocation_network_blocked(
        config,
        material,
        fence=fence,
        response=response,
    )

    connection = connect_database(config.database_url)
    try:
        invocation = connection.execute(
            """
            SELECT status,error_code,execution_status,tool_call_count,cost_status,
              cost_micros FROM mcp_invocations
            WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (claim.tenant_id, claim.invocation_id),
        ).fetchone()
        assert invocation is not None
        assert tuple(invocation) == (
            "blocked",
            "mcp.network_blocked",
            "not_called",
            0,
            "not_applicable",
            None,
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM mcp_network_call_intents WHERE tenant_id=?",
                (claim.tenant_id,),
            ).fetchone()[0]
            == 0
        )
        decision = connection.execute(
            """
            SELECT result,reason_code,phase FROM mcp_network_decisions
            WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (claim.tenant_id, claim.invocation_id),
        ).fetchone()
        assert decision is not None
        assert tuple(decision) == ("blocked", "address_denied", "probe")
        network_event = connection.execute(
            """
            SELECT event.reason_code,outbox.status,outbox.attempt_count,
              outbox.error_code,outbox.subscription_snapshot_json,
              outbox.snapshot_frozen_at,event.occurred_at
            FROM mcp_network_events event
            JOIN mcp_network_event_outbox outbox
              ON outbox.tenant_id=event.tenant_id
             AND outbox.mcp_network_event_id=event.mcp_network_event_id
            WHERE event.tenant_id=? AND event.mcp_invocation_id=?
            """,
            (claim.tenant_id, claim.invocation_id),
        ).fetchone()
        assert network_event is not None
        assert tuple(network_event) == (
            "address_denied",
            "pending",
            0,
            None,
            "[]",
            network_event["occurred_at"],
            network_event["occurred_at"],
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM mcp_invocation_events WHERE tenant_id=?",
                (claim.tenant_id,),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
    _assert_network_outbox_delivery_and_disabled_policy(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        network_event=network_event,
        config=config,
        claim=claim,
    )


def test_user_grant_revoked_during_authorize_never_commits_call_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "authorize-grant-race.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "Prd083SchedulerOnly!2026")
    assert apply_migrations(database_url).applied[-1] == "0076"
    apply_seed_data(database_url)
    config, claim, legacy, frozen_tool, ids = prd082_scheduler._prepare_runtime_invocation(
        database,
        marker="authorize-grant-race",
    )
    config.mcp_network_policy_caps = lambda: McpNetworkPolicyCaps()
    config.mcp_sandbox_envelope_secret = ENVELOPE_SECRET
    policy, current, current_policy = _activate_policy(
        config,
        claim,
        actor_id=ids["actor_id"],
        marker="authorize-grant-race",
    )
    call_generation = claim.lease_generation + 1
    call_token = "6" * 64
    connection = connect_database(config.database_url)
    try:
        call_fence = scheduler._start_network_fence(
            connection,
            tenant_id=claim.tenant_id,
            server_id=claim.server_id,
            policy_row=current_policy,
            phase="call_preflight",
            owner_type="invocation",
            owner_id=claim.invocation_id,
            lease_generation=call_generation,
            lease_owner_id=claim.lease_owner_id,
            lease_token=call_token,
            lease_expires_at=claim.lease_expires_at,
            attempt_number=claim.attempt_number,
            request_id="req_prd083_authorize_grant_race",
            trace_id="trc_prd083_authorize_grant_race",
            current=current,
        )
        tool_policy = connection.execute(
            """
            SELECT tool.schema_revision,tool.policy_revision,
              invocation.policy_fingerprint
            FROM mcp_tools tool
            JOIN mcp_invocations invocation ON invocation.tenant_id=tool.tenant_id
             AND invocation.mcp_tool_id=tool.mcp_tool_id
            WHERE tool.tenant_id=? AND tool.mcp_tool_id=?
            """,
            (claim.tenant_id, claim.tool_id),
        ).fetchone()
        assert tool_policy is not None
        connection.commit()
    finally:
        connection.close()
    material = scheduler._SandboxInvocationMaterial(
        claim=claim,
        policy=policy,
        fence=call_fence,
        endpoint_ciphertext="opaque-endpoint",
        endpoint_hmac="4" * 64,
        endpoint_hmac_key_version=1,
        auth_type="none",
        credential_id=None,
        credential_ciphertext=None,
        credential_revision=1,
        arguments=legacy.arguments,
        arguments_ciphertext="opaque-arguments",
        arguments_hmac="5" * 64,
        arguments_hmac_key_version=1,
        input_dlp=legacy.input_dlp,
        remote_name=legacy.remote_name,
    )
    plan = scheduler._SandboxCallPlan(
        material=material,
        probe=prd082_scheduler._probe_result(frozen_tool),
        policy=policy,
        fence=call_fence,
        network_decision_id="mcpnd_" + "7" * 32,
        call_intent_id="mcpni_" + "8" * 32,
        schema_revision=int(tool_policy["schema_revision"]),
        tool_policy_revision=int(tool_policy["policy_revision"]),
        tool_policy_fingerprint=str(tool_policy["policy_fingerprint"]),
        call_generation=call_generation,
        call_owner_id=claim.lease_owner_id,
        call_token=call_token,
        call_expires_at=claim.lease_expires_at,
    )
    permit_value = "metahub-mcp-sandbox-permit-v1:authorize-grant-race"
    permit_dispatch = McpSandboxResponse(
        status="succeeded",
        failure_code=None,
        dispatch_id="mcpd_" + "c" * 32,
        address_count=1,
        duration_ms=1,
        request_bytes=1,
        response_bytes=1,
        payload={},
    )
    permit = McpSandboxPermitResponse(
        permit=permit_value,
        permit_digest=hashlib.sha256(permit_value.encode("ascii")).hexdigest(),
        permit_expires_at=int(plan.call_expires_at.timestamp()),
        dispatch=permit_dispatch,
    )
    revoke = connect_database(config.database_url)
    try:
        alternate_role = revoke.execute(
            """
            SELECT role.role_id FROM roles role
            JOIN users actor ON actor.tenant_id=role.tenant_id
            WHERE role.tenant_id=? AND actor.user_id=?
              AND role.status='active' AND role.code!=actor.role
            ORDER BY role.role_id LIMIT 1
            """,
            (claim.tenant_id, ids["actor_id"]),
        ).fetchone()
        assert alternate_role is not None
        revoke.execute(
            """
            INSERT INTO mcp_tool_role_grants(
              mcp_tool_role_grant_id,tenant_id,mcp_tool_id,role_id,
              created_by,created_at
            ) VALUES(?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            """,
            (
                "mcprg_" + "b" * 32,
                claim.tenant_id,
                claim.tool_id,
                alternate_role["role_id"],
                ids["actor_id"],
            ),
        )
        deleted = revoke.execute(
            """
            DELETE FROM mcp_tool_user_grants
            WHERE tenant_id=? AND mcp_tool_id=? AND user_id=?
            """,
            (claim.tenant_id, claim.tool_id, ids["actor_id"]),
        ).rowcount
        assert deleted == 1
        revoke.commit()
    finally:
        revoke.close()

    assert scheduler._commit_sandbox_call_intent(config, plan, permit) is None

    connection = connect_database(config.database_url)
    try:
        invocation = connection.execute(
            """
            SELECT status,error_code,execution_status,tool_call_count,cost_status,
              cost_micros,call_attempt_count FROM mcp_invocations
            WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (claim.tenant_id, claim.invocation_id),
        ).fetchone()
        assert invocation is not None
        assert tuple(invocation) == (
            "failed",
            "mcp.authorization_revoked",
            "not_called",
            0,
            "not_applicable",
            None,
            0,
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM mcp_network_decisions
            WHERE tenant_id=? AND mcp_invocation_id=?
            """,
                (claim.tenant_id, claim.invocation_id),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM mcp_network_call_intents WHERE tenant_id=?",
                (claim.tenant_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM mcp_invocation_attempts
            WHERE tenant_id=? AND mcp_invocation_id=? AND attempt_kind='call'
            """,
                (claim.tenant_id, claim.invocation_id),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_closed_circuit_stale_fences_aggregate_against_current_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "closed-circuit-concurrency.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "Prd083SchedulerOnly!2026")
    assert apply_migrations(database_url).applied[-1] == "0076"
    apply_seed_data(database_url)
    config, claim, _legacy, _tool, ids = prd082_scheduler._prepare_runtime_invocation(
        database,
        marker="closed-circuit-concurrency",
    )
    _policy, current, current_policy = _activate_policy(
        config,
        claim,
        actor_id=ids["actor_id"],
        marker="closed-circuit-concurrency",
    )
    connection = connect_database(config.database_url)
    try:
        common = {
            "tenant_id": claim.tenant_id,
            "server_id": claim.server_id,
            "policy_row": current_policy,
            "phase": "probe",
            "owner_type": "invocation",
            "owner_id": claim.invocation_id,
            "lease_owner_id": claim.lease_owner_id,
            "lease_expires_at": claim.lease_expires_at,
            "attempt_number": claim.attempt_number,
            "trace_id": "trc_prd083_closed_circuit_concurrency",
            "current": current,
        }
        first = scheduler._start_network_fence(
            connection,
            lease_generation=claim.lease_generation,
            lease_token="a" * 64,
            request_id="req_prd083_closed_circuit_first",
            **common,
        )
        second = scheduler._start_network_fence(
            connection,
            lease_generation=claim.lease_generation + 1,
            lease_token="b" * 64,
            request_id="req_prd083_closed_circuit_second",
            **common,
        )
        assert first.circuit_revision == second.circuit_revision == 1
        scheduler._complete_network_failure(
            connection,
            first,
            error_code="mcp.server_unavailable",
            availability_reason="connect_unavailable",
            current=current + timedelta(seconds=1),
        )
        scheduler._complete_network_failure(
            connection,
            second,
            error_code="mcp.server_unavailable",
            availability_reason="body_timeout",
            current=current + timedelta(seconds=2),
        )
        circuit = connection.execute(
            """
            SELECT state,failure_count,last_failure_code,state_revision
            FROM mcp_network_circuits
            WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
            """,
            (claim.tenant_id, claim.server_id, first.policy_revision),
        ).fetchone()
        assert circuit is not None
        assert tuple(circuit) == ("closed", 2, "body_timeout", 3)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.commit()
    finally:
        connection.close()


def test_expired_half_open_owner_is_reclaimed_with_two_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "half-open-reclaim.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "Prd083SchedulerOnly!2026")
    assert apply_migrations(database_url).applied[-1] == "0076"
    apply_seed_data(database_url)
    config, claim, _legacy, _tool, ids = prd082_scheduler._prepare_runtime_invocation(
        database,
        marker="half-open-reclaim",
    )
    _policy_value, current, current_policy = _activate_policy(
        config,
        claim,
        actor_id=ids["actor_id"],
        marker="half-open-reclaim",
    )
    connection = connect_database(config.database_url)
    try:
        initial = scheduler._start_network_fence(
            connection,
            tenant_id=claim.tenant_id,
            server_id=claim.server_id,
            policy_row=current_policy,
            phase="probe",
            owner_type="invocation",
            owner_id=claim.invocation_id,
            lease_generation=claim.lease_generation,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
            lease_expires_at=claim.lease_expires_at,
            attempt_number=claim.attempt_number,
            request_id="req_prd083_half_open_initial",
            trace_id="trc_prd083_half_open_initial",
            current=current,
        )
        assert initial.half_open is False
        assert initial.circuit_revision == 1
        timestamp = current.strftime("%Y-%m-%dT%H:%M:%SZ")
        opened = connection.execute(
            """
            UPDATE mcp_network_circuits
            SET state='open',failure_count=3,opened_until=?,
                last_failure_code='connect_unavailable',
                state_revision=state_revision+1,updated_at=?
            WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
              AND state='closed' AND state_revision=1
            """,
            (
                timestamp,
                timestamp,
                claim.tenant_id,
                claim.server_id,
                initial.policy_revision,
            ),
        ).rowcount
        assert opened == 1
        old_owner = scheduler._start_network_fence(
            connection,
            tenant_id=claim.tenant_id,
            server_id=claim.server_id,
            policy_row=current_policy,
            phase="probe",
            owner_type="invocation",
            owner_id=claim.invocation_id,
            lease_generation=claim.lease_generation,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
            lease_expires_at=claim.lease_expires_at,
            attempt_number=claim.attempt_number,
            request_id="req_prd083_half_open_old_owner",
            trace_id="trc_prd083_half_open_old_owner",
            current=current,
        )
        assert old_owner.half_open is True
        assert old_owner.circuit_revision == 3

        with pytest.raises(ApiError) as captured:
            scheduler._start_network_fence(
                connection,
                tenant_id=claim.tenant_id,
                server_id=claim.server_id,
                policy_row=current_policy,
                phase="probe",
                owner_type="invocation",
                owner_id=claim.invocation_id,
                lease_generation=claim.lease_generation + 1,
                lease_owner_id="prd083-half-open-contender",
                lease_token="a" * 64,
                lease_expires_at=claim.lease_expires_at + timedelta(minutes=2),
                attempt_number=claim.attempt_number + 1,
                request_id="req_prd083_half_open_contended",
                trace_id="trc_prd083_half_open_contended",
                current=current + timedelta(seconds=1),
            )
        assert captured.value.code == "mcp.server_unavailable"

        reclaim_at = claim.lease_expires_at + timedelta(seconds=1)
        new_lease_expires_at = reclaim_at + timedelta(minutes=2)
        new_lease_owner_id = "prd083-half-open-reclaimer"
        new_lease_token = "b" * 64
        renewed = connection.execute(
            """
            UPDATE mcp_invocations
            SET state_revision=state_revision+1,
                probe_attempt_count=probe_attempt_count+1,
                lease_generation=lease_generation+1,lease_owner_id=?,lease_token=?,
                lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_id=? AND status='probing'
              AND lease_generation=? AND lease_owner_id=? AND lease_token=?
              AND lease_expires_at=?
            """,
            (
                new_lease_owner_id,
                new_lease_token,
                new_lease_expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                reclaim_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                claim.tenant_id,
                claim.invocation_id,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                claim.lease_expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        ).rowcount
        assert renewed == 1
        reclaimed = scheduler._start_network_fence(
            connection,
            tenant_id=claim.tenant_id,
            server_id=claim.server_id,
            policy_row=current_policy,
            phase="probe",
            owner_type="invocation",
            owner_id=claim.invocation_id,
            lease_generation=claim.lease_generation + 1,
            lease_owner_id=new_lease_owner_id,
            lease_token=new_lease_token,
            lease_expires_at=new_lease_expires_at,
            attempt_number=claim.attempt_number + 1,
            request_id="req_prd083_half_open_reclaimed",
            trace_id="trc_prd083_half_open_reclaimed",
            current=reclaim_at,
        )
        assert reclaimed.half_open is True
        assert reclaimed.circuit_revision == 5
        circuit = connection.execute(
            """
            SELECT state,state_revision,opened_until,half_open_owner_type,
              half_open_owner_id,half_open_lease_generation,half_open_lease_owner_id,
              half_open_lease_token,half_open_lease_expires_at
            FROM mcp_network_circuits
            WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
            """,
            (claim.tenant_id, claim.server_id, reclaimed.policy_revision),
        ).fetchone()
        assert circuit is not None
        assert tuple(circuit) == (
            "half_open",
            5,
            None,
            "invocation",
            claim.invocation_id,
            claim.lease_generation + 1,
            new_lease_owner_id,
            new_lease_token,
            new_lease_expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.commit()
    finally:
        connection.close()


def _assert_network_outbox_delivery_and_disabled_policy(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_event: sqlite3.Row,
    config: SimpleNamespace,
    claim: scheduler.McpInvocationClaim,
) -> None:
    claim_now = datetime.fromisoformat(
        str(network_event["occurred_at"]).replace("Z", "+00:00")
    ) + timedelta(seconds=1)
    event_claim, progressed = scheduler._claim_network_event_with_progress(
        config,
        worker_id="prd083-network-outbox-success",
        now=claim_now,
    )
    assert progressed is True
    assert event_claim is not None
    stale_claim = replace(event_claim)

    def successful_dispatch(
        _config: object,
        tenant_id: str,
        event: object,
        **kwargs: object,
    ) -> list[object]:
        assert tenant_id == claim.tenant_id
        assert event.event_type == "mcp.network_blocked"
        guard = kwargs["source_lease_guard"]
        assert callable(guard)
        guard()
        return []

    monkeypatch.setattr(
        "metahub_control.webhooks.service.dispatch_webhook_event",
        successful_dispatch,
    )
    assert scheduler._dispatch_claimed_network_event(
        config,
        event_claim,
        now=claim_now,
        should_stop=None,
    )
    assert not scheduler._finish_network_event_claim(
        config,
        stale_claim,
        error_code=None,
        delivery_terminal=False,
        now=claim_now,
    )
    connection = connect_database(config.database_url)
    try:
        outbox = connection.execute(
            """
            SELECT status,attempt_count,lease_generation,error_code,delivered_at
            FROM mcp_network_event_outbox WHERE tenant_id=?
            """,
            (claim.tenant_id,),
        ).fetchone()
        assert outbox is not None
        assert tuple(outbox[:4]) == ("succeeded", 1, 3, None)
        assert outbox["delivered_at"] is not None
    finally:
        connection.close()

    disabled_database = tmp_path / "disabled.sqlite3"
    disabled_database_url = f"sqlite:///{disabled_database.as_posix()}"
    assert apply_migrations(disabled_database_url).applied[-1] == "0076"
    apply_seed_data(disabled_database_url)
    connection = connect_database(disabled_database_url)
    try:
        seeded_admin = connection.execute(
            """
            SELECT user.user_id FROM users user
            JOIN tenants tenant ON tenant.tenant_id=user.tenant_id
            WHERE tenant.slug='default' AND user.role='admin'
            ORDER BY user.user_id LIMIT 1
            """
        ).fetchone()
        assert seeded_admin is not None
        seeded_admin_id = str(seeded_admin["user_id"])
    finally:
        connection.close()

    original_digest = prd082_scheduler._digest

    def runtime_digest(value: str) -> str:
        if value == "runtime-permissions-network-policy-disabled":
            return scheduler._sha256([])
        if value == "runtime-grants-network-policy-disabled":
            return scheduler._sha256([[], [seeded_admin_id]])
        return original_digest(value)

    monkeypatch.setattr(prd082_scheduler, "_digest", runtime_digest)
    try:
        disabled_config, disabled_claim, disabled_legacy, _tool, disabled_ids = (
            prd082_scheduler._prepare_runtime_invocation(
                disabled_database,
                marker="network-policy-disabled",
            )
        )
    finally:
        monkeypatch.setattr(prd082_scheduler, "_digest", original_digest)

    connection = connect_database(disabled_config.database_url)
    try:
        endpoint_id = "wh_" + "9" * 32
        endpoint_secret = "prd083-network-outbox-signing-secret"
        endpoint_created_at = (claim_now + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
              max_attempts,created_by,updated_by,created_at,updated_at,payload_format,
              security_event_rate_limit_per_minute
            ) VALUES(?,?,?,?,?,?,?,?,'active',1000,3,?,?,?,?, 'json',60)
            """,
            (
                endpoint_id,
                disabled_claim.tenant_id,
                "PRD083 network event sink",
                "https://webhook.example.invalid/events",
                '["mcp.network_blocked"]',
                encrypt_secret(
                    endpoint_secret,
                    encryption_secret="prd083-network-outbox-encryption-secret",
                ),
                secret_prefix(endpoint_secret),
                secret_fingerprint(endpoint_secret),
                disabled_ids["actor_id"],
                disabled_ids["actor_id"],
                endpoint_created_at,
                endpoint_created_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    disabled_config.mcp_encryption_version = 1
    disabled_config.mcp_encryption_secret = "prd083-disabled-encryption-secret-" + "e" * 32
    disabled_config.mcp_encryption_previous_version = None
    disabled_config.mcp_encryption_previous_secret = None
    disabled_config.mcp_network_policy_caps = lambda: McpNetworkPolicyCaps()
    monkeypatch.setattr(
        scheduler,
        "decrypt_mcp_value_from_keyring",
        lambda *_args, **_kwargs: (b'{"value":"opaque"}', 1),
    )
    monkeypatch.setattr(
        scheduler,
        "_scan_dlp",
        lambda *_args, **_kwargs: disabled_legacy.input_dlp,
    )
    assert scheduler._prepare_sandbox_invocation(disabled_config, disabled_claim) is None

    connection = connect_database(disabled_config.database_url)
    try:
        disabled = connection.execute(
            """
            SELECT invocation.status,invocation.error_code,invocation.execution_status,
              invocation.tool_call_count,invocation.cost_status,event.reason_code,
              event.address_count,outbox.status,outbox.subscription_snapshot_json,
              delivery.status AS delivery_status
            FROM mcp_invocations invocation
            JOIN mcp_network_events event
              ON event.tenant_id=invocation.tenant_id
             AND event.mcp_invocation_id=invocation.mcp_invocation_id
            JOIN mcp_network_event_outbox outbox
              ON outbox.tenant_id=event.tenant_id
             AND outbox.mcp_network_event_id=event.mcp_network_event_id
            LEFT JOIN webhook_deliveries delivery
              ON delivery.tenant_id=event.tenant_id
             AND delivery.event_id=event.mcp_network_event_id
             AND delivery.delivery_kind='original'
            WHERE invocation.tenant_id=? AND invocation.mcp_invocation_id=?
            """,
            (disabled_claim.tenant_id, disabled_claim.invocation_id),
        ).fetchone()
        assert disabled is not None
        snapshot_delivery_id = connection.execute(
            """
            SELECT webhook_delivery_id FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=(
              SELECT mcp_network_event_id FROM mcp_network_events
              WHERE tenant_id=? AND mcp_invocation_id=?
            )
            """,
            (
                disabled_claim.tenant_id,
                disabled_claim.tenant_id,
                disabled_claim.invocation_id,
            ),
        ).fetchone()[0]
        assert tuple(disabled) == (
            "blocked",
            "mcp.network_blocked",
            "not_called",
            0,
            "not_applicable",
            "policy_disabled",
            0,
            "pending",
            json.dumps([str(snapshot_delivery_id)], separators=(",", ":")),
            "pending",
        )
        pending_delivery = connection.execute(
            """
            SELECT delivery.* FROM webhook_deliveries delivery
            JOIN mcp_network_events event
              ON event.tenant_id=delivery.tenant_id
             AND event.mcp_network_event_id=delivery.event_id
            WHERE event.tenant_id=? AND event.mcp_invocation_id=?
              AND delivery.delivery_kind='original'
            """,
            (disabled_claim.tenant_id, disabled_claim.invocation_id),
        ).fetchone()
        assert pending_delivery is not None
        assert webhook_service._validated_pending_delivery_body(  # noqa: SLF001
            connection,
            pending_delivery,
        ) == str(pending_delivery["wire_body_text"]).encode("utf-8")
    finally:
        connection.close()

    def unavailable_dispatch(*_args: object, **kwargs: object) -> list[object]:
        guard = kwargs["source_lease_guard"]
        assert callable(guard)
        guard()
        raise RuntimeError("fixed-test-failure")

    monkeypatch.setattr(
        "metahub_control.webhooks.service.dispatch_webhook_event",
        unavailable_dispatch,
    )
    assert scheduler.run_mcp_outbox_once(
        disabled_config,
        worker_id="prd083-network-outbox-retry-1",
    )
    for attempt in range(2, 11):
        connection = connect_database(disabled_config.database_url)
        try:
            retry = connection.execute(
                """
                SELECT outbox.next_attempt_at FROM mcp_network_event_outbox outbox
                JOIN mcp_network_events event
                  ON event.tenant_id=outbox.tenant_id
                 AND event.mcp_network_event_id=outbox.mcp_network_event_id
                WHERE event.tenant_id=? AND event.mcp_invocation_id=?
                """,
                (disabled_claim.tenant_id, disabled_claim.invocation_id),
            ).fetchone()
            assert retry is not None and retry["next_attempt_at"] is not None
            retry_at = datetime.fromisoformat(
                str(retry["next_attempt_at"]).replace("Z", "+00:00")
            ) + timedelta(seconds=1)
        finally:
            connection.close()
        assert scheduler._drain_network_event_once(
            disabled_config,
            worker_id=f"prd083-network-outbox-retry-{attempt}",
            now=retry_at,
        )

    connection = connect_database(disabled_config.database_url)
    try:
        dead_letter = connection.execute(
            """
            SELECT outbox.status,outbox.attempt_count,outbox.error_code,
              outbox.next_attempt_at,delivery.status,delivery.error_code
            FROM mcp_network_event_outbox outbox
            JOIN mcp_network_events event
              ON event.tenant_id=outbox.tenant_id
             AND event.mcp_network_event_id=outbox.mcp_network_event_id
            JOIN webhook_deliveries delivery
              ON delivery.tenant_id=event.tenant_id
             AND delivery.event_id=event.mcp_network_event_id
             AND delivery.delivery_kind='original'
            WHERE event.tenant_id=? AND event.mcp_invocation_id=?
            """,
            (disabled_claim.tenant_id, disabled_claim.invocation_id),
        ).fetchone()
        assert dead_letter is not None
        assert tuple(dead_letter) == (
            "failed",
            10,
            "webhook.delivery_unavailable",
            None,
            "failed",
            "webhook.delivery_unavailable",
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
