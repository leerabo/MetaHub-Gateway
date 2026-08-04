"""测试 PRD-041 竞品对比导航、访问事件、角色边界和租户追踪。"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.console.routes import router as console_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试密码仅服务于临时 SQLite 租户，不是可部署或生产凭据。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成项目数据库层支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证与控制台路由的 PRD-041 集成测试应用。"""

    app = create_service_app("control-plane-api", "Competitor Positioning Test API")
    app.include_router(auth_router)
    app.include_router(console_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """创建完整迁移库，并在同一租户准备 admin/developer/viewer 三类用户。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
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
                "user_prd041_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd041_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    # 返回租户 ID 只用于确认服务端归属，测试请求体始终不提交 tenant_id。
    return database_url, seed.tenant_id


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录临时租户用户，并只在当前测试请求头内使用返回令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 令牌只存于当前测试协程内，不进入数据库、日志或断言文本。
    return response.json()["access_token"]


def competitor_event_payload() -> dict[str, object]:
    """返回 event/nav/target/path/hash/source/viewport 完全配对的安全事件体。"""

    return {
        "event_type": "help.competitor_viewed",
        "navigation_item_id": "competitor-comparison",
        "target_section": "competitor-comparison",
        "path": "/#competitor-comparison",
        "payload": {
            "source": "sidebar",
            "hash": "#competitor-comparison",
            "viewport_class": "desktop",
        },
    }


@pytest.mark.anyio
async def test_competitor_navigation_roles_event_tenant_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：三类登录角色均可见帮助页，viewer 事件按认证租户写入并保留 request_id。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        tokens = {
            "admin": await login(client, "admin@metahub.local", ADMIN_PASSWORD),
            "developer": await login(client, "developer@example.com", DEVELOPER_PASSWORD),
            "viewer": await login(client, "viewer@example.com", VIEWER_PASSWORD),
        }
        navigations = {
            role: await client.get(
                "/console/navigation",
                headers={"Authorization": f"Bearer {token}"},
            )
            for role, token in tokens.items()
        }
        accepted = await client.post(
            "/console/events",
            json=competitor_event_payload(),
            headers={
                "Authorization": f"Bearer {tokens['viewer']}",
                "X-Request-ID": "req_prd041_event",
            },
        )

    for response in navigations.values():
        assert response.status_code == 200, response.text
        items = response.json()["navigation_items"]
        competitor = next(item for item in items if item["id"] == "competitor-comparison")
        assert competitor["group"] == "帮助中心"
        assert competitor["target_section"] == "competitor-comparison"
        assert competitor["order"] == 115
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["request_id"] == "req_prd041_event"

    row = connect_database(database_url).execute(
        """
        SELECT tenant_id, user_id, session_id, event_type, navigation_item_id,
               target_section, path, request_id, payload_json
        FROM console_page_events WHERE event_id=?
        """,
        (accepted.json()["event_id"],),
    ).fetchone()
    assert row is not None
    assert row["tenant_id"] == tenant_id
    assert row["user_id"] == "user_prd041_viewer"
    assert row["session_id"]
    assert row["event_type"] == "help.competitor_viewed"
    assert row["navigation_item_id"] == row["target_section"] == "competitor-comparison"
    assert row["path"] == "/#competitor-comparison"
    assert row["request_id"] == "req_prd041_event"
    assert "#competitor-comparison" in row["payload_json"]


@pytest.mark.anyio
async def test_competitor_page_requires_auth_and_rejects_forged_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：匿名导航被拒绝，事件配对、敏感字段和伪造 tenant_id 均不能落库。"""

    database_url, _tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        anonymous = await client.get(
            "/console/navigation",
            headers={"X-Request-ID": "req_prd041_anonymous"},
        )
        invalid_payloads = [
            {**competitor_event_payload(), "event_type": "console.page_viewed"},
            {**competitor_event_payload(), "target_section": "providers"},
            {**competitor_event_payload(), "path": "/#competitor-comparison?token=secret"},
            {
                **competitor_event_payload(),
                "payload": {
                    "source": "sidebar",
                    "hash": "#competitor-comparison",
                    "viewport_class": "desktop",
                    "provider_key": "must-not-enter-db",
                },
            },
            {**competitor_event_payload(), "tenant_id": "tenant_forged"},
        ]
        responses = [
            await client.post("/console/events", json=payload, headers=headers)
            for payload in invalid_payloads
        ]

    assert anonymous.status_code == 401
    assert anonymous.json()["request_id"] == "req_prd041_anonymous"
    assert all(response.status_code == 422 for response in responses)
    assert all(response.json()["code"] == "console.event_invalid" for response in responses[:4])
    stored = connect_database(database_url).execute(
        """
        SELECT COUNT(*) AS count
        FROM console_page_events
        WHERE event_type='help.competitor_viewed'
        """
    ).fetchone()
    assert stored["count"] == 0
    assert "must-not-enter-db" not in str(stored)
