"""模块职责：验证 PRD-031 有效期价格计划、Usage 快照不可变性和后端权限。

维护边界：测试必须覆盖 tenant_id、服务器生效时间、Decimal 成本和敏感信息不落事件正文。
来源文件：control-plane-api/tests/integration/test_model_prices.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.model_prices import service as model_price_service
from metahub_control.model_prices.routes import router as model_prices_router
from metahub_gateway.usage import ledger as usage_ledger
from metahub_gateway.usage.ledger import (
    RequestLogContext,
    claim_gateway_request,
    record_request_log,
    record_usage_ledger,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试密码只用于临时角色夹具，不会写入价格事件、Usage 或结构化业务日志。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
CONTROL_NOW = datetime(2026, 7, 15, 8, 0, 0, tzinfo=UTC)


def sqlite_url(path: Path) -> str:
    """把 pytest 临时数据库路径转换成应用支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造仅包含认证和价格接口的控制面，缩小 PRD-031 集成测试边界。"""

    app = create_service_app("control-plane-api", "Model Price Test API")
    app.include_router(auth_router)
    app.include_router(model_prices_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str, dict[str, str]]:
    """准备当前租户模型、环境、Key 和权限用户，供 API 与 Usage 共同使用。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd031-auth-secret")
    monkeypatch.setattr(model_price_service, "_utcnow", lambda: CONTROL_NOW)
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
                "user_prd031_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd031_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    model = connection.execute(
        """
        SELECT m.model_id, m.provider_id, p.provider_code
        FROM models m
        JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        WHERE m.tenant_id=? AND m.model_alias='default-chat'
        """,
        (seed.tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id, environment_id, environment FROM virtual_keys WHERE tenant_id=?",
        (seed.tenant_id,),
    ).fetchone()
    assert model is not None
    assert virtual_key is not None
    connection.commit()
    # 返回当前租户和安全资源 ID，后续测试不得携带或输出任何 Provider Key。
    return database_url, seed.tenant_id, {
        "model_id": model["model_id"],
        "provider_id": model["provider_id"],
        "provider_code": model["provider_code"],
        "virtual_key_id": virtual_key["virtual_key_id"],
        "environment_id": virtual_key["environment_id"],
        "environment_code": virtual_key["environment"],
    }


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录指定测试角色并返回仅用于当前 HTTP 客户端的短期 Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只放入后续 Authorization 头，不写入数据库断言或价格事件。
    return response.json()["access_token"]


def record_usage_at(
    monkeypatch: pytest.MonkeyPatch,
    app,
    metadata: dict[str, str],
    tenant_id: str,
    request_id: str,
    occurred_at: datetime,
) -> None:
    """在固定服务器时刻记录一条 1000 输入 Token Usage，便于直接比较单价成本。"""

    monkeypatch.setattr(usage_ledger, "_utcnow", lambda: occurred_at)
    context = RequestLogContext(
        tenant_id=tenant_id,
        request_id=request_id,
        trace_id=f"trc_{request_id}",
        virtual_key_id=metadata["virtual_key_id"],
        environment_id=metadata["environment_id"],
        environment_code=metadata["environment_code"],
        provider_id=metadata["provider_id"],
        model_id=metadata["model_id"],
        endpoint="/v1/chat/completions",
        request_model="default-chat",
        resolved_model="default-chat",
        provider_code=metadata["provider_code"],
    )
    # 0034 起模型调用日志必须先以 NULL/disabled 快照语义 claim；价格测试不消费 Policy，
    # 但仍需复用真实写入顺序，避免绕过 tenant+request_id 不可变约束。
    claim_gateway_request(app.state.config, context)
    record_request_log(app.state.config, context, status="success", error_code=None, latency_ms=10)
    record_usage_ledger(
        app.state.config,
        context,
        {"prompt_tokens": 1000, "completion_tokens": 0, "total_tokens": 1000},
    )


