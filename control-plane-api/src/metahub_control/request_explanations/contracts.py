"""PRD-054 请求解释公开事实的精确字段与顺序合同。

数据库表会随 PRD 演进，但解释 API 只能暴露这里逐项审查过的安全字段。服务构造器与
Pydantic 响应验证共同复用本模块，任何新字段都必须同时修改合同和安全测试，不能通过
``SELECT *``、sqlite Row 或任意字典自动进入公开响应。
"""

from __future__ import annotations

from typing import Literal

FactKind = Literal[
    "authentication",
    "rate_limit",
    "budget_evaluation",
    "budget_scope_event",
    "dlp_match",
    "policy_snapshot",
    "routing_decision",
    "routing_candidate",
    "cache_affinity",
    "routing_attempt",
    "usage",
    "finops_entry",
    "request_log",
    "stream_session",
    "webhook_delivery",
    "webhook_attempt",
]

# 阶段与区块顺序属于跨后端/Console 的版本化公开合同，禁止按数据库返回顺序动态变化。
STAGE_ORDER = (
    "authentication",
    "rate_limit",
    "budget",
    "dlp",
    "policy",
    "routing",
    "keypool",
    "provider",
    "usage_and_finops",
    "logs_and_webhooks",
)
SECTION_ORDER = (
    "authentication",
    "rate_limit",
    "budget",
    "dlp",
    "policy",
    "routing",
    "keypool",
    "provider_response",
    "usage_and_finops",
    "logs",
    "webhooks",
)

# 每个 kind 都要求 exact-key 相等；None 仍表示“字段已审查但该历史事实没有值”，不会
# 退化为省略字段后让客户端猜测 schema 版本。
FACT_VALUE_KEYS: dict[str, frozenset[str]] = {
    "authentication": frozenset(
        {
            "virtual_key_id",
            "virtual_key_prefix",
            "environment_id",
            "environment_name",
            "project_id",
            "project_name",
            "application_id",
            "application_name",
            "snapshot_version",
            "policy_snapshot_status",
        }
    ),
    "rate_limit": frozenset({"decision", "error_code"}),
    "budget_evaluation": frozenset(
        {
            "budget_rule_id",
            "scope_type",
            "scope_id",
            "budget_date",
            "spent_before_cny",
            "estimated_request_cost_cny",
            "projected_cost_cny",
            "budget_cny",
            "threshold_percent",
            "budget_status",
            "action_taken",
            "reason_code",
        }
    ),
    "budget_scope_event": frozenset(
        {
            "event_id",
            "budget_rule_id",
            "scope_type",
            "scope_id",
            "action_taken",
            "posted_cost_micros",
            "reserved_cost_micros",
            "estimated_request_cost_micros",
            "projected_cost_micros",
            "budget_micros",
        }
    ),
    "dlp_match": frozenset(
        {
            "dlp_rule_id",
            "rule_name",
            "rule_type",
            "action",
            "message_index",
            "match_text_hash",
            "match_length",
        }
    ),
    "policy_snapshot": frozenset(
        {
            "snapshot_version",
            "snapshot_status",
            "policy_id",
            "policy_action_type",
            "policy_target_model_id",
            "policy_decision",
            "policy_reason_code",
            "policy_mask_applied",
            "policy_mask_policy_id",
            "routing_policy_id",
            "routing_policy_version",
            "routing_mode",
        }
    ),
    "routing_decision": frozenset(
        {
            "decision_status",
            "decision_reason",
            "requested_model",
            "selected_model_id",
            "selected_internal_model_id",
            "provider_id",
            "provider_code",
            "error_code",
        }
    ),
    "routing_candidate": frozenset(
        {
            "model_id",
            "provider_id",
            "candidate_role",
            "configured_priority",
            "rank",
            "status",
            "reason_code",
            "health_state",
            "health_fresh",
            "keypool_state",
            "price_available",
            "price_source",
            "price_schema_version",
            "currency",
            "estimated_cost_amount",
            "quality_component_bps",
            "cost_component_bps",
            "latency_component_bps",
            "weighted_score_bps",
        }
    ),
    "cache_affinity": frozenset(
        {
            "attempt_sequence",
            "routing_attempt_id",
            "target_kind",
            "target_generation",
            "provider_id",
            "provider_code",
            "endpoint_preference_applied",
            "endpoint_preference_state",
            "binding_state_before",
            "selection_reason",
            "estimated_hit",
            "outcome_status",
        }
    ),
    "routing_attempt": frozenset(
        {
            "attempt_sequence",
            "attempt_role",
            "trigger_type",
            "trigger_error_code",
            "model_id",
            "model_name",
            "internal_model_id",
            "provider_id",
            "provider_code",
            "provider_name",
            "status",
            "error_code",
            "error_category",
            "upstream_http_status",
            "retryable",
            "latency_ms",
            "usage_status",
            "estimated_cost_cny",
        }
    ),
    "usage": frozenset(
        {
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_tokens",
            "cost_amount",
            "cost_cny",
            "cost_usd",
            "currency",
            "price_schema_version",
            "price_snapshot_id",
            "estimated",
            "usage_source",
            "routing_attempt_id",
        }
    ),
    "finops_entry": frozenset(
        {
            "ledger_sequence",
            "event_id",
            "entry_type",
            "direction",
            "amount_micros",
            "currency",
            "normalized_cny_micros",
            "cny_normalization_status",
            "price_evidence_status",
            "price_snapshot_source",
            "posting_source",
            "quota_type",
            "price_snapshot_id",
            "price_schema_version",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_tokens",
            "estimated",
            "status",
        }
    ),
    "request_log": frozenset(
        {
            "source_status",
            "error_code",
            "stream_status",
            "latency_ms",
            "fallback_triggered",
            "routing_attempt_count",
            "final_routing_attempt_id",
        }
    ),
    "stream_session": frozenset(
        {
            "status",
            "inter_token_timeout_ms",
            "chunk_count",
            "bytes_sent",
            "first_chunk_at",
            "last_chunk_at",
            "error_code",
            "completed_at",
        }
    ),
    "webhook_delivery": frozenset(
        {
            "event_id",
            "event_type",
            "webhook_endpoint_id",
            "endpoint_name",
            "status",
            "attempt_count",
            "max_attempts",
            "http_status",
            "duration_ms",
            "error_code",
            "next_retry_at",
            "delivery_kind",
            "replayed_from_delivery_id",
        }
    ),
    "webhook_attempt": frozenset(
        {
            "webhook_delivery_id",
            "webhook_endpoint_id",
            "attempt_no",
            "status",
            "http_status",
            "duration_ms",
            "error_code",
        }
    ),
}

