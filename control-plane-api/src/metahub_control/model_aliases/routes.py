"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，
Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/model_aliases/routes.py
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_workspace_or_legacy_permissions

from .cache_affinity_service import update_cache_affinity
from .models import (
    CacheAffinityMutationResponse,
    CreateModelAliasRequest,
    FallbackPolicyResponse,
    ModelAliasListResponse,
    ModelAliasResponse,
    RoutingPolicyMutationResponse,
    UpdateFallbackPolicyRequest,
    UpdateModelAliasRequest,
)
from .service import (
    create_model_alias,
    list_model_aliases,
    update_fallback_policy,
    update_model_alias,
    update_routing_mode,
)

router = APIRouter(prefix="/model-aliases", tags=["model-aliases"])

# Routing mode 请求最多包含两个标量和五字段 quality_cost_config；8 KiB 足够正常配置，
# 同时可在认证后尽早阻断异常 wire，避免无界解析。
MAX_ROUTING_MODE_BODY_BYTES = 8 * 1024
# 虽然合法载荷只有根对象和两个标量，仍给未来兼容预留少量层级；显式上限可阻止
# 8 KiB 内的深层 JSON 消耗解码器/遍历器调用栈，节点上限则约束宽数组或宽对象。
MAX_ROUTING_MODE_JSON_DEPTH = 16
MAX_ROUTING_MODE_JSON_NODES = 64


class _DuplicateJsonKey(ValueError):
    """标记 JSON 对象重复键，避免后一个值静默覆盖前一个安全检查结果。"""


