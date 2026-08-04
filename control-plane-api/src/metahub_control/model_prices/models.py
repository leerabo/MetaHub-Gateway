"""模块职责：定义模型价格计划 API 的请求与响应数据契约。

维护边界：金额使用十进制字符串输出，DTO 不接受 tenant_id，也不包含任何密钥或请求正文。
来源文件：control-plane-api/src/metahub_control/model_prices/models.py
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ModelPricePlanInfo(BaseModel):
    """模型价格计划的安全展示对象，包含动态推导的有效状态和结束时刻。"""

    price_plan_id: str
    tenant_id: str
    model_id: str
    internal_model_id: str
    display_name: str | None
    provider_code: str
    input_price_per_1k: str
    output_price_per_1k: str
    currency: str
    price_schema_version: str
    effective_from: datetime
    effective_to: datetime | None
    effective_status: str
    source: str
    created_by: str | None
    created_by_name: str | None
    created_at: datetime


class ModelPricePlanResponse(BaseModel):
    """价格创建响应，统一携带 request_id 与 trace_id 供审计排障。"""

    price_plan: ModelPricePlanInfo
    request_id: str
    trace_id: str


class ModelPricePlanListResponse(BaseModel):
    """价格计划分页响应，同时返回本次有效状态计算采用的服务器时刻。"""

    price_plans: list[ModelPricePlanInfo]
    total: int
    limit: int
    offset: int
    as_of: datetime
    request_id: str
    trace_id: str


class CreateModelPricePlanRequest(BaseModel):
    """创建不可变价格计划的请求体，所有业务校验由服务层映射成统一错误码。"""

    model_id: str | None = None
    input_price_per_1k: object | None = None
    output_price_per_1k: object | None = None
    currency: str | None = None
    effective_from: object | None = None
    price_schema_version: str | None = None
