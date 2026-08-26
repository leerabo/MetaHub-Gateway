from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import pytest
from metahub_gateway.telemetry import (
    EdgeTelemetrySpool,
    EdgeTelemetrySpoolError,
    EdgeTelemetryUploader,
)
from metahub_shared.private_gateway import (
    EdgeTelemetryBatch,
    EdgeTelemetryError,
    EdgeTelemetryReceiptWire,
    canonical_telemetry_bytes,
    reject_forbidden_telemetry_fields,
    telemetry_batch_sha256,
    validate_telemetry_batch,
)
from pydantic import ValidationError

DEPLOYMENT_ID = "edp_" + "1" * 32
NOW = datetime(2026, 8, 27, 1, 2, 3, tzinfo=UTC)


def _health_record() -> dict[str, object]:
    return {
        "kind": "health",
        "bucket_start": "2026-08-27T01:01:00.000Z",
        "bucket_end": "2026-08-27T01:02:00.000Z",
        "component_code": "gateway",
        "heartbeat_count": 1,
        "uptime_seconds": 60,
        "cpu_millis": 50,
        "memory_peak_bytes": 4096,
    }


def _usage_record() -> dict[str, object]:
    return {
        "kind": "usage",
        "bucket_start": "2026-08-27T01:01:00.000Z",
        "bucket_end": "2026-08-27T01:02:00.000Z",
        "model_id": "mdl_private_001",
        "provider_id": "prv_private_001",
        "request_count": 2,
        "input_tokens": 20,
        "output_tokens": 10,
        "cache_tokens": 0,
        "cost_cny_micros": 1200,
        "cost_usd_micros": 0,
    }


def _batch(stream: str, record: dict[str, object]) -> EdgeTelemetryBatch:
    return EdgeTelemetryBatch.model_validate(
        {
            "schema_version": "1",
            "deployment_id": DEPLOYMENT_ID,
            "stream": stream,
            "epoch_id": "ete_" + "2" * 32,
            "previous_epoch_id": None,
            "sequence": 1,
            "policy_revision": 1,
            "sent_at": "2026-08-27T01:02:03.000Z",
            "records": [record],
        }
    )


def test_exact_contract_rejects_open_fields_and_policy_escalation() -> None:
    health = _batch("health", _health_record())
    validate_telemetry_batch(health, "minimal")
    encoded = canonical_telemetry_bytes(health.model_dump(mode="json"))
    assert telemetry_batch_sha256(health) == hashlib.sha256(encoded).hexdigest()
    assert b"prompt" not in encoded.lower()

    invalid = health.model_dump(mode="json")
    invalid["records"][0]["message"] = "must never reach cloud"
    with pytest.raises(EdgeTelemetryError, match="edge_telemetry.forbidden_field"):
        reject_forbidden_telemetry_fields(invalid)
    with pytest.raises(ValidationError):
        EdgeTelemetryBatch.model_validate(invalid)

    usage = _batch("usage", _usage_record())
    with pytest.raises(EdgeTelemetryError, match="edge_telemetry.policy_violation"):
        validate_telemetry_batch(usage, "operational")
    validate_telemetry_batch(usage, "billing")

    mixed = usage.model_dump(mode="json")
    mixed["stream"] = "health"
    with pytest.raises(ValidationError):
        EdgeTelemetryBatch.model_validate(mixed)
    with pytest.raises(ValidationError, match="receipt_invalid"):
        EdgeTelemetryReceiptWire.model_validate(
            {
                "receipt_id": "etr_" + "3" * 32,
                "batch_id": "etb_" + "4" * 32,
                "deployment_id": DEPLOYMENT_ID,
                "stream": "health",
                "epoch_id": "ete_" + "2" * 32,
                "sequence": 2,
                "disposition": "accepted",
                "contiguous_sequence": 1,
                "expected_sequence": 3,
                "highest_seen_sequence": 1,
                "policy_level": "minimal",
                "policy_revision": 1,
                "received_at": "2026-08-27T01:02:03.000Z",
                "request_id": "req_prd122_invalid_receipt",
                "trace_id": "trc_prd122_invalid_receipt",
            }
        )


