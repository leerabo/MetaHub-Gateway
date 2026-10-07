"""PRD-047/048 统一错误、路由事件与人工模型评分 Webhook 合同测试。"""

from __future__ import annotations

import pytest
from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.webhooks import (
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 新错误表锁定 HTTP、重试性和诊断规则，防止跨服务各自解释同一路由故障。
PRD047_ERRORS = {
    "model_alias.routing_mode_invalid": (422, False, "routing_policy_payload_invalid"),
    "model_alias.routing_policy_version_invalid": (
        422,
        False,
        "routing_policy_payload_invalid",
    ),
    "model_alias.routing_policy_version_conflict": (
        409,
        False,
        "routing_policy_write_conflict",
    ),
    "model_alias.routing_policy_request_conflict": (
        409,
        False,
        "routing_policy_write_conflict",
    ),
    "model_alias.routing_policy_update_failed": (
        503,
        True,
        "routing_policy_update_failed",
    ),
    "gateway.no_route_available": (503, True, "gateway_no_route_available"),
    "gateway.routing_policy_invalid": (
        503,
        False,
        "gateway_routing_snapshot_invalid",
    ),
    "gateway.routing_snapshot_unavailable": (
        503,
        True,
        "gateway_routing_snapshot_unavailable",
    ),
    "gateway.routing_snapshot_invalid": (
        503,
        False,
        "gateway_routing_snapshot_invalid",
    ),
    "gateway.routing_decision_unavailable": (
        503,
        True,
        "gateway_routing_snapshot_unavailable",
    ),
}


def _request_selected_payload() -> dict[str, object]:
    """构造运行时选路事件允许的 13 个精确安全字段。"""

    return {
        "trigger": "request_selected",
        "request_id": "req_prd047_route",
        "routing_decision_id": "rdec_prd047_route",
        "model_alias_id": "alias_prd047",
        "routing_policy_id": "rpol_prd047",
        "routing_policy_version": 2,
        "routing_mode": "cost_first",
        "selected_model_id": "model_prd047_low_cost",
        "selected_provider_id": "provider_prd047",
        "candidate_count": 2,
        "eligible_candidate_count": 2,
        "selection_reason": "routing.cost_lowest",
        "project_id": None,
    }


def _configuration_changed_payload() -> dict[str, object]:
    """构造模式和候选在同一事务变更时允许的精确安全摘要。"""

    return {
        "trigger": "configuration_changed",
        "request_id": "req_prd047_config",
        "model_alias_id": "alias_prd047",
        "routing_policy_id": "rpol_prd047",
        "routing_policy_version": 3,
        "change_kinds": ["mode", "candidates", "alias_state"],
        "changed_routing_fields": [
            "status",
            "fallback_model_id",
            "routing_mode",
        ],
        "previous_mode": "fixed",
        "routing_mode": "cost_first",
    }


def _event_id(suffix: str) -> str:
    """把单字符测试后缀扩展为满足 rpe_ 加 32 位小写十六进制的持久事件 ID。"""

    return "rpe_" + suffix * 32


def _quality_cost_config_payload() -> dict[str, int]:
    """构造 v2 路由配置事件要求的完整基点权重、门槛和最小样本快照。"""

    return {
        "quality_weight_bps": 5_000,
        "cost_weight_bps": 3_000,
        "latency_weight_bps": 2_000,
        "quality_floor_bps": 6_000,
        "minimum_sample_count": 20,
    }


def _v2_configuration_payload() -> dict[str, object]:
    """把既有配置事件提升为 schema v2，并冻结休眠质量配置。"""

    payload = _configuration_changed_payload()
    payload.update(
        {
            "configuration_schema_version": 2,
            "quality_cost_config": _quality_cost_config_payload(),
        }
    )
    return payload


def _feedback_payload() -> dict[str, object]:
    """构造管理员人工先验真实变化时允许外发的九字段安全载荷。"""

    return {
        "trigger": "manual_score_updated",
        "request_id": "req_prd048_feedback",
        "model_quality_score_id": "mqs_" + "1" * 32,
        "model_id": "model_prd048_feedback",
        "previous_manual_quality_bps": 7_000,
        "manual_quality_bps": 8_500,
        "feedback_version": 2,
        "statistics_version": 42,
        "sample_count": 42,
    }


def _feedback_event_id(suffix: str) -> str:
    """构造与 model_quality_score_events 行一一对应的稳定反馈事件标识。"""

    return "mqe_" + suffix * 32


def test_prd047_errors_have_exact_http_retry_diagnosis_and_i18n() -> None:
    """十个新错误均显式登记，并保持 retryable、固定诊断和双语标签合同。"""

    for code, (http_status, retryable, rule_id) in PRD047_ERRORS.items():
        spec = ERROR_SPECS[code]
        diagnosis = diagnose_error(code)
        assert (spec.http_status, spec.retryable) == (http_status, retryable)
        assert diagnosis.rule_id == rule_id
        assert diagnosis.retryable is retryable
        assert diagnosis.matched_by == "error_code"
        assert error_label(code, "zh-CN") != code
        assert error_label(code, "en-US") != code


def test_prd047_dependency_errors_keep_provider_key_and_budget_guidance_safe() -> None:
    """安全：Provider Key 与 Budget 依赖错误必须使用可执行建议，不得鼓励跨租户环境回退。"""

    provider_spec = ERROR_SPECS["provider_key.unavailable"]
    provider_diagnosis = diagnose_error(
        "provider_key.unavailable",
        http_status=503,
    )
    assert provider_spec.http_status == 503
    assert provider_spec.retryable is True
    assert provider_spec.suggestion == (
        "Enable a managed provider key for the current tenant. The environment fallback is "
        "limited to the non-production default tenant and its configured Provider endpoint."
    )
    assert provider_diagnosis.rule_id == "provider_key_unavailable"
    assert provider_diagnosis.category == "provider_key_state"
    assert provider_diagnosis.retryable is True
    assert provider_diagnosis.matched_by == "error_code"
    assert "当前租户" in provider_diagnosis.suggestion
    assert "非生产" in provider_diagnosis.suggestion
    assert "默认租户" in provider_diagnosis.suggestion
    assert "Endpoint" in provider_diagnosis.suggestion
    assert "生产或其他租户" in provider_diagnosis.action

    virtual_key_diagnosis = diagnose_error("gateway.auth_invalid", http_status=401)
    assert virtual_key_diagnosis.rule_id == "virtual_key_invalid"
    assert virtual_key_diagnosis.category == "key_state"
    assert virtual_key_diagnosis.retryable is False
    assert virtual_key_diagnosis.matched_by == "error_code"
    assert "Virtual Key" in virtual_key_diagnosis.suggestion

    budget_spec = ERROR_SPECS["gateway.budget_unavailable"]
    budget_diagnosis = diagnose_error(
        "gateway.budget_unavailable",
        http_status=503,
    )
    assert (budget_spec.http_status, budget_spec.retryable) == (503, True)
    assert budget_diagnosis.rule_id == "gateway_budget_unavailable"
    assert budget_diagnosis.retryable is True
    assert budget_diagnosis.matched_by == "error_code"


def test_route_policy_selected_accepts_both_exact_persistent_variants() -> None:
    """请求选路和配置变更均可订阅，且必须复用调用方提供的稳定 event ID。"""

    assert "route.policy_selected" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    request_event = build_webhook_event(
        tenant_id="tenant_prd047",
        event_type="route.policy_selected",
        payload=_request_selected_payload(),
        object_id="rpol_prd047",
        trace_id="trc_prd047_route",
        event_id=_event_id("a"),
    )
    config_event = build_webhook_event(
        tenant_id="tenant_prd047",
        event_type="route.policy_selected",
        payload=_configuration_changed_payload(),
        actor_id="user_prd047_admin",
        object_id="rpol_prd047",
        trace_id="trc_prd047_config",
        event_id=_event_id("b"),
    )
    assert request_event.event_id == _event_id("a")
    assert request_event.actor_id is None
    assert config_event.event_id == _event_id("b")
    assert config_event.actor_id == "user_prd047_admin"


def test_route_policy_selected_accepts_multiple_ordered_fallback_candidates() -> None:
    """一个主模型加多个备用模型必须能记录安全选路事件。"""

    payload = _request_selected_payload()
    payload["candidate_count"] = 3
    payload["eligible_candidate_count"] = 3
    event = build_webhook_event(
        tenant_id="tenant_prd047",
        event_type="route.policy_selected",
        payload=payload,
        object_id="rpol_prd047",
        trace_id="trc_prd047_multi",
        event_id=_event_id("c"),
    )
    assert event.event_id == _event_id("c")


@pytest.mark.parametrize(
    ("mutate", "event_id"),
    [
        (lambda payload: payload.pop("selection_reason"), _event_id("1")),
        (lambda payload: payload.__setitem__("extra", "unsafe"), _event_id("2")),
        (lambda payload: payload.__setitem__("candidate_count", True), _event_id("3")),
        (
            lambda payload: payload.__setitem__("routing_mode", "quality_first"),
            _event_id("4"),
        ),
        (lambda payload: payload.__setitem__("prompt", "secret"), _event_id("5")),
        (lambda _payload: None, None),
    ],
)
def test_request_selected_rejects_missing_extra_sensitive_and_random_id(
    mutate,
    event_id: str | None,
) -> None:
    """运行时事件缺字段、额外字段、敏感正文、类型漂移或随机 ID 均整体拒绝。"""

    payload = _request_selected_payload()
    mutate(payload)
    with pytest.raises(ValueError):
        build_webhook_event(
            tenant_id="tenant_prd047",
            event_type="route.policy_selected",
            payload=payload,
            event_id=event_id,
            object_id="rpol_prd047",
            trace_id="trc_prd047_invalid",
        )


def test_configuration_changed_enforces_stable_arrays_and_mode_transition() -> None:
    """配置事件数组必须去重有序，previous/current mode 必须与 change_kinds 一致。"""

    for field, invalid in (
        ("change_kinds", ["candidates", "mode"]),
        ("change_kinds", ["mode", "mode"]),
        ("changed_routing_fields", ["routing_mode", "status"]),
        ("previous_mode", "cost_first"),
    ):
        payload = _configuration_changed_payload()
        payload[field] = invalid
        with pytest.raises(ValueError):
            build_webhook_event(
                tenant_id="tenant_prd047",
                event_type="route.policy_selected",
                payload=payload,
                event_id=_event_id("c"),
                actor_id="user_prd047_admin",
                object_id="rpol_prd047",
                trace_id="trc_prd047_invalid",
            )


def test_configuration_created_allows_null_previous_mode_and_initial_fields() -> None:
    """Alias 创建事件可显式列出初始 routing_mode，但 previous_mode 必须为空。"""

    payload = _configuration_changed_payload()
    payload.update(
        {
            "routing_policy_version": 1,
            "change_kinds": ["created"],
            "changed_routing_fields": [
                "alias",
                "environment",
                "environment_id",
                "status",
                "default_model_id",
                "fallback_model_id",
                "routing_mode",
            ],
            "previous_mode": None,
            "routing_mode": "fixed",
        }
    )
    event = build_webhook_event(
        tenant_id="tenant_prd047",
        event_type="route.policy_selected",
        payload=payload,
        event_id=_event_id("d"),
        actor_id="user_prd047_admin",
        object_id="rpol_prd047",
        trace_id="trc_prd047_created",
    )
    assert event.payload["change_kinds"] == ["created"]


@pytest.mark.parametrize(
    ("trigger", "actor_id", "object_id", "trace_id", "event_id"),
    [
        ("request_selected", "virtual_key_id", "rpol_prd047", "trc_ok", _event_id("6")),
        ("request_selected", None, "rpol_other", "trc_ok", _event_id("7")),
        ("request_selected", None, "rpol_prd047", None, _event_id("8")),
        ("request_selected", None, "rpol_prd047", "trc_ok", "evt_" + "9" * 32),
        ("configuration_changed", None, "rpol_prd047", "trc_ok", _event_id("a")),
    ],
)
def test_route_policy_selected_rejects_envelope_identity_drift(
    trigger: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    event_id: str,
) -> None:
    """actor、Policy object、trace 或 rpe_ 身份漂移时，签名前必须整体失败关闭。"""

    payload = (
        _request_selected_payload()
        if trigger == "request_selected"
        else _configuration_changed_payload()
    )
    with pytest.raises(ValueError):
        build_webhook_event(
            tenant_id="tenant_prd047",
            event_type="route.policy_selected",
            payload=payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
        )


def test_routing_configuration_v2_accepts_create_quality_change_and_mode_transitions() -> None:
    """v2 必须覆盖创建、休眠配置变化以及进入和退出 quality_cost 的完整生命周期。"""

    quality_changed = _v2_configuration_payload()
    quality_changed.update(
        {
            "change_kinds": ["quality_cost"],
            "changed_routing_fields": ["quality_cost_config"],
            "previous_mode": "fixed",
            "routing_mode": "fixed",
        }
    )
    into_quality = _v2_configuration_payload()
    into_quality.update(
        {
            "change_kinds": ["mode"],
            "changed_routing_fields": ["routing_mode"],
            "previous_mode": "fixed",
            "routing_mode": "quality_cost",
        }
    )
    out_of_quality = _v2_configuration_payload()
    out_of_quality.update(
        {
            "change_kinds": ["mode"],
            "changed_routing_fields": ["routing_mode"],
            "previous_mode": "quality_cost",
            "routing_mode": "fixed",
        }
    )
    created = _v2_configuration_payload()
    created.update(
        {
            "routing_policy_version": 1,
            "change_kinds": ["created"],
            "changed_routing_fields": [
                "alias",
                "environment",
                "environment_id",
                "status",
                "default_model_id",
                "fallback_model_id",
                "routing_mode",
                "quality_cost_config",
            ],
            "previous_mode": None,
            "routing_mode": "fixed",
        }
    )

    for suffix, payload in zip(
        ("1", "2", "3", "4"),
        (quality_changed, into_quality, out_of_quality, created),
        strict=True,
    ):
        event = build_webhook_event(
            tenant_id="tenant_prd048",
            event_type="route.policy_selected",
            payload=payload,
            event_id=_event_id(suffix),
            actor_id="user_prd048_admin",
            object_id="rpol_prd047",
            trace_id=f"trc_prd048_config_{suffix}",
        )
        assert event.payload["configuration_schema_version"] == 2
        assert event.payload["quality_cost_config"] == _quality_cost_config_payload()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("configuration_schema_version", 2.0),
        lambda payload: payload["quality_cost_config"].__setitem__(  # type: ignore[union-attr]
            "minimum_sample_count", True
        ),
        lambda payload: payload["quality_cost_config"].__setitem__(  # type: ignore[union-attr]
            "cost_weight_bps", 2_999
        ),
        lambda payload: payload.__setitem__("change_kinds", ["quality_cost"]),
        lambda payload: payload.pop("quality_cost_config"),
    ],
)
def test_routing_configuration_v2_rejects_schema_config_and_change_kind_drift(mutate) -> None:
    """schema 类型、权重强类型/总和、字段集合和 change kind 漂移都必须整体拒绝。"""

    payload = _v2_configuration_payload()
    mutate(payload)
    with pytest.raises(ValueError):
        build_webhook_event(
            tenant_id="tenant_prd048",
            event_type="route.policy_selected",
            payload=payload,
            event_id=_event_id("5"),
            actor_id="user_prd048_admin",
            object_id="rpol_prd047",
            trace_id="trc_prd048_config_invalid",
        )


