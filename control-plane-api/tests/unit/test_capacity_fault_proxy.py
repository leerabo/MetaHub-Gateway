"""Lease fencing plus actual byte forwarding through disposable loopback sockets."""

from __future__ import annotations

import asyncio
import multiprocessing
import socket
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_capacity_runner.contracts import CapacityPermitClaims, encode_signed
from metahub_capacity_runner.engine import FAULTS
from metahub_capacity_runner.fault_app import app
from metahub_capacity_runner.fault_contracts import (
    FaultBinding,
    FaultLeaseCommand,
    FaultProxySettings,
    private_host,
)
from metahub_capacity_runner.fault_proxy import FaultProxy
from metahub_capacity_runner.fault_store import FaultLeaseStore


def execution(fault="dependency-timeout"):
    now = datetime.now(UTC)
    return CapacityPermitClaims(
        permit_id="crp_" + uuid4().hex,
        object_id="che_" + uuid4().hex,
        operation="chaos",
        service_code="gateway",
        fault_code=fault,
        environment_class="staging",
        region_code="cn-north-1",
        synthetic_tenant_count=2,
        target_rps=0,
        duration_seconds=10,
        objective_p95_ms=1000,
        objective_error_rate_bps=100,
        stop_error_rate_bps=1000,
        manifest_sha256=FAULTS[fault][-1],
        kill_generation=0,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )


def command(run, *, action="apply", sequence=1, seconds=10):
    now = datetime.now(UTC)
    return FaultLeaseCommand(
        execution=run,
        binding_code="synthetic-dependency",
        action=action,
        sequence=sequence,
        issued_at=now,
        expires_at=now + timedelta(seconds=seconds),
    )


def binding(**updates):
    return FaultBinding(
        **(
            {
                "binding_code": "synthetic-dependency",
                "service_code": "gateway",
                "environment_class": "staging",
                "region_code": "cn-north-1",
                "fault_codes": ["dependency-timeout", "redis-unavailable"],
                "listen_host": "127.0.0.1",
                "listen_port": 19001,
                "upstream_host": "127.0.0.1",
                "upstream_port": 19002,
            }
            | updates
        ),
    )


def configuration(tmp_path, target):
    return FaultProxySettings(
        state_directory=tmp_path,
        internal_token_file=tmp_path / "internal.secret",
        permit_secret_file=tmp_path / "permit.secret",
        bindings=[target],
    )


def test_cancel_before_apply_and_conflicting_replays(tmp_path):
    store, target, run = FaultLeaseStore(tmp_path), binding(), execution()
    release = command(run, action="release", sequence=2)
    assert store.command(release, target).state == "released"
    assert store.command(release, target).state == "released"
    with pytest.raises(ValueError, match="sequence_conflict"):
        store.command(command(run), target)
    assert store.command(command(run, sequence=3), target).state == "released"
    changed = run.model_copy(update={"kill_generation": 1})
    with pytest.raises(ValueError, match="scope_conflict"):
        store.command(command(changed, sequence=4), target)
    assert store.active_fault(target.binding_code) is None


def test_apply_replay_does_not_extend_lease_and_only_renew_advances(tmp_path):
    store, target, run = FaultLeaseStore(tmp_path), binding(), execution()
    original = command(run)
    first = store.command(original, target)
    deadline = store.deadlines[run.object_id]
    assert store.command(original, target) == first
    assert store.deadlines[run.object_id] == deadline
    with pytest.raises(ValueError, match="sequence_conflict"):
        store.command(command(run), target)
    with pytest.raises(ValueError, match="already_applied"):
        store.command(command(run, sequence=2), target)
    renewed = store.command(command(run, action="renew", sequence=2, seconds=20), target)
    assert renewed.end_at == first.end_at
    assert renewed.lease_until <= first.end_at
    assert store.command(command(run, action="release", sequence=3), target).state == "released"
    assert store.command(command(run, action="renew", sequence=4), target).state == "released"


