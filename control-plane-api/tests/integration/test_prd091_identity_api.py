"""PRD-091 global account API, credential realm, and browser-session contracts."""

from __future__ import annotations

import asyncio
import math
import shutil
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as legacy_auth_router
from metahub_control.identity import routes as identity_routes
from metahub_control.identity import service as identity_service
from metahub_control.identity.routes import router as identity_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database, verify_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
PASSWORD = "LocalAdmin!2026"
ROTATED_PASSWORD = "RotatedAdmin!2026"


@pytest.fixture(scope="module")
def migrated_identity_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd091-api-migrated")
    database = root / "identity-0083.sqlite3"
    apply_migrations(f"sqlite:///{database.as_posix()}")
    return database


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "identity.sqlite3"
    shutil.copy2(migrated_identity_database, database)
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-legacy-auth-token-secret")
    monkeypatch.setenv(
        "METAHUB_IDENTITY_SESSION_SECRET",
        "test-independent-global-identity-session-secret-2026",
    )
    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", ORIGIN)
    apply_seed_data(database_url)
    app = create_service_app(
        "control-plane-api",
        "PRD-091 Identity Test",
        extra_cors_allow_headers=("X-CSRF-Token",),
        cors_allow_credentials=True,
    )
    app.include_router(legacy_auth_router)
    app.include_router(identity_router)
    return database_url, app


async def _login(client: AsyncClient, password: str = PASSWORD):
    return await client.post(
        "/identity/login",
        json={"email": "admin@metahub.local", "password": password},
        headers={"Origin": ORIGIN, "X-Request-ID": "password-canary-request-id"},
    )


@pytest.mark.anyio
async def test_global_login_me_sessions_rotate_logout_and_legacy_compatibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as first:
        login = await _login(first)
        assert login.status_code == 200
        assert login.json()["account"]["email"] == "admin@metahub.local"
        assert login.json()["account"]["mfa_configured"] is False
        assert login.json()["current_session"]["is_current"] is True
        assert "access_token" not in login.json()
        assert "tenant_id" not in login.text and "role" not in login.text
        assert login.json()["request_id"] != "password-canary-request-id"
        set_cookies = login.headers.get_list("set-cookie")
        assert len(set_cookies) == 2
        assert "Secure" in set_cookies[0] and "HttpOnly" in set_cookies[0]
        assert "Secure" in set_cookies[1] and "HttpOnly" not in set_cookies[1]

        me = await first.get("/identity/me")
        sessions = await first.get("/identity/sessions?limit=20&offset=0")
        assert me.status_code == 200
        assert sessions.status_code == 200
        assert sessions.json()["total"] == 1
        assert sessions.json()["sessions"][0]["is_current"] is True

        csrf = first.cookies["__Host-metahub-global-csrf"]
        missing_csrf = await first.delete(
            f"/identity/sessions/{sessions.json()['sessions'][0]['session_id']}",
            headers={"Origin": ORIGIN},
        )
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["code"] == "identity.csrf_invalid"

        async with AsyncClient(transport=transport, base_url="https://testserver") as second:
            second_login = await _login(second)
            assert second_login.status_code == 200
            assert (await second.get("/identity/me")).status_code == 200

            rotate = await first.post(
                "/identity/credentials/password/rotate",
                json={"current_password": PASSWORD, "new_password": ROTATED_PASSWORD},
                headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            )
            assert rotate.status_code == 200
            assert rotate.json()["rotated"] is True
            assert (await first.get("/identity/me")).status_code == 200
            revoked = await second.get("/identity/me")
            assert revoked.status_code == 401
            assert revoked.json()["code"] == "identity.session_revoked"

        legacy_old = await first.post(
            "/auth/login",
            json={"tenant_slug": "default", "email": "admin@metahub.local", "password": PASSWORD},
        )
        legacy_new = await first.post(
            "/auth/login",
            json={
                "tenant_slug": "default",
                "email": "admin@metahub.local",
                "password": ROTATED_PASSWORD,
            },
        )
        assert legacy_old.status_code == 401
        assert legacy_new.status_code == 200

        csrf = first.cookies["__Host-metahub-global-csrf"]
        logout = await first.post(
            "/identity/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert logout.status_code == 200
        assert logout.json()["logged_out"] is True
        assert (await first.get("/identity/me")).status_code == 401

    connection = connect_database(database_url)
    try:
        credential = connection.execute(
            "SELECT secret_hash,algorithm FROM account_credentials WHERE status='active'"
        ).fetchone()
        assert credential["algorithm"] == "argon2id"
        assert verify_password(ROTATED_PASSWORD, credential["secret_hash"])
        database_dump = "\n".join(
            str(value)
            for table in ("global_sessions", "identity_events", "identity_event_outbox")
            for row in connection.execute(f"SELECT * FROM {table}")
            for value in tuple(row)
        )
        assert PASSWORD not in database_dump
        assert ROTATED_PASSWORD not in database_dump
        assert "password-canary-request-id" not in database_dump
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_warm_login_and_session_validation_meet_latency_budgets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    _database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)

    def p95(samples: list[float]) -> float:
        return sorted(samples)[math.ceil(len(samples) * 0.95) - 1]

    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        # The first call parses the SQLite schema and upgrades the legacy seed hash. Neither
        # operation belongs to the documented warm-path budget.
        assert (await _login(client)).status_code == 200
        assert (await client.get("/identity/me")).status_code == 200

        login_samples: list[float] = []
        for _index in range(12):
            started = time.perf_counter()
            response = await _login(client)
            login_samples.append(time.perf_counter() - started)
            assert response.status_code == 200

        session_samples: list[float] = []
        for _index in range(40):
            started = time.perf_counter()
            response = await client.get("/identity/me")
            session_samples.append(time.perf_counter() - started)
            assert response.status_code == 200

    assert p95(login_samples) < 0.500
    assert p95(session_samples) < 0.030


