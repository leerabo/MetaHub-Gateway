"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_usage.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.usage.routes import router as usage_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试密码只用于用量查询权限矩阵，成本数据来自测试账本夹具。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
FINANCE_PASSWORD = "FinanceLocal!2026"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证和用量路由的测试应用。"""

    app = create_service_app("control-plane-api", "Usage Test Control API")
    app.include_router(auth_router)
    app.include_router(usage_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """准备 PRD-017 用量汇总测试所需的租户、用户、请求日志、价格快照和账本数据。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    tenant_id = seed.tenant_id
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

    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd017_developer",
                tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd017_viewer",
                tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
            (
                "user_prd017_finance",
                tenant_id,
                "finance@example.com",
                "Finance User",
                hash_password(FINANCE_PASSWORD),
                "finance_admin",
            ),
        ],
    )
    # 用量查询夹具没有在线 Policy，仍必须先用 NULL/disabled claim 固定两个请求身份。
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
                "req_prd017_real",
                "trc_prd017_real",
                virtual_key["virtual_key_id"],
                "2026-07-09T09:00:00Z",
            ),
            (
                tenant_id,
                "req_prd017_estimated",
                "trc_prd017_estimated",
                virtual_key["virtual_key_id"],
                "2026-07-09T10:00:00Z",
            ),
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
          'local_openai', 'success', NULL, 30, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "req_prd017_real",
                tenant_id,
                "trc_prd017_real",
                virtual_key["virtual_key_id"],
                provider["provider_id"],
                model["model_id"],
                100,
                50,
                150,
                0.35,
                "CNY",
                "seed-cny",
                "2026-07-09T09:00:00Z",
                "2026-07-09T09:00:01Z",
            ),
            (
                "req_prd017_estimated",
                tenant_id,
                "trc_prd017_estimated",
                virtual_key["virtual_key_id"],
                provider["provider_id"],
                model["model_id"],
                20,
                30,
                50,
                0.01,
                "USD",
                "seed-usd",
                "2026-07-09T10:00:00Z",
                "2026-07-09T10:00:01Z",
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO price_snapshots(
          price_snapshot_id, tenant_id, request_id, model_id, provider_id, provider_code,
          currency, input_price_per_1k, output_price_per_1k, price_schema_version,
          billing_dimensions, provider_raw_mapping, source, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'local_openai', ?, ?, ?, ?, '{}', '{}', 'model_catalog', 'active', ?, ?)
        """,
        [
            (
                "prs_prd017_cny",
                tenant_id,
                "req_prd017_real",
                model["model_id"],
                provider["provider_id"],
                "CNY",
                0.001,
                0.005,
                "seed-cny",
                "2026-07-09T09:00:01Z",
                "2026-07-09T09:00:01Z",
            ),
            (
                "prs_prd017_usd",
                tenant_id,
                "req_prd017_estimated",
                model["model_id"],
                provider["provider_id"],
                "USD",
                0.001,
                0.001,
                "seed-usd",
                "2026-07-09T10:00:01Z",
                "2026-07-09T10:00:01Z",
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens, total_tokens, cache_tokens,
          raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd, currency, price_schema_version,
          estimated, usage_source, price_snapshot_id, model_id, provider_id, virtual_key_id,
          endpoint, request_model, resolved_model, provider_code, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '/v1/chat/completions',
          'company-default', 'default-chat', 'local_openai', ?, ?)
        """,
        [
            (
                "usg_prd017_real",
                tenant_id,
                "req_prd017_real",
                100,
                50,
                150,
                15,
                '{"prompt_tokens": 100, "completion_tokens": 50, "api_key": "sk-history-secret"}',
                '{"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cache_tokens": 15}',
                0.35,
                0.35,
                0,
                "CNY",
                "seed-cny",
                0,
                "upstream",
                "prs_prd017_cny",
                model["model_id"],
                provider["provider_id"],
                virtual_key["virtual_key_id"],
                "2026-07-09T09:00:01Z",
                "2026-07-09T09:00:01Z",
            ),
            (
                "usg_prd017_estimated",
                tenant_id,
                "req_prd017_estimated",
                20,
                30,
                50,
                0,
                "{}",
                '{"input_tokens": 20, "output_tokens": 30, "total_tokens": 50, "estimated": true}',
                0.01,
                0,
                0.01,
                "USD",
                "seed-usd",
                1,
                "estimated",
                "prs_prd017_usd",
                model["model_id"],
                provider["provider_id"],
                virtual_key["virtual_key_id"],
                "2026-07-09T10:00:01Z",
                "2026-07-09T10:00:01Z",
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_other_prd017', 'other-prd017', 'Other Tenant', 'internal_only', 'active', datetime('now'), datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, snapshot_version,
          policy_snapshot_status, claimed_at
        ) VALUES('tenant_other_prd017', 'req_other_prd017', 'trc_other_prd017',
                 NULL, 'disabled', datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO request_logs(request_id, tenant_id, trace_id, status, created_at, updated_at)
        VALUES('req_other_prd017', 'tenant_other_prd017', 'trc_other_prd017', 'success', datetime('now'), datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens, total_tokens,
          raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd, currency, created_at, updated_at
        )
        VALUES('usg_other_prd017', 'tenant_other_prd017', 'req_other_prd017', 999, 999, 1998,
          '{}', '{}', 9, 9, 0, 'CNY', datetime('now'), datetime('now'))
        """
    )
    connection.commit()
    # 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
    return database_url, tenant_id


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
async def test_usage_summary_and_records_are_tenant_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：管理员和财务角色查询租户用量，开发者被后端权限矩阵拒绝。"""

    _database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        finance_token = await login(client, "finance@example.com", FINANCE_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        summary = await client.get(
            "/usage/summary?start_date=2026-07-09&end_date=2026-07-09",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd017_summary"},
        )
        estimated_records = await client.get(
            "/usage/records?estimated=true",
            headers={"Authorization": f"Bearer {finance_token}", "X-Request-ID": "req_prd017_records"},
        )
        real_records = await client.get(
            "/usage/records?estimated=false",
            headers={"Authorization": f"Bearer {finance_token}"},
        )
        # PRD-059 明确禁止 Developer 获得 billing.*；前端隐藏入口不能替代此后端 403。
        developer_denied = await client.get(
            "/usage/records?estimated=true",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd017_developer_usage_denied",
            },
        )

    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["totals"]["request_count"] == 2
    assert body["totals"]["total_tokens"] == 200
    assert body["totals"]["cache_tokens"] == 15
    assert body["totals"]["cost_cny"] == 0.35
    assert body["totals"]["cost_usd"] == 0.01
    assert body["totals"]["estimated_records"] == 1
    assert body["by_day"][0]["day"] == "2026-07-09"
    assert body["by_model"][0]["provider_code"] == "local_openai"
    assert body["by_virtual_key"][0]["virtual_key_prefix"] is not None
    assert "tenant_other_prd017" not in summary.text

    assert estimated_records.status_code == 200, estimated_records.text
    records_body = estimated_records.json()
    assert records_body["total"] == 1
    record = records_body["usage_records"][0]
    assert record["tenant_id"] == tenant_id
    assert record["request_id"] == "req_prd017_estimated"
    assert record["estimated"] is True
    assert record["usage_source"] == "estimated"
    assert record["cost_usd"] == 0.01
    assert record["price_snapshot_id"] == "prs_prd017_usd"

    real_record = real_records.json()["usage_records"][0]
    assert real_record["provider_raw_usage"]["prompt_tokens"] == 100
    assert real_record["provider_raw_usage"]["api_key"] == "[REDACTED]"
    assert "sk-history-secret" not in real_records.text
    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert developer_denied.json()["request_id"] == "req_prd017_developer_usage_denied"


@pytest.mark.anyio
async def test_usage_permissions_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：viewer 被后端拒绝，非法日期、estimated 和分页返回统一 usage 错误码。"""

    prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        viewer_denied = await client.get(
            "/usage/summary",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        bad_date = await client.get(
            "/usage/summary?start_date=2026-07-10&end_date=2026-07-09",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        bad_estimated = await client.get(
            "/usage/records?estimated=maybe",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        bad_limit = await client.get(
            "/usage/records?limit=101",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert bad_date.status_code == 422
    assert bad_date.json()["code"] == "usage.date_invalid"
    assert bad_estimated.status_code == 422
    assert bad_estimated.json()["code"] == "usage.query_invalid"
    assert bad_limit.status_code == 422
    assert bad_limit.json()["code"] == "usage.query_invalid"
