"""PRD-137 fixed-profile assurance, remediation, risk, and fencing contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.security_assurance import routes, service
from metahub_control.security_assurance.models import (
    RiskAcceptanceCommand,
    ScenarioExecution,
)
from metahub_control.security_assurance.routes import router as assurance_router
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_control.workspace_access.service import WorkspaceSessionContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from prd137_receipt_support import sign_receipt, synthetic_receipt, write_test_trust

ORIGIN = "http://localhost:5173"
PLATFORM_CSRF = "c" * 43
STEP_UP = "u" * 43
PLATFORM_A_TOKEN = "a" * 43
PLATFORM_B_TOKEN = "b" * 43
PLATFORM_C_TOKEN = "d" * 43
WORKSPACE_OWNER_TOKEN = "o" * 43
WORKSPACE_SECURITY_TOKEN = "s" * 43
WORKSPACE_FOREIGN_TOKEN = "f" * 43
OPERATOR_A_ID = "pto_" + "a" * 32
OPERATOR_B_ID = "pto_" + "b" * 32
OPERATOR_C_ID = "pto_" + "c" * 32
ACCOUNT_C_ID = "gac_" + "c" * 32
SECURITY_USER_ID = "11111111-1111-4111-8111-111111111111"


@dataclass(frozen=True, slots=True)
class Runtime:
    config: AppConfig
    workspace_id: str
    tenant_id: str
    platform_contexts: dict[str, PlatformContext]
    workspace_contexts: dict[str, WorkspaceSessionContext]


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
        identity_session_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        support_session_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    supplied = os.environ.get("METAHUB_TEST_MIGRATED_BASELINE")
    if supplied:
        baseline = Path(supplied).resolve(strict=True)
        assert baseline.is_file()
        return baseline
    database = tmp_path_factory.mktemp("prd137-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert "0129" in apply_migrations(database_url).applied
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> Runtime:
    database = tmp_path / "prd137.sqlite3"
    shutil.copy2(base_database, database)
    return _runtime(_config(_url(database)), tmp_path / "receipt-public-trust.json")


def _runtime(config: AppConfig, trust_path: Path) -> Runtime:
    write_test_trust(trust_path)
    config = config.model_copy(update={"assurance_receipt_trust_bundle_path": str(trust_path)})
    stamp = "2026-08-31T00:00:00.000Z"
    with closing(connect_database(config.database_url)) as connection:
        owner = connection.execute(
            """SELECT membership.*,account.email_normalized,
                      legacy_user.email,legacy_user.display_name,
                      tenant.slug tenant_slug,tenant.name tenant_name
                 FROM workspace_memberships membership
                 JOIN global_accounts account ON account.account_id=membership.account_id
                 JOIN users legacy_user ON legacy_user.user_id=membership.legacy_user_id
                    AND legacy_user.tenant_id=membership.tenant_id
                 JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
                WHERE membership.role_code='owner' AND membership.status='active'"""
        ).fetchone()
        assert owner is not None
        password_hash = str(
            connection.execute(
                "SELECT password_hash FROM users WHERE user_id=?",
                (owner["legacy_user_id"],),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO users(
                 user_id,tenant_id,email,display_name,password_hash,role,status,
                 created_at,updated_at,failed_login_count,locked_until,last_login_at,
                 password_changed_at)
               VALUES(?,?,?,'Security Admin',?,'security_admin','active',?,?,0,NULL,NULL,?)""",
            (
                SECURITY_USER_ID,
                owner["tenant_id"],
                "security-admin@example.invalid",
                password_hash,
                stamp,
                stamp,
                stamp,
            ),
        )
        security_link = connection.execute(
            """SELECT account_id FROM account_identity_links
                WHERE source_type='legacy_user' AND legacy_tenant_id=?
                  AND legacy_user_id=? AND status='active'""",
            (owner["tenant_id"], SECURITY_USER_ID),
        ).fetchone()
        assert security_link is not None
        security_account_id = str(security_link["account_id"])
        projected_membership = connection.execute(
            """SELECT workspace_membership_id FROM workspace_memberships
                WHERE workspace_id=? AND tenant_id=? AND account_id=?
                  AND legacy_user_id=? AND role_code='security_admin' AND status='active'""",
            (
                owner["workspace_id"],
                owner["tenant_id"],
                security_account_id,
                SECURITY_USER_ID,
            ),
        ).fetchone()
        assert projected_membership is not None
        security_membership_id = str(projected_membership["workspace_membership_id"])
        connection.execute(
            """INSERT INTO global_accounts(
                 account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                 credential_revision,session_revision,mfa_state,failed_login_count,
                 locked_until,last_login_at,created_at,updated_at)
               VALUES(?,?,NULL,?,'active',0,1,1,'not_enrolled',0,NULL,NULL,?,?)""",
            (
                ACCOUNT_C_ID,
                "platform-reviewer@example.invalid",
                "Platform Reviewer",
                stamp,
                stamp,
            ),
        )
        connection.executemany(
            """INSERT INTO platform_operators(
                 operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
               VALUES(?,?,'active',1,NULL,?,?)""",
            (
                (OPERATOR_A_ID, owner["account_id"], stamp, stamp),
                (OPERATOR_B_ID, security_account_id, stamp, stamp),
                (OPERATOR_C_ID, ACCOUNT_C_ID, stamp, stamp),
            ),
        )
        connection.executemany(
            """INSERT INTO platform_operator_role_bindings(
                 operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
               VALUES(?,'platform_security','active',NULL,?,NULL)""",
            ((OPERATOR_A_ID, stamp), (OPERATOR_B_ID, stamp), (OPERATOR_C_ID, stamp)),
        )
        connection.commit()
        roles_a, permissions_a, _fingerprint_a = platform_access_service._authority(
            connection, OPERATOR_A_ID
        )
        roles_b, permissions_b, _fingerprint_b = platform_access_service._authority(
            connection, OPERATOR_B_ID
        )
        roles_c, permissions_c, _fingerprint_c = platform_access_service._authority(
            connection, OPERATOR_C_ID
        )
        security = connection.execute(
            """SELECT membership.*,account.email_normalized,
                      legacy_user.email,legacy_user.display_name,
                      tenant.slug tenant_slug,tenant.name tenant_name
                 FROM workspace_memberships membership
                 JOIN global_accounts account ON account.account_id=membership.account_id
                 JOIN users legacy_user ON legacy_user.user_id=membership.legacy_user_id
                    AND legacy_user.tenant_id=membership.tenant_id
                 JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
                WHERE membership.workspace_membership_id=?""",
            (security_membership_id,),
        ).fetchone()
        assert security is not None

    expires = datetime.now(UTC) + timedelta(hours=1)

    def platform_context(
        token: str,
        operator_id: str,
        account_id: str,
        roles: tuple[str, ...],
        permissions: tuple[str, ...],
    ) -> PlatformContext:
        marker = token[0]
        return PlatformContext(
            platform_session_id="pts_" + marker * 32,
            operator_id=operator_id,
            account_id=account_id,
            global_session_id="gss_" + marker * 32,
            factor_id="mfa_" + marker * 32,
            factor_revision=1,
            operator_revision=1,
            roles=roles,
            permissions=permissions,
            expires_at=expires,
        )

    def workspace_context(row, token: str) -> WorkspaceSessionContext:
        role = str(row["role_code"])
        return WorkspaceSessionContext(
            workspace_session_id="wss_" + token[0] * 32,
            global_session_id="gss_" + token[0] * 32,
            account_id=str(row["account_id"]),
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            membership_id=str(row["workspace_membership_id"]),
            legacy_user_id=str(row["legacy_user_id"]),
            membership_role=role,
            legacy_role="admin" if role == "owner" else role,
            account_email=str(row["email_normalized"]),
            display_name=str(row["display_name"]),
            tenant_slug=str(row["tenant_slug"]),
            tenant_name=str(row["tenant_name"]),
            permission_codes=("security_assurance.accept", "security_assurance.read"),
            permission_revision=1,
            authorization_fingerprint=hashlib.sha256(token.encode("ascii")).hexdigest(),
            expires_at=expires,
            token=token,
        )

    owner_context = workspace_context(owner, WORKSPACE_OWNER_TOKEN)
    security_context = workspace_context(security, WORKSPACE_SECURITY_TOKEN)
    foreign_context = replace(
        owner_context,
        workspace_session_id="wss_" + "f" * 32,
        workspace_id="wsp_" + "f" * 32,
        tenant_id="foreign-tenant",
        membership_id="wmb_" + "f" * 32,
        token=WORKSPACE_FOREIGN_TOKEN,
    )
    return Runtime(
        config=config,
        workspace_id=str(owner["workspace_id"]),
        tenant_id=str(owner["tenant_id"]),
        platform_contexts={
            PLATFORM_A_TOKEN: platform_context(
                PLATFORM_A_TOKEN,
                OPERATOR_A_ID,
                str(owner["account_id"]),
                roles_a,
                permissions_a,
            ),
            PLATFORM_B_TOKEN: platform_context(
                PLATFORM_B_TOKEN,
                OPERATOR_B_ID,
                security_account_id,
                roles_b,
                permissions_b,
            ),
            PLATFORM_C_TOKEN: platform_context(
                PLATFORM_C_TOKEN,
                OPERATOR_C_ID,
                ACCOUNT_C_ID,
                roles_c,
                permissions_c,
            ),
        },
        workspace_contexts={
            WORKSPACE_OWNER_TOKEN: owner_context,
            WORKSPACE_SECURITY_TOKEN: security_context,
            WORKSPACE_FOREIGN_TOKEN: foreign_context,
        },
    )


