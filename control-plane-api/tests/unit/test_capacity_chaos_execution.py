"""Real HTTP chaos orchestration on disposable processes, sockets and journals.

These prove runner/proxy integration, not the Gateway's dependency fail policy.
"""

from __future__ import annotations

import json
import multiprocessing
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import uvicorn
from metahub_capacity_runner.contracts import CapacityPermitClaims
from metahub_capacity_runner.fault_app import app as fault_app
from metahub_capacity_runner.fault_client import FINAL_SEQUENCE
from metahub_capacity_runner.settings import CapacityRunnerSettings
from metahub_capacity_runner.store import RunnerStore
from metahub_capacity_runner.supervisor import CapacityExecutor
from metahub_control.capacity_management.runner_client import _validate_result
from test_capacity_chaos_runtime import fault_binding
from test_capacity_durable_execution import isolated_http_target, target, wait_state  # noqa: F401
from test_capacity_fault_proxy import binding, command, configuration, execution


@pytest.fixture
def chaos_network(tmp_path, request, monkeypatch):
    isolated = request.getfixturevalue("isolated_http_target")
    reservation = socket.socket()
    reservation.bind(("127.0.0.1", 0))
    proxy_port = reservation.getsockname()[1]
    reservation.close()
    management = socket.socket()
    management.bind(("127.0.0.1", 0))
    management_port = management.getsockname()[1]
    endpoint = binding(listen_port=proxy_port, upstream_port=urlsplit(isolated.target.origin).port)
    proxy_settings = configuration(tmp_path / "proxy", endpoint)
    proxy_settings.state_directory.mkdir()
    settings_path = tmp_path / "fault-settings.json"
    client_binding = fault_binding(tmp_path, origin=f"http://127.0.0.1:{management_port}")
    proxy_settings = proxy_settings.model_copy(
        update={
            "internal_token_file": client_binding.internal_token_file,
            "permit_secret_file": client_binding.permit_secret_file,
        }
    )
    settings_path.write_text(proxy_settings.model_dump_json(), encoding="ascii")
    monkeypatch.setenv("METAHUB_CAPACITY_FAULT_SETTINGS_FILE", str(settings_path))
    server = uvicorn.Server(uvicorn.Config(fault_app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [management]})
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "isolated fault API did not start"
        configured_target = isolated.target.model_copy(
            update={
                "origin": f"http://127.0.0.1:{proxy_port}",
                "request_timeout_seconds": 1,
            }
        )
        settings = CapacityRunnerSettings(
            state_directory=tmp_path / "runner",
            targets=[configured_target],
            fault_bindings=[
                client_binding,
                client_binding.model_copy(
                    update={
                        "fault_code": "redis-unavailable",
                    }
                ),
            ],
        )
        yield SimpleNamespace(isolated=isolated, settings=settings, proxy=fault_app.state.proxy)
    finally:
        server.should_exit = True
        thread.join(timeout=12)
        management.close()
        assert not thread.is_alive(), "isolated fault API leaked its thread"


def observed(proxy, run):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            info = proxy.command(command(run, action="status"))
        except ValueError as exc:
            assert str(exc) == "fault.execution_not_found"
        else:
            if info.reset_connections + info.withheld_bytes > 0:
                return info
        time.sleep(0.02)
    pytest.fail("actual proxy fault was not observed")


def metrics(store, run):
    with store.connection() as connection:
        return json.loads(
            connection.execute(
                "SELECT metrics_json FROM executions WHERE object_id=?",
                (run.object_id,),
            ).fetchone()[0]
        )


