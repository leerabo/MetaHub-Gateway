"""A missing commercial table must not be accepted as an empty refund queue."""
import sqlite3

import pytest
from metahub_control.payment_commerce.service import _require_schema
from metahub_shared.runtime.errors import ApiError

TABLES = (
    "subscription_orders", "payment_intents", "payment_webhook_receipts",
    "payment_attempts", "refunds", "dunning_cases", "dunning_attempts",
    "commercial_effects",
)


@pytest.mark.parametrize("missing", (None, *TABLES))
def test_commercial_schema_requires_every_real_table(missing):
    with sqlite3.connect(":memory:") as connection:
        for table in TABLES:
            if table != missing:
                connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')
        if missing is None:
            _require_schema(connection)
        else:
            # A view with the expected name cannot substitute for the authority table.
            connection.execute(f'CREATE VIEW "{missing}" AS SELECT 1 AS id')
            with pytest.raises(ApiError) as error:
                _require_schema(connection)
            assert error.value.code == "payment.unavailable"
