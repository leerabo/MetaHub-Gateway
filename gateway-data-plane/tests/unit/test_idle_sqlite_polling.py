"""Idle SQLite workers must not acquire the process-wide writer lock."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from metahub_gateway import local_model_events
from metahub_gateway.quota import lifecycle as quota_lifecycle
from metahub_gateway.quota import service as quota_service


def _create_idle_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE local_model_call_event_outbox(
              status TEXT NOT NULL,
              lease_expires_at TEXT,
              next_attempt_at TEXT,
              attempt_count INTEGER NOT NULL
            );
            CREATE INDEX idx_local_model_call_outbox_claim_prd084
              ON local_model_call_event_outbox(status,next_attempt_at);
            CREATE TABLE quota_reservations(
              status TEXT NOT NULL,
              expires_at TEXT NOT NULL
            );
            CREATE INDEX idx_quota_reservations_due_prd079
              ON quota_reservations(status,expires_at);
            INSERT INTO local_model_call_event_outbox(
              status,lease_expires_at,next_attempt_at,attempt_count
            ) VALUES('pending',NULL,'2099-01-01T00:00:00Z',0);
            INSERT INTO quota_reservations(status,expires_at)
            VALUES('active','2099-01-01T00:00:00Z');
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_idle_local_model_and_quota_polls_remain_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Empty and not-yet-due queues must not contend with request transactions."""

    database = tmp_path / "idle-workers.sqlite3"
    _create_idle_database(database)
    statements: list[str] = []

    def traced_connect(_database_url: str) -> sqlite3.Connection:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(local_model_events, "connect_database", traced_connect)
    monkeypatch.setattr(quota_service, "_connect", traced_connect)
    monkeypatch.setattr(
        quota_service,
        "_database_now",
        lambda _connection: "2026-08-20T00:00:00Z",
    )
    config = SimpleNamespace(database_url="sqlite:///unused.sqlite3")

    assert local_model_events.claim_local_model_events(
        config,
        "idle-test-worker",
        now=datetime(2026, 8, 20, tzinfo=UTC),
    ) == []
    assert quota_service.reconcile_expired_quota_reservations(config) == 0

    normalized = [" ".join(statement.split()).upper() for statement in statements]
    assert "BEGIN IMMEDIATE" not in normalized
    assert not any(
        statement.startswith(("UPDATE ", "INSERT ", "DELETE "))
        for statement in normalized
    )


class _ConnectionProbe:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


@pytest.mark.anyio
async def test_idle_workers_reuse_one_connection_and_close_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each serialized worker parses the SQLite schema once, then owns clean teardown."""

    local_connection = _ConnectionProbe()
    local_open_count = 0
    local_seen: list[object] = []
    local_stop = asyncio.Event()

    def open_local(*_args: object, **_kwargs: object) -> _ConnectionProbe:
        nonlocal local_open_count
        local_open_count += 1
        return local_connection

    def claim_local(*_args: object, connection: object, **_kwargs: object) -> list[object]:
        local_seen.append(connection)
        if len(local_seen) == 2:
            local_stop.set()
        return []

    monkeypatch.setattr(local_model_events, "connect_database", open_local)
    monkeypatch.setattr(local_model_events, "claim_local_model_events", claim_local)
    monkeypatch.setattr(local_model_events, "_POLL_SECONDS", 0.001)
    config = SimpleNamespace(database_url="sqlite:///unused.sqlite3")
    await local_model_events._outbox_worker(config, "idle-worker", local_stop)

    assert local_open_count == 1
    assert local_seen == [local_connection, local_connection]
    assert local_connection.close_count == 1

    quota_connection = _ConnectionProbe()
    quota_open_count = 0
    quota_seen: list[object] = []
    quota_stop = asyncio.Event()

    def open_quota(*_args: object, **_kwargs: object) -> _ConnectionProbe:
        nonlocal quota_open_count
        quota_open_count += 1
        return quota_connection

    def reconcile_quota(
        _config: object,
        *,
        limit: int,
        connection: object,
    ) -> int:
        assert limit == 50
        quota_seen.append(connection)
        if len(quota_seen) == 2:
            quota_stop.set()
        return 0

    monkeypatch.setattr(quota_lifecycle, "connect_database", open_quota)
    monkeypatch.setattr(
        quota_lifecycle,
        "reconcile_expired_quota_reservations",
        reconcile_quota,
    )
    monkeypatch.setattr(quota_lifecycle, "_poll_seconds", lambda _app: 0.001)
    app = SimpleNamespace(state=SimpleNamespace(config=config))
    await quota_lifecycle._reconciliation_loop(app, quota_stop)

    assert quota_open_count == 1
    assert quota_seen == [quota_connection, quota_connection]
    assert quota_connection.close_count == 1
