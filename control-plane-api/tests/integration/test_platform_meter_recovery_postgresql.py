"""Native migration and concurrent metering classification, in one isolated database."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from threading import Barrier

import pytest
import test_postgresql_native_migrations as native
import test_platform_meter_recovery as cases
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.runtime.errors import ApiError

pytestmark = native.pytestmark
postgresql_database = native.postgresql_database


def test_native_recovery_migration_retry_archive_and_concurrent_exactly_once(postgresql_database, monkeypatch, tmp_path):
    database, app = cases.original._prepare(monkeypatch, tmp_path, database_url=postgresql_database)
    rollback_last_migration(database, expected_version="0156")
    assert apply_migrations(database).applied == ["0156"]
    with closing(connect_database(database)) as db:
        owner = db.execute("SELECT workspace_id,tenant_id FROM workspace_tenant_bindings LIMIT 1").fetchone()
    subject = (database, app, owner["workspace_id"], owner["tenant_id"])
    cases.failed_head(subject)
    retry = cases.request_for(app, "retry")
    assert not cases.recover(app, retry).idempotent_replay
    assert cases.recover(app, retry).idempotent_replay
    with cases.api_error("owner_invalid"):
        cases.billing.project_usage_once(app.state.config, worker_id="native-meter-retry")
    assert cases.state(app).blocked_fact.attempt_count == 2
    payload = cases.request_for(app)
    before = cases.counts(database)
    barrier = Barrier(2)

    def archive(index):
        barrier.wait(timeout=10)
        try:
            return cases.recover(app, payload, f"native-meter-concurrent-{index}")
        except ApiError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(archive, range(2)))
    assert sum(not isinstance(result, str) for result in results) == 1
    assert "platform_billing.revision_conflict" in results
    assert cases.counts(database) == before | {
        "checkpoint": 1,
        "platform_meter_recovery_actions": before["platform_meter_recovery_actions"] + 1,
        "platform_audit_logs": before["platform_audit_logs"] + 1,
    }
    assert cases.state(app).state == "idle"
    with pytest.raises(cases.DbError):
        rollback_last_migration(database, expected_version="0156")
    cases.original._insert_usage(database, owner["tenant_id"], datetime(2020, 1, 2, tzinfo=UTC))
    assert cases.state(app).state == "pending"
