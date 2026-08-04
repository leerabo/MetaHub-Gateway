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
    """向当前租户订阅该事件的 active Endpoint 同步投递，返回匹配 Endpoint 数量。

    调用方必须在后台线程执行本函数。每个 Endpoint 使用事件稳定 event_id 做唯一键，
    重复入队只会复用已有 delivery，不会再次发送网络请求。
    """

    connection = connect_database(config.database_url)
    try:
        rows = connection.execute(
            """
            SELECT * FROM webhook_endpoints
            WHERE tenant_id=? AND status='active'
            ORDER BY created_at ASC
            """,
            (event.tenant_id,),
        ).fetchall()
        matched = 0
        for endpoint in rows:
            if event.event_type not in _event_types(endpoint["event_types_json"]):
                continue
            if event.event_type == "webhook.delivery_failed":
                continue
            matched += 1
            _deliver_to_endpoint(config, connection, endpoint, event)
        return matched
    finally:
        # 网络、解密或数据库异常都必须释放 SQLite 连接，避免后台队列长期运行耗尽句柄。
        connection.close()


def _deliver_to_endpoint(
    config: AppConfig,
    connection: sqlite3.Connection,
    endpoint: sqlite3.Row,
    event: WebhookEvent,
) -> None:
    """幂等创建一条 delivery，并按 Endpoint 上限执行最多三次发送。"""

    delivery_id = f"whd_{uuid4().hex}"
    now = _db_time()
    # WebhookEvent 可被内部调用方直接构造，发送前仍需递归安全校验，不能只信任 builder。
    body = canonical_json_bytes(validate_webhook_payload(event.to_payload()))
    try:
        # 与控制面轮换使用相同写锁顺序；创建 intent 时重读当前 Endpoint，旧批量快照不能决定签名版本。
        connection.execute("BEGIN IMMEDIATE")
        current_endpoint = connection.execute(
            """
            SELECT * FROM webhook_endpoints
            WHERE tenant_id=? AND webhook_endpoint_id=? AND status='active'
            """,
            (event.tenant_id, endpoint["webhook_endpoint_id"]),
        ).fetchone()
        if current_endpoint is None or event.event_type not in _event_types(current_endpoint["event_types_json"]):
            connection.commit()
            return
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO webhook_deliveries(
              webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
              event_timestamp, payload_json, status, attempt_count, max_attempts,
              secret_version, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                event.tenant_id,
                current_endpoint["webhook_endpoint_id"],
                event.event_id,
                event.event_type,
                event.timestamp,
                body.decode("utf-8"),
                int(current_endpoint["max_attempts"]),
                int(current_endpoint["secret_version"]),
                now,
                now,
            ),
        )
        connection.commit()
    # 后台 intent 写入失败必须回滚当前事务，再交给队列既有 best-effort 错误处理。
    except sqlite3.Error:
        # 后台投递失败不能留下半条 intent；调用方队列会按既有 best-effort 合同记录安全错误。
        connection.rollback()
        raise
    if cursor.rowcount != 1:
        return

    max_attempts = min(3, max(1, int(current_endpoint["max_attempts"])))
    for attempt_no in range(1, max_attempts + 1):
        result = _send_once(config, current_endpoint, body)
        _record_attempt(connection, current_endpoint, delivery_id, attempt_no, result)
        final = result["status"] == "success" or attempt_no == max_attempts
        _update_delivery(connection, current_endpoint, delivery_id, attempt_no, result, final=final)
        if result["status"] == "success":
            break


def _send_once(config: AppConfig, endpoint: sqlite3.Row, body: bytes) -> dict[str, object]:
    """发送一次签名请求，只返回状态码、耗时和固定错误分类。"""

    started = time.perf_counter()
    http_status: int | None = None
    try:
        secret = decrypt_secret(str(endpoint["encrypted_secret"]))
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
            with client.stream("POST", str(endpoint["endpoint_url"]), content=body, headers=headers) as response:
                http_status = response.status_code
        if http_status is not None and 200 <= http_status < 300:
            return _send_result("success", http_status, started, None, None)
        return _send_result("failed", http_status, started, "webhook.http_error", "Receiver returned non-2xx.")
    except httpx.TimeoutException:
        return _send_result("failed", http_status, started, "webhook.timeout", "Receiver timed out.")
    # DNS、TLS 和连接中断统一归类为网络不可用，禁止把目标 URL 或异常文本写入审计表。
    except httpx.HTTPError:
        return _send_result("failed", http_status, started, "webhook.network_error", "Receiver is unavailable.")
    # 密钥解密或签名输入格式异常只返回固定分类，绝不输出密钥材料。
    except (ValueError, TypeError):
        # 固定英文摘要用于跨语言日志检索，不携带原始异常消息。
        return _send_result("failed", http_status, started, "webhook.secret_unavailable", "Signing secret unavailable.")
    except Exception:
        # 解密库或运行时异常可能携带内部对象，只返回固定分类并继续写 delivery attempt。
        return _send_result("failed", http_status, started, "webhook.delivery_unavailable", "Delivery unavailable.")


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
    connection.execute(
        """
        UPDATE webhook_deliveries
        SET status=?, attempt_count=?, http_status=?, duration_ms=?, error_code=?, error_summary=?, updated_at=?
        WHERE tenant_id=? AND webhook_delivery_id=?
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
        ),
    )
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
