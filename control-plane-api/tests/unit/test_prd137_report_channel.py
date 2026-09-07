"""Report transport tests do not authenticate report claims or execution."""

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from metahub_assurance_runner.lease_channel import (
    MAX_REPORT_BYTES,
    Finish,
    GuestLease,
    LeaseBinding,
    ReportChunk,
    SupervisorLeaseSession,
    decode_frame,
    encode_frame,
)
from test_prd137_lease_channel import Endpoint


def pair(authority):
    left, right = asyncio.Queue(), asyncio.Queue()
    server, guest = Endpoint(left, right), Endpoint(right, left)
    binding = LeaseBinding(
        request_sha256="1" * 64,
        environment_id="aenv_" + "2" * 32,
        assessment_generation=1,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        channel_nonce="3" * 64,
    )
    session = SupervisorLeaseSession(server, binding, authority)
    return guest, session


async def allowed():
    return True


@pytest.mark.anyio
@pytest.mark.parametrize("size", [1, 1024, 1025, MAX_REPORT_BYTES])
async def test_exact_report_bytes_are_received_and_hidden_from_repr(size):
    client, session = pair(allowed)
    task = asyncio.create_task(session.serve())
    guest = await GuestLease.accept(client)
    report = b"x" * size
    assert await guest.publish(report)
    result = await task
    assert result.reason_code == "report_received"
    assert result.report_bytes == report
    assert result.report_sha256 == hashlib.sha256(report).hexdigest()
    assert "report_bytes=" not in repr(result)
    assert result.checks_permitted == (size + 1023) // 1024
    assert not await guest.publish(report)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    [b"", b"x" * (MAX_REPORT_BYTES + 1), "not-bytes"],
    ids=["empty", "oversize", "wrong-type"],
)
async def test_invalid_size_or_type_does_not_send_a_report(content):
    client, session = pair(allowed)
    task = asyncio.create_task(session.serve())
    guest = await GuestLease.accept(client)
    assert not await guest.publish(content)
    assert (await task).report_bytes is None
    assert not client.sent


@pytest.mark.anyio
@pytest.mark.parametrize("deny_at", [2, 3, 4])
async def test_revoked_chunk_or_finish_does_not_release_partial_or_complete_report(deny_at):
    calls = 0

    async def authority():
        nonlocal calls
        calls += 1
        return calls < deny_at

    client, session = pair(authority)
    task = asyncio.create_task(session.serve())
    guest = await GuestLease.accept(client)
    assert not await guest.publish(b"x" * 1025)
    result = await task
    assert result.reason_code == "lease_revoked" and result.report_bytes is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    ["index", "size", "digest", "truncated", "noncanonical", "finish_digest", "content_digest"],
)
async def test_mismatched_or_incomplete_report_is_rejected(case):
    client, session = pair(allowed)
    task = asyncio.create_task(session.serve())
    await client.receive()
    report = b"x" * 1025
    digest = hashlib.sha256(report).hexdigest()
    first = ReportChunk(
        binding_sha256=session.binding.digest(),
        sequence=1,
        challenge="4" * 64,
        report_sha256=digest,
        total_bytes=len(report),
        chunk_index=0,
        content_base64=base64.b64encode(report[:1024]).decode(),
    )
    await client.send(encode_frame(first))
    assert decode_frame(await client.receive()).permitted
    second = first.model_copy(
        update={
            "sequence": 2,
            "challenge": "5" * 64,
            "chunk_index": 1,
            "content_base64": "eA==",
        }
    )
    if case == "truncated":
        second = Finish(
            binding_sha256=session.binding.digest(),
            sequence=2,
            challenge="5" * 64,
            report_sha256=digest,
        )
    elif case in {"index", "size", "digest", "noncanonical", "content_digest"}:
        key, value = {
            "index": ("chunk_index", 2),
            "size": ("total_bytes", 1026),
            "digest": ("report_sha256", "f" * 64),
            "noncanonical": ("content_base64", "eB=="),
            "content_digest": ("content_base64", "eQ=="),
        }[case]
        second = second.model_copy(update={key: value})
    await client.send(encode_frame(second))
    if case in {"finish_digest", "content_digest"}:
        assert decode_frame(await client.receive()).permitted
        await client.send(
            encode_frame(
                Finish(
                    binding_sha256=session.binding.digest(),
                    sequence=3,
                    challenge="6" * 64,
                    report_sha256="f" * 64 if case == "finish_digest" else digest,
                )
            )
        )
    result = await task
    assert result.reason_code == "channel_unavailable" and result.report_bytes is None


@pytest.mark.anyio
async def test_report_chunk_body_is_not_in_frame_repr():
    frame = ReportChunk(
        binding_sha256="1" * 64,
        sequence=1,
        challenge="2" * 64,
        report_sha256="3" * 64,
        total_bytes=1,
        chunk_index=0,
        content_base64="eA==",
    )
    assert "content_base64" not in repr(frame)
