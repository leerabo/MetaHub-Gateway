"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/integration/test_model_alias_routing.py
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import metahub_gateway.api.openai_compatible as openai_api
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_gateway.api.openai_compatible import router as openai_compatible_router
from metahub_gateway.providers import keypool as provider_keypool
from metahub_gateway.routing import models as routing_models
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试密钥只用于模型别名路由夹具，验证 tenant_id 内的路由解析。
VIRTUAL_KEY = "mhv_prd015_virtual_key"
PROVIDER_KEY = "sk-prd015-provider-secret"


def sqlite_url(path: Path) -> str:
    """sqlite_url：说明该定义的测试或脚本职责。

    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return f"sqlite:///{path.as_posix()}"


def build_gateway_app():
    """build_gateway_app：说明该定义的测试或脚本职责。

    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    app = create_service_app("gateway-data-plane", "Gateway PRD-015 Test API")
    app.include_router(openai_compatible_router)
    return app


@pytest.fixture()
def fake_upstream():
    """fake_upstream：说明该定义的测试或脚本职责。

    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    captured: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        """Handler：说明该定义的测试或脚本职责。

        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        def do_POST(self) -> None:  # noqa: N802
            """do_POST：说明该定义的测试或脚本职责。

            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
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
            # 只有测试显式配置的模型返回 500，用于验证动态初始选择与 PRD-036
            # 请求后 Fallback 的边界；其他既有 Alias 用例继续复用成功响应。
            if body.get("model") == "prd047-upstream-error":
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"controlled upstream failure"}}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "chatcmpl_prd015",
                        "object": "chat.completion",
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "alias pong"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
                    }
                ).encode("utf-8")
            )

        def log_message(self, format: str, *args: object) -> None:
            """log_message：说明该定义的测试或脚本职责。

            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

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


def prepare_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider_base_url: str
) -> str:
    """prepare_database：说明该定义的测试或脚本职责。

    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", provider_base_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (seed.tenant_id,),
    ).fetchone()
    default_model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (seed.tenant_id,),
    ).fetchone()
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (seed.tenant_id,),
    ).fetchone()
    assert provider is not None
    assert default_model is not None
    assert environment is not None
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name, provider_model_id,
          display_name, capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd015_fallback', ?, ?, 'fallback-chat', 'fallback-upstream',
          'fallback-upstream', 'Fallback Chat', '{"chat": true, "stream": true}',
          'active', datetime('now'), datetime('now')
        )
        """,
        (seed.tenant_id, provider["provider_id"]),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status, description, created_at, updated_at
        )
        VALUES(
          'alias_prd015_company_default', ?, 'company-default', 'dev', ?, ?,
          'model_prd015_fallback', 'active', 'prd015 integration alias',
          datetime('now'), datetime('now')
        )
        """,
        (seed.tenant_id, environment["environment_id"], default_model["model_id"]),
    )
    connection.commit()
    return database_url


@pytest.mark.anyio
async def test_model_alias_routes_to_default_model_and_logs_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主流程：调用稳定别名时，数据面解析到默认真实模型并记录路由决策。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd015_alias_default",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "private prompt"}],
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "alias pong"
    assert captured[-1]["body"]["model"] == "local-chat-model"
    assert captured[-1]["authorization"] == f"Bearer {PROVIDER_KEY}"

    connection = connect_database(database_url)
    decision = connection.execute(
        """
        SELECT requested_model, selected_internal_model_id, provider_code, decision_status, decision_reason
        FROM routing_decisions
        WHERE request_id='req_prd015_alias_default'
        """
    ).fetchone()
    assert decision["requested_model"] == "company-default"
    assert decision["selected_internal_model_id"] == "default-chat"
    assert decision["provider_code"] == "local_openai"
    assert decision["decision_status"] == "selected"
    assert decision["decision_reason"] == "alias.default"

    persisted = "\n".join(
        str(value)
        for table in ["routing_decisions", "request_logs", "usage_ledger"]
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
    )
    assert "private prompt" not in persisted
    assert VIRTUAL_KEY not in persisted
    assert PROVIDER_KEY not in persisted


def update_routing_mode(connection, mode: str) -> None:
    """测试夹具按数据库 CAS 合同推进 RoutingPolicy 模式和精确版本。"""

    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode=?, version=version+1, updated_at=datetime('now')
        WHERE model_alias_id='alias_prd015_company_default'
        """,
        (mode,),
    )
    connection.commit()


def configure_quality_cost(
    connection: sqlite3.Connection,
    *,
    quality_weight_bps: int = 5_000,
    cost_weight_bps: int = 3_000,
    latency_weight_bps: int = 2_000,
    quality_floor_bps: int = 6_000,
    minimum_sample_count: int = 20,
) -> None:
    """按真实 Policy 版本触发器启用 ``quality_cost`` 并保存完整配置。

    参数：测试数据库连接及五个强类型策略字段。返回值：无，函数内提交。
    关键约束：不删除触发器或直接改版本，确保测试配置与 Control API 最终落库形态一致。
    """

    cursor = connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode='quality_cost',
            quality_weight_bps=?, cost_weight_bps=?, latency_weight_bps=?,
            quality_floor_bps=?, minimum_sample_count=?,
            version=version+1, updated_at=datetime('now')
        WHERE model_alias_id='alias_prd015_company_default'
        """,
        (
            quality_weight_bps,
            cost_weight_bps,
            latency_weight_bps,
            quality_floor_bps,
            minimum_sample_count,
        ),
    )
    assert cursor.rowcount == 1
    connection.commit()


def set_model_quality_fact(
    connection: sqlite3.Connection,
    *,
    model_alias: str,
    manual_quality_bps: int | None = None,
    latency_ms: int | None = None,
) -> str:
    """通过正式 Score 转换触发器设置人工先验，并可追加一个成功延迟样本。

    参数：内部模型别名、可空人工质量分和可空毫秒延迟。返回：该租户模型 ID。
    关键约束：人工分推进 feedback_version；样本独立推进 statistics_version，不能混写。
    """

    model = connection.execute(
        """
        SELECT model_id, tenant_id FROM models
        WHERE model_alias=?
        """,
        (model_alias,),
    ).fetchone()
    assert model is not None
    actor = connection.execute(
        """
        SELECT user_id FROM users
        WHERE tenant_id=? ORDER BY created_at, user_id LIMIT 1
        """,
        (model["tenant_id"],),
    ).fetchone()
    assert actor is not None
    if manual_quality_bps is not None:
        # 人工先验必须带同租户 actor 和独立反馈版本，不能伪造成自动调用统计。
        cursor = connection.execute(
            """
            UPDATE model_quality_scores
            SET manual_quality_bps=?, feedback_version=feedback_version+1,
                feedback_updated_by=?, feedback_updated_at=datetime('now'),
                updated_at=datetime('now')
            WHERE tenant_id=? AND model_id=?
            """,
            (
                manual_quality_bps,
                actor["user_id"],
                model["tenant_id"],
                model["model_id"],
            ),
        )
        assert cursor.rowcount == 1
    if latency_ms is not None:
        score = connection.execute(
            """
            SELECT updated_at FROM model_quality_scores
            WHERE tenant_id=? AND model_id=?
            """,
            (model["tenant_id"], model["model_id"]),
        ).fetchone()
        assert score is not None
        baseline = datetime.fromisoformat(str(score["updated_at"]).replace("Z", "+00:00"))
        if baseline.tzinfo is None:
            # SQLite datetime('now') 是无时区 UTC 文本；测试显式恢复 UTC 后再构造未来样本。
            baseline = baseline.replace(tzinfo=UTC)
        observed_at = (
            baseline.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        # 单成功样本同时推进计数、统计版本和延迟聚合，保持迁移约束可验证。
        cursor = connection.execute(
            """
            UPDATE model_quality_scores
            SET sample_count=sample_count+1,
                success_count=success_count+1,
                latency_sample_count=latency_sample_count+1,
                total_latency_ms=total_latency_ms+?,
                statistics_version=statistics_version+1,
                first_observed_at=COALESCE(first_observed_at, ?),
                last_observed_at=?, updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (
                latency_ms,
                observed_at,
                observed_at,
                observed_at,
                model["tenant_id"],
                model["model_id"],
            ),
        )
        assert cursor.rowcount == 1
    connection.commit()
    return str(model["model_id"])


