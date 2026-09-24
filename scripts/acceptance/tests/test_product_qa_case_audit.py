import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.quality import product_qa_case_audit as auditor
from scripts.quality import product_qa_evidence as evidence
from scripts.quality.product_qa_case_audit import (
    EXPECTED_APPLIED_MIGRATIONS,
    EXPECTED_LATEST_MIGRATION,
    EXPECTED_PRESSURE_REQUESTS,
    _node_test_output_clean,
)

ROOT = Path(__file__).resolve().parents[3]


def test_node_test_output_accepts_growth_above_minimum() -> None:
    output = "\n".join(("tests 17", "pass 17", "fail 0", "skipped 0", "todo 0"))

    assert _node_test_output_clean(output, minimum_tests=16) is True


def test_node_test_output_rejects_incomplete_or_failed_runs() -> None:
    assert (
        _node_test_output_clean(
            "\n".join(("pass 15", "fail 0", "skipped 0", "todo 0")),
            minimum_tests=16,
        )
        is False
    )
    assert (
        _node_test_output_clean(
            "\n".join(("pass 17", "fail 1", "skipped 0", "todo 0")),
            minimum_tests=16,
        )
        is False
    )


def test_case_audit_tracks_the_current_migration_and_pressure_gate() -> None:
    migrations = sorted(
        path.stem.split("_", 1)[0]
        for path in (ROOT / "infra" / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")
        if not path.name.endswith(".down.sql")
    )
    runner = (ROOT / "scripts" / "quality" / "run-product-qa.ps1").read_text(
        encoding="utf-8"
    )

    assert len(migrations) == EXPECTED_APPLIED_MIGRATIONS
    assert migrations[-1] == EXPECTED_LATEST_MIGRATION
    assert f"[int]$PressureRequests = {EXPECTED_PRESSURE_REQUESTS}" in runner


SOURCE = {"source_revision": "a" * 40, "source_sha256": "b" * 64, "source_file_count": 3}


@pytest.fixture(scope="module")
def simulated_stability(tmp_path_factory):
    """Synthetic report for validator behavior, never elapsed-time evidence."""
    directory = tmp_path_factory.mktemp("simulated-stability")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    count = 4321
    previous = "0" * 64
    targets = [
        {"target": name, "requests": 100, "errors": 0}
        for name in ("console", "control", "gateway")
    ]
    window = {
        "wall_seconds": 60.0, "first_observation_offset_seconds": 0.1,
        "last_observation_offset_seconds": 59.9, "max_observation_gap_seconds": 0.6,
        "requests": 300, "errors": 0, "targets": targets,
    }
    with (directory / "windows.jsonl").open("w", encoding="utf-8") as stream:
        for number in range(1, count + 1):
            record = {
                "sequence": number, "previous_sha256": previous, "window": window,
                "completed_at_utc": (start + timedelta(seconds=number * 60)).isoformat(),
            }
            line = json.dumps(record, sort_keys=True, separators=(",", ":"))
            previous = hashlib.sha256(line.encode()).hexdigest()
            stream.write(line + "\n")
    report = {
        "schema": "metahub.product-qa.continuous-availability.v1", "status": "passed",
        "diagnostic": False, "ga_approval": False, "source_revision": SOURCE["source_revision"],
        "source_start": SOURCE, "source_finish": SOURCE,
        "observed_seconds": 259200.0, "required_observed_seconds": 259200.0,
        "maximum_observation_gap_seconds": 1.0, "sleep_prevention_active": True,
        "started_at_utc": start.isoformat(),
        "finished_at_utc": (start + timedelta(seconds=count * 60 + 1)).isoformat(),
        "requests": count * 300, "errors": 0, "completed_windows": count,
        "journal_sha256": previous,
        "targets": {t["target"]: {"requests": count * 100, "errors": 0} for t in targets},
    }
    path = directory / "status.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path, report


def test_continuous_report_schema_and_chain_are_accepted(simulated_stability):
    path, report = simulated_stability
    assert auditor._long_stability_clean(report, path, SOURCE)
    assert report["ga_approval"] is False


@pytest.mark.parametrize("field,value", [
    ("status", "running"), ("status", "failed"), ("status", "diagnostic_passed"),
    ("diagnostic", True), ("ga_approval", True), ("source_revision", "c" * 40),
    ("schema", "metahub.product-qa.stability-report.v1"),
    ("source_start", None), ("source_finish", None),
    ("source_start", {**SOURCE, "source_sha256": "c" * 64}),
    ("source_finish", {**SOURCE, "source_sha256": "c" * 64}),
    ("observed_seconds", 14400), ("observed_seconds", 259199.99),
    ("required_observed_seconds", 14400), ("required_observed_seconds", 300000),
    ("observed_seconds", float("nan")), ("observed_seconds", float("inf")),
    ("maximum_observation_gap_seconds", 10.1), ("maximum_observation_gap_seconds", -1),
    ("sleep_prevention_active", False), ("errors", 1), ("requests", 1),
    ("completed_windows", 1), ("journal_sha256", "f" * 64), ("targets", {}),
    ("finished_at_utc", "2026-09-01T00:01:00+00:00"),
    ("finished_at_utc", "2026-09-04T00:01:00"),
])
def test_short_stale_incomplete_and_inconsistent_stability_is_rejected(
    simulated_stability, field, value,
):
    path, report = simulated_stability
    assert not auditor._long_stability_clean(
        {**report, field: value}, path, SOURCE,
    )


def test_missing_truncated_and_changed_journal_is_rejected(simulated_stability, tmp_path):
    path, report = simulated_stability
    copied = tmp_path / "status.json"
    assert not auditor._long_stability_clean(report, copied, SOURCE)
    lines = path.with_name("windows.jsonl").read_text(encoding="utf-8").splitlines()
    journal = copied.with_name("windows.jsonl")
    journal.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    assert not auditor._long_stability_clean(report, copied, SOURCE)
    lines[0] = lines[0].replace('"requests":300', '"requests":301')
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not auditor._long_stability_clean(report, copied, SOURCE)


def test_same_commit_with_changed_worktree_cannot_reuse_long_run(simulated_stability):
    path, report = simulated_stability
    changed_source = {**SOURCE, "source_sha256": "d" * 64}
    assert not auditor._long_stability_clean(report, path, changed_source)


@pytest.fixture
def simulated_bundle(tmp_path, monkeypatch):
    """Fabricated test outputs solely to exercise audit rejection and counting."""
    monkeypatch.setattr(auditor, "source_snapshot", lambda: SOURCE)
    monkeypatch.setattr(evidence, "source_snapshot", lambda: SOURCE)
    for name in auditor.BROWSER_FILES:
        browser, scale = name.removeprefix("browser-real-").removesuffix(".json").split("-scale-")
        report = {
            "database": {
                "applied_migrations": EXPECTED_APPLIED_MIGRATIONS,
                "latest_migration": EXPECTED_LATEST_MIGRATION,
                "foreign_key_violations": 0, "integrity_check": "ok", "journal_mode": "wal",
            },
            "browser": {"browser": browser, "scale": float(scale.replace("_", ".")),
                        "journeys": [{
                            "role": role, "passed": True, "console_error_count": 0,
                            "request_failure_count": 0, "horizontal_overflow_px": 0,
                            "clipped_control_count": 0, "interactive_overlap_count": 0,
                            "keyboard_navigation_passed": True, "duration_ms": 20000,
                            "expected_fault_console_error_count": 1 if role == "admin" else 0,
                            "ui_locale": "en-US" if role == "admin" else "zh-CN",
                        } for role in ("admin", "developer", "viewer")]},
        }
        (tmp_path / name).write_text(json.dumps(report), encoding="utf-8")
    for name in ("backend", "performance"):
        (tmp_path / f"{name}-junit.xml").write_text(
            '<testsuites><testsuite tests="8" failures="0" errors="0" skipped="0"/></testsuites>',
            encoding="utf-8",
        )
    (tmp_path / "web.out").write_text("pass 17\nfail 0\nskipped 0\ntodo 0\n", encoding="utf-8")
    (tmp_path / "console-lint.out").write_text("console lint ok (832 files)\n", encoding="utf-8")
    for name, count in (("live", 30), ("pressure", EXPECTED_PRESSURE_REQUESTS)):
        (tmp_path / f"performance-{name}.json").write_text(
            json.dumps({"requests": count, "successes": count, "errors": 0}), encoding="utf-8",
        )
    (tmp_path / "summary.json").write_text(json.dumps({
        "all_108_prds_complete": False,
        "results": [{"group": name, "exit_code": 0, "error_bytes": 0}
                    for name in sorted(auditor.EXPECTED_TASKS)],
    }), encoding="utf-8")
    _seal(tmp_path)
    return tmp_path


def _seal(directory):
    (directory / "evidence-manifest.json").write_text(
        json.dumps(evidence.seal_evidence(directory, SOURCE)), encoding="utf-8",
    )


def test_audit_accepts_complete_current_bundle_without_claiming_ga(
    simulated_bundle, simulated_stability,
):
    result = auditor.audit(simulated_bundle, simulated_stability[0])
    assert result["status"] == "passed" and result["passed"] == 97
    assert result["ga_approval"] is False and result["all_108_prds_complete"] is False


def test_old_bundle_without_provenance_cannot_count_as_current(
    simulated_bundle, simulated_stability,
):
    (simulated_bundle / "evidence-manifest.json").unlink()
    result = auditor.audit(simulated_bundle, simulated_stability[0])
    assert result["status"] == "failed" and result["passed"] == 0
    assert result["facts"]["provenance"] is False


def test_old_schema_fails_even_with_a_valid_manifest(simulated_bundle, simulated_stability):
    path = simulated_bundle / sorted(auditor.BROWSER_FILES)[0]
    report = json.loads(path.read_text(encoding="utf-8"))
    report["database"].update(applied_migrations=139, latest_migration="0139")
    path.write_text(json.dumps(report), encoding="utf-8")
    _seal(simulated_bundle)
    result = auditor.audit(simulated_bundle, simulated_stability[0])
    assert result["status"] == "failed" and result["facts"]["database"] is False


def test_duplicate_task_cannot_replace_a_required_task(simulated_bundle, simulated_stability):
    path = simulated_bundle / "summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary["results"][0] = summary["results"][1]
    path.write_text(json.dumps(summary), encoding="utf-8")
    _seal(simulated_bundle)
    result = auditor.audit(simulated_bundle, simulated_stability[0])
    assert result["status"] == "failed" and result["facts"]["gate"] is False


def test_missing_bundle_lists_required_artifacts_without_claiming_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(auditor, "source_snapshot", lambda: SOURCE)
    result = auditor.audit(tmp_path, tmp_path / "status.json")
    assert result["reason"] == "missing_evidence" and result["status"] == "failed"
    assert set(result["missing_evidence"]) == auditor.REQUIRED_FILES
