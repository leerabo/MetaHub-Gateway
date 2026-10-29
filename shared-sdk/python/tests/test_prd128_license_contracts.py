"""PRD-128 signed entitlement, grace, clock, and capacity contract tests."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.private_gateway import (
    EdgeLicenseStore,
    LicenseCapacity,
    LicenseContractError,
    LicenseEntitlement,
    sign_license_entitlement,
    verify_license_entitlement,
)
from metahub_shared.runtime.errors import ERROR_SPECS

WORKSPACE_ID = "wsp_" + "1" * 32
TENANT_ID = "11111111-1111-4111-8111-111111111111"
DEPLOYMENT_ID = "edp_" + "2" * 32
LICENSE_ID = "lic_" + "3" * 32
KEY_ID = "edge-license-test-v1"
NOW = datetime(2026, 8, 28, 4, 0, tzinfo=UTC)


def test_license_error_catalog_has_fixed_statuses_and_safe_owner() -> None:
    expected = {
        "edge_license.request_invalid": 400,
        "edge_license.not_found": 404,
        "edge_license.idempotency_conflict": 409,
        "edge_license.revision_conflict": 412,
        "edge_license.state_conflict": 409,
        "edge_license.deployment_ineligible": 409,
        "edge_license.commercial_ineligible": 409,
        "edge_license.device_ineligible": 409,
        "edge_license.revoked": 410,
        "edge_license.expiry_invalid": 422,
        "edge_license.ack_invalid": 422,
        "edge_license.ack_conflict": 409,
        "edge_license.integrity_failed": 503,
        "edge_license.unavailable": 503,
    }

    assert {
        code: ERROR_SPECS[code].http_status for code in expected
    } == expected
    assert {ERROR_SPECS[code].owner for code in expected} == {"edge_management"}
    assert ERROR_SPECS["edge_license.unavailable"].retryable is True
    assert ERROR_SPECS["edge_license.integrity_failed"].retryable is False


def _signed(
    private_key: Ed25519PrivateKey,
    *,
    generation: int = 1,
    refresh_counter: int | None = None,
    status: str = "active",
    deployment_id: str = DEPLOYMENT_ID,
    node_limit: int = 2,
    concurrency: int = 3,
    expires_at: datetime | None = None,
):
    expires = expires_at or NOW + timedelta(days=30)
    entitlement = LicenseEntitlement(
        entitlement_id="let_" + f"{generation:032x}",
        license_id=LICENSE_ID,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=deployment_id,
        device_generation=1,
        commercial_source_type="entitlement_snapshot",
        commercial_source_id="ent_" + "4" * 32,
        commercial_source_revision=7,
        generation=generation,
        refresh_counter=refresh_counter or generation,
        revocation_generation=1 if status == "revoked" else 0,
        status=status,
        capacity=LicenseCapacity(
            node_limit=node_limit,
            concurrent_request_limit=concurrency,
            features=["advanced_routing", "policy_enforcement"],
        ),
        issued_at=NOW,
        not_before=NOW,
        expires_at=expires,
        grace_ends_at=expires + timedelta(days=7),
    )
    return sign_license_entitlement(entitlement, private_key=private_key, key_id=KEY_ID)


def _store(tmp_path, private_key: Ed25519PrivateKey) -> EdgeLicenseStore:
    return EdgeLicenseStore(
        tmp_path,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        device_generation=1,
        trusted_keys={KEY_ID: private_key.public_key().public_bytes_raw()},
    )


def test_license_signature_is_exact_owner_bound_and_secret_safe(tmp_path) -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = _signed(private_key)
    verified = verify_license_entitlement(
        signed.model_dump(mode="json"),
        trusted_keys={KEY_ID: private_key.public_key().public_bytes_raw()},
    )
    assert verified.signed.entitlement.deployment_id == DEPLOYMENT_ID

    copied = _signed(private_key, deployment_id="edp_" + "9" * 32)
    store = _store(tmp_path, private_key)
    with pytest.raises(LicenseContractError, match="edge_license.owner_mismatch"):
        store.apply(copied.model_dump(mode="json"), now=NOW)

    forbidden = signed.model_dump(mode="json")
    forbidden["entitlement"]["provider_token"] = "canary-not-a-credential"
    with pytest.raises(LicenseContractError, match="sensitive_field_denied"):
        verify_license_entitlement(
            forbidden,
            trusted_keys={KEY_ID: private_key.public_key().public_bytes_raw()},
        )


def test_generation_conflict_and_stale_refresh_preserve_lkg(tmp_path) -> None:
    private_key = Ed25519PrivateKey.generate()
    store = _store(tmp_path, private_key)
    first = _signed(private_key)
    assert store.apply(first.model_dump(mode="json"), now=NOW)
    assert not store.apply(first.model_dump(mode="json"), now=NOW)

    conflict = _signed(private_key, generation=1, node_limit=9)
    with pytest.raises(LicenseContractError, match="generation_conflict"):
        store.apply(conflict.model_dump(mode="json"), now=NOW)
    assert store.snapshot().signed.manifest_sha256 == first.manifest_sha256

    with pytest.raises((LicenseContractError, ValueError)):
        _signed(private_key, generation=2, refresh_counter=1)


def test_expiry_enters_grace_then_restricted_without_stopping_existing_lease(tmp_path) -> None:
    private_key = Ed25519PrivateKey.generate()
    store = _store(tmp_path, private_key)
    expires = NOW + timedelta(hours=1)
    store.apply(_signed(private_key, expires_at=expires).model_dump(mode="json"), now=NOW)
    lease = store.acquire(
        "lsl_" + "1" * 32,
        dimension="concurrent_request",
        ttl=timedelta(hours=2),
        now=NOW,
    )
    grace = store.decision(now=expires + timedelta(minutes=1))
    assert grace.status == "grace"
    assert grace.existing_work_allowed and not grace.new_capacity_allowed
    assert store.active_units("concurrent_request", now=expires + timedelta(minutes=1)) == 1
    with pytest.raises(LicenseContractError, match="capacity_restricted"):
        store.acquire(
            "lsl_" + "2" * 32,
            dimension="concurrent_request",
            now=expires + timedelta(minutes=1),
        )
    assert lease.expires_at == NOW + timedelta(hours=2)
    assert store.decision(now=expires + timedelta(days=7)).status == "restricted"


def test_explicit_revocation_has_no_grace_and_clears_leases(tmp_path) -> None:
    private_key = Ed25519PrivateKey.generate()
    store = _store(tmp_path, private_key)
    store.apply(_signed(private_key).model_dump(mode="json"), now=NOW)
    store.acquire("lsl_" + "3" * 32, dimension="node", now=NOW)
    revoked = _signed(private_key, generation=2, status="revoked")
    assert store.apply(revoked.model_dump(mode="json"), now=NOW + timedelta(minutes=1))
    decision = store.decision(now=NOW + timedelta(minutes=1))
    assert decision.status == "revoked"
    assert not decision.existing_work_allowed
    assert store.active_units("node", now=NOW + timedelta(minutes=1)) == 0


def test_capacity_leases_are_atomic_and_expansion_keeps_existing_usage(tmp_path) -> None:
    private_key = Ed25519PrivateKey.generate()
    store = _store(tmp_path, private_key)
    store.apply(_signed(private_key, concurrency=3).model_dump(mode="json"), now=NOW)
    outcomes: list[str] = []

    def acquire(index: int) -> None:
        try:
            store.acquire(
                "lsl_" + f"{index + 10:032x}",
                dimension="concurrent_request",
                now=NOW,
            )
            outcomes.append("acquired")
        except LicenseContractError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=acquire, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("acquired") == 3
    assert outcomes.count("edge_license.capacity_exceeded") == 5

    expanded = _signed(private_key, generation=2, concurrency=5)
    assert store.apply(expanded.model_dump(mode="json"), now=NOW + timedelta(minutes=1))
    store.acquire(
        "lsl_" + "e" * 32,
        dimension="concurrent_request",
        units=2,
        now=NOW + timedelta(minutes=1),
    )
    assert store.active_units("concurrent_request", now=NOW + timedelta(minutes=1)) == 5


def test_clock_rollback_does_not_extend_entitlement(tmp_path) -> None:
    private_key = Ed25519PrivateKey.generate()
    store = _store(tmp_path, private_key)
    store.apply(_signed(private_key).model_dump(mode="json"), now=NOW)
    store.checkpoint_trusted_time(NOW + timedelta(hours=2))
    decision = store.decision(now=NOW)
    assert decision.status == "clock_rollback"
    assert decision.existing_work_allowed and not decision.new_capacity_allowed


def test_hot_path_p95_stays_below_two_milliseconds(tmp_path) -> None:
    private_key = Ed25519PrivateKey.generate()
    store = _store(tmp_path, private_key)
    store.apply(_signed(private_key).model_dump(mode="json"), now=NOW)
    durations: list[int] = []
    for _ in range(10_000):
        started = time.perf_counter_ns()
        assert store.decision(now=NOW).status == "current"
        durations.append(time.perf_counter_ns() - started)
    durations.sort()
    p95_ns = durations[int(len(durations) * 0.95) - 1]
    assert p95_ns < 2_000_000


def test_disk_head_is_canonical_and_reloads_without_private_material(tmp_path) -> None:
    private_key = Ed25519PrivateKey.generate()
    store = _store(tmp_path, private_key)
    signed = _signed(private_key)
    store.apply(signed.model_dump(mode="json"), now=NOW)
    document = json.loads((tmp_path / "head.json").read_text(encoding="utf-8"))
    assert "private_key" not in json.dumps(document)
    restored = _store(tmp_path, private_key)
    assert restored.snapshot().signed.manifest_sha256 == signed.manifest_sha256
