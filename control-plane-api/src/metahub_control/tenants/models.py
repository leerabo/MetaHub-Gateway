"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/tenants/models.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ServiceMode = Literal["internal_only", "public_service"]
TenantStatus = Literal["active", "disabled"]


class TenantInfo(BaseModel):
    """承载 控制面租户资料 的 TenantInfo 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    tenant_id: str
    slug: str
    name: str
    service_mode: ServiceMode
    status: TenantStatus
    created_at: datetime
    updated_at: datetime


class TenantCurrentResponse(BaseModel):
    """定义 控制面租户资料 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    tenant: TenantInfo
    request_id: str
    trace_id: str


class TenantUpdateRequest(BaseModel):
    # 禁止 tenant_id/slug/status 等额外字段进入服务层，避免前端尝试指定跨租户目标。
    """定义 控制面租户资料 的请求 DTO，集中约束 TenantUpdateRequest 入参字段和默认值。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    service_mode: ServiceMode | None = None
