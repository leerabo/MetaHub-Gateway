"""PRD-097 Workspace onboarding facts, mutations, and activation transitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.environments.routes import router as environments_router
from metahub_control.identity.routes import router as identity_router
from metahub_control.workspace_access.constants import WORKSPACE_CSRF_COOKIE
from metahub_control.workspace_access.routes import router as workspace_access_router
from metahub_control.workspace_onboarding.routes import router as onboarding_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import encrypt_provider_key
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ADMIN_EMAIL = "admin@metahub.local"
ADMIN_PASSWORD = "LocalAdmin!2026"
ENCRYPTION_SECRET = "test-prd097-provider-encryption-secret"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    database_url = f"sqlite:///{(tmp_path / 'workspace-onboarding.sqlite3').as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_AUTH_TOKEN_SECRET": "test-prd097-legacy-token-secret",
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": ENCRYPTION_SECRET,
        "METAHUB_IDENTITY_SESSION_SECRET": "test-prd097-global-session-secret-2026",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-prd097-identity-challenge-secret-2026",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": "test-prd097-identity-pii-secret-2026",
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-prd097-workspace-session-secret-2026",
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    apply_migrations(database_url)
    apply_seed_data(database_url)
    app = create_service_app(
        "control-plane-api",
        "PRD-097 Workspace Onboarding Test API",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag",),
    )
    app.include_router(identity_router)
    app.include_router(workspace_access_router)
    app.include_router(onboarding_router)
    app.include_router(environments_router)
    return database_url, app


def _origin_headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site", **extra}


def _mutation_headers(client: AsyncClient, revision: int, key: str) -> dict[str, str]:
    csrf = client.cookies.get(WORKSPACE_CSRF_COOKIE)
    assert csrf
    return _origin_headers(
        **{
            "X-CSRF-Token": csrf,
            "If-Match": f'"{revision}"',
            "Idempotency-Key": key,
        }
    )


async def _enter_workspace(client: AsyncClient) -> str:
    logged_in = await client.post(
        "/identity/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        headers=_origin_headers(),
    )
    assert logged_in.status_code == 200, logged_in.text
    listed = await client.get("/identity/workspaces")
    assert listed.status_code == 200, listed.text
    workspace_id = str(listed.json()["workspaces"][0]["workspace_id"])
    csrf = client.cookies.get(GLOBAL_CSRF_COOKIE)
    assert csrf
    switched = await client.post(
        "/identity/workspace-switches",
        json={"workspace_id": workspace_id},
        headers=_origin_headers(**{"X-CSRF-Token": csrf}),
    )
    assert switched.status_code == 200, switched.text
    return workspace_id


async def _step_action(
    client: AsyncClient,
    step_id: str,
    *,
    action: str,
    reason_code: str,
    revision: int,
    key: str,
):
    return await client.patch(
        f"/workspaces/current/onboarding/steps/{step_id}",
        json={"action": action, "reason_code": reason_code},
        headers=_mutation_headers(client, revision, key),
    )


def _insert_verified_resource_chain(database_url: str) -> None:
    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """
            SELECT member.tenant_id,member.legacy_user_id,origin.environment_id
            FROM workspace_memberships member
            JOIN workspace_environment_origins origin
              ON origin.workspace_id=member.workspace_id
             AND origin.tenant_id=member.tenant_id
            JOIN environments environment
              ON environment.tenant_id=origin.tenant_id
             AND environment.environment_id=origin.environment_id
            WHERE member.role_code='owner' AND member.status='active'
              AND environment.code='dev' AND environment.status='active'
            """
        ).fetchone()
        assert owner is not None
        tenant_id = str(owner["tenant_id"])
        user_id = str(owner["legacy_user_id"])
        environment_id = str(owner["environment_id"])
        resource_time = "2030-01-01T00:00:00Z"
        success_time = "2030-01-02T00:00:00Z"
        request_time = "2030-02-01T00:00:00Z"
        provider_id = "provider_prd097_verified"
        credential_id = "credential_prd097_verified"
        model_id = "model_prd097_verified"
        alias_id = "alias_prd097_verified"
        project_id = "project_prd097_verified"
        application_id = "application_prd097_verified"
        virtual_key_id = "virtual_key_prd097_verified"
        request_id = "request_prd097_verified"
        trace_id = "trace_prd097_verified"
        connection.execute(
            """
            INSERT INTO providers(
              provider_id,tenant_id,provider_code,name,base_url,status,created_at,updated_at,
              provider_type,auth_type,health_status,last_tested_at,created_by,last_success_at,
              last_probe_latency_ms,last_probe_http_status,consecutive_probe_failures,
              health_generation,health_updated_at)
            VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,?,?,25,200,0,1,?)
            """,
            (
                provider_id,
                tenant_id,
                "prd097-verified",
                "Verified provider",
                "https://provider.invalid/v1",
                resource_time,
                resource_time,
                "openai_compatible",
                "bearer",
                "healthy",
                success_time,
                user_id,
                success_time,
                success_time,
            ),
        )
        connection.execute(
            """
            INSERT INTO provider_credentials(
              provider_credential_id,tenant_id,provider_id,key_fingerprint,encrypted_key,
              name,key_prefix,weight,status,created_by,created_at,updated_at,
              probe_health_status,probe_last_tested_at,probe_last_success_at)
            VALUES(?,?,?,?,?,?,?,100,'active',?,?,?,'healthy',?,?)
            """,
            (
                credential_id,
                tenant_id,
                provider_id,
                "a" * 64,
                encrypt_provider_key(
                    "local-test-provider-value", encryption_secret=ENCRYPTION_SECRET
                ),
                "Verified credential",
                "local-test",
                user_id,
                resource_time,
                resource_time,
                success_time,
                success_time,
            ),
        )
        connection.execute(
            """
            INSERT INTO models(
              model_id,tenant_id,provider_id,model_alias,upstream_model_name,capabilities,
              status,created_at,updated_at,provider_model_id,display_name,created_by)
            VALUES(?,?,?,?,?,?,'active',?,?,?,?,?)
            """,
            (
                model_id,
                tenant_id,
                provider_id,
                "prd097-model",
                "prd097-upstream",
                json.dumps({"chat": True}),
                resource_time,
                resource_time,
                "prd097-upstream",
                "Verified model",
                user_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id,tenant_id,alias,environment,default_model_id,status,
              created_by,created_at,updated_at,environment_id)
            VALUES(?,?,'prd097-chat','dev',?,'active',?,?,?,?)
            """,
            (alias_id, tenant_id, model_id, user_id, resource_time, resource_time, environment_id),
        )
        connection.execute(
            """
            INSERT INTO projects(
              project_id,tenant_id,project_code,name,status,created_by,updated_by,
              created_at,updated_at)
            VALUES(?,?,'prd097','Verified project','active',?,?,?,?)
            """,
            (project_id, tenant_id, user_id, user_id, resource_time, resource_time),
        )
        connection.execute(
            """
            INSERT INTO applications(
              application_id,tenant_id,project_id,application_code,name,status,
              created_by,updated_by,created_at,updated_at)
            VALUES(?,?,?,'prd097','Verified application','active',?,?,?,?)
            """,
            (
                application_id,
                tenant_id,
                project_id,
                user_id,
                user_id,
                resource_time,
                resource_time,
            ),
        )
        connection.execute(
            """
            INSERT INTO virtual_keys(
              virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at,
              environment,created_by,project_id,application_id,environment_id)
            VALUES(?,?,?,?,?,'active',?,?,'dev',?,?,?,?)
            """,
            (
                virtual_key_id,
                tenant_id,
                "Verified key",
                "mhv_prd097",
                "b" * 64,
                resource_time,
                resource_time,
                user_id,
                project_id,
                application_id,
                environment_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
              policy_snapshot_status,claimed_at)
            VALUES(?,?,?,?,NULL,'disabled',?)
            """,
            (tenant_id, request_id, trace_id, virtual_key_id, request_time),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,status,
              created_at,updated_at,request_model,resolved_model,provider_code,project_id,
              application_id,environment_id,environment_code)
            VALUES(?,?,?,?,?,?,'success',?,?,?,?,?,?,?,?,?)
            """,
            (
                request_id,
                tenant_id,
                trace_id,
                virtual_key_id,
                provider_id,
                model_id,
                request_time,
                request_time,
                "prd097-chat",
                "prd097-upstream",
                "prd097-verified",
                project_id,
                application_id,
                environment_id,
                "dev",
            ),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger(
              usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,raw_usage,
              normalized_usage,cost_amount,currency,created_at,status,total_tokens,cost_cny,
              cost_usd,estimated,usage_source,updated_at,model_id,provider_id,virtual_key_id,
              request_model,resolved_model,provider_code,project_id,application_id,
              environment_id,environment_code)
            VALUES('usage_prd097_verified',?,?,1,1,'{}','{}',0,'CNY',?,'recorded',2,0,0,0,
              'upstream',?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tenant_id,
                request_id,
                request_time,
                request_time,
                model_id,
                provider_id,
                virtual_key_id,
                "prd097-chat",
                "prd097-upstream",
                "prd097-verified",
                project_id,
                application_id,
                environment_id,
                "dev",
            ),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.anyio
async def test_workspace_onboarding_strict_mutations_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _enter_workspace(client)
        initial = await client.get("/workspaces/current/onboarding")
        assert initial.status_code == 200, initial.text
        body = initial.json()
        assert body["completed_steps"] == 0
        assert body["revision"] == 1
        assert len(body["steps"]) == 8
        assert body["steps"][0]["status"] == "current"
        assert body["steps"][2]["source"] == "legacy_seed"

        mixed = await client.get(
            "/workspaces/current/onboarding", headers={"Authorization": "Bearer local-test"}
        )
        assert mixed.status_code == 400
        assert mixed.json()["code"] == "workspace_session.credential_conflict"

        environments = await client.get("/environments")
        assert environments.status_code == 200, environments.text
        resource_mixed = await client.get(
            "/environments", headers={"Authorization": "Bearer local-test"}
        )
        assert resource_mixed.status_code == 400
        assert resource_mixed.json()["code"] == "workspace_session.credential_conflict"
        resource_no_origin = await client.post(
            "/environments",
            json={"code": "test", "name": "Test", "description": "PRD-097"},
            headers={"X-CSRF-Token": client.cookies.get(WORKSPACE_CSRF_COOKIE)},
        )
        assert resource_no_origin.status_code == 403
        assert resource_no_origin.json()["code"] == "onboarding.origin_invalid"
        resource_no_csrf = await client.post(
            "/environments",
            json={"code": "test", "name": "Test", "description": "PRD-097"},
            headers=_origin_headers(),
        )
        assert resource_no_csrf.status_code == 403
        assert resource_no_csrf.json()["code"] == "workspace_session.csrf_invalid"
        resource_created = await client.post(
            "/environments",
            json={"code": "test", "name": "Test", "description": "PRD-097"},
            headers=_origin_headers(
                **{"X-CSRF-Token": client.cookies.get(WORKSPACE_CSRF_COOKIE)}
            ),
        )
        assert resource_created.status_code == 201, resource_created.text

        invalid_origin = await client.patch(
            "/workspaces/current/onboarding/steps/workspace_profile",
            json={"action": "confirm", "reason_code": "user_confirmed"},
            headers={
                "X-CSRF-Token": client.cookies.get(WORKSPACE_CSRF_COOKIE),
                "If-Match": '"1"',
                "Idempotency-Key": "prd097-origin-invalid",
            },
        )
        assert invalid_origin.status_code == 403
        assert invalid_origin.json()["code"] == "onboarding.origin_invalid"

        invalid_pair = await _step_action(
            client,
            "workspace_profile",
            action="confirm",
            reason_code="do_later",
            revision=1,
            key="prd097-invalid-pair",
        )
        assert invalid_pair.status_code == 422
        assert invalid_pair.json()["code"] == "onboarding.request_invalid"

        profile = await _step_action(
            client,
            "workspace_profile",
            action="confirm",
            reason_code="user_confirmed",
            revision=1,
            key="prd097-confirm-profile",
        )
        assert profile.status_code == 200, profile.text
        assert profile.json()["revision"] == 2

        profile_replay = await _step_action(
            client,
            "workspace_profile",
            action="confirm",
            reason_code="user_confirmed",
            revision=1,
            key="prd097-confirm-profile",
        )
        assert profile_replay.status_code == 200, profile_replay.text
        assert profile_replay.json()["revision"] == 2

        environment = await _step_action(
            client,
            "environment",
            action="confirm",
            reason_code="user_confirmed",
            revision=2,
            key="prd097-confirm-environment",
        )
        assert environment.status_code == 200, environment.text
        assert environment.json()["revision"] == 3

        late_replay = await _step_action(
            client,
            "workspace_profile",
            action="confirm",
            reason_code="user_confirmed",
            revision=1,
            key="prd097-confirm-profile",
        )
        assert late_replay.status_code == 200, late_replay.text
        assert late_replay.json()["revision"] == 3

        changed_replay = await _step_action(
            client,
            "workspace_profile",
            action="confirm",
            reason_code="user_confirmed",
            revision=3,
            key="prd097-confirm-environment",
        )
        assert changed_replay.status_code == 409
        assert changed_replay.json()["code"] == "onboarding.idempotency_conflict"

        stale = await _step_action(
            client,
            "provider",
            action="defer",
            reason_code="do_later",
            revision=2,
            key="prd097-stale-revision",
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "onboarding.revision_conflict"

        reopened = await _step_action(
            client,
            "environment",
            action="reopen",
            reason_code="configuration_changed",
            revision=3,
            key="prd097-reopen-environment",
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["steps"][1]["status"] == "current"
        deferred = await _step_action(
            client,
            "environment",
            action="defer",
            reason_code="do_later",
            revision=4,
            key="prd097-defer-environment",
        )
        assert deferred.status_code == 200, deferred.text
        assert deferred.json()["steps"][1]["status"] == "deferred"
        reconfirmed = await _step_action(
            client,
            "environment",
            action="confirm",
            reason_code="user_confirmed",
            revision=5,
            key="prd097-reconfirm-environment",
        )
        assert reconfirmed.status_code == 200, reconfirmed.text
        second_reopen = await _step_action(
            client,
            "environment",
            action="reopen",
            reason_code="retry_required",
            revision=6,
            key="prd097-second-reopen",
        )
        assert second_reopen.status_code == 200, second_reopen.text
        assert second_reopen.json()["steps"][1]["status"] == "current"

    connection = connect_database(database_url)
    try:
        acknowledgement_count = connection.execute(
            "SELECT COUNT(*) FROM onboarding_acknowledgements"
        ).fetchone()[0]
        assert acknowledgement_count == 6
        assert connection.execute("SELECT COUNT(*) FROM activation_checkpoints").fetchone()[0] == 6
    finally:
        connection.close()


@pytest.mark.anyio
async def test_verified_chain_activation_and_evidence_loss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url, app = _prepare(monkeypatch, tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _enter_workspace(client)
        profile = await _step_action(
            client,
            "workspace_profile",
            action="confirm",
            reason_code="user_confirmed",
            revision=1,
            key="prd097-chain-profile",
        )
        assert profile.status_code == 200, profile.text
        environment = await _step_action(
            client,
            "environment",
            action="confirm",
            reason_code="user_confirmed",
            revision=2,
            key="prd097-chain-environment",
        )
        assert environment.status_code == 200, environment.text
        _insert_verified_resource_chain(database_url)

        live = await client.get("/workspaces/current/onboarding")
        assert live.status_code == 200, live.text
        assert live.json()["completed_steps"] == 8
        assert live.json()["status"] == "active"
        assert all(step["status"] == "complete" for step in live.json()["steps"])
        rendered = json.dumps(live.json(), ensure_ascii=False)
        for forbidden in (
            "provider.invalid",
            "local-test-provider-value",
            "mhv_prd097",
            "prd097-chat",
            "request_prd097_verified",
        ):
            assert forbidden not in rendered

        activated = await client.post(
            "/workspaces/current/onboarding/recheck",
            headers=_mutation_headers(client, 3, "prd097-activate-workspace"),
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["revision"] == 4
        assert activated.json()["status"] == "active"

        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                UPDATE provider_credentials
                SET status='disabled',disabled_at='2030-03-01T00:00:00Z',disabled_by=created_by,
                    updated_at='2030-03-01T00:00:00Z'
                WHERE provider_credential_id='credential_prd097_verified'
                """
            )
            connection.commit()
        finally:
            connection.close()

        degraded = await client.get("/workspaces/current/onboarding")
        assert degraded.status_code == 200, degraded.text
        assert degraded.json()["completed_steps"] == 4
        assert degraded.json()["status"] == "incomplete"
        assert degraded.json()["steps"][2]["status"] == "needs_attention"

        lost = await client.post(
            "/workspaces/current/onboarding/recheck",
            headers=_mutation_headers(client, 4, "prd097-record-activation-loss"),
        )
        assert lost.status_code == 200, lost.text
        assert lost.json()["revision"] == 5
        assert lost.json()["status"] == "incomplete"

    connection = connect_database(database_url)
    try:
        results = [
            row["result"]
            for row in connection.execute(
                "SELECT result FROM activation_checkpoints ORDER BY generation"
            )
        ]
        event_types = {
            row["event_type"]
            for row in connection.execute("SELECT event_type FROM onboarding_events")
        }
        assert results[-2:] == ["activated", "activation_lost"]
        assert "workspace.activated" in event_types
        assert "workspace.activation_lost" in event_types
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