SECTION_FACT_KINDS: dict[str, frozenset[str]] = {
    "authentication": frozenset({"authentication"}),
    "rate_limit": frozenset({"rate_limit"}),
    "budget": frozenset({"budget_evaluation", "budget_scope_event"}),
    "dlp": frozenset({"dlp_match"}),
    "policy": frozenset({"policy_snapshot"}),
    "routing": frozenset({"routing_decision", "routing_candidate"}),
    "keypool": frozenset({"routing_candidate", "cache_affinity", "routing_attempt"}),
    "provider_response": frozenset({"routing_attempt"}),
    "usage_and_finops": frozenset({"usage", "finops_entry"}),
    "logs": frozenset({"request_log", "stream_session"}),
    "webhooks": frozenset({"webhook_delivery", "webhook_attempt"}),
}


def validate_fact_values(kind: str, values: dict[str, object]) -> None:
    """校验一条公开事实的 exact-key 集合。

    参数：固定 kind 和准备进入响应的扁平值。返回：合法时无返回值。关键约束：未知 kind、
    缺字段和额外字段同样失败，避免未来数据库列或敏感摘要被静默放行。
    """

    expected = FACT_VALUE_KEYS.get(kind)
    if expected is None or set(values) != expected:
        # 未知 kind、少字段和多字段都可能造成敏感列穿透，因此必须在序列化前失败关闭。
        raise ValueError("request_explanation.fact_fields_invalid")


__all__ = [
    "FACT_VALUE_KEYS",
    "FactKind",
    "SECTION_FACT_KINDS",
    "SECTION_ORDER",
    "STAGE_ORDER",
    "validate_fact_values",
]
