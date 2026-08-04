"""PRD-046 Dry-Run API 的事务、隐私、RBAC、租户与零成本集成测试。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.policies import dry_run_service
from metahub_control.policies.routes import (
    _reset_dry_run_limiter_for_tests,
)
from metahub_control.policies.routes import (
    router as policy_router,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.budget_spend import (
    BudgetSpendAmounts,
    BudgetSpendUnavailableError,
)
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试身份、口令和 Prompt sentinel 只服务本地隔离验证，禁止进入生产配置或业务事实。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
PROMPT_SENTINEL = "PRD046_PROMPT_MUST_EXIST_ONLY_IN_MEMORY"


class _ConnectionProbe:
    """记录每条真实连接执行的 SQL，不改变事务、row_factory 或数据库行为。"""

    def __init__(self, connection: sqlite3.Connection, index: int, timeline: list[tuple[int, str]]):
        """保存连接序号和共享时间线，其余属性透明委托。"""

        self._connection = connection
        self._index = index
        self._timeline = timeline

    def execute(self, sql: str, parameters=()):
        """记录规范化 SQL 时间线后交给真实连接原样执行。"""

        self._timeline.append((self._index, " ".join(sql.split()).upper()))
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name: str):
        """把事务和行工厂等未包装能力透明委托给真实连接。"""

        return getattr(self._connection, name)


def _sqlite_url(path: Path) -> str:
    """构造项目数据库连接支持的绝对 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只含认证与 Policy router 的隔离应用，并清理进程内 limiter 测试状态。"""

    _reset_dry_run_limiter_for_tests()
    app = create_service_app("control-plane-api", "Policy Dry-Run Test API")
    app.include_router(auth_router)
    app.include_router(policy_router)
    return app


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """准备两个租户、三角色、active Environment/Project 与 direct Chat 模型。"""

    database_url = _sqlite_url(tmp_path / "dry-run.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd046-test-auth-secret")
    monkeypatch.setenv("METAHUB_POLICY_EVALUATION_TIMEOUT_MS", "20")
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
                "user_prd046_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd046_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    environment = connection.execute(
        """
        SELECT environment_id FROM environments
        WHERE tenant_id=? AND code='dev' AND status='active'
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert environment is not None
    environment_id = str(environment["environment_id"])
    project_id = "prj_prd046_default"
    connection.execute(
        """
        INSERT INTO projects(
          project_id, tenant_id, project_code, name, status, created_at, updated_at
        )
        VALUES(?, ?, 'prd046', 'PRD046 Project', 'active', datetime('now'), datetime('now'))
        """,
        (project_id, seed.tenant_id),
    )
    other_tenant_id = "tenant_prd046_other"
    other_project_id = "prj_prd046_other"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        )
        VALUES(?, 'prd046-other', 'Other', 'internal_only', 'active',
               datetime('now'), datetime('now'))
        """,
        (other_tenant_id,),
    )
    other_environment = connection.execute(
        """
        SELECT environment_id FROM environments
        WHERE tenant_id=? AND code='dev' AND status='active'
        """,
        (other_tenant_id,),
    ).fetchone()
    assert other_environment is not None
    other_environment_id = str(other_environment["environment_id"])
    connection.execute(
        """
        INSERT INTO projects(
          project_id, tenant_id, project_code, name, status, created_at, updated_at
        )
        VALUES(?, ?, 'other', 'Other Project', 'active', datetime('now'), datetime('now'))
        """,
        (other_project_id, other_tenant_id),
    )
    model = connection.execute(
        """
        SELECT model_id, provider_id FROM models
        WHERE tenant_id=? AND model_alias='default-chat' AND status='active'
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert model is not None
    connection.commit()
    connection.close()
    return {
        "database_url": database_url,
        "tenant_id": seed.tenant_id,
        "environment_id": environment_id,
        "project_id": project_id,
        "other_environment_id": other_environment_id,
        "other_project_id": other_project_id,
        "model_id": str(model["model_id"]),
        "provider_id": str(model["provider_id"]),
    }


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录默认租户角色并返回只用于 Authorization 头的短期 Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 登录响应中的短期令牌只返回给请求头构造器，不进入日志或持久化断言。
    return response.json()["access_token"]


