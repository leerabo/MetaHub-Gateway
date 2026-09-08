"""Native SaaS catalog only; complete subscription lifecycle remains unverified."""

from contextlib import closing

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd099_platform_billing as contracts
from httpx import ASGITransport, AsyncClient
from metahub_control.saas_billing import service
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database
REQUIRED = ("saas_plan_catalogs", "saas_plan_versions", "workspace_subscriptions",
            "subscription_trials", "subscription_order_records", "entitlement_snapshots",
            "saas_subscription_events")


def test_native_catalog_schema_requires_every_current_real_table(native_business_database):
    with closing(connect_database(native_business_database)) as connection:
        service._require_schema(connection)
        connection.rollback()
        for name in REQUIRED:
            connection.execute(f'ALTER TABLE "{name}" RENAME TO schema_probe_original')
            for substitute in (None, "VIEW", "TEMP TABLE"):
                if substitute == "VIEW":
                    connection.execute(f'CREATE VIEW "{name}" AS SELECT 1 AS id')
                elif substitute == "TEMP TABLE":
                    connection.execute(f'CREATE TEMP TABLE "{name}"(id INTEGER)')
                with pytest.raises(ApiError) as error:
                    service._require_schema(connection)
                assert error.value.code == "saas_catalog.unavailable"
                assert connection.in_transaction
                if substitute == "VIEW":
                    connection.execute(f'DROP VIEW "{name}"')
            connection.rollback()
            service._require_schema(connection)
            connection.rollback()


@pytest.mark.anyio
async def test_native_catalog_publish_and_public_projection(
    native_business_database, monkeypatch, tmp_path,
):
    _, app = contracts._prepare(monkeypatch, tmp_path, database_url=native_business_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as platform:
        platform.cookies.set(contracts.PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(contracts.PLATFORM_CSRF_COOKIE, "c" * 43)
        version = await contracts._create_plan(platform)
    result = service.list_public_plans(app.state.config, request_id="req_catalog_native",
                                       trace_id="trc_catalog_native")
    assert len(result.plans) == 1
    assert result.plans[0].versions[0].plan_version_id == version
    assert result.plans[0].versions[0].entitlements.gateway_requests_per_month == 100
