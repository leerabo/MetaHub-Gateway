"""Real HTTP/Redis sockets and full schema, without unrelated Gateway lifecycle workers."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from contextlib import asynccontextmanager, closing
from pathlib import Path

import pytest
import uvicorn
from httpx import AsyncClient
from metahub_gateway.database_runtime import GatewayDatabasePoolMiddleware
from metahub_shared.abuse_risk import runtime_for
from metahub_shared.db import connect_database
from metahub_shared.db.connection import _close_idle_database_connections
from test_rate_limit_redis_transport import resp_probe
from test_virtual_key_rate_limits import (
    PROVIDER_KEY,
    VIRTUAL_KEY_A,
    VIRTUAL_KEY_B,
    _chat,
    build_gateway_app,
    fake_upstream,  # noqa: F401 - imported pytest fixture
    prepare_database,
    request_log_result,
    seeded_virtual_key_id,
    set_limit_rule,
)


@pytest.fixture(autouse=True)
def isolated_gateway_configuration(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[3]
    for name in tuple(os.environ):
        if name.startswith("METAHUB_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "METAHUB_ENV": "test",
        "METAHUB_MIGRATIONS_DIR": str(root / "infra/migrations"),
        "METAHUB_SEED_FILE": str(root / "infra/seeds/default_seed.json"),
        "METAHUB_AUTH_TOKEN_SECRET": "synthetic-http-auth-" + "a" * 32,
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": "synthetic-provider-encryption-" + "b" * 32,
        "METAHUB_SEED_ADMIN_PASSWORD": "Synthetic-only-Aa9!",
        "METAHUB_LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(name, value)
    _close_idle_database_connections()
    yield
    _close_idle_database_connections()


@asynccontextmanager
async def gateway_http(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=1,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("isolated gateway failed to start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), 5)
        finally:
            if not task.done():
                server.force_exit = True
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            listener.close()
        assert task.done()


@pytest.mark.anyio
async def test_redis_fault_is_bounded_at_http_boundary_and_recovery_is_persisted(
    monkeypatch,
    tmp_path,
    request,
    caplog,
    record_property,
):
    upstream_url, captured = request.getfixturevalue("fake_upstream")
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    set_limit_rule(database_url, virtual_key_id=seeded_virtual_key_id(database_url), rpm=10)

    def withheld_command(command):
        if command[0] in {"AUTH", "SELECT"}:
            return b"+OK\r\n"
        assert command[0] in {"EVAL", "PING"}
        return None

    def limit_commands(commands):
        return [command[0] for command in commands if command[0] in {"PING", "INCRBY", "EXPIRE"}]

    replies = [withheld_command] * 3
    async with resp_probe(replies) as probe:
        monkeypatch.setenv(
            "METAHUB_REDIS_URL", f"redis://:private-test-value@127.0.0.1:{probe.port}/7"
        )
        app = build_gateway_app()
        app.add_middleware(GatewayDatabasePoolMiddleware)
        async with gateway_http(app) as origin:
            async with AsyncClient(base_url=origin, timeout=5, trust_env=False) as client:
                before = time.monotonic()
                failed = await _chat(client, VIRTUAL_KEY_A, "req_s3_redis_fault")
                elapsed = time.monotonic() - before
                record_property("redis_fault_http_seconds", round(elapsed, 4))
                assert failed.status_code == 503
                assert failed.json()["code"] == "gateway.rate_limit_unavailable"
                assert failed.json()["request_id"] == "req_s3_redis_fault"
                assert "no-store" in failed.headers["cache-control"]
                assert elapsed < 2.5
                assert not captured
                assert request_log_result(database_url, "req_s3_redis_fault") == (
                    "failed",
                    "gateway.rate_limit_unavailable",
                )
                await asyncio.wait_for(probe.closed.wait(), 1)
                assert limit_commands(probe.commands) == ["PING"]
                risk = runtime_for(app.state, app.state.config)
                assert risk.fixed_counters["gateway_fail_open"] == 1

                command_count = len(probe.commands)
                independent = await _chat(client, VIRTUAL_KEY_B, "req_s3_redis_independent")
                assert independent.status_code == 200
                assert len(captured) == 1
                assert limit_commands(probe.commands[command_count:]) == []
                assert risk.fixed_counters["gateway_fail_open"] == 2
                assert request_log_result(database_url, "req_s3_redis_independent") == (
                    "success",
                    None,
                )

                # Minimal command acknowledgements verify transport recovery, not Redis atomicity.
                def healthy(command):
                    verb = command[0]
                    assert verb in {"AUTH", "SELECT", "EVAL", "PING", "INCRBY", "EXPIRE"}
                    if verb in {"AUTH", "SELECT"}:
                        return b"+OK\r\n"
                    if verb == "EVAL":
                        dimension_count = int(command[2])
                        return (
                            f"*{dimension_count}\r\n".encode("ascii") + b":1\r\n" * dimension_count
                        )
                    return b"+PONG\r\n" if verb == "PING" else b":1\r\n"

                replies[:] = [healthy, healthy, healthy]
                command_count = len(probe.commands)
                recovered = await _chat(client, VIRTUAL_KEY_A, "req_s3_redis_recovered")
                assert recovered.status_code == 200
                assert len(captured) == 2
                assert request_log_result(database_url, "req_s3_redis_recovered") == (
                    "success",
                    None,
                )
                assert limit_commands(probe.commands[command_count:]) == [
                    "PING",
                    "INCRBY",
                    "EXPIRE",
                ]
    with closing(connect_database(database_url)) as connection:
        rows = connection.execute(
            "SELECT * FROM request_logs WHERE request_id IN (?, ?, ?)",
            ("req_s3_redis_fault", "req_s3_redis_independent", "req_s3_redis_recovered"),
        ).fetchall()
        assert len(rows) == 3
        persisted = json.dumps([dict(row) for row in rows], default=str)
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    for secret in [VIRTUAL_KEY_A, VIRTUAL_KEY_B, PROVIDER_KEY, "private-test-value"]:
        assert secret not in failed.text + persisted + caplog.text