def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造不把 Token 写入数据库断言的请求头。"""

    # Authorization 仅用于当前隔离请求，request_id 则作为持久化幂等键。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


def _payload(state: dict[str, str], *, prompt: str = PROMPT_SENTINEL) -> dict[str, object]:
    """构造严格 DTO 允许的最小完整请求。"""

    return {
        "prompt": prompt,
        "model": "default-chat",
        "project_id": state["project_id"],
        "environment_id": state["environment_id"],
        "data_class": "confidential",
        "max_output_tokens": 32,
    }


def _table_counts(connection: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    """读取零成本相关表行数；表名只来自测试常量，不接受外部输入。"""

    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


@pytest.mark.anyio
async def test_dry_run_admin_query_only_snapshot_and_zero_cost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成功预演使用同一 query-only 配置快照，只新增三类安全事实且不保存 Prompt。"""

    state = _prepare_database(monkeypatch, tmp_path)
    setup_connection = connect_database(state["database_url"])
    setup_connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        )
        VALUES('budget_prd046_zero_cost', ?, ?, 'tenant', ?, 'High',
               1000, 80, 100, 'block', 'active', datetime('now'), datetime('now'))
        """,
        (state["tenant_id"], state["environment_id"], state["tenant_id"]),
    )
    setup_connection.commit()
    setup_connection.close()
    zero_cost_tables = (
        "request_logs",
        "usage_ledger",
        "price_snapshots",
        "routing_decisions",
        "routing_attempts",
        "dlp_matches",
        "budget_evaluations",
        "budget_reservations",
        "budget_blocks",
    )
    before_connection = connect_database(state["database_url"])
    before = _table_counts(before_connection, zero_cost_tables)
    before_connection.close()
    timeline: list[tuple[int, str]] = []
    connection_index = 0

    def tracked_connect(database_url: str):
        """为服务的恢复读、配置读与写连接分配稳定序号。"""

        nonlocal connection_index
        connection_index += 1
        return _ConnectionProbe(connect_database(database_url), connection_index, timeline)

    monkeypatch.setattr(dry_run_service, "connect_database", tracked_connect)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, "req_prd046_zero_cost"),
        )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    explanation = body["policy_explanation"]
    assert explanation["status"] == "completed"
    assert explanation["outcome"] == "would_allow"
    assert explanation["provider_called"] is False
    assert explanation["usage_recorded"] is False
    assert explanation["cost_recorded"] is False
    assert explanation["route"]["status"] == "selected"
    assert explanation["route"]["provider_credential_status"] == "not_evaluated"
    stage_nodes = [
        node
        for node in explanation["decision_tree"]["nodes"]
        if node["node_type"] in {"route", "dlp", "budget"}
    ]
    assert [node["node_type"] for node in stage_nodes] == ["route", "dlp", "budget"]
    assert all(node["parent_id"] == "node_000" for node in stage_nodes)
    assert explanation["coverage_complete"] is False
    assert PROMPT_SENTINEL not in response.text

    by_connection: dict[int, list[str]] = {}
    for index, sql in timeline:
        by_connection.setdefault(index, []).append(sql)
    snapshot_connections = [
        statements
        for statements in by_connection.values()
        if "PRAGMA QUERY_ONLY=ON" in statements
        and "BEGIN" in statements
        and any("FROM POLICIES" in sql for sql in statements)
    ]
    assert len(snapshot_connections) == 1
    snapshot_sql = " ".join(snapshot_connections[0])
    for table in (
        "ENVIRONMENTS",
        "PROJECTS",
        "POLICIES",
        "MODELS",
        "PROVIDERS",
        "DLP_RULES",
        "BUDGET_RULES",
        "FINOPS_LEDGER_ENTRIES",
        "BUDGET_RESERVATIONS",
        "BUDGET_BLOCKS",
        "MODEL_PRICE_PLANS",
        "POLICY_SNAPSHOT_HEADS",
    ):
        assert table in snapshot_sql
    assert not any(
        sql.startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE "))
        for sql in snapshot_connections[0]
    )
    assert any("BEGIN IMMEDIATE" in statements for statements in by_connection.values())

    connection = connect_database(state["database_url"])
    assert _table_counts(connection, zero_cost_tables) == before
    assert connection.execute("SELECT COUNT(*) FROM policy_explanations").fetchone()[0] == 1
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM policy_events WHERE event_type='policy.dry_run_executed'"
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='policy.dry_run_executed'"
        ).fetchone()[0]
        == 1
    )
    dump = "\n".join(connection.iterdump())
    connection.close()
    assert PROMPT_SENTINEL not in dump


@pytest.mark.anyio
async def test_dry_run_rbac_auth_before_body_and_tenant_context_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """developer/viewer/匿名不能读正文执行；跨租户 Environment/Project 使用统一 404。"""

    state = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        for token, suffix in ((developer, "developer"), (viewer, "viewer")):
            response = await client.post(
                "/policies/dry-run",
                json=_payload(state),
                headers=_headers(token, f"req_prd046_{suffix}"),
            )
            assert response.status_code == 403
            assert response.json()["code"] == "auth.permission_denied"
        anonymous = await client.post(
            "/policies/dry-run",
            content=b"x" * (300 * 1024),
            headers={"Content-Type": "application/json"},
        )
        assert anonymous.status_code == 401

        cross_environment = _payload(state)
        cross_environment["environment_id"] = state["other_environment_id"]
        cross_project = _payload(state)
        cross_project["project_id"] = state["other_project_id"]
        for index, payload in enumerate((cross_environment, cross_project)):
            response = await client.post(
                "/policies/dry-run",
                json=payload,
                headers=_headers(admin, f"req_prd046_cross_{index}"),
            )
            assert response.status_code == 404
            assert response.json()["code"] == "policy.dry_run_context_not_found"


@pytest.mark.anyio
async def test_dry_run_bounded_duplicate_key_parser_and_safe_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """重复 key、NaN 和超限正文使用专用错误，响应不回显输入。"""

    state = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        duplicate = (
            '{"prompt":"safe","prompt":"'
            + PROMPT_SENTINEL
            + '","model":"default-chat","project_id":"'
            + state["project_id"]
            + '","environment_id":"'
            + state["environment_id"]
            + '","data_class":"confidential"}'
        )
        response = await client.post(
            "/policies/dry-run",
            content=duplicate.encode(),
            headers={**_headers(token, "req_prd046_duplicate"), "Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "policy.dry_run_payload_invalid"
        assert PROMPT_SENTINEL not in response.text

        nan_payload = _payload(state)
        nan_text = str(nan_payload).replace("'", '"').replace("None", "null")
        nan_text = nan_text.replace('"max_output_tokens": 32', '"max_output_tokens": NaN')
        response = await client.post(
            "/policies/dry-run",
            content=nan_text.encode(),
            headers={**_headers(token, "req_prd046_nan"), "Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "policy.dry_run_payload_invalid"
        response = await client.post(
            "/policies/dry-run",
            content=b"{" + b"x" * (256 * 1024) + b"}",
            headers={**_headers(token, "req_prd046_large"), "Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["code"] == "policy.dry_run_payload_too_large"
        deep_body = ('{"unknown":' + "[" * 1200 + "0" + "]" * 1200 + "}").encode()
        assert len(deep_body) < 256 * 1024
        response = await client.post(
            "/policies/dry-run",
            content=deep_body,
            headers={
                **_headers(token, "req_prd046_deep_json"),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "policy.dry_run_payload_invalid"


@pytest.mark.anyio
async def test_dry_run_policy_deny_is_http_200_and_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Policy deny 是已完成的模拟结果，不应误报为接口自身权限拒绝。"""

    state = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            json={
                "name": "PRD046 deny",
                "rule": {"condition_expr": f'tenant == "{state["tenant_id"]}"'},
                "action": {"type": "deny"},
                "status": "active",
                "priority": 1,
            },
            headers=_headers(token, "req_prd046_create_deny"),
        )
        assert created.status_code == 201, created.text
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, "req_prd046_deny"),
        )
    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    assert explanation["outcome"] == "would_deny"
    assert explanation["would_error_code"] == "gateway.policy_denied"
    assert explanation["route"]["status"] == "not_evaluated"
    assert explanation["dlp"]["status"] == "not_evaluated"
    assert explanation["budget"]["status"] == "not_evaluated"


