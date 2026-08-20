"""Bounded delivery adapters for tenant-free identity challenges."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from metahub_shared.runtime.config import AppConfig


class IdentityChallengeDeliveryError(RuntimeError):
    """Raised without embedding the target address or challenge value."""


class IdentityChallengeDelivery(Protocol):
    def deliver(
        self,
        *,
        target: str,
        code: str,
        purpose: str,
        intent_id: str,
        challenge_id: str,
        generation: int,
        expires_at: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CapturedIdentityChallenge:
    purpose: str
    intent_id: str
    challenge_id: str
    generation: int
    expires_at: datetime
    target: str = field(repr=False)
    code: str = field(repr=False)


class _UnavailableDelivery:
    def deliver(self, **_kwargs: object) -> None:
        raise IdentityChallengeDeliveryError("identity challenge delivery is unavailable")


class _TestCaptureDelivery:
    _MAX_CAPTURED = 512

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._captured: OrderedDict[tuple[str, str], CapturedIdentityChallenge] = OrderedDict()

    def deliver(
        self,
        *,
        target: str,
        code: str,
        purpose: str,
        intent_id: str,
        challenge_id: str,
        generation: int,
        expires_at: datetime,
    ) -> None:
        challenge = CapturedIdentityChallenge(
            target=target,
            code=code,
            purpose=purpose,
            intent_id=intent_id,
            challenge_id=challenge_id,
            generation=generation,
            expires_at=expires_at,
        )
        key = (purpose, intent_id)
        with self._lock:
            self._captured[key] = challenge
            self._captured.move_to_end(key)
            while len(self._captured) > self._MAX_CAPTURED:
                self._captured.popitem(last=False)

    def pop(self, purpose: str, intent_id: str) -> CapturedIdentityChallenge | None:
        with self._lock:
            return self._captured.pop((purpose, intent_id), None)

    def clear(self) -> None:
        with self._lock:
            self._captured.clear()


_UNAVAILABLE_DELIVERY = _UnavailableDelivery()
_TEST_CAPTURE_DELIVERY = _TestCaptureDelivery()


def identity_challenge_delivery(config: AppConfig) -> IdentityChallengeDelivery:
    mode = config.identity_challenge_delivery_mode
    if mode == "test_capture" and config.environment != "prod":
        return _TEST_CAPTURE_DELIVERY
    return _UNAVAILABLE_DELIVERY


def pop_test_identity_challenge(
    purpose: str,
    intent_id: str,
) -> CapturedIdentityChallenge | None:
    """Read and remove a challenge only from the non-production test adapter."""

    return _TEST_CAPTURE_DELIVERY.pop(purpose, intent_id)


def clear_test_identity_challenges() -> None:
    _TEST_CAPTURE_DELIVERY.clear()
