"""PRD-104 Workspace 发票与 Platform 对账/冲红的严格凭据分域 HTTP 边界。"""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error
from starlette.concurrency import run_in_threadpool

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
    BillingStatementsResponse,
    InvoiceCreateRequest,
    InvoiceDownloadGrantResponse,
    InvoiceResponse,
    InvoiceReversalApprovalRequest,
    InvoiceReversalCreateRequest,
    InvoiceReversalResponse,
    ReconciliationCreateRequest,
    ReconciliationDifferencesResponse,
    ReconciliationResponse,
)
from .service import (
    approve_invoice_reversal,
    consume_invoice_download,
    create_invoice,
    create_invoice_reversal,
    create_reconciliation,
    get_invoice,
    issue_invoice_download_token,
    list_billing_statements,
    list_reconciliation_differences,
)

router = APIRouter(tags=["invoice-reconciliation"])
_MAX_COMMAND_BYTES = 8 * 1024
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_workspace_invoice(request: Request) -> WorkspaceSessionContext:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "invoice.request_invalid")
        csrf = _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )
    if context.membership_role not in {"owner", "finance_admin"}:
        raise ApiError("workspace_session.permission_denied")
    return context


def require_platform_invoice(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    billing = "platform_billing" in context.roles and "platform.billing.read" in context.permissions
    compliance = (
        "platform_compliance" in context.roles and "platform.compliance.read" in context.permissions
    )
    if not (billing or compliance):
        raise ApiError("platform.permission_denied")
    return context


@router.get("/billing/statements", response_model=BillingStatementsResponse)
async def get_billing_statements(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_invoice)],
) -> BillingStatementsResponse:
    _reject_query(request, "invoice.request_invalid")
    _private(response)
    return await run_in_threadpool(
        list_billing_statements,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/invoices", response_model=InvoiceResponse, status_code=201)
async def post_invoice(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_invoice)],
) -> InvoiceResponse:
    _reject_query(request, "invoice.request_invalid")
    payload = _model(
        await _strict_json(request, "invoice.request_invalid"),
        InvoiceCreateRequest,
        "invoice.request_invalid",
    )
    result = await run_in_threadpool(
        create_invoice,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"),
        payload=payload,
        idempotency_key=_idempotency_key(request, "invoice.request_invalid"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = _etag(result.invoice.state_revision)
    _private(response)
    return result


@router.get("/invoices/{invoice_id}", response_model=InvoiceResponse)
async def read_invoice(
    invoice_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_invoice)],
) -> InvoiceResponse:
    _reject_query(request, "invoice.request_invalid")
    result = await run_in_threadpool(
        get_invoice,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        invoice_id=invoice_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.invoice.state_revision)
    _private(response)
    return result


@router.post(
    "/invoices/{invoice_id}/artifacts/{artifact_id}/download-token",
    response_model=InvoiceDownloadGrantResponse,
)
async def post_invoice_download_token(
    invoice_id: str,
    artifact_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_invoice)],
) -> InvoiceDownloadGrantResponse:
    _reject_query(request, "invoice.request_invalid")
    if await _strict_json(request, "invoice.request_invalid"):
        raise ApiError("invoice.request_invalid")
    result = await run_in_threadpool(
        issue_invoice_download_token,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"),
        invoice_id=invoice_id,
        artifact_id=artifact_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/invoice-downloads")
async def get_invoice_download(
    request: Request,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_invoice)],
) -> Response:
    _reject_query(request, "invoice.download_invalid")
    tokens = _raw_headers(request, b"x-metahub-invoice-download-token")
    if len(tokens) != 1:
        raise ApiError("invoice.download_invalid")
    artifact = await run_in_threadpool(
        consume_invoice_download,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        raw_token=tokens[0],
    )
    response = Response(content=artifact.content, media_type=artifact.mime_type)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Content-Disposition"] = f'attachment; filename="{artifact.filename}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.post("/platform/reconciliations", response_model=ReconciliationResponse, status_code=202)
async def post_reconciliation(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_invoice)],
) -> ReconciliationResponse:
    _reject_query(request, "reconciliation.request_invalid")
    payload = _model(
        await _strict_json(request, "reconciliation.request_invalid"),
        ReconciliationCreateRequest,
        "reconciliation.request_invalid",
    )
    result = await run_in_threadpool(
        create_reconciliation,
        request.app.state.config,
        platform_session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get(
    "/platform/reconciliations/{run_id}/differences",
    response_model=ReconciliationDifferencesResponse,
)
async def get_reconciliation_differences(
    run_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_invoice)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> ReconciliationDifferencesResponse:
    _exact_query(request, {"limit", "cursor"}, "reconciliation.request_invalid")
    result = await run_in_threadpool(
        list_reconciliation_differences,
        request.app.state.config,
        platform_session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        run_id=run_id,
        limit=limit,
        cursor=cursor,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/invoices/{invoice_id}/reversals",
    response_model=InvoiceReversalResponse,
    status_code=201,
)
async def post_invoice_reversal(
    invoice_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_invoice)],
) -> InvoiceReversalResponse:
    _reject_query(request, "invoice.request_invalid")
    payload = _model(
        await _strict_json(request, "invoice.request_invalid"),
        InvoiceReversalCreateRequest,
        "invoice.request_invalid",
    )
    result = await run_in_threadpool(
        create_invoice_reversal,
        request.app.state.config,
        platform_session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        invoice_id=invoice_id,
        payload=payload,
        idempotency_key=_idempotency_key(request, "invoice.request_invalid"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = _etag(result.reversal.state_revision)
    _private(response)
    return result


@router.post(
    "/platform/invoice-reversals/{reversal_id}/approvals",
    response_model=InvoiceReversalResponse,
)
async def post_invoice_reversal_approval(
    reversal_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_invoice)],
) -> InvoiceReversalResponse:
    _reject_query(request, "invoice.request_invalid")
    payload = _model(
        await _strict_json(request, "invoice.request_invalid"),
        InvoiceReversalApprovalRequest,
        "invoice.request_invalid",
    )
    result = await run_in_threadpool(
        approve_invoice_reversal,
        request.app.state.config,
        platform_session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        reversal_id=reversal_id,
        payload=payload,
        idempotency_key=_idempotency_key(request, "invoice.request_invalid"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.reversal.state_revision)
    _private(response)
    return result


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
        if declared < 2 or declared > _MAX_COMMAND_BYTES:
            raise ApiError(error_code)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_COMMAND_BYTES:
            raise ApiError(error_code)
    if not body or (declared is not None and declared != len(body)):
        raise ApiError(error_code)

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        payload = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ApiError(error_code) from None
    if type(payload) is not dict:
        raise ApiError(error_code)
    return payload


def _model(payload: dict[str, Any], model_type, error_code: str):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError(error_code) from None


def _required_cookie(request: Request, name: str, missing_code: str) -> str:
    values = _cookie_values(request, name)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        if values and name == PLATFORM_SESSION_COOKIE:
            raise ApiError("platform.session_invalid")
        if values and name == WORKSPACE_SESSION_COOKIE:
            raise ApiError("workspace_session.invalid")
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


def _idempotency_key(request: Request, error_code: str) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError(error_code)
    return values[0]


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


def _reject_query(request: Request, error_code: str) -> None:
    if request.query_params.multi_items():
        raise ApiError(error_code)


def _exact_query(request: Request, allowed: set[str], error_code: str) -> None:
    keys = [key for key, _value in request.query_params.multi_items()]
    if any(key not in allowed for key in keys) or len(keys) != len(set(keys)):
        raise ApiError(error_code)


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _etag(revision: int) -> str:
    return f'"{revision}"'


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
