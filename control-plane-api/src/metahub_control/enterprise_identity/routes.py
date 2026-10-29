"""Cookie/CSRF transport for enterprise domain and SSO APIs."""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request, Response, status
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

from metahub_control.identity.service import CSRF_COOKIE as GLOBAL_CSRF_COOKIE
from metahub_control.identity.service import SESSION_COOKIE as GLOBAL_SESSION_COOKIE
from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_context,
)

from .models import (
    DomainCreateRequest,
    DomainCreateResponse,
    DomainResponse,
    EnterpriseIdentitySummaryResponse,
    LoginDiscoveryResponse,
    SsoCallbackRequest,
    SsoCallbackResponse,
    SsoConnectionCreateRequest,
    SsoConnectionResponse,
    SsoPolicyResponse,
    SsoPolicyUpdateRequest,
    SsoTestRequest,
    SsoTestResponse,
)
from .service import (
    complete_sso_callback,
    create_sso_connection,
    create_verified_domain,
    discover_login,
    get_enterprise_identity_summary,
    test_sso_connection,
    update_sso_policy,
    verify_domain,
)

router = APIRouter(tags=["enterprise-identity"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_ETAG_RE = re.compile(r'"(?:0|[1-9][0-9]*)"')
_MAX_BODY_BYTES = 180 * 1024
_SSO_BROWSER_COOKIE = "__Host-metahub-sso-browser"
_WORK_LIMITER = anyio.CapacityLimiter(4)


def require_enterprise_identity_context(request: Request) -> WorkspaceSessionContext:
    """Authenticate and authorize the Workspace before parsing any management body."""

    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    manage = request.method not in {"GET", "HEAD", "OPTIONS"}
    if manage:
        _require_origin(request)
        csrf = _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )
    if context.workspace_id != request.path_params.get("workspace_id"):
        raise ApiError("sso.not_found")
    roles = {"owner", "security_admin"} if manage else {"owner", "security_admin", "auditor"}
    permission = "enterprise_sso.manage" if manage else "enterprise_sso.read"
    if context.membership_role not in roles or permission not in context.permission_codes:
        raise ApiError("workspace_session.permission_denied")
    return context


@router.get(
    "/workspaces/{workspace_id}/enterprise-identity",
    response_model=EnterpriseIdentitySummaryResponse,
)
async def get_enterprise_identity(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_enterprise_identity_context)],
) -> EnterpriseIdentitySummaryResponse:
    _reject_query(request)
    result = await _run(
        get_enterprise_identity_summary,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        workspace_id=workspace_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/workspaces/{workspace_id}/domains",
    response_model=DomainCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_domain(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_enterprise_identity_context)],
) -> DomainCreateResponse:
    _reject_query(request)
    payload = _validate(DomainCreateRequest, await _strict_json(request))
    result = await _run(
        create_verified_domain,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        workspace_id=workspace_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.domain.revision}"'
    _private(response)
    return result


@router.post(
    "/workspaces/{workspace_id}/domains/{domain_id}/verify",
    response_model=DomainResponse,
)
async def post_domain_verify(
    workspace_id: str,
    domain_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_enterprise_identity_context)],
) -> DomainResponse:
    _reject_query(request)
    _reject_body(request)
    result = await _run(
        verify_domain,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        workspace_id=workspace_id,
        domain_id=domain_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.domain.revision}"'
    _private(response)
    return result


@router.post(
    "/workspaces/{workspace_id}/sso-connections",
    response_model=SsoConnectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_sso_connection(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_enterprise_identity_context)],
) -> SsoConnectionResponse:
    _reject_query(request)
    payload = _validate(SsoConnectionCreateRequest, await _strict_json(request))
    result = await _run(
        create_sso_connection,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        workspace_id=workspace_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.connection.revision}"'
    _private(response)
    return result


@router.post(
    "/workspaces/{workspace_id}/sso/test",
    response_model=SsoTestResponse,
)
async def post_sso_test(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_enterprise_identity_context)],
) -> SsoTestResponse:
    _reject_query(request)
    payload = _validate(SsoTestRequest, await _strict_json(request))
    result = await _run(
        test_sso_connection,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        workspace_id=workspace_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.connection.revision}"'
    _private(response)
    return result


@router.patch(
    "/workspaces/{workspace_id}/sso-policy",
    response_model=SsoPolicyResponse,
)
async def patch_sso_policy(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_enterprise_identity_context)],
) -> SsoPolicyResponse:
    _reject_query(request)
    payload = _validate(SsoPolicyUpdateRequest, await _strict_json(request))
    result = await _run(
        update_sso_policy,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        workspace_id=workspace_id,
        expected_revision=_if_match(request, allow_zero=True),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.policy.revision}"'
    _private(response)
    return result


