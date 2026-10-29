"""Ledger projections use the real identity and sequence columns without exposing values."""

import hashlib
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from metahub_control.migration_operations.service import _capture_domain


def test_ledger_projection_preserves_sequence_and_tenant_scope():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE finops_ledger_entries(
            ledger_sequence INTEGER PRIMARY KEY,finops_ledger_entry_id TEXT,
            tenant_id TEXT,entry_type TEXT,amount_micros INTEGER)""")
        connection.executemany("INSERT INTO finops_ledger_entries VALUES(?,?,?,?,?)", [
            (9, "entry-a", "tenant-one", "usage", 1000000),
            (17, "entry-b", "tenant-one", "adjustment", 2000000),
            (81, "entry-other", "tenant-two", "usage", 3000000),
        ])
        before = connection.total_changes
        snapshot = _capture_domain(
            connection, SimpleNamespace(tenant_id="tenant-one"), "ledger",
        )
        expected = b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            for row in [("entry-a", "usage", 9), ("entry-b", "adjustment", 17)]
        )
        assert snapshot.matches
        assert snapshot.source_count == 2
        assert snapshot.source_watermark == 2
        assert snapshot.source_sequence == 17
        assert snapshot.source_sha256 == hashlib.sha256(expected).hexdigest()
        assert connection.total_changes == before
        assert connection.in_transaction
