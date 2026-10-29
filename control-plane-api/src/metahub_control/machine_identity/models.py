"""Strict public DTOs for PRD-118 machine identity."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ServiceAccountCreateRequest(_StrictModel):
    name: str = Field(min_length=2, max_length=80)
    display_name: str = Field(min_length=2, max_length=120)
    scope_template: Literal["provider_reader", "observability_reader", "compliance_collector"]
    scopes: list[str] = Field(min_length=1, max_length=8)
    token_ttl_seconds: int = Field(ge=60, le=3600)
    ip_allowlist: list[str] = Field(default_factory=list, max_length=16)
    mtls_fingerprint_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class OAuthClientCreateRequest(_StrictModel):
    service_account_id: str = Field(min_length=36, max_length=36)
    audience: Literal["metahub-control"] = "metahub-control"


class OAuthClientSummary(_StrictModel):
    oauth_client_id: str
    client_id: str
    audience: Literal["metahub-control"]
    status: Literal["active", "rotating", "revoked"]
    credential_generation: int
    revision: int
    credential_prefix: str | None
    credential_expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ServiceAccountSummary(_StrictModel):
    service_account_id: str
    workspace_id: str
    name: str
    display_name: str
    status: Literal["active", "disabled", "revoked"]
    scope_template: Literal["provider_reader", "observability_reader", "compliance_collector"]
    scopes: list[str]
    token_ttl_seconds: int
    ip_allowlist: list[str]
    mtls_required: bool
    constraint_generation: int
    revision: int
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime
    oauth_client: OAuthClientSummary | None = None


class ServiceAccountResponse(_StrictModel):
    service_account: ServiceAccountSummary
    request_id: str
    trace_id: str


class ServiceAccountListResponse(_StrictModel):
    service_accounts: list[ServiceAccountSummary]
    request_id: str
    trace_id: str


class OAuthClientCredentialResponse(_StrictModel):
    service_account: ServiceAccountSummary
    oauth_client: OAuthClientSummary
    client_secret: str
    request_id: str
    trace_id: str


class OAuthTokenResponse(_StrictModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    scope: str
