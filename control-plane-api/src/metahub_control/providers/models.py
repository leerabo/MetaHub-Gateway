"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/providers/models.py
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProviderInfo(BaseModel):
    """Provider Endpoint 的对外展示信息。

    这里只包含 Endpoint 元数据和健康状态，不包含 Provider Key 明文或加密密文。
    """

    provider_id: str
    tenant_id: str
    provider_code: str
    name: str
    provider_type: str
    base_url: str
    auth_type: str
    status: str
    description: str | None
    # 维护备注是状态页读者可见的短文本，与仅供管理员使用的 description 分离。
    maintenance_note: str | None
    health_status: str
    last_tested_at: datetime | None
    last_test_error: str | None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None


class ProviderListResponse(BaseModel):
    """定义 Provider 与 KeyPool 管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    providers: list[ProviderInfo]
    request_id: str
    trace_id: str


class ProviderResponse(BaseModel):
    """定义 Provider 与 KeyPool 管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    provider: ProviderInfo
    request_id: str
    trace_id: str


class ProviderDeleteResponse(BaseModel):
    """删除未被业务事实引用的 Provider 后返回的安全确认。"""

    deleted_provider_id: str
    request_id: str
    trace_id: str


class ProviderKeyInfo(BaseModel):
    """Provider KeyPool 的脱敏展示信息。

    key_prefix/key_fingerprint 用于排障和轮换确认，永远不返回 plain_value 或 encrypted_key。
    """

    provider_key_id: str
    tenant_id: str
    provider_id: str
    name: str
    key_prefix: str
    key_fingerprint: str
    weight: int
    status: str
    last_used_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    rate_limited_until: datetime | None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None


class ProviderKeyListResponse(BaseModel):
    """定义 Provider 与 KeyPool 管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    provider_keys: list[ProviderKeyInfo]
    request_id: str
    trace_id: str


class ProviderKeyResponse(BaseModel):
    """定义 Provider 与 KeyPool 管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    provider_key: ProviderKeyInfo
    request_id: str
    trace_id: str


class ProviderKeyDeleteResponse(BaseModel):
    """删除 Provider Key 后返回的安全确认。"""

    deleted_provider_key_id: str
    request_id: str
    trace_id: str


class CreateProviderRequest(BaseModel):
    """创建 Provider 请求。

    Provider Key 通过独立接口创建，本请求只接收 Endpoint 元数据。
    """

    provider_code: str | None = None
    name: str | None = None
    provider_type: str | None = "openai_compatible"
    base_url: str | None = None
    auth_type: str | None = "bearer"
    description: str | None = None
    maintenance_note: str | None = None

    # 禁止借额外字段夹带 tenant_id、认证头或其他不属于 Provider 元数据的内容。
    model_config = ConfigDict(extra="forbid")


class UpdateProviderRequest(BaseModel):
    """更新 Provider 请求。

    PATCH 未传字段表示不修改；status 用于启用/禁用 Endpoint。
    """

    name: str | None = None
    provider_type: str | None = None
    base_url: str | None = None
    auth_type: str | None = None
    description: str | None = None
    maintenance_note: str | None = None
    status: str | None = None

    # PATCH 通过 model_fields_set 区分“省略”与“显式 null”，因此必须保留严格字段集合。
    model_config = ConfigDict(extra="forbid")


class CreateProviderKeyRequest(BaseModel):
    """创建 Provider Key 请求。

    plain_value 只允许出现在本次请求体中，service 层会加密落库并返回脱敏摘要。
    """

    name: str | None = None
    plain_value: str | None = None
    weight: int | None = 100


class UpdateProviderKeyRequest(BaseModel):
    """更新 Provider Key 请求。

    不接收明文 Key，轮换应创建新 Key 后禁用旧 Key。
    """

    name: str | None = None
    weight: int | None = None
    status: str | None = None


class TestProviderRequest(BaseModel):
    """定义 Provider 与 KeyPool 管理 的请求 DTO，集中约束 TestProviderRequest 入参字段和默认值。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    timeout_seconds: float | None = 5


class ProviderTestResult(BaseModel):
    """承载 Provider 与 KeyPool 管理 的 ProviderTestResult 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    status: str
    http_status: int | None
    latency_ms: int
    message: str
    checked_at: datetime


class ProviderTestResponse(BaseModel):
    """定义 Provider 与 KeyPool 管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    provider: ProviderInfo
    test_result: ProviderTestResult
    request_id: str
    trace_id: str
