"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义。
Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/runtime/webhooks.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from metahub_shared.mcp.contracts import McpContractError
from metahub_shared.mcp.events import (
    MCP_TOOL_CALLED_EVENT_TYPE,
    validate_mcp_tool_called_event,
    validate_mcp_tool_called_payload,
)
from metahub_shared.mcp.network import (
    MCP_NETWORK_BLOCKED_EVENT_TYPE,
    validate_mcp_network_blocked_event,
    validate_mcp_network_blocked_payload,
)
from metahub_shared.rbac import (
    ADMIN_ROLE,
    PERMISSION_CODES,
    ROLE_CODES,
    canonical_permission_codes,
)

from .provider_error_map import TENANT_PROVIDER_ERROR_OUTPUT_CODES
from .security_events import (
    SECURITY_EVENT_TYPE,
    validate_security_event_envelope,
    validate_security_event_payload,
)

# Webhook 事件类型白名单定义可订阅治理事件，payload 生成和签名校验必须共享同一集合。
WEBHOOK_EVENT_TYPES = {
    "webhook.test",
    "webhook.delivery_failed",
    "webhook.dead_lettered",
    "webhook.replayed",
    "budget.threshold_reached",
    "budget.exhausted",
    "budget.scope_exceeded",
    "budget.blocked",
    "budget.unblocked",
    "security.dlp_matched",
    "security.dlp_unavailable",
    "gateway.request_failed",
    "environment.created",
    "price.updated",
    "usage.normalized",
    "usage.exported",
    "audit.recorded",
    "audit.exported",
    "config.imported",
    "role.permission_changed",
    "user.invited",
    "invitation.accepted",
    "provider.unhealthy",
    "route.fallback_triggered",
    "route.policy_selected",
    "cache.affinity_selected",
    # Response Cache 只外发已原子结算的 hit 与业务失效安全摘要，正文、digest、tag value、
    # Credential 和 source owner 永远不属于订阅合同。
    "cache.hit",
    "cache.invalidated",
    "model.feedback_recorded",
    "feedback.created",
    "stream.timeout",
    "policy.created",
    "policy.validation_failed",
    "policy.dry_run_executed",
    "policy_snapshot.published",
    "ledger.entry_created",
    "cost.anomaly_detected",
    "request.explanation_viewed",
    "log.query_executed",
    "error_map.updated",
    "adapter.spec_updated",
    "adapter.contract_failed",
    # 企业 Chat 事件只外发一次调用的安全治理摘要，正文和正文 hash 均不属于事件合同。
    "chat.message_sent",
    # Prompt 发布事件只外发模板/版本身份与固定计数，不包含名称、正文、变量或变更说明。
    "prompt_template.published",
    # 策略模板应用事件只外发应用、生成 Policy、快照版本与不可逆影响摘要。
    "policy_template.applied",
    # 敏感意图事件只外发不可变分类摘要，不包含输入、命中片段、位置或正文 hash。
    "security.intent_detected",
    # 高危会话事件只外发状态、整数计数和短安全引用，不包含 API session/turn HMAC 或正文。
    "security.session_high_risk",
    # 输出阻断只外发扫描/暴露/结算意图摘要，模型正文、命中片段和规则 pattern 永不进入事件。
    "security.output_blocked",
    # 文件事件只外发处理状态与安全计数，文件名、正文、对象键、hash 和规则 ID 永不进入事件。
    "file.uploaded",
    "file.scanned",
    # 文件注入事件只外发报告计数和风险，不包含文件名、finding 证据、OCR 文本或报告哈希。
    "file.prompt_injection_detected",
    # Batch lifecycle events expose only fixed counters and cost summaries. File identities,
    # operation parameters, prompts, responses, grants, leases, and route plans stay internal.
    "batch_job.created",
    "batch_job.completed",
    # 实验完成事件只披露冻结结果可重建的 owner 与整数计数，不披露名称或请求级事实。
    "experiment.completed",
    # 管理助手事件只外发查询类型、结果和计数；问题、回答和目标 Request 永不进入订阅正文。
    "assistant.queried",
    # 移动审批只外发卡片所需的固定状态、领域枚举、ID 与时间；目标正文和凭据永不出网。
    "approval.created",
    "approval.timeout",
    # 套餐额度事件只外发可复核的金额、层级和周期摘要；Bucket 计数、调整原因、
    # 请求正文、模型、Provider 与任何凭据都不属于订阅合同。
    "quota.exhausted",
    "trial.expired",
    # 账单争议只外发创建时冻结的范围类型、条目计数和按币种汇总；request 列表、
    # 时间范围、reason、价格证据和调整明细均留在 tenant-qualified 权威数据库。
    "billing_dispute.created",
    # MCP invocation events expose only fixed execution, DLP, byte, and cost-status facts.
    # Endpoint, Token, Schema, arguments, result, and fingerprints never enter the event.
    MCP_TOOL_CALLED_EVENT_TYPE,
    # ACL blocks expose only the fixed reason and counters; addresses and origins remain internal.
    MCP_NETWORK_BLOCKED_EVENT_TYPE,
    # SIEM 标准事件只外发聚合后的固定安全投影；原 DLP/会话事件仍保留兼容订阅。
    SECURITY_EVENT_TYPE,
}

# 架构文档中的旧名称只允许在 Endpoint 配置入口归一化；生产事件、签名正文和投递记录
# 一律使用 Excel 冻结的 canonical 名称，避免同一账务事实被下游处理两次。
WEBHOOK_EVENT_TYPE_ALIASES = {"billing.ledger_created": "ledger.entry_created"}

# Endpoint 可订阅目录与内部事件目录必须分开；重放事实和测试事件不能被客户订阅后递归投递。
WEBHOOK_SUBSCRIBABLE_EVENT_TYPES = frozenset(
    WEBHOOK_EVENT_TYPES - {"webhook.test", "webhook.dead_lettered", "webhook.replayed"}
)

# 敏感键按完整规范化字段名拒绝，既阻止 Prompt/Response/Key 泄露，也不误伤 input_tokens 等安全指标。
WEBHOOK_SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "api_key",
        "apikey",
        "provider_key",
        "providerkey",
        "virtual_key",
        "virtualkey",
        "token",
        "token_hash",
        "accept_token",
        "invitation_token",
        "invitation_url",
        "invite_url",
        "access_token",
        "refresh_token",
        "secret",
        "signing_secret",
        "password",
        "prompt",
        "response",
        "request_body",
        "response_body",
        "raw_request",
        "raw_response",
        "body",
        "content",
        "variables",
        "template_variables",
        "rendered_user",
        "change_summary",
        "definition_ciphertext",
        "messages",
        "input",
        "condition_expr",
        "condition_ast",
        "canonical_expression",
        "change_reason",
        "context",
        "question",
        "question_hash",
        "answer",
        "target_request_id",
    }
)

# Adapter 合同失败码是 runner、Control 数据库和订阅方共同使用的固定集合。事件只允许这些
# 不含自由正文的 code，禁止把 Provider 名、URL 或底层异常拼接成新值。
ADAPTER_CONTRACT_FAILURE_CODES = frozenset(
    {
        "adapter.contract_fixture_missing",
        "adapter.contract_fixture_unexpected",
        "adapter.contract_fixture_invalid",
        "adapter.contract_coverage_mismatch",
        "adapter.contract_request_failed",
        "adapter.contract_non_stream_failed",
        "adapter.contract_stream_failed",
        "adapter.contract_error_mapping_failed",
        "adapter.contract_usage_failed",
        "adapter.contract_probe_failed",
    }
)


@dataclass(frozen=True)
class WebhookEvent:
    """Webhook 对外事件信封。

    事件信封只允许携带安全业务摘要；调用方不得把 Prompt、Response、Authorization、
    Provider Key、Virtual Key 明文或 DLP 命中文本放进 payload。
    """

    event_id: str
    event_type: str
    timestamp: str
    tenant_id: str
    payload: dict[str, Any]
    actor_id: str | None = None
    object_id: str | None = None
    trace_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """转换为稳定 JSON 对象，供落库、签名和 HTTP 投递共用。"""

        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "tenant_id": self.tenant_id,
            "actor_id": self.actor_id,
            "object_id": self.object_id,
            "trace_id": self.trace_id,
            "payload": self.payload,
        }


def normalize_webhook_event_type(value: object) -> str:
    """把 Endpoint 配置输入中的历史别名归一为 canonical 事件名。

    参数：用户提交的单个事件类型。返回：去首尾空白后的 canonical 文本或原未知文本。
    约束：本函数只供配置校验入口使用；生产者不得用它静默接受旧事件名，构造事件仍会
    对 canonical 白名单失败关闭。
    """

    normalized = str(value or "").strip()
    return WEBHOOK_EVENT_TYPE_ALIASES.get(normalized, normalized)


