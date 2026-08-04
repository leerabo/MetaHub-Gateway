"""策略管理 API 的请求、响应与嵌套对象模型。

请求 DTO 只负责结构和额外字段边界，DSL、动作、数值与长度等领域值由 service
统一规范化，保证 validate 能返回稳定 diagnostics 并记录失败事件。响应模型只向
admin 管理接口暴露编辑所需正文，不得被日志、审计或 Webhook 直接序列化。
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PolicyRequestModel(BaseModel):
    """策略请求基类，禁止夹带 tenant、版本、AST 或其他未声明字段。"""

    model_config = ConfigDict(extra="forbid")


class PolicyRuleDraft(PolicyRequestModel):
    """待校验的单条条件规则。

    condition_expr 保持宽类型是有意设计：字符串之外的领域非法值需要进入策略
    规范化器，才能由 `/policies/validate` 返回 valid=false 并留下可追踪事件。
    """

    condition_expr: Any


class PolicyActionDraft(PolicyRequestModel):
    """待校验的单个策略动作，route_to 目标由服务层按当前租户复核。"""

    type: Any
    target_model_id: Any = None


class PolicyEvaluationContextDraft(PolicyRequestModel):
    """可选示例上下文，不允许客户端提交 tenant。

    tenant 始终由 AuthContext 注入；其余字段保持宽类型，以便 service 对缺失、
    bool、float、对象和超长值生成稳定诊断，而不是泄漏框架内部校验细节。
    """

    env: Any = None
    project: Any = None
    model: Any = None
    data_class: Any = None
    budget_remaining: Any = None


class ValidatePolicyRequest(PolicyRequestModel):
    """校验策略草稿的请求体；不产生 Policy/Rule/Action 业务对象。"""

    rule: PolicyRuleDraft
    action: PolicyActionDraft
    context: PolicyEvaluationContextDraft | None = None


class CreatePolicyRequest(PolicyRequestModel):
    """创建策略请求；tenant、ID、version 和编译结果均由服务端生成。"""

    name: Any
    rule: PolicyRuleDraft
    action: PolicyActionDraft
    description: Any = None
    status: Any = None
    priority: Any = None


class UpdatePolicyRequest(PolicyRequestModel):
    """PATCH 请求；是否提供字段通过 model_fields_set 判断，显式 null 不等于省略。"""

    name: Any = None
    rule: PolicyRuleDraft | None = None
    action: PolicyActionDraft | None = None
    description: Any = None
    status: Any = None
    priority: Any = None


class PolicyRuleInfo(BaseModel):
    """当前策略唯一规则的 admin 管理视图。"""

    policy_rule_id: str
    condition_expr: str
    condition_hash: str
    variables: list[str]
    created_at: datetime
    updated_at: datetime


class PolicyActionInfo(BaseModel):
    """当前策略唯一动作的 admin 管理视图。"""

    policy_action_id: str
    type: str
    target_model_id: str | None = None
    created_at: datetime
    updated_at: datetime


class PolicyInfo(BaseModel):
    """完整 Policy/Rule/Action 聚合视图，显式保留 tenant_id 和版本边界。"""

    policy_id: str
    tenant_id: str
    name: str
    description: str | None = None
    status: str
    priority: int
    version: int
    dsl_version: str
    rule: PolicyRuleInfo
    action: PolicyActionInfo
    created_at: datetime
    updated_at: datetime


class PolicyListResponse(BaseModel):
    """返回当前租户可见的策略列表，并统一携带 request_id/trace_id。"""

    policies: list[PolicyInfo]
    request_id: str
    trace_id: str


class PolicyResponse(BaseModel):
    """创建或更新单条策略后的响应。"""

    policy: PolicyInfo
    request_id: str
    trace_id: str


class PolicyDiagnosticInfo(BaseModel):
    """编辑器可展示的安全诊断，不包含表达式片段或异常正文。"""

    code: str
    field: str
    start: int | None = None
    end: int | None = None
    message: str


class PolicyValidationResponse(BaseModel):
    """策略草稿校验响应；无 context 时 evaluation 由路由层排除。"""

    valid: bool
    diagnostics: list[PolicyDiagnosticInfo]
    dsl_version: str | None = None
    draft_fingerprint: str | None = None
    condition_hash: str | None = None
    variables: list[str] | None = None
    canonical_expression: str | None = None
    evaluation: dict[str, Any] | None = None
    request_id: str
    trace_id: str


class PolicyDryRunRequest(BaseModel):
    """管理员构造的一次性预演上下文，禁止携带 tenant、历史请求或服务端制品字段。

    `strict=True` 防止 bool/float 被 Pydantic 隐式转成整数或字符串。Prompt 只会在当前
    请求函数栈参与策略长度、DLP 和预算估算，不得被该模型的调用方整体序列化到日志。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    prompt: str
    model: str
    project_id: str
    environment_id: str
    data_class: str
    max_output_tokens: int | None = None

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        """保留原始 Unicode code point，但拒绝控制边界、代理字符、空值和超长内容。"""

        # 空 Prompt 和超长输入都会突破 DLP/预算的固定工作量，必须在进入服务前拒绝。
        if not value or len(value) > 20_000:
            raise ValueError("prompt length is invalid")
        for character in value:
            codepoint = ord(character)
            category = unicodedata.category(character)
            # C0/C1 与代理字符会破坏日志和 UTF-8 边界，响应只保留固定校验错误。
            if (
                character == "\x00"
                or (codepoint < 32 and character not in {"\t", "\n", "\r"})
                or 0x7F <= codepoint <= 0x9F
                or category == "Cs"
            ):
                # 控制或代理字符不进入 Pydantic 原始错误详情，只触发固定领域校验失败。
                raise ValueError("prompt contains a forbidden control character")
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        """模型名按 PRD 显式 trim，并在 trim 后执行长度和控制字符校验。"""

        return _dry_run_bounded_text(value, maximum=128, trim=True)

    @field_validator("project_id", "environment_id")
    @classmethod
    def validate_context_id(cls, value: str) -> str:
        """上下文 ID 必须是单行、无控制字符的有界严格字符串。"""

        return _dry_run_bounded_text(value, maximum=128, trim=True)

    @field_validator("data_class")
    @classmethod
    def validate_data_class(cls, value: str) -> str:
        """what-if 分类只作为内存策略上下文，仍限制为有界安全文本。"""

        return _dry_run_bounded_text(
            value,
            maximum=64,
            trim=True,
            allow_internal_space=True,
        )

    @field_validator("max_output_tokens")
    @classmethod
    def validate_max_output_tokens(cls, value: int | None) -> int | None:
        """输出预留只接受真正整数，边界与 Gateway 请求预算保护保持一致。"""

        # bool 已由 strict 模式拒绝，剩余整数仍需限制最坏预算预留规模。
        if value is not None and not 1 <= value <= 131_072:
            raise ValueError("max_output_tokens is outside the supported range")
        return value