@pytest.mark.anyio
async def test_dry_run_mask_with_spaced_data_class_is_http_200_simulation_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """data_class 允许内部普通空格；mask 只作 modifier，变量兼容告警仍独立保留。"""

    state = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            json={
                "name": "PRD046 mask",
                "rule": {"condition_expr": 'data_class == "high risk"'},
                "action": {"type": "mask"},
                "status": "active",
                "priority": 1,
            },
            headers=_headers(token, "req_prd046_create_mask"),
        )
        assert created.status_code == 201, created.text
        request_payload = _payload(state)
        request_payload["data_class"] = "high risk"
        response = await client.post(
            "/policies/dry-run",
            json=request_payload,
            headers=_headers(token, "req_prd046_mask"),
        )
    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    assert explanation["policy"]["decision"] == "allow"
    assert explanation["policy"]["mask_required"] is True
    assert len(explanation["policy"]["mask_matches"]) == 1
    assert explanation["policy"]["mask_matches"][0]["action"] == "mask"
    assert explanation["would_error_code"] != "gateway.policy_action_unsupported"
    assert explanation["runtime_compatible"] is False
    assert explanation["runtime_incompatibility_codes"] == [
        "policy_snapshot.variable_unsupported"
    ]
    assert explanation["route"]["status"] == "selected"


@pytest.mark.anyio
async def test_dry_run_supported_mask_is_runtime_compatible_and_non_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-068：可信 project 条件命中的 mask 继续路由，且不再产生动作不支持错误。"""

    state = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            json={
                "name": "PRD068 mask modifier",
                "rule": {
                    "condition_expr": f'project == "{state["project_id"]}"'
                },
                "action": {"type": "mask"},
                "status": "active",
                "priority": 1,
            },
            headers=_headers(token, "req_prd068_create_mask_modifier"),
        )
        assert created.status_code == 201, created.text
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, "req_prd068_mask_modifier"),
        )
    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    policy = explanation["policy"]
    assert policy["decision"] == "allow"
    assert policy["mask_required"] is True
    assert [item["policy_id"] for item in policy["mask_matches"]] == [
        created.json()["policy"]["policy_id"]
    ]
    assert explanation["runtime_compatible"] is True
    assert explanation["runtime_incompatibility_codes"] == []
    assert explanation["would_error_code"] is None
    assert explanation["route"]["status"] == "selected"


@pytest.mark.anyio
async def test_dry_run_missing_budget_context_is_would_error_not_policy_deny(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """缺少适用预算规则时 budget_remaining 访问失败是上下文错误，不能伪装成策略 deny。"""

    state = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            json={
                "name": "PRD046 budget context",
                "rule": {"condition_expr": "budget_remaining < 1"},
                "action": {"type": "deny"},
                "status": "active",
                "priority": 1,
            },
            headers=_headers(token, "req_prd046_create_budget_context"),
        )
        assert created.status_code == 201, created.text
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, "req_prd046_budget_context_missing"),
        )
    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    assert explanation["outcome"] == "would_error"
    assert explanation["would_error_code"] == "gateway.policy_context_missing"
    assert explanation["policy"]["missing_variable"] == "budget_remaining"


@pytest.mark.anyio
async def test_dry_run_invalid_dlp_regex_is_http_200_unavailable_what_if(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """持久正则损坏应形成 DLP 未执行结果，而非泄漏为接口服务故障。"""

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id, tenant_id, name, rule_type, pattern, pattern_hash,
          action, status, priority, created_at, updated_at
        )
        VALUES('dlp_prd046_invalid', ?, 'Invalid', 'regex', '(a+)+', ?,
               'block', 'active', 1, datetime('now'), datetime('now'))
        """,
        (state["tenant_id"], "f" * 64),
    )
    connection.commit()
    connection.close()
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, "req_prd046_invalid_dlp"),
        )
    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    assert explanation["dlp"]["status"] == "not_evaluated"
    assert explanation["dlp"]["reason_code"] == "dlp.rule_unavailable"
    assert explanation["would_error_code"] == "gateway.dlp_unavailable"
    assert explanation["budget"]["status"] == "not_evaluated"


