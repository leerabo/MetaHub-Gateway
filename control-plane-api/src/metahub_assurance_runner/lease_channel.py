"""Single-use lease checks over an exclusively owned supervisor/guest pipe.

This is not a network authentication protocol. Its authority comes from the
supervisor's verified exact-container attachment, never a guest-chosen address.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Protocol

from metahub_shared.config_distribution import canonical_json_bytes
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

MAX_FRAME_BYTES = 4096
IO_TIMEOUT_SECONDS = 10
GRANT_MILLISECONDS = 1000


def _nonzero(value):
    if value == "0" * 64:
        raise ValueError("missing digest or challenge")
    return value


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$"), AfterValidator(_nonzero)]


class LeaseChannelError(RuntimeError):
    """Fixed protocol failure without frame bodies or transport exceptions."""


class FrameModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class LeaseBinding(FrameModel):
    request_sha256: Digest
    environment_id: str = Field(pattern=r"^aenv_[0-9a-f]{32}$")
    assessment_generation: int = Field(ge=1)
    deadline_at: AwareDatetime
    channel_nonce: Digest

    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.model_dump(mode="json"))).hexdigest()


class Hello(FrameModel):
    kind: Literal["lease.hello"] = "lease.hello"
    binding: LeaseBinding


class Check(FrameModel):
    kind: Literal["lease.check"] = "lease.check"
    binding_sha256: Digest
    sequence: int = Field(ge=1, le=1_000_000)
    challenge: Digest


class Finish(Check):
    kind: Literal["lease.finish"] = "lease.finish"
    report_sha256: Digest


class Decision(FrameModel):
    kind: Literal["lease.decision"] = "lease.decision"
    binding_sha256: Digest
    sequence: int = Field(ge=1, le=1_000_000)
    challenge: Digest
    permitted: bool
    issued_at: AwareDatetime
    expires_at: AwareDatetime


Frame = Annotated[Hello | Check | Finish | Decision, Field(discriminator="kind")]
_FRAME_ADAPTER = TypeAdapter(Frame)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("nonstandard number")


def decode_frame(content: bytes) -> Frame:
    try:
        if type(content) is not bytes or not 0 < len(content) <= MAX_FRAME_BYTES:
            raise ValueError("invalid frame size")
        parsed = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        return _FRAME_ADAPTER.validate_json(json.dumps(parsed))
    except Exception:
        raise LeaseChannelError("lease_channel.invalid_frame") from None


def encode_frame(frame: FrameModel) -> bytes:
    content = canonical_json_bytes(frame.model_dump(mode="json"))
    decode_frame(content)
    return content


def frame_packet(content: bytes) -> bytes:
    if type(content) is not bytes or not 0 < len(content) <= MAX_FRAME_BYTES:
        raise LeaseChannelError("lease_channel.invalid_frame")
    return struct.pack("!I", len(content)) + content


class LeaseTransport(Protocol):
    async def receive(self) -> bytes: ...
    async def send(self, content: bytes) -> None: ...
    def close(self) -> None: ...


@dataclass(slots=True, repr=False)
class StreamLeaseTransport:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def receive(self) -> bytes:
        length = struct.unpack("!I", await self.reader.readexactly(4))[0]
        if not 0 < length <= MAX_FRAME_BYTES:
            raise LeaseChannelError("lease_channel.invalid_frame")
        return await self.reader.readexactly(length)

    async def send(self, content: bytes) -> None:
        self.writer.write(frame_packet(content))
        await self.writer.drain()

    def close(self) -> None:
        self.writer.close()


@dataclass(slots=True, repr=False)
class GuestLease:
    transport: LeaseTransport
    binding: LeaseBinding
    _sequence: int = field(default=0, init=False)
    _fenced: bool = field(default=False, init=False)
    _busy: bool = field(default=False, init=False)

    @classmethod
    async def accept(cls, transport: LeaseTransport):
        try:
            async with asyncio.timeout(IO_TIMEOUT_SECONDS):
                hello = decode_frame(await transport.receive())
            if not isinstance(hello, Hello):
                raise LeaseChannelError("lease_channel.handshake_invalid")
            remaining = (hello.binding.deadline_at - datetime.now(UTC)).total_seconds()
            if not 0 < remaining <= 14_400:
                raise LeaseChannelError("lease_channel.deadline_invalid")
            return cls(transport, hello.binding)
        except asyncio.CancelledError:
            transport.close()
            raise
        except Exception:
            transport.close()
            raise LeaseChannelError("lease_channel.handshake_unavailable") from None

    def close(self):
        self._fenced = True
        self.transport.close()

    async def guard(self) -> bool:
        return await self._exchange(None)

    async def finish(self, report_sha256: str) -> bool:
        result = await self._exchange(report_sha256)
        self.close()
        return result

    async def _exchange(self, report_sha256):
        if self._fenced or self._busy:
            self.close()
            return False
        self._busy = True
        try:
            remaining = (self.binding.deadline_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                self.close()
                return False
            self._sequence += 1
            request = dict(
                binding_sha256=self.binding.digest(),
                sequence=self._sequence,
                challenge=secrets.token_hex(32),
            )
            message = (
                Check(**request)
                if report_sha256 is None
                else Finish(
                    **request,
                    report_sha256=report_sha256,
                )
            )
            started = datetime.now(UTC)
            async with asyncio.timeout(min(IO_TIMEOUT_SECONDS, remaining)):
                await self.transport.send(encode_frame(message))
                response = decode_frame(await self.transport.receive())
            now = datetime.now(UTC)
            if (
                self._fenced
                or not isinstance(response, Decision)
                or response.binding_sha256 != message.binding_sha256
                or response.sequence != message.sequence
                or response.challenge != message.challenge
                or response.permitted is not True
                or not started <= response.issued_at <= now < response.expires_at
                or response.expires_at > self.binding.deadline_at
                or response.expires_at - response.issued_at
                > timedelta(milliseconds=GRANT_MILLISECONDS)
            ):
                self.close()
                return False
            return True
        except asyncio.CancelledError:
            self.close()
            raise
        except Exception:
            self.close()
            return False
        finally:
            self._busy = False


@dataclass(frozen=True, slots=True)
class LeaseSessionResult:
    checks_permitted: int
    report_sha256: str | None
    reason_code: Literal["report_announced", "lease_revoked", "channel_unavailable"]


@dataclass(slots=True, repr=False)
class SupervisorLeaseSession:
    transport: LeaseTransport
    binding: LeaseBinding
    check_lease: Callable[[], Awaitable[bool]]
    on_close: Callable[[], None] | None = field(default=None, repr=False)
    _attempted: bool = field(default=False, init=False)
    _fenced: bool = field(default=False, init=False)

    def close(self):
        first_close = not self._fenced
        self._fenced = True
        try:
            if first_close and self.on_close is not None:
                self.on_close()
        finally:
            self.transport.close()

    async def serve(self) -> LeaseSessionResult:
        if self._attempted or self._fenced:
            raise LeaseChannelError("lease_channel.already_attempted")
        self._attempted = True
        permitted = 0
        sequence = 0
        try:
            remaining = (self.binding.deadline_at - datetime.now(UTC)).total_seconds()
            if not 0 < remaining <= 14_400:
                raise LeaseChannelError("lease_channel.deadline_invalid")
            async with asyncio.timeout(remaining):
                async with asyncio.timeout(IO_TIMEOUT_SECONDS):
                    if await self.check_lease() is not True or self._fenced:
                        return LeaseSessionResult(0, None, "lease_revoked")
                    await self.transport.send(encode_frame(Hello(binding=self.binding)))
                while not self._fenced:
                    async with asyncio.timeout(IO_TIMEOUT_SECONDS):
                        request = decode_frame(await self.transport.receive())
                        if (
                            not isinstance(request, (Check, Finish))
                            or request.binding_sha256 != self.binding.digest()
                            or request.sequence != sequence + 1
                        ):
                            raise LeaseChannelError("lease_channel.request_mismatch")
                        sequence = request.sequence
                        allowed = await self.check_lease() is True
                        now = datetime.now(UTC)
                        allowed = allowed and not self._fenced and now < self.binding.deadline_at
                        decision = Decision(
                            binding_sha256=request.binding_sha256,
                            sequence=sequence,
                            challenge=request.challenge,
                            permitted=allowed,
                            issued_at=now,
                            expires_at=min(
                                now + timedelta(milliseconds=GRANT_MILLISECONDS),
                                self.binding.deadline_at,
                            )
                            if allowed
                            else now,
                        )
                        await self.transport.send(encode_frame(decision))
                    if not allowed:
                        return LeaseSessionResult(permitted, None, "lease_revoked")
                    if isinstance(request, Finish):
                        return LeaseSessionResult(
                            permitted, request.report_sha256, "report_announced"
                        )
                    permitted += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self.close()
        return LeaseSessionResult(permitted, None, "channel_unavailable")
