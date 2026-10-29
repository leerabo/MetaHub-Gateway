"""Owner-scoped exception revisions invalidate reports on every transition."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from metahub_control.compliance_center import service


def test_watermark_tracks_inserts_approvals_and_expiry_not_physical_rowid():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            "CREATE TABLE compliance_exceptions (id TEXT PRIMARY KEY, workspace_id TEXT, "
            "tenant_id TEXT, revision INTEGER) WITHOUT ROWID"
        )
        def watermark():
            return service._exception_watermark(connection, "workspace", "tenant")

        assert watermark() == 0
        connection.execute(
            "INSERT INTO compliance_exceptions VALUES('first','workspace','tenant',1)"
        )
        assert watermark() == 1
        connection.execute("UPDATE compliance_exceptions SET revision=2 WHERE id='first'")
        assert watermark() == 2
        connection.execute(
            "INSERT INTO compliance_exceptions VALUES('other','foreign','tenant',99)"
        )
        assert watermark() == 2
        connection.execute(
            "INSERT INTO compliance_exceptions VALUES('second','workspace','tenant',1)"
        )
        assert watermark() == 3
        connection.execute("UPDATE compliance_exceptions SET revision=3 WHERE id='first'")
        assert watermark() == 4


@pytest.mark.parametrize("microseconds", [0, 1, 999, 1000, 123456, 999999])
def test_exception_updates_advance_at_persisted_precision(microseconds):
    now = datetime(2026, 9, 7, 1, 2, 3, microseconds, tzinfo=UTC)
    previous = service._db_time(now)
    changed = service._db_time(service._changed_time(now, previous))
    assert service._time(changed) > service._time(previous)
