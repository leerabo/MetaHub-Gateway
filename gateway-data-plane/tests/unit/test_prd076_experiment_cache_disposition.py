"""验证 PRD-076 不会用 Response Cache 污染 PRD-075 在线实验总体。"""

from __future__ import annotations

from dataclasses import MISSING, fields
from datetime import UTC, datetime, timedelta

from metahub_gateway.experiments.models import (
    ExperimentExecutionHandle,
    ExperimentRouteResolution,
)
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan


def _route() -> GatewayRoute:
    """构造不含凭据正文的固定路由，只用于验证实验解析合同。"""

    return GatewayRoute(
        tenant_id="tenant-prd076-experiment",
        model_id="model-prd076-experiment",
        model_alias="alias-prd076-experiment",
        upstream_model_name="upstream-prd076-experiment",
        capabilities={"chat": True, "stream": False},
        provider_id="provider-prd076-experiment",
        provider_code="local_openai",
        provider_type="openai_compatible",
        provider_auth_type="none",
        provider_name="PRD076 synthetic provider",
        provider_base_url="http://127.0.0.1:1/v1",
        requested_model="alias-prd076-experiment",
    )


def test_bypass_requires_explicit_response_cache_disposition() -> None:
    """普通旁路与活跃实验未采样必须产生不同且显式的缓存信号。"""

    not_in_scope = ExperimentRouteResolution.bypass(
        response_cache_disposition="not_in_scope"
    )
    unsampled = ExperimentRouteResolution.bypass(
        response_cache_disposition="active_scope_bypass"
    )

    assert not_in_scope.matched is False
    assert not_in_scope.response_cache_disposition == "not_in_scope"
    assert unsampled.matched is False
    assert unsampled.response_cache_disposition == "active_scope_bypass"
    assert fields(ExperimentRouteResolution)[1].name == "response_cache_disposition"
    assert fields(ExperimentRouteResolution)[1].default is MISSING


def test_selected_and_failed_experiment_routes_always_bypass_cache() -> None:
    """已采样和不可用实验臂都不能读取、预留或填充 Response Cache。"""

    route = _route()
    plan = GatewayRoutePlan(initial_route=route, initial_role="primary")
    # 使用完整 owner 证明新信号不会放宽 PRD-075 的 execution 字段或令牌隐藏边界。
    execution = ExperimentExecutionHandle(
        tenant_id=route.tenant_id,
        experiment_id="exp_" + "a" * 32,
        assignment_id="exas_" + "b" * 32,
        execution_id="exrn_" + "c" * 32,
        request_id="req-prd076-experiment",
        trace_id="trc-prd076-experiment",
        generation=1,
        lease_owner_id="gwe_" + "d" * 32,
        lease_token="synthetic-hidden-token",
        absolute_deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    selected = ExperimentRouteResolution.selected(plan, execution)
    failed = ExperimentRouteResolution.failed(route, "gateway.experiment_variant_unavailable")

    assert selected.matched is True
    assert selected.response_cache_disposition == "active_scope_bypass"
    assert failed.matched is True
    assert failed.response_cache_disposition == "active_scope_bypass"
