"""通过 Control API 安全迁移 MetaHub 配置的命令行客户端。

CLI 是标准库实现的薄客户端：租户与权限始终由服务端 Session 决定；本地只负责严格 JSON、
冻结网络 origin、原子导出文件和一次性 Virtual Key 私有文件，不接触数据库或客户代码目录。
"""

from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import math
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Sequence, TextIO
from uuid import uuid4

# Control API 与本地文件共用 2 MiB 上限，避免 CLI 成为绕过服务端资源门禁的放大器。
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 8
# 与 Control API 共用 20 万解析后节点上限，覆盖 5,000 行合法清单且仍受 2 MiB 正文硬限制。
MAX_JSON_NODES = 200_000
MAX_JSON_STRING_LENGTH = 4_096
MAX_TOKEN_LENGTH = 4_096
MAX_RESPONSE_ERRORS = 200
DEFAULT_CONTROL_URL = "http://localhost:8001"
DEFAULT_TIMEOUT_SECONDS = 30.0
SCHEMA_VERSION = "metahub.config/v1"

# 四类集合与数量上限必须和 Control API parser/service 保持一致。
COLLECTION_LIMITS = {
    "providers": 500,
    "models": 2_000,
    "aliases": 2_000,
    "virtual_keys": 2_000,
}
MAX_TOTAL_ROWS = 5_000

# CLI 公开输出只允许稳定机器字段；任何异常文本、URL、路径、正文和 Token 都不会进入该合同。
SAFE_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,119}$")
SAFE_CHAIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:authorization|cookie|credential|secret|token|password|passphrase|bearer|"
    r"plain[_-]?value|key[_-]?hash|key[_-]?prefix|encrypted[_-]?key|"
    r"(?:api|access|private|provider)[_-]?key|(?:^|[_-])key(?:$|[_-]))",
    re.IGNORECASE,
)
# camelCase 凭据键先规范成下划线形式，保证 clientKey/signingKey/virtualKey 与蛇形变体同形拒绝。
CAMEL_CASE_BOUNDARY_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# 导入报告字段冻结，防止服务端协议漂移把新增正文或秘密带入 CLI 普通输出。
COUNT_FIELDS = frozenset(
    {
        "providers_created",
        "providers_unchanged",
        "models_created",
        "models_unchanged",
        "aliases_created",
        "aliases_unchanged",
        "virtual_keys_created",
        "virtual_keys_unchanged",
        "generated_virtual_keys",
        "disabled_virtual_keys",
    }
)
REPORT_FIELDS = frozenset(
    {
        "status",
        "valid",
        "applied",
        "manifest_sha256",
        "schema_version",
        "counts",
        "error_count",
        "errors_truncated",
        "errors",
        "config_import_id",
        "request_id",
        "trace_id",
    }
)

# ConfigExport 每类行均使用显式白名单，凭据字段即使由异常服务端返回也不能进入输出文件。
EXPORT_ROW_FIELDS = {
    "providers": frozenset(
        {
            "provider_code",
            "name",
            "provider_type",
            "base_url",
            "auth_type",
            "description",
            "maintenance_note",
            "status",
        }
    ),
    "models": frozenset(
        {
            "internal_model_id",
            "provider_code",
            "provider_model_id",
            "display_name",
            "capabilities",
            "context_window",
            "input_price_per_1k",
            "output_price_per_1k",
            "cache_price_per_1k",
            "currency",
            "price_schema_version",
            "description",
            "status",
        }
    ),
    "aliases": frozenset(
        {
            "alias",
            "environment",
            "default_internal_model_id",
            "fallback_internal_model_id",
            "fallback_conditions",
            "routing_mode",
            "quality_cost_config",
            "description",
            "status",
        }
    ),
    "virtual_keys": frozenset(
        {
            "name",
            "environment",
            "description",
            "project_code",
            "application_code",
            "status",
            "rpm_limit",
            "tpm_limit",
            "concurrency_limit",
            "rate_limit_status",
        }
    ),
}
QUALITY_FIELDS = frozenset(
    {
        "quality_weight_bps",
        "cost_weight_bps",
        "latency_weight_bps",
        "quality_floor_bps",
        "minimum_sample_count",
    }
)

# 行级错误的 field 只能来自导入协议本身。``credential`` 是服务端对所有敏感键的固定折叠值，
# 它描述错误类别而不是凭据内容，因此必须允许；任何攻击者自定义键仍会因不在白名单中而失败关闭。
REPORT_ERROR_FIELDS = frozenset(
    {
        "credential",
        "unknown",
        "schema_version",
        "source_tenant_id",
        "exported_at",
        "collections",
        "row",
        *COLLECTION_LIMITS,
    }
).union(*(fields for fields in EXPORT_ROW_FIELDS.values()))


class _DuplicateJsonKey(ValueError):
    """表示任意层级出现重复 JSON key，异常对象不携带攻击者字段。"""


class CliFailure(Exception):
    """携带可公开固定错误码与退出码的内部控制流异常。

    参数只允许安全机器字段；原始异常、路径、URL、响应正文和 Token 不得保存在实例中。
    """

    def __init__(
        self,
        code: str,
        exit_code: int,
        *,
        request_id: str = "",
        trace_id: str = "",
        config_import_id: str | None = None,
    ) -> None:
        """创建一条可脱敏输出的 CLI 失败结果。"""

        super().__init__(code)
        self.code = code if SAFE_CODE_PATTERN.fullmatch(code) else "gatewayctl.failed"
        self.exit_code = exit_code
        self.request_id = request_id if _is_safe_chain_id(request_id) else ""
        self.trace_id = trace_id if _is_safe_chain_id(trace_id) else ""
        self.config_import_id = (
            config_import_id if _is_safe_text(config_import_id, maximum=128) else None
        )


class _SafeArgumentParser(argparse.ArgumentParser):
    """把 argparse 的自由文本错误折叠为固定 JSON 错误，避免回显 Token 或路径。"""

    def error(self, message: str) -> None:
        """忽略可能包含原始 argv 的错误文本，并交给统一失败输出。"""

        del message
        raise CliFailure("gatewayctl.argument_invalid", 2)


@dataclass(frozen=True)
class HttpResponse:
    """保存已经通过 Content-Type、大小和严格 JSON 门禁的 HTTP 响应。"""

    status: int
    payload: dict[str, Any]


