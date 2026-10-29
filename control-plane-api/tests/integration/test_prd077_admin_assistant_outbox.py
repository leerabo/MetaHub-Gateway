"""验证 PRD-077 管理助手事件 outbox 的租约恢复、迟到 worker 隔离和原子终态。"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_prd077_admin_assistant_control as control_fixtures
from httpx import ASGITransport, AsyncClient
from metahub_control.admin_assistant import outbox
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_assistant_queried_event

prd077_control_template = control_fixtures.prd077_control_template
# 固定 delivery ID 只用于临时数据库中定位第十次终态恢复记录。
TERMINAL_DELIVERY_ID = "whd_" + "7" * 32
SUCCESS_RESULT = {
    "status": "success",
    "http_status": 204,
    "duration_ms": 1,
    "error_code": None,
    "error_summary": None,
}


def _parse_time(value: str) -> datetime:
    """只接受测试库中 canonical UTC 时间并返回带时区对象。"""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """按 source 表合同生成 UTC 秒，避免测试制造精度漂移。"""

    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _create_pending_attempt(app, suffix: str = "outbox") -> dict[str, object]:
    """经真实认证和 Control route 原子生成一组 pending source 事实。"""

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await control_fixtures._login(
            client,
            "admin@metahub.local",
            control_fixtures.ADMIN_PASSWORD,
        )
        response = await control_fixtures._ask(
            client,
            token,
            suffix,
            "usage in the last 7 days",
        )
    assert response.status_code == 200, response.text
    return response.json()


def _event_for_claim(claim: outbox.AssistantEventClaim):
    """严格从 source claim 重建公开七字段事件，不引入问题、回答或目标请求。"""

    return build_assistant_queried_event(
        tenant_id=claim.tenant_id,
        actor_id=claim.actor_user_id,
        assistant_query_id=claim.assistant_query_id,
        event_id=claim.assistant_event_id,
        trace_id=claim.operation_trace_id,
        source_request_id=claim.operation_request_id,
        query_type=claim.query_type,
        outcome=claim.outcome,
        claim_count=claim.claim_count,
        source_count=claim.source_count,
        redaction_count=claim.redaction_count,
        duration_ms=claim.duration_ms,
        timestamp=claim.created_at,
    )


def _insert_active_assistant_endpoint(
    database_url: str,
    marker: str,
    *,
    max_attempts: int = 1,
) -> str:
    """写入一个只订阅 assistant.queried 的测试 Endpoint，并返回稳定 ID。"""

    endpoint_id = f"wh_prd077_{marker}"
    connection = connect_database(database_url)
    try:
        tenant_id = str(
            connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
              max_attempts,created_at,updated_at
            ) VALUES(?,?,?,'http://127.0.0.1:9/prd077','["assistant.queried"]',
                     'enc_v1:fixture-only','fixture','fixture-fingerprint','active',
                     1000,?,?,?)
            """,
            (
                endpoint_id,
                tenant_id,
                f"PRD077 {marker}",
                max_attempts,
                control_fixtures.NOW,
                control_fixtures.NOW,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return endpoint_id


def _claim_answer(
    config,
    database_url: str,
    answer: dict[str, object],
    *,
    worker_id: str,
) -> outbox.AssistantEventClaim:
    """按回答持久事实的创建时间领取 source，避免测试依赖墙钟先后。"""

    connection = connect_database(database_url)
    try:
        created_at = _parse_time(
            str(
                connection.execute(
                    "SELECT created_at FROM assistant_audits WHERE assistant_audit_id=?",
                    (answer["assistant_audit_id"],),
                ).fetchone()[0]
            )
        )
    finally:
        connection.close()
    claim = outbox.claim_next_assistant_event(
        config,
        worker_id=worker_id,
        now=created_at + timedelta(seconds=1),
    )
    assert claim is not None
    return claim


@pytest.mark.anyio
async def test_stale_lease_reclaim_and_late_worker_cannot_overwrite_new_owner(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """过期租约可恢复，但旧 worker 的迟到成功不能覆盖新 64 位 CAS owner。"""

    control_fixtures._clear_rate_limiter()
    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    answer = await _create_pending_attempt(app, "lease")
    connection = connect_database(database_url)
    try:
        created_at = _parse_time(
            str(
                connection.execute(
                    "SELECT created_at FROM assistant_audits WHERE assistant_audit_id=?",
                    (answer["assistant_audit_id"],),
                ).fetchone()[0]
            )
        )
    finally:
        connection.close()

    first = outbox.claim_next_assistant_event(
        app.state.config,
        worker_id="prd077-worker-old",
        now=created_at + timedelta(seconds=1),
    )
    assert first is not None
    assert first.attempts == 1
    assert len(first.claim_token) == 64
    assert (
        outbox.claim_next_assistant_event(
            app.state.config,
            worker_id="prd077-worker-early",
            now=first.lease_expires_at - timedelta(seconds=1),
        )
        is None
    )

    second = outbox.claim_next_assistant_event(
        app.state.config,
        worker_id="prd077-worker-new",
        now=first.lease_expires_at + timedelta(seconds=1),
    )
    assert second is not None
    assert second.attempts == 2
    assert second.claim_token != first.claim_token
    assert second.lease_owner_id != first.lease_owner_id

    # stub 只代表 dispatcher 成功；真正终态仍必须由 source token/owner/attempt/lease CAS 决定。
    dispatcher_calls: list[str] = []

    def record_dispatch(*args, **kwargs):
        """记录过期旧 claim 的出网尝试，终态 CAS 必须拒绝其完成当前租约。"""

        dispatcher_calls.append("dispatch")
        return []

    monkeypatch.setattr(outbox, "dispatch_webhook_event", record_dispatch)
    completion = second.lease_expires_at - timedelta(seconds=1)
    assert (
        outbox.dispatch_claimed_assistant_event(
            app.state.config,
            first,
            now=completion,
        )
        is False
    )
    # 旧 claim 必须在 dispatcher 之前失败关闭，因而也不可能触达其下游网络层。
    assert dispatcher_calls == []
    assert (
        outbox.dispatch_claimed_assistant_event(
            app.state.config,
            second,
            now=completion,
        )
        is True
    )
    assert dispatcher_calls == ["dispatch"]

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT event_dispatch_status,event_dispatch_attempts,event_claim_token,
                   event_lease_owner_id,event_last_error_code,event_dispatched_at
            FROM assistant_audits WHERE assistant_audit_id=?
            """,
            (answer["assistant_audit_id"],),
        ).fetchone()
        assert tuple(row[:5]) == ("dispatched", 2, None, None, None)
        assert row[5] is not None
    finally:
        connection.close()


@pytest.mark.anyio
async def test_each_real_send_renews_exact_source_lease_and_finishes_latest_owner(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """多 Endpoint 的每次真实出网前都续租，最终终态必须使用最后一次 lease。"""

    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    endpoint_ids = (
        _insert_active_assistant_endpoint(database_url, "lease_a"),
        _insert_active_assistant_endpoint(database_url, "lease_b"),
    )
    app = control_fixtures._build_app()
    answer = await _create_pending_attempt(app, "lease_each_send")
    claim = _claim_answer(
        app.state.config,
        database_url,
        answer,
        worker_id="prd077-lease-each-send",
    )
    observed: list[tuple[str, datetime, datetime]] = []

    def record_success(_config, endpoint, _body, _delivery_id):
        """在每次真实发送替身中读取 source 租约，证明出网前已单独续租。"""

        connection = connect_database(database_url)
        try:
            source = connection.execute(
                """
                SELECT event_lease_expires_at,updated_at
                FROM assistant_audits WHERE assistant_audit_id=?
                """,
                (claim.assistant_audit_id,),
            ).fetchone()
            assert source is not None
            observed.append(
                (
                    str(endpoint["webhook_endpoint_id"]),
                    _parse_time(str(source["event_lease_expires_at"])),
                    _parse_time(str(source["updated_at"])),
                )
            )
        finally:
            connection.close()
        return dict(SUCCESS_RESULT)

    monkeypatch.setattr(webhook_service, "_send_once", record_success)
    assert outbox.dispatch_claimed_assistant_event(
        app.state.config,
        claim,
        now=claim.created_at + timedelta(seconds=2),
    )
    assert [item[0] for item in observed] == list(endpoint_ids)
    assert observed[0][1] > claim.lease_expires_at
    assert observed[1][1] > observed[0][1]
    assert observed[1][2] > observed[0][2]

    connection = connect_database(database_url)
    try:
        source = connection.execute(
            """
            SELECT event_dispatch_status,event_dispatch_attempts,event_claim_token,
                   event_lease_owner_id,event_lease_expires_at,event_last_error_code
            FROM assistant_audits WHERE assistant_audit_id=?
            """,
            (claim.assistant_audit_id,),
        ).fetchone()
        attempts = connection.execute(
            """
            SELECT COUNT(*) FROM webhook_delivery_attempts
            WHERE tenant_id=? AND webhook_delivery_id IN(
              SELECT webhook_delivery_id FROM webhook_deliveries
              WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
            )
            """,
            (claim.tenant_id, claim.tenant_id, claim.assistant_event_id),
        ).fetchone()[0]
        assert tuple(source) == ("dispatched", 1, None, None, None, None)
        assert attempts == 2
    finally:
        connection.close()


@pytest.mark.anyio
async def test_commit_time_endpoint_snapshot_excludes_later_subscriber(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """提交前存在的 A 被冻结；提交后新增的 B 不得得到 intent 或网络调用。"""

    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    endpoint_a = _insert_active_assistant_endpoint(database_url, "snapshot_a")
    app = control_fixtures._build_app()
    answer = await _create_pending_attempt(app, "snapshot_existing")
    endpoint_b = _insert_active_assistant_endpoint(database_url, "snapshot_b")
    claim = _claim_answer(
        app.state.config,
        database_url,
        answer,
        worker_id="prd077-snapshot-existing",
    )
    sent: list[str] = []

    def record_success(_config, endpoint, _body, _delivery_id):
        """记录实际使用的 endpoint，验证 worker 只能消费查询提交时冻结的快照。"""

        sent.append(str(endpoint["webhook_endpoint_id"]))
        return dict(SUCCESS_RESULT)

    monkeypatch.setattr(webhook_service, "_send_once", record_success)
    assert outbox.dispatch_claimed_assistant_event(
        app.state.config,
        claim,
        now=claim.created_at + timedelta(seconds=2),
    )
    assert sent == [endpoint_a]
    connection = connect_database(database_url)
    try:
        frozen = connection.execute(
            """
            SELECT webhook_endpoint_id FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
            ORDER BY webhook_endpoint_id
            """,
            (claim.tenant_id, claim.assistant_event_id),
        ).fetchall()
        assert [str(row[0]) for row in frozen] == [endpoint_a]
        assert endpoint_b not in {str(row[0]) for row in frozen}
    finally:
        connection.close()


@pytest.mark.anyio
async def test_empty_commit_time_snapshot_stays_empty_after_subscriber_is_added(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """提交时无订阅者的历史事件不得追溯投递给后来新增的 Endpoint。"""

    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    answer = await _create_pending_attempt(app, "snapshot_empty")
    _insert_active_assistant_endpoint(database_url, "snapshot_late")
    claim = _claim_answer(
        app.state.config,
        database_url,
        answer,
        worker_id="prd077-snapshot-empty",
    )

    def reject_network(*_args, **_kwargs):
        """拒绝任何发送调用，证明空的提交时快照不会被后续新 endpoint 扩充。"""

        # 若触发即说明 worker 在提交事务后动态重算 endpoint，违反原子快照边界。
        raise AssertionError("empty commit-time snapshot must remain network-free")

    monkeypatch.setattr(webhook_service, "_send_once", reject_network)
    assert outbox.dispatch_claimed_assistant_event(
        app.state.config,
        claim,
        now=claim.created_at + timedelta(seconds=2),
    )
    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM webhook_deliveries
                WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
                """,
                (claim.tenant_id, claim.assistant_event_id),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT event_dispatch_status FROM assistant_audits WHERE assistant_audit_id=?",
                (claim.assistant_audit_id,),
            ).fetchone()[0]
            == "dispatched"
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_snapshot_failure_after_intent_insert_rolls_back_whole_governance_graph(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """快照 helper 在插入 intent 后失败时，查询、两类审计和 delivery 必须全回滚。"""

    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    _insert_active_assistant_endpoint(database_url, "snapshot_rollback")
    app = control_fixtures._build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await control_fixtures._login(
            client,
            "admin@metahub.local",
            control_fixtures.ADMIN_PASSWORD,
        )
        connection = connect_database(database_url)
        try:
            before = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "assistant_queries",
                    "assistant_audits",
                    "audit_logs",
                    "webhook_deliveries",
                )
            }
            session_before = tuple(
                connection.execute(
                    "SELECT last_seen_at,updated_at FROM sessions ORDER BY session_id"
                ).fetchall()
            )
        finally:
            connection.close()

        original = webhook_service.freeze_webhook_event_delivery_snapshot
        inserted: list[int] = []

        def fail_after_insert(connection, event, *, frozen_at):
            """在 delivery 快照插入后注入领域失败，验证外层事务原子回滚治理图。"""

            count = original(connection, event, frozen_at=frozen_at)
            inserted.append(count)
            assert count == 1
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM webhook_deliveries WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()[0]
                == 1
            )
            # 插入后的固定失败必须回滚 Query、Audit、AuditLog、Event 与 delivery 快照。
            raise ApiError("webhook.delivery_unavailable")

        monkeypatch.setattr(
            webhook_service,
            "freeze_webhook_event_delivery_snapshot",
            fail_after_insert,
        )
        response = await control_fixtures._ask(
            client,
            token,
            "snapshot_rollback",
            "usage in the last 7 days",
        )
    control_fixtures._assert_error(
        response,
        "admin_assistant.audit_unavailable",
        503,
    )
    assert inserted == [1]

    connection = connect_database(database_url)
    try:
        after = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "assistant_queries",
                "assistant_audits",
                "audit_logs",
                "webhook_deliveries",
            )
        }
        session_after = tuple(
            connection.execute(
                "SELECT last_seen_at,updated_at FROM sessions ORDER BY session_id"
            ).fetchall()
        )
        assert after == before
        assert session_after == session_before
    finally:
        connection.close()


