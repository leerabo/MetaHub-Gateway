"""PRD-110 risk case, challenge, appeal, and restriction API contracts."""

from __future__ import annotations

import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.service import CSRF_COOKIE, SESSION_COOKIE
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_jobs.runtime import run_platform_job_once
from metahub_control.risk_controls import routes as risk_routes
from metahub_control.risk_controls import service as risk_service
from metahub_control.risk_controls.routes import router
from metahub_control.risk_controls.scheduler import enqueue_due_risk_jobs
from metahub_control.risk_controls.service import create_case_challenge
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import WorkspaceSessionContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
PLATFORM_TOKEN = "s" * 43
PLATFORM_CSRF = "p" * 43
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
GLOBAL_TOKEN = "g" * 43
GLOBAL_CSRF = "i" * 43
OPERATOR_ID = "pto_d110d110d110d110d110d110d110d110"
OPERATOR_ACCOUNT_ID = "gac_d110d110d110d110d110d110d110d110"
WORKSPACE_ID = "wsp_aba85648d6c3c0119b4a41bddf82762b"
TENANT_ID = "944f3e6d-8b66-584e-b095-7cac890a5f87"
ACCOUNT_ID = "gac_2abeaa4602635ac0bd3ad56a7b19320a"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _config(database_url: str) -> AppConfig:
    return AppConfig(
        service_name="control-plane-api",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=datetime.now(UTC),
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(32),
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


def _platform_context(*permissions: str) -> PlatformContext:
    return PlatformContext(
        platform_session_id="pts_" + "d" * 32,
        operator_id=OPERATOR_ID,
        account_id=OPERATOR_ACCOUNT_ID,
        global_session_id="gss_" + "d" * 32,
        factor_id="mfa_" + "d" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=permissions,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _workspace_context(**changes: str) -> WorkspaceSessionContext:
    values = {
        "workspace_session_id": "wss_" + "1" * 32,
        "global_session_id": "gss_" + "1" * 32,
        "account_id": ACCOUNT_ID,
        "workspace_id": WORKSPACE_ID,
        "tenant_id": TENANT_ID,
        "membership_id": "wmb_0a1355f1e054acafb7fe2eaf001797b8",
        "legacy_user_id": "009cc16d-d981-5969-9a91-bf52dbc96715",
        "membership_role": "owner",
        "legacy_role": "admin",
        "account_email": "workspace-owner@example.invalid",
        "display_name": "Workspace owner",
        "tenant_slug": "default",
        "tenant_name": "MetaHub Default Tenant",
        "permission_codes": (),
        "permission_revision": 1,
        "authorization_fingerprint": "f" * 64,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "token": WORKSPACE_TOKEN,
    }
    values.update(changes)
    return WorkspaceSessionContext(**values)


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd110-risk-base")
    database = root / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0103"
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        source = connection.execute(
            "SELECT * FROM global_accounts ORDER BY account_id LIMIT 1"
        ).fetchone()
        assert source is not None
        account = dict(source)
        account.update(
            {
                "account_id": OPERATOR_ACCOUNT_ID,
                "email_normalized": "risk-operator@example.invalid",
                "phone_e164": None,
                "display_name": "Risk operator",
                "status": "active",
                "login_enabled": 1,
                "failed_login_count": 0,
                "locked_until": None,
                "last_login_at": None,
            }
        )
        columns = tuple(account)
        connection.execute(
            f"INSERT INTO global_accounts({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            tuple(account[column] for column in columns),
        )
        connection.execute(
            """INSERT INTO platform_operators(
                 operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
               VALUES(?,?,'active',1,NULL,'2026-08-24T00:00:00Z','2026-08-24T00:00:00Z')""",
            (OPERATOR_ID, OPERATOR_ACCOUNT_ID),
        )
        connection.execute(
            """INSERT INTO platform_operator_role_bindings(
                 operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
               VALUES(?,'platform_owner','active',NULL,'2026-08-24T00:00:00Z',NULL)""",
            (OPERATOR_ID,),
        )
        connection.commit()
    finally:
        connection.close()
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> AppConfig:
    database = tmp_path / "runtime.sqlite3"
    shutil.copy2(base_database, database)
    return _config(_url(database))


def _app(monkeypatch: pytest.MonkeyPatch, config: AppConfig, holder: dict[str, object]):
    monkeypatch.setattr(
        risk_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: holder["platform"],
    )
    monkeypatch.setattr(
        risk_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: holder["platform"],
    )
    monkeypatch.setattr(
        risk_service,
        "authenticate_workspace_session_in_transaction",
        lambda *args, **kwargs: holder["workspace"],
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-110 Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag",),
    )
    app.state.config = config
    app.include_router(router)
    return app


def _platform_headers(*, key: str | None = None, revision: int | None = None):
    headers = {
        "Cookie": (
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; {PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": PLATFORM_CSRF,
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return headers


def _workspace_headers(*, key: str):
    return {
        "Cookie": (
            f"{SESSION_COOKIE}={GLOBAL_TOKEN}; {CSRF_COOKIE}={GLOBAL_CSRF}; "
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": WORKSPACE_CSRF,
        "Idempotency-Key": key,
    }


def _issue(config: AppConfig, marker: str = "case"):
    return create_case_challenge(
        config,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        account_id=ACCOUNT_ID,
        case_kind="api_abuse",
        signal_type="request_velocity",
        safe_reason_family="unusual_volume",
        score_band="high",
        subject_value=f"subject-{marker}",
        ip_prefix="203.0.113.0/24",
        device_value=f"device-{marker}",
        request_id=f"request-{marker}",
        trace_id=f"trace-{marker}",
    )


def _review_payload():
    return {
        "decision": "required_review",
        "reason_code": "manual_review",
        "restriction_kind": None,
        "restriction_minutes": None,
        "confirmed": True,
    }


def _restrict_payload(minutes: int = 30):
    return {
        "decision": "restricted",
        "reason_code": "automation_confirmed",
        "restriction_kind": "api_calls",
        "restriction_minutes": minutes,
        "confirmed": True,
    }


@pytest.mark.anyio
async def test_platform_projection_cursor_permission_and_transport(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    _issue(runtime, "projection-a")
    _issue(runtime, "projection-b")
    holder = {
        "platform": _platform_context("platform.risk.read", "platform.risk.decide"),
        "workspace": _workspace_context(),
    }
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        security = await client.get("/platform/risk/cases?limit=1", headers=_platform_headers())
        assert security.status_code == 200, security.text
        assert security.json()["projection"] == "security"
        assert security.json()["items"][0]["score_band"] == "high"
        assert security.json()["items"][0]["signal_families"] == ["request_velocity"]
        assert security.json()["next_cursor"]
        assert "no-store" in security.headers["cache-control"]

        cursor = security.json()["next_cursor"]
        second = await client.get(
            f"/platform/risk/cases?limit=1&cursor={cursor}", headers=_platform_headers()
        )
        assert second.status_code == 200
        tampered = await client.get(
            f"/platform/risk/cases?limit=1&cursor={cursor[:-1]}x",
            headers=_platform_headers(),
        )
        assert tampered.status_code == 400
        assert tampered.json()["code"] == "risk.cursor_invalid"

        holder["platform"] = _platform_context("platform.risk.support_read")
        support = await client.get("/platform/risk/cases", headers=_platform_headers())
        assert support.status_code == 200
        assert support.json()["projection"] == "support"
        assert support.json()["items"][0]["score_band"] is None
        assert support.json()["items"][0]["signal_families"] == []
        denied = await client.post(
            f"/platform/risk/cases/{support.json()['items'][0]['risk_case_id']}/decisions",
            headers=_platform_headers(key="prd110-support-denied", revision=1),
            json=_review_payload(),
        )
        assert denied.status_code == 403

        conflict_headers = _platform_headers()
        conflict_headers["Cookie"] += f"; {WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"
        conflict = await client.get("/platform/risk/cases", headers=conflict_headers)
        assert conflict.status_code == 400
        assert conflict.json()["code"] == "platform.credential_conflict"


@pytest.mark.anyio
async def test_decision_cas_idempotency_appeal_and_resolution(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    issued = _issue(runtime, "decision")
    case_id = issued.challenge.risk_challenge_id
    connection = connect_database(runtime.database_url)
    try:
        case_id = str(
            connection.execute(
                "SELECT risk_case_id FROM risk_challenges WHERE risk_challenge_id=?",
                (issued.challenge.risk_challenge_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    holder = {
        "platform": _platform_context("platform.risk.read", "platform.risk.decide"),
        "workspace": _workspace_context(),
    }
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        review = await client.post(
            f"/platform/risk/cases/{case_id}/decisions",
            headers=_platform_headers(key="prd110-review-command", revision=1),
            json=_review_payload(),
        )
        assert review.status_code == 201, review.text
        assert review.json()["case"]["status"] == "under_review"
        assert review.headers["etag"] == '"2"'
        replay = await client.post(
            f"/platform/risk/cases/{case_id}/decisions",
            headers=_platform_headers(key="prd110-review-command", revision=1),
            json=_review_payload(),
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        conflict = await client.post(
            f"/platform/risk/cases/{case_id}/decisions",
            headers=_platform_headers(key="prd110-review-command", revision=1),
            json=_restrict_payload(),
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "risk.idempotency_conflict"

        restricted = await client.post(
            f"/platform/risk/cases/{case_id}/decisions",
            headers=_platform_headers(key="prd110-restrict-command", revision=2),
            json=_restrict_payload(),
        )
        assert restricted.status_code == 201, restricted.text
        restriction = restricted.json()["case"]["restriction"]
        assert restriction["status"] == "active"
        stale = await client.post(
            f"/platform/risk/cases/{case_id}/decisions",
            headers=_platform_headers(key="prd110-stale-command", revision=2),
            json={
                "decision": "cleared",
                "reason_code": "false_positive",
                "restriction_kind": None,
                "restriction_minutes": None,
                "confirmed": True,
            },
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "risk.revision_conflict"

        holder["workspace"] = _workspace_context(
            account_id="gac_" + "3" * 32, workspace_id="wsp_" + "3" * 32
        )
        hidden = await client.post(
            "/risk/appeals",
            headers=_workspace_headers(key="prd110-wrong-owner-appeal"),
            json={
                "restriction_id": restriction["risk_restriction_id"],
                "category": "not_my_activity",
                "confirmed": True,
            },
        )
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "risk.restriction_not_found"

        holder["workspace"] = _workspace_context()
        appeal_payload = {
            "restriction_id": restriction["risk_restriction_id"],
            "category": "not_my_activity",
            "confirmed": True,
        }
        appeal = await client.post(
            "/risk/appeals",
            headers=_workspace_headers(key="prd110-owner-appeal"),
            json=appeal_payload,
        )
        assert appeal.status_code == 201, appeal.text
        appeal_replay = await client.post(
            "/risk/appeals",
            headers=_workspace_headers(key="prd110-owner-appeal"),
            json=appeal_payload,
        )
        assert appeal_replay.status_code == 200

        cleared = await client.post(
            f"/platform/risk/cases/{case_id}/decisions",
            headers=_platform_headers(key="prd110-clear-command", revision=3),
            json={
                "decision": "cleared",
                "reason_code": "appeal_approved",
                "restriction_kind": None,
                "restriction_minutes": None,
                "confirmed": True,
            },
        )
        assert cleared.status_code == 201, cleared.text
        assert cleared.json()["case"]["status"] == "cleared"
        assert cleared.json()["case"]["restriction"]["status"] == "cleared"
        assert cleared.json()["case"]["appeals"][0]["status"] == "approved"

    connection = connect_database(runtime.database_url)
    try:
        event_types = {
            row[0] for row in connection.execute("SELECT event_type FROM risk_event_outbox")
        }
        assert {
            "risk.challenge_required",
            "risk.workspace_restricted",
            "risk.appeal_resolved",
        } <= event_types
    finally:
        connection.close()


@pytest.mark.anyio
async def test_challenge_attempt_replay_expiry_and_owner_fencing(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    issued = _issue(runtime, "challenge")
    holder = {
        "platform": _platform_context("platform.risk.read"),
        "workspace": _workspace_context(),
    }
    app = _app(monkeypatch, runtime, holder)
    token = issued.challenge_token.get_secret_value()
    answer = issued.answer.get_secret_value()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        wrong = await client.post(
            "/risk/challenges",
            headers=_workspace_headers(key="prd110-challenge-wrong"),
            json={
                "challenge_id": issued.challenge.risk_challenge_id,
                "challenge_token": token,
                "answer": "000000" if answer != "000000" else "999999",
                "revision": 1,
                "confirmed": True,
            },
        )
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "risk.challenge_invalid"

        completed_payload = {
            "challenge_id": issued.challenge.risk_challenge_id,
            "challenge_token": token,
            "answer": answer,
            "revision": 2,
            "confirmed": True,
        }
        completed = await client.post(
            "/risk/challenges",
            headers=_workspace_headers(key="prd110-challenge-complete"),
            json=completed_payload,
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["challenge"]["status"] == "consumed"
        assert completed.json()["challenge"]["attempts"] == 2
        replay = await client.post(
            "/risk/challenges",
            headers=_workspace_headers(key="prd110-challenge-complete"),
            json=completed_payload,
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        reused = await client.post(
            "/risk/challenges",
            headers=_workspace_headers(key="prd110-challenge-reuse"),
            json=completed_payload,
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "risk.challenge_replayed"

        owner_fenced = _issue(runtime, "owner-fence")
        holder["workspace"] = _workspace_context(account_id="gac_" + "4" * 32)
        hidden = await client.post(
            "/risk/challenges",
            headers=_workspace_headers(key="prd110-challenge-hidden"),
            json={
                "challenge_id": owner_fenced.challenge.risk_challenge_id,
                "challenge_token": owner_fenced.challenge_token.get_secret_value(),
                "answer": owner_fenced.answer.get_secret_value(),
                "revision": 1,
                "confirmed": True,
            },
        )
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "risk.challenge_not_found"

        expiring = _issue(runtime, "expiry")
        holder["workspace"] = _workspace_context()
        future = expiring.challenge.expires_at + timedelta(seconds=1)
        with monkeypatch.context() as patcher:
            patcher.setattr(risk_service, "_database_now", lambda connection: future)
            expired = await client.post(
                "/risk/challenges",
                headers=_workspace_headers(key="prd110-challenge-expired"),
                json={
                    "challenge_id": expiring.challenge.risk_challenge_id,
                    "challenge_token": expiring.challenge_token.get_secret_value(),
                    "answer": expiring.answer.get_secret_value(),
                    "revision": 1,
                    "confirmed": True,
                },
            )
        assert expired.status_code == 409
        assert expired.json()["code"] == "risk.challenge_expired"

    connection = connect_database(runtime.database_url)
    try:
        rows = connection.execute(
            "SELECT outcome FROM risk_challenge_attempts "
            "WHERE risk_challenge_id=? ORDER BY attempt_number",
            (issued.challenge.risk_challenge_id,),
        ).fetchall()
        assert [row[0] for row in rows] == ["rejected", "accepted"]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_restriction_expiry_job_is_bounded_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, runtime: AppConfig
) -> None:
    issued = _issue(runtime, "job-expiry")
    connection = connect_database(runtime.database_url)
    try:
        case_id = str(
            connection.execute(
                "SELECT risk_case_id FROM risk_challenges WHERE risk_challenge_id=?",
                (issued.challenge.risk_challenge_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    holder = {
        "platform": _platform_context("platform.risk.read", "platform.risk.decide"),
        "workspace": _workspace_context(),
    }
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        restricted = await client.post(
            f"/platform/risk/cases/{case_id}/decisions",
            headers=_platform_headers(key="prd110-expiry-restrict", revision=1),
            json=_restrict_payload(minutes=5),
        )
        assert restricted.status_code == 201, restricted.text
        restriction = restricted.json()["case"]["restriction"]
        appeal = await client.post(
            "/risk/appeals",
            headers=_workspace_headers(key="prd110-expiry-appeal"),
            json={
                "restriction_id": restriction["risk_restriction_id"],
                "category": "expected_usage",
                "confirmed": True,
            },
        )
        assert appeal.status_code == 201

    future = datetime.fromisoformat(restriction["expires_at"].replace("Z", "+00:00"))
    future += timedelta(seconds=1)
    assert enqueue_due_risk_jobs(runtime, now=future) == 1
    assert enqueue_due_risk_jobs(runtime, now=future) == 0
    with monkeypatch.context() as patcher:
        patcher.setattr(risk_service, "_database_now", lambda connection: future)
        assert run_platform_job_once(runtime, worker_code="prd110-risk-expiry-worker") is True
    assert enqueue_due_risk_jobs(runtime, now=future) == 0

    connection = connect_database(runtime.database_url)
    try:
        row = connection.execute(
            "SELECT status,revision FROM risk_restrictions WHERE risk_restriction_id=?",
            (restriction["risk_restriction_id"],),
        ).fetchone()
        assert tuple(row) == ("expired", 2)
        case = connection.execute(
            "SELECT status,revision FROM risk_cases WHERE risk_case_id=?", (case_id,)
        ).fetchone()
        assert tuple(case) == ("closed", 3)
        appeal = connection.execute(
            "SELECT status,resolution_reason,revision FROM risk_appeals "
            "WHERE risk_restriction_id=?",
            (restriction["risk_restriction_id"],),
        ).fetchone()
        assert tuple(appeal) == ("rejected", "insufficient_evidence", 2)
    finally:
        connection.close()
