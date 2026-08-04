"""PRD-067 企业 Prompt 模板管理 HTTP 路由。

所有路由先执行 Bearer Session 和数据库实时权限依赖，再读取 query/path/body。JSON 正文采用
有界 UTF-8 解码、任意层重复键拒绝和 Pydantic exact-set；框架校验详情统一折叠为固定模板
错误码，避免正文、变量值或注入字段进入响应。所有成功响应均明确 ``no-store``。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_prompt_template_catalog_user,
    require_prompt_template_reader,
    require_prompt_template_writer,
)

from .models import (
    CreatePromptTemplateRequest,
    PatchPromptTemplateRequest,
    PromptTemplateDefinitionResponse,
    PromptTemplateDetailResponse,
    PromptTemplateListResponse,
    PromptTemplateMutationResponse,
    PromptTemplateOptionsResponse,
    PublishPromptTemplateVersionRequest,
)
from .service import (
    DEFAULT_LIST_LIMIT,
    create_prompt_template,
    get_prompt_template,
    get_prompt_template_definition,
    list_prompt_template_options,
    list_prompt_templates,
    patch_prompt_template,
    publish_prompt_template_version,
)

# 128 KiB canonical definition 在最坏 JSON ``\uXXXX`` 转义下仍被 1 MiB wire 门禁覆盖；
# 门禁只用于接收编码开销，不放宽 shared definition 的实际正文与 canonical 上限。
MAX_PROMPT_TEMPLATE_WIRE_BYTES = 1024 * 1024
_LIST_QUERY_FIELDS = frozenset({"status", "search", "limit", "offset"})


class _DuplicateJsonKey(ValueError):
    """表示任意 JSON 对象层出现重复键；异常不得保存或回显具体键名。"""


class PromptTemplateRoute(APIRoute):
    """把本域 FastAPI 请求校验详情固定转换为 ``prompt_template.request_invalid``。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """返回保留认证/业务错误优先级的模板域处理器。"""

        original_handler = super().get_route_handler()

        async def prompt_template_handler(request: Request) -> Response:
            """执行原路由，并禁止框架 loc/input 详情穿透模板敏感边界。"""

            try:
                return await original_handler(request)
            # FastAPI 校验详情可能携带字段位置或原始输入，模板域只能返回目录化固定错误。
            except RequestValidationError:
                raise ApiError("prompt_template.request_invalid") from None

        return prompt_template_handler


router = APIRouter(
    prefix="/prompt-templates",
    tags=["prompt-templates"],
    route_class=PromptTemplateRoute,
)


