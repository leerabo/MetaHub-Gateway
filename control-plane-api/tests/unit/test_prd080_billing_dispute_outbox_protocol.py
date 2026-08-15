"""PRD-080 事件快照恢复、租约 generation 与迟到 owner 专项测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import metahub_control.billing_disputes.outbox as dispute_outbox
import metahub_control.webhooks.service as webhook_service
import pytest
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

NOW = datetime(2026, 8, 15, 1, 2, 3, tzinfo=UTC)
NOW_TEXT = "2026-08-15T01:02:03Z"
TENANT_ID = "tenant_prd080"
DISPUTE_ID = "bdsp_" + "a" * 32
EVENT_ID = "bdevt_" + "b" * 32
OUTBOX_ID = "bdob_" + "c" * 32
ACTOR_ID = "usr_prd080_finance"
REQUEST_ID = "req_prd080_create"
TRACE_ID = "trc_prd080_create"
PAYLOAD = {
    "schema_version": 1,
    "billing_dispute_id": DISPUTE_ID,
    "scope_type": "request_ids",
    "item_count": 1,
    "currency_totals": {"CNY": "1.000000"},
    "status": "submitted",
    "created_at": NOW_TEXT,
}


def _database_url(path) -> str:
    """把 pytest 临时路径转换为 Shared SQLite URL，避免依赖当前工作目录。"""

    return f"sqlite:///{path.as_posix()}"


def _seed_outbox(database_url: str) -> None:
    """建立 worker 所需最小 owner 图；快照在事件创建时已冻结为空集合。"""

    connection = connect_database(database_url)
    try:
        connection.executescript(
            """
            CREATE TABLE billing_disputes(
              tenant_id TEXT NOT NULL,billing_dispute_id TEXT NOT NULL,item_count INTEGER NOT NULL,
              scope_type TEXT NOT NULL,status TEXT NOT NULL,created_by_user_id TEXT NOT NULL,
              create_request_id TEXT NOT NULL,create_trace_id TEXT NOT NULL,created_at TEXT NOT NULL
            );
            CREATE TABLE billing_dispute_items(
              tenant_id TEXT NOT NULL,billing_dispute_id TEXT NOT NULL,
              currency TEXT NOT NULL,disputable_micros INTEGER NOT NULL,
              ledger_sequence INTEGER NOT NULL
            );
            CREATE TABLE billing_dispute_events(
              billing_dispute_event_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
              billing_dispute_id TEXT NOT NULL,event_type TEXT NOT NULL,payload_json TEXT NOT NULL,
              actor_user_id TEXT NOT NULL,source_request_id TEXT NOT NULL,
              source_trace_id TEXT NOT NULL,occurred_at TEXT NOT NULL,created_at TEXT NOT NULL
            );
            CREATE TABLE billing_dispute_event_outbox(
              billing_dispute_event_outbox_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
              billing_dispute_event_id TEXT NOT NULL,status TEXT NOT NULL,
              attempt_count INTEGER NOT NULL,lease_generation INTEGER NOT NULL,
              lease_owner_id TEXT,lease_token TEXT,lease_expires_at TEXT,next_attempt_at TEXT,
              delivery_snapshot_json TEXT NOT NULL,snapshot_frozen_at TEXT NOT NULL,
              last_error_code TEXT,succeeded_at TEXT,
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            );
            CREATE TABLE billing_dispute_scheduler_tenants(
              tenant_id TEXT PRIMARY KEY,last_claimed_at TEXT NOT NULL,
              last_claim_sequence INTEGER NOT NULL UNIQUE,state_revision INTEGER NOT NULL,
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            );
            CREATE TABLE webhook_deliveries(
              webhook_delivery_id TEXT NOT NULL,tenant_id TEXT NOT NULL,event_id TEXT NOT NULL,
              delivery_kind TEXT NOT NULL
            );
            CREATE TABLE webhook_endpoints(
              webhook_endpoint_id TEXT NOT NULL,tenant_id TEXT NOT NULL,status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO billing_disputes VALUES(?,?,?,?,?,?,?,?,?)",
            (
                TENANT_ID,
                DISPUTE_ID,
                1,
                "request_ids",
                "submitted",
                ACTOR_ID,
                REQUEST_ID,
                TRACE_ID,
                NOW_TEXT,
            ),
        )
        connection.execute(
            "INSERT INTO billing_dispute_items VALUES(?,?,?,?,?)",
            (TENANT_ID, DISPUTE_ID, "CNY", 1_000_000, 1),
        )
        connection.execute(
            "INSERT INTO billing_dispute_events VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                EVENT_ID,
                TENANT_ID,
                DISPUTE_ID,
                "billing_dispute.created",
                json.dumps(PAYLOAD, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                ACTOR_ID,
                REQUEST_ID,
                TRACE_ID,
                NOW_TEXT,
                NOW_TEXT,
            ),
        )
        connection.execute(
            """INSERT INTO billing_dispute_event_outbox
               VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,?,'[]',?,NULL,NULL,?,?)""",
            (OUTBOX_ID, TENANT_ID, EVENT_ID, NOW_TEXT, NOW_TEXT, NOW_TEXT, NOW_TEXT),
        )
        # 模拟 source 提交后、首次 claim 前新增的 Endpoint；claim 不得为旧事件扩大 delivery 集合。
        connection.execute(
            "INSERT INTO webhook_endpoints VALUES('whe_late',?,'active')",
            (TENANT_ID,),
        )
        connection.commit()
    finally:
        connection.close()


