"""Gateway PRD-108 HTTP synchronization, ACK, integrity, and restart LKG tests."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_gateway.config import GatewayConfigSynchronizer
from metahub_shared.config_distribution import (
    ConfigDistributionError,
    ConfigSnapshotOwner,
    canonical_json_bytes,
)
from metahub_shared.config_distribution import (
    test_kms_verifier as _test_kms_verifier,
)
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo

WORKSPACE_ID = "wsp_aba85648d6c3c0119b4a41bddf82762b"
TENANT_ID = "944f3e6d-8b66-584e-b095-7cac890a5f87"
SNAPSHOT_ID = "csn_11111111111111111111111111111111"
MASTER_KEY = "gateway-config-test-master-key-00000001"
INTERNAL_TOKEN = "gateway-config-internal-token-00000001"
GATEWAY_IDENTITY = "gateway-prd108-unit"


def _config() -> AppConfig:
    return AppConfig(
        service_name="gateway-data-plane",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=datetime.now(UTC),
        ),
        auth_token_secret="a" * 43,
        cache_affinity_hmac_secret="b" * 43,
        platform_session_secret="c" * 43,
        internal_config_token=INTERNAL_TOKEN,
        control_api_base_url="https://control.test",
        control_background_schedulers_enabled=False,
    )


def _wire(
    *,
    generation: int = 1,
    revocation_watermark: int = 0,
    source_sha256: str | None = None,
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema_version": 1,
        "workspace_id": WORKSPACE_ID,
        "tenant_id": TENANT_ID,
        "generation": generation,
        "revocation_watermark": revocation_watermark,
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
    safe = dict(manifest)
    manifest["source_sha256"] = source_sha256 or hashlib.sha256(
        canonical_json_bytes(safe)
    ).hexdigest()
    canonical = canonical_json_bytes(manifest)
    seed = hashlib.sha256(
        b"prd108/test-kms-signing/v1\0" + MASTER_KEY.encode("utf-8")
    ).digest()
    signature = Ed25519PrivateKey.from_private_bytes(seed).sign(canonical)
    return {
        "wire_schema_version": 1,
        "kind": "full",
        "snapshot_id": SNAPSHOT_ID,
        "workspace_id": WORKSPACE_ID,
        "generation": generation,
        "revocation_watermark": revocation_watermark,
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
        "kms_key_version": "test-v1",
        "signature_algorithm": "ed25519",
        "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
        "published_at": "2026-08-23T00:00:00Z",
        "stale_policy": {"lkg_max_seconds": 30, "revocation_max_seconds": 2},
        "request_id": "request-prd108-gateway",
        "trace_id": "trace-prd108-gateway",
    }


@pytest.mark.anyio
async def test_http_sync_ack_304_and_restart_lkg(tmp_path) -> None:
    wire = _wire()
    body = canonical_json_bytes(wire)
    etag = f'"{wire["manifest_sha256"]}"'
    calls: list[tuple[str, str]] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        calls.append((request.method, request.url.path))
        assert request.headers["X-Gateway-Identity"] == GATEWAY_IDENTITY
        assert request.headers["X-Internal-Token"] == INTERNAL_TOKEN
        if request.method == "GET":
            get_count += 1
            if get_count == 1:
                return httpx.Response(
                    200,
                    content=body,
                    headers={"Content-Type": "application/json", "ETag": etag},
                )
            assert request.url.params["after_generation"] == "1"
            assert request.headers["If-None-Match"] == etag
            return httpx.Response(304, headers={"ETag": etag})
        assert request.url.path == f"/internal/config-snapshots/{SNAPSHOT_ID}/acks"
        ack = json.loads(request.content)
        assert ack == {
            "generation": 1,
            "manifest_sha256": wire["manifest_sha256"],
            "status": "applied",
        }
        return httpx.Response(
            200,
            json={
                "ack_id": "cak_" + "1" * 32,
                "disposition": "accepted",
                "observed_generation": 1,
                "applied_generation": 1,
                "lkg_generation": 1,
                "idempotent_replay": False,
                "request_id": "request-prd108-ack",
                "trace_id": "trace-prd108-ack",
            },
        )

    clock = [datetime(2026, 8, 23, 0, 0, 1, tzinfo=UTC)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sync = GatewayConfigSynchronizer(
            _config(),
            gateway_identity=GATEWAY_IDENTITY,
            verifier=_test_kms_verifier(MASTER_KEY),
            client=client,
            lkg_root=tmp_path,
            now=lambda: clock[0],
        )
        sync.register_workspace(
            workspace_id=WORKSPACE_ID,
            tenant_id=TENANT_ID,
            cell_code="cn-north-1-a",
        )
        applied = await sync.refresh(WORKSPACE_ID)
        assert applied.status == "applied"
        assert applied.acknowledged is True
        assert sync.decision(WORKSPACE_ID).status == "current"
        clock[0] += timedelta(seconds=1)
        unchanged = await sync.refresh(WORKSPACE_ID)
        assert unchanged.status == "not_modified"
        assert unchanged.acknowledged is True

    assert calls == [
        ("GET", f"/internal/config-snapshots/{WORKSPACE_ID}"),
        ("POST", f"/internal/config-snapshots/{SNAPSHOT_ID}/acks"),
        ("GET", f"/internal/config-snapshots/{WORKSPACE_ID}"),
    ]

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("control unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        restarted = GatewayConfigSynchronizer(
            _config(),
            gateway_identity=GATEWAY_IDENTITY,
            verifier=_test_kms_verifier(MASTER_KEY),
            client=client,
            lkg_root=tmp_path,
            now=lambda: clock[0],
        )
        restarted.register_workspace(
            workspace_id=WORKSPACE_ID,
            tenant_id=TENANT_ID,
            cell_code="cn-north-1-a",
        )
        assert restarted.decision(WORKSPACE_ID).status == "lkg"
        outage = await restarted.refresh(WORKSPACE_ID)
        assert outage.status == "unreachable"
        clock[0] += timedelta(seconds=2.1)
        assert restarted.decision(WORKSPACE_ID).status == "revocation_stale"
        assert restarted.decision(WORKSPACE_ID, security_sensitive=False).status == "lkg"
        clock[0] += timedelta(seconds=28)
        assert restarted.decision(WORKSPACE_ID, security_sensitive=False).status == "expired"


@pytest.mark.anyio
async def test_corrupt_signature_fails_closed_and_discards_lkg(tmp_path) -> None:
    wire = _wire()
    wire["signature"] = "A" + str(wire["signature"])[1:]
    body = canonical_json_bytes(wire)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={
                "Content-Type": "application/json",
                "ETag": f'"{wire["manifest_sha256"]}"',
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sync = GatewayConfigSynchronizer(
            _config(),
            gateway_identity=GATEWAY_IDENTITY,
            verifier=_test_kms_verifier(MASTER_KEY),
            client=client,
            lkg_root=tmp_path,
        )
        sync.register_workspace(
            workspace_id=WORKSPACE_ID,
            tenant_id=TENANT_ID,
            cell_code="cn-north-1-a",
        )
        with pytest.raises(ConfigDistributionError) as failed:
            await sync.refresh(WORKSPACE_ID)
        assert failed.value.code == "config.signature_invalid"
        assert sync.decision(WORKSPACE_ID).allowed is False
    assert list(tmp_path.iterdir()) == []


def test_revocation_and_source_hash_contradictions_fail_closed() -> None:
    owner = ConfigSnapshotOwner(
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        cell_code="cn-north-1-a",
        verifier=_test_kms_verifier(MASTER_KEY),
    )
    assert owner.apply(_wire(generation=1, revocation_watermark=1)) is True
    with pytest.raises(ConfigDistributionError) as rollback:
        owner.apply(_wire(generation=2, revocation_watermark=0))
    assert rollback.value.code == "config.revocation_stale"
    assert owner.decision().allowed is False

    invalid_source_owner = ConfigSnapshotOwner(
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        cell_code="cn-north-1-a",
        verifier=_test_kms_verifier(MASTER_KEY),
    )
    with pytest.raises(ConfigDistributionError) as invalid_source:
        invalid_source_owner.apply(_wire(source_sha256="f" * 64))
    assert invalid_source.value.code == "config.hash_invalid"
    assert invalid_source_owner.decision().allowed is False
