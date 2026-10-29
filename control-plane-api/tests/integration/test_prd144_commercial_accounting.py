"""PRD-144 commercial accounting workflows and credential boundaries."""

from __future__ import annotations

import json
import math
import os
import secrets
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.commercial_accounting import (
    reconciliation,
    routes,
    scheduler,
    service,
)
from metahub_control.commercial_accounting.models import (
    BillingDisputeCreateRequest,
    BillingDisputeDecisionRequest,
    PlatformPlanCommandRequest,
    ProviderCostRateCommandRequest,
    ProviderReferenceRateCommandRequest,
)
from metahub_control.commercial_accounting.routes import router
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.saas_billing.routes import runtime_router as saas_runtime_router
from metahub_control.workspace_access import service as workspace_access_service
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
from pydantic import ValidationError

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


def _insert_operator(connection, marker: str, role_code: str) -> tuple[str, str]:
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
            f"prd144-{marker}@example.invalid",
            f"PRD144 {marker}",
            STAMP,
            STAMP,
        ),
    )
    connection.execute(
        """
        INSERT INTO platform_operators(
          operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
        VALUES(?,?,'active',1,NULL,?,?)
        """,
        (operator_id, account_id, STAMP, STAMP),
    )
    connection.execute(
        """
        INSERT INTO platform_operator_role_bindings(
          operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
        VALUES(?,?,'active',NULL,?,NULL)
        """,
        (operator_id, role_code, STAMP),
    )
    return account_id, operator_id


