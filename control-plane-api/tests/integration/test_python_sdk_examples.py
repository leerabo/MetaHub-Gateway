"""PRD-038 控制面集成测试：验证 Python 示例元数据、受控 ZIP、RBAC、租户隔离与事件降级。"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.developer_portal import service as sdk_service
from metahub_control.developer_portal.routes import router as developer_portal_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试密码只用于临时数据库身份夹具，不写入响应、事件或 ZIP 内容。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
# 内部 Chat 测试身份只参与启动配置校验，任何 SDK 元数据、ZIP 与审计事件都不得包含它。
INTERNAL_CHAT_TOKEN = "prd038-internal-chat-token-2026-secure-test"


def sqlite_url(path: Path) -> str:
    """把 pytest 临时数据库路径转换为项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只挂载认证与开发者门户路由的轻量集成测试应用。"""

    app = create_service_app("control-plane-api", "Python SDK Example Test API")
    app.include_router(auth_router)
    app.include_router(developer_portal_router)
    return app


def prepare_example_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """按生产白名单创建隔离示例树，并把服务根目录切换到该测试目录。

    测试不依赖另一 worker 写入 ``examples/python`` 的时序，同时仍使用生产固定清单验证
    ZIP 成员；内容只含公开占位，不包含种子 Key、Authorization 或本机数据库路径。
    """

    root = tmp_path / "python-example"
    for relative_name in sdk_service.PYTHON_EXAMPLE_FILE_ALLOWLIST:
        target = root.joinpath(*relative_name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative_name == ".env.example":
            content = (
                "METAHUB_BASE_URL=http://localhost:8000/v1\n"
                "METAHUB_VIRTUAL_KEY=your-virtual-key\n"
                "METAHUB_CHAT_MODEL=default-chat\n"
                "METAHUB_EMBEDDING_MODEL=default-embedding\n"
            )
        else:
            content = f"# controlled example file: {relative_name}\n"
        target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(sdk_service, "PYTHON_EXAMPLE_ROOT", root)
    return root


def prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str, str]:
    """准备 admin/developer/viewer、双能力模型和另一租户的隔离数据。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    # 开发环境使用明文 HTTP 时限定字面量回环，防止内部服务 Token 被发送到 DNS 目标。
    monkeypatch.setenv("METAHUB_GATEWAY_API_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("METAHUB_INTERNAL_CHAT_TOKEN", INTERNAL_CHAT_TOKEN)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", "mhv_prd038_seed_only")
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd038-auth-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)

    # Developer 与 viewer 使用同一租户，确保权限差异只来自后端角色校验而不是租户状态。
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd038_developer",
                seed.tenant_id,
                "developer@example.com",
                "SDK Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd038_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "SDK Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    provider_id = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["provider_id"]
    chat_model_id = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["model_id"]
    # Alias 必须引用当前租户的 `dev` Environment，兼容代码本身不能证明归属。
    environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (seed.tenant_id,),
    ).fetchone()["environment_id"]
    connection.execute(
        "UPDATE models SET capabilities=? WHERE model_id=? AND tenant_id=?",
        (json.dumps({"chat": True, "stream": True}), chat_model_id, seed.tenant_id),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, display_name, capabilities, context_window,
          input_price_per_1k, output_price_per_1k, currency, status, created_by,
          created_at, updated_at
        )
        VALUES(
          'model_prd038_embeddings', ?, ?, 'prd038-embedding-internal',
          'prd038-embedding-upstream', 'prd038-embedding-upstream', 'PRD-038 Embeddings',
          ?, 8192, 0, 0, 'CNY', 'active', 'user_prd038_developer',
          datetime('now'), datetime('now')
        )
        """,
        (seed.tenant_id, provider_id, json.dumps({"embeddings": True})),
    )
    connection.executemany(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id, default_model_id, status,
          description, created_by, created_at, updated_at
        )
        VALUES(?, ?, ?, 'dev', ?, ?, 'active', ?, 'user_prd038_developer', ?, ?)
        """,
        [
            (
                "alias_prd038_chat",
                seed.tenant_id,
                "company-chat",
                environment_id,
                chat_model_id,
                "PRD-038 chat alias",
                "2026-07-15T01:00:00Z",
                "2026-07-15T01:00:00Z",
            ),
            (
                "alias_prd038_embeddings",
                seed.tenant_id,
                "company-embeddings",
                environment_id,
                "model_prd038_embeddings",
                "PRD-038 embeddings alias",
                "2026-07-15T01:01:00Z",
                "2026-07-15T01:01:00Z",
            ),
        ],
    )

    # 第二租户只用于证明推荐查询带 tenant_id；其显眼别名不得出现在默认租户响应中。
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES(
          'tenant_prd038_other', 'prd038-other', 'PRD-038 Other', 'internal_only', 'active',
          datetime('now'), datetime('now')
        )
        """
    )
    # 第二租户的默认 Environment 由租户插入触发器创建，仍需按 tenant_id 精确读取。
    other_environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        ("tenant_prd038_other",),
    ).fetchone()["environment_id"]
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, health_status, created_at, updated_at
        )
        VALUES(
          'provider_prd038_other', 'tenant_prd038_other', 'other-provider', 'Other Provider',
          'http://other.invalid/v1', 'active', 'openai_compatible', 'bearer', 'unknown',
          datetime('now'), datetime('now')
        )
        """
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd038_other', 'tenant_prd038_other', 'provider_prd038_other',
          'other-internal', 'other-upstream', '{"chat":true,"embeddings":true}',
          'active', datetime('now'), datetime('now')
        )
        """
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id, default_model_id, status,
          created_at, updated_at
        )
        VALUES(
          'alias_prd038_other', 'tenant_prd038_other', 'other-tenant-secret-alias',
          'dev', ?, 'model_prd038_other', 'active', datetime('now'), datetime('now')
        )
        """,
        (other_environment_id,),
    )
    connection.commit()
    # 返回值只暴露临时数据库地址、可信种子租户和管理员邮箱，供测试登录与事件断言复用。
    return database_url, seed.tenant_id, seed.admin_email


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录指定测试用户并只返回访问令牌，不把令牌写入日志或断言文本。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 访问令牌仅放入后续 Authorization 头，不写入测试日志、ZIP 或失败消息。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_python_sdk_metadata_and_download_use_tenant_context_and_fixed_zip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：developer/admin 可查询，下载 ZIP 固定且事件完整记录认证上下文。"""

    prepare_example_tree(monkeypatch, tmp_path)
    database_url, tenant_id, admin_email = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        admin_token = await login(client, admin_email, ADMIN_PASSWORD)
        metadata = await client.get(
            "/developer/sdk-examples/python",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd038_metadata",
            },
        )
        admin_metadata = await client.get(
            "/developer/sdk-examples/python",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        download = await client.get(
            "/developer/sdk-examples/python/download",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd038_download",
            },
        )

    assert metadata.status_code == 200, metadata.text
    assert admin_metadata.status_code == 200, admin_metadata.text
    payload = metadata.json()
    assert payload["tenant_id"] == tenant_id
    assert payload["gateway_base_url"] == "http://127.0.0.1:8000/v1"
    assert payload["chat_model_alias"] == "company-chat"
    assert payload["embedding_model_alias"] == "company-embeddings"
    assert "other-tenant-secret-alias" not in metadata.text
    assert set(payload["capabilities"]) == {
        "chat",
        "embeddings",
        "timeout",
        "retries",
        "request_id_error_handling",
    }
    variables = {item["name"]: item for item in payload["environment_variables"]}
    assert variables["METAHUB_VIRTUAL_KEY"]["default_value"] is None
    assert variables["METAHUB_VIRTUAL_KEY"]["required"] is True
    commands = {item["id"]: item["command"] for item in payload["commands"]}
    assert "--prompt" in commands["chat"]
    assert "--input" in commands["embeddings"]

    assert download.status_code == 200, download.text
    assert download.headers["content-type"] == "application/zip"
    assert (
        download.headers["content-disposition"]
        == 'attachment; filename="metahub-python-examples-0.1.0.zip"'
    )
    assert download.headers["x-request-id"] == "req_prd038_download"
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["x-content-type-options"] == "nosniff"
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        expected_names = {
            f"{sdk_service.PYTHON_EXAMPLE_ARCHIVE_ROOT}/{relative_name}"
            for relative_name in sdk_service.PYTHON_EXAMPLE_FILE_ALLOWLIST
        }
        assert set(archive.namelist()) == expected_names
        combined_content = b"\n".join(archive.read(name) for name in archive.namelist())
    assert b"mhv_prd038_seed_only" not in combined_content
    assert b"Authorization: Bearer" not in combined_content
    assert str(tmp_path).encode() not in combined_content

    event = connect_database(database_url).execute(
        """
        SELECT tenant_id, user_id, session_id, event_type, object_id,
               path, request_id, trace_id, payload_json
        FROM developer_portal_events
        WHERE request_id='req_prd038_download'
        """
    ).fetchone()
    assert event is not None
    assert event["tenant_id"] == tenant_id
    assert event["user_id"] == "user_prd038_developer"
    assert event["session_id"]
    assert event["event_type"] == "sdk.example_downloaded"
    assert event["object_id"] == "python-openai-example"
    assert event["path"] == "/#sdk-examples"
    assert event["trace_id"]
    assert json.loads(event["payload_json"]) == {
        "artifact_version": "0.1.0",
        "example_id": "python-openai-example",
        "file_count": len(sdk_service.PYTHON_EXAMPLE_FILE_ALLOWLIST),
        "language": "python",
    }


@pytest.mark.anyio
async def test_python_sdk_endpoints_reject_viewer_and_hide_source_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/异常：viewer 在后端被拒绝，缺失白名单文件时错误不泄露绝对路径。"""

    example_root = prepare_example_tree(monkeypatch, tmp_path)
    database_url, _tenant_id, _admin_email = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_metadata = await client.get(
            "/developer/sdk-examples/python",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        viewer_download = await client.get(
            "/developer/sdk-examples/python/download",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        missing_token = await client.get("/developer/sdk-examples/python")

        connection = connect_database(database_url)
        connection.execute(
            """
            UPDATE model_aliases
            SET status='disabled'
            WHERE model_alias_id='alias_prd038_embeddings'
            """
        )
        connection.commit()
        placeholder_metadata = await client.get(
            "/developer/sdk-examples/python",
            headers={"Authorization": f"Bearer {developer_token}"},
        )

        # 删除一个固定成员模拟部署制品不完整；客户端只能看到统一错误码，不能看到本机路径。
        (example_root / "src" / "metahub_gateway_examples" / "chat.py").unlink()
        unavailable = await client.get(
            "/developer/sdk-examples/python",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        archive_failed = await client.get(
            "/developer/sdk-examples/python/download",
            headers={"Authorization": f"Bearer {developer_token}"},
        )

    for response in (viewer_metadata, viewer_download):
        assert response.status_code == 403
        assert response.json()["code"] == "auth.permission_denied"
        assert response.json()["request_id"]
    assert missing_token.status_code == 401
    assert missing_token.json()["code"] == "auth.token_missing"
    assert missing_token.json()["request_id"]
    assert placeholder_metadata.status_code == 200
    assert placeholder_metadata.json()["embedding_model_alias"] == "default-embedding"

    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "developer.sdk_example_unavailable"
    assert archive_failed.status_code == 500
    assert archive_failed.json()["code"] == "developer.sdk_example_archive_failed"
    assert str(example_root) not in unavailable.text
    assert str(example_root) not in archive_failed.text


@pytest.mark.anyio
async def test_python_sdk_event_failure_does_not_block_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """降级流程：数据库拒绝下载事件时仍返回完整 ZIP，且非法事件字段被白名单拒绝。"""

    prepare_example_tree(monkeypatch, tmp_path)
    database_url, _tenant_id, _admin_email = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        CREATE TRIGGER trg_prd038_force_event_failure
        BEFORE INSERT ON developer_portal_events
        FOR EACH ROW
        WHEN NEW.event_type='sdk.example_downloaded'
        BEGIN
          SELECT RAISE(ABORT, 'forced_sdk_event_failure');
        END
        """
    )
    connection.commit()
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        response = await client.get(
            "/developer/sdk-examples/python/download",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd038_event_fail",
            },
        )

    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == len(sdk_service.PYTHON_EXAMPLE_FILE_ALLOWLIST)
    assert connection.execute(
        "SELECT event_id FROM developer_portal_events WHERE request_id='req_prd038_event_fail'"
    ).fetchone() is None

    with pytest.raises(ApiError) as invalid_event:
        sdk_service._safe_sdk_download_event_payload(
            {
                "language": "python",
                "artifact_version": "0.1.0",
                "file_count": 9,
                "example_id": "python-openai-example",
                "provider_key": "must-not-be-recorded",
            }
        )
    assert invalid_event.value.code == "developer.sdk_example_event_invalid"


def test_python_sdk_archive_rejects_allowlist_path_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """路径边界：即使内部白名单被误改为父目录跳转，归档器也必须 fail-closed。"""

    prepare_example_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(sdk_service, "PYTHON_EXAMPLE_FILE_ALLOWLIST", ("../outside.txt",))
    (tmp_path / "outside.txt").write_text("not allowed", encoding="utf-8")

    with pytest.raises(ApiError) as escaped:
        sdk_service._build_python_example_archive()

    assert escaped.value.code == "developer.sdk_example_archive_failed"
