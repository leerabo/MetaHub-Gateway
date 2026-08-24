"""PRD-109 low-cardinality telemetry, SLO, budget, alert, and projection contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.observability import routes as observability_routes
from metahub_control.observability import service as observability_service
from metahub_control.observability.routes import router
from metahub_control.observability.scheduler import enqueue_due_observability_jobs
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_jobs.runtime import run_platform_job_once
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.observability import TelemetryBuffer, TelemetryFact
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
PLATFORM_TOKEN = "s" * 43
PLATFORM_CSRF = "c" * 43
OPERATOR_ID = "pto_d109d109d109d109d109d109d109d109"
ACCOUNT_ID = "gac_d109d109d109d109d109d109d109d109"


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
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd109-observability-base")
    database = root / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0104"
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
                "email_normalized": "observability-operator@example.invalid",
                "phone_e164": None,
                "display_name": "Observability operator",
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
            VALUES(?,?,'active',1,NULL,'2026-08-24T00:00:00Z','2026-08-24T00:00:00Z')
            """,
            (OPERATOR_ID, ACCOUNT_ID),
        )
        connection.execute(
            """
            INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
            VALUES(?,'platform_owner','active',NULL,'2026-08-24T00:00:00Z',NULL)
            """,
            (OPERATOR_ID,),
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


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    holder: dict[str, PlatformContext],
):
    monkeypatch.setattr(
        observability_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: holder["context"],
    )
    monkeypatch.setattr(
        observability_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: holder["context"],
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-109 Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag",),
    )
    app.state.config = config
    app.include_router(router)
    return app


def _headers(*, key: str | None = None, if_match: int | None = None) -> dict[str, str]:
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
    if if_match is not None:
        headers["If-Match"] = f'"{if_match}"'
    return headers


def _slo_payload(*, previous_version: int = 0, display_name: str = "Gateway availability"):
    return {
        "service_code": "gateway",
        "slo_code": "gateway.chat.availability",
        "display_name": display_name,
        "signal_class": "reliability",
        "sli_kind": "availability",
        "operation_code": "chat.completions",
        "region_code": "global",
        "window_minutes": 5,
        "target_bps": 9000,
        "latency_threshold_ms": None,
        "burn_warning_bps": 10000,
        "burn_critical_bps": 20000,
        "previous_version": previous_version,
        "reason_code": "slo_create" if previous_version == 0 else "slo_revision",
        "confirmed": True,
    }


def _emit(
    config: AppConfig, *, good: int, bad: int, at: datetime, request_marker: str = "secret"
) -> TelemetryBuffer:
    runtime = TelemetryBuffer(config, service_code="gateway", now=lambda: at)
    for index in range(good):
        assert runtime.enqueue_http(
            operation_code="chat.completions",
            status_code=200,
            duration_ms=40,
            request_id=f"{request_marker}-request-{index}",
            trace_id=f"{request_marker}-trace-{index}",
        )
    for index in range(bad):
        assert runtime.enqueue_http(
            operation_code="chat.completions",
            status_code=500,
            duration_ms=600,
            request_id=f"{request_marker}-bad-request-{index}",
            trace_id=f"{request_marker}-bad-trace-{index}",
        )
    assert runtime.flush_once() is True
    return runtime


def test_telemetry_is_bounded_low_cardinality_and_fail_open(
    runtime: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)
    buffer = TelemetryBuffer(
        runtime, service_code="gateway", capacity=16, batch_size=16, now=lambda: now
    )
    assert buffer.enqueue_http(
        operation_code="prompt.body.secret",
        status_code=200,
        duration_ms=1,
        request_id="raw-request-secret",
        trace_id="raw-trace-secret",
    ) is False
    for index in range(16):
        assert buffer.enqueue_http(
            operation_code="chat.completions",
            status_code=200,
            duration_ms=10,
            request_id=f"request-{index}",
            trace_id=f"trace-{index}",
        ) is True
    assert buffer.enqueue_http(
        operation_code="chat.completions",
        status_code=200,
        duration_ms=10,
        request_id="queue-full-request",
        trace_id="queue-full-trace",
    ) is False
    monkeypatch.setattr(
        buffer,
        "_persist_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError()),
    )
    assert buffer.flush_once() is False
    snapshot = buffer.snapshot()
    assert snapshot.accepted == 16
    assert snapshot.dropped == 18
    assert snapshot.backend_failures == 1
    assert "raw-request-secret" not in repr(buffer)
    with pytest.raises(ValueError, match="telemetry.fact_invalid"):
        TelemetryFact(
            service_code="gateway",
            operation_code="arbitrary.high.cardinality.value",
            region_code="global",
            status_class="2xx",
            duration_bucket="le_10ms",
            bucket_started_at=now,
            request_link_sha256="a" * 64,
            trace_link_sha256="b" * 64,
        )


