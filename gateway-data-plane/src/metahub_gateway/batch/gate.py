"""Single-process realtime-priority admission gate for PRD-074 Batch calls."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class RealtimeGatewayLease:
    gate: BatchGatewayGate
    released: bool = False

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        await self.gate._release_realtime()


@dataclass(slots=True)
class BatchGatewayLease:
    gate: BatchGatewayGate
    released: bool = False

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        await self.gate._release_batch()


class BatchGatewayGate:
    """Tracks realtime activity without ever making realtime wait for a Batch slot."""

    def __init__(
        self,
        *,
        batch_concurrency: int = 1,
        realtime_cooldown_milliseconds: int = 250,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if type(batch_concurrency) is not int or batch_concurrency != 1:
            raise ValueError("batch_gateway_concurrency_invalid")
        if (
            type(realtime_cooldown_milliseconds) is not int
            or not 0 <= realtime_cooldown_milliseconds <= 5_000
        ):
            raise ValueError("batch_gateway_cooldown_invalid")
        self._batch_concurrency = batch_concurrency
        self._cooldown_seconds = realtime_cooldown_milliseconds / 1_000
        self._monotonic = monotonic or time.monotonic
        self._state_lock = asyncio.Lock()
        self._realtime_waiters = 0
        self._realtime_active = 0
        self._batch_active = 0
        self._last_realtime_finished_at: float | None = None

    async def enter_realtime(self) -> RealtimeGatewayLease:
        # Mark the waiter before yielding so Batch cannot jump ahead of a ready request.
        self._realtime_waiters += 1
        try:
            async with self._state_lock:
                self._realtime_waiters -= 1
                self._realtime_active += 1
        except BaseException:
            self._realtime_waiters -= 1
            raise
        return RealtimeGatewayLease(self)

    async def try_enter_batch(self) -> BatchGatewayLease | None:
        async with self._state_lock:
            now = self._monotonic()
            cooling_down = (
                self._last_realtime_finished_at is not None
                and now - self._last_realtime_finished_at < self._cooldown_seconds
            )
            if (
                self._realtime_waiters
                or self._realtime_active
                or cooling_down
                or self._batch_active >= self._batch_concurrency
            ):
                return None
            self._batch_active += 1
            return BatchGatewayLease(self)

    async def _release_realtime(self) -> None:
        async with self._state_lock:
            if self._realtime_active <= 0:
                raise RuntimeError("realtime_gateway_lease_underflow")
            self._realtime_active -= 1
            self._last_realtime_finished_at = self._monotonic()

    async def _release_batch(self) -> None:
        async with self._state_lock:
            if self._batch_active <= 0:
                raise RuntimeError("batch_gateway_lease_underflow")
            self._batch_active -= 1


def get_batch_gateway_gate(
    app_state: Any,
    *,
    batch_concurrency: int,
    realtime_cooldown_milliseconds: int,
) -> BatchGatewayGate:
    """Return the app-scoped gate, rejecting mixed configuration in one process."""

    injected = getattr(app_state, "batch_gateway_gate", None)
    if injected is not None:
        if not isinstance(injected, BatchGatewayGate):
            raise ValueError("batch_gateway_gate_invalid")
        return injected
    cached = getattr(app_state, "_metahub_batch_gateway_gate", None)
    if cached is None:
        cached = BatchGatewayGate(
            batch_concurrency=batch_concurrency,
            realtime_cooldown_milliseconds=realtime_cooldown_milliseconds,
        )
        app_state._metahub_batch_gateway_gate = cached
    if not isinstance(cached, BatchGatewayGate):
        raise ValueError("batch_gateway_gate_invalid")
    return cached
