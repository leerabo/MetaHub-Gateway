"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/integration/test_request_log_minimal_loop.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import metahub_gateway.usage.ledger as usage_ledger_service
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_gateway.api.openai_compatible import router as openai_compatible_router
from metahub_gateway.usage.ledger import (
    RequestLogContext,
    _estimate_cost,
    claim_gateway_request,
    safe_record_request_log,
    safe_record_usage_ledger,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试密钥只用于最小日志闭环夹具，验证用量和成本字段可追踪。
VIRTUAL_KEY = "mhv_prd016_virtual_key"
PROVIDER_KEY = "sk-prd016-provider-secret"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_gateway_app():
    """构造只包含 OpenAI-compatible 路由的网关测试应用。"""

    app = create_service_app("gateway-data-plane", "Gateway PRD-016 Test API")
    app.include_router(openai_compatible_router)
    return app


@pytest.fixture()
def fake_upstream():
    """启动一个本地 OpenAI-compatible 假上游，返回可验证的 usage。"""

    captured: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        """记录上游收到的请求，并返回固定 chat completion。"""

        def do_POST(self) -> None:  # noqa: N802
            """实现 网关数据面 的 do POST 逻辑，保持职责、输入边界和错误码口径清晰。
            
            边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
            """

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            captured.append(
                {
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "chatcmpl_prd016",
                        "object": "chat.completion",
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "request log pong"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 4,
                            "completion_tokens": 6,
                            "total_tokens": 10,
                            "cost_cny": "0.0099",
                            "api_key": "sk-provider-raw-usage-secret",
                        },
                    }
                ).encode("utf-8")
            )

        def log_message(self, format: str, *args: object) -> None:
            """关闭测试 HTTP Server 默认访问日志，避免污染 pytest 输出。"""

            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", captured
    finally:
        server.shutdown()
        thread.join(timeout=2)


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider_base_url: str) -> str:
    """准备网关请求日志测试数据库，并设置模型价格用于成本估算。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    # 事务故障用例可能触发 spool 回退；绑定到 tmp_path，确保测试残留不进入开发环境默认目录。
    monkeypatch.setenv("METAHUB_FINOPS_USAGE_SPOOL_ROOT", str(tmp_path / "finops-usage-spool"))
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", provider_base_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=0.1,
            output_price_per_1k=0.2,
            currency='CNY',
            price_schema_version='test-price-v1'
        WHERE tenant_id=? AND model_alias='default-chat'
        """,
        (seed.tenant_id,),
    )
    connection.commit()
    return database_url


@pytest.mark.anyio
async def test_gateway_success_request_writes_request_log_usage_and_cost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主流程：成功调用后 request_logs 可查到路由、用量、成本，并且敏感字段不落库。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": "req_prd016_gateway_success"},
            json={"model": "default-chat", "messages": [{"role": "user", "content": "private prompt"}]},
        )

    assert response.status_code == 200, response.text
    assert captured[-1]["authorization"] == f"Bearer {PROVIDER_KEY}"
    connection = connect_database(database_url)
    row = connection.execute(
        """
        SELECT endpoint, request_model, resolved_model, provider_code, status, error_code,
               input_tokens, output_tokens, total_tokens, estimated_cost_amount, currency, price_schema_version
        FROM request_logs
        WHERE request_id='req_prd016_gateway_success'
        """
    ).fetchone()
    assert row is not None
    assert row["endpoint"] == "/v1/chat/completions"
    assert row["request_model"] == "default-chat"
    assert row["resolved_model"] == "default-chat"
    assert row["provider_code"] == "local_openai"
    assert row["status"] == "success"
    assert row["error_code"] is None
    assert row["input_tokens"] == 4
    assert row["output_tokens"] == 6
    assert row["total_tokens"] == 10
    assert abs(float(row["estimated_cost_amount"]) - 0.0016) < 0.0000001
    assert row["currency"] == "CNY"
    assert row["price_schema_version"] == "test-price-v1"

    usage_row = connection.execute(
        """
        SELECT raw_usage, normalized_usage, cost_amount, cost_cny, currency
        FROM usage_ledger
        WHERE request_id='req_prd016_gateway_success'
        """
    ).fetchone()
    assert usage_row is not None
    raw_usage = json.loads(usage_row["raw_usage"])
    normalized_usage = json.loads(usage_row["normalized_usage"])
    assert raw_usage["api_key"] == "[REDACTED]"
    assert normalized_usage["provider_cost_amount"] == "0.0099"
    assert normalized_usage["provider_cost_currency"] == "CNY"
    assert normalized_usage["provider_cost_cny"] == "0.0099"
    # Provider 自报成本只用于对账；内部 PricePlan 成本仍按 Token 单价独立计算。
    assert abs(float(usage_row["cost_amount"]) - 0.0016) < 0.0000001
    assert abs(float(usage_row["cost_cny"]) - 0.0016) < 0.0000001
    assert usage_row["currency"] == "CNY"

    persisted = "\n".join(
        str(value)
        for table in ["request_logs", "usage_ledger", "routing_decisions"]
        for stored_row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(stored_row)
    )
    assert "private prompt" not in persisted
    assert VIRTUAL_KEY not in persisted
    assert PROVIDER_KEY not in persisted
    assert "sk-provider-raw-usage-secret" not in persisted


