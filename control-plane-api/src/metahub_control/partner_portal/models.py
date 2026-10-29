"""PRD-087 伙伴门户的严格请求与响应合同。"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


def _plain_text(value: str) -> str:
    """拒绝控制字符；各字段的 UTF-8 字节上限由后续校验器落实。"""

    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("control characters are not allowed")
    return value


PlainText = Annotated[str, AfterValidator(_plain_text)]


class _StrictModel(BaseModel):
    """伙伴门户模型基类：拒绝未声明字段，保证 API 响应边界稳定。"""

    model_config = ConfigDict(extra="forbid")


class CreatePartnerRequest(_StrictModel):
    """创建伙伴配置的租户内请求字段。"""

    partner_code: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]{0,79}$")
    display_name: PlainText = Field(min_length=1, max_length=120)
    portal_name: PlainText = Field(min_length=1, max_length=80)
    brand_primary_color: str = Field(pattern=r"^#[0-9A-F]{6}$")
    support_text: PlainText | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("display_name", "portal_name", "support_text")
    @classmethod
    def validate_utf8_size(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        limits = {"display_name": 120, "portal_name": 80, "support_text": 160}
        if len(value.encode("utf-8")) > limits[info.field_name]:
            raise ValueError("text is too large")
        return value


class UpdateBrandingRequest(_StrictModel):
    """更新白标品牌信息的请求字段，支持显式清除支持文本。"""

    portal_name: PlainText | None = Field(default=None, min_length=1, max_length=80)
    brand_primary_color: str | None = Field(default=None, pattern=r"^#[0-9A-F]{6}$")
    support_text: PlainText | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("portal_name", "support_text")
    @classmethod
    def validate_utf8_size(cls, value: str | None, info) -> str | None:
        if (
            value is not None
            and len(value.encode("utf-8"))
            > {"portal_name": 80, "support_text": 160}[info.field_name]
        ):
            raise ValueError("text is too large")
        return value

    @model_validator(mode="after")
    def reject_null_required_updates(self) -> UpdateBrandingRequest:
        fields = self.model_fields_set
        if (
            ("portal_name" in fields and self.portal_name is None)
            or ("brand_primary_color" in fields and self.brand_primary_color is None)
        ):
            raise ValueError("required branding updates cannot be null")
        return self


class CreatePartnerCustomerRequest(_StrictModel):
    """创建伙伴客户映射的请求字段。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    customer_code: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]{0,79}$")
    display_name: PlainText = Field(min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def validate_display_name_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 120:
            raise ValueError("text is too large")
        return value


class UpdatePartnerCustomerRequest(_StrictModel):
    """禁用伙伴客户映射的请求字段。"""

    display_name: PlainText | None = Field(default=None, min_length=1, max_length=120)
    status: Literal["disabled"] | None = None

    @field_validator("display_name")
    @classmethod
    def validate_display_name_size(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 120:
            raise ValueError("text is too large")
        return value

    @model_validator(mode="after")
    def reject_null_updates(self) -> UpdatePartnerCustomerRequest:
        fields = self.model_fields_set
        if (
            ("display_name" in fields and self.display_name is None)
            or ("status" in fields and self.status is None)
        ):
            raise ValueError("customer updates cannot be null")
        return self


class PartnerInfo(_StrictModel):
    """伙伴配置的公开响应模型，不包含密钥或正文。"""

    partner_id: str
    partner_code: str
    display_name: str
    portal_name: str
    brand_primary_color: str
    support_text: str | None
    status: Literal["active"]
    default_commission_rate_bps: Literal[1000]
    revision: int
    created_at: str
    updated_at: str


class PartnerResponse(_StrictModel):
    """伙伴配置响应信封，携带统一请求追踪标识。"""

    partner: PartnerInfo
    request_id: str
    trace_id: str


class PartnerProjectInfo(_StrictModel):
    """可供伙伴映射的项目摘要。"""

    project_id: str
    project_code: str
    name: str


class PartnerProjectsResponse(_StrictModel):
    """项目列表响应信封。"""

    items: list[PartnerProjectInfo]
    total: int
    request_id: str
    trace_id: str


class PartnerWindow(_StrictModel):
    """统一 UTC 日期窗口。"""

    start_date: str
    end_date: str
    timezone: Literal["UTC"] = "UTC"


class PartnerCustomerUsage(_StrictModel):
    """客户历史用量和成本摘要。"""

    usage_scope: Literal["project_history"] = "project_history"
    start_date: str
    end_date: str
    request_count: int
    success_count: int
    failed_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_cny_micros: int
    cost_usd_micros: int
    estimated_records: int


class PartnerCommissionCursor(_StrictModel):
    """分润游标的物化状态和新鲜度。"""

    status: Literal["pending", "leased", "complete"]
    last_scanned_ledger_sequence: int
    source_high_water_sequence: int
    freshness_status: Literal["current", "lagging"]


class PartnerCustomerInfo(_StrictModel):
    """伙伴客户及其用量、分润游标信息。"""

    partner_customer_id: str
    project_id: str
    customer_code: str
    display_name: str
    status: Literal["active", "disabled"]
    commission_rate_bps: Literal[1000]
    revision: int
    linked_from_ledger_sequence: int
    commission_cutoff_sequence: int | None
    created_at: str
    updated_at: str
    disabled_at: str | None
    usage: PartnerCustomerUsage
    commission_cursor: PartnerCommissionCursor


class PartnerCustomerResponse(_StrictModel):
    """单个伙伴客户响应信封。"""

    customer: PartnerCustomerInfo
    request_id: str
    trace_id: str


class PartnerCustomersResponse(_StrictModel):
    """伙伴客户分页响应信封。"""

    window: PartnerWindow
    items: list[PartnerCustomerInfo]
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


class CommissionLine(_StrictModel):
    """单条不可变分润记录摘要。"""

    commission_record_id: str
    partner_customer_id: str
    customer_code: str
    customer_name: str
    currency: Literal["CNY", "USD"]
    amount_micros: int
    commission_rate_bps: int
    commission_amount_micros: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    estimated: bool
    status: Literal["accrued"]
    settlement_status: Literal["not_applicable"] = "not_applicable"
    source_request_id: str
    source_trace_id: str
    source_created_at: str


class CommissionCurrencyTotal(_StrictModel):
    """按币种聚合的分润总计。"""

    currency: Literal["CNY", "USD"]
    request_count: int
    total_tokens: int
    amount_micros: int
    commission_amount_micros: int
    estimated_records: int


class CommissionFreshness(_StrictModel):
    """分润报表物化游标与源账本高水位。"""

    materialized_through_sequence: int
    source_high_water_sequence: int
    freshness_status: Literal["current", "lagging"]


class PartnerCommissionResponse(_StrictModel):
    """伙伴分润分页报表响应。"""

    window: PartnerWindow
    items: list[CommissionLine]
    totals: list[CommissionCurrencyTotal]
    cursor: CommissionFreshness
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


class DeliveryBranding(_StrictModel):
    """交付包中的白标品牌字段。"""

    portal_name: str
    brand_primary_color: str
    support_text: str | None


class DeliveryCustomer(_StrictModel):
    """交付包中的客户公开字段。"""

    customer_code: str
    display_name: str
    status: Literal["active", "disabled"]


class DeliveryApiPaths(_StrictModel):
    """交付包声明的 OpenAI-compatible API 路径。"""

    chat_completions: Literal["/v1/chat/completions"] = "/v1/chat/completions"
    responses: Literal["/v1/responses"] = "/v1/responses"


class DeliveryPackage(_StrictModel):
    """客户交付包的固定结构。"""

    schema_: Literal["metahub.partner-delivery/v1"] = Field(
        default="metahub.partner-delivery/v1", alias="schema"
    )
    partner_code: str
    branding: DeliveryBranding
    customers: list[DeliveryCustomer]
    api_paths: DeliveryApiPaths
    documentation_path: Literal["/docs/api/openai-compatible"] = "/docs/api/openai-compatible"
    generated_at: str
    request_id: str
    trace_id: str


class DeliveryEnvelope(_StrictModel):
    """交付包及其完整性哈希信封。"""

    package: DeliveryPackage
    manifest_sha256: str


PARTNER_CUSTOMER_ID = re.compile(r"^pcu_[0-9a-f]{32}$")


__all__ = [
    name
    for name in globals()
    if name.startswith(("Commission", "Create", "Delivery", "Partner", "Update"))
]
