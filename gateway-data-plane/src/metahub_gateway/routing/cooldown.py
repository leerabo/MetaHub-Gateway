"""Short-lived, process-local recovery state for failed upstream routes."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256

RouteKey = tuple[str, str, str]


def scoped_tenant(database_url: str | None, tenant_id: str) -> str:
    """Keep unrelated gateway databases from sharing process-local recovery state."""

    namespace = sha256((database_url or "").encode("utf-8")).hexdigest()[:16]
    return f"{namespace}:{tenant_id}"


@dataclass
class _State:
    failures: int
    retry_at: float
    probe_owner: str | None = None
    probe_until: float = 0.0
    touched_at: float = 0.0


class RouteCooldownRegistry:
    """Defer known failures and admit one recovery probe per route and process."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[RouteKey, _State] = {}
        self._latest_attempts: dict[RouteKey, tuple[float, float]] = {}

    def order(
        self, routes: tuple[RouteKey, ...], request_id: str
    ) -> tuple[tuple[RouteKey, ...], RouteKey | None, tuple[RouteKey, ...]]:
        with self._lock:
            now = self._clock()
            self._prune(now)
            ready: list[RouteKey] = []
            deferred: list[RouteKey] = []
            for key in routes:
                state = self._states.get(key)
                if state is None or (
                    now >= state.retry_at
                    and (state.probe_owner is None or now >= state.probe_until)
                ):
                    ready.append(key)
                else:
                    deferred.append(key)
            ordered = tuple(ready + deferred)
            probe: RouteKey | None = None
            if ordered and ready:
                first = ordered[0]
                state = self._states.get(first)
                if state is not None and now >= state.retry_at:
                    state.probe_owner = request_id
                    state.probe_until = now + 60.0
                    state.touched_at = now
                    probe = first
            return ordered, probe, tuple(deferred)

    def failed(
        self, key: RouteKey, *, request_id: str, base_seconds: int = 30,
        attempt_started: float | None = None,
    ) -> None:
        with self._lock:
            now = self._clock()
            if not self._accept_outcome(key, attempt_started, now):
                return
            previous = self._states.get(key)
            failures = min((previous.failures if previous else 0) + 1, 6)
            delay = min(base_seconds * (2 ** (failures - 1)), 600)
            self._states[key] = _State(failures, now + delay, touched_at=now)

    def succeeded(self, key: RouteKey, *, attempt_started: float | None = None) -> None:
        with self._lock:
            if not self._accept_outcome(key, attempt_started, self._clock()):
                return
            self._states.pop(key, None)

    def inconclusive(self, key: RouteKey, request_id: str) -> None:
        with self._lock:
            state = self._states.get(key)
            if state is not None and state.probe_owner == request_id:
                now = self._clock()
                state.probe_owner = None
                state.probe_until = 0.0
                state.retry_at = max(state.retry_at, now + 30.0)
                state.touched_at = now

    def abandoned(self, key: RouteKey, request_id: str) -> None:
        with self._lock:
            state = self._states.get(key)
            if state is not None and state.probe_owner == request_id:
                state.probe_owner = None
                state.probe_until = 0.0

    def _prune(self, now: float) -> None:
        for key, state in tuple(self._states.items()):
            if now - state.touched_at > 86_400:
                del self._states[key]
        for key, (_, touched_at) in tuple(self._latest_attempts.items()):
            if now - touched_at > 86_400:
                del self._latest_attempts[key]

    def _accept_outcome(
        self, key: RouteKey, attempt_started: float | None, now: float
    ) -> bool:
        started = now if attempt_started is None else attempt_started
        latest = self._latest_attempts.get(key)
        if latest is not None and started < latest[0]:
            return False
        self._latest_attempts[key] = (started, now)
        return True


route_cooldowns = RouteCooldownRegistry()
