"""Native database contracts for non-login employees and their gateway credentials."""

import sqlite3

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
from metahub_control.audit import AuditRequestMeta
from metahub_control.consumers.models import CreateConsumerRequest, UpdateConsumerRequest
from metahub_control.consumers.service import list_consumers, save_consumer
from metahub_control.identity.self_service import register_local_account
from metahub_control.virtual_keys.models import CreateVirtualKeyRequest, RotateVirtualKeyRequest
from metahub_control.virtual_keys.service import create_virtual_key, rotate_virtual_key
from metahub_control.workspace_access.service import (
    authenticate_workspace_session_as_auth_context,
    switch_workspace,
)
from metahub_control.workspaces.service import create_workspace
from metahub_gateway.auth.virtual_key import authenticate_virtual_key
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError
from migration_lifecycle_support import rollback_later_migrations

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


def test_native_consumers_create_rotate_disable_and_immutable_ownership(native_business_database):
    later = rollback_later_migrations(native_business_database, "0155")
    config = load_app_config("control-plane-api").model_copy(
        update={
            "database_url": native_business_database,
            "environment": "dev",
            "identity_session_secret": "consumer-native-global-session-secret-2026",
            "identity_challenge_secret": "consumer-native-challenge-secret-2026",
            "workspace_session_secret": "consumer-native-workspace-session-secret-2026",
            "identity_challenge_delivery_mode": "disabled",
        }
    )
    issued = register_local_account(
        config,
        email="native-consumer-owner@example.test",
        display_name="Owner",
        password="NativeConsumerOwner!2026",
        consent_version="privacy-local-2026-09-09",
        invitation_token=None,
        device_token="native-consumer-device",
        client_host="127.0.0.1",
        user_agent="native-test",
        request_id="consumer-native-register",
        trace_id="consumer-native",
    )
    created = create_workspace(
        config,
        session_token=issued.session_token,
        csrf_token=issued.csrf_token,
        idempotency_key="consumer-native-organization-01",
        kind="organization",
        name="Native Consumer Organization",
        slug="native-consumers",
        region="cn-north-1",
        service_mode="internal_only",
        request_id="consumer-native-organization",
        trace_id="consumer-native",
    )
    _, token, csrf = switch_workspace(
        config,
        global_session_token=issued.session_token,
        global_csrf_token=issued.csrf_token,
        workspace_id=created.workspace.workspace_id,
        request_id="consumer-native-switch",
        trace_id="consumer-native",
    )
    context = authenticate_workspace_session_as_auth_context(config, token, csrf_token=csrf)
    audit = AuditRequestMeta(client_ip=None, user_agent=None)
    employee = save_consumer(
        config,
        context,
        CreateConsumerRequest(display_name="Native Employee", department="Finance"),
        audit_request_meta=audit,
        request_id="consumer-native-create",
        trace_id="consumer-native",
    ).consumer
    key = create_virtual_key(
        config,
        context,
        CreateVirtualKeyRequest(name="Native Tool", consumer_id=employee.consumer_id),
        audit_request_meta=audit,
        request_id="consumer-native-key",
        trace_id="consumer-native",
    )
    assert (
        authenticate_virtual_key(config, "Bearer " + key.plain_value).consumer_id
        == employee.consumer_id
    )
    rotated = rotate_virtual_key(
        config,
        context,
        key.virtual_key.virtual_key_id,
        RotateVirtualKeyRequest(expected_revision=key.virtual_key.credential_revision),
        audit_request_meta=audit,
        request_id="consumer-native-rotate",
        trace_id="consumer-native",
    )
    with pytest.raises(ApiError):
        authenticate_virtual_key(config, "Bearer " + key.plain_value)
    assert (
        authenticate_virtual_key(config, "Bearer " + rotated.plain_value).consumer_id
        == employee.consumer_id
    )
    save_consumer(
        config,
        context,
        UpdateConsumerRequest(
            display_name="Native Employee", department="Finance", status="disabled", revision=1
        ),
        consumer_id=employee.consumer_id,
        audit_request_meta=audit,
        request_id="consumer-native-disable",
        trace_id="consumer-native",
    )
    with pytest.raises(ApiError):
        authenticate_virtual_key(config, "Bearer " + rotated.plain_value)
    assert (
        list_consumers(
            config,
            context,
            limit=50,
            offset=0,
            request_id="consumer-native-list",
            trace_id="consumer-native",
        ).total
        == 1
    )
    with connect_database(native_business_database) as connection:
        for statement in (
            "UPDATE virtual_key_consumers SET consumer_id=consumer_id",
            "DELETE FROM workspace_consumers",
        ):
            with pytest.raises(sqlite3.Error):
                connection.execute(statement)
            connection.rollback()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(native_business_database, expected_version="0155")
    assert blocked.value.code == "db.migration_failed"
    assert blocked.value.message == "Rollback failed for migration 0155."
    with connect_database(native_business_database) as connection:
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0155'"
        ).fetchone()[0] == "applied"
        assert connection.execute(
            "SELECT status FROM workspace_consumers WHERE consumer_id=?",
            (employee.consumer_id,),
        ).fetchone()[0] == "disabled"
        assert connection.execute(
            "SELECT consumer_id FROM virtual_key_consumers WHERE virtual_key_id=?",
            (key.virtual_key.virtual_key_id,),
        ).fetchone()[0] == employee.consumer_id
    assert apply_migrations(native_business_database).applied == later


def test_native_consumers_pristine_migration_lifecycle(native_business_database):
    later = rollback_later_migrations(native_business_database, "0155")
    assert (
        rollback_last_migration(native_business_database, expected_version="0155").rolled_back
        == "0155"
    )
    assert apply_migrations(native_business_database).applied == ["0155", *later]
    assert not apply_migrations(native_business_database).applied
