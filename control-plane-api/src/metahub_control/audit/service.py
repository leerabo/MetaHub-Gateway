"""管理操作审计的同事务写入、查询和事件投递服务。

业务服务必须把 `record_audit_log` 放在自身 `commit()` 之前调用，确保管理变更和审计事实
共同成功或共同回滚。Webhook 属于外部副作用，只能在 commit 成功后尽力投递。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Request
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.rbac import ROLE_CODES
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.policy_dsl import ALLOWED_VARIABLES
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.auth.service import AuthContext

from .filters import audit_where_clause, normalize_audit_filters
from .models import AuditLogInfo, AuditLogListResponse

logger = logging.getLogger("metahub.control.audit")

# 分页、筛选和摘要上限共同保护审计查询与存储，避免管理员输入或内部调用形成无界资源消耗。
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_OFFSET = 1_000_000
MAX_FILTER_LENGTH = 160
MAX_OBJECT_NAME_BYTES = 512
MAX_USER_AGENT_BYTES = 1024
MAX_SUMMARY_BYTES = 16_384

# action 与 object_type 必须成对稳定，防止内部调用写入任意动作码后破坏筛选和 Webhook 合同。
ACTION_OBJECT_TYPES = {
    "provider.created": "provider",
    "provider.updated": "provider",
    "provider_key.created": "provider_key",
    "provider_key.updated": "provider_key",
    "virtual_key.created": "virtual_key",
    "virtual_key.updated": "virtual_key",
    "budget_rule.created": "budget_rule",
    "budget_rule.updated": "budget_rule",
    "dlp_rule.created": "dlp_rule",
    "dlp_rule.updated": "dlp_rule",
    "webhook_delivery.replayed": "webhook_delivery",
    "webhook_dead_letter.archived": "webhook_dead_letter",
    "webhook_endpoint.secret_rotated": "webhook_endpoint",
    "usage_export.requested": "usage_export_job",
    "usage_export.downloaded": "usage_export_job",
    "policy.created": "policy",
    "policy.updated": "policy",
    "policy_snapshot.published": "policy_snapshot",
    "policy_snapshot.rolled_back": "policy_snapshot",
    # 模板应用/撤销是跨 Policy 与 Snapshot 的治理命令，使用独立 application 对象避免
    # 与普通策略编辑或快照发布混淆；摘要只保存不可逆 hash 和资源版本。
    "policy_template.applied": "policy_template_application",
    "policy_template.rolled_back": "policy_template_application",
    "policy.dry_run_executed": "policy_explanation",
    # RoutingPolicy 审计只描述 Alias 路由图和模式版本，不记录价格、KeyPool 或请求正文。
    "routing_policy.created": "routing_policy",
    "routing_policy.updated": "routing_policy",
    # 人工评分是管理员治理配置；自动统计不产生高频管理审计。
    "model_quality_score.updated": "model_quality_score",
    # CacheAffinity 是独立版本配置，不能复用 RoutingPolicy action 混淆审计对象。
    "cache_affinity.created": "cache_affinity",
    "cache_affinity.updated": "cache_affinity",
    "finops.adjustment_created": "finops_adjustment",
    # 成本异常动作只审计状态与非敏感限流摘要，不记录原始账本、请求正文或幂等键。
    "cost_anomaly.acknowledged": "cost_anomaly",
    "cost_anomaly.dismissed": "cost_anomaly",
    "cost_anomaly.temporary_rate_limit_applied": "cost_anomaly",
    "cost_anomaly.temporary_rate_limit_released": "cost_anomaly",
    # 会话风险人工处置只审计固定状态、分数和不透明 overlay ID；完整 API scope、
    # Prompt、命中摘要、幂等 HMAC 和 Virtual Key 明文均不进入 AuditLog。
    "session_risk.freeze_session": "session_risk",
    "session_risk.freeze_virtual_key": "session_risk",
    "session_risk.dismiss": "session_risk",
    "session_risk.release_session": "session_risk",
    "session_risk.release_virtual_key": "session_risk",
    # 错误归一规则的创建与更新必须绑定同事务 source event，不能写成无 owner 的通用审计。
    "error_map.created": "provider_error_map",
    "error_map.updated": "provider_error_map",
    # 审计导出动作只描述不可变复核事实，不携带 CSV、筛选原文或操作者网络信息。
    "audit.exported": "audit_export",
    # 配置迁移审计只绑定不可变批次事实和计数，不复制导入/导出正文或任何凭据。
    "config.imported": "config_transfer",
    "config.exported": "config_transfer",
    # 权限矩阵变更与角色对象分开命名，摘要只保存 revision、hash 和计数。
    "role.permission_changed": "role_permission",
    # 邀请创建与接受都属于租户用户治理动作；对象统一使用不可变 Invitation 身份。
    "user.invited": "invitation",
    "invitation.accepted": "invitation",
    # Prompt 模板元数据和不可变版本分开审计；正文、变量定义与变更说明只允许加密落库。
    "prompt_template.created": "prompt_template",
    "prompt_template.updated": "prompt_template",
    "prompt_template.version_published": "prompt_template_version",
    # Online experiment creation is a governance action over live Alias traffic. The audit
    # projection is deliberately limited to immutable local owners and integer configuration;
    # the user-facing name and all request-level facts remain outside AuditLog.
    "experiment.created": "experiment",
    # Response Cache 管理动作只审计策略安全快照和失效计数；entry、正文、摘要与账务 owner
    # 均由领域事实表保存，不能复制进通用 AuditLog。
    "response_cache.policy_created": "response_cache_policy",
    "response_cache.policy_updated": "response_cache_policy",
    "response_cache.invalidated": "response_cache_invalidation",
    # 只读助手 attempt 是不可变且无正文的治理事实；问题、回答、目标请求、来源值以及
    # 模型/Token 字段均不得进入通用 AuditLog，只保留类型、终态与有界计数。
    "admin_assistant.queried": "assistant_query",
    # 套餐与额度管理只审计当前租户的状态、精确金额和不可逆原因摘要。
    "plan.created": "plan",
    "plan.updated": "plan",
    "quota.adjustment_created": "quota_adjustment",
    # 账单争议状态迁移只使用固定七字段摘要；自由文本理由与调整明细留在领域表。
    "billing_dispute.created": "billing_dispute",
    "billing_dispute.review_started": "billing_dispute",
    "billing_dispute.approved": "billing_dispute",
    "billing_dispute.rejected": "billing_dispute",
    "billing_dispute.cancelled": "billing_dispute",
    # MCP 管理审计仅保存状态、revision、计数和固定错误码。Endpoint、Credential、
    # Schema、description、授权列表和结果正文均由 tenant-qualified 领域表独占。
    "mcp.server_registered": "mcp_server",
    "mcp.server_refreshed": "mcp_server",
    "mcp.server_credential_rotated": "mcp_server",
    "mcp.server_updated": "mcp_server",
    "mcp.server_disabled": "mcp_server",
    "mcp.server_enabled": "mcp_server",
    "mcp.server_operation_completed": "mcp_server_operation",
    "mcp.tool_refreshed": "mcp_tool",
    "mcp.tool_policy_updated": "mcp_tool",
    "mcp.tool_grants_updated": "mcp_tool",
    "mcp.network_policy_updated": "mcp_network_policy",
}

# 配置迁移的导入、导出摘要虽共用对象类型，但机器字段必须按 action 精确互斥，禁止联合白名单混写。
CONFIG_IMPORT_AUDIT_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_sha256",
        "providers_created_count",
        "providers_unchanged_count",
        "models_created_count",
        "models_unchanged_count",
        "aliases_created_count",
        "aliases_unchanged_count",
        "virtual_keys_created_count",
        "virtual_keys_unchanged_count",
        "generated_virtual_key_count",
        "disabled_virtual_key_count",
    }
)
CONFIG_EXPORT_AUDIT_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_sha256",
        "providers_count",
        "models_count",
        "aliases_count",
        "virtual_keys_count",
    }
)
# 各集合与全清单上限复用 ConfigExport v1 合同，避免审计事实声明服务端不可能处理的批次规模。
CONFIG_TRANSFER_COUNT_LIMITS = {
    "providers": 500,
    "models": 2_000,
    "aliases": 2_000,
    "virtual_keys": 2_000,
}
CONFIG_TRANSFER_TOTAL_LIMIT = 5_000

# 对象级字段白名单是审计入库的主要安全边界。新增领域字段必须在设计审查后显式加入，
# 不能通过序列化数据库整行或请求 DTO 的方式自动扩展。
SUMMARY_FIELDS: dict[str, frozenset[str]] = {
    "mcp_network_policy": frozenset(
        {
            "mcp_server_id",
            "status",
            "revision",
            "egress_mode",
            "destination_count",
            "connect_timeout_seconds",
            "probe_timeout_seconds",
            "call_timeout_seconds",
            "failure_threshold",
            "open_seconds",
            "waiting_job_resume_count",
        }
    ),
    "mcp_server": frozenset(
        {
            "mcp_server_id",
            "status",
            "state_revision",
            "active_credential_revision",
            "tool_count",
            "active_tool_count",
        }
    ),
    "mcp_server_operation": frozenset(
        {
            "mcp_server_operation_id",
            "mcp_server_id",
            "operation_type",
            "status",
            "start_server_revision",
            "candidate_credential_revision",
            "error_code",
        }
    ),
    "mcp_tool": frozenset(
        {
            "mcp_tool_id",
            "mcp_server_id",
            "status",
            "schema_revision",
            "policy_revision",
            "risk_level",
            "approval_required",
            "required_permission_count",
            "role_grant_count",
            "user_grant_count",
            "redaction_count",
        }
    ),
    "response_cache_policy": frozenset(
        {
            "status",
            "data_class",
            "ttl_seconds",
            "charge_cache_hits",
            "tags",
            "version",
            "invalidation_generation",
        }
    ),
    "response_cache_invalidation": frozenset(
        {
            "cache_invalidation_id",
            "scope",
            "match_mode",
            "tag_count",
            "matched_entry_count",
            "deleted_entry_count",
            "fenced_fill_count",
            "reason_code",
        }
    ),
    "experiment": frozenset(
        {
            "experiment_id",
            "project_id",
            "environment_id",
            "model_alias_id",
            "model_a_id",
            "model_b_id",
            "target_sample_count",
            "traffic_sample_bps",
            "model_a_allocation_bps",
            "sampling_duration_seconds",
            "feedback_collection_enabled",
            "feedback_window_seconds",
        }
    ),
    "config_transfer": frozenset(
        CONFIG_IMPORT_AUDIT_FIELDS | CONFIG_EXPORT_AUDIT_FIELDS
    ),
    "provider_error_map": frozenset(
        {
            "provider_id",
            "priority",
            "http_statuses_json",
            "http_status_class",
            "error_field",
            "field_operator",
            "message_operator",
            "output_error_code",
            "status",
            "version",
            "pattern_hash",
        }
    ),
    "cost_anomaly": frozenset(
        {
            "status",
            "rule_type",
            "severity",
            "environment_id",
            "scope_type",
            "scope_id",
            "dismiss_reason",
            "temporary_rate_limit_override_id",
            "rpm_limit",
            "tpm_limit",
            "concurrency_limit",
            "effective_status",
        }
    ),
    "session_risk": frozenset(
        {
            "scope_type",
            "status",
            "score",
            "state_revision",
            "session_risk_freeze_id",
            "target_type",
        }
    ),
    "provider": frozenset(
        {
            "name",
            "provider_code",
            "provider_type",
            "base_url_hash",
            "auth_type",
            "description_hash",
            # 状态页维护备注只允许不可逆摘要进入 AuditLog/Webhook，原文由 Provider 表独占。
            "maintenance_note_hash",
            "status",
        }
    ),
    "provider_key": frozenset({"provider_id", "name", "key_prefix", "weight", "status"}),
    "virtual_key": frozenset(
        {
            "name",
            "key_prefix",
            "environment_id",
            "project_id",
            "application_id",
            "description_hash",
            "status",
            "expires_at",
            "rpm_limit",
            "tpm_limit",
            "concurrency_limit",
            "rate_limit_status",
        }
    ),
    "budget_rule": frozenset(
        {
            "environment_id",
            "scope_type",
            "scope_id",
            "name",
            "daily_budget_cny",
            "warning_threshold_percent",
            "exhausted_action",
            "status",
        }
    ),
    # scan_scope 会改变输入、输出两条安全热路径，必须进入不可抵赖审计；
    # 摘要仍只保存枚举和不可逆正文哈希。
    "dlp_rule": frozenset(
        {
            "name",
            "rule_type",
            "action",
            "scan_scope",
            "status",
            "priority",
            "pattern_hash",
            "description_hash",
        }
    ),
    # Webhook 审计只保留定位 ID、状态和 Secret 版本，禁止保存 URL、payload、幂等键或密钥摘要。
    "webhook_delivery": frozenset(
        {
            "endpoint_id",
            "event_type",
            "status",
            "source_delivery_id",
            "replay_delivery_id",
            "secret_version",
        }
    ),
    "webhook_endpoint": frozenset({"status", "secret_version", "secret_rotated_at"}),
    # 死信归档审计只保留稳定引用、前后状态和治理时间，不复制 payload、URL 或 worker lease。
    "webhook_dead_letter": frozenset(
        {
            "dead_letter_event_id",
            "webhook_delivery_id",
            "webhook_endpoint_id",
            "event_type",
            "status",
            "failed_at",
            "archived_at",
        }
    ),
    # 用量导出审计只保存任务规模与不可逆筛选指纹，严禁写 CSV、筛选全文或对象路径。
    "usage_export_job": frozenset(
        {
            "status",
            "generation_mode",
            "format",
            "row_count",
            "total_tokens",
            "total_cost_cny",
            "total_cost_usd",
            "content_size_bytes",
            "download_count",
            "filter_fingerprint",
        }
    ),
    # 策略审计只保留可追踪元数据和不可逆条件摘要，表达式、AST 与示例上下文不得入库。
    "policy": frozenset(
        {
            "name",
            "status",
            "priority",
            "version",
            "dsl_version",
            "action_type",
            "target_model_id",
            "condition_hash",
            "variables",
            "description_hash",
        }
    ),
    # 快照审计只保存版本、不可逆 hash、数量和 provenance；变更原因仅保存不可逆摘要。
    "policy_snapshot": frozenset(
        {
            "snapshot_version",
            "content_hash",
            "artifact_hash",
            "policy_count",
            "operation",
            "previous_snapshot_version",
            "rollback_source_snapshot_version",
            "approval_status",
            "change_reason_hash",
        }
    ),
    # 模板 application 摘要不得复制 bindings、阈值、价格或 DSL；changed_resource_types
    # 使用稳定逗号标量，Webhook source event 才使用严格有界数组。
    "policy_template_application": frozenset(
        {
            "status",
            "template_code",
            "template_version",
            "application_id",
            "generated_policy_id",
            "previous_snapshot_version",
            "result_snapshot_version",
            "rollback_snapshot_version",
            "impact_hash",
            "bindings_hash",
            "changed_resource_types",
        }
    ),
    # Dry-Run 审计只保存可与 explanation/event 权威核对的安全摘要。Prompt、输入 DTO、
    # 完整规则树、路由 Endpoint 和预算余额不属于白名单，即使来自内部服务也必须拒绝。
    "policy_explanation": frozenset(
        {
            "policy_explanation_id",
            "policy_set_fingerprint",
            "decision",
            "decision_reason_code",
            "matched_policy_id",
            "matched_priority",
            "matched_action",
            "route_status",
            "dlp_status",
            "budget_status",
            "would_error_code",
            "checked_policy_count",
            "runtime_compatible",
            "coverage_complete",
        }
    ),
    # 路由策略与 Alias 一对一，审计快照保留可解释路由图；Provider Key、Endpoint、
    # 价格正文和 Fallback 条件不属于管理变更摘要，防止审计库成为敏感配置副本。
    "routing_policy": frozenset(
        {
            "model_alias_id",
            "alias",
            "environment",
            "status",
            "default_model_id",
            "fallback_model_id",
            "routing_mode",
            "quality_weight_bps",
            "cost_weight_bps",
            "latency_weight_bps",
            "quality_floor_bps",
            "minimum_sample_count",
            "version",
        }
    ),
    # 模型评分审计只保留人工先验及其独立版本；成功/错误/延迟由不可变调用事实重建，
    # 不把高频统计复制到管理审计，更不能加入模型输出或请求内容。
    # 亲和审计只保留管理员可见配置；运行 data digest、Binding、Credential 永不进入摘要。
    "cache_affinity": frozenset(
        {
            "model_alias_id",
            "status",
            "data_class",
            "minimum_prefix_chars",
            "ttl_seconds",
            "version",
        }
    ),
    "model_quality_score": frozenset(
        {
            "model_quality_score_id",
            "model_id",
            "status",
            "manual_quality_bps",
            "feedback_version",
        }
    ),
    "finops_adjustment": frozenset(
        {
            "adjustment_type",
            "amount",
            "currency",
            "reversal_of_entry_id",
            "related_request_id",
            "reason_sha256",
        }
    ),
    "billing_dispute": frozenset(
        {
            "status",
            "state_revision",
            "scope_type",
            "item_count",
            "adjustment_count",
            "currency_totals_sha256",
            "reason_sha256",
        }
    ),
    # 权限矩阵审计不复制完整 permission 列表；diff 由同事务不可变 source event 保存。
    "role_permission": frozenset(
        {"role_code", "permission_revision", "matrix_sha256", "permission_count"}
    ),
    # 邮箱只保存在 tenant-qualified Invitation；审计使用随机 invitation_id 加盐后的指纹。
    "invitation": frozenset(
        {"email_fingerprint", "role_code", "status", "expires_at", "accepted_user_id"}
    ),
    "audit_export": frozenset(
        {
            "status",
            "format",
            "schema_version",
            "row_count",
            "content_size_bytes",
            "content_sha256",
            "filter_fingerprint",
            "snapshot_audit_log_id",
            "snapshot_created_at",
        }
    ),
    # 两类模板对象故意共享同一组安全计数字段，使创建、启停和发布都可由数据库
    # owner trigger 反向复核；名称只使用 AuditLog.object_name，绝不复制正文或绑定清单。
    "prompt_template": frozenset(
        {
            "status",
            "revision",
            "version_number",
            "project_scope",
            "project_count",
            "recommended_model_count",
            "definition_size_bytes",
            "variable_count",
            "change_summary_hmac",
        }
    ),
    "prompt_template_version": frozenset(
        {
            "status",
            "revision",
            "version_number",
            "project_scope",
            "project_count",
            "recommended_model_count",
            "definition_size_bytes",
            "variable_count",
            "change_summary_hmac",
        }
    ),
    "assistant_query": frozenset(
        {
            "query_type",
            "outcome",
            "claim_count",
            "source_count",
            "redaction_count",
            "execution_mode",
            "model_call_count",
            "model_cost_status",
        }
    ),
    "plan": frozenset(
        {
            "display_name",
            "base_tier",
            "status",
            "trial_status",
            "trial_started_at",
            "trial_ends_at",
            "trial_cycle",
            "state_revision",
            "quota_generation",
        }
    ),
    "quota_adjustment": frozenset(
        {
            "plan_id",
            "quota_bucket_id",
            "quota_type",
            "direction",
            "amount",
            "expected_bucket_revision",
            "resulting_bucket_revision",
            "reason_sha256",
        }
    ),
}

# 最终拒绝集合防止调用方未来误把秘密字段加入白名单。使用完整键名而非 `key` 子串，
# 这样 `provider_key_id`、`key_prefix` 和 `pattern_hash` 等安全定位字段不会被误伤。
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "password",
        "secret",
        "access_token",
        "refresh_token",
        "token",
        "authorization",
        "cookie",
        "plain_value",
        "encrypted_key",
        "key_hash",
        "provider_key",
        "pattern",
        "prompt",
        "content",
        # variables 仍按全局敏感字段处理；仅 Policy 固定 DSL 名称可由领域证明函数放行。
        "variables",
        "template_variables",
        "rendered_user",
        "change_summary",
        "definition_ciphertext",
        "messages",
        "response",
        "condition_expr",
        "condition_ast",
        "canonical_expression",
        "context",
    }
)


@dataclass(frozen=True)
class AuditRequestMeta:
    """从可信 HTTP 请求边界提取的来源元数据。

    `client_ip` 只取 ASGI 直接对端，不读取客户端可伪造的 X-Forwarded-For；User-Agent
    已移除控制字符并按 UTF-8 字节长度截断。
    """

    client_ip: str | None
    user_agent: str | None


@dataclass(frozen=True)
class AuditActor:
    """描述可被数据库 owner trigger 复核的显式审计操作者。

    常规受保护接口仍从 ``AuthContext`` 构造该结构。公开邀请接受接口没有 Session，
    只能在同一事务成功创建用户后，用该用户的 tenant/user_id/role 构造 actor，并把
    email 字段替换为领域固定脱敏标记；数据库 trigger 会反查真实用户与 Invitation，
    调用方不得使用请求正文或 Token 中的未验证身份填充这些字段。
    """

    tenant_id: str
    user_id: str
    email: str
    role: str


@dataclass(frozen=True)
class AuditRecord:
    """业务事务提交后投递 `audit.recorded` 所需的最小安全事件事实。"""

    audit_log_id: str
    tenant_id: str
    actor_user_id: str
    action: str
    object_type: str
    object_id: str
    changed_fields: tuple[str, ...]
    trace_id: str
    created_at: datetime


def new_audit_log_id() -> str:
    """生成可由强审计事务预留的稳定 AuditLog ID。"""

    return f"aud_{uuid4().hex}"


def commit_audited_transaction(connection: sqlite3.Connection) -> None:
    """提交已包含审计行的业务事务，并把数据库提交故障映射为可重试统一错误。

    领域服务仍负责在捕获异常后 rollback；本函数只确保 SQLite commit 异常不会穿透为
    `infra.unhandled`，从而让调用方能够按 503 进行安全重试。
    """

    try:
        connection.commit()
    # commit 失败意味着业务和审计均未得到可靠确认，统一按审计记录不可用处理。
    except sqlite3.Error as exc:
        raise ApiError("audit_log.record_unavailable") from exc


def build_audit_request_meta(
    request: Request,
    *,
    sensitive_values: tuple[str, ...] = (),
    include_user_agent: bool = True,
) -> AuditRequestMeta:
    """从 FastAPI Request 构造审计来源，不信任转发 IP 或携带凭据的 User-Agent。

    参数：可信 ASGI Request、仅用于本次内存比较的敏感值，以及是否允许采集 User-Agent；
    返回清洗后的来源元数据。
    约束：公开 Invitation accept 的 Token 位于 path，攻击者也可把完整值或连续片段复制到
    User-Agent。命中时整项置空，不能尝试局部遮罩后把可重组凭据持久化进 AuditLog。
    """

    direct_host = request.client.host if request.client else None
    # 公开凭据入口可完全关闭 User-Agent 持久化；这比依赖攻击者可控文本的片段扫描更稳妥。
    user_agent = (
        _clean_text(request.headers.get("User-Agent"), MAX_USER_AGENT_BYTES)
        if include_user_agent
        else None
    )
    if _text_exposes_sensitive_value(user_agent, sensitive_values):
        user_agent = None
    return AuditRequestMeta(
        client_ip=_normalize_client_ip(direct_host),
        user_agent=user_agent,
    )


def _text_exposes_sensitive_value(
    candidate: str | None,
    sensitive_values: tuple[str, ...],
) -> bool:
    """判断有界审计文本是否包含凭据完整值或至少 16 个连续字符。

    Invitation Token 至少有 256-bit 熵，连续 16 字符已构成不可接受的可复用泄漏片段。
    本函数不构造日志、不返回匹配内容，且候选已由 `_clean_text` 限制字节长度。
    """

    if type(candidate) is not str or not candidate:
        return False
    for raw_secret in sensitive_values:
        # 空值或非字符串不构成可比较凭据，避免宽松转换制造误命中或异常文本。
        if type(raw_secret) is not str or not raw_secret:
            continue
        # 完整凭据命中必须立即拒绝，不能等待片段扫描后再决定是否落库。
        if raw_secret in candidate:
            return True
        if len(raw_secret) >= 16 and any(
            raw_secret[start : start + 16] in candidate
            for start in range(len(raw_secret) - 15)
        ):
            return True
    return False


def record_audit_log(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    action: str,
    object_type: str,
    object_id: str,
    object_name: str | None,
    before_summary: dict[str, object],
    after_summary: dict[str, object],
    request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
    audit_log_id: str | None = None,
    created_at: datetime | None = None,
) -> AuditRecord | None:
    """在调用方当前事务内追加一条审计记录。

    返回 None 表示白名单字段没有实际变化，调用方仍可正常提交业务幂等更新，但不应发送
    `audit.recorded`。audit_log_id/created_at 只供需要延迟复合 FK 的内部强审计事务预生成身份；
    普通调用仍由本函数生成。该函数不自行 commit/rollback，事务控制权始终属于领域服务。
    """

    return record_audit_log_for_actor(
        connection,
        AuditActor(
            tenant_id=context.user.tenant_id,
            user_id=context.user.user_id,
            email=context.user.email,
            role=context.user.role,
        ),
        action=action,
        object_type=object_type,
        object_id=object_id,
        object_name=object_name,
        before_summary=before_summary,
        after_summary=after_summary,
        request_meta=request_meta,
        request_id=request_id,
        trace_id=trace_id,
        audit_log_id=audit_log_id,
        created_at=created_at,
    )


def record_audit_log_for_actor(
    connection: sqlite3.Connection,
    actor: AuditActor,
    *,
    action: str,
    object_type: str,
    object_id: str,
    object_name: str | None,
    before_summary: dict[str, object],
    after_summary: dict[str, object],
    request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
    audit_log_id: str | None = None,
    created_at: datetime | None = None,
) -> AuditRecord | None:
    """在当前事务内为一个已验证显式 actor 追加审计记录。

    参数：actor 必须来自认证上下文，或来自当前事务刚写入且可由数据库复合关系复核的用户。
    返回：真实字段变化对应的不可变 AuditRecord；无变化时返回 None。
    约束：本函数不提交事务，且数据库 owner trigger 仍会校验 actor、tenant 和业务对象关系，
    因此公开接口不能借该入口伪造另一个租户或尚未落库的操作者。
    """

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in ACTION_OBJECT_TYPES:
        # 写入未知动作是服务端接入缺陷，按内部不安全摘要处理并让领域事务回滚。
        raise ApiError("audit_log.summary_invalid")
    normalized_object_type = _normalize_object_type(object_type)
    if ACTION_OBJECT_TYPES[normalized_action] != normalized_object_type:
        # 动作与对象不匹配属于服务端审计接入错误，必须回滚业务而不是写入含糊事实。
        raise ApiError("audit_log.summary_invalid")
    normalized_object_id = _normalize_required_text(object_id, MAX_FILTER_LENGTH)
    normalized_request_id = _normalize_required_text(request_id, MAX_FILTER_LENGTH)
    normalized_trace_id = _normalize_required_text(trace_id, MAX_FILTER_LENGTH)
    before = _validate_summary(normalized_object_type, before_summary)
    after = _validate_summary(normalized_object_type, after_summary)
    try:
        # 一次性治理事实与普通更新都由同一函数重算 changed_fields，写入和存量解码因此不会漂移。
        changed_fields = _expected_changed_fields(
            normalized_action,
            normalized_object_type,
            before,
            after,
        )
    # changed_fields 是审计不可抵赖合同的一部分；动作与摘要不匹配时必须折叠为公开错误码，
    # 不能把内部 ValueError 文本或字段推导细节暴露给调用方。
    except ValueError as exc:
        raise ApiError("audit_log.summary_invalid") from exc
    if not changed_fields:
        return None

    normalized_audit_log_id = (
        _normalize_audit_log_id(audit_log_id) if audit_log_id is not None else new_audit_log_id()
    )
    occurred_at = created_at or _utcnow()
    if occurred_at.tzinfo is None:
        # 强审计时间必须可比较且可复核，内部调用也不能写入含糊的本地时间。
        raise ApiError("audit_log.summary_invalid")
    occurred_at = occurred_at.astimezone(UTC)
    try:
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
              action, object_type, object_id, object_name,
              before_summary_json, after_summary_json, changed_fields_json,
              client_ip, user_agent, request_id, trace_id, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_audit_log_id,
                actor.tenant_id,
                actor.user_id,
                actor.email,
                actor.role,
                normalized_action,
                normalized_object_type,
                normalized_object_id,
                _clean_text(object_name, MAX_OBJECT_NAME_BYTES),
                _json_text(before),
                _json_text(after),
                _json_text(list(changed_fields)),
                request_meta.client_ip,
                request_meta.user_agent,
                normalized_request_id,
                normalized_trace_id,
                _to_db_time(occurred_at),
            ),
        )
    # 不记录 SQL、摘要或请求体，只把统一错误抛给领域服务，由领域服务显式回滚业务事务。
    except sqlite3.Error as exc:
        raise ApiError("audit_log.record_unavailable") from exc
    return AuditRecord(
        audit_log_id=normalized_audit_log_id,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action=normalized_action,
        object_type=normalized_object_type,
        object_id=normalized_object_id,
        changed_fields=changed_fields,
        trace_id=normalized_trace_id,
        created_at=occurred_at,
    )


def dispatch_audit_recorded(
    config: AppConfig,
    context: AuthContext,
    record: AuditRecord | None,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后尽力投递 `audit.recorded`，任何外部失败都不能改变已提交业务结果。"""

    # context 参数保留既有领域调用合同；事件身份只使用已提交 AuditRecord，避免提交后
    # 再读取可能已经变化的 Session 或权限快照。
    dispatch_audit_recorded_fact(
        config,
        record,
        request_id=request_id,
        trace_id=trace_id,
    )


