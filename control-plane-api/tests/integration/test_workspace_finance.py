"""Organization-session budgets and internal ledger, without paid model calls."""

from contextlib import aclosing

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.budgets import service as budgets
from metahub_control.budgets.routes import router as budget_router
from metahub_control.finops import service as finops
from metahub_control.finops.routes import router as finops_router
from metahub_shared.db import connect_database
from metahub_shared.db.connection import database_connection_pool_scope
from metahub_shared.runtime.budget_blocks import ensure_budget_block
from metahub_shared.runtime.errors import ApiError
from test_prd095_platform_iam import GLOBAL_CSRF_COOKIE, WORKSPACE_CSRF_COOKIE, _csrf
from test_prd095_platform_iam import migrated_database as migrated_database
from test_prd147_workspace_files import _headers as subject_headers
from test_prd147_workspace_files import workspace_http_subjects
from test_workspace_consumers import _consumer, _key, _open_organization, _prepare

RULE = {
    "scope_type": "tenant",
    "name": "Internal budget",
    "daily_budget_cny": "12.345678",
    "warning_threshold_percent": 80,
    "exhausted_action": "block",
    "status": "active",
}
ADJUSTMENT = {
    "adjustment_type": "manual_debit",
    "amount": "0.000001",
    "reason": "Finance test canary",
}
READS = ("/budgets", "/budget-status", "/finops/ledger")


@pytest.mark.anyio
async def test_workspace_finance_role_and_foreign_cookie_matrix(
    monkeypatch, tmp_path, migrated_database
):
    database, _ = _app(monkeypatch, tmp_path, migrated_database)
    async with aclosing(workspace_http_subjects(database, tmp_path, monkeypatch)) as subjects:
        async for app, actors in subjects:
            app.include_router(budget_router)
            app.include_router(finops_router)
            owner_rule = await actors.source.post(
                "/budgets", json=RULE, headers=subject_headers(actors.source)
            )
            assert owner_rule.status_code == 201, owner_rule.text
            revision = actors.member_revision
            for role, allowed in (
                ("viewer", False),
                ("developer", False),
                ("security_admin", False),
                ("legal_admin", False),
                ("auditor", False),
                ("finance_admin", True),
                ("admin", True),
            ):
                changed = await actors.source.patch(
                    f"/memberships/{actors.member_membership_id}",
                    json={"role": role},
                    headers={**subject_headers(actors.source), "If-Match": f'"{revision}"'},
                )
                assert changed.status_code == 200, changed.text
                revision = changed.json()["membership"]["revision"]
                switched = await actors.member.post(
                    "/identity/workspace-switches",
                    json={"workspace_id": actors.source_workspace_id},
                    headers=subject_headers(actors.member, global_session=True),
                )
                assert switched.status_code == 200, switched.text
                before = _counts(database)
                for path in READS:
                    response = await actors.member.get(path)
                    assert response.status_code == (200 if allowed else 403), (role, path, response.text)
                headers = {
                    **subject_headers(actors.member),
                    "Idempotency-Key": "finance-role-matrix-" + role,
                }
                updated = await actors.member.patch("/budgets", json=RULE, headers=headers)
                assert updated.status_code == (200 if allowed else 403), (role, updated.text)
                adjusted = await actors.member.post(
                    "/finops/adjustments", json=ADJUSTMENT, headers=headers
                )
                assert adjusted.status_code == (201 if allowed else 403), (role, adjusted.text)
                if not allowed:
                    assert _counts(database) == before
            before = _counts(database)
            for cookie_name in ("__Host-metahub-platform-session", "__Host-metahub-support-session"):
                headers = {
                    **subject_headers(actors.source, attack=True),
                    "Idempotency-Key": "finance-foreign-cookie",
                }
                headers["Cookie"] += f"; {cookie_name}=foreign-domain-canary"
                for method, path, body in (
                    *(("GET", path, None) for path in READS),
                    ("PATCH", "/budgets", RULE),
                    ("POST", "/finops/adjustments", ADJUSTMENT),
                ):
                    response = await actors.anonymous.request(method, path, json=body, headers=headers)
                    assert response.status_code == 400, response.text
                    assert response.json()["code"] == "workspace_session.credential_conflict"
            assert _counts(database) == before


def _app(monkeypatch, tmp_path, migrated_database):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    app.include_router(budget_router)
    app.include_router(finops_router)
    return database, app


def _headers(client, key="workspace-finance-0001"):
    return {**_csrf(client, WORKSPACE_CSRF_COOKIE), "Idempotency-Key": key}


def _counts(database):
    with connect_database(database) as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "budget_rules",
                "finops_adjustments",
                "finops_ledger_entries",
                "audit_logs",
            )
        )


