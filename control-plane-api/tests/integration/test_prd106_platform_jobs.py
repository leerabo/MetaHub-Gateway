"""PRD-106 API isolation, crypto, fencing, cancellation and runtime recovery."""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_jobs import routes as job_routes
from metahub_control.platform_jobs import service as job_service
from metahub_control.platform_jobs.crypto import decrypt_job_value, encrypt_job_value
from metahub_control.platform_jobs.models import PlatformJobActionRequest
from metahub_control.platform_jobs.routes import router
from metahub_control.platform_jobs.runtime import (
    PlatformJobCancelled,
    PlatformJobContext,
    claim_platform_job,
    run_platform_job_once,
)
from metahub_control.platform_jobs.supervisor import install_platform_job_supervisor
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ERROR_SPECS, ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
ORIGIN = "http://localhost:5173"
OPERATOR_ID = "pto_" + "6" * 32
TEST_PLATFORM_SESSION = "s" * 43
TEST_PLATFORM_CSRF = "c" * 43


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd106-runtime-base")
    database = root / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0101"
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        account_id = connection.execute(
            "SELECT account_id FROM global_accounts ORDER BY created_at LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
              VALUES(?,?,'active',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')""",
            (OPERATOR_ID, account_id),
        )
        connection.execute(
            """INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
              VALUES(?,'platform_ops','active',NULL,'2026-08-23T00:00:00Z',NULL)""",
            (OPERATOR_ID,),
        )
        connection.commit()
    finally:
        connection.close()
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> tuple[AppConfig, PlatformContext]:
    database = tmp_path / "runtime.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    connection = connect_database(config.database_url)
    try:
        account_id = connection.execute(
            "SELECT account_id FROM platform_operators WHERE operator_id=?", (OPERATOR_ID,)
        ).fetchone()[0]
    finally:
        connection.close()
    return config, _context(account_id)


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _config(database_url: str, *, schedulers: bool = False) -> AppConfig:
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
        control_background_schedulers_enabled=schedulers,
    )