def test_price_snapshot_partial_write_rolls_back_closes_and_next_usage_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """早期 Usage 写故障必须回滚 PriceSnapshot、关闭连接，并允许下一请求立即入账。"""

    upstream_url, _captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    app = build_gateway_app()
    setup_connection = connect_database(database_url)
    model = setup_connection.execute(
        """
        SELECT m.tenant_id, m.model_id, m.provider_id, p.provider_code
        FROM models m
        JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        WHERE m.model_alias='default-chat'
        """
    ).fetchone()
    assert model is not None
    failed_context = RequestLogContext(
        tenant_id=model["tenant_id"],
        request_id="req_usage_early_write_failed",
        trace_id="trc_usage_early_write_failed",
        provider_id=model["provider_id"],
        model_id=model["model_id"],
        provider_code=model["provider_code"],
        endpoint="/v1/chat/completions",
        request_model="default-chat",
        resolved_model="default-chat",
    )
    next_context = RequestLogContext(
        tenant_id=model["tenant_id"],
        request_id="req_usage_after_early_write_failed",
        trace_id="trc_usage_after_early_write_failed",
        provider_id=model["provider_id"],
        model_id=model["model_id"],
        provider_code=model["provider_code"],
        endpoint="/v1/chat/completions",
        request_model="default-chat",
        resolved_model="default-chat",
    )
    setup_connection.close()
    for context in (failed_context, next_context):
        claim_gateway_request(app.state.config, context)
        safe_record_request_log(
            app.state.config,
            context,
            status="success",
            error_code=None,
            latency_ms=1,
        )

    class TrackingConnection:
        """代理真实 SQLite 连接，首轮在 PriceSnapshot 后注入 Usage INSERT 故障并记录事务动作。"""

        def __init__(self, inner: sqlite3.Connection, *, fail_usage_insert: bool) -> None:
            """保存真实连接与单次故障开关，计数仅用于证明 rollback/close 边界。"""

            self.inner = inner
            self.fail_usage_insert = fail_usage_insert
            self.commit_calls = 0
            self.rollback_calls = 0
            self.close_calls = 0

        @property
        def in_transaction(self) -> bool:
            """转发额度结算用于决定 savepoint 边界的真实连接状态。"""

            return self.inner.in_transaction

        def execute(self, sql: str, *parameters: object):
            """转发 SQL；首轮 Usage INSERT 前抛固定异常，已完成的 PriceSnapshot 留给 rollback。"""

            # 只在第一条 Usage INSERT 注入，取价查询与 PriceSnapshot INSERT 必须真实执行。
            if self.fail_usage_insert and "INSERT INTO usage_ledger" in sql:
                self.fail_usage_insert = False
                raise sqlite3.OperationalError("SENSITIVE_EARLY_USAGE_SQL_MUST_NOT_LOG")
            return self.inner.execute(sql, *parameters)

        def commit(self) -> None:
            """记录并转发提交，失败轮不得触发，下一成功轮必须精确触发一次。"""

            self.commit_calls += 1
            self.inner.commit()

        def rollback(self) -> None:
            """记录并转发回滚，证明 PriceSnapshot 的部分写不会残留。"""

            self.rollback_calls += 1
            self.inner.rollback()

        def close(self) -> None:
            """记录并关闭底层连接，确保测试持有代理引用时也不会保留 SQLite 写锁。"""

            self.close_calls += 1
            self.inner.close()

    real_connect = usage_ledger_service._connect
    opened: list[TrackingConnection] = []

    def tracked_connect(config):
        """首轮返回故障连接，后续连接正常，用同一数据库验证锁已释放。"""

        tracked = TrackingConnection(
            real_connect(config),
            fail_usage_insert=not opened,
        )
        opened.append(tracked)
        return tracked

    monkeypatch.setattr(usage_ledger_service, "_connect", tracked_connect)
    usage = {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}
    caplog.set_level(logging.INFO, logger="metahub.gateway.usage")

    # safe wrapper 必须吞掉账本故障，模拟 Provider 已成功后的响应语义不被副作用覆盖。
    safe_record_usage_ledger(app.state.config, failed_context, usage)
    assert len(opened) == 1
    assert (opened[0].commit_calls, opened[0].rollback_calls, opened[0].close_calls) == (0, 1, 1)
    verification = connect_database(database_url)
    assert verification.execute(
        "SELECT COUNT(*) FROM price_snapshots WHERE tenant_id=? AND request_id=?",
        (failed_context.tenant_id, failed_context.request_id),
    ).fetchone()[0] == 0
    assert verification.execute(
        "SELECT COUNT(*) FROM usage_ledger WHERE tenant_id=? AND request_id=?",
        (failed_context.tenant_id, failed_context.request_id),
    ).fetchone()[0] == 0
    failed_log = verification.execute(
        """
        SELECT input_tokens, output_tokens, total_tokens, estimated_cost_amount,
               currency, price_schema_version
        FROM request_logs WHERE tenant_id=? AND request_id=?
        """,
        (failed_context.tenant_id, failed_context.request_id),
    ).fetchone()
    assert tuple(failed_log) == (0, 0, 0, 0, "CNY", None)
    verification.close()

    # 保留首轮代理引用仍能立即完成下一事务，证明 close 已真实释放底层写锁。
    safe_record_usage_ledger(app.state.config, next_context, usage)
    assert len(opened) == 2
    assert (opened[1].commit_calls, opened[1].rollback_calls, opened[1].close_calls) == (1, 0, 1)
    verification = connect_database(database_url)
    assert verification.execute(
        "SELECT COUNT(*) FROM price_snapshots WHERE tenant_id=? AND request_id=?",
        (next_context.tenant_id, next_context.request_id),
    ).fetchone()[0] == 1
    assert verification.execute(
        "SELECT COUNT(*) FROM usage_ledger WHERE tenant_id=? AND request_id=?",
        (next_context.tenant_id, next_context.request_id),
    ).fetchone()[0] == 1
    verification.close()
    failed_warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "usage_ledger.write_failed"
        and record.request_id == failed_context.request_id
    ]
    recorded_events = [
        record
        for record in caplog.records
        if record.getMessage() == "usage.recorded"
    ]
    assert len(failed_warnings) == 1
    assert [record.request_id for record in recorded_events] == [next_context.request_id]
    assert "SENSITIVE_EARLY_USAGE_SQL_MUST_NOT_LOG" not in caplog.text
    assert database_url not in caplog.text


