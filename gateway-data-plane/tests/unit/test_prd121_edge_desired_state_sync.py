"""PRD-121 Gateway desired-state HTTP, LKG, ACK, and local-read tests."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_gateway.config import GatewayDesiredStateSynchronizer
from metahub_shared.config_distribution import canonical_json_bytes as config_json_bytes
from metahub_shared.private_gateway import (
    EdgeDesiredStateError,
    EdgeDesiredStateStore,
    canonical_edge_json_bytes,
    public_key_b64,
    sign_desired_state,
    sign_trust_manifest,
    strict_json_object,
)
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo

WORKSPACE_ID = "wsp_aba85648d6c3c0119b4a41bddf82762b"
TENANT_ID = "944f3e6d-8b66-584e-b095-7cac890a5f87"
DEPLOYMENT_ID = "edp_11111111111111111111111111111111"
SESSION_TOKEN = "egs_" + "A" * 43
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
KID = "edge-ds-current-v1"
NOW = datetime(2026, 8, 26, 1, tzinfo=UTC)


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


def _public_key() -> bytes:
    return KEY.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _source_manifest(*, generation: int = 1) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "workspace_id": WORKSPACE_ID,
        "tenant_id": TENANT_ID,
        "generation": generation,
        "revocation_watermark": 0,
        "workspace": {"status": "active", "revision": 1},
        "placement": {
            "cell_code": "cn-north-1-a",
            "region_code": "cn-north-1",
            "revision": 1,
        },
        "entitlements": None,
        "policy": None,
        "provider_key_refs": [],
        "feature_capabilities": [],
    }
    return {**value, "source_sha256": hashlib.sha256(config_json_bytes(value)).hexdigest()}


def _wire(*, generation: int = 1, base_generation: int = 0) -> dict[str, object]:
    trust = sign_trust_manifest(
        {
            "domain": "metahub.edge-trust-manifest.v1",
            "protocol_version": 1,
            "generation": 1,
            "issued_at": "2026-08-26T00:00:00Z",
            "expires_at": "2027-08-26T00:00:00Z",
            "signer_kid": KID,
            "current": {
                "alg": "Ed25519",
                "kid": KID,
                "public_key": public_key_b64(KEY),
            },
            "next": None,
            "revoked_kids": [],
        },
        KEY,
    )
    content = {
        "schema_version": 1,
        "deployment": {
            "deployment_id": DEPLOYMENT_ID,
            "mode": "connected",
            "region_code": "cn-north-1",
            "revision": 1,
        },
        "source_manifest": _source_manifest(generation=generation),
    }
    snapshot = sign_desired_state(
        {
            "domain": "metahub.desired-state.v1",
            "protocol_version": 1,
            "snapshot_id": "edsn_" + format(generation, "032x"),
            "artifact_id": "esa_" + format(generation, "032x"),
            "workspace_id": WORKSPACE_ID,
            "tenant_id": TENANT_ID,
            "deployment_id": DEPLOYMENT_ID,
            "generation": generation,
            "base_generation": base_generation,
            "revocation_generation": 1,
            "issued_at": "2026-08-26T00:00:00Z",
            "not_before": "2026-08-26T00:00:00Z",
            "security_expires_at": "2026-08-27T00:00:00Z",
            "expires_at": "2026-09-02T00:00:00Z",
            "alg": "Ed25519",
            "kid": KID,
            "content_sha256": hashlib.sha256(canonical_edge_json_bytes(content)).hexdigest(),
            "content": content,
        },
        KEY,
    )
    return {"trust_manifest": trust, "snapshot": snapshot}


def _pull(wire: dict[str, object]) -> tuple[bytes, str]:
    snapshot = wire["snapshot"]
    envelope_sha256 = hashlib.sha256(canonical_edge_json_bytes(snapshot)).hexdigest()
    body = canonical_edge_json_bytes(
        {
            "deployment_id": DEPLOYMENT_ID,
            "generation": snapshot["generation"],
            "envelope_sha256": envelope_sha256,
            "snapshot": snapshot,
            "trust_manifest": wire["trust_manifest"],
            "request_id": "request-prd121-gateway",
            "trace_id": "trace-prd121-gateway",
        }
    )
    return body, f'"{envelope_sha256}"'


def _store(root) -> EdgeDesiredStateStore:
    return EdgeDesiredStateStore(
        root,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        pinned_keys={KID: _public_key()},
        now=lambda: NOW,
    )


def _ack_response(
    payload: dict[str, object],
    *,
    replay: bool,
    disposition: str = "accepted",
    desired_generation: int | None = None,
    applied_generation: int | None = None,
    lkg_generation: int | None = None,
) -> httpx.Response:
    generation = int(payload["generation"])
    applied = generation if payload["status"] == "applied" else 0
    return httpx.Response(
        200,
        json={
            "ack_id": "eak_" + "1" * 32,
            "disposition": disposition,
            "status": payload["status"],
            "desired_generation": desired_generation or generation,
            "applied_generation": applied if applied_generation is None else applied_generation,
            "lkg_generation": (
                int(payload["lkg_generation"])
                if lkg_generation is None
                else lkg_generation
            ),
            "idempotent_replay": replay,
            "request_id": "request-prd121-ack",
            "trace_id": "trace-prd121-ack",
        },
    )


@pytest.mark.anyio
async def test_pull_apply_ack_replay_restart_lkg_and_local_read_slo(tmp_path) -> None:
    wire = _wire()
    body, etag = _pull(wire)
    ack_payloads: list[dict[str, object]] = []

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
        ack_payloads.append(payload)
        return _ack_response(payload, replay=len(ack_payloads) > 1)

    store = _store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sync = GatewayDesiredStateSynchronizer(
            _config(),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            store=store,
            client=client,
        )
        first = await sync.refresh()
        second = await sync.refresh()
        assert first.status == "applied" and first.acknowledged is True
        assert second.status == "not_modified" and second.acknowledged is True
        assert ack_payloads[0] == ack_payloads[1]
        assert ack_payloads[0]["attempt_nonce"].startswith("ack_")
        assert sync.decision().status == "current"

        samples: list[int] = []
        for _ in range(2_000):
            started = time.perf_counter_ns()
            assert sync.content()["deployment"]["deployment_id"] == DEPLOYMENT_ID
            samples.append(time.perf_counter_ns() - started)
        assert sorted(samples)[int(len(samples) * 0.95)] < 2_000_000

    restarted = _store(tmp_path)
    assert restarted.decision().status == "lkg"

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        sync = GatewayDesiredStateSynchronizer(
            _config(),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            store=restarted,
            client=client,
        )
        result = await sync.refresh()
        assert result.status == "unreachable"
        assert sync.decision().status == "lkg"


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [401, 403, 404])
async def test_authorization_and_owner_responses_quarantine_across_restart(
    tmp_path, status_code: int
) -> None:
    root = tmp_path / str(status_code)
    store = _store(root)
    store.apply(_wire())

    def rejected(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    async with httpx.AsyncClient(transport=httpx.MockTransport(rejected)) as client:
        sync = GatewayDesiredStateSynchronizer(
            _config(),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            store=store,
            client=client,
        )
        with pytest.raises(EdgeDesiredStateError) as rejected_response:
            await sync.refresh()
        assert rejected_response.value.code == "edge_desired_state.transport_rejected"
        assert sync.decision().status == "integrity_failed"

    assert _store(root).decision().status == "integrity_failed"


@pytest.mark.anyio
async def test_only_network_failures_fallback_and_protocol_errors_fail_closed(tmp_path) -> None:
    network_root = tmp_path / "network"
    network_store = _store(network_root)
    network_store.apply(_wire())

    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("control unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as client:
        sync = GatewayDesiredStateSynchronizer(
            _config(),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            store=network_store,
            client=client,
        )
        assert (await sync.refresh()).status == "unreachable"
        assert sync.decision().status == "lkg"

    protocol_root = tmp_path / "protocol"
    protocol_store = _store(protocol_root)
    protocol_store.apply(_wire())

    def broken_protocol(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("invalid response framing", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken_protocol)) as client:
        sync = GatewayDesiredStateSynchronizer(
            _config(),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            store=protocol_store,
            client=client,
        )
        with pytest.raises(EdgeDesiredStateError) as protocol:
            await sync.refresh()
        assert protocol.value.code == "edge_desired_state.transport_rejected"
        assert sync.decision().status == "integrity_failed"


@pytest.mark.anyio
async def test_noncanonical_or_invalid_signature_response_never_uses_lkg(tmp_path) -> None:
    cases: list[tuple[str, bytes, str]] = []
    valid = _wire()
    valid_body, valid_etag = _pull(valid)
    cases.append(
        ("noncanonical", json.dumps(json.loads(valid_body), indent=2).encode(), valid_etag)
    )

    tampered = copy.deepcopy(valid)
    signature = str(tampered["snapshot"]["signature"])
    tampered["snapshot"]["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
    tampered_body, tampered_etag = _pull(tampered)
    cases.append(("signature", tampered_body, tampered_etag))

    for name, body, etag in cases:
        root = tmp_path / name
        store = _store(root)
        store.apply(valid)

        def invalid(_request: httpx.Request, body=body, etag=etag) -> httpx.Response:
            return httpx.Response(
                200,
                content=body,
                headers={"Content-Type": "application/json", "ETag": etag},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(invalid)) as client:
            sync = GatewayDesiredStateSynchronizer(
                _config(),
                deployment_id=DEPLOYMENT_ID,
                session_token=lambda: SESSION_TOKEN,
                store=store,
                client=client,
            )
            with pytest.raises(EdgeDesiredStateError):
                await sync.refresh()
            assert sync.decision().status == "integrity_failed"
        assert _store(root).decision().status == "integrity_failed"


@pytest.mark.anyio
async def test_ack_outage_retries_identical_payload_without_blocking_applied_state(
    tmp_path,
) -> None:
    body, etag = _pull(_wire())
    attempts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                content=body,
                headers={"Content-Type": "application/json", "ETag": etag},
            )
        payload = strict_json_object(request.content, canonical=True)
        attempts.append(payload)
        if len(attempts) == 1:
            return httpx.Response(503)
        return _ack_response(
            payload,
            replay=True,
            disposition="late",
            desired_generation=int(payload["generation"]) + 1,
            applied_generation=0,
            lkg_generation=0,
        )

    store = _store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sync = GatewayDesiredStateSynchronizer(
            _config(),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            store=store,
            client=client,
        )
        first = await sync.refresh()
        assert first.status == "applied" and first.acknowledged is False
        assert sync.decision().status == "current"
        second = await sync.refresh()
        assert second.status == "not_modified" and second.acknowledged is True
        assert attempts[0] == attempts[1]


@pytest.mark.anyio
async def test_verified_storage_failure_sends_failed_ack(monkeypatch, tmp_path) -> None:
    body, etag = _pull(_wire())
    acks: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                content=body,
                headers={"Content-Type": "application/json", "ETag": etag},
            )
        payload = strict_json_object(request.content, canonical=True)
        acks.append(payload)
        return _ack_response(payload, replay=False)

    def storage_failure(_path, _value) -> None:
        raise EdgeDesiredStateError("edge_desired_state.storage_unavailable")

    store = _store(tmp_path)
    monkeypatch.setattr(
        "metahub_shared.private_gateway.desired_state._atomic_write",
        storage_failure,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sync = GatewayDesiredStateSynchronizer(
            _config(),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            store=store,
            client=client,
        )
        with pytest.raises(EdgeDesiredStateError) as failed:
            await sync.refresh()
        assert failed.value.code == "edge_desired_state.storage_unavailable"
    assert acks[0]["status"] == "failed"
    assert acks[0]["reason_code"] == "storage_failed"
    assert acks[0]["lkg_generation"] == 0
