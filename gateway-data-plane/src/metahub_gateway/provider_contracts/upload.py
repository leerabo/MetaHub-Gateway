"""PRD-064 canonical 报告的可选 Control 内部上报客户端。

模块职责：把已经原子落盘并严格验证的报告提交到同源 Control API。维护边界：内部 Token
只从命名环境变量读取并隐藏 repr；客户端禁用代理、重定向和自由响应日志，失败只返回固定码。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NoReturn
from urllib.parse import quote

import httpx

from .models import ContractRunReport
from .network import (
    NetworkBoundaryError,
    ServiceOrigin,
    TRUSTED_CONTROL_ORIGIN_ENV,
    TRUSTED_TENANT_ID_ENV,
    identifier_exposes_secret,
    normalize_service_origin,
    response_is_same_origin,
    service_origins_match,
)
from .report import canonical_report_bytes, report_hash


# 上报响应只需容纳一个 run 摘要；有界读取防止错误代理返回无限 HTML 或诊断正文。
_MAX_RESPONSE_BYTES = 128 * 1024
_INTERNAL_TOKEN_ENV = "METAHUB_INTERNAL_CONFIG_TOKEN"


class ControlUploadError(RuntimeError):
    """表示已配置 Control 上报失败；异常不保存 URL、Token 或响应正文。"""

    code = "adapter.contract_upload_failed"

    def __init__(self) -> None:
        """构造固定上报错误，供 CLI 安全映射为退出码 4。"""

        super().__init__(self.code)


class _DuplicateJsonKey(ValueError):
    """标记 Control 响应中的重复 JSON key，不保存字段原文。"""


@dataclass(frozen=True)
class ControlUploadConfig:
    """保存严格 Control origin、内部 Token 和有界超时。

    Token 永远隐藏 repr 且只用于 ``X-Internal-Token``；配置不接受 Bearer 或用户 Session。
    """

    origin: ServiceOrigin
    tenant_id: str
    internal_token_env: str
    internal_token: str = field(repr=False)
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class ControlUploadResult:
    """保存上报后的非敏感确认事实，供 CLI 或验收判断首次写入与幂等回放。"""

    run_id: str
    replayed: bool


def _fail_upload() -> NoReturn:
    """统一抛出固定上报错误并切断所有自由异常正文。"""

    # 上报错误不能保存 Control URL、Token 或响应片段，CLI 只需要稳定失败分类。
    raise ControlUploadError from None


def build_control_upload_config(
    control_url: object,
    *,
    token_env: object = "METAHUB_INTERNAL_CONFIG_TOKEN",
    allow_loopback_http: bool = False,
    timeout_seconds: float = 10.0,
    environ: Mapping[str, str] | None = None,
) -> ControlUploadConfig:
    """从显式 Control URL 与命名环境变量构造安全上报配置。

    参数不接受 Token 明文；返回配置隐藏 Key。HTTPS 是默认边界，HTTP 仅允许显式 loopback。
    """

    if (
        not isinstance(token_env, str)
        or token_env != _INTERNAL_TOKEN_ENV
        or type(allow_loopback_http) is not bool
        or type(timeout_seconds) not in {int, float}
        or not 1 <= float(timeout_seconds) <= 30
    ):
        _fail_upload()
    environment = environ if environ is not None else os.environ
    try:
        origin = normalize_service_origin(
            control_url,
            allow_loopback_http=allow_loopback_http,
        )
        trusted_origin = normalize_service_origin(
            environment.get(TRUSTED_CONTROL_ORIGIN_ENV),
            allow_loopback_http=allow_loopback_http,
        )
    # Control origin 不合法时只返回固定上报码，不泄露内部部署地址。
    except NetworkBoundaryError:
        _fail_upload()
    trusted_tenant_id = environment.get(TRUSTED_TENANT_ID_ENV)
    if (
        not service_origins_match(origin, trusted_origin)
        or not isinstance(trusted_tenant_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", trusted_tenant_id)
    ):
        # CLI 目标不能自行授权内部 Token；部署环境未绑定同源 Control 和租户时必须在读 Token 前失败。
        _fail_upload()
    token = environment.get(token_env)
    if (
        not isinstance(token, str)
        or not 16 <= len(token) <= 512
        or any(character.isspace() or ord(character) < 32 for character in token)
    ):
        # 未配置或畸形 Token 时不能发送匿名报告，也不能回显环境变量名或部署状态。
        _fail_upload()
    return ControlUploadConfig(
        origin=origin,
        tenant_id=trusted_tenant_id,
        internal_token_env=token_env,
        internal_token=token,
        timeout_seconds=float(timeout_seconds),
    )


def upload_report(
    report: ContractRunReport,
    config: ControlUploadConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ControlUploadResult:
    """向 Control 内部入口同步提交 canonical 报告并严格验证确认响应。

    参数：已验证报告、隐藏 Token 的配置和仅供测试注入的 transport。返回：安全确认事实。
    请求复用报告 request/trace，使服务端幂等键、报告正文和 middleware 链路精确一致。
    """

    # 内部 Token 只允许上报部署环境绑定的租户；环境变量名和链路 ID 也必须在首个
    # 无凭据 health 前复核，避免调用方构造 dataclass 绕过 builder 或让 Token 片段落日志。
    if (
        report.tenant_id != config.tenant_id
        or config.internal_token_env != _INTERNAL_TOKEN_ENV
        or identifier_exposes_secret(report.request_id, config.internal_token)
        or identifier_exposes_secret(report.trace_id, config.internal_token)
    ):
        _fail_upload()
    body = canonical_report_bytes(report)
    endpoint = (
        f"{config.origin.base_url}/internal/tenants/"
        f"{quote(report.tenant_id, safe='')}/adapter-contract-runs"
    )
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Token": config.internal_token,
        "X-Request-ID": report.request_id,
        "X-Trace-ID": report.trace_id,
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            _verify_control_identity(client, config, report)
            with client.stream("POST", endpoint, headers=headers, content=body) as response:
                response_body = _read_bounded(response)
                if (
                    response.status_code != 200
                    or response.is_redirect
                    or not response_is_same_origin(config.origin, response.url)
                    or response.headers.get("x-request-id") != report.request_id
                    or response.headers.get("x-trace-id") != report.trace_id
                    or "no-store" not in response.headers.get("cache-control", "").lower()
                ):
                    _fail_upload()
        payload = _strict_json_object(response_body)
        run = payload.get("adapter_contract_run")
        replayed = payload.get("replayed")
        if (
            type(run) is not dict
            or type(replayed) is not bool
            or payload.get("request_id") != report.request_id
            or payload.get("trace_id") != report.trace_id
            or run.get("adapter_contract_run_id") != report.run_id
            or run.get("tenant_id") != report.tenant_id
            or run.get("report_hash") != report_hash(report)
        ):
            _fail_upload()
        return ControlUploadResult(run_id=report.run_id, replayed=replayed)
    # 已归一的上报失败保持原固定语义，不需要再次包装或拼接上下文。
    except ControlUploadError:
        raise
    # 网络、JSON 或类型异常可能包含 URL 与响应片段，统一切断后只保留固定 upload code。
    except (httpx.HTTPError, OSError, RuntimeError, TypeError, ValueError):
        _fail_upload()


def _verify_control_identity(
    client: httpx.Client,
    config: ControlUploadConfig,
    report: ContractRunReport,
) -> None:
    """在发送内部 Token 前无凭据验证同源目标确实是 MetaHub Control API。

    参数：禁代理客户端、受信配置和报告链路。返回：成功时无返回值。
    预检复用报告 request/trace，失败只抛固定上报码且绝不执行后续有 Token 的 POST。
    """

    endpoint = f"{config.origin.base_url}/health"
    headers = {"X-Request-ID": report.request_id, "X-Trace-ID": report.trace_id}
    with client.stream("GET", endpoint, headers=headers) as response:
        body = _read_bounded(response)
        media_type = response.headers.get("content-type", "").partition(";")[0]
        if (
            response.status_code != 200
            or response.is_redirect
            or not response_is_same_origin(config.origin, response.url)
            or media_type.lower() != "application/json"
            or response.headers.get("x-request-id") != report.request_id
            or response.headers.get("x-trace-id") != report.trace_id
        ):
            # 身份预检没有 Token；任一 HTTP 合同漂移都必须阻断有凭据上报。
            _fail_upload()
    payload = _strict_json_object(body)
    if (
        payload.get("status") != "ok"
        or payload.get("service_name") != "control-plane-api"
        or payload.get("request_id") != report.request_id
        or payload.get("trace_id") != report.trace_id
    ):
        # 错误端口或代理返回的自由 JSON 不能凭 200 状态冒充 Control 服务身份。
        _fail_upload()


def _read_bounded(response: httpx.Response) -> bytes:
    """同步有界读取 Control 响应，拒绝非法 Content-Length 和超限分块。"""

    raw_length = response.headers.get("content-length")
    if raw_length is not None:
        if not raw_length.isascii() or not raw_length.isdecimal():
            _fail_upload()
        if int(raw_length) > _MAX_RESPONSE_BYTES:
            _fail_upload()
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
            _fail_upload()
        body.extend(chunk)
    return bytes(body)


def _strict_json_object(body: bytes) -> Mapping[str, object]:
    """严格解析 Control UTF-8 JSON object，拒绝重复 key、NaN 和数组根。"""

    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
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
        _fail_upload()
    if type(value) is not dict:
        _fail_upload()
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造响应对象并拒绝任意层重复 key，避免 hash 与字段选择产生歧义。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    """拒绝 Control 响应中的 NaN/Infinity 非标准常量。

    参数是解析器提供的常量名称但不会被记录；返回路径永远抛出固定解析失败。
    """

    # 非标准数值会破坏响应的跨语言一致性，必须在字段确认前拒绝。
    raise ValueError


__all__ = [
    "ControlUploadConfig",
    "ControlUploadError",
    "ControlUploadResult",
    "build_control_upload_config",
    "upload_report",
]