@pytest.mark.parametrize(
    ("price_schema_version", "expects_catalog_snapshot"),
    [(None, False), ("catalog-free-v1", True)],
)
@pytest.mark.anyio
async def test_catalog_zero_price_requires_version_for_usage_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    price_schema_version: str | None,
    expects_catalog_snapshot: bool,
) -> None:
    """成本真实性：无版本目录 0 生成缺价证据，带版本目录 0 才是真实免费。

    两种请求都保留零成本和不可变快照；缺价分支必须使用 ``finops_unpriced``，
    不能伪造为带目录版本的免费计划。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=0, output_price_per_1k=0,
            currency='CNY', price_schema_version=?
        WHERE model_alias='default-chat'
        """,
        (price_schema_version,),
    )
    connection.commit()
    case_name = "versioned" if price_schema_version else "missing"
    request_id = f"req_usage_catalog_zero_{case_name}"
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": request_id,
            },
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": "price truth"}],
            },
        )

    assert response.status_code == 200, response.text
    assert len(captured) == 1
    request_log = connection.execute(
        """
        SELECT estimated_cost_amount, currency, price_schema_version
        FROM request_logs WHERE request_id=?
        """,
        (request_id,),
    ).fetchone()
    usage = connection.execute(
        """
        SELECT usage_ledger_id, cost_amount, cost_cny, currency, price_schema_version,
               price_snapshot_id, normalized_usage
        FROM usage_ledger WHERE request_id=?
        """,
        (request_id,),
    ).fetchone()
    snapshot = connection.execute(
        """
        SELECT price_snapshot_id, input_price_per_1k, output_price_per_1k,
               price_schema_version, billing_dimensions, source, status
        FROM price_snapshots WHERE request_id=?
        """,
        (request_id,),
    ).fetchone()
    assert request_log is not None and usage is not None
    assert Decimal(str(request_log["estimated_cost_amount"])) == Decimal("0")
    assert Decimal(str(usage["cost_amount"])) == Decimal("0")
    assert Decimal(str(usage["cost_cny"])) == Decimal("0")
    assert request_log["currency"] == usage["currency"] == "CNY"
    expected_version = price_schema_version or "unpriced-v1"
    assert request_log["price_schema_version"] == expected_version
    assert usage["price_schema_version"] == expected_version
    normalized_usage = json.loads(usage["normalized_usage"])
    assert normalized_usage["price_schema_version"] == expected_version
    assert usage["price_snapshot_id"] is not None
    assert normalized_usage["price_snapshot_id"] == usage["price_snapshot_id"]
    assert snapshot is not None
    assert snapshot["price_snapshot_id"] == usage["price_snapshot_id"]
    assert Decimal(str(snapshot["input_price_per_1k"])) == Decimal("0")
    assert Decimal(str(snapshot["output_price_per_1k"])) == Decimal("0")
    assert snapshot["status"] == "active"

    if expects_catalog_snapshot:
        assert snapshot["price_schema_version"] == "catalog-free-v1"
        assert snapshot["source"] == "model_catalog"
    else:
        assert snapshot["price_schema_version"] == "unpriced-v1"
        assert snapshot["source"] == "finops_unpriced"
        dimensions = json.loads(snapshot["billing_dimensions"])
        assert dimensions == {
            "finops_usage_ledger_id": usage["usage_ledger_id"],
            "price_evidence_status": "unpriced",
        }


