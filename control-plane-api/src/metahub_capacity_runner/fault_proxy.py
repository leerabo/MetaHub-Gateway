"""Bounded TCP forwarding with independently expiring, closed fault modes."""

from __future__ import annotations

import asyncio
import sqlite3

from filelock import FileLock, Timeout

from .fault_contracts import FaultBinding, FaultProxySettings
from .fault_store import FaultLeaseStore


class FaultProxy:
    def __init__(self, settings: FaultProxySettings):
        self.settings = settings
        self.store = FaultLeaseStore(settings.state_directory)
        self.lock = FileLock(self.store.lock_path, timeout=0, thread_local=False)
        self.servers: list[asyncio.Server] = []
        self.connections: dict[str, set[asyncio.Task]] = {
            binding.binding_code: set() for binding in settings.bindings
        }
        self.running = False

    async def start(self):
        if self.running or self.lock.is_locked:
            raise ValueError("fault.proxy_busy")
        try:
            self.lock.acquire()
        except Timeout:
            raise ValueError("fault.proxy_busy") from None
        try:
            self.store.interrupt()
            for binding in self.settings.bindings:
                self.servers.append(
                    await asyncio.start_server(
                        lambda reader, writer, target=binding: self._accept(target, reader, writer),
                        binding.listen_host,
                        binding.listen_port,
                        limit=65536,
                    )
                )
            self.running = True
        except BaseException:
            await self.close()
            raise

    async def close(self):
        self.running = False
        for server in self.servers:
            server.close()
        for server in self.servers:
            await server.wait_closed()
        self.servers.clear()
        tasks = [task for group in self.connections.values() for task in group]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self.lock.is_locked:
                self.store.interrupt()
        finally:
            self.lock.release()

    def command(self, command):
        with self.store.mutex:
            if not self.running:
                raise ValueError("fault.proxy_unavailable")
            binding = self.settings.binding(command.binding_code, command.execution)
            return self.store.command(command, binding)

    def _accept(self, binding, reader, writer):
        active = self.connections[binding.binding_code]
        if not self.running or len(active) >= binding.max_connections:
            writer.close()
            return
        task = asyncio.create_task(self._forward(binding, reader, writer))
        active.add(task)
        task.add_done_callback(active.discard)

    async def _forward(self, binding, reader, writer):
        upstream_writer = None
        tasks = []
        seen = set()
        effect_lock = asyncio.Lock()
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(binding.upstream_host, binding.upstream_port, limit=65536),
                3,
            )
            tasks = [
                asyncio.create_task(
                    self._relay(binding, reader, upstream_writer, seen, effect_lock)
                ),
                asyncio.create_task(
                    self._relay(binding, upstream_reader, writer, seen, effect_lock)
                ),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if all(task.result() is True for task in done):
                await asyncio.gather(*pending)
        except (OSError, TimeoutError, sqlite3.Error, ValueError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for stream in (writer, upstream_writer):
                if stream is not None:
                    stream.close()
            closed = True
            for stream in (writer, upstream_writer):
                if stream is not None:
                    closed = await _confirm_closed(stream) and closed
            if seen and closed:
                try:
                    await asyncio.to_thread(self.store.detach_effects, seen)
                except sqlite3.Error:
                    # Keep the durable count nonzero until a later lock-owner restart reconciles it.
                    pass

    async def _attach(self, object_id, reset, seen, lock):
        async with lock:
            if object_id in seen:
                return True
            task = asyncio.create_task(
                asyncio.to_thread(self.store.attach_effect, object_id, reset=reset)
            )
            try:
                accepted = await asyncio.shield(task)
            except asyncio.CancelledError:
                if await task:
                    seen.add(object_id)
                raise
            if accepted:
                seen.add(object_id)
            return accepted

    async def _may_forward(
        self, binding: FaultBinding, seen: set, lock, buffered: int | None = None
    ) -> bool:
        reported = set()
        while self.running:
            effect = await asyncio.to_thread(self.store.active_effect, binding.binding_code)
            if effect is None:
                return not reported
            object_id, fault = effect
            if reported and object_id not in reported:
                return False
            if fault != "dependency-timeout":
                await self._attach(object_id, True, seen, lock)
                return False
            if buffered is None:
                return True
            if buffered and object_id not in reported:
                if not await self._attach(object_id, False, seen, lock):
                    return False
                reported.add(object_id)
                await asyncio.to_thread(
                    self.store.observe_effect, object_id, reset=False, withheld=buffered
                )
            await asyncio.sleep(0.05)
        return False

    async def _relay(self, binding, reader, writer, seen, lock):
        while await self._may_forward(binding, seen, lock):
            try:
                chunk = await asyncio.wait_for(reader.read(65536), 0.1)
            except TimeoutError:
                continue
            if not await self._may_forward(binding, seen, lock, len(chunk)):
                return False
            if not chunk:
                if writer.can_write_eof():
                    writer.write_eof()
                    await writer.drain()
                return True
            writer.write(chunk)
            await writer.drain()
        return False


async def _confirm_closed(stream):
    waiter = asyncio.create_task(stream.wait_closed())
    try:
        for _attempt in range(2):
            try:
                await asyncio.wait_for(asyncio.shield(waiter), 1)
                return True
            except TimeoutError:
                stream.transport.abort()
            except OSError:
                return True  # connection_lost completed with a socket error.
        return False
    finally:
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
