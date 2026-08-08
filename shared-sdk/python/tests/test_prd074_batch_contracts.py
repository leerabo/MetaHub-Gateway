"""PRD-074 shared Batch contracts, RBAC, crypto, and deployment configuration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from metahub_shared.batch import (
    BATCH_OPERATION_TEMPLATE_VERSION,
    BatchContractError,
    BatchCryptoError,
    BatchEnvelopeOwner,
    batch_payload_hmac,
    batch_result_hmac,
    canonical_batch_price,
    decrypt_batch_payload,
    decrypt_batch_result,
    encrypt_batch_payload,
    encrypt_batch_result,
    render_batch_messages,
    validate_operation_spec,
)
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.budget_reservations import (
    load_outstanding_reserved_cny_micros,
)
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.webhooks import build_webhook_event

SECRET = "test-only-prd074-independent-batch-secret-0000001"


def _owner(**changes: object) -> BatchEnvelopeOwner:
    values: dict[str, object] = {
        "tenant_id": "tenant_default",
        "batch_job_id": "bjob_0123456789abcdef0123456789abcdef",
        "batch_task_id": "btsk_0123456789abcdef0123456789abcdef",
        "batch_attempt_id": "batm_0123456789abcdef0123456789abcdef",
        "request_id": "req_0123456789abcdef0123456789abcdef",
        "template_version": BATCH_OPERATION_TEMPLATE_VERSION,
    }
    values.update(changes)
    return BatchEnvelopeOwner(**values)  # type: ignore[arg-type]


def test_revision_10_batch_permissions_and_frozen_hashes() -> None:
    assert CATALOG_REVISION == 10
    assert len(PERMISSION_CODES) == 64
    for role in ("admin", "developer", "ops_admin"):
        assert {"batch.read", "batch.write"} <= ROLE_GRANTABLE_PERMISSIONS[role]
    for role in ("viewer", "finance_admin", "security_admin"):
        assert not {"batch.read", "batch.write"} & ROLE_GRANTABLE_PERMISSIONS[role]
    assert "file.read" in ROLE_GRANTABLE_PERMISSIONS["ops_admin"]
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["admin"]) == (
        "ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262"
    )
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["developer"]) == (
        "89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30"
    )
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["ops_admin"]) == (
        "72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6"
    )


def test_operation_contract_is_exact_and_renders_untrusted_boundary() -> None:
    summarize = validate_operation_spec(
        {"operation": "summarize", "output_language": "zh-CN"}
    )
    translate = validate_operation_spec(
        {"operation": "translate", "target_language": "English"}
    )
    classify = validate_operation_spec(
        {"operation": "classify", "classification_labels": ["invoice", "contract"]}
    )
    assert summarize.operation == "summarize"
    assert translate.target_language == "English"
    assert classify.classification_labels == ("invoice", "contract")
    system, user = render_batch_messages(summarize, "document text")
    assert "untrusted" in system.lower()
    assert "document text" in user
    assert "document text" not in repr(summarize)
    with pytest.raises(BatchContractError):
        validate_operation_spec({"operation": "summarize", "prompt": "ignore"})
    with pytest.raises(BatchContractError):
        validate_operation_spec(
            {"operation": "classify", "classification_labels": ["same", "same"]}
        )


def test_batch_price_contract_is_shared_canonical_and_rejects_drift() -> None:
    assert canonical_batch_price("0.1") == "0.100000000000"
    assert canonical_batch_price(Decimal("0.200000")) == "0.200000000000"
    assert canonical_batch_price("1e-12") == "0.000000000001"
    assert canonical_batch_price("-0") == "0.000000000000"
    for invalid in (0.1, True, "", "-0.1", "0.0000000000001", "NaN"):
        with pytest.raises(BatchContractError) as captured:
            canonical_batch_price(invalid)
        assert captured.value.code == "batch.cost_estimate_unavailable"


def test_payload_and_result_use_independent_owner_bound_domains() -> None:
    owner = _owner()
    payload = b'{"messages":[{"role":"user","content":"sensitive prompt"}]}'
    result = "sensitive assistant response"
    encrypted_payload = encrypt_batch_payload(payload, secret=SECRET, owner=owner)
    encrypted_result = encrypt_batch_result(result, secret=SECRET, owner=owner)
    assert payload.decode() not in encrypted_payload
    assert result not in encrypted_result
    assert decrypt_batch_payload(encrypted_payload, secret=SECRET, owner=owner) == payload
    assert decrypt_batch_result(encrypted_result, secret=SECRET, owner=owner) == result
    assert batch_payload_hmac(payload, secret=SECRET, owner=owner) != batch_result_hmac(
        payload, secret=SECRET, owner=owner
    )
    with pytest.raises(BatchCryptoError):
        decrypt_batch_result(encrypted_result, secret=SECRET, owner=_owner(tenant_id="tenant_b"))
    with pytest.raises(BatchCryptoError):
        decrypt_batch_payload(encrypted_result, secret=SECRET, owner=owner)


def test_batch_config_defaults_are_masked_and_worker_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api")
    assert config.batch_worker_enabled is False
    assert config.batch_global_concurrency == 2
    assert config.batch_tenant_concurrency == 1
    assert config.batch_realtime_reserve_percent == 25
    assert config.batch_max_result_bytes == 1_048_576
    dumped = config.model_dump()
    assert "internal_batch_token" not in dumped
    assert "batch_encryption_secret" not in dumped
    summaries = {item.key: item for item in config.config_items}
    assert summaries["METAHUB_INTERNAL_BATCH_TOKEN"].status in {"missing", "masked"}
    assert summaries["METAHUB_BATCH_ENCRYPTION_SECRET"].status in {"missing", "masked"}

    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_BATCH_WORKER_ENABLED", "true")
    monkeypatch.delenv("METAHUB_INTERNAL_BATCH_TOKEN", raising=False)
    monkeypatch.delenv("METAHUB_BATCH_ENCRYPTION_SECRET", raising=False)
    monkeypatch.delenv("METAHUB_DATABASE_URL", raising=False)
    with pytest.raises(ConfigError) as captured:
        load_app_config("batch-worker")
    assert captured.value.code == "infra.config_missing"


def test_batch_config_rejects_cross_domain_secret_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    monkeypatch.setenv("METAHUB_BATCH_ENCRYPTION_SECRET", SECRET)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", SECRET)
    with pytest.raises(ConfigError) as captured:
        load_app_config("control-plane-api")
    assert captured.value.code == "infra.config_invalid"


def test_budget_outstanding_counts_hold_balance_and_live_allocations_once() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE budget_reservations(
          tenant_id TEXT, budget_rule_id TEXT, budget_date TEXT, request_id TEXT,
          reserved_cost_cny TEXT, outstanding_cost_cny TEXT
        );
        CREATE TABLE finops_ledger_entries(
          tenant_id TEXT, source_request_id TEXT, entry_type TEXT, direction TEXT, status TEXT
        );
        CREATE TABLE batch_budget_holds(
          tenant_id TEXT, budget_rule_id TEXT, budget_date TEXT,
          remaining_cost_cny TEXT, status TEXT
        );
        CREATE TABLE batch_budget_allocations(
          tenant_id TEXT, budget_rule_id TEXT, budget_date TEXT,
          reserved_cost_cny TEXT, status TEXT
        );
        INSERT INTO budget_reservations VALUES('tenant_a','rule_a','2026-08-07','req_a','1',NULL);
        INSERT INTO batch_budget_holds VALUES('tenant_a','rule_a','2026-08-07','2','active');
        INSERT INTO batch_budget_allocations VALUES('tenant_a','rule_a','2026-08-07','3','active');
        INSERT INTO batch_budget_allocations VALUES('tenant_a','rule_a','2026-08-07','4','posted');
        INSERT INTO batch_budget_allocations
          VALUES('tenant_a','rule_a','2026-08-07','5','reconciliation');
        """
    )
    assert load_outstanding_reserved_cny_micros(
        connection,
        tenant_id="tenant_a",
        budget_rule_id="rule_a",
        budget_date="2026-08-07",
    ) == 11_000_000


