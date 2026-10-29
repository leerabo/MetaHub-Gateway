"""Controlled peer tests for the exact-container lease protocol, not attestations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from metahub_assurance_runner.lease_channel import (
    Check,
    Decision,
    GuestLease,
    Hello,
    LeaseBinding,
    LeaseChannelError,
    SupervisorLeaseSession,
    decode_frame,
    encode_frame,
    frame_packet,
)


@dataclass
class Endpoint:
    incoming: asyncio.Queue
    outgoing: asyncio.Queue
    closed: bool = False
    sent: list[bytes] = field(default_factory=list)

    async def receive(self):
        content = await self.incoming.get()
        if content is None:
            raise EOFError("private-transport-details")
        return content

    async def send(self, content):
        if self.closed:
            raise EOFError("private-transport-details")
        self.sent.append(content)
        await self.outgoing.put(content)

    def close(self):
        if not self.closed:
            self.closed = True
            self.outgoing.put_nowait(None)
            self.incoming.put_nowait(None)


@pytest.fixture
def endpoints():
    left, right = asyncio.Queue(), asyncio.Queue()
    return Endpoint(left, right), Endpoint(right, left)


@pytest.fixture
def binding():
    return LeaseBinding(
        request_sha256="1" * 64,
        environment_id="aenv_" + "2" * 32,
        assessment_generation=1,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        channel_nonce="3" * 64,
    )


async def _allowed():
    return True


@pytest.mark.anyio
async def test_each_check_reaches_authority_and_finish_is_only_a_report_claim(endpoints, binding):
    server, client = endpoints
    checks = 0

    async def authority():
        nonlocal checks
        checks += 1
        return True

    session = SupervisorLeaseSession(server, binding, authority)
    task = asyncio.create_task(session.serve())
    lease = await GuestLease.accept(client)
    assert await lease.guard() and await lease.guard()
    assert await lease.finish("4" * 64)
    result = await task
    assert result.reason_code == "report_announced" and result.report_sha256 == "4" * 64
    assert result.checks_permitted == 2 and checks == 4
    assert not await lease.guard()
    assert server.closed and client.closed
    with pytest.raises(LeaseChannelError, match="already_attempted"):
        await session.serve()


@pytest.mark.anyio
@pytest.mark.parametrize("decision", [False, None, 1, "true", RuntimeError("private-lease-secret")])
async def test_authority_denial_and_exceptions_never_allow_execution(endpoints, binding, decision):
    server, client = endpoints
    calls = 0

    async def authority():
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        if isinstance(decision, Exception):
            raise decision
        return decision

    session = SupervisorLeaseSession(server, binding, authority)
    task = asyncio.create_task(session.serve())
    lease = await GuestLease.accept(client)
    assert not await lease.guard()
    assert not await lease.guard()
    result = await task
    assert result.report_sha256 is None
    assert "private-lease-secret" not in repr(result) + repr(lease) + repr(session)
    assert calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["sequence", "binding", "report_before_check"])
async def test_mismatched_request_fences_session_without_authority_check(endpoints, binding, case):
    server, client = endpoints
    calls = 0

    async def authority():
        nonlocal calls
        calls += 1
        return True

    task = asyncio.create_task(SupervisorLeaseSession(server, binding, authority).serve())
    await client.receive()
    request = Check(binding_sha256=binding.digest(), sequence=1, challenge="4" * 64)
    if case == "sequence":
        request = request.model_copy(update={"sequence": 2})
    elif case == "binding":
        request = request.model_copy(update={"binding_sha256": "f" * 64})
    else:
        request = Hello(binding=binding)
    await client.send(encode_frame(request))
    assert (await task).reason_code == "channel_unavailable"
    assert calls == 1


@pytest.mark.anyio
async def test_replaying_a_previous_check_is_rejected(endpoints, binding):
    server, client = endpoints
    task = asyncio.create_task(SupervisorLeaseSession(server, binding, _allowed).serve())
    await client.receive()
    request = encode_frame(Check(binding_sha256=binding.digest(), sequence=1, challenge="4" * 64))
    await client.send(request)
    assert decode_frame(await client.receive()).permitted
    await client.send(request)
    result = await task
    assert result.checks_permitted == 1 and result.report_sha256 is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "binding",
        "sequence",
        "challenge",
        "denied",
        "expired",
        "future",
        "long_grant",
        "past_request",
        "past_deadline",
    ],
)
async def test_guest_rejects_wrong_or_stale_decisions(endpoints, binding, case):
    server, client = endpoints
    await server.send(encode_frame(Hello(binding=binding)))
    lease = await GuestLease.accept(client)
    task = asyncio.create_task(lease.guard())
    request = decode_frame(await server.receive())
    now = datetime.now(UTC)
    values = dict(
        binding_sha256=request.binding_sha256,
        sequence=request.sequence,
        challenge=request.challenge,
        permitted=True,
        issued_at=now,
        expires_at=now + timedelta(milliseconds=500),
    )
    if case in {"binding", "challenge"}:
        values["binding_sha256" if case == "binding" else "challenge"] = "f" * 64
    elif case == "sequence":
        values["sequence"] = 2
    elif case == "denied":
        values["permitted"] = False
    elif case == "expired":
        values["expires_at"] = now - timedelta(seconds=1)
    elif case == "future":
        values["issued_at"] = now + timedelta(seconds=1)
    elif case == "long_grant":
        values["expires_at"] = now + timedelta(seconds=10)
    elif case == "past_request":
        values["issued_at"] = now - timedelta(seconds=1)
    else:
        values["expires_at"] = binding.deadline_at + timedelta(seconds=1)
    await server.send(encode_frame(Decision(**values)))
    assert not await task
    assert not await lease.guard()
    assert client.closed


@pytest.mark.anyio
async def test_concurrent_guest_check_and_cancellation_permanently_fence(endpoints, binding):
    server, client = endpoints
    await server.send(encode_frame(Hello(binding=binding)))
    lease = await GuestLease.accept(client)
    task = asyncio.create_task(lease.guard())
    await server.receive()
    assert not await lease.guard()
    assert not await task
    assert not await lease.finish("5" * 64)


@pytest.mark.anyio
async def test_timeout_and_cancel_close_both_sides(endpoints, binding, monkeypatch):
    server, client = endpoints
    monkeypatch.setattr("metahub_assurance_runner.lease_channel.IO_TIMEOUT_SECONDS", 0.02)
    task = asyncio.create_task(SupervisorLeaseSession(server, binding, _allowed).serve())
    await GuestLease.accept(client)
    assert (await task).reason_code == "channel_unavailable"
    assert server.closed


@pytest.mark.anyio
async def test_cancelled_guest_check_closes_transport(endpoints, binding):
    server, client = endpoints
    await server.send(encode_frame(Hello(binding=binding)))
    lease = await GuestLease.accept(client)
    task = asyncio.create_task(lease.guard())
    await server.receive()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed and not await lease.guard()


@pytest.mark.anyio
async def test_handshake_transport_exception_is_sanitized(endpoints):
    server, client = endpoints
    server.close()
    with pytest.raises(LeaseChannelError) as error:
        await GuestLease.accept(client)
    assert "private-transport-details" not in str(error.value)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"x" * 4097,
        b"[]",
        b"null",
        b"not-json",
        b"\xff",
        '{"kind":"lease.check"}'.encode("utf-16"),
        b'{"kind":"lease.check","kind":"lease.finish"}',
        b'{"kind":"lease.check","sequence":NaN}',
        b'{"kind":"lease.check","sequence":Infinity}',
    ],
)
def test_strict_json_rejects_ambiguous_or_unbounded_frames(content):
    with pytest.raises(LeaseChannelError):
        decode_frame(content)


@pytest.mark.parametrize(
    "field,value",
    [("sequence", True), ("sequence", "1"), ("sequence", 0), ("unexpected", "private-secret")],
)
def test_strict_frame_fields_reject_coercions_and_unknowns(binding, field, value):
    frame = Check(binding_sha256=binding.digest(), sequence=1, challenge="4" * 64)
    payload = frame.model_dump(mode="json")
    payload[field] = value
    content = json.dumps(payload).encode()
    with pytest.raises(LeaseChannelError):
        decode_frame(content)


def test_frame_packet_bounds():
    with pytest.raises(LeaseChannelError):
        frame_packet(b"x" * 4097)


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["past", "unbounded", "wrong_frame"])
async def test_guest_rejects_invalid_handshake(endpoints, binding, case):
    server, client = endpoints
    if case == "wrong_frame":
        message = Check(binding_sha256=binding.digest(), sequence=1, challenge="4" * 64)
    else:
        delta = timedelta(seconds=-1) if case == "past" else timedelta(days=1)
        message = Hello(
            binding=binding.model_copy(update={"deadline_at": datetime.now(UTC) + delta})
        )
    await server.send(encode_frame(message))
    with pytest.raises(LeaseChannelError):
        await GuestLease.accept(client)
    assert client.closed


@pytest.mark.anyio
async def test_guest_timeout_permanently_fences(endpoints, binding, monkeypatch):
    server, client = endpoints
    monkeypatch.setattr("metahub_assurance_runner.lease_channel.IO_TIMEOUT_SECONDS", 0.02)
    await server.send(encode_frame(Hello(binding=binding)))
    lease = await GuestLease.accept(client)
    assert not await lease.guard()
    assert not await lease.guard()
    assert client.closed


@pytest.mark.anyio
async def test_hanging_authority_times_out_without_a_grant(endpoints, binding, monkeypatch):
    server, _ = endpoints
    monkeypatch.setattr("metahub_assurance_runner.lease_channel.IO_TIMEOUT_SECONDS", 0.02)

    async def authority():
        await asyncio.Event().wait()

    result = await SupervisorLeaseSession(server, binding, authority).serve()
    assert result.reason_code == "channel_unavailable" and not server.sent


@pytest.mark.anyio
async def test_cancelled_supervisor_closes_its_transport(endpoints, binding):
    server, client = endpoints
    task = asyncio.create_task(SupervisorLeaseSession(server, binding, _allowed).serve())
    await client.receive()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.closed


def test_zero_binding_and_challenge_are_rejected(binding):
    message = Hello(binding=binding)
    payload = message.model_dump(mode="json")
    payload["binding"]["channel_nonce"] = "0" * 64
    with pytest.raises(LeaseChannelError):
        decode_frame(json.dumps(payload).encode())