@pytest.mark.anyio
async def test_stop_after_first_network_prevents_second_send_and_source_recovers(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """首个网络调用期间到达停服信号后不再出网，剩余 intent 可由新 claim 恢复。"""

    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    endpoint_ids = (
        _insert_active_assistant_endpoint(database_url, "stop_a"),
        _insert_active_assistant_endpoint(database_url, "stop_b"),
    )
    app = control_fixtures._build_app()
    answer = await _create_pending_attempt(app, "stop_after_first")
    first_claim = _claim_answer(
        app.state.config,
        database_url,
        answer,
        worker_id="prd077-stop-first",
    )
    stop_event = threading.Event()
    sent: list[str] = []

    def stop_after_first(_config, endpoint, _body, _delivery_id):
        """首个 endpoint 发送成功后触发停服信号，验证下一次出网前停止处理。"""

        sent.append(str(endpoint["webhook_endpoint_id"]))
        if len(sent) == 1:
            stop_event.set()
        return dict(SUCCESS_RESULT)

    monkeypatch.setattr(webhook_service, "_send_once", stop_after_first)
    assert outbox.dispatch_claimed_assistant_event(
        app.state.config,
        first_claim,
        now=first_claim.created_at + timedelta(seconds=2),
        should_stop=stop_event.is_set,
    )
    assert len(sent) == 1
    assert sent[0] in endpoint_ids
    first_endpoint = sent[0]
    remaining_endpoint = next(endpoint for endpoint in endpoint_ids if endpoint != first_endpoint)

    connection = connect_database(database_url)
    try:
        source = connection.execute(
            """
            SELECT event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,
                   event_claim_token,event_lease_owner_id,event_lease_expires_at
            FROM assistant_audits WHERE assistant_audit_id=?
            """,
            (first_claim.assistant_audit_id,),
        ).fetchone()
        deliveries = connection.execute(
            """
            SELECT webhook_endpoint_id,status FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? ORDER BY webhook_endpoint_id
            """,
            (first_claim.tenant_id, first_claim.assistant_event_id),
        ).fetchall()
        assert tuple(source[:2]) == ("pending", 1)
        assert source[2] is not None
        assert tuple(source[3:]) == (None, None, None)
        assert {tuple(row) for row in deliveries} == {
            (first_endpoint, "success"),
            (remaining_endpoint, "pending"),
        }
        retry_at = _parse_time(str(source[2]))
    finally:
        connection.close()

    stop_event.clear()
    second_claim = outbox.claim_next_assistant_event(
        app.state.config,
        worker_id="prd077-stop-recovery",
        now=retry_at,
    )
    assert second_claim is not None and second_claim.attempts == 2
    assert outbox.dispatch_claimed_assistant_event(
        app.state.config,
        second_claim,
        now=retry_at,
        should_stop=stop_event.is_set,
    )
    assert sent == [first_endpoint, remaining_endpoint]
    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                "SELECT event_dispatch_status FROM assistant_audits WHERE assistant_audit_id=?",
                (first_claim.assistant_audit_id,),
            ).fetchone()[0]
            == "dispatched"
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM webhook_deliveries
                WHERE tenant_id=? AND event_id=? AND status='success'
                """,
                (first_claim.tenant_id, first_claim.assistant_event_id),
            ).fetchone()[0]
            == 2
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_trigger_allows_only_forward_same_owner_lease_renewal(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """0070 trigger 放行同 owner、同 attempt、严格推进时间的合法续租。"""

    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    answer = await _create_pending_attempt(app, "trigger_renew")
    claim = _claim_answer(
        app.state.config,
        database_url,
        answer,
        worker_id="prd077-trigger-renew",
    )
    connection = connect_database(database_url)
    try:
        old_updated = _parse_time(
            str(
                connection.execute(
                    "SELECT updated_at FROM assistant_audits WHERE assistant_audit_id=?",
                    (claim.assistant_audit_id,),
                ).fetchone()[0]
            )
        )
        renewed_at = old_updated + timedelta(seconds=1)
        renewed_lease = claim.lease_expires_at + timedelta(seconds=1)
        connection.execute(
            """
            UPDATE assistant_audits SET event_lease_expires_at=?,updated_at=?
            WHERE assistant_audit_id=?
            """,
            (_db_time(renewed_lease), _db_time(renewed_at), claim.assistant_audit_id),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT event_dispatch_attempts,event_claim_token,event_lease_owner_id,
                   event_lease_expires_at,updated_at
            FROM assistant_audits WHERE assistant_audit_id=?
            """,
            (claim.assistant_audit_id,),
        ).fetchone()
        assert tuple(row) == (
            claim.attempts,
            claim.claim_token,
            claim.lease_owner_id,
            _db_time(renewed_lease),
            _db_time(renewed_at),
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "drift_kind",
    (
        "attempt",
        "token",
        "owner",
        "tenant",
        "event",
        "lease_equal",
        "lease_backward",
        "expired_renewal",
    ),
)
@pytest.mark.anyio
async def test_trigger_rejects_lease_identity_or_time_drift_atomically(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drift_kind: str,
) -> None:
    """attempt/token/owner/租户/事件或非前进时间均不得形成部分续租。"""

    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    answer = await _create_pending_attempt(app, f"trigger_drift_{drift_kind}")
    claim = _claim_answer(
        app.state.config,
        database_url,
        answer,
        worker_id=f"prd077-trigger-{drift_kind}",
    )
    connection = connect_database(database_url)
    try:
        before = tuple(
            connection.execute(
                "SELECT rowid,* FROM assistant_audits WHERE assistant_audit_id=?",
                (claim.assistant_audit_id,),
            ).fetchone()
        )
        old_updated = _parse_time(
            str(
                connection.execute(
                    "SELECT updated_at FROM assistant_audits WHERE assistant_audit_id=?",
                    (claim.assistant_audit_id,),
                ).fetchone()[0]
            )
        )
        updates: dict[str, object] = {
            "updated_at": _db_time(old_updated + timedelta(seconds=1)),
            "event_lease_expires_at": _db_time(claim.lease_expires_at + timedelta(seconds=1)),
        }
        if drift_kind == "attempt":
            updates["event_dispatch_attempts"] = claim.attempts + 1
        # claim token 漂移模拟其他 worker 已抢占，旧 owner 不得继续写入终态。
        elif drift_kind == "token":
            updates["event_claim_token"] = "f" * 64
        elif drift_kind == "owner":
            updates["event_lease_owner_id"] = "prd077-other-owner"
        # tenant_id 是治理图身份的一部分，跨租户改写必须由数据库约束直接拒绝。
        elif drift_kind == "tenant":
            updates["tenant_id"] = "tenant_prd077_foreign"
        elif drift_kind == "event":
            updates["assistant_event_id"] = "asevt_" + "f" * 32
        elif drift_kind == "lease_equal":
            updates["event_lease_expires_at"] = _db_time(claim.lease_expires_at)
        elif drift_kind == "lease_backward":
            updates["event_lease_expires_at"] = _db_time(
                claim.lease_expires_at - timedelta(seconds=1)
            )
        elif drift_kind == "expired_renewal":
            updates["updated_at"] = _db_time(claim.lease_expires_at)
        else:  # pragma: no cover - 参数化集合与分支必须保持一一对应。
            # 参数化列表与分支脱节属于测试错误，必须立即失败而非跳过约束验证。
            raise AssertionError(drift_kind)
        assignments = ",".join(f"{column}=?" for column in updates)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE assistant_audits SET {assignments} WHERE assistant_audit_id=?",
                (*updates.values(), claim.assistant_audit_id),
            )
        connection.rollback()
        after = tuple(
            connection.execute(
                "SELECT rowid,* FROM assistant_audits WHERE assistant_audit_id=?",
                (claim.assistant_audit_id,),
            ).fetchone()
        )
        assert after == before
    finally:
        connection.close()


