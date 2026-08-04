"""PRD-070 高危会话报表与处置 API 的安全数据合同。

本模块只暴露风险分数、固定信号计数、冻结覆盖和不可变处置摘要。Prompt、Response、
公开 Session/Turn Header、完整 API 会话 HMAC、命中片段和幂等摘要均不属于公开 DTO。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

SessionRiskScopeType = Literal["enterprise_chat", "api_session"]
SessionRiskStatus = Literal["high_risk", "frozen", "resolved", "expired"]
SessionRiskRuntimeStatus = Literal["disabled", "degraded", "unavailable", "active"]
SessionRiskActionType = Literal[
    "freeze_session",
    "freeze_virtual_key",
    "dismiss",
    "release_session",
    "release_virtual_key",
]
SessionRiskReasonCode = Literal[
    "confirmed_attack",
    "false_positive",
    "verified_safe",
    "incident_contained",
]


class SessionRiskSignalCounts(BaseModel):
    """一条会话风险 cycle 的三类固定累计信号。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    jailbreak: StrictInt = Field(ge=0)
    sensitive: StrictInt = Field(ge=0)
    tool_inducement: StrictInt = Field(ge=0)


class SessionRiskFreezeInfo(BaseModel):
    """独立风险冻结覆盖的可展示摘要。

    target_scope_key 和 Virtual Key 明文不会返回；同一个 Key 可保留多个来源 score 的
    独立覆盖，控制台仅按本 score 展示和释放对应来源。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    session_risk_freeze_id: str
    target_type: Literal["session", "virtual_key"]
    status: Literal["active", "released"]
    state_revision: StrictInt = Field(ge=1)
    created_at: datetime
    released_at: datetime | None


class SessionRiskActionInfo(BaseModel):
    """一次 append-only 风险处置的安全时间线摘要。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    session_risk_action_id: str
    action: Literal[
        "high_risk",
        "freeze_session",
        "freeze_virtual_key",
        "dismiss",
        "release_session",
        "release_virtual_key",
        "expire",
    ]
    reason_code: Literal[
        "threshold_reached",
        "confirmed_attack",
        "false_positive",
        "verified_safe",
        "incident_contained",
        "window_expired",
    ]
    actor_type: Literal["system", "user"]
    actor_reference: str
    session_risk_freeze_id: str | None
    status_before: Literal["monitoring", "high_risk", "frozen", "resolved", "expired"]
    status_after: Literal["monitoring", "high_risk", "frozen", "resolved", "expired"]
    score_snapshot: StrictInt = Field(ge=0, le=10_000)
    state_revision: StrictInt = Field(ge=1)
    source_request_id: str
    created_at: datetime


class SessionRiskInfo(BaseModel):
    """当前租户一条高危会话 cycle 的完整安全投影。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    session_risk_score_id: str
    scope_type: SessionRiskScopeType
    scope_reference: str
    virtual_key_id: str
    status: SessionRiskStatus
    score: StrictInt = Field(ge=0, le=10_000)
    score_scale: Literal[10_000]
    risky_turn_count: StrictInt = Field(ge=1)
    signal_counts: SessionRiskSignalCounts
    state_revision: StrictInt = Field(ge=1)
    threshold_version: str
    last_source_request_id: str
    # 活跃 high-risk 窗口有终点；frozen/resolved 已退出 TTL 状态机，迁移要求显式 NULL。
    window_expires_at: datetime | None
    first_high_risk_at: datetime | None
    frozen_at: datetime | None
    updated_at: datetime
    freezes: list[SessionRiskFreezeInfo]
    actions: list[SessionRiskActionInfo]


class SessionRiskSummary(BaseModel):
    """PAGE-098 顶部使用的当前租户状态计数。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    high_risk_count: StrictInt = Field(ge=0)
    frozen_count: StrictInt = Field(ge=0)
    resolved_count: StrictInt = Field(ge=0)


class SessionRiskListResponse(BaseModel):
    """当前认证租户的高危会话 keyset 分页响应。

    仅返回固定状态摘要与不透明分页游标，不包含租户输入、原始会话标识或完整 API HMAC。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    runtime_status: SessionRiskRuntimeStatus
    summary: SessionRiskSummary
    session_risks: list[SessionRiskInfo]
    next_cursor: str | None
    request_id: str
    trace_id: str


class SessionRiskActionRequest(BaseModel):
    """风险处置严格正文；动作名本身固定目标，客户端不能提交 target ID。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: SessionRiskActionType
    reason_code: SessionRiskReasonCode
    expected_revision: StrictInt = Field(ge=1)


class SessionRiskActionResponse(BaseModel):
    """风险动作提交或幂等恢复后的完整响应。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    session_risk: SessionRiskInfo
    action: SessionRiskActionInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


__all__ = [
    "SessionRiskActionInfo",
    "SessionRiskActionRequest",
    "SessionRiskActionResponse",
    "SessionRiskFreezeInfo",
    "SessionRiskInfo",
    "SessionRiskListResponse",
    "SessionRiskRuntimeStatus",
    "SessionRiskScopeType",
    "SessionRiskSignalCounts",
    "SessionRiskStatus",
    "SessionRiskSummary",
]
