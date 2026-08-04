"""PRD-068 行业/场景策略模板库的两个固定 HTTP 端点。

认证和基础 permission dependency 必须先于 query、header 与正文解析执行。GET 只接受四个冻结
query 字段；POST 使用有界流读取、严格 UTF-8、任意层重复键拒绝和 exact-set DTO，并把框架
校验详情折叠为统一领域错误，防止绑定值或注入字段出现在响应和日志。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_policy_template_applier,
    require_policy_template_reader,
)

from .models import (
    ApplyPolicyTemplateRequest,
    PolicyTemplateApplyResponse,
    PolicyTemplateBindingsRequest,
    PolicyTemplateListResponse,
)
from .service import apply_policy_template, list_policy_templates

# 设计冻结的管理面正文、深度和节点上限；上限只控制 wire，领域字段仍由 DTO 和 service 收紧。
MAX_POLICY_TEMPLATE_BODY_BYTES = 32 * 1024
MAX_POLICY_TEMPLATE_JSON_DEPTH = 8
MAX_POLICY_TEMPLATE_JSON_NODES = 128
_QUERY_FIELDS = frozenset({"template_id", "project_id", "model_alias_id", "threshold_cny"})


class _DuplicateJsonKey(ValueError):
    """表示任意 JSON 对象层出现重复键；异常不得保留具体键名。"""


class PolicyTemplateRoute(APIRoute):
    """把本域 FastAPI 校验详情固定映射为安全的模板错误码。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """返回保留认证和业务错误优先级的路由处理器。"""

        original_handler = super().get_route_handler()

        async def policy_template_handler(request: Request) -> Response:
            """执行原路由，禁止框架 loc/input 详情穿透模板安全边界。"""

            try:
                return await original_handler(request)
            # 路径、Header 或 dependency 形态校验可能携带原始输入，统一折叠且切断异常链。
            except RequestValidationError:
                raise ApiError("policy_template.payload_invalid") from None

        return policy_template_handler


router = APIRouter(
    prefix="/policy-templates",
    tags=["policy-templates"],
    route_class=PolicyTemplateRoute,
)


@router.get("", response_model=PolicyTemplateListResponse)
async def get_policy_templates(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_policy_template_reader)],
) -> PolicyTemplateListResponse:
    """返回固定五模板目录；指定模板和绑定时返回写锁前的精确影响预览。"""

    await _require_empty_body(request)
    template_id, bindings = _parse_query(request)
    result, head_etag, candidate_etag = list_policy_templates(
        request.app.state.config,
        context,
        template_id=template_id,
        bindings=bindings,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _set_no_store(response)
    # 目录响应没有单一 CAS 对象；只有精确预览才向 apply 交付两条强前置 Header。
    if head_etag is not None:
        response.headers["ETag"] = head_etag
    if candidate_etag is not None:
        response.headers["X-Policy-Candidate-ETag"] = candidate_etag
    return result


@router.post(
    "/{policy_template_id}/apply",
    response_model=PolicyTemplateApplyResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": ApplyPolicyTemplateRequest.model_json_schema(),
                }
            },
        }
    },
)
async def post_policy_template_apply(
    policy_template_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_policy_template_applier)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    candidate_match: Annotated[
        str | None,
        Header(alias="If-Policy-Candidate-Match"),
    ] = None,
) -> PolicyTemplateApplyResponse:
    """在单一写事务内复核权限、预览、依赖和双 CAS，再创建并发布模板 Policy。"""

    _reject_all_query(request)
    payload = await _parse_json_body(request, ApplyPolicyTemplateRequest)
    # 缺失和形态非法必须分层：缺失属于 428，形态由 service 固定为 422，事实漂移才是 409。
    if if_match is None or candidate_match is None:
        raise ApiError("policy_template.precondition_required")
    result = apply_policy_template(
        request.app.state.config,
        context,
        policy_template_id,
        payload,
        if_match=if_match,
        candidate_match=candidate_match,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.status_code = (
        status.HTTP_200_OK if result.idempotent_replay else status.HTTP_201_CREATED
    )
    response.headers["ETag"] = f'"{result.policy_snapshot.snapshot_version}"'
    _set_no_store(response)
    return result


def _parse_query(request: Request) -> tuple[str | None, PolicyTemplateBindingsRequest | None]:
    """严格解析 GET exact-set query，并拒绝重复、未知或无模板却夹带绑定的请求。"""

    pairs = request.query_params.multi_items()
    if any(key not in _QUERY_FIELDS for key, _ in pairs):
        # tenant、actor、Policy 或任意未知筛选都不能进入目录查询。
        raise ApiError("policy_template.query_invalid")
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        # 重复 query 在代理和框架间可能采用不同取值，必须拒绝歧义而不是选择首项或末项。
        raise ApiError("policy_template.query_invalid")
    values = dict(pairs)
    template_id = values.pop("template_id", None)
    if template_id is None:
        # 目录模式不能夹带项目或模型绑定，否则同一 query 会同时具备目录与预览两种解释。
        if values:
            raise ApiError("policy_template.query_invalid")
        return None, None
    try:
        # 绑定联合体要求 project_id，模板专属 exact-set 随后由 planner 校验。
        bindings = PolicyTemplateBindingsRequest.model_validate(values)
    except ValidationError:
        raise ApiError("policy_template.query_invalid") from None
    return template_id, bindings


def _reject_all_query(request: Request) -> None:
    """拒绝 apply 路由的全部 query，关闭隐藏 owner 或 CAS 覆盖注入。"""

    # apply 的模板、绑定和并发条件都有唯一载体，任何 query 都可能形成优先级不明的旁路输入。
    if request.query_params.multi_items():
        raise ApiError("policy_template.payload_invalid")


async def _parse_json_body[RequestPayloadT: BaseModel](
    request: Request,
    model_type: type[RequestPayloadT],
) -> RequestPayloadT:
    """在权限依赖通过后有界解析 JSON，并统一折叠全部敏感校验详情。"""

    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    # 只接受显式 JSON，禁止表单或文本正文依赖代理内容嗅探后产生不同字段解释。
    if media_type != "application/json":
        raise ApiError("policy_template.media_type_unsupported")
    body = await _read_bounded_body(request, maximum=MAX_POLICY_TEMPLATE_BODY_BYTES)
    # apply 没有默认请求模型；空正文不得被解释为使用模板默认值的隐式写操作。
    if not body:
        raise ApiError("policy_template.payload_invalid")
    try:
        # object hook 拒绝任意层重复键，parse_constant 同时关闭 NaN/Infinity 等非标准 JSON 值。
        decoded = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        # DTO 契约以 JSON object 为根，数组或标量不能绕过字段 exact-set 校验。
        if type(decoded) is not dict:
            raise ValueError
        _require_bounded_json(decoded)
        return model_type.model_validate(decoded)
    # 解析异常可能保留绑定、阈值或 reason；禁止传播原异常文本或 Pydantic input。
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        TypeError,
        RecursionError,
    ):
        # 对外只返回稳定错误码，避免编码异常、重复键名或 Pydantic 原始输入进入响应链路。
        raise ApiError("policy_template.payload_invalid") from None


