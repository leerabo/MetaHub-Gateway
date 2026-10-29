"""将客户端异常转换为不会泄露请求与凭据的固定错误输出。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, TextIO

# 远端错误码与请求标识只允许安全单行字符，防止响应内容造成终端日志注入。
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class SafeFailure:
    """描述允许输出到终端的最小失败信息集合。

    字段只包含固定类别、公开错误码、HTTP 状态、服务端请求标识和可重试标记。
    该对象故意不保存异常消息、请求正文、响应正文、URL 或客户端配置。
    """

    category: str
    code: str
    status: int | None
    request_id: str
    retryable: bool

    def render(self) -> str:
        """把安全字段渲染为稳定的单行终端文本。

        固定键名便于人工排查和日志采集；未知 HTTP 状态显示 ``-``，不会回退到
        原始异常字符串或响应内容。
        """

        status_text = "-" if self.status is None else str(self.status)
        retryable_text = "true" if self.retryable else "false"
        return (
            "请求失败: "
            f"category={self.category} "
            f"code={self.code} "
            f"status={status_text} "
            f"request_id={self.request_id} "
            f"retryable={retryable_text}"
        )


def _safe_token(value: Any, default: str) -> str:
    """仅保留适合单行日志的短标识符，非法值使用安全默认值。

    Gateway 错误码和请求标识来自远端响应，仍需防御换行、控制字符与超长内容，
    防止终端日志注入；函数不会尝试修剪或回显部分非法值。
    """

    # 远端标识必须完整匹配白名单，不能通过截断方式保留潜在注入片段。
    if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value):
        return value
    return default


def safe_request_id(value: Any) -> str:
    """规范化成功或失败响应中的公开 ``request_id``。

    合法标识原样返回；缺失、类型错误或包含日志注入字符时统一返回 ``-``，避免
    为了排查链路而引入新的终端安全风险。
    """

    # 非法 request_id 统一替换为横线，绝不回显部分远端输入。
    return _safe_token(value, "-")


def _gateway_error_code(error: BaseException) -> str:
    """从官方状态异常的公开 ``body`` 属性提取 Gateway 错误码。

    兼容 Gateway 常见的 ``{"error": {"code": ...}}`` 与顶层 ``code`` 结构，
    只读取并校验错误码，不序列化或输出可能包含 Prompt 的完整响应体。
    """

    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return "gateway.request_failed"
    nested_error = body.get("error")
    # Gateway 标准错误体优先从 error.code 提取，其他响应字段一律忽略。
    if isinstance(nested_error, dict):
        # 即使 code 来自标准结构，也必须经过单行字符白名单校验。
        return _safe_token(nested_error.get("code"), "gateway.request_failed")
    # 兼容顶层 code 时仍不输出完整 body，避免其中的请求正文或密钥泄露。
    return _safe_token(body.get("code"), "gateway.request_failed")


def _exception_type(openai_module: Any, name: str) -> type[BaseException] | None:
    """安全获取官方模块公开异常类，支持测试假模块缺少部分类型。

    仅当属性确实是异常类型时返回，防止被错误配置的模块属性传给 ``isinstance``
    触发二次异常。
    """

    candidate = getattr(openai_module, name, None)
    if isinstance(candidate, type) and issubclass(candidate, BaseException):
        return candidate
    return None


def _matches(error: BaseException, openai_module: Any, name: str) -> bool:
    """判断异常是否属于指定官方公开类型。

    假模块未定义某个异常类时返回 ``False``，使其余分类逻辑继续执行，而不是让
    示例的安全错误处理本身失败。
    """

    error_type = _exception_type(openai_module, name)
    return error_type is not None and isinstance(error, error_type)


def failure_from_openai_error(error: BaseException, openai_module: Any) -> SafeFailure:
    """把官方客户端异常映射为不包含原始异常文本的安全失败对象。

    状态错误保留服务端 ``request_id`` 和公开 Gateway 错误码；连接、超时等未获
    响应的错误使用 ``request_id=-``。任何未知异常都降级为固定不可重试错误，
    绝不调用 ``str(error)`` 或 ``repr(error)``。
    """

    # 状态错误已获得 Gateway 响应，可以保留经白名单校验的链路标识和错误码。
    if _matches(error, openai_module, "APIStatusError"):
        status = getattr(error, "status_code", None)
        safe_status = status if isinstance(status, int) else None
        # 只读取官方异常公开的 request_id；原始响应和异常消息不得输出。
        request_id = safe_request_id(getattr(error, "request_id", None))
        # 与官方客户端默认策略对齐：超时、冲突、限流和服务端错误可进行有上限重试。
        retryable = safe_status in {408, 409, 429} or (
            safe_status is not None and safe_status >= 500
        )
        return SafeFailure(
            category="gateway_api",
            code=_gateway_error_code(error),
            status=safe_status,
            request_id=request_id,
            retryable=retryable,
        )
    # 超时没有可靠服务端响应，request_id 必须保持横线并标记可重试。
    if _matches(error, openai_module, "APITimeoutError"):
        return SafeFailure("timeout", "client.timeout", None, "-", True)
    # 连接失败同样没有服务端链路标识，不能从异常文本猜测 request_id。
    if _matches(error, openai_module, "APIConnectionError"):
        return SafeFailure("connection", "client.connection_failed", None, "-", True)
    # 其他官方 API 错误使用固定不可重试分类，禁止输出原始错误内容。
    if _matches(error, openai_module, "APIError"):
        return SafeFailure("client", "client.request_failed", None, "-", False)
    # 未知异常可能包含 Virtual Key、Authorization 或请求正文，只输出固定信息。
    return SafeFailure("client", "client.unexpected_error", None, "-", False)


def local_failure(code: str, *, retryable: bool = False) -> SafeFailure:
    """创建未向 Gateway 发出请求时使用的固定本地失败对象。

    调用方只能传入代码中定义的错误码，不应把异常消息或环境变量值作为 ``code``；
    这里仍执行安全标识校验，非法值会退化为通用本地错误码。
    """

    return SafeFailure(
        category="local",
        code=_safe_token(code, "client.local_error"),
        status=None,
        request_id="-",
        retryable=retryable,
    )


def write_failure(stream: TextIO, failure: SafeFailure) -> None:
    """向指定错误流写入一行经过白名单约束的失败信息。

    使用显式流参数便于 CLI 测试确认敏感信息未进入 stderr；函数不会接受或打印
    原始异常对象。
    """

    print(failure.render(), file=stream)
