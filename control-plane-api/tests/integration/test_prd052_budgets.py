"""PRD-052 租户/项目/Key 三级预算 Control API 专项测试。

覆盖 create-only、正式账本金额、可信归属链、累计 AND 聚合、严格 wire、后端权限、
跨租户隐藏、未知 CNY 失败关闭和连接生命周期。测试不记录 Key 明文、Token 或请求正文。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.budgets import service as budget_service
from metahub_control.budgets.models import BudgetScopeInfo
from metahub_control.budgets.routes import router as budgets_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下固定值仅用于沙箱测试认证与上海业务日，不进入生产配置或业务日志。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
FINANCE_PASSWORD = "FinanceLocal!2026"
FIXED_NOW = datetime(2026, 7, 27, 4, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class BudgetFixture:
    """专项测试使用的租户内资源 ID；不保存任何凭证明文。"""

    database_url: str
    tenant_id: str
    environment_id: str
    other_environment_id: str
    virtual_key_id: str
    project_a_id: str
    application_a_id: str
    project_b_id: str
    other_tenant_project_id: str


class TrackingConnection:
    """代理预算服务连接并记录 close，验证成功和异常路径都释放资源。"""

    def __init__(self, connection) -> None:
        """保存底层 SQLite 连接；参数仅来自测试数据库。"""

        self._connection = connection
        self.closed = False

    def close(self) -> None:
        """关闭底层连接并冻结已关闭状态。"""

        self.closed = True
        self._connection.close()

    def __getattr__(self, name: str):
        """把 execute/commit/rollback/in_transaction 等操作透明转发到底层连接。"""

        return getattr(self._connection, name)


def _sqlite_url(path: Path) -> str:
    """把沙箱内测试数据库路径转换为项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只包含认证与预算路由的专项测试应用。"""

    app = create_service_app("control-plane-api", "PRD-052 Budget Control API")
    app.include_router(auth_router)
    app.include_router(budgets_router)
    return app


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BudgetFixture:
    """创建三级归属、权限用户和另一租户资源，全部应用至 0042。"""

    database_url = _sqlite_url(tmp_path / "prd052.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd052-auth-token-secret")
    monkeypatch.setattr(budget_service, "_utcnow", lambda: FIXED_NOW)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    tenant_id = seed.tenant_id
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant_id,),
    ).fetchone()
    key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY virtual_key_id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    assert environment is not None and key is not None
    environment_id = str(environment["environment_id"])
    virtual_key_id = str(key["virtual_key_id"])
    project_a_id = "prj_prd052_a"
    application_a_id = "app_prd052_a"
    project_b_id = "prj_prd052_b"
    other_environment_id = "env_prd052_test"
    other_tenant_id = "tenant_prd052_other"
    other_tenant_project_id = "prj_prd052_other"
    now = "2026-07-27T01:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        [
            (
                "user_prd052_developer",
                tenant_id,
                "developer@example.com",
                "PRD052 Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
                now,
                now,
            ),
            (
                "user_prd052_viewer",
                tenant_id,
                "viewer@example.com",
                "PRD052 Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
                now,
                now,
            ),
            (
                "user_prd052_finance",
                tenant_id,
                "finance@example.com",
                "PRD052 Finance Admin",
                hash_password(FINANCE_PASSWORD),
                "finance_admin",
                now,
                now,
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO projects(
          project_id, tenant_id, project_code, name, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, 'active', ?, ?)
        """,
        [
            (project_a_id, tenant_id, "prd052-a", "项目 A", now, now),
            (project_b_id, tenant_id, "prd052-b", "项目 B", now, now),
        ],
    )
    connection.execute(
        """
        INSERT INTO applications(
          application_id, tenant_id, project_id, application_code, name, status,
          created_at, updated_at
        ) VALUES(?, ?, ?, 'prd052-app-a', '应用 A', 'active', ?, ?)
        """,
        (application_a_id, tenant_id, project_a_id, now, now),
    )
    connection.execute(
        """
        UPDATE virtual_keys
        SET project_id=?, application_id=?
        WHERE tenant_id=? AND virtual_key_id=?
        """,
        (project_a_id, application_a_id, tenant_id, virtual_key_id),
    )
    connection.execute(
        """
        INSERT INTO environments(
          environment_id, tenant_id, code, name, status, created_at, updated_at
        ) VALUES(?, ?, 'test', '测试环境', 'active', ?, ?)
        """,
        (other_environment_id, tenant_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        ) VALUES(?, 'prd052-other', '其他租户', 'internal_only', 'active', ?, ?)
        """,
        (other_tenant_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO projects(
          project_id, tenant_id, project_code, name, status, created_at, updated_at
        ) VALUES(?, ?, 'prd052-other', '其他租户项目', 'active', ?, ?)
        """,
        (other_tenant_project_id, other_tenant_id, now, now),
    )
    connection.commit()
    connection.close()
    return BudgetFixture(
        database_url=database_url,
        tenant_id=tenant_id,
        environment_id=environment_id,
        other_environment_id=other_environment_id,
        virtual_key_id=virtual_key_id,
        project_a_id=project_a_id,
        application_a_id=application_a_id,
        project_b_id=project_b_id,
        other_tenant_project_id=other_tenant_project_id,
    )


