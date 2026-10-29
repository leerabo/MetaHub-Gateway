"""PRD-129 backup summary, dual approval, and isolated drill API smoke flow."""

from __future__ import annotations

import hashlib
import secrets
import shutil
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.edge_management import backup_service
from metahub_control.edge_management import routes as edge_routes
from metahub_control.edge_management import service as edge_service
from metahub_control.edge_management.routes import router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
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
PLATFORM_CSRF = "q" * 43
STEP_UP = "u" * 43
DEPLOYMENT_ID = "edp_" + "1" * 32
IDENTITY_ID = "edi_" + "2" * 32
CERTIFICATE_ID = "edc_" + "3" * 32
SESSION_ID = "eds_" + "4" * 32
BACKUP_ID = "ebk_" + "5" * 32
AGENT_TOKEN = "egs_" + "e" * 43
FINGERPRINT = "f" * 64
_TEST_PASSWORD = f"Prd129!Aa9{secrets.token_urlsafe(18)}"


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
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd129-backup-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0156"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path
) -> tuple[
    AppConfig,
    WorkspaceSessionContext,
    WorkspaceSessionContext,
    dict[str, PlatformContext],
]:
    database = tmp_path / "prd129.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    owner = _owner_actor(config)
    approver = _insert_approver(config, owner)
    _insert_deployment_and_agent(config, owner)
    platforms = {
        "ops": _platform_context(
            "a", "platform_ops", ("platform.edge_backup.read", "platform.edge_drill.manage")
        ),
        "support": _platform_context("b", "platform_support", ("platform.support.request",)),
    }
    return config, owner, approver, platforms


def _owner_actor(config: AppConfig) -> WorkspaceSessionContext:
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            """
            SELECT membership.workspace_membership_id,membership.workspace_id,
              membership.tenant_id,membership.account_id,membership.legacy_user_id,
              tenant.slug,tenant.name,user.email,user.display_name
            FROM workspace_memberships membership
            JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
            JOIN users user ON user.user_id=membership.legacy_user_id
              AND user.tenant_id=membership.tenant_id
            WHERE membership.role_code='owner' AND membership.status='active'
            ORDER BY membership.workspace_id LIMIT 1
            """
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
        "edge.backup.create",
        "edge.backup.read",
        "edge.drill.manage",
        "edge.restore.approve",
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


def _insert_approver(
    config: AppConfig, owner: WorkspaceSessionContext
) -> WorkspaceSessionContext:
    account_id = "gac_" + "9" * 32
    with closing(connect_database(config.database_url)) as connection:
        source = connection.execute(
            "SELECT * FROM global_accounts WHERE account_id=?", (owner.account_id,)
        ).fetchone()
        assert source is not None
        values = dict(source)
        values.update(
            {
                "account_id": account_id,
                "email_normalized": "prd129-approver@example.invalid",
                "phone_e164": None,
                "display_name": "PRD-129 approver",
                "last_login_at": None,
            }
        )
        columns = tuple(values)
        connection.execute(
            f"INSERT INTO global_accounts({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )
        connection.commit()
    return replace(
        owner,
        workspace_session_id="wss_" + "9" * 32,
        global_session_id="gss_" + "9" * 32,
        account_id=account_id,
        membership_id="wsm_" + "9" * 32,
        membership_role="security_admin",
        permission_codes=("edge.backup.read", "edge.drill.manage", "edge.restore.approve"),
        token="a" * 43,
    )


def _insert_deployment_and_agent(
    config: AppConfig, owner: WorkspaceSessionContext
) -> None:
    now = datetime.now(UTC)
    now_text = _time(now)
    expires = _time(now + timedelta(days=30))
    public_key_sha = _sha("prd129-public-key")
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
              status,revision,revocation_generation,agent_version,health_code,
              last_connected_at,certificate_expires_at,created_by_account_id,
              revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'PRD-129 backup','cn-test','connected','active',1,1,'1.0.0',
              'healthy',?,?,?,NULL,NULL,?,?,NULL)
            """,
            (
                DEPLOYMENT_ID,
                owner.workspace_id,
                owner.tenant_id,
                now_text,
                expires,
                owner.account_id,
                now_text,
                now_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO edge_device_identities(
              edge_device_identity_id,workspace_id,tenant_id,edge_deployment_id,
              public_key_sha256,hardware_proof_sha256,capability_codes_json,
              agent_version,generation,status,created_at,updated_at,revoked_at)
            VALUES(?,?,?,?,?,NULL,'["backup"]','1.0.0',1,'active',?,?,NULL)
            """,
            (
                IDENTITY_ID,
                owner.workspace_id,
                owner.tenant_id,
                DEPLOYMENT_ID,
                public_key_sha,
                now_text,
                now_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO edge_device_certificates(
              edge_device_certificate_id,workspace_id,tenant_id,edge_deployment_id,
              edge_device_identity_id,prior_certificate_id,serial_hex,certificate_sha256,
              public_key_sha256,csr_sha256,certificate_pem,issuer_key_id,generation,status,
              not_before,not_after,drain_until,created_at,updated_at,revoked_at)
            VALUES(?,?,?,?,?,NULL,'1290',?,?,?,?,'edge-device-ca-v1',1,'active',
              ?,?,NULL,?,?,NULL)
            """,
            (
                CERTIFICATE_ID,
                owner.workspace_id,
                owner.tenant_id,
                DEPLOYMENT_ID,
                IDENTITY_ID,
                FINGERPRINT,
                public_key_sha,
                _sha("prd129-csr"),
                "-----BEGIN CERTIFICATE-----\n" + "A" * 256 + "\n-----END CERTIFICATE-----",
                now_text,
                expires,
                now_text,
                now_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO edge_sessions(
              edge_session_id,workspace_id,tenant_id,edge_deployment_id,
              edge_device_identity_id,edge_device_certificate_id,session_token_hmac,
              peer_binding_sha256,handshake_nonce_hmac,identity_generation,
              revocation_generation,status,expires_at,revoked_reason_code,created_at,revoked_at)
            VALUES(?,?,?,?,?,?,?,?,?,1,1,'active',?,NULL,?,NULL)
            """,
            (
                SESSION_ID,
                owner.workspace_id,
                owner.tenant_id,
                DEPLOYMENT_ID,
                IDENTITY_ID,
                CERTIFICATE_ID,
                edge_service._secret_hmac(config, "edge-session-token-v1", AGENT_TOKEN),
                FINGERPRINT,
                _sha("prd129-handshake"),
                expires,
                now_text,
            ),
        )
        connection.commit()


