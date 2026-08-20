"""PRD-092 self-service Global Identity API and security contracts."""

from __future__ import annotations

import asyncio
import math
import shutil
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.delivery import (
    clear_test_identity_challenges,
    pop_test_identity_challenge,
)
from metahub_control.identity.routes import router as identity_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.db.security import hash_token
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ADMIN_PASSWORD = "LocalAdmin!2026"
NEW_ACCOUNT_PASSWORD = "NewAccount!2026"
RESET_PASSWORD = "ResetAccount!2026"
INTERMEDIATE_PASSWORD = "Intermediate!2026"


@pytest.fixture(scope="module")
def migrated_identity_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd092-api-migrated")
    database = root / "identity-0084.sqlite3"
    result = apply_migrations(f"sqlite:///{database.as_posix()}")
    assert result.applied[-1] == "0084"
    return database


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
    *,
    delivery_mode: str = "test_capture",
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "identity.sqlite3"
    shutil.copy2(migrated_identity_database, database)
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-legacy-auth-token-secret")
    monkeypatch.setenv(
        "METAHUB_IDENTITY_SESSION_SECRET",
        "test-independent-global-identity-session-secret-2026",
    )
    monkeypatch.setenv(
        "METAHUB_IDENTITY_CHALLENGE_SECRET",
        "test-independent-global-identity-challenge-secret-2026",
    )
    monkeypatch.setenv(
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET",
        "test-independent-global-identity-pii-encryption-secret-2026",
    )
    monkeypatch.setenv("METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE", delivery_mode)
    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", ORIGIN)
    apply_seed_data(database_url)
    clear_test_identity_challenges()
    app = create_service_app(
        "control-plane-api",
        "PRD-092 Identity Test",
        extra_cors_allow_headers=("X-CSRF-Token",),
        cors_allow_credentials=True,
    )
    app.include_router(identity_router)
    return database_url, app


def _write_headers() -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"}