def dispatch_audit_recorded_fact(
    config: AppConfig,
    record: AuditRecord | None,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后仅凭不可变 AuditRecord 尽力投递统一审计事件。

    公开邀请接受没有 AuthContext，但其审计 actor 已被同事务数据库 owner trigger 复核；
    因此提交后只需使用 AuditRecord，不能为投递伪造 Session 或把一次性 Token 带入上下文。
    """

    if record is None:
        return
    try:
        # 事件构造也会执行 payload 安全校验，必须与外部发送处于同一提交后异常隔离边界。
        event = build_webhook_event(
            tenant_id=record.tenant_id,
            event_type="audit.recorded",
            event_id=f"evt_{record.audit_log_id}",
            timestamp=record.created_at,
            actor_id=record.actor_user_id,
            object_id=record.object_id,
            trace_id=record.trace_id,
            payload={
                "audit_log_id": record.audit_log_id,
                "action": record.action,
                "object_type": record.object_type,
                "object_id": record.object_id,
                "changed_fields": list(record.changed_fields),
            },
        )
        # 延迟导入避免 audit 与 webhooks 服务模块在应用启动阶段形成循环依赖。
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            record.tenant_id,
            event,
            request_id=request_id,
            trace_id=trace_id,
        )
    except Exception:
        # 外部异常文本可能包含 SQL、URL 或客户响应，日志只写固定事件名和白名单定位字段。
        logger.error(
            "audit.webhook_dispatch_failed",
            extra={
                "event": "audit.webhook_dispatch_failed",
                "tenant_id": record.tenant_id,
                "actor_id": record.actor_user_id,
                "object_id": record.object_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def list_audit_logs(
    config: AppConfig,
    context: AuthContext,
    *,
    actor_user_id: str | None,
    action: str | None,
    object_type: str | None,
    object_id: str | None,
    request_id_filter: str | None,
    created_from: str | None,
    created_to: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> AuditLogListResponse:
    """按当前认证租户分页查询审计日志，所有可选条件都只能缩小该租户结果集。"""

    filters = normalize_audit_filters(
        actor_user_id=actor_user_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        request_id=request_id_filter,
        created_from=created_from,
        created_to=created_to,
        allowed_actions=ACTION_OBJECT_TYPES,
        allowed_object_types=SUMMARY_FIELDS,
    )
    where_sql, params = audit_where_clause(context.user.tenant_id, filters)
    normalized_limit = _normalize_limit(limit)
    normalized_offset = _normalize_offset(offset)
    connection = _connect(config)
    try:
        total_row = connection.execute(
            f"SELECT COUNT(*) AS total FROM audit_logs WHERE {where_sql}", params
        ).fetchone()
        rows = connection.execute(
            f"""
            SELECT * FROM audit_logs
            WHERE {where_sql}
            ORDER BY created_at DESC, audit_log_id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, normalized_limit, normalized_offset],
        ).fetchall()
        response = AuditLogListResponse(
            audit_logs=[decode_audit_log_row(row) for row in rows],
            total=int(total_row["total"] if total_row else 0),
            limit=normalized_limit,
            offset=normalized_offset,
            request_id=request_id,
            trace_id=trace_id,
        )
    # 查询异常只暴露统一错误码，不向控制台泄露 SQL 或底层表结构。
    except sqlite3.Error as exc:
        raise ApiError("audit_log.query_unavailable") from exc
    finally:
        # Windows 会长期持有未关闭的 SQLite 文件句柄；所有成功和异常路径都必须显式释放。
        connection.close()
    return response


