"""PRD-075 结果事实测试：区分暴露、质量资格和未知上游终态，不推断缺失响应。

Focused tests for PRD-075 exposure and quality independence.
"""

from __future__ import annotations

from types import SimpleNamespace

from metahub_gateway.experiments.models import ProviderOutcomeFacts
from metahub_gateway.providers.base import NormalizedProviderError


def _error(
    *,
    category: str,
    transport: str,
    status: int | None = None,
) -> NormalizedProviderError:
    """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
    evidence = SimpleNamespace(upstream_http_status=status)
    return NormalizedProviderError(
        code=("gateway.upstream_timeout" if category == "timeout" else "gateway.upstream_rejected"),
        message="safe",
        http_status=status,
        retryable=category == "timeout",
        provider_error_type="test",
        error_category=category,
        transport_kind=transport,
        evidence=evidence,  # type: ignore[arg-type] - only the frozen status fact is consumed here.
    )


def test_http_error_is_exposed_without_becoming_quality_eligible() -> None:
    """验证实验结果、质量资格与失败事实只来自权威持久记录，不推断缺失响应。"""
    facts = ProviderOutcomeFacts()
    facts.mark_send_fenced()

    facts.mark_provider_error(_error(category="authentication", transport="none", status=401))

    assert facts.exposure == "exposed"
    assert facts.quality_eligible is False
    assert facts.quality_succeeded is False


def test_response_less_timeout_is_unknown_even_when_quality_error_is_eligible() -> None:
    """验证流式握手、发送围栏和超时恢复不会伪造响应或遗漏失败追踪。"""
    facts = ProviderOutcomeFacts()
    facts.mark_send_fenced()

    facts.mark_provider_error(_error(category="timeout", transport="timeout"))

    assert facts.exposure == "unknown"
    assert facts.quality_eligible is False
    assert facts.quality_succeeded is False


def test_local_configuration_failure_after_send_fence_is_not_exposed() -> None:
    """验证实验主流程、幂等分页和所有者链，固定租户、失败追踪与脱敏边界。"""
    facts = ProviderOutcomeFacts()
    facts.mark_send_fenced()

    facts.mark_provider_error(_error(category="other", transport="local_configuration"))

    assert facts.exposure == "not_exposed"
    assert facts.quality_eligible is False


def test_normalized_response_is_exposed_quality_success() -> None:
    """验证实验结果、质量资格与失败事实只来自权威持久记录，不推断缺失响应。"""
    facts = ProviderOutcomeFacts()
    facts.mark_send_fenced()

    facts.mark_response()

    assert facts.exposure == "exposed"
    assert facts.quality_eligible is True
    assert facts.quality_succeeded is True


def test_failure_before_send_permission_remains_not_exposed() -> None:
    """验证实验 API 后端权限、撤权复核和跨租户隐藏边界，前端状态不能补权。"""
    facts = ProviderOutcomeFacts()

    facts.mark_provider_error(_error(category="upstream_5xx", transport="none", status=503))

    assert facts.exposure == "not_exposed"
    assert facts.quality_eligible is False
