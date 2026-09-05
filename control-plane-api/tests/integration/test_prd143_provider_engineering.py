"""PRD-143 Provider Engineering service workflows and cutover surface."""

from __future__ import annotations

import json
import math
import secrets
import time
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.app import app as control_app
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.provider_engineering import routes, service
from metahub_control.provider_engineering.models import (
    AdapterCommandRequest,
    ErrorCatalogCommandRequest,
    WorkspaceOverrideCommandRequest,
)
from metahub_control.provider_engineering.routes import router
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    workspace_actor_permissions_in_transaction,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.connection import (
    _close_idle_database_connections,
    database_request_scope,
)
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.provider_adapter_spec import catalog_identity

ORIGIN = "http://localhost:5173"
STAMP = "2026-09-01T08:00:00Z"
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
SDK_ID = "sdr_" + "1" * 32


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
            loaded_at=NOW,
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        identity_session_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        support_session_secret=secrets.token_urlsafe(32),
        approval_hmac_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


def _insert_account_and_operator(
    connection, marker: str, role_code: str
) -> tuple[str, str]:
    account_id = "gac_" + marker * 32
    operator_id = "pto_" + marker * 32
    connection.execute(
        """
        INSERT INTO global_accounts(
          account_id,email_normalized,phone_e164,display_name,status,login_enabled,
          credential_revision,session_revision,mfa_state,failed_login_count,
          locked_until,last_login_at,created_at,updated_at)
        VALUES(?,?,NULL,?,'active',0,1,1,'not_enrolled',0,NULL,NULL,?,?)
        """,
        (
            account_id,
            f"prd143-{marker}@example.invalid",
            f"PRD143 {marker}",
            STAMP,
            STAMP,
        ),
    )
    connection.execute(
        """INSERT INTO platform_operators(
           operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
           VALUES(?,?,'active',1,NULL,?,?)""",
        (operator_id, account_id, STAMP, STAMP),
    )
    connection.execute(
        """INSERT INTO platform_operator_role_bindings(
           operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
           VALUES(?,?,'active',NULL,?,NULL)""",
        (operator_id, role_code, STAMP),
    )
    return account_id, operator_id


