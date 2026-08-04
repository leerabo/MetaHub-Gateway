"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/webhooks/models.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WebhookDeliveryAttemptInfo(BaseModel):
    """Webhook 单次投递尝试 DTO。

    attempts 只记录状态、HTTP 状态码、耗时和安全错误摘要，不保存响应正文、签名头或任何密钥明文。
    """

    webhook_delivery_attempt_id: str
    tenant_id: str
    webhook_delivery_id: str
    webhook_endpoint_id: str
    attempt_no: int
    status: str
    http_status: int | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    error_summary: str | None = None
    created_at: datetime


class WebhookDeliveryInfo(BaseModel):
    """Webhook 投递摘要 DTO。

    payload 是事件信封的安全副本，必须已经在服务层过滤，不能包含 Prompt、Response、Authorization 或密钥。
    """

    webhook_delivery_id: str
    tenant_id: str
    webhook_endpoint_id: str
    event_id: str
    event_type: str
    event_timestamp: datetime
    payload: dict[str, Any]
    status: str
    attempt_count: int
    max_attempts: int
    http_status: int | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    error_summary: str | None = None
    next_retry_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    delivery_kind: str = "original"
    replayed_from_delivery_id: str | None = None
    replayed_by: str | None = None
    replayed_at: datetime | None = None
    secret_version: int = 1
    attempts: list[WebhookDeliveryAttemptInfo] = Field(default_factory=list)


class WebhookDeliveryRecordInfo(BaseModel):
    """详情页使用的投递记录 DTO。

    与内部 WebhookDeliveryInfo 相比刻意不包含 payload；排障页只展示事件、状态、关联和 attempt 摘要，
    防止未来安全 payload 扩展后被普通控制台无意回显。
    """

    webhook_delivery_id: str
    tenant_id: str
    webhook_endpoint_id: str
    event_id: str
    event_type: str
    event_timestamp: datetime
    status: str
    attempt_count: int
    max_attempts: int
    http_status: int | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    error_summary: str | None = None
    next_retry_at: datetime | None = None
    delivery_kind: str = "original"
    replayed_from_delivery_id: str | None = None
    replayed_by: str | None = None
    replayed_at: datetime | None = None
    secret_version: int = 1
    created_at: datetime
    updated_at: datetime
    attempts: list[WebhookDeliveryAttemptInfo] = Field(default_factory=list)


class WebhookEndpointInfo(BaseModel):
    """Webhook Endpoint 配置 DTO。

    列表和测试响应只返回 secret_prefix 与 secret_fingerprint；signing_secret 只能在创建响应中一次性返回。
    """

    webhook_endpoint_id: str
    tenant_id: str
    name: str
    endpoint_url: str
    event_types: list[str]
    secret_prefix: str
    secret_fingerprint: str
    secret_version: int = 1
    secret_rotated_at: datetime | None = None
    secret_rotated_by: str | None = None
    status: str
    timeout_ms: int
    max_attempts: int
    last_delivery_status: str | None = None
    last_delivery_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    recent_deliveries: list[WebhookDeliveryInfo] = Field(default_factory=list)


class WebhookEndpointListResponse(BaseModel):
    """Webhook Endpoint 列表响应，统一携带 request_id 和 trace_id 便于审计。"""

    webhooks: list[WebhookEndpointInfo]
    event_types: list[str]
    request_id: str
    trace_id: str


class WebhookEndpointCreateResponse(BaseModel):
    """Webhook Endpoint 创建响应。

    signing_secret 是唯一一次返回的 HMAC 密钥，前端应立即展示给管理员复制，后续接口不再返回。
    """

    webhook: WebhookEndpointInfo
    signing_secret: str
    request_id: str
    trace_id: str


class WebhookTestResponse(BaseModel):
    """Webhook 测试发送响应，包含最终 delivery 和逐次 attempt 记录。"""

    delivery: WebhookDeliveryInfo
    request_id: str
    trace_id: str


class CreateWebhookEndpointRequest(BaseModel):
    """创建 Webhook Endpoint 的请求体。

    tenant_id、secret、max_attempts 都由服务端决定，客户端不能通过请求体伪造租户或覆盖签名边界。
    """

    name: str | None = None
    endpoint_url: str | None = None
    event_types: list[str] | None = None
    status: str | None = None
    timeout_ms: int | None = None


class ReplayWebhookRequest(BaseModel):
    """手工重放请求。

    客户端只能提交来源 delivery 和幂等键；tenant_id、操作者、事件正文与 Secret 全部由后端恢复。
    """

    model_config = ConfigDict(extra="forbid")

    webhook_delivery_id: str | None = None
    idempotency_key: str | None = None