def test_expiry_cannot_be_revived_and_does_not_block_next_owner(tmp_path):
    store, target, run = FaultLeaseStore(tmp_path), binding(), execution()
    store.command(command(run), target)
    store.deadlines[run.object_id] = time.monotonic() - 1
    # Exercise expiry within the command transaction without a prior timer/read.
    assert store.command(command(run, action="renew", sequence=2), target).state == "expired"
    assert store.active_fault(target.binding_code) is None
    assert store.command(command(execution()), target).state == "active"


def test_renewal_cannot_extend_original_monotonic_duration(tmp_path):
    store, target, run = FaultLeaseStore(tmp_path), binding(), execution()
    store.command(command(run, seconds=20), target)
    end = store.end_deadlines[run.object_id]
    store.command(command(run, action="renew", sequence=2, seconds=30), target)
    assert store.deadlines[run.object_id] <= end


def test_overlapping_binding_and_restart_are_fenced(tmp_path):
    store, target, run = FaultLeaseStore(tmp_path), binding(), execution()
    store.command(command(run), target)
    with pytest.raises(ValueError, match="binding_busy"):
        store.command(command(execution()), target)
    reopened = FaultLeaseStore(tmp_path)
    with pytest.raises(ValueError, match="recovery_required"):
        reopened.active_fault(target.binding_code)
    reopened.interrupt()
    assert reopened.command(command(run, action="renew", sequence=2), target).state == "interrupted"
    assert reopened.active_fault(target.binding_code) is None


@pytest.mark.parametrize(
    "host", ["localhost", "8.8.8.8", "169.254.169.254", "0.0.0.0", "::", "ff02::1", "fe80::1%12"]
)
def test_targets_reject_public_dns_metadata_and_wildcards(host):
    with pytest.raises(ValueError, match="address_invalid"):
        private_host(host)


def test_configuration_rejects_loops_duplicate_listeners_and_unknown_fields(tmp_path):
    target = binding(upstream_port=19001)
    with pytest.raises(ValueError, match="configuration_invalid"):
        configuration(tmp_path, target)
    settings = configuration(tmp_path, binding())
    with pytest.raises(ValueError, match="configuration_invalid"):
        FaultProxySettings.model_validate(
            settings.model_dump() | {"bindings": [binding(), binding()]}
        )
    with pytest.raises(ValueError):
        binding(command="arbitrary")
    with pytest.raises(ValueError):
        binding(fault_codes=["resource-pressure"])


def test_expired_start_window_and_binding_mismatch(tmp_path):
    run = execution()
    stale = run.model_copy(
        update={
            "issued_at": datetime.now(UTC) - timedelta(minutes=6),
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
        }
    )
    store = FaultLeaseStore(tmp_path)
    with pytest.raises(ValueError, match="start_window_expired"):
        store.command(command(stale), binding())
    with pytest.raises(ValueError, match="command_invalid"):
        store.command(command(run), binding(region_code="cn-south-1"))
    with pytest.raises(ValueError, match="command_invalid"):
        command(run, seconds=31)


@asynccontextmanager
async def network(tmp_path):
    peers = set()

    async def echo(reader, writer):
        task = asyncio.current_task()
        peers.add(task)
        try:
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            peers.discard(task)

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    target = binding(listen_port=port, upstream_port=server.sockets[0].getsockname()[1])
    proxy = FaultProxy(configuration(tmp_path, target))
    await proxy.start()
    try:
        yield proxy, target
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()
        for peer in list(peers):
            peer.cancel()
        if peers:
            await asyncio.gather(*peers, return_exceptions=True)


async def connect(target):
    return await asyncio.open_connection(target.listen_host, target.listen_port)


async def close(writer):
    writer.close()
    await writer.wait_closed()


@pytest.mark.anyio
async def test_real_timeout_release_resumes_existing_connection_without_payload_persistence(
    tmp_path,
):
    async with network(tmp_path) as (proxy, target):
        reader, writer = await connect(target)
        writer.write(b"synthetic-before")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(16), 2) == b"synthetic-before"
        run = execution()
        proxy.store.command(command(run), target)
        writer.write(b"synthetic-during")
        await writer.drain()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reader.readexactly(16), 0.25)
        proxy.store.command(command(run, action="release", sequence=2), target)
        assert await asyncio.wait_for(reader.readexactly(16), 2) == b"synthetic-during"
        await close(writer)
    with proxy.store.connection() as connection:
        rows = [tuple(row) for row in connection.execute("SELECT * FROM fault_leases")]
        assert "synthetic-during" not in str(rows)


