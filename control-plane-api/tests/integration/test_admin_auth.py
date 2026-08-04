"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_admin_auth.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient

from metahub_control.auth import AuthContext, require_current_admin
from metahub_control.auth.routes import router as auth_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database, verify_password
from metahub_shared.runtime.fastapi_app import create_service_app


def sqlite_url(path: Path) -> str:
    """sqlite_url：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return f"sqlite:///{path.as_posix()}"


def build_auth_app(database_url: str):
    """build_auth_app：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    app = create_service_app("control-plane-api", "Control Plane Auth Test API")
    app.include_router(auth_router)

    @app.get("/admin-only")
    async def admin_only(
        context: Annotated[AuthContext, Depends(require_current_admin)],
    ) -> dict[str, str]:
        """admin_only：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
        return {"tenant_id": context.user.tenant_id, "user_id": context.user.user_id}

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


@pytest.mark.anyio
async def test_admin_login_me_and_logout_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """主流程/安全：管理员能登录、查询自己、登出，Token 与明文邮箱均不进入持久化日志。"""

    password = "LocalAdmin!2026"
    database_url = prepare_database(monkeypatch, tmp_path, password=password)
    app = build_auth_app(database_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/auth/login",
            json={"tenant_slug": "default", "email": "admin@metahub.local", "password": password},
            headers={"X-Request-ID": "req_auth_login"},
        )
        token = login.json()["access_token"]
        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        logout = await client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
        after_logout = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert login.status_code == 200
    assert login.json()["request_id"] == "req_auth_login"
    assert login.json()["token_type"] == "Bearer"
    assert login.json()["user"]["tenant_id"]
    assert me.status_code == 200
    assert me.json()["tenant"]["slug"] == "default"
    assert logout.status_code == 200
    assert logout.json()["logged_out"] is True
    assert after_logout.status_code == 401
    assert after_logout.json()["code"] == "auth.session_revoked"

    connection = connect_database(database_url)
    user = connection.execute("SELECT password_hash FROM users WHERE email=?", ("admin@metahub.local",)).fetchone()
    assert user is not None
    assert user["password_hash"] != password
    assert verify_password(password, user["password_hash"])
    dump = "\n".join(str(value) for row in connection.execute("SELECT * FROM sessions") for value in tuple(row))
    assert token not in dump
    assert "eyJ" not in dump
    login_records = [record for record in caplog.records if record.getMessage() == "user.logged_in"]
    assert len(login_records) == 1
    # actor_id 已提供稳定审计关联，登录邮箱不能再复制到结构化 payload 或格式化日志。
    assert "admin@metahub.local" not in repr(login_records[0].__dict__)
    assert getattr(login_records[0], "actor_id", None) == login.json()["user"]["user_id"]


@pytest.mark.anyio
async def test_failed_password_locks_local_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """异常/安全：连续错误密码会锁定账号，未知或已知邮箱均不进入失败日志。"""

    password = "LocalAdmin!2026"
    database_url = prepare_database(monkeypatch, tmp_path, password=password)
    monkeypatch.setenv("METAHUB_LOGIN_MAX_FAILURES", "2")
    monkeypatch.setenv("METAHUB_LOGIN_LOCK_SECONDS", "60")
    app = build_auth_app(database_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        unknown = await client.post(
            "/auth/login",
            json={"email": "privacy-canary@example.com", "password": "wrong-password"},
        )
        first = await client.post(
            "/auth/login",
            json={"email": "admin@metahub.local", "password": "wrong-password"},
        )
        second = await client.post(
            "/auth/login",
            json={"email": "admin@metahub.local", "password": "wrong-password"},
        )
        locked = await client.post(
            "/auth/login",
            json={"email": "admin@metahub.local", "password": password},
        )

    assert unknown.status_code == 401
    assert first.status_code == 401
    assert first.json()["code"] == "auth.login_invalid"
    assert second.status_code == 429
    assert second.json()["code"] == "auth.login_locked"
    assert locked.status_code == 429
    assert locked.json()["code"] == "auth.login_locked"
    failed_records = [record for record in caplog.records if record.getMessage() == "auth.login_failed"]
    assert len(failed_records) == 4
    rendered_records = "\n".join(repr(record.__dict__) for record in failed_records)
    # 未知登录输入和已知账号邮箱都不得出现在结构化 extra 或格式化日志中。
    assert "privacy-canary@example.com" not in rendered_records
    assert "admin@metahub.local" not in rendered_records
    assert "wrong-password" not in rendered_records
    assert all("email" not in getattr(record, "payload", {}) for record in failed_records)


@pytest.mark.anyio
async def test_token_missing_invalid_and_expired_are_unified_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：缺失、伪造、过期 Token 均返回统一错误结构。"""

    password = "LocalAdmin!2026"
    database_url = prepare_database(monkeypatch, tmp_path, password=password)
    app = build_auth_app(database_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.get("/auth/me")
        invalid = await client.get("/auth/me", headers={"Authorization": "Bearer not-a-token"})
        login = await client.post(
            "/auth/login",
            json={"email": "admin@metahub.local", "password": password},
        )
        token = login.json()["access_token"]
        connection = connect_database(database_url)
        connection.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00Z'")
        connection.commit()
        expired = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert missing.status_code == 401
    assert missing.json()["code"] == "auth.token_missing"
    assert invalid.status_code == 401
    assert invalid.json()["code"] == "auth.token_invalid"
    assert expired.status_code == 401
    assert expired.json()["code"] == "auth.token_expired"
    for response in [missing, invalid, expired]:
        assert response.json()["request_id"]
        assert response.json()["trace_id"]


@pytest.mark.anyio
async def test_backend_admin_permission_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限：控制台隐藏按钮不算权限，后端管理员依赖必须拒绝非 admin 角色。"""

    password = "LocalAdmin!2026"
    database_url = prepare_database(monkeypatch, tmp_path, password=password)
    connection = connect_database(database_url)
    connection.execute("UPDATE users SET role='viewer' WHERE email='admin@metahub.local'")
    connection.commit()
    app = build_auth_app(database_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/auth/login",
            json={"email": "admin@metahub.local", "password": password},
        )
        token = login.json()["access_token"]
        denied = await client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})

    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.permission_denied"
