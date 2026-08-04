"""PRD-070 高危会话报表与处置路由。

认证与独立 RBAC dependency 必须先于 query、Header 和正文解析。写入口手工执行有界
raw JSON 解析，防止重复键、重复 Header 或框架宽松转换改变治理意图。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from pydantic import ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_session_risk_manager,
    require_session_risk_reader,
)

from .models import (
    SessionRiskActionRequest,
    SessionRiskActionResponse,
    SessionRiskListResponse,
)
from .service import (
    apply_session_risk_action,
    dispatch_session_risk_effects_safely,
    ensure_session_risk_store_available,
    get_session_risk_store,
    list_session_risks,
    synchronize_session_risk_store,
)

router = APIRouter(tags=["session-risks"])

# 公开 wire 白名单集中定义，未知字段不能被旧/新节点静默忽略。
ALLOWED_LIST_QUERY_FIELDS = frozenset(
    {"status", "scope_type", "virtual_key_id", "from_time", "to_time", "limit", "cursor"}
)
ACTION_BODY_FIELDS = frozenset({"action", "reason_code", "expected_revision"})
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
MAX_ACTION_BODY_BYTES = 4 * 1024
MAX_JSON_DEPTH = 4
MAX_JSON_NODES = 16
ACTION_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": SessionRiskActionRequest.model_json_schema()}
        },
    },
    "parameters": [
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "minLength": 8, "maxLength": 128},
        }
    ],
}


class _DuplicateJsonKey(ValueError):
    """标记任意层级重复 JSON key，避免代理与应用出现 first/last 语义分裂。"""


class _RejectedJsonNumber(ValueError):
    """标记浮点、指数和非有限数；revision 只接受 JSON integer。"""


@router.get("/security/session-risks", response_model=SessionRiskListResponse)
async def get_session_risks_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_session_risk_reader)],
) -> SessionRiskListResponse:
    """读取当前 Session 所属租户的高危会话报表。

    参数：可信请求、响应头容器和实时权限上下文。返回：tenant-scoped keyset 页。
    关键约束：权限通过后才解析查询；客户端不能提交 tenant、排序字段或正文开关。
    """

    query = _read_query(request)
    response.headers["Cache-Control"] = "no-store"
    # 租户身份只取自实时认证上下文，query 中不存在可覆盖 tenant_id 的入口。
    return list_session_risks(
        request.app.state.config,
        context,
        status=query.get("status"),
        scope_type=query.get("scope_type"),
        virtual_key_id=query.get("virtual_key_id"),
        from_time=query.get("from_time"),
        to_time=query.get("to_time"),
        limit=query.get("limit"),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/security/session-risks/{session_risk_score_id}/action",
    response_model=SessionRiskActionResponse,
    openapi_extra=ACTION_OPENAPI,
)
async def post_session_risk_action_route(
    session_risk_score_id: str,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    context: Annotated[AuthContext, Depends(require_session_risk_manager)],
) -> SessionRiskActionResponse:
    """使用双权限、CAS 和幂等合同处置一条当前租户会话风险。

    参数：不透明 score ID、请求、响应、提交后任务和实时授权上下文。返回：更新后的
    风险项与不可变 action。关键约束：dependency 同时要求 read/manage，并在读正文前执行。
    """

    # 动作目标只由路径 score ID 决定，任何额外 query 都可能制造歧义，因此统一失败关闭。
    if request.query_params.multi_items():
        raise ApiError("session_risk.payload_invalid")
    # 严格媒体类型必须在读取正文前验证，避免代理与应用采用不同 JSON 解码规则。
    if _read_content_type(request) != "application/json":
        raise ApiError("session_risk.payload_invalid")
    idempotency_key = _read_idempotency_key(request)
    payload = await _read_action_payload(request)
    response.headers["Cache-Control"] = "no-store"
    # Redis 先做轻量可用性预检，避免在依赖已明确不可达时提交新的数据库动作；提交后仍须
    # 执行精确 CAS 对账，因为 ping 与写入之间的竞态不能被当作成功。
    store = get_session_risk_store(request.app.state.config, request.app.state)
    await ensure_session_risk_store_available(store)
    result, effects, sync_intent = apply_session_risk_action(
        request.app.state.config,
        context,
        session_risk_score_id,
        payload,
        idempotency_key=idempotency_key,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    try:
        await synchronize_session_risk_store(store, sync_intent)
    except Exception:
        # 数据库事实已经提交。首次动作的 Audit/Webhook 仍需尝试派发，再把固定 Store 错误
        # 交给统一异常层；幂等重试只会继续同步，不会重复写动作。
        if effects is not None:
            dispatch_session_risk_effects_safely(
                request.app.state.config,
                effects,
                request_id=_request_id(request),
                trace_id=_trace_id(request),
            )
        raise
    if effects is not None:
        # 外部 Webhook 和 Redis 同步只接收已提交的安全事实，不能持有原始幂等键或正文。
        background_tasks.add_task(
            dispatch_session_risk_effects_safely,
            request.app.state.config,
            effects,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    return result


def _read_query(request: Request) -> dict[str, str]:
    """读取一次性白名单 query，拒绝重复、未知字段和控制字符。"""

    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        # 未知或重复筛选会改变 cursor 绑定语义，禁止由框架静默保留最后一个值。
        if key not in ALLOWED_LIST_QUERY_FIELDS or key in values:
            raise ApiError("session_risk.query_invalid")
        # 控制字符可能污染日志、签名与页面展示，必须在进入服务层前拒绝。
        if _contains_control(key) or _contains_control(value):
            raise ApiError("session_risk.query_invalid")
        values[key] = value
    return values


def _read_content_type(request: Request) -> str:
    """要求唯一、无歧义的 UTF-8 application/json Content-Type。

    重复 Header、非 ASCII 参数或未知 charset 都可能使代理与应用看到不同正文，必须失败关闭。
    """

    values = _raw_header_values(request, b"content-type")
    # 重复或缺失媒体类型不能交给框架合并，否则攻击者可制造两套正文解释。
    if len(values) != 1:
        raise ApiError("session_risk.payload_invalid")
    try:
        raw = values[0].decode("ascii")
    # Header 合同只接受 ASCII；原始非法字节不得进入统一错误详情或日志。
    except UnicodeDecodeError as exc:
        raise ApiError("session_risk.payload_invalid") from exc
    parts = [part.strip() for part in raw.split(";")]
    # 仅接受精确 JSON 媒体类型，不能把后缀或空类型宽松解释成治理动作。
    if not parts or parts[0].lower() != "application/json":
        raise ApiError("session_risk.payload_invalid")
    seen_charset = False
    for parameter in parts[1:]:
        # 空参数或缺少等号会产生代理兼容差异，因此不做猜测性修复。
        if not parameter or "=" not in parameter:
            raise ApiError("session_risk.payload_invalid")
        name, value = (item.strip() for item in parameter.split("=", 1))
        # 只允许一个 UTF-8 charset，重复或未知参数一律拒绝，保持签名输入唯一。
        if name.lower() != "charset" or seen_charset or value.lower() not in {"utf-8", '"utf-8"'}:
            raise ApiError("session_risk.payload_invalid")
        seen_charset = True
    return "application/json"


def _read_idempotency_key(request: Request) -> str:
    """读取唯一 ASCII 幂等键；原值仅在本次调用内存中使用。"""

    values = _raw_header_values(request, b"idempotency-key")
    # 重复 Header 或逗号拼接会产生不同幂等身份，禁止由网关替调用方合并。
    if len(values) != 1 or b"," in values[0]:
        raise ApiError("session_risk.idempotency_invalid")
    try:
        value = values[0].decode("ascii")
    # 非 ASCII 原值既不参与摘要也不进入日志，统一映射为稳定幂等错误码。
    except UnicodeDecodeError as exc:
        raise ApiError("session_risk.idempotency_invalid") from exc
    # 固定字符集和长度降低解析歧义；数据库只持久化域分隔 HMAC，不保存这里的原值。
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise ApiError("session_risk.idempotency_invalid")
    return value


async def _read_action_payload(request: Request) -> SessionRiskActionRequest:
    """有界读取严格 UTF-8 JSON object，并拒绝重复键与宽松数字。"""

    expected_length = _content_length(request)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        # 流式累计在追加内存前执行上限，避免伪造或缺失 Content-Length 绕过保护。
        if size > MAX_ACTION_BODY_BYTES:
            raise ApiError("session_risk.payload_invalid")
        chunks.append(chunk)
    # 空正文和声明长度不一致都可能代表代理截断，不能提交成默认治理动作。
    if size == 0 or (expected_length is not None and expected_length != size):
        raise ApiError("session_risk.payload_invalid")
    try:
        parsed = json.loads(
            b"".join(chunks).decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _RejectedJsonNumber,
        RecursionError,
        ValueError,
    ) as exc:
        # 解码和结构异常统一收敛，不回显正文、重复键、非法数字或具体解析位置。
        raise ApiError("session_risk.payload_invalid") from exc
    # 动作正文必须是 exact-key object，tenant、actor 与目标字段均由服务端权威派生。
    if not isinstance(parsed, dict) or set(parsed) != ACTION_BODY_FIELDS:
        raise ApiError("session_risk.payload_invalid")
    _validate_json_tree(parsed)
    try:
        # Pydantic 只负责固定枚举和整数边界，验证后对象才允许进入事务服务。
        return SessionRiskActionRequest.model_validate(parsed)
    # 验证器细节可能携带客户端原值，统一转换为稳定错误码而不向响应泄漏。
    except (ValidationError, TypeError, ValueError) as exc:
        raise ApiError("session_risk.payload_invalid") from exc


def _content_length(request: Request) -> int | None:
    """校验唯一 canonical Content-Length，并在读取正文前执行字节上限。"""

    values = _raw_header_values(request, b"content-length")
    # 多个长度声明可能触发请求走私差异，不能依赖框架选择其中一个。
    if len(values) > 1:
        raise ApiError("session_risk.payload_invalid")
    if not values:
        return None
    try:
        raw = values[0].decode("ascii")
    # 长度字段只接受 ASCII 十进制，非法字节不得进入异常详情或日志。
    except UnicodeDecodeError as exc:
        raise ApiError("session_risk.payload_invalid") from exc
    # 前导零与非数字会制造非 canonical 表达，必须在正文读取前失败关闭。
    if not raw.isdigit() or (len(raw) > 1 and raw.startswith("0")):
        raise ApiError("session_risk.payload_invalid")
    value = int(raw)
    # 声明长度超限时不开始读取请求流，降低恶意大正文占用连接和内存的风险。
    if value > MAX_ACTION_BODY_BYTES:
        raise ApiError("session_risk.payload_invalid")
    return value


def _raw_header_values(request: Request, name: bytes) -> list[bytes]:
    """从 ASGI 原始头读取全部同名值，保留重复项供拒绝。"""

    return [value for key, value in request.scope.get("headers", []) if key.lower() == name]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """把 JSON pairs 转为对象，并拒绝任意层级重复键。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_number(value: str) -> object:
    """拒绝所有非整数 JSON 数字表示，且不在错误中回显原值。"""

    raise _RejectedJsonNumber("non-integer JSON number")


