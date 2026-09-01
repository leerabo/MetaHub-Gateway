"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息
脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/app.py
"""

from __future__ import annotations

from contextlib import nullcontext

from metahub_shared.abuse_risk import install_risk_runtime, persistence_sink
from metahub_shared.db.connection import database_request_scope
from metahub_shared.observability import install_telemetry_runtime
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
from .business_verification import router as business_verification_router
from .capacity_management import router as capacity_management_router
from .channel_management import install_channel_scheduler
from .channel_management import router as channel_management_router
from .compliance_center import install_compliance_scheduler
from .compliance_center import router as compliance_center_router
from .config.routes import router as config_router
from .config_distribution import router as config_distribution_router
from .config_transfer.routes import router as config_transfer_router
from .console.routes import router as console_router
from .cost_anomalies import install_cost_anomaly_scheduler
from .cost_anomalies import router as cost_anomalies_router
from .customer_support.routes import router as customer_support_router
from .customer_support.scheduler import install_customer_support_scheduler
from .dashboard.routes import router as dashboard_router
from .data_governance import install_data_governance_scheduler
from .data_governance import router as data_governance_router
from .debug_console.routes import router as debug_console_router
from .developer_contracts import install_developer_contract_scheduler
from .developer_contracts import router as developer_contracts_router
from .developer_portal.routes import router as developer_portal_router
from .disaster_recovery import router as disaster_recovery_router
from .dlp.routes import router as dlp_router
from .edge_management import install_edge_support_bundle_scheduler
from .edge_management import router as edge_management_router
from .enterprise_chat.routes import router as enterprise_chat_router
from .enterprise_identity import router as enterprise_identity_router
from .environments.routes import router as environments_router
from .error_maps import install_provider_error_map_scheduler
from .error_maps import router as error_maps_router
from .experiments import install_experiment_scheduler
from .experiments import router as experiments_router
from .feature_rollouts import router as feature_rollouts_router
from .feedback.routes import router as feedback_router
from .file_security import install_file_security_event_scheduler
from .file_security import router as file_security_router
from .files.routes import router as files_router
from .finops.routes import router as finops_router
from .identity.routes import router as identity_router
from .intent_security import router as intent_security_router
from .invitations.routes import router as invitations_router
from .invoice_reconciliation import install_invoice_reconciliation_scheduler
from .invoice_reconciliation import router as invoice_reconciliation_router
from .launch_governance import install_launch_governance_scheduler
from .launch_governance import router as launch_governance_router
from .local_endpoints import router as local_endpoints_router
from .machine_identity import router as machine_identity_router
from .mcp_governance import install_mcp_scheduler
from .mcp_governance import router as mcp_governance_router
from .migration_operations.routes import router as migration_operations_router
from .model_aliases.routes import router as model_aliases_router
from .model_prices.routes import router as model_prices_router
from .model_quality_scores.routes import router as model_quality_scores_router
from .models.routes import router as models_router
from .notifications import install_notification_scheduler
from .notifications import router as notifications_router
from .observability import install_observability_scheduler
from .observability import router as observability_router
from .payment_commerce import install_payment_commerce_scheduler
from .payment_commerce import router as payment_commerce_router
from .permissions.routes import router as permissions_router
from .plan_billing import install_plan_billing_scheduler
from .plan_billing import router as plan_billing_router
from .platform_access.routes import router as platform_access_router
from .platform_billing import install_platform_billing_scheduler
from .platform_billing import router as platform_billing_router
from .platform_customer_success import install_platform_customer_success_scheduler
from .platform_customer_success import router as platform_customer_success_router
from .platform_gateway import install_gateway_snapshot_scheduler
from .platform_gateway import router as platform_gateway_router
from .platform_growth import install_platform_growth_scheduler
from .platform_growth import router as platform_growth_router
from .platform_infrastructure import router as platform_infrastructure_router
from .platform_jobs import install_platform_job_supervisor
from .platform_jobs import router as platform_jobs_router
from .policies.routes import router as policies_router
from .policy_snapshots.routes import router as policy_snapshots_router
from .policy_templates.routes import router as policy_templates_router
from .privacy_center import install_privacy_scheduler
from .privacy_center import router as privacy_center_router
from .projects.routes import router as projects_router
from .prompt_templates.routes import router as prompt_templates_router
from .provider_catalog import router as provider_catalog_router
from .provider_health import install_provider_health_scheduler
from .provider_health import router as provider_health_router
from .provider_status import router as provider_status_router
from .providers.routes import provider_keys_router
from .providers.routes import router as providers_router
from .public_status import install_status_publication_scheduler
from .public_status import router as public_status_router
from .request_explanations import install_request_explanation_scheduler
from .request_explanations import router as request_explanations_router
from .request_logs import install_request_log_query_event_scheduler
from .request_logs.routes import router as request_logs_router
from .response_cache import install_response_cache_outbox_scheduler
from .response_cache import router as response_cache_router
from .risk_controls import install_risk_scheduler
from .risk_controls import router as risk_controls_router
from .saas_billing import install_saas_billing_scheduler
from .saas_billing import router as saas_billing_router
from .security_assurance import install_security_assurance_scheduler
from .security_assurance import router as security_assurance_router
from .security_events import install_security_event_scheduler
from .session_risks import router as session_risks_router
from .supply_chain_security import install_supply_chain_scheduler
from .supply_chain_security import router as supply_chain_security_router
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
    extra_cors_allow_methods=("PUT",),
    # Policy 编辑器需要发送强 If-Match 并读取 ETag；该 CORS 能力只属于控制面。
    extra_cors_allow_headers=(
        "If-Match",
        "If-Policy-Candidate-Match",
        "Idempotency-Key",
        "X-CSRF-Token",
        "X-MetaHub-Invoice-Download-Token",
        "X-Step-Up-Grant",
        "X-Privacy-Export-Receipt",
        "X-Growth-Export-Receipt",
    ),
    cors_allow_credentials=True,
    extra_cors_expose_headers=(
        "ETag",
        "X-Policy-Candidate-ETag",
        "X-MetaHub-Audit-Export-ID",
        "X-MetaHub-Audit-Export-Row-Count",
        "Digest",
        "X-Artifact-Receipt-SHA256",
        "Content-Disposition",
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

    # Control reads commonly authenticate and then query through a separate service.
    # Keep one request-owned handle and return its reset physical connection to the
    # bounded idle pool; writes retain their existing connection ownership.
    scope = (
        database_request_scope(reuse_connections=True)
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
            "/product-analytics/funnel",
        }
        or request.url.path == "/billing-disputes"
        or request.url.path.startswith("/billing-disputes/")
        or request.url.path == "/notifications"
        or request.url.path.startswith("/notifications/")
        or request.url.path == "/notification-preferences"
        or request.url.path == "/local-endpoints"
        or request.url.path.startswith("/local-endpoints/")
        or request.url.path == "/branding"
        or request.url.path.startswith("/branding/")
        or request.url.path
        in {
            "/identity/workspaces",
            "/identity/workspace-switches",
            "/workspace/session",
            "/memberships",
            "/membership-invitations",
            "/workspace-owner-transfers",
            "/identity/login-discovery",
            "/identity/sso/callback",
        }
        or request.url.path.startswith("/memberships/")
        or request.url.path == "/scim/v2/Users"
        or request.url.path.startswith("/scim/v2/Users/")
        or request.url.path == "/scim/v2/Groups"
        or request.url.path.startswith("/scim/v2/Groups/")
        or request.url.path == "/platform/bootstrap"
        or request.url.path.startswith("/platform/")
        or request.url.path.startswith("/identity/privacy-")
        or request.url.path.startswith("/identity/data-subject-requests")
        or request.url.path.startswith("/identity/exports/")
        or request.url.path.startswith("/risk/")
        or request.url.path.startswith("/support/")
        or request.url.path.startswith("/workspace/support-grants/")
        or request.url.path.startswith("/workspace/break-glass-sessions/")
        or request.url.path == "/subscriptions"
        or request.url.path.startswith("/subscriptions/")
        or request.url.path == "/billing/summary"
        or request.url.path == "/billing/usage"
        or request.url.path == "/billing/reconciliation-export"
        or request.url.path.startswith("/workspaces/")
        and (
            request.url.path.endswith("/business-verification")
            or request.url.path.endswith("/business-verifications")
            or request.url.path.endswith("/billing-profile")
            or "/domains" in request.url.path
            or "/sso" in request.url.path
            or "/scim-" in request.url.path
            or request.url.path.endswith("/enterprise-identity")
            or request.url.path.endswith("/compliance-reports")
        )
        or request.url.path == "/workspaces/current/onboarding"
        or request.url.path.startswith("/workspaces/current/onboarding/")
        or request.url.path == "/tenant/current"
        or request.url.path == "/environments"
        or request.url.path.startswith("/environments/")
        or request.url.path == "/providers"
        or request.url.path.startswith("/providers/")
        or request.url.path == "/provider-catalog"
        or request.url.path.startswith("/provider-catalog/")
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
        or request.url.path == "/service-accounts"
        or request.url.path.startswith("/service-accounts/")
        or request.url.path == "/oauth/clients"
        or request.url.path == "/oauth/token"
        or request.url.path == "/request-logs"
    ):
        # 依赖或 parser 可能在路由函数设置 Header 前失败，因此必须在应用最外层再次覆盖。
        # Preserve endpoint-specific directives while covering error responses
        # that do not set a cache boundary themselves.
        response.headers.setdefault("Cache-Control", "no-store, private")
    if request.url.path in {"/service-accounts", "/oauth/clients"} or request.url.path.startswith(
        "/service-accounts/"
    ):
        response.headers.setdefault("Pragma", "no-cache")
    if request.url.path == "/oauth/token":
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Authorization"
    if request.url.path == "/admin-assistant/query":
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Authorization"
    return response


app.include_router(auth_router)
app.include_router(identity_router)
app.include_router(privacy_center_router)
app.include_router(compliance_center_router)
app.include_router(supply_chain_security_router)
app.include_router(security_assurance_router)
app.include_router(launch_governance_router)
app.include_router(enterprise_identity_router)
app.include_router(edge_management_router)
app.include_router(workspaces_router)
app.include_router(workspace_access_router)
app.include_router(workspace_onboarding_router)
app.include_router(migration_operations_router)
app.include_router(customer_support_router)
app.include_router(business_verification_router)
app.include_router(invoice_reconciliation_router)
app.include_router(notifications_router)
app.include_router(observability_router)
app.include_router(risk_controls_router)
app.include_router(disaster_recovery_router)
app.include_router(public_status_router)
app.include_router(capacity_management_router)
app.include_router(channel_management_router)
app.include_router(platform_customer_success_router)
app.include_router(platform_growth_router)
app.include_router(platform_access_router)
app.include_router(platform_billing_router)
app.include_router(platform_infrastructure_router)
app.include_router(platform_gateway_router)
app.include_router(platform_jobs_router)
app.include_router(payment_commerce_router)
app.include_router(admin_assistant_router)
app.include_router(approvals_router)
app.include_router(batch_jobs_router)
app.include_router(billing_disputes_router)
app.include_router(adapter_contracts_router)
app.include_router(adapter_specs_router)
app.include_router(audit_router)
app.include_router(console_router)
app.include_router(cost_anomalies_router)
app.include_router(dashboard_router)
app.include_router(data_governance_router)
app.include_router(config_router)
app.include_router(config_distribution_router)
app.include_router(config_transfer_router)
app.include_router(debug_console_router)
app.include_router(developer_contracts_router)
app.include_router(developer_portal_router)
app.include_router(dlp_router)
app.include_router(environments_router)
app.include_router(enterprise_chat_router)
app.include_router(error_maps_router)
app.include_router(experiments_router)
app.include_router(feature_rollouts_router)
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
app.include_router(machine_identity_router)
app.include_router(virtual_keys_router)
app.include_router(providers_router)
app.include_router(provider_catalog_router)
app.include_router(provider_health_router)
app.include_router(provider_status_router)
app.include_router(projects_router)
app.include_router(prompt_templates_router)
app.include_router(provider_keys_router)
app.include_router(models_router)
app.include_router(model_prices_router)
app.include_router(model_aliases_router)
app.include_router(model_quality_scores_router)
app.include_router(mcp_governance_router)
app.include_router(permissions_router)
app.include_router(plan_billing_router)
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
# A single supervisor owns startup and reverse-order shutdown for every Control worker.
_scheduler_installers = [install_status_publication_scheduler]
if app.state.config.gateway_placement_enabled:
    _scheduler_installers.append(install_gateway_snapshot_scheduler)
if app.state.config.control_background_schedulers_enabled:
    _scheduler_installers.extend(
        (
            install_admin_assistant_scheduler,
            install_provider_health_scheduler,
            install_usage_export_scheduler,
            install_cost_anomaly_scheduler,
            install_platform_customer_success_scheduler,
            install_customer_support_scheduler,
            install_developer_contract_scheduler,
            install_privacy_scheduler,
            install_compliance_scheduler,
            install_supply_chain_scheduler,
            install_security_assurance_scheduler,
            install_launch_governance_scheduler,
            install_request_explanation_scheduler,
            install_request_log_query_event_scheduler,
            install_provider_error_map_scheduler,
            install_audit_export_scheduler,
            install_webhook_replay_scheduler,
            install_file_security_event_scheduler,
            install_adapter_contract_event_scheduler,
            install_experiment_scheduler,
            install_response_cache_outbox_scheduler,
            install_platform_growth_scheduler,
            install_approval_scheduler,
            install_plan_billing_scheduler,
            install_saas_billing_scheduler,
            install_platform_billing_scheduler,
            install_payment_commerce_scheduler,
            install_invoice_reconciliation_scheduler,
            install_notification_scheduler,
            install_observability_scheduler,
            install_risk_scheduler,
            install_channel_scheduler,
            install_branding_scheduler,
            install_billing_dispute_scheduler,
            install_security_event_scheduler,
            install_mcp_scheduler,
            install_tenant_data_scheduler,
            install_data_governance_scheduler,
            install_edge_support_bundle_scheduler,
        )
    )
install_platform_job_supervisor(app, scheduler_installers=_scheduler_installers)
# Risk facts persist from a bounded background queue; request paths never wait on SQLite writes.
install_risk_runtime(app, sink=persistence_sink(app.state.config))
# Observability is installed last so its lifespan stops accepting facts before workers shut down.
install_telemetry_runtime(app, service_code="control")
