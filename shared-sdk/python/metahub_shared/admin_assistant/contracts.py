"""PRD-077 只读管理助手的跨服务确定性合同。

本模块只接受已经由服务端授权并聚合的安全事实，不接触数据库、HTTP Request 或问题正文。
回答中的 claim value 仍可能包含租户业务摘要，因此从 repr 隐藏，并且只能通过 exact wire
validator 进入公开响应。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

QueryType = Literal[
    "usage_statistics",
    "error_explanation",
    "cost_advice",
    "policy_explanation",
]
ClaimType = Literal["integer", "decimal", "percentage", "text"]
SourceType = Literal[
    "overview",
    "request_logs",
    "request_explanations",
    "usage",
    "finops_ledger",
    "budgets",
    "cost_anomalies",
    "model_prices",
    "policies",
    "policy_snapshots",
]

# HTTP 入口一次冻结来源、业务决策和最终治理三条绝对预算，禁止各层从当前时刻重开窗口。
ADMIN_ASSISTANT_ENDPOINT_DEADLINE_SECONDS: Final[float] = 1.5
ADMIN_ASSISTANT_SOURCE_DEADLINE_SECONDS: Final[float] = 1.2
ADMIN_ASSISTANT_GOVERNANCE_DEADLINE_SECONDS: Final[float] = 5.0

# 查询/claim/source 枚举、内部 href、源权限和固定文案共同组成跨服务 exact 合同；
# 问题、租户、自由 URL 与 Provider 内容均不能扩展这些集合。
QUERY_TYPES: Final[tuple[QueryType, ...]] = (
    "usage_statistics",
    "error_explanation",
    "cost_advice",
    "policy_explanation",
)
CLAIM_TYPES: Final[tuple[ClaimType, ...]] = (
    "integer",
    "decimal",
    "percentage",
    "text",
)
SOURCE_TYPES: Final[tuple[SourceType, ...]] = (
    "overview",
    "request_logs",
    "request_explanations",
    "usage",
    "finops_ledger",
    "budgets",
    "cost_anomalies",
    "model_prices",
    "policies",
    "policy_snapshots",
)

SOURCE_HREFS: Final[dict[SourceType, str]] = {
    "overview": "/#overview",
    "request_logs": "/#request-logs",
    "request_explanations": "/#request-explanations",
    "usage": "/#usage",
    "finops_ledger": "/#finops-ledger",
    "budgets": "/#budgets",
    "cost_anomalies": "/#cost-anomalies",
    "model_prices": "/#model-prices",
    "policies": "/#policies",
    "policy_snapshots": "/#policy-snapshots",
}
SOURCE_LABELS: Final[dict[SourceType, str]] = {
    "overview": "运行概览",
    "request_logs": "调用日志",
    "request_explanations": "请求解释",
    "usage": "用量账本",
    "finops_ledger": "FinOps 账本",
    "budgets": "预算",
    "cost_anomalies": "成本异常",
    "model_prices": "模型价格",
    "policies": "策略",
    "policy_snapshots": "策略快照",
}
QUERY_REQUIRED_PERMISSIONS: Final[dict[QueryType, tuple[str, ...]]] = {
    "usage_statistics": (
        "admin_assistant.query",
        "billing.usage.read",
        "dashboard.read",
    ),
    "error_explanation": (
        "admin_assistant.query",
        "request_log.read",
    ),
    "cost_advice": (
        "admin_assistant.query",
        "billing.anomaly.read",
        "billing.budget.read",
        "billing.finops.read",
        "billing.usage.read",
        "dashboard.read",
        "model_price.read",
    ),
    "policy_explanation": (
        "admin_assistant.query",
        "policy.read",
    ),
}
QUERY_SOURCE_TYPES: Final[dict[QueryType, frozenset[SourceType]]] = {
    "usage_statistics": frozenset({"overview", "usage"}),
    "error_explanation": frozenset({"request_logs"}),
    "cost_advice": frozenset(
        {
            "overview",
            "usage",
            "finops_ledger",
            "budgets",
            "cost_anomalies",
            "model_prices",
        }
    ),
    "policy_explanation": frozenset({"policies", "policy_snapshots", "request_explanations"}),
}
REDACTION_CODES: Final[tuple[str, ...]] = (
    "content_omitted",
    "credentials_omitted",
    "raw_provider_error_omitted",
)
RECOMMENDATION_MESSAGES: Final[dict[str, str]] = {
    "high_failure_rate": "失败率偏高，建议检查固定错误分类和对应调用日志。",
    "high_estimated_cost_share": "估算成本占比较高，建议先补齐正式成本证据再调整预算。",
    "high_model_cost_concentration": "成本集中于少数模型，建议结合质量与价格证据复核路由。",
    "priority_collision_review": "存在同优先级策略候选组，建议进行人工复核。",
    "insufficient_evidence": "当前证据不足以给出优化建议。",
}

# ID、公开 code、单位、十进制、链路长度和危险 Unicode 边界只用于 wire 校验，
# 不对输入做宽松转换，也不把失败值放进异常 repr。
_OBJECT_ID = re.compile(r"^[a-z]+_[0-9a-f]{32}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_UNIT = re.compile(r"^[A-Za-z][A-Za-z0-9._/%-]{0,31}$")
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9][A-Za-z0-9._:-]{0,155}$")
_TRACE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{1,159}$")
# JSON number 会被 Console 解析为 JavaScript Number；超出该上限即使 SQLite/Python 可保存，
# 浏览器也无法逐整数精确关联 claim 与来源，因此 wire 合同收敛到跨语言共同精确范围。
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_BIDI_CONTROLS = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)})


class AdminAssistantContractError(ValueError):
    """只携带固定错误码，避免把 claim value 或来源事实带入异常。"""

    def __init__(self, code: str = "admin_assistant.answer_invalid") -> None:
        """保存目录内固定机器码；异常消息不拼接 claim、source 或原始 wire。"""

        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ParsedAssistantQuery:
    """不含问题正文的解析结果，可安全传给只读查询服务。"""

    query_type: QueryType
    window_hours: Literal[24, 168, 720]
    target_request_id: str | None = None

    def __post_init__(self) -> None:
        """校验类型、窗口和 request-specific 范围，不保留规范化前的问题正文。"""

        # 查询类型和时间窗必须来自有限目录，禁止调用方扩展数据库读取范围。
        if self.query_type not in QUERY_TYPES or self.window_hours not in {24, 168, 720}:
            raise AdminAssistantContractError("admin_assistant.request_invalid")
        # request_id 只接受公开链路格式，畸形值不得进入后续查询参数。
        if self.target_request_id is not None and not _REQUEST_ID.fullmatch(self.target_request_id):
            # 多行格式校验失败仍收敛为固定码，不回显传入的 request_id。
            raise AdminAssistantContractError("admin_assistant.request_invalid")
        # request-specific 聚合只属于错误和策略解释，其他查询不得借 ID 扩大读取范围。
        if self.target_request_id is not None and self.query_type not in {
            "error_explanation",
            "policy_explanation",
        }:
            # 指定请求的其他意图会产生范围歧义，因此不能退化为窗口聚合。
            raise AdminAssistantContractError("admin_assistant.query_ambiguous")


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    """一个可验证回答事实；value 不进入 repr。"""

    claim_id: str
    claim_type: ClaimType
    label: str
    value: int | str = field(repr=False)
    unit: str | None
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """冻结来源 ID，并校验 claim 标识、typed value、单位与公开标签。"""

        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        # claim 标识和类型必须来自受控目录，拒绝自由 schema 扩展。
        if not _SAFE_CODE.fullmatch(self.claim_id) or self.claim_type not in CLAIM_TYPES:
            raise AdminAssistantContractError()
        _bounded_public_text(self.label, maximum=80)
        if self.unit is not None and (
            type(self.unit) is not str or _UNIT.fullmatch(self.unit) is None
        ):
            # 单位将公开展示，只允许短 ASCII 记号，不能夹带任意文本。
            raise AdminAssistantContractError()
        _validate_claim_value(self.claim_type, self.value)
        _validate_source_id_list(self.source_ids)

    def to_wire(self) -> dict[str, object]:
        """输出 exact 六字段 claim；隐藏 repr 不影响经过授权的公开回答值。"""

        return {
            "claim_id": self.claim_id,
            "claim_type": self.claim_type,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True, slots=True)
class AnswerRecommendation:
    """由固定 code 映射生成的建议，不接受调用方提供自由文案。"""

    code: str
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """冻结来源 ID，并要求建议 code 来自 Shared 固定文案目录。"""

        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        # 建议只能使用 Shared 中冻结的机器码，消息正文由合同派生。
        if self.code not in RECOMMENDATION_MESSAGES:
            raise AdminAssistantContractError()
        _validate_source_id_list(self.source_ids)

    @property
    def message(self) -> str:
        """从机器 code 派生公开固定建议，不接受调用方自由文本。"""

        return RECOMMENDATION_MESSAGES[self.code]

    def to_wire(self) -> dict[str, object]:
        """输出 code/message/source_ids 三字段并保持固定文案逐字一致。"""

        return {
            "code": self.code,
            "message": self.message,
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True, slots=True)
class AnswerSource:
    """由枚举构造的控制台内部来源链接和同一 UTC 快照窗口。"""

    source_type: SourceType
    record_count: int
    window_started_at: datetime
    window_ended_at: datetime
    snapshot_at: datetime

    def __post_init__(self) -> None:
        """规范化 UTC 秒并校验 source 枚举、计数和最大三十天窗口。"""

        # 来源类型决定固定权限与内部链接，未知类型不得进入回答图。
        if self.source_type not in SOURCE_TYPES:
            raise AdminAssistantContractError()
        _nonnegative_int(self.record_count)
        started = _utc_second(self.window_started_at)
        ended = _utc_second(self.window_ended_at)
        snapshot = _utc_second(self.snapshot_at)
        object.__setattr__(self, "window_started_at", started)
        object.__setattr__(self, "window_ended_at", ended)
        object.__setattr__(self, "snapshot_at", snapshot)
        # 窗口必须是左闭右开且不超过 30 天；snapshot 不能早于它声称覆盖的窗口终点。
        if not started < ended or ended - started > timedelta(days=30) or snapshot < ended:
            # 非法窗口可能伪造快照覆盖范围，因此整个来源必须失败关闭。
            raise AdminAssistantContractError()

    @property
    def source_id(self) -> str:
        """从受控 source_type 派生唯一内部来源 ID，问题不能提供该值。"""

        return f"src_{self.source_type}"

    @property
    def label(self) -> str:
        """从 Shared 目录派生控制台公开标签，不读取数据库资源名称。"""

        return SOURCE_LABELS[self.source_type]

    @property
    def href(self) -> str:
        """从内部 allowlist 派生相对 href，拒绝任意外链或问题生成 URL。"""

        return SOURCE_HREFS[self.source_type]

    def to_wire(self) -> dict[str, object]:
        """输出 exact 来源元数据；只含计数、窗口和 allowlist 内部链接。"""

        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "label": self.label,
            "href": self.href,
            "record_count": self.record_count,
            "window_started_at": _format_utc_second(self.window_started_at),
            "window_ended_at": _format_utc_second(self.window_ended_at),
            "snapshot_at": _format_utc_second(self.snapshot_at),
        }


@dataclass(frozen=True, slots=True)
class AssistantAnswer:
    """公开响应的完整 exact-schema 图，不保存 tenant、actor 或问题正文。"""

    assistant_query_id: str
    assistant_audit_id: str
    query_type: QueryType
    claims: tuple[AnswerClaim, ...]
    recommendations: tuple[AnswerRecommendation, ...]
    sources: tuple[AnswerSource, ...]
    request_id: str
    trace_id: str

    def __post_init__(self) -> None:
        """校验完整回答图、唯一 ID、来源闭包和查询类型允许的来源集合。"""

        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "recommendations", tuple(self.recommendations))
        object.__setattr__(self, "sources", tuple(self.sources))
        _prefixed_id(self.assistant_query_id, "asq_")
        _prefixed_id(self.assistant_audit_id, "asa_")
        # query_type 必须属于四类确定性只读查询，不能由正文创造新意图。
        if self.query_type not in QUERY_TYPES:
            raise AdminAssistantContractError()
        # 响应 JSON 内的 request_id/trace_id 必须符合公开链路标识合同。
        if not _REQUEST_ID.fullmatch(self.request_id) or not _TRACE_ID.fullmatch(self.trace_id):
            raise AdminAssistantContractError()
        # 回答至少包含一个 claim 和一个来源，防止空壳成功响应被审计为完成。
        if not self.claims or not self.sources:
            raise AdminAssistantContractError()
        # 数组数量采用固定上限，避免异常响应放大内存与页面渲染成本。
        if len(self.claims) > 64 or len(self.recommendations) > 16 or len(self.sources) > 10:
            raise AdminAssistantContractError()
        source_ids = [source.source_id for source in self.sources]
        source_types = [source.source_type for source in self.sources]
        # 来源 ID 或类型重复会破坏 claim 图的唯一指向，必须整份拒绝。
        if len(source_ids) != len(set(source_ids)) or len(source_types) != len(set(source_types)):
            raise AdminAssistantContractError()
        # 每类查询只能引用预先声明的业务域来源，禁止跨权限域拼接事实。
        if not set(source_types).issubset(QUERY_SOURCE_TYPES[self.query_type]):
            raise AdminAssistantContractError()
        claim_ids = [claim.claim_id for claim in self.claims]
        # claim ID 在单次回答内必须唯一，避免相同标识承载冲突事实。
        if len(claim_ids) != len(set(claim_ids)):
            raise AdminAssistantContractError()
        referenced: set[str] = set()
        for item in (*self.claims, *self.recommendations):
            # 每条 claim/建议只能引用回答图内已声明的来源。
            if not set(item.source_ids).issubset(source_ids):
                raise AdminAssistantContractError()
            referenced.update(item.source_ids)
        # 不允许返回没有支撑 claim/recommendation 的装饰来源，也不允许引用图外 ID。
        if referenced != set(source_ids):
            raise AdminAssistantContractError()

    @property
    def summary(self) -> str:
        """只按来源数量派生固定摘要，绝不总结问题或业务值。"""

        return f"基于 {len(self.sources)} 个可验证来源生成。"

    @property
    def redactions(self) -> tuple[str, ...]:
        """按查询类型派生固定脱敏声明；错误查询额外省略 Provider 原始错误。"""

        base = ("content_omitted", "credentials_omitted")
        # 错误解释还必须声明原始 Provider 错误已省略，防止前端误以为可安全展示。
        if self.query_type == "error_explanation":
            return (*base, "raw_provider_error_omitted")
        return base

    def to_wire(self) -> dict[str, object]:
        """输出公开 exact 回答 envelope，不包含 tenant、actor、问题或模型调用事实。"""

        return {
            "assistant_query_id": self.assistant_query_id,
            "assistant_audit_id": self.assistant_audit_id,
            "query_type": self.query_type,
            "answer": {
                "summary": self.summary,
                "claims": [claim.to_wire() for claim in self.claims],
                "recommendations": [item.to_wire() for item in self.recommendations],
            },
            "sources": [source.to_wire() for source in self.sources],
            "redactions": list(self.redactions),
            "request_id": self.request_id,
            "trace_id": self.trace_id,
        }


def required_permissions_for_query(
    query_type: QueryType, *, request_specific: bool = False
) -> tuple[str, ...]:
    """返回排序、去重的后端权限全集，base permission 不能替代源域权限。"""

    # 权限矩阵只接受受控查询类型和 exact bool，避免宽松真值扩大读取范围。
    if query_type not in QUERY_TYPES or type(request_specific) is not bool:
        raise AdminAssistantContractError("admin_assistant.request_invalid")
    permissions = set(QUERY_REQUIRED_PERMISSIONS[query_type])
    if request_specific:
        if query_type == "policy_explanation":
            permissions.add("request_explanation.read")
        # 指定 request_id 仅对错误或策略解释有确定语义，其他类型拒绝降级。
        elif query_type != "error_explanation":
            raise AdminAssistantContractError("admin_assistant.query_ambiguous")
    # 排序后的精确权限全集供后端二次校验，base permission 不能替代来源权限。
    return tuple(sorted(permissions))


def build_assistant_answer(
    *,
    assistant_query_id: str,
    assistant_audit_id: str,
    query_type: QueryType,
    claims: tuple[AnswerClaim, ...] | list[AnswerClaim],
    recommendations: tuple[AnswerRecommendation, ...] | list[AnswerRecommendation],
    sources: tuple[AnswerSource, ...] | list[AnswerSource],
    request_id: str,
    trace_id: str,
) -> AssistantAnswer:
    """从 typed facts 构造回答；summary、redaction 与来源 URL 均由 Shared 派生。"""

    return AssistantAnswer(
        assistant_query_id=assistant_query_id,
        assistant_audit_id=assistant_audit_id,
        query_type=query_type,
        claims=tuple(claims),
        recommendations=tuple(recommendations),
        sources=tuple(sources),
        request_id=request_id,
        trace_id=trace_id,
    )


def validate_assistant_answer_wire(value: object) -> AssistantAnswer:
    """验证任意解码 JSON 的精确字段图，并恢复经过约束的类型化只读回答。"""

    try:
        root = _exact_dict(
            value,
            {
                "assistant_query_id",
                "assistant_audit_id",
                "query_type",
                "answer",
                "sources",
                "redactions",
                "request_id",
                "trace_id",
            },
        )
        answer = _exact_dict(root["answer"], {"summary", "claims", "recommendations"})
        query_type = root["query_type"]
        # wire 查询类型必须是 exact str 且属于 Shared 冻结枚举。
        if type(query_type) is not str or query_type not in QUERY_TYPES:
            raise AdminAssistantContractError()
        sources = tuple(_source_from_wire(item) for item in _exact_list(root["sources"]))
        claims = tuple(_claim_from_wire(item) for item in _exact_list(answer["claims"]))
        recommendations = tuple(
            _recommendation_from_wire(item) for item in _exact_list(answer["recommendations"])
        )
        result = build_assistant_answer(
            assistant_query_id=_exact_str(root["assistant_query_id"]),
            assistant_audit_id=_exact_str(root["assistant_audit_id"]),
            query_type=query_type,  # type: ignore[arg-type]
            claims=claims,
            recommendations=recommendations,
            sources=sources,
            request_id=_exact_str(root["request_id"]),
            trace_id=_exact_str(root["trace_id"]),
        )
        # 摘要必须由来源数量确定性派生，不接受服务端生成的自由总结。
        if type(answer["summary"]) is not str or answer["summary"] != result.summary:
            raise AdminAssistantContractError()
        redactions = _exact_list(root["redactions"])
        if (
            any(type(item) is not str for item in redactions)
            or tuple(redactions) != result.redactions
        ):
            # 脱敏声明缺失、乱序或扩展都会使公开响应合同失效。
            raise AdminAssistantContractError()
        return result
    # 合同异常已经只携带固定码，保持原样供 API 层统一映射。
    except AdminAssistantContractError:
        raise
    # 容器、字段或十进制解析异常统一折叠，不暴露畸形 wire 的 repr。
    except (KeyError, TypeError, ValueError, InvalidOperation):
        # 任意结构错误都归一成固定码，不把畸形输入的 repr 或底层解析正文带出边界。
        raise AdminAssistantContractError() from None


def _claim_from_wire(value: object) -> AnswerClaim:
    """从任意 JSON 值恢复精确 claim，并复用数据类的类型和值域校验。"""

    item = _exact_dict(value, {"claim_id", "claim_type", "label", "value", "unit", "source_ids"})
    claim_type = item["claim_type"]
    # claim_type 必须是合同枚举中的 exact str，禁止数值或自定义对象宽松转换。
    if type(claim_type) is not str or claim_type not in CLAIM_TYPES:
        raise AdminAssistantContractError()
    unit = item["unit"]
    # 单位只能为空或字符串，具体字符白名单由 AnswerClaim 再校验。
    if unit is not None and type(unit) is not str:
        raise AdminAssistantContractError()
    return AnswerClaim(
        claim_id=_exact_str(item["claim_id"]),
        claim_type=claim_type,  # type: ignore[arg-type]
        label=_exact_str(item["label"]),
        value=item["value"],  # validated by AnswerClaim
        unit=unit,
        source_ids=tuple(_exact_str(entry) for entry in _exact_list(item["source_ids"])),
    )


def _recommendation_from_wire(value: object) -> AnswerRecommendation:
    """恢复 exact 建议并核对 code 派生文案，拒绝服务端自由消息漂移。"""

    item = _exact_dict(value, {"code", "message", "source_ids"})
    recommendation = AnswerRecommendation(
        code=_exact_str(item["code"]),
        source_ids=tuple(_exact_str(entry) for entry in _exact_list(item["source_ids"])),
    )
    # 固定建议正文必须与机器码派生值逐字一致，拒绝自由消息覆盖。
    if type(item["message"]) is not str or item["message"] != recommendation.message:
        raise AdminAssistantContractError()
    return recommendation


def _source_from_wire(value: object) -> AnswerSource:
    """恢复 exact 来源，并重新派生 source_id、label 与 href 防止链接注入。"""

    item = _exact_dict(
        value,
        {
            "source_id",
            "source_type",
            "label",
            "href",
            "record_count",
            "window_started_at",
            "window_ended_at",
            "snapshot_at",
        },
    )
    source_type = item["source_type"]
    # 来源类型必须属于冻结目录，后续链接与标签才能由本地重新派生。
    if type(source_type) is not str or source_type not in SOURCE_TYPES:
        raise AdminAssistantContractError()
    source = AnswerSource(
        source_type=source_type,  # type: ignore[arg-type]
        record_count=item["record_count"],  # validated with exact int
        window_started_at=_parse_utc_second(item["window_started_at"]),
        window_ended_at=_parse_utc_second(item["window_ended_at"]),
        snapshot_at=_parse_utc_second(item["snapshot_at"]),
    )
    if (
        item["source_id"] != source.source_id
        or item["label"] != source.label
        or item["href"] != source.href
    ):
        # 任一来源身份字段漂移都可能绕过导航权限求交，必须整项拒绝。
        raise AdminAssistantContractError()
    return source


def _validate_claim_value(claim_type: ClaimType, value: object) -> None:
    """按 integer/decimal/percentage/text 分支校验值类型、范围和 canonical 表示。"""

    if claim_type == "integer":
        _nonnegative_int(value)
        return
    # 非整数 claim 的 wire 值必须是短字符串，避免 bool/float 宽松转换。
    if type(value) is not str or len(value) > 512:
        raise AdminAssistantContractError()
    if claim_type in {"decimal", "percentage"}:
        # 十进制与百分比只接受规范非指数文本，保持跨语言精确一致。
        if len(value) > 64 or _DECIMAL.fullmatch(value) is None:
            raise AdminAssistantContractError()
        try:
            parsed = Decimal(value)
        # Decimal 解析失败只转换成固定合同错误，不附带原始数值。
        except InvalidOperation:
            raise AdminAssistantContractError() from None
        # 非有限值和负值不属于管理助手的公开聚合事实。
        if not parsed.is_finite() or parsed < 0:
            raise AdminAssistantContractError()
        # 百分比严格限制在零到一百，避免前端显示无意义比例。
        if claim_type == "percentage" and parsed > 100:
            raise AdminAssistantContractError()
        return
    _bounded_public_text(value, maximum=512)


def _validate_source_id_list(values: tuple[str, ...]) -> None:
    """校验来源引用非空、唯一、有界且全部属于 Shared source ID 目录。"""

    # 来源引用必须非空、无重复且数量不超过冻结来源目录。
    if not values or len(values) > len(SOURCE_TYPES) or len(values) != len(set(values)):
        raise AdminAssistantContractError()
    # 每个 ID 必须是未修剪的 exact str，调用方不能依赖隐式清洗。
    if any(type(value) is not str or value != value.strip() for value in values):
        raise AdminAssistantContractError()
    # 引用只能落在 Shared 已知 source_id 中，禁止自由资源标识。
    if any(value not in {f"src_{source_type}" for source_type in SOURCE_TYPES} for value in values):
        raise AdminAssistantContractError()


def _bounded_public_text(value: object, *, maximum: int) -> str:
    """校验有界公开文本，拒绝首尾空白、控制符、Bidi 与 Unicode noncharacter。"""

    # 公开文本必须非空、无需修剪且长度受限，避免隐藏空白改变审计含义。
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise AdminAssistantContractError()
    for character in value:
        codepoint = ord(character)
        if (
            codepoint < 32
            or codepoint == 127
            or codepoint in _BIDI_CONTROLS
            or 0xD800 <= codepoint <= 0xDFFF
            or 0xFDD0 <= codepoint <= 0xFDEF
            or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}
            or unicodedata.category(character) == "Cc"
        ):
            # 任一危险 Unicode 字符都会使整段公开文本失败，不做静默替换。
            raise AdminAssistantContractError()
    return value


def _nonnegative_int(value: object) -> int:
    """接受 JavaScript 可精确表示范围内的非负整数，显式拒绝 bool 与文本。"""

    # 精确整数类型与安全上限共同保证 SQLite、Python 和浏览器 wire 解释完全一致。
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        raise AdminAssistantContractError()
    return value


def _prefixed_id(value: object, prefix: str) -> str:
    """校验固定前缀加三十二位小写 hex 的内部对象 ID。"""

    # 对象 ID 必须同时匹配通用形状和调用方指定前缀，禁止跨实体混用。
    if type(value) is not str or not _OBJECT_ID.fullmatch(value) or not value.startswith(prefix):
        raise AdminAssistantContractError()
    return value


def _utc_second(value: object) -> datetime:
    """把带时区 datetime 规范化为 UTC 秒，naive 与其他类型失败关闭。"""

    # naive datetime 没有可审计时区语义，因此不能被猜测为本地时间。
    if type(value) is not datetime or value.tzinfo is None:
        raise AdminAssistantContractError()
    # 数据库快照可能在当前秒内完成；公开合同只保留 UTC 秒，微秒不参与来源身份或窗口语义。
    return value.astimezone(UTC).replace(microsecond=0)


def _format_utc_second(value: datetime) -> str:
    """输出 Shared wire 唯一接受的规范 UTC 秒字符串，避免多种等价时间表示。"""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc_second(value: object) -> datetime:
    """解析 exact UTC 秒并执行往返校验，拒绝 offset、微秒和宽松日期。"""

    # wire 时间必须显式使用 Z 后缀，offset 与其他类型不做宽松转换。
    if type(value) is not str or not value.endswith("Z"):
        raise AdminAssistantContractError()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    # 宽松日期或非法日历值统一映射为合同错误，不回显原时间文本。
    except ValueError:
        raise AdminAssistantContractError() from None
    # 往返不一致说明输入不是唯一规范形式，必须失败关闭。
    if _format_utc_second(parsed) != value:
        raise AdminAssistantContractError()
    return parsed


def _exact_dict(value: object, keys: set[str]) -> dict[str, object]:
    """要求值为字段集合逐字相等的普通 dict，未知或缺失字段均拒绝。"""

    # 只接受 exact dict 与逐字相等的键集合，未知字段可能承载未审计数据。
    if type(value) is not dict or set(value) != keys:
        raise AdminAssistantContractError()
    return value


def _exact_list(value: object) -> list[object]:
    """要求 JSON 容器为 exact list，不把 tuple 或其他迭代对象宽松转换。"""

    # JSON 数组必须保持 exact list，拒绝 tuple 或惰性迭代器的隐式求值。
    if type(value) is not list:
        raise AdminAssistantContractError()
    return value


def _exact_str(value: object) -> str:
    """要求 JSON 标量为 exact str，不接受数字、bool 或自定义对象转换。"""

    # 标量必须是 exact str，禁止通过 __str__ 把任意对象带入公开合同。
    if type(value) is not str:
        raise AdminAssistantContractError()
    return value