async def _require_empty_body(request: Request) -> None:
    """拒绝 GET 正文，避免代理和应用对目录请求产生不同解释。"""

    try:
        body = await _read_bounded_body(request, maximum=0)
    # GET 没有 payload 语义；Content-Length 非法或任何非空正文都属于 query wire
    # 歧义，不能泄漏 apply 专用的 413/422 payload 分类给不同代理路径。
    except ApiError:
        raise ApiError("policy_template.query_invalid") from None
    # 即使客户端未声明 Content-Length，实际读到正文也必须按目录查询歧义拒绝。
    if body:
        raise ApiError("policy_template.query_invalid")


async def _read_bounded_body(request: Request, *, maximum: int) -> bytes:
    """逐块读取有界正文并严格核对唯一规范 Content-Length。"""

    declared_values = [
        value
        for name, value in request.scope.get("headers", [])
        if isinstance(name, bytes) and name.lower() == b"content-length"
    ]
    # 多个 Content-Length 会让代理和 ASGI 服务采用不同正文边界，因此不能择一继续处理。
    if len(declared_values) > 1:
        raise ApiError("policy_template.payload_invalid")
    declared_size: int | None = None
    if declared_values:
        try:
            declared = declared_values[0].decode("ascii", errors="strict")
        # ASGI scope 仍按不可信输入处理；非字节值或非 ASCII 长度统一失败关闭。
        except (AttributeError, UnicodeDecodeError):
            raise ApiError("policy_template.payload_invalid") from None
        if (
            not declared
            or any(character not in "0123456789" for character in declared)
            or (len(declared) > 1 and declared.startswith("0"))
        ):
            # 仅接受无符号规范十进制，拒绝空值、符号和前导零造成的解析器差异。
            raise ApiError("policy_template.payload_invalid")
        declared_size = int(declared, 10)
        # 已声明超限时在进入流读取前终止，避免无意义地接收和分配整段正文。
        if declared_size > maximum:
            raise ApiError("policy_template.payload_too_large")
    body = bytearray()
    try:
        async for chunk in request.stream():
            # 自定义 ASGI transport 也必须交付原始字节，其他类型不能参与长度核算或 JSON 解码。
            if type(chunk) is not bytes:
                raise ApiError("policy_template.payload_invalid")
            # 以实际累计字节为最终上限，防止缺失或伪造 Content-Length 绕过声明检查。
            if len(body) + len(chunk) > maximum:
                raise ApiError("policy_template.payload_too_large")
            body.extend(chunk)
    # 保留本域主动产生的 413/422 分类，不能被后续通用异常转换覆盖。
    except ApiError:
        raise
    # 流读取器的底层异常不应暴露 transport 文本，统一映射为稳定的正文非法错误。
    except Exception:
        raise ApiError("policy_template.payload_invalid") from None
    # 声明长度与实际字节数不一致可能表示请求截断或走私，不能继续解析部分 JSON。
    if declared_size is not None and declared_size != len(body):
        raise ApiError("policy_template.payload_invalid")
    return bytes(body)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """构造 JSON object，并在任意层发现重复 key 时立即失败关闭。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        # first-wins 与 last-wins 会产生不同策略，任意层重复键都必须在对象构造时拒绝。
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _require_bounded_json(value: object) -> None:
    """在构造 DTO 前证明 JSON 深度和节点总数均在冻结上限内。"""

    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        # 深度与总节点任一超限都在 DTO 前终止，避免构造高成本嵌套对象消耗控制面资源。
        if nodes > MAX_POLICY_TEMPLATE_JSON_NODES or depth > MAX_POLICY_TEMPLATE_JSON_DEPTH:
            raise ValueError
        if type(current) is dict:
            stack.extend((item, depth + 1) for item in current.values())
        elif type(current) is list:
            stack.extend((item, depth + 1) for item in current)


def _set_no_store(response: Response) -> None:
    """禁止浏览器和代理缓存租户目录、绑定、影响或 CAS 元数据。"""

    response.headers["Cache-Control"] = "no-store"


def _request_id(request: Request) -> str:
    """读取共享中间件生成的 request_id，禁止从正文或 query 覆盖。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取共享中间件生成的 trace_id，供审计、事件与错误链路关联。"""

    return getattr(request.state, "trace_id", "")
