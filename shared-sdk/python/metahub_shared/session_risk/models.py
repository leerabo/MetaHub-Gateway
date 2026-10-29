"""多轮会话风险引擎的不可变共享数据合同。

本模块只承载已脱敏的租户 owner、版本化 HMAC 摘要和固定风险摘要。API Session/Turn
Header、Prompt、Response、DLP 命中片段、Provider Key 与 Token 均不得进入这些对象，
避免调用方通过 ``repr``、异常或普通结构化日志泄露企业敏感信息。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Literal

from metahub_shared.intent_security import IntentClassification

SessionRiskScopeType = Literal["enterprise_chat", "api_session"]
DlpRiskAction = Literal["allow", "log_only", "block"]

# 分数、阈值和窗口均是跨 Gateway、Redis、数据库及 Console 的冻结合同；业务层不能按租户改写。
SESSION_RISK_SCORE_SCALE: Final[int] = 10_000
SESSION_RISK_TURN_DELTA_CAP: Final[int] = 4_000
SESSION_RISK_HIGH_SCORE: Final[int] = 7_000
SESSION_RISK_FREEZE_SCORE: Final[int] = 9_000
SESSION_RISK_HIGH_RISKY_TURNS: Final[int] = 2
SESSION_RISK_FREEZE_RISKY_TURNS: Final[int] = 3
SESSION_RISK_WINDOW_SECONDS: Final[int] = 30 * 60
SESSION_RISK_RULESET_VERSION: Final[str] = "session-risk-ruleset-v1"
SESSION_RISK_THRESHOLD_VERSION: Final[str] = "session-risk-threshold-v1"
MAX_TURN_INTENT_UTF8_BYTES: Final[int] = 256 * 1024

# 摘要与链路 ID 只接受规范安全字符；原始 Header 或正文无法伪装成持久风险身份。
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^srh_[0-9a-f]{64}$")
_REQUEST_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
)
_INTENT_RESULT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^irr_[0-9a-f]{32}$")
_CHAT_MESSAGE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^chm_[0-9a-f]{32}$")


class SessionRiskContractError(ValueError):
    """表示调用方违反会话风险共享合同，且异常只暴露固定错误码。

    参数：固定内部错误码。返回值：无。关键约束：错误消息不得拼接 Header、正文、
    HMAC 摘要、租户身份或底层异常，防止统一异常日志扩大敏感数据暴露面。
    """

    def __init__(self, code: str = "session_risk.contract_invalid") -> None:
        """保存稳定错误码，供 Gateway 与 Control 映射各自公开错误合同。"""

        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VersionedHmacDigest:
    """保存一个带密钥版本、但不在 ``repr`` 中展示的域分隔 HMAC。

    ``digest`` 允许进入受约束的 Redis/数据库内部字段，但不得进入 API、Webhook 或普通
    日志；版本用于 current/previous keyring 轮换期间精确恢复既有 cycle。
    """

    key_version: int
    digest: str = field(repr=False)

    def __post_init__(self) -> None:
        """拒绝布尔版本、非规范小写摘要和可产生多义表示的自定义字符串对象。"""

        if (
            type(self.key_version) is not int
            or self.key_version < 1
            or type(self.digest) is not str
            or _DIGEST_PATTERN.fullmatch(self.digest) is None
        ):
            # 摘要损坏属于内部合同错误；异常不能回显实际值或密钥版本候选。
            raise SessionRiskContractError()


@dataclass(frozen=True, slots=True)
class SessionRiskScope:
    """保存租户限定、已去明文的可信会话 owner。

    企业 Chat 的 ``scope_key`` 只能来自服务端 grant 恢复的 ChatSession；公开 API 的
    ``scope_key`` 必须是域分隔 HMAC。``owner_slot`` 始终是 HMAC，供 Redis Cluster
    将 score 与 turn claim 放进同一 slot，绝不能拼入 tenant 或 session 明文。
    """

    tenant_id: str
    virtual_key_id: str
    scope_type: SessionRiskScopeType
    scope_key: str = field(repr=False)
    owner_slot: str = field(repr=False)
    hmac_key_version: int
    chat_session_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """校验 owner、scope 类型和 HMAC 版本之间的不可混用关系。"""

        if (
            not _bounded_owner(self.tenant_id)
            or not _bounded_owner(self.virtual_key_id)
            or self.scope_type not in {"enterprise_chat", "api_session"}
            or type(self.scope_key) is not str
            or not self.scope_key
            or _DIGEST_PATTERN.fullmatch(self.owner_slot) is None
            or type(self.hmac_key_version) is not int
            or self.hmac_key_version < 1
        ):
            # owner 或摘要非法时统一失败，禁止调用方从错误差异探测内部身份格式。
            raise SessionRiskContractError()
        if self.scope_type == "enterprise_chat":
            # 企业 Chat scope 必须逐字绑定同一可信 ChatSession，不能由公开 Header 覆盖。
            if self.chat_session_id != self.scope_key or not _bounded_owner(self.chat_session_id):
                raise SessionRiskContractError()
        elif self.chat_session_id is not None or _DIGEST_PATTERN.fullmatch(self.scope_key) is None:
            # API scope 只保存 HMAC；任何原始 mhs_ Header 或 Chat ID 都必须在这里被拒绝。
            raise SessionRiskContractError()


@dataclass(frozen=True, slots=True)
class SessionIntentClassification:
    """保存一次 session 分类调用产生的历史强闸门与当前轮完整摘要。

    ``strong_guard`` 可阻断携带历史攻击指令的当前请求；只有 ``current_turn`` 能进入
    会话累计，避免客户端重复发送历史消息造成重复加分。对象不保存任何规范化文本。
    """

    strong_guard: IntentClassification
    current_turn: IntentClassification
    input_truncated: bool

    def __post_init__(self) -> None:
        """拒绝可变或动作语义漂移的分类结果。"""

        if (
            type(self.strong_guard) is not IntentClassification
            or type(self.current_turn) is not IntentClassification
            or self.strong_guard.action not in {"allow", "block"}
            or self.current_turn.action not in {"allow", "observe"}
            or type(self.input_truncated) is not bool
        ):
            # 两条摘要的动作空间不同；混用会把异步观察错误升级为同步阻断。
            raise SessionRiskContractError()


@dataclass(frozen=True, slots=True)
class TurnRiskAssessment:
    """保存单个可信 turn 的不可变组合风险摘要。

    参数均为 Shared 固定规则计算后的安全事实。返回对象可整体交给一次 Redis Lua 累计，
    但不能按信号拆成多次写入；digest 使用 ``repr=False``，禁止日志关联客户会话。
    """

    turn_key_digest: str = field(repr=False)
    turn_intent_digest: str = field(repr=False)
    jailbreak_count_delta: int
    sensitive_count_delta: int
    tool_inducement_count_delta: int
    turn_delta: int
    input_truncated: bool
    detector_version: str
    ruleset_version: str
    threshold_version: str
    source_request_id: str
    source_trace_id: str
    intent_risk_result_id: str | None = None
    intent_risk_type: str = "none"
    intent_risk_score: int = 0
    dlp_action: DlpRiskAction = "allow"
    dlp_current_turn_match_count: int = 0
    tool_inducement_detected: bool = False
    chat_message_id: str | None = None

    def __post_init__(self) -> None:
        """执行与持久层/Lua 相同的摘要边界，防止宽松测试对象掩盖生产拒绝。"""

        category_deltas = (
            self.jailbreak_count_delta,
            self.sensitive_count_delta,
            self.tool_inducement_count_delta,
        )
        if (
            _DIGEST_PATTERN.fullmatch(self.turn_key_digest) is None
            or _DIGEST_PATTERN.fullmatch(self.turn_intent_digest) is None
            or any(type(value) is not int or value not in {0, 1} for value in category_deltas)
            or type(self.turn_delta) is not int
            or not 0 <= self.turn_delta <= SESSION_RISK_TURN_DELTA_CAP
            or (self.turn_delta == 0 and any(category_deltas))
            or (self.turn_delta > 0 and not any(category_deltas))
            or type(self.input_truncated) is not bool
            or not _bounded_ascii(self.detector_version, maximum=128)
            or not _bounded_ascii(self.ruleset_version, maximum=128)
            or self.threshold_version != SESSION_RISK_THRESHOLD_VERSION
            or _REQUEST_ID_PATTERN.fullmatch(self.source_request_id) is None
            or _REQUEST_ID_PATTERN.fullmatch(self.source_trace_id) is None
            or type(self.intent_risk_score) is not int
            or not 0 <= self.intent_risk_score <= SESSION_RISK_SCORE_SCALE
            or self.dlp_action not in {"allow", "log_only", "block"}
            or type(self.dlp_current_turn_match_count) is not int
            or not 0 <= self.dlp_current_turn_match_count <= 100
            or type(self.tool_inducement_detected) is not bool
        ):
            # 任一字段漂移都折叠为固定合同错误，不能把摘要或来源链路拼入异常。
            raise SessionRiskContractError()
        if self.intent_risk_result_id is not None and (
            type(self.intent_risk_result_id) is not str
            or _INTENT_RESULT_ID_PATTERN.fullmatch(self.intent_risk_result_id) is None
        ):
            # Intent 结果只能引用 PRD-069 服务端 ID，不能用正文 hash 冒充关联身份。
            raise SessionRiskContractError()
        if self.chat_message_id is not None and (
            type(self.chat_message_id) is not str
            or _CHAT_MESSAGE_ID_PATTERN.fullmatch(self.chat_message_id) is None
        ):
            # Chat turn 必须引用服务端 Message ID；API turn 保持为空并只使用 HMAC digest。
            raise SessionRiskContractError()


def _bounded_owner(value: object) -> bool:
    """校验服务端 owner ID 为非空有界安全文本，不接受控制字符或子类副作用。"""

    return (
        type(value) is str
        and 1 <= len(value) <= 160
        and all(ord(character) >= 32 and not character.isspace() for character in value)
    )


def _bounded_ascii(value: object, *, maximum: int) -> bool:
    """校验版本/固定枚举为有界可打印 ASCII，避免动态正文进入安全摘要。"""

    return (
        type(value) is str
        and 1 <= len(value) <= maximum
        and value.isascii()
        and all(ord(character) >= 32 for character in value)
    )


__all__ = [
    "DlpRiskAction",
    "MAX_TURN_INTENT_UTF8_BYTES",
    "SESSION_RISK_FREEZE_RISKY_TURNS",
    "SESSION_RISK_FREEZE_SCORE",
    "SESSION_RISK_HIGH_RISKY_TURNS",
    "SESSION_RISK_HIGH_SCORE",
    "SESSION_RISK_RULESET_VERSION",
    "SESSION_RISK_SCORE_SCALE",
    "SESSION_RISK_THRESHOLD_VERSION",
    "SESSION_RISK_TURN_DELTA_CAP",
    "SESSION_RISK_WINDOW_SECONDS",
    "SessionIntentClassification",
    "SessionRiskContractError",
    "SessionRiskScope",
    "SessionRiskScopeType",
    "TurnRiskAssessment",
    "VersionedHmacDigest",
]
