"""MetaHub 跨服务统一错误注册表与公开响应模型。

业务模块只能通过稳定错误码暴露可诊断信息；底层异常、文件路径、Provider Key、Token、
Prompt 和上游原始响应不得进入客户端错误体。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .error_diagnosis import ErrorDiagnosisInfo, diagnose_error
from .i18n import DEFAULT_LOCALE, error_label


class ErrorResponse(BaseModel):
    """统一错误响应。

    所有 API 错误都必须包含 request_id，方便调用方把响应、服务端日志和后续
    Debug Center 记录串起来。PRD-004 起 trace_id 也进入错误响应，便于跨请求链路排查。
    """

    code: str
    message: str
    request_id: str
    trace_id: str
    suggestion: str
    diagnosis: ErrorDiagnosisInfo


@dataclass(frozen=True)
class ErrorSpec:
    """错误码元数据。

    suggestion 是面向使用者的修复建议，不能写内部堆栈、密钥、数据库地址等敏感信息。
    """

    http_status: int
    message: str
    suggestion: str
    retryable: bool = False
    owner: str = "infra"


# 统一错误码注册表是所有 API 响应的契约来源，message/suggestion 不能包含内部堆栈或密钥信息。
ERROR_SPECS: dict[str, ErrorSpec] = {
    "infra.config_missing": ErrorSpec(
        500,
        "Required configuration is missing.",
        "Check .env.example and local environment variables.",
        owner="config",
    ),
    "infra.config_invalid": ErrorSpec(
        500,
        "Configuration value is invalid.",
        "Fix the invalid environment variable and restart the service.",
        owner="config",
    ),
    "infra.config_secret_unmasked": ErrorSpec(
        500,
        "Sensitive configuration was not masked.",
        "Fix config summary masking before exposing configuration details.",
        owner="config",
    ),
    "infra.config_profile_not_found": ErrorSpec(
        500,
        "Configuration profile file was not found.",
        "Check METAHUB_CONFIG_FILE or remove it to use default configuration.",
        owner="config",
    ),
    "infra.service_unavailable": ErrorSpec(
        503,
        "Service is not ready.",
        "Check local service logs and retry after startup completes.",
        retryable=True,
    ),
    "infra.version_unavailable": ErrorSpec(
        500,
        "Version information is unavailable.",
        "Check build metadata environment variables.",
    ),
    "infra.not_found": ErrorSpec(
        404,
        "Requested resource was not found.",
        "Check the API path and method.",
    ),
    "infra.method_not_allowed": ErrorSpec(
        405,
        "Request method is not allowed.",
        "Use a method listed in the Allow response header.",
    ),
    "infra.validation_failed": ErrorSpec(
        422,
        "Request payload is invalid.",
        "Check required fields and value formats.",
    ),
    "infra.unhandled": ErrorSpec(
        500,
        "Internal server error.",
        "Use request_id to locate structured logs.",
        retryable=True,
    ),
    "auth.internal_token_required": ErrorSpec(
        401,
        "Internal token is required.",
        "Pass X-Internal-Token when accessing internal endpoints in prod.",
        owner="auth",
    ),
    "auth.internal_token_invalid": ErrorSpec(
        403,
        "Internal token is invalid.",
        "Check the caller's internal endpoint token.",
        owner="auth",
    ),
    "auth.login_invalid": ErrorSpec(
        401,
        "Login credentials are invalid.",
        "Check tenant, email, and password.",
        owner="auth",
    ),
    "auth.login_locked": ErrorSpec(
        429,
        "Login is temporarily locked.",
        "Wait for the lock window to expire or contact an administrator.",
        retryable=True,
        owner="auth",
    ),
    "auth.token_missing": ErrorSpec(
        401,
        "Bearer token is required.",
        "Sign in again and pass Authorization: Bearer <token>.",
        owner="auth",
    ),
    "auth.token_invalid": ErrorSpec(
        401,
        "Bearer token is invalid.",
        "Sign in again.",
        owner="auth",
    ),
    "auth.token_expired": ErrorSpec(
        401,
        "Bearer token has expired.",
        "Sign in again.",
        owner="auth",
    ),
    "auth.session_revoked": ErrorSpec(
        401,
        "Session has been revoked.",
        "Sign in again.",
        owner="auth",
    ),
    "auth.permission_denied": ErrorSpec(
        403,
        "Permission denied.",
        "Contact an administrator to grant the required permission.",
        owner="auth",
    ),
    "rbac.role_not_found": ErrorSpec(
        404,
        "Role was not found.",
        "Refresh the permission matrix and choose a role in the current tenant.",
        owner="rbac",
    ),
    "rbac.payload_invalid": ErrorSpec(
        422,
        "Permission matrix payload is invalid.",
        "Provide expected_revision and a unique list of canonical permission codes.",
        owner="rbac",
    ),
    "rbac.permission_not_grantable": ErrorSpec(
        422,
        "Permission cannot be granted to this role.",
        "Remove permissions outside the product-defined role boundary.",
        owner="rbac",
    ),
    "rbac.role_immutable": ErrorSpec(
        409,
        "Role permission matrix is immutable.",
        "The tenant admin role always retains the complete permission catalog.",
        owner="rbac",
    ),
    "rbac.revision_conflict": ErrorSpec(
        409,
        "Permission matrix revision has changed.",
        "Reload the role matrix before saving the draft again.",
        owner="rbac",
    ),
    "rbac.matrix_integrity_failed": ErrorSpec(
        500,
        "Permission matrix integrity validation failed.",
        "Contact the platform administrator and inspect the request id.",
        owner="rbac",
    ),
    "rbac.unavailable": ErrorSpec(
        503,
        "Permission matrix is temporarily unavailable.",
        "Retry later with the same intended role revision.",
        retryable=True,
        owner="rbac",
    ),
    # Prompt 模板管理错误只描述资源状态和可恢复动作，不能包含正文、变量、密文或绑定清单。
    "prompt_template.not_found": ErrorSpec(
        404,
        "Prompt template was not found.",
        "Refresh the prompt template list for the current tenant.",
        owner="prompt_template",
    ),
    "prompt_template.request_invalid": ErrorSpec(
        422,
        "Prompt template request is invalid.",
        "Use the documented fields, limits, pagination, and request identifiers.",
        owner="prompt_template",
    ),
    "prompt_template.definition_invalid": ErrorSpec(
        422,
        "Prompt template definition is invalid.",
        "Correct the template syntax, variable schema, bindings, or resource state.",
        owner="prompt_template",
    ),
    "prompt_template.binding_not_found": ErrorSpec(
        404,
        "Prompt template binding was not found.",
        "Choose an active project and model alias from the template options.",
        owner="prompt_template",
    ),
    "prompt_template.variables_invalid": ErrorSpec(
        422,
        "Prompt template variables are invalid.",
        "Provide every declared variable as a bounded string without extra fields.",
        owner="prompt_template",
    ),
    "prompt_template.name_conflict": ErrorSpec(
        409,
        "Prompt template name already exists.",
        "Choose another name within the current tenant.",
        owner="prompt_template",
    ),
    "prompt_template.revision_conflict": ErrorSpec(
        409,
        "Prompt template revision has changed.",
        "Load the current template and explicitly reconcile the local draft.",
        owner="prompt_template",
    ),
    "prompt_template.idempotency_conflict": ErrorSpec(
        409,
        "Prompt template request id conflicts with a previous intent.",
        "Reuse the request id only for the exact same operation and payload.",
        owner="prompt_template",
    ),
    "prompt_template.disabled": ErrorSpec(
        409,
        "Prompt template is disabled.",
        "Choose an active template or ask an authorized user to enable it.",
        owner="prompt_template",
    ),
    "prompt_template.version_conflict": ErrorSpec(
        409,
        "Prompt template version is no longer current.",
        "Refresh template options and explicitly select the current version.",
        owner="prompt_template",
    ),
    "prompt_template.not_applicable": ErrorSpec(
        409,
        "Prompt template does not apply to the current project.",
        "Choose a template published for the Chat project.",
        owner="prompt_template",
    ),
    "prompt_template.definition_unavailable": ErrorSpec(
        500,
        "Prompt template definition is unavailable.",
        "Use the request id to inspect template definition integrity.",
        owner="prompt_template",
    ),
    "prompt_template.integrity_failed": ErrorSpec(
        500,
        "Prompt template integrity validation failed.",
        "Use the request id to inspect the immutable version and owner graph.",
        owner="prompt_template",
    ),
    "prompt_template.unavailable": ErrorSpec(
        503,
        "Prompt template service is temporarily unavailable.",
        "Retry the same idempotent management intent after service recovery.",
        retryable=True,
        owner="prompt_template",
    ),
    # 企业 Chat 公开错误只暴露稳定状态，不复制 Prompt、Response、grant、Key、URL 或底层异常。
    "chat.request_invalid": ErrorSpec(
        422,
        "Enterprise Chat request is invalid.",
        "Use the documented project, model, session, UUID, and alternating text history fields.",
        owner="enterprise_chat",
    ),
    "chat.session_not_found": ErrorSpec(
        404,
        "Enterprise Chat session was not found.",
        "Create a session for the current signed-in user before sending a message.",
        owner="enterprise_chat",
    ),
    "chat.session_expired": ErrorSpec(
        409,
        "Enterprise Chat session has expired.",
        "Create a new session and send a new message.",
        owner="enterprise_chat",
    ),
    "chat.session_cleared": ErrorSpec(
        409,
        "Enterprise Chat session was cleared.",
        "Create a new session instead of reusing the cleared session.",
        owner="enterprise_chat",
    ),
    "chat.session_busy": ErrorSpec(
        409,
        "Enterprise Chat session already has an in-flight operation.",
        "Wait for the current operation to finish before sending or clearing.",
        retryable=True,
        owner="enterprise_chat",
    ),
    "chat.message_duplicate": ErrorSpec(
        409,
        "Enterprise Chat message id was already used.",
        "Create a new client message id; the previous call is never replayed automatically.",
        owner="enterprise_chat",
    ),
    "chat.project_unavailable": ErrorSpec(
        409,
        "Enterprise Chat project or environment is unavailable.",
        "Choose an active project and environment from the current tenant options.",
        owner="enterprise_chat",
    ),
    "chat.model_unavailable": ErrorSpec(
        409,
        "Enterprise Chat model alias is unavailable.",
        "Choose an active model alias that belongs to the selected environment.",
        owner="enterprise_chat",
    ),
    "chat.virtual_key_unavailable": ErrorSpec(
        409,
        "Enterprise Chat project execution identity is unavailable.",
        "Configure exactly one active project Virtual Key for the selected environment.",
        owner="enterprise_chat",
    ),
    "chat.execution_invalid": ErrorSpec(
        403,
        "Enterprise Chat execution grant is invalid.",
        "Create a new session operation after authorization and resources are restored.",
        owner="enterprise_chat",
    ),
    "chat.gateway_unavailable": ErrorSpec(
        503,
        "Enterprise Chat Gateway is temporarily unavailable.",
        "Use the request id to verify execution state before creating a new message.",
        retryable=True,
        owner="enterprise_chat",
    ),
    "chat.session_risk_frozen": ErrorSpec(
        403,
        "Enterprise Chat session is frozen by the session risk guard.",
        "Ask an authorized security administrator to review the session risk record.",
        owner="enterprise_chat",
    ),
    "chat.response_invalid": ErrorSpec(
        502,
        "Enterprise Chat response is not supported.",
        "Use the request id to inspect the governed call without resending the same message id.",
        retryable=True,
        owner="enterprise_chat",
    ),
    # Batch errors expose only fixed scheduler and reconciliation states. They must never include
    # file text, operation parameters, grants, Provider responses, keys, tokens, or internal URLs.
    "batch.request_invalid": ErrorSpec(
        422, "Batch request is invalid.", "Use the documented bounded Batch fields.", owner="batch"
    ),
    "batch.idempotency_required": ErrorSpec(
        400,
        "Batch idempotency key is required.",
        "Send a canonical UUID Idempotency-Key.",
        owner="batch",
    ),
    "batch.idempotency_conflict": ErrorSpec(
        409,
        "Batch idempotency key conflicts with another request.",
        "Use a new idempotency key for a different Batch intent.",
        owner="batch",
    ),
    "batch.not_found": ErrorSpec(
        404, "Batch job was not found.", "Refresh the current tenant Batch job list.", owner="batch"
    ),
    "batch.task_not_found": ErrorSpec(
        404, "Batch task was not found.", "Refresh the current Batch job details.", owner="batch"
    ),
    "batch.binding_unavailable": ErrorSpec(
        409,
        "Batch execution binding is unavailable.",
        "Choose an active project, environment, alias, and unique project key.",
        owner="batch",
    ),
    "batch.file_not_ready": ErrorSpec(
        409, "Batch file is not ready.", "Select a ready file with a token estimate.", owner="batch"
    ),
    "batch.file_security_blocked": ErrorSpec(
        403,
        "Batch file is blocked by the security boundary.",
        "Review the file security report before creating a new job.",
        owner="batch",
    ),
    "batch.cost_estimate_unavailable": ErrorSpec(
        503,
        "Batch cost estimate is unavailable.",
        "Configure complete CNY model prices and retry.",
        retryable=True,
        owner="batch",
    ),
    "batch.budget_exceeded": ErrorSpec(
        402,
        "Batch job exceeds an applicable budget.",
        "Reduce the job or update the governing budget before retrying.",
        owner="batch",
    ),
    "batch.capacity_full": ErrorSpec(
        429,
        "Batch tenant capacity is full.",
        "Wait for queued work to complete before submitting more tasks.",
        retryable=True,
        owner="batch",
    ),
    "batch.gateway_busy": ErrorSpec(
        429,
        "Batch execution is deferred for realtime traffic.",
        "Retry the same unconsumed execution grant after the cooldown.",
        retryable=True,
        owner="batch",
    ),
    "batch.execution_invalid": ErrorSpec(
        403,
        "Batch execution grant is invalid.",
        "Let the scheduler issue a current execution grant.",
        owner="batch",
    ),
    "batch.authorization_revoked": ErrorSpec(
        403,
        "Batch submitter authorization was revoked.",
        "Restore the required Batch and file permissions before submitting new work.",
        owner="batch",
    ),
    "batch.provider_outcome_unknown": ErrorSpec(
        502,
        "Batch provider outcome is unknown.",
        "Reconcile the request id; do not resend the same attempt.",
        owner="batch",
    ),
    "batch.result_not_ready": ErrorSpec(
        409,
        "Batch result is not ready.",
        "Wait for the task to reach a successful terminal state.",
        retryable=True,
        owner="batch",
    ),
    "batch.result_unavailable": ErrorSpec(
        500,
        "Batch result is unavailable.",
        "Use the request id to inspect durable execution facts without resending.",
        owner="batch",
    ),
    "batch.reconciliation_conflict": ErrorSpec(
        409,
        "Batch reconciliation decision conflicts with an existing intent.",
        "Use a new idempotency key for a different decision.",
        owner="batch",
    ),
    "batch.worker_unavailable": ErrorSpec(
        503,
        "Batch worker is unavailable.",
        "Check Batch configuration and durable worker state, then retry safely.",
        retryable=True,
        owner="batch",
    ),
    # 审批错误只暴露稳定状态与恢复建议；密码、二次验证票据、幂等摘要、目标正文、
    # Webhook Endpoint 与数据库细节都必须留在服务端，不能拼接进统一错误信封。
    "approval.input_invalid": ErrorSpec(
        400,
        "Approval request is invalid.",
        "Use the documented exact approval fields and bounded values.",
        owner="approval",
    ),
    "approval.idempotency_required": ErrorSpec(
        400,
        "Approval idempotency key is required.",
        "Send one canonical Idempotency-Key for this decision intent.",
        owner="approval",
    ),
    "approval.not_found": ErrorSpec(
        404,
        "Approval task was not found.",
        "Refresh the current tenant approval list.",
        owner="approval",
    ),
    "approval.state_conflict": ErrorSpec(
        409,
        "Approval task state changed.",
        "Refresh the task and submit a decision against its current revision.",
        owner="approval",
    ),
    "approval.idempotency_conflict": ErrorSpec(
        409,
        "Approval idempotency key conflicts with another intent.",
        "Use a new idempotency key for a different approval decision.",
        owner="approval",
    ),
    "approval.target_conflict": ErrorSpec(
        409,
        "Approval target changed after the task was created.",
        "Refresh the target state and create a new approval task when still required.",
        owner="approval",
    ),
    "approval.expired": ErrorSpec(
        409,
        "Approval task expired before the decision completed.",
        "Refresh the task to inspect the timeout decision.",
        owner="approval",
    ),
    "approval.step_up_required": ErrorSpec(
        403,
        "Approval step-up verification is required.",
        "Complete the current task action verification before deciding.",
        owner="approval",
    ),
    "approval.step_up_invalid": ErrorSpec(
        403,
        "Approval step-up verification is invalid.",
        "Restart verification for the current task and action.",
        owner="approval",
    ),
    "approval.step_up_rate_limited": ErrorSpec(
        429,
        "Approval step-up verification is temporarily locked.",
        "Wait for the verification lock to expire before retrying.",
        retryable=True,
        owner="approval",
    ),
    "approval.capacity_full": ErrorSpec(
        429,
        "Approval tenant capacity is full.",
        "Wait for pending tasks to complete before creating another approval.",
        retryable=True,
        owner="approval",
    ),
    "approval.unavailable": ErrorSpec(
        503,
        "Approval processing is temporarily unavailable.",
        "Retry after durable approval state and scheduling recover.",
        retryable=True,
        owner="approval",
    ),
    "approval.writeback_unavailable": ErrorSpec(
        503,
        "Approval target writeback is not yet available.",
        "Use the request id to inspect durable recovery before retrying.",
        retryable=True,
        owner="approval",
    ),
    "approval.event_unavailable": ErrorSpec(
        503,
        "Approval event delivery is temporarily unavailable.",
        "Retry after the durable approval event worker recovers.",
        retryable=True,
        owner="approval",
    ),
    # 实验错误只暴露稳定对象与恢复语义；name、路由指纹、幂等材料、正文和数据库异常
    # 均留在服务端边界内。
    "experiment.input_invalid": ErrorSpec(
        422,
        "Experiment request is invalid.",
        "Use the documented exact experiment fields and bounded values.",
        owner="experiment",
    ),
    "experiment.query_invalid": ErrorSpec(
        422,
        "Experiment query is invalid.",
        "Use the documented status, limit, and cursor query fields.",
        owner="experiment",
    ),
    "experiment.idempotency_required": ErrorSpec(
        422,
        "Experiment idempotency key is required.",
        "Send one canonical lowercase UUIDv4 Idempotency-Key.",
        owner="experiment",
    ),
    "experiment.model_invalid": ErrorSpec(
        422,
        "Experiment model configuration is invalid.",
        "Choose two distinct active Chat models in the documented experiment scope.",
        owner="experiment",
    ),
    "experiment.not_found": ErrorSpec(
        404,
        "Experiment was not found.",
        "Refresh the current tenant experiment list.",
        owner="experiment",
    ),
    "experiment.idempotency_conflict": ErrorSpec(
        409,
        "Experiment idempotency key conflicts with another request.",
        "Use a new UUIDv4 idempotency key for a different experiment intent.",
        owner="experiment",
    ),
    "experiment.scope_conflict": ErrorSpec(
        409,
        "An active experiment already owns this scope.",
        "Wait for the active experiment sampling window to close.",
        owner="experiment",
    ),
    "experiment.unavailable": ErrorSpec(
        503,
        "Experiment service is temporarily unavailable.",
        "Retry after the experiment database and scheduler recover.",
        retryable=True,
        owner="experiment",
    ),
    "experiment.statistics_unavailable": ErrorSpec(
        503,
        "Experiment statistics are temporarily unavailable.",
        "Retry after the current experiment facts become readable.",
        retryable=True,
        owner="experiment",
    ),
    # 管理助手错误只携带固定码和安全建议；问题、回答、来源值和底层 SQL 异常不能进入正文。
    "admin_assistant.request_invalid": ErrorSpec(
        422,
        "Admin assistant request is invalid.",
        "Send the documented exact request with one bounded text question.",
        owner="admin_assistant",
    ),
    "admin_assistant.read_only_violation": ErrorSpec(
        403,
        "Admin assistant is read-only.",
        "Use the authorized management page for configuration changes.",
        owner="admin_assistant",
    ),
    "admin_assistant.query_unsupported": ErrorSpec(
        422,
        "Admin assistant query is unsupported.",
        "Ask about usage, errors, cost advice, or policy explanation.",
        owner="admin_assistant",
    ),
    "admin_assistant.query_ambiguous": ErrorSpec(
        422,
        "Admin assistant query is ambiguous.",
        "Ask one supported question with one rolling time window.",
        owner="admin_assistant",
    ),
    "admin_assistant.source_not_found": ErrorSpec(
        404,
        "Admin assistant source was not found.",
        "Verify the request reference in the current tenant and retry.",
        owner="admin_assistant",
    ),
    "admin_assistant.rate_limited": ErrorSpec(
        429,
        "Admin assistant query rate limit was reached.",
        "Wait briefly before submitting another bounded query.",
        retryable=True,
        owner="admin_assistant",
    ),
    "admin_assistant.source_unavailable": ErrorSpec(
        503,
        "Admin assistant source is unavailable.",
        "Retry after the current tenant data sources recover.",
        retryable=True,
        owner="admin_assistant",
    ),
    "admin_assistant.answer_invalid": ErrorSpec(
        503,
        "Admin assistant answer failed validation.",
        "Contact an administrator to review the assistant source graph.",
        owner="admin_assistant",
    ),
    "admin_assistant.audit_unavailable": ErrorSpec(
        503,
        "Admin assistant audit is unavailable.",
        "Retry after the audit database recovers.",
        retryable=True,
        owner="admin_assistant",
    ),
    "admin_assistant.event_unavailable": ErrorSpec(
        503,
        "Admin assistant event is unavailable.",
        "Retry event delivery after the outbox recovers.",
        retryable=True,
        owner="admin_assistant",
    ),
    # Response Cache 错误只描述策略、CAS、配置或原子结算边界；不得拼接 tag、data class、
    # Cache Key、Prompt、Response、Credential、金额或底层数据库异常。
    "response_cache.policy_not_found": ErrorSpec(
        404,
        "Response Cache policy was not found.",
        "Refresh the current tenant model alias list and select an existing policy owner.",
        owner="response_cache",
    ),
    "response_cache.version_conflict": ErrorSpec(
        409,
        "Response Cache policy version conflicts with the current version.",
        "Refresh the policy and retry with its current expected version.",
        owner="response_cache",
    ),
    "response_cache.request_conflict": ErrorSpec(
        409,
        "Response Cache idempotency key conflicts with another request.",
        "Use a new idempotency key for a different cache management intent.",
        owner="response_cache",
    ),
    "response_cache.input_invalid": ErrorSpec(
        422,
        "Response Cache request is invalid.",
        "Use the documented exact fields, headers, limits, and machine-code values.",
        owner="response_cache",
    ),
    "response_cache.query_invalid": ErrorSpec(
        422,
        "Response Cache query is invalid.",
        "Use only the documented limit and cursor query fields.",
        owner="response_cache",
    ),
    "response_cache.configuration_unavailable": ErrorSpec(
        503,
        "Response Cache configuration is unavailable.",
        "Check the feature state, independent key versions, and service configuration.",
        owner="response_cache",
    ),
    "response_cache.unavailable": ErrorSpec(
        503,
        "Response Cache management is temporarily unavailable.",
        "Retry after the cache policy store and event outbox recover.",
        retryable=True,
        owner="response_cache",
    ),
    "gateway.response_cache_settlement_unavailable": ErrorSpec(
        503,
        "Response Cache hit settlement is temporarily unavailable.",
        "Use the request id to confirm recovery before starting a new request.",
        retryable=True,
        owner="response_cache",
    ),
    "config_transfer.query_invalid": ErrorSpec(
        422,
        "Configuration transfer query is invalid.",
        "Remove query parameters and retry the documented endpoint.",
        owner="config_transfer",
    ),
    "config_transfer.body_invalid": ErrorSpec(
        400,
        "Configuration transfer body is invalid.",
        "Send a strict UTF-8 application/json object without duplicate keys.",
        owner="config_transfer",
    ),
    "config_transfer.body_too_large": ErrorSpec(
        413,
        "Configuration transfer body exceeds the safety limit.",
        "Reduce the file size, nesting depth, string length, or object count.",
        owner="config_transfer",
    ),
    "config_transfer.schema_unsupported": ErrorSpec(
        422,
        "Configuration transfer schema is unsupported.",
        "Export a new metahub.config/v1 file from a compatible MetaHub control plane.",
        owner="config_transfer",
    ),
    "config_transfer.import_sensitive_field": ErrorSpec(
        422,
        "Configuration import contains a credential field.",
        "Remove every key, token, secret, password, and credential field before retrying.",
        owner="config_transfer",
    ),
    "config_transfer.import_invalid": ErrorSpec(
        422,
        "Configuration import is invalid.",
        "Review the bounded row report and correct the referenced field.",
        owner="config_transfer",
    ),
    "config_transfer.import_request_conflict": ErrorSpec(
        409,
        "Configuration import request conflicts with a previous intent.",
        "Use a new request id for a different manifest.",
        owner="config_transfer",
    ),
    "config_transfer.import_unavailable": ErrorSpec(
        503,
        "Configuration import is temporarily unavailable.",
        "Retry the same intended import after checking the control plane health.",
        retryable=True,
        owner="config_transfer",
    ),
    "config_transfer.export_state_invalid": ErrorSpec(
        409,
        "Current configuration cannot be safely exported.",
        "Repair the invalid tenant configuration and retry the export.",
        owner="config_transfer",
    ),
    "config_transfer.export_request_conflict": ErrorSpec(
        409,
        "Configuration export request id was already used.",
        "Use a new request id because export file content is not persisted for replay.",
        owner="config_transfer",
    ),
    "config_transfer.export_unavailable": ErrorSpec(
        503,
        "Configuration export is temporarily unavailable.",
        "Retry with a new request id after checking the control plane health.",
        retryable=True,
        owner="config_transfer",
    ),
    "user.not_found": ErrorSpec(
        404,
        "User was not found.",
        "Check the user id in the current tenant.",
        owner="user",
    ),
    "user.email_exists": ErrorSpec(
        409,
        "User email already exists.",
        "Use another email address in the current tenant.",
        owner="user",
    ),
    "user.role_invalid": ErrorSpec(
        422,
        "User role is invalid.",
        "Choose one of the six product-defined tenant roles.",
        owner="user",
    ),
    "user.password_invalid": ErrorSpec(
        422,
        "User password is invalid.",
        "Use a temporary password with at least 8 characters.",
        owner="user",
    ),
    "user.create_invalid": ErrorSpec(
        422,
        "User creation payload is invalid.",
        "Check email, display name, role, and status.",
        owner="user",
    ),
    "user.last_admin_required": ErrorSpec(
        409,
        "At least one active admin is required.",
        "Create or promote another administrator before changing this role.",
        owner="user",
    ),
    "user.self_role_change_denied": ErrorSpec(
        409,
        "Self role downgrade is not allowed.",
        "Ask another administrator to change your role.",
        owner="user",
    ),
    "invitation.create_invalid": ErrorSpec(
        422,
        "Invitation payload is invalid.",
        "Check the email address, role, and expiration duration.",
        owner="invitation",
    ),
    "invitation.role_invalid": ErrorSpec(
        422,
        "Invitation role is invalid.",
        "Choose an active role from the current tenant.",
        owner="invitation",
    ),
    "invitation.user_exists": ErrorSpec(
        409,
        "A user with this email already exists.",
        "Use the existing account or invite another email address.",
        owner="invitation",
    ),
    "invitation.pending_exists": ErrorSpec(
        409,
        "A pending invitation already exists.",
        "Use the existing invitation or wait until it expires.",
        owner="invitation",
    ),
    "invitation.not_available": ErrorSpec(
        410,
        "Invitation is not available.",
        "Ask a tenant administrator to create a new invitation.",
        owner="invitation",
    ),
    "invitation.accept_invalid": ErrorSpec(
        422,
        "Invitation acceptance payload is invalid.",
        "Check the display name and password requirements.",
        owner="invitation",
    ),
    "invitation.unavailable": ErrorSpec(
        503,
        "Invitation service is temporarily unavailable.",
        "Retry later with the same intended operation.",
        retryable=True,
        owner="invitation",
    ),
    "tenant.current_not_found": ErrorSpec(
        404,
        "Current tenant was not found.",
        "Sign in again or contact an administrator.",
        owner="tenant",
    ),
    "tenant.update_empty": ErrorSpec(
        422,
        "Tenant update has no editable fields.",
        "Provide at least one editable tenant field.",
        owner="tenant",
    ),
    "tenant.update_invalid": ErrorSpec(
        422,
        "Tenant update payload is invalid.",
        "Check tenant name and service mode.",
        owner="tenant",
    ),
    "environment.not_found": ErrorSpec(
        404,
        "Environment was not found.",
        "Choose an environment in the current tenant.",
        owner="environment",
    ),
    "environment.code_invalid": ErrorSpec(
        422,
        "Environment code is invalid.",
        "Choose dev, test, or prod.",
        owner="environment",
    ),
    "environment.code_conflict": ErrorSpec(
        409,
        "Environment code already exists.",
        "Use the existing environment in the current tenant.",
        owner="environment",
    ),
    "environment.name_invalid": ErrorSpec(
        422,
        "Environment name is invalid.",
        "Use a name between 1 and 80 characters.",
        owner="environment",
    ),
    "environment.description_invalid": ErrorSpec(
        422,
        "Environment description is invalid.",
        "Use a description no longer than 240 characters.",
        owner="environment",
    ),
    "environment.status_invalid": ErrorSpec(
        422,
        "Environment status is invalid.",
        "Choose active or disabled.",
        owner="environment",
    ),
    "environment.update_empty": ErrorSpec(
        422,
        "Environment update has no editable fields.",
        "Provide name, description, or status.",
        owner="environment",
    ),
    "environment.disabled": ErrorSpec(
        409,
        "Environment is disabled.",
        "Enable the environment before binding resources to it.",
        owner="environment",
    ),
    "environment.binding_mismatch": ErrorSpec(
        409,
        "Environment binding does not match.",
        "Refresh environments and choose one from the current tenant.",
        owner="environment",
    ),
    "virtual_key.not_found": ErrorSpec(
        404,
        "Virtual key was not found.",
        "Check the virtual key id in the current tenant.",
        owner="virtual_key",
    ),
    "virtual_key.create_invalid": ErrorSpec(
        422,
        "Virtual key creation payload is invalid.",
        "Check name, environment, and description.",
        owner="virtual_key",
    ),
    "virtual_key.name_invalid": ErrorSpec(
        422,
        "Virtual key name is invalid.",
        "Use a name between 1 and 80 characters.",
        owner="virtual_key",
    ),
    "virtual_key.environment_invalid": ErrorSpec(
        422,
        "Virtual key environment is invalid.",
        "Choose dev, test, or prod.",
        owner="virtual_key",
    ),
    "virtual_key.environment_mismatch": ErrorSpec(
        409,
        "Virtual key environment does not match.",
        "Choose the environment currently bound to the virtual key.",
        owner="virtual_key",
    ),
    "virtual_key.status_invalid": ErrorSpec(
        422,
        "Virtual key status is invalid.",
        "Choose active or disabled.",
        owner="virtual_key",
    ),
    "virtual_key.update_empty": ErrorSpec(
        422,
        "Virtual key update has no editable fields.",
        "Provide at least one editable virtual key field.",
        owner="virtual_key",
    ),
    "virtual_key.limit_invalid": ErrorSpec(
        422,
        "Virtual key limit payload is invalid.",
        "Use positive integers for RPM, TPM, and concurrency limits, or null to clear a limit.",
        owner="virtual_key",
    ),
    "virtual_key.limit_update_empty": ErrorSpec(
        422,
        "Virtual key limit update has no editable fields.",
        "Provide at least one limit field or status.",
        owner="virtual_key",
    ),
    "virtual_key.project_binding_invalid": ErrorSpec(
        422,
        "Virtual key project binding is invalid.",
        "Choose a project and application that belong to the current tenant and match each other.",
        owner="virtual_key",
    ),
    "project.not_found": ErrorSpec(
        404,
        "Project was not found.",
        "Check the project id in the current tenant.",
        owner="project",
    ),
    "project.name_invalid": ErrorSpec(
        422,
        "Project name is invalid.",
        "Use a project name between 1 and 120 characters.",
        owner="project",
    ),
    "project.code_invalid": ErrorSpec(
        422,
        "Project code is invalid.",
        "Use lowercase letters, numbers, underscores, or hyphens, starting with a letter.",
        owner="project",
    ),
    "project.code_conflict": ErrorSpec(
        409,
        "Project code already exists.",
        "Use another project code in the current tenant.",
        owner="project",
    ),
    "project.status_invalid": ErrorSpec(
        422,
        "Project status is invalid.",
        "Choose active, disabled, or archived.",
        owner="project",
    ),
    "project.owner_invalid": ErrorSpec(
        422,
        "Project owner is invalid.",
        "Choose an active user that belongs to the current tenant.",
        owner="project",
    ),
    "project.update_empty": ErrorSpec(
        422,
        "Project update has no editable fields.",
        "Provide at least one editable project field.",
        owner="project",
    ),
    "application.not_found": ErrorSpec(
        404,
        "Application was not found.",
        "Check the application id in the current tenant.",
        owner="application",
    ),
    "application.name_invalid": ErrorSpec(
        422,
        "Application name is invalid.",
        "Use an application name between 1 and 120 characters.",
        owner="application",
    ),
    "application.code_invalid": ErrorSpec(
        422,
        "Application code is invalid.",
        "Use lowercase letters, numbers, underscores, or hyphens, starting with a letter.",
        owner="application",
    ),
    "application.code_conflict": ErrorSpec(
        409,
        "Application code already exists in this project.",
        "Use another application code under the selected project.",
        owner="application",
    ),
    "application.project_mismatch": ErrorSpec(
        422,
        "Application does not belong to the selected project.",
        "Choose an application under the selected project.",
        owner="application",
    ),
    "application.status_invalid": ErrorSpec(
        422,
        "Application status is invalid.",
        "Choose active, disabled, or archived.",
        owner="application",
    ),
    "application.update_empty": ErrorSpec(
        422,
        "Application update has no editable fields.",
        "Provide at least one editable application field.",
        owner="application",
    ),
    "provider.not_found": ErrorSpec(
        404,
        "Provider was not found.",
        "Check the provider id in the current tenant.",
        owner="provider",
    ),
    "provider.create_invalid": ErrorSpec(
        422,
        "Provider creation payload is invalid.",
        "Check provider code, name, type, base URL, and auth type.",
        owner="provider",
    ),
    "provider.code_invalid": ErrorSpec(
        422,
        "Provider code is invalid.",
        "Use lowercase letters, numbers, and underscores.",
        owner="provider",
    ),
    "provider.code_exists": ErrorSpec(
        409,
        "Provider code already exists.",
        "Use another provider code in the current tenant.",
        owner="provider",
    ),
    "provider.name_invalid": ErrorSpec(
        422,
        "Provider name is invalid.",
        "Use a provider name between 1 and 120 characters.",
        owner="provider",
    ),
    "provider.type_invalid": ErrorSpec(
        422,
        "Provider type is invalid.",
        "Choose a supported model endpoint provider type.",
        owner="provider",
    ),
    "provider.auth_type_invalid": ErrorSpec(
        422,
        "Provider auth type is invalid.",
        "Choose none, bearer, or custom_header.",
        owner="provider",
    ),
    "provider.adapter_spec_missing": ErrorSpec(
        422,
        "Provider adapter specification is unavailable.",
        "Choose a provider type registered in the current adapter catalog.",
        owner="provider",
    ),
    "provider.auth_not_supported": ErrorSpec(
        422,
        "Provider authentication is not supported by its adapter specification.",
        "Choose an authentication type allowed by the provider adapter catalog.",
        owner="provider",
    ),
    "gateway.provider_adapter_spec_missing": ErrorSpec(
        503,
        "Provider adapter specification is unavailable for this route.",
        "Deploy a catalog that contains the routed provider type before retrying.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.provider_adapter_spec_invalid": ErrorSpec(
        503,
        "Provider adapter specification does not match the running adapter.",
        "Deploy matching Gateway and adapter catalog artifacts before retrying.",
        retryable=True,
        owner="gateway",
    ),
    "adapter.spec_catalog_unavailable": ErrorSpec(
        503,
        "Provider adapter specification catalog is unavailable.",
        "Check the tenant catalog activation and deployed adapter catalog identity.",
        retryable=True,
        owner="adapter_spec",
    ),
    "adapter.spec_catalog_invalid": ErrorSpec(
        503,
        "Provider adapter specification catalog is invalid.",
        "Deploy a catalog artifact with a valid frozen version and checksum.",
        retryable=True,
        owner="adapter_spec",
    ),
    "adapter.spec_filter_invalid": ErrorSpec(
        422,
        "Provider adapter specification filter is invalid.",
        "Use one supported classification and status filter without tenant fields.",
        owner="adapter_spec",
    ),
    "adapter.spec_tenant_not_found": ErrorSpec(
        404,
        "Adapter specification activation tenant was not found.",
        "Check the target tenant id used by the internal catalog publisher.",
        owner="adapter_spec",
    ),
    "adapter.spec_activation_conflict": ErrorSpec(
        409,
        "Adapter specification activation conflicts with an existing request.",
        "Replay the original catalog identity or use a new request id.",
        owner="adapter_spec",
    ),
    "adapter.spec_activation_invalid": ErrorSpec(
        422,
        "Adapter specification activation payload is invalid.",
        "Send only the current catalog version and complete lowercase SHA-256 hash.",
        owner="adapter_spec",
    ),
    "adapter.contract_report_invalid": ErrorSpec(
        422,
        "Provider adapter contract report is invalid.",
        "Regenerate the strict report with the current contract runner and retry.",
        owner="adapter_contract",
    ),
    "adapter.contract_catalog_mismatch": ErrorSpec(
        409,
        "Provider adapter contract catalog does not match the active catalog.",
        "Activate and deploy the same adapter catalog before uploading the report.",
        owner="adapter_contract",
    ),
    "adapter.contract_run_conflict": ErrorSpec(
        409,
        "Provider adapter contract run conflicts with an immutable report.",
        "Replay the original report or use a new request and run identity.",
        owner="adapter_contract",
    ),
    "adapter.contract_run_not_found": ErrorSpec(
        404,
        "Provider adapter contract run was not found.",
        "Check the run id in the current tenant.",
        owner="adapter_contract",
    ),
    "adapter.contract_storage_unavailable": ErrorSpec(
        503,
        "Provider adapter contract storage is temporarily unavailable.",
        "Retry after the control plane database and event outbox recover.",
        retryable=True,
        owner="adapter_contract",
    ),
    "provider.base_url_invalid": ErrorSpec(
        422,
        "Provider base URL is invalid.",
        "Use an http:// or https:// endpoint base URL.",
        owner="provider",
    ),
    "provider.status_invalid": ErrorSpec(
        422,
        "Provider status is invalid.",
        "Choose active or disabled.",
        owner="provider",
    ),
    "provider.update_empty": ErrorSpec(
        422,
        "Provider update has no editable fields.",
        "Provide at least one editable provider field.",
        owner="provider",
    ),
    "provider.maintenance_note_invalid": ErrorSpec(
        422,
        "Provider maintenance note is invalid.",
        "Use at most 240 characters without control characters.",
        owner="provider",
    ),
    "provider_status.filter_invalid": ErrorSpec(
        422,
        "Provider status filter is invalid.",
        "Use a supported provider, health, or configuration status filter.",
        owner="provider_status",
    ),
    "provider_status.pagination_invalid": ErrorSpec(
        422,
        "Provider status pagination is invalid.",
        "Use limit 1..100 and offset 0..10000.",
        owner="provider_status",
    ),
    "provider_status.query_unavailable": ErrorSpec(
        503,
        "Provider status is temporarily unavailable.",
        "Retry after the control-plane data store recovers.",
        retryable=True,
        owner="provider_status",
    ),
    "provider_health.filter_invalid": ErrorSpec(
        422,
        "Provider health filter is invalid.",
        "Use a supported health, provider status, or provider type filter.",
        owner="provider_health",
    ),
    "provider_health.pagination_invalid": ErrorSpec(
        422,
        "Provider health pagination is invalid.",
        "Use limit 1..100 and offset 0..10000.",
        owner="provider_health",
    ),
    "provider_health.probe_in_progress": ErrorSpec(
        409,
        "Provider probe is already in progress.",
        "Wait for the active probe lease to finish and retry.",
        retryable=True,
        owner="provider_health",
    ),
    "provider_health.probe_not_due": ErrorSpec(
        409,
        "Provider probe is not due.",
        "Use the manual probe endpoint or wait for the next scheduled time.",
        owner="provider_health",
    ),
    "provider_health.probe_superseded": ErrorSpec(
        409,
        "Provider probe result was superseded by a configuration change.",
        "Retry the probe against the current provider configuration.",
        retryable=True,
        owner="provider_health",
    ),
    "provider_key.not_found": ErrorSpec(
        404,
        "Provider key was not found.",
        "Check the provider key id in the current tenant.",
        owner="provider_key",
    ),
    "provider_key.create_invalid": ErrorSpec(
        422,
        "Provider key creation payload is invalid.",
        "Check key name, plain value, and weight.",
        owner="provider_key",
    ),
    "provider_key.name_invalid": ErrorSpec(
        422,
        "Provider key name is invalid.",
        "Use a provider key name between 1 and 80 characters.",
        owner="provider_key",
    ),
    "provider_key.plain_value_invalid": ErrorSpec(
        422,
        "Provider key value is invalid.",
        "Provide a non-empty upstream API key.",
        owner="provider_key",
    ),
    "provider_key.weight_invalid": ErrorSpec(
        422,
        "Provider key weight is invalid.",
        "Use a weight between 1 and 10000.",
        owner="provider_key",
    ),
    "provider_key.status_invalid": ErrorSpec(
        422,
        "Provider key status is invalid.",
        "Choose active or disabled.",
        owner="provider_key",
    ),
    "provider_key.duplicate": ErrorSpec(
        409,
        "Provider key already exists.",
        "Use another upstream API key for this provider.",
        owner="provider_key",
    ),
    "provider_key.update_empty": ErrorSpec(
        422,
        "Provider key update has no editable fields.",
        "Provide at least one editable provider key field.",
        owner="provider_key",
    ),
    "provider_key.encryption_failed": ErrorSpec(
        500,
        "Provider key encryption failed.",
        "Configure METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET and retry.",
        owner="provider_key",
    ),
    "provider_key.unavailable": ErrorSpec(
        503,
        "No usable provider key is available.",
        "Enable a managed provider key for the current tenant. The environment fallback is "
        "limited to the non-production default tenant and its configured Provider endpoint.",
        retryable=True,
        owner="provider_key",
    ),
    "model.not_found": ErrorSpec(
        404,
        "Model was not found.",
        "Check the model id in the current tenant.",
        owner="model",
    ),
    "model_price.model_required": ErrorSpec(
        422,
        "Model id is required for a price plan.",
        "Choose a model from the current tenant.",
        owner="model_price",
    ),
    "model_price.model_not_found": ErrorSpec(
        404,
        "Model for the price plan was not found.",
        "Refresh the model catalog and choose a model in the current tenant.",
        owner="model_price",
    ),
    "model_price.price_invalid": ErrorSpec(
        422,
        "Model price is invalid.",
        "Use finite non-negative prices with at most 12 decimal places.",
        owner="model_price",
    ),
    "model_price.currency_invalid": ErrorSpec(
        422,
        "Model price currency is invalid.",
        "Choose CNY or USD; currency conversion is not automatic.",
        owner="model_price",
    ),
    "model_price.effective_from_invalid": ErrorSpec(
        422,
        "Model price effective time is invalid.",
        "Provide an ISO timestamp with an explicit timezone.",
        owner="model_price",
    ),
    "model_price.version_invalid": ErrorSpec(
        422,
        "Model price version is invalid.",
        "Use up to 80 letters, numbers, dots, underscores, colons, or hyphens.",
        owner="model_price",
    ),
    "model_price.effective_conflict": ErrorSpec(
        409,
        "A model price already starts at this time.",
        "Choose another effective time for this model.",
        owner="model_price",
    ),
    "model_price.version_conflict": ErrorSpec(
        409,
        "Model price version already exists.",
        "Use another version for this model.",
        owner="model_price",
    ),
    "model_price.query_invalid": ErrorSpec(
        422,
        "Model price query is invalid.",
        "Check effective status and pagination parameters.",
        owner="model_price",
    ),
    "model.create_invalid": ErrorSpec(
        422,
        "Model creation payload is invalid.",
        "Check internal model id, provider, capabilities, context window, and prices.",
        owner="model",
    ),
    "model.internal_id_invalid": ErrorSpec(
        422,
        "Internal model id is invalid.",
        "Use lowercase letters, numbers, dots, underscores, or hyphens.",
        owner="model",
    ),
    "model.internal_id_exists": ErrorSpec(
        409,
        "Internal model id already exists.",
        "Use another internal model id in the current tenant.",
        owner="model",
    ),
    "model.provider_invalid": ErrorSpec(
        422,
        "Model provider is invalid.",
        "Choose a provider that belongs to the current tenant.",
        owner="model",
    ),
    "model.provider_model_id_invalid": ErrorSpec(
        422,
        "Provider model id is invalid.",
        "Use the upstream provider's model id.",
        owner="model",
    ),
    "model.capabilities_invalid": ErrorSpec(
        422,
        "Model capabilities are invalid.",
        "Use a JSON object with boolean capability flags.",
        owner="model",
    ),
    "model.context_window_invalid": ErrorSpec(
        422,
        "Model context window is invalid.",
        "Use a non-negative integer context length.",
        owner="model",
    ),
    "model.price_invalid": ErrorSpec(
        422,
        "Model price is invalid.",
        "Use non-negative numeric prices per 1K tokens.",
        owner="model",
    ),
    "model.currency_invalid": ErrorSpec(
        422,
        "Model currency is invalid.",
        "Choose CNY or USD.",
        owner="model",
    ),
    "model.status_invalid": ErrorSpec(
        422,
        "Model status is invalid.",
        "Choose active or disabled.",
        owner="model",
    ),
    "model.update_empty": ErrorSpec(
        422,
        "Model update has no editable fields.",
        "Provide at least one editable model field.",
        owner="model",
    ),
    "model_alias.not_found": ErrorSpec(
        404,
        "Model alias was not found.",
        "Check the model alias id in the current tenant.",
        owner="model_alias",
    ),
    "model_alias.create_invalid": ErrorSpec(
        422,
        "Model alias creation payload is invalid.",
        "Check alias, environment, default model, fallback model, and status.",
        owner="model_alias",
    ),
    "model_alias.alias_invalid": ErrorSpec(
        422,
        "Model alias is invalid.",
        "Use lowercase letters, numbers, dots, underscores, or hyphens.",
        owner="model_alias",
    ),
    "model_alias.environment_invalid": ErrorSpec(
        422,
        "Model alias environment is invalid.",
        "Choose dev, test, or prod.",
        owner="model_alias",
    ),
    "model_alias.environment_mismatch": ErrorSpec(
        409,
        "Model alias environment does not match.",
        "Refresh environments and choose the model alias environment again.",
        owner="model_alias",
    ),
    "model_alias.model_invalid": ErrorSpec(
        422,
        "Model alias target model is invalid.",
        "Choose active models that belong to the current tenant.",
        owner="model_alias",
    ),
    "model_alias.status_invalid": ErrorSpec(
        422,
        "Model alias status is invalid.",
        "Choose active or disabled.",
        owner="model_alias",
    ),
    "model_alias.duplicate": ErrorSpec(
        409,
        "Model alias already exists in this environment.",
        "Use another alias or environment in the current tenant.",
        owner="model_alias",
    ),
    "model_alias.update_empty": ErrorSpec(
        422,
        "Model alias update has no editable fields.",
        "Provide at least one editable model alias field.",
        owner="model_alias",
    ),
    "model_alias.fallback_invalid": ErrorSpec(
        422,
        "Fallback policy is invalid.",
        "Choose a distinct active fallback model and at least one supported trigger condition.",
        owner="model_alias",
    ),
    "model_alias.fallback_model_not_found": ErrorSpec(
        404,
        "Fallback model was not found.",
        "Choose an available model in the current tenant.",
        owner="model_alias",
    ),
    "model_alias.fallback_model_disabled": ErrorSpec(
        409,
        "Fallback model is disabled.",
        "Enable the fallback model before enabling the policy.",
        owner="model_alias",
    ),
    "model_alias.fallback_update_unavailable": ErrorSpec(
        503,
        "Fallback policy could not be updated.",
        "Retry the update and use request_id to inspect service logs if it continues to fail.",
        retryable=True,
        owner="model_alias",
    ),
    "cache_affinity.version_conflict": ErrorSpec(
        409,
        "Cache affinity configuration was changed by another request.",
        "Refresh the model alias and retry from the latest cache affinity version.",
        owner="cache_affinity",
    ),
    "cache_affinity.request_conflict": ErrorSpec(
        409,
        "The request id was already used for another cache affinity change.",
        "Use the original payload or retry with a new request id.",
        owner="cache_affinity",
    ),
    "cache_affinity.input_invalid": ErrorSpec(
        422,
        "Cache affinity payload is invalid.",
        "Check status, data class, prefix threshold, TTL, version, and bounded JSON fields.",
        owner="cache_affinity",
    ),
    "cache_affinity.update_failed": ErrorSpec(
        503,
        "Cache affinity configuration could not be updated.",
        "Retry the change and use request_id to inspect service logs if it continues to fail.",
        retryable=True,
        owner="cache_affinity",
    ),
    "model_alias.routing_mode_invalid": ErrorSpec(
        422,
        "Routing mode payload is invalid.",
        (
            "Choose fixed, cost_first, availability_first, or quality_cost and remove "
            "unsupported fields."
        ),
        owner="model_alias",
    ),
    "model_alias.quality_cost_settings_invalid": ErrorSpec(
        422,
        "Quality-cost routing settings are invalid.",
        (
            "Use complete basis-point weights totaling 10000, a positive quality floor, "
            "and a positive sample threshold."
        ),
        owner="model_alias",
    ),
    "model_alias.routing_policy_version_invalid": ErrorSpec(
        422,
        "Routing policy version is invalid.",
        "Provide the current positive integer routing policy version.",
        owner="model_alias",
    ),
    "model_alias.routing_policy_version_conflict": ErrorSpec(
        409,
        "Routing policy was changed by another request.",
        "Refresh the model alias and retry from the latest routing policy version.",
        owner="model_alias",
    ),
    "model_alias.routing_policy_request_conflict": ErrorSpec(
        409,
        "The request id was already used for another routing policy change.",
        "Use the original payload or retry with a new request id.",
        owner="model_alias",
    ),
    "model_alias.routing_policy_update_failed": ErrorSpec(
        503,
        "Routing policy could not be updated.",
        "Retry the change and use request_id to inspect service logs if it continues to fail.",
        retryable=True,
        owner="model_alias",
    ),
    "model_quality_score.query_invalid": ErrorSpec(
        422,
        "Model quality score query is invalid.",
        "Use at most one model_id with limit between 1 and 200 and a non-negative offset.",
        owner="model_quality_score",
    ),
    "model_quality_score.input_invalid": ErrorSpec(
        422,
        "Model quality score payload is invalid.",
        (
            "Provide model_id, manual_quality_bps from 0 to 10000 or null, and the expected "
            "feedback version."
        ),
        owner="model_quality_score",
    ),
    "model_quality_score.version_invalid": ErrorSpec(
        422,
        "Model quality score version is invalid.",
        "Provide the current positive integer feedback version.",
        owner="model_quality_score",
    ),
    "model_quality_score.not_found": ErrorSpec(
        404,
        "Model quality score was not found.",
        "Check the model id in the current tenant.",
        owner="model_quality_score",
    ),
    "model_quality_score.version_conflict": ErrorSpec(
        409,
        "Model quality score was changed by another request.",
        "Refresh the score and retry from the latest feedback version.",
        owner="model_quality_score",
    ),
    "model_quality_score.request_conflict": ErrorSpec(
        409,
        "The request id was already used for another model quality score change.",
        "Use the original payload or retry with a new request id.",
        owner="model_quality_score",
    ),
    "model_quality_score.unavailable": ErrorSpec(
        503,
        "Model quality scores are temporarily unavailable.",
        "Retry the query and use request_id to inspect service logs if it continues to fail.",
        retryable=True,
        owner="model_quality_score",
    ),
    "model_quality_score.update_failed": ErrorSpec(
        503,
        "Model quality score could not be updated.",
        "Retry the change and use request_id to inspect service logs if it continues to fail.",
        retryable=True,
        owner="model_quality_score",
    ),
    "feedback.request_not_found": ErrorSpec(
        404,
        "The source request was not found.",
        "Check the request id available to the current tenant or Virtual Key.",
        owner="feedback",
    ),
    "feedback.request_not_feedbackable": ErrorSpec(
        409,
        "The source request cannot be rated.",
        "Rate only completed successful requests with final model attribution.",
        owner="feedback",
    ),
    "feedback.already_exists": ErrorSpec(
        409,
        "Feedback already exists for the source request.",
        "Read the existing immutable feedback instead of submitting another operation.",
        owner="feedback",
    ),
    "feedback.request_conflict": ErrorSpec(
        409,
        "The request id was already used for another feedback intent.",
        "Reuse the original normalized payload or submit a new operation request id.",
        owner="feedback",
    ),
    "feedback.input_invalid": ErrorSpec(
        422,
        "Feedback payload is invalid.",
        "Provide request_id, integer rating 1 to 5, boolean accepted, and allowed tags only.",
        owner="feedback",
    ),
    "feedback.query_invalid": ErrorSpec(
        422,
        "Feedback summary query is invalid.",
        "Use a canonical UTC window up to 366 days and supported model pagination fields.",
        owner="feedback",
    ),
    "feedback.unavailable": ErrorSpec(
        503,
        "Feedback is temporarily unavailable.",
        "Retry with the same operation request id and inspect safe service logs if needed.",
        retryable=True,
        owner="feedback",
    ),
    "feedback.record_failed": ErrorSpec(
        500,
        "Feedback could not be recorded consistently.",
        "Retry with the same operation request id after checking migration and database integrity.",
        retryable=True,
        owner="feedback",
    ),
    # Provider 错误映射使用独立错误域；正文固定且不回显规则 pattern、上游响应或数据库异常。
    "error_map.not_found": ErrorSpec(
        404,
        "Provider error map was not found.",
        "Refresh the error map list and retry with a resource from the current tenant.",
        owner="error_map",
    ),
    "error_map.input_invalid": ErrorSpec(
        422,
        "Provider error map input is invalid.",
        "Check the documented rule fields, types, and bounded values before retrying.",
        owner="error_map",
    ),
    "error_map.update_empty": ErrorSpec(
        422,
        "Provider error map update is empty.",
        "Submit at least one supported field that changes the current rule.",
        owner="error_map",
    ),
    "error_map.output_code_invalid": ErrorSpec(
        422,
        "Provider error map output code is invalid.",
        "Choose one of the supported normalized upstream error codes.",
        owner="error_map",
    ),
    "error_map.pattern_invalid": ErrorSpec(
        422,
        "Provider error map pattern is invalid.",
        "Use a bounded equals or contains pattern that satisfies the rule scope.",
        owner="error_map",
    ),
    "error_map.pattern_sensitive": ErrorSpec(
        422,
        "Provider error map pattern may contain sensitive data.",
        "Remove credentials, URLs, and high-entropy secret fragments from the pattern.",
        owner="error_map",
    ),
    "error_map.conflict": ErrorSpec(
        409,
        "Provider error map conflicts with an active rule.",
        "Refresh the rule set and choose a non-conflicting scope, priority, or condition.",
        owner="error_map",
    ),
    "error_map.version_conflict": ErrorSpec(
        409,
        "Provider error map version changed.",
        "Load the latest rule version and submit a new request id after reconciling the draft.",
        owner="error_map",
    ),
    "error_map.request_conflict": ErrorSpec(
        409,
        "Provider error map request id was reused with different input.",
        "Generate a new request id for the changed mutation.",
        owner="error_map",
    ),
    "error_map.limit_exceeded": ErrorSpec(
        409,
        "Provider error map limit was reached.",
        "Disable or consolidate rules before creating or activating another rule.",
        owner="error_map",
    ),
    "error_map.unavailable": ErrorSpec(
        503,
        "Provider error map service is unavailable.",
        "Retry after the control-plane database and rule registry recover.",
        retryable=True,
        owner="error_map",
    ),
    "error_map.snapshot_unavailable": ErrorSpec(
        503,
        "Provider error map snapshot is unavailable.",
        "Keep the last known good snapshot and retry synchronization later.",
        retryable=True,
        owner="error_map",
    ),
    "error_map.event_unavailable": ErrorSpec(
        503,
        "Provider error map event is unavailable.",
        "Retry after the source outbox and Webhook dispatcher recover.",
        retryable=True,
        owner="error_map",
    ),
    "error_map.tenant_unavailable": ErrorSpec(
        404,
        "Provider error map tenant is unavailable.",
        "Check the authenticated tenant identity used by the internal snapshot request.",
        owner="error_map",
    ),
    "request_log.not_found": ErrorSpec(
        404,
        "Request log was not found.",
        "Check the request_id in the current tenant.",
        owner="request_log",
    ),
    "request_log.query_invalid": ErrorSpec(
        422,
        "Request log query is invalid.",
        "Check request_id, model, provider_code, limit, and offset.",
        owner="request_log",
    ),
    "request_log.status_invalid": ErrorSpec(
        422,
        "Request log status is invalid.",
        "Choose success or failed.",
        owner="request_log",
    ),
    "request_log.sort_invalid": ErrorSpec(
        422,
        "Request log sort is invalid.",
        "Choose an allowed sort field and direction.",
        owner="request_log",
    ),
    "request_log.pagination_invalid": ErrorSpec(
        422,
        "Request log pagination is invalid.",
        "Use limit 1..100 and offset 0..10000.",
        owner="request_log",
    ),
    "request_log.query_unavailable": ErrorSpec(
        503,
        "Request log query is unavailable.",
        "Retry after the trace database is available.",
        retryable=True,
        owner="request_log",
    ),
    "output_safety.integrity_failed": ErrorSpec(
        503,
        "Output safety evidence failed integrity validation.",
        "Use the request id to inspect tenant-qualified scan and billing evidence.",
        owner="output_safety",
    ),
    "request_log.query_audit_unavailable": ErrorSpec(
        503,
        "Request log query audit is unavailable.",
        "Restore the query audit store before retrying.",
        retryable=True,
        owner="request_log",
    ),
    "request_log.query_event_unavailable": ErrorSpec(
        503,
        "Request log query event is unavailable.",
        "Retry event processing after storage is available.",
        retryable=True,
        owner="request_log",
    ),
    "saved_filter.not_found": ErrorSpec(
        404,
        "Saved filter was not found.",
        "Use a saved filter owned by the current user and tenant.",
        owner="saved_filter",
    ),
    "saved_filter.invalid": ErrorSpec(
        422,
        "Saved filter is invalid.",
        "Check the name, filters, sorting, and version.",
        owner="saved_filter",
    ),
    "saved_filter.name_conflict": ErrorSpec(
        409,
        "Saved filter name already exists.",
        "Choose another name for the current user.",
        owner="saved_filter",
    ),
    "saved_filter.limit_exceeded": ErrorSpec(
        409,
        "Saved filter limit was reached.",
        "Delete an active saved filter before creating another.",
        owner="saved_filter",
    ),
    "saved_filter.version_conflict": ErrorSpec(
        409,
        "Saved filter version is stale.",
        "Refresh saved filters and retry with the current version.",
        retryable=True,
        owner="saved_filter",
    ),
    "saved_filter.unavailable": ErrorSpec(
        503,
        "Saved filters are unavailable.",
        "Retry after the saved filter store is available.",
        retryable=True,
        owner="saved_filter",
    ),
    # 请求解释错误与普通日志查询分域，前端可以区分不可见、输入错误和证据依赖故障。
    "request_explanation.not_found": ErrorSpec(
        404,
        "Request explanation was not found.",
        "Use a source request id visible to the current tenant.",
        owner="request_explanation",
    ),
    "request_explanation.request_id_invalid": ErrorSpec(
        422,
        "Source request id is invalid.",
        "Use a 1 to 128 character canonical request id.",
        owner="request_explanation",
    ),
    "request_explanation.request_invalid": ErrorSpec(
        422,
        "Request explanation input is invalid.",
        "Remove unsupported query parameters, body, or content encoding.",
        owner="request_explanation",
    ),
    "request_explanation.unavailable": ErrorSpec(
        503,
        "Request explanations are temporarily unavailable.",
        "Retry after the control plane database and migration are available.",
        retryable=True,
        owner="request_explanation",
    ),
    "request_explanation.audit_unavailable": ErrorSpec(
        503,
        "Request explanation access could not be audited.",
        "Retry with the same source request id after audit storage recovers.",
        retryable=True,
        owner="request_explanation",
    ),
    "request_explanation.integrity_failed": ErrorSpec(
        500,
        "Request explanation evidence is inconsistent.",
        "Inspect migration and tenant ownership integrity using safe diagnostics.",
        owner="request_explanation",
    ),
    "request_explanation.event_unavailable": ErrorSpec(
        503,
        "Request explanation view event is temporarily unavailable.",
        "Inspect the persistent event outbox and retry delivery without exposing payloads.",
        retryable=True,
        owner="request_explanation",
    ),
    "usage.query_invalid": ErrorSpec(
        422,
        "Usage query is invalid.",
        "Check date range, model_id, virtual_key_id, provider_id, estimated, limit, and offset.",
        owner="usage",
    ),
    "usage.date_invalid": ErrorSpec(
        422,
        "Usage date range is invalid.",
        "Use YYYY-MM-DD and keep start_date earlier than or equal to end_date.",
        owner="usage",
    ),
    "usage.export_not_found": ErrorSpec(
        404,
        "Usage export was not found.",
        "Check the export job id in the current tenant.",
        owner="usage",
    ),
    "usage.export_not_ready": ErrorSpec(
        409,
        "Usage export is not ready.",
        "Wait for the asynchronous export to complete before downloading.",
        retryable=True,
        owner="usage",
    ),
    "usage.export_expired": ErrorSpec(
        410,
        "Usage export has expired.",
        "Create a new export for the required date range.",
        owner="usage",
    ),
    "usage.export_too_large": ErrorSpec(
        413,
        "Usage export is too large.",
        "Narrow the date, project, Virtual Key, or model filters and retry.",
        owner="usage",
    ),
    "usage.export_filter_invalid": ErrorSpec(
        422,
        "Usage export filter is invalid.",
        "Use only the documented usage export filters.",
        owner="usage",
    ),
    "usage.export_unavailable": ErrorSpec(
        503,
        "Usage export is unavailable.",
        "Check export job storage and retry with the same request id.",
        retryable=True,
        owner="usage",
    ),
    "finops.query_invalid": ErrorSpec(
        422,
        "FinOps ledger query is invalid.",
        (
            "Use only documented ledger filters, limits, and the cursor returned for this "
            "tenant and filter set."
        ),
        owner="finops",
    ),
    "finops.adjustment_invalid": ErrorSpec(
        422,
        "FinOps adjustment is invalid.",
        "Check the adjustment type, field combination, reason, and Idempotency-Key header.",
        owner="finops",
    ),
    "finops.amount_invalid": ErrorSpec(
        422,
        "FinOps amount is invalid.",
        "Use a positive plain decimal string with no more than six fractional digits.",
        owner="finops",
    ),
    "finops.entry_not_found": ErrorSpec(
        404,
        "FinOps ledger entry was not found.",
        "Check the ledger entry id in the current tenant.",
        owner="finops",
    ),
    "finops.request_not_found": ErrorSpec(
        404,
        "Related billed request was not found.",
        "Check the request id in the current tenant and confirm that it has a formal usage entry.",
        owner="finops",
    ),
    "finops.reversal_not_allowed": ErrorSpec(
        409,
        "FinOps ledger entry cannot be reversed.",
        "Choose a non-reversal ledger entry with a positive amount.",
        owner="finops",
    ),
    "finops.reversal_exceeds_remaining": ErrorSpec(
        409,
        "FinOps reversal exceeds the remaining amount.",
        "Reduce the reversal amount to the unreversed balance of the target entry.",
        owner="finops",
    ),
    "finops.idempotency_conflict": ErrorSpec(
        409,
        "FinOps idempotency key conflicts with an existing adjustment.",
        "Reuse an Idempotency-Key only with the exact same normalized adjustment payload.",
        owner="finops",
    ),
    "finops.ledger_unavailable": ErrorSpec(
        503,
        "FinOps ledger is temporarily unavailable.",
        "Retry after the control plane database and FinOps migration are available.",
        retryable=True,
        owner="finops",
    ),
    "finops.usage_record_unavailable": ErrorSpec(
        503,
        "Usage could not be recorded in the FinOps ledger.",
        "Retry after the ledger database or durable usage spool is available.",
        retryable=True,
        owner="finops",
    ),
    "finops.adjustment_failed": ErrorSpec(
        500,
        "FinOps adjustment could not be recorded consistently.",
        "Inspect the FinOps and audit migration integrity before retrying the adjustment.",
        owner="finops",
    ),
    "audit_log.filter_invalid": ErrorSpec(
        422,
        "Audit log filter is invalid.",
        "Check actor, action, object, and request id filters.",
        owner="audit_log",
    ),
    "audit_log.time_range_invalid": ErrorSpec(
        422,
        "Audit log time range is invalid.",
        "Use timezone-aware ISO-8601 values and keep created_to later than created_from.",
        owner="audit_log",
    ),
    "audit_log.pagination_invalid": ErrorSpec(
        422,
        "Audit log pagination is invalid.",
        "Use limit from 1 to 100 and a non-negative offset.",
        owner="audit_log",
    ),
    "audit_log.summary_invalid": ErrorSpec(
        500,
        "Audit summary is unsafe or invalid.",
        "Review the server-side audit field allowlist before retrying the management change.",
        owner="audit_log",
    ),
    "audit_log.record_unavailable": ErrorSpec(
        503,
        "Audit record could not be stored.",
        "Retry after the control plane database and audit migration are ready.",
        retryable=True,
        owner="audit_log",
    ),
    "audit_log.query_unavailable": ErrorSpec(
        503,
        "Audit logs are temporarily unavailable.",
        "Check the control plane database and audit migration, then retry.",
        retryable=True,
        owner="audit_log",
    ),
    # 审计导出错误与只读列表分域，调用方才能区分筛选错误、幂等冲突、规模门禁和证据损坏。
    "audit_export.filter_invalid": ErrorSpec(
        422,
        "Audit export filter is invalid.",
        "Use only the documented audit export filters and do not send tenant or pagination fields.",
        owner="audit_export",
    ),
    "audit_export.request_conflict": ErrorSpec(
        409,
        "Audit export request conflicts with an existing operation.",
        "Use the original filters for this request id or start a new export with a new request id.",
        owner="audit_export",
    ),
    "audit_export.too_large": ErrorSpec(
        413,
        "Audit export exceeds the bounded CSV limit.",
        "Narrow the time, actor, action, or object filters and retry with a new request id.",
        owner="audit_export",
    ),
    "audit_export.integrity_failed": ErrorSpec(
        500,
        "Audit export integrity verification failed.",
        "Inspect the immutable audit and export facts before attempting another export.",
        owner="audit_export",
    ),
    "audit_export.unavailable": ErrorSpec(
        503,
        "Audit export is temporarily unavailable.",
        "Retry after the control plane database and audit export worker are available.",
        retryable=True,
        owner="audit_export",
    ),
    "budget.rule_conflict": ErrorSpec(
        409,
        "Budget rule already exists for this scope.",
        "Refresh the budget list and update the existing rule instead.",
        owner="budget",
    ),
    "budget.scope_mismatch": ErrorSpec(
        409,
        "Budget scope bindings do not match.",
        "Refresh Project, Application, Virtual Key, and Environment bindings before retrying.",
        owner="budget",
    ),
    "budget.rule_invalid": ErrorSpec(
        422,
        "Budget rule payload is invalid.",
        "Check scope, daily CNY budget, threshold, action, and status.",
        owner="budget",
    ),
    "budget.rule_not_found": ErrorSpec(
        404,
        "Budget rule was not found.",
        "Check the rule id in the current tenant.",
        owner="budget",
    ),
    "budget.mode_invalid": ErrorSpec(
        422,
        "Budget exhaustion mode is invalid.",
        "Choose block or alert_only.",
        owner="budget",
    ),
    "budget.scope_not_found": ErrorSpec(
        404,
        "Budget scope was not found.",
        (
            "Choose a tenant, project, application, or Virtual Key scope that belongs to "
            "the current tenant."
        ),
        owner="budget",
    ),
    "budget.environment_mismatch": ErrorSpec(
        409,
        "Budget environment does not match its scope.",
        "Choose the environment bound to the Virtual Key or refresh environment data.",
        owner="budget",
    ),
    "budget.update_empty": ErrorSpec(
        422,
        "Budget update has no editable fields.",
        "Provide at least one budget rule field.",
        owner="budget",
    ),
    "budget.status_unavailable": ErrorSpec(
        503,
        "Budget status is unavailable.",
        "Check the FinOps accounting ledger and control plane database readiness.",
        retryable=True,
        owner="budget",
    ),
    # 成本异常查询、处置与异步检测错误保持独立命名空间，便于控制台按固定 code 恢复交互。
    "cost_anomaly.query_invalid": ErrorSpec(
        422,
        "Cost anomaly query is invalid.",
        (
            "Use only documented filters, time ranges, limits, and the cursor returned "
            "for this tenant."
        ),
        owner="cost_anomaly",
    ),
    "cost_anomaly.not_found": ErrorSpec(
        404,
        "Cost anomaly resource was not found.",
        (
            "Refresh cost anomalies and use an anomaly, Environment, or Virtual Key from "
            "the current tenant."
        ),
        owner="cost_anomaly",
    ),
    "cost_anomaly.media_type_invalid": ErrorSpec(
        415,
        "Cost anomaly action media type is invalid.",
        "Send application/json with UTF-8 encoding.",
        owner="cost_anomaly",
    ),
    "cost_anomaly.action_invalid": ErrorSpec(
        422,
        "Cost anomaly action is invalid.",
        "Check the Idempotency-Key, action, reason, duration, and bounded rate limits.",
        owner="cost_anomaly",
    ),
    "cost_anomaly.action_conflict": ErrorSpec(
        409,
        "Cost anomaly action conflicts with the current state.",
        "Refresh the anomaly and choose an action allowed by its current status.",
        owner="cost_anomaly",
    ),
    "cost_anomaly.idempotency_conflict": ErrorSpec(
        409,
        "Cost anomaly idempotency key conflicts with another intent.",
        "Reuse an Idempotency-Key only for the exact same anomaly path, action, and parameters.",
        owner="cost_anomaly",
    ),
    "cost_anomaly.mitigation_not_applicable": ErrorSpec(
        409,
        "Temporary rate limiting does not apply to this anomaly.",
        "Choose a Virtual Key anomaly that recommends temporary rate limiting.",
        owner="cost_anomaly",
    ),
    "cost_anomaly.mitigation_active": ErrorSpec(
        409,
        "This anomaly already has an active temporary rate limit.",
        "Refresh the anomaly and release its active mitigation before choosing another action.",
        owner="cost_anomaly",
    ),
    "cost_anomaly.mitigation_expired": ErrorSpec(
        409,
        "The cost anomaly mitigation window has expired.",
        "Review current usage and act on a newer anomaly if the abnormal spend continues.",
        owner="cost_anomaly",
    ),
    "cost_anomaly.mitigation_key_unavailable": ErrorSpec(
        409,
        "The affected Virtual Key is unavailable for mitigation.",
        "Refresh the Key status and Environment binding before retrying.",
        owner="cost_anomaly",
    ),
    "cost_anomaly.mitigation_no_effect": ErrorSpec(
        409,
        "The proposed temporary rate limit would not tighten any dimension.",
        "Lower at least one effective RPM, TPM, or concurrency limit.",
        owner="cost_anomaly",
    ),
    "cost_anomaly.mitigation_not_active": ErrorSpec(
        409,
        "The temporary rate limit is not active.",
        "Refresh the anomaly because the mitigation may be released or expired.",
        owner="cost_anomaly",
    ),
    "cost_anomaly.unavailable": ErrorSpec(
        503,
        "Cost anomalies are temporarily unavailable.",
        "Retry after the control plane database and cost anomaly migration are available.",
        retryable=True,
        owner="cost_anomaly",
    ),
    "cost_anomaly.detector_unavailable": ErrorSpec(
        503,
        "Cost anomaly detection is temporarily unavailable.",
        "Check the formal FinOps ledger and detector scheduler health before retrying.",
        retryable=True,
        owner="cost_anomaly",
    ),
    "dlp.rule_invalid": ErrorSpec(
        422,
        "DLP rule payload is invalid.",
        "Check rule name, type, pattern, action, status, and priority.",
        owner="dlp",
    ),
    "dlp.rule_not_found": ErrorSpec(
        404,
        "DLP rule was not found.",
        "Check the rule id in the current tenant.",
        owner="dlp",
    ),
    "dlp.regex_invalid": ErrorSpec(
        422,
        "DLP regex is invalid.",
        "Fix the regular expression syntax before saving the rule.",
        owner="dlp",
    ),
    "dlp.regex_unsafe": ErrorSpec(
        422,
        "DLP regex is unsafe for the gateway hot path.",
        (
            "Avoid backreferences, lookarounds, nested quantifiers, repeated wildcards, "
            "and overly long patterns."
        ),
        owner="dlp",
    ),
    "dlp.update_empty": ErrorSpec(
        422,
        "DLP update has no editable fields.",
        "Provide at least one editable DLP rule field.",
        owner="dlp",
    ),
    "dlp.status_unavailable": ErrorSpec(
        503,
        "DLP rule status is unavailable.",
        "Check DLP rule tables and control plane database readiness.",
        retryable=True,
        owner="dlp",
    ),
    # 敏感意图错误只描述 wire、分类器或结果存储阶段，不包含正文、命中词和底层异常。
    "intent_risk.media_type_unsupported": ErrorSpec(
        415,
        "Intent classification requires application/json.",
        "Send a JSON object with exactly one text field.",
        owner="intent_risk",
    ),
    "intent_risk.payload_too_large": ErrorSpec(
        413,
        "Intent classification payload is too large.",
        "Reduce the request body below the documented byte limit.",
        owner="intent_risk",
    ),
    "intent_risk.payload_invalid": ErrorSpec(
        400,
        "Intent classification payload is invalid.",
        "Send strict JSON with exactly one text string.",
        owner="intent_risk",
    ),
    "intent_risk.text_invalid": ErrorSpec(
        422,
        "Intent classification text is invalid.",
        "Provide non-empty text within the documented character limit.",
        owner="intent_risk",
    ),
    "intent_risk.classifier_unavailable": ErrorSpec(
        503,
        "Intent classification is temporarily unavailable.",
        "Retry with a new operation after the local classifier recovers.",
        retryable=True,
        owner="intent_risk",
    ),
    "intent_risk.result_store_unavailable": ErrorSpec(
        503,
        "Intent classification result storage is temporarily unavailable.",
        "Check database readiness before submitting a new classification.",
        retryable=True,
        owner="intent_risk",
    ),
    "intent_risk.result_integrity_failed": ErrorSpec(
        503,
        "Intent classification result integrity validation failed.",
        "Use the request id to contact a platform administrator.",
        owner="intent_risk",
    ),
    # 会话风险控制面错误只描述筛选、状态、幂等和存储阶段，不披露 API session HMAC、
    # turn digest、正文、DLP pattern 或底层 Redis/数据库异常消息。
    "session_risk.query_invalid": ErrorSpec(
        400,
        "Session risk query is invalid.",
        "Check the documented filters, page size, and cursor before retrying.",
        owner="session_risk",
    ),
    "session_risk.not_found": ErrorSpec(
        404,
        "Session risk record was not found.",
        "Refresh the tenant-scoped report and select an existing record.",
        owner="session_risk",
    ),
    "session_risk.payload_invalid": ErrorSpec(
        400,
        "Session risk action payload is invalid.",
        "Send strict JSON with a supported action, reason, and expected revision.",
        owner="session_risk",
    ),
    "session_risk.idempotency_invalid": ErrorSpec(
        400,
        "Session risk idempotency key is invalid.",
        "Provide one bounded idempotency key for the management action.",
        owner="session_risk",
    ),
    "session_risk.idempotency_conflict": ErrorSpec(
        409,
        "Session risk idempotency key was already used for another action.",
        "Use a new idempotency key after confirming the intended action.",
        owner="session_risk",
    ),
    "session_risk.state_conflict": ErrorSpec(
        409,
        "Session risk state or revision has changed.",
        "Refresh the record and retry against its latest revision.",
        owner="session_risk",
    ),
    "session_risk.target_already_frozen": ErrorSpec(
        409,
        "The selected session risk target is already frozen.",
        "Refresh the record before submitting another management action.",
        owner="session_risk",
    ),
    "session_risk.store_unavailable": ErrorSpec(
        503,
        "Session risk state storage is temporarily unavailable.",
        "Restore the configured session risk store before retrying the action.",
        retryable=True,
        owner="session_risk",
    ),
    "session_risk.evidence_unavailable": ErrorSpec(
        503,
        "Session risk evidence could not be committed.",
        "Check database and audit readiness before retrying the action.",
        retryable=True,
        owner="session_risk",
    ),
    # 文件错误只描述固定处理阶段，不能拼接文件名、正文、对象键、规则或解析器异常。
    "file.multipart_invalid": ErrorSpec(
        400,
        "File upload multipart data is invalid.",
        "Send one multipart file field using the documented content type.",
        owner="file",
    ),
    "file.name_invalid": ErrorSpec(
        422,
        "File name is invalid.",
        "Use a bounded file name without paths, control characters, or unsafe suffixes.",
        owner="file",
    ),
    "file.unsupported_format": ErrorSpec(
        415,
        "File format is not supported.",
        "Upload a PDF, DOCX, or UTF-8 TXT file with matching media type and content.",
        owner="file",
    ),
    "file.too_large": ErrorSpec(
        413,
        "File upload is too large.",
        "Reduce the file below the configured upload size limit.",
        owner="file",
    ),
    "file.processing_busy": ErrorSpec(
        429,
        "File processing capacity is temporarily full.",
        "Retry after current file processing operations complete.",
        retryable=True,
        owner="file",
    ),
    "file.structure_invalid": ErrorSpec(
        422,
        "File structure is invalid or unsafe.",
        "Create a new supported document without embedded or unsafe structures.",
        owner="file",
    ),
    "file.no_extractable_text": ErrorSpec(
        422,
        "File contains no extractable text.",
        "Upload a supported document that contains selectable text.",
        owner="file",
    ),
    "file.extract_limit_exceeded": ErrorSpec(
        413,
        "Extracted file content exceeds a processing limit.",
        "Reduce document pages or extracted text size before retrying.",
        owner="file",
    ),
    "file.parse_failed": ErrorSpec(
        422,
        "File text extraction failed.",
        "Recreate the document in a supported format and upload it again.",
        owner="file",
    ),
    "file.dlp_blocked": ErrorSpec(
        403,
        "File was blocked by the tenant DLP policy.",
        "Contact a security administrator to review the applicable DLP policy.",
        owner="file",
    ),
    "file.dlp_unavailable": ErrorSpec(
        503,
        "File DLP scanning is temporarily unavailable.",
        "Retry after the tenant DLP rules and scanner are available.",
        retryable=True,
        owner="file",
    ),
    "file.storage_unavailable": ErrorSpec(
        503,
        "File storage is temporarily unavailable.",
        "Retry after database, encryption, and object storage readiness is restored.",
        retryable=True,
        owner="file",
    ),
    "file.integrity_failed": ErrorSpec(
        500,
        "File processing integrity validation failed.",
        "Use the request id to contact a platform administrator.",
        owner="file",
    ),
    "file.not_found": ErrorSpec(
        404,
        "File was not found.",
        "Refresh the current tenant file list and select an existing file.",
        owner="file",
    ),
    "file.security_report_not_found": ErrorSpec(
        404,
        "File security report was not found.",
        "Run a security scan for this file before requesting its report.",
        owner="file_security",
    ),
    "file.security_not_ready": ErrorSpec(
        409,
        "File is not ready for security scanning.",
        "Wait for a ready file that was not blocked by DLP, then retry.",
        owner="file_security",
    ),
    "file.security_scan_busy": ErrorSpec(
        429,
        "File security scanning capacity is temporarily full.",
        "Retry after current file security scans complete.",
        retryable=True,
        owner="file_security",
    ),
    "file.security_scan_unavailable": ErrorSpec(
        503,
        "File security scanning is temporarily unavailable.",
        "Check detector, OCR, database, encryption, and object storage readiness before retrying.",
        retryable=True,
        owner="file_security",
    ),
    "file.model_access_blocked": ErrorSpec(
        403,
        "File is blocked from model context.",
        "Review the current security report and use only a passed file with complete coverage.",
        owner="file_security",
    ),
    "policy.payload_invalid": ErrorSpec(
        422,
        "Policy payload is invalid.",
        "Check policy name, description, status, priority, and nested draft objects.",
        owner="policy",
    ),
    "policy.expression_invalid": ErrorSpec(
        422,
        "Policy expression is invalid.",
        (
            "Use only the supported policy variables, comparisons, boolean operators, "
            "and bounded literals."
        ),
        owner="policy",
    ),
    "policy.action_invalid": ErrorSpec(
        422,
        "Policy action is invalid.",
        "Choose allow, deny, mask, log_only, or route_to with the required target.",
        owner="policy",
    ),
    "policy.route_target_invalid": ErrorSpec(
        422,
        "Policy route target is invalid.",
        "Choose an active model that belongs to the current tenant.",
        owner="policy",
    ),
    "policy.context_invalid": ErrorSpec(
        422,
        "Policy evaluation context is invalid.",
        "Provide bounded string values and a non-negative decimal budget_remaining string.",
        owner="policy",
    ),
    "policy.query_invalid": ErrorSpec(
        422,
        "Policy query is invalid.",
        "Use only supported status and action filters.",
        owner="policy",
    ),
    "policy.duplicate": ErrorSpec(
        409,
        "Policy name already exists.",
        "Choose a unique policy name within the current tenant.",
        owner="policy",
    ),
    "policy.not_found": ErrorSpec(
        404,
        "Policy was not found.",
        "Refresh the policy list and use an id from the current tenant.",
        owner="policy",
    ),
    "policy.version_conflict": ErrorSpec(
        409,
        "Policy version changed before this update.",
        "Refresh the policy and retry the edit against its latest version.",
        owner="policy",
    ),
    "policy.update_empty": ErrorSpec(
        422,
        "Policy update has no editable fields.",
        "Provide at least one policy field to update.",
        owner="policy",
    ),
    "policy.quota_exceeded": ErrorSpec(
        422,
        "Policy limit was exceeded.",
        "Disable or consolidate existing policies before creating another one.",
        owner="policy",
    ),
    "policy.unavailable": ErrorSpec(
        503,
        "Policy service is unavailable.",
        "Check policy migrations, database integrity, audit storage, and retry.",
        retryable=True,
        owner="policy",
    ),
    # Dry-Run HTTP 错误与模拟出的 Gateway would_error_code 分属两层合同；这里仅登记
    # 请求读取、上下文、幂等、CPU 防护和安全事实事务失败，不能把策略 deny 当成 HTTP 错误。
    "policy.dry_run_payload_invalid": ErrorSpec(
        422,
        "Policy dry-run payload is invalid.",
        "Check the bounded dry-run fields and submit a valid JSON object.",
        owner="policy_dry_run",
    ),
    "policy.dry_run_payload_too_large": ErrorSpec(
        413,
        "Policy dry-run payload is too large.",
        "Reduce the JSON request body to no more than 256 KiB.",
        owner="policy_dry_run",
    ),
    "policy.dry_run_context_not_found": ErrorSpec(
        404,
        "Policy dry-run context was not found.",
        "Choose an active environment, project, and model visible to the current tenant.",
        owner="policy_dry_run",
    ),
    "policy.dry_run_request_id_reused": ErrorSpec(
        409,
        "Policy dry-run request id was already completed.",
        "Submit a new dry-run request with a new request_id.",
        owner="policy_dry_run",
    ),
    "policy.dry_run_rate_limited": ErrorSpec(
        429,
        "Policy dry-run rate limit was exceeded.",
        "Wait for the local dry-run CPU protection window to reset before retrying.",
        retryable=True,
        owner="policy_dry_run",
    ),
    "policy.dry_run_unavailable": ErrorSpec(
        503,
        "Policy dry-run service is unavailable.",
        (
            "Check policy graph integrity, database readiness, audit, and event storage "
            "before retrying."
        ),
        retryable=True,
        owner="policy_dry_run",
    ),
    "policy_snapshot.payload_invalid": ErrorSpec(
        422,
        "Policy snapshot request is invalid.",
        "Check the bounded request fields and retry with a new valid payload.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.if_match_required": ErrorSpec(
        428,
        "A strong policy snapshot version precondition is required.",
        "Refresh the current snapshot and submit its strong ETag with the request.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.version_conflict": ErrorSpec(
        409,
        "The current policy snapshot version changed.",
        "Refresh snapshot history and retry against the latest head.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.candidate_conflict": ErrorSpec(
        409,
        "The active policy candidate changed before publication.",
        "Refresh the candidate summary, review the changes, and publish again.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.idempotency_conflict": ErrorSpec(
        409,
        "The policy snapshot request id was already used for another operation.",
        "Use a new request_id for a different publish or rollback operation.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.not_found": ErrorSpec(
        404,
        "Policy snapshot was not found.",
        "Refresh history and choose a snapshot visible to the current tenant.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.tenant_unavailable": ErrorSpec(
        404,
        "Policy snapshot tenant is unavailable.",
        "Check that the tenant exists and is active before synchronizing policies.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.rollback_target_invalid": ErrorSpec(
        409,
        "Policy snapshot rollback target is invalid.",
        "Refresh history and roll back only the previous active snapshot.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.no_changes": ErrorSpec(
        409,
        "Active policies have no execution changes to publish.",
        "Change an active policy execution field before publishing a new snapshot.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.template_invariant_conflict": ErrorSpec(
        409,
        "The active policy candidate conflicts with an applied policy template.",
        "Disable the conflicting policy or roll back the affected template application.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.action_unsupported": ErrorSpec(
        422,
        "An active policy action cannot be published safely.",
        "Remove or disable the unknown action, then publish a supported policy snapshot.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.variable_unsupported": ErrorSpec(
        422,
        "An active policy uses an untrusted runtime variable.",
        "Remove data_class from active policies until a trusted source is configured.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.route_target_invalid": ErrorSpec(
        422,
        "A policy snapshot route target is invalid.",
        "Choose an active model owned by the current tenant.",
        owner="policy_snapshot",
    ),
    "policy_snapshot.graph_invalid": ErrorSpec(
        503,
        "Policy snapshot source graph is invalid.",
        "Check policy rules, actions, compiled facts, and tenant relationships.",
        retryable=True,
        owner="policy_snapshot",
    ),
    "policy_snapshot.unavailable": ErrorSpec(
        503,
        "Policy snapshot service is unavailable.",
        "Check snapshot migrations, database integrity, audit, and event storage.",
        retryable=True,
        owner="policy_snapshot",
    ),
    # 策略模板错误只描述稳定资源状态和修复动作；禁止拼接模板 DSL、绑定值、价格或底层 SQL。
    "policy_template.not_found": ErrorSpec(
        404,
        "Policy template was not found.",
        "Refresh the policy template catalog for the current tenant.",
        owner="policy_template",
    ),
    "policy_template.media_type_unsupported": ErrorSpec(
        415,
        "Policy template media type is unsupported.",
        "Send the apply request as application/json.",
        owner="policy_template",
    ),
    "policy_template.precondition_required": ErrorSpec(
        428,
        "Policy template preconditions are required.",
        "Refresh the preview and send both strong snapshot precondition headers.",
        owner="policy_template",
    ),
    "policy_template.payload_too_large": ErrorSpec(
        413,
        "Policy template payload is too large.",
        "Reduce the apply payload to the documented bounded size.",
        owner="policy_template",
    ),
    "policy_template.query_invalid": ErrorSpec(
        422,
        "Policy template query is invalid.",
        "Use only the documented query fields and binding combination.",
        owner="policy_template",
    ),
    "policy_template.payload_invalid": ErrorSpec(
        422,
        "Policy template payload is invalid.",
        "Correct the version, preview fields, bindings, reason, and identifiers.",
        owner="policy_template",
    ),
    "policy_template.binding_invalid": ErrorSpec(
        422,
        "Policy template binding is invalid.",
        "Choose active resources owned by the current tenant.",
        owner="policy_template",
    ),
    "policy_template.not_applicable": ErrorSpec(
        409,
        "Policy template is not applicable.",
        "Resolve the reported compatibility prerequisites and refresh the preview.",
        owner="policy_template",
    ),
    "policy_template.version_conflict": ErrorSpec(
        409,
        "Policy template version changed.",
        "Refresh the catalog and review the current template version.",
        owner="policy_template",
    ),
    "policy_template.impact_conflict": ErrorSpec(
        409,
        "Policy template impact changed.",
        "Refresh the preview and review the current policy and dependency impact.",
        owner="policy_template",
    ),
    "policy_template.request_conflict": ErrorSpec(
        409,
        "Policy template request identifier was already used.",
        "Reuse the original request unchanged or send a new request identifier.",
        owner="policy_template",
    ),
    "policy_template.nonce_conflict": ErrorSpec(
        409,
        "Policy template preview nonce was already consumed.",
        "Refresh the preview before applying the template again.",
        owner="policy_template",
    ),
    "policy_template.application_conflict": ErrorSpec(
        409,
        "A policy template is already active for the project.",
        "Roll back the active application before applying another template.",
        owner="policy_template",
    ),
    "policy_template.rollback_conflict": ErrorSpec(
        409,
        "Policy template application cannot be rolled back safely.",
        "Resolve policy graph or candidate drift and retry from current snapshot history.",
        owner="policy_template",
    ),
    "policy_template.unavailable": ErrorSpec(
        503,
        "Policy template service is unavailable.",
        "Check migrations, catalog integrity, audit, event, and snapshot storage.",
        retryable=True,
        owner="policy_template",
    ),
    "webhook.endpoint_invalid": ErrorSpec(
        422,
        "Webhook endpoint payload is invalid.",
        "Check name, endpoint URL, event types, status, and timeout.",
        owner="webhook",
    ),
    "webhook.endpoint_not_found": ErrorSpec(
        404,
        "Webhook endpoint was not found.",
        "Check the endpoint id in the current tenant.",
        owner="webhook",
    ),
    "webhook.endpoint_disabled": ErrorSpec(
        409,
        "Webhook endpoint is disabled.",
        "Enable the endpoint before testing or replaying a delivery.",
        owner="webhook",
    ),
    "webhook.delivery_not_found": ErrorSpec(
        404,
        "Webhook delivery was not found.",
        "Check the endpoint and delivery ids in the current tenant.",
        owner="webhook",
    ),
    "webhook.delivery_not_replayable": ErrorSpec(
        409,
        "Webhook delivery cannot be replayed.",
        "Only failed webhook deliveries can be replayed.",
        owner="webhook",
    ),
    "webhook.dead_letter_not_found": ErrorSpec(
        404,
        "Webhook dead letter was not found.",
        "Check the delivery or dead-letter id in the current tenant.",
        owner="webhook",
    ),
    "webhook.dead_letter_not_replayable": ErrorSpec(
        409,
        "Webhook dead letter cannot be replayed.",
        "Only an active dead-letter record can be replayed.",
        owner="webhook",
    ),
    "webhook.dead_letter_replay_pending": ErrorSpec(
        409,
        "Webhook dead-letter replay is already pending.",
        "Wait for the pending replay or retry with its original idempotency key.",
        owner="webhook",
    ),
    "webhook.dead_letter_not_archivable": ErrorSpec(
        409,
        "Webhook dead letter cannot be archived.",
        "Only dead-letter or replayed records can be archived.",
        owner="webhook",
    ),
    "webhook.dead_letter_filter_invalid": ErrorSpec(
        422,
        "Webhook dead-letter filter is invalid.",
        "Use a supported status, event type, endpoint, cursor, and limit.",
        owner="webhook",
    ),
    "webhook.dead_letter_unavailable": ErrorSpec(
        503,
        "Webhook dead-letter storage is unavailable.",
        "Check database migrations and retry with the same request identifiers.",
        retryable=True,
        owner="webhook",
    ),
    "webhook.idempotency_key_invalid": ErrorSpec(
        422,
        "Webhook replay idempotency key is invalid.",
        "Use 8 to 128 ASCII letters, digits, dots, underscores, colons, or hyphens.",
        owner="webhook",
    ),
    "webhook.delivery_filter_invalid": ErrorSpec(
        422,
        "Webhook delivery filter is invalid.",
        "Use a supported event type, status, and a limit between 1 and 100.",
        owner="webhook",
    ),
    "webhook.replay_unavailable": ErrorSpec(
        503,
        "Webhook replay is unavailable.",
        "Check webhook delivery storage and retry with the same idempotency key.",
        retryable=True,
        owner="webhook",
    ),
    "webhook.event_type_invalid": ErrorSpec(
        422,
        "Webhook event type is invalid.",
        "Choose one of the supported MetaHub event types.",
        owner="webhook",
    ),
    "webhook.secret_unavailable": ErrorSpec(
        503,
        "Webhook signing secret is unavailable.",
        "Check encryption secret configuration and retry.",
        retryable=True,
        owner="webhook",
    ),
    "webhook.limit_exceeded": ErrorSpec(
        422,
        "Webhook endpoint limit was exceeded.",
        "Disable unused endpoints or reuse an existing endpoint.",
        owner="webhook",
    ),
    "webhook.delivery_failed": ErrorSpec(
        502,
        "Webhook delivery failed.",
        "Use delivery attempts to inspect HTTP status, timeout, and signature receiver behavior.",
        retryable=True,
        owner="webhook",
    ),
    "webhook.delivery_unavailable": ErrorSpec(
        503,
        "Webhook delivery is unavailable.",
        "Check webhook tables, network access, and receiver availability.",
        retryable=True,
        owner="webhook",
    ),
    "console.navigation_invalid": ErrorSpec(
        422,
        "Console navigation item is invalid.",
        "Choose a navigation item that is available to the current role.",
        owner="console",
    ),
    "console.preference_invalid": ErrorSpec(
        422,
        "Console preference payload is invalid.",
        "Check active_navigation_item_id and sidebar_collapsed.",
        owner="console",
    ),
    "console.event_invalid": ErrorSpec(
        422,
        "Console event payload is invalid.",
        "Match event, navigation, path, hash, source, and viewport without sensitive fields.",
        owner="console",
    ),
    "console.event_unavailable": ErrorSpec(
        503,
        "Console event storage is unavailable.",
        "Retry later and use request_id to inspect the control-plane database health.",
        retryable=True,
        owner="console",
    ),
    "dashboard.unavailable": ErrorSpec(
        503,
        "Dashboard overview is unavailable.",
        "Check database migrations, request_logs, and usage_ledger readiness.",
        retryable=True,
        owner="dashboard",
    ),
    "dashboard.query_invalid": ErrorSpec(
        422,
        "Dashboard query is invalid.",
        (
            "Remove unsupported parameters; role-view accepts at most one supported "
            "lowercase view code."
        ),
        owner="dashboard",
    ),
    "dashboard.config_invalid": ErrorSpec(
        500,
        "Dashboard role configuration is invalid.",
        "Use request_id to inspect the tenant role dashboard configuration and migration state.",
        owner="dashboard",
    ),
    "user_preference.locale_invalid": ErrorSpec(
        422,
        "User preference locale is invalid.",
        "Use zh-CN or en-US.",
        owner="user_preference",
    ),
    "user_preference.payload_invalid": ErrorSpec(
        422,
        "User preference payload is invalid.",
        "Send only supported user preference fields.",
        owner="user_preference",
    ),
    "user_preference.unavailable": ErrorSpec(
        503,
        "User preferences are unavailable.",
        "Check database connectivity and the i18n user preference migration.",
        retryable=True,
        owner="user_preference",
    ),
    "i18n.locale_unsupported": ErrorSpec(
        422,
        "Locale is not supported.",
        "Use zh-CN or en-US.",
        owner="i18n",
    ),
    "developer.quickstart_unavailable": ErrorSpec(
        503,
        "Developer quickstart data is unavailable.",
        "Check model aliases, virtual keys, and control plane database readiness.",
        retryable=True,
        owner="developer_portal",
    ),
    "developer.quickstart_event_invalid": ErrorSpec(
        422,
        "Developer quickstart event payload is invalid.",
        "Send only whitelisted quickstart event metadata without secrets.",
        owner="developer_portal",
    ),
    # SDK 示例错误同时服务 Python 与 JavaScript 制品，公开文案不能暴露具体语言或源码路径。
    "developer.sdk_example_unavailable": ErrorSpec(
        503,
        "SDK example artifact metadata is unavailable.",
        "Check the controlled example source files and model catalog readiness before retrying.",
        retryable=True,
        owner="developer_portal",
    ),
    "developer.sdk_example_archive_failed": ErrorSpec(
        500,
        "SDK example artifact archive could not be created.",
        "Check the controlled example source files and retry the download.",
        retryable=True,
        owner="developer_portal",
    ),
    "developer.sdk_example_event_invalid": ErrorSpec(
        422,
        "SDK example artifact download event payload is invalid.",
        "Record only whitelisted download metadata without secrets or source content.",
        owner="developer_portal",
    ),
    "debug.request_invalid": ErrorSpec(
        422,
        "Debug request payload is invalid.",
        "Choose an active virtual key, a model alias, and one to twenty valid chat messages.",
        owner="debug_console",
    ),
    "debug.virtual_key_invalid": ErrorSpec(
        404,
        "Debug virtual key is unavailable.",
        "Choose an active virtual key that belongs to the current tenant.",
        owner="debug_console",
    ),
    "debug.environment_mismatch": ErrorSpec(
        409,
        "Debug environment does not match the virtual key.",
        "Choose a virtual key from the selected environment.",
        owner="debug_console",
    ),
    "debug.event_invalid": ErrorSpec(
        422,
        "Debug event payload is invalid.",
        "Send only whitelisted debug metadata without prompts, responses, or secrets.",
        owner="debug_console",
    ),
    "debug.upstream_failed": ErrorSpec(
        502,
        "Debug upstream request failed.",
        "Use request_id to inspect the routed provider and upstream error summary.",
        retryable=True,
        owner="debug_console",
    ),
    "gateway.auth_missing": ErrorSpec(
        401,
        "Gateway virtual key is required.",
        "Pass Authorization: Bearer <virtual_key>.",
        owner="gateway",
    ),
    "gateway.auth_invalid": ErrorSpec(
        401,
        "Gateway virtual key is invalid.",
        "Check that the virtual key exists and is active.",
        owner="gateway",
    ),
    "gateway.environment_disabled": ErrorSpec(
        403,
        "Virtual key environment is disabled.",
        "Enable the environment or use a key from an active environment.",
        owner="gateway",
    ),
    "gateway.request_invalid": ErrorSpec(
        422,
        "Gateway request payload is invalid.",
        "Check the OpenAI-compatible request JSON.",
        owner="gateway",
    ),
    "gateway.request_id_reused": ErrorSpec(
        409,
        "Gateway request id was already claimed.",
        "Use a new X-Request-ID for every distinct or retried model request.",
        owner="gateway",
    ),
    "gateway.request_log_unavailable": ErrorSpec(
        503,
        "Gateway request tracking is unavailable.",
        "Restore the request log database before retrying protected traffic.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.policy_snapshot_unavailable": ErrorSpec(
        503,
        "No trusted policy snapshot is available.",
        "Restore Control API synchronization before retrying traffic.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.policy_snapshot_stale": ErrorSpec(
        503,
        "The last known policy snapshot is too stale for public traffic.",
        "Restore Control API synchronization and wait for a successful tenant refresh.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.policy_snapshot_expired": ErrorSpec(
        503,
        "The emergency policy snapshot lifetime expired.",
        "Restore Control API synchronization before accepting new model requests.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.policy_denied": ErrorSpec(
        403,
        "Request was denied by the published policy snapshot.",
        "Review the safe policy decision summary or contact the tenant administrator.",
        owner="gateway",
    ),
    "gateway.policy_evaluation_timeout": ErrorSpec(
        403,
        "Policy evaluation exceeded its safety deadline.",
        "Simplify the policy set or contact the tenant administrator before retrying.",
        owner="gateway",
    ),
    "gateway.policy_context_missing": ErrorSpec(
        403,
        "A required trusted policy context value is unavailable.",
        "Configure the trusted policy context or disable the dependent policy.",
        owner="gateway",
    ),
    "gateway.policy_context_unavailable": ErrorSpec(
        503,
        "Trusted policy context could not be calculated.",
        "Restore budget and usage storage before retrying protected traffic.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.policy_route_unavailable": ErrorSpec(
        503,
        "The policy-selected model route is unavailable.",
        "Check the target model, provider health, and required endpoint capability.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.policy_action_unsupported": ErrorSpec(
        403,
        "The published policy action is not executable safely.",
        "Publish a snapshot containing only supported policy actions.",
        owner="gateway",
    ),
    "gateway.policy_mask_unsupported": ErrorSpec(
        422,
        "The request body cannot be redacted safely.",
        "Use supported Chat user text or text-only Embeddings input within the documented limits.",
        owner="gateway",
    ),
    "gateway.policy_mask_unavailable": ErrorSpec(
        503,
        "The policy redaction runtime is unavailable.",
        "Retry after the Gateway redaction capability is restored.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.model_missing": ErrorSpec(
        422,
        "Model is required.",
        "Pass a model alias in the request body.",
        owner="gateway",
    ),
    "gateway.messages_missing": ErrorSpec(
        422,
        "Messages are required.",
        "Pass a non-empty messages array.",
        owner="gateway",
    ),
    "gateway.input_missing": ErrorSpec(
        422,
        "Input is required.",
        "Pass an input string or array.",
        owner="gateway",
    ),
    "gateway.model_not_found": ErrorSpec(
        404,
        "Model alias was not found.",
        "Check the model alias in the current tenant.",
        owner="gateway",
    ),
    "gateway.model_alias_disabled": ErrorSpec(
        409,
        "Model alias is disabled.",
        "Enable the model alias or choose another model alias.",
        owner="gateway",
    ),
    "gateway.model_disabled": ErrorSpec(
        409,
        "No active model is available for this alias.",
        "Enable the default model, configure a fallback model, or choose another alias.",
        owner="gateway",
    ),
    "gateway.provider_not_found": ErrorSpec(
        404,
        "Provider was not found.",
        "Check provider configuration for the selected model.",
        owner="gateway",
    ),
    "gateway.provider_unhealthy": ErrorSpec(
        503,
        "Provider is temporarily unhealthy.",
        "Choose another model route or retry after provider health recovers.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.no_route_available": ErrorSpec(
        503,
        "No eligible route is currently available.",
        "Check model, Provider, health, and Provider KeyPool status before retrying.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.routing_policy_invalid": ErrorSpec(
        503,
        "Routing policy configuration is invalid.",
        "Repair the model alias routing policy before retrying the request.",
        owner="gateway",
    ),
    "gateway.routing_snapshot_unavailable": ErrorSpec(
        503,
        "Routing snapshot is temporarily unavailable.",
        "Retry after the routing database or required snapshot dependency recovers.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.routing_snapshot_invalid": ErrorSpec(
        503,
        "Routing snapshot contains invalid facts.",
        "Repair the model, Provider, health, KeyPool, or price relationship before retrying.",
        owner="gateway",
    ),
    "gateway.routing_decision_unavailable": ErrorSpec(
        503,
        "Routing decision could not be recorded.",
        "Retry after the routing decision store recovers.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.experiment_unavailable": ErrorSpec(
        503,
        "Experiment routing facts are temporarily unavailable.",
        "Retry after the experiment database and routing owner graph recover.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.experiment_variant_unavailable": ErrorSpec(
        503,
        "The assigned experiment variant is temporarily unavailable.",
        "Repair the assigned model and Provider configuration before retrying new traffic.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.experiment_execution_timeout": ErrorSpec(
        504,
        "Experiment execution exceeded its absolute deadline.",
        "Use the request id to inspect the terminal experiment outcome before retrying.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.cache_affinity_unavailable": ErrorSpec(
        503,
        "Cache affinity routing facts are temporarily unavailable.",
        "Retry after the routing database, binding, or selection store recovers.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.execution_interrupted": ErrorSpec(
        503,
        "The previous Gateway execution was interrupted.",
        "Start a new request after Gateway recovery completes.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.stream_not_supported": ErrorSpec(
        422,
        "Streaming is not supported for this request.",
        "Disable stream or choose a model that supports streaming.",
        owner="gateway",
    ),
    "gateway.stream_session_unavailable": ErrorSpec(
        503,
        "Stream session could not be recorded.",
        "Retry after the request log database and stream session migration are available.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.client_disconnected": ErrorSpec(
        499,
        "Client disconnected before the stream completed.",
        "Start a new request if the stream result is still required.",
        owner="gateway",
    ),
    "gateway.rate_limit_exceeded": ErrorSpec(
        429,
        "Rate limit exceeded.",
        "Reduce request rate or increase the Virtual Key limits.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.rate_limit_unavailable": ErrorSpec(
        503,
        "Rate limit service is unavailable.",
        "Check Redis connectivity before retrying protected traffic.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.budget_exhausted": ErrorSpec(
        402,
        "Daily budget has been exhausted.",
        "Review budget settings or increase the daily budget before retrying.",
        owner="gateway",
    ),
    "gateway.budget_unavailable": ErrorSpec(
        503,
        "Budget guard is unavailable.",
        "Check budget rules and usage ledger connectivity before retrying protected traffic.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.dlp_blocked": ErrorSpec(
        403,
        "Request was blocked by DLP policy.",
        (
            "Review the DLP rule that matched this request or change the rule to log_only "
            "for trial runs."
        ),
        owner="gateway",
    ),
    "gateway.dlp_unavailable": ErrorSpec(
        503,
        "DLP guard is unavailable.",
        "Check DLP rules and database connectivity before retrying protected traffic.",
        retryable=True,
        owner="gateway",
    ),
    # Excel 对输出阻断码要求精确原始值，不能为了命名空间一致性改写成 gateway.* 别名。
    "content_policy_violation": ErrorSpec(
        403,
        "Model output was blocked by content policy.",
        "Review the output safety event with the returned request id.",
        owner="output_safety",
    ),
    "gateway.output_safety_unavailable": ErrorSpec(
        503,
        "Output safety scanning is unavailable.",
        "Retry after output safety rules and evidence storage are available.",
        retryable=True,
        owner="output_safety",
    ),
    # 同步敏感意图闸门必须区分业务阻断、分类器故障和证据存储故障；三者都不能
    # 拼接输入、命中短语或底层异常，否则统一错误响应会成为敏感正文外泄通道。
    "gateway.intent_blocked": ErrorSpec(
        403,
        "Request was blocked by the sensitive intent guard.",
        "Review the fixed intent risk metadata before changing and retrying the request.",
        owner="gateway",
    ),
    "gateway.intent_classifier_unavailable": ErrorSpec(
        503,
        "Sensitive intent classification is unavailable.",
        "Restore the local intent classifier before retrying protected traffic.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.intent_result_store_unavailable": ErrorSpec(
        503,
        "Sensitive intent result storage is unavailable.",
        "Check the security result database and migration state before retrying.",
        retryable=True,
        owner="gateway",
    ),
    # 会话风险数据面错误在 Provider 前失败，且 message/suggestion 不能出现 Header、digest、
    # Redis URL、密钥版本候选或正文，统一链路层会自动附加 request_id/trace_id。
    "gateway.session_risk_identifier_invalid": ErrorSpec(
        400,
        "Session risk identifiers are invalid.",
        "Provide both canonical session and turn identifiers, or omit both headers.",
        owner="gateway",
    ),
    "gateway.session_risk_turn_too_large": ErrorSpec(
        413,
        "The current session turn is too large for risk processing.",
        "Reduce the current user turn below the documented byte limit.",
        owner="gateway",
    ),
    "gateway.session_risk_turn_conflict": ErrorSpec(
        409,
        "The session turn identifier conflicts with an earlier turn.",
        "Use a new turn identifier for changed user content.",
        owner="gateway",
    ),
    "gateway.session_risk_frozen": ErrorSpec(
        403,
        "The session or associated key is frozen by the session risk guard.",
        "Ask an authorized security administrator to review the session risk record.",
        owner="gateway",
    ),
    "gateway.session_risk_unavailable": ErrorSpec(
        503,
        "Session risk processing is temporarily unavailable.",
        "Restore the configured risk store before retrying protected traffic.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.session_risk_hmac_version_unavailable": ErrorSpec(
        503,
        "A required session risk key version is unavailable.",
        "Restore the referenced key version before retrying protected traffic.",
        owner="gateway",
    ),
    "gateway.session_risk_evidence_unavailable": ErrorSpec(
        503,
        "Session risk evidence could not be committed.",
        "Restore database evidence storage before retrying protected traffic.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.upstream_unavailable": ErrorSpec(
        503,
        "Upstream provider is unavailable.",
        "Check provider endpoint connectivity and timeout settings.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.upstream_auth_failed": ErrorSpec(
        502,
        "Upstream provider authentication failed.",
        (
            "Check Provider Key status, account permissions, and model access on the "
            "upstream provider."
        ),
        owner="gateway",
    ),
    "gateway.upstream_rate_limited": ErrorSpec(
        429,
        "Upstream provider is rate limited.",
        "Retry later or enable another available provider key.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.upstream_quota_exceeded": ErrorSpec(
        402,
        "Upstream provider quota is exhausted.",
        "Check upstream balance, billing status, and project quota.",
        owner="gateway",
    ),
    "gateway.upstream_model_not_found": ErrorSpec(
        404,
        "Upstream provider model was not found.",
        "Check provider_model_id/upstream_model_name and upstream model permissions.",
        owner="gateway",
    ),
    "gateway.upstream_resource_not_found": ErrorSpec(
        502,
        "Upstream provider resource was not found.",
        "Check the upstream endpoint and requested resource by request_id.",
        owner="gateway",
    ),
    "gateway.upstream_timeout": ErrorSpec(
        504,
        "Upstream provider request timed out.",
        "Retry later or check provider endpoint latency and timeout settings.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.upstream_stream_timeout": ErrorSpec(
        504,
        "Upstream stream timed out.",
        "Retry later or check provider streaming latency and the inter-token timeout setting.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.upstream_stream_failed": ErrorSpec(
        502,
        "Upstream stream failed before completion.",
        "Retry later or inspect the upstream Provider connection by request_id.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.upstream_transport_failed": ErrorSpec(
        502,
        "Upstream transport failed before a valid response was received.",
        "Retry later or inspect Provider network connectivity by request_id.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.stream_processing_unavailable": ErrorSpec(
        503,
        "Gateway stream processing is temporarily unavailable.",
        "Retry after the Gateway stream processor recovers.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.upstream_request_invalid": ErrorSpec(
        502,
        "Upstream provider rejected the request payload.",
        "Check model capability and provider-specific parameter requirements.",
        owner="gateway",
    ),
    "gateway.upstream_error": ErrorSpec(
        502,
        "Upstream provider returned an error.",
        "Use request_id to inspect the upstream error summary.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.routing_attempt_unavailable": ErrorSpec(
        503,
        "Routing attempt could not be recorded.",
        "Retry after the request log database is available.",
        retryable=True,
        owner="gateway",
    ),
    "gateway.fallback_unavailable": ErrorSpec(
        503,
        "Fallback attempt could not be recorded.",
        "Retry after the request log database is available.",
        retryable=True,
        owner="gateway",
    ),
    "db.config_missing": ErrorSpec(
        500,
        "Database configuration is missing.",
        "Set METAHUB_DATABASE_URL before running database commands.",
        owner="db",
    ),
    "db.connection_failed": ErrorSpec(
        500,
        "Database connection failed.",
        "Check database URL, credentials, and network connectivity.",
        retryable=True,
        owner="db",
    ),
    "db.migration_failed": ErrorSpec(
        500,
        "Database migration failed.",
        "Inspect migration logs and fix the failing migration.",
        owner="db",
    ),
    "db.migration_checksum_changed": ErrorSpec(
        500,
        "Applied migration checksum changed.",
        "Do not edit applied migrations; create a new migration instead.",
        owner="db",
    ),
    "db.migration_locked": ErrorSpec(
        409,
        "Another migration is already running.",
        "Wait for the current migration to finish or clear the stale lock.",
        retryable=True,
        owner="db",
    ),
    "db.seed_failed": ErrorSpec(
        500,
        "Seed data failed.",
        "Check seed values and database constraints.",
        owner="db",
    ),
    "db.constraint_violation": ErrorSpec(
        500,
        "Database constraint violation.",
        "Fix seed or migration data to satisfy constraints.",
        owner="db",
    ),
    "health.dependency_failed": ErrorSpec(
        503,
        "Required health dependency failed.",
        "Check the failed dependency in health checks.",
        retryable=True,
        owner="health",
    ),
    "health.check_timeout": ErrorSpec(
        503,
        "Health check timed out.",
        "Check dependency latency and probe timeout settings.",
        retryable=True,
        owner="health",
    ),
    "health.invalid_dependency_config": ErrorSpec(
        500,
        "Health dependency configuration is invalid.",
        "Fix dependency configuration and restart the service.",
        owner="health",
    ),
}


class ApiError(Exception):
    """业务可预期的 API 错误。

    抛出 ApiError 的代码路径会被 FastAPI exception handler 转换为统一错误结构。
    未捕获异常仍会归一为 infra.unhandled，避免把 Python 堆栈泄漏到客户端。
    """

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        suggestion: str | None = None,
    ) -> None:
        """在 共享运行时 内部复用 init 辅助逻辑，集中处理边界值和错误映射。

        边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
        """

        spec = ERROR_SPECS.get(code, ERROR_SPECS["infra.unhandled"])
        self.code = code
        self.http_status = spec.http_status
        self.message = message or spec.message
        self.suggestion = suggestion or spec.suggestion
        super().__init__(self.message)


class ErrorCodeInfo(BaseModel):
    """承载 共享运行时 的 ErrorCodeInfo 数据结构，明确跨模块传递时允许出现的字段。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    code: str
    http_status: int
    retryable: bool
    message: str
    suggestion: str
    label: str
    owner: str
    diagnosis: ErrorDiagnosisInfo


def build_error_response(error: ApiError, request_id: str, trace_id: str) -> ErrorResponse:
    """把内部错误对象转换成对外响应体。"""

    diagnosis = diagnose_error(error.code, http_status=error.http_status, message=error.message)
    return ErrorResponse(
        code=error.code,
        message=error.message,
        request_id=request_id,
        trace_id=trace_id,
        suggestion=error.suggestion,
        diagnosis=diagnosis,
    )


def list_error_codes(locale: str = DEFAULT_LOCALE) -> list[ErrorCodeInfo]:
    """返回错误码注册表。

    PRD-027 起附带本地化展示 label，但 code/message/suggestion 仍保持英文稳定契约。
    """

    return [
        ErrorCodeInfo(
            code=code,
            http_status=spec.http_status,
            retryable=spec.retryable,
            message=spec.message,
            suggestion=spec.suggestion,
            label=error_label(code, locale),
            owner=spec.owner,
            diagnosis=diagnose_error(code, http_status=spec.http_status, message=spec.message),
        )
        for code, spec in sorted(ERROR_SPECS.items())
    ]