def _revoke(database):
    with database_connection_pool_scope(), connect_database(database) as connection:
        connection.execute("""UPDATE workspace_sessions SET status='revoked',
            revoked_at='2026-09-09T00:00:00Z', revoked_reason='workspace_switched',
            updated_at='2026-09-09T00:00:00Z' WHERE status='active'""")
        connection.commit()


@pytest.mark.anyio
async def test_workspace_budget_and_internal_ledger_roundtrip(
    monkeypatch, tmp_path, migrated_database
):
    database, app = _app(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        for path in READS:
            response = await client.get(path)
            assert response.status_code == 401
            assert "no-store" in response.headers["cache-control"]
        await _open_organization(client, monkeypatch)
        employee = await _consumer(client)
        key = await _key(client, employee)
        headers = _headers(client)
        before = _counts(database)
        for path, body in (("/budgets", RULE), ("/finops/adjustments", ADJUSTMENT)):
            denied = await client.post(path, json=body, headers={"Origin": "http://localhost:5173"})
            assert denied.status_code == 403, denied.text
        assert _counts(database) == before
        created = await client.post("/budgets", json=RULE, headers=headers)
        assert created.status_code == 201, created.text
        rule = created.json()["budget_rule"]
        assert rule["daily_budget_cny"] == "12.345678"
        conflict = await client.post("/budgets", json=RULE, headers=headers)
        assert conflict.status_code == 409 and conflict.json()["code"] == "budget.rule_conflict"
        updated = await client.patch(
            "/budgets", json={**RULE, "daily_budget_cny": "0.000001"}, headers=headers
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["budget_rule"]["budget_rule_id"] == rule["budget_rule_id"]
        changed = await client.patch(
            f"/budgets/{rule['budget_rule_id']}/mode", json={"mode": "alert_only"}, headers=headers
        )
        assert changed.status_code == 200, changed.text
        status = await client.get(
            "/budget-status", params={"virtual_key_id": key["virtual_key"]["virtual_key_id"]}
        )
        assert status.status_code == 200, status.text
        assert status.json()["statuses"][0]["budget_cny"] == "0.000001"
        posted = await client.post("/finops/adjustments", json=ADJUSTMENT, headers=headers)
        assert posted.status_code == 201, posted.text
        entry = posted.json()["ledger_entry"]
        assert entry["amount"] == "0.000001" and entry["currency"] == "CNY"
        counts = _counts(database)
        replay = await client.post("/finops/adjustments", json=ADJUSTMENT, headers=headers)
        assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
        assert (
            replay.json()["ledger_entry"]["finops_ledger_entry_id"]
            == entry["finops_ledger_entry_id"]
        )
        assert _counts(database) == counts
        conflict = await client.post(
            "/finops/adjustments", json={**ADJUSTMENT, "amount": "1"}, headers=headers
        )
        assert conflict.status_code == 409, conflict.text
        reversal = {
            **ADJUSTMENT,
            "adjustment_type": "reversal",
            "reversal_of_entry_id": entry["finops_ledger_entry_id"],
        }
        reversed_entry = await client.post(
            "/finops/adjustments",
            json=reversal,
            headers=_headers(client, "workspace-finance-reverse"),
        )
        assert reversed_entry.status_code == 201, reversed_entry.text
        excess = await client.post(
            "/finops/adjustments",
            json=reversal,
            headers=_headers(client, "workspace-finance-excess"),
        )
        assert excess.status_code == 409, excess.text
        ledger = await client.get("/finops/ledger")
        assert ledger.status_code == 200, ledger.text
        assert ledger.json()["totals"]["by_currency"]["CNY"]["net"] == "0.000000"
        for path in READS:
            response = await client.get(path)
            assert response.status_code == 200, response.text
            assert "no-store" in response.headers["cache-control"]
            mixed = await client.get(path, headers={"Authorization": "Bearer synthetic-mixed"})
            assert mixed.status_code == 400, mixed.text
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as employee_client:
            for path in READS:
                response = await employee_client.get(
                    path, headers={"Authorization": "Bearer " + key["plain_value"]}
                )
                assert response.status_code == 401, response.text
        await _open_organization(
            client, monkeypatch, slug="other-finance-organization", signup=False
        )
        assert (await client.get("/budgets")).json()["budget_rules"] == []
        assert (await client.get("/finops/ledger")).json()["ledger_entries"] == []
        for rule_id in (rule["budget_rule_id"], "budget_missing"):
            denied = await client.patch(
                f"/budgets/{rule_id}/mode", json={"mode": "block"}, headers=_headers(client)
            )
            assert denied.status_code == 404, denied.text
        denied = await client.post("/finops/adjustments", json=reversal, headers=_headers(client))
        assert denied.status_code == 404, denied.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "service,path,hook",
    [
        (budgets, "/budgets", "_environment_summaries"),
        (budgets, "/budget-status", "_budget_aggregate"),
        (finops, "/finops/ledger", "_read_totals"),
    ],
)
async def test_workspace_finance_read_rechecks_new_snapshot(
    monkeypatch, tmp_path, migrated_database, service, path, hook
):
    database, app = _app(monkeypatch, tmp_path, migrated_database)
    with connect_database(database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)
        original = getattr(service, hook)

        def revoke_after_query(*args, **kwargs):
            result = original(*args, **kwargs)
            _revoke(database)
            return result

        monkeypatch.setattr(service, hook, revoke_after_query)
        response = await client.get(path)
        assert response.status_code == 401, response.text
        assert "no-store" in response.headers["cache-control"]
        assert all(
            field not in response.json()
            for field in ("budget_rules", "statuses", "ledger_entries", "totals")
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "service,path,body", [(budgets, "/budgets", RULE), (finops, "/finops/adjustments", ADJUSTMENT)]
)
@pytest.mark.parametrize("boundary", ["admission", "commit", "audit"])
async def test_workspace_finance_write_authority_and_audit_are_atomic(
    monkeypatch, tmp_path, migrated_database, service, path, body, boundary
):
    database, app = _app(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)
        headers, before = _headers(client), _counts(database)
        if boundary == "admission":
            original = service._connect

            def revoked_connection(config):
                _revoke(database)
                return original(config)

            monkeypatch.setattr(service, "_connect", revoked_connection)
        elif boundary == "commit":
            original = ResourceWriteTransaction.verify
            verifications = []

            def reject_final_authorization(transaction):
                verifications.append(transaction.permission)
                if len(verifications) > 1:
                    raise ApiError("auth.permission_denied")
                return original(transaction)

            monkeypatch.setattr(ResourceWriteTransaction, "verify", reject_final_authorization)
        else:

            def unavailable_audit(*args, **kwargs):
                raise ApiError("infra.service_unavailable")

            monkeypatch.setattr(service, "record_audit_log", unavailable_audit)
        response = await client.post(path, json=body, headers=headers)
        assert response.status_code == {"admission": 401, "commit": 403, "audit": 503}[boundary], (
            response.text
        )
        assert _counts(database) == before


@pytest.mark.anyio
async def test_global_session_alone_has_no_finance_access(monkeypatch, tmp_path, migrated_database):
    _database, app = _app(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)
        for cookie in list(client.cookies.jar):
            if "workspace" in cookie.name:
                client.cookies.delete(cookie.name, domain=cookie.domain, path=cookie.path)
        for path in READS:
            assert (await client.get(path)).status_code == 401
        response = await client.post(
            "/budgets", json=RULE, headers=_csrf(client, GLOBAL_CSRF_COOKIE)
        )
        assert response.status_code == 401, response.text


@pytest.mark.anyio
async def test_budget_block_release_rollback_and_success(monkeypatch, tmp_path, migrated_database):
    database, app = _app(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)
        headers = _headers(client)
        response = await client.post("/budgets", json=RULE, headers=headers)
        assert response.status_code == 201, response.text
        rule = response.json()["budget_rule"]
        rule_id = rule["budget_rule_id"]
        with connect_database(database) as connection:
            block, created = ensure_budget_block(
                connection,
                tenant_id=rule["scope_id"],
                environment_id=rule["environment_id"],
                budget_rule_id=rule_id,
                scope_type="tenant",
                scope_id=rule["scope_id"],
                budget_date=budgets._budget_window()[0],
                blocked_cost_cny="20.000000",
                budget_cny=RULE["daily_budget_cny"],
                request_id="req_finance_block",
                trace_id="trc_finance_block",
                now="2026-09-09T00:00:00Z",
            )
            assert created
            connection.commit()
        before = _counts(database)
        original = budgets.record_audit_log

        def unavailable(*args, **kwargs):
            raise ApiError("infra.service_unavailable")

        monkeypatch.setattr(budgets, "record_audit_log", unavailable)
        route = f"/budgets/{rule_id}/mode"
        failed = await client.patch(route, json={"mode": "alert_only"}, headers=headers)
        assert failed.status_code == 503, failed.text
        assert _counts(database) == before
        with connect_database(database) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM budget_blocks WHERE budget_block_id=?",
                    (block.budget_block_id,),
                ).fetchone()[0]
                == "active"
            )
            assert (
                connection.execute(
                    "SELECT exhausted_action FROM budget_rules WHERE budget_rule_id=?", (rule_id,)
                ).fetchone()[0]
                == "block"
            )
        monkeypatch.setattr(budgets, "record_audit_log", original)
        changed = await client.patch(route, json={"mode": "alert_only"}, headers=headers)
        assert changed.status_code == 200, changed.text
        with connect_database(database) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM budget_blocks WHERE budget_block_id=?",
                    (block.budget_block_id,),
                ).fetchone()[0]
                == "released"
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM budget_block_events "
                    "WHERE event_type='budget.unblocked' AND budget_block_id=?",
                    (block.budget_block_id,),
                ).fetchone()[0]
                == 1
            )
