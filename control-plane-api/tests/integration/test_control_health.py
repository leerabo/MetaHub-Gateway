"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_control_health.py
"""

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.app import app


@pytest.mark.anyio
async def test_control_health_returns_request_id() -> None:
    """主流程：控制面 API 健康检查必须与数据面保持同一契约。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health", headers={"X-Request-ID": "req_test_control"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_test_control"
    assert response.headers["X-Trace-ID"] == "trc_test_control"
    assert response.json() == {
        "status": "ok",
        "service_name": "control-plane-api",
        "environment": "dev",
        "request_id": "req_test_control",
        "trace_id": "trc_test_control",
    }


@pytest.mark.anyio
async def test_control_unknown_path_uses_unified_error_shape() -> None:
    """异常流程：控制面未知路径也必须返回统一错误码和 request_id。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/missing", headers={"X-Request-ID": "req_control_missing"})

    assert response.status_code == 404
    assert response.json()["code"] == "infra.not_found"
    assert response.json()["request_id"] == "req_control_missing"
    assert response.json()["trace_id"] == "trc_control_missing"
