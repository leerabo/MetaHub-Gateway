"""PRD-113 result provenance, execution binding and integrity checks."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from metahub_capacity_runner.contracts import (
    CapacityPermitClaims,
    CapacityRunnerResult,
    execution_digest,
    result_digest,
)
from metahub_capacity_runner.engine import SCENARIOS, simulate_capacity_run
from metahub_control.capacity_management import runner_client, runtime
from metahub_control.platform_jobs.runtime import PlatformJobFailure
from metahub_shared.runtime.errors import ApiError
from pydantic import ValidationError


@pytest.fixture
def claims():
    now = datetime.now(UTC)
    return CapacityPermitClaims(
        permit_id="crp_" + "a" * 32,
        operation="load_test",
        object_id="ltr_" + "a" * 32,
        service_code="gateway",
        scenario_code="gateway-release",
        environment_class="staging",
        region_code="cn-north-1",
        synthetic_tenant_count=100,
        target_rps=100,
        duration_seconds=10,
        objective_p95_ms=0,
        objective_error_rate_bps=0,
        stop_error_rate_bps=0,
        manifest_sha256=SCENARIOS["gateway-release"][-1],
        kill_generation=0,
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
    )


@pytest.fixture
def config():
    return SimpleNamespace(
        environment="test",
        capacity_runner_mode="isolated_http",
        capacity_runner_base_url="http://runner.test",
        capacity_runner_internal_token="test-runner-token-" + "a" * 32,
        capacity_runner_permit_secret="test-runner-secret-" + "b" * 32,
    )


def _fixture_result(claims):
    # A protocol fixture, never actual performance evidence.
    payload = simulate_capacity_run(claims).model_dump(mode="json")
    payload["evidence_kind"] = "measured"
    payload["result_sha256"] = result_digest(payload)
    return payload


def _http_result(monkeypatch, payload, *, status=200):
    def post(_url, **kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(runner_client.httpx, "post", post)


def test_bound_result_roundtrip_and_explicit_network_policy(monkeypatch, claims, config):
    payload = _fixture_result(claims)
    _http_result(monkeypatch, payload)
    result = runner_client.execute_capacity_run(config, claims)
    assert result.result_sha256 == payload["result_sha256"]
    assert result.execution_sha256 == execution_digest(claims)


@pytest.mark.parametrize("field,value", [
    ("object_id", "ltr_" + "b" * 32),
    ("manifest_sha256", "f" * 64),
    ("execution_sha256", "f" * 64),
    ("evidence_kind", "simulation"),
])
def test_control_rejects_other_execution_even_with_valid_digest(
    monkeypatch, claims, config, field, value
):
    payload = _fixture_result(claims)
    payload[field] = value
    payload["result_sha256"] = result_digest(payload)
    _http_result(monkeypatch, payload)
    with pytest.raises(ApiError) as rejected:
        runner_client.execute_capacity_run(config, claims)
    assert rejected.value.code == "capacity.runner_unavailable"


@pytest.mark.parametrize("field,value", [
    ("p50_ms", 9999),
    ("p95_ms", 9999),
    ("sample_count", 0),
    ("sample_count", True),
    ("error_rate_bps", 10001),
    ("safe_outcome_code", "runner.arbitrary_text"),
    ("object_id", "che_" + "a" * 32),
    ("contract_version", 1),
])
def test_result_invariants_are_checked_even_with_valid_digest(claims, field, value):
    payload = _fixture_result(claims)
    payload[field] = value
    payload["result_sha256"] = result_digest(payload)
    with pytest.raises(ValidationError):
        CapacityRunnerResult.model_validate(payload)


@pytest.mark.parametrize("field", [
    "p95_ms", "sample_count", "safe_outcome_code", "execution_sha256", "evidence_kind",
    "result_sha256",
])
def test_result_checksum_covers_metrics_outcome_and_provenance(claims, field):
    payload = _fixture_result(claims)
    payload[field] = payload[field] + 1 if isinstance(payload[field], int) else "f" * 64
    with pytest.raises(ValidationError):
        CapacityRunnerResult.model_validate(payload)


@pytest.mark.parametrize("field", [
    "contract_version", "evidence_kind", "execution_sha256", "manifest_sha256", "result_sha256",
])
def test_legacy_or_incomplete_result_is_not_accepted(monkeypatch, claims, config, field):
    payload = _fixture_result(claims)
    del payload[field]
    _http_result(monkeypatch, payload)
    with pytest.raises(ApiError) as rejected:
        runner_client.execute_capacity_run(config, claims)
    assert rejected.value.code == "capacity.runner_unavailable"


@pytest.mark.parametrize("field,value", [
    ("target_rps", 101), ("duration_seconds", 20), ("environment_class", "production"),
    ("region_code", "cn-south-1"), ("kill_generation", 1), ("synthetic_tenant_count", 99),
    ("scenario_code", "gateway-soak"), ("manifest_sha256", "f" * 64),
])
def test_execution_binding_covers_effective_parameters(claims, field, value):
    assert execution_digest(claims) != execution_digest(claims.model_copy(update={field: value}))


def test_execution_binding_survives_permit_renewal(claims):
    renewed = claims.model_copy(update={
        "issued_at": claims.issued_at + timedelta(seconds=30),
        "expires_at": claims.expires_at + timedelta(seconds=30),
    })
    assert execution_digest(claims) == execution_digest(renewed)


@pytest.mark.parametrize("environment", ["dev", "staging", "production"])
def test_simulation_is_only_available_to_explicit_test_environment(claims, config, environment):
    config.environment = environment
    config.capacity_runner_mode = "test_loopback"
    with pytest.raises(ApiError) as rejected:
        runner_client.execute_capacity_run(config, claims)
    assert rejected.value.code == "capacity.runner_unavailable"


def test_test_loopback_is_explicitly_marked(claims, config):
    config.capacity_runner_mode = "test_loopback"
    result = runner_client.execute_capacity_run(config, claims)
    assert result.evidence_kind == "simulation"
    assert result.result_sha256 == result_digest(result.model_dump(mode="json"))


@pytest.mark.parametrize("payload", [
    {},
    {"accepted": False, "experiment_id": "che_" + "a" * 32, "kill_generation": 2},
    {"accepted": True, "experiment_id": "che_" + "b" * 32, "kill_generation": 2},
    {"accepted": True, "experiment_id": "che_" + "a" * 32, "kill_generation": 1},
])
def test_kill_requires_bound_positive_current_acknowledgement(monkeypatch, config, payload):
    _http_result(monkeypatch, payload)
    with pytest.raises(ApiError) as rejected:
        runner_client.signal_capacity_kill(config, experiment_id="che_" + "a" * 32, generation=2)
    assert rejected.value.code == "capacity.runner_unavailable"


def test_kill_accepts_current_or_newer_fence(monkeypatch, config):
    _http_result(monkeypatch, {
        "accepted": True, "experiment_id": "che_" + "a" * 32, "kill_generation": 3,
    })
    runner_client.signal_capacity_kill(config, experiment_id="che_" + "a" * 32, generation=2)


@pytest.mark.parametrize("invalid_kind", ["corrupted", "simulation", "legacy", "unavailable"])
def test_rejected_result_never_reaches_baseline_or_rollout_completion(
    monkeypatch, claims, config, invalid_kind
):
    payload = _fixture_result(claims)
    if invalid_kind == "corrupted":
        payload["p95_ms"] += 1
    elif invalid_kind == "simulation":
        payload = simulate_capacity_run(claims).model_dump(mode="json")
    elif invalid_kind == "legacy":
        del payload["evidence_kind"]
    _http_result(monkeypatch, payload, status=503 if invalid_kind == "unavailable" else 200)
    effects = []
    failures = []
    monkeypatch.setattr(runtime, "_load_claims", lambda *_args: claims)
    monkeypatch.setattr(runtime, "start_load_test_in_transaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime, "fail_load_test_in_transaction",
        lambda *_args, **kwargs: failures.append(kwargs["outcome_code"]),
    )

    def forbidden_completion(*_args, **_kwargs):
        pytest.fail("unverified evidence reached baseline/rollout completion")

    monkeypatch.setattr(runtime, "complete_load_test_in_transaction", forbidden_completion)

    def commit_effect(key, _value, apply):
        effects.append(key)
        apply(None)

    context = SimpleNamespace(config=config, checkpoint=lambda _: None, commit_effect=commit_effect)
    job = runtime.CapacityRunInput(
        operation="load_test", object_id=claims.object_id, manifest_sha256=claims.manifest_sha256
    )
    with pytest.raises(PlatformJobFailure, match="capacity.runner_unavailable"):
        runtime._capacity_handler(context, job)
    assert effects == ["capacity.load.start", "capacity.load_test.failure"]
    assert failures == ["capacity.runner_unavailable"]
