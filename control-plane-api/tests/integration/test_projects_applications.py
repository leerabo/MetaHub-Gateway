"""模块职责：验证 PRD-029 Project/Application 主流程、权限拦截和归属聚合。
维护边界：测试必须覆盖 tenant_id 隔离、Virtual Key 绑定、日志筛选、用量汇总和预算归因，避免项目/应用能力只在控制台层生效。
来源文件：control-plane-api/tests/integration/test_projects_applications.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.budgets.routes import router as budgets_router
from metahub_control.environments.routes import router as environments_router
from metahub_control.projects.routes import router as projects_router
from metahub_control.request_logs.routes import router as request_logs_router
from metahub_control.usage.routes import router as usage_router
from metahub_control.virtual_keys.routes import router as virtual_keys_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试用户密码只在临时库内使用，避免把真实 Provider Key 或业务 Key 写入日志。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
FINANCE_PASSWORD = "FinanceLocal!2026"


def sqlite_url(path: Path) -> str:
    """把 pytest 临时数据库路径转换为应用可识别的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含 PRD-029 依赖路由的控制面应用，缩小集成测试边界。"""

    app = create_service_app("control-plane-api", "Project Application Test Control API")
    app.include_router(auth_router)
    app.include_router(environments_router)
    app.include_router(projects_router)
    app.include_router(virtual_keys_router)
    app.include_router(request_logs_router)
    app.include_router(usage_router)
    app.include_router(budgets_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """准备管理员、开发者、财务、只读用户和跨租户噪声数据的临时数据库。"""

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
                "user_prd029_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd029_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
            (
                "user_prd029_finance",
                seed.tenant_id,
                "finance@example.com",
                "Finance User",
                hash_password(FINANCE_PASSWORD),
                "finance_admin",
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_other_prd029', 'other-prd029', 'Other Tenant', 'internal_only', 'active', datetime('now'), datetime('now'))
        """
    )
    connection.commit()
    # 返回 database_url 与 tenant_id，后续请求和直连插数保持同一租户边界。
    return database_url, seed.tenant_id


