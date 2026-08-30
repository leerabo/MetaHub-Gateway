"""PRD-133 dual-control release, public reads, downloads, and sunset gates."""

from __future__ import annotations

import base64
import secrets
import shutil
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.models import AuthSession, AuthTenant, AuthUser
from metahub_control.auth.service import AuthContext
from metahub_control.developer_contracts import service
from metahub_control.developer_contracts.crypto import ExternalSignature
from metahub_control.developer_contracts.routes import (
    require_platform_contract,
    router,
)
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.developer_contracts import sdk_signature_payload
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
BUILDER_TOKEN = "b" * 43
APPROVER_TOKEN = "a" * 43
PLATFORM_CSRF = "x" * 43
BUILDER_ID = "pto_" + "a" * 32
APPROVER_ID = "pto_" + "b" * 32
SECOND_ACCOUNT_ID = "gac_" + "c" * 32
_TEST_PASSWORD = f"Prd133!Aa9{secrets.token_urlsafe(18)}"


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
    database = tmp_path_factory.mktemp("prd133-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0125"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path
) -> tuple[AppConfig, PlatformContext, PlatformContext, AuthContext]:
    database = tmp_path / "prd133.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    now = "2026-08-29T08:00:00.000Z"
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
        assert tenant is not None and user is not None
        connection.execute(
            """INSERT INTO global_accounts(
                 account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                 credential_revision,session_revision,mfa_state,failed_login_count,
                 locked_until,last_login_at,created_at,updated_at)
               VALUES(?,'release-approver@example.invalid',NULL,'Release approver',
                 'active',0,1,1,'not_enrolled',0,NULL,NULL,?,?)""",
            (SECOND_ACCOUNT_ID, now, now),
        )
        connection.executemany(
            """INSERT INTO platform_operators(
                 operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
               VALUES(?,?,'active',1,NULL,?,?)""",
            ((BUILDER_ID, primary_account, now, now), (APPROVER_ID, SECOND_ACCOUNT_ID, now, now)),
        )
        connection.executemany(
            """INSERT INTO platform_operator_role_bindings(
                 operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
               VALUES(?,?,'active',NULL,?,NULL)""",
            ((BUILDER_ID, "release_manager", now), (APPROVER_ID, "platform_owner", now)),
        )
        connection.commit()
    permissions = (
        "platform.developer_contract.approve",
        "platform.developer_contract.manage",
        "platform.developer_contract.read",
    )
    expires = datetime.now(UTC) + timedelta(hours=1)
    builder = PlatformContext(
        platform_session_id="pts_" + "1" * 32,
        operator_id=BUILDER_ID,
        account_id=primary_account,
        global_session_id="gss_" + "1" * 32,
        factor_id="mfa_" + "1" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("release_manager",),
        permissions=permissions,
        expires_at=expires,
    )
    approver = PlatformContext(
        platform_session_id="pts_" + "2" * 32,
        operator_id=APPROVER_ID,
        account_id=SECOND_ACCOUNT_ID,
        global_session_id="gss_" + "2" * 32,
        factor_id="mfa_" + "2" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=permissions,
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
        session=AuthSession(session_id="ses_" + "3" * 32, expires_at=expires),
        token="developer-test-token",
        token_jti="jti_" + "3" * 32,
    )
    return config, builder, approver, developer


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    builder: PlatformContext,
    approver: PlatformContext,
    developer: AuthContext,
):
    def platform_actor(connection, _config, *, session_token, **_kwargs):
        context = builder if session_token == BUILDER_TOKEN else approver
        roles, permissions, _fingerprint = platform_access_service._authority(
            connection, context.operator_id
        )
        return replace(context, roles=roles, permissions=permissions)

    monkeypatch.setattr(service, "authenticate_platform_session_in_transaction", platform_actor)
    app = create_service_app(
        "control-plane-api",
        "PRD-133 Developer Contract Test",
        extra_cors_allow_headers=("Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=(
            "Digest",
            "X-Artifact-Receipt-SHA256",
            "X-Content-Type-Options",
        ),
    )
    app.state.config = config
    app.include_router(router)
    app.dependency_overrides[require_platform_contract] = lambda: builder
    from metahub_control.auth import require_developer_portal_reader

    app.dependency_overrides[require_developer_portal_reader] = lambda: developer
    return app


