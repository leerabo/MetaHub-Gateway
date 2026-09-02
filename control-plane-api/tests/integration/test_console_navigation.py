"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_console_navigation.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.console.routes import router as console_router
from metahub_control.console.service import EVENT_TYPE_BY_NAVIGATION, NAVIGATION_DEFINITIONS
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试密码只用于登录夹具验证角色化导航，不代表真实租户凭据。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证和控制台导航路由的测试应用。"""

    app = create_service_app("control-plane-api", "Console Navigation Test API")
    app.include_router(auth_router)
    app.include_router(console_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """准备 PRD-018 控制台导航测试所需的租户、用户和空偏好数据。"""

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
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd018_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd018_viewer",
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
    return database_url, seed.tenant_id


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试用户并返回访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_console_navigation_roles_and_preferences(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：不同角色拿到不同导航，开发者保存偏好后重新进入能恢复当前入口。"""

    local_models_definition = next(
        item for item in NAVIGATION_DEFINITIONS if item.id == "local-models"
    )
    assert local_models_definition.required_permissions == ("local_endpoint.read",)
    assert EVENT_TYPE_BY_NAVIGATION["local-models"] == "console.page_viewed"

    prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin_navigation = await client.get(
            "/console/navigation",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd018_admin_nav"},
        )
        developer_navigation = await client.get(
            "/console/navigation",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd018_dev_nav"},
        )
        viewer_navigation = await client.get(
            "/console/navigation",
            headers={"Authorization": f"Bearer {viewer_token}", "X-Request-ID": "req_prd018_viewer_nav"},
        )
        developer_explanation_navigation = await client.get(
            "/console/navigation?active_item_id=request-explanations",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        viewer_explanation_denied = await client.get(
            "/console/navigation?active_item_id=request-explanations",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        developer_local_models_navigation = await client.get(
            "/console/navigation?active_item_id=local-models",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        viewer_local_models_denied = await client.get(
            "/console/navigation?active_item_id=local-models",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        viewer_explanation_preference_denied = await client.patch(
            "/console/preferences",
            json={"active_navigation_item_id": "request-explanations"},
            headers={"Authorization": f"Bearer {viewer_token}", "X-Request-ID": "req_prd054_viewer_pref"},
        )
        saved_preference = await client.patch(
            "/console/preferences",
            json={"active_navigation_item_id": "request-explanations", "sidebar_collapsed": True},
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        restored_navigation = await client.get(
            "/console/navigation",
            headers={"Authorization": f"Bearer {developer_token}"},
        )

    assert admin_navigation.status_code == 200, admin_navigation.text
    admin_payload = admin_navigation.json()
    assert admin_payload["permission_codes"]
    assert admin_payload["permission_revision"] == 1
    assert len(admin_payload["authorization_fingerprint"]) == 64
    assert all(item["required_permissions"] for item in admin_payload["navigation_items"])
    admin_ids = {item["id"] for item in admin_payload["navigation_items"]}
    legacy_provider_engineering_ids = {
        "adapter-specs",
        "adapter-contract-runs",
        "error-maps",
    }
    assert admin_ids.isdisjoint(legacy_provider_engineering_ids)
    assert {
        "providers",
        "local-models",
        "users",
        "role-permissions",
        "environments",
        "config-summary",
        "projects",
        "usage",
        "finops-ledger",
        "model-prices",
        "budgets",
        "cost-anomalies",
        "policies",
        "policy-simulator",
        "routing-policies",
        "dlp-rules",
        "session-risks",
        "request-logs",
        "request-explanations",
        "feedback-summary",
        "audit-logs",
        "sdk-examples",
    }.issubset(admin_ids)

    assert developer_navigation.status_code == 200, developer_navigation.text
    developer_ids = {item["id"] for item in developer_navigation.json()["navigation_items"]}
    assert {
        "projects",
        "environments",
        "virtual-keys",
        "model-prices",
        "routing-policies",
        "request-logs",
        "request-explanations",
        "feedback-summary",
        "api-docs",
        "models",
        "local-models",
        "sdk-examples",
    }.issubset(developer_ids)
    assert developer_ids.isdisjoint(legacy_provider_engineering_ids)
    assert "providers" not in developer_ids
    assert "usage" not in developer_ids
    assert "budgets" not in developer_ids
    assert "users" not in developer_ids
    assert "dlp-rules" not in developer_ids
    assert "session-risks" not in developer_ids
    assert "policies" not in developer_ids
    assert "policy-simulator" not in developer_ids
    # 审计日志包含管理变更前后摘要，developer 不接收导航入口，手写地址仍由审计 API 拒绝。
    assert "audit-logs" not in developer_ids
    assert "finops-ledger" not in developer_ids
    # 成本异常包含财务证据与治理动作，developer 不能通过前端直达恢复 admin-only 入口。
    assert "cost-anomalies" not in developer_ids
    assert developer_explanation_navigation.status_code == 200
    assert (
        developer_explanation_navigation.json()["active_navigation_item_id"]
        == "request-explanations"
    )
    assert developer_local_models_navigation.status_code == 200
    assert (
        developer_local_models_navigation.json()["active_navigation_item_id"]
        == "local-models"
    )

    assert viewer_navigation.status_code == 200, viewer_navigation.text
    viewer_ids = {item["id"] for item in viewer_navigation.json()["navigation_items"]}
    assert {"overview", "api-docs", "system-status"}.issubset(viewer_ids)
    assert "virtual-keys" not in viewer_ids
    assert "usage" not in viewer_ids
    assert "dlp-rules" not in viewer_ids
    assert "session-risks" not in viewer_ids
    assert "policies" not in viewer_ids
    assert "policy-simulator" not in viewer_ids
    assert "environments" not in viewer_ids
    assert "model-prices" not in viewer_ids
    # 路由策略包含租户模型图和版本，viewer 既不能拿导航，也不能靠前端直达 hash 恢复入口。
    assert "routing-policies" not in viewer_ids
    assert "finops-ledger" not in viewer_ids
    # viewer 同样不能接收成本异常导航，真实 GET/PATCH 还会由后端 RBAC 再次拒绝。
    assert "cost-anomalies" not in viewer_ids
    assert "audit-logs" not in viewer_ids
    # 满意度报表属于租户调用事实，viewer 不得收到导航或通过偏好恢复受限入口。
    assert "feedback-summary" not in viewer_ids
    assert viewer_ids.isdisjoint(legacy_provider_engineering_ids)
    assert "local-models" not in viewer_ids
    assert "request-explanations" not in viewer_ids
    assert viewer_explanation_denied.status_code == 422
    assert viewer_explanation_denied.json()["code"] == "console.navigation_invalid"
    assert viewer_local_models_denied.status_code == 422
    assert viewer_local_models_denied.json()["code"] == "console.navigation_invalid"
    # Python 示例归档需要登录开发者权限，viewer 不能只靠前端隐藏后继续保存该导航偏好。
    assert viewer_explanation_preference_denied.status_code == 422
    assert viewer_explanation_preference_denied.json()["code"] == "console.navigation_invalid"
    assert viewer_explanation_preference_denied.json()["request_id"] == "req_prd054_viewer_pref"
    assert "sdk-examples" not in viewer_ids

    assert saved_preference.status_code == 200, saved_preference.text
    preference = saved_preference.json()["preference"]
    assert preference["active_navigation_item_id"] == "request-explanations"
    assert preference["sidebar_collapsed"] is True
    assert restored_navigation.json()["active_navigation_item_id"] == "request-explanations"


@pytest.mark.anyio
async def test_console_navigation_rejects_invalid_items_and_sensitive_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/安全：非法导航和敏感事件 payload 会被后端拒绝，合法事件按租户写入。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        developer_denied = await client.get(
            "/console/navigation?active_item_id=providers",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        bad_preference = await client.patch(
            "/console/preferences",
            json={"active_navigation_item_id": "providers"},
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        valid_event = await client.post(
            "/console/events",
            json={
                "event_type": "console.page_viewed",
                "navigation_item_id": "request-logs",
                "target_section": "request-logs",
                "path": "/#request-logs",
                "payload": {
                    "source": "sidebar",
                    "hash": "#request-logs",
                    "viewport_class": "desktop",
                },
            },
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd018_event"},
        )
        sensitive_event = await client.post(
            "/console/events",
            json={
                "event_type": "console.page_viewed",
                "navigation_item_id": "usage",
                "target_section": "usage",
                "path": "/#usage",
                "payload": {"token": "should-not-enter-db"},
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        mismatched_event = await client.post(
            "/console/events",
            json={
                "event_type": "console.page_viewed",
                "navigation_item_id": "usage",
                "target_section": "providers",
                "path": "/#usage",
                "payload": {
                    "source": "direct_hash",
                    "hash": "#usage",
                    "viewport_class": "desktop",
                },
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert developer_denied.status_code == 422
    assert developer_denied.json()["code"] == "console.navigation_invalid"
    assert bad_preference.status_code == 422
    assert bad_preference.json()["code"] == "console.navigation_invalid"
    assert valid_event.status_code == 200, valid_event.text
    assert valid_event.json()["accepted"] is True
    assert sensitive_event.status_code == 422
    assert sensitive_event.json()["code"] == "console.event_invalid"
    assert mismatched_event.status_code == 422
    assert mismatched_event.json()["code"] == "console.event_invalid"

    row = connect_database(database_url).execute(
        """
        SELECT tenant_id, user_id, event_type, navigation_item_id, request_id, payload_json
        FROM console_page_events
        WHERE event_id=?
        """,
        (valid_event.json()["event_id"],),
    ).fetchone()
    assert row is not None
    assert row["tenant_id"] == tenant_id
    assert row["user_id"] == "user_prd018_developer"
    assert row["event_type"] == "console.page_viewed"
    assert row["navigation_item_id"] == "request-logs"
    assert row["request_id"] == "req_prd018_event"
    assert "should-not-enter-db" not in row["payload_json"]
