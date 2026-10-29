"""Native console claims end preflight snapshots and retain write-time fences."""

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
import test_prd147_postgresql_runtime as native
from metahub_control.console_experience import scheduler
from metahub_shared.db import connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError

pytestmark = native.pytestmark
native_runtime_template = native.native_runtime_template
native_runtime = native.native_runtime


@pytest.mark.parametrize("navigation", [False, True])
def test_postgresql_idle_claim_snapshot_and_transaction_ownership(
    native_runtime, monkeypatch, navigation,
):
    config = native_runtime["config"]
    table = ("workspace_navigation_scheduler_state" if navigation
             else "console_experience_scheduler_state")
    column = "queue_code" if navigation else "worker_kind"
    key = "workspace_navigation_projection" if navigation else "cleanup"
    claim = scheduler._claim_navigation if navigation else scheduler._claim
    now = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(scheduler, "_now", lambda: now)

    def reset(enabled):
        # A nested request scope provides an independent PostgreSQL checkout.
        with database_request_scope():
            with closing(connect_database(config.database_url)) as connection:
                count = connection.execute(
                    f"UPDATE {table} SET enabled=?,next_attempt_at=?,"
                    "lease_expires_at=NULL,lease_owner_id=NULL,lease_token_hmac=NULL "
                    f"WHERE {column}=?",
                    (enabled, scheduler._db_time(now), key),
                ).rowcount
                assert count == 1
                connection.commit()

    reset(1)
    phases = []
    disable_before_write = [True]
    original_connect = scheduler._connect

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                assert phases[-2:] == ["read", "rollback"]
                assert not self.connection.in_transaction
                if disable_before_write[0]:
                    reset(0)
                phases.append("begin")
            result = self.connection.execute(sql, *args)
            if sql.startswith("SELECT"):
                assert self.connection.in_transaction
                phases.append("read")
            return result

        def rollback(self):
            phases.append("rollback")
            return self.connection.rollback()

    monkeypatch.setattr(scheduler, "_connect", lambda current:
                        ObservedConnection(original_connect(current)))
    with database_request_scope(reuse_connections=True):
        assert claim(config, key) is None
        connection = connect_database(config.database_url)
        assert not connection.in_transaction
        assert phases == ["read", "rollback", "begin", "read"]

        # Disabled polls leave no implicit transaction on the reused handle.
        phases.clear()
        assert claim(config, key) is None
        assert phases == ["read", "rollback"]
        assert not connection.in_transaction

        reset(1)
        disable_before_write[0] = False
        phases.clear()
        lease = claim(config, key)
        assert lease is not None
        assert phases == ["read", "rollback", "begin", "read"]
        assert not connection.in_transaction
        state = connection.execute(
            f"SELECT fence_generation,lease_expires_at FROM {table} WHERE {column}=?",
            (key,),
        ).fetchone()
        assert tuple(state) == (
            lease.generation,
            scheduler._db_time(now + timedelta(seconds=scheduler.LEASE_SECONDS)),
        )
        connection.rollback()

        phases.clear()
        assert claim(config, key) is None
        assert phases == ["read", "rollback"]
        assert not connection.in_transaction

        connection.execute("BEGIN")
        connection.execute(f"UPDATE {table} SET attempt_count=17 WHERE {column}=?", (key,))
        phases.clear()
        with pytest.raises(ApiError) as error:
            claim(config, key)
        assert error.value.code == "platform.unavailable"
        assert phases == [] and connection.in_transaction
        assert connection.execute(
            f"SELECT attempt_count FROM {table} WHERE {column}=?", (key,),
        ).fetchone()[0] == 17
        connection.rollback()
