"""PRD-078 移动审批公开数据合同。

公开 DTO 只承载固定枚举、状态、时间和不透明 ID。预算金额、会话范围摘要、密码、
step-up 摘要、幂等摘要、Webhook Endpoint 与租约字段都不得进入响应。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

ApprovalTargetType = Literal[
    "budget_block_release", "session_risk_action", "mcp_tool_invocation"
]
ApprovalAction = Literal["approve", "reject"]
ApprovalStatus = Literal["pending", "approved", "rejected"]
ApprovalRiskLevel = Literal["standard", "high"]
ApprovalWritebackStatus = Literal["pending", "completed", "failed"]
ApprovalResolutionReason = Literal["approved", "user_rejected", "timeout"]


class ApprovalSummary(BaseModel):
    """列表、详情和动作响应共享的审批安全摘要。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    approval_id: StrictStr
    target_type: ApprovalTargetType
    target_action: StrictStr
    risk_level: ApprovalRiskLevel
    status: ApprovalStatus
    resolution_reason: ApprovalResolutionReason | None
    state_revision: StrictInt = Field(ge=1)
    writeback_status: ApprovalWritebackStatus
    required_permission_code: StrictStr
    requires_step_up: bool
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None


class ApprovalTargetSummary(BaseModel):
    """详情页可见的固定目标引用，不复制目标业务正文。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    target_type: ApprovalTargetType
    target_id: StrictStr
    target_action: StrictStr
    target_version: StrictStr


class ApprovalDecisionInfo(BaseModel):
    """append-only 决策时间线的安全投影。
    ``ApprovalDecisionInfo`` 定义审批公开数据投影，只允许固定字段并排除票据、摘要和业务敏感正文。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    decision_id: StrictStr
    decision: ApprovalAction | Literal["timeout"]
    reason_code: StrictStr
    actor_type: Literal["user", "system"]
    actor_id: StrictStr
    revision_before: StrictInt = Field(ge=1)
    revision_after: StrictInt = Field(ge=2)
    step_up_used: bool
    writeback_status: ApprovalWritebackStatus
    created_at: datetime


class ApprovalListResponse(BaseModel):
    """当前租户、当前目标域权限交集后的审批列表。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    approvals: list[ApprovalSummary]
    next_cursor: str | None
    request_id: StrictStr
    trace_id: StrictStr


class ApprovalDetailResponse(BaseModel):
    """单任务详情、动作能力和决策时间线。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    approval: ApprovalSummary
    target_summary: ApprovalTargetSummary
    allowed_actions: list[ApprovalAction]
    decisions: list[ApprovalDecisionInfo]
    request_id: StrictStr
    trace_id: StrictStr


class ApprovalStepUpRequest(BaseModel):
    """高风险任务换取单次票据的 exact 请求。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: ApprovalAction
    password: StrictStr = Field(min_length=1, max_length=256)


class ApprovalStepUpResponse(BaseModel):
    """只返回一次的 step-up 明文票据。
    ``ApprovalStepUpResponse`` 定义审批公开数据投影，只允许固定字段并排除票据、摘要和业务敏感正文。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    step_up_token: StrictStr
    expires_at: datetime
    request_id: StrictStr
    trace_id: StrictStr


class ApprovalDecisionRequest(BaseModel):
    """批准或拒绝请求；服务层按风险等级约束 ticket 是否出现。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: StrictInt = Field(ge=1)
    reason_code: StrictStr = Field(min_length=1, max_length=64)
    step_up_token: StrictStr | None = Field(default=None, min_length=43, max_length=128)


class ApprovalDecisionResponse(BaseModel):
    """审批终态与本次 append-only 决策事实。
    ``ApprovalDecisionResponse`` 定义审批公开数据投影，只允许固定字段并排除票据、
    摘要和业务敏感正文。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    approval: ApprovalSummary
    decision: ApprovalDecisionInfo
    idempotent_replay: bool
    request_id: StrictStr
    trace_id: StrictStr


class CreateApprovalRequest(BaseModel):
    """有界内部生产者请求；风险、版本和目标权限均由服务端派生。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_type: StrictStr = Field(min_length=1, max_length=64)
    source_id: StrictStr = Field(min_length=1, max_length=160)
    target_type: ApprovalTargetType
    target_id: StrictStr = Field(min_length=1, max_length=160)
    target_action: StrictStr = Field(min_length=1, max_length=64)
    target_reason_code: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    expires_in_seconds: StrictInt = Field(ge=300, le=86_400)
    idempotency_key: StrictStr = Field(min_length=8, max_length=128)


class CreateApprovalResponse(BaseModel):
    """内部创建或精确幂等恢复结果。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    approval: ApprovalSummary
    idempotent_replay: bool
    request_id: StrictStr
    trace_id: StrictStr


__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalDecisionResponse",
    "ApprovalDetailResponse",
    "ApprovalListResponse",
    "ApprovalStepUpRequest",
    "ApprovalStepUpResponse",
    "CreateApprovalRequest",
    "CreateApprovalResponse",
]