def _platform_headers(token: str, key: str) -> dict[str, str]:
    return {
        "Cookie": (f"{PLATFORM_SESSION_COOKIE}={token}; {PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"),
        "Idempotency-Key": key,
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": PLATFORM_CSRF,
    }


async def _build_and_approve(
    client: AsyncClient,
    *,
    api_version: str,
    sdk_version: str,
    suffix: str,
) -> dict[str, object]:
    body = {
        "api_version": api_version,
        "python_sdk_version": sdk_version,
        "javascript_sdk_version": sdk_version,
        "producer_revision": 49,
        "confirmed": True,
    }
    built = await client.post(
        "/platform/developer-contracts/build",
        headers=_platform_headers(BUILDER_TOKEN, f"prd133-build-{suffix}-0001"),
        json=body,
    )
    assert built.status_code == 201, built.text
    built_body = built.json()
    assert built_body["api_version"]["status"] == "pending_approval"
    assert len(built_body["sdk_releases"]) == 2
    approved = await client.post(
        f"/platform/developer-contracts/{built_body['api_version']['api_version_id']}/approve",
        headers=_platform_headers(APPROVER_TOKEN, f"prd133-approve-{suffix}-0001"),
        json={"expected_revision": 1, "confirmed": True},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["api_version"]["status"] == "published"
    return approved.json()


@pytest.mark.anyio
async def test_release_download_deprecation_scheduler_and_withdrawal(
    runtime: tuple[AppConfig, PlatformContext, PlatformContext, AuthContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, builder, approver, developer = runtime
    app = _app(monkeypatch, config, builder, approver, developer)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        build_body = {
            "api_version": "1.0.0",
            "python_sdk_version": "1.0.0",
            "javascript_sdk_version": "1.0.0",
            "producer_revision": 49,
            "confirmed": True,
        }
        built = await client.post(
            "/platform/developer-contracts/build",
            headers=_platform_headers(BUILDER_TOKEN, "prd133-build-first-0001"),
            json=build_body,
        )
        assert built.status_code == 201, built.text
        first_id = built.json()["api_version"]["api_version_id"]
        replay = await client.post(
            "/platform/developer-contracts/build",
            headers=_platform_headers(BUILDER_TOKEN, "prd133-build-first-0001"),
            json=build_body,
        )
        assert replay.status_code == 201
        assert replay.json()["idempotent_replay"] is True
        idempotency_conflict = await client.post(
            "/platform/developer-contracts/build",
            headers=_platform_headers(BUILDER_TOKEN, "prd133-build-first-0001"),
            json={**build_body, "producer_revision": 50},
        )
        assert idempotency_conflict.status_code == 409
        assert idempotency_conflict.json()["code"] == "developer_contract.idempotency_conflict"
        self_approval = await client.post(
            f"/platform/developer-contracts/{first_id}/approve",
            headers=_platform_headers(BUILDER_TOKEN, "prd133-self-approve-0001"),
            json={"expected_revision": 1, "confirmed": True},
        )
        assert self_approval.status_code == 403
        assert self_approval.json()["code"] == "developer_contract.self_approval_denied"
        with closing(connect_database(config.database_url)) as connection:
            connection.execute(
                """UPDATE platform_operator_role_bindings
                      SET status='revoked',revoked_at=datetime('now')
                    WHERE operator_id=? AND role_code='platform_owner' AND status='active'""",
                (APPROVER_ID,),
            )
            connection.commit()
        dynamically_revoked = await client.post(
            f"/platform/developer-contracts/{first_id}/approve",
            headers=_platform_headers(APPROVER_TOKEN, "prd133-approve-revoked-0001"),
            json={"expected_revision": 1, "confirmed": True},
        )
        assert dynamically_revoked.status_code == 403
        assert dynamically_revoked.json()["code"] == "platform.permission_denied"
        with closing(connect_database(config.database_url)) as connection:
            connection.execute(
                """UPDATE platform_operator_role_bindings
                      SET status='active',revoked_at=NULL
                    WHERE operator_id=? AND role_code='platform_owner' AND status='revoked'""",
                (APPROVER_ID,),
            )
            connection.commit()
        app.state.developer_contract_signer = lambda _payload: ExternalSignature(
            key_id="invalid-test-v1", public_key=b"k" * 32, signature=b"s" * 64
        )
        invalid_signature = await client.post(
            f"/platform/developer-contracts/{first_id}/approve",
            headers=_platform_headers(APPROVER_TOKEN, "prd133-approve-invalid-kms-0001"),
            json={"expected_revision": 1, "confirmed": True},
        )
        assert invalid_signature.status_code == 503
        assert invalid_signature.json()["code"] == "developer_contract.kms_unavailable"
        app.state.developer_contract_signer = None
        approved = await client.post(
            f"/platform/developer-contracts/{first_id}/approve",
            headers=_platform_headers(APPROVER_TOKEN, "prd133-approve-first-0001"),
            json={"expected_revision": 1, "confirmed": True},
        )
        assert approved.status_code == 200, approved.text
        first_release = approved.json()
        assert first_release["api_version"]["status"] == "published"
        for item in first_release["sdk_releases"]:
            signature = base64.urlsafe_b64decode(item["signature_b64"] + "==")
            public_key = base64.urlsafe_b64decode(item["public_key_b64"] + "=")
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature,
                sdk_signature_payload(
                    language=item["language"],
                    sdk_version=item["sdk_version"],
                    contract_sha256=item["contract_sha256"],
                    artifact_sha256=item["artifact_sha256"],
                ),
            )
            assert item["signature_sha256"]

        workspace = await client.get(
            "/platform/developer-contracts",
            headers={"Cookie": f"{PLATFORM_SESSION_COOKIE}={BUILDER_TOKEN}"},
        )
        assert workspace.status_code == 200, workspace.text
        assert workspace.headers["cache-control"] == "no-store, private"
        assert set(workspace.json()) == {
            "versions",
            "sdk_releases",
            "deprecations",
            "request_id",
            "trace_id",
        }
        assert workspace.json()["versions"][0]["version"] == "1.0.0"

        versions = await client.get("/developer/api-versions")
        assert versions.status_code == 200
        assert [item["version"] for item in versions.json()["versions"]] == ["1.0.0"]
        schemas = await client.get("/developer/webhook-schemas")
        assert schemas.status_code == 200
        assert len(schemas.json()["schemas"]) == 4
        sdks = await client.get("/developer/sdks/python")
        assert sdks.status_code == 200
        download_url = sdks.json()["releases"][0]["download_url"]
        downloaded = await client.get(download_url, headers={"Origin": "http://localhost:5173"})
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b"PK")
        assert downloaded.headers["cache-control"] == "no-store, private"
        assert downloaded.headers["x-artifact-receipt-sha256"]
        exposed = downloaded.headers["access-control-expose-headers"].lower()
        assert "content-disposition" in exposed
        assert "digest" in exposed
        assert "x-artifact-receipt-sha256" in exposed
        with monkeypatch.context() as corrupt:
            corrupt.setattr(service, "build_sdk_archive", lambda *args, **kwargs: (b"bad", {}))
            corrupted = await client.get(download_url)
        assert corrupted.status_code == 503
        assert corrupted.json()["code"] == "developer_contract.artifact_corrupt"

        second_release = await _build_and_approve(
            client, api_version="1.1.0", sdk_version="1.1.0", suffix="second"
        )
        second_id = second_release["api_version"]["api_version_id"]
        sunset_at = datetime.now(UTC) + timedelta(days=91)
        notice = await client.post(
            "/platform/developer-contracts/deprecations",
            headers=_platform_headers(BUILDER_TOKEN, "prd133-notice-create-0001"),
            json={
                "api_version_id": first_id,
                "replacement_api_version_id": second_id,
                "affected_operations": ["developer.api_versions.list"],
                "migration_guide_code": "api-v1-to-v1.1",
                "minimum_notice_days": 90,
                "sunset_at": sunset_at.isoformat(),
                "confirmed": True,
            },
        )
        assert notice.status_code == 201, notice.text
        notice_id = notice.json()["deprecation"]["deprecation_notice_id"]
        announced = await client.post(
            f"/platform/developer-contracts/deprecations/{notice_id}/announce",
            headers=_platform_headers(APPROVER_TOKEN, "prd133-notice-announce-0001"),
            json={"expected_revision": 1, "confirmed": True},
        )
        assert announced.status_code == 200, announced.text
        assert announced.json()["api_version"]["status"] == "deprecated"

        future = sunset_at + timedelta(minutes=1)
        monkeypatch.setattr(service, "_utcnow", lambda: future)
        assert service.schedule_due_deprecations(config, worker_id="prd133-scheduler-test") == 1
        blocked_sunset = await client.post(
            f"/platform/developer-contracts/deprecations/{notice_id}/sunset",
            headers=_platform_headers(BUILDER_TOKEN, "prd133-notice-sunset-blocked-0001"),
            json={
                "expected_revision": 3,
                "notification_watermark": 0,
                "adoption_watermark": 12,
                "confirmed": True,
            },
        )
        assert blocked_sunset.status_code == 409
        assert blocked_sunset.json()["code"] == "developer_contract.sunset_gate_failed"
        sunset = await client.post(
            f"/platform/developer-contracts/deprecations/{notice_id}/sunset",
            headers=_platform_headers(BUILDER_TOKEN, "prd133-notice-sunset-0001"),
            json={
                "expected_revision": 3,
                "notification_watermark": 4,
                "adoption_watermark": 12,
                "confirmed": True,
            },
        )
        assert sunset.status_code == 200, sunset.text
        assert sunset.json()["api_version"]["status"] == "sunset"

        javascript = next(
            item for item in second_release["sdk_releases"] if item["language"] == "javascript"
        )
        withdrawn = await client.post(
            f"/platform/developer-contracts/sdks/{javascript['sdk_release_id']}/withdraw",
            headers=_platform_headers(BUILDER_TOKEN, "prd133-sdk-withdraw-0001"),
            json={"expected_revision": 2, "confirmed": True},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["sdk_release"]["status"] == "withdrawn"

        full_changelog = await client.get("/developer/changelog?limit=100")
        assert full_changelog.status_code == 200
        deprecated_entry = next(
            entry
            for entry in full_changelog.json()["entries"]
            if entry["event_type"] == "api.deprecated"
        )
        assert deprecated_entry["metadata"]["migration_guide_code"] == "api-v1-to-v1.1"

        changelog = await client.get("/developer/changelog?limit=1")
        assert changelog.status_code == 200
        assert len(changelog.json()["entries"]) == 1
        cursor = changelog.json()["next_cursor"]
        assert cursor
        next_page = await client.get(f"/developer/changelog?limit=1&cursor={cursor}")
        assert next_page.status_code == 200
        assert (
            next_page.json()["entries"][0]["event_id"] != changelog.json()["entries"][0]["event_id"]
        )
        invalid_cursor = await client.get("/developer/changelog?cursor=not-a-signed-cursor")
        assert invalid_cursor.status_code == 422
        assert invalid_cursor.json()["code"] == "developer_contract.request_invalid"

    with closing(connect_database(config.database_url)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM developer_contract_download_audits"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM api_contract_snapshots WHERE status='published'"
            ).fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT COUNT(*) FROM webhook_schema_versions").fetchone()[0] == 4
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM developer_contract_event_outbox WHERE status='pending'"
            ).fetchone()[0]
            >= 2
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
