"""SMTP configuration encryption, authority and delivery boundaries (tiny isolated DB)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from metahub_control.identity import smtp_settings as smtp
from metahub_control.platform_access import service
from metahub_shared.db import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from test_smtp_identity_delivery import send, transport


@pytest.fixture
def store(tmp_path, monkeypatch):
    config = AppConfig.model_construct(
        environment="prod",
        database_url="sqlite:///" + str(tmp_path / "smtp.db"),
        identity_pii_encryption_secret="isolated-encryption-root-49",
    )
    db = connect_database(config.database_url)
    db.execute("CREATE TABLE platform_operators(operator_id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO platform_operators VALUES('operator-test')")
    migration = (
        Path(__file__).resolve().parents[3] / "infra/migrations/0198_platform_smtp_settings.sql"
    )
    db.executescript(migration.read_text())
    db.commit()
    db.close()
    audits, auth = [], []

    def authenticate(connection, cfg, **kwargs):
        auth.append(kwargs)
        return SimpleNamespace(
            operator_id="operator-test", permissions={"platform.operator.manage"}
        )

    monkeypatch.setattr(service, "authenticate_platform_session_in_transaction", authenticate)
    monkeypatch.setattr(service, "_audit", lambda connection, **kwargs: audits.append(kwargs))
    return config, audits, auth


def payload(**changes):
    return (
        dict(
            enabled=True,
            host="smtp.example.invalid",
            port=587,
            security="starttls",
            username="smtp-login",
            password="private-authorization-code",
            sender="security@example.invalid",
            timeout_seconds=10,
            revision=0,
        )
        | changes
    )


def call(config, **kwargs):
    return smtp.platform_smtp_settings(
        config,
        session_token="session",
        csrf_token="csrf",
        request_id="req-test",
        trace_id="trace-test",
        **kwargs,
    )


def test_round_trip_encrypted_no_secret_in_response_or_audit_and_live_delivery(store):
    config, audits, auth = store
    assert call(config)["revision"] == 0
    result = call(config, payload=payload())
    assert result["password_set"] and result["revision"] == 1
    assert "password" not in result
    db = connect_database(config.database_url)
    raw = db.execute("SELECT settings_ciphertext FROM platform_smtp_settings").fetchone()[0]
    db.close()
    for value in ("private-authorization-code", "smtp.example.invalid", "smtp-login"):
        assert value not in raw and value not in json.dumps(audits, default=str)
    assert auth[-1] == {"session_token": "session", "csrf_token": "csrf"}
    effective = smtp.effective_delivery_config(config)
    assert effective.smtp_password == "private-authorization-code"
    assert effective.identity_challenge_delivery_mode == "smtp"
    call(config, payload=payload(revision=1, password="", enabled=False))
    assert smtp.effective_delivery_config(config).identity_challenge_delivery_mode == "disabled"


def test_blank_password_preserves_but_does_not_forward_secret_to_changed_host(store):
    config, _, _ = store
    call(config, payload=payload())
    call(config, payload=payload(revision=1, password="", sender="other@example.invalid"))
    assert smtp.effective_delivery_config(config).smtp_password == payload()["password"]
    with pytest.raises(ApiError) as failure:
        call(config, payload=payload(revision=2, password="", host="different.example.invalid"))
    assert failure.value.code == "platform.request_invalid"
    with pytest.raises(ApiError) as stale:
        call(config, payload=payload(revision=1))
    assert stale.value.code == "platform.revision_conflict"
    assert call(config)["revision"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"port": True},
        {"security": []},
        {"sender": "a\nb@example.invalid"},
        {"host": "https://smtp.example.invalid"},
        {"timeout_seconds": 31},
        {"enabled": "true"},
    ],
)
def test_invalid_input_does_not_persist(store, change):
    config, _, _ = store
    with pytest.raises(ApiError) as failure:
        call(config, payload=payload(**change))
    assert failure.value.code == "platform.request_invalid"
    assert call(config)["revision"] == 0


def test_ciphertext_tampering_or_environment_change_fails_closed(store):
    config, _, _ = store
    encrypted = smtp._encrypt(config, payload())
    for changed, value in (
        (config.model_copy(update={"environment": "dev"}), encrypted),
        (config, encrypted[:-4] + "AAAA"),
    ):
        with pytest.raises(ApiError) as failure:
            smtp._decrypt(changed, value)
        assert failure.value.code == "identity.delivery_unavailable"


def test_permission_checked_before_settings_are_read(store, monkeypatch):
    config, _, _ = store
    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: SimpleNamespace(permissions={"platform.audit.read"}),
    )
    with pytest.raises(ApiError) as failure:
        call(config)
    assert failure.value.code == "platform.permission_denied"


def test_real_challenge_transport_and_registration_policy_read_web_configuration(
    store, monkeypatch
):
    from metahub_control.identity.delivery import IdentityChallengeDeliveryError
    from metahub_control.identity.registration_policy import _delivery_available

    config, _, _ = store
    events = []
    monkeypatch.setattr(smtp.smtplib, "SMTP", transport(events))
    assert not _delivery_available(config)
    call(config, payload=payload())
    assert _delivery_available(config)
    send(config)
    assert events[-2][0] == "message"
    call(config, payload=payload(revision=1, enabled=False, password=""))
    assert not _delivery_available(config)
    with pytest.raises(IdentityChallengeDeliveryError):
        send(config)


def test_saved_configuration_tests_tls_order_no_email_for_connection_and_throttle(
    store, monkeypatch
):
    config, audits, _ = store
    events = []
    monkeypatch.setattr(smtp.smtplib, "SMTP", transport(events))
    call(config, payload=payload())
    result = call(config, test={"revision": 1, "kind": "connection"})
    assert result["test"]["status"] == "passed"
    assert [e[0] for e in events] == ["connect", "ehlo", "tls", "ehlo", "login", "close"]
    with pytest.raises(ApiError) as failure:
        call(config, test={"revision": 1, "kind": "connection"})
    assert failure.value.code == "identity.rate_limited"
    events.clear()
    result = call(
        config, test={"revision": 1, "kind": "email", "recipient": "recipient@example.invalid"}
    )
    assert result["test"]["status"] == "passed" and events[-2][0] == "message"
    assert "recipient@example.invalid" not in json.dumps(audits, default=str)


def test_tls_failure_redacted_and_never_authenticates(monkeypatch):
    events = []
    monkeypatch.setattr(smtp.smtplib, "SMTP", transport(events, fail_tls=True))
    result = smtp._test_smtp(payload(), "email", "recipient@example.invalid")
    assert result["status"] == "failed" and result["stage"] == "tls"
    assert "login" not in [e[0] for e in events]
    assert "sensitive" not in str(result) and "recipient@" not in str(result)


def test_http_cookie_origin_csrf_and_strict_payload_boundaries(store):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from httpx import ASGITransport, AsyncClient
    from metahub_control.platform_access.routes import router

    config, _, _ = store
    app = FastAPI()
    app.state.config = config.model_copy(update={"allowed_origins": ["https://console.test"]})
    app.include_router(router)

    @app.exception_handler(ApiError)
    async def error_handler(request: Request, error: ApiError):
        return JSONResponse({"code": error.code}, status_code=400)

    async def run():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://console.test"
        ) as client:
            assert (await client.get("/platform/mail-settings")).json()[
                "code"
            ] == "platform.session_required"
            client.cookies.set("__Host-metahub-platform-session", "s" * 43)
            client.cookies.set("__Host-metahub-platform-csrf", "c" * 43)
            headers = {"Origin": "https://console.test", "X-CSRF-Token": "c" * 43}
            missing = await client.put(
                "/platform/mail-settings",
                json=payload(),
                headers={"Origin": "https://console.test"},
            )
            assert missing.json()["code"] == "platform.csrf_invalid"
            cross = await client.put(
                "/platform/mail-settings",
                json=payload(),
                headers=headers | {"Origin": "https://other.test"},
            )
            assert cross.json()["code"] == "platform.origin_invalid"
            extra = await client.put(
                "/platform/mail-settings", json=payload(unknown="data"), headers=headers
            )
            assert extra.json()["code"] == "platform.request_invalid"
            saved = await client.put("/platform/mail-settings", json=payload(), headers=headers)
            assert saved.status_code == 200 and saved.json()["revision"] == 1
            assert "no-store" in saved.headers["cache-control"]
            assert "private-authorization-code" not in saved.text
            client.cookies.set("__Host-metahub-workspace-session", "w" * 43)
            conflict = await client.get("/platform/mail-settings")
            assert conflict.json()["code"] == "platform.credential_conflict"

    asyncio.run(run())