def _state(database_url: str):
    """读取不会泄露 token 的状态坐标，供 generation/attempt 断言使用。"""

    connection = connect_database(database_url)
    try:
        return connection.execute(
            """SELECT status,attempt_count,lease_generation,lease_owner_id,
                      delivery_snapshot_json,snapshot_frozen_at
               FROM billing_dispute_event_outbox"""
        ).fetchone()
    finally:
        connection.close()


def _insert_source(
    connection,
    *,
    tenant_id: str,
    marker: str,
) -> tuple[str, str]:
    """向最小库追加一个无订阅 source，供多租户公平轮转测试。"""

    dispute_id = f"bdsp_{marker * 32}"
    event_id = f"bdevt_{marker * 32}"
    outbox_id = f"bdob_{marker * 32}"
    payload = {
        "schema_version": 1,
        "billing_dispute_id": dispute_id,
        "scope_type": "request_ids",
        "item_count": 1,
        "currency_totals": {"CNY": "1.000000"},
        "status": "submitted",
        "created_at": NOW_TEXT,
    }
    connection.execute(
        "INSERT INTO billing_disputes VALUES(?,?,?,?,?,?,?,?,?)",
        (
            tenant_id,
            dispute_id,
            1,
            "request_ids",
            "submitted",
            f"actor_{marker}",
            f"request_{marker}",
            f"trace_{marker}",
            NOW_TEXT,
        ),
    )
    connection.execute(
        "INSERT INTO billing_dispute_items VALUES(?,?,?,?,?)",
        (tenant_id, dispute_id, "CNY", 1_000_000, ord(marker)),
    )
    connection.execute(
        "INSERT INTO billing_dispute_events VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            tenant_id,
            dispute_id,
            "billing_dispute.created",
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            f"actor_{marker}",
            f"request_{marker}",
            f"trace_{marker}",
            NOW_TEXT,
            NOW_TEXT,
        ),
    )
    connection.execute(
        """INSERT INTO billing_dispute_event_outbox
           VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,?,'[]',?,NULL,NULL,?,?)""",
        (outbox_id, tenant_id, event_id, NOW_TEXT, NOW_TEXT, NOW_TEXT, NOW_TEXT),
    )
    return event_id, outbox_id


