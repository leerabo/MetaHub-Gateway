"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_providers.py
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.audit.routes import router as audit_router
from metahub_control.providers.routes import router as providers_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app


# 以下测试密码只用于 Provider 管理权限矩阵，覆盖管理员写入和普通用户拒绝路径。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """sqlite_url：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """build_control_app：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    app = create_service_app("control-plane-api", "Provider Test Control API")
    app.include_router(auth_router)
    app.include_router(providers_router)
    app.include_router(audit_router)
    return app


@pytest.fixture()
def fake_provider_endpoint():
    """fake_provider_endpoint：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    class Handler(BaseHTTPRequestHandler):
        """Handler：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        def do_GET(self) -> None:  # noqa: N802
            """do_GET：说明该定义的测试或脚本职责。
            
            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            if self.path == "/v1/models":
                self._json({"object": "list", "data": [{"id": "local-chat"}]})
                return
            # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
            if self.path == "/auth-fail/models":
                self._json({"error": "unauthorized"}, status_code=401)
                return
            # 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
            if self.path == "/plain-base/models":
                self.send_response(404)
                self.end_headers()
                return
            if self.path == "/plain-base":
                self._json({"status": "ok"})
                return
            self._json({"error": "not found"}, status_code=404)

        def _json(self, payload: dict[str, Any], status_code: int = 200) -> None:
            """_json：说明该定义的测试或脚本职责。
            
            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            self.send_response(status_code)
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
        origin = f"http://{host}:{port}"
        yield {
            "success": f"{origin}/v1",
            "auth_fail": f"{origin}/auth-fail",
            "fallback_success": f"{origin}/plain-base",
        }
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
                "user_prd011_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd011_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
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
async def test_provider_create_list_test_and_disable_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_provider_endpoint,
) -> None:
    """主流程/安全：管理员可配置至少两个 Provider，并可测试连接和禁用。"""

    database_url = prepare_database(monkeypatch, tmp_path, fake_provider_endpoint["success"])

    def _local_probe_url(
        config: object,
        value: str,
        *,
        probe_profile: str = "openai_models",
    ) -> str:
        """仅在本地 stub 测试中跳过回环 SSRF 拒绝，仍按 spec 选择探测路径。"""

        del config
        base = value.rstrip("/")
        return f"{base}/models" if probe_profile == "openai_models" else base

    monkeypatch.setattr(
        "metahub_control.provider_health.service._validated_probe_url",
        _local_probe_url,
    )
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd011_admin"}

        deepseek = await client.post(
            "/providers",
            headers=headers,
            json={
                "provider_code": "deepseek_official",
                "name": "DeepSeek Official",
                "provider_type": "deepseek",
                "base_url": fake_provider_endpoint["success"],
                "auth_type": "bearer",
                "description": "prd011 success provider",
            },
        )
        local = await client.post(
            "/providers",
            headers=headers,
            json={
                "provider_code": "local_lab",
                "name": "Local Lab Endpoint",
                "provider_type": "openai_compatible",
                "base_url": fake_provider_endpoint["fallback_success"],
                "auth_type": "none",
            },
        )
        listed = await client.get("/providers", headers=headers)

        success_test = await client.post(
            f"/providers/{deepseek.json()['provider']['provider_id']}/test",
            headers=headers,
            json={"timeout_seconds": 2},
        )
        fallback_test = await client.post(
            f"/providers/{local.json()['provider']['provider_id']}/test",
            headers=headers,
            json={"timeout_seconds": 2},
        )
        failure = await client.post(
            "/providers",
            headers=headers,
            json={
                "provider_code": "auth_fail_lab",
                "name": "Auth Fail Endpoint",
                "provider_type": "openai_compatible",
                "base_url": fake_provider_endpoint["auth_fail"],
                "auth_type": "bearer",
            },
        )
        failed_test = await client.post(
            f"/providers/{failure.json()['provider']['provider_id']}/test",
            headers=headers,
            json={"timeout_seconds": 2},
        )
        # 运行诊断不是业务引用：空 Provider 即使有健康探测日志，也应允许删除。
        deepseek_id = deepseek.json()["provider"]["provider_id"]
        connection = connect_database(database_url)
        deepseek_tenant_id = connection.execute(
            "SELECT tenant_id FROM providers WHERE provider_id=?", (deepseek_id,)
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO provider_health_probe_logs(
              probe_log_id, tenant_id, provider_id, provider_credential_id,
              health_generation, attempt_sequence, trigger_type, result_status,
              auth_result, http_status, latency_ms, error_code, result_summary,
              request_id, trace_id, checked_at, created_at
            ) VALUES(?, ?, ?, NULL, 0, 1, 'manual', 'success', 'unverified',
                      200, 8, NULL, 'Endpoint reachable', ?, ?, datetime('now'), datetime('now'))
            """,
            ("probe_delete_guard", deepseek_tenant_id, deepseek_id,
             "req_probe_delete_guard", "trc_probe_delete_guard"),
        )
        connection.commit()
        connection.close()
        disabled = await client.patch(
            f"/providers/{local.json()['provider']['provider_id']}",
            headers=headers,
            json={"status": "disabled"},
        )
        deleted = await client.delete(
            f"/providers/{deepseek.json()['provider']['provider_id']}",
            headers=headers,
        )

        # Provider 一旦被模型引用就必须保留，以免破坏路由和历史计量事实。
        connection = connect_database(database_url)
        connection.execute(
            """
            INSERT INTO models(
              model_id, tenant_id, provider_id, model_alias, upstream_model_name, provider_model_id,
              display_name, capabilities, context_window, input_price_per_1k, output_price_per_1k,
              currency, price_schema_version, status, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
            """,
            (
                "model_provider_delete_guard",
                connection.execute("SELECT tenant_id FROM providers WHERE provider_id=?", (local.json()["provider"]["provider_id"],)).fetchone()[0],
                local.json()["provider"]["provider_id"],
                "provider-delete-guard",
                "provider-delete-guard",
                "provider-delete-guard",
                "Provider delete guard",
                json.dumps({"chat": True, "stream": True}),
                8192,
                0,
                0,
                "CNY",
                "test-v1",
            ),
        )
        connection.commit()
        connection.close()
        in_use = await client.delete(
            f"/providers/{local.json()['provider']['provider_id']}",
            headers=headers,
        )

    assert deepseek.status_code == 201, deepseek.text
    assert local.status_code == 201, local.text
    providers = listed.json()["providers"]
    assert len(providers) >= 3
    assert "key_hash" not in listed.text
    assert "plain_value" not in listed.text
    assert "sk-" not in listed.text

    assert success_test.status_code == 200
    assert success_test.json()["test_result"]["status"] == "success"
    assert success_test.json()["provider"]["health_status"] == "healthy"
    assert fallback_test.json()["test_result"]["status"] == "success"
    assert failed_test.json()["test_result"]["status"] == "failed"
    assert failed_test.json()["provider"]["health_status"] == "unhealthy"
    assert "authentication failed" in failed_test.json()["test_result"]["message"]
    assert disabled.status_code == 200
    assert disabled.json()["provider"]["status"] == "disabled"
    assert disabled.json()["provider"]["disabled_at"] is not None
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_provider_id"] == deepseek.json()["provider"]["provider_id"]
    assert in_use.status_code == 409, in_use.text
    assert in_use.json()["code"] == "provider.in_use"

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT 1 FROM providers WHERE provider_id=?",
        (deepseek.json()["provider"]["provider_id"],),
    ).fetchone() is None
    row = connection.execute(
        "SELECT health_status, last_tested_at, last_test_error FROM providers WHERE provider_code=?",
        ("auth_fail_lab",),
    ).fetchone()
    assert row["health_status"] == "unhealthy"
    assert row["last_tested_at"] is not None
    assert "authentication failed" in row["last_test_error"]


@pytest.mark.anyio
async def test_provider_permissions_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_provider_endpoint,
) -> None:
    """异常/权限：非管理员被后端拒绝，非法字段返回 provider.* 错误码。"""

    prepare_database(monkeypatch, tmp_path, fake_provider_endpoint["success"])
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        developer_denied = await client.get(
            "/providers",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        viewer_denied = await client.post(
            "/providers",
            headers={"Authorization": f"Bearer {viewer_token}"},
            json={
                "provider_code": "viewer_try",
                "name": "Viewer Try",
                "base_url": fake_provider_endpoint["success"],
            },
        )
        invalid_base_urls = [
            await client.post(
                "/providers",
                headers=admin_headers,
                json={
                    "provider_code": f"bad_url_{index}",
                    "name": "Bad URL",
                    "base_url": base_url,
                },
            )
            for index, base_url in enumerate(
                (
                    "ftp://example.com",
                    "https://user:provider-key-sentinel@example.com/v1",
                    "https://example.com/v1?access_token=token-sentinel",
                    "https://example.com/v1#token-sentinel",
                    "https://example.com:99999/v1",
                ),
                start=1,
            )
        ]
        # 竞品平台名称不能进入模型厂商白名单，三种常见拼写都必须由后端拒绝。
        invalid_types = [
            await client.post(
                "/providers",
                headers=admin_headers,
                json={
                    "provider_code": f"bad_type_{index}",
                    "name": "Bad Competitor Type",
                    "provider_type": provider_type,
                    "base_url": fake_provider_endpoint["success"],
                },
            )
            for index, provider_type in enumerate(
                ["openrouter", "siliconflow", "silicon_flow"],
                start=1,
            )
        ]
        invalid_auth = await client.post(
            "/providers",
            headers=admin_headers,
            json={
                "provider_code": "bad_auth",
                "name": "Bad Auth",
                "auth_type": "api_key",
                "base_url": fake_provider_endpoint["success"],
            },
        )
        created = await client.post(
            "/providers",
            headers=admin_headers,
            json={
                "provider_code": "duplicate_lab",
                "name": "Duplicate Lab",
                "base_url": fake_provider_endpoint["success"],
            },
        )
        duplicate = await client.post(
            "/providers",
            headers=admin_headers,
            json={
                "provider_code": "duplicate_lab",
                "name": "Duplicate Lab 2",
                "base_url": fake_provider_endpoint["success"],
            },
        )
        empty_patch = await client.patch(
            f"/providers/{created.json()['provider']['provider_id']}",
            headers=admin_headers,
            json={},
        )
        not_found = await client.patch(
            "/providers/not-found",
            headers=admin_headers,
            json={"status": "disabled"},
        )

    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert all(response.status_code == 422 for response in invalid_base_urls)
    assert all(response.json()["code"] == "provider.base_url_invalid" for response in invalid_base_urls)
    assert all(response.json()["request_id"] for response in invalid_base_urls)
    assert all(response.status_code == 422 for response in invalid_types)
    assert all(response.json()["code"] == "provider.type_invalid" for response in invalid_types)
    assert invalid_auth.status_code == 422
    assert invalid_auth.json()["code"] == "provider.auth_type_invalid"
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "provider.code_exists"
    assert empty_patch.status_code == 422
    assert empty_patch.json()["code"] == "provider.update_empty"
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "provider.not_found"


@pytest.mark.anyio
async def test_provider_endpoint_is_absent_from_runtime_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_provider_endpoint,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """安全回归：创建和更新 Provider 时，运行日志不泄露客户 Endpoint 或凭据哨兵。"""

    database_url = prepare_database(monkeypatch, tmp_path, fake_provider_endpoint["success"])
    control_app = build_control_app()
    endpoint_before = (
        "https://customer-a.private.example:9443/v1/private/"
        "provider-key-value-sentinel/token-value-sentinel"
    )
    endpoint_after = (
        "https://customer-b.private.example:10443/internal/models/"
        "provider-key-updated-sentinel/token-updated-sentinel"
    )
    caplog.set_level("INFO", logger="metahub.providers")

    async with AsyncClient(
        transport=ASGITransport(app=control_app),
        base_url="http://control",
    ) as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Request-ID": "req_provider_endpoint_log_create",
        }
        # 登录日志与本断言无关；清空后只检查携带客户 Endpoint 的管理操作。
        caplog.clear()
        created = await client.post(
            "/providers",
            headers=headers,
            json={
                "provider_code": "private_endpoint_log_guard",
                "name": "Private Endpoint Log Guard",
                "provider_type": "openai_compatible",
                "base_url": endpoint_before,
                "auth_type": "bearer",
            },
        )
        assert created.status_code == 201, created.text
        provider_id = created.json()["provider"]["provider_id"]
        updated = await client.patch(
            f"/providers/{provider_id}",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_provider_endpoint_log_update",
            },
            json={"base_url": endpoint_after},
        )
        audit_response = await client.get(
            f"/audit-logs?object_id={provider_id}&limit=20",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_provider_endpoint_audit_query",
            },
        )

    assert updated.status_code == 200, updated.text
    assert audit_response.status_code == 200, audit_response.text
    provider_records = [record for record in caplog.records if record.name == "metahub.providers"]
    created_record = next(
        record for record in provider_records if getattr(record, "event", None) == "provider.created"
    )
    # 运行日志只保留路由分类所需的非敏感元数据，Endpoint 字段本身也不能出现。
    assert created_record.payload == {
        "provider_code": "private_endpoint_log_guard",
        "provider_type": "openai_compatible",
        "auth_type": "bearer",
    }
    runtime_serialized = json.dumps(
        [
            {
                "logger": record.name,
                "message": record.getMessage(),
                "event": getattr(record, "event", None),
                "payload": getattr(record, "payload", None),
                "path": getattr(record, "path", None),
                "method": getattr(record, "method", None),
            }
            for record in caplog.records
        ],
        ensure_ascii=False,
        default=str,
    )
    assert "base_url" not in runtime_serialized
    # 使用完整 URL、host、path、Provider Key 和 Token 哨兵逐项证明运行日志无正文泄漏。
    forbidden_fragments = (
        endpoint_before,
        endpoint_after,
        "customer-a.private.example",
        "customer-b.private.example",
        "/v1/private/",
        "/internal/models/",
        "provider-key-value-sentinel",
        "provider-key-updated-sentinel",
        "token-value-sentinel",
        "token-updated-sentinel",
        "user:provider-key-sentinel",
        "access_token",
        "token-sentinel",
    )
    assert all(fragment not in runtime_serialized for fragment in forbidden_fragments)

    # 持久审计只保存完整 URL 的 SHA-256；host/path/Token 哨兵不能通过数据库或查询 API 读回。
    connection = connect_database(database_url)
    audit_rows = connection.execute(
        """
        SELECT action, before_summary_json, after_summary_json, changed_fields_json
        FROM audit_logs
        WHERE object_type='provider' AND object_id=?
        ORDER BY created_at, audit_log_id
        """,
        (provider_id,),
    ).fetchall()
    assert len(audit_rows) == 2
    all_persistent_audit_rows = connection.execute(
        "SELECT before_summary_json, after_summary_json, changed_fields_json FROM audit_logs"
    ).fetchall()
    persistent_audit_serialized = json.dumps(
        [dict(row) for row in all_persistent_audit_rows], ensure_ascii=False, sort_keys=True
    )
    audit_api_serialized = json.dumps(audit_response.json(), ensure_ascii=False, sort_keys=True)
    assert all(fragment not in persistent_audit_serialized for fragment in forbidden_fragments)
    assert all(fragment not in audit_api_serialized for fragment in forbidden_fragments)
    expected_before_hash = hashlib.sha256(endpoint_before.encode("utf-8")).hexdigest()
    expected_after_hash = hashlib.sha256(endpoint_after.encode("utf-8")).hexdigest()
    created_audit = next(row for row in audit_rows if row["action"] == "provider.created")
    updated_audit = next(row for row in audit_rows if row["action"] == "provider.updated")
    created_summary = json.loads(created_audit["after_summary_json"])
    updated_before = json.loads(updated_audit["before_summary_json"])
    updated_after = json.loads(updated_audit["after_summary_json"])
    assert "base_url" not in created_summary
    assert created_summary["base_url_hash"] == expected_before_hash
    assert updated_before["base_url_hash"] == expected_before_hash
    assert updated_after["base_url_hash"] == expected_after_hash
    assert "base_url_hash" in json.loads(updated_audit["changed_fields_json"])
