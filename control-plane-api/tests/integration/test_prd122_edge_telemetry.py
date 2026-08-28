"""PRD-122 Edge telemetry ordering, policy, reconciliation, and privacy acceptance."""

from __future__ import annotations

import gzip
import json
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
from metahub_control.edge_management import routes as edge_routes
from metahub_control.edge_management import service as edge_service
from metahub_control.edge_management import telemetry_service
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
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43
_TEST_PASSWORD = f"Prd122!Aa9{secrets.token_urlsafe(18)}"


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
    def workspace_auth(*_args, **_kwargs):
        return actor_holder["actor"]

    monkeypatch.setattr(
        edge_service, "authenticate_workspace_session_in_transaction", workspace_auth
    )
    monkeypatch.setattr(
        telemetry_service,
        "authenticate_workspace_session_in_transaction",
        workspace_auth,
    )
    monkeypatch.setattr(
        edge_routes,
        "authenticate_platform_session_context",
        lambda *_args, **_kwargs: platform,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-122 Test",
        extra_cors_allow_headers=(
            "Content-Encoding",
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


def _workspace_read_headers() -> dict[str, str]:
    return {"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"}


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
    created = await client.post(
        "/edge/registrations",
        headers=_workspace_headers(
            key=f"prd122-registration-{suffix}-0001", revision=0
        ),
        json={
            "display_name": f"PRD-122 gateway {suffix}",
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
            "agent_version": "1.1.0",
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
            "handshake_nonce": f"ecn_{suffix.upper():A<32}"[:36],
            "ttl_seconds": 45,
        },
    )
    assert session.status_code == 201
    return deployment_id, fingerprint, session.json()["session_token"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _batch(
    *,
    deployment_id: str,
    stream: str,
    epoch: str,
    sequence: int,
    policy_revision: int,
    record: dict[str, object],
    sent_at: str,
    previous_epoch: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "deployment_id": deployment_id,
        "stream": stream,
        "epoch_id": epoch,
        "previous_epoch_id": previous_epoch,
        "sequence": sequence,
        "policy_revision": policy_revision,
        "sent_at": sent_at,
        "records": [record],
    }


def _gzip_body(payload: dict[str, object]) -> bytes:
    return gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


@pytest.mark.anyio
async def test_edge_telemetry_policy_ordering_reconciliation_and_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "prd122.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0122"
    with monkeypatch.context() as seed_patch:
        seed_patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    config = _config(database_url)
    owner = _owner_actor(config)
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
    now = datetime.now(UTC).replace(microsecond=0)
    bucket_start = _wire_time(now - timedelta(minutes=2))
    bucket_end = _wire_time(now - timedelta(minutes=1))
    older_bucket_start = _wire_time(now - timedelta(minutes=4))
    older_bucket_end = _wire_time(now - timedelta(minutes=3))
    sent_at = _wire_time(now)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        deployment_id, fingerprint, edge_session = await _register_agent(
            client, tls, suffix="primary", capabilities=["config_apply", "telemetry"]
        )
        agent_headers = {"X-Edge-Session": edge_session}
        initial = await client.get(
            f"/edge/deployments/{deployment_id}/telemetry-watermark",
            headers=agent_headers,
        )
        assert initial.status_code == 200
        assert initial.json()["policy"]["telemetry_level"] == "minimal"
        assert initial.json()["policy"]["revision"] == 1
        assert initial.json()["cursors"] == []

        usage_epoch = "ete_" + "a" * 32
        first_usage = _batch(
            deployment_id=deployment_id,
            stream="usage",
            epoch=usage_epoch,
            sequence=1,
            policy_revision=1,
            sent_at=sent_at,
            record={
                "kind": "usage",
                "bucket_start": bucket_start,
                "bucket_end": bucket_end,
                "model_id": "model_prd122",
                "provider_id": "provider_prd122",
                "request_count": 2,
                "input_tokens": 20,
                "output_tokens": 10,
                "cache_tokens": 4,
                "cost_cny_micros": 500,
                "cost_usd_micros": 70,
            },
        )
        minimal_reject = await client.post(
            "/edge/telemetry/batches", headers=agent_headers, json=first_usage
        )
        assert minimal_reject.status_code == 400

        billing_headers = _workspace_headers(
            key="prd122-policy-billing-0001", revision=1
        )
        billing = await client.patch(
            f"/edge/deployments/{deployment_id}/telemetry-policy",
            headers=billing_headers,
            json={"telemetry_level": "billing"},
        )
        assert billing.status_code == 200
        assert billing.json()["policy"]["revision"] == 2
        assert billing.headers["etag"] == '"2"'
        replay = await client.patch(
            f"/edge/deployments/{deployment_id}/telemetry-policy",
            headers=billing_headers,
            json={"telemetry_level": "billing"},
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        key_conflict = await client.patch(
            f"/edge/deployments/{deployment_id}/telemetry-policy",
            headers=billing_headers,
            json={"telemetry_level": "operational"},
        )
        assert key_conflict.status_code == 409
        stale = await client.patch(
            f"/edge/deployments/{deployment_id}/telemetry-policy",
            headers=_workspace_headers(
                key="prd122-policy-stale-000001", revision=1
            ),
            json={"telemetry_level": "operational"},
        )
        assert stale.status_code == 412

        first_usage["policy_revision"] = 2
        second_usage = json.loads(json.dumps(first_usage))
        second_usage["sequence"] = 2
        second_usage["records"][0]["request_count"] = 3
        second_usage["records"][0]["input_tokens"] = 30
        second_usage["records"][0]["output_tokens"] = 15
        second_usage["records"][0]["cache_tokens"] = 6
        second_usage["records"][0]["cost_cny_micros"] = 700
        second_usage["records"][0]["cost_usd_micros"] = 90
        gzip_headers = {
            **agent_headers,
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
        sequence_two = await client.post(
            "/edge/telemetry/batches",
            headers=gzip_headers,
            content=_gzip_body(second_usage),
        )
        assert sequence_two.status_code == 200
        assert sequence_two.json()["contiguous_sequence"] == 0
        assert sequence_two.json()["expected_sequence"] == 1
        assert sequence_two.json()["highest_seen_sequence"] == 2
        sequence_one = await client.post(
            "/edge/telemetry/batches",
            headers=gzip_headers,
            content=_gzip_body(first_usage),
        )
        assert sequence_one.status_code == 200
        assert sequence_one.json()["contiguous_sequence"] == 2
        assert sequence_one.json()["expected_sequence"] == 3
        assert sequence_one.json()["highest_seen_sequence"] == 2
        duplicate = await client.post(
            "/edge/telemetry/batches", headers=agent_headers, json=first_usage
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["disposition"] == "duplicate"
        assert duplicate.json()["receipt_id"] == sequence_one.json()["receipt_id"]
        conflicting_usage = json.loads(json.dumps(first_usage))
        conflicting_usage["records"][0]["request_count"] = 9
        conflict = await client.post(
            "/edge/telemetry/batches", headers=agent_headers, json=conflicting_usage
        )
        assert conflict.status_code == 409

        failure = _batch(
            deployment_id=deployment_id,
            stream="failure",
            epoch="ete_" + "b" * 32,
            sequence=1,
            policy_revision=2,
            sent_at=sent_at,
            record={
                "kind": "failure",
                "bucket_start": bucket_start,
                "bucket_end": bucket_end,
                "component_code": "provider_pool",
                "failure_code": "provider_timeout",
                "count": 3,
            },
        )
        assert (
            await client.post(
                "/edge/telemetry/batches", headers=agent_headers, json=failure
            )
        ).status_code == 200
        health = _batch(
            deployment_id=deployment_id,
            stream="health",
            epoch="ete_" + "c" * 32,
            sequence=1,
            policy_revision=2,
            sent_at=sent_at,
            record={
                "kind": "health",
                "bucket_start": bucket_start,
                "bucket_end": bucket_end,
                "component_code": "gateway",
                "heartbeat_count": 60,
                "uptime_seconds": 7200,
                "cpu_millis": 12_000,
                "memory_peak_bytes": 768 * 1024 * 1024,
            },
        )
        assert (
            await client.post(
                "/edge/telemetry/batches", headers=agent_headers, json=health
            )
        ).status_code == 200
        operational = _batch(
            deployment_id=deployment_id,
            stream="operational",
            epoch="ete_" + "d" * 32,
            sequence=1,
            policy_revision=2,
            sent_at=sent_at,
            record={
                "kind": "operational",
                "bucket_start": bucket_start,
                "bucket_end": bucket_end,
                "component_code": "gateway",
                "request_count": 10,
                "success_count": 8,
                "failure_count": 2,
                "latency_sum_ms": 1500,
                "queue_depth_max": 4,
            },
        )
        assert (
            await client.post(
                "/edge/telemetry/batches", headers=agent_headers, json=operational
            )
        ).status_code == 200

        older_health = json.loads(json.dumps(health))
        older_health["sequence"] = 2
        older_health["records"][0].update(
            {
                "bucket_start": older_bucket_start,
                "bucket_end": older_bucket_end,
                "cpu_millis": 60_000,
                "memory_peak_bytes": 4 * 1024 * 1024 * 1024,
            }
        )
        older_operational = json.loads(json.dumps(operational))
        older_operational["sequence"] = 2
        older_operational["records"][0].update(
            {
                "bucket_start": older_bucket_start,
                "bucket_end": older_bucket_end,
                "success_count": 1,
                "failure_count": 9,
            }
        )
        older_failure = json.loads(json.dumps(failure))
        older_failure["sequence"] = 2
        older_failure["records"][0].update(
            {
                "bucket_start": older_bucket_start,
                "bucket_end": older_bucket_end,
                "failure_code": "internal_error",
                "count": 1,
            }
        )
        for out_of_order in (older_health, older_operational, older_failure):
            accepted = await client.post(
                "/edge/telemetry/batches", headers=agent_headers, json=out_of_order
            )
            assert accepted.status_code == 200

        finance = replace(
            owner,
            membership_role="finance_admin",
            permission_codes=("edge.usage.read",),
        )
        actor_holder["actor"] = finance
        finance_view = await client.get(
            f"/edge/deployments/{deployment_id}/telemetry-watermark",
            headers=_workspace_read_headers(),
        )
        assert finance_view.status_code == 200
        assert finance_view.json()["policy"] is None
        assert finance_view.json()["cursors"] == []
        assert finance_view.json()["usage"] == {
            "request_count": 5,
            "input_tokens": 50,
            "output_tokens": 25,
            "cache_tokens": 10,
            "cost_cny_micros": 1200,
            "cost_usd_micros": 160,
        }
        assert finance_view.json()["failures"] == []
        assert finance_view.json()["last_receipt_at"] is None

        security = replace(
            owner,
            membership_role="security_admin",
            permission_codes=("edge.telemetry.manage", "edge.telemetry.read"),
        )
        actor_holder["actor"] = security
        security_view = await client.get(
            f"/edge/deployments/{deployment_id}/telemetry-watermark",
            headers=_workspace_read_headers(),
        )
        assert security_view.status_code == 200
        assert security_view.json()["policy"]["telemetry_level"] == "billing"
        assert security_view.json()["usage"] is None
        assert security_view.json()["failures"] == [
            {
                "component_code": "provider_pool",
                "failure_code": "provider_timeout",
                "count": 3,
            },
            {
                "component_code": "provider_pool",
                "failure_code": "internal_error",
                "count": 1,
            },
        ]
        assert len(security_view.json()["cursors"]) == 4

        actor_holder["actor"] = owner
        downgrade = await client.patch(
            f"/edge/deployments/{deployment_id}/telemetry-policy",
            headers=_workspace_headers(
                key="prd122-policy-minimal-0001", revision=2
            ),
            json={"telemetry_level": "minimal"},
        )
        assert downgrade.status_code == 200
        assert downgrade.json()["policy"]["revision"] == 3
        late_old_policy = json.loads(json.dumps(second_usage))
        late_old_policy["sequence"] = 3
        old_policy_reject = await client.post(
            "/edge/telemetry/batches", headers=agent_headers, json=late_old_policy
        )
        assert old_policy_reject.status_code == 400
        with monkeypatch.context() as historical_replay:
            historical_replay.setattr(
                telemetry_service, "_utcnow", lambda: now + timedelta(days=9)
            )
            duplicate_after_downgrade = await client.post(
                "/edge/telemetry/batches", headers=agent_headers, json=first_usage
            )
        assert duplicate_after_downgrade.status_code == 200
        assert duplicate_after_downgrade.json()["disposition"] == "duplicate"
        assert duplicate_after_downgrade.json()["contiguous_sequence"] == 2

        forbidden = json.loads(json.dumps(health))
        forbidden["records"][0]["tags"] = {"secret": "not-accepted"}
        assert (
            await client.post(
                "/edge/telemetry/batches", headers=agent_headers, json=forbidden
            )
        ).status_code == 400
        duplicated_key_wire = (
            json.dumps(health, separators=(",", ":"))[:-1]
            + ',"schema_version":"1"}'
        )
        assert (
            await client.post(
                "/edge/telemetry/batches",
                headers={**agent_headers, "Content-Type": "application/json"},
                content=duplicated_key_wire,
            )
        ).status_code == 400
        expansion_bomb = gzip.compress(
            ('{"padding":"' + "x" * 70_000 + '"}').encode("utf-8")
        )
        assert (
            await client.post(
                "/edge/telemetry/batches",
                headers=gzip_headers,
                content=expansion_bomb,
            )
        ).status_code == 400
        oversized_body = ('{"padding":"' + "x" * 1_048_576 + '"}').encode()
        assert (
            await client.post(
                "/edge/telemetry/batches",
                headers={**agent_headers, "Content-Type": "application/json"},
                content=oversized_body,
            )
        ).status_code == 400
        deeply_nested = ("{\"node\":" * 1100 + "null" + "}" * 1100).encode()
        assert (
            await client.post(
                "/edge/telemetry/batches",
                headers={**agent_headers, "Content-Type": "application/json"},
                content=deeply_nested,
            )
        ).status_code == 400
        for credential_headers in (
            {**agent_headers, "Cookie": "session=rejected"},
            {**agent_headers, "Authorization": "Bearer rejected"},
            {
                **agent_headers,
                "Cookie": "session=rejected",
                "Authorization": "Bearer rejected",
            },
        ):
            rejected = await client.post(
                "/edge/telemetry/batches", headers=credential_headers, json=health
            )
            assert rejected.status_code == 401

        fleet = await client.get(
            "/platform/fleet/edge-deployments",
            headers={"Cookie": f"__Host-metahub-platform-session={PLATFORM_TOKEN}"},
        )
        assert fleet.status_code == 200
        fleet_item = next(
            item
            for item in fleet.json()["deployments"]
            if item["deployment_id"] == deployment_id
        )
        assert set(fleet_item) == {
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
        assert fleet_item["telemetry_cpu_bucket"] == "low"
        assert fleet_item["telemetry_memory_bucket"] == "medium"
        assert fleet_item["telemetry_error_rate_basis_points"] == 2000
        assert fleet_item["telemetry_failure_code"] == "provider_timeout"
        assert "model_prd122" not in fleet.text
        assert "provider_prd122" not in fleet.text

        no_capability_id, no_capability_fingerprint, no_capability_session = (
            await _register_agent(
                client, tls, suffix="limited", capabilities=["config_apply"]
            )
        )
        tls["fingerprint"] = no_capability_fingerprint
        no_capability = await client.get(
            f"/edge/deployments/{no_capability_id}/telemetry-watermark",
            headers={"X-Edge-Session": no_capability_session},
        )
        assert no_capability.status_code == 401
        mixed_fleet = await client.get(
            "/platform/fleet/edge-deployments",
            headers={"Cookie": f"__Host-metahub-platform-session={PLATFORM_TOKEN}"},
        )
        assert mixed_fleet.status_code == 200
        no_projection = next(
            item
            for item in mixed_fleet.json()["deployments"]
            if item["deployment_id"] == no_capability_id
        )
        assert no_projection["telemetry_last_receipt_at"] is None
        assert no_projection["telemetry_lag_seconds"] is None
        assert no_projection["telemetry_backlog_seconds"] is None
        assert no_projection["telemetry_cpu_bucket"] == "unknown"
        assert no_projection["telemetry_memory_bucket"] == "unknown"
        assert no_projection["telemetry_error_rate_basis_points"] == 0
        assert no_projection["telemetry_failure_code"] == "none"
        tls["fingerprint"] = fingerprint

    with closing(connect_database(database_url)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM edge_telemetry_batches").fetchone()[0] == 8
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_telemetry_receipts"
        ).fetchone()[0] == 8
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_usage_reconciliations"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_failure_summaries"
        ).fetchone()[0] == 2
        projection = connection.execute(
            "SELECT * FROM edge_fleet_telemetry_projections"
        ).fetchone()
        assert projection is not None
        assert projection["last_bucket_at"] == bucket_end
        assert projection["health_bucket_at"] == bucket_end
        assert projection["operational_bucket_at"] == bucket_end
        assert projection["failure_bucket_at"] == bucket_end
        assert {
            str(row[0])
            for row in connection.execute("SELECT event_type FROM edge_telemetry_events")
        } == {
            "edge.telemetry_received",
            "edge.telemetry_lagging",
            "edge.usage_reconciled",
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