@router.get("", response_model=PromptTemplateListResponse)
async def get_prompt_templates(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_prompt_template_reader)],
) -> PromptTemplateListResponse:
    """按状态、搜索和有界分页返回当前租户模板安全列表。"""

    await _require_empty_body(request)
    status_filter, search, limit, offset = _parse_list_query(request)
    _no_store(response)
    return list_prompt_templates(
        request.app.state.config,
        context,
        status=status_filter,
        search=search,
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "",
    response_model=PromptTemplateMutationResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": CreatePromptTemplateRequest.model_json_schema()
                }
            },
        }
    },
)
async def post_prompt_template(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_prompt_template_writer)],
) -> PromptTemplateMutationResponse:
    """创建模板并原子封印 v1；同一规范意图重放返回 200。"""

    _reject_all_query(request)
    payload = await _parse_json_body(request, CreatePromptTemplateRequest)
    result = create_prompt_template(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.status_code = status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
    _no_store(response)
    return result


# 静态 /options 必须先于 /{template_id} 注册，避免被动态路由当作资源 ID。
@router.get("/options", response_model=PromptTemplateOptionsResponse)
async def get_prompt_template_options(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_prompt_template_catalog_user)],
) -> PromptTemplateOptionsResponse:
    """返回具有 read 或 write 任一权限用户的最小 active 绑定目录。"""

    _reject_all_query(request)
    await _require_empty_body(request)
    _no_store(response)
    return list_prompt_template_options(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/{template_id}", response_model=PromptTemplateDetailResponse)
async def get_prompt_template_detail(
    template_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_prompt_template_reader)],
) -> PromptTemplateDetailResponse:
    """读取同租户模板元数据与不可变版本历史，不返回定义正文。"""

    _reject_all_query(request)
    await _require_empty_body(request)
    _no_store(response)
    return get_prompt_template(
        request.app.state.config,
        context,
        template_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get(
    "/{template_id}/versions/{version_id}",
    response_model=PromptTemplateDefinitionResponse,
)
async def get_prompt_template_version_definition(
    template_id: str,
    version_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_prompt_template_reader)],
) -> PromptTemplateDefinitionResponse:
    """为具有 read 权限的当前租户用户返回一份精确解密定义。"""

    _reject_all_query(request)
    await _require_empty_body(request)
    _no_store(response)
    return get_prompt_template_definition(
        request.app.state.config,
        context,
        template_id,
        version_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch(
    "/{template_id}",
    response_model=PromptTemplateMutationResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": PatchPromptTemplateRequest.model_json_schema()
                }
            },
        }
    },
)
async def patch_prompt_template_metadata(
    template_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_prompt_template_writer)],
) -> PromptTemplateMutationResponse:
    """按 revision CAS 修改当前租户模板 metadata 或启停状态。"""

    _reject_all_query(request)
    payload = await _parse_json_body(request, PatchPromptTemplateRequest)
    result = patch_prompt_template(
        request.app.state.config,
        context,
        template_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _no_store(response)
    return result


@router.post(
    "/{template_id}/versions",
    response_model=PromptTemplateMutationResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": PublishPromptTemplateVersionRequest.model_json_schema()
                }
            },
        }
    },
)
async def post_prompt_template_version(
    template_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_prompt_template_writer)],
) -> PromptTemplateMutationResponse:
    """按 metadata revision 发布下一份完整版本；幂等重放返回 200。"""

    _reject_all_query(request)
    payload = await _parse_json_body(request, PublishPromptTemplateVersionRequest)
    result = publish_prompt_template_version(
        request.app.state.config,
        context,
        template_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.status_code = status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
    _no_store(response)
    return result


def _parse_list_query(request: Request) -> tuple[str | None, str | None, int, int]:
    """严格解析列表 query exact-set，并拒绝未知键、重复键和宽松整数表示。"""

    pairs = request.query_params.multi_items()
    # 未知 query 可能伪造 tenant/actor 或扩张筛选面，必须在业务查询前失败关闭。
    if any(key not in _LIST_QUERY_FIELDS for key, _ in pairs):
        raise ApiError("prompt_template.request_invalid")
    keys = [key for key, _ in pairs]
    # 重复键的代理/框架取值顺序不稳定，拒绝歧义而不是静默选择首项或末项。
    if len(keys) != len(set(keys)):
        raise ApiError("prompt_template.request_invalid")
    values = dict(pairs)
    status_filter = values.get("status")
    search = values.get("search")
    limit = _parse_decimal(values.get("limit"), default=DEFAULT_LIST_LIMIT, minimum=1)
    offset = _parse_decimal(values.get("offset"), default=0, minimum=0)
    return status_filter, search, limit, offset


def _parse_decimal(value: str | None, *, default: int, minimum: int) -> int:
    """解析无符号 ASCII 十进制 query；拒绝符号、空白、前导加号与 Unicode 数字。"""

    if value is None:
        return default
    # 只接受 ASCII 十进制，禁止符号、空白和 Unicode 数字造成多层解析不一致。
    if not value or any(character not in "0123456789" for character in value):
        raise ApiError("prompt_template.request_invalid")
    try:
        parsed = int(value, 10)
    # 即使字符门禁已通过仍保留转换失败兜底，并切断含攻击输入的异常链。
    except ValueError:
        raise ApiError("prompt_template.request_invalid") from None
    # 分页下界由服务端冻结，负值或零 limit 不得被数据库方言宽松解释。
    if parsed < minimum:
        raise ApiError("prompt_template.request_invalid")
    return parsed


def _reject_all_query(request: Request) -> None:
    """拒绝非列表路由的全部 query，防止 tenant/actor/Key 或定义覆盖注入。"""

    # 资源路由不定义任何 query；一律拒绝可避免客户端注入隐藏 owner 或正文覆盖字段。
    if request.query_params.multi_items():
        raise ApiError("prompt_template.request_invalid")


async def _parse_json_body[RequestPayloadT: BaseModel](
    request: Request,
    model_type: type[RequestPayloadT],
) -> RequestPayloadT:
    """在权限依赖完成后有界解析严格 JSON，并把所有校验详情折叠为固定错误码。"""

    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    # 只接收 JSON，避免表单或文本解析器采用不同重复键和字符集规则。
    if media_type != "application/json":
        raise ApiError("prompt_template.request_invalid")
    body = await _read_bounded_body(request, maximum=MAX_PROMPT_TEMPLATE_WIRE_BYTES)
    # 空正文不能被解释为默认 DTO，否则会绕过 exact-set 和幂等意图指纹。
    if not body:
        raise ApiError("prompt_template.request_invalid")
    try:
        decoded = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
        # 顶层必须是对象；数组、标量和 null 不得由 Pydantic 宽松转换。
        if type(decoded) is not dict:
            raise ValueError
        return model_type.model_validate(decoded)
    # 解析/校验错误可能持有 Prompt 或变量值，切断异常链并统一为固定公共错误。
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        TypeError,
        RecursionError,
    ):
        # 所有解析详情可能含 Prompt 或变量输入，统一切断异常链并只返回公共目录错误。
        raise ApiError("prompt_template.request_invalid") from None


