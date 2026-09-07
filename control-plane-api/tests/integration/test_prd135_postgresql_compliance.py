"""Native evidence contracts, including the existing real machine OAuth flow."""

from __future__ import annotations

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd135_compliance_center as compliance

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


@pytest.fixture
def runtime(native_business_database, monkeypatch):
    return compliance._runtime(compliance._config(native_business_database), monkeypatch)


@pytest.mark.anyio
async def test_postgresql_real_oauth_collector(runtime, monkeypatch):
    await compliance.test_compliance_collector_real_oauth_and_machine_authentication(
        runtime, monkeypatch,
    )


@pytest.mark.anyio
async def test_postgresql_evidence_reports_replay_and_owner_boundaries(runtime):
    await compliance.test_trusted_evidence_reports_replay_and_owner_boundaries(runtime)


@pytest.mark.anyio
async def test_postgresql_failed_control_exception_and_scheduler(runtime):
    await compliance.test_failed_control_two_person_exception_and_scheduler(runtime)
