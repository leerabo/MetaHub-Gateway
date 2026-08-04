"""敏感意图分类入口的权限后置、有界严格 JSON 解析器。

路由必须先完成 Session 与 ``intent_risk.classify`` 后端校验，再调用本模块读取正文。
解析器只返回一次性字符串，并主动覆盖可变字节缓冲及清空 JSON 容器；任何错误都只暴露
固定统一错误码，绝不把正文、字段值或 JSON 解码异常拼入响应与日志。
"""

from __future__ import annotations

import json

from fastapi import Request
from metahub_shared.intent_security import MAX_INPUT_CODE_POINTS
from metahub_shared.runtime.errors import ApiError

# 128 KiB 同时覆盖 20,000 个中文 code point 和 JSON escape 开销，并限制分块恶意正文。
MAX_INTENT_BODY_BYTES = 128 * 1024


class _DuplicateJsonKey(ValueError):
    """标记任意层重复 JSON key；异常对象不保存攻击者提供的 key 或正文。"""


# OpenAPI 只描述唯一 write-only text 字段，不能让框架把它变成路由 body 参数并提前解析。
INTENT_CLASSIFICATION_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text"],
                    "properties": {
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_INPUT_CODE_POINTS,
                            "writeOnly": True,
                        }
                    },
                }
            }
        },
    }
}


async def read_intent_classification_text(request: Request) -> str:
    """在权限通过后读取并校验一次性分类正文。

    参数：已通过专用权限依赖的 FastAPI 请求。返回：长度不超过 20,000 code points 的
    原始文本，供同一调用栈分类。约束：媒体类型、字节上限、重复键、精确字段和文本边界
    分别映射稳定错误码；可变缓冲在返回或异常前都会被覆写。
    """

    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        # 只接受精确 JSON，避免代理和框架对 form、text 或 vendor JSON 采用不同边界。
        raise ApiError("intent_risk.media_type_unsupported")
    content_lengths = request.headers.getlist("content-length")
    if len(content_lengths) > 1:
        # 重复长度可能让前置代理与应用读取不同正文，读取首个分块前直接拒绝。
        raise ApiError("intent_risk.payload_invalid")
    if content_lengths:
        raw_length = content_lengths[0]
        if (
            not raw_length.isascii()
            or not raw_length.isdecimal()
            or (len(raw_length) > 1 and raw_length.startswith("0"))
        ):
            # 非规范长度无法建立唯一读取边界，固定错误不得回显原 Header。
            raise ApiError("intent_risk.payload_invalid")
        if int(raw_length) > MAX_INTENT_BODY_BYTES:
            # 已声明超限时不读取正文，避免未授权资源放大和敏感大包驻留内存。
            raise ApiError("intent_risk.payload_too_large")

    body = bytearray()
    decoded: str | None = None
    payload: object = None
    sensitive_text: str | None = None
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_INTENT_BODY_BYTES:
                # 分块传输没有可信 Content-Length，仍必须执行同一硬上限。
                raise ApiError("intent_risk.payload_too_large")
            body.extend(chunk)
        if not body:
            # 空 wire 不等同于空字符串字段，先按 JSON 结构错误拒绝。
            raise ApiError("intent_risk.payload_invalid")
        try:
            decoded = bytes(body).decode("utf-8", errors="strict")
            payload = json.loads(
                decoded,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            _DuplicateJsonKey,
            RecursionError,
            TypeError,
            ValueError,
        ):
            # 解码器异常可能引用正文位置或片段，切断异常链后只公开固定码。
            raise ApiError("intent_risk.payload_invalid") from None
        if type(payload) is not dict or set(payload) != {"text"}:
            # 精确字段集合阻止 tenant、阈值、模式、result ID 或未来字段被旧服务静默忽略。
            raise ApiError("intent_risk.payload_invalid")
        value = payload["text"]
        if type(value) is not str:
            # bool、数值、数组和字符串子类都不能借宽松转换成为敏感正文。
            raise ApiError("intent_risk.payload_invalid")
        sensitive_text = value
        if not sensitive_text.strip() or len(sensitive_text) > MAX_INPUT_CODE_POINTS:
            # 结构正确后的空白或 code point 超限使用独立 422，仍不回显文本或长度。
            raise ApiError("intent_risk.text_invalid")
        return sensitive_text
    finally:
        # bytearray 可确定覆盖；dict 清空并释放其正文引用。Python 字符串不可原位擦除，
        # 因此调用方还必须在分类完成后立刻覆盖局部变量，不能把它放入 task、异常或日志。
        for index in range(len(body)):
            body[index] = 0
        body.clear()
        if isinstance(payload, dict):
            payload.clear()
        decoded = None
        sensitive_text = None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON 对象并拒绝任意层重复 key，不保存重复字段诊断文本。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            # 重复 key 在代理与解析器间可能选取不同值，必须整条拒绝且不记录 key 名。
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    """拒绝 NaN/Infinity 等非标准常量，防止跨解析器合同分歧。"""

    # 非标准常量没有合法分类语义，异常对象故意不保存原始 token。
    raise ValueError


__all__ = [
    "INTENT_CLASSIFICATION_OPENAPI",
    "MAX_INTENT_BODY_BYTES",
    "read_intent_classification_text",
]
