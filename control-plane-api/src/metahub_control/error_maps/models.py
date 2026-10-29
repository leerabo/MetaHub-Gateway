"""PRD-056 Provider 错误归一规则的公开数据合同。

本模块只定义控制面可以返回或接收的安全字段。租户、操作者、版本历史、原始上游错误、
Provider Endpoint 与凭据都不能由浏览器提交，也不能通过响应 DTO 暴露。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt


class ProviderErrorMapHeadInfo(BaseModel):
    """当前租户覆盖层 head 的安全摘要。

    revision=0 是没有持久 head 时的合成只读状态；GET 不会为了展示空状态写数据库。
    """

    revision: int
    content_hash: str
    builtin_catalog_version: str
    updated_at: datetime | None


class ProviderErrorMapRuleInfo(BaseModel):
    """PAGE-081 可展示的一条租户、guard 或 builtin 规则。

    租户 pattern 经过敏感值门禁后允许 admin/developer 查看；原始 Provider 响应永远不属于
    该结构。平台规则只读，ordinal 用于解释冻结目录中的确定性匹配顺序。
    """

    error_map_id: str
    source: Literal["tenant", "guard", "builtin"]
    readonly: bool
    provider_id: str | None = None
    provider_code: str | None = None
    provider_name: str | None = None
    priority: int
    ordinal: int | None = None
    http_statuses: list[int]
    http_status_class: str | None = None
    error_field: str | None = None
    field_operator: str | None = None
    field_pattern: str | None = None
    message_operator: str | None = None
    message_pattern: str | None = None
    output_error_code: str
    retryable: bool
    user_action: str
    status: Literal["active", "disabled"]
    version: int
    pattern_hash: str


class ProviderErrorMapProviderOption(BaseModel):
    """规则编辑器可选择的租户内 Provider 安全选项。

    Endpoint、认证方式、KeyPool 和健康错误不属于规则管理页，避免只读 developer 扩大访问面。
    """

    provider_id: str
    provider_code: str
    name: str
    status: Literal["active", "disabled"]


class ProviderErrorMapListResponse(BaseModel):
    """规则列表、Provider 选项与同一读取快照内的 head。"""

    error_maps: list[ProviderErrorMapRuleInfo]
    provider_options: list[ProviderErrorMapProviderOption]
    head: ProviderErrorMapHeadInfo
    head_revision: int
    content_hash: str
    builtin_catalog_version: str
    updated_at: datetime | None
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


class ProviderErrorMapMutationResponse(BaseModel):
    """创建、更新、no-op 或幂等重放的统一安全响应。"""

    error_map: ProviderErrorMapRuleInfo
    head: ProviderErrorMapHeadInfo
    head_revision: int
    content_hash: str
    builtin_catalog_version: str
    updated_at: datetime | None
    changed: bool
    idempotent_replay: bool
    request_id: str
    trace_id: str


class CreateProviderErrorMapRequest(BaseModel):
    """创建租户规则的 exact-schema 正文。

    路由仍会手工解析原始 JSON；该模型只用于字段级严格验证和 OpenAPI 描述，不能替代
    重复 key、深度、节点、UTF-8 与正文长度门禁。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    provider_id: str | None
    priority: StrictInt
    http_statuses: list[StrictInt]
    http_status_class: Literal["4xx"] | None
    error_field: Literal["error.code", "error.type", "code", "type"] | None
    field_operator: Literal["equals", "contains"] | None
    field_pattern: str | None
    message_operator: Literal["contains"] | None
    message_pattern: str | None
    output_error_code: str
    status: Literal["active", "disabled"]


class UpdateProviderErrorMapRequest(BaseModel):
    """更新租户规则的 CAS 正文；未提交字段保持当前版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_version: StrictInt
    provider_id: str | None = None
    priority: StrictInt | None = None
    http_statuses: list[StrictInt] | None = None
    http_status_class: Literal["4xx"] | None = None
    error_field: Literal["error.code", "error.type", "code", "type"] | None = None
    field_operator: Literal["equals", "contains"] | None = None
    field_pattern: str | None = None
    message_operator: Literal["contains"] | None = None
    message_pattern: str | None = None
    output_error_code: str | None = None
    status: Literal["active", "disabled"] | None = None


__all__ = [
    "CreateProviderErrorMapRequest",
    "ProviderErrorMapHeadInfo",
    "ProviderErrorMapListResponse",
    "ProviderErrorMapMutationResponse",
    "ProviderErrorMapProviderOption",
    "ProviderErrorMapRuleInfo",
    "UpdateProviderErrorMapRequest",
]