def _app(monkeypatch: pytest.MonkeyPatch, runtime: Runtime, runner):
    def platform_actor(connection, _config, *, session_token, **_kwargs):
        context = runtime.platform_contexts.get(session_token)
        if context is None:
            raise ApiError("platform.session_required")
        roles, permissions, _fingerprint = platform_access_service._authority(
            connection, context.operator_id
        )
        return replace(context, roles=roles, permissions=permissions)

    def workspace_actor(_connection, _config, *, session_token, **_kwargs):
        context = runtime.workspace_contexts.get(session_token)
        if context is None:
            raise ApiError("workspace_session.required")
        return context

    monkeypatch.setattr(service, "authenticate_platform_session_in_transaction", platform_actor)
    monkeypatch.setattr(service, "authenticate_workspace_session_in_transaction", workspace_actor)
    monkeypatch.setattr(
        service,
        "consume_global_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: "mfa",
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-137 Security Assurance Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = runtime.config
    app.state.security_assurance_runner = runner
    app.include_router(assurance_router)
    return app


def _platform_headers(token: str, key: str | None = None, revision: int = 1) -> dict[str, str]:
    headers = {"Cookie": f"{PLATFORM_SESSION_COOKIE}={token}"}
    if key is not None:
        headers.update(
            {
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={token}; "
                    f"{PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
                ),
                "Idempotency-Key": key,
                "If-Match": f'"{revision}"',
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-site",
                "X-CSRF-Token": PLATFORM_CSRF,
                "X-Step-Up-Grant": STEP_UP,
            }
        )
    return headers


