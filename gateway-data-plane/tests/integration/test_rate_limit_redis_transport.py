"""Real loopback Redis transport faults; not Redis-server or benchmark evidence."""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from metahub_gateway.rate_limits import store as store_module
from metahub_gateway.rate_limits.store import RateLimitStoreUnavailable, RedisRateLimitStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def read_command(reader):
    header = await reader.readline()
    if not header:
        raise asyncio.IncompleteReadError(b"", 1)
    assert header.startswith(b"*") and header.endswith(b"\r\n")
    count = int(header[1:-2])
    assert 1 <= count <= 128
    parts = []
    for _ in range(count):
        bulk = await reader.readline()
        assert bulk.startswith(b"$") and bulk.endswith(b"\r\n")
        size = int(bulk[1:-2])
        assert 0 <= size <= 65536
        parts.append((await reader.readexactly(size)).decode("utf-8"))
        assert await reader.readexactly(2) == b"\r\n"
    return tuple(parts)


@asynccontextmanager
async def resp_probe(responses, *, ssl_context=None, byte_delay=0, raw=False, keep_open=False):
    state = SimpleNamespace(commands=[], received=asyncio.Event(), closed=asyncio.Event())
    handlers, writers, errors = set(), set(), []

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        writers.add(writer)
        try:
            for response in responses:
                state.commands.append(await reader.read(512) if raw else await read_command(reader))
                state.received.set()
                if response is ConnectionResetError:
                    writer.transport.abort()
                    break
                if callable(response):
                    response = response(state.commands[-1])
                if response is None:
                    await reader.read()
                    break
                chunks = (bytes([item]) for item in response) if byte_delay else (response,)
                for chunk in chunks:
                    writer.write(chunk)
                    await writer.drain()
                    if byte_delay:
                        await asyncio.sleep(byte_delay)
            if keep_open:
                await reader.read()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            errors.append(exc)
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (ConnectionError, TimeoutError):
                writer.transport.abort()
            writers.discard(writer)
            handlers.discard(asyncio.current_task())
            state.closed.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ssl_context)
    state.port = server.sockets[0].getsockname()[1]
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*tuple(handlers)), 2)
        assert not writers and not handlers
        assert not errors


def fast_store(url):
    return RedisRateLimitStore(url, command_timeout_seconds=0.1, close_timeout_seconds=0.05)


@pytest.mark.anyio
async def test_blackholed_ping_has_its_own_deadline():
    async with resp_probe([None]) as probe:
        store = fast_store(f"redis://127.0.0.1:{probe.port}/0")
        with pytest.raises(RateLimitStoreUnavailable) as rejected:
            await asyncio.wait_for(store.ping(), timeout=1)
        assert rejected.value.__cause__ is None
        await asyncio.wait_for(probe.closed.wait(), 1)
        assert probe.commands == [("PING",)]


def test_rediss_connection_configuration_is_supported():
    store = RedisRateLimitStore("rediss://synthetic:private-test-value@127.0.0.1:6380/7")
    assert store._config.use_tls
    assert "private-test-value" not in repr(store._config)


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["auth", "select", "ping"])
async def test_wrong_acknowledgement_cannot_admit_next_command(phase):
    replies = [b"+OK\r\n", b"+OK\r\n", b"+PONG\r\n"]
    position = {"auth": 0, "select": 1, "ping": 2}[phase]
    replies[position] = b"+WRONG\r\n"
    async with resp_probe(replies) as probe:
        store = fast_store(f"redis://synthetic:private-test-value@127.0.0.1:{probe.port}/7")
        with pytest.raises(RateLimitStoreUnavailable):
            await store.ping()
        await asyncio.wait_for(probe.closed.wait(), 1)
        assert len(probe.commands) == position + 1


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["auth", "select", "ping"])
async def test_withheld_authentication_or_selection_uses_command_deadline(phase):
    replies = [b"+OK\r\n", b"+OK\r\n", b"+PONG\r\n"]
    position = {"auth": 0, "select": 1, "ping": 2}[phase]
    replies[position] = None
    async with resp_probe(replies) as probe:
        store = fast_store(f"redis://synthetic:private-test-value@127.0.0.1:{probe.port}/7")
        with pytest.raises(RateLimitStoreUnavailable):
            await asyncio.wait_for(store.ping(), 1)
        await asyncio.wait_for(probe.closed.wait(), 1)
        assert len(probe.commands) == position + 1