def test_spool_is_monotonic_append_only_and_epoch_rotation_is_fenced(tmp_path) -> None:
    spool = EdgeTelemetrySpool(
        tmp_path / "edge-telemetry.sqlite3", deployment_id=DEPLOYMENT_ID, now=lambda: NOW
    )
    first = spool.append("health", [_health_record()], policy_level="minimal", policy_revision=1)
    assert first.sequence == 1
    assert spool.backlog().pending_batches == 1
    with pytest.raises(EdgeTelemetrySpoolError, match="edge_telemetry.policy_conflict"):
        spool.append(
            "health", [_health_record()], policy_level="operational", policy_revision=1
        )
    with pytest.raises(EdgeTelemetrySpoolError, match="edge_telemetry.epoch_pending"):
        spool.rotate_epoch("health", policy_level="minimal", policy_revision=1)

    local_batch_id, pending = spool.pending()[0]
    receipt = EdgeTelemetryReceiptWire.model_validate(
        {
            "receipt_id": "etr_" + "3" * 32,
            "batch_id": "etb_" + "4" * 32,
            "deployment_id": DEPLOYMENT_ID,
            "stream": "health",
            "epoch_id": pending.epoch_id,
            "sequence": 1,
            "disposition": "accepted",
            "contiguous_sequence": 1,
            "expected_sequence": 2,
            "highest_seen_sequence": 1,
            "policy_level": "minimal",
            "policy_revision": 1,
            "received_at": "2026-08-27T01:02:03.000Z",
            "request_id": "req_prd122_receipt",
            "trace_id": "trc_prd122_receipt",
        }
    )
    mismatched_policy = receipt.model_copy(update={"policy_level": "operational"})
    with pytest.raises(EdgeTelemetrySpoolError, match="edge_telemetry.receipt_invalid"):
        spool.acknowledge(local_batch_id, mismatched_policy)
    spool.acknowledge(local_batch_id, receipt)
    spool.acknowledge(local_batch_id, receipt)
    assert spool.backlog().pending_batches == 0
    with pytest.raises(EdgeTelemetrySpoolError, match="edge_telemetry.epoch_conflict"):
        spool.rotate_epoch(
            "health",
            policy_level="operational",
            policy_revision=2,
            epoch_id=first.epoch_id,
        )
    next_epoch = spool.rotate_epoch("health", policy_level="operational", policy_revision=2)
    second = spool.append(
        "health", [_health_record()], policy_level="operational", policy_revision=2
    )
    assert second.epoch_id == next_epoch
    assert second.previous_epoch_id == first.epoch_id
    assert second.sequence == 1
    with pytest.raises(EdgeTelemetrySpoolError, match="edge_telemetry.policy_conflict"):
        spool.append("health", [_health_record()], policy_level="minimal", policy_revision=1)

    connection = sqlite3.connect(tmp_path / "edge-telemetry.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            connection.execute("DELETE FROM edge_telemetry_spool_batches")
    finally:
        connection.close()


def test_concurrent_append_allocates_one_contiguous_sequence(tmp_path) -> None:
    spool = EdgeTelemetrySpool(
        tmp_path / "concurrent.sqlite3", deployment_id=DEPLOYMENT_ID, now=lambda: NOW
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        batches = list(
            executor.map(
                lambda _index: spool.append(
                    "health", [_health_record()], policy_level="minimal", policy_revision=1
                ),
                range(16),
            )
        )
    assert sorted(batch.sequence for batch in batches) == list(range(1, 17))
    assert spool.backlog().pending_batches == 16


def test_uploader_uses_gzip_edge_session_and_strict_no_store_receipt(tmp_path) -> None:
    spool = EdgeTelemetrySpool(
        tmp_path / "upload.sqlite3", deployment_id=DEPLOYMENT_ID, now=lambda: NOW
    )
    batch = spool.append("health", [_health_record()], policy_level="minimal", policy_revision=1)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/edge/telemetry/batches"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.headers["content-encoding"] == "gzip"
        parsed = json.loads(gzip.decompress(request.content))
        assert parsed["deployment_id"] == DEPLOYMENT_ID
        assert parsed["records"] == [_health_record()]
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store, private"},
            json={
                "receipt_id": "etr_" + "5" * 32,
                "batch_id": "etb_" + "6" * 32,
                "deployment_id": DEPLOYMENT_ID,
                "stream": "health",
                "epoch_id": batch.epoch_id,
                "sequence": batch.sequence,
                "disposition": "accepted",
                "contiguous_sequence": 1,
                "expected_sequence": 2,
                "highest_seen_sequence": 1,
                "policy_level": "minimal",
                "policy_revision": 1,
                "received_at": "2026-08-27T01:02:03.000Z",
                "request_id": "req_prd122_upload",
                "trace_id": "trc_prd122_upload",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EdgeTelemetrySpoolError, match="edge_telemetry.endpoint_invalid"):
            EdgeTelemetryUploader(client, "http://control.example.test")
        uploader = EdgeTelemetryUploader(client, "https://control.example.test")
        assert uploader.sync(spool, edge_session="egs_" + "A" * 43) == 1
    assert spool.backlog().pending_batches == 0


def test_uploader_never_follows_redirects_with_edge_session(tmp_path) -> None:
    spool = EdgeTelemetrySpool(
        tmp_path / "redirect.sqlite3", deployment_id=DEPLOYMENT_ID, now=lambda: NOW
    )
    spool.append("health", [_health_record()], policy_level="minimal", policy_revision=1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://redirect.example.test/capture"})

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        uploader = EdgeTelemetryUploader(client, "https://control.example.test")
        with pytest.raises(
            EdgeTelemetrySpoolError, match="edge_telemetry.transport_unavailable"
        ):
            uploader.sync(spool, edge_session="egs_" + "A" * 43)
    assert len(requests) == 1
    assert requests[0].url.host == "control.example.test"
    assert spool.backlog().pending_batches == 1
