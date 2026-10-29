"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_models.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.models.routes import router as models_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app


# 以下测试密码只用于模型目录权限矩阵，验证后端拒绝非管理员写入。
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

    app = create_service_app("control-plane-api", "Model Catalog Test Control API")
    app.include_router(auth_router)
    app.include_router(models_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str, str]:
    """prepare_database：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (seed.tenant_id,),
    ).fetchone()
    assert provider is not None
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd012_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd012_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    # 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
    return database_url, seed.tenant_id, provider["provider_id"]


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
async def test_model_catalog_create_list_update_and_disable_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：管理员维护模型目录，开发者可读取内部模型 ID、能力和价格。"""

    database_url, tenant_id, provider_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd012_admin"}

        created = await client.post(
            "/models",
            headers=admin_headers,
            json={
                "internal_model_id": "deepseek-chat",
                "provider_id": provider_id,
                "provider_model_id": "deepseek-chat-upstream",
                "display_name": "DeepSeek Chat",
                "capabilities": {"chat": True, "stream": True, "embeddings": False},
                "context_window": 64000,
                "input_price_per_1k": 0.001,
                "output_price_per_1k": 0.002,
                "currency": "CNY",
                "price_schema_version": "manual-202607",
                "description": "prd012 model",
            },
        )
        listed_by_developer = await client.get(
            "/models?capability=chat",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd012_dev_list"},
        )
        patched = await client.patch(
            f"/models/{created.json()['model']['model_id']}",
            headers=admin_headers,
            json={
                "provider_model_id": "deepseek-chat-v2",
                "context_window": 128000,
                "output_price_per_1k": 0.003,
                "status": "disabled",
            },
        )

    assert created.status_code == 201, created.text
    model = created.json()["model"]
    assert model["tenant_id"] == tenant_id
    assert model["internal_model_id"] == "deepseek-chat"
    assert model["provider_model_id"] == "deepseek-chat-upstream"
    assert model["capabilities"]["chat"] is True
    assert model["context_window"] == 64000
    assert model["input_price_per_1k"] == 0.001
    assert model["currency"] == "CNY"
    assert created.json()["request_id"] == "req_prd012_admin"

    assert listed_by_developer.status_code == 200
    listed_text = listed_by_developer.text
    assert "deepseek-chat" in listed_text
    assert "key_hash" not in listed_text
    assert "plain_value" not in listed_text
    assert "sk-" not in listed_text

    assert patched.status_code == 200
    patched_model = patched.json()["model"]
    assert patched_model["provider_model_id"] == "deepseek-chat-v2"
    assert patched_model["context_window"] == 128000
    assert patched_model["output_price_per_1k"] == 0.003
    assert patched_model["status"] == "disabled"
    assert patched_model["disabled_at"] is not None

    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT upstream_model_name, provider_model_id, disabled_at FROM models WHERE model_id=?",
        (patched_model["model_id"],),
    ).fetchone()
    assert row["upstream_model_name"] == "deepseek-chat-v2"
    assert row["provider_model_id"] == "deepseek-chat-v2"
    assert row["disabled_at"] is not None