async def login(client: AsyncClient, email: str, password: str) -> str:
    """通过真实登录接口获取访问令牌，确保后续权限依赖走完整鉴权链路。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 令牌只在测试内存中传递，不写入日志，避免污染权限与密钥审计样例。
    return response.json()["access_token"]


async def create_movable_application(
    client: AsyncClient,
    admin_token: str,
    *,
    code_prefix: str,
) -> tuple[str, str, str, list[str]]:
    """通过真实管理员 API 创建两个项目、一个应用和两把待迁移 Key。"""

    headers = {"Authorization": f"Bearer {admin_token}"}
    source_response = await client.post(
        "/projects",
        headers={**headers, "X-Request-ID": f"req_{code_prefix}_source"},
        json={"project_code": f"{code_prefix}src", "name": "迁移源项目"},
    )
    target_response = await client.post(
        "/projects",
        headers={**headers, "X-Request-ID": f"req_{code_prefix}_target"},
        json={"project_code": f"{code_prefix}dst", "name": "迁移目标项目"},
    )
    assert source_response.status_code == 201, source_response.text
    assert target_response.status_code == 201, target_response.text
    source_project_id = source_response.json()["project"]["project_id"]
    target_project_id = target_response.json()["project"]["project_id"]
    application_response = await client.post(
        "/applications",
        headers={**headers, "X-Request-ID": f"req_{code_prefix}_application"},
        json={
            "project_id": source_project_id,
            "application_code": f"{code_prefix}app",
            "name": "批量迁移应用",
        },
    )
    assert application_response.status_code == 201, application_response.text
    application_id = application_response.json()["application"]["application_id"]
    virtual_key_ids: list[str] = []
    for index in range(2):
        key_response = await client.post(
            "/virtual-keys",
            headers={**headers, "X-Request-ID": f"req_{code_prefix}_key_{index}"},
            json={
                "name": f"迁移 Key {index}",
                "environment": "dev",
                "project_id": source_project_id,
                "application_id": application_id,
            },
        )
        assert key_response.status_code == 201, key_response.text
        virtual_key_ids.append(key_response.json()["virtual_key"]["virtual_key_id"])
    return source_project_id, target_project_id, application_id, virtual_key_ids


def insert_other_tenant_application_key(database_url: str) -> tuple[str, str]:
    """写入另一租户的 Application 与 Key 噪声，验证迁移不会越过 tenant_id。"""

    connection = connect_database(database_url)
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        ("tenant_other_prd029",),
    ).fetchone()
    assert environment is not None
    connection.execute(
        """
        INSERT INTO projects(
          project_id, tenant_id, project_code, name, status, created_at, updated_at
        ) VALUES(
          'prj_other_prd058', 'tenant_other_prd029', 'other058', '其他租户项目',
          'active', '2026-07-31T08:00:00Z', '2026-07-31T08:00:00Z'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO applications(
          application_id, tenant_id, project_id, application_code, name, status,
          created_at, updated_at
        ) VALUES(
          'app_other_prd058', 'tenant_other_prd029', 'prj_other_prd058',
          'otherapp058', '其他租户应用', 'active',
          '2026-07-31T08:00:00Z', '2026-07-31T08:00:00Z'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
          environment, environment_id, project_id, application_id, created_at, updated_at
        ) VALUES(
          'vk_other_prd058', 'tenant_other_prd029', '其他租户 Key', 'mhv_dev_other',
          'hash_other_prd058', 'active', 'dev', ?, 'prj_other_prd058',
          'app_other_prd058', '2026-07-31T08:00:00Z', '2026-07-31T08:00:00Z'
        )
        """,
        (environment["environment_id"],),
    )
    connection.commit()
    connection.close()
    return "app_other_prd058", "vk_other_prd058"


def insert_project_usage(database_url: str, tenant_id: str, virtual_key_id: str, project_id: str, application_id: str) -> None:
    """插入带项目/应用归属的日志和用量数据，用于验证筛选与聚合口径。"""

    connection = connect_database(database_url)
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (tenant_id,),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (tenant_id,),
    ).fetchone()
    assert provider is not None
    assert model is not None
    # 项目归属日志仍沿用未发布 Policy 基线；每条日志先创建同租户不可变 claim。
    connection.executemany(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
        """,
        [
            (
                tenant_id,
                "req_prd029_project",
                "trc_prd029_project",
                virtual_key_id,
                "2026-07-09T09:00:00Z",
            ),
            (
                tenant_id,
                "req_prd029_unbound",
                "trc_prd029_unbound",
                virtual_key_id,
                "2026-07-09T10:00:00Z",
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, project_id, application_id, provider_id, model_id,
          endpoint, request_model, resolved_model, provider_code, status, error_code,
          latency_ms, input_tokens, output_tokens, total_tokens, estimated_cost_amount,
          currency, price_schema_version, created_at, updated_at
        )
        VALUES(
          'req_prd029_project', ?, 'trc_prd029_project', ?, ?, ?, ?, ?,
          '/v1/chat/completions', 'company-default', 'default-chat', 'local_openai',
          'success', NULL, 25, 40, 10, 50, 1.25, 'CNY', 'prd029', '2026-07-09T09:00:00Z', '2026-07-09T09:00:01Z'
        )
        """,
        (tenant_id, virtual_key_id, project_id, application_id, provider["provider_id"], model["model_id"]),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          endpoint, request_model, resolved_model, provider_code, status, error_code,
          latency_ms, input_tokens, output_tokens, total_tokens, estimated_cost_amount,
          currency, price_schema_version, created_at, updated_at
        )
        VALUES(
          'req_prd029_unbound', ?, 'trc_prd029_unbound', ?, ?, ?,
          '/v1/chat/completions', 'company-default', 'default-chat', 'local_openai',
          'success', NULL, 33, 5, 5, 10, 0.5, 'CNY', 'prd029', '2026-07-09T10:00:00Z', '2026-07-09T10:00:01Z'
        )
        """,
        (tenant_id, virtual_key_id, provider["provider_id"], model["model_id"]),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens, total_tokens, cache_tokens,
          raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd, currency, price_schema_version,
          estimated, usage_source, model_id, provider_id, virtual_key_id, project_id, application_id,
          endpoint, request_model, resolved_model, provider_code, created_at, updated_at
        )
        VALUES(
          'usg_prd029_project', ?, 'req_prd029_project', 40, 10, 50, 0,
          '{}', '{"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}',
          1.25, 1.25, 0, 'CNY', 'prd029', 0, 'upstream', ?, ?, ?, ?, ?,
          '/v1/chat/completions', 'company-default', 'default-chat', 'local_openai',
          '2026-07-09T09:00:01Z', '2026-07-09T09:00:01Z'
        )
        """,
        (tenant_id, model["model_id"], provider["provider_id"], virtual_key_id, project_id, application_id),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens, total_tokens, cache_tokens,
          raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd, currency, price_schema_version,
          estimated, usage_source, model_id, provider_id, virtual_key_id,
          endpoint, request_model, resolved_model, provider_code, created_at, updated_at
        )
        VALUES(
          'usg_prd029_unbound', ?, 'req_prd029_unbound', 5, 5, 10, 0,
          '{}', '{"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}',
          0.5, 0.5, 0, 'CNY', 'prd029', 0, 'upstream', ?, ?, ?,
          '/v1/chat/completions', 'company-default', 'default-chat', 'local_openai',
          '2026-07-09T10:00:01Z', '2026-07-09T10:00:01Z'
        )
        """,
        (tenant_id, model["model_id"], provider["provider_id"], virtual_key_id),
    )
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, snapshot_version,
          policy_snapshot_status, claimed_at
        ) VALUES('tenant_other_prd029', 'req_other_prd029', 'trc_other_prd029',
                 NULL, 'disabled', datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO request_logs(request_id, tenant_id, trace_id, status, created_at, updated_at)
        VALUES('req_other_prd029', 'tenant_other_prd029', 'trc_other_prd029', 'success', datetime('now'), datetime('now'))
        """
    )
    connection.commit()


@pytest.mark.anyio
async def test_project_application_key_usage_log_and_budget_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证项目、应用、Key 绑定后，日志、用量和预算都按项目维度正确归因。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        finance_token = await login(client, "finance@example.com", FINANCE_PASSWORD)
        test_environment_response = await client.post(
            "/environments",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"code": "test", "name": "测试环境"},
        )
        assert test_environment_response.status_code == 201, test_environment_response.text
        test_environment_id = test_environment_response.json()["environment"]["environment_id"]
        project_response = await client.post(
            "/projects",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd029_project_create"},
            json={"project_code": "prd029", "name": "PRD029 项目", "description": "账务归因验证"},
        )
        project = project_response.json()["project"]
        application_response = await client.post(
            "/applications",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd029_app_create"},
            json={"project_id": project["project_id"], "application_code": "billing", "name": "计费应用"},
        )
        application = application_response.json()["application"]
        key_response = await client.post(
            "/virtual-keys",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd029_key_create"},
            json={
                "name": "PRD029 Key",
                    "environment_id": test_environment_id,
                "project_id": project["project_id"],
                "application_id": application["application_id"],
            },
        )
        key = key_response.json()["virtual_key"]
        insert_project_usage(database_url, tenant_id, key["virtual_key_id"], project["project_id"], application["application_id"])
        project_list = await client.get(
            "/projects",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd029_project_list"},
        )
        application_list = await client.get(
            f"/applications?project_id={project['project_id']}",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd029_app_list"},
        )
        project_logs = await client.get(
            f"/request-logs?project_id={project['project_id']}",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd029_log_filter"},
        )
        project_summary = await client.get(
            f"/usage/summary?start_date=2026-07-09&end_date=2026-07-09&project_id={project['project_id']}",
            headers={"Authorization": f"Bearer {finance_token}", "X-Request-ID": "req_prd029_usage_summary"},
        )
        app_records = await client.get(
            f"/usage/records?application_id={application['application_id']}",
            headers={"Authorization": f"Bearer {finance_token}", "X-Request-ID": "req_prd029_usage_records"},
        )
        # PRD-059 将财务域从 Developer 明确拆出；项目读取权不能隐式放大为用量读取权。
        developer_usage_denied = await client.get(
            f"/usage/summary?start_date=2026-07-09&end_date=2026-07-09&project_id={project['project_id']}",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd029_developer_usage_denied",
            },
        )
        budget = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd029_budget"},
            json={"scope_type": "project", "scope_id": project["project_id"], "daily_budget_cny": "100"},
        )
        budget_status = await client.get(
            f"/budget-status?project_id={project['project_id']}",
            headers={"Authorization": f"Bearer {finance_token}", "X-Request-ID": "req_prd029_budget_status"},
        )
        developer_budget_denied = await client.get(
            f"/budget-status?project_id={project['project_id']}",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd029_developer_budget_denied",
            },
        )

    assert project_response.status_code == 201, project_response.text
    assert application_response.status_code == 201, application_response.text
    assert key_response.status_code == 201, key_response.text
    assert key["project_id"] == project["project_id"]
    assert key["application_id"] == application["application_id"]
    assert key["project_name"] == "PRD029 项目"
    assert key["application_name"] == "计费应用"
    assert project_list.status_code == 200, project_list.text
    listed_project = next(item for item in project_list.json()["projects"] if item["project_id"] == project["project_id"])
    assert listed_project["application_count"] == 1
    assert listed_project["virtual_key_count"] == 1
    assert application_list.status_code == 200, application_list.text
    assert application_list.json()["applications"][0]["virtual_key_count"] == 1
    assert project_logs.status_code == 200, project_logs.text
    assert project_logs.json()["total"] == 1
    assert project_logs.json()["request_logs"][0]["project_id"] == project["project_id"]
    assert "req_prd029_unbound" not in project_logs.text
    assert "req_other_prd029" not in project_logs.text
    assert project_summary.status_code == 200, project_summary.text
    assert project_summary.json()["totals"]["request_count"] == 1
    assert project_summary.json()["by_project"][0]["project_id"] == project["project_id"]
    assert project_summary.json()["by_application"][0]["application_id"] == application["application_id"]
    assert app_records.status_code == 200, app_records.text
    assert app_records.json()["total"] == 1
    assert app_records.json()["usage_records"][0]["application_id"] == application["application_id"]
    assert budget.status_code == 200, budget.text
    assert budget.json()["budget_rule"]["scope_type"] == "project"
    assert budget_status.status_code == 200, budget_status.text
    assert budget_status.json()["statuses"][0]["scope_id"] == project["project_id"]
    for denied in (developer_usage_denied, developer_budget_denied):
        # 财务数据即使按 Developer 可见项目筛选，也必须由后端 RBAC 固定返回统一 403。
        assert denied.status_code == 403
        assert denied.json()["code"] == "auth.permission_denied"


@pytest.mark.anyio
async def test_project_application_permissions_and_binding_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证只读/开发者权限被后端拦截，并拒绝跨项目绑定 Application。"""

    prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        viewer_denied = await client.get("/projects", headers={"Authorization": f"Bearer {viewer_token}"})
        developer_denied = await client.post(
            "/projects",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={"project_code": "dev029", "name": "开发者禁用"},
        )
        bad_code = await client.post(
            "/projects",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"project_code": "1bad", "name": "非法项目"},
        )
        project_a = await client.post(
            "/projects",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"project_code": "pa029", "name": "项目 A"},
        )
        project_b = await client.post(
            "/projects",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"project_code": "pb029", "name": "项目 B"},
        )
        app_a = await client.post(
            "/applications",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "project_id": project_a.json()["project"]["project_id"],
                "application_code": "appa",
                "name": "?? A",
            },
        )
        mismatched_key = await client.post(
            "/virtual-keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "name": "错绑 Key",
                "environment": "dev",
                "project_id": project_b.json()["project"]["project_id"],
                "application_id": app_a.json()["application"]["application_id"],
            },
        )

    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert bad_code.status_code == 422
    assert bad_code.json()["code"] == "project.code_invalid"
    assert project_a.status_code == 201, project_a.text
    assert project_b.status_code == 201, project_b.text
    assert app_a.status_code == 201, app_a.text
    assert mismatched_key.status_code == 422
    assert mismatched_key.json()["code"] == "application.project_mismatch"


