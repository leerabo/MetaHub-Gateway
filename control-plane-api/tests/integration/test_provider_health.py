"""PRD-035 Provider 健康探测主流程、恢复、权限和脱敏集成测试。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpcore
import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.provider_health.routes import router as health_router
from metahub_control.provider_health.service import (
    _PinnedNetworkBackend,
    _ValidatedProbeUrl,
    _validated_probe_url,
)
from metahub_control.providers.routes import router as providers_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下固定密码仅用于 PRD-035 角色矩阵测试，不会写入生产配置或应用日志。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


@pytest.fixture()
def mutable_provider_endpoint():
    """启动可在 500 与 200 间切换的本地上游，验证红转绿恢复流程。"""

    state = {"status": 500}

    class Handler(BaseHTTPRequestHandler):
        """只返回固定状态和恶意正文，验证探测不会下载或持久化上游内容。"""

        def do_GET(self) -> None:  # noqa: N802
            """处理 `/v1/models` 探测并记录当前测试状态。"""

            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"secret": "must-not-enter-probe-log"}).encode("utf-8"))

        def log_message(self, format: str, *args: object) -> None:
            """关闭测试 HTTP Server 的访问日志，避免污染脱敏断言。"""

            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield state, f"http://{host}:{port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """创建隔离 SQLite 数据库，并加入 developer/viewer 权限样本。"""

    database_url = f"sqlite:///{(tmp_path / 'health.db').as_posix()}"
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-provider-health-secret")
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.executemany(
        """
        INSERT INTO users(user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            ("health_dev", seed.tenant_id, "health-dev@example.com", "Health Developer", hash_password(DEVELOPER_PASSWORD), "developer"),
            ("health_viewer", seed.tenant_id, "health-viewer@example.com", "Health Viewer", hash_password(VIEWER_PASSWORD), "viewer"),
        ],
    )
    connection.commit()
    return database_url


def _build_app():
    """构造只含认证、Provider 和健康路由的轻量集成测试应用。"""

    app = create_service_app("control-plane-api", "Provider Health Test API")
    app.include_router(auth_router)
    app.include_router(providers_router)
    app.include_router(health_router)
    return app


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试角色并返回短生命周期 Bearer Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 仅用于当前测试请求头，不输出到断言消息或持久化表。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_provider_probe_failure_recovery_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutable_provider_endpoint,
) -> None:
    """主流程：失败立即红色，Endpoint 恢复后自动转绿且每次尝试均留日志。"""

    state, base_url = mutable_provider_endpoint
    database_url = _prepare_database(monkeypatch, tmp_path)
    # SSRF 策略单独做纯函数测试；本用例显式把本地测试服务器转换为已验证目标。
    monkeypatch.setattr(
        "metahub_control.provider_health.service._validated_probe_url",
        lambda config, value: f"{value.rstrip('/')}/models",
    )
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin}"}
        created = await client.post(
            "/providers",
            headers=headers,
            json={"provider_code": "health_lab", "name": "Health Lab", "provider_type": "openai_compatible", "base_url": base_url, "auth_type": "none"},
        )
        provider_id = created.json()["provider"]["provider_id"]
        failed = await client.post(f"/providers/{provider_id}/probe", headers=headers, json={})
        state["status"] = 200
        recovered = await client.post(f"/providers/{provider_id}/probe", headers=headers)
        listed = await client.get("/provider-health", headers=headers)

    assert failed.status_code == 200, failed.text
    assert failed.json()["result_status"] == "failed"
    assert failed.json()["provider_health"]["effective_health_status"] == "unhealthy"
    assert recovered.json()["result_status"] == "success"
    assert recovered.json()["provider_health"]["effective_health_status"] == "healthy"
    assert listed.json()["summary"]["healthy"] >= 1
    assert "must-not-enter-probe-log" not in failed.text + recovered.text
    connection = connect_database(database_url)
    logs = connection.execute(
        "SELECT result_status, result_summary, tenant_id FROM provider_health_probe_logs WHERE provider_id=? ORDER BY checked_at",
        (provider_id,),
    ).fetchall()
    assert [row["result_status"] for row in logs] == ["failed", "success"]
    assert all("must-not-enter" not in row["result_summary"] for row in logs)
    assert all(row["tenant_id"] for row in logs)