@pytest.mark.anyio
async def test_bad_source_is_quarantined_without_starving_later_tenant(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """最早 source 的持久时间损坏时，固定失败隔离后仍领取另一租户的后续事实。"""

    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at
            ) VALUES(
              'user_prd077_foreign_admin','tenant_prd077_foreign',
              'admin@prd077-foreign.local','PRD077 Foreign Admin',?,'admin','active',?,?
            )
            """,
            (
                hash_password(control_fixtures.ROLE_PASSWORD),
                control_fixtures.NOW,
                control_fixtures.NOW,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    app = control_fixtures._build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        local_token = await control_fixtures._login(
            client,
            "admin@metahub.local",
            control_fixtures.ADMIN_PASSWORD,
        )
        foreign_login = await client.post(
            "/auth/login",
            json={
                "tenant_slug": "prd077-foreign",
                "email": "admin@prd077-foreign.local",
                "password": control_fixtures.ROLE_PASSWORD,
            },
        )
        assert foreign_login.status_code == 200, foreign_login.text
        foreign_token = str(foreign_login.json()["access_token"])
        local_answer = await control_fixtures._ask(
            client,
            local_token,
            "bad_source_local",
            "usage in the last 7 days",
        )
        foreign_answer = await control_fixtures._ask(
            client,
            foreign_token,
            "bad_source_foreign",
            "usage in the last 7 days",
        )
    assert local_answer.status_code == 200, local_answer.text
    assert foreign_answer.status_code == 200, foreign_answer.text

    connection = connect_database(database_url)
    try:
        ordered = connection.execute(
            """
            SELECT assistant_audit_id,tenant_id,created_at,event_next_attempt_at
            FROM assistant_audits ORDER BY created_at,assistant_audit_id
            """
        ).fetchall()
        assert len(ordered) == 2
        assert len({str(row["tenant_id"]) for row in ordered}) == 2
        bad = ordered[0]
        healthy = ordered[1]
        trigger_sql = str(
            connection.execute(
                """
                SELECT sql FROM sqlite_schema
                WHERE type='trigger' AND name='trg_assistant_audits_update_prd077'
                """
            ).fetchone()[0]
        )
        trigger_hash = hashlib.sha256(trigger_sql.encode("utf-8")).hexdigest()
        connection.execute("DROP TRIGGER trg_assistant_audits_update_prd077")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        canonical_due = str(bad["event_next_attempt_at"])
        connection.execute(
            "UPDATE assistant_audits SET event_next_attempt_at=? WHERE assistant_audit_id=?",
            (canonical_due.removesuffix("Z"), bad["assistant_audit_id"]),
        )
        connection.execute("PRAGMA ignore_check_constraints=OFF")
        connection.execute(trigger_sql)
        connection.commit()
        restored_sql = str(
            connection.execute(
                """
                SELECT sql FROM sqlite_schema
                WHERE type='trigger' AND name='trg_assistant_audits_update_prd077'
                """
            ).fetchone()[0]
        )
        assert hashlib.sha256(restored_sql.encode("utf-8")).hexdigest() == trigger_hash
        current = max(_parse_time(str(row["created_at"])) for row in ordered) + timedelta(seconds=1)
    finally:
        connection.close()

    claim = outbox.claim_next_assistant_event(
        app.state.config,
        worker_id="prd077-bad-source-isolation",
        now=current,
    )
    assert claim is not None
    assert claim.assistant_audit_id == healthy["assistant_audit_id"]
    assert claim.tenant_id == healthy["tenant_id"]
    connection = connect_database(database_url)
    try:
        quarantined = connection.execute(
            """
            SELECT event_dispatch_status,event_dispatch_attempts,event_last_error_code,
                   event_claim_token,event_lease_owner_id,event_lease_expires_at
            FROM assistant_audits WHERE assistant_audit_id=?
            """,
            (bad["assistant_audit_id"],),
        ).fetchone()
        assert tuple(quarantined) == (
            "failed",
            10,
            "admin_assistant.event_unavailable",
            None,
            None,
            None,
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_pending_backoff_cannot_be_claimed_before_due(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """trigger 必须拒绝绕过 backoff 的 pending->processing，达到 due 后正常 claim 仍可用。"""

    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    answer = await _create_pending_attempt(app, "pending_backoff")
    first = _claim_answer(
        app.state.config,
        database_url,
        answer,
        worker_id="prd077-backoff-first",
    )

    def fail_dispatch(*_args, **_kwargs):
        """持续注入统一投递失败，验证 pending 重试写入有界退避而非热循环。"""

        # 上游失败必须由 outbox 状态机追踪，不能泄露 endpoint 或请求正文。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(outbox, "dispatch_webhook_event", fail_dispatch)
    assert outbox.dispatch_claimed_assistant_event(
        app.state.config,
        first,
        now=first.created_at + timedelta(seconds=2),
    )
    connection = connect_database(database_url)
    try:
        pending = connection.execute(
            """
            SELECT rowid,* FROM assistant_audits WHERE assistant_audit_id=?
            """,
            (first.assistant_audit_id,),
        ).fetchone()
        before = tuple(pending)
        updated_at = _parse_time(str(pending["updated_at"]))
        retry_at = _parse_time(str(pending["event_next_attempt_at"]))
        early = updated_at + timedelta(seconds=1)
        assert early < retry_at
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE assistant_audits
                SET event_dispatch_status='processing',event_dispatch_attempts=2,
                    event_next_attempt_at=NULL,event_claim_token=?,event_lease_owner_id=?,
                    event_lease_expires_at=?,event_last_error_code=NULL,updated_at=?
                WHERE assistant_audit_id=?
                """,
                (
                    "e" * 64,
                    "prd077-backoff-early",
                    _db_time(early + timedelta(seconds=outbox.EVENT_LEASE_SECONDS)),
                    _db_time(early),
                    first.assistant_audit_id,
                ),
            )
        connection.rollback()
        assert (
            tuple(
                connection.execute(
                    "SELECT rowid,* FROM assistant_audits WHERE assistant_audit_id=?",
                    (first.assistant_audit_id,),
                ).fetchone()
            )
            == before
        )
    finally:
        connection.close()

    second = outbox.claim_next_assistant_event(
        app.state.config,
        worker_id="prd077-backoff-due",
        now=retry_at,
    )
    assert second is not None and second.attempts == 2


