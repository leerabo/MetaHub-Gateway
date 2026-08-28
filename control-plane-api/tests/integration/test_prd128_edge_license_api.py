"""PRD-128 issue, pull, ACK, refresh, and explicit-revocation API smoke flow."""

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
from metahub_control.edge_management.license_service import _signing_key
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
from metahub_shared.private_gateway import verify_license_entitlement
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43
PLATFORM_CSRF = "q" * 43
DEPLOYMENT_ID = "edp_12800000000000000000000000000100"
IDENTITY_ID = "edi_12800000000000000000000000000100"
CERTIFICATE_ID = "edc_12800000000000000000000000000100"
SESSION_ID = "eds_12800000000000000000000000000100"
AGENT_TOKEN = "egs_" + "e" * 43
FINGERPRINT = "f" * 64
_TEST_PASSWORD = f"Prd128!Aa9{secrets.token_urlsafe(18)}"


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
    database = tmp_path_factory.mktemp("prd128-license-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0122"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path
) -> tuple[AppConfig, WorkspaceSessionContext, dict[str, PlatformContext]]:
    database = tmp_path / "prd128.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    actor = _owner_actor(config)
    contexts = _insert_platform_operators(config)
    _insert_commercial_and_device_facts(config, actor, contexts["billing"])
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
        "edge.license.import",
        "edge.license.read",
        "edge.license.refresh",
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
    contexts = {
        "billing": _platform_context(
            "a",
            "platform_billing",
            (
                "platform.edge_license.issue",
                "platform.edge_license.read",
                "platform.edge_license.revoke",
            ),
        ),
        "support": _platform_context("b", "platform_support", ("platform.support.request",)),
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
                    "email_normalized": f"prd128-{name}@example.invalid",
                    "phone_e164": None,
                    "display_name": f"PRD-128 {name}",
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


def _insert_commercial_and_device_facts(
    config: AppConfig,
    actor: WorkspaceSessionContext,
    billing: PlatformContext,
) -> None:
    now = datetime.now(UTC)
    before = _utc_text(now - timedelta(days=1))
    expires = _utc_text(now + timedelta(days=60))
    entitlements = (
        '{"gateway_requests_per_month":1000000,"model_aliases":10,'
        '"priority_support":true,"retention_days":30,"workspace_seats":20}'
    )
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """
            INSERT INTO saas_plan_catalogs(
              plan_catalog_id,plan_code,display_name,status,display_order,revision,
              created_by_operator_id,created_at,updated_at)
            VALUES('spc_12800000000000000000000000000100','private_gateway',
              'Private Gateway','active',1,1,?,?,?)
            """,
            (billing.operator_id, before, before),
        )
        connection.execute(
            """
            INSERT INTO saas_plan_versions(
              plan_version_id,plan_catalog_id,version_number,status,billing_period,
              currency,unit_price_minor,trial_days,entitlements_json,entitlements_sha256,
              created_by_operator_id,published_at,created_at)
            VALUES('spv_12800000000000000000000000000100',
              'spc_12800000000000000000000000000100',1,'published','monthly',
              'CNY',10000,0,?,?,?, ?,?)
            """,
            (entitlements, _digest(entitlements), billing.operator_id, before, before),
        )
        connection.execute(
            """
            INSERT INTO workspace_subscriptions(
              subscription_id,workspace_id,tenant_id,plan_version_id,
              pending_plan_version_id,status,current_period_started_at,
              current_period_ends_at,pending_effective_at,state_revision,
              created_by_membership_id,created_at,updated_at)
            VALUES('sub_12800000000000000000000000000100',?,?,
              'spv_12800000000000000000000000000100',NULL,'active',?,?,NULL,1,?,?,?)
            """,
            (
                actor.workspace_id,
                actor.tenant_id,
                before,
                expires,
                actor.membership_id,
                before,
                before,
            ),
        )
        connection.execute(
            """
            INSERT INTO subscription_order_records(
              order_record_id,workspace_id,tenant_id,subscription_id,plan_version_id,
              order_kind,status,currency,amount_minor,idempotency_sha256,request_sha256,
              created_by_membership_id,created_at)
            VALUES('sor_12800000000000000000000000000100',?,?,
              'sub_12800000000000000000000000000100',
              'spv_12800000000000000000000000000100','create','not_required',
              'CNY',0,?,?,?,?)
            """,
            (actor.workspace_id, actor.tenant_id, "1" * 64, "2" * 64, actor.membership_id, before),
        )
        connection.execute(
            """
            INSERT INTO entitlement_snapshots(
              entitlement_snapshot_id,workspace_id,tenant_id,subscription_id,
              plan_version_id,source_order_record_id,snapshot_version,
              entitlements_json,entitlements_sha256,effective_from,effective_until,created_at)
            VALUES('ent_12800000000000000000000000000100',?,?,
              'sub_12800000000000000000000000000100',
              'spv_12800000000000000000000000000100',
              'sor_12800000000000000000000000000100',1,?,?,?,?,?)
            """,
            (
                actor.workspace_id,
                actor.tenant_id,
                entitlements,
                _digest(entitlements),
                before,
                expires,
                before,
            ),
        )
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
              status,revision,revocation_generation,agent_version,health_code,
              last_connected_at,certificate_expires_at,created_by_account_id,
              revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'PRD-128 gateway','cn-test','connected','active',1,1,
              '1.0.0','healthy',?,?,?,NULL,NULL,?,?,NULL)
            """,
            (
                DEPLOYMENT_ID,
                actor.workspace_id,
                actor.tenant_id,
                before,
                expires,
                actor.account_id,
                before,
                before,
            ),
        )
        public_key_sha256 = _digest("prd128-public-key")
        connection.execute(
            """
            INSERT INTO edge_device_identities(
              edge_device_identity_id,workspace_id,tenant_id,edge_deployment_id,
              public_key_sha256,hardware_proof_sha256,capability_codes_json,
              agent_version,generation,status,created_at,updated_at,revoked_at)
            VALUES(?,?,?,?,?,NULL,'["license"]','1.0.0',1,'active',?,?,NULL)
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
            VALUES(?,?,?,?,?,NULL,'12800',?,?,?,?, 'edge-device-ca-v1',1,'active',
              ?,?,NULL,?,?,NULL)
            """,
            (
                CERTIFICATE_ID,
                actor.workspace_id,
                actor.tenant_id,
                DEPLOYMENT_ID,
                IDENTITY_ID,
                _digest("prd128-certificate"),
                public_key_sha256,
                _digest("prd128-csr"),
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
                edge_service._secret_hmac(config, "edge-session-token-v1", AGENT_TOKEN),
                FINGERPRINT,
                _digest("prd128-handshake"),
                expires,
                before,
            ),
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
    monkeypatch.setattr(edge_routes, "verified_mtls_fingerprint", lambda _request: FINGERPRINT)
    app = create_service_app(
        "control-plane-api",
        "PRD-128 Test",
        extra_cors_allow_headers=("Idempotency-Key", "X-CSRF-Token", "X-Edge-Session"),
        cors_allow_credentials=True,
    )
    app.state.config = config
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


def _platform_headers(*, key: str | None = None) -> dict[str, str]:
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
    return headers


def _agent_headers() -> dict[str, str]:
    return {"X-Edge-Session": AGENT_TOKEN}


@pytest.mark.anyio
async def test_license_issue_pull_ack_refresh_and_revoke(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext, dict[str, PlatformContext]],
) -> None:
    config, actor, contexts = runtime
    platform_holder = {"context": contexts["billing"]}
    app = _app(monkeypatch, config, actor, platform_holder)
    expires_at = datetime.now(UTC) + timedelta(days=20)
    issue_payload = {
        "deployment_id": DEPLOYMENT_ID,
        "node_limit": 3,
        "concurrent_request_limit": 25,
        "features": ["advanced_routing", "policy_enforcement"],
        "expires_at": _utc_text(expires_at),
        "grace_days": 7,
        "source_order_id": None,
        "expected_revision": None,
        "reason_code": "commercial_activation",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get(
            f"/edge/licenses/{DEPLOYMENT_ID}", headers=_workspace_headers()
        )
        assert missing.status_code == 404, missing.text
        assert missing.json()["code"] == "edge_license.not_found"

        issued = await client.post(
            "/edge/licenses",
            headers=_platform_headers(key="prd128-license-issue-0001"),
            json=issue_payload,
        )
        assert issued.status_code == 201, issued.text
        issued_body = issued.json()
        license_id = issued_body["license"]["license_id"]
        assert issued_body["license"]["generation"] == 1
        assert issued_body["license"]["revision"] == 2
        assert "signature" not in issued.text
        assert "envelope" not in issued.text

        replay = await client.post(
            "/edge/licenses",
            headers=_platform_headers(key="prd128-license-issue-0001"),
            json=issue_payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["license"]["license_id"] == license_id

        workspace = await client.get(
            f"/edge/licenses/{DEPLOYMENT_ID}", headers=_workspace_headers()
        )
        assert workspace.status_code == 200, workspace.text
        assert workspace.json()["license"]["capacity"]["node_limit"] == 3
        assert "signature" not in workspace.text

        pulled = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/license", headers=_agent_headers()
        )
        assert pulled.status_code == 200, pulled.text
        pulled_body = pulled.json()
        assert pulled_body["entitlement"]["entitlement"]["generation"] == 1
        private_key, key_id = _signing_key(config)
        verified = verify_license_entitlement(
            pulled_body["entitlement"],
            trusted_keys={key_id: private_key.public_key().public_bytes_raw()},
        )
        assert verified.signed.entitlement.deployment_id == DEPLOYMENT_ID

        bucket_end = datetime.now(UTC)
        ack_payload = {
            "entitlement_id": pulled_body["entitlement"]["entitlement"]["entitlement_id"],
            "generation": 1,
            "disposition": "applied",
            "decision_status": "current",
            "reason_code": "edge_license.current",
            "trusted_wall_time": _utc_text(bucket_end),
            "attempt_nonce": "ack-" + "7" * 40,
            "active_node_units": 2,
            "active_request_units": 8,
            "denied_count": 0,
            "bucket_started_at": _utc_text(bucket_end - timedelta(minutes=5)),
            "bucket_ended_at": _utc_text(bucket_end),
        }
        acknowledged = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/license/acks",
            headers=_agent_headers(),
            json=ack_payload,
        )
        assert acknowledged.status_code == 201, acknowledged.text
        ack_replay = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/license/acks",
            headers=_agent_headers(),
            json=ack_payload,
        )
        assert ack_replay.status_code == 200, ack_replay.text
        assert ack_replay.json()["idempotent_replay"] is True

        next_bucket_end = bucket_end + timedelta(minutes=1)
        next_bucket_payload = {
            **ack_payload,
            "active_request_units": 9,
            "bucket_started_at": _utc_text(bucket_end),
            "bucket_ended_at": _utc_text(next_bucket_end),
        }
        next_bucket = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/license/acks",
            headers=_agent_headers(),
            json=next_bucket_payload,
        )
        assert next_bucket.status_code == 200, next_bucket.text
        assert next_bucket.json()["idempotent_replay"] is True

        refreshed = await client.post(
            f"/edge/licenses/{license_id}/refresh",
            headers=_workspace_headers(key="prd128-license-refresh-0001"),
            json={"expected_revision": 2, "reason_code": "online_refresh"},
        )
        assert refreshed.status_code == 201, refreshed.text
        assert refreshed.json()["license"]["generation"] == 2
        assert refreshed.json()["license"]["latest_usage"]["active_request_units"] == 9

        listed = await client.get(
            "/platform/edge/licenses", headers=_platform_headers()
        )
        assert listed.status_code == 200, listed.text
        assert [item["license_id"] for item in listed.json()["licenses"]] == [license_id]

        platform_holder["context"] = contexts["support"]
        denied = await client.get(
            "/platform/edge/licenses", headers=_platform_headers()
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "platform.permission_denied"

        platform_holder["context"] = contexts["billing"]
        revoked = await client.post(
            f"/edge/licenses/{license_id}/revoke",
            headers=_platform_headers(key="prd128-license-revoke-0001"),
            json={"expected_revision": 3, "reason_code": "security_revocation"},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["license"]["status"] == "revoked"
        assert revoked.json()["license"]["generation"] == 3
        assert revoked.json()["license"]["revocation_generation"] == 1

        revoked_pull = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/license", headers=_agent_headers()
        )
        assert revoked_pull.status_code == 200, revoked_pull.text
        assert (
            revoked_pull.json()["entitlement"]["entitlement"]["status"] == "revoked"
        )

    with closing(connect_database(config.database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_signed_entitlements WHERE edge_license_id=?",
            (license_id,),
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_license_acknowledgements"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_license_lease_reports"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_license_refresh_requests WHERE status='fulfilled'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_license_audits"
        ).fetchone()[0] == 3