@pytest.mark.parametrize(
    ("case_name", "invalid_price", "use_price_plan"),
    [
        ("catalog_text", "not-a-decimal", False),
        ("catalog_over_limit", "1000000000.000001", False),
        ("catalog_overprecision", "0.0000000000001", False),
        ("plan_extreme", "1e100", True),
    ],
)
def test_versioned_broken_catalog_price_creates_unpriced_evidence_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    caplog: pytest.LogCaptureFixture,
    case_name: str,
    invalid_price: str,
    use_price_plan: bool,
) -> None:
    """异常流程：目录价或 PricePlan 损坏时创建明确的缺价证据快照。

    私有估价函数仍遵守生产 owner 边界，测试必须先建立同租户 Request Log。
    """

    upstream_url, _captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    model = connection.execute(
        """
        SELECT m.tenant_id, m.model_id, m.provider_id, p.provider_code
        FROM models m
        LEFT JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        WHERE m.model_alias='default-chat'
        """
    ).fetchone()
    assert model is not None
    if use_price_plan:
        # 价格管理 API 已拒绝极端值；直接构造损坏存量，证明 Usage 读取仍会防御性降级。
        connection.execute(
            """
            INSERT INTO model_price_plans(
              price_plan_id, tenant_id, model_id, input_price_per_1k,
              output_price_per_1k, currency, price_schema_version,
              effective_from, source, created_at
            )
            VALUES(?, ?, ?, ?, 0, 'CNY', ?, '2026-07-18T00:00:00Z',
                   'manual', '2026-07-18T00:00:00Z')
            """,
            (
                f"prp_usage_invalid_{case_name}",
                model["tenant_id"],
                model["model_id"],
                invalid_price,
                f"broken-{case_name}-v1",
            ),
        )
    else:
        connection.execute(
            """
            UPDATE models
            SET input_price_per_1k=?, output_price_per_1k=0,
                currency='CNY', price_schema_version=?
            WHERE tenant_id=? AND model_id=?
            """,
            (
                invalid_price,
                f"broken-{case_name}-v1",
                model["tenant_id"],
                model["model_id"],
            ),
        )
    connection.commit()
    context = RequestLogContext(
        tenant_id=model["tenant_id"],
        request_id=f"req_usage_broken_{case_name}",
        trace_id=f"trc_usage_broken_{case_name}",
        provider_id=model["provider_id"],
        model_id=model["model_id"],
        provider_code=model["provider_code"],
        endpoint="/v1/chat/completions",
        request_model="default-chat",
        resolved_model="default-chat",
    )
    app = build_gateway_app()
    claim_gateway_request(app.state.config, context)
    # 生产路径会先持久化 Request Log；直接估价测试也必须满足 0041 快照 owner trigger。
    safe_record_request_log(
        app.state.config,
        context,
        status="success",
        error_code=None,
        latency_ms=1,
    )

    with caplog.at_level(logging.WARNING, logger="metahub.gateway.usage"):
        estimate = _estimate_cost(
            connection,
            context,
            input_tokens=4,
            output_tokens=6,
            now="2026-07-19T00:00:00Z",
        )

    snapshot = connection.execute(
        """
        SELECT price_snapshot_id, input_price_per_1k, output_price_per_1k,
               price_schema_version, billing_dimensions, source, status
        FROM price_snapshots WHERE tenant_id=? AND request_id=?
        """,
        (context.tenant_id, context.request_id),
    ).fetchone()
    assert estimate.amount == estimate.cost_cny == estimate.cost_usd == Decimal("0")
    assert estimate.currency == "CNY"
    assert estimate.price_schema_version == "unpriced-v1"
    assert estimate.price_snapshot_id is not None
    assert snapshot is not None
    assert snapshot["price_snapshot_id"] == estimate.price_snapshot_id
    assert Decimal(str(snapshot["input_price_per_1k"])) == Decimal("0")
    assert Decimal(str(snapshot["output_price_per_1k"])) == Decimal("0")
    assert snapshot["price_schema_version"] == "unpriced-v1"
    assert (snapshot["source"], snapshot["status"]) == ("finops_unpriced", "active")
    dimensions = json.loads(snapshot["billing_dimensions"])
    assert dimensions["price_evidence_status"] == "unpriced"
    assert dimensions["finops_usage_ledger_id"].startswith("usg_")
    assert "usage.price_configuration_invalid" in caplog.text
    assert invalid_price not in caplog.text


