from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "acceptance" / "prd083-runtime-acceptance.py"


def _load():
    name = "_metahub_prd083_runtime_acceptance_tests"
    current = sys.modules.get(name)
    if current is not None:
        return current
    specification = importlib.util.spec_from_file_location(name, SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


module = _load()


def _complete_payload():
    payload = module.empty_evidence_payload()
    payload.update(
        {
            "status": "passed",
            "generated_at": "2026-08-16T12:00:00Z",
            "migration": {
                "fresh": {
                    "count": 76,
                    "version": "0076",
                    "integrity": "ok",
                    "foreign_key_violations": 0,
                },
                "repeat": {
                    "count": 76,
                    "version": "0076",
                    "integrity": "ok",
                    "foreign_key_violations": 0,
                },
                "down": {
                    "count": 75,
                    "version": "0075",
                    "integrity": "ok",
                    "foreign_key_violations": 0,
                },
                "reup": {
                    "count": 76,
                    "version": "0076",
                    "integrity": "ok",
                    "foreign_key_violations": 0,
                },
            },
            "services": dict(module.SERVICE_FACTS),
            "workflow": dict(module.WORKFLOW_FACTS),
            "network": dict(module.NETWORK_FACTS),
            "traceability": {
                "network_decision_count": 5,
                "call_intent_count": 2,
                "network_event_count": 1,
                "webhook_delivery_count": 1,
                "remote_tool_call_count": 2,
                "failed_request_tracking_count": 2,
                "cost_status_tracking_count": 2,
            },
            "database": dict(module.DATABASE_FACTS),
            "privacy": {
                "files_scanned": 10,
                "plaintext_matches": 0,
                "evidence_forbidden_key_matches": 0,
                "sqlite_sidecars_remaining": 0,
            },
            "cleanup": dict(module.CLEANUP_FACTS),
            "source_provenance": module.source_provenance(),
        }
    )
    return payload


def test_empty_evidence_is_explicitly_incomplete_and_exact() -> None:
    payload = module.empty_evidence_payload()
    assert set(payload) == module.EVIDENCE_TOP_LEVEL_KEYS
    assert payload["prd_id"] == "PRD-083"
    assert payload["status"] == "incomplete"
    assert payload["all_108_prds_complete"] is False


def test_complete_evidence_accepts_only_the_0076_reversible_gate() -> None:
    module._validate_evidence(_complete_payload())


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("services", "independent_scheduler"),
        ("workflow", "waiting_policy_observed"),
        ("network", "evt095_delivered"),
        ("database", "active_leases"),
        ("cleanup", "sandbox_active_sessions"),
        ("source_provenance", "sandbox_transport"),
    ],
)
def test_evidence_rejects_deleted_required_facts(section: str, key: str) -> None:
    payload = _complete_payload()
    del payload[section][key]
    with pytest.raises(module.AcceptanceFailure):
        module._validate_evidence(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("migration", "fresh", "version"), "0075"),
        (("migration", "down", "count"), 76),
        (("services", "independent_sandbox"), False),
        (("workflow", "timeout_outcome_unknown"), False),
        (("network", "circuit_opened"), False),
        (("database", "foreign_key_violations"), 1),
        (("privacy", "plaintext_matches"), 1),
        (("cleanup", "fixed_ports_released"), False),
    ],
)
def test_evidence_rejects_mutated_required_facts(path: tuple[str, ...], value: object) -> None:
    payload = _complete_payload()
    cursor = payload
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value
    with pytest.raises(module.AcceptanceFailure):
        module._validate_evidence(payload)


def test_evidence_rejects_private_values_and_forbidden_keys() -> None:
    private = "Prd083-Private-" + "x" * 64
    with pytest.raises(module.AcceptanceFailure):
        module.assert_safe_evidence({"safe": private}, (private,))
    for key in module.FORBIDDEN_EVIDENCE_KEYS:
        with pytest.raises(module.AcceptanceFailure):
            module.assert_safe_evidence({key: "redacted"}, ())


@pytest.mark.parametrize(
    "value",
    ["../escape", "..\\escape", "a/b", "a\\b", "ab", "a" * 81],
)
def test_run_id_rejects_escape_and_unbounded_values(value: str) -> None:
    with pytest.raises(module.AcceptanceFailure):
        module._safe_run_id(value)


