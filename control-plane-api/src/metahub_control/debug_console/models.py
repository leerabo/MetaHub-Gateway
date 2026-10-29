"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/debug_console/models.py
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class DebugMessage(BaseModel):
    """调试台消息 DTO。

    字段保持宽松是为了让 service 层把所有参数错误统一映射为 debug.request_invalid，而不是让 FastAPI
    提前返回通用 422；真正允许的 role/content 组合会在业务校验中收紧。
    """

    role: str | None = None
    content: Any = None
    model_config = ConfigDict(extra="allow")


class DebugChatCompletionRequest(BaseModel):
    """内置调试台 Chat Completions 请求 DTO。

    前端只传 Virtual Key 的记录 ID、模型别名和临时 messages；tenant_id、Authorization、Provider Key
    等敏感或越权字段即使被恶意塞入，也会在 service 层按统一错误码拒绝。
    """

    virtual_key_id: str | None = None
    environment_id: str | None = None
    model_alias: str | None = None
    messages: list[DebugMessage] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    model_config = ConfigDict(extra="allow")


class DebugUsageInfo(BaseModel):
    """调试结果中的用量摘要。

    该结构只包含 Token 计数和来源标记，不包含 Prompt 或 Response 正文，便于前端展示成本追踪入口。
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    estimated: bool
    usage_source: str


class DebugCostInfo(BaseModel):
    """调试结果中的成本摘要。

    金额统一用字符串返回，避免前端展示时受到浮点数精度影响；最终账本来源仍是 usage_ledger。
    """

    amount: str
    currency: str
    cost_cny: str
    cost_usd: str
    price_schema_version: str | None
    price_snapshot_id: str | None


class DebugResultResponse(BaseModel):
    """内置 API 调试台响应 DTO。

    响应面向当前登录用户返回一次性调试结果，携带 request_id/trace_id、路由结果、usage/cost 和日志跳转地址；
    后端不会把 response_json 持久化到事件表或结构化日志。
    """

    tenant_id: str
    debug_request_id: str
    request_id: str
    trace_id: str
    status: Literal["success"]
    latency_ms: int
    virtual_key_id: str
    virtual_key_prefix: str
    environment_id: str
    environment: str
    environment_name: str
    model_alias: str
    resolved_model: str
    provider_code: str
    response_json: dict[str, Any]
    response_preview: str
    usage: DebugUsageInfo
    cost: DebugCostInfo
    log_detail_url: str