@pytest.mark.anyio
async def test_trigger_requires_stale_takeover_and_active_terminal_lease(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未过期 processing 不可接管，过期后可换 owner；普通终态只能由活跃 lease 提交。"""

    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    first_answer = await _create_pending_attempt(app, "takeover_window")
    first = _claim_answer(
        app.state.config,
        database_url,
        first_answer,
        worker_id="prd077-takeover-old",
    )
    connection = connect_database(database_url)
    try:
        source = connection.execute(
            "SELECT rowid,* FROM assistant_audits WHERE assistant_audit_id=?",
            (first.assistant_audit_id,),
        ).fetchone()
        before = tuple(source)
        old_updated = _parse_time(str(source["updated_at"]))
        early = old_updated + timedelta(seconds=1)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE assistant_audits
                SET event_dispatch_attempts=2,event_claim_token=?,event_lease_owner_id=?,
                    event_lease_expires_at=?,updated_at=?
                WHERE assistant_audit_id=?
                """,
                (
                    "d" * 64,
                    "prd077-takeover-early",
                    _db_time(first.lease_expires_at + timedelta(seconds=1)),
                    _db_time(early),
                    first.assistant_audit_id,
                ),
            )
        connection.rollback()
        assert (
            tuple(
                connection.execute(
                    "SELECT rowid,* FROM assistant_audits WHERE assistant_audit_id=?",
                    (first.assistant_audit_id,),
                ).fetchone()
            )
            == before
        )

        takeover_at = first.lease_expires_at + timedelta(seconds=1)
        takeover_lease = takeover_at + timedelta(seconds=outbox.EVENT_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE assistant_audits
            SET event_dispatch_attempts=2,event_claim_token=?,event_lease_owner_id=?,
                event_lease_expires_at=?,updated_at=?
            WHERE assistant_audit_id=?
            """,
            (
                "c" * 64,
                "prd077-takeover-new",
                _db_time(takeover_lease),
                _db_time(takeover_at),
                first.assistant_audit_id,
            ),
        )
        connection.commit()
        terminal_at = takeover_at + timedelta(seconds=1)
        connection.execute(
            """
            UPDATE assistant_audits
            SET event_dispatch_status='dispatched',event_claim_token=NULL,
                event_lease_owner_id=NULL,event_lease_expires_at=NULL,
                event_dispatched_at=?,updated_at=?
            WHERE assistant_audit_id=?
            """,
            (_db_time(terminal_at), _db_time(terminal_at), first.assistant_audit_id),
        )
        connection.commit()
        assert (
            connection.execute(
                "SELECT event_dispatch_status FROM assistant_audits WHERE assistant_audit_id=?",
                (first.assistant_audit_id,),
            ).fetchone()[0]
            == "dispatched"
        )
    finally:
        connection.close()

    second_answer = await _create_pending_attempt(app, "terminal_window")
    second = _claim_answer(
        app.state.config,
        database_url,
        second_answer,
        worker_id="prd077-terminal-old",
    )
    connection = connect_database(database_url)
    try:
        before = tuple(
            connection.execute(
                "SELECT rowid,* FROM assistant_audits WHERE assistant_audit_id=?",
                (second.assistant_audit_id,),
            ).fetchone()
        )
        expired = second.lease_expires_at + timedelta(seconds=1)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE assistant_audits
                SET event_dispatch_status='dispatched',event_claim_token=NULL,
                    event_lease_owner_id=NULL,event_lease_expires_at=NULL,
                    event_dispatched_at=?,updated_at=?
                WHERE assistant_audit_id=?
                """,
                (_db_time(expired), _db_time(expired), second.assistant_audit_id),
            )
        connection.rollback()
        assert (
            tuple(
                connection.execute(
                    "SELECT rowid,* FROM assistant_audits WHERE assistant_audit_id=?",
                    (second.assistant_audit_id,),
                ).fetchone()
            )
            == before
        )
    finally:
        connection.close()


