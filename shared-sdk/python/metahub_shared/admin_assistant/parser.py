"""PRD-077 有界自然语言意图解析与只读动作闸门。"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from .contracts import AdminAssistantContractError, ParsedAssistantQuery, QueryType

# 问题字符/字节上限、危险 Unicode、只读动作、意图与窗口模式共同定义解析器的有限输入面。
MAX_QUESTION_CODE_POINTS: Final[int] = 2_000
MAX_QUESTION_UTF8_BYTES: Final[int] = 8 * 1024

_BIDI_CONTROLS = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)})
_REQUEST_ID = re.compile(
    r"(?<![A-Za-z0-9._:-])"
    r"(req_[A-Za-z0-9][A-Za-z0-9._:-]{0,155})"
    r"(?![A-Za-z0-9._:-])"
)
_WRITE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"创建|新增|添加|修改|更新|删除|移除|禁用|启用|发布|回滚|审批|批准|调整预算|提高额度|降低额度|重放|执行|调用工具",
        r"\b(?:create|add|modify|update|edit|delete|remove|disable|enable|publish|rollback|approve|revoke|replay|execute|run|write|set|change)\b",
        r"\b(?:increase|decrease|adjust)\s+(?:the\s+)?(?:budget|limit|quota)\b",
        r"\bcall\s+(?:a\s+)?tool\b",
    )
)
_CALENDAR_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"今天|昨日|昨天|明天|本周|这周|本月|这个月",
        r"\b(?:today|yesterday|tomorrow|this\s+week|current\s+week|this\s+month|current\s+month)\b",
    )
)
_INTENT_PATTERNS: Final[dict[QueryType, tuple[re.Pattern[str], ...]]] = {
    "usage_statistics": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"用量|使用量|调用量|调用情况|请求量|请求次数|成功率|吞吐|令牌用量",
            r"\b(?:usage|request\s+volume|call\s+volume|request\s+count|call\s+count|success\s+rate|tokens?\s+used)\b",
        )
    ),
    "error_explanation": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"错误|报错|失败原因|异常原因|为什么失败|故障解释",
            r"\b(?:error|errors|failure|failures|failed|exception|why\s+did\s+.*fail)\b",
        )
    ),
    "cost_advice": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"成本|费用|花费|账单|预算建议|省钱|优化开销",
            r"\b(?:cost|costs|spend|spending|expense|expenses|billing|budget\s+advice|save\s+money)\b",
        )
    ),
    "policy_explanation": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"策略|规则|策略冲突|路由决策|治理决定|为什么被拦截",
            r"\b(?:policy|policies|rule|rules|policy\s+conflict|routing\s+decision|governance\s+decision)\b",
        )
    ),
}
_WINDOW_PATTERNS: Final[dict[int, tuple[re.Pattern[str], ...]]] = {
    24: tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"(?:最近|过去|近)?\s*(?:24\s*小时|24h|一天|1\s*天)",
            r"\b(?:last|past)\s+(?:24\s+hours?|one\s+day|1\s+day)\b",
            r"\b24h\b",
        )
    ),
    168: tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"(?:最近|过去|近)?\s*(?:7\s*天|七天|一周|1\s*周)",
            r"\b(?:last|past)\s+(?:7\s+days?|one\s+week|1\s+week)\b",
            r"\b7d\b",
        )
    ),
    720: tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"(?:最近|过去|近)?\s*(?:30\s*天|三十天)",
            r"\b(?:last|past)\s+30\s+days?\b",
            r"\b30d\b",
        )
    ),
}


def parse_assistant_query(question: object) -> ParsedAssistantQuery:
    """把单轮问题归类为四种只读意图，异常只携带固定错误码。"""

    # 问题必须是非空 exact str，禁止其他对象通过字符串转换执行自定义逻辑。
    if type(question) is not str or not question:
        raise AdminAssistantContractError("admin_assistant.request_invalid")
    _validate_unicode(question)
    normalized = unicodedata.normalize("NFKC", question)
    _validate_unicode(normalized)
    # NFKC 后为空说明没有可解析内容，不能落入默认意图或默认窗口。
    if not normalized.strip():
        raise AdminAssistantContractError("admin_assistant.request_invalid")

    # 禁止动作在 intent 识别前执行，避免“修改策略并解释成本”等混合输入降级成只读查询。
    if any(pattern.search(normalized) for pattern in _WRITE_PATTERNS):
        raise AdminAssistantContractError("admin_assistant.read_only_violation")
    # 相对日历词依赖时区和日期边界，本期没有可靠语义，必须要求固定小时窗口。
    if any(pattern.search(normalized) for pattern in _CALENDAR_PATTERNS):
        raise AdminAssistantContractError("admin_assistant.query_ambiguous")

    intents = [
        query_type
        for query_type, patterns in _INTENT_PATTERNS.items()
        if any(pattern.search(normalized) for pattern in patterns)
    ]
    # 没有命中有限意图时不得猜测查询域，返回固定“不支持”错误。
    if not intents:
        raise AdminAssistantContractError("admin_assistant.query_unsupported")
    # 同时命中多个业务域会跨越权限和数据来源边界，必须由用户重新表述。
    if len(intents) != 1:
        raise AdminAssistantContractError("admin_assistant.query_ambiguous")

    windows = {
        hours
        for hours, patterns in _WINDOW_PATTERNS.items()
        if any(pattern.search(normalized) for pattern in patterns)
    }
    # 一个问题出现多个时间窗时不能自行选取，否则审计范围与用户预期不一致。
    if len(windows) > 1:
        raise AdminAssistantContractError("admin_assistant.query_ambiguous")
    window_hours = next(iter(windows), 168)

    request_ids = _REQUEST_ID.findall(normalized)
    # 出现 req_ 标记但没有形成规范 ID 时不能静默退化为窗口聚合，避免扩大用户原本指定的范围。
    if normalized.count("req_") != len(request_ids):
        raise AdminAssistantContractError("admin_assistant.request_invalid")
    # 单轮查询只允许锁定一个 request_id，多个 ID 不能被合并或任选其一。
    if len(request_ids) > 1:
        raise AdminAssistantContractError("admin_assistant.query_ambiguous")
    target_request_id = request_ids[0] if request_ids else None
    query_type = intents[0]
    if target_request_id is not None and query_type not in {
        "error_explanation",
        "policy_explanation",
    }:
        # 指定请求只对错误和策略解释有确定读取语义，其他意图拒绝扩大范围。
        raise AdminAssistantContractError("admin_assistant.query_ambiguous")
    return ParsedAssistantQuery(
        query_type=query_type,
        window_hours=window_hours,  # type: ignore[arg-type]
        target_request_id=target_request_id,
    )


def _validate_unicode(value: str) -> None:
    """校验问题的字符数、UTF-8 字节数和危险码点，且不在异常中回显正文。"""

    # 字符上限先于编码执行，避免超长输入消耗不受控内存。
    if len(value) > MAX_QUESTION_CODE_POINTS:
        raise AdminAssistantContractError("admin_assistant.request_invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    # 孤立代理项无法安全记录或跨服务传输，统一返回固定错误码。
    except UnicodeEncodeError:
        raise AdminAssistantContractError("admin_assistant.request_invalid") from None
    # UTF-8 字节上限与 HTTP wire 限制一致，防止多字节文本绕过字符数约束。
    if len(encoded) > MAX_QUESTION_UTF8_BYTES:
        raise AdminAssistantContractError("admin_assistant.request_invalid")
    for character in value:
        codepoint = ord(character)
        category = unicodedata.category(character)
        # 控制符、Bidi 格式符和 Unicode noncharacter 都可能改变审计/展示解释，统一拒绝。
        if (
            codepoint in _BIDI_CONTROLS
            or 0xD800 <= codepoint <= 0xDFFF
            or 0xFDD0 <= codepoint <= 0xFDEF
            or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}
            or (category == "Cc" and character not in {"\n", "\t"})
        ):
            # 危险码点不做替换，以免审计问题与用户原输入产生不可见差异。
            raise AdminAssistantContractError("admin_assistant.request_invalid")
