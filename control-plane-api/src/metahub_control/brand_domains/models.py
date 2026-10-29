"""Strict PRD-142 brand-domain request and response contracts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_COLOR = re.compile(r"^#[0-9A-F]{6}$")
_TEXT_LIMITS = {
    "product_name": 80,
    "login_title": 120,
    "login_message": 240,
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _VisualCommand(StrictModel):
    product_name: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    login_title: str | None = None
    login_message: str | None = None
    asset_kind: Literal["logo", "favicon"] | None = None
    png_base64: str | None = Field(default=None, max_length=700_000)

    @field_validator("product_name", "login_title", "login_message")
    @classmethod
    def validate_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        limit = _TEXT_LIMITS[info.field_name]
        if not value or len(value.encode("utf-8")) > limit or _has_control(value):
            raise ValueError("invalid brand text")
        return value

    @field_validator("primary_color", "secondary_color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        if value is not None and _COLOR.fullmatch(value) is None:
            raise ValueError("invalid brand color")
        return value

    def _validate_action_fields(self, action: str, *, domain_allowed: bool) -> None:
        fields = self.model_fields_set - {"action"}
        visual = set(_TEXT_LIMITS) | {"primary_color", "secondary_color"}
        if action == "update":
            if (
                not fields
                or not fields <= visual
                or any(getattr(self, name) is None for name in fields)
            ):
                raise ValueError("invalid update fields")
        elif action == "attach_asset":
            if fields != {"asset_kind", "png_base64"} or not self.png_base64:
                raise ValueError("invalid asset fields")
        elif action == "remove_asset":
            if fields != {"asset_kind"}:
                raise ValueError("invalid asset removal fields")
        elif action == "request_domain" and domain_allowed:
            if fields != {"domain"} or not getattr(self, "domain", None):
                raise ValueError("invalid domain fields")
        elif fields:
            raise ValueError("unexpected action fields")


class PlatformBrandCommand(_VisualCommand):
    action: Literal[
        "update",
        "attach_asset",
        "remove_asset",
        "request_domain",
        "release",
        "suspend",
        "rollback_lkg",
    ]
    domain: str | None = Field(default=None, max_length=253)

    @model_validator(mode="after")
    def validate_action(self) -> PlatformBrandCommand:
        self._validate_action_fields(self.action, domain_allowed=True)
        return self


class ChannelBrandCommand(_VisualCommand):
    action: Literal[
        "update",
        "attach_asset",
        "remove_asset",
        "request_domain",
        "release",
        "suspend",
        "rollback_lkg",
    ]
    domain: str | None = Field(default=None, max_length=253)

    @model_validator(mode="after")
    def validate_action(self) -> ChannelBrandCommand:
        self._validate_action_fields(self.action, domain_allowed=True)
        return self


class DeploymentBrandCommand(_VisualCommand):
    action: Literal[
        "update",
        "attach_asset",
        "remove_asset",
        "publish",
        "suspend",
        "rollback_lkg",
    ]

    @model_validator(mode="after")
    def validate_action(self) -> DeploymentBrandCommand:
        self._validate_action_fields(self.action, domain_allowed=False)
        return self


class BrandAssetSummary(StrictModel):
    asset_id: str
    kind: Literal["logo", "favicon"]
    status: Literal["staged", "scanning", "clean", "rejected", "published", "quarantined"]
    byte_count: int
    width: int
    height: int
    content_sha256: str


class BrandDomainSummary(StrictModel):
    domain_id: str
    canonical_domain: str
    status: Literal[
        "pending",
        "verified",
        "active",
        "reverification_required",
        "revoked",
        "quarantined",
    ]
    observation_count: int
    certificate_ready: bool
    routing_ready: bool
    revision: int
    challenge: str | None = None


class PlatformBrandInfo(StrictModel):
    platform_brand_id: str
    status: Literal["draft", "ready", "released", "suspended"]
    product_name: str
    primary_color: str
    secondary_color: str
    login_title: str
    login_message: str
    logo: BrandAssetSummary | None
    favicon: BrandAssetSummary | None
    domain: BrandDomainSummary | None
    published_generation: int
    lkg_generation: int
    revision: int
    source_sha256: str
    actions: tuple[str, ...]
    updated_at: datetime


class ChannelBrandInfo(StrictModel):
    channel_brand_id: str
    status: Literal["draft", "pending_domain", "ready", "released", "suspended", "archived"]
    product_name: str
    primary_color: str
    secondary_color: str
    login_title: str
    login_message: str
    logo: BrandAssetSummary | None
    favicon: BrandAssetSummary | None
    domain: BrandDomainSummary | None
    contract_status: Literal["active", "suspended", "retired"]
    contract_sha256: str
    contract_revision: int
    partner_scope_revision: int | None
    published_generation: int
    lkg_generation: int
    revision: int
    source_sha256: str
    actions: tuple[str, ...]
    updated_at: datetime


class DeploymentBrandInfo(StrictModel):
    deployment_brand_id: str
    status: Literal["draft", "ready", "published", "suspended"]
    product_name: str
    primary_color: str
    secondary_color: str
    login_title: str
    login_message: str
    logo: BrandAssetSummary | None
    favicon: BrandAssetSummary | None
    desired_generation: int
    applied_generation: int
    lkg_generation: int
    revision: int
    source_sha256: str
    ack_status: Literal["pending", "applied", "rejected", "stale"] | None
    actions: tuple[str, ...]
    updated_at: datetime


class PlatformBrandResponse(StrictModel):
    branding: PlatformBrandInfo
    request_id: str
    trace_id: str
    idempotent_replay: bool = False


class ChannelBrandResponse(StrictModel):
    branding: ChannelBrandInfo
    request_id: str
    trace_id: str
    idempotent_replay: bool = False


class DeploymentBrandResponse(StrictModel):
    branding: DeploymentBrandInfo
    request_id: str
    trace_id: str
    idempotent_replay: bool = False


class PublicBrandInfo(StrictModel):
    product_name: str
    primary_color: str
    secondary_color: str
    login_title: str
    login_message: str
    logo_path: str | None
    favicon_path: str | None
    brand_generation: int
    public_etag: str


class PublicBrandResponse(StrictModel):
    branding: PublicBrandInfo
    request_id: str
    trace_id: str


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


__all__ = [
    "BrandAssetSummary",
    "BrandDomainSummary",
    "ChannelBrandCommand",
    "ChannelBrandInfo",
    "ChannelBrandResponse",
    "DeploymentBrandCommand",
    "DeploymentBrandInfo",
    "DeploymentBrandResponse",
    "PlatformBrandCommand",
    "PlatformBrandInfo",
    "PlatformBrandResponse",
    "PublicBrandInfo",
    "PublicBrandResponse",
]