@pytest.mark.anyio
async def test_dripped_authentication_cannot_renew_the_overall_deadline():
    # Individual fragments arrive on time, but the complete AUTH reply exceeds the budget.
    async with resp_probe([b"+OK\r\n", b"+OK\r\n", b"+PONG\r\n"], byte_delay=0.03) as probe:
        store = fast_store(f"redis://:private-test-value@127.0.0.1:{probe.port}/7")
        with pytest.raises(RateLimitStoreUnavailable):
            await asyncio.wait_for(store.ping(), 0.8)
        await asyncio.wait_for(probe.closed.wait(), 1)
        assert probe.commands == [("AUTH", "private-test-value")]


@pytest.mark.anyio
async def test_auth_select_and_command_share_one_budget():
    async with resp_probe([b"+OK\r\n", b"+OK\r\n", b"+PONG\r\n"], byte_delay=0.03) as probe:
        store = RedisRateLimitStore(
            f"redis://:private-test-value@127.0.0.1:{probe.port}/7",
            command_timeout_seconds=0.4,
        )
        with pytest.raises(RateLimitStoreUnavailable):
            await asyncio.wait_for(store.ping(), 2)
        await asyncio.wait_for(probe.closed.wait(), 1)
        assert [command[0] for command in probe.commands] == ["AUTH", "SELECT", "PING"]


@pytest.mark.anyio
@pytest.mark.parametrize("keep_open", [False, True], ids=["eof", "withheld"])
@pytest.mark.parametrize("response", [b"+PO", b"$6\r\nPON", b"*2\r\n:1\r\n"])
async def test_truncated_response_fails_without_hanging(response, keep_open):
    async with resp_probe([response], keep_open=keep_open) as probe:
        with pytest.raises(RateLimitStoreUnavailable):
            await asyncio.wait_for(fast_store(f"redis://127.0.0.1:{probe.port}").ping(), 1)
        await asyncio.wait_for(probe.closed.wait(), 1)


@pytest.mark.anyio
@pytest.mark.parametrize("fault", [None, ConnectionResetError, b"-ERR private-test-value\r\n"])
async def test_same_store_recovers_after_connection_fault(fault, caplog):
    replies = [fault]
    async with resp_probe(replies) as probe:
        store = fast_store(f"redis://127.0.0.1:{probe.port}")
        with pytest.raises(RateLimitStoreUnavailable) as rejected:
            await asyncio.wait_for(store.ping(), 1)
        rendered = "".join(traceback.format_exception(rejected.value))
        assert str(rejected.value) == "redis command failed"
        assert "private-test-value" not in rendered + caplog.text
        assert rejected.value.__cause__ is None
        await asyncio.wait_for(probe.closed.wait(), 1)
        replies[0] = b"+PONG\r\n"
        probe.closed.clear()
        assert await asyncio.wait_for(store.ping(), 1) is True
        await asyncio.wait_for(probe.closed.wait(), 1)
        assert probe.commands == [("PING",), ("PING",)]


