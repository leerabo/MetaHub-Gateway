"""Verified-email onboarding on an isolated native PG database and local TLS SMTP.

HTTP handlers use production identity rules. ASGI and the native PG connection
are supplementary checks, not a substitute for Ubuntu HTTPS/browser acceptance.
"""
import asyncio
import base64
from email import policy
from email.parser import BytesParser
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
for directory in ("shared-sdk/python", "control-plane-api/src", "gateway-data-plane/src"):
    sys.path.insert(0, str(ROOT / directory))
import psycopg
from psycopg import sql
from metahub_shared.db import apply_migrations, close_database_pools


async def verify_http(fixture):
    from httpx import ASGITransport, AsyncClient
    from metahub_shared.runtime.fastapi_app import create_service_app
    from metahub_control.identity.routes import router as identity_router
    from metahub_control.workspaces.routes import router as workspaces_router
    from metahub_control.workspace_access.routes import router as access_router
    from metahub_control.identity.delivery import pop_test_identity_challenge
    origin = "https://acceptance.example.invalid"
    app = create_service_app("control-plane-api", "Native verified-email onboarding",
                             extra_cors_allow_headers=("X-CSRF-Token", "If-Match", "Idempotency-Key"),
                             cors_allow_credentials=True)
    app.state.config = app.state.config.model_copy(update=fixture.settings())
    assert app.state.config.environment == "prod"
    for router in (identity_router, workspaces_router, access_router):
        app.include_router(router)
    observations = []
    base_headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin"}
    email = "verified-onboarding@example.invalid"
    password = "IndependentOnboarding!" + uuid4().hex
    async with AsyncClient(transport=ASGITransport(app=app), base_url=origin) as client:
        async def check(method, path, expected=200, **kwargs):
            response = await client.request(method, path, **kwargs)
            observations.append(dict(method=method, path=path, expected=expected, actual=response.status_code))
            if response.status_code not in (expected if isinstance(expected, tuple) else (expected,)):
                raise AssertionError(f"{method} {path}: {response.status_code}; code={response.json().get('code')}")
            return response

        registration_policy = (await check("GET", "/identity/registration-policy")).json()
        assert registration_policy["email_delivery_available"] is True
        assert registration_policy["email_verification"] is True
        assert registration_policy["human_verification"] is True
        assert registration_policy["local_registration"] is False
        for captcha_attempt in range(3):
            captcha = (await check("GET", "/identity/registration-captcha")).json()
            image_path = Path.cwd() / f"registration-captcha-{captcha_attempt}.png"
            image_path.write_bytes(base64.b64decode(captcha["image"].split(",", 1)[1], validate=True))
            print(json.dumps(dict(phase="read_real_captcha", image=str(image_path), expires=captcha["expires_at"])), flush=True)
            answer = (await asyncio.to_thread(input, "Local acceptance captcha: ")).strip()
            response = await check("POST", "/identity/registrations", expected=(200, 400), headers=base_headers,
                                   json=dict(email=email, display_name="独立邮箱验收用户", password=password,
                                             consent_version="trial-acceptance-20261015",
                                             captcha_id=captcha["challenge_id"], captcha_answer=answer))
            if response.status_code == 200:
                pending = response.json()
                break
            assert response.json()["code"] == "identity.captcha_invalid"
        else:
            raise AssertionError("Real captcha was not completed")
        assert len(fixture.messages) == 1
        message = BytesParser(policy=policy.default).parsebytes(fixture.messages[0])
        assert message["To"] == email
        code = re.search(r"verification code: ([0-9]{6})", message.get_content()).group(1)
        assert code not in json.dumps(pending)
        assert pop_test_identity_challenge("registration", pending["intent_id"]) is None
        wrong = f"{(int(code) + 1) % 1000000:06d}"
        await check("POST", "/identity/verifications", 400, headers=base_headers,
                    json=dict(intent_id=pending["intent_id"], code=wrong))
        verified = await check("POST", "/identity/verifications", headers=base_headers,
                               json=dict(intent_id=pending["intent_id"], code=code))
        assert verified.json()["account"]["display_name"] == "独立邮箱验收用户"
        cookies = verified.headers.get_list("set-cookie")
        assert cookies and all("Secure" in cookie for cookie in cookies)
        assert any(cookie.startswith("__Host-metahub-global-session=") and "HttpOnly" in cookie for cookie in cookies)
        await check("POST", "/identity/verifications", 400, headers=base_headers,
                    json=dict(intent_id=pending["intent_id"], code=code))
        headers = {**base_headers, "X-CSRF-Token": client.cookies["__Host-metahub-global-csrf"],
                   "Idempotency-Key": "onboarding-" + uuid4().hex}
        created = (await check("POST", "/workspaces", 201, headers=headers,
                              json=dict(kind="organization", name="独立邮箱验收组织"))).json()
        workspace_id = created["workspace"]["workspace_id"]
        listed = (await check("GET", "/identity/workspaces")).json()
        assert listed["total"] == 1
        await check("POST", "/identity/workspace-switches", headers=headers,
                    json=dict(workspace_id=workspace_id))
        await check("GET", "/workspace/session")
    return observations, email, workspace_id