def _platform_context(
    marker: str, role: str, permissions: tuple[str, ...]
) -> PlatformContext:
    return PlatformContext(
        platform_session_id="pts_" + marker * 32,
        operator_id="pto_" + marker * 32,
        account_id="gac_" + marker * 32,
        global_session_id="gss_" + marker * 32,
        factor_id="mfa_" + marker * 32,
        factor_revision=1,
        operator_revision=1,
        roles=(role,),
        permissions=permissions,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    actor_holder: dict[str, WorkspaceSessionContext],
    platform_holder: dict[str, PlatformContext],
):
    monkeypatch.setattr(
        edge_routes,
        "authenticate_platform_session_context",
        lambda *_args, **_kwargs: platform_holder["context"],
    )
    monkeypatch.setattr(
        edge_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor_holder["context"],
    )
    monkeypatch.setattr(edge_routes, "verified_mtls_fingerprint", lambda _request: FINGERPRINT)
    consumed: list[tuple[str, str, str]] = []

    def consume(*_args, **kwargs):
        assert kwargs["grant"] == STEP_UP
        consumed.append(
            (kwargs["action_code"], kwargs["resource_type"], kwargs["resource_id"])
        )
        return "totp"

    monkeypatch.setattr(backup_service, "consume_step_up_grant_in_transaction", consume)
    app = create_service_app(
        "control-plane-api",
        "PRD-129 Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "X-CSRF-Token",
            "X-Edge-Session",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app, consumed


def _workspace_headers(*, key: str | None = None) -> dict[str, str]:
    headers = {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": WORKSPACE_CSRF,
        "X-Step-Up-Grant": STEP_UP,
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _platform_headers() -> dict[str, str]:
    return {
        "Cookie": (
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; "
            f"{PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": PLATFORM_CSRF,
    }


def _agent_headers() -> dict[str, str]:
    return {"X-Edge-Session": AGENT_TOKEN}


@pytest.mark.anyio
async def test_backup_report_dual_approval_drill_and_platform_summary(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[
        AppConfig,
        WorkspaceSessionContext,
        WorkspaceSessionContext,
        dict[str, PlatformContext],
    ],
) -> None:
    config, owner, approver, platforms = runtime
    actor_holder = {"context": owner}
    platform_holder = {"context": platforms["ops"]}
    app, consumed = _app(monkeypatch, config, actor_holder, platform_holder)
    completed = datetime.now(UTC) - timedelta(minutes=5)
    backup_payload = {
        "backup_id": BACKUP_ID,
        "source_generation": 11,
        "schema_head": "0121",
        "storage_kind": "customer_object_store",
        "status": "completed",
        "size_bucket": "100mb_1gb",
        "chunk_count": 9,
        "manifest_sha256": "6" * 64,
        "kms_key_id_sha256": "7" * 64,
        "check_outcome": "verified",
        "failure_code": None,
        "started_at": _time(completed - timedelta(minutes=2)),
        "completed_at": _time(completed),
        "retention_until": _time(completed + timedelta(days=30)),
        "attempt_nonce": "backup-" + "8" * 40,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reported = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/backups/reports",
            headers=_agent_headers(),
            json=backup_payload,
        )
        assert reported.status_code == 201, reported.text
        assert reported.json()["backup"]["manifest_sha256"] == "6" * 64
        assert not {"chunks", "envelope", "object_url", "path", "secret"}.intersection(
            reported.json()["backup"]
        )
        replay = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/backups/reports",
            headers=_agent_headers(),
            json=backup_payload,
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True

        status_response = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/backup-status",
            headers=_workspace_headers(),
        )
        assert status_response.status_code == 200, status_response.text
        assert status_response.json()["status"]["status"] == "current"

        requested = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/restore-drills",
            headers=_workspace_headers(key="prd129-drill-request-0001"),
            json={
                "action": "request",
                "source_backup_id": BACKUP_ID,
                "drill_id": None,
                "quarter_code": "2026-Q3",
                "rpo_objective_seconds": 86400,
                "rto_objective_seconds": 14400,
                "isolated_target_sha256": "9" * 64,
                "expected_revision": None,
                "reason_code": "quarterly_recovery_test",
            },
        )
        assert requested.status_code == 201, requested.text
        drill = requested.json()["drill"]
        assert drill["production_untouched"] is True
        assert consumed[-1] == (
            "edge.drill.request",
            "edge_deployment",
            DEPLOYMENT_ID,
        )

        self_approval = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/restore-drills",
            headers=_workspace_headers(key="prd129-drill-approve-self"),
            json={
                "action": "approve",
                "source_backup_id": None,
                "drill_id": drill["drill_id"],
                "quarter_code": None,
                "rpo_objective_seconds": None,
                "rto_objective_seconds": None,
                "isolated_target_sha256": None,
                "expected_revision": 1,
                "reason_code": "security_approval",
            },
        )
        assert self_approval.status_code == 409
        assert self_approval.json()["code"] == "edge_backup.approval_conflict"

        actor_holder["context"] = approver
        approved = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/restore-drills",
            headers=_workspace_headers(key="prd129-drill-approve-0001"),
            json={
                "action": "approve",
                "source_backup_id": None,
                "drill_id": drill["drill_id"],
                "quarter_code": None,
                "rpo_objective_seconds": None,
                "rto_objective_seconds": None,
                "isolated_target_sha256": None,
                "expected_revision": 1,
                "reason_code": "security_approval",
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["drill"]["status"] == "approved"
        assert consumed[-1] == (
            "edge.restore.approve",
            "private_dr_drill",
            drill["drill_id"],
        )

        checked = datetime.now(UTC)
        codes = sorted(backup_service._VERIFICATION_CODES)
        drill_report = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/restore-drills/{drill['drill_id']}/reports",
            headers=_agent_headers(),
            json={
                "restore_job_id": drill["restore_job_id"],
                "expected_revision": 2,
                "outcome": "succeeded",
                "actual_rpo_seconds": 600,
                "actual_rto_seconds": 900,
                "verifications": [
                    {
                        "verification_code": code,
                        "outcome": "passed",
                        "evidence_sha256": hashlib.sha256(code.encode()).hexdigest(),
                        "checked_at": _time(checked),
                    }
                    for code in codes
                ],
                "attempt_nonce": "drill-" + "a" * 40,
            },
        )
        assert drill_report.status_code == 200, drill_report.text
        assert drill_report.json()["drill"]["objective_outcome"] == "met"
        assert drill_report.json()["drill"]["status"] == "succeeded"

        listed = await client.get("/platform/edge/backups", headers=_platform_headers())
        assert listed.status_code == 200, listed.text
        assert listed.json()["statuses"][0]["deployment_id"] == DEPLOYMENT_ID
        assert "chunks" not in listed.text and "wrapped_data_key" not in listed.text

        platform_holder["context"] = platforms["support"]
        denied = await client.get("/platform/edge/backups", headers=_platform_headers())
        assert denied.status_code == 403

    with closing(connect_database(config.database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM edge_backup_reports").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_restore_verifications"
        ).fetchone()[0] == 9
        assert tuple(
            connection.execute(
                "SELECT production_untouched,objective_outcome FROM private_dr_drills"
            ).fetchone()
        ) == (1, "met")
        cloud_columns = {
            str(row[1])
            for table in (
                "edge_backup_reports",
                "edge_backup_statuses",
                "edge_restore_jobs",
                "edge_restore_verifications",
                "private_dr_drills",
            )
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        assert not {
            "chunk",
            "envelope",
            "object_url",
            "object_path",
            "secret",
            "wrapped_data_key",
        }.intersection(cloud_columns)
