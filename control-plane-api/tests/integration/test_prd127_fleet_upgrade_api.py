"""PRD-127 release, rollout, Agent command, and rollback API smoke flow."""

from __future__ import annotations

import hashlib
import secrets
import shutil
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.edge_management import routes as edge_routes
from metahub_control.edge_management import service as edge_service
from metahub_control.edge_management import upgrade_service
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
DEPLOYMENT_ID = "edp_12700000000000000000000000000100"
IDENTITY_ID = "edi_12700000000000000000000000000100"
CERTIFICATE_ID = "edc_12700000000000000000000000000100"
SESSION_ID = "eds_12700000000000000000000000000100"
AGENT_TOKEN = "egs_" + "e" * 43
FINGERPRINT = "f" * 64
INSTALLER_RELEASE_ID = "irl_12400000000000000000000000000000"
_TEST_PASSWORD = f"Prd127!Aa9{secrets.token_urlsafe(18)}"


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
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        provider_key_encryption_secret=secrets.token_urlsafe(32),
        edge_snapshot_signing_secret=secrets.token_urlsafe(32),
        edge_snapshot_signing_version=1,
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd127-fleet-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0156"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path
) -> tuple[AppConfig, WorkspaceSessionContext, dict[str, PlatformContext]]:
    database = tmp_path / "prd127.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    actor = _owner_actor(config)
    contexts = _insert_platform_operators(config)
    _insert_deployment_facts(config, actor)
    return config, actor, contexts


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
        "edge.rollback.manage",
        "edge.upgrade.manage",
        "edge.upgrade.read",
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


def _platform_context(marker: str, role: str, permissions: tuple[str, ...]) -> PlatformContext:
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


def _insert_platform_operators(config: AppConfig) -> dict[str, PlatformContext]:
    release_permissions = (
        "platform.edge_release.approve",
        "platform.edge_release.manage",
        "platform.edge_release.read",
        "platform.edge_rollout.read",
    )
    contexts = {
        "submitter": _platform_context("a", "release_manager", release_permissions),
        "approver": _platform_context("b", "release_manager", release_permissions),
        "ops": _platform_context(
            "c",
            "platform_ops",
            (
                "platform.edge_release.read",
                "platform.edge_rollout.manage",
                "platform.edge_rollout.read",
            ),
        ),
    }
    with closing(connect_database(config.database_url)) as connection:
        source = connection.execute(
            "SELECT * FROM global_accounts ORDER BY account_id LIMIT 1"
        ).fetchone()
        assert source is not None
        for name, context in contexts.items():
            values = dict(source)
            values.update(
                {
                    "account_id": context.account_id,
                    "email_normalized": f"prd127-{name}@example.invalid",
                    "phone_e164": None,
                    "display_name": f"PRD-127 {name}",
                    "status": "active",
                    "login_enabled": 1,
                    "failed_login_count": 0,
                    "locked_until": None,
                    "last_login_at": None,
                }
            )
            columns = tuple(values)
            connection.execute(
                f"INSERT INTO global_accounts({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            connection.execute(
                """
                INSERT INTO platform_operators(
                  operator_id,account_id,status,revision,created_by_operator_id,
                  created_at,updated_at)
                VALUES(?,?,'active',1,NULL,'2026-08-28T00:00:00.000Z',
                  '2026-08-28T00:00:00.000Z')
                """,
                (context.operator_id, context.account_id),
            )
            connection.execute(
                """
                INSERT INTO platform_operator_role_bindings(
                  operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                VALUES(?,?,'active',NULL,'2026-08-28T00:00:00.000Z',NULL)
                """,
                (context.operator_id, context.roles[0]),
            )
        connection.commit()
    return contexts


