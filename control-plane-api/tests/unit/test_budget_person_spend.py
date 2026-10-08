"""员工用量摘要只计入本人名下凭证的可信正式分录。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from metahub_control.budgets import service
from metahub_shared.runtime.budget_spend import BudgetSpendUnavailableError


def _ledger() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE virtual_keys(tenant_id TEXT, virtual_key_id TEXT, created_by TEXT);
        CREATE TABLE finops_ledger_entries(
            tenant_id TEXT, virtual_key_id TEXT, created_at TEXT,
            normalized_cny_micros INTEGER, cny_normalization_status TEXT,
            price_evidence_status TEXT, entry_type TEXT, direction TEXT,
            status TEXT, ledger_sequence INTEGER
        );
        INSERT INTO virtual_keys VALUES('t', 'old_key', 'person_a');
        INSERT INTO virtual_keys VALUES('t', 'new_key', 'person_a');
        INSERT INTO virtual_keys VALUES('t', 'other_key', 'person_b');
    """)
    return db


def _charge(db: sqlite3.Connection, key: str, created_at: str, amount: int, *, evidence: str = "priced") -> None:
    db.execute("""
        INSERT INTO finops_ledger_entries VALUES(
            't', ?, ?, ?, 'normalized', ?, 'usage_charge', 'debit', 'posted',
            (SELECT COUNT(*)+1 FROM finops_ledger_entries)
        )
    """, (key, created_at, amount, evidence))


def test_person_spend_includes_replaced_key_and_calendar_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_utcnow", lambda: datetime(2026, 7, 9, 4, tzinfo=UTC))
    db = _ledger()
    _charge(db, "old_key", "2026-06-20T03:00:00Z", 11_000_000)
    _charge(db, "old_key", "2026-07-08T03:00:00Z", 7_000_000)
    _charge(db, "new_key", "2026-07-09T03:00:00Z", 5_000_000)
    _charge(db, "other_key", "2026-07-09T03:00:00Z", 99_000_000)

    totals = service._person_posted_spend(db, "t", "person_a")

    assert {period: Decimal(value) for period, value in totals.items()} == {
        "day": Decimal(5), "month": Decimal(12), "year": Decimal(23),
    }


def test_person_spend_fails_closed_on_unpriced_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_utcnow", lambda: datetime(2026, 7, 9, 4, tzinfo=UTC))
    db = _ledger()
    _charge(db, "old_key", "2026-07-09T03:00:00Z", 0, evidence="missing")

    with pytest.raises(BudgetSpendUnavailableError):
        service._person_posted_spend(db, "t", "person_a")
