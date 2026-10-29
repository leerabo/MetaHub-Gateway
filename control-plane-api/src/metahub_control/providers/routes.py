"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，
Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/providers/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from starlette.concurrency import run_in_threadpool

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_provider_reader_or_machine,
    require_workspace_or_legacy_permissions,
)
from metahub_control.machine_identity.service import MachineAuthContext

from .models import (
    CreateProviderKeyRequest,
    CreateProviderRequest,
    ProviderKeyListResponse,
    ProviderKeyDeleteResponse,
    ProviderKeyResponse,
    ProviderListResponse,
    ProviderDeleteResponse,
    ProviderResponse,
    ProviderTestResponse,
    TestProviderRequest,
    UpdateProviderKeyRequest,
    UpdateProviderRequest,
)
from .service import (
    create_provider,
    create_provider_key,
    delete_provider_key,
    delete_provider,
    list_provider_keys,
    list_providers,
    test_provider_connection,
    update_provider,
    update_provider_key,
)

router = APIRouter(prefix="/providers", tags=["providers"])
provider_keys_router = APIRouter(prefix="/provider-keys", tags=["provider-keys"])


@router.get("", response_model=ProviderListResponse)
async def get_providers(
    request: Request,
    context: Annotated[
        AuthContext | MachineAuthContext,
        Depends(require_provider_reader_or_machine),
    ],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    provider_type: str | None = None,
) -> ProviderListResponse:
    """列出当前租户 Provider。

    require_provider_reader 先完成稳定权限码校验，service 层再按 context.tenant_id 做租户隔离。
    """

    return list_providers(
        request.app.state.config,
        context,
        status=status_filter,
        provider_type=provider_type,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
async def post_provider(
    payload: CreateProviderRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.write")),
    ],
) -> ProviderResponse:
    """处理 Provider 与 KeyPool 管理 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：路由依赖按数据库权限快照校验 ``provider.write``，服务层只使用可信上下文中的
    ``tenant_id`` 操作当前租户数据；前端隐藏按钮不构成授权边界。
    """

    return create_provider(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/{provider_id}", response_model=ProviderResponse)
async def patch_provider(
    provider_id: str,
    payload: UpdateProviderRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.write")),
    ],
) -> ProviderResponse:
    """更新当前租户 Provider 元数据或状态。"""

    return update_provider(
        request.app.state.config,
        context,
        provider_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.delete("/{provider_id}", response_model=ProviderDeleteResponse)
async def delete_provider_route(
    provider_id: str,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.write")),
    ],
) -> ProviderDeleteResponse:
    """删除没有任何模型、调用或历史事实引用的 Provider。"""

    return delete_provider(
        request.app.state.config,
        context,
        provider_id,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/{provider_id}/test", response_model=ProviderTestResponse)
async def post_provider_test(
    provider_id: str,
    payload: TestProviderRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.write")),
    ],
) -> ProviderTestResponse:
    """处理 Provider 与 KeyPool 管理 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：路由依赖按数据库权限快照校验 ``provider.write``，服务层只使用可信上下文中的
    ``tenant_id`` 操作当前租户数据；前端隐藏按钮不构成授权边界。
    """

    return await run_in_threadpool(
        test_provider_connection,
        request.app.state.config,
        context,
        provider_id,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/{provider_id}/keys", response_model=ProviderKeyListResponse)
async def get_provider_keys(
    provider_id: str,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.read")),
    ],
) -> ProviderKeyListResponse:
    """处理 Provider 与 KeyPool 管理 的查询型 HTTP 入口，把认证上下文中的 tenant_id 交给服务层。
    
    安全：路由依赖按数据库权限快照校验 ``provider.read``，服务层按
    ``context.tenant_id`` 隔离数据；前端参数不能选择租户或补充角色权限。
    """

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return list_provider_keys(
        request.app.state.config,
        context,
        provider_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/{provider_id}/keys",
    response_model=ProviderKeyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_provider_key(
    provider_id: str,
    payload: CreateProviderKeyRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.write")),
    ],
) -> ProviderKeyResponse:
    """处理 Provider 与 KeyPool 管理 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：路由依赖按数据库权限快照校验 ``provider.write``，服务层只使用可信上下文中的
    ``tenant_id`` 操作当前租户数据；前端隐藏按钮不构成授权边界。
    """

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return create_provider_key(
        request.app.state.config,
        context,
        provider_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@provider_keys_router.patch("/{provider_key_id}", response_model=ProviderKeyResponse)
async def patch_provider_key(
    provider_key_id: str,
    payload: UpdateProviderKeyRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.write")),
    ],
) -> ProviderKeyResponse:
    """处理 Provider 与 KeyPool 管理 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：路由依赖按数据库权限快照校验 ``provider.write``，服务层只使用可信上下文中的
    ``tenant_id`` 操作当前租户数据；前端隐藏按钮不构成授权边界。
    """

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return update_provider_key(
        request.app.state.config,
        context,
        provider_key_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@provider_keys_router.delete("/{provider_key_id}", response_model=ProviderKeyDeleteResponse)
async def delete_provider_key_route(
    provider_key_id: str,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.write")),
    ],
) -> ProviderKeyDeleteResponse:
    """删除当前租户中未被运行绑定引用的 Provider Key。"""

    return delete_provider_key(
        request.app.state.config,
        context,
        provider_key_id,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """读取统一中间件生成或透传的 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件生成或透传的 trace_id。"""

    return getattr(request.state, "trace_id", "")
