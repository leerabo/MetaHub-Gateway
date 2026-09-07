"""Native supply-chain workflow contracts, not external scanner attestations."""

from __future__ import annotations

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd136_supply_chain_security as supply_chain

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
