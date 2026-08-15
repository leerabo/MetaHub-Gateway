"""Static safety contracts for the PRD-081 real-process acceptance runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "acceptance" / "prd081-runtime-acceptance.py"


def _load():
    name = "_metahub_prd081_runtime_contract"
    specification = importlib.util.spec_from_file_location(name, SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def test_empty_evidence_is_explicitly_incomplete_and_exact() -> None:
    module = _load()
    payload = module.empty_evidence_payload()
    assert set(payload) == module.EVIDENCE_TOP_LEVEL_KEYS
    assert payload["prd_id"] == "PRD-081"
    assert payload["status"] == "passed"
    assert payload["all_108_prds_complete"] is False


def test_evidence_rejects_private_values_and_forbidden_keys() -> None:
    module = _load()
    private = "Prd081-Private-Value-For-Contract"
    with pytest.raises(module.AcceptanceFailure):
        module.assert_safe_evidence({"safe": private}, (private,))
    with pytest.raises(module.AcceptanceFailure):
        module.assert_safe_evidence({"provider_key": "redacted"}, ())


def test_runner_freezes_loopback_migration_and_source_contracts() -> None:
    module = _load()
    source = SCRIPT.read_text(encoding="utf-8")
    assert module.FIXED_PORTS == (18910, 18911, 18912, 18913, 18914)
    assert module.EXPECTED_MIGRATION_COUNT == 74
    assert module.EXPECTED_MIGRATION_VERSION == "0074"
    assert '"all_108_prds_complete": False' in source
    assert "security.dlp_matched" in source
    assert "security.session_high_risk" in source
    assert "security.provider_key_anomaly" in source
    assert "charge_ledger" not in source
    assert "FROM finops_ledger_entries" in source
    assert "entry_type='usage_charge'" in source
    assert "def terminal_ready" in source
    assert '_wait_database(graph, terminal_ready, "database.terminal")' in source
    for stage in (
        "database.final_query",
        "database.marker_retire",
        "cleanup.quiesce",
        "privacy.scan",
        "cleanup.work",
        "evidence.publish",
    ):
        assert f'"{stage}"' in source
    assert "sandbox_permissions" not in source
