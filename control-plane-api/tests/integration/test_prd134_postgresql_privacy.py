"""Native privacy contracts; controlled sessions are not real-login evidence."""

from __future__ import annotations

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd134_owner_leases as owner_leases
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


@pytest.mark.anyio
@pytest.mark.parametrize("contract", [
    owner_leases.test_claim_binding_forgery_completion_and_failure_are_noops,
    owner_leases.test_exhausted_crash_recovery_is_bounded_and_other_request_progresses,
    owner_leases.test_concurrent_workers_claim_once_and_assemble_one_export,
])
async def test_postgresql_owner_lease_contract(runtime, contract):
    await contract(runtime)


@pytest.mark.anyio
async def test_postgresql_expired_owner_takeover(runtime, monkeypatch):
    await owner_leases.test_expiry_takeover_and_late_failure_preserve_replacement(
        runtime, monkeypatch,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["adapter", "assembly", "failure", "cancelled"])
async def test_postgresql_expiry_during_owner_work(runtime, monkeypatch, phase):
    await owner_leases.test_expiry_during_work_rolls_back_all_writes(runtime, monkeypatch, phase)


def test_postgresql_owner_database_time_advances(runtime):
    owner_leases.test_database_time_advances_inside_one_transaction(runtime)
