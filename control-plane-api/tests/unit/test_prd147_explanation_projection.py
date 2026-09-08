"""Field disclosure is independent of entry permission and never mutates audit evidence."""

import pytest
from metahub_control.request_explanations.contracts import (
    FACT_VALUE_KEYS,
    SECTION_FACT_KINDS,
    SECTION_ORDER,
    STAGE_ORDER,
)
from metahub_control.request_explanations.models import RequestExplanation
from metahub_control.request_explanations.projection import (
    FIELD_PERMISSIONS,
    SECTION_PERMISSIONS,
    project_explanation,
)
from metahub_shared.rbac import PERMISSION_CODES


def explanation():
    timestamp = "2026-09-08T00:00:00Z"
    sections = {
        name: {
            "evidence_state": "complete",
            "reason_code": "test.recorded",
            "facts": [
                {
                    "kind": kind,
                    "fact_id": "fact_" + kind,
                    "occurred_at": timestamp,
                    "values": {key: "private_" + key for key in FACT_VALUE_KEYS[kind]},
                }
                for kind in sorted(SECTION_FACT_KINDS[name])
            ],
            "truncated": False,
        }
        for name in SECTION_ORDER
    }
    return RequestExplanation.model_validate(
        {
            "request_explanation_id": "rxe_test",
            "source_request_id": "req_test",
            "source_trace_id": "trace_test",
            "status": "complete",
            "evidence_version": "request_explanation_v1",
            "captured_at": timestamp,
            "summary": {
                "source_status": "success",
                "input_tokens": 7,
                "output_tokens": 4,
                "total_tokens": 11,
                "currency": "CNY",
                "estimated_cost_amount": "0.123",
                "fallback_triggered": True,
                "created_at": timestamp,
                "updated_at": timestamp,
                "project_id": "private_project",
                "provider_code": "private_provider",
                "request_model": "private_model",
                "snapshot_version": 3,
            },
            "decision_chain": [
                {
                    "stage": stage,
                    "status": "passed",
                    "evidence_state": "complete",
                    "reason_code": "private_reason",
                    "occurred_at": timestamp,
                    "evidence_refs": ["fact_routing_candidate"],
                }
                for stage in STAGE_ORDER
            ],
            "missing_sections": [],
            "truncated_sections": [],
            **sections,
        }
    )


def test_projection_uses_only_existing_permission_codes_and_keeps_input_immutable():
    required = set(FIELD_PERMISSIONS).union(*SECTION_PERMISSIONS.values())
    assert required <= set(PERMISSION_CODES)
    source = explanation()
    before = source.model_dump()
    result = project_explanation(source, ("request_explanation.read",))
    assert source.model_dump() == before
    assert result.status == "restricted"
    assert result.summary.input_tokens is None
    assert result.summary.estimated_cost_amount is None
    assert result.summary.provider_code is None
    assert result.summary.project_id is None
    assert result.summary.fallback_triggered is None
    assert not result.missing_sections and not result.truncated_sections
    for name in SECTION_ORDER:
        section = getattr(result, name)
        assert section.evidence_state == "restricted"
        assert not section.facts and not section.truncated
    for stage in result.decision_chain:
        assert stage.status == stage.evidence_state == "restricted"
        assert stage.reason_code == "request_explanation.permission_required"
        assert stage.occurred_at is None and not stage.evidence_refs


@pytest.mark.parametrize("permission", sorted(FIELD_PERMISSIONS))
def test_cross_domain_fields_are_null_without_source_permission(permission):
    source = explanation()
    result = project_explanation(source, tuple(set(PERMISSION_CODES) - {permission}))
    fields = FIELD_PERMISSIONS[permission]
    for field in fields & type(result.summary).model_fields.keys():
        assert getattr(result.summary, field) is None
    for name in SECTION_ORDER:
        for fact in getattr(result, name).facts:
            for field in fields & fact.values.keys():
                assert fact.values[field] is None
    if permission == "model.read":
        candidate = next(fact for fact in result.routing.facts if fact.kind == "routing_candidate")
        assert candidate.fact_id is None
        assert "fact_routing_candidate" not in result.decision_chain[5].evidence_refs


@pytest.mark.parametrize("name", SECTION_ORDER)
def test_each_section_requires_its_own_domain_permission(name):
    for permission in SECTION_PERMISSIONS[name]:
        result = project_explanation(explanation(), tuple(set(PERMISSION_CODES) - {permission}))
        section = getattr(result, name)
        assert section.evidence_state == "restricted"
        assert section.facts == []
        assert result.status == "restricted"


def test_full_permissions_preserve_complete_facts_and_valid_references():
    source = explanation()
    source.decision_chain[5].evidence_refs = ["fact_routing_candidate"]
    result = project_explanation(source, tuple(PERMISSION_CODES))
    assert result.status == "complete"
    assert result.summary == source.summary
    for name in SECTION_ORDER:
        assert getattr(result, name) == getattr(source, name)
    assert result.decision_chain[5].evidence_refs == ["fact_routing_candidate"]
