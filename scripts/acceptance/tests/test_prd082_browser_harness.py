"""Ownership, migration, RBAC, privacy, and evidence contracts for PAGE-114."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
import uuid
from pathlib import Path

import pytest
from metahub_shared.db import connect_database

ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "acceptance" / "prd082-browser-harness.py"


def _load_harness():
    name = "_metahub_prd082_browser_harness_tests"
    current = sys.modules.get(name)
    if current is not None:
        return current
    specification = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


HARNESS = _load_harness()


def _run_id(label: str) -> str:
    return f"unit-{label}-{uuid.uuid4().hex}"


def _remove_claimed(path: Path, root: Path, owner: str) -> None:
    if not path.exists():
        return
    claim = path / HARNESS.OWNER_FILENAME
    if not claim.exists():
        claim.write_text(f"{owner}\n", encoding="ascii")
    HARNESS._remove_owned_directory(path, root, owner, stage="cleanup.unit")


def _complete_facts() -> dict[str, object]:
    desktop = {name: True for name in HARNESS.DESKTOP_FACT_KEYS}
    desktop.update({"width": 1280, "height": 720, "overlap_count": 0})
    mobile = {name: True for name in HARNESS.MOBILE_FACT_KEYS}
    mobile.update({"width": 390, "height": 844, "overlap_count": 0})
    return {
        "status": "passed",
        "desktop": desktop,
        "mobile": mobile,
        "lifecycle": {name: True for name in HARNESS.LIFECYCLE_FACT_KEYS},
        "network": {
            "external_requests": 0,
            "forbidden_queries": 0,
            "console_errors": 0,
            "page_errors": 0,
            "login_statuses": [200, 200, 200, 200, 200],
            "logout_statuses": [200, 200, 200, 200, 200],
            "registration_statuses": [202],
            "refresh_statuses": [202],
            "server_statuses": [202, 202],
            "policy_statuses": [200],
            "list_successes": 6,
            "detail_successes": 2,
            "no_store_responses": True,
            "request_contracts_exact": True,
        },
        "screenshots": list(HARNESS.SCREENSHOTS),
        "all_108_prds_complete": False,
    }


def test_repository_boundaries_and_artifact_whitelist_are_frozen() -> None:
    assert HARNESS.ROOT == ROOT
    assert HARNESS.WORK_ROOT == ROOT / ".tmp" / "prd082-browser-harness"
    assert HARNESS.ARTIFACT_ROOT == ROOT / ".artifacts" / "prd082-browser"
    assert HARNESS.HARNESS_PORT == 18885
    assert HARNESS.RUNTIME.CONTROL_PORT == 8001
    assert HARNESS.RUNTIME.CONSOLE_PORT == 5173
    assert HARNESS.EXPECTED_MIGRATION_COUNT == 75
    assert HARNESS.EXPECTED_MIGRATION_VERSION == "0075"
    assert HARNESS.ALLOWED_ARTIFACTS == frozenset(
        (*HARNESS.SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json")
    )
    assert all(path.resolve().is_relative_to(ROOT) for path in HARNESS.SOURCE_FILES.values())
    assert {
        "browser_helper_prd080",
        "browser_helper_prd078",
        "browser_helper_prd066",
        "browser_helper_prd056",
        "runtime_helper_prd078",
        "runtime_helper_prd075",
    } <= set(HARNESS.SOURCE_FILES)


def test_owned_directories_reject_wrong_owner_then_clean_exact_children() -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, final = HARNESS.create_run_directories(_run_id("owner"), owner)
    try:
        assert not final.exists()
        with pytest.raises(HARNESS.AcceptanceFailure) as denied:
            HARNESS._remove_owned_directory(
                work,
                HARNESS.WORK_ROOT,
                "0" * 64,
                stage="cleanup.unit",
            )
        assert (denied.value.stage, denied.value.code) == ("cleanup.unit", "owner")
        assert work.exists()
    finally:
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_owned_directory_construction_rolls_back_after_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    run_id = _run_id("rollback")
    work = HARNESS.WORK_ROOT / run_id
    staging = HARNESS.ARTIFACT_ROOT / f".{run_id}.staging"
    original_mkdir = Path.mkdir

    def fail_on_logs(path: Path, *args: object, **kwargs: object) -> None:
        if path == work / "logs":
            raise OSError("injected")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_on_logs)
    with pytest.raises(OSError, match="injected"):
        HARNESS.create_run_directories(run_id, owner)
    assert not work.exists()
    assert not staging.exists()


def test_owned_directory_construction_rolls_back_when_staging_marker_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    run_id = _run_id("marker-rollback")
    work = HARNESS.WORK_ROOT / run_id
    staging = HARNESS.ARTIFACT_ROOT / f".{run_id}.staging"
    marker = staging / HARNESS.OWNER_FILENAME
    original = Path.write_text

    def fail_marker(path: Path, *args: object, **kwargs: object) -> int:
        if path == marker:
            raise OSError("injected")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_marker)
    with pytest.raises(OSError, match="injected"):
        HARNESS.create_run_directories(run_id, owner)
    assert not work.exists()
    assert not staging.exists()


def test_empty_database_round_trip_and_mutable_operator_revocation() -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, _final = HARNESS.create_run_directories(_run_id("migration"), owner)
    graph = None
    try:
        graph = HARNESS.prepare_database(work, HARNESS.RUNTIME.RuntimeSecrets.create())
        assert graph.migration["fresh"] == {
            "count": 75,
            "version": "0075",
            "integrity": "ok",
            "foreign_key_violations": 0,
        }
        assert graph.migration["repeat"] == graph.migration["fresh"]
        assert graph.migration["down"]["version"] == "0074"
        assert graph.migration["reup"] == graph.migration["fresh"]

        connection = connect_database(graph.database_url)
        try:
            role = connection.execute(
                "SELECT code,is_permission_immutable FROM roles WHERE role_id=?",
                (graph.revocable_role_id,),
            ).fetchone()
            assert tuple(role) == ("ops_admin", 0)
        finally:
            connection.close()

        candidate = object.__new__(HARNESS.BrowserHarness)
        candidate.graph = graph
        candidate.lock = threading.Lock()
        candidate.permissions_revoked = False
        candidate.revoke_mcp_read()

        connection = connect_database(graph.database_url)
        try:
            grants = dict(
                connection.execute(
                    """
                    SELECT role.code,COUNT(*) FROM role_permissions relation
                    JOIN roles role ON role.tenant_id=relation.tenant_id
                      AND role.role_id=relation.role_id
                    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND permission.code='mcp.read'
                      AND role.code IN('admin','ops_admin') GROUP BY role.code
                    """,
                    (graph.tenant_id,),
                ).fetchall()
            )
            assert grants == {"admin": 1}
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
    finally:
        if graph is not None:
            HARNESS.RUNTIME.quiesce_database(graph)
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_browser_fact_validator_is_exact_and_keeps_global_incomplete() -> None:
    facts = _complete_facts()
    assert HARNESS.validate_browser_facts(facts) is facts
    facts["all_108_prds_complete"] = True
    with pytest.raises(HARNESS.AcceptanceFailure):
        HARNESS.validate_browser_facts(facts)


def test_browser_source_snapshot_rejects_helper_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = HARNESS._source_provenance()
    assert "console_main" in snapshot
    changed = {**snapshot, "browser_helper_prd056": "0" * 64}
    monkeypatch.setattr(HARNESS, "_source_provenance", lambda: changed)
    with pytest.raises(HARNESS.AcceptanceFailure) as denied:
        HARNESS._assert_source_snapshot(snapshot)
    assert (denied.value.stage, denied.value.code) == ("evidence.source", "changed")


def test_browser_secrets_stay_out_of_repr_and_source_requests_no_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HARNESS.ADMIN_PASSWORD_ENV, "Admin-Aa1-" + "a" * 40)
    monkeypatch.setenv(HARNESS.ROLE_PASSWORD_ENV, "Role-Aa1-" + "r" * 40)
    monkeypatch.setenv(HARNESS.STOP_TOKEN_ENV, "Stop-" + "s" * 64)
    secrets_ = HARNESS.BrowserSecrets.from_environment()
    rendered = repr(secrets_)
    assert secrets_.runtime.admin_password not in rendered
    assert secrets_.runtime.role_password not in rendered
    assert secrets_.stop_token not in rendered
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert '"all_108_prds_complete": False' in source
    assert "sandbox_permissions" not in source
