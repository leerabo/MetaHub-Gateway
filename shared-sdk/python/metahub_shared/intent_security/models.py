"""轻量敏感意图分类器的不可变共享合同。

本模块只定义固定枚举、版本和纯数据结构，不接收租户、数据库或网络对象。分类输入正文
绝不能进入返回对象、异常文本或 ``repr``，避免调用方在日志中意外泄露企业 Prompt。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

RiskType = Literal[
    "none",
    "credential_exfiltration",
    "system_prompt_extraction",
    "policy_bypass",
    "jailbreak",
]
IntentAction = Literal["allow", "observe", "block"]

# 算法与规则目录分别版本化，历史结果必须显示落库版本，不能用当前代码重新解释。
DETECTOR_VERSION: Final[str] = "intent-rule-engine-v1"
RULESET_VERSION: Final[str] = "intent-rules-2026-08-03-v1"
RISK_SCORE_SCALE: Final[int] = 10_000
MAX_INPUT_CODE_POINTS: Final[int] = 20_000

# 固定顺序既是多类同分决胜合同，也是序列化和测试的稳定顺序。
RISK_PRECEDENCE: Final[tuple[RiskType, ...]] = (
    "credential_exfiltration",
    "system_prompt_extraction",
    "policy_bypass",
    "jailbreak",
    "none",
)


class IntentSecurityContractError(ValueError):
    """表示调用方违反分类输入边界，且只暴露固定错误码。

    参数：固定错误码。返回值：无。关键约束：错误文本不得拼接正文、命中词、位置或
    规范化结果，防止框架自动日志扩大敏感输入暴露面。
    """

    def __init__(self, code: str = "intent_risk.text_invalid") -> None:
        """保存稳定错误码，供 Control 与 Gateway 映射各自的公开错误合同。"""

        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class IntentClassification:
    """保存不含正文和命中证据的确定性分类摘要。

    ``risk_score`` 使用 0..10000 整数基点，避免跨 Python/JavaScript 的浮点差异；版本、
    原因码和输入长度用于追踪历史决策，但不得包含命中片段、位置或输入哈希。
    """

    risk_type: RiskType
    risk_score: int
    risk_score_scale: int
    action: IntentAction
    reason_code: str
    detector_version: str
    ruleset_version: str
    input_code_points: int

    @property
    def detected(self) -> bool:
        """返回是否命中固定风险类型，不依据可变阈值重新计算。"""

        return self.risk_type != "none"