def enable_runtime_fallback(
    connection: sqlite3.Connection,
    *,
    policy_id: str,
) -> None:
    """为现有测试 Alias 启用一次性 PRD-036 Fallback 策略。

    参数：已开启外键的测试连接和当前数据库内唯一策略 ID。
    返回值：无；策略在函数内提交。
    关键约束：只允许三类已归一化上游故障触发，避免测试用宽泛重试掩盖动态
    RoutingCandidate 是否真正处于 ``standby``。
    """

    connection.execute(
        """
        INSERT INTO fallback_policies(
          fallback_policy_id, tenant_id, model_alias_id, status, version, priority,
          condition_expr, action, created_at, updated_at
        )
        SELECT ?, tenant_id, model_alias_id, 'enabled', 1, 100,
               '["upstream_timeout","upstream_rate_limited","upstream_5xx"]',
               '{"type":"use_alias_fallback","max_attempts":1}',
               datetime('now'), datetime('now')
        FROM model_aliases
        WHERE model_alias_id='alias_prd015_company_default'
        """,
        (policy_id,),
    )
    connection.commit()


@pytest.mark.anyio
async def test_quality_cost_selects_higher_quality_expensive_model_and_freezes_explanation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主流程：高质量高价格模型按配置胜出，并冻结完整 Decision/Candidate 解释。

    参数：真实迁移数据库、Gateway ASGI 链路和本地双模型上游。返回值：无。
    关键约束：质量、价格和 Token 来自同一事务；三位延迟、双版本和分量必须可复现。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k='9.000000000000', output_price_per_1k='9.000000000000',
            currency='CNY', price_schema_version='prd048-expensive-v1'
        WHERE model_alias='default-chat'
        """
    )
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k='1.000000000000', output_price_per_1k='1.000000000000',
            currency='CNY', price_schema_version='prd048-cheap-v1'
        WHERE model_alias='fallback-chat'
        """
    )
    set_model_quality_fact(
        connection,
        model_alias="default-chat",
        manual_quality_bps=9_500,
        latency_ms=50,
    )
    set_model_quality_fact(
        connection,
        model_alias="fallback-chat",
        manual_quality_bps=6_000,
        latency_ms=100,
    )
    configure_quality_cost(
        connection,
        quality_weight_bps=8_000,
        cost_weight_bps=2_000,
        latency_weight_bps=0,
        quality_floor_bps=5_000,
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd048_quality_wins",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "1234"}],
                "max_completion_tokens": 4,
            },
        )

    assert response.status_code == 200, response.text
    assert [item["body"]["model"] for item in captured] == ["local-chat-model"]
    decision = connection.execute(
        """
        SELECT routing_decision_id, routing_mode, routing_policy_version,
               decision_reason, quality_weight_bps, cost_weight_bps,
               latency_weight_bps, quality_floor_bps, minimum_sample_count,
               quality_as_of, quality_formula_version, quality_scoring_performed,
               price_as_of, estimated_input_tokens, estimated_output_tokens
        FROM routing_decisions WHERE request_id='req_prd048_quality_wins'
        """
    ).fetchone()
    assert decision is not None
    assert tuple(decision)[1:] == (
        "quality_cost",
        2,
        "routing.quality_cost_highest_score",
        8_000,
        2_000,
        0,
        5_000,
        20,
        decision["quality_as_of"],
        "quality_cost_v1",
        1,
        decision["price_as_of"],
        1,
        4,
    )
    assert decision["quality_as_of"] is not None
    assert decision["price_as_of"] == decision["quality_as_of"]
    candidates = connection.execute(
        """
        SELECT candidate_role, rank, status, quality_evidence_state,
               quality_sample_count, quality_success_count, quality_error_count,
               manual_quality_bps, quality_statistics_version, quality_feedback_version,
               average_latency_ms, quality_component_bps, cost_component_bps,
               latency_component_bps, weighted_score_bps, price_source
        FROM routing_candidates
        WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert [tuple(row) for row in candidates] == [
        (
            "default",
            1,
            "selected",
            "ready",
            1,
            1,
            0,
            9_500,
            1,
            2,
            "50.000",
            9_524,
            1_111,
            None,
            7_841,
            "model_catalog",
        ),
        (
            "fallback",
            2,
            "standby",
            "ready",
            1,
            1,
            0,
            6_000,
            1,
            2,
            "100.000",
            6_190,
            10_000,
            None,
            6_952,
            "model_catalog",
        ),
    ]


@pytest.mark.anyio
async def test_quality_cost_all_cold_uses_priority_without_token_or_price(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """双 cold 按配置优先级启动，并冻结质量事实但完全跳过正文和价格读取。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    configure_quality_cost(connection)

    def reject_deferred_dependency(*_args, **_kwargs):
        """若 cold 路径读取 Token 或价格则立即失败，证明依赖最小化合同。"""

        # cold 没有足够证据进入加权，任何正文或价格读取都属于隐私和性能回归。
        raise AssertionError("cold route must not read token or price")

    monkeypatch.setattr(routing_models, "estimate_request_tokens", reject_deferred_dependency)
    monkeypatch.setattr(routing_models, "_load_routing_prices", reject_deferred_dependency)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd048_all_cold",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "cold"}]},
        )

    assert response.status_code == 200, response.text
    assert [item["body"]["model"] for item in captured] == ["local-chat-model"]
    decision = connection.execute(
        """
        SELECT routing_decision_id, decision_reason, quality_scoring_performed,
               quality_formula_version, price_as_of, estimated_input_tokens
        FROM routing_decisions WHERE request_id='req_prd048_all_cold'
        """
    ).fetchone()
    assert tuple(decision)[1:] == (
        "routing.quality_cost_cold_start_priority",
        0,
        "quality_cost_v1",
        None,
        None,
    )
    candidates = connection.execute(
        """
        SELECT candidate_role, rank, status, reason_code, quality_evidence_state,
               quality_component_bps, average_latency_ms, price_source,
               cost_component_bps, latency_component_bps, weighted_score_bps
        FROM routing_candidates WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert [tuple(row) for row in candidates] == [
        (
            "default",
            1,
            "selected",
            "routing.quality_cost_cold_start_priority",
            "cold",
            5_000,
            None,
            "not_evaluated",
            None,
            None,
            None,
        ),
        (
            "fallback",
            2,
            "standby",
            "routing.candidate.standby",
            "cold",
            5_000,
            None,
            "not_evaluated",
            None,
            None,
            None,
        ),
    ]


@pytest.mark.anyio
async def test_quality_cost_ready_candidate_precedes_cold_without_cost_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """fallback 有人工证据时压过 default cold，且 cold 不能因价格便宜抢占流量。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    set_model_quality_fact(
        connection,
        model_alias="fallback-chat",
        manual_quality_bps=9_000,
    )
    configure_quality_cost(connection)

    def reject_cost_dependency(*_args, **_kwargs):
        """ready/cold 分流不允许进入成本阶段，回调触发即代表实现回归。"""

        # 证据优先级已经产生唯一顺序，继续读取正文或价格会扩大敏感数据接触面。
        raise AssertionError("evidence priority must not read cost dependencies")

    monkeypatch.setattr(routing_models, "estimate_request_tokens", reject_cost_dependency)
    monkeypatch.setattr(routing_models, "_load_routing_prices", reject_cost_dependency)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd048_ready_over_cold",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "ready"}]},
        )

    assert response.status_code == 200, response.text
    assert [item["body"]["model"] for item in captured] == ["fallback-upstream"]
    decision = connection.execute(
        """
        SELECT routing_decision_id, decision_reason, quality_scoring_performed
        FROM routing_decisions WHERE request_id='req_prd048_ready_over_cold'
        """
    ).fetchone()
    assert tuple(decision)[1:] == ("routing.quality_cost_evidence_priority", 0)
    candidates = connection.execute(
        """
        SELECT candidate_role, rank, status, reason_code, quality_evidence_state,
               quality_component_bps, cost_component_bps, weighted_score_bps
        FROM routing_candidates WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert [tuple(row) for row in candidates] == [
        (
            "default",
            2,
            "standby",
            "routing.candidate.quality_cold_start_deferred",
            "cold",
            5_000,
            None,
            None,
        ),
        (
            "fallback",
            1,
            "selected",
            "routing.quality_cost_evidence_priority",
            "ready",
            9_000,
            None,
            None,
        ),
    ]


@pytest.mark.anyio
async def test_quality_cost_all_low_rejects_before_provider_and_records_failed_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """两条 ready-low 全部拒绝并返回统一 no-route，绝不尝试任何上游调用。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    set_model_quality_fact(connection, model_alias="default-chat", manual_quality_bps=1_000)
    set_model_quality_fact(connection, model_alias="fallback-chat", manual_quality_bps=2_000)
    configure_quality_cost(connection, quality_floor_bps=6_000)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd048_all_low",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "blocked"}]},
        )

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "gateway.no_route_available"
    assert response.json()["request_id"] == "req_prd048_all_low"
    assert captured == []
    decision = connection.execute(
        """
        SELECT routing_decision_id, decision_status, decision_reason, error_code,
               routing_policy_event_id, quality_scoring_performed
        FROM routing_decisions WHERE request_id='req_prd048_all_low'
        """
    ).fetchone()
    assert tuple(decision)[1:] == (
        "failed",
        "routing.no_available_candidate",
        "gateway.no_route_available",
        None,
        0,
    )
    candidates = connection.execute(
        """
        SELECT candidate_role, rank, status, reason_code, quality_evidence_state,
               quality_component_bps, cost_component_bps, latency_component_bps,
               weighted_score_bps, price_source
        FROM routing_candidates WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert [tuple(row) for row in candidates] == [
        (
            "default",
            None,
            "rejected",
            "routing.candidate.quality_below_floor",
            "ready",
            1_000,
            None,
            None,
            None,
            "not_evaluated",
        ),
        (
            "fallback",
            None,
            "rejected",
            "routing.candidate.quality_below_floor",
            "ready",
            2_000,
            None,
            None,
            None,
            "not_evaluated",
        ),
    ]


@pytest.mark.anyio
async def test_quality_cost_cold_fallback_never_runs_after_ready_default_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """cold fallback 虽可排 standby，却不得在 ready default 上游失败后被重新激活。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET upstream_model_name='prd047-upstream-error',
            provider_model_id='prd047-upstream-error'
        WHERE model_alias='default-chat'
        """
    )
    set_model_quality_fact(connection, model_alias="default-chat", manual_quality_bps=9_000)
    enable_runtime_fallback(connection, policy_id="policy_prd048_cold_fallback")
    configure_quality_cost(connection)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd048_cold_fallback",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "fail"}]},
        )

    assert response.status_code == 502, response.text
    assert response.json()["code"] == "gateway.upstream_error"
    assert [item["body"]["model"] for item in captured] == ["prd047-upstream-error"]
    attempts = connection.execute(
        """
        SELECT attempt_sequence, attempt_role, status FROM routing_attempts
        WHERE request_id='req_prd048_cold_fallback'
        """
    ).fetchall()
    assert [tuple(row) for row in attempts] == [(1, "primary", "failed")]
    request_log = connection.execute(
        """
        SELECT fallback_triggered, routing_attempt_count FROM request_logs
        WHERE request_id='req_prd048_cold_fallback'
        """
    ).fetchone()
    assert tuple(request_log) == (0, 1)
    candidates = connection.execute(
        """
        SELECT candidate_role, status, reason_code, quality_evidence_state
        FROM routing_candidates c
        JOIN routing_decisions d ON d.routing_decision_id=c.routing_decision_id
        WHERE d.request_id='req_prd048_cold_fallback'
        ORDER BY configured_priority
        """
    ).fetchall()
    assert [tuple(row) for row in candidates] == [
        (
            "default",
            "selected",
            "routing.quality_cost_evidence_priority",
            "ready",
        ),
        (
            "fallback",
            "standby",
            "routing.candidate.quality_cold_start_deferred",
            "cold",
        ),
    ]


@pytest.mark.parametrize("snapshot_case", ["missing", "corrupt"])
@pytest.mark.anyio
async def test_quality_cost_score_snapshot_missing_or_corrupt_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    snapshot_case: str,
) -> None:
    """Score 缺行或版本计数损坏统一返回快照无效，并停在 Provider 调用之前。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    configure_quality_cost(connection)
    original_loader = routing_models._load_routing_quality_facts

    def broken_quality_loader(*args, **kwargs):
        """读取真实同租户 Score 后，构造缺行或双版本漂移的受控坏快照。"""

        facts = original_loader(*args, **kwargs)
        if snapshot_case == "missing":
            # 缺少一个候选 Score 不能被 shared 当作默认 cold 后继续放行。
            return facts[:-1]
        # statistics_version 与 sample_count 漂移代表跨事务拼接，必须失败关闭。
        return (replace(facts[0], statistics_version=facts[0].sample_count + 1), *facts[1:])

    monkeypatch.setattr(routing_models, "_load_routing_quality_facts", broken_quality_loader)
    request_id = f"req_prd048_score_{snapshot_case}"
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": request_id,
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "safe"}]},
        )

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "gateway.routing_snapshot_invalid"
    assert response.json()["request_id"] == request_id
    assert captured == []
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM routing_decisions WHERE request_id=?",
            (request_id,),
        ).fetchone()[0]
        == 0
    )


