"""Strict browser transport for tenant-free global account identity."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable
from typing import Any

import anyio
from fastapi import APIRouter, Request, Response
from metahub_shared.abuse_risk import runtime_for
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_identity_cookies_on_error

from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)

from .mfa_service import begin_totp_enrollment, verify_totp_enrollment
from .models import (
    IdentityLogoutResponse,
    IdentityPendingChallengeResponse,
    IdentityPlatformAccessResponse,
    IdentityRevokeResponse,
    IdentityRotateResponse,
    IdentitySecurityOverviewResponse,
    IdentitySessionResponse,
    IdentitySessionsResponse,
    MfaChallengeCompletionResponse,
    MfaChallengeResponse,
    MfaEnrollmentResponse,
    MfaVerificationResponse,
    RecoveryChallengeResponse,
    RecoveryCompletionResponse,
    StepUpGrantResponse,
    TrustedDeviceResponse,
    UnifiedMfaEnrollmentResponse,
)
from .registration_policy import (
    consume_registration_captcha,
    create_registration_captcha,
    public_registration_policy,
)
from .security_service import (
    begin_account_recovery,
    begin_webauthn_registration,
    begin_webauthn_step_up,
    complete_account_recovery,
    complete_webauthn_registration,
    disable_mfa_factor,
    generate_recovery_codes,
    get_identity_security_overview,
    issue_step_up_grant,
    revoke_trusted_device,
    trust_current_device,
)
from .self_service import (
    PendingIdentityChallenge,
    begin_password_reset,
    begin_registration,
    confirm_password_reset,
    register_local_account,
    resend_registration_verification,
    verify_registration,
)
from .service import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    authenticate_global_session,
    list_global_sessions,
    login_global_account,
    project_platform_access,
    revoke_global_session,
    rotate_global_password,
    valid_opaque_value,
)

router = APIRouter(prefix="/identity", tags=["identity"])
_MAX_IDENTITY_BODY_BYTES = 16_384
_IDENTITY_WORK_LIMITER = anyio.CapacityLimiter(4)
_DEVICE_COOKIE = "__Host-metahub-identity-device"
_DEVICE_COOKIE_MAX_AGE = 31_536_000
_RECOVERY_COOKIE_MAX_AGE = 172_800


@router.post("/login", response_model=IdentitySessionResponse)
async def login(request: Request, response: Response) -> IdentitySessionResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    payload = await _strict_json(
        request,
        required_fields=("email", "password"),
        optional_fields=("workspace_id", "totp_code", "recovery_reason"),
    )
    email = _required_string(payload, "email", minimum=3, maximum=320)
    password = _required_string(payload, "password", minimum=1, maximum=1024, byte_limit=True)
    recovery_values = (
        _optional_string(payload, "workspace_id", minimum=36, maximum=36),
        _optional_string(payload, "totp_code", minimum=6, maximum=6),
        _optional_string(payload, "recovery_reason", minimum=3, maximum=64),
    )
    if any(value is not None for value in recovery_values) and not all(
        value is not None for value in recovery_values
    ):
        raise ApiError("sso.request_invalid")
    await _risk_preflight(request, "login", email)
    issued = await _run_identity_operation(
        login_global_account,
        _config(request),
        email=email,
        password=password,
        recovery_workspace_id=recovery_values[0],
        totp_code=recovery_values[1],
        recovery_reason=recovery_values[2],
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        user_agent=request.headers.get("User-Agent"),
        client_host=request.client.host if request.client else None,
    )
    _set_identity_cookies(response, _config(request), issued.session_token, issued.csrf_token)
    return IdentitySessionResponse(
        account=issued.account,
        current_session=issued.current_session,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/registration-policy")
async def registration_policy(request: Request, response: Response) -> dict:
    _reject_query(request, ())
    response.headers["Cache-Control"] = "no-store"
    return await _run_identity_operation(public_registration_policy, _config(request))


@router.get("/registration-captcha")
async def registration_captcha(request: Request, response: Response) -> dict:
    _reject_query(request, ())
    device = _identity_device_token(request)
    result = await _run_identity_operation(
        create_registration_captcha,
        _config(request),
        device_token=device,
        client_host=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    _set_identity_device_cookie(response, device)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/registrations", response_model=IdentityPendingChallengeResponse | IdentitySessionResponse
)
async def register(
    request: Request,
    response: Response,
) -> IdentityPendingChallengeResponse | IdentitySessionResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(
        request,
        required_fields=("email", "display_name", "password", "consent_version"),
        optional_fields=("invitation_token", "captcha_id", "captcha_answer"),
    )
    device_token = _identity_device_token(request)
    email = _required_string(payload, "email", minimum=3, maximum=320)
    await _risk_preflight(request, "registration", email, device_token=device_token)
    policy = await _run_identity_operation(public_registration_policy, _config(request))
    if policy["human_verification"]:
        await _run_identity_operation(
            consume_registration_captcha,
            _config(request),
            challenge_id=_required_string(payload, "captcha_id", minimum=36, maximum=36),
            answer=_required_string(payload, "captcha_answer", minimum=1, maximum=16),
            device_token=device_token,
        )
    pending = await _run_identity_operation(
        begin_registration if policy["email_verification"] else register_local_account,
        _config(request),
        email=email,
        display_name=_required_string(payload, "display_name", minimum=1, maximum=120),
        password=_required_string(
            payload,
            "password",
            minimum=8,
            maximum=1024,
            byte_limit=True,
        ),
        consent_version=_required_string(
            payload,
            "consent_version",
            minimum=1,
            maximum=80,
        ),
        invitation_token=_optional_string(
            payload,
            "invitation_token",
            minimum=1,
            maximum=512,
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        user_agent=request.headers.get("User-Agent"),
        client_host=request.client.host if request.client else None,
        device_token=device_token,
    )
    _set_identity_device_cookie(response, device_token)
    if not isinstance(pending, PendingIdentityChallenge):
        _set_identity_cookies(response, _config(request), pending.session_token, pending.csrf_token)
        return IdentitySessionResponse(
            account=pending.account,
            current_session=pending.current_session,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    return _pending_response(pending, request)


@router.post("/verifications", response_model=IdentitySessionResponse)
async def verify(request: Request, response: Response) -> IdentitySessionResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(request, required_fields=("intent_id", "code"))
    device_token = _identity_device_token(request)
    issued = await _run_identity_operation(
        verify_registration,
        _config(request),
        intent_id=_required_string(payload, "intent_id", minimum=36, maximum=36),
        code=_required_string(payload, "code", minimum=6, maximum=6),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        user_agent=request.headers.get("User-Agent"),
        client_host=request.client.host if request.client else None,
        device_token=device_token,
    )
    _set_identity_cookies(response, _config(request), issued.session_token, issued.csrf_token)
    _set_identity_device_cookie(response, device_token)
    return IdentitySessionResponse(
        account=issued.account,
        current_session=issued.current_session,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/verifications/resend", response_model=IdentityPendingChallengeResponse)
async def resend_verification(
    request: Request,
    response: Response,
) -> IdentityPendingChallengeResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(request, required_fields=("intent_id",))
    device_token = _identity_device_token(request)
    pending = await _run_identity_operation(
        resend_registration_verification,
        _config(request),
        intent_id=_required_string(payload, "intent_id", minimum=36, maximum=36),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        user_agent=request.headers.get("User-Agent"),
        client_host=request.client.host if request.client else None,
        device_token=device_token,
    )
    _set_identity_device_cookie(response, device_token)
    return _pending_response(pending, request)


@router.post("/password-resets", response_model=IdentityPendingChallengeResponse)
async def request_password_reset(
    request: Request,
    response: Response,
) -> IdentityPendingChallengeResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(request, required_fields=("email",))
    device_token = _identity_device_token(request)
    email = _required_string(payload, "email", minimum=3, maximum=320)
    await _risk_preflight(request, "login_recovery", email, device_token=device_token)
    pending = await _run_identity_operation(
        begin_password_reset,
        _config(request),
        email=email,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        user_agent=request.headers.get("User-Agent"),
        client_host=request.client.host if request.client else None,
        device_token=device_token,
    )
    _set_identity_device_cookie(response, device_token)
    return _pending_response(pending, request)


@router.post("/password-resets/confirm", response_model=IdentitySessionResponse)
async def reset_password(request: Request, response: Response) -> IdentitySessionResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(
        request,
        required_fields=("intent_id", "code", "new_password"),
    )
    device_token = _identity_device_token(request)
    issued = await _run_identity_operation(
        confirm_password_reset,
        _config(request),
        intent_id=_required_string(payload, "intent_id", minimum=36, maximum=36),
        code=_required_string(payload, "code", minimum=6, maximum=6),
        new_password=_required_string(
            payload,
            "new_password",
            minimum=8,
            maximum=1024,
            byte_limit=True,
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        user_agent=request.headers.get("User-Agent"),
        client_host=request.client.host if request.client else None,
        device_token=device_token,
    )
    _set_identity_cookies(response, _config(request), issued.session_token, issued.csrf_token)
    _set_identity_device_cookie(response, device_token)
    return IdentitySessionResponse(
        account=issued.account,
        current_session=issued.current_session,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/mfa/totp/enrollments", response_model=MfaEnrollmentResponse)
async def create_totp_enrollment(
    request: Request,
    response: Response,
) -> MfaEnrollmentResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(request, required_fields=("current_password",))
    pending = await _run_identity_operation(
        begin_totp_enrollment,
        _config(request),
        session_token=_required_session_cookie(request),
        csrf_token=_required_csrf(request),
        current_password=_required_string(
            payload, "current_password", minimum=1, maximum=1024, byte_limit=True
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["Cache-Control"] = "no-store, private"
    return MfaEnrollmentResponse(
        factor_id=pending.factor_id,
        secret=pending.secret,
        provisioning_uri=pending.uri,
        expires_at=pending.expires_at,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/mfa/totp/verifications", response_model=MfaVerificationResponse)
async def activate_totp_factor(
    request: Request,
    response: Response,
) -> MfaVerificationResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(request, required_fields=("factor_id", "code"))
    verified = await _run_identity_operation(
        verify_totp_enrollment,
        _config(request),
        session_token=_required_session_cookie(request),
        csrf_token=_required_csrf(request),
        factor_id=_required_string(payload, "factor_id", minimum=36, maximum=36),
        code=_required_string(payload, "code", minimum=6, maximum=6),
        device_fingerprint=(
            (request.headers.get("User-Agent") or "unknown")
            + "|"
            + (request.client.host if request.client else "unknown")
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["Cache-Control"] = "no-store, private"
    return MfaVerificationResponse(
        factor_id=verified.factor_id,
        revision=verified.revision,
        verified_at=verified.verified_at,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/mfa/enrollments", response_model=UnifiedMfaEnrollmentResponse)
async def create_mfa_enrollment(
    request: Request,
    response: Response,
) -> UnifiedMfaEnrollmentResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(
        request,
        required_fields=("method", "current_password"),
    )
    method = _required_string(payload, "method", minimum=4, maximum=32)
    current_password = _required_string(
        payload, "current_password", minimum=1, maximum=1024, byte_limit=True
    )
    session_token = _required_session_cookie(request)
    csrf_token = _required_csrf(request)
    response.headers["Cache-Control"] = "no-store, private"
    if method == "totp":
        pending = await _run_identity_operation(
            begin_totp_enrollment,
            _config(request),
            session_token=session_token,
            csrf_token=csrf_token,
            current_password=current_password,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        return UnifiedMfaEnrollmentResponse(
            method="totp",
            factor_id=pending.factor_id,
            secret=pending.secret,
            provisioning_uri=pending.uri,
            expires_at=pending.expires_at,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    if method == "webauthn":
        browser_binding = _identity_device_token(request)
        pending = await _run_identity_operation(
            begin_webauthn_registration,
            _config(request),
            session_token=session_token,
            csrf_token=csrf_token,
            current_password=current_password,
            origin=_request_origin(request),
            browser_binding=browser_binding,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        _set_identity_device_cookie(response, browser_binding)
        return UnifiedMfaEnrollmentResponse(
            method="webauthn",
            factor_id=pending.factor_id,
            challenge_id=pending.challenge_id,
            public_key_options=pending.public_key_options,
            expires_at=pending.expires_at,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    if method == "recovery_codes":
        generated = await _run_identity_operation(
            generate_recovery_codes,
            _config(request),
            session_token=session_token,
            csrf_token=csrf_token,
            current_password=current_password,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        return UnifiedMfaEnrollmentResponse(
            method="recovery_codes",
            recovery_codes=generated.codes,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    raise ApiError("identity.request_invalid")


@router.post(
    "/mfa/challenges",
    response_model=(
        MfaChallengeResponse
        | MfaChallengeCompletionResponse
        | RecoveryChallengeResponse
        | RecoveryCompletionResponse
    ),
)
async def webauthn_challenge(
    request: Request,
    response: Response,
) -> (
    MfaChallengeResponse
    | MfaChallengeCompletionResponse
    | RecoveryChallengeResponse
    | RecoveryCompletionResponse
):
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(
        request,
        required_fields=("operation",),
        optional_fields=(
            "challenge_id",
            "credential",
            "current_password",
            "notification_code",
            "recovery_code",
        ),
    )
    operation = _required_string(payload, "operation", minimum=5, maximum=64)
    browser_binding = _identity_device_token(request)
    response.headers["Cache-Control"] = "no-store, private"
    _set_identity_device_cookie(response, browser_binding)
    if operation == "begin_step_up":
        if set(payload) != {"operation"}:
            raise ApiError("identity.request_invalid")
        pending = await _run_identity_operation(
            begin_webauthn_step_up,
            _config(request),
            session_token=_required_session_cookie(request),
            csrf_token=_required_csrf(request),
            origin=_request_origin(request),
            browser_binding=browser_binding,
        )
        return MfaChallengeResponse(
            challenge_id=pending.challenge_id,
            purpose="step_up",
            public_key_options=pending.public_key_options,
            expires_at=pending.expires_at,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    if operation == "complete_registration":
        if set(payload) != {"operation", "challenge_id", "credential"}:
            raise ApiError("identity.request_invalid")
        completed = await _run_identity_operation(
            complete_webauthn_registration,
            _config(request),
            session_token=_required_session_cookie(request),
            csrf_token=_required_csrf(request),
            challenge_id=_required_string(
                payload, "challenge_id", minimum=36, maximum=36
            ),
            credential=_required_object(payload, "credential"),
            browser_binding=browser_binding,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        return MfaChallengeCompletionResponse(
            factor_id=completed.factor_id,
            revision=completed.revision,
            verified_at=completed.verified_at,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    if operation == "begin_recovery":
        if set(payload) != {"operation", "current_password"}:
            raise ApiError("identity.request_invalid")
        session_token = _required_session_cookie(request)
        csrf_token = _required_csrf(request)
        pending = await _run_identity_operation(
            begin_account_recovery,
            _config(request),
            session_token=session_token,
            csrf_token=csrf_token,
            current_password=_required_string(
                payload, "current_password", minimum=1, maximum=1024, byte_limit=True
            ),
            browser_binding=browser_binding,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        _set_identity_cookies(
            response,
            _config(request),
            session_token,
            csrf_token,
            max_age=_RECOVERY_COOKIE_MAX_AGE,
        )
        return RecoveryChallengeResponse(
            challenge_id=pending.challenge_id,
            ready_at=pending.ready_at,
            expires_at=pending.expires_at,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    if operation == "complete_recovery":
        if set(payload) != {
            "operation",
            "challenge_id",
            "notification_code",
            "recovery_code",
        }:
            raise ApiError("identity.request_invalid")
        completed = await _run_identity_operation(
            complete_account_recovery,
            _config(request),
            session_token=_required_session_cookie(request),
            csrf_token=_required_csrf(request),
            challenge_id=_required_string(
                payload, "challenge_id", minimum=36, maximum=36
            ),
            notification_code=_required_string(
                payload, "notification_code", minimum=6, maximum=6
            ),
            recovery_code=_required_string(
                payload, "recovery_code", minimum=12, maximum=32
            ),
            browser_binding=browser_binding,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
            user_agent=request.headers.get("User-Agent"),
            client_host=request.client.host if request.client else None,
        )
        _clear_identity_cookies(response)
        _set_identity_cookies(
            response,
            _config(request),
            completed.session_token,
            completed.csrf_token,
        )
        return RecoveryCompletionResponse(
            completed_at=completed.completed_at,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    raise ApiError("identity.request_invalid")


@router.delete("/mfa/enrollments/{factor_id}", response_model=IdentityRevokeResponse)
async def delete_mfa_enrollment(
    factor_id: str,
    request: Request,
    response: Response,
) -> IdentityRevokeResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(request, required_fields=("current_password",))
    await _run_identity_operation(
        disable_mfa_factor,
        _config(request),
        session_token=_required_session_cookie(request),
        csrf_token=_required_csrf(request),
        factor_id=factor_id,
        current_password=_required_string(
            payload, "current_password", minimum=1, maximum=1024, byte_limit=True
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["Cache-Control"] = "no-store, private"
    return IdentityRevokeResponse(
        revoked=True,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/security", response_model=IdentitySecurityOverviewResponse)
async def identity_security(
    request: Request, response: Response
) -> IdentitySecurityOverviewResponse:
    _reject_legacy_authorization(request)
    _reject_query(request, ())
    overview = await _run_identity_operation(
        get_identity_security_overview,
        _config(request),
        session_token=_required_session_cookie(request),
    )
    response.headers["Cache-Control"] = "no-store, private"
    return IdentitySecurityOverviewResponse(
        factors=overview.factors,
        recovery_code_set=overview.recovery_code_set,
        recovery_challenge=overview.recovery_challenge,
        trusted_devices=overview.trusted_devices,
        sessions=overview.sessions,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/trusted-devices", response_model=TrustedDeviceResponse)
async def create_trusted_device(
    request: Request,
    response: Response,
) -> TrustedDeviceResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(request, required_fields=("label", "totp_code"))
    browser_binding = _identity_device_token(request)
    trusted = await _run_identity_operation(
        trust_current_device,
        _config(request),
        session_token=_required_session_cookie(request),
        csrf_token=_required_csrf(request),
        code=_required_string(payload, "totp_code", minimum=6, maximum=6),
        browser_binding=browser_binding,
        label=_required_string(payload, "label", minimum=1, maximum=80),
        device_family=_device_family(request.headers.get("User-Agent")),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _set_identity_device_cookie(response, browser_binding)
    response.headers["Cache-Control"] = "no-store, private"
    return TrustedDeviceResponse(
        trusted_device=trusted,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.delete("/trusted-devices/{trusted_device_id}", response_model=IdentityRevokeResponse)
async def delete_trusted_device(
    trusted_device_id: str,
    request: Request,
    response: Response,
) -> IdentityRevokeResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    await _run_identity_operation(
        revoke_trusted_device,
        _config(request),
        session_token=_required_session_cookie(request),
        csrf_token=_required_csrf(request),
        trusted_device_id=trusted_device_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["Cache-Control"] = "no-store, private"
    return IdentityRevokeResponse(
        revoked=True,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/step-up", response_model=StepUpGrantResponse)
async def step_up(request: Request, response: Response) -> StepUpGrantResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    payload = await _strict_json(
        request,
        required_fields=("method", "action_code", "resource_type", "resource_id"),
        optional_fields=("workspace_id", "code", "challenge_id", "credential"),
    )
    method = _required_string(payload, "method", minimum=4, maximum=32)
    proof: str | dict[str, object]
    challenge_id = _optional_string(payload, "challenge_id", minimum=36, maximum=36)
    if method in {"totp", "recovery_code"}:
        proof = _required_string(payload, "code", minimum=6, maximum=64)
        if "credential" in payload or challenge_id is not None:
            raise ApiError("identity.request_invalid")
    elif method == "webauthn":
        proof = _required_object(payload, "credential")
        if "code" in payload or challenge_id is None:
            raise ApiError("identity.request_invalid")
    else:
        raise ApiError("identity.request_invalid")
    browser_binding = _identity_device_token(request)
    issued = await _run_identity_operation(
        issue_step_up_grant,
        _config(request),
        session_token=_required_session_cookie(request),
        csrf_token=_required_csrf(request),
        workspace_session_token=_single_cookie(
            request, WORKSPACE_SESSION_COOKIE, required=False
        ),
        workspace_id=_optional_string(payload, "workspace_id", minimum=36, maximum=36),
        action_code=_required_string(payload, "action_code", minimum=3, maximum=80),
        resource_type=_required_string(payload, "resource_type", minimum=2, maximum=60),
        resource_id=_required_string(payload, "resource_id", minimum=1, maximum=120),
        method=method,
        proof=proof,
        challenge_id=challenge_id,
        browser_binding=browser_binding,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _set_identity_device_cookie(response, browser_binding)
    response.headers["Cache-Control"] = "no-store, private"
    return StepUpGrantResponse(
        grant=issued.grant,
        grant_id=issued.grant_id,
        action_code=issued.action_code,
        resource_type=issued.resource_type,
        resource_id=issued.resource_id,
        auth_method=issued.auth_method,
        assurance_level=issued.assurance_level,
        expires_at=issued.expires_at,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/me", response_model=IdentitySessionResponse)
async def me(request: Request) -> IdentitySessionResponse:
    _reject_legacy_authorization(request)
    _reject_query(request, ())
    identity = await _run_identity_operation(
        authenticate_global_session,
        _config(request),
        _required_session_cookie(request),
    )
    return IdentitySessionResponse(
        account=identity.account,
        current_session=identity.current_session,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/platform-access", response_model=IdentityPlatformAccessResponse)
async def platform_access(request: Request, response: Response) -> IdentityPlatformAccessResponse:
    _reject_legacy_authorization(request)
    _reject_query(request, ())
    available = await _run_identity_operation(
        project_platform_access,
        _config(request),
        session_token=_required_session_cookie(request),
    )
    response.headers["Cache-Control"] = "no-store, private"
    return IdentityPlatformAccessResponse(
        available=available,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/logout", response_model=IdentityLogoutResponse)
async def logout(request: Request, response: Response) -> IdentityLogoutResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    clear_identity_cookies_on_error(request)
    session_token = _single_cookie(request, SESSION_COOKIE, required=False)
    if session_token is not None:
        csrf_token = _required_csrf(request)
        identity = await _run_identity_operation(
            authenticate_global_session,
            _config(request),
            session_token,
        )
        await _run_identity_operation(
            revoke_global_session,
            _config(request),
            session_token=session_token,
            csrf_token=csrf_token,
            target_session_id=identity.session_id,
            reason="logout",
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    _clear_identity_cookies(response)
    return IdentityLogoutResponse(
        logged_out=True,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/sessions", response_model=IdentitySessionsResponse)
async def sessions(request: Request) -> IdentitySessionsResponse:
    _reject_legacy_authorization(request)
    limit, offset = _pagination(request)
    _identity, rows, total = await _run_identity_operation(
        list_global_sessions,
        _config(request),
        session_token=_required_session_cookie(request),
        limit=limit,
        offset=offset,
    )
    return IdentitySessionsResponse(
        sessions=rows,
        total=total,
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.delete("/sessions/{session_id}", response_model=IdentityRevokeResponse)
async def revoke_session(
    session_id: str,
    request: Request,
    response: Response,
) -> IdentityRevokeResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    _identity, revoked_current = await _run_identity_operation(
        revoke_global_session,
        _config(request),
        session_token=_required_session_cookie(request),
        csrf_token=_required_csrf(request),
        target_session_id=session_id,
        reason="user_requested",
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if revoked_current:
        _clear_identity_cookies(response)
    return IdentityRevokeResponse(
        revoked=True,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/credentials/password/rotate", response_model=IdentityRotateResponse)
async def rotate_password(request: Request, response: Response) -> IdentityRotateResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    payload = await _strict_json(
        request,
        required_fields=("current_password", "new_password"),
    )
    current_password = _required_string(
        payload,
        "current_password",
        minimum=1,
        maximum=1024,
        byte_limit=True,
    )
    new_password = _required_string(
        payload,
        "new_password",
        minimum=8,
        maximum=1024,
        byte_limit=True,
    )
    issued = await _run_identity_operation(
        rotate_global_password,
        _config(request),
        session_token=_required_session_cookie(request),
        csrf_token=_required_csrf(request),
        current_password=current_password,
        new_password=new_password,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        user_agent=request.headers.get("User-Agent"),
        client_host=request.client.host if request.client else None,
    )
    _set_identity_cookies(response, _config(request), issued.session_token, issued.csrf_token)
    return IdentityRotateResponse(
        rotated=True,
        account=issued.account,
        current_session=issued.current_session,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


async def _strict_json(
    request: Request,
    *,
    required_fields: tuple[str, ...],
    optional_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    content_type_values = _raw_header_values(request, b"content-type")
    if (
        len(content_type_values) != 1
        or content_type_values[0].strip().lower() != "application/json"
    ):
        raise ApiError("identity.request_invalid")
    content_length_values = _raw_header_values(request, b"content-length")
    if len(content_length_values) > 1:
        raise ApiError("identity.request_invalid")
    if content_length_values:
        try:
            content_length = int(content_length_values[0])
            if content_length < 0 or content_length > _MAX_IDENTITY_BODY_BYTES:
                raise ApiError("identity.request_invalid")
        except ValueError:
            raise ApiError("identity.request_invalid") from None
    chunks: list[bytes] = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > _MAX_IDENTITY_BODY_BYTES:
            raise ApiError("identity.request_invalid")
        chunks.append(chunk)
    body = b"".join(chunks)
    if not body:
        raise ApiError("identity.request_invalid")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ApiError("identity.request_invalid") from None
    actual_fields = set(payload) if type(payload) is dict else set()
    required = set(required_fields)
    allowed = required | set(optional_fields)
    if (
        type(payload) is not dict
        or not required.issubset(actual_fields)
        or not actual_fields <= allowed
    ):
        raise ApiError("identity.request_invalid")
    return payload


async def _run_identity_operation(function, *args, **kwargs):
    """Keep bounded password KDF and SQLite work off the service event loop."""

    def run_in_request_scope():
        with database_request_scope(reuse_connections=True):
            return function(*args, **kwargs)

    return await anyio.to_thread.run_sync(
        run_in_request_scope,
        limiter=_IDENTITY_WORK_LIMITER,
    )


def _required_string(
    payload: dict[str, Any],
    field: str,
    *,
    minimum: int,
    maximum: int,
    byte_limit: bool = False,
) -> str:
    value = payload.get(field)
    if type(value) is not str:
        raise ApiError("identity.request_invalid")
    try:
        length = len(value.encode("utf-8")) if byte_limit else len(value)
    except UnicodeEncodeError:
        raise ApiError("identity.request_invalid") from None
    if length < minimum or length > maximum:
        raise ApiError("identity.request_invalid")
    return value


def _optional_string(
    payload: dict[str, Any],
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> str | None:
    if field not in payload:
        return None
    return _required_string(
        payload,
        field,
        minimum=minimum,
        maximum=maximum,
    )


def _required_object(payload: dict[str, Any], field: str) -> dict[str, object]:
    value = payload.get(field)
    if type(value) is not dict:
        raise ApiError("identity.request_invalid")
    return value


def _request_origin(request: Request) -> str:
    values = _raw_header_values(request, b"origin")
    if len(values) != 1:
        raise ApiError("identity.origin_invalid")
    return values[0]


def _device_family(user_agent: str | None) -> str:
    normalized = (user_agent or "").lower()
    if any(marker in normalized for marker in ("android", "iphone", "ipad", "mobile")):
        return "mobile"
    if "edg/" in normalized:
        return "edge"
    if "firefox/" in normalized:
        return "firefox"
    if "chrome/" in normalized or "chromium/" in normalized:
        return "chrome"
    if "safari/" in normalized:
        return "safari"
    return "unknown"


def _reject_legacy_authorization(request: Request) -> None:
    if _raw_header_values(request, b"authorization"):
        raise ApiError("identity.credential_invalid")


def _require_write_origin(request: Request) -> None:
    origins = _raw_header_values(request, b"origin")
    if (
        len(origins) != 1
        or not origins[0]
        or "," in origins[0]
        or origins[0] not in _config(request).allowed_origins
    ):
        raise ApiError("identity.origin_invalid")
    fetch_sites = _raw_header_values(request, b"sec-fetch-site")
    if len(fetch_sites) > 1 or (fetch_sites and fetch_sites[0].lower() == "cross-site"):
        raise ApiError("identity.origin_invalid")


def _required_session_cookie(request: Request) -> str:
    value = _single_cookie(request, SESSION_COOKIE, required=True)
    if value is None:
        raise ApiError("identity.session_required")
    return value


def _required_csrf(request: Request) -> str:
    cookie = _single_cookie(request, CSRF_COOKIE, required=True)
    headers = _raw_header_values(request, b"x-csrf-token")
    if (
        cookie is None
        or len(headers) != 1
        or not valid_opaque_value(headers[0])
        or not secrets_compare(cookie, headers[0])
    ):
        raise ApiError("identity.csrf_invalid")
    return cookie


def _single_cookie(request: Request, name: str, *, required: bool) -> str | None:
    values: list[str] = []
    for raw_cookie in _raw_header_values(request, b"cookie"):
        for segment in raw_cookie.split(";"):
            key, separator, value = segment.strip().partition("=")
            if separator and key == name:
                values.append(value)
    if not values:
        if required:
            raise ApiError(
                "identity.session_required" if name == SESSION_COOKIE else "identity.csrf_invalid"
            )
        return None
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError(
            "identity.session_invalid" if name == SESSION_COOKIE else "identity.csrf_invalid"
        )
    return values[0]


def _pagination(request: Request) -> tuple[int, int]:
    items = list(request.query_params.multi_items())
    if any(key not in {"limit", "offset"} for key, _value in items):
        raise ApiError("identity.request_invalid")
    if (
        sum(key == "limit" for key, _value in items) > 1
        or sum(key == "offset" for key, _value in items) > 1
    ):
        raise ApiError("identity.request_invalid")
    try:
        limit = int(request.query_params.get("limit", "20"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        raise ApiError("identity.request_invalid") from None
    if limit < 1 or limit > 100 or offset < 0:
        raise ApiError("identity.request_invalid")
    return limit, offset


def _reject_query(request: Request, allowed: Iterable[str]) -> None:
    allowed_set = set(allowed)
    if any(key not in allowed_set for key, _value in request.query_params.multi_items()):
        raise ApiError("identity.request_invalid")


def _set_identity_cookies(
    response: Response,
    config: AppConfig,
    session_token: str,
    csrf_token: str,
    *,
    max_age: int | None = None,
) -> None:
    cookie_max_age = max_age or config.session_ttl_seconds
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=cookie_max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=cookie_max_age,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def _identity_device_token(request: Request) -> str:
    values: list[str] = []
    for raw_cookie in _raw_header_values(request, b"cookie"):
        for segment in raw_cookie.split(";"):
            key, separator, value = segment.strip().partition("=")
            if separator and key == _DEVICE_COOKIE:
                values.append(value)
    if not values:
        return secrets.token_urlsafe(32)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("identity.credential_invalid")
    return values[0]


def _set_identity_device_cookie(response: Response, device_token: str) -> None:
    response.set_cookie(
        _DEVICE_COOKIE,
        device_token,
        max_age=_DEVICE_COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _pending_response(
    pending: PendingIdentityChallenge,
    request: Request,
) -> IdentityPendingChallengeResponse:
    return IdentityPendingChallengeResponse(
        intent_id=pending.intent_id,
        purpose=pending.purpose,
        status=pending.status,
        expires_at=pending.expires_at,
        resend_available_at=pending.resend_available_at,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _clear_identity_cookies(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )
    response.delete_cookie(
        WORKSPACE_SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        WORKSPACE_CSRF_COOKIE,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def _raw_header_values(request: Request, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for header_name, value in request.scope.get("headers", ())
        if header_name.lower() == name
    ]


def secrets_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _config(request: Request) -> AppConfig:
    return request.app.state.config


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


async def _risk_preflight(
    request: Request,
    action: str,
    account_value: str,
    *,
    device_token: str | None = None,
) -> None:
    subjects = {"account": account_value}
    if request.client and request.client.host:
        subjects["ip_prefix"] = request.client.host
    device = device_token or _single_cookie(request, _DEVICE_COOKIE, required=False)
    if device:
        subjects["device"] = device
    assessment = await runtime_for(request.app.state, _config(request)).assess(
        action,
        subjects,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if assessment.outcome != "allow":
        raise ApiError("risk.challenge_required")
