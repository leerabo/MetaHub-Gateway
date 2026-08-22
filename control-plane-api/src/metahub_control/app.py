"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息
脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/app.py
"""

from __future__ import annotations

from contextlib import nullcontext

from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.fastapi_app import create_service_app
from starlette.requests import Request

from .adapter_contracts import install_adapter_contract_event_scheduler
from .adapter_contracts import router as adapter_contracts_router
from .adapter_specs.routes import router as adapter_specs_router
from .admin_assistant import install_admin_assistant_scheduler
from .admin_assistant import router as admin_assistant_router
from .approvals import install_approval_scheduler
from .approvals import router as approvals_router
from .audit import install_audit_export_scheduler
from .audit.routes import router as audit_router
from .auth.routes import router as auth_router
from .batch_jobs import router as batch_jobs_router
from .billing_disputes import install_billing_dispute_scheduler
from .billing_disputes import router as billing_disputes_router
from .branding import router as branding_router
from .branding.scheduler import install_branding_scheduler
from .budgets.routes import router as budgets_router
from .config.routes import router as config_router
from .config_transfer.routes import router as config_transfer_router
from .console.routes import router as console_router
from .cost_anomalies import install_cost_anomaly_scheduler
from .cost_anomalies import router as cost_anomalies_router
from .customer_health import install_customer_health_scheduler
from .customer_health import router as customer_health_router
from .dashboard.routes import router as dashboard_router
from .debug_console.routes import router as debug_console_router
from .developer_portal.routes import router as developer_portal_router
from .dlp.routes import router as dlp_router
from .enterprise_chat.routes import router as enterprise_chat_router
from .environments.routes import router as environments_router
from .error_maps import install_provider_error_map_scheduler
from .error_maps import router as error_maps_router
from .experiments import install_experiment_scheduler
from .experiments import router as experiments_router
from .feedback.routes import router as feedback_router
from .file_security import install_file_security_event_scheduler
from .file_security import router as file_security_router
from .files.routes import router as files_router
from .finops.routes import router as finops_router
from .identity.routes import router as identity_router
from .intent_security import router as intent_security_router
from .invitations.routes import router as invitations_router
from .local_endpoints import router as local_endpoints_router
from .mcp_governance import install_mcp_scheduler
from .mcp_governance import router as mcp_governance_router
from .model_aliases.routes import router as model_aliases_router
from .model_prices.routes import router as model_prices_router
from .model_quality_scores.routes import router as model_quality_scores_router
from .models.routes import router as models_router
from .partner_portal import install_partner_portal_scheduler
from .partner_portal import router as partner_portal_router
from .permissions.routes import router as permissions_router
from .plan_billing import install_plan_billing_scheduler
from .plan_billing import router as plan_billing_router
from .platform_access.routes import router as platform_access_router
from .policies.routes import router as policies_router
from .policy_snapshots.routes import router as policy_snapshots_router
from .policy_templates.routes import router as policy_templates_router
from .product_analytics import install_product_event_scheduler
from .product_analytics import router as product_analytics_router
from .projects.routes import router as projects_router
from .prompt_templates.routes import router as prompt_templates_router
from .provider_health import install_provider_health_scheduler
from .provider_health import router as provider_health_router
from .provider_status import router as provider_status_router
from .providers.routes import provider_keys_router
from .providers.routes import router as providers_router
from .request_explanations import install_request_explanation_scheduler
from .request_explanations import router as request_explanations_router
from .request_logs import install_request_log_query_event_scheduler
from .request_logs.routes import router as request_logs_router
from .response_cache import install_response_cache_outbox_scheduler
from .response_cache import router as response_cache_router
from .saas_billing import install_saas_billing_scheduler
from .saas_billing import router as saas_billing_router
from .security_events import install_security_event_scheduler
from .session_risks import router as session_risks_router
from .tenant_data import install_tenant_data_scheduler
from .tenant_data.routes import router as tenant_data_router
from .tenants.routes import router as tenant_router
from .usage.routes import router as usage_router
from .usage.scheduler import install_usage_export_scheduler
from .user_preferences.routes import router as user_preferences_router
from .users.routes import router as users_router
from .virtual_keys.routes import router as virtual_keys_router
from .webhooks import install_webhook_replay_scheduler
from .webhooks.routes import router as webhooks_router
from .workspace_access.routes import router as workspace_access_router
from .workspace_onboarding.routes import router as workspace_onboarding_router
from .workspaces.routes import router as workspaces_router

# 控制面承载低频管理 API，不直接处理模型调用热路径。PRD-001 只暴露健康和版本接口，
# 租户、用户、Provider、模型目录等管理能力由后续 PRD 注册。
app = create_service_app(
    service_name="control-plane-api",
    title="MetaHub Control Plane API",
    # Policy 编辑器需要发送强 If-Match 并读取 ETag；该 CORS 能力只属于控制面。
    extra_cors_allow_headers=(
        "If-Match",
        "If-Policy-Candidate-Match",
        "Idempotency-Key",
        "X-CSRF-Token",
    ),
    cors_allow_credentials=True,
    extra_cors_expose_headers=(
        "ETag",
        "X-Policy-Candidate-ETag",
        "X-MetaHub-Audit-Export-ID",
        "X-MetaHub-Audit-Export-Row-Count",
        # 文件安全 CSV 已返回 nosniff；浏览器必须能读取该头并执行下载合同验收。
        "X-Content-Type-Options",
        # PAGE-107 在跨端口 Console 中执行 exact 私有响应门禁，需要读取 CORS
        # 可能追加 Origin 的 Vary。
        "Vary",
    ),
)


@app.middleware("http")
async def response_cache_private_cache_boundary(request: Request, call_next):
    """让 Response Cache 的认证、校验与业务错误和成功响应共享 no-store/private。"""

    # Control GETs commonly authenticate and then read through a separate service.
    # Reuse one handle for that request only; writes keep their existing ownership.
    scope = (
        database_request_scope()
        if request.method in {"GET", "HEAD"}
        else nullcontext()
    )
    with scope:
        response = await call_next(request)
    if (
        request.url.path
        in {
            "/cache-policies",
            "/cache/invalidate",
            "/admin-assistant/query",
            "/plans",
            "/quota-buckets",
            "/quota-adjustments",
            "/customer-health",
            "/product-analytics/funnel",
        }
        or request.url.path == "/billing-disputes"
        or request.url.path.startswith("/billing-disputes/")
        or request.url.path == "/local-endpoints"
        or request.url.path.startswith("/local-endpoints/")
        or request.url.path == "/partners"
        or request.url.path.startswith("/partners/")
        or request.url.path == "/branding"
        or request.url.path.startswith("/branding/")
        or request.url.path in {
            "/identity/workspaces",
            "/identity/workspace-switches",
            "/workspace/session",
            "/memberships",
            "/membership-invitations",
            "/workspace-owner-transfers",
        }
        or request.url.path.startswith("/memberships/")
        or request.url.path == "/platform/bootstrap"
        or request.url.path.startswith("/platform/")
        or request.url.path.startswith("/support/")
        or request.url.path.startswith("/workspace/support-grants/")
        or request.url.path.startswith("/workspace/break-glass-sessions/")
        or request.url.path == "/subscriptions"
        or request.url.path.startswith("/subscriptions/")
        or request.url.path == "/workspaces/current/onboarding"
        or request.url.path.startswith("/workspaces/current/onboarding/")
        or request.url.path == "/tenant/current"
        or request.url.path == "/environments"
        or request.url.path.startswith("/environments/")
        or request.url.path == "/providers"
        or request.url.path.startswith("/providers/")
        or request.url.path.startswith("/provider-keys/")
        or request.url.path == "/provider-health"
        or request.url.path == "/models"
        or request.url.path.startswith("/models/")
        or request.url.path == "/model-aliases"
        or request.url.path.startswith("/model-aliases/")
        or request.url.path == "/projects"
        or request.url.path.startswith("/projects/")
        or request.url.path == "/applications"
        or request.url.path.startswith("/applications/")
        or request.url.path == "/virtual-keys"
        or request.url.path.startswith("/virtual-keys/")
    ):
        # 依赖或 parser 可能在路由函数设置 Header 前失败，因此必须在应用最外层再次覆盖。
        # Preserve endpoint-specific directives while covering error responses
        # that do not set a cache boundary themselves.
        response.headers.setdefault("Cache-Control", "no-store, private")
    if request.url.path == "/admin-assistant/query":
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Authorization"
    return response


app.include_router(auth_router)
app.include_router(identity_router)
app.include_router(workspaces_router)
app.include_router(workspace_access_router)
app.include_router(workspace_onboarding_router)
app.include_router(platform_access_router)
app.include_router(admin_assistant_router)
app.include_router(approvals_router)
app.include_router(batch_jobs_router)
app.include_router(billing_disputes_router)
app.include_router(adapter_contracts_router)
app.include_router(adapter_specs_router)
app.include_router(audit_router)
app.include_router(console_router)
app.include_router(cost_anomalies_router)
app.include_router(customer_health_router)
app.include_router(dashboard_router)
app.include_router(config_router)
app.include_router(config_transfer_router)
app.include_router(debug_console_router)
app.include_router(developer_portal_router)
app.include_router(dlp_router)
app.include_router(environments_router)
app.include_router(enterprise_chat_router)
app.include_router(error_maps_router)
app.include_router(experiments_router)
app.include_router(feedback_router)
app.include_router(files_router)
app.include_router(file_security_router)
app.include_router(finops_router)
app.include_router(tenant_router)
app.include_router(tenant_data_router)
app.include_router(user_preferences_router)
app.include_router(users_router)
app.include_router(invitations_router)
app.include_router(intent_security_router)
app.include_router(local_endpoints_router)
app.include_router(virtual_keys_router)
app.include_router(providers_router)
app.include_router(provider_health_router)
app.include_router(provider_status_router)
app.include_router(projects_router)
app.include_router(product_analytics_router)
app.include_router(prompt_templates_router)
app.include_router(provider_keys_router)
app.include_router(models_router)
app.include_router(model_prices_router)
app.include_router(model_aliases_router)
app.include_router(model_quality_scores_router)
app.include_router(mcp_governance_router)
app.include_router(permissions_router)
app.include_router(plan_billing_router)
app.include_router(partner_portal_router)
app.include_router(branding_router)
app.include_router(policies_router)
app.include_router(policy_templates_router)
app.include_router(policy_snapshots_router)
app.include_router(request_logs_router)
app.include_router(response_cache_router)
app.include_router(saas_billing_router)
app.include_router(session_risks_router)
app.include_router(request_explanations_router)
app.include_router(usage_router)
app.include_router(budgets_router)
app.include_router(webhooks_router)
# PRD-077 runtime 必须先于其他 scheduler 预建 WAL/FULL 连接；后安装的 lifespan 会在
# shutdown 时先停止各自 worker，再由本 runtime 最后关闭长期连接。单进程本地开发默认
# 可关闭这组可选恢复 worker，避免大量空队列轮询争用 SQLite；生产默认保持开启。
if app.state.config.control_background_schedulers_enabled:
    install_admin_assistant_scheduler(app)
    install_provider_health_scheduler(app)
    install_usage_export_scheduler(app)
    install_cost_anomaly_scheduler(app)
    install_customer_health_scheduler(app)
    install_request_explanation_scheduler(app)
    install_request_log_query_event_scheduler(app)
    install_provider_error_map_scheduler(app)
    install_audit_export_scheduler(app)
    install_webhook_replay_scheduler(app)
    install_file_security_event_scheduler(app)
    install_adapter_contract_event_scheduler(app)
    install_experiment_scheduler(app)
    install_response_cache_outbox_scheduler(app)
    install_product_event_scheduler(app)
    install_approval_scheduler(app)
    install_plan_billing_scheduler(app)
    install_saas_billing_scheduler(app)
    install_partner_portal_scheduler(app)
    install_branding_scheduler(app)
    install_billing_dispute_scheduler(app)
    install_security_event_scheduler(app)
    install_mcp_scheduler(app)
    install_tenant_data_scheduler(app)
