"""多轮会话当前 turn 的确定性分类、工具诱导检测与组合计分。

实现只使用代码内冻结常量和纯函数，不访问网络、数据库或租户动态正则。调用方传入的
正文只在当前调用栈中形成最多 20,000 code points 的 head/tail 样本；返回对象仅包含
整数分数、固定版本和域分隔 HMAC 摘要，不保存正文、片段、位置或裸 hash。
"""

from __future__ import annotations

from typing import Final

from metahub_shared.intent_security.classifier import (
    _classify_normalized,
    _normalize_texts,
)
from metahub_shared.intent_security.models import (
    MAX_INPUT_CODE_POINTS,
    IntentClassification,
    IntentSecurityContractError,
)

from .models import (
    SESSION_RISK_RULESET_VERSION,
    SESSION_RISK_THRESHOLD_VERSION,
    SESSION_RISK_TURN_DELTA_CAP,
    DlpRiskAction,
    SessionIntentClassification,
    SessionRiskContractError,
    TurnRiskAssessment,
)

# 固定工具诱导规则必须同时出现目标、执行动作与绕过授权语义；任一单独弱信号都不计分。
_TOOL_TARGETS: Final[tuple[str, ...]] = (
    "external tool",
    "system command",
    "shell command",
    "powershell",
    "terminal",
    "plugin",
    "function",
    "tool",
    "外部工具",
    "系统命令",
    "终端命令",
    "插件",
    "函数",
    "工具",
)
_TOOL_ACTIONS: Final[tuple[str, ...]] = (
    "execute",
    "invoke",
    "call",
    "run",
    "执行",
    "调用",
    "运行",
)
_TOOL_BYPASS_MARKERS: Final[tuple[str, ...]] = (
    "without authorization",
    "without permission",
    "without approval",
    "bypass authorization",
    "bypass permission",
    "skip approval",
    "skip confirmation",
    "ignore permission",
    "disable permission",
    "未经授权",
    "无需授权",
    "无需权限",
    "绕过授权",
    "绕过权限",
    "跳过审批",
    "跳过确认",
    "忽略权限",
    "关闭权限",
)

# 防护说明与明确否定必须优先于正向组合，避免安全团队编写规则或培训材料时被累计误杀。
_TOOL_BENIGN_CONTEXTS: Final[tuple[str, ...]] = (
    "security training",
    "security education",
    "detection rule",
    "defensive example",
    "how to detect",
    "how to prevent",
    "安全培训",
    "安全教育",
    "检测规则",
    "防护规则",
    "如何检测",
    "如何防止",
    "测试用例",
    "负例",
)
_TOOL_NEGATIONS: Final[tuple[str, ...]] = (
    "do not bypass",
    "don't bypass",
    "must not bypass",
    "never bypass",
    "do not execute without",
    "禁止绕过",
    "不要绕过",
    "不得绕过",
    "不能绕过",
    "禁止未经授权",
    "不得未经授权",
)

# 信号权重使用整数基点并由单 turn cap 收敛；租户、请求或远程模型不能覆盖这些值。
_INTENT_STRONG_DELTA: Final[int] = 3_600
_INTENT_HIGH_DELTA: Final[int] = 2_600
_INTENT_REVIEW_DELTA: Final[int] = 1_800
_DLP_BLOCK_DELTA: Final[int] = 3_000
_DLP_LOG_ONLY_DELTA: Final[int] = 1_200
_TOOL_INDUCEMENT_DELTA: Final[int] = 2_400