class ReplayWebhookDeliveryRequest(BaseModel):
    """顶层 delivery replay 请求。

    路径已经唯一确定来源 delivery，因此正文只允许提交幂等键。租户、Endpoint、操作者、
    payload 和 Secret 均由后端按认证上下文恢复，额外字段会在进入服务层前被拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str


class WebhookDeliveryFilters(BaseModel):
    """详情响应回显的规范化筛选条件，方便控制台明确当前查询边界。"""

    event_type: str | None = None
    status: str
    limit: int


class WebhookDeliveryListResponse(BaseModel):
    """单个 Endpoint 最近投递响应，不返回事件 payload 正文或 replay 幂等键。"""

    webhook_endpoint_id: str
    deliveries: list[WebhookDeliveryRecordInfo]
    filters: WebhookDeliveryFilters
    request_id: str
    trace_id: str


class WebhookReplayResponse(BaseModel):
    """手工重放响应；replayed=false 表示命中既有幂等 delivery，未重复出网。"""

    delivery: WebhookDeliveryInfo
    replayed: bool
    request_id: str
    trace_id: str


class WebhookDeadLetterAttemptInfo(BaseModel):
    """死信页面允许展示的单次投递尝试摘要。

    该 DTO 刻意省略 tenant、delivery/Endpoint 归属、错误正文和接收端响应，只保留页面计算
    尝试次数与定位固定错误码所需的字段。租户隔离仍由服务层查询和数据库约束执行。
    """

    attempt_no: int
    status: str
    http_status: int | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    created_at: datetime


class WebhookDeadLetterDeliveryInfo(BaseModel):
    """死信治理动作返回的最小化 replay delivery 摘要。

    内部 delivery 必须携带 tenant_id、操作者和 Secret 版本完成所有权校验，但 PAGE-083 不需要
    这些字段。公开 DTO 只返回稳定事件关系、终态和安全 attempt，不包含 payload、幂等键或 actor。
    """

    webhook_delivery_id: str
    webhook_endpoint_id: str
    event_id: str
    event_type: str
    event_timestamp: datetime
    status: str
    attempt_count: int
    max_attempts: int
    http_status: int | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    next_retry_at: datetime | None = None
    delivery_kind: str
    replayed_from_delivery_id: str | None = None
    replayed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    attempts: list[WebhookDeadLetterAttemptInfo] = Field(default_factory=list)


class WebhookDeadLetterInfo(BaseModel):
    """Webhook 死信的控制台安全摘要。

    DTO 只暴露治理与排障所需的稳定标识、固定错误码和时间，不返回事件 payload、Endpoint
    URL、replay 幂等键、签名、Secret 或内部 claim/lease。current_replay_delivery 同样使用
    无 payload 的详情 DTO，避免 PAGE-083 意外扩大敏感数据读取面。
    """

    dead_letter_event_id: str
    webhook_delivery_id: str
    webhook_endpoint_id: str
    webhook_endpoint_name: str
    event_id: str
    event_type: str
    status: str
    failure_error_code: str | None = None
    failure_http_status: int | None = None
    failed_at: datetime
    expires_at: datetime
    current_replay_delivery_id: str | None = None
    replay_requested_at: datetime | None = None
    replay_dispatch_attempts: int = 0
    last_replay_error_code: str | None = None
    replayed_at: datetime | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    latest_attempt: WebhookDeadLetterAttemptInfo | None = None
    current_replay_delivery: WebhookDeadLetterDeliveryInfo | None = None
    replayable: bool
    allowed_actions: list[str] = Field(default_factory=list)


class WebhookDeadLetterFilters(BaseModel):
    """死信列表回显的规范化筛选，帮助前端稳定维护游标查询身份。"""

    status: str
    event_type: str | None = None
    webhook_endpoint_id: str | None = None
    limit: int


class WebhookDeadLetterListResponse(BaseModel):
    """当前租户死信 seek 分页响应，统一携带请求链路标识。"""

    dead_letters: list[WebhookDeadLetterInfo]
    next_cursor: str | None = None
    filters: WebhookDeadLetterFilters
    request_id: str
    trace_id: str


class WebhookDeliveryReplayResponse(BaseModel):
    """顶层 delivery replay 的安全响应。

    replayed 表示本次是否首次创建 intent；幂等回读为 False。无论 worker 已完成还是仍在
    恢复，响应都返回数据库当前事实，不把事件正文重新暴露给 PAGE-083。
    """

    delivery: WebhookDeadLetterDeliveryInfo
    dead_letter: WebhookDeadLetterInfo
    replayed: bool
    request_id: str
    trace_id: str


class WebhookDeadLetterArchiveResponse(BaseModel):
    """死信逻辑归档响应；归档只改变治理状态，不删除投递与 attempt 证据。"""

    dead_letter: WebhookDeadLetterInfo
    request_id: str
    trace_id: str


class WebhookSecretRotateResponse(BaseModel):
    """Secret 轮换响应，新 signing_secret 与创建接口一样只返回本次。"""

    webhook: WebhookEndpointInfo
    signing_secret: str
    request_id: str
    trace_id: str