@pytest.mark.anyio
async def test_cost_first_selects_lower_estimated_cost_and_persists_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """成本优先只在两候选合格时估算 Token，并原子保存策略、价格和候选解释。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k='9.000000000000', output_price_per_1k='9.000000000000',
            currency='CNY', price_schema_version='catalog-v1'
        WHERE model_alias='default-chat'
        """
    )
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k='1.000000000000', output_price_per_1k='1.000000000000',
            currency='CNY', price_schema_version='catalog-v1'
        WHERE model_alias='fallback-chat'
        """
    )
    update_routing_mode(connection, "cost_first")
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_cost_first",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "12345678"}],
                "max_completion_tokens": 8,
            },
        )

    assert response.status_code == 200, response.text
    assert captured[-1]["body"]["model"] == "fallback-upstream"
    decision = connection.execute(
        """
        SELECT routing_decision_id, routing_mode, routing_policy_version, decision_reason,
               estimated_input_tokens, estimated_output_tokens, routing_policy_event_id
        FROM routing_decisions WHERE request_id='req_prd047_cost_first'
        """
    ).fetchone()
    assert decision["routing_mode"] == "cost_first"
    assert decision["routing_policy_version"] == 2
    assert decision["decision_reason"] == "routing.cost_lowest"
    assert decision["estimated_input_tokens"] == 2
    assert decision["estimated_output_tokens"] == 8
    assert str(decision["routing_policy_event_id"]).startswith("rpe_")
    candidates = connection.execute(
        """
        SELECT candidate_role, rank, status, price_source, estimated_cost_amount
        FROM routing_candidates
        WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert [(row["candidate_role"], row["rank"], row["status"]) for row in candidates] == [
        ("default", 2, "standby"),
        ("fallback", 1, "selected"),
    ]
    assert all(row["price_source"] == "model_catalog" for row in candidates)
    assert all(len(str(row["estimated_cost_amount"]).partition(".")[2]) == 15 for row in candidates)


@pytest.mark.parametrize(
    (
        "case_name",
        "default_price",
        "default_currency",
        "default_version",
        "fallback_price",
        "fallback_currency",
        "fallback_version",
        "expected_upstream_model",
        "expected_reason",
        "expected_sources",
    ),
    [
        (
            "missing",
            "0.000000000000",
            "CNY",
            None,
            "1.000000000000",
            "CNY",
            "catalog-v1",
            "local-chat-model",
            "routing.cost_fixed_fallback.price_missing",
            ("missing", "model_catalog"),
        ),
        (
            "mixed_currency",
            "9.000000000000",
            "CNY",
            "catalog-cny-v1",
            "0.100000000000",
            "USD",
            "catalog-usd-v1",
            "local-chat-model",
            "routing.cost_fixed_fallback.currency_incomparable",
            ("model_catalog", "model_catalog"),
        ),
        (
            "explicit_zero",
            "1.000000000000",
            "CNY",
            "catalog-paid-v1",
            "0.000000000000",
            "CNY",
            "catalog-free-v1",
            "fallback-upstream",
            "routing.cost_lowest",
            ("model_catalog", "model_catalog"),
        ),
    ],
)
@pytest.mark.anyio
async def test_cost_first_gateway_handles_missing_mixed_and_explicit_zero_prices(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    case_name: str,
    default_price: str,
    default_currency: str,
    default_version: str | None,
    fallback_price: str,
    fallback_currency: str,
    fallback_version: str | None,
    expected_upstream_model: str,
    expected_reason: str,
    expected_sources: tuple[str, str],
) -> None:
    """在真实 Gateway 链路锁定缺价、混币和显式零价的确定性选择。

    参数：三组目录价格向量及其预期模型、原因码和候选价格来源。
    返回值：无，直接断言 HTTP、上游调用与不可变 Decision/Candidate 事实。
    关键约束：无版本的目录零价是缺价；跨币种不做数值比较；有版本零价才
    是真实免费。三种情况均必须保留 request_id 且不得绕过 Provider 前的事实提交。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=?, output_price_per_1k=?, currency=?,
            price_schema_version=?
        WHERE model_alias='default-chat'
        """,
        (default_price, default_price, default_currency, default_version),
    )
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=?, output_price_per_1k=?, currency=?,
            price_schema_version=?
        WHERE model_alias='fallback-chat'
        """,
        (fallback_price, fallback_price, fallback_currency, fallback_version),
    )
    update_routing_mode(connection, "cost_first")
    request_id = f"req_prd047_cost_{case_name}"
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": request_id,
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "cost boundary"}],
                "max_completion_tokens": 4,
            },
        )

    assert response.status_code == 200, response.text
    assert captured == [
        {
            "path": "/v1/chat/completions",
            "body": {
                "model": expected_upstream_model,
                "messages": [{"role": "user", "content": "cost boundary"}],
                "max_completion_tokens": 4,
            },
            "authorization": f"Bearer {PROVIDER_KEY}",
        }
    ]
    decision = connection.execute(
        """
        SELECT routing_decision_id, decision_reason, price_as_of,
               estimated_input_tokens, estimated_output_tokens
        FROM routing_decisions WHERE request_id=?
        """,
        (request_id,),
    ).fetchone()
    assert decision is not None
    assert decision["decision_reason"] == expected_reason
    assert decision["price_as_of"] is not None
    assert decision["estimated_input_tokens"] is not None
    assert decision["estimated_output_tokens"] == 4
    candidates = connection.execute(
        """
        SELECT candidate_role, status, price_source, estimated_cost_amount
        FROM routing_candidates
        WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert tuple(row["price_source"] for row in candidates) == expected_sources
    assert sum(row["status"] == "selected" for row in candidates) == 1
    if case_name == "explicit_zero":
        fallback = next(row for row in candidates if row["candidate_role"] == "fallback")
        assert fallback["status"] == "selected"
        assert fallback["estimated_cost_amount"] == "0.000000000000000"


