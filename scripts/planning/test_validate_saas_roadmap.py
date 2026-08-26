"""Focused tests for the PRD-090 roadmap validator."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate_saas_roadmap.py")
SPEC = importlib.util.spec_from_file_location("validate_saas_roadmap", MODULE_PATH)
assert SPEC and SPEC.loader
VALIDATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VALIDATOR
SPEC.loader.exec_module(VALIDATOR)


def test_dependency_extraction_only_accepts_explicit_prd_references() -> None:
    assert VALIDATOR._explicit_dependencies(
        "PRD-090..105、PRD-040/062/089"
    ) == {40, 62, 89, 90, 105}


def test_roadmap_sequence_has_no_fixed_upper_limit() -> None:
    assert VALIDATOR._continuous_prd_ids(["PRD-090", "PRD-091", "PRD-092"])
    assert VALIDATOR._continuous_prd_ids(
        [f"PRD-{number:03d}" for number in range(90, 141)]
    )
    assert not VALIDATOR._continuous_prd_ids(["PRD-090", "PRD-092"])


def test_cycle_detector_accepts_backward_dag_and_rejects_cycle() -> None:
    assert VALIDATOR._is_acyclic({90: set(), 91: {90}, 92: {90, 91}})
    assert not VALIDATOR._is_acyclic({90: {91}, 91: {90}})


def test_repository_roadmap_baseline_passes_after_prd121_completion() -> None:
    report = VALIDATOR.validate(expected_next="PRD-122")
    assert report.ok, report.errors
    assert report.next_prd == "PRD-122"
