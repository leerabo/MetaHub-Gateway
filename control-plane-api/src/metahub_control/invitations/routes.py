"""提供 PRD-060 邀请创建与公开接受 HTTP 入口。

创建路由先完成后端 ``user.manage`` 授权，再读取严格有界正文，避免匿名请求利用 JSON
差异探测管理合同。接受路由不要求 Session，但一次性 Token 是唯一鉴权凭据。
"""

from __future__ import annotations

import json
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_current_admin

from .models import (
    AcceptInvitationRequest,
    CreateInvitationRequest,
    InvitationAcceptResponse,
    InvitationCreateResponse,
    InvitationPreviewRequest,
    InvitationPreviewResponse,
)
from .service import accept_invitation, create_invitation, preview_invitation

router = APIRouter(prefix="/invitations", tags=["invitations"])

# 邀请正文只包含三个短字段；独立字节/深度/节点上限阻断无界解析和代理歧义。
MAX_INVITATION_BODY_BYTES = 4 * 1024
MAX_JSON_DEPTH = 3
MAX_JSON_NODES = 16
MAX_CONTENT_LENGTH_DIGITS = 5
CREATE_FIELDS = frozenset({"email", "role", "expires_in_hours"})
CREATE_REQUIRED_FIELDS = frozenset({"email", "role"})
ACCEPT_FIELDS = frozenset({"display_name", "password"})
ModelT = TypeVar("ModelT", bound=BaseModel)


class _DuplicateJsonKey(ValueError):
    """标记任意层级重复 JSON key，异常对象不保存原始正文。"""


class _RejectedJsonNumber(ValueError):
    """标记邀请正文不支持的浮点、指数或非有限数字。"""


