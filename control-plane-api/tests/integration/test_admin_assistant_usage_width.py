"""Cost summaries retain 64-bit micro-CNY values and tenant/time boundaries."""

import sqlite3
from contextlib import closing

import pytest
import test_postgresql_native_migrations as native
from metahub_control.admin_assistant import repository
from metahub_shared.db import connect_database
from test_postgresql_background_connection_lifecycle import migrated_template as migrated_template
from test_postgresql_background_connection_lifecycle import (
    postgresql_database as postgresql_database,
)


@pytest.mark.parametrize(
    "backend",
    ["sqlite", pytest.param("postgresql", marks=native.pytestmark)],
)
def test_usage_cost_above_int32_micro_cny_preserves_scope_and_precision(backend, request):
    url = request.getfixturevalue("postgresql_database") if backend == "postgresql" else None
    connection = connect_database(url) if url else sqlite3.connect(":memory:")
    if not url:
        connection.row_factory = sqlite3.Row
    with closing(connection):
        connection.execute(
            "CREATE TEMP TABLE request_logs(tenant_id TEXT,status TEXT,latency_ms INTEGER,"
            "created_at TEXT)"
        )
        connection.execute(
            "CREATE TEMP TABLE usage_ledger(tenant_id TEXT,input_tokens INTEGER,"
            "output_tokens INTEGER,total_tokens INTEGER,cost_cny DOUBLE PRECISION,"
            "estimated INTEGER,created_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO request_logs VALUES(?,?,?,?)",
            [
                ("own", "success", 20, "2026-10-15T00:00:00Z"),
                ("own", "failed", 40, "2026-10-15T01:00:00Z"),
                ("foreign", "failed", 999, "2026-10-15T01:00:00Z"),
            ],
        )
        connection.executemany(
            "INSERT INTO usage_ledger VALUES(?,?,?,?,?,?,?)",
            [
                ("own", 4, 2, 6, 4000.125, 0, "2026-10-15T00:00:00Z"),
                ("own", 3, 3, 6, 0.000001, 1, "2026-10-15T01:00:00Z"),
                ("foreign", 100, 100, 200, 10000, 0, "2026-10-15T01:00:00Z"),
                ("own", 100, 100, 200, 10000, 0, "2026-10-16T00:00:00Z"),
            ],
        )
        values, counts = repository._usage_values(
            connection,
            "own",
            "2026-10-15T00:00:00Z",
            "2026-10-16T00:00:00Z",
        )
        assert values == {
            "request_count": 2,
            "success_count": 1,
            "failure_count": 1,
            "average_latency_ms": 30,
            "usage_count": 2,
            "input_tokens": 7,
            "output_tokens": 5,
            "total_tokens": 12,
            "cost_cny_micros": 4000125001,
            "estimated_count": 1,
        }
        assert counts == {"overview": 2, "usage": 2}
