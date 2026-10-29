"""PRD-054 请求解释的决策边界、精度、故障关闭与调度器生命周期测试。

本文件复用主专项的真实迁移和 Session 夹具，只补主测试未覆盖的高风险边界。每个场景
均在 ``tmp_path`` 独占数据库执行，不访问网络，也不依赖工作区外临时目录。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.service import authenticate_bearer
from metahub_control.request_explanations import outbox, scheduler
from metahub_control.request_explanations import service as explanation_service
from metahub_shared.db import connect_database
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError
from test_prd054_request_explanations import (
    ADMIN_PASSWORD,
    FIXTURE_TIME,
    SOURCE_REQUEST_ID,
    _assert_error,
    _build_app,
    _headers,
    _login,
    _prepare_fixture,
)


def _source_metadata(database_url: str, tenant_id: str) -> dict[str, Any]:
    """读取测试租户的 Key、Environment、Model 与 Provider 安全稳定 ID。"""

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT
              vk.virtual_key_id, environment.environment_id,
              environment.code AS environment_code,
              model.model_id, provider.provider_id, provider.provider_code
            FROM virtual_keys vk
            JOIN environments environment
              ON environment.tenant_id=vk.tenant_id
             AND environment.environment_id=vk.environment_id
            JOIN models model ON model.tenant_id=vk.tenant_id
            JOIN providers provider
              ON provider.tenant_id=model.tenant_id
             AND provider.provider_id=model.provider_id
            WHERE vk.tenant_id=?
            ORDER BY model.model_id, vk.virtual_key_id
            LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()
        assert row is not None
        return dict(row)
    finally:
        connection.close()


def _stage(explanation: dict[str, Any], name: str) -> dict[str, Any]:
    """按固定阶段名读取一项公开决策，测试不存在时立即失败。"""

    return next(item for item in explanation["decision_chain"] if item["stage"] == name)


@pytest.mark.anyio
async def test_prd054_legacy_partial_async_pending_and_dlp_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证 legacy 缺证据、异步 pending、真实 DLP hash 以及 N/N+1 截断。"""

    fixture = _prepare_fixture(monkeypatch, tmp_path)
    connection = connect_database(fixture.database_url)
    try:
        connection.execute(
            """
            INSERT INTO dlp_rules(
              dlp_rule_id, tenant_id, name, rule_type, pattern, pattern_hash,
              action, status, priority, created_at, updated_at
            ) VALUES(
              'dlp_prd054_edge', ?, 'PRD054 摘要规则', 'keyword',
              '仅存在测试进程', ?, 'log_only', 'active', 100, ?, ?
            )
            """,
            (fixture.tenant_id, f"sha256:{'a' * 64}", FIXTURE_TIME, FIXTURE_TIME),
        )
        connection.execute(
            """
            UPDATE request_logs
            SET dlp_status='matched', dlp_rule_id='dlp_prd054_edge',
                dlp_action='log_only', dlp_match_count=100,
                dlp_reason_code='dlp.keyword_matched'
            WHERE tenant_id=? AND request_id=?
            """,
            (fixture.tenant_id, SOURCE_REQUEST_ID),
        )
        # 前 100 行恰好等于公开上限；哈希沿用 Gateway 的 sha256: 前缀合同。
        connection.executemany(
            """
            INSERT INTO dlp_matches(
              dlp_match_id, tenant_id, request_id, trace_id, dlp_rule_id,
              rule_name, rule_type, action, message_index,
              match_text_hash, match_length, created_at
            ) VALUES(?, ?, ?, 'trc_prd054_explain', 'dlp_prd054_edge',
                     'PRD054 摘要规则', 'keyword', 'log_only', ?, ?, 8, ?)
            """,
            [
                (
                    f"dlpm_{index:032x}",
                    fixture.tenant_id,
                    SOURCE_REQUEST_ID,
                    index,
                    f"sha256:{index:064x}",
                    FIXTURE_TIME,
                )
                for index in range(100)
            ],
        )
        connection.commit()
    finally:
        connection.close()

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        at_limit = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_dlp_at_limit"),
        )
        assert at_limit.status_code == 200, at_limit.text
        first = at_limit.json()["request_explanation"]
        first_ids = [fact["fact_id"] for fact in first["dlp"]["facts"]]
        assert len(first_ids) == 100
        assert first["dlp"]["truncated"] is False
        assert first["dlp"]["facts"][0]["values"]["match_text_hash"].startswith("sha256:")
        # legacy 请求缺少路由事实仍为 partial；成功后尚未到账的 Usage 明确标 pending。
        assert first["status"] == "partial"
        assert first["routing"]["evidence_state"] == "not_recorded"
        assert first["usage_and_finops"]["evidence_state"] == "pending"
        assert _stage(first, "usage_and_finops")["status"] == "pending"

        connection = connect_database(fixture.database_url)
        try:
            connection.execute(
                """
                INSERT INTO dlp_matches(
                  dlp_match_id, tenant_id, request_id, trace_id, dlp_rule_id,
                  rule_name, rule_type, action, message_index,
                  match_text_hash, match_length, created_at
                ) VALUES(
                  'dlpm_00000000000000000000000000000064', ?, ?,
                  'trc_prd054_explain', 'dlp_prd054_edge', 'PRD054 摘要规则',
                  'keyword', 'log_only', 100, ?, 8, ?
                )
                """,
                (
                    fixture.tenant_id,
                    SOURCE_REQUEST_ID,
                    f"sha256:{100:064x}",
                    FIXTURE_TIME,
                ),
            )
            connection.execute(
                """
                UPDATE request_logs SET dlp_match_count=101
                WHERE tenant_id=? AND request_id=?
                """,
                (fixture.tenant_id, SOURCE_REQUEST_ID),
            )
            connection.commit()
        finally:
            connection.close()

        over_limit = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_dlp_over_limit"),
        )

    assert over_limit.status_code == 200, over_limit.text
    second = over_limit.json()["request_explanation"]
    assert [fact["fact_id"] for fact in second["dlp"]["facts"]] == first_ids
    assert second["dlp"]["truncated"] is True
    assert "dlp" in second["truncated_sections"]
    assert "dlp" not in second["missing_sections"]


