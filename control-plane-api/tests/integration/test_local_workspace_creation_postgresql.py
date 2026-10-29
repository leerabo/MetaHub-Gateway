"""Native PostgreSQL local admission and migration lifecycle contracts."""

import sqlite3
from pathlib import Path

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
from metahub_control.identity.self_service import register_local_account
from metahub_control.workspaces.service import create_workspace
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError
from migration_lifecycle_support import rollback_later_migrations

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database
NATIVE = Path(__file__).resolve().parents[3] / "infra/migrations/postgresql"


def test_native_local_signup_workspace_and_mailbox_separation(native_business_database):
    config = load_app_config("control-plane-api").model_copy(
        update={
            "database_url": native_business_database,
            "environment": "dev",
            "identity_session_secret": "native-local-signup-session-secret-2026",
            "identity_challenge_secret": "native-local-signup-challenge-secret-2026",
            "identity_challenge_delivery_mode": "disabled",
        }
    )
    issued = register_local_account(
        config,
        email="native-local-owner@example.test",
        display_name="Native Trial",
        password="NativeLocalTrial!2026",
        consent_version="privacy-local-2026-09-09",
        invitation_token=None,
        device_token="native-local-device",
        client_host="127.0.0.1",
        user_agent="native-test",
        request_id="native-signup",
        trace_id="native-signup",
    )
    fields = dict(
        session_token=issued.session_token,
        csrf_token=issued.csrf_token,
        idempotency_key="native-local-workspace-create-001",
        kind="organization",
        name="Native Trial Organization",
        slug="native-local-organization",
        region="cn-north-1",
        service_mode="internal_only",
        request_id="native-create",
        trace_id="native-create",
    )
    created = create_workspace(config, **fields)
    assert (
        create_workspace(config, **fields).workspace.workspace_id == created.workspace.workspace_id
    )
    connection = connect_database(native_business_database)
    try:
        admission = connection.execute(
            "SELECT * FROM local_workspace_creation_admissions WHERE workspace_id=?",
            (created.workspace.workspace_id,),
        ).fetchone()
        assert admission and admission["source_environment"] == "dev"
        assert connection.execute(
            "SELECT disposition FROM tenant_deletion_table_manifest "
            "WHERE source_table='local_workspace_creation_admissions'"
        ).fetchone()[0] == "archive_then_delete"
        assert connection.execute(
            "SELECT m.trigger_sha256=metahub_sha256(pg_get_triggerdef(t.oid)||';'||chr(10)"
            "||pg_get_functiondef(t.tgfoid)) FROM tenant_deletion_trigger_manifest m "
            "JOIN pg_trigger t ON t.tgname=m.trigger_name "
            "WHERE m.trigger_name='trg_users_projection_claim_validate_prd093'"
        ).fetchone()[0]
        assert not connection.execute(
            "SELECT 1 FROM account_verifications WHERE account_id=?",
            (admission["account_id"],),
        ).fetchall()
        assert (
            connection.execute(
                "SELECT role_code FROM workspace_memberships WHERE workspace_id=? AND account_id=?",
                (created.workspace.workspace_id, admission["account_id"]),
            ).fetchone()[0]
            == "owner"
        )
        connection.rollback()
        with pytest.raises(sqlite3.Error):
            connection.execute("UPDATE local_workspace_creation_admissions SET policy_revision=2")
        connection.rollback()
        with pytest.raises(sqlite3.Error):
            connection.executescript(
                (NATIVE / "0154_local_workspace_creation.down.sql").read_text()
            )
        connection.rollback()
    finally:
        connection.close()
    with pytest.raises(ApiError) as denied:
        create_workspace(config.model_copy(update={"environment": "prod"}), **fields)
    assert denied.value.code == "workspace.account_unverified"


def test_native_local_admission_pristine_rollback_upgrade(native_business_database):
    later = rollback_later_migrations(native_business_database, "0154")
    assert (
        rollback_last_migration(
            native_business_database,
            expected_version="0154",
        ).rolled_back
        == "0154"
    )
    assert apply_migrations(native_business_database).applied == ["0154", *later]
