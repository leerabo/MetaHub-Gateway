"""PRD-077 只读管理员助手的有界精确 HTTP wire 与危险输入失败关闭边界。"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from fastapi import Request
from metahub_shared.runtime.errors import ApiError

# wire、问题字节/码点上限和会改变解析方向的 Header/Unicode 集合在此集中冻结；
# parser 只返回 NFKC 问题到当前调用栈，任何拒绝路径都不回显原始正文。
MAX_WIRE_BYTES = 16 * 1024
MAX_QUESTION_BYTES = 8 * 1024
MAX_QUESTION_CODEPOINTS = 2_000
_ENTITY_HEADERS = {
    b"content-encoding",
    b"transfer-encoding",
    b"content-range",
    b"idempotency-key",
}
_UNIQUE_HEADERS = {
    b"authorization",
    b"content-type",
    b"content-length",
    b"x-request-id",
    b"x-trace-id",
}
_BIDI_CONTROLS = {
    0x061C,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
}


class _DuplicateKey(ValueError):
    """JSON object 出现重复字段时使用的内部哨兵，异常文本不越过 wire 边界。"""


async def read_assistant_question(request: Request) -> str:
    """在认证与基础 RBAC 后读取单个有界、exact-schema JSON 正文。"""

    # endpoint 不接受查询参数，避免同一问题在 URL 与 JSON 中产生范围歧义或泄漏日志。
    if request.query_params.multi_items():
        raise ApiError("admin_assistant.request_invalid")
    content_length = _validate_headers(request)
    payload = bytearray()
    async for chunk in request.stream():
        if chunk:
            payload.extend(chunk)
            # 流式累计一旦超过 wire 上限立即停止，不能依赖伪造 Content-Length。
            if len(payload) > MAX_WIRE_BYTES:
                raise ApiError("admin_assistant.request_invalid")
    # 实际字节数必须与规范 Content-Length 一致，截断或追加正文都失败关闭。
    if len(payload) != content_length:
        raise ApiError("admin_assistant.request_invalid")
    try:
        decoded = bytes(payload).decode("utf-8", errors="strict")
        body = json.loads(decoded, object_pairs_hook=_exact_object)
    # 编码、JSON 或重复键异常统一转成固定请求错误，不回显原始问题字节。
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey):
        raise ApiError("admin_assistant.request_invalid") from None
    # 请求对象只能有 question 一个字段，tenant、权限与来源不得由客户端提交。
    if type(body) is not dict or set(body) != {"question"}:
        raise ApiError("admin_assistant.request_invalid")
    question = body["question"]
    # question 必须是 exact str，禁止其他 JSON 类型参与宽松字符串转换。
    if type(question) is not str:
        raise ApiError("admin_assistant.request_invalid")
    _validate_question(question)
    return unicodedata.normalize("NFKC", question)


def _validate_headers(request: Request) -> int:
    """从 raw ASGI Header 校验唯一身份/实体语义，并返回 canonical Content-Length。"""

    grouped: dict[bytes, list[bytes]] = {}
    for key, value in request.scope.get("headers", ()):
        grouped.setdefault(key.lower(), []).append(value)
    # 压缩、分块、范围或幂等 Header 会改变实体/attempt 语义，本 endpoint 全部拒绝。
    if any(grouped.get(name) for name in _ENTITY_HEADERS):
        raise ApiError("admin_assistant.request_invalid")
    # 框架可能折叠重复安全 Header；必须在 raw bytes 层确认每个值唯一。
    if any(len(grouped.get(name, ())) > 1 for name in _UNIQUE_HEADERS):
        raise ApiError("admin_assistant.request_invalid")
    for name in (b"authorization", b"x-request-id", b"x-trace-id"):
        values = grouped.get(name, ())
        # 逗号折叠可能代表多个安全值，绝不能任选身份或链路标识继续处理。
        if values and b"," in values[0]:
            raise ApiError("admin_assistant.request_invalid")
    media_values = grouped.get(b"content-type", ())
    # media type 精确限定为 UTF-8 JSON，不接受参数或兼容类型扩大解码面。
    if len(media_values) != 1 or media_values[0].lower() != b"application/json":
        raise ApiError("admin_assistant.request_invalid")
    lengths = grouped.get(b"content-length", ())
    # Content-Length 必须恰好出现一次，避免代理与应用对请求边界解释不同。
    if len(lengths) != 1:
        raise ApiError("admin_assistant.request_invalid")
    raw_length = lengths[0]
    if (
        not raw_length
        or not raw_length.isdigit()
        or (len(raw_length) > 1 and raw_length.startswith(b"0"))
    ):
        # 长度只接受无前导零的十进制规范形式，拒绝符号和宽松解析。
        raise ApiError("admin_assistant.request_invalid")
    length = int(raw_length)
    # 空实体或超过总 wire 预算的实体都不得进入流式正文读取。
    if length <= 0 or length > MAX_WIRE_BYTES:
        raise ApiError("admin_assistant.request_invalid")
    return length


def _exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """把 JSON pairs 转成 exact dict，并在第一次重复 key 时无正文失败。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        # 重复字段不能按 first/last wins 解释，否则客户端与服务端可能授权不同问题。
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _validate_question(question: str) -> None:
    """校验问题码点、UTF-8 字节与危险 Unicode，正文只留在当前请求内存。"""

    # 码点与字节双上限分别阻止组合字符膨胀和多字节正文绕过 wire 预算。
    if not question or len(question) > MAX_QUESTION_CODEPOINTS:
        raise ApiError("admin_assistant.request_invalid")
    # UTF-8 编码后再限制字节数，与 Shared 解析器和反向代理预算保持一致。
    if len(question.encode("utf-8")) > MAX_QUESTION_BYTES:
        raise ApiError("admin_assistant.request_invalid")
    for char in question:
        codepoint = ord(char)
        category = unicodedata.category(char)
        if (
            codepoint == 0
            or codepoint in _BIDI_CONTROLS
            or category == "Cs"
            or (category == "Cc" and char not in {"\t", "\n"})
            or 0xFDD0 <= codepoint <= 0xFDEF
            or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}
        ):
            # 危险字符不做清洗替换，避免服务端解析内容与用户审计语义出现差异。
            raise ApiError("admin_assistant.request_invalid")


__all__ = ["MAX_QUESTION_BYTES", "MAX_WIRE_BYTES", "read_assistant_question"]
