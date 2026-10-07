"""受控故障切换条件的窄单元测试。"""

from __future__ import annotations

import pytest

from metahub_gateway.api.openai_compatible import _should_fallback
from metahub_gateway.providers.base import NormalizedProviderError, UpstreamApiError
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan


def _route() -> GatewayRoute:
    return GatewayRoute(
        tenant_id="tenant_test",
        model_id="model_test",
        model_alias="model-test",
        upstream_model_name="upstream-test",
        capabilities={"chat": True, "stream": True},
        provider_id="provider_test",
        provider_code="provider-test",
        provider_type="openai_compatible",
        provider_auth_type="bearer",
        provider_name="Provider Test",
        provider_base_url="https://provider.invalid/v1",
    )


def _error(*, category: str, code: str) -> UpstreamApiError:
    return UpstreamApiError(
        NormalizedProviderError(
            code=code,
            message="safe",
            http_status=402,
            retryable=False,
            provider_error_type="provider",
            error_category=category,
        )
    )


def test_quota_error_continues_for_any_enabled_fallback_policy() -> None:
    route = _route()
    enabled = GatewayRoutePlan(
        initial_route=route,
        initial_role="primary",
        fallback_route=route,
        fallback_conditions=("upstream_quota_exceeded",),
    )
    disabled = GatewayRoutePlan(
        initial_route=route,
        initial_role="primary",
        fallback_route=route,
        fallback_conditions=("upstream_5xx",),
    )

    quota = _error(category="quota_exceeded", code="gateway.upstream_quota_exceeded")
    assert _should_fallback(enabled, quota) is True
    # The legacy policy name is retained for compatibility, but an enabled
    # candidate chain must not stop on a different normalized upstream error.
    assert _should_fallback(disabled, quota) is True


def test_quota_error_code_falls_back_when_quality_category_is_other() -> None:
    route = _route()
    plan = GatewayRoutePlan(
        initial_route=route,
        initial_role="primary",
        fallback_route=route,
        fallback_conditions=("upstream_quota_exceeded",),
    )
    # 额度问题不参与模型质量评分，因此归一化类别可能是 other；路由仍按稳定错误码切换。
    quota = _error(category="other", code="gateway.upstream_quota_exceeded")
    assert _should_fallback(plan, quota) is True


def test_auth_error_falls_back_when_all_upstream_failures_are_enabled() -> None:
    route = _route()
    plan = GatewayRoutePlan(
        initial_route=route,
        initial_role="primary",
        fallback_route=route,
        fallback_conditions=("upstream_any_error",),
    )
    auth = _error(category="auth_failed", code="gateway.upstream_auth_failed")
    assert _should_fallback(plan, auth) is True


def test_auth_error_continues_even_for_a_legacy_partial_policy() -> None:
    route = _route()
    plan = GatewayRoutePlan(
        initial_route=route,
        initial_role="primary",
        fallback_route=route,
        fallback_conditions=("upstream_quota_exceeded", "upstream_5xx"),
    )
    auth = _error(category="auth_failed", code="gateway.upstream_auth_failed")
    assert _should_fallback(plan, auth) is True


@pytest.mark.parametrize(
    ("category", "code"),
    [
        ("other", "gateway.upstream_model_not_found"),
        ("other", "gateway.upstream_request_invalid"),
        ("upstream_5xx", "gateway.upstream_error"),
        ("other", "gateway.upstream_resource_not_found"),
    ],
)
def test_all_normalized_upstream_errors_follow_the_candidate_chain(
    category: str,
    code: str,
) -> None:
    route = _route()
    plan = GatewayRoutePlan(
        initial_route=route,
        initial_role="primary",
        fallback_route=route,
        fallback_conditions=("upstream_any_error",),
    )
    assert _should_fallback(plan, _error(category=category, code=code)) is True


def test_network_transport_error_falls_back_but_local_url_error_does_not() -> None:
    route = _route()
    plan = GatewayRoutePlan(
        initial_route=route,
        initial_role="primary",
        fallback_route=route,
        fallback_conditions=("upstream_unavailable",),
    )
    network = UpstreamApiError(
        NormalizedProviderError(
            code="gateway.upstream_unavailable",
            message="safe",
            http_status=None,
            retryable=True,
            provider_error_type="ConnectError",
            error_category="other",
            transport_kind="network",
        )
    )
    local_url = UpstreamApiError(
        NormalizedProviderError(
            code="gateway.upstream_unavailable",
            message="safe",
            http_status=None,
            retryable=True,
            provider_error_type="InvalidURL",
            error_category="other",
            transport_kind="local_configuration",
        )
    )
    assert _should_fallback(plan, network) is True
    assert _should_fallback(plan, local_url) is False
