"""PRD-113 strict API, runner fencing, regression, and economics contracts."""

from __future__ import annotations

import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.capacity_management import routes as capacity_routes
from metahub_control.capacity_management import service as capacity_service
from metahub_control.capacity_management.routes import router
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_jobs.runtime import run_platform_job_once
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
PLATFORM_TOKEN = "s" * 43
PLATFORM_CSRF = "c" * 43
OPS_ID = "pto_" + "a113" * 8
SECURITY_ID = "pto_" + "b113" * 8
BILLING_ID = "pto_" + "c113" * 8
OPS_ACCOUNT_ID = "gac_" + "a113" * 8
SECURITY_ACCOUNT_ID = "gac_" + "b113" * 8
BILLING_ACCOUNT_ID = "gac_" + "c113" * 8


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
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
        capacity_runner_mode="test_loopback",
    )


def _context(
    operator_id: str, account_id: str, role: str, *permissions: str
) -> PlatformContext:
    marker = operator_id[4]
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


OPS = _context(
    OPS_ID,
    OPS_ACCOUNT_ID,
    "platform_ops",
    "platform.capacity.read",
    "platform.capacity.test",
    "platform.capacity.chaos",
)
SECURITY = _context(
    SECURITY_ID,
    SECURITY_ACCOUNT_ID,
    "platform_security",
    "platform.capacity.read",
    "platform.capacity.chaos",
)
BILLING = _context(
    BILLING_ID,
    BILLING_ACCOUNT_ID,
    "platform_billing",
    "platform.unit_economics.read",
)


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd113-capacity-base")
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
            ("billing", BILLING_ACCOUNT_ID, BILLING_ID, "platform_billing"),
        ):
            account = dict(source)
            account.update(
                {
                    "account_id": account_id,
                    "email_normalized": f"capacity-{marker}@example.invalid",
                    "phone_e164": None,
                    "display_name": f"Capacity {marker}",
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
        capacity_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: holder["platform"],
    )
    monkeypatch.setattr(
        capacity_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: holder["platform"],
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-113 Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def _headers(*, key: str | None = None, revision: int | None = None) -> dict[str, str]:
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


def test_capacity_role_projection_and_navigation_are_permission_driven(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    service = platform_access_service
    expected = {
        OPS_ID: {
            "platform.capacity.read",
            "platform.capacity.test",
            "platform.capacity.chaos",
        },
        SECURITY_ID: {"platform.capacity.read", "platform.capacity.chaos"},
        BILLING_ID: {"platform.unit_economics.read"},
    }
    contexts = {OPS_ID: OPS, SECURITY_ID: SECURITY, BILLING_ID: BILLING}
    connection = connect_database(runtime.database_url)
    try:
        for operator_id, expected_permissions in expected.items():
            _, permissions, fingerprint = service._authority(connection, operator_id)
            projected = {
                permission
                for permission in permissions
                if permission.startswith("platform.capacity.")
                or permission == "platform.unit_economics.read"
            }
            assert projected == expected_permissions
            assert len(fingerprint) == 64
    finally:
        connection.close()

    for operator_id, context in contexts.items():
        monkeypatch.setattr(
            service,
            "_authenticate_platform",
            lambda *args, _context=context, **kwargs: _context,
        )
        navigation = service.get_platform_navigation(
            runtime,
            session_token=PLATFORM_TOKEN,
            request_id=f"request-navigation-{operator_id}",
            trace_id=f"trace-navigation-{operator_id}",
        )
        assert "capacity" in {item.id for item in navigation.items}

    monkeypatch.setattr(
        service,
        "_authenticate_platform",
        lambda *args, **kwargs: _context(
            OPS_ID, OPS_ACCOUNT_ID, "platform_ops", "platform.capacity.test"
        ),
    )
    revoked = service.get_platform_navigation(
        runtime,
        session_token=PLATFORM_TOKEN,
        request_id="request-navigation-revoked",
        trace_id="trace-navigation-revoked",
    )
    assert "capacity" not in {item.id for item in revoked.items}


@pytest.mark.anyio
async def test_capacity_forecast_cas_idempotency_and_runtime(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    holder: dict[str, object] = {"platform": OPS}
    app = _app(monkeypatch, runtime, holder)
    payload = {
        "service_code": "gateway",
        "region_code": "cn-north-1",
        "horizon_days": 30,
        "model_code": "linear-v1",
        "confirmed": True,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        status = await client.get("/platform/capacity", headers=_headers())
        assert status.status_code == 200, status.text
        assert len(status.json()["services"]) == 4
        assert "tenant_id" not in status.text and "internal_endpoint" not in status.text

        created = await client.post(
            "/platform/capacity/forecasts",
            headers=_headers(key="prd113-forecast-create-0001", revision=1),
            json=payload,
        )
        assert created.status_code == 201, created.text
        assert created.json()["forecast"]["status"] == "queued"
        replay = await client.post(
            "/platform/capacity/forecasts",
            headers=_headers(key="prd113-forecast-create-0001", revision=1),
            json=payload,
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        conflict = await client.post(
            "/platform/capacity/forecasts",
            headers=_headers(key="prd113-forecast-create-0001", revision=1),
            json={**payload, "horizon_days": 90},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "capacity.idempotency_conflict"

        assert run_platform_job_once(runtime, worker_code="prd113-forecast-worker") is True
        refreshed = await client.get("/platform/capacity", headers=_headers())
        forecast = refreshed.json()["forecasts"][0]
        assert forecast["status"] == "succeeded"
        assert forecast["predicted_demand_rps"] >= 800
        assert forecast["confidence_bps"] == 8500


@pytest.mark.anyio
async def test_fixed_load_scenario_creates_aggregate_baseline(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    holder: dict[str, object] = {"platform": OPS}
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        created = await client.post(
            "/platform/load-tests",
            headers=_headers(key="prd113-load-create-0001"),
            json={
                "scenario_code": "gateway-release",
                "environment_class": "staging",
                "region_code": "cn-north-1",
                "synthetic_tenant_count": 100,
                "target_rps": 1600,
                "duration_seconds": 60,
                "confirmed": True,
            },
        )
        assert created.status_code == 201, created.text
        assert run_platform_job_once(runtime, worker_code="prd113-load-worker") is True
        status = await client.get("/platform/capacity", headers=_headers())
        run = status.json()["load_tests"][0]
        assert run["status"] == "succeeded"
        assert run["regression_status"] == "no_baseline"
        assert run["controlled_degradation"] is True
        assert run["open_connection_delta"] == 0
        assert len(status.json()["baselines"]) == 1

        arbitrary = await client.post(
            "/platform/load-tests",
            headers=_headers(key="prd113-load-arbitrary-0001"),
            json={
                "scenario_code": "https://not-allowed.invalid/script.js",
                "environment_class": "staging",
                "region_code": "cn-north-1",
                "synthetic_tenant_count": 1,
                "target_rps": 1,
                "duration_seconds": 10,
                "confirmed": True,
            },
        )
        assert arbitrary.status_code == 422
        assert arbitrary.json()["code"] == "capacity.request_invalid"


@pytest.mark.anyio
async def test_production_chaos_requires_distinct_current_approvals(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    holder: dict[str, object] = {"platform": OPS}
    app = _app(monkeypatch, runtime, holder)
    payload = {
        "action": "create",
        "fault_code": "dependency-timeout",
        "service_code": "gateway",
        "environment_class": "production",
        "region_code": "cn-north-1",
        "blast_tenant_count": 10,
        "duration_seconds": 60,
        "objective_p95_ms": 2000,
        "objective_error_rate_bps": 100,
        "stop_error_rate_bps": 500,
        "reason_code": "resilience_validation",
        "confirmed": True,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        created = await client.post(
            "/platform/chaos-experiments",
            headers=_headers(key="prd113-chaos-create-0001"),
            json=payload,
        )
        assert created.status_code == 201, created.text
        experiment_id = created.json()["experiment"]["chaos_experiment_id"]
        assert created.json()["experiment"]["status"] == "pending_approval"

        self_approval = await client.post(
            "/platform/chaos-experiments",
            headers=_headers(key="prd113-chaos-self-approve-0001", revision=1),
            json={
                "action": "approve",
                "chaos_experiment_id": experiment_id,
                "reason_code": "release_validation",
                "confirmed": True,
            },
        )
        assert self_approval.status_code == 409
        assert self_approval.json()["code"] == "capacity.approver_conflict"

        holder["platform"] = SECURITY
        approved = await client.post(
            "/platform/chaos-experiments",
            headers=_headers(key="prd113-chaos-security-0001", revision=1),
            json={
                "action": "approve",
                "chaos_experiment_id": experiment_id,
                "reason_code": "release_validation",
                "confirmed": True,
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["experiment"]["status"] == "queued"
        assert len(approved.json()["experiment"]["approvals"]) == 2

        assert run_platform_job_once(runtime, worker_code="prd113-chaos-worker") is True
        status = await client.get("/platform/capacity", headers=_headers())
        experiment = next(
            item
            for item in status.json()["chaos_experiments"]
            if item["chaos_experiment_id"] == experiment_id
        )
        assert experiment["status"] == "succeeded"
        assert experiment["objective_met"] is True


@pytest.mark.anyio
async def test_revoked_approval_and_kill_fence_stop_effects(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    holder: dict[str, object] = {"platform": OPS}
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        created = await client.post(
            "/platform/chaos-experiments",
            headers=_headers(key="prd113-chaos-revoke-create-0001"),
            json={
                "action": "create",
                "fault_code": "redis-unavailable",
                "service_code": "control",
                "environment_class": "production",
                "region_code": "cn-north-1",
                "blast_tenant_count": 5,
                "duration_seconds": 30,
                "objective_p95_ms": 500,
                "objective_error_rate_bps": 100,
                "stop_error_rate_bps": 400,
                "reason_code": "resilience_validation",
                "confirmed": True,
            },
        )
        experiment_id = created.json()["experiment"]["chaos_experiment_id"]
        holder["platform"] = SECURITY
        approved = await client.post(
            "/platform/chaos-experiments",
            headers=_headers(key="prd113-chaos-revoke-security-0001", revision=1),
            json={
                "action": "approve",
                "chaos_experiment_id": experiment_id,
                "reason_code": "release_validation",
                "confirmed": True,
            },
        )
        assert approved.status_code == 200, approved.text
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
        assert run_platform_job_once(runtime, worker_code="prd113-chaos-revoked") is True
        connection = connect_database(runtime.database_url)
        try:
            failed = connection.execute(
                "SELECT status,stop_reason_code FROM chaos_experiments "
                "WHERE chaos_experiment_id=?",
                (experiment_id,),
            ).fetchone()
            assert tuple(failed) == ("failed", "runner_unavailable")
        finally:
            connection.close()

        holder["platform"] = OPS
        staged = await client.post(
            "/platform/chaos-experiments",
            headers=_headers(key="prd113-chaos-kill-create-0001"),
            json={
                "action": "create",
                "fault_code": "resource-pressure",
                "service_code": "gateway",
                "environment_class": "staging",
                "region_code": "cn-north-1",
                "blast_tenant_count": 5,
                "duration_seconds": 30,
                "objective_p95_ms": 2000,
                "objective_error_rate_bps": 100,
                "stop_error_rate_bps": 500,
                "reason_code": "release_validation",
                "confirmed": True,
            },
        )
        staged_id = staged.json()["experiment"]["chaos_experiment_id"]
        killed = await client.post(
            "/platform/chaos-experiments",
            headers=_headers(key="prd113-chaos-kill-0001", revision=1),
            json={
                "action": "kill",
                "chaos_experiment_id": staged_id,
                "reason_code": "operator_containment",
                "confirmed": True,
            },
        )
        assert killed.status_code == 200, killed.text
        assert killed.json()["experiment"]["status"] == "killed"
        assert killed.json()["experiment"]["kill_generation"] == 1


@pytest.mark.anyio
async def test_unit_economics_is_billing_only_aggregate(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    connection = connect_database(runtime.database_url)
    try:
        connection.execute(
            """INSERT INTO platform_cost_allocations VALUES(
                 'pca_dddddddddddddddddddddddddddddddd','2026-08','gateway','cn-north-1',
                 'compute',2500000,'CNY',?,1,datetime('now'))""",
            ("d" * 64,),
        )
        connection.commit()
    finally:
        connection.close()
    holder: dict[str, object] = {"platform": BILLING}
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        result = await client.get(
            "/platform/unit-economics?billing_period=2026-08&currency=CNY",
            headers=_headers(),
        )
        assert result.status_code == 200, result.text
        assert result.json()["total"] == 1
        item = result.json()["items"][0]
        assert item["allocated_cost_micros"] == 2_500_000
        assert item["currency"] == "CNY"
        for forbidden in ("tenant_id", "email", "provider_key", "raw_usage"):
            assert forbidden not in result.text

        holder["platform"] = OPS
        denied = await client.get("/platform/unit-economics", headers=_headers())
        assert denied.status_code == 403
        assert denied.json()["code"] == "platform.permission_denied"
