from __future__ import annotations

import re
from uuid import uuid4

# request_id/trace_id Header 和安全字符规则集中定义，保证调用链路标识可透传且不可注入日志。
REQUEST_ID_HEADER = "X-Request-ID"
TRACE_ID_HEADER = "X-Trace-ID"
_SAFE_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.:-]")
# 连续 16 字符已足以暴露长 Token 的可复用片段；更短碰撞按普通 request_id 处理以降低误报。
_CREDENTIAL_FRAGMENT_MIN_LENGTH = 16


def new_request_id() -> str:
    """生成内部 request_id。

    使用 req_ 前缀便于在日志、错误响应和未来调用日志表中快速识别系统生成的请求 ID。
    """

    return f"req_{uuid4().hex}"


def new_trace_id() -> str:
    """生成内部 trace_id。

    trace_id 表示一条业务调用链，未来可能跨网关、控制面、Provider Adapter 和 Webhook。
    它和 request_id 分离，是为了让一次链路里可以包含多个 HTTP 请求。
    """

    return f"trc_{uuid4().hex}"


def resolve_request_id(
    incoming: str | None,
    *,
    authorization: str | None = None,
    sensitive_values: tuple[str, ...] = (),
) -> str:
    """复用调用方传入的 request_id，缺失时自动生成。

    截断到 128 字符是为了防止调用方传入超长 header 污染日志或响应头。
    """

    # 仅复用不含 Bearer 凭证的外部标识，避免认证秘密借 request_id 进入响应和访问日志。
    if incoming and incoming.strip() and not _exposes_bearer_credential(
        incoming,
        authorization,
        sensitive_values=sensitive_values,
    ):
        return _normalize_external_id(incoming.strip(), fallback_prefix="req")
    # 链路头包含完整或显著部分 Bearer credential 时必须换成内部 ID，禁止 Token 进入日志和响应。
    return new_request_id()


def resolve_trace_id(
    incoming: str | None,
    request_id: str | None = None,
    *,
    authorization: str | None = None,
    sensitive_values: tuple[str, ...] = (),
) -> str:
    """复用调用方 trace_id，缺失时自动生成。

    客户端传入的 trace_id 进入日志和响应头，因此必须做字符过滤和长度限制，避免
    换行、控制字符或超长字符串污染结构化日志。
    """

    # trace_id 会持久化到业务事件，凭证碰撞时必须丢弃外部值并生成内部标识。
    if incoming and incoming.strip() and not _exposes_bearer_credential(
        incoming,
        authorization,
        sensitive_values=sensitive_values,
    ):
        return _normalize_external_id(incoming.strip(), fallback_prefix="trc")
    # 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
    if request_id and request_id.startswith("req_"):
        return f"trc_{request_id[4:]}"
    return new_trace_id()


def _normalize_external_id(value: str, *, fallback_prefix: str) -> str:
    """规范化 共享运行时 的 normalize external id 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    normalized = _SAFE_ID_PATTERN.sub("_", value)[:128]
    if normalized:
        return normalized
    if fallback_prefix == "trc":
        return new_trace_id()
    return new_request_id()


def _exposes_bearer_credential(
    value: str,
    authorization: str | None,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> bool:
    """判断外部链路 ID 是否会泄露任一请求凭据的完整值或显著片段。

    参数：未经规范化的链路 Header、可空 Authorization Header，以及由路由边界识别的
    额外敏感值，例如一次性 Invitation path Token。
    返回：可见链路值包含完整 Authorization、潜在 credential 或其至少 16 字符片段时为真。
    约束：脱敏先于认证且不能依赖 scheme 合法；这里只做内存比较，不记录任何秘密。
    """

    candidate = value.strip()
    # 空链路值最终会由调用方生成内部 ID，无需进入片段扫描。
    if not candidate:
        return False
    potential_secrets = [
        item.strip()
        for item in sensitive_values
        if type(item) is str and item.strip()
    ]
    # Authorization 即使格式非法也可能含可复用凭据，链路 ID 回显前必须参与碰撞检测。
    if type(authorization) is str and authorization.strip():
        normalized_authorization = authorization.strip()
        # 认证失败响应同样会透出链路 ID，因此 Bearer<TAB>、裸 Token 或错误 scheme 后的
        # secret 都必须先脱敏；不能等待认证层决定 Authorization 是否有效。
        authorization_parts = normalized_authorization.split()
        potential_secrets.append(normalized_authorization)
        if len(authorization_parts) > 1:
            # scheme 之后的每个非空字段都按潜在秘密处理，覆盖额外空白和畸形多字段输入。
            potential_secrets.extend(authorization_parts[1:])
    return any(
        _visible_id_exposes_secret(candidate, secret)
        for secret in dict.fromkeys(potential_secrets)
    )


def _visible_id_exposes_secret(candidate: str, secret: str) -> bool:
    """检查一个实际链路候选是否暴露指定 Authorization secret。"""

    # 最终只有规范化后的前 128 字符会进入响应与日志；同时保留 raw 完整值检查，覆盖
    # 短 secret 被包在装饰文本中的场景。
    visible = _SAFE_ID_PATTERN.sub("_", candidate)[:128]
    normalized_secret = _SAFE_ID_PATTERN.sub("_", secret)
    if (
        secret in candidate
        or secret in visible
        or normalized_secret in visible
    ):
        return True
    # 攻击者可能只复制 Token 的前缀、中段或后缀再添加装饰字符；只比较“整个 Header
    # 是否为子串”会漏掉这种情况，因此对实际可见文本的全部 16 字符窗口做重叠检查。
    return _shares_sensitive_fragment(visible, secret) or _shares_sensitive_fragment(
        visible,
        normalized_secret,
    )


def _shares_sensitive_fragment(visible: str, credential: str) -> bool:
    """判断两个文本是否共享至少 16 个连续字符的敏感片段。

    ``visible`` 已被限制为最多 128 字符，因此窗口数量有硬上限；函数不构造或记录
    credential 子串集合，避免长 Authorization Header 额外放大内存占用。
    """

    minimum = _CREDENTIAL_FRAGMENT_MIN_LENGTH
    if len(visible) < minimum or len(credential) < minimum:
        # 短 credential 的完整包含关系已由调用方处理，普通短 ID 不应因零碎字符误判。
        return False
    # 任意更长公共片段必然包含一个长度为 16 的公共窗口，逐窗口检查即可完整覆盖。
    return any(
        visible[start : start + minimum] in credential
        for start in range(len(visible) - minimum + 1)
    )
