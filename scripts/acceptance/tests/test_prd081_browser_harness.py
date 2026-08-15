"""Ownership, migration, privacy, and evidence contracts for PAGE-113."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "acceptance" / "prd081-browser-harness.py"


def _load_harness():
    name = "_metahub_prd081_browser_harness_tests"
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
        "interaction": {name: True for name in HARNESS.INTERACTION_FACT_KEYS},
        "permissions": {name: True for name in HARNESS.PERMISSION_FACT_KEYS},
        "network": {
            "external_requests": 0,
            "forbidden_queries": 0,
            "console_errors": 0,
            "page_errors": 0,
            "login_statuses": [200, 200],
            "logout_statuses": [200, 200],
            "create_statuses": [200, 200],
            "test_statuses": [200, 200],
            "list_successes": 3,
            "delivery_list_successes": 2,
            "create_contracts_exact": True,
        },
        "screenshots": list(HARNESS.SCREENSHOTS),
        "all_108_prds_complete": False,
    }


def test_repository_boundaries_and_artifact_whitelist_are_frozen() -> None:
    assert HARNESS.ROOT == ROOT
    assert HARNESS.WORK_ROOT == ROOT / ".tmp" / "prd081-browser-harness"
    assert HARNESS.ARTIFACT_ROOT == ROOT / ".artifacts" / "prd081-browser"
    assert HARNESS.HARNESS_PORT == 18884
    assert HARNESS.BROWSER_RUNTIME.CONTROL_PORT == 8001
    assert HARNESS.BROWSER_RUNTIME.CONSOLE_PORT == 5173
    assert HARNESS.EXPECTED_MIGRATION_COUNT == 74
    assert HARNESS.EXPECTED_MIGRATION_VERSION == "0074"
    assert HARNESS.ALLOWED_ARTIFACTS == frozenset(
        (*HARNESS.SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json")
    )
    assert all(path.resolve().is_relative_to(ROOT) for path in HARNESS.SOURCE_FILES.values())


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


def test_empty_database_round_trip_reaches_0074() -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, _final = HARNESS.create_run_directories(_run_id("migration"), owner)
    runtime = HARNESS.RUNTIME.RuntimeSecrets.create()
    graph = None
    try:
        graph = HARNESS.RUNTIME._prepare_database(work, runtime, "http://127.0.0.1:1")
        assert graph.migration["fresh"] == {
            "count": 74,
            "version": "0074",
            "integrity": "ok",
            "foreign_key_violations": 0,
        }
        assert graph.migration["repeat"] == graph.migration["fresh"]
        assert graph.migration["down"]["version"] == "0073"
        assert graph.migration["reup"] == graph.migration["fresh"]
        assert runtime.dlp_marker.encode("utf-8") in graph.database.read_bytes()
        HARNESS._retire_private_database_values(graph)
        assert runtime.dlp_marker.encode("utf-8") not in graph.database.read_bytes()
    finally:
        if graph is not None:
            HARNESS.RUNTIME._quiesce_database(graph)
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_browser_fact_validator_is_exact_and_keeps_global_incomplete() -> None:
    facts = _complete_facts()
    assert HARNESS.validate_browser_facts(facts) is facts
    facts["all_108_prds_complete"] = True
    with pytest.raises(HARNESS.AcceptanceFailure):
        HARNESS.validate_browser_facts(facts)


def test_foreign_helper_failures_are_mapped_without_exception_text() -> None:
    class ForeignFailure(Exception):
        stage = "cleanup.process_tree"
        code = "exit"

    with pytest.raises(HARNESS.AcceptanceFailure) as mapped:
        HARNESS._stage_call("cleanup.processes", lambda: (_ for _ in ()).throw(ForeignFailure()))
    assert (mapped.value.stage, mapped.value.code) == ("cleanup.process_tree", "exit")

    with pytest.raises(HARNESS.AcceptanceFailure) as fixed:
        HARNESS._stage_call("evidence.publish", lambda: (_ for _ in ()).throw(OSError("private")))
    assert (fixed.value.stage, fixed.value.code) == ("evidence.publish", "failed")


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
