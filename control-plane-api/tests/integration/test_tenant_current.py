"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_tenant_current.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.tenants.routes import router as tenant_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app


def sqlite_url(path: Path) -> str:
    """sqlite_url：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return f"sqlite:///{path.as_posix()}"


def build_tenant_app(database_url: str):
    """build_tenant_app：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    app = create_service_app("control-plane-api", "Control Plane Tenant Test API")
    app.include_router(auth_router)
    app.include_router(tenant_router)
    return app


def prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    password: str = "LocalAdmin!2026",
) -> str:
    """prepare_database：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", password)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    apply_migrations(database_url)
    apply_seed_data(database_url)
    return database_url


async def login_token(client: AsyncClient, password: str = "LocalAdmin!2026") -> str:
    """login_token：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": "admin@metahub.local", "password": password},
    )
    assert response.status_code == 200
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_current_tenant_can_be_read_and_updated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：已登录管理员只能读写认证上下文里的当前租户。"""

    database_url = prepare_database(monkeypatch, tmp_path)
    app = build_tenant_app(database_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await login_token(client)
        before = await client.get(
            "/tenant/current",
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": "req_tenant_get"},
        )
        updated = await client.patch(
            "/tenant/current",
            json={"name": "MetaHub Demo Tenant", "service_mode": "public_service"},
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": "req_tenant_patch"},
        )
        after = await client.get("/tenant/current", headers={"Authorization": f"Bearer {token}"})

    assert before.status_code == 200
    assert before.json()["tenant"]["slug"] == "default"
    assert before.json()["request_id"] == "req_tenant_get"
    assert updated.status_code == 200
    assert updated.json()["tenant"]["name"] == "MetaHub Demo Tenant"
    assert updated.json()["tenant"]["service_mode"] == "public_service"
    assert updated.json()["request_id"] == "req_tenant_patch"
    assert after.json()["tenant"]["name"] == "MetaHub Demo Tenant"


@pytest.mark.anyio
async def test_tenant_current_requires_authentication_and_tenant_settings_permission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：未登录不能读取，缺少 tenant.settings.read 时读写均失败关闭。"""

    database_url = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute("UPDATE users SET role='viewer' WHERE email='admin@metahub.local'")
    connection.commit()
    app = build_tenant_app(database_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.get("/tenant/current")
        token = await login_token(client)
        read_denied = await client.get(
            "/tenant/current",
            headers={"Authorization": f"Bearer {token}"},
        )
        denied = await client.patch(
            "/tenant/current",
            json={"name": "Should Not Update"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert missing.status_code == 401
    assert missing.json()["code"] == "auth.token_missing"
    assert read_denied.status_code == 403
    assert read_denied.json()["code"] == "auth.permission_denied"
    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.permission_denied"


@pytest.mark.anyio
async def test_tenant_update_empty_or_cross_tenant_payload_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/安全：空更新和携带 tenant_id 的跨租户尝试都不能修改租户。"""

    database_url = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('other-tenant', 'other', 'Other Tenant', 'internal_only', 'active', datetime('now'), datetime('now'))
        """
    )
    connection.commit()
    app = build_tenant_app(database_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await login_token(client)
        empty = await client.patch(
            "/tenant/current",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        cross = await client.patch(
            "/tenant/current",
            json={"tenant_id": "other-tenant", "name": "Cross Tenant Write"},
            headers={"Authorization": f"Bearer {token}"},
        )

    other = connect_database(database_url).execute(
        "SELECT name FROM tenants WHERE tenant_id='other-tenant'"
    ).fetchone()
    assert empty.status_code == 422
    assert empty.json()["code"] == "tenant.update_empty"
    assert cross.status_code == 422
    assert cross.json()["code"] == "infra.validation_failed"
    assert other["name"] == "Other Tenant"


def test_mvp_business_tables_have_tenant_id(tmp_path: Path) -> None:
    """安全审计：MVP 业务表必须显式包含 tenant_id，基础设施迁移表例外。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    apply_migrations(database_url)
    connection = connect_database(database_url)
    business_tables = {
        "tenants",
        "seed_runs",
        "roles",
        "users",
        "sessions",
        "virtual_keys",
        "providers",
        "provider_credentials",
        "models",
        "request_logs",
        "usage_ledger",
    }

    for table in business_tables:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        assert "tenant_id" in columns, table

    schema_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(schema_migrations)").fetchall()
    }
    assert "tenant_id" not in schema_columns