def classify_session_intent(user_texts: tuple[str, ...]) -> SessionIntentClassification:
    """一次返回全部 user 历史的 strong guard 与当前 user turn 的轻分类。

    参数：按消息顺序排列的严格字符串元组，只包含 user 文本投影。返回：不可变双摘要。
    关键约束：历史只用于同步强闸门，当前最后一项才用于累计；超长输入使用 head/tail
    各半样本并标记 truncated，不因客户端重复携带历史而重复计算当前风险。
    """

    history_sample, history_truncated = _bounded_text_sample(user_texts)
    current_source = () if not user_texts else (user_texts[-1],)
    current_sample, current_truncated = _bounded_text_sample(current_source)

    history_normalized, history_points = _normalize_texts((history_sample,))
    if len(user_texts) == 1 and history_sample == current_sample:
        # 单 turn 是主要路径；复用同一规范化结果可保证只扫描正文一次并降低 Provider 前延迟。
        current_normalized, current_points = history_normalized, history_points
    else:
        # 多轮只额外扫描最后一条 current turn；完整历史不会被第二次扫描或进入累计。
        current_normalized, current_points = _normalize_texts((current_sample,))
    strong_guard = _classify_normalized(
        history_normalized,
        code_points=history_points,
        kind="strong",
    )
    current_turn = _classify_normalized(
        current_normalized,
        code_points=current_points,
        kind="light",
    )
    # 只返回固定分类摘要和截断标志，规范化正文必须随函数退出释放且不得进入日志。
    return SessionIntentClassification(
        strong_guard=strong_guard,
        current_turn=current_turn,
        input_truncated=history_truncated or current_truncated,
    )


def detect_tool_inducement(texts: tuple[str, ...]) -> bool:
    """检测当前 turn 是否明确要求绕过授权执行工具。

    参数：当前 user turn 的严格文本元组。返回：固定布尔信号。关键约束：目标、执行动作、
    绕过语义三者必须同时存在；安全培训、规则说明与明确否定优先返回 false。
    """

    sample, _ = _bounded_text_sample(texts)
    normalized, _ = _normalize_texts((sample,))
    if any(marker in normalized for marker in _TOOL_BENIGN_CONTEXTS):
        # 防护场景会引用完整攻击短语，必须先于正向匹配排除，降低安全团队误杀。
        return False
    if any(marker in normalized for marker in _TOOL_NEGATIONS):
        # 明确禁止绕过不代表用户正在诱导执行；否定语义优先且不累计风险。
        return False
    return (
        any(target in normalized for target in _TOOL_TARGETS)
        and any(action in normalized for action in _TOOL_ACTIONS)
        and any(marker in normalized for marker in _TOOL_BYPASS_MARKERS)
    )


def build_turn_risk_assessment(
    *,
    turn_key_digest: str,
    turn_intent_digest: str,
    intent: IntentClassification,
    dlp_action: DlpRiskAction,
    dlp_current_turn_match_count: int,
    tool_inducement_detected: bool,
    input_truncated: bool,
    source_request_id: str,
    source_trace_id: str,
    intent_risk_result_id: str | None = None,
    chat_message_id: str | None = None,
) -> TurnRiskAssessment:
    """把当前 turn 的 Intent、DLP 与工具信号合成一次原子 assessment。

    参数：版本化 turn 摘要、PRD-069 当前轮分类、安全 DLP 摘要、工具布尔及来源链路。
    返回：不可变 ``TurnRiskAssessment``。关键约束：权重只取冻结整数，组合 delta 上限
    4000；历史 intent 与 DLP 历史行不得传入本函数。
    """

    # 必须使用 Shared 冻结结构，禁止相似 Mapping 绕过类型和枚举不变量。
    if type(intent) is not IntentClassification:
        raise SessionRiskContractError()
    if (
        dlp_action not in {"allow", "log_only", "block"}
        or type(dlp_current_turn_match_count) is not int
        or not 0 <= dlp_current_turn_match_count <= 100
        or type(tool_inducement_detected) is not bool
        or type(input_truncated) is not bool
    ):
        # 宽松 bool/int 或未知 DLP 动作会导致 Python 与 Lua 计分不一致，必须先失败关闭。
        raise SessionRiskContractError()
    if dlp_action in {"log_only", "block"} and dlp_current_turn_match_count < 1:
        # 只有当前 turn 存在持久 DLP 命中时才能加分，禁止凭动作枚举伪造敏感计数。
        raise SessionRiskContractError()
    if dlp_action == "allow" and dlp_current_turn_match_count != 0:
        # allow 不能同时声称有阻断/记录命中，否则持久 observation 无法复核来源。
        raise SessionRiskContractError()

    intent_delta = _intent_delta(intent)
    dlp_delta = {
        "allow": 0,
        "log_only": _DLP_LOG_ONLY_DELTA,
        "block": _DLP_BLOCK_DELTA,
    }[dlp_action]
    tool_delta = _TOOL_INDUCEMENT_DELTA if tool_inducement_detected else 0
    turn_delta = min(
        SESSION_RISK_TURN_DELTA_CAP,
        intent_delta + dlp_delta + tool_delta,
    )

    # jailbreak_count 代表意图类风险 turn；sensitive_count 合并 DLP 与凭据提取且每轮最多加一。
    jailbreak_delta = 1 if intent_delta > 0 else 0
    sensitive_delta = 1 if (
        dlp_delta > 0 or (intent_delta > 0 and intent.risk_type == "credential_exfiltration")
    ) else 0
    return TurnRiskAssessment(
        turn_key_digest=turn_key_digest,
        turn_intent_digest=turn_intent_digest,
        jailbreak_count_delta=jailbreak_delta,
        sensitive_count_delta=sensitive_delta,
        tool_inducement_count_delta=1 if tool_inducement_detected else 0,
        turn_delta=turn_delta,
        input_truncated=input_truncated,
        detector_version=intent.detector_version,
        ruleset_version=intent.ruleset_version,
        threshold_version=SESSION_RISK_THRESHOLD_VERSION,
        source_request_id=source_request_id,
        source_trace_id=source_trace_id,
        intent_risk_result_id=intent_risk_result_id,
        intent_risk_type=intent.risk_type,
        intent_risk_score=intent.risk_score,
        dlp_action=dlp_action,
        dlp_current_turn_match_count=dlp_current_turn_match_count,
        tool_inducement_detected=tool_inducement_detected,
        chat_message_id=chat_message_id,
    )


