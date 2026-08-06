"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息
脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/app.py
"""

from __future__ import annotations

from metahub_shared.runtime.fastapi_app import create_service_app

from .adapter_contracts import install_adapter_contract_event_scheduler
from .adapter_contracts import router as adapter_contracts_router
from .adapter_specs.routes import router as adapter_specs_router
from .audit import install_audit_export_scheduler
from .audit.routes import router as audit_router
from .auth.routes import router as auth_router
from .budgets.routes import router as budgets_router
from .config.routes import router as config_router
from .config_transfer.routes import router as config_transfer_router
from .console.routes import router as console_router
from .cost_anomalies import install_cost_anomaly_scheduler
from .cost_anomalies import router as cost_anomalies_router
from .dashboard.routes import router as dashboard_router
from .debug_console.routes import router as debug_console_router
from .developer_portal.routes import router as developer_portal_router
from .dlp.routes import router as dlp_router
from .enterprise_chat.routes import router as enterprise_chat_router
from .environments.routes import router as environments_router
from .error_maps import install_provider_error_map_scheduler
from .error_maps import router as error_maps_router
from .feedback.routes import router as feedback_router
from .file_security import install_file_security_event_scheduler
from .file_security import router as file_security_router
from .files.routes import router as files_router
from .finops.routes import router as finops_router
from .intent_security import router as intent_security_router
from .invitations.routes import router as invitations_router
from .model_aliases.routes import router as model_aliases_router
from .model_prices.routes import router as model_prices_router
from .model_quality_scores.routes import router as model_quality_scores_router
from .models.routes import router as models_router
from .permissions.routes import router as permissions_router
from .policies.routes import router as policies_router
from .policy_snapshots.routes import router as policy_snapshots_router
from .policy_templates.routes import router as policy_templates_router
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
from .session_risks import router as session_risks_router
from .tenants.routes import router as tenant_router
from .usage.routes import router as usage_router
from .usage.scheduler import install_usage_export_scheduler
from .user_preferences.routes import router as user_preferences_router
from .users.routes import router as users_router
from .virtual_keys.routes import router as virtual_keys_router
from .webhooks import install_webhook_replay_scheduler
from .webhooks.routes import router as webhooks_router

# 控制面承载低频管理 API，不直接处理模型调用热路径。PRD-001 只暴露健康和版本接口，
# 租户、用户、Provider、模型目录等管理能力由后续 PRD 注册。
app = create_service_app(
    service_name="control-plane-api",
    title="MetaHub Control Plane API",
    # Policy 编辑器需要发送强 If-Match 并读取 ETag；该 CORS 能力只属于控制面。
    extra_cors_allow_headers=("If-Match", "If-Policy-Candidate-Match", "Idempotency-Key"),
    extra_cors_expose_headers=(
        "ETag",
        "X-Policy-Candidate-ETag",
        "X-MetaHub-Audit-Export-ID",
        "X-MetaHub-Audit-Export-Row-Count",
        # 文件安全 CSV 已返回 nosniff；浏览器必须能读取该头并执行下载合同验收。
        "X-Content-Type-Options",
    ),
)
app.include_router(auth_router)
app.include_router(adapter_contracts_router)
app.include_router(adapter_specs_router)
app.include_router(audit_router)
app.include_router(console_router)
app.include_router(cost_anomalies_router)
app.include_router(dashboard_router)
app.include_router(config_router)
app.include_router(config_transfer_router)
app.include_router(debug_console_router)
app.include_router(developer_portal_router)
app.include_router(dlp_router)
app.include_router(environments_router)
app.include_router(enterprise_chat_router)
app.include_router(error_maps_router)
app.include_router(feedback_router)
app.include_router(files_router)
app.include_router(file_security_router)
app.include_router(finops_router)
app.include_router(tenant_router)
app.include_router(user_preferences_router)
app.include_router(users_router)
app.include_router(invitations_router)
app.include_router(intent_security_router)
app.include_router(virtual_keys_router)
app.include_router(providers_router)
app.include_router(provider_health_router)
app.include_router(provider_status_router)
app.include_router(projects_router)
app.include_router(prompt_templates_router)
app.include_router(provider_keys_router)
app.include_router(models_router)
app.include_router(model_prices_router)
app.include_router(model_aliases_router)
app.include_router(model_quality_scores_router)
app.include_router(permissions_router)
app.include_router(policies_router)
app.include_router(policy_templates_router)
app.include_router(policy_snapshots_router)
app.include_router(request_logs_router)
app.include_router(session_risks_router)
app.include_router(request_explanations_router)
app.include_router(usage_router)
app.include_router(budgets_router)
app.include_router(webhooks_router)
install_provider_health_scheduler(app)
install_usage_export_scheduler(app)
install_cost_anomaly_scheduler(app)
install_request_explanation_scheduler(app)
install_request_log_query_event_scheduler(app)
install_provider_error_map_scheduler(app)
install_audit_export_scheduler(app)
install_webhook_replay_scheduler(app)
install_file_security_event_scheduler(app)
install_adapter_contract_event_scheduler(app)
