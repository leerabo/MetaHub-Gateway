"""Read a workspace's effective USD/CNY comparison rate in the caller's DB snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkspaceFxRate:
    version_id: str
    workspace_id: str
    tenant_id: str
    version_number: int
    usd_to_cny: Decimal
    effective_from: str


def resolve_workspace_fx_rate(
    connection: Any, *, tenant_id: str, observed_at: str
) -> WorkspaceFxRate | None:
    rows = connection.execute(
        """
        SELECT fx.* FROM workspace_fx_rate_versions fx
        JOIN workspace_tenant_bindings binding
          ON binding.workspace_id=fx.workspace_id AND binding.tenant_id=fx.tenant_id
        WHERE fx.tenant_id=? AND binding.status='active'
          AND unixepoch(fx.effective_from)<=unixepoch(?)
        ORDER BY fx.effective_from DESC,fx.version_number DESC LIMIT 2
        """,
        (tenant_id, observed_at),
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    try:
        rate = Decimal(str(row["usd_to_cny"]))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("workspace fx rate invalid") from exc
    if not rate.is_finite() or not Decimal("0") < rate <= Decimal("100"):
        raise ValueError("workspace fx rate invalid")
    if len(rows) > 1 and str(rows[1]["effective_from"]) == str(row["effective_from"]):
        raise ValueError("workspace fx interval ambiguous")
    return WorkspaceFxRate(
        version_id=str(row["workspace_fx_rate_version_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        version_number=int(row["version_number"]),
        usd_to_cny=rate,
        effective_from=str(row["effective_from"]),
    )
