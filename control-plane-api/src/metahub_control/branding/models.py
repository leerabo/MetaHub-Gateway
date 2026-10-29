"""PRD-088 BrandingConfig 的严格请求、响应和公开投影模型。"""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


def _plain_text(value: str) -> str:
    """拒绝控制字符，避免登录文案穿透到 HTML、日志和审计摘要。"""

    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("control characters are not allowed")
    return value


PlainText = Annotated[str, AfterValidator(_plain_text)]
_RESERVED_DOMAIN_SUFFIXES = (
    ".example",
    ".invalid",
    ".internal",
    ".local",
    ".localhost",
    ".test",
)
_DOMAIN_NAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


def validate_custom_domain(value: object) -> str | None:
    """Validate a reserved public DNS name without performing network access."""

    if value is None:
        return None
    if type(value) is not str or any(
        ord(character) < 33 or ord(character) == 127 for character in value
    ):
        raise ValueError("invalid domain")
    try:
        encoded = value.encode("ascii")
        canonical = encoded.decode("idna").encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        raise ValueError("invalid domain") from None
    if len(encoded) > 253 or value != value.lower() or value.endswith(".") or canonical != value:
        raise ValueError("invalid domain")
    if _DOMAIN_NAME.fullmatch(value) is None:
        raise ValueError("invalid domain")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("invalid domain")
    if value in {"example.com", "example.cn"} or value.endswith(_RESERVED_DOMAIN_SUFFIXES):
        raise ValueError("invalid domain")
    return value


class _StrictModel(BaseModel):
    """所有 Branding DTO 拒绝未知字段，固定前后端响应边界。"""

    model_config = ConfigDict(extra="forbid")


class BrandingPatchRequest(_StrictModel):
    """白标局部更新请求；省略字段保持原值，null 仅清除 Logo/域名。"""

    product_name: PlainText | None = Field(default=None, min_length=1, max_length=80)
    primary_color: str | None = Field(default=None, pattern=r"^#[0-9A-F]{6}$")
    secondary_color: str | None = Field(default=None, pattern=r"^#[0-9A-F]{6}$")
    login_title: PlainText | None = Field(default=None, min_length=1, max_length=120)
    login_message: PlainText | None = Field(default=None, min_length=1, max_length=240)
    custom_domain: str | None = Field(default=None, max_length=253)
    logo_png_base64: str | None = Field(default=None, max_length=180_000)

    @field_validator("product_name", "login_title", "login_message")
    @classmethod
    def validate_utf8_size(cls, value: str | None, info) -> str | None:
        """按 UTF-8 字节而不是 Python 字符数执行持久化上限。"""

        if value is not None and len(value.encode("utf-8")) > {
            "product_name": 80,
            "login_title": 120,
            "login_message": 240,
        }[info.field_name]:
            raise ValueError("text is too large")
        return value

    @field_validator("custom_domain")
    @classmethod
    def validate_domain_wire(cls, value: str | None) -> str | None:
        """域名仅允许静态规范化，禁止 IP、路径、端口或控制字符。"""

        return validate_custom_domain(value)

    @model_validator(mode="after")
    def reject_empty_update(self) -> BrandingPatchRequest:
        """空 PATCH 没有可追踪变更，统一映射 branding.request_invalid。"""

        if not self.model_fields_set:
            raise ValueError("empty branding update")
        return self


class BrandingInfo(_StrictModel):
    """受保护 BrandingConfig DTO，不包含 tenant_id、Logo 正文或操作者身份。"""

    branding_config_id: str
    product_name: str
    primary_color: str
    secondary_color: str
    login_title: str
    login_message: str
    custom_domain: str | None
    custom_domain_status: Literal["not_configured", "reserved"]
    logo_present: bool
    logo_sha256: str | None
    logo_width: int | None
    logo_height: int | None
    revision: int
    status: Literal["active"]
    updated_at: str


class BrandingResponse(_StrictModel):
    """统一响应 envelope，固定 request_id/trace_id 追踪字段。"""

    branding: BrandingInfo
    request_id: str
    trace_id: str


class PublicBrandingInfo(_StrictModel):
    """登录页默认/租户品牌公共投影，不暴露存在性或内部版本。"""

    product_name: str
    primary_color: str
    secondary_color: str
    login_title: str
    login_message: str
    logo_present: bool


class PublicBrandingResponse(_StrictModel):
    """公共投影始终 200，并带统一请求追踪标识。"""

    branding: PublicBrandingInfo
    request_id: str
    trace_id: str


__all__ = [
    "BrandingInfo",
    "BrandingPatchRequest",
    "BrandingResponse",
    "PublicBrandingInfo",
    "PublicBrandingResponse",
    "validate_custom_domain",
]
