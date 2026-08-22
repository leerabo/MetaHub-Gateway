"""PRD-103 数据库权威订单、支付 receipt、催收和退款服务。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.payment_commerce import (
    PaymentCryptoError,
    decrypt_receipt_body,
    encrypt_receipt_body,
    verify_webhook_signature,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    BillingOrdersResponse,
    DunningCaseInfo,
    PaymentIntentCreateRequest,
    PaymentIntentInfo,
    PaymentIntentResponse,
    PaymentWebhookReceiptResponse,
    PlatformRefundDecisionRequest,
    PlatformRefundQueueResponse,
    RefundCreateRequest,
    RefundInfo,
    RefundResponse,
    SubscriptionOrderInfo,
)

_FINANCE_ROLES = frozenset({"owner", "finance_admin"})
_ORDER_ID_RE = re.compile(r"^ord_[0-9a-f]{32}$")
_INTENT_ID_RE = re.compile(r"^pmi_[0-9a-f]{32}$")
_REFUND_ID_RE = re.compile(r"^rfd_[0-9a-f]{32}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_PROVIDERS = frozenset({"mockpay", "cloudpay"})
_PAYMENT_EVENT_KEYS = frozenset(
    {
        "event_id",
        "event_version",
        "occurred_at",
        "event_type",
        "payment_intent_id",
        "client_token",
        "amount_minor",
        "currency",
    }
)
_REFUND_EVENT_KEYS = _PAYMENT_EVENT_KEYS | {"refund_id"}
_EVENT_TO_STATUS = {
    "payment.processing": "processing",
    "payment.succeeded": "succeeded",
    "payment.failed": "failed",
    "payment.canceled": "canceled",
}


def list_billing_orders(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> BillingOrdersResponse:
    """列出当前 Workspace 的安全订单摘要，并补建 0090 待支付记录的一对一正式订单。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        _require_finance(actor)
        _require_schema(connection)
        _materialize_orders(connection, actor.workspace_id)
        response = _orders_response(
            connection, actor.workspace_id, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("payment.unavailable") from None
    finally:
        connection.close()


def create_payment_intent(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    payload: PaymentIntentCreateRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> PaymentIntentResponse:
    """幂等创建支付意图，只把一次性 SDK token 返回给本次首次请求。"""

    if _ORDER_ID_RE.fullmatch(payload.order_id) is None or payload.provider not in _PROVIDERS:
        raise ApiError("payment.request_invalid")
    _validate_idempotency(idempotency_key, "payment.request_invalid")
    request_sha256 = _intent_hash(
        "payment_intent", {"order_id": payload.order_id, "provider": payload.provider}
    )
    idempotency_sha256 = _sha256(idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_finance(actor)
        _require_schema(connection)
        _materialize_orders(connection, actor.workspace_id)
        replay = connection.execute(
            "SELECT * FROM payment_intents WHERE workspace_id=? AND idempotency_sha256=?",
            (actor.workspace_id, idempotency_sha256),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("payment.idempotency_conflict")
            response = PaymentIntentResponse(
                payment_intent=_payment_intent(replay),
                client_token=None,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        order = connection.execute(
            "SELECT * FROM subscription_orders WHERE order_id=? AND workspace_id=?",
            (payload.order_id, actor.workspace_id),
        ).fetchone()
        if order is None:
            raise ApiError("payment.not_found")
        if order["status"] not in {"pending_payment", "past_due"} or int(
            order["amount_minor"]
        ) <= 0:
            raise ApiError("payment.conflict")
        active = connection.execute(
            """
            SELECT 1 FROM payment_intents
            WHERE order_id=? AND status IN('requires_action','processing','succeeded')
            """,
            (payload.order_id,),
        ).fetchone()
        if active is not None:
            raise ApiError("payment.conflict")
        now = _utcnow()
        intent_id = _id("pmi_")
        # token 仅在进程内短暂停留；数据库只保存独立域 HMAC，重放响应不能恢复它。
        client_token = "ptok_" + os.urandom(32).hex()
        token_hmac = _client_token_hmac(config, intent_id, client_token)
        connection.execute(
            """
            INSERT INTO payment_intents(
              payment_intent_id,order_id,workspace_id,tenant_id,provider,amount_minor,currency,
              client_token_hmac,status,provider_event_version,provider_occurred_at,
              idempotency_sha256,request_sha256,state_revision,expires_at,
              created_by_membership_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,'requires_action',0,NULL,?,?,1,?,?,?,?)
            """,
            (
                intent_id,
                order["order_id"],
                actor.workspace_id,
                actor.tenant_id,
                payload.provider,
                int(order["amount_minor"]),
                order["currency"],
                token_hmac,
                idempotency_sha256,
                request_sha256,
                _db_time(now + timedelta(minutes=20)),
                actor.membership_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        created = connection.execute(
            "SELECT * FROM payment_intents WHERE payment_intent_id=?", (intent_id,)
        ).fetchone()
        if created is None:
            raise ApiError("payment.unavailable")
        response = PaymentIntentResponse(
            payment_intent=_payment_intent(created),
            client_token=client_token,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("payment.unavailable") from None
    finally:
        connection.close()


def persist_webhook_receipt(
    config: AppConfig,
    *,
    provider: str,
    timestamp: str,
    signature: str,
    declared_key_version: int,
    body: bytes,
    request_id: str,
    trace_id: str,
) -> PaymentWebhookReceiptResponse:
    """先对原始 bytes 验签，再解析最小事件头并持久化加密 receipt。"""

    if provider not in _PROVIDERS or config.payment_webhook_secret is None:
        raise ApiError("payment.webhook_invalid")
    if config.payment_receipt_encryption_secret is None:
        raise ApiError("payment.unavailable")
    try:
        stamp = int(timestamp)
    except (TypeError, ValueError):
        raise ApiError("payment.webhook_invalid") from None
    if abs(int(_utcnow().timestamp()) - stamp) > config.payment_webhook_tolerance_seconds:
        raise ApiError("payment.webhook_invalid")
    try:
        verified = verify_webhook_signature(
            timestamp=timestamp,
            body=body,
            signature=signature,
            declared_key_version=declared_key_version,
            current_key_version=config.payment_webhook_key_version,
            current_secret=config.payment_webhook_secret,
            previous_key_version=config.payment_webhook_previous_key_version,
            previous_secret=config.payment_webhook_previous_secret,
        )
        event = _event(body)
    except PaymentCryptoError:
        raise ApiError("payment.webhook_invalid") from None
    raw_sha256 = hashlib.sha256(body).hexdigest()
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        existing = connection.execute(
            "SELECT * FROM payment_webhook_receipts WHERE provider=? AND provider_event_id=?",
            (provider, event["event_id"]),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["raw_body_sha256"]), raw_sha256):
                connection.execute(
                    """
                    UPDATE payment_webhook_receipts
                    SET status='conflict',safe_error_code='payment.state_conflict',
                      processing_owner=NULL,processing_lease_until=NULL
                    WHERE receipt_id=?
                    """,
                    (existing["receipt_id"],),
                )
                connection.commit()
                raise ApiError("payment.webhook_conflict")
            response = PaymentWebhookReceiptResponse(
                receipt_id=str(existing["receipt_id"]),
                status=existing["status"],
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        receipt_id = _id("pwr_")
        object_reference = _object_reference(provider, receipt_id)
        encrypted = encrypt_receipt_body(
            config.payment_receipt_encryption_secret,
            provider=provider,
            receipt_id=receipt_id,
            key_version=config.payment_receipt_encryption_key_version,
            body=body,
        )
        _write_receipt_object(config, object_reference, encrypted)
        now = _utcnow()
        connection.execute(
            """
            INSERT INTO payment_webhook_receipts(
              receipt_id,provider,provider_event_id,provider_event_version,
              provider_occurred_at,raw_body_object_reference,raw_body_sha256,
              signature_key_version,encryption_key_version,status,processing_owner,
              processing_lease_until,safe_error_code,received_at,processed_at)
            VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,NULL,?,NULL)
            """,
            (
                receipt_id,
                provider,
                event["event_id"],
                event["event_version"],
                event["occurred_at"],
                object_reference,
                raw_sha256,
                verified.key_version,
                config.payment_receipt_encryption_key_version,
                _db_time(now),
            ),
        )
        connection.commit()
        return PaymentWebhookReceiptResponse(
            receipt_id=receipt_id,
            status="pending",
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, OSError, KeyError, TypeError, ValueError, PaymentCryptoError):
        _rollback(connection)
        raise ApiError("payment.unavailable") from None
    finally:
        connection.close()


def process_payment_receipts(config: AppConfig, *, worker_id: str, limit: int = 50) -> int:
    """通过有界 lease 处理待办 receipt；每个状态和商业效果在同一数据库事务提交。"""

    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,96}", worker_id) or not 1 <= limit <= 100:
        raise ApiError("payment.unavailable")
    processed = 0
    for _ in range(limit):
        claimed = _claim_receipt(config, worker_id)
        if claimed is None:
            break
        try:
            _process_claimed(config, worker_id, claimed)
        except ApiError:
            _fail_claimed(config, worker_id, str(claimed["receipt_id"]))
        processed += 1
    return processed


def dispatch_commercial_effects(config: AppConfig, *, limit: int = 50) -> int:
    """独立收敛商业 outbox；发送失败不会回滚 capture、refund 或 dunning 事实。"""

    if not 1 <= limit <= 100:
        raise ApiError("payment.unavailable")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT commercial_effect_id FROM commercial_effects
            WHERE outbox_status='pending' ORDER BY created_at,commercial_effect_id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        now = _db_time(_utcnow())
        for row in rows:
            connection.execute(
                """
                UPDATE commercial_effects
                SET outbox_status='completed',completed_at=?
                WHERE commercial_effect_id=? AND outbox_status='pending'
                """,
                (now, row["commercial_effect_id"]),
            )
        connection.commit()
        return len(rows)
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("payment.unavailable") from None
    finally:
        connection.close()


def request_refund(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    payload: RefundCreateRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> RefundResponse:
    """锁内计算剩余可退金额并幂等创建客户退款申请。"""

    if _ORDER_ID_RE.fullmatch(payload.order_id) is None:
        raise ApiError("refund.request_invalid")
    _validate_idempotency(idempotency_key, "refund.request_invalid")
    request_sha256 = _intent_hash("refund", payload.model_dump(mode="json"))
    idempotency_sha256 = _sha256(idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_finance(actor)
        _require_schema(connection)
        replay = connection.execute(
            "SELECT * FROM refunds WHERE workspace_id=? AND idempotency_sha256=?",
            (actor.workspace_id, idempotency_sha256),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("refund.idempotency_conflict")
            result = RefundResponse(
                refund=_refund(replay),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        order = connection.execute(
            "SELECT * FROM subscription_orders WHERE order_id=? AND workspace_id=?",
            (payload.order_id, actor.workspace_id),
        ).fetchone()
        if order is None:
            raise ApiError("refund.not_found")
        intent = connection.execute(
            """
            SELECT * FROM payment_intents
            WHERE order_id=? AND status='succeeded'
            ORDER BY provider_event_version DESC,created_at DESC LIMIT 1
            """,
            (payload.order_id,),
        ).fetchone()
        if intent is None or order["status"] not in {
            "paid",
            "fulfilled",
            "partially_refunded",
        }:
            raise ApiError("refund.conflict")
        reserved = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(amount_minor),0) FROM refunds
                WHERE order_id=? AND status IN('requested','processing','succeeded')
                """,
                (payload.order_id,),
            ).fetchone()[0]
        )
        if reserved + payload.amount_minor > int(order["captured_amount_minor"]):
            raise ApiError("refund.amount_exceeded")
        now = _utcnow()
        refund_id = _id("rfd_")
        connection.execute(
            """
            INSERT INTO refunds(
              refund_id,order_id,payment_intent_id,workspace_id,tenant_id,amount_minor,
              currency,reason_code,status,idempotency_sha256,request_sha256,
              approved_by_operator_id,decision_reason_code,state_revision,
              requested_by_membership_id,requested_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,'requested',?,?,NULL,NULL,1,?,?,?)
            """,
            (
                refund_id,
                order["order_id"],
                intent["payment_intent_id"],
                actor.workspace_id,
                actor.tenant_id,
                payload.amount_minor,
                order["currency"],
                payload.reason_code,
                idempotency_sha256,
                request_sha256,
                actor.membership_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        row = connection.execute("SELECT * FROM refunds WHERE refund_id=?", (refund_id,)).fetchone()
        if row is None:
            raise ApiError("payment.unavailable")
        result = RefundResponse(
            refund=_refund(row), request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("payment.unavailable") from None
    finally:
        connection.close()


def review_refund(
    config: AppConfig,
    *,
    platform_session_token: str,
    csrf_token: str,
    refund_id: str,
    expected_revision: int,
    payload: PlatformRefundDecisionRequest,
    request_id: str,
    trace_id: str,
) -> RefundResponse:
    """平台 billing 权限在持锁事务内动态恢复，并推进申请到处理或拒绝。"""

    if _REFUND_ID_RE.fullmatch(refund_id) is None or expected_revision < 1:
        raise ApiError("refund.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=platform_session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_platform_billing(actor)
        _require_schema(connection)
        row = connection.execute("SELECT * FROM refunds WHERE refund_id=?", (refund_id,)).fetchone()
        if row is None:
            raise ApiError("refund.not_found")
        if int(row["state_revision"]) != expected_revision or row["status"] != "requested":
            raise ApiError("refund.conflict")
        next_status = "processing" if payload.decision == "approve" else "rejected"
        now = _utcnow()
        updated = connection.execute(
            """
            UPDATE refunds
            SET status=?,approved_by_operator_id=?,decision_reason_code=?,
              state_revision=state_revision+1,updated_at=?
            WHERE refund_id=? AND state_revision=? AND status='requested'
            """,
            (
                next_status,
                actor.operator_id,
                payload.reason_code,
                _db_time(now),
                refund_id,
                expected_revision,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("refund.conflict")
        reviewed = connection.execute(
            "SELECT * FROM refunds WHERE refund_id=?", (refund_id,)
        ).fetchone()
        if reviewed is None:
            raise ApiError("payment.unavailable")
        result = RefundResponse(
            refund=_refund(reviewed), request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("payment.unavailable") from None
    finally:
        connection.close()


def list_platform_refunds(
    config: AppConfig,
    *,
    platform_session_token: str,
    status: str,
    request_id: str,
    trace_id: str,
) -> PlatformRefundQueueResponse:
    """平台 billing 只读取固定状态队列和脱敏退款摘要。"""

    if status not in {"requested", "processing", "succeeded", "failed", "rejected"}:
        raise ApiError("refund.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=platform_session_token, touch=False
        )
        if "platform.billing.read" not in actor.permissions:
            raise ApiError("platform.permission_denied")
        _require_schema(connection)
        rows = connection.execute(
            """
            SELECT * FROM refunds WHERE status=?
            ORDER BY requested_at,refund_id LIMIT 100
            """,
            (status,),
        ).fetchall()
        response = PlatformRefundQueueResponse(
            refunds=tuple(_refund(row) for row in rows),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("payment.unavailable") from None
    finally:
        connection.close()


def _claim_receipt(config: AppConfig, worker_id: str) -> sqlite3.Row | None:
    """短事务领取一条到期 receipt，网络或对象读取不占用数据库写锁。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        row = connection.execute(
            """
            SELECT * FROM payment_webhook_receipts
            WHERE status='pending'
              OR(status='processing' AND processing_lease_until<=?)
            ORDER BY received_at,receipt_id LIMIT 1
            """,
            (_db_time(now),),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        connection.execute(
            """
            UPDATE payment_webhook_receipts
            SET status='processing',processing_owner=?,processing_lease_until=?,safe_error_code=NULL
            WHERE receipt_id=?
            """,
            (worker_id, _db_time(now + timedelta(seconds=60)), row["receipt_id"]),
        )
        claimed = connection.execute(
            "SELECT * FROM payment_webhook_receipts WHERE receipt_id=?", (row["receipt_id"],)
        ).fetchone()
        connection.commit()
        return claimed
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("payment.unavailable") from None
    finally:
        connection.close()


def _process_claimed(config: AppConfig, worker_id: str, receipt: sqlite3.Row) -> None:
    """读取隔离密文对象并在一个事务内提交 attempt、状态和唯一商业效果。"""

    if config.payment_receipt_encryption_secret is None:
        raise ApiError("payment.unavailable")
    try:
        envelope = _read_receipt_object(config, str(receipt["raw_body_object_reference"]))
        body = decrypt_receipt_body(
            config.payment_receipt_encryption_secret,
            provider=str(receipt["provider"]),
            receipt_id=str(receipt["receipt_id"]),
            key_version=int(receipt["encryption_key_version"]),
            envelope=envelope,
        )
        if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), receipt["raw_body_sha256"]):
            raise PaymentCryptoError("payment receipt hash invalid")
        event = _event(body)
    except (OSError, PaymentCryptoError, KeyError, TypeError, ValueError):
        raise ApiError("payment.unavailable") from None
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        locked = connection.execute(
            """
            SELECT * FROM payment_webhook_receipts
            WHERE receipt_id=? AND status='processing' AND processing_owner=?
            """,
            (receipt["receipt_id"], worker_id),
        ).fetchone()
        if locked is None:
            raise ApiError("payment.unavailable")
        if event["event_type"].startswith("refund."):
            _apply_refund_event(config, connection, locked, event)
        else:
            _apply_payment_event(config, connection, locked, event)
        connection.execute(
            """
            UPDATE payment_webhook_receipts
            SET status='completed',processing_owner=NULL,processing_lease_until=NULL,
              safe_error_code=NULL,processed_at=?
            WHERE receipt_id=? AND processing_owner=?
            """,
            (_db_time(_utcnow()), receipt["receipt_id"], worker_id),
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("payment.unavailable") from None
    finally:
        connection.close()


def _apply_payment_event(
    config: AppConfig,
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    event: dict[str, Any],
) -> None:
    """按 provider version 单调推进意图；旧事件只追加 ignored attempt。"""

    intent = connection.execute(
        "SELECT * FROM payment_intents WHERE payment_intent_id=? AND provider=?",
        (event["payment_intent_id"], receipt["provider"]),
    ).fetchone()
    if intent is None or not _event_matches_intent(config, intent, event):
        raise ApiError("payment.webhook_invalid")
    current_version = int(intent["provider_event_version"])
    stale = int(event["event_version"]) <= current_version
    provider_status = "ignored_stale" if stale else _EVENT_TO_STATUS[event["event_type"]]
    _insert_attempt(connection, receipt, intent, event, provider_status)
    if stale:
        return
    next_status = _EVENT_TO_STATUS[event["event_type"]]
    connection.execute(
        """
        UPDATE payment_intents
        SET status=?,provider_event_version=?,provider_occurred_at=?,
          state_revision=state_revision+1,updated_at=?
        WHERE payment_intent_id=?
        """,
        (
            next_status,
            event["event_version"],
            event["occurred_at"],
            _db_time(_utcnow()),
            intent["payment_intent_id"],
        ),
    )
    if next_status == "succeeded":
        _capture_order(connection, receipt, intent)
    elif next_status == "failed":
        _mark_past_due(connection, receipt, intent)


def _apply_refund_event(
    config: AppConfig,
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    event: dict[str, Any],
) -> None:
    """退款回调仅能推进平台已审核的 processing 申请，并锁内累计实退金额。"""

    refund = connection.execute(
        "SELECT * FROM refunds WHERE refund_id=?", (event["refund_id"],)
    ).fetchone()
    intent = connection.execute(
        "SELECT * FROM payment_intents WHERE payment_intent_id=? AND provider=?",
        (event["payment_intent_id"], receipt["provider"]),
    ).fetchone()
    if (
        refund is None
        or intent is None
        or refund["payment_intent_id"] != intent["payment_intent_id"]
        or refund["status"] != "processing"
        or int(refund["amount_minor"]) != int(event["amount_minor"])
        or refund["currency"] != event["currency"]
        or not _event_matches_intent(
            config, intent, event, amount_override=int(refund["amount_minor"])
        )
    ):
        raise ApiError("payment.webhook_invalid")
    status = "succeeded" if event["event_type"] == "refund.succeeded" else "failed"
    _insert_attempt(connection, receipt, intent, event, status)
    now = _utcnow()
    connection.execute(
        """
        UPDATE refunds SET status=?,state_revision=state_revision+1,updated_at=?
        WHERE refund_id=? AND status='processing'
        """,
        (status, _db_time(now), refund["refund_id"]),
    )
    if status != "succeeded":
        return
    effect_key = f"refund:{refund['refund_id']}"
    if connection.execute(
        "SELECT 1 FROM commercial_effects WHERE effect_key=?", (effect_key,)
    ).fetchone():
        return
    order = connection.execute(
        "SELECT * FROM subscription_orders WHERE order_id=?", (refund["order_id"],)
    ).fetchone()
    if order is None:
        raise ApiError("payment.unavailable")
    refunded = int(order["refunded_amount_minor"]) + int(refund["amount_minor"])
    if refunded > int(order["captured_amount_minor"]):
        raise ApiError("refund.amount_exceeded")
    order_status = (
        "refunded" if refunded == int(order["captured_amount_minor"]) else "partially_refunded"
    )
    connection.execute(
        """
        UPDATE subscription_orders
        SET refunded_amount_minor=?,status=?,state_revision=state_revision+1,updated_at=?
        WHERE order_id=?
        """,
        (refunded, order_status, _db_time(now), order["order_id"]),
    )
    _insert_effect(
        connection,
        effect_key=effect_key,
        source_type="refund",
        source_id=str(refund["refund_id"]),
        order=order,
        effect_type="refund_ledger",
        amount_minor=int(refund["amount_minor"]),
        status="completed",
        now=now,
    )


def _capture_order(
    connection: sqlite3.Connection, receipt: sqlite3.Row, intent: sqlite3.Row
) -> None:
    """capture effect 原子更新订单、订阅和 entitlement snapshot，重放不会复制任何事实。"""

    effect_key = f"capture:{intent['payment_intent_id']}"
    if connection.execute(
        "SELECT 1 FROM commercial_effects WHERE effect_key=?", (effect_key,)
    ).fetchone():
        return
    order = connection.execute(
        "SELECT * FROM subscription_orders WHERE order_id=?", (intent["order_id"],)
    ).fetchone()
    if order is None or int(order["amount_minor"]) != int(intent["amount_minor"]):
        raise ApiError("payment.unavailable")
    now = _utcnow()
    connection.execute(
        """
        UPDATE subscription_orders
        SET status='fulfilled',captured_amount_minor=amount_minor,
          state_revision=state_revision+1,updated_at=?
        WHERE order_id=?
        """,
        (_db_time(now), order["order_id"]),
    )
    subscription = connection.execute(
        "SELECT * FROM workspace_subscriptions WHERE subscription_id=?",
        (order["subscription_id"],),
    ).fetchone()
    plan = connection.execute(
        "SELECT * FROM saas_plan_versions WHERE plan_version_id=?", (order["plan_version_id"],)
    ).fetchone()
    if subscription is None or plan is None:
        raise ApiError("payment.unavailable")
    period_end = _period_end(now, str(plan["billing_period"]))
    connection.execute(
        """
        UPDATE workspace_subscriptions
        SET plan_version_id=?,pending_plan_version_id=NULL,status='active',
          current_period_started_at=?,current_period_ends_at=?,pending_effective_at=NULL,
          state_revision=state_revision+1,updated_at=?
        WHERE subscription_id=?
        """,
        (
            order["plan_version_id"],
            _db_time(now),
            _db_time(period_end) if period_end else None,
            _db_time(now),
            order["subscription_id"],
        ),
    )
    snapshot_version = int(
        connection.execute(
            "SELECT COALESCE(MAX(snapshot_version),0)+1 FROM entitlement_snapshots "
            "WHERE subscription_id=?",
            (order["subscription_id"],),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO entitlement_snapshots(
          entitlement_snapshot_id,workspace_id,tenant_id,subscription_id,plan_version_id,
          source_order_record_id,snapshot_version,entitlements_json,entitlements_sha256,
          effective_from,effective_until,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("ent_"),
            order["workspace_id"],
            order["tenant_id"],
            order["subscription_id"],
            order["plan_version_id"],
            order["source_order_record_id"],
            snapshot_version,
            plan["entitlements_json"],
            plan["entitlements_sha256"],
            _db_time(now),
            _db_time(period_end) if period_end else None,
            _db_time(now),
        ),
    )
    connection.execute(
        """
        UPDATE dunning_cases SET status='recovered',next_action_at=NULL,updated_at=?
        WHERE order_id=? AND status='open'
        """,
        (_db_time(now), order["order_id"]),
    )
    _insert_effect(
        connection,
        effect_key=effect_key,
        source_type="receipt",
        source_id=str(receipt["receipt_id"]),
        order=order,
        effect_type="capture_fulfilled",
        amount_minor=int(order["amount_minor"]),
        status="completed",
        now=now,
    )


def _mark_past_due(
    connection: sqlite3.Connection, receipt: sqlite3.Row, intent: sqlite3.Row
) -> None:
    """失败支付独立创建催收 outbox；通知结果不能反向修改支付 attempt。"""

    order = connection.execute(
        "SELECT * FROM subscription_orders WHERE order_id=?", (intent["order_id"],)
    ).fetchone()
    if order is None or order["status"] not in {"pending_payment", "past_due"}:
        return
    now = _utcnow()
    if order["status"] != "past_due":
        connection.execute(
            """
            UPDATE subscription_orders SET status='past_due',state_revision=state_revision+1,
              updated_at=? WHERE order_id=?
            """,
            (_db_time(now), order["order_id"]),
        )
        connection.execute(
            """
            UPDATE workspace_subscriptions SET status='past_due',
              state_revision=state_revision+1,updated_at=? WHERE subscription_id=?
            """,
            (_db_time(now), order["subscription_id"]),
        )
    case = connection.execute(
        "SELECT * FROM dunning_cases WHERE order_id=?", (order["order_id"],)
    ).fetchone()
    if case is None:
        case_id = _id("dun_")
        template = "payment-failed-v1"
        connection.execute(
            """
            INSERT INTO dunning_cases(
              dunning_case_id,order_id,workspace_id,tenant_id,status,cycle_number,
              template_reference,next_action_at,grace_policy_reference,created_at,updated_at)
            VALUES(?,?,?,?,'open',1,?,?,?, ?,?)
            """,
            (
                case_id,
                order["order_id"],
                order["workspace_id"],
                order["tenant_id"],
                template,
                _db_time(now + timedelta(days=3)),
                "commercial-grace-v1",
                _db_time(now),
                _db_time(now),
            ),
        )
        connection.execute(
            """
            INSERT INTO dunning_attempts(
              dunning_attempt_id,dunning_case_id,attempt_number,action_code,
              template_reference,result_code,occurred_at)
            VALUES(?,?,1,'notify',?,'pending',?)
            """,
            (_id("dua_"), case_id, template, _db_time(now)),
        )
        _insert_effect(
            connection,
            effect_key=f"dunning:{order['order_id']}:1",
            source_type="receipt",
            source_id=str(receipt["receipt_id"]),
            order=order,
            effect_type="dunning_scheduled",
            amount_minor=0,
            status="pending",
            now=now,
        )


def _insert_attempt(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    intent: sqlite3.Row,
    event: dict[str, Any],
    provider_status: str,
) -> None:
    """receipt 唯一约束保证每个已验证渠道事件只追加一个 attempt。"""

    connection.execute(
        """
        INSERT INTO payment_attempts(
          payment_attempt_id,payment_intent_id,receipt_id,provider_status,
          provider_event_version,provider_occurred_at,safe_error_code,occurred_at,created_at)
        VALUES(?,?,?,?,?,?,NULL,?,?)
        """,
        (
            _id("pat_"),
            intent["payment_intent_id"],
            receipt["receipt_id"],
            provider_status,
            event["event_version"],
            event["occurred_at"],
            event["occurred_at"],
            _db_time(_utcnow()),
        ),
    )


def _insert_effect(
    connection: sqlite3.Connection,
    *,
    effect_key: str,
    source_type: str,
    source_id: str,
    order: sqlite3.Row,
    effect_type: str,
    amount_minor: int,
    status: str,
    now: datetime,
) -> None:
    """商业效果与业务状态同事务写入，completed 与 pending 都由数据库唯一键防重。"""

    connection.execute(
        """
        INSERT INTO commercial_effects(
          commercial_effect_id,effect_key,source_type,source_id,order_id,workspace_id,
          tenant_id,effect_type,amount_minor,currency,outbox_status,created_at,completed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("cef_"),
            effect_key,
            source_type,
            source_id,
            order["order_id"],
            order["workspace_id"],
            order["tenant_id"],
            effect_type,
            amount_minor,
            order["currency"],
            status,
            _db_time(now),
            _db_time(now) if status != "pending" else None,
        ),
    )


def _materialize_orders(connection: sqlite3.Connection, workspace_id: str) -> None:
    """把 0090 待支付来源一对一提升为 0095 正式订单，不改写已发布迁移事实。"""

    rows = connection.execute(
        """
        SELECT source.* FROM subscription_order_records source
        LEFT JOIN subscription_orders target
          ON target.source_order_record_id=source.order_record_id
        WHERE source.workspace_id=? AND source.status='pending_payment'
          AND target.order_id IS NULL
        ORDER BY source.created_at,source.order_record_id
        """,
        (workspace_id,),
    ).fetchall()
    for source in rows:
        canonical = _intent_hash(
            "subscription_order",
            {
                "source_order_record_id": source["order_record_id"],
                "subscription_id": source["subscription_id"],
                "plan_version_id": source["plan_version_id"],
                "order_kind": source["order_kind"],
                "currency": source["currency"],
                "amount_minor": int(source["amount_minor"]),
            },
        )
        connection.execute(
            """
            INSERT INTO subscription_orders(
              order_id,source_order_record_id,workspace_id,tenant_id,subscription_id,
              plan_version_id,order_kind,status,currency,amount_minor,captured_amount_minor,
              refunded_amount_minor,canonical_intent_sha256,state_revision,
              created_by_membership_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'pending_payment',?,?,0,0,?,1,?,?,?)
            """,
            (
                _id("ord_"),
                source["order_record_id"],
                source["workspace_id"],
                source["tenant_id"],
                source["subscription_id"],
                source["plan_version_id"],
                source["order_kind"],
                source["currency"],
                int(source["amount_minor"]),
                canonical,
                source["created_by_membership_id"],
                source["created_at"],
                source["created_at"],
            ),
        )


def _orders_response(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> BillingOrdersResponse:
    """从同一事务快照构造四组严格摘要，内部 hash 和对象引用不进入 DTO。"""

    orders = connection.execute(
        "SELECT * FROM subscription_orders WHERE workspace_id=? ORDER BY created_at DESC,order_id",
        (workspace_id,),
    ).fetchall()
    intents = connection.execute(
        "SELECT * FROM payment_intents WHERE workspace_id=? "
        "ORDER BY created_at DESC,payment_intent_id",
        (workspace_id,),
    ).fetchall()
    refunds = connection.execute(
        "SELECT * FROM refunds WHERE workspace_id=? ORDER BY requested_at DESC,refund_id",
        (workspace_id,),
    ).fetchall()
    cases = connection.execute(
        "SELECT * FROM dunning_cases WHERE workspace_id=? ORDER BY created_at DESC,dunning_case_id",
        (workspace_id,),
    ).fetchall()
    return BillingOrdersResponse(
        orders=tuple(_order(row) for row in orders),
        payment_intents=tuple(_payment_intent(row) for row in intents),
        refunds=tuple(_refund(row) for row in refunds),
        dunning_cases=tuple(
            DunningCaseInfo(
                dunning_case_id=str(row["dunning_case_id"]),
                order_id=str(row["order_id"]),
                status=row["status"],
                cycle_number=int(row["cycle_number"]),
                template_reference=str(row["template_reference"]),
                next_action_at=_optional_time(row["next_action_at"]),
            )
            for row in cases
        ),
        request_id=request_id,
        trace_id=trace_id,
    )


def _order(row: sqlite3.Row) -> SubscriptionOrderInfo:
    """把订单数据库行收窄为公共字段。"""

    return SubscriptionOrderInfo(
        order_id=str(row["order_id"]),
        subscription_id=str(row["subscription_id"]),
        plan_version_id=str(row["plan_version_id"]),
        order_kind=row["order_kind"],
        status=row["status"],
        amount_minor=int(row["amount_minor"]),
        captured_amount_minor=int(row["captured_amount_minor"]),
        refunded_amount_minor=int(row["refunded_amount_minor"]),
        currency=row["currency"],
        state_revision=int(row["state_revision"]),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _payment_intent(row: sqlite3.Row) -> PaymentIntentInfo:
    """把支付意图数据库行收窄，明确排除 token HMAC 与请求 hash。"""

    return PaymentIntentInfo(
        payment_intent_id=str(row["payment_intent_id"]),
        order_id=str(row["order_id"]),
        provider=row["provider"],
        status=row["status"],
        amount_minor=int(row["amount_minor"]),
        currency=row["currency"],
        state_revision=int(row["state_revision"]),
        expires_at=_parse_time(row["expires_at"]),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _refund(row: sqlite3.Row) -> RefundInfo:
    """把退款数据库行收窄，隐藏审核 operator 与幂等摘要。"""

    return RefundInfo(
        refund_id=str(row["refund_id"]),
        order_id=str(row["order_id"]),
        payment_intent_id=str(row["payment_intent_id"]),
        amount_minor=int(row["amount_minor"]),
        currency=row["currency"],
        reason_code=row["reason_code"],
        status=row["status"],
        state_revision=int(row["state_revision"]),
        requested_at=_parse_time(row["requested_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _event(body: bytes) -> dict[str, Any]:
    """验签后才解析严格事件合同，并拒绝支付敏感字段或未知扩展。"""

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PaymentCryptoError("payment event invalid") from None
    if type(value) is not dict or type(value.get("event_type")) is not str:
        raise PaymentCryptoError("payment event invalid")
    refund_event = value["event_type"] in {"refund.succeeded", "refund.failed"}
    if set(value) != (_REFUND_EVENT_KEYS if refund_event else _PAYMENT_EVENT_KEYS):
        raise PaymentCryptoError("payment event invalid")
    if not refund_event and value["event_type"] not in _EVENT_TO_STATUS:
        raise PaymentCryptoError("payment event invalid")
    if (
        type(value["event_id"]) is not str
        or _EVENT_ID_RE.fullmatch(value["event_id"]) is None
        or type(value["event_version"]) is not int
        or value["event_version"] < 1
        or type(value["payment_intent_id"]) is not str
        or _INTENT_ID_RE.fullmatch(value["payment_intent_id"]) is None
        or type(value["client_token"]) is not str
        or not re.fullmatch(r"ptok_[0-9a-f]{64}", value["client_token"])
        or type(value["amount_minor"]) is not int
        or not 1 <= value["amount_minor"] <= 1_000_000_000_000
        or value["currency"] not in {"CNY", "USD"}
    ):
        raise PaymentCryptoError("payment event invalid")
    if refund_event and (
        type(value["refund_id"]) is not str
        or _REFUND_ID_RE.fullmatch(value["refund_id"]) is None
    ):
        raise PaymentCryptoError("payment event invalid")
    occurred = _parse_time(value["occurred_at"])
    value["occurred_at"] = _db_time(occurred)
    return value


def _event_matches_intent(
    config: AppConfig,
    intent: sqlite3.Row,
    event: dict[str, Any],
    *,
    amount_override: int | None = None,
) -> bool:
    """事件必须同时绑定意图、一次性 token、原币金额和币种。"""

    expected_amount = int(intent["amount_minor"]) if amount_override is None else amount_override
    try:
        token_matches = hmac.compare_digest(
            str(intent["client_token_hmac"]),
            _client_token_hmac(config, str(intent["payment_intent_id"]), event["client_token"]),
        )
    except (ApiError, TypeError):
        return False
    return (
        token_matches
        and int(event["amount_minor"]) == expected_amount
        and event["currency"] == intent["currency"]
    )

def _client_token_hmac(config: AppConfig, intent_id: str, token: str) -> str:
    """使用 receipt 根密钥的显式 token 子域生成 HMAC，数据库不可恢复原 token。"""

    if config.payment_receipt_encryption_secret is None:
        raise ApiError("payment.unavailable")
    return _client_token_hmac_from_secret(
        config.payment_receipt_encryption_secret, intent_id, token
    )


def _client_token_hmac_from_secret(secret: str | None, intent_id: str, token: str) -> str:
    """内部 token HMAC 编码绑定意图 ID，防止跨意图替换。"""

    if secret is None:
        raise TypeError("secret unavailable")
    return hmac.new(
        secret.encode("utf-8"),
        f"payment-client-token-v1\0{intent_id}\0{token}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _write_receipt_object(config: AppConfig, reference: str, payload: bytes) -> None:
    """以独占创建写入加密对象；既有路径绝不覆盖。"""

    path = _receipt_path(config, reference)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_receipt_object(config: AppConfig, reference: str) -> bytes:
    """读取前再次验证路径 owner，并施加密文大小上限。"""

    path = _receipt_path(config, reference)
    if not path.is_file() or path.is_symlink():
        raise OSError("receipt object unavailable")
    payload = path.read_bytes()
    if not payload or len(payload) > config.payment_webhook_max_body_bytes * 2 + 256:
        raise OSError("receipt object invalid")
    return payload


def _receipt_path(config: AppConfig, reference: str) -> Path:
    """对象引用只允许两层安全片段，规范化后必须仍位于部署根目录内。"""

    if re.fullmatch(r"(?:mockpay|cloudpay)/[0-9a-f]{2}/pwr_[0-9a-f]{32}\.pay", reference) is None:
        raise OSError("receipt object reference invalid")
    root = Path(config.payment_receipt_storage_root).resolve()
    candidate = (root / reference).resolve()
    if root not in candidate.parents:
        raise OSError("receipt object outside root")
    return candidate


def _object_reference(provider: str, receipt_id: str) -> str:
    """对象名只由随机 receipt 派生，不包含 event id、Workspace 或客户字段。"""

    return f"{provider}/{receipt_id[4:6]}/{receipt_id}.pay"


def _fail_claimed(config: AppConfig, worker_id: str, receipt_id: str) -> None:
    """处理失败只写固定安全错误码，并释放 lease 供运维审查。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE payment_webhook_receipts
            SET status='failed',processing_owner=NULL,processing_lease_until=NULL,
              safe_error_code='payment.storage_error',processed_at=?
            WHERE receipt_id=? AND status='processing' AND processing_owner=?
            """,
            (_db_time(_utcnow()), receipt_id, worker_id),
        )
        connection.commit()
    except sqlite3.Error:
        _rollback(connection)
    finally:
        connection.close()


def _require_finance(actor: WorkspaceSessionContext) -> None:
    """订单、支付和退款客户命令只允许 owner 或 finance_admin。"""

    if actor.membership_role not in _FINANCE_ROLES:
        raise ApiError("workspace_session.permission_denied")


def _require_platform_billing(actor: PlatformContext) -> None:
    """退款审核要求事务内最新 platform.billing.adjust 权限。"""

    if "platform.billing.adjust" not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _require_schema(connection: sqlite3.Connection) -> None:
    """业务入口要求 0095 八张表完整，代码回退不得对缺失 receipt 表返回成功。"""

    count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
              'subscription_orders','payment_intents','payment_webhook_receipts',
              'payment_attempts','refunds','dunning_cases','dunning_attempts',
              'commercial_effects')
            """
        ).fetchone()[0]
    )
    if count != 8:
        raise ApiError("payment.unavailable")


def _validate_idempotency(value: str, code: str) -> None:
    """幂等键只在内存校验，持久化前统一哈希。"""

    if type(value) is not str or _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError(code)


def _intent_hash(operation: str, payload: object) -> str:
    """规范 JSON 绑定操作类型，防止同键跨命令复用。"""

    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return _sha256(f"{operation}\0{canonical}")


def _period_end(start: datetime, period: str) -> datetime | None:
    """沿用冻结账期长度；none 套餐不产生截止时间。"""

    if period == "monthly":
        return start + timedelta(days=30)
    if period == "yearly":
        return start + timedelta(days=365)
    return None


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接商业权威数据库，缺失 DSN 固定失败关闭。"""

    if not config.database_url:
        raise ApiError("payment.unavailable")
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("payment.unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    """最佳努力回滚，保留原始安全业务错误。"""

    try:
        connection.rollback()
    except sqlite3.Error:
        return


def _utcnow() -> datetime:
    """集中提供可冻结 UTC 时钟。"""

    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    """数据库时间统一写为 UTC Z 格式。"""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    """拒绝无时区和非 UTC 可转换时间。"""

    if type(value) is not str:
        raise ValueError("time invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("time invalid")
    return parsed.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    """只把数据库 NULL 映射为 None。"""

    return None if value is None else _parse_time(value)


def _id(prefix: str) -> str:
    """生成带域前缀的不可预测本地标识。"""

    return prefix + uuid4().hex


def _sha256(value: str) -> str:
    """对规范 UTF-8 文本生成稳定摘要。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