@pytest.mark.anyio
async def test_identity_transport_rejects_mixed_credentials_origin_and_ambiguous_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    _database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        mixed = await client.post(
            "/identity/login",
            json={"email": "admin@metahub.local", "password": PASSWORD},
            headers={"Origin": ORIGIN, "Authorization": "Bearer legacy-token-canary"},
        )
        cross_origin = await client.post(
            "/identity/login",
            json={"email": "admin@metahub.local", "password": PASSWORD},
            headers={"Origin": "https://attacker.invalid", "Sec-Fetch-Site": "cross-site"},
        )
        duplicate = await client.post(
            "/identity/login",
            content=(b'{"email":"admin@metahub.local","password":"one","password":"two"}'),
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
        unknown = await client.post(
            "/identity/login",
            json={"email": "unknown@example.invalid", "password": "Unknown!2026"},
            headers={"Origin": ORIGIN},
        )
        wrong = await client.post(
            "/identity/login",
            json={"email": "admin@metahub.local", "password": "Unknown!2026"},
            headers={"Origin": ORIGIN},
        )

    assert mixed.status_code == 400 and mixed.json()["code"] == "identity.credential_invalid"
    assert cross_origin.status_code == 403
    assert cross_origin.json()["code"] == "identity.origin_invalid"
    assert duplicate.status_code == 422
    assert duplicate.json()["code"] == "identity.request_invalid"
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["code"] == wrong.json()["code"] == "identity.login_invalid"
    for response in (mixed, cross_origin, duplicate, unknown, wrong):
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.json()["request_id"]


@pytest.mark.anyio
async def test_global_cookie_does_not_authorize_legacy_tenant_routes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    _database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        assert (await _login(client)).status_code == 200
        tenant_me = await client.get("/auth/me")
    assert tenant_me.status_code == 401
    assert tenant_me.json()["code"] == "auth.token_missing"


@pytest.mark.anyio
async def test_expired_write_commits_tombstone_and_logout_error_clears_cookies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        login = await _login(client)
        session_id = login.json()["current_session"]["session_id"]
        csrf = client.cookies["__Host-metahub-global-csrf"]
        connection = connect_database(database_url)
        try:
            connection.execute(
                "UPDATE global_sessions SET expires_at=?,idle_expires_at=? WHERE session_id=?",
                ("2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z", session_id),
            )
            connection.commit()
        finally:
            connection.close()

        expired = await client.delete(
            f"/identity/sessions/{session_id}",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert expired.status_code == 401
        assert expired.json()["code"] == "identity.session_expired"

        connection = connect_database(database_url)
        try:
            assert (
                connection.execute(
                    "SELECT status FROM global_sessions WHERE session_id=?", (session_id,)
                ).fetchone()[0]
                == "expired"
            )
        finally:
            connection.close()

        logout = await client.post(
            "/identity/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert logout.status_code == 401
        assert logout.json()["code"] == "identity.session_expired"
        cleared = logout.headers.get_list("set-cookie")
        assert len(cleared) == 2
        assert all("Max-Age=0" in item and "Secure" in item for item in cleared)
        assert "__Host-metahub-global-session" not in client.cookies
        assert "__Host-metahub-global-csrf" not in client.cookies


@pytest.mark.anyio
async def test_unavailable_logout_clears_cookies_and_invalid_rotate_skips_argon2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    _database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        assert (await _login(client)).status_code == 200
        csrf = client.cookies["__Host-metahub-global-csrf"]

        def unavailable(*_args, **_kwargs):
            raise ApiError("identity.unavailable")

        monkeypatch.setattr(identity_routes, "authenticate_global_session", unavailable)
        logout = await client.post(
            "/identity/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert logout.status_code == 503
        assert logout.json()["code"] == "identity.unavailable"
        assert len(logout.headers.get_list("set-cookie")) == 2

    monkeypatch.undo()
    _database_url, app = _prepare(
        monkeypatch,
        tmp_path / "invalid-rotate",
        migrated_identity_database,
    )
    transport = ASGITransport(app=app)

    def unexpected_argon2(_password: str) -> str:
        raise AssertionError("Argon2 must not run before session and CSRF authentication")

    monkeypatch.setattr(identity_service, "hash_password_argon2id", unexpected_argon2)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set("__Host-metahub-global-session", "A" * 43)
        client.cookies.set("__Host-metahub-global-csrf", "B" * 43)
        rotate = await client.post(
            "/identity/credentials/password/rotate",
            json={"current_password": PASSWORD, "new_password": ROTATED_PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": "B" * 43},
        )
        assert rotate.status_code == 401
        assert rotate.json()["code"] == "identity.session_invalid"

        async def oversized_body():
            yield b'{"email":"admin@metahub.local","password":"'
            yield b"x" * 4096

        oversized = await client.post(
            "/identity/login",
            content=oversized_body(),
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
        assert oversized.status_code == 422
        assert oversized.json()["code"] == "identity.request_invalid"


@pytest.mark.anyio
async def test_login_failure_states_share_response_and_timing_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    password_canary = "Failure-Timing-Password-Canary"

    async def timed_login(client: AsyncClient, email: str, password: str):
        started = time.perf_counter()
        response = await client.post(
            "/identity/login",
            json={"email": email, "password": password},
            headers={"Origin": ORIGIN},
        )
        return response, time.perf_counter() - started

    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        unknown, unknown_time = await timed_login(
            client, "timing-canary@example.invalid", password_canary
        )
        legacy_wrong, legacy_time = await timed_login(
            client, "admin@metahub.local", password_canary
        )
        assert (await _login(client)).status_code == 200
        argon_wrong, argon_time = await timed_login(client, "admin@metahub.local", password_canary)

        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                UPDATE global_accounts SET status='disabled',login_enabled=1
                WHERE email_normalized=?
                """,
                ("admin@metahub.local",),
            )
            connection.commit()
        finally:
            connection.close()
        disabled, disabled_time = await timed_login(client, "admin@metahub.local", PASSWORD)

        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                UPDATE global_accounts
                SET status='locked',login_enabled=1,locked_until='2099-01-01T00:00:00Z'
                WHERE email_normalized=?
                """,
                ("admin@metahub.local",),
            )
            connection.commit()
        finally:
            connection.close()
        locked, locked_time = await timed_login(client, "admin@metahub.local", PASSWORD)

    responses = (unknown, legacy_wrong, argon_wrong, disabled, locked)
    durations = (unknown_time, legacy_time, argon_time, disabled_time, locked_time)
    assert all(response.status_code == 401 for response in responses)
    assert {response.json()["code"] for response in responses} == {"identity.login_invalid"}
    assert min(durations) >= 1.22
    assert max(durations) - min(durations) < 0.30
    assert password_canary not in caplog.text
    assert "timing-canary@example.invalid" not in caplog.text


@pytest.mark.anyio
async def test_concurrent_session_revoke_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_identity_database: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path, migrated_identity_database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as owner:
        assert (await _login(owner)).status_code == 200
        csrf = owner.cookies["__Host-metahub-global-csrf"]
        async with AsyncClient(transport=transport, base_url="https://testserver") as other:
            target = await _login(other)
            target_session_id = target.json()["current_session"]["session_id"]

        responses = await asyncio.gather(
            *(
                owner.delete(
                    f"/identity/sessions/{target_session_id}",
                    headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
                )
                for _index in range(100)
            )
        )
        assert all(response.status_code == 200 for response in responses)
        assert all(response.json()["revoked"] is True for response in responses)

    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM identity_events
            WHERE event_type='identity.session_revoked' AND session_id=?
            """,
                (target_session_id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()