def _validate_summary(object_type: str, value: dict[str, object]) -> dict[str, object]:
    """按对象白名单验证审计摘要，并执行敏感键与大小的最终拒绝。"""

    # 审计摘要必须是扁平对象，拒绝列表、字符串等意外输入。
    if not isinstance(value, dict):
        raise ApiError("audit_log.summary_invalid")
    allowed = SUMMARY_FIELDS[object_type]
    normalized: dict[str, object] = {}
    for key, item in value.items():
        normalized_key = str(key).strip()
        lowered = normalized_key.lower()
        # ``variables`` 通常承载 Prompt 实参，仍属于全局敏感字段。Policy 审计中的同名字段
        # 只是一组共享 DSL 固定变量名，必须通过下面的领域级严格证明后才能作为唯一例外。
        sensitive_field_allowed = _is_safe_policy_variable_summary(
            object_type,
            normalized_key,
            item,
        )
        # 敏感字段和对象白名单之外的字段都不得写入审计库；领域例外不能绕过对象白名单。
        if (
            lowered in SENSITIVE_FIELD_NAMES
            and not sensitive_field_allowed
        ) or normalized_key not in allowed:
            # 未通过领域证明的敏感键或对象白名单外字段必须整体拒绝，不能静默裁剪后落审计。
            raise ApiError("audit_log.summary_invalid")
        # 嵌套容器会放大泄密面，MVP 审计摘要只接受标量值。
        if isinstance(item, (dict, list, tuple, set, bytes, bytearray)):
            raise ApiError("audit_log.summary_invalid")
        normalized[normalized_key] = item
    try:
        encoded = _json_text(normalized).encode("utf-8")
    # 无法稳定序列化的自定义对象按不安全摘要拒绝，并回滚业务事务。
    except (TypeError, ValueError) as exc:
        raise ApiError("audit_log.summary_invalid") from exc
    # 单条摘要设置字节上限，防止自由文本或异常对象撑大审计存储。
    if len(encoded) > MAX_SUMMARY_BYTES:
        raise ApiError("audit_log.summary_invalid")
    return normalized


