"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_dashboard.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.console.routes import router as console_router
from metahub_control.dashboard.routes import router as dashboard_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# Dashboard 测试密码仅用于认证夹具，业务数据仍通过 tenant_id 与真实日志/账本断言隔离。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造包含认证、控制台事件和 Dashboard 路由的测试应用。"""

    app = create_service_app("control-plane-api", "Dashboard Test Control API")
    app.include_router(auth_router)
    app.include_router(console_router)
    app.include_router(dashboard_router)
    return app


def prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    with_dashboard_rows: bool = True,
) -> tuple[str, str]:
    """准备 PRD-026 Dashboard 测试所需的租户、角色用户、调用日志和用量账本。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    tenant_id = seed.tenant_id
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        [
            (
                "user_prd026_developer",
                tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
                db_time(),
                db_time(),
            ),
            (
                "user_prd026_viewer",
                tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
                db_time(),
                db_time(),
            ),
        ],
    )
    if with_dashboard_rows:
        insert_dashboard_rows(connection, tenant_id)
    connection.commit()
    # 租户状态或租户筛选是 Dashboard 隔离边界，命中异常时必须立即截断。
    return database_url, tenant_id


def insert_dashboard_rows(connection, tenant_id: str) -> None:
    """插入当前租户与其他租户的 Dashboard 聚合夹具数据。"""

    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (tenant_id,),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    assert provider is not None
    assert model is not None
    assert virtual_key is not None
    request_rows = [
        ("req_prd026_fast", "trc_prd026_fast", "success", None, 20, 100, 0.20, db_time(-12)),
        ("req_prd026_slow", "trc_prd026_slow", "success", None, 400, 300, 0.80, db_time(-8)),
        (
            "req_prd026_failed",
            "trc_prd026_failed",
            "failed",
            "gateway.upstream_rate_limited",
            120,
            50,
            0.25,
            db_time(-4),
        ),
    ]
    # Dashboard 历史数据没有发布快照，仍需按 0034 真实顺序先写 NULL/disabled claim。
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
                request_id,
                trace_id,
                virtual_key["virtual_key_id"],
                created_at,
            )
            for request_id, trace_id, _status, _error_code, _latency_ms, _total_tokens, _cost_cny, created_at in request_rows
        ],
    )
    connection.executemany(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          endpoint, request_model, resolved_model, provider_code, status, error_code,
          latency_ms, input_tokens, output_tokens, total_tokens, estimated_cost_amount,
          currency, price_schema_version, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, '/v1/chat/completions', 'company-default', 'default-chat',
          'local_openai', ?, ?, ?, ?, 0, ?, ?, 'CNY', 'dashboard-test-v1', ?, ?)
        """,
        [
            (
                request_id,
                tenant_id,
                trace_id,
                virtual_key["virtual_key_id"],
                provider["provider_id"],
                model["model_id"],
                status,
                error_code,
                latency_ms,
                total_tokens,
                total_tokens,
                cost_cny,
                created_at,
                created_at,
            )
            for request_id, trace_id, status, error_code, latency_ms, total_tokens, cost_cny, created_at in request_rows
        ],
    )
    connection.executemany(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens, total_tokens, cache_tokens,
          raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd, currency, price_schema_version,
          estimated, usage_source, model_id, provider_id, virtual_key_id,
          endpoint, request_model, resolved_model, provider_code, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?, 0, 'CNY', 'dashboard-test-v1',
          0, 'upstream', ?, ?, ?, '/v1/chat/completions', 'company-default', 'default-chat',
          'local_openai', ?, ?)
        """,
        [
            (
                f"usg_{request_id}",
                tenant_id,
                request_id,
                total_tokens,
                total_tokens,
                '{"secret_sample": "sk-dashboard-secret"}',
                f'{{"total_tokens": {total_tokens}}}',
                cost_cny,
                cost_cny,
                model["model_id"],
                provider["provider_id"],
                virtual_key["virtual_key_id"],
                created_at,
                created_at,
            )
            for request_id, _trace_id, _status, _error_code, _latency_ms, total_tokens, cost_cny, created_at in request_rows
        ],
    )
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_other_prd026', 'other-prd026', 'Other Dashboard Tenant', 'internal_only', 'active', ?, ?)
        """,
        (db_time(), db_time()),
    )
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, snapshot_version,
          policy_snapshot_status, claimed_at
        ) VALUES('tenant_other_prd026', 'req_other_prd026', 'trc_other_prd026',
                 NULL, 'disabled', ?)
        """,
        (db_time(),),
    )
    connection.execute(
        """
        INSERT INTO request_logs(request_id, tenant_id, trace_id, status, total_tokens, estimated_cost_amount, currency, created_at, updated_at)
        VALUES('req_other_prd026', 'tenant_other_prd026', 'trc_other_prd026', 'success', 999, 99, 'CNY', ?, ?)
        """,
        (db_time(), db_time()),
    )


def db_time(offset_minutes: int = 0) -> str:
    """生成 Dashboard 今日窗口内的 UTC 数据库时间字符串。"""

    # Dashboard 按上海业务日统计；先固定到当前上海日中午再转 UTC，避免任一时区午夜的负偏移跨日。
    shanghai_noon = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )
    value = shanghai_noon.astimezone(UTC) + timedelta(minutes=offset_minutes)
    return value.isoformat().replace("+00:00", "Z")


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
async def test_dashboard_overview_aggregates_real_logs_usage_and_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：Dashboard 聚合当前租户真实日志和 usage，并可记录 dashboard.viewed 事件。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        dashboard = await client.get(
            "/dashboard/overview",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd026_dashboard"},
        )
        event = await client.post(
            "/console/events",
            json={
                "event_type": "dashboard.viewed",
                "navigation_item_id": "overview",
                "target_section": "overview",
                "path": "/#overview",
                "payload": {
                    "source": "sidebar",
                    "hash": "#overview",
                    "viewport_class": "desktop",
                },
            },
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd026_event"},
        )

    assert dashboard.status_code == 200, dashboard.text
    body = dashboard.json()
    metrics = {item["id"]: item for item in body["metrics"]}
    assert metrics["today_requests"]["value"] == 3
    assert metrics["success_rate"]["value"] == 66.67
    assert metrics["success_rate"]["status"] == "critical"
    assert metrics["p95_latency_ms"]["value"] == 400
    assert metrics["today_cost_cny"]["value"] == 1.25
    assert metrics["total_tokens"]["value"] == 450
    assert metrics["error_rate"]["value"] == 33.33
    assert body["empty_state"]["is_empty"] is False
    assert body["top_models"][0]["request_count"] == 3
    assert body["top_models"][0]["success_count"] == 2
    assert body["top_models"][0]["total_tokens"] == 450
    assert body["top_virtual_keys"][0]["virtual_key_prefix"] is not None
    assert body["recent_errors"][0]["request_id"] == "req_prd026_failed"
    assert body["recent_errors"][0]["error_code"] == "gateway.upstream_rate_limited"
    assert body["request_id"] == "req_prd026_dashboard"
    assert tenant_id not in dashboard.text
    assert "tenant_other_prd026" not in dashboard.text
    assert "sk-dashboard-secret" not in dashboard.text

    assert event.status_code == 200, event.text
    row = connect_database(database_url).execute(
        """
        SELECT tenant_id, event_type, navigation_item_id, request_id, payload_json
        FROM console_page_events
        WHERE event_id=?
        """,
        (event.json()["event_id"],),
    ).fetchone()
    assert row is not None
    assert row["tenant_id"] == tenant_id
    assert row["event_type"] == "dashboard.viewed"
    assert row["navigation_item_id"] == "overview"
    assert row["request_id"] == "req_prd026_event"
    assert "token" not in row["payload_json"]


@pytest.mark.anyio
async def test_dashboard_empty_state_viewer_access_and_auth_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：viewer 可读安全聚合摘要，未登录被拒绝，空租户返回接入引导。"""

    prepare_database(monkeypatch, tmp_path, with_dashboard_rows=False)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        viewer_dashboard = await client.get(
            "/dashboard/overview",
            headers={"Authorization": f"Bearer {viewer_token}", "X-Request-ID": "req_prd026_viewer"},
        )
        anonymous_dashboard = await client.get("/dashboard/overview")

    assert viewer_dashboard.status_code == 200, viewer_dashboard.text
    body = viewer_dashboard.json()
    metrics = {item["id"]: item for item in body["metrics"]}
    assert metrics["today_requests"]["value"] == 0
    assert metrics["today_requests"]["status"] == "empty"
    assert body["empty_state"]["is_empty"] is True
    assert [action["target"] for action in body["empty_state"]["recommended_actions"]] == [
        "providers",
        "models",
        "virtual-keys",
        "debug-console",
    ]
    assert body["top_models"] == []
    assert body["top_virtual_keys"] == []
    assert body["recent_errors"] == []

    assert anonymous_dashboard.status_code == 401
    assert anonymous_dashboard.json()["code"] == "auth.token_missing"
    assert anonymous_dashboard.json()["request_id"]
