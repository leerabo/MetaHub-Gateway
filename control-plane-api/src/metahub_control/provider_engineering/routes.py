"""Credential-isolated strict HTTP boundaries for PRD-143."""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error
from pydantic import ValidationError

from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_context,
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
    AdapterCommandRequest,
    AdapterCommandResponse,
    AdapterContractProjectionResponse,
    ErrorCatalogCommandRequest,
    ErrorCatalogCommandResponse,
    ProviderAdapterListResponse,
    WorkspaceErrorOverrideListResponse,
    WorkspaceOverrideCommandRequest,
    WorkspaceOverrideCommandResponse,
)
from .permissions import (
    PLATFORM_PROVIDER_ENGINEERING_PERMISSIONS,
    WORKSPACE_PROVIDER_ENGINEERING_PERMISSIONS,
)
from .service import (
    command_error_catalog,
    command_provider_adapter,
    command_workspace_error_override,
    list_adapter_contract_projections,
    list_provider_adapters,
    list_workspace_error_overrides,
)

router = APIRouter(tags=["provider-engineering"])
_MAX_BODY_BYTES = 16_384
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_IF_MATCH = re.compile(r'^"(0|[1-9][0-9]{0,9})"$')


def require_platform_provider_engineering(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method == "POST":
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    if not set(context.permissions).intersection(PLATFORM_PROVIDER_ENGINEERING_PERMISSIONS):
        raise ApiError("platform.permission_denied")
    return context


def require_workspace_provider_engineering(request: Request) -> WorkspaceSessionContext:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, PLATFORM_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    if request.method == "PATCH":
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )
    required = (
        "workspace.provider_error_override.manage"
        if request.method == "PATCH"
        else "workspace.provider_error_override.read"
    )
    if (
        required not in context.permission_codes
        or required not in WORKSPACE_PROVIDER_ENGINEERING_PERMISSIONS
    ):
        raise ApiError("workspace_session.permission_denied")
    return context


@router.get("/platform/provider-adapters", response_model=ProviderAdapterListResponse)
async def get_provider_adapters(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_provider_engineering)],
) -> ProviderAdapterListResponse:
    result = list_provider_adapters(
        request.app.state.config,
        session_token=_platform_token(request),
        query=_unique_query(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.get(
    "/platform/provider-adapters/{provider_adapter_release_id}/contract-runs",
    response_model=AdapterContractProjectionResponse,
)
async def get_provider_adapter_contract_runs(
    provider_adapter_release_id: str,
    request: Request,
    _context: Annotated[PlatformContext, Depends(require_platform_provider_engineering)],
) -> Response:
    result = list_adapter_contract_projections(
        request.app.state.config,
        session_token=_platform_token(request),
        provider_adapter_release_id=provider_adapter_release_id,
        query=_unique_query(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response = Response(content=result.model_dump_json(), media_type="application/json")
    _private(response, result.permission_fingerprint)
    return response


@router.post(
    "/platform/provider-adapters/{adapter_identifier}/commands",
    response_model=AdapterCommandResponse,
)
async def post_provider_adapter_command(
    adapter_identifier: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_provider_engineering)],
) -> AdapterCommandResponse:
    _reject_query(request)
    command = await _model(request, AdapterCommandRequest, "platform.request_invalid")
    result = command_provider_adapter(
        request.app.state.config,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request, workspace=False),
        expected_revision=_if_match(request, workspace=False),
        adapter_identifier=adapter_identifier,
        command=command,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if command.action == "run_contract" and not result.idempotent_replay:
        response.status_code = status.HTTP_202_ACCEPTED
    elif command.action == "create_candidate" and not result.idempotent_replay:
        response.status_code = status.HTTP_201_CREATED
    response.headers["ETag"] = f'"{result.adapter.revision}"'
    _private(response, result.permission_fingerprint)
    return result


@router.post(
    "/platform/provider-error-catalogs/releases",
    response_model=ErrorCatalogCommandResponse,
)
async def post_provider_error_catalog_release(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_provider_engineering)],
) -> ErrorCatalogCommandResponse:
    _reject_query(request)
    command = await _model(request, ErrorCatalogCommandRequest, "platform.request_invalid")
    result = command_error_catalog(
        request.app.state.config,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request, workspace=False),
        expected_revision=_if_match(request, workspace=False),
        command=command,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if command.action == "create_candidate" and not result.idempotent_replay:
        response.status_code = status.HTTP_201_CREATED
    response.headers["ETag"] = f'"{result.catalog.revision}"'
    _private(response, result.permission_fingerprint)
    return result