@pytest.mark.anyio
async def test_dry_run_dlp_block_is_http_200_and_budget_not_evaluated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """关键词 block 只返回安全规则摘要，预算阶段短路且不创建 dlp_matches。"""

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id, tenant_id, name, rule_type, pattern, pattern_hash,
          action, status, priority, created_at, updated_at
        )
        VALUES('dlp_prd046_block', ?, 'Block', 'keyword', ?, ?,
               'block', 'active', 1, datetime('now'), datetime('now'))
        """,
        (state["tenant_id"], PROMPT_SENTINEL, "e" * 64),
    )
    connection.commit()
    connection.close()
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, "req_prd046_dlp_block"),
        )
    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    assert explanation["outcome"] == "would_block"
    assert explanation["would_error_code"] == "gateway.dlp_blocked"
    assert explanation["dlp"]["status"] == "blocked"
    assert explanation["dlp"]["matches"] == [
        {"dlp_rule_id": "dlp_prd046_block", "priority": 1, "action": "block"}
    ]
    assert explanation["budget"]["status"] == "not_evaluated"
    connection = connect_database(state["database_url"])
    assert connection.execute("SELECT COUNT(*) FROM dlp_matches").fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_dry_run_budget_block_is_http_200_without_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """tenant 预算最坏估算耗尽形成 would_block，但不创建 reservation/block/evaluation。"""

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        )
        VALUES('budget_prd046_block', ?, ?, 'tenant', ?, 'Tiny',
               0.000001, 80, 100, 'block', 'active', datetime('now'), datetime('now'))
        """,
        (state["tenant_id"], state["environment_id"], state["tenant_id"]),
    )
    connection.execute(
        """
        UPDATE models SET input_price_per_1k=1, output_price_per_1k=1, currency='CNY'
        WHERE tenant_id=? AND model_id=?
        """,
        (state["tenant_id"], state["model_id"]),
    )
    connection.commit()
    connection.close()
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, "req_prd046_budget_block"),
        )
    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    assert explanation["outcome"] == "would_block"
    assert explanation["would_error_code"] == "gateway.budget_exhausted"
    assert explanation["budget"]["status"] == "exhausted"
    assert explanation["budget"]["rules"][0]["action"] == "block"
    connection = connect_database(state["database_url"])
    assert connection.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM budget_blocks").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM budget_evaluations").fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_dry_run_chat_capability_does_not_preselect_alias_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """default chat=false 仍先被选中并错误短路，不能错误切到 chat=true fallback。"""

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.executemany(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, capabilities, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "model_prd046_no_chat",
                state["tenant_id"],
                state["provider_id"],
                "prd046-no-chat-internal",
                "no-chat",
                "no-chat",
                '{"chat": false}',
            ),
            (
                "model_prd046_chat_fallback",
                state["tenant_id"],
                state["provider_id"],
                "prd046-chat-fallback",
                "chat-fallback",
                "chat-fallback",
                '{"chat": true}',
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status, created_at, updated_at
        )
        VALUES('alias_prd046_chat', ?, 'prd046-chat', 'dev', ?, ?, ?, 'active',
               datetime('now'), datetime('now'))
        """,
        (
            state["tenant_id"],
            state["environment_id"],
            "model_prd046_no_chat",
            "model_prd046_chat_fallback",
        ),
    )
    connection.commit()
    connection.close()
    request_payload = _payload(state)
    request_payload["model"] = "prd046-chat"
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/dry-run",
            json=request_payload,
            headers=_headers(token, "req_prd046_chat_capability"),
        )
        connection = connect_database(state["database_url"])
        connection.execute(
            """
            UPDATE models SET status='disabled'
            WHERE tenant_id=? AND model_id IN (?, ?)
            """,
            (
                state["tenant_id"],
                "model_prd046_no_chat",
                "model_prd046_chat_fallback",
            ),
        )
        connection.commit()
        connection.close()
        disabled = await client.post(
            "/policies/dry-run",
            json=request_payload,
            headers=_headers(token, "req_prd046_disabled_chat_priority"),
        )
    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    assert explanation["route"]["status"] == "selected"
    assert explanation["route"]["selected_model_id"] == "model_prd046_no_chat"
    assert explanation["route"]["chat_capability_supported"] is False
    assert explanation["would_error_code"] == "gateway.model_not_found"
    assert explanation["dlp"]["status"] == "not_evaluated"
    assert disabled.status_code == 200, disabled.text
    disabled_explanation = disabled.json()["policy_explanation"]
    assert disabled_explanation["route"]["status"] == "unavailable"
    assert disabled_explanation["would_error_code"] == "gateway.model_disabled"


@pytest.mark.anyio
async def test_dry_run_cost_first_reuses_canonical_routing_and_token_estimate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成本预演复用线上排序与 TokenEstimate；环境 Key 无法证明时保持 unknown/partial。"""

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.executemany(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, capabilities, status, input_price_per_1k,
          output_price_per_1k, currency, price_schema_version, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, '{"chat": true}', 'active', ?, ?, 'CNY',
               'catalog-prd047-v1', datetime('now'), datetime('now'))
        """,
        [
            (
                "model_prd047_dry_expensive",
                state["tenant_id"],
                state["provider_id"],
                "prd047-dry-expensive",
                "dry-expensive",
                "dry-expensive",
                "8.000000000000",
                "8.000000000000",
            ),
            (
                "model_prd047_dry_cheap",
                state["tenant_id"],
                state["provider_id"],
                "prd047-dry-cheap",
                "dry-cheap",
                "dry-cheap",
                "1.000000000000",
                "1.000000000000",
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status, created_at, updated_at
        )
        VALUES('alias_prd047_dry_cost', ?, 'prd047-dry-cost', 'dev', ?, ?, ?,
               'active', datetime('now'), datetime('now'))
        """,
        (
            state["tenant_id"],
            state["environment_id"],
            "model_prd047_dry_expensive",
            "model_prd047_dry_cheap",
        ),
    )
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode='cost_first', version=version+1, updated_at=datetime('now')
        WHERE model_alias_id='alias_prd047_dry_cost'
        """
    )
    connection.commit()
    request_payload = _payload(state, prompt="12345678")
    request_payload["model"] = "prd047-dry-cost"
    request_payload["max_output_tokens"] = 8
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/dry-run",
            json=request_payload,
            headers=_headers(token, "req_prd047_dry_cost"),
        )

    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    route = explanation["route"]
    assert route["routing_mode"] == "cost_first"
    assert route["routing_policy_version"] == 2
    assert route["selected_model_id"] == "model_prd047_dry_cheap"
    assert route["reason_code"] == "routing.cost_lowest"
    assert route["estimated_input_tokens"] == 2
    assert route["estimated_output_tokens"] == 8
    assert route["estimated_input_tokens"] == explanation["budget"]["estimated_input_tokens"]
    assert route["estimated_output_tokens"] == explanation["budget"]["estimated_output_tokens"]
    assert [item["rank"] for item in route["candidates"]] == [2, 1]
    assert all(item["keypool_state"] == "unknown" for item in route["candidates"])
    assert all(item["price_source"] == "model_catalog" for item in route["candidates"])
    assert all(
        len(item["estimated_cost_amount"].partition(".")[2]) == 15 for item in route["candidates"]
    )
    assert explanation["gateway_parity"] == "partial"
    assert explanation["coverage"]["provider_credentials_keypool"] is False
    # 预演只写 explanation/event/audit，不生成真实路由、Usage 或 Provider 调用事实。
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM routing_decisions WHERE request_id='req_prd047_dry_cost'"
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM usage_ledger WHERE request_id='req_prd047_dry_cost'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.anyio
async def test_dry_run_quality_cost_uses_frozen_scores_without_runtime_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """质量成本预演复用 shared 加权，并完整返回冻结 Score 而不写运行时事实。

    参数：真实 0037 SQLite、管理员身份及两条同租户候选。返回值：无，断言高质量高价
    模型胜出、配置/双版本/分量可复现且旧 PRD-046 三事实之外零副作用。关键约束：延迟
    权重为零时不得伪造延迟分，预演不得写 Decision、Candidate、Usage 或反馈事件。
    """

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.executemany(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, capabilities, status, input_price_per_1k,
          output_price_per_1k, currency, price_schema_version, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, '{"chat": true}', 'active', ?, ?, 'CNY',
               'catalog-prd048-v1', datetime('now'), datetime('now'))
        """,
        [
            (
                "model_prd048_dry_quality",
                state["tenant_id"],
                state["provider_id"],
                "prd048-dry-quality",
                "prd048-dry-quality",
                "prd048-dry-quality",
                "8.000000000000",
                "8.000000000000",
            ),
            (
                "model_prd048_dry_cheap",
                state["tenant_id"],
                state["provider_id"],
                "prd048-dry-cheap",
                "prd048-dry-cheap",
                "prd048-dry-cheap",
                "1.000000000000",
                "1.000000000000",
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status, created_at, updated_at
        )
        VALUES('alias_prd048_dry_quality', ?, 'prd048-dry-quality', 'dev', ?, ?, ?,
               'active', datetime('now'), datetime('now'))
        """,
        (
            state["tenant_id"],
            state["environment_id"],
            "model_prd048_dry_quality",
            "model_prd048_dry_cheap",
        ),
    )
    admin_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND role='admin' ORDER BY user_id LIMIT 1",
        (state["tenant_id"],),
    ).fetchone()["user_id"]
    # 人工先验让零样本候选进入 ready；两个版本仍独立于 statistics_version=0。
    connection.executemany(
        """
        UPDATE model_quality_scores
        SET manual_quality_bps=?, feedback_version=feedback_version+1,
            feedback_updated_by=?, feedback_updated_at=datetime('now'),
            updated_at=datetime('now')
        WHERE tenant_id=? AND model_id=?
        """,
        [
            (10_000, admin_id, state["tenant_id"], "model_prd048_dry_quality"),
            (7_000, admin_id, state["tenant_id"], "model_prd048_dry_cheap"),
        ],
    )
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode='quality_cost', quality_weight_bps=9000,
            cost_weight_bps=1000, latency_weight_bps=0,
            version=version+1, updated_at=datetime('now')
        WHERE model_alias_id='alias_prd048_dry_quality'
        """
    )
    connection.commit()
    feedback_events_before = connection.execute(
        "SELECT COUNT(*) FROM model_quality_score_events"
    ).fetchone()[0]
    request_payload = _payload(state, prompt="12345678")
    request_payload["model"] = "prd048-dry-quality"
    request_payload["max_output_tokens"] = 8
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/dry-run",
            json=request_payload,
            headers=_headers(token, "req_prd048_dry_quality"),
        )

    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    route = explanation["route"]
    assert route["routing_mode"] == "quality_cost"
    assert route["selected_model_id"] == "model_prd048_dry_quality"
    assert route["reason_code"] == "routing.quality_cost_highest_score"
    assert route["quality_formula_version"] == "quality_cost_v1"
    assert route["quality_scoring_performed"] is True
    assert (
        route["quality_weight_bps"],
        route["cost_weight_bps"],
        route["latency_weight_bps"],
        route["quality_floor_bps"],
        route["minimum_sample_count"],
    ) == (9000, 1000, 0, 6000, 20)
    assert route["quality_as_of"] is not None
    candidates = route["candidates"]
    assert [item["rank"] for item in candidates] == [1, 2]
    assert [item["quality_evidence_state"] for item in candidates] == ["ready", "ready"]
    assert [item["manual_quality_bps"] for item in candidates] == [10_000, 7_000]
    assert [item["quality_statistics_version"] for item in candidates] == [0, 0]
    assert [item["quality_feedback_version"] for item in candidates] == [2, 2]
    assert all(item["quality_component_bps"] is not None for item in candidates)
    assert all(item["cost_component_bps"] is not None for item in candidates)
    assert all(item["latency_component_bps"] is None for item in candidates)
    assert all(item["weighted_score_bps"] is not None for item in candidates)
    # Dry-Run 只保留既有 explanation/audit/event；不能制造线上选择、计量或新反馈。
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM routing_decisions WHERE request_id='req_prd048_dry_quality'"
        ).fetchone()[0]
        == 0
    )
    assert connection.execute("SELECT COUNT(*) FROM routing_candidates").fetchone()[0] == 0
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM usage_ledger WHERE request_id='req_prd048_dry_quality'"
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM model_quality_score_events").fetchone()[0]
        == feedback_events_before
    )


