"""Exercise normal identity/platform HTTP routes against an isolated native PG DB.

Supplementary Windows verification, not a claim of Ubuntu, TLS or browser QA.
No development seed, fixed OTP, paid upstream or existing account is used.
"""
import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
for directory in ("shared-sdk/python", "control-plane-api/src", "gateway-data-plane/src"):
    sys.path.insert(0, str(ROOT / directory))
import psycopg
from metahub_shared.db import apply_migrations, close_database_pools
from metahub_shared.identity import generate_totp_secret
from psycopg import sql


def totp(secret, now):
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", int(now.timestamp()) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"


async def verify_http(email, password, secret):
    from httpx import ASGITransport, AsyncClient
    from metahub_control.console_experience.routes import router as experience_router
    from metahub_control.identity.routes import router as identity_router
    from metahub_control.platform_access.routes import router as platform_router
    from metahub_shared.runtime.fastapi_app import create_service_app
    origin = "https://acceptance.example.invalid"
    app = create_service_app("control-plane-api", "Native PG identity acceptance",
                             extra_cors_allow_headers=("X-CSRF-Token", "If-Match"), cors_allow_credentials=True)
    app.include_router(identity_router)
    app.include_router(platform_router)
    app.include_router(experience_router)
    observations = []
    headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url=origin) as client:
        async def check(method, path, expected=200, **kwargs):
            response = await client.request(method, path, **kwargs)
            observations.append({"method": method, "path": path, "expected": expected,
                                 "actual": response.status_code})
            if response.status_code != expected:
                code = response.json().get("code") if response.headers.get("content-type", "").startswith("application/json") else "non-json"
                raise AssertionError(f"{method} {path}: {response.status_code}; code={code}")
            return response
        await check("GET", "/identity/me", 401)
        await check("GET", "/platform/session", 401)
        await check("POST", "/identity/login", 401, json={"email": email, "password": "incorrect-acceptance-password"}, headers=headers)
        logged_in = await check("POST", "/identity/login", json={"email": email, "password": password}, headers=headers)
        assert logged_in.json()["account"]["mfa_configured"] is True
        cookies = logged_in.headers.get_list("set-cookie")
        assert cookies and all("Secure" in value for value in cookies)
        assert any(value.startswith("__Host-metahub-global-session=") and "HttpOnly" in value for value in cookies)
        assert all(password not in value and secret not in value for value in cookies)
        await check("GET", "/identity/me")
        preference = await check("GET", "/identity/preferences")
        repeated_preference = await check("GET", "/identity/preferences")
        assert repeated_preference.json()["preference_id"] == preference.json()["preference_id"]
        assert preference.json()["locale"] == "zh-CN"
        preference_headers = {
            **headers, "X-CSRF-Token": client.cookies["__Host-metahub-global-csrf"],
            "If-Match": preference.headers["etag"],
            "Idempotency-Key": "native-http-preference-" + uuid4().hex,
        }
        changed = await check("PATCH", "/identity/preferences", headers=preference_headers,
                              json={"locale": "en-US"})
        replayed = await check("PATCH", "/identity/preferences", headers=preference_headers,
                               json={"locale": "en-US"})
        assert changed.json()["idempotent_replay"] is False
        assert replayed.json()["idempotent_replay"] is True
        for field in ("preference_id", "locale", "revision"):
            assert replayed.json()[field] == changed.json()[field]
        final_preference = await check("GET", "/identity/preferences")
        assert final_preference.json()["locale"] == "en-US"
        assert final_preference.json()["revision"] == preference.json()["revision"] + 1
        sessions = await check("GET", "/identity/sessions")
        assert sessions.json()["total"] == 1
        availability = await check("GET", "/identity/platform-access")
        assert availability.json()["available"] is True
        await check("POST", "/platform/sessions", 403, json={"totp_code": totp(secret, datetime.now(UTC))}, headers=headers)
        csrf_headers = {**headers, "X-CSRF-Token": client.cookies["__Host-metahub-global-csrf"]}
        issued_code = totp(secret, datetime.now(UTC))
        await check("POST", "/platform/sessions", json={"totp_code": issued_code}, headers=csrf_headers)
        await check("GET", "/platform/session")
        navigation = await check("GET", "/platform/navigation")
        assert navigation.json()
        await check("GET", "/platform/operators")
        await check("GET", "/platform/workspaces")
        await check("GET", "/platform/audit-logs")
        # Session issuance must consume the counter, even without a test OTP bypass.
        for _ in range(6):
            await check("POST", "/platform/sessions", 400, json={"totp_code": issued_code}, headers=csrf_headers)
        await check("POST", "/identity/logout", headers=csrf_headers)
        await check("GET", "/identity/me", 401)
        await check("GET", "/platform/session", 401)
    return observations


