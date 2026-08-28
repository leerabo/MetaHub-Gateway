"""PRD-125 encrypted support-bundle upload, projection, and grant download API."""

from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from metahub_control.edge_management import service as edge_service
from metahub_control.edge_management import support_bundle_service
from metahub_control.edge_management.routes import router
from metahub_control.platform_access import service as platform_service
from metahub_control.platform_access.constants import SUPPORT_SESSION_COOKIE
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
    SupportBundleEntry,
    SupportBundleManifest,
    SupportBundleUploadMetadata,
    build_registration_challenge_proof,
    canonical_edge_json_bytes,
    certificate_sha256,
    parse_edge_csr,
    support_bundle_approval_proof_sha256,
    support_bundle_manifest_sha256,
)
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
REQUESTER_ACCOUNT_ID = "gac_12500000000000000000000000000001"
APPROVER_ACCOUNT_ID = "gac_12500000000000000000000000000002"
REQUESTER_OPERATOR_ID = "pto_12500000000000000000000000000001"
APPROVER_OPERATOR_ID = "pto_12500000000000000000000000000002"
REQUESTER_FACTOR_ID = "mfa_12500000000000000000000000000001"
_TEST_PASSWORD = f"Prd125!Aa9{secrets.token_urlsafe(18)}"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _wire_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _config(database_url: str, object_root: Path) -> AppConfig:
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
        support_session_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        provider_key_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        edge_support_bundle_root=str(object_root),
        edge_support_bundle_encryption_secret=None,
        edge_support_bundle_ttl_seconds=3600,
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
    assert "edge.support_bundle.read" in snapshot.permission_codes
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
):
    monkeypatch.setattr(
        edge_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-125 Support Bundle Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "X-CSRF-Token",
            "X-Edge-Deployment-ID",
            "X-Edge-Session",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def _workspace_headers(*, key: str) -> dict[str, str]:
    return {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Idempotency-Key": key,
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
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
    client: AsyncClient, tls: dict[str, str | None]
) -> tuple[str, str]:
    created = await client.post(
        "/edge/registrations",
        headers=_workspace_headers(key="prd125-support-registration-0001"),
        json={
            "display_name": "PRD-125 support gateway",
            "region_code": "cn-east-1",
            "mode": "connected",
            "token_ttl_seconds": 600,
        },
    )
    assert created.status_code == 201, created.text
    registration = created.json()
    deployment_id = str(registration["deployment"]["deployment_id"])
    csr = _csr(Ed25519PrivateKey.generate())
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
            "agent_version": "1.2.5",
            "capability_codes": ["diagnostics"],
            "hardware_proof_sha256": "a" * 64,
        },
    )
    assert registered.status_code == 201, registered.text
    certificate = x509.load_pem_x509_certificate(
        registered.json()["certificate_pem"].encode("ascii")
    )
    tls["fingerprint"] = certificate_sha256(certificate)
    session = await client.post(
        "/edge/sessions",
        json={
            "deployment_id": deployment_id,
            "handshake_nonce": "ecn_" + "a" * 32,
            "ttl_seconds": 45,
        },
    )
    assert session.status_code == 201, session.text
    return deployment_id, str(session.json()["session_token"])


def _upload_metadata(
    actor: WorkspaceSessionContext,
    deployment_id: str,
    *,
    bundle_marker: str,
    object_marker: bytes,
    created_at: datetime,
    expires_at: datetime,
) -> tuple[SupportBundleUploadMetadata, bytes]:
    bundle_id = "esb_" + bundle_marker * 32
    ciphertext = b"MHSB1" + b"n" * 12 + object_marker * 12 + b"t" * 16
    manifest = SupportBundleManifest(
        schema_version="metahub.support-bundle/v1",
        bundle_id=bundle_id,
        deployment_id=deployment_id,
        scan_version="support-bundle-scan-v1",
        scan_outcome="clean",
        entries=(
            SupportBundleEntry(
                collector_code="runtime.health",
                path="health/summary.json",
                classification="health",
                media_type="application/json",
                size_bytes=12,
                sha256="a" * 64,
            ),
        ),
        total_uncompressed_bytes=12,
        archive_size_bytes=len(ciphertext) - 33,
        archive_sha256="b" * 64,
        created_at=_wire_time(created_at),
        expires_at=_wire_time(expires_at),
    )
    approved_at = _wire_time(created_at + timedelta(seconds=1))
    manifest_sha256 = support_bundle_manifest_sha256(manifest)
    metadata = SupportBundleUploadMetadata(
        schema_version="metahub.support-bundle-upload/v1",
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        object_sha256=hashlib.sha256(ciphertext).hexdigest(),
        object_size_bytes=len(ciphertext),
        approved_by_account_id=actor.account_id,
        approval_proof_sha256=support_bundle_approval_proof_sha256(
            bundle_id=bundle_id,
            deployment_id=deployment_id,
            tenant_id=actor.tenant_id,
            workspace_id=actor.workspace_id,
            manifest_sha256=manifest_sha256,
            approved_by_account_id=actor.account_id,
            approved_at=approved_at,
            local_revision=2,
        ),
        approved_at=approved_at,
    )
    return metadata, ciphertext


