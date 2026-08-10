"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息
脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/usage/ledger.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from typing import Any, Literal
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.budget_reservations import (
    set_request_budget_reservations_outstanding,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.routing_policy import MAX_PRICE_DECIMAL_PLACES, MAX_PRICE_PER_1K
from metahub_shared.runtime.usage import sanitize_provider_usage
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

logger = logging.getLogger("metahub.gateway.usage")

# 归一化警告白名单只允许稳定代码进入账本，禁止 Provider 原始错误文本污染成本记录。
NORMALIZATION_WARNING_CODES = {
    "invalid_input_tokens",
    "invalid_output_tokens",
    "invalid_total_tokens",
    "invalid_cache_tokens",
    "total_tokens_less_than_components",
    "invalid_provider_cost",
    "currency_conversion_unavailable",
    "unsupported_provider_currency",
    "provider_currency_missing",
}
# spool 使用独立版本号和精确字段集合；未知字段必须失败关闭，避免未来上下文扩展时把敏感值落盘。
_SPOOL_RECORD_SCHEMA_VERSION = 6
# v1/v2 字段集必须长期保留用于恢复升级前已 fsync 的待重放记录；各版本使用精确集合，
# 旧记录缺失的安全证据只能解释为“未记录”，不能用宽松默认冒充新版本事实。
_SPOOL_CONTEXT_FIELDS_V1 = frozenset(
    {
        "tenant_id",
        "request_id",
        "trace_id",
        "virtual_key_id",
        "environment_id",
        "environment_code",
        "project_id",
        "application_id",
        "provider_id",
        "model_id",
        "request_model",
        "resolved_model",
        "provider_code",
        "snapshot_version",
        "policy_snapshot_status",
        "policy_decision",
        "policy_reason_code",
        "policy_id",
        "policy_action_type",
        "policy_target_model_id",
        "budget_status",
        "budget_rule_id",
        "budget_scope_type",
        "budget_scope_id",
        "budget_action",
        "budget_reason_code",
        "budget_current_cost_cny",
        "budget_limit_cny",
        "budget_reservation_outstanding_cny",
        "dlp_status",
        "dlp_rule_id",
        "dlp_action",
        "dlp_match_count",
        "dlp_reason_code",
        "fallback_triggered",
        "routing_attempt_count",
        "final_routing_attempt_id",
        "routing_attempt_id",
        "stream_session_id",
        "stream_status",
    }
)
_SPOOL_CONTEXT_FIELDS_V2 = _SPOOL_CONTEXT_FIELDS_V1 | frozenset(
    {"policy_mask_applied", "policy_mask_policy_id"}
)
_SPOOL_CONTEXT_FIELDS_V3 = _SPOOL_CONTEXT_FIELDS_V2 | frozenset(
    {
        "intent_risk_result_id",
        "intent_risk_schedule_status",
        "intent_risk_mode",
        "intent_risk_type",
        "intent_risk_score",
        "intent_risk_action",
        "intent_risk_reason_code",
        "intent_risk_detector_version",
        "intent_risk_input_truncated",
    }
)
_SPOOL_CONTEXT_FIELDS_V4 = _SPOOL_CONTEXT_FIELDS_V3 | frozenset(
    {
        "session_risk_status",
        "session_risk_score_id",
        "session_risk_scope_type",
        "session_risk_state",
        "session_risk_reason_code",
    }
)
_SPOOL_CONTEXT_FIELDS_V5 = _SPOOL_CONTEXT_FIELDS_V4 | frozenset(
    {
        "output_safety_scan_id",
        "output_safety_scan_status",
        "output_safety_decision",
        "billing_disposition",
    }
)
_SPOOL_CONTEXT_FIELDS_V6 = _SPOOL_CONTEXT_FIELDS_V5 | frozenset(
    {
        "batch_job_id",
        "batch_task_id",
        "batch_attempt_id",
        "batch_route_plan_hash",
        "batch_allocation_settlement_cny",
    }
)
_SPOOL_USAGE_FIELDS = frozenset(
    {
        "usage_ledger_id",
        "pricing_observed_at",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_tokens",
        "estimated",
        "usage_source",
        "provider_cost_amount",
        "provider_cost_currency",
        "provider_cost_cny",
        "normalization_warnings",
    }
)
_SPOOL_ESTIMATE_FIELDS = frozenset(
    {
        "amount",
        "currency",
        "price_schema_version",
        "cost_cny",
        "cost_usd",
        "price_snapshot_id",
        "evidence",
    }
)
_SPOOL_EVIDENCE_FIELDS = frozenset(
    {
        "price_snapshot_id",
        "model_id",
        "provider_id",
        "provider_code",
        "currency",
        "input_price_per_1k",
        "output_price_per_1k",
        "price_schema_version",
        "source",
        "price_plan_id",
        "effective_from",
    }
)
# Provider 辅助成本只作为审计元数据；上限防止超大 Decimal 在恢复路径消耗异常 CPU/内存。
_MAX_SPOOL_DECIMAL = Decimal("1000000000000000")


@dataclass(frozen=True)
class RequestLogContext:
    """一次网关请求落库所需的最小安全上下文。

    这里刻意只保存可审计的元数据，不保存 Prompt、Response、Authorization、
    Provider Key 或原始输入内容。后续所有 request_logs/usage_ledger 写入都从
    这个上下文取 tenant_id，确保业务数据默认具备租户边界。
    """

    tenant_id: str
    request_id: str
    trace_id: str
    virtual_key_id: str | None = None
    environment_id: str | None = None
    environment_code: str | None = None
    project_id: str | None = None
    application_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    endpoint: str | None = None
    request_model: str | None = None
    resolved_model: str | None = None
    provider_code: str | None = None
    # 快照版本和状态在请求读取正文前由 request claim 冻结；后续流式/Fallback 补写不得漂移。
    snapshot_version: int | None = None
    policy_snapshot_status: str = "disabled"
    # Policy 只保存决定性命中的安全标识和稳定 reason，不保存 DSL、AST 或上下文实际值。
    policy_decision: str | None = None
    policy_reason_code: str | None = None
    policy_id: str | None = None
    policy_action_type: str | None = None
    policy_target_model_id: str | None = None
    # mask trace 只保存是否完整执行和首个稳定 Policy ID；禁止增加正文、位置、命中数或 hash。
    policy_mask_applied: bool = False
    policy_mask_policy_id: str | None = None
    budget_status: str | None = None
    budget_rule_id: str | None = None
    budget_scope_type: str | None = None
    budget_scope_id: str | None = None
    budget_action: str | None = None
    budget_reason_code: str | None = None
    budget_current_cost_cny: str | None = None
    budget_limit_cny: str | None = None
    # 仅在进程内携带预算预留终态覆盖额；不会写入 request_logs 或对外响应。
    # ``None`` 表示本请求没有预算预留，"0" 表示已确认没有未知上游账单。
    budget_reservation_outstanding_cny: str | None = None
    dlp_status: str | None = None
    dlp_rule_id: str | None = None
    dlp_action: str | None = None
    dlp_match_count: int = 0
    dlp_reason_code: str | None = None
    # 意图摘要只记录调度与同步强阻断事实；异步分类正文和结果内容留在独立结果表。
    intent_risk_result_id: str | None = None
    intent_risk_schedule_status: str = "disabled"
    intent_risk_mode: str | None = None
    intent_risk_type: str | None = None
    intent_risk_score: int | None = None
    intent_risk_action: str | None = None
    intent_risk_reason_code: str | None = None
    intent_risk_detector_version: str | None = None
    intent_risk_input_truncated: bool = False
    # 会话风险只保存状态、score ID、scope 类型和固定原因；scope key 与两个
    # HMAC digest 永不进入日志。
    session_risk_status: str = "not_applicable"
    session_risk_score_id: str | None = None
    session_risk_scope_type: str | None = None
    session_risk_state: str | None = None
    session_risk_reason_code: str | None = None
    fallback_triggered: bool = False
    routing_attempt_count: int = 0
    final_routing_attempt_id: str | None = None
    routing_attempt_id: str | None = None
    # 流式请求只关联安全会话 ID 和终态，不在调用日志上下文保存任何 SSE 正文。
    stream_session_id: str | None = None
    stream_status: str | None = None
    # OutputSafetyScan 只携带稳定 ID/二维终态和账单意图；正文、pattern 与命中片段永不进入上下文。
    output_safety_scan_id: str | None = None
    output_safety_scan_status: str | None = None
    output_safety_decision: str | None = None
    billing_disposition: str | None = None
    # Batch owner and settlement fields are internal-only. They are excluded from request_logs and
    # enter disk only through the exact-set v6 Usage spool contract.
    batch_job_id: str | None = None
    batch_task_id: str | None = None
    batch_attempt_id: str | None = None
    batch_route_plan_hash: str | None = None
    batch_allocation_settlement_cny: str | None = None


# RequestLog 安全摘要按固定顺序比较，防止同一 request 被不同 result/mode/version 拼接。
_INTENT_CONTEXT_FIELDS = (
    "intent_risk_result_id",
    "intent_risk_schedule_status",
    "intent_risk_mode",
    "intent_risk_type",
    "intent_risk_score",
    "intent_risk_action",
    "intent_risk_reason_code",
    "intent_risk_detector_version",
    "intent_risk_input_truncated",
)

# RequestLog 会话摘要也采用一次冻结语义；Usage/Fallback 只能复用，不能重解释同一请求。
_SESSION_RISK_CONTEXT_FIELDS = (
    "session_risk_status",
    "session_risk_score_id",
    "session_risk_scope_type",
    "session_risk_state",
    "session_risk_reason_code",
)


def _validate_request_log_intent_merge(
    existing: sqlite3.Row,
    context: RequestLogContext,
) -> None:
    """复核 RequestLog upsert 不会覆盖已冻结的非 disabled 意图事实。

    参数：同 tenant/request 的既有行和本次可信上下文。返回值：无。关键约束：旧版或
    非 Chat 的 disabled 上下文可以被保留，但两个非 disabled 摘要必须逐字段一致。
    """

    existing_status = existing["intent_risk_schedule_status"]
    incoming_status = context.intent_risk_schedule_status
    if existing_status == "disabled" or incoming_status == "disabled":
        return
    existing_values = tuple(
        bool(existing[name]) if name == "intent_risk_input_truncated" else existing[name]
        for name in _INTENT_CONTEXT_FIELDS
    )
    incoming_values = tuple(getattr(context, name) for name in _INTENT_CONTEXT_FIELDS)
    if existing_values != incoming_values:
        # 任一 identity、模式、原因或版本漂移都说明请求上下文被错误拼接，必须整体回滚。
        raise sqlite3.IntegrityError("request_log_intent_conflict")


def _validate_request_log_session_risk_merge(
    existing: sqlite3.Row,
    context: RequestLogContext,
) -> None:
    """复核 RequestLog upsert 不会覆盖已形成的会话风险安全事实。

    claim 的 ``not_applicable`` 可以被首个最终摘要替换；一旦形成 disabled/clean/risk/
    failed 状态，后续 Usage、Fallback 或流终态只能逐字段相等地重放。
    """

    if (
        existing["session_risk_status"] == "not_applicable"
        or context.session_risk_status == "not_applicable"
    ):
        return
    existing_values = tuple(existing[name] for name in _SESSION_RISK_CONTEXT_FIELDS)
    incoming_values = tuple(getattr(context, name) for name in _SESSION_RISK_CONTEXT_FIELDS)
    if existing_values != incoming_values:
        # 冲突可能把另一 scope/score 的状态拼到本请求，必须在写入前整体回滚。
        raise sqlite3.IntegrityError("request_log_session_risk_conflict")


@dataclass(frozen=True)
class PriceSnapshotEvidence:
    """一次成本估算实际使用或明确缺失的不可变价格证据。

    该结构只保存安全价格元数据；不保存 Provider Endpoint、Credential、Key 或原始响应。
    spool 重放使用同一 snapshot ID 和观测时刻，不能改用重放时的当前价格。
    """

    price_snapshot_id: str
    model_id: str | None
    provider_id: str | None
    provider_code: str | None
    currency: str
    input_price_per_1k: str
    output_price_per_1k: str
    price_schema_version: str
    source: str
    price_plan_id: str | None = None
    effective_from: str | None = None


@dataclass(frozen=True)
class CostEstimate:
    """按当前模型价格快照估算出的调用成本。

    M0 阶段不做复杂账单结算，但每次调用都要具备可追踪的成本估算字段。
    价格缺失或版本价格损坏时回落到 0 CNY，并保留 price_schema_version 为空。
    """

    amount: Decimal
    currency: str
    price_schema_version: str | None
    cost_cny: Decimal
    cost_usd: Decimal
    price_snapshot_id: str | None
    price_snapshot_evidence: PriceSnapshotEvidence | None = None


@dataclass
class UsageLedgerIntent:
    """一次 Usage 账本写入在连接数据库前冻结的安全意图。

    ``captured_estimate`` 仅在同一数据库尝试已选出价格后赋值，供失败 spool 保存原证据；
    它是内部事务状态，不由 Provider 或客户端提交。
    """

    usage_ledger_id: str
    pricing_observed_at: str
    raw_usage_value: dict[str, Any]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    estimated: bool
    usage_source: str
    estimate_reason: str | None
    provider_cost_amount: str | None
    provider_cost_currency: str | None
    provider_cost_cny: str | None
    normalization_warnings: list[str]
    captured_estimate: CostEstimate | None = None


@dataclass(frozen=True)
class FinOpsLedgerCommitted:
    """数据库已提交或已确认存在的正式 FinOps 分录安全摘要。

    ``created`` 只说明当前事务是否新建分录；spool 在 existing 分支不得再次投递事件。
    其余字段完全来自 tenant-qualified 正式表，不从请求内存重新推导。
    """

    created: bool
    ledger_entry_id: str
    event_id: str
    tenant_id: str
    entry_type: str
    direction: str
    amount: str
    currency: str
    normalized_cny: str | None
    cny_normalization_status: str
    quota_type: str
    source_request_id: str | None
    source_trace_id: str | None
    operation_trace_id: str
    price_snapshot_id: str | None
    price_schema_version: str | None
    price_snapshot_source: str | None
    price_evidence_status: str
    reversal_of_entry_id: str | None
    actor_user_id: str | None
    created_at: str

    def to_webhook_event(self) -> WebhookEvent:
        """构造经过共享严格合同校验的 canonical 账本 Webhook 事件。

        返回：稳定 ``flevt_`` 信封；调用方只能在数据库 commit 后入队。
        约束：payload 不含调整原因、幂等键、Prompt、Response、Key、Token 或 Endpoint。
        """

        return build_webhook_event(
            tenant_id=self.tenant_id,
            event_type="ledger.entry_created",
            event_id=self.event_id,
            actor_id=self.actor_user_id,
            object_id=self.ledger_entry_id,
            trace_id=self.operation_trace_id,
            timestamp=_parse_db_time(self.created_at),
            payload={
                "ledger_entry_id": self.ledger_entry_id,
                "entry_type": self.entry_type,
                "direction": self.direction,
                "amount": self.amount,
                "currency": self.currency,
                "normalized_cny": self.normalized_cny,
                "cny_normalization_status": self.cny_normalization_status,
                "quota_type": self.quota_type,
                "source_request_id": self.source_request_id,
                "price_snapshot_id": self.price_snapshot_id,
                "price_schema_version": self.price_schema_version,
                "price_snapshot_source": self.price_snapshot_source,
                "price_evidence_status": self.price_evidence_status,
                "reversal_of_entry_id": self.reversal_of_entry_id,
            },
        )


@dataclass(frozen=True)
class UsageLedgerWriteResult:
    """一次 Usage 写入的明确 durability 结果。

    ``committed`` 仅在主库事务成功时存在；``spooled`` 表示 HMAC+fsync crash spool 已落盘；
    ``lost`` 只供已开始流式响应的容错路径报告，非流式或安全阻断必须用 ``require_durable`` 抛 503。
    """

    durability: Literal["committed", "spooled", "lost"]
    committed: FinOpsLedgerCommitted | None


def claim_gateway_request(config: AppConfig, context: RequestLogContext) -> None:
    """在 Provider 前为一次已认证 HTTP 请求创建不可变占位。

    参数：config 提供数据库连接，context 必须已经包含可信 tenant、trace、Virtual Key 与
    快照 lease 决策。返回：成功时无返回值。约束：同 tenant 的 request_id 只能 claim
    一次；重复请求返回统一 409，其他数据库或约束故障失败关闭，绝不能继续调用 Provider。
    """

    try:
        connection = _connect(config)
    # claim 数据库不可连接时没有可信审计落点，必须阻断请求而不是继续访问 Provider。
    except DbError as exc:
        # claim 是 Provider 前审计闸门，连接失败必须转换为统一可重试错误，不能进入上游。
        raise ApiError("gateway.request_log_unavailable") from exc
    try:
        claimed_at = _to_db_time(_utcnow())
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id, request_id, trace_id, virtual_key_id,
              snapshot_version, policy_snapshot_status, claimed_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.tenant_id,
                context.request_id,
                context.trace_id,
                context.virtual_key_id,
                context.snapshot_version,
                context.policy_snapshot_status,
                claimed_at,
            ),
        )
        if _has_complete_experiment_scope(context):
            # 实验只认认证阶段已完整冻结的 Project/Environment；旧调用缺字段时保留普通 claim，
            # 但不从可变 Virtual Key 反查补猜 scope，也就不可能获得实验 Assignment。
            connection.execute(
                """
                INSERT INTO gateway_request_scope_claims(
                  tenant_id, request_id, trace_id, virtual_key_id,
                  project_id, application_id, environment_id, environment_code, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.tenant_id,
                    context.request_id,
                    context.trace_id,
                    context.virtual_key_id,
                    context.project_id,
                    context.application_id,
                    context.environment_id,
                    context.environment_code,
                    claimed_at,
                ),
            )
        connection.commit()
    # 唯一性或外键约束失败需要进一步区分客户端重用与审计基础设施损坏。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 只有同租户同 request_id 的既有 claim 才属于客户端重用；其他 FK/CHECK 异常
        # 表示审计边界不可用，不能误报 409 后掩盖迁移或数据损坏。
        try:
            existing = connection.execute(
                """
                SELECT 1
                FROM gateway_request_claims
                WHERE tenant_id=? AND request_id=?
                """,
                (context.tenant_id, context.request_id),
            ).fetchone()
        # 二次查询失败时不能推断为客户端重用，否则会用 409 掩盖数据库真实故障。
        except sqlite3.Error:
            existing = None
        # 只有当前租户确有同 request_id 占位时才返回稳定的请求标识重用错误。
        if existing is not None:
            raise ApiError("gateway.request_id_reused") from exc
        # 其余完整性异常均视为审计不可用，防止迁移缺失或约束损坏被误分类。
        raise ApiError("gateway.request_log_unavailable") from exc
    # 普通数据库故障无法建立不可变 claim，必须回滚并阻断本次上游调用。
    except (DbError, sqlite3.Error) as exc:
        connection.rollback()
        # 对外只返回统一可重试错误，不泄露数据库路径、表结构或底层异常文本。
        raise ApiError("gateway.request_log_unavailable") from exc
    finally:
        connection.close()


def _has_complete_experiment_scope(context: RequestLogContext) -> bool:
    """仅在认证上下文具备实验所需完整 owner 时创建不可变 scope claim。"""

    required = (
        context.virtual_key_id,
        context.project_id,
        context.environment_id,
        context.environment_code,
    )
    return all(isinstance(value, str) and bool(value) for value in required)


def record_request_log(
    config: AppConfig,
    context: RequestLogContext,
    *,
    status: str,
    error_code: str | None,
    latency_ms: int | None,
) -> None:
    """写入或更新请求日志主表。

    失败请求也必须尽量落库，因此这里使用 tenant_id+request_id 做幂等 upsert；
    如果同一个 request_id 后续补齐了路由信息，会更新 provider/model 等字段，
    但保留最早的 created_at，并用 updated_at 表示最近一次写入时间。
    """

    if context.policy_mask_applied and context.policy_mask_policy_id is None:
        # applied=true 没有稳定 Policy ID 无法追踪来源，必须在进入 SQL 前失败关闭。
        raise ApiError("gateway.request_log_unavailable")
    connection = _connect(config)
    now = _to_db_time(_utcnow())
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_state = connection.execute(
            """
            SELECT policy_mask_policy_id,
                   intent_risk_result_id,intent_risk_schedule_status,intent_risk_mode,
                   intent_risk_type,intent_risk_score,intent_risk_action,
                   intent_risk_reason_code,intent_risk_detector_version,
                   intent_risk_input_truncated,session_risk_status,
                   session_risk_score_id,session_risk_scope_type,session_risk_state,
                   session_risk_reason_code
            FROM request_logs
            WHERE tenant_id=? AND request_id=?
            """,
            (context.tenant_id, context.request_id),
        ).fetchone()
        if (
            existing_state is not None
            and existing_state["policy_mask_policy_id"] is not None
            and context.policy_mask_policy_id is not None
            and existing_state["policy_mask_policy_id"] != context.policy_mask_policy_id
        ):
            # 同一请求出现两个 mask Policy ID 表示请求状态被拼接，不能选择任一值掩盖冲突。
            raise sqlite3.IntegrityError("request_log_policy_mask_conflict")
        if existing_state is not None:
            _validate_request_log_intent_merge(existing_state, context)
            _validate_request_log_session_risk_merge(existing_state, context)
        connection.execute(
            """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, environment_id, environment_code,
          project_id, application_id, provider_id, model_id,
          endpoint, request_model, resolved_model, provider_code,
          snapshot_version, policy_snapshot_status, policy_decision, policy_reason_code,
          policy_id, policy_action_type, policy_target_model_id,
          policy_mask_policy_id, policy_mask_applied,
          status, error_code, latency_ms,
          budget_status, budget_rule_id, budget_scope_type, budget_scope_id,
          budget_action, budget_reason_code, budget_current_cost_cny, budget_limit_cny,
          dlp_status, dlp_rule_id, dlp_action, dlp_match_count, dlp_reason_code,
          intent_risk_result_id, intent_risk_schedule_status, intent_risk_mode,
          intent_risk_type, intent_risk_score, intent_risk_action,
          intent_risk_reason_code, intent_risk_detector_version, intent_risk_input_truncated,
          session_risk_status, session_risk_score_id, session_risk_scope_type,
          session_risk_state, session_risk_reason_code,
          fallback_triggered, routing_attempt_count, final_routing_attempt_id,
          stream_session_id, stream_status,
          created_at, updated_at
        )
        VALUES(
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(tenant_id, request_id) DO UPDATE SET
          virtual_key_id=excluded.virtual_key_id,
          environment_id=excluded.environment_id,
          environment_code=excluded.environment_code,
          project_id=excluded.project_id,
          application_id=excluded.application_id,
          provider_id=excluded.provider_id,
          model_id=excluded.model_id,
          endpoint=excluded.endpoint,
          request_model=excluded.request_model,
          resolved_model=excluded.resolved_model,
          provider_code=excluded.provider_code,
          policy_decision=excluded.policy_decision,
          policy_reason_code=excluded.policy_reason_code,
          policy_id=excluded.policy_id,
          policy_action_type=excluded.policy_action_type,
          policy_target_model_id=excluded.policy_target_model_id,
          policy_mask_policy_id=COALESCE(
            request_logs.policy_mask_policy_id,
            excluded.policy_mask_policy_id
          ),
          policy_mask_applied=CASE
            WHEN request_logs.policy_mask_applied=1 OR excluded.policy_mask_applied=1 THEN 1
            ELSE 0
          END,
          status=excluded.status,
          error_code=excluded.error_code,
          latency_ms=excluded.latency_ms,
          budget_status=excluded.budget_status,
          budget_rule_id=excluded.budget_rule_id,
          budget_scope_type=excluded.budget_scope_type,
          budget_scope_id=excluded.budget_scope_id,
          budget_action=excluded.budget_action,
          budget_reason_code=excluded.budget_reason_code,
          budget_current_cost_cny=excluded.budget_current_cost_cny,
          budget_limit_cny=excluded.budget_limit_cny,
          dlp_status=excluded.dlp_status,
          dlp_rule_id=excluded.dlp_rule_id,
          dlp_action=excluded.dlp_action,
          dlp_match_count=excluded.dlp_match_count,
          dlp_reason_code=excluded.dlp_reason_code,
          intent_risk_result_id=CASE
            WHEN request_logs.intent_risk_schedule_status!='disabled'
              THEN request_logs.intent_risk_result_id
            ELSE excluded.intent_risk_result_id
          END,
          intent_risk_schedule_status=CASE
            WHEN request_logs.intent_risk_schedule_status!='disabled'
              THEN request_logs.intent_risk_schedule_status
            ELSE excluded.intent_risk_schedule_status
          END,
          intent_risk_mode=CASE
            WHEN request_logs.intent_risk_schedule_status!='disabled'
              THEN request_logs.intent_risk_mode
            ELSE excluded.intent_risk_mode
          END,
          intent_risk_type=CASE
            WHEN request_logs.intent_risk_schedule_status!='disabled'
              THEN request_logs.intent_risk_type
            ELSE excluded.intent_risk_type
          END,
          intent_risk_score=CASE
            WHEN request_logs.intent_risk_schedule_status!='disabled'
              THEN request_logs.intent_risk_score
            ELSE excluded.intent_risk_score
          END,
          intent_risk_action=CASE
            WHEN request_logs.intent_risk_schedule_status!='disabled'
              THEN request_logs.intent_risk_action
            ELSE excluded.intent_risk_action
          END,
          intent_risk_reason_code=CASE
            WHEN request_logs.intent_risk_schedule_status!='disabled'
              THEN request_logs.intent_risk_reason_code
            ELSE excluded.intent_risk_reason_code
          END,
          intent_risk_detector_version=CASE
            WHEN request_logs.intent_risk_schedule_status!='disabled'
              THEN request_logs.intent_risk_detector_version
            ELSE excluded.intent_risk_detector_version
          END,
          intent_risk_input_truncated=CASE
            WHEN request_logs.intent_risk_schedule_status!='disabled'
              THEN request_logs.intent_risk_input_truncated
            ELSE excluded.intent_risk_input_truncated
          END,
          session_risk_status=CASE
            WHEN request_logs.session_risk_status!='not_applicable'
              THEN request_logs.session_risk_status
            ELSE excluded.session_risk_status
          END,
          session_risk_score_id=CASE
            WHEN request_logs.session_risk_status!='not_applicable'
              THEN request_logs.session_risk_score_id
            ELSE excluded.session_risk_score_id
          END,
          session_risk_scope_type=CASE
            WHEN request_logs.session_risk_status!='not_applicable'
              THEN request_logs.session_risk_scope_type
            ELSE excluded.session_risk_scope_type
          END,
          session_risk_state=CASE
            WHEN request_logs.session_risk_status!='not_applicable'
              THEN request_logs.session_risk_state
            ELSE excluded.session_risk_state
          END,
          session_risk_reason_code=CASE
            WHEN request_logs.session_risk_status!='not_applicable'
              THEN request_logs.session_risk_reason_code
            ELSE excluded.session_risk_reason_code
          END,
          fallback_triggered=excluded.fallback_triggered,
          routing_attempt_count=excluded.routing_attempt_count,
          final_routing_attempt_id=excluded.final_routing_attempt_id,
          stream_session_id=excluded.stream_session_id,
          stream_status=excluded.stream_status,
          updated_at=excluded.updated_at
            """,
            (
            context.request_id,
            context.tenant_id,
            context.trace_id,
            context.virtual_key_id,
            context.environment_id,
            context.environment_code,
            context.project_id,
            context.application_id,
            context.provider_id,
            context.model_id,
            context.endpoint,
            context.request_model,
            context.resolved_model,
            context.provider_code,
            context.snapshot_version,
            context.policy_snapshot_status,
            context.policy_decision,
            context.policy_reason_code,
            context.policy_id,
            context.policy_action_type,
            context.policy_target_model_id,
            context.policy_mask_policy_id,
            1 if context.policy_mask_applied else 0,
            status,
            error_code,
            latency_ms,
            context.budget_status,
            context.budget_rule_id,
            context.budget_scope_type,
            context.budget_scope_id,
            context.budget_action,
            context.budget_reason_code,
            context.budget_current_cost_cny,
            context.budget_limit_cny,
            context.dlp_status,
            context.dlp_rule_id,
            context.dlp_action,
            context.dlp_match_count,
            context.dlp_reason_code,
            context.intent_risk_result_id,
            context.intent_risk_schedule_status,
            context.intent_risk_mode,
            context.intent_risk_type,
            context.intent_risk_score,
            context.intent_risk_action,
            context.intent_risk_reason_code,
            context.intent_risk_detector_version,
            1 if context.intent_risk_input_truncated else 0,
            context.session_risk_status,
            context.session_risk_score_id,
            context.session_risk_scope_type,
            context.session_risk_state,
            context.session_risk_reason_code,
            1 if context.fallback_triggered else 0,
            context.routing_attempt_count,
            context.final_routing_attempt_id,
            context.stream_session_id,
            context.stream_status,
            now,
            now,
            ),
        )
        connection.commit()
    # 请求日志写入是审计闭环的一部分，任何数据库异常都必须回滚并收敛为固定公开错误。
    except sqlite3.Error as exc:
        connection.rollback()
        # 冲突和存储错误都只能公开固定码，不能把列名、SQL 或持久值带入响应。
        raise ApiError("gateway.request_log_unavailable") from exc
    finally:
        connection.close()
    _log_request_logged(context, status=status, error_code=error_code, latency_ms=latency_ms)


def record_usage_ledger(
    config: AppConfig,
    context: RequestLogContext,
    usage: object,
    *,
    intent: UsageLedgerIntent | None = None,
    replay: bool = False,
) -> FinOpsLedgerCommitted:
    """在同一事务写 Usage、正式分录投影、请求汇总和预算结算。

    参数：正常路径传 Adapter usage；spool 重放传已严格反序列化的 ``intent``。
    返回：仅在 commit 成功后返回正式 ``FinOpsLedgerCommitted``；existing 重放返回
    ``created=False``。约束：Gateway 不直接插正式分录，0041 trigger 必须在本事务内创建，
    且 tenant/request/金额/快照证据任一不一致都会回滚全部 Usage、Attempt 和预算结算。
    """

    write_intent = intent or _prepare_usage_ledger_intent(usage)
    connection = _connect(config)
    try:
        if replay:
            existing = _load_finops_committed(
                connection,
                tenant_id=context.tenant_id,
                usage_ledger_id=write_intent.usage_ledger_id,
                created=False,
            )
            if existing is not None:
                # 上次 commit 后进程可能在删除 claim 前退出；只确认既有事实，不再写 Audit/Event。
                if (
                    existing.source_request_id != context.request_id
                    or existing.source_trace_id != context.trace_id
                ):
                    # 文件 HMAC 通过后仍必须核对数据库 owner，不能只凭 Usage ID 清理 claim。
                    raise sqlite3.IntegrityError("finops_spool_existing_owner_mismatch")
                # commit 后 crash 可能留下既有 charge 但尚未清理 spool；必须复核/补齐唯一 dispute。
                _ensure_output_safety_billing_evidence(
                    connection,
                    context=context,
                    intent=write_intent,
                    committed=existing,
                )
                if write_intent.captured_estimate is None and context.batch_attempt_id is not None:
                    # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
                    raise sqlite3.IntegrityError("batch_usage_settlement_evidence_missing")
                if write_intent.captured_estimate is not None:
                    _settle_batch_usage_allocation(
                        connection,
                        context=context,
                        intent=write_intent,
                        estimate=write_intent.captured_estimate,
                        replay=True,
                    )
                connection.commit()
                return existing
            _ensure_replay_request_log(connection, context, write_intent.pricing_observed_at)
        if replay and write_intent.captured_estimate is not None:
            estimate = _restore_captured_estimate(
                connection,
                context,
                write_intent.captured_estimate,
                write_intent.pricing_observed_at,
                usage_ledger_id=write_intent.usage_ledger_id,
            )
        elif replay:
            # 原失败发生在选价前时不能使用重放时当前价格；明确记为当时无可用证据。
            estimate = _unpriced_cost(
                connection,
                context,
                now=write_intent.pricing_observed_at,
                usage_ledger_id=write_intent.usage_ledger_id,
            )
        else:
            estimate = _estimate_cost(
                connection,
                context,
                input_tokens=write_intent.input_tokens,
                output_tokens=write_intent.output_tokens,
                now=write_intent.pricing_observed_at,
                usage_ledger_id=write_intent.usage_ledger_id,
                capture_intent=write_intent,
            )
        # 连接建立后即使后续 INSERT/commit 失败，也要把已选价格安全证据写入 spool。
        write_intent.captured_estimate = estimate
        connection.execute(
            """
            INSERT INTO usage_ledger(
              usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
              total_tokens, cache_tokens, raw_usage, normalized_usage,
              cost_amount, cost_cny, cost_usd, currency, price_schema_version,
              estimated, usage_source, price_snapshot_id,
              model_id, provider_id, virtual_key_id, environment_id, environment_code,
              project_id, application_id, endpoint, request_model, resolved_model, provider_code,
              routing_attempt_id,
              created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                write_intent.usage_ledger_id,
                context.tenant_id,
                context.request_id,
                write_intent.input_tokens,
                write_intent.output_tokens,
                write_intent.total_tokens,
                write_intent.cache_tokens,
                json.dumps(write_intent.raw_usage_value, ensure_ascii=False),
                json.dumps(
                    _normalized_usage_payload(
                        write_intent,
                        estimate,
                        posting_source="spool_replay" if replay else "direct",
                    ),
                    ensure_ascii=False,
                ),
                format(estimate.amount, "f"),
                format(estimate.cost_cny, "f"),
                format(estimate.cost_usd, "f"),
                estimate.currency,
                estimate.price_schema_version,
                1 if write_intent.estimated else 0,
                write_intent.usage_source,
                estimate.price_snapshot_id,
                context.model_id,
                context.provider_id,
                context.virtual_key_id,
                context.environment_id,
                context.environment_code,
                context.project_id,
                context.application_id,
                context.endpoint,
                context.request_model,
                context.resolved_model,
                context.provider_code,
                context.routing_attempt_id,
                write_intent.pricing_observed_at,
                write_intent.pricing_observed_at,
            ),
        )
        if context.routing_attempt_id is not None:
            # Usage 已在同一事务创建后才把 attempt 标为 recorded，避免详情出现悬空账本引用。
            cursor = connection.execute(
                """
                UPDATE routing_attempts
                SET usage_status='recorded', usage_ledger_id=?, updated_at=?
                WHERE tenant_id=? AND request_id=? AND routing_attempt_id=? AND status='succeeded'
                """,
                (
                    write_intent.usage_ledger_id,
                    write_intent.pricing_observed_at,
                    context.tenant_id,
                    context.request_id,
                    context.routing_attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                # 成功用量必须唯一关联成功 attempt；缺失关联时回滚账本，避免成本与调用链脱节。
                raise sqlite3.IntegrityError("routing_attempt_usage_link_missing")
        # request_logs 是控制台列表的查询主表，因此把用量和成本同步成可分页过滤的扁平字段。
        connection.execute(
            """
            UPDATE request_logs
            SET input_tokens=?,
                output_tokens=?,
                total_tokens=?,
                estimated_cost_amount=?,
                currency=?,
                price_schema_version=?,
                environment_id=?,
                environment_code=?,
                project_id=?,
                application_id=?,
                updated_at=?
            WHERE tenant_id=? AND request_id=?
            """,
            (
                write_intent.input_tokens,
                write_intent.output_tokens,
                write_intent.total_tokens,
                format(estimate.amount, "f"),
                estimate.currency,
                estimate.price_schema_version,
                context.environment_id,
                context.environment_code,
                context.project_id,
                context.application_id,
                write_intent.pricing_observed_at,
                context.tenant_id,
                context.request_id,
            ),
        )
        _settle_batch_usage_allocation(
            connection,
            context=context,
            intent=write_intent,
            estimate=estimate,
            replay=replay,
        )
        if context.budget_reservation_outstanding_cny is not None:
            # Usage 与未结算覆盖额必须同事务提交；任一失败都保留原总预留并回滚本次账本。
            updated = set_request_budget_reservations_outstanding(
                connection,
                tenant_id=context.tenant_id,
                request_id=context.request_id,
                outstanding_cost_cny=_reservation_outstanding_text(
                    context.budget_reservation_outstanding_cny
                ),
            )
            if updated < 1:
                # Guard 已声明存在预留却无法 tenant-qualified 更新，属于账本一致性故障。
                raise sqlite3.IntegrityError("budget_reservation_settlement_missing")
        committed = _load_finops_committed(
            connection,
            tenant_id=context.tenant_id,
            usage_ledger_id=write_intent.usage_ledger_id,
            created=True,
        )
        if committed is None:
            # 0041 trigger 缺失或被绕过时整笔 Usage 必须失败，不能留下无正式账本的来源行。
            raise sqlite3.IntegrityError("finops_usage_projection_missing")
        _validate_usage_projection(
            committed, context=context, intent=write_intent, estimate=estimate
        )
        # blocked dispute 与 Usage/正式 charge 共用当前事务；零金额只待复核，不伪造可冲正。
        _ensure_output_safety_billing_evidence(
            connection,
            context=context,
            intent=write_intent,
            committed=committed,
        )
        connection.commit()
    # 连接建立后的任意异常都可能发生在 PriceSnapshot 或多表写入之后，必须完整回滚。
    except BaseException:
        connection.rollback()
        raise
    finally:
        # 不依赖异常 traceback 的垃圾回收时机释放连接，避免下一请求被残留事务错误阻断。
        connection.close()
    _log_usage_recorded(
        context,
        input_tokens=write_intent.input_tokens,
        output_tokens=write_intent.output_tokens,
        total_tokens=write_intent.total_tokens,
        cache_tokens=write_intent.cache_tokens,
        estimated=write_intent.estimated,
        cost_amount=estimate.amount,
        currency=estimate.currency,
        price_snapshot_id=estimate.price_snapshot_id,
    )
    return committed


def _settle_batch_usage_allocation(
    connection: sqlite3.Connection,
    *,
    context: RequestLogContext,
    intent: UsageLedgerIntent,
    estimate: CostEstimate,
    replay: bool,
) -> None:
    """在用量账本事务内结算批任务预算分配，并校验任务、尝试和金额归属一致。

    Post all allocation rows and the attempt projection in the Usage/FinOps transaction.
    """

    owner_values = (
        context.batch_job_id,
        context.batch_task_id,
        context.batch_attempt_id,
        context.batch_route_plan_hash,
    )
    if not any(value is not None for value in owner_values):
        if context.batch_allocation_settlement_cny is not None:
            # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
            raise sqlite3.IntegrityError("batch_usage_settlement_owner_missing")
        return
    if any(value is None for value in owner_values):
        # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
        raise sqlite3.IntegrityError("batch_usage_settlement_owner_incomplete")
    settlement = format(estimate.cost_cny, ".6f")
    if replay and context.batch_allocation_settlement_cny is None:
        # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
        raise sqlite3.IntegrityError("batch_usage_settlement_amount_missing")
    if (
        context.batch_allocation_settlement_cny is not None
        and context.batch_allocation_settlement_cny != settlement
    ):
        # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
        raise sqlite3.IntegrityError("batch_usage_settlement_amount_mismatch")
    attempt = connection.execute(
        """
        SELECT status,execution_outcome,result_state,input_tokens,output_tokens,cost_cny
        FROM batch_attempts
        WHERE tenant_id=? AND batch_job_id=? AND batch_task_id=? AND batch_attempt_id=?
          AND request_id=? AND trace_id=?
        """,
        (
            context.tenant_id,
            context.batch_job_id,
            context.batch_task_id,
            context.batch_attempt_id,
            context.request_id,
            context.trace_id,
        ),
    ).fetchone()
    # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
    if attempt is None or str(attempt["status"]) not in {"calling", "provider_succeeded"}:
        # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
        raise sqlite3.IntegrityError("batch_usage_settlement_attempt_invalid")
    rows = connection.execute(
        """
        SELECT allocation.batch_budget_allocation_id,allocation.budget_date,
               allocation.reserved_cost_cny,allocation.route_plan_hash,
               allocation.execution_outcome,allocation.charge_state,allocation.status
        FROM batch_budget_allocations allocation
        WHERE allocation.tenant_id=? AND allocation.batch_job_id=?
          AND allocation.batch_task_id=? AND allocation.batch_attempt_id=?
          AND allocation.budget_date=(
            SELECT MAX(latest.budget_date)
            FROM batch_budget_allocations latest
            WHERE latest.tenant_id=allocation.tenant_id
              AND latest.batch_job_id=allocation.batch_job_id
              AND latest.batch_task_id=allocation.batch_task_id
              AND latest.batch_attempt_id=allocation.batch_attempt_id
          )
        ORDER BY allocation.budget_rule_id
        """,
        (
            context.tenant_id,
            context.batch_job_id,
            context.batch_task_id,
            context.batch_attempt_id,
        ),
    ).fetchall()
    row_states: set[tuple[str, str, str]] = set()
    actual_amount = Decimal(settlement)
    for row in rows:
        try:
            reserved = Decimal(str(row["reserved_cost_cny"]))
        # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
        except (InvalidOperation, ValueError) as exc:
            # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
            raise sqlite3.IntegrityError("batch_usage_settlement_amount_invalid") from exc
        if (
            not reserved.is_finite()
            or reserved < actual_amount
            or str(row["route_plan_hash"]) != context.batch_route_plan_hash
        ):
            # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
            raise sqlite3.IntegrityError("batch_usage_settlement_coverage_invalid")
        row_states.add(
            (
                str(row["execution_outcome"]),
                str(row["charge_state"]),
                str(row["status"]),
            )
        )
    active_state = ("not_invoked", "none", "active")
    posted_state = ("succeeded", "posted", "posted")
    if row_states and row_states not in ({active_state}, {posted_state}):
        # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
        raise sqlite3.IntegrityError("batch_usage_settlement_state_invalid")
    if row_states == {active_state}:
        latest_budget_date = str(rows[0]["budget_date"])
        cursor = connection.execute(
            """
            UPDATE batch_budget_allocations
            SET execution_outcome='succeeded',charge_state='posted',status='posted',updated_at=?
            WHERE tenant_id=? AND batch_job_id=? AND batch_task_id=? AND batch_attempt_id=?
              AND budget_date=? AND execution_outcome='not_invoked'
              AND charge_state='none' AND status='active'
            """,
            (
                intent.pricing_observed_at,
                context.tenant_id,
                context.batch_job_id,
                context.batch_task_id,
                context.batch_attempt_id,
                latest_budget_date,
            ),
        )
        if cursor.rowcount != len(rows):
            # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
            raise sqlite3.IntegrityError("batch_usage_settlement_allocation_conflict")
    if str(attempt["status"]) == "calling":
        cursor = connection.execute(
            """
            UPDATE batch_attempts
            SET status='provider_succeeded',execution_outcome='succeeded',retry_safe=0,
                gateway_error_code=NULL,input_tokens=?,output_tokens=?,cost_cny=?,updated_at=?
            WHERE tenant_id=? AND batch_attempt_id=? AND status='calling'
              AND execution_outcome='not_invoked' AND result_state='none'
            """,
            (
                intent.input_tokens,
                intent.output_tokens,
                settlement,
                intent.pricing_observed_at,
                context.tenant_id,
                context.batch_attempt_id,
            ),
        )
        if cursor.rowcount != 1:
            # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
            raise sqlite3.IntegrityError("batch_usage_settlement_attempt_conflict")
    elif (
        str(attempt["execution_outcome"]) == "succeeded"
        and attempt["input_tokens"] is not None
        and attempt["output_tokens"] is not None
        and int(attempt["input_tokens"]) == intent.input_tokens
        and int(attempt["output_tokens"]) == intent.output_tokens
        and attempt["cost_cny"] is None
    ):
        # A durable v6 spool may be followed by encrypted result persistence before replay.
        cursor = connection.execute(
            """
            UPDATE batch_attempts SET cost_cny=?,updated_at=?
            WHERE tenant_id=? AND batch_attempt_id=? AND status='provider_succeeded'
              AND execution_outcome='succeeded' AND cost_cny IS NULL
            """,
            (
                settlement,
                intent.pricing_observed_at,
                context.tenant_id,
                context.batch_attempt_id,
            ),
        )
        if cursor.rowcount != 1:
            # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
            raise sqlite3.IntegrityError("batch_usage_settlement_attempt_conflict")
    elif (
        str(attempt["execution_outcome"]) != "succeeded"
        or attempt["input_tokens"] is None
        or attempt["output_tokens"] is None
        or attempt["cost_cny"] is None
        or int(attempt["input_tokens"]) != intent.input_tokens
        or int(attempt["output_tokens"]) != intent.output_tokens
        or format(Decimal(str(attempt["cost_cny"])), ".6f") != settlement
    ):
        # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
        raise sqlite3.IntegrityError("batch_usage_settlement_attempt_mismatch")
def _ensure_output_safety_billing_evidence(
    connection: sqlite3.Connection,
    *,
    context: RequestLogContext,
    intent: UsageLedgerIntent,
    committed: FinOpsLedgerCommitted,
) -> None:
    """复核 v5 scan owner，并为 blocked charge 幂等创建 append-only dispute。

    参数：当前 Usage 事务连接、可信上下文、Usage 意图和正式 charge 摘要。返回值：无。
    关键约束：scan/Usage/charge 必须同 tenant/request/trace；冲正资格只按正式 charge
    ``amount_micros > 0`` 派生，数据面绝不自动创建 reversal。
    """

    output_context = _validate_output_safety_spool_context(
        context.output_safety_scan_id,
        context.output_safety_scan_status,
        context.output_safety_decision,
        context.billing_disposition,
        allow_absent=True,
    )
    if output_context is None:
        return
    scan_id, scan_status, decision, billing = output_context
    scan = connection.execute(
        """
        SELECT scan_status,decision,billing_disposition,trace_id
        FROM output_safety_scans
        WHERE tenant_id=? AND request_id=? AND output_safety_scan_id=?
        """,
        (context.tenant_id, context.request_id, scan_id),
    ).fetchone()
    if scan is None or tuple(scan) != (
        scan_status,
        decision,
        billing,
        context.trace_id,
    ):
        # HMAC 正确的 spool 仍不能覆盖数据库中的另一 scan owner 或不同终态。
        raise sqlite3.IntegrityError("finops_output_safety_scan_mismatch")
    if billing != "dispute_pending":
        return
    charge = connection.execute(
        """
        SELECT amount_micros,entry_type,source_request_id,source_trace_id,usage_ledger_id
        FROM finops_ledger_entries
        WHERE tenant_id=? AND finops_ledger_entry_id=?
        """,
        (context.tenant_id, committed.ledger_entry_id),
    ).fetchone()
    if charge is None or tuple(charge)[1:] != (
        "usage_charge",
        context.request_id,
        context.trace_id,
        intent.usage_ledger_id,
    ):
        # 争议只能绑定当前请求刚提交的正式 charge，缺失或 owner 错配都表示账本完整性损坏。
        raise sqlite3.IntegrityError("finops_output_safety_charge_mismatch")
    reversal_eligible = 1 if int(charge["amount_micros"]) > 0 else 0
    existing = connection.execute(
        """
        SELECT tenant_id,request_id,trace_id,output_safety_scan_id,usage_ledger_id,
               finops_ledger_entry_id,review_status,reversal_eligible,reason_code
        FROM finops_ledger_disputes
        WHERE tenant_id=? AND request_id=? AND output_safety_scan_id=?
        """,
        (context.tenant_id, context.request_id, scan_id),
    ).fetchone()
    expected = (
        context.tenant_id,
        context.request_id,
        context.trace_id,
        scan_id,
        intent.usage_ledger_id,
        committed.ledger_entry_id,
        "dispute_pending",
        reversal_eligible,
        "output_safety.content_policy_violation",
    )
    if existing is not None:
        if tuple(existing) != expected:
            # 同 scan 已有不同账单 owner 或资格表示不可变事实损坏，不能以重放覆盖。
            raise sqlite3.IntegrityError("finops_output_safety_dispute_conflict")
        return
    connection.execute(
        """
        INSERT INTO finops_ledger_disputes(
          finops_ledger_dispute_id,tenant_id,request_id,trace_id,output_safety_scan_id,
          usage_ledger_id,finops_ledger_entry_id,review_status,reversal_eligible,
          reason_code,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"fld_{uuid4().hex}",
            *expected[:6],
            expected[6],
            expected[7],
            expected[8],
            committed.created_at,
        ),
    )


def _prepare_usage_ledger_intent(usage: object) -> UsageLedgerIntent:
    """在打开数据库前防御性归一 Adapter usage，并预生成稳定 Usage ID/观测时刻。"""

    usage_payload = usage if isinstance(usage, dict) else {}
    raw_usage_value = sanitize_provider_usage(usage_payload.get("raw_usage"))
    input_tokens = _int_usage(
        usage_payload.get("prompt_tokens")
        or usage_payload.get("input_tokens")
        or raw_usage_value.get("prompt_tokens")
        or raw_usage_value.get("input_tokens")
    )
    output_tokens = _int_usage(
        usage_payload.get("completion_tokens")
        or usage_payload.get("output_tokens")
        or raw_usage_value.get("completion_tokens")
        or raw_usage_value.get("output_tokens")
    )
    cache_tokens = _int_usage(
        usage_payload.get("cache_tokens") or raw_usage_value.get("cache_tokens")
    )
    total_tokens = _int_usage(
        usage_payload.get("total_tokens") or raw_usage_value.get("total_tokens")
    )
    # 上游缺少总量时只由已归一输入输出相加，避免信任不完整 raw usage 字段。
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    estimated = _bool_usage(usage_payload.get("estimated"))
    return UsageLedgerIntent(
        usage_ledger_id=f"usg_{uuid4().hex}",
        pricing_observed_at=_to_db_time(_utcnow()),
        raw_usage_value=raw_usage_value,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_tokens=cache_tokens,
        estimated=estimated,
        usage_source=_normalize_usage_source(usage_payload.get("usage_source"), estimated),
        estimate_reason=str(usage_payload.get("estimate_reason") or "") or None,
        provider_cost_amount=_optional_decimal_text(
            usage_payload.get("provider_cost_amount")
        ),
        provider_cost_currency=_optional_currency(
            usage_payload.get("provider_cost_currency")
        ),
        provider_cost_cny=_optional_decimal_text(usage_payload.get("provider_cost_cny")),
        normalization_warnings=_normalization_warnings(
            usage_payload.get("normalization_warnings")
        ),
    )


def _validate_output_safety_spool_context(
    scan_id_value: object,
    scan_status_value: object,
    decision_value: object,
    billing_value: object,
    *,
    allow_absent: bool,
) -> tuple[str, str, str, str] | None:
    """校验 v5 仅允许的 OutputSafetyScan 安全摘要。

    参数：scan ID、二维终态、账单意图及是否允许四字段全部缺失。返回：规范四元组或 None。
    关键约束：任意半组、pending、未知枚举或冲突组合都拒绝，正文和规则元数据不在该合同中。
    """

    values = (scan_id_value, scan_status_value, decision_value, billing_value)
    if all(value is None for value in values):
        if allow_absent:
            return None
        # v5 明确要求安全四元组时，全部缺失不能退回旧版无 scan 语义。
        raise ValueError("finops_spool_output_safety_missing")
    # 四元组只缺部分字段无法证明同一终态，拒绝重放而不是猜测默认值。
    if any(value is None for value in values):
        raise ValueError("finops_spool_payload_invalid")
    scan_id = _spool_prefixed_id(scan_id_value, prefix="oss_")
    scan_status = _spool_required_text(scan_status_value, maximum=32)
    decision = _spool_required_text(decision_value, maximum=32)
    billing = _spool_required_text(billing_value, maximum=32)
    valid = (
        (scan_status == "not_configured" and decision == "none" and billing == "not_required")
        or (
            scan_status == "completed"
            and decision in {"clean", "matched"}
            and billing == "not_required"
        )
        or (
            scan_status == "completed"
            and decision == "blocked"
            and billing == "dispute_pending"
        )
        or (
            scan_status == "unavailable"
            and decision in {"unknown", "matched"}
            and billing == "not_required"
        )
        or (
            scan_status == "not_applicable"
            and decision == "none"
            and billing == "not_required"
        )
    )
    if not valid:
        # pending 或自相矛盾状态不能写入 crash spool，否则重放会伪造已经完成的安全证据。
        raise ValueError("finops_spool_payload_invalid")
    return scan_id, scan_status, decision, billing


def _serialize_usage_spool_record(
    context: RequestLogContext,
    intent: UsageLedgerIntent,
) -> dict[str, Any]:
    """把失败 Usage 转成严格白名单记录，不保存 raw usage、Endpoint 或任何正文。

    参数：可信请求上下文和预生成 Usage 意图。返回：可由 spool 进行 canonical JSON/HMAC
    持久化的版本化对象。约束：价格已经选出时必须连同不可变证据保存，不能让重放改用现价。
    """

    output_safety_values = _validate_output_safety_spool_context(
        context.output_safety_scan_id,
        context.output_safety_scan_status,
        context.output_safety_decision,
        context.billing_disposition,
        allow_absent=True,
    )
    batch_values = (
        context.batch_job_id,
        context.batch_task_id,
        context.batch_attempt_id,
        context.batch_route_plan_hash,
    )
    batch_present = any(value is not None for value in batch_values)
    if batch_present and (
        any(value is None for value in batch_values)
        or context.batch_allocation_settlement_cny is not None
        or intent.captured_estimate is None
        or output_safety_values is None
    ):
        # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
        raise ValueError("finops_spool_batch_context_invalid")
    # Batch always uses v6. Other Chat uses v5 when Output Safety has a complete owner.
    record_version = 6 if batch_present else (5 if output_safety_values is not None else 4)
    context_fields = {
        4: _SPOOL_CONTEXT_FIELDS_V4,
        5: _SPOOL_CONTEXT_FIELDS_V5,
        6: _SPOOL_CONTEXT_FIELDS_V6,
    }[record_version]
    # 字段集合是显式白名单；RequestLogContext 未来新增字段不会自动落盘。
    context_payload = {name: getattr(context, name) for name in context_fields}
    if batch_present:
        context_payload["batch_allocation_settlement_cny"] = format(
            intent.captured_estimate.cost_cny, ".6f"
        )
    # 写盘前复用严格状态矩阵；无效上下文不能依赖重放时才发现。
    _deserialize_spool_intent_context(context_payload)
    estimate = intent.captured_estimate
    estimate_payload: dict[str, Any] | None = None
    # 已选价意图必须同时携带完整证据，否则重放无法证明没有改用当前价格。
    if estimate is not None:
        evidence = estimate.price_snapshot_evidence
        # 缺少快照证据时拒绝落盘，不能生成看似可恢复但无法审计的半记录。
        if evidence is None:
            raise ValueError("finops_spool_price_evidence_missing")
        estimate_payload = {
            "amount": format(estimate.amount, ".6f"),
            "currency": estimate.currency,
            "price_schema_version": estimate.price_schema_version,
            "cost_cny": format(estimate.cost_cny, ".6f"),
            "cost_usd": format(estimate.cost_usd, ".6f"),
            "price_snapshot_id": estimate.price_snapshot_id,
            "evidence": {
                "price_snapshot_id": evidence.price_snapshot_id,
                "model_id": evidence.model_id,
                "provider_id": evidence.provider_id,
                "provider_code": evidence.provider_code,
                "currency": evidence.currency,
                "input_price_per_1k": evidence.input_price_per_1k,
                "output_price_per_1k": evidence.output_price_per_1k,
                "price_schema_version": evidence.price_schema_version,
                "source": evidence.source,
                "price_plan_id": evidence.price_plan_id,
                "effective_from": evidence.effective_from,
            },
        }
    return {
        "schema_version": record_version,
        "context": context_payload,
        "usage": {
            "usage_ledger_id": intent.usage_ledger_id,
            "pricing_observed_at": intent.pricing_observed_at,
            "input_tokens": intent.input_tokens,
            "output_tokens": intent.output_tokens,
            "total_tokens": intent.total_tokens,
            "cache_tokens": intent.cache_tokens,
            "estimated": intent.estimated,
            "usage_source": intent.usage_source,
            "provider_cost_amount": intent.provider_cost_amount,
            "provider_cost_currency": intent.provider_cost_currency,
            "provider_cost_cny": intent.provider_cost_cny,
            "normalization_warnings": list(intent.normalization_warnings),
        },
        "estimate": estimate_payload,
    }


def replay_usage_spool_record(
    config: AppConfig,
    payload: dict[str, Any],
) -> FinOpsLedgerCommitted:
    """严格反序列化一条已验 HMAC 的 spool 记录，并执行 tenant-qualified 幂等入账。

    参数：数据库配置和 spool payload。返回：本次新建或已存在的正式分录。
    约束：文件真实性不能代替字段、租户和数据库 owner 校验；未知字段一律拒绝。
    """

    # 严格解码后的 tenant/context 仍由数据库 owner 约束复核，不能只依赖文件 HMAC。
    context, intent = _deserialize_usage_spool_record(payload)
    return record_usage_ledger(config, context, {}, intent=intent, replay=True)


def _deserialize_usage_spool_record(
    payload: object,
) -> tuple[RequestLogContext, UsageLedgerIntent]:
    """从 JSON 值恢复安全上下文和 Usage 意图，逐字段拒绝宽松类型与额外数据。"""

    record = _spool_mapping(
        payload,
        frozenset({"schema_version", "context", "usage", "estimate"}),
    )
    # v1-v6 均为已审查的升级证据；历史版本缺少新增摘要只能解释为“未记录”，未来版本仍拒绝。
    record_version = record["schema_version"]
    if record_version not in {1, 2, 3, 4, 5, _SPOOL_RECORD_SCHEMA_VERSION}:
        raise ValueError("finops_spool_payload_version_invalid")
    context_fields = {
        1: _SPOOL_CONTEXT_FIELDS_V1,
        2: _SPOOL_CONTEXT_FIELDS_V2,
        3: _SPOOL_CONTEXT_FIELDS_V3,
        4: _SPOOL_CONTEXT_FIELDS_V4,
        5: _SPOOL_CONTEXT_FIELDS_V5,
        6: _SPOOL_CONTEXT_FIELDS_V6,
    }[record_version]
    context_value = _spool_mapping(record["context"], context_fields)
    usage_value = _spool_mapping(record["usage"], _SPOOL_USAGE_FIELDS)

    tenant_id = _spool_required_text(context_value["tenant_id"], maximum=128)
    request_id = _spool_required_text(context_value["request_id"], maximum=128)
    trace_id = _spool_required_text(context_value["trace_id"], maximum=128)
    optional_context_names = context_fields - {
        "tenant_id",
        "request_id",
        "trace_id",
        "snapshot_version",
        "policy_snapshot_status",
        "policy_mask_applied",
        "intent_risk_schedule_status",
        "intent_risk_score",
        "intent_risk_input_truncated",
        "session_risk_status",
        "budget_current_cost_cny",
        "budget_limit_cny",
        "budget_reservation_outstanding_cny",
        "dlp_match_count",
        "fallback_triggered",
        "routing_attempt_count",
        "batch_allocation_settlement_cny",
    }
    optional_context = {
        name: _spool_optional_text(context_value[name], maximum=256)
        for name in optional_context_names
    }
    snapshot_version = _spool_optional_nonnegative_int(context_value["snapshot_version"])
    policy_snapshot_status = _spool_required_text(
        context_value["policy_snapshot_status"], maximum=32
    )
    dlp_match_count = _spool_nonnegative_int(context_value["dlp_match_count"])
    routing_attempt_count = _spool_nonnegative_int(
        context_value["routing_attempt_count"]
    )
    fallback_triggered = _spool_bool(context_value["fallback_triggered"])
    policy_mask_applied = (
        False
        if record_version == 1
        else _spool_bool(context_value["policy_mask_applied"])
    )
    policy_mask_policy_id = (
        None
        if record_version == 1
        else optional_context["policy_mask_policy_id"]
    )
    if policy_mask_applied and policy_mask_policy_id is None:
        # spool 不能恢复没有来源 ID 的 applied 证据，否则数据库 CHECK 只会给出不透明错误。
        raise ValueError("finops_spool_payload_invalid")
    intent_context = (
        _disabled_spool_intent_context()
        if record_version < 3
        else _deserialize_spool_intent_context(context_value)
    )
    session_risk_context = (
        _not_applicable_spool_session_risk_context()
        if record_version < 4
        else _deserialize_spool_session_risk_context(context_value)
    )
    output_safety_context = (
        None
        if record_version < 5
        else _validate_output_safety_spool_context(
            context_value["output_safety_scan_id"],
            context_value["output_safety_scan_status"],
            context_value["output_safety_decision"],
            context_value["billing_disposition"],
            allow_absent=False,
        )
    )
    budget_current = _spool_optional_decimal_text(
        context_value["budget_current_cost_cny"]
    )
    budget_limit = _spool_optional_decimal_text(context_value["budget_limit_cny"])
    reservation_outstanding = _spool_optional_decimal_text(
        context_value["budget_reservation_outstanding_cny"]
    )
    if record_version == 6:
        batch_job_id = _spool_prefixed_id(context_value["batch_job_id"], prefix="bjob_")
        batch_task_id = _spool_prefixed_id(context_value["batch_task_id"], prefix="btsk_")
        batch_attempt_id = _spool_prefixed_id(
            context_value["batch_attempt_id"], prefix="batm_"
        )
        batch_route_plan_hash = _spool_required_text(
            context_value["batch_route_plan_hash"], maximum=64
        )
        if len(batch_route_plan_hash) != 64 or any(
            character not in "0123456789abcdef"
            for character in batch_route_plan_hash
        ):
            # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
            raise ValueError("finops_spool_payload_invalid")
        batch_settlement = _spool_money_text(
            context_value["batch_allocation_settlement_cny"]
        )
    else:
        batch_job_id = None
        batch_task_id = None
        batch_attempt_id = None
        batch_route_plan_hash = None
        batch_settlement = None
    context = RequestLogContext(
        tenant_id=tenant_id,
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=optional_context["virtual_key_id"],
        environment_id=optional_context["environment_id"],
        environment_code=optional_context["environment_code"],
        project_id=optional_context["project_id"],
        application_id=optional_context["application_id"],
        provider_id=optional_context["provider_id"],
        model_id=optional_context["model_id"],
        endpoint=None,
        request_model=optional_context["request_model"],
        resolved_model=optional_context["resolved_model"],
        provider_code=optional_context["provider_code"],
        snapshot_version=snapshot_version,
        policy_snapshot_status=policy_snapshot_status,
        policy_decision=optional_context["policy_decision"],
        policy_reason_code=optional_context["policy_reason_code"],
        policy_id=optional_context["policy_id"],
        policy_action_type=optional_context["policy_action_type"],
        policy_target_model_id=optional_context["policy_target_model_id"],
        policy_mask_applied=policy_mask_applied,
        policy_mask_policy_id=policy_mask_policy_id,
        budget_status=optional_context["budget_status"],
        budget_rule_id=optional_context["budget_rule_id"],
        budget_scope_type=optional_context["budget_scope_type"],
        budget_scope_id=optional_context["budget_scope_id"],
        budget_action=optional_context["budget_action"],
        budget_reason_code=optional_context["budget_reason_code"],
        budget_current_cost_cny=budget_current,
        budget_limit_cny=budget_limit,
        budget_reservation_outstanding_cny=reservation_outstanding,
        dlp_status=optional_context["dlp_status"],
        dlp_rule_id=optional_context["dlp_rule_id"],
        dlp_action=optional_context["dlp_action"],
        dlp_match_count=dlp_match_count,
        dlp_reason_code=optional_context["dlp_reason_code"],
        intent_risk_result_id=intent_context["intent_risk_result_id"],
        intent_risk_schedule_status=intent_context["intent_risk_schedule_status"],
        intent_risk_mode=intent_context["intent_risk_mode"],
        intent_risk_type=intent_context["intent_risk_type"],
        intent_risk_score=intent_context["intent_risk_score"],
        intent_risk_action=intent_context["intent_risk_action"],
        intent_risk_reason_code=intent_context["intent_risk_reason_code"],
        intent_risk_detector_version=intent_context["intent_risk_detector_version"],
        intent_risk_input_truncated=intent_context["intent_risk_input_truncated"],
        session_risk_status=session_risk_context["session_risk_status"],
        session_risk_score_id=session_risk_context["session_risk_score_id"],
        session_risk_scope_type=session_risk_context["session_risk_scope_type"],
        session_risk_state=session_risk_context["session_risk_state"],
        session_risk_reason_code=session_risk_context["session_risk_reason_code"],
        fallback_triggered=fallback_triggered,
        routing_attempt_count=routing_attempt_count,
        final_routing_attempt_id=optional_context["final_routing_attempt_id"],
        routing_attempt_id=optional_context["routing_attempt_id"],
        stream_session_id=optional_context["stream_session_id"],
        stream_status=optional_context["stream_status"],
        output_safety_scan_id=(output_safety_context[0] if output_safety_context else None),
        output_safety_scan_status=(
            output_safety_context[1] if output_safety_context else None
        ),
        output_safety_decision=(
            output_safety_context[2] if output_safety_context else None
        ),
        billing_disposition=(output_safety_context[3] if output_safety_context else None),
        batch_job_id=batch_job_id,
        batch_task_id=batch_task_id,
        batch_attempt_id=batch_attempt_id,
        batch_route_plan_hash=batch_route_plan_hash,
        batch_allocation_settlement_cny=(
            format(batch_settlement, ".6f") if batch_settlement is not None else None
        ),
    )

    usage_ledger_id = _spool_prefixed_id(
        usage_value["usage_ledger_id"], prefix="usg_"
    )
    pricing_observed_at = _spool_db_time(usage_value["pricing_observed_at"])
    warnings_value = usage_value["normalization_warnings"]
    # 警告列表只接受冻结枚举且禁止重复，避免重放后规范化事实发生解释漂移。
    if (
        type(warnings_value) is not list
        or any(
            type(item) is not str or item not in NORMALIZATION_WARNING_CODES
            for item in warnings_value
        )
        or list(dict.fromkeys(warnings_value)) != warnings_value
    ):
        # 任一类型、枚举或顺序唯一性检查失败都说明持久记录不再可信。
        raise ValueError("finops_spool_payload_invalid")
    usage_source = _spool_required_text(usage_value["usage_source"], maximum=32)
    # Usage 来源决定估算语义，只允许上游真实值或本地估算两类冻结状态。
    if usage_source not in {"upstream", "estimated"}:
        raise ValueError("finops_spool_payload_invalid")
    provider_currency = usage_value["provider_cost_currency"]
    # Provider 原始成本只接受当前支持币种，未知币种不能被静默当作无成本。
    if provider_currency is not None and provider_currency not in {"CNY", "USD"}:
        raise ValueError("finops_spool_payload_invalid")
    intent = UsageLedgerIntent(
        usage_ledger_id=usage_ledger_id,
        pricing_observed_at=pricing_observed_at,
        # raw usage 可能包含 Provider 扩展或正文，spool 永不持久化，重放只写安全归一字段。
        raw_usage_value={},
        input_tokens=_spool_nonnegative_int(usage_value["input_tokens"]),
        output_tokens=_spool_nonnegative_int(usage_value["output_tokens"]),
        total_tokens=_spool_nonnegative_int(usage_value["total_tokens"]),
        cache_tokens=_spool_nonnegative_int(usage_value["cache_tokens"]),
        estimated=_spool_bool(usage_value["estimated"]),
        usage_source=usage_source,
        # estimate_reason 可能由 Provider 文本派生，因此不跨进程持久化。
        estimate_reason=None,
        provider_cost_amount=_spool_optional_decimal_text(
            usage_value["provider_cost_amount"]
        ),
        provider_cost_currency=provider_currency,
        provider_cost_cny=_spool_optional_decimal_text(
            usage_value["provider_cost_cny"]
        ),
        normalization_warnings=list(warnings_value),
        captured_estimate=_deserialize_spool_estimate(record["estimate"]),
    )
    if record_version == 6 and (
        intent.captured_estimate is None
        or context.batch_allocation_settlement_cny
        != format(intent.captured_estimate.cost_cny, ".6f")
    ):
        # 结算证据必须完整且归属一致；冲突时中止事务，防止重复或错误记账。
        raise ValueError("finops_spool_payload_invalid")
    return context, intent


def _disabled_spool_intent_context() -> dict[str, Any]:
    """为 v1/v2 历史记录构造明确 disabled 的意图调度摘要。"""

    return {
        "intent_risk_result_id": None,
        "intent_risk_schedule_status": "disabled",
        "intent_risk_mode": None,
        "intent_risk_type": None,
        "intent_risk_score": None,
        "intent_risk_action": None,
        "intent_risk_reason_code": None,
        "intent_risk_detector_version": None,
        "intent_risk_input_truncated": False,
    }


def _deserialize_spool_intent_context(value: dict[str, Any]) -> dict[str, Any]:
    """严格解析 v3+ 的九字段意图摘要并验证完整状态矩阵。

    参数：已经通过 v3 exact-set 的 context 映射。返回：可直接构造 RequestLogContext 的
    字段字典。关键约束：不得接受 bool 充当分数、任意 reason/version 或缺失 result 身份。
    """

    result_value = value["intent_risk_result_id"]
    result_id = (
        None
        if result_value is None
        else _spool_prefixed_id(result_value, prefix="irr_")
    )
    status = _spool_required_text(value["intent_risk_schedule_status"], maximum=32)
    mode = _spool_optional_text(value["intent_risk_mode"], maximum=32)
    risk_type = _spool_optional_text(value["intent_risk_type"], maximum=64)
    score = _spool_optional_nonnegative_int(value["intent_risk_score"])
    action = _spool_optional_text(value["intent_risk_action"], maximum=32)
    reason = _spool_optional_text(value["intent_risk_reason_code"], maximum=64)
    detector = _spool_optional_text(value["intent_risk_detector_version"], maximum=128)
    truncated = _spool_bool(value["intent_risk_input_truncated"])

    blocked_contracts = {
        "explicit_credential_extraction": ("credential_exfiltration", 9900),
        "explicit_system_prompt_request": ("system_prompt_extraction", 9800),
        "explicit_policy_bypass": ("policy_bypass", 9700),
        "explicit_instruction_override": ("jailbreak", 9600),
    }
    disabled = (
        status == "disabled"
        and result_id is None
        and mode is None
        and risk_type is None
        and score is None
        and action is None
        and reason is None
        and detector is None
        and not truncated
    )
    queued = (
        status == "queued"
        and result_id is not None
        and mode == "async_light"
        and risk_type is None
        and score is None
        and action is None
        and reason == "queued"
        and detector is not None
    )
    skipped = (
        status == "skipped"
        and result_id is not None
        and mode == "async_light"
        and risk_type is None
        and score is None
        and action is None
        and reason in {"queue_full", "worker_unavailable"}
        and detector is not None
    )
    blocked = (
        status == "blocked"
        and result_id is not None
        and mode == "sync_strong"
        and reason in blocked_contracts
        and blocked_contracts.get(reason) == (risk_type, score)
        and action == "block"
        and detector is not None
    )
    failed_closed = (
        status == "failed_closed"
        and mode in {"sync_strong", "sync_session"}
        and risk_type is None
        and score is None
        and action == "none"
        and reason in {"classifier_failed", "result_store_unavailable"}
        and detector is not None
        and (reason != "result_store_unavailable" or result_id is None)
    )
    completed_contracts = {
        "explicit_credential_extraction": ("credential_exfiltration", 9900),
        "explicit_system_prompt_request": ("system_prompt_extraction", 9800),
        "explicit_policy_bypass": ("policy_bypass", 9700),
        "explicit_instruction_override": ("jailbreak", 9600),
        "credential_access_request": ("credential_exfiltration", 7600),
        "system_prompt_access_request": ("system_prompt_extraction", 7400),
    }
    completed_clean = (
        status == "completed"
        and result_id is not None
        and mode == "sync_session"
        and risk_type is None
        and score == 0
        and action == "allow"
        and reason == "no_risk_detected"
        and detector is not None
    )
    completed_risk = (
        status == "completed"
        and result_id is not None
        and mode == "sync_session"
        and reason in completed_contracts
        and completed_contracts.get(reason) == (risk_type, score)
        and action in {"observe", "block"}
        and (action != "block" or score is not None and score >= 9_500)
        and detector is not None
    )
    if not (
        disabled
        or queued
        or skipped
        or blocked
        or failed_closed
        or completed_clean
        or completed_risk
    ):
        # 状态矩阵漂移意味着 HMAC 文件虽未被篡改，但已不满足当前可恢复业务合同。
        raise ValueError("finops_spool_payload_invalid")
    return {
        "intent_risk_result_id": result_id,
        "intent_risk_schedule_status": status,
        "intent_risk_mode": mode,
        "intent_risk_type": risk_type,
        "intent_risk_score": score,
        "intent_risk_action": action,
        "intent_risk_reason_code": reason,
        "intent_risk_detector_version": detector,
        "intent_risk_input_truncated": truncated,
    }


def _not_applicable_spool_session_risk_context() -> dict[str, Any]:
    """为 v1-v3 历史记录构造升级前不存在的明确 not_applicable 摘要。"""

    return {
        "session_risk_status": "not_applicable",
        "session_risk_score_id": None,
        "session_risk_scope_type": None,
        "session_risk_state": None,
        "session_risk_reason_code": None,
    }


def _deserialize_spool_session_risk_context(value: dict[str, Any]) -> dict[str, Any]:
    """严格解析 v4 五字段会话风险摘要，不接受 scope key 或摘要扩展。

    参数：已通过 v4 exact-set 的 context。返回：RequestLogContext 可直接使用的固定字段。
    关键约束：score ID 只能是 ``srs_`` 随机身份；状态矩阵与 0062 trigger 等价。
    """

    status = _spool_required_text(value["session_risk_status"], maximum=32)
    score_value = value["session_risk_score_id"]
    score_id = (
        None if score_value is None else _spool_prefixed_id(score_value, prefix="srs_")
    )
    scope_type = _spool_optional_text(value["session_risk_scope_type"], maximum=32)
    state = _spool_optional_text(value["session_risk_state"], maximum=32)
    reason = _spool_optional_text(value["session_risk_reason_code"], maximum=64)
    valid_scope = scope_type in {"enterprise_chat", "api_session"}
    not_applicable = (
        status == "not_applicable"
        and score_id is None
        and scope_type is None
        and state is None
        and reason is None
    )
    disabled = (
        status == "disabled"
        and score_id is None
        and scope_type is None
        and state is None
        and reason in {"feature_disabled", "no_session_identifiers"}
    )
    clean = (
        status == "clean"
        and score_id is None
        and valid_scope
        and state is None
        and reason == "clean_turn"
    )
    accumulated = (
        status in {"monitoring", "high_risk", "frozen"}
        and score_id is not None
        and valid_scope
        and state == status
        and reason in {"risk_accumulated", "session_frozen", "key_frozen"}
    )
    duplicate = status == "duplicate" and valid_scope and reason == "duplicate_turn"
    degraded = (
        status == "degraded"
        and score_id is None
        and valid_scope
        and state is None
        and reason == "store_degraded"
    )
    failed = status == "failed" and reason in {
        "store_unavailable",
        "evidence_unavailable",
        "session_frozen",
        "key_frozen",
        "turn_conflict",
        "hmac_version_unavailable",
    }
    # 任意半组或未知枚举都可能伪造会话风险证据，spool 恢复时必须固定拒绝整条记录。
    if not (not_applicable or disabled or clean or accumulated or duplicate or degraded or failed):
        raise ValueError("finops_spool_payload_invalid")
    return {
        "session_risk_status": status,
        "session_risk_score_id": score_id,
        "session_risk_scope_type": scope_type,
        "session_risk_state": state,
        "session_risk_reason_code": reason,
    }


def _deserialize_spool_estimate(value: object) -> CostEstimate | None:
    """恢复原入账时价格证据，并校验原币金额、CNY/USD 分拆与快照字段互相一致。"""

    if value is None:
        return None
    estimate_value = _spool_mapping(value, _SPOOL_ESTIMATE_FIELDS)
    evidence_value = _spool_mapping(
        estimate_value["evidence"], _SPOOL_EVIDENCE_FIELDS
    )
    amount = _spool_money_text(estimate_value["amount"])
    cost_cny = _spool_money_text(estimate_value["cost_cny"])
    cost_usd = _spool_money_text(estimate_value["cost_usd"])
    currency = estimate_value["currency"]
    # spool 恢复只支持 PRD 冻结币种，未知值不允许按零金额降级。
    if currency not in {"CNY", "USD"}:
        raise ValueError("finops_spool_payload_invalid")
    expected_cny, expected_usd = _cost_amounts(amount, currency)
    # 原币金额与 CNY/USD 投影必须精确一致，防止文件字段被局部替换后通过 HMAC 之外的逻辑。
    if cost_cny != expected_cny or cost_usd != expected_usd:
        raise ValueError("finops_spool_payload_invalid")
    snapshot_id = _spool_prefixed_id(
        estimate_value["price_snapshot_id"], prefix="prs_"
    )
    evidence_snapshot_id = _spool_prefixed_id(
        evidence_value["price_snapshot_id"], prefix="prs_"
    )
    version = _spool_required_text(
        estimate_value["price_schema_version"], maximum=128
    )
    evidence_version = _spool_required_text(
        evidence_value["price_schema_version"], maximum=128
    )
    evidence_currency = evidence_value["currency"]
    source = evidence_value["source"]
    # Estimate 与 Evidence 的快照身份、版本、币种和来源必须组成同一不可变事实。
    if (
        snapshot_id != evidence_snapshot_id
        or version != evidence_version
        or currency != evidence_currency
        or source not in {"price_plan", "model_catalog", "finops_unpriced"}
    ):
        # 跨字段不一致意味着价格证据可能被错配，必须整条拒绝而不是局部修补。
        raise ValueError("finops_spool_payload_invalid")
    input_price = _spool_price_text(evidence_value["input_price_per_1k"])
    output_price = _spool_price_text(evidence_value["output_price_per_1k"])
    price_plan_id = _spool_optional_text(evidence_value["price_plan_id"], maximum=128)
    effective_from = _spool_optional_text(evidence_value["effective_from"], maximum=64)
    # 缺价、价格计划和模型目录三类来源各有互斥字段合同，禁止跨类型拼接证据。
    if source == "finops_unpriced":
        if (
            version != "unpriced-v1"
            or currency != "CNY"
            or amount != Decimal("0")
            or input_price != Decimal("0")
            or output_price != Decimal("0")
            or price_plan_id is not None
        ):
            # 缺价证据只能是零额、零单价和固定版本，任何偏差都不是原始缺价事实。
            raise ValueError("finops_spool_payload_invalid")
    elif source == "price_plan" and price_plan_id is None:
        # 计划价格没有计划身份便无法审计生效版本，因此拒绝恢复。
        raise ValueError("finops_spool_payload_invalid")
    elif source == "model_catalog" and price_plan_id is not None:
        # 模型目录价格禁止夹带计划身份，避免来源标签与真实证据矛盾。
        raise ValueError("finops_spool_payload_invalid")
    evidence = PriceSnapshotEvidence(
        price_snapshot_id=evidence_snapshot_id,
        model_id=_spool_optional_text(evidence_value["model_id"], maximum=128),
        provider_id=_spool_optional_text(evidence_value["provider_id"], maximum=128),
        provider_code=_spool_optional_text(evidence_value["provider_code"], maximum=64),
        currency=currency,
        input_price_per_1k=format(input_price, "f"),
        output_price_per_1k=format(output_price, "f"),
        price_schema_version=version,
        source=source,
        price_plan_id=price_plan_id,
        effective_from=effective_from,
    )
    return CostEstimate(
        amount=amount,
        currency=currency,
        price_schema_version=version,
        cost_cny=cost_cny,
        cost_usd=cost_usd,
        price_snapshot_id=snapshot_id,
        price_snapshot_evidence=evidence,
    )


def _spool_mapping(value: object, expected_fields: frozenset[str]) -> dict[str, Any]:
    """要求 JSON 对象字段与冻结 schema 完全相等，禁止缺失、扩展和 bool 冒充映射。"""

    # 字段集合必须完全相等，避免新增字段在旧进程中被忽略后改变重放语义。
    if type(value) is not dict or set(value) != expected_fields:
        raise ValueError("finops_spool_payload_invalid")
    return value


def _spool_required_text(value: object, *, maximum: int) -> str:
    """校验非空、无控制字符的有界文本，错误正文不回显原始值。"""

    # 控制字符和超长文本会污染日志、索引或签名边界，因此统一按损坏记录拒绝。
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        # 文本边界失败时使用固定错误，绝不把原字符串拼入异常正文。
        raise ValueError("finops_spool_payload_invalid")
    return value


def _spool_optional_text(value: object, *, maximum: int) -> str | None:
    """校验可空有界文本；空字符串不能替代 NULL。"""

    if value is None:
        return None
    return _spool_required_text(value, maximum=maximum)


def _spool_prefixed_id(value: object, *, prefix: str) -> str:
    """校验内部对象 ID 的固定前缀和 32 位小写十六进制随机段。"""

    identifier = _spool_required_text(value, maximum=len(prefix) + 32)
    suffix = identifier[len(prefix) :] if identifier.startswith(prefix) else ""
    # 内部身份只接受固定前缀和小写十六进制随机段，防止跨对象类型替换。
    if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
        raise ValueError("finops_spool_payload_invalid")
    return identifier


def _spool_nonnegative_int(value: object) -> int:
    """只接受 JSON integer 的非负安全范围，拒绝 bool、float 和数字字符串。"""

    # bool 在 Python 中是 int 子类，必须用精确类型判断避免签名后产生两种解释。
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise ValueError("finops_spool_payload_invalid")
    return value


def _spool_optional_nonnegative_int(value: object) -> int | None:
    """校验可空非负整数；非空值仍执行精确 JSON integer 与安全范围约束。"""

    return None if value is None else _spool_nonnegative_int(value)


def _spool_bool(value: object) -> bool:
    """只接受 JSON boolean，避免 0/1 与布尔字段在签名后产生解释差异。"""

    # 精确布尔类型保证 canonical JSON 与 Python 重放不会把 0/1 当成状态值。
    if type(value) is not bool:
        raise ValueError("finops_spool_payload_invalid")
    return value


def _spool_db_time(value: object) -> str:
    """要求秒级 UTC Z 时间与数据库 canonical 格式完全一致。"""

    text = _spool_required_text(value, maximum=32)
    # 往返格式必须完全相等，拒绝时区别名和高精度时间造成幂等事实漂移。
    if _to_db_time(_parse_db_time(text)) != text:
        raise ValueError("finops_spool_payload_invalid")
    return text


def _spool_money_text(value: object) -> Decimal:
    """解析正式账本六位小数金额，禁止指数、Unicode 数字、负数和超上限。"""

    text = _spool_required_text(value, maximum=17)
    integer, separator, fraction = text.partition(".")
    # 正式金额固定六位 ASCII 小数，禁止 Unicode 数字、前导零和科学计数法。
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
        # 非 canonical 金额会导致数据库、事件和签名解释不同，必须拒绝。
        raise ValueError("finops_spool_payload_invalid")
    amount = Decimal(text)
    # 单笔金额硬上限与正式账本一致，防止异常记录制造溢出或失真投影。
    if amount > Decimal("1000000000"):
        raise ValueError("finops_spool_payload_invalid")
    return amount


def _spool_price_text(value: object) -> Decimal:
    """解析价格快照单价，复用 Gateway 价格上限并拒绝非 canonical 十进制。"""

    text = _spool_required_text(value, maximum=64)
    amount = _decimal_value(text)
    # 单价必须是 canonical 普通十进制，解析后格式变化说明原记录不稳定。
    if amount is None or format(amount, "f") != text:
        raise ValueError("finops_spool_payload_invalid")
    return amount


def _spool_optional_decimal_text(value: object) -> str | None:
    """校验可空 Provider/预算十进制文本，不允许指数、负值或过长精度。"""

    if value is None:
        return None
    text = _spool_required_text(value, maximum=64)
    # Decimal 解析异常只转为固定内部码，绝不回显可能包含敏感内容的原文本。
    try:
        amount = Decimal(text)
    except DecimalException as exc:
        # Decimal 解析失败只保留固定错误码，底层异常正文不得穿透恢复边界。
        raise ValueError("finops_spool_payload_invalid") from exc
    places = max(-amount.as_tuple().exponent, 0)
    # 非有限、负值、超上限、超精度或非 canonical 表示都不能进入重放事务。
    if (
        not amount.is_finite()
        or amount < 0
        or amount > _MAX_SPOOL_DECIMAL
        or places > 12
        or format(amount, "f") != text
    ):
        # 违反有限值、范围、精度或稳定格式任一条件都拒绝整条记录。
        raise ValueError("finops_spool_payload_invalid")
    return text


def _normalized_usage_payload(
    intent: UsageLedgerIntent,
    estimate: CostEstimate,
    *,
    posting_source: str = "direct",
) -> dict[str, Any]:
    """生成数据库 trigger 使用的稳定十进制 Usage JSON，并显式冻结入账来源。"""

    return {
        "input_tokens": intent.input_tokens,
        "output_tokens": intent.output_tokens,
        "total_tokens": intent.total_tokens,
        "cache_tokens": intent.cache_tokens,
        "cost_amount": format(estimate.amount, "f"),
        "cost_cny": format(estimate.cost_cny, "f") if estimate.currency == "CNY" else None,
        "cost_usd": format(estimate.cost_usd, "f"),
        "currency": estimate.currency,
        "price_schema_version": estimate.price_schema_version,
        "price_snapshot_id": estimate.price_snapshot_id,
        "provider_cost_amount": intent.provider_cost_amount,
        "provider_cost_currency": intent.provider_cost_currency,
        "provider_cost_cny": intent.provider_cost_cny,
        "normalization_warnings": intent.normalization_warnings,
        "estimated": intent.estimated,
        "usage_source": intent.usage_source,
        "estimate_reason": intent.estimate_reason,
        "posting_source": posting_source,
    }


def _load_finops_committed(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    usage_ledger_id: str,
    created: bool,
) -> FinOpsLedgerCommitted | None:
    """按 tenant+Usage 唯一来源读取正式分录，禁止依赖全局 sequence 或来源 ID 格式。"""

    row = connection.execute(
        """
        SELECT finops_ledger_entry_id, event_id, tenant_id, entry_type, direction,
               amount_micros, currency, normalized_cny_micros, cny_normalization_status,
               quota_type, source_request_id, source_trace_id, operation_trace_id,
               price_snapshot_id, price_schema_version, price_snapshot_source,
               price_evidence_status, reversal_of_entry_id, actor_user_id, created_at
        FROM finops_ledger_entries
        WHERE tenant_id=? AND usage_ledger_id=?
        """,
        (tenant_id, usage_ledger_id),
    ).fetchone()
    if row is None:
        return None
    normalized_micros = row["normalized_cny_micros"]
    return FinOpsLedgerCommitted(
        created=created,
        ledger_entry_id=str(row["finops_ledger_entry_id"]),
        event_id=str(row["event_id"]),
        tenant_id=str(row["tenant_id"]),
        entry_type=str(row["entry_type"]),
        direction=str(row["direction"]),
        amount=_format_micros(int(row["amount_micros"])),
        currency=str(row["currency"]),
        normalized_cny=(
            _format_micros(int(normalized_micros)) if normalized_micros is not None else None
        ),
        cny_normalization_status=str(row["cny_normalization_status"]),
        quota_type=str(row["quota_type"]),
        source_request_id=row["source_request_id"],
        source_trace_id=row["source_trace_id"],
        operation_trace_id=str(row["operation_trace_id"]),
        price_snapshot_id=row["price_snapshot_id"],
        price_schema_version=row["price_schema_version"],
        price_snapshot_source=row["price_snapshot_source"],
        price_evidence_status=str(row["price_evidence_status"]),
        reversal_of_entry_id=row["reversal_of_entry_id"],
        actor_user_id=row["actor_user_id"],
        created_at=str(row["created_at"]),
    )


def _validate_usage_projection(
    committed: FinOpsLedgerCommitted,
    *,
    context: RequestLogContext,
    intent: UsageLedgerIntent,
    estimate: CostEstimate,
) -> None:
    """逐字段核对 trigger 投影与来源 Usage/PriceSnapshot，失败时让整个事务回滚。"""

    evidence = estimate.price_snapshot_evidence
    expected_status = "unpriced" if evidence and evidence.source == "finops_unpriced" else "priced"
    expected_cny = format(estimate.amount, ".6f") if estimate.currency == "CNY" else None
    # trigger 产物必须逐字段匹配当前租户、请求、trace、金额和快照，不一致即回滚整个事务。
    if (
        committed.entry_type != "usage_charge"
        or committed.direction != "debit"
        or committed.tenant_id != context.tenant_id
        or committed.source_request_id != context.request_id
        or committed.source_trace_id != context.trace_id
        or committed.operation_trace_id != context.trace_id
        or committed.amount != format(estimate.amount, ".6f")
        or committed.currency != estimate.currency
        or committed.normalized_cny != expected_cny
        or committed.price_snapshot_id != estimate.price_snapshot_id
        or committed.price_schema_version != estimate.price_schema_version
        or committed.price_snapshot_source != (evidence.source if evidence else None)
        or committed.price_evidence_status != expected_status
        or committed.reversal_of_entry_id is not None
        or committed.actor_user_id is not None
        or intent.usage_ledger_id == ""
    ):
        # trigger 投影与来源事实不一致时回滚，不能提交部分正确的财务分录。
        raise sqlite3.IntegrityError("finops_usage_projection_mismatch")




def _ensure_replay_request_log(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    observed_at: str,
) -> None:
    """确认或恢复 spool 所属 Request Log，并绑定原 claim 的 tenant/trace/Key/快照。

    参数：当前重放事务、严格反序列化上下文和原价格观测时刻。返回：成功时无返回值。
    约束：不得跨租户查找或覆盖已有请求；缺失不可变 claim 时必须失败关闭。
    """

    if context.policy_mask_applied and context.policy_mask_policy_id is None:
        # 缺少来源的 applied 事实不可恢复，避免依赖数据库 CHECK 的实现文本分类故障。
        raise sqlite3.IntegrityError("finops_spool_policy_mask_identity_missing")
    existing = connection.execute(
        """
        SELECT trace_id, virtual_key_id, snapshot_version, policy_snapshot_status,
               policy_mask_policy_id, policy_mask_applied,
               intent_risk_result_id,intent_risk_schedule_status,intent_risk_mode,
               intent_risk_type,intent_risk_score,intent_risk_action,
               intent_risk_reason_code,intent_risk_detector_version,
               intent_risk_input_truncated,session_risk_status,
               session_risk_score_id,session_risk_scope_type,session_risk_state,
               session_risk_reason_code
        FROM request_logs
        WHERE tenant_id=? AND request_id=?
        """,
        (context.tenant_id, context.request_id),
    ).fetchone()
    if existing is not None:
        # 同 tenant/request 的既有日志仍要匹配 trace、Key 与策略快照，避免复用碰撞身份。
        if (
            existing["trace_id"] != context.trace_id
            or existing["virtual_key_id"] != context.virtual_key_id
            or existing["snapshot_version"] != context.snapshot_version
            or existing["policy_snapshot_status"] != context.policy_snapshot_status
        ):
            # 既有请求 owner 不匹配说明 tenant/request 发生碰撞，禁止覆盖或复用。
            raise sqlite3.IntegrityError("finops_spool_request_owner_mismatch")
        if (
            existing["policy_mask_policy_id"] is not None
            and context.policy_mask_policy_id is not None
            and existing["policy_mask_policy_id"] != context.policy_mask_policy_id
        ):
            # HMAC 正确也不能让同一请求的两条 spool 证据绑定到不同 mask Policy。
            raise sqlite3.IntegrityError("finops_spool_policy_mask_conflict")
        if context.policy_mask_applied and context.policy_mask_policy_id is None:
            raise sqlite3.IntegrityError("finops_spool_policy_mask_identity_missing")
        _validate_request_log_intent_merge(existing, context)
        _validate_request_log_session_risk_merge(existing, context)
        # 已存在 RequestLog 时只允许单调补齐：mask、intent 与会话风险冻结事实
        # 永不被旧 spool 默认值覆盖。
        connection.execute(
            """
            UPDATE request_logs
            SET policy_mask_policy_id=COALESCE(policy_mask_policy_id, ?),
                policy_mask_applied=CASE
                  WHEN policy_mask_applied=1 OR ?=1 THEN 1 ELSE 0
                END,
                intent_risk_result_id=CASE
                  WHEN intent_risk_schedule_status='disabled' THEN ?
                  ELSE intent_risk_result_id END,
                intent_risk_schedule_status=CASE
                  WHEN intent_risk_schedule_status='disabled' THEN ?
                  ELSE intent_risk_schedule_status END,
                intent_risk_mode=CASE
                  WHEN intent_risk_schedule_status='disabled' THEN ?
                  ELSE intent_risk_mode END,
                intent_risk_type=CASE
                  WHEN intent_risk_schedule_status='disabled' THEN ?
                  ELSE intent_risk_type END,
                intent_risk_score=CASE
                  WHEN intent_risk_schedule_status='disabled' THEN ?
                  ELSE intent_risk_score END,
                intent_risk_action=CASE
                  WHEN intent_risk_schedule_status='disabled' THEN ?
                  ELSE intent_risk_action END,
                intent_risk_reason_code=CASE
                  WHEN intent_risk_schedule_status='disabled' THEN ?
                  ELSE intent_risk_reason_code END,
                intent_risk_detector_version=CASE
                  WHEN intent_risk_schedule_status='disabled' THEN ?
                  ELSE intent_risk_detector_version END,
                intent_risk_input_truncated=CASE
                  WHEN intent_risk_schedule_status='disabled' THEN ?
                  ELSE intent_risk_input_truncated END,
                session_risk_status=CASE
                  WHEN session_risk_status='not_applicable' THEN ?
                  ELSE session_risk_status END,
                session_risk_score_id=CASE
                  WHEN session_risk_status='not_applicable' THEN ?
                  ELSE session_risk_score_id END,
                session_risk_scope_type=CASE
                  WHEN session_risk_status='not_applicable' THEN ?
                  ELSE session_risk_scope_type END,
                session_risk_state=CASE
                  WHEN session_risk_status='not_applicable' THEN ?
                  ELSE session_risk_state END,
                session_risk_reason_code=CASE
                  WHEN session_risk_status='not_applicable' THEN ?
                  ELSE session_risk_reason_code END
            WHERE tenant_id=? AND request_id=?
            """,
            (
                context.policy_mask_policy_id,
                1 if context.policy_mask_applied else 0,
                context.intent_risk_result_id,
                context.intent_risk_schedule_status,
                context.intent_risk_mode,
                context.intent_risk_type,
                context.intent_risk_score,
                context.intent_risk_action,
                context.intent_risk_reason_code,
                context.intent_risk_detector_version,
                1 if context.intent_risk_input_truncated else 0,
                context.session_risk_status,
                context.session_risk_score_id,
                context.session_risk_scope_type,
                context.session_risk_state,
                context.session_risk_reason_code,
                context.tenant_id,
                context.request_id,
            ),
        )
        return
    claim = connection.execute(
        """
        SELECT trace_id, virtual_key_id, snapshot_version, policy_snapshot_status
        FROM gateway_request_claims
        WHERE tenant_id=? AND request_id=?
        """,
        (context.tenant_id, context.request_id),
    ).fetchone()
    if claim is None or (
        claim["trace_id"] != context.trace_id
        or claim["virtual_key_id"] != context.virtual_key_id
        or claim["snapshot_version"] != context.snapshot_version
        or claim["policy_snapshot_status"] != context.policy_snapshot_status
    ):
        # Request Log 的 insert trigger 也会复核，但这里给恢复状态机一个稳定内部故障码。
        raise sqlite3.IntegrityError("finops_spool_request_claim_mismatch")
    is_experiment_request = _has_complete_experiment_assignment(connection, context)
    values = {
        "request_id": context.request_id,
        "tenant_id": context.tenant_id,
        "trace_id": context.trace_id,
        "virtual_key_id": context.virtual_key_id,
        "environment_id": context.environment_id,
        "environment_code": context.environment_code,
        "project_id": context.project_id,
        "application_id": context.application_id,
        "provider_id": context.provider_id,
        "model_id": context.model_id,
        # spool 明确不保存 Endpoint；恢复行使用 NULL，不能猜测或回填外部地址。
        "endpoint": None,
        "request_model": context.request_model,
        "resolved_model": context.resolved_model,
        "provider_code": context.provider_code,
        "snapshot_version": context.snapshot_version,
        "policy_snapshot_status": context.policy_snapshot_status,
        "policy_decision": context.policy_decision,
        "policy_reason_code": context.policy_reason_code,
        "policy_id": context.policy_id,
        "policy_action_type": context.policy_action_type,
        "policy_target_model_id": context.policy_target_model_id,
        "policy_mask_policy_id": context.policy_mask_policy_id,
        "policy_mask_applied": 1 if context.policy_mask_applied else 0,
        "budget_status": context.budget_status,
        "budget_rule_id": context.budget_rule_id,
        "budget_scope_type": context.budget_scope_type,
        "budget_scope_id": context.budget_scope_id,
        "budget_action": context.budget_action,
        "budget_reason_code": context.budget_reason_code,
        "budget_current_cost_cny": context.budget_current_cost_cny,
        "budget_limit_cny": context.budget_limit_cny,
        "dlp_status": context.dlp_status,
        "dlp_rule_id": context.dlp_rule_id,
        "dlp_action": context.dlp_action,
        "dlp_match_count": context.dlp_match_count,
        "dlp_reason_code": context.dlp_reason_code,
        "intent_risk_result_id": context.intent_risk_result_id,
        "intent_risk_schedule_status": context.intent_risk_schedule_status,
        "intent_risk_mode": context.intent_risk_mode,
        "intent_risk_type": context.intent_risk_type,
        "intent_risk_score": context.intent_risk_score,
        "intent_risk_action": context.intent_risk_action,
        "intent_risk_reason_code": context.intent_risk_reason_code,
        "intent_risk_detector_version": context.intent_risk_detector_version,
        "intent_risk_input_truncated": 1 if context.intent_risk_input_truncated else 0,
        "session_risk_status": context.session_risk_status,
        "session_risk_score_id": context.session_risk_score_id,
        "session_risk_scope_type": context.session_risk_scope_type,
        "session_risk_state": context.session_risk_state,
        "session_risk_reason_code": context.session_risk_reason_code,
        "fallback_triggered": 1 if context.fallback_triggered else 0,
        "routing_attempt_count": context.routing_attempt_count,
        "final_routing_attempt_id": context.final_routing_attempt_id,
        "stream_session_id": context.stream_session_id,
        "stream_status": context.stream_status,
        # 实验执行缺失 RequestLog 表示 Gateway 终态未完成；spool 只能恢复成本，不能伪造成功。
        "request_status": "failed" if is_experiment_request else "success",
        "request_error_code": (
            "gateway.experiment_unavailable" if is_experiment_request else None
        ),
        "observed_at": observed_at,
    }
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, environment_id, environment_code,
          project_id, application_id, provider_id, model_id, endpoint, request_model,
          resolved_model, provider_code, snapshot_version, policy_snapshot_status,
          policy_decision, policy_reason_code, policy_id, policy_action_type,
          policy_target_model_id, policy_mask_policy_id, policy_mask_applied,
          status, error_code, latency_ms,
          budget_status, budget_rule_id, budget_scope_type, budget_scope_id,
          budget_action, budget_reason_code, budget_current_cost_cny, budget_limit_cny,
          dlp_status, dlp_rule_id, dlp_action, dlp_match_count, dlp_reason_code,
          intent_risk_result_id, intent_risk_schedule_status, intent_risk_mode,
          intent_risk_type, intent_risk_score, intent_risk_action,
          intent_risk_reason_code, intent_risk_detector_version, intent_risk_input_truncated,
          session_risk_status, session_risk_score_id, session_risk_scope_type,
          session_risk_state, session_risk_reason_code,
          fallback_triggered, routing_attempt_count, final_routing_attempt_id,
          stream_session_id, stream_status, created_at, updated_at
        )
        VALUES(
          :request_id, :tenant_id, :trace_id, :virtual_key_id, :environment_id,
          :environment_code, :project_id, :application_id, :provider_id, :model_id,
          :endpoint, :request_model, :resolved_model, :provider_code, :snapshot_version,
          :policy_snapshot_status, :policy_decision, :policy_reason_code, :policy_id,
          :policy_action_type, :policy_target_model_id,
          :policy_mask_policy_id, :policy_mask_applied,
          :request_status, :request_error_code, NULL,
          :budget_status, :budget_rule_id, :budget_scope_type, :budget_scope_id,
          :budget_action, :budget_reason_code, :budget_current_cost_cny, :budget_limit_cny,
          :dlp_status, :dlp_rule_id, :dlp_action, :dlp_match_count, :dlp_reason_code,
          :intent_risk_result_id, :intent_risk_schedule_status, :intent_risk_mode,
          :intent_risk_type, :intent_risk_score, :intent_risk_action,
          :intent_risk_reason_code, :intent_risk_detector_version, :intent_risk_input_truncated,
          :session_risk_status, :session_risk_score_id, :session_risk_scope_type,
          :session_risk_state, :session_risk_reason_code,
          :fallback_triggered, :routing_attempt_count, :final_routing_attempt_id,
          :stream_session_id, :stream_status, :observed_at, :observed_at
        )
        """,
        values,
    )


def _has_complete_experiment_assignment(
    connection: sqlite3.Connection,
    context: RequestLogContext,
) -> bool:
    """识别 tenant-qualified 完整实验 owner；半事实或身份漂移必须失败关闭。"""

    assigned = connection.execute(
        """
        SELECT 1 FROM experiment_assignments
        WHERE tenant_id=? AND request_id=?
        """,
        (context.tenant_id, context.request_id),
    ).fetchone()
    if assigned is None:
        return False
    complete = connection.execute(
        """
        SELECT 1
        FROM experiment_assignments assignment
        JOIN gateway_request_scope_claims scope
          ON scope.tenant_id=assignment.tenant_id
         AND scope.request_id=assignment.request_id
        JOIN experiment_request_executions execution
          ON execution.tenant_id=assignment.tenant_id
         AND execution.assignment_id=assignment.assignment_id
         AND execution.execution_id=assignment.execution_id
         AND execution.experiment_id=assignment.experiment_id
         AND execution.request_id=assignment.request_id
        JOIN experiment_usage_settlements settlement
          ON settlement.tenant_id=assignment.tenant_id
         AND settlement.assignment_id=assignment.assignment_id
         AND settlement.usage_settlement_id=assignment.usage_settlement_id
         AND settlement.experiment_id=assignment.experiment_id
         AND settlement.request_id=assignment.request_id
         AND settlement.model_id=assignment.model_id
         AND settlement.provider_id=assignment.provider_id
        WHERE assignment.tenant_id=? AND assignment.request_id=?
          AND assignment.trace_id=? AND assignment.virtual_key_id=?
          AND assignment.model_id=? AND assignment.provider_id=?
          AND assignment.provider_code=?
          AND scope.trace_id=? AND scope.virtual_key_id=?
          AND scope.project_id IS ? AND scope.application_id IS ?
          AND scope.environment_id IS ? AND scope.environment_code IS ?
        """,
        (
            context.tenant_id,
            context.request_id,
            context.trace_id,
            context.virtual_key_id,
            context.model_id,
            context.provider_id,
            context.provider_code,
            context.trace_id,
            context.virtual_key_id,
            context.project_id,
            context.application_id,
            context.environment_id,
            context.environment_code,
        ),
    ).fetchone()
    if complete is None:
        # HMAC 正确也不能把残缺 Assignment 或另一 owner 的 spool 当作普通请求恢复成功。
        raise sqlite3.IntegrityError("finops_spool_experiment_owner_mismatch")
    return True


def _restore_captured_estimate(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    estimate: CostEstimate,
    observed_at: str,
    *,
    usage_ledger_id: str,
) -> CostEstimate:
    """按原 ID 重建回滚掉的 PriceSnapshot，禁止重放时重新选价或更换证据来源。"""

    evidence = estimate.price_snapshot_evidence
    # spool Estimate 与证据及当前路由 owner 必须一致，防止跨模型或 Provider 恢复。
    if (
        evidence is None
        or estimate.price_snapshot_id != evidence.price_snapshot_id
        or estimate.price_schema_version != evidence.price_schema_version
        or estimate.currency != evidence.currency
        or (evidence.model_id is not None and evidence.model_id != context.model_id)
        or (evidence.provider_id is not None and evidence.provider_id != context.provider_id)
    ):
        # 任一 owner 或证据字段漂移都可能造成跨模型计费，重放事务必须失败关闭。
        raise sqlite3.IntegrityError("finops_spool_price_evidence_mismatch")
    _insert_price_snapshot_evidence(
        connection,
        context,
        evidence,
        now=observed_at,
        usage_ledger_id=usage_ledger_id,
    )
    return estimate


def _unpriced_cost(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    *,
    now: str,
    usage_ledger_id: str,
    capture_intent: UsageLedgerIntent | None = None,
) -> CostEstimate:
    """创建明确表示“当时无价格”的零价快照，而不是把未知成本伪装成免费计划。"""

    evidence = PriceSnapshotEvidence(
        price_snapshot_id=f"prs_{uuid4().hex}",
        model_id=context.model_id,
        provider_id=context.provider_id,
        provider_code=context.provider_code,
        currency="CNY",
        input_price_per_1k="0",
        output_price_per_1k="0",
        price_schema_version="unpriced-v1",
        source="finops_unpriced",
        price_plan_id=None,
        effective_from=now,
    )
    estimate = CostEstimate(
        amount=Decimal("0"),
        currency="CNY",
        price_schema_version="unpriced-v1",
        cost_cny=Decimal("0"),
        cost_usd=Decimal("0"),
        price_snapshot_id=evidence.price_snapshot_id,
        price_snapshot_evidence=evidence,
    )
    # 先捕获证据再执行 INSERT；即使快照写入本身失败，fsync spool 仍可按原事实重放。
    if capture_intent is not None:
        capture_intent.captured_estimate = estimate
    _insert_price_snapshot_evidence(
        connection,
        context,
        evidence,
        now=now,
        usage_ledger_id=usage_ledger_id,
    )
    return estimate


def _insert_price_snapshot_evidence(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    evidence: PriceSnapshotEvidence,
    *,
    now: str,
    usage_ledger_id: str | None,
) -> None:
    """插入严格 PriceSnapshot 证据；特殊缺价来源必须绑定同一 Usage ID。"""

    # 缺价证据需要绑定稳定 Usage 身份，防止多个零价快照被误认为同一事实。
    if evidence.source == "finops_unpriced":
        if usage_ledger_id is None:
            raise sqlite3.IntegrityError("finops_unpriced_usage_identity_missing")
        # Usage ID 仍按 spool 身份合同复核，不能只检查非空字符串。
        try:
            _spool_prefixed_id(usage_ledger_id, prefix="usg_")
        except ValueError as exc:
            # 非法 Usage 身份转换为事务完整性错误，确保快照和分录一起回滚。
            raise sqlite3.IntegrityError("finops_unpriced_usage_identity_invalid") from exc
        billing_dimensions = {
            "finops_usage_ledger_id": usage_ledger_id,
            "price_evidence_status": "unpriced",
        }
    else:
        billing_dimensions = {
            "unit": "per_1k_tokens",
            "input": True,
            "output": True,
        }
    # Provider 原始映射只保留公开代码；Endpoint、Credential 和任何密钥都不进入快照。
    provider_mapping = (
        {"provider_code": evidence.provider_code}
        if evidence.provider_code is not None
        else {}
    )
    connection.execute(
        """
        INSERT INTO price_snapshots(
          price_snapshot_id, tenant_id, request_id, model_id, provider_id, provider_code,
          currency, input_price_per_1k, output_price_per_1k, price_schema_version,
          billing_dimensions, provider_raw_mapping, source, status, created_at, updated_at,
          price_plan_id, effective_from
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
        """,
        (
            evidence.price_snapshot_id,
            context.tenant_id,
            context.request_id,
            evidence.model_id,
            evidence.provider_id,
            evidence.provider_code,
            evidence.currency,
            evidence.input_price_per_1k,
            evidence.output_price_per_1k,
            evidence.price_schema_version,
            json.dumps(billing_dimensions, ensure_ascii=False, sort_keys=True),
            json.dumps(provider_mapping, ensure_ascii=False, sort_keys=True),
            evidence.source,
            now,
            now,
            evidence.price_plan_id,
            evidence.effective_from,
        ),
    )


def safe_record_request_log(
    config: AppConfig,
    context: RequestLogContext,
    *,
    status: str,
    error_code: str | None,
    latency_ms: int | None,
) -> None:
    """容错写入 request_logs，避免日志库故障反向影响网关响应。

    PRD-016 要求异步写入失败可观测；当前 M0 仍是同步 SQLite 写入，所以这里
    以安全降级方式记录结构化 warning，后续接入队列时可复用同一事件语义。
    """

    try:
        record_request_log(
            config,
            context,
            status=status,
            error_code=error_code,
            latency_ms=latency_ms,
        )
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except (DbError, sqlite3.Error) as exc:
        logger.warning(
            "request_log.write_failed",
            extra={
                "event": "request_log.write_failed",
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "tenant_id": context.tenant_id,
                "error_code": "request_log.write_failed",
                "payload": {"status": status, "exception_type": exc.__class__.__name__},
            },
        )


def safe_record_usage_ledger(
    config: AppConfig,
    context: RequestLogContext,
    usage: object,
    *,
    require_durable: bool = False,
    return_durability: bool = False,
) -> FinOpsLedgerCommitted | UsageLedgerWriteResult | None:
    """优先提交主库，失败时同步 fsync 安全 spool，并可返回明确 durability。

    参数：非流式调用必须传 ``require_durable=True``；PRD-071 调用传 ``return_durability=True``。
    返回：兼容模式沿用正式分录/None；显式模式返回 committed/spooled/lost 三态。
    约束：主库和 spool 同时失败时，非流式必须抛统一 503；流式只能记 critical，不能伪造 SSE。
    """

    # 仅接受真实 bool，避免调用方用 0/1 或字符串意外改变非流式持久化合同。
    if type(require_durable) is not bool:
        raise ValueError("finops_usage_durability_contract_invalid")
    # 返回模式同样必须由显式布尔值决定，防止字符串真值改变调用方错误优先级。
    if type(return_durability) is not bool:
        raise ValueError("finops_usage_durability_contract_invalid")
    intent = _prepare_usage_ledger_intent(usage)
    try:
        # 主库事务成功才返回正式分录；任何受控存储故障统一进入下方 spool 回退。
        committed = record_usage_ledger(config, context, usage, intent=intent)
        return (
            UsageLedgerWriteResult(durability="committed", committed=committed)
            if return_durability
            else committed
        )
    # 仅存储/Decimal 合同故障进入 spool；编程错误不能被伪装成可恢复 Usage。
    except (DbError, sqlite3.Error, DecimalException, OverflowError) as database_exc:
        try:
            from metahub_gateway.usage.spool import store_usage_spool_record

            store_usage_spool_record(config, _serialize_usage_spool_record(context, intent))
        except Exception as spool_exc:
            logger.critical(
                "usage_ledger.durability_lost",
                extra={
                    "event": "usage_ledger.durability_lost",
                    "request_id": context.request_id,
                    "trace_id": context.trace_id,
                    "tenant_id": context.tenant_id,
                    "error_code": "finops.usage_record_unavailable",
                    "payload": {
                        "database_exception_type": database_exc.__class__.__name__,
                        "spool_exception_type": spool_exc.__class__.__name__,
                    },
                },
            )
            if require_durable:
                # 不能串联底层 cause；其正文可能含 SQLite 路径或文件系统目录。
                raise ApiError("finops.usage_record_unavailable") from None
            return (
                UsageLedgerWriteResult(durability="lost", committed=None)
                if return_durability
                else None
            )
        logger.warning(
            "usage_ledger.write_failed",
            extra={
                "event": "usage_ledger.write_failed",
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "tenant_id": context.tenant_id,
                "error_code": "usage_ledger.write_failed",
                "payload": {"exception_type": database_exc.__class__.__name__, "spooled": True},
            },
        )
        return (
            UsageLedgerWriteResult(durability="spooled", committed=None)
            if return_durability
            else None
        )


def _estimate_cost(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    *,
    input_tokens: int,
    output_tokens: int,
    now: str,
    usage_ledger_id: str | None = None,
    capture_intent: UsageLedgerIntent | None = None,
) -> CostEstimate:
    """按服务器入账时刻选择有效价格计划并计算本次调用成本。

    查询始终使用 tenant_id + model_id；存在已生效 PricePlan 时优先使用，否则回退模型目录兼容价格。
    客户端不能指定价格计划或时间，因此无法通过请求体绕过价格生效边界。
    """

    # 正常路径传入预生成 Usage ID；直接单测估价时也生成合法 ID，确保缺价快照满足 0041 owner 合同。
    snapshot_usage_id = usage_ledger_id or f"usg_{uuid4().hex}"
    # 模型路由结果决定上游调用，异常时必须用统一错误码停止。
    if not context.model_id:
        return _unpriced_cost(
            connection,
            context,
            now=now,
            usage_ledger_id=snapshot_usage_id,
            capture_intent=capture_intent,
        )
    row = connection.execute(
        """
        SELECT
          CASE WHEN pp.price_plan_id IS NOT NULL
            THEN pp.input_price_per_1k ELSE m.input_price_per_1k END AS input_price_per_1k,
          CASE WHEN pp.price_plan_id IS NOT NULL
            THEN pp.output_price_per_1k ELSE m.output_price_per_1k END AS output_price_per_1k,
          CASE WHEN pp.price_plan_id IS NOT NULL
            THEN pp.currency ELSE m.currency END AS currency,
          CASE WHEN pp.price_plan_id IS NOT NULL
            THEN pp.price_schema_version ELSE m.price_schema_version END AS price_schema_version,
          pp.price_plan_id, pp.effective_from,
          m.provider_id, p.provider_code
        FROM models m
        LEFT JOIN providers p ON p.provider_id=m.provider_id AND p.tenant_id=m.tenant_id
        LEFT JOIN model_price_plans pp ON pp.price_plan_id=(
          SELECT selected.price_plan_id
          FROM model_price_plans selected
          WHERE selected.tenant_id=m.tenant_id
            AND selected.model_id=m.model_id
            AND selected.effective_from<=?
          ORDER BY selected.effective_from DESC, selected.created_at DESC
          LIMIT 1
        )
        WHERE m.tenant_id=? AND m.model_id=?
        """,
        (now, context.tenant_id, context.model_id),
    ).fetchone()
    if row is None:
        return _unpriced_cost(
            connection,
            context,
            now=now,
            usage_ledger_id=snapshot_usage_id,
            capture_intent=capture_intent,
        )

    price_schema_version = str(row["price_schema_version"] or "").strip()
    if not price_schema_version:
        # models 的历史默认单价是 0；没有版本时它只表示“尚未配置价格”，不能创建
        # source=model_catalog/status=active 的零价快照，否则下游会把未知成本误认为免费。
        return _unpriced_cost(
            connection,
            context,
            now=now,
            usage_ledger_id=snapshot_usage_id,
            capture_intent=capture_intent,
        )
    input_price = _decimal_value(row["input_price_per_1k"])
    output_price = _decimal_value(row["output_price_per_1k"])
    currency = _normalize_currency(row["currency"])
    # 版本号不能替损坏的金额或币种背书；否则非法值会被伪装成可审计的 active 零价快照。
    if input_price is None or output_price is None or currency is None:
        _log_price_configuration_invalid(
            context,
            price_source="price_plan" if row["price_plan_id"] else "model_catalog",
        )
        return _unpriced_cost(
            connection,
            context,
            now=now,
            usage_ledger_id=snapshot_usage_id,
            capture_intent=capture_intent,
        )
    try:
        # 与成本路由共用 50 位局部精度，既避免默认 28 位上下文提前舍入，也把异常
        # token/价格组合限制在本次计算内；任何 Decimal 故障都只能降级账本，不能覆盖
        # 已经成功取得的 Provider 响应。
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            amount = (
                Decimal(input_tokens) * input_price
                + Decimal(output_tokens) * output_price
            ) / Decimal(1000)
            money_amount = _money_amount(amount)
    # 极端乘积或量化失败沿用损坏价格降级，避免 Provider 已成功后向客户端返回 500。
    except (DecimalException, OverflowError):
        _log_price_configuration_invalid(
            context,
            price_source="price_plan" if row["price_plan_id"] else "model_catalog",
        )
        return _unpriced_cost(
            connection,
            context,
            now=now,
            usage_ledger_id=snapshot_usage_id,
            capture_intent=capture_intent,
        )
    cost_cny, cost_usd = _cost_amounts(money_amount, currency)
    evidence = _build_price_snapshot_evidence(
        context, row=row, input_price=input_price, output_price=output_price, currency=currency
    )
    estimate = CostEstimate(
        amount=money_amount,
        currency=currency,
        price_schema_version=price_schema_version,
        cost_cny=cost_cny,
        cost_usd=cost_usd,
        price_snapshot_id=evidence.price_snapshot_id,
        price_snapshot_evidence=evidence,
    )
    # 证据捕获必须早于 PriceSnapshot INSERT，避免瞬时数据库故障让重放改用现价或缺价。
    if capture_intent is not None:
        capture_intent.captured_estimate = estimate
    _insert_price_snapshot_evidence(
        connection,
        context,
        evidence,
        now=now,
        usage_ledger_id=None,
    )
    return estimate


def _build_price_snapshot_evidence(
    context: RequestLogContext,
    *,
    row: sqlite3.Row,
    input_price: Decimal,
    output_price: Decimal,
    currency: str,
) -> PriceSnapshotEvidence:
    """构造本次成本估算使用的不可变价格证据，不执行任何数据库写入。

    先构造纯数据再写库，使瞬时故障时 spool 仍能保存原单价；证据不含 Provider Key 或请求正文。
    """

    return PriceSnapshotEvidence(
        price_snapshot_id=f"prs_{uuid4().hex}",
        model_id=context.model_id,
        provider_id=context.provider_id or row["provider_id"],
        provider_code=context.provider_code or row["provider_code"],
        currency=currency,
        input_price_per_1k=format(input_price, "f"),
        output_price_per_1k=format(output_price, "f"),
        price_schema_version=str(row["price_schema_version"] or ""),
        source="price_plan" if row["price_plan_id"] else "model_catalog",
        price_plan_id=row["price_plan_id"],
        effective_from=row["effective_from"],
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接当前服务配置指定的数据库。"""

    return connect_database(config.database_url)


def _int_usage(value: object) -> int:
    """把上游 usage 字段安全转换成非负整数。"""

    try:
        parsed = int(value or 0)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _decimal_value(value: object) -> Decimal | None:
    """按共享上限严格解析版本价格，损坏值返回 ``None`` 交由调用方安全降级。

    参数：数据库中的 PricePlan 或模型目录单价。返回：满足有限、非负、不超过十亿且
    最多 12 位小数的 Decimal；否则返回 ``None``。该函数不得抛出 Decimal 解析异常，
    因为 Usage 是 Provider 成功后的审计副作用，价格损坏不能反向把成功响应改成 500。
    """

    try:
        amount = Decimal(str(value))
    # 解析失败不能静默改写为免费价格；调用方会清空价格版本并禁止创建快照。
    except (DecimalException, ValueError):
        return None
    if not amount.is_finite():
        return None
    decimal_places = max(-amount.as_tuple().exponent, 0)
    # 与成本路由复用相同金额合同，防止固定路由绕过动态路由已执行的价格校验。
    if (
        amount < 0
        or amount > MAX_PRICE_PER_1K
        or decimal_places > MAX_PRICE_DECIMAL_PLACES
    ):
        return None
    return amount


def _optional_decimal_text(value: object) -> str | None:
    """校验 Adapter 提供的可选 Provider 成本，只保留有限非负十进制文本。"""

    if value is None or str(value).strip() == "":
        return None
    try:
        amount = Decimal(str(value))
    # Provider 成本解析失败只丢弃该辅助口径，内部 PricePlan 成本仍正常入账。
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        # NaN、Infinity 和负数不能进入 normalized_usage 财务字段。
        return None
    return format(amount, "f")


def _optional_currency(value: object) -> str | None:
    """规范化 Provider 自报币种，仅保留 CNY/USD，未知币种由 warning 解释。"""

    currency = str(value or "").strip().upper()
    return currency if currency in {"CNY", "USD"} else None


def _normalization_warnings(value: object) -> list[str]:
    """过滤 Adapter 归一警告，只把稳定白名单代码写入账本。"""

    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            str(item) for item in value if str(item) in NORMALIZATION_WARNING_CODES
        )
    )


def _money_amount(value: Decimal) -> Decimal:
    """统一成本估算精度，避免浮点数写入造成不可解释的小数尾差。"""

    if value <= 0:
        return Decimal("0")
    return value.quantize(Decimal("0.000001"))


def _reservation_outstanding_text(value: object) -> str:
    """严格量化 Budget Guard 提供的内存覆盖额，拒绝伪造负数或非有限财务事实。"""

    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        with localcontext() as decimal_context:
            decimal_context.prec = 64
            normalized = amount.quantize(Decimal("0.000001"))
    # 结算覆盖额无效时必须让 Usage 与预留更新一并回滚，避免形成部分成功的财务事实。
    except (DecimalException, ValueError, OverflowError) as exc:
        # 使用固定异常码触发同事务回滚，原始字符串不会进入日志或客户端响应。
        raise sqlite3.IntegrityError("budget_reservation_settlement_invalid") from exc
    return format(normalized, "f")


def _normalize_currency(value: object) -> str | None:
    """严格限制版本价格币种为 CNY/USD，缺失或未知币种返回 None。"""

    currency = str(value or "").strip().upper()
    # 未知币种不能默认映射为 CNY，否则会把无法解释的原币成本写成可信人民币成本。
    return currency if currency in {"CNY", "USD"} else None


def _cost_amounts(amount: Decimal, currency: str) -> tuple[Decimal, Decimal]:
    """按照原币种拆分 CNY/USD 成本字段，M0 阶段不做汇率折算。"""

    # 用量成本是审计账本字段，缺失或异常必须进入可追踪分支。
    if currency == "USD":
        return Decimal("0"), amount
    return amount, Decimal("0")


def _bool_usage(value: object) -> bool:
    """把 Adapter 传入的 estimated 标记转换成布尔值。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _normalize_usage_source(value: object, estimated: bool) -> str:
    """归一化 usage 来源字段，确保只写入表约束允许的枚举值。"""

    if estimated:
        return "estimated"
    normalized = str(value or "upstream").strip().lower()
    return normalized if normalized in {"upstream", "estimated"} else "upstream"


def _zero_cost() -> CostEstimate:
    """返回无价格配置时的默认成本估算。"""

    return CostEstimate(
        amount=Decimal("0"),
        currency="CNY",
        price_schema_version=None,
        cost_cny=Decimal("0"),
        cost_usd=Decimal("0"),
        price_snapshot_id=None,
    )


def _log_price_configuration_invalid(context: RequestLogContext, *, price_source: str) -> None:
    """记录版本价格损坏告警，仅携带租户/请求标识和稳定来源，不输出原始价格或币种。"""

    logger.warning(
        "usage.price_configuration_invalid",
        extra={
            "event": "usage.price_configuration_invalid",
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "tenant_id": context.tenant_id,
            "model_id": context.model_id,
            "payload": {
                "reason": "invalid_versioned_price",
                "price_source": price_source,
            },
        },
    )


def _log_request_logged(
    context: RequestLogContext,
    *,
    status: str,
    error_code: str | None,
    latency_ms: int | None,
) -> None:
    """记录 request.logged 事件，事件内容只包含安全元数据。"""

    logger.info(
        "request.logged",
        extra={
            "event": "request.logged",
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "tenant_id": context.tenant_id,
            "object_id": context.request_id,
            "payload": {
                "endpoint": context.endpoint,
                "request_model": context.request_model,
                "resolved_model": context.resolved_model,
                "provider_code": context.provider_code,
                "status": status,
                "error_code": error_code,
                "latency_ms": latency_ms,
            },
        },
    )


def _log_usage_recorded(
    context: RequestLogContext,
    *,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cache_tokens: int,
    estimated: bool,
    cost_amount: Decimal,
    currency: str,
    price_snapshot_id: str | None,
) -> None:
    """记录 usage.recorded 事件，事件只包含成本和路由元数据，不包含请求或响应正文。"""

    logger.info(
        "usage.recorded",
        extra={
            "event": "usage.recorded",
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "tenant_id": context.tenant_id,
            "object_id": context.request_id,
            "payload": {
                "model_id": context.model_id,
                "provider_code": context.provider_code,
                "virtual_key_id": context.virtual_key_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cache_tokens": cache_tokens,
                "estimated": estimated,
                "cost_amount": str(cost_amount),
                "currency": currency,
                "price_snapshot_id": price_snapshot_id,
            },
        },
    )


def _format_micros(value: int) -> str:
    """把数据库非负整数微元无损格式化为固定六位小数，不经过 float。"""

    # 微元必须是非负有界整数，拒绝 bool、负数和异常大值污染公开账本金额。
    if type(value) is not int or value < 0 or value > 1_000_000_000_000_000:
        raise ValueError("finops_ledger_amount_invalid")
    integer, fraction = divmod(value, 1_000_000)
    return f"{integer}.{fraction:06d}"


def _parse_db_time(value: str) -> datetime:
    """解析数据库 ISO 时间为 UTC aware datetime，拒绝无时区和控制字符文本。

    该函数只处理内部持久事实；异常使用固定代码，不把数据库原值带入日志或 Webhook。
    """

    # 原始数据库时间必须有界且无控制字符，错误信息不能包含持久值本身。
    if (
        type(value) is not str
        or not value
        or len(value) > 64
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        # 结构边界不满足时直接使用固定错误，避免数据库原值泄露。
        raise ValueError("finops_ledger_time_invalid")
    # 只接受 Python 可解析的 ISO 时间，解析异常转换为固定内部错误而不串联原值。
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    # 解析器拒绝的时间统一转换为固定错误，不能把原数据库文本串联给调用方。
    except ValueError as exc:
        # 解析异常不保留原始时间文本，统一转成可预期的内部合同错误。
        raise ValueError("finops_ledger_time_invalid") from exc
    # 无时区时间无法证明账务顺序，必须拒绝而不是默认解释为本地时间。
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("finops_ledger_time_invalid")
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 UTC 时间转换成数据库统一保存的 ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，避免测试中毫秒差异造成不稳定。"""

    return datetime.now(UTC).replace(microsecond=0)
