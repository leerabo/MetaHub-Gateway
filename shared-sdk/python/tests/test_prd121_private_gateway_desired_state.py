"""PRD-121 RFC 8785, signature, trust rotation, and persistent LKG tests."""

from __future__ import annotations

import copy
import hashlib
import threading
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.config_distribution import canonical_json_bytes as config_json_bytes
from metahub_shared.private_gateway import (
    EdgeDesiredStateError,
    EdgeDesiredStateStore,
    canonical_edge_json_bytes,
    public_key_b64,
    sign_desired_state,
    sign_trust_manifest,
    strict_json_object,
    verify_desired_state,
    verify_trust_manifest,
)
from metahub_shared.runtime.config import ConfigError, load_app_config

WORKSPACE_ID = "wsp_aba85648d6c3c0119b4a41bddf82762b"
TENANT_ID = "944f3e6d-8b66-584e-b095-7cac890a5f87"
DEPLOYMENT_ID = "edp_11111111111111111111111111111111"
KEY_ONE = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
KEY_TWO = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
KID_ONE = "edge-ds-current-v1"
KID_TWO = "edge-ds-next-v2"
SIGNING_SECRET = "prd121-independent-edge-signing-secret-0123456789abcdef"


def _source_manifest(*, generation: int = 1, revocation: int = 0) -> dict[str, object]:
    safe: dict[str, object] = {
        "schema_version": 1,
        "workspace_id": WORKSPACE_ID,
        "tenant_id": TENANT_ID,
        "generation": generation,
        "revocation_watermark": revocation,
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
    return {
        **safe,
        "source_sha256": hashlib.sha256(config_json_bytes(safe)).hexdigest(),
    }


def _content(*, source_generation: int = 1, revocation: int = 0) -> dict[str, object]:
    return {
        "schema_version": 1,
        "deployment": {
            "deployment_id": DEPLOYMENT_ID,
            "mode": "connected",
            "region_code": "cn-north-1",
            "revision": 1,
        },
        "source_manifest": _source_manifest(
            generation=source_generation,
            revocation=revocation,
        ),
    }


def _trust(
    *,
    generation: int = 1,
    current_key: Ed25519PrivateKey = KEY_ONE,
    current_kid: str = KID_ONE,
    next_key: Ed25519PrivateKey | None = KEY_TWO,
    next_kid: str | None = KID_TWO,
    signer_key: Ed25519PrivateKey = KEY_ONE,
    signer_kid: str = KID_ONE,
    issued_at: str = "2026-08-26T00:00:00Z",
    expires_at: str = "2026-08-27T00:00:00Z",
) -> dict[str, object]:
    next_value = (
        {
            "alg": "Ed25519",
            "kid": next_kid,
            "public_key": public_key_b64(next_key),
        }
        if next_key is not None and next_kid is not None
        else None
    )
    return sign_trust_manifest(
        {
            "domain": "metahub.edge-trust-manifest.v1",
            "protocol_version": 1,
            "generation": generation,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "signer_kid": signer_kid,
            "current": {
                "alg": "Ed25519",
                "kid": current_kid,
                "public_key": public_key_b64(current_key),
            },
            "next": next_value,
            "revoked_kids": [],
        },
        signer_key,
    )


def _snapshot(
    *,
    generation: int = 1,
    base_generation: int = 0,
    key: Ed25519PrivateKey = KEY_ONE,
    kid: str = KID_ONE,
    content: dict[str, object] | None = None,
    issued_at: str = "2026-08-26T00:00:00Z",
    security_expires_at: str = "2026-08-27T00:00:00Z",
    expires_at: str = "2026-09-02T00:00:00Z",
) -> dict[str, object]:
    artifact = content or _content(source_generation=generation)
    return sign_desired_state(
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
            "issued_at": issued_at,
            "not_before": issued_at,
            "security_expires_at": security_expires_at,
            "expires_at": expires_at,
            "alg": "Ed25519",
            "kid": kid,
            "content_sha256": hashlib.sha256(canonical_edge_json_bytes(artifact)).hexdigest(),
            "content": artifact,
        },
        key,
    )