@pytest.mark.parametrize(
    "corruption",
    ["invalid_status", "invalid_probe_state", "future_probe", "invalid_cooldown"],
)
@pytest.mark.anyio
async def test_dry_run_keypool_validates_every_managed_row_like_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    corruption: str,
) -> None:
    """首个可用 Key 不能短路后续坏行；枚举和时间损坏统一返回可追踪 503。"""

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.executemany(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, capabilities, status, input_price_per_1k,
          output_price_per_1k, currency, price_schema_version, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, '{"chat": true}', 'active', '1.0', '1.0',
               'CNY', 'catalog-prd047-keypool-v1', datetime('now'), datetime('now'))
        """,
        [
            (
                "model_prd047_keypool_default",
                state["tenant_id"],
                state["provider_id"],
                "prd047-keypool-default",
                "prd047-keypool-default",
                "prd047-keypool-default",
            ),
            (
                "model_prd047_keypool_fallback",
                state["tenant_id"],
                state["provider_id"],
                "prd047-keypool-fallback",
                "prd047-keypool-fallback",
                "prd047-keypool-fallback",
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status, created_at, updated_at
        )
        VALUES(
          'alias_prd047_keypool_parity', ?, 'prd047-keypool-parity', 'dev', ?,
          'model_prd047_keypool_default', 'model_prd047_keypool_fallback',
          'active', datetime('now'), datetime('now')
        )
        """,
        (state["tenant_id"], state["environment_id"]),
    )
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode='cost_first', version=version+1, updated_at=datetime('now')
        WHERE model_alias_id='alias_prd047_keypool_parity'
        """
    )
    connection.executemany(
        """
        INSERT INTO provider_credentials(
          provider_credential_id, tenant_id, provider_id, key_fingerprint,
          encrypted_key, name, key_prefix, weight, status,
          probe_health_status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'mhpk_test', 100, 'active', 'unknown',
               datetime('now'), datetime('now'))
        """,
        [
            (
                "pkey_prd047_parity_first",
                state["tenant_id"],
                state["provider_id"],
                "a" * 64,
                "aesgcm-v1:first-safe-fixture",
                "First available",
            ),
            (
                "pkey_prd047_parity_later",
                state["tenant_id"],
                state["provider_id"],
                "b" * 64,
                "aesgcm-v1:later-corrupt-fixture",
                "Later corrupted",
            ),
        ],
    )
    # 这些值模拟磁盘损坏/旧导入绕过；关闭 CHECK 只限本测试连接，API 新连接仍正常校验读取。
    connection.execute("PRAGMA ignore_check_constraints=ON")
    if corruption == "invalid_status":
        connection.execute(
            "UPDATE provider_credentials SET status='corrupt' "
            "WHERE provider_credential_id='pkey_prd047_parity_later'"
        )
    elif corruption == "invalid_probe_state":
        connection.execute(
            "UPDATE provider_credentials SET probe_health_status='corrupt' "
            "WHERE provider_credential_id='pkey_prd047_parity_later'"
        )
    elif corruption == "future_probe":
        connection.execute(
            "UPDATE provider_credentials SET probe_last_tested_at=datetime('now', '+1 day') "
            "WHERE provider_credential_id='pkey_prd047_parity_later'"
        )
    else:
        connection.execute(
            "UPDATE provider_credentials SET rate_limited_until='not-a-time' "
            "WHERE provider_credential_id='pkey_prd047_parity_later'"
        )
    connection.commit()
    connection.execute("PRAGMA ignore_check_constraints=OFF")

    payload = _payload(state, prompt="keypool parity")
    payload["model"] = "prd047-keypool-parity"
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/dry-run",
            json=payload,
            headers=_headers(token, f"req_prd047_keypool_{corruption}"),
        )

    assert response.status_code == 503
    assert response.json()["code"] == "policy.dry_run_unavailable"
    assert response.json()["request_id"] == f"req_prd047_keypool_{corruption}"
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM policy_explanations WHERE request_id=?",
            (f"req_prd047_keypool_{corruption}",),
        ).fetchone()[0]
        == 0
    )


@pytest.mark.anyio
async def test_dry_run_whole_candidate_invalid_unmatched_route_target_is_incompatible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未命中的 disabled route_to 目标仍使 whole candidate 与 PRD-045 发布器不兼容。"""

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, capabilities, status, created_at, updated_at
        )
        VALUES('model_prd046_target', ?, ?, 'prd046-target', 'target', 'target',
               '{"chat": true}', 'active', datetime('now'), datetime('now'))
        """,
        (state["tenant_id"], state["provider_id"]),
    )
    connection.commit()
    connection.close()
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            json={
                "name": "PRD046 unmatched route",
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "route_to", "target_model_id": "model_prd046_target"},
                "status": "active",
                "priority": 1,
            },
            headers=_headers(token, "req_prd046_create_route"),
        )
        assert created.status_code == 201, created.text
        connection = connect_database(state["database_url"])
        connection.execute(
            """
            UPDATE models SET status='disabled'
            WHERE tenant_id=? AND model_id='model_prd046_target'
            """,
            (state["tenant_id"],),
        )
        connection.commit()
        connection.close()
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, "req_prd046_invalid_target"),
        )
    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    assert explanation["outcome"] == "would_allow"
    assert explanation["runtime_compatible"] is False
    assert "policy_snapshot.route_target_invalid" in explanation["runtime_incompatibility_codes"]


@pytest.mark.anyio
async def test_dry_run_route_to_selects_tenant_target_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """命中 route_to 后只选择同租户配置目标，DLP/预算继续但绝不调用 Provider。"""

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, capabilities, status, created_at, updated_at
        )
        VALUES('model_prd046_selected_target', ?, ?, 'prd046-selected-target',
               'selected-target', 'selected-target', '{"chat": true}', 'active',
               datetime('now'), datetime('now'))
        """,
        (state["tenant_id"], state["provider_id"]),
    )
    connection.commit()
    connection.close()
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            json={
                "name": "PRD046 selected route",
                "rule": {"condition_expr": 'env == "dev"'},
                "action": {
                    "type": "route_to",
                    "target_model_id": "model_prd046_selected_target",
                },
                "status": "active",
                "priority": 1,
            },
            headers=_headers(token, "req_prd046_create_selected_route"),
        )
        assert created.status_code == 201, created.text
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, "req_prd046_selected_route"),
        )
    assert response.status_code == 200, response.text
    explanation = response.json()["policy_explanation"]
    assert explanation["policy"]["decision"] == "route_to"
    assert explanation["route"]["selected_model_id"] == "model_prd046_selected_target"
    assert explanation["would_error_code"] is None
    assert explanation["dlp"]["status"] == "clean"
    assert explanation["budget"]["status"] == "no_rules"
    assert explanation["provider_called"] is False


