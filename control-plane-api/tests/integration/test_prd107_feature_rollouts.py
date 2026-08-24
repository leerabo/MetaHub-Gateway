"""PRD-107 strict APIs, approvals, runtime, kill fencing and evaluation order."""

from __future__ import annotations

import json
import secrets
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter_ns

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.feature_rollouts import routes as feature_routes
from metahub_control.feature_rollouts import service as feature_service
from metahub_control.feature_rollouts.routes import router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_jobs.runtime import run_platform_job_once
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_control.workspace_access.service import WorkspaceSessionContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
PLATFORM_TOKEN = "s" * 43
PLATFORM_CSRF = "c" * 43
WORKSPACE_TOKEN = "w" * 43
OPERATOR_IDS = tuple("pto_" + marker * 32 for marker in ("a", "b", "c"))


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd107-feature-base")
    database = root / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0104"
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        source = connection.execute(
            "SELECT * FROM global_accounts ORDER BY account_id LIMIT 1"
        ).fetchone()
        assert source is not None
        for index, operator_id in enumerate(OPERATOR_IDS):
            account_id = "gac_" + f"{index + 10:032x}"
            values = dict(source)
            values.update(
                {
                    "account_id": account_id,
                    "email_normalized": f"feature-operator-{index}@example.invalid",
                    "phone_e164": None,
                    "display_name": f"Feature operator {index}",
                    "status": "active",
                    "login_enabled": 1,
                    "failed_login_count": 0,
                    "locked_until": None,
                    "last_login_at": None,
                }
            )
            columns = tuple(values)
            connection.execute(
                f"INSERT INTO global_accounts({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            connection.execute(
                """
                INSERT INTO platform_operators(
                  operator_id,account_id,status,revision,created_by_operator_id,
                  created_at,updated_at)
                VALUES(?,?,'active',1,NULL,'2026-08-23T00:00:00Z',
                  '2026-08-23T00:00:00Z')
                """,
                (operator_id, account_id),
            )
            connection.execute(
                """
                INSERT INTO platform_operator_role_bindings(
                  operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                VALUES(?,'release_manager','active',NULL,'2026-08-23T00:00:00Z',NULL)
                """,
                (operator_id,),
            )
        connection.commit()
    finally:
        connection.close()
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> tuple[AppConfig, list[PlatformContext]]:
    database = tmp_path / "runtime.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    contexts = [
        _platform_context(operator_id, "gac_" + f"{index + 10:032x}")
        for index, operator_id in enumerate(OPERATOR_IDS)
    ]
    return config, contexts


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


def _platform_context(operator_id: str, account_id: str) -> PlatformContext:
    roles = ("platform_owner",) if operator_id == OPERATOR_IDS[0] else ("release_manager",)
    return PlatformContext(
        platform_session_id="pts_" + operator_id[-1] * 32,
        operator_id=operator_id,
        account_id=account_id,
        global_session_id="gss_" + operator_id[-1] * 32,
        factor_id="mfa_" + operator_id[-1] * 32,
        factor_revision=1,
        operator_revision=1,
        roles=roles,
        permissions=(
            "platform.feature.read",
            "platform.feature.write",
            "platform.feature.approve",
            "platform.feature.kill",
        ),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _platform_app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    holder: dict[str, PlatformContext],
):
    monkeypatch.setattr(
        feature_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: holder["context"],
    )
    monkeypatch.setattr(
        feature_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: holder["context"],
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-107 Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag",),
    )
    app.state.config = config
    app.include_router(router)
    return app


