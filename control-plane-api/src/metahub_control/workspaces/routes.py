"""Strict GlobalSession HTTP boundary for PRD-093 Workspace APIs."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable
from functools import partial
from typing import Any

import anyio
from fastapi import APIRouter, Request, Response, status
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.service import CSRF_COOKIE, SESSION_COOKIE, valid_opaque_value

from .models import WorkspaceListResponse, WorkspaceResponse
from .service import create_workspace, get_workspace, list_workspaces, update_workspace

router = APIRouter(prefix="/workspaces", tags=["workspaces"])
_MAX_BODY_BYTES = 4096
_WORK_LIMITER = anyio.CapacityLimiter(4)


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def post_workspace(request: Request, response: Response) -> WorkspaceResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    idempotency_key = _single_header(request, b"idempotency-key", required=True)
    payload = await _strict_json(request)
    kind = _required_string(payload, "kind", maximum=20)
    required = (
        {"kind", "name", "slug", "region", "service_mode"}
        if kind.strip().lower() == "organization"
        else {"kind", "region", "service_mode"}
    )
    if set(payload) != required:
        raise ApiError("workspace.request_invalid")
    result = await _run(
        create_workspace,
        _config(request),
        session_token=_required_session_cookie(request),
        csrf_token=_required_csrf(request),
        idempotency_key=idempotency_key,
        kind=kind,
        name=_optional_string(payload, "name", maximum=160),
        slug=_optional_string(payload, "slug", maximum=48),
        region=_required_string(payload, "region", maximum=40),
        service_mode=_required_string(payload, "service_mode", maximum=40),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.workspace.revision)
    return result


@router.get("", response_model=WorkspaceListResponse)
async def get_workspaces(request: Request) -> WorkspaceListResponse:
    _reject_legacy_authorization(request)
    limit, offset = _pagination(request)
    return await _run(
        list_workspaces,
        _config(request),
        session_token=_required_session_cookie(request),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace_detail(
    workspace_id: str,
    request: Request,
    response: Response,
) -> WorkspaceResponse:
    _reject_legacy_authorization(request)
    _reject_query(request, ())
    result = await _run(
        get_workspace,
        _config(request),
        session_token=_required_session_cookie(request),
        workspace_id=workspace_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.workspace.revision)
    return result


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
async def patch_workspace(
    workspace_id: str,
    request: Request,
    response: Response,
) -> WorkspaceResponse:
    _reject_legacy_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())
    expected_revision = _if_match(request)
    payload = await _strict_json(request)
    allowed = {"name", "display_label", "profile_state"}
    if not payload or not set(payload) <= allowed:
        raise ApiError("workspace.update_empty" if not payload else "workspace.update_invalid")
    result = await _run(
        update_workspace,
        _config(request),
        session_token=_required_session_cookie(request),
        csrf_token=_required_csrf(request),
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        name=_optional_string(payload, "name", maximum=160),
        display_label=_optional_string(payload, "display_label", maximum=120),
        profile_state=_optional_string(payload, "profile_state", maximum=40),
        supplied_fields=frozenset(payload),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.workspace.revision)
    return result


async def _strict_json(request: Request) -> dict[str, Any]:
    content_types = _raw_header_values(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError("workspace.request_invalid")
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/json":
        raise ApiError("workspace.request_invalid")
    if separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}:
        raise ApiError("workspace.request_invalid")
    if _raw_header_values(request, b"transfer-encoding"):
        raise ApiError("workspace.request_invalid")
    lengths = _raw_header_values(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError("workspace.request_invalid")
    if lengths:
        try:
            declared = int(lengths[0])
        except ValueError:
            raise ApiError("workspace.request_invalid") from None
        if declared < 2 or declared > _MAX_BODY_BYTES:
            raise ApiError("workspace.request_invalid")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise ApiError("workspace.request_invalid")
    if not body:
        raise ApiError("workspace.request_invalid")

    def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        payload = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ApiError("workspace.request_invalid") from None
    if type(payload) is not dict:
        raise ApiError("workspace.request_invalid")
    return payload


def _required_string(payload: dict[str, Any], field: str, *, maximum: int) -> str:
    value = payload.get(field)
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or "\x00" in value
        or any(ord(character) < 32 and character not in "\t\n\r" for character in value)
    ):
        raise ApiError("workspace.request_invalid")
    return value


def _optional_string(payload: dict[str, Any], field: str, *, maximum: int) -> str | None:
    if field not in payload:
        return None
    return _required_string(payload, field, maximum=maximum)


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
    value = _single_cookie(request, SESSION_COOKIE)
    if value is None:
        raise ApiError("identity.session_required")
    return value


def _required_csrf(request: Request) -> str:
    cookie = _single_cookie(request, CSRF_COOKIE)
    headers = _raw_header_values(request, b"x-csrf-token")
    if (
        cookie is None
        or len(headers) != 1
        or not valid_opaque_value(headers[0])
        or not secrets.compare_digest(cookie, headers[0])
    ):
        raise ApiError("identity.csrf_invalid")
    return cookie


def _single_cookie(request: Request, name: str) -> str | None:
    values: list[str] = []
    for raw_cookie in _raw_header_values(request, b"cookie"):
        for segment in raw_cookie.split(";"):
            key, separator, value = segment.strip().partition("=")
            if separator and key == name:
                values.append(value)
    if not values:
        return None
    if len(values) != 1 or not valid_opaque_value(values[0]):
        code = "identity.session_invalid" if name == SESSION_COOKIE else "identity.csrf_invalid"
        raise ApiError(code)
    return values[0]


def _single_header(request: Request, name: bytes, *, required: bool) -> str:
    values = _raw_header_values(request, name)
    if len(values) != 1 or not values[0]:
        raise ApiError("workspace.idempotency_invalid" if required else "workspace.request_invalid")
    return values[0]


def _if_match(request: Request) -> int:
    values = _raw_header_values(request, b"if-match")
    if len(values) != 1:
        raise ApiError("workspace.revision_invalid")
    value = values[0]
    if len(value) < 3 or value[0] != '"' or value[-1] != '"' or not value[1:-1].isdigit():
        raise ApiError("workspace.revision_invalid")
    return int(value[1:-1])


def _pagination(request: Request) -> tuple[int, int]:
    items = list(request.query_params.multi_items())
    if any(key not in {"limit", "offset"} for key, _value in items):
        raise ApiError("workspace.request_invalid")
    if sum(key == "limit" for key, _value in items) > 1 or sum(
        key == "offset" for key, _value in items
    ) > 1:
        raise ApiError("workspace.request_invalid")
    try:
        limit = int(request.query_params.get("limit", "20"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        raise ApiError("workspace.request_invalid") from None
    if limit < 1 or limit > 100 or offset < 0:
        raise ApiError("workspace.request_invalid")
    return limit, offset


def _reject_query(request: Request, allowed: Iterable[str]) -> None:
    allowed_set = set(allowed)
    if any(key not in allowed_set for key, _value in request.query_params.multi_items()):
        raise ApiError("workspace.request_invalid")


def _raw_header_values(request: Request, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for header_name, value in request.scope.get("headers", ())
        if header_name.lower() == name
    ]


async def _run(function, *args, **kwargs):
    return await anyio.to_thread.run_sync(
        partial(function, *args, **kwargs),
        limiter=_WORK_LIMITER,
    )


def _config(request: Request):
    return request.app.state.config


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


def _etag(revision: int) -> str:
    return f'"{revision}"'
