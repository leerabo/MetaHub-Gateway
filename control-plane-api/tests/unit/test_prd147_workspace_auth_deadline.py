"""Both authentication phases share one deadline, including actual writer contention."""

import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from types import SimpleNamespace

import pytest
from metahub_control.workspace_access import service
from metahub_shared.runtime.errors import ApiError


def test_workspace_write_admission_uses_remaining_deadline(tmp_path, monkeypatch):
    path = tmp_path / "admission.sqlite3"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("CREATE TABLE lock_probe(id INTEGER)")
        writer.commit()
        reader = sqlite3.connect(path)
        timeouts = []
        writer_budgets = []

        def trace(statement):
            if statement.startswith("PRAGMA busy_timeout="):
                timeouts.append(int(statement.split("=", 1)[1]))
            elif statement == "BEGIN IMMEDIATE":
                writer_budgets.append(timeouts[-1])

        reader.set_trace_callback(trace)
        monkeypatch.setattr(service, "_connect", lambda _config: reader)
        phases = []

        def authenticate(_connection, _config, **kwargs):
            phases.append(kwargs.get("read_only", False))
            assert phases == [True], "writer admission must time out before authentication"
            time.sleep(0.300)
            return object(), {
                "last_seen_at": service._db_time(service._utcnow() - timedelta(seconds=61)),
            }

        monkeypatch.setattr(service, "_authenticate_workspace", authenticate)
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(ApiError) as error:
            service.authenticate_workspace_session_context(
                SimpleNamespace(), "synthetic-workspace-session",
                monotonic_deadline=started + 0.600,
                deadline_error_code="feedback.unavailable",
            )
        elapsed = time.monotonic() - started
        writer.rollback()
    assert error.value.code == "feedback.unavailable"
    assert phases == [True]
    assert len(writer_budgets) == 1
    assert 0 < writer_budgets[0] <= timeouts[0] - 250
    # Assert the actual SQLite setting as well as a coarse wall-clock bound;
    # this checks budget propagation, not the feedback endpoint's 250ms SLA.
    assert elapsed < 0.850
