"""Native PostgreSQL organization-session budget and internal ledger acceptance."""

import secrets
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import test_postgresql_native_migrations as native_migrations
from metahub_control.audit import AuditRequestMeta
from metahub_control.budgets import service as budget_service
from metahub_control.budgets.models import CreateBudgetRuleRequest, UpdateBudgetModeRequest
from metahub_control.budgets.service import (
    create_budget_rule,
    get_budget_status,
    list_budget_rules,
    update_budget_mode,
)
from metahub_control.finops.service import create_finops_adjustment, list_finops_ledger
from metahub_control.identity.self_service import register_local_account
from metahub_control.workspace_access.service import (
    authenticate_workspace_session_as_auth_context,
    switch_workspace,
)
from metahub_control.workspaces.service import create_workspace
from metahub_gateway.budgets.models import BudgetDecision, BudgetRule
from metahub_gateway.budgets.service import _insert_alert_event
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.budget_blocks import ensure_budget_block
from metahub_shared.runtime.budget_reservations import ensure_budget_reservation
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError

pytestmark = native_migrations.pytestmark
postgresql_database = native_migrations.postgresql_database


@pytest.fixture
def native_business_database(postgresql_database, monkeypatch):
    # A single acceptance flow needs one migrated database, not a template and a clone.
    assert "0155" in apply_migrations(postgresql_database).applied
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(48))
    apply_seed_data(postgresql_database)
    return postgresql_database


