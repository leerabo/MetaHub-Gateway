"""TLS SMTP ordering and fail-closed delivery, with no external messages."""
from datetime import UTC, datetime, timedelta
import ssl
from types import SimpleNamespace

import pytest
from metahub_control.identity import delivery


def config(**updates):
    values = dict(environment="prod", identity_challenge_delivery_mode="smtp", smtp_host="mail.example.invalid",
                  smtp_port=587, smtp_security="starttls", smtp_username="independent-smtp-user",
                  smtp_password="independent-smtp-password", smtp_sender="security@example.invalid",
                  smtp_timeout_seconds=10)
    return SimpleNamespace(**(values | updates))


def send(settings):
    delivery.identity_challenge_delivery(settings).deliver(
        target="recipient@example.invalid", code="735491", purpose="registration",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        intent_id="private-intent", challenge_id="private-challenge", generation=1)


def transport(events, *, fail_tls=False, refused=False):
    class Client:
        def __init__(self, **kwargs):
            events.append(("connect", kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append(("close", None))

        def ehlo(self):
            events.append(("ehlo", None))

        def starttls(self, *, context):
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            events.append(("tls", None))
            if fail_tls:
                raise RuntimeError("sensitive SMTP server reply recipient@example.invalid 735491")

        def login(self, username, password):
            events.append(("login", (username, password)))

        def send_message(self, message, **kwargs):
            events.append(("message", (message, kwargs)))
            return {"recipient@example.invalid": (550, "rejected")} if refused else {}
    return Client


def test_starttls_authenticates_only_after_verified_tls(monkeypatch):
    events = []
    monkeypatch.setattr(delivery.smtplib, "SMTP", transport(events))
    send(config())
    assert [e[0] for e in events] == ["connect", "ehlo", "tls", "ehlo", "login", "message", "close"]
    message, envelope = events[-2][1]
    assert envelope["to_addrs"] == ["recipient@example.invalid"]
    assert "735491" in message.get_content()
    assert "private-intent" not in str(message) and "private-challenge" not in str(message)


def test_implicit_tls_uses_verified_context(monkeypatch):
    events = []
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", transport(events))
    send(config(smtp_security="tls", smtp_port=465))
    context = events[0][1]["context"]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert [e[0] for e in events] == ["connect", "ehlo", "login", "message", "close"]


def test_tls_failure_never_sends_credentials_or_challenge(monkeypatch):
    events = []
    monkeypatch.setattr(delivery.smtplib, "SMTP", transport(events, fail_tls=True))
    with pytest.raises(delivery.IdentityChallengeDeliveryError) as failed:
        send(config())
    assert "login" not in [e[0] for e in events] and "message" not in [e[0] for e in events]
    assert str(failed.value) == "identity challenge delivery failed"
    assert delivery.pop_test_identity_challenge("registration", "private-intent") is None


def test_refused_recipient_is_delivery_failure(monkeypatch):
    events = []
    monkeypatch.setattr(delivery.smtplib, "SMTP", transport(events, refused=True))
    with pytest.raises(delivery.IdentityChallengeDeliveryError):
        send(config())


def test_production_test_capture_stays_unavailable():
    with pytest.raises(delivery.IdentityChallengeDeliveryError):
        send(config(identity_challenge_delivery_mode="test_capture"))
