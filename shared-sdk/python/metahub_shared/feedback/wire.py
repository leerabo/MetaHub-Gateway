"""PRD-049 双入口共用的 HTTP wire 解析边界。

该模块不依赖 FastAPI/Starlette。路由只负责先完成认证和权限校验，再把受限读取的 bytes、
Content-Type 与 ASGI raw headers 交给这里；这样 Control 和 Gateway 不会复制安全规则。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from email.message import Message

from metahub_shared.runtime.errors import ApiError

from .contracts import FeedbackIntent
from .validation import normalize_feedback_payload, normalize_safe_identifier

# 反馈正文和 JSON 嵌套深度限制用于保护双服务资源，并阻止自由结构扩大敏感信息面。
MAX_FEEDBACK_BODY_BYTES = 4096
MAX_FEEDBACK_JSON_DEPTH = 3


class _DuplicateJsonKey(ValueError):
    """标记严格 JSON 对象中的重复键，外部只会看到统一 input_invalid。"""


def parse_feedback_json_bytes(
    body: bytes,
    *,
    content_type: str | None,
    content_length: str | None,
) -> FeedbackIntent:
    """解析受限 Feedback JSON bytes 并返回规范化意图。

    参数：路由在认证/RBAC 后读取的不超过 4096 bytes 正文及原始媒体头。
    返回：双入口共用的 ``FeedbackIntent``。
    约束：只接受 application/json 与可选 utf-8 charset，拒绝重复键、NaN、控制字符、
    未知字段和深度超过三层的结构。
    """

    _validate_json_content_type(content_type)
    _validate_content_length(content_length)
    if type(body) is not bytes or len(body) == 0 or len(body) > MAX_FEEDBACK_BODY_BYTES:
        # 路由应提前硬截断；shared 再次检查可防止内部调用绕过资源上限。
        raise ApiError("feedback.input_invalid")
    try:
        decoded = body.decode("utf-8", errors="strict")
        payload = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    # 解码、语法、重复键和 NaN/Infinity 全部折叠为同一安全错误，不回显正文或位置。
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError) as exc:
        raise ApiError("feedback.input_invalid") from exc
    _validate_json_tree(payload, depth=1)
    return normalize_feedback_payload(payload)


def resolve_feedback_operation_request_id(
    raw_headers: Sequence[tuple[bytes, bytes]],
    state_request_id: str,
) -> str:
    """从 ASGI raw headers 恢复未被中间件改写的反馈幂等 ID。

    没有外部 ``X-Request-ID`` 时使用中间件生成值；恰好一个时要求原始 ASCII 值与
    state 完全一致。重复、非法字符、替换或截断均拒绝，避免两个外部 ID 折叠冲突。
    """

    normalized_state = normalize_safe_identifier(
        state_request_id, error_code="feedback.input_invalid"
    )
    request_id_values = [
        value
        for name, value in raw_headers
        if isinstance(name, bytes) and name.lower() == b"x-request-id"
    ]
    # 未提供外部头时只能采用中间件新生成且已通过安全字符校验的链路 ID。
    if not request_id_values:
        return normalized_state
    # 重复 X-Request-ID 的代理合并语义不确定，不能任选一项作为不可变幂等键。
    if len(request_id_values) != 1:
        # 多个同名头在代理层可能被拼接或择一，中间件 state 无法证明原始幂等身份。
        raise ApiError("feedback.input_invalid")
    raw_value = request_id_values[0]
    try:
        external_request_id = raw_value.decode("ascii", errors="strict")
    # 非 ASCII header 不能安全进入 SQLite 索引或结构化日志。
    except (AttributeError, UnicodeDecodeError) as exc:
        raise ApiError("feedback.input_invalid") from exc
    normalized_external = normalize_safe_identifier(
        external_request_id, error_code="feedback.input_invalid"
    )
    if normalized_external != normalized_state:
        # state 与 raw 不同意味着中间件做过替换/截断，不能把结果作为幂等键。
        raise ApiError("feedback.input_invalid")
    return normalized_external


def _validate_json_content_type(content_type: str | None) -> None:
    """校验 Feedback 媒体类型，仅接受 JSON 与唯一 UTF-8 charset。

    参数：未经框架规范化的 Content-Type；返回：合法时无返回值。
    约束：拒绝 ``application/*+json``、未知参数和重复 charset，避免双入口解析漂移。
    """

    if type(content_type) is not str or not content_type:
        # 缺少媒体类型时禁止猜测 JSON，保持双入口解析合同确定。
        raise ApiError("feedback.input_invalid")
    message = Message()
    message["content-type"] = content_type
    if message.get_content_type().lower() != "application/json":
        # application/*+json 和其他媒体类型均不在本期精确合同内。
        raise ApiError("feedback.input_invalid")
    params = message.get_params(header="content-type", unquote=True) or []
    extra = [(str(name).lower(), value) for name, value in params[1:]]
    if len(extra) > 1 or any(name != "charset" for name, _value in extra):
        # 未知或重复参数会让代理与应用产生不同解析，应在进入 JSON 解码前拒绝。
        raise ApiError("feedback.input_invalid")
    if extra and str(extra[0][1]).lower() != "utf-8":
        # 本接口只采用 UTF-8，拒绝兼容编码以避免相同 bytes 得到不同标签/ID。
        raise ApiError("feedback.input_invalid")


def _validate_content_length(content_length: str | None) -> None:
    """校验可选 Content-Length 的十进制规范形式和反馈正文上限。"""

    if content_length is None:
        return
    if (
        type(content_length) is not str
        or not content_length
        or not content_length.isascii()
        or not content_length.isdecimal()
    ):
        # 不接受空白、符号或非 ASCII 数字，避免各框架对长度产生不同解释。
        raise ApiError("feedback.input_invalid")
    length = int(content_length)
    if length <= 0 or length > MAX_FEEDBACK_BODY_BYTES:
        # 先用声明长度拒绝明显超限请求，真实 bytes 长度仍由主函数二次验证。
        raise ApiError("feedback.input_invalid")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON 对象并在任何层级拒绝重复键。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            # last-write-wins 会让签名、日志和客户端看到不同意图，因此整体拒绝。
            raise _DuplicateJsonKey("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    """拒绝 JSON 标准之外的 NaN 和 Infinity 常量。

    Python 解码器会把这些 token 交给本回调；函数始终抛出内部 ValueError，外层再折叠为
    ``feedback.input_invalid``，且不会把正文 token 或解析位置回显给客户端。
    """

    # Python 默认解码器会接受非标准浮点常量；反馈合同必须保持跨语言一致。
    raise ValueError(f"invalid JSON constant category: {type(value).__name__}")


def _validate_json_tree(value: object, *, depth: int) -> None:
    """递归限制 JSON 深度并拒绝解码后字符串中的控制字符。"""

    if depth > MAX_FEEDBACK_JSON_DEPTH:
        # 固定四字段合同无需更深结构，超深输入只会增加解析资源和隐蔽数据面。
        raise ApiError("feedback.input_invalid")
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_json_string(key)
            _validate_json_tree(child, depth=depth + 1)
        return
    if isinstance(value, list):
        for child in value:
            _validate_json_tree(child, depth=depth + 1)
        return
    if isinstance(value, str):
        _validate_json_string(value)


def _validate_json_string(value: str) -> None:
    """拒绝转义后出现的 C0/DEL 控制字符，防止日志和事件注入。"""

    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        # JSON 的 \u0000 等转义可通过语法解析，仍不能进入链路 ID 或标签处理。
        raise ApiError("feedback.input_invalid")