@pytest.mark.anyio
async def test_fixed_routing_never_reads_keypool_price_or_token_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """fixed 模式即使 KeyPool、价格和 Token 路由依赖故障也保持黄金路径。

    参数：真实 SQLite/Gateway fixture，并把三项不应读取的依赖替换为失败函数。
    返回值：无，断言成功调用和 Decision/Candidate 的 NULL/not_evaluated 事实。
    关键约束：这里只约束 PRD-047 初始排序；实际 Provider Key 选择仍按既有调用阶段执行，
    不能因测试替身绕过真实上游鉴权。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)

    def reject_unexpected_dependency(*_args, **_kwargs):
        """任何 fixed 初始排序依赖调用都表示最小读取合同被破坏。"""

        # 立即失败让测试无法被当前依赖恰好健康所掩盖；异常正文不会进入业务链路。
        raise AssertionError("fixed routing dependency must not be read")

    monkeypatch.setattr(
        provider_keypool,
        "snapshot_provider_key_pool",
        reject_unexpected_dependency,
    )
    monkeypatch.setattr(
        routing_models,
        "_load_routing_prices",
        reject_unexpected_dependency,
    )
    monkeypatch.setattr(
        routing_models,
        "estimate_request_tokens",
        reject_unexpected_dependency,
    )
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_fixed_minimal_dependencies",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "fixed"}]},
        )

    assert response.status_code == 200, response.text
    assert [item["body"]["model"] for item in captured] == ["local-chat-model"]
    connection = connect_database(database_url)
    decision = connection.execute(
        """
        SELECT routing_decision_id, routing_mode, price_as_of,
               estimated_input_tokens, estimated_output_tokens,
               token_estimate_truncated
        FROM routing_decisions
        WHERE request_id='req_prd047_fixed_minimal_dependencies'
        """
    ).fetchone()
    assert tuple(decision)[1:] == ("fixed", None, None, None, None)
    candidates = connection.execute(
        """
        SELECT keypool_state, price_source, estimated_cost_amount
        FROM routing_candidates
        WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert len(candidates) == 2
    assert all(tuple(row) == ("not_evaluated", "not_evaluated", None) for row in candidates)


@pytest.mark.anyio
async def test_availability_routing_ignores_price_and_token_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """availability_first 只读取 Health 与 KeyPool，价格或 Token 依赖故障不影响选择。

    参数：真实 Gateway fixture 和两个不应被调用的失败替身。
    返回值：无，断言成功、KeyPool 已评估，同时价格与 Token 字段保持空值。
    关键约束：后续 Budget Guard 的自身价格职责不属于路由器；本测试库没有预算规则，
    因此完整请求也不应凭空增加价格依赖。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    update_routing_mode(connection, "availability_first")

    def reject_price_or_token(*_args, **_kwargs):
        """availability 路由触达价格或 Token 估算时立即暴露回归。"""

        # 失败替身只约束初始 Router；测试响应不得依赖这段异常文本。
        raise AssertionError("availability price or token dependency must not be read")

    monkeypatch.setattr(routing_models, "_load_routing_prices", reject_price_or_token)
    monkeypatch.setattr(routing_models, "estimate_request_tokens", reject_price_or_token)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_availability_no_price",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "available"}],
            },
        )

    assert response.status_code == 200, response.text
    assert [item["body"]["model"] for item in captured] == ["local-chat-model"]
    decision = connection.execute(
        """
        SELECT routing_decision_id, price_as_of, estimated_input_tokens,
               estimated_output_tokens, token_estimate_truncated
        FROM routing_decisions
        WHERE request_id='req_prd047_availability_no_price'
        """
    ).fetchone()
    assert tuple(decision)[1:] == (None, None, None, None)
    candidates = connection.execute(
        """
        SELECT keypool_state, price_source FROM routing_candidates
        WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert len(candidates) == 2
    assert all(tuple(row) == ("available", "not_evaluated") for row in candidates)


