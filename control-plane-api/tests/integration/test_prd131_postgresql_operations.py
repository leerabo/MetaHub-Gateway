"""Native migration workflows; controlled actors are not independent approvals."""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd131_migration_operations as contracts
from metahub_control.migration_operations.permissions import (
    MIGRATION_ROLE_PERMISSIONS,
    load_verified_workspace_migration_permissions,
)
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.rbac_snapshot import require_actor_runtime_permissions

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", [
    "test_assessment_run_cutover_rollback_and_owner_isolation",
    "test_permission_expansion_and_projection_drift_are_blocking",
    "test_support_jit_summary_revocation_and_break_glass_rejection",
])
async def test_postgresql_migration_workflows(native_business_database, monkeypatch, scenario):
    config = contracts._config(native_business_database)
    runtime = config, contracts._owner_actor(config)
    await getattr(contracts, scenario)(runtime=runtime, monkeypatch=monkeypatch)


def test_postgresql_migration_catalog_and_read_only_projection(native_business_database):
    config = contracts._config(native_business_database)
    owner = contracts._owner_actor(config)
    with closing(connect_database(native_business_database)) as connection:
        for role, expected in {**MIGRATION_ROLE_PERMISSIONS, "viewer": ()}.items():
            assert load_verified_workspace_migration_permissions(
                connection, role_code=role, unavailable_error_code="migration.unavailable",
            ) == expected
        connection.execute("BEGIN")
        before = contracts.migration_service._assessment_write_watermark(connection)
        snapshots = contracts.migration_service._capture_snapshots(
            connection, owner, contracts.migration_service._DOMAINS,
        )
        assert len(snapshots) == 7
        assert all(snapshot.matches for snapshot in snapshots)
        assert contracts.migration_service._assessment_write_watermark(connection) == before
        assert connection.in_transaction
        connection.rollback()


def test_postgresql_runtime_permissions_require_every_catalog_table(native_business_database):
    owner = contracts._owner_actor(contracts._config(native_business_database))
    with closing(connect_database(native_business_database)) as connection:
        for table in (
            "roles", "permissions", "role_permissions", "rbac_permission_templates",
            "rbac_role_permission_templates",
        ):
            connection.execute("BEGIN")
            connection.execute(f"ALTER TABLE {table} RENAME TO unavailable_catalog")
            with pytest.raises(ApiError) as failure:
                require_actor_runtime_permissions(
                    connection, tenant_id=owner.tenant_id, user_id=owner.legacy_user_id,
                    permission_codes=("console.basic.read",),
                    unavailable_error_code="workspace_session.unavailable",
                )
            assert failure.value.code == "rbac.matrix_integrity_failed"
            assert connection.in_transaction
            connection.rollback()


def test_postgresql_assessment_rejects_and_rolls_back_adapter_writes(
    native_business_database, monkeypatch,
):
    config = contracts._config(native_business_database)
    owner = contracts._owner_actor(config)
    service = contracts.migration_service
    contracts._app(monkeypatch, config, {"context": owner})
    original = service._capture_snapshots

    def writing_adapter(connection, context, domains):
        snapshots = original(connection, context, domains)
        connection.execute(
            "UPDATE users SET display_name='unexpected-adapter-write' WHERE user_id=?",
            (owner.legacy_user_id,),
        )
        return snapshots

    monkeypatch.setattr(service, "_capture_snapshots", writing_adapter)
    with pytest.raises(contracts.migration_service.ApiError) as failure:
        service.create_migration_assessment(
            config, service.MigrationAssessmentCreateRequest.model_validate(
                contracts._assessment_payload(),
            ), session_token=contracts.WORKSPACE_TOKEN, csrf_token=contracts.WORKSPACE_CSRF,
            idempotency_key="prd131-adapter-write-0001",
            request_id="req_prd131_adapter", trace_id="trc_prd131_adapter",
        )
    assert failure.value.code == "migration.assessment_write_detected"
    with closing(connect_database(native_business_database)) as connection:
        assert connection.execute("SELECT count(*) FROM migration_assessments").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM migration_action_commands"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT display_name FROM users WHERE user_id=?", (owner.legacy_user_id,),
        ).fetchone()[0] == owner.display_name


def test_postgresql_new_seed_is_operational_but_reseed_preserves_existing_conflict(
    native_business_database, monkeypatch,
):
    config = contracts._config(native_business_database)
    owner = contracts._owner_actor(config)
    with closing(connect_database(native_business_database)) as connection:
        state = connection.execute(
            "SELECT status,revision FROM workspace_operational_states WHERE workspace_id=?",
            (owner.workspace_id,),
        ).fetchone()
        assert state["status"] == "active"
        connection.execute(
            "UPDATE workspace_operational_states "
            "SET status='migration_conflict',revision=revision+1 "
            "WHERE workspace_id=?", (owner.workspace_id,),
        )
        connection.commit()
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(48))
    apply_seed_data(native_business_database)
    with closing(connect_database(native_business_database)) as connection:
        assert tuple(connection.execute(
            "SELECT status,revision FROM workspace_operational_states WHERE workspace_id=?",
            (owner.workspace_id,),
        ).fetchone()) == ("migration_conflict", state["revision"] + 1)


def test_postgresql_operational_identity_delta_roundtrip_and_guards(native_business_database):
    with closing(connect_database(native_business_database)) as connection:
        before = dict(connection.execute("SELECT * FROM workspace_operational_states").fetchone())
        later_versions = [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations "
            "WHERE status='applied' AND version>'0140' ORDER BY version DESC"
        ).fetchall()]
    for version in later_versions:
        assert rollback_last_migration(
            native_business_database, expected_version=version,
        ).rolled_back == version
    assert rollback_last_migration(
        native_business_database, expected_version="0140",
    ).rolled_back == "0140"
    assert apply_migrations(native_business_database).applied == ["0140", *reversed(later_versions)]
    assert not apply_migrations(native_business_database).applied
    with closing(connect_database(native_business_database)) as connection:
        assert dict(connection.execute(
            "SELECT * FROM workspace_operational_states"
        ).fetchone()) == before
        for assignment in (
            "workspace_operational_state_id='wos_' || repeat('a',32),revision=revision+1",
            "workspace_id='wsp_' || repeat('b',32),revision=revision+1",
            "tenant_id='other',revision=revision+1",
            "created_at='2026-01-01T00:00:00Z',revision=revision+1",
            "revision=revision+2",
            "risk_revision=risk_revision+1,revision=revision+1",
            "updated_at='2026-01-01T00:00:00Z',revision=revision+1",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE workspace_operational_states SET {assignment}")
            connection.rollback()
        connection.execute("UPDATE workspace_operational_states SET revision=revision+1")
        connection.commit()
        assert connection.execute(
            "SELECT revision FROM workspace_operational_states"
        ).fetchone()[0] == before["revision"] + 1
