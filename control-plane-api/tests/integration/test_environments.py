"""模块职责：验证 PRD-030 Environment 隔离、资源绑定和后端权限边界。

维护边界：测试必须覆盖 tenant_id 隔离、request_id、跨环境误用阻断；任何 Key 明文都不得写入断言失败日志或数据库。
来源文件：control-plane-api/tests/integration/test_environments.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.debug_console.routes import router as debug_console_router
from metahub_control.environments.routes import router as environments_router
from metahub_control.model_aliases.routes import router as model_aliases_router
from metahub_control.virtual_keys.routes import router as virtual_keys_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下密码只用于环境权限矩阵夹具，不会作为业务数据、日志字段或响应内容持久化。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """把独立临时数据库路径转换成测试运行时可识别的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造 PRD-030 所需的最小控制面，避免无关路由影响隔离断言。"""

    app = create_service_app("control-plane-api", "Environment Isolation Test API")
    app.include_router(auth_router)
    app.include_router(environments_router)
    app.include_router(model_aliases_router)
    app.include_router(virtual_keys_router)
    app.include_router(debug_console_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str, str, str]:
    """准备角色和两个可区分模型，Environment 由正式 API 创建。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd030-auth-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (seed.tenant_id,),
    ).fetchone()
    dev_model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (seed.tenant_id,),
    ).fetchone()
    assert provider is not None
    assert dev_model is not None
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name, provider_model_id,
          display_name, capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd030_test', ?, ?, 'test-chat', 'test-chat-upstream', 'test-chat-upstream',
          'PRD030 Test Model', '{"chat": true}', 'active', datetime('now'), datetime('now')
        )
        """,
        (seed.tenant_id, provider["provider_id"]),
    )
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd030_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd030_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    # 返回租户与两个模型标识，后续断言不得依赖或暴露任何 Provider Key。
    return database_url, seed.tenant_id, dev_model["model_id"], "model_prd030_test"


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录指定角色并返回短期测试 Token，失败时保留服务端响应便于定位。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 测试 Token 仅用于当前客户端请求头，不写入数据库或测试诊断文本。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_environment_alias_resolution_and_resource_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：同名别名按环境绑定不同模型，Key 和事件均记录可信环境 ID。"""

    database_url, tenant_id, dev_model_id, test_model_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        created_test = await client.post(
            "/environments",
            headers={**admin_headers, "X-Request-ID": "req_prd030_environment_create"},
            json={"code": "test", "name": "测试环境", "description": "PRD-030"},
        )
        environments = await client.get(
            "/environments",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        assert created_test.status_code == 201, created_test.text
        test_environment_id = created_test.json()["environment"]["environment_id"]
        dev_environment_id = next(
            item["environment_id"] for item in environments.json()["environments"] if item["code"] == "dev"
        )
        dev_alias = await client.post(
            "/model-aliases",
            headers=admin_headers,
            json={
                "alias": "company-default",
                "environment_id": dev_environment_id,
                "default_model_id": dev_model_id,
            },
        )
        test_alias = await client.post(
            "/model-aliases",
            headers=admin_headers,
            json={
                "alias": "company-default",
                "environment_id": test_environment_id,
                "default_model_id": test_model_id,
            },
        )
        test_key = await client.post(
            "/virtual-keys",
            headers=admin_headers,
            json={"name": "PRD030 Test Key", "environment_id": test_environment_id},
        )

    assert environments.status_code == 200, environments.text
    assert dev_alias.status_code == 201, dev_alias.text
    assert test_alias.status_code == 201, test_alias.text
    assert dev_alias.json()["model_alias"]["default_model_id"] == dev_model_id
    assert test_alias.json()["model_alias"]["default_model_id"] == test_model_id
    assert test_key.status_code == 201, test_key.text
    assert test_key.json()["virtual_key"]["environment_id"] == test_environment_id
    connection = connect_database(database_url)
    event = connection.execute(
        "SELECT tenant_id, event_type, request_id FROM environment_events WHERE environment_id=?",
        (test_environment_id,),
    ).fetchone()
    assert event is not None
    assert event["tenant_id"] == tenant_id
    assert event["event_type"] == "environment.created"
    assert event["request_id"] == "req_prd030_environment_create"


@pytest.mark.anyio
async def test_environment_permissions_and_prod_key_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：developer 不可写、viewer 不可读，生产 Key 不能用于测试环境调试。"""

    prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        test_environment = await client.post(
            "/environments",
            headers=admin_headers,
            json={"code": "test", "name": "测试环境"},
        )
        prod_environment = await client.post(
            "/environments",
            headers=admin_headers,
            json={"code": "prod", "name": "生产环境"},
        )
        prod_key = await client.post(
            "/virtual-keys",
            headers=admin_headers,
            json={
                "name": "PRD030 Production Key",
                "environment_id": prod_environment.json()["environment"]["environment_id"],
            },
        )
        developer_write = await client.post(
            "/environments",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={"code": "prod", "name": "越权环境"},
        )
        viewer_read = await client.get(
            "/environments",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        mismatch = await client.post(
            "/debug/chat-completions",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd030_mismatch"},
            json={
                "environment_id": test_environment.json()["environment"]["environment_id"],
                "virtual_key_id": prod_key.json()["virtual_key"]["virtual_key_id"],
                "model_alias": "company-default",
                "messages": [{"role": "user", "content": "must not reach upstream"}],
            },
        )

    assert developer_write.status_code == 403
    assert developer_write.json()["code"] == "auth.permission_denied"
    assert viewer_read.status_code == 403
    assert viewer_read.json()["code"] == "auth.permission_denied"
    # 环境绑定冲突属于资源当前状态冲突，统一映射为 HTTP 409。
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "debug.environment_mismatch"
    assert mismatch.json()["request_id"] == "req_prd030_mismatch"