@pytest.mark.anyio
async def test_availability_keypool_snapshot_failure_is_unified_and_stops_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """availability_first 的必要 KeyPool 快照失败时返回可重试统一码并停止上游。

    参数：真实请求链路和注入私有路径文本的 SQLite KeyPool 故障。
    返回值：无，断言 503/request_id、失败日志、零 Provider 与零半完成 Decision。
    关键约束：价格 loader 仍不得执行；异常中的数据库细节不能进入响应。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    update_routing_mode(connection, "availability_first")

    def fail_keypool_snapshot(*_args, **_kwargs):
        """模拟当前模式必需的 KeyPool 查询暂时不可用。"""

        # 使用 SQLite 异常验证包装层按可重试依赖故障分类，同时禁止泄露私有路径。
        raise sqlite3.OperationalError("D:\\private\\keypool.db unavailable")

    def reject_price_loader(*_args, **_kwargs):
        """availability 即使 KeyPool 失败也不能越过阶段去读取价格。"""

        # 若价格依赖被错误读取，AssertionError 会使目标用例直接失败而非伪装成统一码。
        raise AssertionError("availability price loader must not be read")

    monkeypatch.setattr(
        provider_keypool,
        "snapshot_provider_key_pool",
        fail_keypool_snapshot,
    )
    monkeypatch.setattr(routing_models, "_load_routing_prices", reject_price_loader)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_availability_keypool_failed",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "safe"}]},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.routing_snapshot_unavailable"
    assert response.json()["request_id"] == "req_prd047_availability_keypool_failed"
    assert "private" not in response.text
    assert captured == []
    request_log = connection.execute(
        """
        SELECT status, error_code FROM request_logs
        WHERE request_id='req_prd047_availability_keypool_failed'
        """
    ).fetchone()
    assert tuple(request_log) == ("failed", "gateway.routing_snapshot_unavailable")
    decision_count = connection.execute(
        """
        SELECT COUNT(*) FROM routing_decisions
        WHERE request_id='req_prd047_availability_keypool_failed'
        """
    ).fetchone()[0]
    assert decision_count == 0


@pytest.mark.anyio
async def test_cost_single_eligible_candidate_skips_price_and_token_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """cost_first 只有一条合格候选时不读价格、不计算 Token，直接稳定选择。

    参数：把 fallback 模型禁用的真实 Gateway fixture 和两项失败依赖替身。
    返回值：无，断言成功、单候选原因及所有 price/token 字段未评估。
    关键约束：损坏但未读取的价格不能阻断本分支；被模型状态先行拒绝的候选也不能
    触发 KeyPool 快照。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute("UPDATE models SET status='disabled' WHERE model_alias='fallback-chat'")
    connection.commit()
    update_routing_mode(connection, "cost_first")

    def reject_cost_dependency(*_args, **_kwargs):
        """单合格候选若访问价格或 Token，即表示两阶段延迟求值被破坏。"""

        # 立即失败比返回伪价格更能证明未读取依赖，而不会把错误文本写入业务事实。
        raise AssertionError("single candidate cost dependency must not be read")

    monkeypatch.setattr(routing_models, "_load_routing_prices", reject_cost_dependency)
    monkeypatch.setattr(routing_models, "estimate_request_tokens", reject_cost_dependency)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_cost_single_eligible",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "single"}]},
        )

    assert response.status_code == 200, response.text
    assert [item["body"]["model"] for item in captured] == ["local-chat-model"]
    decision = connection.execute(
        """
        SELECT routing_decision_id, decision_reason, price_as_of,
               estimated_input_tokens, estimated_output_tokens,
               token_estimate_truncated
        FROM routing_decisions
        WHERE request_id='req_prd047_cost_single_eligible'
        """
    ).fetchone()
    assert tuple(decision)[1:] == (
        "routing.single_eligible_candidate",
        None,
        None,
        None,
        None,
    )
    candidates = connection.execute(
        """
        SELECT candidate_role, status, keypool_state, price_source
        FROM routing_candidates
        WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert [tuple(row) for row in candidates] == [
        ("default", "selected", "available", "not_evaluated"),
        ("fallback", "rejected", "not_evaluated", "not_evaluated"),
    ]


@pytest.mark.anyio
async def test_cost_two_candidate_price_failure_is_unified_and_stops_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """cost_first 两条合格候选的价格查询失败时统一失败，不能猜测固定顺序。

    参数：真实两候选请求、计数 Token 估算器和抛 SQLite 错误的价格 loader。
    返回值：无，断言第二阶段只估算一次、503/request_id、失败日志与零 Provider。
    关键约束：价格是本分支必需依赖；异常路径或 SQL 文本不能进入响应，也不能留下
    Decision/Candidate 半事实。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    update_routing_mode(connection, "cost_first")
    original_estimator = routing_models.estimate_request_tokens
    estimator_calls = 0

    def count_token_estimate(request_kind: str, payload: dict[str, Any]):
        """调用真实有界估算器并记录第二阶段只执行一次。"""

        nonlocal estimator_calls
        estimator_calls += 1
        return original_estimator(request_kind, payload)

    def fail_price_loader(*_args, **_kwargs):
        """模拟当前 cost 分支必需的同快照价格查询失败。"""

        # 私有路径只用于证明统一错误会脱敏，绝不能进入响应或 RequestLog。
        raise sqlite3.OperationalError("D:\\private\\price.db unavailable")

    monkeypatch.setattr(routing_models, "estimate_request_tokens", count_token_estimate)
    monkeypatch.setattr(routing_models, "_load_routing_prices", fail_price_loader)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_cost_price_failed",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "price"}],
                "max_tokens": 4,
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.routing_snapshot_unavailable"
    assert response.json()["request_id"] == "req_prd047_cost_price_failed"
    assert "private" not in response.text
    assert estimator_calls == 1
    assert captured == []
    request_log = connection.execute(
        """
        SELECT status, error_code FROM request_logs
        WHERE request_id='req_prd047_cost_price_failed'
        """
    ).fetchone()
    assert tuple(request_log) == ("failed", "gateway.routing_snapshot_unavailable")
    decision_count = connection.execute(
        """
        SELECT COUNT(*) FROM routing_decisions
        WHERE request_id='req_prd047_cost_price_failed'
        """
    ).fetchone()[0]
    assert decision_count == 0


@pytest.mark.anyio
async def test_availability_first_rejects_fresh_unhealthy_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """可用性优先避开新鲜 unhealthy Provider，且动态初始 fallback 仍记为 primary。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute("SELECT tenant_id FROM tenants LIMIT 1").fetchone()["tenant_id"]
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status, provider_type,
          auth_type, health_status, health_updated_at, created_at, updated_at
        ) VALUES(
          'provider_prd047_available', ?, 'prd047_available', 'PRD047 Available', ?,
          'active', 'openai_compatible', 'none', 'healthy', datetime('now'),
          datetime('now'), datetime('now')
        )
        """,
        (tenant_id, upstream_url),
    )
    connection.execute(
        """
        UPDATE models SET provider_id='provider_prd047_available'
        WHERE model_id='model_prd015_fallback'
        """
    )
    connection.execute(
        """
        UPDATE providers SET health_status='unhealthy', health_updated_at=datetime('now')
        WHERE tenant_id=? AND provider_code='local_openai'
        """,
        (tenant_id,),
    )
    update_routing_mode(connection, "availability_first")
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_availability_first",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "route"}]},
        )

    assert response.status_code == 200, response.text
    assert captured[-1]["body"]["model"] == "fallback-upstream"
    decision = connection.execute(
        """
        SELECT routing_decision_id, routing_mode, decision_reason
        FROM routing_decisions WHERE request_id='req_prd047_availability_first'
        """
    ).fetchone()
    assert decision["routing_mode"] == "availability_first"
    assert decision["decision_reason"] == "routing.single_eligible_candidate"
    candidates = connection.execute(
        """
        SELECT candidate_role, status, reason_code, keypool_state
        FROM routing_candidates WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert candidates[0]["status"] == "rejected"
    assert candidates[0]["reason_code"] == "routing.candidate.provider_unhealthy"
    assert candidates[0]["keypool_state"] == "not_evaluated"
    assert candidates[1]["status"] == "selected"
    assert candidates[1]["keypool_state"] == "available"
    attempt = connection.execute(
        """
        SELECT attempt_role, trigger_type, attempt_sequence
        FROM routing_attempts WHERE request_id='req_prd047_availability_first'
        """
    ).fetchone()
    assert (attempt["attempt_role"], attempt["trigger_type"], attempt["attempt_sequence"]) == (
        "primary",
        "initial",
        1,
    )


@pytest.mark.parametrize(
    ("case_name", "condition_expr"),
    [
        ("mixed_non_string", '[1,"upstream_5xx"]'),
        ("unknown_only", '["private_condition_must_not_leak"]'),
        ("duplicate", '["upstream_5xx","upstream_5xx"]'),
        ("noncanonical_order", '["upstream_5xx","upstream_timeout"]'),
    ],
)
@pytest.mark.anyio
async def test_dynamic_fallback_rejects_noncanonical_condition_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    case_name: str,
    condition_expr: str,
) -> None:
    """动态路由遇到混合类型、未知、重复或乱序 Fallback 条件时失败关闭。

    参数：四类绕过控制面写入口的持久条件 JSON。
    返回值：无，断言统一错误、request_id、零 Provider、零事件与失败 RequestLog。
    关键约束：合法项与非法项混合时也不能静默过滤；响应和日志链路只保留固定错误码，
    不回显原始条件正文。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    enable_runtime_fallback(
        connection,
        policy_id=f"policy_prd047_invalid_{case_name}",
    )
    connection.execute(
        """
        UPDATE fallback_policies SET condition_expr=?, updated_at=datetime('now')
        WHERE model_alias_id='alias_prd015_company_default'
        """,
        (condition_expr,),
    )
    connection.commit()
    update_routing_mode(connection, "availability_first")
    queued_events = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued_events.append(event) or True,
    )
    request_id = f"req_prd047_invalid_fallback_{case_name}"
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": request_id,
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "safe"}]},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.routing_snapshot_invalid"
    assert response.json()["request_id"] == request_id
    assert condition_expr not in response.text
    assert captured == []
    assert queued_events == []
    request_log = connection.execute(
        """
        SELECT status, error_code FROM request_logs WHERE request_id=?
        """,
        (request_id,),
    ).fetchone()
    assert tuple(request_log) == ("failed", "gateway.routing_snapshot_invalid")
    decision_count = connection.execute(
        "SELECT COUNT(*) FROM routing_decisions WHERE request_id=?",
        (request_id,),
    ).fetchone()[0]
    assert decision_count == 0


