"""PRD-078 timeout、事件 outbox 与 Session Risk 写回恢复专项测试。

覆盖租约接管、重启恢复、跨租户拒绝和失败状态追踪，证据中不得出现任何敏感原文。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_prd078_approvals_control as control_fixtures
from httpx import ASGITransport, AsyncClient
from metahub_control.approvals import outbox, scheduler, service
from metahub_control.webhooks import service as webhook_service
from metahub_gateway.session_risk.store import InMemorySessionRiskStore
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

prd078_control_template = control_fixtures.prd078_control_template
approval_fixture = control_fixtures.approval_fixture


def _parse_time(value: object) -> datetime:
    """``_parse_time`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed.astimezone(UTC)


def _insert_approval_endpoint(database_url: str, endpoint_id: str) -> None:
    """插入只订阅两个审批事件的合成 Endpoint，测试不读取其私有材料。"""

    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """
            SELECT tenant_id,user_id FROM users
            WHERE email='admin@metahub.local' AND status='active'
            """
        ).fetchone()
        assert owner is not None
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
              max_attempts,created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,'PRD078 approval sink','https://example.invalid/prd078',
                     '["approval.created","approval.timeout"]','synthetic-ciphertext',
                     'whsec_test','prd078-synthetic-fingerprint','active',5000,3,
                     ?,?,?,?)
            """,
            (
                endpoint_id,
                owner["tenant_id"],
                owner["user_id"],
                owner["user_id"],
                control_fixtures.NOW,
                control_fixtures.NOW,
            ),
        )
        connection.commit()
    finally:
        connection.close()


async def _create_budget(
    app: object,
    fixture: control_fixtures.ApprovalFixture,
    suffix: str,
    *,
    key: str,
    tenant_id: str | None = None,
) -> str:
    """``_create_budget`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await control_fixtures._create(
            client,
            fixture,
            suffix,
            key=key,
            tenant_id=tenant_id,
        )
    assert response.status_code == 201, response.text
    return str(response.json()["approval"]["approval_id"])


async def _approve_risk(
    app: object,
    fixture: control_fixtures.ApprovalFixture,
    score_name: str,
    *,
    suffix: str,
) -> str:
    """经真实路由完成 step-up 与批准，留下待恢复的持久 writeback。"""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await control_fixtures._create_risk(
            client,
            fixture,
            score_name,
            key=f"prd078-recovery-create-{suffix}",
        )
        assert created.status_code == 201, created.text
        approval_id = str(created.json()["approval"]["approval_id"])
        token = await control_fixtures._login(client, "security_admin")
        verified = await client.post(
            f"/approvals/{approval_id}/step-up",
            headers=control_fixtures._auth(token),
            json={"action": "approve", "password": control_fixtures.ROLE_PASSWORD},
        )
        assert verified.status_code == 200, verified.text
        approved = await client.post(
            f"/approvals/{approval_id}/approve",
            headers={
                **control_fixtures._auth(token),
                "Idempotency-Key": f"prd078-recovery-decide-{suffix}",
            },
            json={
                "expected_revision": 1,
                "reason_code": "approved_business_need",
                "step_up_token": verified.json()["step_up_token"],
            },
        )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"]["writeback_status"] == "pending"
    return approval_id


