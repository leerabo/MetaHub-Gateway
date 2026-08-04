"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/models/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from metahub_control.auth import (
    AuthContext,
    require_model_catalog_reader,
    require_model_catalog_writer,
)

from .models import CreateModelRequest, ModelListResponse, ModelResponse, UpdateModelRequest
from .service import create_model, list_models, update_model

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ModelListResponse)
async def get_models(
    request: Request,
    context: Annotated[AuthContext, Depends(require_model_catalog_reader)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    provider_id: str | None = None,
    capability: str | None = None,
) -> ModelListResponse:
    """处理 模型目录 的查询型 HTTP 入口，把认证上下文中的 tenant_id 交给服务层。
    
    安全：前端参数不能伪造租户边界，后端仍按会话角色和租户过滤。
    """

    return list_models(
        request.app.state.config,
        context,
        status=status_filter,
        provider_id=provider_id,
        capability=capability,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("", response_model=ModelResponse, status_code=status.HTTP_201_CREATED)
async def post_model(
    payload: CreateModelRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_model_catalog_writer)],
) -> ModelResponse:
    """处理 模型目录 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：按钮隐藏只影响体验，实际写权限必须在依赖和服务层再次校验。
    """

    return create_model(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/{model_id}", response_model=ModelResponse)
async def patch_model(
    model_id: str,
    payload: UpdateModelRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_model_catalog_writer)],
) -> ModelResponse:
    """处理 模型目录 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：按钮隐藏只影响体验，实际写权限必须在依赖和服务层再次校验。
    """

    return update_model(
        request.app.state.config,
        context,
        model_id,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """在 模型目录 内部复用 request id 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """在 模型目录 内部复用 trace id 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "trace_id", "")
