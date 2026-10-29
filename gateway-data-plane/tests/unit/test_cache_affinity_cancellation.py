"""PRD-050 Provider 调用取消路径的状态机测试。

用例在进程内替换数据库写边界，验证主调用、Fallback 和流握手被取消时，attempt、
StreamSession、Selection、预算上下文先形成安全终态，随后仍传播 ``CancelledError``。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from metahub_gateway.api import openai_compatible as api_module
from metahub_gateway.cache_affinity.models import CacheAffinitySelectionHandle
from metahub_gateway.policies.cache import PolicySnapshotLeaseDecision
from metahub_gateway.providers.base import (
    NormalizedProviderError,
    ProviderRequest,
    UpstreamApiError,
)
from metahub_gateway.routing.attempts import RoutingAttempt
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan
from metahub_gateway.session_risk import SessionRiskPreflightResult
from metahub_gateway.streaming.sessions import StreamSession
from metahub_gateway.usage.ledger import RequestLogContext


class _CancellingAdapter:
    """在指定调用阶段抛出 asyncio 取消，模拟 ASGI 请求任务被终止。"""

    async def send_non_stream(self, _request: ProviderRequest) -> None:
        """模拟非流网络等待点收到取消，不返回任何 Provider 正文。"""

        # 直接抛 asyncio 取消以证明业务状态机不会把它误归一成普通 Provider 异常。
        raise asyncio.CancelledError

    async def send_stream(self, _request: ProviderRequest) -> None:
        """模拟流握手等待点收到取消，不建立可交给客户端的响应流。"""

        # 握手取消必须保留 BaseException 语义，同时由调用层完成 Session 与 Selection 终态。
        raise asyncio.CancelledError


class _CalledLocalCancellingAdapter(_CancellingAdapter):
    """Cross the simulated local send fence before cancellation reaches the caller."""

    async def send_non_stream(self, request: ProviderRequest) -> None:
        request.local_provider_call_started = True
        raise asyncio.CancelledError

    async def send_stream(self, request: ProviderRequest) -> None:
        request.local_provider_call_started = True
        raise asyncio.CancelledError


class _UpstreamFailingAdapter:
    """让主 Provider 返回可触发 Fallback 的受控 5xx 分类。"""

    async def send_non_stream(self, _request: ProviderRequest) -> None:
        """抛出不含上游正文的标准化异常，驱动唯一一次备用尝试。"""

        # 固定 5xx 分类只用于触发受控 Fallback，不携带上游响应正文、Header 或 Endpoint。
        raise UpstreamApiError(
            NormalizedProviderError(
                code="gateway.upstream_unavailable",
                message="Upstream provider is unavailable.",
                http_status=503,
                retryable=True,
                provider_error_type="http_5xx",
                error_category="upstream_5xx",
            )
        )


def _route(
    *,
    model_id: str,
    provider_id: str,
    alias_id: str,
    provider_type: str = "openai_compatible",
) -> GatewayRoute:
    """构造不含真实 Endpoint 或 Credential 的租户内测试路由。"""

    return GatewayRoute(
        tenant_id="tenant-cancel-test",
        model_id=model_id,
        model_alias="company-cache",
        upstream_model_name="upstream-model",
        capabilities={"chat": True, "stream": True},
        provider_id=provider_id,
        provider_code=f"code-{provider_id}",
        provider_type=provider_type,
        provider_auth_type="none" if provider_type == "local_endpoint" else "bearer",
        provider_name="Provider Test",
        provider_base_url="https://provider.invalid/v1",
        model_alias_id=alias_id,
    )


def _context() -> RequestLogContext:
    """构造含预算预留标记但不含 Prompt/Key 的请求审计上下文。"""

    return RequestLogContext(
        tenant_id="tenant-cancel-test",
        request_id="req-cancel-test",
        trace_id="trc-cancel-test",
        virtual_key_id="vk-cancel-test",
        endpoint="chat/completions",
        request_model="company-cache",
        budget_reservation_outstanding_cny="0",
    )


def _selection(*, sequence: int, route: GatewayRoute) -> CacheAffinitySelectionHandle:
    """创建 repr-safe Selection 句柄，证明取消只释放当前租户和当前 attempt。"""

    return CacheAffinitySelectionHandle(
        cache_affinity_selection_id=f"cas-cancel-{sequence}",
        tenant_id=route.tenant_id,
        request_id="req-cancel-test",
        trace_id="trc-cancel-test",
        attempt_sequence=sequence,
        routing_attempt_id=f"rta-cancel-{sequence}",
        stream_session_id=None,
        cache_affinity_binding_id=f"cab-cancel-{sequence}",
        target_kind="managed",
        target_generation=1,
        pending_lease_generation=1,
        model_endpoint_id=route.model_id,
        provider_id=route.provider_id,
        endpoint_revision_digest=f"revision-{sequence}",
        provider_credential_id=f"credential-{sequence}",
    )


def _provider_request(route: GatewayRoute, *, sequence: int) -> ProviderRequest:
    """构造带已提交 Selection 的内存请求，敏感字段由 ProviderRequest repr 隐藏。"""

    return ProviderRequest(
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        provider_type=route.provider_type,
        adapter_name="openai_compatible",
        endpoint="chat/completions",
        url="https://provider.invalid/v1/chat/completions",
        headers={"Authorization": "Bearer test-only-key"},
        payload={"model": "upstream-model", "messages": []},
        tenant_id=route.tenant_id,
        model_id=route.model_id,
        cache_affinity_selection=_selection(sequence=sequence, route=route),
    )


def _install_attempt_spies(
    monkeypatch: pytest.MonkeyPatch,
    plan: GatewayRoutePlan,
) -> dict[str, list[Any]]:
    """替换 attempt/Selection/Event 边界并返回调用记录，避免测试写共享数据库。"""

    state: dict[str, list[Any]] = {"attempts": [], "completed": [], "selections": [], "events": []}

    def create_attempt(
        _config: object,
        _plan: GatewayRoutePlan,
        route: GatewayRoute,
        **kwargs: Any,
    ) -> RoutingAttempt:
        """按真实参数构造 started attempt，并保留顺序供后续断言。"""

        attempt = RoutingAttempt(
            routing_attempt_id=f"rta-cancel-{kwargs['attempt_sequence']}",
            tenant_id=route.tenant_id,
            request_id=kwargs["request_id"],
            trace_id=kwargs["trace_id"],
            attempt_sequence=kwargs["attempt_sequence"],
            attempt_role=kwargs["attempt_role"],
            route=route,
        )
        state["attempts"].append(attempt)
        return attempt

    def complete_attempt(_config: object, attempt: RoutingAttempt, **kwargs: Any) -> None:
        """记录 attempt 终态参数，不吞掉或改写待传播的取消异常。"""

        state["completed"].append((attempt, kwargs))

    def finalize_selection(_config: object, selection: object, *, succeeded: bool) -> bool:
        """记录 Selection outcome；取消路径必须恰好提交 failed。"""

        state["selections"].append((selection, succeeded))
        return True

    def build_request(
        _adapter: object,
        route: GatewayRoute,
        _endpoint: str,
        _payload: dict[str, Any],
        _config: object,
        _request: object,
        **kwargs: Any,
    ) -> ProviderRequest:
        """按 attempt_sequence 返回已经提交亲和 Selection 的请求。"""

        return _provider_request(route, sequence=kwargs["attempt_sequence"])

    monkeypatch.setattr(api_module, "create_routing_attempt", create_attempt)
    monkeypatch.setattr(api_module, "safe_complete_routing_attempt", complete_attempt)
    monkeypatch.setattr(api_module, "safe_finalize_cache_affinity_selection", finalize_selection)
    monkeypatch.setattr(api_module, "_provider_request_with_affinity", build_request)
    monkeypatch.setattr(
        api_module,
        "log_fallback_triggered",
        lambda _plan, _attempt, _error: SimpleNamespace(event_type="route.fallback_triggered"),
    )
    monkeypatch.setattr(
        api_module,
        "enqueue_runtime_event",
        lambda _state, event: state["events"].append(event) is None,
    )
    # 参数用于提醒调用方 spies 与当前 plan 成对安装，避免测试错误复用另一租户路由。
    assert plan.initial_route.tenant_id == "tenant-cancel-test"
    return state


@pytest.mark.anyio
async def test_non_stream_primary_cancellation_closes_attempt_and_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主调用取消先写本地非质量失败和预算上下文，再传播 CancelledError。"""

    primary = _route(
        model_id="model-primary", provider_id="provider-primary", alias_id="alias-test"
    )
    plan = GatewayRoutePlan(
        initial_route=primary,
        initial_role="primary",
        cache_affinity_context=SimpleNamespace(),  # type: ignore[arg-type] - 构建边界已由 spy 替换。
    )
    state = _install_attempt_spies(monkeypatch, plan)
    monkeypatch.setattr(api_module, "adapter_for_route", lambda _route: _CancellingAdapter())
    budget = SimpleNamespace(
        initial_estimated_cost_cny=Decimal("1.250000"),
        fallback_estimated_cost_cny=Decimal("0"),
    )

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await api_module._non_stream_response_with_fallback(
            SimpleNamespace(),
            _context(),
            plan,
            endpoint="chat/completions",
            payload={"model": "company-cache", "messages": []},
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            request_started=time.perf_counter(),
            budget_guard_result=budget,
        )

    assert len(state["completed"]) == 1
    _attempt, completion = state["completed"][0]
    assert completion["succeeded"] is False
    assert completion["error"].code == "gateway.client_disconnected"
    assert completion["usage_status"] == "not_applicable"
    assert completion["quality_observation_eligible"] is False
    assert state["selections"] == [
        (_provider_request(primary, sequence=1).cache_affinity_selection, False)
    ]
    audit_context = cancelled.value.request_log_context
    assert audit_context.routing_attempt_count == 1
    assert audit_context.final_routing_attempt_id == "rta-cancel-1"
    assert audit_context.budget_reservation_outstanding_cny == "1.250000"
    assert api_module._cancellation_error_code(cancelled.value) == "gateway.client_disconnected"


