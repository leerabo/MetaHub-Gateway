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
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(binding.upstream_host, binding.upstream_port, limit=65536),
                3,
            )
            tasks = [
                asyncio.create_task(self._relay(binding, reader, upstream_writer)),
                asyncio.create_task(self._relay(binding, upstream_reader, writer)),
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
            for stream in (writer, upstream_writer):
                if stream is not None:
                    try:
                        await asyncio.wait_for(stream.wait_closed(), 1)
                    except (OSError, TimeoutError):
                        pass

    async def _may_forward(self, binding: FaultBinding) -> bool:
        while self.running:
            fault = await asyncio.to_thread(self.store.active_fault, binding.binding_code)
            if fault is None:
                return True
            if fault != "dependency-timeout":
                return False
            await asyncio.sleep(0.05)
        return False

    async def _relay(self, binding, reader, writer):
        while await self._may_forward(binding):
            try:
                chunk = await asyncio.wait_for(reader.read(65536), 0.1)
            except TimeoutError:
                continue
            if not await self._may_forward(binding):
                return False
            if not chunk:
                if writer.can_write_eof():
                    writer.write_eof()
                    await writer.drain()
                return True
            writer.write(chunk)
            await writer.drain()
        return False