def _frame(
    metadata: SupportBundleUploadMetadata, ciphertext: bytes, *, canonical: bool = True
) -> bytes:
    value = metadata.model_dump(mode="json")
    encoded = (
        canonical_edge_json_bytes(value)
        if canonical
        else json.dumps(value, indent=2, ensure_ascii=True).encode("ascii")
    )
    return len(encoded).to_bytes(4, "big") + encoded + ciphertext


def _agent_headers(deployment_id: str, edge_session: str, body: bytes) -> dict[str, str]:
    return {
        "Content-Length": str(len(body)),
        "Content-Type": "application/vnd.metahub.edge-support-bundle",
        "X-Edge-Deployment-ID": deployment_id,
        "X-Edge-Session": edge_session,
    }


def _prepare_support_principals(config: AppConfig, actor: WorkspaceSessionContext) -> None:
    stamp = _wire_time(datetime.now(UTC))
    with closing(connect_database(config.database_url)) as connection:
        source = connection.execute(
            "SELECT * FROM global_accounts WHERE account_id=?", (actor.account_id,)
        ).fetchone()
        assert source is not None
        for account_id, operator_id, role, suffix in (
            (REQUESTER_ACCOUNT_ID, REQUESTER_OPERATOR_ID, "platform_support", "requester"),
            (APPROVER_ACCOUNT_ID, APPROVER_OPERATOR_ID, "platform_security", "approver"),
        ):
            values = dict(source)
            values.update(
                {
                    "account_id": account_id,
                    "email_normalized": f"prd125-support-{suffix}@example.invalid",
                    "phone_e164": None,
                    "display_name": f"PRD-125 support {suffix}",
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
                VALUES(?,?,'active',1,NULL,?,?)
                """,
                (operator_id, account_id, stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO platform_operator_role_bindings(
                  operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                VALUES(?,?,'active',NULL,?,NULL)
                """,
                (operator_id, role, stamp),
            )
        connection.execute(
            """
            INSERT INTO account_mfa_factors(
              factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,
              revision,last_accepted_counter,verified_at,disabled_at,created_at,updated_at)
            VALUES(?,?,'webauthn',NULL,NULL,'active',1,NULL,?,NULL,?,?)
            """,
            (REQUESTER_FACTOR_ID, REQUESTER_ACCOUNT_ID, stamp, stamp, stamp),
        )
        connection.commit()


def _issue_grant(
    config: AppConfig,
    actor: WorkspaceSessionContext,
    marker: str,
    *,
    grant_expires_at: datetime | None = None,
) -> tuple[str, str, str]:
    now = datetime.now(UTC)
    expires_at = grant_expires_at or now + timedelta(minutes=15)
    grant_id = "sgr_" + marker * 32
    session_id = "sps_" + marker * 32
    support_token = "support_" + marker * 50
    with closing(connect_database(config.database_url)) as connection:
        workspace_revision = int(
            connection.execute(
                "SELECT revision FROM workspaces WHERE workspace_id=?",
                (actor.workspace_id,),
            ).fetchone()[0]
        )
        membership_revision = int(
            connection.execute(
                "SELECT revision FROM workspace_memberships WHERE workspace_membership_id=?",
                (actor.membership_id,),
            ).fetchone()[0]
        )
        stamp = _wire_time(now)
        connection.execute(
            """
            INSERT INTO support_grants(
              grant_id,requester_operator_id,workspace_id,tenant_id,scope_code,reason_code,
              status,operator_revision,workspace_revision,expires_at,revoked_at,
              created_at,updated_at)
            VALUES(?,?,?,?,'workspace.diagnostics.read','workspace_diagnostics',
              'active',1,?,?,NULL,?,?)
            """,
            (
                grant_id,
                REQUESTER_OPERATOR_ID,
                actor.workspace_id,
                actor.tenant_id,
                workspace_revision,
                _wire_time(expires_at),
                stamp,
                stamp,
            ),
        )
        connection.executemany(
            """
            INSERT INTO support_grant_approvals(
              approval_id,grant_id,approval_domain,operator_id,membership_id,
              authority_revision,created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                (
                    "sap_" + marker * 31 + "a",
                    grant_id,
                    "platform",
                    APPROVER_OPERATOR_ID,
                    None,
                    1,
                    stamp,
                ),
                (
                    "sap_" + marker * 31 + "b",
                    grant_id,
                    "workspace",
                    None,
                    actor.membership_id,
                    membership_revision,
                    stamp,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO support_sessions(
              support_session_id,source_kind,source_id,operator_id,factor_id,workspace_id,
              tenant_id,scope_code,token_hmac,status,operator_revision,factor_revision,
              absolute_expires_at,revoked_at,created_at,updated_at)
            VALUES(?,'grant',?,?,?,?,?,'workspace.diagnostics.read',?,'active',1,1,
              ?,NULL,?,?)
            """,
            (
                session_id,
                grant_id,
                REQUESTER_OPERATOR_ID,
                REQUESTER_FACTOR_ID,
                actor.workspace_id,
                actor.tenant_id,
                platform_service._support_hmac(config, support_token),
                _wire_time(now + timedelta(minutes=10)),
                stamp,
                stamp,
            ),
        )
        connection.commit()
    return support_token, grant_id, session_id


def _issue_break_glass(
    config: AppConfig, actor: WorkspaceSessionContext
) -> tuple[str, str]:
    now = datetime.now(UTC)
    stamp = _wire_time(now)
    break_glass_id = "bgs_" + "a" * 32
    support_session_id = "sps_" + "a" * 32
    token = "support_" + "a" * 50
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """
            INSERT INTO break_glass_sessions(
              break_glass_id,requester_operator_id,workspace_id,tenant_id,reason_code,
              status,expires_at,review_due_at,created_at,updated_at)
            VALUES(?,?,?,?,'service_outage','active',?,?,?,?)
            """,
            (
                break_glass_id,
                REQUESTER_OPERATOR_ID,
                actor.workspace_id,
                actor.tenant_id,
                _wire_time(now + timedelta(minutes=10)),
                _wire_time(now + timedelta(hours=24)),
                stamp,
                stamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO support_sessions(
              support_session_id,source_kind,source_id,operator_id,factor_id,workspace_id,
              tenant_id,scope_code,token_hmac,status,operator_revision,factor_revision,
              absolute_expires_at,revoked_at,created_at,updated_at)
            VALUES(?,'break_glass',?,?,?,?,?,'workspace.diagnostics.read',?,'active',1,1,
              ?,NULL,?,?)
            """,
            (
                support_session_id,
                break_glass_id,
                REQUESTER_OPERATOR_ID,
                REQUESTER_FACTOR_ID,
                actor.workspace_id,
                actor.tenant_id,
                platform_service._support_hmac(config, token),
                _wire_time(now + timedelta(minutes=10)),
                stamp,
                stamp,
            ),
        )
        connection.commit()
    return token, support_session_id


def _support_headers(token: str, *, workspace: bool = False) -> dict[str, str]:
    cookie = f"{SUPPORT_SESSION_COOKIE}={token}"
    if workspace:
        cookie += f"; {WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"
    return {"Cookie": cookie}


@pytest.mark.anyio
async def test_support_bundle_full_upload_projection_grant_and_expiry_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "control.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0122"
    with monkeypatch.context() as seed_patch:
        seed_patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    config = _config(database_url, tmp_path / "ciphertext-objects")
    actor = _owner_actor(config)
    _prepare_support_principals(config, actor)
    tls: dict[str, str | None] = {"fingerprint": None}
    transport = ASGITransport(app=_TlsScope(_app(monkeypatch, config, actor), tls))

    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        deployment_id, edge_session = await _register_agent(client, tls)
        now = datetime.now(UTC).replace(microsecond=0)
        metadata, ciphertext = _upload_metadata(
            actor,
            deployment_id,
            bundle_marker="1",
            object_marker=b"c",
            created_at=now - timedelta(seconds=2),
            expires_at=now + timedelta(minutes=30),
        )
        body = _frame(metadata, ciphertext)

        tls["fingerprint"] = None
        missing_mtls = await client.post(
            "/edge/support-bundles",
            headers=_agent_headers(deployment_id, edge_session, body),
            content=body,
        )
        assert missing_mtls.status_code == 401
        assert missing_mtls.json()["code"] == "edge.authentication_failed"

        with closing(connect_database(database_url)) as connection:
            fingerprint = str(
                connection.execute(
                    "SELECT peer_binding_sha256 FROM edge_sessions WHERE edge_deployment_id=?",
                    (deployment_id,),
                ).fetchone()[0]
            )
        tls["fingerprint"] = fingerprint
        noncanonical_body = _frame(metadata, ciphertext, canonical=False)
        noncanonical = await client.post(
            "/edge/support-bundles",
            headers=_agent_headers(deployment_id, edge_session, noncanonical_body),
            content=noncanonical_body,
        )
        assert noncanonical.status_code == 422
        assert noncanonical.json()["code"] == "edge.support_bundle_invalid"

        tampered_body = _frame(metadata, ciphertext[:-1] + b"x")
        tampered = await client.post(
            "/edge/support-bundles",
            headers=_agent_headers(deployment_id, edge_session, tampered_body),
            content=tampered_body,
        )
        assert tampered.status_code == 422
        assert tampered.json()["code"] == "edge.support_bundle_invalid"

        oversized_headers = _agent_headers(deployment_id, edge_session, b"")
        oversized_headers["Content-Length"] = "200000000"
        oversized = await client.post(
            "/edge/support-bundles", headers=oversized_headers, content=b""
        )
        assert oversized.status_code == 422
        assert oversized.json()["code"] == "edge.support_bundle_invalid"

        uploaded = await client.post(
            "/edge/support-bundles",
            headers=_agent_headers(deployment_id, edge_session, body),
            content=body,
        )
        assert uploaded.status_code == 201, uploaded.text
        assert uploaded.json()["bundle_id"] == metadata.manifest.bundle_id
        assert uploaded.json()["idempotent_replay"] is False

        replay = await client.post(
            "/edge/support-bundles",
            headers=_agent_headers(deployment_id, edge_session, body),
            content=body,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        changed_ciphertext = ciphertext[:17] + b"d" * 12 + ciphertext[-16:]
        conflicting_metadata = metadata.model_copy(
            update={"object_sha256": hashlib.sha256(changed_ciphertext).hexdigest()}
        )
        conflict_body = _frame(conflicting_metadata, changed_ciphertext)
        conflict = await client.post(
            "/edge/support-bundles",
            headers=_agent_headers(deployment_id, edge_session, conflict_body),
            content=conflict_body,
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "edge.support_bundle_conflict"

        tls["fingerprint"] = None
        workspace = await client.get(
            f"/edge/support-bundles/{metadata.manifest.bundle_id}",
            headers=_workspace_headers(key="prd125-support-read-0001"),
        )
        assert workspace.status_code == 200, workspace.text
        summary = workspace.json()["bundle"]
        assert summary["status"] == "ready"
        assert set(summary) == {
            "support_bundle_id",
            "deployment_id",
            "status",
            "revision",
            "manifest_sha256",
            "entries",
            "scan_version",
            "scan_outcome",
            "archive_size_bytes",
            "object_size_bytes",
            "approved_by_account_id",
            "approved_at",
            "expires_at",
            "created_at",
        }
        projection = workspace.text.lower()
        for forbidden in (
            "object_reference",
            "object_sha256",
            "approval_proof_sha256",
            "source_edge_session_id",
            '"path"',
        ):
            assert forbidden not in projection

        valid_token, _, _ = _issue_grant(config, actor, "1")
        collision = await client.get(
            f"/edge/support-bundles/{metadata.manifest.bundle_id}",
            headers=_support_headers(valid_token, workspace=True),
        )
        assert collision.status_code == 401
        assert collision.json()["code"] == "support.session_invalid"

        first_download = await client.get(
            f"/edge/support-bundles/{metadata.manifest.bundle_id}",
            headers=_support_headers(valid_token),
        )
        second_download = await client.get(
            f"/edge/support-bundles/{metadata.manifest.bundle_id}",
            headers=_support_headers(valid_token),
        )
        assert first_download.status_code == second_download.status_code == 200
        assert first_download.content == second_download.content == ciphertext
        assert first_download.headers["content-type"] == (
            "application/vnd.metahub.edge-support-bundle-encrypted"
        )
        assert "attachment" in first_download.headers["content-disposition"]

        with closing(connect_database(database_url)) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM edge_support_bundle_download_audits"
                ).fetchone()[0]
                == 2
            )
            object_reference = str(
                connection.execute(
                    "SELECT object_reference FROM edge_support_bundles "
                    "WHERE support_bundle_id=?",
                    (metadata.manifest.bundle_id,),
                ).fetchone()[0]
            )
        object_path = Path(config.edge_support_bundle_root).joinpath(
            *object_reference.split("/")
        )
        object_path.write_bytes(ciphertext[:-1] + b"z")
        integrity_failure = await client.get(
            f"/edge/support-bundles/{metadata.manifest.bundle_id}",
            headers=_support_headers(valid_token),
        )
        assert integrity_failure.status_code == 503
        assert integrity_failure.json()["code"] == "edge.support_bundle_integrity_failed"
        object_path.write_bytes(ciphertext)

        revoked_token, revoked_grant, revoked_session = _issue_grant(config, actor, "2")
        with closing(connect_database(database_url)) as connection:
            connection.execute(
                "UPDATE support_grants SET status='revoked',revoked_at=?,updated_at=? "
                "WHERE grant_id=?",
                (_wire_time(now), _wire_time(now), revoked_grant),
            )
            connection.commit()
        revoked = await client.get(
            f"/edge/support-bundles/{metadata.manifest.bundle_id}",
            headers=_support_headers(revoked_token),
        )
        assert revoked.status_code == 401
        assert revoked.json()["code"] == "support.session_invalid"

        expired_token, expired_grant, _ = _issue_grant(
            config, actor, "3", grant_expires_at=now - timedelta(seconds=1)
        )
        expired_grant_response = await client.get(
            f"/edge/support-bundles/{metadata.manifest.bundle_id}",
            headers=_support_headers(expired_token),
        )
        assert expired_grant_response.status_code == 401
        assert expired_grant_response.json()["code"] == "support.session_invalid"

        drift_token, drift_grant, _ = _issue_grant(config, actor, "4")
        with closing(connect_database(database_url)) as connection:
            connection.execute(
                "UPDATE support_grant_approvals SET authority_revision=2 "
                "WHERE grant_id=? AND approval_domain='platform'",
                (drift_grant,),
            )
            connection.commit()
        approval_drift = await client.get(
            f"/edge/support-bundles/{metadata.manifest.bundle_id}",
            headers=_support_headers(drift_token),
        )
        assert approval_drift.status_code == 401
        assert approval_drift.json()["code"] == "support.session_invalid"

        workspace_drift_token, _, _ = _issue_grant(config, actor, "5")
        with closing(connect_database(database_url)) as connection:
            connection.execute(
                "UPDATE workspaces SET revision=revision+1,updated_at=? WHERE workspace_id=?",
                (_wire_time(now), actor.workspace_id),
            )
            connection.commit()
        workspace_drift = await client.get(
            f"/edge/support-bundles/{metadata.manifest.bundle_id}",
            headers=_support_headers(workspace_drift_token),
        )
        assert workspace_drift.status_code == 401
        assert workspace_drift.json()["code"] == "support.session_invalid"

        break_glass_token, _ = _issue_break_glass(config, actor)
        break_glass = await client.get(
            f"/edge/support-bundles/{metadata.manifest.bundle_id}",
            headers=_support_headers(break_glass_token),
        )
        assert break_glass.status_code == 403
        assert break_glass.json()["code"] == "support.scope_denied"

        tls["fingerprint"] = fingerprint
        expiring_metadata, expiring_ciphertext = _upload_metadata(
            actor,
            deployment_id,
            bundle_marker="2",
            object_marker=b"e",
            created_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=5),
        )
        expiring_body = _frame(expiring_metadata, expiring_ciphertext)
        expiring_upload = await client.post(
            "/edge/support-bundles",
            headers=_agent_headers(deployment_id, edge_session, expiring_body),
            content=expiring_body,
        )
        assert expiring_upload.status_code == 201, expiring_upload.text
        with closing(connect_database(database_url)) as connection:
            expiring_reference = str(
                connection.execute(
                    "SELECT object_reference FROM edge_support_bundles "
                    "WHERE support_bundle_id=?",
                    (expiring_metadata.manifest.bundle_id,),
                ).fetchone()[0]
            )
        expiring_path = Path(config.edge_support_bundle_root).joinpath(
            *expiring_reference.split("/")
        )
        assert expiring_path.is_file()

        reaper_metadata, reaper_ciphertext = _upload_metadata(
            actor,
            deployment_id,
            bundle_marker="3",
            object_marker=b"r",
            created_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=4),
        )
        reaper_body = _frame(reaper_metadata, reaper_ciphertext)
        reaper_upload = await client.post(
            "/edge/support-bundles",
            headers=_agent_headers(deployment_id, edge_session, reaper_body),
            content=reaper_body,
        )
        assert reaper_upload.status_code == 201, reaper_upload.text
        with closing(connect_database(database_url)) as connection:
            reaper_reference = str(
                connection.execute(
                    "SELECT object_reference FROM edge_support_bundles "
                    "WHERE support_bundle_id=?",
                    (reaper_metadata.manifest.bundle_id,),
                ).fetchone()[0]
            )
        reaper_path = Path(config.edge_support_bundle_root).joinpath(
            *reaper_reference.split("/")
        )
        assert reaper_path.is_file()
        assert (
            support_bundle_service.reap_expired_support_bundles(
                config, now=now + timedelta(minutes=4, seconds=30), limit=1
            )
            == 1
        )
        assert not reaper_path.exists()
        with closing(connect_database(database_url)) as connection:
            reaped = connection.execute(
                "SELECT status,revision FROM edge_support_bundles WHERE support_bundle_id=?",
                (reaper_metadata.manifest.bundle_id,),
            ).fetchone()
            assert tuple(reaped) == ("deleted", 3)

        ttl_token, _, _ = _issue_grant(config, actor, "6")
        after_expiry = now + timedelta(minutes=6)
        monkeypatch.setattr(support_bundle_service, "_utcnow", lambda: after_expiry)
        tls["fingerprint"] = None
        expired_metadata = await client.get(
            f"/edge/support-bundles/{expiring_metadata.manifest.bundle_id}",
            headers=_workspace_headers(key="prd125-support-expired-read-0001"),
        )
        assert expired_metadata.status_code == 200
        assert expired_metadata.json()["bundle"]["status"] == "expired"
        assert expired_metadata.json()["bundle"]["revision"] == 2
        assert not expiring_path.exists()

        expired_download = await client.get(
            f"/edge/support-bundles/{expiring_metadata.manifest.bundle_id}",
            headers=_support_headers(ttl_token),
        )
        assert expired_download.status_code == 410
        assert expired_download.json()["code"] == "edge.support_bundle_expired"

    with closing(connect_database(database_url)) as connection:
        assert (
            connection.execute(
                "SELECT status FROM support_sessions WHERE support_session_id=?",
                (revoked_session,),
            ).fetchone()[0]
            == "revoked"
        )
        assert (
            connection.execute(
                "SELECT status FROM support_grants WHERE grant_id=?", (expired_grant,)
            ).fetchone()[0]
            == "expired"
        )
        assert connection.execute("SELECT COUNT(*) FROM edge_support_bundles").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM edge_delivery_events").fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
