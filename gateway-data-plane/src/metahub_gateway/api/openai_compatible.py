"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义。
Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/api/openai_compatible.py
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator, Coroutine, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from metahub_shared.intent_security import (
    DETECTOR_VERSION,
    RISK_SCORE_SCALE,
    RULESET_VERSION,
    IntentClassification,
)
from metahub_shared.output_safety import (
    BoundedSseEventFramer,
    OutputSafetyError,
    OutputScanDecision,
    SlidingOutputScanner,
    SseFrame,
    scan_full_output,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_error_map import (
    ProviderErrorMapLease,
    builtin_provider_error_map_lease,
)
from metahub_shared.runtime.request_id import resolve_request_id, resolve_trace_id
from metahub_shared.runtime.webhooks import WebhookEvent
from metahub_shared.session_risk import (
    SessionRiskContractError,
    build_turn_risk_assessment,
    detect_tool_inducement,
)
from starlette.datastructures import Headers

from metahub_gateway.auth.virtual_key import GatewayAuthContext, authenticate_virtual_key
from metahub_gateway.batch import (
    RealtimeGatewayLease,
    TrustedGatewayInvocation,
    get_batch_gateway_gate,
)
from metahub_gateway.budgets import (
    BudgetExhaustedError,
    BudgetGuardResult,
    apply_budget_decision,
    enforce_budget_guard,
    finalize_request_budget_reservations,
)
from metahub_gateway.cache_affinity.keypool import select_provider_access
from metahub_gateway.cache_affinity.lifecycle import safe_finalize_cache_affinity_selection
from metahub_gateway.cache_affinity.models import (
    CacheAffinityRouteContext,
    CacheAffinitySelectionHandle,
)
from metahub_gateway.dlp import (
    DlpBlockedError,
    apply_dlp_decision,
    enforce_dlp_guard,
    extract_session_user_texts,
)
from metahub_gateway.dlp.models import DlpDecision
from metahub_gateway.events import enqueue_runtime_event
from metahub_gateway.intent_security import (
    IntentSecurityError,
    apply_intent_decision,
    enforce_intent_guard,
    enforce_session_intent_guard,
)
from metahub_gateway.output_safety import (
    OutputSafetyFinalization,
    OutputSafetyTerminalState,
    PreparedOutputSafetyScan,
    finalize_output_safety_scan,
    output_safety_event_to_webhook,
    prepare_output_safety_scan,
)
from metahub_gateway.policies import (
    PolicySnapshotLeaseDecision,
    apply_policy_execution_result,
    apply_policy_mask_payload,
    evaluate_policy_snapshot,
    policy_execution_error,
)
from metahub_gateway.providers.base import (
    NormalizedProviderError,
    NormalizedResponse,
    NormalizedUsage,
    ProviderAdapter,
    ProviderRequest,
    UpstreamApiError,
    is_quality_observation_eligible_error,
    is_upstream_cost_exposure_possible,
    quality_observation_error,
)
from metahub_gateway.providers.factory import adapter_for_route
from metahub_gateway.providers.openai_compatible import estimate_stream_usage
from metahub_gateway.rate_limits import RateLimitLease, enforce_virtual_key_rate_limit
from metahub_gateway.routing.attempts import (
    RoutingAttempt,
    create_routing_attempt,
    log_fallback_triggered,
    safe_complete_routing_attempt,
)
from metahub_gateway.routing.models import (
    GatewayRoute,
    GatewayRoutePlan,
    resolve_model_route_plan,
    resolve_policy_target_route,
    validate_requested_model_authority,
)
from metahub_gateway.session_risk import (
    SessionRiskPreflightResult,
    SessionRiskRequestIdentity,
    SessionRiskTurnResult,
    preflight_session_risk,
    record_session_risk_turn,
    resolve_public_session_risk_candidates,
)
from metahub_gateway.streaming import (
    LOCAL_STREAM_PROCESSING_ERROR_CODE,
    UPSTREAM_STREAM_FAILURE_CODE,
    build_stream_timeout_event,
    create_stream_session,
    mark_stream_session_active,
    safe_finish_stream_session,
)
from metahub_gateway.usage.ledger import (
    FinOpsLedgerCommitted,
    RequestLogContext,
    UsageLedgerWriteResult,
    claim_gateway_request,
    safe_record_request_log,
    safe_record_usage_ledger,
)

logger = logging.getLogger("metahub.gateway")
router = APIRouter(prefix="/v1", tags=["openai-compatible"])

# ASGI 取消通常表示客户端已放弃尚未返回的 HTTP 请求；该固定码只进入安全审计事实，
# CancelledError 本身仍原样传播给服务器，不得转换成一个虚假的普通 JSON 响应。
_REQUEST_CANCELLATION_ERROR_CODE = "gateway.client_disconnected"
# DLP 已阻断时严禁再调用分类器；该不可变零风险摘要只为组合当前 turn 的 DLP 信号提供固定版本证据。
_NO_INTENT_RISK = IntentClassification(
    risk_type="none",
    risk_score=0,
    risk_score_scale=RISK_SCORE_SCALE,
    action="allow",
    reason_code="no_risk_detected",
    detector_version=DETECTOR_VERSION,
    ruleset_version=RULESET_VERSION,
    input_code_points=0,
)
# 只允许稳定公开错误映射进入 RequestLog，未知内部异常统一折叠为证据不可用，禁止落底层消息。
_SESSION_RISK_ERROR_REASONS = {
    "gateway.session_risk_unavailable": "store_unavailable",
    "gateway.session_risk_evidence_unavailable": "evidence_unavailable",
    "gateway.session_risk_turn_conflict": "turn_conflict",
    "gateway.session_risk_hmac_version_unavailable": "hmac_version_unavailable",
    "gateway.session_risk_frozen": "session_frozen",
}
# 该头只服务 PRD-064 真实探测：请求声明租户，Gateway 在 Virtual Key 鉴权后核对并回送证明。
_CONTRACT_PROBE_TENANT_HEADER = "X-MetaHub-Contract-Tenant-ID"


def _contract_probe_tenant_headers(
    request: object | None,
    tenant_id: str,
) -> dict[str, str]:
    """为已通过鉴权态租户核对的 probe 响应构造最小证明头。

    参数：客户端请求和 Virtual Key 的可信 tenant。返回：仅在声明精确匹配时回送同名头。
    普通 OpenAI-compatible 请求不增加租户响应字段，避免改变既有客户端表面。
    """

    # 只有单一请求声明与 Virtual Key 鉴权得到的可信租户完全一致时，才返回 probe 证明头。
    declarations = _contract_probe_tenant_declarations(request)
    # 重复头可能被代理合并为歧义值，必须要求 wire 上恰好一个且与可信租户相等。
    if declarations != (tenant_id,):
        # 缺失或不匹配必须返回空头集合，避免把可信租户身份泄露给普通调用方。
        return {}
    # 精确匹配时只回送单个租户证明头，不扩展普通 OpenAI-compatible 响应正文。
    return {_CONTRACT_PROBE_TENANT_HEADER: tenant_id}


def _contract_probe_tenant_declarations(request: object | None) -> tuple[str, ...]:
    """读取可选合同探针租户声明，并保留重复 Header 的失败关闭语义。

    参数：真实 Starlette Request 或内部单测使用的最小请求替身。返回：按 wire 顺序保留的
    声明值；没有 ``headers`` 的最小替身等同于未声明。真实请求优先使用 ``getlist``，普通
    Mapping 仅用于内部兼容，仍会合并大小写同名键并保留列表值，不能把重复声明择一通过。
    """

    if request is None:
        return ()
    headers = getattr(request, "headers", None)
    # 既有流式单测只构造断连能力而不模拟 Header；可选 probe 声明缺失时不应破坏正常请求。
    if headers is None:
        return ()
    getlist = getattr(headers, "getlist", None)
    # Starlette Headers 在真实 HTTP 路径保留重复行，必须优先使用该接口而不是普通 ``get``。
    if callable(getlist):
        return tuple(getlist(_CONTRACT_PROBE_TENANT_HEADER))
    if not isinstance(headers, Mapping):
        # 未知 Header 容器不能被解释成可信声明；哨兵值让调用方按不匹配路径失败关闭。
        return ("",)
    declarations: list[str] = []
    expected = _CONTRACT_PROBE_TENANT_HEADER.lower()
    for name, value in headers.items():
        if not isinstance(name, str) or name.lower() != expected:
            continue
        # Mapping 可能用列表表达重复行；逐项保留后由上层的“恰好一个”约束统一拒绝。
        values = value if isinstance(value, (list, tuple)) else (value,)
        declarations.extend(item if isinstance(item, str) else "" for item in values)
    return tuple(declarations)


@router.post("/adapter-contract-probes/identity")
async def verify_adapter_contract_probe_identity(request: Request) -> JSONResponse:
    """在真实模型探测前证明 Virtual Key 的实际租户归属。

    参数：只从 Authorization 恢复身份，并从专用请求头读取期望租户。返回：不含 tenant_id
    正文的最小成功事实。租户不匹配沿用统一鉴权错误，避免形成租户存在性枚举接口。
    """

    auth_context = authenticate_virtual_key(_config(request), request.headers.get("Authorization"))
    _ensure_environment_active(auth_context)
    declared_tenants = tuple(request.headers.getlist(_CONTRACT_PROBE_TENANT_HEADER))
    if (
        len(declared_tenants) != 1
        or not declared_tenants[0].isascii()
        or not hmac.compare_digest(declared_tenants[0], auth_context.tenant_id)
    ):
        # 重复/合并值不能由框架替调用方选一个；错误正文也不能指出声明租户或真实租户。
        raise ApiError("gateway.auth_invalid")
    request_id = _request_id(request)
    trace_id = _trace_id(request)
    return JSONResponse(
        content={
            "status": "ok",
            "service_name": _config(request).service_name,
            "request_id": request_id,
            "trace_id": trace_id,
        },
        headers={
            _CONTRACT_PROBE_TENANT_HEADER: auth_context.tenant_id,
            "Cache-Control": "no-store",
        },
    )


def _enqueue_finops_ledger_event(
    app_state: object,
    committed: FinOpsLedgerCommitted | None,
    context: RequestLogContext,
) -> bool:
    """仅为本次新建且已 commit 的正式分录非阻塞入队一次 canonical Webhook。

    参数：Gateway app state、账本提交结果和安全请求上下文。返回：是否成功进入内存队列。
    约束：spool-only 与 existing 都不得发送；构造或队列故障不能回滚财务事实或破坏响应。
    """

    if committed is None or not committed.created:
        return False
    try:
        event = committed.to_webhook_event()
        return enqueue_runtime_event(app_state, event)
    except Exception as exc:
        # 异常正文可能含底层对象表示，只记录类型和固定错误码，不输出 payload 或数据库值。
        logger.warning(
            "finops.ledger_event_enqueue_failed",
            extra={
                "event": "finops.ledger_event_enqueue_failed",
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "tenant_id": context.tenant_id,
                "object_id": committed.ledger_entry_id,
                "error_code": "webhook.delivery_unavailable",
                "payload": {"exception_type": exc.__class__.__name__},
            },
        )
        return False


def _enqueue_budget_scope_events(
    app_state: object,
    events: tuple[WebhookEvent, ...],
) -> int:
    """在预算事务提交后机会性投递稳定 Scope 越限事件。

    参数：Gateway 状态和 Guard 返回的不可变事件 tuple。返回成功入队数量。队列缺失、满载
    或意外异常都不能回滚预算事实、改变 402 错误码或阻止 alert_only 请求进入 Provider。
    """

    accepted = 0
    for event in events:
        try:
            accepted += int(enqueue_runtime_event(app_state, event))
        except Exception as exc:
            # 只记录稳定身份和异常类型，不输出可能扩展的事件 payload。
            logger.warning(
                "budget.scope_event_enqueue_failed",
                extra={
                    "event": "budget.scope_event_enqueue_failed",
                    "request_id": event.payload.get("request_id"),
                    "trace_id": event.trace_id,
                    "tenant_id": event.tenant_id,
                    "object_id": event.event_id,
                    "error_code": "webhook.delivery_unavailable",
                    "payload": {"exception_type": exc.__class__.__name__},
                },
            )
    return accepted


@router.post("/chat/completions")
async def chat_completions(request: Request):
    """OpenAI Chat Completions 兼容入口。

    这里只负责 M0/M1 热路径的最小编排：鉴权、请求校验、模型别名解析、
    Provider Adapter 调用和调用日志闭环。复杂治理能力会按后续 PRD 分阶段接入。
    """

    return await _handle_gateway_request(request, endpoint="chat/completions", request_kind="chat")


@router.post("/embeddings")
async def embeddings(request: Request) -> JSONResponse:
    """实现 网关数据面 的 embeddings 逻辑，保持职责、输入边界和错误码口径清晰。

    边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
    """

    response = await _handle_gateway_request(
        request, endpoint="embeddings", request_kind="embeddings"
    )
    if isinstance(response, StreamingResponse):
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("gateway.stream_not_supported")
    return response


async def _handle_gateway_request(
    request: Request,
    *,
    endpoint: str,
    request_kind: str,
    audit_endpoint: str | None = None,
    trusted_invocation: TrustedGatewayInvocation | None = None,
) -> JSONResponse | StreamingResponse:
    """处理一次网关请求并保证成功/失败都尽量写入调用日志。

    request_model 单独保存，是为了在路由失败或能力校验失败时仍能把用户请求的
    模型名写入 request_logs；日志上下文只包含安全元数据，不携带 Prompt/Input。

    ``trusted_invocation`` 只能由已消费单次 grant 的内部路由构造；其来源枚举确保
    Enterprise Chat 与 Batch 的身份、正文和结算事实不能半组合。``endpoint`` 始终是发给
    Provider Adapter 的相对路径，
    ``audit_endpoint`` 只覆盖 RequestLog/接收日志中的入口身份，避免把内部 URL 发往上游。
    """

    started = time.perf_counter()
    config = _config(request)
    auth_context: GatewayAuthContext | None = None
    route: GatewayRoute | None = None
    route_plan: GatewayRoutePlan | None = None
    request_log_context: RequestLogContext | None = None
    request_model: str | None = None
    rate_limit_lease: RateLimitLease | None = None
    policy_lease: PolicySnapshotLeaseDecision | None = None
    budget_guard_result: BudgetGuardResult | None = None
    realtime_gateway_lease: RealtimeGatewayLease | None = None
    request_claimed = False
    log_endpoint = audit_endpoint or endpoint

    try:
        # trusted invocation 必须是后端构造的冻结 DTO，任意松散对象都不能绕过公开鉴权。
        if trusted_invocation is not None and not isinstance(
            trusted_invocation, TrustedGatewayInvocation
        ):
            # 类型不受信时统一按鉴权失败处理，不能读取其伪造的 tenant 或 payload 字段。
            raise ApiError("gateway.auth_invalid")
        # 内部身份与 Authorization 不能同时存在，避免两个租户 owner 在热路径发生歧义。
        if trusted_invocation is not None and request.headers.get("Authorization") is not None:
            # 内部调用不接受 Virtual Key Bearer 冒充，避免出现两个相互冲突的身份来源。
            raise ApiError(
                "batch.execution_invalid"
                if trusted_invocation.source == "batch"
                else "chat.execution_invalid"
            )
        trusted_batch = (
            trusted_invocation.batch
            if trusted_invocation is not None and trusted_invocation.source == "batch"
            else None
        )
        auth_context = (
            trusted_invocation.auth_context
            if trusted_invocation is not None
            else authenticate_virtual_key(config, request.headers.get("Authorization"))
        )
        # Batch already owns a dedicated slot. Other traffic is admitted to the realtime
        # gate only after its backend identity is trusted, so unauthenticated requests cannot
        # suppress scheduler work or extend the realtime cooldown.
        if trusted_invocation is None or trusted_invocation.source != "batch":
            try:
                realtime_gateway_lease = await get_batch_gateway_gate(
                    request.app.state,
                    batch_concurrency=getattr(config, "batch_gateway_concurrency", 1),
                    realtime_cooldown_milliseconds=(
                        getattr(config, "batch_realtime_cooldown_milliseconds", 250)
                    ),
                ).enter_realtime()
            # admission gate 无法证明状态时必须在路由和 Provider 前失败关闭。
            except (RuntimeError, ValueError) as exc:
                raise ApiError("gateway.request_log_unavailable") from exc
        # tenant_id 只取自已认证 Virtual Key；正文尚未读取，因此客户端不能影响租约归属。
        # 当前请求只获取一次，主调用、Fallback 与流式握手都复用 Request.state 中的同一引用。
        await _acquire_provider_error_map_lease(request, auth_context.tenant_id)
        # 先完成环境状态判断，但延迟到 claim 后抛错：所有已认证 HTTP 请求都必须在读取
        # JSON 前冻结快照/version/status，禁用环境失败也不能绕过不可变 request_id 占位。
        environment_error: ApiError | None = None
        try:
            _ensure_environment_active(auth_context)
        # 环境错误先暂存到审计占位之后再抛出，确保已认证失败请求也冻结同一快照版本。
        except ApiError as exc:
            environment_error = exc
        policy_lease = await _acquire_policy_snapshot_lease(request, auth_context.tenant_id)
        request_log_context = _partial_request_log_context(
            request,
            auth_context,
            route,
            endpoint=log_endpoint,
            request_model=None,
        )
        # 已认证请求若无法构造租户日志上下文就必须失败关闭，绝不能绕过 claim 继续调用上游。
        if request_log_context is None:  # pragma: no cover - 已认证上下文按类型不可能为空。
            raise ApiError("gateway.request_log_unavailable")
        request_log_context = _context_with_policy_lease(request_log_context, policy_lease)
        if trusted_invocation is not None and trusted_invocation.batch is not None:
            batch = trusted_invocation.batch
            if (
                batch.request_id != request_log_context.request_id
                or batch.trace_id != request_log_context.trace_id
            ):
                # Batch owner 与当前不可变请求链路不一致时不能继续执行或写错归属。
                raise ApiError("batch.execution_invalid")
            request_log_context = replace(
                request_log_context,
                batch_job_id=batch.batch_job_id,
                batch_task_id=batch.batch_task_id,
                batch_attempt_id=batch.batch_attempt_id,
                batch_route_plan_hash=batch.route_plan_hash,
            )
        claim_gateway_request(config, request_log_context)
        request_claimed = True
        declared_contract_tenants = _contract_probe_tenant_declarations(request)
        # 声明头只用于合同探测；重复行与逗号合并值都不能由框架替调用方选择其一。
        if len(declared_contract_tenants) > 1 or (
            len(declared_contract_tenants) == 1
            and declared_contract_tenants[0] != auth_context.tenant_id
        ):
            # 沿用不暴露租户存在性的统一鉴权错误，同时由 request_logs 记录真实租户和链路。
            raise ApiError("gateway.auth_invalid")
        # 只有不可变请求占位成功后才回放环境错误，使异常请求仍具备租户级追踪事实。
        if environment_error is not None:
            raise environment_error
        # 不可执行 lease 表示快照已过期或同步不可用，必须在读取正文和路由前失败关闭。
        if not policy_lease.executable:
            raise ApiError(policy_lease.error_code or "gateway.policy_snapshot_unavailable")

        # 风险身份只允许来自公开规范 Header 或已消费 grant，且冻结 overlay
        # 必须在读取 Prompt 前点查。
        # Embeddings 不建立 session score，但仍需执行同 Key 的人工冻结 overlay，防止换接口绕过。
        session_risk_candidates = (
            trusted_invocation.session_risk_candidates
            if trusted_invocation is not None
            else (
                resolve_public_session_risk_candidates(request, config, auth_context)
                if request_kind == "chat"
                else ()
            )
        )
        try:
            session_risk_preflight = await preflight_session_risk(
                config,
                request.app.state,
                auth_context,
                session_risk_candidates,
            )
        # 预检异常必须在读取 Prompt 前绑定固定日志摘要，禁止后续路由掩盖安全故障。
        except ApiError as exc:
            # 预检已在 Prompt 前失败；只把固定状态和 scope 类型附到 RequestLog，
            # 绝不记录 Header 摘要。
            request_log_context = _context_with_session_risk_error(
                request_log_context,
                exc,
                identity=(session_risk_candidates[0] if session_risk_candidates else None),
            )
            exc.request_log_context = request_log_context
            raise
        request_log_context = _context_with_session_risk_preflight(
            request_log_context,
            session_risk_preflight,
            request_kind=request_kind,
        )

        # 内部入口已经对 grant/history 做过 exact-set 和有界校验；这里复制顶层映射，
        # 后续治理只能看到固定 model/messages/stream，不能读回 grant 或客户端身份字段。
        payload = (
            dict(trusted_invocation.payload)
            if trusted_invocation is not None
            else await _read_json_body(request)
        )
        request_model = _model_alias(payload)
        # 只更新安全模型元数据，不能重建上下文而丢失 claim 已冻结的快照版本和状态。
        request_log_context = replace(request_log_context, request_model=request_model)
        _validate_payload(request_kind, payload)
        validate_requested_model_authority(
            config,
            tenant_id=auth_context.tenant_id,
            model_alias=request_model,
            environment=auth_context.environment,
            environment_id=auth_context.environment_id,
        )
        policy_result = evaluate_policy_snapshot(
            config,
            policy_lease,
            auth_context,
            request_model,
        )
        request_log_context = apply_policy_execution_result(request_log_context, policy_result)
        policy_error = policy_execution_error(
            policy_result,
            tenant_id=auth_context.tenant_id,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
            snapshot_version=policy_lease.snapshot_version,
        )
        # 策略拒绝、上下文缺失或超时都使用冻结版本的统一错误，禁止继续解析 Provider 路由。
        if policy_error is not None:
            raise policy_error

        # mask 只能在最终策略允许继续后、解析任何路由前执行；返回新 payload 保证内部 Chat
        # 调用方传入的 trusted invocation 和客户端 JSON 对象都不会被原地修改。
        request_log_context, payload = apply_policy_mask_payload(
            config,
            request_log_context,
            policy_result,
            payload,
            request_kind=request_kind,
        )

        # route_to 仅解析策略指定的租户内目标，不继承客户端别名原有的运行时 Fallback。
        if policy_result.decision == "route_to":
            route = resolve_policy_target_route(
                config,
                tenant_id=auth_context.tenant_id,
                target_model_id=policy_result.target_model_id or "",
                requested_model=request_model,
                environment=auth_context.environment,
                environment_id=auth_context.environment_id,
                request_id=_request_id(request),
                trace_id=_trace_id(request),
                virtual_key_id=auth_context.virtual_key_id,
                policy_id=policy_result.policy_id,
                snapshot_version=policy_lease.snapshot_version,
            )
            # route_to 是单候选安全重路由，不能继承原 alias 的运行时 Fallback。
            # routing_attempts 的角色合同仍只有 primary/fallback；Policy 来源由 route 的
            # routing_reason 与 request_logs 摘要表达，不能发明会被数据库拒绝的新枚举。
            route_plan = GatewayRoutePlan(initial_route=route, initial_role="primary")
        else:
            trusted_routing_kwargs = (
                {"trusted_token_estimate": trusted_batch.token_estimate}
                if trusted_batch is not None
                else {}
            )
            route_plan = resolve_model_route_plan(
                config,
                tenant_id=auth_context.tenant_id,
                model_alias=request_model,
                environment=auth_context.environment,
                environment_id=auth_context.environment_id,
                request_id=_request_id(request),
                trace_id=_trace_id(request),
                virtual_key_id=auth_context.virtual_key_id,
                project_id=auth_context.project_id,
                request_kind=request_kind,
                payload=payload,
                **trusted_routing_kwargs,
            )
            route = route_plan.initial_route
        if trusted_batch is not None:
            route_plan = replace(route_plan, token_estimate=trusted_batch.token_estimate)
        # RoutingPolicy 运行事件只引用已提交 Decision；有界队列满或未就绪不能反压主请求。
        if route_plan.policy_selected_event is not None:
            enqueue_runtime_event(request.app.state, route_plan.policy_selected_event)
        request_log_context = _context_for_route(request_log_context, route)
        _validate_route_capability(request_kind, payload, route)
        route_plan = _filter_fallback_capability(route_plan, request_kind, payload)
        try:
            dlp_decision = enforce_dlp_guard(
                config,
                auth_context,
                route,
                payload,
                endpoint=endpoint,
                request_kind=request_kind,
                request_id=_request_id(request),
                trace_id=_trace_id(request),
            )
        # DLP 是更早的内容安全闸门，其阻断错误必须优先于后续会话风险故障。
        except DlpBlockedError as exc:
            # DLP 已经形成更早、更具体的安全阻断；可信 session 仍提交本轮
            # DLP 摘要，但零次调用意图分类器。
            request_log_context = apply_dlp_decision(request_log_context, exc.dlp_decision)
            if session_risk_preflight.identity is not None:
                try:
                    request_log_context = await _record_trusted_session_turn(
                        config,
                        request.app.state,
                        request_log_context,
                        session_risk_preflight.identity,
                        payload,
                        dlp_decision=exc.dlp_decision,
                        current_turn_intent=_NO_INTENT_RISK,
                        input_truncated=False,
                        detect_tools=False,
                    )
                # 会话累计失败只补充固定日志状态，不得覆盖已经成立的 DLP 对外错误。
                except ApiError as session_error:
                    # 会话风险故障会留在 RequestLog，但绝不能覆盖已经成立的 DLP 公开错误码。
                    request_log_context = getattr(
                        session_error,
                        "request_log_context",
                        _context_with_session_risk_error(
                            request_log_context,
                            session_error,
                            identity=session_risk_preflight.identity,
                        ),
                    )
            exc.request_log_context = request_log_context
            raise
        request_log_context = apply_dlp_decision(request_log_context, dlp_decision)
        # 语义闸门严格位于 DLP 之后、Rate Limit 与 Budget 之前：DLP 已阻断时不会分类，
        # 强规则结果未提交时不会误报 blocked，异步队列故障则保留 skipped 后继续主链路。
        if session_risk_preflight.identity is None:
            request_log_context = enforce_intent_guard(
                config,
                request.app.state,
                request_log_context,
                payload,
                request_kind=request_kind,
            )
        else:
            try:
                request_log_context, session_classification = enforce_session_intent_guard(
                    config,
                    request.app.state,
                    request_log_context,
                    payload,
                )
            # Intent 失败或阻断已经形成确定语义，后续会话累计只能附加安全摘要。
            except IntentSecurityError as exc:
                # strong 阻断已持久化时仍累计 current-turn light 摘要；
                # 分类或结果存储失败时没有可伪造的摘要。
                request_log_context = apply_intent_decision(
                    request_log_context,
                    exc.intent_risk_decision,
                )
                # 只有分类器返回可信 current-turn 摘要时才允许进入会话累计，禁止伪造风险信号。
                if exc.session_classification is not None:
                    try:
                        request_log_context = await _record_trusted_session_turn(
                            config,
                            request.app.state,
                            request_log_context,
                            session_risk_preflight.identity,
                            payload,
                            dlp_decision=dlp_decision,
                            current_turn_intent=exc.session_classification.current_turn,
                            input_truncated=exc.session_classification.input_truncated,
                            detect_tools=True,
                        )
                    # 累计故障必须保持 Intent 原错误优先级，同时冻结可追踪的失败摘要。
                    except ApiError as session_error:
                        # Intent 已阻断时保持原错误优先级，同时保留会话风险固定失败/冻结摘要。
                        request_log_context = getattr(
                            session_error,
                            "request_log_context",
                            _context_with_session_risk_error(
                                request_log_context,
                                session_error,
                                identity=session_risk_preflight.identity,
                            ),
                        )
                exc.request_log_context = request_log_context
                raise
            request_log_context = await _record_trusted_session_turn(
                config,
                request.app.state,
                request_log_context,
                session_risk_preflight.identity,
                payload,
                dlp_decision=dlp_decision,
                current_turn_intent=session_classification.current_turn,
                input_truncated=session_classification.input_truncated,
                detect_tools=True,
            )
        rate_limit_lease = await enforce_virtual_key_rate_limit(
            config,
            request.app.state,
            auth_context,
            route,
            payload,
            endpoint=endpoint,
            request_kind=request_kind,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
            batch_lane=(trusted_invocation is not None and trusted_invocation.source == "batch"),
            batch_token_estimate=(
                trusted_batch.token_estimate if trusted_batch is not None else None
            ),
        )
        budget_guard_result = enforce_budget_guard(
            config,
            auth_context,
            route,
            payload,
            endpoint=endpoint,
            request_kind=request_kind,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
            # 流式请求不会执行 PRD-036 Fallback，因此预算也只能预留实际初始候选。
            fallback_route=(
                route_plan.fallback_route
                if not (request_kind == "chat" and payload.get("stream") is True)
                else None
            ),
            routing_token_estimate=route_plan.token_estimate,
            routing_price_as_of=route_plan.routing_as_of,
            batch_context=trusted_batch,
            route_plan=route_plan,
        )
        if budget_guard_result is not None and budget_guard_result.scope_events:
            # 事件事实已由 Guard 提交；入队失败不能反压 Provider 或改变 alert_only 准入。
            _enqueue_budget_scope_events(request.app.state, budget_guard_result.scope_events)
        request_log_context = apply_budget_decision(
            request_log_context,
            budget_guard_result.decision if budget_guard_result is not None else None,
        )
        if budget_guard_result is not None and not (
            trusted_invocation is not None and trusted_invocation.source == "batch"
        ):
            # Guard 已创建规则级预留；Provider 尚未调用，默认未知分项为 0，网络尝试后再提升。
            request_log_context = replace(
                request_log_context,
                budget_reservation_outstanding_cny="0",
            )
        _log_request_received(
            request,
            auth_context,
            log_endpoint,
            request_model,
            route,
            payload.get("stream") is True,
        )

        if request_kind == "chat" and payload.get("stream") is True:
            # PRD-036 明确只处理非流式请求；流式握手和输出阶段均沿用单候选路径。
            adapter = adapter_for_route(route)
            streaming_response = await _stream_response(
                config,
                request_log_context,
                adapter,
                None,
                started,
                rate_limit_lease,
                client_request=request,
                route=route,
                endpoint=endpoint,
                payload=payload,
                cache_affinity_context=route_plan.cache_affinity_context,
                enable_output_safety=True,
                budget_initial_estimated_cost_cny=(
                    budget_guard_result.initial_estimated_cost_cny
                    if budget_guard_result is not None
                    else Decimal("0")
                ),
            )
            rate_limit_lease = None
            return streaming_response

        output_safety_scan: PreparedOutputSafetyScan | None = None
        if request_kind == "chat":
            # 非流式 Chat 在任何 Provider attempt 前先提交 pending owner 并冻结同租户输出规则。
            # Embeddings 没有 Chat 文本投影，继续沿用原 Usage v4 合同，不伪造输出扫描事实。
            output_safety_scan = prepare_output_safety_scan(
                config,
                tenant_id=request_log_context.tenant_id,
                request_id=request_log_context.request_id,
                trace_id=request_log_context.trace_id,
                scan_mode="non_stream_full",
            )
            request_log_context = _context_with_prepared_output_safety_scan(
                request_log_context,
                output_safety_scan,
            )
        return await _non_stream_response_with_fallback(
            config,
            request_log_context,
            route_plan,
            endpoint=endpoint,
            payload=payload,
            request=request,
            request_started=started,
            budget_guard_result=budget_guard_result,
            output_safety_scan=output_safety_scan,
        )
    # asyncio.CancelledError 继承 BaseException，不会进入下面的 ApiError/Exception 分支。
    # 必须先完成已经 claim 的请求审计与预算结算，再保留取消语义交给 ASGI 服务器。
    except asyncio.CancelledError as exc:
        context = (
            getattr(exc, "request_log_context", None)
            or request_log_context
            or _partial_request_log_context(
                request,
                auth_context,
                route,
                endpoint=log_endpoint,
                request_model=request_model,
            )
        )
        cancellation_code = _cancellation_error_code(exc)
        if context is not None and request_claimed:
            safe_record_request_log(
                config,
                context,
                status="failed",
                error_code=cancellation_code,
                latency_ms=_latency_ms(started),
            )
        # Provider 可能已经收到请求，因此使用内层状态机附带的保守 outstanding 金额结算；
        # 未进入 Provider 的取消仍保持 claim 时写入的 0，不会凭空产生费用。
        if (
            request_claimed
            and auth_context is not None
            and context is not None
            and context.budget_reservation_outstanding_cny is not None
        ):
            finalize_request_budget_reservations(
                config,
                tenant_id=auth_context.tenant_id,
                request_id=_request_id(request),
                trace_id=_trace_id(request),
                outstanding_cost_cny=Decimal(context.budget_reservation_outstanding_cny),
            )
        raise
    # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    except ApiError as exc:
        context = (
            getattr(exc, "request_log_context", None)
            or request_log_context
            or _partial_request_log_context(
                request,
                auth_context,
                route,
                endpoint=log_endpoint,
                request_model=request_model,
            )
        )
        # 预算异常携带的事件已在 Guard 事务中提交，失败响应前只能做机会性重投。
        if isinstance(exc, BudgetExhaustedError):
            # 402 返回前重投已提交稳定事件；队列故障不会覆盖预算错误或数据库事实。
            _enqueue_budget_scope_events(request.app.state, exc.scope_events)

        if context is not None and request_claimed:
            # 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
            if isinstance(exc, DlpBlockedError):
                # DLP 阻断摘要来自安全模块，写入 request_logs 时不能包含用户输入明文。
                context = apply_dlp_decision(context, exc.dlp_decision)
            if isinstance(exc, IntentSecurityError):
                # 同步语义异常只携带已冻结安全摘要；正文、命中词和底层异常都不进入日志上下文。
                context = apply_intent_decision(context, exc.intent_risk_decision)
            if isinstance(exc, BudgetExhaustedError):
                # 预算阻断的详细原因来自数据面预算模块，写入 request_logs 便于控制台解释。
                context = apply_budget_decision(context, exc.budget_decision)
            safe_record_request_log(
                config,
                context,
                status="failed",
                error_code=exc.code,
                latency_ms=_latency_ms(started),
            )
        # 只有 Guard 已成功创建预留才结算；尝试状态机在上下文中携带实际仍未知的上游分项。
        if (
            request_claimed
            and auth_context is not None
            and context is not None
            and context.budget_reservation_outstanding_cny is not None
        ):
            finalize_request_budget_reservations(
                config,
                tenant_id=auth_context.tenant_id,
                request_id=_request_id(request),
                trace_id=_trace_id(request),
                outstanding_cost_cny=Decimal(context.budget_reservation_outstanding_cny),
            )
        raise
    finally:
        if rate_limit_lease is not None:
            await rate_limit_lease.release()
        if realtime_gateway_lease is not None:
            await realtime_gateway_lease.release()


async def _non_stream_response_with_fallback(
    config: AppConfig,
    base_context: RequestLogContext,
    plan: GatewayRoutePlan,
    *,
    endpoint: str,
    payload: dict[str, Any],
    request: Request,
    request_started: float,
    budget_guard_result: BudgetGuardResult | None,
    output_safety_scan: PreparedOutputSafetyScan | None = None,
) -> JSONResponse:
    """执行最多主、备各一次的非流式上游状态机。

    每次调用都先写 routing_attempts.started，再选择 Provider Key 和发送网络请求；
    只有 Adapter 明确给出的超时、429、5xx 且命中策略时才创建第二次尝试。
    """

    initial = plan.initial_route
    first_attempt = create_routing_attempt(
        config,
        plan,
        initial,
        request_id=base_context.request_id,
        trace_id=base_context.trace_id,
        attempt_sequence=1,
        attempt_role=plan.initial_role,
        estimated_cost_cny=(
            budget_guard_result.initial_estimated_cost_cny
            if budget_guard_result is not None
            else None
        ),
    )
    first_request: ProviderRequest | None = None
    first_started = time.perf_counter()
    try:
        first_adapter = adapter_for_route(initial)
        first_request = _provider_request_with_affinity(
            first_adapter,
            initial,
            endpoint,
            payload,
            config,
            request,
            affinity=plan.cache_affinity_context,
            attempt_sequence=1,
            routing_attempt_id=first_attempt.routing_attempt_id,
            stream_session_id=None,
        )
        first_response = await first_adapter.send_non_stream(first_request)
    # 当前 Task 被取消时网络请求可能已经发出；必须先终结 attempt/Selection，再原样传播取消。
    except asyncio.CancelledError as caught:
        interrupted_error = _local_interruption_attempt_error(_REQUEST_CANCELLATION_ERROR_CODE)
        safe_complete_routing_attempt(
            config,
            first_attempt,
            succeeded=False,
            latency_ms=_latency_ms(first_started),
            error=interrupted_error,
            usage_status="not_applicable",
            quality_observation_eligible=False,
        )
        safe_finalize_cache_affinity_selection(
            config,
            _selection_for_outcome(first_request, caught),
            succeeded=False,
        )
        cancellation_context = _context_with_budget_outstanding(
            _context_for_attempt(
                base_context,
                initial,
                first_attempt,
                fallback_triggered=False,
                attempt_count=1,
            ),
            (
                budget_guard_result.initial_estimated_cost_cny
                if budget_guard_result is not None
                else Decimal("0")
            ),
        )
        cancellation_context = _finalize_non_stream_without_output(
            config,
            cancellation_context,
            output_safety_scan,
        )
        _attach_cancellation_context(caught, cancellation_context)
        raise
    # 首次调用任何业务错误都先落 attempt 终态，再决定是否满足受控 Fallback 条件。
    except Exception as caught:
        exc = _normalized_provider_exception(caught)
        first_error = _attempt_error(exc)
        quality_eligible = is_quality_observation_eligible_error(first_error)
        safe_complete_routing_attempt(
            config,
            first_attempt,
            succeeded=False,
            latency_ms=_latency_ms(first_started),
            error=first_error,
            usage_status="unavailable" if quality_eligible else "not_applicable",
            quality_observation_eligible=quality_eligible,
        )
        safe_finalize_cache_affinity_selection(
            config,
            _selection_for_outcome(first_request, exc),
            succeeded=False,
        )
        if not _should_fallback(plan, exc):
            terminal_context = _context_with_budget_outstanding(
                _context_for_attempt(
                    base_context,
                    initial,
                    first_attempt,
                    fallback_triggered=False,
                    attempt_count=1,
                ),
                (
                    budget_guard_result.initial_estimated_cost_cny
                    if (
                        budget_guard_result is not None
                        and isinstance(exc, UpstreamApiError)
                        and is_upstream_cost_exposure_possible(exc.normalized_error)
                    )
                    else Decimal("0")
                ),
            )
            exc.request_log_context = _finalize_non_stream_without_output(
                config,
                terminal_context,
                output_safety_scan,
            )
            _raise_normalized_provider_exception(exc, caught)
        try:
            return await _execute_fallback_attempt(
                config,
                base_context,
                plan,
                first_error,
                endpoint=endpoint,
                payload=payload,
                request=request,
                request_started=request_started,
                budget_guard_result=budget_guard_result,
                output_safety_scan=output_safety_scan,
            )
        # 备用闸门和备用调用都沿用统一 ApiError；这里补齐首次上游已发生后的预算与审计上下文。
        except ApiError as fallback_exc:
            if getattr(fallback_exc, "request_log_context", None) is None:
                # 第二次 attempt 闸门可能在备用函数建立上下文前失败；主调用已经发生，
                # 必须保留主估算而不能让外层按 base_context 的 0 覆盖额结算。
                fallback_exc.request_log_context = _context_with_budget_outstanding(
                    _context_for_attempt(
                        base_context,
                        initial,
                        first_attempt,
                        fallback_triggered=True,
                        attempt_count=1,
                    ),
                    (
                        budget_guard_result.initial_estimated_cost_cny
                        if budget_guard_result is not None
                        else Decimal("0")
                    ),
                )
            # 备用闸门或调用失败后已经没有第三个候选；已配置规则必须从 pending 收口为
            # not_applicable，且保留原 Provider/预算错误优先级。
            fallback_exc.request_log_context = _finalize_non_stream_without_output(
                config,
                fallback_exc.request_log_context,
                output_safety_scan,
            )
            raise

    safe_complete_routing_attempt(
        config,
        first_attempt,
        succeeded=True,
        latency_ms=_latency_ms(first_started),
        quality_observation_eligible=True,
    )
    safe_finalize_cache_affinity_selection(
        config,
        _selection_for_outcome(first_request),
        succeeded=True,
    )
    final_context = _context_for_attempt(
        base_context,
        initial,
        first_attempt,
        fallback_triggered=False,
        attempt_count=1,
    )
    return _complete_non_stream_response(
        config,
        final_context,
        first_response,
        request=request,
        request_started=request_started,
        output_safety_scan=output_safety_scan,
    )


async def _execute_fallback_attempt(
    config: AppConfig,
    base_context: RequestLogContext,
    plan: GatewayRoutePlan,
    trigger_error: NormalizedProviderError,
    *,
    endpoint: str,
    payload: dict[str, Any],
    request: Request,
    request_started: float,
    budget_guard_result: BudgetGuardResult | None,
    output_safety_scan: PreparedOutputSafetyScan | None = None,
) -> JSONResponse:
    """在主尝试满足白名单后执行唯一一次备用模型调用。"""

    fallback = plan.fallback_route
    if fallback is None:
        # 调用者只会在 _should_fallback 为真时进入；防御式检查避免未来重构产生空路由调用。
        raise ApiError("gateway.fallback_unavailable")
    attempt = create_routing_attempt(
        config,
        plan,
        fallback,
        request_id=base_context.request_id,
        trace_id=base_context.trace_id,
        attempt_sequence=2,
        attempt_role="fallback",
        trigger_error_code=trigger_error.code,
        estimated_cost_cny=(
            budget_guard_result.fallback_estimated_cost_cny
            if budget_guard_result is not None
            else None
        ),
    )
    event = log_fallback_triggered(plan, attempt, trigger_error)
    enqueue_runtime_event(request.app.state, event)
    provider_request: ProviderRequest | None = None
    attempt_started = time.perf_counter()
    try:
        adapter = adapter_for_route(fallback)
        provider_request = _provider_request_with_affinity(
            adapter,
            fallback,
            endpoint,
            payload,
            config,
            request,
            affinity=plan.cache_affinity_context,
            attempt_sequence=2,
            routing_attempt_id=attempt.routing_attempt_id,
            stream_session_id=None,
        )
        response = await adapter.send_non_stream(provider_request)
    # 备用调用取消与主调用一样先收口；两次请求都可能已到上游，预算采用冻结估算上界。
    except asyncio.CancelledError as caught:
        interrupted_error = _local_interruption_attempt_error(_REQUEST_CANCELLATION_ERROR_CODE)
        safe_complete_routing_attempt(
            config,
            attempt,
            succeeded=False,
            latency_ms=_latency_ms(attempt_started),
            error=interrupted_error,
            usage_status="not_applicable",
            quality_observation_eligible=False,
        )
        safe_finalize_cache_affinity_selection(
            config,
            _selection_for_outcome(provider_request, caught),
            succeeded=False,
        )
        cancellation_context = _context_with_budget_outstanding(
            _context_for_attempt(
                base_context,
                fallback,
                attempt,
                fallback_triggered=True,
                attempt_count=2,
            ),
            (
                budget_guard_result.initial_estimated_cost_cny
                + budget_guard_result.fallback_estimated_cost_cny
                if budget_guard_result is not None
                else Decimal("0")
            ),
        )
        cancellation_context = _finalize_non_stream_without_output(
            config,
            cancellation_context,
            output_safety_scan,
        )
        _attach_cancellation_context(caught, cancellation_context)
        raise
    # 备用模型失败是请求最终失败，必须保存第二次 attempt 摘要供外层 request_log 关联。
    except Exception as caught:
        exc = _normalized_provider_exception(caught)
        attempt_error = _attempt_error(exc)
        quality_eligible = is_quality_observation_eligible_error(attempt_error)
        safe_complete_routing_attempt(
            config,
            attempt,
            succeeded=False,
            latency_ms=_latency_ms(attempt_started),
            error=attempt_error,
            usage_status="unavailable" if quality_eligible else "not_applicable",
            quality_observation_eligible=quality_eligible,
        )
        safe_finalize_cache_affinity_selection(
            config,
            _selection_for_outcome(provider_request, exc),
            succeeded=False,
        )
        terminal_context = _context_with_budget_outstanding(
            _context_for_attempt(
                base_context,
                fallback,
                attempt,
                fallback_triggered=True,
                attempt_count=2,
            ),
            (
                budget_guard_result.initial_estimated_cost_cny
                + (
                    budget_guard_result.fallback_estimated_cost_cny
                    if (
                        isinstance(exc, UpstreamApiError)
                        and is_upstream_cost_exposure_possible(exc.normalized_error)
                    )
                    else Decimal("0")
                )
                if budget_guard_result is not None
                else Decimal("0")
            ),
        )
        exc.request_log_context = _finalize_non_stream_without_output(
            config,
            terminal_context,
            output_safety_scan,
        )
        _raise_normalized_provider_exception(exc, caught)

    safe_complete_routing_attempt(
        config,
        attempt,
        succeeded=True,
        latency_ms=_latency_ms(attempt_started),
        quality_observation_eligible=True,
    )
    safe_finalize_cache_affinity_selection(
        config,
        _selection_for_outcome(provider_request),
        succeeded=True,
    )
    final_context = _context_for_attempt(
        base_context,
        fallback,
        attempt,
        fallback_triggered=True,
        attempt_count=2,
    )
    final_context = _context_with_budget_outstanding(
        final_context,
        (
            budget_guard_result.initial_estimated_cost_cny
            if budget_guard_result is not None
            else Decimal("0")
        ),
    )
    return _complete_non_stream_response(
        config,
        final_context,
        response,
        request=request,
        request_started=request_started,
        output_safety_scan=output_safety_scan,
    )


def _context_with_prepared_output_safety_scan(
    context: RequestLogContext,
    scan: PreparedOutputSafetyScan,
) -> RequestLogContext:
    """把 Provider 前已提交的 scan owner 绑定到当前请求上下文。

    参数：可信 RequestLog 上下文与同请求 prepare 句柄。返回：只增加安全 ID/状态的副本。
    关键约束：tenant/request/trace 必须逐项相同；pending 只用于异常审计，不能写入 Usage v5。
    """

    if (
        scan.tenant_id != context.tenant_id
        or scan.request_id != context.request_id
        or scan.trace_id != context.trace_id
    ):
        # owner 错配可能把另一租户的规则结果拼到当前请求，必须在进入 Provider 前失败关闭。
        raise ApiError("gateway.output_safety_unavailable")
    return replace(
        context,
        output_safety_scan_id=scan.output_safety_scan_id,
        output_safety_scan_status=scan.scan_status,
        output_safety_decision="none" if scan.scan_status == "not_configured" else None,
        billing_disposition=("not_required" if scan.scan_status == "not_configured" else None),
    )


def _context_with_output_safety_terminal(
    context: RequestLogContext,
    scan: PreparedOutputSafetyScan,
    terminal: OutputSafetyTerminalState,
) -> RequestLogContext:
    """把已经提交的输出安全终态投影到 Usage crash-spool 上下文。

    参数：同请求上下文、scan owner 和无正文终态。返回：可写 v5 的完整四字段副本。
    关键约束：该函数只能在 scan/session 事务成功后调用，避免 spool 声称不存在的终态。
    """

    prepared = _context_with_prepared_output_safety_scan(context, scan)
    return replace(
        prepared,
        output_safety_scan_status=terminal.scan_status,
        output_safety_decision=terminal.decision,
        billing_disposition=terminal.billing_disposition,
    )


def _context_without_output_safety(context: RequestLogContext) -> RequestLogContext:
    """移除未能提交的输出扫描摘要，保证真实 Usage 仍可按旧 v4 可靠结算。"""

    return replace(
        context,
        output_safety_scan_id=None,
        output_safety_scan_status=None,
        output_safety_decision=None,
        billing_disposition=None,
    )


def _finalize_non_stream_without_output(
    config: AppConfig,
    context: RequestLogContext,
    scan: PreparedOutputSafetyScan | None,
) -> RequestLogContext:
    """在所有 Provider 候选均未产出成功响应时把 pending scan 收口为不适用。

    参数：运行配置、最终 attempt 上下文和可空 Chat scan。返回：尽可能携带已提交终态的上下文。
    Provider 原错误具有更高解释价值，因此证据库二次故障由持久化服务记录后保留原错误，
    不改写为扫描命中。
    """

    if scan is None or scan.scan_status != "pending":
        return context
    terminal = OutputSafetyTerminalState.not_applicable()
    try:
        finalize_output_safety_scan(config, scan, terminal)
    # Provider 原错误优先级已经确定，终结证据失败只能保留 pending 供控制面核查。
    except ApiError:
        return context
    return _context_with_output_safety_terminal(context, scan, terminal)


def _complete_non_stream_response(
    config: AppConfig,
    context: RequestLogContext,
    response: NormalizedResponse,
    *,
    request: Request,
    request_started: float,
    output_safety_scan: PreparedOutputSafetyScan | None,
) -> JSONResponse:
    """扫描最终非流式响应，结算真实 Usage，并返回响应或统一安全错误。

    参数：最终路由上下文、Adapter 成功响应、HTTP 请求与可空 Chat scan。返回：允许时的 JSONResponse。
    关键约束：扫描发生在 Fallback 状态机之外；blocked/unavailable 仍按 Provider Usage 结算，
    且只有 scan 与 Usage/争议均可靠后才发布 ``security.output_blocked``。
    """

    finalization: OutputSafetyFinalization | None = None
    safety_error_code: str | None = None
    if output_safety_scan is not None and output_safety_scan.scan_status == "pending":
        try:
            decision = scan_full_output(
                output_safety_scan.snapshot,
                response.payload,
                max_codepoints=config.output_safety_max_full_text_code_points,
            )
            blocked = decision.decision == "blocked"
            terminal = OutputSafetyTerminalState.from_scan_decision(
                decision,
                observed_output_token_count=decision.scanned_token_count,
                emitted_token_count=0 if blocked else decision.scanned_token_count,
                emitted_sse_event_count=0,
                emitted_bytes=0,
                exposure_state="none" if blocked else "complete",
            )
            safety_error_code = "content_policy_violation" if blocked else None
        # 已知投影或容量异常固定转为安全不可用，禁止泄露响应和底层解析位置。
        except OutputSafetyError as exc:
            terminal = OutputSafetyTerminalState.unavailable(
                exc.reason_code,
                observed_output_token_count=response.usage.output_tokens,
                exposure_state="none",
            )
            safety_error_code = "gateway.output_safety_unavailable"
        except Exception as exc:
            # 未预期本地扫描故障同样失败关闭，只记录异常类型，绝不序列化响应或规则对象。
            logger.warning(
                "output_safety.non_stream_scan_failed",
                extra={
                    "event": "output_safety.non_stream_scan_failed",
                    "request_id": context.request_id,
                    "trace_id": context.trace_id,
                    "tenant_id": context.tenant_id,
                    "error_code": "gateway.output_safety_unavailable",
                    "payload": {"exception_type": exc.__class__.__name__},
                },
            )
            terminal = OutputSafetyTerminalState.unavailable(
                "output_safety.evidence_unavailable",
                observed_output_token_count=response.usage.output_tokens,
                exposure_state="none",
            )
            safety_error_code = "gateway.output_safety_unavailable"
        try:
            finalization = finalize_output_safety_scan(config, output_safety_scan, terminal)
        # 安全证据事务失败后仍须保存真实 Usage，但不能携带未提交的扫描四元组。
        except ApiError as exc:
            billing_context = _context_without_output_safety(context)
            safe_record_request_log(
                config,
                context,
                status="failed",
                error_code="gateway.output_safety_unavailable",
                latency_ms=_latency_ms(request_started),
            )
            committed = safe_record_usage_ledger(
                config,
                billing_context,
                response.usage.as_ledger_payload(),
                require_durable=True,
            )
            _enqueue_finops_ledger_event(request.app.state, committed, billing_context)
            exc.request_log_context = context
            raise
        context = _context_with_output_safety_terminal(
            context,
            output_safety_scan,
            terminal,
        )
    elif output_safety_scan is not None:
        # 零规则 scan 已在 Provider 前提交 not_configured；完整四字段让 Chat spool 使用 v5，
        # 但不启用严格扫描或改变响应字节语义。
        context = _context_with_prepared_output_safety_scan(context, output_safety_scan)

    request_status = "failed" if safety_error_code is not None else "success"
    safe_record_request_log(
        config,
        context,
        status=request_status,
        error_code=safety_error_code,
        latency_ms=_latency_ms(request_started),
    )
    usage_committed = True
    if output_safety_scan is None:
        # 非 Chat 路径保持既有返回类型和 v4 spool 行为，避免扩大 PRD-071 的协议表面。
        committed = safe_record_usage_ledger(
            config,
            context,
            response.usage.as_ledger_payload(),
            require_durable=True,
        )
        _enqueue_finops_ledger_event(request.app.state, committed, context)
    else:
        try:
            durability = safe_record_usage_ledger(
                config,
                context,
                response.usage.as_ledger_payload(),
                require_durable=True,
                return_durability=True,
            )
        # 主库与 spool 双失败时没有正式 charge，错误上下文必须禁止外层结算预算预留。
        except ApiError as exc:
            exc.request_log_context = _context_with_deferred_budget_settlement(context)
            raise
        if not isinstance(durability, UsageLedgerWriteResult) or durability.durability not in {
            "committed",
            "spooled",
        }:
            # 只有已提交或可靠落入 spool 的 Usage 才可恢复；lost/未知值均不得放行
            # 模型正文或阻断 Webhook。
            error = ApiError("finops.usage_record_unavailable")
            error.request_log_context = _context_with_deferred_budget_settlement(context)
            # durability 无法证明时禁止模型正文离开网关，并保留预算预留等待人工核查。
            raise error
        usage_committed = durability.durability == "committed"
        _enqueue_finops_ledger_event(request.app.state, durability.committed, context)
        if finalization is not None and finalization.source_event is not None:
            _enqueue_output_safety_event(
                request.app.state,
                finalization,
                durability,
                context,
            )

    # 只有 Usage durability 已判定后才抛安全错误，确保账单和 Webhook 的先后门禁稳定。
    if safety_error_code is not None:
        error = ApiError(safety_error_code)
        # spooled 只证明 Usage 可恢复，不代表 charge 已提交；策略错误不能借外层收口提前释放预留。
        error.request_log_context = (
            context if usage_committed else _context_with_deferred_budget_settlement(context)
        )
        # 公开错误只携带固定安全码和已脱敏上下文，绝不附加 Provider 响应正文。
        raise error
    if context.batch_attempt_id is not None:
        # Internal Batch result persistence needs only bounded token counts when Usage was spooled.
        # The assistant payload remains in the immediate response object and is never put on state.
        request.state.batch_gateway_usage = (
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
    return JSONResponse(
        content=response.payload,
        headers=_contract_probe_tenant_headers(request, context.tenant_id),
    )


def _enqueue_output_safety_event(
    app_state: object,
    finalization: OutputSafetyFinalization,
    durability: UsageLedgerWriteResult,
    context: RequestLogContext,
) -> bool:
    """在 scan 与账单 durability 都成立后机会性发布输出阻断 Webhook。

    参数：应用队列、已提交终结结果、Usage durability 和安全上下文。返回：是否成功入队。
    committed/spooled 都证明争议意图可恢复；lost 或无 source event 必须拒绝发布。
    """

    source_event = finalization.source_event
    if source_event is None or durability.durability not in {"committed", "spooled"}:
        return False
    try:
        return enqueue_runtime_event(
            app_state,
            output_safety_event_to_webhook(source_event),
        )
    except Exception as exc:
        # source event 已提交，队列故障只能安全告警，不能回滚扫描、Usage 或改变客户端错误。
        logger.warning(
            "output_safety.event_enqueue_failed",
            extra={
                "event": "output_safety.event_enqueue_failed",
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "tenant_id": context.tenant_id,
                "object_id": source_event.output_safety_scan_id,
                "error_code": "webhook.delivery_unavailable",
                "payload": {"exception_type": exc.__class__.__name__},
            },
        )
        return False


def _stream_not_applicable_terminal(
    scan: PreparedOutputSafetyScan | None,
) -> OutputSafetyTerminalState | None:
    """为尚未进入 active 的配置规则流构造 not_applicable 终态。"""

    if scan is None or scan.scan_status != "pending":
        return None
    return OutputSafetyTerminalState.not_applicable()


def _stream_output_safety_finish_kwargs(
    scan: PreparedOutputSafetyScan | None,
    terminal: OutputSafetyTerminalState | None,
) -> dict[str, object]:
    """仅在 scan/terminal 成对存在时构造 StreamSession 原子终结参数。"""

    if scan is None or terminal is None:
        return {}
    return {
        "output_safety_scan": scan,
        "output_safety_terminal": terminal,
    }


def _context_after_stream_output_safety(
    context: RequestLogContext,
    scan: PreparedOutputSafetyScan | None,
    terminal: OutputSafetyTerminalState | None,
    *,
    finalized: bool,
) -> RequestLogContext:
    """只在 session/scan 原子事务成功后把终态交给 RequestLog 与 Usage。"""

    if finalized and scan is not None and terminal is not None:
        return _context_with_output_safety_terminal(context, scan, terminal)
    return context


def _provider_request(
    adapter: ProviderAdapter,
    route: GatewayRoute,
    endpoint: str,
    payload: dict[str, Any],
    config: AppConfig,
    request: Request,
) -> ProviderRequest:
    """为一次具体路由重新选择 Key 并构建只属于该 Provider 的请求。"""

    provider_request = adapter.build_request(route, endpoint, payload, config)
    provider_request.error_map_lease = _provider_error_map_lease_for_request(
        request, route.tenant_id
    )
    provider_request.request_id = _request_id(request)
    provider_request.trace_id = _trace_id(request)
    return provider_request


def _provider_request_with_affinity(
    adapter: ProviderAdapter,
    route: GatewayRoute,
    endpoint: str,
    payload: dict[str, Any],
    config: AppConfig,
    request: Request,
    *,
    affinity: CacheAffinityRouteContext | None,
    attempt_sequence: int,
    routing_attempt_id: str | None,
    stream_session_id: str | None,
) -> ProviderRequest:
    """在 execution gate 后选择 Provider access，并为亲和请求原子提交 Selection。

    无亲和上下文时复用旧 ``_provider_request``，保持既有 KeyPool 与测试注入点。有效亲和
    上下文则先提交 Selection、立即投递一次安全事件，再显式把已选 Key 交给 Adapter；
    Adapter 不能再次选择 Key。构造失败时把 repr-safe 句柄临时附到异常，供父状态提交后释放。
    """

    if affinity is None:
        return _provider_request(adapter, route, endpoint, payload, config, request)
    access = select_provider_access(
        config,
        route,
        affinity=affinity,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        attempt_sequence=attempt_sequence,
        routing_attempt_id=routing_attempt_id,
        stream_session_id=stream_session_id,
    )
    if access.cache_affinity_event is not None:
        # Selection 已经提交，队列故障只能丢事件告警，不能回滚事实或阻塞 Provider 调用。
        enqueue_runtime_event(request.app.state, access.cache_affinity_event)
    try:
        provider_request = adapter.build_request(
            route,
            endpoint,
            payload,
            config,
            selected_key=access.selected_key,
            cache_affinity_selection=access.cache_affinity_selection,
        )
    except Exception as exc:
        # 仅在当前调用栈携带 repr-safe 句柄；异常日志不得序列化 __dict__ 或句柄内容。
        exc._cache_affinity_selection = access.cache_affinity_selection
        raise
    provider_request.error_map_lease = _provider_error_map_lease_for_request(
        request, route.tenant_id
    )
    provider_request.request_id = _request_id(request)
    provider_request.trace_id = _trace_id(request)
    return provider_request


def _selection_for_outcome(
    provider_request: ProviderRequest | None,
    error: BaseException | None = None,
) -> CacheAffinitySelectionHandle | None:
    """从内存请求或构造异常取得 repr-safe Selection 句柄，不读取任何客户端字段。"""

    # 已成功构造 ProviderRequest 时只读取内部句柄，禁止回看请求正文或认证 Header。
    if provider_request is not None:
        return provider_request.cache_affinity_selection
    candidate = getattr(error, "_cache_affinity_selection", None) if error is not None else None
    return candidate if isinstance(candidate, CacheAffinitySelectionHandle) else None


def _normalized_provider_exception(error: BaseException) -> ApiError:
    """把 Provider 阶段未预期异常转换为固定错误，并保留内存 Selection 句柄供终态释放。

    Adapter 的 RuntimeError/TypeError 可能携带 URL、Header 或第三方正文，绝不能直接进入统一
    响应或结构化日志。已有 ApiError 保持原分类；其他异常只映射到固定 upstream unavailable。
    """

    # 已经归一化的领域错误保留固定分类，避免再次解析可能含敏感正文的底层异常。
    if isinstance(error, ApiError):
        return error
    normalized = ApiError("gateway.upstream_unavailable")
    selection = getattr(error, "_cache_affinity_selection", None)
    if isinstance(selection, CacheAffinitySelectionHandle):
        # 句柄只活在当前调用栈且 repr-safe，不能把原异常 __dict__ 整体复制给公开错误。
        normalized._cache_affinity_selection = selection
    return normalized


def _raise_normalized_provider_exception(
    error: ApiError,
    original: BaseException,
) -> None:
    """抛出归一化 Provider 错误；仅在发生转换时保留 Python cause 供本地调试。"""

    # 同一领域错误不建立冗余 cause 链，避免调试工具重复序列化异常上下文。
    if error is original:
        # 原异常已经满足公开错误合同，可以直接传播且不改变错误码。
        raise error
    # 未分类底层异常只保留为本地 cause，对外始终由固定 ApiError 控制内容。
    raise error from original


def _should_fallback(plan: GatewayRoutePlan, exc: ApiError) -> bool:
    """只按 Adapter 受控错误分类和策略白名单判断是否切换备用模型。"""

    # 没有可执行备用路由或错误并非受控上游错误时禁止切换，避免本地故障触发重试风暴。
    if plan.fallback_route is None or not isinstance(exc, UpstreamApiError):
        return False
    condition_by_category = {
        "timeout": "upstream_timeout",
        "rate_limited": "upstream_rate_limited",
        "upstream_5xx": "upstream_5xx",
    }
    condition = condition_by_category.get(exc.normalized_error.error_category)
    return condition is not None and condition in plan.fallback_conditions


def _attempt_error(exc: ApiError) -> NormalizedProviderError:
    """把本地错误和上游错误统一成只含白名单元数据的 attempt 终态。"""

    # Adapter 已完成安全归一化时直接复用，禁止再次解析可能含密钥或响应正文的异常对象。
    if isinstance(exc, UpstreamApiError):
        return quality_observation_error(exc.normalized_error)
    return NormalizedProviderError(
        code=exc.code,
        message="",
        http_status=None,
        retryable=False,
        provider_error_type="gateway",
        error_category="other",
    )


def _local_interruption_attempt_error(error_code: str) -> NormalizedProviderError:
    """构造不参与模型质量统计的本地中断 attempt 摘要。

    参数只能是服务端固定错误码，返回值不含异常正文、Prompt、Endpoint 或凭据。调用方必须
    同时传 ``quality_observation_eligible=False`` 和 ``usage_status=not_applicable``。
    """

    return NormalizedProviderError(
        code=error_code,
        message="",
        http_status=None,
        retryable=False,
        provider_error_type="gateway",
        error_category="other",
    )


def _attach_cancellation_context(
    error: asyncio.CancelledError,
    context: RequestLogContext,
    *,
    error_code: str = _REQUEST_CANCELLATION_ERROR_CODE,
) -> None:
    """给当前栈内取消异常附加 repr-safe 审计上下文，供最外层完成 RequestLog/预算闭环。"""

    # 只传递服务端构造的 dataclass 和固定错误码；不得复制原异常 __dict__ 或客户端字段。
    error.request_log_context = context
    error._gateway_cancellation_error_code = error_code


def _cancellation_error_code(error: asyncio.CancelledError) -> str:
    """从内部取消路径读取固定错误码，未知属性一律回退到客户端断开。"""

    candidate = getattr(error, "_gateway_cancellation_error_code", None)
    # 白名单阻止未来第三方异常属性把任意文本写入 request_logs.error_code。
    if candidate in {
        _REQUEST_CANCELLATION_ERROR_CODE,
        LOCAL_STREAM_PROCESSING_ERROR_CODE,
    }:
        return candidate
    return _REQUEST_CANCELLATION_ERROR_CODE


def _context_for_attempt(
    base: RequestLogContext,
    route: GatewayRoute,
    attempt: RoutingAttempt,
    *,
    fallback_triggered: bool,
    attempt_count: int,
) -> RequestLogContext:
    """在保留 DLP/预算摘要的同时，把日志最终路由切换到实际响应模型。"""

    return replace(
        base,
        provider_id=route.provider_id,
        model_id=route.model_id,
        resolved_model=route.model_alias,
        provider_code=route.provider_code,
        fallback_triggered=fallback_triggered,
        routing_attempt_count=attempt_count,
        final_routing_attempt_id=attempt.routing_attempt_id,
        routing_attempt_id=attempt.routing_attempt_id,
    )


def _context_for_route(base: RequestLogContext, route: GatewayRoute) -> RequestLogContext:
    """在不改变快照/Policy/Guard 摘要的前提下补齐最终权威路由元数据。"""

    return replace(
        base,
        provider_id=route.provider_id,
        model_id=route.model_id,
        request_model=route.requested_model or base.request_model,
        resolved_model=route.model_alias,
        provider_code=route.provider_code,
    )


def _context_with_budget_outstanding(
    context: RequestLogContext,
    outstanding_cost_cny: Decimal,
) -> RequestLogContext:
    """把实际仍未知的上游分项写入内存日志上下文，供 Usage/失败终态原子结算。

    没有 active 预算规则时字段保持 None，避免动态路由给无预算请求新增价格依赖。金额只来自
    Budget Guard 的冻结估算，不读取 Provider 响应正文，也不会持久化到 request_logs。
    """

    if context.budget_reservation_outstanding_cny is None:
        return context
    return replace(
        context,
        budget_reservation_outstanding_cny=format(outstanding_cost_cny, "f"),
    )


def _context_with_deferred_budget_settlement(
    context: RequestLogContext,
) -> RequestLogContext:
    """移除外层异常处理的预算结算指令，保留原预留等待 Usage 正式入账。

    参数：已携带路由、扫描和预算摘要的请求上下文。返回：仅清除临时结算金额的副本。
    关键约束：Usage 只写入 spool 或 durability 丢失时尚无正式 charge；此时若把 0 交给
    外层 ``ApiError`` 收口，会提前释放预留并允许并发请求绕过预算上限。spool replay 会在
    Usage、FinOps 和争议事实原子提交时完成真正结算，因此这里不能直接修改数据库。
    """

    if context.budget_reservation_outstanding_cny is None:
        return context
    return replace(context, budget_reservation_outstanding_cny=None)


def _context_with_session_risk_preflight(
    context: RequestLogContext,
    result: SessionRiskPreflightResult,
    *,
    request_kind: str,
) -> RequestLogContext:
    """把读取正文前的固定门禁结果转换为 RequestLog 摘要。

    参数：当前日志上下文、可信预检结果和请求类型。返回：不含 scope key/Header 的新上下文。
    关键约束：Chat 缺少成对 Session/Turn 标识时明确记录 disabled/no_session_identifiers；
    Embeddings 不建立 session，始终保留 not_applicable，不能被误记为一个共享 Key score。
    """

    if result.status == "clean":
        # clean 只表示可以读取正文，尚未完成当前 turn 分类，不能提前伪造 clean_turn 事实。
        return context
    if result.status == "degraded":
        return replace(
            context,
            session_risk_status="degraded",
            session_risk_score_id=None,
            session_risk_scope_type=result.scope_type,
            session_risk_state=None,
            session_risk_reason_code="store_degraded",
        )
    if request_kind != "chat":
        return replace(
            context,
            session_risk_status="not_applicable",
            session_risk_score_id=None,
            session_risk_scope_type=None,
            session_risk_state=None,
            session_risk_reason_code=None,
        )
    if result.status == "disabled":
        reason_code = "feature_disabled"
    elif result.status == "not_applicable":
        reason_code = "no_session_identifiers"
    else:
        # Service 返回未知状态意味着调用边界漂移；固定失败关闭，不能继续 Rate/Budget/Provider。
        raise ApiError("gateway.session_risk_unavailable")
    return replace(
        context,
        session_risk_status="disabled",
        session_risk_score_id=None,
        session_risk_scope_type=None,
        session_risk_state=None,
        session_risk_reason_code=reason_code,
    )


def _context_with_session_risk_error(
    context: RequestLogContext,
    error: ApiError,
    *,
    identity: SessionRiskRequestIdentity | None,
) -> RequestLogContext:
    """把会话风险异常折叠为数据库允许的固定失败摘要。

    参数：当前上下文、统一 API 异常和可选可信身份。返回：仅含固定错误原因及 scope 类型的
    新上下文。异常 message、Header、HMAC、Prompt 和 Redis/SQL 细节都不得进入 RequestLog。
    """

    reason_code = getattr(error, "session_risk_reason_code", None)
    if reason_code not in {
        "store_unavailable",
        "evidence_unavailable",
        "session_frozen",
        "key_frozen",
        "turn_conflict",
        "hmac_version_unavailable",
    }:
        reason_code = _SESSION_RISK_ERROR_REASONS.get(
            error.code,
            "evidence_unavailable",
        )
    return replace(
        context,
        session_risk_status="failed",
        session_risk_score_id=None,
        session_risk_scope_type=(
            identity.scope.scope_type if identity is not None else context.session_risk_scope_type
        ),
        session_risk_state=None,
        session_risk_reason_code=reason_code,
    )


def _context_with_session_risk_turn(
    context: RequestLogContext,
    identity: SessionRiskRequestIdentity,
    result: SessionRiskTurnResult,
) -> RequestLogContext:
    """把一次原子累计结果转换为不可回退的 RequestLog 安全摘要。

    参数：当前上下文、可信 session identity 和 Store/DB 已确认结果。返回：固定状态的新上下文。
    关键约束：零分新 turn 不创建 score；已有活动 score 上的零分 turn仍显示当前 session 状态；
    duplicate 只描述幂等重放，绝不再次累计或伪造新 observation。
    """

    snapshot = result.snapshot
    if result.duplicate:
        return replace(
            context,
            session_risk_status="duplicate",
            session_risk_score_id=snapshot.score_id if snapshot is not None else None,
            session_risk_scope_type=identity.scope.scope_type,
            session_risk_state=snapshot.state if snapshot is not None else None,
            session_risk_reason_code="duplicate_turn",
        )
    if snapshot is None:
        if result.status != "clean":
            # 非 clean 却没有活动快照属于证据合同错误，必须固定失败关闭且不能继续调用上游。
            raise SessionRiskContractError()
        return replace(
            context,
            session_risk_status="clean",
            session_risk_score_id=None,
            session_risk_scope_type=identity.scope.scope_type,
            session_risk_state=None,
            session_risk_reason_code="clean_turn",
        )
    # Store 只能返回三个活动状态，resolved/expired 或未知值出现在请求期属于证据合同损坏。
    if snapshot.state not in {"monitoring", "high_risk", "frozen"}:
        # 合同损坏必须在写 RequestLog 前失败关闭，禁止把未知状态传播到上游调用阶段。
        raise SessionRiskContractError()
    return replace(
        context,
        session_risk_status=snapshot.state,
        session_risk_score_id=snapshot.score_id,
        session_risk_scope_type=identity.scope.scope_type,
        session_risk_state=snapshot.state,
        session_risk_reason_code=(
            "session_frozen" if snapshot.state == "frozen" else "risk_accumulated"
        ),
    )


async def _record_trusted_session_turn(
    config: AppConfig,
    app_state: object,
    context: RequestLogContext,
    identity: SessionRiskRequestIdentity,
    payload: dict[str, Any],
    *,
    dlp_decision: DlpDecision | None,
    current_turn_intent: IntentClassification,
    input_truncated: bool,
    detect_tools: bool,
) -> RequestLogContext:
    """构造并提交一个可信 current turn 的唯一会话风险 assessment。

    参数：部署配置、应用状态、日志 owner、可信身份、Policy mask 后正文及可选 DLP/固定 Intent 摘要。
    返回：带最终会话风险状态的新日志上下文。关键约束：完整 current turn 仅用于分域 HMAC；
    没有生效 DLP 规则时按 allow/零命中累计；DLP 已阻断路径传 ``detect_tools=False``，保证
    零次意图分类且只累计 DLP 当前轮信号。
    """

    texts = extract_session_user_texts(payload)
    current_turn_text = texts[-1] if texts else ""
    try:
        turn_intent_digest = identity.derive_turn_intent_digest(current_turn_text)
        # 没有命中或配置 DLP 规则是合法主流程，只能解释为 allow/零命中，不能解引用空决策导致 500。
        current_turn_dlp_action = (
            dlp_decision.current_turn_action if dlp_decision is not None else "allow"
        )
        current_turn_dlp_match_count = (
            dlp_decision.current_turn_match_count if dlp_decision is not None else 0
        )
        assessment = build_turn_risk_assessment(
            turn_key_digest=identity.turn_key_digest,
            turn_intent_digest=turn_intent_digest,
            intent=current_turn_intent,
            dlp_action=current_turn_dlp_action,
            dlp_current_turn_match_count=current_turn_dlp_match_count,
            tool_inducement_detected=(
                detect_tool_inducement((current_turn_text,)) if detect_tools else False
            ),
            input_truncated=input_truncated,
            source_request_id=context.request_id,
            source_trace_id=context.trace_id,
            intent_risk_result_id=context.intent_risk_result_id,
            chat_message_id=identity.chat_message_id,
        )
        turn_result = await record_session_risk_turn(
            config,
            app_state,
            identity,
            assessment,
        )
        updated = _context_with_session_risk_turn(context, identity, turn_result)
    # Shared/Store 合同错误统一映射为证据不可用，异常不得携带正文、摘要或内部对象。
    except SessionRiskContractError:
        # Shared/Store 合同漂移不得泄露对象内容，只暴露统一证据不可用并附带安全失败摘要。
        error = ApiError("gateway.session_risk_evidence_unavailable")
        error.request_log_context = _context_with_session_risk_error(
            context,
            error,
            identity=identity,
        )
        # 只抛出固定 ApiError，避免原始合同异常被框架日志记录并泄露内部状态。
        raise error from None
    # Store 已产生公开 ApiError 时保留其错误码，只补齐缺失的 RequestLog 安全上下文。
    except ApiError as error:
        # 已带上下文的错误不可二次改写，否则可能覆盖更具体的冻结或证据失败原因。
        if getattr(error, "request_log_context", None) is None:
            error.request_log_context = _context_with_session_risk_error(
                context,
                error,
                identity=identity,
            )
        raise
    finally:
        # 及时释放当前函数持有的正文投影；任何日志或异常都不能引用该字符串。
        current_turn_text = ""
        texts = ()

    # 冻结事实已提交后必须在 Rate/Budget/Provider 前终止，确保零次上游模型调用。
    if updated.session_risk_status == "frozen":
        # 当前第三个风险 turn 已提交冻结证据；在 Rate/Budget/Provider/SSE 前返回普通统一 JSON 错误。
        error = ApiError("gateway.session_risk_frozen")
        error.request_log_context = updated
        raise error
    return updated


def _filter_fallback_capability(
    plan: GatewayRoutePlan,
    request_kind: str,
    payload: dict[str, Any],
) -> GatewayRoutePlan:
    """在调用前移除不支持当前接口能力的备用候选，不影响可用主模型。"""

    fallback = plan.fallback_route
    # 无备用候选或能力已满足时保留原计划，只有能力不匹配才禁用请求期 Fallback。
    if fallback is None or _route_supports_request(request_kind, payload, fallback):
        return plan
    return replace(
        plan,
        fallback_route=None,
        fallback_policy_id=None,
        fallback_policy_version=None,
        fallback_conditions=(),
    )


def _route_supports_request(
    request_kind: str, payload: dict[str, Any], route: GatewayRoute
) -> bool:
    """以布尔结果检查备用模型能力，避免主失败后才抛本地能力错误。"""

    capabilities = route.capabilities
    if request_kind == "chat" and capabilities.get("chat") is False:
        return False
    if request_kind == "embeddings" and capabilities.get("embeddings") is False:
        return False
    if (
        request_kind == "chat"
        and payload.get("stream") is True
        and capabilities.get("stream") is False
    ):
        return False
    return True


async def _stream_response(
    config: AppConfig,
    context: RequestLogContext,
    adapter: ProviderAdapter,
    provider_request: ProviderRequest | None,
    started: float,
    rate_limit_lease: RateLimitLease | None,
    *,
    client_request: Request | None = None,
    route: GatewayRoute | None = None,
    endpoint: str | None = None,
    payload: dict[str, Any] | None = None,
    cache_affinity_context: CacheAffinityRouteContext | None = None,
    enable_output_safety: bool = False,
    budget_initial_estimated_cost_cny: Decimal = Decimal("0"),
) -> StreamingResponse:
    """执行可审计的 SSE 透传、块间看门狗和客户端断开清理。

    StreamSession 在上游网络调用前创建；上游 2xx 后才推进 active 并返回 StreamingResponse。
    HTTP 200 发出后的错误只能使用标准 SSE error，不能伪装成可修改状态码的 JSON 响应。
    """

    timeout_seconds = float(config.stream_inter_token_timeout_seconds)
    session = create_stream_session(
        config,
        context,
        inter_token_timeout_ms=int(timeout_seconds * 1000),
    )
    session_context = replace(context, stream_session_id=session.stream_session_id)
    output_safety_scan: PreparedOutputSafetyScan | None = None
    selection_handle = (
        provider_request.cache_affinity_selection if provider_request is not None else None
    )
    try:
        if enable_output_safety:
            # 流式 scan 必须在已提交 starting Session 之后创建，复合 FK 不允许先写空 session owner。
            output_safety_scan = prepare_output_safety_scan(
                config,
                tenant_id=session.tenant_id,
                request_id=session.request_id,
                trace_id=session.trace_id,
                scan_mode="stream_window",
                stream_session_id=session.stream_session_id,
                interval_tokens=config.output_safety_interval_tokens,
                window_tokens=config.output_safety_window_tokens,
            )
            session_context = _context_with_prepared_output_safety_scan(
                session_context,
                output_safety_scan,
            )
        # 未预构造请求表示亲和 Selection 必须绑定刚创建的 StreamSession 后再提交。
        if provider_request is None:
            # 动态构造缺少任一可信服务端参数都必须停在 Provider 网络调用之前。
            if route is None or endpoint is None or payload is None or client_request is None:
                # 会话虽已创建但请求图不完整，统一进入握手失败收口而不泄露缺失字段。
                raise ApiError("gateway.stream_session_unavailable")
            provider_request = _provider_request_with_affinity(
                adapter,
                route,
                endpoint,
                payload,
                config,
                client_request,
                affinity=cache_affinity_context,
                attempt_sequence=1,
                routing_attempt_id=None,
                stream_session_id=session.stream_session_id,
            )
            selection_handle = provider_request.cache_affinity_selection
        upstream_response = await adapter.send_stream(provider_request)
    # 握手尚未返回 StreamingResponse，取消必须把 starting 会话写成固定本地失败并释放 Selection。
    except asyncio.CancelledError as exc:
        output_terminal = _stream_not_applicable_terminal(output_safety_scan)
        finalized = safe_finish_stream_session(
            config,
            session,
            status="upstream_failed",
            error_code=LOCAL_STREAM_PROCESSING_ERROR_CODE,
            quality_observation_eligible=False,
            **_stream_output_safety_finish_kwargs(output_safety_scan, output_terminal),
        )
        safe_finalize_cache_affinity_selection(
            config,
            selection_handle or _selection_for_outcome(None, exc),
            succeeded=False,
        )
        terminal_context = _context_after_stream_output_safety(
            session_context,
            output_safety_scan,
            output_terminal,
            finalized=finalized,
        )
        cancellation_context = _context_with_budget_outstanding(
            _stream_terminal_context(
                terminal_context,
                "upstream_failed",
                finalized=finalized,
            ),
            budget_initial_estimated_cost_cny,
        )
        _attach_cancellation_context(
            exc,
            cancellation_context,
            error_code=LOCAL_STREAM_PROCESSING_ERROR_CODE,
        )
        raise
    # Adapter 已归一化的握手错误必须先结束会话，再交给统一 JSON 异常处理器。
    except ApiError as exc:
        # 握手前统一错误仍由 FastAPI JSON handler 返回；先提交会话终态再交给外层写 request_logs。
        session_error_code, quality_eligible = _stream_session_handshake_facts(exc)
        provider_error = exc.normalized_error if isinstance(exc, UpstreamApiError) else None
        fact_kwargs = (
            {"error": provider_error}
            if provider_error is not None and provider_error.evidence is not None
            else {}
        )
        output_terminal = _stream_not_applicable_terminal(output_safety_scan)
        finalized = safe_finish_stream_session(
            config,
            session,
            status="upstream_failed",
            error_code=session_error_code,
            # 只有固定质量白名单内的上游错误才处罚模型；账号、Key 和配置错误继续排除。
            quality_observation_eligible=quality_eligible,
            **fact_kwargs,
            **_stream_output_safety_finish_kwargs(output_safety_scan, output_terminal),
        )
        safe_finalize_cache_affinity_selection(
            config,
            selection_handle or _selection_for_outcome(None, exc),
            succeeded=False,
        )
        terminal_context = _context_after_stream_output_safety(
            session_context,
            output_safety_scan,
            output_terminal,
            finalized=finalized,
        )
        exc.request_log_context = _context_with_budget_outstanding(
            _stream_terminal_context(terminal_context, "upstream_failed", finalized=finalized),
            (
                budget_initial_estimated_cost_cny
                if (
                    isinstance(exc, UpstreamApiError)
                    and is_upstream_cost_exposure_possible(exc.normalized_error)
                )
                else Decimal("0")
            ),
        )
        raise
    except Exception as exc:
        # Adapter 未归一化异常没有可信上游归因，必须使用本地处理码并排除质量统计。
        output_terminal = _stream_not_applicable_terminal(output_safety_scan)
        finalized = safe_finish_stream_session(
            config,
            session,
            status="upstream_failed",
            error_code=LOCAL_STREAM_PROCESSING_ERROR_CODE,
            quality_observation_eligible=False,
            **_stream_output_safety_finish_kwargs(output_safety_scan, output_terminal),
        )
        safe_finalize_cache_affinity_selection(
            config,
            selection_handle or _selection_for_outcome(None, exc),
            succeeded=False,
        )
        error = ApiError(LOCAL_STREAM_PROCESSING_ERROR_CODE)
        terminal_context = _context_after_stream_output_safety(
            session_context,
            output_safety_scan,
            output_terminal,
            finalized=finalized,
        )
        error.request_log_context = _context_with_budget_outstanding(
            _stream_terminal_context(terminal_context, "upstream_failed", finalized=finalized),
            budget_initial_estimated_cost_cny,
        )
        # 原始异常只作为 Python cause 保留，客户端和结构化日志始终使用固定错误码。
        raise error from exc

    try:
        mark_stream_session_active(config, session)
    # active 审计闸门失败时已建立的上游连接必须立即关闭，禁止向下游发送响应头。
    except ApiError as exc:
        # 上游已经连接但审计状态无法推进时必须 Fail Closed，不能开始无法确认状态的下游流。
        await _safe_close_stream_response(upstream_response, session_context)
        output_terminal = _stream_not_applicable_terminal(output_safety_scan)
        finalized = safe_finish_stream_session(
            config,
            session,
            status="upstream_failed",
            error_code="gateway.stream_session_unavailable",
            quality_observation_eligible=False,
            **_stream_output_safety_finish_kwargs(output_safety_scan, output_terminal),
        )
        safe_finalize_cache_affinity_selection(
            config,
            selection_handle,
            succeeded=False,
        )
        terminal_context = _context_after_stream_output_safety(
            session_context,
            output_safety_scan,
            output_terminal,
            finalized=finalized,
        )
        exc.request_log_context = _context_with_budget_outstanding(
            _stream_terminal_context(terminal_context, "upstream_failed", finalized=finalized),
            budget_initial_estimated_cost_cny,
        )
        raise
    # StreamingResponse 必须是 ASGI receive 的唯一消费者；其断连监听把信号桥接给块读取状态机。
    disconnect_event = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        """逐块处理 SSE，并把输出扫描、Session、Usage 和资源统一收口。"""

        status = "success"
        error_code: str | None = None
        stream_status = "completed"
        session_finalized = False
        upstream_read_failed = False
        session_error_code: str | None = None
        collector = _SseUsageCollector(adapter)
        safety_gate_enabled = bool(output_safety_scan and output_safety_scan.configured)
        framer = BoundedSseEventFramer() if safety_gate_enabled else None
        scanner = (
            SlidingOutputScanner(
                output_safety_scan.snapshot,
                interval_tokens=output_safety_scan.interval_tokens or 0,
                window_tokens=output_safety_scan.window_tokens or 0,
            )
            if safety_gate_enabled and output_safety_scan is not None
            else None
        )
        emitted_counter = _OutputTokenCounter()
        emitted_sse_event_count = 0
        output_decision: OutputScanDecision | None = None
        output_terminal: OutputSafetyTerminalState | None = None
        scan_finished = False
        deferred_safety_error = False
        upstream_iterator = upstream_response.iter_bytes().__aiter__()
        read_task: asyncio.Task[bytes] | None = None
        disconnect_task: asyncio.Task[None] | None = None
        cancellation_in_flight = False
        try:
            while True:
                read_task = asyncio.create_task(
                    anext(upstream_iterator), name="gateway-stream-read"
                )
                disconnect_task = asyncio.create_task(
                    disconnect_event.wait(),
                    name="gateway-stream-disconnect",
                )
                # 任一条件完成就立即判定；每个网络块都会重置既有 inter-token watchdog。
                done, _pending = await asyncio.wait(
                    {read_task, disconnect_task},
                    timeout=timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    # 已确认断开优先于同轮到达的新块，避免继续为已关闭客户端处理模型正文。
                    await _cancel_stream_task(read_task)
                    read_task = None
                    disconnect_task = None
                    raise _ClientStreamDisconnected
                if read_task in done or read_task.done():
                    await _cancel_stream_task(disconnect_task)
                    disconnect_task = None
                    try:
                        chunk = read_task.result()
                    except StopAsyncIteration:
                        read_task = None
                        if framer is not None and scanner is not None:
                            # EOF 必须先证明没有残缺 event，再执行强制尾窗；
                            # 通过后才能声明 completed。
                            framer.finish()
                            output_decision = scanner.finish()
                            scan_finished = True
                            if output_decision.decision == "blocked":
                                raise _OutputSafetyBlocked(output_decision) from None
                        break
                    except Exception:
                        # 只有上游 iterator 的异常才具备 Provider 质量归因资格。
                        upstream_read_failed = True
                        raise
                    read_task = None
                else:
                    await _cancel_stream_task(read_task)
                    await _cancel_stream_task(disconnect_task)
                    read_task = None
                    disconnect_task = None
                    # 看门狗超时使用内部控制异常切换终态，不能继续等待或发送未知正文。
                    raise _UpstreamStreamTimedOut

                if framer is None or scanner is None:
                    # 零输出规则保持 PRD-070 raw passthrough 与宽容 Usage parser，
                    # 不能改变网络兼容性。
                    collector.feed(chunk)
                    session.note_chunk(chunk)
                    yield chunk
                    continue

                done_seen = False
                for frame in framer.feed(chunk):
                    # 完整 event 先计入 Provider observed Usage，再扫描；命中 frame 不计入 emitted。
                    collector.observe_frame(frame)
                    if frame.is_done:
                        output_decision = scanner.finish()
                        scan_finished = True
                        decision = output_decision
                    else:
                        decision = scanner.feed_sse_frame(frame.fragments)
                    if decision is not None and decision.decision == "blocked":
                        raise _OutputSafetyBlocked(decision)
                    session.note_chunk(frame.raw_bytes)
                    emitted_counter.observe_fragments(frame.fragments)
                    if frame.fragments:
                        # 该字段只统计已经交付正文投影的 event；keepalive、usage-only 与 [DONE]
                        # 虽然按原始字节发送，但不能夸大客户已暴露的模型正文事件数。
                        emitted_sse_event_count += 1
                    yield frame.raw_bytes
                    if frame.is_done:
                        # `[DONE]` 是协议终点；同一 raw chunk 中其后的非标准 event
                        # 不再向客户端发送。
                        done_seen = True
                        break
                if done_seen:
                    break
        except _OutputSafetyBlocked as blocked:
            status = "failed"
            stream_status = "output_blocked"
            error_code = "content_policy_violation"
            session_error_code = error_code
            output_decision = blocked.decision
            output_terminal = OutputSafetyTerminalState.from_scan_decision(
                blocked.decision,
                observed_output_token_count=collector.observed_output_token_count,
                emitted_token_count=emitted_counter.token_count,
                emitted_sse_event_count=emitted_sse_event_count,
                emitted_bytes=session.bytes_sent,
                exposure_state="partial" if emitted_counter.has_content else "none",
            )
            await _safe_close_stream_response(upstream_response, session_context)
            session_finalized = safe_finish_stream_session(
                config,
                session,
                status=stream_status,
                error_code=error_code,
                quality_observation_eligible=False,
                **_stream_output_safety_finish_kwargs(output_safety_scan, output_terminal),
            )
            # 原子终结失败时阻断事件没有可信证据，公开错误必须降级为安全不可用。
            if not session_finalized:
                error_code = "gateway.output_safety_unavailable"
            deferred_safety_error = True
            _log_stream_failure(session_context, error_code, include_exception=False)
        # 严格 SSE、投影或窗口故障均在本地失败关闭，不能归因并处罚 Provider 模型质量。
        except OutputSafetyError as exc:
            status = "failed"
            stream_status = "output_safety_unavailable"
            error_code = "gateway.output_safety_unavailable"
            session_error_code = error_code
            if scanner is not None:
                output_terminal = _stream_unavailable_terminal(
                    scanner,
                    emitted_counter,
                    observed_output_token_count=collector.observed_output_token_count,
                    emitted_sse_event_count=emitted_sse_event_count,
                    emitted_bytes=session.bytes_sent,
                    reason_code=exc.reason_code,
                )
            await _safe_close_stream_response(upstream_response, session_context)
            session_finalized = safe_finish_stream_session(
                config,
                session,
                status=stream_status,
                error_code=error_code,
                quality_observation_eligible=False,
                **_stream_output_safety_finish_kwargs(output_safety_scan, output_terminal),
            )
            deferred_safety_error = True
            _log_stream_failure(session_context, error_code, include_exception=False)
        # 块间看门狗超时保留 Provider 质量资格，并终结任何仍为 pending 的安全扫描。
        except _UpstreamStreamTimedOut:
            status = "failed"
            stream_status = "timed_out"
            error_code = "gateway.upstream_stream_timeout"
            session_error_code = error_code
            await _safe_close_stream_response(upstream_response, session_context)
            if scanner is not None:
                output_terminal = _stream_unavailable_terminal(
                    scanner,
                    emitted_counter,
                    observed_output_token_count=collector.observed_output_token_count,
                    emitted_sse_event_count=emitted_sse_event_count,
                    emitted_bytes=session.bytes_sent,
                    reason_code="output_safety.stream_incomplete",
                )
            session_finalized = safe_finish_stream_session(
                config,
                session,
                status=stream_status,
                error_code=error_code,
                quality_observation_eligible=True,
                **_stream_output_safety_finish_kwargs(output_safety_scan, output_terminal),
            )
            # 只有 Session 终态已提交且应用队列可用时才发布可订阅的超时事实。
            if session_finalized and client_request is not None:
                enqueue_runtime_event(client_request.app.state, build_stream_timeout_event(session))
            _log_stream_failure(session_context, error_code, include_exception=False)
            yield _stream_error_event(error_code, session_context)
        except _ClientStreamDisconnected:
            status = "failed"
            stream_status = "client_disconnected"
            error_code = "gateway.client_disconnected"
            session_error_code = error_code
            _log_stream_failure(session_context, error_code, include_exception=False)
        # ASGI 取消与显式断连使用同一清理终态，但取消信号仍需在最终收口后向上传播。
        except asyncio.CancelledError:
            status = "failed"
            stream_status = "client_disconnected"
            error_code = "gateway.client_disconnected"
            session_error_code = error_code
            cancellation_in_flight = True
            _log_stream_failure(session_context, error_code, include_exception=False)
            raise
        except GeneratorExit:
            status = "failed"
            stream_status = "client_disconnected"
            error_code = "gateway.client_disconnected"
            session_error_code = error_code
            _log_stream_failure(session_context, error_code, include_exception=False)
            raise
        except Exception:
            status = "failed"
            stream_status = "upstream_failed"
            error_code = (
                "gateway.upstream_unavailable"
                if upstream_read_failed
                else LOCAL_STREAM_PROCESSING_ERROR_CODE
            )
            session_error_code = (
                UPSTREAM_STREAM_FAILURE_CODE
                if upstream_read_failed
                else LOCAL_STREAM_PROCESSING_ERROR_CODE
            )
            _log_stream_failure(session_context, error_code, include_exception=True)
            yield _stream_error_event(error_code, session_context)
        finally:

            async def finalize_body_state() -> None:
                """在 shield 内提交 scan/session、Usage/争议并释放全部请求资源。"""

                nonlocal session_finalized, output_terminal, error_code
                await _cancel_stream_task(read_task)
                await _cancel_stream_task(disconnect_task)
                await _safe_close_stream_response(upstream_response, session_context)
                try:
                    if scanner is not None and output_terminal is None:
                        if (
                            stream_status == "completed"
                            and scan_finished
                            and output_decision is not None
                        ):
                            output_terminal = OutputSafetyTerminalState.from_scan_decision(
                                output_decision,
                                observed_output_token_count=collector.observed_output_token_count,
                                emitted_token_count=emitted_counter.token_count,
                                emitted_sse_event_count=emitted_sse_event_count,
                                emitted_bytes=session.bytes_sent,
                                exposure_state="complete",
                            )
                        else:
                            # timeout/disconnect/read/local failure 在尾窗前终止时
                            # 只能保存 unavailable。
                            output_terminal = _stream_unavailable_terminal(
                                scanner,
                                emitted_counter,
                                observed_output_token_count=collector.observed_output_token_count,
                                emitted_sse_event_count=emitted_sse_event_count,
                                emitted_bytes=session.bytes_sent,
                                reason_code="output_safety.stream_incomplete",
                            )
                    # 尚未由异常分支原子提交的 Session 必须在 shield 内只终结一次。
                    if not session_finalized:
                        session_finalized = safe_finish_stream_session(
                            config,
                            session,
                            status=stream_status,
                            error_code=(
                                session_error_code if session_error_code is not None else error_code
                            ),
                            quality_observation_eligible=(
                                stream_status in {"completed", "timed_out"}
                                or (stream_status == "upstream_failed" and upstream_read_failed)
                            ),
                            **_stream_output_safety_finish_kwargs(
                                output_safety_scan,
                                output_terminal,
                            ),
                        )
                    # 输出策略是 Gateway 本地判定，Provider 已成功产出；亲和状态不得因此受罚。
                    safe_finalize_cache_affinity_selection(
                        config,
                        selection_handle,
                        succeeded=stream_status
                        in {"completed", "output_blocked", "output_safety_unavailable"},
                    )
                    usage = _safe_finish_stream_usage(
                        collector,
                        provider_request.payload,
                        session_context,
                    )
                    final_context = _stream_terminal_context(
                        session_context,
                        stream_status,
                        finalized=session_finalized,
                    )
                    final_context = _context_after_stream_output_safety(
                        final_context,
                        output_safety_scan,
                        output_terminal,
                        finalized=session_finalized,
                    )
                    safe_record_request_log(
                        config,
                        final_context,
                        status=status,
                        error_code=error_code,
                        latency_ms=_latency_ms(started),
                    )
                    if output_safety_scan is None:
                        committed = safe_record_usage_ledger(
                            config,
                            final_context,
                            usage.as_ledger_payload(),
                        )
                        _enqueue_finops_ledger_event(
                            client_request.app.state,
                            committed,
                            final_context,
                        )
                    else:
                        # 原子终结失败时不能把 pending 写入 v5；去掉四字段后仍可靠
                        # 保存真实 Provider Usage。
                        billing_context = (
                            final_context
                            if output_safety_scan.scan_status == "not_configured"
                            or (session_finalized and output_terminal is not None)
                            else _context_without_output_safety(final_context)
                        )
                        result = safe_record_usage_ledger(
                            config,
                            billing_context,
                            usage.as_ledger_payload(),
                            return_durability=True,
                        )
                        # 显式 durability 对象才允许发布 FinOps 或安全事件，未知返回类型失败关闭。
                        if isinstance(result, UsageLedgerWriteResult):
                            _enqueue_finops_ledger_event(
                                client_request.app.state,
                                result.committed,
                                billing_context,
                            )
                            # 阻断 Webhook 只从已提交 Session source event 构造，
                            # 禁止根据正文临时重建。
                            # 缺少已提交来源事件时不得猜测或发布任何租户安全通知。
                            if session.output_safety_event is not None:
                                _enqueue_output_safety_event(
                                    client_request.app.state,
                                    OutputSafetyFinalization(
                                        scan=output_safety_scan,
                                        terminal=output_terminal,
                                        source_event=session.output_safety_event,
                                        idempotent_replay=False,
                                    ),
                                    result,
                                    billing_context,
                                )
                            # 已开始的流无法改 HTTP 状态；Usage 丢失时 FinOps 错误必须覆盖政策错误。
                            if deferred_safety_error and result.durability == "lost":
                                error_code = "finops.usage_record_unavailable"
                                safe_record_request_log(
                                    config,
                                    final_context,
                                    status="failed",
                                    error_code=error_code,
                                    latency_ms=_latency_ms(started),
                                )
                        # 返回类型异常同样表示 durability 无法证明，延迟安全错误升级为 FinOps 失败。
                        elif deferred_safety_error:
                            error_code = "finops.usage_record_unavailable"
                finally:
                    await _safe_release_rate_limit_lease(rate_limit_lease, session_context)

            await _run_stream_finalizer_shielded(
                finalize_body_state(),
                cancellation_already_active=cancellation_in_flight,
            )

        # 仅在连接仍存在时发送唯一安全 SSE error；断连后只保留数据库和 Usage 终态。
        if deferred_safety_error and not disconnect_event.is_set():
            blocked_committed = (
                error_code == "content_policy_violation"
                and session_finalized
                and output_safety_scan is not None
            )
            yield _stream_error_event(
                error_code or "gateway.output_safety_unavailable",
                session_context,
                output_safety_scan_id=(
                    output_safety_scan.output_safety_scan_id if blocked_committed else None
                ),
                already_emitted=(emitted_counter.has_content if blocked_committed else None),
                emitted_token_count=(emitted_counter.token_count if blocked_committed else None),
            )

    return _GatewayStreamingResponse(
        body(),
        disconnect_event=disconnect_event,
        media_type="text/event-stream",
        headers=_contract_probe_tenant_headers(client_request, session_context.tenant_id),
    )


def _stream_session_handshake_facts(error: ApiError) -> tuple[str, bool]:
    """返回 StreamSession 可重建的握手归因码和质量资格，不改变客户端公开错误。

    历史错误码同时被用于真实连接失败和本地异常，0037 无法仅凭原码安全回填质量。
    新请求只把质量白名单内的 ``UpstreamApiError`` 收敛为上游归因码；账号、Key、模型
    映射、请求参数和本地 ``ApiError`` 保留安全诊断码但资格为 false。
    """

    normalized = _attempt_error(error)
    eligible = is_quality_observation_eligible_error(normalized)
    return (UPSTREAM_STREAM_FAILURE_CODE if eligible else normalized.code, eligible)


async def _safe_close_stream_response(
    upstream_response: Any,
    context: RequestLogContext,
) -> None:
    """关闭上游流且不让本地清理异常覆盖已确定的请求终态。

    参数：上游流响应和只含安全元数据的请求上下文。返回值：无。关闭失败只记录固定事件、
    request_id、tenant_id 与异常类型，绝不记录 URL、Header、Prompt、Response 或异常正文。
    """

    try:
        await upstream_response.aclose()
    except Exception as exc:
        # 网络上下文关闭属于本地资源清理；失败可观测但不能跳过 Session、Usage 和租约收口。
        logger.warning(
            "stream.upstream_close_failed",
            extra={
                "event": "stream.upstream_close_failed",
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "tenant_id": context.tenant_id,
                "error_code": LOCAL_STREAM_PROCESSING_ERROR_CODE,
                "payload": {"exception_type": exc.__class__.__name__},
            },
        )


async def _run_stream_finalizer_shielded(
    finalizer: Coroutine[Any, Any, None],
    *,
    cancellation_already_active: bool,
) -> None:
    """在断连 cancel scope 外完成流清理，并正确保留原始取消语义。

    参数：只含资源和安全审计状态的清理协程，以及进入 finally 前是否已捕获取消。返回：无。
    AnyIO shield 处理 Starlette 的 level cancellation；独立 asyncio task 和循环 shield 同时处理
    直接 ``Task.cancel()`` 或清理期间的重复取消。清理结束后，新发生的取消会重新抛出；
    已在传播的取消由原异常分支继续抛出，避免生成第二个无关异常。
    """

    cancellation_observed = False
    current_task = asyncio.current_task()
    # 清理任务在 shield 作用域内创建，使其不继承 Starlette 已取消 task group 的有效取消边界。
    with anyio.CancelScope(shield=True):
        cleanup_task = asyncio.create_task(
            finalizer,
            name="gateway-stream-terminal-finalizer",
        )
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            # 直接 Task.cancel 不受 AnyIO shield 控制；记录后继续等待独立清理任务完成。
            except asyncio.CancelledError:
                cancellation_observed = True
                continue
        # result() 不产生新的 await，能够在已取消作用域内可靠观察清理异常和避免任务告警。
        cleanup_task.result()

    cancellation_requested = bool(current_task and current_task.cancelling())
    if (cancellation_observed or cancellation_requested) and not cancellation_already_active:
        # 正常输出期间新到达的取消必须在清理完成后传播，不能被 shield 永久吞掉。
        raise asyncio.CancelledError


async def _safe_release_rate_limit_lease(
    lease: RateLimitLease | None,
    context: RequestLogContext,
) -> None:
    """尽力释放并发租约，屏蔽实现外泄的非标准清理异常。

    参数：可空租约和安全请求上下文。返回值：无。正常 ``RateLimitLease`` 已自行处理存储不可用；
    此处再隔离测试替身或未来实现的未归一化异常，避免它覆盖客户端断开/上游终态。
    """

    if lease is None:
        return
    try:
        await lease.release()
    except Exception as exc:
        # 只记录异常类型和追踪标识，不输出租约键、Virtual Key 或底层连接详情。
        logger.warning(
            "rate_limit.release_failed",
            extra={
                "event": "rate_limit.release_failed",
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "tenant_id": context.tenant_id,
                "error_code": LOCAL_STREAM_PROCESSING_ERROR_CODE,
                "payload": {"exception_type": exc.__class__.__name__},
            },
        )


def _safe_finish_stream_usage(
    collector: _SseUsageCollector,
    request_payload: Mapping[str, Any],
    context: RequestLogContext,
) -> NormalizedUsage:
    """完成流式计量；本地解析失败时返回不含正文的最低可追踪估算。

    参数：增量计量器、仅驻留内存的请求体和安全上下文。返回值：规范化 Usage。关键约束：
    回退不再次解析请求或上游数据，只保留已经不可逆累计的输出字符估算，确保账本仍可追踪。
    """

    try:
        return collector.finish(request_payload)
    except Exception as exc:
        # 最终 usage 解析发生在响应结束后，不能把本地异常改写为模型质量失败或泄露正文。
        logger.warning(
            "stream.usage_finalize_failed",
            extra={
                "event": "stream.usage_finalize_failed",
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "tenant_id": context.tenant_id,
                "error_code": LOCAL_STREAM_PROCESSING_ERROR_CODE,
                "payload": {"exception_type": exc.__class__.__name__},
            },
        )
        return collector.fallback_usage()


class _UpstreamStreamTimedOut(Exception):
    """内部控制流：当前上游块等待超过配置阈值。"""


class _ClientStreamDisconnected(Exception):
    """内部控制流：ASGI 已确认客户端断开。"""


class _OutputSafetyBlocked(Exception):
    """内部控制流：完整 SSE event 已观察到确定 block，且当前 event 尚未发送。"""

    def __init__(self, decision: OutputScanDecision) -> None:
        """保存不含正文的阻断判定，供 Session/scan 原子终结。"""

        self.decision = decision
        super().__init__("content_policy_violation")


def _stream_unavailable_terminal(
    scanner: SlidingOutputScanner,
    emitted_counter: _OutputTokenCounter,
    *,
    observed_output_token_count: int,
    emitted_sse_event_count: int,
    emitted_bytes: int,
    reason_code: str,
) -> OutputSafetyTerminalState:
    """把未完成流收口为 unavailable，并保留此前已确认的 matched 元数据。

    参数：当前滑窗、Provider 已观察量、已发送计数、事件/字节数和固定 reason。返回：无正文终态。
    clean 不能在尾窗未完成时保留；``OutputSafetyTerminalState`` 会把它降为 unknown。
    """

    previous = scanner.current_decision()
    return OutputSafetyTerminalState.unavailable(
        reason_code,
        previous_decision=previous,
        # 解析器会先观察完整 event，扫描器可能在 event 内的早期 checkpoint 阻断，两者不能混为一谈。
        observed_output_token_count=observed_output_token_count,
        emitted_token_count=emitted_counter.token_count,
        emitted_sse_event_count=emitted_sse_event_count,
        emitted_bytes=emitted_bytes,
        exposure_state="partial" if emitted_counter.has_content else "none",
    )


class _GatewayStreamingResponse(StreamingResponse):
    """把 Starlette 的唯一断连监听桥接为状态机可等待的事件。

    Starlette 在 ASGI spec 2.0-2.3 下已经并发消费 `receive`；业务代码不能再调用
    `Request.is_disconnected()` 争抢同一通道。该覆写保留框架调度，只在收到明确
    `http.disconnect` 时置位事件，使正在等待上游块的任务立即取消并进入统一 finally。
    """

    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        disconnect_event: asyncio.Event,
        media_type: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """保存单连接断开事件及可选安全证明头，并沿用 StreamingResponse 的编码行为。"""

        super().__init__(content, media_type=media_type, headers=headers)
        self.disconnect_event = disconnect_event

    async def listen_for_disconnect(self, receive: Any) -> None:
        """独占读取 ASGI 消息，确认断开后通知流状态机并结束框架监听任务。"""

        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.disconnect_event.set()
                return


async def _cancel_stream_task(task: asyncio.Task[Any] | None) -> None:
    """取消并消费内部子任务结果，避免遗留任务和未读取异常告警。

    这里只处理当前状态机自己创建的任务。外层协程收到的 CancelledError 仍由调用栈传播，
    不能在此辅助函数中吞掉客户端取消语义。
    """

    if task is None:
        return
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _stream_error_event(
    error_code: str,
    context: RequestLogContext,
    *,
    output_safety_scan_id: str | None = None,
    already_emitted: bool | None = None,
    emitted_token_count: int | None = None,
) -> bytes:
    """构造不含上游详情和正文的标准 SSE error event。

    参数：统一错误码、请求上下文，以及仅对已提交 output block 开放的暴露摘要。
    返回：UTF-8 SSE bytes。
    关键约束：三个输出安全字段必须成组出现，其他错误不能借该入口暴露 scan 身份。
    """

    output_values = (output_safety_scan_id, already_emitted, emitted_token_count)
    if any(value is not None for value in output_values):
        if (
            error_code != "content_policy_violation"
            or not isinstance(output_safety_scan_id, str)
            or not output_safety_scan_id.startswith("oss_")
            or type(already_emitted) is not bool
            or type(emitted_token_count) is not int
            or emitted_token_count < 0
        ):
            # 非法组合属于内部编排错误，使用更保守的安全不可用事件而不输出半组字段。
            error_code = "gateway.output_safety_unavailable"
            output_safety_scan_id = None
            already_emitted = None
            emitted_token_count = None

    error = ApiError(error_code)
    error_payload: dict[str, object] = {
        "code": error.code,
        "message": error.message,
        "request_id": context.request_id,
        "trace_id": context.trace_id,
    }
    if output_safety_scan_id is not None:
        # 只返回客户已经知道的暴露边界；规则、pattern、Provider 和命中正文均不进入事件。
        error_payload.update(
            {
                "output_safety_scan_id": output_safety_scan_id,
                "already_emitted": already_emitted,
                "emitted_token_count": emitted_token_count,
            }
        )
    payload = {
        "error": {
            **error_payload,
        }
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: error\ndata: {data}\n\n".encode()


def _stream_terminal_context(
    context: RequestLogContext,
    stream_status: str,
    *,
    finalized: bool,
) -> RequestLogContext:
    """把已提交会话终态写入日志上下文，提交失败时移除成对关联字段。

    0027 触发器要求 `stream_session_id/stream_status` 同时出现且与会话终态一致。终态数据库
    写入失败时仍应尽量保存基础 request_log，因此两个流字段都置空，由 request_id 继续排障。
    """

    if finalized:
        return replace(context, stream_status=stream_status)
    # 会话终态未提交时成对移除关联字段，避免 0027 一致性触发器拒绝基础失败日志。
    return replace(context, stream_session_id=None, stream_status=None)


class _OutputTokenCounter:
    """只累计已经通过门禁并交给响应生成器的模型文本估算量。"""

    def __init__(self) -> None:
        """初始化不可逆 CJK/其他字符计数，不保存任何输出字符串。"""

        self._cjk_chars = 0
        self._other_nonspace_chars = 0
        self._has_content = False

    def observe_fragments(self, fragments: tuple[Any, ...]) -> None:
        """消费已允许 frame 的短期 fragments，并立即丢弃字符引用。

        参数：Shared framer 返回的 ``OutputFragment`` tuple。返回值：无。
        空白正文也会形成客户暴露事实，但 Token 估算仍按既有口径忽略空白。
        """

        for fragment in fragments:
            text = fragment.text
            if text:
                self._has_content = True
            for char in text:
                if char.isspace():
                    continue
                if "\u4e00" <= char <= "\u9fff":
                    self._cjk_chars += 1
                else:
                    self._other_nonspace_chars += 1

    @property
    def token_count(self) -> int:
        """返回已发送模型正文的确定性近似 Token 数。"""

        return self._cjk_chars + (self._other_nonspace_chars + 3) // 4

    @property
    def has_content(self) -> bool:
        """返回是否至少发送过一个非空模型文本 fragment。"""

        return self._has_content


class _SseUsageCollector:
    """增量解析 SSE 中的安全计量字段，不缓存完整流或完整输出正文。"""

    # OpenAI usage chunk 很小；限制单事件大小可避免异常 Provider 用无换行正文撑爆网关内存。
    _MAX_EVENT_BYTES = 1024 * 1024

    def __init__(self, adapter: ProviderAdapter) -> None:
        """初始化单次流的增量缓冲、Adapter 和不可逆输出字符计数器。"""

        self._adapter = adapter
        self._line_buffer = bytearray()
        self._event_data = bytearray()
        self._discard_event = False
        self._latest_usage: NormalizedUsage | None = None
        self._output_cjk_chars = 0
        self._output_non_cjk_chars = 0

    def feed(self, chunk: bytes) -> None:
        """消费任意边界的网络 chunk；解析失败不影响原始 SSE 透传。"""

        self._line_buffer.extend(chunk)
        while True:
            newline = self._line_buffer.find(b"\n")
            if newline < 0:
                # 没有事件边界时同样执行内存上限，超限内容仅停止解析，仍由调用方原样透传。
                if len(self._line_buffer) > self._MAX_EVENT_BYTES:
                    self._line_buffer.clear()
                    self._discard_event = True
                return
            line = bytes(self._line_buffer[:newline]).rstrip(b"\r")
            del self._line_buffer[: newline + 1]
            self._consume_line(line)

    def observe_frame(self, frame: SseFrame) -> None:
        """从 Shared 已严格验证的完整 event 观察 Usage 与模型输出计数。

        参数：已通过 UTF-8、JSON 和 duplicate-key 门禁的 frame。返回值：无。
        关键约束：模型文本直接从 exact fragments 计数；JSON 仅重读 usage 子对象，
        且从不持久化整个 event。
        """

        payload: Mapping[str, Any] | None = None
        if frame.data_text and not frame.is_done:
            try:
                candidate = json.loads(frame.data_text)
            # Shared 已严格解析；防御性失败只放弃 Usage 自报值，不能改变已经成立的输出安全判定。
            except (json.JSONDecodeError, UnicodeError, ValueError):
                candidate = None
            if isinstance(candidate, Mapping):
                payload = candidate
        if payload is not None:
            # Adapter 只读取 usage 子对象并执行脱敏，不会保存完整 SSE payload。
            usage = self._adapter.parse_usage(payload)
            if not usage.estimated or usage.provider_raw_usage:
                self._latest_usage = usage
        for fragment in frame.fragments:
            self._count_output(fragment.text)

    def finish(self, request_payload: Mapping[str, Any]) -> NormalizedUsage:
        """流终止时返回上游 usage，缺失时仅按请求和已输出字符计数估算。"""

        if self._line_buffer and not self._discard_event:
            self._consume_line(bytes(self._line_buffer).rstrip(b"\r"))
        self._line_buffer.clear()
        self._dispatch_event()

        # Provider 最终 usage 是权威口径，只有缺失时才允许本地字符估算补位。
        if self._latest_usage is not None and not self._latest_usage.estimated:
            return self._latest_usage
        base_usage = self._latest_usage or self._adapter.parse_usage({})
        output_tokens = self._output_cjk_chars + (self._output_non_cjk_chars + 3) // 4
        # 回退函数只接收数值计数和内存请求体，不会把请求或输出正文写入账本。
        return estimate_stream_usage(request_payload, output_tokens, base_usage)

    def fallback_usage(self) -> NormalizedUsage:
        """在最终解析故障时仅凭累计字符返回保守 Usage，不再次接触正文。

        参数：无。返回值：输入 Token 为零、输出 Token 为字符估算的 ``NormalizedUsage``。
        该口径显式标记为 estimated，既不伪造上游 usage，也保证失败请求仍有可查询账本记录。
        """

        output_tokens = self._output_cjk_chars + (self._output_non_cjk_chars + 3) // 4
        return NormalizedUsage(
            output_tokens=output_tokens,
            total_tokens=output_tokens,
            estimated=True,
            estimate_reason="stream_usage_finalize_failed",
            normalization_warnings=("stream_usage_finalize_failed",),
        )

    @property
    def observed_output_token_count(self) -> int:
        """返回 Gateway 已完整解析事件中的模型文本估算量，包括尚未放行的命中 event。"""

        return self._output_cjk_chars + (self._output_non_cjk_chars + 3) // 4

    def _consume_line(self, line: bytes) -> None:
        """消费一行 SSE 字段，只聚合 data 内容并在空行边界派发事件。"""

        if not line:
            self._dispatch_event()
            self._discard_event = False
            return
        if self._discard_event or line.startswith(b":"):
            return
        field, separator, value = line.partition(b":")
        if field != b"data":
            return
        if separator and value.startswith(b" "):
            value = value[1:]
        projected_size = len(self._event_data) + len(value) + 1
        if projected_size > self._MAX_EVENT_BYTES:
            self._event_data.clear()
            self._discard_event = True
            return
        if self._event_data:
            self._event_data.extend(b"\n")
        self._event_data.extend(value)

    def _dispatch_event(self) -> None:
        """解析当前 SSE 事件，提取 usage 并把输出文本立即折算为不可逆字符计数。"""

        if not self._event_data or self._discard_event:
            self._event_data.clear()
            return
        data = bytes(self._event_data)
        self._event_data.clear()
        if data.strip() == b"[DONE]":
            return
        try:
            payload = json.loads(data.decode("utf-8"))
        # 单个上游事件编码或 JSON 异常不能中断原始 SSE 透传，只放弃该事件的计量解析。
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, Mapping):
            return

        # parse_usage 内部只提取并脱敏 usage 子对象，绝不把整个事件当作 raw_usage 保存。
        usage = self._adapter.parse_usage(payload)
        if not usage.estimated or usage.provider_raw_usage:
            self._latest_usage = usage
        for text in _stream_output_texts(payload):
            self._count_output(text)

    def _count_output(self, text: str) -> None:
        """按中日韩字符和其他字符分类累计估算量，调用结束后不保留输入文本。"""

        # 字符读取后立即折算并丢弃，collector 中不存在可还原的响应正文。
        for char in text:
            if char.isspace():
                continue
            if "\u4e00" <= char <= "\u9fff":
                self._output_cjk_chars += 1
            else:
                self._output_non_cjk_chars += 1


def _stream_output_texts(payload: Mapping[str, Any]) -> list[str]:
    """只返回当前 SSE 事件中的文本片段，调用方必须立即计数且不得持久化。"""

    texts: list[str] = []
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return texts
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        delta = choice.get("delta")
        if isinstance(delta, Mapping):
            texts.extend(_content_texts(delta.get("content")))
        texts.extend(_content_texts(choice.get("text")))
    return texts


def _content_texts(value: object) -> list[str]:
    """兼容 OpenAI 文本和 content parts，忽略图片、工具参数等非输出正文。"""

    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for part in value:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return texts


def _log_stream_failure(
    context: RequestLogContext,
    error_code: str,
    *,
    include_exception: bool,
) -> None:
    """记录不含 Provider Key、请求头、Prompt 或 SSE 正文的流失败事件。"""

    # 即使调用点正在处理异常也不使用 logger.exception；traceback 可能携带上游 URL 或异常正文。
    logger.warning(
        "gateway.stream_failed",
        extra={
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "event": "gateway.stream_failed",
            "tenant_id": context.tenant_id,
            "model_id": context.model_id,
            "provider_id": context.provider_id,
            "error_code": error_code,
            "payload": {"exception_captured": include_exception},
        },
    )


async def _read_json_body(request: Request) -> dict[str, Any]:
    """在 网关数据面 内部复用 read json body 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    try:
        body = await request.json()
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except ValueError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("gateway.request_invalid") from exc
    if not isinstance(body, dict):
        raise ApiError("gateway.request_invalid")
    return body


def _model_alias(payload: dict[str, Any]) -> str:
    """从 OpenAI-compatible 请求体中提取用户请求的模型别名。"""

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        # 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
        raise ApiError("gateway.model_missing")
    return model.strip()


def _validate_payload(request_kind: str, payload: dict[str, Any]) -> None:
    """按接口类型做最小请求校验。"""

    if request_kind == "chat":
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
            raise ApiError("gateway.messages_missing")
        for message in messages:
            # 只校验结构，不读取或记录 content 内容，避免 Prompt 明文进入日志路径。
            if not isinstance(message, dict):
                raise ApiError("gateway.messages_missing")
            if not isinstance(message.get("role"), str) or "content" not in message:
                # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
                raise ApiError("gateway.messages_missing")
        return

    if payload.get("stream") is True:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("gateway.stream_not_supported")
    if "input" not in payload:
        raise ApiError("gateway.input_missing")
    input_value = payload["input"]
    if isinstance(input_value, str):
        if input_value == "":
            # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
            raise ApiError("gateway.input_missing")
        return
    if isinstance(input_value, list) and input_value:
        return
    # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
    raise ApiError("gateway.input_missing")


def _validate_route_capability(
    request_kind: str,
    payload: dict[str, Any],
    route: GatewayRoute,
) -> None:
    """校验已解析模型是否支持当前接口能力。"""

    capabilities = route.capabilities
    if request_kind == "chat" and capabilities.get("chat") is False:
        # 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
        raise ApiError("gateway.model_not_found")
    if request_kind == "embeddings" and capabilities.get("embeddings") is False:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("gateway.model_not_found")
    if (
        request_kind == "chat"
        and payload.get("stream") is True
        and capabilities.get("stream") is False
    ):
        # 已解析模型明确不支持 SSE 时必须在 Provider 前拒绝，并沿用统一流能力错误码，
        # 不能让 Adapter 尝试私有扩展或把不兼容响应伪装成流。
        raise ApiError("gateway.stream_not_supported")


def _ensure_environment_active(auth_context: GatewayAuthContext) -> None:
    """在进入请求体解析和模型路由前阻断禁用 Environment。

    鉴权函数先恢复可信 Key/租户/环境上下文，本判断随后立即执行；这样既不允许禁用环境
    继续调用上游，又能让异常分支写入带环境的 request_log。
    """

    # 环境状态是数据面访问控制边界，不能依赖控制台隐藏 Key 或别名选项。
    if auth_context.environment_status != "active":
        raise ApiError("gateway.environment_disabled")


def _request_log_context(
    request: Request,
    auth_context: GatewayAuthContext,
    route: GatewayRoute,
    *,
    endpoint: str,
    request_model: str,
) -> RequestLogContext:
    """在 网关数据面 内部复用 request log context 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return RequestLogContext(
        tenant_id=auth_context.tenant_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        virtual_key_id=auth_context.virtual_key_id,
        environment_id=auth_context.environment_id,
        environment_code=auth_context.environment,
        project_id=auth_context.project_id,
        application_id=auth_context.application_id,
        provider_id=route.provider_id,
        model_id=route.model_id,
        endpoint=_canonical_request_log_endpoint(endpoint),
        request_model=route.requested_model or request_model,
        resolved_model=route.model_alias,
        provider_code=route.provider_code,
    )


def _partial_request_log_context(
    request: Request,
    auth_context: GatewayAuthContext | None,
    route: GatewayRoute | None,
    *,
    endpoint: str,
    request_model: str | None,
) -> RequestLogContext | None:
    """在异常路径构造尽可能完整的调用日志上下文。

    鉴权失败时无法可信地确定 tenant_id，因此不能写业务日志；只要鉴权成功，
    即使请求体验证或路由失败，也应写入失败记录，满足 PRD-016 的最小闭环。
    """

    # 关键控制流影响权限、审计或错误归因，必须保留统一错误语义。
    if auth_context is None:
        return None
    return RequestLogContext(
        tenant_id=auth_context.tenant_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        virtual_key_id=auth_context.virtual_key_id,
        environment_id=auth_context.environment_id,
        environment_code=auth_context.environment,
        project_id=auth_context.project_id,
        application_id=auth_context.application_id,
        provider_id=route.provider_id if route else None,
        model_id=route.model_id if route else None,
        endpoint=_canonical_request_log_endpoint(endpoint),
        request_model=(route.requested_model if route else request_model),
        resolved_model=route.model_alias if route else None,
        provider_code=route.provider_code if route else None,
    )


def _canonical_request_log_endpoint(endpoint: str) -> str:
    """把公开相对路径和内部绝对路径统一为稳定审计入口。

    ``endpoint`` 只来自服务端路由编排：公开 OpenAI-compatible 入口传相对路径，必须补
    ``/v1/``；PRD-066 内部入口传以 ``/`` 开头的固定绝对路径，必须原样保留。这里不做
    URL 解码或客户端输入归一化，避免审计身份与实际注册路由发生漂移。
    """

    # 内部绝对入口已经是 canonical 路由；再次添加公开前缀会制造 ``/v1//internal``，
    # 让结构化事件和 RequestLog 对同一调用产生错误身份。
    if endpoint.startswith("/"):
        return endpoint
    return f"/v1/{endpoint}"


def _context_with_policy_lease(
    context: RequestLogContext,
    lease: PolicySnapshotLeaseDecision,
) -> RequestLogContext:
    """把读取正文前取得的快照版本与状态冻结到最小日志上下文。"""

    return replace(
        context,
        snapshot_version=lease.snapshot_version,
        policy_snapshot_status=lease.status,
    )


async def _acquire_provider_error_map_lease(
    request: Request,
    tenant_id: str,
) -> ProviderErrorMapLease:
    """在鉴权后、正文前取得一次租户错误映射租约并冻结到当前 ASGI 请求。

    参数：当前请求和可信 tenant_id。返回：不可变租约。缓存缺失、关闭或返回异常租户时
    使用当前租户 builtin 基线，避免诊断配置故障阻断模型热路径，同时绝不复用跨租户制品。
    """

    existing = getattr(request.state, "provider_error_map_lease", None)
    # 同一请求仅复用认证租户完全一致的不可变引用，避免主调用与 Fallback 观察不同版本。
    if isinstance(existing, ProviderErrorMapLease) and existing.tenant_id == tenant_id:
        return existing
    cache = getattr(request.app.state, "provider_error_map_cache", None)
    if cache is None:
        lease = builtin_provider_error_map_lease(tenant_id)
    else:
        lease = await cache.acquire(tenant_id)
        # 缓存结果类型或租户异常时按可信 tenant 降级，绝不能把跨租户制品冻结到请求。
        if not isinstance(lease, ProviderErrorMapLease) or lease.tenant_id != tenant_id:
            # 应用状态损坏或错误 cache 结果不可进入请求；按正确租户 builtin 降级而不是阻断调用。
            lease = builtin_provider_error_map_lease(tenant_id)
    request.state.provider_error_map_lease = lease
    return lease


def _provider_error_map_lease_for_request(
    request: Request,
    tenant_id: str,
) -> ProviderErrorMapLease:
    """复制当前请求已冻结的租约引用，兼容直接调用内部构造器的旧测试。

    完整 HTTP 流程必定已在鉴权后设置 Request.state；测试或内部兼容入口若绕过该阶段，只能
    得到当前路由 tenant 的 builtin 租约。租户不一致时绝不沿用旧引用。
    """

    lease = getattr(request.state, "provider_error_map_lease", None)
    # 兼容入口也只能读取同租户冻结值，测试或内部调用不得借旧 state 跨租户复用。
    if isinstance(lease, ProviderErrorMapLease) and lease.tenant_id == tenant_id:
        return lease
    lease = builtin_provider_error_map_lease(tenant_id)
    request.state.provider_error_map_lease = lease
    return lease


async def _acquire_policy_snapshot_lease(
    request: Request,
    tenant_id: str,
) -> PolicySnapshotLeaseDecision:
    """从应用级缓存取得租约，并为测试/显式关闭同步提供确定性基线。

    正常进程由 lifespan 安装缓存。手工装配 router 的单元测试在同步关闭时仍可使用
    ``disabled`` 基线；若配置要求同步但缓存缺失，则必须失败关闭，不能绕过已发布策略。
    """

    config = _config(request)
    cache = getattr(request.app.state, "policy_snapshot_cache", None)
    if cache is None:
        if not config.policy_snapshot_sync_enabled:
            return PolicySnapshotLeaseDecision(
                executable=True,
                snapshot=None,
                snapshot_version=None,
                status="disabled",
                error_code=None,
                acquired_monotonic=time.monotonic(),
            )
        return PolicySnapshotLeaseDecision(
            executable=False,
            snapshot=None,
            snapshot_version=None,
            status="unavailable",
            error_code="gateway.policy_snapshot_unavailable",
            acquired_monotonic=time.monotonic(),
        )
    decision = await cache.acquire(tenant_id)
    if not isinstance(decision, PolicySnapshotLeaseDecision):
        # 应用状态被错误覆盖时按不可用处理，避免未知对象被当作可执行快照。
        return PolicySnapshotLeaseDecision(
            executable=False,
            snapshot=None,
            snapshot_version=None,
            status="unavailable",
            error_code="gateway.policy_snapshot_unavailable",
            acquired_monotonic=time.monotonic(),
        )
    return decision


def _log_request_received(
    request: Request,
    auth_context: GatewayAuthContext,
    endpoint: str,
    model_alias: str,
    route: GatewayRoute,
    stream: bool,
) -> None:
    """记录请求进入数据面的安全元数据。"""

    # 这里刻意只记路由元数据，不记录 prompt、input、Authorization 或 Provider Key。
    logger.info(
        "request.received",
        extra={
            "request_id": _request_id(request),
            "trace_id": _trace_id(request),
            "service_name": _config(request).service_name,
            "event": "request.received",
            "tenant_id": auth_context.tenant_id,
            "actor_id": auth_context.virtual_key_id,
            "object_id": _request_id(request),
            "payload": {
                "endpoint": _canonical_request_log_endpoint(endpoint),
                "requested_model": model_alias,
                "selected_model_id": route.model_id,
                "selected_internal_model_id": route.model_alias,
                "provider_id": route.provider_id,
                "provider_code": route.provider_code,
                "routing_reason": route.routing_reason,
                "environment_id": auth_context.environment_id,
                "environment": auth_context.environment,
                "project_id": auth_context.project_id,
                "application_id": auth_context.application_id,
                "stream": stream,
            },
        },
    )


def _config(request: Request) -> AppConfig:
    """读取 FastAPI 应用启动时加载的配置。"""

    return request.app.state.config


def _request_id(request: Request) -> str:
    """读取当前请求的 request_id，缺失时按统一规则生成。"""

    return getattr(
        request.state, "request_id", resolve_request_id(_headers(request).get("X-Request-ID"))
    )


def _trace_id(request: Request) -> str:
    """读取当前请求的 trace_id，缺失时以 request_id 为基础生成。"""

    return getattr(
        request.state,
        "trace_id",
        resolve_trace_id(_headers(request).get("X-Trace-ID"), _request_id(request)),
    )


def _headers(request: Request) -> Headers:
    """返回请求头对象，便于 request_id/trace_id 辅助函数复用。"""

    return request.headers


def _latency_ms(started: float) -> int:
    """在 网关数据面 内部复用 latency ms 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return int((time.perf_counter() - started) * 1000)