def build_webhook_event(
    *,
    tenant_id: str,
    event_type: str,
    payload: dict[str, Any],
    actor_id: str | None = None,
    object_id: str | None = None,
    trace_id: str | None = None,
    source_request_id: str | None = None,
    event_id: str | None = None,
    timestamp: datetime | None = None,
) -> WebhookEvent:
    """构造统一 Webhook 事件信封，并校验事件类型白名单。"""

    if event_type not in WEBHOOK_EVENT_TYPES:
        # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
        raise ValueError("webhook.event_type_invalid")
    occurred_at = timestamp or datetime.now(UTC)
    if event_type == "error_map.updated":
        # 专用 4096-byte 门禁必须先于通用递归复制，防止异常 source payload 放大内存与签名正文。
        _validate_provider_error_map_payload_size(payload)
    safe_payload = validate_webhook_payload(payload)
    if event_type == "policy_snapshot.published":
        # 快照事件只允许设计文档规定的安全摘要，不能把 change reason、DSL 或策略名称扩散
        # 到订阅端；持久 event_id 让提交后重试始终保持同一幂等事实。
        _validate_policy_snapshot_event_payload(safe_payload)
    elif event_type == "policy.dry_run_executed":
        # 单请求预演只外发不可逆候选指纹和阶段状态；决定原因必须使用独立字段，不能
        # 覆盖持久 policy_events.reason_code 的事件事实语义。
        _validate_policy_dry_run_event_payload(safe_payload)
    elif event_type == "route.policy_selected":
        # 运行时选择和配置变更共用事件类型，但各自只能携带设计中精确列出的安全字段。
        _validate_routing_policy_selected_event_payload(safe_payload)
    elif event_type == "cache.affinity_selected":
        # 运行选择和配置变化共用事件名，由 trigger 冻结两套互斥且不含 Prompt 摘要的字段合同。
        _validate_cache_affinity_selected_payload(safe_payload)
    elif event_type == "cache.hit":
        # hit 事件精确绑定四组独立成本事实；金额只能是六位 CNY 或不可用 null。
        _validate_response_cache_hit_payload(safe_payload)
    elif event_type == "cache.invalidated":
        # 失效事件只披露范围、固定 reason 和安全计数，不外发 selector 或 tag value。
        _validate_response_cache_invalidated_payload(safe_payload)
    elif event_type == "model.feedback_recorded":
        # PRD-048 只外发管理员人工先验真实变化；自动调用统计不得制造高频反馈事件。
        _validate_model_feedback_recorded_payload(safe_payload)
    elif event_type == "feedback.created":
        # PRD-049 逐请求反馈只外发七字段安全摘要，不能混入人工先验或请求正文。
        _validate_request_feedback_created_payload(safe_payload)
    elif event_type == "budget.scope_exceeded":
        # 三级预算越限事件只允许持久事实的金额、scope 和调用链摘要，不能混入凭据或正文。
        _validate_budget_scope_exceeded_payload(safe_payload)
    elif event_type == "ledger.entry_created":
        # 正式账本事件只允许不可变分录的安全金额与证据摘要，reason 正文永不外发。
        _validate_finops_ledger_entry_payload(safe_payload)
    elif event_type == "cost.anomaly_detected":
        # 成本异常事件只外发不可变检测证据；Key 名称、请求样本和任何正文都不进入合同。
        _validate_cost_anomaly_detected_payload(safe_payload)
    elif event_type == "request.explanation_viewed":
        # 查看事件只外发解释完整度和源请求安全标识，不复制详情页的治理证据或正文。
        _validate_request_explanation_viewed_payload(safe_payload)
    elif event_type == "log.query_executed":
        # 查询事件只外发规范化指纹、字段名和计数，原始筛选值与名称不得离开控制面。
        _validate_request_log_query_executed_payload(safe_payload)
    elif event_type == "error_map.updated":
        # 错误映射事件只外发不可逆 pattern hash 和稳定资源版本，原始匹配文本永不进入事件。
        _validate_provider_error_map_updated_payload(safe_payload)
    elif event_type == "adapter.spec_updated":
        # Adapter 目录事件只外发冻结身份与切换时间，不复制 spec JSON 或租户 Endpoint。
        _validate_adapter_spec_updated_payload(safe_payload)
    elif event_type == "adapter.contract_failed":
        # 合同失败只外发 run、目录、计数和固定失败码，不复制 case 证据或报告正文。
        _validate_adapter_contract_failed_payload(safe_payload)
    elif event_type == "chat.message_sent":
        # PAGE-093 只外发 Message 终态和 Gateway 治理元数据，不能夹带对话正文或展示名称。
        _validate_chat_message_sent_payload(safe_payload)
    elif event_type == "prompt_template.published":
        # PRD-067 发布事件只接受迁移 source row 冻结的七字段安全计数合同。
        _validate_prompt_template_published_payload(safe_payload)
    elif event_type == "policy_template.applied":
        # PRD-068 应用事件只接受迁移 source row 冻结的九字段安全影响摘要。
        _validate_policy_template_applied_payload(safe_payload)
    elif event_type == "security.intent_detected":
        # PRD-069 只允许结果表可复核的固定分类摘要，正文和命中证据永不进入事件。
        _validate_intent_detected_payload(safe_payload)
    elif event_type == "security.session_high_risk":
        # PRD-070 只接受 source event 的精确状态摘要，完整 API scope/digest 永不出网。
        _validate_session_high_risk_payload(safe_payload)
    elif event_type == "security.output_blocked":
        # PRD-071 只接受持久 blocked scan 可复核的固定摘要，不能复制模型输出或规则正文。
        _validate_output_blocked_payload(safe_payload)
    elif event_type in {"file.uploaded", "file.scanned"}:
        # PRD-072 两个阶段只接受 UploadedFile/FileExtract 的固定安全投影。
        _validate_file_event_payload(event_type, safe_payload)
    elif event_type == "file.prompt_injection_detected":
        # PRD-073 只接受不可变安全报告可反向证明的七字段 blocked 摘要。
        _validate_file_prompt_injection_payload(safe_payload)
    elif event_type in {"batch_job.created", "batch_job.completed"}:
        # PRD-074 只接受 Batch source row 冻结的任务计数与金额摘要。
        _validate_batch_job_event_payload(event_type, safe_payload)
    elif event_type == "experiment.completed":
        # PRD-075 只接受两条 Result 与 Experiment owner 可逐字段重建的十九字段摘要。
        _validate_experiment_completed_payload(safe_payload)
    elif event_type == "assistant.queried":
        # PRD-077 只接受不可变 Query/Audit 可逐字段重建的七字段安全摘要。
        _validate_admin_assistant_queried_payload(safe_payload)
    elif event_type in {"approval.created", "approval.timeout"}:
        # PRD-078 只接受审批卡片的 exact 安全摘要，不允许备注、目标正文或外部平台凭据。
        _validate_approval_event_payload(event_type, safe_payload)
    elif event_type in {"quota.exhausted", "trial.expired"}:
        # PRD-079 只接受 Plan/Event source 可逐字段重建的 exact 安全摘要。
        _validate_quota_event_payload(event_type, safe_payload)
    elif event_type == "billing_dispute.created":
        # PRD-080 只接受 immutable item/source 可重建的七字段创建摘要。
        _validate_billing_dispute_created_payload(safe_payload)
    elif event_type == MCP_TOOL_CALLED_EVENT_TYPE:
        try:
            safe_payload = validate_mcp_tool_called_payload(safe_payload)
        except McpContractError:
            raise ValueError("webhook.payload_invalid") from None
    elif event_type == MCP_NETWORK_BLOCKED_EVENT_TYPE:
        try:
            safe_payload = validate_mcp_network_blocked_payload(safe_payload)
        except McpContractError:
            raise ValueError("webhook.payload_invalid") from None
    elif event_type == SECURITY_EVENT_TYPE:
        # PRD-081 聚合事件必须使用 exact 字段合同，不能通过通用 JSON 校验自由扩展。
        safe_payload = validate_security_event_payload(safe_payload)
    elif event_type == "audit.exported":
        # 审计导出事件只外发不可变复核摘要，筛选原文、CSV 和操作者邮箱不得进入信封。
        _validate_audit_exported_payload(safe_payload)
    elif event_type == "config.imported":
        # 配置导入只外发不可变批次身份、manifest 指纹和有界计数，不复制任一配置行。
        _validate_config_imported_payload(safe_payload)
    elif event_type == "role.permission_changed":
        # 权限矩阵事件只允许可复核 diff 和哈希，不得复制用户、页面草稿或完整租户矩阵。
        _validate_role_permission_changed_payload(safe_payload)
    elif event_type in {"user.invited", "invitation.accepted"}:
        # 邀请事件只披露随机身份、邮箱指纹、角色和时间，不能复制邮箱、Token 或密码。
        _validate_invitation_event_payload(event_type, safe_payload)
    if event_type in {
        "policy_snapshot.published",
        "policy.dry_run_executed",
        "route.policy_selected",
        "cache.affinity_selected",
        "cache.hit",
        "cache.invalidated",
        "model.feedback_recorded",
        "feedback.created",
        "budget.scope_exceeded",
        "ledger.entry_created",
        "cost.anomaly_detected",
        "request.explanation_viewed",
        "log.query_executed",
        "error_map.updated",
        "adapter.spec_updated",
        "adapter.contract_failed",
        "chat.message_sent",
        "prompt_template.published",
        "policy_template.applied",
        "security.intent_detected",
        "security.session_high_risk",
        "security.output_blocked",
        "file.uploaded",
        "file.scanned",
        "file.prompt_injection_detected",
        "batch_job.created",
        "batch_job.completed",
        "experiment.completed",
        "assistant.queried",
        "approval.created",
        "approval.timeout",
        "quota.exhausted",
        "trial.expired",
        "billing_dispute.created",
        MCP_TOOL_CALLED_EVENT_TYPE,
        MCP_NETWORK_BLOCKED_EVENT_TYPE,
        SECURITY_EVENT_TYPE,
        "audit.exported",
        "config.imported",
        "role.permission_changed",
        "user.invited",
        "invitation.accepted",
    }:
        _validate_persistent_event_id(event_id)
    # 路由事件还要绑定 rpe_ 身份、actor、object 和 trace，防止信封与 payload 漂移。
    if event_type == "route.policy_selected":
        _validate_routing_policy_selected_envelope(
            safe_payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
        )
    elif event_type == "cache.affinity_selected":
        # runtime 必须绑定 cas_ Selection 且没有 actor；配置事件必须绑定 cae_ 与可信管理员。
        _validate_cache_affinity_selected_envelope(
            safe_payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
        )
    elif event_type in {"cache.hit", "cache.invalidated"}:
        # 两类 source event 共用 rcev_ 前缀，但 object/actor 矩阵由 event type 严格区分。
        _validate_response_cache_envelope(
            event_type,
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == "model.feedback_recorded":
        _validate_model_feedback_recorded_envelope(
            safe_payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
        )
    elif event_type == "feedback.created":
        # 逐请求反馈必须绑定 fbe_、RequestFeedback 对象、可信 actor 和首次 trace。
        _validate_request_feedback_created_envelope(
            safe_payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
        )
    elif event_type == "ledger.entry_created":
        # 信封必须绑定持久 flevt_/fle_、可信操作者和来源/操作 trace，不能仅校验 payload。
        _validate_finops_ledger_entry_envelope(
            safe_payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
        )
    elif event_type == "budget.scope_exceeded":
        # 运行时预算事件没有用户 actor，且对象必须绑定稳定 BudgetScope 身份。
        _validate_budget_scope_exceeded_envelope(
            safe_payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
        )
    elif event_type == "cost.anomaly_detected":
        # 系统检测事件必须绑定持久 anomaly/event 身份且没有用户 actor，避免事实错配。
        _validate_cost_anomaly_detected_envelope(
            safe_payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
        )
    elif event_type == "request.explanation_viewed":
        # 访问事件必须绑定 rxe_/rxevt_、可信用户和本次 Control operation trace。
        _validate_request_explanation_viewed_envelope(
            safe_payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            occurred_at=occurred_at,
        )
    elif event_type == "log.query_executed":
        # 查询事件必须绑定持久 lqe_/lqevt_、可信 actor 和本次 Control operation trace。
        _validate_request_log_query_executed_envelope(
            safe_payload,
            event_id=event_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            occurred_at=occurred_at,
        )
    # 错误映射事件必须执行专用信封校验，不能退化为只检查通用 Webhook 字段。
    elif event_type == "error_map.updated":
        # 持久 source row 的租户、actor、对象与 trace 都必须形成可追踪且不可混淆的信封。
        _validate_provider_error_map_updated_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
        )
    elif event_type == "adapter.spec_updated":
        # 目录切换必须绑定持久 activation、固定发布 actor、来源请求和同一 UTC 秒。
        _validate_adapter_spec_updated_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == "adapter.contract_failed":
        # 失败事件必须绑定不可变 run、固定 runner actor、首次链路和完成秒。
        _validate_adapter_contract_failed_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == "chat.message_sent":
        # Message、actor、Gateway 链路和完成时间必须与持久 ChatMessage 事实逐字绑定。
        _validate_chat_message_sent_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == "prompt_template.published":
        # 信封必须与 ptev_ source event、精确版本对象及原发布链路逐字绑定。
        _validate_prompt_template_published_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
        )
    elif event_type == "policy_template.applied":
        # 信封必须与 ptevt_ source event、模板应用对象及原始操作链路逐字绑定。
        _validate_policy_template_applied_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
        )
    elif event_type == "security.intent_detected":
        # 信封必须绑定持久 intent source event、结果 owner、actor、原请求和完成时刻。
        _validate_intent_detected_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    # 会话高危信封必须反向绑定同租户 source event，不能只相信调用方提供的 JSON 摘要。
    elif event_type == "security.session_high_risk":
        # source event 必须绑定同一 tenant、score、actor、请求链路和发生秒。
        _validate_session_high_risk_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == "security.output_blocked":
        # 自动阻断事件没有用户 actor；scan、request、trace 和发生秒必须与 source row 一致。
        _validate_output_blocked_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type in {"file.uploaded", "file.scanned"}:
        # 文件事件必须绑定同一个 file owner、可信 Session actor、原请求链路和 source event 秒。
        _validate_file_event_envelope(
            event_type,
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == "file.prompt_injection_detected":
        # 安全事件信封绑定 fsev_ source row、Session actor、file owner 和原扫描链路。
        _validate_file_prompt_injection_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type in {"batch_job.created", "batch_job.completed"}:
        # Batch source event 必须绑定同租户 Job、持久事件、actor 和原始操作链路。
        _validate_batch_job_event_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == "experiment.completed":
        # 完成事件必须复用首次创建 actor/request/trace，scheduler 或重放 worker 不能冒充来源。
        _validate_experiment_completed_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == "assistant.queried":
        # Query、actor、原始操作链路和 stable source event 必须逐字段绑定，不能在重试时重建身份。
        _validate_admin_assistant_queried_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type in {"approval.created", "approval.timeout"}:
        # 审批 source event 必须绑定同租户任务、原始链路与创建者或固定 timeout scheduler。
        _validate_approval_event_envelope(
            event_type,
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type in {"quota.exhausted", "trial.expired"}:
        # 两类额度 source 事件必须绑定同一 Plan、系统 actor、原始链路和发生秒。
        _validate_quota_event_envelope(
            event_type,
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == "billing_dispute.created":
        # source event 必须绑定认证申请人、争议对象和原始创建链路，恢复 worker 不得自造身份。
        _validate_billing_dispute_created_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == MCP_TOOL_CALLED_EVENT_TYPE:
        try:
            safe_payload = validate_mcp_tool_called_event(
                safe_payload,
                event_id=event_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                object_id=object_id,
                trace_id=trace_id,
                source_request_id=source_request_id,
                occurred_at=occurred_at,
            )
        except McpContractError:
            raise ValueError("webhook.payload_invalid") from None
    elif event_type == MCP_NETWORK_BLOCKED_EVENT_TYPE:
        try:
            safe_payload = validate_mcp_network_blocked_event(
                safe_payload,
                event_id=event_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                object_id=object_id,
                trace_id=trace_id,
                source_request_id=source_request_id,
                occurred_at=occurred_at,
            )
        except McpContractError:
            raise ValueError("webhook.payload_invalid") from None
    elif event_type == SECURITY_EVENT_TYPE:
        # 聚合器信封必须绑定稳定 sevt_、固定 actor、窗口结束秒和最后来源请求。
        validate_security_event_envelope(
            {
                "event_id": event_id,
                "event_type": event_type,
                "timestamp": _to_iso(occurred_at),
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "object_id": object_id,
                "trace_id": trace_id,
                "payload": safe_payload,
            },
            source_request_id=source_request_id,
        )
    elif event_type == "audit.exported":
        # source worker 必须把稳定 aexevt_ 事件绑定到同一 AuditExport、actor、trace 和请求事实。
        _validate_audit_exported_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    elif event_type == "config.imported":
        # 批次事件必须与同一 actor、对象、请求和提交秒绑定，防止跨租户事实漂移。
        _validate_config_imported_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    # 持久矩阵事件必须绑定同租户可信操作者、目标角色、来源请求和操作 trace。
    elif event_type == "role.permission_changed":
        _validate_role_permission_changed_envelope(
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
        )
    elif event_type in {"user.invited", "invitation.accepted"}:
        # 创建和接受都必须绑定持久 source event、同租户 actor、Invitation 和操作链路。
        _validate_invitation_event_envelope(
            event_type,
            safe_payload,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            object_id=object_id,
            trace_id=trace_id,
            source_request_id=source_request_id,
            occurred_at=occurred_at,
        )
    return WebhookEvent(
        event_id=event_id or f"evt_{uuid4().hex}",
        event_type=event_type,
        timestamp=_to_iso(occurred_at),
        tenant_id=tenant_id,
        actor_id=actor_id,
        object_id=object_id,
        trace_id=trace_id,
        payload=safe_payload,
    )


def validate_webhook_payload(payload: object) -> dict[str, Any]:
    """递归校验 Webhook 业务 payload，并返回与调用方解耦的安全副本。

    参数：payload 必须是 JSON 对象；键必须是字符串且不能命中敏感字段集合。
    返回：可安全序列化、签名和持久化的新字典。
    约束：任何层级出现 Prompt、Response、Authorization、Key、Token 或 Secret 都整体拒绝，
    不能因为数据已经落库或来自内部服务就跳过检查。
    """

    if not isinstance(payload, dict):
        # 顶层必须是 JSON 对象，数组或标量不能表达稳定业务字段。
        raise ValueError("webhook.payload_invalid")
    copied = _validate_webhook_value(payload, path="payload")
    if not isinstance(copied, dict):
        # 递归复制后的类型仍需守住对象合同，防止未来 helper 行为变化。
        raise ValueError("webhook.payload_invalid")
    return copied


def _validate_webhook_value(value: object, *, path: str) -> Any:
    """递归复制 JSON 值，并在具体字段路径上执行敏感键拒绝。"""

    if isinstance(value, dict):
        copied: dict[str, Any] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                # 非字符串 JSON 键序列化结果不稳定，也无法执行统一敏感键比较。
                raise ValueError("webhook.payload_invalid")
            normalized_key = raw_key.lower().replace("-", "_")
            if _is_sensitive_webhook_key(normalized_key):
                # 任意层级命中敏感键都整体拒绝，不能只删除字段后继续发送不完整事件。
                raise ValueError("webhook.payload_sensitive")
            copied[raw_key] = _validate_webhook_value(child, path=f"{path}.{raw_key}")
        return copied
    if isinstance(value, list):
        return [_validate_webhook_value(item, path=f"{path}[]") for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # 非 JSON 类型可能在序列化阶段泄露对象表示或触发非结构异常，因此在签名前明确拒绝。
    raise ValueError("webhook.payload_invalid")


def _is_sensitive_webhook_key(normalized_key: str) -> bool:
    """判断规范化字段名是否可能承载密钥、Token、密码或请求/响应正文。

    exact 集合覆盖常见正文键；后缀规则覆盖 access_token、signing_secret、provider_api_key
    等业务前缀变体，同时不误伤 input_tokens、provider_key_id 和 secret_version 这类安全元数据。
    """

    if normalized_key in WEBHOOK_SENSITIVE_PAYLOAD_KEYS:
        return True
    compact_key = normalized_key.replace("_", "")
    # camelCase 在 lower() 后会丢失词边界；同时检查紧凑后缀，覆盖 tokenHash、
    # providerTokenHash、acceptToken 和 signingSecret，而不误伤 inputTokens、
    # providerKeyId 或 secretVersion 这些安全元数据。
    if compact_key.endswith(("tokenhash", "token", "secret", "password", "apikey")):
        return True
    # 后缀规则覆盖业务前缀变体，但刻意不拒绝 provider_key_id、input_tokens 和 secret_version。
    return normalized_key.endswith(("_token", "_secret", "_password", "_api_key"))


def _validate_persistent_event_id(event_id: str | None) -> None:
    """校验由数据库事件事实派生的稳定 Webhook ID。

    参数：快照或 Dry-Run 服务显式传入的持久事件 ID；返回：合法时无返回值。
    约束：不能回退到随机 UUID，否则提交后进程退出再派发会产生不同幂等事实。
    """

    if (
        not isinstance(event_id, str)
        or not event_id
        or len(event_id) > 128
        or any(character.isspace() or ord(character) < 32 for character in event_id)
    ):
        # 持久事件 ID 不合法时必须失败关闭，不能生成随机 ID 掩盖幂等事实损坏。
        raise ValueError("webhook.payload_invalid")


def _validate_response_cache_hit_payload(payload: dict[str, Any]) -> None:
    """校验 ``cache.hit`` 的 exact 字段、四组成本证据和收费等式。

    source generation 只描述历史成本；saved 必须由本次 current-reference 与 billed 的整数
    微元相减得到。任何 unknown 字段、指数金额或把 unavailable 冒充 0 都失败关闭。
    """

    expected = {
        "schema_version",
        "cache_hit_id",
        "cache_policy_id",
        "cache_policy_version",
        "charge_cache_hits",
        "source_generation_cost_status",
        "source_generation_cost_cny",
        "current_reference_cost_status",
        "current_reference_cost_cny",
        "saved_cost_status",
        "saved_cost_cny",
        "billed_cost_status",
        "billed_cost_cny",
    }
    # exact-set 防止未来 owner、digest、正文或调试字段未经审计进入客户订阅事件。
    if set(payload) != expected:
        raise ValueError("webhook.payload_invalid")
    # schema、领域 ID、policy version 与收费标志必须使用精确 wire 类型并保持有界。
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not _is_prefixed_lower_hex_id(payload["cache_hit_id"], prefix="rch_")
        or not _is_prefixed_lower_hex_id(payload["cache_policy_id"], prefix="rcp_")
        or type(payload["cache_policy_version"]) is not int
        or not 1 <= payload["cache_policy_version"] <= 2_147_483_647
        or type(payload["charge_cache_hits"]) is not bool
    ):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise ValueError("webhook.payload_invalid")

    source = _response_cache_amount_micros(payload, "source_generation_cost")
    current = _response_cache_amount_micros(payload, "current_reference_cost")
    saved = _response_cache_amount_micros(payload, "saved_cost")
    billed = _response_cache_amount_micros(payload, "billed_cost")
    # source 独立于本次收费等式，但调用 helper 可证明它不是 not_applicable 或伪造零值。
    del source
    expected_saved = None if current is None or billed is None else current - billed
    # 负节省说明 reference/billed 价格 owner 错绑，不能裁剪为零后继续发送。
    if expected_saved is not None and expected_saved < 0:
        raise ValueError("webhook.payload_invalid")
    # saved 必须由同一次结算的整数微元现算，不能相信数据库或调用方自报值。
    if saved != expected_saved:
        raise ValueError("webhook.payload_invalid")
    # 收费开关决定 billed 证据矩阵，两种模式均不能省略正式 Usage/FinOps 事实。
    if payload["charge_cache_hits"]:
        # 收费 hit 必须复用同一 reference snapshot；两者状态和值均必须逐字相等。
        if (
            payload["current_reference_cost_status"]
            != payload["billed_cost_status"]
            or current != billed
        ):
            # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
            raise ValueError("webhook.payload_invalid")
    # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
    elif billed != 0 or payload["billed_cost_status"] != "known":
        # 免费 hit 仍有正式零价账务事实；unavailable/null 不能伪装成“未收费”。
        raise ValueError("webhook.payload_invalid")


def _response_cache_amount_micros(
    payload: dict[str, Any],
    prefix: str,
) -> int | None:
    """把缓存 fixed-six CNY 文本严格还原为非负 signed-64 微元并验证证据状态。"""

    status = payload[f"{prefix}_status"]
    value = payload[f"{prefix}_cny"]
    # unavailable 只能与 null 配对，绝不能把缺价转换为可计费的零元事实。
    if status == "unavailable" and value is None:
        return None
    # known 必须携带普通十进制字符串，其他状态不属于 cache.hit Webhook。
    if status != "known" or type(value) is not str:
        raise ValueError("webhook.payload_invalid")
    integer, separator, fraction = value.partition(".")
    # 金额拒绝指数、符号、前导零和非六位小数，确保跨语言解析只有一种结果。
    if (
        separator != "."
        or not integer
        or not integer.isascii()
        or not integer.isdecimal()
        or (len(integer) > 1 and integer.startswith("0"))
        or len(fraction) != 6
        or not fraction.isascii()
        or not fraction.isdecimal()
    ):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise ValueError("webhook.payload_invalid")
    micros = int(integer) * 1_000_000 + int(fraction)
    # 超出 SQLite signed-64 的金额无法与持久 hit owner 对照，必须在签名前拒绝。
    if micros > 9_223_372_036_854_775_807:
        raise ValueError("webhook.payload_invalid")
    return micros


def _validate_response_cache_invalidated_payload(payload: dict[str, Any]) -> None:
    """校验 ``cache.invalidated`` 的 exact 范围、reason 和有界安全计数。"""

    expected = {
        "schema_version",
        "cache_invalidation_id",
        "scope",
        "match_mode",
        "tag_count",
        "matched_entry_count",
        "deleted_entry_count",
        "fenced_fill_count",
        "reason_code",
    }
    # exact-set 明确禁止 selector/tag value、Cache Key 或其他内部失效材料进入事件。
    if set(payload) != expected:
        raise ValueError("webhook.payload_invalid")
    # 对象、scope、match mode 与 reason 必须与迁移 CHECK 的固定集合完全一致。
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not _is_prefixed_lower_hex_id(
            payload["cache_invalidation_id"], prefix="rci_"
        )
        or payload["scope"] not in {"all", "tags"}
        or payload["match_mode"] != "all"
        or payload["reason_code"]
        not in {
            "manual_finops",
            "manual_security",
            "manual_configuration",
            "manual_other",
            "policy_changed",
            "system_corrupt",
            "system_output_blocked",
        }
    ):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise ValueError("webhook.payload_invalid")
    count_fields = (
        "tag_count",
        "matched_entry_count",
        "deleted_entry_count",
        "fenced_fill_count",
    )
    # 所有计数拒绝 bool、负数和 SQLite 范围外整数，避免 wire/数据库解释分叉。
    if any(
        type(payload[field]) is not int
        or not 0 <= payload[field] <= 9_223_372_036_854_775_807
        for field in count_fields
    ):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise ValueError("webhook.payload_invalid")
    # all/tags 的 tag_count 互斥关系和 deleted<=matched 必须可由持久 owner 重建。
    if (
        payload["tag_count"] > 16
        or (payload["scope"] == "all" and payload["tag_count"] != 0)
        or (payload["scope"] == "tags" and payload["tag_count"] < 1)
        or payload["deleted_entry_count"] > payload["matched_entry_count"]
    ):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise ValueError("webhook.payload_invalid")


def _validate_response_cache_envelope(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把缓存事件绑定到持久 rcev_ source owner、租户、操作者和操作链路。"""

    object_field = "cache_hit_id" if event_type == "cache.hit" else "cache_invalidation_id"
    # 信封必须逐字绑定 source event、租户、对象与操作链路，不能回退随机 event ID。
    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="rcev_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or object_id != payload[object_field]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=128)
        or occurred_at.tzinfo is None
    ):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise ValueError("webhook.payload_invalid")
    # hit 是 Gateway 系统事实且 actor 固定为空；用户身份不能冒充运行时事件来源。
    if event_type == "cache.hit":
        if actor_id is not None:
            # cache hit 是 Gateway 运行时事实，不能把 Virtual Key 或请求用户伪装为 actor。
            raise ValueError("webhook.payload_invalid")
        return
    system_reason = payload["reason_code"] in {
        "system_corrupt",
        "system_output_blocked",
    }
    if (system_reason and actor_id is not None) or (
        not system_reason and not _is_bounded_safe_text(actor_id, maximum=160)
    ):
        # 手动/Policy 变化保留可信 Session actor；系统安全淘汰只能使用 null actor。
        raise ValueError("webhook.payload_invalid")


def build_assistant_queried_event(
    *,
    tenant_id: str,
    actor_id: str,
    assistant_query_id: str,
    event_id: str,
    trace_id: str,
    source_request_id: str,
    query_type: str,
    outcome: str,
    claim_count: int,
    source_count: int,
    redaction_count: int,
    duration_ms: int,
    timestamp: datetime,
) -> WebhookEvent:
    """从持久 AssistantAudit 安全摘要构造 stable ``assistant.queried`` 事件。

    duration bucket 由整数耗时派生，调用方不能自报另一个边界；问题、回答、目标请求和
    错误正文均不属于参数或 payload。
    """

    if (
        type(duration_ms) is not int
        or duration_ms < 0
        or duration_ms > 9_223_372_036_854_775_807
    ):
        # 耗时必须是有符号六十四位非负整数，bool 与越界值不能进入事件摘要。
        raise ValueError("webhook.payload_invalid")
    # 1.5 秒是整个 endpoint 的绝对期限；超时失败仍落入第二个固定 bucket 供容量复核。
    duration_bucket = "under_1500ms" if duration_ms < 1500 else "at_least_1500ms"
    return build_webhook_event(
        tenant_id=tenant_id,
        event_type="assistant.queried",
        payload={
            "schema_version": "assistant-queried-v1",
            "query_type": query_type,
            "outcome": outcome,
            "claim_count": claim_count,
            "source_count": source_count,
            "redaction_count": redaction_count,
            "duration_bucket": duration_bucket,
        },
        actor_id=actor_id,
        object_id=assistant_query_id,
        trace_id=trace_id,
        source_request_id=source_request_id,
        event_id=event_id,
        timestamp=timestamp,
    )


def _validate_admin_assistant_queried_payload(payload: dict[str, Any]) -> None:
    """校验管理助手事件的 exact 七字段、状态矩阵与固定脱敏计数。"""

    expected = {
        "schema_version",
        "query_type",
        "outcome",
        "claim_count",
        "source_count",
        "redaction_count",
        "duration_bucket",
    }
    if set(payload) != expected:
        # exact-set 阻止问题、回答、target request、金额或未来调试字段被签名外发。
        raise ValueError("webhook.payload_invalid")
    query_type = payload["query_type"]
    outcome = payload["outcome"]
    duration_bucket = payload["duration_bucket"]
    if (
        type(payload["schema_version"]) is not str
        or payload["schema_version"] != "assistant-queried-v1"
        or type(query_type) is not str
        or query_type
        not in {
            "usage_statistics",
            "error_explanation",
            "cost_advice",
            "policy_explanation",
            "unknown",
        }
        or type(outcome) is not str
        or outcome
        not in {
            "answered",
            "read_only_blocked",
            "unsupported",
            "ambiguous",
            "permission_denied",
            "rate_limited",
            "failed",
        }
        or type(duration_bucket) is not str
        or duration_bucket not in {"under_1500ms", "at_least_1500ms"}
    ):
        # 版本、意图、终态和耗时桶任一漂移都会破坏订阅方可验证合同。
        raise ValueError("webhook.payload_invalid")
    counts = (
        payload["claim_count"],
        payload["source_count"],
        payload["redaction_count"],
    )
    if any(
        type(value) is not int or not 0 <= value <= 9_223_372_036_854_775_807
        for value in counts
    ):
        # bool 是 int 子类，必须用 exact type 拒绝，避免 true 被下游解释成计数一。
        raise ValueError("webhook.payload_invalid")
    if outcome == "answered":
        expected_redactions = 3 if query_type == "error_explanation" else 2
        if (
            query_type == "unknown"
            or payload["claim_count"] < 1
            or payload["source_count"] < 1
            or payload["redaction_count"] != expected_redactions
        ):
            # answered 必须有事实、来源及匹配意图的脱敏数，不能发布空壳成功事件。
            raise ValueError("webhook.payload_invalid")
        return
    # 没有公开回答的终态不得伪造 claim/source/redaction 计数来暗示受保护事实存在。
    if any(value != 0 for value in counts):
        raise ValueError("webhook.payload_invalid")


def _validate_admin_assistant_queried_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把公开摘要绑定到同租户 AssistantQuery、可信 actor 和原始操作链路。"""

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="asevt_")
        or not _is_prefixed_lower_hex_id(object_id, prefix="asq_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or not _is_bounded_safe_text(actor_id, maximum=160)
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=160)
        or type(occurred_at) is not datetime
        or occurred_at.tzinfo is None
    ):
        # stable event/object 与可信 owner 任一缺失都失败关闭，重试 worker 不生成替代身份。
        raise ValueError("webhook.payload_invalid")


def _validate_approval_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    """校验移动审批卡片的 exact v1 安全摘要。

    目标名称、金额、会话摘要、审批备注和任何凭据都不属于此合同；目标类型与动作也必须
    命中服务端冻结 registry，不能把事件通道扩成任意命令载体。
    """

    common = {
        "schema_version",
        "approval_id",
        "status",
        "target_type",
        "target_action",
    }
    if event_type == "approval.created":
        expected = common | {"risk_level", "requires_step_up", "expires_at", "entry"}
    elif event_type == "approval.timeout":
        expected = common | {"resolution_reason", "decided_at"}
    else:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ValueError("webhook.payload_invalid")
    if set(payload) != expected:
        # exact-set 防止自由备注、目标正文和未来未经审计字段进入签名卡片。
        raise ValueError("webhook.payload_invalid")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not _is_prefixed_lower_hex_id(payload["approval_id"], prefix="apr_")
    ):
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ValueError("webhook.payload_invalid")

    target_type = payload["target_type"]
    target_action = payload["target_action"]
    target_actions = {
        "budget_block_release": {"release"},
        "mcp_tool_invocation": {"invoke"},
        "session_risk_action": {
            "freeze_session",
            "freeze_virtual_key",
            "dismiss",
            "release_session",
            "release_virtual_key",
        },
    }
    if (
        type(target_type) is not str
        or target_type not in target_actions
        or type(target_action) is not str
        or target_action not in target_actions[target_type]
    ):
        # 事件绝不能承载 URL、函数名或 registry 外的动态动作。
        raise ValueError("webhook.payload_invalid")

    if event_type == "approval.created":
        expected_risk = "standard" if target_type == "budget_block_release" else "high"
        if (
            payload["status"] != "pending"
            or payload["risk_level"] != expected_risk
            or type(payload["requires_step_up"]) is not bool
            or payload["requires_step_up"] != (expected_risk == "high")
            or payload["entry"] != "approval-card"
            or not _is_canonical_utc_second(payload["expires_at"])
        ):
            # 风险等级由 registry 固定派生，调用方不能把高风险会话降级为免验证卡片。
            raise ValueError("webhook.payload_invalid")
        return

    if (
        payload["status"] != "rejected"
        or payload["resolution_reason"] != "timeout"
        or not _is_canonical_utc_second(payload["decided_at"])
    ):
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ValueError("webhook.payload_invalid")


def _validate_approval_event_envelope(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把审批卡片绑定到同租户持久 source event 与可信操作者。"""

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="apevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or object_id != payload["approval_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=128)
        or type(occurred_at) is not datetime
        or occurred_at.tzinfo is None
    ):
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ValueError("webhook.payload_invalid")
    if event_type == "approval.created":
        if not _is_bounded_safe_text(actor_id, maximum=160):
            # 创建者是经过内部身份校验的服务或用户；匿名 source 不能生成通知。
            raise ValueError("webhook.payload_invalid")
        return
    if (
        actor_id != "approval-timeout-scheduler"
        or _to_iso(occurred_at) != payload["decided_at"]
    ):
        # timeout 只能由固定系统 actor 产生，且信封时间必须与终态事实同秒。
        raise ValueError("webhook.payload_invalid")


def _validate_quota_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    """校验额度耗尽和试用到期的 exact v1 安全摘要。"""

    if event_type == "quota.exhausted":
        expected = {
            "schema_version",
            "plan_id",
            "quota_generation",
            "required_cny",
            "available_cny",
            "reason_code",
            "effective_tier",
        }
    elif event_type == "trial.expired":
        expected = {
            "schema_version",
            "plan_id",
            "trial_cycle",
            "expired_at",
            "effective_tier",
        }
    else:
        # 额度校验器只处理固定事件类型，未知类型不能共享宽松载荷合同。
        raise ValueError("webhook.payload_invalid")
    # 载荷必须逐字段等于冻结合同，并绑定规范 Plan 标识与版本。
    if (
        set(payload) != expected
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not _is_prefixed_lower_hex_id(payload["plan_id"], prefix="pln_")
    ):
        # 字段、版本或 Plan 标识不符合冻结合同，应整条拒绝且不回显载荷。
        raise ValueError("webhook.payload_invalid")

    if event_type == "quota.exhausted":
        required = payload["required_cny"]
        available = payload["available_cny"]
        # 耗尽金额、代际、原因和生效层级均为安全摘要，不允许任何格式漂移。
        if (
            type(payload["quota_generation"]) is not int
            or payload["quota_generation"] < 1
            or not _is_finops_money_text(required)
            or not _is_finops_money_text(available)
            or required == "0.000000"
            or int(available.replace(".", "")) > int(required.replace(".", ""))
            or payload["reason_code"] != "quota_exhausted"
            or payload["effective_tier"] not in {"trial", "free", "paid"}
        ):
            # 耗尽摘要任一金额或枚举漂移都必须拒绝，禁止下游错误归因。
            raise ValueError("webhook.payload_invalid")
        return

    # 试用到期必须使用固定周期、规范时间和受限生效层级。
    if (
        payload["trial_cycle"] != 1
        or type(payload["trial_cycle"]) is not int
        or not _is_canonical_utc_second(payload["expired_at"])
        or payload["effective_tier"] not in {"free", "paid"}
    ):
        # 到期周期、时间或层级异常时拒绝事件，避免伪造试用终态。
        raise ValueError("webhook.payload_invalid")


def _validate_quota_event_envelope(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把额度事件绑定到同租户 Plan、固定系统 actor 和原始请求链路。"""

    # 事件信封必须绑定租户、Plan、请求链路和带时区发生时间。
    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="qevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or object_id != payload["plan_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=128)
        or type(occurred_at) is not datetime
        or occurred_at.tzinfo is None
    ):
        # 信封 owner 或链路坐标不完整时禁止发布，防止跨租户事件混淆。
        raise ValueError("webhook.payload_invalid")
    if event_type == "quota.exhausted":
        # 耗尽事件只能由额度准入系统身份发布，禁止客户端伪造 actor。
        if actor_id != "quota-admission":
            raise ValueError("webhook.payload_invalid")
        return
    # 试用到期仅允许两条系统路径，并要求信封时间与载荷完全一致。
    if (
        actor_id not in {"quota-trial-scheduler", "quota-expiry-guard"}
        or _to_iso(occurred_at) != payload["expired_at"]
    ):
        # 系统身份或发生时间不匹配时拒绝发布，载荷不能自行声明终态。
        raise ValueError("webhook.payload_invalid")


