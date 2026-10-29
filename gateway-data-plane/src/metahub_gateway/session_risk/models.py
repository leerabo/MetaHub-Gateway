"""PRD-070 会话风险热状态的 Gateway 侧不可变模型。

本模块只保存租户限定的资源标识、版本化 HMAC 摘要和固定安全枚举。API Session/Turn
Header、Prompt、Response、Provider Key 与 Token 均不得进入这些对象；调用方必须在构造
模型前完成 HMAC 派生并立即丢弃原始 Header 引用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from metahub_shared.session_risk.models import (
    SessionRiskContractError,
    SessionRiskScope,
    SessionRiskScopeType,
    TurnRiskAssessment,
)

SessionRiskState = Literal["monitoring", "high_risk", "frozen"]

# Score ID 是对外可见的不透明资源标识；严格小写十六进制可避免等价编码和日志注入。
_SCORE_ID_PATTERN = re.compile(r"^srs_[0-9a-f]{32}$")


class SessionRiskStoreUnavailable(RuntimeError):
    """表示 Redis/内存测试 Store 无法证明热状态操作已经完成。"""


class SessionRiskPendingEvidence(SessionRiskStoreUnavailable):
    """表示上一轮 Redis 累计仍有未提交或未确认的持久证据。"""


class SessionRiskTurnConflict(SessionRiskStoreUnavailable):
    """表示同一可信 Turn ID 在保留期内被用于另一份当前轮意图。"""


# Gateway 对外暂时保留旧名字，实际类型唯一来自 Shared，避免 Store 与分类器形成两套合同。
SessionRiskAssessment = TurnRiskAssessment


@dataclass(frozen=True, slots=True)
class SessionRiskSnapshot:
    """表示 Store 中一个活动 score cycle 的非敏感快照。"""

    score_id: str
    score: int
    risky_turn_count: int
    jailbreak_count: int
    sensitive_count: int
    tool_inducement_count: int
    state: SessionRiskState
    revision: int
    window_started_at_epoch: int
    last_risk_at_epoch: int
    expires_at_epoch: int | None
    hmac_key_version: int
    threshold_version: str

    def __post_init__(self) -> None:
        """拒绝损坏、倒退或越界的 Redis 快照。"""

        # 任一字段损坏都拒绝整个快照，不能拿部分可信 Redis 状态继续 Provider 调用。
        if (
            _SCORE_ID_PATTERN.fullmatch(self.score_id) is None
            or type(self.score) is not int
            or not 1 <= self.score <= 10_000
            or type(self.risky_turn_count) is not int
            or self.risky_turn_count < 1
            or any(
                type(value) is not int or value < 0
                for value in (
                    self.jailbreak_count,
                    self.sensitive_count,
                    self.tool_inducement_count,
                )
            )
            or self.state not in {"monitoring", "high_risk", "frozen"}
            or type(self.revision) is not int
            or self.revision < 1
            or type(self.window_started_at_epoch) is not int
            or type(self.last_risk_at_epoch) is not int
            or self.last_risk_at_epoch < self.window_started_at_epoch
            or (
                self.expires_at_epoch is not None
                and (
                    type(self.expires_at_epoch) is not int
                    or self.expires_at_epoch <= self.last_risk_at_epoch
                )
            )
            or (self.state == "frozen" and self.expires_at_epoch is not None)
            or type(self.hmac_key_version) is not int
            or self.hmac_key_version < 1
            or not _fixed_text(self.threshold_version, 128)
        ):
            # 快照字段共同构成 Redis 与数据库对账证据，任一类型或范围漂移都必须整体拒绝。
            raise SessionRiskContractError()


@dataclass(frozen=True, slots=True)
class SessionRiskPending:
    """表示 Redis 中唯一待持久化的风险证据摘要。"""

    scope: SessionRiskScope
    assessment: TurnRiskAssessment
    snapshot: SessionRiskSnapshot
    score_before: int
    state_before: str
    high_risk_transition: bool
    freeze_transition: bool

    def __post_init__(self) -> None:
        """保证 pending 的 before/after 与 revision 关系可由数据库 CAS 复核。"""

        # pending 是 Redis 与数据库之间的清债凭据，before/after 不一致时必须失败关闭。
        if (
            type(self.score_before) is not int
            or not 0 <= self.score_before <= self.snapshot.score
            or self.state_before not in {"none", "monitoring", "high_risk"}
            or type(self.high_risk_transition) is not bool
            or type(self.freeze_transition) is not bool
        ):
            # pending 必须精确描述一次状态跃迁，损坏字段不能通过默认值继续持久化或 ACK。
            raise SessionRiskContractError()


@dataclass(frozen=True, slots=True)
class SessionRiskAccumulateResult:
    """返回一次原子累计结果；duplicate/clean 不会携带待持久证据。"""

    snapshot: SessionRiskSnapshot | None
    pending: SessionRiskPending | None
    duplicate: bool = False


def _fixed_text(value: object, maximum: int) -> bool:
    """校验固定枚举/版本文本，不允许控制字符或无界动态内容。"""

    return (
        type(value) is str
        and 1 <= len(value) <= maximum
        and value.isascii()
        and all(ord(character) >= 32 for character in value)
    )


__all__ = [
    "SessionRiskAccumulateResult",
    "SessionRiskAssessment",
    "SessionRiskContractError",
    "SessionRiskPending",
    "SessionRiskPendingEvidence",
    "SessionRiskScope",
    "SessionRiskScopeType",
    "SessionRiskSnapshot",
    "SessionRiskState",
    "SessionRiskStoreUnavailable",
    "SessionRiskTurnConflict",
]
