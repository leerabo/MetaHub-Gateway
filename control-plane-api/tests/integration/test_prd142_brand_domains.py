"""PRD-142 owner-separated branding API and public LKG contracts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import struct
import zlib
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.brand_domains import routes, service
from metahub_control.brand_domains.routes import router
from metahub_control.identity.service import CSRF_COOKIE as GLOBAL_CSRF_COOKIE
from metahub_control.identity.service import SESSION_COOKIE as GLOBAL_SESSION_COOKIE
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
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
NOW = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
STAMP = "2026-09-01T04:00:00Z"


@dataclass(slots=True)
class Runtime:
    config: AppConfig
    contexts: dict[str, PlatformContext]
    workspace_contexts: dict[str, WorkspaceSessionContext]
    platform_tokens: dict[str, str]
    workspace_tokens: dict[str, str]
    csrf_token: str
    step_up_grant: str
    partner_ids: tuple[str, str]
    deployment_ids: tuple[str, str]


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
            loaded_at=NOW,
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        identity_session_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        support_session_secret=secrets.token_urlsafe(32),
        approval_hmac_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd142-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0134"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", f"Aa9!{secrets.token_urlsafe(24)}")
        apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        for marker in ("a", "b", "c"):
            connection.execute(
                """
                INSERT INTO global_accounts(
                  account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                  credential_revision,session_revision,mfa_state,failed_login_count,
                  locked_until,last_login_at,created_at,updated_at)
                VALUES(?,?,NULL,?,'active',0,1,1,'not_enrolled',0,NULL,NULL,?,?)
                """,
                (
                    "gac_" + marker * 32,
                    f"prd142-{marker}@example.invalid",
                    f"PRD142 {marker}",
                    STAMP,
                    STAMP,
                ),
            )
            connection.execute(
                """
                INSERT INTO platform_operators(
                  operator_id,account_id,status,revision,created_by_operator_id,
                  created_at,updated_at)
                VALUES(?,?,'active',1,NULL,?,?)
                """,
                ("pto_" + marker * 32, "gac_" + marker * 32, STAMP, STAMP),
            )
        connection.commit()
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> Runtime:
    database = tmp_path / "prd142.sqlite3"
    database.write_bytes(base_database.read_bytes())
    config = _config(_url(database))
    platform_tokens = {
        "owner": secrets.token_urlsafe(32),
        "releaser": secrets.token_urlsafe(32),
        "channel": secrets.token_urlsafe(32),
    }
    workspace_tokens = {
        "owner": secrets.token_urlsafe(32),
        "viewer": secrets.token_urlsafe(32),
    }
    contexts = {
        platform_tokens["owner"]: PlatformContext(
            platform_session_id="pts_" + "a" * 32,
            operator_id="pto_" + "a" * 32,
            account_id="gac_" + "a" * 32,
            global_session_id="gss_" + "a" * 32,
            factor_id="mfa_" + "a" * 32,
            factor_revision=1,
            operator_revision=1,
            roles=("platform_owner",),
            permissions=(
                "platform.brand.read",
                "platform.brand.manage",
                "platform.brand.release",
            ),
            expires_at=NOW + timedelta(hours=1),
        ),
        platform_tokens["releaser"]: PlatformContext(
            platform_session_id="pts_" + "b" * 32,
            operator_id="pto_" + "b" * 32,
            account_id="gac_" + "b" * 32,
            global_session_id="gss_" + "b" * 32,
            factor_id="mfa_" + "b" * 32,
            factor_revision=1,
            operator_revision=1,
            roles=("release_manager",),
            permissions=(
                "platform.brand.read",
                "platform.brand.release",
                "platform.channel_brand.read",
                "platform.channel_brand.release",
            ),
            expires_at=NOW + timedelta(hours=1),
        ),
        platform_tokens["channel"]: PlatformContext(
            platform_session_id="pts_" + "c" * 32,
            operator_id="pto_" + "c" * 32,
            account_id="gac_" + "c" * 32,
            global_session_id="gss_" + "c" * 32,
            factor_id="mfa_" + "c" * 32,
            factor_revision=1,
            operator_revision=1,
            roles=("platform_channel",),
            permissions=("platform.channel_brand.read", "platform.channel_brand.manage"),
            expires_at=NOW + timedelta(hours=1),
        ),
    }
    partner_ids = ("par_" + "1" * 32, "par_" + "2" * 32)
    deployment_ids = ("edp_" + "1" * 32, "edp_" + "2" * 32)
    with closing(connect_database(config.database_url)) as connection:
        owner = connection.execute(
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
        assert owner is not None
        contract_digests = tuple(
            hashlib.sha256(f"prd142-contract-{index}".encode()).hexdigest() for index in (1, 2)
        )
        for index, partner_id in enumerate(partner_ids):
            connection.execute(
                """
                INSERT INTO channel_partners(
                  channel_partner_id,owner_workspace_id,tenant_id,partner_code,
                  display_name,tier_code,region_scope,contract_sha256,status,revision,
                  created_by_operator_id,updated_by_operator_id,created_at,updated_at)
                VALUES(?,?,?,? ,?,'silver','cn',?,'active',1,?,?,?,?)
                """,
                (
                    partner_id,
                    owner["workspace_id"],
                    owner["tenant_id"],
                    f"prd142.channel.{index + 1}",
                    f"PRD-142 Channel {index + 1}",
                    contract_digests[index],
                    "pto_" + "a" * 32,
                    "pto_" + "a" * 32,
                    STAMP,
                    STAMP,
                ),
            )
        connection.execute(
            """
            INSERT INTO platform_channel_partner_scopes(
              operator_id,channel_partner_id,status,revision,granted_by_operator_id,
              created_at,updated_at)
            VALUES(?,?,'active',1,?,?,?)
            """,
            ("pto_" + "c" * 32, partner_ids[0], "pto_" + "a" * 32, STAMP, STAMP),
        )
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
              status,revision,revocation_generation,agent_version,health_code,
              last_connected_at,certificate_expires_at,created_by_account_id,
              revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'PRD-142 private gateway','cn-test','connected','active',1,1,
              '1.0.0','healthy',NULL,NULL,?,NULL,NULL,?,?,NULL)
            """,
            (
                deployment_ids[0],
                owner["workspace_id"],
                owner["tenant_id"],
                owner["account_id"],
                STAMP,
                STAMP,
            ),
        )
        other_tenant_id = "ten_" + "f" * 32
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,'prd142-other','PRD-142 Other','internal_only','active',?,?)
            """,
            (other_tenant_id, STAMP, STAMP),
        )
        other_workspace = connection.execute(
            "SELECT workspace_id FROM workspace_tenant_bindings WHERE tenant_id=?",
            (other_tenant_id,),
        ).fetchone()
        assert other_workspace is not None
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
              status,revision,revocation_generation,agent_version,health_code,
              last_connected_at,certificate_expires_at,created_by_account_id,
              revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'PRD-142 other gateway','cn-test','connected','active',1,1,
              '1.0.0','healthy',NULL,NULL,?,NULL,NULL,?,?,NULL)
            """,
            (
                deployment_ids[1],
                other_workspace["workspace_id"],
                other_tenant_id,
                owner["account_id"],
                STAMP,
                STAMP,
            ),
        )
        snapshot = workspace_actor_permissions_in_transaction(
            connection,
            account_id=str(owner["account_id"]),
            workspace_id=str(owner["workspace_id"]),
            tenant_id=str(owner["tenant_id"]),
        )
        connection.commit()
    owner_context = WorkspaceSessionContext(
        workspace_session_id="wss_" + "1" * 32,
        global_session_id="gss_" + "1" * 32,
        account_id=str(owner["account_id"]),
        workspace_id=str(owner["workspace_id"]),
        tenant_id=str(owner["tenant_id"]),
        membership_id=str(owner["workspace_membership_id"]),
        legacy_user_id=str(owner["legacy_user_id"]),
        membership_role="owner",
        legacy_role="admin",
        account_email=str(owner["email"]),
        display_name=str(owner["display_name"]),
        tenant_slug=str(owner["slug"]),
        tenant_name=str(owner["name"]),
        permission_codes=snapshot.permission_codes,
        permission_revision=snapshot.permission_revision,
        authorization_fingerprint=snapshot.authorization_fingerprint,
        expires_at=NOW + timedelta(hours=1),
        token=workspace_tokens["owner"],
        legacy_authorization_fingerprint=snapshot.authorization_fingerprint,
    )
    assert {"deployment.branding.read", "deployment.branding.manage"} <= set(
        owner_context.permission_codes
    )
    workspace_contexts = {
        workspace_tokens["owner"]: owner_context,
        workspace_tokens["viewer"]: replace(
            owner_context,
            workspace_session_id="wss_" + "2" * 32,
            membership_role="viewer",
            legacy_role="viewer",
            permission_codes=(),
            token=workspace_tokens["viewer"],
        ),
    }
    return Runtime(
        config=config,
        contexts=contexts,
        workspace_contexts=workspace_contexts,
        platform_tokens=platform_tokens,
        workspace_tokens=workspace_tokens,
        csrf_token=secrets.token_urlsafe(32),
        step_up_grant=secrets.token_urlsafe(32),
        partner_ids=partner_ids,
        deployment_ids=deployment_ids,
    )


