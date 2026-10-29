"""Coordinator-owned lease capability; database authority never enters the worker payload."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

LEASE_SECONDS = 30
RENEW_BEFORE_SECONDS = 15
START_TIMEOUT_SECONDS = 30


@dataclass(slots=True, repr=False)
class AssuranceLease:
    _renew: Callable[[int], int] = field(repr=False)
    _generation: int = field(default=1, init=False)
    _fenced: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def fencing_generation(self) -> int:
        with self._lock:
            return self._generation

    def check(self) -> bool:
        with self._lock:
            if self._fenced.is_set():
                return False
            try:
                generation = self._renew(self._generation)
                if type(generation) is not int or generation not in {
                    self._generation, self._generation + 1,
                }:
                    raise ValueError("invalid lease response")
            except Exception:
                self._fenced.set()
                return False
            self._generation = generation
            return not self._fenced.is_set()

    async def guard(self) -> bool:
        try:
            return await asyncio.to_thread(self.check)
        except asyncio.CancelledError:
            self.close()
            raise

    def close(self) -> None:
        self._fenced.set()