@pytest.mark.anyio
async def test_implicit_fixed_fallback_invalid_snapshot_uses_unified_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """缺少 RoutingPolicy 的 legacy fixed Alias 也不能把非法 Fallback 条件变成裸 500。

    参数：真实 SQLite 夹具，并只在测试库删除 Alias 自动生成的 RoutingPolicy。
    返回值：无，断言统一错误/request_id、零 Provider、失败日志和无策略事件。
    关键约束：implicit fixed 只兼容旧 Alias 读取，不放宽已启用 Fallback 快照；条件正文
    仍不得进入响应，legacy 已记录的选择事实也不能触发真实上游调用。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    enable_runtime_fallback(
        connection,
        policy_id="policy_prd047_legacy_invalid_conditions",
    )
    invalid_condition = '[1,"private_legacy_condition_must_not_leak"]'
    connection.execute(
        """
        UPDATE fallback_policies SET condition_expr=?, updated_at=datetime('now')
        WHERE model_alias_id='alias_prd015_company_default'
        """,
        (invalid_condition,),
    )
    # 正常 0036/0037 禁止删除绑定 Policy；这里只移除测试库保护以构造兼容旧夹具。
    connection.execute("DROP TRIGGER IF EXISTS trg_routing_policies_prd047_delete")
    connection.execute("DROP TRIGGER IF EXISTS trg_routing_policies_prd048_delete")
    connection.execute(
        """
        DELETE FROM model_alias_routing_policies
        WHERE model_alias_id='alias_prd015_company_default'
        """
    )
    connection.commit()
    queued_events = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued_events.append(event) or True,
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_legacy_invalid_fallback",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "safe"}]},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.routing_snapshot_invalid"
    assert response.json()["request_id"] == "req_prd047_legacy_invalid_fallback"
    assert invalid_condition not in response.text
    assert captured == []
    assert queued_events == []
    request_log = connection.execute(
        """
        SELECT status, error_code FROM request_logs
        WHERE request_id='req_prd047_legacy_invalid_fallback'
        """
    ).fetchone()
    assert tuple(request_log) == ("failed", "gateway.routing_snapshot_invalid")
    attempts = connection.execute(
        """
        SELECT COUNT(*) FROM routing_attempts
        WHERE request_id='req_prd047_legacy_invalid_fallback'
        """
    ).fetchone()[0]
    assert attempts == 0


@pytest.mark.anyio
async def test_dynamic_default_failure_uses_only_eligible_standby_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """动态模式初始 default 失败后，只执行同一快照内 eligible standby。

    参数：真实 SQLite 测试库和受控 500 上游 fixture。
    返回值：无，断言两次调用、attempt 顺序、最终 Usage 路由和两类事件。
    关键约束：成本排序先选 default，PRD-036 只能复用已冻结的 fallback standby；
    不得在首次失败后重新读取候选或追加第三次尝试。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET upstream_model_name='prd047-upstream-error',
            provider_model_id='prd047-upstream-error',
            input_price_per_1k='1.000000000000',
            output_price_per_1k='1.000000000000',
            currency='CNY', price_schema_version='dynamic-default-v1'
        WHERE model_alias='default-chat'
        """
    )
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k='9.000000000000',
            output_price_per_1k='9.000000000000',
            currency='CNY', price_schema_version='dynamic-standby-v1'
        WHERE model_alias='fallback-chat'
        """
    )
    enable_runtime_fallback(connection, policy_id="policy_prd047_dynamic_default")
    update_routing_mode(connection, "cost_first")
    queued_events = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued_events.append(event) or True,
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_dynamic_default_fallback",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "dynamic fallback"}],
                "max_tokens": 8,
            },
        )

    assert response.status_code == 200, response.text
    assert [item["body"]["model"] for item in captured] == [
        "prd047-upstream-error",
        "fallback-upstream",
    ]
    attempts = connection.execute(
        """
        SELECT attempt_sequence, attempt_role, status
        FROM routing_attempts
        WHERE request_id='req_prd047_dynamic_default_fallback'
        ORDER BY attempt_sequence
        """
    ).fetchall()
    assert [tuple(row) for row in attempts] == [
        (1, "primary", "failed"),
        (2, "fallback", "succeeded"),
    ]
    request_log = connection.execute(
        """
        SELECT fallback_triggered, routing_attempt_count, resolved_model
        FROM request_logs WHERE request_id='req_prd047_dynamic_default_fallback'
        """
    ).fetchone()
    assert tuple(request_log) == (1, 2, "fallback-chat")
    decision = connection.execute(
        """
        SELECT routing_decision_id, routing_mode, selected_model_id
        FROM routing_decisions WHERE request_id='req_prd047_dynamic_default_fallback'
        """
    ).fetchone()
    assert decision["routing_mode"] == "cost_first"
    candidates = connection.execute(
        """
        SELECT candidate_role, status FROM routing_candidates
        WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert [tuple(row) for row in candidates] == [
        ("default", "selected"),
        ("fallback", "standby"),
    ]
    assert [event.event_type for event in queued_events] == [
        "route.policy_selected",
        "route.fallback_triggered",
        "ledger.entry_created",
    ]


@pytest.mark.anyio
async def test_dynamic_initial_fallback_failure_never_retries_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """动态排序初始选中 fallback 时按 primary 记账，失败后不反向调用 default。

    参数：真实 SQLite 测试库和只让 fallback 上游返回 500 的 fixture。
    返回值：无，断言单次调用、失败日志和事件序列。
    关键约束：候选角色只解释配置来源；动态初始 fallback 不是 PRD-036 已触发，
    因此 ``fallback_triggered`` 必须为 false，且成功的 default 也不能被反向重试。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k='9.000000000000',
            output_price_per_1k='9.000000000000',
            currency='CNY', price_schema_version='dynamic-default-expensive-v1'
        WHERE model_alias='default-chat'
        """
    )
    connection.execute(
        """
        UPDATE models
        SET upstream_model_name='prd047-upstream-error',
            provider_model_id='prd047-upstream-error',
            input_price_per_1k='1.000000000000',
            output_price_per_1k='1.000000000000',
            currency='CNY', price_schema_version='dynamic-fallback-cheap-v1'
        WHERE model_alias='fallback-chat'
        """
    )
    enable_runtime_fallback(connection, policy_id="policy_prd047_initial_fallback")
    update_routing_mode(connection, "cost_first")
    queued_events = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued_events.append(event) or True,
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_initial_fallback_failed",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "do not reverse"}],
                "max_tokens": 8,
            },
        )

    assert response.status_code == 502, response.text
    assert response.json()["code"] == "gateway.upstream_error"
    assert [item["body"]["model"] for item in captured] == ["prd047-upstream-error"]
    attempt = connection.execute(
        """
        SELECT attempt_sequence, attempt_role, status
        FROM routing_attempts WHERE request_id='req_prd047_initial_fallback_failed'
        """
    ).fetchone()
    assert tuple(attempt) == (1, "primary", "failed")
    request_log = connection.execute(
        """
        SELECT fallback_triggered, routing_attempt_count, resolved_model, error_code
        FROM request_logs WHERE request_id='req_prd047_initial_fallback_failed'
        """
    ).fetchone()
    assert tuple(request_log) == (0, 1, "fallback-chat", "gateway.upstream_error")
    assert [event.event_type for event in queued_events] == ["route.policy_selected"]


@pytest.mark.anyio
async def test_dynamic_rejected_fallback_is_never_attached_after_default_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """动态候选已因 Provider unhealthy 被拒绝时，主调用失败也不能追加它。

    参数：真实 SQLite 测试库和受控 default 500 上游。
    返回值：无，断言一条 attempt、拒绝原因和零 Fallback 事件。
    关键约束：PRD-036 只能消费同一冻结事务中的 ``standby``；重新查询后复活
    ``rejected`` 候选会绕过健康治理，因此测试同时锁定 Candidate 与网络调用数量。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute("SELECT tenant_id FROM tenants LIMIT 1").fetchone()["tenant_id"]
    connection.execute(
        """
        UPDATE models
        SET upstream_model_name='prd047-upstream-error',
            provider_model_id='prd047-upstream-error'
        WHERE model_alias='default-chat'
        """
    )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status, provider_type,
          auth_type, health_status, health_updated_at, created_at, updated_at
        ) VALUES(
          'provider_prd047_rejected_fallback', ?, 'prd047_rejected_fallback',
          'Rejected Fallback', ?, 'active', 'openai_compatible', 'none',
          'unhealthy', datetime('now'), datetime('now'), datetime('now')
        )
        """,
        (tenant_id, upstream_url),
    )
    connection.execute(
        """
        UPDATE models SET provider_id='provider_prd047_rejected_fallback'
        WHERE model_alias='fallback-chat'
        """
    )
    enable_runtime_fallback(connection, policy_id="policy_prd047_rejected_fallback")
    update_routing_mode(connection, "availability_first")
    queued_events = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued_events.append(event) or True,
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_rejected_fallback",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "fail"}]},
        )

    assert response.status_code == 502, response.text
    assert [item["body"]["model"] for item in captured] == ["prd047-upstream-error"]
    attempts = connection.execute(
        """
        SELECT attempt_sequence, attempt_role FROM routing_attempts
        WHERE request_id='req_prd047_rejected_fallback'
        """
    ).fetchall()
    assert [tuple(row) for row in attempts] == [(1, "primary")]
    request_log = connection.execute(
        """
        SELECT fallback_triggered, routing_attempt_count
        FROM request_logs WHERE request_id='req_prd047_rejected_fallback'
        """
    ).fetchone()
    assert tuple(request_log) == (0, 1)
    decision = connection.execute(
        """
        SELECT routing_decision_id FROM routing_decisions
        WHERE request_id='req_prd047_rejected_fallback'
        """
    ).fetchone()
    candidates = connection.execute(
        """
        SELECT candidate_role, status, reason_code FROM routing_candidates
        WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert [tuple(row) for row in candidates] == [
        ("default", "selected", "routing.single_eligible_candidate"),
        ("fallback", "rejected", "routing.candidate.provider_unhealthy"),
    ]
    assert [event.event_type for event in queued_events] == ["route.policy_selected"]