@router.get("", response_model=ModelAliasListResponse)
async def get_model_aliases(
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("routing.read")),
    ],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    environment_id: str | None = None,
    environment: str | None = None,
) -> ModelAliasListResponse:
    """处理 模型别名路由 的查询型 HTTP 入口，把认证上下文中的 tenant_id 交给服务层。
    
    安全：前端参数不能伪造租户边界，后端仍按会话角色和租户过滤。
    """

    return list_model_aliases(
        request.app.state.config,
        context,
        status=status_filter,
        environment_id=environment_id,
        environment=environment,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("", response_model=ModelAliasResponse, status_code=status.HTTP_201_CREATED)
async def post_model_alias(
    payload: CreateModelAliasRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("routing.write")),
    ],
) -> ModelAliasResponse:
    """处理 模型别名路由 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：按钮隐藏只影响体验，实际写权限必须在依赖和服务层再次校验。
    """

    return create_model_alias(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/{model_alias_id}", response_model=ModelAliasResponse)
async def patch_model_alias(
    model_alias_id: str,
    payload: UpdateModelAliasRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("routing.write")),
    ],
) -> ModelAliasResponse:
    """处理 模型别名路由 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：按钮隐藏只影响体验，实际写权限必须在依赖和服务层再次校验。
    """

    return update_model_alias(
        request.app.state.config,
        context,
        model_alias_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch(
    "/{model_alias_id}/cache-affinity",
    response_model=CacheAffinityMutationResponse,
)
async def patch_cache_affinity(
    model_alias_id: str,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("routing.write")),
    ],
) -> CacheAffinityMutationResponse:
    """认证通过后严格更新当前租户 Alias 的 Prompt Cache 亲和配置。

    这里刻意不声明 Pydantic Body 参数，使 FastAPI 先执行 admin 依赖，再读取最多
    8 KiB 的不可信正文。developer/viewer 的攻击载荷因此只得到权限错误，不能通过
    解析耗时、编码错误或字段差异探测写接口实现。
    """

    payload = await _read_cache_affinity_payload(request)
    return update_cache_affinity(
        request.app.state.config,
        context,
        model_alias_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/{model_alias_id}/fallback", response_model=FallbackPolicyResponse)
async def patch_fallback_policy(
    model_alias_id: str,
    payload: UpdateFallbackPolicyRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("routing.write")),
    ],
) -> FallbackPolicyResponse:
    """修改当前租户模型别名的非流式 Fallback 策略。

    权限依赖在路由层强制 admin，服务层仍只使用认证上下文中的 tenant_id 查询
    别名和模型，防止调用者通过路径 ID 或请求体构造跨租户主备关系。
    """

    return update_fallback_policy(
        request.app.state.config,
        context,
        model_alias_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch(
    "/{model_alias_id}/routing-mode",
    response_model=RoutingPolicyMutationResponse,
)
async def patch_routing_mode(
    model_alias_id: str,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("routing.write")),
    ],
) -> RoutingPolicyMutationResponse:
    """认证通过后读取严格小 JSON，并按版本 CAS 修改当前租户 RoutingPolicy。

    路由刻意不声明 Pydantic Body 参数，使 401/403 在读取攻击者正文之前完成；结构、
    重复键、编码和字段集合错误统一映射到 ModelAlias 领域错误码并保留 request_id。
    """

    payload = await _read_routing_mode_payload(request)
    return update_routing_mode(
        request.app.state.config,
        context,
        model_alias_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


async def _read_routing_mode_payload(request: Request) -> dict[str, Any]:
    """有界读取 Routing mode JSON object，拒绝重复键、控制字符和非 JSON wire。

    参数：已经通过 admin 依赖的 FastAPI Request。返回：未做业务枚举转换的普通字典。
    关键约束：最多读取 8 KiB，且只能接受 UTF-8 application/json；失败不回显正文。
    """

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    # 路由模式属于行为开关，只接受明确 JSON 媒体类型，避免表单或文本被隐式转换。
    if content_type != "application/json":
        raise ApiError("model_alias.routing_mode_invalid")
    declared = request.headers.get("content-length")
    if declared:
        # 非法长度头不能交给流读取器猜测，否则权限后的资源上限可能被绕过。
        try:
            declared_length = int(declared)
        except ValueError as exc:
            # 长度头解析失败只返回固定领域码，禁止把原始 Header 或转换错误写入响应。
            raise ApiError("model_alias.routing_mode_invalid") from exc
        # 空正文和超过八 KiB 的正文均在分配完整缓冲前拒绝。
        if declared_length < 1 or declared_length > MAX_ROUTING_MODE_BODY_BYTES:
            raise ApiError("model_alias.routing_mode_invalid")

    body = bytearray()
    async for chunk in request.stream():
        # 实际流长度仍需独立计数，不能信任调用方声明的 Content-Length。
        if len(body) + len(chunk) > MAX_ROUTING_MODE_BODY_BYTES:
            raise ApiError("model_alias.routing_mode_invalid")
        body.extend(chunk)
    # 空对象载荷没有可执行意图，统一使用领域错误而不是框架默认校验错误。
    if not body:
        raise ApiError("model_alias.routing_mode_invalid")
    try:
        text = bytes(body).decode("utf-8", errors="strict")
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    # Python JSON 解码器可能在 8 KiB 内的深层数组上抛 RecursionError，超长整数也可能
    # 抛 ValueError；两者都属于不可信 wire，而不是应暴露为 500 的服务故障。
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ApiError("model_alias.routing_mode_invalid") from exc
    # 顶层必须是有界、无控制字符对象；迭代扫描避免再次使用 Python 调用栈。
    if not isinstance(parsed, dict) or _has_unsafe_json_structure(parsed):
        raise ApiError("model_alias.routing_mode_invalid")
    return parsed


async def _read_cache_affinity_payload(request: Request) -> dict[str, Any]:
    """在 admin 权限通过后有界读取 CacheAffinity 完整配置对象。

    参数：可信 FastAPI Request。返回：保留 JSON 精确类型、尚未做领域归一化的字典。
    关键约束：只接受 UTF-8 application/json，拒绝重复键、控制字符、深层/过宽结构
    和超过 8 KiB 的正文；任何失败都使用固定领域码且不回显攻击载荷。
    """

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        # CacheAffinity 只接受 JSON 正文，避免表单或文本解析产生类型与重复键歧义。
        raise ApiError("cache_affinity.input_invalid")
    declared = request.headers.get("content-length")
    if declared:
        try:
            declared_length = int(declared)
        # Content-Length 来自不可信客户端，解析失败时只返回固定错误而不回显原值。
        except ValueError as exc:
            # Content-Length 属于不可信 wire，解析器错误文本不能进入统一响应。
            raise ApiError("cache_affinity.input_invalid") from exc
        if declared_length < 1 or declared_length > MAX_ROUTING_MODE_BODY_BYTES:
            raise ApiError("cache_affinity.input_invalid")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_ROUTING_MODE_BODY_BYTES:
            # 流式读取也执行实时上限，防止缺失或伪造长度头绕过正文容量门禁。
            raise ApiError("cache_affinity.input_invalid")
        body.extend(chunk)
    if not body:
        # 空正文不具备完整快照语义，必须在进入服务层和写事务前直接拒绝。
        raise ApiError("cache_affinity.input_invalid")
    try:
        text = bytes(body).decode("utf-8", errors="strict")
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    # 解码与 JSON 结构错误可能携带攻击正文片段，统一转换后禁止原文外泄。
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # 解码器异常可能包含正文片段；只向外转换固定错误码。
        raise ApiError("cache_affinity.input_invalid") from exc
    if not isinstance(parsed, dict) or _has_unsafe_json_structure(parsed):
        raise ApiError("cache_affinity.input_invalid")
    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """把 JSON pairs 转为字典，并在任何对象层级发现重复键时立即拒绝。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _has_unsafe_json_structure(value: object) -> bool:
    """迭代检查 JSON 的控制字符、深度和节点数，避免攻击者耗尽 Python 调用栈。

    参数：已经由标准 JSON 解码器生成的根对象。返回：结构越界或包含控制字符时为 True。
    关键约束：对象键也计入节点和深度，因为键同样会进入字段校验及潜在结构化日志。
    """

    stack: list[tuple[object, int]] = [(value, 1)]
    visited_nodes = 0
    while stack:
        current, depth = stack.pop()
        visited_nodes += 1
        if (
            depth > MAX_ROUTING_MODE_JSON_DEPTH
            or visited_nodes > MAX_ROUTING_MODE_JSON_NODES
        ):
            return True
        if isinstance(current, str):
            if any(ord(character) < 32 for character in current):
                return True
            continue
        if isinstance(current, dict):
            # 反向压栈只为保持稳定遍历顺序；安全结论不依赖对象字段顺序。
            for key, child in reversed(list(current.items())):
                stack.append((child, depth + 1))
                stack.append((key, depth + 1))
            continue
        if isinstance(current, list):
            for child in reversed(current):
                stack.append((child, depth + 1))
    return False


def _request_id(request: Request) -> str:
    """在 模型别名路由 内部复用 request id 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """在 模型别名路由 内部复用 trace id 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "trace_id", "")
