"""PRD-140 Platform customer-success API, state, and isolation contracts."""

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
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_customer_success import outbox, projection, routes, scheduler, service
from metahub_control.platform_customer_success.routes import router
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
CSRF = "c" * 43
STEP_UP = "u" * 43
STAMP = "2026-09-01T00:00:00.000Z"
ROLE_BY_NAME = {
    "customer_success": "platform_customer_success",
    "support": "platform_support",
    "billing": "platform_billing",
    "owner1": "platform_owner",
    "owner2": "platform_owner",
}


@dataclass(frozen=True, slots=True)
class Runtime:
    config: AppConfig
    now: datetime
    account_ids: tuple[str, ...]
    health_sequence: int
    renewal_id: str
    tokens: dict[str, str]
    contexts: dict[str, PlatformContext]
    operator_ids: dict[str, str]


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
        approval_hmac_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, datetime]:
    database = tmp_path_factory.mktemp("prd140-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0139"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", f"Aa9!{secrets.token_urlsafe(24)}")
        seed = apply_seed_data(database_url)
    now = datetime.now(UTC) + timedelta(minutes=2)
    with closing(connect_database(database_url)) as connection:
        owner = connection.execute(
            """SELECT binding.workspace_id,binding.tenant_id,membership.workspace_membership_id
                 FROM workspace_tenant_bindings binding
                 JOIN workspace_memberships membership
                   ON membership.workspace_id=binding.workspace_id
                  AND membership.tenant_id=binding.tenant_id
                WHERE binding.tenant_id=? AND membership.role_code='owner'
                  AND membership.status='active' LIMIT 1""",
            (seed.tenant_id,),
        ).fetchone()
        assert owner is not None
        operator_ids: dict[str, str] = {}
        for index, (name, role) in enumerate(ROLE_BY_NAME.items(), start=1):
            marker = format(index, "x")
            account_id = "gac_" + marker * 32
            operator_id = "pto_" + marker * 32
            connection.execute(
                """INSERT INTO global_accounts(
                  account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                  credential_revision,session_revision,mfa_state,failed_login_count,
                  locked_until,last_login_at,created_at,updated_at)
                VALUES(?,?,NULL,?,'active',0,1,1,'not_enrolled',0,NULL,NULL,?,?)""",
                (account_id, f"prd140-{name}@example.invalid", name, STAMP, STAMP),
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
        connection.execute(
            """INSERT INTO tenants(
              tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,'prd140-secondary','PRD-140 Secondary','internal_only','active',?,?)""",
            ("ten_" + "8" * 32, STAMP, STAMP),
        )
        plan_catalog_id = "spc_" + "7" * 32
        plan_version_id = "spv_" + "7" * 32
        connection.execute(
            """INSERT INTO saas_plan_catalogs(
              plan_catalog_id,plan_code,display_name,status,display_order,revision,
              created_by_operator_id,created_at,updated_at)
            VALUES(?,'prd140-test','PRD-140 Test','active',1,1,?,?,?)""",
            (plan_catalog_id, operator_ids["owner1"], STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO saas_plan_versions(
              plan_version_id,plan_catalog_id,version_number,status,billing_period,currency,
              unit_price_minor,trial_days,entitlements_json,entitlements_sha256,
              created_by_operator_id,published_at,created_at)
            VALUES(?,?,1,'published','yearly','CNY',120000,0,'{}',?,?,?,?)""",
            (
                plan_version_id,
                plan_catalog_id,
                hashlib.sha256(b"{}").hexdigest(),
                operator_ids["owner1"],
                STAMP,
                STAMP,
            ),
        )
        renewal_at = (
            (now + timedelta(days=90)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        connection.execute(
            """INSERT INTO workspace_subscriptions(
              subscription_id,workspace_id,tenant_id,plan_version_id,pending_plan_version_id,
              status,current_period_started_at,current_period_ends_at,pending_effective_at,
              state_revision,created_by_membership_id,created_at,updated_at)
            VALUES(?,?,?,?,NULL,'active',?,?,NULL,1,?,?,?)""",
            (
                "sub_" + "7" * 32,
                owner["workspace_id"],
                owner["tenant_id"],
                plan_version_id,
                STAMP,
                renewal_at,
                owner["workspace_membership_id"],
                STAMP,
                STAMP,
            ),
        )
        assert projection.reconcile_accounts(connection, now=now) == 2
        primary = connection.execute(
            """SELECT customer_account_id FROM platform_customer_account_projections
                WHERE tenant_id=?""",
            (seed.tenant_id,),
        ).fetchone()[0]
        assert projection.materialize_health_v2(connection, primary, now=now) is not None
        assert projection.reconcile_renewals(connection, now=now) == 1
        connection.commit()
    return database, now


@pytest.fixture
def runtime(base_database: tuple[Path, datetime], tmp_path: Path) -> Runtime:
    base, now = base_database
    database = tmp_path / "prd140.sqlite3"
    shutil.copy2(base, database)
    config = _config(_url(database))
    tokens: dict[str, str] = {}
    contexts: dict[str, PlatformContext] = {}
    operator_ids: dict[str, str] = {}
    with closing(connect_database(config.database_url)) as connection:
        accounts = tuple(
            row[0]
            for row in connection.execute(
                """SELECT customer_account_id FROM platform_customer_account_projections
                    ORDER BY CASE WHEN latest_health_sequence IS NULL THEN 1 ELSE 0 END,
                             customer_account_id"""
            )
        )
        health_sequence = int(
            connection.execute(
                "SELECT source_sequence FROM platform_customer_health_heads LIMIT 1"
            ).fetchone()[0]
        )
        renewal_id = str(
            connection.execute(
                "SELECT renewal_opportunity_id FROM platform_renewal_opportunities LIMIT 1"
            ).fetchone()[0]
        )
        expires = now + timedelta(hours=1)
        for index, name in enumerate(ROLE_BY_NAME, start=1):
            marker = format(index, "x")
            token = chr(96 + index) * 43
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
                expires_at=expires,
            )
    return Runtime(
        config=config,
        now=now,
        account_ids=accounts,
        health_sequence=health_sequence,
        renewal_id=renewal_id,
        tokens=tokens,
        contexts=contexts,
        operator_ids=operator_ids,
    )


def _app(monkeypatch: pytest.MonkeyPatch, runtime: Runtime):
    def current_context(connection, token: str) -> PlatformContext:
        context = runtime.contexts.get(token)
        if context is None:
            raise ApiError("platform.session_required")
        operator = connection.execute(
            "SELECT status,revision FROM platform_operators WHERE operator_id=?",
            (context.operator_id,),
        ).fetchone()
        if operator is None or operator["status"] != "active":
            raise ApiError("platform.session_invalid")
        roles, permissions, _fingerprint = platform_access_service._authority(
            connection, context.operator_id
        )
        return replace(
            context,
            roles=roles,
            permissions=permissions,
            operator_revision=int(operator["revision"]),
        )

    def service_actor(connection, _config, *, session_token, **_kwargs):
        return current_context(connection, session_token)

    def route_actor(_config, session_token, *, csrf_token=None):
        del csrf_token
        with closing(connect_database(runtime.config.database_url)) as connection:
            return current_context(connection, session_token)

    monkeypatch.setattr(service, "authenticate_platform_session_in_transaction", service_actor)
    monkeypatch.setattr(routes, "authenticate_platform_session_context", route_actor)
    monkeypatch.setattr(service, "_utcnow", lambda: runtime.now + timedelta(seconds=1))
    monkeypatch.setattr(
        service,
        "consume_global_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: "mfa",
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-140 Customer Success Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = runtime.config
    app.include_router(router)
    return app


def _headers(token: str, key: str | None = None, revision: int = 0) -> dict[str, str]:
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


def _plan_create(runtime: Runtime) -> dict[str, object]:
    return {
        "operation": "create",
        "customer_account_id": runtime.account_ids[0],
        "expected_revision": 0,
        "objective_code": "adoption",
        "starts_on": "2026-09-01",
        "ends_on": "2026-12-01",
        "target_health_bps": 8000,
        "target_activation_steps": 8,
        "target_adoption_bps": 7500,
        "source_health_sequence": runtime.health_sequence,
        "confirmed": True,
    }


def _task_create(runtime: Runtime, *, plan_id: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "operation": "create_one",
        "customer_account_id": runtime.account_ids[0],
        "task_type": "adoption_follow_up",
        "priority": "high",
        "reason_code": "adoption.follow_up",
        "assignee_operator_id": runtime.operator_ids["customer_success"],
        "due_at": "2026-09-15T00:00:00Z",
        "required": True,
        "expected_revision": 0,
        "confirmed": True,
    }
    if plan_id is not None:
        body["customer_success_plan_id"] = plan_id
    return body


def test_platform_navigation_exposes_exact_customer_success_read_scopes() -> None:
    permission_sets = (
        {"platform.customer_success.read"},
        {"platform.customer_success.support_read"},
        {"platform.customer_success.finance_read", "platform.customer_success.renewal.read"},
    )
    for permissions in permission_sets:
        items = platform_access_service._navigation_items(permissions)
        assert [item.id for item in items].count("customer-success") == 1
    assert "customer-success" not in {
        item.id for item in platform_access_service._navigation_items({"platform.audit.read"})
    }


@pytest.mark.anyio
async def test_production_route_table_returns_ordinary_404_for_legacy_customer_health() -> None:
    from metahub_control.app import app as production_app

    async with AsyncClient(
        transport=ASGITransport(app=production_app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/customer-health", headers={"X-Request-ID": "req_prd140_legacy_route"}
        )
    assert response.status_code == 404
    assert response.json()["code"] == "infra.not_found"
    assert response.json()["request_id"] == "req_prd140_legacy_route"
    assert "retired" not in response.text.lower()


@pytest.mark.anyio
async def test_identity_query_cursor_and_exact_role_projections(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    app = _app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        anonymous = await client.get("/platform/customer-accounts")
        assert anonymous.status_code == 401
        assert anonymous.json()["code"] == "platform.session_required"
        conflict = await client.get(
            "/platform/customer-accounts",
            headers={
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={runtime.tokens['customer_success']}; "
                    f"{WORKSPACE_SESSION_COOKIE}={'w' * 43}"
                )
            },
        )
        assert conflict.json()["code"] == "platform.credential_conflict"
        bad_query = await client.get(
            "/platform/customer-accounts?tenant_id=hidden",
            headers=_headers(runtime.tokens["customer_success"]),
        )
        assert bad_query.status_code == 422
        directory = await client.get(
            "/platform/customer-accounts?limit=1",
            headers=_headers(runtime.tokens["customer_success"]),
        )
        assert directory.status_code == 200, directory.text
        customer_item = directory.json()["items"][0]
        assert customer_item["scope"] == "customer_success"
        assert "subscription_id" not in customer_item and "email" not in directory.text
        cursor = directory.json()["next_cursor"]
        assert cursor
        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        rejected_cursor = await client.get(
            f"/platform/customer-accounts?limit=1&cursor={tampered}",
            headers=_headers(runtime.tokens["customer_success"]),
        )
        assert rejected_cursor.status_code == 422
        support = await client.get(
            "/platform/customer-accounts",
            headers=_headers(runtime.tokens["support"]),
        )
        support_item = support.json()["items"][0]
        assert set(support_item) == {
            "scope",
            "customer_account_id",
            "workspace_id",
            "operational_status",
            "health_risk",
            "open_support_ticket_count",
            "breached_sla_count",
            "projection_updated_at",
        }
        billing = await client.get(
            "/platform/customer-accounts",
            headers=_headers(runtime.tokens["billing"]),
        )
        billing_item = next(
            item for item in billing.json()["items"] if item["subscription_status"] == "active"
        )
        assert "display_name" not in billing_item and "health_risk" not in billing_item
        owner = await client.get(
            "/platform/customer-accounts",
            headers=_headers(runtime.tokens["owner1"]),
        )
        owner_item = next(
            item for item in owner.json()["items"] if item["subscription_status"] == "active"
        )
        assert owner_item["scope"] == "owner"
        assert owner_item["display_name"] and owner_item["subscription_id"]

        customer_renewal = await client.get(
            "/platform/renewal-pipeline",
            headers=_headers(runtime.tokens["customer_success"]),
        )
        assert customer_renewal.status_code == 200, customer_renewal.text
        assert set(customer_renewal.json()["items"][0]) == {
            "scope",
            "renewal_opportunity_id",
            "customer_account_id",
            "workspace_id",
            "display_name",
            "renewal_at",
            "health_risk_code",
            "adoption_risk_code",
            "payment_risk_code",
            "invoice_status",
            "status",
            "open_plan_count",
            "open_task_count",
            "revision",
        }
        finance_renewal = await client.get(
            "/platform/renewal-pipeline",
            headers=_headers(runtime.tokens["billing"]),
        )
        finance_renewal_item = finance_renewal.json()["items"][0]
        assert "subscription_id" in finance_renewal_item
        assert "display_name" not in finance_renewal_item
        assert "health_risk_code" not in finance_renewal_item

        account_id = runtime.account_ids[0]
        health = await client.get(
            f"/platform/customer-accounts/{account_id}/health",
            headers=_headers(runtime.tokens["customer_success"]),
        )
        assert health.status_code == 200, health.text
        assert len(health.json()["factors"]) == 6 and "score_bps" in health.json()
        support_health = await client.get(
            f"/platform/customer-accounts/{account_id}/health",
            headers=_headers(runtime.tokens["support"]),
        )
        assert support_health.status_code == 200, support_health.text
        assert [item["code"] for item in support_health.json()["factors"]] == ["ticket_health"]
        assert "score_bps" not in support_health.json()
        assert "window_start" not in support_health.json()
        denied_health = await client.get(
            f"/platform/customer-accounts/{account_id}/health",
            headers=_headers(runtime.tokens["billing"]),
        )
        assert denied_health.status_code == 403

        contact = await client.get(
            f"/platform/customer-accounts?customer_account_id={account_id}&view=contact",
            headers=_headers(runtime.tokens["customer_success"]),
        )
        assert contact.status_code == 200, contact.text
        assert contact.json()["contacts"] and contact.json()["contacts"][0]["email"]
        with closing(connect_database(runtime.config.database_url)) as connection:
            assert (
                connection.execute(
                    """SELECT count(*) FROM platform_audit_logs
                    WHERE action_code='platform.customer_contact_read'"""
                ).fetchone()[0]
                == 1
            )
            connection.execute(
                """UPDATE platform_customer_contacts
                      SET membership_revision=membership_revision+1,
                          revision=revision+1,
                          updated_at=strftime('%Y-%m-%dT%H:%M:%fZ',updated_at,'+1 second')
                    WHERE customer_account_id=?""",
                (account_id,),
            )
            connection.commit()
        stale_contact = await client.get(
            f"/platform/customer-accounts?customer_account_id={account_id}&view=contact",
            headers=_headers(runtime.tokens["customer_success"]),
        )
        assert stale_contact.status_code == 200, stale_contact.text
        assert stale_contact.json()["contacts"] == []
        assert "@example.invalid" not in stale_contact.text


@pytest.mark.anyio
async def test_contact_source_failure_is_fixed_and_rolls_back_sensitive_audit(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    def unavailable_contact_source(*_args, **_kwargs):
        raise ValueError("synthetic contact source failure")

    app = _app(monkeypatch, runtime)
    monkeypatch.setattr(service, "_contacts", unavailable_contact_source)
    account_id = runtime.account_ids[0]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/platform/customer-accounts?customer_account_id={account_id}&view=contact",
            headers=_headers(runtime.tokens["customer_success"]),
        )
    assert response.status_code == 503
    assert response.json()["code"] == "platform.unavailable"
    assert "@example.invalid" not in response.text
    assert "synthetic contact source failure" not in response.text
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert (
            connection.execute(
                """SELECT count(*) FROM platform_audit_logs
                    WHERE action_code='platform.customer_contact_read'"""
            ).fetchone()[0]
            == 0
        )


@pytest.mark.anyio
async def test_plan_task_idempotency_cas_terminal_and_events(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    app = _app(monkeypatch, runtime)
    token = runtime.tokens["customer_success"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        create = await client.post(
            "/platform/customer-success-plans",
            headers=_headers(token, "prd140-plan-create-0001"),
            json=_plan_create(runtime),
        )
        assert create.status_code == 201, create.text
        plan = create.json()["plan"]
        replay = await client.post(
            "/platform/customer-success-plans",
            headers=_headers(token, "prd140-plan-create-0001"),
            json=_plan_create(runtime),
        )
        assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
        changed_body = {**_plan_create(runtime), "target_health_bps": 8100}
        conflict = await client.post(
            "/platform/customer-success-plans",
            headers=_headers(token, "prd140-plan-create-0001"),
            json=changed_body,
        )
        assert conflict.status_code == 409
        stale = await client.post(
            "/platform/customer-success-plans",
            headers=_headers(token, "prd140-plan-stale-0001"),
            json={**_plan_create(runtime), "source_health_sequence": runtime.health_sequence + 1},
        )
        assert stale.status_code == 412
        invalid_shape = await client.post(
            "/platform/customer-success-plans",
            headers=_headers(token, "prd140-plan-shape-0001", 1),
            json={
                "operation": "activate",
                "customer_account_id": runtime.account_ids[0],
                "customer_success_plan_id": plan["customer_success_plan_id"],
                "expected_revision": 1,
                "target_health_bps": None,
                "confirmed": True,
            },
        )
        assert invalid_shape.status_code == 422
        activate = await client.post(
            "/platform/customer-success-plans",
            headers=_headers(token, "prd140-plan-activate-0001", 1),
            json={
                "operation": "activate",
                "customer_account_id": runtime.account_ids[0],
                "customer_success_plan_id": plan["customer_success_plan_id"],
                "expected_revision": 1,
                "confirmed": True,
            },
        )
        assert activate.status_code == 200 and activate.json()["plan"]["status"] == "active"
        task = await client.post(
            "/platform/customer-tasks",
            headers=_headers(token, "prd140-task-create-0001"),
            json=_task_create(runtime, plan_id=plan["customer_success_plan_id"]),
        )
        assert task.status_code == 201, task.text
        task_id = task.json()["task"]["customer_task_id"]
        completed_task = await client.post(
            "/platform/customer-tasks",
            headers=_headers(token, "prd140-task-complete-0001", 1),
            json={
                "operation": "transition_one",
                "customer_task_id": task_id,
                "target_status": "completed",
                "reason_code": "task.completed",
                "expected_revision": 1,
                "confirmed": True,
            },
        )
        assert completed_task.status_code == 200
        terminal_task = await client.post(
            "/platform/customer-tasks",
            headers=_headers(token, "prd140-task-terminal-0001", 2),
            json={
                "operation": "transition_one",
                "customer_task_id": task_id,
                "target_status": "in_progress",
                "reason_code": "task.reopened",
                "expected_revision": 2,
                "confirmed": True,
            },
        )
        assert terminal_task.status_code == 412
        complete_plan = await client.post(
            "/platform/customer-success-plans",
            headers=_headers(token, "prd140-plan-complete-0001", 2),
            json={
                "operation": "complete",
                "customer_account_id": runtime.account_ids[0],
                "customer_success_plan_id": plan["customer_success_plan_id"],
                "expected_revision": 2,
                "confirmed": True,
            },
        )
        assert complete_plan.status_code == 200
        terminal_plan = await client.post(
            "/platform/customer-success-plans",
            headers=_headers(token, "prd140-plan-terminal-0001", 3),
            json={
                "operation": "cancel",
                "customer_account_id": runtime.account_ids[0],
                "customer_success_plan_id": plan["customer_success_plan_id"],
                "expected_revision": 3,
                "confirmed": True,
            },
        )
        assert terminal_plan.status_code == 412
    with closing(connect_database(runtime.config.database_url)) as connection:
        event = connection.execute(
            """SELECT payload_json FROM platform_customer_success_events
                WHERE event_type='success_plan.completed'"""
        ).fetchone()
        assert event is not None and '"source_digest"' in event["payload_json"]


@pytest.mark.anyio
async def test_batch_independent_approval_and_requester_revocation(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    app = _app(monkeypatch, runtime)
    task_owner = runtime.operator_ids["customer_success"]
    prepare_body = {
        "operation": "prepare_batch",
        "customer_account_ids": list(runtime.account_ids),
        "task_type": "health_review",
        "priority": "normal",
        "reason_code": "health.batch_review",
        "assignee_operator_id": task_owner,
        "due_at": "2026-09-20T00:00:00Z",
        "expected_revision": 0,
        "confirmed": True,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        prepared = await client.post(
            "/platform/customer-tasks",
            headers=_headers(runtime.tokens["customer_success"], "prd140-batch-prepare-0001"),
            json=prepare_body,
        )
        assert prepared.status_code == 201, prepared.text
        batch_id = prepared.json()["batch"]["customer_task_batch_id"]
        approved = await client.post(
            "/platform/customer-tasks",
            headers=_headers(runtime.tokens["owner2"], "prd140-batch-approve-0001", 1),
            json={
                "operation": "approve_batch",
                "customer_task_batch_id": batch_id,
                "decision": "approved",
                "reason_code": "owner.approved",
                "expected_revision": 1,
                "confirmed": True,
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["batch"]["result_count"] == len(runtime.account_ids)
        prepared_again = await client.post(
            "/platform/customer-tasks",
            headers=_headers(runtime.tokens["customer_success"], "prd140-batch-prepare-0002"),
            json=prepare_body,
        )
        second_batch = prepared_again.json()["batch"]["customer_task_batch_id"]
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                """UPDATE platform_operator_role_bindings
                      SET status='revoked',revoked_at=?
                    WHERE operator_id=? AND role_code='platform_customer_success'""",
                (STAMP, task_owner),
            )
            before = connection.execute("SELECT count(*) FROM platform_customer_tasks").fetchone()[
                0
            ]
            connection.commit()
        revoked = await client.post(
            "/platform/customer-tasks",
            headers=_headers(runtime.tokens["owner2"], "prd140-batch-approve-0002", 1),
            json={
                "operation": "approve_batch",
                "customer_task_batch_id": second_batch,
                "decision": "approved",
                "reason_code": "owner.approved",
                "expected_revision": 1,
                "confirmed": True,
            },
        )
        assert revoked.status_code == 403
        assert revoked.json()["code"] == "platform.permission_denied"
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert (
            connection.execute("SELECT count(*) FROM platform_customer_tasks").fetchone()[0]
            == before
        )
        assert (
            connection.execute(
                """SELECT status FROM platform_customer_task_batches
                WHERE customer_task_batch_id=?""",
                (second_batch,),
            ).fetchone()[0]
            == "awaiting_approval"
        )


def test_due_event_and_outbox_takeover(runtime: Runtime) -> None:
    command = service.CustomerTaskCommand.model_validate(_task_create(runtime))
    monkey_context = runtime.contexts[runtime.tokens["customer_success"]]
    original_actor = service.authenticate_platform_session_in_transaction
    original_now = service._utcnow
    service.authenticate_platform_session_in_transaction = lambda *_args, **_kwargs: monkey_context
    service._utcnow = lambda: runtime.now
    try:
        response = service.apply_customer_task_command(
            runtime.config,
            command,
            session_token=runtime.tokens["customer_success"],
            csrf_token=CSRF,
            step_up_grant=None,
            idempotency_key="prd140-due-task-create-0001",
            request_id="req_prd140_due_task",
            trace_id="trc_prd140_due_task",
        )
    finally:
        service.authenticate_platform_session_in_transaction = original_actor
        service._utcnow = original_now
    assert response.task is not None
    due_at = datetime(2026, 9, 16, tzinfo=UTC)
    with closing(connect_database(runtime.config.database_url)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert projection.emit_due_task_events(connection, now=due_at) == 1
        assert projection.emit_due_task_events(connection, now=due_at) == 0
        connection.commit()
    first = outbox.claim_next_event(runtime.config, worker_id="prd140-worker-a", now=due_at)
    assert first is not None
    assert (
        outbox.fail_event(
            runtime.config,
            first,
            error_code="event.delivery_unavailable",
            now=due_at + timedelta(seconds=31),
        )
        is False
    )
    pending = outbox.claim_next_event(
        runtime.config,
        worker_id="prd140-worker-b",
        now=due_at + timedelta(seconds=31),
    )
    assert pending is not None and pending.outbox_id != first.outbox_id
    takeover = outbox.claim_next_event(
        runtime.config,
        worker_id="prd140-worker-c",
        now=due_at + timedelta(seconds=31),
    )
    assert takeover is not None and takeover.outbox_id == first.outbox_id
    assert takeover.generation > first.generation
    assert outbox.complete_event(runtime.config, first, now=due_at + timedelta(seconds=32)) is False
    assert (
        outbox.complete_event(runtime.config, takeover, now=due_at + timedelta(seconds=32)) is True
    )


def test_renewal_and_qbr_restart_idempotency_and_superseding_snapshot(runtime: Runtime) -> None:
    account_id = runtime.account_ids[0]
    qbr_at = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)
    with closing(connect_database(runtime.config.database_url)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        first_qbr = projection.generate_qbr(connection, account_id, now=qbr_at)
        assert first_qbr is not None
        connection.commit()

    with closing(connect_database(runtime.config.database_url)) as restarted:
        restarted.execute("BEGIN IMMEDIATE")
        assert projection.reconcile_renewals(restarted, now=qbr_at + timedelta(seconds=1)) == 0
        assert projection.generate_qbr(restarted, account_id, now=qbr_at) is None
        account = restarted.execute(
            "SELECT adoption_band,revision,updated_at FROM platform_customer_account_projections "
            "WHERE customer_account_id=?",
            (account_id,),
        ).fetchone()
        next_band = "low" if account["adoption_band"] != "low" else "high"
        restarted.execute(
            """UPDATE platform_customer_account_projections
                  SET adoption_band=?,source_revision_sha256=?,revision=revision+1,
                      updated_at=strftime('%Y-%m-%dT%H:%M:%fZ',updated_at,'+1 second')
                WHERE customer_account_id=? AND revision=?""",
            (
                next_band,
                hashlib.sha256(f"prd140-source:{next_band}".encode()).hexdigest(),
                account_id,
                int(account["revision"]),
            ),
        )
        assert projection.reconcile_renewals(
            restarted, now=qbr_at + timedelta(seconds=2)
        ) == 1
        second_qbr = projection.generate_qbr(
            restarted, account_id, now=qbr_at + timedelta(seconds=2)
        )
        assert second_qbr is not None and second_qbr != first_qbr
        restarted.commit()

    with closing(connect_database(runtime.config.database_url)) as verified:
        verified.execute("BEGIN IMMEDIATE")
        qbr_rows = verified.execute(
            """SELECT qbr_summary_id,supersedes_qbr_summary_id
                 FROM platform_qbr_summaries WHERE customer_account_id=?
                 ORDER BY generated_at,qbr_summary_id""",
            (account_id,),
        ).fetchall()
        assert len(qbr_rows) == 2
        assert qbr_rows[-1]["supersedes_qbr_summary_id"] == first_qbr
        assert (
            verified.execute(
                """SELECT count(*) FROM platform_customer_success_events
                    WHERE customer_account_id=? AND event_type='renewal.risk_changed'""",
                (account_id,),
            ).fetchone()[0]
            == 1
        )
        assert projection.reconcile_renewals(
            verified, now=qbr_at + timedelta(seconds=3)
        ) == 0
        assert projection.generate_qbr(
            verified, account_id, now=qbr_at + timedelta(seconds=3)
        ) is None
        verified.commit()


def test_scheduler_failure_rolls_back_job_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    started_at = runtime.now + timedelta(minutes=5)
    due_at = started_at - timedelta(seconds=1)
    future_at = started_at + timedelta(hours=1)
    account_id = runtime.account_ids[0]
    with closing(connect_database(runtime.config.database_url)) as connection:
        connection.execute("CREATE TABLE scheduler_test_probe(value TEXT NOT NULL)")
        connection.execute(
            """UPDATE platform_customer_success_scheduler_state
                  SET next_run_at=CASE
                        WHEN customer_account_id=? AND job_code='task_due' THEN ? ELSE ? END,
                      revision=revision+1,updated_at=?""",
            (
                account_id,
                scheduler._db_time(due_at),
                scheduler._db_time(future_at),
                scheduler._db_time(started_at),
            ),
        )
        before = connection.execute(
            """SELECT * FROM platform_customer_success_scheduler_state
                WHERE customer_account_id=? AND job_code='task_due'""",
            (account_id,),
        ).fetchone()
        assert before is not None
        connection.commit()

    original_process = scheduler._process

    def fail_after_write(connection, _row, _now, _should_stop):
        connection.execute("INSERT INTO scheduler_test_probe(value) VALUES('must-rollback')")
        raise ValueError("synthetic scheduler failure")

    monkeypatch.setattr(scheduler, "_process", fail_after_write)
    with pytest.raises(ApiError) as failed:
        scheduler.run_platform_customer_success_scheduler_once(
            runtime.config,
            worker_id="prd140-scheduler-worker",
            now=started_at,
        )
    assert failed.value.code == "platform.unavailable"

    with closing(connect_database(runtime.config.database_url)) as connection:
        failed_row = connection.execute(
            """SELECT * FROM platform_customer_success_scheduler_state
                WHERE customer_account_id=? AND job_code='task_due'""",
            (account_id,),
        ).fetchone()
        assert failed_row is not None
        assert connection.execute("SELECT count(*) FROM scheduler_test_probe").fetchone()[0] == 0
        assert failed_row["status"] == "pending"
        assert int(failed_row["consecutive_failure_count"]) == (
            int(before["consecutive_failure_count"]) + 1
        )
        assert int(failed_row["fencing_generation"]) == int(before["fencing_generation"]) + 1
        assert int(failed_row["revision"]) == int(before["revision"]) + 2
        assert failed_row["lease_owner_id"] is None
        assert failed_row["lease_token_sha256"] is None
        assert failed_row["lease_expires_at"] is None
        assert failed_row["last_error_code"] == ("platform.customer_success_scheduler_unavailable")
        assert failed_row["next_run_at"] == scheduler._db_time(started_at + timedelta(seconds=2))

    monkeypatch.setattr(scheduler, "_process", original_process)
    recovered_at = started_at + timedelta(seconds=2)
    assert scheduler.run_platform_customer_success_scheduler_once(
        runtime.config,
        worker_id="prd140-scheduler-worker",
        now=recovered_at,
    )
    with closing(connect_database(runtime.config.database_url)) as connection:
        recovered = connection.execute(
            """SELECT * FROM platform_customer_success_scheduler_state
                WHERE customer_account_id=? AND job_code='task_due'""",
            (account_id,),
        ).fetchone()
        assert recovered is not None
        assert recovered["status"] == "pending"
        assert int(recovered["consecutive_failure_count"]) == 0
        assert int(recovered["fencing_generation"]) == int(before["fencing_generation"]) + 2
        assert int(recovered["revision"]) == int(before["revision"]) + 4
        assert recovered["lease_owner_id"] is None
        assert recovered["lease_token_sha256"] is None
        assert recovered["lease_expires_at"] is None
        assert recovered["last_error_code"] is None