def _insert_verified_sdk(connection, engineer_id: str, security_id: str) -> None:
    connection.execute(
        """INSERT INTO developer_contract_signing_keys(
           key_id,purpose,algorithm,public_key_b64,public_key_sha256,status,
           not_before,not_after,created_at)
           VALUES('prd143-contract-key','metahub.public-contract.release/v1','ed25519',
             ? ,?,'active','2026-01-01T00:00:00Z','2030-01-01T00:00:00Z',?)""",
        ("A" * 43, "1" * 64, STAMP),
    )
    connection.execute(
        """INSERT INTO api_versions(
           api_version_id,version,registry_revision,status,revision,created_by_operator_id,
           approved_by_operator_id,published_at,deprecated_at,sunset_at,created_at,updated_at)
           VALUES(?,'14.3.0',1,'published',1,?,?,?,NULL,NULL,?,?)""",
        ("apv_" + "2" * 32, engineer_id, security_id, STAMP, STAMP, STAMP),
    )
    registry = json.dumps({"schema": "prd143", "padding": "x" * 128})
    connection.execute(
        """INSERT INTO api_contract_snapshots(
           api_contract_snapshot_id,api_version_id,public_registry_json,canonical_sha256,
           compatibility_baseline_sha256,compatibility_outcome,breaking_reasons_json,
           producer_revision,source_sha256,build_sha256,status,revision,signing_key_id,
           signature_b64,signature_sha256,approved_by_operator_id,created_at,approved_at)
           VALUES(?,?,?,?,NULL,'compatible','[]',1,?,?,'published',1,
             'prd143-contract-key',?,?,?,?,?)""",
        (
            "acs_" + "3" * 32,
            "apv_" + "2" * 32,
            registry,
            "2" * 64,
            "3" * 64,
            "4" * 64,
            "B" * 86,
            "5" * 64,
            security_id,
            STAMP,
            STAMP,
        ),
    )
    artifact = "a" * 64
    sbom = "b" * 64
    provenance = "c" * 64
    connection.execute(
        """INSERT INTO sdk_releases(
           sdk_release_id,api_contract_snapshot_id,language,sdk_version,runtime_version,
           contract_sha256,source_sha256,build_sha256,artifact_sha256,sbom_sha256,
           provenance_sha256,file_manifest_json,smoke_matrix_json,artifact_receipt_sha256,
           status,revision,signing_key_id,signature_b64,signature_sha256,
           approved_by_operator_id,created_at,published_at)
           VALUES(?,?,'python','14.3.0','python-3.12',?,?,?,?,?,?,'{}','{}',?,
             'published',1,'prd143-contract-key',?,?,?, ?,?)""",
        (
            SDK_ID,
            "acs_" + "3" * 32,
            "6" * 64,
            "7" * 64,
            "8" * 64,
            artifact,
            sbom,
            provenance,
            "9" * 64,
            "C" * 86,
            "d" * 64,
            security_id,
            STAMP,
            STAMP,
        ),
    )
    connection.execute(
        """INSERT INTO release_sbom_reports(
           sbom_report_id,sdk_release_id,artifact_sha256,format,format_version,
           component_count,license_summary_json,vulnerability_summary_json,
           generator_sha256,sbom_sha256,immutable_object_receipt_sha256,generated_at)
           VALUES(?,?,?,'cyclonedx','1.5',1,'{}','{}',?,?,?,?)""",
        ("sbr_" + "4" * 32, SDK_ID, artifact, "e" * 64, sbom, "f" * 64, STAMP),
    )
    connection.execute(
        """INSERT INTO release_build_provenance(
           build_provenance_id,sdk_release_id,artifact_sha256,source_commit_sha256,
           source_tree_sha256,dependency_lock_sha256,builder_identity_code,
           builder_image_sha256,build_invocation_sha256,artifact_set_sha256,
           environment_allowlist_sha256,provenance_sha256,reproducibility_receipt_sha256,
           reproducible,generated_at)
           VALUES(?,?,?,?,?,?,'metahub-release-builder',?,?,?,?,?,?,1,?)""",
        (
            "bpr_" + "5" * 32,
            SDK_ID,
            artifact,
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "4" * 64,
            "5" * 64,
            "6" * 64,
            "7" * 64,
            provenance,
            "8" * 64,
            STAMP,
        ),
    )
    connection.execute(
        """INSERT INTO supply_chain_signing_keys(
           key_id,purpose,algorithm,public_key_b64,public_key_sha256,status,
           not_before,not_after,created_at)
           VALUES('prd143-artifact-key','metahub.artifact.attestation/v1','ed25519',
             ?,?,'active','2026-01-01T00:00:00Z','2030-01-01T00:00:00Z',?)""",
        ("D" * 43, "9" * 64, STAMP),
    )
    connection.execute(
        """INSERT INTO release_artifact_signatures(
           artifact_signature_id,sdk_release_id,purpose,algorithm,key_id,key_version,
           artifact_sha256,sbom_sha256,provenance_sha256,attestation_sha256,
           signature_b64,signature_sha256,verification_outcome,signed_at)
           VALUES(?,?,'metahub.artifact.attestation/v1','ed25519',
             'prd143-artifact-key',1,?,?,?,?,? ,?,'passed',?)""",
        (
            "asi_" + "6" * 32,
            SDK_ID,
            artifact,
            sbom,
            provenance,
            "0" * 64,
            "E" * 86,
            "f" * 64,
            STAMP,
        ),
    )


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database_url = _url(tmp_path / "prd143.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0139"
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
    apply_seed_data(database_url)
    config = _config(database_url)
    with closing(connect_database(database_url)) as connection:
        engineer_account, engineer_id = _insert_account_and_operator(
            connection, "a", "platform_provider_engineer"
        )
        security_account, security_id = _insert_account_and_operator(
            connection, "b", "platform_security"
        )
        release_account, release_id = _insert_account_and_operator(
            connection, "c", "release_manager"
        )
        _insert_verified_sdk(connection, engineer_id, security_id)
        owner = connection.execute(
            """SELECT membership.*,tenant.slug,tenant.name,user.email,user.display_name
               FROM workspace_memberships membership
               JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
               JOIN users user ON user.tenant_id=membership.tenant_id
                 AND user.user_id=membership.legacy_user_id
               WHERE membership.role_code='owner' LIMIT 1"""
        ).fetchone()
        assert owner is not None
        snapshot = workspace_actor_permissions_in_transaction(
            connection,
            account_id=str(owner["account_id"]),
            workspace_id=str(owner["workspace_id"]),
            tenant_id=str(owner["tenant_id"]),
        )
        provider_id = str(
            connection.execute(
                "SELECT provider_id FROM providers WHERE tenant_id=? LIMIT 1",
                (owner["tenant_id"],),
            ).fetchone()[0]
        )
        connection.commit()
    expires = NOW + timedelta(hours=1)
    contexts = {
        "engineer": PlatformContext(
            "pts_" + "a" * 32,
            engineer_id,
            engineer_account,
            "gss_" + "a" * 32,
            "mfa_" + "a" * 32,
            1,
            1,
            ("platform_provider_engineer",),
            (
                "platform.provider_adapter.read",
                "platform.provider_adapter.candidate_manage",
                "platform.provider_adapter.contract_run",
                "platform.provider_adapter.failure_review",
                "platform.error_catalog.read",
                "platform.error_catalog.candidate_manage",
            ),
            expires,
        ),
        "security": PlatformContext(
            "pts_" + "b" * 32,
            security_id,
            security_account,
            "gss_" + "b" * 32,
            "mfa_" + "b" * 32,
            1,
            1,
            ("platform_security",),
            (
                "platform.provider_adapter.read",
                "platform.provider_adapter.failure_review",
                "platform.error_catalog.read",
                "platform.error_catalog.security_approve",
            ),
            expires,
        ),
        "release": PlatformContext(
            "pts_" + "c" * 32,
            release_id,
            release_account,
            "gss_" + "c" * 32,
            "mfa_" + "c" * 32,
            1,
            1,
            ("release_manager",),
            (
                "platform.provider_adapter.read",
                "platform.provider_adapter.release",
                "platform.error_catalog.read",
                "platform.error_catalog.release",
            ),
            expires,
        ),
    }
    workspace = WorkspaceSessionContext(
        workspace_session_id="wss_" + "1" * 32,
        global_session_id="gss_" + "1" * 32,
        account_id=str(owner["account_id"]),
        workspace_id=str(owner["workspace_id"]),
        tenant_id=str(owner["tenant_id"]),
        membership_id=str(owner["workspace_membership_id"]),
        legacy_user_id=str(owner["legacy_user_id"]),
        membership_role="owner",
        legacy_role="admin",
        account_email=str(owner["email"]),
        display_name=str(owner["display_name"]),
        tenant_slug=str(owner["slug"]),
        tenant_name=str(owner["name"]),
        permission_codes=snapshot.permission_codes,
        permission_revision=snapshot.permission_revision,
        authorization_fingerprint=snapshot.authorization_fingerprint,
        expires_at=expires,
        token="workspace",
        legacy_authorization_fingerprint=snapshot.authorization_fingerprint,
    )
    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda *_args, session_token, **_kwargs: contexts[session_token],
    )
    monkeypatch.setattr(
        service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: workspace,
    )
    monkeypatch.setattr(
        service, "consume_global_step_up_grant_in_transaction", lambda *_args, **_kwargs: "totp"
    )
    monkeypatch.setattr(
        service, "consume_step_up_grant_in_transaction", lambda *_args, **_kwargs: "totp"
    )
    try:
        yield config, contexts, workspace, provider_id
    finally:
        _close_idle_database_connections()


