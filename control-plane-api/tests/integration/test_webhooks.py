"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_webhooks.py
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.audit import AuditRequestMeta
from metahub_control.auth.routes import router as auth_router
from metahub_control.auth.service import authenticate_bearer
from metahub_control.webhooks import service as webhook_service
from metahub_control.webhooks.models import ReplayWebhookRequest
from metahub_control.webhooks.routes import router as webhooks_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.webhook_delivery import dispatch_runtime_webhook_event
from metahub_shared.runtime.webhooks import build_webhook_event, verify_webhook_signature

# 以下测试密码只用于 Webhook 权限矩阵，发送 secret 和投递记录都在临时测试库内验证。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换为项目支持的 sqlite URL，供 Webhook 集成测试隔离数据。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证和 Webhook API 的控制面测试应用。"""

    app = create_service_app("control-plane-api", "Webhook Test Control API")
    app.include_router(auth_router)
    app.include_router(webhooks_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """准备 Webhook 测试所需的租户、管理员、开发者和 viewer 用户。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", "test-webhook-secret")
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
                "user_prd025_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd025_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    # 测试只把 tenant_id 作为断言基准返回，业务接口仍必须从登录态恢复租户。
    return database_url, seed.tenant_id


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试用户并返回访问令牌，后续请求只把 token 放入 Authorization 头。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # token 只在测试请求头中使用，不能写入业务表或断言输出文本。
    return response.json()["access_token"]


class RecordingWebhookHandler(BaseHTTPRequestHandler):
    """本地 Webhook 接收端，用于记录请求并校验 MetaHub HMAC 签名。"""

    server: "RecordingWebhookServer"

    def do_POST(self) -> None:
        """接收一次 Webhook POST，把安全摘要写入 server.received 供断言使用。"""

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        signature = self.headers.get("X-MetaHub-Signature", "")
        secret = self.server.signing_secret or ""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        self.server.received.append(
            {
                "path": self.path,
                "body": body,
                "payload": payload,
                "content_type": self.headers.get("Content-Type"),
                "event": self.headers.get("X-MetaHub-Event"),
                "signature": signature,
                "signature_valid": verify_webhook_signature(signature, secret, body) if secret else False,
            }
        )
        # /fail 固定返回 500，其他路径返回 204，便于测试成功和接收端失败两类分支。
        status = 500 if self.path == "/fail" else 204
        self.send_response(status)
        self.end_headers()

    def log_message(self, _format: str, *args: Any) -> None:
        """关闭 http.server 默认访问日志，避免测试输出携带无关请求细节。"""

        return None


class RecordingWebhookServer(ThreadingHTTPServer):
    """带 received 和 signing_secret 状态的测试 HTTP Server。

    该类只服务本地集成测试，保存接收到的安全摘要和一次性签名密钥，便于断言 HMAC 验签结果。
    """

    received: list[dict[str, Any]]
    signing_secret: str | None


class RunningWebhookServer:
    """管理测试接收端生命周期，确保后台线程在测试结束时关闭。"""

    def __enter__(self) -> RecordingWebhookServer:
        """启动本地 HTTP 接收端，并返回带动态端口的 server 对象。"""

        self.server = RecordingWebhookServer(("127.0.0.1", 0), RecordingWebhookHandler)
        self.server.received = []
        self.server.signing_secret = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        """关闭本地 HTTP 接收端，避免端口和线程泄漏影响后续测试。"""

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def server_url(server: RecordingWebhookServer, path: str) -> str:
    """拼接本地 Webhook 接收端 URL，隐藏动态端口细节。"""

    host, port = server.server_address
    return f"http://{host}:{port}{path}"


@pytest.mark.anyio
async def test_webhook_create_list_test_and_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：开发者创建 Endpoint、发送测试事件、接收端校验签名并回写发送记录。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
            developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            created = await client.post(
                "/webhooks",
                headers={
                    "Authorization": f"Bearer {developer_token}",
                    "X-Request-ID": "req_prd025_create",
                },
                json={
                    "name": "预算和安全事件",
                    "endpoint_url": server_url(receiver, "/ok"),
                    "event_types": ["budget.threshold_reached", "security.dlp_matched", "gateway.request_failed"],
                    "timeout_ms": 2000,
                },
            )
            assert created.status_code == 200, created.text
            body = created.json()
            webhook_id = body["webhook"]["webhook_endpoint_id"]
            signing_secret = body["signing_secret"]
            receiver.signing_secret = signing_secret

            tested = await client.post(
                f"/webhooks/{webhook_id}/test",
                headers={
                    "Authorization": f"Bearer {developer_token}",
                    "X-Request-ID": "req_prd025_test",
                },
            )
            listed = await client.get(
                "/webhooks?include_deliveries=true",
                headers={
                    "Authorization": f"Bearer {developer_token}",
                    "X-Request-ID": "req_prd025_list",
                },
            )

    assert body["webhook"]["tenant_id"] == tenant_id
    assert body["webhook"]["secret_prefix"] == signing_secret[:12]
    assert body["webhook"]["secret_fingerprint"]
    assert tested.status_code == 200, tested.text
    delivery = tested.json()["delivery"]
    assert delivery["status"] == "success"
    assert delivery["attempt_count"] == 1
    assert delivery["attempts"][0]["status"] == "success"
    assert delivery["payload"]["tenant_id"] == tenant_id
    assert delivery["payload"]["event_id"].startswith("evt_")
    assert delivery["payload"]["timestamp"].endswith("Z")
    assert receiver.received[0]["event"] == "webhook.test"
    assert receiver.received[0]["signature_valid"] is True
    assert receiver.received[0]["payload"]["tenant_id"] == tenant_id
    assert listed.status_code == 200, listed.text
    listed_body = listed.json()
    assert listed_body["webhooks"][0]["recent_deliveries"][0]["webhook_delivery_id"] == delivery["webhook_delivery_id"]
    assert signing_secret not in listed.text

    connection = connect_database(database_url)
    stored = connection.execute(
        "SELECT encrypted_secret, secret_prefix, tenant_id FROM webhook_endpoints WHERE webhook_endpoint_id=?",
        (webhook_id,),
    ).fetchone()
    assert stored["tenant_id"] == tenant_id
    assert stored["encrypted_secret"].startswith("aesgcm-v1:")
    assert signing_secret not in stored["encrypted_secret"]


@pytest.mark.anyio
async def test_prd081_webhook_exact_wire_cef_and_security_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-081：权限先于正文，CEF/rate 合同和服务端链路 ID 全部由后端执行。"""

    _database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    auth_headers: dict[str, str]
    with RunningWebhookServer() as receiver:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://control",
        ) as client:
            developer_token = await login(
                client,
                "developer@example.com",
                DEVELOPER_PASSWORD,
            )
            viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
            auth_headers = {
                "Authorization": f"Bearer {developer_token}",
                "Content-Type": "application/json",
            }
            malformed = b'{"name":'
            anonymous = await client.post(
                "/webhooks",
                headers={"Content-Type": "application/json"},
                content=malformed,
            )
            viewer = await client.post(
                "/webhooks",
                headers={
                    "Authorization": f"Bearer {viewer_token}",
                    "Content-Type": "application/json",
                },
                content=malformed,
            )
            wrong_content_type = await client.post(
                "/webhooks",
                headers={
                    "Authorization": f"Bearer {developer_token}",
                    "Content-Type": "text/plain",
                },
                content=b"{}",
            )
            duplicate_key = await client.post(
                "/webhooks",
                headers=auth_headers,
                content=(
                    b'{"name":"first","name":"second","endpoint_url":'
                    b'"http://127.0.0.1/hook","event_types":["security.event_pushed"]}'
                ),
            )
            nested: object = "leaf"
            for _ in range(9):
                nested = {"next": nested}
            deep_body = await client.post(
                "/webhooks",
                headers=auth_headers,
                content=json.dumps(
                    {
                        "name": "deep",
                        "endpoint_url": "http://127.0.0.1/hook",
                        "event_types": ["security.event_pushed"],
                        "future": nested,
                    }
                ).encode("utf-8"),
            )
            node_body = await client.post(
                "/webhooks",
                headers=auth_headers,
                content=json.dumps(
                    {
                        "name": "nodes",
                        "endpoint_url": "http://127.0.0.1/hook",
                        "event_types": ["security.event_pushed"] * 260,
                    }
                ).encode("utf-8"),
            )
            oversized = await client.post(
                "/webhooks",
                headers=auth_headers,
                content=json.dumps(
                    {
                        "name": "x" * (16 * 1024),
                        "endpoint_url": "http://127.0.0.1/hook",
                        "event_types": ["security.event_pushed"],
                    }
                ).encode("utf-8"),
            )
            format_type = await client.post(
                "/webhooks",
                headers=auth_headers,
                content=json.dumps(
                    {
                        "name": "format-type",
                        "endpoint_url": "http://127.0.0.1/hook",
                        "event_types": ["security.event_pushed"],
                        "format": True,
                    }
                ).encode("utf-8"),
            )
            rate_type = await client.post(
                "/webhooks",
                headers=auth_headers,
                content=json.dumps(
                    {
                        "name": "rate-type",
                        "endpoint_url": "http://127.0.0.1/hook",
                        "event_types": ["security.event_pushed"],
                        "security_event_rate_limit_per_minute": True,
                    }
                ).encode("utf-8"),
            )
            rate_range = await client.post(
                "/webhooks",
                headers=auth_headers,
                json={
                    "name": "rate-range",
                    "endpoint_url": "http://127.0.0.1/hook",
                    "event_types": ["security.event_pushed"],
                    "security_event_rate_limit_per_minute": 0,
                },
            )
            mixed_cef = await client.post(
                "/webhooks",
                headers=auth_headers,
                json={
                    "name": "mixed-cef",
                    "endpoint_url": "http://127.0.0.1/hook",
                    "event_types": [
                        "security.event_pushed",
                        "security.dlp_matched",
                    ],
                    "format": "cef",
                },
            )
            client_request_id = "req_client_body_canary"
            client_trace_id = "trc_client_body_canary"
            created = await client.post(
                "/webhooks",
                headers={
                    **auth_headers,
                    "X-Request-ID": client_request_id,
                    "X-Trace-ID": client_trace_id,
                },
                json={
                    "name": "CEF SIEM",
                    "endpoint_url": server_url(receiver, "/cef"),
                    "event_types": ["security.event_pushed"],
                    "format": "cef",
                    "security_event_rate_limit_per_minute": 1,
                    "timeout_ms": 1000,
                },
            )
            assert created.status_code == 200, created.text
            created_body = created.json()
            endpoint_id = created_body["webhook"]["webhook_endpoint_id"]
            receiver.signing_secret = created_body["signing_secret"]
            tested = await client.post(
                f"/webhooks/{endpoint_id}/test",
                headers={"Authorization": f"Bearer {developer_token}"},
            )
            listed = await client.get(
                "/webhooks?include_deliveries=true",
                headers={"Authorization": f"Bearer {developer_token}"},
            )

    assert anonymous.status_code == 401
    assert viewer.status_code == 403
    assert viewer.json()["code"] == "auth.permission_denied"
    for response in (
        anonymous,
        viewer,
        wrong_content_type,
        duplicate_key,
        deep_body,
        node_body,
        oversized,
        format_type,
        rate_type,
        rate_range,
        mixed_cef,
        created,
        tested,
        listed,
    ):
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Request-ID"]
        assert response.headers["X-Trace-ID"]
    for response in (
        wrong_content_type,
        duplicate_key,
        deep_body,
        node_body,
        oversized,
    ):
        assert response.status_code == 422
        assert response.json()["code"] == "infra.validation_failed"
    assert format_type.json()["code"] == "webhook.format_invalid"
    assert rate_type.json()["code"] == "webhook.security_rate_limit_invalid"
    assert rate_range.json()["code"] == "webhook.security_rate_limit_invalid"
    assert mixed_cef.json()["code"] == "webhook.format_invalid"
    assert created_body["request_id"] == created.headers["X-Request-ID"]
    assert created_body["trace_id"] == created.headers["X-Trace-ID"]
    assert created_body["request_id"] != client_request_id
    assert created_body["trace_id"] != client_trace_id
    assert created_body["webhook"]["tenant_id"] == tenant_id
    assert created_body["webhook"]["format"] == "cef"
    assert created_body["webhook"]["security_event_rate_limit_per_minute"] == 1
    assert created_body["webhook"]["event_types"] == ["security.event_pushed"]
    assert tested.status_code == 200, tested.text
    assert tested.json()["delivery"]["format"] == "cef"
    assert tested.json()["delivery"]["security_event_rate_limit_per_minute"] == 1
    assert len(receiver.received) == 1
    received = receiver.received[0]
    assert received["payload"] is None
    assert received["content_type"] == "text/plain; charset=utf-8"
    assert received["event"] == "webhook.test"
    assert received["body"].startswith(
        b"CEF:0|MetaHub|AI Gateway|1|webhook.test|Webhook test|1|"
    )
    assert received["signature_valid"] is True
    assert listed.status_code == 200, listed.text
    listed_endpoint = listed.json()["webhooks"][0]
    assert listed_endpoint["format"] == "cef"
    assert listed_endpoint["security_event_rate_limit_per_minute"] == 1
    for forbidden in ("wire_body_text", "encrypted_secret", "X-MetaHub-Signature"):
        assert forbidden not in created.text
        assert forbidden not in listed.text


