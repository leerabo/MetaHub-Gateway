"""结构化日志敏感边界测试。

覆盖字段/自由文本脱敏以及第三方 HTTP Logger 的 Endpoint Fail Closed 规则；测试制品不得输出
Provider Key、Authorization、Token、完整 URL、Prompt 或响应正文，同时保留 MetaHub request_id。
"""

import json
import logging
from io import StringIO

import httpx
from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.providers.base import (
    NormalizedResponse,
    NormalizedUsage,
    ProviderStreamResponse,
)
from metahub_gateway.routing.models import GatewayRoute
from metahub_shared.runtime.logging import (
    DropUnsafeEndpointLoggerFilter,
    JsonFormatter,
    configure_json_logging,
    sanitize,
)


class _PrivateRepr:
    """在 repr 中暴露固定哨兵的测试对象，用来证明外层 DTO 会隐藏整个字段。"""

    def __repr__(self) -> str:
        """返回敏感哨兵；生产 DTO 的 repr 中不得触发或包含该结果。"""

        return "private-response-or-client-sentinel"


def test_sanitize_masks_provider_keys_and_authorization() -> None:
    """安全验收：Provider Key、Authorization、Token 等敏感字段不能明文落日志。"""

    payload = {
        "provider_key": "sk-provider-secret",
        "Authorization": "Bearer vk-live-secret",
        "nested": {"access_token": "token-secret", "safe": "visible"},
    }

    sanitized = sanitize(payload)

    assert sanitized["provider_key"] == "***MASKED***"
    assert sanitized["Authorization"] == "***MASKED***"
    assert sanitized["nested"]["access_token"] == "***MASKED***"
    assert sanitized["nested"]["safe"] == "visible"


def test_json_formatter_keeps_request_id_without_sensitive_values() -> None:
    """异常排障：结构化日志必须保留 request_id，同时不暴露敏感字段。"""

    record = logging.LogRecord(
        name="metahub.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="provider_key=sk-provider-secret",
        args=(),
        exc_info=None,
    )
    record.request_id = "req_log"
    record.trace_id = "trc_log"
    record.provider_key = "sk-provider-secret"

    rendered = JsonFormatter().format(record)
    payload = json.loads(rendered)

    assert payload["request_id"] == "req_log"
    assert payload["trace_id"] == "trc_log"
    assert "sk-provider-secret" not in rendered


def test_gateway_route_and_provider_responses_hide_sensitive_repr_fields() -> None:
    """Route Endpoint、Provider 正文和流资源不得因 dataclass repr 进入异常日志。"""

    route = GatewayRoute(
        tenant_id="tenant-log-test",
        model_id="model-log-test",
        model_alias="model-safe",
        upstream_model_name="private-upstream-model-sentinel",
        capabilities={"chat": True},
        provider_id="provider-log-test",
        provider_code="provider-safe",
        provider_type="openai_compatible",
        provider_auth_type="bearer",
        provider_name="Provider Safe",
        provider_base_url="https://private-endpoint.invalid/v1",
    )
    auth = GatewayAuthContext(
        tenant_id="tenant-log-test",
        virtual_key_id="virtual-key-safe-id",
        key_prefix="private-key-prefix-sentinel",
        environment="dev",
        environment_id="environment-log-test",
        environment_name="Dev",
    )
    normalized = NormalizedResponse(
        payload={"content": "private-response-sentinel"},
        usage=NormalizedUsage(total_tokens=1),
        status_code=200,
    )
    private = _PrivateRepr()
    stream = ProviderStreamResponse(
        client=private,  # type: ignore[arg-type] - 故意注入可识别 repr 的资源替身。
        response=private,  # type: ignore[arg-type] - 不访问网络，只验证 DTO 表示。
        stream_context=private,
    )

    rendered = "\n".join((repr(route), repr(auth), repr(normalized), repr(stream)))

    assert "private-endpoint" not in rendered
    assert "private-upstream-model-sentinel" not in rendered
    assert "private-key-prefix-sentinel" not in rendered
    assert "private-response-sentinel" not in rendered
    assert "private-response-or-client-sentinel" not in rendered
    assert "tenant-log-test" in rendered
    assert "provider-safe" in rendered
    assert "status_code=200" in rendered


def test_logging_configuration_drops_all_http_client_endpoint_messages() -> None:
    """真实 httpx 请求和 httpcore 子日志不得把 Endpoint/URL 带入 root 输出。"""

    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    httpcore_connection_logger = logging.getLogger("httpcore.connection")
    original_root_handlers = list(root.handlers)
    original_root_level = root.level
    original_states = {
        logger: (list(logger.handlers), logger.level, logger.propagate)
        for logger in (httpx_logger, httpcore_logger, httpcore_connection_logger)
    }
    stream = StringIO()
    root.handlers = [logging.StreamHandler(stream)]
    try:
        configure_json_logging("INFO")
        configure_json_logging("INFO")
        root_handler = root.handlers[0]
        assert (
            sum(isinstance(item, DropUnsafeEndpointLoggerFilter) for item in root_handler.filters)
            == 1
        )

        # 显式降低子 logger 级别模拟第三方库未来改配置；root Filter 仍必须是最终硬边界。
        httpx_logger.setLevel(logging.DEBUG)
        httpcore_connection_logger.setLevel(logging.DEBUG)
        endpoint = (
            "https://endpoint-sentinel.invalid/upstream-b/v1/chat/completions?token=query-sentinel"
        )
        httpx_logger.info("HTTP Request: POST %s", endpoint)
        httpx_logger.error("HTTP transport failed for %s", endpoint)
        httpcore_connection_logger.debug("connect_tcp endpoint=%s", endpoint)
        httpcore_connection_logger.warning("retry endpoint=%s", endpoint)
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
            trust_env=False,
        ) as client:
            response = client.get(endpoint, headers={"Authorization": "Bearer header-sentinel"})
        assert response.status_code == 200

        logging.getLogger("metahub.test").info(
            "usage.recorded",
            extra={
                "request_id": "req_safe_info_log",
                "trace_id": "trc_safe_info_log",
                "event": "usage.recorded",
            },
        )
        logging.getLogger("metahub.test").warning(
            "provider.call_failed",
            extra={
                "request_id": "req_safe_http_log",
                "trace_id": "trc_safe_http_log",
                "error_code": "provider_adapter.upstream_error",
            },
        )
        rendered = stream.getvalue()
        assert "endpoint-sentinel" not in rendered
        assert "/upstream-b/" not in rendered
        assert "query-sentinel" not in rendered
        assert "header-sentinel" not in rendered
        assert '"logger": "httpx"' not in rendered
        assert '"logger": "httpcore.connection"' not in rendered
        assert '"request_id": "req_safe_info_log"' in rendered
        assert '"trace_id": "trc_safe_info_log"' in rendered
        assert '"event": "usage.recorded"' in rendered
        assert '"request_id": "req_safe_http_log"' in rendered
        assert '"trace_id": "trc_safe_http_log"' in rendered
        assert '"error_code": "provider_adapter.upstream_error"' in rendered
    finally:
        root.handlers = original_root_handlers
        root.setLevel(original_root_level)
        for logger, (handlers, level, propagate) in original_states.items():
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
