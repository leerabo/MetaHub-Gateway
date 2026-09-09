"""Native parity for tenant-scoped membership status and global account isolation."""

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from test_workspace_membership_scope import verify_membership_scope

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


@pytest.mark.anyio
async def test_native_membership_status_scope(native_business_database, tmp_path, monkeypatch):
    await verify_membership_scope(native_business_database, tmp_path, monkeypatch)


def test_native_membership_status_roundtrip(native_business_database):
    assert (
        rollback_last_migration(native_business_database, expected_version="0157").rolled_back
        == "0157"
    )
    assert apply_migrations(native_business_database).applied == ["0157"]
    with connect_database(native_business_database) as connection:
        row = connection.execute(
            "SELECT m.trigger_sha256=metahub_sha256(pg_get_triggerdef(t.oid)||';'||chr(10)"
            "||pg_get_functiondef(t.tgfoid)) FROM pg_trigger t "
            "JOIN tenant_deletion_trigger_manifest m ON m.trigger_name=t.tgname "
            "WHERE t.tgrelid='users'::regclass "
            "AND t.tgname='trg_users_identity_status_update_prd091'",
        ).fetchone()
        assert row[0]