@router.post("", response_model=InvitationCreateResponse, status_code=status.HTTP_201_CREATED)
async def post_invitation(
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_admin)],
) -> InvitationCreateResponse:
    """为当前认证租户创建一次性邀请。

    认证依赖在函数体执行前完成；正文不能携带 tenant_id 或 actor，服务层还会复核
    当前租户 active role、重复用户和 pending 邀请。
    """

    payload = await _read_payload(
        request,
        model=CreateInvitationRequest,
        allowed_fields=CREATE_FIELDS,
        required_fields=CREATE_REQUIRED_FIELDS,
        error_code="invitation.create_invalid",
    )
    return create_invitation(
        request.app.state.config,
        context,
        payload,
        request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/preview", response_model=InvitationPreviewResponse)
async def post_invitation_preview(
    request: Request, response: Response,
) -> InvitationPreviewResponse:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Referrer-Policy"] = "no-referrer"
    payload = await _read_payload(
        request, model=InvitationPreviewRequest,
        allowed_fields=frozenset({"token"}), required_fields=frozenset({"token"}),
        error_code="invitation.not_available",
    )
    return preview_invitation(request.app.state.config, payload.token)


@router.post(
    "/{token}/accept",
    response_model=InvitationAcceptResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_invitation_accept(token: str, request: Request) -> InvitationAcceptResponse:
    """使用一次性 Token 接受邀请并创建用户。

    该入口在用户存在前调用，因此不能要求 Session，也不能读取已有浏览器 Bearer；
    tenant、email 和 role 均由 Token 哈希命中的 Invitation 恢复。
    """

    payload = await _read_payload(
        request,
        model=AcceptInvitationRequest,
        allowed_fields=ACCEPT_FIELDS,
        required_fields=ACCEPT_FIELDS,
        error_code="invitation.accept_invalid",
    )
    return accept_invitation(
        request.app.state.config,
        token,
        payload,
        # 公开 Token 可被攻击者复制进 User-Agent；该入口固定不把此 Header 写入审计库。
        request_meta=build_audit_request_meta(
            request,
            sensitive_values=(token,),
            include_user_agent=False,
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


# 邀请正文可能包含密码或邮箱，必须在统一异常层之前完成有界、无回显的严格解析。
async def _read_payload(
    request: Request,
    *,
    model: type[ModelT],
    allowed_fields: frozenset[str],
    required_fields: frozenset[str],
    error_code: str,
) -> ModelT:
    """有界读取唯一 UTF-8 JSON，并拒绝重复键、额外字段和宽松类型转换。"""

    _require_json_content_type(request, error_code)
    declared_length = _content_length(request, error_code)
    if declared_length is not None and (
        declared_length < 1 or declared_length > MAX_INVITATION_BODY_BYTES
    ):
        # 声明长度越界时必须在分配正文缓冲区前失败。
        raise ApiError(error_code)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_INVITATION_BODY_BYTES:
            # 实际字节数独立受限，不能只相信 Content-Length。
            raise ApiError(error_code)
        body.extend(chunk)
    # 空正文或声明长度与实际流不一致都按领域 wire 错误处理，不能交给宽松 JSON 解析。
    if not body or (declared_length is not None and declared_length != len(body)):
        raise ApiError(error_code)
    try:
        parsed = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _RejectedJsonNumber,
        RecursionError,
        ValueError,
    ):
        # JSON 异常可能携带密码或邮箱片段，公开响应只能保留固定业务码。
        raise ApiError(error_code) from None
    parsed_fields = set(parsed) if type(parsed) is dict else set()
    if (
        type(parsed) is not dict
        or not required_fields <= parsed_fields
        or not parsed_fields <= allowed_fields
    ):
        # 缺失或额外字段都没有唯一业务含义，尤其不能静默接受 tenant/email/role 注入。
        raise ApiError(error_code)
    _validate_json_shape(parsed, error_code)
    try:
        return model.model_validate(parsed, strict=True)
    # Pydantic 异常保存原始 input，必须在此边界折叠为固定错误码。
    except ValidationError:
        # Pydantic 错误保存原始 input，禁止把异常文本链接到公开错误。
        raise ApiError(error_code) from None


# Content-Type 是代理与应用共同解释正文的边界，重复或模糊声明必须失败关闭。
def _require_json_content_type(request: Request, error_code: str) -> None:
    """要求唯一 application/json 与可选 UTF-8 charset。

    参数：原始 FastAPI 请求和调用方领域错误码；返回：合法时无返回值。
    约束：不选择重复 Header，不猜测媒体类型或编码，失败不回显正文。
    """

    values = _raw_header_values(request, b"content-type")
    # 缺失或重复媒体类型会产生解析歧义，统一拒绝而不猜测第一个值。
    if len(values) != 1:
        raise ApiError(error_code)
    try:
        parts = [part.strip() for part in values[0].decode("ascii", errors="strict").split(";")]
    # 非 ASCII 参数可能制造代理和应用的媒体类型解释差异。
    except UnicodeDecodeError:
        raise ApiError(error_code) from None
    # 只接受唯一 application/json，禁止 text/json 等宽松别名。
    if not parts or parts[0].lower() != "application/json":
        raise ApiError(error_code)
    seen_charset = False
    for parameter in parts[1:]:
        if not parameter or "=" not in parameter:
            # 无名或无值参数无法安全判断 charset，不能静默忽略。
            raise ApiError(error_code)
        name, value = (part.strip() for part in parameter.split("=", 1))
        if name.lower() != "charset" or seen_charset or value.lower() not in {
            "utf-8",
            '"utf-8"',
        }:
            # 只允许一次 UTF-8 charset，防止重复参数覆盖和编码差异。
            raise ApiError(error_code)
        seen_charset = True


def _content_length(request: Request, error_code: str) -> int | None:
    """解析唯一规范 Content-Length，缺失时仍由流式字节上限保护。"""

    values = _raw_header_values(request, b"content-length")
    if not values:
        return None
    # 多个 Content-Length 可能触发请求走私，不能合并或选择其中一个。
    if len(values) != 1:
        raise ApiError(error_code)
    try:
        text = values[0].decode("ascii", errors="strict")
    # 长度 Header 必须是规范 ASCII 十进制，异常字节不能宽松替换。
    except UnicodeDecodeError:
        raise ApiError(error_code) from None
    if (
        not text
        or len(text) > MAX_CONTENT_LENGTH_DIGITS
        or any(character < "0" or character > "9" for character in text)
        or (len(text) > 1 and text.startswith("0"))
    ):
        # 拒绝前导零、符号和超长数字，确保代理与应用使用同一长度语义。
        raise ApiError(error_code)
    return int(text)


def _raw_header_values(request: Request, name: bytes) -> list[bytes]:
    """从 ASGI 原始 Header 列表读取全部同名值，保留重复项供拒绝。"""

    return [
        value
        for raw_name, value in request.scope.get("headers", [])
        if raw_name.lower() == name
    ]


# JSON object 通过 pairs 构造，才能在字典覆盖发生前发现身份字段重复。
def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON object，并拒绝任意层级重复 key。

    参数：json.loads 按原始顺序提供的键值对；返回：无重复键的字典。
    约束：必须在字典覆盖发生前拒绝 tenant、email、role 或 password 重复定义。
    """

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey()
        result[key] = value
    return result


def _reject_number(_: object) -> object:
    """拒绝浮点、指数和非有限数字，过期时长只接受严格整数。"""

    raise _RejectedJsonNumber()


def _validate_json_shape(value: object, error_code: str) -> None:
    """限制 JSON 深度和节点数，防止小字节递归对象放大解析成本。"""

    nodes = 0

    # 递归访问只计数结构节点，不保存或格式化可能包含密码的具体值。
    def visit(current: object, depth: int) -> None:
        """累计当前 JSON 子树的节点和深度，越界时抛出固定领域错误。"""

        nonlocal nodes
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            # 小字节深层对象仍可能放大解析成本，超过任一上限立即终止。
            raise ApiError(error_code)
        if isinstance(current, dict):
            for child in current.values():
                visit(child, depth + 1)
        elif isinstance(current, list):
            for child in current:
                visit(child, depth + 1)

    visit(value, 1)


# 链路 ID 已由共享中间件完成 Token 片段碰撞替换，路由层只读取可信结果。
def _request_id(request: Request) -> str:
    """读取统一 middleware 已生成的 request_id。

    参数：已通过共享链路中间件的请求；返回：脱敏后的 request_id 字符串。
    约束：不得重新读取可能与 Invitation Token 碰撞的原始 Header。
    """

    return getattr(request.state, "request_id", "")


# trace_id 与 request_id 使用同一共享脱敏边界，禁止重新读取原始 Header。
def _trace_id(request: Request) -> str:
    """读取统一 middleware 已生成的 trace_id。

    参数：已通过共享链路中间件的请求；返回：脱敏后的 trace_id 字符串。
    约束：只消费 request.state，禁止把原始 path Token 或 Header 片段回显。
    """

    return getattr(request.state, "trace_id", "")
