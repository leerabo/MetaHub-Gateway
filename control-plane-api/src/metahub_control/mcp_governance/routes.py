"""Authenticated HTTP routes for PRD-082 MCP governance."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_mcp_invoker,
    require_mcp_policy_manager,
    require_mcp_reader,
    require_mcp_server_manager,
    require_mcp_server_refresher,
)

from .models import (
    InvokeMcpToolRequest,
    McpInvocationListResponse,
    McpInvocationResponse,
    McpInvocationResultResponse,
    McpServerListResponse,
    McpServerOperationResponse,
    McpServerResponse,
    McpToolListResponse,
    McpToolResponse,
    RefreshMcpServerRequest,
    RegisterMcpServerRequest,
    UpdateMcpServerRequest,
    UpdateMcpToolPolicyRequest,
)
from .service import (
    get_invocation,
    get_invocation_result,
    get_server_operation,
    get_tool,
    invoke_tool,
    list_invocations,
    list_servers,
    list_tools,
    refresh_server_tools,
    register_server,
    update_server,
    update_tool_policy,
)
from .wire import (
    McpRoute,
    bounded_config_int,
    no_store,
    parse_json,
    read_body,
    reject_query,
    request_id,
    require_idempotency_key,
    strict_query,
    trace_id,
)

router = APIRouter(prefix="/mcp", tags=["mcp"], route_class=McpRoute)


@router.get("/servers", response_model=McpServerListResponse)
async def get_servers(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_mcp_reader)],
) -> McpServerListResponse:
    query = strict_query(request, {"status", "limit", "cursor"})
    await read_body(request, maximum=0, allow_empty=True)
    no_store(response)
    return list_servers(
        request.app.state.config,
        context,
        status=query.get("status"),
        limit=query.get("limit"),
        cursor=query.get("cursor"),
        request_id=request_id(request),
        trace_id=trace_id(request),
    )


@router.post("/servers", response_model=McpServerResponse, status_code=status.HTTP_202_ACCEPTED)
async def post_server(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_mcp_server_manager)],
) -> McpServerResponse:
    reject_query(request)
    key = require_idempotency_key(request)
    payload = await parse_json(
        request,
        RegisterMcpServerRequest,
        bounded_config_int(request.app.state.config, "mcp_register_max_bytes", 16384, 16384),
    )
    no_store(response)
    return register_server(
        request.app.state.config,
        context,
        payload,
        idempotency_key=key,
        request_id=request_id(request),
        trace_id=trace_id(request),
        request_meta=build_audit_request_meta(
            request,
            sensitive_values=tuple(
                value
                for value in (key, payload.endpoint_url, payload.auth.bearer_token)
                if value is not None
            ),
        ),
    )


@router.patch(
    "/servers/{mcp_server_id}",
    response_model=McpServerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def patch_server(
    request: Request,
    response: Response,
    mcp_server_id: str,
    context: Annotated[AuthContext, Depends(require_mcp_server_manager)],
) -> McpServerResponse:
    reject_query(request)
    key = require_idempotency_key(request)
    payload = await parse_json(
        request,
        UpdateMcpServerRequest,
        bounded_config_int(request.app.state.config, "mcp_register_max_bytes", 16384, 16384),
    )
    no_store(response)
    return update_server(
        request.app.state.config,
        context,
        mcp_server_id,
        payload,
        idempotency_key=key,
        request_id=request_id(request),
        trace_id=trace_id(request),
        request_meta=build_audit_request_meta(
            request,
            sensitive_values=tuple(
                value
                for value in (
                    key,
                    payload.auth.bearer_token if payload.auth is not None else None,
                )
                if value is not None
            ),
        ),
    )


@router.post(
    "/servers/{mcp_server_id}/refresh-tools",
    response_model=McpServerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_server_refresh(
    request: Request,
    response: Response,
    mcp_server_id: str,
    context: Annotated[AuthContext, Depends(require_mcp_server_refresher)],
) -> McpServerResponse:
    reject_query(request)
    key = require_idempotency_key(request)
    payload = await parse_json(
        request,
        RefreshMcpServerRequest,
        bounded_config_int(request.app.state.config, "mcp_refresh_max_bytes", 4096, 4096),
    )
    no_store(response)
    return refresh_server_tools(
        request.app.state.config,
        context,
        mcp_server_id,
        payload,
        idempotency_key=key,
        request_id=request_id(request),
        trace_id=trace_id(request),
        request_meta=build_audit_request_meta(request, sensitive_values=(key,)),
    )


@router.get("/server-operations/{operation_id}", response_model=McpServerOperationResponse)
async def get_operation(
    request: Request,
    response: Response,
    operation_id: str,
    context: Annotated[AuthContext, Depends(require_mcp_reader)],
) -> McpServerOperationResponse:
    reject_query(request)
    await read_body(request, maximum=0, allow_empty=True)
    no_store(response)
    return get_server_operation(
        request.app.state.config,
        context,
        operation_id,
        request_id=request_id(request),
        trace_id=trace_id(request),
    )


@router.get("/tools", response_model=McpToolListResponse)
async def get_tools(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_mcp_reader)],
) -> McpToolListResponse:
    query = strict_query(
        request,
        {"server_id", "status", "risk", "approval_required", "limit", "cursor"},
    )
    await read_body(request, maximum=0, allow_empty=True)
    no_store(response)
    return list_tools(
        request.app.state.config,
        context,
        server_id=query.get("server_id"),
        status=query.get("status"),
        risk=query.get("risk"),
        approval_required=query.get("approval_required"),
        limit=query.get("limit"),
        cursor=query.get("cursor"),
        request_id=request_id(request),
        trace_id=trace_id(request),
    )


@router.get("/tools/{mcp_tool_id}", response_model=McpToolResponse)
async def get_tool_detail(
    request: Request,
    response: Response,
    mcp_tool_id: str,
    context: Annotated[AuthContext, Depends(require_mcp_reader)],
) -> McpToolResponse:
    reject_query(request)
    await read_body(request, maximum=0, allow_empty=True)
    no_store(response)
    return get_tool(
        request.app.state.config,
        context,
        mcp_tool_id,
        request_id=request_id(request),
        trace_id=trace_id(request),
    )


@router.patch("/tools/{mcp_tool_id}", response_model=McpToolResponse)
async def patch_tool(
    request: Request,
    response: Response,
    mcp_tool_id: str,
    context: Annotated[AuthContext, Depends(require_mcp_policy_manager)],
) -> McpToolResponse:
    reject_query(request)
    payload = await parse_json(
        request,
        UpdateMcpToolPolicyRequest,
        bounded_config_int(request.app.state.config, "mcp_policy_max_bytes", 32768, 32768),
    )
    no_store(response)
    return update_tool_policy(
        request.app.state.config,
        context,
        mcp_tool_id,
        payload,
        request_id=request_id(request),
        trace_id=trace_id(request),
        request_meta=build_audit_request_meta(request),
    )


@router.post(
    "/tools/{mcp_tool_id}/invoke",
    response_model=McpInvocationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_tool_invocation(
    request: Request,
    response: Response,
    mcp_tool_id: str,
    context: Annotated[AuthContext, Depends(require_mcp_invoker)],
) -> McpInvocationResponse:
    reject_query(request)
    key = require_idempotency_key(request)
    payload = await parse_json(
        request,
        InvokeMcpToolRequest,
        bounded_config_int(request.app.state.config, "mcp_invoke_max_bytes", 131072, 131072),
    )
    no_store(response)
    return invoke_tool(
        request.app.state.config,
        context,
        mcp_tool_id,
        payload,
        idempotency_key=key,
        request_id=request_id(request),
        trace_id=trace_id(request),
    )


@router.get("/invocations", response_model=McpInvocationListResponse)
async def get_invocations(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_mcp_reader)],
) -> McpInvocationListResponse:
    query = strict_query(request, {"status", "tool_id", "server_id", "limit", "cursor"})
    await read_body(request, maximum=0, allow_empty=True)
    no_store(response)
    return list_invocations(
        request.app.state.config,
        context,
        status=query.get("status"),
        tool_id=query.get("tool_id"),
        server_id=query.get("server_id"),
        limit=query.get("limit"),
        cursor=query.get("cursor"),
        request_id=request_id(request),
        trace_id=trace_id(request),
    )


@router.get("/invocations/{invocation_id}", response_model=McpInvocationResponse)
async def get_invocation_detail(
    request: Request,
    response: Response,
    invocation_id: str,
    context: Annotated[AuthContext, Depends(require_mcp_reader)],
) -> McpInvocationResponse:
    reject_query(request)
    await read_body(request, maximum=0, allow_empty=True)
    no_store(response)
    return get_invocation(
        request.app.state.config,
        context,
        invocation_id,
        request_id=request_id(request),
        trace_id=trace_id(request),
    )


@router.get("/invocations/{invocation_id}/result", response_model=McpInvocationResultResponse)
async def get_result(
    request: Request,
    response: Response,
    invocation_id: str,
    context: Annotated[AuthContext, Depends(require_mcp_invoker)],
) -> McpInvocationResultResponse:
    reject_query(request)
    await read_body(request, maximum=0, allow_empty=True)
    no_store(response)
    return get_invocation_result(
        request.app.state.config,
        context,
        invocation_id,
        request_id=request_id(request),
        trace_id=trace_id(request),
    )


__all__ = ["router"]
