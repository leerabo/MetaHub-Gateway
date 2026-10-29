"""Logical bucket keys work without SQLite rowid and preserve bounded cleanup."""

import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from metahub_control.identity import self_service
from metahub_shared.runtime.errors import ApiError


@pytest.fixture
def buckets(tmp_path, monkeypatch):
    database = tmp_path / "buckets.sqlite3"
    config = SimpleNamespace(
        database_url=f"sqlite:///{database.as_posix()}",
        identity_challenge_secret=secrets.token_urlsafe(48),
    )
    now = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(self_service, "_utcnow", lambda: now)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("""CREATE TABLE identity_abuse_buckets(
            identity_abuse_bucket_id TEXT PRIMARY KEY,scope TEXT NOT NULL,
            bucket_hmac TEXT NOT NULL,window_started_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,blocked_until TEXT,expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,UNIQUE(scope,bucket_hmac,window_started_at)
        ) WITHOUT ROWID""")
        for index in range(53):
            expiry = now + timedelta(seconds=-1 if index < 51 else 1)
            connection.execute(
                "INSERT INTO identity_abuse_buckets VALUES(?,?,?, ?,1,NULL,?,?)",
                (
                    f"old-{index:03}",
                    "registration_email",
                    str(index),
                    self_service._db_time(now - timedelta(minutes=30)),
                    self_service._db_time(expiry),
                    self_service._db_time(now),
                ),
            )
        connection.commit()
    return config, database


def _consume(config, action="registration"):
    self_service._consume_rate_limits(
        config,
        action=action,
        primary_key="synthetic-account",
        user_agent="unit-test",
        client_host="127.0.0.1",
        device_token="synthetic-device",
    )


@pytest.mark.parametrize("action", ["registration", "verification", "reset"])
def test_cleanup_is_bounded_and_upsert_preserves_counters(buckets, action):
    config, database = buckets
    _consume(config, action)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM identity_abuse_buckets").fetchone()[0] == 7
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM identity_abuse_buckets "
                "WHERE identity_abuse_bucket_id LIKE 'old-%'",
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM identity_abuse_buckets WHERE identity_abuse_bucket_id "
                "IN ('old-051','old-052')",
            ).fetchone()[0]
            == 2
        )
    _consume(config, action)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM identity_abuse_buckets").fetchone()[0] == 6
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM identity_abuse_buckets WHERE attempt_count=2",
            ).fetchone()[0]
            == 4
        )


def test_cleanup_is_rolled_back_on_counter_write_failure(buckets, monkeypatch):
    config, database = buckets
    attempted_insert = []
    original = self_service.connect_database

    def connect(url):
        connection = original(url)
        connection.set_trace_callback(
            lambda sql: (
                attempted_insert.append(True)
                if sql.strip().startswith("INSERT INTO identity_abuse_buckets")
                else None
            )
        )
        return connection

    monkeypatch.setattr(self_service, "connect_database", connect)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON identity_abuse_buckets
            BEGIN SELECT RAISE(ABORT,'controlled-write-failure'); END""")
        connection.commit()
    with pytest.raises(ApiError) as rejected:
        _consume(config)
    assert rejected.value.code == "identity.unavailable"
    assert attempted_insert
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM identity_abuse_buckets").fetchone()[0] == 53


def test_rate_limit_still_commits_persistent_block(buckets):
    config, database = buckets
    for _ in range(5):
        _consume(config)
    with pytest.raises(ApiError) as rejected:
        _consume(config)
    assert rejected.value.code == "identity.rate_limited"
    with closing(sqlite3.connect(database)) as connection:
        count, blocked = connection.execute(
            "SELECT attempt_count,blocked_until FROM identity_abuse_buckets "
            "WHERE identity_abuse_bucket_id LIKE 'iab_%' AND scope='registration_email'",
        ).fetchone()
        assert count == 6 and blocked is not None
