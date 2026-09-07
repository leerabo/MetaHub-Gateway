"""Native privacy contracts; controlled sessions are not real-login evidence."""

from __future__ import annotations

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd134_privacy_center as privacy

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


@pytest.fixture
def runtime(native_business_database, monkeypatch):
    return privacy._runtime(privacy._config(native_business_database), monkeypatch)


@pytest.mark.anyio
async def test_postgresql_preferences_export_once_and_minimal_platform_queue(runtime):
    await privacy.test_preferences_export_once_and_minimal_platform_queue(runtime)


@pytest.mark.anyio
async def test_postgresql_erasure_retention_and_idempotency(runtime):
    await privacy.test_erasure_decisions_retention_exceptions_and_idempotency(runtime)


def test_postgresql_scheduler_fencing(runtime):
    privacy.test_scheduler_lease_fences_concurrent_scans(runtime)
