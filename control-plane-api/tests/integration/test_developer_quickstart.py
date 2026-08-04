"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_developer_quickstart.py
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.developer_portal.routes import router as developer_portal_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试常量用于开发者引导夹具，Virtual Key 样例只验证脱敏展示和权限边界。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
SEED_VIRTUAL_KEY = "mhv_dev_seed_virtual_key"
# 测试专用内部服务身份只用于配置校验，不能进入快速开始响应、日志或持久事件。
INTERNAL_CHAT_TOKEN = "prd019-internal-chat-token-2026-secure-test"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成 sqlite URL，供控制面测试应用使用。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证和开发者门户路由的测试应用。"""

    app = create_service_app("control-plane-api", "Developer Quickstart Test API")
    app.include_router(auth_router)
    app.include_router(developer_portal_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, with_alias: bool = True) -> tuple[str, str]:
    """准备 PRD-019 快速开始测试所需的租户、用户、Key 和模型别名数据。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    # PRD-066 后，开发环境的明文内部 Chat 调用只能使用字面量回环 Gateway。
    monkeypatch.setenv("METAHUB_GATEWAY_API_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("METAHUB_INTERNAL_CHAT_TOKEN", INTERNAL_CHAT_TOKEN)
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
                "user_prd019_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd019_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    if with_alias:
        # Alias 的兼容环境代码与主数据 ID 必须来自同一租户，防止夹具绕过真实隔离约束。
        environment_id = connection.execute(
            "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
            (seed.tenant_id,),
        ).fetchone()["environment_id"]
        model_id = connection.execute(
            "SELECT model_id FROM models WHERE tenant_id=? ORDER BY created_at ASC LIMIT 1",
            (seed.tenant_id,),
        ).fetchone()["model_id"]
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id, tenant_id, alias, environment, environment_id, default_model_id,
              status, description, created_by, created_at, updated_at
            )
            VALUES('alias_prd019_default', ?, 'company-default', 'dev', ?, ?, 'active',
                   'PRD-019 test alias', 'user_prd019_developer', datetime('now'), datetime('now'))
            """,
            (seed.tenant_id, environment_id, model_id),
        )
    connection.commit()
    # 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
    return database_url, seed.tenant_id


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
async def test_quickstart_data_generates_safe_runnable_examples(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程/安全：快速开始返回可复制示例、推荐模型别名，并且不泄露 Virtual Key 明文。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        response = await client.get(
            "/developer/quickstart-data",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd019_quickstart"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["tenant_id"] == tenant_id
    assert payload["gateway_base_url"] == "http://127.0.0.1:8000/v1"
    assert payload["model_alias"] == "company-default"
    assert payload["has_active_virtual_key"] is True
    assert payload["virtual_key_prefix"].startswith("mhv_dev")
    assert SEED_VIRTUAL_KEY not in response.text
    assert "key_hash" not in response.text
    assert "provider_key" not in response.text.lower()

    examples = {example["language"]: example for example in payload["examples"]}
    assert {"python", "javascript", "curl"} == set(examples)
    assert 'os.environ["METAHUB_VIRTUAL_KEY"]' in examples["python"]["code"]
    assert "process.env.METAHUB_VIRTUAL_KEY" in examples["javascript"]["code"]
    assert "Authorization: Bearer ${METAHUB_VIRTUAL_KEY}" in examples["curl"]["code"]
    ast.parse(examples["python"]["code"])

    row = connect_database(database_url).execute(
        """
        SELECT tenant_id, user_id, event_type, object_id, request_id, payload_json
        FROM developer_portal_events
        WHERE request_id=?
        """,
        ("req_prd019_quickstart",),
    ).fetchone()
    assert row is not None
    assert row["tenant_id"] == tenant_id
    assert row["user_id"] == "user_prd019_developer"
    assert row["event_type"] == "developer.quickstart_viewed"
    assert row["object_id"] == "quickstart"
    assert "company-default" in row["payload_json"]


@pytest.mark.anyio
async def test_quickstart_rejects_viewer_and_guides_missing_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：viewer 被后端拒绝，缺 Key/别名时返回下一步引导而不是泄露内部错误。"""

    database_url, _tenant_id = prepare_database(monkeypatch, tmp_path, with_alias=False)
    connection = connect_database(database_url)
    connection.execute("DELETE FROM virtual_keys")
    connection.commit()
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_response = await client.get(
            "/developer/quickstart-data",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        developer_response = await client.get(
            "/developer/quickstart-data",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd019_missing"},
        )

    assert viewer_response.status_code == 403
    assert viewer_response.json()["code"] == "auth.permission_denied"

    assert developer_response.status_code == 200, developer_response.text
    payload = developer_response.json()
    assert payload["has_active_virtual_key"] is False
    assert payload["virtual_key_prefix"] is None
    assert payload["model_alias"] == "default-chat"
    step_status = {step["id"]: step["status"] for step in payload["steps"]}
    assert step_status["prepare-key"] == "action_required"
    assert step_status["choose-model"] == "action_required"
    assert SEED_VIRTUAL_KEY not in developer_response.text
