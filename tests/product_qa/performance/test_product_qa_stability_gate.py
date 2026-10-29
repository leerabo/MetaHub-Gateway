"""Unit contract for the configuration-driven stability gate."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.quality.product_qa_stability_gate import run_gate


def test_long_stability_is_explicitly_not_run_without_opt_in() -> None:
    report = run_gate(profile_name="long_stability")
    assert report["schema"] == "metahub.product-qa.stability-report.v1"
    assert report["status"] == "not_run"
    assert report["configured_duration_seconds"] == 14400
    assert report["requests"] == 0
    assert "4-hour" in report["reason"]


def test_stability_config_has_short_and_long_profiles_without_credentials() -> None:
    config_path = Path(__file__).parents[3] / "scripts" / "quality" / "product_qa_stability.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(config["profiles"]) == {"short_stability", "long_stability"}
    assert config["profiles"]["short_stability"]["duration_seconds"] < 60
    assert config["profiles"]["long_stability"]["duration_seconds"] == 14400
    assert config["profiles"]["long_stability"]["request_interval_seconds"] == 0.5
    assert config["profiles"]["long_stability"]["observation_bucket_seconds"] == 10
    assert config["profiles"]["long_stability"]["requests"] >= 100_000
    assert config["profiles"]["long_stability"]["maximum_observation_gap_seconds"] <= 10
    assert all("@" not in target and "?" not in target for target in config["targets"])


def test_not_run_report_contains_only_redacted_gate_facts() -> None:
    report = run_gate(profile_name="long_stability")
    serialized = json.dumps(report, ensure_ascii=True)
    assert "127.0.0.1" not in serialized
    assert "http://" not in serialized
    assert "authorization" not in serialized.lower()
    assert "response" not in serialized.lower()
