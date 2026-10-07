"""Workspace USD/CNY comparison-rate resolution tests."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from metahub_shared.runtime.workspace_fx import resolve_workspace_fx_rate


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE workspace_tenant_bindings(
          workspace_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          status TEXT NOT NULL,
          PRIMARY KEY(workspace_id, tenant_id)
        );
        CREATE TABLE workspace_fx_rate_versions(
          workspace_fx_rate_version_id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          version_number INTEGER NOT NULL,
          usd_to_cny TEXT NOT NULL,
          effective_from TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        INSERT INTO workspace_tenant_bindings VALUES ('ws_test', 'tenant_test', 'active');
        """
    )
    return connection


def test_resolves_the_rate_effective_at_the_observed_time() -> None:
    connection = _connection()
    connection.executemany(
        "INSERT INTO workspace_fx_rate_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ("wfxv_old", "ws_test", "tenant_test", 1, "7.20", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            ("wfxv_new", "ws_test", "tenant_test", 2, "7.35", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
        ),
    )

    old = resolve_workspace_fx_rate(
        connection, tenant_id="tenant_test", observed_at="2026-08-31T23:59:59Z"
    )
    new = resolve_workspace_fx_rate(
        connection, tenant_id="tenant_test", observed_at="2026-09-17T00:00:00Z"
    )

    assert old is not None and old.usd_to_cny == Decimal("7.20")
    assert old.version_id == "wfxv_old"
    assert new is not None and new.usd_to_cny == Decimal("7.35")
    assert new.version_id == "wfxv_new"


def test_ignores_rates_from_inactive_workspace_binding() -> None:
    connection = _connection()
    connection.execute(
        "UPDATE workspace_tenant_bindings SET status='disabled' WHERE workspace_id='ws_test'"
    )
    connection.execute(
        "INSERT INTO workspace_fx_rate_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("wfxv_disabled", "ws_test", "tenant_test", 1, "7.35", "1970-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )

    assert (
        resolve_workspace_fx_rate(
            connection, tenant_id="tenant_test", observed_at="2026-09-17T00:00:00Z"
        )
        is None
    )
