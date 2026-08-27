"""PRD-121 Workspace publication, Agent convergence, LKG, and privacy acceptance."""

from __future__ import annotations

import base64
import hashlib
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
from metahub_control.config_distribution.crypto import seal_config_value
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
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.private_gateway import (
    build_registration_challenge_proof,
    canonical_edge_json_bytes,
    certificate_sha256,
    parse_edge_csr,
    verify_desired_state,
    verify_trust_manifest,
)
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43
_TEST_PASSWORD = f"Prd121!Aa9{secrets.token_urlsafe(18)}"


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


def _second_admin(config: AppConfig, owner: WorkspaceSessionContext) -> WorkspaceSessionContext:
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            "SELECT account_id FROM global_accounts WHERE account_id!=? "
            "AND status='active' ORDER BY account_id LIMIT 1",
            (owner.account_id,),
        ).fetchone()
        if row is None:
            source = connection.execute(
                "SELECT * FROM global_accounts WHERE account_id=?", (owner.account_id,)
            ).fetchone()
            assert source is not None
            columns = [
                str(item["name"])
                for item in connection.execute("PRAGMA table_info(global_accounts)")
            ]
            values = {name: source[name] for name in columns}
            values["account_id"] = "gac_" + "2" * 32
            values["email_normalized"] = "prd121-approver@example.test"
            values["phone_e164"] = None
            values["display_name"] = "PRD-121 Approver"
            connection.execute(
                f"INSERT INTO global_accounts({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(values[name] for name in columns),
            )
            connection.commit()
            account_id = str(values["account_id"])
        else:
            account_id = str(row["account_id"])
    return replace(
        owner,
        account_id=account_id,
        membership_id="wsm_" + "2" * 32,
        membership_role="admin",
        display_name="PRD-121 Approver",
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
    platform: PlatformContext,
):
    monkeypatch.setattr(
        edge_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor_holder["actor"],
    )
    monkeypatch.setattr(
        edge_routes,
        "authenticate_platform_session_context",
        lambda *_args, **_kwargs: platform,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-121 Test",
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


def _workspace_headers(*, key: str, revision: int) -> dict[str, str]:
    return {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": WORKSPACE_CSRF,
        "Idempotency-Key": key,
        "If-Match": f'"{revision}"',
    }


def _csr(key: Ed25519PrivateKey) -> str:
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(key, None)
        .public_bytes(serialization.Encoding.PEM)
        .decode("ascii")
    )