@pytest.mark.parametrize("fault", ["dependency-timeout", "redis-unavailable"])
def test_real_chaos_reverses_and_requires_fresh_recovery_probes(chaos_network, fault):
    network = chaos_network
    run = execution(fault).model_copy(update={"stop_error_rate_bps": 10000})
    executor = CapacityExecutor(network.settings)
    executor.start()
    try:
        executor.submit(run)
        status = wait_state(executor, run, {"succeeded", "failed"}, timeout=30)
        assert status.state == "succeeded", (status.safe_outcome_code, metrics(executor.store, run))
        assert status.stop_scope == "runner_requests_and_fault"
        result = _validate_result(status.result, run)
        assert result.evidence_kind == "measured" and not result.objective_met
        assert result.observed_error_rate_bps == 10000
        evidence = result.chaos_evidence
        assert evidence.injected_duration_ms >= 9000
        assert evidence.baseline_sample_count == 2 and evidence.recovery_sample_count == 6
        assert evidence.fault_sample_count > 2
        assert evidence.target_recovered and evidence.fault_reversed
        info = network.proxy.command(command(run, action="status"))
        assert info.active_connections == 0 and info.state != "active"
        # Requests already in flight at natural expiry may arrive before runner cancellation.
        # Buffered fault traffic is separately proven discarded by the TCP tests.
        count = network.isolated.count.value
        assert 8 <= count <= 8 + network.settings.targets[0].max_concurrency
        time.sleep(0.3)
        assert network.isolated.count.value == count
        stored = metrics(executor.store, run)
        assert stored["resource_sample_count"] >= 3
        assert stored["resource_scope"] == "target_process" and stored["memory_peak_bytes"] > 0
        assert stored["resource_windows"]
        private = [
            *network.isolated.keys,
            network.isolated.resource_key,
            *network.settings.fault(run).credentials(),
            "never-persist-this-target-response",
        ]
        assert all(value not in json.dumps(stored) for value in private)
        assert RunnerStore(network.settings.state_directory).status(run) == status
    finally:
        executor.close()


def test_cancel_stops_requests_and_fault_without_claiming_recovery(chaos_network):
    network, run = chaos_network, execution()
    executor = CapacityExecutor(network.settings)
    executor.start()
    try:
        executor.submit(run)
        assert observed(network.proxy, run).active_connections > 0
        assert executor.store.cancel(run).state == "stopping"
        status = wait_state(executor, run, {"cancelled", "failed"})
        assert status.state == "cancelled" and status.result is None
        assert status.stop_scope == "runner_requests_and_fault"
        info = network.proxy.command(command(run, action="status"))
        assert info.active_connections == 0 and info.state == "released"
        assert info.sequence == FINAL_SEQUENCE
        assert not metrics(executor.store, run)["target_recovered"]
        count = network.isolated.count.value
        time.sleep(0.3)
        assert network.isolated.count.value == count == 2
    finally:
        executor.close()


def test_error_threshold_stops_real_fault_early(chaos_network):
    network, run = chaos_network, execution()
    executor = CapacityExecutor(network.settings)
    executor.start()
    try:
        executor.submit(run)
        status = wait_state(executor, run, {"failed", "succeeded"})
        assert status.state == "failed" and status.result is None
        assert status.safe_outcome_code == "runner.chaos_stop_threshold"
        assert status.elapsed_ms < 10000
        assert status.stop_scope == "runner_requests_and_fault"
        assert network.proxy.command(command(run, action="status")).active_connections == 0
    finally:
        executor.close()


def test_failed_apply_reply_still_reverses_committed_remote_fault(chaos_network, monkeypatch):
    network, run = chaos_network, execution()
    dispatch = network.proxy.command
    applied = threading.Event()

    def fail_reply(request):
        info = dispatch(request)
        if request.action == "apply":
            applied.set()
            raise ValueError("fault.state_unavailable")
        return info

    monkeypatch.setattr(network.proxy, "command", fail_reply)
    executor = CapacityExecutor(network.settings)
    executor.start()
    try:
        executor.submit(run)
        status = wait_state(executor, run, {"failed", "succeeded"})
        assert applied.is_set()
        assert status.state == "failed" and status.result is None
        assert status.stop_scope == "runner_requests_and_fault"
        info = dispatch(command(run, action="status"))
        assert info.state == "released" and info.sequence == FINAL_SEQUENCE
        assert info.active_connections == 0
    finally:
        executor.close()


def test_unobserved_fault_cannot_manufacture_a_successful_experiment(chaos_network, monkeypatch):
    network, run = chaos_network, execution()
    monkeypatch.setattr(network.proxy.store, "active_effect", lambda _binding: None)
    executor = CapacityExecutor(network.settings)
    executor.start()
    try:
        executor.submit(run)
        status = wait_state(executor, run, {"failed", "succeeded"}, timeout=30)
        assert status.state == "failed" and status.result is None
        assert status.safe_outcome_code == "runner.chaos_fault_not_observed"
        stored = metrics(executor.store, run)
        assert stored["fault_outcomes"]["success"] > 0
        assert stored["recovery_sample_count"] == 0 and not stored["target_recovered"]
        assert status.stop_scope == "runner_requests_and_fault"
    finally:
        executor.close()