def _insert_deployment_facts(config: AppConfig, actor: WorkspaceSessionContext) -> None:
    now = datetime.now(UTC)
    now_text = _utc_text(now)
    before = _utc_text(now - timedelta(minutes=2))
    completed = _utc_text(now - timedelta(minutes=1))
    expires = _utc_text(now + timedelta(days=30))
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
              status,revision,revocation_generation,agent_version,health_code,
              last_connected_at,certificate_expires_at,created_by_account_id,
              revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'PRD-127 gateway','cn-test','connected','active',1,1,
              '1.0.0','healthy',?,?,?,NULL,NULL,?,?,NULL)
            """,
            (
                DEPLOYMENT_ID,
                actor.workspace_id,
                actor.tenant_id,
                now_text,
                expires,
                actor.account_id,
                before,
                before,
            ),
        )
        public_key_sha256 = _digest("prd127-public-key")
        connection.execute(
            """
            INSERT INTO edge_device_identities(
              edge_device_identity_id,workspace_id,tenant_id,edge_deployment_id,
              public_key_sha256,hardware_proof_sha256,capability_codes_json,
              agent_version,generation,status,created_at,updated_at,revoked_at)
            VALUES(?,?,?,?,?,NULL,'["upgrade"]','1.0.0',1,'active',?,?,NULL)
            """,
            (
                IDENTITY_ID,
                actor.workspace_id,
                actor.tenant_id,
                DEPLOYMENT_ID,
                public_key_sha256,
                before,
                before,
            ),
        )
        connection.execute(
            """
            INSERT INTO edge_device_certificates(
              edge_device_certificate_id,workspace_id,tenant_id,edge_deployment_id,
              edge_device_identity_id,prior_certificate_id,serial_hex,certificate_sha256,
              public_key_sha256,csr_sha256,certificate_pem,issuer_key_id,generation,status,
              not_before,not_after,drain_until,created_at,updated_at,revoked_at)
            VALUES(?,?,?,?,?,NULL,'12700',?,?,?,?, 'edge-device-ca-v1',1,'active',
              ?,?,NULL,?,?,NULL)
            """,
            (
                CERTIFICATE_ID,
                actor.workspace_id,
                actor.tenant_id,
                DEPLOYMENT_ID,
                IDENTITY_ID,
                _digest("prd127-certificate"),
                public_key_sha256,
                _digest("prd127-csr"),
                "-----BEGIN CERTIFICATE-----\n"
                + "A" * 256
                + "\n-----END CERTIFICATE-----",
                before,
                expires,
                before,
                before,
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
                actor.workspace_id,
                actor.tenant_id,
                DEPLOYMENT_ID,
                IDENTITY_ID,
                CERTIFICATE_ID,
                edge_service._secret_hmac(
                    config, "edge-session-token-v1", AGENT_TOKEN
                ),
                FINGERPRINT,
                _digest("prd127-handshake"),
                expires,
                before,
            ),
        )
        run_id = "dvr_12700000000000000000000000000100"
        connection.execute(
            """
            INSERT INTO edge_validation_runs(
              validation_run_id,workspace_id,tenant_id,edge_deployment_id,
              source_validation_run_id,attempt,plan_version,deployment_revision,
              installer_release_id,desired_generation,applied_generation,lkg_generation,
              input_contract_sha256,requested_check_codes_json,status,outcome,duration_ms,
              requested_by_account_id,idempotency_sha256,request_sha256,started_at,
              completed_at,created_at,updated_at)
            VALUES(?,?,?,?,NULL,1,'edge-delivery-v1',1,?,0,0,0,?,
              '["backup_readiness","capacity_readiness"]',
              'requested',NULL,NULL,?,?,?,NULL,NULL,?,?)
            """,
            (
                run_id,
                actor.workspace_id,
                actor.tenant_id,
                DEPLOYMENT_ID,
                INSTALLER_RELEASE_ID,
                "1" * 64,
                actor.account_id,
                "2" * 64,
                "3" * 64,
                before,
                before,
            ),
        )
        connection.execute(
            """
            INSERT INTO edge_validation_checks(
              validation_check_id,workspace_id,tenant_id,edge_deployment_id,
              validation_run_id,check_code,outcome,reason_code,recovery_action_code,
              evidence_sha256,duration_ms,created_at)
            VALUES('dvc_12700000000000000000000000000100',?,?,?,?,'backup_readiness',
              'passed','check_passed',NULL,?,1,?)
            """,
            (
                actor.workspace_id,
                actor.tenant_id,
                DEPLOYMENT_ID,
                run_id,
                "4" * 64,
                before,
            ),
        )
        connection.execute(
            """
            UPDATE edge_validation_runs SET status='completed',outcome='passed',
              duration_ms=1,started_at=?,completed_at=?,updated_at=?
            WHERE validation_run_id=?
            """,
            (before, completed, completed, run_id),
        )
        snapshot_id = "ehs_12700000000000000000000000000100"
        connection.execute(
            """
            INSERT INTO edge_health_snapshots(
              edge_health_snapshot_id,workspace_id,tenant_id,edge_deployment_id,generation,
              source_code,source_revision,score_model_version,health_score,health_code,
              certificate_signal,version_signal,capacity_signal,config_signal,
              telemetry_signal,validation_signal,desired_generation,applied_generation,
              lkg_generation,telemetry_lag_seconds,certificate_expires_at,agent_version,
              snapshot_sha256,observed_at,created_at)
            VALUES(?,?,?,?,1,'runtime',1,'edge-health-v1',100,'healthy','healthy',
              'current','healthy','converged','current','passed',0,0,0,0,?,
              '1.0.0',?,?,?)
            """,
            (
                snapshot_id,
                actor.workspace_id,
                actor.tenant_id,
                DEPLOYMENT_ID,
                expires,
                "5" * 64,
                now_text,
                now_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO edge_health_heads(
              edge_deployment_id,workspace_id,tenant_id,generation,
              edge_health_snapshot_id,snapshot_sha256,revision,updated_at)
            VALUES(?,?,?,1,?,?,1,?)
            """,
            (
                DEPLOYMENT_ID,
                actor.workspace_id,
                actor.tenant_id,
                snapshot_id,
                "5" * 64,
                now_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO edge_fleet_deployment_index(
              edge_deployment_id,workspace_id,tenant_id,region_code,mode,status,
              health_code,health_score,alert_code,maintenance_mode,agent_version,
              desired_generation,applied_generation,lkg_generation,telemetry_lag_seconds,
              certificate_signal,source_health_generation,index_revision,updated_at)
            VALUES(?,?,?,'cn-test','connected','active','healthy',100,'none',0,
              '1.0.0',0,0,0,0,'healthy',1,1,?)
            """,
            (DEPLOYMENT_ID, actor.workspace_id, actor.tenant_id, now_text),
        )
        connection.commit()


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    actor: WorkspaceSessionContext,
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
        lambda *_args, **_kwargs: actor,
    )
    monkeypatch.setattr(
        upgrade_service,
        "consume_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        upgrade_service,
        "consume_global_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(edge_routes, "verified_mtls_fingerprint", lambda _request: FINGERPRINT)
    app = create_service_app(
        "control-plane-api",
        "PRD-127 Test",
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
    return app


def _workspace_headers(*, key: str | None = None, step_up: str | None = None):
    headers = {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": WORKSPACE_CSRF,
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    if step_up is not None:
        headers["X-Step-Up-Grant"] = step_up
    return headers


def _platform_headers(*, key: str | None = None, step_up: str | None = None):
    headers = {
        "Cookie": (
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; "
            f"{PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": PLATFORM_CSRF,
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    if step_up is not None:
        headers["X-Step-Up-Grant"] = step_up
    return headers


def _agent_headers() -> dict[str, str]:
    return {"X-Edge-Session": AGENT_TOKEN}


@pytest.mark.anyio
async def test_release_rollout_agent_report_pause_and_rollback_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext, dict[str, PlatformContext]],
) -> None:
    config, actor, contexts = runtime
    platform_holder = {"context": contexts["submitter"]}
    app = _app(monkeypatch, config, actor, platform_holder)
    now = datetime.now(UTC)
    minute = now.hour * 60 + now.minute
    start_minute = (minute - 30) % 1440

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        initial = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/upgrade",
            headers=_workspace_headers(),
        )
        assert initial.status_code == 200, initial.text
        assert initial.json()["maintenance"] is None
        assert initial.json()["head"]["current_version"] == "1.0.0"

        maintenance = await client.patch(
            f"/edge/deployments/{DEPLOYMENT_ID}/maintenance-window",
            headers=_workspace_headers(key="prd127-maintenance-create-0001"),
            json={
                "release_channel": "stable",
                "timezone_name": "UTC",
                "weekdays": [0, 1, 2, 3, 4, 5, 6],
                "start_minute": start_minute,
                "duration_minutes": 120,
                "freeze_starts_at": None,
                "freeze_ends_at": None,
                "status": "active",
                "expected_revision": 0,
                "reason_code": "customer_window",
            },
        )
        assert maintenance.status_code == 200, maintenance.text
        assert maintenance.json()["maintenance"]["policy_generation"] == 1

        submitted = await client.post(
            "/platform/releases",
            headers=_platform_headers(key="prd127-release-submit-0001"),
            json={
                "action": "submit",
                "installer_release_id": INSTALLER_RELEASE_ID,
                "release_channel": "stable",
                "schema_from": 119,
                "schema_to": 119,
                "down_migration_supported": True,
                "compatible_from_versions": ["1.0.0"],
                "reason_code": "scheduled_release",
            },
        )
        assert submitted.status_code == 201, submitted.text
        release_id = submitted.json()["release"]["release"]["manifest"]["release_id"]

        self_approval = await client.post(
            "/platform/releases",
            headers=_platform_headers(
                key="prd127-release-self-approve-0001", step_up="s" * 43
            ),
            json={
                "action": "approve",
                "release_id": release_id,
                "expected_revision": 1,
                "reason_code": "release_review",
            },
        )
        assert self_approval.status_code == 403
        assert self_approval.json()["code"] == "fleet.release_self_approval_denied"

        platform_holder["context"] = contexts["approver"]
        approved = await client.post(
            "/platform/releases",
            headers=_platform_headers(
                key="prd127-release-approve-0001", step_up="s" * 43
            ),
            json={
                "action": "approve",
                "release_id": release_id,
                "expected_revision": 1,
                "reason_code": "release_review",
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["release"]["status"] == "released"

        platform_holder["context"] = contexts["ops"]
        compatibility = await client.get(
            f"/fleet/compatibility?release_id={release_id}&deployment_id={DEPLOYMENT_ID}",
            headers=_platform_headers(),
        )
        assert compatibility.status_code == 200, compatibility.text
        assert compatibility.json()["items"][0]["compatible"] is True

        created = await client.post(
            f"/platform/releases/{release_id}/rollouts",
            headers=_platform_headers(key="prd127-rollout-create-0001"),
            json={
                "deployment_ids": [DEPLOYMENT_ID],
                "canary_basis_points": 100,
                "observation_seconds": 1800,
                "batch_size": 10,
                "failure_threshold_basis_points": 1000,
                "reason_code": "scheduled_rollout",
            },
        )
        assert created.status_code == 201, created.text
        rollout_id = created.json()["rollout"]["rollout_id"]
        assert created.json()["rollout"]["batches"][0]["batch_kind"] == "canary"

        with closing(connect_database(config.database_url)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rollout = upgrade_service._rollout_row(connection, rollout_id)
            assert (
                upgrade_service._advance_rollout(
                    connection, config, rollout, now=datetime.now(UTC)
                )
                == "fleet.rollout_started"
            )
            connection.commit()

        pending = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/upgrades/pending",
            headers=_agent_headers(),
        )
        assert pending.status_code == 200, pending.text
        command_body = pending.json()
        command = command_body["command"]["command"]
        command_id = command["upgrade_command_id"]
        lease_nonce = "lease-" + "7" * 40

        leased = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/upgrades/{command_id}",
            headers=_agent_headers(),
            json={"action": "lease", "lease_nonce": lease_nonce, "expected_revision": 1},
        )
        assert leased.status_code == 200, leased.text

        with closing(connect_database(config.database_url)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            leased_row = connection.execute(
                "SELECT * FROM edge_upgrade_commands WHERE upgrade_command_id=?",
                (command_id,),
            ).fetchone()
            expired_at = datetime.now(UTC) - timedelta(seconds=1)
            connection.execute(
                """
                UPDATE edge_upgrade_commands SET lease_expires_at=?,revision=revision+1,
                  updated_at=? WHERE upgrade_command_id=? AND revision=?
                """,
                (
                    _utc_text(expired_at),
                    upgrade_service._changed_stamp(datetime.now(UTC), leased_row["updated_at"]),
                    command_id,
                    leased_row["revision"],
                ),
            )
            connection.commit()

        recovered = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/upgrades/pending",
            headers=_agent_headers(),
        )
        assert recovered.status_code == 200, recovered.text
        recovered_revision = recovered.json()["command_revision"]
        assert recovered_revision == 4
        lease_nonce = "lease-recovered-" + "8" * 32
        leased = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/upgrades/{command_id}",
            headers=_agent_headers(),
            json={
                "action": "lease",
                "lease_nonce": lease_nonce,
                "expected_revision": recovered_revision,
            },
        )
        assert leased.status_code == 200, leased.text
        started = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/upgrades/{command_id}",
            headers=_agent_headers(),
            json={
                "action": "start",
                "lease_nonce": lease_nonce,
                "expected_revision": leased.json()["command_revision"],
            },
        )
        assert started.status_code == 200, started.text

        completed_at = datetime.now(UTC)
        attempt_nonce = upgrade_service._attempt_nonce_sha256(
            DEPLOYMENT_ID, command_id, lease_nonce
        )
        report = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/upgrades/{command_id}/report",
            headers=_agent_headers(),
            json={
                "attempt_nonce": attempt_nonce,
                "report": {
                    "schema_version": "metahub.edge-upgrade-report/v1",
                    "upgrade_command_id": command_id,
                    "deployment_id": DEPLOYMENT_ID,
                    "command_generation": command["command_generation"],
                    "command_kind": "upgrade",
                    "outcome": "succeeded",
                    "reason_code": "edge_upgrade.applied",
                    "from_version": command["expected_current_version"],
                    "target_version": command["target_version"],
                    "applied_version": command["target_version"],
                    "lkg_version": command["expected_current_version"],
                    "drain_duration_ms": 1,
                    "stage_duration_ms": 1,
                    "migration_duration_ms": 1,
                    "health_duration_ms": 1,
                    "total_duration_ms": 4,
                    "release_manifest_sha256": command["release"]["manifest_sha256"],
                    "command_sha256": command_body["command"]["command_sha256"],
                    "completed_at": _utc_text(completed_at),
                },
            },
        )
        assert report.status_code == 201, report.text
        assert report.json()["outcome"] == "succeeded"

        with closing(connect_database(config.database_url)) as connection:
            rollout_revision = connection.execute(
                "SELECT revision FROM edge_fleet_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()[0]
        paused = await client.post(
            f"/fleet/rollouts/{rollout_id}/pause",
            headers=_platform_headers(key="prd127-rollout-pause-0001"),
            json={
                "action": "pause",
                "expected_revision": rollout_revision,
                "reason_code": "operator_pause",
            },
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["rollout"]["status"] == "paused"

        current = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/upgrade",
            headers=_workspace_headers(),
        )
        rollback = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/rollback",
            headers=_workspace_headers(
                key="prd127-rollback-unavailable-0001", step_up="s" * 43
            ),
            json={
                "expected_head_revision": current.json()["head"]["revision"],
                "reason_code": "customer_rollback",
            },
        )
        assert rollback.status_code == 409, rollback.text
        assert rollback.json()["code"] == "edge_upgrade.rollback_unavailable"

    with closing(connect_database(config.database_url)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_upgrade_receipts WHERE outcome='succeeded'"
        ).fetchone()[0] == 1
        assert {
            "fleet.rollout_started",
            "fleet.rollout_paused",
        } <= {
            str(row[0])
            for row in connection.execute(
                "SELECT event_type FROM edge_fleet_upgrade_events"
            ).fetchall()
        }
