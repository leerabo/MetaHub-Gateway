"""Audit every declared product-QA case against current executed evidence."""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "testing" / "product-qa" / "TEST-PLAN.md"
SCHEMA = "metahub.product-qa.case-audit.v1"
EXPECTED_COUNTS = {
    "WB": 8,
    "API": 8,
    "BB": 14,
    "GR": 9,
    "PERF": 10,
    "WEB": 8,
    "UI": 7,
    "UCD": 9,
    "COMP": 7,
    "SEC": 7,
    "FLOW": 10,
}
EXPECTED_APPLIED_MIGRATIONS = 122
EXPECTED_LATEST_MIGRATION = "0122"
SENSITIVE_JSON_KEYS = {
    "access_token",
    "authorization",
    "cookie",
    "database_path",
    "database_url",
    "password",
    "prompt",
    "provider_key",
    "request_body",
    "response",
    "response_body",
    "uploaded_filename",
    "virtual_key",
}
FORBIDDEN_EVIDENCE_SUFFIXES = {
    ".db",
    ".env",
    ".har",
    ".jpeg",
    ".jpg",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".png",
    ".sqlite",
    ".zip",
}


CASE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "WB-01": ("backend",),
    "WB-02": ("backend", "database"),
    "WB-03": ("backend",),
    "WB-04": ("backend",),
    "WB-05": ("backend",),
    "WB-06": ("backend", "performance", "database"),
    "WB-07": ("sensitive",),
    "WB-08": ("backend", "web"),
    "API-01": ("backend",),
    "API-02": ("backend",),
    "API-03": ("backend",),
    "API-04": ("backend",),
    "API-05": ("backend",),
    "API-06": ("backend", "web"),
    "API-07": ("recovery",),
    "API-08": ("backend", "web"),
    "BB-01": ("browser",),
    "BB-02": ("backend", "browser"),
    "BB-03": ("backend",),
    "BB-04": ("admin",),
    "BB-05": ("developer",),
    "BB-06": ("backend", "viewer"),
    "BB-07": ("browser",),
    "BB-08": ("web",),
    "BB-09": ("web",),
    "BB-10": ("web", "browser"),
    "BB-11": ("recovery",),
    "BB-12": ("recovery",),
    "BB-13": ("browser",),
    "BB-14": ("web", "sensitive"),
    "GR-01": ("backend", "web"),
    "GR-02": ("backend", "recovery"),
    "GR-03": ("backend",),
    "GR-04": ("browser", "web"),
    "GR-05": ("backend", "performance"),
    "GR-06": ("database", "browser"),
    "GR-07": ("recovery",),
    "GR-08": ("backend", "sensitive"),
    "GR-09": ("web", "sensitive"),
    "PERF-01": ("performance",),
    "PERF-02": ("browser_latency",),
    "PERF-03": ("live",),
    "PERF-04": ("live",),
    "PERF-05": ("live",),
    "PERF-06": ("pressure",),
    "PERF-07": ("live", "pressure"),
    "PERF-08": ("long",),
    "PERF-09": ("performance", "database"),
    "PERF-10": ("sensitive", "long"),
    "WEB-01": ("web", "browser"),
    "WEB-02": ("keyboard",),
    "WEB-03": ("browser",),
    "WEB-04": ("browser",),
    "WEB-05": ("browser",),
    "WEB-06": ("browser",),
    "WEB-07": ("browser",),
    "WEB-08": ("browser", "web"),
    "UI-01": ("web", "browser"),
    "UI-02": ("browser",),
    "UI-03": ("web", "sensitive"),
    "UI-04": ("web", "recovery"),
    "UI-05": ("browser",),
    "UI-06": ("browser",),
    "UI-07": ("backend", "recovery", "sensitive"),
    "UCD-01": ("admin",),
    "UCD-02": ("developer",),
    "UCD-03": ("backend", "viewer"),
    "UCD-04": ("recovery",),
    "UCD-05": ("web", "browser"),
    "UCD-06": ("browser",),
    "UCD-07": ("viewer",),
    "UCD-08": ("browser",),
    "UCD-09": ("backend", "recovery"),
    "COMP-01": ("chromium",),
    "COMP-02": ("edge",),
    "COMP-03": ("firefox",),
    "COMP-04": ("scale",),
    "COMP-05": ("locale",),
    "COMP-06": ("recovery",),
    "COMP-07": ("backend", "web"),
    "SEC-01": ("backend",),
    "SEC-02": ("backend",),
    "SEC-03": ("backend",),
    "SEC-04": ("backend", "web"),
    "SEC-05": ("backend", "web", "sensitive"),
    "SEC-06": ("sensitive",),
    "SEC-07": ("backend", "web"),
    "FLOW-01": ("admin",),
    "FLOW-02": ("admin",),
    "FLOW-03": ("developer",),
    "FLOW-04": ("developer",),
    "FLOW-05": ("viewer",),
    "FLOW-06": ("backend", "viewer"),
    "FLOW-07": ("browser",),
    "FLOW-08": ("recovery",),
    "FLOW-09": ("recovery",),
    "FLOW-10": ("browser",),
}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _junit_clean(path: Path, *, minimum_tests: int) -> bool:
    suite = ET.parse(path).getroot().find("testsuite")
    if suite is None:
        return False
    return (
        int(suite.attrib.get("tests", 0)) >= minimum_tests
        and int(suite.attrib.get("failures", 0)) == 0
        and int(suite.attrib.get("errors", 0)) == 0
        and int(suite.attrib.get("skipped", 0)) == 0
    )


