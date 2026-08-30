"""PRD-136 release attestation, vulnerability gate, and dual-control exception API."""

from __future__ import annotations

import base64
import secrets
import shutil
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.models import AuthSession, AuthTenant, AuthUser
from metahub_control.auth.service import AuthContext
from metahub_control.developer_contracts import service as developer_service
from metahub_control.developer_contracts.routes import (
    require_platform_contract,
)
from metahub_control.developer_contracts.routes import (
    router as developer_router,
)
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.supply_chain_security import routes, service
from metahub_control.supply_chain_security.crypto import AttestationSignature
from metahub_control.supply_chain_security.models import PatchReleaseCommand
from metahub_control.supply_chain_security.routes import router as supply_chain_router
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
BUILDER_TOKEN = "b" * 43
APPROVER_TOKEN = "a" * 43
PLATFORM_CSRF = "x" * 43
BUILDER_ID = "pto_" + "d" * 32
APPROVER_ID = "pto_" + "e" * 32
SECOND_ACCOUNT_ID = "gac_" + "f" * 32


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
    database = tmp_path_factory.mktemp("prd136-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0128"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path
) -> tuple[AppConfig, PlatformContext, PlatformContext, AuthContext, str]:
    database = tmp_path / "prd136.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    stamp = "2026-08-31T00:00:00.000Z"
    with closing(connect_database(config.database_url)) as connection:
        primary_account = str(
            connection.execute(
                "SELECT account_id FROM global_accounts ORDER BY account_id LIMIT 1"
            ).fetchone()[0]
        )
        tenant = connection.execute(
            "SELECT tenant_id,slug,name FROM tenants ORDER BY tenant_id LIMIT 1"
        ).fetchone()
        user = connection.execute(
            "SELECT user_id,email,display_name FROM users ORDER BY user_id LIMIT 1"
        ).fetchone()
        workspace_id = str(
            connection.execute(
                "SELECT workspace_id FROM workspace_tenant_bindings ORDER BY workspace_id LIMIT 1"
            ).fetchone()[0]
        )
        assert tenant is not None and user is not None
        connection.execute(
            """INSERT INTO global_accounts(
                 account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                 credential_revision,session_revision,mfa_state,failed_login_count,
                 locked_until,last_login_at,created_at,updated_at)
               VALUES(?,'supply-chain-approver@example.invalid',NULL,'Supply chain approver',
                 'active',0,1,1,'not_enrolled',0,NULL,NULL,?,?)""",
            (SECOND_ACCOUNT_ID, stamp, stamp),
        )
        connection.executemany(
            """INSERT INTO platform_operators(
                 operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
               VALUES(?,?,'active',1,NULL,?,?)""",
            (
                (BUILDER_ID, primary_account, stamp, stamp),
                (APPROVER_ID, SECOND_ACCOUNT_ID, stamp, stamp),
            ),
        )
        connection.executemany(
            """INSERT INTO platform_operator_role_bindings(
                 operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
               VALUES(?,?,'active',NULL,?,NULL)""",
            ((BUILDER_ID, "release_manager", stamp), (APPROVER_ID, "platform_owner", stamp)),
        )
        connection.commit()
    expires = datetime.now(UTC) + timedelta(hours=1)
    builder = PlatformContext(
        platform_session_id="pts_" + "4" * 32,
        operator_id=BUILDER_ID,
        account_id=primary_account,
        global_session_id="gss_" + "4" * 32,
        factor_id="mfa_" + "4" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("release_manager",),
        permissions=(),
        expires_at=expires,
    )
    approver = PlatformContext(
        platform_session_id="pts_" + "5" * 32,
        operator_id=APPROVER_ID,
        account_id=SECOND_ACCOUNT_ID,
        global_session_id="gss_" + "5" * 32,
        factor_id="mfa_" + "5" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=(),
        expires_at=expires,
    )
    developer = AuthContext(
        user=AuthUser(
            user_id=str(user["user_id"]),
            tenant_id=str(tenant["tenant_id"]),
            email=str(user["email"]),
            display_name=str(user["display_name"]),
            role="developer",
            permission_codes=("developer_portal.read",),
        ),
        tenant=AuthTenant(
            tenant_id=str(tenant["tenant_id"]), slug=str(tenant["slug"]), name=str(tenant["name"])
        ),
        session=AuthSession(session_id="ses_" + "6" * 32, expires_at=expires),
        token="developer-supply-chain-token",
        token_jti="jti_" + "6" * 32,
    )
    return config, builder, approver, developer, workspace_id


