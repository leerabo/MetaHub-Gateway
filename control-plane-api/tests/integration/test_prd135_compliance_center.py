"""PRD-135 machine evidence, exceptions, reports, and freshness orchestration."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import shutil
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.compliance_center import routes, service
from metahub_control.compliance_center.routes import router
from metahub_control.machine_identity import service as machine_service
from metahub_control.machine_identity.routes import router as machine_router
from metahub_control.machine_identity.service import MachineAuthContext
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import WorkspaceSessionContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
MACHINE_TOKEN = "mat_" + "m" * 43
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "x" * 43
PLATFORM_TOKEN = "p" * 43
PLATFORM_CSRF = "c" * 43
SERVICE_ACCOUNT_ID = "svc_" + "c" * 32
SECOND_ACCOUNT_ID = "gac_" + "d" * 32
CONTROL_ACCESS = "ctl_00000000000000000000000000000001"
CONTROL_ENCRYPTION = "ctl_00000000000000000000000000000002"
CONTROL_PRIVACY = "ctl_00000000000000000000000000000003"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _config(database_url: str) -> AppConfig:
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
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(48),
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd135-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0127"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AppConfig, WorkspaceSessionContext, MachineAuthContext, dict[str, PlatformContext]]:
    database = tmp_path / "prd135.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    stamp = "2026-08-30T00:00:00.000Z"
    with closing(connect_database(config.database_url)) as connection:
        owner = connection.execute(
            """SELECT member.workspace_membership_id,member.workspace_id,member.tenant_id,
                      member.account_id,member.legacy_user_id,account.email_normalized,
                      account.display_name,tenant.slug,tenant.name
                 FROM workspace_memberships member
                 JOIN global_accounts account ON account.account_id=member.account_id
                 JOIN tenants tenant ON tenant.tenant_id=member.tenant_id
                WHERE member.status='active' AND member.role_code='owner'
                ORDER BY member.workspace_id LIMIT 1"""
        ).fetchone()
        assert owner is not None
        connection.execute(
            """INSERT INTO global_accounts(
                 account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                 credential_revision,session_revision,mfa_state,failed_login_count,
                 locked_until,last_login_at,created_at,updated_at)
               VALUES(?,'compliance-approver@example.invalid',NULL,'Compliance approver',
                 'active',0,1,1,'not_enrolled',0,NULL,NULL,?,?)""",
            (SECOND_ACCOUNT_ID, stamp, stamp),
        )
        connection.execute(
            """INSERT INTO service_accounts(
                 service_account_id,workspace_id,tenant_id,name,display_name,status,
                 scope_template,catalog_revision,constraint_sha256,constraint_generation,
                 ip_allowlist_json,mtls_fingerprint_sha256,token_ttl_seconds,revision,
                 created_by_account_id,updated_by_account_id,last_used_at,revoked_at,
                 created_at,updated_at)
               VALUES(?,?,?,'compliance-collector','Compliance collector','active',
                 'compliance_collector',1,?,1,'[]',NULL,3600,1,?,?,NULL,NULL,?,?)""",
            (
                SERVICE_ACCOUNT_ID,
                owner["workspace_id"],
                owner["tenant_id"],
                "a" * 64,
                owner["account_id"],
                owner["account_id"],
                stamp,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO service_account_scopes(service_account_id,permission_code,created_at) "
            "VALUES(?,'compliance.collect',?)",
            (SERVICE_ACCOUNT_ID, stamp),
        )
        connection.commit()
    expires = datetime.now(UTC) + timedelta(hours=1)
    workspace_actor = WorkspaceSessionContext(
        workspace_session_id="wss_" + "a" * 32,
        global_session_id="gss_" + "a" * 32,
        account_id=str(owner["account_id"]),
        workspace_id=str(owner["workspace_id"]),
        tenant_id=str(owner["tenant_id"]),
        membership_id=str(owner["workspace_membership_id"]),
        legacy_user_id=str(owner["legacy_user_id"]),
        membership_role="security_admin",
        legacy_role="security_admin",
        account_email=str(owner["email_normalized"]),
        display_name=str(owner["display_name"]),
        tenant_slug=str(owner["slug"]),
        tenant_name=str(owner["name"]),
        permission_codes=("compliance.collect", "compliance.report.read"),
        permission_revision=47,
        authorization_fingerprint="b" * 64,
        expires_at=expires,
        token=WORKSPACE_TOKEN,
    )
    machine = MachineAuthContext(
        subject_type="service_account",
        service_account_id=SERVICE_ACCOUNT_ID,
        oauth_client_id="ocl_" + "c" * 32,
        access_token_grant_id="atg_" + "c" * 32,
        workspace_id=workspace_actor.workspace_id,
        tenant_id=workspace_actor.tenant_id,
        audience="metahub-control",
        permission_codes=("compliance.collect",),
        permission_revision=47,
        authorization_fingerprint="c" * 64,
        expires_at=expires,
    )
    requester = PlatformContext(
        platform_session_id="pts_" + "1" * 32,
        operator_id="pto_" + "1" * 32,
        account_id=workspace_actor.account_id,
        global_session_id="gss_" + "1" * 32,
        factor_id="mfa_" + "1" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_compliance",),
        permissions=(
            "platform.compliance.read",
            "platform.compliance.manage",
            "platform.compliance.exception",
        ),
        expires_at=expires,
    )
    approver = PlatformContext(
        platform_session_id="pts_" + "2" * 32,
        operator_id="pto_" + "2" * 32,
        account_id=SECOND_ACCOUNT_ID,
        global_session_id="gss_" + "2" * 32,
        factor_id="mfa_" + "2" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_compliance",),
        permissions=requester.permissions,
        expires_at=expires,
    )
    platform_state = {"actor": requester, "approver": approver}
    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda *_args, **_kwargs: platform_state["actor"],
    )
    monkeypatch.setattr(
        service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: workspace_actor,
    )
    monkeypatch.setattr(
        routes,
        "authenticate_machine_request",
        lambda *_args, **_kwargs: machine,
    )
    return config, workspace_actor, machine, platform_state


def _app(config: AppConfig):
    app = create_service_app(
        "control-plane-api",
        "PRD-135 Compliance Test",
        extra_cors_allow_headers=("Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(machine_router)
    app.include_router(router)
    return app


def _platform_headers(*, write: bool = False, key: str | None = None) -> dict[str, str]:
    headers = {
        "Cookie": (
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; {PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
        )
    }
    if write:
        headers.update(
            {
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-site",
                "X-CSRF-Token": PLATFORM_CSRF,
            }
        )
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _workspace_headers() -> dict[str, str]:
    return {"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"}


def _workspace_write_headers(key: str) -> dict[str, str]:
    return {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": WORKSPACE_CSRF,
        "Idempotency-Key": key,
    }


def _basic(client_id: str, client_secret: str) -> str:
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode("ascii")).decode("ascii")
    return f"Basic {encoded}"


def _evidence_payload(
    workspace_id: str,
    control_id: str,
    evidence_kind: str,
    *,
    observed_state: str,
    failed_count: int,
    key: str,
    nonce_seed: str,
    validity: timedelta = timedelta(hours=12),
    access_token: str = MACHINE_TOKEN,
) -> dict[str, object]:
    now = datetime.now(UTC).replace(microsecond=0)
    unsigned: dict[str, object] = {
        "audience": "metahub-control",
        "route": "/platform/compliance/evidence",
        "workspace_id": workspace_id,
        "compliance_control_id": control_id,
        "collector_version": "1.0.0",
        "evidence_kind": evidence_kind,
        "schema_version": 1,
        "artifact_sha256": hashlib.sha256(f"artifact-{nonce_seed}".encode()).hexdigest(),
        "classification": "confidential",
        "scope_state": "present",
        "observed_state": observed_state,
        "sample_count": 10,
        "failed_count": failed_count,
        "captured_at": now.isoformat().replace("+00:00", "Z"),
        "valid_until": (now + validity).isoformat().replace("+00:00", "Z"),
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "nonce": (nonce_seed * 32)[:32],
    }
    canonical = json.dumps(
        {"envelope": unsigned, "idempotency_key": key},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return {
        **unsigned,
        "signature_sha256": hmac.new(
            access_token.encode("ascii"),
            b"prd135/evidence-envelope/v1\0" + canonical,
            hashlib.sha256,
        ).hexdigest(),
    }


@pytest.mark.anyio
async def test_compliance_collector_real_oauth_and_machine_authentication(
    runtime: tuple[
        AppConfig, WorkspaceSessionContext, MachineAuthContext, dict[str, PlatformContext]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, actor, _machine, _platform = runtime
    machine_actor = replace(
        actor,
        membership_role="security_admin",
        legacy_role="security_admin",
        permission_codes=tuple(
            sorted(
                set(actor.permission_codes) | {"machine_identity.manage", "machine_identity.read"}
            )
        ),
    )
    monkeypatch.setattr(
        machine_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: machine_actor,
    )
    monkeypatch.setattr(
        machine_service,
        "workspace_actor_permissions_in_transaction",
        lambda *_args, **_kwargs: machine_actor,
    )
    monkeypatch.setattr(
        routes, "authenticate_machine_request", machine_service.authenticate_machine_request
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app(config)), base_url="http://test"
    ) as client:
        created = await client.post(
            "/service-accounts",
            headers=_workspace_write_headers("prd135-real-collector-create-0001"),
            json={
                "name": "compliance-chain",
                "display_name": "Compliance chain",
                "scope_template": "compliance_collector",
                "scopes": ["compliance.collect"],
                "token_ttl_seconds": 300,
                "ip_allowlist": [],
            },
        )
        assert created.status_code == 201, created.text
        service_account = created.json()["service_account"]
        credential = await client.post(
            "/oauth/clients",
            headers=_workspace_write_headers("prd135-real-oauth-client-0001"),
            json={"service_account_id": service_account["service_account_id"]},
        )
        assert credential.status_code == 201, credential.text
        oauth = credential.json()
        token = await client.post(
            "/oauth/token",
            headers={
                "Authorization": _basic(oauth["oauth_client"]["client_id"], oauth["client_secret"]),
                "Idempotency-Key": "prd135-real-token-issue-0001",
            },
            data={
                "grant_type": "client_credentials",
                "audience": "metahub-control",
                "scope": "compliance.collect",
            },
        )
        assert token.status_code == 200, token.text
        access_token = token.json()["access_token"]
        evidence_key = "prd135-real-evidence-submit-0001"
        evidence = await client.post(
            "/platform/compliance/evidence",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Idempotency-Key": evidence_key,
            },
            json=_evidence_payload(
                actor.workspace_id,
                CONTROL_ACCESS,
                "access_review",
                observed_state="effective",
                failed_count=0,
                key=evidence_key,
                nonce_seed="real-auth",
                access_token=access_token,
            ),
        )
        assert evidence.status_code == 201, evidence.text
        assert (
            evidence.json()["evidence"]["collector_service_account_id"]
            == service_account["service_account_id"]
        )


@pytest.mark.anyio
async def test_trusted_evidence_reports_replay_and_owner_boundaries(
    runtime: tuple[
        AppConfig, WorkspaceSessionContext, MachineAuthContext, dict[str, PlatformContext]
    ],
) -> None:
    config, actor, _machine, _platform = runtime
    app = _app(config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        catalog = await client.get("/platform/compliance/controls", headers=_platform_headers())
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["certification_claimed"] is False
        assert len(catalog.json()["controls"]) == 3

        key = "prd135-evidence-pass-0001"
        payload = _evidence_payload(
            actor.workspace_id,
            CONTROL_ACCESS,
            "access_review",
            observed_state="effective",
            failed_count=0,
            key=key,
            nonce_seed="pass",
        )
        evidence_headers = {"Authorization": f"Bearer {MACHINE_TOKEN}", "Idempotency-Key": key}
        created = await client.post(
            "/platform/compliance/evidence", headers=evidence_headers, json=payload
        )
        assert created.status_code == 201, created.text
        assert created.json()["evidence"]["outcome"] == "passed"
        assert created.json()["remediation"] is None
        replay = await client.post(
            "/platform/compliance/evidence", headers=evidence_headers, json=payload
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert (
            replay.json()["evidence"]["compliance_evidence_id"]
            == created.json()["evidence"]["compliance_evidence_id"]
        )

        projection = await client.get(
            f"/platform/compliance/controls?workspace_id={actor.workspace_id}",
            headers=_platform_headers(),
        )
        access = next(
            item
            for item in projection.json()["controls"]
            if item["control"]["compliance_control_id"] == CONTROL_ACCESS
        )
        assert access["report_outcome"] == "implemented"
        assert (
            "certified"
            not in " ".join(
                item["control"]["title"] for item in projection.json()["controls"]
            ).lower()
        )

        reports = await client.get(
            f"/workspaces/{actor.workspace_id}/compliance-reports",
            headers=_workspace_headers(),
        )
        assert reports.status_code == 200, reports.text
        assert reports.json()["current"]["implemented_count"] == 1
        assert reports.json()["certification_claimed"] is False
        cross_owner = await client.get(
            f"/workspaces/wsp_{'f' * 32}/compliance-reports",
            headers=_workspace_headers(),
        )
        assert cross_owner.status_code == 404

        arbitrary_url = {**payload, "artifact_url": "https://example.invalid/evidence"}
        rejected = await client.post(
            "/platform/compliance/evidence",
            headers={
                "Authorization": f"Bearer {MACHINE_TOKEN}",
                "Idempotency-Key": "prd135-evidence-extra-0001",
            },
            json=arbitrary_url,
        )
        assert rejected.status_code == 422
        cookie_conflict = await client.post(
            "/platform/compliance/evidence",
            headers={**evidence_headers, "Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
            json=payload,
        )
        assert cookie_conflict.status_code == 401


@pytest.mark.anyio
async def test_failed_control_two_person_exception_and_scheduler(
    runtime: tuple[
        AppConfig, WorkspaceSessionContext, MachineAuthContext, dict[str, PlatformContext]
    ],
) -> None:
    config, actor, _machine, platform = runtime
    app = _app(config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        key = "prd135-evidence-fail-0001"
        failed_payload = _evidence_payload(
            actor.workspace_id,
            CONTROL_ENCRYPTION,
            "encryption_posture",
            observed_state="ineffective",
            failed_count=2,
            key=key,
            nonce_seed="fail",
        )
        failed = await client.post(
            "/platform/compliance/evidence",
            headers={"Authorization": f"Bearer {MACHINE_TOKEN}", "Idempotency-Key": key},
            json=failed_payload,
        )
        assert failed.status_code == 201, failed.text
        assert failed.json()["evidence"]["outcome"] == "failed"
        assert failed.json()["remediation"]["status"] == "open"

        now = datetime.now(UTC).replace(microsecond=0)
        requested = await client.post(
            "/platform/compliance/exceptions",
            headers=_platform_headers(write=True, key="prd135-exception-request-0001"),
            json={
                "operation": "request",
                "workspace_id": actor.workspace_id,
                "compliance_control_id": CONTROL_ENCRYPTION,
                "evidence_generation": failed.json()["evidence"]["evidence_generation"],
                "reason_code": "planned_remediation",
                "compensating_controls": ["access_restriction", "enhanced_monitoring"],
                "starts_at": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                "confirmed": True,
            },
        )
        assert requested.status_code == 200, requested.text
        exception = requested.json()["exception"]
        assert exception["status"] == "pending"
        approval_body = {
            "operation": "approve",
            "compliance_exception_id": exception["compliance_exception_id"],
            "review_generation": exception["review_generation"],
            "confirmed": True,
        }
        self_approval = await client.post(
            "/platform/compliance/exceptions",
            headers=_platform_headers(write=True, key="prd135-exception-self-0001"),
            json=approval_body,
        )
        assert self_approval.status_code == 403
        assert self_approval.json()["code"] == "compliance.self_approval_denied"

        platform["actor"] = platform["approver"]
        approved = await client.post(
            "/platform/compliance/exceptions",
            headers=_platform_headers(write=True, key="prd135-exception-approve-0001"),
            json=approval_body,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["exception"]["status"] == "active"
        projection = await client.get(
            f"/platform/compliance/controls?workspace_id={actor.workspace_id}",
            headers=_platform_headers(),
        )
        encryption = next(
            item
            for item in projection.json()["controls"]
            if item["control"]["compliance_control_id"] == CONTROL_ENCRYPTION
        )
        assert encryption["report_outcome"] == "exception_active"

        expiring_key = "prd135-evidence-expiring-0001"
        expiring_payload = _evidence_payload(
            actor.workspace_id,
            CONTROL_PRIVACY,
            "privacy_request_process",
            observed_state="effective",
            failed_count=0,
            key=expiring_key,
            nonce_seed="soon",
            validity=timedelta(hours=1),
        )
        expiring = await client.post(
            "/platform/compliance/evidence",
            headers={
                "Authorization": f"Bearer {MACHINE_TOKEN}",
                "Idempotency-Key": expiring_key,
            },
            json=expiring_payload,
        )
        assert expiring.status_code == 201, expiring.text

    assert service.process_compliance_once(config, worker_id="prd135-test", limit=50) > 0
    with closing(connect_database(config.database_url)) as connection:
        event_types = {
            str(row[0])
            for row in connection.execute("SELECT event_type FROM compliance_events").fetchall()
        }
        assert {
            "compliance.control_failed",
            "compliance.exception_approved",
            "compliance.evidence_expiring",
        } <= event_types
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM compliance_event_outbox WHERE status='delivered'"
            ).fetchone()[0]
            >= 3
        )
        scheduler = connection.execute(
            "SELECT lease_owner_id,fencing_generation FROM compliance_scheduler_state"
        ).fetchone()
        assert scheduler["lease_owner_id"] is None
        assert int(scheduler["fencing_generation"]) >= 1
