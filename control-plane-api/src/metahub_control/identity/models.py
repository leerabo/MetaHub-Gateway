"""Public, tenant-free response models for global account identity."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GlobalAccountSummary(BaseModel):
    email: str
    display_name: str
    status: str
    mfa_configured: bool


class GlobalSessionSummary(BaseModel):
    session_id: str
    device_family: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_current: bool


class IdentitySessionResponse(BaseModel):
    account: GlobalAccountSummary
    current_session: GlobalSessionSummary
    request_id: str
    trace_id: str


class IdentitySessionsResponse(BaseModel):
    sessions: tuple[GlobalSessionSummary, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    request_id: str
    trace_id: str


class IdentityLogoutResponse(BaseModel):
    logged_out: bool
    request_id: str
    trace_id: str


class IdentityRevokeResponse(BaseModel):
    revoked: bool
    request_id: str
    trace_id: str


class IdentityRotateResponse(BaseModel):
    rotated: bool
    account: GlobalAccountSummary
    current_session: GlobalSessionSummary
    request_id: str
    trace_id: str


class IdentityPendingChallengeResponse(BaseModel):
    intent_id: str
    purpose: str
    status: str
    expires_at: datetime
    resend_available_at: datetime
    request_id: str
    trace_id: str


class IdentitySecurityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MfaEnrollmentResponse(IdentitySecurityResponse):
    factor_id: str
    secret: str = Field(repr=False)
    provisioning_uri: str = Field(repr=False)
    expires_at: datetime
    request_id: str
    trace_id: str


class MfaVerificationResponse(IdentitySecurityResponse):
    factor_id: str
    status: Literal["active"] = "active"
    revision: int = Field(ge=2)
    verified_at: datetime
    request_id: str
    trace_id: str


class IdentityPlatformAccessResponse(IdentitySecurityResponse):
    available: bool
    request_id: str
    trace_id: str