def test_claim_takeover_renew_and_finish_reject_late_owner(tmp_path) -> None:
    """claim 不增 attempt；每次租约变更增 generation，旧 owner 永远不能覆盖新状态。"""

    database_url = _database_url(tmp_path / "prd080-outbox.sqlite")
    _seed_outbox(database_url)
    config = SimpleNamespace(database_url=database_url)

    first = dispute_outbox.claim_next_billing_dispute_event(
        config,
        worker_id="prd080-first",
        now=NOW,
    )
    assert first is not None
    assert (first.attempt_count, first.lease_generation) == (0, 1)
    first_state = _state(database_url)
    assert tuple(first_state) == (
        "processing",
        0,
        1,
        "prd080-first",
        "[]",
        NOW_TEXT,
    )

    takeover = dispute_outbox.claim_next_billing_dispute_event(
        config,
        worker_id="prd080-takeover",
        now=NOW + timedelta(seconds=91),
    )
    assert takeover is not None
    assert (takeover.attempt_count, takeover.lease_generation) == (0, 2)

    # 时间覆盖回到未过期窗口，使失败只能由 generation/token 失配解释。
    with pytest.raises(ApiError) as first_renew_error:
        dispute_outbox._renew(config, first, now=NOW + timedelta(seconds=1))
    assert first_renew_error.value.code == "billing_dispute.unavailable"
    assert (
        dispute_outbox._finish(
            config,
            first,
            error_code=None,
            now=NOW + timedelta(seconds=1),
        )
        is False
    )

    before_renew = replace(takeover)
    dispute_outbox._renew(config, takeover, now=NOW + timedelta(seconds=92))
    assert takeover.lease_generation == 3
    with pytest.raises(ApiError) as stale_renew_error:
        dispute_outbox._renew(config, before_renew, now=NOW + timedelta(seconds=93))
    assert stale_renew_error.value.code == "billing_dispute.unavailable"
    assert (
        dispute_outbox._finish(
            config,
            before_renew,
            error_code=None,
            now=NOW + timedelta(seconds=93),
        )
        is False
    )

    assert (
        dispute_outbox._finish(
            config,
            takeover,
            error_code=None,
            now=NOW + timedelta(seconds=93),
        )
        is True
    )
    terminal = _state(database_url)
    assert tuple(terminal) == ("succeeded", 1, 4, None, "[]", NOW_TEXT)

    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 0
    finally:
        connection.close()


def test_pending_delivery_recovers_private_request_from_dispute_owner_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公共 Webhook 恢复入口必须调用争议 owner 图，不能用本轮 worker request_id。"""

    event = build_webhook_event(
        tenant_id=TENANT_ID,
        event_type="billing_dispute.created",
        event_id=EVENT_ID,
        timestamp=NOW,
        actor_id=ACTOR_ID,
        object_id=DISPUTE_ID,
        trace_id=TRACE_ID,
        source_request_id=REQUEST_ID,
        payload=PAYLOAD,
    )
    envelope = event.to_payload()
    calls: list[tuple[object, dict[str, object]]] = []
    connection = object()

    def recover(candidate_connection: object, candidate_envelope: dict[str, object]) -> str:
        """记录公共恢复入口传入的精确信封，并返回不可公开的 source request。"""

        calls.append((candidate_connection, candidate_envelope))
        return REQUEST_ID

    monkeypatch.setattr(
        dispute_outbox,
        "recover_billing_dispute_source_request_id",
        recover,
    )
    delivery = {
        "tenant_id": TENANT_ID,
        "event_id": EVENT_ID,
        "event_type": "billing_dispute.created",
        "event_timestamp": NOW_TEXT,
        "payload_json": json.dumps(
            envelope,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }

    body = webhook_service._validated_pending_delivery_body(connection, delivery)  # type: ignore[arg-type]

    assert json.loads(body) == envelope
    assert calls == [(connection, envelope)]


def test_failed_delivery_result_is_terminal_and_zero_io_stop_releases_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Webhook failed DTO 立即收敛；首次外呼前停服归还租约且 attempt 保持不变。"""

    failed_url = _database_url(tmp_path / "prd080-failed-delivery.sqlite")
    _seed_outbox(failed_url)
    failed_config = SimpleNamespace(database_url=failed_url)
    failed_claim = dispute_outbox.claim_next_billing_dispute_event(
        failed_config,
        worker_id="prd080-failed-result",
        now=NOW,
    )
    assert failed_claim is not None
    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        lambda *args, **kwargs: [SimpleNamespace(status="failed")],
    )
    failed_closed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        webhook_service,
        "fail_pending_webhook_event_deliveries",
        lambda _connection, **values: failed_closed.append(
            (values["event_id"], values["failure_code"])
        ),
    )
    assert dispute_outbox.dispatch_claimed_billing_dispute_event(
        failed_config,
        failed_claim,
        now=NOW,
    )
    with connect_database(failed_url) as connection:
        failed = connection.execute(
            """SELECT status,attempt_count,last_error_code,next_attempt_at
               FROM billing_dispute_event_outbox"""
        ).fetchone()
    assert tuple(failed) == (
        "failed",
        1,
        "webhook.delivery_unavailable",
        None,
    )
    assert failed_closed == [(failed_claim.event_id, "webhook.delivery_unavailable")]

    stopped_url = _database_url(tmp_path / "prd080-stopped-delivery.sqlite")
    _seed_outbox(stopped_url)
    stopped_config = SimpleNamespace(database_url=stopped_url)
    stopped_claim = dispute_outbox.claim_next_billing_dispute_event(
        stopped_config,
        worker_id="prd080-stopped-result",
        now=NOW,
    )
    assert stopped_claim is not None
    dispatch_called = False

    def unexpected_dispatch(*args, **kwargs):
        nonlocal dispatch_called
        dispatch_called = True
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", unexpected_dispatch)
    assert dispute_outbox.dispatch_claimed_billing_dispute_event(
        stopped_config,
        stopped_claim,
        now=NOW,
        should_stop=lambda: True,
    )
    assert dispatch_called is False
    with connect_database(stopped_url) as connection:
        stopped = connection.execute(
            """SELECT status,attempt_count,lease_generation,lease_owner_id,
                      next_attempt_at,last_error_code
               FROM billing_dispute_event_outbox"""
        ).fetchone()
    assert tuple(stopped) == ("pending", 0, 2, None, NOW_TEXT, None)

    expired_url = _database_url(tmp_path / "prd080-expired-release.sqlite")
    _seed_outbox(expired_url)
    expired_config = SimpleNamespace(database_url=expired_url)
    expired_claim = dispute_outbox.claim_next_billing_dispute_event(
        expired_config,
        worker_id="prd080-expired-release",
        now=NOW,
    )
    assert expired_claim is not None
    expired_at = NOW + timedelta(seconds=91)
    assert not dispute_outbox._release_claim(expired_config, expired_claim, now=expired_at)
    takeover = dispute_outbox.claim_next_billing_dispute_event(
        expired_config,
        worker_id="prd080-expired-takeover",
        now=expired_at,
    )
    assert takeover is not None and takeover.lease_generation == expired_claim.lease_generation + 1
    assert dispute_outbox._finish(expired_config, takeover, error_code=None, now=expired_at)


def test_claims_rotate_tenants_with_persistent_fairness_watermark(tmp_path) -> None:
    """同一到期秒内持续积压按 tenant 轮转，不能由 tenant_id 次排序饿死后者。"""

    database_url = _database_url(tmp_path / "prd080-tenant-fairness.sqlite")
    _seed_outbox(database_url)
    with connect_database(database_url) as connection:
        _insert_source(connection, tenant_id=TENANT_ID, marker="d")
        _insert_source(connection, tenant_id="tenant_prd080_b", marker="e")
        _insert_source(connection, tenant_id="tenant_prd080_b", marker="f")
        connection.executemany(
            """INSERT INTO billing_dispute_scheduler_tenants(
                 tenant_id,last_claimed_at,last_claim_sequence,state_revision,created_at,updated_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                (TENANT_ID, NOW_TEXT, 100, 1, NOW_TEXT, NOW_TEXT),
                ("tenant_prd080_b", NOW_TEXT, 1, 1, NOW_TEXT, NOW_TEXT),
            ),
        )
        connection.commit()
    config = SimpleNamespace(database_url=database_url)

    claimed_tenants: list[str] = []
    for index in range(4):
        claim = dispute_outbox.claim_next_billing_dispute_event(
            config,
            worker_id=f"prd080-fair-{index}",
            now=NOW,
        )
        assert claim is not None
        claimed_tenants.append(claim.tenant_id)
        assert dispute_outbox._finish(config, claim, error_code=None, now=NOW)

    assert claimed_tenants == ["tenant_prd080_b", TENANT_ID, "tenant_prd080_b", TENANT_ID]
    with connect_database(database_url) as connection:
        cursors = connection.execute(
            """SELECT tenant_id,last_claim_sequence,state_revision
               FROM billing_dispute_scheduler_tenants
               ORDER BY tenant_id"""
        ).fetchall()
    assert [tuple(row) for row in cursors] == [
        (TENANT_ID, 104, 3),
        ("tenant_prd080_b", 103, 3),
    ]
