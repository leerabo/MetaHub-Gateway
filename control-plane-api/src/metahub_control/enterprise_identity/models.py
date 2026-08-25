"""Strict, non-secret DTOs for enterprise domain and SSO control."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnterpriseIdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DomainCreateRequest(EnterpriseIdentityModel):
    domain: str = Field(min_length=3, max_length=253)


class SsoConnectionCreateRequest(EnterpriseIdentityModel):
    verified_domain_id: str = Field(pattern=r"^vdm_[0-9a-f]{32}$")
    connection_code: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    protocol: Literal["oidc", "saml"]
    issuer: str = Field(min_length=3, max_length=512)
    authorization_endpoint: str | None = Field(default=None, max_length=1024)
    token_endpoint: str | None = Field(default=None, max_length=1024)
    client_id: str | None = Field(default=None, min_length=1, max_length=256)
    client_secret: str | None = Field(default=None, min_length=16, max_length=2048)
    jwks: dict[str, Any] | None = None
    sso_url: str | None = Field(default=None, max_length=1024)
    certificate_pem: str | None = Field(default=None, min_length=64, max_length=32768)

    @model_validator(mode="after")
    def protocol_fields_are_exact(self) -> SsoConnectionCreateRequest:
        oidc_values = (
            self.authorization_endpoint,
            self.token_endpoint,
            self.client_id,
            self.client_secret,
            self.jwks,
        )
        saml_values = (self.sso_url, self.certificate_pem)
        if self.protocol == "oidc":
            if any(value is None for value in oidc_values) or any(
                value is not None for value in saml_values
            ):
                raise ValueError("OIDC fields are incomplete")
        elif any(value is not None for value in oidc_values) or any(
            value is None for value in saml_values
        ):
            raise ValueError("SAML fields are incomplete")
        return self


class SsoTestRequest(EnterpriseIdentityModel):
    sso_connection_id: str = Field(pattern=r"^ssc_[0-9a-f]{32}$")


class SsoPolicyUpdateRequest(EnterpriseIdentityModel):
    mode: Literal["optional", "enforced"]
    active_connection_id: str | None = Field(
        default=None, pattern=r"^ssc_[0-9a-f]{32}$"
    )
    break_glass_account_id: str | None = Field(
        default=None, pattern=r"^gac_[0-9a-f]{32}$"
    )

    @model_validator(mode="after")
    def enforced_policy_is_complete(self) -> SsoPolicyUpdateRequest:
        if self.mode == "enforced" and (
            self.active_connection_id is None or self.break_glass_account_id is None
        ):
            raise ValueError("enforced SSO requires connection and break-glass owner")
        return self


class LoginDiscoveryResponse(EnterpriseIdentityModel):
    mode: Literal["password", "sso"]
    transaction_id: str
    connection_code: str | None
    authorization_url: str | None
    expires_at: datetime
    request_id: str
    trace_id: str


class SsoCallbackRequest(EnterpriseIdentityModel):
    protocol: Literal["oidc", "saml"]
    state: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    code: str | None = Field(default=None, min_length=8, max_length=4096)
    saml_response: str | None = Field(default=None, min_length=16, max_length=131072)

    @model_validator(mode="after")
    def callback_fields_are_exact(self) -> SsoCallbackRequest:
        if self.protocol == "oidc" and (self.code is None or self.saml_response is not None):
            raise ValueError("OIDC callback fields are invalid")
        if self.protocol == "saml" and (self.saml_response is None or self.code is not None):
            raise ValueError("SAML callback fields are invalid")
        return self


class VerifiedDomainSummary(EnterpriseIdentityModel):
    verified_domain_id: str
    domain: str
    status: Literal["pending", "verified", "suspended"]
    revision: int = Field(ge=1)
    challenge_expires_at: datetime
    verified_at: datetime | None
    updated_at: datetime


class DomainCreateResponse(EnterpriseIdentityModel):
    domain: VerifiedDomainSummary
    verification_record: str
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class DomainResponse(EnterpriseIdentityModel):
    domain: VerifiedDomainSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class SsoCertificateSummary(EnterpriseIdentityModel):
    sso_certificate_id: str
    purpose: Literal["saml_signing", "oidc_jwk"]
    fingerprint_sha256: str
    status: Literal["staged", "active", "retired", "expired"]
    not_before: datetime
    not_after: datetime


class SsoConnectionSummary(EnterpriseIdentityModel):
    sso_connection_id: str
    verified_domain_id: str
    connection_code: str
    display_name: str
    protocol: Literal["oidc", "saml"]
    issuer: str
    status: Literal["draft", "testing", "active", "disabled"]
    revision: int = Field(ge=1)
    tested_at: datetime | None
    updated_at: datetime
    certificates: tuple[SsoCertificateSummary, ...]


class SsoConnectionResponse(EnterpriseIdentityModel):
    connection: SsoConnectionSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class SsoTestResponse(EnterpriseIdentityModel):
    connection: SsoConnectionSummary
    outcome: Literal["passed"] = "passed"
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class SsoPolicySummary(EnterpriseIdentityModel):
    mode: Literal["optional", "enforced"]
    active_connection_id: str | None
    break_glass_configured: bool
    revision: int = Field(ge=1)
    updated_at: datetime


class SsoPolicyResponse(EnterpriseIdentityModel):
    policy: SsoPolicySummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class BreakGlassCandidateSummary(EnterpriseIdentityModel):
    account_id: str = Field(pattern=r"^gac_[0-9a-f]{32}$")
    display_name: str = Field(min_length=1, max_length=160)
    password_configured: bool
    mfa_configured: bool


class EnterpriseIdentitySummaryResponse(EnterpriseIdentityModel):
    workspace_id: str
    domains: tuple[VerifiedDomainSummary, ...]
    connections: tuple[SsoConnectionSummary, ...]
    policy: SsoPolicySummary | None
    break_glass_candidates: tuple[BreakGlassCandidateSummary, ...]
    request_id: str
    trace_id: str


class SsoCallbackResponse(EnterpriseIdentityModel):
    authenticated: Literal[True] = True
    workspace_id: str
    redirect_to: str = "/"
    request_id: str
    trace_id: str