def _validate_json_tree(value: object) -> None:
    """限制解析后树深度、节点数和控制字符，避免异常正文放大资源。"""

    nodes = 0

    def visit(item: object, depth: int) -> None:
        """递归检查单个节点；所有边界在进入子节点前生效。"""

        nonlocal nodes
        nodes += 1
        # 深度或节点数超限说明正文可能用于资源放大，不能继续递归或部分接受。
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ApiError("session_risk.payload_invalid")
        if isinstance(item, dict):
            for key, child in item.items():
                # JSON key 中的控制字符会污染日志和签名输入，因此在遍历子值前拒绝。
                if _contains_control(key):
                    raise ApiError("session_risk.payload_invalid")
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        # 字符串值同样禁止控制字符，避免原因码或未来字段形成日志注入载体。
        elif isinstance(item, str) and _contains_control(item):
            raise ApiError("session_risk.payload_invalid")

    visit(value, 1)


def _contains_control(value: str) -> bool:
    """判断文本是否含会污染日志、签名或 UI 的 Unicode 控制字符。"""

    return any(unicodedata.category(character).startswith("C") for character in value)


def _request_id(request: Request) -> str:
    """读取统一 middleware 生成的内部 request_id。

    只信任服务端状态中的链路标识，不从 query 或动作正文接受调用方覆盖值。
    """

    return str(getattr(request.state, "request_id", ""))


def _trace_id(request: Request) -> str:
    """读取统一 middleware 生成的内部 trace_id。

    该标识只用于服务端审计和诊断关联，不允许客户端正文改变事务归属。
    """

    return str(getattr(request.state, "trace_id", ""))


__all__ = ["router"]