def test_scheduler_enqueues_one_job_per_minute_watermark(runtime: AppConfig) -> None:
    connection = connect_database(runtime.database_url)
    try:
        connection.execute(
            """
            INSERT INTO service_level_objectives(
              service_level_objective_id,slo_code,version_number,service_catalog_entry_id,
              display_name,signal_class,sli_kind,operation_code,region_code,window_minutes,
              target_bps,latency_threshold_ms,burn_warning_bps,burn_critical_bps,
              definition_sha256,created_by_operator_id,reason_code,request_id,trace_id,created_at)
            VALUES('slo_77777777777777777777777777777777','gateway.scheduler.test',1,
              'sce_00000000000000000000000000000001','Scheduler test','reliability',
              'availability','chat.completions','global',5,9990,NULL,20000,50000,?,?,
              'slo_create','request-prd109-scheduler','trace-prd109-scheduler',
              '2026-08-24T00:00:00Z')
            """,
            ("7" * 64, OPERATOR_ID),
        )
        connection.execute(
            "INSERT INTO service_level_objective_heads VALUES("
            "'slh_77777777777777777777777777777777','gateway.scheduler.test',"
            "'slo_77777777777777777777777777777777',1,1,'2026-08-24T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()
    now = datetime(2026, 8, 24, 8, 0, 8, tzinfo=UTC)
    assert enqueue_due_observability_jobs(runtime, now=now) == 1
    assert enqueue_due_observability_jobs(runtime, now=now + timedelta(seconds=1)) == 0
    connection = connect_database(runtime.database_url)
    try:
        row = connection.execute(
            "SELECT source_code,request_id,trace_id FROM platform_jobs "
            "WHERE type_code='observability.aggregate'"
        ).fetchone()
        assert tuple(row) == (
            "observability.scheduler",
            "observability-scheduler",
            "observability-scheduler",
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_slo_budget_alert_ack_recovery_and_rollout_summary(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    holder = {
        "context": _context(
            "platform.observability.read",
            "platform.observability.manage",
            "platform.observability.alert_ack",
        )
    }
    app = _app(monkeypatch, runtime, holder)
    bucket_time = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)
    _emit(runtime, good=8, bad=2, at=bucket_time, request_marker="must-not-persist")
    connection = connect_database(runtime.database_url)
    try:
        connection.execute(
            """
            INSERT INTO on_call_routes VALUES(
              'ocr_d109d109d109d109d109d109d109d109','gateway','critical',?,
              'disabled',1,'2026-08-24T00:00:00Z','2026-08-24T00:00:00Z')
            """,
            ("a" * 64,),
        )
        connection.commit()
    finally:
        connection.close()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        create = await client.post(
            "/platform/slos",
            headers=_headers(key="prd109-slo-create-0001", if_match=0),
            json=_slo_payload(),
        )
        assert create.status_code == 201, create.text
        created = create.json()
        assert created["slo"]["version_number"] == 1
        assert create.headers["etag"] == '"1"'
        replay = await client.post(
            "/platform/slos",
            headers=_headers(key="prd109-slo-create-0001", if_match=0),
            json=_slo_payload(),
        )
        assert replay.status_code == 200
        assert replay.json()["aggregation_job_id"] == created["aggregation_job_id"]
        changed = _slo_payload(display_name="Changed intent")
        conflict = await client.post(
            "/platform/slos",
            headers=_headers(key="prd109-slo-create-0001", if_match=0),
            json=changed,
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "observability.idempotency_conflict"
        invalid_operation = _slo_payload()
        invalid_operation["operation_code"] = "tenant.dynamic.operation"
        rejected_operation = await client.post(
            "/platform/slos",
            headers=_headers(key="prd109-slo-invalid-op", if_match=0),
            json=invalid_operation,
        )
        assert rejected_operation.status_code == 422
        assert rejected_operation.json()["code"] == "observability.request_invalid"

        assert run_platform_job_once(runtime, worker_code="prd109-aggregate-worker") is True
        budgets = await client.get("/platform/error-budgets", headers=_headers())
        assert budgets.status_code == 200, budgets.text
        body = budgets.json()
        assert body["projection"] == "operations"
        assert body["items"][0]["total_count"] == 10
        assert body["items"][0]["good_count"] == 8
        assert body["items"][0]["allowed_bad_count"] == 1
        assert body["items"][0]["consumed_bad_count"] == 2
        assert body["items"][0]["health_status"] == "exhausted"
        assert body["alerts"][0]["state"] == "suppressed"
        alert = body["alerts"][0]
        ack_payload = {
            "revision": alert["revision"],
            "reason_code": "investigating",
            "confirmed": True,
        }
        ack = await client.post(
            f"/platform/alerts/{alert['alert_id']}/ack",
            headers=_headers(key="prd109-alert-ack-0001"),
            json=ack_payload,
        )
        assert ack.status_code == 201, ack.text
        ack_replay = await client.post(
            f"/platform/alerts/{alert['alert_id']}/ack",
            headers=_headers(key="prd109-alert-ack-0001"),
            json=ack_payload,
        )
        assert ack_replay.status_code == 200
        assert ack_replay.json()["acknowledgement"] == ack.json()["acknowledgement"]

        version_payload = _slo_payload(previous_version=1, display_name="Gateway availability v2")
        versioned = await client.post(
            "/platform/slos",
            headers=_headers(key="prd109-slo-version-0002", if_match=1),
            json=version_payload,
        )
        assert versioned.status_code == 201, versioned.text
        versions = await client.get(
            "/platform/slos?current_only=false", headers=_headers()
        )
        assert [item["is_current"] for item in versions.json()["items"]] == [True, False]

    _emit(runtime, good=100, bad=0, at=bucket_time, request_marker="recovery")
    connection = connect_database(runtime.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        budget = connection.execute(
            "SELECT * FROM error_budgets ORDER BY computed_at DESC LIMIT 1"
        ).fetchone()
        head = connection.execute(
            "SELECT source_watermark FROM telemetry_ingest_head"
        ).fetchone()
        assert budget is not None and head is not None
        observability_service.aggregate_slo_in_transaction(
            connection,
            runtime,
            slo_code="gateway.chat.availability",
            slo_version=1,
            source_watermark=int(head[0]),
            window_ended_at=datetime.fromisoformat(
                str(budget["window_ended_at"]).replace("Z", "+00:00")
            ),
        )
        connection.commit()
        alert_row = connection.execute(
            "SELECT revision,state FROM platform_alerts ORDER BY created_at LIMIT 1"
        ).fetchone()
        assert tuple(alert_row) == (2, "resolved")
        ack_row = connection.execute(
            "SELECT alert_revision FROM alert_acknowledgements"
        ).fetchone()
        assert int(ack_row[0]) == 1
        summary = connection.execute(
            "SELECT * FROM rollout_health_summaries WHERE source_watermark=?",
            (int(head[0]),),
        ).fetchone()
        assert summary is not None
        summary_body = json.dumps(
            {
                "service_code": summary["service_code"],
                "region_code": summary["region_code"],
                "watermark": int(summary["source_watermark"]),
                "health_status": summary["health_status"],
                "error_rate_bps": int(summary["error_rate_bps"]),
                "latency_p95_ms": int(summary["latency_p95_ms"]),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hmac.new(
            runtime.platform_session_secret.encode(),
            f"prd107/health-summary/v1\0{summary_body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert hmac.compare_digest(summary["summary_sha256"], expected)
        serialized = json.dumps(
            [dict(row) for row in connection.execute("SELECT * FROM sli_time_buckets")],
            sort_keys=True,
        )
        assert "must-not-persist" not in serialized
    finally:
        connection.close()


@pytest.mark.anyio
async def test_security_projection_and_strict_transport(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    holder = {"context": _context("platform.observability.security_read")}
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        services = await client.get(
            "/platform/observability/services", headers=_headers()
        )
        assert services.status_code == 200
        assert services.json()["projection"] == "security"
        assert {item["service_code"] for item in services.json()["items"]} == {
            "control",
            "gateway",
        }
        slos = await client.get("/platform/slos", headers=_headers())
        assert slos.status_code == 200
        assert slos.json()["items"] == []
        denied = await client.post(
            "/platform/slos",
            headers=_headers(key="prd109-security-denied", if_match=0),
            json=_slo_payload(),
        )
        assert denied.status_code == 403
        invalid_query = await client.get(
            "/platform/observability/services?tenant_id=tenant-secret",
            headers=_headers(),
        )
        assert invalid_query.status_code == 422
        conflict = await client.get(
            "/platform/slos",
            headers={**_headers(), "Authorization": "Bearer workspace-secret"},
        )
        assert conflict.status_code == 400
        assert conflict.json()["code"] == "platform.credential_conflict"
