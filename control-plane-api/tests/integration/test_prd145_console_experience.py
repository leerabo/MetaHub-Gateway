"""PRD-145 console experience APIs, projections, and credential boundaries."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.console_experience import routes, scheduler, service
from metahub_control.console_experience.routes import router
from metahub_control.identity.models import GlobalAccountSummary, GlobalSessionSummary
from metahub_control.identity.service import (
    CSRF_COOKIE as IDENTITY_CSRF_COOKIE,
)
from metahub_control.identity.service import (
    SESSION_COOKIE as IDENTITY_SESSION_COOKIE,
)
from metahub_control.identity.service import AuthenticatedIdentity
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    workspace_actor_permissions_in_transaction,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.migrate import _default_migrations_dir, _load_migration_files
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
STAMP = "2026-09-03T08:00:00Z"
NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


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


def _insert_platform_owner(connection) -> tuple[str, str]:
    account_id = "gac_" + "a" * 32
    operator_id = "pto_" + "a" * 32
    connection.execute(
        """INSERT INTO global_accounts(
             account_id,email_normalized,phone_e164,display_name,status,login_enabled,
             credential_revision,session_revision,mfa_state,failed_login_count,
             locked_until,last_login_at,created_at,updated_at)
           VALUES(?,?,NULL,'PRD-145 Platform Owner','active',0,1,1,
             'not_enrolled',0,NULL,NULL,?,?)""",
        (account_id, "prd145-platform@example.invalid", STAMP, STAMP),
    )
    connection.execute(
        """INSERT INTO platform_operators(
             operator_id,account_id,status,revision,created_by_operator_id,
             created_at,updated_at)
           VALUES(?,?,'active',1,NULL,?,?)""",
        (operator_id, account_id, STAMP, STAMP),
    )
    connection.execute(
        """INSERT INTO platform_operator_role_bindings(
             operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
           VALUES(?,'platform_owner','active',NULL,?,NULL)""",
        (operator_id, STAMP),
    )
    return account_id, operator_id


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory):
    database = tmp_path_factory.mktemp("prd145-api") / "prd145.sqlite3"
    database_url = _url(database)
    supplied = os.environ.get("METAHUB_TEST_MIGRATED_BASELINE")
    if supplied:
        shutil.copyfile(Path(supplied).resolve(strict=True), database)
    apply_migrations(database_url)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        seed = apply_seed_data(database_url)
    config = _config(database_url)
    with closing(connect_database(database_url)) as connection:
        assert {tuple(row) for row in connection.execute(
            "SELECT version,checksum FROM schema_migrations WHERE status='applied'"
        )} == {
            (migration.version, migration.checksum)
            for migration in _load_migration_files(_default_migrations_dir())
        }
        platform_account_id, operator_id = _insert_platform_owner(connection)
        owner = connection.execute(
            """SELECT membership.*,tenant.slug,tenant.name,user.email,user.display_name
               FROM workspace_memberships membership
               JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
               JOIN users user ON user.tenant_id=membership.tenant_id
                 AND user.user_id=membership.legacy_user_id
               WHERE membership.tenant_id=? AND membership.role_code='owner'
                 AND membership.status='active' LIMIT 1""",
            (seed.tenant_id,),
        ).fetchone()
        assert owner is not None
        snapshot = workspace_actor_permissions_in_transaction(
            connection,
            account_id=owner["account_id"],
            workspace_id=owner["workspace_id"],
            tenant_id=owner["tenant_id"],
        )
        connection.execute(
            """INSERT INTO edge_deployments(
                 edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
                 status,revision,revocation_generation,agent_version,health_code,
                 last_connected_at,certificate_expires_at,created_by_account_id,
                 revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
               VALUES(?,?,?,'PRD-145 Edge','cn-east-1','connected','active',1,1,
                 '1.0.0','healthy',?,NULL,?,NULL,NULL,?,?,NULL)""",
            (
                "edp_" + "1" * 32,
                owner["workspace_id"],
                owner["tenant_id"],
                STAMP,
                owner["account_id"],
                STAMP,
                STAMP,
            ),
        )
        for index, risk in enumerate(("standard", "high"), start=1):
            marker = str(index)
            connection.execute(
                """INSERT INTO platform_domain_approval_queue_items(
                     platform_approval_projection_id,source_domain,source_type,
                     source_id_digest,source_revision,status,risk_band,applicant_domain,
                     applicant_pseudonym,required_permission_code,summary_code,
                     decision_contract_code,projection_generation,projection_sha256,
                     deadline_at,created_at,updated_at)
                   VALUES(?,'security','risk_review',?,?,'pending',?,'platform',?,
                     'platform.console.approval.read','approval.security.pending',
                     'security.review',?,?,?, ?,?)""",
                (
                    "pdq_" + marker * 32,
                    marker * 64,
                    index,
                    risk,
                    (str(index + 4) * 64),
                    index,
                    str(index + 6) * 64,
                    f"2026-09-0{index + 3}T08:00:00Z",
                    STAMP,
                    STAMP,
                ),
            )
        for index, risk in enumerate(("standard", "critical"), start=1):
            marker = str(index + 2)
            connection.execute(
                """INSERT INTO workspace_domain_approval_queue_items(
                     workspace_approval_projection_id,workspace_id,tenant_id,
                     source_domain,source_type,source_id_digest,source_revision,status,
                     risk_band,applicant_domain,applicant_pseudonym,
                     required_permission_code,summary_code,decision_contract_code,
                     projection_generation,projection_sha256,deadline_at,created_at,
                     updated_at)
                   VALUES(?,?,?,'security','workspace_review',?,?,'pending',?,
                         'service',?,'workspace.console.approval.read',
                         'approval.security.pending','security.review',?,?,?,?,?)""",
                (
                    "wdq_" + marker * 32,
                    owner["workspace_id"],
                    owner["tenant_id"],
                    marker * 64,
                    index,
                    risk,
                    str(index + 7) * 64,
                    index,
                    hashlib.sha256(
                        f"workspace-approval-projection-{index}".encode("ascii")
                    ).hexdigest(),
                    f"2026-09-0{index + 5}T08:00:00Z",
                    STAMP,
                    STAMP,
                ),
            )
        connection.commit()
    expires = NOW + timedelta(hours=1)
    platform_permissions = (
        "platform.brand.read",
        "platform.capacity.read",
        "platform.channel.statement.read",
        "platform.commercial.plan.read",
        "platform.console.approval.read",
        "platform.console.assistant.query",
        "platform.console.overview.read",
        "platform.customer_success.read",
        "platform.growth.read",
        "platform.job.read",
        "platform.observability.read",
        "platform.provider_adapter.read",
        "platform.status.read",
    )
    platform_token = "p" * 43
    workspace_token = "w" * 43
    identity_token = "i" * 43
    platform = PlatformContext(
        "pts_" + "a" * 32,
        operator_id,
        platform_account_id,
        "gss_" + "a" * 32,
        "mfa_" + "a" * 32,
        1,
        1,
        ("platform_owner",),
        platform_permissions,
        expires,
    )
    workspace = WorkspaceSessionContext(
        "wss_" + "b" * 32,
        "gss_" + "b" * 32,
        owner["account_id"],
        owner["workspace_id"],
        owner["tenant_id"],
        owner["workspace_membership_id"],
        owner["legacy_user_id"],
        owner["role_code"],
        "admin",
        owner["email"],
        owner["display_name"],
        owner["slug"],
        owner["name"],
        snapshot.permission_codes,
        snapshot.permission_revision,
        snapshot.authorization_fingerprint,
        expires,
        workspace_token,
        snapshot.authorization_fingerprint,
    )
    identity = AuthenticatedIdentity(
        account_id=owner["account_id"],
        session_id="gss_" + "b" * 32,
        account=GlobalAccountSummary(
            email=owner["email"],
            display_name=owner["display_name"],
            status="active",
            mfa_configured=False,
        ),
        current_session=GlobalSessionSummary(
            session_id="gss_" + "b" * 32,
            device_family="chrome",
            created_at=NOW,
            last_seen_at=NOW,
            expires_at=expires,
            is_current=True,
            revoke_generation=1,
        ),
    )
    assert scheduler._refresh_platform_dashboard(config)
    assert scheduler._refresh_workspace_dashboards(config)
    assert scheduler._refresh_workspace_deployment_diagnostic(config)
    return {
        "config": config,
        "database": database,
        "identity": identity,
        "identity_token": identity_token,
        "platform": platform,
        "platform_token": platform_token,
        "workspace": workspace,
        "workspace_token": workspace_token,
    }


def _api_app(monkeypatch: pytest.MonkeyPatch, runtime):
    monkeypatch.setattr(
        routes,
        "authenticate_platform_session_context",
        lambda _config, _token, **_kwargs: runtime["platform"],
    )
    monkeypatch.setattr(
        routes,
        "authenticate_workspace_session_context",
        lambda _config, _token, **_kwargs: runtime["workspace"],
    )
    monkeypatch.setattr(
        routes,
        "authenticate_global_session",
        lambda _config, _token, **_kwargs: runtime["identity"],
    )
    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda _connection, _config, **_kwargs: runtime["platform"],
    )
    monkeypatch.setattr(
        service,
        "authenticate_workspace_session_in_transaction",
        lambda _connection, _config, **_kwargs: runtime["workspace"],
    )
    monkeypatch.setattr(
        service,
        "authenticate_global_session_in_transaction",
        lambda _connection, _config, **_kwargs: (runtime["identity"], NOW),
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-145 Console Experience Test",
        extra_cors_allow_headers=("Idempotency-Key", "If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = runtime["config"]
    app.include_router(router)
    return app


def _platform_headers(runtime, *, write: bool = False) -> dict[str, str]:
    cookie = f"{PLATFORM_SESSION_COOKIE}={runtime['platform_token']}"
    headers = {"Cookie": cookie}
    if write:
        csrf = "c" * 43
        headers.update(
            {
                "Cookie": f"{cookie}; {PLATFORM_CSRF_COOKIE}={csrf}",
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-site",
                "X-CSRF-Token": csrf,
            }
        )
    return headers


def _workspace_headers(runtime, *, write: bool = False) -> dict[str, str]:
    cookie = f"{WORKSPACE_SESSION_COOKIE}={runtime['workspace_token']}"
    headers = {"Cookie": cookie}
    if write:
        csrf = "d" * 43
        headers.update(
            {
                "Cookie": f"{cookie}; {WORKSPACE_CSRF_COOKIE}={csrf}",
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-site",
                "X-CSRF-Token": csrf,
            }
        )
    return headers


def _identity_headers(
    runtime,
    *,
    revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    csrf = "e" * 43
    headers = {
        "Cookie": (
            f"{IDENTITY_SESSION_COOKIE}={runtime['identity_token']}; "
            f"{IDENTITY_CSRF_COOKIE}={csrf}; "
            f"{WORKSPACE_SESSION_COOKIE}={runtime['workspace_token']}; "
            f"{WORKSPACE_CSRF_COOKIE}={'d' * 43}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": csrf,
    }
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def test_workspace_overview_resource_and_usage_totals(monkeypatch):
    """Calendar boundaries, currencies and tenant isolation use real ledger facts."""
    monkeypatch.setattr(scheduler, "_now", lambda: datetime(2026, 10, 8, 1, tzinfo=UTC))
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE workspace_memberships(workspace_id, tenant_id, status);
            CREATE TABLE edge_deployments(workspace_id, tenant_id);
            CREATE TABLE provider_health_probe_logs(tenant_id);
            CREATE TABLE dlp_rules(tenant_id);
            CREATE TABLE approval_tasks(tenant_id);
            CREATE TABLE notifications(workspace_id, tenant_id);
            CREATE TABLE providers(tenant_id, status, health_status);
            CREATE TABLE projects(tenant_id, status);
            CREATE TABLE applications(tenant_id, status);
            CREATE TABLE models(tenant_id, status);
            CREATE TABLE request_logs(tenant_id, created_at, status);
            CREATE TABLE usage_ledger(
                tenant_id, created_at, total_tokens, cost_cny, cost_usd, estimated);
            INSERT INTO workspace_memberships VALUES
                ('w','t','active'),('w','t','disabled'),('x','other','active');
            INSERT INTO projects VALUES
                ('t','active'),('t','disabled'),('t','archived'),('other','active');
            INSERT INTO applications VALUES ('t','active'),('t','archived');
            INSERT INTO models VALUES ('t','active'),('t','disabled'),('other','active');
            INSERT INTO request_logs VALUES ('t','2026-10-07T16:00:00Z','success'),
                ('t','2026-10-07T15:59:59Z','failed'),('other','2026-10-08T00:00:00Z','success');
            INSERT INTO usage_ledger VALUES ('t','2026-10-07T16:00:00Z',100,1.25,0.01,0),
                ('t','2026-09-30T16:00:00Z',200,2.50,0.02,1),
                ('t','2026-09-30T15:59:59Z',300,3.75,0.03,0),
                ('other','2026-10-08T00:00:00Z',99999,999,999,0);
        """)
        values = {metric["metric_code"]: metric["value"]
                  for metric in scheduler._workspace_dashboard_metrics(connection, "w", "t")}
    assert values["role_tasks.records"] == 1
    assert values["role_tasks.projects"] == 2
    assert values["role_tasks.applications"] == 1
    assert values["provider_health.models"] == 2
    assert values["provider_health.active_models"] == 1
    assert values["usage_cost.day_requests"] == 1
    assert values["usage_cost.month_failures"] == 1
    assert values["usage_cost.day_tokens"] == 100
    assert values["usage_cost.month_tokens"] == 300
    assert values["usage_cost.all_tokens"] == 600
    assert values["usage_cost.month_cny_micros"] == 3_750_000
    assert values["usage_cost.month_usd_micros"] == 30_000
    assert values["usage_cost.month_estimated"] == 1


@pytest.mark.anyio
async def test_domain_overviews_assistants_and_credential_isolation(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    app = _api_app(monkeypatch, runtime)
    sentinel = "PRD145-private-question-security-7f6e5d4c"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        platform = await client.get(
            "/platform/overview", headers=_platform_headers(runtime)
        )
        workspace = await client.get(
            "/workspace/overview", headers=_workspace_headers(runtime)
        )
        assert platform.status_code == workspace.status_code == 200
        assert platform.json()["domain"] == "platform"
        assert workspace.json()["domain"] == "workspace"
        assert platform.headers["cache-control"] == "no-store, private"
        assert workspace.headers["cache-control"] == "no-store, private"
        assert platform.headers["x-permission-fingerprint"] != ""
        assert workspace.headers["x-permission-fingerprint"] != ""

        owner_sections = {section["source_code"] for section in workspace.json()["sections"]}
        assert "role_tasks" in owner_sections
        owner_metrics = {
            metric["metric_code"]
            for section in workspace.json()["sections"]
            for metric in section["metrics"]
        }
        assert {"usage_cost.day_requests", "usage_cost.month_tokens",
                "usage_cost.all_tokens", "usage_cost.month_cny_micros",
                "usage_cost.month_usd_micros", "role_tasks.projects",
                "role_tasks.applications", "provider_health.models",
                "provider_health.needs_check"} <= owner_metrics

        runtime["workspace"] = replace(runtime["workspace"], membership_role="viewer")
        viewer_workspace = await client.get(
            "/workspace/overview", headers=_workspace_headers(runtime)
        )
        assert viewer_workspace.status_code == 200, viewer_workspace.text
        viewer_sections = {
            section["source_code"] for section in viewer_workspace.json()["sections"]
        }
        assert "role_tasks" not in viewer_sections
        runtime["workspace"] = replace(runtime["workspace"], membership_role="owner")

        platform_answer = await client.post(
            "/platform/assistant/query",
            headers=_platform_headers(runtime, write=True),
            json={"question": sentinel},
        )
        workspace_answer = await client.post(
            "/workspace/assistant/query",
            headers=_workspace_headers(runtime, write=True),
            json={"question": sentinel},
        )
        assert platform_answer.status_code == workspace_answer.status_code == 200
        assert platform_answer.json()["domain"] == "platform"
        assert workspace_answer.json()["domain"] == "workspace"
        assert platform_answer.json()["query_type"] == "security_summary"
        assert workspace_answer.json()["query_type"] == "security_summary"
        assert platform_answer.json()["query_id"].startswith("paq_")
        assert workspace_answer.json()["query_id"].startswith("waq_")

        unknown = await client.post(
            "/platform/assistant/query",
            headers=_platform_headers(runtime, write=True),
            json={"question": "security status", "internal_prompt": sentinel},
        )
        assert unknown.status_code == 422
        assert unknown.json()["code"] == "platform.request_invalid"

        wrong_platform_cookie = await client.get(
            "/platform/overview", headers=_workspace_headers(runtime)
        )
        wrong_workspace_cookie = await client.get(
            "/workspace/overview", headers=_platform_headers(runtime)
        )
        wrong_identity_cookie = await client.get(
            "/identity/preferences", headers=_platform_headers(runtime)
        )
        assert wrong_platform_cookie.status_code == 400
        assert wrong_platform_cookie.json()["code"] == "platform.credential_conflict"
        assert wrong_workspace_cookie.status_code == 400
        assert (
            wrong_workspace_cookie.json()["code"]
            == "workspace_session.credential_conflict"
        )
        assert wrong_identity_cookie.status_code == 400
        assert wrong_identity_cookie.json()["code"] == "identity.credential_invalid"

    database_bytes = runtime["database"].read_bytes()
    assert sentinel.encode("ascii") not in database_bytes
    assert hashlib.sha256(sentinel.encode("ascii")).hexdigest().encode("ascii") not in (
        database_bytes
    )
    with closing(connect_database(runtime["config"].database_url)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM platform_assistant_query_projections"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM workspace_assistant_query_projections"
        ).fetchone()[0] == 1


@pytest.mark.anyio
async def test_approval_queues_are_read_only_paginated_and_cursor_bound(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    app = _api_app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        first = await client.get(
            "/platform/approvals",
            params={"status": "pending", "limit": "1"},
            headers=_platform_headers(runtime),
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert len(first_body["items"]) == 1
        assert first_body["next_cursor"]
        assert first_body["items"][0]["can_open"] is True

        second = await client.get(
            "/platform/approvals",
            params={
                "status": "pending",
                "limit": "1",
                "cursor": first_body["next_cursor"],
            },
            headers=_platform_headers(runtime),
        )
        assert second.status_code == 200, second.text
        assert second.json()["next_cursor"] is None
        assert second.json()["items"][0]["projection_id"] != (
            first_body["items"][0]["projection_id"]
        )

        cursor = first_body["next_cursor"]
        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        invalid = await client.get(
            "/platform/approvals",
            params={"status": "pending", "limit": "1", "cursor": tampered},
            headers=_platform_headers(runtime),
        )
        rebound = await client.get(
            "/platform/approvals",
            params={"status": "approved", "limit": "1", "cursor": cursor},
            headers=_platform_headers(runtime),
        )
        assert invalid.status_code == rebound.status_code == 422
        assert invalid.json()["code"] == rebound.json()["code"] == (
            "platform.request_invalid"
        )

        workspace = await client.get(
            "/workspace/approvals",
            params={"limit": "1"},
            headers=_workspace_headers(runtime),
        )
        assert workspace.status_code == 200, workspace.text
        workspace_body = workspace.json()
        assert len(workspace_body["items"]) == 1
        assert workspace_body["next_cursor"]
        workspace_next = await client.get(
            "/workspace/approvals",
            params={"limit": "1", "cursor": workspace_body["next_cursor"]},
            headers=_workspace_headers(runtime),
        )
        assert workspace_next.status_code == 200, workspace_next.text
        assert workspace_next.json()["items"][0]["projection_id"] != (
            workspace_body["items"][0]["projection_id"]
        )


@pytest.mark.anyio
async def test_global_account_preference_cas_idempotency_and_outbox(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    app = _api_app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        current = await client.get(
            "/identity/preferences", headers=_identity_headers(runtime)
        )
        assert current.status_code == 200, current.text
        initial = current.json()
        assert current.headers["etag"] == f'"{initial["revision"]}"'
        target_locale = "en-US" if initial["locale"] == "zh-CN" else "zh-CN"
        key = "prd145-preference-command-0001"
        headers = _identity_headers(
            runtime, revision=initial["revision"], idempotency_key=key
        )

        unknown = await client.patch(
            "/identity/preferences",
            headers=headers,
            json={"locale": target_locale, "timezone": "UTC"},
        )
        assert unknown.status_code == 422
        assert unknown.json()["code"] == "identity.request_invalid"

        changed = await client.patch(
            "/identity/preferences", headers=headers, json={"locale": target_locale}
        )
        assert changed.status_code == 200, changed.text
        changed_body = changed.json()
        assert changed_body["changed"] is True
        assert changed_body["idempotent_replay"] is False
        assert changed_body["revision"] == initial["revision"] + 1
        assert changed.headers["cache-control"] == "no-store, private"

        replay = await client.patch(
            "/identity/preferences", headers=headers, json={"locale": target_locale}
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        changed_replay = await client.patch(
            "/identity/preferences",
            headers=headers,
            json={"locale": initial["locale"]},
        )
        assert changed_replay.status_code == 409
        assert changed_replay.json()["code"] == "identity.idempotency_conflict"

        stale = await client.patch(
            "/identity/preferences",
            headers=_identity_headers(
                runtime,
                revision=initial["revision"],
                idempotency_key="prd145-preference-command-stale",
            ),
            json={"locale": initial["locale"]},
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "identity.revision_conflict"

    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            "UPDATE account_preference_event_outbox SET next_attempt_at=?",
            ("2020-01-01T00:00:00Z",),
        )
        connection.commit()
    assert scheduler._drain_outbox(runtime["config"], "account_preference_outbox")
    with closing(connect_database(runtime["config"].database_url)) as connection:
        command = connection.execute(
            "SELECT changed,result_revision FROM account_preference_commands"
        ).fetchall()
        assert [tuple(row) for row in command] == [(1, initial["revision"] + 1)]
        outbox = connection.execute(
            "SELECT status,attempt_count,lease_owner_id,lease_token_hmac "
            "FROM account_preference_event_outbox"
        ).fetchone()
        assert tuple(outbox) == ("dispatched", 1, None, None)


@pytest.mark.anyio
async def test_deployment_summary_is_whitelisted_and_scheduler_fence_is_cas(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    app = _api_app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.get(
            "/workspace/deployment-summary", headers=_workspace_headers(runtime)
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "operator"
    assert len(body["items"]) == 1
    assert set(body["items"][0]) == {
        "deployment_ref",
        "deployment_kind",
        "region_code",
        "release_band",
        "compatibility_band",
        "desired_generation",
        "observed_generation",
        "health_state",
        "reason_code",
        "action_code",
        "evidence_digest",
        "observed_at",
    }
    serialized = response.text.lower()
    for forbidden in ("base_url", "endpoint", "secret", "token", "environment", "path"):
        assert forbidden not in serialized

    assert scheduler._drain_outbox(runtime["config"], "workspace_outbox")
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            """UPDATE console_experience_scheduler_state
               SET next_attempt_at=?,lease_owner_id=NULL,lease_token_hmac=NULL,
                 lease_expires_at=NULL
               WHERE worker_kind='cleanup'""",
            ("2020-01-01T00:00:00Z",),
        )
        connection.commit()
    lease = scheduler._claim(runtime["config"], "cleanup")
    assert lease is not None
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            "UPDATE console_experience_scheduler_state SET fence_generation=? "
            "WHERE worker_kind='cleanup'",
            (lease.generation + 1,),
        )
        connection.commit()
    with pytest.raises(ApiError):
        scheduler._release(runtime["config"], lease, worked=True)
    with closing(connect_database(runtime["config"].database_url)) as connection:
        state = connection.execute(
            "SELECT fence_generation,lease_owner_id FROM console_experience_scheduler_state "
            "WHERE worker_kind='cleanup'"
        ).fetchone()
        assert tuple(state) == (lease.generation + 1, scheduler.WORKER_ID)
        connection.execute(
            """UPDATE console_experience_scheduler_state
               SET lease_owner_id=NULL,lease_token_hmac=NULL,lease_expires_at=NULL
               WHERE worker_kind='cleanup'"""
        )
        connection.commit()


def test_workspace_approval_projection_rejects_revision_regression(runtime) -> None:
    task_id = "apr_" + "e" * 32
    rule_id = "br_prd145_revision"
    block_id = "bb_prd145_revision"
    projection_id = scheduler._stable_id(
        "wdq_", f"workspace-approval:{task_id}"
    )
    with closing(connect_database(runtime["config"].database_url)) as connection:
        workspace = runtime["workspace"]
        environment_id = connection.execute(
            "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
            (workspace.tenant_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO budget_rules(
                 budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
                 daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
                 exhausted_action,status,created_at,updated_at)
               VALUES(?,?,?,'tenant',?,'PRD145 revision rule',10,80,100,'block',
                 'active',?,?)""",
            (
                rule_id,
                workspace.tenant_id,
                environment_id,
                workspace.tenant_id,
                STAMP,
                STAMP,
            ),
        )
        connection.execute(
            """INSERT INTO budget_blocks(
                 budget_block_id,tenant_id,environment_id,budget_rule_id,scope_type,
                 scope_id,budget_date,status,blocked_cost_cny,budget_cny,
                 blocked_request_id,blocked_trace_id,blocked_at,created_at,updated_at)
               VALUES(?,?,?,?,'tenant',?,'2026-09-03','active',10,10,?,?,?, ?,?)""",
            (
                block_id,
                workspace.tenant_id,
                environment_id,
                rule_id,
                workspace.tenant_id,
                "req_prd145_budget_block",
                "trace_prd145_budget_block",
                STAMP,
                STAMP,
                STAMP,
            ),
        )
        connection.execute(
            """INSERT INTO approval_tasks(
                 approval_task_id,tenant_id,source_type,source_id,target_type,
                 target_id,target_action,target_reason_code,target_version,
                 target_fingerprint,risk_level,status,resolution_reason,state_revision,
                 writeback_status,required_permission_code,created_by_type,created_by_id,
                 decided_by_user_id,decided_at,expires_at,create_hmac_key_version,
                 create_idempotency_key_hmac,create_intent_hmac,source_request_id,
                 source_trace_id,decision_request_id,decision_trace_id,created_at,updated_at)
               VALUES(?,?,'budget',?,'budget_block_release',?,'release',NULL,'v2',?,
                 'standard','pending',NULL,1,'pending','billing.budget.write','service',
                 'prd145-scheduler',NULL,NULL,'2099-12-31T23:59:59Z',1,?,?,?, ?,
                 NULL,NULL,?,?)""",
            (
                task_id,
                workspace.tenant_id,
                block_id,
                rule_id,
                hashlib.sha256(b"prd145-target").hexdigest(),
                hashlib.sha256(b"prd145-create-key").hexdigest(),
                hashlib.sha256(b"prd145-create-intent").hexdigest(),
                "req_prd145_projection",
                "trace_prd145_projection",
                STAMP,
                STAMP,
            ),
        )
        task_count = connection.execute(
            "SELECT count(*) FROM approval_tasks"
        ).fetchone()[0]
        connection.commit()

    for _ in range(task_count + 1):
        scheduler._project_workspace_approval(runtime["config"])
        with closing(connect_database(runtime["config"].database_url)) as connection:
            if connection.execute(
                "SELECT 1 FROM workspace_domain_approval_queue_items "
                "WHERE workspace_approval_projection_id=?",
                (projection_id,),
            ).fetchone():
                break
    else:
        pytest.fail("workspace approval projection was not created")

    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            """UPDATE workspace_domain_approval_queue_items
               SET source_revision=2,projection_generation=2,projection_sha256=?
               WHERE workspace_approval_projection_id=?""",
            (hashlib.sha256(b"prd145-newer-projection").hexdigest(), projection_id),
        )
        connection.execute(
            """UPDATE workspace_domain_approval_source_cursors
               SET source_revision=2,source_watermark='approval:2',generation=2
               WHERE workspace_id=? AND tenant_id=? AND source_domain='budget'
                 AND source_type='budget_block_release'""",
            (workspace.workspace_id, workspace.tenant_id),
        )
        connection.commit()
    assert scheduler._project_workspace_approval(runtime["config"])
    with closing(connect_database(runtime["config"].database_url)) as connection:
        projected_revision = connection.execute(
            "SELECT source_revision FROM workspace_domain_approval_queue_items "
            "WHERE workspace_approval_projection_id=?",
            (projection_id,),
        ).fetchone()[0]
        rejection = connection.execute(
            "SELECT reason_code,source_revision FROM workspace_domain_approval_rejections "
            "WHERE source_domain='budget' AND source_type='budget_block_release' "
            "AND source_id_digest=?",
            (
                scheduler._pseudonym(
                    runtime["config"], "workspace-approval-source", block_id
                ),
            ),
        ).fetchone()
        cursor_revision = connection.execute(
            """SELECT source_revision FROM workspace_domain_approval_source_cursors
               WHERE workspace_id=? AND tenant_id=? AND source_domain='budget'
                 AND source_type='budget_block_release'""",
            (runtime["workspace"].workspace_id, runtime["workspace"].tenant_id),
        ).fetchone()[0]
        assert projected_revision == 2
        assert tuple(rejection) == ("source_revision_regressed", 1)
        assert cursor_revision >= 2


@pytest.mark.parametrize(
    ("function_name", "domain", "action"),
    [
        ("get_platform_overview", "platform", "platform.overview.read"),
        ("get_workspace_overview", "workspace", "workspace.overview.read"),
        ("query_platform_assistant", "platform", "platform.assistant.query"),
        ("query_workspace_assistant", "workspace", "workspace.assistant.query"),
        ("list_platform_approvals", "platform", "platform.approvals.read"),
        ("list_workspace_approvals", "workspace", "workspace.approvals.read"),
        ("get_account_preferences", "identity", "identity.preferences.read"),
        (
            "get_workspace_deployment_summary",
            "workspace",
            "workspace.deployment_summary.read",
        ),
    ],
)
def test_console_reads_serialize_audit_with_competing_writer(
    monkeypatch, runtime, record_property, function_name, domain, action
) -> None:
    config = runtime["config"]
    with closing(connect_database(config.database_url)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

    writer_outcomes = []

    def competing_write():
        with closing(connect_database(config.database_url)) as writer:
            writer.execute("PRAGMA busy_timeout=0")
            try:
                writer.execute("BEGIN IMMEDIATE")
                assert writer.execute(
                    "UPDATE console_experience_scheduler_state "
                    "SET attempt_count=attempt_count+1 WHERE worker_kind='reconciliation'"
                ).rowcount == 1
                writer.commit()
                return "committed"
            except sqlite3.OperationalError as error:
                assert error.sqlite_errorname == "SQLITE_BUSY"
                writer.rollback()
                return "blocked"

    def authenticate(connection, _config, **_kwargs):
        # Establish a real read snapshot before the independent writer attempts its commit.
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        writer_outcomes.append(competing_write())
        if domain == "identity":
            return runtime["identity"], NOW
        return runtime[domain]

    auth_name = (
        "authenticate_global_session_in_transaction" if domain == "identity"
        else f"authenticate_{domain}_session_in_transaction"
    )
    monkeypatch.setattr(service, auth_name, authenticate)
    request_id = "req_prd145_serial_" + function_name
    arguments = {
        "session_token": runtime[f"{domain}_token"],
        "request_id": request_id,
        "trace_id": "trace_prd145_serial",
    }
    if function_name.startswith("query_"):
        arguments["question"] = "status"
    if function_name.startswith("list_"):
        arguments.update(
            status_filter=None, risk_filter=None, source_domain=None, cursor=None, limit=10
        )
    try:
        result = getattr(service, function_name)(config, **arguments)
    except ApiError as error:
        record_property("database_failure", getattr(error.__context__, "sqlite_errorname", "none"))
        raise
    assert result.request_id == request_id
    assert writer_outcomes == ["blocked"]
    assert competing_write() == "committed"
    with closing(connect_database(config.database_url)) as connection:
        rows = connection.execute(
            "SELECT action_code FROM console_experience_read_audits WHERE request_id=?",
            (request_id,),
        ).fetchall()
        assert [row[0] for row in rows] == [action]
    record_property("writer_resumed_after_request_commit", True)


@pytest.mark.parametrize("domain", ["platform", "workspace"])
def test_assistant_audit_failure_rolls_back_query_and_allows_recovery(
    monkeypatch, runtime, domain
) -> None:
    config = runtime["config"]
    monkeypatch.setattr(
        service,
        f"authenticate_{domain}_session_in_transaction",
        lambda _connection, _config, **_kwargs: runtime[domain],
    )
    request_id = "req_prd145_atomic_" + domain
    function = getattr(service, f"query_{domain}_assistant")
    arguments = {
        "session_token": runtime[f"{domain}_token"],
        "question": "status",
        "request_id": request_id,
        "trace_id": "trace_prd145_atomic",
    }

    def failed_audit(*_args, **_kwargs):
        raise sqlite3.OperationalError("test_audit_write_failure")

    with monkeypatch.context() as fault:
        fault.setattr(service, "_write_read_audit", failed_audit)
        with pytest.raises(ApiError) as failure:
            function(config, **arguments)
        expected = (
            "platform.unavailable" if domain == "platform" else "workspace_session.unavailable"
        )
        assert failure.value.code == expected
    with closing(connect_database(config.database_url)) as connection:
        for table in (
            f"{domain}_assistant_query_projections",
            "console_experience_read_audits",
        ):
            assert connection.execute(
                f"SELECT count(*) FROM {table} WHERE request_id=?", (request_id,)
            ).fetchone()[0] == 0
    assert function(config, **arguments).request_id == request_id
    with closing(connect_database(config.database_url)) as connection:
        for table in (
            f"{domain}_assistant_query_projections",
            "console_experience_read_audits",
        ):
            assert connection.execute(
                f"SELECT count(*) FROM {table} WHERE request_id=?", (request_id,)
            ).fetchone()[0] == 1


def test_poison_outbox_persists_failed_attempt(runtime) -> None:
    event_id = "pce_" + "f" * 32
    outbox_id = "pco_" + "f" * 32
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """INSERT INTO platform_console_events(
                 platform_console_event_id,event_type,object_type,object_id,
                 event_key_sha256,payload_json,created_at)
               VALUES(?,'platform_dashboard.refreshed','dashboard','poison-prd145',
                 ?,'not-json','2020-01-01T00:00:00Z')""",
            (event_id, "f" * 64),
        )
        connection.execute(
            """INSERT INTO platform_console_event_outbox(
                 platform_console_event_outbox_id,platform_console_event_id,status,
                 attempt_count,next_attempt_at,lease_owner_id,lease_generation,
                 lease_token_hmac,lease_expires_at,dispatched_at,created_at,updated_at)
               VALUES(?,?,'pending',0,'2020-01-01T00:00:00Z',NULL,0,NULL,NULL,NULL,
                 '2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')""",
            (outbox_id, event_id),
        )
        connection.execute("PRAGMA ignore_check_constraints = OFF")
        connection.commit()

    with pytest.raises(ApiError):
        scheduler._drain_outbox(runtime["config"], "platform_outbox")
    with closing(connect_database(runtime["config"].database_url)) as connection:
        row = connection.execute(
            """SELECT status,attempt_count,lease_owner_id,lease_token_hmac,
                 lease_expires_at,next_attempt_at
               FROM platform_console_event_outbox
               WHERE platform_console_event_outbox_id=?""",
            (outbox_id,),
        ).fetchone()
    assert tuple(row[:5]) == ("failed", 1, None, None, None)
    retry_at = datetime.fromisoformat(row["next_attempt_at"].replace("Z", "+00:00"))
    assert retry_at > datetime.now(UTC) + timedelta(minutes=4)