def _app(monkeypatch, config, builder, approver, developer):
    def platform_actor(connection, _config, *, session_token, **_kwargs):
        context = builder if session_token == BUILDER_TOKEN else approver
        roles, permissions, _fingerprint = platform_access_service._authority(
            connection, context.operator_id
        )
        return replace(context, roles=roles, permissions=permissions)

    def platform_reader(_config, session_token):
        with closing(connect_database(config.database_url)) as connection:
            return platform_actor(connection, config, session_token=session_token)

    monkeypatch.setattr(
        developer_service, "authenticate_platform_session_in_transaction", platform_actor
    )
    monkeypatch.setattr(service, "authenticate_platform_session_in_transaction", platform_actor)
    monkeypatch.setattr(routes, "authenticate_platform_session_context", platform_reader)
    monkeypatch.setattr(routes, "require_developer_portal_reader", lambda *_args: developer)
    app = create_service_app(
        "control-plane-api",
        "PRD-136 Supply Chain Test",
        extra_cors_allow_headers=("Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(developer_router)
    app.include_router(supply_chain_router)
    app.dependency_overrides[require_platform_contract] = lambda: builder
    return app


def _platform_headers(token: str, key: str | None = None) -> dict[str, str]:
    headers = {"Cookie": f"{PLATFORM_SESSION_COOKIE}={token}"}
    if key is not None:
        headers.update(
            {
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={token}; {PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
                ),
                "Idempotency-Key": key,
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-site",
                "X-CSRF-Token": PLATFORM_CSRF,
            }
        )
    return headers


async def _build(
    client: AsyncClient,
    *,
    version: str = "13.6.0",
    producer_revision: int = 50,
    idempotency_key: str = "prd136-build-release-0001",
) -> dict[str, object]:
    built = await client.post(
        "/platform/developer-contracts/build",
        headers=_platform_headers(BUILDER_TOKEN, idempotency_key),
        json={
            "api_version": version,
            "python_sdk_version": version,
            "javascript_sdk_version": version,
            "producer_revision": producer_revision,
            "confirmed": True,
        },
    )
    assert built.status_code == 201, built.text
    return built.json()


@pytest.mark.anyio
async def test_attestation_reports_scan_and_dual_control_exception(
    runtime: tuple[AppConfig, PlatformContext, PlatformContext, AuthContext, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, builder, approver, developer, workspace_id = runtime
    app = _app(monkeypatch, config, builder, approver, developer)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        built = await _build(client)
        api_version_id = built["api_version"]["api_version_id"]
        app.state.supply_chain_signer = lambda _payload: AttestationSignature(
            key_id="invalid-supply-chain-v1", public_key=b"k" * 32, signature=b"s" * 64
        )
        invalid = await client.post(
            f"/platform/developer-contracts/{api_version_id}/approve",
            headers=_platform_headers(APPROVER_TOKEN, "prd136-invalid-attestation-0001"),
            json={"expected_revision": 1, "confirmed": True},
        )
        assert invalid.status_code == 503
        assert invalid.json()["code"] == "supply_chain.kms_unavailable"
        app.state.supply_chain_signer = None
        approved = await client.post(
            f"/platform/developer-contracts/{api_version_id}/approve",
            headers=_platform_headers(APPROVER_TOKEN, "prd136-approve-release-0001"),
            json={"expected_revision": 1, "confirmed": True},
        )
        assert approved.status_code == 200, approved.text
        releases = approved.json()["sdk_releases"]
        assert len(releases) == 2
        release_id = releases[0]["sdk_release_id"]

        report = await client.get(
            f"/releases/{release_id}/security-report",
            headers={"Authorization": "Bearer developer-supply-chain-token"},
        )
        assert report.status_code == 200, report.text
        body = report.json()["report"]
        assert body["gate_outcome"] == "passed"
        assert set(body) == {
            "sdk_release_id",
            "language",
            "sdk_version",
            "artifact_sha256",
            "gate_outcome",
            "blocking_finding_count",
            "sbom",
            "signature",
            "vulnerabilities",
            "generated_at",
        }
        assert "repository_url" not in report.text
        signature = body["signature"]
        payload = canonical_json_bytes(
            {
                "artifact_sha256": signature["artifact_sha256"],
                "provenance_sha256": signature["provenance_sha256"],
                "purpose": "metahub.artifact.attestation/v1",
                "sbom_sha256": signature["sbom_sha256"],
                "sdk_release_id": release_id,
            }
        )
        Ed25519PublicKey.from_public_bytes(
            base64.urlsafe_b64decode(signature["public_key_b64"] + "=")
        ).verify(base64.urlsafe_b64decode(signature["signature_b64"] + "=="), payload)

        provenance = await client.get(
            f"/releases/{release_id}/provenance",
            headers=_platform_headers(BUILDER_TOKEN),
        )
        assert provenance.status_code == 200, provenance.text
        assert provenance.json()["provenance"]["builder_verified"] is True
        assert "environment" not in provenance.json()["provenance"]

        with pytest.raises(Exception) as arbitrary_input:
            service.record_vulnerability_scan(
                config,
                release_id,
                (
                    {
                        "component_name": "https://internal.invalid/feed",
                        "component_version": "1.0.0",
                        "advisory_code": "CVE-2026-0001",
                        "severity": "critical",
                        "fixed_version_code": "1.0.1",
                    },
                ),
                scanner_version="1.0.0",
                request_id="prd136-invalid-scan",
                trace_id="prd136-invalid-scan",
            )
        assert arbitrary_input.value.code == "supply_chain.scan_invalid"
        with pytest.raises(Exception) as missing_feed:
            service.record_vulnerability_scan(
                config,
                release_id,
                (),
                scanner_version="1.0.0",
                feed_version="missing",
                request_id="prd136-missing-feed",
                trace_id="prd136-missing-feed",
            )
        assert missing_feed.value.code == "supply_chain.feed_invalid"
        finding_id = service.record_vulnerability_scan(
            config,
            release_id,
            (
                {
                    "component_name": "locked-dependency",
                    "component_version": "1.0.0",
                    "advisory_code": "CVE-2026-0001",
                    "severity": "critical",
                    "fixed_version_code": "1.0.1",
                },
            ),
            scanner_version="1.0.0",
            request_id="prd136-critical-scan",
            trace_id="prd136-critical-scan",
        )[0]
        blocked = await client.get(
            f"/releases/{release_id}/security-report",
            headers={"Authorization": "Bearer developer-supply-chain-token"},
        )
        assert blocked.status_code == 200
        assert blocked.json()["report"]["gate_outcome"] == "blocked"
        assert blocked.json()["report"]["blocking_finding_count"] == 1
        assert "locked-dependency" not in blocked.text

        starts = datetime.now(UTC) + timedelta(minutes=1)
        expires = starts + timedelta(days=7)
        requested = await client.post(
            f"/platform/vulnerabilities/{finding_id}/exceptions",
            headers=_platform_headers(APPROVER_TOKEN, "prd136-exception-request-0001"),
            json={
                "operation": "request",
                "workspace_id": workspace_id,
                "reason_code": "vendor_fix_pending",
                "compensating_controls": ["network_isolation", "runtime_detection"],
                "starts_at": starts.isoformat(),
                "expires_at": expires.isoformat(),
                "confirmed": True,
            },
        )
        assert requested.status_code == 200, requested.text
        exception = requested.json()["exception"]
        self_approval = await client.post(
            f"/platform/vulnerabilities/{finding_id}/exceptions",
            headers=_platform_headers(APPROVER_TOKEN, "prd136-exception-self-0001"),
            json={
                "operation": "approve",
                "security_exception_id": exception["security_exception_id"],
                "expected_revision": 1,
                "confirmed": True,
            },
        )
        assert self_approval.status_code == 403
        assert self_approval.json()["code"] == "supply_chain.self_approval_denied"
        accepted = await client.post(
            f"/platform/vulnerabilities/{finding_id}/exceptions",
            headers=_platform_headers(BUILDER_TOKEN, "prd136-exception-approve-0001"),
            json={
                "operation": "approve",
                "security_exception_id": exception["security_exception_id"],
                "expected_revision": 1,
                "confirmed": True,
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["exception"]["status"] == "active"

        fixed_private_key = Ed25519PrivateKey.generate()

        def fixed_signer(payload: bytes) -> AttestationSignature:
            return AttestationSignature(
                key_id="supply-chain-fixed-v1",
                public_key=fixed_private_key.public_key().public_bytes_raw(),
                signature=fixed_private_key.sign(payload),
            )

        app.state.supply_chain_signer = fixed_signer
        fixed_built = await _build(
            client,
            version="13.6.1",
            producer_revision=51,
            idempotency_key="prd136-build-fixed-release-0001",
        )
        fixed_approved = await client.post(
            f"/platform/developer-contracts/{fixed_built['api_version']['api_version_id']}/approve",
            headers=_platform_headers(APPROVER_TOKEN, "prd136-approve-fixed-release-0001"),
            json={"expected_revision": 1, "confirmed": True},
        )
        assert fixed_approved.status_code == 200, fixed_approved.text
        fixed_release = next(
            item
            for item in fixed_approved.json()["sdk_releases"]
            if item["language"] == releases[0]["language"]
        )
        app.state.supply_chain_signer = None

        compatibility = {
            "cloud": {"n": "passed", "n_minus_1": "passed"},
            "edge": {"n": "passed", "n_minus_1": "passed"},
        }
        proposed = service.apply_patch_release(
            config,
            finding_id,
            PatchReleaseCommand.model_validate(
                {
                    "operation": "propose",
                    "workspace_id": workspace_id,
                    "fixed_sdk_release_id": fixed_release["sdk_release_id"],
                    "compatibility_matrix": compatibility,
                    "rollback_artifact_sha256": body["artifact_sha256"],
                    "confirmed": True,
                }
            ),
            platform_session_token=BUILDER_TOKEN,
            platform_csrf_token=PLATFORM_CSRF,
            idempotency_key="prd136-patch-propose-0001",
            request_id="prd136-patch-propose",
            trace_id="prd136-patch-propose",
        )
        patch_id = proposed.patch_release.patch_release_id
        with pytest.raises(Exception) as patch_self_approval:
            service.apply_patch_release(
                config,
                finding_id,
                PatchReleaseCommand.model_validate(
                    {
                        "operation": "approve",
                        "patch_release_id": patch_id,
                        "expected_revision": 1,
                        "confirmed": True,
                    }
                ),
                platform_session_token=BUILDER_TOKEN,
                platform_csrf_token=PLATFORM_CSRF,
                idempotency_key="prd136-patch-self-approve-0001",
                request_id="prd136-patch-self-approve",
                trace_id="prd136-patch-self-approve",
            )
        assert patch_self_approval.value.code == "supply_chain.self_approval_denied"
        approve_command = PatchReleaseCommand.model_validate(
            {
                "operation": "approve",
                "patch_release_id": patch_id,
                "expected_revision": 1,
                "confirmed": True,
            }
        )
        patch_approved = service.apply_patch_release(
            config,
            finding_id,
            approve_command,
            platform_session_token=APPROVER_TOKEN,
            platform_csrf_token=PLATFORM_CSRF,
            idempotency_key="prd136-patch-approve-0001",
            request_id="prd136-patch-approve",
            trace_id="prd136-patch-approve",
        )
        assert patch_approved.patch_release.status == "approved"
        patch_replay = service.apply_patch_release(
            config,
            finding_id,
            approve_command,
            platform_session_token=APPROVER_TOKEN,
            platform_csrf_token=PLATFORM_CSRF,
            idempotency_key="prd136-patch-approve-0001",
            request_id="prd136-patch-approve-replay",
            trace_id="prd136-patch-approve-replay",
        )
        assert patch_replay.idempotent_replay is True
        released_patch = service.apply_patch_release(
            config,
            finding_id,
            PatchReleaseCommand.model_validate(
                {
                    "operation": "release",
                    "patch_release_id": patch_id,
                    "expected_revision": 2,
                    "confirmed": True,
                }
            ),
            platform_session_token=BUILDER_TOKEN,
            platform_csrf_token=PLATFORM_CSRF,
            idempotency_key="prd136-patch-release-0001",
            request_id="prd136-patch-release",
            trace_id="prd136-patch-release",
        )
        assert released_patch.patch_release.status == "released"
        patched_report = await client.get(
            f"/releases/{release_id}/security-report",
            headers={"Authorization": "Bearer developer-supply-chain-token"},
        )
        finding = patched_report.json()["report"]["vulnerabilities"][0]
        assert finding["exceptions"][0]["status"] == "active"
        assert finding["patch_releases"][0]["status"] == "released"

        with closing(connect_database(config.database_url)) as connection:
            connection.execute(
                "UPDATE supply_chain_signing_keys SET status='revoked' WHERE key_id=?",
                ("supply-chain-fixed-v1",),
            )
            connection.commit()
        rolled_back_patch = service.apply_patch_release(
            config,
            finding_id,
            PatchReleaseCommand.model_validate(
                {
                    "operation": "rollback",
                    "patch_release_id": patch_id,
                    "expected_revision": 3,
                    "confirmed": True,
                }
            ),
            platform_session_token=BUILDER_TOKEN,
            platform_csrf_token=PLATFORM_CSRF,
            idempotency_key="prd136-patch-rollback-0001",
            request_id="prd136-patch-rollback",
            trace_id="prd136-patch-rollback",
        )
        assert rolled_back_patch.patch_release.status == "rolled_back"

        assert (
            service.record_vulnerability_scan(
                config,
                release_id,
                (),
                scanner_version="1.0.1",
                request_id="prd136-clear-scan",
                trace_id="prd136-clear-scan",
            )
            == ()
        )
        cleared = await client.get(
            f"/releases/{release_id}/security-report",
            headers={"Authorization": "Bearer developer-supply-chain-token"},
        )
        assert cleared.status_code == 200
        assert cleared.json()["report"]["gate_outcome"] == "passed"
        assert cleared.json()["report"]["vulnerabilities"] == []
        with closing(connect_database(config.database_url)) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM supply_chain_security_exceptions "
                    "WHERE security_exception_id=?",
                    (exception["security_exception_id"],),
                ).fetchone()[0]
                == "revoked"
            )

        conflict = await client.get(
            f"/releases/{release_id}/security-report",
            headers={
                "Authorization": "Bearer developer-supply-chain-token",
                "Cookie": f"{PLATFORM_SESSION_COOKIE}={BUILDER_TOKEN}",
            },
        )
        assert conflict.status_code == 400
        assert conflict.json()["code"] == "supply_chain.credential_conflict"

        with closing(connect_database(config.database_url)) as connection:
            connection.execute(
                "UPDATE supply_chain_signing_keys SET status='revoked' WHERE key_id=?",
                (signature["key_id"],),
            )
            connection.commit()
        revoked = await client.get(
            f"/releases/{release_id}/security-report",
            headers={"Authorization": "Bearer developer-supply-chain-token"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["report"]["gate_outcome"] == "attestation_invalid"
        assert revoked.json()["report"]["signature"]["verification_outcome"] == "revoked"
        revoked_provenance = await client.get(
            f"/releases/{release_id}/provenance",
            headers={"Authorization": "Bearer developer-supply-chain-token"},
        )
        assert revoked_provenance.status_code == 200
        assert revoked_provenance.json()["provenance"]["builder_verified"] is False

    assert service.process_supply_chain_once(config, worker_id="prd136-test-worker") > 0
    with closing(connect_database(config.database_url)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM release_artifact_signatures").fetchone()[0]
            == 4
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM supply_chain_event_outbox WHERE status='delivered'"
            ).fetchone()[0]
            >= 7
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
async def test_strict_release_read_rejects_unknown_and_body(
    runtime: tuple[AppConfig, PlatformContext, PlatformContext, AuthContext, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, builder, approver, developer, _workspace_id = runtime
    app = _app(monkeypatch, config, builder, approver, developer)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unknown = await client.get(
            f"/releases/sdr_{'0' * 32}/security-report",
            headers={"Authorization": "Bearer developer-supply-chain-token"},
        )
        assert unknown.status_code == 404
        body = await client.request(
            "GET",
            f"/releases/sdr_{'0' * 32}/provenance",
            headers={
                "Authorization": "Bearer developer-supply-chain-token",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
        assert body.status_code == 400
        assert body.json()["code"] == "supply_chain.request_invalid"