@pytest.mark.anyio
async def test_caller_cancellation_closes_transport_and_remains_cancellation():
    async with resp_probe([None]) as probe:
        store = RedisRateLimitStore(f"redis://127.0.0.1:{probe.port}")
        task = asyncio.create_task(store.ping())
        await asyncio.wait_for(probe.received.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        await asyncio.wait_for(probe.closed.wait(), 1)
        assert task.cancelled()


@pytest.fixture
def tls_material(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-redis-test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path, key_path = tmp_path / "certificate.pem", tmp_path / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate_path, key_path)
    return SimpleNamespace(server=server, certificate_path=certificate_path)


@pytest.mark.anyio
async def test_real_tls_authenticates_only_over_verified_connection(tls_material, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_material.certificate_path))
    async with resp_probe(
        [b"+OK\r\n", b"+OK\r\n", b"+PONG\r\n"], ssl_context=tls_material.server
    ) as probe:
        store = RedisRateLimitStore(
            f"rediss://synthetic:private-test-value@127.0.0.1:{probe.port}/7",
            command_timeout_seconds=1,
        )
        assert store._ssl_context.check_hostname
        assert store._ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert await asyncio.wait_for(store.ping(), 2) is True
        await asyncio.wait_for(probe.closed.wait(), 1)
        assert probe.commands == [
            ("AUTH", "synthetic", "private-test-value"),
            ("SELECT", "7"),
            ("PING",),
        ]


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["untrusted", "hostname"])
async def test_invalid_tls_certificate_never_sends_auth(tls_material, monkeypatch, invalid):
    if invalid == "hostname":
        monkeypatch.setenv("SSL_CERT_FILE", str(tls_material.certificate_path))
    async with resp_probe([b"+OK\r\n"], ssl_context=tls_material.server) as probe:
        host = "localhost" if invalid == "hostname" else "127.0.0.1"
        store = RedisRateLimitStore(
            f"rediss://:private-test-value@{host}:{probe.port}", command_timeout_seconds=1
        )
        with pytest.raises(RateLimitStoreUnavailable) as rejected:
            await asyncio.wait_for(store.ping(), 2)
        assert str(rejected.value) == "redis command failed"
        assert not probe.commands


@pytest.mark.anyio
async def test_tls_handshake_blackhole_never_falls_back_to_plaintext():
    async with resp_probe([None], raw=True) as probe:
        store = fast_store(f"rediss://:private-test-value@127.0.0.1:{probe.port}")
        with pytest.raises(RateLimitStoreUnavailable):
            await asyncio.wait_for(store.ping(), 1)
        await asyncio.wait_for(probe.closed.wait(), 1)
        assert len(probe.commands) == 1
        assert probe.commands[0].startswith(b"\x16\x03")
        assert b"private-test-value" not in probe.commands[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        b"+PONG\n",
        b"+" + b"a" * 1024 + b"\r\n",
        b"+\xff\r\n",
        b":+1\r\n",
        b":01\r\n",
        b":9223372036854775808\r\n",
        b": 1\r\n",
        b"$-2\r\n",
        b"$65536\r\n",
        b"$4\r\nPONGxx",
        b"$2\r\n\xff\xff\r\n",
        b"*-2\r\n",
        b"*128\r\n",
        b"*1\r\n" * 5 + b":1\r\n",
        b"?PONG\r\n",
        b"*2\r\n$32768\r\n" + b"a" * 32768 + b"\r\n$32768\r\n",
    ],
    ids=[
        "bare-lf",
        "long-line",
        "invalid-text",
        "signed-plus",
        "leading-zero",
        "overflow",
        "whitespace",
        "negative-bulk",
        "large-bulk",
        "bad-bulk-crlf",
        "invalid-bulk-text",
        "negative-array",
        "large-array",
        "deep-array",
        "unknown-type",
        "aggregate-byte-budget",
    ],
)
async def test_malformed_or_oversized_resp_is_bounded(response):
    async with resp_probe([response]) as probe:
        with pytest.raises(RateLimitStoreUnavailable) as rejected:
            await asyncio.wait_for(
                fast_store(f"redis://127.0.0.1:{probe.port}")._command("PING"), 1
            )
        assert str(rejected.value) == "redis command failed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (b"+PONG\r\n", "PONG"),
        (b":-9223372036854775808\r\n", -(2**63)),
        (b":9223372036854775807\r\n", 2**63 - 1),
        (b"$4\r\nPONG\r\n", "PONG"),
        (b"$-1\r\n", None),
        (b"*-1\r\n", None),
        (b"*0\r\n", []),
        (b"*3\r\n:1\r\n:0\r\n:0\r\n", [1, 0, 0]),
    ],
)
async def test_supported_resp_types_remain_compatible(response, expected):
    async with resp_probe([response]) as probe:
        assert await fast_store(f"redis://127.0.0.1:{probe.port}")._command("PING") == expected


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["increment", "acquire", "release"])
async def test_counters_reject_string_coercion(operation):
    async with resp_probe([b"$1\r\n1\r\n"]) as probe:
        store = fast_store(f"redis://127.0.0.1:{probe.port}")
        call = {
            "increment": lambda: store.increment_window("counter", 1, 60),
            "acquire": lambda: store.acquire_concurrency("counter", 2, 60),
            "release": lambda: store.release_concurrency("counter"),
        }[operation]
        with pytest.raises(RateLimitStoreUnavailable):
            await call()
        assert len(probe.commands) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "redis:///0",
        "redis://127.0.0.1:0",
        "redis://127.0.0.1:65536",
        "redis://127.0.0.1:no",
        "redis://user@127.0.0.1",
        "redis://127.0.0.1/-1",
        "redis://127.0.0.1/256",
        "redis://127.0.0.1/1/2",
        "redis://127.0.0.1/1?secret=x",
        "redis://127.0.0.1/#secret",
        None,
    ],
)
def test_invalid_configuration_is_rejected_without_disclosure(url):
    with pytest.raises(RateLimitStoreUnavailable, match="^redis command failed$"):
        RedisRateLimitStore(url)