def _assessment_body(workspace_id: str, *, extra: dict[str, object] | None = None):
    body: dict[str, object] = {
        "workspace_id": workspace_id,
        "profile_code": "incremental_release",
        "profile_version": 1,
        "code_tree_sha256": "1" * 64,
        "migration_set_sha256": "2" * 64,
        "contract_set_sha256": "3" * 64,
        "artifact_sha256": "4" * 64,
        "confirmed": True,
    }
    body.update(extra or {})
    return body


def _passed_results(request) -> tuple[ScenarioExecution, ...]:
    return tuple(
        ScenarioExecution(
            scenario_code=scenario.scenario_code,
            outcome="passed",
            duration_ms=2,
            evidence_sha256=hashlib.sha256(
                f"{request.security_assessment_id}:{scenario.scenario_code}:passed".encode()
            ).hexdigest(),
        )
        for scenario in request.scenarios
    )


def _passed_runner(request, lease=None) -> bytes:
    if lease is not None:
        assert lease.check()
    return sign_receipt(synthetic_receipt(request, _passed_results(request)))


def _failed_runner(request, lease=None) -> bytes:
    if lease is not None:
        assert lease.check()
    results = list(_passed_results(request))
    first = request.scenarios[0]
    results[0] = ScenarioExecution(
        scenario_code=first.scenario_code,
        outcome="failed",
        severity="p0",
        duration_ms=5,
        evidence_sha256=hashlib.sha256(
            f"{request.security_assessment_id}:{first.scenario_code}:failed".encode()
        ).hexdigest(),
    )
    return sign_receipt(synthetic_receipt(request, results))


async def _create(
    client: AsyncClient,
    runtime: Runtime,
    *,
    token: str,
    key: str,
) -> object:
    return await client.post(
        "/platform/security/assessments",
        headers=_platform_headers(token, key),
        json=_assessment_body(runtime.workspace_id),
    )


@pytest.mark.anyio
async def test_get_assessment_supports_reused_request_connection(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch, runtime, _passed_runner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await _create(
            client,
            runtime,
            token=PLATFORM_A_TOKEN,
            key="prd137-reused-connection-0001",
        )
    assert created.status_code == 201, created.text
    assessment_id = created.json()["assessment"]["security_assessment_id"]

    with database_request_scope(reuse_connections=True):
        response = service.get_security_assessment(
            runtime.config,
            assessment_id,
            platform_session_token=PLATFORM_A_TOKEN,
            request_id="req_prd137_reused_connection",
            trace_id="trc_prd137_reused_connection",
        )

    assert response.assessment.security_assessment_id == assessment_id


@pytest.mark.anyio
async def test_missing_runner_fails_closed(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch, runtime, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await _create(
            client,
            runtime,
            token=PLATFORM_A_TOKEN,
            key="prd137-missing-runner-0001",
        )
    assert response.status_code == 201, response.text
    assessment = response.json()["assessment"]
    assert assessment["status"] == "failed"
    assert len(assessment["runs"]) == 10
    assert len(assessment["findings"]) == 10
    assert {run["outcome"] for run in assessment["runs"]} == {"error"}
    assert assessment["environment_receipt_sha256"] is None
    assert {run["runner_image_sha256"] for run in assessment["runs"]} == {"0" * 64}
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM assurance_cleanup_journal"
        ).fetchone()[0] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["bare_results", "tampered", "wrong_binding", "trust_removed"])