def main():
    work = ROOT / ".tmp/trial-pg-import-4yycc3ou"
    pg_ctl = ROOT / ".tools/postgresql17/pgsql/bin/pg_ctl.exe"
    status = subprocess.run([str(pg_ctl), "-D", str(work / "data"), "status"],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if status.returncode != 3:
        raise ValueError("Cannot establish stopped state of task-owned cluster")
    attempt = work / ("registration-http-" + uuid4().hex)
    attempt.mkdir()
    os.chdir(attempt)
    for key in list(os.environ):
        if key.startswith("METAHUB_"):
            del os.environ[key]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def lifecycle(*args):
        with (attempt / "lifecycle.log").open("ab") as log:
            subprocess.run([str(pg_ctl), "-D", str(work / "data"), *args], check=True,
                           stdout=log, stderr=log, timeout=210, creationflags=subprocess.CREATE_NO_WINDOW)

    began = time.monotonic()
    started = False
    try:
        started = True
        lifecycle("-l", str(attempt / "server.log"), "-w", "-t", "180", "start", "-o",
                  f"-h 127.0.0.1 -p {port} -c timezone=UTC -c max_locks_per_transaction=2048 -c log_min_messages=panic -c log_min_error_statement=panic -c max_wal_size=128MB -c min_wal_size=16MB")
        database = "registration_http_" + uuid4().hex
        with psycopg.connect(f"postgresql://acceptance@127.0.0.1:{port}/postgres", autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        url = f"postgresql://acceptance@127.0.0.1:{port}/{database}"
        from metahub_shared.runtime.config import DEFAULT_CONFIG, load_app_config
        for key, value in DEFAULT_CONFIG.items():
            if key.endswith("_SECRET") and value:
                os.environ[key] = hashlib.sha256(uuid4().bytes).hexdigest()
        os.environ.update(dict(METAHUB_ENV="test", METAHUB_DATABASE_URL=url,
                               METAHUB_ALLOWED_ORIGINS="https://acceptance.example.invalid",
                               METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE="disabled",
                               METAHUB_PLATFORM_BOOTSTRAP_ENABLED="false", METAHUB_DEV_PLATFORM_NO_MFA="false",
                               METAHUB_LOG_LEVEL="ERROR"))
        load_app_config("control-plane-api")
        migrations = apply_migrations(url, migrations_dir=ROOT / "infra/migrations")
        print(json.dumps(dict(phase="native_registration_schema", migrations=len(migrations.applied))), flush=True)
        spec = importlib.util.spec_from_file_location("smtp_acceptance", ROOT / "scripts/acceptance/trial-smtp-tls.py")
        smtp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smtp)
        logging.basicConfig(filename=attempt / "private-http.log", level=logging.ERROR)
        with smtp.LocalSmtp(attempt) as fixture, fixture.trusted_ca():
            observations, email, workspace_id = asyncio.run(verify_http(fixture))
            assert fixture.authenticated == 1 and len(fixture.messages) == 1
        with psycopg.connect(url) as connection:
            assert connection.execute("SELECT count(*) FROM account_verifications v JOIN global_accounts a ON a.account_id=v.account_id WHERE a.email_normalized=%s", (email,)).fetchone()[0] == 1
        report = dict(status="passed", platform="Windows native PostgreSQL; supplementary",
                      identity_rules_environment="prod", http_transport="ASGI; normal production handlers",
                      smtp_transport="real localhost STARTTLS with verified private CA and authentication",
                      observations=observations, http_checks=len(observations), migrations=len(migrations.applied),
                      verified_mail_record_present_for_created_organization=True, wrong_and_replayed_code_rejected=True,
                      secure_httponly_cookie_verified=True, seed_applied=False, fixed_otp_used=False,
                      external_mail_sent=False, original_data_modified=False, ubuntu_deployment_verified=False,
                      browser_visual_verified=False, elapsed_seconds=round(time.monotonic() - began, 3))
        (ROOT / "output/deployment-ubuntu-20261014/registration-native-validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    except Exception as error:
        import traceback
        (attempt / "private-diagnostic.txt").write_text(traceback.format_exc(), encoding="utf-8")
        print(json.dumps(dict(status="failed", exception_type=type(error).__name__, attempt=str(attempt))), flush=True)
        return 1
    finally:
        close_database_pools()
        if started:
            lifecycle("-w", "stop", "-m", "fast")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
