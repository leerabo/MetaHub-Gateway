"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_provider_keys.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.providers.routes import provider_keys_router, router as providers_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app


# 以下测试密码和 Provider Key 样例只用于密钥管理接口，断言响应和日志不回显明文。
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

    app = create_service_app("control-plane-api", "Provider KeyPool Test Control API")
    app.include_router(auth_router)
    app.include_router(providers_router)
    app.include_router(provider_keys_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """prepare_database：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", "test-provider-key-encryption-secret")
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
                "user_prd014_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd014_viewer",
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


def seed_provider_id(database_url: str) -> str:
    """seed_provider_id：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    connection = connect_database(database_url)
    row = connection.execute("SELECT provider_id FROM providers WHERE provider_code='local_openai'").fetchone()
    assert row is not None
    return row["provider_id"]


@pytest.mark.anyio
async def test_provider_keypool_create_list_and_disable_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """test_provider_keypool_create_list_and_disable_flow：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url = prepare_database(monkeypatch, tmp_path)
    provider_id = seed_provider_id(database_url)
    secret = "sk-prd014-control-secret"
    control_app = build_control_app()

    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd014_key"}
        created = await client.post(
            f"/providers/{provider_id}/keys",
            headers=headers,
            json={"name": "Primary upstream key", "plain_value": secret, "weight": 200},
        )
        listed = await client.get(f"/providers/{provider_id}/keys", headers=headers)
        provider_key_id = created.json()["provider_key"]["provider_key_id"]
        disabled = await client.patch(
            f"/provider-keys/{provider_key_id}",
            headers=headers,
            json={"status": "disabled"},
        )
        connection = connect_database(database_url)
        row = connection.execute(
            "SELECT encrypted_key, key_fingerprint, key_prefix, status FROM provider_credentials WHERE provider_credential_id=?",
            (provider_key_id,),
        ).fetchone()
        connection.close()
        deleted = await client.delete(f"/provider-keys/{provider_key_id}", headers=headers)

    assert created.status_code == 201, created.text
    assert created.json()["provider_key"]["key_prefix"] == secret[:12]
    assert created.json()["provider_key"]["weight"] == 200
    assert secret not in created.text
    assert "encrypted_key" not in created.text
    assert listed.status_code == 200
    assert any(row["provider_key_id"] == provider_key_id for row in listed.json()["provider_keys"])
    assert secret not in listed.text
    assert "encrypted_key" not in listed.text
    assert disabled.status_code == 200
    assert disabled.json()["provider_key"]["status"] == "disabled"
    assert disabled.json()["provider_key"]["disabled_at"] is not None
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_provider_key_id"] == provider_key_id

    assert row["encrypted_key"].startswith("aesgcm-v1:")
    assert row["key_prefix"] == secret[:12]
    assert row["status"] == "disabled"
    assert secret not in "\n".join(str(value) for value in tuple(row))
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT 1 FROM provider_credentials WHERE provider_credential_id=?", (provider_key_id,)
    ).fetchone() is None
    connection.close()


@pytest.mark.anyio
async def test_provider_keypool_permissions_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """test_provider_keypool_permissions_and_validation_errors：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url = prepare_database(monkeypatch, tmp_path)
    provider_id = seed_provider_id(database_url)
    control_app = build_control_app()

    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        developer_denied = await client.get(
            f"/providers/{provider_id}/keys",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        viewer_denied = await client.post(
            f"/providers/{provider_id}/keys",
            headers={"Authorization": f"Bearer {viewer_token}"},
            json={"name": "Viewer key", "plain_value": "sk-viewer-denied"},
        )
        invalid_plain = await client.post(
            f"/providers/{provider_id}/keys",
            headers=admin_headers,
            json={"name": "Invalid", "plain_value": ""},
        )
        invalid_weight = await client.post(
            f"/providers/{provider_id}/keys",
            headers=admin_headers,
            json={"name": "Invalid weight", "plain_value": "sk-weight-invalid", "weight": 0},
        )
        created = await client.post(
            f"/providers/{provider_id}/keys",
            headers=admin_headers,
            json={"name": "Duplicate", "plain_value": "sk-duplicate-provider-key"},
        )
        duplicate = await client.post(
            f"/providers/{provider_id}/keys",
            headers=admin_headers,
            json={"name": "Duplicate again", "plain_value": "sk-duplicate-provider-key"},
        )
        provider_key_id = created.json()["provider_key"]["provider_key_id"]
        invalid_status = await client.patch(
            f"/provider-keys/{provider_key_id}",
            headers=admin_headers,
            json={"status": "rate_limited"},
        )
        empty_patch = await client.patch(
            f"/provider-keys/{provider_key_id}",
            headers=admin_headers,
            json={},
        )
        not_found = await client.patch(
            "/provider-keys/not-found",
            headers=admin_headers,
            json={"status": "disabled"},
        )

    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert invalid_plain.status_code == 422
    assert invalid_plain.json()["code"] == "provider_key.plain_value_invalid"
    assert invalid_weight.status_code == 422
    assert invalid_weight.json()["code"] == "provider_key.weight_invalid"
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "provider_key.duplicate"
    assert invalid_status.status_code == 422
    assert invalid_status.json()["code"] == "provider_key.status_invalid"
    assert empty_patch.status_code == 422
    assert empty_patch.json()["code"] == "provider_key.update_empty"
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "provider_key.not_found"
