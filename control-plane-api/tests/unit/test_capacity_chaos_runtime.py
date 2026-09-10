"""Chaos client and journal contracts; protocol fixtures are not resilience evidence."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from metahub_capacity_runner.contracts import (
    CapacityExecutionInfo,
    CapacityRunnerResult,
    decode_signed,
    execution_digest,
    result_digest,
)
from metahub_capacity_runner.engine import simulate_capacity_run
from metahub_capacity_runner.fault_client import FINAL_SEQUENCE, FaultLeaseClient
from metahub_capacity_runner.fault_contracts import FaultLeaseCommand, FaultLeaseInfo
from metahub_capacity_runner.fault_execution import FaultExecutionStore
from metahub_capacity_runner.settings import CapacityFaultBinding
from metahub_capacity_runner.store import RunnerStore
from metahub_control.capacity_management.runner_client import _validate_result
from metahub_shared.runtime.errors import ApiError
from test_capacity_fault_proxy import execution


def fault_binding(tmp_path, *, origin="http://127.0.0.1:19003", service="gateway"):
    token, secret = tmp_path / "fault-token.secret", tmp_path / "fault-signing.secret"
    token.write_text("synthetic-token-" + "t" * 32, encoding="ascii")
    secret.write_text("synthetic-signing-" + "s" * 32, encoding="ascii")
    return CapacityFaultBinding(
        service_code=service,
        environment_class="staging",
        region_code="cn-north-1",
        fault_code="dependency-timeout",
        binding_code="synthetic-dependency",
        origin=origin,
        internal_token_file=token,
        permit_secret_file=secret,
        probe_service_code=service,
        probe_rps=20,
        recovery_timeout_seconds=10,
    )


def proof(run, binding, *, state="released", sequence=FINAL_SEQUENCE):
    now = datetime.now(UTC)
    return FaultLeaseInfo(
        protocol_version=3,
        object_id=run.object_id,
        execution_sha256=execution_digest(run),
        binding_code=binding.binding_code,
        state=state,
        sequence=sequence,
        lease_until=now + timedelta(seconds=5),
        end_at=now + timedelta(seconds=30),
        reset_connections=0,
        withheld_bytes=128,
        active_connections=0,
    )


class WireBytes(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload = payload

    async def __aiter__(self):
        yield self.payload


def response(payload, status=200, headers=None):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("ascii")
    return httpx.Response(status, headers=headers or {}, stream=WireBytes(raw))


def prepared(tmp_path):
    store, run = RunnerStore(tmp_path / "runner"), execution()
    binding = fault_binding(tmp_path)
    store.submit(run, "b" * 64)
    _, generation, _ = store.claim()
    effects = FaultExecutionStore(store)
    effects.prepare(run, generation, binding)
    return store, effects, run, generation, binding


def test_unconfirmed_effect_blocks_terminal_commit_and_raw_sql(tmp_path):
    store, effects, run, generation, binding = prepared(tmp_path)
    with pytest.raises(ValueError, match="fault_stop_unconfirmed"):
        store.finish(
            run.object_id,
            generation,
            result=None,
            state="failed",
            outcome="runner.execution_failed",
            metrics={},
        )
    with store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="fault reversal unconfirmed"):
            connection.execute("UPDATE executions SET state='failed'")
        with pytest.raises(sqlite3.IntegrityError, match="owner immutable"):
            connection.execute("UPDATE fault_effects SET binding_json='{}'")
    effects.reversed(run, generation, proof(run, binding))
    store.finish(
        run.object_id,
        generation,
        result=None,
        state="failed",
        outcome="runner.execution_failed",
        metrics={"target_recovered": False},
    )
    assert store.status(run).stop_scope == "runner_requests_and_fault"


def test_restart_keeps_fault_pending_and_fences_old_generation(tmp_path):
    store, effects, run, generation, binding = prepared(tmp_path)
    store.interrupted()
    assert store.status(run).state == "stopping"
    pending = FaultExecutionStore(RunnerStore(tmp_path / "runner")).pending()
    assert pending["generation"] == generation + 1
    assert pending["binding_json"] == binding.model_dump_json()
    assert pending["terminal_state"] == "interrupted"
    with pytest.raises(ValueError, match="execution_fenced"):
        effects.reversed(run, generation, proof(run, binding))
    effects.reversed(run, pending["generation"], proof(run, binding))
    store.finish(
        run.object_id,
        pending["generation"],
        result=None,
        state="interrupted",
        outcome="runner.interrupted",
        metrics={},
    )
    assert store.status(run).state == "interrupted"


def test_cancel_during_cleanup_overrides_late_failure(tmp_path):
    store, effects, run, generation, binding = prepared(tmp_path)
    effects.defer(
        run.object_id,
        generation,
        state="failed",
        outcome="runner.fault_stop_unconfirmed",
        metrics={"fault_sample_count": 4},
    )
    pending = effects.pending()
    assert store.cancel(run).state == "stopping"
    effects.reversed(run, generation, proof(run, binding))
    store.finish(
        run.object_id,
        generation,
        result=None,
        state=pending["terminal_state"],
        outcome=pending["terminal_outcome"],
        metrics={},
    )
    assert store.status(run).state == "cancelled"


@pytest.mark.parametrize("reversal_saved", [False, True])
def test_restart_preserves_cancel_intent_during_fault_cleanup(tmp_path, reversal_saved):
    store, effects, run, generation, binding = prepared(tmp_path)
    store.cancel(run)
    effects.defer(
        run.object_id, generation, state="interrupted", outcome="runner.interrupted", metrics={}
    )
    if reversal_saved:
        effects.reversed(run, generation, proof(run, binding))
    store.interrupted()
    if reversal_saved:
        assert store.status(run).state == "cancelled"
    else:
        pending = effects.pending()
        assert pending["terminal_state"] == "cancelled"
        assert pending["terminal_outcome"] == "runner.cancelled"
        store.interrupted()
        assert effects.pending()["terminal_state"] == "cancelled"


def test_preparation_requires_current_owner_and_does_not_save_credentials(tmp_path):
    store, effects, run, generation, binding = prepared(tmp_path)
    with pytest.raises(ValueError, match="execution_fenced"):
        effects.prepare(run, generation + 1, binding)
    store.cancel(run)
    with pytest.raises(ValueError, match="execution_fenced"):
        effects.prepare(run, generation, binding)
    with store.connection() as connection:
        stored = str([tuple(row) for row in connection.execute("SELECT * FROM fault_effects")])
    assert all(secret not in stored for secret in binding.credentials())


def test_reversal_cannot_confirm_while_old_connections_remain(tmp_path):
    store, effects, run, generation, binding = prepared(tmp_path)
    pending = proof(run, binding).model_copy(update={"active_connections": 1})
    with pytest.raises(ValueError):
        effects.reversed(run, generation, pending)
    assert effects.pending() is None  # Still owned by the running execution.
    with pytest.raises(ValueError, match="fault_stop_unconfirmed"):
        store.finish(
            run.object_id,
            generation,
            result=None,
            state="failed",
            outcome="runner.execution_failed",
            metrics={},
        )


@pytest.mark.anyio
async def test_release_waits_for_old_connections_and_recovers_lost_release_reply(tmp_path):
    binding, run = fault_binding(tmp_path), execution()
    actions = []

    async def serve(request):
        value = decode_signed(
            json.loads(request.content)["permit"],
            binding.credentials()[1],
            domain="fault-lease",
            model_type=FaultLeaseCommand,
        )
        actions.append(value.action)
        if value.action == "release":
            raise httpx.ReadError("synthetic-lost-release-reply")
        state = "active" if len(actions) == 1 else "released"
        info = proof(run, binding, state=state)
        info = info.model_copy(update={"active_connections": int(len(actions) < 4)})
        return response(info.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), trust_env=False) as http:
        client = FaultLeaseClient(http, binding, run)
        assert await client.release() is None
        released = await client.release()
        assert released.state == "released" and released.active_connections == 0
    assert actions == ["status", "release", "status", "status"]


@pytest.mark.anyio
async def test_client_uses_separate_signature_and_validates_exact_binding(tmp_path):
    binding, run = fault_binding(tmp_path), execution()
    token, secret = binding.credentials()

    async def serve(request):
        assert request.url.path == "/internal/capacity/faults"
        assert request.headers["Authorization"] == "Bearer " + token
        assert request.headers["Accept-Encoding"] == "identity"
        value = decode_signed(
            json.loads(request.content)["permit"],
            secret,
            domain="fault-lease",
            model_type=FaultLeaseCommand,
        )
        assert value.execution == run and value.action == "apply" and value.sequence == 1
        return response(proof(run, binding, state="active", sequence=1).model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), trust_env=False) as http:
        assert (await FaultLeaseClient(http, binding, run).command("apply")).state == "active"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    ["object", "digest", "binding", "sequence", "version", "large", "redirect", "compressed"],
)
async def test_client_rejects_unbound_old_oversized_and_redirected_replies(tmp_path, change):
    binding, run = fault_binding(tmp_path), execution()
    payload = proof(run, binding, state="active", sequence=1).model_dump(mode="json")
    if change == "object":
        payload["object_id"] = "che_" + "f" * 32
    elif change == "digest":
        payload["execution_sha256"] = "f" * 64
    elif change == "binding":
        payload["binding_code"] = "synthetic-other"
    elif change == "sequence":
        payload["sequence"] = 3
    elif change == "version":
        del payload["protocol_version"]
    elif change == "large":
        payload = b"private-reply" * 2000

    async def serve(_request):
        return response(
            payload,
            status=307 if change == "redirect" else 200,
            headers={"Content-Encoding": "gzip"} if change == "compressed" else None,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), trust_env=False) as http:
        with pytest.raises(ValueError, match="^runner.fault_unavailable$"):
            await FaultLeaseClient(http, binding, run).command("apply")


@pytest.mark.anyio
async def test_release_before_unknown_apply_creates_final_sequence_tombstone(tmp_path):
    binding, run = fault_binding(tmp_path), execution()
    actions = []

    async def serve(request):
        value = decode_signed(
            json.loads(request.content)["permit"],
            binding.credentials()[1],
            domain="fault-lease",
            model_type=FaultLeaseCommand,
        )
        actions.append((value.action, value.sequence))
        if value.action == "status":
            return response({}, status=404)
        return response(proof(run, binding).model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), trust_env=False) as http:
        assert (await FaultLeaseClient(http, binding, run).release()).state == "released"
    assert actions == [("status", 1), ("release", FINAL_SEQUENCE)]


def measured_fixture(run):
    value = simulate_capacity_run(run).model_dump(mode="json", exclude_none=True)
    value["evidence_kind"] = "measured"
    value["chaos_evidence"] = {
        "baseline_sample_count": 2,
        "fault_sample_count": 2,
        "recovery_sample_count": 6,
        "reset_connections": 0,
        "withheld_bytes": 128,
        "injected_duration_ms": 10000,
        "recovery_duration_ms": 100,
        "fault_binding_sha256": "a" * 64,
        "target_configuration_sha256": "b" * 64,
        "fault_state": "released",
        "requests_stopped": True,
        "fault_reversed": True,
        "target_recovered": True,
    }
    value["result_sha256"] = result_digest(value)
    return value


@pytest.mark.parametrize("missing", ["chaos_evidence", "fault_reversed", "target_recovered"])
def test_measured_chaos_cannot_omit_reversal_and_recovery(missing):
    value = measured_fixture(execution())
    if missing == "chaos_evidence":
        del value[missing]
    else:
        del value["chaos_evidence"][missing]
    value["result_sha256"] = result_digest(value)
    with pytest.raises(ValueError):
        CapacityRunnerResult.model_validate(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("baseline_sample_count", 1),
        ("recovery_sample_count", 1),
        ("injected_duration_ms", 5000),
        ("withheld_bytes", 0),
    ],
)
def test_control_validates_measured_chaos_against_original_request(field, value):
    run = execution()
    payload = measured_fixture(run)
    assert _validate_result(CapacityRunnerResult.model_validate(payload), run).objective_met
    payload["chaos_evidence"][field] = value
    if field == "withheld_bytes":
        payload["chaos_evidence"]["reset_connections"] = 1
    payload["result_sha256"] = result_digest(payload)
    with pytest.raises(ApiError) as rejected:
        _validate_result(CapacityRunnerResult.model_validate(payload), run)
    assert rejected.value.code == "capacity.runner_unavailable"


def test_terminal_chaos_cannot_claim_only_request_stop():
    run = execution()
    with pytest.raises(ValueError, match="fault stop is unconfirmed"):
        CapacityExecutionInfo(
            object_id=run.object_id,
            execution_sha256=execution_digest(run),
            state="cancelled",
            safe_outcome_code="runner.cancelled",
            sample_count=0,
            elapsed_ms=0,
        )
