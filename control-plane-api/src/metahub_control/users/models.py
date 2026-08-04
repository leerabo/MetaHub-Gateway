"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/users/models.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UserStatus = Literal["active", "disabled"]


class RoleInfo(BaseModel):
    """承载 控制面用户管理 的 RoleInfo 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    code: str
    name: str
    description: str


class UserInfo(BaseModel):
    """承载 控制面用户管理 的 UserInfo 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    user_id: str
    tenant_id: str
    email: str
    display_name: str
    role: str
    status: UserStatus
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None


class UserListResponse(BaseModel):
    """定义 控制面用户管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    users: list[UserInfo]
    roles: list[RoleInfo]
    request_id: str
    trace_id: str


class UserResponse(BaseModel):
    """定义 控制面用户管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    user: UserInfo
    request_id: str
    trace_id: str


class CreateUserRequest(BaseModel):
    # 禁止 tenant_id/password_hash 等字段进入服务层。创建目标租户只能来自 AuthContext。
    """定义 控制面用户管理 的请求 DTO，集中约束 CreateUserRequest 入参字段和默认值。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=160)
    password: str = Field(max_length=256)
    role: str = Field(min_length=1, max_length=40)
    status: str | None = Field(default="active", max_length=40)


class UpdateUserRoleRequest(BaseModel):
    # 只允许修改角色，避免请求体夹带 tenant_id/status 等字段绕过服务层边界。
    """定义 控制面用户管理 的请求 DTO，集中约束 UpdateUserRoleRequest 入参字段和默认值。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=40)
