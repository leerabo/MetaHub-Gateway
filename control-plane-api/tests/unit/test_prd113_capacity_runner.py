"""PRD-113 isolated runner authentication, permit, and kill-fence contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_capacity_runner.app import app
from metahub_capacity_runner.contracts import (
    CapacityKillClaims,
    CapacityPermitClaims,
    encode_signed,
)

TOKEN = "runner-token-" + "t" * 32
SECRET = "runner-permit-" + "s" * 32
LOAD_MANIFEST = "5f24c3049b4da0105935b1b2d49f3e88de76d550f3d3dc3028efd71ac360b5e4"
CHAOS_MANIFEST = "1259aae8d8f794dd5c1e91d47b37e217c0b439d6225f9e77a423f3bfeb9f2197"


def _claims(
    object_id: str,
    *,
    operation: str = "load_test",
    scenario_code: str | None = "gateway-release",
    fault_code: str | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> CapacityPermitClaims:
    now = datetime.now(UTC)
    return CapacityPermitClaims(
        permit_id="crp_" + object_id[-32:],
        operation=operation,
        object_id=object_id,
        service_code="gateway",
        scenario_code=scenario_code,
        fault_code=fault_code,
        environment_class="staging",
        region_code="cn-north-1",
        synthetic_tenant_count=100,
        target_rps=1600 if operation == "load_test" else 0,
        duration_seconds=60,
        objective_p95_ms=2000 if operation == "chaos" else 0,
        objective_error_rate_bps=100 if operation == "chaos" else 0,
        stop_error_rate_bps=500 if operation == "chaos" else 0,
        manifest_sha256=CHAOS_MANIFEST if operation == "chaos" else LOAD_MANIFEST,
        kill_generation=0,
        issued_at=issued_at or now,
        expires_at=expires_at or now + timedelta(minutes=5),
    )


@pytest.fixture(autouse=True)
def runner_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_PERMIT_SECRET", SECRET)


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_runner_requires_independent_credentials_and_valid_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_id = "ltr_" + "1" * 32
    permit = encode_signed(_claims(object_id), SECRET, domain="run-permit")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://runner"
    ) as client:
        unauthorized = await client.post(
            "/internal/capacity/runs", json={"permit": permit}
        )
        assert unauthorized.status_code == 401
        assert unauthorized.json()["detail"] == "runner.unauthorized"

        tampered = await client.post(
            "/internal/capacity/runs",
            headers=_headers(),
            json={"permit": permit[:-1] + ("0" if permit[-1] != "0" else "1")},
        )
        assert tampered.status_code == 401
        assert tampered.json()["detail"] == "runner.permit_invalid"

        monkeypatch.delenv("METAHUB_CAPACITY_RUNNER_PERMIT_SECRET")
        unavailable = await client.post(
            "/internal/capacity/runs", headers=_headers(), json={"permit": permit}
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == "runner.not_configured"

        monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_PERMIT_SECRET", TOKEN)
        reused = await client.post(
            "/internal/capacity/runs", headers=_headers(), json={"permit": permit}
        )
        assert reused.status_code == 503
        assert reused.json()["detail"] == "runner.not_configured"


@pytest.mark.anyio
async def test_runner_rejects_expired_future_and_unknown_catalog_permits() -> None:
    now = datetime.now(UTC)
    cases = (
        _claims(
            "ltr_" + "2" * 32,
            issued_at=now - timedelta(minutes=2),
            expires_at=now - timedelta(minutes=1),
        ),
        _claims(
            "ltr_" + "3" * 32,
            issued_at=now + timedelta(seconds=31),
            expires_at=now + timedelta(minutes=5),
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://runner"
    ) as client:
        for claims in cases:
            response = await client.post(
                "/internal/capacity/runs",
                headers=_headers(),
                json={
                    "permit": encode_signed(claims, SECRET, domain="run-permit")
                },
            )
            assert response.status_code == 401
            assert response.json()["detail"] == "runner.permit_invalid"

        unknown = _claims("ltr_" + "4" * 32, scenario_code="unknown-scenario")
        response = await client.post(
            "/internal/capacity/runs",
            headers=_headers(),
            json={"permit": encode_signed(unknown, SECRET, domain="run-permit")},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "runner.scenario_invalid"

        mismatched_manifest = _claims("ltr_" + "9" * 32).model_copy(
            update={"manifest_sha256": "f" * 64}
        )
        response = await client.post(
            "/internal/capacity/runs",
            headers=_headers(),
            json={
                "permit": encode_signed(
                    mismatched_manifest, SECRET, domain="run-permit"
                )
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "runner.scenario_invalid"


@pytest.mark.anyio
async def test_runner_does_not_report_simulated_measurements_as_execution() -> None:
    claims = _claims("ltr_" + "5" * 32)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://runner"
    ) as client:
        response = await client.post(
            "/internal/capacity/runs",
            headers=_headers(),
            json={"permit": encode_signed(claims, SECRET, domain="run-permit")},
        )
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "runner.executor_unavailable"}


@pytest.mark.anyio
async def test_kill_generation_is_idempotent_and_fences_old_permits() -> None:
    experiment_id = "che_" + "6" * 32
    run_claims = _claims(
        experiment_id,
        operation="chaos",
        scenario_code=None,
        fault_code="dependency-timeout",
    )
    now = datetime.now(UTC)
    kill_claims = CapacityKillClaims(
        experiment_id=experiment_id,
        kill_generation=1,
        issued_at=now,
        expires_at=now + timedelta(seconds=30),
    )
    run_permit = encode_signed(run_claims, SECRET, domain="run-permit")
    kill_permit = encode_signed(kill_claims, SECRET, domain="kill-permit")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://runner"
    ) as client:
        first = await client.post(
            f"/internal/capacity/runs/{experiment_id}/kill",
            headers=_headers(),
            json={"permit": kill_permit},
        )
        replay = await client.post(
            f"/internal/capacity/runs/{experiment_id}/kill",
            headers=_headers(),
            json={"permit": kill_permit},
        )
        fenced = await client.post(
            "/internal/capacity/runs",
            headers=_headers(),
            json={"permit": run_permit},
        )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert fenced.status_code == 409
    assert fenced.json()["detail"] == "runner.killed"


@pytest.mark.anyio
async def test_kill_permit_is_bound_to_path_scope() -> None:
    claimed_id = "che_" + "7" * 32
    path_id = "che_" + "8" * 32
    now = datetime.now(UTC)
    claims = CapacityKillClaims(
        experiment_id=claimed_id,
        kill_generation=1,
        issued_at=now,
        expires_at=now + timedelta(seconds=30),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://runner"
    ) as client:
        response = await client.post(
            f"/internal/capacity/runs/{path_id}/kill",
            headers=_headers(),
            json={
                "permit": encode_signed(claims, SECRET, domain="kill-permit")
            },
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "runner.scope_conflict"
