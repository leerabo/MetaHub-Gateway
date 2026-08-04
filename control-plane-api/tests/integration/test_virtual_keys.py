"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_virtual_keys.py
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.environments.routes import router as environments_router
from metahub_control.virtual_keys.routes import router as virtual_keys_router
from metahub_gateway.api.openai_compatible import router as openai_compatible_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password, hash_secret
from metahub_shared.runtime.fastapi_app import create_service_app


# 以下测试密码和 Provider Key 样例只用于 Virtual Key 主流程，断言明文只返回一次。
ADMIN_PASSWORD = "LocalAdmin!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
PROVIDER_KEY = "sk-prd010-provider-secret"


def sqlite_url(path: Path) -> str:
    """sqlite_url：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """build_control_app：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    app = create_service_app("control-plane-api", "Virtual Key Test Control API")
    app.include_router(auth_router)
    app.include_router(environments_router)
    app.include_router(virtual_keys_router)
    return app


def build_gateway_app():
    """build_gateway_app：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    app = create_service_app("gateway-data-plane", "Virtual Key Test Gateway API")
    app.include_router(openai_compatible_router)
    return app


@pytest.fixture()
def fake_upstream():
    """fake_upstream：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    class Handler(BaseHTTPRequestHandler):
        """Handler：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        def do_POST(self) -> None:  # noqa: N802
            """do_POST：说明该定义的测试或脚本职责。
            
            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if self.path == "/v1/chat/completions":
                self._json(
                    {
                        "id": "chatcmpl_prd010",
                        "object": "chat.completion",
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "pong"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                )
                return
            self.send_response(404)
            self.end_headers()

        def _json(self, payload: dict[str, Any]) -> None:
            """_json：说明该定义的测试或脚本职责。
            
            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def log_message(self, format: str, *args: object) -> None:
            """log_message：说明该定义的测试或脚本职责。
            
            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider_base_url: str) -> str:
    """prepare_database：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", provider_base_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES('user_prd010_viewer', ?, 'viewer@example.com', 'Viewer User', ?, 'viewer', 'active',
          datetime('now'), datetime('now'))
        """,
        (seed.tenant_id, hash_password(VIEWER_PASSWORD)),
    )
    connection.commit()
    return database_url


