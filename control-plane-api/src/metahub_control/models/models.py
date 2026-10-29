"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/models/models.py
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ModelCatalogInfo(BaseModel):
    """模型目录的对外展示信息。

    每条记录属于单一 tenant_id，并携带 Provider 映射、能力、上下文长度和价格快照，
    供 Gateway 路由和用量成本估算使用。
    """

    model_id: str
    tenant_id: str
    internal_model_id: str
    provider_id: str
    provider_code: str
    provider_name: str
    provider_model_id: str
    display_name: str | None
    capabilities: dict[str, bool]
    context_window: int
    input_price_per_1k: float
    output_price_per_1k: float
    cache_price_per_1k: float
    currency: str
    price_schema_version: str | None
    status: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None


class ModelListResponse(BaseModel):
    """定义 模型目录 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    models: list[ModelCatalogInfo]
    request_id: str
    trace_id: str


class ModelResponse(BaseModel):
    """定义 模型目录 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    model: ModelCatalogInfo
    request_id: str
    trace_id: str


class CreateModelRequest(BaseModel):
    """创建模型目录请求。

    请求 DTO 保持可空，service 层负责输出业务错误码，避免框架默认 422 泄漏实现细节。
    """

    internal_model_id: str | None = None
    provider_id: str | None = None
    provider_model_id: str | None = None
    display_name: str | None = None
    capabilities: dict[str, object] | None = None
    context_window: int | None = 0
    input_price_per_1k: float | None = 0
    output_price_per_1k: float | None = 0
    # 旧客户端未携带缓存价时由服务层沿用输入价；显式 0 仍表示免费命中。
    cache_price_per_1k: float | None = None
    currency: str | None = "CNY"
    price_schema_version: str | None = None
    description: str | None = None
    status: str | None = "active"


class UpdateModelRequest(BaseModel):
    """更新模型目录请求。

    未传字段表示保持原值，status 用于启停模型而非物理删除。
    """

    provider_id: str | None = None
    provider_model_id: str | None = None
    display_name: str | None = None
    capabilities: dict[str, object] | None = None
    context_window: int | None = None
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    cache_price_per_1k: float | None = None
    currency: str | None = None
    price_schema_version: str | None = None
    description: str | None = None
    status: str | None = None