@pytest.mark.anyio
async def test_dry_run_duplicate_recovers_before_changed_config_and_reuses_stable_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """首次 commit 后配置被禁用，重复仍从旧三事实重派同 ID 并返回 409。"""

    state = _prepare_database(monkeypatch, tmp_path)
    dispatched_policy_ids: list[str] = []
    dispatched_audit_ids: list[str] = []

    def capture_policy(_config, event):
        """记录恢复路径重派的策略事件标识，验证事件身份保持稳定。"""

        dispatched_policy_ids.append(event.policy_event_id)

    def capture_audit(_config, _context, record, *, request_id, trace_id):
        """记录恢复路径重派的审计标识，并校验链路键始终存在。"""

        assert request_id
        assert trace_id
        assert record is not None
        dispatched_audit_ids.append(record.audit_log_id)

    monkeypatch.setattr(dry_run_service, "_dispatch_dry_run_event", capture_policy)
    monkeypatch.setattr(dry_run_service, "dispatch_audit_recorded", capture_audit)
    app = _build_app()
    request_id = "req_prd046_duplicate_recovery"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        first = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, request_id),
        )
        assert first.status_code == 200, first.text
        for index in range(29):
            invalid = await client.post(
                "/policies/dry-run",
                json={},
                headers=_headers(token, f"req_prd046_limiter_fill_{index}"),
            )
            assert invalid.status_code == 422
        limited = await client.post(
            "/policies/dry-run",
            json={},
            headers=_headers(token, "req_prd046_limiter_reject"),
        )
        assert limited.status_code == 429
        assert limited.json()["code"] == "policy.dry_run_rate_limited"
        connection = connect_database(state["database_url"])
        connection.execute(
            "UPDATE environments SET status='disabled' WHERE tenant_id=? AND environment_id=?",
            (state["tenant_id"], state["environment_id"]),
        )
        connection.execute(
            "UPDATE projects SET status='disabled' WHERE tenant_id=? AND project_id=?",
            (state["tenant_id"], state["project_id"]),
        )
        connection.commit()
        connection.close()
        second = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, request_id),
        )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "policy.dry_run_request_id_reused"
    assert dispatched_policy_ids[0] == dispatched_policy_ids[1]
    assert dispatched_audit_ids[0] == dispatched_audit_ids[1]
    connection = connect_database(state["database_url"])
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM policy_explanations WHERE request_id=?", (request_id,)
        ).fetchone()[0]
        == 1
    )
    connection.close()


