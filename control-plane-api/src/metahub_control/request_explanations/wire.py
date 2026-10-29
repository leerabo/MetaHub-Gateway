"""PRD-054 请求解释 GET 接口的严格原始 wire 校验。

认证与 RBAC 由路由依赖先执行；本模块随后拒绝 query、正文、压缩编码、重复链路头和
非 canonical 源请求 ID。这样 viewer/anonymous 不能借输入校验差异探测接口合同，已授权
调用者也不会因代理对同一报文采用不同解释而得到含糊结果。
"""

from __future__ import annotations

import re

from fastapi import Request

from metahub_shared.runtime.errors import ApiError

# Gateway 与迁移使用同一受限字符集；不做 strip 或 Unicode 归一化，避免两个外部值
# 在数据库查询前被折叠成同一个 request_id。
SOURCE_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
CHAIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


async def validate_request_explanation_get(
    request: Request,
    source_request_id: str,
) -> str:
    """校验已通过 RBAC 的解释 GET，并返回原样 canonical 源请求 ID。

    参数：``request`` 是 ASGI 原始请求，``source_request_id`` 是 FastAPI 解码后的 path。
    返回：未改写的安全 ID。关键约束：不接受任何 query/body/content 编码；链路头最多
    各一个且必须是受限 ASCII，防止代理合并语义进入访问审计。
    """

    # Path 保持原样 canonical；trim/Unicode 折叠会让两个外部请求 ID 错误映射为同一事实。
    if not SOURCE_REQUEST_ID_PATTERN.fullmatch(source_request_id or ""):
        # 非法 ID 在数据库访问前统一拒绝，避免用格式差异探测租户内记录。
        raise ApiError("request_explanation.request_id_invalid")
    if request.query_params.multi_items():
        # Explain 响应是固定最小披露合同，未知或重复 query 不能被静默忽略。
        raise ApiError("request_explanation.request_invalid")
    _validate_no_entity_headers(request)
    _validate_optional_chain_header(request, b"x-request-id")
    _validate_optional_chain_header(request, b"x-trace-id")
    await _reject_request_body(request)
    return source_request_id


def _validate_no_entity_headers(request: Request) -> None:
    """拒绝会让无正文 GET 产生第二种解释的实体或传输头。"""

    raw_headers = request.scope.get("headers", [])
    grouped: dict[bytes, list[bytes]] = {}
    for key, value in raw_headers:
        grouped.setdefault(key.lower(), []).append(value)
    if grouped.get(b"content-encoding") or grouped.get(b"transfer-encoding"):
        # 压缩或分块正文在不同代理上可能被先解码再丢弃，解释接口统一失败关闭。
        raise ApiError("request_explanation.request_invalid")
    lengths = grouped.get(b"content-length", [])
    if len(lengths) > 1 or (lengths and lengths[0] != b"0"):
        # 仅允许一个 canonical 零长度声明；前导零、符号和重复值均拒绝。
        raise ApiError("request_explanation.request_invalid")


def _validate_optional_chain_header(request: Request, name: bytes) -> None:
    """校验一个可选原始链路头，保留重复项而不依赖框架合并结果。"""

    values = [
        value
        for key, value in request.scope.get("headers", [])
        if key.lower() == name
    ]
    if len(values) > 1 or (values and b"," in values[0]):
        # 多值或代理合并头会让中间件与审计看到不同 ID，不能继续生成查看事实。
        raise ApiError("request_explanation.request_invalid")
    if not values:
        return
    try:
        value = values[0].decode("ascii", errors="strict")
    # 解码失败的原始头可能含任意字节，只能折叠成统一 wire 错误。
    except UnicodeDecodeError as exc:
        # 链路标识只允许受限 ASCII，原始字节不得进入错误响应或日志。
        raise ApiError("request_explanation.request_invalid") from exc
    if not CHAIN_ID_PATTERN.fullmatch(value):
        raise ApiError("request_explanation.request_invalid")


async def _reject_request_body(request: Request) -> None:
    """流式确认 GET 没有正文，不把潜在敏感正文缓存在内存中。"""

    async for chunk in request.stream():
        if chunk:
            # 收到第一个非空 chunk 即拒绝；不继续聚合，也不记录内容或长度。
            raise ApiError("request_explanation.request_invalid")


__all__ = ["SOURCE_REQUEST_ID_PATTERN", "validate_request_explanation_get"]