@pytest.mark.anyio
async def test_provider_health_permissions_and_strict_probe_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """异常/权限：developer 可读，viewer 被后端拒绝，probe 不接受 URL 或 tenant_id。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer = await _login(client, "health-dev@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "health-viewer@example.com", VIEWER_PASSWORD)
        developer_list = await client.get(
            "/provider-health", headers={"Authorization": f"Bearer {developer}"}
        )
        viewer_list = await client.get(
            "/provider-health", headers={"Authorization": f"Bearer {viewer}"}
        )
        invalid_body = await client.post(
            "/providers/not-found/probe",
            headers={"Authorization": f"Bearer {developer}"},
            json={"base_url": "http://127.0.0.1", "tenant_id": "other"},
        )

    assert developer_list.status_code == 200
    assert viewer_list.status_code == 403
    assert viewer_list.json()["code"] == "auth.permission_denied"
    assert invalid_body.status_code == 422
    assert invalid_body.json()["request_id"]


def test_provider_probe_ssrf_policy_blocks_local_and_accepts_public_dns(monkeypatch) -> None:
    """安全边界：回环地址始终拒绝，全部解析为公开地址的标准 HTTPS Endpoint 可探测。"""

    class Config:
        """提供 SSRF 纯函数所需的最小私网 allowlist 配置。"""

        provider_health_private_cidrs: list[str] = []

    addresses = {
        "localhost": "127.0.0.1",
        "mapped-metadata.example": "::ffff:100.100.100.200",
        "cgnat.example": "100.64.0.1",
        "provider.example": "8.8.8.8",
    }

    def _resolved_address(host: str, port: int, type: int) -> list[tuple[object, ...]]:
        """返回固定 DNS 样本，覆盖回环、mapped metadata、CGNAT 和正常公网。"""

        del type
        return [(2, 1, 6, "", (addresses[host], port))]

    monkeypatch.setattr(
        "metahub_control.provider_health.service.socket.getaddrinfo",
        _resolved_address,
    )
    for blocked_url in (
        "http://localhost:8000/v1",
        "https://mapped-metadata.example/v1",
        "https://cgnat.example/v1",
    ):
        # 所有非 global 或 metadata 变体都应在返回探测目标前失败关闭。
        with pytest.raises(ValueError):
            _validated_probe_url(Config(), blocked_url)
    assert _validated_probe_url(Config(), "https://provider.example/v1") == "https://provider.example/v1/models"


def test_provider_health_pins_validated_dns_address() -> None:
    """安全：TCP 层只能使用校验阶段的 IP，后续域名变化或目标替换均失败关闭。"""

    sentinel = object()

    class RecordingBackend:
        """记录连接目标的无网络后端，用于证明不会再次解析原域名。"""

        def __init__(self) -> None:
            """初始化本用例内的连接记录。"""

            self.hosts: list[str] = []

        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: object = None,
        ) -> object:
            """返回哨兵连接，并保存实际交给 TCP 后端的主机。"""

            del port, timeout, local_address, socket_options
            self.hosts.append(host)
            return sentinel

        def sleep(self, seconds: float) -> None:
            """本测试不发生真实重试等待，只满足固定连接后端的同步接口合同。"""

            del seconds

    target = _ValidatedProbeUrl(
        "https://provider.example/v1/models",
        hostname="provider.example",
        port=443,
        addresses=("8.8.8.8",),
    )
    delegate = RecordingBackend()
    backend = _PinnedNetworkBackend(target, delegate=delegate)  # type: ignore[arg-type]

    assert backend.connect_tcp("provider.example", 443) is sentinel
    assert delegate.hosts == ["8.8.8.8"]
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("changed.example", 443)