@pytest.mark.anyio
async def test_dynamic_routing_fails_before_provider_when_all_keypools_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """动态模式全部 KeyPool 不可用时写失败事实并返回统一码，绝不尝试上游。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute("DELETE FROM provider_credentials")
    connection.commit()
    monkeypatch.delenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", raising=False)
    update_routing_mode(connection, "availability_first")
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_no_keypool_route",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "no key"}]},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.no_route_available"
    assert response.json()["request_id"] == "req_prd047_no_keypool_route"
    assert captured == []
    decision = connection.execute(
        """
        SELECT routing_decision_id, decision_status, error_code, routing_policy_event_id
        FROM routing_decisions WHERE request_id='req_prd047_no_keypool_route'
        """
    ).fetchone()
    decision_fact = (
        decision["decision_status"],
        decision["error_code"],
        decision["routing_policy_event_id"],
    )
    assert decision_fact == (
        "failed",
        "gateway.no_route_available",
        None,
    )
    candidates = connection.execute(
        """
        SELECT status, reason_code, keypool_state FROM routing_candidates
        WHERE routing_decision_id=? ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert len(candidates) == 2
    assert all(row["status"] == "rejected" for row in candidates)
    assert all(row["reason_code"] == "routing.candidate.keypool_unavailable" for row in candidates)
    assert all(row["keypool_state"] == "unavailable" for row in candidates)


@pytest.mark.anyio
async def test_dynamic_routing_fact_write_failure_stops_before_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """动态 Decision/Candidate 原子写失败时返回统一错误并保持零上游调用。

    参数：真实 SQLite 请求链路和仅阻断 Candidate INSERT 的测试触发器。
    返回值：无，断言统一错误、失败 RequestLog 以及无半条路由事实。
    关键约束：动态选择没有解释事实就不得调用 Provider；异常正文不能进入响应，
    外层仍必须用同一 request_id 记录失败，便于租户内追踪。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    update_routing_mode(connection, "availability_first")
    # 触发器只在 Candidate INSERT 时失败：此时同一事务中的 Decision 已经写入，
    # 因而最终零行可以直接证明 rollback 覆盖了半完成事实，而非仅证明函数未执行。
    connection.execute(
        """
        CREATE TRIGGER trg_test_prd047_candidate_write_failure
        BEFORE INSERT ON routing_candidates
        BEGIN
          SELECT RAISE(ABORT, 'private routing fact path must stay hidden');
        END
        """
    )
    connection.commit()
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_decision_write_failed",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "private"}]},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.routing_decision_unavailable"
    assert response.json()["request_id"] == "req_prd047_decision_write_failed"
    assert "private routing fact path" not in response.text
    assert captured == []
    request_log = connection.execute(
        """
        SELECT status, error_code FROM request_logs
        WHERE request_id='req_prd047_decision_write_failed'
        """
    ).fetchone()
    assert tuple(request_log) == ("failed", "gateway.routing_decision_unavailable")
    decision_count = connection.execute(
        """
        SELECT COUNT(*) FROM routing_decisions
        WHERE request_id='req_prd047_decision_write_failed'
        """
    ).fetchone()[0]
    candidate_count = connection.execute(
        """
        SELECT COUNT(*) FROM routing_candidates
        WHERE routing_decision_id IN (
          SELECT routing_decision_id FROM routing_decisions
          WHERE request_id='req_prd047_decision_write_failed'
        )
        """
    ).fetchone()[0]
    assert decision_count == 0
    assert candidate_count == 0


@pytest.mark.anyio
async def test_model_alias_skips_fresh_unhealthy_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """异常恢复：别名默认 Provider 新鲜异常时选择健康 fallback，并记录拒绝原因。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute("SELECT tenant_id FROM tenants LIMIT 1").fetchone()["tenant_id"]
    default_provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (tenant_id,),
    ).fetchone()["provider_id"]
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status, provider_type,
          auth_type, health_status, health_updated_at, created_at, updated_at
        ) VALUES(
          'provider_prd035_fallback', ?, 'health_fallback', 'Health Fallback', ?, 'active',
          'openai_compatible', 'none', 'healthy', datetime('now'), datetime('now'), datetime('now')
        )
        """,
        (tenant_id, upstream_url),
    )
    connection.execute(
        "UPDATE models SET provider_id='provider_prd035_fallback' WHERE model_id='model_prd015_fallback'"
    )
    connection.execute(
        """
        UPDATE providers SET health_status='unhealthy', health_updated_at=datetime('now')
        WHERE tenant_id=? AND provider_id=?
        """,
        (tenant_id, default_provider),
    )
    connection.commit()
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd035_fallback",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "health route"}],
            },
        )

    assert response.status_code == 200, response.text
    assert captured[-1]["body"]["model"] == "fallback-upstream"
    decision = connection.execute(
        "SELECT candidate_summary FROM routing_decisions WHERE request_id='req_prd035_fallback'"
    ).fetchone()
    assert "provider_unhealthy" in decision["candidate_summary"]


@pytest.mark.anyio
async def test_direct_model_rejects_fresh_but_allows_stale_unhealthy_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """路由边界：新鲜 unhealthy 返回统一 503，过期证据 Fail Open 且请求可成功。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        "UPDATE providers SET health_status='unhealthy', health_updated_at=datetime('now') WHERE provider_code='local_openai'"
    )
    connection.commit()
    app = build_gateway_app()
    headers = {"Authorization": f"Bearer {VIRTUAL_KEY}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        rejected = await client.post(
            "/v1/chat/completions",
            headers={**headers, "X-Request-ID": "req_prd035_unhealthy"},
            json={"model": "default-chat", "messages": [{"role": "user", "content": "blocked"}]},
        )
        connection.execute(
            "UPDATE providers SET health_updated_at=datetime('now', '-10 minutes') WHERE provider_code='local_openai'"
        )
        connection.commit()
        allowed = await client.post(
            "/v1/chat/completions",
            headers={**headers, "X-Request-ID": "req_prd035_stale"},
            json={"model": "default-chat", "messages": [{"role": "user", "content": "allowed"}]},
        )

    assert rejected.status_code == 503
    assert rejected.json()["code"] == "gateway.provider_unhealthy"
    assert allowed.status_code == 200
    assert captured[-1]["body"]["model"] == "local-chat-model"
    failed_log = connection.execute(
        "SELECT error_code FROM request_logs WHERE request_id='req_prd035_unhealthy'"
    ).fetchone()
    assert failed_log is not None
    assert failed_log["error_code"] == "gateway.provider_unhealthy"


