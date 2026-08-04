"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_debug_console.py
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
from metahub_control.debug_console.routes import router as debug_console_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试常量是调试台集成夹具，Provider Key 样例只进入测试数据库和 mocked 上游。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DebugDeveloper!2026"
VIEWER_PASSWORD = "DebugViewer!2026"
SEED_VIRTUAL_KEY = "mhv_prd020_virtual_key_secret"
PROVIDER_KEY = "sk-prd020-provider-secret"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换为 sqlite URL，供 PRD-020 控制面测试应用使用。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证和调试台路由的测试应用。

    该应用复用真实 FastAPI 中间件和错误处理器，确保 request_id、trace_id 和统一错误结构与生产入口一致。
    """

    app = create_service_app("control-plane-api", "Debug Console Test API")
    app.include_router(auth_router)
    app.include_router(debug_console_router)
    return app


@pytest.fixture()
def fake_upstream():
    """启动一个 OpenAI-compatible 假上游，记录请求头和请求体用于断言脱敏与路由结果。"""

    captured: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        """假上游 HTTP Handler。

        Handler 只在测试进程内监听本地端口；它返回最小 Chat Completions JSON，并在指定模型上模拟上游 500。
        """

        def do_POST(self) -> None:  # noqa: N802
            """处理测试中的 POST /v1/chat/completions 请求，并记录路由后的模型与脱敏请求头。"""

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            captured.append(
                {
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
            # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
            if body.get("model") == "upstream-error-model":
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"type":"server_error","message":"failed"}}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "chatcmpl_prd020",
                        "object": "chat.completion",
                        "model": body.get("model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "调试台响应"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )

        def log_message(self, format: str, *args: object) -> None:
            """关闭 BaseHTTPRequestHandler 默认日志，避免测试输出携带请求细节。"""

            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", captured
    finally:
        server.shutdown()
        thread.join(timeout=2)


def prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_base_url: str,
) -> tuple[str, str, str]:
    """准备调试台集成测试需要的租户、用户、Virtual Key、模型别名和上游配置。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", provider_base_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", SEED_VIRTUAL_KEY)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd020_developer",
                seed.tenant_id,
                "developer@example.com",
                "Debug Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd020_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Debug Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    default_model = connection.execute(
        "SELECT model_id, provider_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (seed.tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY created_at ASC LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    # Alias 必须显式绑定同租户的 Environment 主数据，不能只依赖兼容代码 `dev`。
    environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (seed.tenant_id,),
    ).fetchone()["environment_id"]
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd020_error', ?, ?, 'debug-error-internal', 'upstream-error-model',
          '{"chat": true}', 'active', datetime('now'), datetime('now')
        )
        """,
        (seed.tenant_id, default_model["provider_id"]),
    )
    connection.executemany(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id, default_model_id, status,
          description, created_by, created_at, updated_at
        )
        VALUES(?, ?, ?, 'dev', ?, ?, 'active', ?, 'user_prd020_developer', datetime('now'), datetime('now'))
        """,
        [
            (
                "alias_prd020_default",
                seed.tenant_id,
                "company-default",
                environment_id,
                default_model["model_id"],
                "PRD-020 success alias",
            ),
            (
                "alias_prd020_error",
                seed.tenant_id,
                "company-error",
                environment_id,
                "model_prd020_error",
                "PRD-020 upstream error alias",
            ),
        ],
    )
    connection.commit()
    # 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
    return database_url, seed.tenant_id, virtual_key["virtual_key_id"]


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试用户并返回控制面访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_debug_console_sends_request_and_records_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主流程/安全：调试台完成一次请求，并落 request_logs、usage_ledger 和 debug_console_events。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        response = await client.post(
            "/debug/chat-completions",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd020_success"},
            json={
                "virtual_key_id": virtual_key_id,
                "model_alias": "company-default",
                "messages": [{"role": "user", "content": "private debug prompt"}],
                "temperature": 0.2,
                "max_tokens": 128,
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["tenant_id"] == tenant_id
    assert payload["request_id"] == "req_prd020_success"
    assert payload["status"] == "success"
    assert payload["model_alias"] == "company-default"
    assert payload["resolved_model"] == "default-chat"
    assert payload["provider_code"] == "local_openai"
    assert payload["usage"]["total_tokens"] == 10
    assert payload["cost"]["currency"] in {"CNY", "USD"}
    assert payload["log_detail_url"].startswith("#request-logs")
    assert payload["response_preview"] == "调试台响应"
    assert SEED_VIRTUAL_KEY not in response.text
    assert PROVIDER_KEY not in response.text

    assert captured[-1]["path"] == "/v1/chat/completions"
    assert captured[-1]["body"]["model"] == "local-chat-model"
    assert captured[-1]["body"]["messages"][0]["content"] == "private debug prompt"
    assert captured[-1]["authorization"] == f"Bearer {PROVIDER_KEY}"

    connection = connect_database(database_url)
    request_log = connection.execute(
        "SELECT * FROM request_logs WHERE request_id='req_prd020_success'"
    ).fetchone()
    usage = connection.execute(
        "SELECT * FROM usage_ledger WHERE request_id='req_prd020_success'"
    ).fetchone()
    event = connection.execute(
        "SELECT * FROM debug_console_events WHERE gateway_request_id='req_prd020_success'"
    ).fetchone()
    assert request_log["status"] == "success"
    assert request_log["endpoint"] == "/debug/chat-completions"
    assert request_log["virtual_key_id"] == virtual_key_id
    assert usage["total_tokens"] == 10
    assert event["status"] == "success"
    assert event["event_type"] == "debug.request_sent"

    persisted = "\n".join(
        str(value)
        for table in ["request_logs", "usage_ledger", "debug_console_events"]
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
    )
    assert "private debug prompt" not in persisted
    assert "调试台响应" not in persisted
    assert SEED_VIRTUAL_KEY not in persisted
    assert PROVIDER_KEY not in persisted


@pytest.mark.anyio
async def test_debug_console_reused_request_id_does_not_overwrite_first_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """幂等边界：第二次同 ID 在 Provider 前 409，首次成功日志和 Usage 保持不可变。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(
        monkeypatch, tmp_path, upstream_url
    )
    control_app = build_control_app()
    headers: dict[str, str]
    payload = {
        "virtual_key_id": virtual_key_id,
        "model_alias": "company-default",
        "messages": [{"role": "user", "content": "only once"}],
    }
    async with AsyncClient(
        transport=ASGITransport(app=control_app), base_url="http://control"
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": "req_prd045_debug_reused",
        }
        first = await client.post("/debug/chat-completions", headers=headers, json=payload)
        second = await client.post("/debug/chat-completions", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "gateway.request_id_reused"
    assert len(captured) == 1
    connection = connect_database(database_url)
    request_log = connection.execute(
        """
        SELECT snapshot_version, policy_snapshot_status, status, error_code
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd045_debug_reused'
        """,
        (tenant_id,),
    ).fetchone()
    usage_count = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM usage_ledger
        WHERE tenant_id=? AND request_id='req_prd045_debug_reused'
        """,
        (tenant_id,),
    ).fetchone()["count"]
    claim_count = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM gateway_request_claims
        WHERE tenant_id=? AND request_id='req_prd045_debug_reused'
        """,
        (tenant_id,),
    ).fetchone()["count"]
    assert tuple(request_log) == (None, "disabled", "success", None)
    assert usage_count == 1
    assert claim_count == 1


@pytest.mark.anyio
async def test_debug_console_rejects_viewer_and_invalid_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """权限/参数异常：viewer 被后端拒绝，非法 messages 返回统一 debug.request_invalid 并写调试事件。"""

    upstream_url, _captured = fake_upstream
    database_url, _tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_response = await client.post(
            "/debug/chat-completions",
            headers={"Authorization": f"Bearer {viewer_token}", "X-Request-ID": "req_prd020_viewer"},
            json={
                "virtual_key_id": virtual_key_id,
                "model_alias": "company-default",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        invalid_response = await client.post(
            "/debug/chat-completions",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd020_invalid"},
            json={
                "virtual_key_id": virtual_key_id,
                "model_alias": "company-default",
                "messages": [],
                "provider_key": "sk-should-not-enter-debug",
            },
        )

    assert viewer_response.status_code == 403
    assert viewer_response.json()["code"] == "auth.permission_denied"
    assert invalid_response.status_code == 422
    assert invalid_response.json()["code"] == "debug.request_invalid"
    assert "sk-should-not-enter-debug" not in invalid_response.text

    connection = connect_database(database_url)
    invalid_event = connection.execute(
        "SELECT status, error_code, payload_json FROM debug_console_events WHERE gateway_request_id=?",
        ("req_prd020_invalid",),
    ).fetchone()
    invalid_log = connection.execute(
        "SELECT request_id FROM request_logs WHERE request_id='req_prd020_invalid'"
    ).fetchone()
    assert invalid_event["status"] == "failed"
    assert invalid_event["error_code"] == "debug.request_invalid"
    assert "sk-should-not-enter-debug" not in invalid_event["payload_json"]
    assert invalid_log is None


@pytest.mark.anyio
async def test_debug_console_records_upstream_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """异常归因：上游 5xx 返回统一 gateway.upstream_error，并写失败 request_logs 与调试事件。"""

    upstream_url, _captured = fake_upstream
    database_url, _tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        response = await client.post(
            "/debug/chat-completions",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd020_upstream"},
            json={
                "virtual_key_id": virtual_key_id,
                "model_alias": "company-error",
                "messages": [{"role": "user", "content": "trigger provider failure"}],
            },
        )

    assert response.status_code == 502
    assert response.json()["code"] == "gateway.upstream_error"
    assert "trigger provider failure" not in response.text

    connection = connect_database(database_url)
    request_log = connection.execute(
        "SELECT status, error_code FROM request_logs WHERE request_id='req_prd020_upstream'"
    ).fetchone()
    event = connection.execute(
        "SELECT status, error_code, payload_json FROM debug_console_events WHERE gateway_request_id=?",
        ("req_prd020_upstream",),
    ).fetchone()
    assert request_log["status"] == "failed"
    assert request_log["error_code"] == "gateway.upstream_error"
    assert event["status"] == "failed"
    assert event["error_code"] == "gateway.upstream_error"
    assert "trigger provider failure" not in event["payload_json"]
