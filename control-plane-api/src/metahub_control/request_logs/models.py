"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/request_logs/models.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from metahub_shared.runtime.error_diagnosis import ErrorDiagnosisInfo
from pydantic import BaseModel, Field

from metahub_control.response_cache.models import ResponseCacheRequestLogInfo


class RequestLogOutputSafetySummary(BaseModel):
    """调用日志列表中的输出安全最小摘要。

    列表只需要定位安全终态和暴露边界，因此明确禁止加入 DLP pattern、命中正文、
    规则详情或 Provider 信息。``decision`` 与 ``scan_status`` 必须同时保留，避免把
    unavailable 或 log-only 命中错误解释为已阻断。
    """

    output_safety_scan_id: str
    scan_mode: Literal["non_stream_full", "stream_window"]
    scan_status: Literal["pending", "not_configured", "completed", "unavailable", "not_applicable"]
    decision: Literal["none", "clean", "matched", "blocked", "unknown"]
    exposure_state: Literal["none", "partial", "complete", "unknown"]
    emitted_token_count: int
    emitted_sse_event_count: int
    emitted_bytes: int
    billing_disposition: Literal["not_required", "dispute_pending"]


class RequestLogOutputSafetyDispute(BaseModel):
    """调用日志详情中的账单复核最小投影。

    该对象只证明已提交的 append-only dispute marker，不代表已经自动冲正；正式
    金额、Usage 与规则证据仍由各自模块管理。
    """

    output_safety_scan_id: str
    review_status: Literal["dispute_pending"]
    reversal_eligible: bool


class RequestLogOutputSafetyDetail(RequestLogOutputSafetySummary):
    """调用日志详情中的输出安全证据白名单。

    迁移前事实和 Provider 前不可扫描请求没有真实 scan ID/模式，因此这两个字段
    允许为空；其余字段保持与持久化扫描事实同名，便于前端精确区分 pending、
    not_configured、clean、matched、blocked、unavailable 与 not_applicable。
    """

    output_safety_scan_id: str | None
    scan_mode: Literal["non_stream_full", "stream_window"] | None
    scan_status: Literal[
        "pending",
        "not_configured",
        "completed",
        "unavailable",
        "not_applicable",
        "legacy_unavailable",
    ]
    action: Literal["allow", "log_only", "block"] | None
    dlp_rule_id: str | None
    rule_type: Literal["keyword", "regex"] | None
    match_count: int
    ruleset_fingerprint: str | None
    detector_version: str | None
    interval_tokens: int | None
    window_tokens: int | None
    checkpoint_count: int
    observed_output_token_count: int
    scanned_token_count: int
    failure_reason_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    dispute: RequestLogOutputSafetyDispute | None


class RequestLogInfo(BaseModel):
    """调用日志列表项。

    列表只展示排障和成本追踪所需的安全元数据，不包含 Prompt、Response、
    Virtual Key 明文、Provider Key 或上游请求头。
    """

    request_id: str
    trace_id: str
    tenant_id: str
    virtual_key_id: str | None
    virtual_key_prefix: str | None
    virtual_key_name: str | None = None
    consumer_name: str | None = None
    key_owner_name: str | None = None
    environment_id: str | None
    environment: str | None
    environment_name: str | None
    project_id: str | None = None
    project_name: str | None = None
    application_id: str | None = None
    application_name: str | None = None
    endpoint: str | None
    request_model: str | None
    resolved_model: str | None
    provider_id: str | None
    provider_code: str | None
    provider_name: str | None = None
    # 快照/策略字段只承载安全排障摘要；DSL、AST、Prompt 与上下文实际值不进入 DTO。
    snapshot_version: int | None = None
    policy_snapshot_status: str = "disabled"
    policy_decision: str | None = None
    policy_reason_code: str | None = None
    policy_id: str | None = None
    policy_action_type: str | None = None
    policy_target_model_id: str | None = None
    # mask 是非终止修饰动作，必须与最终 deny/allow/route_to 决定分栏追踪。
    policy_mask_applied: bool = False
    policy_mask_policy_id: str | None = None
    status: str
    error_code: str | None
    # 阶段是由成功终态或冻结错误码推导的终止点；unknown 表示证据不足，不能解释为已通过其他阶段。
    terminal_stage: str = "unknown"
    stream_session_id: str | None = None
    stream_status: str | None = None
    latency_ms: int | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_amount: float
    currency: str
    price_schema_version: str | None
    budget_status: str | None = None
    budget_rule_id: str | None = None
    budget_scope_type: str | None = None
    budget_scope_id: str | None = None
    budget_action: str | None = None
    budget_reason_code: str | None = None
    budget_current_cost_cny: float | None = None
    budget_limit_cny: float | None = None
    dlp_status: str | None = None
    dlp_rule_id: str | None = None
    dlp_action: str | None = None
    dlp_match_count: int = 0
    dlp_reason_code: str | None = None
    fallback_triggered: bool = False
    routing_attempt_count: int = 0
    final_routing_attempt_id: str | None = None
    # 列表只接收上面的固定摘要；详情中的规则引用和争议状态不会由此字段穿透。
    output_safety: RequestLogOutputSafetySummary | None = None
    # 列表仅暴露是否存在反馈，评分正文必须进入租户限定详情查询后才可返回。
    has_feedback: bool = False
    created_at: datetime
    updated_at: datetime | None