def _validate_billing_dispute_created_payload(payload: dict[str, Any]) -> None:
    """校验账单争议创建事件的 exact v1 安全摘要。"""

    expected = {
        "schema_version",
        "billing_dispute_id",
        "scope_type",
        "item_count",
        "currency_totals",
        "status",
        "created_at",
    }
    if (
        set(payload) != expected
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not _is_prefixed_lower_hex_id(payload["billing_dispute_id"], prefix="bdsp_")
        or payload["scope_type"] not in {"request_ids", "time_range"}
        or type(payload["item_count"]) is not int
        or not 1 <= payload["item_count"] <= 100
        or payload["status"] != "submitted"
        or not _is_canonical_utc_second(payload["created_at"])
    ):
        # 字段集合、版本、资源身份或冻结状态任一漂移都拒绝，不能宽松转发未知字段。
        raise ValueError("webhook.payload_invalid")

    totals = payload["currency_totals"]
    if (
        type(totals) is not dict
        or not 1 <= len(totals) <= 2
        or not set(totals) <= {"CNY", "USD"}
        or any(
            not _is_billing_dispute_aggregate_money_text(
                value,
                item_count=payload["item_count"],
            )
            for value in totals.values()
        )
    ):
        # 不同币种必须分组且只使用固定六位正金额，禁止 float 或伪造总计键。
        raise ValueError("webhook.payload_invalid")


def _validate_billing_dispute_created_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把创建摘要绑定到持久事件、申请人和原始事务链路。"""

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="bdevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or not _is_bounded_safe_text(actor_id, maximum=160)
        or object_id != payload["billing_dispute_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=128)
        or type(occurred_at) is not datetime
        or occurred_at.tzinfo is None
        or _to_iso(occurred_at) != payload["created_at"]
    ):
        # lease worker 或恢复任务不得替换申请人、request/trace、object 或发生时刻。
        raise ValueError("webhook.payload_invalid")


def _validate_batch_job_event_payload(
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """校验批任务 Webhook 公共载荷的固定字段、状态和成本摘要，不接受业务正文。

    Validate the exact public summary for a persisted Batch lifecycle event.
    """

    common = {"schema_version", "batch_job_id"}
    if event_type == "batch_job.created":
        expected = common | {
            "operation",
            "priority",
            "task_count",
            "estimated_cost_cny",
        }
    else:
        expected = common | {
            "status",
            "succeeded_count",
            "failed_count",
            "cancelled_count",
            "actual_cost_cny",
            "possible_cost_cny",
        }
    if set(payload) != expected:
        # Webhook 载荷必须满足固定字段和类型边界；异常数据不得进入投递链路。
        raise ValueError("webhook.payload_invalid")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not _is_prefixed_lower_hex_id(payload["batch_job_id"], prefix="bjob_")
    ):
        # Webhook 载荷必须满足固定字段和类型边界；异常数据不得进入投递链路。
        raise ValueError("webhook.payload_invalid")
    if event_type == "batch_job.created":
        if (
            payload["operation"] not in {"summarize", "translate", "classify"}
            or payload["priority"] not in {"low", "normal", "high"}
            or type(payload["task_count"]) is not int
            or not 1 <= payload["task_count"] <= 100
            or not _is_finops_money_text(payload["estimated_cost_cny"])
        ):
            # Webhook 载荷必须满足固定字段和类型边界；异常数据不得进入投递链路。
            raise ValueError("webhook.payload_invalid")
        return
    counts = (
        payload["succeeded_count"],
        payload["failed_count"],
        payload["cancelled_count"],
    )
    if (
        payload["status"]
        not in {"completed", "partial_failed", "failed", "cancelled", "expired"}
        or any(type(value) is not int or not 0 <= value <= 100 for value in counts)
        or sum(counts) > 100
        or not _is_finops_money_text(payload["actual_cost_cny"])
        or not _is_finops_money_text(payload["possible_cost_cny"])
    ):
        # Webhook 载荷必须满足固定字段和类型边界；异常数据不得进入投递链路。
        raise ValueError("webhook.payload_invalid")


def _validate_batch_job_event_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """校验批任务 Webhook 信封的事件类型、租户所有者和载荷一致性。

    Bind a Batch summary to its immutable source event and tenant-qualified Job.
    """

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="bjev_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or not _is_bounded_safe_text(actor_id, maximum=160)
        or object_id != payload["batch_job_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=128)
        or occurred_at.tzinfo is None
    ):
        # Webhook 载荷必须满足固定字段和类型边界；异常数据不得进入投递链路。
        raise ValueError("webhook.payload_invalid")


def _validate_experiment_completed_payload(payload: dict[str, Any]) -> None:
    """校验实验完成 Webhook 的聚合结果边界，拒绝样本正文和未知字段。

    校验 ``experiment.completed`` 的精确安全结果摘要。
    """

    expected = {
        "schema_version",
        "experiment_id",
        "project_id",
        "environment_id",
        "model_alias_id",
        "model_a_id",
        "model_b_id",
        "status",
        "completion_reason",
        "assigned_count",
        "model_a_assigned_count",
        "model_b_assigned_count",
        "gateway_success_count",
        "gateway_failure_count",
        "unresolved_count",
        "cost_unknown_count",
        "feedback_count",
        "facts_version",
        "facts_through_at",
    }
    if set(payload) != expected:
        # exact-set 防止名称、请求 owner、HMAC 指纹或 Provider 配置混入订阅事件。
        raise ValueError("webhook.payload_invalid")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not _is_prefixed_lower_hex_id(payload["experiment_id"], prefix="exp_")
        or any(
            not _is_bounded_safe_text(payload[field], maximum=128)
            for field in (
                "project_id",
                "environment_id",
                "model_alias_id",
                "model_a_id",
                "model_b_id",
            )
        )
        or payload["model_a_id"] == payload["model_b_id"]
        or payload["status"] != "completed"
        or payload["completion_reason"]
        not in {"target_reached", "sampling_deadline_reached"}
        or not _is_canonical_utc_second(payload["facts_through_at"])
    ):
        # Webhook 载荷必须满足固定字段和类型边界；异常数据不得进入投递链路。
        raise ValueError("webhook.payload_invalid")

    count_fields = (
        "assigned_count",
        "model_a_assigned_count",
        "model_b_assigned_count",
        "gateway_success_count",
        "gateway_failure_count",
        "unresolved_count",
        "cost_unknown_count",
        "feedback_count",
    )
    if any(
        type(payload[field]) is not int or not 0 <= payload[field] <= 100_000
        for field in count_fields
    ):
        # Webhook 载荷必须满足固定字段和类型边界；异常数据不得进入投递链路。
        raise ValueError("webhook.payload_invalid")
    assigned = payload["assigned_count"]
    if (
        payload["model_a_assigned_count"] + payload["model_b_assigned_count"]
        != assigned
        or payload["gateway_success_count"]
        + payload["gateway_failure_count"]
        + payload["unresolved_count"]
        != assigned
        or payload["cost_unknown_count"] > assigned
        or payload["feedback_count"] > assigned
        or type(payload["facts_version"]) is not int
        or not 0 <= payload["facts_version"] <= 9_223_372_036_854_775_807
    ):
        # 计数必须能由同一 Experiment 与 A/B Result 精确重建，不能接受近似汇总。
        raise ValueError("webhook.payload_invalid")


def _validate_experiment_completed_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把完成摘要绑定到持久 Experiment source event 与首次创建链路。"""

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="exev_")
        or not _is_bounded_safe_text(tenant_id, maximum=128)
        or not _is_bounded_safe_text(actor_id, maximum=128)
        or object_id != payload["experiment_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=160)
        or occurred_at.tzinfo is None
    ):
        # 重试与死信重放只能恢复原 source owner，不能生成替代 actor 或链路。
        raise ValueError("webhook.payload_invalid")


def _validate_prompt_template_published_payload(payload: dict[str, Any]) -> None:
    """校验 ``prompt_template.published`` 的精确七字段安全摘要。

    参数：已通过通用敏感键递归扫描的 payload。返回值：无。
    关键约束：模板名称、描述、正文、变量 schema/值、变更说明、绑定清单和密文均不在
    合同中；Project/Model 只披露有界计数，防止事件成为管理目录副本。
    """

    expected = {
        "template_id",
        "version_id",
        "version_number",
        "template_revision",
        "project_scope",
        "project_count",
        "recommended_model_count",
    }
    # 发布事件必须是精确七字段安全摘要，未知字段可能携带正文或绑定清单，统一拒绝。
    if set(payload) != expected:
        raise ValueError("webhook.payload_invalid")
    # 模板 ID 只允许冻结前缀和随机小写十六进制，避免事件订阅侧接受伪造资源身份。
    if not _is_prefixed_lower_hex_id(payload["template_id"], prefix="ptpl_"):
        raise ValueError("webhook.payload_invalid")
    # 版本 ID 与对象 owner 绑定，格式异常不能通过字符串兼容或自动修复。
    if not _is_prefixed_lower_hex_id(payload["version_id"], prefix="ptv_"):
        raise ValueError("webhook.payload_invalid")
    version_number = payload["version_number"]
    template_revision = payload["template_revision"]
    project_count = payload["project_count"]
    recommended_model_count = payload["recommended_model_count"]
    if (
        type(version_number) is not int
        or version_number < 1
        or type(template_revision) is not int
        or template_revision < version_number
        or type(project_count) is not int
        or not 0 <= project_count <= 100
        or type(recommended_model_count) is not int
        or not 0 <= recommended_model_count <= 8
    ):
        # 版本/revision/计数是固定整数摘要，拒绝 bool、负值和越界目录规模。
        raise ValueError("webhook.payload_invalid")
    scope = payload["project_scope"]
    # 适用范围只允许冻结枚举，不能把项目标识或任意文本借 scope 发送出去。
    if scope not in {"all", "selected"}:
        raise ValueError("webhook.payload_invalid")
    if (scope == "all" and project_count != 0) or (
        scope == "selected" and project_count < 1
    ):
        # scope 与计数必须一致，避免消费者根据矛盾摘要做出错误治理决策。
        raise ValueError("webhook.payload_invalid")


def _validate_prompt_template_published_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
) -> None:
    """绑定发布 payload 与 ptev_ source event、精确版本对象和原始操作链路。

    参数：已验证 payload 与信封身份字段。返回值：无。关键约束：随机 event ID、缺失 actor、
    另一版本 object、缺失 request/trace 或不安全 tenant 文本均失败关闭，不能自动补默认值。
    """

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="ptev_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or not _is_bounded_safe_text(actor_id, maximum=160)
        or object_id != payload["version_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=128)
    ):
        # source event、tenant、actor、version 与链路 ID 必须精确绑定，缺失时不生成替代身份。
        raise ValueError("webhook.payload_invalid")


def _validate_policy_template_applied_payload(payload: dict[str, Any]) -> None:
    """校验 ``policy_template.applied`` 的精确九字段安全摘要。

    参数：已通过通用敏感键递归扫描的 payload。返回值：无。关键约束：模板定义、
    DSL、绑定值、Policy 条件、价格、Prompt 和 Response 都不能进入订阅事件。
    """

    expected = {
        "template_code",
        "template_version",
        "application_id",
        "generated_policy_id",
        "previous_snapshot_version",
        "result_snapshot_version",
        "impact_hash",
        "bindings_hash",
        "changed_resource_types",
    }
    # 未知或缺失字段都可能绕过迁移的 exact-schema source fact，签名前必须失败关闭。
    if set(payload) != expected:
        raise ValueError("webhook.payload_invalid")
    template_code = payload["template_code"]
    if type(template_code) is not str or template_code not in {
        "low_cost_mode",
        "high_security_mode",
        "domestic_models_only",
        "budget_burst_guard",
        "customer_service_redaction",
    }:
        # 只允许五条内置模板 code，任意显示名称或客户自定义文本均不得代替稳定枚举。
        raise ValueError("webhook.payload_invalid")
    if type(payload["template_version"]) is not int or payload["template_version"] != 1:
        # 模板定义当前只冻结 v1；bool 和字符串不能通过 Python 的隐式整数相等性。
        raise ValueError("webhook.payload_invalid")
    if not _is_prefixed_lower_hex_id(payload["application_id"], prefix="ptapp_"):
        # 应用身份必须是服务端生成的规范 ID，任意文本不能成为事件 owner。
        raise ValueError("webhook.payload_invalid")
    if not _is_prefixed_lower_hex_id(payload["generated_policy_id"], prefix="pol_"):
        # 生成策略身份必须使用规范小写十六进制 ID，避免消费者接受伪造引用。
        raise ValueError("webhook.payload_invalid")
    previous_version = payload["previous_snapshot_version"]
    result_version = payload["result_snapshot_version"]
    if (
        type(previous_version) is not int
        or previous_version < 1
        or type(result_version) is not int
        or result_version != previous_version + 1
    ):
        # apply 必须从真实 published baseline 单调生成下一版，不能接受跳号或虚构 v0。
        raise ValueError("webhook.payload_invalid")
    for field in ("impact_hash", "bindings_hash"):
        if type(payload[field]) is not str or not _is_lower_hex(
            payload[field], expected_length=64
        ):
            # 两个摘要都是不可逆 canonical SHA-256；大小写或长度漂移会破坏消费者幂等判断。
            raise ValueError("webhook.payload_invalid")
    if payload["changed_resource_types"] != ["policy", "policy_snapshot"]:
        # 应用固定只产生 Policy 与 Snapshot；模板正文和跨域资源不能借列表扩散到事件。
        raise ValueError("webhook.payload_invalid")


def _validate_policy_template_applied_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
) -> None:
    """绑定模板应用 payload 与 ptevt_ source event、应用对象和原操作链路。

    参数：已验证 payload 与信封身份字段。返回值：无。关键约束：tenant、actor、
    application、request 和 trace 任一缺失或漂移都失败关闭，不生成替代身份。
    """

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="ptevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or not _is_bounded_safe_text(actor_id, maximum=160)
        or object_id != payload["application_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=128)
    ):
        # 任一信封身份漂移都必须拒绝，不能生成替代事件身份。
        raise ValueError("webhook.payload_invalid")


def _validate_intent_detected_payload(payload: dict[str, Any]) -> None:
    """校验 ``security.intent_detected`` 的精确分类摘要。

    参数：已通过通用敏感键扫描的 payload。返回值：无。关键约束：只允许持久 result
    可复核的枚举、整数分数、版本和时间，不允许正文、命中位置、片段或输入 hash。
    """

    expected = {
        "intent_risk_result_id",
        "source_request_id",
        "source_kind",
        "classification_mode",
        "risk_type",
        "risk_score",
        "risk_score_scale",
        "action",
        "reason_code",
        "detector_version",
        "ruleset_version",
        "input_truncated",
        "completed_at",
    }
    if set(payload) != expected:
        # exact-set 阻止正文、命中证据和未来未知字段被意外签名并发送到租户 Endpoint。
        raise ValueError("webhook.payload_invalid")
    if not _is_prefixed_lower_hex_id(payload["intent_risk_result_id"], prefix="irr_"):
        # 结果 ID 是 object_id 和投递幂等关联的唯一权威，任意文本不能代替。
        raise ValueError("webhook.payload_invalid")
    if not _is_bounded_safe_text(payload["source_request_id"], maximum=128):
        # 请求身份必须是有界服务端链路 ID，禁止控制字符污染日志和下游索引。
        raise ValueError("webhook.payload_invalid")
    source_kind = payload["source_kind"]
    mode = payload["classification_mode"]
    action = payload["action"]
    if (
        type(source_kind) is not str
        or type(mode) is not str
        or type(action) is not str
        or (source_kind, mode, action) not in {
            ("gateway", "sync_strong", "block"),
            ("gateway", "async_light", "observe"),
            ("gateway", "sync_session", "block"),
            ("gateway", "sync_session", "observe"),
            ("manual", "manual_sync", "review"),
        }
    ):
        # 来源、模式和动作共同决定安全语义，不能独立接受后再由消费者猜测。
        raise ValueError("webhook.payload_invalid")
    score = payload["risk_score"]
    if type(score) is not int or not 1 <= score <= 10_000:
        # bool 不能借 Python 整数子类关系成为风险分数，量纲由独立固定字段冻结。
        raise ValueError("webhook.payload_invalid")
    if (
        payload["risk_score_scale"] != 10_000
        or type(payload["risk_score_scale"]) is not int
    ):
        # 禁止 float 或另一量纲导致下游错误比较。
        raise ValueError("webhook.payload_invalid")
    # reason、taxonomy、分数和强弱属性属于当前 ruleset 的一个不可拆分事实；只分别
    # 校验枚举会允许生产者伪造不存在的组合，导致订阅端把弱观察误当同步阻断。
    reason_contracts = {
        # 完整轻分类在安全培训等良性语境下固定降 2500 分；这里逐项列出原始值和
        # 唯一允许的降分值，既接受分类器真实产物，也不把校验放宽成任意分数区间。
        "explicit_credential_extraction": (
            "credential_exfiltration",
            frozenset({9900, 7400}),
            True,
        ),
        "explicit_system_prompt_request": (
            "system_prompt_extraction",
            frozenset({9800, 7300}),
            True,
        ),
        "explicit_policy_bypass": (
            "policy_bypass",
            frozenset({9700, 7200}),
            True,
        ),
        "explicit_instruction_override": (
            "jailbreak",
            frozenset({9600, 7100}),
            True,
        ),
        "credential_access_request": (
            "credential_exfiltration",
            frozenset({7600, 5100}),
            False,
        ),
        "system_prompt_access_request": (
            "system_prompt_extraction",
            frozenset({7400, 4900}),
            False,
        ),
    }
    reason_code = payload["reason_code"]
    reason_contract = (
        reason_contracts.get(reason_code) if type(reason_code) is str else None
    )
    if (
        reason_contract is None
        or payload["risk_type"] != reason_contract[0]
        or score not in reason_contract[1]
        or (
            (mode == "sync_strong" or (mode == "sync_session" and action == "block"))
            and not reason_contract[2]
        )
    ):
        # 同步阻断只能引用强规则；所有模式都只能使用冻结 ruleset 可产生的精确分数。
        raise ValueError("webhook.payload_invalid")
    if (
        payload["detector_version"] != "intent-rule-engine-v1"
        or payload["ruleset_version"] != "intent-rules-2026-08-03-v1"
        or not _is_canonical_utc_second(payload["completed_at"])
    ):
        # Webhook v1 必须绑定实际执行版本和规范 UTC 秒，未知版本需要显式升级合同。
        raise ValueError("webhook.payload_invalid")
    if type(payload["input_truncated"]) is not bool:
        # Webhook JSON 使用显式布尔，拒绝 SQLite 整数或字符串产生跨语言歧义。
        raise ValueError("webhook.payload_invalid")


def _validate_intent_detected_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """绑定敏感意图 payload 与不可变 source event、result、actor 和原请求链路。"""

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="irevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or not _is_bounded_safe_text(actor_id, maximum=160)
        or object_id != payload["intent_risk_result_id"]
        or source_request_id != payload["source_request_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or occurred_at.tzinfo is None
        or _to_iso(occurred_at) != payload["completed_at"]
    ):
        # 任一 owner、链路或完成时刻漂移都失败关闭，不能生成替代身份掩盖损坏 source row。
        raise ValueError("webhook.payload_invalid")


