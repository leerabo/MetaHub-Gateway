"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_dlp_rules.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.dlp.routes import router as dlp_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试密码只用于 DLP 规则权限矩阵，测试库会在用例内隔离重建。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证和 DLP 规则 API 的控制面测试应用。"""

    app = create_service_app("control-plane-api", "DLP Test Control API")
    app.include_router(auth_router)
    app.include_router(dlp_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """准备 DLP 规则测试所需的租户和三类角色用户。"""

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
                "user_prd024_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd024_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    # 返回数据库 URL 和默认租户，后续断言会确认请求体里的 tenant_id 不能覆盖登录态租户。
    return database_url, seed.tenant_id


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试用户并返回访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 令牌只用于 Authorization 头，测试断言不能把明文 token 写入业务数据。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_dlp_rule_admin_crud_and_tenant_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：管理员创建、查询、更新 DLP 规则，tenant_id 只能来自登录态。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/dlp/rules",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd024_create"},
            json={
                "tenant_id": "tenant_should_be_ignored",
                "name": "高风险关键词",
                "rule_type": "keyword",
                "pattern": "敏感词",
                "action": "log_only",
                "status": "active",
                "priority": 20,
                "description": "先试运行观察误拦截",
            },
        )
        rule_id = created.json()["dlp_rule"]["dlp_rule_id"]
        connection = connect_database(database_url)
        connection.execute(
            """
            INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
            VALUES('tenant_prd071_other', 'prd071-other', 'PRD-071 Other', 'internal_only', 'active', ?, ?)
            """,
            ("2026-08-04T08:00:00Z", "2026-08-04T08:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO dlp_rules(
              dlp_rule_id, tenant_id, name, rule_type, pattern, pattern_hash,
              action, scan_scope, status, priority, created_at, updated_at
            )
            VALUES(?, ?, ?, 'keyword', ?, ?, 'block', 'input', 'active', 1, ?, ?)
            """,
            (
                "dlp_prd071_other_tenant",
                "tenant_prd071_other",
                "其他租户规则",
                "其他租户敏感词",
                "sha256:prd071-other-tenant",
                "2026-08-04T08:00:00Z",
                "2026-08-04T08:00:00Z",
            ),
        )
        connection.commit()
        listed = await client.get(
            "/dlp/rules?status=all&scan_scope=input",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd024_list"},
        )
        updated = await client.patch(
            f"/dlp/rules/{rule_id}",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd024_update"},
            json={"action": "block", "scan_scope": "both", "status": "disabled", "priority": 10},
        )
        cross_tenant_update = await client.patch(
            "/dlp/rules/dlp_prd071_other_tenant",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd071_cross_patch"},
            json={"scan_scope": "both"},
        )

    assert created.status_code == 200, created.text
    created_rule = created.json()["dlp_rule"]
    assert created_rule["tenant_id"] == tenant_id
    assert created_rule["name"] == "高风险关键词"
    assert created_rule["pattern"] == "敏感词"
    assert created_rule["pattern_hash"].startswith("sha256:")
    assert created_rule["scan_scope"] == "input"
    assert "tenant_should_be_ignored" not in created.text
    assert listed.status_code == 200, listed.text
    assert [rule["dlp_rule_id"] for rule in listed.json()["dlp_rules"]] == [rule_id]
    assert listed.json()["dlp_rules"][0]["scan_scope"] == "input"
    assert updated.status_code == 200, updated.text
    updated_rule = updated.json()["dlp_rule"]
    assert updated_rule["action"] == "block"
    assert updated_rule["scan_scope"] == "both"
    assert updated_rule["status"] == "disabled"
    assert updated_rule["priority"] == 10
    assert cross_tenant_update.status_code == 404
    assert cross_tenant_update.json()["code"] == "dlp.rule_not_found"

    connection = connect_database(database_url)
    stored = connection.execute(
        "SELECT tenant_id, scan_scope, created_by, updated_by FROM dlp_rules WHERE dlp_rule_id=?",
        (rule_id,),
    ).fetchone()
    admin_user_id = connection.execute("SELECT user_id FROM users WHERE email=?", ("admin@metahub.local",)).fetchone()["user_id"]
    assert stored["tenant_id"] == tenant_id
    assert stored["scan_scope"] == "both"
    assert stored["created_by"] == admin_user_id
    assert stored["updated_by"] == admin_user_id


@pytest.mark.anyio
async def test_dlp_permissions_regex_validation_and_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：非 admin 被拒绝，危险正则、语法错误、空 PATCH 和不存在规则返回统一 DLP 错误码。"""

    prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        developer_denied = await client.get("/dlp/rules", headers={"Authorization": f"Bearer {developer_token}"})
        viewer_denied = await client.post(
            "/dlp/rules",
            headers={"Authorization": f"Bearer {viewer_token}"},
            json={"name": "viewer", "rule_type": "keyword", "pattern": "x", "action": "log_only"},
        )
        unsafe_regex = await client.post(
            "/dlp/rules",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "unsafe", "rule_type": "regex", "pattern": "(a+)+", "action": "block"},
        )
        invalid_regex = await client.post(
            "/dlp/rules",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "invalid", "rule_type": "regex", "pattern": "[", "action": "block"},
        )
        scope_target = await client.post(
            "/dlp/rules",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "scope-target", "rule_type": "keyword", "pattern": "scope", "action": "log_only"},
        )
        scope_target_id = scope_target.json()["dlp_rule"]["dlp_rule_id"]
        invalid_scope_create = await client.post(
            "/dlp/rules",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "name": "invalid-scope",
                "rule_type": "keyword",
                "pattern": "invalid-scope",
                "action": "block",
                "scan_scope": "sideways",
            },
        )
        invalid_scope_filter = await client.get(
            "/dlp/rules?scan_scope=sideways",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        invalid_scope_patch = await client.patch(
            f"/dlp/rules/{scope_target_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"scan_scope": "sideways"},
        )
        bad_filter = await client.get(
            "/dlp/rules?action=delete",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        empty_patch = await client.patch(
            "/dlp/rules/dlp_missing",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )
        not_found = await client.patch(
            "/dlp/rules/dlp_missing",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"status": "disabled"},
        )

    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert unsafe_regex.status_code == 422
    assert unsafe_regex.json()["code"] == "dlp.regex_unsafe"
    assert "(a+)+" not in unsafe_regex.text
    assert invalid_regex.status_code == 422
    assert invalid_regex.json()["code"] == "dlp.regex_invalid"
    assert scope_target.status_code == 200
    assert invalid_scope_create.status_code == 422
    assert invalid_scope_create.json()["code"] == "dlp.rule_invalid"
    assert invalid_scope_filter.status_code == 422
    assert invalid_scope_filter.json()["code"] == "dlp.rule_invalid"
    assert invalid_scope_patch.status_code == 422
    assert invalid_scope_patch.json()["code"] == "dlp.rule_invalid"
    assert bad_filter.status_code == 422
    assert bad_filter.json()["code"] == "dlp.rule_invalid"
    assert empty_patch.status_code == 422
    assert empty_patch.json()["code"] == "dlp.update_empty"
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "dlp.rule_not_found"
