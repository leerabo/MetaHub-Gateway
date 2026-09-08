"""实验 HTTP 路由：严格解析请求、校验后端 RBAC，并统一返回错误码与 request_id。

Strict authenticated HTTP routes for PRD-075 experiments.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import RFC_4122, UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_workspace_or_legacy_permissions

from .models import (
    CreateExperimentRequest,
    ExperimentCreateResponse,
    ExperimentDetailResponse,
    ExperimentListResponse,
    ExperimentResultsResponse,
)
from .service import (
    create_experiment,
    get_experiment,
    get_experiment_results,
    list_experiments,
)

# 该常量组固定请求体上限与创建权限集合，前端可见性不能改变后端判定。
MAX_EXPERIMENT_BODY_BYTES = 16 * 1024
_CREATE_PERMISSIONS = (
    "experiment.write",
    "project.read",
    "environment.read",
    "routing.read",
    "model.read",
)


class _DuplicateJsonKey(ValueError):
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    pass


class _InvalidJsonNumber(ValueError):
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    pass


class ExperimentRoute(APIRoute):
    """封装实验路由依赖与处理器，确保权限守卫先于业务参数解析执行。

    Collapse framework validation details before they can echo input values.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """处理实验 API 命令或查询，统一附带 request_id、trace_id 与禁止缓存响应头。"""
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            """封装实验路由内部边界，固定错误映射且不回显认证材料或请求正文。"""
            try:
                return await original(request)
            # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
            except RequestValidationError:
                # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
                raise ApiError("experiment.input_invalid") from None

        return handler


router = APIRouter(prefix="/experiments", tags=["experiments"], route_class=ExperimentRoute)


def require_experiment_reader(
    context: Annotated[AuthContext, Depends(require_workspace_or_legacy_permissions("experiment.read"))],
) -> AuthContext:
    """在解析业务正文前执行实验后端权限依赖，并从实时认证上下文取得租户。"""
    # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
    return context


def require_experiment_creator(
    context: Annotated[AuthContext, Depends(require_workspace_or_legacy_permissions(*_CREATE_PERMISSIONS))],
) -> AuthContext:
    """在解析业务正文前执行实验后端权限依赖，并从实时认证上下文取得租户。"""
    return context


def require_experiment_statistics_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    # Release authentication's parsed connection before the isolated statistics worker.
    with database_request_scope(reuse_connections=True):
        return require_workspace_or_legacy_permissions("experiment.read")(request, authorization)


@router.post(
    "",
    response_model=ExperimentCreateResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": CreateExperimentRequest.model_json_schema(),
                }
            },
        }
    },
)
async def post_experiment(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_experiment_creator)],
) -> ExperimentCreateResponse:
    """处理实验 API 命令或查询，统一附带 request_id、trace_id 与禁止缓存响应头。"""
    _reject_query(request)
    idempotency_key = _idempotency_key(request)
    payload = await _parse_json(request, CreateExperimentRequest)
    _no_store(response)
    return create_experiment(
        request.app.state.config,
        context,
        payload,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        audit_request_meta=build_audit_request_meta(request),
    )


