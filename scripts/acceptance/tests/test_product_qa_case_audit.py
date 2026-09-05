from pathlib import Path

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
