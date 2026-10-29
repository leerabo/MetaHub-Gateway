"""Native customer/internal ticket, attachment, and SLA contracts."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd132_customer_support as contracts
from httpx import ASGITransport, AsyncClient
from metahub_control.customer_support.permissions import (
    CUSTOMER_SUPPORT_ROLE_PERMISSIONS,
    load_verified_workspace_customer_support_permissions,
)
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", [
    "test_dual_domain_ticket_internal_note_and_idempotency",
    "test_p1_sla_scheduler_escalates_once",
    "test_attachment_quarantine_scan_and_timeout",
])
async def test_postgresql_support_workflows(
    native_business_database, monkeypatch, tmp_path, scenario,
):
    config = contracts._config(native_business_database, tmp_path / "file-storage")
    runtime = contracts._runtime(config)
    await getattr(contracts, scenario)(runtime=runtime, monkeypatch=monkeypatch)


def test_postgresql_support_permission_catalog(native_business_database):
    with closing(connect_database(native_business_database)) as connection:
        for role, expected in {**CUSTOMER_SUPPORT_ROLE_PERMISSIONS, "unknown": ()}.items():
            assert load_verified_workspace_customer_support_permissions(
                connection, role_code=role, unavailable_error_code="support.unavailable",
            ) == expected


@pytest.mark.anyio
async def test_postgresql_ticket_cas_rejects_a_committed_competing_update(
    native_business_database, monkeypatch, tmp_path,
):
    config = contracts._config(native_business_database, tmp_path / "file-storage")
    config, owner, platform = contracts._runtime(config)
    app = contracts._app(monkeypatch, config, owner, platform)
    service = contracts.support_service
    original = service._authorize_ticket_mutation

    def concurrent_update(actor, row, payload):
        original(actor, row, payload)
        with closing(connect_database(native_business_database)) as competitor:
            competitor.execute(
                "UPDATE support_tickets SET revision=revision+1,updated_at=? "
                "WHERE support_ticket_id=?",
                (service._db_time(service._time(row["updated_at"]) + timedelta(milliseconds=1)),
                 row["support_ticket_id"]),
            )
            competitor.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/support/tickets", headers=contracts._workspace_headers(key="prd132-cas-create-0001"),
            json=contracts._payload(),
        )
        assert created.status_code == 201
        ticket_id = created.json()["ticket"]["ticket_id"]
        monkeypatch.setattr(service, "_authorize_ticket_mutation", concurrent_update)
        rejected = await client.patch(
            f"/support/tickets/{ticket_id}",
            headers=contracts._platform_headers(key="prd132-cas-patch-0001", revision=1),
            json={"status": "triaged", "message": "must-not-persist",
                  "message_visibility": "internal", "reason_code": "initial_triage"},
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "support.revision_conflict"
    with closing(connect_database(native_business_database)) as connection:
        assert tuple(connection.execute(
            "SELECT status,revision FROM support_tickets WHERE support_ticket_id=?", (ticket_id,),
        ).fetchone()) == ("open", 2)
        assert connection.execute(
            "SELECT count(*) FROM support_ticket_messages WHERE support_ticket_id=?", (ticket_id,),
        ).fetchone()[0] == 1


def test_postgresql_file_identity_delta_roundtrip_preserves_facts_and_guards(
    native_business_database, monkeypatch, tmp_path,
):
    config = contracts._config(native_business_database, tmp_path / "file-storage")
    owner = contracts._owner_actor(config)
    actor = owner.as_auth_context()
    monkeypatch.setattr(
        contracts.file_service, "require_transaction_permission", lambda *_args, **_kwargs: actor,
    )
    result, _events = contracts.file_service.create_file(
        config, actor, contracts.ParsedFilePart(
            filename="identity-check.txt", media_type="text/plain", content=b"file identity check",
        ), request_id="req_prd132_file_identity", trace_id="trc_prd132_file_identity",
    )
    file_id = result.file.file_id
    with closing(connect_database(native_business_database)) as connection:
        before = dict(connection.execute(
            "SELECT * FROM uploaded_files WHERE file_id=?", (file_id,),
        ).fetchone())
        extracts_before = [dict(row) for row in connection.execute(
            "SELECT * FROM file_extracts WHERE file_id=?", (file_id,),
        ).fetchall()]
    assert before["status"] == "ready"
    assert extracts_before
    assert rollback_last_migration(
        native_business_database, expected_version="0141",
    ).rolled_back == "0141"
    assert apply_migrations(native_business_database).applied == ["0141"]
    assert not apply_migrations(native_business_database).applied
    with closing(connect_database(native_business_database)) as connection:
        assert dict(connection.execute(
            "SELECT * FROM uploaded_files WHERE file_id=?", (file_id,),
        ).fetchone()) == before
        assert [dict(row) for row in connection.execute(
            "SELECT * FROM file_extracts WHERE file_id=?", (file_id,),
        ).fetchall()] == extracts_before
        for assignment in (
            "file_id='other-file'", "tenant_id='other-tenant'", "uploaded_by='other-user'",
            "source_request_id='other-request'", "content_fingerprint=repeat('a',64)",
            "created_at='2026-01-01T00:00:00Z'", "status='processing'", "status='blocked'",
            "match_count=match_count+1", "completed_at=NULL",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE uploaded_files SET {assignment} WHERE file_id=?", (file_id,),
                )
            connection.rollback()
        connection.execute("UPDATE uploaded_files SET status=status WHERE file_id=?", (file_id,))
        connection.commit()
        assert dict(connection.execute(
            "SELECT * FROM uploaded_files WHERE file_id=?", (file_id,),
        ).fetchone()) == before