@router.get("", response_model=ExperimentListResponse)
async def get_experiments(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_experiment_reader)],
) -> ExperimentListResponse:
    """处理实验 API 命令或查询，统一附带 request_id、trace_id 与禁止缓存响应头。"""
    await _reject_get_body(request)
    query = _strict_query(request, {"status", "limit", "cursor"})
    _no_store(response)
    return list_experiments(
        request.app.state.config,
        context,
        status=query.get("status"),
        limit=query.get("limit"),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/{experiment_id}", response_model=ExperimentDetailResponse)
async def get_experiment_route(
    request: Request,
    response: Response,
    experiment_id: str,
    context: Annotated[AuthContext, Depends(require_experiment_reader)],
) -> ExperimentDetailResponse:
    """处理实验 API 命令或查询，统一附带 request_id、trace_id 与禁止缓存响应头。"""
    await _reject_get_body(request)
    _reject_query(request)
    _no_store(response)
    return get_experiment(
        request.app.state.config,
        context,
        experiment_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/{experiment_id}/results", response_model=ExperimentResultsResponse)
async def get_experiment_results_route(
    request: Request,
    response: Response,
    experiment_id: str,
    context: Annotated[AuthContext, Depends(require_experiment_statistics_reader)],
) -> ExperimentResultsResponse:
    """处理实验 API 命令或查询，统一附带 request_id、trace_id 与禁止缓存响应头。"""
    await _reject_get_body(request)
    _reject_query(request)
    _no_store(response)
    return await get_experiment_results(
        request.app.state.config,
        context,
        experiment_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


async def _parse_json[PayloadT: BaseModel](
    request: Request,
    model: type[PayloadT],
) -> PayloadT:
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    if _single_content_type(request) != "application/json":
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid")
    try:
        body = await _read_body(
            request,
            maximum=MAX_EXPERIMENT_BODY_BYTES,
            allow_empty=False,
        )
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
        if type(value) is not dict:
            # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
            raise ValueError
        _validate_json_shape(value)
        return model.model_validate(value)
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValidationError,
        _DuplicateJsonKey,
        _InvalidJsonNumber,
        RecursionError,
        TypeError,
        ValueError,
    ):
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid") from None


async def _reject_get_body(request: Request) -> None:
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    await _read_body(request, maximum=0, allow_empty=True)


async def _read_body(request: Request, *, maximum: int, allow_empty: bool) -> bytes:
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    declared = _content_length(request)
    if declared is not None and (declared > maximum or (declared == 0 and not allow_empty)):
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum:
                # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
                raise ApiError("experiment.input_invalid")
            body.extend(chunk)
    # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
    except ApiError:
        raise
    except Exception:
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid") from None
    if (not body and not allow_empty) or (declared is not None and declared != len(body)):
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid")
    return bytes(body)


def _content_length(request: Request) -> int | None:
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    values = _raw_headers(request, b"content-length")
    if not values:
        return None
    if len(values) != 1:
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid")
    try:
        value = values[0].decode("ascii", errors="strict")
    # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
    except UnicodeError:
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid") from None
    if not value or len(value) > 12 or not value.isdigit():
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid")
    return int(value)


def _single_content_type(request: Request) -> str:
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    values = _raw_headers(request, b"content-type")
    if len(values) != 1:
        return ""
    try:
        value = values[0].decode("ascii", errors="strict")
    # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
    except UnicodeError:
        return ""
    return value


def _idempotency_key(request: Request) -> str:
    """封装实验路由内部边界，固定错误映射且不回显认证材料或请求正文。"""
    values = _raw_headers(request, b"idempotency-key")
    if not values:
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.idempotency_required")
    if len(values) != 1:
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid")
    try:
        value = values[0].decode("ascii", errors="strict")
    # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
    except UnicodeError:
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid") from None
    framework_value = request.headers.get("idempotency-key")
    if (
        len(value) != 36
        or value != framework_value
        or value != value.strip()
        or "," in value
    ):
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid")
    try:
        parsed = UUID(value)
    # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
    except (AttributeError, ValueError):
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid") from None
    if str(parsed) != value or parsed.version != 4 or parsed.variant != RFC_4122:
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.input_invalid")
    return value


def _raw_headers(request: Request, name: bytes) -> list[bytes]:
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    return [
        value
        for key, value in request.scope.get("headers", ())
        if key.lower() == name
    ]


def _strict_query(request: Request, allowed: set[str]) -> dict[str, str]:
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    raw = request.scope.get("query_string", b"")
    try:
        text = raw.decode("ascii", errors="strict")
    # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
    except UnicodeError:
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.query_invalid") from None
    if "%" in text or "+" in text:
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.query_invalid")
    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in result or not value:
            # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
            raise ApiError("experiment.query_invalid")
        result[key] = value
    if text:
        expected = "&".join(f"{key}={value}" for key, value in result.items())
        if expected != text:
            # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
            raise ApiError("experiment.query_invalid")
    return result


def _reject_query(request: Request) -> None:
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    if request.scope.get("query_string", b""):
        # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
        raise ApiError("experiment.query_invalid")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """封装实验路由内部边界，固定错误映射且不回显认证材料或请求正文。"""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _reject_json_number(_: str) -> None:
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    raise _InvalidJsonNumber


def _validate_json_shape(value: object) -> None:
    """严格解析实验 HTTP 输入并拒绝重复键、非规范数字、未知查询或多值头。"""
    nodes = 0

    def visit(item: object, depth: int) -> None:
        """封装实验路由内部边界，固定错误映射且不回显认证材料或请求正文。"""
        nonlocal nodes
        nodes += 1
        if nodes > 128 or depth > 8:
            # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
            raise ValueError
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
                    raise ValueError
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif item is not None and type(item) not in {str, int, bool}:
            # HTTP 输入、权限或请求标识不满足固定契约时拒绝，不回显正文与认证材料。
            raise ValueError

    visit(value, 1)


def _no_store(response: Response) -> None:
    """封装实验路由内部边界，固定错误映射且不回显认证材料或请求正文。"""
    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    """封装实验路由内部边界，固定错误映射且不回显认证材料或请求正文。"""
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """封装实验路由内部边界，固定错误映射且不回显认证材料或请求正文。"""
    return getattr(request.state, "trace_id", "")


__all__ = [
    "require_experiment_creator",
    "require_experiment_reader",
    "router",
]
