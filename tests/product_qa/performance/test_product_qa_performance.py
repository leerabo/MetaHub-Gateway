"""Product QA PERF/COMP smoke tests using only isolated state by default."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.app import app as control_app
from metahub_gateway.app import app as gateway_app
from metahub_shared.db import apply_migrations, connect_database
from metahub_shared.db.connection import (
    SQLITE_BUSY_TIMEOUT_MILLISECONDS,
    database_request_scope,
)

from scripts.quality.product_qa_performance import (
    Sample,
    Target,
    _classify_http_error,
    _safe_target,
    summarize,
)


async def _asgi_health_samples(app, *, target: str, count: int = 12) -> list[Sample]:
    """Exercise concurrent isolated ASGI health calls without a developer DB."""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://isolated") as client:
        started = __import__("time").perf_counter()
        responses = await asyncio.gather(
            *[
                client.get("/health", headers={"X-Request-ID": f"qa_perf_{target}_{index}"})
                for index in range(count)
            ]
        )
        elapsed_ms = (__import__("time").perf_counter() - started) * 1000 / count
    return [Sample(target, elapsed_ms, response.status_code, None) for response in responses]


@pytest.mark.anyio
async def test_perf_isolated_asgi_health_smoke_preserves_success_and_request_isolation() -> None:
    """PERF-01: isolated Control/Gateway health fan-out remains uniformly successful."""

    samples = await _asgi_health_samples(control_app, target="control")
    samples.extend(await _asgi_health_samples(gateway_app, target="gateway"))
    report = summarize(samples, wall_seconds=0.1)

    assert report["errors"] == 0
    assert report["requests"] == 24
    assert {item["target"] for item in report["targets"]} == {"control", "gateway"}
    assert all(item["latency_ms"]["p95"] is not None for item in report["targets"])


def test_perf_empty_sqlite_migration_and_request_scope_health(tmp_path: Path) -> None:
    """PERF-09: a disposable database migrates and request handles close cleanly."""

    database_url = f"sqlite:///{(tmp_path / 'perf-compat.db').as_posix()}"
    migration = apply_migrations(database_url)
    with database_request_scope():
        connection = connect_database(database_url)
        applied_versions = connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        assert migration.applied
        assert len(applied_versions) == len(migration.applied)
        assert integrity == "ok"
        assert foreign_key_violations == []
        connection.execute("PRAGMA query_only=ON")
        connection.close()

    with database_request_scope():
        next_request = connect_database(database_url)
        assert next_request.execute("PRAGMA query_only").fetchone()[0] == 0
        assert next_request.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (
            next_request.execute("PRAGMA busy_timeout").fetchone()[0]
            == SQLITE_BUSY_TIMEOUT_MILLISECONDS
        )
        next_request.execute("CREATE TABLE perf_request_probe (id INTEGER PRIMARY KEY)")
        next_request.commit()


def test_perf_report_is_aggregate_and_does_not_retain_response_bodies() -> None:
    """Evidence may contain timings/statuses, never an HTTP response or its body."""

    report = summarize(
        [Sample("control", 1.25, 200, None), Sample("control", 2.5, None, "timeout")],
        wall_seconds=1.0,
    )
    serialized = repr(report)
    assert report["errors"] == 1
    assert "response" not in serialized.lower()
    assert report["targets"][0]["status_counts"] == {"200": 1}


def test_perf_diagnostics_keep_only_safe_error_classes_codes_and_time_buckets() -> None:
    system_error = OSError("message must not be retained")
    system_error.winerror = 10061  # type: ignore[attr-defined]
    http_error = httpx.ConnectError("url and message must not be retained")
    http_error.__cause__ = system_error

    error_kind, system_error_code = _classify_http_error(http_error)
    report = summarize(
        [
            Sample("control", 2.0, 200, None, 1.0),
            Sample("control", 4.0, None, error_kind, 12.0, system_error_code),
            Sample("control", 3.0, 503, None, 15.0),
        ],
        wall_seconds=20.0,
        observation_bucket_seconds=10.0,
    )

    assert report["errors"] == 2
    assert report["error_counts"] == {"connect_error": 1, "http_status_503": 1}
    assert report["system_error_counts"] == {"winerror_10061": 1}
    assert report["time_buckets"][1]["errors"] == 2
    serialized = repr(report)
    assert "message must not be retained" not in serialized
    assert "url and message must not be retained" not in serialized


def test_perf_runner_rejects_non_loopback_or_credential_targets() -> None:
    """The real-service runner cannot be redirected at an external environment."""

    assert _safe_target("control=http://127.0.0.1:8001/health") == Target(
        "control", "http://127.0.0.1:8001/health"
    )
    for raw in (
        "prod=https://example.test/health",
        "external=http://10.0.0.4/health",
        "secret=http://user:password@127.0.0.1:8001/health",
        "query=http://127.0.0.1:8001/health?token=not-allowed",
    ):
        with pytest.raises(ValueError):
            _safe_target(raw)


def test_perf_real_loopback_http_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """PERF-01/COMP-06 exercises a real isolated loopback HTTP socket without credentials."""

    from scripts.quality import product_qa_performance

    real_client = product_qa_performance.httpx.Client
    trust_env_values: list[object] = []

    def direct_client(*args: object, **kwargs: object) -> httpx.Client:
        trust_env_values.append(kwargs.get("trust_env"))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(product_qa_performance.httpx, "Client", direct_client)

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        report = product_qa_performance.run_loopback_smoke(
            [_safe_target(f"isolated=http://127.0.0.1:{port}/health")],
            requests=8,
            concurrency=2,
            duration_seconds=0,
            timeout_seconds=3,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert report["errors"] == 0
    assert report["requests"] == 8
    assert trust_env_values and all(value is False for value in trust_env_values)
