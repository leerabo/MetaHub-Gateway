"""Audit the product-QA plan catalog without treating catalog entries as test passes."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "testing" / "product-qa" / "TEST-PLAN.md"
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


def test_product_qa_plan_has_every_declared_case_exactly_once() -> None:
    """The catalog is complete metadata; runtime artifacts remain the pass authority."""

    text = PLAN.read_text(encoding="utf-8")
    observed = re.findall(r"^\| ([A-Z]+-\d{2}) \|", text, flags=re.MULTILINE)
    expected = [
        f"{group}-{index:02d}"
        for group, count in EXPECTED_COUNTS.items()
        for index in range(1, count + 1)
    ]
    assert len(observed) == len(set(observed)), "duplicate product-QA test ID"
    assert set(observed) == set(expected)
    assert "not_run" in text
    assert "skipped" in text
    assert "all_108_prds_complete" in text
