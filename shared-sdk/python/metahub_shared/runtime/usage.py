"""模块职责：提供 Provider raw usage 的通用脱敏、限深和 JSON 安全转换。

维护边界：该模块只处理 usage 子对象，任何认证信息、Prompt、Response 或超大私有字段都不能进入账本和 API。
来源文件：shared-sdk/python/metahub_shared/runtime/usage.py
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

# raw usage 的大小限制和敏感键集合集中定义，避免不同 Adapter 使用不一致的脱敏规则。
MAX_USAGE_DEPTH = 4
MAX_USAGE_KEYS = 64
MAX_USAGE_LIST_ITEMS = 32
MAX_USAGE_STRING_LENGTH = 256
REDACTED_VALUE = "[REDACTED]"
TRUNCATED_VALUE = "[TRUNCATED]"
SENSITIVE_EXACT_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "credential",
    "credentials",
    "prompt",
    "response",
    "message",
    "messages",
    "content",
    "request_body",
    "response_body",
}
SENSITIVE_KEY_SUFFIXES = ("_api_key", "_secret", "_password", "_access_token", "_authorization")


def sanitize_provider_usage(value: object) -> dict[str, Any]:
    """把 Provider usage 子对象转换成有界、脱敏且可 JSON 序列化的字典。

    正常 `input_tokens/output_tokens` 等计费字段会保留；认证、正文和超限内容会替换为明确占位符。
    """

    if not isinstance(value, Mapping):
        # 非对象 usage 无法提供稳定字段语义，统一返回空对象而不是保存任意文本。
        return {}
    sanitized = _sanitize_mapping(value, depth=0)
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_mapping(value: Mapping[object, object], *, depth: int) -> dict[str, Any]:
    """递归处理字典并限制键数量，超出部分使用单一截断标记说明。"""

    # 达到深度上限后不再遍历未知对象，避免恶意 usage 造成递归开销或深层敏感值泄露。
    if depth >= MAX_USAGE_DEPTH:
        return {"_truncated": TRUNCATED_VALUE}
    result: dict[str, Any] = {}
    for index, (raw_key, raw_value) in enumerate(value.items()):
        # 单个 usage 对象最多保留固定数量字段，保证日志、数据库和 API 响应大小可控。
        if index >= MAX_USAGE_KEYS:
            result["_truncated"] = TRUNCATED_VALUE
            break
        key = str(raw_key)[:128]
        normalized_key = key.strip().lower().replace("-", "_")
        if _is_sensitive_key(normalized_key):
            # 敏感字段只保留已脱敏占位，不保存原始值、长度或哈希。
            result[key] = REDACTED_VALUE
            continue
        result[key] = _sanitize_value(raw_value, depth=depth + 1)
    return result


def _sanitize_value(value: object, *, depth: int) -> Any:
    """把嵌套值归一为安全 JSON 标量、字典或有界数组。"""

    if isinstance(value, Mapping):
        return _sanitize_mapping(value, depth=depth)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [_sanitize_value(item, depth=depth + 1) for item in value[:MAX_USAGE_LIST_ITEMS]]
        # 数组超限时保留明确截断标记，使排障人员能区分“原本为空”和“被安全裁剪”。
        if len(value) > MAX_USAGE_LIST_ITEMS:
            items.append(TRUNCATED_VALUE)
        return items
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # JSON 不支持 NaN/Infinity，非法浮点必须转为空值而不是产生非标准 JSON。
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value if len(value) <= MAX_USAGE_STRING_LENGTH else f"{value[:MAX_USAGE_STRING_LENGTH]}{TRUNCATED_VALUE}"
    # Decimal 等安全数值对象只保留短文本；未知复杂对象不调用 repr，避免泄露内部状态。
    text = str(value)
    return text if len(text) <= MAX_USAGE_STRING_LENGTH else f"{text[:MAX_USAGE_STRING_LENGTH]}{TRUNCATED_VALUE}"


def _is_sensitive_key(key: str) -> bool:
    """判断 raw usage 键是否承载认证或请求/响应正文，避免误伤正常复数 Token 字段。"""

    return key in SENSITIVE_EXACT_KEYS or key.endswith(SENSITIVE_KEY_SUFFIXES)
