"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_basic_rbac_users.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient

from metahub_control.auth import AuthContext, require_current_admin, require_virtual_key_writer
from metahub_control.auth.routes import router as auth_router
from metahub_control.users.routes import router as users_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database, verify_password
from metahub_shared.rbac import ROLE_CODES
from metahub_shared.runtime.fastapi_app import create_service_app


def sqlite_url(path: Path) -> str:
    """sqlite_url：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return f"sqlite:///{path.as_posix()}"


def build_rbac_app(database_url: str):
    """build_rbac_app：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    app = create_service_app("control-plane-api", "Control Plane RBAC Test API")
    app.include_router(auth_router)
    app.include_router(users_router)

    @app.post("/fake/providers")
    async def create_provider(
        context: Annotated[AuthContext, Depends(require_current_admin)],
    ) -> dict[str, str]:
        """create_provider：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        # 角色判断是后端权限边界，未命中允许角色时必须统一拒绝。
        return {"tenant_id": context.user.tenant_id, "role": context.user.role}

    @app.post("/fake/virtual-keys")
    async def create_virtual_key(
        context: Annotated[AuthContext, Depends(require_virtual_key_writer)],
    ) -> dict[str, str]:
        """create_virtual_key：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        # 角色判断是后端权限边界，未命中允许角色时必须统一拒绝。
        return {"tenant_id": context.user.tenant_id, "role": context.user.role}

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