class PolicyDryRunComparison(BaseModel):
    """已访问比较节点的安全解释；故意删除 shared 结果中的 actual。"""

    variable: str
    operator: str
    value_type: str
    expected: str | list[str]
    matched: bool


class PolicyDryRunCheck(BaseModel):
    """单条 active Policy 的规则、动作、顺序与有界比较说明。"""

    policy_id: str
    policy_rule_id: str
    policy_action_id: str
    priority: int
    condition_hash: str
    action: Literal["allow", "deny", "mask", "log_only", "route_to"]
    matched: bool
    match_order: int | None = None
    comparisons: list[PolicyDryRunComparison] = Field(max_length=64)


class PolicyDryRunPolicyPreview(BaseModel):
    """策略纯求值结果，保留决定性规则以及 log_only/mask 非终止命中。"""

    decision: Literal["allow", "deny", "mask", "log_only", "route_to"]
    reason_code: str
    target_model_id: str | None = None
    matched_policy_id: str | None = None
    matched_priority: int | None = None
    matched_action: str | None = None
    policy_reason: PolicyDryRunCheck | None = None
    policy_checks: list[PolicyDryRunCheck] = Field(max_length=200)
    log_only_matches: list[PolicyDryRunCheck] = Field(max_length=20)
    # mask 仅公开稳定策略元数据；Prompt、命中位置、数量和正文 hash 均不进入响应。
    mask_matches: list[PolicyDryRunCheck] = Field(max_length=20)
    mask_required: bool
    checked_policy_count: int = Field(ge=0, le=200)
    timed_out: bool
    missing_variable: str | None = None
    explanation_truncated: bool


class PolicyDryRunTreeNode(BaseModel):
    """扁平决策树节点，禁止任意 payload 字典扩大响应合同。"""

    node_id: str
    parent_id: str | None = None
    order: int = Field(ge=0, le=255)
    node_type: Literal["policy_set", "policy_rule", "route", "dlp", "budget"]
    status: str
    reason_code: str
    policy_id: str | None = None
    policy_rule_id: str | None = None
    priority: int | None = None
    action: str | None = None
    matched: bool | None = None