@dataclass
class SecretFileReservation:
    """保存 apply 前独占创建并持续持有的一次性密钥文件句柄。"""

    path: Path
    descriptor: int
    committed: bool = False

    def commit(self, payload: dict[str, Any], *, config_import_id: str | None) -> None:
        """写入全部密钥 JSON、刷新磁盘并关闭句柄；失败时只抛固定退出码 6。"""

        try:
            body = _json_bytes(payload) + b"\n"
            view = memoryview(body)
            while view:
                written = os.write(self.descriptor, view)
                # 零字节写入无法推进密钥交付，继续循环会形成死循环并遗留半成品文件。
                if written <= 0:
                    raise OSError
                view = view[written:]
            os.fsync(self.descriptor)
            os.close(self.descriptor)
            self.descriptor = -1
            self.committed = True
        # 序列化或磁盘提交任一步失败都必须销毁残留文件，不能暴露部分一次性密钥。
        except (OSError, TypeError, ValueError) as exc:
            self.abort()
            raise CliFailure(
                "gatewayctl.generated_keys_commit_failed",
                6,
                config_import_id=config_import_id,
            ) from exc

    def abort(self) -> None:
        """关闭未提交句柄并删除空白或部分密钥文件，不让失败残留扩大泄漏面。"""

        if self.descriptor >= 0:
            # 清理阶段不能用关闭失败覆盖最初业务错误，也不能把系统异常文本写入输出。
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            self.descriptor = -1
        if not self.committed:
            # 未提交文件可能含部分明文密钥，因此尽最大努力立即删除且不回显具体路径。
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


class FrozenOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """只允许 307/308 保持方法的同 origin 重定向，阻断 Authorization 跨站转发。"""

    def __init__(self, origin: tuple[str, str, int]) -> None:
        """冻结初始化时已经验证的 scheme、host 与有效端口。"""

        super().__init__()
        self._origin = origin

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        """验证跳转目标后交给标准库构造请求；拒绝会把 POST 改写为 GET 的状态码。"""

        del fp, msg, headers
        target = urllib.parse.urljoin(req.full_url, newurl)
        # 只接受保持请求语义的状态码和同源目标，避免认证头跨站或 POST 被降级为 GET。
        if code not in {307, 308} or not _is_safe_redirect_target(target, self._origin):
            raise CliFailure("gatewayctl.redirect_rejected", 5)
        # 标准库默认拒绝 POST 的 307/308，因此在 origin 校验后显式保留方法、正文和认证头。
        # 只有同 origin 才会进入这里，Authorization 不会被转发给其他主机、协议或端口。
        return urllib.request.Request(
            target,
            data=req.data,
            headers=dict(req.headers),
            origin_req_host=req.origin_req_host,
            unverifiable=True,
            method=req.get_method(),
        )


class ControlClient:
    """使用标准库 HTTPS/HTTP 发送冻结 origin 的 Control API POST 请求。"""

    def __init__(
        self,
        control_url: str,
        token: str,
        *,
        opener: urllib.request.OpenerDirector | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """验证 Control URL 并创建只允许同 origin 跳转的 opener。"""

        self.base_url, self.origin = _normalize_control_url(control_url)
        self.token = token
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(
            FrozenOriginRedirectHandler(self.origin)
        )

    def post(self, endpoint: str, payload: dict[str, Any], request_id: str) -> HttpResponse:
        """发送一个 JSON POST，并把网络、HTTP 和协议异常折叠为固定 CLI 错误。"""

        url = urllib.parse.urljoin(f"{self.base_url.rstrip('/')}/", endpoint.lstrip("/"))
        # endpoint 即使由内部常量传入也再次冻结源站，防止未来扩展意外覆盖认证边界。
        if _url_origin(url) != self.origin:
            raise CliFailure("gatewayctl.origin_invalid", 2, request_id=request_id)
        # 请求正文必须使用同一严格编码器，禁止非有限数字或不可序列化对象进入网络层。
        try:
            body = _json_bytes(payload)
        except (TypeError, ValueError) as exc:
            # 编码器异常统一映射为输入无效，底层对象和值不得进入终端错误信息。
            raise CliFailure("gatewayctl.input_invalid", 2, request_id=request_id) from exc
        # 本地同样执行服务端请求体上限，避免 CLI 成为资源门禁的放大入口。
        if len(body) > MAX_CONFIG_BYTES:
            raise CliFailure("gatewayctl.input_too_large", 2, request_id=request_id)
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
                "X-Request-ID": request_id,
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status = int(response.getcode())
                decoded = _decode_http_json(response, request_id=request_id, require_no_store=True)
                return HttpResponse(status=status, payload=decoded)
        # HTTP 错误正文仍按有界 JSON 处理，绝不直接输出服务器返回的自由文本。
        except urllib.error.HTTPError as exc:
            try:
                payload_error = _decode_http_json(
                    exc,
                    request_id=request_id,
                    require_no_store=False,
                )
            # 错误响应若不符合公开协议，必须折叠为固定代码以阻断正文或头部泄漏。
            except CliFailure:
                raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id) from None
            # 合法错误只映射稳定机器字段，服务端 message 和原始响应不会进入 CLI 输出。
            raise _http_failure(exc.code, payload_error, request_id) from None
        except CliFailure:
            raise
        # 连接、超时和 TLS 类底层错误可能包含 URL，统一转换后仅保留本地请求标识。
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise CliFailure("gatewayctl.network_unavailable", 5, request_id=request_id) from exc


def _new_request_id() -> str:
    """生成不含 Token、路径或业务值的 CLI 请求 ID。"""

    return f"req_gatewayctl_{uuid4().hex}"


def _is_safe_chain_id(value: object) -> bool:
    """判断 request_id/trace_id 是否满足日志安全字符与长度约束。"""

    return type(value) is str and SAFE_CHAIN_ID_PATTERN.fullmatch(value) is not None


def _is_safe_text(value: object, *, maximum: int = MAX_JSON_STRING_LENGTH) -> bool:
    """判断公开字符串非空、有限且不含控制字符。"""

    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and not any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
    )


def _plain_int(value: object, *, minimum: int = 0, maximum: int = 100_000_000) -> bool:
    """校验 JSON 整数并显式排除 Python 中属于 int 子类的 bool。"""

    return type(value) is int and minimum <= value <= maximum


def _json_bytes(value: object) -> bytes:
    """生成禁止 NaN/Infinity 的紧凑 UTF-8 JSON，供网络和原子文件共用。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """严格构造 JSON 对象并在首个重复 key 时失败关闭。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_non_finite_number(value: str) -> None:
    """拒绝 JSON 标准之外的非有限数字，避免不同运行时产生不一致配置语义。"""

    del value
    # 非有限数字没有跨语言一致表示，必须在解析边界立即停止而不能静默转换。
    raise ValueError("non-finite")


def _parse_finite_float(value: str) -> float:
    """解析 JSON 浮点数并拒绝指数溢出得到的无穷值。"""

    parsed = float(value)
    # 超大指数可能被 Python 解析为无穷值，必须与显式 Infinity 使用相同拒绝策略。
    if not math.isfinite(parsed):
        raise ValueError("non-finite")
    return parsed


def _strict_json_bytes(body: bytes) -> dict[str, Any]:
    """解析有限 UTF-8 JSON 对象，拒绝重复键、控制字符和资源放大结构。"""

    try:
        text = body.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
            parse_float=_parse_finite_float,
        )
    # 编码、重复键、非有限数和递归超限都属于不可信输入，只返回统一 JSON 错误。
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise CliFailure("gatewayctl.json_invalid", 2) from exc
    if not isinstance(value, dict) or _unsafe_json_structure(value):
        raise CliFailure("gatewayctl.json_invalid", 2)
    return value


