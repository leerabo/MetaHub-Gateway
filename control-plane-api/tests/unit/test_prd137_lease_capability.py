"""Lease capability concurrency and failure behavior, with controlled callbacks."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from metahub_control.security_assurance.leases import AssuranceLease


@pytest.mark.parametrize("value", [None, False, True, 0, -1, "1", 3])
def test_invalid_renewal_permanently_fences(value):
    calls = []

    def renew(generation):
        calls.append(generation)
        return value

    lease = AssuranceLease(renew)
    assert not lease.check()
    assert not lease.check()
    assert calls == [1]
    assert lease.fencing_generation == 1


def test_failure_and_close_never_reopen_or_expose_callback():
    calls = []

    def renew(generation):
        calls.append(generation)
        raise RuntimeError("private-database-password")

    lease = AssuranceLease(renew)
    assert not lease.check()
    assert not lease.check()
    lease.close()
    assert not lease.check()
    assert calls == [1]
    assert "private-database-password" not in repr(lease)


def test_concurrent_checks_serialize_generation_updates():
    seen = []

    def renew(generation):
        seen.append(generation)
        return generation + 1

    lease = AssuranceLease(renew)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda _: lease.check(), range(20)))
    assert seen == list(range(1, 21))
    assert lease.fencing_generation == 21
    lease.close()
    assert not lease.check()
    assert len(seen) == 20


@pytest.mark.anyio
async def test_async_guard_supports_probe_callback():
    lease = AssuranceLease(lambda generation: generation)
    assert await lease.guard()
    lease.close()
    assert not await lease.guard()


@pytest.mark.anyio
async def test_cancelled_guard_fences_even_while_renewal_finishes():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def renew(generation):
        started.set()
        assert release.wait(timeout=5)
        finished.set()
        return generation + 1

    lease = AssuranceLease(renew)
    task = asyncio.create_task(lease.guard())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    assert not await lease.guard()