def test_native_workspace_budget_and_finops_transaction_contracts(
    native_business_database, monkeypatch
):
    config = load_app_config("control-plane-api").model_copy(
        update={
            "database_url": native_business_database,
            "environment": "dev",
            "identity_session_secret": "finance-native-global-session-secret-2026",
            "identity_challenge_secret": "finance-native-challenge-secret-2026",
            "workspace_session_secret": "finance-native-workspace-session-secret-2026",
            "identity_challenge_delivery_mode": "disabled",
        }
    )
    chain = {"request_id": "finance-native", "trace_id": "finance-native"}
    issued = register_local_account(
        config,
        email="native-finance-owner@example.test",
        display_name="Owner",
        password="NativeFinanceOwner!2026",
        consent_version="privacy-local-2026-09-09",
        invitation_token=None,
        device_token="native-finance-device",
        client_host="127.0.0.1",
        user_agent="native-test",
        **chain,
    )
    workspace = create_workspace(
        config,
        session_token=issued.session_token,
        csrf_token=issued.csrf_token,
        idempotency_key="finance-native-organization",
        kind="organization",
        name="Native Finance",
        slug="native-finance",
        region="cn-north-1",
        service_mode="internal_only",
        **chain,
    ).workspace
    _, token, csrf = switch_workspace(
        config,
        global_session_token=issued.session_token,
        global_csrf_token=issued.csrf_token,
        workspace_id=workspace.workspace_id,
        **chain,
    )
    context = authenticate_workspace_session_as_auth_context(config, token, csrf_token=csrf)
    payload = CreateBudgetRuleRequest(
        scope_type="tenant", daily_budget_cny="12.345678", exhausted_action="block"
    )
    rule = create_budget_rule(config, context, payload, **chain).budget_rule
    assert rule.daily_budget_cny == "12.345678"
    # These production writes previously used SQLite-only INSERT OR IGNORE.
    # Exercise real native constraints, deduplication and returned rowcount.
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect_database(native_business_database) as connection:
        shared = dict(tenant_id=rule.tenant_id, environment_id=rule.environment_id,
                      budget_rule_id=rule.budget_rule_id, scope_type="tenant",
                      scope_id=rule.tenant_id, budget_date=stamp[:10], now=stamp)
        reservation_args = dict(**shared, request_id="native-reservation",
                                trace_id="native-reservation", reserved_cost_cny="0.123456")
        first, created = ensure_budget_reservation(connection, **reservation_args)
        repeated, repeated_created = ensure_budget_reservation(connection, **reservation_args)
        assert created and not repeated_created
        assert first.budget_reservation_id == repeated.budget_reservation_id
        assert Decimal(repeated.reserved_cost_cny) == Decimal("0.123456")
        connection.execute("SAVEPOINT invalid_reservation")
        with pytest.raises(sqlite3.IntegrityError):
            ensure_budget_reservation(connection, **{
                **reservation_args, "reserved_cost_cny": "-1",
            })
        connection.execute("ROLLBACK TO SAVEPOINT invalid_reservation")
        connection.execute("RELEASE SAVEPOINT invalid_reservation")
        block_args = dict(**shared, request_id="native-block", trace_id="native-block",
                          blocked_cost_cny="12.345678", budget_cny="12.345678")
        block, created = ensure_budget_block(connection, **block_args)
        repeated_block, repeated_created = ensure_budget_block(connection, **block_args)
        assert created and not repeated_created
        assert block.budget_block_id == repeated_block.budget_block_id
        assert connection.execute(
            "SELECT COUNT(*) FROM budget_block_events WHERE budget_block_id=?",
            (block.budget_block_id,),
        ).fetchone()[0] == 1
        runtime_rule = BudgetRule(
            budget_rule_id=rule.budget_rule_id, tenant_id=rule.tenant_id,
            environment_id=rule.environment_id, scope_type="tenant", scope_id=rule.tenant_id,
            daily_budget_cny=Decimal("12.345678"), warning_threshold_percent=80,
            exhausted_action="block",
        )
        warning = BudgetDecision(
            budget_rule_id=rule.budget_rule_id, scope_type="tenant", scope_id=rule.tenant_id,
            budget_status="warning", action_taken="warn", reason_code="budget.warning",
            current_cost_cny=Decimal("10"), budget_limit_cny=Decimal("12.345678"),
        )
        for _ in range(2):
            _insert_alert_event(
                connection, runtime_rule, warning, budget_date=stamp[:10],
                spent_before=Decimal("10"), request_id="native-warning",
                trace_id="native-warning", threshold_percent=80,
                event_type="budget.threshold_reached",
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM budget_alert_events WHERE budget_rule_id=?",
            (rule.budget_rule_id,),
        ).fetchone()[0] == 1
        # Roll back the synthetic gateway facts; the actual rule remains intact.
        connection.rollback()
    with pytest.raises(ApiError) as conflict:
        create_budget_rule(config, context, payload, **chain)
    assert conflict.value.code == "budget.rule_conflict"
    result = update_budget_mode(
        config, context, rule.budget_rule_id, UpdateBudgetModeRequest(mode="alert_only"), **chain
    )
    assert result.budget_rule.exhausted_action == "alert_only"
    original_begin_read = budget_service._begin_read_transaction

    def checked_begin_read(connection):
        original_begin_read(connection)
        assert connection.execute("SHOW transaction_read_only").fetchone()[0] == "on"

    monkeypatch.setattr(budget_service, "_begin_read_transaction", checked_begin_read)
    listed = list_budget_rules(
        config,
        context,
        scope_type=None,
        environment_id=None,
        environment=None,
        status=None,
        **chain,
    )
    assert len(listed.budget_rules) == 1
    status = get_budget_status(
        config,
        context,
        virtual_key_id=None,
        environment_id=None,
        project_id=None,
        application_id=None,
        **chain,
    )
    assert status.statuses[0].budget_cny == "12.345678"
    adjustment = {
        "adjustment_type": "manual_debit",
        "amount": "0.000003",
        "reason": "Native test adjustment",
    }
    finance_chain = {**chain, "audit_request_meta": AuditRequestMeta(None, "native-test")}
    posted = create_finops_adjustment(
        config, context, adjustment, idempotency_key="native-finance-adjustment", **finance_chain
    )
    replay = create_finops_adjustment(
        config, context, adjustment, idempotency_key="native-finance-adjustment", **finance_chain
    )
    assert replay.idempotent_replay
    assert replay.ledger_entry.finops_ledger_entry_id == posted.ledger_entry.finops_ledger_entry_id
    reversal = create_finops_adjustment(
        config,
        context,
        {
            **adjustment,
            "adjustment_type": "reversal",
            "amount": "0.000001",
            "reversal_of_entry_id": posted.ledger_entry.finops_ledger_entry_id,
        },
        idempotency_key="native-finance-reversal",
        **finance_chain,
    )
    assert reversal.ledger_entry.direction == "credit"
    remaining = {
        **adjustment,
        "adjustment_type": "reversal",
        "amount": "0.000002",
        "reversal_of_entry_id": posted.ledger_entry.finops_ledger_entry_id,
    }
    create_finops_adjustment(
        config, context, remaining, idempotency_key="native-finance-reversal-remaining",
        **finance_chain,
    )
    with pytest.raises(ApiError) as excess:
        create_finops_adjustment(
            config, context, remaining, idempotency_key="native-finance-reversal-excess",
            **finance_chain,
        )
    assert excess.value.code == "finops.reversal_exceeds_remaining"
    ledger = list_finops_ledger(config, context, query={}, limit=20, cursor=None, **chain)
    assert ledger.totals.by_currency["CNY"].net == "0.000000"
    assert len(ledger.ledger_entries) == 3
    with connect_database(native_business_database) as connection:
        connection.execute("""UPDATE workspace_sessions SET status='revoked',
            revoked_at='2026-09-09T00:00:00Z', revoked_reason='workspace_switched',
            updated_at='2026-09-09T00:00:00Z' WHERE status='active'""")
        connection.commit()
    with pytest.raises(ApiError):
        create_finops_adjustment(
            config, context, adjustment, idempotency_key="native-finance-adjustment", **finance_chain
        )
    with pytest.raises(ApiError):
        list_budget_rules(
            config,
            context,
            scope_type=None,
            environment_id=None,
            environment=None,
            status=None,
            **chain,
        )
