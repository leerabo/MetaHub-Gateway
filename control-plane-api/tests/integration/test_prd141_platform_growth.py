"""PRD-141 Platform Growth API, state, privacy, and scheduler contracts."""

from __future__ import annotations

import hashlib
import secrets
import shutil
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.service import CSRF_COOKIE as GLOBAL_CSRF_COOKIE
from metahub_control.identity.service import SESSION_COOKIE as GLOBAL_SESSION_COOKIE
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_growth import outbox, projection, routes, scheduler, service
from metahub_control.platform_growth.routes import router
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
CSRF = "c" * 43
STEP_UP = "u" * 43
NOW = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)
STAMP = "2026-09-01T00:00:00.000Z"
SOURCE_ID = "acs_" + "9" * 32
ROLE_BY_NAME = {
    "growth": "platform_growth",
    "channel": "platform_channel",
    "customer_success": "platform_customer_success",
    "owner1": "platform_owner",
    "owner2": "platform_owner",
}


@dataclass(slots=True)
class Runtime:
    config: AppConfig
    tokens: dict[str, str]
    contexts: dict[str, PlatformContext]
    operator_ids: dict[str, str]
    source_watermark: str
    revoked_tokens: set[str]


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


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd141-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0156"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", f"Aa9!{secrets.token_urlsafe(24)}")
        seed = apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        binding = connection.execute(
            """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
                WHERE tenant_id=?""",
            (seed.tenant_id,),
        ).fetchone()
        assert binding is not None
        operator_ids: dict[str, str] = {}
        for index, (name, role) in enumerate(ROLE_BY_NAME.items(), start=10):
            marker = format(index, "x")
            account_id = "gac_" + marker * 32
            operator_id = "pto_" + marker * 32
            connection.execute(
                """INSERT INTO global_accounts(
                  account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                  credential_revision,session_revision,mfa_state,failed_login_count,
                  locked_until,last_login_at,created_at,updated_at)
                VALUES(?,?,NULL,?,'active',0,1,1,'not_enrolled',0,NULL,NULL,?,?)""",
                (account_id, f"prd141-{name}@example.invalid", name, STAMP, STAMP),
            )
            connection.execute(
                """INSERT INTO platform_operators(
                  operator_id,account_id,status,revision,created_by_operator_id,
                  created_at,updated_at) VALUES(?,?,'active',1,NULL,?,?)""",
                (operator_id, account_id, STAMP, STAMP),
            )
            connection.execute(
                """INSERT INTO platform_operator_role_bindings(
                  operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                VALUES(?,?,'active',NULL,?,NULL)""",
                (operator_id, role, STAMP),
            )
            operator_ids[name] = operator_id
        source_hash = hashlib.sha256(b"prd141-source").hexdigest()
        connection.execute(
            """INSERT INTO platform_acquisition_sources(
              acquisition_source_id,source_code_sha256,source_kind,channel_code,
              channel_partner_id,channel_referral_code_id,campaign_id,verification_status,
              valid_from,valid_until,revision,canonical_sha256,created_at,updated_at)
            VALUES(?,?,'paid','search',NULL,NULL,NULL,'verified',?,NULL,1,?,?,?)""",
            (SOURCE_ID, source_hash, STAMP, source_hash, STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO platform_growth_funnel_snapshots(
              growth_funnel_snapshot_id,bucket_start,bucket_kind,view_code,dimension_code,
              dimension_value,formula_version,generation,registered_count,activated_count,
              trial_started_count,paid_count,retained_30d_count,registered_to_activated_bps,
              activated_to_trial_bps,trial_to_paid_bps,paid_to_retained_bps,currency_code,
              revenue_minor,cost_minor,coverage_bps,distinct_subject_count,
              suppression_threshold,suppressed,suppression_code,source_watermark,
              source_sha256,supersedes_snapshot_id,generated_at)
            VALUES('gfs_14100000000000000000000000000000','2026-09-01','day','overview',
              'none','all','growth-funnel-v2',1,40,32,28,24,20,8000,8750,8571,8333,
              'CNY',240000,50000,10000,40,20,0,NULL,?,?,NULL,?)""",
            (STAMP, "a" * 64, STAMP),
        )
        for view in ("adoption", "retention"):
            marker = "1" if view == "adoption" else "2"
            connection.execute(
                """INSERT INTO platform_growth_funnel_snapshots(
                  growth_funnel_snapshot_id,bucket_start,bucket_kind,view_code,dimension_code,
                  dimension_value,formula_version,generation,registered_count,activated_count,
                  trial_started_count,paid_count,retained_30d_count,
                  registered_to_activated_bps,activated_to_trial_bps,trial_to_paid_bps,
                  paid_to_retained_bps,currency_code,revenue_minor,cost_minor,coverage_bps,
                  distinct_subject_count,suppression_threshold,suppressed,suppression_code,
                  source_watermark,source_sha256,supersedes_snapshot_id,generated_at)
                VALUES(?, '2026-09-01','day',?,'none','all','growth-funnel-v2',1,
                  25,20,0,0,18,8000,NULL,NULL,NULL,NULL,NULL,NULL,10000,25,20,0,NULL,
                  ?,?,NULL,?)""",
                ("gfs_" + marker * 32, view, STAMP, marker * 64, STAMP),
            )
        connection.execute(
            """INSERT INTO platform_growth_cohort_snapshots(
              growth_cohort_snapshot_id,cohort_kind,cohort_bucket,age_bucket,view_code,
              dimension_code,dimension_value,eligible_count,retained_count,churned_count,
              retention_bps,coverage_bps,distinct_subject_count,suppression_threshold,
              suppressed,suppression_code,formula_version,generation,source_watermark,
              source_sha256,supersedes_snapshot_id,generated_at)
            VALUES('gco_14100000000000000000000000000000','signup_week','2026-09-01',0,
              'retention','none','all',40,30,10,7500,10000,40,20,0,NULL,
              'growth-cohort-v2',1,?,?,NULL,?)""",
            (STAMP, "b" * 64, STAMP),
        )
        for index in range(20):
            marker = f"{index + 1:032x}"
            token = hashlib.sha256(f"subject-{index}".encode()).hexdigest()
            connection.execute(
                """INSERT INTO platform_conversion_attributions(
                  conversion_attribution_id,conversion_source_kind,conversion_source_id,
                  conversion_stage,owner_workspace_id,tenant_id,subject_token_sha256,
                  subject_key_version,acquisition_source_id,growth_campaign_id,
                  channel_partner_id,formula_version,lookback_days,value_minor,currency_code,
                  consent_record_id,privacy_preference_revision,source_sha256,occurred_at,created_at)
                VALUES(?,'product_event',?,'activated',?,?,?,1,?,NULL,NULL,
                  'last-eligible-touch-30d-v1',30,1200,'CNY',NULL,NULL,?,?,?)""",
                (
                    "cat_" + marker,
                    "source-" + marker,
                    binding["workspace_id"],
                    binding["tenant_id"],
                    token,
                    SOURCE_ID,
                    hashlib.sha256(marker.encode()).hexdigest(),
                    STAMP,
                    STAMP,
                ),
            )
        connection.execute(
            """UPDATE platform_growth_migration_cursors SET source_watermark=?,
              source_sha256=?,generation=1,updated_at=? WHERE source_code='product_events'""",
            (STAMP, "c" * 64, STAMP),
        )
        connection.commit()
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> Runtime:
    database = tmp_path / "prd141.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    tokens: dict[str, str] = {}
    contexts: dict[str, PlatformContext] = {}
    operator_ids: dict[str, str] = {}
    with closing(connect_database(config.database_url)) as connection:
        for index, name in enumerate(ROLE_BY_NAME, start=10):
            marker = format(index, "x")
            token = marker * 43
            operator_id = "pto_" + marker * 32
            roles, permissions, _fingerprint = platform_access_service._authority(
                connection, operator_id
            )
            tokens[name] = token
            operator_ids[name] = operator_id
            contexts[token] = PlatformContext(
                platform_session_id="pts_" + marker * 32,
                operator_id=operator_id,
                account_id="gac_" + marker * 32,
                global_session_id="gss_" + marker * 32,
                factor_id="mfa_" + marker * 32,
                factor_revision=1,
                operator_revision=1,
                roles=roles,
                permissions=permissions,
                expires_at=NOW + timedelta(hours=1),
            )
    return Runtime(
        config=config,
        tokens=tokens,
        contexts=contexts,
        operator_ids=operator_ids,
        source_watermark=STAMP,
        revoked_tokens=set(),
    )


def _app(monkeypatch: pytest.MonkeyPatch, runtime: Runtime):
    def current_context(connection, token: str) -> PlatformContext:
        context = runtime.contexts.get(token)
        if context is None:
            raise ApiError("platform.session_required")
        if token in runtime.revoked_tokens:
            return replace(context, permissions=())
        roles, permissions, _fingerprint = platform_access_service._authority(
            connection, context.operator_id
        )
        return replace(context, roles=roles, permissions=permissions)

    def service_actor(connection, _config, *, session_token, **_kwargs):
        return current_context(connection, session_token)

    def route_actor(_config, session_token, *, csrf_token=None):
        del csrf_token
        with closing(connect_database(runtime.config.database_url)) as connection:
            return current_context(connection, session_token)

    monkeypatch.setattr(service, "authenticate_platform_session_in_transaction", service_actor)
    monkeypatch.setattr(routes, "authenticate_platform_session_context", route_actor)
    monkeypatch.setattr(service, "_utcnow", lambda: NOW)
    monkeypatch.setattr(
        service,
        "consume_global_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: "mfa",
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-141 Platform Growth Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Growth-Export-Receipt",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = runtime.config
    app.include_router(router)
    return app


def _headers(
    token: str,
    *,
    key: str | None = None,
    revision: int = 0,
) -> dict[str, str]:
    if key is None:
        return {"Cookie": f"{PLATFORM_SESSION_COOKIE}={token}"}
    return {
        "Cookie": f"{PLATFORM_SESSION_COOKIE}={token}; {PLATFORM_CSRF_COOKIE}={CSRF}",
        "Idempotency-Key": key,
        "If-Match": f'"{revision}"',
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": CSRF,
        "X-Step-Up-Grant": STEP_UP,
    }


def _campaign_create() -> dict[str, object]:
    return {
        "operation": "create",
        "expected_revision": 0,
        "display_name": "Search activation",
        "acquisition_source_id": SOURCE_ID,
        "channel_code": "search",
        "target_stage": "activated",
        "starts_at": "2026-09-02T00:00:00Z",
        "ends_at": "2026-09-30T00:00:00Z",
        "budget_minor": 500000,
        "currency": "CNY",
        "confirmed": True,
    }


def _experiment_create() -> dict[str, object]:
    return {
        "operation": "create",
        "expected_revision": 0,
        "hypothesis_code": "activation.copy",
        "surface_code": "workspace.onboarding",
        "primary_metric": "activation.completed",
        "control_code": "control",
        "variant_code": "concise-copy",
        "control_allocation_bps": 5000,
        "variant_allocation_bps": 5000,
        "eligibility_policy_version": "growth-eligibility-v2",
        "consent_version": "analytics-v2",
        "sample_cap": 1000,
        "starts_at": "2026-09-02T00:00:00Z",
        "ends_at": "2026-09-30T00:00:00Z",
        "guardrail_code": "activation-error-rate",
        "decision_policy_version": "fixed-horizon-v1",
        "confirmed": True,
    }


def _export_request(sensitivity: str = "aggregate") -> dict[str, object]:
    return {
        "query_kind": "funnels",
        "view": "overview",
        "from_date": "2026-09-01",
        "to_date": "2026-09-01",
        "bucket": "day",
        "dimension": "none",
        "formula_version": "growth-funnel-v2",
        "sensitivity": sensitivity,
        "confirmed": True,
    }


def test_exact_permission_matrix_and_navigation(runtime: Runtime) -> None:
    growth = set(runtime.contexts[runtime.tokens["growth"]].permissions)
    channel = set(runtime.contexts[runtime.tokens["channel"]].permissions)
    customer_success = set(runtime.contexts[runtime.tokens["customer_success"]].permissions)
    owner = set(runtime.contexts[runtime.tokens["owner1"]].permissions)
    assert {
        "platform.growth.read",
        "platform.growth.campaign.manage",
        "platform.growth.experiment.manage",
        "platform.growth.export.request",
        "platform.growth.export.download",
    } <= growth
    assert "platform.growth.export.approve" not in growth
    assert "platform.growth.channel_attribution_read" in channel
    assert "platform.growth.read" not in channel
    assert "platform.growth.adoption_read" in customer_success
    assert "platform.growth.read" not in customer_success
    assert "platform.growth.export.approve" in owner
    assert "growth" in {
        item.id
        for item in platform_access_service._navigation_items(channel)
    }
    assert "growth" in {
        item.id
        for item in platform_access_service._navigation_items(customer_success)
    }


@pytest.mark.anyio
async def test_role_clipped_queries_strict_inputs_and_retired_route(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(monkeypatch, runtime)),
        base_url=ORIGIN,
    ) as client:
        growth = await client.get(
            "/platform/growth/funnels?view=overview&from=2026-09-01&to=2026-09-01",
            headers=_headers(runtime.tokens["growth"]),
        )
        assert growth.status_code == 200
        assert growth.json()["items"][0]["stages"][0]["subject_count"] == 40
        browser_chain = await client.get(
            "/platform/growth/funnels?view=overview&from=2026-09-01&to=2026-09-01",
            headers={
                **_headers(runtime.tokens["growth"]),
                "Cookie": f"{PLATFORM_SESSION_COOKIE}={runtime.tokens['growth']}; "
                f"{GLOBAL_SESSION_COOKIE}={'g' * 43}; {GLOBAL_CSRF_COOKIE}={'c' * 43}",
            },
        )
        assert browser_chain.status_code == 200
        customer = await client.get(
            "/platform/growth/funnels?view=retention&from=2026-09-01&to=2026-09-01",
            headers=_headers(runtime.tokens["customer_success"]),
        )
        assert customer.status_code == 200
        assert customer.json()["items"][0]["suppressed"] is True
        assert customer.json()["items"][0]["stages"][0]["subject_count"] is None
        denied = await client.get(
            "/platform/growth/funnels?view=overview&from=2026-09-01&to=2026-09-01",
            headers=_headers(runtime.tokens["customer_success"]),
        )
        assert denied.status_code == 403
        channel = await client.get(
            "/platform/growth/attribution?view=channel_summary"
            "&from=2026-09-01&to=2026-09-01",
            headers=_headers(runtime.tokens["channel"]),
        )
        assert channel.status_code == 200
        assert channel.json()["items"][0]["source_id"] is None
        assert channel.json()["items"][0]["value_minor"] is None
        unknown = await client.get(
            "/platform/growth/funnels?unknown=1",
            headers=_headers(runtime.tokens["growth"]),
        )
        assert unknown.status_code == 422
        mixed = await client.get(
            "/platform/growth/funnels",
            headers={
                "Cookie": f"{PLATFORM_SESSION_COOKIE}={runtime.tokens['growth']}; "
                f"{WORKSPACE_SESSION_COOKIE}={'w' * 43}"
            },
        )
        assert mixed.status_code == 401
        retired = await client.get(
            "/product-analytics/funnel",
            headers=_headers(runtime.tokens["growth"]),
        )
        assert retired.status_code == 404


@pytest.mark.anyio
async def test_campaign_and_experiment_commands_are_cas_idempotent_and_isolated(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(monkeypatch, runtime)),
        base_url=ORIGIN,
    ) as client:
        created = await client.post(
            "/platform/growth/campaigns",
            json=_campaign_create(),
            headers=_headers(runtime.tokens["growth"], key="campaign-create-0001"),
        )
        assert created.status_code == 201, created.text
        campaign_id = created.json()["campaign"]["campaign_id"]
        replay = await client.post(
            "/platform/growth/campaigns",
            json=_campaign_create(),
            headers=_headers(runtime.tokens["growth"], key="campaign-create-0001"),
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        schedule = {
            "operation": "schedule",
            "campaign_id": campaign_id,
            "expected_revision": 1,
            "confirmed": True,
        }
        scheduled = await client.post(
            "/platform/growth/campaigns",
            json=schedule,
            headers=_headers(
                runtime.tokens["growth"], key="campaign-schedule-01", revision=1
            ),
        )
        assert scheduled.status_code == 200
        assert scheduled.json()["campaign"]["status"] == "scheduled"
        stale = await client.post(
            "/platform/growth/campaigns",
            json=schedule,
            headers=_headers(
                runtime.tokens["growth"], key="campaign-schedule-02", revision=1
            ),
        )
        assert stale.status_code == 409
        experiment = await client.post(
            "/platform/growth/experiments",
            json=_experiment_create(),
            headers=_headers(runtime.tokens["growth"], key="experiment-create-1"),
        )
        assert experiment.status_code == 201, experiment.text
        experiment_id = experiment.json()["experiment"]["experiment_id"]
        scheduled_experiment = await client.post(
            "/platform/growth/experiments",
            json={
                "operation": "schedule",
                "experiment_id": experiment_id,
                "expected_revision": 1,
                "confirmed": True,
            },
            headers=_headers(
                runtime.tokens["growth"], key="experiment-schedule-1", revision=1
            ),
        )
        assert scheduled_experiment.status_code == 200
        started = await client.post(
            "/platform/growth/experiments",
            json={
                "operation": "start",
                "experiment_id": experiment_id,
                "expected_revision": 2,
                "source_watermark": runtime.source_watermark,
                "confirmed": True,
            },
            headers=_headers(
                runtime.tokens["growth"], key="experiment-start-0001", revision=2
            ),
        )
        assert started.status_code == 200, started.text
        assert started.json()["experiment"]["status"] == "running"
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute("SELECT count(*) FROM model_experiments").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM feature_rollouts").fetchone()[0] == 0


@pytest.mark.anyio
async def test_export_build_independent_approval_and_one_shot_download(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    app = _app(monkeypatch, runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        requested = await client.post(
            "/platform/growth/exports",
            json=_export_request(),
            headers=_headers(runtime.tokens["growth"], key="growth-export-low-1"),
        )
        assert requested.status_code == 202, requested.text
        export_id = requested.json()["export"]["export_id"]
        assert requested.json()["export"]["status"] == "approved"
        assert service.build_next_export(runtime.config, now=NOW) is True
        status = await client.get(
            f"/platform/growth/exports/{export_id}",
            headers=_headers(runtime.tokens["growth"]),
        )
        assert status.json()["export"]["status"] == "ready"
        receipt = await client.post(
            f"/platform/growth/exports/{export_id}/receipts",
            json={"confirmed": True},
            headers=_headers(runtime.tokens["growth"], key="unused-receipt-key"),
        )
        assert receipt.status_code == 200, receipt.text
        receipt_value = receipt.json()["receipt"]
        downloaded = await client.get(
            f"/platform/growth/exports/{export_id}/download",
            headers={
                **_headers(runtime.tokens["growth"]),
                "X-Growth-Export-Receipt": receipt_value,
            },
        )
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].startswith("text/csv")
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert "workspace_id" not in downloaded.text
        repeated = await client.get(
            f"/platform/growth/exports/{export_id}/download",
            headers={
                **_headers(runtime.tokens["growth"]),
                "X-Growth-Export-Receipt": receipt_value,
            },
        )
        assert repeated.status_code in {409, 503}
        high = await client.post(
            "/platform/growth/exports",
            json=_export_request("high_cardinality"),
            headers=_headers(runtime.tokens["owner1"], key="growth-export-high-1"),
        )
        assert high.status_code == 202
        high_id = high.json()["export"]["export_id"]
        assert high.json()["export"]["status"] == "policy_review"
        self_approval = await client.post(
            f"/platform/growth/exports/{high_id}/approvals",
            json={
                "operation": "approve",
                "expected_revision": 1,
                "reason_code": "policy_approved",
                "confirmed": True,
            },
            headers=_headers(runtime.tokens["owner1"], key="growth-export-self-01", revision=1),
        )
        assert self_approval.status_code == 409
        approved = await client.post(
            f"/platform/growth/exports/{high_id}/approvals",
            json={
                "operation": "approve",
                "expected_revision": 1,
                "reason_code": "policy_approved",
                "confirmed": True,
            },
            headers=_headers(runtime.tokens["owner2"], key="growth-export-approve", revision=1),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["export"]["status"] == "approved"
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM platform_growth_events WHERE event_type='growth.exported'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM platform_growth_event_outbox"
        ).fetchone()[0] >= 1


@pytest.mark.anyio
async def test_dynamic_permission_loss_rejects_next_request(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    app = _app(monkeypatch, runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        allowed = await client.get(
            "/platform/growth/campaigns",
            headers=_headers(runtime.tokens["growth"]),
        )
        assert allowed.status_code == 200
        runtime.revoked_tokens.add(runtime.tokens["growth"])
        denied = await client.get(
            "/platform/growth/campaigns",
            headers=_headers(runtime.tokens["growth"]),
        )
        assert denied.status_code == 403


def test_scheduler_and_growth_event_outbox_are_persistently_fenced(runtime: Runtime) -> None:
    first_event_id = "gca_" + "1" * 32
    with closing(connect_database(runtime.config.database_url)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        service._event(
            connection,
            event_type="growth.campaign_changed",
            object_kind="campaign",
            object_id=first_event_id,
            revision=1,
            generation=1,
            payload={
                "campaign_id": first_event_id,
                "revision": 1,
                "schema_version": "growth-event-v1",
                "source_digest": "1" * 64,
                "status": "draft",
            },
            now=NOW,
        )
        connection.execute(
            """UPDATE platform_growth_scheduler_state SET status='idle',
              lease_owner_id=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,
              next_run_at='2026-09-02T00:00:00.000Z'"""
        )
        connection.execute(
            """UPDATE platform_growth_scheduler_state SET next_run_at=?
              WHERE job_code='growth_event_outbox'""",
            (STAMP,),
        )
        connection.commit()

    assert scheduler.run_platform_growth_scheduler_once(
        runtime.config,
        worker_id="growth-worker-141-a",
        now=NOW,
    ) is True
    with closing(connect_database(runtime.config.database_url)) as connection:
        state = connection.execute(
            """SELECT status,generation,cursor_json FROM platform_growth_scheduler_state
              WHERE job_code='growth_event_outbox'"""
        ).fetchone()
        delivered = connection.execute(
            """SELECT status,attempt_count,generation,subscription_snapshot_json,
              snapshot_frozen_at FROM platform_growth_event_outbox
              ORDER BY created_at,growth_event_outbox_id LIMIT 1"""
        ).fetchone()
        assert tuple(state[:2]) == ("idle", 1)
        assert state["cursor_json"] == '{"completed_generation":1}'
        assert tuple(delivered[:4]) == ("sent", 1, 1, "[]")
        assert delivered["snapshot_frozen_at"] == "2026-09-01T00:05:00.000Z"

        connection.execute("BEGIN IMMEDIATE")
        second_event_id = "gca_" + "2" * 32
        service._event(
            connection,
            event_type="growth.campaign_changed",
            object_kind="campaign",
            object_id=second_event_id,
            revision=1,
            generation=1,
            payload={
                "campaign_id": second_event_id,
                "revision": 1,
                "schema_version": "growth-event-v1",
                "source_digest": "2" * 64,
                "status": "draft",
            },
            now=NOW + timedelta(seconds=10),
        )
        connection.commit()

    first_lease = outbox.claim_next_event(
        runtime.config,
        worker_id="growth-event-worker-a",
        now=NOW + timedelta(seconds=10),
    )
    assert first_lease is not None
    replacement_lease = outbox.claim_next_event(
        runtime.config,
        worker_id="growth-event-worker-b",
        now=NOW + timedelta(seconds=71),
    )
    assert replacement_lease is not None
    assert replacement_lease.generation == first_lease.generation + 1
    assert outbox.complete_event(
        runtime.config,
        first_lease,
        now=NOW + timedelta(seconds=72),
    ) is False
    assert outbox.complete_event(
        runtime.config,
        replacement_lease,
        now=NOW + timedelta(seconds=72),
    ) is True
    with closing(connect_database(runtime.config.database_url)) as connection:
        replacement = connection.execute(
            """SELECT status,attempt_count,generation FROM platform_growth_event_outbox
              WHERE growth_event_id=?""",
            (replacement_lease.event_id,),
        ).fetchone()
        assert tuple(replacement) == ("sent", 1, 2)


def test_eligibility_assignment_and_privacy_correction_are_replay_safe(
    runtime: Runtime,
) -> None:
    consent_id = "con_" + "3" * 32
    preference_id = "prf_" + "4" * 32
    experiment_id = "gex_" + "7" * 32
    mismatch_experiment_id = "gex_" + "8" * 32
    with closing(connect_database(runtime.config.database_url)) as connection:
        owner = connection.execute(
            """SELECT membership.workspace_id,membership.tenant_id,
                     membership.account_id,membership.legacy_user_id,
                     user.email actor_email,user.role actor_role
              FROM workspace_memberships membership
              JOIN users user ON user.tenant_id=membership.tenant_id
                AND user.user_id=membership.legacy_user_id
              WHERE membership.status='active' LIMIT 1"""
        ).fetchone()
        assert owner is not None
        excluded_event_id = connection.execute(
            "SELECT product_event_id FROM product_events WHERE actor_user_id IS NULL LIMIT 1"
        ).fetchone()[0]
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO consent_records(
              consent_record_id,account_id,consent_type,consent_version,
              source_registration_intent_id,locale,source_kind,decision,
              payload_sha256,consented_at)
            VALUES(?,?,'analytics','analytics-v2',NULL,'en-us','privacy_center',
              'granted',?,'2026-08-31T23:00:00.000Z')""",
            (consent_id, owner["account_id"], "3" * 64),
        )
        connection.execute(
            """INSERT INTO privacy_preferences(
              privacy_preference_id,workspace_id,tenant_id,account_id,analytics_enabled,
              product_updates_enabled,model_improvement_enabled,terms_version,locale,
              revision,created_at,updated_at)
            VALUES(?,?,?,?,1,0,0,'analytics-v2','en-us',1,?,?)""",
            (
                preference_id,
                owner["workspace_id"],
                owner["tenant_id"],
                owner["account_id"],
                STAMP,
                STAMP,
            ),
        )
        audit_id = "aud_" + "5" * 32
        request_id = "req_prd141_eligibility"
        trace_id = "trc_prd141_eligibility"
        connection.execute(
            """INSERT INTO audit_logs(
              audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,
              object_type,object_id,object_name,before_summary_json,after_summary_json,
              changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at)
            VALUES(?,?,?,?,?,'provider.created','provider','prd141-provider',NULL,
              '{}','{}','[]',NULL,NULL,?,?,?)""",
            (
                audit_id,
                owner["tenant_id"],
                owner["legacy_user_id"],
                owner["actor_email"],
                owner["actor_role"],
                request_id,
                trace_id,
                STAMP,
            ),
        )
        eligible_event_id = connection.execute(
            """SELECT 'pev_'||substr(metahub_sha256(json_array(?,?,?,?)),1,32)""",
            (owner["tenant_id"], "provider.created", "audit_log", audit_id),
        ).fetchone()[0]
        generated_event = connection.execute(
            """SELECT actor_user_id,source_id,operation_request_id,operation_trace_id
              FROM product_events WHERE product_event_id=?""",
            (eligible_event_id,),
        ).fetchone()
        assert tuple(generated_event) == (
            owner["legacy_user_id"],
            audit_id,
            request_id,
            trace_id,
        )
        assert projection.evaluate_source_eligibility(
            connection,
            runtime.config,
            now=NOW + timedelta(minutes=1),
        ) >= 2
        eligible = connection.execute(
            """SELECT * FROM platform_growth_event_eligibility
              WHERE product_event_id=?""",
            (eligible_event_id,),
        ).fetchone()
        excluded = connection.execute(
            """SELECT status,reason_code,subject_token_sha256
              FROM platform_growth_event_eligibility WHERE product_event_id=?""",
            (excluded_event_id,),
        ).fetchone()
        assert eligible["status"] == "eligible"
        assert len(eligible["subject_token_sha256"]) == 64
        assert owner["account_id"] not in eligible["subject_token_sha256"]
        assert tuple(excluded) == ("excluded", "historical_consent_unproven", None)

        for current_id, consent_version in (
            (experiment_id, "analytics-v2"),
            (mismatch_experiment_id, "analytics-v3"),
        ):
            connection.execute(
                """INSERT INTO platform_growth_experiments(
                  growth_experiment_id,hypothesis_code,surface_code,primary_metric_code,
                  control_code,variant_code,control_allocation_bps,variant_allocation_bps,
                  eligibility_policy_version,consent_purpose,consent_version,sample_cap,
                  starts_at,ends_at,guardrail_code,decision_policy_version,status,
                  decision_code,source_watermark,revision,canonical_sha256,
                  created_by_operator_id,updated_by_operator_id,created_at,updated_at,decided_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    current_id,
                    "activation.copy",
                    "workspace.onboarding",
                    "activation.completed",
                    "control",
                    "variant",
                    5000,
                    5000,
                    "growth-eligibility-v2",
                    "analytics",
                    consent_version,
                    20,
                    "2026-09-01T00:00:00.000Z",
                    "2026-09-30T00:00:00.000Z",
                    "activation-error-rate",
                    "fixed-horizon-v1",
                    "running",
                    None,
                    STAMP,
                    1,
                    current_id[-1] * 64,
                    runtime.operator_ids["growth"],
                    runtime.operator_ids["growth"],
                    STAMP,
                    STAMP,
                    None,
                ),
            )
        assignment_id = projection.assign_experiment(
            connection,
            runtime.config,
            experiment_id=experiment_id,
            eligibility_id=eligible["growth_event_eligibility_id"],
            now=NOW + timedelta(minutes=2),
        )
        assert projection.assign_experiment(
            connection,
            runtime.config,
            experiment_id=experiment_id,
            eligibility_id=eligible["growth_event_eligibility_id"],
            now=NOW + timedelta(minutes=2),
        ) == assignment_id
        with pytest.raises(ApiError) as conflict:
            projection.assign_experiment(
                connection,
                runtime.config,
                experiment_id=mismatch_experiment_id,
                eligibility_id=eligible["growth_event_eligibility_id"],
                now=NOW + timedelta(minutes=2),
            )
        assert conflict.value.code == "platform.growth_state_conflict"
        assignment = connection.execute(
            """SELECT deterministic_bucket,variant_code,subject_token_sha256
              FROM platform_growth_experiment_assignments
              WHERE growth_experiment_assignment_id=?""",
            (assignment_id,),
        ).fetchone()
        assert 0 <= assignment["deterministic_bucket"] <= 9999
        assert assignment["variant_code"] in {"control", "variant"}
        assert assignment["subject_token_sha256"] == eligible["subject_token_sha256"]

        connection.execute(
            """UPDATE privacy_preferences SET analytics_enabled=0,revision=2,
              updated_at='2026-09-01T00:06:00.000Z' WHERE privacy_preference_id=?""",
            (preference_id,),
        )
        assert projection.apply_privacy_corrections(
            connection,
            now=NOW + timedelta(minutes=7),
        ) >= 1
        corrected = connection.execute(
            """SELECT status,reason_code,subject_token_sha256,subject_key_version
              FROM platform_growth_event_eligibility WHERE product_event_id=?""",
            (eligible_event_id,),
        ).fetchone()
        withdrawn = connection.execute(
            """SELECT subject_token_sha256,subject_key_version,withdrawn_at
              FROM platform_growth_experiment_assignments
              WHERE growth_experiment_assignment_id=?""",
            (assignment_id,),
        ).fetchone()
        assert tuple(corrected) == ("withdrawn", "preference_disabled", None, None)
        assert withdrawn["subject_token_sha256"] is None
        assert withdrawn["subject_key_version"] is None
        assert withdrawn["withdrawn_at"] == "2026-09-01T00:12:00.000Z"
        connection.commit()