@pytest.mark.anyio
async def test_model_catalog_price_contract_rejects_overflow_and_overprecision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """价格边界：目录创建/更新必须复用十亿上限和 12 位小数合同。

    主流程先写入两个合法边界值；异常流程分别验证超上限、极端有限数和第 13 位
    小数都返回统一错误，且失败 PATCH 不能覆盖已经持久化的可信价格。
    """

    database_url, tenant_id, provider_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(
        transport=ASGITransport(app=control_app),
        base_url="http://control",
    ) as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin_token}"}
        created = await client.post(
            "/models",
            headers={**headers, "X-Request-ID": "req_model_price_boundary"},
            json={
                "internal_model_id": "price-boundary-model",
                "provider_id": provider_id,
                "provider_model_id": "price-boundary-upstream",
                "capabilities": {"chat": True},
                "input_price_per_1k": 1_000_000_000,
                "output_price_per_1k": 0.123456789012,
                "currency": "CNY",
                "price_schema_version": "catalog-boundary-v1",
            },
        )
        over_limit_create = await client.post(
            "/models",
            headers={**headers, "X-Request-ID": "req_model_price_over_limit_create"},
            json={
                "internal_model_id": "price-over-limit-create",
                "provider_id": provider_id,
                "provider_model_id": "price-over-limit-create",
                "capabilities": {"chat": True},
                "input_price_per_1k": 1_000_000_000.000001,
            },
        )
        overprecision_create = await client.post(
            "/models",
            headers={**headers, "X-Request-ID": "req_model_price_overprecision_create"},
            json={
                "internal_model_id": "price-overprecision-create",
                "provider_id": provider_id,
                "provider_model_id": "price-overprecision-create",
                "capabilities": {"chat": True},
                "input_price_per_1k": 0.0000000000001,
            },
        )
        model_id = created.json()["model"]["model_id"]
        extreme_patch = await client.patch(
            f"/models/{model_id}",
            headers={**headers, "X-Request-ID": "req_model_price_extreme_patch"},
            json={"output_price_per_1k": 1e308},
        )
        overprecision_patch = await client.patch(
            f"/models/{model_id}",
            headers={**headers, "X-Request-ID": "req_model_price_overprecision_patch"},
            json={"output_price_per_1k": 0.0000000000001},
        )

    assert created.status_code == 201, created.text
    assert created.json()["model"]["tenant_id"] == tenant_id
    for response, request_id in (
        (over_limit_create, "req_model_price_over_limit_create"),
        (overprecision_create, "req_model_price_overprecision_create"),
        (extreme_patch, "req_model_price_extreme_patch"),
        (overprecision_patch, "req_model_price_overprecision_patch"),
    ):
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "model.price_invalid"
        assert response.json()["request_id"] == request_id

    # 两次非法 PATCH 都必须在 SQL 前失败，目录中的合法边界价格不能发生部分覆盖。
    persisted = connect_database(database_url).execute(
        """
        SELECT CAST(input_price_per_1k AS TEXT) AS input_price,
               CAST(output_price_per_1k AS TEXT) AS output_price
        FROM models WHERE tenant_id=? AND model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()
    assert persisted is not None
    assert float(persisted["input_price"]) == 1_000_000_000
    assert float(persisted["output_price"]) == pytest.approx(0.123456789012)


@pytest.mark.anyio
async def test_model_catalog_permissions_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：非授权角色被拒绝，非法字段返回 model.* 错误码。"""

    _database_url, _tenant_id, provider_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        developer_post_denied = await client.post(
            "/models",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={
                "internal_model_id": "developer-try",
                "provider_id": provider_id,
                "provider_model_id": "developer-try",
                "capabilities": {"chat": True},
            },
        )
        viewer_get_denied = await client.get("/models", headers={"Authorization": f"Bearer {viewer_token}"})
        created = await client.post(
            "/models",
            headers=admin_headers,
            json={
                "internal_model_id": "duplicate-model",
                "provider_id": provider_id,
                "provider_model_id": "duplicate-upstream",
                "capabilities": {"chat": True},
            },
        )
        duplicate = await client.post(
            "/models",
            headers=admin_headers,
            json={
                "internal_model_id": "duplicate-model",
                "provider_id": provider_id,
                "provider_model_id": "another-upstream",
                "capabilities": {"chat": True},
            },
        )
        invalid_provider = await client.post(
            "/models",
            headers=admin_headers,
            json={
                "internal_model_id": "bad-provider",
                "provider_id": "missing-provider",
                "provider_model_id": "upstream",
                "capabilities": {"chat": True},
            },
        )
        invalid_capability = await client.post(
            "/models",
            headers=admin_headers,
            json={
                "internal_model_id": "bad-capability",
                "provider_id": provider_id,
                "provider_model_id": "upstream",
                "capabilities": {"chat": "yes"},
            },
        )
        invalid_price = await client.post(
            "/models",
            headers=admin_headers,
            json={
                "internal_model_id": "bad-price",
                "provider_id": provider_id,
                "provider_model_id": "upstream",
                "capabilities": {"chat": True},
                "input_price_per_1k": -0.01,
            },
        )
        invalid_context = await client.post(
            "/models",
            headers=admin_headers,
            json={
                "internal_model_id": "bad-context",
                "provider_id": provider_id,
                "provider_model_id": "upstream",
                "capabilities": {"chat": True},
                "context_window": -1,
            },
        )
        empty_patch = await client.patch(
            f"/models/{created.json()['model']['model_id']}",
            headers=admin_headers,
            json={},
        )
        not_found = await client.patch(
            "/models/not-found",
            headers=admin_headers,
            json={"status": "disabled"},
        )

    assert developer_post_denied.status_code == 403
    assert developer_post_denied.json()["code"] == "auth.permission_denied"
    assert developer_post_denied.json()["request_id"]
    assert viewer_get_denied.status_code == 403
    assert viewer_get_denied.json()["code"] == "auth.permission_denied"
    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "model.internal_id_exists"
    assert invalid_provider.status_code == 422
    assert invalid_provider.json()["code"] == "model.provider_invalid"
    assert invalid_capability.status_code == 422
    assert invalid_capability.json()["code"] == "model.capabilities_invalid"
    assert invalid_price.status_code == 422
    assert invalid_price.json()["code"] == "model.price_invalid"
    assert invalid_context.status_code == 422
    assert invalid_context.json()["code"] == "model.context_window_invalid"
    assert empty_patch.status_code == 422
    assert empty_patch.json()["code"] == "model.update_empty"
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "model.not_found"