class RequestFeedbackInfo(BaseModel):
    """调用日志详情中可公开的一条不可变反馈事实。

    该结构只包含页面展示所需的对象 ID、源调用 ID、最终模型、评分、采纳、白名单
    标签、actor 类型和时间；tenant、actor ID、Provider、Virtual Key、指纹及事件
    payload 均不得进入响应，避免详情接口扩大身份与基础设施信息的暴露面。
    """

    request_feedback_id: str
    request_id: str
    model_id: str
    rating: int
    accepted: bool
    tags: list[str]
    actor_type: str
    status: str
    created_at: datetime


class RequestLogRoutingDecision(BaseModel):
    """调用日志详情中的路由决策摘要。"""

    routing_decision_id: str
    model_alias_id: str | None
    decision_status: str
    decision_reason: str
    requested_model: str
    environment: str
    environment_id: str | None
    selected_model_id: str | None
    selected_internal_model_id: str | None
    provider_id: str | None
    provider_code: str | None
    provider_name: str | None = None
    error_code: str | None
    routing_policy_id: str | None = None
    routing_policy_version: int | None = None
    routing_mode: str | None = None
    price_as_of: datetime | None = None
    estimated_input_tokens: int | None = None
    estimated_output_tokens: int | None = None
    token_estimate_truncated: bool | None = None
    routing_policy_event_id: str | None = None
    quality_weight_bps: int | None = None
    cost_weight_bps: int | None = None
    latency_weight_bps: int | None = None
    quality_floor_bps: int | None = None
    minimum_sample_count: int | None = None
    quality_as_of: datetime | None = None
    quality_formula_version: str | None = None
    quality_scoring_performed: bool = False
    candidate_summary: dict[str, Any]
    created_at: datetime


class RequestLogRoutingCandidate(BaseModel):
    """调用日志详情中的单个不可变路由候选事实。

    金额保留数据库 15 位小数字符串，避免 JSON float 破坏“排序值与解释值完全一致”；
    Model/Provider 展示名来自租户限定 LEFT JOIN，仅作为辅助标签，稳定 ID 仍是权威字段。
    """

    routing_candidate_id: str
    candidate_role: str
    configured_priority: int
    rank: int | None
    status: str
    reason_code: str
    model_id: str
    internal_model_id: str | None
    model_display_name: str | None
    provider_id: str | None
    provider_code: str | None
    provider_name: str | None
    health_state: str
    health_fresh: bool
    keypool_state: str
    price_available: bool
    price_source: str
    price_schema_version: str | None
    currency: str | None
    estimated_cost_amount: str | None
    comparison_currency: str | None = None
    comparison_fx_rate: str | None = None
    comparison_fx_rate_version: str | None = None
    comparison_cost_amount: str | None = None
    model_quality_score_id: str | None = None
    quality_statistics_version: int | None = None
    quality_feedback_version: int | None = None
    quality_evidence_state: str = "not_evaluated"
    quality_sample_count: int | None = None
    quality_success_count: int | None = None
    quality_error_count: int | None = None
    manual_quality_bps: int | None = None
    average_latency_ms: str | None = None
    quality_component_bps: int | None = None
    cost_component_bps: int | None = None
    latency_component_bps: int | None = None
    weighted_score_bps: int | None = None
    created_at: datetime