def _is_safe_policy_variable_summary(
    object_type: str,
    field_name: str,
    value: object,
) -> bool:
    """证明 Policy 的 ``variables`` 仅含规范 DSL 变量名而非 Prompt 实参。

    参数：审计对象类型、规范字段名和待写入标量；返回：仅在 Policy 专用安全例外成立时为真。
    关键约束：值必须是去重、字典序且逗号连接的共享 DSL 固定名称，任何自由文本、容器、
    重复项或非规范顺序都继续命中全局敏感字段拒绝，避免该兼容字段演变成任意变量载体。
    """

    if object_type != "policy" or field_name != "variables" or type(value) is not str:
        return False
    names = value.split(",")
    if not names or any(not name for name in names):
        return False
    canonical = tuple(sorted(set(names)))
    return (
        tuple(names) == canonical
        and len(names) <= len(ALLOWED_VARIABLES)
        and all(name in ALLOWED_VARIABLES for name in names)
    )


def decode_audit_log_row(row: sqlite3.Row) -> AuditLogInfo:
    """严格复演写入合同，把一条持久 AuditLog 转换为安全 DTO。

    列表和导出必须共用本函数，避免一个路径接受未知 action、敏感摘要或伪造 changed_fields，
    另一个路径却失败关闭。安全的历史 JSON 空白与键顺序允许存在，但返回值始终是已验证对象。
    """

    try:
        audit_log_id = _stored_audit_log_id(row["audit_log_id"])
        tenant_id = _stored_required_text(row["tenant_id"], maximum_bytes=160)
        actor_user_id = _stored_required_text(row["actor_user_id"], maximum_bytes=160)
        actor_email = _stored_required_text(row["actor_email"], maximum_bytes=512)
        actor_role = _stored_required_text(row["actor_role"], maximum_bytes=32)
        # 未知角色可能绕过权限语义，不能作为历史兼容值静默展示或导出。
        if actor_role not in ROLE_CODES:
            raise ValueError("actor_role")
        action = _stored_required_text(row["action"], maximum_bytes=160)
        object_type = _stored_required_text(row["object_type"], maximum_bytes=160)
        if action not in ACTION_OBJECT_TYPES or ACTION_OBJECT_TYPES[action] != object_type:
            # 动作与对象必须精确成对，否则筛选、摘要白名单和复核语义都不可信。
            raise ValueError("action_object")
        object_id = _stored_required_text(row["object_id"], maximum_bytes=MAX_FILTER_LENGTH)
        request_id = _stored_required_text(row["request_id"], maximum_bytes=MAX_FILTER_LENGTH)
        trace_id = _stored_required_text(row["trace_id"], maximum_bytes=MAX_FILTER_LENGTH)
        object_name = _stored_optional_text(row["object_name"], maximum_bytes=MAX_OBJECT_NAME_BYTES)
        user_agent = _stored_optional_text(row["user_agent"], maximum_bytes=MAX_USER_AGENT_BYTES)
        client_ip = row["client_ip"]
        if client_ip is not None:
            if type(client_ip) is not str or _normalize_client_ip(client_ip) != client_ip:
                # 存量 IP 不允许在读取时清洗后伪装成原始事实。
                raise ValueError("client_ip")
        before_raw = json.loads(row["before_summary_json"])
        after_raw = json.loads(row["after_summary_json"])
        changed = json.loads(row["changed_fields_json"])
        if not isinstance(before_raw, dict) or not isinstance(after_raw, dict):
            # 摘要必须保持扁平 JSON 对象，列表或标量会绕过字段白名单。
            raise ValueError("summary_shape")
        before = _validate_summary(object_type, before_raw)
        after = _validate_summary(object_type, after_raw)
        # 规范化若改变键名或值，说明存量行绕过了写入白名单，不能静默修复后继续展示。
        if before != before_raw or after != after_raw:
            raise ValueError("summary_normalization")
        expected_changed = _expected_changed_fields(action, object_type, before, after)
        if (
            not isinstance(changed, list)
            or any(type(item) is not str for item in changed)
            or changed != list(expected_changed)
        ):
            # changed_fields 必须等于真实差异的稳定序列，禁止伪造或漏记变更字段。
            raise ValueError("changed_fields")
        created_at = _parse_db_time(row["created_at"])
    # 存量坏值只转换为稳定查询错误；异常文本、字段值和数据库路径均不得进入响应或日志。
    except (ApiError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("audit_log.query_unavailable") from exc
    return AuditLogInfo(
        audit_log_id=audit_log_id,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        actor_role=actor_role,
        action=action,
        object_type=object_type,
        object_id=object_id,
        object_name=object_name,
        before_summary=before,
        after_summary=after,
        changed_fields=list(expected_changed),
        client_ip=client_ip,
        user_agent=user_agent,
        request_id=request_id,
        trace_id=trace_id,
        created_at=created_at,
    )


def _expected_changed_fields(
    action: str,
    object_type: str,
    before: dict[str, object],
    after: dict[str, object],
) -> tuple[str, ...]:
    """按写入规则重算稳定 changed_fields，特殊一次性事实也使用精确字段集合。"""

    if action == "policy.dry_run_executed":
        expected = SUMMARY_FIELDS["policy_explanation"]
        if before or set(after) != expected:
            # Dry-Run 是一次性完整事实，缺字段或伪造 before 都会破坏专用合同。
            raise ValueError("policy_dry_run")
        return tuple(sorted(expected))
    if action == "admin_assistant.queried":
        expected = SUMMARY_FIELDS["assistant_query"]
        if (
            before
            or set(after) != expected
            or not _valid_admin_assistant_audit(after)
        ):
            # 管理助手审计必须是完整一次性安全摘要，不能夹带 before 或问题/回答字段。
            raise ValueError("admin_assistant_summary")
        return tuple(sorted(expected))
    if action in {"config.imported", "config.exported"}:
        expected = (
            CONFIG_IMPORT_AUDIT_FIELDS
            if action == "config.imported"
            else CONFIG_EXPORT_AUDIT_FIELDS
        )
        if (
            before
            or set(after) != expected
            or not _valid_config_transfer_audit_counts(action, after)
        ):
            # 配置迁移是一次性完整事实；动作字段混用、缺字段或非法计数都使事实不可复核。
            raise ValueError("config_transfer_summary")
        return tuple(sorted(expected))
    return tuple(
        sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
    )


def _valid_admin_assistant_audit(after: dict[str, object]) -> bool:
    """校验单次助手 attempt 无正文投影的 exact 字段、枚举与计数关系。"""

    query_type = after.get("query_type")
    outcome = after.get("outcome")
    if query_type not in {
        "usage_statistics",
        "error_explanation",
        "cost_advice",
        "policy_explanation",
        "unknown",
    } or outcome not in {
        "answered",
        "read_only_blocked",
        "unsupported",
        "ambiguous",
        "permission_denied",
        "rate_limited",
        "failed",
    }:
        return False
    counts = tuple(
        after.get(key)
        for key in ("claim_count", "source_count", "redaction_count")
    )
    if any(type(value) is not int or value < 0 for value in counts):
        return False
    claim_count, source_count, redaction_count = counts
    if claim_count > 1000 or source_count > 100 or redaction_count > 100:
        return False
    if outcome == "answered":
        expected_redactions = 3 if query_type == "error_explanation" else 2
        if claim_count < 1 or source_count < 1 or redaction_count != expected_redactions:
            return False
    elif claim_count != 0 or source_count != 0 or redaction_count != 0:
        return False
    return (
        after.get("execution_mode") == "deterministic_read_model_v1"
        and after.get("model_call_count") == 0
        and after.get("model_cost_status") == "not_applicable_local"
    )


def _valid_config_transfer_audit_counts(action: str, after: dict[str, object]) -> bool:
    """校验配置迁移审计的 schema、hash、纯整数范围和跨字段计数关系。

    参数：已通过对象级标量白名单的 after 摘要；返回：全部动作级约束成立时为真。
    关键约束：bool 不得借 int 子类关系通过，导入 generated/disabled 必须精确分割新建 Key。
    """

    manifest = after.get("manifest_sha256")
    if (
        after.get("schema_version") != "metahub.config/v1"
        or type(manifest) is not str
        or len(manifest) != 64
        or any(character not in "0123456789abcdef" for character in manifest)
    ):
        return False
    total = 0
    if action == "config.imported":
        for collection, maximum in CONFIG_TRANSFER_COUNT_LIMITS.items():
            created = after.get(f"{collection}_created_count")
            unchanged = after.get(f"{collection}_unchanged_count")
            if (
                type(created) is not int
                or type(unchanged) is not int
                or created < 0
                or unchanged < 0
                or created + unchanged > maximum
            ):
                return False
            total += created + unchanged
        generated = after.get("generated_virtual_key_count")
        disabled = after.get("disabled_virtual_key_count")
        if (
            type(generated) is not int
            or type(disabled) is not int
            or generated < 0
            or disabled < 0
            or generated + disabled != after.get("virtual_keys_created_count")
        ):
            return False
    else:
        for collection, maximum in CONFIG_TRANSFER_COUNT_LIMITS.items():
            count = after.get(f"{collection}_count")
            if type(count) is not int or count < 0 or count > maximum:
                return False
            total += count
    return total <= CONFIG_TRANSFER_TOTAL_LIMIT


def _append_exact_filter(
    clauses: list[str],
    params: list[object],
    column: str,
    value: str | None,
) -> None:
    """追加受长度约束的精确筛选，列名只允许由内部常量调用。"""

    if value is None or not str(value).strip():
        return
    clauses.append(f"{column}=?")
    params.append(_normalize_required_text(value, MAX_FILTER_LENGTH))


def _normalize_object_type(value: object) -> str:
    """校验审计对象类型，并以统一错误拒绝未知资源。"""

    normalized = str(value or "").strip().lower()
    # 未登记的对象没有确定的摘要白名单，查询和写入都必须拒绝。
    if normalized not in SUMMARY_FIELDS:
        raise ApiError("audit_log.filter_invalid")
    return normalized


def _normalize_action(value: object) -> str:
    """校验稳定审计动作码，查询和写入共用同一集合。"""

    normalized = str(value or "").strip().lower()
    if normalized not in ACTION_OBJECT_TYPES:
        # 未知动作不能静默写入，也不能在查询时伪装成合法但无结果的筛选。
        raise ApiError("audit_log.filter_invalid")
    return normalized


def _normalize_required_text(value: object, max_length: int) -> str:
    """规范化必填筛选或标识，拒绝空值、控制字符和超长输入。"""

    normalized = _clean_text(str(value or "").strip(), max_length)
    # 空标识无法形成可追踪的审计关联，统一按非法筛选处理。
    if not normalized:
        raise ApiError("audit_log.filter_invalid")
    return normalized


def _normalize_audit_log_id(value: object) -> str:
    """校验内部预生成的 `aud_` + 32 位小写十六进制身份。"""

    normalized = str(value or "")
    suffix = normalized[4:] if normalized.startswith("aud_") else ""
    if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
        # 预生成 ID 参与延迟 FK，必须是不可歧义的固定前缀小写十六进制。
        raise ApiError("audit_log.summary_invalid")
    return normalized


def _stored_audit_log_id(value: object) -> str:
    """校验存量 AuditLog ID，并把写入错误转换留给统一 decoder。"""

    try:
        return _normalize_audit_log_id(value)
    # 写入侧错误在 decoder 内转为坏存量标记，不能泄露内部 ApiError 语义。
    except ApiError as exc:
        # 上层统一映射为 audit_log.query_unavailable，原始 ID 不回显。
        raise ValueError("audit_log_id") from exc


def _stored_required_text(value: object, *, maximum_bytes: int) -> str:
    """读取存量必填文本；不清洗坏值，避免把篡改后的事实伪装成合法。"""

    if type(value) is not str or not value or value != value.strip():
        # 非字符串、空值或首尾空白都不是写入合同产生的可信文本。
        raise ValueError("required_text")
    if len(value.encode("utf-8")) > maximum_bytes:
        # 超出写入字节上限的历史值不能进入 API 或 CSV。
        raise ValueError("required_text")
    if any(unicodedata.category(character).startswith("C") for character in value):
        # 控制字符可能形成日志、CSV 或界面注入，读取时必须失败关闭。
        raise ValueError("required_text")
    return value


def _stored_optional_text(value: object | None, *, maximum_bytes: int) -> str | None:
    """读取存量可选文本；非空值必须精确符合写入时的控制字符和字节门禁。"""

    if value is None:
        return None
    return _stored_required_text(value, maximum_bytes=maximum_bytes)


def _clean_text(value: object | None, max_bytes: int) -> str | None:
    """移除 Unicode 控制字符并按 UTF-8 字节安全截断。"""

    if value is None:
        return None
    cleaned = "".join(
        character
        for character in str(value)
        if not unicodedata.category(character).startswith("C")
    ).strip()
    if not cleaned:
        return None
    encoded = cleaned.encode("utf-8")[:max_bytes]
    # 截断可能落在多字节字符中间，循环缩短到合法 UTF-8 边界。
    while encoded:
        try:
            return encoded.decode("utf-8")
        # 命中半个多字节字符时逐字节回退，直到得到合法 UTF-8 文本。
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return None


def _normalize_client_ip(value: str | None) -> str | None:
    """仅接受直接对端的合法 IPv4/IPv6；测试客户端主机名等非 IP 值不入库。"""

    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    # 非法地址或测试客户端主机名不写入 client_ip，不能信任为真实来源。
    except ValueError:
        return None


def _parse_optional_time(value: str | None) -> datetime | None:
    """解析可选 ISO-8601 时间并统一转换为 UTC。"""

    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    # 无法解析的时间格式统一映射为时间范围错误。
    except ValueError as exc:
        raise ApiError("audit_log.time_range_invalid") from exc
    # 无时区时间存在解释歧义，接口只接受明确时区的 ISO-8601 值。
    if parsed.tzinfo is None:
        raise ApiError("audit_log.time_range_invalid")
    return parsed.astimezone(UTC)


def _normalize_limit(value: int) -> int:
    """限制单页规模，避免管理员查询形成无界读取。"""

    # limit 必须处于有限区间，避免无界读取和零尺寸页面。
    if value < 1 or value > MAX_LIMIT:
        raise ApiError("audit_log.pagination_invalid")
    return value


def _normalize_offset(value: int) -> int:
    """限制 offset 范围，MVP 暂沿用控制台偏移分页合同。"""

    # offset 不得为负且设置上限，避免低效的超深偏移查询。
    if value < 0 or value > MAX_OFFSET:
        raise ApiError("audit_log.pagination_invalid")
    return value


def _json_text(value: object) -> str:
    """生成稳定、紧凑的 UTF-8 JSON 文本，便于审计比较与测试。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建审计查询连接并映射基础设施异常。"""

    try:
        return connect_database(config.database_url)
    # 连接建立失败时只返回审计查询不可用，隐藏数据库地址与驱动细节。
    except DbError as exc:
        raise ApiError("audit_log.query_unavailable") from exc


def _utcnow() -> datetime:
    """返回带时区 UTC 当前时间，便于测试替换时间边界。"""

    return datetime.now(UTC)


def _to_db_time(value: datetime) -> str:
    """把时间稳定保存为 UTC 秒级字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: str) -> datetime:
    """解析数据库 UTC 时间并拒绝损坏记录。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 数据库时间损坏意味着该行无法可信展示，终止本次查询。
    except ValueError as exc:
        raise ApiError("audit_log.query_unavailable") from exc
    if parsed.tzinfo is None:
        # 无时区存量值无法在列表与导出之间保持确定顺序和同字节复核。
        raise ApiError("audit_log.query_unavailable")
    return parsed.astimezone(UTC)
