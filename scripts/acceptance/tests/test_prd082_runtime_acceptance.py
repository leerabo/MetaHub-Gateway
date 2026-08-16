"""Static and unit safety contracts for the PRD-082 runtime acceptance runner."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "acceptance" / "prd082-runtime-acceptance.py"


def _load():
    name = "_metahub_prd082_runtime_contract"
    current = sys.modules.get(name)
    if current is not None:
        return current
    specification = importlib.util.spec_from_file_location(name, SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _complete_payload(module):
    payload = module.empty_evidence_payload()
    payload["status"] = "passed"
    payload["migration"] = {
        "fresh": {"count": 75, "version": "0075", "integrity": "ok", "foreign_key_violations": 0},
        "repeat": {"count": 75, "version": "0075", "integrity": "ok", "foreign_key_violations": 0},
        "down": {"count": 74, "version": "0074", "integrity": "ok", "foreign_key_violations": 0},
        "reup": {"count": 75, "version": "0075", "integrity": "ok", "foreign_key_violations": 0},
    }
    payload["services"] = {name: True for name in module.SERVICE_FACTS}
    payload["transport"] = {name: True for name in module.TRANSPORT_FACTS}
    payload["api"] = {name: True for name in module.API_FACTS}
    payload["governance"] = dict(module.GOVERNANCE_FACTS)
    payload["traceability"] = dict(module.TRACEABILITY_FACTS)
    payload["database"] = dict(module.DATABASE_FACTS)
    payload["privacy"] = dict(module.PRIVACY_FACTS)
    payload["cleanup"] = dict(module.CLEANUP_FACTS)
    payload["source_provenance"] = module.source_provenance()
    return payload


def test_empty_evidence_is_explicitly_incomplete_and_exact() -> None:
    module = _load()
    payload = module.empty_evidence_payload()
    assert set(payload) == module.EVIDENCE_TOP_LEVEL_KEYS
    assert payload["prd_id"] == "PRD-082"
    assert payload["status"] == "incomplete"
    assert payload["all_108_prds_complete"] is False


def test_complete_evidence_contract_accepts_only_the_0075_reversible_gate() -> None:
    module = _load()
    payload = _complete_payload(module)
    module._validate_evidence(payload)
    payload["migration"]["down"]["version"] = "0073"
    with pytest.raises(module.AcceptanceFailure):
        module._validate_evidence(payload)


@pytest.mark.parametrize(
    ("section", "key"),
    (
        ("services", "production_executor_zero_network"),
        ("transport", "tools_list_completed"),
        ("api", "uniform_request_id"),
        ("governance", "input_block_zero_call"),
        ("traceability", "failed_request_tracking_count"),
        ("database", "active_leases"),
        ("privacy", "plaintext_matches"),
        ("cleanup", "fixed_ports_released"),
        ("source_provenance", "runtime_helper_prd075"),
    ),
)
def test_evidence_rejects_deleted_required_facts(section: str, key: str) -> None:
    module = _load()
    payload = _complete_payload(module)
    del payload[section][key]
    with pytest.raises(module.AcceptanceFailure):
        module._validate_evidence(payload)


@pytest.mark.parametrize(
    ("section", "key", "invalid"),
    (
        ("services", "production_executor_zero_network", False),
        ("transport", "tools_call_success_completed", False),
        ("api", "tenant_isolation_hidden", False),
        ("governance", "known_error_code", "mcp.unavailable"),
        ("traceability", "failed_request_tracking_count", 0),
        ("database", "active_leases", 1),
        ("privacy", "plaintext_matches", 1),
        ("cleanup", "fixed_ports_released", False),
        ("source_provenance", "runtime_helper_prd075", "0" * 64),
    ),
)
def test_evidence_rejects_mutated_required_facts(
    section: str,
    key: str,
    invalid: object,
) -> None:
    module = _load()
    payload = _complete_payload(module)
    payload[section][key] = invalid
    with pytest.raises(module.AcceptanceFailure):
        module._validate_evidence(payload)


def test_evidence_rejects_private_values_and_forbidden_keys() -> None:
    module = _load()
    private = "Prd082-Private-Value-For-Contract"
    with pytest.raises(module.AcceptanceFailure):
        module.assert_safe_evidence({"safe": private}, (private,))
    for key in ("endpoint_url", "bearer_token", "arguments", "result", "request_id"):
        with pytest.raises(module.AcceptanceFailure):
            module.assert_safe_evidence({key: "redacted"}, ())


@pytest.mark.parametrize(
    "value",
    ("..", "../escape", "a/b", "a\\b", "", "x" * 81),
)
def test_run_id_rejects_escape_and_unbounded_values(value: str) -> None:
    module = _load()
    with pytest.raises(module.AcceptanceFailure):
        module._safe_run_id(value)


def test_runner_freezes_official_sdk_loopback_and_privacy_contracts() -> None:
    module = _load()
    source = SCRIPT.read_text(encoding="utf-8")
    assert module.FIXED_PORTS == (18920, 18921)
    assert module.EXPECTED_MIGRATION_COUNT == 75
    assert module.EXPECTED_MIGRATION_VERSION == "0075"
    assert module.PREVIOUS_MIGRATION_VERSION == "0074"
    assert "from mcp.server.fastmcp import FastMCP" in source
    assert 'server.run(transport="streamable-http")' in source
    assert "McpDiscoverRequest" in source
    assert "McpProbeRequest" in source
    assert "McpCallRequest" in source
    assert "class RuntimeMcpResult(BaseModel):" in source
    assert 'ConfigDict(extra="forbid", frozen=True)' in source
    assert "mcp.executor_disabled" in source
    assert "mcp.arguments_blocked" in source
    assert "mcp.tool_failed" in source
    assert "mcp.outcome_unknown" in source
    assert '"all_108_prds_complete": False' in source
    assert "sandbox_permissions" not in source
    assert "require_escalated" not in source


def test_source_provenance_is_sha256_and_repository_owned() -> None:
    module = _load()
    provenance = module.source_provenance()
    assert set(provenance) == set(module.SOURCE_FILES)
    assert all(len(value) == 64 for value in provenance.values())
    assert all(set(value) <= set("0123456789abcdef") for value in provenance.values())
    assert {
        "mcp_outbox",
        "mcp_shared_events",
        "mcp_shared_package",
        "mcp_shared_schema",
        "runtime_helper_prd081",
        "runtime_helper_prd080",
        "runtime_helper_prd079",
        "runtime_helper_prd075",
    } <= set(provenance)


def _remove_owned(module, path: Path, root: Path, owner: str) -> None:
    if not path.exists():
        return
    module._remove_owned_tree(path, root, owner, stage="cleanup.unit")


def _publication_fixture(module, label: str):
    run_id = f"unit-{label}-{uuid.uuid4().hex}"
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, final = module._create_directories(run_id, owner)
    payload = _complete_payload(module)
    return owner, work, staging, final, payload


def test_runtime_directory_marker_write_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    run_id = f"unit-marker-{uuid.uuid4().hex}"
    work = module.WORK_ROOT / run_id
    staging = module.ARTIFACT_ROOT / f".{run_id}.staging"
    marker = staging / module.OWNER_FILENAME
    original = Path.write_text

    def fail_marker(path: Path, *args: object, **kwargs: object) -> int:
        if path == marker:
            raise OSError("injected")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_marker)
    with pytest.raises(OSError, match="injected"):
        module._create_directories(run_id, hashlib.sha256(uuid.uuid4().bytes).hexdigest())
    assert not work.exists()
    assert not staging.exists()


def test_publish_write_failure_keeps_staging_owner_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    owner, work, staging, final, payload = _publication_fixture(module, "write")
    evidence = staging / "runtime-acceptance.json"
    original = Path.open

    def fail_evidence(path: Path, *args: object, **kwargs: object):
        if path == evidence and args and args[0] == "xb":
            raise OSError("injected")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_evidence)
    try:
        with pytest.raises(OSError, match="injected"):
            module._write_evidence(staging, final, owner, payload, (), payload["source_provenance"])
        assert (staging / module.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
        assert not final.exists()
    finally:
        _remove_owned(module, staging, module.ARTIFACT_ROOT, owner)
        _remove_owned(module, work, module.WORK_ROOT, owner)


def test_publish_staging_scan_failure_keeps_owner_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    owner, work, staging, final, payload = _publication_fixture(module, "staging-scan")

    def fail_scan(path: Path, _private_values: tuple[str, ...]):
        if path == staging:
            raise module.AcceptanceFailure("privacy.scan", "injected")
        return {"files_scanned": 0, "plaintext_matches": 0, "sqlite_sidecars_remaining": 0}

    monkeypatch.setattr(module.BASE, "scan_private_storage", fail_scan)
    try:
        with pytest.raises(module.AcceptanceFailure):
            module._write_evidence(staging, final, owner, payload, (), payload["source_provenance"])
        assert (staging / module.OWNER_FILENAME).is_file()
        assert not final.exists()
    finally:
        _remove_owned(module, staging, module.ARTIFACT_ROOT, owner)
        _remove_owned(module, work, module.WORK_ROOT, owner)


def test_publish_final_scan_failure_removes_formal_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    owner, work, staging, final, payload = _publication_fixture(module, "final-scan")

    def scan(path: Path, _private_values: tuple[str, ...]):
        if path == final:
            raise module.AcceptanceFailure("privacy.scan", "injected")
        return {"files_scanned": 0, "plaintext_matches": 0, "sqlite_sidecars_remaining": 0}

    monkeypatch.setattr(module.BASE, "scan_private_storage", scan)
    try:
        with pytest.raises(module.AcceptanceFailure):
            module._write_evidence(staging, final, owner, payload, (), payload["source_provenance"])
        assert not staging.exists()
        assert not final.exists()
    finally:
        _remove_owned(module, final, module.ARTIFACT_ROOT, owner)
        _remove_owned(module, staging, module.ARTIFACT_ROOT, owner)
        _remove_owned(module, work, module.WORK_ROOT, owner)


def test_publish_helper_drift_after_rename_removes_formal_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    owner, work, staging, final, payload = _publication_fixture(module, "helper-drift")
    expected = copy.deepcopy(payload["source_provenance"])
    changed = {**expected, "runtime_helper_prd075": "0" * 64}
    snapshots = iter((expected, expected, changed))

    monkeypatch.setattr(
        module.BASE,
        "scan_private_storage",
        lambda *_args: {"files_scanned": 0, "plaintext_matches": 0, "sqlite_sidecars_remaining": 0},
    )
    monkeypatch.setattr(module, "source_provenance", lambda: next(snapshots))
    try:
        with pytest.raises(module.AcceptanceFailure):
            module._write_evidence(staging, final, owner, payload, (), expected)
        assert not staging.exists()
        assert not final.exists()
    finally:
        _remove_owned(module, final, module.ARTIFACT_ROOT, owner)
        _remove_owned(module, staging, module.ARTIFACT_ROOT, owner)
        _remove_owned(module, work, module.WORK_ROOT, owner)


def test_hidden_worker_modes_do_not_contain_private_cli_parameters() -> None:
    module = _load()
    serve = module.parse_arguments(["--serve-mcp", "--mcp-port", str(module.MCP_PORT)])
    worker = module.parse_arguments(["--scheduler-once"])
    assert serve.serve_mcp is True
    assert worker.scheduler_once is True
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--bearer-token" not in source
    assert "--endpoint" not in source
    assert "--arguments" not in source
    assert "--result" not in source


def test_outbox_drain_waits_for_a_concurrent_active_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    database = tmp_path / "concurrent-outbox.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE mcp_invocation_event_outbox(
              status TEXT NOT NULL,
              lease_expires_at TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO mcp_invocation_event_outbox VALUES"
            "('processing',strftime('%Y-%m-%dT%H:%M:%SZ','now','+30 seconds'))"
        )

    calls = 0

    def no_manual_progress(*_args: object, **_kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        return False

    def complete_in_background(_seconds: float) -> None:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE mcp_invocation_event_outbox SET status='succeeded',lease_expires_at=NULL"
            )

    monkeypatch.setattr(module, "run_mcp_outbox_once", no_manual_progress)
    monkeypatch.setattr(module.time, "sleep", complete_in_background)

    module._drain_mcp_outbox(
        SimpleNamespace(),
        SimpleNamespace(database_url=database_url),
    )

    assert calls == 2