def test_operation_timeout_detail_reports_only_bounded_state_enums() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE mcp_server_operations(mcp_server_operation_id TEXT, status TEXT)"
    )
    connection.execute(
        "CREATE TABLE mcp_server_sync_jobs(mcp_server_operation_id TEXT, status TEXT)"
    )
    connection.execute("INSERT INTO mcp_server_operations VALUES('operation', 'processing')")
    connection.execute("INSERT INTO mcp_server_sync_jobs VALUES('operation', 'pending')")
    assert module._operation_timeout_detail(connection, "operation") == "timeout.processing.pending"
    connection.execute("UPDATE mcp_server_operations SET status='PRIVATE VALUE'")
    connection.execute("UPDATE mcp_server_sync_jobs SET status='PRIVATE VALUE'")
    assert module._operation_timeout_detail(connection, "operation") == "timeout.missing.missing"


def test_invocation_terminal_detail_reports_only_bounded_status_and_error() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    failed = connection.execute(
        "SELECT 'failed' AS status, 'mcp.sandbox_unavailable' AS error_code"
    ).fetchone()
    unsafe = connection.execute(
        "SELECT 'failed' AS status, 'PRIVATE VALUE' AS error_code"
    ).fetchone()
    assert failed is not None and unsafe is not None
    assert (
        module._invocation_terminal_detail(failed, "probe")
        == "failed.mcp.sandbox_unavailable.probe"
    )
    assert module._invocation_terminal_detail(unsafe) == "terminal.unknown"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError(), "timeout"),
        (RuntimeError(), "runtime"),
        (ValueError(), "contract"),
        (Exception("private detail"), "unexpected"),
        (ExceptionGroup("private detail", [RuntimeError(), TimeoutError()]), "timeout"),
    ],
)
def test_sandbox_exception_class_is_fixed_and_contains_no_exception_text(
    failure: BaseException, expected: str
) -> None:
    assert module._sandbox_exception_class(failure) == expected


def test_sandbox_exception_class_uses_only_fixed_network_stage_reasons() -> None:
    error = RuntimeError("private")
    error.reason_code = "overall_timeout"  # type: ignore[attr-defined]
    assert module._sandbox_exception_class(error) == "overall"
    error.reason_code = "dns_unavailable"  # type: ignore[attr-defined]
    assert module._sandbox_exception_class(error) == "dns"
    error.reason_code = "private-upstream-detail"  # type: ignore[attr-defined]
    assert module._sandbox_exception_class(error) == "runtime"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (sqlite3.IntegrityError("mcp_invocation_transition_invalid"), "invocation_transition"),
        (sqlite3.IntegrityError("mcp_network_decision_owner_mismatch"), "decision_owner"),
        (sqlite3.IntegrityError("mcp_network_call_intent_owner_mismatch"), "intent_owner"),
        (sqlite3.IntegrityError("mcp_attempt_owner_mismatch"), "attempt_owner"),
        (sqlite3.OperationalError("PRIVATE VALUE"), "other_sql"),
        (ValueError("PRIVATE VALUE"), None),
    ],
)
def test_scheduler_sqlite_failure_class_is_fixed_and_contains_no_exception_text(
    failure: BaseException, expected: str | None
) -> None:
    assert module._scheduler_sqlite_failure_class(failure) == expected


def test_scheduler_sqlite_failure_class_traverses_wrapped_causes() -> None:
    try:
        try:
            raise sqlite3.IntegrityError("mcp_network_call_intent_owner_mismatch")
        except sqlite3.Error as failure:
            raise RuntimeError("PRIVATE VALUE") from failure
    except RuntimeError as wrapped:
        assert module._scheduler_sqlite_failure_class(wrapped) == "intent_owner"