@pytest.mark.anyio
async def test_effective_prices_keep_historical_usage_immutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：未来价格到点后才生效，新旧 Usage 分别引用当时计划且旧账不变。"""

    database_url, tenant_id, metadata = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        old_plan_response = await client.post(
            "/model-prices",
            headers={**admin_headers, "X-Request-ID": "req_prd031_old_price"},
            json={
                "model_id": metadata["model_id"],
                "input_price_per_1k": "1.25",
                "output_price_per_1k": "2.5",
                "currency": "CNY",
                "effective_from": "2026-07-01T00:00:00Z",
                "price_schema_version": "manual-old",
            },
        )
        future_plan_response = await client.post(
            "/model-prices",
            headers=admin_headers,
            json={
                "model_id": metadata["model_id"],
                "input_price_per_1k": "3.75",
                "output_price_per_1k": "5",
                "currency": "CNY",
                "effective_from": "2026-08-01T00:00:00Z",
                "price_schema_version": "manual-future",
            },
        )
        listed = await client.get(
            "/model-prices",
            headers={"Authorization": f"Bearer {developer_token}"},
        )

    assert old_plan_response.status_code == 201, old_plan_response.text
    assert future_plan_response.status_code == 201, future_plan_response.text
    statuses = {item["price_schema_version"]: item["effective_status"] for item in listed.json()["price_plans"]}
    assert statuses == {"manual-old": "current", "manual-future": "scheduled"}

    record_usage_at(monkeypatch, app, metadata, tenant_id, "req_prd031_before", datetime(2026, 7, 20, tzinfo=UTC))
    connection = connect_database(database_url)
    before = connection.execute(
        """
        SELECT ul.cost_cny, ul.price_schema_version, ul.price_snapshot_id,
               ps.price_plan_id, ps.effective_from
        FROM usage_ledger ul
        JOIN price_snapshots ps ON ps.tenant_id=ul.tenant_id AND ps.price_snapshot_id=ul.price_snapshot_id
        WHERE ul.tenant_id=? AND ul.request_id='req_prd031_before'
        """,
        (tenant_id,),
    ).fetchone()
    record_usage_at(monkeypatch, app, metadata, tenant_id, "req_prd031_after", datetime(2026, 8, 2, tzinfo=UTC))
    after = connection.execute(
        """
        SELECT ul.cost_cny, ul.price_schema_version, ul.price_snapshot_id,
               ps.price_plan_id, ps.effective_from
        FROM usage_ledger ul
        JOIN price_snapshots ps ON ps.tenant_id=ul.tenant_id AND ps.price_snapshot_id=ul.price_snapshot_id
        WHERE ul.tenant_id=? AND ul.request_id='req_prd031_after'
        """,
        (tenant_id,),
    ).fetchone()
    before_again = connection.execute(
        "SELECT cost_cny, price_schema_version, price_snapshot_id FROM usage_ledger WHERE request_id='req_prd031_before'",
    ).fetchone()

    assert Decimal(str(before["cost_cny"])) == Decimal("1.25")
    assert before["price_schema_version"] == "manual-old"
    assert before["effective_from"] == "2026-07-01T00:00:00Z"
    assert Decimal(str(after["cost_cny"])) == Decimal("3.75")
    assert after["price_schema_version"] == "manual-future"
    assert after["effective_from"] == "2026-08-01T00:00:00Z"
    assert before["price_plan_id"] != after["price_plan_id"]
    assert before["price_snapshot_id"] != after["price_snapshot_id"]
    assert Decimal(str(before_again["cost_cny"])) == Decimal("1.25")
    assert before_again["price_schema_version"] == "manual-old"
    assert before_again["price_snapshot_id"] == before["price_snapshot_id"]

    event = connection.execute(
        "SELECT event_type, request_id, payload_json FROM model_price_events WHERE price_plan_id=?",
        (old_plan_response.json()["price_plan"]["price_plan_id"],),
    ).fetchone()
    assert event["event_type"] == "price.updated"
    assert event["request_id"] == "req_prd031_old_price"
    payload = json.loads(event["payload_json"])
    assert payload["price_schema_version"] == "manual-old"
    assert "key" not in event["payload_json"].lower()
    assert "prompt" not in event["payload_json"].lower()


@pytest.mark.anyio
async def test_model_price_permissions_conflicts_and_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：developer 不可写、viewer 不可读，重复时间和非法金额返回统一错误。"""

    _, _, metadata = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    valid_payload = {
        "model_id": metadata["model_id"],
        "input_price_per_1k": "0.1",
        "output_price_per_1k": "0.2",
        "currency": "USD",
        "effective_from": "2026-07-10T00:00:00+08:00",
        "price_schema_version": "manual-permission",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        created = await client.post("/model-prices", headers=admin_headers, json=valid_payload)
        duplicate_time = await client.post(
            "/model-prices",
            headers=admin_headers,
            json={**valid_payload, "price_schema_version": "manual-duplicate-time"},
        )
        invalid_price = await client.post(
            "/model-prices",
            headers=admin_headers,
            json={**valid_payload, "input_price_per_1k": "NaN", "effective_from": "2026-07-11T00:00:00Z"},
        )
        developer_write = await client.post(
            "/model-prices",
            headers={"Authorization": f"Bearer {developer_token}"},
            json=valid_payload,
        )
        viewer_read = await client.get(
            "/model-prices",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )

    assert created.status_code == 201, created.text
    assert duplicate_time.status_code == 409
    assert duplicate_time.json()["code"] == "model_price.effective_conflict"
    assert invalid_price.status_code == 422
    assert invalid_price.json()["code"] == "model_price.price_invalid"
    assert developer_write.status_code == 403
    assert developer_write.json()["code"] == "auth.permission_denied"
    assert viewer_read.status_code == 403
    assert viewer_read.json()["code"] == "auth.permission_denied"