def _insert_usage_charge(
    fixture: BudgetFixture,
    *,
    request_id: str,
    amount: str,
    currency: str = "CNY",
) -> None:
    """写入带三级快照的 Usage，由 0041 trigger 同事务生成正式 usage_charge。"""

    connection = connect_database(fixture.database_url)
    created_at = "2026-07-27T02:00:00Z"
    trace_id = f"trc_{request_id}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
        """,
        (
            fixture.tenant_id,
            request_id,
            trace_id,
            fixture.virtual_key_id,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, project_id, application_id,
          environment_id, environment_code, status, input_tokens, output_tokens, total_tokens,
          estimated_cost_amount, currency, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, 'dev', 'success', 1, 1, 2, ?, ?, ?, ?)
        """,
        (
            request_id,
            fixture.tenant_id,
            trace_id,
            fixture.virtual_key_id,
            fixture.project_a_id,
            fixture.application_a_id,
            fixture.environment_id,
            amount,
            currency,
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens, total_tokens,
          raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd, currency,
          virtual_key_id, project_id, application_id, environment_id, environment_code,
          created_at, updated_at
        ) VALUES(?, ?, ?, 1, 1, 2, '{}', '{}', ?, ?, ?, ?, ?, ?, ?, ?, 'dev', ?, ?)
        """,
        (
            f"usg_{request_id}",
            fixture.tenant_id,
            request_id,
            amount,
            amount if currency == "CNY" else "0",
            amount if currency == "USD" else "0",
            currency,
            fixture.virtual_key_id,
            fixture.project_a_id,
            fixture.application_a_id,
            fixture.environment_id,
            created_at,
            created_at,
        ),
    )
    connection.commit()
    connection.close()


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试用户并返回仅供 Authorization Header 使用的令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 令牌只返回给当前测试请求头，不能写入响应快照或持久测试证据。
    return str(response.json()["access_token"])


def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造带调用链 ID 的认证头，令牌不写入日志或断言快照。"""

    # 认证头只在内存中传给 ASGI 客户端，断言始终使用脱敏 request_id。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


