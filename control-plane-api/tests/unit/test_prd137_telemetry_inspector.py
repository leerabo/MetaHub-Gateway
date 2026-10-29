"""Controlled persisted faults for telemetry windows, not deployment attestations."""

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
from metahub_assurance_runner.resources import (
    LocalResourceInspector,
    ResourceContractViolation,
    ResourceUnavailable,
)
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.db.connection import _close_idle_database_connections
from metahub_shared.observability import TelemetryBuffer

TENANT = "ten_" + "1" * 32
FOREIGN = "ten_" + "2" * 32


@pytest.fixture
def inspector(tmp_path):
    database = tmp_path / "telemetry.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("""
            CREATE TABLE telemetry_ingest_head(
                telemetry_ingest_head_id,source_watermark,dropped_fact_count,revision,updated_at);
            INSERT INTO telemetry_ingest_head VALUES('head',0,0,1,'2026-09-01T00:00:00Z');
            CREATE TABLE sli_time_buckets(
                sli_time_bucket_id,source_watermark,bucket_started_at,service_code,operation_code,
                region_code,status_class,duration_bucket,tenant_pseudonym,good_count,total_count,
                source_sha256,created_at);
        """)
    secret = secrets.token_urlsafe(48)
    buffer = TelemetryBuffer(
        SimpleNamespace(
            database_url=f"sqlite:///{database.as_posix()}",
            platform_session_secret=secret,
        ),
        service_code="control",
    )
    return LocalResourceInspector(database, tmp_path / "file-storage", secret, buffer, secret)


def _emit(inspector):
    buffer = inspector.telemetry_runtime
    for owner, status, repetitions in ((TENANT, 200, 3), (FOREIGN, 200, 1), (None, 401, 2)):
        for _ in range(repetitions):
            assert buffer.enqueue_http(
                operation_code="other",
                status_code=status,
                duration_ms=10,
                request_id="private-request",
                trace_id="private-trace",
                tenant_owner=owner,
            )
    assert inspector.telemetry_checkpoint() is None
    assert buffer.flush_once()


def _observe(inspector, before):
    return inspector.telemetry_window(
        before,
        inspector.telemetry_checkpoint(),
        (
            (TENANT, "2xx", 3),
            (FOREIGN, "2xx", 1),
            (None, "4xx", 2),
        ),
    )


def test_repeated_telemetry_flushes_reuse_one_handle_and_persist_every_batch(
    inspector, monkeypatch,
):
    real_connect = sqlite3.connect
    opened = []

    def counted_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    _close_idle_database_connections()
    monkeypatch.setattr(sqlite3, "connect", counted_connect)
    try:
        buffer = inspector.telemetry_runtime
        for _ in range(20):
            assert buffer.enqueue_http(
                operation_code="other", status_code=200, duration_ms=10,
                request_id="private-request", trace_id="private-trace", tenant_owner=TENANT,
            )
            assert buffer.flush_once()
        assert len(opened) == 1
        assert buffer.snapshot().persisted == 20
        assert buffer.snapshot().backend_failures == 0
        with closing(inspector._read()) as connection:
            assert connection.execute(
                "SELECT SUM(total_count) FROM sli_time_buckets",
            ).fetchone()[0] == 20
            assert connection.execute(
                "SELECT source_watermark FROM telemetry_ingest_head",
            ).fetchone()[0] == 20
    finally:
        _close_idle_database_connections()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_exact_flush_counts_hashes_and_private_observation(inspector):
    before = inspector.telemetry_checkpoint()
    _emit(inspector)
    observation = _observe(inspector, before)
    assert all(satisfied for _, satisfied in observation.checks)
    assert observation.resource_code == "metric_bucket"
    assert all(
        value not in repr(observation) + repr(inspector)
        for value in (
            TENANT,
            FOREIGN,
            inspector.telemetry_secret,
            "private-request",
            "private-trace",
        )
    )
    with closing(inspector._read()) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM sli_time_buckets")


@pytest.mark.parametrize("fault", ["owner", "none", "count", "hash", "operation", "good_count"])
def test_persisted_fault_is_not_reported_as_passed(inspector, fault):
    before = inspector.telemetry_checkpoint()
    _emit(inspector)
    target = hmac.new(
        inspector.telemetry_secret.encode(),
        f"prd109/telemetry-tenant/v1\0{TENANT}".encode(),
        hashlib.sha256,
    ).hexdigest()
    with closing(sqlite3.connect(inspector.database)) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM sli_time_buckets WHERE tenant_pseudonym=?",
                (target,),
            ).fetchone()
        )
        key, value = {
            "owner": ("tenant_pseudonym", "f" * 64),
            "none": ("tenant_pseudonym", "none"),
            "count": ("total_count", row["total_count"] + 1),
            "hash": ("source_sha256", "0" * 64),
            "operation": ("operation_code", "private-label-canary"),
            "good_count": ("good_count", 0),
        }[fault]
        row[key] = value
        # Rehash most faults so an owner/count check cannot pass merely due to a hash check.
        digest = (
            "0" * 64
            if fault == "hash"
            else hashlib.sha256(
                canonical_json_bytes(
                    {
                        key: value
                        for key, value in row.items()
                        if key not in ("sli_time_bucket_id", "source_sha256", "created_at")
                    }
                )
            ).hexdigest()
        )
        connection.execute(
            f"UPDATE sli_time_buckets SET {key}=?,source_sha256=? WHERE tenant_pseudonym=?",
            (value, digest, target),
        )
        connection.commit()
    observation = _observe(inspector, before)
    assert not all(satisfied for _, satisfied in observation.checks)
    if fault in ("owner", "none", "count"):
        assert not dict(observation.checks)["telemetry.exact_owner_counts"]


def test_missing_runtime_degraded_queue_and_invalid_window_fail_closed(inspector, monkeypatch):
    with pytest.raises(ResourceUnavailable):
        replace(inspector, telemetry_runtime=None, telemetry_secret=None).telemetry_checkpoint()
    before = inspector.telemetry_checkpoint()
    with pytest.raises(ResourceContractViolation):
        inspector.telemetry_window(before, before, ((TENANT, "2xx", 1),))

    def backend_failure(*args):
        raise sqlite3.OperationalError("private-diagnostic")

    monkeypatch.setattr(inspector.telemetry_runtime, "_persist_batch", backend_failure)
    assert inspector.telemetry_runtime.enqueue_http(
        operation_code="other",
        status_code=200,
        duration_ms=1,
        request_id="private-request",
        trace_id="private-trace",
        tenant_owner=TENANT,
    )
    assert not inspector.telemetry_runtime.flush_once()
    with pytest.raises(ResourceUnavailable, match="resource.telemetry_degraded"):
        inspector.telemetry_checkpoint()
