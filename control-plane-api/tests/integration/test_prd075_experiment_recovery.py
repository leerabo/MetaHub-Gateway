"""PRD-075 过期执行、Usage spool 与完成 Outbox 的联合恢复反例。"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import test_prd075_experiments_control as control_fixtures
from httpx import ASGITransport, AsyncClient
from metahub_control.experiments.reaper import reap_expired_executions
from metahub_control.experiments.scheduler import run_experiment_scheduler_once
from metahub_gateway.usage.ledger import (
    RequestLogContext,
    _prepare_usage_ledger_intent,
    _serialize_usage_spool_record,
    claim_gateway_request,
    record_request_log,
    record_usage_ledger,
    replay_usage_spool_record,
)
from metahub_shared.db import connect_database

# 复用同目录的只读迁移模板；测试本身仍为每个用例复制独立 SQLite 数据库。
prd075_control_template = control_fixtures.prd075_control_template

# 固定安全 ID 只用于串联 Assignment、Attempt、日志和错误断言，不包含客户或上游正文。
_REQUEST_ID = "req_prd075_reaper"
_TRACE_ID = "trace_prd075_reaper"
_ATTEMPT_ID = "rta_prd075_recovery_success"
_ERROR_CODE = "gateway.experiment_unavailable"


async def _create_experiment(
    app: object,
    graph: dict[str, str],
    *,
    suffix: str,
) -> tuple[str, datetime]:
    """通过真实 Control API 创建短时实验并返回采样截止时间。"""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await control_fixtures._login(
            client,
            email="admin@metahub.local",
            password=control_fixtures.ADMIN_PASSWORD,
        )
        created = await client.post(
            "/experiments",
            json=control_fixtures._payload(
                graph,
                name=f"PRD075 recovery {suffix}",
                duration=60,
            ),
            headers=control_fixtures._headers(
                token,
                f"prd075-recovery-create-{suffix}",
                f"75000000-0000-4000-8000-{suffix:0>12}",
            ),
        )
    assert created.status_code == 201, created.text
    body = created.json()["experiment"]
    deadline = datetime.fromisoformat(str(body["sampling_deadline"]).replace("Z", "+00:00"))
    return str(body["experiment_id"]), deadline


def _stage_provider_success(
    connection,
    graph: dict[str, str],
    experiment_id: str,
) -> tuple[str, datetime]:
    """建立已越过 send fence 的 Provider 成功事实，但故意不写 Outcome。"""

    execution_id, reap_at = control_fixtures._insert_expired_assignment(
        connection,
        graph,
        experiment_id,
        lease_seconds=2,
    )
    execution = connection.execute(
        """
        SELECT created_at FROM experiment_request_executions
        WHERE tenant_id=? AND execution_id=?
        """,
        (graph["tenant_id"], execution_id),
    ).fetchone()
    assert execution is not None
    fact_at = str(execution["created_at"])
    connection.execute(
        """
        UPDATE experiment_request_executions
        SET status='provider_calling',send_fence_at=?,updated_at=?
        WHERE tenant_id=? AND execution_id=? AND generation=1 AND status='assigned'
        """,
        (fact_at, fact_at, graph["tenant_id"], execution_id),
    )
    assert connection.execute("SELECT changes()").fetchone()[0] == 1
    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id,tenant_id,request_id,trace_id,model_alias_id,
          attempt_sequence,attempt_role,trigger_type,model_id,internal_model_id,
          provider_id,provider_code,status,upstream_http_status,retryable,latency_ms,
          usage_status,created_at,completed_at,updated_at
        ) VALUES(?,?,?,?,?,1,'primary','initial',?,?,?,?,'succeeded',200,0,41,
                 'pending',?,?,?)
        """,
        (
            _ATTEMPT_ID,
            graph["tenant_id"],
            _REQUEST_ID,
            _TRACE_ID,
            graph["model_alias_id"],
            graph["model_a_id"],
            graph["model_a_id"],
            graph["provider_id"],
            graph["provider_code"],
            fact_at,
            fact_at,
            fact_at,
        ),
    )
    connection.commit()
    return execution_id, reap_at


