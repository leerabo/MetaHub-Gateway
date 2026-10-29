"""Real socket bytes through Docker's SDK decoder, with synthetic daemon frames."""

from __future__ import annotations

import asyncio
import socket
import struct
from contextlib import contextmanager

import pytest
from metahub_assurance_runner.docker_channel import DockerLeaseTransport
from metahub_assurance_runner.lease_channel import LeaseChannelError, frame_packet


@contextmanager
def sockets():
    left, right = socket.socketpair()
    try:
        yield left, right
    finally:
        left.close()
        right.close()


def multiplex(stream, content):
    return struct.pack("!BxxxI", stream, len(content)) + content


@pytest.mark.anyio
async def test_sdk_demultiplexes_fragmented_stdout_and_discards_stderr():
    with sockets() as (left, right):
        transport = DockerLeaseTransport(left)
        packet = frame_packet(b'{"test":"bounded"}')
        right.sendall(multiplex(2, b"private-daemon-output"))
        for piece in (packet[:2], packet[2:8], packet[8:]):
            right.sendall(multiplex(1, piece))
        assert await transport.receive() == b'{"test":"bounded"}'
        assert "private-daemon-output" not in repr(transport)
        await transport.send(b'{"reply":true}')
        assert right.recv(4096) == frame_packet(b'{"reply":true}')
        transport.close()
        assert left.fileno() == -1


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["stderr", "stdout", "length", "stream", "truncated"])
async def test_corrupt_or_excessive_output_is_rejected_and_closed(case, monkeypatch):
    monkeypatch.setattr("metahub_assurance_runner.docker_channel.MAX_STDERR_BYTES", 32)
    with sockets() as (left, right):
        transport = DockerLeaseTransport(left)
        if case == "stderr":
            packet = multiplex(2, b"x" * 33)
        elif case == "stdout":
            packet = multiplex(1, struct.pack("!I", 4096) + b"x" * 4097)
        elif case == "length":
            packet = multiplex(1, struct.pack("!I", 2**32 - 1))
        elif case == "stream":
            packet = multiplex(9, b"invalid")
        else:
            packet = multiplex(1, b"\x00\x00")
        right.sendall(packet)
        right.shutdown(socket.SHUT_WR)
        with pytest.raises(LeaseChannelError):
            await transport.receive()
        assert left.fileno() == -1


@pytest.mark.anyio
async def test_cancellation_shuts_socket_and_unblocks_the_sdk_reader():
    with sockets() as (left, right):
        transport = DockerLeaseTransport(left)
        task = asyncio.create_task(transport.receive())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert left.fileno() == -1
        assert right.recv(1) == b""
        with pytest.raises(LeaseChannelError):
            await transport.send(b"closed")


@pytest.mark.anyio
async def test_socketio_attachment_closes_underlying_socket_too():
    with sockets() as (left, right):
        attachment = left.makefile("rwb", buffering=0)
        transport = DockerLeaseTransport(attachment)
        right.sendall(multiplex(1, frame_packet(b"hello")))
        assert await transport.receive() == b"hello"
        transport.close()
        assert attachment.closed and left.fileno() == -1


def test_failed_close_cannot_be_reclassified_as_success_by_retry():
    with sockets() as (left, _):

        class BrokenAttachment:
            _sock = left

            def close(self):
                raise OSError("private-close-details")

        transport = DockerLeaseTransport(BrokenAttachment())
        for _ in range(2):
            with pytest.raises(LeaseChannelError, match="close_unverified") as caught:
                transport.close()
            assert "private-close-details" not in str(caught.value)
        assert left.fileno() == -1