@pytest.mark.anyio
async def test_direct_model_does_not_touch_routing_policy_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """Direct 模型不读 Policy/Score、不取写锁，治理依赖故障时仍走 legacy。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    original_connect = routing_models._connect
    observed_sql: list[str] = []

    class GuardedConnection:
        """代理真实 SQLite 连接，并把 RoutingPolicy 访问模拟为独立依赖故障。"""

        def __init__(self, inner) -> None:
            """保存底层连接；测试只拦截顶层 SQL，不改变触发器与租户约束。"""

            self._inner = inner

        def execute(self, sql: str, parameters=()):
            """拒绝策略表和写锁语句，以证明 Direct 分支不会触发这些依赖。"""

            normalized = " ".join(sql.lower().split())
            observed_sql.append(normalized)
            if (
                "model_alias_routing_policies" in normalized
                or "model_quality_scores" in normalized
                or normalized.startswith("begin immediate")
            ):
                # Direct 触及 Alias Policy/Score 就模拟故障，避免被当前表可用性掩盖。
                raise sqlite3.OperationalError("routing governance dependency unavailable")
            return self._inner.execute(sql, parameters)

        def __getattr__(self, name: str):
            """其余连接能力原样转发，保持真实迁移、触发器和事务行为。"""

            return getattr(self._inner, name)

    def guarded_connect(config):
        """为路由模块的每次连接安装策略依赖防线。"""

        return GuardedConnection(original_connect(config))

    monkeypatch.setattr(routing_models, "_connect", guarded_connect)
    queued_events = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued_events.append(event) or True,
    )
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_direct_policy_isolation",
            },
            json={"model": "default-chat", "messages": [{"role": "user", "content": "direct"}]},
        )

    assert response.status_code == 200, response.text
    assert captured[-1]["body"]["model"] == "local-chat-model"
    assert not any("model_alias_routing_policies" in sql for sql in observed_sql)
    assert not any("model_quality_scores" in sql for sql in observed_sql)
    assert not any(sql.startswith("begin immediate") for sql in observed_sql)
    # Direct 分支不得产生路由治理事件；PRD-051 的 Usage 账本事件属于独立财务事实。
    assert [event.event_type for event in queued_events] == ["ledger.entry_created"]
    connection = connect_database(database_url)
    decision = connection.execute(
        """
        SELECT routing_policy_id, routing_mode, routing_policy_event_id, decision_reason
        FROM routing_decisions WHERE request_id='req_prd047_direct_policy_isolation'
        """
    ).fetchone()
    assert tuple(decision) == (None, None, None, "direct_model")


@pytest.mark.anyio
async def test_alias_routing_database_failure_uses_sanitized_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """路由快照连接失败固定映射为 503，响应不泄露数据库路径且保留失败 RequestLog。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    original_connect = routing_models._connect
    connect_count = 0

    def fail_snapshot_connect(config):
        """仅让权威预校验后的路由快照连接失败，模拟真实数据库依赖中断。"""

        nonlocal connect_count
        connect_count += 1
        if connect_count == 2:
            # 原始错误刻意携带私有路径，统一响应层必须只返回固定错误码与安全文案。
            raise ApiError(
                "db.connection_failed",
                message="failed to open D:\\private\\tenant-routing.db",
            )
        return original_connect(config)

    monkeypatch.setattr(routing_models, "_connect", fail_snapshot_connect)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_snapshot_db_failed",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "private"}]},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.routing_snapshot_unavailable"
    assert response.json()["request_id"] == "req_prd047_snapshot_db_failed"
    assert "private\\tenant-routing.db" not in response.text
    assert captured == []
    connection = connect_database(database_url)
    request_log = connection.execute(
        """
        SELECT request_id, status, error_code FROM request_logs
        WHERE request_id='req_prd047_snapshot_db_failed'
        """
    ).fetchone()
    assert tuple(request_log) == (
        "req_prd047_snapshot_db_failed",
        "failed",
        "gateway.routing_snapshot_unavailable",
    )


@pytest.mark.anyio
async def test_alias_routing_rejects_unknown_health_with_invalid_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """unknown 不形成健康证据，但其非空坏时间仍会使冻结快照失败且不调用 Provider。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE providers SET health_status='unknown', health_updated_at='not-a-time'
        WHERE provider_code='local_openai'
        """
    )
    connection.commit()
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_invalid_health_time",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "private"}]},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.routing_snapshot_invalid"
    assert response.json()["request_id"] == "req_prd047_invalid_health_time"
    assert captured == []
    request_log = connection.execute(
        """
        SELECT status, error_code FROM request_logs
        WHERE request_id='req_prd047_invalid_health_time'
        """
    ).fetchone()
    assert tuple(request_log) == ("failed", "gateway.routing_snapshot_invalid")


@pytest.mark.anyio
async def test_alias_routing_rejects_corrupt_managed_key_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """托管 Key 状态枚举损坏时统一返回快照无效，且绝不解密或调用 Provider。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    provider = connection.execute(
        """
        SELECT tenant_id, provider_id FROM providers
        WHERE provider_code='local_openai'
        """
    ).fetchone()
    connection.execute(
        "DELETE FROM provider_credentials WHERE provider_id=?",
        (provider["provider_id"],),
    )
    # 模拟绕过正常写 API 的存量坏数据；伪密文只用于触发托管边界，快照不得尝试解密。
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        """
        INSERT INTO provider_credentials(
          provider_credential_id, tenant_id, provider_id, key_fingerprint,
          encrypted_key, name, key_prefix, weight, status, created_at, updated_at
        ) VALUES(
          'key_prd047_corrupt_snapshot', ?, ?, 'fingerprint-prd047-corrupt',
          'aesgcm-v1:must-not-decrypt', 'Corrupt Snapshot', 'sk-corrupt', 100,
          'corrupt', datetime('now'), datetime('now')
        )
        """,
        (provider["tenant_id"], provider["provider_id"]),
    )
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode='availability_first', version=version+1,
            updated_at=datetime('now')
        WHERE model_alias_id='alias_prd015_company_default'
        """
    )
    connection.commit()
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_corrupt_key_snapshot",
            },
            json={"model": "company-default", "messages": [{"role": "user", "content": "private"}]},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.routing_snapshot_invalid"
    assert response.json()["request_id"] == "req_prd047_corrupt_key_snapshot"
    assert captured == []
    request_log = connection.execute(
        """
        SELECT status, error_code FROM request_logs
        WHERE request_id='req_prd047_corrupt_key_snapshot'
        """
    ).fetchone()
    assert tuple(request_log) == ("failed", "gateway.routing_snapshot_invalid")


@pytest.mark.anyio
async def test_model_alias_falls_back_when_default_model_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主流程：默认模型禁用时，最小路由选择管理员配置的备用模型。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute("UPDATE models SET status='disabled' WHERE model_alias='default-chat'")
    connection.commit()
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd015_alias_fallback",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "use fallback"}],
            },
        )

    assert response.status_code == 200, response.text
    assert captured[-1]["body"]["model"] == "fallback-upstream"
    decision = connection.execute(
        """
        SELECT selected_internal_model_id, decision_status, decision_reason, candidate_summary
        FROM routing_decisions
        WHERE request_id='req_prd015_alias_fallback'
        """
    ).fetchone()
    assert decision["selected_internal_model_id"] == "fallback-chat"
    assert decision["decision_status"] == "selected"
    assert decision["decision_reason"] == "alias.fallback"
    assert "model_disabled" in decision["candidate_summary"]


@pytest.mark.anyio
async def test_model_alias_disabled_and_no_active_model_failures_are_logged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """异常流程：禁用别名与无可用模型都返回可解释错误，并写入失败日志。"""

    upstream_url, _captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute("UPDATE model_aliases SET status='disabled' WHERE alias='company-default'")
    connection.commit()
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        disabled_alias = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd015_alias_disabled",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "should fail"}],
            },
        )
        connection.execute("UPDATE model_aliases SET status='active' WHERE alias='company-default'")
        connection.execute(
            "UPDATE models SET status='disabled' WHERE model_alias IN ('default-chat', 'fallback-chat')"
        )
        connection.commit()
        no_active_model = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd015_model_disabled",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "should fail too"}],
            },
        )

    assert disabled_alias.status_code == 409
    assert disabled_alias.json()["code"] == "gateway.model_alias_disabled"
    assert no_active_model.status_code == 409
    assert no_active_model.json()["code"] == "gateway.model_disabled"

    request_logs = connection.execute(
        """
        SELECT request_id, status, error_code
        FROM request_logs
        WHERE request_id IN ('req_prd015_alias_disabled', 'req_prd015_model_disabled')
        ORDER BY request_id
        """
    ).fetchall()
    assert [(row["request_id"], row["status"], row["error_code"]) for row in request_logs] == [
        ("req_prd015_alias_disabled", "failed", "gateway.model_alias_disabled"),
        ("req_prd015_model_disabled", "failed", "gateway.model_disabled"),
    ]
    decisions = connection.execute(
        """
        SELECT request_id, decision_status, decision_reason, error_code
        FROM routing_decisions
        WHERE request_id IN ('req_prd015_alias_disabled', 'req_prd015_model_disabled')
        ORDER BY request_id
        """
    ).fetchall()
    # PRD-045 的源模型权威预校验必须无副作用：disabled alias 在 Policy 前拒绝，不能
    # 写成一次已经发生的路由决策；只有 allow 后真正进入 resolver 的无可用模型失败落表。
    assert [
        (row["request_id"], row["decision_status"], row["decision_reason"], row["error_code"])
        for row in decisions
    ] == [
        ("req_prd015_model_disabled", "failed", "alias.no_active_model", "gateway.model_disabled"),
    ]