@pytest.mark.anyio
async def test_real_timeout_expires_without_runner_heartbeat(tmp_path):
    async with network(tmp_path) as (proxy, target):
        run = execution()
        proxy.store.command(command(run, seconds=0.45), target)
        reader, writer = await connect(target)
        writer.write(b"probe")
        await writer.drain()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reader.readexactly(5), 0.15)
        assert await asyncio.wait_for(reader.readexactly(5), 2) == b"probe"
        assert proxy.store.command(command(run, action="status"), target).state == "expired"
        await close(writer)


@pytest.mark.anyio
async def test_real_reset_closes_idle_connection_and_new_connection_recovers(tmp_path):
    async with network(tmp_path) as (proxy, target):
        reader, writer = await connect(target)
        writer.write(b"ok")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(2), 2) == b"ok"
        run = execution("redis-unavailable")
        proxy.store.command(command(run), target)
        assert await asyncio.wait_for(reader.read(), 2) == b""
        await close(writer)
        proxy.store.command(command(run, action="release", sequence=2), target)
        reader, writer = await connect(target)
        writer.write(b"recovered")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(9), 2) == b"recovered"
        await close(writer)


@pytest.mark.anyio
async def test_proxy_single_owner_and_restart_cannot_reinject(tmp_path):
    async with network(tmp_path) as (proxy, target):
        run = execution()
        proxy.store.command(command(run), target)
        competitor = FaultProxy(proxy.settings)
        with pytest.raises(ValueError, match="proxy_busy"):
            await competitor.start()
        assert proxy.store.active_fault(target.binding_code) == "dependency-timeout"
        await proxy.close()
        await competitor.start()
        try:
            assert (
                competitor.store.command(command(run, action="status"), target).state
                == "interrupted"
            )
            assert (
                competitor.store.command(command(run, action="renew", sequence=2), target).state
                == "interrupted"
            )
            reader, writer = await connect(target)
            writer.write(b"ok")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(2), 2) == b"ok"
            await close(writer)
        finally:
            await competitor.close()


@pytest.mark.anyio
async def test_half_close_keeps_response_and_shutdown_closes_active_streams(tmp_path):
    async with network(tmp_path) as (proxy, target):
        reader, writer = await connect(target)
        writer.write(b"half-closed")
        await writer.drain()
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 2) == b"half-closed"
        await close(writer)
        reader, writer = await connect(target)
        await proxy.close()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        assert all(not group for group in proxy.connections.values())
        await close(writer)


@pytest.mark.anyio
async def test_api_auth_signature_scope_and_validation_redact_input(tmp_path, monkeypatch):
    async with network(tmp_path) as (proxy, target):
        token, secret = "synthetic-internal-" + "t" * 32, "synthetic-signing-" + "s" * 32
        monkeypatch.setattr(app.state, "proxy", proxy, raising=False)
        monkeypatch.setattr(app.state, "credentials", (token, secret), raising=False)
        original = command(execution())
        permit = encode_signed(original, secret, domain="fault-lease")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://fault.test"
        ) as client:
            endpoint = "/internal/capacity/faults"
            headers = {"Authorization": f"Bearer {token}"}
            assert (await client.post(endpoint, json={"permit": permit})).status_code == 401
            bad = await client.post(endpoint, headers=headers, json={"permit": permit[:-1] + "z"})
            assert bad.status_code == 401
            assert permit not in bad.text
            invalid = await client.post(endpoint, headers=headers, json={"permit": "private-input"})
            assert invalid.status_code == 422 and "private-input" not in invalid.text
            response = await client.post(endpoint, headers=headers, json={"permit": permit})
            assert response.status_code == 200 and response.json()["state"] == "active"
            assert response.headers["cache-control"] == "no-store"
            assert token not in response.text and permit not in response.text
            assert (await client.get("/ready")).status_code == 200

            def unavailable(_binding):
                raise sqlite3.OperationalError("private-database-path")

            monkeypatch.setattr(proxy.store, "active_fault", unavailable)
            unavailable_response = await client.get("/ready")
            assert unavailable_response.status_code == 503
            assert "private-database-path" not in unavailable_response.text