def _public(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def test_rfc8785_vectors_and_strict_json_reject_duplicate_or_non_finite() -> None:
    assert canonical_edge_json_bytes(
        {"numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27]}
    ) == b'{"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27]}'
    assert canonical_edge_json_bytes({"\u20ac": "euro", "a": "latin"}) == (
        '{"a":"latin","\u20ac":"euro"}'.encode()
    )
    with pytest.raises(EdgeDesiredStateError) as duplicate:
        strict_json_object(b'{"a":1,"a":2}', canonical=False)
    assert duplicate.value.code == "edge_desired_state.payload_invalid"
    with pytest.raises(EdgeDesiredStateError):
        strict_json_object(b'{"a":NaN}', canonical=False)
    with pytest.raises(EdgeDesiredStateError):
        canonical_edge_json_bytes({"unsafe": 2**60})


def test_full_envelope_signature_owner_hash_and_time_fail_closed() -> None:
    now = datetime(2026, 8, 26, 1, tzinfo=UTC)
    manifest = _trust()
    trust = verify_trust_manifest(
        manifest,
        trusted_keys={KID_ONE: _public(KEY_ONE)},
        now=now,
    )
    snapshot = _snapshot()
    verified = verify_desired_state(
        snapshot,
        trust=trust,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        now=now,
    )
    assert verified.generation == 1
    assert verified.content["deployment"]["deployment_id"] == DEPLOYMENT_ID

    tampered = copy.deepcopy(snapshot)
    tampered["content"]["deployment"]["revision"] = 2
    with pytest.raises(EdgeDesiredStateError) as invalid_signature:
        verify_desired_state(
            tampered,
            trust=trust,
            workspace_id=WORKSPACE_ID,
            tenant_id=TENANT_ID,
            deployment_id=DEPLOYMENT_ID,
            now=now,
        )
    assert invalid_signature.value.code == "edge_desired_state.hash_invalid"

    with pytest.raises(EdgeDesiredStateError) as wrong_owner:
        verify_desired_state(
            snapshot,
            trust=trust,
            workspace_id=WORKSPACE_ID,
            tenant_id=TENANT_ID,
            deployment_id="edp_" + "2" * 32,
            now=now,
        )
    assert wrong_owner.value.code == "edge_desired_state.owner_invalid"


def test_store_atomic_restart_lkg_ttl_and_generation_fencing(tmp_path) -> None:
    clock = [datetime(2026, 8, 26, 1, tzinfo=UTC)]
    store = EdgeDesiredStateStore(
        tmp_path,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        pinned_keys={KID_ONE: _public(KEY_ONE)},
        now=lambda: clock[0],
    )
    wire = {"trust_manifest": _trust(), "snapshot": _snapshot()}
    assert store.apply(wire) is True
    assert store.decision().status == "current"
    assert store.content()["deployment"]["revision"] == 1
    assert {path.name for path in tmp_path.iterdir()} == {
        "active.json",
        "slots",
        "trust.json",
        "watermark.json",
    }

    restarted = EdgeDesiredStateStore(
        tmp_path,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        pinned_keys={KID_ONE: _public(KEY_ONE)},
        now=lambda: clock[0],
    )
    assert restarted.decision().status == "lkg"
    clock[0] += timedelta(hours=25)
    stale = restarted.decision()
    assert stale.configuration_allowed is True
    assert stale.security_allowed is False
    assert stale.status == "stale"
    clock[0] += timedelta(days=7)
    assert restarted.decision().status == "expired"

    with pytest.raises(EdgeDesiredStateError) as old:
        restarted.apply(wire)
    assert old.value.code == "edge_desired_state.trust_expired"
    assert restarted.decision().status == "integrity_failed"


def test_trust_rotation_chain_survives_restart_and_rejects_conflict(tmp_path) -> None:
    clock = [datetime(2026, 8, 26, 1, tzinfo=UTC)]
    store = EdgeDesiredStateStore(
        tmp_path,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        pinned_keys={KID_ONE: _public(KEY_ONE)},
        now=lambda: clock[0],
    )
    assert store.apply({"trust_manifest": _trust(), "snapshot": _snapshot()})
    transition = _trust(
        generation=2,
        current_key=KEY_TWO,
        current_kid=KID_TWO,
        next_key=None,
        next_kid=None,
        signer_key=KEY_ONE,
        signer_kid=KID_ONE,
    )
    second = _snapshot(
        generation=2,
        base_generation=1,
        key=KEY_TWO,
        kid=KID_TWO,
        content=_content(source_generation=2),
    )
    assert store.apply({"trust_manifest": transition, "snapshot": second})

    restarted = EdgeDesiredStateStore(
        tmp_path,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        pinned_keys={KID_ONE: _public(KEY_ONE)},
        now=lambda: clock[0],
    )
    assert restarted.snapshot().generation == 2
    current_manifest = _trust(
        generation=3,
        current_key=KEY_TWO,
        current_kid=KID_TWO,
        next_key=None,
        next_kid=None,
        signer_key=KEY_TWO,
        signer_kid=KID_TWO,
    )
    third = _snapshot(
        generation=3,
        base_generation=2,
        key=KEY_TWO,
        kid=KID_TWO,
        content=_content(source_generation=3),
    )
    assert restarted.apply({"trust_manifest": current_manifest, "snapshot": third})

    conflict = copy.deepcopy(third)
    conflict["signature"] = ("A" if conflict["signature"][0] != "A" else "B") + conflict[
        "signature"
    ][1:]
    with pytest.raises(EdgeDesiredStateError):
        restarted.apply({"trust_manifest": current_manifest, "snapshot": conflict})
    assert restarted.decision().status == "integrity_failed"


def test_store_rejects_unannounced_or_wrong_signer_trust_transition(tmp_path) -> None:
    clock = [datetime(2026, 8, 26, 1, tzinfo=UTC)]
    store = EdgeDesiredStateStore(
        tmp_path,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        pinned_keys={KID_ONE: _public(KEY_ONE)},
        now=lambda: clock[0],
    )
    assert store.apply({"trust_manifest": _trust(), "snapshot": _snapshot()})
    wrong_signer = _trust(
        generation=2,
        current_key=KEY_TWO,
        current_kid=KID_TWO,
        next_key=None,
        next_kid=None,
        signer_key=KEY_TWO,
        signer_kid=KID_TWO,
    )
    second = _snapshot(
        generation=2,
        base_generation=1,
        key=KEY_TWO,
        kid=KID_TWO,
        content=_content(source_generation=2),
    )
    with pytest.raises(EdgeDesiredStateError) as rejected:
        store.apply({"trust_manifest": wrong_signer, "snapshot": second})
    assert rejected.value.code == "edge_desired_state.trust_invalid"
    assert store.snapshot().generation == 1


def test_store_serializes_concurrent_apply_without_generation_regression(tmp_path) -> None:
    clock = [datetime(2026, 8, 26, 1, tzinfo=UTC)]
    store = EdgeDesiredStateStore(
        tmp_path,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        pinned_keys={KID_ONE: _public(KEY_ONE)},
        now=lambda: clock[0],
    )
    manifest = _trust()
    assert store.apply({"trust_manifest": manifest, "snapshot": _snapshot()})
    second = {
        "trust_manifest": manifest,
        "snapshot": _snapshot(
            generation=2,
            base_generation=1,
            content=_content(source_generation=2),
        ),
    }
    third = {
        "trust_manifest": manifest,
        "snapshot": _snapshot(
            generation=3,
            base_generation=2,
            content=_content(source_generation=3),
        ),
    }
    second_entered = threading.Event()
    release_second = threading.Event()
    release_third = threading.Event()
    original_persist = store._persist

    def delayed_persist(value, trust, snapshot, observed_at) -> None:
        if snapshot.generation == 2:
            second_entered.set()
            assert release_second.wait(timeout=5)
        elif snapshot.generation == 3:
            assert release_third.wait(timeout=5)
        original_persist(value, trust, snapshot, observed_at)

    store._persist = delayed_persist
    failures: list[BaseException] = []

    def apply_wire(wire) -> None:
        try:
            store.apply(wire)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    second_thread = threading.Thread(target=apply_wire, args=(second,))
    third_thread = threading.Thread(target=apply_wire, args=(third,))
    second_thread.start()
    assert second_entered.wait(timeout=5)
    third_thread.start()
    release_third.set()
    release_second.set()
    second_thread.join(timeout=5)
    third_thread.join(timeout=5)
    assert not second_thread.is_alive()
    assert not third_thread.is_alive()
    assert failures == []
    assert store.snapshot().generation == 3

    restarted = EdgeDesiredStateStore(
        tmp_path,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        pinned_keys={KID_ONE: _public(KEY_ONE)},
        now=lambda: clock[0],
    )
    assert restarted.snapshot().generation == 3


def test_edge_signing_config_is_masked_and_rejects_root_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_EDGE_SNAPSHOT_SIGNING_SECRET", SIGNING_SECRET)
    config = load_app_config("control-plane-api")
    assert config.edge_snapshot_signing_secret == SIGNING_SECRET
    assert "edge_snapshot_signing_secret" not in config.model_dump()
    assert SIGNING_SECRET not in repr(config)

    monkeypatch.setenv("METAHUB_TENANT_DATA_ENCRYPTION_SECRET", SIGNING_SECRET)
    with pytest.raises(ConfigError) as reused:
        load_app_config("control-plane-api")
    assert reused.value.code == "infra.config_invalid"
    assert SIGNING_SECRET not in reused.value.message
