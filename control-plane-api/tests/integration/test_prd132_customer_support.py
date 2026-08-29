"""PRD-132 ticket API, credential isolation, SLA, and secure attachment smoke tests."""

from __future__ import annotations

import secrets
import shutil
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.customer_support import service as support_service
from metahub_control.customer_support.routes import router
from metahub_control.files import service as file_service
from metahub_control.files.multipart import ParsedFilePart
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    workspace_actor_permissions_in_transaction,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43
PLATFORM_CSRF = "x" * 43
_TEST_PASSWORD = f"Prd132!Aa9{secrets.token_urlsafe(18)}"
_PLATFORM_OPERATOR_ID = "pto_" + "d" * 32


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _config(database_url: str, storage_root: Path) -> AppConfig:
    return AppConfig(
        service_name="control-plane-api",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=datetime.now(UTC),
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        identity_session_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        support_session_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        file_encryption_secret=secrets.token_urlsafe(32),
        file_storage_root=str(storage_root),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd132-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0124"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path
) -> tuple[AppConfig, WorkspaceSessionContext, PlatformContext]:
    database = tmp_path / "prd132.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database), tmp_path / "file-storage")
    owner = _owner_actor(config)
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """INSERT INTO platform_operators(
                 operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
               VALUES(?,?,'active',1,NULL,?,?)""",
            (
                _PLATFORM_OPERATOR_ID,
                owner.account_id,
                "2026-08-29T08:00:00.000Z",
                "2026-08-29T08:00:00.000Z",
            ),
        )
        connection.execute(
            """INSERT INTO platform_operator_role_bindings(
                 operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
               VALUES(?,'platform_support','active',NULL,?,NULL)""",
            (_PLATFORM_OPERATOR_ID, "2026-08-29T08:00:00.000Z"),
        )
        connection.commit()
    platform = PlatformContext(
        platform_session_id="pts_" + "2" * 32,
        operator_id=_PLATFORM_OPERATOR_ID,
        account_id=owner.account_id,
        global_session_id="gss_" + "2" * 32,
        factor_id="mfa_" + "2" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_support",),
        permissions=(
            "platform.support.sla.manage",
            "platform.support.ticket.manage",
            "platform.support.ticket.read",
        ),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return config, owner, platform


def _owner_actor(config: AppConfig) -> WorkspaceSessionContext:
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            """SELECT membership.workspace_membership_id,membership.workspace_id,
                      membership.tenant_id,membership.account_id,membership.legacy_user_id,
                      tenant.slug,tenant.name,user.email,user.display_name
                 FROM workspace_memberships membership
                 JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
                 JOIN users user ON user.user_id=membership.legacy_user_id
                  AND user.tenant_id=membership.tenant_id
                WHERE membership.role_code='owner' AND membership.status='active'
                ORDER BY membership.workspace_id LIMIT 1"""
        ).fetchone()
        assert row is not None
        connection.execute("BEGIN")
        snapshot = workspace_actor_permissions_in_transaction(
            connection,
            account_id=str(row["account_id"]),
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
        )
        connection.rollback()
    assert {
        "support.ticket.read",
        "support.ticket.create",
        "support.ticket.manage",
    } <= set(snapshot.permission_codes)
    return WorkspaceSessionContext(
        workspace_session_id="wss_" + "1" * 32,
        global_session_id="gss_" + "1" * 32,
        account_id=str(row["account_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        membership_id=str(row["workspace_membership_id"]),
        legacy_user_id=str(row["legacy_user_id"]),
        membership_role="owner",
        legacy_role="admin",
        account_email=str(row["email"]),
        display_name=str(row["display_name"]),
        tenant_slug=str(row["slug"]),
        tenant_name=str(row["name"]),
        permission_codes=snapshot.permission_codes,
        permission_revision=snapshot.permission_revision,
        authorization_fingerprint=snapshot.authorization_fingerprint,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        token=WORKSPACE_TOKEN,
        legacy_authorization_fingerprint=snapshot.authorization_fingerprint,
    )


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    owner: WorkspaceSessionContext,
    platform: PlatformContext,
):
    monkeypatch.setattr(
        support_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: owner,
    )
    monkeypatch.setattr(
        support_service,
        "authenticate_platform_session_in_transaction",
        lambda *_args, **_kwargs: platform,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-132 Customer Support Test",
        extra_cors_allow_headers=("Idempotency-Key", "If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def _workspace_headers(*, key: str | None = None, revision: int | None = None) -> dict[str, str]:
    result = {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": WORKSPACE_CSRF,
    }
    if key is not None:
        result["Idempotency-Key"] = key
    if revision is not None:
        result["If-Match"] = f'"{revision}"'
    return result


def _platform_headers(*, key: str | None = None, revision: int | None = None) -> dict[str, str]:
    result = {
        "Cookie": (
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; {PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": PLATFORM_CSRF,
    }
    if key is not None:
        result["Idempotency-Key"] = key
    if revision is not None:
        result["If-Match"] = f'"{revision}"'
    return result


def _payload(*, severity: str = "p1") -> dict[str, object]:
    return {
        "category": "reliability",
        "severity": severity,
        "subject": "Gateway requests fail after deployment",
        "message": "The failure is reproducible in the owning Workspace.",
        "related_kind": "none",
        "related_reference": None,
    }


@pytest.mark.anyio
async def test_dual_domain_ticket_internal_note_and_idempotency(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, owner, platform = runtime
    app = _app(monkeypatch, config, owner, platform)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/support/tickets",
            headers=_workspace_headers(key="prd132-create-ticket-0001"),
            json=_payload(),
        )
        assert created.status_code == 201
        ticket = created.json()["ticket"]
        ticket_id = ticket["ticket_id"]
        replay = await client.post(
            "/support/tickets",
            headers=_workspace_headers(key="prd132-create-ticket-0001"),
            json=_payload(),
        )
        assert replay.status_code == 200
        assert replay.json()["ticket"]["ticket_id"] == ticket_id

        internal_text = "INTERNAL_ONLY_PLATFORM_TRIAGE_NOTE"
        platform_update = await client.patch(
            f"/support/tickets/{ticket_id}",
            headers=_platform_headers(
                key="prd132-platform-patch-0001", revision=ticket["revision"]
            ),
            json={
                "status": "triaged",
                "message": internal_text,
                "message_visibility": "internal",
                "reason_code": "initial_triage",
            },
        )
        assert platform_update.status_code == 200
        assert any(
            item["visibility"] == "internal" and item["body"] == internal_text
            for item in platform_update.json()["ticket"]["messages"]
        )

        workspace_list = await client.get(
            "/support/tickets",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert workspace_list.status_code == 200
        workspace_body = workspace_list.json()
        assert internal_text not in workspace_list.text
        assert {item["visibility"] for item in workspace_body["items"][0]["messages"]} == {
            "customer"
        }

        conflict = await client.get(
            "/support/tickets",
            headers={
                "Cookie": (
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
                    f"{SUPPORT_SESSION_COOKIE}=support_{'s' * 50}"
                )
            },
        )
        assert conflict.json()["code"] == "workspace_session.credential_conflict"
        denied = await client.post(
            "/support/tickets",
            headers=_platform_headers(key="prd132-platform-create-0001"),
            json=_payload(),
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "support.permission_denied"

    with closing(connect_database(config.database_url)) as connection:
        persisted = connection.execute(
            """SELECT ticket.subject_ciphertext,message.body_ciphertext
                 FROM support_tickets ticket
                 JOIN support_ticket_messages message
                   ON message.support_ticket_id=ticket.support_ticket_id
                WHERE ticket.support_ticket_id=? ORDER BY message.created_at""",
            (ticket_id,),
        ).fetchall()
        assert all("Gateway requests fail" not in str(row[0]) for row in persisted)
        assert all(internal_text not in str(row[1]) for row in persisted)


@pytest.mark.anyio
async def test_p1_sla_scheduler_escalates_once(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, owner, platform = runtime
    app = _app(monkeypatch, config, owner, platform)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/support/tickets",
            headers=_workspace_headers(key="prd132-create-ticket-sla1"),
            json=_payload(),
        )
    assert created.status_code == 201
    ticket_id = created.json()["ticket"]["ticket_id"]
    assert support_service.process_support_sla_once(config, worker_id="prd132-test-worker") == 1
    assert support_service.process_support_sla_once(config, worker_id="prd132-test-worker") == 0
    with closing(connect_database(config.database_url)) as connection:
        escalation = connection.execute(
            """SELECT source_code,generation,status FROM support_escalations
               WHERE support_ticket_id=?""",
            (ticket_id,),
        ).fetchall()
        assert [tuple(row) for row in escalation] == [("sla_at_risk", 1, "open")]
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM support_ticket_events
                   WHERE support_ticket_id=? AND event_type='support.sla_at_risk'""",
                (ticket_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM support_event_outbox outbox
                   JOIN support_ticket_events event
                     ON event.support_ticket_event_id=outbox.support_ticket_event_id
                  WHERE event.support_ticket_id=? AND event.event_type='support.sla_at_risk'""",
                (ticket_id,),
            ).fetchone()[0]
            == 2
        )


@pytest.mark.anyio
async def test_attachment_quarantine_scan_and_timeout(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, owner, platform = runtime
    file_actor = owner.as_auth_context()
    monkeypatch.setattr(
        file_service,
        "require_transaction_permission",
        lambda *_args, **_kwargs: file_actor,
    )
    file_result, _events = file_service.create_file(
        config,
        file_actor,
        ParsedFilePart(
            filename="diagnostic.txt",
            media_type="text/plain",
            content=b"bounded diagnostic evidence without credentials",
        ),
        request_id="prd132-file-upload",
        trace_id="prd132-file-upload-trace",
    )
    app = _app(monkeypatch, config, owner, platform)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/support/tickets",
            headers=_workspace_headers(key="prd132-create-ticket-file"),
            json=_payload(severity="p3"),
        )
        ticket = created.json()["ticket"]
        attached = await client.post(
            f"/support/tickets/{ticket['ticket_id']}/attachments",
            headers=_workspace_headers(
                key="prd132-create-attachment1", revision=ticket["revision"]
            ),
            json={
                "file_id": file_result.file.file_id,
                "intent_code": "diagnostic_evidence",
                "confirmed": True,
            },
        )
        assert attached.status_code == 201
        assert attached.json()["attachment"]["status"] == "quarantined"
        assert "file_id" not in attached.json()["attachment"]
        assert support_service.process_attachment_scans(config) == 1
        listed = await client.get(
            "/support/tickets",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        attachment = listed.json()["items"][0]["attachments"][0]
        assert attachment["status"] == "available"
        assert attachment["scan_outcome"] == "clean"

        current_revision = listed.json()["items"][0]["revision"]
        second = await client.post(
            f"/support/tickets/{ticket['ticket_id']}/attachments",
            headers=_workspace_headers(key="prd132-create-attachment2", revision=current_revision),
            json={
                "file_id": file_result.file.file_id,
                "intent_code": "reproduction",
                "confirmed": True,
            },
        )
        assert second.status_code == 201
        assert (
            support_service.process_attachment_scans(
                config, now=datetime.now(UTC) + timedelta(minutes=10)
            )
            == 1
        )
        final = await client.get(
            "/support/tickets",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        statuses = {item["status"] for item in final.json()["items"][0]["attachments"]}
        assert statuses == {"available", "scan_timeout"}
