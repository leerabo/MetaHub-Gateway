"""Strict HTTP boundary for tenant data lifecycle operations."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response as RawResponse
from metahub_shared.runtime.errors import ApiError
from pydantic import ValidationError

from metahub_control.auth import (
    AuthContext,
    require_tenant_data_deleter,
    require_tenant_data_downloader,
    require_tenant_data_exporter,
    require_tenant_data_reader,
)

from .models import (
    TenantDataManagementResponse,
    TenantDeletionRequest,
    TenantDeletionResponse,
    TenantExportResponse,
)
from .service import (
    create_deletion_request,
    create_export,
    download_export,
    get_data_management,
    get_export,
)

router = APIRouter(prefix="/tenant", tags=["tenant-data"])


@router.get("/data-management", response_model=TenantDataManagementResponse)
async def data_management(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_tenant_data_reader)],
) -> TenantDataManagementResponse:
    _reject_query(request)
    _no_store(response)
    return get_data_management(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/export", response_model=TenantExportResponse, status_code=202)
async def post_export(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_tenant_data_exporter)],
) -> TenantExportResponse:
    _reject_query(request)
    key = _single_header(request, "idempotency-key")
    body = await request.body()
    if not body or len(body) > 4096:
        raise ApiError("tenant_data.request_invalid")
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        raise ApiError("tenant_data.request_invalid") from None
    if not isinstance(value, dict) or value != {}:
        raise ApiError("tenant_data.request_invalid")
    _no_store(response)
    return create_export(
        request.app.state.config,
        context,
        idempotency_key=key,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/exports/{job_id}", response_model=TenantExportResponse)
async def export_status(
    job_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_tenant_data_reader)],
) -> TenantExportResponse:
    _reject_query(request)
    _no_store(response)
    return get_export(
        request.app.state.config,
        context,
        job_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/exports/{job_id}/download")
async def export_download(
    job_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_tenant_data_downloader)],
) -> RawResponse:
    _reject_query(request)
    content, job = download_export(request.app.state.config, context, job_id)
    filename = f"metahub-tenant-export-{job.tenant_export_job_id}.zip"
    return RawResponse(
        content=content,
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(len(content)),
            "X-Content-SHA256": job.content_sha256 or "",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/deletion-request", response_model=TenantDeletionResponse, status_code=202)
async def post_deletion_request(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_tenant_data_deleter)],
) -> TenantDeletionResponse:
    _reject_query(request)
    key = _single_header(request, "idempotency-key")
    body = await request.body()
    if not body or len(body) > 4096:
        raise ApiError("tenant_data.request_invalid")
    try:
        payload = TenantDeletionRequest.model_validate(json.loads(body))
    except (TypeError, ValueError, RequestValidationError, ValidationError):
        raise ApiError("tenant_data.request_invalid") from None
    _no_store(response)
    return create_deletion_request(
        request.app.state.config,
        context,
        payload,
        idempotency_key=key,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _single_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise ApiError("tenant_data.request_invalid")
    value = values[0].strip()
    if not value or len(value) > 200 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        raise ApiError("tenant_data.request_invalid")
    return value


def _reject_query(request: Request) -> None:
    if request.query_params:
        raise ApiError("tenant_data.request_invalid")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