def test_sync_finalize_timing_class_prioritizes_deadline_then_lease(tmp_path: Path) -> None:
    database = tmp_path / "sync-timing.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE mcp_server_sync_jobs(
          tenant_id TEXT,mcp_server_sync_job_id TEXT,deadline_at TEXT,lease_expires_at TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO mcp_server_sync_jobs VALUES(?,?,?,?)",
        ("tenant", "job", "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    config = type("Config", (), {"database_url": f"sqlite:///{database.as_posix()}"})()
    claim = type("Claim", (), {"tenant_id": "tenant", "job_id": "job"})()
    assert module._sync_finalize_timing_class(config, claim) == "sync_deadline_expired"
    connection = sqlite3.connect(database)
    connection.execute("UPDATE mcp_server_sync_jobs SET deadline_at='2099-01-01T00:00:00Z'")
    connection.commit()
    connection.close()
    assert module._sync_finalize_timing_class(config, claim) == "sync_lease_expired"
    connection = sqlite3.connect(database)
    connection.execute("UPDATE mcp_server_sync_jobs SET lease_expires_at='2099-01-01T00:00:00Z'")
    connection.commit()
    connection.close()
    assert module._sync_finalize_timing_class(config, claim) is None


@pytest.mark.parametrize(
    ("action", "status", "failure_code", "expected"),
    [
        ("discover", "succeeded", None, "sync_discover_ok"),
        ("probe", "failed", "mcp.server_unavailable", "invoke_probe_server"),
        ("authorize-call", "succeeded", None, "invoke_auth_ok"),
        ("call", "failed", "mcp.outcome_unknown", "invoke_call_outcome"),
        ("PRIVATE VALUE", "failed", "mcp.server_unavailable", None),
        ("probe", "PRIVATE VALUE", "PRIVATE VALUE", "invoke_probe_invalid"),
    ],
)
def test_scheduler_dispatch_marker_uses_only_fixed_action_and_result(
    action: object, status: object, failure_code: object, expected: str | None
) -> None:
    assert module._scheduler_dispatch_marker(action, status, failure_code) == expected


def test_fixed_diagnostics_append_deduplicates_and_rejects_unknown_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "diagnostic.txt"
    allowed = frozenset({"invoke_probe_ok", "invoke_intent_owner"})
    module._append_fixed_diagnostic(path, "PRIVATE VALUE", allowed)
    assert not path.exists()
    module._append_fixed_diagnostic(path, "invoke_probe_ok", allowed)
    module._append_fixed_diagnostic(path, "invoke_probe_ok", allowed)
    module._append_fixed_diagnostic(path, "invoke_intent_owner", allowed)
    assert module._read_fixed_diagnostics(path, allowed) == (
        "invoke_probe_ok",
        "invoke_intent_owner",
    )


def test_failure_diagnostic_selects_current_workflow_and_latest_marker() -> None:
    scheduler = (
        "sync_discover_ok",
        "sync_finalize_success",
        "invoke_probe_ok",
        "invoke_auth_ok",
        "invoke_intent_owner",
    )
    sandbox = ("discover_os", "probe_timeout")
    invoke = module.AcceptanceFailure("workflow.invoke_success", "failed.mcp.unavailable")
    discover = module.AcceptanceFailure("workflow.discover", "mcp.state_conflict")
    assert module._select_failure_diagnostic(invoke, scheduler, sandbox) == "invoke_intent_owner"
    assert (
        module._select_failure_diagnostic(discover, scheduler, sandbox) == "sync_finalize_success"
    )
    assert (
        module._select_failure_diagnostic(
            module.AcceptanceFailure("workflow.discover", "mcp.sync_failed"),
            ("sync_discover_server",),
            ("discover_network",),
        )
        == "discover_network"
    )
    assert (
        module._select_failure_diagnostic(
            module.AcceptanceFailure("http.register_blocked", "rbac.unavailable"),
            scheduler,
            sandbox,
        )
        is None
    )


def test_diagnostic_failure_code_appends_or_replaces_only_within_bound() -> None:
    assert (
        module._diagnostic_failure_code("mcp.state_conflict", "sync_prepare")
        == "mcp.state_conflict.sync_prepare"
    )
    assert (
        module._diagnostic_failure_code("failed.mcp.unavailable.probe", "invoke_prepare")
        == "failed.mcp.unavailable.invoke_prepare"
    )
    assert (
        module._diagnostic_failure_code("failed.mcp.unavailable.probe", "invoke_intent_owner")
        == "failed.mcp.unavailable.invoke_intent_owner"
    )


def test_every_fixed_runtime_diagnostic_fits_the_unified_error_code_bound() -> None:
    diagnostics = module._SANDBOX_FAILURE_DIAGNOSTICS | module._SCHEDULER_FAILURE_DIAGNOSTICS
    assert diagnostics
    for diagnostic in diagnostics:
        assert (
            module._diagnostic_failure_code("failed.mcp.unavailable.probe", diagnostic) is not None
        )


def test_scheduler_transport_diagnostics_are_fixed_safe_classes() -> None:
    assert (
        module._scheduler_transport_failure_class(httpx.ConnectTimeout("private"))
        == "connect_timeout"
    )
    assert (
        module._scheduler_transport_failure_class(httpx.ConnectError("private")) == "connect_error"
    )
    assert module._scheduler_transport_failure_class(httpx.ReadTimeout("private")) == "read_timeout"
    assert (
        module._scheduler_transport_failure_class(httpx.RemoteProtocolError("private"))
        == "protocol"
    )
    assert module._scheduler_transport_status_class(200) == "http_200"
    assert module._scheduler_transport_status_class(401) == "http_4xx"
    assert module._scheduler_transport_status_class(503) == "http_5xx"
    assert module._scheduler_transport_status_class("private") == "http_other"


def test_role_environment_matrix_keeps_the_three_secret_boundaries(tmp_path: Path) -> None:
    runtime = module.RuntimeSecrets.create()
    base = {
        "PATH": "runtime-path",
        "METAHUB_DATABASE_URL": "sqlite:///private",
        "METAHUB_MCP_HMAC_SECRET": runtime.legacy.mcp_hmac_secret,
    }
    control, scheduler, sandbox = module.role_environments(base, runtime, tmp_path)
    module._assert_role_isolation(control, scheduler, sandbox)
    assert control["METAHUB_MCP_EXECUTOR_MODE"] == "disabled"
    assert not any(key.startswith("METAHUB_MCP_SANDBOX_") for key in control)
    assert scheduler["METAHUB_MCP_EXECUTOR_MODE"] == "sandbox_http"
    assert "METAHUB_MCP_SANDBOX_PERMIT_SECRET" not in scheduler
    assert "METAHUB_MCP_SANDBOX_PRIVATE_CIDRS" not in scheduler
    assert sandbox["METAHUB_MCP_SANDBOX_PERMIT_SECRET"] == runtime.sandbox_permit
    assert "METAHUB_DATABASE_URL" not in sandbox
    assert "METAHUB_MCP_HMAC_SECRET" not in sandbox


def test_runner_freezes_real_process_and_privacy_contracts() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert module.FIXED_PORTS == (18930, 18931, 18932, 18933, 18934)
    assert module.EXPECTED_MIGRATION_COUNT == 76
    assert module.EXPECTED_MIGRATION_VERSION == "0076"
    assert module.PREVIOUS_MIGRATION_VERSION == "0075"
    assert module.SCHEDULER_POLL_INTERVAL_SECONDS == "2"
    assert module.OPERATION_LEASE_SECONDS == "60"
    assert module.INVOCATION_LEASE_SECONDS == "90"
    assert "metahub_control.mcp_governance.scheduler_main" in source
    assert "create_sandbox_app()" in source
    assert "server.streamable_http_app()" in source
    assert "ssl_certfile=str(cert)" in source
    assert "TransportSecuritySettings(" in source
    assert "enable_dns_rebinding_protection=True" in source
    assert 'allowed_hosts=[f"{TARGET_HOST}:{MCP_PORT}"]' in source
    assert "_SAFE_ERROR_CODE.fullmatch(failure)" in source
    assert "mcp.network_blocked" in source
    assert "mcp.outcome_unknown" in source
    assert "mcp_network_event_outbox WHERE tenant_id=? AND status='succeeded'" in source
    assert "status='delivered'" not in source
    assert "timeout.{detail}" in source
    assert "pending_unclaimed" in source and "pending_retry" in source
    assert "scheduler_alive" in source and "scheduler_exited" in source
    assert 'ready.get("owned_tasks") == 0' in source
    assert 'ready.get("active_tasks")' not in source
    assert '"all_108_prds_complete": False' in source
    assert "sandbox_permissions" not in source
    assert "require_escalated" not in source


def test_hidden_process_modes_do_not_accept_private_cli_values() -> None:
    parser = module.parse_arguments
    assert parser(["--serve-mcp"]).serve_mcp is True
    assert parser(["--serve-sandbox"]).serve_sandbox is True
    assert parser(["--serve-scheduler"]).serve_scheduler is True
    assert parser(["--serve-webhook"]).serve_webhook is True
    assert parser(["--diagnose-discovery"]).diagnose_discovery is True
    source = SCRIPT.read_text(encoding="utf-8")
    for flag in (
        "--endpoint",
        "--ip",
        "--cidr",
        "--token",
        "--secret",
        "--permit",
        "--arguments",
        "--result",
    ):
        assert flag not in source


def test_source_provenance_is_sha256_and_repository_owned() -> None:
    provenance = module.source_provenance()
    assert set(provenance) == set(module.SOURCE_FILES)
    assert all(len(value) == 64 for value in provenance.values())
    assert all(set(value) <= set("0123456789abcdef") for value in provenance.values())
    assert all(
        path.resolve().is_relative_to(ROOT.resolve()) for path in module.SOURCE_FILES.values()
    )


def test_safe_evidence_round_trip_contains_no_network_target_fields() -> None:
    payload = _complete_payload()
    module._validate_evidence(payload)
    module.assert_safe_evidence(payload, ())
    encoded = json.dumps(payload, sort_keys=True).casefold()
    for name in ("endpoint_url", '"ip"', '"cidr"', '"token"', '"permit"'):
        assert name not in encoded


def test_evidence_validation_does_not_mutate_payload() -> None:
    payload = _complete_payload()
    before = copy.deepcopy(payload)
    module._validate_evidence(payload)
    assert payload == before