def _experiment_context(graph: dict[str, str]) -> RequestLogContext:
    """构造与 Assignment、Scope 和 Provider Attempt 完全一致的租户安全 spool owner。"""

    return RequestLogContext(
        tenant_id=graph["tenant_id"],
        request_id=_REQUEST_ID,
        trace_id=_TRACE_ID,
        virtual_key_id=graph["virtual_key_id"],
        environment_id=graph["environment_id"],
        environment_code=graph["environment_code"],
        project_id=graph["project_id"],
        application_id=graph["application_id"],
        provider_id=graph["provider_id"],
        model_id=graph["model_a_id"],
        endpoint="chat/completions",
        request_model=graph["alias"],
        resolved_model=graph["alias"],
        provider_code=graph["provider_code"],
        routing_attempt_count=1,
        final_routing_attempt_id=_ATTEMPT_ID,
        routing_attempt_id=_ATTEMPT_ID,
    )


def _usage() -> dict[str, object]:
    """返回只含合成计数且不含 Prompt、Response、Key 或 Token 的安全用量。"""

    return {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "total_tokens": 8,
        "cache_tokens": 0,
        "estimated": False,
        "usage_source": "upstream",
        "raw_usage": {"prompt_tokens": 3, "completion_tokens": 5},
    }


def _wait_until_expired(database_url: str, tenant_id: str, execution_id: str) -> None:
    """等待两秒测试租约按 SQLite 时钟到期，避免伪造数据库当前时间。"""

    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        connection = connect_database(database_url)
        try:
            expired = connection.execute(
                """
                SELECT unixepoch(lease_expires_at)<=unixepoch('now')
                FROM experiment_request_executions
                WHERE tenant_id=? AND execution_id=?
                """,
                (tenant_id, execution_id),
            ).fetchone()
        finally:
            connection.close()
        if expired is not None and bool(expired[0]):
            return
        time.sleep(0.05)
    # 租约未按数据库时钟到期会使恢复反例失真，因此必须立即停止当前测试。
    raise AssertionError("experiment execution lease did not expire")


def _ordinary_spool_context(graph: dict[str, str]) -> RequestLogContext:
    """构造同租户但没有 Experiment Assignment 的普通请求对照组。"""

    return RequestLogContext(
        tenant_id=graph["tenant_id"],
        request_id="req_prd075_ordinary_spool",
        trace_id="trace_prd075_ordinary_spool",
        virtual_key_id=graph["virtual_key_id"],
        environment_id=graph["environment_id"],
        environment_code=graph["environment_code"],
        project_id=graph["project_id"],
        application_id=graph["application_id"],
        provider_id=graph["provider_id"],
        model_id=graph["model_a_id"],
        request_model=graph["alias"],
        resolved_model=graph["alias"],
        provider_code=graph["provider_code"],
    )