def _database_artifact_bytes(database_url: str) -> bytes:
    database = Path(database_url.removeprefix("sqlite:///"))
    artifacts = (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
    return b"".join(path.read_bytes() for path in artifacts if path.exists())


async def _register(
    client: AsyncClient,
    *,
    email: str,
    password: str,
    invitation_token: str | None = None,
):
    payload = {
        "email": email,
        "display_name": "New Owner",
        "password": password,
        "consent_version": "privacy-2026-08",
    }
    if invitation_token is not None:
        payload["invitation_token"] = invitation_token
    return await client.post(
        "/identity/registrations",
        json=payload,
        headers=_write_headers(),
    )


@pytest.mark.anyio
async def test_new_registration_verifies_global_account_without_workspace_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        started = await _register(
            client,
            email="new.owner@example.com",
            password=NEW_ACCOUNT_PASSWORD,
        )
        assert started.status_code == 200
        payload = started.json()
        assert set(payload) == {
            "intent_id",
            "purpose",
            "status",
            "expires_at",
            "resend_available_at",
            "request_id",
            "trace_id",
        }
        assert payload["purpose"] == "registration"
        assert payload["status"] == "pending"
        assert "code" not in started.text
        assert "workspace" not in started.text and "tenant" not in started.text
        assert "__Host-metahub-identity-device" in started.headers["set-cookie"]

        captured = pop_test_identity_challenge("registration", payload["intent_id"])
        assert captured is not None
        assert captured.target == "new.owner@example.com"
        verified = await client.post(
            "/identity/verifications",
            json={"intent_id": payload["intent_id"], "code": captured.code},
            headers=_write_headers(),
        )
        assert verified.status_code == 200
        assert verified.json()["account"]["email"] == "new.owner@example.com"
        assert "account_id" not in verified.text
        assert "__Host-metahub-global-session" in verified.headers["set-cookie"]
        assert (await client.get("/identity/me")).status_code == 200

    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM global_accounts").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM account_verifications").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM consent_records").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM identity_lifecycle_events").fetchone()[0]
            == 2
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM identity_lifecycle_outbox"
        ).fetchone()[0] == 2
        delivery_event = connection.execute(
            """
            SELECT account_id,event_type,reason_code,payload_json
            FROM identity_lifecycle_events
            WHERE event_type='identity.challenge_dispatched'
            """
        ).fetchone()
        assert delivery_event is not None
        assert tuple(delivery_event[:3]) == (
            None,
            "identity.challenge_dispatched",
            "delivery_adapter_accepted",
        )
        assert "new.owner@example.com" not in delivery_event["payload_json"]
        assert captured.code not in delivery_event["payload_json"]
        sensitive_dump = "\n".join(
            str(value)
            for table in (
                "registration_intents",
                "verification_challenges",
                "account_verifications",
                "identity_lifecycle_events",
                "identity_lifecycle_outbox",
            )
            for row in connection.execute(f"SELECT * FROM {table}")
            for value in tuple(row)
        )
        assert NEW_ACCOUNT_PASSWORD not in sensitive_dump
        assert captured.code not in sensitive_dump
        assert "new.owner@example.com" not in sensitive_dump
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_existing_account_requires_password_and_email_proofs_without_merging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        wrong = await _register(
            client,
            email="admin@metahub.local",
            password="WrongAdmin!2026",
        )
        assert wrong.status_code == 200
        wrong_challenge = pop_test_identity_challenge(
            "registration",
            wrong.json()["intent_id"],
        )
        assert wrong_challenge is not None
        rejected = await client.post(
            "/identity/verifications",
            json={"intent_id": wrong.json()["intent_id"], "code": wrong_challenge.code},
            headers=_write_headers(),
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "identity.verification_invalid"
        assert "global-session" not in rejected.headers.get("set-cookie", "")

        correct = await _register(
            client,
            email="ADMIN@METAHUB.LOCAL",
            password=ADMIN_PASSWORD,
        )
        assert correct.status_code == 200
        challenge = pop_test_identity_challenge("registration", correct.json()["intent_id"])
        assert challenge is not None
        verified = await client.post(
            "/identity/verifications",
            json={"intent_id": correct.json()["intent_id"], "code": challenge.code},
            headers=_write_headers(),
        )
        assert verified.status_code == 200

    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM global_accounts").fetchone()[0] == 1
        kinds = [
            row[0]
            for row in connection.execute(
                "SELECT intent_kind FROM registration_intents ORDER BY created_at"
            )
        ]
        assert kinds == ["sink", "existing_account"]
        assert connection.execute("SELECT COUNT(*) FROM account_identity_links").fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.anyio
async def test_verified_invitation_records_binding_without_membership_or_consumption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    raw_token = "I" * 43
    invitation_id = "inv_" + "1" * 32
    invited_email = "invited.owner@example.com"
    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            "SELECT tenant_id,user_id FROM users WHERE email='admin@metahub.local'"
        ).fetchone()
        assert owner is not None
        connection.execute(
            """
            INSERT INTO invitations(
              invitation_id,tenant_id,email,email_fingerprint,role_code,token_hash,status,
              invited_by,accepted_user_id,expires_at,accepted_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'pending',?,NULL,'2026-08-24T00:00:00Z',NULL,
              '2026-08-21T00:00:00Z','2026-08-21T00:00:00Z')
            """,
            (
                invitation_id,
                owner["tenant_id"],
                invited_email,
                "f" * 64,
                "developer",
                hash_token(raw_token),
                owner["user_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        started = await _register(
            client,
            email=invited_email,
            password=NEW_ACCOUNT_PASSWORD,
            invitation_token=raw_token,
        )
        assert started.status_code == 200
        challenge = pop_test_identity_challenge(
            "registration",
            started.json()["intent_id"],
        )
        assert challenge is not None
        verified = await client.post(
            "/identity/verifications",
            json={"intent_id": started.json()["intent_id"], "code": challenge.code},
            headers=_write_headers(),
        )
        assert verified.status_code == 200

    connection = connect_database(database_url)
    try:
        invitation = connection.execute(
            "SELECT status,accepted_user_id,accepted_at FROM invitations WHERE invitation_id=?",
            (invitation_id,),
        ).fetchone()
        assert invitation is not None
        assert tuple(invitation) == ("pending", None, None)
        binding = connection.execute(
            """
            SELECT invitation_id,tenant_id,account_id,registration_intent_id,
              email_fingerprint,status,revision
            FROM invitation_identity_bindings
            """
        ).fetchone()
        assert binding is not None
        assert binding["invitation_id"] == invitation_id
        assert binding["tenant_id"] == owner["tenant_id"]
        assert binding["registration_intent_id"] == started.json()["intent_id"]
        assert binding["email_fingerprint"] == "f" * 64
        assert binding["status"] == "verified_pending_membership"
        assert binding["revision"] == 1
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' "
            "AND name='workspace_memberships'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.anyio
async def test_password_reset_revokes_old_session_and_rotates_legacy_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as old_client:
        login = await old_client.post(
            "/identity/login",
            json={"email": "admin@metahub.local", "password": ADMIN_PASSWORD},
            headers=_write_headers(),
        )
        assert login.status_code == 200
        async with AsyncClient(transport=transport, base_url="https://testserver") as reset_client:
            started = await reset_client.post(
                "/identity/password-resets",
                json={"email": "admin@metahub.local"},
                headers=_write_headers(),
            )
            assert started.status_code == 200
            challenge = pop_test_identity_challenge(
                "password_reset",
                started.json()["intent_id"],
            )
            assert challenge is not None
            csrf = old_client.cookies["__Host-metahub-global-csrf"]
            rotated = await old_client.post(
                "/identity/credentials/password/rotate",
                json={
                    "current_password": ADMIN_PASSWORD,
                    "new_password": INTERMEDIATE_PASSWORD,
                },
                headers={**_write_headers(), "X-CSRF-Token": csrf},
            )
            assert rotated.status_code == 200
            stale = await reset_client.post(
                "/identity/password-resets/confirm",
                json={
                    "intent_id": started.json()["intent_id"],
                    "code": challenge.code,
                    "new_password": RESET_PASSWORD,
                },
                headers=_write_headers(),
            )
            assert stale.status_code == 400

            started = await reset_client.post(
                "/identity/password-resets",
                json={"email": "admin@metahub.local"},
                headers=_write_headers(),
            )
            challenge = pop_test_identity_challenge(
                "password_reset",
                started.json()["intent_id"],
            )
            assert challenge is not None
            confirmed = await reset_client.post(
                "/identity/password-resets/confirm",
                json={
                    "intent_id": started.json()["intent_id"],
                    "code": challenge.code,
                    "new_password": RESET_PASSWORD,
                },
                headers=_write_headers(),
            )
            assert confirmed.status_code == 200
            assert (await reset_client.get("/identity/me")).status_code == 200
        old_me = await old_client.get("/identity/me")
        assert old_me.status_code == 401
        assert old_me.json()["code"] == "identity.session_revoked"

        old_password = await old_client.post(
            "/identity/login",
            json={"email": "admin@metahub.local", "password": ADMIN_PASSWORD},
            headers=_write_headers(),
        )
        new_password = await old_client.post(
            "/identity/login",
            json={"email": "admin@metahub.local", "password": RESET_PASSWORD},
            headers=_write_headers(),
        )
        assert old_password.status_code == 401
        assert new_password.status_code == 200

    connection = connect_database(database_url)
    try:
        statuses = [
            row[0]
            for row in connection.execute(
                "SELECT status FROM password_reset_intents ORDER BY created_at"
            )
        ]
        assert statuses == ["sink_consumed", "completed"]
        assert connection.execute(
            "SELECT COUNT(*) FROM global_sessions WHERE status='revoked'"
        ).fetchone()[0] >= 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_unknown_reset_and_missing_delivery_fail_closed_without_enumeration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        unknown = await client.post(
            "/identity/password-resets",
            json={"email": "unknown@example.com"},
            headers=_write_headers(),
        )
        assert unknown.status_code == 200
        challenge = pop_test_identity_challenge(
            "password_reset",
            unknown.json()["intent_id"],
        )
        assert challenge is not None
        rejected = await client.post(
            "/identity/password-resets/confirm",
            json={
                "intent_id": unknown.json()["intent_id"],
                "code": challenge.code,
                "new_password": RESET_PASSWORD,
            },
            headers=_write_headers(),
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "identity.verification_invalid"

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            "SELECT intent_kind,status FROM password_reset_intents"
        ).fetchone()
        assert tuple(row) == ("sink", "sink_consumed")
    finally:
        connection.close()

    disabled_database_url, disabled_app = _prepare(
        monkeypatch,
        tmp_path / "disabled",
        migrated_identity_database,
        delivery_mode="disabled",
    )
    disabled_transport = ASGITransport(app=disabled_app)
    async with AsyncClient(
        transport=disabled_transport,
        base_url="https://testserver",
    ) as disabled_client:
        unavailable = await _register(
            disabled_client,
            email="delivery.unavailable@example.com",
            password=NEW_ACCOUNT_PASSWORD,
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "identity.delivery_unavailable"

    connection = connect_database(disabled_database_url)
    try:
        intent = connection.execute(
            "SELECT status FROM registration_intents"
        ).fetchone()
        assert intent is not None and intent["status"] == "superseded"
        event = connection.execute(
            """
            SELECT account_id,event_type,reason_code,payload_json
            FROM identity_lifecycle_events
            WHERE event_type='identity.challenge_delivery_failed'
            """
        ).fetchone()
        assert event is not None
        assert tuple(event[:3]) == (
            None,
            "identity.challenge_delivery_failed",
            "delivery_adapter_failed",
        )
        assert "delivery.unavailable@example.com" not in event["payload_json"]
        assert connection.execute(
            "SELECT COUNT(*) FROM identity_lifecycle_outbox"
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.anyio
async def test_verification_is_single_consumer_under_100_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        started = await _register(
            client,
            email="concurrent.owner@example.com",
            password=NEW_ACCOUNT_PASSWORD,
        )
        assert started.status_code == 200
        intent_id = started.json()["intent_id"]
        challenge = pop_test_identity_challenge("registration", intent_id)
        assert challenge is not None

        async def verify_once(index: int):
            async with AsyncClient(
                transport=transport,
                base_url="https://testserver",
                headers={"User-Agent": f"PRD092-Concurrent-{index}"},
            ) as contender:
                return await contender.post(
                    "/identity/verifications",
                    json={"intent_id": intent_id, "code": challenge.code},
                    headers=_write_headers(),
                )

        responses = await asyncio.gather(*(verify_once(index) for index in range(100)))
        assert sum(response.status_code == 200 for response in responses) == 1
        assert {response.status_code for response in responses} <= {200, 400, 429}

    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM global_accounts WHERE email_normalized=?",
            ("concurrent.owner@example.com",),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM registration_intents WHERE status='verified'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM verification_challenges WHERE status='consumed'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM identity_lifecycle_events "
            "WHERE event_type='identity.registered'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_resend_supersedes_old_generation_and_only_latest_code_verifies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        registration_email = "resend.owner@example.com"
        started = await _register(
            client,
            email=registration_email,
            password=NEW_ACCOUNT_PASSWORD,
        )
        assert started.status_code == 200
        intent_id = started.json()["intent_id"]
        first = pop_test_identity_challenge("registration", intent_id)
        assert first is not None and first.generation == 1
        assert registration_email.encode("utf-8") not in _database_artifact_bytes(database_url)

        connection = connect_database(database_url)
        try:
            registration = connection.execute(
                "SELECT email_ciphertext FROM registration_intents WHERE registration_intent_id=?",
                (intent_id,),
            ).fetchone()
            assert registration is not None
            assert registration["email_ciphertext"].startswith(
                "metahub-identity-pii-aesgcm-v1:1:email_normalized:"
            )
            assert registration_email not in registration["email_ciphertext"]
        finally:
            connection.close()

        cooling_down = await client.post(
            "/identity/verifications/resend",
            json={"intent_id": intent_id},
            headers=_write_headers(),
        )
        assert cooling_down.status_code == 200
        assert pop_test_identity_challenge("registration", intent_id) is None

        connection = connect_database(database_url)
        try:
            connection.execute(
                "UPDATE verification_challenges SET sent_at=datetime('now','-2 minutes') "
                "WHERE registration_intent_id=? AND generation=1",
                (intent_id,),
            )
            connection.commit()
        finally:
            connection.close()

        resent = await client.post(
            "/identity/verifications/resend",
            json={"intent_id": intent_id},
            headers=_write_headers(),
        )
        assert resent.status_code == 200
        second = pop_test_identity_challenge("registration", intent_id)
        assert second is not None and second.generation == 2
        assert registration_email.encode("utf-8") not in _database_artifact_bytes(database_url)

        stale = await client.post(
            "/identity/verifications",
            json={"intent_id": intent_id, "code": first.code},
            headers=_write_headers(),
        )
        assert stale.status_code == 400
        verified = await client.post(
            "/identity/verifications",
            json={"intent_id": intent_id, "code": second.code},
            headers=_write_headers(),
        )
        assert verified.status_code == 200

    connection = connect_database(database_url)
    try:
        states = [
            tuple(row)
            for row in connection.execute(
                "SELECT generation,status FROM verification_challenges "
                "WHERE registration_intent_id=? ORDER BY generation",
                (intent_id,),
            )
        ]
        assert states == [(1, "superseded"), (2, "consumed")]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_registration_ciphertext_tamper_fails_closed_without_consuming_challenge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        started = await _register(
            client,
            email="tampered-envelope@example.com",
            password=NEW_ACCOUNT_PASSWORD,
        )
        assert started.status_code == 200
        intent_id = started.json()["intent_id"]
        challenge = pop_test_identity_challenge("registration", intent_id)
        assert challenge is not None

        connection = connect_database(database_url)
        try:
            ciphertext = str(
                connection.execute(
                    "SELECT email_ciphertext FROM registration_intents "
                    "WHERE registration_intent_id=?",
                    (intent_id,),
                ).fetchone()[0]
            )
            replacement = "A" if ciphertext[-1] != "A" else "B"
            connection.execute(
                "UPDATE registration_intents SET email_ciphertext=? "
                "WHERE registration_intent_id=?",
                (ciphertext[:-1] + replacement, intent_id),
            )
            connection.commit()
        finally:
            connection.close()

        rejected = await client.post(
            "/identity/verifications",
            json={"intent_id": intent_id, "code": challenge.code},
            headers=_write_headers(),
        )
        assert rejected.status_code == 503
        assert rejected.json()["code"] == "identity.unavailable"
        assert "cipher" not in rejected.text.lower()

    connection = connect_database(database_url)
    try:
        states = connection.execute(
            """
            SELECT i.status,c.status FROM registration_intents i
            JOIN verification_challenges c
              ON c.registration_intent_id=i.registration_intent_id
            WHERE i.registration_intent_id=?
            """,
            (intent_id,),
        ).fetchone()
        assert tuple(states) == ("pending", "pending")
    finally:
        connection.close()


@pytest.mark.anyio
async def test_public_identity_routes_reject_ambiguous_or_authorized_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    valid_body = (
        b'{"email":"strict@example.com","display_name":"Strict",'
        b'"password":"NewAccount!2026","consent_version":"privacy-2026-08"}'
    )
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        duplicate_key = await client.post(
            "/identity/registrations",
            content=valid_body[:-1] + b',"email":"other@example.com"}',
            headers={**_write_headers(), "Content-Type": "application/json"},
        )
        unknown_field = await client.post(
            "/identity/registrations",
            json={
                "email": "strict@example.com",
                "display_name": "Strict",
                "password": NEW_ACCOUNT_PASSWORD,
                "consent_version": "privacy-2026-08",
                "workspace_id": "forbidden",
            },
            headers=_write_headers(),
        )
        bearer = await client.post(
            "/identity/registrations",
            content=valid_body,
            headers={
                **_write_headers(),
                "Content-Type": "application/json",
                "Authorization": "Bearer forbidden",
            },
        )
        cross_site = await client.post(
            "/identity/registrations",
            content=valid_body,
            headers={
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "application/json",
            },
        )
        duplicate_origin = await client.post(
            "/identity/registrations",
            content=valid_body,
            headers=[
                ("Origin", ORIGIN),
                ("Origin", ORIGIN),
                ("Sec-Fetch-Site", "same-site"),
                ("Content-Type", "application/json"),
            ],
        )

    assert duplicate_key.status_code == 422
    assert unknown_field.status_code == 422
    assert duplicate_key.json()["code"] == unknown_field.json()["code"] == (
        "identity.request_invalid"
    )
    assert bearer.json()["code"] == "identity.credential_invalid"
    assert cross_site.json()["code"] == "identity.origin_invalid"
    assert duplicate_origin.json()["code"] == "identity.origin_invalid"
    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM registration_intents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_abuse_buckets").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.anyio
async def test_registration_rate_limit_runs_before_password_kdf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    import metahub_control.identity.self_service as self_service

    hash_calls = 0

    def bounded_fake_hash(_password: str) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return "test-candidate-password-hash-" + "a" * 40

    monkeypatch.setattr(self_service, "hash_password_argon2id", bounded_fake_hash)
    _database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        responses = [
            await _register(
                client,
                email="limited.owner@example.com",
                password=NEW_ACCOUNT_PASSWORD,
            )
            for _index in range(6)
        ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200, 429]
    assert responses[-1].json()["code"] == "identity.rate_limited"
    assert hash_calls == 5


@pytest.mark.anyio
async def test_warm_registration_verification_and_abuse_buckets_meet_latency_budgets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    import metahub_control.identity.self_service as self_service

    _database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)

    def p95(samples: list[float]) -> float:
        return sorted(samples)[math.ceil(len(samples) * 0.95) - 1]

    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        warm = await _register(
            client,
            email="perf-warm@example.com",
            password=NEW_ACCOUNT_PASSWORD,
        )
        assert warm.status_code == 200
        assert pop_test_identity_challenge("registration", warm.json()["intent_id"]) is not None

        pending: list[tuple[str, str]] = []
        registration_samples: list[float] = []
        for index in range(12):
            started_at = time.perf_counter()
            response = await _register(
                client,
                email=f"perf-{index}@example.com",
                password=NEW_ACCOUNT_PASSWORD,
            )
            registration_samples.append(time.perf_counter() - started_at)
            assert response.status_code == 200
            intent_id = response.json()["intent_id"]
            challenge = pop_test_identity_challenge("registration", intent_id)
            assert challenge is not None
            pending.append((intent_id, challenge.code))

        verification_samples: list[float] = []
        for intent_id, code in pending:
            started_at = time.perf_counter()
            response = await client.post(
                "/identity/verifications",
                json={"intent_id": intent_id, "code": code},
                headers=_write_headers(),
            )
            verification_samples.append(time.perf_counter() - started_at)
            assert response.status_code == 200

    abuse_samples: list[float] = []
    for index in range(41):
        started_at = time.perf_counter()
        with database_request_scope(reuse_connections=True):
            self_service._consume_rate_limits(
                app.state.config,
                action="registration",
                primary_key=f"bucket-{index}@example.com",
                user_agent=f"PRD092-Perf/{index}",
                client_host=f"127.0.1.{index}",
                device_token=f"perf-device-{index}",
            )
        if index:
            abuse_samples.append(time.perf_counter() - started_at)

    assert p95(registration_samples) < 0.500, registration_samples
    assert p95(verification_samples) < 0.500, verification_samples
    assert p95(abuse_samples) < 0.030, abuse_samples
