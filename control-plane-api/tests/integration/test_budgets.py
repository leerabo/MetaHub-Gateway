"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_budgets.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.budgets import service as budget_service
from metahub_control.budgets.routes import router as budgets_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.budget_blocks import ensure_budget_block
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试常量是固定租户夹具和时间锚点，只用于隔离数据库中的权限、预算和审计断言。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
FINANCE_PASSWORD = "FinanceLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
FIXED_NOW = datetime(2026, 7, 9, 4, 0, 0, tzinfo=UTC)


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证和预算 API 的控制面测试应用。"""

    app = create_service_app("control-plane-api", "Budget Test Control API")
    app.include_router(auth_router)
    app.include_router(budgets_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str, str]:
    """准备预算测试所需的租户、用户、Virtual Key 和当日用量数据。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    monkeypatch.setattr(budget_service, "_utcnow", lambda: FIXED_NOW)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    tenant_id = seed.tenant_id
    virtual_key_id = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()["virtual_key_id"]
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd023_developer",
                tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd023_viewer",
                tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
            (
                "user_prd023_finance",
                tenant_id,
                "finance@example.com",
                "Finance User",
                hash_password(FINANCE_PASSWORD),
                "finance_admin",
            ),
        ],
    )
    _insert_usage(connection, tenant_id, virtual_key_id, "req_prd023_tenant_spend", "2026-07-09T03:00:00Z", 85)
    _insert_usage(connection, tenant_id, virtual_key_id, "req_prd023_key_spend", "2026-07-09T03:30:00Z", 25)
    connection.commit()
    # 返回租户和 Key 标识给用例，后续断言必须证明预算接口没有信任客户端 tenant_id。
    return database_url, tenant_id, virtual_key_id


def _insert_usage(
    connection,
    tenant_id: str,
    virtual_key_id: str,
    request_id: str,
    created_at: str,
    cost_cny: float,
) -> None:
    """写入带权威 Environment 快照的 Usage，并由 0041 trigger 生成正式分录。"""

    key = connection.execute(
        """
        SELECT environment_id
        FROM virtual_keys
        WHERE tenant_id=? AND virtual_key_id=?
        """,
        (tenant_id, virtual_key_id),
    ).fetchone()
    assert key is not None and key["environment_id"]
    # 历史预算夹具不消费 Policy，但 RequestLog 与 Usage 仍须共享 tenant/trace/Key 事实。
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
        """,
        (tenant_id, request_id, f"trc_{request_id}", virtual_key_id, created_at),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, status, input_tokens, output_tokens,
          total_tokens, estimated_cost_amount, currency, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, 'success', 1, 1, 2, ?, 'CNY', ?, ?)
        """,
        (
            request_id,
            tenant_id,
            f"trc_{request_id}",
            virtual_key_id,
            cost_cny,
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens, total_tokens,
          raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd, currency,
          virtual_key_id, environment_id, created_at, updated_at
        )
        VALUES(?, ?, ?, 1, 1, 2, '{}', '{}', ?, ?, 0, 'CNY', ?, ?, ?, ?)
        """,
        (
            f"usg_{request_id}",
            tenant_id,
            request_id,
            cost_cny,
            cost_cny,
            virtual_key_id,
            key["environment_id"],
            created_at,
            created_at,
        ),
    )


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试用户并返回访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 认证令牌只在请求头中使用，断言时不写入任何业务日志或响应快照。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_budget_rule_upsert_status_and_reader_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：管理员配置预算，财务角色读取，开发者读写均被后端拒绝。"""

    _database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        finance_token = await login(client, "finance@example.com", FINANCE_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        tenant_rule = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd023_tenant_budget"},
            json={
                "scope_type": "tenant",
                "name": "默认租户日预算",
                "daily_budget_cny": "100",
                "warning_threshold_percent": 80,
                "exhausted_action": "alert_only",
            },
        )
        key_rule = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd023_key_budget"},
            json={
                "scope_type": "virtual_key",
                "scope_id": virtual_key_id,
                "daily_budget_cny": "20",
                "warning_threshold_percent": 80,
                "exhausted_action": "block",
            },
        )
        listed = await client.get(
            "/budgets",
            headers={"Authorization": f"Bearer {finance_token}", "X-Request-ID": "req_prd023_list"},
        )
        status = await client.get(
            f"/budget-status?virtual_key_id={virtual_key_id}",
            headers={"Authorization": f"Bearer {finance_token}", "X-Request-ID": "req_prd023_status"},
        )
        # PRD-059 将所有 billing.* 从 Developer 上限移除；旧 Token 不能沿用
        # M0 的角色名兼容规则绕过数据库权限矩阵。
        developer_read_denied = await client.get(
            "/budgets",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        developer_write_denied = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={"scope_type": "tenant", "daily_budget_cny": "200"},
        )

    assert tenant_rule.status_code == 200, tenant_rule.text
    tenant_payload = tenant_rule.json()["budget_rule"]
    assert tenant_payload["scope_type"] == "tenant"
    assert tenant_payload["scope_id"] == tenant_id
    assert tenant_payload["today_status"]["spent_cny"] == "110"
    assert tenant_payload["today_status"]["status"] == "exhausted"
    assert tenant_payload["today_status"]["next_action"] == "allow"
    assert key_rule.status_code == 200, key_rule.text
    key_payload = key_rule.json()["budget_rule"]
    assert key_payload["scope_type"] == "virtual_key"
    assert key_payload["scope_id"] == virtual_key_id
    assert key_payload["today_status"]["status"] == "exhausted"
    assert key_payload["today_status"]["next_action"] == "block"
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["budget_rules"]) == 2
    assert status.status_code == 200, status.text
    assert {item["scope_type"] for item in status.json()["statuses"]} == {"tenant", "virtual_key"}
    assert developer_read_denied.status_code == 403
    assert developer_read_denied.json()["code"] == "auth.permission_denied"
    assert developer_write_denied.status_code == 403
    assert developer_write_denied.json()["code"] == "auth.permission_denied"