@pytest.mark.anyio
async def test_reaper_downgrades_residual_success_and_preserves_provider_quality(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Outcome CAS 丢失且失败日志改写也丢失时，Reaper 不得恢复 Gateway success。"""

    database_url, graphs = control_fixtures._clone_environment(
        prd075_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    experiment_id, _ = await _create_experiment(app, graphs["c"], suffix="41")
    connection = connect_database(database_url)
    try:
        execution_id, reap_at = _stage_provider_success(connection, graphs["c"], experiment_id)
    finally:
        connection.close()
    context = _experiment_context(graphs["c"])
    record_request_log(app.state.config, context, status="success", error_code=None, latency_ms=41)
    committed = record_usage_ledger(app.state.config, context, _usage())
    assert committed.created is True

    # 同 request_id 的另一租户日志用于证明 Reaper 的降级不会越过 tenant owner。
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd075_recovery_other','prd075-recovery-other',
                   'PRD075 recovery other','internal_only','active',
                   '2026-08-10T00:00:00Z','2026-08-10T00:00:00Z')
            """
        )
        connection.commit()
    finally:
        connection.close()
    other_context = RequestLogContext(
        tenant_id="tenant_prd075_recovery_other",
        request_id=_REQUEST_ID,
        trace_id="trace_prd075_recovery_other",
    )
    claim_gateway_request(app.state.config, other_context)
    record_request_log(
        app.state.config,
        other_context,
        status="success",
        error_code=None,
        latency_ms=9,
    )
    _wait_until_expired(database_url, graphs["c"]["tenant_id"], execution_id)

    connection = connect_database(database_url)
    try:
        before = connection.execute(
            """
            SELECT status,error_code FROM request_logs
            WHERE tenant_id=? AND request_id=?
            """,
            (graphs["c"]["tenant_id"], _REQUEST_ID),
        ).fetchone()
        assert tuple(before) == ("success", None)
        connection.execute("BEGIN IMMEDIATE")
        assert (
            reap_expired_executions(
                connection,
                tenant_id=graphs["c"]["tenant_id"],
                experiment_id=experiment_id,
                now=reap_at,
            )
            == 1
        )
        connection.commit()
        request_log = connection.execute(
            """
            SELECT status,error_code,latency_ms,total_tokens
            FROM request_logs WHERE tenant_id=? AND request_id=?
            """,
            (graphs["c"]["tenant_id"], _REQUEST_ID),
        ).fetchone()
        outcome = connection.execute(
            """
            SELECT gateway_status,exposure_status,error_code,quality_eligible,
                   quality_succeeded,latency_ms,routing_attempt_id
            FROM experiment_request_outcomes
            WHERE tenant_id=? AND execution_id=?
            """,
            (graphs["c"]["tenant_id"], execution_id),
        ).fetchone()
        other = connection.execute(
            """
            SELECT status,error_code FROM request_logs
            WHERE tenant_id='tenant_prd075_recovery_other' AND request_id=?
            """,
            (_REQUEST_ID,),
        ).fetchone()
        assert tuple(request_log) == ("failed", _ERROR_CODE, 41, 8)
        assert tuple(outcome) == ("failed", "exposed", _ERROR_CODE, 1, 1, 41, _ATTEMPT_ID)
        assert tuple(other) == ("success", None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_reaper_rolls_back_when_success_request_log_owner_is_incomplete(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """残留 success 缺 Model/Provider owner 时不得同时提交 unresolved 实验终态。"""

    database_url, graphs = control_fixtures._clone_environment(
        prd075_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    experiment_id, _ = await _create_experiment(app, graphs["c"], suffix="43")
    connection = connect_database(database_url)
    try:
        execution_id, reap_at = _stage_provider_success(connection, graphs["c"], experiment_id)
    finally:
        connection.close()
    context = _experiment_context(graphs["c"])
    record_request_log(app.state.config, context, status="success", error_code=None, latency_ms=41)
    _wait_until_expired(database_url, graphs["c"]["tenant_id"], execution_id)

    connection = connect_database(database_url)
    try:
        # 0068 为早期失败日志保留可空 owner；success 出现该形态则属于不可恢复矛盾事实。
        connection.execute(
            """
            UPDATE request_logs SET model_id=NULL,provider_id=NULL,provider_code=NULL
            WHERE tenant_id=? AND request_id=?
            """,
            (graphs["c"]["tenant_id"], _REQUEST_ID),
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="experiment_request_log_recovery_conflict",
        ):
            reap_expired_executions(
                connection,
                tenant_id=graphs["c"]["tenant_id"],
                experiment_id=experiment_id,
                now=reap_at,
            )
        connection.rollback()
        request_log = connection.execute(
            """
            SELECT status,error_code,model_id,provider_id FROM request_logs
            WHERE tenant_id=? AND request_id=?
            """,
            (graphs["c"]["tenant_id"], _REQUEST_ID),
        ).fetchone()
        execution = connection.execute(
            """
            SELECT status,generation FROM experiment_request_executions
            WHERE tenant_id=? AND execution_id=?
            """,
            (graphs["c"]["tenant_id"], execution_id),
        ).fetchone()
        outcome_count = connection.execute(
            """
            SELECT COUNT(*) FROM experiment_request_outcomes
            WHERE tenant_id=? AND execution_id=?
            """,
            (graphs["c"]["tenant_id"], execution_id),
        ).fetchone()[0]
        assert tuple(request_log) == ("success", None, None, None)
        assert tuple(execution) == ("provider_calling", 1)
        assert outcome_count == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_experiment_spool_replay_cannot_create_success_or_success_outbox(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """缺失 RequestLog 的实验 spool 只能补失败，并贯穿 Reaper、结果和 Outbox。"""

    database_url, graphs = control_fixtures._clone_environment(
        prd075_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    experiment_id, sampling_deadline = await _create_experiment(
        app,
        graphs["c"],
        suffix="42",
    )
    connection = connect_database(database_url)
    try:
        execution_id, _ = _stage_provider_success(connection, graphs["c"], experiment_id)
    finally:
        connection.close()

    experiment_context = _experiment_context(graphs["c"])
    experiment_record = _serialize_usage_spool_record(
        experiment_context,
        _prepare_usage_ledger_intent(_usage()),
    )
    assert replay_usage_spool_record(app.state.config, experiment_record).created is True

    # 普通请求保持历史兼容：缺失 RequestLog 的 spool 仍恢复 success，而非被实验规则误伤。
    ordinary_context = _ordinary_spool_context(graphs["c"])
    claim_gateway_request(app.state.config, ordinary_context)
    ordinary_record = _serialize_usage_spool_record(
        ordinary_context,
        _prepare_usage_ledger_intent(_usage()),
    )
    assert replay_usage_spool_record(app.state.config, ordinary_record).created is True
    _wait_until_expired(database_url, graphs["c"]["tenant_id"], execution_id)

    assert (
        run_experiment_scheduler_once(
            app.state.config,
            now=sampling_deadline + timedelta(seconds=1),
        )
        is True
    )
    connection = connect_database(database_url)
    try:
        logs = {
            str(row["request_id"]): (str(row["status"]), row["error_code"])
            for row in connection.execute(
                """
                SELECT request_id,status,error_code FROM request_logs
                WHERE tenant_id=? AND request_id IN (?,?)
                """,
                (
                    graphs["c"]["tenant_id"],
                    experiment_context.request_id,
                    ordinary_context.request_id,
                ),
            ).fetchall()
        }
        assert logs == {
            experiment_context.request_id: ("failed", _ERROR_CODE),
            ordinary_context.request_id: ("success", None),
        }
        outcome = connection.execute(
            """
            SELECT gateway_status,exposure_status,quality_eligible,quality_succeeded,error_code
            FROM experiment_request_outcomes
            WHERE tenant_id=? AND experiment_id=?
            """,
            (graphs["c"]["tenant_id"], experiment_id),
        ).fetchone()
        assert tuple(outcome) == ("failed", "exposed", 1, 1, _ERROR_CODE)
        result = connection.execute(
            """
            SELECT assigned_count,gateway_success_count,gateway_failure_count,
                   exposed_count,quality_success_count,settlement_recorded_count
            FROM experiment_results
            WHERE tenant_id=? AND experiment_id=? AND assigned_count=1
            """,
            (graphs["c"]["tenant_id"], experiment_id),
        ).fetchone()
        assert tuple(result) == (1, 0, 1, 1, 1, 1)
        event = connection.execute(
            """
            SELECT event.payload_json,outbox.dispatch_status
            FROM experiment_events event
            JOIN experiment_event_outbox outbox
              ON outbox.tenant_id=event.tenant_id
             AND outbox.experiment_event_id=event.experiment_event_id
            WHERE event.tenant_id=? AND event.experiment_id=?
              AND event.event_type='experiment.completed'
            """,
            (graphs["c"]["tenant_id"], experiment_id),
        ).fetchone()
        payload = json.loads(str(event["payload_json"]))
        assert event["dispatch_status"] == "pending"
        assert payload["gateway_success_count"] == 0
        assert payload["gateway_failure_count"] == 1
        assert payload["unresolved_count"] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