@pytest.mark.anyio
async def test_dry_run_mid_write_failure_rolls_back_all_three_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """explanation/event 已插入后 audit 故障必须整体回滚，并返回不含 Prompt 的统一 503。"""

    state = _prepare_database(monkeypatch, tmp_path)

    def fail_audit(*_args, **_kwargs):
        """在审计写入阶段注入故障，验证同一事务整体回滚。"""

        # 人工审计故障必须走统一服务错误，且不得留下前两类部分事实。
        raise ApiError("audit_log.record_unavailable")

    monkeypatch.setattr(dry_run_service, "record_audit_log", fail_audit)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, "req_prd046_mid_write_failure"),
        )
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "policy.dry_run_unavailable"
    assert PROMPT_SENTINEL not in response.text
    connection = connect_database(state["database_url"])
    assert connection.execute("SELECT COUNT(*) FROM policy_explanations").fetchone()[0] == 0
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM policy_events WHERE event_type='policy.dry_run_executed'"
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='policy.dry_run_executed'"
        ).fetchone()[0]
        == 0
    )
    connection.close()


@pytest.mark.anyio
async def test_dry_run_two_thread_race_writes_one_fact_set_and_reuses_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """两个真实线程先完成各自读快照，再由 BEGIN IMMEDIATE 二查收敛为 200/409。"""

    state = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
    # ASGI 应用/路由器不是跨事件循环线程同步原语；为每个工作线程预先构造独立应用，
    # 只让两条请求竞争同一 SQLite、request_id 和写屏障，避免测试夹具偶发返回无关 405。
    thread_apps = (_build_app(), _build_app())

    original_persist = dry_run_service._persist_safe_facts
    write_barrier = threading.Barrier(2)
    dispatched_ids: list[str] = []
    dispatch_lock = threading.Lock()

    def synchronized_persist(*args, **kwargs):
        """让两个线程在写事务前会合，稳定复现同键提交竞争。"""

        write_barrier.wait(timeout=15)
        return original_persist(*args, **kwargs)

    def capture_event(_config, event):
        """在线程锁内收集重派事件标识，避免测试列表自身产生竞争。"""

        with dispatch_lock:
            dispatched_ids.append(event.policy_event_id)

    monkeypatch.setattr(dry_run_service, "_persist_safe_facts", synchronized_persist)
    monkeypatch.setattr(dry_run_service, "_dispatch_dry_run_event", capture_event)
    request_id = "req_prd046_real_connection_race"

    def send_request(thread_app) -> tuple[int, str]:
        """在线程专属应用和事件循环内发送相同幂等键的请求。"""

        async def send() -> tuple[int, str]:
            """为当前线程创建独立客户端并返回稳定状态与错误码。"""

            async with AsyncClient(
                transport=ASGITransport(app=thread_app), base_url="http://control"
            ) as thread_client:
                response = await thread_client.post(
                    "/policies/dry-run",
                    json=_payload(state),
                    headers=_headers(token, request_id),
                )
                return response.status_code, response.json().get("code", "")

        return asyncio.run(send())

    results = await asyncio.gather(
        asyncio.to_thread(send_request, thread_apps[0]),
        asyncio.to_thread(send_request, thread_apps[1]),
    )
    assert sorted(status for status, _code in results) == [200, 409]
    assert [code for status, code in results if status == 409] == [
        "policy.dry_run_request_id_reused"
    ]
    assert len(dispatched_ids) == 2
    assert dispatched_ids[0] == dispatched_ids[1]
    connection = connect_database(state["database_url"])
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM policy_explanations WHERE request_id=?", (request_id,)
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            """
        SELECT COUNT(*) FROM policy_events
        WHERE request_id=? AND event_type='policy.dry_run_executed'
        """,
            (request_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            """
        SELECT COUNT(*) FROM audit_logs
        WHERE request_id=? AND action='policy.dry_run_executed'
        """,
            (request_id,),
        ).fetchone()[0]
        == 1
    )
    connection.close()


