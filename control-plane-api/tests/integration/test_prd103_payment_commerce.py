"""PRD-103 订单、支付回调、乱序、催收、退款和隐私端到端合同。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.routes import router as identity_router
from metahub_control.payment_commerce import routes as payment_routes
from metahub_control.payment_commerce import service as payment_service
from metahub_control.payment_commerce.routes import router as payment_router
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
from metahub_shared.payment_commerce import sign_webhook
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ADMIN_EMAIL = "admin@metahub.local"
ADMIN_PASSWORD = "LocalAdmin!2026"
GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf"
CURRENT_WEBHOOK_SECRET = "test-prd103-current-webhook-secret-independent-2026"
PREVIOUS_WEBHOOK_SECRET = "test-prd103-previous-webhook-secret-independent-2026"
RECEIPT_SECRET = "test-prd103-receipt-encryption-secret-independent-2026"


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """创建隔离数据库、receipt 对象根、真实 Workspace 登录和受控 Platform 上下文。"""

    database_url = f"sqlite:///{(tmp_path / 'payment.sqlite3').as_posix()}"
    storage_root = tmp_path / "payment-receipts"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_AUTH_TOKEN_SECRET": "test-prd103-auth-token-secret-2026",
        "METAHUB_IDENTITY_SESSION_SECRET": "test-prd103-global-session-secret-2026",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-prd103-challenge-secret-2026",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": "test-prd103-pii-secret-2026-long",
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-prd103-workspace-secret-2026-long",
        "METAHUB_PLATFORM_SESSION_SECRET": "test-prd103-platform-secret-2026-long",
        "METAHUB_SUPPORT_SESSION_SECRET": "test-prd103-support-secret-2026-long",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "test-prd103-mfa-secret-2026-long",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": (
            "test-prd103-recovery-secret-2026-long"
        ),
        "METAHUB_PAYMENT_WEBHOOK_KEY_VERSION": "2",
        "METAHUB_PAYMENT_WEBHOOK_SECRET": CURRENT_WEBHOOK_SECRET,
        "METAHUB_PAYMENT_WEBHOOK_PREVIOUS_KEY_VERSION": "1",
        "METAHUB_PAYMENT_WEBHOOK_PREVIOUS_SECRET": PREVIOUS_WEBHOOK_SECRET,
        "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_KEY_VERSION": "3",
        "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_SECRET": RECEIPT_SECRET,
        "METAHUB_PAYMENT_RECEIPT_STORAGE_ROOT": str(storage_root),
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    apply_migrations(database_url)
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        account_id = connection.execute(
            "SELECT account_id FROM global_accounts WHERE email_normalized=?", (ADMIN_EMAIL,)
        ).fetchone()[0]
        operator_id = "pto_" + "7" * 32
        connection.execute(
            """
            INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,NULL,datetime('now'),datetime('now'))
            """,
            (operator_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
            VALUES(?,'platform_billing','active',NULL,datetime('now'),NULL)
            """,
            (operator_id,),
        )
        connection.commit()
    finally:
        connection.close()
    platform = PlatformContext(
        platform_session_id="pts_test_prd103",
        operator_id=operator_id,
        account_id=account_id,
        global_session_id="gis_test_prd103",
        factor_id="mfa_test_prd103",
        factor_revision=1,
        operator_revision=1,
        roles=("platform_billing",),
        permissions=(
            "platform.billing.adjust",
            "platform.billing.read",
            "platform.catalog.read",
            "platform.catalog.write",
        ),
        expires_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    monkeypatch.setattr(
        saas_routes, "authenticate_platform_session_context", lambda *args, **kwargs: platform
    )
    monkeypatch.setattr(
        saas_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: platform,
    )
    monkeypatch.setattr(
        payment_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: platform,
    )
    monkeypatch.setattr(
        payment_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: platform,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-103 Payment Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag",),
    )
    app.include_router(identity_router)
    app.include_router(workspace_access_router)
    app.include_router(saas_router)
    app.include_router(payment_router)
    return database_url, storage_root, app


def _origin_headers(**extra: str) -> dict[str, str]:
    """构造同站浏览器写请求头。"""

    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site", **extra}


def _csrf(client: AsyncClient, cookie_name: str, **extra: str) -> dict[str, str]:
    """从当前测试客户端读取 CSRF Cookie 并形成双提交 header。"""

    value = client.cookies.get(cookie_name)
    assert value
    return _origin_headers(**{"X-CSRF-Token": value, **extra})


async def _enter_workspace(client: AsyncClient) -> str:
    """使用隔离测试账号真实登录并切换到 organization Workspace。"""

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
    return workspace_id


async def _create_plan(client: AsyncClient, code: str, amount: str) -> str:
    """通过 Platform API 创建发布中的付费计划。"""

    created = await client.post(
        "/platform/plans",
        json={
            "plan_code": code,
            "display_name": code.title(),
            "display_order": 10,
            "billing_period": "monthly",
            "currency": "CNY",
            "unit_price": amount,
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
        headers=_csrf(client, PLATFORM_CSRF_COOKIE),
    )
    assert created.status_code == 201, created.text
    return created.json()["plans"][0]["versions"][0]["plan_version_id"]


def _event_body(
    *,
    event_id: str,
    version: int,
    event_type: str,
    intent_id: str,
    client_token: str,
    amount_minor: int,
    occurred_at: datetime,
    refund_id: str | None = None,
) -> bytes:
    """生成渠道严格 JSON；签名必须覆盖这里返回的精确 bytes。"""

    value = {
        "event_id": event_id,
        "event_version": version,
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "event_type": event_type,
        "payment_intent_id": intent_id,
        "client_token": client_token,
        "amount_minor": amount_minor,
        "currency": "CNY",
    }
    if refund_id is not None:
        value["refund_id"] = refund_id
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


async def _webhook(
    transport: ASGITransport,
    *,
    body: bytes,
    clock: datetime,
    secret: str = CURRENT_WEBHOOK_SECRET,
    key_version: int = 2,
    extra_headers: dict[str, str] | None = None,
    signature_body: bytes | None = None,
):
    """从无 Cookie 的独立渠道客户端发送签名回调。"""

    timestamp = str(int(clock.timestamp()))
    headers = {
        "Content-Type": "application/json",
        "X-MetaHub-Payment-Timestamp": timestamp,
        "X-MetaHub-Payment-Key-Version": str(key_version),
        "X-MetaHub-Payment-Signature": sign_webhook(
            secret, timestamp=timestamp, body=signature_body or body
        ),
        **(extra_headers or {}),
    }
    async with AsyncClient(transport=transport, base_url="https://testserver") as provider:
        return await provider.post("/payments/webhooks/mockpay", content=body, headers=headers)


@pytest.mark.anyio
async def test_capture_replay_dunning_refund_and_sensitive_zero_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """贯穿三种凭据域，覆盖 exactly-once、乱序、催收、退款和 PCI 零泄漏。"""

    database_url, storage_root, app = _prepare(monkeypatch, tmp_path)
    clock = [datetime(2026, 8, 23, 6, 0, tzinfo=UTC)]
    monkeypatch.setattr(payment_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(saas_service, "_utcnow", lambda: clock[0])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as platform:
        platform.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        first_plan = await _create_plan(platform, "prd103-team", "100.00")
        second_plan = await _create_plan(platform, "prd103-scale", "200.00")

    async with AsyncClient(transport=transport, base_url="https://testserver") as workspace:
        await _enter_workspace(workspace)
        created = await workspace.post(
            "/subscriptions",
            json={"plan_version_id": first_plan, "start_trial": False},
            headers=_csrf(
                workspace,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd103-subscription-create"},
            ),
        )
        assert created.status_code == 201, created.text
        subscription_id = created.json()["subscription"]["subscription_id"]
        listed = await workspace.get("/billing/orders")
        assert listed.status_code == 200, listed.text
        first_order = listed.json()["orders"][0]
        assert first_order["status"] == "pending_payment"

        # PCI 类字段即使值是测试 canary 也必须在严格 DTO 边界拒绝，不能进入任何存储。
        pci_canaries = (
            "4111111111111111",
            "cvv-prd103-999",
            "payment-password-prd103",
        )
        rejected_card = await workspace.post(
            "/payments/intents",
            json={
                "order_id": first_order["order_id"],
                "provider": "mockpay",
                "card_number": pci_canaries[0],
                "cvv": pci_canaries[1],
                "payment_password": pci_canaries[2],
            },
            headers=_csrf(
                workspace,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd103-card-rejected"},
            ),
        )
        assert rejected_card.status_code == 422
        intent_key = "prd103-payment-intent-first"
        intent_response = await workspace.post(
            "/payments/intents",
            json={"order_id": first_order["order_id"], "provider": "mockpay"},
            headers=_csrf(
                workspace,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": intent_key},
            ),
        )
        assert intent_response.status_code == 201, intent_response.text
        intent = intent_response.json()["payment_intent"]
        client_token = intent_response.json()["client_token"]
        assert client_token.startswith("ptok_")
        replay = await workspace.post(
            "/payments/intents",
            json={"order_id": first_order["order_id"], "provider": "mockpay"},
            headers=_csrf(
                workspace,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": intent_key},
            ),
        )
        assert replay.status_code == 200
        assert replay.json()["client_token"] is None

    capture = _event_body(
        event_id="evt-prd103-capture-0001",
        version=2,
        event_type="payment.succeeded",
        intent_id=intent["payment_intent_id"],
        client_token=client_token,
        amount_minor=10_000,
        occurred_at=clock[0],
    )
    browser_conflict = await _webhook(
        transport,
        body=capture,
        clock=clock[0],
        extra_headers={"Cookie": "__Host-metahub-workspace-session=forbidden"},
    )
    assert browser_conflict.status_code == 400
    tampered = await _webhook(
        transport,
        body=capture + b" ",
        clock=clock[0],
        signature_body=capture,
    )
    assert tampered.status_code == 400
    accepted = await _webhook(transport, body=capture, clock=clock[0])
    assert accepted.status_code == 202, accepted.text
    # 一百次网络重放只能命中同一个 receipt，不能复制 attempt、权益或商业效果。
    repeated = await asyncio.gather(
        *[_webhook(transport, body=capture, clock=clock[0]) for _ in range(100)]
    )
    assert all(item.status_code == 200 for item in repeated)
    assert payment_service.process_payment_receipts(
        app.state.config, worker_id="prd103-test-worker"
    ) == 1

    # 上一 key 的旧版本 processing 事件保留 attempt，但不得把 succeeded 倒退。
    stale = _event_body(
        event_id="evt-prd103-stale-0001",
        version=1,
        event_type="payment.processing",
        intent_id=intent["payment_intent_id"],
        client_token=client_token,
        amount_minor=10_000,
        occurred_at=clock[0] - timedelta(minutes=1),
    )
    stale_response = await _webhook(
        transport,
        body=stale,
        clock=clock[0],
        secret=PREVIOUS_WEBHOOK_SECRET,
        key_version=1,
    )
    assert stale_response.status_code == 202
    assert payment_service.process_payment_receipts(
        app.state.config, worker_id="prd103-test-worker"
    ) == 1

    async with AsyncClient(transport=transport, base_url="https://testserver") as workspace:
        await _enter_workspace(workspace)
        captured = await workspace.get("/billing/orders")
        captured_order = next(
            item
            for item in captured.json()["orders"]
            if item["order_id"] == first_order["order_id"]
        )
        assert captured_order["status"] == "fulfilled"
        assert captured_order["captured_amount_minor"] == 10_000
        assert captured.json()["payment_intents"][0]["status"] == "succeeded"
        current = await workspace.get("/subscriptions/current")
        assert current.json()["subscription"]["status"] == "active"

        changed = await workspace.patch(
            f"/subscriptions/{subscription_id}",
            json={"operation": "change_plan", "plan_version_id": second_plan},
            headers=_csrf(
                workspace,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd103-plan-upgrade", "If-Match": '"2"'},
            ),
        )
        assert changed.status_code == 200, changed.text
        orders = await workspace.get("/billing/orders")
        second_order = next(
            item for item in orders.json()["orders"] if item["order_id"] != first_order["order_id"]
        )
        second_intent_response = await workspace.post(
            "/payments/intents",
            json={"order_id": second_order["order_id"], "provider": "mockpay"},
            headers=_csrf(
                workspace,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd103-payment-intent-second"},
            ),
        )
        second_intent = second_intent_response.json()["payment_intent"]
        second_token = second_intent_response.json()["client_token"]

    failed = _event_body(
        event_id="evt-prd103-failed-0001",
        version=1,
        event_type="payment.failed",
        intent_id=second_intent["payment_intent_id"],
        client_token=second_token,
        amount_minor=20_000,
        occurred_at=clock[0] + timedelta(minutes=1),
    )
    assert (await _webhook(transport, body=failed, clock=clock[0])).status_code == 202
    assert payment_service.process_payment_receipts(
        app.state.config, worker_id="prd103-test-worker"
    ) == 1
    async with AsyncClient(transport=transport, base_url="https://testserver") as workspace:
        await _enter_workspace(workspace)
        dunning = await workspace.get("/billing/orders")
        assert dunning.json()["dunning_cases"][0]["status"] == "open"
        assert dunning.json()["orders"][0]["status"] == "past_due"

    recovered = _event_body(
        event_id="evt-prd103-recovered-0001",
        version=2,
        event_type="payment.succeeded",
        intent_id=second_intent["payment_intent_id"],
        client_token=second_token,
        amount_minor=20_000,
        occurred_at=clock[0] + timedelta(minutes=2),
    )
    assert (await _webhook(transport, body=recovered, clock=clock[0])).status_code == 202
    assert payment_service.process_payment_receipts(
        app.state.config, worker_id="prd103-test-worker"
    ) == 1

    async with AsyncClient(transport=transport, base_url="https://testserver") as workspace:
        await _enter_workspace(workspace)
        refund_response = await workspace.post(
            "/refunds",
            json={
                "order_id": first_order["order_id"],
                "amount_minor": 4_000,
                "reason_code": "customer_request",
                "confirmed": True,
            },
            headers=_csrf(
                workspace,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd103-refund-first"},
            ),
        )
        assert refund_response.status_code == 201, refund_response.text
        refund = refund_response.json()["refund"]
        excessive = await workspace.post(
            "/refunds",
            json={
                "order_id": first_order["order_id"],
                "amount_minor": 7_000,
                "reason_code": "service_failure",
                "confirmed": True,
            },
            headers=_csrf(
                workspace,
                WORKSPACE_CSRF_COOKIE,
                **{"Idempotency-Key": "prd103-refund-excessive"},
            ),
        )
        assert excessive.status_code == 409
        assert excessive.json()["code"] == "refund.amount_exceeded"

    async with AsyncClient(transport=transport, base_url="https://testserver") as platform:
        platform.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        platform.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        reviewed = await platform.post(
            f"/platform/refunds/{refund['refund_id']}/review",
            json={
                "decision": "approve",
                "reason_code": "policy_approved",
                "confirmed": True,
            },
            headers=_csrf(platform, PLATFORM_CSRF_COOKIE, **{"If-Match": '"1"'}),
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["refund"]["status"] == "processing"

    refund_event = _event_body(
        event_id="evt-prd103-refund-0001",
        version=3,
        event_type="refund.succeeded",
        intent_id=intent["payment_intent_id"],
        client_token=client_token,
        amount_minor=4_000,
        occurred_at=clock[0] + timedelta(minutes=3),
        refund_id=refund["refund_id"],
    )
    assert (await _webhook(transport, body=refund_event, clock=clock[0])).status_code == 202
    assert payment_service.process_payment_receipts(
        app.state.config, worker_id="prd103-test-worker"
    ) == 1
    assert payment_service.dispatch_commercial_effects(app.state.config) >= 1

    connection = connect_database(database_url)
    try:
        receipt_count = connection.execute(
            "SELECT COUNT(*) FROM payment_webhook_receipts"
        ).fetchone()[0]
        assert receipt_count == 5
        assert connection.execute("SELECT COUNT(*) FROM payment_attempts").fetchone()[0] == 5
        assert connection.execute(
            "SELECT COUNT(*) FROM commercial_effects WHERE effect_type='capture_fulfilled'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM commercial_effects WHERE effect_type='refund_ledger'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM refunds WHERE refund_id=?", (refund["refund_id"],)
        ).fetchone()[0] == "succeeded"
        assert connection.execute(
            "SELECT refunded_amount_minor FROM subscription_orders WHERE order_id=?",
            (first_order["order_id"],),
        ).fetchone()[0] == 4_000
        serialized = "\n".join(
            str(tuple(row))
            for table in (
                "subscription_orders",
                "payment_intents",
                "payment_attempts",
                "payment_webhook_receipts",
                "refunds",
                "dunning_cases",
                "commercial_effects",
            )
            for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()
        )
        assert client_token not in serialized and second_token not in serialized
        assert all(canary not in serialized for canary in pci_canaries)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
    encrypted_objects = list(storage_root.rglob("*.pay"))
    assert len(encrypted_objects) == 5
    assert all(client_token.encode() not in path.read_bytes() for path in encrypted_objects)
    assert all(canary not in caplog.text for canary in (*pci_canaries, client_token, second_token))
