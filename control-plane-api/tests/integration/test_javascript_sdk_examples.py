"""PRD-039 控制面集成测试：验证 JavaScript 元数据、受控 ZIP、安全边界和事件降级。"""

from __future__ import annotations

import io
import json
import logging
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

# 密码和种子 Key 只存在于临时测试环境，用于证明响应、ZIP、事件和日志不会泄露敏感值。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "JavaScriptDeveloper!2026"
VIEWER_PASSWORD = "JavaScriptViewer!2026"
# 内部 Chat 服务身份仅存在于隔离测试进程；固定高强度值用于通过启动校验，禁止写入响应或事件。
INTERNAL_CHAT_TOKEN = "prd039-internal-chat-token-2026-secure-test"


def sqlite_url(path: Path) -> str:
    """把 pytest 临时数据库路径转换为项目统一支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只挂载认证与开发者门户路由的轻量集成应用。"""

    app = create_service_app("control-plane-api", "JavaScript SDK Example Test API")
    app.include_router(auth_router)
    app.include_router(developer_portal_router)
    return app


def prepare_javascript_example_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """按生产九成员白名单创建隔离 Node 制品树，并保留根常量 monkeypatch 注入点。

    夹具不依赖另一 worker 创建 ``examples/js`` 的时序；内容仅含公开占位，确保后端测试
    可以独立验证路径、大小、ZIP 成员和敏感值排除规则。
    """

    root = tmp_path / "javascript-example"
    for relative_name in sdk_service.JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST:
        target = root.joinpath(*relative_name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative_name == ".env.example":
            content = (
                "METAHUB_BASE_URL=http://localhost:8000/v1\n"
                "METAHUB_VIRTUAL_KEY=your-virtual-key\n"
                "METAHUB_CHAT_MODEL=default-chat\n"
            )
        elif relative_name == "package.json":
            content = json.dumps(
                {
                    "name": sdk_service.JAVASCRIPT_EXAMPLE_PACKAGE,
                    "version": sdk_service.JAVASCRIPT_EXAMPLE_VERSION,
                    "type": "module",
                }
            )
        else:
            content = f"// controlled JavaScript example file: {relative_name}\n"
        target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(sdk_service, "JAVASCRIPT_EXAMPLE_ROOT", root)
    return root


def prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str, str]:
    """准备 admin/developer/viewer、当前租户 Chat 别名和第二租户隔离数据。"""

    database_url = sqlite_url(tmp_path / "metahub-prd039.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    # 测试使用明文 HTTP 时必须指向字面量回环地址，防止内部 Chat Token 经 DNS 目标泄露。
    monkeypatch.setenv("METAHUB_GATEWAY_API_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("METAHUB_INTERNAL_CHAT_TOKEN", INTERNAL_CHAT_TOKEN)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", "mhv_prd039_seed_only")
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd039-auth-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)

    # 两个普通用户属于同一租户，确保 403 只来自后端角色校验，而非租户或账号状态差异。
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
                "user_prd039_developer",
                seed.tenant_id,
                "javascript-developer@example.com",
                "JavaScript Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd039_viewer",
                seed.tenant_id,
                "javascript-viewer@example.com",
                "JavaScript Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    chat_model_id = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()["model_id"]
    # 当前租户 Alias 必须同时携带 `dev` 代码和同租户 Environment 主数据 ID。
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
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id, default_model_id, status,
          description, created_by, created_at, updated_at
        )
        VALUES(
          'alias_prd039_chat', ?, 'company-javascript-chat', 'dev', ?, ?, 'active',
          'PRD-039 current tenant chat alias', 'user_prd039_developer',
          '2026-07-15T02:00:00Z', '2026-07-15T02:00:00Z'
        )
        """,
        (seed.tenant_id, environment_id, chat_model_id),
    )

    # 构造创建时间更早但 Provider 已不健康的候选，证明示例不会把已知故障路由推荐给用户。
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, health_status, created_at, updated_at
        )
        VALUES(
          'provider_prd039_unhealthy', ?, 'prd039-unhealthy-provider',
          'PRD-039 Unhealthy Provider', 'http://unhealthy.invalid/v1', 'active',
          'openai_compatible', 'bearer', 'unhealthy', datetime('now'), datetime('now')
        )
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd039_unhealthy', ?, 'provider_prd039_unhealthy',
          'unhealthy-javascript-chat', 'unhealthy-upstream',
          '{"chat":true,"stream":true}', 'active', datetime('now'), datetime('now')
        )
        """,
        (seed.tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id, default_model_id, status,
          created_at, updated_at
        )
        VALUES(
          'alias_prd039_unhealthy', ?, 'unhealthy-javascript-chat', 'dev',
          ?, 'model_prd039_unhealthy', 'active',
          '2026-07-15T01:00:00Z', '2026-07-15T01:00:00Z'
        )
        """,
        (seed.tenant_id, environment_id),
    )

    # 第二租户使用显眼别名；若查询遗漏 tenant_id，该值会直接让元数据断言失败。
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES(
          'tenant_prd039_other', 'prd039-other', 'PRD-039 Other', 'internal_only', 'active',
          datetime('now'), datetime('now')
        )
        """
    )
    # 新租户触发器已创建默认 `dev` Environment；按租户读取可验证并保留隔离边界。
    other_environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        ("tenant_prd039_other",),
    ).fetchone()["environment_id"]
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, health_status, created_at, updated_at
        )
        VALUES(
          'provider_prd039_other', 'tenant_prd039_other', 'prd039-other-provider',
          'PRD-039 Other Provider', 'http://other.invalid/v1', 'active',
          'openai_compatible', 'bearer', 'unknown', datetime('now'), datetime('now')
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
          'model_prd039_other', 'tenant_prd039_other', 'provider_prd039_other',
          'other-javascript-internal', 'other-javascript-upstream',
          '{"chat":true,"stream":true}', 'active', datetime('now'), datetime('now')
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
          'alias_prd039_other', 'tenant_prd039_other', 'other-tenant-javascript-secret',
          'dev', ?, 'model_prd039_other', 'active', datetime('now'), datetime('now')
        )
        """,
        (other_environment_id,),
    )
    connection.commit()
    # 只把临时数据库地址、可信种子租户和管理员邮箱交给调用测试，不返回任何 Key 或哈希。
    return database_url, seed.tenant_id, seed.admin_email


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录临时用户并只返回后续 Authorization 头需要的访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 令牌只进入当前测试进程的 Authorization 请求头，不写入断言文本、事件或 ZIP。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_javascript_metadata_download_rbac_tenant_and_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程同时验证双角色成功、viewer/匿名拒绝、固定 ZIP 和租户事件。"""

    prepare_javascript_example_tree(monkeypatch, tmp_path)
    database_url, tenant_id, admin_email = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer_token = await login(
            client,
            "javascript-developer@example.com",
            DEVELOPER_PASSWORD,
        )
        viewer_token = await login(client, "javascript-viewer@example.com", VIEWER_PASSWORD)
        admin_token = await login(client, admin_email, ADMIN_PASSWORD)
        metadata = await client.get(
            "/developer/sdk-examples/javascript",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd039_metadata",
            },
        )
        admin_metadata = await client.get(
            "/developer/sdk-examples/javascript",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        viewer_metadata = await client.get(
            "/developer/sdk-examples/javascript",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        viewer_download = await client.get(
            "/developer/sdk-examples/javascript/download",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        anonymous_metadata = await client.get("/developer/sdk-examples/javascript")
        download = await client.get(
            "/developer/sdk-examples/javascript/download",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd039_download",
            },
        )

    assert metadata.status_code == 200, metadata.text
    assert admin_metadata.status_code == 200, admin_metadata.text
    payload = metadata.json()
    assert payload["id"] == "javascript-openai-example"
    assert payload["tenant_id"] == tenant_id
    assert payload["language"] == "javascript"
    assert payload["package_name"] == "@metahub/gateway-node-examples"
    assert payload["runtime"] == "Node.js >= 22"
    assert payload["gateway_base_url"] == "http://127.0.0.1:8000/v1"
    assert payload["chat_model_alias"] == "company-javascript-chat"
    assert payload["browser_direct_supported"] is False
    assert "Node.js" in payload["security_notice"]
    assert "Virtual Key" in payload["security_notice"]
    assert "other-tenant-javascript-secret" not in metadata.text
    assert payload["files"] == list(sdk_service.JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST)
    assert payload["capabilities"] == ["chat", "stream", "timeout", "retries", "request_id"]
    variables = {item["name"]: item for item in payload["environment_variables"]}
    assert variables["METAHUB_VIRTUAL_KEY"]["required"] is True
    assert variables["METAHUB_VIRTUAL_KEY"]["default_value"] is None
    commands = {item["id"]: item["command"] for item in payload["commands"]}
    assert set(commands) == {"install", "configure", "chat", "stream"}
    assert commands["install"] == "npm ci"
    assert commands["configure"] == "Copy-Item .env.example .env"
    assert "src/chat.js" in commands["chat"]
    assert "src/stream.js" in commands["stream"]

    for response in (viewer_metadata, viewer_download):
        assert response.status_code == 403
        assert response.json()["code"] == "auth.permission_denied"
        assert response.json()["request_id"]
    assert anonymous_metadata.status_code == 401
    assert anonymous_metadata.json()["code"] == "auth.token_missing"

    assert download.status_code == 200, download.text
    assert download.headers["content-type"] == "application/zip"
    assert download.headers["content-disposition"] == (
        'attachment; filename="metahub-javascript-examples-0.1.0.zip"'
    )
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["x-request-id"] == "req_prd039_download"
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        expected_names = {
            f"{sdk_service.JAVASCRIPT_EXAMPLE_ARCHIVE_ROOT}/{relative_name}"
            for relative_name in sdk_service.JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST
        }
        assert set(archive.namelist()) == expected_names
        combined_content = b"\n".join(archive.read(name) for name in archive.namelist())
    assert b"mhv_prd039_seed_only" not in combined_content
    assert b"Authorization: Bearer" not in combined_content
    assert str(tmp_path).encode() not in combined_content

    event = connect_database(database_url).execute(
        """
        SELECT tenant_id, user_id, session_id, event_type, object_id,
               path, request_id, trace_id, payload_json
        FROM developer_portal_events
        WHERE request_id='req_prd039_download'
        """
    ).fetchone()
    assert event is not None
    assert event["tenant_id"] == tenant_id
    assert event["user_id"] == "user_prd039_developer"
    assert event["session_id"]
    assert event["event_type"] == "sdk.example_downloaded"
    assert event["object_id"] == "javascript-openai-example"
    assert event["path"] == "/#sdk-examples"
    assert event["trace_id"]
    assert json.loads(event["payload_json"]) == {
        "artifact_version": "0.1.0",
        "example_id": "javascript-openai-example",
        "file_count": 9,
        "language": "javascript",
    }


def test_javascript_archive_rejects_escape_and_pre_read_oversize(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """归档器在读取前拒绝父目录跳转和超过单文件声明上限的成员。"""

    root = prepare_javascript_example_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(sdk_service, "JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST", ("../outside.js",))
    (tmp_path / "outside.js").write_text("not allowed", encoding="utf-8")
    with pytest.raises(ApiError) as escaped:
        sdk_service._build_javascript_example_archive()
    assert escaped.value.code == "developer.sdk_example_archive_failed"

    # 第二个分支把白名单切换为九字节文件和八字节上限，证明 stat 预检直接 fail-closed。
    (root / "oversized.js").write_bytes(b"123456789")
    monkeypatch.setattr(sdk_service, "JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST", ("oversized.js",))
    monkeypatch.setattr(sdk_service, "JAVASCRIPT_EXAMPLE_MAX_FILE_BYTES", 8)
    monkeypatch.setattr(sdk_service, "JAVASCRIPT_EXAMPLE_MAX_TOTAL_BYTES", 16)
    with pytest.raises(ApiError) as oversized:
        sdk_service._build_javascript_example_archive()
    assert oversized.value.code == "developer.sdk_example_archive_failed"


def test_javascript_archive_rejects_root_parent_and_leaf_symlinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """根、白名单父目录和叶子文件只要是符号链接就必须拒绝，即使目标仍位于根内。"""

    root = prepare_javascript_example_tree(monkeypatch, tmp_path)
    leaf_link = root / "src" / "linked-chat.js"
    try:
        leaf_link.symlink_to(root / "src" / "chat.js")
    # 测试环境缺少 Windows 符号链接权限时明确跳过，不把权限问题误报为归档器失败。
    except OSError as exc:
        # Windows 未启用开发者模式时无法创建测试链接；其余安全用例仍由独立测试覆盖。
        pytest.skip(f"当前环境不能创建符号链接: {exc}")
    monkeypatch.setattr(
        sdk_service,
        "JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST",
        ("src/linked-chat.js",),
    )
    with pytest.raises(ApiError) as leaf_rejected:
        sdk_service._build_javascript_example_archive()
    assert leaf_rejected.value.code == "developer.sdk_example_archive_failed"

    linked_parent = root / "linked-src"
    linked_parent.symlink_to(root / "src", target_is_directory=True)
    monkeypatch.setattr(
        sdk_service,
        "JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST",
        ("linked-src/chat.js",),
    )
    with pytest.raises(ApiError) as parent_rejected:
        sdk_service._build_javascript_example_archive()
    assert parent_rejected.value.code == "developer.sdk_example_archive_failed"

    linked_root = tmp_path / "linked-javascript-root"
    linked_root.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(sdk_service, "JAVASCRIPT_EXAMPLE_ROOT", linked_root)
    monkeypatch.setattr(sdk_service, "JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST", ("src/chat.js",))
    with pytest.raises(ApiError) as root_rejected:
        sdk_service._build_javascript_example_archive()
    assert root_rejected.value.code == "developer.sdk_example_archive_failed"


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {
            "language": "python",
            "artifact_version": "0.1.0",
            "file_count": 9,
            "example_id": "javascript-openai-example",
        },
        {
            "language": "javascript",
            "artifact_version": "0.1",
            "file_count": 9,
            "example_id": "javascript-openai-example",
        },
        {
            "language": "javascript",
            "artifact_version": "0.1.0",
            "file_count": True,
            "example_id": "javascript-openai-example",
        },
        {
            "language": "javascript",
            "artifact_version": "0.1.0",
            "file_count": 8,
            "example_id": "javascript-openai-example",
        },
        {
            "language": "javascript",
            "artifact_version": "0.1.0",
            "file_count": 9,
            "example_id": "python-openai-example",
        },
        {
            "language": "javascript",
            "artifact_version": "0.1.0",
            "file_count": 9,
            "example_id": "javascript-openai-example",
            "prompt": "must-not-be-recorded",
        },
    ],
)
def test_javascript_download_event_rejects_invalid_values(
    invalid_payload: dict[str, object],
) -> None:
    """事件不仅校验字段名，还严格拒绝语言、版本、bool/数量、ID 和敏感字段偏差。"""

    with pytest.raises(ApiError) as invalid_event:
        sdk_service._safe_sdk_download_event_payload(
            invalid_payload,
            artifact_spec=sdk_service._javascript_sdk_artifact_spec(),
        )
    assert invalid_event.value.code == "developer.sdk_example_event_invalid"


@pytest.mark.anyio
async def test_javascript_arbitrary_event_runtime_error_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """事件调用点出现额外 RuntimeError 时仍返回完整 ZIP，日志只保留固定安全字段。"""

    prepare_javascript_example_tree(monkeypatch, tmp_path)
    _database_url, _tenant_id, _admin_email = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer_token = await login(
            client,
            "javascript-developer@example.com",
            DEVELOPER_PASSWORD,
        )

        def raise_unexpected_event_error(*args, **kwargs) -> None:
            """模拟事件记录器新增的非数据库异常，消息故意包含不应进入日志的敏感文本。"""

            # 故意抛出含敏感诱饵的异常，验证 fail-open 日志绝不会序列化异常消息。
            raise RuntimeError("Authorization Bearer mhv_runtime_secret prompt=classified")

        monkeypatch.setattr(
            sdk_service,
            "_record_sdk_example_downloaded",
            raise_unexpected_event_error,
        )
        caplog.set_level(logging.WARNING, logger="metahub.developer_portal")
        response = await client.get(
            "/developer/sdk-examples/javascript/download",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd039_runtime_fail_open",
            },
        )

    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == len(sdk_service.JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST)
    assert "sdk.example_download_event_write_failed" in caplog.text
    assert "mhv_runtime_secret" not in caplog.text
    assert "classified" not in caplog.text