@pytest.mark.anyio
async def test_application_code_is_generated_for_user_facing_create_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """应用创建不要求用户填写内部编码，旧非法值也应回退为自动编码。"""

    prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        project_response = await client.post(
            "/projects",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"project_code": "auto-app", "name": "自动编码项目"},
        )
        project_id = project_response.json()["project"]["project_id"]
        generated = await client.post(
            "/applications",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"project_id": project_id, "name": "中文应用"},
        )
        legacy_invalid = await client.post(
            "/applications",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"project_id": project_id, "application_code": "提示", "name": "旧页面应用"},
        )

    assert project_response.status_code == 201, project_response.text
    assert generated.status_code == 201, generated.text
    assert generated.json()["application"]["application_code"].startswith("app-")
    assert legacy_invalid.status_code == 201, legacy_invalid.text
    assert legacy_invalid.json()["application"]["application_code"].startswith("app-")


@pytest.mark.anyio
async def test_application_project_move_audits_each_key_and_isolates_tenant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证管理员迁移应用时逐 Key 审计、提交后投递，并保持跨租户数据不变。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    other_application_id, other_virtual_key_id = insert_other_tenant_application_key(database_url)
    control_app = build_control_app()
    transport = ASGITransport(app=control_app)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        source_id, target_id, application_id, virtual_key_ids = await create_movable_application(
            client,
            admin_token,
            code_prefix="prd058m",
        )
        denied = await client.patch(
            f"/applications/{application_id}",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd058_move_denied",
            },
            json={"project_id": target_id},
        )

        from metahub_control.projects import service as projects_service

        dispatched: list[tuple[str, str, str]] = []

        def capture_committed_dispatch(
            _config,
            _context,
            record,
            *,
            request_id: str,
            trace_id: str,
        ) -> None:
            """在投递回调中用新连接确认业务行和审计行已经提交。"""

            verification = connect_database(database_url)
            key_project = verification.execute(
                "SELECT project_id FROM virtual_keys WHERE tenant_id=? AND virtual_key_id=?",
                (tenant_id, record.object_id),
            ).fetchone()
            audit_count = verification.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE tenant_id=? AND audit_log_id=?",
                (tenant_id, record.audit_log_id),
            ).fetchone()[0]
            verification.close()
            assert key_project is not None and key_project["project_id"] == target_id
            assert audit_count == 1
            dispatched.append((record.object_id, request_id, trace_id))

        monkeypatch.setattr(projects_service, "dispatch_audit_recorded", capture_committed_dispatch)
        moved = await client.patch(
            f"/applications/{application_id}",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd058_application_move",
                "X-Trace-ID": "trc_prd058_application_move",
                "User-Agent": "MetaHub-PRD058-Application-Move",
            },
            json={"project_id": target_id},
        )

    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.permission_denied"
    assert moved.status_code == 200, moved.text
    assert moved.json()["application"]["project_id"] == target_id
    assert moved.json()["request_id"] == "req_prd058_application_move"
    assert moved.json()["trace_id"] == "trc_prd058_application_move"
    assert {item[0] for item in dispatched} == set(virtual_key_ids)
    assert all(
        item[1:] == ("req_prd058_application_move", "trc_prd058_application_move")
        for item in dispatched
    )

    connection = connect_database(database_url)
    moved_keys = connection.execute(
        """
        SELECT virtual_key_id, project_id
        FROM virtual_keys
        WHERE tenant_id=? AND application_id=?
        ORDER BY virtual_key_id
        """,
        (tenant_id, application_id),
    ).fetchall()
    audit_rows = connection.execute(
        """
        SELECT object_id, action, object_type, before_summary_json, after_summary_json,
               changed_fields_json, client_ip, user_agent, request_id, trace_id
        FROM audit_logs
        WHERE tenant_id=? AND request_id='req_prd058_application_move'
        ORDER BY object_id
        """,
        (tenant_id,),
    ).fetchall()
    other_application = connection.execute(
        "SELECT project_id FROM applications WHERE tenant_id=? AND application_id=?",
        ("tenant_other_prd029", other_application_id),
    ).fetchone()
    other_key = connection.execute(
        "SELECT project_id FROM virtual_keys WHERE tenant_id=? AND virtual_key_id=?",
        ("tenant_other_prd029", other_virtual_key_id),
    ).fetchone()
    connection.close()

    assert {row["virtual_key_id"] for row in moved_keys} == set(virtual_key_ids)
    assert all(row["project_id"] == target_id for row in moved_keys)
    assert len(audit_rows) == len(virtual_key_ids) == 2
    assert {row["object_id"] for row in audit_rows} == set(virtual_key_ids)
    for row in audit_rows:
        assert row["action"] == "virtual_key.updated"
        assert row["object_type"] == "virtual_key"
        assert json.loads(row["changed_fields_json"]) == ["project_id"]
        before = json.loads(row["before_summary_json"])
        after = json.loads(row["after_summary_json"])
        assert before["project_id"] == source_id
        assert after["project_id"] == target_id
        assert {key: value for key, value in before.items() if key != "project_id"} == {
            key: value for key, value in after.items() if key != "project_id"
        }
        assert row["client_ip"] == "127.0.0.1"
        assert row["user_agent"] == "MetaHub-PRD058-Application-Move"
        assert row["request_id"] == "req_prd058_application_move"
        assert row["trace_id"] == "trc_prd058_application_move"
    assert other_application is not None and other_application["project_id"] == "prj_other_prd058"
    assert other_key is not None and other_key["project_id"] == "prj_other_prd058"


