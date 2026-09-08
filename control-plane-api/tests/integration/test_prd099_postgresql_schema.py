"""Native billing schema boundaries; not end-to-end metering acceptance."""

from contextlib import closing

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
from metahub_control.platform_billing import service
from metahub_shared.db import connect_database
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database
REQUIRED = ("platform_meter_definitions", "platform_meter_readings", "platform_billing_cycles",
            "platform_quota_allocations", "platform_billing_summaries",
            "platform_billing_adjustments", "platform_meter_outbox", "platform_meter_checkpoints")


def test_native_metering_schema_requires_every_current_real_table(native_business_database):
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
                assert error.value.code == "platform_billing.unavailable"
                assert connection.in_transaction
                if substitute == "VIEW":
                    connection.execute(f'DROP VIEW "{name}"')
            connection.rollback()
            service._require_schema(connection)
            connection.rollback()


def test_native_empty_projection_retains_checkpoint(native_business_database, monkeypatch):
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_DATABASE_URL", native_business_database)
    config = load_app_config("control-plane-api")
    with closing(connect_database(native_business_database)) as connection:
        before = tuple(connection.execute("SELECT * FROM platform_meter_checkpoints").fetchone())
    assert not service.project_usage_once(config, worker_id="native-empty-projection")
    with closing(connect_database(native_business_database)) as connection:
        after = tuple(connection.execute("SELECT * FROM platform_meter_checkpoints").fetchone())
    assert after == before
