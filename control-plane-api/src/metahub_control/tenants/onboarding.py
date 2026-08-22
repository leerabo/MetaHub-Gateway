"""Tenant-scoped first-use onboarding status.

The endpoint intentionally returns only safe readiness facts.  It is an admin
view, but it must not become a second credential or request-content endpoint.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ConfigDict, Field

from metahub_control.auth import AuthContext
from metahub_control.workspace_onboarding.models import WorkspaceOnboardingResponse
from metahub_control.workspace_onboarding.service import get_legacy_workspace_onboarding

OnboardingStepStatus = Literal["complete", "current", "blocked", "optional"]


class OnboardingStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    order: int = Field(ge=1)
    status: OnboardingStepStatus
    title: str
    description: str
    target_hash: str
    action_label: str
    blocking_reason: str | None = None


class TenantOnboardingSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    completed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=0)
    is_complete: bool
    steps: list[OnboardingStep]
    request_id: str
    trace_id: str


_MANDATORY_STEP_IDS = (
    "tenant-profile",
    "environment",
    "provider-keypool",
    "model-alias",
    "project-application",
    "virtual-key",
    "first-governed-call",
    "usage-evidence",
)


def get_onboarding_summary(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> TenantOnboardingSummaryResponse:
    """Return the current tenant's authoritative initialization progress.

    Every predicate is constrained by ``context.user.tenant_id``.  The query
    returns no names, URLs, credentials, prompts, responses, SQL, or paths.
    """

    tenant_id = context.user.tenant_id
    workspace_summary = get_legacy_workspace_onboarding(
        config,
        context,
        request_id=request_id,
        trace_id=trace_id,
    )
    if workspace_summary is not None:
        return _map_workspace_summary(tenant_id, workspace_summary)
    try:
        connection = connect_database(config.database_url)
    except DbError as exc:
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc

    try:
        facts = _load_readiness_facts(connection, tenant_id)
    except sqlite3.Error as exc:
        raise ApiError("tenant.onboarding_unavailable") from exc
    finally:
        connection.close()

    steps = _build_steps(tenant_id, facts)
    mandatory = [step for step in steps if step.id in _MANDATORY_STEP_IDS]
    completed_steps = sum(step.status == "complete" for step in mandatory)
    return TenantOnboardingSummaryResponse(
        tenant_id=tenant_id,
        completed_steps=completed_steps,
        total_steps=len(mandatory),
        is_complete=completed_steps == len(mandatory),
        steps=steps,
        request_id=request_id,
        trace_id=trace_id,
    )


def _map_workspace_summary(
    tenant_id: str,
    summary: WorkspaceOnboardingResponse,
) -> TenantOnboardingSummaryResponse:
    """Map the shared eight-step evaluator to the frozen Legacy Console DTO."""

    by_id = {step.id: step for step in summary.steps}
    mappings = (
        ("tenant-profile", "workspace_profile", "准备企业资料", "#tenant", "查看企业设置"),
        ("environment", "environment", "准备开发环境", "#environments", "配置环境"),
        (
            "provider-keypool",
            "provider",
            "接入模型服务",
            "#providers",
            "配置模型服务",
        ),
        ("model-alias", "model_alias", "准备模型别名", "#model-aliases", "配置模型别名"),
        (
            "project-application",
            "project_application",
            "建立业务归属",
            "#projects",
            "创建项目和应用",
        ),
        ("virtual-key", "virtual_key", "创建调用凭据", "#virtual-keys", "创建 Virtual Key"),
        (
            "first-governed-call",
            "first_call",
            "完成首次受控调用",
            "#quickstart",
            "发起首次调用",
        ),
        ("usage-evidence", "first_call", "核对用量证据", "#usage", "查看用量"),
    )
    steps: list[OnboardingStep] = []
    for order, (legacy_id, current_id, title, target, action) in enumerate(mappings, start=1):
        current = by_id[current_id]
        status: OnboardingStepStatus
        if current.status == "complete":
            status = "complete"
        elif current.status == "blocked":
            status = "blocked"
        else:
            status = "current"
        steps.append(
            OnboardingStep(
                id=legacy_id,
                order=order,
                status=status,
                title=title,
                description="按当前 Workspace 的真实配置和验证事实完成此步骤。",
                target_hash=target,
                action_label=action,
                blocking_reason=(
                    "请先完成前置初始化步骤。" if status == "blocked" else None
                ),
            )
        )
    for legacy_id, title, target, action in (
        ("policy", "配置安全策略", "#policies", "配置策略"),
        ("budget", "设置预算约束", "#budgets", "设置预算"),
        ("team", "邀请协作成员", "#users", "管理成员"),
    ):
        steps.append(
            OnboardingStep(
                id=legacy_id,
                order=len(steps) + 1,
                status="optional",
                title=title,
                description="可在完成基础初始化后继续配置。",
                target_hash=target,
                action_label=action,
            )
        )
    completed = sum(step.status == "complete" for step in steps[:8])
    return TenantOnboardingSummaryResponse(
        tenant_id=tenant_id,
        completed_steps=completed,
        total_steps=8,
        is_complete=completed == 8,
        steps=steps,
        request_id=summary.request_id,
        trace_id=summary.trace_id,
    )


def _load_readiness_facts(connection: sqlite3.Connection, tenant_id: str) -> dict[str, bool]:
    """Load one tenant-scoped readiness snapshot using safe aggregate facts."""

    row = connection.execute(
        """
        SELECT
          EXISTS(
            SELECT 1 FROM tenants
            WHERE tenant_id=? AND status='active' AND length(trim(name))>0
          ) AS tenant_ready,
          EXISTS(
            SELECT 1 FROM environments
            WHERE tenant_id=? AND code='dev' AND status='active'
          ) AS environment_ready,
          EXISTS(
            SELECT 1
            FROM providers provider
            WHERE provider.tenant_id=? AND provider.status='active'
              AND (
                (provider.provider_type='local_endpoint'
                 AND provider.auth_type IN ('none', 'no_auth'))
                OR EXISTS(
                  SELECT 1 FROM provider_credentials credential
                  WHERE credential.tenant_id=provider.tenant_id
                    AND credential.provider_id=provider.provider_id
                    AND credential.status='active'
                )
              )
          ) AS provider_keypool_ready,
          EXISTS(
            SELECT 1
            FROM model_aliases alias_row
            JOIN models model
              ON model.tenant_id=alias_row.tenant_id
             AND model.model_id=alias_row.default_model_id
            WHERE alias_row.tenant_id=?
              AND alias_row.environment='dev'
              AND alias_row.status='active'
              AND model.status='active'
          ) AS model_alias_ready,
          EXISTS(
            SELECT 1 FROM projects project
            JOIN applications application
              ON application.tenant_id=project.tenant_id
             AND application.project_id=project.project_id
            WHERE project.tenant_id=?
              AND project.status='active'
              AND application.status='active'
          ) AS project_application_ready,
          EXISTS(
            SELECT 1 FROM virtual_keys
            WHERE tenant_id=? AND status='active'
          ) AS virtual_key_ready,
          EXISTS(
            SELECT 1 FROM request_logs
            WHERE tenant_id=? AND status='success'
          ) AS successful_call,
          EXISTS(
            SELECT 1 FROM usage_ledger
            WHERE tenant_id=?
          ) AS usage_ready,
          EXISTS(
            SELECT 1 FROM policies
            WHERE tenant_id=? AND status='active'
          ) AS policy_ready,
          EXISTS(
            SELECT 1 FROM budget_rules
            WHERE tenant_id=? AND status='active'
          ) AS budget_ready,
          (
            SELECT COUNT(*) FROM users
            WHERE tenant_id=? AND status='active'
          ) > 1 AS team_ready
        """,
        (tenant_id,) * 11,
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("onboarding readiness query returned no row")
    return {key: bool(row[key]) for key in row.keys()}


def _build_steps(tenant_id: str, facts: dict[str, bool]) -> list[OnboardingStep]:
    definitions = (
        (
            "tenant-profile",
            "准备企业资料",
            "确认当前企业处于启用状态并完成基础资料。",
            "#tenant",
            "查看企业设置",
            "tenant_ready",
            None,
        ),
        (
            "environment",
            "准备开发环境",
            "为当前企业准备一个可用的开发环境，后续配置都会归属到该环境。",
            "#environments",
            "配置环境",
            "environment_ready",
            "先完成企业资料。",
        ),
        (
            "provider-keypool",
            "接入模型服务",
            "配置一个启用的模型服务和可用的 KeyPool，或接入免密的本地 OpenAI-compatible Endpoint。",
            "#providers",
            "配置模型服务",
            "provider_keypool_ready",
            "先准备开发环境。",
        ),
        (
            "model-alias",
            "准备模型别名",
            "创建一个指向启用模型的开发环境模型别名，供业务请求稳定使用。",
            "#model-aliases",
            "配置模型别名",
            "model_alias_ready",
            "先完成模型服务接入。",
        ),
        (
            "project-application",
            "建立业务归属",
            "创建 Project 和 Application，让调用、用量和成本可以按业务归属追踪。",
            "#projects",
            "创建项目和应用",
            "project_application_ready",
            "先完成模型别名配置。",
        ),
        (
            "virtual-key",
            "创建调用凭据",
            "创建当前企业的 Virtual Key，供业务系统通过 Gateway 发起受控调用。",
            "#virtual-keys",
            "创建 Virtual Key",
            "virtual_key_ready",
            "先建立业务归属。",
        ),
        (
            "first-governed-call",
            "完成首次受控调用",
            "发起一次真实 Gateway 调用；成功请求会留下可追踪的 request_id。",
            "#quickstart",
            "发起首次调用",
            "successful_call",
            "先创建 Virtual Key。",
        ),
        (
            "usage-evidence",
            "核对用量证据",
            "确认用量账本已记录，之后可以持续追踪 Token、成本和失败请求。",
            "#usage",
            "查看用量",
            "usage_ready",
            "先完成一次受控调用。",
        ),
        (
            "policy",
            "配置安全策略",
            "按企业安全要求配置至少一条启用策略。",
            "#policies",
            "配置策略",
            "policy_ready",
            None,
        ),
        (
            "budget",
            "设置预算约束",
            "按企业的环境或业务归属设置预算约束。",
            "#budgets",
            "设置预算",
            "budget_ready",
            None,
        ),
        (
            "team",
            "邀请协作成员",
            "为企业管理员之外的协作成员创建账号并分配合适角色。",
            "#users",
            "管理成员",
            "team_ready",
            None,
        ),
    )
    steps: list[OnboardingStep] = []
    mandatory_ready = True
    for order, definition in enumerate(
        definitions, start=1
    ):
        (
            step_id,
            title,
            description,
            target_hash,
            action_label,
            fact_key,
            blocked_reason,
        ) = definition
        ready = facts[fact_key]
        is_mandatory = step_id in _MANDATORY_STEP_IDS
        if ready:
            status: OnboardingStepStatus = "complete"
            reason = None
        elif not is_mandatory:
            status = "optional"
            reason = None
        elif mandatory_ready:
            status = "current"
            reason = None
        else:
            status = "blocked"
            reason = blocked_reason
        if is_mandatory and not ready:
            mandatory_ready = False
        steps.append(
            OnboardingStep(
                id=step_id,
                order=order,
                status=status,
                title=title,
                description=description,
                target_hash=target_hash,
                action_label=action_label,
                blocking_reason=reason,
            )
        )
    return steps