def _validate_output_blocked_payload(payload: dict[str, Any]) -> None:
    """校验 ``security.output_blocked`` 的精确、无正文安全摘要。

    参数：已通过通用敏感键递归检查的 payload。返回值：无。
    关键约束：只允许 blocked scan 的稳定 ID、计数、版本和结算意图；模型输出、命中片段、
    pattern、规则名称以及依赖正式 charge 的冲正资格均不属于事件合同。
    """

    expected = {
        "schema_version",
        "output_safety_scan_id",
        "request_id",
        "scan_mode",
        "scan_status",
        "decision",
        "error_code",
        "exposure_state",
        "emitted_token_count",
        "checkpoint_count",
        "dlp_rule_id",
        "detector_version",
        "billing_disposition",
        "occurred_at",
    }
    if set(payload) != expected:
        # exact-set 防止未来字段或调试正文在未审计的情况下进入租户 Webhook。
        raise ValueError("webhook.payload_invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("webhook.payload_invalid")
    if not _is_prefixed_lower_hex_id(
        payload["output_safety_scan_id"], prefix="oss_"
    ) or not _is_prefixed_lower_hex_id(payload["dlp_rule_id"], prefix="dlp_"):
        # scan/rule 只能引用已提交领域对象，任意文本不能成为下游治理身份。
        raise ValueError("webhook.payload_invalid")
    if not _is_bounded_safe_text(payload["request_id"], maximum=128):
        raise ValueError("webhook.payload_invalid")
    if (
        payload["scan_mode"] not in {"non_stream_full", "stream_window"}
        or payload["scan_status"] != "completed"
        or payload["decision"] != "blocked"
        or payload["error_code"] != "content_policy_violation"
        or payload["billing_disposition"]
        not in {"dispute_pending", "not_billable_response_cache_candidate"}
        or payload["detector_version"] != "output-safety-dlp-v1"
    ):
        # 该事件只表达已完成的 block；matched/unavailable 不能冒充对外阻断事实。
        raise ValueError("webhook.payload_invalid")
    exposure_state = payload["exposure_state"]
    emitted = payload["emitted_token_count"]
    checkpoints = payload["checkpoint_count"]
    if (
        exposure_state not in {"none", "partial"}
        or type(emitted) is not int
        or emitted < 0
        or type(checkpoints) is not int
        or checkpoints < 1
        or (exposure_state == "none" and emitted != 0)
        or (exposure_state == "partial" and emitted < 1)
    ):
        # 暴露状态与已放行 Token 必须相互证明，bool 也不能借 int 子类关系通过。
        raise ValueError("webhook.payload_invalid")
    if payload["billing_disposition"] == "not_billable_response_cache_candidate" and (
        payload["scan_mode"] != "non_stream_full"
        or exposure_state != "none"
        or emitted != 0
    ):
        # 只有未调用 Provider、未向客户端暴露任何内容的 cache candidate 才能标记不计费；
        # 流式或 partial 事实仍必须沿用真实 Provider 成本争议路径。
        raise ValueError("webhook.payload_invalid")
    if not _is_canonical_utc_second(payload["occurred_at"]):
        raise ValueError("webhook.payload_invalid")


def _validate_output_blocked_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把输出阻断 payload 绑定到同租户 source event、scan 和原请求链路。"""

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="osevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or actor_id is not None
        or object_id != payload["output_safety_scan_id"]
        or source_request_id != payload["request_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or occurred_at.tzinfo is None
        or _to_iso(occurred_at) != payload["occurred_at"]
    ):
        # 自动事件没有用户 actor；任一 owner、链路或时间漂移均拒绝生成替代事件。
        raise ValueError("webhook.payload_invalid")


def _validate_file_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    """校验文件上传/扫描事件的 exact-key 安全摘要。

    参数：canonical 文件事件类型和已通过通用敏感键扫描的 payload。返回值：无。
    关键约束：事件不包含文件名、正文、对象键、指纹、规则 ID 或命中片段；blocked/failed
    才能携带固定错误码，成功事件不能用空错误字段制造另一种 wire 形状。
    """

    uploaded_fields = {
        "file_id",
        "status",
        "media_type",
        "size_bytes",
        "virus_scan_status",
        "created_at",
    }
    scanned_fields = {
        "file_id",
        "status",
        "media_type",
        "size_bytes",
        "virus_scan_status",
        "dlp_scan_status",
        "dlp_action",
        "match_count",
        "estimated_tokens",
        "extractor_version",
        "completed_at",
        "error_code",
    }
    if event_type == "file.uploaded":
        if set(payload) != uploaded_fields or payload.get("status") != "processing":
            # uploaded 只能证明 owner 已创建，额外字段或终态会提前泄露扫描结果。
            raise ValueError("webhook.payload_invalid")
        event_time = payload.get("created_at")
    elif event_type == "file.scanned":
        status = payload.get("status")
        if set(payload) != scanned_fields or status not in ("ready", "blocked", "failed"):
            # scanned 必须是唯一终态 exact wire，processing 或字段漂移都不可签名外发。
            raise ValueError("webhook.payload_invalid")
        _validate_file_scanned_decision(payload)
        event_time = payload.get("completed_at")
    else:
        # 此 helper 只接受两个冻结事件类型，未来新增类型必须显式设计字段集合。
        raise ValueError("webhook.payload_invalid")

    media_type = payload.get("media_type")
    size_bytes = payload.get("size_bytes")
    if (
        not _is_prefixed_lower_hex_id(payload.get("file_id"), prefix="file_")
        or media_type
        not in (
            "text/plain",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        or type(size_bytes) is not int
        or not 1 <= size_bytes <= 50 * 1024 * 1024
        or payload.get("virus_scan_status") != "not_configured"
        or not _is_canonical_utc_second(event_time)
    ):
        # bool 不能冒充文件字节数；无病毒引擎时状态必须诚实保持 not_configured。
        raise ValueError("webhook.payload_invalid")


def _validate_file_scanned_decision(payload: dict[str, Any]) -> None:
    """校验终态文件事件的 DLP、Token、解析器和错误码组合。

    该校验只使用可公开的枚举与计数，不接触文件正文或 DLP 规则内容。成功、阻断和失败
    三类状态必须保持可区分，避免订阅方把失败文件误当已通过扫描。
    """

    status = payload["status"]
    dlp_status = payload["dlp_scan_status"]
    dlp_action = payload["dlp_action"]
    match_count = payload["match_count"]
    estimated_tokens = payload["estimated_tokens"]
    extractor_by_media_type = {
        "text/plain": "txt-v1",
        "application/pdf": "pdf-v1",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx-v1",
    }
    if (
        type(payload["media_type"]) is not str
        or type(match_count) is not int
        or not 0 <= match_count <= 100
        or payload["extractor_version"]
        != extractor_by_media_type.get(payload["media_type"])
        or (
            estimated_tokens is not None
            and (type(estimated_tokens) is not int or estimated_tokens < 1)
        )
    ):
        # bool/负计数、媒体与解析器漂移或零 Token 都会让订阅方误判处理事实。
        raise ValueError("webhook.payload_invalid")

    dlp_contract_valid = (
        (dlp_status == "clean" and dlp_action is None and match_count == 0)
        or (
            dlp_status == "matched"
            and dlp_action in ("allow", "log_only")
            and match_count >= 1
        )
        or (dlp_status == "blocked" and dlp_action == "block" and match_count >= 1)
        or (
            dlp_status in ("pending", "unavailable")
            and dlp_action is None
            and match_count == 0
        )
    )
    if not dlp_contract_valid:
        # DLP 状态、动作与命中数必须成组自洽，不能由调用方自由拼装。
        raise ValueError("webhook.payload_invalid")

    if status == "ready":
        if (
            dlp_status not in ("clean", "matched")
            or estimated_tokens is None
            or payload["error_code"] is not None
        ):
            # ready 必须已经完成可解释扫描和 Token 估算，且不能夹带失败码。
            raise ValueError("webhook.payload_invalid")
        return
    if status == "blocked":
        if (
            dlp_status != "blocked"
            or estimated_tokens is None
            or payload.get("error_code") != "file.dlp_blocked"
        ):
            # blocked 只接受 DLP 阻断终态，不能用其他失败码冒充策略决策。
            raise ValueError("webhook.payload_invalid")
        return

    allowed_failure_codes = {
        "file.structure_invalid",
        "file.no_extractable_text",
        "file.extract_limit_exceeded",
        "file.parse_failed",
        "file.dlp_unavailable",
        "file.storage_unavailable",
        "file.integrity_failed",
    }
    # failed 只能携带冻结错误目录，并与独立 blocked 状态保持互斥。
    error_code = payload.get("error_code")
    if (
        type(error_code) is not str
        or error_code not in allowed_failure_codes
        or dlp_status == "blocked"
    ):
        # 未知失败码或 blocked/failed 混淆会误导订阅方，必须在签名前整体拒绝。
        raise ValueError("webhook.payload_invalid")


def _validate_file_event_envelope(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """绑定文件事件与 tenant、Session actor、file owner 和原始上传链路。

    source_request_id 不进入外发 payload，但必须作为构造参数存在；数据库 source event trigger
    会进一步把它与 UploadedFile 逐字绑定。Shared 层拒绝缺失身份，不能生成替代 request/trace。
    """

    time_field = "created_at" if event_type == "file.uploaded" else "completed_at"
    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="fevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or not _is_bounded_safe_text(actor_id, maximum=160)
        or object_id != payload["file_id"]
        or not _is_bounded_safe_text(source_request_id, maximum=128)
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or occurred_at.tzinfo is None
        or _to_iso(occurred_at) != payload[time_field]
    ):
        # 任一 owner、actor、链路或 source event 时间漂移都在签名前失败关闭。
        raise ValueError("webhook.payload_invalid")


def _validate_file_prompt_injection_payload(payload: dict[str, Any]) -> None:
    """校验文件 Prompt Injection 事件的七字段 exact-key 安全摘要。"""

    expected = {
        "scan_id",
        "file_id",
        "risk_level",
        "finding_count",
        "high_finding_count",
        "model_access_status",
        "detector_version",
    }
    if set(payload) != expected:
        # exact-set 阻止文件名、finding ID、页码、OCR 状态或报告哈希被未来调用方意外外发。
        raise ValueError("webhook.payload_invalid")
    finding_count = payload["finding_count"]
    high_count = payload["high_finding_count"]
    if (
        not _is_prefixed_lower_hex_id(payload["scan_id"], prefix="fscn_")
        or not _is_prefixed_lower_hex_id(payload["file_id"], prefix="file_")
        or payload["risk_level"] not in {"high", "critical"}
        or type(finding_count) is not int
        or not 1 <= finding_count <= 256
        or type(high_count) is not int
        or not 1 <= high_count <= finding_count
        or payload["model_access_status"] != "blocked"
        or payload["detector_version"] != "file-security-detector-v1"
    ):
        # bool 不能冒充计数；只有真实高风险 blocked 报告才具备该事件语义。
        raise ValueError("webhook.payload_invalid")


def _validate_file_prompt_injection_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把安全摘要绑定到持久 fsev_、tenant/actor/file 和原扫描 request/trace。"""

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="fsev_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or not _is_bounded_safe_text(actor_id, maximum=160)
        or object_id != payload["file_id"]
        or not _is_bounded_safe_text(source_request_id, maximum=128)
        or not _is_bounded_safe_text(trace_id, maximum=128)
        or occurred_at.tzinfo is None
    ):
        # payload 不复制 request/trace/time；数据库 source trigger 进一步证明它们与 Scan 完全一致。
        raise ValueError("webhook.payload_invalid")


