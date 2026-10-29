"""Project immutable explanation evidence using the final verified permissions."""

from .models import RequestExplanation

SECTION_PERMISSIONS = {
    "authentication": {"virtual_key.read"},
    "rate_limit": {"request_log.read"},
    "budget": {"billing.budget.read"},
    "dlp": {"dlp.read"},
    "policy": {"policy.read"},
    "routing": {"routing.read"},
    "keypool": {"provider.read"},
    "provider_response": {"provider.read", "request_log.read"},
    "usage_and_finops": {"billing.usage.read", "billing.finops.read"},
    "logs": {"request_log.read"},
    "webhooks": {"webhook.manage"},
}
STAGE_SECTIONS = {
    "provider": ("provider_response",),
    "logs_and_webhooks": ("logs", "webhooks"),
}
# Cross-domain fields remain nullable within the exact-key fact contract.
FIELD_PERMISSIONS = {
    "environment.read": {"environment_id", "environment_name"},
    "project.read": {"project_id", "project_name", "application_id", "application_name"},
    "policy.read": {
        "snapshot_version",
        "snapshot_status",
        "policy_snapshot_status",
        "policy_id",
        "policy_action_type",
        "policy_target_model_id",
        "policy_decision",
        "policy_reason_code",
    },
    "routing.read": {
        "routing_policy_id",
        "routing_policy_version",
        "routing_mode",
        "fallback_triggered",
        "routing_attempt_count",
        "final_routing_attempt_id",
        "routing_attempt_id",
    },
    "provider.read": {"provider_id", "provider_code", "keypool_state"},
    "provider.health.read": {"health_state", "health_fresh"},
    "model.read": {
        "request_model",
        "requested_model",
        "resolved_model",
        "selected_model_id",
        "selected_internal_model_id",
        "model_id",
        "internal_model_id",
    },
    "model_price.read": {
        "price_available",
        "price_source",
        "price_schema_version",
        "price_snapshot_id",
        "price_snapshot_source",
        "price_evidence_status",
        "cost_component_bps",
    },
    "model_quality.read": {"quality_component_bps", "weighted_score_bps"},
    "billing.usage.read": {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_tokens",
        "estimated_cost_amount",
        "estimated_cost_cny",
        "cost_amount",
        "cost_cny",
        "cost_usd",
        "currency",
    },
    "request_log.read": {"latency_ms", "error_code"},
}
FACT_ID_PERMISSIONS = {"routing_candidate": "model.read"}


def project_explanation(
    explanation: RequestExplanation, permissions: tuple[str, ...]
) -> RequestExplanation:
    allowed = set(permissions)
    payload = explanation.model_dump()
    restricted = {name for name, required in SECTION_PERMISSIONS.items() if not required <= allowed}
    hidden_fields = set().union(
        *(fields for permission, fields in FIELD_PERMISSIONS.items() if permission not in allowed)
    )
    for field in hidden_fields & payload["summary"].keys():
        payload["summary"][field] = None
    for name in SECTION_PERMISSIONS:
        if name in restricted:
            payload[name] = {
                "evidence_state": "restricted",
                "reason_code": "request_explanation.permission_required",
                "facts": [],
                "truncated": False,
            }
        else:
            for fact in payload[name]["facts"]:
                for field in hidden_fields & fact["values"].keys():
                    fact["values"][field] = None
                required = FACT_ID_PERMISSIONS.get(fact["kind"])
                if required and required not in allowed:
                    fact["fact_id"] = None
    for stage in payload["decision_chain"]:
        section_names = STAGE_SECTIONS.get(stage["stage"], (stage["stage"],))
        if restricted.intersection(section_names):
            stage.update(
                status="restricted",
                evidence_state="restricted",
                reason_code="request_explanation.permission_required",
                occurred_at=None,
                evidence_refs=[],
            )
        else:
            visible_refs = {
                fact["fact_id"] for name in section_names for fact in payload[name]["facts"]
            }
            stage["evidence_refs"] = [ref for ref in stage["evidence_refs"] if ref in visible_refs]
    payload["missing_sections"] = [
        name for name in payload["missing_sections"] if name not in restricted
    ]
    payload["truncated_sections"] = [
        name for name in payload["truncated_sections"] if name not in restricted
    ]
    if restricted:
        payload["status"] = "restricted"
    return RequestExplanation.model_validate(payload)