def _insert_config_snapshot(
    config: AppConfig,
    actor: WorkspaceSessionContext,
    *,
    generation: int,
    revocation_watermark: int,
) -> str:
    snapshot_id = "csn_" + str(generation) * 32
    safe = {
        "schema_version": 1,
        "workspace_id": actor.workspace_id,
        "tenant_id": actor.tenant_id,
        "generation": generation,
        "revocation_watermark": revocation_watermark,
        "workspace": {"status": "active", "revision": generation},
        "placement": {
            "cell_code": "cn-east-1a",
            "region_code": "cn-east-1",
            "revision": 1,
        },
        "entitlements": None,
        "policy": None,
        "provider_key_refs": [],
        "feature_capabilities": [],
    }
    manifest = {
        **safe,
        "source_sha256": hashlib.sha256(canonical_json_bytes(safe)).hexdigest(),
    }
    sealed = seal_config_value(
        config,
        manifest,
        owner_id=snapshot_id,
        workspace_id=actor.workspace_id,
        tenant_id=actor.tenant_id,
        kind="manifest",
    )
    stamp = f"2026-08-26T12:00:0{generation}.000Z"
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """
            INSERT INTO config_snapshots(
              config_snapshot_id,workspace_id,tenant_id,generation,manifest_envelope,
              manifest_sha256,source_sha256,kms_key_version,signature_algorithm,
              signature_b64,revocation_watermark,published_at)
            VALUES(?,?,?,?,?,?,?,'test-v1','ed25519',?,?,?)
            """,
            (
                snapshot_id,
                actor.workspace_id,
                actor.tenant_id,
                generation,
                sealed.envelope,
                sealed.plaintext_sha256,
                manifest["source_sha256"],
                "s" * 86,
                revocation_watermark,
                stamp,
            ),
        )
        head = connection.execute(
            "SELECT * FROM config_snapshot_heads WHERE workspace_id=?",
            (actor.workspace_id,),
        ).fetchone()
        if head is None:
            connection.execute(
                """
                INSERT INTO config_snapshot_heads(
                  config_snapshot_head_id,workspace_id,tenant_id,generation,
                  config_snapshot_id,manifest_sha256,revocation_watermark,revision,
                  published_at,updated_at)
                VALUES(?,?,?,?,?,?,?,1,?,?)
                """,
                (
                    "csh_" + "1" * 32,
                    actor.workspace_id,
                    actor.tenant_id,
                    generation,
                    snapshot_id,
                    sealed.plaintext_sha256,
                    revocation_watermark,
                    stamp,
                    stamp,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE config_snapshot_heads SET generation=?,config_snapshot_id=?,
                  manifest_sha256=?,revocation_watermark=?,revision=revision+1,
                  published_at=?,updated_at=? WHERE config_snapshot_head_id=?
                """,
                (
                    generation,
                    snapshot_id,
                    sealed.plaintext_sha256,
                    revocation_watermark,
                    stamp,
                    stamp,
                    head["config_snapshot_head_id"],
                ),
            )
        connection.commit()
    return snapshot_id


@pytest.mark.anyio
async def test_signed_desired_state_approval_failure_lkg_and_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "prd121.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0118"
    with monkeypatch.context() as seed_patch:
        seed_patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    config = _config(database_url)
    owner = _owner_actor(config)
    approver = _second_admin(config, owner)
    actor_holder = {"actor": owner}
    platform = PlatformContext(
        platform_session_id="pts_" + "9" * 32,
        operator_id="pto_" + "9" * 32,
        account_id=owner.account_id,
        global_session_id="gss_" + "9" * 32,
        factor_id="mfa_" + "9" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=("platform.edge_fleet.manage", "platform.edge_fleet.read"),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    tls: dict[str, str | None] = {"fingerprint": None}
    transport = ASGITransport(
        app=_TlsScope(_app(monkeypatch, config, actor_holder, platform), tls)
    )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/edge/registrations",
            headers=_workspace_headers(key="prd121-registration-create-0001", revision=0),
            json={
                "display_name": "PRD-121 private gateway",
                "region_code": "cn-east-1",
                "mode": "connected",
                "token_ttl_seconds": 600,
            },
        )
        assert created.status_code == 201, created.text
        registration = created.json()
        deployment_id = registration["deployment"]["deployment_id"]
        agent_key = Ed25519PrivateKey.generate()
        csr = _csr(agent_key)
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
                "agent_version": "1.0.0",
                "capability_codes": ["config_apply"],
                "hardware_proof_sha256": "a" * 64,
            },
        )
        assert registered.status_code == 201, registered.text
        leaf = x509.load_pem_x509_certificate(
            registered.json()["certificate_pem"].encode("ascii")
        )
        tls["fingerprint"] = certificate_sha256(leaf)
        session = await client.post(
            "/edge/sessions",
            json={
                "deployment_id": deployment_id,
                "handshake_nonce": "ecn_" + "A" * 32,
                "ttl_seconds": 45,
            },
        )
        assert session.status_code == 201, session.text
        edge_session = session.json()["session_token"]
        agent_headers = {"X-Edge-Session": edge_session}

        source_one = _insert_config_snapshot(
            config, owner, generation=1, revocation_watermark=0
        )
        empty = await client.get(
            f"/edge/deployments/{deployment_id}/desired-state",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert empty.status_code == 200
        assert empty.json()["state"] is None
        assert empty.headers["etag"] == '"0"'
        mixed_workspace = await client.get(
            f"/edge/deployments/{deployment_id}/desired-state",
            headers={
                "Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}",
                "X-Edge-Session": edge_session,
            },
        )
        assert mixed_workspace.status_code == 400
        published = await client.post(
            f"/edge/deployments/{deployment_id}/desired-state",
            headers=_workspace_headers(key="prd121-publish-standard-0001", revision=0),
            json={"action": "publish", "source_config_snapshot_id": source_one},
        )
        assert published.status_code == 200, published.text
        assert published.json()["state"]["status"] == "published"
        assert published.json()["state"]["change_class"] == "standard"
        assert published.headers["etag"] == '"1"'

        pull_one = await client.get(
            f"/edge/deployments/{deployment_id}/desired-state", headers=agent_headers
        )
        assert pull_one.status_code == 200, pull_one.text
        assert canonical_edge_json_bytes(pull_one.json()) == pull_one.content
        first_wire = pull_one.json()
        trust_document = first_wire["trust_manifest"]
        kid = trust_document["current"]["kid"]
        public_key = base64.urlsafe_b64decode(
            trust_document["current"]["public_key"] + "="
        )
        trust = verify_trust_manifest(
            trust_document,
            trusted_keys={kid: public_key},
            now=datetime.now(UTC),
        )
        verified = verify_desired_state(
            first_wire["snapshot"],
            trust=trust,
            workspace_id=owner.workspace_id,
            tenant_id=owner.tenant_id,
            deployment_id=deployment_id,
            now=datetime.now(UTC),
        )
        assert verified.generation == 1
        first_snapshot_id = first_wire["snapshot"]["snapshot_id"]
        first_hash = first_wire["envelope_sha256"]
        applied = await client.post(
            f"/edge/deployments/{deployment_id}/acks",
            headers=agent_headers,
            json={
                "signed_snapshot_id": first_snapshot_id,
                "generation": 1,
                "envelope_sha256": first_hash,
                "status": "applied",
                "reason_code": "applied",
                "apply_duration_ms": 15,
                "lkg_generation": 1,
                "lkg_envelope_sha256": first_hash,
                "attempt_nonce": "ack_" + "A" * 32,
            },
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["applied_generation"] == 1
        assert applied.json()["lkg_generation"] == 1

        source_two = _insert_config_snapshot(
            config, owner, generation=2, revocation_watermark=1
        )
        pending = await client.post(
            f"/edge/deployments/{deployment_id}/desired-state",
            headers=_workspace_headers(key="prd121-publish-risk-000001", revision=2),
            json={"action": "publish", "source_config_snapshot_id": source_two},
        )
        assert pending.status_code == 200, pending.text
        pending_state = pending.json()["state"]
        assert pending_state["status"] == "approval_pending"
        assert pending_state["change_class"] == "high_risk"
        assert pending.headers["etag"] == '"1"'
        with closing(connect_database(database_url)) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM edge_signed_snapshots WHERE generation=2"
            ).fetchone()[0] == 0

        self_approval = await client.post(
            f"/edge/deployments/{deployment_id}/desired-state",
            headers=_workspace_headers(key="prd121-self-approve-00001", revision=1),
            json={
                "action": "approve",
                "desired_state_id": pending_state["desired_state_id"],
                "reason_code": "approved_change",
            },
        )
        assert self_approval.status_code == 412
        actor_holder["actor"] = approver
        approved = await client.post(
            f"/edge/deployments/{deployment_id}/desired-state",
            headers=_workspace_headers(key="prd121-second-approve-0001", revision=1),
            json={
                "action": "approve",
                "desired_state_id": pending_state["desired_state_id"],
                "reason_code": "approved_change",
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["state"]["approved_by_account_id"] == approver.account_id
        assert approved.headers["etag"] == '"3"'
        actor_holder["actor"] = owner

        pull_two = await client.get(
            f"/edge/deployments/{deployment_id}/desired-state", headers=agent_headers
        )
        assert pull_two.status_code == 200, pull_two.text
        second_wire = pull_two.json()
        assert second_wire["generation"] == 2
        failed = await client.post(
            f"/edge/deployments/{deployment_id}/acks",
            headers=agent_headers,
            json={
                "signed_snapshot_id": second_wire["snapshot"]["snapshot_id"],
                "generation": 2,
                "envelope_sha256": second_wire["envelope_sha256"],
                "status": "failed",
                "reason_code": "apply_failed",
                "apply_duration_ms": 25,
                "lkg_generation": 1,
                "lkg_envelope_sha256": first_hash,
                "attempt_nonce": "ack_" + "B" * 32,
            },
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["applied_generation"] == 1
        assert failed.json()["lkg_generation"] == 1
        drifted = await client.get(
            f"/edge/deployments/{deployment_id}/desired-state",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert drifted.status_code == 200
        assert drifted.json()["state"]["drifted"] is True
        assert drifted.json()["state"]["last_reason_code"] == "apply_failed"
        assert drifted.headers["etag"] == '"4"'

        rollback = await client.post(
            f"/edge/deployments/{deployment_id}/rollbacks",
            headers=_workspace_headers(key="prd121-rollback-lkg-00001", revision=4),
            json={"lkg_generation": 1, "reason_code": "apply_failed_recovery"},
        )
        assert rollback.status_code == 200, rollback.text
        assert rollback.json()["state"]["generation"] == 3
        assert rollback.json()["state"]["rollback_of_generation"] == 1
        pull_three = await client.get(
            f"/edge/deployments/{deployment_id}/desired-state", headers=agent_headers
        )
        assert pull_three.status_code == 200
        third_wire = pull_three.json()
        converged = await client.post(
            f"/edge/deployments/{deployment_id}/acks",
            headers=agent_headers,
            json={
                "signed_snapshot_id": third_wire["snapshot"]["snapshot_id"],
                "generation": 3,
                "envelope_sha256": third_wire["envelope_sha256"],
                "status": "applied",
                "reason_code": "applied",
                "apply_duration_ms": 12,
                "lkg_generation": 3,
                "lkg_envelope_sha256": third_wire["envelope_sha256"],
                "attempt_nonce": "ack_" + "C" * 32,
            },
        )
        assert converged.status_code == 200, converged.text
        assert converged.json()["applied_generation"] == 3
        fleet = await client.get(
            "/platform/fleet/edge-deployments",
            headers={"Cookie": f"__Host-metahub-platform-session={PLATFORM_TOKEN}"},
        )
        assert fleet.status_code == 200, fleet.text
        fleet_item = next(
            item
            for item in fleet.json()["deployments"]
            if item["deployment_id"] == deployment_id
        )
        assert fleet_item["desired_generation"] == 3
        assert fleet_item["applied_generation"] == 3
        assert fleet_item["lkg_generation"] == 3
        assert fleet_item["desired_state_drifted"] is False
        assert not {
            "workspace_id",
            "tenant_id",
            "edge_snapshot_artifact_id",
            "manifest",
        }.intersection(fleet_item)
        mixed_platform = await client.get(
            "/platform/fleet/edge-deployments",
            headers={
                "Cookie": f"__Host-metahub-platform-session={PLATFORM_TOKEN}",
                "X-Edge-Session": edge_session,
            },
        )
        assert mixed_platform.status_code == 400

        credential_conflict = await client.get(
            f"/edge/deployments/{deployment_id}/desired-state",
            headers={**agent_headers, "Authorization": "Bearer rejected"},
        )
        assert credential_conflict.status_code == 401

    with closing(connect_database(database_url)) as connection:
        event_types = {
            str(row[0])
            for row in connection.execute(
                "SELECT event_type FROM edge_desired_state_events"
            )
        }
        assert event_types == {
            "edge.desired_state_published",
            "edge.apply_failed",
            "edge.state_converged",
            "edge.rollback_completed",
        }
        text_values = [
            str(value)
            for table in (
                "edge_snapshot_signing_roots",
                "edge_trust_manifests",
                "edge_desired_states",
                "edge_snapshot_artifacts",
                "edge_signed_snapshots",
                "edge_apply_attempts",
                "edge_acks",
                "edge_desired_state_events",
            )
            for row in connection.execute(f"SELECT * FROM {table}")
            for value in tuple(row)
            if value is not None
        ]
        joined = "\n".join(text_values)
        assert edge_session not in joined
        assert config.edge_snapshot_signing_secret not in joined
        assert registration["registration_token"] not in joined
        assert registration["challenge"] not in joined
        assert "PRIVATE KEY" not in joined
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