def main():
    # Never reuse a cluster or postmaster.pid from an earlier acceptance run.
    # A stale Windows PID may now identify an unrelated PostgreSQL instance.
    work = Path(tempfile.mkdtemp(prefix="trial-identity-native-", dir=ROOT / ".tmp"))
    attempt = work
    # Isolate all config lookup from the repository's developer .env.
    os.chdir(attempt)
    for key in list(os.environ):
        if key.startswith("METAHUB_"):
            del os.environ[key]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    def lifecycle(*args):
        with (attempt / "lifecycle.log").open("ab") as log:
            subprocess.run([str(ROOT / ".tools/postgresql17/pgsql/bin/pg_ctl.exe"), "-D", str(work / "data"), *args],
                           check=True, stdout=log, stderr=log, timeout=210, creationflags=subprocess.CREATE_NO_WINDOW)
    started = False
    began = time.monotonic()
    try:
        with (attempt / "initdb.log").open("ab") as log:
            subprocess.run([
                str(ROOT / ".tools/postgresql17/pgsql/bin/initdb.exe"), "-D", str(work / "data"),
                "-A", "trust", "-U", "acceptance", "--no-locale", "-E", "UTF8",
                "--wal-segsize=1",
            ], check=True, stdout=log, stderr=log, timeout=120,
               creationflags=subprocess.CREATE_NO_WINDOW)
        started = True
        lifecycle("-l", str(attempt / "server.log"), "-w", "-t", "180", "start", "-o",
                  f"-h 127.0.0.1 -p {port} -c timezone=UTC -c max_locks_per_transaction=2048 -c log_min_messages=panic -c log_min_error_statement=panic -c max_wal_size=128MB -c min_wal_size=16MB")
        database = "identity_http_" + uuid4().hex
        with psycopg.connect(f"postgresql://acceptance@127.0.0.1:{port}/postgres", autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        url = f"postgresql://acceptance@127.0.0.1:{port}/{database}"
        from metahub_shared.runtime.config import DEFAULT_CONFIG, load_app_config
        for key, value in DEFAULT_CONFIG.items():
            if key.endswith("_SECRET") and value:
                os.environ[key] = hashlib.sha256(uuid4().bytes).hexdigest()
        os.environ.update({"METAHUB_ENV": "test", "METAHUB_DATABASE_URL": url,
                           "METAHUB_ALLOWED_ORIGINS": "https://acceptance.example.invalid",
                           "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "disabled",
                           "METAHUB_PLATFORM_BOOTSTRAP_ENABLED": "false",
                           "METAHUB_DEV_PLATFORM_NO_MFA": "false", "METAHUB_LOG_LEVEL": "ERROR"})
        load_app_config("control-plane-api")
        result = apply_migrations(url, migrations_dir=ROOT / "infra/migrations")
        print(json.dumps({"phase": "native_identity_schema", "migrations": len(result.applied)}), flush=True)
        spec = importlib.util.spec_from_file_location("native_http_owner", ROOT / "scripts/deploy/bootstrap-owner.py")
        owner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(owner)
        email = "native-http-owner@example.invalid"
        password = "IndependentHttp!" + uuid4().hex
        secret = generate_totp_secret()
        # Terminal enrollment consumed a previous real TOTP period; login uses
        # the current period, without clock patches in the HTTP service.
        provisioned_at = datetime.now(UTC) - timedelta(seconds=60)
        with psycopg.connect(url, autocommit=True) as connection:
            owner.provision(connection, email=email, name="Native HTTP acceptance owner", password=password,
                            secret=secret, code=totp(secret, provisioned_at), now=provisioned_at,
                            encryption_root=os.environ["METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET"])
        logging.basicConfig(filename=attempt / "private-http.log", level=logging.ERROR)
        observations = asyncio.run(verify_http(email, password, secret))
        with psycopg.connect(url) as connection:
            failures, locked_until = connection.execute("SELECT failed_count,locked_until FROM account_mfa_attempt_guards").fetchone()
            assert failures == 5 and datetime.fromisoformat(locked_until) > datetime.now(UTC)
        report = {"status": "passed", "platform": "Windows native PostgreSQL (supplementary)",
                  "migrations": len(result.applied), "http_transport": "ASGI; production route handlers",
                  "observations": observations, "http_checks": len(observations),
                  "secure_httponly_cookie_verified": True, "real_totp_and_replay_rejection": True,
                  "five_failed_mfa_attempts_lock_device": True,
                  "seed_applied": False, "fixed_otp_used": False, "original_data_modified": False,
                  "tls_network_verified": False, "ubuntu_deployment_verified": False,
                  "browser_visual_verified": False, "elapsed_seconds": round(time.monotonic() - began, 3)}
        (ROOT / "output/deployment-ubuntu-20261014/identity-native-validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    except Exception as error:
        import traceback
        (attempt / "private-diagnostic.txt").write_text(traceback.format_exc(), encoding="utf-8")
        print(json.dumps({"status": "failed", "exception_type": type(error).__name__, "attempt": str(attempt)}), flush=True)
        return 1
    finally:
        close_database_pools()
        if started:
            lifecycle("-w", "stop", "-m", "fast")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