class RequestLogUsageInfo(BaseModel):
    """调用日志详情中的用量与成本条目。"""

    environment_id: str | None
    environment: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_amount: float
    cost_cny: float
    cost_usd: float
    currency: str
    price_schema_version: str | None
    price_snapshot_id: str | None
    estimated: bool
    usage_source: str
    normalized_usage: dict[str, Any]
    routing_attempt_id: str | None = None
    created_at: datetime


class RequestLogProviderErrorFact(BaseModel):
    """调用日志详情中的 Provider 错误归一化冻结事实。

    该 DTO 逐字段映射 0046 的安全投影，只允许规则身份、固定枚举、存在位、长度桶和哈希。
    原始 Provider code/type/message/body、Prompt、Response、Endpoint、Header、Key 与 Token
    均没有公开字段，数据库以后新增列也不能自动穿透到控制面响应。
    """

    provider_error_fact_id: str
    routing_attempt_id: str | None
    stream_session_id: str | None
    matched_error_map_id: str
    matched_rule_source: str
    matched_rule_version: int | None
    matched_pattern_hash: str
    error_map_revision: int
    overlay_content_hash: str
    builtin_catalog_version: str
    error_map_snapshot_source: str
    normalized_output_error_code: str
    owner_terminal_error_code: str
    upstream_http_status: int | None
    transport_kind: str
    retryable: bool
    match_basis: str
    provider_error_code_class: str | None
    provider_error_type_class: str | None
    provider_error_code_present: bool
    provider_error_type_present: bool
    message_present: bool
    redacted_projection_json: dict[str, Any]
    projection_hash: str
    body_length_bucket: str
    body_truncated: bool
    is_json: bool


class RequestLogRoutingAttempt(BaseModel):
    """调用日志详情中的单次上游尝试安全摘要。

    该对象不包含请求正文、响应正文、Endpoint、Header 或 Provider Key；started
    记录允许终态字段为空，用于如实展示进程中止后的“结果未知”。
    """

    routing_attempt_id: str
    attempt_sequence: int
    attempt_role: str
    trigger_type: str
    trigger_error_code: str | None
    model_id: str
    resolved_model: str
    provider_id: str
    provider_code: str
    provider_name: str | None = None
    status: str
    error_code: str | None
    error_category: str | None
    upstream_http_status: int | None
    retryable: bool
    latency_ms: int | None
    usage_status: str
    usage_ledger_id: str | None
    estimated_cost_cny: float | None
    created_at: datetime
    completed_at: datetime | None
    provider_error_fact: RequestLogProviderErrorFact | None = None


class RequestLogBudgetEvaluation(BaseModel):
    """调用日志详情中的预算命中快照。

    该结构只暴露预算规则、阈值、动作和成本金额，不返回任何 Provider Key、Virtual Key 明文
    或原始请求内容，便于财务和管理员定位 80%/100% 告警与阻断原因。
    """

    budget_rule_id: str
    environment_id: str
    scope_type: str
    scope_id: str
    budget_date: str
    spent_before_cny: float
    estimated_request_cost_cny: float
    projected_cost_cny: float
    budget_cny: float
    threshold_percent: int
    budget_status: str
    action_taken: str
    reason_code: str
    created_at: datetime


class RequestLogDlpMatch(BaseModel):
    """调用日志详情中的 DLP 命中摘要。

    该 DTO 只返回规则 ID、规则名称、动作、类型、哈希和长度，不返回命中文本、Prompt 或完整 messages。
    """

    dlp_match_id: str
    dlp_rule_id: str
    rule_name: str
    rule_type: str
    action: str
    message_index: int | None
    match_text_hash: str
    match_length: int
    created_at: datetime


class RequestLogStreamSession(BaseModel):
    """调用日志详情中的流会话安全摘要。

    字段白名单只包含生命周期、看门狗阈值和传输计数；不得加入 Endpoint、
    Credential、请求/响应正文、Header 或任何 Key 值。
    """

    stream_session_id: str
    status: str
    inter_token_timeout_ms: int
    chunk_count: int
    bytes_sent: int
    first_chunk_at: datetime | None
    last_chunk_at: datetime | None
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None
    # 0046 把握手失败的可信 HTTP/重试语义冻结到 owner；旧会话使用迁移默认值。
    upstream_http_status: int | None = None
    retryable: bool = False
    # 流会话自己保存实际候选模型和服务；主 RequestLog 在失败时可能尚未落最终路由。
    model_id: str | None = None
    resolved_model: str | None = None
    provider_id: str | None = None
    provider_code: str | None = None
    provider_name: str | None = None
    provider_error_fact: RequestLogProviderErrorFact | None = None


