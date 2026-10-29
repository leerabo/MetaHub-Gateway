"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_health_readiness.py
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_shared.runtime.fastapi_app import create_service_app


def sqlite_url(path: Path) -> str:
    """sqlite_url：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return f"sqlite:///{path.as_posix()}"


@pytest.mark.anyio
async def test_live_does_not_require_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """主流程：live 只证明进程可响应，不能依赖数据库。"""

    monkeypatch.delenv("METAHUB_DATABASE_URL", raising=False)
    app = create_service_app("control-plane-api", "Control Test")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/health/live",
            headers={"X-Request-ID": "req_live", "X-Trace-ID": "trc_live"},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["live"] is True
    assert payload["ready"] is False
    assert payload["checks"] == []
    assert payload["trace_id"] == "trc_live"


@pytest.mark.anyio
async def test_ready_is_true_when_database_ping_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：配置数据库且 ping 成功时 ready=true。"""

    monkeypatch.setenv("METAHUB_DATABASE_URL", sqlite_url(tmp_path / "ready.db"))
    app = create_service_app("control-plane-api", "Control Test")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert payload["ready"] is True
    assert payload["status"] == "ok"
    assert any(item["name"] == "database" and item["status"] == "ok" for item in payload["checks"])


@pytest.mark.anyio
async def test_ready_is_false_when_database_ping_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """异常流程：数据库断开或驱动不支持时 ready=false，但响应仍可解释。"""

    monkeypatch.setenv("METAHUB_DATABASE_URL", "postgresql://user:secret@localhost:5432/metahub")
    app = create_service_app("control-plane-api", "Control Test")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/health/ready",
            headers={"X-Request-ID": "req_ready_fail", "X-Trace-ID": "trc_ready_fail"},
        )

    payload = response.json()
    assert response.status_code == 503
    assert payload["ready"] is False
    assert payload["status"] == "fail"
    assert payload["request_id"] == "req_ready_fail"
    assert payload["trace_id"] == "trc_ready_fail"
    assert "secret" not in response.text
    assert any(item["name"] == "database" and item["status"] == "fail" for item in payload["checks"])


@pytest.mark.anyio
async def test_health_response_does_not_expose_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """安全验收：health 响应不能泄露 DB 密码、Redis 密码或 Provider Key。"""

    monkeypatch.setenv("METAHUB_DATABASE_URL", "postgresql://user:db-password@localhost:5432/metahub")
    monkeypatch.setenv("METAHUB_REDIS_URL", "redis://:redis-password@localhost:6379/0")
    monkeypatch.setenv("METAHUB_PROVIDER_DEEPSEEK_API_KEY", "sk-provider-secret")
    app = create_service_app("control-plane-api", "Control Test")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health/ready")

    body = response.text
    assert "db-password" not in body
    assert "redis-password" not in body
    assert "sk-provider-secret" not in body
