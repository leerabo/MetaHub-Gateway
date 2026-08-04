"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_user_preferences.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.console.routes import router as console_router
from metahub_control.user_preferences.routes import router as user_preferences_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# PRD-027 测试账号只用于验证语言偏好、导航入口和权限边界，不代表真实租户凭据。
ADMIN_PASSWORD = "LocalAdmin!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造包含认证、控制台导航和用户偏好路由的测试应用。"""

    app = create_service_app("control-plane-api", "User Preference Test API")
    app.include_router(auth_router)
    app.include_router(console_router)
    app.include_router(user_preferences_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """准备 PRD-027 语言偏好测试所需的默认租户、管理员和 viewer 用户。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES('user_prd027_viewer', ?, 'viewer@example.com', 'Viewer User', ?, 'viewer', 'active',
               datetime('now'), datetime('now'))
        """,
        (seed.tenant_id, hash_password(VIEWER_PASSWORD)),
    )
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_other_prd027', 'other-prd027', 'Other Locale Tenant', 'internal_only', 'active',
               datetime('now'), datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES('user_other_prd027', 'tenant_other_prd027', 'other-prd027@example.com', 'Other Locale User',
               'not-a-login-hash', 'viewer', 'active', datetime('now'), datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO user_preferences(
          preference_id, tenant_id, user_id, active_navigation_item_id, sidebar_collapsed,
          locale, created_at, updated_at
        )
        VALUES('upr_other_prd027', 'tenant_other_prd027', 'user_other_prd027', 'overview', 0,
               'en-US', datetime('now'), datetime('now'))
        """
    )
    connection.commit()
    # 其他租户偏好用于证明 /user/preferences 只按 AuthContext 读取当前用户数据。
    return database_url, seed.tenant_id


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试用户并返回控制面访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只放在 Authorization 头里，后续偏好接口不能把 token 写入事件 payload。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_user_preferences_default_locale_update_event_and_navigation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：默认中文、切换英文、写入稳定事件，并在控制台导航中暴露语言设置入口。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        initial = await client.get(
            "/user/preferences",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd027_initial"},
        )
        updated = await client.patch(
            "/user/preferences",
            json={"locale": "en-US"},
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd027_update"},
        )
        repeated = await client.patch(
            "/user/preferences",
            json={"locale": "en-US"},
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd027_repeat"},
        )
        viewer_navigation = await client.get(
            "/console/navigation",
            headers={"Authorization": f"Bearer {viewer_token}", "X-Request-ID": "req_prd027_nav"},
        )

    assert initial.status_code == 200, initial.text
    preference = initial.json()["preference"]
    assert preference["tenant_id"] == tenant_id
    assert preference["locale"] == "zh-CN"
    assert {item["locale"] for item in preference["available_locales"]} == {"zh-CN", "en-US"}
    assert "tenant_other_prd027" not in initial.text

    assert updated.status_code == 200, updated.text
    assert updated.json()["request_id"] == "req_prd027_update"
    assert updated.json()["preference"]["locale"] == "en-US"
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["preference"]["locale"] == "en-US"

    connection = connect_database(database_url)
    events = connection.execute(
        """
        SELECT tenant_id, user_id, event_type, old_locale, new_locale, request_id, payload_json
        FROM user_preference_events
        WHERE tenant_id=? AND user_id=?
        """,
        (tenant_id, preference["user_id"]),
    ).fetchall()
    assert len(events) == 1
    assert events[0]["event_type"] == "user.locale_changed"
    assert events[0]["old_locale"] == "zh-CN"
    assert events[0]["new_locale"] == "en-US"
    assert events[0]["request_id"] == "req_prd027_update"
    assert "token" not in events[0]["payload_json"].lower()

    assert viewer_navigation.status_code == 200, viewer_navigation.text
    nav_ids = {item["id"] for item in viewer_navigation.json()["navigation_items"]}
    assert "language-settings" in nav_ids


@pytest.mark.anyio
async def test_user_preferences_reject_invalid_payload_and_missing_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：非法 locale、夹带 tenant_id 和未登录请求都必须走统一错误码。"""

    prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        invalid_locale = await client.patch(
            "/user/preferences",
            json={"locale": "fr-FR"},
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd027_bad_locale"},
        )
        extra_field = await client.patch(
            "/user/preferences",
            json={"locale": "en-US", "tenant_id": "tenant_other_prd027"},
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd027_extra"},
        )
        missing_payload = await client.patch(
            "/user/preferences",
            json={},
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd027_empty"},
        )
        anonymous = await client.get("/user/preferences", headers={"X-Request-ID": "req_prd027_anonymous"})

    assert invalid_locale.status_code == 422
    assert invalid_locale.json()["code"] == "user_preference.locale_invalid"
    assert invalid_locale.json()["request_id"] == "req_prd027_bad_locale"
    assert extra_field.status_code == 422
    assert extra_field.json()["code"] == "user_preference.payload_invalid"
    assert missing_payload.status_code == 422
    assert missing_payload.json()["code"] == "user_preference.payload_invalid"
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "auth.token_missing"
    assert anonymous.json()["request_id"] == "req_prd027_anonymous"


@pytest.mark.anyio
async def test_error_codes_include_localized_labels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程/异常：错误码保持英文 code，同时展示层可按 locale 获取中文或英文 label。"""

    prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        zh_response = await client.get("/error-codes?locale=zh-CN")
        en_response = await client.get("/error-codes?locale=en-US")
        fallback_response = await client.get("/error-codes?locale=fr-FR")

    assert zh_response.status_code == 200
    zh_codes = {item["code"]: item for item in zh_response.json()["error_codes"]}
    en_codes = {item["code"]: item for item in en_response.json()["error_codes"]}
    fallback_codes = {item["code"]: item for item in fallback_response.json()["error_codes"]}
    assert zh_codes["auth.permission_denied"]["code"] == "auth.permission_denied"
    assert zh_codes["auth.permission_denied"]["label"] == "权限不足"
    assert en_codes["auth.permission_denied"]["label"] == "Permission denied"
    assert fallback_codes["user_preference.locale_invalid"]["label"] == "语言不受支持"
    assert zh_codes["auth.permission_denied"]["message"] == "Permission denied."
