"""PRD-054 请求解释中心的公开、最小披露数据合同。

本模块只声明 Control API 可以返回的白名单字段。所有业务事实都来自按租户限定的
数据库快照，模型中故意不提供 Prompt、Response、Provider Credential、Endpoint URL、
Webhook payload 或原始异常正文的容器，避免服务层误把内部行整体序列化到响应。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from .contracts import (
    FactKind,
    SECTION_FACT_KINDS,
    SECTION_ORDER,
    STAGE_ORDER,
    validate_fact_values,
)

# 决策阶段与详情区块使用两套正交状态：业务阶段说明“发生了什么”，证据状态说明
# “我们能否完整证明”。这样 Provider 失败也可以拥有 complete 证据，而不会被误报为缺失。
EvidenceState = Literal["complete", "partial", "not_recorded", "not_applicable", "pending"]
DecisionStageStatus = Literal[
    "passed",
    "blocked",
    "failed",
    "pending",
    "not_evaluated",
    "not_recorded",
]
ExplanationStatus = Literal["complete", "partial"]
StageName = Literal[
    "authentication",
    "rate_limit",
    "budget",
    "dlp",
    "policy",
    "routing",
    "keypool",
    "provider",
    "usage_and_finops",
    "logs_and_webhooks",
]
SectionName = Literal[
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
]
SafeScalar = StrictStr | StrictInt | StrictBool | None


class RequestExplanationFact(BaseModel):
    """详情区块中的一条扁平安全事实。

    ``kind`` 和 ``fact_id`` 是固定分类与稳定对象 ID；``values`` 只允许标量，不能嵌入
    任意 JSON 正文。服务层仍须逐字段构造 values，禁止把 sqlite Row 或 model_dump
    结果直接塞入该结构。
    """

    model_config = ConfigDict(extra="forbid")

    kind: FactKind
    fact_id: StrictStr | None = None
    occurred_at: datetime | None = None
    values: dict[StrictStr, SafeScalar] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_exact_values(self) -> "RequestExplanationFact":
        """要求事实精确匹配已审查字段，阻止数据库新增列自动穿透 API。"""

        validate_fact_values(self.kind, dict(self.values))
        return self


class RequestExplanationSection(BaseModel):
    """一个详情区块的证据状态、有界事实和截断标志。"""

    model_config = ConfigDict(extra="forbid")

    evidence_state: EvidenceState
    reason_code: StrictStr
    facts: list[RequestExplanationFact] = Field(default_factory=list)
    truncated: StrictBool = False


class RequestExplanationDecisionStage(BaseModel):
    """按 Gateway 执行顺序展示的一步决策及其安全证据引用。"""

    model_config = ConfigDict(extra="forbid")

    stage: StageName
    status: DecisionStageStatus
    evidence_state: EvidenceState
    reason_code: StrictStr
    occurred_at: datetime | None = None
    evidence_refs: list[StrictStr] = Field(default_factory=list, max_length=16)


class RequestExplanationSummary(BaseModel):
    """解释页首屏需要的请求、归属、结果与用量安全摘要。

    名称字段只是当前租户内的显示标签；稳定 ID、源请求时间和冻结 snapshot 才是审计
    权威。成本金额使用字符串，避免 JSON Number 与 JavaScript Number 造成精度漂移。
    """

    model_config = ConfigDict(extra="forbid")

    source_status: Literal["success", "failed"]
    error_code: StrictStr | None = None
    request_model: StrictStr | None = None
    resolved_model: StrictStr | None = None
    provider_code: StrictStr | None = None
    environment_id: StrictStr | None = None
    environment_name: StrictStr | None = None
    project_id: StrictStr | None = None
    project_name: StrictStr | None = None
    application_id: StrictStr | None = None
    application_name: StrictStr | None = None
    snapshot_version: StrictInt | None = None
    latency_ms: StrictInt | None = None
    input_tokens: StrictInt
    output_tokens: StrictInt
    total_tokens: StrictInt
    estimated_cost_amount: StrictStr | None = None
    currency: StrictStr
    fallback_triggered: StrictBool
    created_at: datetime
    updated_at: datetime


class RequestExplanation(BaseModel):
    """一次已审计的请求解释响应。

    十个决策阶段和十一个详情区块都是固定字段；客户端不能用动态 include 参数扩大
    数据面。``view_event_status`` 反映 source outbox 已持久化，而非宣称远端已收到事件。
    """

    model_config = ConfigDict(extra="forbid")

    request_explanation_id: StrictStr
    source_request_id: StrictStr
    source_trace_id: StrictStr
    status: ExplanationStatus
    evidence_version: Literal["request_explanation_v1"]
    captured_at: datetime
    summary: RequestExplanationSummary
    decision_chain: list[RequestExplanationDecisionStage] = Field(min_length=10, max_length=10)
    authentication: RequestExplanationSection
    rate_limit: RequestExplanationSection
    budget: RequestExplanationSection
    dlp: RequestExplanationSection
    policy: RequestExplanationSection
    routing: RequestExplanationSection
    keypool: RequestExplanationSection
    provider_response: RequestExplanationSection
    usage_and_finops: RequestExplanationSection
    logs: RequestExplanationSection
    webhooks: RequestExplanationSection
    missing_sections: list[SectionName]
    truncated_sections: list[SectionName]
    view_event_status: Literal["pending"] = "pending"

    @model_validator(mode="after")
    def validate_frozen_shape(self) -> "RequestExplanation":
        """冻结阶段顺序、区块事实类型和完整性数组，防止客户端解释分叉。"""

        if tuple(stage.stage for stage in self.decision_chain) != STAGE_ORDER:
            # 阶段乱序会让页面把证据归到错误决策，不能由客户端自行纠正或猜测。
            raise ValueError("request_explanation.stage_order_invalid")
        missing = tuple(self.missing_sections)
        truncated = tuple(self.truncated_sections)
        positions = {name: index for index, name in enumerate(SECTION_ORDER)}
        if (
            tuple(sorted(set(missing), key=positions.__getitem__)) != missing
            or tuple(sorted(set(truncated), key=positions.__getitem__)) != truncated
            or set(missing) & set(truncated)
        ):
            # 缺失与截断必须稳定排序且互斥，否则签名、审计和前端完整性提示会产生分叉。
            raise ValueError("request_explanation.section_order_invalid")
        if self.status == "complete" and (missing or truncated):
            # complete 不能掩盖任何缺失或截断证据，避免治理人员得到虚假的完整结论。
            raise ValueError("request_explanation.status_invalid")
        if self.status == "partial" and not (missing or truncated):
            # partial 必须能定位到具体缺口，禁止返回无法解释的笼统降级状态。
            raise ValueError("request_explanation.status_invalid")
        for section_name, allowed_kinds in SECTION_FACT_KINDS.items():
            section = getattr(self, section_name)
            if any(fact.kind not in allowed_kinds for fact in section.facts):
                # kind 放错区块会误导决策链引用，也可能扩大该区块原本允许的披露范围。
                raise ValueError("request_explanation.section_fact_invalid")
            if section.truncated != (section_name in truncated):
                # 局部标志与汇总数组必须一致，防止 Console 只看到其中一处而漏报截断。
                raise ValueError("request_explanation.section_truncation_invalid")
        return self


class RequestExplanationResponse(BaseModel):
    """统一携带本次操作链路标识的请求解释响应。

    ``request_id`` 与 ``trace_id`` 只描述 Control 读取操作，源 Gateway 链路保留在 Explanation
    内，二者不得混用或覆盖。
    """

    model_config = ConfigDict(extra="forbid")

    request_explanation: RequestExplanation
    request_id: StrictStr
    trace_id: StrictStr


__all__ = [
    "DecisionStageStatus",
    "EvidenceState",
    "ExplanationStatus",
    "RequestExplanation",
    "RequestExplanationDecisionStage",
    "RequestExplanationFact",
    "RequestExplanationResponse",
    "RequestExplanationSection",
    "RequestExplanationSummary",
    "SectionName",
    "StageName",
]
