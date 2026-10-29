"""PRD-128 Gateway license HTTP sync, LKG, ACK, and local-decision tests."""

from __future__ import annotations

import copy
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_gateway.config import (
    GatewayLicenseSynchronizer,
    LicenseUsageSnapshot,
)
from metahub_shared.private_gateway import (
    EdgeLicenseStore,
    LicenseCapacity,
    LicenseContractError,
    LicenseEntitlement,
    canonical_edge_json_bytes,
    sign_license_entitlement,
    strict_json_object,
)
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo

WORKSPACE_ID = "wsp_" + "1" * 32
TENANT_ID = "11111111-1111-4111-8111-111111111111"
DEPLOYMENT_ID = "edp_" + "2" * 32
LICENSE_ID = "lic_" + "3" * 32
SESSION_TOKEN = "egs_" + "A" * 43
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
KEY_ID = "edge-license-sync-v1"
NOW = datetime(2026, 8, 28, 4, 0, tzinfo=UTC)


def _config() -> AppConfig:
    return AppConfig(
        service_name="gateway-data-plane",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=NOW,
        ),
        auth_token_secret="a" * 43,
        cache_affinity_hmac_secret="b" * 43,
        platform_session_secret="c" * 43,
        control_api_base_url="https://control.test",
        control_background_schedulers_enabled=False,
    )


def _signed(*, generation: int = 1, concurrency: int = 10):
    expires_at = NOW + timedelta(days=30)
    return sign_license_entitlement(
        LicenseEntitlement(
            entitlement_id="let_" + f"{generation:032x}",
            license_id=LICENSE_ID,
            workspace_id=WORKSPACE_ID,
            tenant_id=TENANT_ID,
            deployment_id=DEPLOYMENT_ID,
            device_generation=1,
            commercial_source_type="entitlement_snapshot",
            commercial_source_id="ent_" + "4" * 32,
            commercial_source_revision=1,
            generation=generation,
            refresh_counter=generation,
            revocation_generation=0,
            status="active",
            capacity=LicenseCapacity(
                node_limit=3,
                concurrent_request_limit=concurrency,
                features=["advanced_routing", "policy_enforcement"],
            ),
            issued_at=NOW,
            not_before=NOW,
            expires_at=expires_at,
            grace_ends_at=expires_at + timedelta(days=7),
        ),
        private_key=KEY,
        key_id=KEY_ID,
    )


def _pull(signed) -> tuple[bytes, str]:
    body = canonical_edge_json_bytes(
        {
            "deployment_id": DEPLOYMENT_ID,
            "entitlement": signed.model_dump(mode="json"),
            "server_time": NOW.isoformat().replace("+00:00", "Z"),
            "request_id": "request-prd128-gateway",
            "trace_id": "trace-prd128-gateway",
        }
    )
    return body, f'"{signed.manifest_sha256}"'


def _store(root) -> EdgeLicenseStore:
    return EdgeLicenseStore(
        root,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        device_generation=1,
        trusted_keys={KEY_ID: KEY.public_key().public_bytes_raw()},
    )


def _usage() -> LicenseUsageSnapshot:
    return LicenseUsageSnapshot(
        active_node_units=1,
        active_request_units=4,
        denied_count=0,
        bucket_started_at=NOW - timedelta(minutes=5),
        bucket_ended_at=NOW,
    )


def _ack_response(payload: dict[str, object], *, replay: bool) -> httpx.Response:
    return httpx.Response(
        200 if replay else 201,
        json={
            "deployment_id": DEPLOYMENT_ID,
            "acknowledgement": {
                "acknowledgement_id": "lak_" + "5" * 32,
                "generation": payload["generation"],
                "disposition": payload["disposition"],
                "decision_status": payload["decision_status"],
                "reason_code": payload["reason_code"],
                "acknowledged_at": NOW.isoformat().replace("+00:00", "Z"),
            },
            "idempotent_replay": replay,
            "request_id": "request-prd128-ack",
            "trace_id": "trace-prd128-ack",
        },
    )


@pytest.mark.anyio
async def test_pull_apply_ack_replay_and_local_decision_slo(tmp_path) -> None:
    body, etag = _pull(_signed())
    acknowledgements: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Edge-Session"] == SESSION_TOKEN
        assert request.headers["Accept-Encoding"] == "identity"
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        if request.method == "GET":
            return httpx.Response(
                200,
                content=body,
                headers={"Content-Type": "application/json", "ETag": etag},
            )
        payload = strict_json_object(request.content, canonical=True)
        acknowledgements.append(payload)
        return _ack_response(payload, replay=len(acknowledgements) > 1)

    store = _store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sync = GatewayLicenseSynchronizer(
            _config(),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            store=store,
            usage_probe=_usage,
            client=client,
        )
        first = await sync.refresh()
        second = await sync.refresh()
        assert first.status == "applied" and first.acknowledged is True
        assert second.status == "not_modified" and second.acknowledged is True
        assert acknowledgements[0] == acknowledgements[1]
        assert acknowledgements[0]["attempt_nonce"].startswith("ack_")
        assert sync.decision(now=NOW).status == "current"

        samples: list[int] = []
        for _ in range(2_000):
            started = time.perf_counter_ns()
            assert sync.decision(now=NOW).new_capacity_allowed
            samples.append(time.perf_counter_ns() - started)
        assert sorted(samples)[int(len(samples) * 0.95)] < 2_000_000

    restarted = _store(tmp_path)
    assert restarted.snapshot().signed.entitlement.generation == 1


@pytest.mark.anyio
async def test_network_failure_preserves_lkg_without_ack(tmp_path) -> None:
    store = _store(tmp_path)
    store.apply(_signed().model_dump(mode="json"), now=NOW)

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        sync = GatewayLicenseSynchronizer(
            _config(),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            store=store,
            usage_probe=_usage,
            client=client,
        )
        result = await sync.refresh()
        assert result.status == "unreachable"
        assert result.acknowledged is False
        assert sync.decision(now=NOW).status == "current"


@pytest.mark.anyio
async def test_invalid_signature_is_rejected_and_current_lkg_is_unchanged(tmp_path) -> None:
    current = _signed()
    candidate = copy.deepcopy(_signed(generation=2, concurrency=20).model_dump(mode="json"))
    signature = candidate["signature"]["value"]
    candidate["signature"]["value"] = ("A" if signature[0] != "A" else "B") + signature[1:]
    candidate_body = canonical_edge_json_bytes(
        {
            "deployment_id": DEPLOYMENT_ID,
            "entitlement": candidate,
            "server_time": NOW.isoformat().replace("+00:00", "Z"),
            "request_id": "request-prd128-invalid",
            "trace_id": "trace-prd128-invalid",
        }
    )
    rejected: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                content=candidate_body,
                headers={
                    "Content-Type": "application/json",
                    "ETag": f'"{candidate["manifest_sha256"]}"',
                },
            )
        payload = strict_json_object(request.content, canonical=True)
        rejected.append(payload)
        return _ack_response(payload, replay=False)

    store = _store(tmp_path)
    store.apply(current.model_dump(mode="json"), now=NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sync = GatewayLicenseSynchronizer(
            _config(),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            store=store,
            usage_probe=_usage,
            client=client,
        )
        with pytest.raises(LicenseContractError, match="edge_license.signature_invalid"):
            await sync.refresh()
    assert rejected[0]["disposition"] == "rejected"
    assert rejected[0]["reason_code"] == "edge_license.signature_invalid"
    assert store.snapshot().signed.manifest_sha256 == current.manifest_sha256
