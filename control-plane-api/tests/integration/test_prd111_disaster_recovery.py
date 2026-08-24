"""PRD-111 strict API, approval, fencing, and restore-evidence contracts."""

from __future__ import annotations

import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.disaster_recovery import routes as dr_routes
from metahub_control.disaster_recovery import service as dr_service
from metahub_control.disaster_recovery.routes import router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_jobs.runtime import run_platform_job_once
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
PLATFORM_TOKEN = "s" * 43
PLATFORM_CSRF = "c" * 43
OPS_ID = "pto_" + "d111" * 8
SECURITY_ID = "pto_" + "e111" * 8
OPS_ACCOUNT_ID = "gac_" + "d111" * 8
SECURITY_ACCOUNT_ID = "gac_" + "e111" * 8
NORTH_REGION_ID = "reg_00000000000000000000000000000001"
NORTH_TO_EAST_PLAN = "fpl_00000000000000000000000000000001"
BACKUP_ID = "bkp_" + "a" * 32
MANIFEST = "b" * 64


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
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(32),
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


def _context(operator_id: str, account_id: str, role: str, *permissions: str) -> PlatformContext:
    marker = "d" if operator_id == OPS_ID else "e"
    return PlatformContext(
        platform_session_id="pts_" + marker * 32,
        operator_id=operator_id,
        account_id=account_id,
        global_session_id="gss_" + marker * 32,
        factor_id="mfa_" + marker * 32,
        factor_revision=1,
        operator_revision=1,
        roles=(role,),
        permissions=permissions,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


OPS = _context(OPS_ID, OPS_ACCOUNT_ID, "platform_ops", "platform.dr.read", "platform.dr.execute")
SECURITY = _context(
    SECURITY_ID,
    SECURITY_ACCOUNT_ID,
    "platform_security",
    "platform.dr.read",
    "platform.dr.approve",
    "platform.dr.security_review",
)


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd111-dr-base")
    database = root / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0105"
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        source = connection.execute(
            "SELECT * FROM global_accounts ORDER BY account_id LIMIT 1"
        ).fetchone()
        assert source is not None
        for marker, account_id, operator_id, role in (
            ("ops", OPS_ACCOUNT_ID, OPS_ID, "platform_ops"),
            ("security", SECURITY_ACCOUNT_ID, SECURITY_ID, "platform_security"),
        ):
            account = dict(source)
            account.update(
                {
                    "account_id": account_id,
                    "email_normalized": f"dr-{marker}@example.invalid",
                    "phone_e164": None,
                    "display_name": f"DR {marker}",
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
                """INSERT INTO platform_operators(
                     operator_id,account_id,status,revision,created_by_operator_id,
                     created_at,updated_at)
                   VALUES(?,?,'active',1,NULL,datetime('now'),datetime('now'))""",
                (operator_id, account_id),
            )
            connection.execute(
                """INSERT INTO platform_operator_role_bindings(
                     operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                   VALUES(?,?,'active',NULL,datetime('now'),NULL)""",
                (operator_id, role),
            )
        connection.execute(
            """INSERT INTO backup_sets(
                 backup_id,backup_type,scope_kind,workspace_id,tenant_id,status,
                 lease_owner_id,lease_token_sha256,lease_expires_at,source_backend,
                 source_watermark,object_key,size_bytes,content_sha256,manifest_sha256,
                 kms_key_version,schema_head,requested_by_operator_id,idempotency_sha256,
                 request_sha256,request_id,trace_id,error_code,retention_until,
                 created_at,updated_at,completed_at)
               VALUES(?,'full','platform',NULL,NULL,'ready',NULL,NULL,NULL,'sqlite',
                 'ledger:1','backups/prd111/control.enc',1024,?,?,
                 'kms-test-version','0103',?,?,?,'request-prd111-backup',
                 'trace-prd111-backup',NULL,datetime('now','+30 days'),
                 datetime('now','-30 seconds'),datetime('now'),datetime('now'))""",
            (BACKUP_ID, "a" * 64, MANIFEST, OPS_ID, "c" * 64, "d" * 64),
        )
        connection.execute(
            """INSERT INTO backup_restore_verifications(
                 backup_restore_verification_id,backup_id,status,schema_head,integrity_status,
                 manifest_sha256,tenant_owner_sample_count,error_code,started_at,completed_at,
                 created_at,updated_at)
               VALUES(? ,?,'passed','0103','ok',?,1,NULL,datetime('now','-2 seconds'),
                 datetime('now'),datetime('now','-2 seconds'),datetime('now'))""",
            ("brv_" + "a" * 32, BACKUP_ID, MANIFEST),
        )
        binding = connection.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings "
            "ORDER BY workspace_id LIMIT 1"
        ).fetchone()
        north_cell = connection.execute(
            "SELECT gateway_cell_id FROM gateway_cells WHERE region_code='cn-north-1'"
        ).fetchone()
        east_cell = connection.execute(
            "SELECT gateway_cell_id FROM gateway_cells WHERE region_code='cn-east-1'"
        ).fetchone()
        assert binding is not None and north_cell is not None and east_cell is not None
        connection.execute(
            """INSERT INTO workspace_gateway_placements(
                 workspace_gateway_placement_id,workspace_id,tenant_id,primary_region_code,
                 primary_gateway_cell_id,failover_policy,failover_gateway_cell_id,
                 residency_policy_version,revision,snapshot_generation,updated_by_operator_id,
                 idempotency_sha256,request_sha256,created_at,updated_at)
               VALUES(?,?,?,'cn-north-1',?,'explicit',?,'test-cn-main-v1',1,1,?,?,?,
                 datetime('now'),datetime('now'))""",
            (
                "wgp_" + "d111" * 8,
                binding["workspace_id"],
                binding["tenant_id"],
                north_cell[0],
                east_cell[0],
                OPS_ID,
                "e" * 64,
                "f" * 64,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> AppConfig:
    database = tmp_path / "runtime.sqlite3"
    shutil.copy2(base_database, database)
    return _config(_url(database))


def _app(monkeypatch: pytest.MonkeyPatch, config: AppConfig, holder: dict[str, object]):
    monkeypatch.setattr(
        dr_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: holder["platform"],
    )
    monkeypatch.setattr(
        dr_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: holder["platform"],
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-111 Test",
        extra_cors_allow_headers=("Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def _headers(*, key: str | None = None) -> dict[str, str]:
    headers = {
        "Cookie": (
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; {PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": PLATFORM_CSRF,
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _payload(*, reason: str = "quarterly_drill", epoch: int = 0) -> dict[str, object]:
    return {
        "failover_plan_id": NORTH_TO_EAST_PLAN,
        "plan_version": 1,
        "reason_code": reason,
        "expected_epoch": epoch,
        "confirmed": True,
    }


async def _approve(
    client: AsyncClient,
    holder: dict[str, object],
    endpoint: str,
    payload: dict[str, object],
    marker: str,
):
    holder["platform"] = OPS
    first = await client.post(
        endpoint,
        headers=_headers(key=f"prd111-{marker}-execution-0001"),
        json=payload,
    )
    assert first.status_code == 201, first.text
    assert first.json()["run"]["status"] == "planned"
    assert [item["approval_kind"] for item in first.json()["run"]["approvals"]] == ["execution"]

    holder["platform"] = SECURITY
    second = await client.post(
        endpoint,
        headers=_headers(key=f"prd111-{marker}-security-0001"),
        json=payload,
    )
    assert second.status_code == 201, second.text
    assert second.json()["run"]["status"] == "approved"
    assert {item["approval_kind"] for item in second.json()["run"]["approvals"]} == {
        "execution",
        "security_review",
    }
    return second.json()["run"]


@pytest.mark.anyio
async def test_status_backup_projection_and_transport_are_closed(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    holder: dict[str, object] = {"platform": OPS}
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        status = await client.get("/platform/dr/status", headers=_headers())
        assert status.status_code == 200, status.text
        assert len(status.json()["regions"]) == 2
        assert len(status.json()["plans"]) == 2
        assert status.json()["objectives"] == {
            "control_rpo_seconds": 900,
            "control_rto_seconds": 7200,
            "gateway_rto_seconds": 600,
            "latest_recovery_age_seconds": status.json()["objectives"][
                "latest_recovery_age_seconds"
            ],
            "recovery_ready": True,
        }
        backups = await client.get("/platform/backups", headers=_headers())
        assert backups.status_code == 200, backups.text
        assert backups.json()["total"] == 1
        wire = backups.text
        assert BACKUP_ID in wire and MANIFEST in wire
        for forbidden in ("object_key", "kms_key_version", "source_watermark", "tenant_id"):
            assert forbidden not in wire

        conflict_headers = _headers()
        conflict_headers["Authorization"] = "Bearer workspace-token"
        conflict = await client.get("/platform/dr/status", headers=conflict_headers)
        assert conflict.status_code == 400
        assert conflict.json()["code"] == "platform.credential_conflict"


@pytest.mark.anyio
async def test_drill_requires_independent_votes_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    holder: dict[str, object] = {"platform": OPS}
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        payload = _payload()
        first = await client.post(
            "/platform/dr/drills",
            headers=_headers(key="prd111-drill-execution-0001"),
            json=payload,
        )
        assert first.status_code == 201, first.text
        replay = await client.post(
            "/platform/dr/drills",
            headers=_headers(key="prd111-drill-execution-0001"),
            json=payload,
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        conflict = await client.post(
            "/platform/dr/drills",
            headers=_headers(key="prd111-drill-execution-0001"),
            json={**payload, "reason_code": "control_recovery"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "dr.idempotency_conflict"

        holder["platform"] = SECURITY
        second = await client.post(
            "/platform/dr/drills",
            headers=_headers(key="prd111-drill-security-0001"),
            json=payload,
        )
        assert second.status_code == 201, second.text
        run_id = second.json()["run"]["disaster_recovery_run_id"]
        connection = connect_database(runtime.database_url)
        try:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(ApiError) as tampered:
                dr_service.precheck_run_in_transaction(
                    connection,
                    run_id=run_id,
                    expected_epoch=0,
                    plan_manifest_sha256="f" * 64,
                )
            assert tampered.value.code == "dr.plan_unavailable"
            connection.rollback()
        finally:
            connection.close()
        assert run_platform_job_once(runtime, worker_code="prd111-drill-worker") is True
        status = await client.get("/platform/dr/status", headers=_headers())
        run = next(
            item
            for item in status.json()["recent_runs"]
            if item["disaster_recovery_run_id"] == run_id
        )
        assert run["status"] == "succeeded"
        assert run["checkpoint"] == "complete"
        assert len(run["restore_evidence"]) >= 6
        assert {item["rpo_outcome"] for item in run["restore_evidence"]} == {"met"}


@pytest.mark.anyio
async def test_failover_advances_writer_epoch_and_gateway_placement(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    holder: dict[str, object] = {"platform": OPS}
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        run = await _approve(
            client,
            holder,
            f"/platform/regions/{NORTH_REGION_ID}/failovers",
            _payload(reason="regional_outage"),
            "failover",
        )
        assert run_platform_job_once(runtime, worker_code="prd111-failover-worker") is True
        connection = connect_database(runtime.database_url)
        try:
            regions = {
                row["region_code"]: tuple(row)[1:]
                for row in connection.execute(
                    "SELECT region_code,writer_status,failover_epoch FROM platform_regions"
                ).fetchall()
            }
            assert regions == {
                "cn-north-1": ("fenced", 1),
                "cn-east-1": ("active", 1),
            }
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM region_writer_leases WHERE status='active'"
                ).fetchone()[0]
                == 1
            )
            placement = connection.execute(
                "SELECT primary_region_code FROM workspace_gateway_placements "
                "ORDER BY workspace_id LIMIT 1"
            ).fetchone()
            assert placement[0] == "cn-east-1"
            events = {
                row[0]
                for row in connection.execute(
                    "SELECT event_type FROM dr_event_outbox WHERE disaster_recovery_run_id=?",
                    (run["disaster_recovery_run_id"],),
                ).fetchall()
            }
            assert {"region.failover_started", "region.failover_completed"} <= events
        finally:
            connection.close()

        holder["platform"] = OPS
        stale = await client.post(
            f"/platform/regions/{NORTH_REGION_ID}/failovers",
            headers=_headers(key="prd111-failover-stale-0001"),
            json=_payload(reason="regional_outage"),
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "dr.epoch_conflict"


@pytest.mark.anyio
async def test_dynamic_revoke_fails_before_any_region_effect(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    holder: dict[str, object] = {"platform": OPS}
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        run = await _approve(
            client,
            holder,
            "/platform/dr/drills",
            _payload(),
            "revoked",
        )
        connection = connect_database(runtime.database_url)
        try:
            connection.execute(
                """UPDATE platform_operator_role_bindings
                   SET status='revoked',revoked_at=datetime('now')
                   WHERE operator_id=? AND role_code='platform_security' AND status='active'""",
                (SECURITY_ID,),
            )
            connection.commit()
        finally:
            connection.close()
        assert run_platform_job_once(runtime, worker_code="prd111-revocation-worker") is True
        connection = connect_database(runtime.database_url)
        try:
            result = connection.execute(
                "SELECT status,safe_outcome_code FROM disaster_recovery_runs "
                "WHERE disaster_recovery_run_id=?",
                (run["disaster_recovery_run_id"],),
            ).fetchone()
            assert tuple(result) == ("failed", "dr.approval_required")
            assert (
                connection.execute("SELECT COUNT(*) FROM region_writer_leases").fetchone()[0] == 0
            )
        finally:
            connection.close()
