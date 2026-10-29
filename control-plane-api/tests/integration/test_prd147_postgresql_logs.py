"""Run the same business contracts against a real native PostgreSQL database."""

from contextlib import aclosing

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd147_workspace_logs as contracts
from test_prd147_workspace_files import workspace_http_subjects

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


@pytest.fixture
async def log_subjects(native_business_database, tmp_path, monkeypatch):
    async with aclosing(
        workspace_http_subjects(native_business_database, tmp_path, monkeypatch)
    ) as subjects:
        async for app, fixtures in subjects:
            app.include_router(contracts.log_routes.router)
            app.include_router(contracts.audit_routes.router)
            yield app, native_business_database, fixtures


test_workspace_query_fact_and_personal_filter_ownership = (
    contracts.test_workspace_query_fact_and_personal_filter_ownership
)
test_workspace_audit_export_is_csrf_protected_and_replayable = (
    contracts.test_workspace_audit_export_is_csrf_protected_and_replayable
)
test_log_identity_history_roundtrip_and_explicit_legacy = (
    contracts.test_log_identity_history_roundtrip_and_explicit_legacy
)
test_workspace_query_owner_outbox_and_lossless_rollback_guard = (
    contracts.test_workspace_query_owner_outbox_and_lossless_rollback_guard
)
test_log_terminal_expiry_rolls_back_all_facts = (
    contracts.test_log_terminal_expiry_rolls_back_all_facts
)
test_log_migration_rejects_schema_drift_and_preserves_privacy_manifest = (
    contracts.test_log_migration_rejects_schema_drift_and_preserves_privacy_manifest
)
