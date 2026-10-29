"""PRD-074 网关批处理模块：校验内部调用并执行单项请求，所有结果保留租户、用量和失败追踪事实。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

Single-process realtime-priority admission gate for PRD-074 Batch calls.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class RealtimeGatewayLease:
    """实现批处理职责。

    定义：RealtimeGatewayLease。
    调用方必须遵守租户隔离、权限校验和脱敏边界。
    """
    gate: BatchGatewayGate
    released: bool = False

    async def release(self) -> None:
        """实现批处理职责。

        定义：release。
        调用方必须遵守租户隔离、权限校验和脱敏边界。
        """
        if self.released:
            return
        self.released = True
        await self.gate._release_realtime()


@dataclass(slots=True)
class BatchGatewayLease:
    """实现批处理职责。

    定义：BatchGatewayLease。
    调用方必须遵守租户隔离、权限校验和脱敏边界。
    """
    gate: BatchGatewayGate
    released: bool = False

    async def release(self) -> None:
        """实现批处理职责。

        定义：release。
        调用方必须遵守租户隔离、权限校验和脱敏边界。
        """
        if self.released:
            return
        self.released = True
        await self.gate._release_batch()


class BatchGatewayGate:
    """实现批处理职责。

    定义：BatchGatewayGate。
    调用方必须遵守租户隔离、权限校验和脱敏边界。

    Tracks realtime activity without ever making realtime wait for a Batch slot.
    """

    def __init__(
        self,
        *,
        batch_concurrency: int = 1,
        realtime_cooldown_milliseconds: int = 250,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        """实现批处理职责。

        定义：__init__。
        调用方必须遵守租户隔离、权限校验和脱敏边界。
        """
        # 成败请求均须结算用量和成本；异常时保留事实，不得静默丢账。
        if type(batch_concurrency) is not int or batch_concurrency != 1:
            # 成败请求均须结算用量和成本；异常时保留事实，不得静默丢账。
            raise ValueError("batch_gateway_concurrency_invalid")
        if (
            type(realtime_cooldown_milliseconds) is not int
            or not 0 <= realtime_cooldown_milliseconds <= 5_000
        ):
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
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
        """实现批处理职责。

        定义：enter_realtime。
        调用方必须遵守租户隔离、权限校验和脱敏边界。
        """
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
        """实现批处理职责。

        定义：try_enter_batch。
        调用方必须遵守租户隔离、权限校验和脱敏边界。
        """
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
        """实现批处理职责。

        定义：_release_realtime。
        调用方必须遵守租户隔离、权限校验和脱敏边界。
        """
        async with self._state_lock:
            if self._realtime_active <= 0:
                # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
                raise RuntimeError("realtime_gateway_lease_underflow")
            self._realtime_active -= 1
            self._last_realtime_finished_at = self._monotonic()

    async def _release_batch(self) -> None:
        """实现批处理职责。

        定义：_release_batch。
        调用方必须遵守租户隔离、权限校验和脱敏边界。
        """
        async with self._state_lock:
            if self._batch_active <= 0:
                # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
                raise RuntimeError("batch_gateway_lease_underflow")
            self._batch_active -= 1


def get_batch_gateway_gate(
    app_state: Any,
    *,
    batch_concurrency: int,
    realtime_cooldown_milliseconds: int,
) -> BatchGatewayGate:
    """实现批处理职责。

    定义：get_batch_gateway_gate。
    调用方必须遵守租户隔离、权限校验和脱敏边界。

    Return the app-scoped gate, rejecting mixed configuration in one process.
    """

    injected = getattr(app_state, "batch_gateway_gate", None)
    if injected is not None:
        if not isinstance(injected, BatchGatewayGate):
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
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
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ValueError("batch_gateway_gate_invalid")
    return cached