def test_quality_cost_runtime_event_keeps_prd047_exact_safe_payload() -> None:
    """quality_cost 运行时复用原 13 字段摘要，不外发质量、价格、Token 或配置明细。"""

    payload = _request_selected_payload()
    payload.update(
        {
            "routing_mode": "quality_cost",
            "selected_model_id": "model_prd048_quality",
            "selection_reason": "routing.quality_cost_highest_score",
        }
    )
    event = build_webhook_event(
        tenant_id="tenant_prd048",
        event_type="route.policy_selected",
        payload=payload,
        event_id=_event_id("6"),
        object_id="rpol_prd047",
        trace_id="trc_prd048_runtime",
    )
    assert event.payload == payload
    assert len(event.payload) == 13

    payload["quality_cost_config"] = _quality_cost_config_payload()
    with pytest.raises(ValueError):
        build_webhook_event(
            tenant_id="tenant_prd048",
            event_type="route.policy_selected",
            payload=payload,
            event_id=_event_id("7"),
            object_id="rpol_prd047",
            trace_id="trc_prd048_runtime_extra",
        )


def test_model_feedback_recorded_accepts_set_and_clear_with_exact_envelope() -> None:
    """人工评分设置和清除都绑定稳定 mqe_、管理员、Score 对象与 trace_id。"""

    assert "model.feedback_recorded" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    set_payload = _feedback_payload()
    set_event = build_webhook_event(
        tenant_id="tenant_prd048",
        event_type="model.feedback_recorded",
        payload=set_payload,
        event_id=_feedback_event_id("8"),
        actor_id="user_prd048_admin",
        object_id=set_payload["model_quality_score_id"],  # type: ignore[arg-type]
        trace_id="trc_prd048_feedback_set",
    )
    assert set_event.payload == set_payload
    assert set_event.event_id == _feedback_event_id("8")

    clear_payload = _feedback_payload()
    clear_payload.update(
        {
            "previous_manual_quality_bps": 8_500,
            "manual_quality_bps": None,
            "feedback_version": 3,
        }
    )
    clear_event = build_webhook_event(
        tenant_id="tenant_prd048",
        event_type="model.feedback_recorded",
        payload=clear_payload,
        event_id=_feedback_event_id("9"),
        actor_id="user_prd048_admin",
        object_id=clear_payload["model_quality_score_id"],  # type: ignore[arg-type]
        trace_id="trc_prd048_feedback_clear",
    )
    assert clear_event.payload["manual_quality_bps"] is None
    assert clear_event.payload["feedback_version"] == 3


