"""PRD-123 Agent sync and Workspace cloud-safe reference acceptance."""

from __future__ import annotations

import secrets
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from metahub_control.edge_management import secret_reference_service
from metahub_control.edge_management import service as edge_service
from metahub_control.edge_management.routes import router
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
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
_TEST_PASSWORD = f"Prd123!Aa9{secrets.token_urlsafe(18)}"


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
    actor_holder: dict[str, WorkspaceSessionContext],
):
    def workspace_auth(*_args, **_kwargs):
        return actor_holder["actor"]

    monkeypatch.setattr(
        edge_service, "authenticate_workspace_session_in_transaction", workspace_auth
    )
    monkeypatch.setattr(
        secret_reference_service,
        "authenticate_workspace_session_in_transaction",
        workspace_auth,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-123 Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Edge-Session",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def _workspace_headers() -> dict[str, str]:
    return {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "Idempotency-Key": "prd123-registration-command-0001",
        "X-CSRF-Token": WORKSPACE_CSRF,
    }


def _csr(key: Ed25519PrivateKey) -> str:
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(key, None)
        .public_bytes(serialization.Encoding.PEM)
        .decode("ascii")
    )


async def _register_agent(
    client: AsyncClient,
    tls: dict[str, str | None],
    *,
    suffix: str,
    capabilities: list[str],
) -> tuple[str, str, str]:
    tls["fingerprint"] = None
    headers = _workspace_headers()
    headers["Idempotency-Key"] = f"prd123-registration-{suffix}-0001"
    created = await client.post(
        "/edge/registrations",
        headers=headers,
        json={
            "display_name": f"PRD-123 gateway {suffix}",
            "region_code": "cn-east-1",
            "mode": "connected",
            "token_ttl_seconds": 600,
        },
    )
    assert created.status_code == 201
    registration = created.json()
    deployment_id = registration["deployment"]["deployment_id"]
    key = Ed25519PrivateKey.generate()
    csr = _csr(key)
    parsed = parse_edge_csr(csr)
    registered = await client.post(
        "/edge/registrations",
        json={
            "deployment_id": deployment_id,
            "registration_token": registration["registration_token"],
            "challenge": registration["challenge"],
            "challenge_proof": build_registration_challenge_proof(
                registration["registration_token"],
                deployment_id=deployment_id,
                challenge=registration["challenge"],
                csr_sha256=parsed.csr_sha256,
            ),
            "csr_pem": csr,
            "agent_version": "1.2.0",
            "capability_codes": capabilities,
            "hardware_proof_sha256": "a" * 64,
        },
    )
    assert registered.status_code == 201
    leaf = x509.load_pem_x509_certificate(
        registered.json()["certificate_pem"].encode("ascii")
    )
    fingerprint = certificate_sha256(leaf)
    tls["fingerprint"] = fingerprint
    session = await client.post(
        "/edge/sessions",
        json={
            "deployment_id": deployment_id,
            "handshake_nonce": "ecn_" + suffix[0].lower() * 32,
            "ttl_seconds": 45,
        },
    )
    assert session.status_code == 201
    return deployment_id, fingerprint, session.json()["session_token"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _changed_batch(
    *,
    batch_suffix: str,
    event_suffix: str,
    deployment_id: str,
    actor: WorkspaceSessionContext,
    occurred_at: str,
    tenant_id: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "batch_id": "lsb_" + batch_suffix * 32,
        "deployment_id": deployment_id,
        "events": [
            {
                "event_id": "lse_" + event_suffix * 32,
                "event_type": "edge.secret_reference_changed",
                "reference": {
                    "schema_version": "1",
                    "reference_id": "lsr_" + event_suffix * 32,
                    "deployment_id": deployment_id,
                    "tenant_id": tenant_id or actor.tenant_id,
                    "workspace_id": actor.workspace_id,
                    "provider_connection_id": "pcn_private_001",
                    "label": "Primary provider credential",
                    "kind": "api_key",
                    "status": "active",
                    "revision": 1,
                    "active_version": 1,
                    "last_test_outcome": None,
                    "capabilities": [],
                    "rotated_at": None,
                    "updated_at": occurred_at,
                },
                "occurred_at": occurred_at,
            }
        ],
        "sent_at": occurred_at,
    }


@pytest.mark.anyio
async def test_agent_sync_workspace_read_and_secret_zero_cloud_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "prd123.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0122"
    with monkeypatch.context() as seed_patch:
        seed_patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    config = _config(database_url)
    owner = _owner_actor(config)
    assert "edge.secret_reference.read" in owner.permission_codes
    actor_holder = {"actor": owner}
    tls: dict[str, str | None] = {"fingerprint": None}
    transport = ASGITransport(app=_TlsScope(_app(monkeypatch, config, actor_holder), tls))
    now = datetime.now(UTC).replace(microsecond=0)
    occurred = _wire_time(now - timedelta(seconds=1))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        deployment_id, fingerprint, edge_session = await _register_agent(
            client,
            tls,
            suffix="primary",
            capabilities=["config_apply", "secret_reference"],
        )
        agent_headers = {"X-Edge-Session": edge_session}
        first = _changed_batch(
            batch_suffix="1",
            event_suffix="1",
            deployment_id=deployment_id,
            actor=owner,
            occurred_at=occurred,
        )
        accepted = await client.post(
            "/internal/edge/secret-references/sync",
            headers=agent_headers,
            json=first,
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["disposition"] == "accepted"
        assert accepted.json()["accepted_event_count"] == 1
        assert "no-store" in accepted.headers["cache-control"]

        replay = await client.post(
            "/internal/edge/secret-references/sync",
            headers=agent_headers,
            json=first,
        )
        assert replay.status_code == 200
        assert replay.json()["disposition"] == "duplicate"
        assert replay.json()["receipt_id"] == accepted.json()["receipt_id"]

        conflict = dict(first)
        conflict["sent_at"] = _wire_time(now)
        reused = await client.post(
            "/internal/edge/secret-references/sync",
            headers=agent_headers,
            json=conflict,
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "edge.idempotency_conflict"

        listed = await client.get(
            "/edge/secret-references",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert listed.status_code == 200
        assert len(listed.json()["references"]) == 1
        assert listed.json()["references"][0]["reference_id"] == "lsr_" + "1" * 32
        assert set(listed.json()["references"][0]) == {
            "schema_version",
            "reference_id",
            "deployment_id",
            "tenant_id",
            "workspace_id",
            "provider_connection_id",
            "label",
            "kind",
            "status",
            "revision",
            "active_version",
            "last_test_outcome",
            "capabilities",
            "rotated_at",
            "updated_at",
        }

        second_time = _wire_time(now)
        rotation = {
            "schema_version": "1",
            "batch_id": "lsb_" + "2" * 32,
            "deployment_id": deployment_id,
            "events": [
                {
                    "event_id": "lse_" + "2" * 32,
                    "event_type": "edge.secret_rotation_failed",
                    "reference_id": "lsr_" + "1" * 32,
                    "revision": 2,
                    "outcome": "authentication_failed",
                    "occurred_at": second_time,
                },
                {
                    "event_id": "lse_" + "3" * 32,
                    "event_type": "edge.provider_test_completed",
                    "reference_id": "lsr_" + "1" * 32,
                    "version": 2,
                    "outcome": "authentication_failed",
                    "capabilities": [],
                    "latency_bucket": "100_499ms",
                    "occurred_at": second_time,
                },
            ],
            "sent_at": second_time,
        }
        failed_rotation = await client.post(
            "/internal/edge/secret-references/sync",
            headers=agent_headers,
            json=rotation,
        )
        assert failed_rotation.status_code == 200, failed_rotation.text
        listed = await client.get(
            "/edge/secret-references",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        projection = listed.json()["references"][0]
        assert projection["status"] == "degraded"
        assert projection["revision"] == 2
        assert projection["last_test_outcome"] == "authentication_failed"

        sensitive_canary = "prd123-secret-canary-must-not-persist"
        forbidden = _changed_batch(
            batch_suffix="4",
            event_suffix="4",
            deployment_id=deployment_id,
            actor=owner,
            occurred_at=second_time,
        )
        forbidden["events"][0]["reference"]["value"] = sensitive_canary
        rejected = await client.post(
            "/internal/edge/secret-references/sync",
            headers=agent_headers,
            json=forbidden,
        )
        assert rejected.status_code == 400
        assert sensitive_canary not in rejected.text

        cross_owner = _changed_batch(
            batch_suffix="5",
            event_suffix="5",
            deployment_id=deployment_id,
            actor=owner,
            occurred_at=second_time,
            tenant_id="ten_cross_owner",
        )
        denied_owner = await client.post(
            "/internal/edge/secret-references/sync",
            headers=agent_headers,
            json=cross_owner,
        )
        assert denied_owner.status_code == 401

        mixed = await client.post(
            "/internal/edge/secret-references/sync",
            headers={
                **agent_headers,
                "Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}",
            },
            json=first,
        )
        assert mixed.status_code == 401
        tls["fingerprint"] = None
        missing_mtls = await client.post(
            "/internal/edge/secret-references/sync",
            headers=agent_headers,
            json=first,
        )
        assert missing_mtls.status_code == 401
        tls["fingerprint"] = fingerprint

        actor_holder["actor"] = replace(
            owner,
            permission_codes=tuple(
                code for code in owner.permission_codes if code != "edge.secret_reference.read"
            ),
        )
        revoked = await client.get(
            "/edge/secret-references",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert revoked.status_code == 403
        actor_holder["actor"] = owner

        _deployment_without_capability, _fingerprint, no_capability_session = (
            await _register_agent(
                client,
                tls,
                suffix="limited",
                capabilities=["config_apply"],
            )
        )
        no_capability = await client.post(
            "/internal/edge/secret-references/sync",
            headers={"X-Edge-Session": no_capability_session},
            json=_changed_batch(
                batch_suffix="6",
                event_suffix="6",
                deployment_id=_deployment_without_capability,
                actor=owner,
                occurred_at=second_time,
            ),
        )
        assert no_capability.status_code == 401

    assert sensitive_canary.encode() not in database.read_bytes()
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM edge_secret_references").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM edge_secret_events").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_secret_event_outbox"
        ).fetchone()[0] == 6
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