class RequestLogCacheAffinitySelection(BaseModel):
    """调用日志详情中的单次 CacheAffinity 选择安全快照。

    DTO 只暴露请求级 Selection ID、配置版本、稳定原因及模型/Provider；Credential、
    Binding、data_class、Prompt 指纹和 endpoint revision 仅留数据库内部做 CAS。
    """

    cache_affinity_selection_id: str
    cache_affinity_id: str
    cache_affinity_version: int
    attempt_sequence: int
    selection_reason: str
    estimated_hit: bool
    endpoint_preference_applied: bool
    target_kind: str
    model_id: str
    provider_id: str
    provider_code: str
    created_at: datetime


class RequestLogIntentRisk(BaseModel):
    """调用日志详情中的轻量敏感意图安全摘要。

    该对象只公开固定枚举、整数分数、版本和时间等不可逆元数据。Prompt、命中片段、
    输入 hash、actor ID 与异常文本均不属于响应合同；``pending`` 也不能被解释为 clean。
    """

    intent_risk_result_id: str | None
    status: str
    classification_mode: str | None
    risk_type: str | None
    risk_score: int | None
    risk_score_scale: int = 10000
    action: str | None
    reason_code: str | None
    detector_version: str | None
    ruleset_version: str | None
    input_code_points: int | None
    input_truncated: bool
    duration_ms: int | None
    completed_at: datetime | None


class RequestLogDetail(BaseModel):
    """承载 模型目录 的 RequestLogDetail 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    log: RequestLogInfo
    routing_decision: RequestLogRoutingDecision | None
    routing_candidates: list[RequestLogRoutingCandidate]
    usage: list[RequestLogUsageInfo]
    routing_attempts: list[RequestLogRoutingAttempt]
    cache_affinity_selections: list[RequestLogCacheAffinitySelection]
    # Response Cache 只暴露 7.5 exact 财务投影；entry、tag、data class 与 source owner 不得加入。
    response_cache: ResponseCacheRequestLogInfo | None
    budget_evaluations: list[RequestLogBudgetEvaluation]
    dlp_matches: list[RequestLogDlpMatch]
    intent_risk: RequestLogIntentRisk
    output_safety: RequestLogOutputSafetyDetail
    stream_session: RequestLogStreamSession | None
    error_diagnosis: ErrorDiagnosisInfo | None
    feedback: RequestFeedbackInfo | None
    feedback_eligible: bool
    feedback_ineligible_reason: str


class RequestLogListResponse(BaseModel):
    """定义 模型目录 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    request_logs: list[RequestLogInfo]
    total: int
    limit: int
    offset: int
    has_more: bool = False
    sort_by: str = "created_at"
    sort_direction: str = "desc"
    query_event_id: str | None = None
    request_id: str
    trace_id: str


class SavedFilterInfo(BaseModel):
    """当前用户私有的可复用调用日志筛选。

    响应只包含规范化筛选、白名单排序和乐观版本；owner、tenant 由认证上下文隐式限定，
    不向前端暴露可被误用为授权依据的身份字段。
    """

    saved_filter_id: str
    name: str
    filters: dict[str, Any]
    sort_by: str
    sort_direction: str
    version: int
    created_at: datetime
    updated_at: datetime


class SavedFilterListResponse(BaseModel):
    """SavedFilter 列表响应，统一携带本次控制面链路标识。"""

    saved_filters: list[SavedFilterInfo] = Field(default_factory=list)
    request_id: str
    trace_id: str


class SavedFilterResponse(BaseModel):
    """SavedFilter 单对象响应，供创建和更新后直接刷新页面状态。"""

    saved_filter: SavedFilterInfo
    request_id: str
    trace_id: str


class RequestLogDetailResponse(BaseModel):
    """定义 模型目录 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    request_log: RequestLogDetail
    request_id: str
    trace_id: str