@pytest.mark.anyio
async def test_scheduler_fair_claim_takeover_and_stale_owner_rejected(
    approval_fixture: control_fixtures.ApprovalFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同租户各获一次 claim；过期 lease 可接管，旧 owner 不能终结任务。"""

    app = control_fixtures._build_app()
    await _create_budget(
        app,
        approval_fixture,
        "approve",
        key="prd078-scheduler-local",
    )
    await _create_budget(
        app,
        approval_fixture,
        "foreign",
        key="prd078-scheduler-foreign",
        tenant_id=approval_fixture.foreign_tenant_id,
    )
    connection = connect_database(approval_fixture.database_url)
    try:
        due = max(
            _parse_time(row[0])
            for row in connection.execute("SELECT next_scan_at FROM approval_scheduler_tenants")
        ) + timedelta(seconds=1)
    finally:
        connection.close()

    first = scheduler.claim_next_scheduler_tenant(
        app.state.config,
        worker_id="prd078-scheduler-old",
        now=due,
    )
    assert first is not None
    second = scheduler.claim_next_scheduler_tenant(
        app.state.config,
        worker_id="prd078-scheduler-peer",
        now=due + timedelta(seconds=10),
    )
    assert second is not None
    assert {first.tenant_id, second.tenant_id} == {
        approval_fixture.tenant_id,
        approval_fixture.foreign_tenant_id,
    }
    assert (
        scheduler.claim_next_scheduler_tenant(
            app.state.config,
            worker_id="prd078-scheduler-early",
            now=first.lease_expires_at - timedelta(seconds=1),
        )
        is None
    )

    takeover_at = first.lease_expires_at + timedelta(seconds=1)
    takeover = scheduler.claim_next_scheduler_tenant(
        app.state.config,
        worker_id="prd078-scheduler-new",
        now=takeover_at,
    )
    assert takeover is not None
    assert takeover.tenant_id == first.tenant_id
    assert takeover.lease_generation == first.lease_generation + 1
    assert takeover.lease_token != first.lease_token

    monkeypatch.setattr(service, "_database_now", lambda _connection: takeover_at)
    with pytest.raises(ApiError) as stale:
        service.timeout_one_due_approval(
            app.state.config,
            first,
            request_id="req_prd078_stale_timeout",
            trace_id="trace_prd078_stale_timeout",
            next_scan_delay_seconds=1,
        )
    assert stale.value.code == "approval.unavailable"

    connection = connect_database(approval_fixture.database_url)
    try:
        row = connection.execute(
            """
            SELECT status,lease_generation,lease_owner_id,lease_token
            FROM approval_scheduler_tenants WHERE tenant_id=?
            """,
            (takeover.tenant_id,),
        ).fetchone()
        assert tuple(row) == (
            "leased",
            takeover.lease_generation,
            takeover.lease_owner_id,
            takeover.lease_token,
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM approval_tasks WHERE status!='pending'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_timeout_commits_task_decision_writeback_event_and_lease_atomically(
    approval_fixture: control_fixtures.ApprovalFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一次 timeout 同时产生唯一终态、Decision、no-effect writeback 与事件 source。"""

    app = control_fixtures._build_app()
    approval_id = await _create_budget(
        app,
        approval_fixture,
        "approve",
        key="prd078-timeout-atomic",
    )
    connection = connect_database(approval_fixture.database_url)
    try:
        task = connection.execute(
            "SELECT expires_at FROM approval_tasks WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()
        assert task is not None
        timeout_at = _parse_time(task["expires_at"]) + timedelta(seconds=1)
    finally:
        connection.close()

    lease = scheduler.claim_next_scheduler_tenant(
        app.state.config,
        worker_id="prd078-timeout-worker",
        now=timeout_at,
    )
    assert lease is not None and lease.tenant_id == approval_fixture.tenant_id
    monkeypatch.setattr(service, "_database_now", lambda _connection: timeout_at)
    result = service.timeout_one_due_approval(
        app.state.config,
        lease,
        request_id="req_prd078_timeout_atomic",
        trace_id="trace_prd078_timeout_atomic",
        next_scan_delay_seconds=2,
    )
    assert result is not None and result.approval_id == approval_id

    connection = connect_database(approval_fixture.database_url)
    try:
        task = connection.execute(
            """
            SELECT status,resolution_reason,state_revision,writeback_status,
                   decided_by_user_id,decision_request_id,decision_trace_id
            FROM approval_tasks WHERE approval_task_id=?
            """,
            (approval_id,),
        ).fetchone()
        assert tuple(task) == (
            "rejected",
            "timeout",
            2,
            "completed",
            None,
            "req_prd078_timeout_atomic",
            "trace_prd078_timeout_atomic",
        )
        decision = connection.execute(
            """
            SELECT decision,reason_code,actor_type,actor_id,revision_before,
                   revision_after,step_up_used,writeback_status
            FROM approval_decisions WHERE approval_task_id=?
            """,
            (approval_id,),
        ).fetchone()
        assert tuple(decision) == (
            "timeout",
            "timeout",
            "system",
            "approval-timeout-scheduler",
            1,
            2,
            0,
            "completed",
        )
        writeback = connection.execute(
            """
            SELECT adapter_kind,status,attempt_count,target_fact_id,target_revision
            FROM approval_writebacks WHERE approval_task_id=?
            """,
            (approval_id,),
        ).fetchone()
        assert tuple(writeback) == ("no_effect", "completed", 0, None, None)
        events = connection.execute(
            """
            SELECT event.event_type,outbox.status
            FROM approval_events event JOIN approval_event_outbox outbox
              ON outbox.tenant_id=event.tenant_id
             AND outbox.approval_event_id=event.approval_event_id
            WHERE event.approval_task_id=? ORDER BY event.event_type
            """,
            (approval_id,),
        ).fetchall()
        assert [tuple(row) for row in events] == [
            ("approval.created", "pending"),
            ("approval.timeout", "pending"),
        ]
        scheduler_row = connection.execute(
            """
            SELECT status,lease_generation,lease_owner_id,lease_token,lease_expires_at
            FROM approval_scheduler_tenants WHERE tenant_id=?
            """,
            (approval_fixture.tenant_id,),
        ).fetchone()
        assert tuple(scheduler_row) == ("idle", lease.lease_generation + 1, None, None, None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_event_snapshot_stale_owner_backoff_and_tenth_failure(
    approval_fixture: control_fixtures.ApprovalFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """created 快照不吸收后加 Endpoint，旧 worker 零出网，十次失败后原子终结。"""

    primary_endpoint = "wh_prd078_primary"
    late_endpoint = "wh_prd078_late"
    _insert_approval_endpoint(approval_fixture.database_url, primary_endpoint)
    app = control_fixtures._build_app()
    approval_id = await _create_budget(
        app,
        approval_fixture,
        "approve",
        key="prd078-event-recovery",
    )
    connection = connect_database(approval_fixture.database_url)
    try:
        source = connection.execute(
            """
            SELECT event.approval_event_id,outbox.next_attempt_at,
                   outbox.subscription_snapshot_json
            FROM approval_events event JOIN approval_event_outbox outbox
              ON outbox.tenant_id=event.tenant_id
             AND outbox.approval_event_id=event.approval_event_id
            WHERE event.approval_task_id=? AND event.event_type='approval.created'
            """,
            (approval_id,),
        ).fetchone()
        assert source is not None
        event_id = str(source["approval_event_id"])
        first_at = _parse_time(source["next_attempt_at"]) + timedelta(seconds=1)
        frozen_ids = json.loads(str(source["subscription_snapshot_json"]))
        assert len(frozen_ids) == 1
    finally:
        connection.close()

    first = outbox.claim_next_approval_event(
        app.state.config,
        worker_id="prd078-event-old",
        now=first_at,
    )
    assert first is not None and first.event_id == event_id and first.attempt_count == 1
    _insert_approval_endpoint(approval_fixture.database_url, late_endpoint)
    takeover = outbox.claim_next_approval_event(
        app.state.config,
        worker_id="prd078-event-new",
        now=first.lease_expires_at + timedelta(seconds=1),
    )
    assert takeover is not None and takeover.attempt_count == 2
    assert takeover.snapshot_json == first.snapshot_json

    calls: list[str] = []

    def fail_delivery(*_args: object, **_kwargs: object) -> None:
        """``fail_delivery`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
        calls.append("attempt")
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise RuntimeError("synthetic unavailable")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", fail_delivery)
    assert (
        outbox.dispatch_claimed_approval_event(
            app.state.config,
            first,
            now=takeover.lease_expires_at - timedelta(seconds=1),
        )
        is False
    )
    assert calls == []

    current = first.lease_expires_at + timedelta(seconds=1)
    claim = takeover
    for expected_attempt in range(2, outbox.MAX_ATTEMPTS + 1):
        assert claim.attempt_count == expected_attempt
        assert outbox.dispatch_claimed_approval_event(
            app.state.config,
            claim,
            now=current,
        )
        if expected_attempt == outbox.MAX_ATTEMPTS:
            break
        connection = connect_database(approval_fixture.database_url)
        try:
            next_at = connection.execute(
                """
                SELECT next_attempt_at FROM approval_event_outbox
                WHERE approval_event_id=?
                """,
                (event_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        current = _parse_time(next_at)
        claim = outbox.claim_next_approval_event(
            app.state.config,
            worker_id=f"prd078-event-retry-{expected_attempt + 1}",
            now=current,
        )
        assert claim is not None

    assert len(calls) == outbox.MAX_ATTEMPTS - 1
    assert (
        outbox.claim_next_approval_event(
            app.state.config,
            worker_id="prd078-event-no-eleventh",
            now=current + timedelta(hours=1),
        )
        is None
    )
    connection = connect_database(approval_fixture.database_url)
    try:
        terminal = connection.execute(
            """
            SELECT status,attempt_count,last_error_code,subscription_snapshot_json,
                   lease_owner_id,lease_token
            FROM approval_event_outbox WHERE approval_event_id=?
            """,
            (event_id,),
        ).fetchone()
        assert tuple(terminal[:3]) == (
            "failed",
            outbox.MAX_ATTEMPTS,
            "webhook.delivery_unavailable",
        )
        assert json.loads(str(terminal["subscription_snapshot_json"])) == frozen_ids
        assert terminal["lease_owner_id"] is None and terminal["lease_token"] is None
        deliveries = connection.execute(
            """
            SELECT webhook_endpoint_id,status,error_code FROM webhook_deliveries
            WHERE event_id=? AND delivery_kind='original'
            """,
            (event_id,),
        ).fetchall()
        assert [tuple(row) for row in deliveries] == [
            (primary_endpoint, "failed", "webhook.delivery_unavailable")
        ]
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM webhook_deliveries
                WHERE webhook_endpoint_id=? AND event_id=?
                """,
                (late_endpoint, event_id),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_session_risk_writeback_takeover_reconciles_store_and_rejects_late_owner(
    approval_fixture: control_fixtures.ApprovalFixture,
) -> None:
    """崩溃后的 writeback 可接管并恢复 Redis；旧 owner 不能完成新租约。"""

    app = control_fixtures._build_app()
    approval_id = await _approve_risk(
        app,
        approval_fixture,
        "binding",
        suffix="takeover",
    )
    connection = connect_database(approval_fixture.database_url)
    try:
        next_at = connection.execute(
            "SELECT next_attempt_at FROM approval_writebacks WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    first = outbox.claim_next_approval_writeback(
        app.state.config,
        worker_id="prd078-writeback-old",
        now=_parse_time(next_at) + timedelta(seconds=1),
    )
    assert first is not None and first.attempt_count == 1
    takeover_at = first.lease_expires_at + timedelta(seconds=1)
    second = outbox.claim_next_approval_writeback(
        app.state.config,
        worker_id="prd078-writeback-new",
        now=takeover_at,
    )
    assert second is not None and second.attempt_count == 2
    assert second.lease_generation == first.lease_generation + 1
    intent = outbox._load_writeback_intent(app.state.config, second, now=takeover_at)
    store = InMemorySessionRiskStore()
    state = SimpleNamespace(session_risk_store=store)

    assert not await outbox.dispatch_claimed_approval_writeback(
        app.state.config,
        state,
        first,
        now=takeover_at,
    )
    connection = connect_database(approval_fixture.database_url)
    try:
        active = connection.execute(
            """
            SELECT status,attempt_count,lease_generation,lease_owner_id,lease_token
            FROM approval_writebacks WHERE approval_task_id=?
            """,
            (approval_id,),
        ).fetchone()
        assert tuple(active) == (
            "processing",
            second.attempt_count,
            second.lease_generation,
            second.lease_owner_id,
            second.lease_token,
        )
    finally:
        connection.close()

    assert await outbox.dispatch_claimed_approval_writeback(
        app.state.config,
        state,
        second,
        now=takeover_at,
    )
    assert await store.load(intent.scope) == intent.snapshot
    connection = connect_database(approval_fixture.database_url)
    try:
        writeback = connection.execute(
            """
            SELECT status,attempt_count,last_error_code,completed_at
            FROM approval_writebacks WHERE approval_task_id=?
            """,
            (approval_id,),
        ).fetchone()
        assert writeback["status"] == "completed"
        assert writeback["attempt_count"] == 2
        assert writeback["last_error_code"] is None
        assert writeback["completed_at"] is not None
        task = connection.execute(
            "SELECT status,writeback_status FROM approval_tasks WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()
        assert tuple(task) == ("approved", "completed")
    finally:
        connection.close()


@pytest.mark.anyio
async def test_session_risk_writeback_tenth_failure_is_terminal(
    approval_fixture: control_fixtures.ApprovalFixture,
) -> None:
    """Redis 持续不可用时按 5/30/300 秒退避，第十次固定失败且无第十一次。"""

    app = control_fixtures._build_app()
    approval_id = await _approve_risk(
        app,
        approval_fixture,
        "binding",
        suffix="terminal",
    )
    store = InMemorySessionRiskStore(available=False)
    state = SimpleNamespace(session_risk_store=store)
    connection = connect_database(approval_fixture.database_url)
    try:
        current = _parse_time(
            connection.execute(
                "SELECT next_attempt_at FROM approval_writebacks WHERE approval_task_id=?",
                (approval_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()

    for expected_attempt in range(1, outbox.MAX_ATTEMPTS + 1):
        claim = outbox.claim_next_approval_writeback(
            app.state.config,
            worker_id=f"prd078-writeback-fail-{expected_attempt}",
            now=current,
        )
        assert claim is not None and claim.attempt_count == expected_attempt
        assert await outbox.dispatch_claimed_approval_writeback(
            app.state.config,
            state,
            claim,
            now=current,
        )
        connection = connect_database(approval_fixture.database_url)
        try:
            row = connection.execute(
                """
                SELECT status,next_attempt_at,last_error_code
                FROM approval_writebacks WHERE approval_task_id=?
                """,
                (approval_id,),
            ).fetchone()
        finally:
            connection.close()
        if expected_attempt < outbox.MAX_ATTEMPTS:
            assert row["status"] == "pending"
            assert row["last_error_code"] == "approval.writeback_unavailable"
            current = _parse_time(row["next_attempt_at"])
        else:
            assert tuple(row) == (
                "failed",
                None,
                "approval.writeback_unavailable",
            )

    assert (
        outbox.claim_next_approval_writeback(
            app.state.config,
            worker_id="prd078-writeback-no-eleventh",
            now=current + timedelta(hours=1),
        )
        is None
    )
    connection = connect_database(approval_fixture.database_url)
    try:
        task = connection.execute(
            "SELECT status,writeback_status FROM approval_tasks WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()
        assert tuple(task) == ("approved", "failed")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