def _platform_headers(**values: str) -> dict[str, str]:
    return {
        "Cookie": (
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; "
            f"{PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": PLATFORM_CSRF,
        **values,
    }


def _workspace_context() -> WorkspaceSessionContext:
    return WorkspaceSessionContext(
        workspace_session_id="wss_" + "1" * 32,
        global_session_id="gss_" + "1" * 32,
        account_id="gac_2abeaa4602635ac0bd3ad56a7b19320a",
        workspace_id="wsp_aba85648d6c3c0119b4a41bddf82762b",
        tenant_id="944f3e6d-8b66-584e-b095-7cac890a5f87",
        membership_id="wmb_0a1355f1e054acafb7fe2eaf001797b8",
        legacy_user_id="009cc16d-d981-5969-9a91-bf52dbc96715",
        membership_role="owner",
        legacy_role="admin",
        account_email="workspace-owner@example.invalid",
        display_name="Workspace owner",
        tenant_slug="seed-workspace",
        tenant_name="Seed workspace",
        permission_codes=(),
        permission_revision=1,
        authorization_fingerprint="f" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        token=WORKSPACE_TOKEN,
    )


def _create_payload(code: str, risk: str = "standard") -> dict[str, object]:
    return {
        "flag_code": code,
        "display_name": code.replace(".", " ").title(),
        "risk_level": risk,
        "rules": {
            "environments": ["production"],
            "regions": ["cn-north-1"],
            "cohort_code": None,
            "percentage_bps": 10000,
            "prerequisite_flag_code": None,
        },
        "confirmed": True,
    }


def _insert_health_summary(
    config: AppConfig,
    *,
    region_code: str,
    watermark: int,
    health_status: str,
    signed: bool = False,
) -> None:
    observed_at = datetime.now(UTC)
    body = {
        "service_code": "gateway",
        "region_code": region_code,
        "watermark": watermark,
        "health_status": health_status,
        "error_rate_bps": 0 if health_status == "healthy" else 10000,
        "latency_p95_ms": 0,
    }
    signature = (
        feature_service._secret_digest(
            config, "health-summary", feature_service._canonical(body)
        )
        if signed
        else "0" * 64
    )
    connection = connect_database(config.database_url)
    try:
        connection.execute(
            """
            INSERT INTO rollout_health_summaries(
              rollout_health_summary_id,service_code,region_code,source_watermark,
              health_status,error_rate_bps,latency_p95_ms,summary_sha256,
              observed_at,expires_at)
            VALUES(?,?,?, ?,?,?,?, ?,?,?)
            """,
            (
                "rhs_" + f"{watermark:032x}",
                "gateway",
                region_code,
                watermark,
                health_status,
                body["error_rate_bps"],
                body["latency_p95_ms"],
                signature,
                observed_at.isoformat(),
                (observed_at + timedelta(minutes=5)).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.anyio
async def test_standard_rollout_runs_and_kill_is_synchronous(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, list[PlatformContext]],
) -> None:
    config, contexts = runtime
    holder = {"context": contexts[0]}
    app = _platform_app(monkeypatch, config, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        created = await client.post(
            "/platform/feature-flags",
            headers=_platform_headers(
                **{"Idempotency-Key": "prd107-create-standard-0001"}
            ),
            json=_create_payload("console.dense_mode"),
        )
        assert created.status_code == 200, created.text
        flag = created.json()["flag"]
        assert flag["rollout_percentage_bps"] == 0
        assert flag["exposure_count"] == 0
        assert [item["version_number"] for item in flag["versions"]] == [1]
        assert "rules_sha256" not in json.dumps(created.json())

        malformed = await client.post(
            "/platform/feature-flags",
            headers=_platform_headers(
                **{"Idempotency-Key": "prd107-create-invalid-0001"}
            ),
            json={**_create_payload("console.invalid"), "client_override": True},
        )
        assert malformed.status_code == 422

        versioned = await client.post(
            "/platform/feature-flags",
            headers=_platform_headers(
                **{
                    "If-Match": '"1"',
                    "Idempotency-Key": "prd107-version-standard-0001",
                }
            ),
            json={
                **_create_payload("console.dense_mode"),
                "action": "version",
                "flag_id": flag["flag_id"],
            },
        )
        assert versioned.status_code == 200, versioned.text
        flag = versioned.json()["flag"]
        assert flag["current_version"] == 1
        assert flag["revision"] == 2
        assert [item["version_number"] for item in flag["versions"]] == [2, 1]

        started = await client.post(
            f"/platform/feature-flags/{flag['flag_id']}/rollouts",
            headers=_platform_headers(
                **{
                    "If-Match": '"2"',
                    "Idempotency-Key": "prd107-start-standard-0001",
                }
            ),
            json={
                "action": "start",
                "rollout_id": None,
                "version_number": 2,
                "percentage_bps": 10000,
                "environment_code": "production",
                "region_code": "cn-north-1",
                "reason_code": "release_start",
                "confirmed": True,
            },
        )
        assert started.status_code == 200, started.text
        assert started.json()["rollout"]["status"] == "scheduled"
        frozen_watermark = started.json()["rollout"]["health_source_watermark"]
        assert isinstance(frozen_watermark, int)
        _insert_health_summary(
            config,
            region_code="cn-north-1",
            watermark=frozen_watermark + 1,
            health_status="unhealthy",
        )
        assert run_platform_job_once(config, worker_code="prd107-test-worker") is True

        listed = await client.get(
            "/platform/feature-flags?limit=20", headers=_platform_headers()
        )
        assert listed.status_code == 200
        current = listed.json()["flags"][0]
        assert current["current_version"] == 2
        assert current["rollout_percentage_bps"] == 10000
        assert current["latest_rollout"]["status"] == "completed"
        downgrade = await client.post(
            f"/platform/feature-flags/{flag['flag_id']}/rollouts",
            headers=_platform_headers(
                **{
                    "If-Match": f'"{current["revision"]}"',
                    "Idempotency-Key": "prd107-downgrade-via-start-001",
                }
            ),
            json={
                "action": "start",
                "rollout_id": None,
                "version_number": 1,
                "percentage_bps": 10000,
                "environment_code": "production",
                "region_code": "cn-north-1",
                "reason_code": "release_start",
                "confirmed": True,
            },
        )
        assert downgrade.status_code == 409
        assert downgrade.json()["code"] == "feature_flag.conflict"
        connection = connect_database(config.database_url)
        try:
            connection.execute("BEGIN")
            exposure_recorded = feature_service.record_feature_exposure_in_transaction(
                connection,
                config,
                actor=_workspace_context(),
                flag_code="console.dense_mode",
                request_id="request-prd107-exposure",
                trace_id="trace-prd107-exposure",
            )
            exposure_replayed = feature_service.record_feature_exposure_in_transaction(
                connection,
                config,
                actor=_workspace_context(),
                flag_code="console.dense_mode",
                request_id="request-prd107-exposure",
                trace_id="trace-prd107-exposure",
            )
            connection.commit()
            assert exposure_recorded is True
            assert exposure_replayed is False
            exposure = connection.execute(
                "SELECT subject_pseudonym,request_sha256 FROM feature_exposures"
            ).fetchone()
            assert exposure is not None
            assert _workspace_context().account_id not in tuple(exposure)
        finally:
            connection.close()
        _insert_health_summary(
            config,
            region_code="cn-north-1",
            watermark=frozen_watermark + 2,
            health_status="healthy",
            signed=True,
        )

        rolled_back = await client.post(
            f"/platform/feature-flags/{flag['flag_id']}/rollouts",
            headers=_platform_headers(
                **{
                    "If-Match": f'"{current["latest_rollout"]["revision"]}"',
                    "Idempotency-Key": "prd107-rollback-standard-001",
                }
            ),
            json={
                "action": "rollback",
                "rollout_id": current["latest_rollout"]["rollout_id"],
                "version_number": None,
                "percentage_bps": None,
                "environment_code": None,
                "region_code": None,
                "reason_code": "release_rollback",
                "confirmed": True,
            },
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["rollout"]["action"] == "rollback"
        assert run_platform_job_once(config, worker_code="prd107-rollback-worker") is True

        listed = await client.get(
            "/platform/feature-flags?limit=20", headers=_platform_headers()
        )
        current = listed.json()["flags"][0]
        assert current["current_version"] == 1
        assert current["latest_rollout"]["status"] == "rolled_back"
        assert current["exposure_count"] == 1
        assert {
            item["version_number"]: item["exposure_count"]
            for item in current["versions"]
        } == {2: 1, 1: 0}

        killed = await client.post(
            f"/platform/feature-flags/{flag['flag_id']}/kill",
            headers=_platform_headers(
                **{
                    "If-Match": f'"{current["revision"]}"',
                    "Idempotency-Key": "prd107-kill-standard-00001",
                }
            ),
            json={"reason_code": "incident_containment", "confirmed": True},
        )
        assert killed.status_code == 200, killed.text
        assert killed.json()["flag"]["status"] == "killed"
        assert killed.json()["flag"]["rollout_percentage_bps"] == 0

        crossover = await client.get(
            "/platform/feature-flags",
            headers={
                **_platform_headers(),
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; "
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"
                ),
            },
        )
        assert crossover.status_code == 400
        assert crossover.json()["code"] == "platform.credential_conflict"


@pytest.mark.anyio
async def test_p0_rollout_requires_two_independent_approvers(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, list[PlatformContext]],
) -> None:
    config, contexts = runtime
    holder = {"context": contexts[0]}
    app = _platform_app(monkeypatch, config, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        created = await client.post(
            "/platform/feature-flags",
            headers=_platform_headers(
                **{"Idempotency-Key": "prd107-create-p0-flag-00001"}
            ),
            json=_create_payload("gateway.new_router", "p0"),
        )
        flag = created.json()["flag"]
        started = await client.post(
            f"/platform/feature-flags/{flag['flag_id']}/rollouts",
            headers=_platform_headers(
                **{
                    "If-Match": '"1"',
                    "Idempotency-Key": "prd107-start-p0-rollout-001",
                }
            ),
            json={
                "action": "start",
                "rollout_id": None,
                "version_number": 1,
                "percentage_bps": 2500,
                "environment_code": "production",
                "region_code": "cn-north-1",
                "reason_code": "release_start",
                "confirmed": True,
            },
        )
        rollout = started.json()["rollout"]
        assert rollout["status"] == "pending_approval"

        self_approval = await client.post(
            f"/platform/feature-flags/{flag['flag_id']}/rollouts",
            headers=_platform_headers(
                **{
                    "If-Match": '"1"',
                    "Idempotency-Key": "prd107-self-approve-p0-001",
                }
            ),
            json={
                "action": "approve",
                "rollout_id": rollout["rollout_id"],
                "version_number": None,
                "percentage_bps": None,
                "environment_code": None,
                "region_code": None,
                "reason_code": "release_start",
                "confirmed": True,
            },
        )
        assert self_approval.status_code == 409
        assert self_approval.json()["code"] == "feature_flag.approval_conflict"

        holder["context"] = replace(
            contexts[1], permissions=("platform.feature.approve",)
        )
        first = await client.post(
            f"/platform/feature-flags/{flag['flag_id']}/rollouts",
            headers=_platform_headers(
                **{
                    "If-Match": '"1"',
                    "Idempotency-Key": "prd107-first-approve-p0-001",
                }
            ),
            json={
                "action": "approve",
                "rollout_id": rollout["rollout_id"],
                "version_number": None,
                "percentage_bps": None,
                "environment_code": None,
                "region_code": None,
                "reason_code": "release_start",
                "confirmed": True,
            },
        )
        assert first.status_code == 200, first.text
        assert first.json()["rollout"]["approval_count"] == 1
        assert first.json()["rollout"]["status"] == "pending_approval"

        holder["context"] = replace(
            contexts[2], permissions=("platform.feature.approve",)
        )
        second = await client.post(
            f"/platform/feature-flags/{flag['flag_id']}/rollouts",
            headers=_platform_headers(
                **{
                    "If-Match": '"2"',
                    "Idempotency-Key": "prd107-second-approve-p0-01",
                }
            ),
            json={
                "action": "approve",
                "rollout_id": rollout["rollout_id"],
                "version_number": None,
                "percentage_bps": None,
                "environment_code": None,
                "region_code": None,
                "reason_code": "release_start",
                "confirmed": True,
            },
        )
        assert second.status_code == 200, second.text
        assert second.json()["rollout"]["approval_count"] == 2
        assert second.json()["rollout"]["status"] == "scheduled"


@pytest.mark.anyio
async def test_workspace_capabilities_are_session_scoped_and_entitlement_fenced(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, list[PlatformContext]],
) -> None:
    config, contexts = runtime
    holder = {"context": contexts[0]}
    workspace_context = _workspace_context()
    monkeypatch.setattr(
        feature_routes,
        "authenticate_workspace_session_context",
        lambda *args, **kwargs: workspace_context,
    )
    monkeypatch.setattr(
        feature_service,
        "authenticate_workspace_session_in_transaction",
        lambda *args, **kwargs: workspace_context,
    )
    app = _platform_app(monkeypatch, config, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        created = await client.post(
            "/platform/feature-flags",
            headers=_platform_headers(
                **{"Idempotency-Key": "prd107-create-workspace-flag-01"}
            ),
            json=_create_payload("workspace.new_console"),
        )
        assert created.status_code == 200, created.text

        capabilities = await client.get(
            "/workspaces/current/capabilities",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert capabilities.status_code == 200, capabilities.text
        assert capabilities.json()["capabilities"] == [
            {
                "flag_code": "workspace.new_console",
                "enabled": False,
                "public_version": 1,
                "reason_code": "entitlement_denied",
            }
        ]
        assert workspace_context.tenant_id not in json.dumps(capabilities.json())
        connection = connect_database(config.database_url)
        try:
            snapshot = connection.execute(
                """SELECT workspace_id,tenant_id,subject_pseudonym,enabled,reason_code
                   FROM flag_evaluation_snapshots"""
            ).fetchone()
            assert snapshot is not None
            assert snapshot["workspace_id"] == workspace_context.workspace_id
            assert snapshot["tenant_id"] == workspace_context.tenant_id
            assert snapshot["subject_pseudonym"] != workspace_context.account_id
            assert len(snapshot["subject_pseudonym"]) == 64
            assert tuple(snapshot)[3:] == (0, "entitlement_denied")
        finally:
            connection.close()

        tenant_override = await client.get(
            f"/workspaces/current/capabilities?tenant_id={workspace_context.tenant_id}",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert tenant_override.status_code == 422
        assert tenant_override.json()["code"] == "feature_flag.request_invalid"

        crossover = await client.get(
            "/workspaces/current/capabilities",
            headers={
                "Cookie": (
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
                    f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"
                )
            },
        )
        assert crossover.status_code == 400
        assert crossover.json()["code"] == "workspace_session.credential_conflict"


@pytest.mark.anyio
async def test_invalid_frozen_health_summary_pauses_without_expanding_rollout(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, list[PlatformContext]],
) -> None:
    config, contexts = runtime
    holder = {"context": contexts[0]}
    app = _platform_app(monkeypatch, config, holder)
    region_code = "cn-tampered-1"
    _insert_health_summary(
        config,
        region_code=region_code,
        watermark=9001,
        health_status="healthy",
    )
    payload = _create_payload("gateway.health_guard")
    payload["rules"] = {
        **payload["rules"],
        "regions": [region_code],
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        created = await client.post(
            "/platform/feature-flags",
            headers=_platform_headers(
                **{"Idempotency-Key": "prd107-create-health-guard-001"}
            ),
            json=payload,
        )
        flag = created.json()["flag"]
        started = await client.post(
            f"/platform/feature-flags/{flag['flag_id']}/rollouts",
            headers=_platform_headers(
                **{
                    "If-Match": '"1"',
                    "Idempotency-Key": "prd107-start-health-guard-0001",
                }
            ),
            json={
                "action": "start",
                "rollout_id": None,
                "version_number": 1,
                "percentage_bps": 10000,
                "environment_code": "production",
                "region_code": region_code,
                "reason_code": "release_start",
                "confirmed": True,
            },
        )
        assert started.status_code == 200, started.text
        assert started.json()["rollout"]["health_source_watermark"] == 9001
        assert run_platform_job_once(config, worker_code="prd107-health-worker") is True

        listed = await client.get(
            "/platform/feature-flags?limit=20", headers=_platform_headers()
        )
        current = listed.json()["flags"][0]
        assert current["rollout_percentage_bps"] == 0
        assert current["latest_rollout"]["status"] == "paused"
        assert current["latest_rollout"]["health_status"] == "unavailable"


def test_stable_bucket_is_versioned_and_deterministic() -> None:
    config = _config("sqlite:///:memory:")
    first = feature_service._stable_bucket(
        config,
        flag_code="gateway.new_router",
        version=1,
        workspace_id="wsp_" + "1" * 32,
        subject_id="gac_" + "2" * 32,
    )
    second = feature_service._stable_bucket(
        config,
        flag_code="gateway.new_router",
        version=1,
        workspace_id="wsp_" + "1" * 32,
        subject_id="gac_" + "2" * 32,
    )
    next_version = feature_service._stable_bucket(
        config,
        flag_code="gateway.new_router",
        version=2,
        workspace_id="wsp_" + "1" * 32,
        subject_id="gac_" + "2" * 32,
    )
    assert first == second
    assert 0 <= first < 10000
    assert first != next_version


def test_control_evaluation_order_and_p95_below_twenty_milliseconds() -> None:
    config = _config("sqlite:///:memory:")
    actor = _workspace_context()
    prerequisite = _evaluation_row(
        "gateway.prerequisite",
        status="killed",
        regions=["cn-north-1"],
    )
    target = _evaluation_row(
        "gateway.new_router",
        status="active",
        regions=["us-east-1"],
        prerequisite_flag_code="gateway.prerequisite",
    )
    by_code = {
        "gateway.prerequisite": prerequisite,
        "gateway.new_router": target,
    }
    capability = feature_service._evaluate_capability(
        None,
        config,
        actor=actor,
        context={"operational_status": "active", "entitled": 1, "region_code": "cn-north-1"},
        by_code=by_code,
        resolved={},
        resolving=set(),
        flag_code="gateway.new_router",
    )
    assert capability.reason_code == "prerequisite_disabled"
    entitlement_fenced = feature_service._evaluate_capability(
        None,
        config,
        actor=actor,
        context={"operational_status": "active", "entitled": 0, "region_code": "cn-north-1"},
        by_code=by_code,
        resolved={},
        resolving=set(),
        flag_code="gateway.new_router",
    )
    assert entitlement_fenced.reason_code == "entitlement_denied"

    hot_row = _evaluation_row(
        "gateway.hot_path",
        status="active",
        regions=["cn-north-1"],
    )
    samples: list[float] = []
    for _index in range(5_000):
        started = perf_counter_ns()
        result = feature_service._evaluate_capability(
            None,
            config,
            actor=actor,
            context={
                "operational_status": "active",
                "entitled": 1,
                "region_code": "cn-north-1",
            },
            by_code={"gateway.hot_path": hot_row},
            resolved={},
            resolving=set(),
            flag_code="gateway.hot_path",
        )
        samples.append((perf_counter_ns() - started) / 1_000_000)
        assert result.enabled is True
    samples.sort()
    assert samples[int(len(samples) * 0.95) - 1] < 20.0


def _evaluation_row(
    flag_code: str,
    *,
    status: str,
    regions: list[str],
    prerequisite_flag_code: str | None = None,
) -> dict[str, object]:
    return {
        "feature_flag_id": "ffg_" + "1" * 32,
        "current_version_id": "ffv_" + "2" * 32,
        "flag_code": flag_code,
        "status": status,
        "rollout_percentage_bps": 10000,
        "version_number": 1,
        "rules_json": json.dumps(
            {
                "environments": ["production"],
                "regions": regions,
                "cohort_code": None,
                "percentage_bps": 10000,
                "prerequisite_flag_code": prerequisite_flag_code,
            }
        ),
    }