def _unsafe_json_structure(value: object) -> bool:
    """迭代检查 JSON 深度、节点数、字符串长度、控制字符和有限数。"""

    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            return True
        if isinstance(current, str):
            if len(current) > MAX_JSON_STRING_LENGTH or any(
                ord(character) < 32 or 127 <= ord(character) <= 159
                for character in current
            ):
                return True
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                # 转义产生的未配对 surrogate 无法安全写成 UTF-8，必须在任何网络或文件输出前拒绝。
                return True
            continue
        if isinstance(current, float) and not math.isfinite(current):
            return True
        if isinstance(current, dict):
            for key, child in current.items():
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            for child in current:
                stack.append((child, depth + 1))
    return False


def _has_sensitive_key(value: object) -> bool:
    """递归识别凭据语义字段名，只返回布尔值且不复制攻击者 key。"""

    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                normalized_key = CAMEL_CASE_BOUNDARY_PATTERN.sub("_", str(key)).replace("-", "_")
                if SENSITIVE_KEY_PATTERN.search(normalized_key):
                    return True
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _normalize_control_url(value: str) -> tuple[str, tuple[str, str, int]]:
    """验证 Control URL，并返回无 query/fragment 的基址与冻结 origin。"""

    raw = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    # URL 解析异常可能携带原始参数，转换后禁止把用户输入写入错误流。
    except ValueError as exc:
        raise CliFailure("gatewayctl.control_url_invalid", 2) from exc
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in raw
        or any(ord(character) < 33 for character in raw)
    ):
        raise CliFailure("gatewayctl.control_url_invalid", 2)
    try:
        hostname.encode("idna")
    # 非法国际化域名不能参与源站比较，且底层编码异常不得向调用方透传。
    except UnicodeError as exc:
        raise CliFailure("gatewayctl.control_url_invalid", 2) from exc
    if scheme == "http" and not _is_loopback_host(hostname):
        raise CliFailure("gatewayctl.control_url_invalid", 2)
    effective_port = port or (443 if scheme == "https" else 80)
    if not (1 <= effective_port <= 65_535):
        raise CliFailure("gatewayctl.control_url_invalid", 2)
    path = parsed.path.rstrip("/")
    base_url = urllib.parse.urlunsplit((scheme, parsed.netloc, path, "", ""))
    return base_url, (scheme, hostname, effective_port)


def _is_loopback_host(hostname: str) -> bool:
    """只按字面 localhost 或 loopback IP 判断，避免为 URL 安全校验触发 DNS。"""

    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    # 非 IP 文本不会触发 DNS 探测，直接按非回环主机处理以维持离线安全判断。
    except ValueError:
        return False


def _url_origin(value: str) -> tuple[str, str, int] | None:
    """提取 URL 的规范 origin；无效端口或主机返回 None。"""

    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    # 无效端口无法形成可比较源站，返回空值让上层按协议失败关闭。
    except ValueError:
        return None
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    return parsed.scheme.lower(), hostname, port


def _is_safe_redirect_target(value: str, origin: tuple[str, str, int]) -> bool:
    """确认重定向没有 userinfo/query/fragment，且逐项等于冻结 origin。"""

    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port
    # 重定向目标的非法端口或 URL 结构一律视为不安全，不能继续携带认证头。
    except ValueError:
        return False
    return (
        parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and _url_origin(value) == origin
    )


def _decode_http_json(
    response: Any,
    *,
    request_id: str,
    require_no_store: bool,
) -> dict[str, Any]:
    """有界读取 application/json 响应，并拒绝缓存策略与严格 JSON 合同异常。"""

    content_type = str(response.headers.get("Content-Type", ""))
    media_parts = [part.strip().lower() for part in content_type.split(";") if part.strip()]
    # 只接受明确 UTF-8 JSON，避免浏览器式内容嗅探或错误字符集改变字段判断。
    if not media_parts or media_parts[0] != "application/json" or any(
        part not in {"charset=utf-8", "charset=\"utf-8\""} for part in media_parts[1:]
    ):
        # 媒体类型不可信时不读取或解释正文，避免错误格式绕过后续字段白名单。
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    # 成功响应可能包含一次性密钥，缺少 no-store 时客户端必须拒绝处理和落盘。
    if require_no_store and "no-store" not in str(response.headers.get("Cache-Control", "")).lower():
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            # 声明长度必须为正且受限，防止服务器诱导无界读取或空正文解析。
            if int(declared) < 1 or int(declared) > MAX_CONFIG_BYTES:
                raise ValueError
        # 非数字或越界长度均按协议错误处理，不能把原始响应头写入错误信息。
        except (TypeError, ValueError) as exc:
            raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id) from exc
    try:
        body = response.read(MAX_CONFIG_BYTES + 1)
    # 读取阶段的系统错误可能包含连接细节，仅映射为稳定网络错误和请求标识。
    except OSError as exc:
        raise CliFailure("gatewayctl.network_unavailable", 5, request_id=request_id) from exc
    # 空正文或超过上限均不具备可信协议语义，不能进入后续字段验证。
    if not body or len(body) > MAX_CONFIG_BYTES:
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    try:
        return _strict_json_bytes(body)
    # HTTP 响应解析失败属于服务端协议异常，不能复用本地输入错误退出码。
    except CliFailure as exc:
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id) from exc


def _http_failure(status: int, payload: dict[str, Any], request_id: str) -> CliFailure:
    """把 HTTP 错误分类为权限、业务或网络退出码，只保留安全服务端机器字段。"""

    raw_code = payload.get("code")
    code = raw_code if type(raw_code) is str and SAFE_CODE_PATTERN.fullmatch(raw_code) else "gatewayctl.server_rejected"
    server_request_id = payload.get("request_id")
    server_trace_id = payload.get("trace_id")
    safe_request_id = server_request_id if _is_safe_chain_id(server_request_id) else request_id
    safe_trace_id = server_trace_id if _is_safe_chain_id(server_trace_id) else ""
    if status in {401, 403}:
        exit_code = 3
    elif status in {400, 409, 413, 422}:
        exit_code = 4
    else:
        exit_code = 5
    return CliFailure(
        code,
        exit_code,
        request_id=safe_request_id,
        trace_id=safe_trace_id,
    )