def _validate_session_high_risk_payload(payload: dict[str, Any]) -> None:
    """校验 ``security.session_high_risk`` 的精确安全状态摘要。

    参数：已通过通用敏感键扫描的 payload。返回值：无。关键约束：API session 只允许
    16 hex 的短引用，不接受完整 HMAC、turn digest、正文、DLP pattern 或动态原因文本。
    """

    expected = {
        "schema_version",
        "session_risk_score_id",
        "session_risk_action_id",
        "session_risk_freeze_id",
        "scope_type",
        "scope_reference",
        "status",
        "target_type",
        "score",
        "score_scale",
        "risky_turn_count",
        "jailbreak_count",
        "sensitive_count",
        "tool_inducement_count",
        "state_revision",
        "threshold_version",
        "occurred_at",
    }
    if set(payload) != expected:
        # exact-set 阻止未来字段、完整 scope 或调试证据被意外签名并发送到租户 Endpoint。
        raise ValueError("webhook.payload_invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        # bool 不能借 int 子类关系成为 schema 版本，下游只实现冻结 v1 合同。
        raise ValueError("webhook.payload_invalid")
    # Score ID 必须是服务端生成的固定前缀小写十六进制，阻断自由文本进入 Webhook。
    if not _is_prefixed_lower_hex_id(payload["session_risk_score_id"], prefix="srs_"):
        raise ValueError("webhook.payload_invalid")
    # Action ID 同样执行精确格式校验，防止跨实体或跨租户摘要被拼接到事件中。
    if not _is_prefixed_lower_hex_id(payload["session_risk_action_id"], prefix="sra_"):
        # 对外只返回统一 payload 错误，不能回显攻击者提交的标识符内容。
        raise ValueError("webhook.payload_invalid")
    freeze_id = payload["session_risk_freeze_id"]
    target_type = payload["target_type"]
    if freeze_id is None:
        # 纯 high-risk/dismiss/expire 跃迁没有 overlay，target 也必须为空，禁止半关联事件。
        if target_type is not None:
            raise ValueError("webhook.payload_invalid")
    elif (
        not _is_prefixed_lower_hex_id(freeze_id, prefix="srf_")
        or target_type not in {"session", "virtual_key"}
    ):
        # overlay 事件必须同时提供规范 freeze ID 和固定目标类型。
        raise ValueError("webhook.payload_invalid")

    scope_type = payload["scope_type"]
    scope_reference = payload["scope_reference"]
    valid_chat_reference = (
        scope_type == "enterprise_chat"
        and _is_prefixed_lower_hex_id(scope_reference, prefix="chs_")
    )
    valid_api_reference = (
        scope_type == "api_session"
        and type(scope_reference) is str
        and scope_reference.startswith("api_")
        and _is_lower_hex(scope_reference[4:], expected_length=16)
    )
    if not (valid_chat_reference or valid_api_reference):
        # API 只允许 64-bit 展示引用；完整 srh_ 摘要或原始 mhs_ Header 都必须失败关闭。
        raise ValueError("webhook.payload_invalid")
    if payload["status"] not in {"high_risk", "frozen", "resolved", "expired"}:
        # monitoring 不产生高危事件，未知状态不能由订阅端自行猜测。
        raise ValueError("webhook.payload_invalid")

    score = payload["score"]
    scale = payload["score_scale"]
    risky_turn_count = payload["risky_turn_count"]
    if (
        type(score) is not int
        or not 1 <= score <= 10_000
        or type(scale) is not int
        or scale != 10_000
        or type(risky_turn_count) is not int
        or risky_turn_count < 1
    ):
        # 分数和风险 turn 使用固定整数口径，禁止 float/bool 或另一量纲。
        raise ValueError("webhook.payload_invalid")
    for field_name in (
        "jailbreak_count",
        "sensitive_count",
        "tool_inducement_count",
    ):
        count = payload[field_name]
        if type(count) is not int or not 0 <= count <= risky_turn_count:
            # 单类每个 turn 最多加一；超出总风险 turn 表示 source row 已损坏。
            raise ValueError("webhook.payload_invalid")
    if (
        type(payload["state_revision"]) is not int
        or payload["state_revision"] < 1
        or payload["threshold_version"] != "session-risk-threshold-v1"
        or not _is_canonical_utc_second(payload["occurred_at"])
    ):
        # revision、阈值版本与 UTC 秒共同冻结一次状态跃迁，漂移时整体拒绝。
        raise ValueError("webhook.payload_invalid")


def _validate_session_high_risk_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """绑定高危会话 payload 与不可变 source event、租户、score 和来源链路。

    自动与人工事件都必须有固定 actor；系统 actor 是代码内服务身份，人工 actor 是后端
    鉴权用户 ID。两者均不能由 payload 自由文本替代，且时间必须与 source row 同秒。
    """

    if (
        not _is_prefixed_lower_hex_id(event_id, prefix="srevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or not _is_bounded_safe_text(actor_id, maximum=160)
        or object_id != payload["session_risk_score_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=128)
        or occurred_at.tzinfo is None
        or _to_iso(occurred_at) != payload["occurred_at"]
    ):
        # 任一 owner、对象、链路或发生时刻漂移都失败关闭，不能生成替代身份掩盖损坏事实。
        raise ValueError("webhook.payload_invalid")


def _is_prefixed_lower_hex_id(value: object, *, prefix: str) -> bool:
    """判断领域 ID 是否由冻结前缀和 32 位小写十六进制组成。"""

    return (
        type(value) is str
        and value.startswith(prefix)
        and _is_lower_hex(value[len(prefix) :], expected_length=32)
    )


def _validate_request_explanation_viewed_payload(payload: dict[str, Any]) -> None:
    """校验 `request.explanation_viewed` 的精确、最小披露 payload。

    参数：已通过通用敏感键扫描的 payload；返回：合法时无返回值。
    约束：只允许源请求标识、解释完整度和区块缺失/截断枚举，绝不携带解释正文。
    """

    expected_fields = {
        "schema_version",
        "request_explanation_id",
        "source_request_id",
        "source_trace_id",
        "source_status",
        "explanation_status",
        "missing_sections",
        "truncated_sections",
        "evidence_version",
        "viewed_at",
    }
    if set(payload) != expected_fields:
        # 额外字段可能复制成本、错误或 DLP 证据，必须整体拒绝而不是静默裁剪。
        raise ValueError("webhook.payload_invalid")
    explanation_id = payload["request_explanation_id"]
    if (
        type(explanation_id) is not str
        or not explanation_id.startswith("rxe_")
        or not _is_lower_hex(explanation_id[4:], expected_length=32)
    ):
        # Explanation ID 必须绑定持久 rxe_ 对象，禁止临时值或任意文本进入审计事件。
        raise ValueError("webhook.payload_invalid")
    for field in ("source_request_id", "source_trace_id"):
        if not _is_bounded_safe_text(payload[field], maximum=128):
            # 源链路 ID 只允许受限文本，控制字符和无界值不得进入签名或投递日志。
            raise ValueError("webhook.payload_invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        # 事件仅支持冻结 v1 schema，bool/字符串版本不能被隐式转换后接受。
        raise ValueError("webhook.payload_invalid")
    if payload["source_status"] not in {"success", "failed"}:
        # 未知源终态会改变决策链含义，不能作为任意字符串向租户 Webhook 扩散。
        raise ValueError("webhook.payload_invalid")
    explanation_status = payload["explanation_status"]
    if explanation_status not in {"complete", "partial"}:
        # 完整性只有冻结双态，未知状态不得被订阅方误解为已完整验证。
        raise ValueError("webhook.payload_invalid")
    if payload["evidence_version"] != "request_explanation_v1":
        # 证据版本必须和 API/数据库冻结合同一致，禁止跨版本字段语义混投。
        raise ValueError("webhook.payload_invalid")
    if not _is_canonical_utc_second(payload["viewed_at"]):
        # 查看时间参与 envelope 绑定，非 canonical UTC 会破坏稳定签名与审计排序。
        raise ValueError("webhook.payload_invalid")

    section_order = (
        "authentication",
        "rate_limit",
        "budget",
        "dlp",
        "policy",
        "routing",
        "keypool",
        "provider_response",
        "usage_and_finops",
        "logs",
        "webhooks",
    )
    missing = payload["missing_sections"]
    truncated = payload["truncated_sections"]
    if (
        not _is_stable_subset(missing, allowed_order=section_order, allow_empty=True)
        or not _is_stable_subset(truncated, allowed_order=section_order, allow_empty=True)
        or set(missing) & set(truncated)
    ):
        # 同一区块不能同时声明完全缺失和仅被截断，数组顺序也必须签名稳定。
        raise ValueError("webhook.payload_invalid")
    if explanation_status == "complete" and (missing or truncated):
        # complete 事件不能同时承认缺失或截断，否则会向订阅者发送虚假完整结论。
        raise ValueError("webhook.payload_invalid")
    if explanation_status == "partial" and not (missing or truncated):
        # partial 必须给出稳定缺口，禁止无法定位原因的笼统降级事件。
        raise ValueError("webhook.payload_invalid")


def _validate_request_explanation_viewed_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    occurred_at: datetime,
) -> None:
    """把查看事件绑定到持久 Explanation、可信 actor 和本次操作链路。"""

    if (
        type(event_id) is not str
        or not event_id.startswith("rxevt_")
        or not _is_lower_hex(event_id[6:], expected_length=32)
        or object_id != payload["request_explanation_id"]
        or not _is_bounded_safe_text(actor_id, maximum=128)
        or not _is_bounded_safe_text(trace_id, maximum=128)
        or _to_iso(occurred_at) != payload["viewed_at"]
    ):
        # 稳定事件、对象、用户、operation trace 或查看时间任一漂移都会破坏审计绑定。
        raise ValueError("webhook.payload_invalid")



def _validate_request_log_query_executed_payload(payload: dict[str, Any]) -> None:
    """校验 log.query_executed 只包含不可逆筛选摘要和有界计数。

    参数：已通过通用敏感键扫描的 payload；返回：合法时无返回值。
    约束：不得加入原始 filters、cursor、Virtual Key、模型列表、保存筛选名称或日志正文。
    """

    expected_fields = {
        "schema_version", "query_event_id", "filter_version", "filter_keys",
        "filter_fingerprint", "saved_filter_id", "sort_by", "sort_direction",
        "limit", "offset", "matched_count", "returned_count", "has_more",
        "query_duration_ms", "executed_at",
    }
    if set(payload) != expected_fields:
        # 精确白名单防止 filters 值、保存筛选名称或未来未审字段进入签名事件。
        raise ValueError("webhook.payload_invalid")
    query_event_id = payload["query_event_id"]
    if (
        type(query_event_id) is not str
        or not query_event_id.startswith("lqe_")
        or not _is_lower_hex(query_event_id[4:], expected_length=32)
    ):
        # QueryEvent ID 必须绑定 0045 持久事实，禁止随机或非 canonical 标识。
        raise ValueError("webhook.payload_invalid")
    fingerprint = payload["filter_fingerprint"]
    if type(fingerprint) is not str or not _is_lower_hex(fingerprint, expected_length=64):
        # 指纹只接受 canonical SHA-256，不能用短摘要或任意文本替代原筛选值。
        raise ValueError("webhook.payload_invalid")
    allowed_keys = (
        "application_id", "cost_amount_max", "cost_amount_min", "created_from", "created_to",
        "currency", "environment_id", "error_code", "latency_ms_max", "latency_ms_min",
        "model", "project_id", "provider_code", "request_id", "request_model", "resolved_model",
        "snapshot_version", "status", "terminal_stage", "total_tokens_max",
        "total_tokens_min", "trace_id", "virtual_key_id",
    )
    if not _is_stable_subset(payload["filter_keys"], allowed_order=allowed_keys, allow_empty=True):
        # 字段名必须唯一且按冻结顺序排列，确保重试签名稳定并拒绝未知筛选字段。
        raise ValueError("webhook.payload_invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        # 未知事件 schema 不能由订阅端猜测，bool 也不得冒充整数版本。
        raise ValueError("webhook.payload_invalid")
    if type(payload["filter_version"]) is not int or payload["filter_version"] != 1:
        # 指纹解释依赖过滤器版本，未知版本必须在发送前失败关闭。
        raise ValueError("webhook.payload_invalid")
    saved_filter_id = payload["saved_filter_id"]
    if saved_filter_id is not None and (
        type(saved_filter_id) is not str
        or not saved_filter_id.startswith("sf_")
        or not _is_lower_hex(saved_filter_id[3:], expected_length=32)
    ):
        # 只有已验证 owner 的稳定 sf_ ID 可进入事件，名称和任意文本一律拒绝。
        raise ValueError("webhook.payload_invalid")
    if payload["sort_by"] not in {
        "created_at",
        "latency_ms",
        "total_tokens",
        "estimated_cost_amount",
    }:
        # 排序字段必须来自公开白名单，禁止任意列名进入下游审计合同。
        raise ValueError("webhook.payload_invalid")
    if payload["sort_direction"] not in {"asc", "desc"}:
        # 排序方向仅允许强枚举，未知文本不得由订阅端自行解释。
        raise ValueError("webhook.payload_invalid")
    integer_bounds = {
        "limit": (1, 100), "offset": (0, 10000), "matched_count": (0, 9_007_199_254_740_991),
        "returned_count": (0, 100), "query_duration_ms": (0, 9_007_199_254_740_991),
    }
    for field, (minimum, maximum) in integer_bounds.items():
        value = payload[field]
        if type(value) is not int or not minimum <= value <= maximum:
            # 计数必须是 JavaScript 安全范围内的严格整数，bool 和负数都会破坏分页语义。
            raise ValueError("webhook.payload_invalid")
    has_more = payload["has_more"]
    returned_count = payload["returned_count"]
    matched_count = payload["matched_count"]
    offset = payload["offset"]
    limit = payload["limit"]
    remaining = max(matched_count - offset, 0)
    if (
        type(has_more) is not bool
        or returned_count > limit
        or returned_count > remaining
        or has_more != (offset + returned_count < matched_count)
    ):
        # has_more 必须由公开计数唯一推导，不能让生产者伪造“还有下一页”或隐藏剩余结果。
        raise ValueError("webhook.payload_invalid")
    if not _is_canonical_utc_second(payload["executed_at"]):
        raise ValueError("webhook.payload_invalid")


def _validate_request_log_query_executed_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    occurred_at: datetime,
) -> None:
    """把查询事件绑定到持久 QueryEvent、可信用户和本次 Control operation trace。"""

    if (
        type(event_id) is not str
        or not event_id.startswith("lqevt_")
        or not _is_lower_hex(event_id[6:], expected_length=32)
        or object_id != payload["query_event_id"]
        or not _is_bounded_safe_text(actor_id, maximum=128)
        or not _is_bounded_safe_text(trace_id, maximum=128)
        or _to_iso(occurred_at) != payload["executed_at"]
    ):
        # 信封身份或时间任一漂移都会把查询通知绑定到错误持久事实，必须整体拒绝。
        raise ValueError("webhook.payload_invalid")

def _is_canonical_utc_second(value: object) -> bool:
    """判断值是否为无小数、Z 后缀的 canonical UTC 秒级时间。"""

    if type(value) is not str or not value.endswith("Z") or "." in value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 非法日期或偏移格式不能进入签名事件，避免接收端产生不同时间解释。
    except ValueError:
        return False
    return parsed.tzinfo is not None and _to_iso(parsed) == value


def _validate_cost_anomaly_detected_payload(payload: dict[str, Any]) -> None:
    """校验 ``cost.anomaly_detected`` 的精确、可公开检测证据。

    参数：已经通过递归敏感键扫描的事件 payload；返回：合法时无返回值。
    约束：金额和比率必须保持任意精度十进制文本，计数必须落在 JavaScript 安全整数范围；
    规则映射和交叉乘积会在共享层重算，不能信任生产者给出的严重级别或触发结论。
    """

    expected_fields = {
        "anomaly_id",
        "rule_type",
        "severity",
        "status",
        "environment_id",
        "scope_type",
        "scope_id",
        "window_start",
        "window_end",
        "current_cost_micros",
        "baseline_total_micros",
        "observed_ratio_millis",
        "baseline_bucket_count",
        "threshold_multiplier_millis",
        "minimum_cost_micros",
        "source_entry_count",
        "estimated_entry_count",
        "affected_virtual_key_count",
        "recommended_action",
        "detection_version",
    }
    if set(payload) != expected_fields:
        # 精确字段集合阻止 Key 名称、请求样本、价格正文或未来未审计字段意外外发。
        raise ValueError("webhook.payload_invalid")

    rule_type = payload["rule_type"]
    rule_contracts = {
        "tenant_spend_spike": ("critical", "tenant", "review_impacted_keys"),
        "virtual_key_spend_spike": (
            "critical",
            "virtual_key",
            "apply_temporary_rate_limit",
        ),
        "virtual_key_off_hours": (
            "warning",
            "virtual_key",
            "apply_temporary_rate_limit",
        ),
    }
    # 未知规则无法绑定冻结的严重级别和处置建议，不能作为可扩展枚举静默外发。
    if type(rule_type) is not str or rule_type not in rule_contracts:
        raise ValueError("webhook.payload_invalid")
    severity, scope_type, recommended_action = rule_contracts[rule_type]
    if (
        payload["severity"] != severity
        or payload["scope_type"] != scope_type
        or payload["recommended_action"] != recommended_action
        or payload["status"] != "open"
        or payload["detection_version"] != "cost-anomaly-rules-v1"
        or not _is_prefixed_lower_hex(payload["anomaly_id"], prefix="can_")
        or not _is_bounded_safe_text(payload["environment_id"], maximum=128)
        or not _is_bounded_safe_text(payload["scope_id"], maximum=128)
    ):
        # 严重级别、作用域和建议动作由规则冻结，生产者不能用 payload 动态改写治理语义。
        raise ValueError("webhook.payload_invalid")

    window_start = _canonical_utc_five_minute(payload["window_start"])
    window_end = _canonical_utc_five_minute(payload["window_end"])
    if (
        window_start is None
        or window_end is None
        or int((window_end - window_start).total_seconds()) != 300
    ):
        # 事件只描述一个左闭右开的完整五分钟桶，禁止模糊时区、秒数或跨桶窗口。
        raise ValueError("webhook.payload_invalid")

    current_cost = _canonical_unsigned_decimal(payload["current_cost_micros"])
    baseline_total = _canonical_unsigned_decimal(payload["baseline_total_micros"])
    observed_ratio = _canonical_unsigned_decimal(payload["observed_ratio_millis"])
    threshold = payload["threshold_multiplier_millis"]
    minimum_cost = payload["minimum_cost_micros"]
    if (
        current_cost is None
        or baseline_total is None
        or type(threshold) is not int
        or not 1001 <= threshold <= 100_000
        or type(minimum_cost) is not int
        or not 1 <= minimum_cost <= 1_000_000_000_000
        or current_cost < minimum_cost
        or type(payload["baseline_bucket_count"]) is not int
        or payload["baseline_bucket_count"] != 288
    ):
        # 使用整数和 canonical 文本可避免浮点、bool 与科学计数法改变阈值边界。
        raise ValueError("webhook.payload_invalid")

    count_fields = (
        "source_entry_count",
        "estimated_entry_count",
        "affected_virtual_key_count",
    )
    if any(
        type(payload[field]) is not int
        or not 0 <= payload[field] <= 9_007_199_254_740_991
        for field in count_fields
    ):
        # 计数保持 JSON 数字是为了接收端易用，因此必须限制在 IEEE-754 安全整数范围内。
        raise ValueError("webhook.payload_invalid")

    if rule_type == "virtual_key_off_hours":
        if payload["observed_ratio_millis"] is not None:
            # 非工作时段规则不依赖历史倍数，必须显式使用 null，不能伪造一个解释比率。
            raise ValueError("webhook.payload_invalid")
        return
    if (
        baseline_total <= 0
        or observed_ratio is None
        or observed_ratio != current_cost * 288 * 1000 // baseline_total
        or current_cost * 288 * 1000 <= baseline_total * threshold
    ):
        # 暴涨事件必须用同一整数公式重新证明严格越限，等于门限或零基线都不是异常。
        raise ValueError("webhook.payload_invalid")


def _validate_cost_anomaly_detected_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
) -> None:
    """绑定成本异常事件的持久事件 ID、异常对象和检测运行链路。"""

    if (
        not _is_prefixed_lower_hex(event_id, prefix="caevt_")
        or actor_id is not None
        or object_id != payload["anomaly_id"]
        or not _is_bounded_safe_text(trace_id, maximum=128)
    ):
        # 检测器没有用户 actor；任一身份漂移都会破坏订阅端幂等和租户内追踪。
        raise ValueError("webhook.payload_invalid")


def _canonical_unsigned_decimal(value: object) -> int | None:
    """把最长 40 位、无前导零的非负十进制文本转换为 Python 整数。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= 40
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        return None
    return int(value)


def _canonical_utc_five_minute(value: object) -> datetime | None:
    """解析规范 UTC RFC3339 时间，并确认它位于五分钟桶边界。"""

    if type(value) is not str or len(value) != 20 or not value.isascii():
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    # 日期、时区或格式非法时返回失败标记，避免把解析器异常和原始值带入日志。
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value or parsed.minute % 5 != 0:
        return None
    return parsed

def _validate_budget_scope_exceeded_payload(payload: dict[str, Any]) -> None:
    """校验 ``budget.scope_exceeded`` 的精确安全 payload。

    金额表达 Provider 前的正式入账、未结算预留和本次冻结估算。字段集合和加法关系固定，
    防止接收方把估算误认成已消费成本，也防止未来扩展把请求正文或凭据带出系统。
    """

    expected_fields = {
        "budget_scope_event_id",
        "budget_scope_id",
        "budget_rule_id",
        "environment_id",
        "scope_type",
        "scope_id",
        "budget_date",
        "posted_cost_cny",
        "reserved_cost_cny",
        "estimated_request_cost_cny",
        "projected_cost_cny",
        "budget_cny",
        "action_taken",
        "request_id",
        "conflict_policy",
    }
    if set(payload) != expected_fields:
        # 精确字段合同既阻止敏感内容外泄，也让下游无需猜测金额语义。
        raise ValueError("webhook.payload_invalid")
    if (
        not _is_prefixed_lower_hex(payload["budget_scope_event_id"], prefix="bse_")
        or not _is_prefixed_lower_hex(payload["budget_scope_id"], prefix="bsc_")
        or not _is_bounded_safe_text(payload["budget_rule_id"], maximum=128)
        or not _is_bounded_safe_text(payload["environment_id"], maximum=128)
        or not _is_bounded_safe_text(payload["scope_id"], maximum=128)
        or not _is_bounded_safe_text(payload["request_id"], maximum=128)
        or payload["scope_type"] not in {"tenant", "project", "application", "virtual_key"}
        or payload["action_taken"] not in {"allow", "block"}
        or payload["conflict_policy"] != "all_applicable_caps_deny_wins_v1"
        or not _is_budget_date(payload["budget_date"])
    ):
        # Scope 身份、首次请求链和固定冲突策略共同决定幂等投递语义，任一项异常都必须失败关闭。
        raise ValueError("webhook.payload_invalid")

    amount_fields = (
        "posted_cost_cny",
        "reserved_cost_cny",
        "estimated_request_cost_cny",
        "projected_cost_cny",
        "budget_cny",
    )
    amounts = {field: _budget_money_micros(payload[field]) for field in amount_fields}
    if any(value is None for value in amounts.values()):
        # 金额必须先统一为有界整数微元，避免浮点、科学计数法或超精度文本改变越限结论。
        raise ValueError("webhook.payload_invalid")
    posted = amounts["posted_cost_cny"]
    reserved = amounts["reserved_cost_cny"]
    estimated = amounts["estimated_request_cost_cny"]
    projected = amounts["projected_cost_cny"]
    budget = amounts["budget_cny"]
    if projected != posted + reserved + estimated or projected < budget:
        # 越限事实必须满足整数微元恒等式且确实达到额度，不能信任调用方提供的汇总文本。
        raise ValueError("webhook.payload_invalid")


def _validate_budget_scope_exceeded_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
) -> None:
    """绑定三级预算越限事件的稳定 ID、Scope 对象和首次调用链。"""

    if (
        not _is_prefixed_lower_hex(event_id, prefix="bsevt_")
        or actor_id is not None
        or object_id != payload["budget_scope_id"]
        or not _is_bounded_safe_text(trace_id, maximum=128)
    ):
        # 运行时没有可信用户 actor；错误绑定会破坏租户内审计和幂等投递语义。
        raise ValueError("webhook.payload_invalid")


def _is_budget_date(value: object) -> bool:
    """判断值是否为规范的公历 ``YYYY-MM-DD`` 业务日文本。"""

    if type(value) is not str or len(value) != 10 or not value.isascii():
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    # 非法公历日期只返回布尔失败，不把解析器异常或原始输入带入 Webhook 错误链。
    except ValueError:
        return False


def _budget_money_micros(value: object) -> int | None:
    """把最多六位小数的普通非负 CNY 文本严格转换为有界整数微元。"""

    if type(value) is not str:
        return None
    integer, separator, fraction = value.partition(".")
    if (
        not 1 <= len(integer) <= 10
        or not integer.isascii()
        or not integer.isdecimal()
        or (len(integer) > 1 and integer.startswith("0"))
        or (
            separator
            and (
                not 1 <= len(fraction) <= 6
                or not fraction.isascii()
                or not fraction.isdecimal()
            )
        )
    ):
        return None
    micros = int(integer) * 1_000_000 + int((fraction if separator else "").ljust(6, "0") or "0")
    return micros if micros <= 1_000_000_000_000_000 else None


def _validate_finops_ledger_entry_payload(payload: dict[str, Any]) -> None:
    """校验 ``ledger.entry_created`` 的精确安全 payload。

    金额只能是六位小数普通十进制文本；价格证据必须与实际 PriceSnapshot source 对应，
    不能通过零金额反推免费或有价。字段集合固定也阻止 reason、幂等键和请求正文外泄。
    """

    expected_fields = {
        "ledger_entry_id",
        "entry_type",
        "direction",
        "amount",
        "currency",
        "normalized_cny",
        "cny_normalization_status",
        "quota_type",
        "source_request_id",
        "price_snapshot_id",
        "price_schema_version",
        "price_snapshot_source",
        "price_evidence_status",
        "reversal_of_entry_id",
        "quota_allocation_class",
        "quota_allocation_count",
    }
    # 精确字段白名单和分录身份共同阻止调整原因、正文或伪造对象混入订阅事件。
    # 固定字段集阻止完整矩阵、邮箱或未来未知内容被意外签名并发送出控制面。
    if set(payload) != expected_fields:
        # 字段集合不一致只返回稳定分类，不能把未知字段名或正文拼进异常。
        raise ValueError("webhook.payload_invalid")
    if not _is_prefixed_lower_hex(payload["ledger_entry_id"], prefix="fle_"):
        # 非法分录身份不能进入 object_id 或下游幂等索引，统一拒绝事件。
        raise ValueError("webhook.payload_invalid")
    entry_type = payload["entry_type"]
    direction = payload["direction"]
    allowed_types = {
        "usage_charge",
        "manual_debit",
        "manual_credit",
        "reversal",
        "gift_credit",
        "trial_credit",
    }
    # 分录类型和会计方向只接受冻结枚举，未知数据库值不能被静默外发。
    if entry_type not in allowed_types or direction not in {"debit", "credit"}:
        raise ValueError("webhook.payload_invalid")
    fixed_directions = {
        "usage_charge": "debit",
        "manual_debit": "debit",
        "manual_credit": "credit",
        "gift_credit": "credit",
        "trial_credit": "credit",
    }
    if entry_type in fixed_directions and direction != fixed_directions[entry_type]:
        # reversal 方向由目标反向得出；其余类型必须使用固定会计方向。
        raise ValueError("webhook.payload_invalid")
    # 金额必须使用固定六位小数文本，防止科学计数法或浮点解释差异破坏签名事实。
    if not _is_finops_money_text(payload["amount"]):
        raise ValueError("webhook.payload_invalid")
    # 人工调整必须具有正金额，只有无价格 Usage 可以形成零额分录。
    if entry_type != "usage_charge" and payload["amount"] == "0.000000":
        raise ValueError("webhook.payload_invalid")
    currency = payload["currency"]
    normalized_cny = payload["normalized_cny"]
    normalization_status = payload["cny_normalization_status"]
    # PRD-051 只冻结 CNY/USD 原币事实，未知币种和折算状态必须失败关闭。
    if currency not in {"CNY", "USD"} or normalization_status not in {
        "normalized",
        "unavailable",
    }:
        # 未知币种或归一状态会改变金额解释，不能发送含糊财务事实。
        raise ValueError("webhook.payload_invalid")
    if normalization_status == "normalized":
        # 已归一状态必须携带同样受限的六位小数金额，不能让字符串格式漂移。
        if not _is_finops_money_text(normalized_cny):
            raise ValueError("webhook.payload_invalid")
    elif normalized_cny is not None:
        # 未折算必须显式为 NULL，禁止用 0 CNY 冒充已归一或免费。
        raise ValueError("webhook.payload_invalid")
    # CNY 原币无需汇率，归一金额必须与原金额逐字符一致才能保持审计确定性。
    if currency == "CNY" and (
        normalization_status != "normalized" or normalized_cny != payload["amount"]
    ):
        # CNY 原币与归一金额不一致时不能选择任一值继续外发。
        raise ValueError("webhook.payload_invalid")
    # PRD-051 不引入 FX；USD 原币事实不得被无快照汇率折算。
    if currency == "USD" and normalization_status != "unavailable":
        raise ValueError("webhook.payload_invalid")
    # 配额类型影响财务展示和治理统计，未知值不能回退为付费或免费。
    if payload["quota_type"] not in {"paid", "free", "trial", "gift"}:
        raise ValueError("webhook.payload_invalid")
    allocation_class = payload["quota_allocation_class"]
    allocation_count = payload["quota_allocation_count"]
    # 用量费用必须携带权威分摊分类和有界计数，禁止推测额度归属。
    if entry_type == "usage_charge":
        if (
            allocation_class not in {"paid", "free", "trial", "gift", "mixed", "unallocated"}
            or type(allocation_count) is not int
            or not 0 <= allocation_count <= 4
            or (allocation_class == "unallocated") != (allocation_count == 0)
            or (allocation_class == "mixed" and allocation_count < 2)
            or (allocation_class in {"paid", "free", "trial", "gift"} and allocation_count != 1)
        ):
            # 分摊分类与计数必须相互一致，否则会污染财务和额度统计。
            raise ValueError("webhook.payload_invalid")
    elif allocation_class != "not_applicable" or allocation_count != 0:
        # 人工 FinOps 调整不消耗租户套餐额度，禁止把历史 quota_type 伪装成真实分摊。
        raise ValueError("webhook.payload_invalid")
    source_request_id = payload["source_request_id"]
    # 可选请求身份仍须有界且无控制字符，避免事件日志和下游索引被污染。
    if source_request_id is not None and not _is_bounded_safe_text(
        source_request_id, maximum=128
    ):
        # 请求身份含控制字符或超长时会污染审计关联，必须整条拒绝。
        raise ValueError("webhook.payload_invalid")
    # Usage 分录必须绑定原请求；人工调整是否关联请求由调整类型另行约束。
    if entry_type == "usage_charge" and source_request_id is None:
        raise ValueError("webhook.payload_invalid")
    reversal_id = payload["reversal_of_entry_id"]
    # 冲正只能指向合法正式分录，非冲正事件则禁止夹带目标身份。
    if entry_type == "reversal":
        if not _is_prefixed_lower_hex(reversal_id, prefix="fle_"):
            raise ValueError("webhook.payload_invalid")
    elif reversal_id is not None:
        # 非冲正分录携带目标身份会制造虚假关系，因此禁止外发。
        raise ValueError("webhook.payload_invalid")
    _validate_finops_price_evidence(payload, entry_type=entry_type)


def _validate_finops_price_evidence(payload: dict[str, Any], *, entry_type: str) -> None:
    """校验账本事件的快照 source、版本和公开证据状态是一致事实。"""

    snapshot_id = payload["price_snapshot_id"]
    version = payload["price_schema_version"]
    source = payload["price_snapshot_source"]
    status = payload["price_evidence_status"]
    # 证据状态是公开审计枚举，未知值不能根据金额或快照字段自动猜测。
    if status not in {"priced", "unpriced", "legacy_amount_only", "not_applicable"}:
        raise ValueError("webhook.payload_invalid")
    if status == "not_applicable":
        # Usage 永远需要价格证据；人工项不适用时三个快照字段必须同时为空。
        if entry_type == "usage_charge" or any(
            item is not None for item in (snapshot_id, version, source)
        ):
            # 不适用状态与 Usage 或任一快照字段冲突时不能猜测调用方意图。
            raise ValueError("webhook.payload_invalid")
        return
    # 非不适用状态必须绑定可审计快照、版本和来源，拒绝半填充证据。
    if (
        not _is_prefixed_lower_hex(snapshot_id, prefix="prs_")
        or not _is_bounded_safe_text(version, maximum=128)
        or not _is_bounded_safe_text(source, maximum=64)
    ):
        # 半填充或非法证据无法追溯历史价格，必须拒绝发布事件。
        raise ValueError("webhook.payload_invalid")
    expected_special = {
        "unpriced": ("finops_unpriced", "unpriced-v1"),
        "legacy_amount_only": ("finops_legacy_backfill", "legacy-unversioned-v1"),
    }
    if status in expected_special:
        # 特殊证据状态必须与冻结 source/version 成对，不能借历史名义包装当前价格。
        if (source, version) != expected_special[status]:
            raise ValueError("webhook.payload_invalid")
    elif source in {"finops_unpriced", "finops_legacy_backfill"}:
        # 特殊 source 不能被标成 priced，金额为零也不能反向决定证据状态。
        raise ValueError("webhook.payload_invalid")


def _validate_finops_ledger_entry_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
) -> None:
    """绑定账本分录的持久事件、对象、操作者和链路身份。"""

    # 持久事件、对象和 trace 必须来自同一账本事实，避免 payload 与信封错配。
    if (
        not _is_prefixed_lower_hex(event_id, prefix="flevt_")
        or object_id != payload["ledger_entry_id"]
        or not _is_bounded_safe_text(trace_id, maximum=128)
    ):
        # 信封身份无法与 payload 财务事实绑定时不得进入投递队列。
        raise ValueError("webhook.payload_invalid")
    # 自动 Usage 不得伪造操作者；所有人工调整必须绑定认证用户身份。
    if payload["entry_type"] == "usage_charge":
        if actor_id is not None:
            raise ValueError("webhook.payload_invalid")
    elif not _is_bounded_safe_text(actor_id, maximum=128):
        # 人工调整必须绑定认证上下文中的操作者，不能由 payload 自报或留空。
        raise ValueError("webhook.payload_invalid")


def _is_finops_money_text(value: object) -> bool:
    """判断金额是否为上限十亿、固定六位小数的普通 ASCII 十进制文本。"""

    if type(value) is not str:
        return False
    integer, separator, fraction = value.partition(".")
    if (
        separator != "."
        or not 1 <= len(integer) <= 10
        or len(fraction) != 6
        or not integer.isascii()
        or not fraction.isascii()
        or not integer.isdecimal()
        or not fraction.isdecimal()
        or (len(integer) > 1 and integer.startswith("0"))
    ):
        return False
    return int(integer) <= 1_000_000_000


def _is_billing_dispute_aggregate_money_text(value: object, *, item_count: int) -> bool:
    """Validate a canonical positive total bounded by the frozen item count."""

    if type(value) is not str or type(item_count) is not int or not 1 <= item_count <= 100:
        return False
    integer, separator, fraction = value.partition(".")
    if (
        separator != "."
        or not 1 <= len(integer) <= 12
        or len(fraction) != 6
        or not integer.isascii()
        or not fraction.isascii()
        or not integer.isdecimal()
        or not fraction.isdecimal()
        or (len(integer) > 1 and integer.startswith("0"))
    ):
        return False
    amount_micros = int(integer) * 1_000_000 + int(fraction)
    maximum_micros = item_count * 1_000_000_000 * 1_000_000
    return 0 < amount_micros <= maximum_micros


def _validate_cache_affinity_selected_payload(payload: dict[str, Any]) -> None:
    """按 trigger 校验 PRD-050 运行选择或配置变化的精确安全 payload。

    参数：已通过通用敏感键递归扫描的 payload；返回：合法时无返回值。
    约束：两种 trigger 字段完全互斥，禁止 Prompt 指纹、scope、data_class 值、Credential
    或端点 URL 借额外字段进入订阅链路。
    """

    trigger = payload.get("trigger")
    if trigger == "request_selected":
        _validate_cache_affinity_runtime_payload(payload)
        return
    if trigger == "configuration_changed":
        _validate_cache_affinity_configuration_payload(payload)
        return
    # 未知 trigger 不能默认解释成配置或运行事件，否则 envelope actor 约束会被绕过。
    raise ValueError("webhook.payload_invalid")


def _validate_cache_affinity_runtime_payload(payload: dict[str, Any]) -> None:
    """校验每次真实 Credential/节点选择产生的 runtime 安全摘要。"""

    expected_fields = {
        "trigger",
        "request_id",
        "cache_affinity_selection_id",
        "cache_affinity_id",
        "cache_affinity_version",
        "model_alias_id",
        "model_endpoint_id",
        "selected_provider_id",
        "target_kind",
        "selection_reason",
        "estimated_hit",
    }
    if set(payload) != expected_fields:
        # 精确字段集同时拒绝缺字段和额外 digest、data_class、Credential 或正文扩展。
        raise ValueError("webhook.payload_invalid")
    selection_id = payload["cache_affinity_selection_id"]
    affinity_id = payload["cache_affinity_id"]
    version = payload["cache_affinity_version"]
    if not _is_prefixed_lower_hex(selection_id, prefix="cas_"):
        # runtime event_id 直接复用不可变 Selection ID，格式漂移会破坏幂等投递。
        raise ValueError("webhook.payload_invalid")
    if not _is_prefixed_lower_hex(affinity_id, prefix="caf_"):
        # object 身份必须是当前租户 CacheAffinity，不接受 Alias 或 Binding ID 冒充。
        raise ValueError("webhook.payload_invalid")
    if type(version) is not int or version < 1:
        # bool 不能借 Python 整数继承冒充配置版本，零值也没有持久配置来源。
        raise ValueError("webhook.payload_invalid")
    for field_name in (
        "request_id",
        "model_alias_id",
        "model_endpoint_id",
        "selected_provider_id",
    ):
        if not _is_bounded_safe_text(payload[field_name], maximum=128):
            # 链路与本地对象 ID 必须有界且无控制字符，避免污染接收端日志关联。
            raise ValueError("webhook.payload_invalid")
    if payload["target_kind"] not in ("managed", "environment", "none"):
        # 未知目标种类可能隐藏新的 Credential 语义，旧订阅端不得猜测处理。
        raise ValueError("webhook.payload_invalid")
    allowed_reasons = (
        "binding_created",
        "binding_pending_reused",
        "binding_warm_reused",
        "binding_rebound",
    )
    reason = payload["selection_reason"]
    estimated_hit = payload["estimated_hit"]
    if reason not in allowed_reasons or type(estimated_hit) is not bool:
        # 原因和预计命中必须是固定枚举/布尔，不能透传内部错误或 Provider 返回文本。
        raise ValueError("webhook.payload_invalid")
    if estimated_hit != (reason == "binding_warm_reused"):
        # pending、首次创建和重绑都没有 warm 成功证据，不得被事件虚报为预计命中。
        raise ValueError("webhook.payload_invalid")


def _validate_cache_affinity_configuration_payload(payload: dict[str, Any]) -> None:
    """校验 admin 创建/更新 CacheAffinity 后产生的配置变化安全摘要。"""

    expected_fields = {
        "trigger",
        "request_id",
        "cache_affinity_id",
        "cache_affinity_version",
        "model_alias_id",
        "change_kind",
        "changed_fields",
        "status",
    }
    if set(payload) != expected_fields:
        # 事件只公布变化字段名和最终状态，不外发 data_class 值、门限、TTL 或 Prompt 信息。
        raise ValueError("webhook.payload_invalid")
    affinity_id = payload["cache_affinity_id"]
    version = payload["cache_affinity_version"]
    if not _is_prefixed_lower_hex(affinity_id, prefix="caf_"):
        # 配置事件 object_id 必须可与租户限定 CacheAffinity API 事实稳定关联。
        raise ValueError("webhook.payload_invalid")
    if type(version) is not int or version < 1:
        # 配置版本必须是真正正整数，不能接收 bool 或不存在的 version=0。
        raise ValueError("webhook.payload_invalid")
    for field_name in ("request_id", "model_alias_id"):
        if not _is_bounded_safe_text(payload[field_name], maximum=128):
            # 请求和 Alias 标识只用于安全追踪，不允许空白、控制字符或无界正文。
            raise ValueError("webhook.payload_invalid")
    change_kind = payload["change_kind"]
    if change_kind not in ("created", "updated") or payload["status"] not in (
        "enabled",
        "disabled",
    ):
        # 生命周期和最终开关只接受设计枚举，未知值不能默认成 updated/enabled。
        raise ValueError("webhook.payload_invalid")
    field_order = ("status", "data_class", "minimum_prefix_chars", "ttl_seconds")
    changed_fields = payload["changed_fields"]
    if not _is_stable_subset(changed_fields, allowed_order=field_order, allow_empty=False):
        # 固定顺序去重可保证同一事务意图生成相同签名和下游幂等摘要。
        raise ValueError("webhook.payload_invalid")
    if change_kind == "created":
        if version != 1 or changed_fields != list(field_order):
            # 创建必须完整声明四个初始字段且从 v1 开始，禁止伪装成部分更新。
            raise ValueError("webhook.payload_invalid")
    elif version == 1:
        # updated 必须来自既有配置的后续版本，v1 更新没有可审计前态。
        raise ValueError("webhook.payload_invalid")


def _validate_cache_affinity_selected_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
) -> None:
    """绑定两种 CacheAffinity 事件的 event、actor、object 与 trace 信封身份。"""

    if object_id != payload["cache_affinity_id"] or not _is_bounded_safe_text(
        trace_id, maximum=128
    ):
        # object 漂移或缺 trace 会让订阅端无法把事件与本地事务安全关联，必须拒绝派发。
        raise ValueError("webhook.payload_invalid")
    if payload["trigger"] == "request_selected":
        if event_id != payload["cache_affinity_selection_id"] or actor_id is not None:
            # runtime 事件身份就是 cas_ Selection，且它由 Gateway 执行而不是控制台 actor 触发。
            raise ValueError("webhook.payload_invalid")
        return
    if (
        not _is_prefixed_lower_hex(event_id, prefix="cae_")
        or not _is_bounded_safe_text(actor_id, maximum=128)
    ):
        # 配置事件使用独立 cae_ 幂等 ID 并强制可信 actor，不能复用 runtime Selection 身份。
        raise ValueError("webhook.payload_invalid")


def _is_prefixed_lower_hex(value: object, *, prefix: str) -> bool:
    """判断对象 ID 是否由固定前缀和 32 位小写十六进制随机段组成。"""

    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and _is_lower_hex(value[len(prefix) :], expected_length=32)
    )


def _is_canonical_role_identifier(value: object) -> bool:
    """判断角色 ID 是否为当前随机格式或迁移保留的规范 UUID。

    参数：待校验的角色身份。返回：仅在 ``role_<32 hex>`` 或小写带连字符 UUID 时为真。
    约束：0050 必须保留更早版本的 UUID 主键，因此事件层需要兼容该历史事实；UUID 解析后
    仍要求逐字等于规范字符串，避免大写、无连字符、花括号或任意文本产生多种签名表示。
    """

    if type(value) is not str:
        return False
    # 当前触发器生成的前缀 ID 是首选合同，只有未命中时才进入历史 UUID 兼容路径。
    if _is_prefixed_lower_hex(value, prefix="role_"):
        return True
    try:
        canonical_uuid = str(UUID(value))
    # 解析失败只返回布尔结论，不能把攻击者提供的角色文本带入公开异常。
    except (ValueError, AttributeError):
        return False
    return canonical_uuid == value


def _validate_policy_dry_run_event_payload(payload: dict[str, Any]) -> None:
    """校验 ``policy.dry_run_executed`` 的精确安全 payload。

    字段集合必须与 PRD-046 完全一致；所有可空值也必须显式出现，便于接收端区分
    “没有命中/没有 would-error”和合同字段缺失。该校验只接受安全摘要，不读取 Prompt、
    data_class、模型输入、DSL/AST、预算余额或 Provider 配置。
    """

    expected_fields = {
        "policy_explanation_id",
        "request_id",
        "policy_set_fingerprint",
        "decision",
        "decision_reason_code",
        "matched_policy_id",
        "route_status",
        "dlp_status",
        "budget_status",
        "would_error_code",
        "checked_policy_count",
        "runtime_compatible",
        "coverage_complete",
    }
    if set(payload) != expected_fields:
        # 精确字段集同时拒绝缺字段和额外敏感字段，避免 Prompt 或租户配置被透传到 Webhook。
        raise ValueError("webhook.payload_invalid")

    explanation_id = payload["policy_explanation_id"]
    fingerprint = payload["policy_set_fingerprint"]
    if (
        type(explanation_id) is not str
        or len(explanation_id) != 37
        or not explanation_id.startswith("pexp_")
        or not _is_lower_hex(explanation_id[5:], expected_length=32)
    ):
        # explanation_id 必须是稳定的不透明标识，异常格式不能降级为可外发事件。
        raise ValueError("webhook.payload_invalid")
    if type(fingerprint) is not str or not _is_lower_hex(fingerprint, expected_length=64):
        # 指纹只接受固定小写摘要，拒绝原始策略内容或非规范摘要进入订阅链路。
        raise ValueError("webhook.payload_invalid")

    # request_id 是跨服务追踪边界，控制字符或超长值可能污染接收端日志，因此失败关闭。
    if not _is_bounded_safe_text(payload["request_id"], maximum=128):
        # 非法链路标识不能省略后继续派发，否则失败请求将无法可靠追踪。
        raise ValueError("webhook.payload_invalid")
    if not _is_bounded_safe_text(payload["decision_reason_code"], maximum=128):
        # 原因码必须保持为有界安全文本，不能借该字段外带策略表达式或 Prompt。
        raise ValueError("webhook.payload_invalid")
    matched_policy_id = payload["matched_policy_id"]
    if matched_policy_id is not None and not _is_bounded_safe_text(matched_policy_id, maximum=128):
        # 可空策略 ID 仍须满足有界文本合同，防止跨租户业务正文进入事件摘要。
        raise ValueError("webhook.payload_invalid")

    decision = payload["decision"]
    if type(decision) is not str or decision not in {
        "allow",
        "deny",
        "mask",
        "log_only",
        "route_to",
    }:
        # 决策仅允许稳定枚举，未知动作必须在生产端修复而不能由接收端猜测。
        raise ValueError("webhook.payload_invalid")
    route_status = payload["route_status"]
    if type(route_status) is not str or route_status not in {
        "selected",
        "unavailable",
        "not_evaluated",
    }:
        # 路由状态采用失败关闭白名单，禁止把 Provider 地址或内部故障正文伪装成状态。
        raise ValueError("webhook.payload_invalid")
    dlp_status = payload["dlp_status"]
    if type(dlp_status) is not str or dlp_status not in {
        "clean",
        "matched",
        "blocked",
        "not_evaluated",
    }:
        # DLP 状态只能外发分类结论，不允许携带命中原文或脱敏前片段。
        raise ValueError("webhook.payload_invalid")
    budget_status = payload["budget_status"]
    if type(budget_status) is not str or budget_status not in {
        "no_rules",
        "within_limit",
        "threshold_reached",
        "exhausted",
        "not_evaluated",
    }:
        # 预算事件只公开区间状态，拒绝余额、单价或其他越出租户摘要边界的值。
        raise ValueError("webhook.payload_invalid")

    would_error_code = payload["would_error_code"]
    if would_error_code is not None and (
        not _is_bounded_safe_text(would_error_code, maximum=128)
        or not would_error_code.startswith("gateway.")
    ):
        # would-error 只接受统一 Gateway 错误码，底层异常文本与 Traceback 不得外发。
        raise ValueError("webhook.payload_invalid")
    checked_policy_count = payload["checked_policy_count"]
    if type(checked_policy_count) is not int or not 0 <= checked_policy_count <= 200:
        # 策略计数必须与模拟器上限一致，异常数值视为合同损坏而不是自动截断。
        raise ValueError("webhook.payload_invalid")
    if type(payload["runtime_compatible"]) is not bool:
        # 兼容性必须是明确布尔结论，禁止 truthy 字符串造成接收端误判。
        raise ValueError("webhook.payload_invalid")
    # 本期缺少可信 Virtual Key/Application/KeyPool 上下文，覆盖完整性必须固定为 false。
    if payload["coverage_complete"] is not False:
        # 声称完整覆盖会误导治理决策，缺少运行上下文时必须拒绝整个事件。
        raise ValueError("webhook.payload_invalid")


def _validate_model_feedback_recorded_payload(payload: dict[str, Any]) -> None:
    """校验人工模型评分事件的精确安全载荷。

    该事件不是 PRD-049 的逐请求反馈，禁止加入 rating、accepted、tags、请求内容或
    模型输出；统计版本和样本数仅用于接收端判断人工先验发生时的观测水位。
    """

    expected_fields = {
        "trigger",
        "request_id",
        "model_quality_score_id",
        "model_id",
        "previous_manual_quality_bps",
        "manual_quality_bps",
        "feedback_version",
        "statistics_version",
        "sample_count",
    }
    if set(payload) != expected_fields or payload.get("trigger") != "manual_score_updated":
        # 精确字段集合阻止后续反馈正文或任意标签被顺手加入治理事件。
        raise ValueError("webhook.payload_invalid")
    for field in ("request_id", "model_quality_score_id", "model_id"):
        if not _is_bounded_safe_text(payload[field], maximum=128):
            # 评分对象和链路 ID 都会进入订阅端索引，控制字符或空值必须失败关闭。
            raise ValueError("webhook.payload_invalid")
    score_id = payload["model_quality_score_id"]
    if (
        type(score_id) is not str
        or len(score_id) != 36
        or not score_id.startswith("mqs_")
        or not _is_lower_hex(score_id[4:], expected_length=32)
    ):
        # Score ID 采用 mqs_ + 32 hex；其他文本不能冒充持久业务对象。
        raise ValueError("webhook.payload_invalid")
    for field in ("previous_manual_quality_bps", "manual_quality_bps"):
        score = payload[field]
        if score is not None and (type(score) is not int or not 0 <= score <= 10_000):
            # bool 不能借 int 继承通过，人工评分只允许基点整数或显式 null。
            raise ValueError("webhook.payload_invalid")
    feedback_version = payload["feedback_version"]
    statistics_version = payload["statistics_version"]
    sample_count = payload["sample_count"]
    if type(feedback_version) is not int or feedback_version < 2:
        # v1 是初始未变更状态，真实 feedback 事件最早只能形成 v2。
        raise ValueError("webhook.payload_invalid")
    if (
        type(statistics_version) is not int
        or type(sample_count) is not int
        or statistics_version < 0
        or sample_count < 0
        or statistics_version != sample_count
    ):
        # 当前版本每个纳入样本推进一次统计版本，事件必须冻结一致水位。
        raise ValueError("webhook.payload_invalid")
    if payload["previous_manual_quality_bps"] == payload["manual_quality_bps"]:
        # 同值 no-op 不得伪造为真实人工反馈事件。
        raise ValueError("webhook.payload_invalid")


def _validate_model_feedback_recorded_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
) -> None:
    """把人工评分事件的稳定 ID、管理员和 Score 对象与载荷交叉绑定。"""

    if (
        type(event_id) is not str
        or len(event_id) != 36
        or not event_id.startswith("mqe_")
        or not _is_lower_hex(event_id[4:], expected_length=32)
    ):
        # 随机 evt_ 无法与不可变 model_quality_score_events 行一一对应，必须拒绝。
        raise ValueError("webhook.payload_invalid")
    if object_id != payload["model_quality_score_id"]:
        # 信封对象和 payload Score 不一致会破坏接收端幂等归档。
        raise ValueError("webhook.payload_invalid")
    if not _is_bounded_safe_text(actor_id, maximum=128):
        # 人工评分必须由认证管理员负责，不能使用空 actor 或 Virtual Key 身份。
        raise ValueError("webhook.payload_invalid")
    if not _is_bounded_safe_text(trace_id, maximum=128):
        # 评分写入必须可由 trace_id 串联审计、事件和投递故障。
        raise ValueError("webhook.payload_invalid")


def _validate_request_feedback_created_payload(payload: dict[str, Any]) -> None:
    """校验逐请求反馈事件的精确七字段安全载荷。

    载荷只表达 RequestFeedback 对象、源调用、最终模型、actor 类型、评分、采纳和固定标签；
    不允许 operation request ID、actor ID、Provider、Prompt、Response、Token 或自由文本。
    """

    expected_fields = {
        "request_feedback_id",
        "request_id",
        "model_id",
        "actor_type",
        "rating",
        "accepted",
        "tags",
    }
    if set(payload) != expected_fields:
        # 精确字段集合让订阅端可稳定反序列化，并阻止内部身份或请求正文顺手外发。
        raise ValueError("webhook.payload_invalid")
    feedback_id = payload["request_feedback_id"]
    if (
        type(feedback_id) is not str
        or len(feedback_id) != 36
        or not feedback_id.startswith("rfb_")
        or not _is_lower_hex(feedback_id[4:], expected_length=32)
    ):
        # 对象 ID 必须能与不可变 request_feedbacks 行一一对应，普通文本不能冒充。
        raise ValueError("webhook.payload_invalid")
    for field in ("request_id", "model_id"):
        if not _is_bounded_safe_text(payload[field], maximum=128):
            # 源调用和模型 ID 会进入接收端索引，空白/控制字符必须在签名前拒绝。
            raise ValueError("webhook.payload_invalid")
    if payload["actor_type"] not in {"session_user", "virtual_key"}:
        # actor_type 只区分可信身份类别，不允许具体 user/key ID 混入 payload。
        raise ValueError("webhook.payload_invalid")
    rating = payload["rating"]
    if type(rating) is not int or not 1 <= rating <= 5:
        # bool 不能借 int 继承冒充评分，接收端只处理 1..5 的 JSON integer。
        raise ValueError("webhook.payload_invalid")
    if type(payload["accepted"]) is not bool:
        # 采纳必须是明确 JSON boolean，0/1、null 或字符串都会改变统计口径。
        raise ValueError("webhook.payload_invalid")
    _validate_request_feedback_tags(payload["tags"])


def _validate_request_feedback_created_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
) -> None:
    """把逐请求反馈的稳定事件、对象、可信 actor 和首次 trace 与 payload 绑定。"""

    if (
        type(event_id) is not str
        or len(event_id) != 36
        or not event_id.startswith("fbe_")
        or not _is_lower_hex(event_id[4:], expected_length=32)
    ):
        # 随机 evt_ 无法与 request_feedback_events 一一对应，重派时会破坏幂等。
        raise ValueError("webhook.payload_invalid")
    if object_id != payload["request_feedback_id"]:
        # 信封对象必须精确引用 payload 中的 RequestFeedback，禁止身份漂移。
        raise ValueError("webhook.payload_invalid")
    if not _is_bounded_safe_text(actor_id, maximum=128):
        # actor ID 只存在信封，必须来自持久 user/key 对象且不能为空或包含控制字符。
        raise ValueError("webhook.payload_invalid")
    if not _is_bounded_safe_text(trace_id, maximum=128):
        # 首次创建 trace 用于串联事务和投递故障，重放不能生成另一持久 trace。
        raise ValueError("webhook.payload_invalid")


def _validate_request_feedback_tags(value: object) -> None:
    """校验反馈事件标签的固定白名单、顺序、唯一性和三个互斥组。"""

    ordered_tags = (
        "accurate",
        "relevant",
        "complete",
        "helpful",
        "clear",
        "inaccurate",
        "irrelevant",
        "incomplete",
        "unsafe",
        "slow",
    )
    if type(value) is not list or len(value) > 5:
        # 标签必须是最多五项的 JSON array，自由文本或其他容器不得进入事件。
        raise ValueError("webhook.payload_invalid")
    if any(type(tag) is not str or tag not in ordered_tags for tag in value):
        # 未知标签可能携带业务正文或个人信息，因此不能作为可忽略扩展。
        raise ValueError("webhook.payload_invalid")
    positions = [ordered_tags.index(tag) for tag in value]
    if positions != sorted(set(positions)):
        # 重复或乱序标签会破坏 canonical payload 与接收端幂等比较。
        raise ValueError("webhook.payload_invalid")
    selected = set(value)
    if any(
        pair.issubset(selected)
        for pair in (
            {"accurate", "inaccurate"},
            {"relevant", "irrelevant"},
            {"complete", "incomplete"},
        )
    ):
        # 同一维度正反标签不能并存，否则事件语义自相矛盾并污染报表。
        raise ValueError("webhook.payload_invalid")


def _validate_routing_policy_selected_event_payload(payload: dict[str, Any]) -> None:
    """校验 ``route.policy_selected`` 的运行时与配置变更两种精确 payload。

    两种触发都必须显式包含所有可空字段，且不能携带 Key 数量、价格正文、Provider URL、
    Prompt/Response 或 Virtual Key 标识。稳定 event_id 由外层统一校验，保证提交后重派幂等。
    """

    trigger = payload.get("trigger")
    if trigger == "request_selected":
        _validate_route_request_selected_payload(payload)
        return
    if trigger == "configuration_changed":
        _validate_route_configuration_changed_payload(payload)
        return
    # 未知 trigger 不能通过字段猜测降级到任一变体，否则接收端无法稳定反序列化。
    raise ValueError("webhook.payload_invalid")


def _validate_routing_policy_selected_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
) -> None:
    """把路由事件信封身份与已经校验的 payload 权威字段交叉绑定。

    运行时 Virtual Key 不是人类 actor，因此必须为空；配置变更必须携带认证用户 ID。
    两种事件都引用同一个 RoutingPolicy object，并要求稳定 trace 便于投递失败排查。
    """

    if (
        type(event_id) is not str
        or len(event_id) != 36
        or not event_id.startswith("rpe_")
        or not _is_lower_hex(event_id[4:], expected_length=32)
    ):
        # 随机 evt_ 或短 ID 无法与 routing_decisions/events 持久事实一一对应，必须拒绝。
        raise ValueError("webhook.payload_invalid")
    routing_policy_id = payload["routing_policy_id"]
    if object_id != routing_policy_id or not _is_bounded_safe_text(object_id, maximum=128):
        # 信封 object 与 payload Policy 不一致会破坏订阅端幂等归档和租户内追踪。
        raise ValueError("webhook.payload_invalid")
    if not _is_bounded_safe_text(trace_id, maximum=128):
        # 所有真实选择和配置写入都有 trace_id，缺失时不能产生不可串联的治理事件。
        raise ValueError("webhook.payload_invalid")
    if payload["trigger"] == "request_selected":
        if actor_id is not None:
            # Virtual Key 不能冒充人类 actor，运行时调用归属只留在 tenant-qualified 日志。
            raise ValueError("webhook.payload_invalid")
        return
    if not _is_bounded_safe_text(actor_id, maximum=128):
        # configuration_changed 必须由认证管理员负责，空 actor 会形成无法问责的配置事实。
        raise ValueError("webhook.payload_invalid")


def _validate_route_request_selected_payload(payload: dict[str, Any]) -> None:
    """校验真实 Alias 初始选路提交后产生的安全事件摘要。"""

    expected_fields = {
        "trigger",
        "request_id",
        "routing_decision_id",
        "model_alias_id",
        "routing_policy_id",
        "routing_policy_version",
        "routing_mode",
        "selected_model_id",
        "selected_provider_id",
        "candidate_count",
        "eligible_candidate_count",
        "selection_reason",
        "project_id",
    }
    # 字段集合必须精确匹配，额外字段可能把价格、Key 或请求正文扩散到订阅端。
    if set(payload) != expected_fields:
        raise ValueError("webhook.payload_invalid")
    for field in (
        "request_id",
        "routing_decision_id",
        "model_alias_id",
        "routing_policy_id",
        "selected_model_id",
        "selected_provider_id",
        "selection_reason",
    ):
        # 运行时关联 ID 和原因码只允许有界安全文本，不能借字段外带正文或控制字符。
        if not _is_bounded_safe_text(payload[field], maximum=128):
            raise ValueError("webhook.payload_invalid")
    project_id = payload["project_id"]
    # 项目归属可空，但非空时仍必须是安全 ID，且不能由请求正文覆盖可信上下文。
    if project_id is not None and not _is_bounded_safe_text(project_id, maximum=128):
        raise ValueError("webhook.payload_invalid")
    _validate_routing_policy_identity(payload)
    candidate_count = payload["candidate_count"]
    eligible_count = payload["eligible_candidate_count"]
    # 候选计数严格受 Alias 主备上限约束，bool 不能借 Python 整数继承通过校验。
    if type(candidate_count) is not int or not 1 <= candidate_count <= 2:
        raise ValueError("webhook.payload_invalid")
    # 合格数必须是候选总数内的正整数，selected 事件不允许没有可执行候选。
    if type(eligible_count) is not int or not 1 <= eligible_count <= candidate_count:
        raise ValueError("webhook.payload_invalid")
    # 选择原因必须是共享算法的稳定机器码，禁止使用任意错误正文或自然语言。
    if not str(payload["selection_reason"]).startswith("routing."):
        raise ValueError("webhook.payload_invalid")


def _validate_route_configuration_changed_payload(payload: dict[str, Any]) -> None:
    """校验 Alias 创建或路由语义真实变化后的持久配置事件摘要。"""

    legacy_fields = {
        "trigger",
        "request_id",
        "model_alias_id",
        "routing_policy_id",
        "routing_policy_version",
        "change_kinds",
        "changed_routing_fields",
        "previous_mode",
        "routing_mode",
    }
    v2_fields = legacy_fields | {"configuration_schema_version", "quality_cost_config"}
    is_v2 = set(payload) == v2_fields
    # 存量 v1 事件原文必须可重派；0037 新事件携带 schema=2 和完整休眠质量配置。
    if set(payload) != legacy_fields and not is_v2:
        raise ValueError("webhook.payload_invalid")
    if is_v2:
        if (
            type(payload["configuration_schema_version"]) is not int
            or payload["configuration_schema_version"] != 2
        ):
            # bool 也不会等同合法 schema 意图；显式类型检查避免未来值兼容漂移。
            raise ValueError("webhook.payload_invalid")
        _validate_quality_cost_config_payload(payload["quality_cost_config"])
    elif (
        payload.get("routing_mode") == "quality_cost"
        or payload.get("previous_mode") == "quality_cost"
    ):
        # quality_cost 从 0037 才存在，不能伪造成缺少配置快照的 v1 持久事件。
        raise ValueError("webhook.payload_invalid")
    for field in ("request_id", "model_alias_id", "routing_policy_id"):
        # 请求、Alias 和 Policy 标识用于幂等恢复，控制字符会破坏下游日志关联。
        if not _is_bounded_safe_text(payload[field], maximum=128):
            raise ValueError("webhook.payload_invalid")
    _validate_routing_policy_identity(payload)

    change_order = (
        "created",
        "mode",
        *(('quality_cost',) if is_v2 else ()),
        "candidates",
        "alias_state",
    )
    field_order = (
        "alias",
        "environment",
        "environment_id",
        "status",
        "default_model_id",
        "fallback_model_id",
        "routing_mode",
        *(("quality_cost_config",) if is_v2 else ()),
    )
    change_kinds = payload["change_kinds"]
    changed_fields = payload["changed_routing_fields"]
    # 数组必须按固定业务顺序去重，避免同一配置事实生成不同签名正文。
    if not _is_stable_subset(change_kinds, allowed_order=change_order, allow_empty=False):
        raise ValueError("webhook.payload_invalid")
    # 字段名数组采用独立白名单，不能把变更前后值伪装成字段名称外发。
    if not _is_stable_subset(changed_fields, allowed_order=field_order, allow_empty=False):
        raise ValueError("webhook.payload_invalid")
    # 创建是独立生命周期事件；更新可在同一事务同时改变 mode、候选和 Alias 状态。
    # 创建与更新属于互斥生命周期，混在同一数组会让接收端无法解释 previous_mode。
    if "created" in change_kinds and change_kinds != ["created"]:
        raise ValueError("webhook.payload_invalid")
    previous_mode = payload["previous_mode"]
    # 前态模式只能为空或三种强枚举，未知值表示持久配置事件已损坏。
    if previous_mode is not None and previous_mode not in {
        "fixed",
        "cost_first",
        "availability_first",
        "quality_cost",
    }:
        # 非法前态无法对应已提交策略版本，必须拒绝整个持久事件而不是清空字段。
        raise ValueError("webhook.payload_invalid")
    if change_kinds == ["created"]:
        # 新建 Policy 没有前态，伪造 previous_mode 会形成不存在的版本迁移。
        if previous_mode is not None:
            raise ValueError("webhook.payload_invalid")
        expected_created_fields = list(field_order)
        if changed_fields != expected_created_fields:
            # 创建事件必须完整声明初始 Alias 图、模式和 v2 休眠质量配置。
            raise ValueError("webhook.payload_invalid")
        # 创建事件允许把初始 routing_mode 列入 changed fields；它不是一次旧模式到新模式的更新。
        return
    # 所有更新事件都必须显式给出前态，不能让接收端从 change_kinds 猜测。
    elif previous_mode is None:
        raise ValueError("webhook.payload_invalid")
    if "mode" in change_kinds:
        # mode 变更必须同时改变字段值，防止 no-op 被误投递为真实配置事件。
        if "routing_mode" not in changed_fields or previous_mode == payload["routing_mode"]:
            raise ValueError("webhook.payload_invalid")
    # 非 mode 更新必须维持相同模式且不能把 routing_mode 混入 changed_fields。
    elif "routing_mode" in changed_fields or previous_mode != payload["routing_mode"]:
        raise ValueError("webhook.payload_invalid")
    if is_v2:
        quality_changed = "quality_cost_config" in changed_fields
        if ("quality_cost" in change_kinds) != quality_changed:
            # 配置 change kind 与逻辑字段必须一一对应，不能只改其中一个数组。
            raise ValueError("webhook.payload_invalid")


def _validate_quality_cost_config_payload(value: object) -> None:
    """校验 RoutingPolicy v2 事件中的完整基点权重和门槛快照。"""

    expected = {
        "quality_weight_bps",
        "cost_weight_bps",
        "latency_weight_bps",
        "quality_floor_bps",
        "minimum_sample_count",
    }
    # 配置快照字段必须精确完整，缺字段或额外字段都会让订阅端无法复现策略语义。
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("webhook.payload_invalid")
    if any(type(value[field]) is not int for field in expected):
        # bool 不能冒充权重或样本数，接收端必须得到精确整数合同。
        raise ValueError("webhook.payload_invalid")
    quality = value["quality_weight_bps"]
    cost = value["cost_weight_bps"]
    latency = value["latency_weight_bps"]
    floor = value["quality_floor_bps"]
    samples = value["minimum_sample_count"]
    if (
        not 1 <= quality <= 10_000
        or not 0 <= cost <= 10_000
        or not 0 <= latency <= 10_000
        or quality + cost + latency != 10_000
        or not 1 <= floor <= 10_000
        or not 1 <= samples <= 10_000
    ):
        # 越界或权重和漂移不能被截断重算，否则事件会偏离数据库权威 Policy 版本。
        raise ValueError("webhook.payload_invalid")


def _validate_routing_policy_identity(payload: dict[str, Any]) -> None:
    """校验两种路由事件共用的严格版本和模式字段。"""

    version = payload["routing_policy_version"]
    mode = payload["routing_mode"]
    # 版本只接受真正正整数，bool 或零值无法对应数据库中的不可变策略版本。
    if type(version) is not int or version < 1:
        raise ValueError("webhook.payload_invalid")
    # 未知模式代表生产端合同损坏，不能默认 fixed 后继续签名外发。
    if type(mode) is not str or mode not in {
        "fixed",
        "cost_first",
        "availability_first",
        "quality_cost",
    }:
        # 未知当前模式不能默认 fixed，否则签名事件将与数据库权威事实不一致。
        raise ValueError("webhook.payload_invalid")


def _is_stable_subset(
    value: object,
    *,
    allowed_order: tuple[str, ...],
    allow_empty: bool,
) -> bool:
    """判断字符串数组是否为允许集合的唯一、有序子序列。"""

    if type(value) is not list or (not value and not allow_empty):
        return False
    if any(type(item) is not str for item in value):
        return False
    positions = {item: index for index, item in enumerate(allowed_order)}
    try:
        indexes = [positions[item] for item in value]
    # 未知数组项不是可忽略扩展，否则旧接收端可能把事件解释为另一类变更。
    except KeyError:
        return False
    return indexes == sorted(set(indexes))


def _is_lower_hex(value: str, *, expected_length: int) -> bool:
    """判断字符串是否为指定长度的小写十六进制摘要，不接受大写或缩写。"""

    return len(value) == expected_length and all(
        character in "0123456789abcdef" for character in value
    )


def _is_bounded_safe_text(value: object, *, maximum: int) -> bool:
    """校验事件标识/原因码的有界文本，拒绝空白和控制字符。"""

    return (
        type(value) is str
        and 1 <= len(value) <= maximum
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _validate_adapter_spec_updated_payload(payload: dict[str, Any]) -> None:
    """校验 ``adapter.spec_updated`` 的精确五字段目录切换摘要。

    参数：已经通过通用敏感键扫描的 payload；返回：合法时无返回值。
    约束：事件只能说明版本身份发生切换，不能携带 spec 正文、Endpoint、Header、凭据或
    厂商错误正文；当前与上一版本必须不同，否则同版本幂等请求会伪造成更新事件。
    """

    expected_fields = {
        "catalog_version",
        "catalog_hash",
        "previous_catalog_version",
        "spec_count",
        "activated_at",
    }
    if set(payload) != expected_fields:
        # 缺字段或额外字段会改变订阅方对目录切换的解释，必须在事件签名和投递前失败关闭。
        raise ValueError("webhook.payload_invalid")
    for field in ("catalog_version", "previous_catalog_version"):
        value = payload[field]
        if (
            type(value) is not str
            or not 3 <= len(value) <= 80
            or not value[0].islower()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
                for character in value
            )
        ):
            # 版本只允许受控 ASCII 短码，不能把自由说明或 URL 填进事件。
            raise ValueError("webhook.payload_invalid")
    if payload["catalog_version"] == payload["previous_catalog_version"]:
        # 同一版本不构成 spec_updated，幂等发布必须保持静默。
        raise ValueError("webhook.payload_invalid")
    catalog_hash = payload["catalog_hash"]
    if type(catalog_hash) is not str or not _is_lower_hex(catalog_hash, expected_length=64):
        # 目录身份必须是完整小写 SHA-256，禁止缩写或可逆正文冒充 hash。
        raise ValueError("webhook.payload_invalid")
    spec_count = payload["spec_count"]
    if type(spec_count) is not int or not 1 <= spec_count <= 64:
        # bool 不能冒充数量，有界上限也阻止异常目录放大订阅端处理成本。
        raise ValueError("webhook.payload_invalid")
    if not _is_canonical_utc_second(payload["activated_at"]):
        # UTC 秒会与 envelope timestamp 精确绑定，其他格式会破坏签名和重放排序。
        raise ValueError("webhook.payload_invalid")


def _validate_adapter_spec_updated_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把目录切换事件绑定到同租户 activation 与固定内部发布身份。

    参数：已验证 payload 和标准事件信封字段；返回：全部归属一致时无返回值。
    约束：事件 ID 必须由 activation ID 稳定派生，未来时间、随机 actor 或缺失链路都失败
    关闭，防止跨租户激活事实被合法签名后投递。
    """

    valid_object = (
        type(object_id) is str
        and object_id.startswith("paca_")
        and _is_lower_hex(object_id[5:], expected_length=32)
    )
    if (
        not valid_object
        or event_id != f"evt_{object_id}"
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or actor_id != "adapter-spec-publisher"
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=160)
        or occurred_at.tzinfo is None
        or _to_iso(occurred_at) != payload["activated_at"]
        or occurred_at.astimezone(UTC) > datetime.now(UTC) + timedelta(seconds=1)
    ):
        # 不返回具体漂移字段，避免内部租户、对象或链路信息进入异常正文。
        raise ValueError("webhook.payload_invalid")


def _validate_adapter_contract_failed_payload(payload: dict[str, Any]) -> None:
    """校验 ``adapter.contract_failed`` 的精确九字段失败摘要。

    参数：已经通过通用敏感键扫描的 payload；返回：合法时无返回值。
    约束：failure_codes 必须有序、唯一且来自 runner 固定白名单；事件不得携带 case、
    assertion、Endpoint、Header、凭据、Prompt、Response 或厂商错误正文。
    """

    expected_fields = {
        "run_id",
        "catalog_version",
        "catalog_hash",
        "suite_kind",
        "mode",
        "total_cases",
        "failed_cases",
        "failure_codes",
        "completed_at",
    }
    if set(payload) != expected_fields:
        # 精确字段集合禁止额外正文或遗漏摘要，避免敏感内容随失败事件出网。
        raise ValueError("webhook.payload_invalid")
    run_id = payload["run_id"]
    if (
        type(run_id) is not str
        or not run_id.startswith("acr_")
        or not _is_lower_hex(run_id[4:], expected_length=32)
    ):
        # run 身份必须使用固定前缀和小写哈希，防止事件对象产生多义表示。
        raise ValueError("webhook.payload_invalid")
    catalog_version = payload["catalog_version"]
    if (
        type(catalog_version) is not str
        or not 1 <= len(catalog_version) <= 80
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
               for character in catalog_version)
    ):
        # 目录版本只允许有界安全字符，禁止控制字符或路径片段进入签名载荷。
        raise ValueError("webhook.payload_invalid")
    catalog_hash = payload["catalog_hash"]
    if type(catalog_hash) is not str or not _is_lower_hex(catalog_hash, expected_length=64):
        # 目录哈希必须是完整小写 SHA-256，拒绝缩写和大小写等价编码。
        raise ValueError("webhook.payload_invalid")
    suite_kind = payload["suite_kind"]
    mode = payload["mode"]
    if (suite_kind, mode) not in {("merge_gate", "mock"), ("probe", "gateway_probe")}:
        # merge gate 和显式真实探测的覆盖语义不可混用。
        # 固定组合避免 probe 子集被接收端误判为完整发布门禁证据。
        raise ValueError("webhook.payload_invalid")
    total_cases = payload["total_cases"]
    failed_cases = payload["failed_cases"]
    if (
        type(total_cases) is not int
        or type(failed_cases) is not int
        or not 1 <= total_cases <= 2048
        or not 1 <= failed_cases <= total_cases
    ):
        # 失败事件必须至少有一项失败，且计数不能超过报告合同的硬上限。
        raise ValueError("webhook.payload_invalid")
    failure_codes = payload["failure_codes"]
    if (
        type(failure_codes) is not list
        or not 1 <= len(failure_codes) <= 16
        or any(type(code) is not str for code in failure_codes)
        or failure_codes != sorted(set(failure_codes))
        or any(code not in ADAPTER_CONTRACT_FAILURE_CODES for code in failure_codes)
    ):
        # 失败码只接受有序唯一白名单，禁止自由文本或上游响应进入 Webhook。
        raise ValueError("webhook.payload_invalid")
    if not _is_canonical_utc_second(payload["completed_at"]):
        # 完成时间必须是规范 UTC 秒，才能与不可变 run 和事件时间精确绑定。
        raise ValueError("webhook.payload_invalid")