class PolicyDryRunDecisionTree(BaseModel):
    """有界、顺序稳定的预演决策树。"""

    nodes: list[PolicyDryRunTreeNode] = Field(max_length=256)


class PolicyDryRunRouteCandidate(BaseModel):
    """不含 Provider URL、凭据、正文和健康原始错误的候选摘要。

    质量字段只展示同一预演快照的可复现计数与分量；``not_evaluated`` 时其余质量字段
    必须为空，页面不得查询当前 Score 后补算或覆盖 shared 已给出的排序。
    """

    role: str
    model_id: str
    internal_model_id: str | None = None
    provider_id: str | None = None
    provider_code: str | None = None
    status: Literal["selected", "rejected", "standby"]
    reason_code: str
    rank: int | None = Field(default=None, ge=1, le=2)
    health_state: Literal["healthy", "unhealthy", "unknown"] = "unknown"
    health_fresh: bool = False
    keypool_state: Literal["available", "unavailable", "unknown", "not_evaluated"] = (
        "not_evaluated"
    )
    price_available: bool = False
    price_source: Literal["price_plan", "model_catalog", "missing", "not_evaluated"] = (
        "not_evaluated"
    )
    price_schema_version: str | None = None
    currency: Literal["CNY", "USD"] | None = None
    estimated_cost_amount: str | None = None
    model_quality_score_id: str | None = None
    quality_statistics_version: int | None = Field(default=None, ge=0)
    quality_feedback_version: int | None = Field(default=None, ge=1)
    quality_evidence_state: Literal["ready", "cold", "not_evaluated"] = "not_evaluated"
    quality_sample_count: int | None = Field(default=None, ge=0)
    quality_success_count: int | None = Field(default=None, ge=0)
    quality_error_count: int | None = Field(default=None, ge=0)
    manual_quality_bps: int | None = Field(default=None, ge=0, le=10_000)
    average_latency_ms: str | None = None
    quality_component_bps: int | None = Field(default=None, ge=0, le=10_000)
    cost_component_bps: int | None = Field(default=None, ge=0, le=10_000)
    latency_component_bps: int | None = Field(default=None, ge=0, le=10_000)
    weighted_score_bps: int | None = Field(default=None, ge=0, le=10_000)


class PolicyDryRunRoutePreview(BaseModel):
    """租户内配置路由选择，不宣称 Provider 凭据或真实上游可调用。

    ``quality_scoring_performed`` 仅表示正权重分量齐备；质量配置和公式来自本次只读快照，
    不代表已经生成 Gateway Decision、Usage 或任何反馈统计。
    """

    status: Literal["selected", "unavailable", "not_evaluated"]
    reason_code: str
    requested_model: str
    routing_reason: str | None = None
    selected_model_id: str | None = None
    internal_model_id: str | None = None
    provider_id: str | None = None
    provider_code: str | None = None
    provider_credential_status: Literal["not_evaluated"] = "not_evaluated"
    chat_capability_supported: bool | None = None
    candidates: list[PolicyDryRunRouteCandidate] = Field(max_length=2)
    runtime_fallback_model_id: str | None = None
    fallback_cost_included: bool = False
    routing_policy_id: str | None = None
    routing_policy_version: int | None = Field(default=None, ge=1)
    routing_mode: Literal["fixed", "cost_first", "availability_first", "quality_cost"] | None = (
        None
    )
    price_as_of: datetime | None = None
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    estimated_output_tokens: int | None = Field(default=None, ge=0)
    token_estimate_truncated: bool | None = None
    eligible_candidate_count: int | None = Field(default=None, ge=0, le=2)
    quality_as_of: datetime | None = None
    quality_formula_version: Literal["quality_cost_v1"] | None = None
    quality_scoring_performed: bool = False
    quality_weight_bps: int | None = Field(default=None, ge=1, le=10_000)
    cost_weight_bps: int | None = Field(default=None, ge=0, le=10_000)
    latency_weight_bps: int | None = Field(default=None, ge=0, le=10_000)
    quality_floor_bps: int | None = Field(default=None, ge=1, le=10_000)
    minimum_sample_count: int | None = Field(default=None, ge=1, le=10_000)


class PolicyDryRunDlpMatch(BaseModel):
    """DLP 命中仅暴露稳定规则 ID、优先级和动作。"""

    dlp_rule_id: str
    priority: int
    action: Literal["allow", "log_only", "block"]


