"""Workspace administration and cookie-free RFC 7644 SCIM transport."""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error
from metahub_shared.scim_directory import SCIM_ERROR_SCHEMA

from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
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

from .scim_models import (
    ScimConflictResolveRequest,
    ScimConflictResponse,
    ScimDirectorySummaryResponse,
    ScimRoleMappingRequest,
    ScimRoleMappingResponse,
    ScimSyncListResponse,
    ScimTokenCreateRequest,
    ScimTokenResponse,
)
from .scim_service import (
    ScimServiceError,
    create_scim_group,
    create_scim_token,
    create_scim_user,
    get_scim_directory_summary,
    get_scim_group,
    get_scim_user,
    list_scim_groups,
    list_scim_syncs,
    list_scim_users,
    patch_scim_group,
    patch_scim_user,
    resolve_scim_conflict,
    rotate_scim_token,
    upsert_scim_role_mapping,
    validate_page,
)

router = APIRouter(tags=["scim-directory"])

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$", re.ASCII)
_ETAG_RE = re.compile(r'"(?:0|[1-9][0-9]*)"', re.ASCII)
_SCIM_ETAG_RE = re.compile(r'(?:W/)?"[1-9][0-9]*"', re.ASCII)
_RESOURCE_ID_RE = re.compile(r"^(?:sct|sul|sgl|scf)_[0-9a-f]{32}$", re.ASCII)
_PAGE_RE = re.compile(r"^(?:0|[1-9][0-9]{0,9})$", re.ASCII)
_MAX_BODY_BYTES = 256 * 1024
_SCIM_MEDIA_TYPE = "application/scim+json"
_WORK_LIMITER = anyio.CapacityLimiter(4)


def require_scim_admin_context(request: Request) -> WorkspaceSessionContext:
    """Authenticate the Workspace and SCIM permission before reading a management body."""

    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    manage = request.method not in {"GET", "HEAD", "OPTIONS"}
    csrf = None
    if manage:
        _require_origin(request)
        csrf = _required_csrf(request)
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )
    if context.workspace_id != request.path_params.get("workspace_id"):
        raise ApiError("scim.not_found")
    permission = "scim.manage" if manage else "scim.read"
    if permission not in context.permission_codes:
        raise ApiError("workspace_session.permission_denied")
    return context


@router.get(
    "/workspaces/{workspace_id}/scim-directory",
    response_model=ScimDirectorySummaryResponse,
)
async def get_scim_directory(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_scim_admin_context)],
) -> ScimDirectorySummaryResponse:
    _reject_query(request)
    result = await _run(
        get_scim_directory_summary,
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


@router.get(
    "/workspaces/{workspace_id}/scim-syncs",
    response_model=ScimSyncListResponse,
)
async def get_scim_syncs(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_scim_admin_context)],
) -> ScimSyncListResponse:
    _reject_query(request)
    result = await _run(
        list_scim_syncs,
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
    "/workspaces/{workspace_id}/scim-tokens",
    response_model=ScimTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_scim_token(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_scim_admin_context)],
) -> ScimTokenResponse:
    _reject_query(request)
    payload = _admin_validate(ScimTokenCreateRequest, await _strict_json(request, admin=True))
    result = await _run(
        create_scim_token,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(request),
        workspace_id=workspace_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.token.revision}"'
    _private(response)
    return result


@router.post(
    "/workspaces/{workspace_id}/scim-tokens/{token_id}/rotate",
    response_model=ScimTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_scim_token_rotation(
    workspace_id: str,
    token_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_scim_admin_context)],
) -> ScimTokenResponse:
    _reject_query(request)
    _resource_id(token_id, "sct_")
    payload = _admin_validate(ScimTokenCreateRequest, await _strict_json(request, admin=True))
    result = await _run(
        rotate_scim_token,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(request),
        workspace_id=workspace_id,
        token_id=token_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.token.revision}"'
    _private(response)
    return result


@router.put(
    "/workspaces/{workspace_id}/scim-role-mappings",
    response_model=ScimRoleMappingResponse,
)
async def put_scim_role_mapping(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_scim_admin_context)],
) -> ScimRoleMappingResponse:
    _reject_query(request)
    payload = _admin_validate(ScimRoleMappingRequest, await _strict_json(request, admin=True))
    result = await _run(
        upsert_scim_role_mapping,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(request),
        workspace_id=workspace_id,
        expected_revision=_if_match(request, allow_zero=True),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.mapping.revision}"'
    _private(response)
    return result


@router.post(
    "/workspaces/{workspace_id}/scim-conflicts/{conflict_id}/resolve",
    response_model=ScimConflictResponse,
)
async def post_scim_conflict_resolution(
    workspace_id: str,
    conflict_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_scim_admin_context)],
) -> ScimConflictResponse:
    _reject_query(request)
    _resource_id(conflict_id, "scf_")
    payload = _admin_validate(
        ScimConflictResolveRequest, await _strict_json(request, admin=True)
    )
    result = await _run(
        resolve_scim_conflict,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(request),
        workspace_id=workspace_id,
        conflict_id=conflict_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.conflict.revision}"'
    _private(response)
    return result


