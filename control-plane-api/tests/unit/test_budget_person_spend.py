"""Employee spend lookup preserves known periods when older pricing is unverifiable."""

import sqlite3
from datetime import UTC, datetime

from metahub_control.budgets import service as budgets


def test_person_spend_marks_only_affected_periods_unavailable(monkeypatch):
    monkeypatch.setattr(budgets, "_utcnow", lambda: datetime(2026, 7, 9, 4, 0, tzinfo=UTC))
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
      CREATE TABLE virtual_keys(tenant_id TEXT, virtual_key_id TEXT, created_by TEXT);
      CREATE TABLE finops_ledger_entries(
        tenant_id TEXT, virtual_key_id TEXT, created_at TEXT, normalized_cny_micros INTEGER,
        cny_normalization_status TEXT, price_evidence_status TEXT, entry_type TEXT,
        direction TEXT, status TEXT, ledger_sequence INTEGER
      );
      INSERT INTO virtual_keys VALUES('t', 'k', 'u');
      INSERT INTO finops_ledger_entries VALUES
        ('t','k','2026-07-08T03:00:00Z',NULL,'unavailable','unpriced','usage_charge','debit','posted',1),
        ('t','k','2026-07-09T03:00:00Z',2000000,'normalized','priced','usage_charge','debit','posted',2);
    """)
    try:
        result = budgets._person_posted_spend(connection, "t", "u")
    finally:
        connection.close()
    assert result == {"day": "2", "month": None, "year": None}