def _sensitive_key_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(
            (1 if str(key).lower() in SENSITIVE_JSON_KEYS else 0) + _sensitive_key_count(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return sum(_sensitive_key_count(item) for item in value)
    return 0


def _plan_cases() -> dict[str, str]:
    rows = re.findall(
        r"^\| ((?:WB|API|BB|GR|PERF|WEB|UI|UCD|COMP|SEC|FLOW)-\d{2}) \| ([^|]+) \|",
        PLAN.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    return {case_id: title.strip() for case_id, title in rows}


def audit(evidence_dir: Path, long_stability_path: Path) -> dict[str, Any]:
    summary = _json(evidence_dir / "summary.json")
    live = _json(evidence_dir / "performance-live.json")
    pressure = _json(evidence_dir / "performance-pressure.json")
    long_stability = _json(long_stability_path)
    browser_reports = [
        _json(evidence_dir / f"browser-real-{browser}-scale-{scale}.json")
        for browser in ("chromium", "edge", "firefox")
        for scale in ("1", "1_25", "1_5")
    ]
    journeys = [journey for report in browser_reports for journey in report["browser"]["journeys"]]
    expected_pairs = {
        (browser, scale) for browser in ("chromium", "edge", "firefox") for scale in (1, 1.25, 1.5)
    }
    observed_pairs = {
        (report["browser"]["browser"], float(report["browser"]["scale"]))
        for report in browser_reports
    }
    browser_clean = (
        expected_pairs == observed_pairs
        and len(journeys) == 27
        and all(journey.get("passed") is True for journey in journeys)
        and all(journey.get("console_error_count") == 0 for journey in journeys)
        and all(journey.get("request_failure_count") == 0 for journey in journeys)
        and all(journey.get("horizontal_overflow_px") == 0 for journey in journeys)
        and all(journey.get("clipped_control_count") == 0 for journey in journeys)
        and all(journey.get("interactive_overlap_count") == 0 for journey in journeys)
    )
    database_clean = all(
        report["database"]
        == {
            "applied_migrations": EXPECTED_APPLIED_MIGRATIONS,
            "foreign_key_violations": 0,
            "integrity_check": "ok",
            "latest_migration": EXPECTED_LATEST_MIGRATION,
        }
        for report in browser_reports
    )
    web_output = (evidence_dir / "web.out").read_text(encoding="utf-8", errors="replace")
    web_clean = (
        "pass 16" in web_output
        and "fail 0" in web_output
        and "skipped 0" in web_output
        and "todo 0" in web_output
    )
    json_paths = [*evidence_dir.glob("*.json"), long_stability_path]
    sensitive_clean = all(
        _sensitive_key_count(_json(path)) == 0 for path in json_paths
    ) and not any(
        path.suffix.lower() in FORBIDDEN_EVIDENCE_SUFFIXES
        for path in evidence_dir.rglob("*")
        if path.is_file()
    )
    long_clean = (
        long_stability.get("status") == "passed"
        and long_stability.get("errors") == 0
        and long_stability.get("observed_seconds", 0) >= 14000
        and long_stability.get("max_observation_gap_seconds", 999) <= 10
        and long_stability.get("sleep_prevention_active") is True
    )
    gate_clean = (
        summary.get("all_108_prds_complete") is False
        and len(summary.get("results", [])) == 15
        and all(item.get("exit_code") == 0 for item in summary.get("results", []))
    )
    facts = {
        "gate": gate_clean,
        "backend": _junit_clean(evidence_dir / "backend-junit.xml", minimum_tests=8),
        "performance": _junit_clean(evidence_dir / "performance-junit.xml", minimum_tests=8),
        "web": web_clean
        and (evidence_dir / "console-lint.out").read_text().startswith("console lint ok"),
        "browser": browser_clean,
        "database": database_clean,
        "admin": browser_clean and sum(j["role"] == "admin" for j in journeys) == 9,
        "developer": browser_clean and sum(j["role"] == "developer" for j in journeys) == 9,
        "viewer": browser_clean and sum(j["role"] == "viewer" for j in journeys) == 9,
        "keyboard": browser_clean
        and all(j.get("keyboard_navigation_passed") is True for j in journeys),
        "recovery": browser_clean
        and web_clean
        and any(
            j.get("expected_fault_console_error_count") == 1
            for j in journeys
            if j["role"] == "admin"
        ),
        "browser_latency": browser_clean and max(j["duration_ms"] for j in journeys) < 60000,
        "live": live.get("requests") == 30
        and live.get("successes") == 30
        and live.get("errors") == 0,
        "pressure": pressure.get("requests") == 120
        and pressure.get("successes") == 120
        and pressure.get("errors") == 0,
        "long": long_clean,
        "chromium": browser_clean
        and sum(r["browser"]["browser"] == "chromium" for r in browser_reports) == 3,
        "edge": browser_clean
        and sum(r["browser"]["browser"] == "edge" for r in browser_reports) == 3,
        "firefox": browser_clean
        and sum(r["browser"]["browser"] == "firefox" for r in browser_reports) == 3,
        "scale": browser_clean and observed_pairs == expected_pairs,
        "locale": browser_clean and {j.get("ui_locale") for j in journeys} == {"en-US", "zh-CN"},
        "sensitive": sensitive_clean,
    }
    plan_cases = _plan_cases()
    expected_cases = {
        f"{group}-{index:02d}"
        for group, count in EXPECTED_COUNTS.items()
        for index in range(1, count + 1)
    }
    if set(plan_cases) != expected_cases or set(CASE_REQUIREMENTS) != expected_cases:
        raise ValueError("product QA case mapping does not match the 97-case plan")
    cases = []
    for case_id in sorted(expected_cases):
        required = CASE_REQUIREMENTS[case_id]
        passed = all(facts.get(key) is True for key in required)
        cases.append(
            {
                "case_id": case_id,
                "title": plan_cases[case_id],
                "status": "passed" if passed else "failed",
                "evidence_checks": list(required),
            }
        )
    all_passed = gate_clean and all(case["status"] == "passed" for case in cases)
    return {
        "schema": SCHEMA,
        "status": "passed" if all_passed else "failed",
        "all_108_prds_complete": False,
        "case_count": len(cases),
        "passed": sum(case["status"] == "passed" for case in cases),
        "failed": sum(case["status"] == "failed" for case in cases),
        "gate_task_count": len(summary.get("results", [])),
        "gate_failed_tasks": sum(item.get("exit_code") != 0 for item in summary.get("results", [])),
        "facts": facts,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--long-stability", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(args.evidence_dir.resolve(), args.long_stability.resolve())
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, ET.ParseError) as exc:
        report = {
            "schema": SCHEMA,
            "status": "failed",
            "all_108_prds_complete": False,
            "reason": type(exc).__name__,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "case_count": report.get("case_count", 0),
                "passed": report.get("passed", 0),
                "failed": report.get("failed", 0),
            },
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
