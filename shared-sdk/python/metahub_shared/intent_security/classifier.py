"""确定性、无网络依赖的轻量敏感意图分类实现。

分类器只处理调用方已严格提取的文本元组。规范化和命中状态仅存在于当前调用栈，返回值
只包含固定安全摘要；规则全部为代码内冻结常量，不允许租户输入动态生成正则。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

from .models import (
    DETECTOR_VERSION,
    MAX_INPUT_CODE_POINTS,
    RISK_PRECEDENCE,
    RISK_SCORE_SCALE,
    RULESET_VERSION,
    IntentClassification,
    IntentSecurityContractError,
    RiskType,
)

ClassifierKind = Literal["strong", "light"]

# 只归一有限分隔符并使用无嵌套量词表达式，保证 20,000 字符输入上的线性有界行为。
_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[\s\-_./\\|:：,，;；'\"`~!！?？]+")

# 安全教育与防护语境会禁用同步强阻断，并降低旁路分数，优先控制企业场景中的误阻断。
_BENIGN_CONTEXT: Final[tuple[str, ...]] = (
    "安全培训",
    "安全教育",
    "检测规则",
    "防护规则",
    "如何检测",
    "如何识别",
    "测试用例",
    "负例",
    "security training",
    "security education",
    "detection rule",
    "defensive example",
    "how to detect",
    "how to prevent",
)


@dataclass(frozen=True, slots=True)
class _Rule:
    """描述一个冻结的目标与动作组合，不保存或返回实际命中片段。"""

    risk_type: RiskType
    reason_code: str
    score: int
    strong: bool
    targets: tuple[str, ...]
    actions: tuple[str, ...]


# 每条强规则都要求敏感目标与明确动作同时出现；弱规则只影响异步观察，不直接阻断模型调用。
_RULES: Final[tuple[_Rule, ...]] = (
    _Rule(
        "credential_exfiltration",
        "explicit_credential_extraction",
        9900,
        True,
        (
            "api key",
            "apikey",
            "access token",
            "secret key",
            "password",
            "密钥",
            "令牌",
            "密码",
            "凭据",
        ),
        (
            "show",
            "give",
            "reveal",
            "print",
            "dump",
            "tell me",
            "output",
            "输出",
            "给我",
            "告诉我",
            "显示",
            "打印",
            "泄露",
            "提取",
        ),
    ),
    _Rule(
        "system_prompt_extraction",
        "explicit_system_prompt_request",
        9800,
        True,
        (
            "system prompt",
            "developer message",
            "hidden instruction",
            "系统提示词",
            "系统指令",
            "开发者消息",
            "隐藏指令",
        ),
        (
            "show",
            "give",
            "reveal",
            "print",
            "dump",
            "repeat",
            "output",
            "输出",
            "给我",
            "告诉我",
            "显示",
            "打印",
            "复述",
            "提取",
        ),
    ),
    _Rule(
        "policy_bypass",
        "explicit_policy_bypass",
        9700,
        True,
        (
            "security policy",
            "safety filter",
            "content filter",
            "guardrail",
            "permission check",
            "安全策略",
            "安全过滤",
            "内容审核",
            "权限校验",
        ),
        (
            "bypass",
            "disable",
            "turn off",
            "skip",
            "remove",
            "绕过",
            "关闭",
            "禁用",
            "跳过",
            "规避",
            "解除",
        ),
    ),
    _Rule(
        "jailbreak",
        "explicit_instruction_override",
        9600,
        True,
        (
            "previous instruction",
            "prior instruction",
            "developer mode",
            "dan mode",
            "先前指令",
            "之前指令",
            "开发者模式",
            "dan 模式",
        ),
        ("ignore", "forget", "override", "enable", "忽略", "无视", "忘掉", "覆盖", "开启", "进入"),
    ),
    _Rule(
        "credential_exfiltration",
        "credential_access_request",
        7600,
        False,
        ("api key", "apikey", "token", "secret", "password", "密钥", "令牌", "密码", "凭据"),
        ("find", "read", "access", "get", "查找", "读取", "访问", "获取"),
    ),
    _Rule(
        "system_prompt_extraction",
        "system_prompt_access_request",
        7400,
        False,
        (
            "system prompt",
            "developer message",
            "hidden instruction",
            "系统提示词",
            "系统指令",
            "开发者消息",
            "隐藏指令",
        ),
        ("find", "read", "access", "get", "查看", "读取", "访问", "获取"),
    ),
)


def classify_strong_intent(texts: tuple[str, ...]) -> IntentClassification:
    """执行 Provider 前的高精度强规则扫描。

    参数：严格提取且合计不超过 20,000 code points 的文本元组。返回：block 或 allow
    摘要。关键约束：防护语境不触发强阻断，且函数不记录、不缓存、不返回正文或命中位置。
    """

    return _classify(texts, kind="strong")


def classify_intent(texts: tuple[str, ...]) -> IntentClassification:
    """执行完整轻量旁路分类。

    参数：严格提取且有界的文本元组。返回：observe 或 allow 摘要。关键约束：即使命中
    强规则，本函数也只给出观察动作，调用方不得据此中断已经开始的 Provider 请求。
    """

    return _classify(texts, kind="light")


def _classify(texts: tuple[str, ...], *, kind: ClassifierKind) -> IntentClassification:
    """校验输入、执行固定规则并按分数和 taxonomy 优先级决胜。"""

    normalized, code_points = _normalize_texts(texts)
    return _classify_normalized(normalized, code_points=code_points, kind=kind)


def _classify_normalized(
    normalized: str,
    *,
    code_points: int,
    kind: ClassifierKind,
) -> IntentClassification:
    """对已在当前调用栈规范化的有界文本执行固定规则。

    该内部入口供 PRD-070 在一次 session 分类编排中复用规范化结果；调用方不得把
    ``normalized`` 保存、记录或导出。公开分类函数仍负责全部类型和长度校验。
    """

    benign_context = any(marker in normalized for marker in _BENIGN_CONTEXT)
    candidates: list[_Rule] = []
    for rule in _RULES:
        # 同步路径只接受标记为 strong 的高精度规则，弱信号绝不能升级成阻断。
        if kind == "strong" and (not rule.strong or benign_context):
            continue
        if _matches(rule, normalized):
            candidates.append(rule)

    if not candidates:
        return _empty_classification(code_points)

    # 先按整数分数，再按冻结 taxonomy 顺序决胜，避免依赖集合或字典遍历顺序。
    precedence = {risk_type: index for index, risk_type in enumerate(RISK_PRECEDENCE)}
    winner = min(candidates, key=lambda rule: (-rule.score, precedence[rule.risk_type]))
    score = winner.score
    if kind == "light" and benign_context:
        # 防护语境只降低旁路置信度，仍保留可追踪观察事实，不制造同步误阻断。
        score = max(1, score - 2500)
    return IntentClassification(
        risk_type=winner.risk_type,
        risk_score=score,
        risk_score_scale=RISK_SCORE_SCALE,
        action="block" if kind == "strong" else "observe",
        reason_code=winner.reason_code,
        detector_version=DETECTOR_VERSION,
        ruleset_version=RULESET_VERSION,
        input_code_points=code_points,
    )


def _normalize_texts(texts: tuple[str, ...]) -> tuple[str, int]:
    """严格校验文本元组并返回仅在调用栈内使用的规范化字符串。"""

    if type(texts) is not tuple or any(type(text) is not str for text in texts):
        # 固定类型拒绝阻止可变容器和自定义字符串对象在分类中执行副作用，错误不能回显元素。
        raise IntentSecurityContractError()
    code_points = sum(len(text) for text in texts)
    if code_points > MAX_INPUT_CODE_POINTS:
        # 在规范化复制前执行硬上限，避免超长 Prompt 放大内存；固定错误不包含正文或实际长度。
        raise IntentSecurityContractError()
    # 空元组与全空白输入按 clean 处理；Control 的非空正文约束由 HTTP 层单独执行。
    joined = " ".join(texts)
    # 企业中文正文通常已经满足 NFKC；先用标准库的只读快路径判断，可避免再次分配并复制
    # 20,000 code points。存在全角字母、兼容字符等变体时仍执行完整 NFKC，兼容合同不变。
    normalized = (
        joined
        if unicodedata.is_normalized("NFKC", joined)
        else unicodedata.normalize("NFKC", joined)
    ).casefold()
    # 纯中文/英文数字正文不可能含受控分隔符；标准库 C 层快路径可避免正则再次扫描 20k 输入。
    # 其他字符仍进入原有白名单正则，emoji 等非分隔符也会按既有语义原样保留。
    if not normalized.isalnum():
        # 表达式自身包含空白且使用 ``+`` 合并连续命中，替换结果天然最多一个空格。
        normalized = _SEPARATOR_RE.sub(" ", normalized).strip()
    return normalized, code_points


def _matches(rule: _Rule, normalized: str) -> bool:
    """判断冻结规则的目标和动作是否同时出现，不返回敏感命中位置。"""

    return any(target in normalized for target in rule.targets) and any(
        action in normalized for action in rule.actions
    )


def _empty_classification(code_points: int) -> IntentClassification:
    """构造固定 clean 摘要，确保 none 分数严格为零。"""

    return IntentClassification(
        risk_type="none",
        risk_score=0,
        risk_score_scale=RISK_SCORE_SCALE,
        action="allow",
        reason_code="no_risk_detected",
        detector_version=DETECTOR_VERSION,
        ruleset_version=RULESET_VERSION,
        input_code_points=code_points,
    )
