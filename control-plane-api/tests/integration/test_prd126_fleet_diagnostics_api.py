"""PRD-126 Fleet projection and controlled diagnostic API state machine."""

from __future__ import annotations

import hashlib
import secrets
import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.edge_management import fleet_diagnostic_service as fleet_service
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
from metahub_shared.private_gateway import RuntimeVersionResult, diagnostic_result_sha256
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43
PLATFORM_CSRF = "q" * 43
DEPLOYMENTS = tuple("edp_" + f"{12600 + index:032x}" for index in range(3))
AGENT_TOKENS = {
    deployment_id: "egs_" + chr(ord("e") + index) * 43
    for index, deployment_id in enumerate(DEPLOYMENTS)
}
AGENT_IDENTITIES = {
    deployment_id: "edi_" + f"{12610 + index:032x}"
    for index, deployment_id in enumerate(DEPLOYMENTS)
}
AGENT_CERTIFICATES = {
    deployment_id: "edc_" + f"{12620 + index:032x}"
    for index, deployment_id in enumerate(DEPLOYMENTS)
}
AGENT_SESSIONS = {
    deployment_id: "eds_" + f"{12630 + index:032x}"
    for index, deployment_id in enumerate(DEPLOYMENTS)
}
PLATFORM_OPERATOR_ID = "pto_" + "a" * 32
PLATFORM_ACCOUNT_ID = "gac_" + "a" * 32
_TEST_PASSWORD = f"Prd126!Aa9{secrets.token_urlsafe(18)}"


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
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _insert_agent_chain(
    connection: sqlite3.Connection,
    *,
    config: AppConfig,
    actor: WorkspaceSessionContext,
    deployment_id: str,
    index: int,
    now: str,
    expires: str,
) -> None:
    identity_id = AGENT_IDENTITIES[deployment_id]
    certificate_id = AGENT_CERTIFICATES[deployment_id]
    session_id = AGENT_SESSIONS[deployment_id]
    public_key_sha256 = _digest(f"prd126-public-key-{deployment_id}")
    connection.execute(
        """
        INSERT INTO edge_device_identities(
          edge_device_identity_id,workspace_id,tenant_id,edge_deployment_id,
          public_key_sha256,hardware_proof_sha256,capability_codes_json,
          agent_version,generation,status,created_at,updated_at,revoked_at)
        VALUES(?,?,?,?,?,NULL,'["diagnostics"]','1.0.0',1,'active',?,?,NULL)
        """,
        (
            identity_id,
            actor.workspace_id,
            actor.tenant_id,
            deployment_id,
            public_key_sha256,
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO edge_device_certificates(
          edge_device_certificate_id,workspace_id,tenant_id,edge_deployment_id,
          edge_device_identity_id,prior_certificate_id,serial_hex,certificate_sha256,
          public_key_sha256,csr_sha256,certificate_pem,issuer_key_id,generation,status,
          not_before,not_after,drain_until,created_at,updated_at,revoked_at)
        VALUES(?,?,?,?,?,NULL,?,?,?,?,?,'edge-device-ca-v1',1,'active',
          ?,?,NULL,?,?,NULL)
        """,
        (
            certificate_id,
            actor.workspace_id,
            actor.tenant_id,
            deployment_id,
            identity_id,
            f"126{index:02x}",
            _digest(f"prd126-certificate-{deployment_id}"),
            public_key_sha256,
            _digest(f"prd126-csr-{deployment_id}"),
            "-----BEGIN CERTIFICATE-----\n"
            + "A" * 256
            + "\n-----END CERTIFICATE-----",
            now,
            expires,
            now,
            now,
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
            session_id,
            actor.workspace_id,
            actor.tenant_id,
            deployment_id,
            identity_id,
            certificate_id,
            edge_service._secret_hmac(
                config, "edge-session-token-v1", AGENT_TOKENS[deployment_id]
            ),
            "f" * 64,
            _digest(f"prd126-handshake-nonce-{deployment_id}"),
            expires,
            now,
        ),
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd126-fleet-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0122"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path
) -> tuple[AppConfig, WorkspaceSessionContext, PlatformContext]:
    database = tmp_path / "prd126.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    actor = _owner_actor(config)
    platform = _prepare_platform_operator(config)
    now = datetime.now(UTC)
    now_text = _utc_text(now)
    expires = _utc_text(now + timedelta(days=30))
    with closing(connect_database(config.database_url)) as connection:
        for index, deployment_id in enumerate(DEPLOYMENTS):
            connection.execute(
                """
                INSERT INTO edge_deployments(
                  edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
                  status,revision,revocation_generation,agent_version,health_code,
                  last_connected_at,certificate_expires_at,created_by_account_id,
                  revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
                VALUES(?,?,?,? ,?,'connected','active',1,1,'1.0.0','healthy',?,?,?,
                  NULL,NULL,?,?,NULL)
                """,
                (
                    deployment_id,
                    actor.workspace_id,
                    actor.tenant_id,
                    f"PRD-126 diagnostic {index}",
                    f"cn-test-{index}",
                    now_text,
                    expires,
                    actor.account_id,
                    now_text,
                    now_text,
                ),
            )
            _insert_agent_chain(
                connection,
                config=config,
                actor=actor,
                deployment_id=deployment_id,
                index=index,
                now=now_text,
                expires=expires,
            )
        connection.commit()
    return config, actor, platform


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
        "edge.health.read",
        "edge.diagnostic.read",
        "edge.diagnostic.approve",
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


def _prepare_platform_operator(config: AppConfig) -> PlatformContext:
    context = PlatformContext(
        platform_session_id="pts_" + "a" * 32,
        operator_id=PLATFORM_OPERATOR_ID,
        account_id=PLATFORM_ACCOUNT_ID,
        global_session_id="gss_" + "a" * 32,
        factor_id="mfa_" + "a" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_ops",),
        permissions=("platform.edge_fleet.manage", "platform.edge_fleet.read"),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with closing(connect_database(config.database_url)) as connection:
        source = connection.execute(
            "SELECT * FROM global_accounts ORDER BY account_id LIMIT 1"
        ).fetchone()
        assert source is not None
        values = dict(source)
        values.update(
            {
                "account_id": context.account_id,
                "email_normalized": "prd126-platform-ops@example.invalid",
                "phone_e164": None,
                "display_name": "PRD-126 platform ops",
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
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,NULL,'2026-08-27T00:00:00.000Z',
              '2026-08-27T00:00:00.000Z')
            """,
            (context.operator_id, context.account_id),
        )
        connection.execute(
            """
            INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
            VALUES(?,'platform_ops','active',NULL,'2026-08-27T00:00:00.000Z',NULL)
            """,
            (context.operator_id,),
        )
        connection.commit()
    return context


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    actor: WorkspaceSessionContext,
    platform: PlatformContext,
):
    monkeypatch.setattr(
        edge_routes,
        "authenticate_platform_session_context",
        lambda *_args, **_kwargs: platform,
    )
    monkeypatch.setattr(
        edge_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    monkeypatch.setattr(
        fleet_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    monkeypatch.setattr(
        fleet_service,
        "consume_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        fleet_service,
        "consume_global_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        edge_routes,
        "verified_mtls_fingerprint",
        lambda _request: "f" * 64,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-126 Test",
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


def _agent_headers(deployment_id: str) -> dict[str, str]:
    return {"X-Edge-Session": AGENT_TOKENS[deployment_id]}


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _authorize_diagnostic_command(
    client: AsyncClient,
    deployment_id: str,
    *,
    key_prefix: str,
    deadline_seconds: int = 60,
) -> tuple[str, dict[str, object]]:
    requested = await client.post(
        f"/edge/deployments/{deployment_id}/diagnostic-grants",
        headers=_platform_headers(key=f"{key_prefix}-request-0001"),
        json={
            "action": "request",
            "invocation": {"command_code": "runtime.version"},
            "reason_code": "customer_reported_issue",
            "max_runs": 1,
        },
    )
    assert requested.status_code == 201, requested.text
    grant_id = requested.json()["grant"]["diagnostic_grant_id"]
    approved = await client.post(
        f"/edge/deployments/{deployment_id}/diagnostic-grants",
        headers=_workspace_headers(
            key=f"{key_prefix}-approve-0001", step_up="s" * 43
        ),
        json={
            "action": "approve",
            "diagnostic_grant_id": grant_id,
            "reason_code": "customer_approved",
            "ttl_seconds": 300,
        },
    )
    assert approved.status_code == 200, approved.text
    run = await client.post(
        f"/edge/deployments/{deployment_id}/diagnostics",
        headers=_platform_headers(
            key=f"{key_prefix}-run-0001", step_up="t" * 43
        ),
        json={
            "action": "run",
            "diagnostic_grant_id": grant_id,
            "deadline_seconds": deadline_seconds,
            "reason_code": "approved_diagnostic",
        },
    )
    assert run.status_code == 201, run.text
    return grant_id, run.json()["command"]


async def _complete_runtime_version_command(
    client: AsyncClient,
    deployment_id: str,
    grant_id: str,
    command: dict[str, object],
) -> None:
    command_id = str(command["diagnostic_command_id"])
    pending = await client.get(
        f"/edge/deployments/{deployment_id}/diagnostics/pending",
        headers=_agent_headers(deployment_id),
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["command"]["command_id"] == command_id
    leased = await client.post(
        f"/edge/deployments/{deployment_id}/diagnostics/{command_id}/lease",
        headers=_agent_headers(deployment_id),
        json={"action": "lease", "lease_nonce": "v" * 32, "expected_revision": 1},
    )
    assert leased.status_code == 200, leased.text
    running = await client.post(
        f"/edge/deployments/{deployment_id}/diagnostics/{command_id}/lease",
        headers=_agent_headers(deployment_id),
        json={"action": "start", "lease_nonce": "v" * 32, "expected_revision": 2},
    )
    assert running.status_code == 200, running.text
    started = datetime.now(UTC)
    completed = started + timedelta(milliseconds=10)
    result_value = RuntimeVersionResult(
        command_code="runtime.version",
        agent_version="1.0.0",
        gateway_schema_version=118,
        os_family="linux",
        architecture="x86_64",
    )
    report = {
        "schema_version": "metahub.edge-diagnostic-result/v1",
        "command_id": command_id,
        "grant_id": grant_id,
        "deployment_id": deployment_id,
        "generation": command["generation"],
        "status": "completed",
        "reason_code": "completed",
        "result": result_value.model_dump(mode="json"),
        "result_sha256": diagnostic_result_sha256(result_value),
        "started_at": _utc_text(started),
        "completed_at": _utc_text(completed),
        "duration_ms": 10,
    }
    response = await client.post(
        f"/edge/deployments/{deployment_id}/diagnostics/{command_id}/result",
        headers=_agent_headers(deployment_id),
        json={"report": report},
    )
    assert response.status_code == 201, response.text


def _insert_restart_session(config: AppConfig, deployment_id: str) -> str:
    token = "egs_" + "r" * 43
    with closing(connect_database(config.database_url)) as connection:
        source = connection.execute(
            "SELECT * FROM edge_sessions WHERE edge_deployment_id=?",
            (deployment_id,),
        ).fetchone()
        assert source is not None
        connection.execute(
            """
            INSERT INTO edge_sessions(
              edge_session_id,workspace_id,tenant_id,edge_deployment_id,
              edge_device_identity_id,edge_device_certificate_id,session_token_hmac,
              peer_binding_sha256,handshake_nonce_hmac,identity_generation,
              revocation_generation,status,expires_at,revoked_reason_code,created_at,revoked_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?,NULL,?,NULL)
            """,
            (
                "eds_" + "9" * 32,
                source["workspace_id"],
                source["tenant_id"],
                deployment_id,
                source["edge_device_identity_id"],
                source["edge_device_certificate_id"],
                edge_service._secret_hmac(config, "edge-session-token-v1", token),
                "f" * 64,
                _digest(f"prd126-restart-{deployment_id}"),
                source["identity_generation"],
                source["revocation_generation"],
                source["expires_at"],
                _utc_text(datetime.now(UTC)),
            ),
        )
        connection.commit()
    return token


@pytest.mark.anyio
async def test_fleet_health_cursor_and_complete_diagnostic_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, platform = runtime
    app = _app(monkeypatch, config, actor, platform)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for deployment_id in DEPLOYMENTS:
            health = await client.get(
                f"/edge/deployments/{deployment_id}/health",
                headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
            )
            assert health.status_code == 200, health.text
            assert health.json()["health"]["score_model_version"] == "edge-health-v1"

        first_page = await client.get(
            "/platform/fleet?limit=1",
            headers={"Cookie": f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"},
        )
        assert first_page.status_code == 200, first_page.text
        assert first_page.json()["count"] == 1
        cursor = first_page.json()["next_cursor"]
        assert cursor
        second_page = await client.get(
            f"/platform/fleet?limit=1&cursor={cursor}",
            headers={"Cookie": f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"},
        )
        assert second_page.status_code == 200, second_page.text
        tampered = await client.get(
            f"/platform/fleet?limit=1&health=critical&cursor={cursor}",
            headers={"Cookie": f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"},
        )
        assert tampered.status_code == 400
        assert tampered.json()["code"] == "platform.edge_fleet_cursor_invalid"

        requested = await client.post(
            f"/edge/deployments/{DEPLOYMENTS[0]}/diagnostic-grants",
            headers=_platform_headers(key="prd126-grant-request-0001"),
            json={
                "action": "request",
                "invocation": {"command_code": "runtime.version"},
                "reason_code": "customer_reported_issue",
                "max_runs": 1,
            },
        )
        assert requested.status_code == 201, requested.text
        grant_id = requested.json()["grant"]["diagnostic_grant_id"]

        missing_approval_step_up = await client.post(
            f"/edge/deployments/{DEPLOYMENTS[0]}/diagnostic-grants",
            headers=_workspace_headers(key="prd126-grant-approve-missing-0001"),
            json={
                "action": "approve",
                "diagnostic_grant_id": grant_id,
                "reason_code": "customer_approved",
                "ttl_seconds": 300,
            },
        )
        assert missing_approval_step_up.status_code == 403
        assert missing_approval_step_up.json()["code"] == "step_up.required"

        approved = await client.post(
            f"/edge/deployments/{DEPLOYMENTS[0]}/diagnostic-grants",
            headers=_workspace_headers(
                key="prd126-grant-approve-0001", step_up="s" * 43
            ),
            json={
                "action": "approve",
                "diagnostic_grant_id": grant_id,
                "reason_code": "customer_approved",
                "ttl_seconds": 300,
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["grant"]["status"] == "active"

        missing_run_step_up = await client.post(
            f"/edge/deployments/{DEPLOYMENTS[0]}/diagnostics",
            headers=_platform_headers(key="prd126-command-missing-step-up-0001"),
            json={
                "action": "run",
                "diagnostic_grant_id": grant_id,
                "deadline_seconds": 30,
                "reason_code": "approved_diagnostic",
            },
        )
        assert missing_run_step_up.status_code == 403
        assert missing_run_step_up.json()["code"] == "step_up.required"

        run = await client.post(
            f"/edge/deployments/{DEPLOYMENTS[0]}/diagnostics",
            headers=_platform_headers(
                key="prd126-command-run-0001", step_up="t" * 43
            ),
            json={
                "action": "run",
                "diagnostic_grant_id": grant_id,
                "deadline_seconds": 30,
                "reason_code": "approved_diagnostic",
            },
        )
        assert run.status_code == 201, run.text
        command = run.json()["command"]
        command_id = command["diagnostic_command_id"]
        assert command["status"] == "authorized"

        pending = await client.get(
            f"/edge/deployments/{DEPLOYMENTS[0]}/diagnostics/pending",
            headers=_agent_headers(DEPLOYMENTS[0]),
        )
        assert pending.status_code == 200, pending.text
        assert pending.json()["command"]["command_id"] == command_id
        assert "signature" in pending.json()["command"]

        leased = await client.post(
            f"/edge/deployments/{DEPLOYMENTS[0]}/diagnostics/{command_id}/lease",
            headers=_agent_headers(DEPLOYMENTS[0]),
            json={"action": "lease", "lease_nonce": "n" * 32, "expected_revision": 1},
        )
        assert leased.status_code == 200, leased.text
        assert leased.json()["command"]["status"] == "leased"
        running = await client.post(
            f"/edge/deployments/{DEPLOYMENTS[0]}/diagnostics/{command_id}/lease",
            headers=_agent_headers(DEPLOYMENTS[0]),
            json={"action": "start", "lease_nonce": "n" * 32, "expected_revision": 2},
        )
        assert running.status_code == 200, running.text
        assert running.json()["command"]["status"] == "running"

        started = datetime.now(UTC)
        completed = started + timedelta(milliseconds=10)
        result_value = RuntimeVersionResult(
            command_code="runtime.version",
            agent_version="1.0.0",
            gateway_schema_version=118,
            os_family="linux",
            architecture="x86_64",
        )
        report = {
            "schema_version": "metahub.edge-diagnostic-result/v1",
            "command_id": command_id,
            "grant_id": grant_id,
            "deployment_id": DEPLOYMENTS[0],
            "generation": command["generation"],
            "status": "completed",
            "reason_code": "completed",
            "result": result_value.model_dump(mode="json"),
            "result_sha256": diagnostic_result_sha256(result_value),
            "started_at": _utc_text(started),
            "completed_at": _utc_text(completed),
            "duration_ms": 10,
        }
        completed_response = await client.post(
            f"/edge/deployments/{DEPLOYMENTS[0]}/diagnostics/{command_id}/result",
            headers=_agent_headers(DEPLOYMENTS[0]),
            json={"report": report},
        )
        assert completed_response.status_code == 201, completed_response.text
        assert completed_response.json()["result"]["status"] == "completed"
        replay = await client.post(
            f"/edge/deployments/{DEPLOYMENTS[0]}/diagnostics/{command_id}/result",
            headers=_agent_headers(DEPLOYMENTS[0]),
            json={"report": report},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        health_after = await client.get(
            f"/edge/deployments/{DEPLOYMENTS[0]}/health",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert health_after.status_code == 200, health_after.text
        assert health_after.json()["diagnostic_grants"][0]["status"] == "exhausted"
        assert health_after.json()["diagnostic_results"][0]["status"] == "completed"

    with closing(connect_database(config.database_url)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_diagnostic_events "
            "WHERE event_type='edge.diagnostic_completed'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
async def test_workspace_health_only_permission_hides_diagnostic_data(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, platform = runtime
    app = _app(monkeypatch, config, actor, platform)
    deployment_id = DEPLOYMENTS[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        grant_id, command = await _authorize_diagnostic_command(
            client,
            deployment_id,
            key_prefix="prd126-health-only",
        )
        await _complete_runtime_version_command(
            client,
            deployment_id,
            grant_id,
            command,
        )
        with closing(connect_database(config.database_url)) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM edge_diagnostic_grants WHERE edge_deployment_id=?",
                (deployment_id,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM edge_diagnostic_results WHERE edge_deployment_id=?",
                (deployment_id,),
            ).fetchone()[0] == 1

        health_only_actor = replace(
            actor,
            permission_codes=("edge.health.read",),
        )
        monkeypatch.setattr(
            fleet_service,
            "authenticate_workspace_session_in_transaction",
            lambda *_args, **_kwargs: health_only_actor,
        )
        response = await client.get(
            f"/edge/deployments/{deployment_id}/health",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["health"]["deployment_id"] == deployment_id
        assert response.json()["diagnostic_grants"] == []
        assert response.json()["diagnostic_results"] == []


@pytest.mark.anyio
async def test_workspace_revoke_cancels_authorized_command_and_agent_observes_watermark(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, platform = runtime
    app = _app(monkeypatch, config, actor, platform)
    deployment_id = DEPLOYMENTS[1]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.get(
            f"/edge/deployments/{deployment_id}/health",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        requested = await client.post(
            f"/edge/deployments/{deployment_id}/diagnostic-grants",
            headers=_platform_headers(key="prd126-revoke-grant-request-0001"),
            json={
                "action": "request",
                "invocation": {
                    "command_code": "runtime.disk_budget",
                    "volume_code": "runtime",
                },
                "reason_code": "capacity_investigation",
                "max_runs": 1,
            },
        )
        assert requested.status_code == 201, requested.text
        grant_id = requested.json()["grant"]["diagnostic_grant_id"]
        approved = await client.post(
            f"/edge/deployments/{deployment_id}/diagnostic-grants",
            headers=_workspace_headers(
                key="prd126-revoke-grant-approve-0001", step_up="u" * 43
            ),
            json={
                "action": "approve",
                "diagnostic_grant_id": grant_id,
                "reason_code": "customer_approved",
                "ttl_seconds": 300,
            },
        )
        assert approved.status_code == 200, approved.text
        run = await client.post(
            f"/edge/deployments/{deployment_id}/diagnostics",
            headers=_platform_headers(
                key="prd126-revoke-command-run-0001", step_up="v" * 43
            ),
            json={
                "action": "run",
                "diagnostic_grant_id": grant_id,
                "deadline_seconds": 30,
                "reason_code": "approved_diagnostic",
            },
        )
        assert run.status_code == 201, run.text
        command_id = run.json()["command"]["diagnostic_command_id"]
        revoked = await client.post(
            f"/edge/deployments/{deployment_id}/diagnostic-grants",
            headers=_workspace_headers(
                key="prd126-grant-revoke-0001", step_up="x" * 43
            ),
            json={
                "action": "revoke",
                "diagnostic_grant_id": grant_id,
                "reason_code": "customer_revoked",
            },
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["grant"]["status"] == "revoked"
        pending = await client.get(
            f"/edge/deployments/{deployment_id}/diagnostics/pending",
            headers=_agent_headers(deployment_id),
        )
        assert pending.status_code == 200, pending.text
        assert pending.json()["command"] is None
        assert command_id in pending.json()["cancelled_command_ids"]


@pytest.mark.anyio
async def test_domain_credentials_and_authority_revision_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, platform = runtime
    app = _app(monkeypatch, config, actor, platform)
    deployment_id = DEPLOYMENTS[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        mixed_platform = await client.get(
            "/platform/fleet?limit=1",
            headers={
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; "
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"
                )
            },
        )
        assert mixed_platform.status_code == 400
        assert mixed_platform.json()["code"] == "platform.credential_conflict"

        mixed_workspace = await client.get(
            f"/edge/deployments/{deployment_id}/health",
            headers={
                "Cookie": (
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
                    f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"
                )
            },
        )
        assert mixed_workspace.status_code == 400
        assert mixed_workspace.json()["code"] == "workspace_session.credential_conflict"

        agent_with_cookie = await client.get(
            f"/edge/deployments/{deployment_id}/diagnostics/pending",
            headers={
                **_agent_headers(deployment_id),
                "Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}",
            },
        )
        assert agent_with_cookie.status_code == 401
        assert agent_with_cookie.json()["code"] == "edge.authentication_failed"

        _grant_id, command = await _authorize_diagnostic_command(
            client,
            deployment_id,
            key_prefix="prd126-authority",
        )
        command_id = str(command["diagnostic_command_id"])
        cross_deployment_agent = await client.get(
            f"/edge/deployments/{deployment_id}/diagnostics/pending",
            headers=_agent_headers(DEPLOYMENTS[1]),
        )
        assert cross_deployment_agent.status_code == 401
        assert cross_deployment_agent.json()["code"] == "edge.authentication_failed"

        with closing(connect_database(config.database_url)) as connection:
            connection.execute(
                """
                UPDATE platform_operators
                SET revision=revision+1,updated_at='2026-08-27T23:59:59.999Z'
                WHERE operator_id=?
                """,
                (platform.operator_id,),
            )
            connection.commit()

        invalidated = await client.get(
            f"/edge/deployments/{deployment_id}/diagnostics/pending",
            headers=_agent_headers(deployment_id),
        )
        assert invalidated.status_code == 200, invalidated.text
        assert invalidated.json()["command"] is None
        assert command_id in invalidated.json()["cancelled_command_ids"]

    with closing(connect_database(config.database_url)) as connection:
        stored = connection.execute(
            "SELECT status FROM edge_diagnostic_commands WHERE diagnostic_command_id=?",
            (command_id,),
        ).fetchone()
        assert stored is not None
        assert stored["status"] == "expired"


@pytest.mark.anyio
async def test_parameter_injection_and_free_output_are_rejected_without_reflection(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, platform = runtime
    app = _app(monkeypatch, config, actor, platform)
    deployment_id = DEPLOYMENTS[0]
    invalid_requests = (
        {
            "action": "request",
            "invocation": {
                "command_code": "runtime.version",
                "arguments": ["--all;rm -rf /"],
            },
            "reason_code": "customer_reported_issue",
            "max_runs": 1,
        },
        {
            "action": "request",
            "invocation": {"command_code": "runtime.shell", "script": "printenv"},
            "reason_code": "customer_reported_issue",
            "max_runs": 1,
        },
        {
            "action": "request",
            "invocation": {"command_code": "runtime.version"},
            "reason_code": "approved;drop_table",
            "max_runs": 1,
            "shell": True,
        },
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for index, payload in enumerate(invalid_requests):
            response = await client.post(
                f"/edge/deployments/{deployment_id}/diagnostic-grants",
                headers=_platform_headers(key=f"prd126-invalid-params-{index:04d}"),
                json=payload,
            )
            assert response.status_code == 400
            assert response.json()["code"] == "edge.diagnostic_request_invalid"
            assert "rm -rf" not in response.text
            assert "printenv" not in response.text
            assert "drop_table" not in response.text

        now = datetime.now(UTC)
        forbidden_result = await client.post(
            f"/edge/deployments/{deployment_id}/diagnostics/{'dcm_' + '8' * 32}/result",
            headers=_agent_headers(deployment_id),
            json={
                "report": {
                    "schema_version": "metahub.edge-diagnostic-result/v1",
                    "command_id": "dcm_" + "8" * 32,
                    "grant_id": "dgr_" + "8" * 32,
                    "deployment_id": deployment_id,
                    "generation": 1,
                    "status": "cancelled",
                    "reason_code": "cancelled",
                    "result": None,
                    "result_sha256": None,
                    "started_at": _utc_text(now),
                    "completed_at": _utc_text(now),
                    "duration_ms": 0,
                    "stdout": "METAHUB_PROVIDER_KEY=do-not-reflect",
                }
            },
        )
        assert forbidden_result.status_code == 422
        assert forbidden_result.json()["code"] == "edge.diagnostic_result_invalid"
        assert "METAHUB_PROVIDER_KEY" not in forbidden_result.text
        assert "do-not-reflect" not in forbidden_result.text


@pytest.mark.anyio
async def test_lease_expiry_recovers_after_agent_restart_and_late_timeout_is_audited(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, platform = runtime
    restart_token = _insert_restart_session(config, DEPLOYMENTS[0])
    app = _app(monkeypatch, config, actor, platform)
    deployment_id = DEPLOYMENTS[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        grant_id, command = await _authorize_diagnostic_command(
            client,
            deployment_id,
            key_prefix="prd126-restart",
            deadline_seconds=90,
        )
        command_id = str(command["diagnostic_command_id"])
        leased = await client.post(
            f"/edge/deployments/{deployment_id}/diagnostics/{command_id}/lease",
            headers=_agent_headers(deployment_id),
            json={"action": "lease", "lease_nonce": "l" * 32, "expected_revision": 1},
        )
        assert leased.status_code == 200, leased.text
        lease_expiry = _datetime_value(leased.json()["command"]["lease_expires_at"])
        monkeypatch.setattr(
            fleet_service, "_utcnow", lambda: lease_expiry + timedelta(milliseconds=1)
        )

        recovered = await client.get(
            f"/edge/deployments/{deployment_id}/diagnostics/pending",
            headers={"X-Edge-Session": restart_token},
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["command"]["command_id"] == command_id
        assert recovered.json()["command_revision"] == 3

        restarted_lease = await client.post(
            f"/edge/deployments/{deployment_id}/diagnostics/{command_id}/lease",
            headers={"X-Edge-Session": restart_token},
            json={"action": "lease", "lease_nonce": "r" * 32, "expected_revision": 3},
        )
        assert restarted_lease.status_code == 200, restarted_lease.text
        started = await client.post(
            f"/edge/deployments/{deployment_id}/diagnostics/{command_id}/lease",
            headers={"X-Edge-Session": restart_token},
            json={"action": "start", "lease_nonce": "r" * 32, "expected_revision": 4},
        )
        assert started.status_code == 200, started.text

        issued_at = _datetime_value(str(command["issued_at"]))
        deadline_at = _datetime_value(str(command["deadline_at"]))
        late_time = deadline_at + timedelta(milliseconds=1)
        monkeypatch.setattr(fleet_service, "_utcnow", lambda: late_time)
        expired = await client.get(
            f"/edge/deployments/{deployment_id}/diagnostics/pending",
            headers={"X-Edge-Session": restart_token},
        )
        assert expired.status_code == 200, expired.text
        assert expired.json()["command"] is None
        assert command_id in expired.json()["cancelled_command_ids"]

        late_receipt = await client.post(
            f"/edge/deployments/{deployment_id}/diagnostics/{command_id}/result",
            headers={"X-Edge-Session": restart_token},
            json={
                "report": {
                    "schema_version": "metahub.edge-diagnostic-result/v1",
                    "command_id": command_id,
                    "grant_id": grant_id,
                    "deployment_id": deployment_id,
                    "generation": command["generation"],
                    "status": "timed_out",
                    "reason_code": "deadline_exceeded",
                    "result": None,
                    "result_sha256": None,
                    "started_at": _utc_text(issued_at),
                    "completed_at": _utc_text(late_time),
                    "duration_ms": 90_001,
                }
            },
        )
        assert late_receipt.status_code == 201, late_receipt.text
        assert late_receipt.json()["result"]["status"] == "timed_out"

    with closing(connect_database(config.database_url)) as connection:
        command_row = connection.execute(
            "SELECT status,lease_owner_session_id FROM edge_diagnostic_commands "
            "WHERE diagnostic_command_id=?",
            (command_id,),
        ).fetchone()
        assert command_row is not None
        assert dict(command_row) == {"status": "expired", "lease_owner_session_id": None}
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_diagnostic_results "
            "WHERE diagnostic_command_id=? AND status='timed_out'",
            (command_id,),
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_platform_fleet_10k_keyset_is_stable_and_each_page_meets_slo(
    tmp_path: Path,
) -> None:
    database = tmp_path / "prd126-fleet-10k.sqlite3"
    config = _config(_url(database))
    statuses = ("active", "pending", "retired", "revoked")
    health_codes = ("healthy", "degraded", "critical", "offline", "revoked", "unknown")
    rows: list[tuple[object, ...]] = []
    expected: list[tuple[str, str, str, str]] = []
    updated_at = "2026-08-27T12:00:00.000Z"
    for index in range(10_000):
        deployment_id = "edp_" + f"{200_000 + index:032x}"
        status = statuses[index % len(statuses)]
        region = f"cn-fleet-{index % 17:02d}"
        health = health_codes[index % len(health_codes)]
        expected.append((status, region, health, deployment_id))
        rows.append(
            (
                deployment_id,
                region,
                "connected" if index % 2 == 0 else "isolated",
                status,
                health,
                index % 101,
                "none",
                0,
                "1.0.0",
                index % 20,
                index % 20,
                index % 20,
                index % 601,
                "healthy",
                1,
                1,
                updated_at,
            )
        )
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """
            CREATE TABLE edge_fleet_deployment_index(
              edge_deployment_id TEXT PRIMARY KEY,region_code TEXT NOT NULL,
              mode TEXT NOT NULL,status TEXT NOT NULL,health_code TEXT NOT NULL,
              health_score INTEGER NOT NULL,alert_code TEXT NOT NULL,
              maintenance_mode INTEGER NOT NULL,agent_version TEXT,
              desired_generation INTEGER NOT NULL,applied_generation INTEGER NOT NULL,
              lkg_generation INTEGER NOT NULL,telemetry_lag_seconds INTEGER,
              certificate_signal TEXT NOT NULL,source_health_generation INTEGER NOT NULL,
              index_revision INTEGER NOT NULL,updated_at TEXT NOT NULL)
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_edge_fleet_keyset_prd126
            ON edge_fleet_deployment_index(
              status,region_code,health_code,edge_deployment_id)
            """
        )
        connection.executemany(
            """
            INSERT INTO edge_fleet_deployment_index(
              edge_deployment_id,region_code,mode,status,health_code,health_score,
              alert_code,maintenance_mode,agent_version,desired_generation,
              applied_generation,lkg_generation,telemetry_lag_seconds,
              certificate_signal,source_health_generation,index_revision,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        connection.commit()
    platform = PlatformContext(
        platform_session_id="pts_" + "b" * 32,
        operator_id="pto_" + "b" * 32,
        account_id="gac_" + "b" * 32,
        global_session_id="gss_" + "b" * 32,
        factor_id="mfa_" + "b" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_ops",),
        permissions=("platform.edge_fleet.read",),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    def walk() -> tuple[list[str], list[float]]:
        cursor = None
        deployment_ids: list[str] = []
        durations: list[float] = []
        while True:
            started = time.perf_counter()
            page = fleet_service.list_platform_fleet(
                config,
                platform,
                status_filter=None,
                region_filter=None,
                health_filter=None,
                cursor=cursor,
                limit=200,
                request_id="req_prd126_fleet_10k",
                trace_id="trc_prd126_fleet_10k",
            )
            durations.append(time.perf_counter() - started)
            deployment_ids.extend(item.deployment_id for item in page.deployments)
            cursor = page.next_cursor
            if cursor is None:
                return deployment_ids, durations

    first_ids, first_durations = walk()
    second_ids, second_durations = walk()
    expected_ids = [row[3] for row in sorted(expected)]
    assert first_ids == expected_ids
    assert second_ids == expected_ids
    assert len(set(first_ids)) == 10_000
    durations = sorted(first_durations + second_durations)
    p95 = durations[int(len(durations) * 0.95) - 1]
    assert p95 < 1.0