@pytest.mark.anyio
async def test_extreme_versioned_price_does_not_break_successful_gateway_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """运行回归：极端版本价降级为缺价证据，不得把成功上游请求转换为 500。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=?, output_price_per_1k=0,
            currency='CNY', price_schema_version='extreme-catalog-v1'
        WHERE model_alias='default-chat'
        """,
        ("1e100",),
    )
    connection.commit()
    app = build_gateway_app()

    with caplog.at_level(logging.WARNING, logger="metahub.gateway.usage"):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://gateway",
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {VIRTUAL_KEY}",
                    "X-Request-ID": "req_usage_extreme_catalog_price",
                },
                json={
                    "model": "default-chat",
                    "messages": [{"role": "user", "content": "extreme price"}],
                },
            )

    assert response.status_code == 200, response.text
    assert len(captured) == 1
    usage = connection.execute(
        """
        SELECT usage_ledger_id, cost_amount, cost_cny, price_schema_version,
               price_snapshot_id, normalized_usage
        FROM usage_ledger WHERE request_id='req_usage_extreme_catalog_price'
        """
    ).fetchone()
    snapshot = connection.execute(
        """
        SELECT price_snapshot_id, input_price_per_1k, output_price_per_1k,
               price_schema_version, billing_dimensions, source, status
        FROM price_snapshots WHERE request_id='req_usage_extreme_catalog_price'
        """
    ).fetchone()
    assert usage is not None
    assert Decimal(str(usage["cost_amount"])) == Decimal("0")
    assert Decimal(str(usage["cost_cny"])) == Decimal("0")
    assert usage["price_schema_version"] == "unpriced-v1"
    assert usage["price_snapshot_id"] is not None
    normalized_usage = json.loads(usage["normalized_usage"])
    assert normalized_usage["price_schema_version"] == "unpriced-v1"
    assert normalized_usage["price_snapshot_id"] == usage["price_snapshot_id"]
    assert snapshot is not None
    assert snapshot["price_snapshot_id"] == usage["price_snapshot_id"]
    assert Decimal(str(snapshot["input_price_per_1k"])) == Decimal("0")
    assert Decimal(str(snapshot["output_price_per_1k"])) == Decimal("0")
    assert snapshot["price_schema_version"] == "unpriced-v1"
    assert (snapshot["source"], snapshot["status"]) == ("finops_unpriced", "active")
    assert json.loads(snapshot["billing_dimensions"]) == {
        "finops_usage_ledger_id": usage["usage_ledger_id"],
        "price_evidence_status": "unpriced",
    }
    assert "usage.price_configuration_invalid" in caplog.text
    assert "1e100" not in caplog.text


