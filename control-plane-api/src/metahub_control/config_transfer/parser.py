"""配置迁移接口的严格 JSON reader。

FastAPI 默认 JSON reader 不提供重复键拒绝和本 PRD 的统一资源上限，因此迁移接口直接读取有限正文，
再用标准库严格解码。任何异常都折叠成稳定错误码，不把原始正文或解码器异常写入响应和日志。
"""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import Request
from metahub_shared.runtime.errors import ApiError

# 迁移文件上限兼顾企业 POC 批量配置和 Control API 内存边界；CLI 使用同一数值做本地门禁。
MAX_CONFIG_BODY_BYTES = 2 * 1024 * 1024
MAX_CONFIG_JSON_DEPTH = 8
# 2 MiB 正文仍是权威内存上限；20 万节点可覆盖 5,000 行最大合法清单的字段节点，
# 同时阻断由海量空对象/数组制造的解析后对象放大。
MAX_CONFIG_JSON_NODES = 200_000
MAX_CONFIG_STRING_LENGTH = 4_096


class _DuplicateJsonKey(ValueError):
    """标记任意对象层级的重复 JSON key，异常文本不得穿透 API。"""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """把 JSON pairs 转为字典，并在首个重复 key 时失败关闭。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_non_finite_number(value: str) -> None:
    """拒绝 JSON 标准之外的 NaN/Infinity，避免 hash 与数据库比较产生歧义。"""

    # 非标准数值会让不同 JSON 实现得到不同结果，必须在形成可审计 hash 前失败关闭。
    raise ValueError("non-finite number")


def _parse_finite_float(value: str) -> float:
    """解析 JSON 浮点数并拒绝指数溢出形成的无穷值。"""

    parsed = float(value)
    if not math.isfinite(parsed):
        # ``1e999`` 是合法 JSON token，但 Python 会得到 inf；必须在进入 manifest 前拒绝。
        raise ValueError("non-finite number")
    return parsed


async def read_strict_json_object(request: Request) -> dict[str, Any]:
    """读取有限、UTF-8、无重复键的 JSON 对象。

    参数：FastAPI 原始请求。返回：通过结构资源门禁的根对象。
    关键约束：正文从不进入异常、日志或审计；Content-Length 只是提前拒绝提示，流式累计仍是权威上限。
    """

    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        # 迁移合同只接受 JSON；不尝试猜测表单或文本，避免同一字节流出现多种解释。
        raise ApiError("config_transfer.body_invalid")
    declared = request.headers.get("content-length")
    if declared:
        try:
            declared_length = int(declared)
        # 长度头由客户端控制，解析失败不能泄露原值或绕过后续流式累计门禁。
        except ValueError as exc:
            # 非法长度头不能替代真实流式上限，统一按无效正文处理且不回显原值。
            raise ApiError("config_transfer.body_invalid") from exc
        if declared_length < 1:
            # 空正文不等于导出接口要求的空对象，调用方必须显式发送 ``{}``。
            raise ApiError("config_transfer.body_invalid")
        if declared_length > MAX_CONFIG_BODY_BYTES:
            # 提前拒绝明确超限请求可避免无意义读取，流式累计仍会再次执行权威检查。
            raise ApiError("config_transfer.body_too_large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_CONFIG_BODY_BYTES:
            # 不能信任 Content-Length；分块传输同样必须受 2 MiB 内存边界约束。
            raise ApiError("config_transfer.body_too_large")
        body.extend(chunk)
    if not body:
        # 缺失正文无法形成稳定迁移清单，不能让 JSON decoder 的异常文本进入响应。
        raise ApiError("config_transfer.body_invalid")
    try:
        text = bytes(body).decode("utf-8", errors="strict")
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
            parse_float=_parse_finite_float,
        )
    # 解码异常可能夹带攻击者正文片段，统一折叠为固定领域错误码。
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # 解码器异常可能含攻击者字段或正文片段，对外只能保留固定领域码。
        raise ApiError("config_transfer.body_invalid") from exc
    if not isinstance(parsed, dict):
        # 顶层数组或标量没有 schema 身份，不能被当作空清单或单行配置兼容处理。
        raise ApiError("config_transfer.body_invalid")
    structure_error = config_json_structure_error(parsed)
    if structure_error is not None:
        # 资源超限与非法字符属于不同公开合同，不能都折叠为 413。
        raise ApiError(structure_error)
    return parsed


def config_json_structure_error(value: object) -> str | None:
    """迭代检查 JSON 资源边界并返回对应的稳定错误码。

    深度、节点或字符串长度超限返回 ``body_too_large``；控制字符和非有限数属于内容无效，
    返回 ``body_invalid``。合法结构返回 ``None``，检查过程不会复制或记录正文。
    """

    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_CONFIG_JSON_DEPTH or nodes > MAX_CONFIG_JSON_NODES:
            return "config_transfer.body_too_large"
        if isinstance(current, str):
            if len(current) > MAX_CONFIG_STRING_LENGTH:
                return "config_transfer.body_too_large"
            if any(
                ord(character) < 32 or 127 <= ord(character) <= 159
                for character in current
            ):
                # C0/C1 字符可能污染终端和日志，属于正文非法而不是容量超限。
                return "config_transfer.body_invalid"
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                # JSON 可转义未配对 surrogate，但 UTF-8 无法编码；必须在 hash 和审计前拒绝。
                return "config_transfer.body_invalid"
            continue
        if isinstance(current, float) and not math.isfinite(current):
            # 服务内部测试也可能绕过 JSON decoder 直接传值，结构门禁仍需防御非有限数。
            return "config_transfer.body_invalid"
        if isinstance(current, dict):
            for key, child in current.items():
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
            continue
        if isinstance(current, list):
            for child in current:
                stack.append((child, depth + 1))
    return None
