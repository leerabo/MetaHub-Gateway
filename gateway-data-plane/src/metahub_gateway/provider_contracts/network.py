"""PRD-064 合同探测与内部上报共用的网络边界校验。

模块职责：把显式服务 URL 规范为单一 origin，并判断响应是否仍属于该 origin。
维护边界：这里只接受 Gateway 或 Control 根地址，不解析 Provider Endpoint，也不执行 DNS 探测。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


# 受信 origin/tenant 由部署环境独立下发，不能放进 probe JSON 或 CLI 参数由探测目标自行证明。
TRUSTED_GATEWAY_ORIGIN_ENV = "METAHUB_CONTRACT_TRUSTED_GATEWAY_ORIGIN"
TRUSTED_CONTROL_ORIGIN_ENV = "METAHUB_CONTRACT_TRUSTED_CONTROL_ORIGIN"
TRUSTED_TENANT_ID_ENV = "METAHUB_CONTRACT_TRUSTED_TENANT_ID"

# request_id/trace_id 会进入 health 日志；发送凭据前必须识别完整 Secret 及其至少 16 字符片段。
_UNSAFE_IDENTIFIER_CHARACTER = re.compile(r"[^A-Za-z0-9_.:-]")
_SENSITIVE_FRAGMENT_LENGTH = 16


class NetworkBoundaryError(ValueError):
    """表示服务 origin 违反 HTTPS、loopback 或 URL 结构边界。

    异常正文固定，不保存调用方 URL；CLI 可据此返回安全配置错误而不泄露内部域名。
    """

    def __init__(self) -> None:
        """构造固定网络边界错误，不接收不可信 URL 文本。"""

        super().__init__("adapter.contract_network_boundary_invalid")


@dataclass(frozen=True)
class ServiceOrigin:
    """保存已经规范化的服务根地址与同源比较元组。

    ``base_url`` 不含路径、query、fragment 或用户信息；host 和有效端口仅用于响应同源验证。
    """

    base_url: str
    scheme: str
    host: str
    port: int


def normalize_service_origin(
    value: object,
    *,
    allow_loopback_http: bool,
) -> ServiceOrigin:
    """校验并规范 Gateway/Control 根地址。

    参数：URL 值与是否显式允许 loopback HTTP。返回：不可变同源对象。
    约束：默认仅 HTTPS；HTTP 必须同时满足显式开关和真实 loopback 主机。
    """

    # URL 必须先满足有界字符串合同，非文本不能交给解析器产生自由异常。
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise NetworkBoundaryError
    try:
        parsed = urlsplit(value)
        port = parsed.port
    # 非法端口可能把原 URL 拼入 ValueError，必须切断后转成固定边界错误。
    except ValueError:
        raise NetworkBoundaryError from None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if (
        scheme not in {"https", "http"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        # 服务配置只承认根 origin，路径、身份和 query 都可能把请求导向非预期入口。
        raise NetworkBoundaryError
    try:
        host.encode("ascii")
    # IDN 必须由部署配置先显式转成 ASCII；运行时不做隐式域名重写。
    except UnicodeEncodeError:
        raise NetworkBoundaryError from None
    if scheme == "http" and not (allow_loopback_http and _is_loopback(host)):
        # 明文 HTTP 只服务本机验收，任何局域网或公网地址都必须使用 HTTPS。
        raise NetworkBoundaryError
    effective_port = port or (443 if scheme == "https" else 80)
    host_text = f"[{host}]" if ":" in host else host
    explicit_port = f":{port}" if port is not None else ""
    return ServiceOrigin(
        base_url=f"{scheme}://{host_text}{explicit_port}",
        scheme=scheme,
        host=host,
        port=effective_port,
    )


def response_is_same_origin(origin: ServiceOrigin, response_url: object) -> bool:
    """判断响应最终 URL 是否与配置 origin 完全同源。

    参数：可信配置 origin 与 httpx 返回的 URL 字符串化对象。返回：scheme/host/有效端口均相同。
    解析失败返回假，不抛出含响应 URL 的自由异常。
    """

    try:
        parsed = urlsplit(str(response_url))
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    # 畸形响应 URL 不能参与同源比较，也不能把自由文本带入异常链。
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == origin.scheme
        and (parsed.hostname or "").lower() == origin.host
        and port == origin.port
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def service_origins_match(left: ServiceOrigin, right: ServiceOrigin) -> bool:
    """比较两个已规范化服务地址是否属于完全相同的 origin。

    参数：两个通过 ``normalize_service_origin`` 校验的地址。返回：协议、主机和有效端口是否一致。
    这里不比较 ``base_url`` 文本，避免显式默认端口与省略默认端口被误判为不同服务。
    """

    return (
        left.scheme == right.scheme
        and left.host == right.host
        and left.port == right.port
    )


def identifier_exposes_secret(identifier: object, secret: object) -> bool:
    """判断链路标识是否会暴露凭据全文或可复用片段。

    参数：即将写入 health/业务请求日志的链路标识，以及仅在内存中持有的 Secret。
    返回：标识包含 Secret 全文、规范化全文或任一至少 16 字符公共片段时为真。
    约束：只做有界内存比较，不返回命中的片段，也不把 Secret 放入异常或日志。
    """

    # 非文本输入无法安全执行片段比较，按可能泄漏处理比异常放行更符合凭据边界。
    if not isinstance(identifier, str) or not isinstance(secret, str):
        # 类型漂移不能被当成“没有泄漏”；调用方应在首个网络请求前失败关闭。
        return True
    if not identifier or not secret:
        return True
    normalized_secret = _UNSAFE_IDENTIFIER_CHARACTER.sub("_", secret)
    # 短 Secret 不适用 16 字符窗口，全文包含关系仍必须在窗口判断前覆盖。
    if secret in identifier or normalized_secret in identifier:
        return True
    # 链路标识已有严格长度上限，逐窗口比较不会被超长凭据放大为无界集合。
    if len(identifier) < _SENSITIVE_FRAGMENT_LENGTH:
        return False
    return any(
        identifier[start : start + _SENSITIVE_FRAGMENT_LENGTH] in candidate
        for candidate in (secret, normalized_secret)
        for start in range(len(identifier) - _SENSITIVE_FRAGMENT_LENGTH + 1)
    )


def _is_loopback(host: str) -> bool:
    """识别 localhost 与 IP loopback，不通过 DNS 把普通域名猜测为本机。"""

    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    # 普通域名不是字面量 loopback，不能因为本机 DNS 恰好解析到回环而放宽 HTTP。
    except ValueError:
        return False


__all__ = [
    "NetworkBoundaryError",
    "ServiceOrigin",
    "TRUSTED_CONTROL_ORIGIN_ENV",
    "TRUSTED_GATEWAY_ORIGIN_ENV",
    "TRUSTED_TENANT_ID_ENV",
    "normalize_service_origin",
    "identifier_exposes_secret",
    "response_is_same_origin",
    "service_origins_match",
]