@pytest.mark.anyio
async def test_application_project_move_rolls_back_when_any_key_audit_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证第二把 Key 审计失败时 Application、全部 Key 和首条审计一起回滚。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    transport = ASGITransport(app=control_app)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        source_id, target_id, application_id, virtual_key_ids = await create_movable_application(
            client,
            admin_token,
            code_prefix="prd058r",
        )

        from metahub_control.projects import service as projects_service

        real_record_audit_log = projects_service.record_audit_log
        attempted_object_ids: list[str] = []
        unexpected_dispatches: list[str] = []

        def fail_second_audit(*args, **kwargs):
            """先真实写入审计行，再在第二条后抛错，验证事务可撤销部分写入。"""

            record = real_record_audit_log(*args, **kwargs)
            attempted_object_ids.append(kwargs["object_id"])
            if len(attempted_object_ids) == 2:
                # 第二条已写入但尚未提交时模拟持久层故障，预期外层回滚两条审计和全部业务行。
                raise ApiError("audit_log.record_unavailable")
            return record

        def capture_unexpected_dispatch(*_args, **_kwargs) -> None:
            """提交前失败不允许触发任何外部审计投递。"""

            unexpected_dispatches.append("unexpected")

        monkeypatch.setattr(projects_service, "record_audit_log", fail_second_audit)
        monkeypatch.setattr(
            projects_service,
            "dispatch_audit_recorded",
            capture_unexpected_dispatch,
        )
        failed = await client.patch(
            f"/applications/{application_id}",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd058_application_move_rollback",
                "X-Trace-ID": "trc_prd058_application_move_rollback",
            },
            json={"project_id": target_id},
        )

    assert failed.status_code == 503
    assert failed.json()["code"] == "audit_log.record_unavailable"
    assert failed.json()["request_id"] == "req_prd058_application_move_rollback"
    assert set(attempted_object_ids) == set(virtual_key_ids)
    assert unexpected_dispatches == []

    connection = connect_database(database_url)
    application_project = connection.execute(
        "SELECT project_id FROM applications WHERE tenant_id=? AND application_id=?",
        (tenant_id, application_id),
    ).fetchone()
    key_projects = connection.execute(
        """
        SELECT virtual_key_id, project_id
        FROM virtual_keys
        WHERE tenant_id=? AND application_id=?
        ORDER BY virtual_key_id
        """,
        (tenant_id, application_id),
    ).fetchall()
    failed_audit_count = connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE tenant_id=? AND request_id=?",
        (tenant_id, "req_prd058_application_move_rollback"),
    ).fetchone()[0]
    connection.close()
    assert application_project is not None and application_project["project_id"] == source_id
    assert {row["virtual_key_id"] for row in key_projects} == set(virtual_key_ids)
    assert all(row["project_id"] == source_id for row in key_projects)
    assert failed_audit_count == 0


