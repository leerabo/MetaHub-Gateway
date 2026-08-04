"""从环境变量读取并校验 MetaHub Gateway 示例配置。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

# 这些默认值和上下限共同约束 Gateway 地址、模型选择、超时以及重试成本边界。
DEFAULT_BASE_URL = "http://localhost:8000/v1"
DEFAULT_CHAT_MODEL = "default-chat"
DEFAULT_EMBEDDING_MODEL = "default-embedding"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 300.0
MAX_RETRIES = 10


class ExampleConfigError(ValueError):
    """表示示例启动前发现的安全配置错误。

    异常消息只描述变量名和允许范围，绝不拼接用户输入值，因此调用方可以安全地
    将消息转换为固定终端提示，而不会意外泄露 Virtual Key 或其他环境变量。
    """

    def __init__(self, code: str) -> None:
        """使用代码中定义的公开错误码初始化配置异常。

        异常只保存固定错误码，不保存用户输入；CLI 可以据此指出具体配置项，同时
        不会把 Virtual Key、URL 凭据或非法原值带入 stderr。
        """

        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ExampleConfig:
    """保存构建官方 OpenAI 客户端与选择模型所需的最小配置。

    ``virtual_key`` 只会传给官方客户端的 ``api_key`` 参数；示例中的日志、成功
    输出和错误输出均不得序列化此对象，避免凭据进入终端或日志采集系统。
    """

    base_url: str
    virtual_key: str
    chat_model: str
    embedding_model: str
    timeout_seconds: float
    max_retries: int


def _required_text(environ: Mapping[str, str], name: str) -> str:
    """读取必填文本变量，并在缺失时只报告变量名。

    参数值经过首尾空白清理；错误路径不会回显原值，尤其适合读取 Virtual Key
    这类不能出现在日志中的敏感配置。
    """

    value = environ.get(name, "").strip()
    if not value:
        # Virtual Key 缺失时只指出变量名，不能打印整个环境或输入值。
        raise ExampleConfigError(f"config.{name}_missing")
    return value


def _optional_text(environ: Mapping[str, str], name: str, default: str) -> str:
    """读取可选文本变量，空字符串按给定默认值处理。

    该函数用于模型别名等非敏感配置，但仍不在异常中拼接原始输入，保持各配置
    分支一致的安全输出行为。
    """

    value = environ.get(name, "").strip()
    return value or default


def _parse_timeout(environ: Mapping[str, str]) -> float:
    """解析并限制官方客户端的请求总超时秒数。

    超时必须位于 0.1 至 300 秒之间，以避免零超时造成必然失败，也避免极大值
    让命令行长时间无响应。错误只说明合法范围，不回显原始字符串。
    """

    raw_value = environ.get("METAHUB_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)).strip()
    try:
        value = float(raw_value)
    # 非数字超时不能交给客户端，否则会在建连阶段产生难以定位的类型错误。
    except (TypeError, ValueError) as exc:
        # 固定代码不拼接非法原值，避免环境变量内容进入终端采集日志。
        raise ExampleConfigError("config.METAHUB_TIMEOUT_SECONDS_invalid") from exc
    # 超时越界会导致请求必然失败或等待过久，必须在创建客户端前拒绝。
    if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
        raise ExampleConfigError("config.METAHUB_TIMEOUT_SECONDS_out_of_range")
    return value


def _parse_max_retries(environ: Mapping[str, str]) -> int:
    """解析并限制官方客户端自动重试次数。

    只接受 0 至 10 的十进制整数，防止负数语义不清或无意配置过多重试，从而
    放大调用流量、等待时间和模型成本。
    """

    raw_value = environ.get("METAHUB_MAX_RETRIES", str(DEFAULT_MAX_RETRIES)).strip()
    try:
        value = int(raw_value)
    # 非整数重试值不具备明确语义，统一转换为安全配置错误。
    except (TypeError, ValueError) as exc:
        # 错误码只标识变量，不回显可能来自外部部署系统的原始字符串。
        raise ExampleConfigError("config.METAHUB_MAX_RETRIES_invalid") from exc
    # 限制自动重试次数，避免错误配置放大上游流量、时延和模型成本。
    if not 0 <= value <= MAX_RETRIES:
        raise ExampleConfigError("config.METAHUB_MAX_RETRIES_out_of_range")
    return value


def _normalize_base_url(environ: Mapping[str, str]) -> str:
    """校验 Gateway 地址并移除末尾多余斜杠。

    示例仅允许带主机的 HTTP 或 HTTPS 地址，同时拒绝 URL 用户名和密码，避免
    凭据被误写在 ``base_url`` 后进入代理日志、诊断信息或 shell 历史。
    """

    raw_value = environ.get("METAHUB_BASE_URL", DEFAULT_BASE_URL).strip()
    value = raw_value or DEFAULT_BASE_URL
    parsed = urlparse(value)
    # Gateway 地址必须带受支持协议和主机，非法地址不进入官方客户端。
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ExampleConfigError("config.METAHUB_BASE_URL_invalid")
    # URL 内嵌凭据容易进入代理日志和命令历史，因此明确禁止该配置方式。
    if parsed.username is not None or parsed.password is not None:
        raise ExampleConfigError("config.METAHUB_BASE_URL_credentials_forbidden")
    return value.rstrip("/")


def load_config(environ: Mapping[str, str] | None = None) -> ExampleConfig:
    """从指定映射或当前进程环境构建经过校验的示例配置。

    测试可以传入独立映射以避免污染进程环境；正常命令行调用使用 ``os.environ``。
    Virtual Key 只保存到返回对象供客户端初始化使用，不会被校验错误回显。
    """

    source = os.environ if environ is None else environ
    return ExampleConfig(
        base_url=_normalize_base_url(source),
        # 密钥只在这里读取，后续仅传入 OpenAI(api_key=...)，禁止打印配置对象。
        virtual_key=_required_text(source, "METAHUB_VIRTUAL_KEY"),
        chat_model=_optional_text(source, "METAHUB_CHAT_MODEL", DEFAULT_CHAT_MODEL),
        embedding_model=_optional_text(
            source,
            "METAHUB_EMBEDDING_MODEL",
            DEFAULT_EMBEDDING_MODEL,
        ),
        timeout_seconds=_parse_timeout(source),
        max_retries=_parse_max_retries(source),
    )
