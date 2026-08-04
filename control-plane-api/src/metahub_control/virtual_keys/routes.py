"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/virtual_keys/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_virtual_key_reader, require_virtual_key_writer

from .models import (
    CreateVirtualKeyRequest,
    CreateVirtualKeyResponse,
    UpdateVirtualKeyLimitsRequest,
    UpdateVirtualKeyRequest,
    VirtualKeyListResponse,
    VirtualKeyResponse,
)
from .service import (
    create_virtual_key,
    disable_virtual_key,
    list_virtual_keys,
    update_virtual_key,
    update_virtual_key_limits,
)

router = APIRouter(prefix="/virtual-keys", tags=["virtual-keys"])


@router.get("", response_model=VirtualKeyListResponse)
async def get_virtual_keys(
    request: Request,
    context: Annotated[AuthContext, Depends(require_virtual_key_reader)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    environment_id: str | None = None,
    environment: str | None = None,
) -> VirtualKeyListResponse:
    """处理 Virtual Key 管理 的查询型 HTTP 入口，把认证上下文中的 tenant_id 交给服务层。
    
    安全：前端参数不能伪造租户边界，后端仍按会话角色和租户过滤。
    """

    return list_virtual_keys(
        request.app.state.config,
        context,
        status=status_filter,
        environment_id=environment_id,
        environment=environment,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("", response_model=CreateVirtualKeyResponse, status_code=status.HTTP_201_CREATED)
async def post_virtual_key(
    payload: CreateVirtualKeyRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_virtual_key_writer)],
) -> CreateVirtualKeyResponse:
    """创建 Virtual Key 并只在本次响应返回一次明文。"""

    return create_virtual_key(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/{virtual_key_id}", response_model=VirtualKeyResponse)
async def patch_virtual_key(
    virtual_key_id: str,
    payload: UpdateVirtualKeyRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_virtual_key_writer)],
) -> VirtualKeyResponse:
    """更新当前租户 Virtual Key 元数据或状态。"""

    return update_virtual_key(
        request.app.state.config,
        context,
        virtual_key_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/{virtual_key_id}/limits", response_model=VirtualKeyResponse)
async def patch_virtual_key_limits(
    virtual_key_id: str,
    payload: UpdateVirtualKeyLimitsRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_virtual_key_writer)],
) -> VirtualKeyResponse:
    """更新当前租户 Virtual Key 的基础限流设置。

    前端是否显示限制表单只影响体验；后端仍通过 writer 依赖和 tenant_id 查询共同保证权限边界。
    """

    return update_virtual_key_limits(
        request.app.state.config,
        context,
        virtual_key_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.delete("/{virtual_key_id}", response_model=VirtualKeyResponse)
async def delete_virtual_key(
    virtual_key_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_virtual_key_writer)],
) -> VirtualKeyResponse:
    """处理 Virtual Key 管理 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：按钮隐藏只影响体验，实际写权限必须在依赖和服务层再次校验。
    """

    return disable_virtual_key(
        request.app.state.config,
        context,
        virtual_key_id,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """在 Virtual Key 管理 内部复用 request id 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """在 Virtual Key 管理 内部复用 trace id 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "trace_id", "")