def _validate_adapter_contract_failed_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把合同失败事件绑定到同租户不可变 run 与首次上报链路。

    事件 ID 由 run ID 稳定派生；固定 actor 代表受控 runner，而不是可伪造的用户角色。
    完成时间必须与 envelope timestamp 同秒，未来事件在签名前失败关闭。
    """

    run_id = payload["run_id"]
    if (
        object_id != run_id
        or event_id != f"evt_{run_id}"
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or actor_id != "adapter-contract-runner"
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=160)
        or occurred_at.tzinfo is None
        or _to_iso(occurred_at) != payload["completed_at"]
        or occurred_at.astimezone(UTC) > datetime.now(UTC) + timedelta(seconds=1)
    ):
        # 不指出具体漂移字段，避免对象、租户或链路信息进入异常正文。
        # 任一信封身份漂移都统一拒绝，不能向调用方透露是哪一项链路字段不匹配。
        raise ValueError("webhook.payload_invalid")


def _validate_provider_error_map_payload_size(payload: object) -> None:
    """先校验错误映射事件的 canonical 字节上限，避免递归复制无界 source payload。

    参数：待构造 Webhook 的原始 payload。返回：合法时无返回值。
    约束：序列化失败、非有限数字或超过 4096 UTF-8 bytes 都使用固定错误，绝不回显原值。
    """

    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    # 序列化异常可能携带任意对象表示，统一转换后才能保证日志和响应不泄露源值。
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        # 原始异常可能带出非法对象 repr，因此只抛固定 Webhook 合同错误。
        raise ValueError("webhook.payload_invalid") from None
    if len(encoded) > 4096:
        # Source Outbox 与接收端共同依赖固定上限，不能在签名阶段再截断或丢字段。
        raise ValueError("webhook.payload_invalid")


def _validate_provider_error_map_updated_payload(payload: dict[str, Any]) -> None:
    """校验 error_map.updated 的精确、扁平且不含 pattern 的安全 payload。"""

    expected_fields = {
        "schema_version",
        "operation",
        "provider_error_map_id",
        "rule_version",
        "head_revision",
        "provider_scope",
        "status",
        "priority",
        "output_error_code",
        "pattern_hash",
        "request_id",
    }
    _validate_provider_error_map_payload_size(payload)
    if set(payload) != expected_fields:
        # 精确字段集同时拒绝 pattern、原始正文、Provider Endpoint 和 actor 邮箱等扩展。
        raise ValueError("webhook.payload_invalid")
    if payload["schema_version"] != 1 or type(payload["schema_version"]) is not int:
        raise ValueError("webhook.payload_invalid")
    operation = payload["operation"]
    if type(operation) is not str or operation not in {
        "create",
        "update",
        "enable",
        "disable",
    }:
        # operation 决定订阅方如何推进缓存，未知动作必须失败关闭而不能按 update 猜测。
        raise ValueError("webhook.payload_invalid")
    # 对象 ID 必须是平台生成的固定前缀标识，防止跨资源类型混淆订阅事件。
    if not _is_prefixed_lower_hex(payload["provider_error_map_id"], prefix="pem_"):
        raise ValueError("webhook.payload_invalid")
    for field_name in ("rule_version", "head_revision"):
        value = payload[field_name]
        # bool 是 int 子类但不是合法版本；上界与数据库 revision/head 门禁保持一致。
        if type(value) is not int or not 1 <= value <= 2_147_483_647:
            raise ValueError("webhook.payload_invalid")
    provider_scope = payload["provider_scope"]
    # Provider scope 只允许显式 all 或有界安全 ID，不能把名称、Endpoint 等信息放入事件。
    if provider_scope != "all" and not _is_bounded_safe_text(provider_scope, maximum=128):
        raise ValueError("webhook.payload_invalid")
    status = payload["status"]
    # 状态值与数据库枚举保持同一闭集，订阅方无需对未知状态做不确定回退。
    if type(status) is not str or status not in {"active", "disabled"}:
        raise ValueError("webhook.payload_invalid")
    # 启用动作必须携带 active，动作与结果矛盾时拒绝事件，避免缓存状态倒置。
    if operation == "enable" and status != "active":
        raise ValueError("webhook.payload_invalid")
    # 禁用动作必须携带 disabled，不能让消费方根据到达顺序自行推断最终状态。
    if operation == "disable" and status != "disabled":
        raise ValueError("webhook.payload_invalid")
    # 优先级必须与控制面持久化范围一致，bool 也不能借用整数子类关系通过。
    if type(payload["priority"]) is not int or not 1 <= payload["priority"] <= 1000:
        raise ValueError("webhook.payload_invalid")
    output_error_code = payload["output_error_code"]
    if (
        type(output_error_code) is not str
        or output_error_code not in TENANT_PROVIDER_ERROR_OUTPUT_CODES
    ):
        # 租户规则只能输出批准的公开错误码，禁止把上游原始 code 传播给 Webhook 接收方。
        raise ValueError("webhook.payload_invalid")
    pattern_hash = payload["pattern_hash"]
    # 事件只携带不可逆摘要；固定长度十六进制门禁防止误把规则 pattern 写入载荷。
    if type(pattern_hash) is not str or not _is_lower_hex(pattern_hash, expected_length=64):
        raise ValueError("webhook.payload_invalid")
    # request_id 用于关联不可变 Source Outbox 行，必须保持有界安全文本合同。
    if not _is_bounded_safe_text(payload["request_id"], maximum=128):
        raise ValueError("webhook.payload_invalid")


def _validate_provider_error_map_updated_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
) -> None:
    """把错误映射 source event 绑定到稳定事件、租户、actor、对象与 trace 身份。"""

    if (
        not _is_prefixed_lower_hex(event_id, prefix="pemevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=128)
        or not _is_bounded_safe_text(actor_id, maximum=128)
        or object_id != payload["provider_error_map_id"]
        or not _is_bounded_safe_text(trace_id, maximum=128)
        or source_request_id != payload["request_id"]
    ):
        # 信封任何身份缺失都会破坏幂等投递和租户追踪，不能降级为随机 evt_。
        raise ValueError("webhook.payload_invalid")


def _validate_audit_exported_payload(payload: dict[str, Any]) -> None:
    """校验 `audit.exported` 的固定八字段复核摘要。

    参数：已通过通用敏感键扫描的 payload；返回：合法时无返回值。
    约束：不得扩展筛选原文、CSV、邮箱、IP、User-Agent 或数据库位置；摘要与规模必须可直接
    对照不可变 AuditExport，不能由订阅端猜测类型或范围。
    """

    expected_fields = {
        "audit_export_id",
        "status",
        "format",
        "schema_version",
        "row_count",
        "content_size_bytes",
        "content_sha256",
        "filter_fingerprint",
    }
    if set(payload) != expected_fields:
        # 精确键集合阻止未来 DTO 自动把 filters_json、文件名或内部状态扩散到租户 Endpoint。
        raise ValueError("webhook.payload_invalid")
    if not _is_prefixed_lower_hex(payload["audit_export_id"], prefix="aex_"):
        # 对象 ID 必须与 AuditExport 固定前缀身份一致，禁止通用 ID 冒充。
        raise ValueError("webhook.payload_invalid")
    if payload["status"] != "completed" or type(payload["status"]) is not str:
        # 同步导出只发布不可变完成事实，不允许 pending/failed 或布尔值混入。
        raise ValueError("webhook.payload_invalid")
    if payload["format"] != "csv" or type(payload["format"]) is not str:
        # MVP 信封只登记 CSV，不能把该事件扩展成任意文件生成器。
        raise ValueError("webhook.payload_invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        # 精确整数 v1 冻结列顺序，bool 和未来版本都必须显式升级合同。
        raise ValueError("webhook.payload_invalid")
    row_count = payload["row_count"]
    content_size = payload["content_size_bytes"]
    # bool 是 int 子类，必须用精确类型避免 true/false 混入签名数值合同。
    if type(row_count) is not int or not 0 <= row_count <= 10_000:
        # 行数超界或类型漂移会让订阅端无法复核本地 CSV。
        raise ValueError("webhook.payload_invalid")
    if type(content_size) is not int or not 1 <= content_size <= 16 * 1024 * 1024:
        # 字节数包含 BOM/表头且必须落在同步导出硬上限内。
        raise ValueError("webhook.payload_invalid")
    for field_name in ("content_sha256", "filter_fingerprint"):
        value = payload[field_name]
        # 复核摘要只接受完整小写 SHA-256，拒绝大写、缩写和任意可逆筛选文本。
        if type(value) is not str or not _is_lower_hex(value, expected_length=64):
            # 非完整 hash 无法复核且可能夹带可逆筛选或文件内容。
            raise ValueError("webhook.payload_invalid")


def _validate_audit_exported_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把审计导出 payload 绑定到持久 source event 与可信租户操作主体。"""

    if (
        not _is_prefixed_lower_hex(event_id, prefix="aexevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=128)
        or not _is_bounded_safe_text(actor_id, maximum=128)
        or object_id != payload["audit_export_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=160)
        or occurred_at.tzinfo is None
    ):
        # 任一身份漂移都会破坏租户隔离、幂等恢复或复核链路，必须在 source 事务提交前失败关闭。
        raise ValueError("webhook.payload_invalid")


def _validate_config_imported_payload(payload: dict[str, Any]) -> None:
    """校验 ``config.imported`` 的固定十四字段安全批次摘要。

    参数：已通过通用敏感键扫描的 payload；返回：合法时无返回值。
    约束：禁止配置名称、URL、description、行内容和任何 Key/Token；四类计数必须与
    ConfigExport 的硬上限一致，generated/disabled 必须精确分割新建 Virtual Key。
    """

    expected_fields = {
        "config_import_id",
        "schema_version",
        "manifest_sha256",
        "providers_created",
        "providers_unchanged",
        "models_created",
        "models_unchanged",
        "aliases_created",
        "aliases_unchanged",
        "virtual_keys_created",
        "virtual_keys_unchanged",
        "generated_virtual_keys",
        "disabled_virtual_keys",
        "applied_at",
    }
    # 精确字段集阻止配置名称、URL、描述或未知扩展绕过上游白名单进入签名正文。
    if set(payload) != expected_fields:
        raise ValueError("webhook.payload_invalid")
    import_id = payload["config_import_id"]
    try:
        canonical_uuid = str(UUID(str(import_id)))
    # UUID 解析异常不得携带攻击者原值；订阅端只需要稳定 payload_invalid 结论。
    except (TypeError, ValueError, AttributeError):
        raise ValueError("webhook.payload_invalid") from None
    if type(import_id) is not str or canonical_uuid != import_id:
        # 批次必须引用控制面生成的 canonical UUID，任意业务名称不能冒充对象身份。
        raise ValueError("webhook.payload_invalid")
    # schema 必须逐字冻结，避免订阅端用 v1 规则误读未来字段或计数含义。
    if payload["schema_version"] != "metahub.config/v1":
        raise ValueError("webhook.payload_invalid")
    manifest = payload["manifest_sha256"]
    # manifest 只能是不可逆的完整小写 SHA-256，禁止正文或缩写摘要伪装成批次指纹。
    if type(manifest) is not str or not _is_lower_hex(manifest, expected_length=64):
        raise ValueError("webhook.payload_invalid")

    limits = {
        "providers": 500,
        "models": 2_000,
        "aliases": 2_000,
        "virtual_keys": 2_000,
    }
    total = 0
    for collection, maximum in limits.items():
        created = payload[f"{collection}_created"]
        unchanged = payload[f"{collection}_unchanged"]
        if (
            type(created) is not int
            or type(unchanged) is not int
            or created < 0
            or unchanged < 0
            or created + unchanged > maximum
        ):
            # bool 不能借 int 子类关系通过；接收端无需处理负数或超限批次。
            raise ValueError("webhook.payload_invalid")
        total += created + unchanged
    # 四类对象合计复用导入端硬上限，订阅端不接受计数正确但总体资源边界失真的事件。
    if total > 5_000:
        raise ValueError("webhook.payload_invalid")
    generated = payload["generated_virtual_keys"]
    disabled = payload["disabled_virtual_keys"]
    # 每条新建 Key 必须被精确归入“已生成一次性明文”或“停用且不交付明文”，不得遗漏或重叠。
    if (
        type(generated) is not int
        or type(disabled) is not int
        or generated < 0
        or disabled < 0
        or generated + disabled != payload["virtual_keys_created"]
    ):
        # 分类计数漂移会让一次性明文交付事实不可复核，因此拒绝整个签名事件。
        raise ValueError("webhook.payload_invalid")
    # 提交时间参与 envelope 逐字复核，只接受无歧义的 UTC 秒级格式。
    if not _is_canonical_utc_second(payload["applied_at"]):
        raise ValueError("webhook.payload_invalid")


def _validate_config_imported_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把配置导入摘要绑定到持久来源事件及可信租户操作主体。

    参数：已验证 payload，以及 source event 的事件、租户、actor、对象、链路和时间字段。
    返回：全部归属逐字一致时无返回值，不一致时抛出固定 ``webhook.payload_invalid``。
    约束：任何身份或时间漂移都必须在 source 事务提交前失败，不能补默认租户或复用当前请求时间。
    """

    import_id = payload["config_import_id"]
    # payload 合法不能替代 envelope 归属校验；逐项绑定可防止跨租户事件或错误批次被合法签名。
    if (
        event_id != f"evt_{import_id}"
        or not _is_bounded_safe_text(tenant_id, maximum=160)
        or not _is_bounded_safe_text(actor_id, maximum=160)
        or object_id != import_id
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=160)
        or occurred_at.tzinfo is None
        or _to_iso(occurred_at) != payload["applied_at"]
    ):
        # 任一归属或链路漂移都可能把合法摘要绑定到错误租户，必须整体失败关闭。
        raise ValueError("webhook.payload_invalid")


def _validate_role_permission_changed_payload(payload: dict[str, Any]) -> None:
    """校验 ``role.permission_changed`` 的固定九字段安全摘要。

    参数：已通过通用敏感键扫描的 payload；返回：合法时无返回值。
    约束：只披露角色身份、相邻 revision、前后哈希及规范 diff，不包含邮箱、完整矩阵、
    页面状态、Token 或其他租户信息。
    """

    expected_fields = {
        "schema_version",
        "role_id",
        "role_code",
        "before_revision",
        "after_revision",
        "before_sha256",
        "after_sha256",
        "added_permissions",
        "removed_permissions",
    }
    if set(payload) != expected_fields:
        # 字段集合不一致只返回稳定错误码，不能把未知字段名、完整矩阵或正文拼入异常。
        raise ValueError("webhook.payload_invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        # bool 是 int 子类，必须要求精确整数以冻结事件 schema。
        raise ValueError("webhook.payload_invalid")
    if not _is_canonical_role_identifier(payload["role_id"]):
        # 只兼容 0050 保留的规范 UUID；新格式、历史格式之外的自由文本仍须失败关闭。
        raise ValueError("webhook.payload_invalid")
    role_code = payload["role_code"]
    # admin 矩阵不可变，因此不可能产生合法修改事件；角色也必须来自固定产品目录。
    if type(role_code) is not str or role_code not in ROLE_CODES or role_code == ADMIN_ROLE:
        raise ValueError("webhook.payload_invalid")
    before_revision = payload["before_revision"]
    after_revision = payload["after_revision"]
    if (
        type(before_revision) is not int
        or before_revision < 1
        or type(after_revision) is not int
        or after_revision != before_revision + 1
    ):
        # 真实 PATCH 只能推进一个 CAS revision，跳号或倒退说明事件与数据库事实不一致。
        raise ValueError("webhook.payload_invalid")
    before_hash = payload["before_sha256"]
    after_hash = payload["after_sha256"]
    if (
        type(before_hash) is not str
        or type(after_hash) is not str
        or not _is_lower_hex(before_hash, expected_length=64)
        or not _is_lower_hex(after_hash, expected_length=64)
        or before_hash == after_hash
    ):
        # 变更事件要求两个不同规范哈希，避免 no-op 被伪装成可审计权限变化。
        raise ValueError("webhook.payload_invalid")
    added = _canonical_role_permission_diff(payload["added_permissions"])
    removed = _canonical_role_permission_diff(payload["removed_permissions"])
    if (not added and not removed) or set(added) & set(removed):
        # 真实变更必须至少有一个 diff，且同一权限不能同时增删。
        raise ValueError("webhook.payload_invalid")


def _canonical_role_permission_diff(value: object) -> tuple[str, ...]:
    """校验权限事件中的一个已排序、唯一且有界 diff 列表。"""

    # diff 必须是有界 JSON 数组，禁止迭代器或超目录长度输入放大签名正文。
    if type(value) is not list or len(value) > len(PERMISSION_CODES):
        raise ValueError("webhook.payload_invalid")
    try:
        canonical = canonical_permission_codes(value)
    # 目录解析可能包含未知 code，统一折叠为公开 payload 错误且不传播原始值。
    except ValueError:
        raise ValueError("webhook.payload_invalid") from None
    if list(canonical) != value:
        # 事件签名必须只有一种字节表示，调用方不能依赖接收端重新排序或去重。
        raise ValueError("webhook.payload_invalid")
    return canonical


def _validate_role_permission_changed_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
) -> None:
    """把权限矩阵 payload 绑定到持久 source event 与可信租户操作主体。"""

    if (
        not _is_prefixed_lower_hex(event_id, prefix="rpevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=128)
        or not _is_bounded_safe_text(actor_id, maximum=128)
        or object_id != payload["role_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=160)
    ):
        # 任一身份漂移都会破坏租户隔离、投递幂等或审计追踪，必须在构造事件时失败关闭。
        raise ValueError("webhook.payload_invalid")


def _validate_chat_message_sent_payload(payload: dict[str, Any]) -> None:
    """校验 ``chat.message_sent`` 的精确治理元数据合同。

    参数：已通过通用敏感键扫描的 payload。返回：合同合法时无返回值。
    约束：事件只包含租户限定资源 ID、固定状态、整数用量/成本和治理码；正文、正文哈希、
    Key、Provider、Endpoint、展示名称及自由错误说明都不能通过精确字段集。
    """

    expected_fields = {
        "schema_version",
        "chat_session_id",
        "chat_message_id",
        "project_id",
        "environment_id",
        "model_alias_id",
        "gateway_request_id",
        "status",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_tokens",
        "cost_cny_micros",
        "policy_decision",
        "policy_reason_code",
        "fallback_triggered",
        "cache_status",
        "error_code",
        "completed_at",
    }
    if set(payload) != expected_fields:
        # 额外字段可能携带会话正文、模型展示名或执行凭据，不能静默裁剪后继续投递。
        raise ValueError("webhook.payload_invalid")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not _is_prefixed_lower_hex(payload["chat_session_id"], prefix="chs_")
        or not _is_prefixed_lower_hex(payload["chat_message_id"], prefix="chm_")
        or any(
            not _is_bounded_safe_text(payload[field], maximum=128)
            for field in (
                "project_id",
                "environment_id",
                "model_alias_id",
                "gateway_request_id",
            )
        )
        or payload["status"] not in {"succeeded", "failed"}
        or not _is_canonical_utc_second(payload["completed_at"])
    ):
        # owner、链路、状态或时间任一形状不合法都整体验证失败，不能裁剪后继续投递。
        raise ValueError("webhook.payload_invalid")

    numeric_fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_tokens",
        "cost_cny_micros",
    )
    for field in numeric_fields:
        value = payload[field]
        if value is not None and (
            type(value) is not int or not 0 <= value <= 9_007_199_254_740_991
        ):
            # bool 不得借用 int 继承关系，且所有数值限制在 JSON 安全整数范围内。
            raise ValueError("webhook.payload_invalid")
    input_tokens = payload["input_tokens"]
    output_tokens = payload["output_tokens"]
    total_tokens = payload["total_tokens"]
    cache_tokens = payload["cache_tokens"]
    if total_tokens is not None and (
        input_tokens is None
        or output_tokens is None
        or total_tokens != input_tokens + output_tokens
    ):
        # 汇总 Token 必须能由同一事件中的输入/输出事实重新计算，不能接受不完整或漂移值。
        raise ValueError("webhook.payload_invalid")
    if cache_tokens is not None and input_tokens is not None and cache_tokens > input_tokens:
        # cache Token 不得超过输入 Token，否则治理摘要与正式 UsageLedger 无法自洽。
        raise ValueError("webhook.payload_invalid")

    decision = payload["policy_decision"]
    reason_code = payload["policy_reason_code"]
    fallback = payload["fallback_triggered"]
    cache_status = payload["cache_status"]
    error_code = payload["error_code"]
    if (
        decision not in {None, "allow", "deny", "route_to"}
        or (reason_code is not None and not _is_fixed_dotted_code(reason_code))
        or fallback not in {None, False, True}
        or (fallback is not None and type(fallback) is not bool)
        or cache_status not in {None, "hit", "miss", "not_configured", "unknown"}
        or (error_code is not None and not _is_fixed_dotted_code(error_code))
    ):
        # reason/error 只能是稳定点分 code，禁止把策略或 Provider 自由正文塞入固定字段。
        raise ValueError("webhook.payload_invalid")
    if payload["status"] == "succeeded":
        if (
            any(payload[field] is None for field in numeric_fields)
            or decision is None
            or fallback is None
            or cache_status is None
            or error_code is not None
        ):
            # 成功事件必须给出完整治理数值且没有错误码，缺一项都不能伪造为成功。
            raise ValueError("webhook.payload_invalid")
    # 失败事件必须携带固定错误码；用量未知可以为 null，但不能省略失败原因。
    elif error_code is None:
        # 失败事件必须给出统一固定错误码；无法确认的用量和成本可以保持 null。
        raise ValueError("webhook.payload_invalid")


def _validate_chat_message_sent_envelope(
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把 Chat 元数据绑定到持久 Message、可信用户和同一 Gateway 调用链。

    参数：严格 payload、信封归属、来源 request/trace 和终结事务时间。
    返回：所有值逐字绑定时无返回值。
    约束：稳定事件 ID 必须等于 ``evt_`` 加 Message ID；任一 owner、链路或时间漂移都
    在网络投递前失败关闭，不能生成新事件 ID 或猜测默认 actor。
    """

    if (
        event_id != f"evt_{payload['chat_message_id']}"
        or not _is_bounded_safe_text(tenant_id, maximum=128)
        or not _is_bounded_safe_text(actor_id, maximum=128)
        or object_id != payload["chat_message_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or source_request_id != payload["gateway_request_id"]
        or occurred_at.tzinfo is None
        or _to_iso(occurred_at) != payload["completed_at"]
    ):
        # 信封和 payload 任一 owner、链路或时间不一致都可能导致跨租户错误投递。
        raise ValueError("webhook.payload_invalid")


def _is_fixed_dotted_code(value: object) -> bool:
    """判断值是否为不含空白和自由正文的有界小写点分治理 code。"""

    if type(value) is not str or not 3 <= len(value) <= 128 or not value.isascii():
        return False
    parts = value.split(".")
    return len(parts) >= 2 and all(
        part
        and part[0].islower()
        and part[0].isalpha()
        and all(
            character.islower() or character.isdigit() or character == "_"
            for character in part
        )
        for part in parts
    )


def _validate_invitation_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    """校验邀请创建或接受事件的精确六字段安全摘要。

    参数：冻结事件名和已通过通用敏感键扫描的 payload。
    返回：合同合法时无返回值。
    约束：email、Token、密码、tenant 名称和邮件文案均不得进入事件；邮箱只使用
    随机 invitation_id 加盐后的不可逆指纹。
    """

    common_fields = {
        "schema_version",
        "invitation_id",
        "email_fingerprint",
        "role_code",
    }
    event_fields = {
        "user.invited": common_fields | {"expires_at", "invited_at"},
        "invitation.accepted": common_fields | {"user_id", "accepted_at"},
    }
    expected = event_fields.get(event_type)
    if expected is None or set(payload) != expected:
        # 字段集合必须精确，额外字段可能携带邮箱、链接或前端邮件正文。
        raise ValueError("webhook.payload_invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        # bool 是 int 子类，事件版本必须要求真实整数。
        raise ValueError("webhook.payload_invalid")
    # Invitation ID 必须是已持久化对象标识，未知形状不能进入订阅投递链。
    if not _is_prefixed_lower_hex(payload["invitation_id"], prefix="inv_"):
        raise ValueError("webhook.payload_invalid")
    fingerprint = payload["email_fingerprint"]
    if type(fingerprint) is not str or not _is_lower_hex(fingerprint, expected_length=64):
        # 邮箱指纹必须是稳定 SHA-256，不允许明文、短前缀或大写变体。
        raise ValueError("webhook.payload_invalid")
    role_code = payload["role_code"]
    if type(role_code) is not str or role_code not in ROLE_CODES:
        # 被邀请角色只能来自已发布固定目录，不能由订阅端猜测未知角色。
        raise ValueError("webhook.payload_invalid")
    if event_type == "user.invited":
        # 邀请和过期时间都必须是规范 UTC 秒，避免订阅端按不同本地时区解释。
        if not _is_canonical_utc_second(payload["invited_at"]) or not _is_canonical_utc_second(
            payload["expires_at"]
        ):
            # 非规范时间会破坏跨服务逐字签名和过期语义，统一拒绝整个事件。
            raise ValueError("webhook.payload_invalid")
        invited_at = datetime.fromisoformat(payload["invited_at"].replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
        if expires_at <= invited_at or expires_at - invited_at > timedelta(hours=168):
            # 到期时间必须处于 (0,168h]，防止订阅端接收天然失效或越过产品上限的邀请。
            raise ValueError("webhook.payload_invalid")
    else:
        # 接受事件 actor 必须引用有界新用户 ID，不能把邮箱或任意正文放入该字段。
        if not _is_bounded_safe_text(payload["user_id"], maximum=128):
            raise ValueError("webhook.payload_invalid")
        # accepted_at 与事务中的 User/Invitation/Audit 时间逐字一致，拒绝宽松时间。
        if not _is_canonical_utc_second(payload["accepted_at"]):
            raise ValueError("webhook.payload_invalid")


# Invitation envelope 绑定 tenant、actor、object 和来源链路，payload 合法不能替代归属校验。
def _validate_invitation_event_envelope(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str | None,
    tenant_id: str,
    actor_id: str | None,
    object_id: str | None,
    trace_id: str | None,
    source_request_id: str | None,
    occurred_at: datetime,
) -> None:
    """把 Invitation payload 绑定到持久 source event、actor 和操作时间。

    参数：严格 payload、事件/租户/actor/object/链路标识和事务时间。
    返回：全部归属与时间逐字一致时无返回值。
    约束：任一漂移必须在提交和网络投递前失败，不能补默认租户或 actor。
    """

    event_time = payload["invited_at"] if event_type == "user.invited" else payload["accepted_at"]
    if (
        not _is_prefixed_lower_hex(event_id, prefix="ivevt_")
        or not _is_bounded_safe_text(tenant_id, maximum=128)
        or not _is_bounded_safe_text(actor_id, maximum=128)
        or object_id != payload["invitation_id"]
        or not _is_bounded_safe_text(trace_id, maximum=160)
        or not _is_bounded_safe_text(source_request_id, maximum=160)
        or occurred_at.tzinfo is None
        or _to_iso(occurred_at) != event_time
        or (event_type == "invitation.accepted" and actor_id != payload["user_id"])
    ):
        # 任一身份或时间漂移都会破坏一次性状态、租户隔离和投递幂等，必须提交前失败关闭。
        raise ValueError("webhook.payload_invalid")

def _validate_policy_snapshot_event_payload(payload: dict[str, Any]) -> None:
    """校验 ``policy_snapshot.published`` 的精确安全 payload。

    参数：已经通过通用敏感字段扫描的 payload；返回：合法时无返回值。
    约束：只允许版本、内容 hash、数量、操作和版本 provenance，不允许原因正文或 DSL。
    """

    expected_fields = {
        "version",
        "content_hash",
        "policy_count",
        "operation",
        "previous_version",
        "rollback_source_version",
    }
    # 字段集合必须精确匹配安全摘要，额外字段可能携带原因正文或策略内容。
    if set(payload) != expected_fields:
        raise ValueError("webhook.payload_invalid")
    version = payload["version"]
    policy_count = payload["policy_count"]
    previous_version = payload["previous_version"]
    rollback_source_version = payload["rollback_source_version"]
    operation = payload["operation"]
    content_hash = payload["content_hash"]
    # 版本必须是严格正整数，禁止 bool 借用 Python 整数继承关系通过校验。
    if type(version) is not int or version < 1:
        raise ValueError("webhook.payload_invalid")
    # 事件计数复用快照策略上限，避免伪造无界数值污染订阅端指标。
    if type(policy_count) is not int or not 0 <= policy_count <= 200:
        raise ValueError("webhook.payload_invalid")
    # content hash 只接受 shared 生成的小写 SHA-256，不接受短摘要或大写变体。
    if (
        type(content_hash) is not str
        or len(content_hash) != 64
        or any(character not in "0123456789abcdef" for character in content_hash)
    ):
        # 非标准摘要无法与快照事实稳定关联，不能进入签名事件或下游审计。
        raise ValueError("webhook.payload_invalid")
    # 首版没有前驱，后续版本必须连续递增，事件不能描述断裂发布图。
    if version == 1:
        # v1 携带前驱会让订阅端误判为覆盖发布，因此必须明确为空。
        if previous_version is not None:
            raise ValueError("webhook.payload_invalid")
    elif type(previous_version) is not int or previous_version != version - 1:
        # 非连续前驱无法对应控制面已封印 head，统一拒绝生成 Webhook。
        raise ValueError("webhook.payload_invalid")
    # publish 与 rollback 使用互斥 provenance，不能依赖接收端猜测空值含义。
    if operation == "publish":
        # 普通发布绝不携带回滚来源，防止审计语义与事件语义分叉。
        if rollback_source_version is not None:
            raise ValueError("webhook.payload_invalid")
    elif operation == "rollback":
        # M1 仅允许回滚当前 head 的上一版，新 vN 来源必须精确为 vN-2。
        if (
            version < 3
            or type(rollback_source_version) is not int
            or rollback_source_version != version - 2
        ):
            # 回滚来源版本不连续时事件 provenance 与 head 图不一致，必须拒绝发送。
            raise ValueError("webhook.payload_invalid")
    else:
        # 未知操作不能被订阅端默认解释成发布，否则会掩盖状态机升级不兼容。
        raise ValueError("webhook.payload_invalid")


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """生成稳定 JSON 字节，确保签名和重试时 body 完全一致。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def webhook_signature_header(secret: str, timestamp_seconds: int, body: bytes) -> str:
    """生成 MetaHub Webhook HMAC 签名头。

    签名头只包含时间戳和摘要，不包含 signing secret 明文，便于接收端复用统一验签逻辑。
    """

    digest = sign_webhook_payload(secret, timestamp_seconds, body)
    return f"t={timestamp_seconds},v1={digest}"


def sign_webhook_payload(secret: str, timestamp_seconds: int, body: bytes) -> str:
    """按 timestamp.body 规则生成 HMAC-SHA256 十六进制摘要。

    body 必须是已经稳定序列化的字节，确保重试、测试接收端和后续 SDK 示例得到一致签名。
    """

    signing_input = str(timestamp_seconds).encode("ascii") + b"." + body
    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    header: str,
    secret: str,
    body: bytes,
    *,
    now_seconds: int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    """校验 Webhook 签名，供测试接收端和后续 SDK 示例复用。"""

    parsed = _parse_signature_header(header)
    if parsed is None:
        return False
    timestamp_seconds, digest = parsed
    current = now_seconds if now_seconds is not None else int(time.time())
    if abs(current - timestamp_seconds) > tolerance_seconds:
        return False
    expected = sign_webhook_payload(secret, timestamp_seconds, body)
    return hmac.compare_digest(expected, digest)


def _parse_signature_header(header: str) -> tuple[int, str] | None:
    """解析 t=<timestamp>,v1=<signature> 签名头，非法格式直接返回 None。"""

    parts = {}
    for item in header.split(","):
        key, sep, value = item.partition("=")
        if sep:
            parts[key.strip()] = value.strip()
    try:
        timestamp_seconds = int(parts.get("t", ""))
    # 时间戳不是整数时签名头格式不可信，直接返回 None 让上层判定验签失败。
    except ValueError:
        return None
    digest = parts.get("v1", "")
    if not digest:
        return None
    return timestamp_seconds, digest


def _to_iso(value: datetime) -> str:
    """统一把事件时间保存为 UTC ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
