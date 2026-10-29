"""Bounded delivery adapters for tenant-free identity challenges."""

from __future__ import annotations

import threading
import smtplib
import ssl
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.headerregistry import Address
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


class _SmtpDelivery:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def deliver(self, *, target: str, code: str, purpose: str, expires_at: datetime,
                **_metadata: object) -> None:
        # Do not interpolate untrusted headers or expose intent/challenge IDs.
        try:
            address = Address(addr_spec=target)
            if not address.username or not address.domain or any(ord(c) < 32 for c in target):
                raise ValueError()
            if not re.fullmatch(r"[0-9]{6,10}", code):
                raise ValueError()
            message = EmailMessage()
            message["From"] = self._config.smtp_sender
            message["To"] = target
            message["Subject"] = "MetaHub 验证码 / Verification code"
            message.set_content(
                f"你的 MetaHub 验证码为：{code}\n"
                f"有效期至 {expires_at.isoformat()}。请勿将验证码透露给他人。\n"
                "如果不是你本人发起，请忽略此邮件。\n\n"
                f"Your MetaHub verification code: {code}\n"
                f"Expires at {expires_at.isoformat()}. Do not share this code.\n"
                "If you did not request it, ignore this message.\n"
            )
            context = ssl.create_default_context()
            options = {"host": self._config.smtp_host, "port": self._config.smtp_port,
                       "timeout": self._config.smtp_timeout_seconds}
            if self._config.smtp_security == "tls":
                transport = smtplib.SMTP_SSL(**options, context=context)
            elif self._config.smtp_security == "starttls":
                transport = smtplib.SMTP(**options)
            else:
                raise ValueError()
            with transport as client:
                client.ehlo()
                if self._config.smtp_security == "starttls":
                    client.starttls(context=context)
                    client.ehlo()
                client.login(self._config.smtp_username, self._config.smtp_password)
                refused = client.send_message(message, from_addr=self._config.smtp_sender,
                                              to_addrs=[target])
                if refused:
                    raise ValueError()
        except Exception:
            # SMTP errors can contain recipients, credentials or complete messages.
            # No plaintext retry, debugging output or test-capture fallback.
            raise IdentityChallengeDeliveryError("identity challenge delivery failed") from None


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
    from .smtp_settings import effective_delivery_config
    config = effective_delivery_config(config)
    mode = config.identity_challenge_delivery_mode
    if mode == "test_capture" and config.environment != "prod":
        return _TEST_CAPTURE_DELIVERY
    if mode == "smtp":
        return _SmtpDelivery(config)
    return _UNAVAILABLE_DELIVERY


def pop_test_identity_challenge(
    purpose: str,
    intent_id: str,
) -> CapturedIdentityChallenge | None:
    """Read and remove a challenge only from the non-production test adapter."""

    return _TEST_CAPTURE_DELIVERY.pop(purpose, intent_id)


def clear_test_identity_challenges() -> None:
    _TEST_CAPTURE_DELIVERY.clear()
