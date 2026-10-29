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
    assurance_level: Literal["password", "mfa", "recovery"] = "password"
    auth_method: Literal["password", "totp", "webauthn", "recovery_code"] = "password"
    trusted_device_id: str | None = None
    revoke_generation: int = Field(default=1, ge=1)


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


class MfaFactorSummary(IdentitySecurityResponse):
    factor_id: str
    factor_type: Literal["totp", "webauthn"]
    status: Literal["pending", "active", "disabled"]
    revision: int = Field(ge=1)
    verified_at: datetime | None
    created_at: datetime


class RecoveryCodeSetSummary(IdentitySecurityResponse):
    recovery_code_set_id: str
    status: Literal["active", "superseded", "exhausted", "revoked"]
    revision: int = Field(ge=1)
    remaining_count: int = Field(ge=0)
    generated_at: datetime


class TrustedDeviceSummary(IdentitySecurityResponse):
    trusted_device_id: str
    label: str
    device_family: Literal["chrome", "edge", "firefox", "safari", "mobile", "unknown"]
    trust_level: Literal["mfa_verified"] = "mfa_verified"
    status: Literal["active", "revoked", "expired"]
    revision: int = Field(ge=1)
    expires_at: datetime
    last_used_at: datetime | None
    created_at: datetime


class RecoveryChallengeSummary(IdentitySecurityResponse):
    challenge_id: str
    status: Literal["pending"] = "pending"
    ready_at: datetime
    expires_at: datetime


class IdentitySecurityOverviewResponse(IdentitySecurityResponse):
    factors: tuple[MfaFactorSummary, ...]
    recovery_code_set: RecoveryCodeSetSummary | None
    recovery_challenge: RecoveryChallengeSummary | None
    trusted_devices: tuple[TrustedDeviceSummary, ...]
    sessions: tuple[GlobalSessionSummary, ...]
    request_id: str
    trace_id: str


class UnifiedMfaEnrollmentResponse(IdentitySecurityResponse):
    method: Literal["totp", "webauthn", "recovery_codes"]
    factor_id: str | None = None
    challenge_id: str | None = None
    public_key_options: dict[str, object] | None = None
    secret: str | None = Field(default=None, repr=False)
    provisioning_uri: str | None = Field(default=None, repr=False)
    recovery_codes: tuple[str, ...] | None = Field(default=None, repr=False)
    expires_at: datetime | None = None
    request_id: str
    trace_id: str


class MfaChallengeResponse(IdentitySecurityResponse):
    challenge_id: str
    purpose: Literal["webauthn_registration", "webauthn_authentication", "step_up"]
    method: Literal["webauthn"] = "webauthn"
    public_key_options: dict[str, object]
    expires_at: datetime
    request_id: str
    trace_id: str


class RecoveryChallengeResponse(RecoveryChallengeSummary):
    purpose: Literal["recovery"] = "recovery"
    request_id: str
    trace_id: str


class RecoveryCompletionResponse(IdentitySecurityResponse):
    recovered: Literal[True] = True
    assurance_level: Literal["recovery"] = "recovery"
    completed_at: datetime
    request_id: str
    trace_id: str


class MfaChallengeCompletionResponse(IdentitySecurityResponse):
    factor_id: str
    factor_type: Literal["webauthn"] = "webauthn"
    status: Literal["active"] = "active"
    revision: int = Field(ge=2)
    verified_at: datetime
    request_id: str
    trace_id: str


class RecoveryCodeSetResponse(IdentitySecurityResponse):
    recovery_code_set: RecoveryCodeSetSummary
    recovery_codes: tuple[str, ...] = Field(repr=False)
    request_id: str
    trace_id: str


class TrustedDeviceResponse(IdentitySecurityResponse):
    trusted_device: TrustedDeviceSummary
    request_id: str
    trace_id: str


class TrustedDevicesResponse(IdentitySecurityResponse):
    trusted_devices: tuple[TrustedDeviceSummary, ...]
    request_id: str
    trace_id: str


class StepUpGrantResponse(IdentitySecurityResponse):
    grant: str = Field(repr=False)
    grant_id: str
    action_code: str
    resource_type: str
    resource_id: str
    auth_method: Literal["totp", "webauthn", "recovery_code"]
    assurance_level: Literal["mfa", "recovery"]
    expires_at: datetime
    request_id: str
    trace_id: str