def test_prd052_limiting_rule_uses_smallest_capacity_despite_sticky_block() -> None:
    """聚合容量：余额更小的 Project 是 limiting，旧 tenant block 仍保持总体阻断。"""

    tenant_scope = BudgetScopeInfo(
        budget_scope_id="bsc_tenant",
        tenant_id="tenant_1",
        environment_id="env_1",
        scope_type="tenant",
        scope_id="tenant_1",
        scope_label="租户",
        priority=0,
    )
    project_scope = BudgetScopeInfo(
        budget_scope_id="bsc_project",
        tenant_id="tenant_1",
        environment_id="env_1",
        scope_type="project",
        scope_id="prj_1",
        scope_label="项目",
        priority=1,
        parent_scope_type="tenant",
        parent_scope_id="tenant_1",
    )
    blocked_tenant = SimpleNamespace(
        status="exhausted",
        next_action="block",
        block_status="active",
        effective_remaining_cny="100",
        budget_scope=tenant_scope,
        budget_rule_id="br_tenant",
    )
    tighter_project = SimpleNamespace(
        status="ok",
        next_action="allow",
        block_status=None,
        effective_remaining_cny="1",
        budget_scope=project_scope,
        budget_rule_id="br_project",
    )

    aggregate = budget_service._budget_aggregate(
        [blocked_tenant, tighter_project],
        [tenant_scope, project_scope],
    )

    assert aggregate.aggregate_status == "exhausted"
    assert aggregate.effective_action == "block"
    assert aggregate.effective_remaining_cny == "1"
    assert aggregate.limiting_rule_id == "br_project"
    assert aggregate.limiting_scope == project_scope