@pytest.mark.parametrize(
    ("mutate", "event_id", "actor_id", "object_id", "trace_id"),
    [
        (
            lambda payload: payload.__setitem__("manual_quality_bps", True),
            _feedback_event_id("a"),
            "user_admin",
            "payload",
            "trc_ok",
        ),
        (
            lambda payload: payload.__setitem__("feedback_version", 1),
            _feedback_event_id("b"),
            "user_admin",
            "payload",
            "trc_ok",
        ),
        (
            lambda payload: payload.__setitem__("statistics_version", 41),
            _feedback_event_id("c"),
            "user_admin",
            "payload",
            "trc_ok",
        ),
        (
            lambda payload: payload.__setitem__("manual_quality_bps", 7_000),
            _feedback_event_id("d"),
            "user_admin",
            "payload",
            "trc_ok",
        ),
        (
            lambda payload: payload.__setitem__("rating", 5),
            _feedback_event_id("e"),
            "user_admin",
            "payload",
            "trc_ok",
        ),
        (lambda _payload: None, _event_id("f"), "user_admin", "payload", "trc_ok"),
        (lambda _payload: None, _feedback_event_id("1"), None, "payload", "trc_ok"),
        (lambda _payload: None, _feedback_event_id("2"), "user_admin", "other", "trc_ok"),
        (lambda _payload: None, _feedback_event_id("3"), "user_admin", "payload", None),
    ],
)
def test_model_feedback_recorded_rejects_payload_and_envelope_drift(
    mutate,
    event_id: str,
    actor_id: str | None,
    object_id: str,
    trace_id: str | None,
) -> None:
    """同值、PRD-049 字段、坏版本和信封身份漂移均不得进入订阅投递。"""

    payload = _feedback_payload()
    mutate(payload)
    resolved_object = payload["model_quality_score_id"] if object_id == "payload" else object_id
    with pytest.raises(ValueError):
        build_webhook_event(
            tenant_id="tenant_prd048",
            event_type="model.feedback_recorded",
            payload=payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=resolved_object,  # type: ignore[arg-type]
            trace_id=trace_id,
        )