def _app(monkeypatch: pytest.MonkeyPatch, runtime: Runtime):
    def platform_actor(token: str) -> PlatformContext:
        context = runtime.contexts.get(token)
        if context is None:
            raise ApiError("platform.session_required")
        return context

    def workspace_actor(token: str) -> WorkspaceSessionContext:
        context = runtime.workspace_contexts.get(token)
        if context is None:
            raise ApiError("workspace_session.required")
        return context

    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda _connection, _config, *, session_token, **_kwargs: platform_actor(session_token),
    )
    monkeypatch.setattr(
        routes,
        "authenticate_platform_session_context",
        lambda _config, session_token, **_kwargs: platform_actor(session_token),
    )
    monkeypatch.setattr(
        service,
        "authenticate_workspace_session_in_transaction",
        lambda _connection, _config, *, session_token, **_kwargs: workspace_actor(session_token),
    )
    monkeypatch.setattr(
        routes,
        "authenticate_workspace_session_context",
        lambda _config, session_token, **_kwargs: workspace_actor(session_token),
    )
    monkeypatch.setattr(service, "_utcnow", lambda: NOW)
    monkeypatch.setattr(
        service,
        "consume_global_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: "mfa",
    )
    monkeypatch.setattr(
        service,
        "consume_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: "mfa",
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-142 Brand Domain Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = runtime.config
    app.include_router(router)
    return app


def _headers(
    runtime: Runtime,
    actor: str,
    *,
    key: str | None = None,
    revision: int = 1,
) -> dict[str, str]:
    token = runtime.platform_tokens[actor]
    if key is None:
        return {"Cookie": f"{PLATFORM_SESSION_COOKIE}={token}"}
    return {
        "Cookie": (
            f"{PLATFORM_SESSION_COOKIE}={token}; {PLATFORM_CSRF_COOKIE}={runtime.csrf_token}"
        ),
        "Idempotency-Key": key,
        "If-Match": f'"{revision}"',
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": runtime.csrf_token,
        "X-Step-Up-Grant": runtime.step_up_grant,
    }


def _workspace_headers(
    runtime: Runtime,
    actor: str,
    *,
    key: str | None = None,
    revision: int = 1,
) -> dict[str, str]:
    token = runtime.workspace_tokens[actor]
    if key is None:
        return {"Cookie": f"{WORKSPACE_SESSION_COOKIE}={token}"}
    return {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={token}; {WORKSPACE_CSRF_COOKIE}={runtime.csrf_token}"
        ),
        "Idempotency-Key": key,
        "If-Match": f'"{revision}"',
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": runtime.csrf_token,
        "X-Step-Up-Grant": runtime.step_up_grant,
    }


