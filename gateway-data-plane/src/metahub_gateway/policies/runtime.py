"""把已冻结 Policy 快照转换为可信上下文并执行 shared 纯求值器。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Literal

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.policy_dsl import (
    PolicyContextError,
    PolicyEvaluationInput,
    evaluate_policy_set,
)

from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.budgets import load_policy_budget_remaining
from metahub_gateway.usage.ledger import RequestLogContext

from .cache import PolicySnapshotLeaseDecision
from .redaction import (
    PolicyMaskUnsupportedError,
    policy_mask_runtime_self_test,
    redact_policy_payload,
)

logger = logging.getLogger("metahub.gateway.policies")


@dataclass(frozen=True, slots=True)
class PolicyExecutionResult:
    """保存一次快照纯求值的安全决定摘要。

    结构只含稳定 ID、动作和 reason；comparison 实际值、DSL、AST、Prompt 和 Response
    均不跨越运行时边界，也不会进入 request_logs。
    """

    decision: str
    reason_code: str
    policy_id: str | None = None
    action_type: str | None = None
    target_model_id: str | None = None
    timed_out: bool = False
    missing_variable: str | None = None
    alert_required: bool = False
    # mask 是非终止修饰事实；只保存第一个稳定 Policy ID，不携带命中文本或位置。
    mask_required: bool = False
    mask_policy_id: str | None = None


def evaluate_policy_snapshot(
    config: AppConfig,
    lease: PolicySnapshotLeaseDecision,
    auth_context: GatewayAuthContext,
    requested_model: str,
) -> PolicyExecutionResult:
    """使用已认证上下文和不可变 lease 执行当前请求的 Policy 集。

    参数：requested_model 已通过服务端模型字段校验；tenant/env/project 只来自认证上下文。
    返回：安全执行摘要，调用方随后先写入日志上下文，再根据 ``policy_execution_error``
    阻断或继续路由。约束：仅快照实际引用预算变量时才做无副作用余额读取。
    """

    snapshot = lease.snapshot
    if snapshot is None or not snapshot.policies:
        return PolicyExecutionResult("allow", "policy.no_snapshot_default_allow")
    context: dict[str, object] = {
        "tenant": auth_context.tenant_id,
        "env": auth_context.environment,
        # PRD-044 已固定无 Project 时使用空串，不能改成缺失值而改变历史 DSL 语义。
        "project": auth_context.project_id or "",
        "model": requested_model,
    }
    if any("budget_remaining" in item.variables for item in snapshot.policies):
        remaining = load_policy_budget_remaining(config, auth_context)
        if remaining is not None:
            context["budget_remaining"] = remaining

    policies = tuple(
        PolicyEvaluationInput(
            policy_id=item.policy_id,
            policy_rule_id=item.policy_rule_id,
            policy_action_id=item.policy_action_id,
            priority=item.priority,
            compiled_expression=item.compiled_expression,
            action_type=item.action_type,
            target_model_id=item.target_model_id,
        )
        for item in snapshot.policies
    )
    try:
        evaluated = evaluate_policy_set(
            policies,
            context,
            timeout_ms=config.policy_evaluation_timeout_ms,
        )
    # 上下文类型不满足 DSL 合同时按缺失处理，避免异常值绕过策略并进入后续路由链路。
    except PolicyContextError as exc:
        # 可信上下文的类型异常属于防御性拒绝；只保留白名单变量名，不记录实际值。
        return PolicyExecutionResult(
            "deny",
            "policy.context_missing",
            missing_variable=exc.variable,
        )
    reason = evaluated.policy_reason
    first_mask = evaluated.mask_matches[0] if evaluated.mask_matches else None
    return PolicyExecutionResult(
        decision=evaluated.decision,
        reason_code=evaluated.reason_code,
        policy_id=reason.policy_id if reason is not None else None,
        action_type=reason.action_type if reason is not None else None,
        target_model_id=evaluated.target_model_id,
        timed_out=evaluated.timed_out,
        missing_variable=evaluated.missing_variable,
        alert_required=evaluated.alert_required,
        mask_required=evaluated.mask_required,
        mask_policy_id=first_mask.policy_id if first_mask is not None else None,
    )


def apply_policy_execution_result(
    context: RequestLogContext,
    result: PolicyExecutionResult,
) -> RequestLogContext:
    """把安全 Policy 摘要复制到请求日志上下文，保留此前冻结的 version/status。"""

    return replace(
        context,
        policy_decision=result.decision,
        policy_reason_code=result.reason_code,
        policy_id=result.policy_id,
        policy_action_type=result.action_type,
        policy_target_model_id=result.target_model_id,
        policy_mask_applied=False,
        policy_mask_policy_id=result.mask_policy_id,
    )


def apply_policy_mask_payload(
    config: AppConfig,
    context: RequestLogContext,
    result: PolicyExecutionResult,
    payload: dict[str, Any],
    *,
    request_kind: Literal["chat", "embeddings"],
) -> tuple[RequestLogContext, dict[str, Any]]:
    """在最终策略允许继续后执行一次 copy-on-write 脱敏。

    参数：冻结配置、已带 mask ID 的日志上下文、纯求值结果和已校验 payload。返回：
    更新安全 trace 的上下文与新 payload。关键约束：门禁、自检、稳定 Policy ID 或完整遍历
    任一失败都抛统一错误，调用方不得继续路由或 Provider 链路。
    """

    if not result.mask_required:
        return context, payload
    if (
        not config.policy_mask_runtime_enabled
        or not policy_mask_runtime_self_test()
        or result.mask_policy_id is None
    ):
        # 配置/执行器/制品证明任一缺失都按内部能力不可用处理，不能降级为发送原始正文。
        raise ApiError("gateway.policy_mask_unavailable")
    try:
        redacted = redact_policy_payload(request_kind, payload)
    # 结构异常只映射固定 422；异常正文可能随实现变化，绝不能进入日志或 API suggestion。
    except PolicyMaskUnsupportedError as exc:
        raise ApiError("gateway.policy_mask_unsupported") from exc
    return replace(context, policy_mask_applied=True), redacted


def policy_execution_error(
    result: PolicyExecutionResult,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    snapshot_version: int | None,
) -> ApiError | None:
    """把 fail-closed 决策映射为统一 API 错误，并输出不含 Policy 正文的超时告警。"""

    if result.timed_out:
        logger.warning(
            "policy.evaluation_timeout",
            extra={
                "event": "policy.evaluation_timeout",
                "tenant_id": tenant_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "payload": {"snapshot_version": snapshot_version},
            },
        )
        return ApiError("gateway.policy_evaluation_timeout")
    if result.missing_variable is not None or result.reason_code == "policy.context_missing":
        return ApiError("gateway.policy_context_missing")
    if result.decision == "deny":
        return ApiError("gateway.policy_denied")
    # route_to 没有目标模型属于损坏制品，必须拒绝而不能静默退回客户端请求模型。
    if result.decision == "route_to" and not result.target_model_id:
        return ApiError("gateway.policy_action_unsupported")
    # 任何未知动作都按不支持失败关闭，防止新旧执行器版本差异意外放行请求。
    if result.decision not in {"allow", "route_to"}:
        return ApiError("gateway.policy_action_unsupported")
    return None
