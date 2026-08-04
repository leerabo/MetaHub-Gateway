"""定义 PRD-060 Invitation API 的严格请求与安全响应结构。

请求 DTO 不接受 tenant_id、邮箱之外的用户身份、操作者或状态字段；创建租户来自
AuthContext，接受租户、邮箱和角色只能从数据库 Invitation 恢复。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

InvitationStatus = Literal["pending", "accepted", "expired"]


class CreateInvitationRequest(BaseModel):
    """创建租户邀请的严格请求，只描述管理员本次邀请意图。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    email: str = Field(min_length=3, max_length=255)
    role: str = Field(min_length=1, max_length=40)
    expires_in_hours: int = Field(default=72, ge=1, le=168)


class AcceptInvitationRequest(BaseModel):
    """接受邀请的严格请求，不允许受邀者覆盖租户、邮箱或目标角色。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    display_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=1, max_length=256)


class InvitationInfo(BaseModel):
    """返回给邀请管理员的 tenant-qualified Invitation 安全摘要。"""

    invitation_id: str
    tenant_id: str
    email: str
    role: str
    status: InvitationStatus
    expires_at: datetime
    created_at: datetime


class InvitationCreateResponse(BaseModel):
    """返回创建结果和仅本次可见的原始接受 Token。

    ``accept_token`` 是 bearer credential，调用方只能短暂驻留内存并构造同源 fragment；
    任何列表、日志、审计、事件或后续详情响应都不得再次返回该字段。
    """

    invitation: InvitationInfo
    accept_token: str
    request_id: str
    trace_id: str


class AcceptedInvitationUser(BaseModel):
    """接受成功后的最小用户摘要，不暴露密码哈希、权限矩阵或 Session。"""

    user_id: str
    email: str
    display_name: str
    role: str
    status: Literal["active"] = "active"


class InvitationAcceptResponse(BaseModel):
    """返回一次性邀请接受结果；接受成功不自动签发登录 Session。"""

    accepted: Literal[True] = True
    user: AcceptedInvitationUser
    request_id: str
    trace_id: str