def test_reversal_without_target_recovery_cannot_report_success(chaos_network, monkeypatch):
    network = chaos_network
    run = execution("redis-unavailable").model_copy(update={"stop_error_rate_bps": 10000})
    dispatch = network.proxy.command
    stopped_target = threading.Event()

    def fail_target_after_reversal(request):
        info = dispatch(request)
        if info.state != "active" and not stopped_target.is_set():
            stopped_target.set()
            network.isolated.process.terminate()
            network.isolated.process.join(timeout=2)
        return info

    monkeypatch.setattr(network.proxy, "command", fail_target_after_reversal)
    executor = CapacityExecutor(network.settings)
    executor.start()
    try:
        executor.submit(run)
        status = wait_state(executor, run, {"failed", "succeeded"}, timeout=35)
        assert stopped_target.is_set()
        assert status.state == "failed" and status.result is None
        assert status.safe_outcome_code in {
            "runner.chaos_recovery_failed",
            "runner.chaos_probe_timeout",
        }
        assert status.stop_scope == "runner_requests_and_fault"
        stored = metrics(executor.store, run)
        assert stored["recovery_sample_count"] > 0 and not stored["target_recovered"]
        assert stored["fault_reversal"]["active_connections"] == 0
    finally:
        executor.close()


def test_missing_reversal_proof_stays_nonterminal_across_shutdown(chaos_network, monkeypatch):
    network = chaos_network
    run = execution().model_copy(update={"stop_error_rate_bps": 10000})
    dispatch = network.proxy.command
    unavailable = threading.Event()

    def unavailable_reply(request):
        if unavailable.is_set() and request.action in {"status", "release"}:
            raise ValueError("fault.state_unavailable")
        return dispatch(request)

    monkeypatch.setattr(network.proxy, "command", unavailable_reply)
    executor = CapacityExecutor(network.settings)
    executor.start()
    try:
        executor.submit(run)
        observed(network.proxy, run)
        unavailable.set()
        executor.store.cancel(run)
        executor.close()
        status = executor.store.status(run)
        assert status.state == "stopping" and status.result is None
        assert status.stop_scope == "runner_requests"
        assert executor.effects.pending() is not None
        assert executor.store.claim() is None
        unavailable.clear()
        recovered = CapacityExecutor(network.settings)
        recovered.start()
        try:
            status = wait_state(recovered, run, {"cancelled", "interrupted", "failed"})
            assert status.state == "cancelled" and status.result is None
            assert status.stop_scope == "runner_requests_and_fault"
            assert recovered.effects.pending() is None
            info = dispatch(command(run, action="status"))
            assert info.state != "active" and info.active_connections == 0
        finally:
            recovered.close()
    finally:
        unavailable.clear()
        executor.close()


def runner_process(settings_json, claims_json, channel):
    executor = CapacityExecutor(CapacityRunnerSettings.model_validate_json(settings_json))
    executor.start()
    try:
        executor.submit(CapacityPermitClaims.model_validate_json(claims_json))
        channel.send("ready")
        threading.Event().wait()
    finally:
        executor.close()


def test_process_death_reconciles_original_binding_after_settings_change(chaos_network):
    network = chaos_network
    run = execution().model_copy(update={"stop_error_rate_bps": 10000})
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=runner_process,
        args=(
            network.settings.model_dump_json(),
            run.model_dump_json(),
            child,
        ),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(15), "isolated runner did not start"
        assert parent.recv() == "ready"
        assert observed(network.proxy, run).active_connections > 0
        process.terminate()
        process.join(timeout=5)
        assert not process.is_alive()
        # The restarted deployment no longer exposes this binding; cleanup must use the journal.
        changed = network.settings.model_copy(update={"fault_bindings": []})
        recovered = CapacityExecutor(changed)
        recovered.start()
        try:
            status = wait_state(recovered, run, {"interrupted", "failed"})
            assert status.state == "interrupted" and status.result is None
            assert status.stop_scope == "runner_requests_and_fault"
            info = network.proxy.command(command(run, action="status"))
            assert info.state == "released" and info.active_connections == 0
            assert info.sequence == FINAL_SEQUENCE
            assert recovered.store.claim() is None
            assert network.isolated.count.value == 2
        finally:
            recovered.close()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        assert not process.is_alive()
        process.close()
        parent.close()
