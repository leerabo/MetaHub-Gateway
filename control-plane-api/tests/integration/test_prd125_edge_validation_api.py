"""PRD-125 validation retry, Agent report, and delivery acceptance API."""

from __future__ import annotations

import secrets
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.edge_management import routes as edge_routes
from metahub_control.edge_management import service as edge_service
from metahub_control.edge_management import validation_service
from metahub_control.edge_management.routes import router
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    workspace_actor_permissions_in_transaction,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.private_gateway import VALIDATION_CHECK_CODES
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
EDGE_SESSION = "egs_" + "e" * 43
DEPLOYMENT_ID = "edp_12500000000000000000000000000001"
RELEASE_ID = "irl_12400000000000000000000000000000"
_TEST_PASSWORD = f"Prd125!Aa9{secrets.token_urlsafe(18)}"


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
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        provider_key_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> tuple[
    AppConfig, dict[str, WorkspaceSessionContext]
]:
    database = tmp_path_factory.mktemp("prd125-validation-api") / "control.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0156"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    config = _config(database_url)
    actor = _owner_actor(config)
    now = "2026-08-27T00:00:00.000Z"
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
              status,revision,revocation_generation,agent_version,health_code,
              last_connected_at,certificate_expires_at,created_by_account_id,
              revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'PRD-125 validation','cn-test','connected','active',1,1,
              '1.0.0','healthy',NULL,NULL,?,NULL,NULL,?,?,NULL)
            """,
            (
                DEPLOYMENT_ID,
                actor.workspace_id,
                actor.tenant_id,
                actor.account_id,
                now,
                now,
            ),
        )
        connection.commit()
    return config, {"actor": actor}


def _owner_actor(config: AppConfig) -> WorkspaceSessionContext:
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            """
            SELECT membership.workspace_membership_id,membership.workspace_id,
              membership.tenant_id,membership.account_id,membership.legacy_user_id,
              tenant.slug,tenant.name,user.email,user.display_name
            FROM workspace_memberships membership
            JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
            JOIN users user ON user.user_id=membership.legacy_user_id
              AND user.tenant_id=membership.tenant_id
            WHERE membership.role_code='owner' AND membership.status='active'
            ORDER BY membership.workspace_id LIMIT 1
            """
        ).fetchone()
        assert row is not None
        connection.execute("BEGIN")
        snapshot = workspace_actor_permissions_in_transaction(
            connection,
            account_id=str(row["account_id"]),
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
        )
        connection.rollback()
    assert {
        "edge.validation.read",
        "edge.validation.manage",
        "edge.delivery.accept",
    } <= set(snapshot.permission_codes)
    return WorkspaceSessionContext(
        workspace_session_id="wss_" + "1" * 32,
        global_session_id="gss_" + "1" * 32,
        account_id=str(row["account_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        membership_id=str(row["workspace_membership_id"]),
        legacy_user_id=str(row["legacy_user_id"]),
        membership_role="owner",
        legacy_role="admin",
        account_email=str(row["email"]),
        display_name=str(row["display_name"]),
        tenant_slug=str(row["slug"]),
        tenant_name=str(row["name"]),
        permission_codes=snapshot.permission_codes,
        permission_revision=snapshot.permission_revision,
        authorization_fingerprint=snapshot.authorization_fingerprint,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        token=WORKSPACE_TOKEN,
        legacy_authorization_fingerprint=snapshot.authorization_fingerprint,
    )


def _contract() -> dict[str, object]:
    return {
        "schema_version": "metahub.delivery-validation/v1",
        "plan_version": "edge-delivery-v1",
        "deployment_id": DEPLOYMENT_ID,
        "deployment_revision": 1,
        "revocation_generation": 1,
        "installation_id": "ins_" + "1" * 32,
        "installation_generation": 1,
        "installer_release_id": RELEASE_ID,
        "desired_state_id": "edst_" + "1" * 32,
        "signed_snapshot_id": "edsn_" + "1" * 32,
        "desired_envelope_sha256": "1" * 64,
        "desired_generation": 1,
        "applied_generation": 1,
        "lkg_generation": 1,
        "identity_id": "edi_" + "1" * 32,
        "identity_generation": 1,
        "certificate_id": "edc_" + "1" * 32,
        "certificate_generation": 1,
        "certificate_sha256": "2" * 64,
        "certificate_not_after": "2026-09-01T00:00:00.000Z",
    }


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    actor_holder: dict[str, WorkspaceSessionContext],
):
    monkeypatch.setattr(
        edge_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor_holder["actor"],
    )
    monkeypatch.setattr(
        validation_service,
        "_current_input_contract",
        lambda *_args, **_kwargs: _contract(),
    )
    monkeypatch.setattr(
        validation_service,
        "_authenticated_agent",
        lambda *_args, **_kwargs: {"capability_codes_json": '["diagnostics"]'},
    )
    monkeypatch.setattr(
        edge_routes,
        "verified_mtls_fingerprint",
        lambda _request: "f" * 64,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-125 Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Edge-Session",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def _workspace_headers(
    *, key: str | None = None, revision: int | None = None, step_up: str | None = None
) -> dict[str, str]:
    headers = {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": WORKSPACE_CSRF,
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    if step_up is not None:
        headers["X-Step-Up-Grant"] = step_up
    return headers


def _agent_headers() -> dict[str, str]:
    return {"X-Edge-Session": EDGE_SESSION}


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _report(
    validation: dict[str, object], *, failed_codes: set[str]
) -> dict[str, object]:
    completed = datetime.now(UTC).replace(microsecond=0)
    started = completed - timedelta(milliseconds=200)
    checks = []
    for index, code in enumerate(validation["requested_check_codes"]):
        failed = code in failed_codes
        checks.append(
            {
                "code": code,
                "outcome": "failed" if failed else "passed",
                "reason_code": f"{code}.{'failed' if failed else 'passed'}",
                "recovery_action_code": f"{code}.recover" if failed else None,
                "evidence_sha256": f"{index + 1:x}" * 64,
                "duration_ms": 20,
            }
        )
    return {
        "schema_version": "metahub.delivery-validation/v1",
        "validation_id": validation["validation_run_id"],
        "deployment_id": validation["deployment_id"],
        "plan_version": validation["plan_version"],
        "attempt": validation["attempt"],
        "input_contract_sha256": validation["input_contract_sha256"],
        "outcome": "failed" if failed_codes else "passed",
        "checks": checks,
        "started_at": _utc_text(started),
        "completed_at": _utc_text(completed),
        "duration_ms": 200,
    }


@pytest.mark.anyio
async def test_validation_failure_retry_agent_replay_and_delivery_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, dict[str, WorkspaceSessionContext]],
) -> None:
    config, actor_holder = runtime
    consumed: list[tuple[str, str]] = []

    def consume(*_args, **kwargs) -> None:
        consumed.append((str(kwargs["action_code"]), str(kwargs["resource_id"])))

    monkeypatch.setattr(
        validation_service, "consume_step_up_grant_in_transaction", consume
    )
    app = _app(monkeypatch, config, actor_holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations",
            headers=_workspace_headers(key="prd125-validation-start-0001"),
            json={"action": "start", "source_validation_run_id": None},
        )
        assert started.status_code == 201, started.text
        first = started.json()["validation"]
        assert first["requested_check_codes"] == list(VALIDATION_CHECK_CODES)
        assert first["effective_checks"] == []

        replay = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations",
            headers=_workspace_headers(key="prd125-validation-start-0001"),
            json={"action": "start", "source_validation_run_id": None},
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True

        agent_read = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations/{first['validation_run_id']}",
            headers=_agent_headers(),
        )
        assert agent_read.status_code == 200, agent_read.text
        first_report = _report(first, failed_codes={"provider_connectivity"})
        failed = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations/"
            f"{first['validation_run_id']}/report",
            headers=_agent_headers(),
            json=first_report,
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["validation"]["outcome"] == "failed"

        report_replay = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations/"
            f"{first['validation_run_id']}/report",
            headers=_agent_headers(),
            json=first_report,
        )
        assert report_replay.status_code == 200, report_replay.text
        assert report_replay.json()["idempotent_replay"] is True

        retry_response = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations",
            headers=_workspace_headers(key="prd125-validation-retry-0001"),
            json={
                "action": "retry_failed",
                "source_validation_run_id": first["validation_run_id"],
            },
        )
        assert retry_response.status_code == 201, retry_response.text
        retry = retry_response.json()["validation"]
        assert retry["attempt"] == 2
        assert retry["requested_check_codes"] == ["provider_connectivity"]
        retry_report = _report(retry, failed_codes=set())
        passed = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations/"
            f"{retry['validation_run_id']}/report",
            headers=_agent_headers(),
            json=retry_report,
        )
        assert passed.status_code == 200, passed.text
        assert [
            (item["code"], item["outcome"])
            for item in passed.json()["validation"]["effective_checks"]
        ] == [(code, "passed") for code in VALIDATION_CHECK_CODES]

        missing_step_up = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/delivery-acceptances",
            headers=_workspace_headers(
                key="prd125-delivery-missing-step-up-0001", revision=1
            ),
            json={"validation_run_id": retry["validation_run_id"]},
        )
        assert missing_step_up.status_code == 403
        assert missing_step_up.json()["code"] == "step_up.required"

        accepted = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/delivery-acceptances",
            headers=_workspace_headers(
                key="prd125-delivery-accept-0001", revision=1, step_up="s" * 43
            ),
            json={"validation_run_id": retry["validation_run_id"]},
        )
        assert accepted.status_code == 201, accepted.text
        assert consumed == [("edge.delivery.accept", DEPLOYMENT_ID)]
        acceptance_id = accepted.json()["acceptance"]["delivery_acceptance_id"]

        acceptance_replay = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/delivery-acceptances",
            headers=_workspace_headers(
                key="prd125-delivery-accept-0001", revision=1, step_up="s" * 43
            ),
            json={"validation_run_id": retry["validation_run_id"]},
        )
        assert acceptance_replay.status_code == 200, acceptance_replay.text
        assert acceptance_replay.json()["idempotent_replay"] is True
        assert consumed == [("edge.delivery.accept", DEPLOYMENT_ID)]

        workspace_read = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations/{retry['validation_run_id']}",
            headers=_workspace_headers(),
        )
        assert workspace_read.status_code == 200
        assert (
            workspace_read.json()["delivery_acceptance"]["delivery_acceptance_id"]
            == acceptance_id
        )
        assert workspace_read.headers["cache-control"] == "no-store, private"

    with closing(connect_database(config.database_url)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM edge_validation_runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM edge_validation_checks").fetchone()[0] == 9
        assert (
            connection.execute("SELECT COUNT(*) FROM edge_delivery_acceptances").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM edge_delivery_events").fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.anyio
async def test_validation_api_rejects_unknown_fields_and_mismatched_report(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, dict[str, WorkspaceSessionContext]],
) -> None:
    config, actor_holder = runtime
    app = _app(monkeypatch, config, actor_holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unknown = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations",
            headers=_workspace_headers(key="prd125-validation-unknown-0001"),
            json={
                "action": "start",
                "source_validation_run_id": None,
                "check_codes": ["device_certificate"],
            },
        )
        assert unknown.status_code == 400
        assert unknown.json()["code"] == "edge.request_invalid"

        started = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations",
            headers=_workspace_headers(key="prd125-validation-mismatch-start-0001"),
            json={"action": "start", "source_validation_run_id": None},
        )
        assert started.status_code == 409
        assert started.json()["code"] == "edge.validation_conflict"

        first_id = ""
        with closing(connect_database(config.database_url)) as connection:
            first_id = str(
                connection.execute(
                    "SELECT validation_run_id FROM edge_validation_runs ORDER BY attempt LIMIT 1"
                ).fetchone()[0]
            )
        read = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations/{first_id}",
            headers=_workspace_headers(),
        )
        report = _report(read.json()["validation"], failed_codes=set())
        report["input_contract_sha256"] = "f" * 64
        mismatch = await client.post(
            f"/edge/deployments/{DEPLOYMENT_ID}/validations/{first_id}/report",
            headers=_agent_headers(),
            json=report,
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["code"] == "edge.validation_report_invalid"