def _install_real_slo_sessions(runtime) -> tuple[str, str, str, str]:
    platform_token = "r" * 43
    platform_csrf = "t" * 43
    workspace_token = "v" * 43
    workspace_csrf = "u" * 43
    platform = runtime["contexts"]["billing-a"]
    workspace = runtime["workspace"]
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            "UPDATE global_accounts SET login_enabled=1 WHERE account_id=?",
            (platform.account_id,),
        )
        factor_id = "mfa_" + "9" * 32
        platform_global_session_id = "gss_" + "8" * 32
        connection.execute(
            """
            INSERT INTO account_mfa_factors(
              factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,
              revision,last_accepted_counter,verified_at,disabled_at,created_at,updated_at)
            VALUES(?,?,'totp',?,1,'active',1,0,?,NULL,?,?)
            """,
            (factor_id, platform.account_id, "x" * 80, STAMP, STAMP, STAMP),
        )
        connection.execute(
            """
            INSERT INTO global_sessions(
              session_id,account_id,token_hmac,csrf_hmac,status,credential_revision,
              session_revision,device_family,client_risk_hmac,expires_at,idle_expires_at,
              last_seen_at,revoked_at,revoked_reason,created_at,updated_at)
            VALUES(?,?,?,?,'active',1,1,'prd144-slo',NULL,?,?,?,NULL,NULL,?,?)
            """,
            (
                platform_global_session_id,
                platform.account_id,
                "8" * 64,
                "9" * 64,
                "2027-09-03T08:00:00Z",
                "2027-09-03T08:00:00Z",
                STAMP,
                STAMP,
                STAMP,
            ),
        )
        _roles, _permissions, fingerprint = platform_access_service._authority(
            connection, platform.operator_id
        )
        connection.execute(
            """
            INSERT INTO platform_sessions(
              platform_session_id,operator_id,account_id,global_session_id,factor_id,
              token_hmac,csrf_hmac,status,operator_revision,factor_revision,role_fingerprint,
              last_seen_at,absolute_expires_at,idle_expires_at,revoked_at,revoked_reason,
              created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'active',1,1,?,?,?,?,NULL,NULL,?,?)
            """,
            (
                "pts_" + "7" * 32,
                platform.operator_id,
                platform.account_id,
                platform_global_session_id,
                factor_id,
                platform_access_service._platform_hmac(
                    runtime["config"], "token", platform_token
                ),
                platform_access_service._platform_hmac(
                    runtime["config"], "csrf", platform_csrf
                ),
                fingerprint,
                STAMP,
                "2027-09-03T08:00:00Z",
                "2027-09-03T08:00:00Z",
                STAMP,
                STAMP,
            ),
        )
        workspace_global_session_id = "gss_" + "6" * 32
        connection.execute(
            """
            INSERT INTO global_sessions(
              session_id,account_id,token_hmac,csrf_hmac,status,credential_revision,
              session_revision,device_family,client_risk_hmac,expires_at,idle_expires_at,
              last_seen_at,revoked_at,revoked_reason,created_at,updated_at)
            VALUES(?,?,?,?,'active',1,1,'prd144-slo',NULL,?,?,?,NULL,NULL,?,?)
            """,
            (
                workspace_global_session_id,
                workspace.account_id,
                "6" * 64,
                "7" * 64,
                "2027-09-03T08:00:00Z",
                "2027-09-03T08:00:00Z",
                STAMP,
                STAMP,
                STAMP,
            ),
        )
        member = connection.execute(
            """
            SELECT membership.revision membership_revision,workspace.revision workspace_revision,
              account.session_revision
            FROM workspace_memberships membership
            JOIN workspaces workspace USING(workspace_id)
            JOIN global_accounts account USING(account_id)
            WHERE membership.workspace_membership_id=?
            """,
            (workspace.membership_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO workspace_sessions(
              workspace_session_id,global_session_id,account_id,workspace_id,tenant_id,
              membership_id,token_hmac,csrf_hmac,status,membership_revision,
              workspace_revision,account_session_revision,legacy_permission_revision,
              matrix_fingerprint,created_at,last_seen_at,absolute_expires_at,
              idle_expires_at,revoked_at,revoked_reason,updated_at)
            VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?,NULL,NULL,?)
            """,
            (
                "wss_" + "5" * 32,
                workspace_global_session_id,
                workspace.account_id,
                workspace.workspace_id,
                workspace.tenant_id,
                workspace.membership_id,
                workspace_access_service._session_hmac(
                    runtime["config"], "token", workspace_token
                ),
                workspace_access_service._session_hmac(
                    runtime["config"], "csrf", workspace_csrf
                ),
                member["membership_revision"],
                member["workspace_revision"],
                member["session_revision"],
                workspace.permission_revision,
                workspace.authorization_fingerprint,
                STAMP,
                STAMP,
                "2027-09-03T08:00:00Z",
                "2027-09-03T08:00:00Z",
                STAMP,
            ),
        )
        connection.commit()
    return platform_token, platform_csrf, workspace_token, workspace_csrf


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database_url = _url(tmp_path / "prd144.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0164"
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
    apply_seed_data(database_url)
    config = _config(database_url)
    with closing(connect_database(database_url)) as connection:
        billing_a_account, billing_a_id = _insert_operator(
            connection, "a", "platform_billing"
        )
        billing_b_account, billing_b_id = _insert_operator(
            connection, "b", "platform_billing"
        )
        owner_account, owner_id = _insert_operator(connection, "c", "platform_owner")
        owner = connection.execute(
            """
            SELECT membership.*,tenant.slug,tenant.name,user.email,user.display_name
            FROM workspace_memberships membership
            JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
            JOIN users user ON user.tenant_id=membership.tenant_id
              AND user.user_id=membership.legacy_user_id
            WHERE membership.role_code='owner' LIMIT 1
            """
        ).fetchone()
        assert owner is not None
        snapshot = workspace_actor_permissions_in_transaction(
            connection,
            account_id=str(owner["account_id"]),
            workspace_id=str(owner["workspace_id"]),
            tenant_id=str(owner["tenant_id"]),
        )
        provider_model = connection.execute(
            """
            SELECT provider.provider_id,model.model_id
            FROM providers provider
            JOIN models model
              ON model.tenant_id=provider.tenant_id
             AND model.provider_id=provider.provider_id
            WHERE provider.tenant_id=? AND provider.status='active' AND model.status='active'
            ORDER BY provider.provider_id,model.model_id LIMIT 1
            """,
            (owner["tenant_id"],),
        ).fetchone()
        assert provider_model is not None
        connection.commit()

    expires = NOW + timedelta(hours=1)
    billing_permissions = (
        "platform.channel.statement.read",
        "platform.commercial.dispute.decide",
        "platform.commercial.invoice.read",
        "platform.commercial.plan.manage",
        "platform.commercial.plan.read",
        "platform.commercial.reconciliation.manage",
        "platform.commercial.subscription.read",
    )
    contexts = {
        "billing-a": PlatformContext(
            "pts_" + "a" * 32,
            billing_a_id,
            billing_a_account,
            "gss_" + "a" * 32,
            "mfa_" + "a" * 32,
            1,
            1,
            ("platform_billing",),
            billing_permissions,
            expires,
        ),
        "billing-b": PlatformContext(
            "pts_" + "b" * 32,
            billing_b_id,
            billing_b_account,
            "gss_" + "b" * 32,
            "mfa_" + "b" * 32,
            1,
            1,
            ("platform_billing",),
            billing_permissions,
            expires,
        ),
        "owner": PlatformContext(
            "pts_" + "c" * 32,
            owner_id,
            owner_account,
            "gss_" + "c" * 32,
            "mfa_" + "c" * 32,
            1,
            1,
            ("platform_owner",),
            (
                "platform.channel.statement.read",
                "platform.commercial.governance.approve",
                "platform.commercial.invoice.read",
                "platform.commercial.plan.read",
                "platform.commercial.subscription.read",
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
    live_contexts = dict(contexts)
    platform_tokens = {
        "p" + "a" * 42: "billing-a",
        "p" + "b" * 42: "billing-b",
        "p" + "c" * 42: "owner",
    }
    workspace_token = "w" * 43

    def platform_actor(token: str) -> PlatformContext:
        name = platform_tokens.get(token, token)
        try:
            return live_contexts[name]
        except KeyError:
            raise ApiError("platform.session_required") from None

    def workspace_actor(token: str) -> WorkspaceSessionContext:
        if token not in {"workspace", workspace_token}:
            raise ApiError("workspace_session.required")
        return workspace

    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda _connection, _config, *, session_token, **_kwargs: platform_actor(
            session_token
        ),
    )
    monkeypatch.setattr(
        service,
        "authenticate_workspace_session_in_transaction",
        lambda _connection, _config, *, session_token, **_kwargs: workspace_actor(
            session_token
        ),
    )
    monkeypatch.setattr(
        service,
        "consume_global_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: "totp",
    )
    monkeypatch.setattr(
        service,
        "consume_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: "totp",
    )
    try:
        yield {
            "config": config,
            "contexts": contexts,
            "live_contexts": live_contexts,
            "workspace": workspace,
            "platform_tokens": platform_tokens,
            "workspace_token": workspace_token,
            "provider_id": str(provider_model["provider_id"]),
            "model_id": str(provider_model["model_id"]),
            "governance_operator_id": owner_id,
            "platform_actor": platform_actor,
            "workspace_actor": workspace_actor,
        }
    finally:
        _close_idle_database_connections()


def _platform_command(
    runtime, actor: str, revision: int, key: str, command: ProviderReferenceRateCommandRequest
):
    return service.command_platform_provider_reference_rate(
        runtime["config"],
        session_token=actor,
        csrf_token="c" * 43,
        step_up_grant="s" * 43,
        idempotency_key=key,
        expected_revision=revision,
        provider_id=runtime["provider_id"],
        command=command,
        request_id="prd144-request-0001",
        trace_id="prd144-trace-000001",
    )


def _workspace_command(
    runtime, revision: int, key: str, command: ProviderCostRateCommandRequest
):
    return service.command_workspace_provider_cost_rate(
        runtime["config"],
        session_token="workspace",
        csrf_token="c" * 43,
        step_up_grant="s" * 43,
        idempotency_key=key,
        expected_head_revision=revision,
        command=command,
        request_id="prd144-request-0002",
        trace_id="prd144-trace-000002",
    )


def _plan_command(
    runtime, actor: str, revision: int, key: str, command: PlatformPlanCommandRequest
):
    return service.command_platform_plan(
        runtime["config"],
        session_token=actor,
        csrf_token="c" * 43,
        step_up_grant="s" * 43,
        idempotency_key=key,
        expected_revision=revision,
        command=command,
        request_id="prd144-request-plan",
        trace_id="prd144-trace-plan",
    )


def _workspace_headers(token: str, csrf: str, *, key: str, revision: int) -> dict[str, str]:
    return {
        "Cookie": f"{WORKSPACE_SESSION_COOKIE}={token}; {WORKSPACE_CSRF_COOKIE}={csrf}",
        "Idempotency-Key": key,
        "If-Match": f'"{revision}"',
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": csrf,
        "X-Step-Up-Grant": "s" * 43,
    }


def _seed_statement_source(
    runtime, *, catalog_id: str, version_id: str
) -> str:
    workspace = runtime["workspace"]
    subscription_id = "sub_" + "f" * 32
    order_record_id = "sor_" + "0" * 32
    entitlement_id = "ent_" + "1" * 32
    billing_cycle_id = "pbc_" + "2" * 32
    statement_id = "bst_" + "3" * 32
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            """
            INSERT INTO workspace_subscriptions(
              subscription_id,workspace_id,tenant_id,plan_version_id,
              pending_plan_version_id,status,current_period_started_at,
              current_period_ends_at,pending_effective_at,state_revision,
              created_by_membership_id,created_at,updated_at)
            VALUES(?,?,?,?,NULL,'active',?,?,NULL,1,?,?,?)
            """,
            (
                subscription_id,
                workspace.workspace_id,
                workspace.tenant_id,
                version_id,
                STAMP,
                "2026-10-03T08:00:00Z",
                workspace.membership_id,
                STAMP,
                STAMP,
            ),
        )
        connection.execute(
            """
            INSERT INTO subscription_order_records(
              order_record_id,workspace_id,tenant_id,subscription_id,plan_version_id,
              order_kind,status,currency,amount_minor,idempotency_sha256,request_sha256,
              created_by_membership_id,created_at)
            VALUES(?,?,?,?,?,'create','not_required','USD',10000,?,?,?,?)
            """,
            (
                order_record_id,
                workspace.workspace_id,
                workspace.tenant_id,
                subscription_id,
                version_id,
                "b" * 64,
                "c" * 64,
                workspace.membership_id,
                STAMP,
            ),
        )
        connection.execute(
            """
            INSERT INTO entitlement_snapshots(
              entitlement_snapshot_id,workspace_id,tenant_id,subscription_id,
              plan_version_id,source_order_record_id,snapshot_version,entitlements_json,
              entitlements_sha256,effective_from,effective_until,created_at)
            VALUES(?,?,?,?,?,?,1,'{}',?,?,NULL,?)
            """,
            (
                entitlement_id,
                workspace.workspace_id,
                workspace.tenant_id,
                subscription_id,
                version_id,
                order_record_id,
                "a" * 64,
                STAMP,
                STAMP,
            ),
        )
        connection.execute(
            """
            INSERT INTO platform_billing_cycles(
              billing_cycle_id,workspace_id,tenant_id,subscription_id,plan_version_id,
              entitlement_snapshot_id,status,period_started_at,period_ends_at,currency,
              base_amount_minor,source_watermark,summary_set_sha256,revision,
              created_at,updated_at)
            VALUES(?,?,?,?,?,?,'closed',?,?,'USD',10000,1,?,1,?,?)
            """,
            (
                billing_cycle_id,
                workspace.workspace_id,
                workspace.tenant_id,
                subscription_id,
                version_id,
                entitlement_id,
                STAMP,
                "2026-10-03T08:00:00Z",
                "d" * 64,
                STAMP,
                STAMP,
            ),
        )
        connection.execute(
            """
            INSERT INTO billing_statements(
              billing_statement_id,workspace_id,tenant_id,billing_cycle_id,
              statement_kind,predecessor_statement_id,immutable_version,
              meter_summary_set_sha256,source_watermark,currency,subtotal_minor,
              tax_rate_basis_points,tax_minor,total_minor,status,statement_sha256,issued_at)
            VALUES(?,?,?,?,'standard',NULL,1,?,1,'USD',10000,0,0,10000,
              'published',?,?)
            """,
            (
                statement_id,
                workspace.workspace_id,
                workspace.tenant_id,
                billing_cycle_id,
                "d" * 64,
                "e" * 64,
                STAMP,
            ),
        )
        connection.commit()
    return statement_id


def test_command_models_reject_explicit_null_required_fields() -> None:
    with pytest.raises(ValidationError):
        BillingDisputeCreateRequest.model_validate(
            {
                "source_kind": "statement_line",
                "source_id": "bst_123",
                "source_line_id": None,
                "reason_code": "amount_mismatch",
            }
        )
    with pytest.raises(ValidationError):
        ProviderCostRateCommandRequest.model_validate(
            {
                "action": "disable",
                "workspace_provider_cost_rate_id": None,
            }
        )
    with pytest.raises(ValidationError):
        ProviderReferenceRateCommandRequest.model_validate(
            {
                "action": "release",
                "provider_reference_rate_release_id": None,
                "governance_approval_id": None,
            }
        )


def test_runtime_router_has_single_platform_plan_owner() -> None:
    legacy_runtime_methods = {
        method
        for route in saas_runtime_router.routes
        if getattr(route, "path", None) == "/platform/plans"
        for method in getattr(route, "methods", set())
    }
    commercial_methods = {
        method
        for route in router.routes
        if getattr(route, "path", None) == "/platform/plans"
        for method in getattr(route, "methods", set())
    }
    assert legacy_runtime_methods == set()
    assert commercial_methods == {"GET", "POST"}


@pytest.mark.anyio
async def test_reference_rate_workspace_adoption_idempotency_and_api_boundaries(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    assert {
        "workspace.commercial.dispute.submit",
        "workspace.commercial.invoice.read",
        "workspace.commercial.subscription.read",
        "workspace.internal_finops.manage",
        "workspace.internal_finops.read",
        "workspace.provider_cost.manage",
        "workspace.provider_cost.read",
    }.issubset(runtime["workspace"].permission_codes)

    candidate_command = ProviderReferenceRateCommandRequest(
        action="create_candidate",
        model_id=runtime["model_id"],
        currency="USD",
        input_rate_micros_per_1k=1250,
        output_rate_micros_per_1k=3750,
        cache_rate_micros_per_1k=625,
        effective_from=datetime(2026, 10, 1, tzinfo=UTC),
        effective_until=datetime(2027, 1, 1, tzinfo=UTC),
        source_version="official-2026-q4",
    )
    candidate = _platform_command(
        runtime,
        "billing-a",
        0,
        "prd144-reference-candidate-0001",
        candidate_command,
    )
    release_id = candidate.rate.provider_reference_rate_release_id
    approval_id = "pcga_" + "1" * 32
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            """
            INSERT INTO platform_commercial_governance_approvals(
              commercial_governance_approval_id,object_kind,object_id,candidate_sha256,
              candidate_revision,approved_by_operator_id,approved_at,expires_at,consumed_at)
            VALUES(?,'provider_reference_rate',?,?,1,?,?,?,NULL)
            """,
            (
                approval_id,
                release_id,
                candidate.rate.content_sha256,
                runtime["governance_operator_id"],
                STAMP,
                "2027-09-03T08:00:00Z",
            ),
        )
        connection.commit()
    released = _platform_command(
        runtime,
        "billing-b",
        1,
        "prd144-reference-release-00001",
        ProviderReferenceRateCommandRequest(
            action="release",
            provider_reference_rate_release_id=release_id,
            governance_approval_id=approval_id,
        ),
    )
    assert released.rate.status == "released"
    assert released.rate.is_head and released.rate.release_generation == 1

    adopt_command = ProviderCostRateCommandRequest(
        action="adopt_reference",
        provider_id=runtime["provider_id"],
        model_id=runtime["model_id"],
        currency="USD",
        provider_reference_rate_release_id=release_id,
    )
    adopted = _workspace_command(
        runtime, 0, "prd144-workspace-adopt-00001", adopt_command
    )
    assert adopted.rate.source_type == "platform_reference"
    assert adopted.rate.head_revision == 1
    replay = _workspace_command(
        runtime, 0, "prd144-workspace-adopt-00001", adopt_command
    )
    assert replay.idempotent_replay is True
    with pytest.raises(ApiError) as changed_replay:
        _workspace_command(
            runtime,
            0,
            "prd144-workspace-adopt-00001",
            ProviderCostRateCommandRequest(
                action="adopt_reference",
                provider_id=runtime["provider_id"],
                model_id=runtime["model_id"],
                currency="CNY",
                provider_reference_rate_release_id=release_id,
            ),
        )
    assert changed_replay.value.code == "billing_dispute.idempotency_conflict"
    with pytest.raises(ApiError) as overlapping_rate:
        _workspace_command(
            runtime,
            1,
            "prd144-workspace-overlap-0001",
            ProviderCostRateCommandRequest(
                action="create_version",
                provider_id=runtime["provider_id"],
                model_id=runtime["model_id"],
                currency="USD",
                input_rate_micros_per_1k=1500,
                output_rate_micros_per_1k=4000,
                cache_rate_micros_per_1k=750,
                effective_from=datetime(2026, 11, 1, tzinfo=UTC),
                effective_until=datetime(2027, 2, 1, tzinfo=UTC),
            ),
        )
    assert overlapping_rate.value.code == "model_price.effective_conflict"
    versioned = _workspace_command(
        runtime,
        1,
        "prd144-workspace-version-0001",
        ProviderCostRateCommandRequest(
            action="create_version",
            provider_id=runtime["provider_id"],
            model_id=runtime["model_id"],
            currency="USD",
            input_rate_micros_per_1k=1500,
            output_rate_micros_per_1k=4000,
            cache_rate_micros_per_1k=750,
            effective_from=datetime(2027, 1, 1, tzinfo=UTC),
            effective_until=datetime(2027, 4, 1, tzinfo=UTC),
        ),
    )
    assert versioned.rate.head_revision == 2
    with pytest.raises(ApiError) as stale_revision:
        _workspace_command(
            runtime,
            0,
            "prd144-workspace-disable-stale",
            ProviderCostRateCommandRequest(
                action="disable",
                workspace_provider_cost_rate_id=adopted.rate.workspace_provider_cost_rate_id,
            ),
        )
    assert stale_revision.value.code == "model_price.version_conflict"
    disabled = _workspace_command(
        runtime,
        2,
        "prd144-workspace-disable-0001",
        ProviderCostRateCommandRequest(
            action="disable",
            workspace_provider_cost_rate_id=adopted.rate.workspace_provider_cost_rate_id,
        ),
    )
    assert disabled.rate.status == "disabled"
    assert disabled.rate.head_revision == 3

    listed = service.list_workspace_provider_cost_rates(
        runtime["config"],
        session_token="workspace",
        query={
            "provider_id": runtime["provider_id"],
            "currency": "USD",
            "limit": "10",
        },
        request_id="prd144-request-0003",
        trace_id="prd144-trace-000003",
    )
    assert [rate.workspace_provider_cost_rate_id for rate in listed.rates] == [
        adopted.rate.workspace_provider_cost_rate_id
    ]

    plan_candidate = _plan_command(
        runtime,
        "billing-a",
        0,
        "prd144-plan-candidate-000001",
        PlatformPlanCommandRequest(
            action="create_candidate",
            plan_code="prd144-team",
            display_name="PRD144 Team",
            display_order=10,
            billing_period="monthly",
            currency="USD",
            unit_price_minor=10000,
            trial_days=0,
            entitlements={
                "gateway_requests_per_month": 10000,
                "workspace_seats": 5,
                "model_aliases": 10,
                "retention_days": 30,
                "priority_support": True,
            },
        ),
    )
    plan_version_id = plan_candidate.plan.plan_version_id
    reviewed_plan = _plan_command(
        runtime,
        "billing-a",
        1,
        "prd144-plan-review-0000001",
        PlatformPlanCommandRequest(
            action="submit_review", plan_version_id=plan_version_id
        ),
    )
    assert reviewed_plan.plan.status == "review_required"
    plan_approval_id = "pcga_" + "2" * 32
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            """
            INSERT INTO platform_commercial_governance_approvals(
              commercial_governance_approval_id,object_kind,object_id,candidate_sha256,
              candidate_revision,approved_by_operator_id,approved_at,expires_at,consumed_at)
            VALUES(?,'plan_release',?,?,2,?,?,?,NULL)
            """,
            (
                plan_approval_id,
                reviewed_plan.plan.platform_plan_catalog_release_id,
                reviewed_plan.plan.contract_sha256,
                runtime["governance_operator_id"],
                STAMP,
                "2027-09-03T08:00:00Z",
            ),
        )
        connection.commit()
    released_plan = _plan_command(
        runtime,
        "billing-b",
        2,
        "prd144-plan-release-000001",
        PlatformPlanCommandRequest(
            action="release",
            plan_version_id=plan_version_id,
            governance_approval_id=plan_approval_id,
        ),
    )
    assert released_plan.plan.status == "released"
    assert released_plan.plan.is_head
    assert released_plan.plan.release_generation == 1
    statement_id = _seed_statement_source(
        runtime,
        catalog_id=released_plan.plan.plan_catalog_id,
        version_id=plan_version_id,
    )
    assert service.refresh_subscription_projections(runtime["config"], limit=10) == 1
    workspace_subscription = service.get_workspace_subscription(
        runtime["config"],
        session_token="workspace",
        query={},
        request_id="prd144-request-0004",
        trace_id="prd144-trace-000004",
    )
    assert workspace_subscription.subscription is not None
    assert workspace_subscription.subscription.currency == "USD"
    assert workspace_subscription.subscription.projection_state == "partial"

    submitted = service.create_workspace_billing_dispute(
        runtime["config"],
        session_token="workspace",
        csrf_token="c" * 43,
        step_up_grant="s" * 43,
        idempotency_key="prd144-dispute-submit-00001",
        expected_source_revision=1,
        command=BillingDisputeCreateRequest(
            source_kind="statement",
            source_id=statement_id,
            reason_code="amount_mismatch",
        ),
        request_id="prd144-request-0005",
        trace_id="prd144-trace-000005",
    )
    dispute_id = submitted.dispute.billing_dispute_case_id
    reviewed = service.decide_platform_billing_dispute(
        runtime["config"],
        session_token="billing-a",
        csrf_token="c" * 43,
        step_up_grant="s" * 43,
        idempotency_key="prd144-dispute-review-00001",
        expected_case_revision=1,
        dispute_id=dispute_id,
        command=BillingDisputeDecisionRequest(
            action="start_review", expected_source_revision=1
        ),
        request_id="prd144-request-0006",
        trace_id="prd144-trace-000006",
    )
    assert reviewed.dispute.status == "under_review"
    approved_command = BillingDisputeDecisionRequest(
        action="approve_credit",
        decision_code="service_credit",
        expected_source_revision=1,
    )
    approved = service.decide_platform_billing_dispute(
        runtime["config"],
        session_token="billing-b",
        csrf_token="c" * 43,
        step_up_grant="s" * 43,
        idempotency_key="prd144-dispute-approve-0001",
        expected_case_revision=2,
        dispute_id=dispute_id,
        command=approved_command,
        request_id="prd144-request-0007",
        trace_id="prd144-trace-000007",
    )
    assert approved.dispute.status == "approved"
    assert approved.dispute.credited_amount_minor == 10000
    assert approved.adjustment_id is not None
    replayed_approval = service.decide_platform_billing_dispute(
        runtime["config"],
        session_token="billing-b",
        csrf_token="c" * 43,
        step_up_grant="s" * 43,
        idempotency_key="prd144-dispute-approve-0001",
        expected_case_revision=2,
        dispute_id=dispute_id,
        command=approved_command,
        request_id="prd144-request-0008",
        trace_id="prd144-trace-000008",
    )
    assert replayed_approval.idempotent_replay is True
    assert service.refresh_subscription_projections(runtime["config"], limit=10) == 1
    platform_subscriptions = service.list_platform_subscriptions(
        runtime["config"],
        session_token="billing-a",
        query={"dispute_id": dispute_id},
        request_id="prd144-request-0009",
        trace_id="prd144-trace-000009",
    )
    assert len(platform_subscriptions.subscriptions) == 1
    assert platform_subscriptions.subscriptions[0].disputes[0].status == "approved"
    with closing(connect_database(runtime["config"].database_url)) as connection:
        assert connection.execute(
            "SELECT amount_minor FROM platform_commercial_adjustments "
            "WHERE billing_dispute_case_id=?",
            (dispute_id,),
        ).fetchone()[0] == 10000
        event_counts = dict(
            connection.execute(
                "SELECT event_type,count(*) FROM commercial_accounting_events GROUP BY event_type"
            ).fetchall()
        )
    assert event_counts == {
        "billing_dispute.decided": 1,
        "billing_dispute.submitted": 1,
        "plan.released": 1,
        "provider_cost_rate.changed": 3,
        "subscription.changed": 1,
    }

    monkeypatch.setattr(
        routes,
        "authenticate_platform_session_context",
        lambda _config, session_token, **_kwargs: runtime["platform_actor"](
            session_token
        ),
    )
    monkeypatch.setattr(
        routes,
        "authenticate_workspace_session_context",
        lambda _config, session_token, **_kwargs: runtime["workspace_actor"](
            session_token
        ),
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-144 Commercial Accounting Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = runtime["config"]
    app.include_router(router)
    platform_token = "p" + "a" * 42
    workspace_token = runtime["workspace_token"]
    csrf = "c" * 43
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        plans = await client.get(
            "/platform/plans",
            headers={"Cookie": f"{PLATFORM_SESSION_COOKIE}={platform_token}"},
        )
        assert plans.status_code == 200, plans.text
        assert plans.headers["cache-control"] == "no-store, private"
        assert [
            rate["provider_reference_rate_release_id"]
            for rate in plans.json()["reference_rates"]
        ] == [release_id]

        rates = await client.get(
            "/workspace/provider-cost-rates",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={workspace_token}"},
        )
        assert rates.status_code == 200, rates.text
        assert adopted.rate.workspace_provider_cost_rate_id in {
            rate["workspace_provider_cost_rate_id"] for rate in rates.json()["rates"]
        }

        subscription = await client.get(
            "/workspace/subscription",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={workspace_token}"},
        )
        assert subscription.status_code == 200, subscription.text
        assert subscription.json()["subscription"]["disputes"][0]["status"] == "approved"

        subscriptions = await client.get(
            f"/platform/subscriptions?dispute_id={dispute_id}",
            headers={"Cookie": f"{PLATFORM_SESSION_COOKIE}={platform_token}"},
        )
        assert subscriptions.status_code == 200, subscriptions.text
        assert len(subscriptions.json()["subscriptions"]) == 1

        mixed_platform = await client.get(
            "/platform/plans",
            headers={
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={platform_token}; "
                    f"{WORKSPACE_SESSION_COOKIE}={workspace_token}"
                )
            },
        )
        assert mixed_platform.status_code == 400
        assert mixed_platform.json()["code"] == "platform.credential_conflict"

        mixed_workspace = await client.get(
            "/workspace/provider-cost-rates",
            headers={
                "Cookie": (
                    f"{WORKSPACE_SESSION_COOKIE}={workspace_token}; "
                    f"{PLATFORM_SESSION_COOKIE}={platform_token}"
                )
            },
        )
        assert mixed_workspace.status_code == 400
        assert mixed_workspace.json()["code"] == "workspace_session.credential_conflict"

        null_command = await client.patch(
            "/workspace/provider-cost-rates",
            headers=_workspace_headers(
                workspace_token,
                csrf,
                key="prd144-api-null-command-0001",
                revision=1,
            ),
            json={"action": "disable", "workspace_provider_cost_rate_id": None},
        )
        assert null_command.status_code == 422
        assert null_command.json()["code"] == "model_price.query_invalid"


def test_scheduler_release_failure_does_not_mask_worker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = scheduler._SchedulerLease("quota_trial_outbox", 1, "lease-token")
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler,
        "_claim",
        lambda _config, worker_kind: lease if worker_kind == lease.worker_kind else None,
    )
    monkeypatch.setattr(
        scheduler,
        "_run_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ApiError("worker.failed")),
    )
    monkeypatch.setattr(
        scheduler,
        "_release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ApiError("release.failed")),
    )
    monkeypatch.setattr(
        scheduler, "_warning", lambda worker_kind, code: warnings.append((worker_kind, code))
    )

    assert scheduler.run_commercial_accounting_once(object()) is False
    assert (lease.worker_kind, "worker.failed") in warnings
    assert (lease.worker_kind, "platform.unavailable") in warnings


def test_billing_domain_reconciliation_freezes_and_replays(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        reconciliation,
        "authenticate_platform_session_in_transaction",
        lambda _connection, _config, *, session_token, **_kwargs: runtime[
            "platform_actor"
        ](session_token),
    )
    run_id = "rec_" + "4" * 32
    workspace = runtime["workspace"]
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            """
            INSERT INTO reconciliation_runs(
              reconciliation_run_id,scope,order_watermark,payment_watermark,meter_watermark,
              statement_watermark,invoice_watermark,order_source_sha256,payment_source_sha256,
              meter_source_sha256,statement_source_sha256,invoice_source_sha256,status,
              lease_owner,lease_token,lease_expires_at,generation,safe_error_code,
              created_by_operator_id,created_at,completed_at,updated_at)
            VALUES(?,'all',5,6,7,8,9,?,?,?,?,?,'completed',NULL,NULL,NULL,2,NULL,
              ?,?,?,?)
            """,
            (
                run_id,
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "4" * 64,
                "5" * 64,
                runtime["contexts"]["billing-a"].operator_id,
                STAMP,
                STAMP,
                STAMP,
            ),
        )
        for index, difference_type in enumerate(
            (
                "order_payment_amount_mismatch",
                "paid_order_missing_capture",
                "closed_cycle_missing_statement",
            ),
            start=1,
        ):
            connection.execute(
                """
                INSERT INTO reconciliation_differences(
                  reconciliation_difference_id,reconciliation_run_id,workspace_id,tenant_id,
                  difference_type,left_source_type,left_source_id,right_source_type,
                  right_source_id,currency,amount_difference_minor,status,
                  adjustment_reference,state_revision,detected_at,updated_at)
                VALUES(?,?,?,?,?,'order',?,NULL,NULL,'USD',?,'open',NULL,1,?,?)
                """,
                (
                    "rcd_" + f"{index:032x}",
                    run_id,
                    workspace.workspace_id,
                    workspace.tenant_id,
                    difference_type,
                    "order_" + f"{index:08x}",
                    index * 100,
                    STAMP,
                    STAMP,
                ),
            )
        event_count_before = connection.execute(
            "SELECT count(*) FROM commercial_accounting_events"
        ).fetchone()[0]
        connection.commit()

    result = reconciliation.run_billing_domain_reconciliation(
        runtime["config"],
        platform_session_token="billing-a",
        source_reconciliation_run_id=run_id,
        request_id="prd144-reconciliation-request",
        trace_id="prd144-reconciliation-trace",
    )
    assert result.status == "completed"
    assert result.difference_count == 3
    assert len(result.source_set_sha256) == 64
    assert len(result.result_sha256) == 64

    replay = reconciliation.run_billing_domain_reconciliation(
        runtime["config"],
        platform_session_token="billing-a",
        source_reconciliation_run_id=run_id,
        request_id="prd144-reconciliation-replay",
        trace_id="prd144-reconciliation-replay-trace",
    )
    assert replay == result

    with closing(connect_database(runtime["config"].database_url)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM billing_domain_reconciliations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM billing_domain_reconciliation_differences"
        ).fetchone()[0] == 3
        difference = connection.execute(
            """
            SELECT workspace_id,tenant_id,currency,amount_difference_minor,left_source_ref
            FROM billing_domain_reconciliation_differences
            ORDER BY amount_difference_minor LIMIT 1
            """
        ).fetchone()
        assert tuple(difference) == (
            workspace.workspace_id,
            workspace.tenant_id,
            "USD",
            100,
            "order:order_00000001",
        )
        source = connection.execute(
            """
            SELECT row_count,amount_minor_or_micros,owner_coverage_count
            FROM billing_domain_reconciliation_source_snapshots
            WHERE source_kind='invoice_reconciliation_difference'
            """
        ).fetchone()
        assert tuple(source) == (3, 600, 3)
        assert connection.execute(
            "SELECT count(*) FROM commercial_accounting_events"
        ).fetchone()[0] == event_count_before
        assert connection.execute(
            "SELECT count(*) FROM platform_audit_logs "
            "WHERE action_code='commercial_accounting.reconciliation.completed'"
        ).fetchone()[0] == 1

    with pytest.raises(ApiError) as denied:
        reconciliation.run_billing_domain_reconciliation(
            runtime["config"],
            platform_session_token="owner",
            source_reconciliation_run_id=run_id,
            request_id="prd144-reconciliation-denied",
            trace_id="prd144-reconciliation-denied-trace",
        )
    assert denied.value.code == "platform.permission_denied"


def test_prd144_commercial_accounting_slos(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        platform_token,
        platform_csrf,
        workspace_token,
        workspace_csrf,
    ) = _install_real_slo_sessions(runtime)
    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        platform_access_service.authenticate_platform_session_in_transaction,
    )
    monkeypatch.setattr(
        service,
        "authenticate_workspace_session_in_transaction",
        workspace_access_service.authenticate_workspace_session_in_transaction,
    )
    plan = service.command_platform_plan(
        runtime["config"],
        session_token=platform_token,
        csrf_token=platform_csrf,
        step_up_grant="s" * 43,
        idempotency_key="prd144-slo-plan-create",
        expected_revision=0,
        command=PlatformPlanCommandRequest(
            action="create_candidate",
            plan_code="prd144-slo",
            display_name="PRD144 SLO",
            display_order=1,
            billing_period="monthly",
            currency="USD",
            unit_price_minor=10_000,
            trial_days=0,
            entitlements={
                "gateway_requests_per_month": 100_000,
                "workspace_seats": 10,
                "model_aliases": 20,
                "retention_days": 30,
                "priority_support": False,
            },
        ),
        request_id="prd144-slo-plan-request",
        trace_id="prd144-slo-plan-trace",
    ).plan
    statement_id = _seed_statement_source(
        runtime,
        catalog_id=plan.plan_catalog_id,
        version_id=plan.plan_version_id,
    )
    assert service.refresh_subscription_projections(runtime["config"], limit=10) == 1

    def platform_query() -> None:
        with database_request_scope(reuse_connections=True):
            response = service.list_platform_subscriptions(
                runtime["config"],
                session_token=platform_token,
                query={"limit": "100"},
                request_id="prd144-slo-platform-query",
                trace_id="prd144-slo-platform-query-trace",
            )
        assert len(response.subscriptions) == 1

    def workspace_query() -> None:
        with database_request_scope(reuse_connections=True):
            response = service.get_workspace_subscription(
                runtime["config"],
                session_token=workspace_token,
                query={},
                request_id="prd144-slo-workspace-query",
                trace_id="prd144-slo-workspace-query-trace",
            )
        assert response.subscription is not None

    platform_query()
    workspace_query()
    platform_samples: list[float] = []
    workspace_samples: list[float] = []
    for _sample in range(30):
        started = time.perf_counter()
        platform_query()
        platform_samples.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        workspace_query()
        workspace_samples.append((time.perf_counter() - started) * 1000)

    propagation_started = time.perf_counter()
    submitted = service.create_workspace_billing_dispute(
        runtime["config"],
        session_token=workspace_token,
        csrf_token=workspace_csrf,
        step_up_grant="s" * 43,
        idempotency_key="prd144-slo-dispute-submit",
        expected_source_revision=1,
        command=BillingDisputeCreateRequest(
            source_kind="statement",
            source_id=statement_id,
            reason_code="amount_mismatch",
        ),
        request_id="prd144-slo-dispute-submit-request",
        trace_id="prd144-slo-dispute-submit-trace",
    )
    assert service.refresh_subscription_projections(runtime["config"], limit=10) == 1
    dispatched = 0
    while scheduler.run_commercial_accounting_once(runtime["config"]):
        dispatched += 1
        if dispatched > 20:
            pytest.fail("commercial accounting supervisor did not quiesce")
    with database_request_scope(reuse_connections=True):
        platform_projection = service.list_platform_subscriptions(
            runtime["config"],
            session_token=platform_token,
            query={"dispute_id": submitted.dispute.billing_dispute_case_id},
            request_id="prd144-slo-platform-projection",
            trace_id="prd144-slo-platform-projection-trace",
        )
    with database_request_scope(reuse_connections=True):
        workspace_projection = service.get_workspace_subscription(
            runtime["config"],
            session_token=workspace_token,
            query={},
            request_id="prd144-slo-workspace-projection",
            trace_id="prd144-slo-workspace-projection-trace",
        )
    propagation_seconds = time.perf_counter() - propagation_started
    assert platform_projection.subscriptions[0].disputes[0].status == "submitted"
    assert workspace_projection.subscription is not None
    assert workspace_projection.subscription.disputes[0].status == "submitted"
    with closing(connect_database(runtime["config"].database_url)) as connection:
        submitted_outbox = connection.execute(
            """
            SELECT outbox.status
            FROM commercial_accounting_event_outbox outbox
            JOIN commercial_accounting_events event
              USING(commercial_accounting_event_id)
            WHERE event.event_type='billing_dispute.submitted'
              AND event.object_id=?
            """,
            (submitted.dispute.billing_dispute_case_id,),
        ).fetchone()
        assert submitted_outbox["status"] == "succeeded"

        run_id = "rec_" + "a" * 32
        connection.execute(
            """
            INSERT INTO reconciliation_runs(
              reconciliation_run_id,scope,order_watermark,payment_watermark,meter_watermark,
              statement_watermark,invoice_watermark,order_source_sha256,payment_source_sha256,
              meter_source_sha256,statement_source_sha256,invoice_source_sha256,status,
              lease_owner,lease_token,lease_expires_at,generation,safe_error_code,
              created_by_operator_id,created_at,completed_at,updated_at)
            VALUES(?,'all',5,6,7,8,9,?,?,?,?,?,'completed',NULL,NULL,NULL,2,NULL,
              ?,?,?,?)
            """,
            (
                run_id,
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "4" * 64,
                "5" * 64,
                runtime["contexts"]["billing-a"].operator_id,
                STAMP,
                STAMP,
                STAMP,
            ),
        )
        connection.execute(
            """
            WITH digits(value) AS (
              VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)
            ), numbers(value) AS (
              SELECT d0.value+10*d1.value+100*d2.value+1000*d3.value+10000*d4.value
              FROM digits d0 CROSS JOIN digits d1 CROSS JOIN digits d2
              CROSS JOIN digits d3 CROSS JOIN digits d4
            )
            INSERT INTO reconciliation_differences(
              reconciliation_difference_id,reconciliation_run_id,workspace_id,tenant_id,
              difference_type,left_source_type,left_source_id,right_source_type,
              right_source_id,currency,amount_difference_minor,status,
              adjustment_reference,state_revision,detected_at,updated_at)
            SELECT 'rcd_'||printf('%032x',value+1),?, ?,?,
              CASE value%5
                WHEN 0 THEN 'order_payment_amount_mismatch'
                WHEN 1 THEN 'paid_order_missing_capture'
                WHEN 2 THEN 'closed_cycle_missing_statement'
                WHEN 3 THEN 'meter_summary_watermark_mismatch'
                ELSE 'issued_invoice_missing_artifact'
              END,
              'order','order_'||printf('%08x',value),NULL,NULL,'USD',
              value%1000+1,'open',NULL,1,?,?
            FROM numbers ORDER BY value
            """,
            (
                run_id,
                runtime["workspace"].workspace_id,
                runtime["workspace"].tenant_id,
                STAMP,
                STAMP,
            ),
        )
        connection.commit()

    reconciliation_started = time.perf_counter()
    reconciled = reconciliation.run_billing_domain_reconciliation(
        runtime["config"],
        platform_session_token=platform_token,
        source_reconciliation_run_id=run_id,
        request_id="prd144-slo-reconciliation",
        trace_id="prd144-slo-reconciliation-trace",
    )
    reconciliation_seconds = time.perf_counter() - reconciliation_started
    with closing(connect_database(runtime["config"].database_url)) as connection:
        difference_snapshot = connection.execute(
            """
            SELECT row_count,amount_minor_or_micros,owner_coverage_count
            FROM billing_domain_reconciliation_source_snapshots
            WHERE billing_domain_reconciliation_id=?
              AND source_kind='invoice_reconciliation_difference'
              AND currency='USD'
            """,
            (reconciled.billing_domain_reconciliation_id,),
        ).fetchone()
    assert tuple(difference_snapshot) == (100_000, 50_050_000, 100_000)

    def nearest_rank_p95(values: list[float]) -> float:
        return sorted(values)[math.ceil(len(values) * 0.95) - 1]

    metrics = {
        "status": "passed",
        "prd_id": "PRD-144",
        "query_sample_count": 30,
        "platform_subscription_query_p95_ms": round(
            nearest_rank_p95(platform_samples), 3
        ),
        "workspace_subscription_query_p95_ms": round(
            nearest_rank_p95(workspace_samples), 3
        ),
        "query_threshold_ms": 500,
        "dispute_projection_seconds": round(propagation_seconds, 6),
        "dispute_projection_threshold_seconds": 5,
        "dispatched_event_count": dispatched,
        "reconciliation_difference_count": reconciled.difference_count,
        "reconciliation_seconds": round(reconciliation_seconds, 6),
        "reconciliation_threshold_seconds": 600,
        "reconciliation_source_set_sha256": reconciled.source_set_sha256,
        "reconciliation_result_sha256": reconciled.result_sha256,
    }
    assert metrics["platform_subscription_query_p95_ms"] < 500
    assert metrics["workspace_subscription_query_p95_ms"] < 500
    assert propagation_seconds < 5
    assert reconciled.difference_count == 100_000
    assert reconciliation_seconds < 600
    print("PRD144_SLO " + json.dumps(metrics, sort_keys=True))
    evidence_path = os.environ.get("METAHUB_PRD144_SLO_EVIDENCE")
    if evidence_path:
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
