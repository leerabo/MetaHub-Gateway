"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/runtime/logging.py
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

# 日志脱敏关键词和消息模式集中定义，任何 Provider Key、Token 或 Authorization 都不能明文落日志。
SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "provider_key",
    "secret",
    "token",
    "credential",
    "password",
)

SENSITIVE_MESSAGE_PATTERNS = (
    re.compile(
        r"(?i)\b(authorization|api[_-]?key|provider[_-]?key|access[_-]?token|token|secret|password)\b\s*[:=]\s*[^,\s]+"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    # 处理常见 sk- 前缀密钥，覆盖多数 OpenAI-compatible/Provider Key 样式。
    re.compile(r"\bsk-[A-Za-z0-9._-]+"),
)

# httpx/httpcore 的自由文本会包含完整上游 URL，pypdf 的告警则可能包含用户控制的 PDF token
# 或正文。业务层已经按固定 error_code 记录安全归因，因此这些第三方日志必须整体丢弃，不能
# 依赖不稳定的字符串格式尝试清洗 Endpoint 或文件内容。
UNSAFE_THIRD_PARTY_LOGGER_PREFIXES = ("httpx", "httpcore", "pypdf")


def is_sensitive_key(key: str) -> bool:
    """判断字段名是否疑似敏感。

    这里故意使用包含匹配，而不是精确匹配，因为真实请求里可能出现
    provider-api-key、accessToken、credentialSecret 等不同命名。宁可多脱敏，也不冒险
    把 Provider Key 或 Authorization 明文写入日志。
    """

    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def sanitize(value: Any) -> Any:
    """递归脱敏日志字段。

    该函数会处理 dict/list/tuple，便于后续请求日志、错误日志、Webhook 投递日志统一复用。
    PRD-001 的测试会覆盖 Provider Key 不明文落日志这一验收红线。
    """

    if isinstance(value, Mapping):
        return {
            str(key): "***MASKED***" if is_sensitive_key(str(key)) else sanitize(inner)
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item) for item in value)
    return value


def sanitize_message(message: str) -> str:
    """脱敏自由文本日志消息。

    很多泄露不是来自结构化字段，而是开发者临时写的 f-string 日志，例如
    provider_key=sk-xxx。这里对 message 本体再做一层正则脱敏，作为结构化脱敏的兜底。
    """

    sanitized = message
    for pattern in SENSITIVE_MESSAGE_PATTERNS:
        sanitized = pattern.sub(lambda match: _mask_message_match(match.group(0)), sanitized)
    return sanitized


def _mask_message_match(value: str) -> str:
    """生成 结构化日志 可安全展示或落日志的 mask message match 内容。
    
    安全：输出只能包含脱敏摘要，不能泄露明文密钥、Token、Prompt 或完整凭据。
    """


    if "bearer" in value.lower():
        return "Bearer ***MASKED***"
    if ":" in value:
        return f"{value.split(':', 1)[0]}:***MASKED***"
    if "=" in value:
        return f"{value.split('=', 1)[0]}=***MASKED***"
    return "***MASKED***"


class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器。

    PRD-001 不接入外部 Trace 后端，但日志从第一版就输出 JSON，保证运维可以按
    request_id grep，也为后续 OpenTelemetry 或日志平台接入留下稳定字段。
    """

    def format(self, record: logging.LogRecord) -> str:
        """实现 结构化日志 的 format 逻辑，保持职责、输入边界和错误码口径清晰。
        
        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """


        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize_message(record.getMessage()),
        }
        for key in (
            "request_id",
            "trace_id",
            "service_name",
            "event",
            "path",
            "method",
            "status_code",
            "latency_ms",
            "error_code",
            "config_summary",
            "tenant_id",
            "actor_id",
            "object_id",
            "payload",
        ):
            # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(sanitize(payload), ensure_ascii=False, default=str)


class DropUnsafeThirdPartyLoggerFilter(logging.Filter):
    """丢弃可能携带 Endpoint 或用户文件正文的第三方自由文本日志。

    httpx/httpcore 可能把完整 URL、path 或 query 组装进 message，pypdf 的容错告警可能包含
    用户控制的损坏对象或 CMap 行。MetaHub 已单独记录固定错误类别、request_id 和安全对象 ID，
    所以这里 Fail Closed，不把第三方原文交给 formatter 或外部日志平台。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """仅放行安全命名空间记录，不读取或输出第三方 message 正文。"""

        return not any(
            record.name == prefix or record.name.startswith(f"{prefix}.")
            for prefix in UNSAFE_THIRD_PARTY_LOGGER_PREFIXES
        )


# 兼容 PRD-056 已公开的旧导入名；过滤边界现同时覆盖第三方 Endpoint 与文件解析器日志。
DropUnsafeEndpointLoggerFilter = DropUnsafeThirdPartyLoggerFilter


def _install_third_party_log_safety(root: logging.Logger) -> None:
    """给全部 root handler 幂等安装第三方日志硬过滤，并收拢其传播路径。"""

    for handler in root.handlers:
        # 热重载和测试会重复初始化；同一 handler 只保留一个安全过滤器，避免状态无界增长。
        if not any(isinstance(item, DropUnsafeThirdPartyLoggerFilter) for item in handler.filters):
            handler.addFilter(DropUnsafeThirdPartyLoggerFilter())
    for name in UNSAFE_THIRD_PARTY_LOGGER_PREFIXES:
        third_party = logging.getLogger(name)
        # 第三方不应拥有绕过 root Filter 的私有 handler；高于 CRITICAL 的继承级别同时保护后续
        # 新增 handler 的部署配置，当前 root Filter 则阻断显式降低级别的子 logger。
        third_party.handlers.clear()
        third_party.setLevel(logging.CRITICAL + 1)
        third_party.propagate = True


def configure_json_logging(level: str = "INFO") -> None:
    """配置根 logger。

    多次调用时只替换 formatter，不重复添加 handler，避免 pytest 或热重载场景下日志重复。
    """

    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        root.addHandler(handler)
    for handler in root.handlers:
        handler.setFormatter(JsonFormatter())
    _install_third_party_log_safety(root)
