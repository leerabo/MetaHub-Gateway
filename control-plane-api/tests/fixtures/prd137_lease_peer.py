"""Subprocess test peer, not a deployed assurance guest or a probe executor."""

from __future__ import annotations

import asyncio
import struct
import sys

from metahub_assurance_runner.lease_channel import Check, GuestLease, encode_frame, frame_packet


class TestPipeTransport:
    def __init__(self):
        self.closed = False

    def _read(self):
        header = sys.stdin.buffer.read(4)
        if len(header) != 4:
            raise EOFError
        length = struct.unpack("!I", header)[0]
        if not 0 < length <= 4096:
            raise ValueError("invalid test frame")
        content = sys.stdin.buffer.read(length)
        if len(content) != length:
            raise EOFError
        return content

    async def receive(self):
        return await asyncio.to_thread(self._read)

    async def send(self, content):
        sys.stdout.buffer.write(frame_packet(content))
        sys.stdout.buffer.flush()

    def close(self):
        self.closed = True


async def main():
    transport = TestPipeTransport()
    lease = await GuestLease.accept(transport)
    mode = sys.argv[1]
    if mode == "disconnect":
        return
    if mode == "invalid":
        await transport.send(b'{"kind":"lease.check","sequence":true}')
        return
    if mode == "replay":
        frame = encode_frame(
            Check(binding_sha256=lease.binding.digest(), sequence=1, challenge="5" * 64)
        )
        await transport.send(frame)
        await transport.receive()
        await transport.send(frame)
        return
    for _ in range(3):
        if not await lease.guard():
            return
    await lease.finish("4" * 64)


if __name__ == "__main__":
    asyncio.run(main())
