"""控制面和数据面均可复用的 Webhook 事件投递内核。

该模块只处理已注册事件的签名、HTTP 投递和 delivery/attempt 审计，不负责 Endpoint
配置管理或 API DTO。所有错误摘要均为固定枚举，禁止记录响应正文、签名密钥或 Header。
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from metahub_shared.db.connection import connect_database
from metahub_shared.db.security import decrypt_secret
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.webhooks import (
    WebhookEvent,
    canonical_json_bytes,
    validate_webhook_payload,
    webhook_signature_header,
)


def dispatch_runtime_webhook_event(config: AppConfig, event: WebhookEvent) -> int:
    """Durably converge the event's frozen delivery intents and return their count.

    The first call freezes every matching JSON Endpoint in one transaction before any
    network request. A repeated call resumes existing ``pending`` intents from their
    persisted attempt count; terminal intents are only read and are never sent again.
    Returning therefore means that no frozen intent remains pending.
    """

    # WebhookEvent can be constructed by internal callers, so validate the complete
    # envelope again before either persisting or sending it.
    body = canonical_json_bytes(validate_webhook_payload(event.to_payload()))
    connection = connect_database(config.database_url)
    try:
        deliveries = _freeze_delivery_intents(connection, event, body)
        for delivery in deliveries:
            status = str(delivery["status"])
            if status in {"success", "failed"}:
                continue
            if status != "pending":
                raise sqlite3.IntegrityError("webhook_delivery_status_invalid")
            _resume_pending_delivery(config, connection, delivery)

        remaining = connection.execute(
            """
            SELECT COUNT(*) FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
              AND status='pending'
            """,
            (event.tenant_id, event.event_id),
        ).fetchone()[0]
        if int(remaining) != 0:
            raise sqlite3.IntegrityError("webhook_delivery_pending")
        return len(deliveries)
    finally:
        # Network, decryption and database failures must all release the long-running
        # worker's SQLite handle.
        connection.close()


def _freeze_delivery_intents(
    connection: sqlite3.Connection,
    event: WebhookEvent,
    body: bytes,
) -> list[sqlite3.Row]:
    """Freeze the original Endpoint snapshot before the first network request."""

    body_text = body.decode("utf-8")
    try:
        connection.execute("BEGIN IMMEDIATE")
        deliveries = _load_original_deliveries(connection, event.tenant_id, event.event_id)
        if not deliveries and event.event_type != "webhook.delivery_failed":
            endpoints = connection.execute(
                """
                SELECT * FROM webhook_endpoints
                WHERE tenant_id=? AND status='active' AND payload_format='json'
                ORDER BY created_at ASC, webhook_endpoint_id ASC
                """,
                (event.tenant_id,),
            ).fetchall()
            now = _db_time()
            for endpoint in endpoints:
                if event.event_type not in _event_types(endpoint["event_types_json"]):
                    continue
                connection.execute(
                    """
                    INSERT INTO webhook_deliveries(
                      webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
                      event_timestamp,payload_json,status,attempt_count,max_attempts,
                      secret_version,created_at,updated_at,payload_format,wire_body_text,
                      security_event_rate_limit_per_minute
                    ) VALUES(?,?,?,?,?,?,?,'pending',0,?,?,?,?, 'json',?,?) ON CONFLICT DO NOTHING
                    """,
                    (
                        f"whd_{uuid4().hex}",
                        event.tenant_id,
                        endpoint["webhook_endpoint_id"],
                        event.event_id,
                        event.event_type,
                        event.timestamp,
                        body_text,
                        int(endpoint["max_attempts"]),
                        int(endpoint["secret_version"]),
                        now,
                        now,
                        body_text,
                        int(endpoint["security_event_rate_limit_per_minute"]),
                    ),
                )
            deliveries = _load_original_deliveries(connection, event.tenant_id, event.event_id)

        for delivery in deliveries:
            if (
                str(delivery["event_type"]) != event.event_type
                or str(delivery["payload_format"]) != "json"
                or str(delivery["payload_json"]) != body_text
                or str(delivery["wire_body_text"]) != body_text
            ):
                raise sqlite3.IntegrityError("webhook_event_id_conflict")
        connection.commit()
        return deliveries
    except Exception:
        connection.rollback()
        raise


def _load_original_deliveries(
    connection: sqlite3.Connection,
    tenant_id: str,
    event_id: str,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        ORDER BY created_at ASC, webhook_delivery_id ASC
        """,
        (tenant_id, event_id),
    ).fetchall()