@pytest.mark.anyio
async def test_oversized_chunked_request_is_bounded_and_has_safe_request_id():
    async def chunks():
        for _ in range(4):
            yield b"private-input" * 1024

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://fault.test"
    ) as client:
        response = await client.post(
            "/internal/capacity/faults",
            content=chunks(),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["detail"] == "fault.request_too_large"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["cache-control"] == "no-store"
    assert "private-input" not in response.text


@pytest.mark.parametrize(
    "field,value", [("manifest_sha256", "0" * 64), ("synthetic_tenant_count", 26)]
)
def test_fault_commands_enforce_original_closed_manifest_and_blast_radius(field, value):
    with pytest.raises(ValueError, match="manifest_invalid"):
        command(execution().model_copy(update={field: value}))


def test_concurrent_apply_has_one_owner_and_cancel_wins(tmp_path):
    store, target, run = FaultLeaseStore(tmp_path), binding(), execution()
    original = command(run)
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(lambda _: store.command(original, target), range(16)))
    assert all(reply == replies[0] for reply in replies)
    release = command(run, action="release", sequence=3)
    renewal = command(run, action="renew", sequence=2)

    def dispatch(item):
        try:
            return store.command(item, target).state
        except ValueError as exc:
            assert str(exc) == "fault.sequence_conflict"
            return "fenced"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(dispatch, [renewal, release]))
    assert store.command(command(run, action="status"), target).state == "released"


def test_backward_wall_clock_still_expires_by_monotonic_time(tmp_path, monkeypatch):
    store, target, run = FaultLeaseStore(tmp_path), binding(), execution()
    store.command(command(run), target)
    now, monotonic = time.time(), time.monotonic()
    monkeypatch.setattr(time, "time", lambda: now - 3600)
    monkeypatch.setattr(time, "monotonic", lambda: monotonic + 31)
    assert store.active_fault(target.binding_code) is None
    assert not store.deadlines and not store.end_deadlines


def test_allowed_clock_skew_does_not_extend_thirty_second_lease(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    run = execution().model_copy(update={"duration_seconds": 60})
    request = command(run).model_copy(
        update={
            "issued_at": now + timedelta(seconds=0.5),
            "expires_at": now + timedelta(seconds=30.5),
        }
    )
    monkeypatch.setattr(time, "time", lambda: now.timestamp())
    info = FaultLeaseStore(tmp_path).command(request, binding())
    assert info.lease_until.timestamp() == now.timestamp() + 30


def test_rolled_back_expiry_does_not_drop_process_deadline(tmp_path):
    store, target, run = FaultLeaseStore(tmp_path), binding(), execution()
    store.command(command(run), target)
    store.deadlines[run.object_id] = time.monotonic() - 1
    with pytest.raises(ValueError, match="sequence_conflict"):
        store.command(command(run), target)
    assert store.active_fault(target.binding_code) is None
    assert store.command(command(run, action="status"), target).state == "expired"


@pytest.mark.anyio
async def test_store_failure_closes_real_connection(tmp_path, monkeypatch):
    async with network(tmp_path) as (proxy, target):
        reader, writer = await connect(target)
        writer.write(b"ok")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(2), 2) == b"ok"

        def unavailable(_binding):
            raise sqlite3.OperationalError("synthetic-store-unavailable")

        monkeypatch.setattr(proxy.store, "active_fault", unavailable)
        assert await asyncio.wait_for(reader.read(), 2) == b""
        await close(writer)


@pytest.mark.anyio
async def test_double_start_and_commands_after_shutdown_are_rejected(tmp_path):
    async with network(tmp_path) as (proxy, target):
        run = execution()
        proxy.command(command(run))
        with pytest.raises(ValueError, match="proxy_busy"):
            await proxy.start()
        assert proxy.store.active_fault(target.binding_code) == "dependency-timeout"
        await proxy.close()
        with pytest.raises(ValueError, match="proxy_unavailable"):
            proxy.command(command(execution()))


