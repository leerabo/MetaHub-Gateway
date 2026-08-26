"""PRD-119 signed catalog API, ownership, publication, and preflight acceptance."""

from __future__ import annotations

import base64
import copy
import math
import secrets
import shutil
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.console.routes import router as console_router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.provider_catalog import routes as catalog_routes
from metahub_control.provider_catalog import service as catalog_service
from metahub_control.provider_catalog.models import ProviderCatalogManifest
from metahub_control.provider_catalog.routes import router
from metahub_control.provider_catalog.seed import SEED_MANIFEST
from metahub_control.workspace_access import service as workspace_service
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
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43
PLATFORM_CSRF = "s" * 43
_LEGACY_TEST_PASSWORD = f"Prd119!9Aa{secrets.token_urlsafe(18)}"


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
    database = tmp_path_factory.mktemp("prd119-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0113"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _LEGACY_TEST_PASSWORD)
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path
) -> tuple[AppConfig, WorkspaceSessionContext, PlatformContext]:
    database = tmp_path / "prd119.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    actor = _owner_actor(config)
    operator_id = "pto_" + "9" * 32
    with connect_database(config.database_url) as connection:
        connection.execute(
            """
            INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,NULL,?,?)
            """,
            (
                operator_id,
                actor.account_id,
                "2026-08-26T01:00:00.000Z",
                "2026-08-26T01:00:00.000Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
            VALUES(?,'platform_owner','active',NULL,?,NULL)
            """,
            (operator_id, "2026-08-26T01:00:00.000Z"),
        )
        connection.commit()
    platform = PlatformContext(
        platform_session_id="pts_" + "9" * 32,
        operator_id=operator_id,
        account_id=actor.account_id,
        global_session_id="gss_" + "9" * 32,
        factor_id="mfa_" + "9" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=(
            "platform.provider_catalog.manage",
            "platform.provider_catalog.security_read",
        ),
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
    assert "provider_catalog.read" in snapshot.permission_codes
    assert "provider.write" in snapshot.permission_codes
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


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    actor: WorkspaceSessionContext,
    platform_holder: dict[str, PlatformContext],
):
    monkeypatch.setattr(
        workspace_service,
        "authenticate_workspace_session_as_auth_context",
        lambda *_args, **_kwargs: actor.as_auth_context(),
    )
    monkeypatch.setattr(
        catalog_routes,
        "authenticate_platform_session_context",
        lambda *_args, **_kwargs: platform_holder["actor"],
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-119 Test",
        extra_cors_allow_headers=("Idempotency-Key", "If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def _legacy_app(config: AppConfig):
    app = create_service_app(
        "control-plane-api",
        "PRD-119 Legacy Bearer Test",
        extra_cors_allow_headers=("Idempotency-Key", "If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(auth_router)
    app.include_router(console_router)
    app.include_router(router)
    return app


def _workspace_headers(*, key: str | None = None) -> dict[str, str]:
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
    return headers


def _platform_headers(*, key: str | None = None, revision: int | None = None):
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
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return headers


async def _legacy_login(client: AsyncClient) -> dict:
    response = await client.post(
        "/auth/login",
        json={
            "tenant_slug": "default",
            "email": "admin@metahub.local",
            "password": _LEGACY_TEST_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.anyio
async def test_legacy_bearer_owner_and_developer_allow_viewer_denies(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, _actor, _platform = runtime
    app = _legacy_app(config)
    with connect_database(config.database_url) as connection:
        owner = connection.execute(
            """SELECT membership.role_code
                 FROM workspace_memberships membership
                 JOIN users user ON user.user_id=membership.legacy_user_id
                WHERE user.email='admin@metahub.local'"""
        ).fetchone()
        assert owner is not None and owner["role_code"] == "owner"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        owner_login = await _legacy_login(client)
        assert owner_login["permission_codes"] == owner_login["user"]["permission_codes"]
        assert "provider_catalog.read" in owner_login["permission_codes"]
        assert (
            owner_login["authorization_fingerprint"]
            == owner_login["user"]["authorization_fingerprint"]
        )
        owner_headers = {"Authorization": f"Bearer {owner_login['access_token']}"}
        owner_me = await client.get("/auth/me", headers=owner_headers)
        owner_navigation = await client.get("/console/navigation", headers=owner_headers)
        owner_catalog = await client.get("/provider-catalog", headers=owner_headers)
        assert owner_me.status_code == 200
        assert "provider_catalog.read" in owner_me.json()["permission_codes"]
        assert owner_navigation.status_code == 200
        assert owner_navigation.json()["permission_codes"] == owner_me.json()["permission_codes"]
        assert (
            owner_navigation.json()["authorization_fingerprint"]
            == owner_me.json()["authorization_fingerprint"]
        )
        assert owner_catalog.status_code == 200, owner_catalog.text

        with connect_database(config.database_url) as connection:
            connection.execute(
                "UPDATE users SET role='developer' WHERE email='admin@metahub.local'"
            )
            connection.commit()
        developer_login = await _legacy_login(client)
        assert "provider_catalog.read" in developer_login["permission_codes"]
        developer_catalog = await client.get(
            "/provider-catalog",
            headers={"Authorization": f"Bearer {developer_login['access_token']}"},
        )
        assert developer_catalog.status_code == 200, developer_catalog.text

        with connect_database(config.database_url) as connection:
            connection.execute(
                "UPDATE users SET role='viewer' WHERE email='admin@metahub.local'"
            )
            connection.commit()
        viewer_login = await _legacy_login(client)
        assert "provider_catalog.read" not in viewer_login["permission_codes"]
        viewer_catalog = await client.get(
            "/provider-catalog",
            headers={"Authorization": f"Bearer {viewer_login['access_token']}"},
        )
        assert viewer_catalog.status_code == 403
        assert viewer_catalog.json()["code"] == "auth.permission_denied"


@pytest.mark.anyio
@pytest.mark.parametrize("tamper", ["missing_table", "changed_assignments"])
async def test_legacy_bearer_provider_catalog_extension_tamper_fails_closed(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
    tamper: str,
) -> None:
    config, _actor, _platform = runtime
    with connect_database(config.database_url) as connection:
        connection.execute("UPDATE users SET role='developer' WHERE email='admin@metahub.local'")
        connection.commit()
    transport = ASGITransport(app=_legacy_app(config))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await _legacy_login(client)
        token = login["access_token"]
        with connect_database(config.database_url) as connection:
            if tamper == "missing_table":
                connection.execute("DROP TABLE workspace_provider_catalog_role_permissions")
            else:
                connection.execute(
                    "DROP TRIGGER trg_workspace_provider_catalog_roles_delete_prd119"
                )
                connection.execute(
                    "DELETE FROM workspace_provider_catalog_role_permissions "
                    "WHERE role_code='developer'"
                )
            connection.commit()
        response = await client.get(
            "/provider-catalog",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 503
        assert response.json()["code"] == "rbac.unavailable"


def test_delegated_machine_authority_revalidates_provider_catalog_matrix(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, _platform = runtime
    with connect_database(config.database_url) as connection:
        snapshot = workspace_service.workspace_actor_authorization_probe_in_transaction(
            connection,
            account_id=actor.account_id,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            permission_codes=("provider.read",),
        )
        assert snapshot.workspace_id == actor.workspace_id
        connection.execute(
            "DROP TRIGGER trg_workspace_provider_catalog_roles_delete_prd119"
        )
        connection.execute(
            "DELETE FROM workspace_provider_catalog_role_permissions WHERE role_code='owner'"
        )
        connection.commit()
        with pytest.raises(ApiError) as failure:
            workspace_service.workspace_actor_authorization_probe_in_transaction(
                connection,
                account_id=actor.account_id,
                workspace_id=actor.workspace_id,
                tenant_id=actor.tenant_id,
                permission_codes=("provider.read",),
            )
        assert failure.value.code == "workspace_session.unavailable"


@pytest.mark.anyio
async def test_enterprise_catalog_create_compatibility_and_preflight(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, actor, platform = runtime
    holder = {"actor": platform}
    transport = ASGITransport(app=_app(monkeypatch, config, actor, holder))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(
            "/provider-catalog?capability_code=chat",
            headers=_workspace_headers(),
        )
        assert listed.status_code == 200, listed.text
        assert "no-store" in listed.headers["cache-control"]
        body = listed.json()
        assert body["version"] == "2026.08.26.1"
        assert len(body["entries"]) == 10
        assert all("adapter" not in entry and "base_url" not in entry for entry in body["entries"])
        migration = next(
            entry for entry in body["entries"] if entry["entry_code"] == "openrouter_migration"
        )
        assert migration["instantiation_mode"] == "migration_only"
        assert migration["endpoints"] == []

        compatibility = await client.get(
            "/provider-catalog/deepseek/compatibility",
            headers=_workspace_headers(),
        )
        assert compatibility.status_code == 200, compatibility.text
        assert compatibility.json()["compatibility"]["level"] == "native"

        create_body = {
            "entry_code": "deepseek",
            "version": body["version"],
            "region_code": "cn-mainland",
            "capability_codes": ["chat"],
            "provider_code": "deepseek_catalog",
            "name": "DeepSeek catalog",
            "confirmed": True,
        }
        created = await client.post(
            "/providers/from-catalog",
            headers=_workspace_headers(key="prd119-create-provider-0001"),
            json=create_body,
        )
        assert created.status_code == 201, created.text
        instance = created.json()["instance"]
        replay = await client.post(
            "/providers/from-catalog",
            headers=_workspace_headers(key="prd119-create-provider-0001"),
            json=create_body,
        )
        assert replay.status_code == 201
        assert replay.json()["idempotent_replay"] is True
        assert (
            replay.json()["instance"]["provider_catalog_instance_id"]
            == instance["provider_catalog_instance_id"]
        )
        with connect_database(config.database_url) as connection:
            provider = connection.execute(
                "SELECT base_url FROM providers WHERE provider_id=?",
                (instance["provider_id"],),
            ).fetchone()
            assert provider["base_url"] == "https://api.deepseek.com/v1"

        checked_at = datetime.now(UTC)
        monkeypatch.setattr(
            catalog_service,
            "probe_catalog_provider_for_tenant",
            lambda *_args, **_kwargs: SimpleNamespace(
                result_status="success",
                attempts=[
                    SimpleNamespace(
                        latency_ms=9,
                        http_status=200,
                        error_code=None,
                        checked_at=checked_at,
                    )
                ],
            ),
        )
        preflight_started = time.perf_counter()
        preflight = await client.post(
            f"/providers/{instance['provider_id']}/preflight",
            headers=_workspace_headers(key="prd119-preflight-0001"),
            json={"timeout_seconds": 2.0},
        )
        preflight_elapsed = time.perf_counter() - preflight_started
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["run"]["outcome_code"] == "probe_succeeded"
        assert preflight.json()["run"]["status"] == "succeeded"
        assert preflight_elapsed < 30.0
        second = await client.post(
            f"/providers/{instance['provider_id']}/preflight",
            headers=_workspace_headers(key="prd119-preflight-0001"),
            json={"timeout_seconds": 2.0},
        )
        assert second.json()["idempotent_replay"] is True

    with connect_database(config.database_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_preflight_runs").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
async def test_platform_signed_publication_is_isolated_and_idempotent(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, actor, platform = runtime
    holder = {"actor": platform}
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    public_b64 = base64.urlsafe_b64encode(public).decode("ascii").rstrip("=")
    with connect_database(config.database_url) as connection:
        connection.execute(
            """
            INSERT INTO provider_catalog_trusted_keys(
              key_id,algorithm,public_key_b64,status,key_purpose,created_at)
            VALUES('prd119-api-active-v1','ed25519',?,'active','provider_catalog',?)
            """,
            (public_b64, "2026-08-26T01:00:00.000Z"),
        )
        provider_count = connection.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
        connection.commit()

    manifest_value = copy.deepcopy(SEED_MANIFEST)
    manifest_value["version"] = "2026.08.26.2"
    manifest = ProviderCatalogManifest.model_validate(manifest_value)
    signature = base64.urlsafe_b64encode(
        private.sign(canonical_json_bytes(manifest.model_dump(mode="json")))
    ).decode("ascii").rstrip("=")
    private = None
    publish_body = {
        "manifest": manifest.model_dump(mode="json"),
        "key_id": "prd119-api-active-v1",
        "signature": signature,
        "reason_code": "catalog_release",
        "confirmed": True,
    }
    transport = ASGITransport(app=_app(monkeypatch, config, actor, holder))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        warm_catalog = await client.get(
            "/provider-catalog?status=active&capability_code=chat",
            headers=_workspace_headers(),
        )
        assert warm_catalog.status_code == 200, warm_catalog.text
        catalog_samples: list[float] = []
        for _index in range(40):
            started = time.perf_counter()
            catalog = await client.get(
                "/provider-catalog?status=active&capability_code=chat",
                headers=_workspace_headers(),
            )
            catalog_samples.append(time.perf_counter() - started)
            assert catalog.status_code == 200, catalog.text
        catalog_p95 = sorted(catalog_samples)[math.ceil(len(catalog_samples) * 0.95) - 1]
        assert catalog_p95 < 0.300, catalog_samples

        summary = await client.get(
            "/platform/provider-catalog", headers=_platform_headers()
        )
        assert summary.status_code == 200, summary.text
        assert summary.headers["etag"] == '"1"'
        propagation_started = time.perf_counter()
        published = await client.post(
            "/platform/provider-catalog/versions",
            headers=_platform_headers(key="prd119-publish-version-0001", revision=1),
            json=publish_body,
        )
        assert published.status_code == 201, published.text
        assert published.json()["catalog"]["version"] == "2026.08.26.2"
        assert published.json()["catalog"]["generation"] == 2
        assert "signature" not in published.json()["catalog"]
        propagated_catalog = await client.get(
            "/provider-catalog",
            headers=_workspace_headers(),
        )
        propagation_elapsed = time.perf_counter() - propagation_started
        assert propagated_catalog.status_code == 200, propagated_catalog.text
        assert propagated_catalog.json()["version"] == "2026.08.26.2"
        assert propagation_elapsed < 300.0
        replay = await client.post(
            "/platform/provider-catalog/versions",
            headers=_platform_headers(key="prd119-publish-version-0001", revision=1),
            json=publish_body,
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["idempotent_replay"] is True

        holder["actor"] = replace(
            platform,
            permissions=("platform.provider_catalog.security_read",),
        )
        security = await client.get(
            "/platform/provider-catalog", headers=_platform_headers()
        )
        assert security.status_code == 200
        assert security.json()["security_projection"] is True
        denied = await client.post(
            "/platform/provider-catalog/versions",
            headers=_platform_headers(key="prd119-publish-version-0002", revision=2),
            json=publish_body,
        )
        assert denied.status_code == 403

    with connect_database(config.database_url) as connection:
        assert tuple(
            connection.execute(
                "SELECT version,generation,revision FROM provider_catalog_head"
            ).fetchone()
        ) == ("2026.08.26.2", 2, 2)
        assert connection.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == provider_count
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
async def test_workspace_and_platform_credential_domains_do_not_mix(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, actor, platform = runtime
    holder = {"actor": platform}
    transport = ASGITransport(app=_app(monkeypatch, config, actor, holder))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/provider-catalog",
            headers={
                "Cookie": (
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
                    f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"
                )
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "workspace_session.credential_conflict"
        platform_response = await client.get(
            "/platform/provider-catalog",
            headers={
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; "
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"
                )
            },
        )
        assert platform_response.status_code == 400
        assert platform_response.json()["code"] == "platform.credential_conflict"