@pytest.mark.anyio
async def test_webhook_permission_validation_and_timeout_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：viewer 被拒绝，非法配置返回统一错误码，超时会写入 3 次失败 attempt。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()

    class TimeoutClient:
        """替代 httpx.Client 的测试桩，模拟每次 Webhook POST 都超时。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """保存构造参数但不建立真实网络连接，保证超时分支测试稳定。"""

            self.args = args
            self.kwargs = kwargs

        def __enter__(self) -> "TimeoutClient":
            """进入上下文时返回自身，匹配 httpx.Client 的使用方式。"""

            return self

        def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
            """退出上下文不做网络清理，因为测试桩没有打开连接。"""

            return None

        class TimeoutStream:
            """在进入 streaming 响应上下文时抛出超时，匹配真实 httpx 调用阶段。"""

            def __enter__(self) -> httpx.Response:
                """模拟等待接收端响应头超时，不返回任何响应正文。"""

                raise httpx.TimeoutException("receiver timed out")

            def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
                """超时上下文没有网络资源，退出时无需清理。"""

                return None

        def stream(self, *_args: Any, **_kwargs: Any) -> "TimeoutClient.TimeoutStream":
            """返回超时 streaming 上下文，让服务层写入固定 webhook.timeout attempt。"""

            return self.TimeoutStream()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        viewer_denied = await client.get("/webhooks", headers={"Authorization": f"Bearer {viewer_token}"})
        invalid_url = await client.post(
            "/webhooks",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={"name": "bad", "endpoint_url": "ftp://example.com/hook", "event_types": ["gateway.request_failed"]},
        )
        invalid_event = await client.post(
            "/webhooks",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={"name": "bad", "endpoint_url": "http://127.0.0.1/hook", "event_types": ["unknown.event"]},
        )
        unknown_field = await client.post(
            "/webhooks",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={
                "name": "bad",
                "endpoint_url": "http://127.0.0.1/hook",
                "event_types": ["gateway.request_failed"],
                "tenant_id": "tenant_injected",
            },
        )
        created = await client.post(
            "/webhooks",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={
                "name": "超时接收端",
                "endpoint_url": "http://127.0.0.1:65530/hook",
                "event_types": ["gateway.request_failed"],
                "timeout_ms": 1000,
            },
        )
        webhook_id = created.json()["webhook"]["webhook_endpoint_id"]
        viewer_test_denied = await client.post(
            f"/webhooks/{webhook_id}/test",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        missing = await client.post(
            "/webhooks/wh_missing/test",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        monkeypatch.setattr(webhook_service.httpx, "Client", TimeoutClient)
        timed_out = await client.post(
            f"/webhooks/{webhook_id}/test",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd025_timeout"},
        )

    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert invalid_url.status_code == 422
    assert invalid_url.json()["code"] == "webhook.endpoint_invalid"
    assert invalid_event.status_code == 422
    assert invalid_event.json()["code"] == "webhook.event_type_invalid"
    assert unknown_field.status_code == 422
    assert unknown_field.json()["code"] == "infra.validation_failed"
    assert unknown_field.json()["request_id"]
    assert viewer_test_denied.status_code == 403
    assert viewer_test_denied.json()["code"] == "auth.permission_denied"
    assert missing.status_code == 404
    assert missing.json()["code"] == "webhook.endpoint_not_found"
    assert timed_out.status_code == 200, timed_out.text
    delivery = timed_out.json()["delivery"]
    assert delivery["tenant_id"] == tenant_id
    assert delivery["status"] == "failed"
    assert delivery["attempt_count"] == 3
    assert len(delivery["attempts"]) == 3
    assert {attempt["error_code"] for attempt in delivery["attempts"]} == {"webhook.timeout"}

    connection = connect_database(database_url)
    attempts = connection.execute(
        "SELECT COUNT(*) AS count FROM webhook_delivery_attempts WHERE webhook_delivery_id=?",
        (delivery["webhook_delivery_id"],),
    ).fetchone()
    stored_delivery = connection.execute(
        "SELECT status, error_code FROM webhook_deliveries WHERE webhook_delivery_id=?",
        (delivery["webhook_delivery_id"],),
    ).fetchone()
    assert int(attempts["count"]) == 3
    assert stored_delivery["status"] == "failed"
    assert stored_delivery["error_code"] == "webhook.timeout"


@pytest.mark.anyio
async def test_webhook_replay_filter_rotate_secret_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-042 主流程：失败记录可重放、同键幂等、详情过滤和 Secret 轮换验签全部可追踪。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
            developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            created = await client.post(
                "/webhooks",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={
                    "name": "失败重放接收端",
                    "endpoint_url": server_url(receiver, "/fail"),
                    "event_types": ["gateway.request_failed"],
                    "timeout_ms": 1000,
                },
            )
            assert created.status_code == 200, created.text
            endpoint_id = created.json()["webhook"]["webhook_endpoint_id"]
            old_secret = created.json()["signing_secret"]
            receiver.signing_secret = old_secret
            original = await client.post(
                f"/webhooks/{endpoint_id}/test",
                headers={"Authorization": f"Bearer {developer_token}"},
            )
            assert original.status_code == 200, original.text
            source = original.json()["delivery"]
            assert source["status"] == "failed"
            assert source["secret_version"] == 1
            assert len(receiver.received) == 3
            assert all(item["signature_valid"] for item in receiver.received)

            rotated = await client.post(
                f"/webhooks/{endpoint_id}/rotate-secret",
                headers={
                    "Authorization": f"Bearer {developer_token}",
                    "X-Request-ID": "req_prd042_rotate",
                },
            )
            assert rotated.status_code == 200, rotated.text
            new_secret = rotated.json()["signing_secret"]
            assert new_secret != old_secret
            assert rotated.json()["webhook"]["secret_version"] == 2
            receiver.signing_secret = new_secret

            replay_payload = {
                "webhook_delivery_id": source["webhook_delivery_id"],
                "idempotency_key": "console_prd042_same_key",
            }
            replayed = await client.post(
                f"/webhooks/{endpoint_id}/replay",
                headers={
                    "Authorization": f"Bearer {developer_token}",
                    "X-Request-ID": "req_prd042_replay",
                },
                json=replay_payload,
            )
            received_after_first_replay = len(receiver.received)
            duplicate = await client.post(
                f"/webhooks/{endpoint_id}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json=replay_payload,
            )
            details = await client.get(
                f"/webhooks/{endpoint_id}/deliveries?event_type=webhook.test&status=failed&limit=100",
                headers={"Authorization": f"Bearer {developer_token}"},
            )
            listed = await client.get(
                "/webhooks?include_deliveries=false",
                headers={"Authorization": f"Bearer {developer_token}"},
            )

    assert replayed.status_code == 200, replayed.text
    replay_delivery = replayed.json()["delivery"]
    assert replayed.json()["replayed"] is True
    assert replay_delivery["status"] == "failed"
    assert replay_delivery["delivery_kind"] == "replay"
    assert replay_delivery["replayed_from_delivery_id"] == source["webhook_delivery_id"]
    assert replay_delivery["event_id"] == source["event_id"]
    assert replay_delivery["secret_version"] == 2
    assert received_after_first_replay == 6
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["replayed"] is False
    assert duplicate.json()["delivery"]["webhook_delivery_id"] == replay_delivery["webhook_delivery_id"]
    assert len(receiver.received) == received_after_first_replay
    for received in receiver.received[3:]:
        assert received["signature_valid"] is True
        # 使用同一 body 和签名头验证旧 Secret，排除时间戳或正文差异造成的假失败。
        assert verify_webhook_signature(received["signature"], old_secret, received["body"]) is False

    assert details.status_code == 200, details.text
    detail_body = details.json()
    assert len(detail_body["deliveries"]) == 2
    assert all(item["status"] == "failed" for item in detail_body["deliveries"])
    assert all("payload" not in item for item in detail_body["deliveries"])
    assert "replay_idempotency_key" not in details.text
    assert "webhook.replayed" not in listed.json()["event_types"]
    assert old_secret not in rotated.text

    connection = connect_database(database_url)
    audits = connection.execute(
        """
        SELECT action, before_summary_json, after_summary_json
        FROM audit_logs WHERE tenant_id=? AND action LIKE 'webhook_%' ORDER BY created_at
        """,
        (tenant_id,),
    ).fetchall()
    # 两个动作可能落在同一秒，审计合同只要求事实完整，不依赖无业务意义的同秒插入顺序。
    assert {row["action"] for row in audits} == {
        "webhook_endpoint.secret_rotated",
        "webhook_delivery.replayed",
    }
    audit_text = "".join(row["before_summary_json"] + row["after_summary_json"] for row in audits)
    assert old_secret not in audit_text
    assert new_secret not in audit_text
    assert "console_prd042_same_key" not in audit_text


@pytest.mark.anyio
async def test_webhook_replay_permission_tenant_endpoint_and_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-042 异常/权限：viewer、成功记录、跨租户/Endpoint、禁用状态和非法输入均被后端拒绝。"""

    database_url, _tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
            anonymous_details = await client.get("/webhooks/wh_missing/deliveries")
            anonymous_replay = await client.post(
                "/webhooks/wh_missing/replay",
                json={"webhook_delivery_id": "whd_missing", "idempotency_key": "console_anonymous_01"},
            )
            anonymous_rotate = await client.post("/webhooks/wh_missing/rotate-secret")
            developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
            created = await client.post(
                "/webhooks",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={
                    "name": "成功接收端",
                    "endpoint_url": server_url(receiver, "/ok"),
                    "event_types": ["gateway.request_failed"],
                },
            )
            endpoint_id = created.json()["webhook"]["webhook_endpoint_id"]
            receiver.signing_secret = created.json()["signing_secret"]
            connection = connect_database(database_url)
            connection.execute(
                """
                INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
                VALUES('tenant_prd042_other', 'prd042-other', '其他租户', 'internal_only', 'active',
                       '2026-07-16T00:00:00Z', '2026-07-16T00:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO webhook_endpoints(
                  webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
                  encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
                  max_attempts, secret_version, created_at, updated_at
                )
                SELECT
                  'wh_other_tenant', 'tenant_prd042_other', '其他租户 Endpoint', endpoint_url,
                  event_types_json, encrypted_secret, secret_prefix, secret_fingerprint,
                  status, timeout_ms, max_attempts, secret_version, created_at, updated_at
                FROM webhook_endpoints WHERE webhook_endpoint_id=?
                """,
                (endpoint_id,),
            )
            connection.commit()
            successful = await client.post(
                f"/webhooks/{endpoint_id}/test",
                headers={"Authorization": f"Bearer {developer_token}"},
            )
            delivery_id = successful.json()["delivery"]["webhook_delivery_id"]
            replay_body = {"webhook_delivery_id": delivery_id, "idempotency_key": "console_success_01"}
            viewer_replay = await client.post(
                f"/webhooks/{endpoint_id}/replay",
                headers={"Authorization": f"Bearer {viewer_token}"},
                json=replay_body,
            )
            viewer_rotate = await client.post(
                f"/webhooks/{endpoint_id}/rotate-secret",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
            success_replay = await client.post(
                f"/webhooks/{endpoint_id}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json=replay_body,
            )
            injected = await client.post(
                f"/webhooks/{endpoint_id}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={**replay_body, "tenant_id": "other", "signing_secret": "do-not-accept"},
            )
            invalid_key = await client.post(
                f"/webhooks/{endpoint_id}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={"webhook_delivery_id": delivery_id, "idempotency_key": "bad key"},
            )
            missing_key = await client.post(
                f"/webhooks/{endpoint_id}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={"webhook_delivery_id": delivery_id},
            )
            invalid_filter = await client.get(
                f"/webhooks/{endpoint_id}/deliveries?status=unknown&limit=101",
                headers={"Authorization": f"Bearer {developer_token}"},
            )
            invalid_limit_type = await client.get(
                f"/webhooks/{endpoint_id}/deliveries?limit=abc",
                headers={"Authorization": f"Bearer {developer_token}"},
            )
            missing_tenant = await client.get(
                "/webhooks/wh_other_tenant/deliveries",
                headers={"Authorization": f"Bearer {developer_token}"},
            )

    assert anonymous_details.status_code == 401
    assert anonymous_replay.status_code == 401
    assert anonymous_rotate.status_code == 401
    assert viewer_replay.status_code == 403
    assert viewer_replay.json()["code"] == "auth.permission_denied"
    assert viewer_rotate.status_code == 403
    assert success_replay.status_code == 409
    assert success_replay.json()["code"] == "webhook.delivery_not_replayable"
    assert injected.status_code == 422
    assert injected.json()["code"] == "infra.validation_failed"
    assert invalid_key.status_code == 422
    assert invalid_key.json()["code"] == "webhook.idempotency_key_invalid"
    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "webhook.idempotency_key_invalid"
    assert invalid_filter.status_code == 422
    assert invalid_filter.json()["code"] == "webhook.delivery_filter_invalid"
    assert invalid_limit_type.status_code == 422
    assert invalid_limit_type.json()["code"] == "webhook.delivery_filter_invalid"
    assert missing_tenant.status_code == 404
    assert missing_tenant.json()["code"] == "webhook.endpoint_not_found"

    connection = connect_database(database_url)
    connection.execute(
        "UPDATE webhook_endpoints SET status='disabled' WHERE webhook_endpoint_id=?",
        (endpoint_id,),
    )
    # 先把来源置为真实 failed，再验证 disabled Endpoint 闸门；成功来源已在上文单独验证不可重放。
    connection.execute(
        "UPDATE webhook_deliveries SET status='failed' WHERE webhook_delivery_id=?",
        (delivery_id,),
    )
    connection.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        disabled_test = await client.post(
            f"/webhooks/{endpoint_id}/test",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        disabled_replay = await client.post(
            f"/webhooks/{endpoint_id}/replay",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={"webhook_delivery_id": delivery_id, "idempotency_key": "console_disabled_01"},
        )
    assert disabled_test.status_code == 409
    assert disabled_test.json()["code"] == "webhook.endpoint_disabled"
    assert disabled_replay.status_code == 409
    assert disabled_replay.json()["code"] == "webhook.endpoint_disabled"


@pytest.mark.anyio
async def test_webhook_delivery_detail_returns_latest_one_hundred(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-042 有界查询：101 条历史记录只返回最新 100 条，排序稳定且不受当前订阅变化影响。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        created = await client.post(
            "/webhooks",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={
                "name": "历史记录接收端",
                "endpoint_url": "http://127.0.0.1:65530/fail",
                "event_types": ["gateway.request_failed"],
            },
        )
        endpoint_id = created.json()["webhook"]["webhook_endpoint_id"]
        connection = connect_database(database_url)
        connection.executemany(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
              event_timestamp, payload_json, status, attempt_count, max_attempts,
              secret_version, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, 'gateway.request_failed', ?, ?, 'failed', 3, 3, 1, ?, ?)
            """,
            [
                (
                    f"whd_{index:032x}",
                    tenant_id,
                    endpoint_id,
                    f"evt_history_{index:03d}",
                    f"2026-07-16T00:{index // 60:02d}:{index % 60:02d}Z",
                    json.dumps({"event_id": f"evt_history_{index:03d}", "payload": {"index": index}}),
                    f"2026-07-16T00:{index // 60:02d}:{index % 60:02d}Z",
                    f"2026-07-16T00:{index // 60:02d}:{index % 60:02d}Z",
                )
                for index in range(101)
            ],
        )
        connection.commit()
        # 修改当前订阅后仍应能按历史 event_type 查询，证明筛选不依赖 event_types_json。
        connection.execute(
            "UPDATE webhook_endpoints SET event_types_json='[]' WHERE webhook_endpoint_id=?",
            (endpoint_id,),
        )
        connection.commit()
        details = await client.get(
            f"/webhooks/{endpoint_id}/deliveries?event_type=gateway.request_failed&status=failed&limit=100",
            headers={"Authorization": f"Bearer {developer_token}"},
        )

    assert details.status_code == 200, details.text
    deliveries = details.json()["deliveries"]
    assert len(deliveries) == 100
    # 0047 以正式 Delivery ID 的十六进制主体派生死信 ID，历史夹具也遵守同一格式。
    assert deliveries[0]["webhook_delivery_id"] == f"whd_{100:032x}"
    assert deliveries[-1]["webhook_delivery_id"] == f"whd_{1:032x}"


@pytest.mark.anyio
async def test_webhook_replay_concurrent_same_key_sends_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-042 并发幂等：两个线程使用同一来源和幂等键时只创建一个 replay 并发送一组 attempts。"""

    database_url, _tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        # 先通过 API 创建失败来源，避免并发测试绕过正常 HMAC 和 delivery 写入路径。
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
            token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            created = await client.post(
                "/webhooks",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "并发重放接收端",
                    "endpoint_url": server_url(receiver, "/fail"),
                    "event_types": ["gateway.request_failed"],
                },
            )
            endpoint_id = created.json()["webhook"]["webhook_endpoint_id"]
            receiver.signing_secret = created.json()["signing_secret"]
            tested = await client.post(
                f"/webhooks/{endpoint_id}/test",
                headers={"Authorization": f"Bearer {token}"},
            )
            source_delivery_id = tested.json()["delivery"]["webhook_delivery_id"]
        receiver.received.clear()
        context = authenticate_bearer(app.state.config, f"Bearer {token}")
        request_payload = ReplayWebhookRequest(
            webhook_delivery_id=source_delivery_id,
            idempotency_key="console_concurrent_same_key",
        )

        def run_replay(index: int):
            """在线程内调用同步服务，使用独立 request_id 验证数据库幂等而非请求 ID 去重。"""

            return webhook_service.replay_webhook(
                app.state.config,
                context,
                endpoint_id,
                request_payload,
                request_id=f"req_prd042_concurrent_{index}",
                trace_id=f"trc_prd042_concurrent_{index}",
                audit_request_meta=AuditRequestMeta(client_ip="127.0.0.1", user_agent="pytest"),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run_replay, range(2)))

    assert results[0].delivery.webhook_delivery_id == results[1].delivery.webhook_delivery_id
    assert sorted(result.replayed for result in results) == [False, True]
    assert len(receiver.received) == 3
    connection = connect_database(database_url)
    replay_count = connection.execute(
        """
        SELECT COUNT(*) AS count FROM webhook_deliveries
        WHERE replayed_from_delivery_id=? AND replay_idempotency_key=?
        """,
        (source_delivery_id, "console_concurrent_same_key"),
    ).fetchone()
    assert int(replay_count["count"]) == 1


@pytest.mark.anyio
async def test_webhook_replay_rejects_sensitive_or_inconsistent_stored_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-042 安全：递归敏感键或与索引列不一致的历史信封必须零出网拒绝。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
            token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            created = await client.post(
                "/webhooks",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "存量安全校验接收端",
                    "endpoint_url": server_url(receiver, "/fail"),
                    "event_types": ["gateway.request_failed"],
                },
            )
            endpoint_id = created.json()["webhook"]["webhook_endpoint_id"]
            receiver.signing_secret = created.json()["signing_secret"]
            tested = await client.post(
                f"/webhooks/{endpoint_id}/test",
                headers={"Authorization": f"Bearer {token}"},
            )
            source = tested.json()["delivery"]
            receiver.received.clear()
            connection = connect_database(database_url)
            # Simulate legacy/on-disk corruption after proving 0074 normally blocks rewrites.
            with pytest.raises(sqlite3.IntegrityError, match="webhook_delivery_wire_immutable"):
                connection.execute(
                    "UPDATE webhook_deliveries SET payload_json=? WHERE webhook_delivery_id=?",
                    ("{}", source["webhook_delivery_id"]),
                )
            connection.execute("DROP TRIGGER trg_webhook_deliveries_siem_immutable_prd081")
            sensitive_envelope = dict(source["payload"])
            sensitive_envelope["payload"] = {"nested": {"access-token": "must-not-send"}}
            connection.execute(
                "UPDATE webhook_deliveries SET payload_json=? WHERE webhook_delivery_id=?",
                (json.dumps(sensitive_envelope), source["webhook_delivery_id"]),
            )
            connection.commit()
            sensitive = await client.post(
                f"/webhooks/{endpoint_id}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "webhook_delivery_id": source["webhook_delivery_id"],
                    "idempotency_key": "console_sensitive_01",
                },
            )
            inconsistent_envelope = dict(source["payload"])
            inconsistent_envelope["tenant_id"] = "tenant_other"
            connection.execute(
                "UPDATE webhook_deliveries SET payload_json=? WHERE webhook_delivery_id=?",
                (json.dumps(inconsistent_envelope), source["webhook_delivery_id"]),
            )
            connection.commit()
            inconsistent = await client.post(
                f"/webhooks/{endpoint_id}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "webhook_delivery_id": source["webhook_delivery_id"],
                    "idempotency_key": "console_inconsistent_01",
                },
            )

    assert sensitive.status_code == 503
    assert sensitive.json()["code"] == "webhook.replay_unavailable"
    assert inconsistent.status_code == 503
    assert inconsistent.json()["code"] == "webhook.replay_unavailable"
    assert sensitive.json()["request_id"]
    assert sensitive.json()["trace_id"]
    assert len(receiver.received) == 0
    connection = connect_database(database_url)
    replay_rows = connection.execute(
        "SELECT COUNT(*) AS count FROM webhook_deliveries WHERE tenant_id=? AND delivery_kind='replay'",
        (tenant_id,),
    ).fetchone()
    assert int(replay_rows["count"]) == 0