def _adapter_command(config, token: str, identifier: str, revision: int, command, key: str):
    return service.command_provider_adapter(
        config,
        session_token=token,
        csrf_token="x" * 43,
        step_up_grant="y" * 43,
        idempotency_key=key,
        expected_revision=revision,
        adapter_identifier=identifier,
        command=command,
        request_id="prd143-request-0001",
        trace_id="prd143-trace-000001",
    )


def _api_app(monkeypatch: pytest.MonkeyPatch, runtime):
    config, contexts, workspace, _provider_id = runtime
    platform_tokens = {
        "engineer": "p" + "a" * 42,
        "security": "p" + "b" * 42,
        "release": "p" + "c" * 42,
    }
    workspace_token = "w" * 43
    live_contexts = dict(contexts)

    def platform_actor(token: str) -> PlatformContext:
        for actor, expected in platform_tokens.items():
            if secrets.compare_digest(token, expected):
                return live_contexts[actor]
        raise ApiError("platform.session_required")

    def workspace_actor(token: str) -> WorkspaceSessionContext:
        if not secrets.compare_digest(token, workspace_token):
            raise ApiError("workspace_session.required")
        return workspace

    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda _connection, _config, *, session_token, **_kwargs: platform_actor(session_token),
    )
    monkeypatch.setattr(
        routes,
        "authenticate_platform_session_context",
        lambda _config, session_token, **_kwargs: platform_actor(session_token),
    )
    monkeypatch.setattr(
        service,
        "authenticate_workspace_session_in_transaction",
        lambda _connection, _config, *, session_token, **_kwargs: workspace_actor(session_token),
    )
    monkeypatch.setattr(
        routes,
        "authenticate_workspace_session_context",
        lambda _config, session_token, **_kwargs: workspace_actor(session_token),
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-143 Provider Engineering Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )

    @app.middleware("http")
    async def reuse_get_database_connections(request, call_next):
        if request.method not in {"GET", "HEAD"}:
            return await call_next(request)
        with database_request_scope(reuse_connections=True):
            return await call_next(request)

    app.state.config = config
    app.include_router(router)
    return app, platform_tokens, workspace_token, live_contexts