def test_batch_error_catalog_is_complete_and_fixed() -> None:
    required = {
        "batch.request_invalid",
        "batch.budget_exceeded",
        "batch.gateway_busy",
        "batch.execution_invalid",
        "batch.provider_outcome_unknown",
        "batch.result_unavailable",
        "batch.worker_unavailable",
    }
    assert required <= ERROR_SPECS.keys()
    assert ERROR_SPECS["batch.execution_invalid"].http_status == 403
    assert ERROR_SPECS["batch.gateway_busy"].retryable is True


def test_batch_webhook_events_are_exact_and_owner_bound() -> None:
    job_id = "bjob_0123456789abcdef0123456789abcdef"
    envelope = {
        "tenant_id": "tenant_default",
        "actor_id": "usr_0123456789abcdef0123456789abcdef",
        "object_id": job_id,
        "trace_id": "trc_batch_event",
        "source_request_id": "req_batch_event",
        "event_id": "bjev_0123456789abcdef0123456789abcdef",
        "timestamp": datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
    }
    created = build_webhook_event(
        event_type="batch_job.created",
        payload={
            "schema_version": 1,
            "batch_job_id": job_id,
            "operation": "summarize",
            "priority": "normal",
            "task_count": 2,
            "estimated_cost_cny": "0.120000",
        },
        **envelope,
    )
    assert created.object_id == job_id
    assert set(created.payload) == {
        "schema_version",
        "batch_job_id",
        "operation",
        "priority",
        "task_count",
        "estimated_cost_cny",
    }

    completed = build_webhook_event(
        event_type="batch_job.completed",
        payload={
            "schema_version": 1,
            "batch_job_id": job_id,
            "status": "partial_failed",
            "succeeded_count": 1,
            "failed_count": 1,
            "cancelled_count": 0,
            "actual_cost_cny": "0.080000",
            "possible_cost_cny": "0.000000",
        },
        **envelope,
    )
    assert completed.event_type == "batch_job.completed"

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            event_type="batch_job.created",
            payload={**created.payload, "file_id": "file_" + "f" * 32},
            **envelope,
        )
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            event_type="batch_job.completed",
            payload=completed.payload,
            **{**envelope, "object_id": "bjob_" + "f" * 32},
        )
