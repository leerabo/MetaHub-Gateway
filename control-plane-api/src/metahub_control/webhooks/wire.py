"""Webhook 写接口的权限后置 wire 解析器。

路由必须先完成 Session、角色和租户恢复，再调用本模块读取正文。这样未授权请求即使携带
畸形 JSON 或敏感字段，也只得到稳定 401/403，不能借解析差异探测业务合同。
"""

from __future__ import annotations

import json

from fastapi import Request
from metahub_shared.runtime.errors import ApiError
from pydantic import ValidationError

from .models import ReplayWebhookDeliveryRequest

# replay 正文只有一个短幂等键，4 KiB 足以兼容 JSON 开销并限制恶意请求的内存占用。
MAX_REPLAY_DELIVERY_BODY_BYTES = 4 * 1024


class _DuplicateJsonKey(ValueError):
    """标记重复 JSON key；异常本身不保存 key 或正文，避免后续诊断链泄露输入。"""


# 手工解析正文后仍保留与原 Pydantic 路由参数一致的 OpenAPI 请求体说明。
REPLAY_DELIVERY_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": ReplayWebhookDeliveryRequest.model_json_schema(),
            }
        },
    }
}


async def read_replay_delivery_request(request: Request) -> ReplayWebhookDeliveryRequest:
    """在 RBAC 通过后有界解析顶层 replay 正文。

    参数：request 是已通过 ``require_webhook_manager`` 的当前请求。
    返回：只包含规范化幂等键的 replay DTO。
    约束：正文不超过 4 KiB；非 JSON、重复键、未知字段与类型错误共享固定错误且不回显正文。
    """

    content_type = (
        request.headers.get("content-type", "")
        .partition(";")[0]
        .strip()
        .lower()
    )
    # 仅接受明确 JSON 媒体类型，避免代理与框架对同一敏感正文产生不同解析语义。
    if content_type != "application/json" and not content_type.endswith("+json"):
        raise ApiError("infra.validation_failed")
    content_lengths = request.headers.getlist("content-length")
    if len(content_lengths) > 1:
        # 多个长度声明可能被代理按不同规则合并，必须在读取任何正文前失败关闭。
        raise ApiError("infra.validation_failed")
    if content_lengths:
        raw_length = content_lengths[0]
        if (
            not raw_length.isascii()
            or not raw_length.isdecimal()
            or (len(raw_length) > 1 and raw_length.startswith("0"))
        ):
            # 拒绝含糊或非规范长度，避免代理与应用对正文边界作出不同解释。
            raise ApiError("infra.validation_failed")
        if int(raw_length) > MAX_REPLAY_DELIVERY_BODY_BYTES:
            # 已声明超限时不消费正文，避免把敏感或恶意大包读入进程内存。
            raise ApiError("infra.validation_failed")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REPLAY_DELIVERY_BODY_BYTES:
            # 未声明长度或分块传输同样受硬上限约束，不能依赖 Content-Length 才限流。
            raise ApiError("infra.validation_failed")
        body.extend(chunk)
    try:
        text = bytes(body).decode("utf-8", errors="strict")
        raw_payload = json.loads(
            text,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
        # 顶层数组、标量与 null 均不能表达 replay 命令，必须在 DTO 转换前明确拒绝。
        if type(raw_payload) is not dict:
            raise ValueError
        return ReplayWebhookDeliveryRequest.model_validate(raw_payload, strict=True)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        # 解析异常可能引用正文片段；切断异常链后只让统一中间件看到固定错误码。
        raise ApiError("infra.validation_failed") from None


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """把 JSON object 转为字典，并在不记录 key 原文的前提下拒绝任意层重复键。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    """拒绝 NaN/Infinity 等非标准 JSON 常量，避免不同解析器产生合同分歧。"""

    # 常量原文可能来自敏感正文，使用无消息异常让统一转换层只公开固定错误码。
    raise ValueError
