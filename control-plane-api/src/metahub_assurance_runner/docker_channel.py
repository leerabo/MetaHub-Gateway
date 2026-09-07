"""Bounded Docker attach transport using the SDK's multiplexed-stream decoder."""

from __future__ import annotations

import asyncio
import socket
import struct
from dataclasses import dataclass, field

from docker.utils.socket import frames_iter

from .lease_channel import MAX_FRAME_BYTES, LeaseChannelError, frame_packet

MAX_STDERR_BYTES = 1_048_576


class _BoundedSocket:
    def __init__(self, sock):
        self.sock = sock

    def fileno(self):
        return self.sock.fileno()

    def recv(self, count):
        return self.sock.recv(min(count, MAX_FRAME_BYTES))


@dataclass(slots=True, repr=False)
class DockerLeaseTransport:
    attachment: object
    _socket: object = field(init=False, repr=False)
    _frames: object = field(init=False, repr=False)
    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _stderr_bytes: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)
    _close_failed: bool = field(default=False, init=False)

    def __post_init__(self):
        self._socket = getattr(self.attachment, "_sock", self.attachment)
        self._frames = frames_iter(_BoundedSocket(self._socket), tty=False)

    def _receive(self):
        while not self._closed:
            if len(self._buffer) >= 4:
                length = struct.unpack("!I", self._buffer[:4])[0]
                if not 0 < length <= MAX_FRAME_BYTES:
                    raise LeaseChannelError("lease_channel.invalid_frame")
                if len(self._buffer) >= 4 + length:
                    content = bytes(self._buffer[4 : 4 + length])
                    del self._buffer[: 4 + length]
                    return content
            try:
                stream, content = next(self._frames)
            except StopIteration:
                raise LeaseChannelError("lease_channel.closed") from None
            if stream == 2:
                self._stderr_bytes += len(content)
                if self._stderr_bytes > MAX_STDERR_BYTES:
                    raise LeaseChannelError("lease_channel.output_limit")
            elif stream == 1:
                self._buffer.extend(content)
                if len(self._buffer) > MAX_FRAME_BYTES + 4:
                    raise LeaseChannelError("lease_channel.output_limit")
            else:
                raise LeaseChannelError("lease_channel.invalid_stream")
        raise LeaseChannelError("lease_channel.closed")

    async def receive(self) -> bytes:
        try:
            return await asyncio.to_thread(self._receive)
        except asyncio.CancelledError:
            self.close()
            raise
        except Exception:
            self.close()
            raise LeaseChannelError("lease_channel.transport_unavailable") from None

    async def send(self, content: bytes) -> None:
        try:
            if self._closed:
                raise LeaseChannelError("lease_channel.closed")
            await asyncio.to_thread(self._socket.sendall, frame_packet(content))
        except asyncio.CancelledError:
            self.close()
            raise
        except Exception:
            self.close()
            raise LeaseChannelError("lease_channel.transport_unavailable") from None

    def close(self) -> None:
        if self._closed:
            if self._close_failed:
                raise LeaseChannelError("lease_channel.close_unverified")
            return
        self._closed = True
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            for resource in (self._socket, self.attachment):
                try:
                    resource.close()
                except Exception:
                    self._close_failed = True
        if self._close_failed:
            raise LeaseChannelError("lease_channel.close_unverified") from None