@pytest.mark.parametrize("value", [True, None, "0.5", 0, 0.049, 10.1, float("inf"), float("nan")])
@pytest.mark.parametrize("field", ["command_timeout_seconds", "close_timeout_seconds"])
def test_invalid_deadline_configuration_is_rejected(field, value):
    with pytest.raises(RateLimitStoreUnavailable):
        RedisRateLimitStore("redis://127.0.0.1", **{field: value})


class StuckCloseWriter:
    def __init__(self):
        self.transport = self
        self.closed = False
        self.aborted = False
        self.closing = asyncio.Event()

    def write(self, _data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.closing.set()
        await asyncio.Event().wait()

    def abort(self):
        self.aborted = True


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["connect", "write"])
async def test_deadline_also_covers_connection_and_backpressure(monkeypatch, phase):
    writer = StuckCloseWriter()

    async def never_finishes():
        await asyncio.Event().wait()

    async def connect(*_args, **_kwargs):
        if phase == "connect":
            await never_finishes()
        writer.drain = never_finishes
        return asyncio.StreamReader(), writer

    monkeypatch.setattr(store_module.asyncio, "open_connection", connect)
    with pytest.raises(RateLimitStoreUnavailable):
        await asyncio.wait_for(fast_store("redis://127.0.0.1").ping(), 1)
    assert writer.closed == (phase == "write")
    assert writer.aborted == (phase == "write")


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["success", "timeout", "cancel-response", "cancel-close"])
async def test_stuck_close_is_aborted_without_masking_original_outcome(monkeypatch, outcome):
    reader, writer = asyncio.StreamReader(), StuckCloseWriter()
    connected = asyncio.Event()
    if outcome in {"success", "cancel-close"}:
        reader.feed_data(b"+PONG\r\n")

    async def connect(*_args, **_kwargs):
        connected.set()
        return reader, writer

    monkeypatch.setattr(store_module.asyncio, "open_connection", connect)
    task = asyncio.create_task(fast_store("redis://127.0.0.1").ping())
    await asyncio.wait_for(connected.wait(), 1)
    if outcome == "cancel-response":
        task.cancel()
    if outcome == "cancel-close":
        await asyncio.wait_for(writer.closing.wait(), 1)
        task.cancel()
    if outcome == "success":
        assert await asyncio.wait_for(task, 1) is True
    else:
        expected = (
            asyncio.CancelledError if outcome.startswith("cancel") else RateLimitStoreUnavailable
        )
        with pytest.raises(expected):
            await asyncio.wait_for(task, 1)
    assert writer.closed and writer.aborted