@pytest.mark.anyio
async def test_gateway_failed_request_is_logged_before_routing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """异常流程：请求体验证失败时也写入失败日志，并保留安全的 request_model。"""

    upstream_url, _captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": "req_prd016_gateway_failed"},
            json={"model": "default-chat"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "gateway.messages_missing"
    row = connect_database(database_url).execute(
        """
        SELECT endpoint, request_model, resolved_model, provider_code, status, error_code
        FROM request_logs
        WHERE request_id='req_prd016_gateway_failed'
        """
    ).fetchone()
    assert row is not None
    assert row["endpoint"] == "/v1/chat/completions"
    assert row["request_model"] == "default-chat"
    assert row["resolved_model"] is None
    assert row["provider_code"] is None
    assert row["status"] == "failed"
    assert row["error_code"] == "gateway.messages_missing"


def test_request_log_write_failure_is_warning_not_gateway_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """异常流程：日志库写入失败时记录 warning，不把异常抛回网关热路径。"""

    upstream_url, _captured = fake_upstream
    prepare_database(monkeypatch, tmp_path, upstream_url)
    app = build_gateway_app()
    bad_config = app.state.config.model_copy(update={"database_url": "postgresql://unsupported/metahub"})

    with caplog.at_level(logging.WARNING, logger="metahub.gateway.usage"):
        safe_record_request_log(
            bad_config,
            RequestLogContext(
                tenant_id="tenant_prd016",
                request_id="req_prd016_write_failed",
                trace_id="trc_prd016_write_failed",
                endpoint="/v1/chat/completions",
                request_model="default-chat",
            ),
            status="failed",
            error_code="gateway.upstream_error",
            latency_ms=12,
        )

    assert "request_log.write_failed" in caplog.text