async def test_untrusted_runner_cannot_pass_or_claim_cleanup(runtime, monkeypatch, mode):
    def untrusted_runner(request, lease):
        assert lease.check()
        if mode == "bare_results":
            return _passed_results(request)
        if mode == "wrong_binding":
            return _passed_runner(replace(request, artifact_sha256="f" * 64))
        content = _passed_runner(request)
        if mode == "tampered":
            value = json.loads(content)
            value["receipt"]["runs"][0]["result"]["evidence_sha256"] = "f" * 64
            return json.dumps(value).encode()
        Path(runtime.config.assurance_receipt_trust_bundle_path).write_text(
            '{"schema_version":"metahub.assurance-trust.v1","keys":[]}', encoding="utf-8",
        )
        return content

    app = _app(monkeypatch, runtime, untrusted_runner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await _create(
            client, runtime, token=PLATFORM_A_TOKEN, key="prd137-untrusted-receipt-0001",
        )
    assert response.status_code == 201, response.text
    assessment = response.json()["assessment"]
    assert assessment["status"] == "failed"
    assert assessment["environment_receipt_sha256"] is None
    assert len(assessment["findings"]) == 10
    assert {run["outcome"] for run in assessment["runs"]} == {"error"}
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM assurance_cleanup_journal"
        ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_signed_cleanup_failure_prevents_assessment_pass(runtime, monkeypatch):
    def cleanup_failed_runner(request, lease):
        assert lease.check()
        receipt = synthetic_receipt(request, _passed_results(request))
        run = receipt.runs[0]
        failed = run.fixtures[0].model_copy(update={"cleanup_status": "failed"})
        run = run.model_copy(update={"fixtures": (failed, *run.fixtures[1:])})
        return sign_receipt(receipt.model_copy(update={"runs": (run, *receipt.runs[1:])}))

    app = _app(monkeypatch, runtime, cleanup_failed_runner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await _create(
            client, runtime, token=PLATFORM_A_TOKEN, key="prd137-cleanup-failed-0001",
        )
    assert response.status_code == 201, response.text
    assessment = response.json()["assessment"]
    assert assessment["status"] == "failed"
    assert assessment["environment_receipt_sha256"] is not None
    assert assessment["runs"][0]["outcome"] == "error"
    assert len(assessment["findings"]) == 1
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert dict(connection.execute(
            "SELECT status,COUNT(*) FROM assurance_cleanup_journal GROUP BY status"
        ).fetchall()) == {"failed": 1, "deleted": 23}


@pytest.mark.anyio
async def test_fixed_runner_response_idempotency_and_arbitrary_input_rejection(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch, runtime, _passed_runner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await _create(
            client,
            runtime,
            token=PLATFORM_A_TOKEN,
            key="prd137-pass-assessment-0001",
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assessment = body["assessment"]
        assert assessment["status"] == "passed"
        assert assessment["generation"] == 1
        assert len(assessment["runs"]) == 10
        assert assessment["findings"] == []
        assert created.headers["etag"] == '"3"'
        assert "source_subject" not in created.text
        assert "target_subject" not in created.text

        replay = await _create(
            client,
            runtime,
            token=PLATFORM_A_TOKEN,
            key="prd137-pass-assessment-0001",
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["assessment"]["security_assessment_id"] == assessment[
            "security_assessment_id"
        ]

        fetched = await client.get(
            f"/platform/security/assessments/{assessment['security_assessment_id']}",
            headers=_platform_headers(PLATFORM_A_TOKEN),
        )
        assert fetched.status_code == 200
        assert fetched.headers["cache-control"] == "no-store, private"

        arbitrary = await client.post(
            "/platform/security/assessments",
            headers=_platform_headers(PLATFORM_A_TOKEN, "prd137-arbitrary-input-0001"),
            json=_assessment_body(
                runtime.workspace_id,
                extra={"target_url": "http://127.0.0.1/internal", "script": "select 1"},
            ),
        )
        assert arbitrary.status_code == 400
        assert arbitrary.json()["code"] == "security_assurance.request_invalid"

        mixed = await client.post(
            "/platform/security/assessments",
            headers={
                **_platform_headers(PLATFORM_A_TOKEN, "prd137-mixed-credential-0001"),
                "Authorization": "Bearer forbidden",
            },
            json=_assessment_body(runtime.workspace_id),
        )
        assert mixed.status_code == 400
        assert mixed.json()["code"] == "security_assurance.credential_conflict"

        query = await client.get(
            f"/platform/security/assessments/{assessment['security_assessment_id']}?url=x",
            headers=_platform_headers(PLATFORM_A_TOKEN),
        )
        assert query.status_code == 400

        cookie_conflict = await client.get(
            f"/platform/security/assessments/{assessment['security_assessment_id']}",
            headers={
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={PLATFORM_A_TOKEN}; "
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_OWNER_TOKEN}; "
                    f"{SUPPORT_SESSION_COOKIE}={'z' * 43}"
                )
            },
        )
        assert cookie_conflict.status_code == 400

    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM security_assessments").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM isolation_test_runs").fetchone()[0] == 10
        assert (
            connection.execute("SELECT COUNT(*) FROM assurance_cleanup_journal").fetchone()[0]
            == 24
        )


@pytest.mark.anyio
async def test_finding_requires_new_generation_and_independent_verifier(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch, runtime, _failed_runner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        failed = await _create(
            client,
            runtime,
            token=PLATFORM_A_TOKEN,
            key="prd137-failed-assessment-0001",
        )
        assert failed.status_code == 201, failed.text
        first_assessment = failed.json()["assessment"]
        assert first_assessment["status"] == "failed"
        assert len(first_assessment["findings"]) == 1
        finding = first_assessment["findings"][0]
        assert finding["severity"] == "p0"

        proposed = await client.post(
            f"/platform/security/findings/{finding['pen_test_finding_id']}/remediations",
            headers=_platform_headers(
                PLATFORM_A_TOKEN, "prd137-propose-remediation-0001", finding["revision"]
            ),
            json={
                "operation": "propose",
                "expected_revision": finding["revision"],
                "change_sha256": "9" * 64,
                "confirmed": True,
            },
        )
        assert proposed.status_code == 201, proposed.text
        proposal = proposed.json()["remediation"]
        assert proposal["outcome"] == "proposed"
        assert proposed.json()["finding"]["status"] == "remediation_pending"

        await asyncio.sleep(0.01)
        app.state.security_assurance_runner = _passed_runner
        verification = await _create(
            client,
            runtime,
            token=PLATFORM_B_TOKEN,
            key="prd137-verification-assessment-0001",
        )
        assert verification.status_code == 201, verification.text
        verified_assessment = verification.json()["assessment"]
        assert verified_assessment["generation"] == 2
        assert verified_assessment["status"] == "passed"

        self_verify = await client.post(
            f"/platform/security/findings/{finding['pen_test_finding_id']}/remediations",
            headers=_platform_headers(
                PLATFORM_A_TOKEN, "prd137-self-verify-remediation-0001", 2
            ),
            json={
                "operation": "verify",
                "expected_revision": 2,
                "remediation_id": proposal["remediation_id"],
                "verification_assessment_id": verified_assessment["security_assessment_id"],
                "confirmed": True,
            },
        )
        assert self_verify.status_code == 409
        assert self_verify.json()["code"] == "security_assurance.remediation_conflict"

        closed = await client.post(
            f"/platform/security/findings/{finding['pen_test_finding_id']}/remediations",
            headers=_platform_headers(
                PLATFORM_B_TOKEN, "prd137-verify-remediation-0001", 2
            ),
            json={
                "operation": "verify",
                "expected_revision": 2,
                "remediation_id": proposal["remediation_id"],
                "verification_assessment_id": verified_assessment["security_assessment_id"],
                "confirmed": True,
            },
        )
        assert closed.status_code == 201, closed.text
        assert closed.json()["remediation"]["outcome"] == "verified_passed"
        assert closed.json()["finding"]["status"] == "closed"
        assert closed.headers["etag"] == '"3"'

    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM security_remediations").fetchone()[0] == 2
        assert {
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM security_assurance_events"
            ).fetchall()
        } == {
            "security.assessment_failed",
            "security.finding_created",
            "security.finding_closed",
        }


@pytest.mark.anyio
async def test_workspace_dual_approval_platform_review_and_expiry(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch, runtime, _failed_runner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        failed = await _create(
            client,
            runtime,
            token=PLATFORM_A_TOKEN,
            key="prd137-risk-assessment-0001",
        )
        assert failed.status_code == 201, failed.text
        finding_id = failed.json()["assessment"]["findings"][0]["pen_test_finding_id"]

    expires_at = datetime.now(UTC) + timedelta(days=1)
    requested = service.apply_risk_acceptance(
        runtime.config,
        finding_id,
        RiskAcceptanceCommand(
            operation="request",
            scope_sha256="8" * 64,
            compensating_controls=["network_isolation", "runtime_detection"],
            expires_at=expires_at,
            confirmed=True,
        ),
        workspace_session_token=WORKSPACE_OWNER_TOKEN,
        idempotency_key="prd137-risk-request-0001",
        request_id="request-risk",
        trace_id="trace-risk",
    )
    assert requested.status == "pending_owner"

    with pytest.raises(ApiError) as duplicate_risk:
        service.apply_risk_acceptance(
            runtime.config,
            finding_id,
            RiskAcceptanceCommand(
                operation="request",
                scope_sha256="8" * 64,
                compensating_controls=["network_isolation", "runtime_detection"],
                expires_at=expires_at,
                confirmed=True,
            ),
            workspace_session_token=WORKSPACE_OWNER_TOKEN,
            idempotency_key="prd137-risk-second-request-0001",
            request_id="request-risk-second",
            trace_id="trace-risk-second",
        )
    assert duplicate_risk.value.code == "security_assurance.risk_conflict"

    with pytest.raises(ApiError) as foreign:
        service.apply_risk_acceptance(
            runtime.config,
            finding_id,
            RiskAcceptanceCommand(
                operation="approve",
                expected_revision=1,
                risk_acceptance_id=requested.risk_acceptance_id,
                confirmed=True,
            ),
            workspace_session_token=WORKSPACE_FOREIGN_TOKEN,
            idempotency_key="prd137-risk-foreign-0001",
            request_id="request-foreign",
            trace_id="trace-foreign",
        )
    assert foreign.value.code == "security_assurance.finding_not_found"

    owner_approved = service.apply_risk_acceptance(
        runtime.config,
        finding_id,
        RiskAcceptanceCommand(
            operation="approve",
            expected_revision=1,
            risk_acceptance_id=requested.risk_acceptance_id,
            confirmed=True,
        ),
        workspace_session_token=WORKSPACE_OWNER_TOKEN,
        idempotency_key="prd137-risk-owner-approve-0001",
        request_id="request-owner",
        trace_id="trace-owner",
    )
    assert owner_approved.status == "pending_security"

    security_approved = service.apply_risk_acceptance(
        runtime.config,
        finding_id,
        RiskAcceptanceCommand(
            operation="approve",
            expected_revision=2,
            risk_acceptance_id=requested.risk_acceptance_id,
            confirmed=True,
        ),
        workspace_session_token=WORKSPACE_SECURITY_TOKEN,
        idempotency_key="prd137-risk-security-approve-0001",
        request_id="request-security",
        trace_id="trace-security",
    )
    assert security_approved.status == "pending_platform"

    with pytest.raises(ApiError) as same_reviewer:
        service.apply_risk_acceptance(
            runtime.config,
            finding_id,
            RiskAcceptanceCommand(
                operation="review",
                expected_revision=3,
                risk_acceptance_id=requested.risk_acceptance_id,
                approved=True,
                confirmed=True,
            ),
            platform_session_token=PLATFORM_B_TOKEN,
            platform_csrf_token=PLATFORM_CSRF,
            idempotency_key="prd137-risk-same-reviewer-0001",
            request_id="request-same-reviewer",
            trace_id="trace-same-reviewer",
        )
    assert same_reviewer.value.code == "security_assurance.risk_conflict"

    reviewed = service.apply_risk_acceptance(
        runtime.config,
        finding_id,
        RiskAcceptanceCommand(
            operation="review",
            expected_revision=3,
            risk_acceptance_id=requested.risk_acceptance_id,
            approved=True,
            confirmed=True,
        ),
        platform_session_token=PLATFORM_C_TOKEN,
        platform_csrf_token=PLATFORM_CSRF,
        idempotency_key="prd137-risk-platform-review-0001",
        request_id="request-review",
        trace_id="trace-review",
    )
    assert reviewed.status == "active"
    assert reviewed.revision == 4

    with closing(connect_database(runtime.config.database_url)) as connection:
        assert [
            row[0]
            for row in connection.execute(
                "SELECT action_code FROM security_assurance_action_commands "
                "WHERE result_object_id=? AND action_code LIKE 'risk.%' "
                "ORDER BY result_revision",
                (requested.risk_acceptance_id,),
            ).fetchall()
        ] == [
            "risk.request",
            "risk.owner_approve",
            "risk.security_approve",
            "risk.platform_review",
        ]
        assert connection.execute(
            "SELECT status FROM pen_test_findings WHERE pen_test_finding_id=?", (finding_id,)
        ).fetchone()[0] == "risk_accepted"

    future = expires_at + timedelta(seconds=1)
    monkeypatch.setattr(service, "_utcnow", lambda: future)
    assert service.process_security_assurance_once(
        runtime.config, worker_id="prd137-test-worker"
    ) >= 1
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute(
            "SELECT status FROM security_risk_acceptances WHERE risk_acceptance_id=?",
            (requested.risk_acceptance_id,),
        ).fetchone()[0] == "expired"
        assert connection.execute(
            "SELECT status FROM pen_test_findings WHERE pen_test_finding_id=?", (finding_id,)
        ).fetchone()[0] == "remediation_pending"
        assert connection.execute(
            "SELECT COUNT(*) FROM security_assurance_event_outbox WHERE status='pending'"
        ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_permission_revision_change_fails_late_runner_results(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def revision_changing_runner(request, lease):
        assert lease.check()
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                """UPDATE platform_operators
                      SET revision=revision+1,updated_at='2026-08-31T00:00:01.000Z'
                    WHERE operator_id=?""",
                (OPERATOR_A_ID,),
            )
            connection.commit()
        return _passed_runner(request)

    app = _app(monkeypatch, runtime, revision_changing_runner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await _create(
            client,
            runtime,
            token=PLATFORM_A_TOKEN,
            key="prd137-revision-fencing-0001",
        )
        assert result.status_code == 201, result.text
        assessment = result.json()["assessment"]
        assert assessment["status"] == "failed"
        assert len(assessment["findings"]) == 10
        assert {item["outcome"] for item in assessment["runs"]} == {"error"}

    with closing(connect_database(runtime.config.database_url)) as connection:
        lease = connection.execute("SELECT * FROM assurance_runner_leases").fetchone()
        assert lease["permission_revision"] == 1
        assert connection.execute(
            "SELECT revision FROM platform_operators WHERE operator_id=?", (OPERATOR_A_ID,)
        ).fetchone()[0] == 2


@pytest.mark.anyio
async def test_stale_runner_is_fenced_after_lease_takeover(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def takeover_runner(request, lease):
        assert lease.check()
        now = datetime.now(UTC)
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                """UPDATE assurance_runner_leases
                      SET lease_token_sha256=?,fencing_generation=fencing_generation+1,
                          lease_expires_at=?,updated_at=?
                    WHERE security_assessment_id=?""",
                (
                    "f" * 64,
                    (now + timedelta(minutes=5)).isoformat(timespec="milliseconds"),
                    (now + timedelta(milliseconds=1)).isoformat(timespec="milliseconds"),
                    request.security_assessment_id,
                ),
            )
            connection.commit()
        return _passed_runner(request)

    app = _app(monkeypatch, runtime, takeover_runner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        fenced = await _create(
            client,
            runtime,
            token=PLATFORM_A_TOKEN,
            key="prd137-lease-takeover-0001",
        )
        assert fenced.status_code == 409
        assert fenced.json()["code"] == "security_assurance.runner_fenced"

    with closing(connect_database(runtime.config.database_url)) as connection:
        assessment = connection.execute(
            "SELECT status FROM security_assessments ORDER BY requested_at DESC LIMIT 1"
        ).fetchone()
        assert assessment["status"] == "running"
        assert connection.execute("SELECT COUNT(*) FROM isolation_test_runs").fetchone()[0] == 0
        assert connection.execute(
            "SELECT fencing_generation FROM assurance_runner_leases"
        ).fetchone()[0] == 2


def test_route_surface_is_exact_and_internal_risk_has_no_http_route() -> None:
    methods = {
        (route.path, method)
        for route in routes.router.routes
        for method in route.methods or set()
    }
    assert methods == {
        ("/platform/security/assessments", "POST"),
        ("/platform/security/assessments/{assessment_id}", "GET"),
        ("/platform/security/findings/{finding_id}/remediations", "POST"),
    }