@pytest.mark.anyio
async def test_dry_run_accepts_twenty_thousand_unicode_codepoints_within_wire_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """20,000 astral surrogate escape 与中文 UTF-8 均小于 256 KiB 并通过业务上限。"""

    state = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        cases = (
            ("astral", "\U0001f642" * 20_000, True),
            ("chinese", "策" * 20_000, False),
        )
        for label, prompt, ensure_ascii in cases:
            request_payload = _payload(state, prompt=prompt)
            body = json.dumps(
                request_payload,
                ensure_ascii=ensure_ascii,
                separators=(",", ":"),
            ).encode("utf-8")
            assert len(body) <= 256 * 1024
            response = await client.post(
                "/policies/dry-run",
                content=body,
                headers={
                    **_headers(token, f"req_prd046_unicode_{label}"),
                    "Content-Type": "application/json",
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["policy_explanation"]["input_summary"] == {
                "prompt_redacted": True,
                "prompt_codepoints": 20_000,
                "estimated_input_tokens": 5_000,
            }
            assert prompt not in response.text


def test_dry_run_budget_facts_use_shared_finops_aggregation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-Run 把完整隔离键和业务日交给共享 FinOps 聚合器，并精确恢复分项 CNY。"""

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        )
        VALUES('budget_prd047_outstanding', ?, ?, 'tenant', ?, 'Outstanding parity',
               100, 80, 100, 'block', 'active', datetime('now'), datetime('now'))
        """,
        (state["tenant_id"], state["environment_id"], state["tenant_id"]),
    )
    connection.commit()
    calls: list[tuple[object, ...]] = []

    def load_spend(
        current_connection: sqlite3.Connection,
        *,
        tenant_id: str,
        environment_id: str,
        scope_type: str,
        scope_id: str,
        budget_rule_id: str,
        budget_date: str,
        start_utc: str,
        end_utc: str,
    ) -> BudgetSpendAmounts:
        """记录全部可信查询边界，并返回正式消费与未结算预留的整数微元。"""

        calls.append(
            (
                current_connection,
                tenant_id,
                environment_id,
                scope_type,
                scope_id,
                budget_rule_id,
                budget_date,
                start_utc,
                end_utc,
            )
        )
        return BudgetSpendAmounts(
            posted_cny_micros=2_750_000,
            reserved_cny_micros=3_250_000,
            effective_cny_micros=6_000_000,
        )

    monkeypatch.setattr(
        dry_run_service,
        "load_budget_spend_amounts",
        load_spend,
    )
    facts = dry_run_service._load_budget_rule_facts(
        connection,
        tenant_id=state["tenant_id"],
        environment_id=state["environment_id"],
        project_id=state["project_id"],
        now=dry_run_service.datetime(2026, 7, 19, 4, 0, tzinfo=dry_run_service.UTC),
    )

    assert len(facts) == 1
    assert facts[0].spent_cny == Decimal("2.75")
    assert facts[0].reserved_cny == Decimal("3.25")
    assert calls == [
        (
            connection,
            state["tenant_id"],
            state["environment_id"],
            "tenant",
            state["tenant_id"],
            "budget_prd047_outstanding",
            "2026-07-19",
            "2026-07-18T16:00:00Z",
            "2026-07-19T16:00:00Z",
        )
    ]
    connection.close()


@pytest.mark.anyio
async def test_dry_run_budget_spend_unavailable_fails_closed_without_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """正式账本金额不可证明时返回带 request_id 的统一 503，且不持久化预演事实。"""

    state = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(state["database_url"])
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        )
        VALUES('budget_prd052_unavailable', ?, ?, 'tenant', ?, 'Unavailable spend',
               100, 80, 100, 'block', 'active', datetime('now'), datetime('now'))
        """,
        (state["tenant_id"], state["environment_id"], state["tenant_id"]),
    )
    connection.commit()
    connection.close()

    def fail_spend(*_args, **_kwargs):
        """模拟正式 Usage Charge 证据损坏，异常消息不携带任何原始金额。"""

        # 不可证明的正式金额必须失败关闭，不能按零消费继续生成误导性的预演结果。
        raise BudgetSpendUnavailableError("budget_spend.usage_evidence_unavailable")

    monkeypatch.setattr(dry_run_service, "load_budget_spend_amounts", fail_spend)
    request_id = "req_prd052_dry_run_budget_unavailable"
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/dry-run",
            json=_payload(state),
            headers=_headers(token, request_id),
        )

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "policy.dry_run_unavailable"
    assert response.json()["request_id"] == request_id
    assert PROMPT_SENTINEL not in response.text
    connection = connect_database(state["database_url"])
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM policy_explanations WHERE request_id=?", (request_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM policy_events WHERE request_id=?", (request_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE request_id=?", (request_id,)
        ).fetchone()[0]
        == 0
    )
    connection.close()