@pytest.mark.anyio
async def test_brand_routes_allow_global_cookie_coexistence_and_reject_cross_domain_credentials(
    monkeypatch: pytest.MonkeyPatch, runtime: Runtime
) -> None:
    app = _app(monkeypatch, runtime)
    deployment_id = runtime.deployment_ids[0]
    global_cookies = (
        f"{GLOBAL_SESSION_COOKIE}=global-session-test; "
        f"{GLOBAL_CSRF_COOKIE}=global-csrf-test"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        platform_headers = _headers(runtime, "owner")
        platform_headers["Cookie"] += f"; {global_cookies}"
        platform = await client.get("/platform/branding", headers=platform_headers)
        assert platform.status_code == 200, platform.text

        workspace_headers = _workspace_headers(runtime, "owner")
        workspace_headers["Cookie"] += f"; {global_cookies}"
        deployment = await client.get(
            f"/deployments/{deployment_id}/branding", headers=workspace_headers
        )
        assert deployment.status_code == 200, deployment.text

        mixed_platform = await client.get(
            "/platform/branding",
            headers={
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={runtime.platform_tokens['owner']}; "
                    f"{WORKSPACE_SESSION_COOKIE}={runtime.workspace_tokens['owner']}"
                )
            },
        )
        assert mixed_platform.status_code == 400
        assert mixed_platform.json()["code"] == "platform.credential_conflict"

        mixed_workspace = await client.get(
            f"/deployments/{deployment_id}/branding",
            headers={
                "Cookie": (
                    f"{WORKSPACE_SESSION_COOKIE}={runtime.workspace_tokens['owner']}; "
                    f"{PLATFORM_SESSION_COOKIE}={runtime.platform_tokens['owner']}"
                )
            },
        )
        assert mixed_workspace.status_code == 400
        assert mixed_workspace.json()["code"] == "workspace_session.credential_conflict"


