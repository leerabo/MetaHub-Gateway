"""调用日志与个人 SavedFilter 的控制面路由。

所有端点先通过 require_request_log_reader 执行后端 RBAC；tenant、owner 和 session
只取认证上下文。静态 SavedFilter 路由必须注册在 /{log_request_id} 之前，避免路径误捕获。
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import (
    AuthContext,
    require_request_log_reader,
    require_request_log_reader_or_machine,
)
from metahub_control.machine_identity.service import MachineAuthContext

from .models import (
    RequestLogDetailResponse,
    RequestLogListResponse,
    SavedFilterListResponse,
    SavedFilterResponse,
)
from .query import list_request_logs_advanced, list_request_logs_machine
from .saved_filters import (
    create_saved_filter,
    delete_saved_filter,
    list_saved_filters,
    update_saved_filter,
)
from .service import get_request_log_detail

router = APIRouter(prefix="/request-logs", tags=["request-logs"])

# SavedFilter 只包含名称、白名单筛选与排序，8 KiB 足以覆盖合法合同并限制解析资源。
MAX_SAVED_FILTER_BODY_BYTES = 8 * 1024
CREATE_BODY_FIELDS = frozenset({"name", "filters", "sort_by", "sort_direction"})
PATCH_BODY_FIELDS = CREATE_BODY_FIELDS | {"version"}


@router.get("", response_model=RequestLogListResponse)
async def get_request_logs(
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext | MachineAuthContext,
        Depends(require_request_log_reader_or_machine),
    ],
) -> RequestLogListResponse:
    """高级或兼容模式分页查询当前租户调用日志，并写 query_executed 源事实。"""

    _private_no_store(response)
    # multi_items 保留重复单值参数，服务层可关闭拒绝而不是由“最后一个值”制造歧义。
    query_items = tuple(request.query_params.multi_items())
    if isinstance(context, MachineAuthContext):
        return list_request_logs_machine(
            request.app.state.config,
            context,
            query_items=query_items,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    return list_request_logs_advanced(
        request.app.state.config,
        context,
        query_items=query_items,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/saved-filters", response_model=SavedFilterListResponse)
async def get_saved_filters(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_request_log_reader)],
) -> SavedFilterListResponse:
    """列出当前 tenant、当前 user 自己的 active 保存筛选。"""

    _private_no_store(response)
    return list_saved_filters(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/saved-filters", response_model=SavedFilterResponse, status_code=201)
async def post_saved_filter(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_request_log_reader)],
) -> SavedFilterResponse:
    """RBAC 通过后有界读取原始 JSON，并创建当前用户私有筛选。"""

    _private_no_store(response)
    body = await _read_saved_filter_body(request, allowed_fields=CREATE_BODY_FIELDS)
    return create_saved_filter(
        request.app.state.config,
        context,
        body,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/saved-filters/{saved_filter_id}", response_model=SavedFilterResponse)
async def patch_saved_filter(
    saved_filter_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_request_log_reader)],
) -> SavedFilterResponse:
    """RBAC 通过后有界读取原始 JSON，并按乐观 version 更新个人筛选。"""

    _private_no_store(response)
    body = await _read_saved_filter_body(request, allowed_fields=PATCH_BODY_FIELDS)
    return update_saved_filter(
        request.app.state.config,
        context,
        saved_filter_id,
        body,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.delete("/saved-filters/{saved_filter_id}", status_code=204)
async def remove_saved_filter(
    saved_filter_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_request_log_reader)],
    version: str | None = None,
) -> Response:
    """按乐观 version 软删除当前用户筛选，目标行和任何查询证据都不物理删除。"""

    _private_no_store(response)
    delete_saved_filter(
        request.app.state.config,
        context,
        saved_filter_id,
        version,
    )
    # 204 不含 JSON 正文；统一 request_id/trace_id 仍由全局响应头和中间件提供。
    result = Response(status_code=204)
    _private_no_store(result)
    return result


@router.get("/{log_request_id}", response_model=RequestLogDetailResponse)
async def get_request_log(
    log_request_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_request_log_reader)],
) -> RequestLogDetailResponse:
    """读取当前租户内单条调用日志详情。"""

    _private_no_store(response)
    return get_request_log_detail(
        request.app.state.config,
        context,
        log_request_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _private_no_store(response: Response) -> None:
    """为所有成功响应冻结身份相关的禁缓存头。"""

    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    existing = [
        item.strip()
        for item in response.headers.get("Vary", "").split(",")
        if item.strip()
    ]
    # Vary 必须显式包含认证维度，防止代理把一个用户的筛选响应复用给另一身份。
    if not any(item.lower() == "authorization" for item in existing):
        existing.append("Authorization")
    response.headers["Vary"] = ", ".join(existing)


def _request_id(request: Request) -> str:
    """读取统一中间件生成的 request_id，不接受 query/body 覆盖。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件生成的 trace_id，不接受 query/body 覆盖。"""

    return getattr(request.state, "trace_id", "")


async def _read_saved_filter_body(
    request: Request,
    *,
    allowed_fields: frozenset[str],
) -> dict[str, object]:
    """在后端 RBAC 通过后有界读取唯一 UTF-8 JSON object。

    原始正文只驻留本次请求内存；重复键、未知字段、含糊媒体类型、长度不一致和
    非标准 JSON 常量统一关闭为 saved_filter.invalid，且不把正文写入日志。
    """

    _validate_json_content_type(request)
    expected_length = _validated_content_length(request)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_SAVED_FILTER_BODY_BYTES:
            # 实际流独立计数，不能信任客户端声明的 Content-Length。
            raise ApiError("saved_filter.invalid")
        chunks.append(chunk)
    if size == 0 or (expected_length is not None and expected_length != size):
        # 空正文或长度不一致可能产生代理/应用解释差异，必须失败关闭。
        raise ApiError("saved_filter.invalid")
    try:
        parsed = json.loads(
            b"".join(chunks).decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    # 编码、递归深度和 JSON 语法异常都折叠成稳定业务错误，原始正文不得回显。
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        # 解析器异常可能包含正文片段，因此只透出 SavedFilter 固定错误码。
        raise ApiError("saved_filter.invalid") from exc
    if not isinstance(parsed, dict) or set(parsed) - allowed_fields:
        # tenant、owner、status 以及任何未知字段都不允许从客户端进入领域层。
        raise ApiError("saved_filter.invalid")
    return parsed


def _validate_json_content_type(request: Request) -> None:
    """要求唯一 application/json，可选参数只允许一次 UTF-8 charset。"""

    values = _raw_header_values(request, b"content-type")
    # 缺失或重复媒体类型会造成代理解释歧义，不能任意选择其中一个值。
    if len(values) != 1:
        raise ApiError("saved_filter.invalid")
    try:
        raw = values[0].decode("ascii")
    # Content-Type 语法只允许 ASCII，编码异常统一关闭且不回显原始头。
    except UnicodeDecodeError as exc:
        raise ApiError("saved_filter.invalid") from exc
    parts = [part.strip() for part in raw.split(";")]
    # 写接口只接受明确 JSON，禁止文本或表单触发另一套正文解释路径。
    if not parts or parts[0].lower() != "application/json":
        raise ApiError("saved_filter.invalid")
    seen_charset = False
    for parameter in parts[1:]:
        # 裸参数和空段不是明确媒体类型合同，必须在读取正文前失败关闭。
        if not parameter or "=" not in parameter:
            raise ApiError("saved_filter.invalid")
        name, value = (part.strip() for part in parameter.split("=", 1))
        # 仅允许一个 charset，避免重复参数在不同代理中采用首值或末值。
        if name.lower() != "charset" or seen_charset:
            raise ApiError("saved_filter.invalid")
        # SavedFilter JSON 固定使用 UTF-8，其他字符集不能进入统一规范化器。
        if value.lower() not in {"utf-8", '"utf-8"'}:
            raise ApiError("saved_filter.invalid")
        seen_charset = True


def _validated_content_length(request: Request) -> int | None:
    """校验最多一个 canonical Content-Length，并在读取前拒绝超限声明。"""

    values = _raw_header_values(request, b"content-length")
    # 重复长度头可能形成请求走私边界，服务端不能信任任意一个声明。
    if len(values) > 1:
        raise ApiError("saved_filter.invalid")
    if not values:
        return None
    try:
        raw = values[0].decode("ascii")
    # 长度声明必须是 ASCII 数字，解码失败不能泄露底层头解析细节。
    except UnicodeDecodeError as exc:
        raise ApiError("saved_filter.invalid") from exc
    # 前导零、空值和越界长度都可能绕过上游限额，统一按非法正文处理。
    if (
        not raw.isdigit()
        or (len(raw) > 1 and raw.startswith("0"))
        or int(raw) < 1
        or int(raw) > MAX_SAVED_FILTER_BODY_BYTES
    ):
        # 非规范或越界长度可能造成正文解析歧义，必须在读取请求体之前按统一输入错误拒绝。
        raise ApiError("saved_filter.invalid")
    return int(raw)


def _raw_header_values(request: Request, name: bytes) -> list[bytes]:
    """直接读取 ASGI 原始请求头，保留重复项用于关闭请求走私歧义。"""

    return [
        value
        for key, value in request.scope.get("headers", [])
        if key.lower() == name
    ]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝任意层级重复 JSON key，避免代理与服务端采用不同覆盖规则。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        # 任意层级重复键都会造成覆盖优先级歧义，不能采用默认末值胜出规则。
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    """拒绝 NaN/Infinity 等非标准 JSON 常量，避免跨语言解释不一致。"""

    # 非标准数值在不同语言中结果不一致，必须在进入领域校验前直接拒绝。
    raise ValueError("non-standard JSON constant")
