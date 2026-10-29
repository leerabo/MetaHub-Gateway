"""Ownership, 0076 migration, RBAC, and evidence contracts for PAGE-115."""

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
MODULE_PATH = ROOT / "scripts" / "acceptance" / "prd083-browser-harness.py"


def _load_harness():
    name = "_metahub_prd083_browser_harness_tests"
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
            "login_statuses": [200] * 6,
            "logout_statuses": [200] * 6,
            "policy_statuses": [200, 200, 409, 200],
            "list_successes": 8,
            "no_store_responses": True,
            "request_contracts_exact": True,
        },
        "screenshots": list(HARNESS.SCREENSHOTS),
        "all_108_prds_complete": False,
    }


def test_repository_boundaries_and_artifact_whitelist_are_frozen() -> None:
    assert HARNESS.ROOT == ROOT
    assert HARNESS.WORK_ROOT == ROOT / ".tmp" / "prd083-browser-harness"
    assert HARNESS.ARTIFACT_ROOT == ROOT / ".artifacts" / "prd083-browser"
    assert HARNESS.HARNESS_PORT == 18886
    assert HARNESS.RUNTIME.CONTROL_PORT == 8001
    assert HARNESS.RUNTIME.CONSOLE_PORT == 5173
    assert HARNESS.EXPECTED_MIGRATION_COUNT == 76
    assert HARNESS.EXPECTED_MIGRATION_VERSION == "0076"
    assert HARNESS.PREVIOUS_MIGRATION_VERSION == "0075"
    assert 1 <= HARNESS.CONFLICT_BUMP_CALL_TIMEOUT_SECONDS <= 30
    assert HARNESS.CONFLICT_BUMP_CALL_TIMEOUT_SECONDS not in {22, 24}
    assert HARNESS.ALLOWED_ARTIFACTS == frozenset(
        (*HARNESS.SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json")
    )
    assert all(path.resolve().is_relative_to(ROOT) for path in HARNESS.SOURCE_FILES.values())
    assert {"migration_up", "migration_down", "browser_base_prd082"} <= set(HARNESS.SOURCE_FILES)


def test_owned_directories_reject_wrong_owner_and_clean_exact_children() -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, final = HARNESS.create_run_directories(_run_id("owner"), owner)
    try:
        assert not final.exists()
        with pytest.raises(HARNESS.AcceptanceFailure) as denied:
            HARNESS._remove_owned_directory(work, HARNESS.WORK_ROOT, "0" * 64, stage="cleanup.unit")
        assert (denied.value.stage, denied.value.code) == ("cleanup.unit", "owner")
        assert work.exists()
    finally:
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_empty_database_round_trip_seeds_page_115_roles_and_policy() -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, _final = HARNESS.create_run_directories(_run_id("migration"), owner)
    graph = None
    try:
        graph = HARNESS.prepare_database(work, HARNESS.RUNTIME.RuntimeSecrets.create())
        assert graph.migration == {
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
        }
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
                    WHERE relation.tenant_id=? AND permission.code='mcp.network.manage'
                      AND role.code IN('admin','security_admin','ops_admin','developer','viewer')
                    GROUP BY role.code
                    """,
                    (graph.tenant_id,),
                ).fetchall()
            )
            assert grants == {"admin": 1, "ops_admin": 1, "security_admin": 1}
            users = dict(
                connection.execute(
                    "SELECT role,COUNT(*) FROM users WHERE tenant_id=? AND email LIKE '%prd083%' "
                    "GROUP BY role",
                    (graph.tenant_id,),
                ).fetchall()
            )
            assert users == {
                "developer": 1,
                "ops_admin": 1,
                "security_admin": 1,
                "viewer": 1,
            }
            policy = connection.execute(
                "SELECT current_revision,status FROM mcp_network_policies "
                "WHERE tenant_id=? AND mcp_server_id=?",
                (graph.tenant_id, graph.server_id),
            ).fetchone()
            assert tuple(policy) == (1, "disabled")
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
    finally:
        if graph is not None:
            HARNESS.RUNTIME.quiesce_database(graph)
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_permission_drift_removes_only_ops_mcp_read() -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, _final = HARNESS.create_run_directories(_run_id("revoke"), owner)
    graph = None
    try:
        graph = HARNESS.prepare_database(work, HARNESS.RUNTIME.RuntimeSecrets.create())
        candidate = object.__new__(HARNESS.BrowserHarness)
        candidate.graph = graph
        candidate.lock = threading.Lock()
        candidate.permissions_revoked = False
        candidate.revoke_mcp_read()
        connection = connect_database(graph.database_url)
        try:
            result = dict(
                connection.execute(
                    """
                    SELECT role.code,COUNT(*) FROM role_permissions relation
                    JOIN roles role ON role.tenant_id=relation.tenant_id
                      AND role.role_id=relation.role_id
                    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND permission.code='mcp.read'
                      AND role.code IN('admin','security_admin','ops_admin','developer')
                    GROUP BY role.code
                    """,
                    (graph.tenant_id,),
                ).fetchall()
            )
            assert result == {"admin": 1, "developer": 1, "security_admin": 1}
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


def test_source_snapshot_and_secrets_are_private(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = HARNESS._source_provenance()
    changed = {**snapshot, "migration_up": "0" * 64}
    monkeypatch.setattr(HARNESS, "_source_provenance", lambda: changed)
    with pytest.raises(HARNESS.AcceptanceFailure):
        HARNESS._assert_source_snapshot(snapshot)

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