@pytest.mark.anyio
async def test_prd054_budget_block_preserves_decimal_and_micros_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证预算阻断链，以及 Decimal 和上限 micros 不经 JSON Number 重算。"""

    fixture = _prepare_fixture(monkeypatch, tmp_path)
    metadata = _source_metadata(fixture.database_url, fixture.tenant_id)
    connection = connect_database(fixture.database_url)
    try:
        connection.execute(
            """
            INSERT INTO budget_rules(
              budget_rule_id, tenant_id, environment_id, scope_type, scope_id,
              name, daily_budget_cny, warning_threshold_percent,
              exhausted_threshold_percent, exhausted_action, status,
              created_at, updated_at
            ) VALUES(
              'br_prd054_edge', ?, ?, 'virtual_key', ?, 'PRD054 精度预算',
              1.000001, 80, 100, 'block', 'active', ?, ?
            )
            """,
            (
                fixture.tenant_id,
                metadata["environment_id"],
                metadata["virtual_key_id"],
                FIXTURE_TIME,
                FIXTURE_TIME,
            ),
        )
        connection.execute(
            """
            INSERT INTO budget_evaluations(
              budget_evaluation_id, tenant_id, environment_id, request_id,
              trace_id, virtual_key_id, budget_rule_id, scope_type, scope_id,
              budget_date, spent_before_cny, estimated_request_cost_cny,
              projected_cost_cny, budget_cny, threshold_percent,
              budget_status, action_taken, reason_code, created_at
            ) VALUES(
              'be_prd054_edge', ?, ?, ?, 'trc_prd054_explain', ?,
              'br_prd054_edge', 'virtual_key', ?, '2026-07-28',
              0.123456, 1.000001, 1.123457, 1.000001, 100,
              'exhausted', 'block', 'budget.exhausted.block', ?
            )
            """,
            (
                fixture.tenant_id,
                metadata["environment_id"],
                SOURCE_REQUEST_ID,
                metadata["virtual_key_id"],
                metadata["virtual_key_id"],
                FIXTURE_TIME,
            ),
        )
        connection.execute(
            """
            INSERT INTO budget_scope_events(
              budget_scope_event_id, event_id, tenant_id, environment_id,
              budget_rule_id, budget_scope_id, scope_type, scope_id,
              budget_date, action_taken, posted_cost_micros,
              reserved_cost_micros, estimated_request_cost_micros,
              projected_cost_micros, budget_micros,
              request_id, trace_id, created_at
            ) VALUES(
              'bse_00000000000000000000000000000001',
              'bsevt_00000000000000000000000000000001', ?, ?,
              'br_prd054_edge', 'bsc_00000000000000000000000000000001',
              'virtual_key', ?, '2026-07-28', 'block',
              999999999999997, 1, 2, 1000000000000000, 999999999999999,
              ?, 'trc_prd054_explain', ?
            )
            """,
            (
                fixture.tenant_id,
                metadata["environment_id"],
                metadata["virtual_key_id"],
                SOURCE_REQUEST_ID,
                FIXTURE_TIME,
            ),
        )
        connection.execute(
            """
            UPDATE request_logs
            SET status='failed', error_code='gateway.budget_exhausted',
                budget_status='exhausted', budget_rule_id='br_prd054_edge',
                budget_scope_type='virtual_key', budget_scope_id=?,
                budget_action='block', budget_reason_code='budget.exhausted.block',
                budget_current_cost_cny=1.123457, budget_limit_cny=1.000001
            WHERE tenant_id=? AND request_id=?
            """,
            (metadata["virtual_key_id"], fixture.tenant_id, SOURCE_REQUEST_ID),
        )
        connection.commit()
    finally:
        connection.close()

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_budget_block"),
        )

    assert response.status_code == 200, response.text
    explanation = response.json()["request_explanation"]
    assert _stage(explanation, "budget")["status"] == "blocked"
    assert explanation["dlp"]["evidence_state"] == "not_applicable"
    by_kind = {fact["kind"]: fact["values"] for fact in explanation["budget"]["facts"]}
    assert by_kind["budget_evaluation"]["spent_before_cny"] == "0.123456"
    assert by_kind["budget_evaluation"]["projected_cost_cny"] == "1.123457"
    assert by_kind["budget_scope_event"]["posted_cost_micros"] == "999999999999997"
    assert by_kind["budget_scope_event"]["projected_cost_micros"] == "1000000000000000"
    assert isinstance(by_kind["budget_scope_event"]["projected_cost_micros"], str)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("attempt_status", "source_status", "expected_stage", "expected_evidence"),
    [
        ("succeeded", "success", "passed", "complete"),
        ("failed", "failed", "failed", "complete"),
        ("started", "success", "pending", "pending"),
    ],
)
async def test_prd054_provider_attempt_uses_database_status_enum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    attempt_status: str,
    source_status: str,
    expected_stage: str,
    expected_evidence: str,
) -> None:
    """验证 started/succeeded/failed 与 Provider 阶段公开状态逐项一致。"""

    fixture = _prepare_fixture(monkeypatch, tmp_path)
    metadata = _source_metadata(fixture.database_url, fixture.tenant_id)
    connection = connect_database(fixture.database_url)
    try:
        decision_id = f"rtd_prd054_{attempt_status}"
        attempt_id = f"rta_prd054_{attempt_status}"
        connection.execute(
            """
            INSERT INTO routing_decisions(
              routing_decision_id, tenant_id, request_id, trace_id,
              virtual_key_id, requested_model, environment, environment_id,
              selected_model_id, selected_internal_model_id,
              provider_id, provider_code, decision_status, decision_reason,
              candidate_summary, created_at
            ) VALUES(
              ?, ?, ?, 'trc_prd054_explain', ?, 'company-default', ?, ?,
              ?, 'default-chat', ?, ?, 'selected', 'routing.fixed_priority',
              '{"selected":true}', ?
            )
            """,
            (
                decision_id,
                fixture.tenant_id,
                SOURCE_REQUEST_ID,
                metadata["virtual_key_id"],
                metadata["environment_code"],
                metadata["environment_id"],
                metadata["model_id"],
                metadata["provider_id"],
                metadata["provider_code"],
                FIXTURE_TIME,
            ),
        )
        connection.execute(
            """
            INSERT INTO routing_attempts(
              routing_attempt_id, tenant_id, request_id, trace_id,
              attempt_sequence, attempt_role, trigger_type, model_id,
              internal_model_id, provider_id, provider_code, status,
              error_code, error_category, upstream_http_status, retryable,
              latency_ms, usage_status, created_at, completed_at, updated_at
            ) VALUES(
              ?, ?, ?, 'trc_prd054_explain', 1, 'primary', 'initial', ?,
              'default-chat', ?, ?, ?, ?, ?, ?, ?, 37, ?, ?, ?, ?
            )
            """,
            (
                attempt_id,
                fixture.tenant_id,
                SOURCE_REQUEST_ID,
                metadata["model_id"],
                metadata["provider_id"],
                metadata["provider_code"],
                attempt_status,
                "gateway.upstream_error" if attempt_status == "failed" else None,
                "upstream_5xx" if attempt_status == "failed" else None,
                503 if attempt_status == "failed" else 200 if attempt_status == "succeeded" else None,
                1 if attempt_status == "failed" else 0,
                "unavailable" if attempt_status == "failed" else "pending",
                FIXTURE_TIME,
                None if attempt_status == "started" else FIXTURE_TIME,
                FIXTURE_TIME,
            ),
        )
        connection.execute(
            """
            UPDATE request_logs
            SET status=?, error_code=?, provider_id=?, model_id=?,
                provider_code=?, routing_attempt_count=1,
                final_routing_attempt_id=?
            WHERE tenant_id=? AND request_id=?
            """,
            (
                source_status,
                "gateway.upstream_error" if source_status == "failed" else None,
                metadata["provider_id"],
                metadata["model_id"],
                metadata["provider_code"],
                attempt_id,
                fixture.tenant_id,
                SOURCE_REQUEST_ID,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, f"req_prd054_provider_{attempt_status}"),
        )

    assert response.status_code == 200, response.text
    explanation = response.json()["request_explanation"]
    assert explanation["provider_response"]["evidence_state"] == expected_evidence
    assert _stage(explanation, "provider")["status"] == expected_stage
    provider_fact = explanation["provider_response"]["facts"][0]
    assert provider_fact["values"]["status"] == attempt_status
    assert "error_summary" not in provider_fact["values"]


@pytest.mark.anyio
async def test_prd054_database_missing_table_lock_and_connection_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证缺表、写锁统一失败关闭，且失败路径不遗留连接或访问事实。"""

    fixture = _prepare_fixture(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)

        connection = connect_database(fixture.database_url)
        try:
            connection.execute(
                "ALTER TABLE routing_candidates RENAME TO routing_candidates_prd054_missing"
            )
            connection.commit()
        finally:
            connection.close()
        missing_table = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_missing_table"),
        )
        _assert_error(missing_table, 503, "request_explanation.unavailable")

        # 能立即改回表名，证明缺表异常的只读事务和连接均已在 finally 释放。
        connection = connect_database(fixture.database_url)
        try:
            connection.execute(
                "ALTER TABLE routing_candidates_prd054_missing RENAME TO routing_candidates"
            )
            connection.commit()
        finally:
            connection.close()
        recovered = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_after_missing_table"),
        )
        assert recovered.status_code == 200, recovered.text

        config = load_app_config("control-plane-api")
        # 真实 Bearer 鉴权在加锁前完成，锁场景只考察 Explain 的读/审计事务边界。
        context = authenticate_bearer(config, f"Bearer {admin}")
        original_connect = explanation_service.connect_explanation_database

        def short_timeout_connect(config: Any) -> sqlite3.Connection:
            """创建真实 SQLite 连接并缩短专项锁等待，避免测试无意义等待五秒。"""

            database = original_connect(config)
            database.execute("PRAGMA busy_timeout=1")
            return database

        monkeypatch.setattr(
            explanation_service,
            "connect_explanation_database",
            short_timeout_connect,
        )
        blocker = connect_database(fixture.database_url)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with pytest.raises(ApiError) as locked_failure:
                explanation_service.get_request_explanation(
                    config,
                    context,
                    SOURCE_REQUEST_ID,
                    request_id="req_prd054_audit_locked",
                    trace_id="trc_prd054_audit_locked",
                )
            assert locked_failure.value.code == "request_explanation.audit_unavailable"
        finally:
            blocker.rollback()
            blocker.close()

        after_lock = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_after_lock"),
        )

    assert after_lock.status_code == 200, after_lock.text
    connection = connect_database(fixture.database_url)
    try:
        count = connection.execute(
            "SELECT COUNT(*) AS total FROM request_explanations"
        ).fetchone()
        assert count is not None and int(count["total"]) == 2
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd054_corrupted_outbox_json_rolls_back_claim_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """模拟物理损坏 JSON，验证 worker 返回固定错误且 claim 更新完整回滚。"""

    fixture = _prepare_fixture(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_corrupt_json_source"),
        )
    assert created.status_code == 200, created.text

    connection = connect_database(fixture.database_url)
    try:
        # 测试库临时移除不可变触发器并忽略 CHECK，等价模拟磁盘/旧版本造成的物理坏行；
        # 生产迁移本身仍由迁移专项证明会拒绝此写入。
        connection.execute("DROP TRIGGER trg_request_explanations_state_update_prd054")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE request_explanations SET missing_sections_json='{'"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ApiError) as failure:
        outbox.claim_next_request_explanation_event(
            load_app_config("control-plane-api"),
            worker_id="worker-prd054-corrupt-json",
            now=datetime.now(UTC) + timedelta(seconds=2),
        )
    assert failure.value.code == "request_explanation.event_unavailable"

    connection = connect_database(fixture.database_url)
    try:
        row = connection.execute(
            """
            SELECT event_dispatch_status, event_dispatch_attempts
            FROM request_explanations
            """
        ).fetchone()
        assert row is not None
        assert row["event_dispatch_status"] == "pending"
        assert row["event_dispatch_attempts"] == 0
        # BEGIN EXCLUSIVE 可立即获得锁，证明 JSON 解析异常路径已经关闭 worker 连接。
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd054_scheduler_composes_lifespan_and_waits_for_active_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 scheduler 不覆盖既有 lifespan，关闭时等待已经开始的同步 tick。"""

    events: list[str] = []
    started = threading.Event()
    release = threading.Event()

    @asynccontextmanager
    async def original_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """记录被组合生命周期的进入和退出顺序。"""

        events.append("original.enter")
        try:
            yield
        finally:
            events.append("original.exit")

    def blocking_tick(_config: Any) -> bool:
        """模拟已经进入 SQLite/网络边界且不能用 task.cancel 中断的一轮工作。"""

        events.append("worker.start")
        started.set()
        assert release.wait(timeout=3)
        events.append("worker.finish")
        return False

    app = FastAPI(lifespan=original_lifespan)
    app.state.config = SimpleNamespace(
        request_explanation_event_enabled=True,
        request_explanation_event_poll_interval_seconds=60.0,
    )
    monkeypatch.setattr(scheduler, "run_request_explanation_event_once", blocking_tick)
    scheduler.install_request_explanation_scheduler(app)

    manager = app.router.lifespan_context(app)
    await manager.__aenter__()
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()

    exit_task = asyncio.create_task(manager.__aexit__(None, None, None))
    await asyncio.sleep(0.02)
    assert exit_task.done() is False
    assert "original.exit" not in events
    release.set()
    await asyncio.wait_for(exit_task, timeout=2)
    assert events == [
        "original.enter",
        "worker.start",
        "worker.finish",
        "original.exit",
    ]
