"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/virtual_keys/models.py
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RateLimitRuleInfo(BaseModel):
    """Virtual Key 限流规则的安全响应结构。

    该结构只包含 RPM、TPM、并发和状态等治理元数据，不包含明文 Virtual Key、
    key_hash、Redis Key 或任何请求内容。
    """

    rate_limit_rule_id: str
    rpm_limit: int | None
    tpm_limit: int | None
    concurrency_limit: int | None
    status: str
    updated_at: datetime


class VirtualKeyInfo(BaseModel):
    """承载 Virtual Key 管理 的 VirtualKeyInfo 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    virtual_key_id: str
    tenant_id: str
    name: str
    key_prefix: str
    environment: str
    environment_id: str
    environment_name: str
    environment_status: str
    description: str | None
    project_id: str | None = None
    project_name: str | None = None
    application_id: str | None = None
    application_name: str | None = None
    status: str
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None
    rate_limit_rule: RateLimitRuleInfo | None = None


class VirtualKeyListResponse(BaseModel):
    """定义 Virtual Key 管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    virtual_keys: list[VirtualKeyInfo]
    request_id: str
    trace_id: str


class VirtualKeyResponse(BaseModel):
    """定义 Virtual Key 管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    virtual_key: VirtualKeyInfo
    request_id: str
    trace_id: str


class CreateVirtualKeyRequest(BaseModel):
    # 字段在 service 层做业务化校验，以便返回 virtual_key.* 错误码，而不是框架默认 422。
    """定义 Virtual Key 管理 的请求 DTO，集中约束 CreateVirtualKeyRequest 入参字段和默认值。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    name: str | None = None
    environment_id: str | None = None
    # 未提供 ID 和兼容代码时由服务层解析默认 dev，避免 ID 被 DTO 默认值错误覆盖。
    environment: str | None = None
    description: str | None = None
    project_id: str | None = None
    application_id: str | None = None


class CreateVirtualKeyResponse(VirtualKeyResponse):
    """定义 Virtual Key 管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    plain_value: str


class UpdateVirtualKeyRequest(BaseModel):
    """定义 Virtual Key 管理 的请求 DTO，集中约束 UpdateVirtualKeyRequest 入参字段和默认值。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    name: str | None = None
    environment_id: str | None = None
    environment: str | None = None
    description: str | None = None
    status: str | None = None
    project_id: str | None = None
    application_id: str | None = None


class UpdateVirtualKeyLimitsRequest(BaseModel):
    """Virtual Key 限制设置请求。

    字段使用 object 接收，服务层再归一为业务错误码，避免框架默认 422 绕过
    virtual_key.* 错误码体系；null 表示清除对应限制，字段缺失表示不修改。
    """

    rpm_limit: object | None = None
    tpm_limit: object | None = None
    concurrency_limit: object | None = None
    status: object | None = None
