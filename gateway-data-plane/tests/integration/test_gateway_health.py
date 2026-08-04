"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/integration/test_gateway_health.py
"""

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_gateway.app import app


@pytest.mark.anyio
async def test_gateway_health_returns_request_id() -> None:
    """主流程：数据面健康检查必须可用，并透出 request_id。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health", headers={"X-Request-ID": "req_test_gateway"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_test_gateway"
    assert response.headers["X-Trace-ID"] == "trc_test_gateway"
    assert response.json() == {
        "status": "ok",
        "service_name": "gateway-data-plane",
        "environment": "dev",
        "request_id": "req_test_gateway",
        "trace_id": "trc_test_gateway",
    }


@pytest.mark.anyio
async def test_gateway_version_contains_service_info() -> None:
    """主流程：版本接口必须返回服务名、版本、启动时间和 request_id。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/version")

    payload = response.json()
    assert response.status_code == 200
    assert payload["service_name"] == "gateway-data-plane"
    assert payload["version"]
    assert payload["started_at"]
    assert payload["request_id"].startswith("req_")
    assert payload["trace_id"].startswith("trc_")


@pytest.mark.anyio
async def test_gateway_internal_health_returns_trace_id() -> None:
    """主流程：内部健康检查也必须返回 request_id/trace_id。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/internal/health",
            headers={"X-Request-ID": "req_internal", "X-Trace-ID": "trc_internal"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_internal"
    assert response.headers["X-Trace-ID"] == "trc_internal"
    assert response.json()["trace_id"] == "trc_internal"


@pytest.mark.anyio
async def test_gateway_unknown_path_uses_unified_error_shape() -> None:
    """异常流程：未知路径不能返回框架默认错误，必须走统一错误码结构。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/missing", headers={"X-Request-ID": "req_missing"})

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "req_missing"
    assert response.headers["X-Trace-ID"] == "trc_missing"
    payload = response.json()
    assert payload["code"] == "infra.not_found"
    assert payload["message"] == "Requested resource was not found."
    assert payload["request_id"] == "req_missing"
    assert payload["trace_id"] == "trc_missing"
    assert payload["suggestion"] == "Check the API path and method."
    assert payload["diagnosis"]["rule_id"] == "unknown_error"


@pytest.mark.anyio
async def test_cors_preflight_allows_delete_management_calls() -> None:
    """浏览器流程：控制台需要 DELETE 管理接口，CORS 预检必须允许。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.options(
            "/virtual-keys/vk_test",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "DELETE",
            },
        )

    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]


@pytest.mark.anyio
async def test_cors_exposes_request_and_trace_headers_to_console() -> None:
    """浏览器流程：跨域调试台必须能读取 request_id 和 trace_id 响应头。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/health",
            headers={
                "Origin": "http://localhost:5173",
                "X-Request-ID": "req_cors_exposed",
                "X-Trace-ID": "trc_cors_exposed",
            },
        )

    # 控制台既要读取链路标识，也要读取 CSV 的安全文件名；Authorization 等敏感头
    # 绝不能进入暴露列表。共享 CORS 白名单由 Gateway 与 Control API 使用同一合同。
    exposed_headers = {
        item.strip().lower()
        for item in response.headers["access-control-expose-headers"].split(",")
    }
    assert exposed_headers == {"content-disposition", "x-request-id", "x-trace-id"}
    assert response.headers["X-Request-ID"] == "req_cors_exposed"
    assert response.headers["X-Trace-ID"] == "trc_cors_exposed"


@pytest.mark.anyio
async def test_gateway_concurrent_requests_do_not_mix_trace_ids() -> None:
    """异常/并发：多个请求不能串 request_id 或 trace_id。"""

    import asyncio

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        responses = await asyncio.gather(
            *[
                client.get(
                    "/health",
                    headers={"X-Request-ID": f"req_parallel_{idx}", "X-Trace-ID": f"trc_parallel_{idx}"},
                )
                for idx in range(8)
            ]
        )

    for idx, response in enumerate(responses):
        payload = response.json()
        assert payload["request_id"] == f"req_parallel_{idx}"
        assert payload["trace_id"] == f"trc_parallel_{idx}"
        assert response.headers["X-Request-ID"] == f"req_parallel_{idx}"
        assert response.headers["X-Trace-ID"] == f"trc_parallel_{idx}"