def _intent_delta(intent: IntentClassification) -> int:
    """把 PRD-069 当前轮整数分数映射为冻结的三档 session delta。"""

    if intent.risk_type == "none":
        # clean 摘要必须是零分；漂移结果不能被当成无风险继续 Provider。
        if intent.risk_score != 0:
            raise SessionRiskContractError()
        return 0
    if intent.risk_score >= 9_500:
        return _INTENT_STRONG_DELTA
    if intent.risk_score >= 8_000:
        return _INTENT_HIGH_DELTA
    if intent.risk_score >= 6_500:
        return _INTENT_REVIEW_DELTA
    return 0


def _bounded_text_sample(texts: tuple[str, ...]) -> tuple[str, bool]:
    """严格校验文本元组并构造最多 20,000 code points 的确定性 head/tail 样本。

    函数不会先拼接无界全文；超限时分别从逻辑 ``" ".join(texts)`` 的首尾收集一半，
    从而限制 Provider 前额外内存，同时保留攻击者可能放在开头或结尾的风险短语。
    """

    if type(texts) is not tuple or any(type(text) is not str for text in texts):
        # 自定义字符串或可变容器可能在迭代/规范化时执行副作用，使用 PRD-069 固定错误。
        raise IntentSecurityContractError()
    logical_length = sum(len(text) for text in texts) + max(0, len(texts) - 1)
    if logical_length <= MAX_INPUT_CODE_POINTS:
        return " ".join(texts), False
    half = MAX_INPUT_CODE_POINTS // 2
    prefix = _take_joined_prefix(texts, half)
    suffix = _take_joined_suffix(texts, half)
    return prefix + suffix, True


def _take_joined_prefix(texts: tuple[str, ...], limit: int) -> str:
    """从逻辑空格连接文本头部收集固定 code points，不构造完整中间字符串。"""

    pieces: list[str] = []
    remaining = limit
    for index, text in enumerate(texts):
        if index and remaining:
            pieces.append(" ")
            remaining -= 1
        if not remaining:
            break
        piece = text[:remaining]
        pieces.append(piece)
        remaining -= len(piece)
    return "".join(pieces)


def _take_joined_suffix(texts: tuple[str, ...], limit: int) -> str:
    """从逻辑空格连接文本尾部逆向收集固定 code points，再恢复原顺序。"""

    pieces: list[str] = []
    remaining = limit
    for reverse_index, text in enumerate(reversed(texts)):
        if reverse_index and remaining:
            pieces.append(" ")
            remaining -= 1
        if not remaining:
            break
        piece = text[-remaining:]
        pieces.append(piece)
        remaining -= len(piece)
    return "".join(reversed(pieces))


__all__ = [
    "SESSION_RISK_RULESET_VERSION",
    "build_turn_risk_assessment",
    "classify_session_intent",
    "detect_tool_inducement",
]