async def login(client: AsyncClient, email: str, password: str) -> str:
    """login：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_virtual_key_create_call_disable_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主流程/安全：Key 明文只展示一次，可调用 Gateway，禁用后不能再鉴权。"""

    database_url = prepare_database(monkeypatch, tmp_path, fake_upstream)
    control_app = build_control_app()
    gateway_app = build_gateway_app()

    control_transport = ASGITransport(app=control_app)
    gateway_transport = ASGITransport(app=gateway_app)
    async with AsyncClient(transport=control_transport, base_url="http://control") as control:
        admin_token = await login(control, "admin@metahub.local", ADMIN_PASSWORD)
        environment = await control.post(
            "/environments",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"code": "test", "name": "测试环境"},
        )
        assert environment.status_code == 201, environment.text
        create = await control.post(
            "/virtual-keys",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd010_create"},
            json={
                "name": "PRD010 Key",
                "environment_id": environment.json()["environment"]["environment_id"],
                "description": "integration",
            },
        )
        list_after_create = await control.get(
            "/virtual-keys",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd010_list"},
        )

    assert create.status_code == 201
    created_payload = create.json()
    plain_value = created_payload["plain_value"]
    virtual_key = created_payload["virtual_key"]
    assert plain_value.startswith("mhv_test_")
    assert virtual_key["key_prefix"] == plain_value[:12]
    assert virtual_key["environment"] == "test"
    assert "plain_value" not in json.dumps(list_after_create.json()["virtual_keys"], ensure_ascii=False)
    assert "key_hash" not in list_after_create.text
    assert plain_value not in list_after_create.text

    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT key_hash, key_prefix, environment, last_used_at FROM virtual_keys WHERE virtual_key_id=?",
        (virtual_key["virtual_key_id"],),
    ).fetchone()
    assert row["key_hash"] == hash_secret(plain_value)
    assert row["key_prefix"] == plain_value[:12]
    assert row["environment"] == "test"
    assert row["last_used_at"] is None
    dump = "\n".join(
        str(value)
        for table in ["virtual_keys", "request_logs", "usage_ledger"]
        for row_item in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row_item)
    )
    assert plain_value not in dump

    async with AsyncClient(transport=gateway_transport, base_url="http://gateway") as gateway:
        call = await gateway.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {plain_value}", "X-Request-ID": "req_prd010_gateway"},
            json={"model": "default-chat", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert call.status_code == 200
    used = connection.execute(
        "SELECT last_used_at FROM virtual_keys WHERE virtual_key_id=?",
        (virtual_key["virtual_key_id"],),
    ).fetchone()
    assert used["last_used_at"] is not None

    async with AsyncClient(transport=control_transport, base_url="http://control") as control:
        admin_token = await login(control, "admin@metahub.local", ADMIN_PASSWORD)
        disabled = await control.delete(
            f"/virtual-keys/{virtual_key['virtual_key_id']}",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd010_disable"},
        )
    assert disabled.status_code == 200
    assert disabled.json()["virtual_key"]["status"] == "disabled"

    async with AsyncClient(transport=gateway_transport, base_url="http://gateway") as gateway:
        denied = await gateway.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {plain_value}", "X-Request-ID": "req_prd010_disabled_call"},
            json={"model": "default-chat", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert denied.status_code == 401
    assert denied.json()["code"] == "gateway.auth_invalid"


@pytest.mark.anyio
async def test_virtual_key_permissions_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """异常/权限：viewer 被后端拒绝，非法字段返回 virtual_key.* 错误码。"""

    prepare_database(monkeypatch, tmp_path, fake_upstream)
    control_app = build_control_app()
    transport = ASGITransport(app=control_app)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        viewer_denied = await client.get("/virtual-keys", headers={"Authorization": f"Bearer {viewer_token}"})
        invalid_env = await client.post(
            "/virtual-keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "bad env", "environment": "stage"},
        )
        empty_patch = await client.patch(
            "/virtual-keys/not-found",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )
        not_found = await client.delete(
            "/virtual-keys/not-found",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert invalid_env.status_code == 422
    assert invalid_env.json()["code"] == "virtual_key.environment_invalid"
    assert empty_patch.status_code == 422
    assert empty_patch.json()["code"] == "virtual_key.update_empty"
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "virtual_key.not_found"


@pytest.mark.anyio
async def test_virtual_key_limit_settings_flow_and_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """PRD-022 主流程/权限：writer 可设置 Key 限制，viewer 与非法字段被后端拒绝。"""

    prepare_database(monkeypatch, tmp_path, fake_upstream)
    control_app = build_control_app()
    transport = ASGITransport(app=control_app)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        create = await client.post(
            "/virtual-keys",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd022_create"},
            json={"name": "PRD022 Key", "environment": "dev"},
        )
        virtual_key = create.json()["virtual_key"]
        limits = await client.patch(
            f"/virtual-keys/{virtual_key['virtual_key_id']}/limits",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd022_limits"},
            json={"rpm_limit": 2, "tpm_limit": 20, "concurrency_limit": 1},
        )
        listed = await client.get(
            "/virtual-keys",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd022_list"},
        )
        viewer_denied = await client.patch(
            f"/virtual-keys/{virtual_key['virtual_key_id']}/limits",
            headers={"Authorization": f"Bearer {viewer_token}"},
            json={"rpm_limit": 3},
        )
        invalid = await client.patch(
            f"/virtual-keys/{virtual_key['virtual_key_id']}/limits",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"rpm_limit": 0},
        )
        empty = await client.patch(
            f"/virtual-keys/{virtual_key['virtual_key_id']}/limits",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )

    assert limits.status_code == 200, limits.text
    rule = limits.json()["virtual_key"]["rate_limit_rule"]
    assert rule["rpm_limit"] == 2
    assert rule["tpm_limit"] == 20
    assert rule["concurrency_limit"] == 1
    listed_key = next(
        item for item in listed.json()["virtual_keys"] if item["virtual_key_id"] == virtual_key["virtual_key_id"]
    )
    assert listed_key["rate_limit_rule"]["rpm_limit"] == 2
    assert "plain_value" not in listed.text
    assert "key_hash" not in listed.text
    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "virtual_key.limit_invalid"
    assert empty.status_code == 422
    assert empty.json()["code"] == "virtual_key.limit_update_empty"


@pytest.mark.anyio
async def test_virtual_key_concurrent_gateway_auth_updates_last_used(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """并发：同一 Key 多个请求同时鉴权不报错，并保留最近使用时间。"""

    database_url = prepare_database(monkeypatch, tmp_path, fake_upstream)
    control_app = build_control_app()
    gateway_app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as control:
        admin_token = await login(control, "admin@metahub.local", ADMIN_PASSWORD)
        create = await control.post(
            "/virtual-keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Concurrent Key", "environment": "dev"},
        )
    plain_value = create.json()["plain_value"]
    virtual_key_id = create.json()["virtual_key"]["virtual_key_id"]

    async with AsyncClient(transport=ASGITransport(app=gateway_app), base_url="http://gateway") as gateway:
        import asyncio

        responses = await asyncio.gather(
            *[
                gateway.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {plain_value}",
                        "X-Request-ID": f"req_prd010_parallel_{idx}",
                    },
                    json={"model": "default-chat", "messages": [{"role": "user", "content": "hi"}]},
                )
                for idx in range(5)
            ]
        )

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200]
    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT last_used_at FROM virtual_keys WHERE virtual_key_id=?",
        (virtual_key_id,),
    ).fetchone()
    assert row["last_used_at"] is not None