@pytest.mark.anyio
async def test_connection_limit_and_large_stream_have_bounded_ownership(tmp_path):
    async with network(tmp_path) as (proxy, original):
        await proxy.close()
        target = binding(
            listen_port=original.listen_port,
            upstream_port=original.upstream_port,
            max_connections=1,
        )
        limited = FaultProxy(configuration(tmp_path, target))
        await limited.start()
        try:
            reader, writer = await connect(target)
            payload = b"synthetic-transfer-" * 32768
            writer.write(payload)
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(len(payload)), 5) == payload
            extra_reader, extra_writer = await connect(target)
            assert await asyncio.wait_for(extra_reader.read(), 2) == b""
            assert len(limited.connections[target.binding_code]) == 1
            await close(extra_writer)
            await close(writer)
        finally:
            await limited.close()


def fault_process(settings_json, command_json, channel):
    async def serve():
        proxy = FaultProxy(FaultProxySettings.model_validate_json(settings_json))
        await proxy.start()
        try:
            proxy.command(FaultLeaseCommand.model_validate_json(command_json))
            channel.send("ready")
            await asyncio.Event().wait()
        finally:
            await proxy.close()

    asyncio.run(serve())


@pytest.mark.anyio
async def test_abrupt_process_death_closes_sockets_and_restart_ends_fault(tmp_path):
    async with network(tmp_path) as (initial, target):
        await initial.close()
        run = execution()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=fault_process,
            args=(
                initial.settings.model_dump_json(),
                command(run, seconds=20).model_dump_json(),
                child,
            ),
        )
        process.start()
        child.close()
        writer = None
        try:
            assert await asyncio.to_thread(parent.poll, 10), "fault child did not become ready"
            assert parent.recv() == "ready"
            reader, writer = await connect(target)
            writer.write(b"held")
            await writer.drain()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(reader.readexactly(4), 0.2)
            process.terminate()
            await asyncio.to_thread(process.join, 5)
            assert not process.is_alive()
            try:
                assert await asyncio.wait_for(reader.read(), 2) == b""
            except ConnectionResetError:
                pass
            with initial.store.connection() as connection:
                assert (
                    connection.execute("SELECT state FROM fault_leases").fetchone()[0] == "active"
                )
            recovered = FaultProxy(initial.settings)
            await recovered.start()
            try:
                assert recovered.command(command(run, action="status")).state == "interrupted"
                assert (
                    recovered.command(command(run, action="renew", sequence=2)).state
                    == "interrupted"
                )
                new_reader, new_writer = await connect(target)
                new_writer.write(b"recovered")
                await new_writer.drain()
                assert await asyncio.wait_for(new_reader.readexactly(9), 2) == b"recovered"
                await close(new_writer)
            finally:
                await recovered.close()
        finally:
            if writer is not None:
                try:
                    await close(writer)
                except ConnectionResetError:
                    pass
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
            assert not process.is_alive()
            process.close()
            parent.close()


@pytest.mark.anyio
async def test_service_lifespan_loads_deployment_files_and_closes_listeners(tmp_path, monkeypatch):
    async with network(tmp_path) as (initial, target):
        await initial.close()
        settings = initial.settings
        settings.internal_token_file.write_text("synthetic-token-" + "a" * 32, encoding="ascii")
        settings.permit_secret_file.write_text("synthetic-secret-" + "b" * 32, encoding="ascii")
        path = tmp_path / "fault-settings.json"
        path.write_text(settings.model_dump_json(), encoding="ascii")
        monkeypatch.setenv("METAHUB_CAPACITY_FAULT_SETTINGS_FILE", str(path))
        async with app.router.lifespan_context(app):
            assert app.state.proxy.running
            reader, writer = await connect(target)
            writer.write(b"ready")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(5), 2) == b"ready"
            await close(writer)
        assert app.state.proxy is None and app.state.credentials is None
        with pytest.raises(OSError):
            await connect(target)
        settings.permit_secret_file.write_text(
            settings.internal_token_file.read_text(), encoding="ascii"
        )
        with pytest.raises(ValueError, match="^fault.configuration_invalid$"):
            async with app.router.lifespan_context(app):
                pytest.fail("reused internal credential started a fault proxy")
