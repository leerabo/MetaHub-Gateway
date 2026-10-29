"""PRD-087 PAGE-119 浏览器 harness 合约测试：验证进程边界、固定事实和敏感信息清理。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "acceptance" / "prd087-browser-harness.py"


def _load_harness():
    name = "_metahub_prd087_browser_harness_contract"
    specification = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


HARNESS = _load_harness()


def _layout(width: int, height: int, *, setup: bool = False) -> dict[str, object]:
    facts: dict[str, object] = {
        "width": width,
        "height": height,
        "nonblank": True,
        "page_119": True,
        "horizontal_overflow_free": True,
        "content_bounded": True,
        "text_overflow_free": True,
        "overlap_count": 0,
        "no_sensitive_dom": True,
    }
    if setup:
        facts.update({"setup_visible": True, "tenant_isolation_visible": True})
    else:
        facts.update({"ready_visible": True, "operations_visible": True})
    return facts


def _complete_facts() -> dict[str, object]:
    lifecycle = {name: True for name in HARNESS.LIFECYCLE_FACT_KEYS}
    return {
        "status": "passed",
        "desktop": _layout(1280, 720),
        "mobile": _layout(390, 844, setup=True),
        "lifecycle": lifecycle,
        "network": {
            "external_requests": 0,
            "forbidden_queries": 0,
            "console_errors": 0,
            "page_errors": 0,
            "login_statuses": [200, 200, 200, 200],
            "logout_statuses": [200, 200, 200, 200],
            "partner_request_count": 16,
            "partner_statuses": [200] * 14 + [201, 404],
            "no_store_responses": True,
            "request_contract_exact": True,
            "required_routes_observed": True,
            "no_response_body_capture": True,
        },
        "screenshots": list(HARNESS.SCREENSHOTS),
        "all_108_prds_complete": False,
    }


def test_page_119_constants_freeze_migration_ports_and_artifact_whitelist() -> None:
    assert HARNESS.EXPECTED_MIGRATION_COUNT == 80
    assert HARNESS.EXPECTED_MIGRATION_VERSION == "0080"
    assert HARNESS.PREVIOUS_MIGRATION_VERSION == "0079"
    assert HARNESS.HARNESS_PORT == 18889
    assert HARNESS.SCREENSHOTS == (
        "page-119-partner-setup-mobile-390x844.png",
        "page-119-partner-ready-desktop-1280x720.png",
        "page-119-partner-commission-wide-1440x900.png",
        "page-119-partner-tenant-isolation-mobile-390x844.png",
    )
    assert HARNESS.ALLOWED_ARTIFACTS == frozenset(
        (*HARNESS.SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json")
    )
    assert HARNESS.WORK_ROOT.is_relative_to(ROOT)
    assert HARNESS.ARTIFACT_ROOT.is_relative_to(ROOT)


def test_browser_fact_schema_requires_every_identity_and_privacy_boundary() -> None:
    facts = _complete_facts()
    assert HARNESS.validate_browser_facts(facts) is facts

    for section, field in (
        ("lifecycle", "unauthorized_api_prefetch_absent"),
        ("lifecycle", "dynamic_revocation_branding_cleared"),
        ("lifecycle", "late_response_discarded"),
        ("network", "no_response_body_capture"),
    ):
        changed = json.loads(json.dumps(facts))
        changed[section][field] = False
        with pytest.raises(HARNESS.AcceptanceFailure):
            HARNESS.validate_browser_facts(changed)


def test_browser_facts_reject_completion_flag_status_drift_and_extra_fields() -> None:
    facts = _complete_facts()
    facts["all_108_prds_complete"] = True
    with pytest.raises(HARNESS.AcceptanceFailure):
        HARNESS.validate_browser_facts(facts)

    facts = _complete_facts()
    facts["network"]["login_statuses"] = [200]
    with pytest.raises(HARNESS.AcceptanceFailure):
        HARNESS.validate_browser_facts(facts)

    facts = _complete_facts()
    facts["desktop"]["unexpected"] = True
    with pytest.raises(HARNESS.AcceptanceFailure):
        HARNESS.validate_browser_facts(facts)


def test_migration_state_contract_is_exact() -> None:
    assert HARNESS._expected_state(80, "0080") == {
        "count": 80,
        "version": "0080",
        "integrity": "ok",
        "foreign_key_violations": 0,
    }
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "len(fresh.applied) == EXPECTED_MIGRATION_COUNT" in source
    assert "rollback_last_migration" in source
    assert "fresh_state == _expected_state(80, \"0080\")" in source
    assert "down_state == _expected_state(79, \"0079\")" in source
    assert "PRAGMA integrity_check" in source
    assert "PRAGMA foreign_key_check" in source


def test_source_snapshot_and_runtime_secrets_are_private(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_privacy_diagnostic_catches_registered_browser_session() -> None:
    candidate = object.__new__(HARNESS.BrowserHarness)
    candidate.secrets = HARNESS.BrowserSecrets(
        runtime=HARNESS.RUNTIME.RuntimeSecrets.create(),
        stop_token="Stop-" + "s" * 64,
    )
    private_session = "Session-" + "b" * 48
    candidate.private_extra_labels = ("browser_session",)
    candidate.private_extra = (private_session,)
    root = HARNESS.WORK_ROOT / f"unit-privacy-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    evidence = root / "evidence.json"
    try:
        evidence.write_text(private_session, encoding="utf-8")
        with pytest.raises(HARNESS.AcceptanceFailure) as exposed:
            candidate._diagnose_private_storage(root, "staging")
        assert (exposed.value.stage, exposed.value.code) == (
            "privacy.diagnostic",
            "staging.json.browser_session",
        )
    finally:
        if evidence.exists():
            evidence.unlink()
        root.rmdir()


def test_source_prohibits_escalation_and_keeps_total_incomplete() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert '"all_108_prds_complete": False' in source
    assert "sandbox_permissions" not in source
    assert "PRD-088" not in source
    assert "/revoke-partner-portal-read" in source
    assert "/restore-partner-portal-read" in source
    assert "partner_portal.read" in source
    assert hashlib.sha256(source.encode()).hexdigest()
    assert uuid.uuid4().hex