def _resume_pending_delivery(
    config: AppConfig,
    connection: sqlite3.Connection,
    original: sqlite3.Row,
) -> None:
    """Resume one pending intent from its persisted body and attempt count."""

    delivery_id = str(original["webhook_delivery_id"])
    tenant_id = str(original["tenant_id"])
    endpoint_id = str(original["webhook_endpoint_id"])
    body = canonical_json_bytes(validate_webhook_payload(json.loads(str(original["payload_json"]))))
    if body.decode("utf-8") != str(original["wire_body_text"]):
        raise sqlite3.IntegrityError("webhook_delivery_body_invalid")

    while True:
        try:
            connection.execute("BEGIN IMMEDIATE")
            delivery = connection.execute(
                """
                SELECT * FROM webhook_deliveries
                WHERE tenant_id=? AND webhook_endpoint_id=? AND webhook_delivery_id=?
                  AND delivery_kind='original'
                """,
                (tenant_id, endpoint_id, delivery_id),
            ).fetchone()
            if delivery is None:
                raise sqlite3.IntegrityError("webhook_delivery_missing")
            if delivery["status"] in {"success", "failed"}:
                connection.commit()
                return
            if delivery["status"] != "pending":
                raise sqlite3.IntegrityError("webhook_delivery_status_invalid")

            attempt_count = int(delivery["attempt_count"])
            max_attempts = int(delivery["max_attempts"])
            if not 1 <= max_attempts <= 3 or not 0 <= attempt_count <= max_attempts:
                raise sqlite3.IntegrityError("webhook_delivery_attempt_count_invalid")
            endpoint = connection.execute(
                """
                SELECT * FROM webhook_endpoints
                WHERE tenant_id=? AND webhook_endpoint_id=?
                """,
                (tenant_id, endpoint_id),
            ).fetchone()
            if endpoint is None or endpoint["status"] != "active":
                _finish_pending_without_network(
                    connection,
                    delivery,
                    endpoint,
                    error_code="webhook.endpoint_disabled",
                    error_summary="Endpoint is disabled.",
                )
                connection.commit()
                return
            if attempt_count >= max_attempts:
                _finish_pending_without_network(
                    connection,
                    delivery,
                    endpoint,
                    error_code="webhook.delivery_unavailable",
                    error_summary="Delivery attempts are exhausted.",
                )
                connection.commit()
                return

            updated = connection.execute(
                """
                UPDATE webhook_deliveries
                SET secret_version=?,updated_at=?
                WHERE tenant_id=? AND webhook_delivery_id=? AND status='pending'
                  AND attempt_count=? AND secret_version=?
                """,
                (
                    int(endpoint["secret_version"]),
                    _db_time(),
                    tenant_id,
                    delivery_id,
                    attempt_count,
                    int(delivery["secret_version"]),
                ),
            )
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("webhook_delivery_state_changed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        attempt_no = attempt_count + 1
        result = _send_once(config, endpoint, body)
        try:
            _record_attempt(connection, endpoint, delivery_id, attempt_no, result)
            final = result["status"] == "success" or attempt_no >= max_attempts
            _update_delivery(
                connection,
                endpoint,
                delivery_id,
                attempt_no,
                result,
                final=final,
            )
        except Exception:
            # Attempt and delivery summary are one transaction. A crash after the
            # network response therefore leaves the intent pending for at-least-once
            # recovery with the same event_id.
            connection.rollback()
            raise
        if result["status"] == "success" or attempt_no >= max_attempts:
            return


def _finish_pending_without_network(
    connection: sqlite3.Connection,
    delivery: sqlite3.Row,
    endpoint: sqlite3.Row | None,
    *,
    error_code: str,
    error_summary: str,
) -> None:
    """Safely terminalize an intent which is no longer deliverable."""

    now = _db_time()
    cursor = connection.execute(
        """
        UPDATE webhook_deliveries
        SET status='failed',error_code=?,error_summary=?,next_retry_at=NULL,updated_at=?
        WHERE tenant_id=? AND webhook_delivery_id=? AND status='pending'
          AND attempt_count=?
        """,
        (
            error_code,
            error_summary,
            now,
            delivery["tenant_id"],
            delivery["webhook_delivery_id"],
            int(delivery["attempt_count"]),
        ),
    )
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("webhook_delivery_state_changed")
    if endpoint is not None:
        connection.execute(
            """
            UPDATE webhook_endpoints
            SET last_delivery_status='failed',last_delivery_at=?,last_failure_at=?,updated_at=?
            WHERE tenant_id=? AND webhook_endpoint_id=?
            """,
            (now, now, now, endpoint["tenant_id"], endpoint["webhook_endpoint_id"]),
        )


def _send_once(config: AppConfig, endpoint: sqlite3.Row, body: bytes) -> dict[str, object]:
    """发送一次签名请求，只返回状态码、耗时和固定错误分类。"""

    started = time.perf_counter()
    http_status: int | None = None
    try:
        secret = decrypt_secret(
            str(endpoint["encrypted_secret"]),
            encryption_secret=config.provider_key_encryption_secret,
        )
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"MetaHub-Webhook/{config.environment}",
            "X-MetaHub-Event": str(json.loads(body.decode("utf-8")).get("event_type", "")),
            "X-MetaHub-Signature": webhook_signature_header(secret, timestamp, body),
        }
        timeout = max(1.0, min(30.0, int(endpoint["timeout_ms"]) / 1000))
        # 使用 streaming 响应只读取状态，不下载或保存接收端可能返回的敏感/超长正文。
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            with client.stream(
                "POST",
                str(endpoint["endpoint_url"]),
                content=body,
                headers=headers,
            ) as response:
                http_status = response.status_code
        if http_status is not None and 200 <= http_status < 300:
            return _send_result("success", http_status, started, None, None)
        return _send_result(
            "failed",
            http_status,
            started,
            "webhook.http_error",
            "Receiver returned non-2xx.",
        )
    except httpx.TimeoutException:
        return _send_result(
            "failed", http_status, started, "webhook.timeout", "Receiver timed out."
        )
    # DNS、TLS 和连接中断统一归类为网络不可用，禁止把目标 URL 或异常文本写入审计表。
    except httpx.HTTPError:
        return _send_result(
            "failed",
            http_status,
            started,
            "webhook.network_error",
            "Receiver is unavailable.",
        )
    # 密钥解密或签名输入格式异常只返回固定分类，绝不输出密钥材料。
    except (ValueError, TypeError):
        # 固定英文摘要用于跨语言日志检索，不携带原始异常消息。
        return _send_result(
            "failed",
            http_status,
            started,
            "webhook.secret_unavailable",
            "Signing secret unavailable.",
        )
    except Exception:
        # 解密库或运行时异常可能携带内部对象，只返回固定分类并继续写 delivery attempt。
        return _send_result(
            "failed",
            http_status,
            started,
            "webhook.delivery_unavailable",
            "Delivery unavailable.",
        )


