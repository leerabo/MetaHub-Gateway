"""PRD-075 审计契约测试：确保实验审计仅保存冻结配置摘要，不包含敏感或自由文本。

PRD-075 experiment audit projections exclude names and execution material.
"""

from metahub_control.audit.service import ACTION_OBJECT_TYPES, SUMMARY_FIELDS


def test_experiment_created_audit_uses_only_frozen_safe_configuration() -> None:
    """验证实验审计只保存冻结安全配置和关联标识，不包含敏感或自由文本。"""
    assert ACTION_OBJECT_TYPES["experiment.created"] == "experiment"
    assert SUMMARY_FIELDS["experiment"] == frozenset(
        {
            "experiment_id",
            "project_id",
            "environment_id",
            "model_alias_id",
            "model_a_id",
            "model_b_id",
            "target_sample_count",
            "traffic_sample_bps",
            "model_a_allocation_bps",
            "sampling_duration_seconds",
            "feedback_collection_enabled",
            "feedback_window_seconds",
        }
    )


def test_experiment_audit_forbids_sensitive_or_free_text_fields() -> None:
    """验证实验审计只保存冻结安全配置和关联标识，不包含敏感或自由文本。"""
    forbidden = {
        "name",
        "prompt",
        "response",
        "provider_key",
        "token",
        "endpoint",
        "base_url",
        "idempotency_key",
        "idempotency_key_hash",
        "request_fingerprint",
        "sampling_key_fingerprint",
        "route_config_fingerprint",
    }
    assert SUMMARY_FIELDS["experiment"].isdisjoint(forbidden)
