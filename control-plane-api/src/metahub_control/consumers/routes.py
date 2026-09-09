"""Consumer management accepts human tenant administrators, never employee API keys."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_workspace_or_legacy_permissions

from .models import (
    ConsumerListResponse,
    ConsumerResponse,
    CreateConsumerRequest,
    UpdateConsumerRequest,
)
from .service import list_consumers, save_consumer

router = APIRouter(prefix="/consumers", tags=["consumers"])
Reader = Annotated[
    AuthContext, Depends(require_workspace_or_legacy_permissions("virtual_key.read"))
]
Writer = Annotated[
    AuthContext, Depends(require_workspace_or_legacy_permissions("virtual_key.write"))
]


@router.get("", response_model=ConsumerListResponse)
def get_consumers(
    request: Request,
    context: Reader,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    return list_consumers(
        request.app.state.config,
        context,
        limit=limit,
        offset=offset,
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )


@router.post("", response_model=ConsumerResponse, status_code=201)
def post_consumer(payload: CreateConsumerRequest, request: Request, context: Writer):
    return save_consumer(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )


@router.patch("/{consumer_id}", response_model=ConsumerResponse)
def patch_consumer(
    consumer_id: str, payload: UpdateConsumerRequest, request: Request, context: Writer
):
    return save_consumer(
        request.app.state.config,
        context,
        payload,
        consumer_id=consumer_id,
        audit_request_meta=build_audit_request_meta(request),
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )
