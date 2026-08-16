"""PRD-082 MCP event source writer and outbox recovery contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import metahub_control.mcp_governance.outbox as mcp_outbox
import metahub_control.webhooks.service as webhook_service
import pytest
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.errors import ApiError

NOW = datetime(2026, 8, 16, 2, 3, 4, tzinfo=UTC)
NOW_TEXT = "2026-08-16T02:03:04Z"
TENANT_ID = "tenant_prd082"
INVOCATION_ID = "mcpi_" + "a" * 32
SERVER_ID = "mcps_" + "b" * 32
TOOL_ID = "mcpt_" + "c" * 32
ACTOR_ID = "user_prd082"
REQUEST_ID = "req_prd082_invoke"
TRACE_ID = "trc_prd082_invoke"
DELIVERY_ID = "whd_" + "d" * 32


def _url(path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _seed(database_url: str) -> None:
    connection = connect_database(database_url)
    try:
        connection.executescript(
            """
            CREATE TABLE mcp_invocations(
              tenant_id TEXT NOT NULL,mcp_invocation_id TEXT NOT NULL,
              mcp_server_id TEXT NOT NULL,mcp_tool_id TEXT NOT NULL,
              requested_by TEXT NOT NULL,request_id TEXT NOT NULL,trace_id TEXT NOT NULL,
              status TEXT NOT NULL,risk_level TEXT NOT NULL,approval_required INTEGER NOT NULL,
              input_dlp_status TEXT NOT NULL,input_dlp_match_count INTEGER NOT NULL,
              output_dlp_status TEXT,output_dlp_match_count INTEGER,
              result_redaction_count INTEGER,probe_attempt_count INTEGER NOT NULL,
              call_attempt_count INTEGER NOT NULL,duration_ms INTEGER,request_bytes INTEGER,
              response_bytes INTEGER,execution_status TEXT NOT NULL,
              invocation_count INTEGER NOT NULL,
              tool_call_count INTEGER,cost_status TEXT NOT NULL,error_code TEXT,completed_at TEXT
            );
            CREATE TABLE mcp_invocation_events(
              mcp_invocation_event_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
              mcp_invocation_id TEXT NOT NULL,event_type TEXT NOT NULL,
              schema_version INTEGER NOT NULL,
              mcp_server_id TEXT NOT NULL,mcp_tool_id TEXT NOT NULL,status TEXT NOT NULL,
              risk_level TEXT NOT NULL,approval_required INTEGER NOT NULL,
              network_attempted INTEGER NOT NULL,input_dlp_status TEXT NOT NULL,
              input_dlp_match_count INTEGER NOT NULL,output_dlp_status TEXT NOT NULL,
              output_dlp_match_count INTEGER NOT NULL,redaction_count INTEGER NOT NULL,
              probe_attempt_count INTEGER NOT NULL,call_attempt_count INTEGER NOT NULL,
              duration_ms INTEGER NOT NULL,request_bytes INTEGER NOT NULL,response_bytes INTEGER,
              execution_status TEXT NOT NULL,invocation_count INTEGER NOT NULL,
              tool_call_count INTEGER,cost_status TEXT NOT NULL,error_code TEXT,
              completed_at TEXT NOT NULL,created_at TEXT NOT NULL
            );
            CREATE TABLE mcp_invocation_event_outbox(
              mcp_invocation_event_outbox_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
              mcp_invocation_event_id TEXT NOT NULL,status TEXT NOT NULL,
              attempt_count INTEGER NOT NULL,lease_generation INTEGER NOT NULL,
              lease_owner_id TEXT,lease_token TEXT,lease_expires_at TEXT,next_attempt_at TEXT,
              subscription_snapshot_json TEXT,snapshot_frozen_at TEXT,error_code TEXT,
              delivered_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            );
            CREATE TABLE webhook_deliveries(
              webhook_delivery_id TEXT NOT NULL,tenant_id TEXT NOT NULL,event_id TEXT NOT NULL,
              delivery_kind TEXT NOT NULL,status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO mcp_invocations VALUES(
              ?,?,?,?,?,?,?,'succeeded','standard',0,'clean',0,'clean',0,0,1,1,12,2,3,
              'called',1,1,'not_reported',NULL,?
            )
            """,
            (
                TENANT_ID,
                INVOCATION_ID,
                SERVER_ID,
                TOOL_ID,
                ACTOR_ID,
                REQUEST_ID,
                TRACE_ID,
                NOW_TEXT,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _config(database_url: str) -> SimpleNamespace:
    return SimpleNamespace(database_url=database_url, mcp_invocation_lease_seconds=90)


def _install_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_delivery: bool,
) -> None:
    def freeze(connection, event, *, frozen_at: str) -> int:
        assert connection.in_transaction
        assert frozen_at == NOW_TEXT
        if with_delivery:
            connection.execute(
                "INSERT INTO webhook_deliveries VALUES(?,?,?,'original','pending')",
                (DELIVERY_ID, event.tenant_id, event.event_id),
            )
            return 1
        return 0

    monkeypatch.setattr(webhook_service, "freeze_webhook_event_delivery_snapshot", freeze)


def _create_source(database_url: str) -> tuple[str, str]:
    with connect_database(database_url) as connection:
        invocation = connection.execute(
            "SELECT tenant_id,mcp_invocation_id FROM mcp_invocations"
        ).fetchone()
        assert invocation is not None
        connection.execute("BEGIN IMMEDIATE")
        result = mcp_outbox.create_mcp_invocation_event_in_transaction(
            connection,
            invocation,
        )
        assert result is not None and connection.in_transaction
        connection.commit()
        return result


def _state(database_url: str):
    with connect_database(database_url) as connection:
        return connection.execute(
            """
            SELECT status,attempt_count,lease_generation,lease_owner_id,
                   next_attempt_at,subscription_snapshot_json,snapshot_frozen_at,
                   error_code,delivered_at
            FROM mcp_invocation_event_outbox
            """
        ).fetchone()


def test_transaction_writer_freezes_exact_snapshot_and_dispatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    database_url = _url(tmp_path / "mcp-event-success.sqlite3")
    _seed(database_url)
    _install_snapshot(monkeypatch, with_delivery=True)
    event_id, outbox_id = _create_source(database_url)

    with connect_database(database_url) as connection:
        event_row = connection.execute("SELECT * FROM mcp_invocation_events").fetchone()
        outbox_row = connection.execute("SELECT * FROM mcp_invocation_event_outbox").fetchone()
        assert event_row is not None and outbox_row is not None
        assert event_row["mcp_invocation_event_id"] == event_id
        assert outbox_row["mcp_invocation_event_outbox_id"] == outbox_id
        assert outbox_row["subscription_snapshot_json"] == f'["{DELIVERY_ID}"]'
        assert outbox_row["snapshot_frozen_at"] == NOW_TEXT
        assert connection.execute("SELECT COUNT(*) FROM mcp_invocation_events").fetchone()[0] == 1

    config = _config(database_url)
    claim = mcp_outbox.claim_next_mcp_invocation_event(
        config,
        worker_id="prd082-event-worker",
        now=NOW,
    )
    assert claim is not None
    assert (claim.attempt_count, claim.lease_generation) == (1, 1)
    dispatched: list[str] = []

    def dispatch(_config, tenant_id, event, **options):
        assert tenant_id == TENANT_ID
        assert event.event_id == event_id
        assert options["request_id"] == REQUEST_ID
        options["source_lease_guard"]()
        dispatched.append(event.event_id)
        return [SimpleNamespace(status="success")]

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", dispatch)
    assert mcp_outbox.dispatch_claimed_mcp_invocation_event(config, claim, now=NOW)
    assert dispatched == [event_id]
    assert tuple(_state(database_url)) == (
        "succeeded",
        1,
        3,
        None,
        None,
        f'["{DELIVERY_ID}"]',
        NOW_TEXT,
        None,
        NOW_TEXT,
    )

    with connect_database(database_url) as connection:
        envelope = claim.event.to_payload()
        assert (
            mcp_outbox.recover_mcp_invocation_source_request_id(connection, envelope)
            == REQUEST_ID
        )
        tampered = dict(envelope)
        tampered["trace_id"] = "trc_tampered"
        with pytest.raises(ApiError):
            mcp_outbox.recover_mcp_invocation_source_request_id(connection, tampered)


def test_late_owner_is_zero_io_and_current_owner_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    database_url = _url(tmp_path / "mcp-event-late-owner.sqlite3")
    _seed(database_url)
    _install_snapshot(monkeypatch, with_delivery=False)
    _create_source(database_url)
    config = _config(database_url)

    first = mcp_outbox.claim_next_mcp_invocation_event(
        config,
        worker_id="prd082-owner-first",
        now=NOW,
    )
    assert first is not None
    takeover_at = NOW + timedelta(seconds=91)
    second = mcp_outbox.claim_next_mcp_invocation_event(
        config,
        worker_id="prd082-owner-second",
        now=takeover_at,
    )
    assert second is not None
    assert (second.attempt_count, second.lease_generation) == (2, 2)
    calls: list[str] = []

    def dispatch(_config, _tenant_id, _event, **options):
        options["source_lease_guard"]()
        calls.append("network")
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", dispatch)
    assert not mcp_outbox.dispatch_claimed_mcp_invocation_event(
        config,
        first,
        now=NOW + timedelta(seconds=1),
    )
    assert calls == []
    assert mcp_outbox.dispatch_claimed_mcp_invocation_event(
        config,
        second,
        now=takeover_at + timedelta(seconds=1),
    )
    assert calls == ["network"]  # Only the current owner reached the injected dispatcher.
    state = _state(database_url)
    assert (state["status"], state["attempt_count"], state["lease_generation"]) == (
        "succeeded",
        2,
        4,
    )


def test_known_tool_failure_without_output_scan_uses_not_scanned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    database_url = _url(tmp_path / "mcp-event-known-failure.sqlite3")
    _seed(database_url)
    with connect_database(database_url) as connection:
        connection.execute(
            """
            UPDATE mcp_invocations
            SET status='failed',output_dlp_status=NULL,output_dlp_match_count=NULL,
                result_redaction_count=NULL,error_code='mcp.tool_failed'
            """
        )
        connection.commit()
    _install_snapshot(monkeypatch, with_delivery=False)
    _create_source(database_url)
    with connect_database(database_url) as connection:
        event = connection.execute(
            """
            SELECT status,output_dlp_status,output_dlp_match_count,redaction_count,error_code
            FROM mcp_invocation_events
            """
        ).fetchone()
    assert tuple(event) == ("failed", "not_scanned", 0, 0, "mcp.tool_failed")


def test_not_called_terminal_never_creates_tool_called_source(tmp_path) -> None:
    database_url = _url(tmp_path / "mcp-event-not-called.sqlite3")
    _seed(database_url)
    with connect_database(database_url) as connection:
        connection.execute(
            """
            UPDATE mcp_invocations
            SET status='blocked',output_dlp_status=NULL,output_dlp_match_count=NULL,
                result_redaction_count=NULL,probe_attempt_count=0,call_attempt_count=0,
                execution_status='not_called',tool_call_count=0,cost_status='not_applicable',
                error_code='mcp.arguments_blocked'
            """
        )
        connection.commit()
        invocation = connection.execute(
            "SELECT tenant_id,mcp_invocation_id FROM mcp_invocations"
        ).fetchone()
        assert invocation is not None
        with pytest.raises(ApiError):
            mcp_outbox.create_mcp_invocation_event_in_transaction(connection, invocation)
        connection.execute("BEGIN IMMEDIATE")
        assert (
            mcp_outbox.create_mcp_invocation_event_in_transaction(connection, invocation) is None
        )
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM mcp_invocation_events").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM mcp_invocation_event_outbox").fetchone()[0]
            == 0
        )


def test_retry_backoff_caps_at_ten_and_dead_letters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    database_url = _url(tmp_path / "mcp-event-retries.sqlite3")
    _seed(database_url)
    _install_snapshot(monkeypatch, with_delivery=False)
    event_id, _ = _create_source(database_url)
    config = _config(database_url)

    def unavailable(_config, _tenant_id, _event, **options):
        options["source_lease_guard"]()
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", unavailable)
    dead_letters: list[tuple[str, str]] = []

    def fail_pending(_connection, **values):
        dead_letters.append((values["event_id"], values["failure_code"]))
        return 0

    monkeypatch.setattr(webhook_service, "fail_pending_webhook_event_deliveries", fail_pending)
    current = NOW
    delays = (5, 30, 300, 300, 300, 300, 300, 300, 300)
    for attempt in range(1, 11):
        claim = mcp_outbox.claim_next_mcp_invocation_event(
            config,
            worker_id=f"prd082-retry-{attempt}",
            now=current,
        )
        assert claim is not None and claim.attempt_count == attempt
        assert mcp_outbox.dispatch_claimed_mcp_invocation_event(config, claim, now=current)
        state = _state(database_url)
        if attempt < 10:
            assert state["status"] == "pending"
            next_time = current + timedelta(seconds=delays[attempt - 1])
            assert state["next_attempt_at"] == mcp_outbox._db_time(next_time)
            current = next_time
        else:
            assert (state["status"], state["attempt_count"], state["error_code"]) == (
                "failed",
                10,
                "webhook.delivery_unavailable",
            )
    assert dead_letters == [(event_id, "webhook.delivery_unavailable")]
    assert (
        mcp_outbox.claim_next_mcp_invocation_event(
            config,
            worker_id="prd082-eleventh",
            now=current + timedelta(days=1),
        )
        is None
    )


def test_bad_source_is_quarantined_and_stop_before_claim_is_zero_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    database_url = _url(tmp_path / "mcp-event-quarantine.sqlite3")
    _seed(database_url)
    _install_snapshot(monkeypatch, with_delivery=False)
    event_id, _ = _create_source(database_url)
    config = _config(database_url)
    assert not mcp_outbox.drain_mcp_invocation_event_once(
        config,
        worker_id="prd082-stopped",
        now=NOW,
        should_stop=lambda: True,
    )
    assert _state(database_url)["status"] == "pending"

    with connect_database(database_url) as connection:
        connection.execute("UPDATE mcp_invocation_events SET duration_ms=13")
        connection.commit()
    failed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        webhook_service,
        "fail_pending_webhook_event_deliveries",
        lambda _connection, **values: failed.append(
            (values["event_id"], values["failure_code"])
        ),
    )
    assert mcp_outbox.drain_mcp_invocation_event_once(
        config,
        worker_id="prd082-quarantine",
        now=NOW,
    )
    state = _state(database_url)
    assert (state["status"], state["attempt_count"], state["error_code"]) == (
        "failed",
        1,
        "mcp.unavailable",
    )
    assert failed == [(event_id, "webhook.payload_invalid")]
