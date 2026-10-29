"""Each outbox iteration starts with its own empty bad-source skip set."""

from types import SimpleNamespace

import pytest
from metahub_control.admin_assistant import outbox
from metahub_shared.db.connection import (
    _close_idle_database_connections,
    connect_database,
    database_commit_guard,
)


@pytest.mark.parametrize("previous_skip", [False, True])
def test_repeated_empty_outbox_claims_reset_a_warm_connections_skip_set(tmp_path, previous_skip):
    config = SimpleNamespace(database_url=f"sqlite:///{(tmp_path / 'outbox.db').as_posix()}")
    setup = connect_database(config.database_url)
    setup.executescript("""
        CREATE TABLE assistant_audits(
          assistant_audit_id TEXT, tenant_id TEXT, assistant_query_id TEXT,
          event_dispatch_status TEXT, event_dispatch_attempts INTEGER,
          event_lease_expires_at TEXT, event_next_attempt_at TEXT, created_at TEXT);
        CREATE TABLE assistant_queries(
          tenant_id TEXT, assistant_query_id TEXT, query_type TEXT);
    """)
    setup.close()
    physical = None
    try:
        with database_commit_guard(lambda _: None):
            for _ in range(3):
                assert outbox.claim_next_assistant_event(config, worker_id="warm-test") is None
                observer = connect_database(config.database_url)
                try:
                    if physical is None:
                        physical = observer._connection
                    assert observer._connection is physical
                    assert observer.execute(
                        "SELECT count(*) FROM temp.prd077_bad_source_skip"
                    ).fetchone()[0] == 0
                    if previous_skip:
                        observer.execute("INSERT INTO temp.prd077_bad_source_skip VALUES(1)")
                        observer.commit()
                finally:
                    observer.close()
    finally:
        _close_idle_database_connections()