def _is_reparse_point(result: os.stat_result) -> bool:
    """判断 Windows reparse point；POSIX 平台没有该属性时安全返回 False。"""

    attributes = int(getattr(result, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _path_components(path: Path, *, include_leaf: bool) -> list[Path]:
    """构造需要逐项 lstat 的绝对路径组件，不枚举任何目录内容。"""

    absolute = Path(os.path.abspath(os.fspath(path)))
    target = absolute if include_leaf else absolute.parent
    components: list[Path] = []
    current = target
    while current != current.parent:
        components.append(current)
        current = current.parent
    components.reverse()
    return components


def _ensure_no_link_components(path: Path, *, include_leaf: bool, exit_code: int) -> None:
    """逐项拒绝显式路径中的 symlink/reparse point，避免链接把文件操作改向其他位置。"""

    for component in _path_components(path, include_leaf=include_leaf):
        try:
            result = component.lstat()
        # 任一路径组件不可检查时都按文件无效处理，避免回显系统路径或权限细节。
        except OSError as exc:
            raise CliFailure("gatewayctl.file_invalid", exit_code) from exc
        if stat.S_ISLNK(result.st_mode) or _is_reparse_point(result):
            raise CliFailure("gatewayctl.file_invalid", exit_code)


def _read_input_document(path_value: str) -> dict[str, Any]:
    """从一个普通非链接文件读取最多 2 MiB，并执行本地严格 JSON 与结构门禁。"""

    path = Path(path_value)
    _ensure_no_link_components(path, include_leaf=True, exit_code=2)
    try:
        before = path.lstat()
    # 输入元数据读取失败不能区分不存在或权限原因，统一返回脱敏本地错误。
    except OSError as exc:
        raise CliFailure("gatewayctl.input_invalid", 2) from exc
    if not stat.S_ISREG(before.st_mode) or _is_reparse_point(before) or before.st_size > MAX_CONFIG_BYTES:
        code = "gatewayctl.input_too_large" if before.st_size > MAX_CONFIG_BYTES else "gatewayctl.input_invalid"
        raise CliFailure(code, 2)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            # 打开后的句柄必须再次确认普通文件，封闭预检与打开之间的链接替换竞态。
            if not stat.S_ISREG(opened.st_mode) or _is_reparse_point(opened):
                raise OSError
            chunks: list[bytes] = []
            remaining = MAX_CONFIG_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
        after = path.lstat()
    # 读取、关闭或二次元数据检查失败都不能继续解析可能不完整的配置正文。
    except OSError as exc:
        raise CliFailure("gatewayctl.input_invalid", 2) from exc
    if (
        before.st_dev != opened.st_dev
        or before.st_ino != opened.st_ino
        or after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
    ):
        raise CliFailure("gatewayctl.input_invalid", 2)
    body = b"".join(chunks)
    if not body or len(body) > MAX_CONFIG_BYTES:
        code = "gatewayctl.input_too_large" if len(body) > MAX_CONFIG_BYTES else "gatewayctl.input_invalid"
        raise CliFailure(code, 2)
    document = _strict_json_bytes(body)
    _validate_local_manifest(document)
    return document


def _validate_local_manifest(document: dict[str, Any]) -> None:
    """在发网前验证顶层 schema、集合规模和敏感字段，不替代服务端业务校验。"""

    expected = {"schema_version", "source_tenant_id", "exported_at", *COLLECTION_LIMITS}
    if _has_sensitive_key(document):
        # 凭据语义优先于 exact-shape 错误，额外顶层 apiKey/clientKey 也必须返回固定敏感码。
        raise CliFailure("gatewayctl.sensitive_input_rejected", 2)
    if set(document) != expected or document.get("schema_version") != SCHEMA_VERSION:
        raise CliFailure("gatewayctl.input_invalid", 2)
    # 来源租户仅是迁移元数据，也必须限制长度和控制字符以免污染报告与哈希。
    if not _is_safe_text(document.get("source_tenant_id"), maximum=160):
        raise CliFailure("gatewayctl.input_invalid", 2)
    exported_at = document.get("exported_at")
    if not _is_safe_text(exported_at, maximum=64):
        raise CliFailure("gatewayctl.input_invalid", 2)
    try:
        parsed_time = datetime.fromisoformat(str(exported_at).replace("Z", "+00:00"))
    # 无法解析的来源时间不回显原值，避免攻击载荷通过异常消息进入日志。
    except ValueError as exc:
        raise CliFailure("gatewayctl.input_invalid", 2) from exc
    if parsed_time.tzinfo is None:
        raise CliFailure("gatewayctl.input_invalid", 2)
    total = 0
    for collection, maximum in COLLECTION_LIMITS.items():
        rows = document.get(collection)
        if not isinstance(rows, list) or len(rows) > maximum:
            raise CliFailure("gatewayctl.input_invalid", 2)
        total += len(rows)
    if total > MAX_TOTAL_ROWS:
        raise CliFailure("gatewayctl.input_invalid", 2)


def _prepare_output_path(path_value: str, *, allow_existing: bool, exit_code: int) -> Path:
    """验证输出目标与父目录都是普通本地路径，不跟随链接或共享路径。"""

    path = Path(path_value)
    if not path.name or path_value == "-":
        raise CliFailure("gatewayctl.output_invalid", exit_code)
    absolute = Path(os.path.abspath(os.fspath(path)))
    if os.name == "nt" and str(absolute).startswith("\\\\"):
        raise CliFailure("gatewayctl.output_invalid", exit_code)
    _ensure_no_link_components(absolute, include_leaf=False, exit_code=exit_code)
    try:
        parent = absolute.parent.lstat()
    # 父目录状态无法证明时禁止创建文件，避免写入位置被并发替换或越界。
    except OSError as exc:
        raise CliFailure("gatewayctl.output_invalid", exit_code) from exc
    if not stat.S_ISDIR(parent.st_mode) or _is_reparse_point(parent):
        raise CliFailure("gatewayctl.output_invalid", exit_code)
    try:
        target = absolute.lstat()
    # 目标不存在是独占创建的正常前置状态，不应被转换成用户可见错误。
    except FileNotFoundError:
        target = None
    # 其他元数据异常无法证明目标安全，必须在任何网络或写入动作前失败关闭。
    except OSError as exc:
        raise CliFailure("gatewayctl.output_invalid", exit_code) from exc
    if target is not None:
        if stat.S_ISLNK(target.st_mode) or _is_reparse_point(target) or not stat.S_ISREG(target.st_mode):
            raise CliFailure("gatewayctl.output_invalid", exit_code)
        if not allow_existing:
            raise CliFailure("gatewayctl.output_exists", exit_code)
    return absolute


def _write_atomic_export(path: Path, payload: dict[str, Any], *, force: bool) -> None:
    """在目标同目录写入并 fsync，再以 replace 或无覆盖 hard-link 原子发布导出文件。"""

    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".part",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        body = _json_bytes(payload) + b"\n"
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if force:
            current = path.lstat() if path.exists() else None
            # 覆盖前再次验证目标仍是普通文件，防止预检后被替换为链接或特殊节点。
            if current is not None and (
                stat.S_ISLNK(current.st_mode)
                or _is_reparse_point(current)
                or not stat.S_ISREG(current.st_mode)
            ):
                # 目标类型已经变化时停止覆盖，绝不能跟随新链接发布配置文件。
                raise OSError
            os.replace(temporary_path, path)
            temporary_path = None
        else:
            # hard-link 只在目标仍不存在时成功，避免预检到发布之间的竞态静默覆盖用户文件。
            os.link(temporary_path, path)
            temporary_path.unlink()
            temporary_path = None
        _fsync_directory(path.parent)
    # 编码或原子发布失败时只暴露稳定错误码，并由 finally 清除同目录中间文件。
    except (OSError, TypeError, ValueError) as exc:
        raise CliFailure("gatewayctl.export_write_failed", 2) from exc
    finally:
        if descriptor >= 0:
            # 清理临时描述符的失败不能覆盖原始写入结果，也不得暴露底层文件信息。
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            # 未发布的中间文件必须尽力删除，删除失败也不能把真实路径写入错误流。
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _fsync_directory(path: Path) -> None:
    """在 POSIX 刷新目录项；Windows 不支持目录 fsync 时由文件 flush 与原子操作保证。"""

    if os.name == "nt":
        return
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    # 某些文件系统不支持目录刷新，文件本体已持久化时允许兼容性降级。
    except OSError:
        # 部分文件系统不支持目录 fsync；文件本身已经 fsync，不能因能力差异泄漏路径或异常。
        return
    finally:
        if descriptor >= 0:
            # 目录句柄清理属于尽力操作，不能改变已经完成的原子发布结果。
            try:
                os.close(descriptor)
            except OSError:
                pass


def _reserve_secret_file(path_value: str) -> SecretFileReservation:
    """独占创建一次性密钥文件，并在 POSIX/Windows 收紧可读主体后保持句柄打开。"""

    path = _prepare_output_path(path_value, allow_existing=False, exit_code=6)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        result = os.fstat(descriptor)
        # 独占创建后仍要验证真实句柄类型，阻断文件系统竞态或特殊节点替换。
        if not stat.S_ISREG(result.st_mode) or _is_reparse_point(result):
            raise OSError
        # Windows 必须成功写入保护 DACL，否则一次性明文密钥绝不能交付到该文件。
        if os.name == "nt":
            if not _harden_windows_secret_file(path):
                raise OSError
        else:
            os.fchmod(descriptor, 0o600)
            hardened = os.fstat(descriptor)
            # POSIX 权限必须精确为当前用户读写，不能依赖调用者不确定的 umask。
            if stat.S_IMODE(hardened.st_mode) != 0o600:
                raise OSError
            # 所有者不一致会让密钥交付对象不可证明，因此在写入明文前立即拒绝。
            if hasattr(os, "geteuid") and hardened.st_uid != os.geteuid():
                raise OSError
        return SecretFileReservation(path=path, descriptor=descriptor)
    # 创建或加固失败必须关闭句柄并删除空文件，不能继续发起不可回滚的 apply。
    except OSError as exc:
        if descriptor >= 0:
            # 失败清理不得覆盖固定退出码，也不能把操作系统错误文本带入 stderr。
            try:
                os.close(descriptor)
            except OSError:
                pass
        # 已创建但未加固的文件必须立即移除，避免留下可被其他主体观察的交付位置。
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise CliFailure("gatewayctl.generated_keys_open_failed", 6) from exc


def _harden_windows_secret_file(path: Path) -> bool:
    """用当前用户、SYSTEM 与 Administrators 的保护 DACL 收紧 Windows 密钥文件。"""

    if os.name != "nt":
        return True
    try:
        sid = _current_windows_user_sid()
        if not sid:
            return False
        # 保护 DACL 禁止继续继承宽松目录权限；三类主体覆盖当前用户运维与系统备份需求。
        sddl = f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)"
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        descriptor = ctypes.c_void_p()
        convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        convert.restype = ctypes.c_int
        if not convert(sddl, 1, ctypes.byref(descriptor), None):
            return False
        try:
            set_security = advapi32.SetFileSecurityW
            set_security.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]
            set_security.restype = ctypes.c_int
            security_information = 0x00000004 | 0x80000000
            return bool(set_security(str(path), security_information, descriptor))
        finally:
            local_free = kernel32.LocalFree
            local_free.argtypes = [ctypes.c_void_p]
            local_free.restype = ctypes.c_void_p
            local_free(descriptor)
    # Win32 能力缺失或调用失败时不能假定 ACL 已安全，调用方将删除文件并返回固定错误。
    except (AttributeError, OSError, ValueError):
        return False


