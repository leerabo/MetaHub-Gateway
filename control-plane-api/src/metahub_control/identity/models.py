"""Public, tenant-free response models for global account identity."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


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