@router.get(
    "/workspace/provider-error-overrides",
    response_model=WorkspaceErrorOverrideListResponse,
)
async def get_workspace_provider_error_overrides(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_provider_engineering)],
) -> WorkspaceErrorOverrideListResponse:
    result = list_workspace_error_overrides(
        request.app.state.config,
        session_token=_workspace_token(request),
        query=_unique_query(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.head.revision}"'
    _private(response, result.permission_fingerprint)
    return result


@router.patch(
    "/workspace/provider-error-overrides",
    response_model=WorkspaceOverrideCommandResponse,
)
async def patch_workspace_provider_error_overrides(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_provider_engineering)],
) -> WorkspaceOverrideCommandResponse:
    _reject_query(request)
    command = await _model(request, WorkspaceOverrideCommandRequest, "error_map.input_invalid")
    result = command_workspace_error_override(
        request.app.state.config,
        session_token=_workspace_token(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request, workspace=True),
        expected_head_revision=_if_match(request, workspace=True),
        command=command,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if command.action == "create" and not result.idempotent_replay:
        response.status_code = status.HTTP_201_CREATED
    response.headers["ETag"] = f'"{result.head.revision}"'
    _private(response, result.permission_fingerprint)
    return result


async def _model(request: Request, model_type, error_code: str):
    payload = await _strict_json(request, error_code)
    try:
        return model_type.model_validate_json(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            strict=True,
        )
    except ValidationError:
        raise ApiError(error_code) from None


async def _strict_json(request: Request, error_code: str) -> dict[str, Any]:
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError(error_code)
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/json" or (
        separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise ApiError(error_code)
    if _raw_headers(request, b"transfer-encoding"):
        raise ApiError(error_code)
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError(error_code)
    declared = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError(error_code)
        declared = int(lengths[0])
        if not 2 <= declared <= _MAX_BODY_BYTES:
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
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_float=lambda _: (_ for _ in ()).throw(ValueError("float")),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ApiError(error_code) from None
    if type(payload) is not dict:
        raise ApiError(error_code)
    return payload


def _unique_query(request: Request) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key in result or _control(key) or _control(value):
            raise ApiError(
                "error_map.input_invalid"
                if request.url.path.startswith("/workspace/")
                else "platform.request_invalid"
            )
        result[key] = value
    return result


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError(
            "error_map.input_invalid"
            if request.url.path.startswith("/workspace/")
            else "platform.request_invalid"
        )


def _required_cookie(request: Request, name: str, missing_code: str) -> str:
    values = _cookie_values(request, name)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError(missing_code)
    return values[0]


def _required_csrf(request: Request, cookie_name: str, error_code: str) -> str:
    cookies = _cookie_values(request, cookie_name)
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


def _require_origin(request: Request, error_code: str) -> None:
    origins = _raw_headers(request, b"origin")
    sites = _raw_headers(request, b"sec-fetch-site")
    if (
        len(origins) != 1
        or "," in origins[0]
        or origins[0] not in request.app.state.config.allowed_origins
        or len(sites) > 1
        or (sites and sites[0].lower() == "cross-site")
    ):
        raise ApiError(error_code)


def _if_match(request: Request, *, workspace: bool) -> int:
    values = _raw_headers(request, b"if-match")
    match = _IF_MATCH.fullmatch(values[0]) if len(values) == 1 else None
    if match is None:
        raise ApiError("error_map.version_conflict" if workspace else "platform.revision_conflict")
    return int(match.group(1))


def _idempotency_key(request: Request, *, workspace: bool) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError(
            "error_map.request_conflict" if workspace else "platform.idempotency_conflict"
        )
    return values[0]


def _step_up(request: Request) -> str:
    values = _raw_headers(request, b"x-step-up-grant")
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("step_up.invalid")
    return values[0]


def _platform_token(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _platform_csrf(request: Request) -> str:
    return _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


def _workspace_token(request: Request) -> str:
    return _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required")


def _workspace_csrf(request: Request) -> str:
    return _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")


def _cookie_values(request: Request, name: str) -> list[str]:
    values: list[str] = []
    for raw in _raw_headers(request, b"cookie"):
        for segment in raw.split(";"):
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


def _private(response: Response, permission_fingerprint: str) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Permission-Fingerprint"] = permission_fingerprint


def _control(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
