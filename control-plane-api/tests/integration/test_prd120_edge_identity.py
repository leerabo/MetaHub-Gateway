"""PRD-120 Edge registration, mTLS, rotation, revocation, and privacy acceptance."""

from __future__ import annotations

import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from metahub_control.edge_management import routes as edge_routes
from metahub_control.edge_management import service as edge_service
from metahub_control.edge_management.routes import router
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
from metahub_shared.private_gateway import (
    build_registration_challenge_proof,
    certificate_sha256,
    parse_edge_csr,
)
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43
_TEST_PASSWORD = f"Prd120!Aa9{secrets.token_urlsafe(18)}"


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
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd120-edge-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0118"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path
) -> tuple[AppConfig, WorkspaceSessionContext, PlatformContext]:
    database = tmp_path / "prd120.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    actor = _owner_actor(config)
    platform = PlatformContext(
        platform_session_id="pts_" + "9" * 32,
        operator_id="pto_" + "9" * 32,
        account_id=actor.account_id,
        global_session_id="gss_" + "9" * 32,
        factor_id="mfa_" + "9" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=("platform.edge_fleet.manage", "platform.edge_fleet.read"),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return config, actor, platform


def _owner_actor(config: AppConfig) -> WorkspaceSessionContext:
    with connect_database(config.database_url) as connection:
        row = connection.execute(
            """
            SELECT membership.workspace_membership_id,membership.workspace_id,
              membership.tenant_id,membership.account_id,membership.legacy_user_id,
              tenant.slug,tenant.name,user.email,user.display_name
            FROM workspace_memberships membership
            JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
            JOIN users user
              ON user.user_id=membership.legacy_user_id
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
    assert {"edge.deployment.read", "edge.deployment.manage"} <= set(
        snapshot.permission_codes
    )
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


class _TlsScope:
    def __init__(self, app, holder: dict[str, str | None]) -> None:
        self.app = app
        self.holder = holder

    async def __call__(self, scope, receive, send) -> None:
        fingerprint = self.holder.get("fingerprint")
        if scope["type"] == "http" and fingerprint is not None:
            scope = dict(scope)
            extensions = dict(scope.get("extensions") or {})
            extensions["tls"] = {"client_cert_sha256": fingerprint}
            scope["extensions"] = extensions
        await self.app(scope, receive, send)


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    actor: WorkspaceSessionContext,
    platform: PlatformContext,
):
    monkeypatch.setattr(
        edge_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    monkeypatch.setattr(
        edge_routes,
        "authenticate_platform_session_context",
        lambda *_args, **_kwargs: platform,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-120 Test",
        extra_cors_allow_headers=("Idempotency-Key", "If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def _workspace_headers(
    *, key: str | None = None, revision: int | None = None
) -> dict[str, str]:
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
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return headers


def _csr(key: Ed25519PrivateKey) -> str:
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(key, None)
        .public_bytes(serialization.Encoding.PEM)
        .decode("ascii")
    )


@pytest.mark.anyio
async def test_registration_rotation_session_replay_and_atomic_revoke(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, platform = runtime
    tls: dict[str, str | None] = {"fingerprint": None}
    transport = ASGITransport(app=_TlsScope(_app(monkeypatch, config, actor, platform), tls))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/edge/registrations",
            headers=_workspace_headers(key="prd120-registration-create-0001"),
            json={
                "display_name": "Shanghai private gateway",
                "region_code": "cn-east-1",
                "mode": "connected",
                "token_ttl_seconds": 600,
            },
        )
        assert create.status_code == 201, create.text
        registration = create.json()
        deployment_id = registration["deployment"]["deployment_id"]
        assert registration["deployment"]["status"] == "pending"
        assert registration["registration_token"].startswith("erg_")
        assert registration["challenge"].startswith("ech_")
        assert create.headers["cache-control"] == "no-store, private"

        replay_create = await client.post(
            "/edge/registrations",
            headers=_workspace_headers(key="prd120-registration-create-0001"),
            json={
                "display_name": "Shanghai private gateway",
                "region_code": "cn-east-1",
                "mode": "connected",
                "token_ttl_seconds": 600,
            },
        )
        assert replay_create.status_code == 201
        assert replay_create.json()["idempotent_replay"] is True
        assert replay_create.json()["registration_token"] is None
        assert replay_create.json()["challenge"] is None

        first_key = Ed25519PrivateKey.generate()
        first_csr = _csr(first_key)
        parsed = parse_edge_csr(first_csr)
        agent_payload = {
            "deployment_id": deployment_id,
            "registration_token": registration["registration_token"],
            "challenge": registration["challenge"],
            "challenge_proof": build_registration_challenge_proof(
                registration["registration_token"],
                deployment_id=deployment_id,
                challenge=registration["challenge"],
                csr_sha256=parsed.csr_sha256,
            ),
            "csr_pem": first_csr,
            "agent_version": "1.0.0",
            "capability_codes": ["config_apply", "telemetry"],
            "hardware_proof_sha256": "a" * 64,
        }
        registered = await client.post("/edge/registrations", json=agent_payload)
        assert registered.status_code == 201, registered.text
        issued = registered.json()
        assert issued["identity"]["generation"] == 1
        assert set(issued["permissions"]) == {
            "edge.self.register",
            "edge.self.rotate",
            "edge.self.connect",
        }
        leaf = x509.load_pem_x509_certificate(issued["certificate_pem"].encode("ascii"))
        ca = x509.load_pem_x509_certificate(issued["ca_certificate_pem"].encode("ascii"))
        ca.public_key().verify(leaf.signature, leaf.tbs_certificate_bytes)
        first_fingerprint = certificate_sha256(leaf)
        assert first_fingerprint == issued["certificate"]["certificate_sha256"]

        with connect_database(config.database_url) as connection:
            connection.execute(
                "UPDATE edge_registration_intents SET expires_at=? "
                "WHERE edge_deployment_id=? AND status='consumed'",
                ("2020-01-01T00:00:00.000Z", deployment_id),
            )
            connection.commit()
        exact_replay = await client.post("/edge/registrations", json=agent_payload)
        assert exact_replay.status_code == 201, exact_replay.text
        assert exact_replay.json()["idempotent_replay"] is True
        assert exact_replay.json()["certificate"]["certificate_id"] == issued["certificate"][
            "certificate_id"
        ]

        tls["fingerprint"] = first_fingerprint
        session = await client.post(
            "/edge/sessions",
            json={
                "deployment_id": deployment_id,
                "handshake_nonce": "ecn_" + "A" * 32,
                "ttl_seconds": 45,
            },
        )
        assert session.status_code == 201, session.text
        assert session.json()["session_token"].startswith("egs_")
        nonce_replay = await client.post(
            "/edge/sessions",
            json={
                "deployment_id": deployment_id,
                "handshake_nonce": "ecn_" + "A" * 32,
                "ttl_seconds": 45,
            },
        )
        assert nonce_replay.status_code == 401
        assert nonce_replay.json()["code"] == "edge.authentication_failed"

        second_key = Ed25519PrivateKey.generate()
        rotated = await client.post(
            "/edge/certificates",
            json={
                "deployment_id": deployment_id,
                "expected_generation": 1,
                "rotation_nonce": "ecn_" + "B" * 32,
                "csr_pem": _csr(second_key),
                "hardware_proof_sha256": "b" * 64,
            },
        )
        assert rotated.status_code == 200, rotated.text
        rotated_body = rotated.json()
        assert rotated_body["identity"]["generation"] == 2
        new_leaf = x509.load_pem_x509_certificate(
            rotated_body["certificate_pem"].encode("ascii")
        )
        second_fingerprint = certificate_sha256(new_leaf)

        deployments = await client.get(
            "/edge/deployments",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert deployments.status_code == 200, deployments.text
        deployment = deployments.json()["deployments"][0]
        assert deployment["identity_generation"] == 2
        assert deployment["current_certificate"]["certificate_sha256"] == second_fingerprint
        assert {event["event_type"] for event in deployment["recent_events"]} >= {
            "edge.registered",
            "edge.certificate_rotated",
            "edge.authentication_failed",
        }

        tls["fingerprint"] = second_fingerprint
        active_session = await client.post(
            "/edge/sessions",
            json={
                "deployment_id": deployment_id,
                "handshake_nonce": "ecn_" + "C" * 32,
                "ttl_seconds": 45,
            },
        )
        assert active_session.status_code == 201, active_session.text

        revoked = await client.post(
            f"/edge/deployments/{deployment_id}/revoke",
            headers=_workspace_headers(
                key="prd120-deployment-revoke-0001", revision=deployment["revision"]
            ),
            json={"reason_code": "device_lost"},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["deployment"]["status"] == "revoked"
        assert revoked.json()["deployment"]["revocation_generation"] == 2
        assert revoked.json()["revoked_session_count"] == 1

        after_revoke = await client.post(
            "/edge/sessions",
            json={
                "deployment_id": deployment_id,
                "handshake_nonce": "ecn_" + "D" * 32,
                "ttl_seconds": 45,
            },
        )
        assert after_revoke.status_code == 401
        fleet = await client.get(
            "/platform/fleet/edge-deployments",
            headers={"Cookie": f"__Host-metahub-platform-session={PLATFORM_TOKEN}"},
        )
        assert fleet.status_code == 200, fleet.text
        assert set(fleet.json()["deployments"][0]) == {
            "deployment_id",
            "region_code",
            "mode",
            "status",
            "revision",
            "revocation_generation",
            "agent_version",
            "health_code",
            "certificate_expires_at",
            "last_connected_at",
            "desired_generation",
            "applied_generation",
            "lkg_generation",
            "desired_state_status",
            "desired_state_reason_code",
            "desired_state_drifted",
            "telemetry_last_receipt_at",
            "telemetry_lag_seconds",
            "telemetry_backlog_seconds",
            "telemetry_cpu_bucket",
            "telemetry_memory_bucket",
            "telemetry_error_rate_basis_points",
            "telemetry_failure_code",
            "updated_at",
        }

    with connect_database(config.database_url) as connection:
        text_values = [
            str(value)
            for table in (
                "edge_deployments",
                "edge_registration_intents",
                "edge_device_identities",
                "edge_device_certificates",
                "edge_sessions",
                "edge_identity_events",
            )
            for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in tuple(row)
            if value is not None
        ]
        joined = "\n".join(text_values)
        assert registration["registration_token"] not in joined
        assert registration["challenge"] not in joined
        assert session.json()["session_token"] not in joined
        assert "PRIVATE KEY" not in joined
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
async def test_unverified_peer_and_spoofed_certificate_headers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, platform = runtime
    app = _app(monkeypatch, config, actor, platform)
    transport = ASGITransport(app=app)
    payload = {
        "deployment_id": "edp_" + "1" * 32,
        "handshake_nonce": "ecn_" + "A" * 32,
        "ttl_seconds": 45,
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/edge/sessions", json=payload)
        spoofed = await client.post(
            "/edge/sessions", json=payload, headers={"X-Client-Cert-Sha256": "a" * 64}
        )
    assert missing.status_code == spoofed.status_code == 401
    assert missing.json()["code"] == spoofed.json()["code"] == "edge.authentication_failed"


def test_edge_permission_matrix_tamper_fails_closed(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, _platform = runtime
    with connect_database(config.database_url) as connection:
        connection.execute("DROP TRIGGER trg_workspace_edge_roles_delete_prd120")
        connection.execute(
            "DELETE FROM workspace_edge_role_permissions "
            "WHERE role_code='owner' AND permission_code='edge.deployment.manage'"
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(ApiError) as failure:
            workspace_actor_permissions_in_transaction(
                connection,
                account_id=actor.account_id,
                workspace_id=actor.workspace_id,
                tenant_id=actor.tenant_id,
            )
        connection.rollback()
    assert failure.value.code == "workspace_session.unavailable"