def _current_windows_user_sid() -> str | None:
    """通过进程 Token 取得当前 Windows 用户 SID 字符串，不调用 shell 或外部命令。"""

    if os.name != "nt":
        return None

    class _SidAndAttributes(ctypes.Structure):
        """映射 Windows 用户安全标识及其属性，供令牌缓冲区按原生布局读取。"""

        _fields_ = [("sid", ctypes.c_void_p), ("attributes", ctypes.c_uint32)]

    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = ctypes.c_void_p
        open_process_token = advapi32.OpenProcessToken
        open_process_token.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        open_process_token.restype = ctypes.c_int
        get_token_information = advapi32.GetTokenInformation
        get_token_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_token_information.restype = ctypes.c_int
        convert_sid = advapi32.ConvertSidToStringSidW
        convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        convert_sid.restype = ctypes.c_int
        local_free = kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        token = ctypes.c_void_p()
        # 只申请查询权限读取当前进程用户，避免 CLI 获得任何令牌修改或模拟能力。
        if not open_process_token(get_current_process(), 0x0008, ctypes.byref(token)):
            return None
        try:
            required = ctypes.c_uint32()
            get_token_information(token, 1, None, 0, ctypes.byref(required))
            # 无法取得可信缓冲区长度时停止 ACL 构造，绝不能退回宽松继承权限。
            if required.value == 0:
                return None
            buffer = ctypes.create_string_buffer(required.value)
            # 第二次查询必须完整取得当前用户结构，否则 SID 来源无法证明且文件不得写密钥。
            if not get_token_information(
                token,
                1,
                buffer,
                required.value,
                ctypes.byref(required),
            ):
                return None
            token_user = ctypes.cast(buffer, ctypes.POINTER(_SidAndAttributes)).contents
            sid_text = ctypes.c_wchar_p()
            # SID 字符串转换失败时不拼接 SDDL，防止生成语义错误或意外放宽的访问控制表。
            if not convert_sid(token_user.sid, ctypes.byref(sid_text)):
                return None
            try:
                return str(sid_text.value or "") or None
            finally:
                local_free(ctypes.cast(sid_text, ctypes.c_void_p))
        finally:
            close_handle(token)
    # 原生接口布局或系统调用异常统一返回不可证明，调用方会安全删除预留文件。
    except (AttributeError, OSError, ValueError):
        return None


def _exact_object(value: object, fields: frozenset[str]) -> dict[str, Any] | None:
    """仅当值是字段集合逐项相等的 JSON 对象时返回该对象。"""

    if not isinstance(value, dict) or set(value) != fields:
        return None
    return value


def _nullable_text(value: object, *, maximum: int = MAX_JSON_STRING_LENGTH) -> bool:
    """校验允许空值的公开文本字段，同时执行长度和控制字符安全边界。"""

    return value is None or _is_safe_text(value, maximum=maximum)