def _png_chunk(chunk_type: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + chunk_type
        + body
        + struct.pack(">I", binascii.crc32(chunk_type + body) & 0xFFFFFFFF)
    )


def _png(*, red: int = 18, green: int = 94, blue: int = 117) -> bytes:
    width = height = 16
    pixels = b"".join(b"\x00" + bytes((red, green, blue, 255)) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(pixels))
        + _png_chunk(b"IEND", b"")
    )


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _desired_state_counts(runtime: Runtime) -> tuple[int, ...]:
    tables = (
        "edge_desired_states",
        "edge_desired_state_approvals",
        "edge_snapshot_artifacts",
        "edge_signed_snapshots",
        "edge_desired_state_heads",
        "edge_desired_state_events",
        "edge_desired_state_event_outbox",
    )
    with closing(connect_database(runtime.config.database_url)) as connection:
        return tuple(
            int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in tables
        )


@pytest.mark.anyio
async def test_platform_draft_release_public_lkg_and_legacy_route_cutover(
    monkeypatch: pytest.MonkeyPatch, runtime: Runtime
) -> None:
    app = _app(monkeypatch, runtime)
    key = "prd142-platform-update-0001"
    update_body = {
        "action": "update",
        "product_name": "MetaHub Enterprise",
        "primary_color": "#125E75",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        initial_public = await client.get("/branding/public", headers={"Host": "unknown.test"})
        assert initial_public.status_code == 200
        assert initial_public.json()["branding"]["product_name"] == "MetaHub"
        assert initial_public.json()["branding"]["brand_generation"] == 1
        assert initial_public.headers["cache-control"] == "no-store"

        current = await client.get("/platform/branding", headers=_headers(runtime, "owner"))
        assert current.status_code == 200, current.text
        assert current.json()["branding"]["revision"] == 1
        assert current.headers["etag"] == '"1"'

        updated = await client.patch(
            "/platform/branding",
            headers=_headers(runtime, "owner", key=key, revision=1),
            json=update_body,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["branding"]["status"] == "draft"
        assert updated.json()["branding"]["revision"] == 2

        replay = await client.patch(
            "/platform/branding",
            headers=_headers(runtime, "owner", key=key, revision=1),
            json=update_body,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        conflict = await client.patch(
            "/platform/branding",
            headers=_headers(runtime, "owner", key=key, revision=1),
            json={**update_body, "product_name": "Different"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "platform.idempotency_conflict"

        before_release = await client.get("/branding/public", headers={"Host": "unknown.test"})
        assert before_release.json()["branding"]["product_name"] == "MetaHub"

        released = await client.patch(
            "/platform/branding",
            headers=_headers(runtime, "releaser", key="prd142-platform-release-0001", revision=2),
            json={"action": "release"},
        )
        assert released.status_code == 200, released.text
        assert released.json()["branding"]["status"] == "released"
        assert released.json()["branding"]["published_generation"] == 2

        public = await client.get("/branding/public", headers={"Host": "unknown.test"})
        assert public.status_code == 200
        assert public.json()["branding"]["product_name"] == "MetaHub Enterprise"
        assert public.json()["branding"]["brand_generation"] == 2

        rejected_query = await client.get("/branding/public", params={"tenant_slug": "default"})
        assert rejected_query.status_code == 400
        assert (await client.get("/branding")).status_code == 404
        assert (await client.get("/branding/logo")).status_code == 404
        assert (await client.get("/branding/public/logo")).status_code == 404

    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute("SELECT count(*) FROM brand_commands").fetchone()[0] == 2
        events = connection.execute(
            "SELECT event_type,payload_json FROM brand_events ORDER BY created_at"
        ).fetchall()
        assert [row["event_type"] for row in events] == ["platform_brand.changed"]
        assert "MetaHub Enterprise" not in events[0]["payload_json"]
        assert connection.execute("SELECT count(*) FROM branding_configs").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.anyio
async def test_channel_scope_domain_asset_release_rotation_and_partner_fallback(
    monkeypatch: pytest.MonkeyPatch, runtime: Runtime
) -> None:
    app = _app(monkeypatch, runtime)
    partner_id, out_of_scope_partner_id = runtime.partner_ids
    domain = "login.prd142-channel.com"
    first_logo = _png()
    second_logo = _png(red=245, green=158, blue=11)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        current = await client.get(
            f"/platform/channel-partners/{partner_id}/branding",
            headers=_headers(runtime, "channel"),
        )
        assert current.status_code == 200, current.text
        assert current.json()["branding"]["partner_scope_revision"] == 1

        hidden = await client.get(
            f"/platform/channel-partners/{out_of_scope_partner_id}/branding",
            headers=_headers(runtime, "channel"),
        )
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "channel.partner_not_found"

        updated = await client.patch(
            f"/platform/channel-partners/{partner_id}/branding",
            headers=_headers(runtime, "channel", key="prd142-channel-update-0001", revision=1),
            json={"action": "update", "product_name": "North AI Gateway"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["branding"]["revision"] == 2

        no_change = await client.patch(
            f"/platform/channel-partners/{partner_id}/branding",
            headers=_headers(runtime, "channel", key="prd142-channel-no-change-0001", revision=2),
            json={"action": "update", "product_name": "North AI Gateway"},
        )
        assert no_change.status_code == 400
        assert no_change.json()["code"] == "branding.request_invalid"

        attached = await client.patch(
            f"/platform/channel-partners/{partner_id}/branding",
            headers=_headers(runtime, "channel", key="prd142-channel-logo-0001", revision=2),
            json={
                "action": "attach_asset",
                "asset_kind": "logo",
                "png_base64": _encoded(first_logo),
            },
        )
        assert attached.status_code == 200, attached.text
        first_asset_id = attached.json()["branding"]["logo"]["asset_id"]
        assert attached.json()["branding"]["logo"]["status"] == "staged"
        assert service.scan_brand_asset_once(runtime.config) is True

        requested = await client.patch(
            f"/platform/channel-partners/{partner_id}/branding",
            headers=_headers(runtime, "channel", key="prd142-channel-domain-0001", revision=3),
            json={"action": "request_domain", "domain": domain},
        )
        assert requested.status_code == 200, requested.text
        first_domain = requested.json()["branding"]["domain"]
        first_challenge = first_domain["challenge"]
        domain_id = first_domain["domain_id"]
        first_observation = hashlib.sha256(b"prd142-observation-first").hexdigest()
        second_observation = hashlib.sha256(b"prd142-observation-conflict").hexdigest()
        assert (
            service.record_domain_observation(
                runtime.config,
                verified_domain_id=domain_id,
                challenge_generation=1,
                resolver_code="resolver_a",
                observation_sha256=first_observation,
                dnssec_valid=True,
                certificate_ready=False,
                routing_ready=False,
            ).status
            == "pending"
        )
        mismatch = service.record_domain_observation(
            runtime.config,
            verified_domain_id=domain_id,
            challenge_generation=1,
            resolver_code="resolver_b",
            observation_sha256=second_observation,
            dnssec_valid=True,
            certificate_ready=True,
            routing_ready=True,
        )
        assert mismatch.status == "reverification_required"
        assert mismatch.certificate_ready is False
        assert mismatch.routing_ready is False

        rotated = await client.patch(
            f"/platform/channel-partners/{partner_id}/branding",
            headers=_headers(runtime, "channel", key="prd142-channel-domain-0002", revision=4),
            json={"action": "request_domain", "domain": domain},
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["branding"]["domain"]["domain_id"] == domain_id
        assert rotated.json()["branding"]["domain"]["challenge"] != first_challenge
        matching_observation = hashlib.sha256(b"prd142-observation-matching").hexdigest()
        service.record_domain_observation(
            runtime.config,
            verified_domain_id=domain_id,
            challenge_generation=2,
            resolver_code="resolver_a",
            observation_sha256=matching_observation,
            dnssec_valid=True,
            certificate_ready=False,
            routing_ready=False,
        )
        verified = service.record_domain_observation(
            runtime.config,
            verified_domain_id=domain_id,
            challenge_generation=2,
            resolver_code="resolver_b",
            observation_sha256=matching_observation,
            dnssec_valid=True,
            certificate_ready=True,
            routing_ready=True,
        )
        assert verified.status == "active"

        released = await client.patch(
            f"/platform/channel-partners/{partner_id}/branding",
            headers=_headers(runtime, "releaser", key="prd142-channel-release-0001", revision=5),
            json={"action": "release"},
        )
        assert released.status_code == 200, released.text
        assert released.json()["branding"]["published_generation"] == 1
        first_public = await client.get("/branding/public", headers={"Host": domain})
        assert first_public.json()["branding"]["product_name"] == "North AI Gateway"
        first_path = first_public.json()["branding"]["logo_path"]
        first_asset = await client.get(first_path, headers={"Host": domain})
        assert first_asset.status_code == 200
        assert first_asset.content == first_logo
        assert first_asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert first_asset.headers["x-content-type-options"] == "nosniff"

        cross_owner = await client.get(first_path, headers={"Host": "unknown.test"})
        assert cross_owner.status_code == 404

        replacement = await client.patch(
            f"/platform/channel-partners/{partner_id}/branding",
            headers=_headers(runtime, "channel", key="prd142-channel-logo-0002", revision=6),
            json={
                "action": "attach_asset",
                "asset_kind": "logo",
                "png_base64": _encoded(second_logo),
            },
        )
        assert replacement.status_code == 200, replacement.text
        second_asset_id = replacement.json()["branding"]["logo"]["asset_id"]
        assert second_asset_id != first_asset_id
        assert service.scan_brand_asset_once(runtime.config) is True
        second_release = await client.patch(
            f"/platform/channel-partners/{partner_id}/branding",
            headers=_headers(runtime, "releaser", key="prd142-channel-release-0002", revision=7),
            json={"action": "release"},
        )
        assert second_release.status_code == 200, second_release.text
        assert second_release.json()["branding"]["published_generation"] == 2
        assert (await client.get(first_path, headers={"Host": domain})).status_code == 404
        new_public = await client.get("/branding/public", headers={"Host": domain})
        assert second_asset_id in new_public.json()["branding"]["logo_path"]

        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                "UPDATE channel_partners SET status='suspended',revision=revision+1,updated_at=? "
                "WHERE channel_partner_id=?",
                ("2026-09-01T04:01:00Z", partner_id),
            )
            connection.commit()
        fallback = await client.get("/branding/public", headers={"Host": domain})
        assert fallback.status_code == 200
        assert fallback.json()["branding"]["product_name"] == "MetaHub"

    with closing(connect_database(runtime.config.database_url)) as connection:
        stored_domain = connection.execute(
            "SELECT challenge_hmac,last_verified_at,expires_at FROM brand_verified_domains "
            "WHERE verified_domain_id=?",
            (domain_id,),
        ).fetchone()
        assert stored_domain is not None
        assert stored_domain["challenge_hmac"] != first_challenge
        assert stored_domain["last_verified_at"] is not None
        event_types = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM brand_events ORDER BY brand_event_id"
            ).fetchall()
        ]
        assert event_types.count("brand_domain.verified") == 1
        assert event_types.count("channel_brand.changed") == 2
        payloads = "".join(
            row[0] for row in connection.execute("SELECT payload_json FROM brand_events")
        )
        assert domain not in payloads
        assert "North AI Gateway" not in payloads


@pytest.mark.anyio
async def test_platform_asset_validation_scan_and_public_head_isolation(
    monkeypatch: pytest.MonkeyPatch, runtime: Runtime
) -> None:
    app = _app(monkeypatch, runtime)
    valid = _png()
    ancillary = valid[:-12] + _png_chunk(b"tEXt", b"name\x00value") + valid[-12:]
    polyglot = valid + b"<script>alert(1)</script>"
    bad_crc = bytearray(valid)
    bad_crc[-1] ^= 0x01
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (512 * 1024)
    invalid_cases = (
        ("ancillary", ancillary, 400, "branding.logo_invalid"),
        ("polyglot", polyglot, 400, "branding.logo_invalid"),
        ("crc", bytes(bad_crc), 400, "branding.logo_invalid"),
        ("oversized", oversized, 413, "branding.logo_too_large"),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        for name, body, status, code in invalid_cases:
            rejected = await client.patch(
                "/platform/branding",
                headers=_headers(runtime, "owner", key=f"prd142-invalid-{name}-0001", revision=1),
                json={"action": "attach_asset", "asset_kind": "logo", "png_base64": _encoded(body)},
            )
            assert rejected.status_code == status, rejected.text
            assert rejected.json()["code"] == code

        attached = await client.patch(
            "/platform/branding",
            headers=_headers(runtime, "owner", key="prd142-platform-logo-0001", revision=1),
            json={"action": "attach_asset", "asset_kind": "logo", "png_base64": _encoded(valid)},
        )
        assert attached.status_code == 200, attached.text
        asset_id = attached.json()["branding"]["logo"]["asset_id"]
        assert service.scan_brand_asset_once(runtime.config) is True
        assert service.scan_brand_asset_once(runtime.config) is False
        scanned = await client.get("/platform/branding", headers=_headers(runtime, "owner"))
        assert scanned.json()["branding"]["logo"]["status"] == "clean"
        released = await client.patch(
            "/platform/branding",
            headers=_headers(
                runtime, "releaser", key="prd142-platform-logo-release-0001", revision=2
            ),
            json={"action": "release"},
        )
        assert released.status_code == 200, released.text
        public = await client.get("/branding/public", headers={"Host": "unknown.test"})
        path = public.json()["branding"]["logo_path"]
        assert asset_id in path
        asset = await client.get(path, headers={"Host": "unknown.test"})
        assert asset.status_code == 200
        assert asset.content == valid
        assert asset.headers["content-length"] == str(len(valid))
        missing = await client.get(
            "/branding/public/assets/bas_" + "f" * 32,
            headers={"Host": "unknown.test"},
        )
        assert missing.status_code == 404

    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute("SELECT count(*) FROM brand_assets").fetchone()[0] == 1
        row = connection.execute("SELECT * FROM brand_assets").fetchone()
        assert row["status"] == "published"
        assert row["mime_type"] == "image/png"
        assert row["object_key"] != path
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
async def test_deployment_owner_publish_ack_lkg_rollback_and_desired_state_separation(
    monkeypatch: pytest.MonkeyPatch, runtime: Runtime
) -> None:
    app = _app(monkeypatch, runtime)
    deployment_id, other_deployment_id = runtime.deployment_ids
    desired_before = _desired_state_counts(runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        forbidden = await client.get(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(runtime, "viewer"),
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "workspace_session.permission_denied"

        hidden = await client.get(
            f"/deployments/{other_deployment_id}/branding",
            headers=_workspace_headers(runtime, "owner"),
        )
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "edge.deployment_not_found"

        current = await client.get(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(runtime, "owner"),
        )
        assert current.status_code == 200, current.text
        assert current.json()["branding"]["revision"] == 1

        updated = await client.patch(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(
                runtime, "owner", key="prd142-deployment-update-0001", revision=1
            ),
            json={"action": "update", "product_name": "Private Gateway"},
        )
        assert updated.status_code == 200, updated.text
        first_publish = await client.patch(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(
                runtime, "owner", key="prd142-deployment-publish-0001", revision=2
            ),
            json={"action": "publish"},
        )
        assert first_publish.status_code == 200, first_publish.text
        first_brand = first_publish.json()["branding"]
        assert (
            first_brand["desired_generation"],
            first_brand["applied_generation"],
            first_brand["lkg_generation"],
        ) == (1, 0, 0)
        with closing(connect_database(runtime.config.database_url)) as connection:
            first_bundle = connection.execute(
                "SELECT manifest_json,manifest_sha256 FROM deployment_brand_bundles "
                "WHERE edge_deployment_id=? AND generation=1",
                (deployment_id,),
            ).fetchone()
        manifest = json.loads(first_bundle["manifest_json"])
        assert manifest["edge_deployment_id"] == deployment_id
        assert manifest["visual"]["product_name"] == "Private Gateway"
        assert "routing" not in manifest
        assert "provider" not in manifest
        assert (
            service.record_deployment_brand_ack(
                runtime.config,
                edge_deployment_id=deployment_id,
                generation=1,
                manifest_sha256=first_bundle["manifest_sha256"],
                accepted=True,
            )
            is True
        )

        applied = await client.get(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(runtime, "owner"),
        )
        assert applied.json()["branding"]["ack_status"] == "applied"
        assert (
            applied.json()["branding"]["applied_generation"],
            applied.json()["branding"]["lkg_generation"],
        ) == (1, 1)

        changed = await client.patch(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(
                runtime, "owner", key="prd142-deployment-update-0002", revision=4
            ),
            json={"action": "update", "login_message": "Generation two"},
        )
        assert changed.status_code == 200, changed.text
        second_publish = await client.patch(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(
                runtime, "owner", key="prd142-deployment-publish-0002", revision=5
            ),
            json={"action": "publish"},
        )
        assert second_publish.status_code == 200, second_publish.text
        with closing(connect_database(runtime.config.database_url)) as connection:
            second_bundle = connection.execute(
                "SELECT manifest_sha256 FROM deployment_brand_bundles "
                "WHERE edge_deployment_id=? AND generation=2",
                (deployment_id,),
            ).fetchone()
        assert (
            service.record_deployment_brand_ack(
                runtime.config,
                edge_deployment_id=deployment_id,
                generation=2,
                manifest_sha256=second_bundle["manifest_sha256"],
                accepted=True,
            )
            is True
        )

        transient = await client.patch(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(
                runtime, "owner", key="prd142-deployment-update-0003", revision=7
            ),
            json={"action": "update", "product_name": "Transient Brand"},
        )
        assert transient.status_code == 200, transient.text
        third_publish = await client.patch(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(
                runtime, "owner", key="prd142-deployment-publish-0003", revision=8
            ),
            json={"action": "publish"},
        )
        assert third_publish.status_code == 200, third_publish.text
        rolled_back = await client.patch(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(
                runtime, "owner", key="prd142-deployment-rollback-0001", revision=9
            ),
            json={"action": "rollback_lkg"},
        )
        assert rolled_back.status_code == 200, rolled_back.text
        rolled_brand = rolled_back.json()["branding"]
        assert rolled_brand["product_name"] == "Private Gateway"
        assert rolled_brand["desired_generation"] == 4
        assert rolled_brand["applied_generation"] == 2
        assert rolled_brand["lkg_generation"] == 2

        revision_before_stale = rolled_brand["revision"]
        assert (
            service.record_deployment_brand_ack(
                runtime.config,
                edge_deployment_id=deployment_id,
                generation=1,
                manifest_sha256=first_bundle["manifest_sha256"],
                accepted=True,
            )
            is False
        )
        after_stale = await client.get(
            f"/deployments/{deployment_id}/branding",
            headers=_workspace_headers(runtime, "owner"),
        )
        assert after_stale.json()["branding"]["revision"] == revision_before_stale

        central_public = await client.get("/branding/public", headers={"Host": "unknown.test"})
        assert central_public.json()["branding"]["product_name"] == "MetaHub"

    assert _desired_state_counts(runtime) == desired_before
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert (
            connection.execute("SELECT count(*) FROM deployment_brand_bundles").fetchone()[0] == 4
        )
        assert connection.execute("SELECT count(*) FROM deployment_brand_acks").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
