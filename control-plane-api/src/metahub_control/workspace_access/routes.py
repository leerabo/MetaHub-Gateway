"""Strict browser transport for PRD-094 Workspace authorization."""

from __future__ import annotations

import json
import secrets
from typing import Any

import anyio
from fastapi import APIRouter, Request, Response, status
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

from metahub_control.identity.service import (
    CSRF_COOKIE as GLOBAL_CSRF_COOKIE,
)
from metahub_control.identity.service import (
    SESSION_COOKIE as GLOBAL_SESSION_COOKIE,
)
from metahub_control.identity.service import valid_opaque_value

from .constants import WORKSPACE_CSRF_COOKIE, WORKSPACE_SESSION_COOKIE
from .models import (
    IdentityWorkspaceListResponse,
    MembershipInvitationResponse,
    MembershipListResponse,
    MembershipResponse,
    OwnerTransferResponse,
    WorkspaceSessionResponse,
    WorkspaceSwitchResponse,
)
from .service import (
    create_membership_invitation,
    get_workspace_session,
    list_identity_workspaces,
    list_memberships,
    switch_workspace,
    transfer_owner,
    update_membership,
)

router = APIRouter(tags=["workspace-access"])
_MAX_BODY_BYTES = 4096
_WORK_LIMITER = anyio.CapacityLimiter(4)