async def login_token(
    client: AsyncClient,
    *,
    email: str = "admin@metahub.local",
    password: str = "LocalAdmin!2026",
) -> str:
    """login_token：说明该定义的测试或脚本职责。
    
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
async def test_admin_can_list_create_login_and_change_user_roles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：管理员可管理当前租户用户，响应不泄露密码哈希。"""

    database_url = prepare_database(monkeypatch, tmp_path)
    app = build_rbac_app(database_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin_token = await login_token(client)
        listed = await client.get(
            "/users",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_users_list"},
        )
        created = await client.post(
            "/users",
            json={
                "email": "Dev@Example.com",
                "display_name": "Dev User",
                "password": "LocalDev!2026",
                "role": "developer",
            },
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_users_create"},
        )
        user_id = created.json()["user"]["user_id"]
        dev_token = await login_token(client, email="dev@example.com", password="LocalDev!2026")
        dev_me = await client.get("/auth/me", headers={"Authorization": f"Bearer {dev_token}"})
        changed = await client.patch(
            f"/users/{user_id}/role",
            json={"role": "viewer"},
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_users_role"},
        )
        viewer_me = await client.get("/auth/me", headers={"Authorization": f"Bearer {dev_token}"})

    assert listed.status_code == 200
    assert listed.json()["request_id"] == "req_users_list"
    assert {role["code"] for role in listed.json()["roles"]} == set(ROLE_CODES)
    assert "password_hash" not in listed.text
    assert created.status_code == 201
    assert created.json()["request_id"] == "req_users_create"
    assert created.json()["user"]["email"] == "dev@example.com"
    assert created.json()["user"]["role"] == "developer"
    assert "LocalDev!2026" not in created.text
    assert "password_hash" not in created.text
    assert dev_me.json()["user"]["role"] == "developer"
    assert changed.status_code == 200
    assert changed.json()["user"]["role"] == "viewer"
    assert changed.json()["request_id"] == "req_users_role"
    assert viewer_me.json()["user"]["role"] == "viewer"

    row = connect_database(database_url).execute(
        "SELECT tenant_id, password_hash FROM users WHERE email=?",
        ("dev@example.com",),
    ).fetchone()
    assert row is not None
    assert row["tenant_id"] == created.json()["user"]["tenant_id"]
    assert row["password_hash"] != "LocalDev!2026"
    assert verify_password("LocalDev!2026", row["password_hash"])


@pytest.mark.anyio
async def test_backend_role_guards_block_permission_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限：后端依赖必须拒绝越权调用，不能只依赖控制台隐藏入口。"""

    database_url = prepare_database(monkeypatch, tmp_path)
    app = build_rbac_app(database_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin_token = await login_token(client)
        developer = await client.post(
            "/users",
            json={
                "email": "dev@example.com",
                "display_name": "Dev User",
                "password": "LocalDev!2026",
                "role": "developer",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        viewer = await client.post(
            "/users",
            json={
                "email": "viewer@example.com",
                "display_name": "Viewer User",
                "password": "LocalViewer!2026",
                "role": "viewer",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        developer_token = await login_token(client, email="dev@example.com", password="LocalDev!2026")
        viewer_token = await login_token(client, email="viewer@example.com", password="LocalViewer!2026")

        developer_users = await client.get("/users", headers={"Authorization": f"Bearer {developer_token}"})
        developer_provider = await client.post(
            "/fake/providers",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        developer_virtual_key = await client.post(
            "/fake/virtual-keys",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        viewer_virtual_key = await client.post(
            "/fake/virtual-keys",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )

    assert developer.status_code == 201
    assert viewer.status_code == 201
    assert developer_users.status_code == 403
    assert developer_users.json()["code"] == "auth.permission_denied"
    assert developer_provider.status_code == 403
    assert developer_provider.json()["code"] == "auth.permission_denied"
    assert developer_virtual_key.status_code == 200
    assert developer_virtual_key.json()["role"] == "developer"
    assert viewer_virtual_key.status_code == 403
    assert viewer_virtual_key.json()["code"] == "auth.permission_denied"


@pytest.mark.anyio
async def test_user_management_rejects_invalid_duplicate_and_cross_tenant_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/安全：非法角色、重复邮箱、跨租户 user_id 和自降级都必须被后端拒绝。"""

    database_url = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('other-tenant', 'other', 'Other Tenant', 'internal_only', 'active', datetime('now'), datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO users(user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at)
        VALUES('other-user', 'other-tenant', 'other@example.com', 'Other User', lower(hex(randomblob(32))),
               'viewer', 'active',
               datetime('now'), datetime('now'))
        """
    )
    connection.commit()
    app = build_rbac_app(database_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin_token = await login_token(client)
        admin_list = await client.get("/users", headers={"Authorization": f"Bearer {admin_token}"})
        admin_id = next(user["user_id"] for user in admin_list.json()["users"] if user["role"] == "admin")
        invalid_role = await client.post(
            "/users",
            json={
                "email": "bad-role@example.com",
                "display_name": "Bad Role",
                "password": "LocalBad!2026",
                "role": "owner",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        created = await client.post(
            "/users",
            json={
                "email": "dup@example.com",
                "display_name": "Dup User",
                "password": "LocalDup!2026",
                "role": "viewer",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        duplicate = await client.post(
            "/users",
            json={
                "email": "DUP@example.com",
                "display_name": "Dup User 2",
                "password": "LocalDup!2026",
                "role": "viewer",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        cross = await client.patch(
            "/users/other-user/role",
            json={"role": "admin"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        last_admin = await client.patch(
            f"/users/{admin_id}/role",
            json={"role": "viewer"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        second_admin = await client.post(
            "/users",
            json={
                "email": "admin2@example.com",
                "display_name": "Admin Two",
                "password": "LocalAdmin2!2026",
                "role": "admin",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self_downgrade = await client.patch(
            f"/users/{admin_id}/role",
            json={"role": "viewer"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    other = connect_database(database_url).execute(
        "SELECT role FROM users WHERE tenant_id='other-tenant' AND user_id='other-user'"
    ).fetchone()
    assert invalid_role.status_code == 422
    assert invalid_role.json()["code"] == "user.role_invalid"
    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "user.email_exists"
    assert cross.status_code == 404
    assert cross.json()["code"] == "user.not_found"
    assert other["role"] == "viewer"
    assert last_admin.status_code == 409
    assert last_admin.json()["code"] == "user.last_admin_required"
    assert second_admin.status_code == 201
    assert self_downgrade.status_code == 409
    assert self_downgrade.json()["code"] == "user.self_role_change_denied"


def test_roles_table_is_seeded_for_default_tenant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：默认租户 seed 必须包含当前六种固定角色。"""

    database_url = prepare_database(monkeypatch, tmp_path)
    rows = connect_database(database_url).execute(
        "SELECT code, tenant_id, status FROM roles ORDER BY code"
    ).fetchall()

    assert {row["code"] for row in rows} == set(ROLE_CODES)
    assert {row["status"] for row in rows} == {"active"}
    assert all(row["tenant_id"] for row in rows)
