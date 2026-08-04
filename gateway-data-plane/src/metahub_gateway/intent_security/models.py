"""Gateway 轻量敏感意图分类的不可变运行时模型。

本模块只承载不含正文的审计摘要，以及正文被 ``repr=False`` 隐藏的短生命周期队列任务。
租户、请求和 Virtual Key 身份都来自已认证的 ``RequestLogContext``，客户端字段不得覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from metahub_shared.intent_security import RiskType
from metahub_shared.session_risk import SessionIntentClassification
from metahub_shared.runtime.errors import ApiError

IntentScheduleStatus = Literal[
    "disabled",
    "queued",
    "skipped",
    "blocked",
    "completed",
    "failed_closed",
]
IntentClassificationMode = Literal["async_light", "sync_strong", "sync_session"]


@dataclass(frozen=True, slots=True)
class IntentRiskDecision:
    """保存一次 Gateway 调度或同步闸门的安全摘要。

    该对象会被复制到 ``RequestLogContext``，因此只允许结果 ID、固定枚举、整数分数、
    分类器版本和截断标志；不得增加正文、命中片段、位置、输入 hash 或异常消息。
    """

    intent_risk_result_id: str | None
    schedule_status: IntentScheduleStatus
    classification_mode: IntentClassificationMode
    risk_type: RiskType | None
    risk_score: int | None
    action: Literal["allow", "observe", "block", "none"] | None
    reason_code: str
    detector_version: str
    input_truncated: bool


@dataclass(frozen=True, slots=True)
class IntentClassificationJob:
    """表示进程内 best-effort 完整分类任务。

    ``texts`` 只在当前进程内存中存活并从 ``repr`` 隐藏；其他字段均为服务端冻结的
    tenant-qualified 身份或版本元数据。该任务不具备 durable retry 语义，进程重启后不重放。
    """

    tenant_id: str
    request_id: str
    trace_id: str
    virtual_key_id: str
    intent_risk_result_id: str
    detector_version: str
    ruleset_version: str
    input_truncated: bool
    enqueued_at: datetime
    texts: tuple[str, ...] = field(repr=False)


class IntentSecurityError(ApiError):
    """携带不含正文的意图决策，供最外层失败日志统一落库。"""

    def __init__(
        self,
        code: str,
        decision: IntentRiskDecision,
        *,
        session_classification: SessionIntentClassification | None = None,
    ) -> None:
        """创建固定公开错误，并附带仅供 Gateway 内部使用的安全决策摘要。"""

        super().__init__(code)
        self.intent_risk_decision = decision
        # 双摘要不含正文，仅供 PRD-070 在保留原错误优先级时累计 current turn。
        self.session_classification = session_classification


class IntentBlockedError(IntentSecurityError):
    """表示强规则结果和 source event 已提交后的 Provider 前阻断。"""


class IntentClassifierUnavailableError(IntentSecurityError):
    """表示同步强分类器异常且已按失败关闭处理。"""


class IntentResultStoreUnavailableError(IntentSecurityError):
    """表示同步结果无法提交，审计证据不足时必须失败关闭。"""


__all__ = [
    "IntentBlockedError",
    "IntentClassificationJob",
    "IntentClassificationMode",
    "IntentClassifierUnavailableError",
    "IntentResultStoreUnavailableError",
    "IntentRiskDecision",
    "IntentScheduleStatus",
    "IntentSecurityError",
]
