"""Reversal totals preserve exact integer facts across supported databases."""

import sqlite3
from contextlib import closing

import pytest
from metahub_control.finops.service import _load_reversal_total


def _connection():
    connection = sqlite3.connect(":memory:")
    connection.execute("""CREATE TABLE finops_ledger_entries(
        ledger_sequence INTEGER PRIMARY KEY, tenant_id TEXT, entry_type TEXT,
        reversal_of_entry_id TEXT, amount_micros)""")
    return connection


def test_reversal_total_is_exact_and_scoped_to_tenant_target_and_entry_type():
    with closing(_connection()) as connection:
        assert _load_reversal_total(connection, "tenant_a", "target_a") == 0
        connection.executemany(
            "INSERT INTO finops_ledger_entries VALUES(?, ?, ?, ?, ?)",
            [
                (2, "tenant_a", "reversal", "target_a", 9_000_000_000_000_000),
                (1, "tenant_a", "reversal", "target_a", 1),
                (3, "tenant_b", "reversal", "target_a", 99),
                (4, "tenant_a", "reversal", "target_b", 99),
                (5, "tenant_a", "manual_credit", "target_a", 99),
            ],
        )
        assert _load_reversal_total(connection, "tenant_a", "target_a") == 9_000_000_000_000_001


@pytest.mark.parametrize("amount", [None, "1", 1.0, -1, "NaN"])
def test_reversal_total_rejects_invalid_persisted_amount(amount):
    with closing(_connection()) as connection:
        connection.execute(
            "INSERT INTO finops_ledger_entries VALUES(1,'tenant_a','reversal','target_a',?)",
            (amount,),
        )
        with pytest.raises(ValueError, match="finops non-negative integer invalid"):
            _load_reversal_total(connection, "tenant_a", "target_a")


def test_reversal_total_rejects_sum_overflow_without_rounding():
    with closing(_connection()) as connection:
        connection.executemany(
            "INSERT INTO finops_ledger_entries VALUES(?,'tenant_a','reversal','target_a',?)",
            [(1, 9_223_372_036_854_775_807), (2, 1)],
        )
        with pytest.raises(ValueError, match="finops non-negative integer invalid"):
            _load_reversal_total(connection, "tenant_a", "target_a")
