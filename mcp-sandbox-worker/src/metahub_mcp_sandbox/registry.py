"""Bounded, observable ownership for active sandbox network resources."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

Closer = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class _Session:
    tasks: set[asyncio.Task[object]] = field(default_factory=set, repr=False)
    graceful_closers: list[Closer] = field(default_factory=list, repr=False)
    force_closers: list[Closer] = field(default_factory=list, repr=False)
    connection_count: int = 0


class SessionRegistry:
    def __init__(self, *, maximum: int) -> None:
        if type(maximum) is not int or maximum < 1:
            raise ValueError("mcp.sandbox_unavailable")
        self._maximum = maximum
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _Session] = {}

    async def register(self, dispatch_id: str) -> None:
        async with self._lock:
            if dispatch_id in self._sessions or len(self._sessions) >= self._maximum:
                raise RuntimeError("mcp.sandbox_unavailable")
            self._sessions[dispatch_id] = _Session()

    async def attach(
        self,
        dispatch_id: str,
        *,
        task: asyncio.Task[object] | None = None,
        graceful_close: Closer | None = None,
        force_close: Closer | None = None,
        connection: bool = False,
    ) -> None:
        async with self._lock:
            session = self._sessions.get(dispatch_id)
            if session is None:
                raise RuntimeError("mcp.sandbox_unavailable")
            if task is not None:
                session.tasks.add(task)
            if graceful_close is not None:
                session.graceful_closers.append(graceful_close)
            if force_close is not None:
                session.force_closers.append(force_close)
            if connection:
                session.connection_count += 1

    async def cleanup(self, dispatch_id: str, *, deadline_seconds: float) -> None:
        async with self._lock:
            session = self._sessions.get(dispatch_id)
        if session is None:
            return
        cleanup_task = asyncio.current_task()

        async def graceful() -> None:
            for closer in reversed(session.graceful_closers):
                try:
                    await closer()
                except Exception:
                    continue
            for task in tuple(session.tasks):
                if task is not cleanup_task and not task.done():
                    task.cancel()
            owned_tasks = tuple(task for task in session.tasks if task is not cleanup_task)
            if owned_tasks:
                await asyncio.gather(*owned_tasks, return_exceptions=True)

        graceful_task = asyncio.create_task(graceful())
        try:
            await asyncio.wait_for(asyncio.shield(graceful_task), timeout=deadline_seconds)
        except (TimeoutError, asyncio.CancelledError):
            graceful_task.cancel()
            await asyncio.gather(graceful_task, return_exceptions=True)
            for task in tuple(session.tasks):
                if task is not cleanup_task:
                    task.cancel()
            for closer in reversed(session.force_closers):
                try:
                    await closer()
                except Exception:
                    continue
        finally:
            async with self._lock:
                self._sessions.pop(dispatch_id, None)

    async def snapshot(self) -> dict[str, int]:
        async with self._lock:
            return {
                "active_sessions": len(self._sessions),
                "active_connections": sum(
                    item.connection_count for item in self._sessions.values()
                ),
                "owned_tasks": sum(len(item.tasks) for item in self._sessions.values()),
            }
