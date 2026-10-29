"""Ownership, 0077 migration, RBAC, and evidence contracts for PAGE-116."""

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
MODULE_PATH = ROOT / "scripts" / "acceptance" / "prd084-browser-harness.py"


def _load_harness():
    name = "_metahub_prd084_browser_harness_tests"
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
    desktop.update({"width": 1280, "height": 720, "overlap_count": 0, "endpoint_rows": 2})
    mobile = {name: True for name in HARNESS.MOBILE_FACT_KEYS}
    mobile.update({"width": 390, "height": 844, "overlap_count": 0, "endpoint_rows": 2})
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
            "login_statuses": [200, 200, 200],
            "logout_statuses": [200, 200, 200],
            "create_statuses": [201, 201],
            "test_statuses": [200, 200, 200],
            "list_successes": 3,
            "no_store_responses": True,
            "request_contracts_exact": True,
        },
        "screenshots": list(HARNESS.SCREENSHOTS),
        "all_108_prds_complete": False,
    }


def test_repository_boundaries_and_artifact_whitelist_are_frozen() -> None:
    assert HARNESS.ROOT == ROOT
    assert HARNESS.WORK_ROOT == ROOT / ".tmp" / "prd084-browser-harness"
    assert HARNESS.ARTIFACT_ROOT == ROOT / ".artifacts" / "prd084-browser"
    assert HARNESS.HARNESS_PORT == 18887
    assert HARNESS.SUCCESS_STUB_PORT == 18884
    assert HARNESS.FAILURE_STUB_PORT == 18885
    assert HARNESS.EXPECTED_MIGRATION_COUNT == 77
    assert HARNESS.EXPECTED_MIGRATION_VERSION == "0077"
    assert HARNESS.PREVIOUS_MIGRATION_VERSION == "0076"
    assert HARNESS.ALLOWED_ARTIFACTS == frozenset(
        (*HARNESS.SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json")
    )
    assert all(path.resolve().is_relative_to(ROOT) for path in HARNESS.SOURCE_FILES.values())
    assert {"migration_up", "migration_down", "browser_base_prd083"} <= set(HARNESS.SOURCE_FILES)


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


def test_empty_database_round_trip_seeds_page_116_tenants_and_roles() -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, _final = HARNESS.create_run_directories(_run_id("migration"), owner)
    graph = None
    try:
        graph = HARNESS.prepare_database(work, HARNESS.RUNTIME.RuntimeSecrets.create())
        assert graph.migration == {
            "fresh": {
                "count": 77,
                "version": "0077",
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
            "repeat": {
                "count": 77,
                "version": "0077",
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
            "down": {
                "count": 76,
                "version": "0076",
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
            "reup": {
                "count": 77,
                "version": "0077",
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
        }
        connection = connect_database(graph.database_url)
        try:
            assert HARNESS._endpoint_summary_rows(connection) == []
            assert HARNESS._provider_audit_rows(connection, graph.tenant_id) == []
            grants = dict(
                connection.execute(
                    """
                    SELECT permission.code,COUNT(*) FROM role_permissions relation
                    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND relation.role_id=?
                      AND permission.code LIKE 'local_endpoint.%' GROUP BY permission.code
                    """,
                    (graph.tenant_id, graph.developer_role_id),
                ).fetchall()
            )
            assert grants == {"local_endpoint.read": 1, "local_endpoint.test": 1}
            tenants = dict(
                connection.execute(
                    "SELECT tenants.slug,COUNT(*) FROM users "
                    "JOIN tenants ON tenants.tenant_id=users.tenant_id "
                    "WHERE users.email LIKE '%prd084%' GROUP BY tenants.slug"
                ).fetchall()
            )
            assert tenants == {"default": 1, "prd084-secondary": 1}
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
    finally:
        if graph is not None:
            HARNESS.RUNTIME.quiesce_database(graph)
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_permission_drift_removes_only_developer_local_read() -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, _final = HARNESS.create_run_directories(_run_id("revoke"), owner)
    graph = None
    try:
        graph = HARNESS.prepare_database(work, HARNESS.RUNTIME.RuntimeSecrets.create())
        candidate = object.__new__(HARNESS.BrowserHarness)
        candidate.graph = graph
        candidate.lock = threading.Lock()
        candidate.permissions_revoked = False
        candidate.revoke_local_read()
        connection = connect_database(graph.database_url)
        try:
            retained = tuple(
                row[0]
                for row in connection.execute(
                    """
                    SELECT permission.code FROM role_permissions relation
                    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND relation.role_id=?
                      AND permission.code LIKE 'local_endpoint.%' ORDER BY permission.code
                    """,
                    (graph.tenant_id, graph.developer_role_id),
                ).fetchall()
            )
            assert retained == ("local_endpoint.test",)
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


def test_privacy_scopes_allow_endpoint_urls_only_in_work_database(tmp_path: Path) -> None:
    candidate = object.__new__(HARNESS.BrowserHarness)
    candidate.secrets = HARNESS.BrowserSecrets(
        runtime=HARNESS.RUNTIME.RuntimeSecrets.create(),
        stop_token="Stop-" + "s" * 64,
    )
    bearer = "Bearer-" + "b" * 48
    success_endpoint = "http://127.0.0.1:18884/v1"
    failure_endpoint = "http://127.0.0.1:18885/v1"
    candidate.private_extra_labels = (
        "driver_owner",
        "bearer_credential",
        "success_endpoint",
        "failure_endpoint",
    )
    candidate.private_extra = (
        "Owner-" + "o" * 48,
        bearer,
        success_endpoint,
        failure_endpoint,
    )

    work_values = candidate._private_values_for_scope("work")
    staging_values = candidate._private_values_for_scope("staging")
    assert bearer in work_values
    assert success_endpoint not in work_values and failure_endpoint not in work_values
    assert success_endpoint in staging_values and failure_endpoint in staging_values

    database = tmp_path / "local-endpoints.sqlite3"
    database.write_text(f"{success_endpoint}\n{failure_endpoint}\n", encoding="utf-8")
    candidate._diagnose_private_storage(tmp_path, "work")
    with pytest.raises(HARNESS.AcceptanceFailure) as staged_endpoint:
        candidate._diagnose_private_storage(tmp_path, "staging")
    assert (staged_endpoint.value.stage, staged_endpoint.value.code) == (
        "privacy.diagnostic",
        "staging.database.success_endpoint",
    )

    database.write_text(f"{success_endpoint}\n{bearer}\n", encoding="utf-8")
    with pytest.raises(HARNESS.AcceptanceFailure) as database_secret:
        candidate._diagnose_private_storage(tmp_path, "work")
    assert (database_secret.value.stage, database_secret.value.code) == (
        "privacy.diagnostic",
        "work.database.bearer_credential",
    )

    database.write_text(success_endpoint, encoding="utf-8")
    log = tmp_path / "control.log"
    log.write_text(success_endpoint, encoding="utf-8")
    with pytest.raises(HARNESS.AcceptanceFailure) as logged_endpoint:
        candidate._diagnose_private_storage(tmp_path, "work")
    assert (logged_endpoint.value.stage, logged_endpoint.value.code) == (
        "privacy.diagnostic",
        "work.log.success_endpoint",
    )