def _finite_number(value: object) -> bool:
    """校验非负有限 JSON 数字，并排除 bool 类型混淆。"""

    return (
        type(value) in {int, float}
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _validate_export_row(collection: str, value: object) -> bool:
    """按 ConfigExport v1 的每类精确字段与基础类型验证一行业务白名单。"""

    row = _exact_object(value, EXPORT_ROW_FIELDS[collection])
    if row is None:
        return False
    # Provider 行只接受可迁移元数据白名单，凭据、健康错误和探测状态不会进入导出。
    if collection == "providers":
        return all(
            _is_safe_text(row[field])
            for field in ("provider_code", "name", "provider_type", "base_url", "auth_type", "status")
        ) and all(_nullable_text(row[field]) for field in ("description", "maintenance_note"))
    if collection == "models":
        capabilities = row["capabilities"]
        return (
            all(
                _is_safe_text(row[field])
                for field in (
                    "internal_model_id",
                    "provider_code",
                    "provider_model_id",
                    "currency",
                    "status",
                )
            )
            and all(
                _nullable_text(row[field])
                for field in ("display_name", "price_schema_version", "description")
            )
            and isinstance(capabilities, dict)
            and all(_is_safe_text(key, maximum=80) and type(flag) is bool for key, flag in capabilities.items())
            and _plain_int(row["context_window"], maximum=100_000_000)
            and _finite_number(row["input_price_per_1k"])
            and _finite_number(row["output_price_per_1k"])
            and _finite_number(row["cache_price_per_1k"])
        )
    if collection == "aliases":
        quality = _exact_object(row["quality_cost_config"], QUALITY_FIELDS)
        conditions = row["fallback_conditions"]
        return (
            all(
                _is_safe_text(row[field])
                for field in (
                    "alias",
                    "environment",
                    "default_internal_model_id",
                    "routing_mode",
                    "status",
                )
            )
            and _nullable_text(row["fallback_internal_model_id"])
            and _nullable_text(row["description"])
            and isinstance(conditions, list)
            and len(conditions) <= 16
            and all(_is_safe_text(condition, maximum=120) for condition in conditions)
            and quality is not None
            and all(_plain_int(quality[field], maximum=100_000_000) for field in QUALITY_FIELDS)
        )
    return (
        all(_is_safe_text(row[field]) for field in ("name", "environment", "status"))
        and all(
            _nullable_text(row[field])
            for field in ("description", "project_code", "application_code", "rate_limit_status")
        )
        and all(
            row[field] is None or _plain_int(row[field], minimum=1, maximum=100_000_000)
            for field in ("rpm_limit", "tpm_limit", "concurrency_limit")
        )
    )


def _validate_config_export(value: object) -> dict[str, Any]:
    """严格验证服务端 ConfigExport，确保凭据字段和协议扩展不能落盘或写 stdout。"""

    fields = frozenset({"schema_version", "source_tenant_id", "exported_at", *COLLECTION_LIMITS})
    document = _exact_object(value, fields)
    # 顶层扩展字段或 schema 漂移可能夹带未知数据，客户端必须在落盘前失败关闭。
    if document is None or document.get("schema_version") != SCHEMA_VERSION:
        raise CliFailure("gatewayctl.response_invalid", 5)
    # 服务端响应也不被默认信任，任何凭据语义或不安全租户文本都禁止进入文件。
    if _has_sensitive_key(document) or not _is_safe_text(document.get("source_tenant_id"), maximum=160):
        raise CliFailure("gatewayctl.response_invalid", 5)
    exported_at = document.get("exported_at")
    if not _is_safe_text(exported_at, maximum=64):
        raise CliFailure("gatewayctl.response_invalid", 5)
    try:
        parsed = datetime.fromisoformat(str(exported_at).replace("Z", "+00:00"))
    # 服务端时间解析失败同样不能回显原值，统一折叠为响应协议错误。
    except ValueError as exc:
        raise CliFailure("gatewayctl.response_invalid", 5) from exc
    if parsed.tzinfo is None:
        raise CliFailure("gatewayctl.response_invalid", 5)
    total = 0
    for collection, maximum in COLLECTION_LIMITS.items():
        rows = document.get(collection)
        if (
            not isinstance(rows, list)
            or len(rows) > maximum
            or not all(_validate_export_row(collection, row) for row in rows)
        ):
            raise CliFailure("gatewayctl.response_invalid", 5)
        total += len(rows)
    if total > MAX_TOTAL_ROWS or _unsafe_json_structure(document):
        raise CliFailure("gatewayctl.response_invalid", 5)
    return document


def _validate_export_response(response: HttpResponse, request_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """验证导出状态、响应信封、链路标识、事实标识与清单摘要的完整协议合同。"""

    envelope = _exact_object(
        response.payload,
        frozenset(
            {
                "config_export",
                "config_export_id",
                "manifest_sha256",
                "request_id",
                "trace_id",
            }
        ),
    )
    # 状态、字段或链路不一致时不能采信正文，避免把串租户或伪造响应保存为配置。
    if (
        response.status != 200
        or envelope is None
        or envelope.get("request_id") != request_id
        or not _is_safe_chain_id(envelope.get("request_id"))
        or not _is_safe_chain_id(envelope.get("trace_id"))
        or not _is_safe_text(envelope.get("config_export_id"), maximum=128)
        or type(envelope.get("manifest_sha256")) is not str
        or SHA256_PATTERN.fullmatch(envelope["manifest_sha256"]) is None
    ):
        # 信封校验失败后不再读取内部清单，防止错配请求或异常字段继续传播。
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    try:
        config_export = _validate_config_export(envelope["config_export"])
    # 内层清单错误不得丢失当前请求标识，也不得暴露服务端返回的异常细节。
    except CliFailure as exc:
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id) from exc
    return envelope, config_export


def _validate_report_error(value: object) -> bool:
    """验证一条有界行错误只包含对象、行号、白名单字段名和稳定错误码。"""

    item = _exact_object(value, frozenset({"object_type", "row", "field", "code"}))
    return bool(
        item is not None
        and item.get("object_type") in {"document", "provider", "model", "alias", "virtual_key"}
        and _plain_int(item.get("row"), maximum=MAX_TOTAL_ROWS)
        and item.get("field") in REPORT_ERROR_FIELDS
        and type(item.get("code")) is str
        and SAFE_CODE_PATTERN.fullmatch(item["code"]) is not None
    )


def _validate_import_report(value: object, request_id: str) -> dict[str, Any]:
    """验证 ImportReport 的状态、计数、错误边界和链路一致性。"""

    report = _exact_object(value, REPORT_FIELDS)
    # 报告字段必须精确匹配冻结合同，未知扩展可能携带攻击者控制文本或秘密。
    if report is None:
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    counts = _exact_object(report.get("counts"), COUNT_FIELDS)
    errors = report.get("errors")
    # 计数、错误列表和链路标识必须整体一致，任一异常都按不可信响应失败关闭。
    if (
        report.get("status") not in {"invalid", "validated", "applied", "replayed"}
        or type(report.get("valid")) is not bool
        or type(report.get("applied")) is not bool
        or report.get("schema_version") != SCHEMA_VERSION
        or type(report.get("manifest_sha256")) is not str
        or SHA256_PATTERN.fullmatch(report["manifest_sha256"]) is None
        or counts is None
        or not all(_plain_int(counts[field], maximum=MAX_TOTAL_ROWS) for field in COUNT_FIELDS)
        or not _plain_int(report.get("error_count"), maximum=MAX_JSON_NODES)
        or type(report.get("errors_truncated")) is not bool
        or not isinstance(errors, list)
        or len(errors) > MAX_RESPONSE_ERRORS
        or not all(_validate_report_error(item) for item in errors)
        or report.get("error_count", 0) < len(errors)
        or report.get("request_id") != request_id
        or not _is_safe_chain_id(report.get("trace_id"))
        or not _nullable_text(report.get("config_import_id"), maximum=128)
    ):
        # 报告任一不变量失败都拒绝整个响应，不能输出部分可信的迁移结论。
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    status = report["status"]
    valid_state = {
        "invalid": (False, False),
        "validated": (True, False),
        "applied": (True, True),
        "replayed": (True, True),
    }[status]
    # valid/applied 布尔值必须由状态唯一决定，禁止接受互相矛盾的迁移结论。
    if (report["valid"], report["applied"]) != valid_state:
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    # 无效状态必须提供至少一条安全错误，防止 CLI 展示无法解释的失败结果。
    if status == "invalid" and report["error_count"] == 0:
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    # 非无效状态不得夹带行级错误，避免成功摘要与失败明细产生歧义。
    if status != "invalid" and report["error_count"] != 0:
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    expected_error_items = min(report["error_count"], MAX_RESPONSE_ERRORS)
    if (
        len(errors) != expected_error_items
        or report["errors_truncated"] != (report["error_count"] > MAX_RESPONSE_ERRORS)
    ):
        # 服务端只能在固定 200 条边界截断；数量或标记不一致会隐藏真实校验结果，必须整体拒绝。
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    # 已提交或重放必须绑定持久导入事实，才能支持后续排障和密钥交付确认。
    if status in {"applied", "replayed"} and report["config_import_id"] is None:
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    # 预检阶段不允许伪造持久导入事实，避免把尚未提交的结果误认为已经落库。
    if status in {"invalid", "validated"} and report["config_import_id"] is not None:
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    return report


def _validate_generated_key(value: object) -> bool:
    """验证仅允许写入私有文件的一条新 Virtual Key，不把明文复制到普通 DTO。"""

    item = _exact_object(
        value,
        frozenset({"virtual_key_id", "name", "environment", "key_prefix", "plain_value"}),
    )
    return bool(
        item is not None
        and _is_safe_text(item.get("virtual_key_id"), maximum=128)
        and _is_safe_text(item.get("name"), maximum=80)
        and item.get("environment") in {"dev", "test", "prod"}
        and _is_safe_text(item.get("key_prefix"), maximum=128)
        and _is_safe_text(item.get("plain_value"), maximum=MAX_TOKEN_LENGTH)
    )


def _validate_import_response(
    response: HttpResponse,
    request_id: str,
    *,
    phase: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """验证 validate/apply envelope，并确保明文 Key 只在 apply 成功响应中出现。"""

    envelope = _exact_object(
        response.payload,
        frozenset({"report", "generated_virtual_keys", "request_id", "trace_id"}),
    )
    # 外层信封必须回显本次请求标识，阻断错配响应或跨请求结果被当前命令接受。
    if (
        envelope is None
        or envelope.get("request_id") != request_id
        or not _is_safe_chain_id(envelope.get("trace_id"))
    ):
        # 信封缺失或链路错配时不访问报告与密钥数组，避免接受跨请求数据。
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    report = _validate_import_report(envelope["report"], request_id)
    # 信封和报告必须共享同一追踪标识，避免审计链在客户端摘要中被拆分。
    if envelope["trace_id"] != report["trace_id"]:
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    generated = envelope.get("generated_virtual_keys")
    # 一次性密钥逐字段验证后才可进入私有文件，任意协议扩展都必须拒绝。
    if not isinstance(generated, list) or not all(_validate_generated_key(item) for item in generated):
        raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    # 预检响应不得包含任何明文密钥，避免未提交阶段提前产生不可追踪的凭据。
    if phase == "validate":
        if response.status != 200 or report["status"] not in {"validated", "invalid"} or generated:
            raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    else:
        expected_status = 201 if report["status"] == "applied" else 200
        # 应用阶段只接受提交、精确重放或业务无效三种状态，并严格匹配 HTTP 语义。
        if response.status != expected_status or report["status"] not in {"applied", "replayed", "invalid"}:
            raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
        # 重放和无效结果绝不能再次返回明文，防止幂等重试扩大一次性密钥暴露面。
        if report["status"] != "applied" and generated:
            raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
        # 新生成密钥条数必须与安全计数一致，否则不能确认文件交付内容完整。
        if (
            report["status"] == "applied"
            and len(generated) != report["counts"]["generated_virtual_keys"]
        ):
            # 数量不一致意味着密钥文件可能不完整，必须在任何明文提交前拒绝。
            raise CliFailure("gatewayctl.response_invalid", 5, request_id=request_id)
    return report, generated, str(envelope["trace_id"])


def _read_token(token_stdin: bool, stdin: TextIO, environ: Mapping[str, str]) -> str:
    """只从显式标准输入或 METAHUB_CONTROL_TOKEN 读取原始 Session Token。"""

    # 只有显式开关才读取标准输入，避免管道内容在默认路径中被误当成认证令牌。
    if token_stdin:
        value = stdin.readline(MAX_TOKEN_LENGTH + 2)
        value = value.rstrip("\r\n")
    else:
        value = environ.get("METAHUB_CONTROL_TOKEN", "")
    if (
        not value
        or len(value) > MAX_TOKEN_LENGTH
        or value.lower().startswith("bearer ")
        or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        # 缺失、超长或带空白的令牌统一失败且不回显原值，避免进入命令日志。
        raise CliFailure("gatewayctl.token_unavailable", 2)
    return value


def _build_parser() -> _SafeArgumentParser:
    """构造不提供 tenant/--token 的两级 gatewayctl 参数合同。"""

    parser = _SafeArgumentParser(prog="gatewayctl", description="MetaHub configuration transfer CLI")
    command_parsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SafeArgumentParser,
    )
    config_parser = command_parsers.add_parser("config")
    operations = config_parser.add_subparsers(
        dest="operation",
        required=True,
        parser_class=_SafeArgumentParser,
    )

    export_parser = operations.add_parser("export")
    export_parser.add_argument("--output", default="-")
    export_parser.add_argument("--force", action="store_true")
    export_parser.add_argument("--control-url", default=DEFAULT_CONTROL_URL)
    export_parser.add_argument("--token-stdin", action="store_true")

    import_parser = operations.add_parser("import")
    import_parser.add_argument("--input", required=True)
    import_parser.add_argument("--generated-keys-output")
    import_parser.add_argument("--dry-run", action="store_true")
    import_parser.add_argument("--control-url", default=DEFAULT_CONTROL_URL)
    import_parser.add_argument("--token-stdin", action="store_true")
    return parser


def _emit_json(stream: TextIO, value: dict[str, Any]) -> None:
    """向指定流写一行紧凑 JSON，不使用 repr 或异常文本。"""

    stream.write(_json_bytes(value).decode("utf-8") + "\n")
    stream.flush()


def _error_payload(failure: CliFailure, fallback_request_id: str) -> dict[str, Any]:
    """构造字段固定且不含路径、URL、正文或秘密的 stderr 错误对象。"""

    request_id = failure.request_id or fallback_request_id
    return {
        "status": "error",
        "code": failure.code,
        "request_id": request_id,
        "trace_id": failure.trace_id or None,
        "config_import_id": failure.config_import_id,
    }


def _run_export(
    arguments: argparse.Namespace,
    client: ControlClient,
    stdout: TextIO,
) -> int:
    """执行租户配置导出，并选择 stdout ConfigExport 或原子文件加安全摘要。"""

    if arguments.output == "-" and arguments.force:
        raise CliFailure("gatewayctl.argument_invalid", 2)
    output_path = None
    if arguments.output != "-":
        output_path = _prepare_output_path(
            arguments.output,
            allow_existing=bool(arguments.force),
            exit_code=2,
        )
    request_id = _new_request_id()
    response = client.post("config/export", {}, request_id)
    envelope, config_export = _validate_export_response(response, request_id)
    if output_path is None:
        # stdout 必须能直接作为下一次 import 输入，因此只输出 ConfigExport 本体。
        _emit_json(stdout, config_export)
    else:
        _write_atomic_export(output_path, config_export, force=bool(arguments.force))
        _emit_json(
            stdout,
            {
                "status": "exported",
                "config_export_id": envelope["config_export_id"],
                "manifest_sha256": envelope["manifest_sha256"],
                "request_id": envelope["request_id"],
                "trace_id": envelope["trace_id"],
            },
        )
    return 0


def _validation_summary(report: dict[str, Any], validation_request_id: str) -> dict[str, Any]:
    """构造 dry-run 或无效预检的安全单行摘要。"""

    return {
        "status": report["status"],
        "report": report,
        "validation_request_id": validation_request_id,
        "request_id": report["request_id"],
        "trace_id": report["trace_id"],
    }


def _apply_summary(report: dict[str, Any], validation_request_id: str) -> dict[str, Any]:
    """构造不含一次性密钥正文的应用或重放摘要，仅公开计数与安全链路标识。"""

    return {
        "status": report["status"],
        "report": report,
        "generated_virtual_key_count": report["counts"]["generated_virtual_keys"],
        "validation_request_id": validation_request_id,
        "request_id": report["request_id"],
        "trace_id": report["trace_id"],
    }


def _may_generate_active_virtual_key(document: dict[str, Any]) -> bool:
    """判断清单是否包含可能在 apply 快照中新建凭据的 active Virtual Key。

    服务端预检成功后行结构已经过严格校验；状态判断仍复用服务端 ``strip().lower()`` 语义，
    避免合法大小写或外侧空白在 validate 与 apply 之间的竞态中绕过私密文件预留。
    """

    rows = document.get("virtual_keys", [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        # 导入服务对缺失 status 使用 active 默认值；CLI 必须保持相同语义才能提前预留秘密文件。
        status = row.get("status", "active")
        if type(status) is str and status.strip().lower() == "active":
            return True
    return False


def _run_import(
    arguments: argparse.Namespace,
    client: ControlClient,
    stdout: TextIO,
) -> int:
    """执行本地门禁、服务端预检、密钥文件预留和原子 apply 两阶段工作流。"""

    if arguments.dry_run and arguments.generated_keys_output:
        raise CliFailure("gatewayctl.argument_invalid", 2)
    document = _read_input_document(arguments.input)
    validation_request_id = _new_request_id()
    validation_response = client.post("config/import/validate", document, validation_request_id)
    validation_report, _, _ = _validate_import_response(
        validation_response,
        validation_request_id,
        phase="validate",
    )
    if validation_report["status"] == "invalid":
        _emit_json(stdout, _validation_summary(validation_report, validation_request_id))
        return 4
    if arguments.dry_run:
        _emit_json(stdout, _validation_summary(validation_report, validation_request_id))
        return 0

    expected_keys = validation_report["counts"]["generated_virtual_keys"]
    requires_secret_output = expected_keys > 0 or _may_generate_active_virtual_key(document)
    # 预检与 apply 是不同快照，潜在活跃 Key 必须在提交前已有安全文件句柄承接明文。
    if requires_secret_output and not arguments.generated_keys_output:
        raise CliFailure("gatewayctl.generated_keys_output_required", 2)
    reservation: SecretFileReservation | None = None
    apply_request_id = _new_request_id()
    try:
        if arguments.generated_keys_output:
            reservation = _reserve_secret_file(arguments.generated_keys_output)
        apply_response = client.post("config/import", document, apply_request_id)
        report, generated_keys, _ = _validate_import_response(
            apply_response,
            apply_request_id,
            phase="apply",
        )
        if report["status"] == "invalid":
            _emit_json(stdout, _apply_summary(report, validation_request_id))
            return 4
        if generated_keys and reservation is None:
            raise CliFailure(
                "gatewayctl.generated_keys_output_required",
                6,
                request_id=apply_request_id,
                trace_id=report["trace_id"],
                config_import_id=report["config_import_id"],
            )
        if reservation is not None and generated_keys:
            # 只有实际收到首次明文才提交文件；unchanged/replayed 会删除预留占位，避免制造空秘密文件。
            reservation.commit(
                {
                    "config_import_id": report["config_import_id"],
                    "request_id": report["request_id"],
                    "generated_virtual_keys": generated_keys,
                },
                config_import_id=report["config_import_id"],
            )
        _emit_json(stdout, _apply_summary(report, validation_request_id))
        return 0
    finally:
        if reservation is not None and not reservation.committed:
            reservation.abort()


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[str, str], ControlClient] = ControlClient,
) -> int:
    """执行 gatewayctl 并返回稳定退出码，便于包装脚本和专项测试直接调用。"""

    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    environment = environ if environ is not None else os.environ
    fallback_request_id = _new_request_id()
    try:
        arguments = _build_parser().parse_args(list(argv) if argv is not None else None)
        token = _read_token(bool(arguments.token_stdin), input_stream, environment)
        client = client_factory(arguments.control_url, token)
        if arguments.operation == "export":
            return _run_export(arguments, client, output_stream)
        return _run_import(arguments, client, output_stream)
    except CliFailure as failure:
        _emit_json(error_stream, _error_payload(failure, fallback_request_id))
        return failure.exit_code
    # 未分类运行时错误可能携带路径或参数，最终边界必须固定脱敏且保留本地请求标识。
    except Exception:
        # Exception 总边界保留 KeyboardInterrupt/SystemExit，同时阻止未知库异常打印路径、正文或堆栈。
        failure = CliFailure("gatewayctl.failed", 5, request_id=fallback_request_id)
        _emit_json(error_stream, _error_payload(failure, fallback_request_id))
        return failure.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """模块入口：运行 CLI 并把整数退出码交给 PowerShell 或 shell。"""

    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
