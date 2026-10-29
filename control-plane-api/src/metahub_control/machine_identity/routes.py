"""Strict Workspace and OAuth transport boundaries for PRD-118 machine identity."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any
from urllib.parse import parse_qsl

import anyio
from fastapi import APIRouter, Request, Response, status
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

from metahub_control.platform_access.constants import (
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.saas_billing.routes import (
    _cookie_values,
    _private,
    _raw_headers,
    _request_id,
    _require_origin,
    _required_cookie,
    _required_csrf,
    _strict_json,
    _trace_id,
)
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)

from .models import (
    OAuthClientCreateRequest,
    OAuthClientCredentialResponse,
    OAuthTokenResponse,
    ServiceAccountCreateRequest,
    ServiceAccountListResponse,
    ServiceAccountResponse,
)
from .service import (
    create_oauth_client,
    create_service_account,
    issue_access_token,
    list_service_accounts,
    revoke_service_account,
    rotate_service_account_credential,
)
from .transport import direct_peer_ip, verified_mtls_fingerprint

router = APIRouter(tags=["machine-identity"])
_MAX_FORM_BYTES = 4096
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_WORK_LIMITER = anyio.CapacityLimiter(4)


@router.get("/service-accounts", response_model=ServiceAccountListResponse)
async def get_service_accounts(request: Request, response: Response) -> ServiceAccountListResponse:
    _prepare_workspace_request(request, write=False)
    _reject_query_and_body(request, "machine_identity.request_invalid")
    result = await _run(
        list_service_accounts,
        request.app.state.config,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _sensitive_response(response)
    return result


@router.post(
    "/service-accounts",
    response_model=ServiceAccountResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_service_account(request: Request, response: Response) -> ServiceAccountResponse:
    _prepare_workspace_request(request, write=True)
    _reject_query(request, "machine_identity.request_invalid")
    payload = _model(
        await _strict_json(request, "machine_identity.request_invalid"),
        ServiceAccountCreateRequest,
    )
    result = await _run(
        create_service_account,
        request.app.state.config,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.service_account.revision)
    _sensitive_response(response)
    return result


@router.post(
    "/oauth/clients",
    response_model=OAuthClientCredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_oauth_client(
    request: Request,
    response: Response,
) -> OAuthClientCredentialResponse:
    _prepare_workspace_request(request, write=True)
    _reject_query(request, "machine_identity.request_invalid")
    payload = _model(
        await _strict_json(request, "machine_identity.request_invalid"),
        OAuthClientCreateRequest,
    )
    result = await _run(
        create_oauth_client,
        request.app.state.config,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.oauth_client.revision)
    _sensitive_response(response)
    return result


@router.post(
    "/service-accounts/{service_account_id}/rotate",
    response_model=OAuthClientCredentialResponse,
)
async def post_service_account_rotation(
    service_account_id: str,
    request: Request,
    response: Response,
) -> OAuthClientCredentialResponse:
    _prepare_workspace_request(request, write=True)
    _reject_query_and_body(request, "machine_identity.request_invalid")
    result = await _run(
        rotate_service_account_credential,
        request.app.state.config,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        service_account_id=service_account_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.oauth_client.revision)
    _sensitive_response(response)
    return result


@router.delete(
    "/service-accounts/{service_account_id}",
    response_model=ServiceAccountResponse,
)
async def delete_service_account(
    service_account_id: str,
    request: Request,
    response: Response,
) -> ServiceAccountResponse:
    _prepare_workspace_request(request, write=True)
    _reject_query_and_body(request, "machine_identity.request_invalid")
    result = await _run(
        revoke_service_account,
        request.app.state.config,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        service_account_id=service_account_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.service_account.revision)
    _sensitive_response(response)
    return result


@router.post("/oauth/token", response_model=OAuthTokenResponse)
async def post_oauth_token(request: Request, response: Response) -> OAuthTokenResponse:
    _reject_query(request, "oauth.invalid_request")
    if _raw_headers(request, b"cookie"):
        raise ApiError("oauth.invalid_client")
    client_id, client_secret = _basic_credentials(request)
    form = await _strict_form(request)
    scope = form["scope"]
    scopes = tuple(scope.split(" "))
    if not scope or scope != " ".join(scopes) or any(not item for item in scopes):
        raise ApiError("oauth.invalid_scope")
    result = await _run(
        issue_access_token,
        request.app.state.config,
        client_id=client_id,
        client_secret=client_secret,
        idempotency_key=_idempotency_key(request, "oauth.invalid_request"),
        audience=form["audience"],
        scopes=scopes,
        peer_ip=direct_peer_ip(request),
        verified_mtls_fingerprint=verified_mtls_fingerprint(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _oauth_response(response)
    return result


def _prepare_workspace_request(request: Request, *, write: bool) -> None:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    _workspace_cookie(request)
    if write:
        _require_origin(request, "machine_identity.request_invalid")
        _workspace_csrf(request)


def _workspace_cookie(request: Request) -> str:
    return _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required")


def _workspace_csrf(request: Request) -> str:
    return _required_csrf(
        request,
        WORKSPACE_CSRF_COOKIE,
        "workspace_session.csrf_invalid",
    )


def _model(payload: dict[str, Any], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("machine_identity.request_invalid") from None


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("machine_identity.request_invalid")
    return int(values[0][1:-1])


def _idempotency_key(
    request: Request, error_code: str = "machine_identity.request_invalid"
) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError(error_code)
    return values[0]


def _basic_credentials(request: Request) -> tuple[str, str]:
    values = _raw_headers(request, b"authorization")
    if len(values) != 1:
        raise ApiError("oauth.invalid_client")
    match = re.fullmatch(r"Basic ([A-Za-z0-9+/]+={0,2})", values[0])
    if match is None:
        raise ApiError("oauth.invalid_client")
    try:
        decoded = base64.b64decode(match.group(1), validate=True).decode(
            "ascii", errors="strict"
        )
    except (binascii.Error, UnicodeDecodeError):
        raise ApiError("oauth.invalid_client") from None
    client_id, separator, client_secret = decoded.partition(":")
    if not separator or not client_id or not client_secret or ":" in client_secret:
        raise ApiError("oauth.invalid_client")
    return client_id, client_secret


async def _strict_form(request: Request) -> dict[str, str]:
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError("oauth.invalid_request")
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/x-www-form-urlencoded" or (
        separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise ApiError("oauth.invalid_request")
    if _raw_headers(request, b"transfer-encoding"):
        raise ApiError("oauth.invalid_request")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError("oauth.invalid_request")
    declared: int | None = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError("oauth.invalid_request")
        declared = int(lengths[0])
        if declared < 1 or declared > _MAX_FORM_BYTES:
            raise ApiError("oauth.invalid_request")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_FORM_BYTES:
            raise ApiError("oauth.invalid_request")
    if not body or (declared is not None and declared != len(body)):
        raise ApiError("oauth.invalid_request")
    try:
        pairs = parse_qsl(
            bytes(body).decode("ascii", errors="strict"),
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=4,
            separator="&",
        )
    except (UnicodeDecodeError, ValueError):
        raise ApiError("oauth.invalid_request") from None
    if len(pairs) != 3 or len({key for key, _value in pairs}) != 3:
        raise ApiError("oauth.invalid_request")
    form = dict(pairs)
    if set(form) != {"grant_type", "audience", "scope"}:
        raise ApiError("oauth.invalid_request")
    if form["grant_type"] != "client_credentials":
        raise ApiError("oauth.invalid_request")
    return form


def _reject_query(request: Request, error_code: str) -> None:
    if request.query_params.multi_items():
        raise ApiError(error_code)


def _reject_query_and_body(request: Request, error_code: str) -> None:
    _reject_query(request, error_code)
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError(error_code)
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError(error_code)


def _etag(revision: int) -> str:
    return f'"{revision}"'


def _sensitive_response(response: Response) -> None:
    _private(response)
    response.headers["Pragma"] = "no-cache"


def _oauth_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Authorization"


async def _run(function, *args, **kwargs):
    """Run bounded machine-identity database work on one reusable request handle."""

    def run_in_request_scope():
        with database_request_scope(reuse_connections=True):
            return function(*args, **kwargs)

    return await anyio.to_thread.run_sync(
        run_in_request_scope,
        limiter=_WORK_LIMITER,
    )


__all__ = ["router"]