def _send_result(
    status: str,
    http_status: int | None,
    started: float,
    error_code: str | None,
    error_summary: str | None,
) -> dict[str, object]:
    """构造不含异常对象和响应正文的投递结果。"""

    return {
        "status": status,
        "http_status": http_status,
        "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "error_code": error_code,
        "error_summary": error_summary,
    }


def _record_attempt(
    connection: sqlite3.Connection,
    endpoint: sqlite3.Row,
    delivery_id: str,
    attempt_no: int,
    result: dict[str, object],
) -> None:
    """持久化一次安全发送摘要，不保存请求/响应 Header 和正文。"""

    connection.execute(
        """
        INSERT INTO webhook_delivery_attempts(
          webhook_delivery_attempt_id, tenant_id, webhook_delivery_id, webhook_endpoint_id,
          attempt_no, status, http_status, duration_ms, error_code, error_summary, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"wha_{uuid4().hex}",
            endpoint["tenant_id"],
            delivery_id,
            endpoint["webhook_endpoint_id"],
            attempt_no,
            result["status"],
            result["http_status"],
            result["duration_ms"],
            result["error_code"],
            result["error_summary"],
            _db_time(),
        ),
    )
    # 与 delivery 摘要更新在同一事务提交，避免进程故障留下“有 attempt、delivery 仍 pending”。


def _update_delivery(
    connection: sqlite3.Connection,
    endpoint: sqlite3.Row,
    delivery_id: str,
    attempt_no: int,
    result: dict[str, object],
    *,
    final: bool,
) -> None:
    """更新 delivery 与 Endpoint 最近状态；未到终态时保持 pending。"""

    now = _db_time()
    delivery_status = result["status"] if final else "pending"
    cursor = connection.execute(
        """
        UPDATE webhook_deliveries
        SET status=?,attempt_count=?,http_status=?,duration_ms=?,error_code=?,
            error_summary=?,updated_at=?
        WHERE tenant_id=? AND webhook_delivery_id=? AND status='pending'
          AND attempt_count=? AND secret_version=?
        """,
        (
            delivery_status,
            attempt_no,
            result["http_status"],
            result["duration_ms"],
            result["error_code"],
            result["error_summary"],
            now,
            endpoint["tenant_id"],
            delivery_id,
            attempt_no - 1,
            int(endpoint["secret_version"]),
        ),
    )
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("webhook_delivery_state_changed")
    if final:
        success = result["status"] == "success"
        connection.execute(
            """
            UPDATE webhook_endpoints
            SET last_delivery_status=?, last_delivery_at=?,
                last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
                last_failure_at=CASE WHEN ? THEN last_failure_at ELSE ? END,
                updated_at=?
            WHERE tenant_id=? AND webhook_endpoint_id=?
            """,
            (
                result["status"],
                now,
                1 if success else 0,
                now,
                1 if success else 0,
                now,
                now,
                endpoint["tenant_id"],
                endpoint["webhook_endpoint_id"],
            ),
        )
    connection.commit()


def _event_types(value: object) -> set[str]:
    """解析 Endpoint 订阅列表，非法存量数据按无订阅安全处理。"""

    try:
        parsed = json.loads(str(value))
    # 存量订阅 JSON 损坏时按无订阅处理，避免错误事件被意外投递到不明确 Endpoint。
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    return {str(item) for item in parsed} if isinstance(parsed, list) else set()


def _db_time() -> str:
    """返回项目统一的 UTC 秒级数据库时间。"""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