@pytest.mark.anyio
async def test_shared_runtime_uses_rotated_secret_and_current_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-042 轮换竞态：共享投递即使拿到旧批量快照，也必须重读新 Secret 和版本后创建 intent。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
            token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            created = await client.post(
                "/webhooks",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "共享运行时轮换接收端",
                    "endpoint_url": server_url(receiver, "/fail"),
                    "event_types": ["gateway.request_failed"],
                },
            )
            endpoint_id = created.json()["webhook"]["webhook_endpoint_id"]
            old_secret = created.json()["signing_secret"]
            rotated = await client.post(
                f"/webhooks/{endpoint_id}/rotate-secret",
                headers={"Authorization": f"Bearer {token}"},
            )
            new_secret = rotated.json()["signing_secret"]
            receiver.signing_secret = new_secret

        event = build_webhook_event(
            tenant_id=tenant_id,
            event_type="gateway.request_failed",
            event_id="evt_prd042_shared_rotated",
            trace_id="trc_prd042_shared_rotated",
            payload={"request_id": "req_prd042_shared_rotated", "error_code": "gateway.upstream_timeout"},
        )
        matched = dispatch_runtime_webhook_event(app.state.config, event)

    assert matched == 1
    assert len(receiver.received) == 3
    assert all(item["signature_valid"] for item in receiver.received)
    assert all(not verify_webhook_signature(item["signature"], old_secret, item["body"]) for item in receiver.received)
    row = connect_database(database_url).execute(
        """
        SELECT secret_version, status FROM webhook_deliveries
        WHERE tenant_id=? AND webhook_endpoint_id=? AND event_id=?
        """,
        (tenant_id, endpoint_id, "evt_prd042_shared_rotated"),
    ).fetchone()
    assert row["secret_version"] == 2
    assert row["status"] == "failed"