async def _require_empty_body(request: Request) -> None:
    """拒绝 GET 正文，且不缓存攻击者提交的无界 payload。"""

    body = await _read_bounded_body(request, maximum=0)
    # GET 正文没有合同语义，拒绝而不是忽略，避免代理与应用看到不同请求。
    if body:
        raise ApiError("prompt_template.request_invalid")


async def _read_bounded_body(request: Request, *, maximum: int) -> bytes:
    """逐块读取最多 ``maximum`` 字节，并严格核对唯一规范 Content-Length。"""

    # Headers.get 会折叠重复字段，必须从 ASGI 原始 header 列表检查唯一性，避免不同代理
    # 分别选择首值或末值。Header 值只按 ASCII 十进制解释，绝不写入异常或日志。
    declared_values = [
        value
        for name, value in request.scope.get("headers", [])
        if isinstance(name, bytes) and name.lower() == b"content-length"
    ]
    if len(declared_values) > 1:
        # 重复声明的取值顺序跨代理不稳定，必须在读取正文前拒绝。
        raise ApiError("prompt_template.request_invalid")
    declared_size: int | None = None
    if declared_values:
        try:
            declared = declared_values[0].decode("ascii", errors="strict")
        # 非 ASCII 或非法长度不信任框架/代理兜底，统一失败且不回显原始 Header。
        except (AttributeError, UnicodeDecodeError):
            raise ApiError("prompt_template.request_invalid") from None
        # 只接受无符号规范十进制；空白、正号和前导零可能在多层组件中产生歧义。
        if (
            not declared
            or any(character not in "0123456789" for character in declared)
            or (len(declared) > 1 and declared.startswith("0"))
        ):
            # 非规范表示不能被不同组件各自宽松解释为同一长度。
            raise ApiError("prompt_template.request_invalid")
        declared_size = int(declared, 10)
        # 声明长度和实际流量分别限制，超限请求在分配正文前失败关闭。
        if declared_size > maximum:
            raise ApiError("prompt_template.request_invalid")
    body = bytearray()
    try:
        async for chunk in request.stream():
            # 自定义 ASGI transport 若违反 bytes 合同也固定失败，不能把任意对象异常传播出去。
            if type(chunk) is not bytes or len(body) + len(chunk) > maximum:
                raise ApiError("prompt_template.request_invalid")
            body.extend(chunk)
    # 已完成安全映射的本域错误保持原码；不能再折叠后丢失确定诊断。
    except ApiError:
        # 重新抛出时不附加流块或 Header，保留固定公共错误对象。
        raise
    # 客户端断流或 ASGI receive 异常可能携带传输细节，模板域只传播固定公共错误。
    except Exception:
        raise ApiError("prompt_template.request_invalid") from None
    # 声明长度与实际流必须完全一致；缺失声明仍依赖上方逐块硬上限。
    if declared_size is not None and declared_size != len(body):
        raise ApiError("prompt_template.request_invalid")
    return bytes(body)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """将 JSON pairs 转为对象，并在任意层检测到重复键时立即失败关闭。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        # 任意层重复键都具有实现相关语义，拒绝时不保留或回显敏感键名。
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _no_store(response: Response) -> None:
    """统一禁止代理或浏览器持久缓存模板 metadata 与授权 definition。"""

    response.headers["Cache-Control"] = "no-store"


def _request_id(request: Request) -> str:
    """读取 request middleware 生成的稳定链路 ID，不信任业务正文覆盖。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取 trace middleware 生成的当前链路 ID，统一进入响应与不可变事实。"""

    return getattr(request.state, "trace_id", "")
