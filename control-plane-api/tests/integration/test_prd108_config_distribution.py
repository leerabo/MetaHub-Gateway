"""PRD-108 rebuild, signed delivery, owner fencing, and ACK contracts."""

from __future__ import annotations

import copy
import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.config_distribution import routes as config_routes
from metahub_control.config_distribution import service as config_service
from metahub_control.config_distribution.crypto import seal_config_value, sign_config_manifest
from metahub_control.config_distribution.routes import router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_jobs.runtime import run_platform_job_once
from metahub_shared.config_distribution import (
    ConfigDistributionError,
    ConfigSnapshotOwner,
)
from metahub_shared.config_distribution import (
    test_kms_verifier as _test_kms_verifier,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
INTERNAL_TOKEN = "internal-config-test-token-00000001"
PLATFORM_TOKEN = "s" * 43
PLATFORM_CSRF = "c" * 43
OPERATOR_ID = "pto_d108d108d108d108d108d108d108d108"
ACCOUNT_ID = "gac_d108d108d108d108d108d108d108d108"
PRIMARY_GATEWAY = "gateway-prd108-primary"
SECONDARY_GATEWAY = "gateway-prd108-secondary"
WRONG_CELL_GATEWAY = "gateway-prd108-wrong-cell"
PRIMARY_CELL_ID = "gcl_00000000000000000000000000000001"
SECONDARY_CELL_ID = "gcl_00000000000000000000000000000002"


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
        platform_session_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        internal_config_token=INTERNAL_TOKEN,
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


def _context(*permissions: str) -> PlatformContext:
    return PlatformContext(
        platform_session_id="pts_" + "d" * 32,
        operator_id=OPERATOR_ID,
        account_id=ACCOUNT_ID,
        global_session_id="gss_" + "d" * 32,
        factor_id="mfa_" + "d" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=permissions,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    root = tmp_path_factory.mktemp("prd108-config-base")
    database = root / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0101"
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        source = connection.execute(
            "SELECT * FROM global_accounts ORDER BY account_id LIMIT 1"
        ).fetchone()
        assert source is not None
        account = dict(source)
        account.update(
            {
                "account_id": ACCOUNT_ID,
                "email_normalized": "config-distribution-operator@example.invalid",
                "phone_e164": None,
                "display_name": "Config distribution operator",
                "status": "active",
                "login_enabled": 1,
                "failed_login_count": 0,
                "locked_until": None,
                "last_login_at": None,
            }
        )
        columns = tuple(account)
        connection.execute(
            f"INSERT INTO global_accounts({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            tuple(account[column] for column in columns),
        )
        connection.execute(
            """
            INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')
            """,
            (OPERATOR_ID, ACCOUNT_ID),
        )
        connection.execute(
            """
            INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
            VALUES(?,'platform_owner','active',NULL,'2026-08-23T00:00:00Z',NULL)
            """,
            (OPERATOR_ID,),
        )
        owner = connection.execute(
            """
            SELECT binding.workspace_id,binding.tenant_id
            FROM workspace_tenant_bindings binding
            JOIN workspace_operational_states state
              ON state.workspace_id=binding.workspace_id AND state.tenant_id=binding.tenant_id
            WHERE binding.status='active' AND state.status='active'
            ORDER BY binding.workspace_id LIMIT 1
            """
        ).fetchone()
        assert owner is not None
        workspace_id = str(owner["workspace_id"])
        tenant_id = str(owner["tenant_id"])
        connection.execute(
            """
            INSERT INTO workspace_gateway_placements(
              workspace_gateway_placement_id,workspace_id,tenant_id,primary_region_code,
              primary_gateway_cell_id,failover_policy,failover_gateway_cell_id,
              residency_policy_version,revision,snapshot_generation,updated_by_operator_id,
              idempotency_sha256,request_sha256,created_at,updated_at)
            VALUES('wgp_d108d108d108d108d108d108d108d108',?,?,'cn-north-1',?,'deny',NULL,
              'cn-residency-v1',1,1,?,?,?,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')
            """,
            (workspace_id, tenant_id, PRIMARY_CELL_ID, OPERATOR_ID, "1" * 64, "2" * 64),
        )
        for index, (worker, cell_id) in enumerate(
            (
                (PRIMARY_GATEWAY, PRIMARY_CELL_ID),
                (SECONDARY_GATEWAY, PRIMARY_CELL_ID),
                (WRONG_CELL_GATEWAY, SECONDARY_CELL_ID),
            ),
            start=1,
        ):
            connection.execute(
                """
                INSERT INTO gateway_config_snapshot_leases(
                  gateway_config_snapshot_lease_id,gateway_cell_id,worker_id,snapshot_id,
                  snapshot_sha256,signing_key_version,config_watermark,generation,status,
                  expires_at,acquired_at,released_at)
                VALUES(?,?,?,?,?,1,1,1,'active',datetime('now','+1 day'),datetime('now'),NULL)
                """,
                (
                    "gcs_" + f"{index:032x}",
                    cell_id,
                    worker,
                    "lease-snapshot-prd108",
                    f"{index}" * 64,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return database, workspace_id, tenant_id


@pytest.fixture
def runtime(
    base_database: tuple[Path, str, str], tmp_path: Path
) -> tuple[AppConfig, str, str]:
    source, workspace_id, tenant_id = base_database
    database = tmp_path / "runtime.sqlite3"
    shutil.copy2(source, database)
    return _config(_url(database)), workspace_id, tenant_id


def _app(monkeypatch: pytest.MonkeyPatch, config: AppConfig, holder: dict[str, PlatformContext]):
    monkeypatch.setattr(
        config_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: holder["context"],
    )
    monkeypatch.setattr(
        config_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: holder["context"],
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-108 Test",
        extra_cors_allow_headers=("Idempotency-Key", "X-CSRF-Token", "X-Gateway-Identity"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag",),
    )
    app.state.config = config
    app.include_router(router)
    return app


def _platform_headers(*, key: str | None = None, csrf: bool = True) -> dict[str, str]:
    headers = {
        "Cookie": (
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; "
            f"{PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
    }
    if csrf:
        headers["X-CSRF-Token"] = PLATFORM_CSRF
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _gateway_headers(worker: str) -> dict[str, str]:
    return {"X-Internal-Token": INTERNAL_TOKEN, "X-Gateway-Identity": worker}


def _rebuild_payload(workspace_id: str, *, reason: str = "operator_rebuild") -> dict[str, object]:
    return {"workspace_id": workspace_id, "reason_code": reason, "confirmed": True}


@pytest.mark.anyio
async def test_platform_rebuild_auth_csrf_permissions_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, str, str],
) -> None:
    config, workspace_id, _tenant_id = runtime
    holder = {"context": _context("platform.config_distribution.read")}
    app = _app(monkeypatch, config, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        denied = await client.post(
            "/platform/config-rebuilds",
            headers=_platform_headers(key="prd108-platform-denied-0001"),
            json=_rebuild_payload(workspace_id),
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "platform.permission_denied"

        holder["context"] = _context(
            "platform.config_distribution.read", "platform.config_distribution.rebuild"
        )
        no_csrf = await client.post(
            "/platform/config-rebuilds",
            headers=_platform_headers(key="prd108-platform-csrf-0001", csrf=False),
            json=_rebuild_payload(workspace_id),
        )
        assert no_csrf.status_code == 403
        assert no_csrf.json()["code"] == "platform.csrf_invalid"

        created = await client.post(
            "/platform/config-rebuilds",
            headers=_platform_headers(key="prd108-platform-create-0001"),
            json=_rebuild_payload(workspace_id),
        )
        assert created.status_code == 201, created.text
        assert created.json()["rebuild"]["target_generation"] == 1
        replay = await client.post(
            "/platform/config-rebuilds",
            headers=_platform_headers(key="prd108-platform-create-0001"),
            json=_rebuild_payload(workspace_id),
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["rebuild"]["rebuild_job_id"] == created.json()["rebuild"][
            "rebuild_job_id"
        ]
        conflict = await client.post(
            "/platform/config-rebuilds",
            headers=_platform_headers(key="prd108-platform-create-0001"),
            json=_rebuild_payload(workspace_id, reason="consistency_repair"),
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "config_distribution.idempotency_conflict"

        malformed = await client.post(
            "/platform/config-rebuilds",
            headers=_platform_headers(key="prd108-platform-malformed-0001"),
            json={**_rebuild_payload(workspace_id), "manifest": {"secret": "forbidden"}},
        )
        assert malformed.status_code == 422
        assert malformed.json()["code"] == "config_distribution.request_invalid"


@pytest.mark.anyio
async def test_worker_publication_full_delta_etag_owner_and_monotonic_acks(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, str, str],
) -> None:
    config, workspace_id, tenant_id = runtime
    holder = {
        "context": _context(
            "platform.config_distribution.read", "platform.config_distribution.rebuild"
        )
    }
    app = _app(monkeypatch, config, holder)
    verifier = _test_kms_verifier(str(config.kms_test_master_key))
    owner = ConfigSnapshotOwner(
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        cell_code="cn-north-1-a",
        verifier=verifier,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        created = await client.post(
            "/platform/config-rebuilds",
            headers=_platform_headers(key="prd108-publish-first-0001"),
            json=_rebuild_payload(workspace_id),
        )
        assert created.status_code == 201, created.text
        assert run_platform_job_once(config, worker_code="config-worker-prd108") is True

        path = f"/internal/config-snapshots/{workspace_id}"
        missing = await client.get(path, headers={"X-Internal-Token": INTERNAL_TOKEN})
        assert missing.status_code == 403
        assert missing.json()["code"] == "config_distribution.gateway_identity_invalid"
        wrong_cell = await client.get(path, headers=_gateway_headers(WRONG_CELL_GATEWAY))
        assert wrong_cell.status_code == 404
        assert wrong_cell.json()["code"] == "config_distribution.snapshot_unavailable"
        mixed = await client.get(
            path,
            headers={**_gateway_headers(PRIMARY_GATEWAY), "Authorization": "Bearer rejected"},
        )
        assert mixed.status_code == 403

        full = await client.get(path, headers=_gateway_headers(PRIMARY_GATEWAY))
        assert full.status_code == 200, full.text
        assert full.headers["cache-control"] == "no-store"
        first = full.json()
        assert first["kind"] == "full"
        assert first["generation"] == 1
        assert first["manifest"]["tenant_id"] == tenant_id
        assert owner.apply(first) is True
        assert "api_key" not in str(first).lower()
        assert "provider_secret" not in str(first).lower()

        unchanged = await client.get(
            path,
            headers={**_gateway_headers(PRIMARY_GATEWAY), "If-None-Match": full.headers["etag"]},
        )
        assert unchanged.status_code == 304
        assert unchanged.content == b""
        assert unchanged.headers["cache-control"] == "no-store"

        secondary_first = await client.get(path, headers=_gateway_headers(SECONDARY_GATEWAY))
        assert secondary_first.status_code == 200

        connection = connect_database(config.database_url)
        try:
            first_snapshot_id = str(
                connection.execute(
                    "SELECT config_snapshot_id FROM config_snapshots "
                    "WHERE workspace_id=? AND generation=1",
                    (workspace_id,),
                ).fetchone()[0]
            )
        finally:
            connection.close()
        first_ack = await client.post(
            f"/internal/config-snapshots/{first_snapshot_id}/acks",
            headers=_gateway_headers(PRIMARY_GATEWAY),
            json={
                "generation": 1,
                "manifest_sha256": first["manifest_sha256"],
                "status": "applied",
            },
        )
        assert first_ack.status_code == 200, first_ack.text
        assert first_ack.json()["disposition"] == "accepted"
        replay = await client.post(
            f"/internal/config-snapshots/{first_snapshot_id}/acks",
            headers=_gateway_headers(PRIMARY_GATEWAY),
            json={
                "generation": 1,
                "manifest_sha256": first["manifest_sha256"],
                "status": "applied",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True

        second_job = await client.post(
            "/platform/config-rebuilds",
            headers=_platform_headers(key="prd108-publish-second-0001"),
            json=_rebuild_payload(workspace_id),
        )
        assert second_job.status_code == 201, second_job.text
        assert second_job.json()["rebuild"]["target_generation"] == 2
        assert run_platform_job_once(config, worker_code="config-worker-prd108") is True

        delta = await client.get(
            f"{path}?after_generation=1", headers=_gateway_headers(PRIMARY_GATEWAY)
        )
        assert delta.status_code == 200, delta.text
        second = delta.json()
        assert second["kind"] == "delta"
        assert second["from_generation"] == 1
        assert second["generation"] == 2
        assert owner.apply(second) is True

        secondary_second = await client.get(
            f"{path}?after_generation=1", headers=_gateway_headers(SECONDARY_GATEWAY)
        )
        assert secondary_second.status_code == 200
        connection = connect_database(config.database_url)
        try:
            second_snapshot_id = str(
                connection.execute(
                    "SELECT config_snapshot_id FROM config_snapshots "
                    "WHERE workspace_id=? AND generation=2",
                    (workspace_id,),
                ).fetchone()[0]
            )
        finally:
            connection.close()
        second_ack = await client.post(
            f"/internal/config-snapshots/{second_snapshot_id}/acks",
            headers=_gateway_headers(SECONDARY_GATEWAY),
            json={
                "generation": 2,
                "manifest_sha256": second["manifest_sha256"],
                "status": "applied",
            },
        )
        assert second_ack.status_code == 200, second_ack.text
        assert second_ack.json()["applied_generation"] == 2
        late = await client.post(
            f"/internal/config-snapshots/{first_snapshot_id}/acks",
            headers=_gateway_headers(SECONDARY_GATEWAY),
            json={
                "generation": 1,
                "manifest_sha256": first["manifest_sha256"],
                "status": "applied",
            },
        )
        assert late.status_code == 200, late.text
        assert late.json()["disposition"] == "late"
        assert late.json()["applied_generation"] == 2

        distribution = await client.get(
            "/platform/config-distribution", headers=_platform_headers()
        )
        assert distribution.status_code == 200, distribution.text
        item = next(
            row
            for row in distribution.json()["items"]
            if row["workspace_id"] == workspace_id
        )
        assert item["generation"] == 2
        assert item["minimum_applied_generation"] == 1
        assert item["latest_rebuild"]["status"] == "completed"
        assert item["latest_rebuild"]["result_manifest_sha256"] == second["manifest_sha256"]


def test_integrity_failures_fail_closed_and_transport_lkg_is_bounded(
    runtime: tuple[AppConfig, str, str],
) -> None:
    config, workspace_id, tenant_id = runtime
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        manifest_hash = config_service._publish_snapshot(
            connection,
            config,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            generation=1,
            request_id="request-prd108-runtime",
            trace_id="trace-prd108-runtime",
            now=datetime.now(UTC),
        )
        row = connection.execute(
            """
            SELECT head.*,snapshot.manifest_envelope,snapshot.source_sha256,
              snapshot.kms_key_version,snapshot.signature_algorithm,snapshot.signature_b64,
              snapshot.published_at snapshot_published_at
            FROM config_snapshot_heads head JOIN config_snapshots snapshot
              ON snapshot.config_snapshot_id=head.config_snapshot_id
            WHERE head.workspace_id=?
            """,
            (workspace_id,),
        ).fetchone()
        wire = config_service._snapshot_wire(
            connection,
            config,
            row=row,
            after_generation=None,
            request_id="request-prd108-runtime",
            trace_id="trace-prd108-runtime",
        )
        connection.commit()
    finally:
        connection.close()
    assert wire["manifest_sha256"] == manifest_hash

    clock = [datetime.now(UTC)]
    owner = ConfigSnapshotOwner(
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        cell_code="cn-north-1-a",
        verifier=_test_kms_verifier(str(config.kms_test_master_key)),
        now=lambda: clock[0],
    )
    assert owner.apply(wire) is True
    owner.mark_unreachable()
    clock[0] += timedelta(seconds=1)
    assert owner.decision(security_sensitive=True).status == "lkg"
    clock[0] += timedelta(seconds=2)
    assert owner.decision(security_sensitive=True).status == "revocation_stale"
    assert owner.decision(security_sensitive=False).status == "lkg"
    clock[0] += timedelta(seconds=28)
    assert owner.decision(security_sensitive=False).status == "expired"

    tampered = copy.deepcopy(wire)
    tampered["signature"] = ("A" if tampered["signature"][0] != "A" else "B") + tampered[
        "signature"
    ][1:]
    with pytest.raises(ConfigDistributionError) as invalid_signature:
        owner.apply(tampered)
    assert invalid_signature.value.code == "config.signature_invalid"
    assert owner.decision().allowed is False

    invalid_hash_owner = ConfigSnapshotOwner(
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        cell_code="cn-north-1-a",
        verifier=_test_kms_verifier(str(config.kms_test_master_key)),
    )
    tampered_hash = copy.deepcopy(wire)
    tampered_hash["manifest_sha256"] = "0" * 64
    with pytest.raises(ConfigDistributionError) as invalid_hash:
        invalid_hash_owner.apply(tampered_hash)
    assert invalid_hash.value.code == "config.hash_invalid"
    assert invalid_hash_owner.decision().allowed is False

    unavailable = config.model_copy(update={"tenant_data_encryption_secret": None})
    with pytest.raises(ApiError) as kms_unavailable:
        seal_config_value(
            unavailable,
            {"safe": True},
            owner_id="csn_" + "1" * 32,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            kind="manifest",
        )
    assert kms_unavailable.value.code == "config_distribution.kms_unavailable"
    with pytest.raises(ApiError) as signing_unavailable:
        sign_config_manifest(
            unavailable.model_copy(update={"kms_provider": "disabled"}), {"safe": True}
        )
    assert signing_unavailable.value.code == "config_distribution.kms_unavailable"