@router.get("/identity/login-discovery", response_model=LoginDiscoveryResponse)
async def get_login_discovery(request: Request, response: Response) -> LoginDiscoveryResponse:
    _reject_public_credential_mix(request)
    items = request.query_params.multi_items()
    if len(items) != 1 or items[0][0] not in {"email", "domain"} or not items[0][1]:
        raise ApiError("sso.request_invalid")
    browser_token = _optional_browser_token(request) or secrets.token_urlsafe(32)
    result = await _run(
        discover_login,
        request.app.state.config,
        email_or_domain=items[0][1],
        browser_token=browser_token,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.set_cookie(
        _SSO_BROWSER_COOKIE,
        browser_token,
        max_age=600,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    _private(response)
    return result


@router.post("/identity/sso/callback", response_model=SsoCallbackResponse)
async def post_sso_callback(request: Request, response: Response) -> SsoCallbackResponse:
    _reject_public_credential_mix(request)
    _require_origin(request)
    _reject_query(request)
    payload = _validate(SsoCallbackRequest, await _strict_json(request))
    browser_token = _required_browser_token(request)
    bundle = await _run(
        complete_sso_callback,
        request.app.state.config,
        payload=payload,
        browser_token=browser_token,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        user_agent=request.headers.get("User-Agent"),
        client_host=request.client.host if request.client else None,
    )
    _set_identity_cookies(
        response,
        request.app.state.config,
        bundle.global_session_token,
        bundle.global_csrf_token,
    )
    _set_workspace_cookies(
        response,
        request.app.state.config,
        bundle.workspace_session_token,
        bundle.workspace_csrf_token,
    )
    response.delete_cookie(
        PLATFORM_SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    response.delete_cookie(
        PLATFORM_CSRF_COOKIE, path="/", secure=True, httponly=False, samesite="strict"
    )
    response.delete_cookie(
        SUPPORT_SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    response.delete_cookie(
        _SSO_BROWSER_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    _private(response)
    return bundle.response


def _validate(model, payload: dict[str, Any]):
    try:
        return model.model_validate(payload)
    except ValueError:
        raise ApiError("sso.request_invalid") from None


async def _strict_json(request: Request) -> dict[str, Any]:
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError("sso.request_invalid")
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/json" or (
        separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise ApiError("sso.request_invalid")
    if _raw_headers(request, b"transfer-encoding"):
        raise ApiError("sso.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError("sso.request_invalid")
    declared = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError("sso.request_invalid")
        declared = int(lengths[0])
        if declared < 2 or declared > _MAX_BODY_BYTES:
            raise ApiError("sso.request_invalid")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise ApiError("sso.request_invalid")
    if not body or (declared is not None and declared != len(body)):
        raise ApiError("sso.request_invalid")

    def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ApiError("sso.request_invalid") from None
    if type(value) is not dict:
        raise ApiError("sso.request_invalid")
    return value


def _required_cookie(request: Request, name: str, error_code: str) -> str:
    values = _cookie_values(request, name)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError(error_code)
    return values[0]


def _required_csrf(request: Request, name: str, error_code: str) -> str:
    cookies = _cookie_values(request, name)
    headers = _raw_headers(request, b"x-csrf-token")
    if (
        len(cookies) != 1
        or len(headers) != 1
        or not valid_opaque_value(cookies[0])
        or not valid_opaque_value(headers[0])
        or not secrets.compare_digest(cookies[0], headers[0])
    ):
        raise ApiError(error_code)
    return cookies[0]


def _optional_browser_token(request: Request) -> str | None:
    values = _cookie_values(request, _SSO_BROWSER_COOKIE)
    if not values:
        return None
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("sso.state_invalid")
    return values[0]


def _required_browser_token(request: Request) -> str:
    value = _optional_browser_token(request)
    if value is None:
        raise ApiError("sso.state_invalid")
    return value


def _reject_public_credential_mix(request: Request) -> None:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
    ):
        raise ApiError("sso.request_invalid")


def _require_origin(request: Request) -> None:
    origins = _raw_headers(request, b"origin")
    sites = _raw_headers(request, b"sec-fetch-site")
    if (
        len(origins) != 1
        or "," in origins[0]
        or origins[0] not in request.app.state.config.allowed_origins
        or len(sites) > 1
        or (sites and sites[0].lower() == "cross-site")
    ):
        raise ApiError("sso.origin_invalid")


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("sso.idempotency_invalid")
    return values[0]


def _if_match(request: Request, *, allow_zero: bool = False) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or _ETAG_RE.fullmatch(values[0]) is None:
        raise ApiError("sso.revision_required")
    result = int(values[0][1:-1])
    if result == 0 and not allow_zero:
        raise ApiError("sso.revision_required")
    return result


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("sso.request_invalid")


def _reject_body(request: Request) -> None:
    lengths = _raw_headers(request, b"content-length")
    if _raw_headers(request, b"transfer-encoding") or len(lengths) > 1:
        raise ApiError("sso.request_invalid")
    if lengths and lengths[0] != "0":
        raise ApiError("sso.request_invalid")


def _cookie_values(request: Request, name: str) -> list[str]:
    values: list[str] = []
    for raw_cookie in _raw_headers(request, b"cookie"):
        for segment in raw_cookie.split(";"):
            key, separator, value = segment.strip().partition("=")
            if separator and key == name:
                values.append(value)
    return values


def _raw_headers(request: Request, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", ())
        if key.lower() == name
    ]


def _set_identity_cookies(response: Response, config, session_token: str, csrf_token: str) -> None:
    response.set_cookie(
        GLOBAL_SESSION_COOKIE,
        session_token,
        max_age=config.session_ttl_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        GLOBAL_CSRF_COOKIE,
        csrf_token,
        max_age=config.session_ttl_seconds,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def _set_workspace_cookies(response: Response, config, session_token: str, csrf_token: str) -> None:
    response.set_cookie(
        WORKSPACE_SESSION_COOKIE,
        session_token,
        max_age=config.workspace_session_ttl_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        WORKSPACE_CSRF_COOKIE,
        csrf_token,
        max_age=config.workspace_session_ttl_seconds,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


async def _run(function, *args, **kwargs):
    def run_in_request_scope():
        with database_request_scope(reuse_connections=True):
            return function(*args, **kwargs)

    return await anyio.to_thread.run_sync(
        run_in_request_scope, limiter=_WORK_LIMITER
    )


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")
