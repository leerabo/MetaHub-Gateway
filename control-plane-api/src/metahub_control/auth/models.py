"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/auth/models.py
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """控制台登录请求体。

    tenant_slug 用于选择租户登录入口；真正的 tenant_id 只能由认证服务查询得到，
    不能由调用方直接提交。
    """

    tenant_slug: str | None = Field(default=None, max_length=80)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class AuthUser(BaseModel):
    """认证后返回的用户安全摘要。"""

    user_id: str
    tenant_id: str
    email: str
    display_name: str
    role: str
    # 使用不可变元组避免服务层在一次请求处理中意外扩大授权；JSON 响应仍编码为数组。
    permission_codes: tuple[str, ...] = ()
    # revision 为租户内角色矩阵版本，前端只能用于发现变化，不能把它作为授权依据。
    permission_revision: int = Field(default=0, ge=0)
    # 指纹只由可信身份和矩阵事实计算，不包含 Token、邮箱或其他敏感正文。
    authorization_fingerprint: str = ""


class AuthTenant(BaseModel):
    """认证后返回的租户安全摘要。"""

    tenant_id: str
    slug: str
    name: str


class AuthSession(BaseModel):
    """承载 控制面认证与会话 的 AuthSession 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    session_id: str
    expires_at: datetime


class LoginResponse(BaseModel):
    """登录成功响应。

    access_token 只返回给控制台本次会话使用；所有响应都带 request_id/trace_id，
    方便登录失败或权限问题排障。
    """

    access_token: str
    token_type: str = "Bearer"
    expires_at: datetime
    session_id: str
    user: AuthUser
    # 顶层镜像字段兼容无需解包 user 的旧控制台调用方，值必须与 user 内字段完全一致。
    permission_codes: tuple[str, ...] = ()
    permission_revision: int = Field(default=0, ge=0)
    authorization_fingerprint: str = ""
    request_id: str
    trace_id: str


class MeResponse(BaseModel):
    """定义 控制面认证与会话 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    user: AuthUser
    tenant: AuthTenant
    session: AuthSession
    # 每次 /auth/me 都返回本次数据库鉴权快照，便于长生命周期页面及时收敛降权状态。
    permission_codes: tuple[str, ...] = ()
    permission_revision: int = Field(default=0, ge=0)
    authorization_fingerprint: str = ""
    request_id: str
    trace_id: str


class LogoutResponse(BaseModel):
    """定义 控制面认证与会话 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    logged_out: bool
    request_id: str
    trace_id: str