@pytest.mark.anyio
async def test_prd052_create_only_three_level_status_and_aggregate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：POST 创建三级预算，正式金额、父链、摘要和 limiting rule 一致。"""

    fixture = _prepare_database(monkeypatch, tmp_path)
    _insert_usage_charge(fixture, request_id="req_prd052_usage_cny", amount="30")
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        # PRD-059 将全部 billing 权限从 Developer 上限移交给 FinanceAdmin；
        # 仍使用非 admin 角色验证只读路径，避免管理员全权限掩盖预算读权限守卫。
        budget_reader = await _login(client, "finance@example.com", FINANCE_PASSWORD)
        tenant_rule = await client.post(
            "/budgets",
            headers=_headers(admin, "req_prd052_create_tenant"),
            json={
                "scope_type": "tenant",
                "daily_budget_cny": "100",
                "exhausted_action": "block",
            },
        )
        project_rule = await client.post(
            "/budgets",
            headers=_headers(admin, "req_prd052_create_project"),
            json={
                "scope_type": "project",
                "scope_id": fixture.project_a_id,
                "daily_budget_cny": "40",
                "exhausted_action": "block",
            },
        )
        key_rule = await client.post(
            "/budgets",
            headers=_headers(admin, "req_prd052_create_key"),
            json={
                "scope_type": "virtual_key",
                "scope_id": fixture.virtual_key_id,
                "daily_budget_cny": "80",
                "exhausted_action": "block",
            },
        )
        duplicate = await client.post(
            "/budgets",
            headers=_headers(admin, "req_prd052_duplicate_project"),
            json={
                "scope_type": "project",
                "scope_id": fixture.project_a_id,
                "daily_budget_cny": "999",
            },
        )
        listed = await client.get(
            "/budgets",
            headers=_headers(budget_reader, "req_prd052_list"),
        )
        status = await client.get(
            f"/budget-status?virtual_key_id={fixture.virtual_key_id}",
            headers=_headers(budget_reader, "req_prd052_status"),
        )
        patched = await client.patch(
            "/budgets",
            headers=_headers(admin, "req_prd052_patch_project"),
            json={
                "scope_type": "project",
                "scope_id": fixture.project_a_id,
                "daily_budget_cny": "60",
            },
        )

    for response in (tenant_rule, project_rule, key_rule):
        assert response.status_code == 201, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        rule = response.json()["budget_rule"]
        assert rule["today_status"]["ledger_spent_cny"] == "30"
        assert rule["today_status"]["reserved_cny"] == "0"
        assert rule["today_status"]["effective_spend_cny"] == "30"
        assert rule["budget_scope"]["budget_scope_id"].startswith("bsc_")
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "budget.rule_conflict"
    assert duplicate.headers["X-Request-ID"] == duplicate.json()["request_id"]
    assert listed.status_code == 200, listed.text
    listed_rules = listed.json()["budget_rules"]
    assert len(listed_rules) == 3
    assert next(
        rule for rule in listed_rules if rule["scope_type"] == "project"
    )["daily_budget_cny"] == "40"
    summary = listed.json()["environment_summaries"][0]
    summary_by_scope = {item["scope_type"]: item for item in summary["scope_summaries"]}
    assert summary_by_scope["tenant"]["rule_count"] == 1
    assert summary_by_scope["project"]["rule_count"] == 1
    assert summary_by_scope["virtual_key"]["rule_count"] == 1
    assert summary_by_scope["application"]["rule_count"] == 0

    assert status.status_code == 200, status.text
    payload = status.json()
    assert [item["scope_type"] for item in payload["aggregate"]["scope_chain"]] == [
        "tenant",
        "project",
        "application",
        "virtual_key",
    ]
    assert {item["scope_type"] for item in payload["statuses"]} == {
        "tenant",
        "project",
        "virtual_key",
    }
    assert payload["aggregate"]["aggregate_status"] == "ok"
    assert payload["aggregate"]["effective_action"] == "allow"
    assert payload["aggregate"]["limiting_rule_id"] == project_rule.json()["budget_rule"]["budget_rule_id"]
    assert payload["aggregate"]["limiting_scope"]["scope_type"] == "project"
    key_scope = next(
        item["budget_scope"]
        for item in payload["statuses"]
        if item["scope_type"] == "virtual_key"
    )
    assert key_scope["parent_scope_type"] == "application"
    assert key_scope["parent_scope_id"] == fixture.application_a_id
    assert patched.status_code == 200
    assert patched.json()["budget_rule"]["daily_budget_cny"] == "60"


@pytest.mark.anyio
async def test_prd052_strict_wire_rbac_and_scope_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：RBAC 先行，严格 wire、跨租户与归属冲突返回稳定错误码。"""

    fixture = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "viewer@example.com", VIEWER_PASSWORD)
        malformed = b'{"scope_type":"tenant","scope_type":"project","daily_budget_cny":"10"}'
        developer_malformed = await client.post(
            "/budgets",
            headers={
                **_headers(developer, "req_prd052_dev_malformed"),
                "Content-Type": "application/json",
            },
            content=malformed,
        )
        viewer_unknown = await client.get(
            "/budgets?tenant_id=other",
            headers=_headers(viewer, "req_prd052_viewer_unknown"),
        )
        viewer_post = await client.post(
            "/budgets",
            headers=_headers(viewer, "req_prd052_viewer_post"),
            json={"scope_type": "tenant", "daily_budget_cny": "10"},
        )
        viewer_patch = await client.patch(
            "/budgets",
            headers=_headers(viewer, "req_prd052_viewer_patch"),
            json={"scope_type": "tenant", "daily_budget_cny": "10"},
        )
        developer_patch = await client.patch(
            "/budgets",
            headers=_headers(developer, "req_prd052_developer_patch"),
            json={"scope_type": "tenant", "daily_budget_cny": "10"},
        )
        unknown_query = await client.get(
            "/budgets?unknown=value",
            headers=_headers(admin, "req_prd052_unknown_query"),
        )
        duplicate_query = await client.get(
            "/budgets?status=active&status=disabled",
            headers=_headers(admin, "req_prd052_duplicate_query"),
        )
        tenant_query = await client.get(
            "/budget-status?tenant_id=other",
            headers=_headers(admin, "req_prd052_tenant_query"),
        )
        duplicate_json = await client.post(
            "/budgets",
            headers={
                **_headers(admin, "req_prd052_duplicate_json"),
                "Content-Type": "application/json",
            },
            content=malformed,
        )
        tenant_body = await client.post(
            "/budgets",
            headers=_headers(admin, "req_prd052_tenant_body"),
            json={
                "scope_type": "tenant",
                "daily_budget_cny": "10",
                "tenant_id": "other",
            },
        )
        non_json = await client.post(
            "/budgets",
            headers={
                **_headers(admin, "req_prd052_non_json"),
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
        cross_tenant = await client.post(
            "/budgets",
            headers=_headers(admin, "req_prd052_cross_tenant"),
            json={
                "scope_type": "project",
                "scope_id": fixture.other_tenant_project_id,
                "daily_budget_cny": "10",
            },
        )
        project_mismatch = await client.get(
            (
                f"/budget-status?virtual_key_id={fixture.virtual_key_id}"
                f"&project_id={fixture.project_b_id}"
            ),
            headers=_headers(admin, "req_prd052_project_mismatch"),
        )
        environment_mismatch = await client.get(
            (
                f"/budget-status?virtual_key_id={fixture.virtual_key_id}"
                f"&environment_id={fixture.other_environment_id}"
            ),
            headers=_headers(admin, "req_prd052_environment_mismatch"),
        )

    assert developer_malformed.status_code == 403
    assert developer_malformed.json()["code"] == "auth.permission_denied"
    assert viewer_unknown.status_code == 403
    assert viewer_unknown.json()["code"] == "auth.permission_denied"
    for forbidden_write in (viewer_post, viewer_patch, developer_patch):
        assert forbidden_write.status_code == 403
        assert forbidden_write.json()["code"] == "auth.permission_denied"
        assert forbidden_write.headers["X-Request-ID"] == forbidden_write.json()["request_id"]
    for response in (
        unknown_query,
        duplicate_query,
        tenant_query,
        duplicate_json,
        tenant_body,
        non_json,
    ):
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "budget.rule_invalid"
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["code"] == "budget.scope_not_found"
    assert project_mismatch.status_code == 409
    assert project_mismatch.json()["code"] == "budget.scope_mismatch"
    assert environment_mismatch.status_code == 409
    assert environment_mismatch.json()["code"] == "budget.scope_mismatch"


@pytest.mark.anyio
async def test_prd052_unknown_spend_and_connections_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常流程：未知 CNY 正式金额返回 503，所有服务连接仍在 finally 中关闭。"""

    fixture = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/budgets",
            headers=_headers(admin, "req_prd052_unknown_create"),
            json={
                "scope_type": "tenant",
                "daily_budget_cny": "100",
                "exhausted_action": "block",
            },
        )
        assert created.status_code == 201, created.text
        _insert_usage_charge(
            fixture,
            request_id="req_prd052_usage_usd",
            amount="1",
            currency="USD",
        )
        original_connect = budget_service._connect
        tracked: list[TrackingConnection] = []

        def tracking_connect(config):
            """为预算服务每次调用包装新连接，并保存 close 证据。"""

            connection = TrackingConnection(original_connect(config))
            tracked.append(connection)
            return connection

        monkeypatch.setattr(budget_service, "_connect", tracking_connect)
        unavailable = await client.get(
            "/budget-status",
            headers=_headers(admin, "req_prd052_unknown_status"),
        )
        missing = await client.get(
            "/budget-status?virtual_key_id=vk_other_tenant_or_missing",
            headers=_headers(admin, "req_prd052_missing_scope"),
        )
        listed = await client.get(
            "/budgets?status=disabled",
            headers=_headers(admin, "req_prd052_empty_list"),
        )

    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "budget.status_unavailable"
    assert unavailable.headers["X-Request-ID"] == unavailable.json()["request_id"]
    assert missing.status_code == 404
    assert missing.json()["code"] == "budget.scope_not_found"
    assert listed.status_code == 200
    assert listed.json()["budget_rules"] == []
    assert len(tracked) == 3
    assert all(connection.closed for connection in tracked)
