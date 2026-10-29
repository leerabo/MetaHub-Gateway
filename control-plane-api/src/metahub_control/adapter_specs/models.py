"""PRD-063 Provider Adapter 规范目录与激活响应模型。

公开模型只投影发布制品中的安全合同和租户级激活身份，不包含真实 Endpoint、Provider Key、
Token、Prompt、Response 或错误正文。所有响应都携带 request_id/trace_id，便于权限和目录
漂移问题按统一链路排查。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AdapterSpecCatalogInfo(BaseModel):
    """当前租户已经激活且与进程内目录一致的版本摘要。"""

    version: str
    hash: str
    activated_at: datetime


class ProviderAdapterSpecInfo(BaseModel):
    """PAGE-090 可展示的一条 Provider Adapter 安全合同。

    ``tenant_id`` 仅用于证明响应归属当前会话租户；可执行字段始终来自 Shared SDK 冻结目录，
    不能从租户数据库或客户端请求覆盖。
    """

    spec_id: str
    tenant_id: str
    provider_type: str
    display_name: str
    classification: str
    adapter_name: str
    adapter_version: str
    protocol_family: str
    allowed_auth_types: tuple[str, ...]
    model_id_strategy: str
    usage_profile: str
    error_profile: str
    stream_profile: str
    price_fields: tuple[str, ...]
    probe_profile: str
    platform_examples: tuple[str, ...]
    status: str
    contract_case_id: str


class AdapterSpecListResponse(BaseModel):
    """当前租户的只读 Adapter 目录响应，统一携带目录身份和请求追踪字段。"""

    catalog: AdapterSpecCatalogInfo
    specs: list[ProviderAdapterSpecInfo]
    request_id: str
    trace_id: str


class AdapterSpecActivationInfo(BaseModel):
    """内部发布服务可读取的单次目录激活事实。

    该结构不包含 spec JSON；调用方只能提交并核对版本与 hash，不能借内部接口注入 Adapter
    行为。``changed`` 放在外层响应中表达原始请求是否真的切换了 active 版本。
    """

    activation_id: str
    tenant_id: str
    catalog_version: str
    catalog_hash: str
    spec_count: int
    status: str
    activated_by: str
    activated_at: datetime
    superseded_at: datetime | None


class AdapterSpecActivationResponse(BaseModel):
    """内部目录激活或幂等重放响应。"""

    activation: AdapterSpecActivationInfo
    changed: bool
    request_id: str
    trace_id: str

    # 响应合同禁止未来无审查地附加 spec 正文、Endpoint 或凭据字段。
    model_config = ConfigDict(extra="forbid")
