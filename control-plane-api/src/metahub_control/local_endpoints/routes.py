"""HTTP routes for the local model Endpoint aggregate."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_workspace_or_legacy_permissions,
)

from .models import (
    CreateLocalEndpointRequest,
    LocalEndpointCreateResponse,
    LocalEndpointListResponse,
    LocalEndpointTestResponse,
    TestLocalEndpointRequest,
)
from .service import create_local_endpoint, list_local_endpoints, test_local_endpoint

router = APIRouter(prefix="/local-endpoints", tags=["local-endpoints"])


@router.get("", response_model=LocalEndpointListResponse)
async def get_local_endpoints(
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("local_endpoint.read")),
    ],
) -> LocalEndpointListResponse:
    return list_local_endpoints(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "",
    response_model=LocalEndpointCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_local_endpoint(
    payload: CreateLocalEndpointRequest,
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("local_endpoint.manage")),
    ],
) -> LocalEndpointCreateResponse:
    result = create_local_endpoint(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return result


@router.post("/{local_endpoint_id}/test", response_model=LocalEndpointTestResponse)
async def post_local_endpoint_test(
    local_endpoint_id: str,
    payload: TestLocalEndpointRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("local_endpoint.test")),
    ],
) -> LocalEndpointTestResponse:
    del payload
    return test_local_endpoint(
        request.app.state.config,
        context,
        local_endpoint_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")
