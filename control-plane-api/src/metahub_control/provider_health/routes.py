"""PRD-035 Provider 健康 HTTP 路由与后端角色、租户权限边界。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from metahub_control.auth import (
    AuthContext,
    require_workspace_or_legacy_permissions,
)

from .models import ProbeProviderRequest, ProviderHealthListResponse, ProviderProbeResponse
from .service import list_provider_health, probe_provider

router = APIRouter(tags=["provider-health"])


@router.get("/provider-health", response_model=ProviderHealthListResponse)
async def get_provider_health(
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.health.read")),
    ],
    health_status: str | None = None,
    provider_status: str | None = None,
    provider_type: str | None = None,
    provider_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> ProviderHealthListResponse:
    """列出当前租户 Provider 健康状态，筛选条件不能改变 tenant_id 边界。"""

    response.headers["Cache-Control"] = "no-store"
    return list_provider_health(
        request.app.state.config,
        context,
        health_status=health_status,
        provider_status=provider_status,
        provider_type=provider_type,
        provider_id=provider_id,
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/providers/{provider_id}/probe", response_model=ProviderProbeResponse)
async def post_provider_probe(
    provider_id: str,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.health.probe")),
    ],
    payload: Annotated[ProbeProviderRequest | None, Body()] = None,
) -> ProviderProbeResponse:
    """立即探测一个当前租户 Provider；空 body 与 `{}` 等价，额外字段会被拒绝。"""

    del payload
    return await run_in_threadpool(
        probe_provider,
        request.app.state.config,
        context,
        provider_id,
        trigger_type="manual",
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """读取统一中间件生成或透传的 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件生成或透传的 trace_id。"""

    return getattr(request.state, "trace_id", "")