@pytest.mark.anyio
async def test_unused_project_tree_can_be_deleted_with_empty_child_application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """新建但从未使用的项目及其空应用可以一起删除。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin_token}"}
        project_response = await client.post(
            "/projects",
            headers={**headers, "X-Request-ID": "req_prd029_unused_project"},
            json={"name": "临时项目"},
        )
        assert project_response.status_code == 201, project_response.text
        project_id = project_response.json()["project"]["project_id"]
        application_response = await client.post(
            "/applications",
            headers={**headers, "X-Request-ID": "req_prd029_unused_application"},
            json={"project_id": project_id, "name": "临时应用"},
        )
        assert application_response.status_code == 201, application_response.text
        application_id = application_response.json()["application"]["application_id"]
        deleted = await client.delete(
            f"/projects/{project_id}",
            headers={**headers, "X-Request-ID": "req_prd029_unused_project_delete"},
        )
        assert deleted.status_code == 204, deleted.text

    connection = connect_database(database_url)
    project_row = connection.execute(
        "SELECT 1 FROM projects WHERE tenant_id=? AND project_id=?",
        (tenant_id, project_id),
    ).fetchone()
    application_row = connection.execute(
        "SELECT 1 FROM applications WHERE tenant_id=? AND application_id=?",
        (tenant_id, application_id),
    ).fetchone()
    event_rows = connection.execute(
        "SELECT COUNT(*) AS count FROM project_events WHERE tenant_id=? AND project_id=?",
        (tenant_id, project_id),
    ).fetchone()
    connection.close()
    assert project_row is None
    assert application_row is None
    assert event_rows["count"] == 0

    # 应用本身也支持独立删除；空项目随后仍可删除。
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin_token}"}
        second_project = await client.post(
            "/projects",
            headers=headers,
            json={"name": "第二个临时项目"},
        )
        second_project_id = second_project.json()["project"]["project_id"]
        second_application = await client.post(
            "/applications",
            headers=headers,
            json={"project_id": second_project_id, "name": "第二个临时应用"},
        )
        second_application_id = second_application.json()["application"]["application_id"]
        deleted_application = await client.delete(
            f"/applications/{second_application_id}",
            headers=headers,
        )
        assert deleted_application.status_code == 204, deleted_application.text
        deleted_second_project = await client.delete(
            f"/projects/{second_project_id}",
            headers=headers,
        )
        assert deleted_second_project.status_code == 204, deleted_second_project.text
