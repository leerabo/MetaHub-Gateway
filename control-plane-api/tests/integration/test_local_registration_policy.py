"""Local signup and platform-only policy changes do not fabricate mailbox proof."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity import registration_policy
from metahub_control.identity.self_service import register_local_account
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError
from test_prd095_platform_iam import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    GLOBAL_CSRF_COOKIE,
    PLATFORM_CSRF_COOKIE,
    RECOVERY_SECRET,
    _csrf,
    _enroll,
    _login,
    _prepare,
    _totp,
    identity_service,
    platform_service,
)
from test_prd095_platform_iam import (
    migrated_database as migrated_database,
)


@pytest.mark.anyio
async def test_settings_are_platform_only_csrf_cas_and_audited(
    monkeypatch, tmp_path, migrated_database
):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    moment = [datetime.now(UTC)]
    monkeypatch.setattr(identity_service, "_utcnow", lambda: moment[0])
    monkeypatch.setattr(platform_service, "_utcnow", lambda: moment[0])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as owner:
        await _login(owner, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert (await owner.get("/platform/registration-policy")).status_code == 401
        secret = await _enroll(owner, ADMIN_PASSWORD, moment[0])
        moment[0] += timedelta(seconds=30)
        boot = await owner.post(
            "/platform/bootstrap",
            json={
                "current_password": ADMIN_PASSWORD,
                "totp_code": _totp(secret, moment[0]),
                "recovery_secret": RECOVERY_SECRET,
                "reason_code": "initial_platform_owner",
                "confirmed": True,
            },
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert boot.status_code == 200, boot.text
        moment[0] += timedelta(seconds=30)
        opened = await owner.post(
            "/platform/sessions",
            json={"totp_code": _totp(secret, moment[0])},
            headers=_csrf(owner, GLOBAL_CSRF_COOKIE),
        )
        assert opened.status_code == 200, opened.text
        app.state.config = app.state.config.model_copy(update={
            "environment": "dev", "identity_challenge_delivery_mode": "disabled",
        })
        current = await owner.get("/platform/registration-policy")
        assert current.status_code == 200, current.text
        body = {"email_verification": False, "human_verification": False, "revision": 1}
        no_csrf = await owner.put(
            "/platform/registration-policy", json=body, headers={"Origin": "http://localhost:5173"}
        )
        assert no_csrf.status_code == 403
        headers = _csrf(owner, PLATFORM_CSRF_COOKIE)
        saved = await owner.put("/platform/registration-policy", json=body, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 2
        public = (await owner.get("/identity/registration-policy")).json()
        assert not public["human_verification"]
        stale = await owner.put("/platform/registration-policy", json=body, headers=headers)
        assert stale.status_code == 412, stale.text
        body.update(revision=2, human_verification=True)
        saved = await owner.put("/platform/registration-policy", json=body, headers=headers)
        assert saved.status_code == 200
        body.update(revision=3, email_verification=True)
        unavailable = await owner.put("/platform/registration-policy", json=body, headers=headers)
        assert unavailable.status_code == 503
        owner.cookies.set("__Host-metahub-workspace-session", "w" * 43)
        conflict = await owner.put("/platform/registration-policy", json=body, headers=headers)
        assert conflict.status_code in (400, 401, 403)
    connection = connect_database(database)
    try:
        assert (
            connection.execute(
                "SELECT count(*) FROM platform_audit_logs WHERE "
                "action_code='registration_policy.updated'"
            ).fetchone()[0]
            == 2
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_local_registration_captcha_is_required_single_use_and_unverified(
    monkeypatch,
    tmp_path,
    migrated_database,
):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    app.state.config = app.state.config.model_copy(update={
        "environment": "dev", "identity_challenge_delivery_mode": "disabled",
    })
    monkeypatch.setattr(registration_policy.secrets, "choice", lambda choices: choices[0])
    headers = {"Origin": "http://localhost:5173", "Sec-Fetch-Site": "same-site"}
    body = {
        "email": "local.signup@example.test",
        "display_name": "Local trial",
        "password": "LocalSignup!2026",
        "consent_version": "privacy-local-2026-09-09",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        policy = (await client.get("/identity/registration-policy")).json()
        assert policy["human_verification"] and not policy["email_verification"]
        missing = await client.post("/identity/registrations", json=body, headers=headers)
        assert missing.status_code == 422
        captcha = await client.get("/identity/registration-captcha")
        assert captcha.status_code == 200, captcha.text
        assert "no-store" in captcha.headers["cache-control"]
        challenge = captcha.json()
        assert challenge["image"].startswith("data:image/png;base64,iVBOR")
        body.update(captcha_id=challenge["challenge_id"], captcha_answer="99999")
        wrong = await client.post("/identity/registrations", json=body, headers=headers)
        assert wrong.status_code == 400
        body["captcha_answer"] = "22222"
        result = await client.post("/identity/registrations", json=body, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["account"]["email"] == body["email"]
        assert "intent_id" not in result.json()
        assert "__Host-metahub-global-session" in result.headers["set-cookie"]
        replay = await client.post("/identity/registrations", json=body, headers=headers)
        assert replay.status_code == 400
        assert (await client.get("/platform/registration-policy")).status_code == 401
        connection = connect_database(database)
        try:
            account_id = connection.execute(
                "SELECT account_id FROM global_accounts WHERE email_normalized=?", (body["email"],)
            ).fetchone()[0]
            assert (
                connection.execute(
                    "SELECT count(*) FROM account_verifications WHERE account_id=?", (account_id,)
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM local_registration_consents WHERE account_id=?",
                    (account_id,),
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM identity_lifecycle_events WHERE "
                    "account_id=? AND reason_code='local_unverified_registration'",
                    (account_id,),
                ).fetchone()[0]
                == 1
            )
        finally:
            connection.close()


@pytest.mark.anyio
async def test_captcha_device_binding_expiry_and_attempt_limit(
    monkeypatch, tmp_path, migrated_database
):
    _, app = _prepare(monkeypatch, tmp_path, migrated_database)
    config = app.state.config
    monkeypatch.setattr(registration_policy.secrets, "choice", lambda choices: choices[0])
    image = registration_policy.create_registration_captcha(
        config,
        device_token="device-a",
        user_agent="test",
        client_host="127.0.0.1",
    )
    with pytest.raises(ApiError, match="图形验证码"):
        registration_policy.consume_registration_captcha(
            config, challenge_id=image["challenge_id"], answer="22222", device_token="device-b"
        )
    for _ in range(5):
        with pytest.raises(ApiError, match="图形验证码"):
            registration_policy.consume_registration_captcha(
                config, challenge_id=image["challenge_id"], answer="99999", device_token="device-a"
            )
    with pytest.raises(ApiError, match="图形验证码"):
        registration_policy.consume_registration_captcha(
            config, challenge_id=image["challenge_id"], answer="22222", device_token="device-a"
        )
    image = registration_policy.create_registration_captcha(
        config,
        device_token="device-c",
        user_agent="test",
        client_host="127.0.0.1",
    )
    connection = connect_database(config.database_url)
    try:
        connection.execute("UPDATE registration_captchas SET expires_at='2000-01-01T00:00:00Z'")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ApiError, match="图形验证码"):
        registration_policy.consume_registration_captcha(
            config, challenge_id=image["challenge_id"], answer="22222", device_token="device-c"
        )


def test_local_signup_rejects_production_and_invitation(monkeypatch, tmp_path, migrated_database):
    _, app = _prepare(monkeypatch, tmp_path, migrated_database)
    fields = dict(
        email="other@example.test",
        display_name="Local",
        password="LocalSignup!2026",
        consent_version="privacy-local-2026-09-09",
        invitation_token=None,
        request_id="request-local-test",
        trace_id="trace-local-test",
        user_agent="test",
        client_host="127.0.0.1",
        device_token="test-device",
    )
    with pytest.raises(ApiError, match="仅用于本地开发"):
        register_local_account(
            app.state.config.model_copy(update={"environment": "prod"}), **fields,
        )
    fields["invitation_token"] = "a" * 43
    with pytest.raises(ApiError, match="仅用于本地开发"):
        register_local_account(app.state.config.model_copy(update={"environment": "dev"}), **fields)