@router.get("/identity/workspaces", response_model=IdentityWorkspaceListResponse)
async def get_identity_workspaces(request: Request) -> IdentityWorkspaceListResponse:
    _reject_authorization(request)
    limit, offset = _pagination(request, "identity.request_invalid")
    return await _run(
        list_identity_workspaces,
        _config(request),
        session_token=_required_cookie(
            request,
            GLOBAL_SESSION_COOKIE,
            missing_code="identity.session_required",
            invalid_code="identity.session_invalid",
        ),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/identity/workspace-switches", response_model=WorkspaceSwitchResponse)
async def post_workspace_switch(request: Request, response: Response) -> WorkspaceSwitchResponse:
    _reject_authorization(request)
    _require_write_origin(request)
    _reject_query(request, "identity.request_invalid")
    payload = await _strict_json(request, "identity.request_invalid")
    if set(payload) != {"workspace_id"}:
        raise ApiError("identity.request_invalid")
    workspace_id = _required_string(payload, "workspace_id", 36, "identity.request_invalid")
    result, session_token, csrf_token = await _run(
        switch_workspace,
        _config(request),
        global_session_token=_required_cookie(
            request,
            GLOBAL_SESSION_COOKIE,
            missing_code="identity.session_required",
            invalid_code="identity.session_invalid",
        ),
        global_csrf_token=_required_csrf(
            request,
            GLOBAL_CSRF_COOKIE,
            "identity.csrf_invalid",
        ),
        workspace_id=workspace_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _set_workspace_cookies(response, _config(request), session_token, csrf_token)
    return result


@router.get("/workspace/session", response_model=WorkspaceSessionResponse)
async def get_current_workspace_session(request: Request) -> WorkspaceSessionResponse:
    _prepare_workspace_request(request)
    _reject_query(request, "membership.request_invalid")
    return await _run(
        get_workspace_session,
        _config(request),
        session_token=_required_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/memberships", response_model=MembershipListResponse)
async def get_memberships(request: Request) -> MembershipListResponse:
    _prepare_workspace_request(request)
    limit, offset = _pagination(request, "membership.request_invalid")
    return await _run(
        list_memberships,
        _config(request),
        session_token=_required_workspace_cookie(request),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/memberships/{membership_id}", response_model=MembershipResponse)
async def patch_membership(
    membership_id: str,
    request: Request,
    response: Response,
) -> MembershipResponse:
    _prepare_workspace_request(request)
    _require_write_origin(request)
    _reject_query(request, "membership.request_invalid")
    expected_revision = _membership_if_match(request)
    payload = await _strict_json(request, "membership.request_invalid")
    if not payload or not set(payload) <= {"role", "status"}:
        raise ApiError("membership.request_invalid")
    role = _optional_string(payload, "role", 32, "membership.request_invalid")
    membership_status = _optional_string(payload, "status", 16, "membership.request_invalid")
    result = await _run(
        update_membership,
        _config(request),
        session_token=_required_workspace_cookie(request),
        csrf_token=_required_csrf(
            request,
            WORKSPACE_CSRF_COOKIE,
            "workspace_session.csrf_invalid",
        ),
        membership_id=membership_id,
        expected_revision=expected_revision,
        role=role,
        status=membership_status,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.membership.revision}"'
    return result


@router.post(
    "/membership-invitations",
    response_model=MembershipInvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_membership_invitation(request: Request) -> MembershipInvitationResponse:
    _prepare_workspace_request(request)
    _require_write_origin(request)
    _reject_query(request, "membership.invitation_invalid")
    payload = await _strict_json(request, "membership.invitation_invalid")
    if not {"email", "role"} <= set(payload) <= {"email", "role", "expires_in_hours"}:
        raise ApiError("membership.invitation_invalid")
    expires_in_hours = payload.get("expires_in_hours", 72)
    if type(expires_in_hours) is not int:
        raise ApiError("membership.invitation_invalid")
    return await _run(
        create_membership_invitation,
        _config(request),
        session_token=_required_workspace_cookie(request),
        csrf_token=_required_csrf(
            request,
            WORKSPACE_CSRF_COOKIE,
            "workspace_session.csrf_invalid",
        ),
        email=_required_string(payload, "email", 320, "membership.invitation_invalid"),
        role=_required_string(payload, "role", 32, "membership.invitation_invalid"),
        expires_in_hours=expires_in_hours,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/workspace-owner-transfers", response_model=OwnerTransferResponse)
async def post_owner_transfer(request: Request) -> OwnerTransferResponse:
    _prepare_workspace_request(request)
    _require_write_origin(request)
    _reject_query(request, "owner_transfer.request_invalid")
    payload = await _strict_json(request, "owner_transfer.request_invalid")
    if set(payload) != {"target_membership_id", "current_password"}:
        raise ApiError("owner_transfer.request_invalid")
    return await _run(
        transfer_owner,
        _config(request),
        session_token=_required_workspace_cookie(request),
        csrf_token=_required_csrf(
            request,
            WORKSPACE_CSRF_COOKIE,
            "workspace_session.csrf_invalid",
        ),
        target_membership_id=_required_string(
            payload,
            "target_membership_id",
            36,
            "owner_transfer.request_invalid",
        ),
        current_password=_required_string(
            payload,
            "current_password",
            1024,
            "owner_transfer.request_invalid",
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _prepare_workspace_request(request: Request) -> None:
    clear_workspace_cookies_on_error(request)


async def _strict_json(request: Request, error_code: str) -> dict[str, Any]:
    content_types = _raw_header_values(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError(error_code)
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/json":
        raise ApiError(error_code)
    if separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}:
        raise ApiError(error_code)
    if _raw_header_values(request, b"transfer-encoding"):
        raise ApiError(error_code)
    lengths = _raw_header_values(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError(error_code)
    declared: int | None = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError(error_code)
        declared = int(lengths[0])
        if declared < 2 or declared > _MAX_BODY_BYTES:
            raise ApiError(error_code)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise ApiError(error_code)
    if not body or (declared is not None and declared != len(body)):
        raise ApiError(error_code)

    def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        payload = json.loads(bytes(body).decode(), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ApiError(error_code) from None
    if type(payload) is not dict:
        raise ApiError(error_code)
    return payload


def _required_string(
    payload: dict[str, Any],
    field: str,
    maximum: int,
    error_code: str,
) -> str:
    value = payload.get(field)
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ApiError(error_code)
    return value


def _optional_string(
    payload: dict[str, Any],
    field: str,
    maximum: int,
    error_code: str,
) -> str | None:
    if field not in payload:
        return None
    return _required_string(payload, field, maximum, error_code)


def _reject_authorization(request: Request) -> None:
    if _raw_header_values(request, b"authorization"):
        raise ApiError("identity.credential_invalid")


def _required_workspace_cookie(request: Request) -> str:
    cookie = _cookie(
        request,
        WORKSPACE_SESSION_COOKIE,
        invalid_code="workspace_session.invalid",
    )
    if cookie is not None and _raw_header_values(request, b"authorization"):
        raise ApiError("workspace_session.credential_conflict")
    if cookie is None:
        raise ApiError("workspace_session.required")
    return cookie


def _required_cookie(
    request: Request,
    name: str,
    *,
    missing_code: str,
    invalid_code: str,
) -> str:
    value = _cookie(request, name, invalid_code=invalid_code)
    if value is None:
        raise ApiError(missing_code)
    return value


def _cookie(request: Request, name: str, *, invalid_code: str) -> str | None:
    values: list[str] = []
    for raw_cookie in _raw_header_values(request, b"cookie"):
        for segment in raw_cookie.split(";"):
            key, separator, value = segment.strip().partition("=")
            if separator and key == name:
                values.append(value)
    if not values:
        return None
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError(invalid_code)
    return values[0]


def _required_csrf(request: Request, cookie_name: str, error_code: str) -> str:
    cookie = _cookie(request, cookie_name, invalid_code=error_code)
    headers = _raw_header_values(request, b"x-csrf-token")
    if (
        cookie is None
        or len(headers) != 1
        or not valid_opaque_value(headers[0])
        or not secrets.compare_digest(cookie, headers[0])
    ):
        raise ApiError(error_code)
    return cookie


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


def _membership_if_match(request: Request) -> int:
    values = _raw_header_values(request, b"if-match")
    if not values:
        raise ApiError("membership.revision_required")
    if len(values) != 1:
        raise ApiError("membership.request_invalid")
    value = values[0]
    if len(value) < 3 or value[0] != '"' or value[-1] != '"' or not value[1:-1].isdigit():
        raise ApiError("membership.request_invalid")
    return int(value[1:-1])


def _pagination(request: Request, error_code: str) -> tuple[int, int]:
    items = list(request.query_params.multi_items())
    if any(key not in {"limit", "offset"} for key, _value in items):
        raise ApiError(error_code)
    if sum(key == "limit" for key, _value in items) > 1 or sum(
        key == "offset" for key, _value in items
    ) > 1:
        raise ApiError(error_code)
    try:
        limit = int(request.query_params.get("limit", "20"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        raise ApiError(error_code) from None
    if limit < 1 or limit > 100 or offset < 0:
        raise ApiError(error_code)
    return limit, offset


def _reject_query(request: Request, error_code: str) -> None:
    if request.query_params.multi_items():
        raise ApiError(error_code)


def _set_workspace_cookies(
    response: Response,
    config,
    session_token: str,
    csrf_token: str,
) -> None:
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


def _raw_header_values(request: Request, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for header_name, value in request.scope.get("headers", ())
        if header_name.lower() == name
    ]


async def _run(function, *args, **kwargs):
    def run_in_request_scope():
        with database_request_scope(reuse_connections=True):
            return function(*args, **kwargs)

    return await anyio.to_thread.run_sync(
        run_in_request_scope,
        limiter=_WORK_LIMITER,
    )


def _config(request: Request):
    return request.app.state.config


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")