def _context(account_id: str, *permissions: str) -> PlatformContext:
    granted = permissions or (
        "platform.job.read",
        "platform.job.manage",
        "platform.job.type_manage",
    )
    return PlatformContext(
        platform_session_id="pts_" + "6" * 32,
        operator_id=OPERATOR_ID,
        account_id=account_id,
        global_session_id="gss_" + "6" * 32,
        factor_id="mfa_" + "6" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_ops",),
        permissions=tuple(granted),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _enqueue(config: AppConfig, *, marker: str, outcome: str = "success") -> str:
    job_id, replay = job_service.enqueue_platform_job(
        config,
        type_code="platform.runtime.probe",
        payload={"marker": marker, "outcome": outcome},
        priority=50,
        source_code="platform.runtime.test",
        idempotency_key=f"prd106-{marker}-idempotency-key",
        request_id=f"request-{marker}",
        trace_id=f"trace-{marker}",
    )
    assert replay is False
    return job_id


def _app(monkeypatch: pytest.MonkeyPatch, config: AppConfig, context: PlatformContext):
    monkeypatch.setattr(
        job_routes, "authenticate_platform_session_context", lambda *args, **kwargs: context
    )
    monkeypatch.setattr(
        job_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: context,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-106 Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag",),
    )
    app.state.config = config
    app.include_router(router)
    return app


def _cookie_headers(
    *, workspace: bool = False, support: bool = False
) -> dict[str, str]:
    values = [
        f"{PLATFORM_SESSION_COOKIE}={TEST_PLATFORM_SESSION}",
        f"{PLATFORM_CSRF_COOKIE}={TEST_PLATFORM_CSRF}",
    ]
    if workspace:
        values.append(f"{WORKSPACE_SESSION_COOKIE}=workspace-session-must-not-cross")
    if support:
        values.append(f"{SUPPORT_SESSION_COOKIE}=support-session-must-not-cross")
    return {"Cookie": "; ".join(values)}


def _write_headers(**values: str) -> dict[str, str]:
    return {
        **_cookie_headers(),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": TEST_PLATFORM_CSRF,
        **values,
    }


def _p95(values: list[float]) -> float:
    assert values
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


@pytest.mark.anyio
async def test_platform_api_is_strict_idempotent_and_credential_isolated(
    monkeypatch: pytest.MonkeyPatch, runtime: tuple[AppConfig, PlatformContext]
) -> None:
    config, context = runtime
    job_id = _enqueue(config, marker="api-cancel")
    app = _app(monkeypatch, config, context)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        queues = await client.get(
            "/platform/job-queues?limit=20", headers=_cookie_headers()
        )
        assert queues.status_code == 200
        assert queues.headers["cache-control"] == "no-store, private"
        body = queues.json()
        assert len(body["queues"]) == 3
        assert body["recent_jobs"][0]["job_id"] == job_id
        assert "input_envelope" not in json.dumps(body)

        detail = await client.get(
            f"/platform/jobs/{job_id}", headers=_cookie_headers()
        )
        assert detail.status_code == 200
        assert detail.headers["etag"] == '"1"'
        headers = _write_headers(
            **{"If-Match": '"1"', "Idempotency-Key": "prd106-api-cancel-command"}
        )
        cancelled = await client.post(
            f"/platform/jobs/{job_id}/cancel",
            headers=headers,
            json={"reason_code": "operator_cancelled", "confirmed": True},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["job"]["status"] == "cancelled"
        replay = await client.post(
            f"/platform/jobs/{job_id}/cancel",
            headers=headers,
            json={"reason_code": "operator_cancelled", "confirmed": True},
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True

        malformed = await client.post(
            f"/platform/jobs/{job_id}/retry",
            headers=_write_headers(
                **{"If-Match": '"2"', "Idempotency-Key": "prd106-api-invalid-command"}
            ),
            json={"reason_code": "operator_retry", "confirmed": True, "payload": {}},
        )
        assert malformed.status_code == 422
        conflicting_headers = (
            _cookie_headers(workspace=True),
            _cookie_headers(support=True),
            {**_cookie_headers(), "Authorization": "Bearer test-workspace-token"},
        )
        for conflict_headers in conflicting_headers:
            crossover = await client.get(
                "/platform/job-queues", headers=conflict_headers
            )
            assert crossover.status_code == 400
            assert crossover.json()["code"] == "platform.credential_conflict"


@pytest.mark.anyio
async def test_revocation_precedes_body_parsing(
    monkeypatch: pytest.MonkeyPatch, runtime: tuple[AppConfig, PlatformContext]
) -> None:
    config, context = runtime
    denied = _context(context.account_id, "platform.job.read")
    app = _app(monkeypatch, config, denied)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        response = await client.post(
            "/platform/job-types",
            headers={**_write_headers(), "Content-Type": "application/json"},
            content=b"not-json",
        )
    assert response.status_code == 403
    assert response.json()["code"] == "platform.permission_denied"


def test_job_authority_extension_and_navigation_are_permission_driven(
    monkeypatch: pytest.MonkeyPatch, runtime: tuple[AppConfig, PlatformContext]
) -> None:
    config, context = runtime
    connection = connect_database(config.database_url)
    try:
        roles, permissions, fingerprint = platform_access_service._authority(
            connection, OPERATOR_ID
        )
    finally:
        connection.close()
    assert roles == ("platform_ops",)
    assert {
        "platform.job.read",
        "platform.job.manage",
        "platform.job.type_manage",
    } <= set(permissions)
    assert len(fingerprint) == 64

    monkeypatch.setattr(
        platform_access_service,
        "_authenticate_platform",
        lambda *args, **kwargs: context,
    )
    navigation = platform_access_service.get_platform_navigation(
        config,
        session_token=TEST_PLATFORM_SESSION,
        request_id="request-job-navigation",
        trace_id="trace-job-navigation",
    )
    assert "job-runtime" in {item.id for item in navigation.items}

    monkeypatch.setattr(
        platform_access_service,
        "_authenticate_platform",
        lambda *args, **kwargs: replace(context, permissions=()),
    )
    revoked = platform_access_service.get_platform_navigation(
        config,
        session_token=TEST_PLATFORM_SESSION,
        request_id="request-job-navigation-revoked",
        trace_id="trace-job-navigation-revoked",
    )
    assert "job-runtime" not in {item.id for item in revoked.items}


def test_platform_job_error_contract_is_complete() -> None:
    expected_statuses = {
        "platform_job.request_invalid": 422,
        "platform_job.not_found": 404,
        "platform_job.type_unavailable": 409,
        "platform_job.conflict": 409,
        "platform_job.revision_conflict": 412,
        "platform_job.idempotency_conflict": 409,
        "platform_job.effect_conflict": 409,
        "platform_job.lease_lost": 409,
        "platform_job.input_invalid": 422,
        "platform_job.kms_unavailable": 503,
        "platform_job.unavailable": 503,
        "platform_job.response_invalid": 502,
    }
    assert {
        code: ERROR_SPECS[code].http_status for code in expected_statuses
    } == expected_statuses
    assert all(ERROR_SPECS[code].owner == "platform_job" for code in expected_statuses)


def test_crypto_tamper_and_kms_outage_are_distinct(
    runtime: tuple[AppConfig, PlatformContext],
) -> None:
    config, _context_value = runtime
    prepared = encrypt_job_value(
        config,
        {"marker": "crypto"},
        kind="input",
        owner_id="pjob_" + "1" * 32,
        type_code="platform.runtime.probe",
    )
    document = json.loads(prepared.envelope)
    document["ciphertext"] = ("A" if document["ciphertext"][0] != "A" else "B") + document[
        "ciphertext"
    ][1:]
    tampered = json.dumps(document, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ApiError) as invalid:
        decrypt_job_value(
            config,
            tampered,
            expected_sha256=prepared.plaintext_sha256,
            kind="input",
            owner_id="pjob_" + "1" * 32,
            type_code="platform.runtime.probe",
        )
    assert invalid.value.code == "platform_job.input_invalid"
    unavailable_config = config.model_copy(update={"kms_provider": "disabled"})
    with pytest.raises(ApiError) as unavailable:
        decrypt_job_value(
            unavailable_config,
            prepared.envelope,
            expected_sha256=prepared.plaintext_sha256,
            kind="input",
            owner_id="pjob_" + "1" * 32,
            type_code="platform.runtime.probe",
        )
    assert unavailable.value.code == "platform_job.kms_unavailable"


def test_probe_checkpoint_effect_and_enqueue_idempotency(
    runtime: tuple[AppConfig, PlatformContext]
) -> None:
    config, _context_value = runtime
    job_id = _enqueue(config, marker="probe-success")
    repeated, replay = job_service.enqueue_platform_job(
        config,
        type_code="platform.runtime.probe",
        payload={"marker": "probe-success", "outcome": "success"},
        priority=50,
        source_code="platform.runtime.test",
        idempotency_key="prd106-probe-success-idempotency-key",
        request_id="another-request",
        trace_id="another-trace",
    )
    assert (repeated, replay) == (job_id, True)
    assert run_platform_job_once(config, worker_code="prd106-success-worker")
    connection = connect_database(config.database_url)
    try:
        job = connection.execute(
            "SELECT status,safe_outcome_code FROM platform_jobs WHERE platform_job_id=?",
            (job_id,),
        ).fetchone()
        assert tuple(job) == ("succeeded", "platform_job.probe_completed")
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_job_checkpoints WHERE platform_job_id=?", (job_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_job_effects WHERE platform_job_id=?", (job_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_job_event_outbox WHERE platform_job_id=?", (job_id,)
        ).fetchone()[0] == 2
    finally:
        connection.close()


@pytest.mark.anyio
async def test_enqueue_and_idle_queue_start_performance(
    runtime: tuple[AppConfig, PlatformContext],
) -> None:
    config, _context_value = runtime
    active_config = config.model_copy(
        update={"control_background_schedulers_enabled": True}
    )
    app = create_service_app("control-plane-api", "PRD-106 Performance Test")
    app.state.config = active_config
    install_platform_job_supervisor(app)

    idle_start_durations: list[float] = []
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.6)
        for sample in range(5):
            started = time.perf_counter()
            job_id = _enqueue(active_config, marker=f"idle-start-{sample}")
            deadline = started + 10
            while time.perf_counter() < deadline:
                connection = connect_database(active_config.database_url)
                try:
                    row = connection.execute(
                        "SELECT status,started_at FROM platform_jobs WHERE platform_job_id=?",
                        (job_id,),
                    ).fetchone()
                finally:
                    connection.close()
                if row is not None and (row["started_at"] is not None or row["status"] != "queued"):
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("idle queue did not start within 10 seconds")
            idle_start_durations.append((time.perf_counter() - started) * 1000)
    assert _p95(idle_start_durations) < 10_000

    enqueue_durations: list[float] = []
    for sample in range(40):
        started = time.perf_counter()
        _enqueue(config, marker=f"enqueue-p95-{sample}")
        enqueue_durations.append((time.perf_counter() - started) * 1000)
    assert _p95(enqueue_durations) < 100


def test_expired_lease_takeover_fences_stale_worker(
    runtime: tuple[AppConfig, PlatformContext]
) -> None:
    config, _context_value = runtime
    job_id = _enqueue(config, marker="takeover")
    stale = claim_platform_job(config, worker_code="prd106-stale-worker", lease_seconds=10)
    assert stale is not None and stale.job_id == job_id
    connection = connect_database(config.database_url)
    try:
        connection.execute(
            "UPDATE platform_scheduler_leases SET expires_at='2000-01-01T00:00:00Z' "
            "WHERE platform_job_attempt_id=?",
            (stale.attempt_id,),
        )
        connection.commit()
    finally:
        connection.close()
    takeover_started = time.perf_counter()
    current = claim_platform_job(config, worker_code="prd106-current-worker", lease_seconds=10)
    assert time.perf_counter() - takeover_started < 60
    assert current is not None and current.job_id == job_id and current.generation == 2
    with pytest.raises(ApiError) as fenced:
        PlatformJobContext(config=config, lease=stale).checkpoint({"stale": True})
    assert fenced.value.code == "platform_job.lease_lost"
    connection = connect_database(config.database_url)
    try:
        attempts = connection.execute(
            "SELECT attempt_number,status FROM platform_job_attempts "
            "WHERE platform_job_id=? ORDER BY attempt_number",
            (job_id,),
        ).fetchall()
        assert [tuple(row) for row in attempts] == [(1, "stale"), (2, "leased")]
    finally:
        connection.close()


def test_cancel_fence_prevents_effect_commit(runtime: tuple[AppConfig, PlatformContext]) -> None:
    config, _context_value = runtime
    job_id = _enqueue(config, marker="cancel-race")
    lease = claim_platform_job(config, worker_code="prd106-cancel-worker", lease_seconds=10)
    assert lease is not None
    connection = connect_database(config.database_url)
    try:
        row = connection.execute(
            "SELECT revision,updated_at FROM platform_jobs WHERE platform_job_id=?", (job_id,)
        ).fetchone()
        connection.execute(
            """UPDATE platform_jobs SET status='cancel_requested',revision=revision+1,
                 updated_at=datetime(updated_at,'+1 second')
               WHERE platform_job_id=? AND revision=?""",
            (job_id, int(row["revision"])),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(PlatformJobCancelled):
        PlatformJobContext(config=config, lease=lease).commit_effect(
            "cancel.race", {"must_not_commit": True}
        )
    connection = connect_database(config.database_url)
    try:
        assert connection.execute(
            "SELECT status FROM platform_jobs WHERE platform_job_id=?", (job_id,)
        ).fetchone()[0] == "cancelled"
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_job_effects WHERE platform_job_id=?", (job_id,)
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_retry_exhaustion_dead_letters_immutably(
    monkeypatch: pytest.MonkeyPatch, runtime: tuple[AppConfig, PlatformContext]
) -> None:
    config, context = runtime
    job_id = _enqueue(config, marker="dead-letter", outcome="retry")
    for attempt in range(3):
        assert run_platform_job_once(config, worker_code=f"prd106-retry-worker-{attempt}")
        if attempt < 2:
            connection = connect_database(config.database_url)
            try:
                connection.execute(
                    """UPDATE platform_jobs SET scheduled_at='2000-01-01T00:00:00Z',
                         revision=revision+1,updated_at=datetime(updated_at,'+1 second')
                       WHERE platform_job_id=? AND status='queued'""",
                    (job_id,),
                )
                connection.commit()
            finally:
                connection.close()
    connection = connect_database(config.database_url)
    try:
        assert tuple(
            connection.execute(
                "SELECT status,attempt_count,generation FROM platform_jobs "
                "WHERE platform_job_id=?",
                (job_id,),
            ).fetchone()
        ) == ("dead_lettered", 3, 3)
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_dead_letter_jobs WHERE platform_job_id=?", (job_id,)
        ).fetchone()[0] == 1
        revision = connection.execute(
            "SELECT revision FROM platform_jobs WHERE platform_job_id=?", (job_id,)
        ).fetchone()[0]
    finally:
        connection.close()

    monkeypatch.setattr(
        job_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: context,
    )
    retried = job_service.update_platform_job(
        config,
        session_token=TEST_PLATFORM_SESSION,
        csrf_token=TEST_PLATFORM_CSRF,
        job_id=job_id,
        action="retry",
        expected_revision=int(revision),
        idempotency_key="prd106-manual-dead-letter-retry",
        payload=PlatformJobActionRequest(reason_code="operator_retry", confirmed=True),
        request_id="request-manual-retry",
        trace_id="trace-manual-retry",
    )
    assert retried.job.status == "queued"
    assert retried.job.attempt_count == 3
    assert run_platform_job_once(config, worker_code="prd106-manual-retry-worker")
    connection = connect_database(config.database_url)
    try:
        assert tuple(
            connection.execute(
                "SELECT status,attempt_count,generation FROM platform_jobs "
                "WHERE platform_job_id=?",
                (job_id,),
            ).fetchone()
        ) == ("dead_lettered", 4, 4)
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_dead_letter_jobs WHERE platform_job_id=?",
            (job_id,),
        ).fetchone()[0] == 2
    finally:
        connection.close()


@pytest.mark.anyio
async def test_single_supervisor_preserves_startup_and_reverse_shutdown(
    runtime: tuple[AppConfig, PlatformContext]
) -> None:
    config, _context_value = runtime
    app = create_service_app("control-plane-api", "PRD-106 Supervisor Test")
    app.state.config = config
    events: list[str] = []

    def installer(name: str):
        def install(current_app):
            original = current_app.router.lifespan_context

            @asynccontextmanager
            async def combined(active_app):
                async with original(active_app):
                    events.append(f"enter:{name}")
                    try:
                        yield
                    finally:
                        events.append(f"exit:{name}")

            current_app.router.lifespan_context = combined

        return install

    install_platform_job_supervisor(
        app,
        scheduler_installers=(installer("one"), installer("two")),
    )
    async with app.router.lifespan_context(app):
        assert events == ["enter:one", "enter:two"]
        assert app.state.platform_job_scheduler_context_count == 2
    assert events == ["enter:one", "enter:two", "exit:two", "exit:one"]
