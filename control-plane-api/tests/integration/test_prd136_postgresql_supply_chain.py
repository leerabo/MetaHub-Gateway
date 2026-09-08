"""Native supply-chain workflow contracts, not external scanner attestations."""

from __future__ import annotations

from contextlib import closing

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd136_supply_chain_security as supply_chain
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


@pytest.fixture
def runtime(native_business_database):
    return supply_chain._runtime(supply_chain._config(native_business_database))


@pytest.mark.anyio
async def test_postgresql_attestation_scan_exception_and_patch_lifecycle(runtime, monkeypatch):
    await supply_chain.test_attestation_reports_scan_and_dual_control_exception(
        runtime, monkeypatch,
    )


@pytest.mark.anyio
async def test_postgresql_strict_release_read(runtime, monkeypatch):
    await supply_chain.test_strict_release_read_rejects_unknown_and_body(runtime, monkeypatch)


@pytest.mark.parametrize("substitute", ["missing", "view", "temp", "other_schema"])
def test_postgresql_supply_chain_requires_current_real_tables(native_business_database, substitute):
    with closing(connect_database(native_business_database)) as connection:
        supply_chain.service._require_schema(connection)
        connection.rollback()
        connection.execute("ALTER TABLE supply_chain_events RENAME TO supply_chain_events_saved")
        if substitute == "view":
            connection.execute("CREATE VIEW supply_chain_events AS SELECT 1 AS id")
        elif substitute == "temp":
            connection.execute("CREATE TEMP TABLE supply_chain_events(id integer)")
        elif substitute == "other_schema":
            connection.execute("CREATE SCHEMA supply_chain_decoy")
            connection.execute("CREATE TABLE supply_chain_decoy.supply_chain_events(id integer)")
            connection.execute("SET LOCAL search_path TO public, supply_chain_decoy")
        with pytest.raises(ApiError) as error:
            supply_chain.service._require_schema(connection)
        assert error.value.code == "supply_chain.unavailable"
        assert connection.in_transaction
        connection.rollback()
        supply_chain.service._require_schema(connection)