class PolicyDryRunDlpPreview(BaseModel):
    """DLP 扫描结果，不包含规则名、pattern、命中文本、位置或 hash。"""

    status: Literal["clean", "matched", "blocked", "not_evaluated"]
    reason_code: str
    matches: list[PolicyDryRunDlpMatch] = Field(max_length=100)
    match_count: int = Field(ge=0, le=100)
    scan_truncated: bool


class PolicyDryRunBudgetRule(BaseModel):
    """tenant/project 单条预算规则的固定精度安全结果。"""

    budget_rule_id: str
    scope_type: Literal["tenant", "project"]
    spent_cny: str
    reserved_cny: str
    projected_cny: str
    limit_cny: str
    status: Literal["within_limit", "threshold_reached", "exhausted"]
    action: Literal["allow", "warn", "block"]
    reason_code: str


class PolicyDryRunBudgetPreview(BaseModel):
    """当前业务日预算预演；application/virtual_key 明确保持未覆盖。"""

    status: Literal[
        "no_rules",
        "within_limit",
        "threshold_reached",
        "exhausted",
        "not_evaluated",
    ]
    reason_code: str
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)
    estimated_cost_cny: str
    price_available: bool
    fallback_cost_included: bool
    rules: list[PolicyDryRunBudgetRule] = Field(max_length=2)
    uncovered_scopes: list[Literal["application", "virtual_key"]]


class PolicyDryRunCoverage(BaseModel):
    """显式列出本期覆盖与缺口，避免把 runtime compatibility 误读为完整线上等价。"""

    policy: bool
    model_provider_routing: bool
    chat_capability: bool
    dlp: bool
    tenant_project_budget: bool
    virtual_key_application_budget: bool
    rpm_tpm_concurrency_limits: bool
    provider_credentials_keypool: bool
    real_provider: bool
    not_simulated: list[str]


class PolicyDryRunInputSummary(BaseModel):
    """输入只返回长度摘要，永远不返回所谓“脱敏 Prompt”或 hash。"""

    prompt_redacted: Literal[True]
    prompt_codepoints: int = Field(ge=1, le=20_000)
    estimated_input_tokens: int = Field(ge=1)


class PolicyExplanation(BaseModel):
    """OBJ-065 完整内存响应；数据库仅持久化其中不可逆的安全摘要。"""

    policy_explanation_id: str
    tenant_id: str
    status: Literal["completed"]
    version: Literal[1]
    policy_source: Literal["active_candidate"]
    policy_set_fingerprint: str
    published_snapshot_version: int | None = None
    simulation_only: Literal[True]
    runtime_compatible: bool
    runtime_incompatibility_codes: list[str]
    gateway_parity: Literal["partial"]
    coverage_complete: Literal[False]
    provider_called: Literal[False]
    usage_recorded: Literal[False]
    cost_recorded: Literal[False]
    outcome: Literal["would_allow", "would_deny", "would_block", "would_error"]
    would_error_code: str | None = None
    decision_tree: PolicyDryRunDecisionTree
    policy: PolicyDryRunPolicyPreview
    route: PolicyDryRunRoutePreview
    dlp: PolicyDryRunDlpPreview
    budget: PolicyDryRunBudgetPreview
    coverage: PolicyDryRunCoverage
    input_summary: PolicyDryRunInputSummary
    created_at: datetime
    updated_at: datetime


class PolicyDryRunResponse(BaseModel):
    """Dry-Run 成功响应，链路 ID 同时由共享中间件写入响应头。"""

    policy_explanation: PolicyExplanation
    request_id: str
    trace_id: str


def _dry_run_bounded_text(
    value: str,
    *,
    maximum: int,
    trim: bool,
    allow_internal_space: bool = False,
) -> str:
    """规范化非 Prompt 短文本，并拒绝空白、控制/格式/代理字符。"""

    normalized = value.strip() if trim else value
    # 标识与分类值不能为空或无界，否则会扩大查询和 DSL 上下文的解释空间。
    if not normalized or len(normalized) > maximum:
        raise ValueError("text length is invalid")
    # 仅 data_class 可含内部 ASCII 空格；控制、格式和其他空白始终拒绝。
    if any(
        unicodedata.category(character).startswith("C")
        or (character.isspace() and not (allow_internal_space and character == " "))
        for character in normalized
    ):
        # 非法短文本不回显具体字符或位置，避免将敏感上下文复制到错误响应。
        raise ValueError("text contains a forbidden character")
    return normalized
