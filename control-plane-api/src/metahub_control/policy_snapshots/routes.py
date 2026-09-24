"""策略快照公共管理与内部数据面拉取 HTTP 路由。"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import Response as FastAPIResponse
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_internal_service_token,
    require_policy_publisher,
)
from metahub_control.auth.dependencies import require_workspace_or_legacy_permissions

from .models import (
    PolicySnapshotCurrentResponse,
    PolicySnapshotListResponse,
    PolicySnapshotMutationResponse,
    PublishPolicySnapshotRequest,
)
from .service import (
    build_internal_current_response,
    get_current_policy_snapshot,
    list_policy_snapshots,
    publish_policy_snapshot,
    rollback_policy_snapshot,
)

router = APIRouter(tags=["policy-snapshots"])
SNAPSHOT_WRITE_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": PublishPolicySnapshotRequest.model_json_schema()}
        },
    }
}


@router.get("/policy-snapshots/current", response_model=PolicySnapshotCurrentResponse)
async def get_policy_snapshot_current(
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext, Depends(require_workspace_or_legacy_permissions("policy.read"))
    ],
) -> PolicySnapshotCurrentResponse:
    """读取当前 head 与 active 候选；未知 query 由服务层拒绝。"""

    _set_no_store(response)
    result, head_etag, candidate_etag = get_current_policy_snapshot(
        request.app.state.config,
        context,
        query_keys=tuple(request.query_params.keys()),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = head_etag
    if candidate_etag is not None:
        response.headers["X-Policy-Candidate-ETag"] = candidate_etag
    return result


@router.get("/policy-snapshots", response_model=PolicySnapshotListResponse)
async def get_policy_snapshot_history(
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext, Depends(require_workspace_or_legacy_permissions("policy.read"))
    ],
    limit: Annotated[str | None, Query()] = None,
    offset: Annotated[str | None, Query()] = None,
) -> PolicySnapshotListResponse:
    """读取当前租户发布历史；字符串入参确保领域错误码不被框架 422 取代。"""

    _set_no_store(response)
    return list_policy_snapshots(
        request.app.state.config,
        context,
        limit=limit,
        offset=offset,
        query_keys=tuple(request.query_params.keys()),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/policy-snapshots/publish",
    response_model=PolicySnapshotMutationResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=SNAPSHOT_WRITE_OPENAPI,
)
async def post_policy_snapshot_publish(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_policy_publisher)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    candidate_match: Annotated[
        str | None, Header(alias="If-Policy-Candidate-Match")
    ] = None,
) -> PolicySnapshotMutationResponse:
    """在一个写事务内执行 head+candidate 双 CAS 并发布不可变快照。"""

    _set_no_store(response)
    payload = await _read_snapshot_request(request)
    result = publish_policy_snapshot(
        request.app.state.config,
        context,
        payload,
        if_match=if_match,
        candidate_match=candidate_match,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.policy_snapshot.snapshot_version}"'
    return result


@router.post(
    "/policy-snapshots/{policy_snapshot_id}/rollback",
    response_model=PolicySnapshotMutationResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=SNAPSHOT_WRITE_OPENAPI,
)
async def post_policy_snapshot_rollback(
    policy_snapshot_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_policy_publisher)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> PolicySnapshotMutationResponse:
    """复制当前上一版生成新单调版本，绝不把 head 数字直接倒退。"""

    _set_no_store(response)
    payload = await _read_snapshot_request(request)
    result = rollback_policy_snapshot(
        request.app.state.config,
        context,
        policy_snapshot_id,
        payload,
        if_match=if_match,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.policy_snapshot.snapshot_version}"'
    return result


@router.get("/internal/policy-snapshots/{tenant_id}/current")
async def get_internal_policy_snapshot_current(
    tenant_id: str,
    request: Request,
    _: Annotated[None, Depends(require_internal_service_token)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> FastAPIResponse:
    """按已认证 Gateway tenant 返回严格 wire；内部接口不复用用户 Bearer 身份。"""

    return build_internal_current_response(
        request.app.state.config,
        tenant_id,
        if_none_match=if_none_match,
        query_keys=tuple(request.query_params.keys()),
        authorization=request.headers.get("Authorization"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


async def _read_snapshot_request(request: Request) -> PublishPolicySnapshotRequest:
    """Bound the reason body after authentication, without exposing parser inputs."""
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise ApiError("policy_snapshot.payload_invalid")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise ApiError("policy_snapshot.payload_invalid")
        body.extend(chunk)
    try:
        payload = json.loads(
            bytes(body).decode("utf-8"), object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(payload, dict):
            raise ValueError
        return PublishPolicySnapshotRequest.model_validate(payload)
    except (ValueError, TypeError, RecursionError):
        raise ApiError("policy_snapshot.payload_invalid") from None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload = dict(pairs)
    if len(payload) != len(pairs):
        raise ValueError
    return payload


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _set_no_store(response: Response) -> None:
    """禁止浏览器或代理缓存租户快照历史、原因和 CAS 元数据。"""

    response.headers["Cache-Control"] = "no-store"


def _request_id(request: Request) -> str:
    """读取共享中间件生成的 request_id，串联快照、事件、审计与 Webhook。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取共享中间件生成的 trace_id，供跨服务排障。"""

    return getattr(request.state, "trace_id", "")
