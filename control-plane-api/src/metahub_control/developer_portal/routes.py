"""开发者门户 HTTP 路由。

所有入口复用后端 admin/developer 权限依赖，并从统一请求上下文传递 request_id/trace_id；
路由不接受 tenant_id、源码路径或密钥参数，二进制下载同样保留统一链路响应头。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from metahub_control.auth import AuthContext, require_developer_portal_reader
from metahub_control.auth.dependencies import require_workspace_or_legacy_permissions

from .models import JavaScriptSDKExampleResponse, PythonSDKExampleResponse, QuickstartDataResponse
from .service import (
    JAVASCRIPT_EXAMPLE_ARCHIVE_NAME,
    PYTHON_EXAMPLE_ARCHIVE_NAME,
    download_javascript_sdk_example,
    download_python_sdk_example,
    get_javascript_sdk_example_metadata,
    get_python_sdk_example_metadata,
    get_quickstart_data,
)

router = APIRouter(prefix="/developer", tags=["developer-portal"])
require_sdk_examples_reader = require_workspace_or_legacy_permissions("developer_portal.read")


@router.get("/quickstart-data", response_model=QuickstartDataResponse)
async def get_quickstart_data_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_developer_portal_reader)],
) -> QuickstartDataResponse:
    """读取当前租户的快速开始数据。

    后端从认证上下文恢复租户和用户，不接受前端传入 tenant_id，避免跨租户生成示例。
    """

    return get_quickstart_data(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/sdk-examples/python", response_model=PythonSDKExampleResponse)
async def get_python_sdk_example_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_sdk_examples_reader)],
) -> PythonSDKExampleResponse:
    """读取当前认证租户的 Python 示例元数据。

    ``require_sdk_examples_reader`` 在后端复核读取权限；接口不接收 ``tenant_id``、
    源码路径或任何密钥字段，viewer 即使绕过前端隐藏按钮也会得到统一权限错误。
    """

    return get_python_sdk_example_metadata(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/sdk-examples/python/download", response_class=Response)
async def download_python_sdk_example_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_sdk_examples_reader)],
) -> Response:
    """下载由固定白名单构建的 Python 示例 ZIP。

    归档在响应发送前完成路径和大小校验；成功响应只使用固定文件名，并由统一中间件补充
    ``X-Request-ID``/``X-Trace-ID``，避免二进制响应绕过链路标识。
    """

    archive = download_python_sdk_example(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{PYTHON_EXAMPLE_ARCHIVE_NAME}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/sdk-examples/javascript", response_model=JavaScriptSDKExampleResponse)
async def get_javascript_sdk_example_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_sdk_examples_reader)],
) -> JavaScriptSDKExampleResponse:
    """读取当前认证租户的 JavaScript/Node 示例元数据。

    路由与 Python 入口复用同一后端 RBAC 依赖，并且不声明 query/body 参数；租户、角色、
    源码位置和文件名均不能由请求方覆盖，防止跨租户查询或任意文件下载。
    """

    return get_javascript_sdk_example_metadata(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/sdk-examples/javascript/download", response_class=Response)
async def download_javascript_sdk_example_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_sdk_examples_reader)],
) -> Response:
    """下载由固定 JavaScript 白名单构建的受控 ZIP。

    服务层在事件写入前完成全部路径、符号链接、体积和压缩校验；路由只使用固定 ASCII
    文件名返回二进制，并由统一中间件补充请求与链路标识，不能反射任何客户端文件名。
    """

    archive = download_javascript_sdk_example(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{JAVASCRIPT_EXAMPLE_ARCHIVE_NAME}"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _request_id(request: Request) -> str:
    """从统一中间件写入的 request.state 中读取 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从统一中间件写入的 request.state 中读取 trace_id。"""

    return getattr(request.state, "trace_id", "")