def _platform_headers(
    token: str,
    *,
    csrf: str | None = None,
    key: str | None = None,
    revision: int | None = None,
    step_up: bool = True,
) -> dict[str, str]:
    headers = {"Cookie": f"{PLATFORM_SESSION_COOKIE}={token}"}
    if csrf is not None:
        headers.update(
            {
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={token}; {PLATFORM_CSRF_COOKIE}={csrf}"
                ),
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-site",
                "X-CSRF-Token": csrf,
            }
        )
    if key is not None:
        headers["Idempotency-Key"] = key
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    if step_up:
        headers["X-Step-Up-Grant"] = "s" * 43
    return headers


def _workspace_headers(
    token: str, csrf: str, *, key: str, revision: int
) -> dict[str, str]:
    return {
        "Cookie": f"{WORKSPACE_SESSION_COOKIE}={token}; {WORKSPACE_CSRF_COOKIE}={csrf}",
        "Idempotency-Key": key,
        "If-Match": f'"{revision}"',
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": csrf,
        "X-Step-Up-Grant": "s" * 43,
    }


@pytest.mark.anyio
async def test_platform_contract_case_100k_keyset_is_stable_and_meets_slo(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    config, _contexts, _workspace, _provider_id = runtime
    identity = catalog_identity()
    candidate = _adapter_command(
        config,
        "engineer",
        "generic-http",
        0,
        AdapterCommandRequest(
            action="create_candidate",
            sdk_release_id=SDK_ID,
            release_version="14.3.100",
            protocol_family="generic_http",
            spec_manifest_version=identity.version,
            spec_manifest_sha256=identity.catalog_hash,
            compatibility_sha256="1" * 64,
            required_runtime_version="python-3.12",
            required_api_version="14.3.0",
        ),
        "prd143-adapter-perf-create-01",
    )
    adapter_id = candidate.adapter.provider_adapter_release_id
    run_id = "pacr_" + "f" * 32
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """
            INSERT INTO platform_adapter_contract_run_projections(
              contract_run_id,scope,provider_adapter_release_id,adapter_code,
              schema_version,runner_version,suite_kind,mode,source_revision_sha256,
              artifact_sha256,catalog_sha256,status,coverage_complete,total_cases,
              passed_cases,failed_cases,failure_codes_json,report_sha256,
              attestation_sha256,qualifies_release,legacy_owner_sha256,generation,
              security_reviewed_by_operator_id,started_at,completed_at,created_at)
            VALUES(?,'platform_release_ci',?,'generic-http','adapter-contract-v1',
              'provider-runner-v1','release_ci','isolated_runner',?,?,?,'passed',1,
              100000,100000,0,'[]',?,?,1,NULL,1,NULL,?,?,?)
            """,
            (
                run_id,
                adapter_id,
                "2" * 64,
                "a" * 64,
                identity.catalog_hash,
                "3" * 64,
                "4" * 64,
                STAMP,
                STAMP,
                STAMP,
            ),
        )
        connection.execute(
            """
            WITH RECURSIVE case_number(value) AS(
              SELECT 1 UNION ALL SELECT value+1 FROM case_number WHERE value<100000)
            INSERT INTO platform_adapter_contract_case_projections(
              contract_case_id,contract_run_id,ordinal,case_key_sha256,case_kind,
              variant,provider_code,adapter_code,status,failure_code,duration_bucket,
              evidence_sha256,created_at)
            SELECT 'pacc_'||printf('%032x',value),?,value,printf('%064x',value),
              CASE value%5 WHEN 0 THEN 'request' WHEN 1 THEN 'non_stream'
                WHEN 2 THEN 'stream' WHEN 3 THEN 'error' ELSE 'usage' END,
              'standard','generic-http','generic-http','passed',NULL,'lt_100ms',
              printf('%064x',100000+value),?
            FROM case_number
            """,
            (run_id, STAMP),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_adapter_contract_case_projections "
            "WHERE contract_run_id=?",
            (run_id,),
        ).fetchone()[0] == 100_000
        plan = " ".join(
            str(row[3])
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT case_row.*
                FROM platform_adapter_contract_case_projections case_row
                INDEXED BY idx_platform_contract_cases_page_prd143
                WHERE case_row.contract_run_id=?
                  AND (case_row.ordinal,case_row.contract_case_id)>(?,?)
                ORDER BY case_row.ordinal,case_row.contract_case_id LIMIT ?
                """,
                (run_id, 50_000, "pacc_" + f"{50000:032x}", 501),
            ).fetchall()
        )
        assert "idx_platform_contract_cases_page_prd143" in plan
        assert "USE TEMP B-TREE" not in plan
        connection.commit()

    app, platform_tokens, _workspace_token, _live_contexts = _api_app(
        monkeypatch, runtime
    )
    statements: list[str] = []
    original_connect = service._connect

    def traced_connect(inner_config: AppConfig):
        connection = original_connect(inner_config)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(service, "_connect", traced_connect)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:

        async def walk() -> tuple[list[str], list[float]]:
            cursor = None
            case_ids: list[str] = []
            durations: list[float] = []
            while True:
                params = {"view": "cases", "run_id": run_id, "limit": "500"}
                if cursor is not None:
                    params["cursor"] = cursor
                started = time.perf_counter()
                response = await client.get(
                    f"/platform/provider-adapters/{adapter_id}/contract-runs",
                    params=params,
                    headers=_platform_headers(platform_tokens["engineer"]),
                )
                durations.append(time.perf_counter() - started)
                assert response.status_code == 200, response.text
                body = response.json()
                case_ids.extend(item["contract_case_id"] for item in body["cases"])
                cursor = body["next_cursor"]
                if cursor is None:
                    return case_ids, durations

        first_ids, first_durations = await walk()
        second_ids, second_durations = await walk()

    expected_ids = ["pacc_" + f"{value:032x}" for value in range(1, 100_001)]
    assert first_ids == expected_ids
    assert second_ids == expected_ids
    assert len(set(first_ids)) == 100_000
    durations = sorted(first_durations + second_durations)
    p95 = durations[math.ceil(len(durations) * 0.95) - 1]
    assert p95 < 0.500, {"p95_seconds": p95, "slowest_seconds": durations[-1]}

    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and statement.strip().rstrip(";").upper() != "SELECT 1"
    ]
    assert len(first_durations) == len(second_durations) == 200
    assert len(selects) == 3 * (len(first_durations) + len(second_durations))
    normalized_selects = "\n".join(selects).upper()
    assert "COUNT(" not in normalized_selects
    assert "JSON_" not in normalized_selects


def test_adapter_catalog_and_workspace_override_workflows(runtime) -> None:
    config, _contexts, workspace, provider_id = runtime
    identity = catalog_identity()
    candidate = _adapter_command(
        config,
        "engineer",
        "generic-http",
        0,
        AdapterCommandRequest(
            action="create_candidate",
            sdk_release_id=SDK_ID,
            release_version="14.3.0",
            protocol_family="generic_http",
            spec_manifest_version=identity.version,
            spec_manifest_sha256=identity.catalog_hash,
            compatibility_sha256="1" * 64,
            required_runtime_version="python-3.12",
            required_api_version="14.3.0",
        ),
        "prd143-adapter-create-0001",
    )
    adapter_id = candidate.adapter.provider_adapter_release_id
    queued = _adapter_command(
        config,
        "engineer",
        adapter_id,
        1,
        AdapterCommandRequest(action="run_contract"),
        "prd143-adapter-run-000001",
    )
    assert queued.contract_run_id and queued.adapter.status == "testing"
    assert service.run_next_contract_command(config) is True
    reviewed = _adapter_command(
        config,
        "security",
        adapter_id,
        3,
        AdapterCommandRequest(
            action="review_failure",
            contract_run_id=queued.contract_run_id,
            failure_disposition="approve_remediation",
        ),
        "prd143-adapter-review-001",
    )
    assert reviewed.adapter.status == "approved"
    released = _adapter_command(
        config,
        "release",
        adapter_id,
        4,
        AdapterCommandRequest(action="release"),
        "prd143-adapter-release-01",
    )
    assert released.adapter.status == "released" and released.adapter.is_head

    catalog = service.command_error_catalog(
        config,
        session_token="engineer",
        csrf_token="x" * 43,
        step_up_grant="y" * 43,
        idempotency_key="prd143-catalog-create-0001",
        expected_revision=1,
        command=ErrorCatalogCommandRequest(
            action="create_candidate",
            builtin_catalog_version="provider-error-builtin-v1",
            sdk_release_id=SDK_ID,
            signed_candidate_id="signed-candidate-prd143-v1",
        ),
        request_id="prd143-request-0002",
        trace_id="prd143-trace-000002",
    )
    catalog_id = catalog.catalog.error_catalog_release_id
    revision = 1
    for token, action, key in (
        ("engineer", "submit_review", "prd143-catalog-review-0001"),
        ("security", "security_approve", "prd143-catalog-approve-001"),
        ("release", "release", "prd143-catalog-release-001"),
    ):
        catalog = service.command_error_catalog(
            config,
            session_token=token,
            csrf_token="x" * 43,
            step_up_grant="y" * 43,
            idempotency_key=key,
            expected_revision=revision,
            command=ErrorCatalogCommandRequest(
                action=action,
                error_catalog_release_id=catalog_id,
                expected_revision=revision,
            ),
            request_id=f"prd143-request-{revision + 2:04d}",
            trace_id=f"prd143-trace-{revision + 2:06d}",
        )
        revision += 1
    assert catalog.catalog.status == "released" and catalog.catalog.is_head

    created = service.command_workspace_error_override(
        config,
        session_token="workspace",
        csrf_token="x" * 43,
        step_up_grant="y" * 43,
        idempotency_key="prd143-override-create-001",
        expected_head_revision=0,
        command=WorkspaceOverrideCommandRequest(
            action="create",
            provider_id=provider_id,
            priority=500,
            http_statuses=(418,),
            http_status_class=None,
            error_field="error.code",
            field_operator="equals",
            field_pattern="vendor_teapot",
            output_error_code="gateway.upstream_request_invalid",
        ),
        request_id="prd143-request-0010",
        trace_id="prd143-trace-000010",
    )
    assert created.changed and created.head.revision == 1
    assert created.compatibility.provider_id == provider_id
    with closing(connect_database(config.database_url)) as connection:
        override_id = created.override.provider_error_override_id
        assert connection.execute(
            "SELECT current_version FROM provider_error_maps WHERE provider_error_map_id=?",
            (override_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            """SELECT current_version FROM workspace_provider_error_overrides
               WHERE workspace_id=? AND provider_error_override_id=?""",
            (workspace.workspace_id, override_id),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM provider_engineering_events"
        ).fetchone()[0] >= 3


@pytest.mark.anyio
async def test_api_credentials_revocation_write_guards_and_cross_owner(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    config, contexts, workspace, provider_id = runtime
    app, platform_tokens, workspace_token, live_contexts = _api_app(monkeypatch, runtime)
    csrf = "c" * 43
    identity = catalog_identity()
    candidate_body = {
        "action": "create_candidate",
        "sdk_release_id": SDK_ID,
        "release_version": "14.3.1",
        "protocol_family": "generic_http",
        "spec_manifest_version": identity.version,
        "spec_manifest_sha256": identity.catalog_hash,
        "compatibility_sha256": "1" * 64,
        "required_runtime_version": "python-3.12",
        "required_api_version": "14.3.0",
    }
    engineer_token = platform_tokens["engineer"]
    platform_write = _platform_headers(
        engineer_token,
        csrf=csrf,
        key="prd143-api-adapter-create-001",
        revision=0,
    )

    with closing(connect_database(config.database_url)) as connection:
        cross_tenant_id = "tenant-prd143-cross-owner"
        cross_provider_id = "00000000-0000-4000-8000-000000000143"
        connection.execute(
            """INSERT INTO tenants(
               tenant_id,slug,name,service_mode,status,created_at,updated_at)
               VALUES(?,?,'PRD143 Cross Owner','internal_only','active',?,?)""",
            (cross_tenant_id, "prd143-cross-owner", STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO providers(
               provider_id,tenant_id,provider_code,name,base_url,status,created_at,updated_at,
               provider_type,auth_type)
               SELECT ?,?,'cross-owner','Cross Owner',base_url,'active',?,?,provider_type,auth_type
               FROM providers WHERE tenant_id=? AND provider_id=?""",
            (
                cross_provider_id,
                cross_tenant_id,
                STAMP,
                STAMP,
                workspace.tenant_id,
                provider_id,
            ),
        )
        connection.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        mixed_platform = await client.get(
            "/platform/provider-adapters",
            headers={
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={engineer_token}; "
                    f"{WORKSPACE_SESSION_COOKIE}={workspace_token}"
                )
            },
        )
        assert mixed_platform.status_code == 400
        assert mixed_platform.json()["code"] == "platform.credential_conflict"

        mixed_workspace = await client.get(
            "/workspace/provider-error-overrides",
            headers={
                "Cookie": (
                    f"{WORKSPACE_SESSION_COOKIE}={workspace_token}; "
                    f"{PLATFORM_SESSION_COOKIE}={engineer_token}"
                )
            },
        )
        assert mixed_workspace.status_code == 400
        assert mixed_workspace.json()["code"] == "workspace_session.credential_conflict"

        live_contexts["engineer"] = replace(contexts["engineer"], permissions=())
        revoked = await client.get(
            "/platform/provider-adapters",
            headers=_platform_headers(engineer_token),
        )
        assert revoked.status_code == 403
        assert revoked.json()["code"] == "platform.permission_denied"
        live_contexts["engineer"] = contexts["engineer"]

        missing_csrf_headers = _platform_headers(engineer_token)
        missing_csrf_headers.update({"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"})
        missing_csrf = await client.post(
            "/platform/provider-adapters/generic-http/commands",
            headers=missing_csrf_headers,
            json=candidate_body,
        )
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["code"] == "platform.csrf_invalid"

        wrong_origin_headers = dict(platform_write)
        wrong_origin_headers["Origin"] = "https://cross-origin.invalid"
        wrong_origin = await client.post(
            "/platform/provider-adapters/generic-http/commands",
            headers=wrong_origin_headers,
            json=candidate_body,
        )
        assert wrong_origin.status_code == 403
        assert wrong_origin.json()["code"] == "platform.origin_invalid"

        missing_step_up = await client.post(
            "/platform/provider-adapters/generic-http/commands",
            headers=_platform_headers(
                engineer_token,
                csrf=csrf,
                key="prd143-api-missing-step-up",
                revision=0,
                step_up=False,
            ),
            json=candidate_body,
        )
        assert missing_step_up.status_code == 403
        assert missing_step_up.json()["code"] == "step_up.invalid"

        unknown_field = await client.post(
            "/platform/provider-adapters/generic-http/commands",
            headers=platform_write,
            json={**candidate_body, "runner_path": "forbidden"},
        )
        assert unknown_field.status_code == 422
        assert unknown_field.json()["code"] == "platform.request_invalid"

        missing_key = await client.post(
            "/platform/provider-adapters/generic-http/commands",
            headers=_platform_headers(engineer_token, csrf=csrf, revision=0),
            json=candidate_body,
        )
        assert missing_key.status_code == 409
        assert missing_key.json()["code"] == "platform.idempotency_conflict"

        missing_revision = await client.post(
            "/platform/provider-adapters/generic-http/commands",
            headers=_platform_headers(
                engineer_token, csrf=csrf, key="prd143-api-missing-revision"
            ),
            json=candidate_body,
        )
        assert missing_revision.status_code == 412
        assert missing_revision.json()["code"] == "platform.revision_conflict"

        created = await client.post(
            "/platform/provider-adapters/generic-http/commands",
            headers=platform_write,
            json=candidate_body,
        )
        assert created.status_code == 201, created.text
        assert created.headers["etag"] == '"1"'
        assert created.headers["cache-control"] == "no-store, private"
        adapter_id = created.json()["adapter"]["provider_adapter_release_id"]

        replay = await client.post(
            "/platform/provider-adapters/generic-http/commands",
            headers=platform_write,
            json=candidate_body,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        changed_replay = await client.post(
            "/platform/provider-adapters/generic-http/commands",
            headers=platform_write,
            json={**candidate_body, "release_version": "14.3.2"},
        )
        assert changed_replay.status_code == 409
        assert changed_replay.json()["code"] == "platform.idempotency_conflict"

        wrong_revision = await client.post(
            f"/platform/provider-adapters/{adapter_id}/commands",
            headers=_platform_headers(
                engineer_token,
                csrf=csrf,
                key="prd143-api-wrong-revision",
                revision=0,
            ),
            json={"action": "run_contract"},
        )
        assert wrong_revision.status_code == 412
        assert wrong_revision.json()["code"] == "platform.revision_conflict"

        invalid_cursor = await client.get(
            "/platform/provider-adapters?cursor=not-a-valid-cursor",
            headers=_platform_headers(engineer_token),
        )
        assert invalid_cursor.status_code == 422
        assert invalid_cursor.json()["code"] == "platform.request_invalid"

        override_body = {
            "action": "create",
            "provider_id": cross_provider_id,
            "priority": 500,
            "http_statuses": [418],
            "http_status_class": None,
            "error_field": "error.code",
            "field_operator": "equals",
            "field_pattern": "vendor_teapot",
            "output_error_code": "gateway.upstream_request_invalid",
        }
        wrong_numeric_type = await client.patch(
            "/workspace/provider-error-overrides",
            headers=_workspace_headers(
                workspace_token,
                csrf,
                key="prd143-api-wrong-numeric-type",
                revision=0,
            ),
            json={**override_body, "priority": "500"},
        )
        assert wrong_numeric_type.status_code == 422
        assert wrong_numeric_type.json()["code"] == "error_map.input_invalid"

        cross_owner = await client.patch(
            "/workspace/provider-error-overrides",
            headers=_workspace_headers(
                workspace_token,
                csrf,
                key="prd143-api-cross-owner-001",
                revision=0,
            ),
            json=override_body,
        )
        assert cross_owner.status_code == 404
        assert cross_owner.json()["code"] == "error_map.not_found"

        workspace_headers = _workspace_headers(
            workspace_token,
            csrf,
            key="prd143-api-override-create-001",
            revision=0,
        )
        override_body["provider_id"] = provider_id
        override = await client.patch(
            "/workspace/provider-error-overrides",
            headers=workspace_headers,
            json=override_body,
        )
        assert override.status_code == 201, override.text
        assert override.headers["etag"] == '"1"'
        assert override.json()["compatibility"]["provider_id"] == provider_id

        override_replay = await client.patch(
            "/workspace/provider-error-overrides",
            headers=workspace_headers,
            json=override_body,
        )
        assert override_replay.status_code == 200, override_replay.text
        assert override_replay.json()["idempotent_replay"] is True


@pytest.mark.anyio
async def test_runtime_unloads_legacy_external_routes() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=control_app), base_url="http://testserver"
    ) as client:
        for path in (
            "/adapter-specs",
            "/adapter-contract-runs",
            "/adapter-contract-runs/acr_" + "1" * 32,
            "/error-maps",
            "/error-maps/pem_" + "1" * 32,
        ):
            response = await client.get(
                path, headers={"X-Request-ID": "prd143-retired-route"}
            )
            assert response.status_code == 404, path
            assert response.json()["code"] == "infra.not_found"

        platform = await client.get("/platform/provider-adapters")
        workspace = await client.get("/workspace/provider-error-overrides")
        internal = await client.get("/internal/error-maps/tenant-prd143/current")

    assert platform.status_code == 401
    assert platform.json()["code"] == "platform.session_required"
    assert workspace.status_code == 401
    assert workspace.json()["code"] == "workspace_session.required"
    assert internal.status_code != 404
