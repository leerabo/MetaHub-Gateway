"""PRD-076 缓存结算 failure spool 的最小载荷、HMAC 与恢复状态机测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from metahub_gateway.response_cache.failure_spool import (
    ResponseCacheFailureRecord,
    ResponseCacheFailureSpool,
    ResponseCacheFailureSpoolError,
    _replay_response_cache_failure_batch,
)
from metahub_gateway.response_cache.models import ResponseCacheSettlementIds
from metahub_shared.response_cache import VersionedResponseCacheSecret


def _ids(character: str = "a") -> ResponseCacheSettlementIds:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _ids 对应步骤。"""
    return ResponseCacheSettlementIds(
        cache_hit_id="rch_" + character * 32,
        usage_ledger_id="usg_" + character * 32,
        finops_ledger_entry_id="fle_" + character * 32,
        finops_event_id="flevt_" + character * 32,
        reference_price_snapshot_id="prs_" + character * 32,
        billed_price_snapshot_id="prs_" + character * 32,
        response_cache_event_id="rcev_" + character * 32,
    )


def _record(character: str = "a") -> ResponseCacheFailureRecord:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _record 对应步骤。"""
    return ResponseCacheFailureRecord(
        tenant_id="tenant_prd076",
        request_id=f"req_prd076_{character}",
        trace_id=f"trace_prd076_{character}",
        cache_policy_id="rcp_" + character * 32,
        cache_policy_version=3,
        ids=_ids(character),
    )


def _config(root: Path, *keys: VersionedResponseCacheSecret) -> SimpleNamespace:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _config 对应步骤。"""
    return SimpleNamespace(
        response_cache_failure_spool_root=str(root),
        response_cache_failure_spool_keyring=tuple(keys),
        response_cache_failure_spool_claim_lease_seconds=60,
        response_cache_failure_spool_poll_interval_seconds=1,
    )


def test_failure_spool_payload_is_minimal_idempotent_and_tamper_evident(
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_failure_spool_payload_is_minimal_idempotent_and_tamper_evident 对应步骤。"""
    key = VersionedResponseCacheSecret(7, "prd076-failure-spool-key-material-000000000000")
    spool = ResponseCacheFailureSpool(_config(tmp_path / "spool", key))
    record = _record()

    assert spool.store(record) is True
    assert spool.store(record) is False
    path = next(spool.pending.glob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert set(envelope) == {
        "schema_version",
        "hmac_key_version",
        "payload",
        "hmac_sha256",
    }
    assert set(envelope["payload"]) == {
        "tenant_id",
        "request_id",
        "trace_id",
        "cache_policy_id",
        "cache_policy_version",
        "settlement_ids",
        "error_code",
    }
    serialized = json.dumps(envelope["payload"], sort_keys=True).lower()
    for forbidden in (
        "prompt",
        "ciphertext",
        "cache_key_digest",
        "input_tokens",
        "output_tokens",
        "cost_cny",
        "provider_key",
    ):
        assert forbidden not in serialized
    assert "tenant_prd076" not in repr(record)
    assert record.ids.cache_hit_id not in repr(record)

    envelope["payload"]["request_id"] = "req_prd076_tampered"
    path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    claims, quarantined = spool.claim_batch()
    assert claims == []
    assert quarantined == 1
    assert not list(spool.pending.glob("*.json"))
    assert len(list(spool.quarantine.glob("*.bad"))) == 1


def test_failure_spool_retained_key_can_verify_records_after_rotation(tmp_path: Path) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_failure_spool_retained_key_can_verify_records_after_rotation 对应步骤。"""
    old = VersionedResponseCacheSecret(2, "prd076-old-failure-spool-key-material-000000000")
    active = VersionedResponseCacheSecret(3, "prd076-new-failure-spool-key-material-000000000")
    root = tmp_path / "rotation"
    writer = ResponseCacheFailureSpool(_config(root, old))
    assert writer.store(_record("b")) is True

    reader = ResponseCacheFailureSpool(_config(root, active, old))
    claims, quarantined = reader.claim_batch()
    assert quarantined == 0
    assert len(claims) == 1
    assert claims[0].record == _record("b")
    reader.release(claims[0])
    assert len(list(reader.pending.glob("*.json"))) == 1


def test_failure_spool_does_not_hide_destination_path_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_failure_spool_does_not_hide_destination_path_failure 对应步骤。"""
    key = VersionedResponseCacheSecret(4, "prd076-claim-failure-spool-key-material-000000000")
    spool = ResponseCacheFailureSpool(_config(tmp_path / "claim-failure", key))
    assert spool.store(_record("d")) is True
    original_replace = os.replace

    def fail_claim(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 fail_claim 对应步骤。"""
        if Path(source).parent == spool.pending and Path(destination).parent == spool.processing:
            # 该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。
            raise FileNotFoundError(3, "synthetic_destination_failure", str(destination))
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_claim)
    with pytest.raises(
        ResponseCacheFailureSpoolError,
        match="response_cache_failure_spool_claim_failed",
    ):
        spool.claim_batch()
    assert len(list(spool.pending.glob("*.json"))) == 1
    assert not list(spool.processing.glob("*.claim"))


@pytest.mark.parametrize(
    ("read_status", "recovery_status", "expected_field", "expected_value"),
    (
        ("complete", None, "completed", 1),
        ("absent", "recovered", "recovered_failed", 1),
        ("partial", None, "quarantined", 1),
        ("unreadable", None, "deferred", 1),
    ),
)
def test_failure_spool_recovery_uses_exact_four_state_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_status: str,
    recovery_status: str | None,
    expected_field: str,
    expected_value: int,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_failure_spool_recovery_uses_exact_four_state_readback 对应步骤。"""
    from metahub_gateway.response_cache import settlement as settlement_module

    key = VersionedResponseCacheSecret(1, "prd076-replay-failure-spool-key-material-000000")
    root = tmp_path / read_status
    config = _config(root, key)
    spool = ResponseCacheFailureSpool(config)
    assert spool.store(_record("c")) is True
    monkeypatch.setattr(
        settlement_module,
        "read_response_cache_settlement",
        lambda *_args, **_kwargs: SimpleNamespace(status=read_status),
    )

    def recover(*_args, **_kwargs):
        """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 recover 对应步骤。"""
        assert recovery_status is not None
        return SimpleNamespace(status=recovery_status)

    monkeypatch.setattr(
        settlement_module,
        "recover_absent_response_cache_settlement",
        recover,
    )
    summary = _replay_response_cache_failure_batch(config)
    assert summary.claimed == 1
    assert getattr(summary, expected_field) == expected_value
    if read_status == "partial":
        assert len(list(spool.quarantine.glob("*.bad"))) == 1
    elif read_status == "unreadable":
        assert len(list(spool.pending.glob("*.json"))) == 1
    else:
        assert not list(spool.pending.glob("*.json"))
        assert not list(spool.processing.glob("*.claim"))