@pytest.mark.anyio
async def test_budget_calendar_periods_and_employee_search(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """自然月、自然年累积前几日成本；员工目录按服务端姓名搜索。"""

    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    _insert_usage(connection, tenant_id, virtual_key_id, "req_prior_day", "2026-07-08T03:00:00Z", 7)
    _insert_usage(connection, tenant_id, virtual_key_id, "req_prior_month", "2026-06-20T03:00:00Z", 11)
    connection.commit()
    connection.close()
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin_token}"}
        async def set_period(period: str) -> dict:
            response = await client.patch("/budgets", headers=headers, json={
                "scope_type": "tenant", "daily_budget_cny": "200", "budget_period": period,
                "exhausted_action": "alert_only",
            })
            assert response.status_code == 200, response.text
            return response.json()["budget_rule"]

        daily = await set_period("day")
        monthly = await set_period("month")
        yearly = await set_period("year")
        listed = await client.get("/budgets", headers=headers)
        searched = await client.get("/budget-people?search=Finance&offset=0", headers=headers)
        all_people = await client.get("/budget-people?offset=0", headers=headers)

    assert daily["today_status"]["spent_cny"] == "110"
    assert monthly["today_status"]["spent_cny"] == "117"
    assert yearly["today_status"]["spent_cny"] == "128"
    assert yearly["budget_period"] == "year"
    assert listed.status_code == 200, listed.text
    assert {rule["budget_period"] for rule in listed.json()["budget_rules"]} == {"day", "month", "year"}
    assert len({rule["budget_rule_id"] for rule in listed.json()["budget_rules"]}) == 3
    assert searched.status_code == 200, searched.text
    assert [person["email"] for person in searched.json()["people"]] == ["finance@example.com"]
    assert all_people.status_code == 200, all_people.text
    assert all_people.json()["total"] >= 4