@router.get("/scim/v2/Users")
async def get_scim_users(request: Request) -> JSONResponse:
    try:
        bearer = _machine_bearer(request)
        filter_text, start_index, count = _list_query(request)
        result = await _run(
            list_scim_users,
            request.app.state.config,
            bearer_token=bearer,
            filter_text=filter_text,
            start_index=start_index,
            count=count,
        )
        return _scim_json(result)
    except ScimServiceError as error:
        return _scim_error(error)


@router.get("/scim/v2/Users/{resource_id}")
async def get_one_scim_user(resource_id: str, request: Request) -> JSONResponse:
    try:
        _reject_machine_query(request)
        bearer = _machine_bearer(request)
        _machine_resource_id(resource_id, "sul_")
        result = await _run(
            get_scim_user,
            request.app.state.config,
            bearer_token=bearer,
            resource_id=resource_id,
        )
        return _scim_json(result, etag=_resource_etag(result))
    except ScimServiceError as error:
        return _scim_error(error)


@router.post("/scim/v2/Users")
async def post_scim_user(request: Request) -> JSONResponse:
    try:
        _reject_machine_query(request)
        bearer = _machine_bearer(request)
        payload = await _strict_json(request, admin=False)
        result = await _run(
            create_scim_user,
            request.app.state.config,
            bearer_token=bearer,
            payload=payload,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        return _scim_json(
            result,
            status_code=status.HTTP_201_CREATED,
            etag=_resource_etag(result),
            location=f"/scim/v2/Users/{result['id']}",
        )
    except ScimServiceError as error:
        return _scim_error(error)


@router.patch("/scim/v2/Users/{resource_id}")
async def patch_one_scim_user(resource_id: str, request: Request) -> JSONResponse:
    try:
        _reject_machine_query(request)
        bearer = _machine_bearer(request)
        _machine_resource_id(resource_id, "sul_")
        expected_revision = _machine_if_match(request)
        payload = await _strict_json(request, admin=False)
        result = await _run(
            patch_scim_user,
            request.app.state.config,
            bearer_token=bearer,
            resource_id=resource_id,
            expected_revision=expected_revision,
            payload=payload,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        return _scim_json(result, etag=_resource_etag(result))
    except ScimServiceError as error:
        return _scim_error(error)


@router.get("/scim/v2/Groups")
async def get_scim_groups(request: Request) -> JSONResponse:
    try:
        bearer = _machine_bearer(request)
        filter_text, start_index, count = _list_query(request)
        result = await _run(
            list_scim_groups,
            request.app.state.config,
            bearer_token=bearer,
            filter_text=filter_text,
            start_index=start_index,
            count=count,
        )
        return _scim_json(result)
    except ScimServiceError as error:
        return _scim_error(error)


@router.get("/scim/v2/Groups/{resource_id}")
async def get_one_scim_group(resource_id: str, request: Request) -> JSONResponse:
    try:
        _reject_machine_query(request)
        bearer = _machine_bearer(request)
        _machine_resource_id(resource_id, "sgl_")
        result = await _run(
            get_scim_group,
            request.app.state.config,
            bearer_token=bearer,
            resource_id=resource_id,
        )
        return _scim_json(result, etag=_resource_etag(result))
    except ScimServiceError as error:
        return _scim_error(error)


@router.post("/scim/v2/Groups")
async def post_scim_group(request: Request) -> JSONResponse:
    try:
        _reject_machine_query(request)
        bearer = _machine_bearer(request)
        payload = await _strict_json(request, admin=False)
        result = await _run(
            create_scim_group,
            request.app.state.config,
            bearer_token=bearer,
            payload=payload,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        return _scim_json(
            result,
            status_code=status.HTTP_201_CREATED,
            etag=_resource_etag(result),
            location=f"/scim/v2/Groups/{result['id']}",
        )
    except ScimServiceError as error:
        return _scim_error(error)


@router.patch("/scim/v2/Groups/{resource_id}")
async def patch_one_scim_group(resource_id: str, request: Request) -> JSONResponse:
    try:
        _reject_machine_query(request)
        bearer = _machine_bearer(request)
        _machine_resource_id(resource_id, "sgl_")
        expected_revision = _machine_if_match(request)
        payload = await _strict_json(request, admin=False)
        result = await _run(
            patch_scim_group,
            request.app.state.config,
            bearer_token=bearer,
            resource_id=resource_id,
            expected_revision=expected_revision,
            payload=payload,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        return _scim_json(result, etag=_resource_etag(result))
    except ScimServiceError as error:
        return _scim_error(error)


async def _strict_json(request: Request, *, admin: bool) -> dict[str, Any]:
    error = (
        ApiError("scim.request_invalid")
        if admin
        else ScimServiceError(400, "invalidSyntax", "SCIM request body is invalid.")
    )
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1:
        raise error
    expected = "application/json" if admin else _SCIM_MEDIA_TYPE
    media_type, separator, parameters = content_types[0].partition(";")
    allowed_parameter = admin and separator and parameters.strip().lower() in {
        "charset=utf-8",
        'charset="utf-8"',
    }
    if media_type.strip().lower() != expected or (separator and not allowed_parameter):
        raise error
    if _raw_headers(request, b"transfer-encoding"):
        raise error
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise error
    declared = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise error
        declared = int(lengths[0])
        if declared < 2 or declared > _MAX_BODY_BYTES:
            raise error
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise error
    if not body or (declared is not None and declared != len(body)):
        raise error

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
        raise error from None
    if type(value) is not dict:
        raise error
    return value


def _admin_validate(model, payload: dict[str, Any]):
    try:
        return model.model_validate(payload)
    except ValueError:
        raise ApiError("scim.request_invalid") from None


def _machine_bearer(request: Request) -> str:
    if _raw_headers(request, b"cookie") or _raw_headers(request, b"x-csrf-token"):
        raise ScimServiceError(400, "invalidValue", "SCIM credential transport is invalid.")
    values = _raw_headers(request, b"authorization")
    if len(values) != 1:
        raise ScimServiceError(401, None, "SCIM bearer is required.")
    scheme, separator, token = values[0].partition(" ")
    if separator != " " or scheme.casefold() != "bearer" or not token or token != token.strip():
        raise ScimServiceError(401, None, "SCIM bearer is invalid.")
    return token


def _list_query(request: Request) -> tuple[str | None, int, int]:
    allowed = {"filter", "startIndex", "count"}
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in values or not value:
            raise ScimServiceError(400, "invalidValue", "SCIM query is invalid.")
        values[key] = value
    for name in ("startIndex", "count"):
        if name in values and _PAGE_RE.fullmatch(values[name]) is None:
            raise ScimServiceError(400, "invalidValue", "SCIM pagination is invalid.")
    start, count = validate_page(values.get("startIndex"), values.get("count"))
    return values.get("filter"), start, count


def _reject_machine_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ScimServiceError(400, "invalidValue", "SCIM query is invalid.")


def _machine_if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or _SCIM_ETAG_RE.fullmatch(values[0]) is None:
        raise ScimServiceError(428, "mutability", "SCIM resource version is required.")
    return int(values[0][values[0].find('"') + 1 : -1])


def _machine_resource_id(value: str, prefix: str) -> None:
    if not value.startswith(prefix) or _RESOURCE_ID_RE.fullmatch(value) is None:
        raise ScimServiceError(404, None, "SCIM resource was not found.")


def _resource_etag(result: dict[str, object]) -> str:
    meta = result.get("meta")
    if type(meta) is not dict or not isinstance(meta.get("version"), str):
        raise ScimServiceError(503, None, "SCIM service is temporarily unavailable.")
    return str(meta["version"])


def _scim_json(
    payload: dict[str, object],
    *,
    status_code: int = 200,
    etag: str | None = None,
    location: str | None = None,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if etag is not None:
        headers["ETag"] = etag
    if location is not None:
        headers["Location"] = location
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers=headers,
        media_type=_SCIM_MEDIA_TYPE,
    )


def _scim_error(error: ScimServiceError) -> JSONResponse:
    payload: dict[str, object] = {
        "schemas": [SCIM_ERROR_SCHEMA],
        "status": str(error.status),
        "detail": error.detail,
    }
    if error.scim_type is not None:
        payload["scimType"] = error.scim_type
    headers = {"Cache-Control": "no-store"}
    if error.status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="MetaHub SCIM"'
    return JSONResponse(
        status_code=error.status,
        content=payload,
        headers=headers,
        media_type=_SCIM_MEDIA_TYPE,
    )


def _required_cookie(request: Request, name: str, error_code: str) -> str:
    values = _cookie_values(request, name)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError(error_code)
    return values[0]


def _required_csrf(request: Request) -> str:
    cookies = _cookie_values(request, WORKSPACE_CSRF_COOKIE)
    headers = _raw_headers(request, b"x-csrf-token")
    if (
        len(cookies) != 1
        or len(headers) != 1
        or not valid_opaque_value(cookies[0])
        or not valid_opaque_value(headers[0])
        or not secrets.compare_digest(cookies[0], headers[0])
    ):
        raise ApiError("workspace_session.csrf_invalid")
    return cookies[0]


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
        raise ApiError("scim.origin_invalid")


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("scim.idempotency_invalid")
    return values[0]


def _if_match(request: Request, *, allow_zero: bool = False) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or _ETAG_RE.fullmatch(values[0]) is None:
        raise ApiError("scim.revision_required")
    result = int(values[0][1:-1])
    if result == 0 and not allow_zero:
        raise ApiError("scim.revision_required")
    return result


def _resource_id(value: str, prefix: str) -> None:
    if not value.startswith(prefix) or _RESOURCE_ID_RE.fullmatch(value) is None:
        raise ApiError("scim.not_found")


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("scim.request_invalid")


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


async def _run(function, *args, **kwargs):
    def run_in_request_scope():
        with database_request_scope(reuse_connections=True):
            return function(*args, **kwargs)

    return await anyio.to_thread.run_sync(run_in_request_scope, limiter=_WORK_LIMITER)


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