@pytest.mark.anyio
async def test_called_local_primary_cancellation_keeps_usage_unavailable_without_quality_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _route(
        model_id="model-local-primary",
        provider_id="provider-local-primary",
        alias_id="alias-local",
        provider_type="local_endpoint",
    )
    plan = GatewayRoutePlan(
        initial_route=primary,
        initial_role="primary",
        cache_affinity_context=SimpleNamespace(),  # type: ignore[arg-type]
    )
    state = _install_attempt_spies(monkeypatch, plan)
    monkeypatch.setattr(
        api_module,
        "adapter_for_route",
        lambda _route: _CalledLocalCancellingAdapter(),
    )

    with pytest.raises(asyncio.CancelledError):
        await api_module._non_stream_response_with_fallback(
            SimpleNamespace(),
            _context(),
            plan,
            endpoint="chat/completions",
            payload={"model": "company-cache", "messages": []},
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            request_started=time.perf_counter(),
            budget_guard_result=None,
        )

    completion = state["completed"][0][1]
    assert completion["usage_status"] == "unavailable"
    assert completion["usage_exposure_possible"] is True
    assert completion["quality_observation_eligible"] is False


@pytest.mark.anyio
async def test_fallback_cancellation_closes_second_attempt_without_quality_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 5xx 后备用调用取消时，两次 attempt 均终结且备用不形成模型质量样本。"""

    primary = _route(
        model_id="model-primary", provider_id="provider-primary", alias_id="alias-test"
    )
    fallback = _route(
        model_id="model-fallback", provider_id="provider-fallback", alias_id="alias-test"
    )
    plan = GatewayRoutePlan(
        initial_route=primary,
        initial_role="primary",
        fallback_route=fallback,
        fallback_conditions=("upstream_5xx",),
        cache_affinity_context=SimpleNamespace(),  # type: ignore[arg-type] - 构建边界已由 spy 替换。
    )
    state = _install_attempt_spies(monkeypatch, plan)
    monkeypatch.setattr(
        api_module,
        "adapter_for_route",
        lambda route: _UpstreamFailingAdapter() if route is primary else _CancellingAdapter(),
    )
    budget = SimpleNamespace(
        initial_estimated_cost_cny=Decimal("1.250000"),
        fallback_estimated_cost_cny=Decimal("2.750000"),
    )

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await api_module._non_stream_response_with_fallback(
            SimpleNamespace(),
            _context(),
            plan,
            endpoint="chat/completions",
            payload={"model": "company-cache", "messages": []},
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            request_started=time.perf_counter(),
            budget_guard_result=budget,
        )

    assert [item[0].attempt_sequence for item in state["completed"]] == [1, 2]
    assert state["completed"][0][1]["quality_observation_eligible"] is True
    fallback_completion = state["completed"][1][1]
    assert fallback_completion["error"].code == "gateway.client_disconnected"
    assert fallback_completion["usage_status"] == "not_applicable"
    assert fallback_completion["quality_observation_eligible"] is False
    assert [item[1] for item in state["selections"]] == [False, False]
    audit_context = cancelled.value.request_log_context
    assert audit_context.fallback_triggered is True
    assert audit_context.routing_attempt_count == 2
    assert audit_context.final_routing_attempt_id == "rta-cancel-2"
    assert audit_context.budget_reservation_outstanding_cny == "4.000000"


@pytest.mark.anyio
async def test_called_local_fallback_cancellation_keeps_usage_unavailable_without_quality_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _route(
        model_id="model-primary",
        provider_id="provider-primary",
        alias_id="alias-test",
    )
    fallback = _route(
        model_id="model-local-fallback",
        provider_id="provider-local-fallback",
        alias_id="alias-test",
        provider_type="local_endpoint",
    )
    plan = GatewayRoutePlan(
        initial_route=primary,
        initial_role="primary",
        fallback_route=fallback,
        fallback_conditions=("upstream_5xx",),
        cache_affinity_context=SimpleNamespace(),  # type: ignore[arg-type]
    )
    state = _install_attempt_spies(monkeypatch, plan)
    monkeypatch.setattr(
        api_module,
        "adapter_for_route",
        lambda route: (
            _UpstreamFailingAdapter()
            if route is primary
            else _CalledLocalCancellingAdapter()
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        await api_module._non_stream_response_with_fallback(
            SimpleNamespace(),
            _context(),
            plan,
            endpoint="chat/completions",
            payload={"model": "company-cache", "messages": []},
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(events=[]))),
            request_started=time.perf_counter(),
            budget_guard_result=None,
        )

    fallback_completion = state["completed"][1][1]
    assert fallback_completion["usage_status"] == "unavailable"
    assert fallback_completion["usage_exposure_possible"] is True
    assert fallback_completion["quality_observation_eligible"] is False


@pytest.mark.anyio
async def test_stream_handshake_cancellation_closes_session_and_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """流握手取消把 starting 会话收口为固定本地失败，并保留取消传播语义。"""

    route = _route(model_id="model-stream", provider_id="provider-stream", alias_id="alias-test")
    context = _context()
    provider_request = _provider_request(route, sequence=1)
    session = StreamSession(
        stream_session_id="str-cancel-1",
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        model_id=route.model_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        inter_token_timeout_ms=1000,
        started_at=datetime.now(UTC),
    )
    finishes: list[tuple[str, str | None, bool]] = []
    selections: list[tuple[object, bool]] = []
    monkeypatch.setattr(api_module, "create_stream_session", lambda *_args, **_kwargs: session)

    def finish_session(
        _config: object,
        _session: StreamSession,
        *,
        status: str,
        error_code: str | None,
        quality_observation_eligible: bool,
    ) -> bool:
        """记录 stream 终态矩阵，并模拟原子提交成功。"""

        finishes.append((status, error_code, quality_observation_eligible))
        return True

    monkeypatch.setattr(api_module, "safe_finish_stream_session", finish_session)
    monkeypatch.setattr(
        api_module,
        "safe_finalize_cache_affinity_selection",
        lambda _config, selection, *, succeeded: selections.append((selection, succeeded)) or True,
    )

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await api_module._stream_response(
            SimpleNamespace(stream_inter_token_timeout_seconds=1),
            context,
            _CancellingAdapter(),
            provider_request,
            time.perf_counter(),
            None,
            client_request=SimpleNamespace(),
            budget_initial_estimated_cost_cny=Decimal("3.500000"),
        )

    assert finishes == [("upstream_failed", "gateway.stream_processing_unavailable", False)]
    assert selections == [(provider_request.cache_affinity_selection, False)]
    audit_context = cancelled.value.request_log_context
    assert audit_context.stream_session_id == "str-cancel-1"
    assert audit_context.stream_status == "upstream_failed"
    assert audit_context.budget_reservation_outstanding_cny == "3.500000"
    assert api_module._cancellation_error_code(cancelled.value) == (
        "gateway.stream_processing_unavailable"
    )


@pytest.mark.anyio
async def test_called_local_stream_cancellation_keeps_usage_unavailable_without_quality_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(
        model_id="model-local-stream",
        provider_id="provider-local-stream",
        alias_id="alias-local-stream",
        provider_type="local_endpoint",
    )
    plan = GatewayRoutePlan(initial_route=route, initial_role="primary")
    context = _context()
    provider_request = _provider_request(route, sequence=1)
    attempt = RoutingAttempt(
        routing_attempt_id="rta-local-stream-cancel",
        tenant_id=route.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        attempt_sequence=1,
        attempt_role="primary",
        route=route,
    )
    session = StreamSession(
        stream_session_id="str-local-stream-cancel",
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        model_id=route.model_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        inter_token_timeout_ms=1000,
        started_at=datetime.now(UTC),
    )
    completions: list[dict[str, Any]] = []
    monkeypatch.setattr(api_module, "create_routing_attempt", lambda *_args, **_kwargs: attempt)
    monkeypatch.setattr(api_module, "create_stream_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(api_module, "safe_finish_stream_session", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        api_module,
        "safe_complete_routing_attempt",
        lambda *_args, **kwargs: completions.append(kwargs),
    )
    monkeypatch.setattr(
        api_module,
        "safe_finalize_cache_affinity_selection",
        lambda *_args, **_kwargs: True,
    )

    with pytest.raises(asyncio.CancelledError):
        await api_module._stream_response(
            SimpleNamespace(stream_inter_token_timeout_seconds=1),
            context,
            _CalledLocalCancellingAdapter(),
            provider_request,
            time.perf_counter(),
            None,
            client_request=SimpleNamespace(),
            route=route,
            route_plan=plan,
        )

    assert len(completions) == 1
    assert completions[0]["usage_status"] == "unavailable"
    assert completions[0]["usage_exposure_possible"] is True
    assert completions[0]["quality_observation_eligible"] is False


@pytest.mark.anyio
async def test_outer_handler_records_and_settles_cancelled_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整编排器必须用内层安全上下文写失败日志、结算预算并释放限流租约。"""

    primary = _route(
        model_id="model-primary",
        provider_id="provider-primary",
        alias_id="alias-test",
    )
    plan = GatewayRoutePlan(
        initial_route=primary,
        initial_role="primary",
        cache_affinity_context=SimpleNamespace(),  # type: ignore[arg-type] - 选择边界由 spy 替换。
    )
    state = _install_attempt_spies(monkeypatch, plan)
    config = SimpleNamespace()
    auth = SimpleNamespace(
        tenant_id="tenant-cancel-test",
        virtual_key_id="vk-cancel-test",
        environment="dev",
        environment_id="env-cancel-test",
        environment_status="active",
        project_id=None,
        application_id=None,
    )
    policy_lease = PolicySnapshotLeaseDecision(
        executable=True,
        snapshot=None,
        snapshot_version=None,
        status="disabled",
        error_code=None,
        acquired_monotonic=0.0,
    )
    # 取消链路不触发 PRD-068 mask；桩仍需显式携带门禁结果，避免绕过真实策略结果合同。
    policy_result = SimpleNamespace(decision="allow", mask_required=False)
    budget = SimpleNamespace(
        decision=SimpleNamespace(),
        initial_estimated_cost_cny=Decimal("1.250000"),
        fallback_estimated_cost_cny=Decimal("0"),
        scope_events=(),
    )
    request = SimpleNamespace(
        headers={"Authorization": "Bearer test-only-virtual-key"},
        # Starlette Request 始终提供请求级 state；测试替身也必须保留该边界，
        # 不能误用应用级 app.state。
        state=SimpleNamespace(),
        app=SimpleNamespace(state=SimpleNamespace()),
    )
    request_logs: list[tuple[RequestLogContext, dict[str, Any]]] = []
    settlements: list[dict[str, Any]] = []

    class _RateLease:
        """统计外层 finally 对并发限流租约的唯一释放。"""

        def __init__(self) -> None:
            """初始化零次释放计数，确保每个测试请求使用独立租约状态。"""

            self.release_calls = 0

        async def release(self) -> None:
            """记录一次释放，不访问 Redis 或其他外部资源。"""

            self.release_calls += 1

    rate_lease = _RateLease()

    async def acquire_policy(*_args: Any, **_kwargs: Any) -> object:
        """返回可执行的固定 Policy lease，绕过本测试无关的缓存依赖。"""

        return policy_lease

    async def read_payload(_request: object) -> dict[str, Any]:
        """返回不含正文内容的最小合法 Chat 请求。"""

        return {"model": "company-cache", "messages": []}

    async def acquire_rate_limit(*_args: Any, **_kwargs: Any) -> object:
        """返回可计数的并发租约，供取消 finally 释放。"""

        return rate_lease

    async def preflight_session(*_args: Any, **_kwargs: Any) -> SessionRiskPreflightResult:
        """取消专项不建立会话身份，但仍显式经过 PRD-070 读取正文前门禁。"""

        # 固定 not_applicable 证明调用已发生，同时不引入该专项无关的 Session Store。
        return SessionRiskPreflightResult(status="not_applicable", identity=None)

    prepared_output_scan = SimpleNamespace(
        output_safety_scan_id="oss_" + "c" * 32,
        tenant_id="tenant-cancel-test",
        request_id="req-cancel-test",
        trace_id="trc-cancel-test",
        scan_status="not_configured",
    )

    monkeypatch.setattr(api_module, "_config", lambda _request: config)
    monkeypatch.setattr(api_module, "_request_id", lambda _request: "req-cancel-test")
    monkeypatch.setattr(api_module, "_trace_id", lambda _request: "trc-cancel-test")
    monkeypatch.setattr(api_module, "authenticate_virtual_key", lambda *_args: auth)
    monkeypatch.setattr(api_module, "_ensure_environment_active", lambda _auth: None)
    monkeypatch.setattr(api_module, "_acquire_policy_snapshot_lease", acquire_policy)
    monkeypatch.setattr(api_module, "preflight_session_risk", preflight_session)
    monkeypatch.setattr(
        api_module,
        "prepare_output_safety_scan",
        lambda *_args, **_kwargs: prepared_output_scan,
    )
    monkeypatch.setattr(
        api_module, "_partial_request_log_context", lambda *_args, **_kwargs: _context()
    )
    monkeypatch.setattr(api_module, "_context_with_policy_lease", lambda context, _lease: context)
    monkeypatch.setattr(api_module, "claim_gateway_request", lambda *_args: None)
    monkeypatch.setattr(api_module, "_read_json_body", read_payload)
    monkeypatch.setattr(api_module, "_validate_payload", lambda *_args: None)
    monkeypatch.setattr(
        api_module, "validate_requested_model_authority", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(api_module, "evaluate_policy_snapshot", lambda *_args: policy_result)
    monkeypatch.setattr(
        api_module, "apply_policy_execution_result", lambda context, _result: context
    )
    monkeypatch.setattr(api_module, "policy_execution_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api_module, "resolve_model_route_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(api_module, "_validate_route_capability", lambda *_args: None)
    monkeypatch.setattr(
        api_module, "_filter_fallback_capability", lambda selected, *_args: selected
    )
    monkeypatch.setattr(
        api_module, "enforce_dlp_guard", lambda *_args, **_kwargs: SimpleNamespace()
    )
    monkeypatch.setattr(api_module, "apply_dlp_decision", lambda context, _decision: context)
    monkeypatch.setattr(api_module, "enforce_virtual_key_rate_limit", acquire_rate_limit)
    monkeypatch.setattr(
        api_module,
        "prepare_response_cache",
        lambda *_args, **_kwargs: api_module.PreparedResponseCache(status="disabled"),
    )
    monkeypatch.setattr(api_module, "enforce_budget_guard", lambda *_args, **_kwargs: budget)
    monkeypatch.setattr(api_module, "apply_budget_decision", lambda context, _decision: context)
    monkeypatch.setattr(
        api_module,
        "enforce_quota_guard",
        lambda *_args, **_kwargs: api_module.QuotaGuardResult(None, 0, None),
    )
    monkeypatch.setattr(api_module, "_log_request_received", lambda *_args: None)
    monkeypatch.setattr(api_module, "adapter_for_route", lambda _route: _CancellingAdapter())
    monkeypatch.setattr(
        api_module,
        "safe_record_request_log",
        lambda _config, context, **kwargs: request_logs.append((context, kwargs)),
    )
    monkeypatch.setattr(
        api_module,
        "finalize_request_budget_reservations",
        lambda _config, **kwargs: settlements.append(kwargs),
    )

    with pytest.raises(asyncio.CancelledError):
        await api_module._handle_gateway_request(
            request,  # type: ignore[arg-type] - 只提供本编排路径实际读取的 Request 属性。
            endpoint="chat/completions",
            request_kind="chat",
        )

    assert len(state["completed"]) == 1
    assert state["completed"][0][1]["error"].code == "gateway.client_disconnected"
    assert len(request_logs) == 1
    logged_context, logged_terminal = request_logs[0]
    assert logged_terminal["status"] == "failed"
    assert logged_terminal["error_code"] == "gateway.client_disconnected"
    assert logged_context.final_routing_attempt_id == "rta-cancel-1"
    assert logged_context.budget_reservation_outstanding_cny == "1.250000"
    assert settlements == [
        {
            "tenant_id": "tenant-cancel-test",
            "request_id": "req-cancel-test",
            "trace_id": "trc-cancel-test",
            "outstanding_cost_cny": Decimal("1.250000"),
        }
    ]
    assert rate_lease.release_calls == 1
