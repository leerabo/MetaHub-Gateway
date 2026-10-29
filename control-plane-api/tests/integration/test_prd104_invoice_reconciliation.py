"""PRD-104 Statement、发票产物、单次下载、冻结对账和双审批端到端合同。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.routes import router as identity_router
from metahub_control.invoice_reconciliation import routes as invoice_routes
from metahub_control.invoice_reconciliation import service as invoice_service
from metahub_control.invoice_reconciliation.routes import router as invoice_router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.saas_billing import routes as saas_routes
from metahub_control.saas_billing import service as saas_service
from metahub_control.saas_billing.routes import router as saas_router
from metahub_control.workspace_access.constants import WORKSPACE_CSRF_COOKIE
from metahub_control.workspace_access.routes import router as workspace_access_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ADMIN_EMAIL = "admin@metahub.local"
ADMIN_PASSWORD = "LocalAdmin!2026"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"


def _context(operator_id: str, account_id: str, role: str) -> PlatformContext:
    permissions = (
        ("platform.billing.read", "platform.catalog.read", "platform.catalog.write")
        if role == "platform_billing"
        else ("platform.compliance.read",)
    )
    return PlatformContext(
        platform_session_id="pts_" + operator_id[4:],
        operator_id=operator_id,
        account_id=account_id,
        global_session_id="gss_" + operator_id[4:],
        factor_id="mfa_" + operator_id[4:],
        factor_revision=1,
        operator_revision=1,
        roles=(role,),
        permissions=permissions,
        expires_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    database_url = f"sqlite:///{(tmp_path / 'invoice.sqlite3').as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_AUTH_TOKEN_SECRET": "test-prd104-auth-token-secret-2026",
        "METAHUB_IDENTITY_SESSION_SECRET": "test-prd104-global-session-secret-2026",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-prd104-challenge-secret-2026",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": "test-prd104-pii-secret-2026-long",
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-prd104-workspace-secret-2026-long",
        "METAHUB_PLATFORM_SESSION_SECRET": "test-prd104-platform-secret-2026-long",
        "METAHUB_SUPPORT_SESSION_SECRET": "test-prd104-support-secret-2026-long",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "test-prd104-mfa-secret-2026-long",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": "test-prd104-recovery-secret-2026-long",
        "METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET": (
            "test-prd104-business-verification-secret-independent-2026"
        ),
        "METAHUB_PAYMENT_WEBHOOK_SECRET": "test-prd104-payment-webhook-secret-2026-long",
        "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_SECRET": (
            "test-prd104-payment-receipt-secret-2026-long"
        ),
        "METAHUB_PAYMENT_RECEIPT_STORAGE_ROOT": str(tmp_path / "payment-receipts"),
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    apply_migrations(database_url)
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        admin_account_id = connection.execute(
            "SELECT account_id FROM global_accounts WHERE email_normalized=?", (ADMIN_EMAIL,)
        ).fetchone()[0]
        actor_specs = (
            ("1", admin_account_id, "platform_billing"),
            ("2", "gac_" + "2" * 32, "platform_billing"),
            ("3", "gac_" + "3" * 32, "platform_compliance"),
        )
        contexts: list[PlatformContext] = []
        for suffix, account_id, role in actor_specs:
            if suffix != "1":
                connection.execute(
                    """
                    INSERT INTO global_accounts(
                      account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                      credential_revision,session_revision,mfa_state,failed_login_count,
                      locked_until,last_login_at,created_at,updated_at)
                    VALUES(?,?,NULL,?,'active',0,1,1,'not_enrolled',0,NULL,NULL,
                      '2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')
                    """,
                    (account_id, f"prd104-{suffix}@example.test", f"PRD-104 Actor {suffix}"),
                )
            operator_id = "pto_" + suffix * 32
            connection.execute(
                """
                INSERT INTO platform_operators(
                  operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
                VALUES(?,?,'active',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')
                """,
                (operator_id, account_id),
            )
            connection.execute(
                """
                INSERT INTO platform_operator_role_bindings(
                  operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                VALUES(?,?,'active',NULL,'2026-08-23T00:00:00Z',NULL)
                """,
                (operator_id, role),
            )
            contexts.append(_context(operator_id, account_id, role))
        connection.commit()
    finally:
        connection.close()
    current = [contexts[0]]
    monkeypatch.setattr(
        saas_routes, "authenticate_platform_session_context", lambda *args, **kwargs: current[0]
    )
    monkeypatch.setattr(
        saas_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: current[0],
    )
    monkeypatch.setattr(
        invoice_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: current[0],
    )
    monkeypatch.setattr(
        invoice_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: current[0],
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-104 Invoice Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "X-CSRF-Token",
            "X-MetaHub-Invoice-Download-Token",
        ),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag", "Content-Disposition"),
    )
    app.include_router(identity_router)
    app.include_router(workspace_access_router)
    app.include_router(saas_router)
    app.include_router(invoice_router)
    return database_url, app, current, contexts


def _origin_headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site", **extra}


def _csrf(client: AsyncClient, cookie_name: str, **extra: str) -> dict[str, str]:
    value = client.cookies.get(cookie_name)
    assert value
    return _origin_headers(**{"X-CSRF-Token": value, **extra})


async def _enter_workspace(client: AsyncClient) -> tuple[str, str]:
    logged_in = await client.post(
        "/identity/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        headers=_origin_headers(),
    )
    assert logged_in.status_code == 200, logged_in.text
    listed = await client.get("/identity/workspaces")
    workspace_id = listed.json()["workspaces"][0]["workspace_id"]
    switched = await client.post(
        "/identity/workspace-switches",
        json={"workspace_id": workspace_id},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert switched.status_code == 200, switched.text
    return workspace_id, ""


async def _create_subscription(platform: AsyncClient, workspace: AsyncClient) -> str:
    created_plan = await platform.post(
        "/platform/plans",
        json={
            "plan_code": "prd104-invoice",
            "display_name": "PRD-104 Invoice",
            "display_order": 10,
            "billing_period": "monthly",
            "currency": "CNY",
            "unit_price": "100.00",
            "trial_days": 0,
            "entitlements": {
                "gateway_requests_per_month": 1000,
                "workspace_seats": 10,
                "model_aliases": 20,
                "retention_days": 30,
                "priority_support": False,
            },
            "publish": True,
        },
        headers=_csrf(platform, PLATFORM_CSRF_COOKIE),
    )
    assert created_plan.status_code == 201, created_plan.text
    plan_version_id = created_plan.json()["plans"][0]["versions"][0]["plan_version_id"]
    created_subscription = await workspace.post(
        "/subscriptions",
        json={"plan_version_id": plan_version_id, "start_trial": False},
        headers=_csrf(
            workspace,
            WORKSPACE_CSRF_COOKIE,
            **{"Idempotency-Key": "prd104-subscription-create"},
        ),
    )
    assert created_subscription.status_code == 201, created_subscription.text
    return created_subscription.json()["subscription"]["subscription_id"]


def _close_cycle_and_verify_profile(database_url: str, workspace_id: str) -> None:
    connection = connect_database(database_url)
    try:
        subscription = connection.execute(
            "SELECT * FROM workspace_subscriptions WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        snapshot = connection.execute(
            """
            SELECT * FROM entitlement_snapshots WHERE workspace_id=?
            ORDER BY created_at DESC,entitlement_snapshot_id DESC LIMIT 1
            """,
            (workspace_id,),
        ).fetchone()
        if snapshot is None:
            order_record = connection.execute(
                """
                SELECT * FROM subscription_order_records WHERE subscription_id=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (subscription["subscription_id"],),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO entitlement_snapshots(
                  entitlement_snapshot_id,workspace_id,tenant_id,subscription_id,
                  plan_version_id,source_order_record_id,snapshot_version,entitlements_json,
                  entitlements_sha256,effective_from,effective_until,created_at)
                VALUES('ent_10400000000000000000000000000000',?,?,?,?,?,1,'{}',?,
                  '2026-07-01T00:00:00Z',NULL,'2026-07-01T00:00:00Z')
                """,
                (
                    workspace_id,
                    subscription["tenant_id"],
                    subscription["subscription_id"],
                    subscription["plan_version_id"],
                    order_record["order_record_id"],
                    "9" * 64,
                ),
            )
            snapshot = connection.execute(
                "SELECT * FROM entitlement_snapshots WHERE entitlement_snapshot_id=?",
                ("ent_10400000000000000000000000000000",),
            ).fetchone()
        membership_id = subscription["created_by_membership_id"]
        connection.execute(
            """
            INSERT INTO workspace_billing_profiles(
              workspace_billing_profile_id,workspace_id,tenant_id,legal_entity_profile_id,
              invoice_title_ciphertext,tax_number_ciphertext,tax_number_blind_index,
              address_ciphertext,phone_ciphertext,bank_name_ciphertext,bank_account_ciphertext,
              key_version,idempotency_sha256,request_sha256,revision,verification_status,
              created_at,updated_at)
            VALUES('wbp_10400000000000000000000000000000',?,?,NULL,?,?,?,NULL,NULL,NULL,NULL,
              1,?,?,1,'verified','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')
            """,
            (
                workspace_id,
                subscription["tenant_id"],
                "c" * 64,
                "d" * 64,
                "hmac-sha256:" + "e" * 64,
                "f" * 64,
                "a" * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO platform_billing_cycles(
              billing_cycle_id,workspace_id,tenant_id,subscription_id,plan_version_id,
              entitlement_snapshot_id,status,period_started_at,period_ends_at,currency,
              base_amount_minor,source_watermark,summary_set_sha256,revision,created_at,updated_at)
            VALUES('pbc_10400000000000000000000000000000',?,?,?,?,?,'closed',
              '2026-07-01T00:00:00Z','2026-08-01T00:00:00Z','CNY',10000,0,?,1,
              '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')
            """,
            (
                workspace_id,
                subscription["tenant_id"],
                subscription["subscription_id"],
                subscription["plan_version_id"],
                snapshot["entitlement_snapshot_id"],
                "b" * 64,
            ),
        )
        assert membership_id
        connection.commit()
    finally:
        connection.close()


@pytest.mark.anyio
async def test_invoice_download_reconciliation_and_two_domain_reversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_url, app, current, contexts = _prepare(monkeypatch, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as platform:
        platform.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        async with AsyncClient(transport=transport, base_url="https://testserver") as workspace:
            workspace_id, _tenant_id = await _enter_workspace(workspace)
            await _create_subscription(platform, workspace)
            _close_cycle_and_verify_profile(database_url, workspace_id)

            statements = await workspace.get("/billing/statements")
            assert statements.status_code == 200, statements.text
            statement = statements.json()["statements"][0]
            assert statement["currency"] == "CNY"
            assert statement["subtotal_minor"] == 10000
            assert statement["tax_minor"] == 600
            assert statement["total_minor"] == 10600

            rejected_authority = await workspace.post(
                "/invoices",
                json={
                    "billing_statement_id": statement["billing_statement_id"],
                    "invoice_type": "electronic_general",
                    "total_minor": 1,
                },
                headers=_csrf(
                    workspace,
                    WORKSPACE_CSRF_COOKIE,
                    **{"Idempotency-Key": "prd104-invoice-authority-reject"},
                ),
            )
            assert rejected_authority.status_code == 422

            created = await workspace.post(
                "/invoices",
                json={
                    "billing_statement_id": statement["billing_statement_id"],
                    "invoice_type": "electronic_general",
                },
                headers=_csrf(
                    workspace,
                    WORKSPACE_CSRF_COOKIE,
                    **{"Idempotency-Key": "prd104-invoice-create-0001"},
                ),
            )
            assert created.status_code == 201, created.text
            invoice_id = created.json()["invoice"]["invoice_request_id"]
            replay = await workspace.post(
                "/invoices",
                json={
                    "billing_statement_id": statement["billing_statement_id"],
                    "invoice_type": "electronic_general",
                },
                headers=_csrf(
                    workspace,
                    WORKSPACE_CSRF_COOKIE,
                    **{"Idempotency-Key": "prd104-invoice-create-0001"},
                ),
            )
            assert replay.status_code == 200
            assert replay.json()["idempotent_replay"] is True
            assert invoice_service.process_invoice_provider_once(
                app.state.config, worker_id="prd104-test-worker"
            )
            assert not invoice_service.process_invoice_provider_once(
                app.state.config, worker_id="prd104-test-worker"
            )

            issued = await workspace.get(f"/invoices/{invoice_id}")
            assert issued.status_code == 200, issued.text
            invoice = issued.json()["invoice"]
            assert invoice["status"] == "issued"
            assert {item["mime_type"] for item in invoice["artifacts"]} == {
                "application/pdf",
                "text/csv",
            }
            artifact = next(
                item for item in invoice["artifacts"] if item["mime_type"] == "application/pdf"
            )
            token = await workspace.post(
                artifact["download_action"],
                json={},
                headers=_csrf(workspace, WORKSPACE_CSRF_COOKIE),
            )
            assert token.status_code == 200, token.text
            raw_token = token.json()["download_token"]
            downloaded = await workspace.get(
                "/invoice-downloads",
                headers={"X-MetaHub-Invoice-Download-Token": raw_token},
            )
            assert downloaded.status_code == 200
            assert downloaded.content.startswith(b"%PDF-1.4")
            assert downloaded.headers["cache-control"] == "no-store, private"
            assert downloaded.headers["x-content-type-options"] == "nosniff"
            repeated = await workspace.get(
                "/invoice-downloads",
                headers={"X-MetaHub-Invoice-Download-Token": raw_token},
            )
            assert repeated.status_code == 404

            workspace.cookies.set(PLATFORM_SESSION_COOKIE, "x" * 43)
            conflict = await workspace.get("/billing/statements")
            assert conflict.status_code == 400
            workspace.cookies.delete(PLATFORM_SESSION_COOKIE)

        frozen = await platform.post(
            "/platform/reconciliations",
            json={"scope": "all", "confirmed": True},
            headers=_csrf(platform, PLATFORM_CSRF_COOKIE),
        )
        assert frozen.status_code == 202, frozen.text
        run_id = frozen.json()["reconciliation"]["reconciliation_run_id"]
        assert invoice_service.process_reconciliation_once(
            app.state.config, worker_id="prd104-reconciliation-worker"
        )
        differences = await platform.get(f"/platform/reconciliations/{run_id}/differences")
        assert differences.status_code == 200, differences.text
        assert differences.json()["reconciliation"]["status"] == "completed"

        reversal = await platform.post(
            f"/platform/invoices/{invoice_id}/reversals",
            json={"reason_code": "invoice_error", "confirmed": True},
            headers=_csrf(
                platform,
                PLATFORM_CSRF_COOKIE,
                **{"Idempotency-Key": "prd104-reversal-create-0001"},
            ),
        )
        assert reversal.status_code == 201, reversal.text
        reversal_id = reversal.json()["reversal"]["invoice_reversal_request_id"]
        self_approval = await platform.post(
            f"/platform/invoice-reversals/{reversal_id}/approvals",
            json={"decision": "approve", "reason_code": "facts_confirmed", "confirmed": True},
            headers=_csrf(
                platform,
                PLATFORM_CSRF_COOKIE,
                **{"Idempotency-Key": "prd104-reversal-self-0001"},
            ),
        )
        assert self_approval.status_code == 409

        current[0] = contexts[1]
        billing_approval = await platform.post(
            f"/platform/invoice-reversals/{reversal_id}/approvals",
            json={"decision": "approve", "reason_code": "facts_confirmed", "confirmed": True},
            headers=_csrf(
                platform,
                PLATFORM_CSRF_COOKIE,
                **{"Idempotency-Key": "prd104-reversal-billing-0001"},
            ),
        )
        assert billing_approval.status_code == 200, billing_approval.text
        assert billing_approval.json()["reversal"]["status"] == "pending_approvals"

        current[0] = contexts[2]
        compliance_approval = await platform.post(
            f"/platform/invoice-reversals/{reversal_id}/approvals",
            json={"decision": "approve", "reason_code": "policy_confirmed", "confirmed": True},
            headers=_csrf(
                platform,
                PLATFORM_CSRF_COOKIE,
                **{"Idempotency-Key": "prd104-reversal-compliance-0001"},
            ),
        )
        assert compliance_approval.status_code == 200, compliance_approval.text
        assert compliance_approval.json()["reversal"]["status"] == "queued"
        assert compliance_approval.json()["reversal"]["approved_domains"] == [
            "billing",
            "compliance",
        ]
        assert invoice_service.process_invoice_provider_once(
            app.state.config, worker_id="prd104-reversal-worker"
        )

    connection = connect_database(database_url)
    try:
        stored = connection.execute(
            "SELECT status FROM invoice_requests WHERE invoice_request_id=?", (invoice_id,)
        ).fetchone()
        assert stored["status"] == "reversed"
        assert (
            connection.execute("SELECT COUNT(*) FROM invoice_reversal_approvals").fetchone()[0] == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM invoice_provider_receipts").fetchone()[0] == 2
        )
        encrypted = connection.execute(
            "SELECT object_reference_ciphertext FROM invoice_artifacts LIMIT 1"
        ).fetchone()[0]
        assert "invoice-artifacts" not in encrypted
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
