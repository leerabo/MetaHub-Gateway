"""Resolve the exact Workspace ProviderCostRate used by Gateway accounting."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from metahub_shared.runtime.routing_policy import (
    MAX_PRICE_DECIMAL_PLACES,
    MAX_PRICE_PER_1K,
)


@dataclass(frozen=True, slots=True)
class ResolvedCostRate:
    provider_id: str
    provider_code: str
    model_id: str
    currency: str
    input_price_per_1k: Decimal
    output_price_per_1k: Decimal
    price_schema_version: str
    source: str
    source_id: str | None
    effective_from: str | None


def resolve_workspace_cost_rate(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    model_id: str,
    observed_at: str,
) -> ResolvedCostRate | None:
    """Return one exact active rate; target absence is unpriced, never reference fallback."""

    if _table_exists(connection, "workspace_provider_cost_rate_versions"):
        rows = connection.execute(
            """
            SELECT rate.provider_id,provider.provider_code,rate.model_id,rate.currency,
              version.input_rate_micros_per_1k,version.output_rate_micros_per_1k,
              version.workspace_provider_cost_rate_version_id,version.effective_from
            FROM workspace_tenant_bindings binding
            JOIN workspace_provider_cost_rates rate
              ON rate.workspace_id=binding.workspace_id AND rate.tenant_id=binding.tenant_id
            JOIN workspace_provider_cost_rate_heads head
              USING(workspace_provider_cost_rate_id)
            JOIN workspace_provider_cost_rate_versions head_version
              ON head_version.workspace_provider_cost_rate_version_id=
                 head.workspace_provider_cost_rate_version_id
             AND head_version.workspace_provider_cost_rate_id=
                 rate.workspace_provider_cost_rate_id
            JOIN workspace_provider_cost_rate_versions version
              ON version.workspace_provider_cost_rate_id=
                 rate.workspace_provider_cost_rate_id
            JOIN models model
              ON model.tenant_id=rate.tenant_id AND model.model_id=rate.model_id
             AND model.provider_id=rate.provider_id
            JOIN providers provider
              ON provider.tenant_id=rate.tenant_id
             AND provider.provider_id=rate.provider_id
            WHERE binding.tenant_id=? AND binding.status='active'
              AND rate.model_id=? AND rate.currency=model.currency
              AND head.adoption_state='active' AND head_version.status='active'
              AND version.status IN('active','superseded')
              AND unixepoch(version.effective_from)<=unixepoch(?)
              AND(version.effective_until IS NULL
                OR unixepoch(version.effective_until)>unixepoch(?))
              AND model.status='active' AND provider.status='active'
            ORDER BY version.effective_from DESC,
              version.workspace_provider_cost_rate_version_id DESC
            LIMIT 2
            """,
            (tenant_id, model_id, observed_at, observed_at),
        ).fetchall()
        if len(rows) > 1:
            raise sqlite3.IntegrityError("workspace_provider_cost_rate_ambiguous")
        if not rows:
            # A retired Workspace rate is an explicit migration boundary: no
            # active workspace price remains authoritative, so legacy model
            # catalog data may be read for old databases.  An active head with
            # no effective version (future/expired/invalid) stays fail-closed.
            active_head = connection.execute(
                """
                SELECT 1
                FROM workspace_provider_cost_rates rate
                JOIN workspace_provider_cost_rate_heads head
                  ON head.workspace_provider_cost_rate_id=rate.workspace_provider_cost_rate_id
                WHERE rate.tenant_id=? AND rate.model_id=?
                  AND head.adoption_state='active'
                LIMIT 1
                """,
                (tenant_id, model_id),
            ).fetchone()
            if active_head is not None:
                return None
            return _resolve_legacy_rate(
                connection, tenant_id=tenant_id, model_id=model_id, observed_at=observed_at
            )
        row = rows[0]
        version_id = str(row["workspace_provider_cost_rate_version_id"])
        return ResolvedCostRate(
            provider_id=str(row["provider_id"]),
            provider_code=str(row["provider_code"]),
            model_id=str(row["model_id"]),
            currency=_currency(row["currency"]),
            input_price_per_1k=_micros(row["input_rate_micros_per_1k"]),
            output_price_per_1k=_micros(row["output_rate_micros_per_1k"]),
            price_schema_version=f"provider-cost-v1:{version_id}",
            source="workspace_provider_cost_rate",
            source_id=version_id,
            effective_from=str(row["effective_from"]),
        )
    return _resolve_legacy_rate(
        connection, tenant_id=tenant_id, model_id=model_id, observed_at=observed_at
    )


def _resolve_legacy_rate(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    model_id: str,
    observed_at: str,
) -> ResolvedCostRate | None:
    row = connection.execute(
        """
        SELECT
          CASE WHEN plan.price_plan_id IS NOT NULL
            THEN plan.input_price_per_1k ELSE model.input_price_per_1k END input_price,
          CASE WHEN plan.price_plan_id IS NOT NULL
            THEN plan.output_price_per_1k ELSE model.output_price_per_1k END output_price,
          CASE WHEN plan.price_plan_id IS NOT NULL
            THEN plan.currency ELSE model.currency END currency,
          CASE WHEN plan.price_plan_id IS NOT NULL
            THEN plan.price_schema_version ELSE model.price_schema_version END schema_version,
          plan.price_plan_id,plan.effective_from,model.provider_id,provider.provider_code
        FROM models model
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
        LEFT JOIN model_price_plans plan ON plan.price_plan_id=(
          SELECT selected.price_plan_id FROM model_price_plans selected
          WHERE selected.tenant_id=model.tenant_id AND selected.model_id=model.model_id
            AND selected.effective_from<=?
          ORDER BY selected.effective_from DESC,selected.created_at DESC LIMIT 1)
        WHERE model.tenant_id=? AND model.model_id=?
          AND model.status='active' AND provider.status='active'
        """,
        (observed_at, tenant_id, model_id),
    ).fetchone()
    if row is None or not str(row["schema_version"] or "").strip():
        return None
    try:
        input_price = Decimal(str(row["input_price"]))
        output_price = Decimal(str(row["output_price"]))
    except (InvalidOperation, TypeError, ValueError):
        raise sqlite3.IntegrityError("legacy_provider_cost_rate_invalid") from None
    if (
        not input_price.is_finite()
        or not output_price.is_finite()
        or input_price < 0
        or output_price < 0
        or input_price > MAX_PRICE_PER_1K
        or output_price > MAX_PRICE_PER_1K
        or max(-input_price.as_tuple().exponent, 0) > MAX_PRICE_DECIMAL_PLACES
        or max(-output_price.as_tuple().exponent, 0) > MAX_PRICE_DECIMAL_PLACES
    ):
        raise sqlite3.IntegrityError("legacy_provider_cost_rate_invalid")
    price_plan_id = row["price_plan_id"]
    return ResolvedCostRate(
        provider_id=str(row["provider_id"]),
        provider_code=str(row["provider_code"]),
        model_id=model_id,
        currency=_currency(row["currency"]),
        input_price_per_1k=input_price,
        output_price_per_1k=output_price,
        price_schema_version=str(row["schema_version"]),
        source="price_plan" if price_plan_id is not None else "model_catalog",
        source_id=(str(price_plan_id) if price_plan_id is not None else None),
        effective_from=(
            str(row["effective_from"]) if row["effective_from"] is not None else None
        ),
    )


def _micros(value: object) -> Decimal:
    if type(value) is not int or value < 0:
        raise sqlite3.IntegrityError("workspace_provider_cost_rate_invalid")
    return Decimal(value) / Decimal(1_000_000)


def _currency(value: object) -> str:
    currency = str(value)
    if currency not in {"CNY", "USD"}:
        raise sqlite3.IntegrityError("workspace_provider_cost_rate_invalid")
    return currency


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )
