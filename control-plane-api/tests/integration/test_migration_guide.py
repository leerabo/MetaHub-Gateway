"""测试 PRD-040 后端迁移指南导航、事件契约、租户边界与数据库失败回滚。

测试只写临时 SQLite 数据库，不接触真实 Provider Key、Virtual Key 或客户代码。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.console import service as console_service
from metahub_control.console.routes import router as console_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试密码仅用于临时租户的 RBAC 登录，不代表任何可部署凭据。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换为项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证和控制台路由的 PRD-040 测试应用。"""

    app = create_service_app("control-plane-api", "Migration Guide Test API")
    app.include_router(auth_router)
    app.include_router(console_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """创建已迁移数据库，并准备 developer/viewer 两种受控角色。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    monkeypatch.setenv("METAHUB_GATEWAY_API_BASE_URL", "https://gateway.example.com/")
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
                "user_prd040_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd040_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    # 返回值只暴露临时数据库定位和认证租户 ID，供后续隔离断言使用。
    return database_url, seed.tenant_id


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录当前临时租户用户并返回受统一中间件保护的访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 令牌只在当前测试请求头中使用，不写入事件 payload 或断言输出。
    return response.json()["access_token"]


def migration_event_payload() -> dict[str, object]:
    """返回 event/nav/path/hash/source/viewport 完全配对的迁移指南事件。"""

    return {
        "event_type": "migration.guide_viewed",
        "navigation_item_id": "migration-guide",
        "target_section": "migration-guide",
        "path": "/#migration-guide",
        "payload": {
            "source": "sidebar",
            "hash": "#migration-guide",
            "viewport_class": "desktop",
        },
    }


@pytest.mark.anyio
async def test_migration_navigation_rbac_tenant_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证 admin/developer 可见入口，viewer 被拒绝，事件归属认证租户并保留 request_id。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin_navigation = await client.get(
            "/console/navigation", headers={"Authorization": f"Bearer {admin_token}"}
        )
        developer_navigation = await client.get(
            "/console/navigation", headers={"Authorization": f"Bearer {developer_token}"}
        )
        viewer_navigation = await client.get(
            "/console/navigation", headers={"Authorization": f"Bearer {viewer_token}"}
        )
        accepted = await client.post(
            "/console/events",
            json=migration_event_payload(),
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd040_event",
            },
        )
        viewer_denied = await client.post(
            "/console/events",
            json=migration_event_payload(),
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        forged_tenant = await client.post(
            "/console/events",
            json={**migration_event_payload(), "tenant_id": "tenant_other"},
            headers={"Authorization": f"Bearer {developer_token}"},
        )

    admin_items = admin_navigation.json()["navigation_items"]
    developer_items = developer_navigation.json()["navigation_items"]
    viewer_ids = {item["id"] for item in viewer_navigation.json()["navigation_items"]}
    assert [item["id"] for item in admin_items if 105 <= item["order"] <= 110] == [
        "sdk-examples",
        "migration-guide",
        "api-docs",
    ]
    assert "migration-guide" in {item["id"] for item in developer_items}
    assert "migration-guide" not in viewer_ids
    assert developer_navigation.json()["gateway_base_url"] == "https://gateway.example.com/v1"
    assert developer_navigation.json()["gateway_base_url_status"] == "ready"
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["request_id"] == "req_prd040_event"
    assert viewer_denied.status_code == 422
    assert viewer_denied.json()["code"] == "console.event_invalid"
    assert forged_tenant.status_code == 422

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
    assert row["user_id"] == "user_prd040_developer"
    assert row["session_id"]
    assert row["event_type"] == "migration.guide_viewed"
    assert row["navigation_item_id"] == row["target_section"] == "migration-guide"
    assert row["path"] == "/#migration-guide"
    assert row["request_id"] == "req_prd040_event"
    assert "#migration-guide" in row["payload_json"]


@pytest.mark.anyio
async def test_migration_event_rejects_every_pairing_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证 event/nav/path/hash/source/viewport 任一不匹配都返回统一 422 且不落库。"""

    database_url, _tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        invalid_payloads = [
            {**migration_event_payload(), "event_type": "console.page_viewed"},
            {**migration_event_payload(), "path": "/#migration-guide?token=hidden"},
            {
                **migration_event_payload(),
                "payload": {
                    "source": "sidebar",
                    "hash": "#api-docs",
                    "viewport_class": "desktop",
                },
            },
            {
                **migration_event_payload(),
                "payload": {
                    "source": "prompt-secret",
                    "hash": "#migration-guide",
                    "viewport_class": "tablet",
                },
            },
        ]
        responses = [
            await client.post("/console/events", json=payload, headers=headers)
            for payload in invalid_payloads
        ]

    assert all(response.status_code == 422 for response in responses)
    assert all(response.json()["code"] == "console.event_invalid" for response in responses)
    row = connect_database(database_url).execute(
        """
        SELECT COUNT(*) AS count
        FROM console_page_events
        WHERE event_type='migration.guide_viewed'
        """
    ).fetchone()
    assert row["count"] == 0


@pytest.mark.anyio
async def test_migration_event_commit_failure_rolls_back_and_returns_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证事件 commit 异常调用 rollback，并返回携带统一 request_id 的可重试 503。"""

    database_url, _tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        real_connection = connect_database(database_url)
        failing_connection = Mock(wraps=real_connection)
        failing_connection.commit.side_effect = sqlite3.OperationalError("commit failed")
        monkeypatch.setattr(
            console_service,
            "connect_database",
            Mock(return_value=failing_connection),
        )
        response = await client.post(
            "/console/events",
            json=migration_event_payload(),
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd040_db_failure",
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == "console.event_unavailable"
    assert response.json()["request_id"] == "req_prd040_db_failure"
    assert failing_connection.rollback.called
    row = connect_database(database_url).execute(
        """
        SELECT COUNT(*) AS count
        FROM console_page_events
        WHERE event_type='migration.guide_viewed'
        """
    ).fetchone()
    assert row["count"] == 0


@pytest.mark.anyio
async def test_migration_event_connection_failure_returns_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证事件数据库连接失败不泄露底层信息，并保留请求链路标识。"""

    prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        unavailable = Mock(
            side_effect=DbError("db.connection_failed", "private path", "private detail")
        )
        monkeypatch.setattr(console_service, "connect_database", unavailable)
        response = await client.post(
            "/console/events",
            json=migration_event_payload(),
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd040_connect_failure",
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == "console.event_unavailable"
    assert response.json()["request_id"] == "req_prd040_connect_failure"
    assert "private" not in response.text