def _insert_pending_delivery(
    database_url: str,
    claim: outbox.AssistantEventClaim,
) -> None:
    """建立与第十次来源 claim 同 owner 的原始待投递记录，验证双表原子终结。"""

    event = _event_for_claim(claim)
    timestamp = _db_time(claim.created_at)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
              max_attempts,created_at,updated_at
            ) VALUES(
              'wh_prd077_terminal',?,'PRD077 terminal fixture',
              'http://127.0.0.1:9/prd077','["assistant.queried"]',
              'enc_v1:fixture-only','fixture','fixture-fingerprint','disabled',
              1000,1,?,?
            )
            """,
            (claim.tenant_id, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
              event_timestamp,payload_json,status,attempt_count,max_attempts,
              next_retry_at,delivery_kind,secret_version,created_at,updated_at
            ) VALUES(
              ? ,?,'wh_prd077_terminal',?,'assistant.queried',
              ?,?,'pending',0,1,?,'original',1,?,?
            )
            """,
            (
                TERMINAL_DELIVERY_ID,
                claim.tenant_id,
                claim.assistant_event_id,
                timestamp,
                json.dumps(event.to_payload(), ensure_ascii=True, separators=(",", ":")),
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.anyio
async def test_tenth_crashed_attempt_atomically_fails_source_and_pending_delivery(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """第十次 worker 崩溃后只做终态恢复，不发生第十一次网络调用或孤儿 pending。"""

    control_fixtures._clear_rate_limiter()
    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    answer = await _create_pending_attempt(app, "terminal")
    connection = connect_database(database_url)
    try:
        created_at = _parse_time(
            str(
                connection.execute(
                    "SELECT created_at FROM assistant_audits WHERE assistant_audit_id=?",
                    (answer["assistant_audit_id"],),
                ).fetchone()[0]
            )
        )
    finally:
        connection.close()

    def fail_network(*args, **kwargs):
        """模拟每次 Webhook 网络投递均返回已经归一的固定不可用错误。"""

        # 网络失败 stub 不包含 Endpoint、凭据或响应正文，避免测试证据泄漏。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(outbox, "dispatch_webhook_event", fail_network)
    current = created_at + timedelta(seconds=1)
    for expected_attempt in range(1, 10):
        claim = outbox.claim_next_assistant_event(
            app.state.config,
            worker_id=f"prd077-failing-{expected_attempt}",
            now=current,
        )
        assert claim is not None and claim.attempts == expected_attempt
        assert (
            outbox.dispatch_claimed_assistant_event(
                app.state.config,
                claim,
                now=current,
            )
            is True
        )
        connection = connect_database(database_url)
        try:
            state = connection.execute(
                """
                SELECT event_dispatch_status,event_dispatch_attempts,
                       event_last_error_code,event_next_attempt_at
                FROM assistant_audits WHERE assistant_audit_id=?
                """,
                (answer["assistant_audit_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert tuple(state[:3]) == (
            "pending",
            expected_attempt,
            "admin_assistant.event_unavailable",
        )
        current = _parse_time(str(state[3]))

    tenth = outbox.claim_next_assistant_event(
        app.state.config,
        worker_id="prd077-crashed-tenth",
        now=current,
    )
    assert tenth is not None and tenth.attempts == 10
    _insert_pending_delivery(database_url, tenth)
    # claim_next 的首个事务分支负责识别过期第十次 attempt，并与 delivery 同时终结。
    assert (
        outbox.claim_next_assistant_event(
            app.state.config,
            worker_id="prd077-no-eleventh-call",
            now=tenth.lease_expires_at + timedelta(seconds=1),
        )
        is None
    )

    connection = connect_database(database_url)
    try:
        source = connection.execute(
            """
            SELECT event_dispatch_status,event_dispatch_attempts,event_claim_token,
                   event_lease_owner_id,event_last_error_code
            FROM assistant_audits WHERE assistant_audit_id=?
            """,
            (answer["assistant_audit_id"],),
        ).fetchone()
        delivery = connection.execute(
            """
            SELECT status,error_code,error_summary,next_retry_at
            FROM webhook_deliveries WHERE webhook_delivery_id=?
            """,
            (TERMINAL_DELIVERY_ID,),
        ).fetchone()
        assert tuple(source) == (
            "failed",
            10,
            None,
            None,
            "admin_assistant.event_unavailable",
        )
        assert delivery["status"] == "failed"
        assert delivery["error_code"] == "webhook.delivery_unavailable"
        assert delivery["next_retry_at"] is None
        assert "Source delivery attempts" in delivery["error_summary"]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_pending_envelope_recovery_rejects_every_owner_tamper(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """恢复 request owner 时必须重建并比对 tenant/event/actor/trace/time/payload 全图。"""

    control_fixtures._clear_rate_limiter()
    database_url = control_fixtures._clone_environment(
        prd077_control_template,
        monkeypatch,
        tmp_path,
    )
    app = control_fixtures._build_app()
    await _create_pending_attempt(app, "recovery")
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            "SELECT created_at FROM assistant_audits ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        claim = outbox.claim_next_assistant_event(
            app.state.config,
            worker_id="prd077-recovery-owner",
            now=_parse_time(str(row[0])) + timedelta(seconds=1),
        )
        assert claim is not None
        envelope = _event_for_claim(claim).to_payload()
        assert outbox.recover_assistant_source_request_id(connection, envelope) == (
            claim.operation_request_id
        )

        mutations = []
        for key, value in (
            ("tenant_id", "tenant_prd077_tampered"),
            ("event_id", "asevt_" + "f" * 32),
            ("actor_id", "user_prd077_tampered"),
            ("object_id", "asq_" + "f" * 32),
            ("trace_id", "trc_prd077_tampered"),
            ("timestamp", "2026-08-11T23:59:59Z"),
        ):
            candidate = copy.deepcopy(envelope)
            candidate[key] = value
            mutations.append(candidate)
        payload_tamper = copy.deepcopy(envelope)
        payload_tamper["payload"]["claim_count"] += 1
        mutations.append(payload_tamper)
        extra_key = copy.deepcopy(envelope)
        extra_key["question"] = "must never be trusted"
        mutations.append(extra_key)

        for candidate in mutations:
            with pytest.raises(ApiError) as captured:
                outbox.recover_assistant_source_request_id(connection, candidate)
            assert captured.value.code == "admin_assistant.event_unavailable"
    finally:
        connection.close()
