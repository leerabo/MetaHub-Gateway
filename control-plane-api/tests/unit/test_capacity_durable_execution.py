"""Runner persistence, isolation and real HTTP measurement against a disposable target."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import psutil
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_capacity_runner import probe
from metahub_capacity_runner.app import app
from metahub_capacity_runner.contracts import CapacityPermitClaims, encode_signed
from metahub_capacity_runner.engine import SCENARIOS
from metahub_capacity_runner.settings import CapacityRunnerSettings, CapacityTarget, private_origin
from metahub_capacity_runner.store import RunnerStore
from metahub_capacity_runner.supervisor import CapacityExecutor
from metahub_control.capacity_management import runner_client
from starlette.testclient import TestClient

TOKEN = "synthetic-runner-token-" + "d" * 32
PERMIT_SECRET = "synthetic-permit-secret-" + "e" * 32


@pytest.fixture
def claims():
    now = datetime.now(UTC)
    return CapacityPermitClaims(
        permit_id="crp_" + "a" * 32,
        object_id="ltr_" + uuid4().hex,
        operation="load_test",
        service_code="gateway",
        scenario_code="gateway-release",
        environment_class="staging",
        region_code="cn-north-1",
        synthetic_tenant_count=2,
        target_rps=10,
        duration_seconds=10,
        objective_p95_ms=0,
        objective_error_rate_bps=0,
        stop_error_rate_bps=0,
        manifest_sha256=SCENARIOS["gateway-release"][-1],
        kill_generation=0,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )


@pytest.fixture
def target(tmp_path):
    keys = ["synthetic-one-" + "a" * 32, "synthetic-two-" + "b" * 32]
    paths = []
    for index, key in enumerate(keys):
        path = tmp_path / f"key-{index}.secret"
        path.write_text(key, encoding="ascii")
        paths.append(path)
    resource_key = tmp_path / "resource.secret"
    resource_key.write_text("synthetic-resource-" + "c" * 32, encoding="ascii")
    return CapacityTarget.model_validate(
        {
            "service_code": "gateway",
            "environment_class": "staging",
            "region_code": "cn-north-1",
            "origin": "http://127.0.0.1:18080",
            "resource_origin": "http://127.0.0.1:18081",
            "resource_credential_file": resource_key,
            "model_name": "synthetic-model",
            "max_rps": 20,
            "max_concurrency": 4,
            "request_timeout_seconds": 2,
            "identities": [
                {"subject_code": f"synthetic-owner-{i}", "credential_file": path}
                for i, path in enumerate(paths)
            ],
        }
    )


def test_store_replay_is_durable_and_conflicting_parameters_are_rejected(tmp_path, claims):
    store = RunnerStore(tmp_path)
    first = store.submit(claims, "b" * 64)
    reloaded = RunnerStore(tmp_path)
    assert reloaded.submit(claims, "b" * 64) == first
    renewed = claims.model_copy(
        update={
            "issued_at": claims.issued_at + timedelta(seconds=10),
            "expires_at": claims.expires_at + timedelta(seconds=10),
        }
    )
    assert reloaded.status(renewed) == first
    with pytest.raises(ValueError, match="runner.scope_conflict"):
        reloaded.submit(claims.model_copy(update={"target_rps": 11}), "b" * 64)
    assert reloaded.claim() is not None
    assert reloaded.claim() is None


def test_concurrent_submit_creates_one_execution(tmp_path, claims):
    store = RunnerStore(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda _: store.submit(claims, "b" * 64), range(16)))
    assert all(item == responses[0] for item in responses)
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1


def test_cancel_and_kill_fences_survive_restart(tmp_path, claims):
    store = RunnerStore(tmp_path)
    store.submit(claims, "b" * 64)
    assert store.cancel(claims).state == "cancelled"
    assert store.claim() is None
    assert RunnerStore(tmp_path).status(claims).state == "cancelled"
    assert store.kill(claims.object_id, 3) == 3
    assert RunnerStore(tmp_path).kill(claims.object_id, 1) == 3
    with pytest.raises(ValueError, match="runner.killed"):
        RunnerStore(tmp_path).submit(claims, "b" * 64)


def test_cancel_wins_over_late_completion(tmp_path, claims):
    store = RunnerStore(tmp_path)
    store.submit(claims, "b" * 64)
    _, generation, _ = store.claim()
    assert store.cancel(claims).state == "stopping"
    store.finish(
        claims.object_id,
        generation,
        result=None,
        state="failed",
        outcome="runner.execution_failed",
        metrics={},
    )
    assert store.status(claims).state == "cancelled"


def test_interrupted_execution_never_runs_again(tmp_path, claims):
    store = RunnerStore(tmp_path)
    store.submit(claims, "b" * 64)
    _, old_generation, _ = store.claim()
    RunnerStore(tmp_path).interrupted()
    assert store.submit(claims, "b" * 64).state == "interrupted"
    assert store.claim() is None
    with pytest.raises(ValueError, match="runner.execution_fenced"):
        store.finish(
            claims.object_id,
            old_generation,
            result=None,
            state="failed",
            outcome="runner.execution_failed",
            metrics={},
        )


def test_cancel_before_submit_is_a_durable_fence(tmp_path, claims):
    store = RunnerStore(tmp_path)
    assert store.cancel(claims).state == "cancelled"
    assert RunnerStore(tmp_path).submit(claims, "b" * 64).state == "cancelled"
    assert store.claim() is None
    with pytest.raises(ValueError, match="runner.scope_conflict"):
        store.submit(claims.model_copy(update={"target_rps": 11}), "b" * 64)


def test_expired_queue_permit_cannot_be_renewed_to_restart(tmp_path, claims, monkeypatch):
    from metahub_capacity_runner import store as store_module

    store = RunnerStore(tmp_path)
    store.submit(claims, "b" * 64)
    monkeypatch.setattr(store_module, "datetime", SimpleNamespace(now=lambda _: claims.expires_at))
    assert store.claim() is None
    status = store.status(claims)
    assert (status.state, status.safe_outcome_code) == ("failed", "runner.permit_expired")
    renewed = claims.model_copy(
        update={
            "issued_at": claims.issued_at + timedelta(minutes=10),
            "expires_at": claims.expires_at + timedelta(minutes=10),
        }
    )
    assert RunnerStore(tmp_path).submit(renewed, "b" * 64) == status
    assert store.claim() is None


@pytest.mark.parametrize("field", ["issued_at", "expires_at"])
def test_permit_requires_an_explicit_timezone(claims, field):
    payload = claims.model_dump()
    payload[field] = payload[field].replace(tzinfo=None)
    with pytest.raises(ValueError, match="permit expiry is invalid"):
        CapacityPermitClaims.model_validate(payload)


def test_executor_os_lock_has_one_owner(tmp_path, target):
    settings = CapacityRunnerSettings(state_directory=tmp_path / "state", targets=[target])
    first, second = CapacityExecutor(settings), CapacityExecutor(settings)
    first.start()
    try:
        with pytest.raises(ValueError, match="runner.executor_busy"):
            second.start()
    finally:
        first.close()
    second.start()
    second.close()
    assert not any(thread.name == "capacity-executor" for thread in threading.enumerate())


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com:443",
        "http://8.8.8.8:80",
        "http://169.254.169.254:80",
        "http://[::ffff:169.254.169.254]:80",
        "http://[::ffff:8.8.8.8]:80",
        "http://[fd00::1%interface]:80",
        "http://127.0.0.1:80/\n",
        "http://127.0.0.1:80/path",
        "http://user:password@127.0.0.1:80",
        "http://127.0.0.1:80?redirect=elsewhere",
        "file:///tmp/target",
        "http://0.0.0.0:80",
    ],
)
def test_only_deployment_private_ip_origins_are_allowed(origin):
    with pytest.raises(ValueError, match="runner.target_invalid"):
        private_origin(origin)


def test_credential_rotation_changes_deployment_binding(target):
    before = target.digest()
    target.identities[0].credential_file.write_text(
        "rotated-synthetic-" + "d" * 32, encoding="ascii"
    )
    assert target.digest() != before


def test_duplicate_actual_credentials_cannot_pretend_to_be_different_subjects(target):
    source = target.identities[0].credential_file.read_text(encoding="ascii")
    target.identities[1].credential_file.write_text(source, encoding="ascii")
    with pytest.raises(ValueError, match="runner.identities_invalid"):
        target.credentials(2)


def test_store_refuses_an_unrelated_database(tmp_path):
    with sqlite3.connect(tmp_path / "executions.sqlite3") as connection:
        connection.execute("CREATE TABLE customer_data(value TEXT)")
        connection.execute("INSERT INTO customer_data VALUES('preserve')")
    with pytest.raises(ValueError, match="runner.state_invalid"):
        RunnerStore(tmp_path)
    with sqlite3.connect(tmp_path / "executions.sqlite3") as connection:
        assert connection.execute("SELECT value FROM customer_data").fetchone()[0] == "preserve"


def _serve_target(ready, request_count, keys):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            received = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(received)
            assert self.path == "/v1/chat/completions"
            assert body["model"] == "synthetic-model"
            accepted = self.headers.get("Authorization") in {f"Bearer {key}" for key in keys}
            with request_count.get_lock():
                request_count.value += 1
            time.sleep(0.025)
            payload = b'{"private_response":"never-persist-this-target-response"}'
            self.send_response(200 if accepted else 401)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ready.put((server.server_port, os.getpid(), psutil.Process().create_time()))
    server.serve_forever(poll_interval=0.1)


@pytest.fixture
def isolated_http_target(target, monkeypatch):
    context = multiprocessing.get_context("spawn")
    ready, count = context.Queue(), context.Value("i", 0)
    keys, resource_key = target.credentials(2)
    process = context.Process(target=_serve_target, args=(ready, count, keys))
    process.start()
    server = None
    thread = None
    try:
        port, pid, created_at = ready.get(timeout=15)
        monkeypatch.setenv(
            "METAHUB_CAPACITY_PROBE_TOKEN_FILE", str(target.resource_credential_file)
        )
        monkeypatch.setenv("METAHUB_CAPACITY_PROBE_TARGET_PID", str(pid))
        monkeypatch.setenv("METAHUB_CAPACITY_PROBE_TARGET_CREATED_AT", str(created_at))

        class ResourceHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                assert self.path == "/internal/capacity/telemetry"
                sample = probe.telemetry(authorization=self.headers.get("Authorization"))
                payload = sample.model_dump_json().encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ResourceHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        configured = target.model_copy(
            update={
                "origin": f"http://127.0.0.1:{port}",
                "resource_origin": f"http://127.0.0.1:{server.server_port}",
            }
        )
        yield SimpleNamespace(
            target=configured, count=count, process=process, resource_key=resource_key, keys=keys
        )
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)
        process.terminate()
        process.join(timeout=5)
        ready.close()
        ready.join_thread()
        assert not process.is_alive()


def wait_state(executor, claims, states, *, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = executor.store.status(claims)
        if state.state in states:
            return state
        time.sleep(0.05)
    pytest.fail("runner did not reach the required state")


def test_real_requests_and_target_resources_produce_durable_measured_result(
    tmp_path, claims, isolated_http_target
):
    isolated = isolated_http_target
    settings = CapacityRunnerSettings(state_directory=tmp_path / "state", targets=[isolated.target])
    executor = CapacityExecutor(settings)
    executor.start()
    try:
        assert executor.submit(claims).state == "queued"
        status = wait_state(executor, claims, {"succeeded", "failed"})
        assert status.state == "succeeded", status.safe_outcome_code
        result = status.result
        assert result.evidence_kind == "measured"
        assert result.sample_count == isolated.count.value
        assert 90 <= result.sample_count <= 105
        assert result.p50_ms >= 25
        assert result.error_rate_bps == 0
        assert result.memory_peak_mib > 0
        assert status.elapsed_ms >= 10_000
        assert executor.submit(claims).result == result
    finally:
        executor.close()
    reloaded = RunnerStore(settings.state_directory)
    assert reloaded.status(claims).result == result
    with reloaded.connection() as connection:
        stored = connection.execute("SELECT claims_json,metrics_json,result_json FROM executions")
        serialized = "".join(str(value) for row in stored for value in row)
    for forbidden in [
        *isolated.keys,
        isolated.resource_key,
        "never-persist-this-target-response",
        "Authorization",
        isolated.target.origin,
    ]:
        assert forbidden not in serialized


def test_cancellation_stops_real_request_issuance_and_does_not_restart(
    tmp_path, claims, isolated_http_target
):
    isolated = isolated_http_target
    settings = CapacityRunnerSettings(state_directory=tmp_path / "state", targets=[isolated.target])
    executor = CapacityExecutor(settings)
    executor.start()
    try:
        executor.submit(claims)
        wait_state(executor, claims, {"running"})
        deadline = time.monotonic() + 5
        while isolated.count.value < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert isolated.count.value >= 3
        assert executor.store.cancel(claims).state == "stopping"
        result = wait_state(executor, claims, {"cancelled", "failed"})
        assert result.state == "cancelled"
        stopped_count = isolated.count.value
        time.sleep(0.3)
        assert isolated.count.value == stopped_count
    finally:
        executor.close()
    restarted = CapacityExecutor(settings)
    restarted.start()
    try:
        assert restarted.submit(claims).state == "cancelled"
        time.sleep(0.3)
        assert isolated.count.value == stopped_count
    finally:
        restarted.close()


def test_probe_rejects_another_process_generation(isolated_http_target, monkeypatch):
    isolated = isolated_http_target
    monkeypatch.setenv("METAHUB_CAPACITY_PROBE_TARGET_CREATED_AT", "1")
    with pytest.raises(Exception) as rejected:
        probe.telemetry(authorization=f"Bearer {isolated.resource_key}")
    assert rejected.value.status_code == 503


@pytest.fixture
def configured_api(tmp_path, isolated_http_target, monkeypatch):
    settings = CapacityRunnerSettings(
        state_directory=tmp_path / "api-state", targets=[isolated_http_target.target]
    )
    path = tmp_path / "runner-settings.json"
    path.write_text(settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_SETTINGS_FILE", str(path))
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_PERMIT_SECRET", PERMIT_SECRET)
    return settings


def signed_body(claims):
    return {"permit": encode_signed(claims, PERMIT_SECRET, domain="run-permit")}


async def api_state(client, claims, states):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        response = await client.post(
            f"/internal/capacity/runs/{claims.object_id}/status", json=signed_body(claims)
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        state = response.json()
        if state["state"] in states:
            return state
        await asyncio.sleep(0.05)
    pytest.fail("runner API did not reach expected state")


@pytest.mark.anyio
async def test_api_pending_poll_complete_and_replay_use_actual_executor(
    configured_api, claims, isolated_http_target
):
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runner",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            first = await client.post("/internal/capacity/runs", json=signed_body(claims))
            assert first.status_code == 202
            assert first.json()["state"] in {"queued", "running"}
            assert first.headers["cache-control"] == "no-store"
            state = await api_state(client, claims, {"succeeded", "failed"})
            assert state["state"] == "succeeded", state["safe_outcome_code"]
            assert state["sample_count"] == isolated_http_target.count.value
            assert state["result"]["sample_count"] == isolated_http_target.count.value
            assert state["result"]["evidence_kind"] == "measured"
            assert state["elapsed_ms"] >= 10000
            replay = await client.post("/internal/capacity/runs", json=signed_body(claims))
            assert replay.status_code == 200
            assert replay.json() == state["result"]
    assert RunnerStore(configured_api.state_directory).status(claims).state == "succeeded"
    assert app.state.executor is None


@pytest.mark.anyio
async def test_api_cancel_stops_traffic_and_survives_lifespan_restart(
    configured_api, claims, isolated_http_target
):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://runner",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        async with app.router.lifespan_context(app):
            await client.post("/internal/capacity/runs", json=signed_body(claims))
            await api_state(client, claims, {"running"})
            deadline = time.monotonic() + 5
            while isolated_http_target.count.value < 3 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert isolated_http_target.count.value >= 3
            cancel = await client.post(
                f"/internal/capacity/runs/{claims.object_id}/cancel", json=signed_body(claims)
            )
            assert cancel.status_code == 200
            assert cancel.json()["state"] in {"stopping", "cancelled"}
            status = await api_state(client, claims, {"cancelled", "failed"})
            assert status["state"] == "cancelled"
            assert status["stop_scope"] == "runner_requests"
            stopped_count = isolated_http_target.count.value
        async with app.router.lifespan_context(app):
            replay = await client.post("/internal/capacity/runs", json=signed_body(claims))
            assert replay.status_code == 200
            assert replay.json()["state"] == "cancelled"
            await asyncio.sleep(0.3)
            assert isolated_http_target.count.value == stopped_count


@pytest.mark.anyio
async def test_api_status_and_cancel_validate_scope_without_starting_traffic(
    configured_api, claims, isolated_http_target
):
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runner",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            missing = await client.post(
                f"/internal/capacity/runs/{claims.object_id}/status", json=signed_body(claims)
            )
            assert missing.status_code == 404
            for action in ("status", "cancel"):
                path = f"/internal/capacity/runs/{claims.object_id}/{action}"
                unauthorized = await client.post(
                    path, json=signed_body(claims), headers={"Authorization": "Bearer invalid"}
                )
                assert unauthorized.status_code == 401
                assert unauthorized.headers["cache-control"] == "no-store"
                conflict = await client.post(
                    f"/internal/capacity/runs/ltr_{'f' * 32}/{action}", json=signed_body(claims)
                )
                assert conflict.status_code == 409
                expired = claims.model_copy(
                    update={
                        "issued_at": datetime.now(UTC) - timedelta(minutes=2),
                        "expires_at": datetime.now(UTC) - timedelta(minutes=1),
                    }
                )
                assert (await client.post(path, json=signed_body(expired))).status_code == 401
            cancelled = await client.post(
                f"/internal/capacity/runs/{claims.object_id}/cancel", json=signed_body(claims)
            )
            assert cancelled.json()["state"] == "cancelled"
            replay = await client.post("/internal/capacity/runs", json=signed_body(claims))
            assert replay.json()["state"] == "cancelled"
            conflict = claims.model_copy(update={"target_rps": 11})
            assert (
                await client.post(
                    f"/internal/capacity/runs/{claims.object_id}/cancel", json=signed_body(conflict)
                )
            ).status_code == 409
            await asyncio.sleep(0.3)
            assert isolated_http_target.count.value == 0


@pytest.mark.anyio
async def test_cancellation_before_initial_probe_never_issues_a_request(
    target, claims, monkeypatch
):
    from metahub_capacity_runner import measurement

    async def forbidden_probe(*_args):
        pytest.fail("cancelled work contacted the resource probe")

    monkeypatch.setattr(measurement, "_resource", forbidden_probe)
    result = await measurement.measure_load(claims, target, lambda: True, lambda *_args: None)
    assert result.state == "cancelled"


@pytest.mark.anyio
@pytest.mark.parametrize("stopping", [True, False])
async def test_cancellation_or_expiry_during_initial_probe_never_starts_target_requests(
    claims, isolated_http_target, monkeypatch, stopping
):
    from metahub_capacity_runner import measurement

    original = measurement._resource
    observed = False

    async def checked_probe(*args):
        nonlocal observed
        sample = await original(*args)
        observed = True
        if not stopping:
            monkeypatch.setattr(
                measurement, "datetime", SimpleNamespace(now=lambda _: claims.expires_at)
            )
            sample = sample.model_copy(update={"sampled_at": claims.expires_at})
        return sample

    monkeypatch.setattr(measurement, "_resource", checked_probe)
    outcome = await measurement.measure_load(
        claims, isolated_http_target.target, lambda: stopping and observed, lambda *_args: None
    )
    assert outcome.state == ("cancelled" if stopping else "failed")
    assert outcome.outcome == ("runner.cancelled" if stopping else "runner.permit_expired")
    assert isolated_http_target.count.value == 0


def test_control_client_observes_real_runner_pending_and_cancellation(
    configured_api, claims, isolated_http_target, monkeypatch
):
    config = SimpleNamespace(
        capacity_runner_mode="isolated_http",
        capacity_runner_base_url="http://runner",
        capacity_runner_internal_token=TOKEN,
        capacity_runner_permit_secret=PERMIT_SECRET,
    )
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200

        def post(url, **kwargs):
            assert kwargs.pop("trust_env") is False
            assert kwargs.pop("follow_redirects") is False
            kwargs.pop("timeout")
            return client.post(url.removeprefix("http://runner"), **kwargs)

        monkeypatch.setattr(runner_client.httpx, "post", post)
        first = runner_client.capacity_execution(config, claims, action="submit")
        assert first.state in {"queued", "running"}
        observed = runner_client.capacity_execution(config, claims, action="status")
        assert observed.object_id == first.object_id
        assert observed.execution_sha256 == first.execution_sha256
        cancelled = runner_client.capacity_execution(config, claims, action="cancel")
        assert cancelled.state in {"cancelled", "stopping"}
        deadline = time.monotonic() + 5
        while cancelled.state == "stopping" and time.monotonic() < deadline:
            time.sleep(0.1)
            cancelled = runner_client.capacity_execution(config, claims, action="status")
        assert cancelled.state == "cancelled"
        count = isolated_http_target.count.value
        assert runner_client.capacity_execution(config, claims, action="submit") == cancelled
        time.sleep(0.3)
        assert isolated_http_target.count.value == count
    assert client.get("/ready").status_code == 503