@pytest.mark.anyio
async def test_budget_future_activation_waits_then_counts_from_activation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A scheduled cap is visible to admins but cannot constrain calls before its start."""

    prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    activation_at = budget_service._utcnow() + timedelta(days=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        scheduled = await client.post("/budgets", headers=headers, json={
            "scope_type": "tenant", "budget_period": "month", "daily_budget_cny": "200",
            "activation_at_utc": activation_at.isoformat().replace("+00:00", "Z"), "exhausted_action": "block",
        })
        assert scheduled.status_code == 201, scheduled.text
        assert scheduled.json()["budget_rule"]["today_status"]["status"] == "scheduled"
        before = await client.get("/budget-status", headers=headers)
        assert before.status_code == 200, before.text
        assert before.json()["statuses"] == []
        monkeypatch.setattr(budget_service, "_utcnow", lambda: activation_at + timedelta(hours=1))
        after = await client.get("/budget-status", headers=headers)
        assert after.status_code == 200, after.text
        assert len(after.json()["statuses"]) == 1
        assert after.json()["statuses"][0]["spent_cny"] == "0"
        assert after.json()["statuses"][0]["status"] == "ok"


@pytest.mark.anyio
async def test_budget_permissions_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：viewer 不能读取，非法 scope、空 Patch 和错误阈值返回统一预算错误码。"""

    prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        viewer_denied = await client.get("/budgets", headers={"Authorization": f"Bearer {viewer_token}"})
        empty_patch = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"scope_type": "tenant"},
        )
        bad_scope = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"scope_type": "virtual_key", "scope_id": "vk_missing", "daily_budget_cny": "10"},
        )
        bad_threshold = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"scope_type": "tenant", "daily_budget_cny": "10", "warning_threshold_percent": 100},
        )
        bad_status_query = await client.get(
            "/budgets?status=archived",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert empty_patch.status_code == 422
    assert empty_patch.json()["code"] == "budget.update_empty"
    assert bad_scope.status_code == 404
    assert bad_scope.json()["code"] == "budget.scope_not_found"
    assert bad_threshold.status_code == 422
    assert bad_threshold.json()["code"] == "budget.rule_invalid"
    assert bad_status_query.status_code == 422
    assert bad_status_query.json()["code"] == "budget.rule_invalid"


@pytest.mark.anyio
async def test_budget_mode_and_raise_release_active_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-033 主流程：切换仅告警或提高额度会立即释放 active block，越权和跨租户请求被拒绝。"""

    database_url, tenant_id, _virtual_key_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        created = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope_type": "tenant",
                "daily_budget_cny": "100",
                "warning_threshold_percent": 80,
                "exhausted_action": "block",
            },
        )
        assert created.status_code == 200, created.text
        rule = created.json()["budget_rule"]
        rule_id = rule["budget_rule_id"]
        connection = connect_database(database_url)
        first_block, first_created = ensure_budget_block(
            connection,
            tenant_id=tenant_id,
            environment_id=rule["environment_id"],
            budget_rule_id=rule_id,
            scope_type="tenant",
            scope_id=tenant_id,
            budget_date="2026-07-09",
            blocked_cost_cny="111",
            budget_cny="100",
            request_id="req_prd033_first_block",
            trace_id="trc_prd033_first_block",
            now="2026-07-09T04:00:00Z",
        )
        connection.commit()
        assert first_created is True
        connection.execute(
            """
            INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
            VALUES('tenant_other_prd033', 'other-prd033', 'Other Budget Tenant', 'internal_only', 'active',
                   '2026-07-09T04:00:00Z', '2026-07-09T04:00:00Z')
            """
        )
        other_environment = connection.execute(
            "SELECT environment_id FROM environments WHERE tenant_id='tenant_other_prd033' AND code='dev'"
        ).fetchone()
        assert other_environment is not None
        connection.execute(
            """
            INSERT INTO budget_rules(
              budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name, daily_budget_cny,
              warning_threshold_percent, exhausted_threshold_percent, exhausted_action, status, created_at, updated_at
            )
            VALUES('br_other_tenant', 'tenant_other_prd033', ?, 'tenant', 'tenant_other_prd033',
                   'Other Tenant Budget', 100, 80, 100, 'block', 'active',
                   '2026-07-09T04:00:00Z', '2026-07-09T04:00:00Z')
            """,
            (other_environment["environment_id"],),
        )
        connection.commit()

        listed = await client.get("/budgets", headers={"Authorization": f"Bearer {admin_token}"})
        developer_denied = await client.patch(
            f"/budgets/{rule_id}/mode",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={"mode": "alert_only"},
        )
        invalid_mode = await client.patch(
            f"/budgets/{rule_id}/mode",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"mode": "approve_then_block"},
        )
        switched = await client.patch(
            f"/budgets/{rule_id}/mode",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd033_mode"},
            json={"mode": "alert_only"},
        )
        missing = await client.patch(
            "/budgets/br_other_tenant/mode",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"mode": "alert_only"},
        )

        # 重新启用 block 并制造第二次生命周期，随后通过现有规则接口提额完成恢复。
        await client.patch(
            f"/budgets/{rule_id}/mode",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"mode": "block"},
        )
        second_block, second_created = ensure_budget_block(
            connection,
            tenant_id=tenant_id,
            environment_id=rule["environment_id"],
            budget_rule_id=rule_id,
            scope_type="tenant",
            scope_id=tenant_id,
            budget_date="2026-07-09",
            blocked_cost_cny="112",
            budget_cny="100",
            request_id="req_prd033_second_block",
            trace_id="trc_prd033_second_block",
            now="2026-07-09T04:01:00Z",
        )
        connection.commit()
        assert second_created is True
        same_amount = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd033_same_amount"},
            json={"scope_type": "tenant", "daily_budget_cny": "100"},
        )
        raised = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd033_raise"},
            json={"scope_type": "tenant", "daily_budget_cny": "200"},
        )
        await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"scope_type": "tenant", "daily_budget_cny": "100"},
        )
        third_block, third_created = ensure_budget_block(
            connection,
            tenant_id=tenant_id,
            environment_id=rule["environment_id"],
            budget_rule_id=rule_id,
            scope_type="tenant",
            scope_id=tenant_id,
            budget_date="2026-07-09",
            blocked_cost_cny="113",
            budget_cny="100",
            request_id="req_prd033_third_block",
            trace_id="trc_prd033_third_block",
            now="2026-07-09T04:02:00Z",
        )
        connection.commit()
        assert third_created is True
        disabled = await client.patch(
            "/budgets",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd033_disable"},
            json={"scope_type": "tenant", "status": "disabled"},
        )

    assert listed.json()["budget_rules"][0]["active_block"]["budget_block_id"] == first_block.budget_block_id
    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert invalid_mode.status_code == 422
    assert invalid_mode.json()["code"] == "budget.mode_invalid"
    assert switched.status_code == 200
    assert switched.json()["budget_rule"]["exhausted_action"] == "alert_only"
    assert switched.json()["budget_rule"]["active_block"] is None
    assert missing.status_code == 404
    assert missing.json()["code"] == "budget.rule_not_found"
    assert same_amount.status_code == 200
    assert same_amount.json()["budget_rule"]["active_block"]["budget_block_id"] == second_block.budget_block_id
    assert raised.status_code == 200
    assert raised.json()["budget_rule"]["daily_budget_cny"] == "200"
    assert raised.json()["budget_rule"]["active_block"] is None
    assert disabled.status_code == 200
    assert disabled.json()["budget_rule"]["status"] == "disabled"
    assert disabled.json()["budget_rule"]["active_block"] is None

    connection = connect_database(database_url)
    released = connection.execute(
        "SELECT budget_block_id, release_reason, status FROM budget_blocks WHERE tenant_id=? ORDER BY blocked_at",
        (tenant_id,),
    ).fetchall()
    block_events = [
        (row["budget_block_id"], row["event_type"])
        for row in connection.execute(
            "SELECT budget_block_id, event_type FROM budget_block_events WHERE tenant_id=? ORDER BY budget_block_id, event_type",
            (tenant_id,),
        ).fetchall()
    ]
    assert [(row["budget_block_id"], row["release_reason"], row["status"]) for row in released] == [
        (first_block.budget_block_id, "mode_alert_only", "released"),
        (second_block.budget_block_id, "budget_raised", "released"),
        (third_block.budget_block_id, "rule_disabled", "released"),
    ]
    assert sorted(block_events) == sorted(
        [
            (first_block.budget_block_id, "budget.blocked"),
            (first_block.budget_block_id, "budget.unblocked"),
            (second_block.budget_block_id, "budget.blocked"),
            (second_block.budget_block_id, "budget.unblocked"),
            (third_block.budget_block_id, "budget.blocked"),
            (third_block.budget_block_id, "budget.unblocked"),
        ]
    )
