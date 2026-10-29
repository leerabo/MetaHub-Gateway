"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，
Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/webhooks/service.py
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import (
    decrypt_secret,
    encrypt_secret,
    secret_fingerprint,
    secret_prefix,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.security_events import (
    SECURITY_EVENT_TYPE,
    normalize_webhook_payload_format,
    security_event_cef_bytes,
    webhook_test_cef_bytes,
)
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    WebhookEvent,
    build_webhook_event,
    canonical_json_bytes,
    normalize_webhook_event_type,
    validate_webhook_payload,
    webhook_signature_header,
)

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext

from .models import (
    CreateWebhookEndpointRequest,
    ReplayWebhookDeliveryRequest,
    ReplayWebhookRequest,
    WebhookDeadLetterArchiveResponse,
    WebhookDeadLetterAttemptInfo,
    WebhookDeadLetterDeliveryInfo,
    WebhookDeadLetterFilters,
    WebhookDeadLetterInfo,
    WebhookDeadLetterListResponse,
    WebhookDeliveryAttemptInfo,
    WebhookDeliveryFilters,
    WebhookDeliveryInfo,
    WebhookDeliveryListResponse,
    WebhookDeliveryRecordInfo,
    WebhookDeliveryReplayResponse,
    WebhookEndpointCreateResponse,
    WebhookEndpointInfo,
    WebhookEndpointListResponse,
    WebhookReplayResponse,
    WebhookSecretRotateResponse,
    WebhookTestResponse,
)

logger = logging.getLogger("metahub.control.webhooks")

# Webhook 状态、限额、超时、重试和敏感字段名单集中定义，避免投递链路泄露密钥或正文。
VALID_STATUSES = {"active", "disabled"}
MAX_ENDPOINTS_PER_TENANT = 20
MAX_NAME_LENGTH = 120
MAX_URL_LENGTH = 2048
DEFAULT_TIMEOUT_MS = 5000
MIN_TIMEOUT_MS = 1000
MAX_TIMEOUT_MS = 30000
MAX_ATTEMPTS = 3
DEFAULT_SECURITY_EVENT_RATE_LIMIT = 60
MIN_SECURITY_EVENT_RATE_LIMIT = 1
MAX_SECURITY_EVENT_RATE_LIMIT = 600
RECENT_DELIVERY_LIMIT = 20
DETAIL_DELIVERY_LIMIT = 100
DEAD_LETTER_DEFAULT_LIMIT = 50
DEAD_LETTER_MAX_LIMIT = 100
DEAD_LETTER_CURSOR_MAX_LENGTH = 1024
VALID_DEAD_LETTER_STATUSES = {"dead_letter", "replay_pending", "replayed", "archived", "expired"}
VALID_DELIVERY_STATUSES = {"pending", "success", "failed"}
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def list_webhooks(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    event_type: str | None,
    include_deliveries: bool,
    request_id: str,
    trace_id: str,
) -> WebhookEndpointListResponse:
    """读取当前租户 Webhook Endpoint 和最近发送记录。

    查询条件只能收窄当前登录态 tenant_id 下的数据，前端传入的任何租户字段都不能影响结果归属。
    """

    normalized_status = _normalize_query_status(status)
    normalized_event_type = _normalize_optional_event_type(event_type)
    clauses = ["tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    if normalized_status != "all":
        clauses.append("status=?")
        params.append(normalized_status)
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            SELECT *
            FROM webhook_endpoints
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at DESC, name ASC
            """,
            params,
        ).fetchall()
        endpoints = [
            _endpoint_info(
                connection,
                row,
                include_deliveries=include_deliveries,
                event_type=normalized_event_type,
            )
            for row in rows
            # JSON event types are filtered after the tenant-scoped database query.
            if normalized_event_type is None
            or normalized_event_type in _event_types_from_row(row)
        ]
        return WebhookEndpointListResponse(
            webhooks=endpoints,
            event_types=sorted(WEBHOOK_SUBSCRIBABLE_EVENT_TYPES),
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def create_webhook(
    config: AppConfig,
    context: AuthContext,
    payload: CreateWebhookEndpointRequest,
    *,
    request_id: str,
    trace_id: str,
) -> WebhookEndpointCreateResponse:
    """创建当前租户的 Webhook Endpoint。

    服务端生成 signing_secret 并只返回一次；数据库只保存密文、前缀和指纹，日志也只记录指纹。
    """

    name = _normalize_name(payload.name)
    endpoint_url = _normalize_endpoint_url(config, payload.endpoint_url)
    event_types = _normalize_event_types(payload.event_types)
    payload_format = _normalize_payload_format(payload.format)
    security_event_rate_limit = _normalize_security_event_rate_limit(
        payload.security_event_rate_limit_per_minute
    )
    if payload_format == "cef" and event_types != [SECURITY_EVENT_TYPE]:
        raise ApiError("webhook.format_invalid")
    status = _normalize_status(payload.status or "active")
    timeout_ms = _normalize_timeout_ms(payload.timeout_ms)
    connection = _connect(config)
    _ensure_endpoint_quota(connection, context.user.tenant_id)
    signing_secret = f"whsec_{secrets.token_urlsafe(32)}"
    try:
        encrypted_secret = encrypt_secret(
            signing_secret, encryption_secret=config.provider_key_encryption_secret,
        )
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except ValueError as exc:
        # 加密失败时不能退化为明文保存，必须让调用方看到可重试的密钥错误。
        raise ApiError("webhook.secret_unavailable") from exc

    endpoint_id = f"wh_{uuid4().hex}"
    now = _to_db_time(_utcnow())
    try:
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
              encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
              max_attempts, created_by, updated_by, created_at, updated_at,
              payload_format, security_event_rate_limit_per_minute
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                endpoint_id,
                context.user.tenant_id,
                name,
                endpoint_url,
                _json_dumps(event_types),
                encrypted_secret,
                secret_prefix(signing_secret),
                secret_fingerprint(signing_secret),
                status,
                timeout_ms,
                MAX_ATTEMPTS,
                context.user.user_id,
                context.user.user_id,
                now,
                now,
                payload_format,
                security_event_rate_limit,
            ),
        )
        connection.commit()
    # 数据库唯一性或外键失败都属于配置写入异常，不能把表名和索引名透给调用方。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 唯一性或外键异常都统一映射为 Endpoint 参数错误，避免泄露表结构和索引名称。
        raise ApiError("webhook.endpoint_invalid") from exc

    row = _load_endpoint_row(connection, context.user.tenant_id, endpoint_id)
    if row is None:
        # 写入后无法回读说明数据库状态异常，返回统一投递服务不可用错误。
        raise ApiError("webhook.delivery_unavailable")
    _log_endpoint_created(context, row, request_id, trace_id)
    return WebhookEndpointCreateResponse(
        webhook=_endpoint_info(connection, row, include_deliveries=False),
        signing_secret=signing_secret,
        request_id=request_id,
        trace_id=trace_id,
    )


def test_webhook(
    config: AppConfig,
    context: AuthContext,
    endpoint_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> WebhookTestResponse:
    """向指定 Endpoint 发送一次 webhook.test 事件，并返回投递和重试记录。"""

    connection = _connect(config)
    row = _load_endpoint_or_404(connection, context.user.tenant_id, endpoint_id)
    _ensure_endpoint_active(row)
    event = build_webhook_event(
        tenant_id=context.user.tenant_id,
        event_type="webhook.test",
        actor_id=context.user.user_id,
        object_id=row["webhook_endpoint_id"],
        trace_id=trace_id,
        payload={
            "source": "control-plane-api",
            "request_id": request_id,
            "message": "Webhook test event.",
        },
    )
    delivery = _deliver_event_to_endpoint(
        config,
        connection,
        row,
        event,
        request_id=request_id,
        trace_id=trace_id,
    )
    return WebhookTestResponse(delivery=delivery, request_id=request_id, trace_id=trace_id)


def list_webhook_deliveries(
    config: AppConfig,
    context: AuthContext,
    endpoint_id: str,
    *,
    event_type: str | None,
    status: str | None,
    limit: object,
    request_id: str,
    trace_id: str,
) -> WebhookDeliveryListResponse:
    """读取当前租户指定 Endpoint 的最近投递记录。

    参数：筛选条件只收窄认证租户和路径 Endpoint 下的历史数据，limit 最大为 100。
    返回：delivery 与 attempts 的安全摘要、规范化筛选和请求链路 ID。
    约束：历史筛选不依赖 Endpoint 当前订阅列表，取消订阅后仍可排查旧失败记录。
    """

    normalized_event_type = _normalize_delivery_event_type(event_type)
    normalized_status = _normalize_delivery_status(status)
    normalized_limit = _normalize_delivery_limit(limit)
    connection = _connect(config)
    try:
        endpoint = _load_endpoint_or_404(connection, context.user.tenant_id, endpoint_id)
        clauses = ["tenant_id=?", "webhook_endpoint_id=?"]
        params: list[object] = [context.user.tenant_id, endpoint["webhook_endpoint_id"]]
        if normalized_event_type:
            clauses.append("event_type=?")
            params.append(normalized_event_type)
        if normalized_status != "all":
            clauses.append("status=?")
            params.append(normalized_status)
        rows = connection.execute(
            f"""
            SELECT * FROM webhook_deliveries
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC, webhook_delivery_id DESC
            LIMIT ?
            """,
            [*params, normalized_limit],
        ).fetchall()
        return WebhookDeliveryListResponse(
            webhook_endpoint_id=endpoint["webhook_endpoint_id"],
            deliveries=[_delivery_record_from_row(connection, row) for row in rows],
            filters=WebhookDeliveryFilters(
                event_type=normalized_event_type,
                status=normalized_status,
                limit=normalized_limit,
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


@dataclass(frozen=True)
class _ReplayIntent:
    """已经提交的 replay intent 最小事实，避免在事务外携带 payload 或 Secret。"""

    source_delivery_id: str
    replay_delivery_id: str
    dead_letter_event_id: str
    created: bool
    audit_record: AuditRecord | None


def replay_webhook(
    config: AppConfig,
    context: AuthContext,
    endpoint_id: str,
    payload: ReplayWebhookRequest,
    *,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> WebhookReplayResponse:
    """兼容旧 Endpoint replay 路径，并复用 PRD-057 持久 intent/worker。

    参数与响应保持 PRD-042 合同；租户、权限和实际出网仍由后端持久事实约束。
    """

    normalized_endpoint_id = _normalize_endpoint_id(endpoint_id)
    # 旧合同先确认路径 Endpoint，保持不存在时返回 endpoint_not_found。
    connection = _connect(config)
    try:
        _load_endpoint_or_404(connection, context.user.tenant_id, normalized_endpoint_id)
    finally:
        connection.close()
    try:
        intent = _create_replay_intent(
            config,
            context,
            _normalize_delivery_id(payload.webhook_delivery_id),
            _normalize_idempotency_key(payload.idempotency_key),
            expected_endpoint_id=normalized_endpoint_id,
            request_id=request_id,
            trace_id=trace_id,
            audit_request_meta=audit_request_meta,
        )
    # 兼容入口只映射公开错误码，底层死信分类和异常细节不得直接泄露给旧客户端。
    except ApiError as exc:
        # 兼容接口保留 PRD-042 的公开错误合同，新顶层接口使用更精确的死信错误码。
        if exc.code == "webhook.dead_letter_not_found":
            raise ApiError("webhook.delivery_not_found") from exc
        if exc.code in {
            "webhook.dead_letter_not_replayable",
            "webhook.dead_letter_replay_pending",
        }:
            # replay_pending 与其他不可重放状态在旧合同中必须收敛为同一个稳定错误。
            raise ApiError("webhook.delivery_not_replayable") from exc
        raise

    # 幂等回读必须是纯读取；只有本请求新建 intent 时才允许触发即时出网。
    if intent.created:
        _attempt_replay_worker(
            config,
            intent.dead_letter_event_id,
            intent.replay_delivery_id,
        )
    delivery, _dead_letter = _read_replay_state(
        config,
        context.user.tenant_id,
        intent.replay_delivery_id,
        intent.dead_letter_event_id,
    )
    _dispatch_replay_audit(
        config,
        context,
        intent,
        delivery,
        request_id=request_id,
        trace_id=trace_id,
    )
    return WebhookReplayResponse(
        delivery=delivery,
        replayed=intent.created,
        request_id=request_id,
        trace_id=trace_id,
    )


def replay_webhook_delivery(
    config: AppConfig,
    context: AuthContext,
    delivery_id: str,
    payload: ReplayWebhookDeliveryRequest,
    *,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> WebhookDeliveryReplayResponse:
    """按 Excel 指定顶层路径持久重放来源 delivery，响应严格排除事件正文。"""

    intent = _create_replay_intent(
        config,
        context,
        _normalize_dead_letter_delivery_id(delivery_id),
        _normalize_idempotency_key(payload.idempotency_key),
        expected_endpoint_id=None,
        request_id=request_id,
        trace_id=trace_id,
        audit_request_meta=audit_request_meta,
    )
    # 旧幂等键可能对应历史 delivery，禁止借回读触发根死信当前的另一条 pending intent。
    if intent.created:
        _attempt_replay_worker(
            config,
            intent.dead_letter_event_id,
            intent.replay_delivery_id,
        )
    delivery, dead_letter = _read_replay_state(
        config,
        context.user.tenant_id,
        intent.replay_delivery_id,
        intent.dead_letter_event_id,
    )
    _dispatch_replay_audit(
        config,
        context,
        intent,
        delivery,
        request_id=request_id,
        trace_id=trace_id,
    )
    return WebhookDeliveryReplayResponse(
        delivery=_dead_letter_delivery_info(delivery),
        dead_letter=dead_letter,
        replayed=intent.created,
        request_id=request_id,
        trace_id=trace_id,
    )


def list_webhook_dead_letters(
    config: AppConfig,
    context: AuthContext,
    *,
    status: object,
    event_type: object,
    webhook_endpoint_id: object,
    limit: object,
    cursor: object,
    request_id: str,
    trace_id: str,
) -> WebhookDeadLetterListResponse:
    """使用 seek cursor 读取当前认证租户的安全死信摘要。"""

    normalized_status = _normalize_dead_letter_status(status)
    normalized_event_type = _normalize_dead_letter_event_type(event_type)
    normalized_endpoint_id = _normalize_dead_letter_endpoint_filter(webhook_endpoint_id)
    normalized_limit = _normalize_dead_letter_limit(limit)
    cursor_value = _decode_dead_letter_cursor(cursor)
    clauses = ["dead.tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    if normalized_status != "all":
        clauses.append("dead.status=?")
        params.append(normalized_status)
    if normalized_event_type is not None:
        clauses.append("dead.event_type=?")
        params.append(normalized_event_type)
    if normalized_endpoint_id is not None:
        clauses.append("dead.webhook_endpoint_id=?")
        params.append(normalized_endpoint_id)
    if cursor_value is not None:
        clauses.append("(dead.failed_at<? OR (dead.failed_at=? AND dead.dead_letter_event_id<?))")
        params.extend(
            [
                cursor_value["failed_at"],
                cursor_value["failed_at"],
                cursor_value["dead_letter_event_id"],
            ]
        )

    connection = _connect(config, unavailable_code="webhook.dead_letter_unavailable")
    try:
        rows = connection.execute(
            f"""
            SELECT dead.*, endpoint.name AS webhook_endpoint_name
            FROM webhook_dead_letter_events dead
            JOIN webhook_endpoints endpoint
              ON endpoint.tenant_id=dead.tenant_id
             AND endpoint.webhook_endpoint_id=dead.webhook_endpoint_id
            WHERE {" AND ".join(clauses)}
            ORDER BY dead.failed_at DESC, dead.dead_letter_event_id DESC
            LIMIT ?
            """,
            [*params, normalized_limit + 1],
        ).fetchall()
        page_rows = rows[:normalized_limit]
        dead_letters = [_dead_letter_info(connection, row) for row in page_rows]
        next_cursor = None
        if len(rows) > normalized_limit and page_rows:
            last = page_rows[-1]
            next_cursor = _encode_dead_letter_cursor(
                str(last["failed_at"]),
                str(last["dead_letter_event_id"]),
            )
        return WebhookDeadLetterListResponse(
            dead_letters=dead_letters,
            next_cursor=next_cursor,
            filters=WebhookDeadLetterFilters(
                status=normalized_status,
                event_type=normalized_event_type,
                webhook_endpoint_id=normalized_endpoint_id,
                limit=normalized_limit,
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
    # 列表查询失败只公开固定错误，禁止把 SQL、数据库路径或其他租户事实带入响应。
    except sqlite3.Error as exc:
        raise ApiError("webhook.dead_letter_unavailable") from exc
    finally:
        connection.close()


def archive_webhook_dead_letter(
    config: AppConfig,
    context: AuthContext,
    dead_letter_event_id: str,
    *,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> WebhookDeadLetterArchiveResponse:
    """逻辑归档当前租户死信，并把状态变更与管理审计放在同一事务。"""

    normalized_id = _normalize_dead_letter_id(dead_letter_event_id)
    connection = _connect(config, unavailable_code="webhook.dead_letter_unavailable")
    audit_record: AuditRecord | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_dead_letter_row(connection, context.user.tenant_id, normalized_id)
        # 同租户查询未命中与跨租户 ID 共享 404，避免归属枚举侧信道。
        if row is None:
            raise ApiError("webhook.dead_letter_not_found")
        # replay_pending、归档和过期状态都不能再次归档，状态机必须由数据库事实决定。
        if row["status"] not in {"dead_letter", "replayed"}:
            raise ApiError("webhook.dead_letter_not_archivable")
        now_dt = _utcnow()
        if _parse_db_time(row["expires_at"]) <= now_dt:
            # scheduler 尚未落过期状态时也必须拒绝绕过固定 30 天留存边界。
            raise ApiError("webhook.dead_letter_not_archivable")
        now = _to_db_time(now_dt)
        cursor_result = connection.execute(
            """
            UPDATE webhook_dead_letter_events
            SET status='archived', archived_by=?, archived_at=?, updated_at=?
            WHERE tenant_id=? AND dead_letter_event_id=? AND status IN ('dead_letter','replayed')
            """,
            (
                context.user.user_id,
                now,
                now,
                context.user.tenant_id,
                normalized_id,
            ),
        )
        # CAS 未命中表示状态已被并发请求推进，不能基于旧快照补写归档审计。
        if cursor_result.rowcount != 1:
            raise ApiError("webhook.dead_letter_not_archivable")
        before_summary = _dead_letter_audit_summary(row)
        after_summary = dict(before_summary)
        after_summary.update({"status": "archived", "archived_at": now})
        audit_record = record_audit_log(
            connection,
            context,
            action="webhook_dead_letter.archived",
            object_type="webhook_dead_letter",
            object_id=normalized_id,
            object_name=str(row["event_type"]),
            before_summary=before_summary,
            after_summary=after_summary,
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
        archived = _load_dead_letter_row(connection, context.user.tenant_id, normalized_id)
        # 提交后无法回读说明持久事实异常，不能构造猜测性的成功响应。
        if archived is None:
            raise ApiError("webhook.dead_letter_unavailable")
        response = WebhookDeadLetterArchiveResponse(
            dead_letter=_dead_letter_info(connection, archived),
            request_id=request_id,
            trace_id=trace_id,
        )
    # 领域拒绝与审计提交失败都必须回滚归档事务，再原样保留已统一的错误码。
    except ApiError:
        connection.rollback()
        raise
    # 数据库或时间字段损坏时统一回滚，响应不得泄露 SQL 和持久字段内容。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        connection.rollback()
        raise ApiError("webhook.dead_letter_unavailable") from exc
    finally:
        connection.close()
    if audit_record is not None:
        dispatch_audit_recorded(
            config,
            context,
            audit_record,
            request_id=request_id,
            trace_id=trace_id,
        )
    return response


def _create_replay_intent(
    config: AppConfig,
    context: AuthContext,
    source_delivery_id: str,
    idempotency_key: str,
    *,
    expected_endpoint_id: str | None,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> _ReplayIntent:
    """同事务创建 replay delivery、推进根死信并写审计；任何 HTTP 都在提交之后。"""

    connection = _connect(config, unavailable_code="webhook.replay_unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        source = _load_delivery_by_id(
            connection,
            context.user.tenant_id,
            source_delivery_id,
        )
        # 来源必须属于认证租户且匹配旧路径 Endpoint；两种未命中统一按不可枚举资源处理。
        if source is None or (
            expected_endpoint_id is not None
            and source["webhook_endpoint_id"] != expected_endpoint_id
        ):
            # 不区分不存在、跨租户或 Endpoint 不匹配，统一 404 才不会暴露资源归属。
            raise ApiError("webhook.dead_letter_not_found")
        existing = _load_replay_by_idempotency(
            connection,
            context.user.tenant_id,
            str(source["webhook_endpoint_id"]),
            source_delivery_id,
            idempotency_key,
        )
        dead_letter = _load_dead_letter_by_source(
            connection,
            context.user.tenant_id,
            source_delivery_id,
        )
        # 同幂等键必须先回读，即使死信后来归档、过期或已经发起另一轮 replay。
        if existing is not None:
            if dead_letter is None:
                raise ApiError("webhook.dead_letter_unavailable")
            connection.commit()
            return _ReplayIntent(
                source_delivery_id=source_delivery_id,
                replay_delivery_id=str(existing["webhook_delivery_id"]),
                dead_letter_event_id=str(dead_letter["dead_letter_event_id"]),
                created=False,
                audit_record=None,
            )
        # 只有最终失败来源可创建新 intent，成功或 pending delivery 不能被 replay 放大出网。
        if source["status"] != "failed":
            raise ApiError("webhook.dead_letter_not_replayable")
        # 同租户根死信不存在时按资源不存在返回，不从 delivery 推测或补造治理事实。
        if dead_letter is None:
            raise ApiError("webhook.dead_letter_not_found")
        # 根死信已有活跃 intent 时拒绝第二个新键，避免 current delivery 被并发覆盖。
        if dead_letter["status"] == "replay_pending":
            raise ApiError("webhook.dead_letter_replay_pending")
        # archived、expired 和 replayed 都不是新 replay 起点，必须遵守单向状态机。
        if dead_letter["status"] != "dead_letter":
            raise ApiError("webhook.dead_letter_not_replayable")
        now_dt = _utcnow()
        # scheduler 尚未更新 expired 时仍按固定时间边界拒绝，避免竞态绕过留存策略。
        if _parse_db_time(dead_letter["expires_at"]) <= now_dt:
            raise ApiError("webhook.dead_letter_not_replayable")

        endpoint = _load_endpoint_row(
            connection,
            context.user.tenant_id,
            str(source["webhook_endpoint_id"]),
        )
        # Endpoint 必须由同租户外键事实回读，缺失时不能使用来源 payload 中的任何地址。
        if endpoint is None:
            raise ApiError("webhook.dead_letter_not_found")
        _ensure_endpoint_active(endpoint)
        _validated_stored_envelope(
            source["payload_json"],
            source=source,
            tenant_id=context.user.tenant_id,
        )
        replay_delivery_id = f"whd_{uuid4().hex}"
        now = _to_db_time(now_dt)
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
              event_timestamp, payload_json, status, attempt_count, max_attempts,
              delivery_kind, replayed_from_delivery_id, replay_idempotency_key,
              replayed_by, replayed_at, secret_version, created_at, updated_at,
              payload_format, wire_body_text, security_event_rate_limit_per_minute
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, 'replay', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                replay_delivery_id,
                context.user.tenant_id,
                endpoint["webhook_endpoint_id"],
                source["event_id"],
                source["event_type"],
                source["event_timestamp"],
                str(source["payload_json"]),
                int(endpoint["max_attempts"] or MAX_ATTEMPTS),
                source_delivery_id,
                idempotency_key,
                context.user.user_id,
                now,
                int(endpoint["secret_version"]),
                now,
                now,
                source["payload_format"],
                source["wire_body_text"],
                int(source["security_event_rate_limit_per_minute"]),
            ),
        )
        updated = connection.execute(
            """
            UPDATE webhook_dead_letter_events
            SET status='replay_pending', current_replay_delivery_id=?,
                replay_requested_by=?, replay_requested_at=?,
                replay_dispatch_attempts=0, replay_next_attempt_at=?,
                replay_claim_token=NULL, replay_lease_owner_id=NULL,
                replay_lease_expires_at=NULL, last_replay_error_code=NULL,
                replayed_at=NULL, updated_at=?
            WHERE tenant_id=? AND dead_letter_event_id=? AND status='dead_letter'
              AND expires_at>?
            """,
            (
                replay_delivery_id,
                context.user.user_id,
                now,
                now,
                now,
                context.user.tenant_id,
                dead_letter["dead_letter_event_id"],
                now,
            ),
        )
        # 根死信 CAS 未命中表示状态或过期边界已变化，必须回滚刚插入的 replay delivery。
        if updated.rowcount != 1:
            raise ApiError("webhook.dead_letter_not_replayable")
        audit_record = record_audit_log(
            connection,
            context,
            action="webhook_delivery.replayed",
            object_type="webhook_delivery",
            object_id=replay_delivery_id,
            object_name=str(source["event_type"]),
            before_summary={},
            after_summary={
                "endpoint_id": endpoint["webhook_endpoint_id"],
                "event_type": source["event_type"],
                "status": "pending",
                "source_delivery_id": source_delivery_id,
                "replay_delivery_id": replay_delivery_id,
                "secret_version": int(endpoint["secret_version"]),
            },
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
        return _ReplayIntent(
            source_delivery_id=source_delivery_id,
            replay_delivery_id=replay_delivery_id,
            dead_letter_event_id=str(dead_letter["dead_letter_event_id"]),
            created=True,
            audit_record=audit_record,
        )
    # 领域拒绝必须回滚 intent、根死信和审计的整个事务，不能留下孤立 pending delivery。
    except ApiError:
        connection.rollback()
        raise
    # 持久字段或数据库异常同样全量回滚，并折叠为不含 SQL、payload 和幂等键的错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        connection.rollback()
        # intent 异常不回显 SQL、payload、幂等键或数据库路径。
        raise ApiError("webhook.replay_unavailable") from exc
    finally:
        connection.close()


def _attempt_replay_worker(
    config: AppConfig,
    dead_letter_event_id: str,
    replay_delivery_id: str,
) -> None:
    """提交后尽力领取当前 intent 一次；失败保留持久状态，由 scheduler 后续接管。

    死信 ID 与 replay delivery ID 必须共同约束领取。命中旧幂等键时，根死信可能已经指向
    另一条新 intent，仅按死信领取会让旧请求意外触发新任务并造成重复出网。
    """

    from .outbox import claim_next_replay, process_claimed_replay

    try:
        claim = claim_next_replay(
            config,
            worker_id=f"webhook-api-{uuid4().hex}",
            dead_letter_event_id=dead_letter_event_id,
            replay_delivery_id=replay_delivery_id,
        )
        if claim is not None:
            process_claimed_replay(config, claim)
    # 即时 drain 是提交后的尽力优化，失败只记录固定 code 并交给 scheduler 接管。
    except ApiError as exc:
        logger.warning(
            "webhook replay immediate drain failed",
            extra={
                "event": "webhook.replay_worker_failed",
                "object_id": dead_letter_event_id,
                "error_code": exc.code,
            },
        )


def _read_replay_state(
    config: AppConfig,
    tenant_id: str,
    replay_delivery_id: str,
    dead_letter_event_id: str,
) -> tuple[WebhookDeliveryInfo, WebhookDeadLetterInfo]:
    """按同租户回读 replay 与根死信当前事实，构造 API 响应时不信任 worker 内存状态。"""

    connection = _connect(config, unavailable_code="webhook.replay_unavailable")
    try:
        replay_row = _load_delivery_by_id(connection, tenant_id, replay_delivery_id)
        dead_letter = _load_dead_letter_row(connection, tenant_id, dead_letter_event_id)
        # 响应只能由同租户两份持久事实共同构造，任一缺失都不能返回部分或跨租户数据。
        if replay_row is None or dead_letter is None:
            raise ApiError("webhook.dead_letter_unavailable")
        return (
            _delivery_info_from_row(connection, replay_row, include_attempts=True),
            _dead_letter_info(connection, dead_letter),
        )
    # 回读异常折叠为固定错误，避免响应泄露 SQL、数据库路径和 payload_json 内容。
    except sqlite3.Error as exc:
        raise ApiError("webhook.dead_letter_unavailable") from exc
    finally:
        connection.close()


def _dispatch_replay_audit(
    config: AppConfig,
    context: AuthContext,
    intent: _ReplayIntent,
    delivery: WebhookDeliveryInfo,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """首次 intent 提交后记录安全 replay 事件并尽力派发审计 Webhook。"""

    if not intent.created:
        return
    _log_replay_created(
        context,
        intent.source_delivery_id,
        delivery,
        request_id,
        trace_id,
    )
    if intent.audit_record is not None:
        dispatch_audit_recorded(
            config,
            context,
            intent.audit_record,
            request_id=request_id,
            trace_id=trace_id,
        )


def _dead_letter_attempt_info(
    attempt: WebhookDeliveryAttemptInfo,
) -> WebhookDeadLetterAttemptInfo:
    """从内部 attempt 复制 PAGE-083 白名单，排除租户归属和错误正文。"""

    return WebhookDeadLetterAttemptInfo(
        attempt_no=attempt.attempt_no,
        status=attempt.status,
        http_status=attempt.http_status,
        duration_ms=attempt.duration_ms,
        error_code=attempt.error_code,
        created_at=attempt.created_at,
    )


def _dead_letter_delivery_info(
    delivery: WebhookDeliveryInfo | WebhookDeliveryRecordInfo,
) -> WebhookDeadLetterDeliveryInfo:
    """显式复制死信动作允许公开的 delivery 字段。

    内部模型包含 tenant_id、replayed_by、Secret 版本与错误摘要。这里逐字段构造专用 DTO，
    使后续内部模型增加字段时也不会被 Pydantic 的宽松 extra 行为意外带入浏览器响应。
    """

    return WebhookDeadLetterDeliveryInfo(
        webhook_delivery_id=delivery.webhook_delivery_id,
        webhook_endpoint_id=delivery.webhook_endpoint_id,
        event_id=delivery.event_id,
        event_type=delivery.event_type,
        event_timestamp=delivery.event_timestamp,
        status=delivery.status,
        attempt_count=delivery.attempt_count,
        max_attempts=delivery.max_attempts,
        http_status=delivery.http_status,
        duration_ms=delivery.duration_ms,
        error_code=delivery.error_code,
        next_retry_at=delivery.next_retry_at,
        delivery_kind=delivery.delivery_kind,
        replayed_from_delivery_id=delivery.replayed_from_delivery_id,
        replayed_at=delivery.replayed_at,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
        attempts=[_dead_letter_attempt_info(attempt) for attempt in delivery.attempts],
    )


def _dead_letter_info(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> WebhookDeadLetterInfo:
    """把数据库死信转换为无 payload、URL、幂等键和 lease 的安全 DTO。"""

    replay_row = None
    if row["current_replay_delivery_id"]:
        replay_row = _load_delivery_row(
            connection,
            str(row["tenant_id"]),
            str(row["webhook_endpoint_id"]),
            str(row["current_replay_delivery_id"]),
        )
    latest_row = replay_row or _load_delivery_row(
        connection,
        str(row["tenant_id"]),
        str(row["webhook_endpoint_id"]),
        str(row["webhook_delivery_id"]),
    )
    attempts = _delivery_attempts(connection, latest_row) if latest_row is not None else []
    replay_delivery = (
        _delivery_record_from_row(connection, replay_row) if replay_row is not None else None
    )
    expired_by_time = _parse_db_time(row["expires_at"]) <= _utcnow()
    status = str(row["status"])
    allowed_actions: list[str] = []
    if not expired_by_time and status == "dead_letter":
        allowed_actions = ["replay", "archive"]
    elif not expired_by_time and status == "replayed":
        allowed_actions = ["archive"]
    return WebhookDeadLetterInfo(
        dead_letter_event_id=row["dead_letter_event_id"],
        webhook_delivery_id=row["webhook_delivery_id"],
        webhook_endpoint_id=row["webhook_endpoint_id"],
        webhook_endpoint_name=row["webhook_endpoint_name"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        status=status,
        failure_error_code=row["failure_error_code"],
        failure_http_status=row["failure_http_status"],
        failed_at=_parse_db_time(row["failed_at"]),
        expires_at=_parse_db_time(row["expires_at"]),
        current_replay_delivery_id=row["current_replay_delivery_id"],
        replay_requested_at=_parse_optional_db_time(row["replay_requested_at"]),
        replay_dispatch_attempts=int(row["replay_dispatch_attempts"]),
        last_replay_error_code=row["last_replay_error_code"],
        replayed_at=_parse_optional_db_time(row["replayed_at"]),
        archived_at=_parse_optional_db_time(row["archived_at"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        latest_attempt=_dead_letter_attempt_info(attempts[-1]) if attempts else None,
        current_replay_delivery=(
            _dead_letter_delivery_info(replay_delivery) if replay_delivery is not None else None
        ),
        replayable="replay" in allowed_actions,
        allowed_actions=allowed_actions,
    )


def _load_delivery_by_id(
    connection: sqlite3.Connection,
    tenant_id: str,
    delivery_id: str,
) -> sqlite3.Row | None:
    """仅按认证租户与全局 delivery ID 回读，顶层路径不能携带或覆盖 Endpoint。"""

    return connection.execute(
        """
        SELECT * FROM webhook_deliveries
        WHERE tenant_id=? AND webhook_delivery_id=?
        """,
        (tenant_id, delivery_id),
    ).fetchone()


def _load_dead_letter_by_source(
    connection: sqlite3.Connection,
    tenant_id: str,
    source_delivery_id: str,
) -> sqlite3.Row | None:
    """按租户和原始失败 delivery 读取唯一根死信，失败 replay 不形成子死信。"""

    return connection.execute(
        """
        SELECT dead.*, endpoint.name AS webhook_endpoint_name
        FROM webhook_dead_letter_events dead
        JOIN webhook_endpoints endpoint
          ON endpoint.tenant_id=dead.tenant_id
         AND endpoint.webhook_endpoint_id=dead.webhook_endpoint_id
        WHERE dead.tenant_id=? AND dead.webhook_delivery_id=?
        """,
        (tenant_id, source_delivery_id),
    ).fetchone()


def _load_dead_letter_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    dead_letter_event_id: str,
) -> sqlite3.Row | None:
    """按认证租户和死信 ID 查询，跨租户与不存在保持同一结果。"""

    return connection.execute(
        """
        SELECT dead.*, endpoint.name AS webhook_endpoint_name
        FROM webhook_dead_letter_events dead
        JOIN webhook_endpoints endpoint
          ON endpoint.tenant_id=dead.tenant_id
         AND endpoint.webhook_endpoint_id=dead.webhook_endpoint_id
        WHERE dead.tenant_id=? AND dead.dead_letter_event_id=?
        """,
        (tenant_id, dead_letter_event_id),
    ).fetchone()


def _dead_letter_audit_summary(row: sqlite3.Row) -> dict[str, object]:
    """生成归档审计白名单摘要，不包含 payload、URL、幂等键、actor 个人信息或 lease。"""

    return {
        "dead_letter_event_id": row["dead_letter_event_id"],
        "webhook_delivery_id": row["webhook_delivery_id"],
        "webhook_endpoint_id": row["webhook_endpoint_id"],
        "event_type": row["event_type"],
        "status": row["status"],
        "failed_at": row["failed_at"],
        "archived_at": row["archived_at"],
    }


def _normalize_dead_letter_id(value: object) -> str:
    """严格校验死信 ID；非法格式与跨租户 ID 一律按 404 处理，避免枚举侧信道。"""

    normalized = str(value or "").strip()
    # 格式错误与跨租户资源未命中共享 404，避免利用校验差异枚举死信 ID。
    if not re.fullmatch(r"whdl_[0-9a-f]{32}", normalized):
        raise ApiError("webhook.dead_letter_not_found")
    return normalized


def _normalize_dead_letter_status(value: object) -> str:
    """校验死信状态筛选，all 表示不限制但仍强制当前 tenant_id。"""

    normalized = str(value or "all").strip().lower()
    # 状态白名单固定公开查询能力，未知值不能下沉为任意 SQL 条件。
    if normalized != "all" and normalized not in VALID_DEAD_LETTER_STATUSES:
        raise ApiError("webhook.dead_letter_filter_invalid")
    return normalized


def _normalize_dead_letter_event_type(value: object) -> str | None:
    """校验死信事件类型筛选，允许稳定目录中的内部测试事件用于故障排查。"""

    normalized = str(value or "").strip()
    if not normalized:
        return None
    # 事件类型必须来自服务端稳定目录，客户端不能借筛选探测未公开内部事件。
    if normalized not in WEBHOOK_EVENT_TYPES:
        raise ApiError("webhook.dead_letter_filter_invalid")
    return normalized


def _normalize_dead_letter_endpoint_filter(value: object) -> str | None:
    """校验 Endpoint 筛选文本，只收窄当前租户查询，不按全局 ID 回读归属。"""

    normalized = str(value or "").strip()
    if not normalized:
        return None
    # 超长值和控制字符会污染查询与日志边界，必须在接触数据库前统一拒绝。
    if len(normalized) > 80 or "\x00" in normalized:
        raise ApiError("webhook.dead_letter_filter_invalid")
    return normalized


def _normalize_dead_letter_limit(value: object) -> int:
    """限制死信每页 1-100 条，避免 attempts 关联形成无界读取。"""

    # bool 是 int 子类但不是合法分页数量，避免 true/false 被静默转换为 1/0。
    if isinstance(value, bool):
        raise ApiError("webhook.dead_letter_filter_invalid")
    try:
        normalized = int(value if value is not None else DEAD_LETTER_DEFAULT_LIMIT)
    # 类型转换失败只返回筛选错误，不能把 Python 异常或原始值带入 API。
    except (TypeError, ValueError) as exc:
        raise ApiError("webhook.dead_letter_filter_invalid") from exc
    # 有界分页限制 attempts 关联读取规模，防止单请求形成无界租户扫描。
    if normalized < 1 or normalized > DEAD_LETTER_MAX_LIMIT:
        raise ApiError("webhook.dead_letter_filter_invalid")
    return normalized


def _encode_dead_letter_cursor(failed_at: str, dead_letter_event_id: str) -> str:
    """编码只含排序键的 URL-safe cursor，不把 tenant、payload 或内部状态放入浏览器。"""

    raw = _json_dumps(
        {
            "dead_letter_event_id": dead_letter_event_id,
            "failed_at": failed_at,
        }
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_dead_letter_cursor(value: object) -> dict[str, str] | None:
    """结构化解析 seek cursor；非法 base64、JSON、键集合或排序值统一返回 422。"""

    normalized = str(value or "").strip()
    if not normalized:
        return None
    # cursor 长度先于解码受限，避免恶意 base64 输入造成无界内存和异常日志。
    if len(normalized) > DEAD_LETTER_CURSOR_MAX_LENGTH:
        raise ApiError("webhook.dead_letter_filter_invalid")
    try:
        padding = "=" * (-len(normalized) % 4)
        parsed = json.loads(
            base64.b64decode(
                normalized + padding,
                altchars=b"-_",
                validate=True,
            ).decode("utf-8")
        )
        # seek cursor 只允许两个排序键，拒绝 tenant、payload 或内部状态等额外字段。
        if not isinstance(parsed, dict) or set(parsed) != {
            "dead_letter_event_id",
            "failed_at",
        }:
            # 键集合不精确匹配即视为不可信 cursor，不能容忍额外内部字段或缺失排序键。
            raise ValueError("invalid cursor fields")
        dead_letter_id = _normalize_dead_letter_id(parsed["dead_letter_event_id"])
        failed_at = str(parsed["failed_at"])
        _parse_db_time(failed_at)
    # 解码、结构和时间错误统一为筛选错误，不能回显 cursor 原文或解析异常。
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError("webhook.dead_letter_filter_invalid") from exc
    return {
        "dead_letter_event_id": dead_letter_id,
        "failed_at": failed_at,
    }


def rotate_webhook_secret(
    config: AppConfig,
    context: AuthContext,
    endpoint_id: str,
    *,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> WebhookSecretRotateResponse:
    """原子轮换当前租户 Endpoint signing secret，并只返回一次新明文。

    参数：Endpoint、租户和操作者均来自可信路径或认证上下文，请求体不能覆盖 Secret。
    返回：不含密文的 Endpoint DTO、新 secret 和请求链路 ID。
    约束：Secret 替换与 PRD-034 管理审计同事务，任一失败都会保留旧 secret 和版本。
    """

    normalized_endpoint_id = _normalize_endpoint_id(endpoint_id)
    signing_secret = f"whsec_{secrets.token_urlsafe(32)}"
    try:
        encrypted_secret = encrypt_secret(
            signing_secret, encryption_secret=config.provider_key_encryption_secret,
        )
    # 加密失败时严禁回退到明文或沿用不确定 Secret，直接返回统一服务错误。
    except ValueError as exc:
        # 统一错误不携带加密异常文本，避免运行配置和密钥材料泄露。
        raise ApiError("webhook.secret_unavailable") from exc
    try:
        connection = _connect(config)
    # 轮换建立连接失败时旧 Secret 尚未改变，返回专用密钥服务错误。
    except ApiError as exc:
        # 轮换尚未开始写事务时连接失败，旧 Secret 不变并返回专用密钥服务错误。
        raise ApiError("webhook.secret_unavailable") from exc
    audit_record = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        endpoint = _load_endpoint_or_404(connection, context.user.tenant_id, normalized_endpoint_id)
        next_version = int(endpoint["secret_version"]) + 1
        now = _to_db_time(_utcnow())
        connection.execute(
            """
            UPDATE webhook_endpoints
            SET encrypted_secret=?, secret_prefix=?, secret_fingerprint=?, secret_version=?,
                secret_rotated_at=?, secret_rotated_by=?, updated_by=?, updated_at=?
            WHERE tenant_id=? AND webhook_endpoint_id=?
            """,
            (
                encrypted_secret,
                secret_prefix(signing_secret),
                secret_fingerprint(signing_secret),
                next_version,
                now,
                context.user.user_id,
                context.user.user_id,
                now,
                context.user.tenant_id,
                endpoint["webhook_endpoint_id"],
            ),
        )
        audit_record = record_audit_log(
            connection,
            context,
            action="webhook_endpoint.secret_rotated",
            object_type="webhook_endpoint",
            object_id=endpoint["webhook_endpoint_id"],
            object_name=str(endpoint["name"]),
            before_summary={
                "status": endpoint["status"],
                "secret_version": int(endpoint["secret_version"]),
                "secret_rotated_at": endpoint["secret_rotated_at"],
            },
            after_summary={
                "status": endpoint["status"],
                "secret_version": next_version,
                "secret_rotated_at": now,
            },
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    # 审计或租户校验失败必须与 Secret 更新一起回滚，不能留下无审计轮换。
    except ApiError:
        connection.rollback()
        raise
    # 数据库异常统一回滚并脱敏，不把 SQL、表名或密文写入响应。
    except sqlite3.Error as exc:
        connection.rollback()
        # 轮换失败继续保留旧 Secret 和版本，由调用方使用 request_id 排查。
        raise ApiError("webhook.secret_unavailable") from exc

    updated = _load_endpoint_row(connection, context.user.tenant_id, normalized_endpoint_id)
    if updated is None:
        # 提交后无法回读说明数据库状态不可靠，不能返回未确认的 Secret 元数据。
        raise ApiError("webhook.secret_unavailable")
    if audit_record is not None:
        dispatch_audit_recorded(
            config,
            context,
            audit_record,
            request_id=request_id,
            trace_id=trace_id,
        )
    _log_secret_rotated(context, updated, request_id, trace_id)
    # signing_secret 只在本次成功响应出现，Endpoint DTO 永远不含密文和完整签名材料。
    return WebhookSecretRotateResponse(
        webhook=_endpoint_info(connection, updated, include_deliveries=False),
        signing_secret=signing_secret,
        request_id=request_id,
        trace_id=trace_id,
    )


def dispatch_webhook_event(
    config: AppConfig,
    tenant_id: str,
    event: WebhookEvent,
    *,
    request_id: str,
    trace_id: str,
    resume_existing_pending: bool = False,
    source_lease_guard: Callable[[], None] | None = None,
    create_snapshot_if_missing: bool = True,
) -> list[WebhookDeliveryInfo]:
    """向当前租户订阅了事件类型的 Endpoint 投递治理事件。

    参数：resume_existing_pending 只供持有源 outbox lease 的内部 worker 使用；默认关闭，
    以保持既有预算、DLP 和调用失败事件“重复调用只回读、不再次出网”的行为。
    source_lease_guard 在每次真实网络尝试前续租或协作停服，不能用于普通直接派发。
    create_snapshot_if_missing=False 表示上游已在同库事务冻结过（包括空集合）快照，恢复时禁止
    根据当前 Endpoint 补建 intent；默认 True 保持其他既有 source outbox 的行为。
    返回：本次订阅者快照中的 delivery 摘要，终态记录不会重复发送。
    """

    # tenant_id 必须与不可变事件信封一致，调用方不能借恢复模式跨租户枚举历史 delivery。
    if tenant_id != event.tenant_id:
        raise ApiError("webhook.endpoint_invalid")
    if source_lease_guard is not None and not resume_existing_pending:
        # 只有持久 source 恢复链路拥有 lease；普通调用传入 guard 说明内部合同使用错误。
        raise ApiError("webhook.delivery_unavailable")
    if not create_snapshot_if_missing and not resume_existing_pending:
        # “禁止补快照”只对持久 resume 有定义，普通派发不能用它静默跳过订阅者。
        raise ApiError("webhook.delivery_unavailable")
    connection = _connect(config)
    try:
        if resume_existing_pending:
            # 显式恢复先一次性冻结全部 intent，再处理网络；崩溃后不会遗漏已快照的其他 Endpoint。
            return _dispatch_event_with_pending_resume(
                config,
                connection,
                tenant_id,
                event,
                request_id=request_id,
                trace_id=trace_id,
                source_lease_guard=source_lease_guard,
                create_snapshot_if_missing=create_snapshot_if_missing,
            )
        rows = connection.execute(
            """
            SELECT *
            FROM webhook_endpoints
            WHERE tenant_id=? AND status='active'
            ORDER BY created_at ASC
            """,
            (tenant_id,),
        ).fetchall()
        deliveries: list[WebhookDeliveryInfo] = []
        for row in rows:
            if event.event_type not in _event_types_from_row(row):
                continue
            # 失败事件不递归投递，避免接收端故障形成无限失败风暴。
            if event.event_type == "webhook.delivery_failed":
                continue
            deliveries.append(
                _deliver_event_to_endpoint(
                    config,
                    connection,
                    row,
                    event,
                    request_id=request_id,
                    trace_id=trace_id,
                )
            )
        return deliveries
    finally:
        # 后台派发无论成功、失败或恢复终结都显式释放连接，避免 Windows 数据库句柄残留。
        connection.close()


def _dispatch_event_with_pending_resume(
    config: AppConfig,
    connection: sqlite3.Connection,
    tenant_id: str,
    event: WebhookEvent,
    *,
    request_id: str,
    trace_id: str,
    source_lease_guard: Callable[[], None] | None,
    create_snapshot_if_missing: bool,
) -> list[WebhookDeliveryInfo]:
    """冻结或读取原始 delivery 快照，并只续发其中仍为 pending 的记录。"""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _load_original_event_deliveries(connection, tenant_id, event.event_id)
        if existing and any(row["event_type"] != event.event_type for row in existing):
            # 同一持久 event_id 不能映射到另一事件类型，否则按损坏事实失败关闭且零出网。
            raise ApiError("webhook.delivery_unavailable")
        if not existing and create_snapshot_if_missing:
            # 只有首次 claim 会读取当前 active 订阅；恢复已有快照时不追溯后来新增的订阅者。
            safe_envelope = _safe_payload(event.to_payload())
            payload_json = _json_dumps(safe_envelope)
            now = _to_db_time(_utcnow())
            endpoints = connection.execute(
                """
                SELECT * FROM webhook_endpoints
                WHERE tenant_id=? AND status='active'
                ORDER BY created_at ASC
                """,
                (tenant_id,),
            ).fetchall()
            for endpoint in endpoints:
                if event.event_type not in _event_types_from_row(endpoint):
                    continue
                if event.event_type == "webhook.delivery_failed":
                    continue
                payload_format, wire_body_text, _body = _event_wire(
                    endpoint,
                    event,
                    safe_envelope,
                )
                connection.execute(
                    """
                    INSERT INTO webhook_deliveries(
                      webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
                      event_timestamp, payload_json, status, attempt_count, max_attempts,
                      secret_version, created_at, updated_at, payload_format, wire_body_text,
                      security_event_rate_limit_per_minute
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
                    """,
                    (
                        f"whd_{uuid4().hex}",
                        tenant_id,
                        endpoint["webhook_endpoint_id"],
                        event.event_id,
                        event.event_type,
                        event.timestamp,
                        payload_json,
                        int(endpoint["max_attempts"] or MAX_ATTEMPTS),
                        int(endpoint["secret_version"]),
                        now,
                        now,
                        payload_format,
                        wire_body_text,
                        int(endpoint["security_event_rate_limit_per_minute"]),
                    ),
                )
            existing = _load_original_event_deliveries(connection, tenant_id, event.event_id)
        connection.commit()
    # 业务校验失败必须回滚完整订阅者快照，不能留下部分 intent。
    except ApiError:
        connection.rollback()
        raise
    # 数据库或存量类型异常同样回滚，并折叠为不含底层细节的统一错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        # 快照必须全有或全无；任何结构/数值异常都回滚，避免只创建部分 Endpoint intent。
        connection.rollback()
        raise ApiError("webhook.delivery_unavailable") from exc

    deliveries: list[WebhookDeliveryInfo] = []
    for delivery in existing:
        if delivery["status"] in {"success", "failed"}:
            # 终态回读不解析调用方重构 payload，更不会重复访问远端。
            deliveries.append(_delivery_info_from_row(connection, delivery, include_attempts=True))
            continue
        # 快照中只有三种数据库终态；未知状态不能猜测为 pending 后出网。
        if delivery["status"] != "pending":
            raise ApiError("webhook.delivery_unavailable")
        deliveries.append(
            _resume_pending_delivery(
                config,
                connection,
                delivery,
                request_id=request_id,
                trace_id=trace_id,
                source_lease_guard=source_lease_guard,
            )
        )
    return deliveries


def _load_original_event_deliveries(
    connection: sqlite3.Connection,
    tenant_id: str,
    event_id: str,
) -> list[sqlite3.Row]:
    """按租户和持久 event_id 读取稳定的 original delivery 快照。"""

    return connection.execute(
        """
        SELECT * FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        ORDER BY created_at ASC, webhook_delivery_id ASC
        """,
        (tenant_id, event_id),
    ).fetchall()


def freeze_webhook_event_delivery_snapshot(
    connection: sqlite3.Connection,
    event: WebhookEvent,
    *,
    frozen_at: str,
) -> int:
    """在调用方写事务中冻结 Webhook Endpoint 快照，补齐 legacy 部分 intent。

    参数：已执行 ``BEGIN IMMEDIATE`` 的同库连接、由不可变 source 重建的事件和 canonical 冻结时间。
    返回：冻结后的 original delivery 数量；零表示当时没有订阅者，但调用方仍必须持久化 marker。
    约束：既有 delivery 的租户、事件、时间和 canonical payload 必须全部一致；本函数不提交事务、
    不执行网络，也不覆盖已有 delivery ID、状态、attempt 或 Secret 版本。
    """

    if not connection.in_transaction:
        # 快照与 source marker 必须同事务提交；无调用方事务时禁止自行创建 delivery。
        raise ApiError("webhook.delivery_unavailable")
    try:
        if _to_db_time(_parse_db_time(frozen_at)) != frozen_at:
            # 非规范时间会破坏 marker、delivery 和 source 的同一冻结点，必须拒绝写入。
            raise ApiError("webhook.delivery_unavailable")
        payload_json = _json_dumps(_safe_payload(event.to_payload()))
        existing = _load_original_event_deliveries(connection, event.tenant_id, event.event_id)
        for delivery in existing:
            expected_format, expected_wire_text, _body = _event_wire(
                {"payload_format": delivery["payload_format"]},
                event,
                _safe_payload(event.to_payload()),
            )
            if (
                delivery["tenant_id"] != event.tenant_id
                or delivery["event_type"] != event.event_type
                or delivery["event_timestamp"] != event.timestamp
                or delivery["payload_json"] != payload_json
                or delivery["payload_format"] != expected_format
                or delivery["wire_body_text"] != expected_wire_text
            ):
                # 0065 遗留子集只有与 immutable source 完全一致时才能作为权威历史保留。
                raise ApiError("webhook.delivery_unavailable")
        endpoints = connection.execute(
            """
            SELECT * FROM webhook_endpoints
            WHERE tenant_id=? AND status='active'
            ORDER BY created_at ASC,webhook_endpoint_id ASC
            """,
            (event.tenant_id,),
        ).fetchall()
        expected_endpoint_ids: set[str] = set()
        for endpoint in endpoints:
            if event.event_type not in _event_types_from_row(endpoint):
                continue
            if event.event_type == "webhook.delivery_failed":
                continue
            endpoint_id = str(endpoint["webhook_endpoint_id"])
            expected_endpoint_ids.add(endpoint_id)
            payload_format, wire_body_text, _body = _event_wire(
                endpoint,
                event,
                _safe_payload(event.to_payload()),
            )
            connection.execute(
                """
                INSERT INTO webhook_deliveries(
                  webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
                  event_timestamp,payload_json,status,attempt_count,max_attempts,
                  delivery_kind,secret_version,created_at,updated_at,payload_format,
                  wire_body_text,security_event_rate_limit_per_minute
                ) VALUES(?,?,?,?,?,?,?,'pending',0,?,'original',?,?,?,?,?,?) ON CONFLICT DO NOTHING
                """,
                (
                    f"whd_{uuid4().hex}",
                    event.tenant_id,
                    endpoint_id,
                    event.event_id,
                    event.event_type,
                    event.timestamp,
                    payload_json,
                    int(endpoint["max_attempts"] or MAX_ATTEMPTS),
                    int(endpoint["secret_version"]),
                    frozen_at,
                    frozen_at,
                    payload_format,
                    wire_body_text,
                    int(endpoint["security_event_rate_limit_per_minute"]),
                ),
            )
        frozen = _load_original_event_deliveries(connection, event.tenant_id, event.event_id)
        frozen_endpoint_ids: set[str] = set()
        for delivery in frozen:
            expected_format, expected_wire_text, _body = _event_wire(
                {"payload_format": delivery["payload_format"]},
                event,
                _safe_payload(event.to_payload()),
            )
            if (
                delivery["tenant_id"] != event.tenant_id
                or delivery["event_type"] != event.event_type
                or delivery["event_timestamp"] != event.timestamp
                or delivery["payload_json"] != payload_json
                or delivery["payload_format"] != expected_format
                or delivery["wire_body_text"] != expected_wire_text
            ):
                # 补齐后再次验证全部 intent，防止唯一键冲突掩盖损坏或跨事件旧行。
                raise ApiError("webhook.delivery_unavailable")
            frozen_endpoint_ids.add(str(delivery["webhook_endpoint_id"]))
        if not expected_endpoint_ids.issubset(frozen_endpoint_ids):
            # 唯一键冲突只能由既有 intent 解释；随机 ID 冲突或并发漂移必须整体回滚。
            raise ApiError("webhook.delivery_unavailable")
        return len(frozen)
    except ApiError:
        raise
    # JSON、字段类型或 SQLite 异常可能携带配置内容，只转换为固定投递错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise ApiError("webhook.delivery_unavailable") from exc


def fail_pending_webhook_event_deliveries(
    connection: sqlite3.Connection,
    *,
    tenant_id: object,
    event_id: object,
    event_type: object,
    updated_at: str,
    failure_code: str,
) -> int:
    """在 source 终态事务中失败关闭一个事件的全部 original pending delivery。

    参数：调用方写事务连接、source tenant/event、canonical UTC 终态时间和固定失败码。
    返回：本次转为 failed 的 delivery 数量；零表示快照为空或全部已经是终态。
    约束：本函数不提交事务；delivery 更新会复用既有 trigger 原子建立死信，且禁止自由错误摘要。
    """

    summaries = {
        "webhook.delivery_unavailable": "Source delivery attempts are exhausted.",
        "webhook.payload_invalid": "Source payload validation failed.",
    }
    allowed_event_types = {
        "approval.created",
        "approval.timeout",
        "assistant.queried",
        "billing_dispute.created",
        "cache.hit",
        "cache.invalidated",
        "customer.health_changed",
        "experiment.completed",
        "file.prompt_injection_detected",
        "mcp.network_blocked",
        "mcp.tool_called",
        "quota.exhausted",
        "product_event.recorded",
        SECURITY_EVENT_TYPE,
        "trial.expired",
    }
    if (
        not connection.in_transaction
        or type(tenant_id) is not str
        or not tenant_id
        or type(event_id) is not str
        or not event_id
        or type(event_type) is not str
        or event_type not in allowed_event_types
        or failure_code not in summaries
    ):
        # 只有持有 source 写事务的内部 worker 能终结 intent，参数漂移时不能自行开启新事务。
        raise ApiError("webhook.delivery_unavailable")
    try:
        if _to_db_time(_parse_db_time(updated_at)) != updated_at:
            # source、delivery 和死信必须共享同一个 canonical 秒，禁止容错改写坏时间。
            raise ApiError("webhook.delivery_unavailable")
        cursor = connection.execute(
            """
            UPDATE webhook_deliveries
            SET status='failed',error_code=?,error_summary=?,next_retry_at=NULL,updated_at=?
            WHERE tenant_id=? AND event_id=?
              AND event_type=?
              AND delivery_kind='original' AND status='pending'
            """,
            (
                failure_code,
                summaries[failure_code],
                updated_at,
                tenant_id,
                event_id,
                event_type,
            ),
        )
        remaining = connection.execute(
            """
            SELECT COUNT(*) FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=?
              AND event_type=?
              AND delivery_kind='original' AND status='pending'
            """,
            (tenant_id, event_id, event_type),
        ).fetchone()[0]
        if remaining != 0:
            # source 一旦终态就不会再次领取；任何残留 pending 都必须让整个事务回滚。
            raise ApiError("webhook.delivery_unavailable")
        return int(cursor.rowcount)
    except ApiError:
        raise
    # SQLite、时间或行数异常可能含内部值，边界只转换为固定投递错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise ApiError("webhook.delivery_unavailable") from exc


def _resume_pending_delivery(
    config: AppConfig,
    connection: sqlite3.Connection,
    original: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    source_lease_guard: Callable[[], None] | None,
) -> WebhookDeliveryInfo:
    """使用持久正文和尝试上限，从 attempt_count + 1 续发一条 pending delivery。"""

    body = _validated_pending_delivery_body(connection, original)
    tenant_id = str(original["tenant_id"])
    endpoint_id = str(original["webhook_endpoint_id"])
    delivery_id = str(original["webhook_delivery_id"])
    while True:
        try:
            # 每个 attempt 前重读 delivery 与 Endpoint，并与 Secret 轮换使用相同写锁顺序。
            connection.execute("BEGIN IMMEDIATE")
            delivery = _load_delivery_row(connection, tenant_id, endpoint_id, delivery_id)
            # intent 在快照后消失说明数据库事实损坏，禁止创建替代 delivery。
            if delivery is None:
                raise ApiError("webhook.delivery_unavailable")
            if delivery["status"] in {"success", "failed"}:
                connection.commit()
                return _delivery_info_from_row(connection, delivery, include_attempts=True)
            max_attempts = int(delivery["max_attempts"])
            attempt_count = int(delivery["attempt_count"])
            # 持久上限和计数必须满足迁移约束，不能用 Endpoint 当前配置覆盖损坏事实。
            if not 1 <= max_attempts <= MAX_ATTEMPTS or not 0 <= attempt_count <= max_attempts:
                raise ApiError("webhook.delivery_unavailable")
            endpoint = _load_endpoint_row(connection, tenant_id, endpoint_id)
            if endpoint is None or endpoint["status"] != "active":
                # disabled 或异常缺失 Endpoint 都不能继续出网；原 pending 事实原子进入可排查终态。
                _finish_pending_without_network(
                    connection,
                    delivery,
                    endpoint,
                    error_code="webhook.endpoint_disabled",
                    error_summary="Endpoint is disabled.",
                )
                connection.commit()
                finalized = _load_delivery_row(connection, tenant_id, endpoint_id, delivery_id)
                # 原子终结后必须能按原租户和 Endpoint 回读同一 delivery。
                if finalized is None:
                    raise ApiError("webhook.delivery_unavailable")
                return _delivery_info_from_row(connection, finalized, include_attempts=True)
            if attempt_count >= max_attempts:
                # 理论上最后一次 attempt 会同步写终态；损坏的 pending 行安全终结而不是超额发送。
                _finish_pending_without_network(
                    connection,
                    delivery,
                    endpoint,
                    error_code="webhook.delivery_unavailable",
                    error_summary="Delivery attempts are exhausted.",
                )
                connection.commit()
                finalized = _load_delivery_row(connection, tenant_id, endpoint_id, delivery_id)
                # 原子终结后必须能按原租户和 Endpoint 回读同一 delivery。
                if finalized is None:
                    raise ApiError("webhook.delivery_unavailable")
                return _delivery_info_from_row(connection, finalized, include_attempts=True)
            if not _claim_security_event_rate_slot(connection, delivery, now=_utcnow()):
                # 满额只推进 next_retry_at；零 attempt、零 Secret 刷新且零网络。
                connection.commit()
                deferred = _load_delivery_row(connection, tenant_id, endpoint_id, delivery_id)
                if deferred is None:
                    raise ApiError("webhook.delivery_unavailable")
                return _delivery_info_from_row(connection, deferred, include_attempts=True)
            stored_secret_version = int(delivery["secret_version"])
            current_secret_version = int(endpoint["secret_version"])
            cursor = connection.execute(
                """
                UPDATE webhook_deliveries
                SET secret_version=?, updated_at=?
                WHERE tenant_id=? AND webhook_delivery_id=? AND status='pending'
                  AND attempt_count=? AND secret_version=?
                """,
                (
                    current_secret_version,
                    _to_db_time(_utcnow()),
                    tenant_id,
                    delivery_id,
                    attempt_count,
                    stored_secret_version,
                ),
            )
            # CAS 未命中表示 delivery 已被并发改变，当前 worker 不能继续使用陈旧 Secret。
            if cursor.rowcount != 1:
                raise ApiError("webhook.delivery_unavailable")
            connection.commit()
        # 任一业务闸门失败都释放写锁，并保持 pending 或已提交终态的真实状态。
        except ApiError:
            connection.rollback()
            raise
        # 数据库、计数或 Secret 版本类型异常统一回滚，不把原始异常写入日志。
        except (sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            # 恢复失败只公开固定错误码，由 request_id 关联内部排查。
            raise ApiError("webhook.delivery_unavailable") from exc

        attempt_no = attempt_count + 1
        if source_lease_guard is not None:
            # source 可能包含 20 个 Endpoint；每次最长 30 秒外呼前都必须独立确认 owner 并续租。
            source_lease_guard()
        attempt_result = _send_once(
            config,
            endpoint,
            body,
            delivery_id,
            event_type=str(delivery["event_type"]),
            payload_format=str(delivery["payload_format"]),
        )
        try:
            _record_attempt(connection, endpoint, delivery_id, attempt_no, attempt_result)
            _update_delivery_after_attempt(
                connection,
                endpoint,
                delivery_id,
                attempt_no,
                attempt_result,
                final=attempt_result["status"] == "success" or attempt_no >= max_attempts,
            )
        # 网络返回后 source 可能已被 reaper 终结；delivery CAS 失败必须回滚迟到 attempt。
        except ApiError:
            connection.rollback()
            raise
        # attempt 与摘要更新共享事务，SQL 故障时必须整体回滚。
        except sqlite3.Error as exc:
            # attempt 和 delivery 摘要必须同事务；失败回滚后下次仍从同一序号进行
            # at-least-once 重试。
            connection.rollback()
            raise ApiError("webhook.delivery_unavailable") from exc
        if attempt_result["status"] == "success" or attempt_no >= max_attempts:
            final_delivery = _delivery_info(
                connection,
                tenant_id,
                delivery_id,
                include_attempts=True,
            )
            # 最终 attempt 提交后必须能回读完整 attempts，否则不能返回不确定结果。
            if final_delivery is None:
                raise ApiError("webhook.delivery_unavailable")
            _log_delivery_result(endpoint, final_delivery, request_id, trace_id)
            return final_delivery


def _validated_pending_delivery_body(
    connection: sqlite3.Connection,
    delivery: sqlite3.Row,
) -> bytes:
    """重新校验 pending 的持久信封，并返回与首次 intent 完全相同的 canonical 正文。

    参数：当前租户投递连接和已经完成租户/Endpoint 归属校验的原始 delivery 行。
    返回：仅由持久信封生成的 canonical JSON 字节，不使用调用方重构的新正文。
    约束：未公开在 Webhook 正文中的 source request_id 只能从对应不可变业务事实恢复；
    禁止用 worker request_id 或占位值绕过持久事件的身份绑定校验。
    """

    try:
        envelope = _validated_stored_envelope(
            delivery["payload_json"],
            source=delivery,
            tenant_id=str(delivery["tenant_id"]),
        )
        source_request_id: str | None = None
        if envelope.get("event_type") == "error_map.updated" and isinstance(
            envelope.get("payload"), dict
        ):
            # error_map.updated 的 source_request_id 只参与构造期身份绑定，不属于公开信封字段。
            # 恢复时必须从已校验的持久 payload 取回原 request_id，不能使用本次 worker request_id，
            # 否则合法 pending delivery 会在真实 HTTP attempt 前被误判为正文损坏。
            source_request_id = envelope["payload"].get("request_id")
        elif envelope.get("event_type") == "audit.exported":
            # 审计导出 request_id 按隐私合同不进入公开八字段 payload；恢复时必须以租户、
            # event、export、actor、trace 和时间六重身份从不可变 AuditExport 取回，不能
            # 使用本轮 worker request_id，也不能为了让校验通过而伪造一个安全占位值。
            source_request_id = _audit_export_source_request_id(connection, envelope)
        elif envelope.get("event_type") == "adapter.spec_updated":
            # Adapter 更新的 source_request_id 同样不属于公开信封。pending 恢复和死信重放
            # 必须回到不可变 activation 事实取值；使用 worker ID 会让合法事件在出网前被
            # exact validator 拒绝，使用占位值则会切断激活请求与投递证据的追踪关系。
            source_request_id = _adapter_spec_source_request_id(connection, envelope)
        elif envelope.get("event_type") == "adapter.contract_failed":
            # 合同失败的 request_id 同样不公开在 Webhook 信封。恢复必须回到不可变 run，
            # 不能使用本轮 worker ID，否则会切断 CI 上报、数据库和投递的同一链路。
            source_request_id = _adapter_contract_source_request_id(connection, envelope)
        elif envelope.get("event_type") in {"batch_job.created", "batch_job.completed"}:
            # Batch request_id 只存在于不可变生命周期事件中。恢复必须重新绑定同租户的
            # event、Job、actor、trace、发生时间和完整公开摘要，禁止回退到 outbox worker ID。
            source_request_id = _batch_job_source_request_id(connection, envelope)
        elif envelope.get("event_type") == "experiment.completed":
            # The source request is intentionally absent from the public envelope. Rebuild it
            # only from the immutable Experiment, Result and source Event owner graph.
            from metahub_control.experiments.outbox import (
                recover_experiment_completed_source_request_id,
            )

            source_request_id = recover_experiment_completed_source_request_id(
                connection,
                envelope,
            )
        elif envelope.get("event_type") in {"cache.hit", "cache.invalidated"}:
            # Response Cache 的 request owner 不进入公开信封；恢复必须重新绑定 immutable
            # hit/invalidation source，不能使用 outbox worker request_id 掩盖 source 漂移。
            from metahub_control.response_cache.outbox import (
                recover_response_cache_source_request_id,
            )

            source_request_id = recover_response_cache_source_request_id(
                connection,
                envelope,
            )
        elif envelope.get("event_type") == "product_event.recorded":
            # 产品事件公开信封不含原始 request_id；恢复必须重新绑定不可变 ProductEvent。
            from metahub_control.product_analytics.outbox import (
                recover_product_event_source_request_id,
            )

            source_request_id = recover_product_event_source_request_id(
                connection,
                envelope,
            )
        elif envelope.get("event_type") == "partner.customer_created":
            # EVT-099 不公开原始 request_id；恢复必须重绑同租户 PartnerCustomer source 图。
            from metahub_control.partner_portal.outbox import (
                recover_partner_customer_source_request_id,
            )

            source_request_id = recover_partner_customer_source_request_id(
                connection,
                envelope,
            )
        elif envelope.get("event_type") == "file.prompt_injection_detected":
            # 文件安全 request_id 不进入公开七字段 payload；pending 恢复必须从同 tenant
            # 不可变 source/scan/finding 重新取得，禁止用 worker request_id 或占位值绕过绑定。
            source_request_id = _file_security_source_request_id(connection, envelope)
        elif envelope.get("event_type") == "assistant.queried":
            # 公开助手信封刻意不含原始 HTTP request_id；pending 恢复只能从不可变的
            # tenant/event/query owner 图取得，禁止信任 delivery 副本或本轮 worker 链路。
            from metahub_control.admin_assistant.outbox import (
                recover_assistant_source_request_id,
            )

            source_request_id = recover_assistant_source_request_id(connection, envelope)
        elif envelope.get("event_type") in {"approval.created", "approval.timeout"}:
            # 审批 request_id 不进入公开卡片；恢复只能回到 immutable task/event owner 图，
            # 禁止用 worker 链路或 delivery 副本替代原创建/timeout 请求。
            from metahub_control.approvals.outbox import (
                recover_approval_source_request_id,
            )

            source_request_id = recover_approval_source_request_id(connection, envelope)
        elif envelope.get("event_type") in {"quota.exhausted", "trial.expired"}:
            # Quota source request identity is private and must be recovered from the
            # immutable tenant/event owner graph before a pending delivery can resume.
            from metahub_control.plan_billing.outbox import recover_quota_source_request_id

            source_request_id = recover_quota_source_request_id(connection, envelope)
        elif envelope.get("event_type") == "billing_dispute.created":
            # 争议创建 request_id 不进入公开安全摘要；恢复只能从同租户不可变的
            # dispute/item/event owner 图取得，禁止用 worker 链路或占位值绕过身份绑定。
            from metahub_control.billing_disputes.outbox import (
                recover_billing_dispute_source_request_id,
            )

            source_request_id = recover_billing_dispute_source_request_id(
                connection,
                envelope,
            )
        elif envelope.get("event_type") == "mcp.network_blocked":
            # MCP network events keep request identity in the immutable source row,
            # not in the public webhook envelope.
            source_request_id = _mcp_network_source_request_id(connection, envelope)
        elif envelope.get("event_type") == "mcp.tool_called":
            # MCP request_id 不进入公开安全摘要；恢复必须重绑同租户不可变 Invocation
            # source，而不能使用本轮 outbox worker 链路或 delivery 副本。
            from metahub_control.mcp_governance.outbox import (
                recover_mcp_invocation_source_request_id,
            )

            source_request_id = recover_mcp_invocation_source_request_id(
                connection,
                envelope,
            )
        elif envelope.get("event_type") == SECURITY_EVENT_TYPE and isinstance(
            envelope.get("payload"), dict
        ):
            # 标准聚合事件公开 last_request_id，可直接恢复构造期身份且无需查询敏感来源表。
            source_request_id = envelope["payload"].get("last_request_id")
        rebuilt = build_webhook_event(
            tenant_id=envelope["tenant_id"],
            event_type=envelope["event_type"],
            payload=envelope["payload"],
            actor_id=envelope.get("actor_id"),
            object_id=envelope.get("object_id"),
            trace_id=envelope.get("trace_id"),
            source_request_id=source_request_id,
            event_id=envelope["event_id"],
            timestamp=_parse_db_time(envelope["timestamp"]),
        ).to_payload()
    # 历史 JSON、事件合同或时间解析失败都禁止出网，并且不回显原始 payload。
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        # 存量正文损坏不能回退到调用方新 payload，也不能泄露命中的敏感字段。
        raise ApiError("webhook.delivery_unavailable") from exc
    if rebuilt != envelope:
        raise ApiError("webhook.delivery_unavailable")
    try:
        payload_format = normalize_webhook_payload_format(delivery["payload_format"])
        if payload_format == "json":
            expected = canonical_json_bytes(envelope)
        elif envelope.get("event_type") == SECURITY_EVENT_TYPE:
            expected = security_event_cef_bytes(envelope)
        elif envelope.get("event_type") == "webhook.test":
            expected = webhook_test_cef_bytes(envelope)
        else:
            raise ValueError("webhook.format_invalid")
        stored = str(delivery["wire_body_text"]).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ApiError("webhook.delivery_unavailable") from exc
    if stored != expected:
        # 重试和 replay 必须逐字使用 intent 冻结 wire，不能从当前 Endpoint 或调用方重新解释。
        raise ApiError("webhook.delivery_unavailable")
    return stored


def _batch_job_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, Any],
) -> str:
    """从不可变 Batch 生命周期事件恢复构造期 request_id。

    参数：当前 pending delivery 的数据库连接和已通过通用敏感字段扫描的持久信封。
    返回：与同租户 event、Job、actor、trace、发生时间和完整 payload 精确绑定的 request_id。
    约束：缺失、多义、跨租户、字段漂移或数据库/JSON 错误都必须在出网前失败关闭。
    """

    event_type = envelope.get("event_type")
    payload = envelope.get("payload")
    expected_payload_fields = (
        {
            "schema_version",
            "batch_job_id",
            "operation",
            "priority",
            "task_count",
            "estimated_cost_cny",
        }
        if event_type == "batch_job.created"
        else {
            "schema_version",
            "batch_job_id",
            "status",
            "succeeded_count",
            "failed_count",
            "cancelled_count",
            "actual_cost_cny",
            "possible_cost_cny",
        }
    )
    event_id = envelope.get("event_id")
    job_id = envelope.get("object_id")
    if (
        event_type not in {"batch_job.created", "batch_job.completed"}
        or not isinstance(payload, dict)
        or set(payload) != expected_payload_fields
        or type(event_id) is not str
        or type(job_id) is not str
        or payload.get("batch_job_id") != job_id
        or type(envelope.get("tenant_id")) is not str
        or type(envelope.get("actor_id")) is not str
        or type(envelope.get("trace_id")) is not str
        or type(envelope.get("timestamp")) is not str
    ):
        # 先证明公开信封内部自洽，再允许不可变 source 为其恢复非公开请求身份。
        raise ApiError("webhook.delivery_unavailable")
    try:
        payload_json = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        rows = connection.execute(
            """
            SELECT event.source_request_id
            FROM batch_job_events event
            JOIN batch_jobs job
              ON job.tenant_id=event.tenant_id AND job.batch_job_id=event.batch_job_id
            WHERE event.tenant_id=? AND event.batch_job_event_id=?
              AND event.event_type=? AND event.batch_job_id=?
              AND event.actor_id=? AND event.source_trace_id=?
              AND event.occurred_at=? AND event.payload_json=?
            LIMIT 2
            """,
            (
                envelope.get("tenant_id"),
                event_id,
                event_type,
                job_id,
                envelope.get("actor_id"),
                envelope.get("trace_id"),
                envelope.get("timestamp"),
                payload_json,
            ),
        ).fetchall()
    # 恢复边界只能公开固定错误码，不能泄露数据库位置、SQL 或损坏的持久摘要。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise ApiError("webhook.delivery_unavailable") from exc
    if len(rows) != 1 or type(rows[0]["source_request_id"]) is not str:
        # 禁止使用本轮 worker request_id 或占位值掩盖 source 缺失、漂移或多义。
        raise ApiError("webhook.delivery_unavailable")
    return rows[0]["source_request_id"]


def _mcp_network_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, Any],
) -> str:
    """Recover the immutable source request for a network-blocked event."""

    if (
        envelope.get("event_type") != "mcp.network_blocked"
        or type(envelope.get("tenant_id")) is not str
        or type(envelope.get("event_id")) is not str
    ):
        raise ApiError("webhook.delivery_unavailable")
    try:
        rows = connection.execute(
            """
            SELECT request_id
            FROM mcp_network_events
            WHERE tenant_id=? AND mcp_network_event_id=?
            LIMIT 2
            """,
            (envelope["tenant_id"], envelope["event_id"]),
        ).fetchall()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise ApiError("webhook.delivery_unavailable") from exc
    if len(rows) != 1 or type(rows[0]["request_id"]) is not str:
        raise ApiError("webhook.delivery_unavailable")
    return rows[0]["request_id"]


def _adapter_spec_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, Any],
) -> str:
    """从不可变激活事实恢复 ``adapter.spec_updated`` 的构造期请求身份。

    参数：当前 delivery 的数据库连接和已完成通用安全扫描的持久信封。
    返回：与同租户 activation、目录身份、发布 actor、trace 和切换秒精确绑定的 request_id。
    约束：当前 activation 必须是 changed=1 的发布事实，且存在同秒被 supersede 的上一版本；
    任一字段漂移、缺失、多义或数据库错误都失败关闭，不回显 SQL、请求 ID 或持久正文。
    """

    payload = envelope.get("payload")
    activation_id = envelope.get("object_id")
    if (
        not isinstance(payload, dict)
        or type(activation_id) is not str
        or envelope.get("event_id") != f"evt_{activation_id}"
        or payload.get("activated_at") != envelope.get("timestamp")
    ):
        # 先绑定公开信封中的派生身份，避免用一个合法 activation 为另一份损坏正文背书。
        raise ApiError("webhook.delivery_unavailable")
    try:
        rows = connection.execute(
            """
            SELECT activation.request_id
            FROM provider_adapter_catalog_activations activation
            WHERE activation.tenant_id=? AND activation.activation_id=?
              AND activation.catalog_version=? AND activation.catalog_hash=?
              AND activation.spec_count=? AND activation.changed=1
              AND activation.activated_by=? AND activation.trace_id=? AND activation.activated_at=?
              AND EXISTS (
                SELECT 1
                FROM provider_adapter_catalog_activations previous
                WHERE previous.tenant_id=activation.tenant_id
                  AND previous.activation_id!=activation.activation_id
                  AND previous.catalog_version=? AND previous.status='superseded'
                  AND previous.superseded_at=activation.activated_at
              )
            LIMIT 2
            """,
            (
                envelope.get("tenant_id"),
                activation_id,
                payload.get("catalog_version"),
                payload.get("catalog_hash"),
                payload.get("spec_count"),
                envelope.get("actor_id"),
                envelope.get("trace_id"),
                envelope.get("timestamp"),
                payload.get("previous_catalog_version"),
            ),
        ).fetchall()
    # 恢复边界只公开固定错误码，数据库路径、SQL 和损坏行细节不能进入重放响应或日志。
    except sqlite3.Error as exc:
        raise ApiError("webhook.delivery_unavailable") from exc
    if len(rows) != 1 or type(rows[0]["request_id"]) is not str:
        # 不允许退回本轮 worker request_id；找不到唯一权威事实时必须在联网前停止。
        raise ApiError("webhook.delivery_unavailable")
    return rows[0]["request_id"]


def _adapter_contract_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, Any],
) -> str:
    """从不可变 run 恢复 ``adapter.contract_failed`` 的构造期 request_id。

    参数：当前 delivery 数据库连接和已通过通用安全扫描的持久信封。
    返回：与同租户 run、目录、计数、trace 和完成秒精确绑定的首次上报 request_id。
    约束：任一字段漂移、缺失、多义或数据库错误都在出网前失败关闭，不回显 SQL 或正文。
    """

    payload = envelope.get("payload")
    run_id = envelope.get("object_id")
    if (
        not isinstance(payload, dict)
        or type(run_id) is not str
        or envelope.get("event_id") != f"evt_{run_id}"
        or payload.get("run_id") != run_id
        or payload.get("completed_at") != envelope.get("timestamp")
    ):
        # 信封身份与失败摘要必须先自洽，避免查询其他 run 为损坏事件提供权威背书。
        raise ApiError("webhook.delivery_unavailable")
    try:
        rows = connection.execute(
            """
            SELECT run.source_request_id
            FROM adapter_contract_runs run
            WHERE run.tenant_id=? AND run.adapter_contract_run_id=?
              AND run.status='failed' AND run.failed_cases>0
              AND run.catalog_version=? AND run.catalog_hash=?
              AND run.suite_kind=? AND run.mode=?
              AND run.total_cases=? AND run.failed_cases=?
              AND run.failure_codes_json=?
              AND run.source_trace_id=? AND run.completed_at=?
            LIMIT 2
            """,
            (
                envelope.get("tenant_id"),
                run_id,
                payload.get("catalog_version"),
                payload.get("catalog_hash"),
                payload.get("suite_kind"),
                payload.get("mode"),
                payload.get("total_cases"),
                payload.get("failed_cases"),
                _json_dumps(payload.get("failure_codes")),
                envelope.get("trace_id"),
                envelope.get("timestamp"),
            ),
        ).fetchall()
    # 数据库或 canonical JSON 转换失败时禁止继续出网，也不回显持久字段。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        # JSON 编码或数据库失败只暴露统一投递不可用，不复制损坏 payload 或 SQL。
        raise ApiError("webhook.delivery_unavailable") from exc
    if len(rows) != 1 or type(rows[0]["source_request_id"]) is not str:
        # 找不到唯一权威 source 时禁止退回 worker request_id 或安全占位值。
        raise ApiError("webhook.delivery_unavailable")
    return rows[0]["source_request_id"]


def _audit_export_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, Any],
) -> str:
    """从不可变 AuditExport 恢复 `audit.exported` 构造期请求身份。

    参数：当前 delivery 使用的数据库连接和已通过通用敏感字段扫描的持久信封。
    返回：与同一租户、事件、导出、操作者、trace 和创建时间绑定的原始 request_id。
    约束：公开信封 payload 必须与 AuditExport 的不可变八字段摘要完全一致；缺失、多义、
    跨租户或任何数据库错误都失败关闭，且不得把 SQL、持久正文或 request_id 写入异常。
    """

    payload = envelope.get("payload")
    # audit.exported 必须携带对象型八字段摘要；其他 JSON 类型无法与权威 Export 做逐字段绑定。
    if not isinstance(payload, dict):
        raise ApiError("webhook.delivery_unavailable")
    audit_export_id = envelope.get("object_id")
    if payload.get("audit_export_id") != audit_export_id:
        # object_id 与 payload 主键必须先一致，避免用一个合法导出替另一份损坏信封背书。
        raise ApiError("webhook.delivery_unavailable")
    try:
        rows = connection.execute(
            """
            SELECT request_id,event_payload_json
            FROM audit_exports
            WHERE tenant_id=? AND event_id=? AND audit_export_id=?
              AND requested_by=? AND trace_id=? AND created_at=?
            LIMIT 2
            """,
            (
                envelope.get("tenant_id"),
                envelope.get("event_id"),
                audit_export_id,
                envelope.get("actor_id"),
                envelope.get("trace_id"),
                envelope.get("timestamp"),
            ),
        ).fetchall()
    # SQLite 原始异常可能含数据库位置或 SQL 细节，恢复边界只允许固定领域错误。
    except sqlite3.Error as exc:
        # 数据库位置、SQL 和损坏行细节不得穿透 Webhook 恢复边界。
        raise ApiError("webhook.delivery_unavailable") from exc
    if len(rows) != 1 or _json_loads(rows[0]["event_payload_json"]) != payload:
        # AuditExport 是恢复 request_id 的唯一权威；不能回退到 delivery 或本轮 worker 值。
        raise ApiError("webhook.delivery_unavailable")
    request_id = rows[0]["request_id"]
    if type(request_id) is not str:
        # 共享事件构造器还会执行字符集和长度校验；这里先拒绝 SQLite 类型混淆。
        raise ApiError("webhook.delivery_unavailable")
    return request_id


def _file_security_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, Any],
) -> str:
    """从不可变文件安全事实恢复 Prompt Injection 事件的构造期 request_id。

    参数：当前 pending delivery 的数据库连接与已通过通用安全扫描的持久信封。
    返回：与同租户 event、blocked scan、Prompt finding、actor、trace 和七字段摘要精确绑定的 ID。
    约束：缺失、多义、跨租户或任一字段漂移都在真实网络请求前失败关闭。
    """

    payload = envelope.get("payload")
    expected_fields = {
        "scan_id",
        "file_id",
        "risk_level",
        "finding_count",
        "high_finding_count",
        "model_access_status",
        "detector_version",
    }
    event_id = envelope.get("event_id")
    file_id = envelope.get("object_id")
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_fields
        or type(event_id) is not str
        or type(file_id) is not str
        or payload.get("file_id") != file_id
    ):
        # 先证明公开信封内部自洽，避免用另一个合法 source 为损坏 delivery 提供背书。
        raise ApiError("webhook.delivery_unavailable")
    try:
        rows = connection.execute(
            """
            SELECT event.source_request_id
            FROM file_security_events event
            JOIN file_security_scans scan
              ON scan.tenant_id=event.tenant_id AND scan.scan_id=event.scan_id
             AND scan.file_id=event.file_id
            WHERE event.tenant_id=? AND event.file_security_event_id=?
              AND event.event_type='file.prompt_injection_detected'
              AND event.scan_id=? AND event.file_id=? AND event.actor_user_id=?
              AND event.source_trace_id=? AND event.occurred_at=?
              AND event.risk_level=? AND event.finding_count=?
              AND event.high_finding_count=? AND event.model_access_status=?
              AND event.detector_version=?
              AND scan.status='blocked' AND scan.requested_by=event.actor_user_id
              AND scan.source_request_id=event.source_request_id
              AND scan.source_trace_id=event.source_trace_id
              AND scan.risk_level=event.risk_level
              AND scan.finding_count=event.finding_count
              AND scan.high_finding_count=event.high_finding_count
              AND scan.model_access_status=event.model_access_status
              AND scan.detector_version=event.detector_version
              AND scan.completed_at=event.occurred_at
              AND EXISTS (
                SELECT 1 FROM file_security_findings finding
                WHERE finding.tenant_id=event.tenant_id
                  AND finding.scan_id=event.scan_id AND finding.file_id=event.file_id
                  AND finding.detector_code IN (
                    'prompt.visible_instruction','ocr.prompt_instruction'
                  )
              )
            LIMIT 2
            """,
            (
                envelope.get("tenant_id"),
                event_id,
                payload.get("scan_id"),
                file_id,
                envelope.get("actor_id"),
                envelope.get("trace_id"),
                envelope.get("timestamp"),
                payload.get("risk_level"),
                payload.get("finding_count"),
                payload.get("high_finding_count"),
                payload.get("model_access_status"),
                payload.get("detector_version"),
            ),
        ).fetchall()
    # 数据库异常可能携带路径或 SQL，恢复边界只公开固定 Webhook 错误。
    except sqlite3.Error as exc:
        raise ApiError("webhook.delivery_unavailable") from exc
    if len(rows) != 1 or type(rows[0]["source_request_id"]) is not str:
        # 禁止回退到本轮 worker request_id；只有唯一权威 source 能恢复构造身份。
        raise ApiError("webhook.delivery_unavailable")
    return rows[0]["source_request_id"]


def _finish_pending_without_network(
    connection: sqlite3.Connection,
    delivery: sqlite3.Row,
    endpoint: sqlite3.Row | None,
    *,
    error_code: str,
    error_summary: str,
) -> None:
    """在当前写事务内把无法继续发送的 pending delivery 原子终结为 failed。"""

    now = _to_db_time(_utcnow())
    cursor = connection.execute(
        """
        UPDATE webhook_deliveries
        SET status='failed', error_code=?, error_summary=?, next_retry_at=NULL, updated_at=?
        WHERE tenant_id=? AND webhook_delivery_id=? AND status='pending'
        """,
        (
            error_code,
            error_summary,
            now,
            delivery["tenant_id"],
            delivery["webhook_delivery_id"],
        ),
    )
    # 只有仍为 pending 的同租户记录可被终结；并发终态不能被覆盖。
    if cursor.rowcount != 1:
        raise ApiError("webhook.delivery_unavailable")
    if endpoint is not None:
        # Endpoint 最近状态与 delivery 终态同事务更新，但不伪造一次 HTTP attempt。
        connection.execute(
            """
            UPDATE webhook_endpoints
            SET last_delivery_status='failed', last_delivery_at=?, last_failure_at=?, updated_at=?
            WHERE tenant_id=? AND webhook_endpoint_id=?
            """,
            (
                now,
                now,
                now,
                delivery["tenant_id"],
                delivery["webhook_endpoint_id"],
            ),
        )


def _deliver_event_to_endpoint(
    config: AppConfig,
    connection: sqlite3.Connection,
    endpoint: sqlite3.Row,
    event: WebhookEvent,
    *,
    request_id: str,
    trace_id: str,
) -> WebhookDeliveryInfo:
    """创建投递记录并最多尝试 3 次 HTTP POST，所有失败都写入 attempts。"""

    # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
    if endpoint["tenant_id"] != event.tenant_id:
        # 投递前再次核对租户边界，避免调用方把其他租户 Endpoint 传入该内部函数。
        raise ApiError("webhook.endpoint_not_found")
    payload = event.to_payload()
    safe_payload = _safe_payload(payload)
    delivery_id = f"whd_{uuid4().hex}"
    created_at = _to_db_time(_utcnow())
    try:
        # 与 Secret 轮换共用 BEGIN IMMEDIATE 顺序；intent 创建时重新读取当前行，
        # 禁止使用批量查询的陈旧密钥。
        connection.execute("BEGIN IMMEDIATE")
        current_endpoint = _load_endpoint_row(
            connection,
            event.tenant_id,
            endpoint["webhook_endpoint_id"],
        )
        if current_endpoint is None:
            # Endpoint 在批量读取后被删除时按租户级 404 处理，不泄露历史快照。
            raise ApiError("webhook.endpoint_not_found")
        _ensure_endpoint_active(current_endpoint)
        payload_format, wire_body_text, body = _event_wire(
            current_endpoint,
            event,
            safe_payload,
        )
        cursor = connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
              event_timestamp, payload_json, status, attempt_count, max_attempts,
              secret_version, created_at, updated_at, payload_format, wire_body_text,
              security_event_rate_limit_per_minute
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (
                delivery_id,
                current_endpoint["tenant_id"],
                current_endpoint["webhook_endpoint_id"],
                event.event_id,
                event.event_type,
                event.timestamp,
                _json_dumps(safe_payload),
                int(current_endpoint["max_attempts"] or MAX_ATTEMPTS),
                int(current_endpoint["secret_version"]),
                created_at,
                created_at,
                payload_format,
                wire_body_text,
                int(current_endpoint["security_event_rate_limit_per_minute"]),
            ),
        )
        connection.commit()
    # 租户、状态等业务校验失败必须释放写事务，不能阻塞后续轮换或投递。
    except ApiError:
        connection.rollback()
        raise
    # intent 写入异常统一回滚并脱敏，不能留下未提交 delivery 或暴露索引名称。
    except sqlite3.Error as exc:
        connection.rollback()
        # 所有数据库细节折叠为稳定投递错误码，并由中间件附加 request_id。
        raise ApiError("webhook.delivery_unavailable") from exc

    if cursor.rowcount != 1:
        # 稳定原始 event_id 已处理时直接返回既有 delivery，绝不能再次发送网络请求。
        existing = connection.execute(
            """
            SELECT * FROM webhook_deliveries
            WHERE tenant_id=? AND webhook_endpoint_id=? AND event_id=? AND delivery_kind='original'
            """,
            (event.tenant_id, current_endpoint["webhook_endpoint_id"], event.event_id),
        ).fetchone()
        if existing is None:
            # INSERT 被忽略但无法回读唯一事件说明数据库状态不可靠，禁止再次出网。
            raise ApiError("webhook.delivery_unavailable")
        return _delivery_info_from_row(connection, existing, include_attempts=True)

    return _send_pending_delivery(
        config,
        connection,
        current_endpoint,
        delivery_id,
        body,
        request_id=request_id,
        trace_id=trace_id,
    )


def _send_pending_delivery(
    config: AppConfig,
    connection: sqlite3.Connection,
    endpoint: sqlite3.Row,
    delivery_id: str,
    body: bytes,
    *,
    request_id: str,
    trace_id: str,
) -> WebhookDeliveryInfo:
    """发送一条已经持久化的 pending delivery，并写入有界 attempts。

    该原语同时服务原始控制面事件与手工 replay，避免复制第三套发送循环。
    调用前 delivery 必须已提交；网络失败只更新当前 delivery，不撤销管理审计事实。
    """

    delivery_row = _load_delivery_row(
        connection,
        str(endpoint["tenant_id"]),
        str(endpoint["webhook_endpoint_id"]),
        delivery_id,
    )
    if delivery_row is None:
        raise ApiError("webhook.delivery_unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_delivery = _load_delivery_row(
            connection,
            str(endpoint["tenant_id"]),
            str(endpoint["webhook_endpoint_id"]),
            delivery_id,
        )
        if current_delivery is None:
            raise ApiError("webhook.delivery_unavailable")
        if not _claim_security_event_rate_slot(connection, current_delivery, now=_utcnow()):
            connection.commit()
            deferred = _load_delivery_row(
                connection,
                str(endpoint["tenant_id"]),
                str(endpoint["webhook_endpoint_id"]),
                delivery_id,
            )
            if deferred is None:
                raise ApiError("webhook.delivery_unavailable")
            return _delivery_info_from_row(connection, deferred, include_attempts=True)
        connection.commit()
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        connection.rollback()
        raise ApiError("webhook.delivery_unavailable") from exc
    for attempt_no in range(1, int(endpoint["max_attempts"] or MAX_ATTEMPTS) + 1):
        attempt_result = _send_once(
            config,
            endpoint,
            body,
            delivery_id,
            event_type=str(delivery_row["event_type"]),
            payload_format=str(delivery_row["payload_format"]),
        )
        try:
            _record_attempt(
                connection,
                endpoint,
                delivery_id,
                attempt_no,
                attempt_result,
            )
            _update_delivery_after_attempt(
                connection,
                endpoint,
                delivery_id,
                attempt_no,
                attempt_result,
                final=attempt_result["status"] == "success"
                or attempt_no >= int(endpoint["max_attempts"] or MAX_ATTEMPTS),
            )
        # 重复 dispatcher 或终态并发同样不能覆盖已经提交的 delivery 结果。
        except ApiError:
            connection.rollback()
            raise
        # attempt 与摘要任一 SQL 失败都回滚当前发送事实，避免部分提交。
        except sqlite3.Error as exc:
            # attempt 与 delivery 摘要属于同一发送事实，任一写入失败必须一起回滚。
            connection.rollback()
            raise ApiError("webhook.delivery_unavailable") from exc
        if attempt_result["status"] == "success":
            break

    delivery = _delivery_info(connection, endpoint["tenant_id"], delivery_id, include_attempts=True)
    if delivery is None:
        # delivery 写入后无法回读时，调用方无法确认结果，必须返回明确的投递服务错误。
        raise ApiError("webhook.delivery_unavailable")
    _log_delivery_result(endpoint, delivery, request_id, trace_id)
    return delivery


def _claim_security_event_rate_slot(
    connection: sqlite3.Connection,
    delivery: sqlite3.Row,
    *,
    now: datetime,
) -> bool:
    """在当前写事务为标准安全 delivery 领取分钟 slot；满额仅延迟。"""

    if delivery["event_type"] != SECURITY_EVENT_TYPE:
        return True
    existing = connection.execute(
        """
        SELECT 1 FROM webhook_security_rate_claims
        WHERE tenant_id=? AND webhook_delivery_id=?
        """,
        (delivery["tenant_id"], delivery["webhook_delivery_id"]),
    ).fetchone()
    if existing is not None:
        return True
    current = now.astimezone(UTC).replace(microsecond=0)
    bucket_started = current.replace(second=0)
    bucket_ended = bucket_started + timedelta(minutes=1)
    limit = int(delivery["security_event_rate_limit_per_minute"])
    slot_number = int(
        connection.execute(
            """
            SELECT COALESCE(MAX(slot_number),0)+1
            FROM webhook_security_rate_claims
            WHERE tenant_id=? AND webhook_endpoint_id=? AND bucket_started_at=?
            """,
            (
                delivery["tenant_id"],
                delivery["webhook_endpoint_id"],
                _to_db_time(bucket_started),
            ),
        ).fetchone()[0]
    )
    if slot_number > limit:
        connection.execute(
            """
            UPDATE webhook_deliveries SET next_retry_at=?,updated_at=?
            WHERE tenant_id=? AND webhook_delivery_id=? AND status='pending'
              AND attempt_count=?
            """,
            (
                _to_db_time(bucket_ended),
                _to_db_time(current),
                delivery["tenant_id"],
                delivery["webhook_delivery_id"],
                int(delivery["attempt_count"]),
            ),
        )
        return False
    connection.execute(
        """
        INSERT INTO webhook_security_rate_claims(
          webhook_security_rate_claim_id,tenant_id,webhook_endpoint_id,webhook_delivery_id,
          bucket_started_at,bucket_ended_at,slot_number,rate_limit_per_minute,
          created_at,expires_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "wsrc_" + str(delivery["webhook_delivery_id"])[-32:],
            delivery["tenant_id"],
            delivery["webhook_endpoint_id"],
            delivery["webhook_delivery_id"],
            _to_db_time(bucket_started),
            _to_db_time(bucket_ended),
            slot_number,
            limit,
            _to_db_time(current),
            _to_db_time(bucket_ended + timedelta(days=30)),
        ),
    )
    connection.execute(
        """
        UPDATE webhook_deliveries SET next_retry_at=NULL,updated_at=?
        WHERE tenant_id=? AND webhook_delivery_id=? AND status='pending'
        """,
        (_to_db_time(current), delivery["tenant_id"], delivery["webhook_delivery_id"]),
    )
    return True


def _send_once(
    config: AppConfig,
    endpoint: sqlite3.Row,
    body: bytes,
    delivery_id: str,
    *,
    event_type: str,
    payload_format: str,
) -> dict[str, object]:
    """执行一次 HTTP POST，只读取状态码并返回可安全落库的摘要。

    参数：Endpoint 行只在内存中提供密文 Secret，delivery_id 作为稳定出站定位头。
    返回：状态、HTTP 状态码、耗时和固定错误分类。
    约束：不得读取响应正文、跟随重定向、使用系统代理或记录签名 Header。
    """

    started = time.perf_counter()
    http_status: int | None = None
    status = "failed"
    error_code: str | None = None
    error_summary: str | None = None
    try:
        signing_secret = decrypt_secret(
            str(endpoint["encrypted_secret"]),
            encryption_secret=config.provider_key_encryption_secret,
        )
        timestamp_seconds = int(time.time())
        normalized_format = normalize_webhook_payload_format(payload_format)
        headers = {
            "Content-Type": (
                "application/json" if normalized_format == "json" else "text/plain; charset=utf-8"
            ),
            "User-Agent": f"MetaHub-Webhook/{config.environment}",
            "X-MetaHub-Event": event_type,
            "X-MetaHub-Delivery": delivery_id,
            "X-MetaHub-Signature": webhook_signature_header(
                signing_secret,
                timestamp_seconds,
                body,
            ),
        }
        # Webhook 应直接访问客户配置的 Endpoint，避免系统代理污染企业内网回调和本地测试。
        with httpx.Client(
            timeout=max(float(endpoint["timeout_ms"] or DEFAULT_TIMEOUT_MS) / 1000.0, 0.1),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            # streaming 响应只读取状态码，客户接收端返回的敏感或超长正文不会进入进程内存。
            with client.stream(
                "POST",
                str(endpoint["endpoint_url"]),
                content=body,
                headers=headers,
            ) as response:
                http_status = response.status_code
        if http_status is not None and 200 <= http_status < 300:
            status = "success"
        else:
            error_code = "webhook.http_error"
            error_summary = f"Receiver returned HTTP {http_status}."
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (ValueError, TypeError):
        # Secret 或安全信封损坏也要形成失败 attempt，不能让 delivery 永久停留在 pending。
        error_code = "webhook.secret_unavailable"
        error_summary = "Signing secret is unavailable."
    except httpx.TimeoutException:
        error_code = "webhook.timeout"
        error_summary = "Receiver timed out."
    except (httpx.InvalidURL, httpx.UnsupportedProtocol):
        error_code = "webhook.invalid_url"
        error_summary = "Endpoint URL is invalid."
    # 其他 HTTP 客户端异常只记录安全摘要，不能把接收端原始响应或连接细节写入日志。
    except httpx.HTTPError:
        error_code = "webhook.http_error"
        error_summary = "Receiver is unavailable."
    duration_ms = int((time.perf_counter() - started) * 1000)
    return {
        "status": status,
        "http_status": http_status,
        "duration_ms": duration_ms,
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
    """写入一次 Webhook 发送尝试，保证失败请求也有可审计记录。"""

    connection.execute(
        """
        INSERT INTO webhook_delivery_attempts(
          webhook_delivery_attempt_id, tenant_id, webhook_delivery_id, webhook_endpoint_id,
          attempt_no, status, http_status, duration_ms, error_code, error_summary, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"whda_{uuid4().hex}",
            endpoint["tenant_id"],
            delivery_id,
            endpoint["webhook_endpoint_id"],
            attempt_no,
            result["status"],
            result["http_status"],
            result["duration_ms"],
            result["error_code"],
            _truncate_text(result["error_summary"], 240),
            _to_db_time(_utcnow()),
        ),
    )
    # commit 由紧随其后的 delivery/Endpoint 摘要更新统一执行，避免只落 attempt 的中间状态。


def _update_delivery_after_attempt(
    connection: sqlite3.Connection,
    endpoint: sqlite3.Row,
    delivery_id: str,
    attempt_no: int,
    result: dict[str, object],
    *,
    final: bool,
) -> None:
    """根据单次尝试结果更新 delivery 摘要和 Endpoint 最近状态。"""

    delivery_status = (
        "success" if result["status"] == "success" else ("failed" if final else "pending")
    )
    now = _to_db_time(_utcnow())
    cursor = connection.execute(
        """
        UPDATE webhook_deliveries
        SET status=?, attempt_count=?, http_status=?, duration_ms=?, error_code=?,
            error_summary=?, updated_at=?
        WHERE tenant_id=? AND webhook_endpoint_id=? AND webhook_delivery_id=?
          AND status='pending' AND attempt_count=? AND secret_version=?
        """,
        (
            delivery_status,
            attempt_no,
            result["http_status"],
            result["duration_ms"],
            result["error_code"],
            _truncate_text(result["error_summary"], 240),
            now,
            endpoint["tenant_id"],
            endpoint["webhook_endpoint_id"],
            delivery_id,
            attempt_no - 1,
            int(endpoint["secret_version"]),
        ),
    )
    if cursor.rowcount != 1:
        # 迟到 worker、重复 attempt 或 Secret 轮换竞态都不能覆盖已提交终态。
        raise ApiError("webhook.delivery_unavailable")
    if final:
        success_at = now if delivery_status == "success" else None
        failure_at = now if delivery_status == "failed" else None
        connection.execute(
            """
            UPDATE webhook_endpoints
            SET last_delivery_status=?, last_delivery_at=?,
                last_success_at=COALESCE(?, last_success_at),
                last_failure_at=COALESCE(?, last_failure_at), updated_at=?
            WHERE tenant_id=? AND webhook_endpoint_id=?
            """,
            (
                delivery_status,
                now,
                success_at,
                failure_at,
                now,
                endpoint["tenant_id"],
                endpoint["webhook_endpoint_id"],
            ),
        )
    connection.commit()


def _endpoint_info(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_deliveries: bool,
    event_type: str | None = None,
) -> WebhookEndpointInfo:
    """把 webhook_endpoints 数据库行转换为控制台 DTO，绝不返回 encrypted_secret。"""

    return WebhookEndpointInfo(
        webhook_endpoint_id=row["webhook_endpoint_id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        endpoint_url=row["endpoint_url"],
        event_types=_event_types_from_row(row),
        format=str(row["payload_format"]),
        security_event_rate_limit_per_minute=int(row["security_event_rate_limit_per_minute"]),
        secret_prefix=row["secret_prefix"],
        secret_fingerprint=row["secret_fingerprint"],
        secret_version=int(row["secret_version"]),
        secret_rotated_at=_parse_optional_db_time(row["secret_rotated_at"]),
        secret_rotated_by=row["secret_rotated_by"],
        status=row["status"],
        timeout_ms=int(row["timeout_ms"]),
        max_attempts=int(row["max_attempts"]),
        last_delivery_status=row["last_delivery_status"],
        last_delivery_at=_parse_optional_db_time(row["last_delivery_at"]),
        last_success_at=_parse_optional_db_time(row["last_success_at"]),
        last_failure_at=_parse_optional_db_time(row["last_failure_at"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        recent_deliveries=(
            _recent_deliveries(connection, row, event_type=event_type) if include_deliveries else []
        ),
    )


def _recent_deliveries(
    connection: sqlite3.Connection,
    endpoint: sqlite3.Row,
    *,
    event_type: str | None,
) -> list[WebhookDeliveryInfo]:
    """读取某个 Endpoint 最近投递记录，供发送记录页直接展示。"""

    clauses = ["tenant_id=?", "webhook_endpoint_id=?"]
    params: list[object] = [endpoint["tenant_id"], endpoint["webhook_endpoint_id"]]
    if event_type:
        clauses.append("event_type=?")
        params.append(event_type)
    rows = connection.execute(
        f"""
        SELECT *
        FROM webhook_deliveries
        WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [*params, RECENT_DELIVERY_LIMIT],
    ).fetchall()
    return [_delivery_info_from_row(connection, row, include_attempts=True) for row in rows]


def _delivery_info(
    connection: sqlite3.Connection,
    tenant_id: str,
    delivery_id: str,
    *,
    include_attempts: bool,
) -> WebhookDeliveryInfo | None:
    """按租户和 delivery_id 回读投递摘要，避免跨租户 ID 枚举。"""

    row = connection.execute(
        """
        SELECT *
        FROM webhook_deliveries
        WHERE tenant_id=? AND webhook_delivery_id=?
        """,
        (tenant_id, delivery_id),
    ).fetchone()
    if row is None:
        return None
    return _delivery_info_from_row(connection, row, include_attempts=include_attempts)


def _delivery_info_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_attempts: bool,
) -> WebhookDeliveryInfo:
    """把 webhook_deliveries 行转换为 DTO。

    转换时会解析安全 payload JSON，并按需附带 attempts，确保发送记录页能看到失败重试证据。
    """

    attempts = _delivery_attempts(connection, row) if include_attempts else []
    return WebhookDeliveryInfo(
        webhook_delivery_id=row["webhook_delivery_id"],
        tenant_id=row["tenant_id"],
        webhook_endpoint_id=row["webhook_endpoint_id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        event_timestamp=_parse_db_time(row["event_timestamp"]),
        payload=_json_loads(row["payload_json"]),
        format=str(row["payload_format"]),
        security_event_rate_limit_per_minute=int(row["security_event_rate_limit_per_minute"]),
        status=row["status"],
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        http_status=row["http_status"],
        duration_ms=row["duration_ms"],
        error_code=row["error_code"],
        error_summary=row["error_summary"],
        next_retry_at=_parse_optional_db_time(row["next_retry_at"]),
        delivery_kind=row["delivery_kind"],
        replayed_from_delivery_id=row["replayed_from_delivery_id"],
        replayed_by=row["replayed_by"],
        replayed_at=_parse_optional_db_time(row["replayed_at"]),
        secret_version=int(row["secret_version"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        attempts=attempts,
    )


def _delivery_record_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> WebhookDeliveryRecordInfo:
    """把 delivery 行转换为不含 payload 的详情记录，避免控制台无意回显事件正文。"""

    info = _delivery_info_from_row(connection, row, include_attempts=True)
    return WebhookDeliveryRecordInfo.model_validate(info.model_dump(exclude={"payload"}))


def _delivery_attempts(
    connection: sqlite3.Connection,
    delivery: sqlite3.Row,
) -> list[WebhookDeliveryAttemptInfo]:
    """读取 delivery 的逐次发送记录，按 attempt_no 升序返回给控制台。"""

    rows = connection.execute(
        """
        SELECT *
        FROM webhook_delivery_attempts
        WHERE tenant_id=? AND webhook_delivery_id=?
        ORDER BY attempt_no ASC
        """,
        (delivery["tenant_id"], delivery["webhook_delivery_id"]),
    ).fetchall()
    return [
        WebhookDeliveryAttemptInfo(
            webhook_delivery_attempt_id=row["webhook_delivery_attempt_id"],
            tenant_id=row["tenant_id"],
            webhook_delivery_id=row["webhook_delivery_id"],
            webhook_endpoint_id=row["webhook_endpoint_id"],
            attempt_no=int(row["attempt_no"]),
            status=row["status"],
            http_status=row["http_status"],
            duration_ms=row["duration_ms"],
            error_code=row["error_code"],
            error_summary=row["error_summary"],
            created_at=_parse_db_time(row["created_at"]),
        )
        for row in rows
    ]


def _load_endpoint_or_404(
    connection: sqlite3.Connection,
    tenant_id: str,
    endpoint_id: str,
) -> sqlite3.Row:
    """按当前租户读取 Endpoint，未找到时不区分不存在和跨租户。"""

    row = _load_endpoint_row(connection, tenant_id, _normalize_endpoint_id(endpoint_id))
    if row is None:
        # Webhook 事件必须校验租户边界和载荷合法性，异常不能跨租户投递。
        raise ApiError("webhook.endpoint_not_found")
    return row


def _load_endpoint_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    endpoint_id: str,
) -> sqlite3.Row | None:
    """按 tenant_id + endpoint_id 加载 Endpoint 原始行，供鉴权和投递复用。"""

    return connection.execute(
        """
        SELECT *
        FROM webhook_endpoints
        WHERE tenant_id=? AND webhook_endpoint_id=?
        """,
        (tenant_id, endpoint_id),
    ).fetchone()


def _load_delivery_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    endpoint_id: str,
    delivery_id: str,
) -> sqlite3.Row | None:
    """按租户、Endpoint 和 delivery 三重边界读取来源记录，防止跨 Endpoint ID 枚举。"""

    return connection.execute(
        """
        SELECT * FROM webhook_deliveries
        WHERE tenant_id=? AND webhook_endpoint_id=? AND webhook_delivery_id=?
        """,
        (tenant_id, endpoint_id, delivery_id),
    ).fetchone()


def _load_replay_by_idempotency(
    connection: sqlite3.Connection,
    tenant_id: str,
    endpoint_id: str,
    source_delivery_id: str,
    idempotency_key: str,
) -> sqlite3.Row | None:
    """读取同一租户、Endpoint、来源和幂等键的 replay，命中后不得再次出网。"""

    return connection.execute(
        """
        SELECT * FROM webhook_deliveries
        WHERE tenant_id=? AND webhook_endpoint_id=? AND delivery_kind='replay'
          AND replayed_from_delivery_id=? AND replay_idempotency_key=?
        """,
        (tenant_id, endpoint_id, source_delivery_id, idempotency_key),
    ).fetchone()


def _ensure_endpoint_active(endpoint: sqlite3.Row) -> None:
    """拒绝对 disabled Endpoint 测试或重放，停用状态不能被操作接口绕过。"""

    if endpoint["status"] != "active":
        # disabled 是明确运维闸门，测试和重放都不能通过旁路恢复出站流量。
        raise ApiError("webhook.endpoint_disabled")


def _ensure_endpoint_quota(connection: sqlite3.Connection, tenant_id: str) -> None:
    """限制单租户 Webhook Endpoint 数量，避免 M0 同步投递被大量 Endpoint 拖垮。"""

    row = connection.execute(
        "SELECT COUNT(*) AS count FROM webhook_endpoints WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    # 单租户 Endpoint 超过上限会放大同步投递成本，必须在写入前拦截。
    if int(row["count"] if row else 0) >= MAX_ENDPOINTS_PER_TENANT:
        raise ApiError("webhook.limit_exceeded")


def _normalize_name(value: object | None) -> str:
    """校验 Endpoint 名称，避免空名称或超长名称影响控制台展示。"""

    name = str(value or "").strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        # Webhook 事件必须校验租户边界和载荷合法性，异常不能跨租户投递。
        raise ApiError("webhook.endpoint_invalid")
    return name


def _normalize_endpoint_url(config: AppConfig, value: object | None) -> str:
    """校验 Webhook URL，只允许 http/https 且生产环境要求 https。"""

    endpoint_url = str(value or "").strip()
    if not endpoint_url or len(endpoint_url) > MAX_URL_LENGTH:
        # Webhook 事件必须校验租户边界和载荷合法性，异常不能跨租户投递。
        raise ApiError("webhook.endpoint_invalid")
    parsed = urlparse(endpoint_url)
    # URL scheme 和 host 是投递边界，非法值不允许进入数据库或 HTTP 客户端。
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiError("webhook.endpoint_invalid")
    if parsed.username or parsed.password:
        # URL userinfo 常被误用来携带密钥，M0 直接拒绝，避免凭据进入 URL、日志或截图。
        raise ApiError("webhook.endpoint_invalid")
    if config.environment == "prod" and parsed.scheme != "https":
        # 生产环境推送企业治理事件必须走 TLS；本地 dev/test 仍允许 http://127.0.0.1。
        raise ApiError("webhook.endpoint_invalid")
    return endpoint_url


def _normalize_event_types(value: object | None) -> list[str]:
    """校验订阅事件类型列表，去重后按字母序保存，保证响应和测试稳定。"""

    # 订阅事件为空会导致控制台误以为已接收所有事件，因此必须明确拒绝。
    if not isinstance(value, list) or not value:
        raise ApiError("webhook.event_type_invalid")
    normalized = sorted({normalize_webhook_event_type(item) for item in value})
    if not normalized or any(item not in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES for item in normalized):
        # webhook.test 和 webhook.replayed 是内部事件，不能保存为客户订阅类型。
        raise ApiError("webhook.event_type_invalid")
    return normalized


def _normalize_payload_format(value: object | None) -> str:
    """规范化 Endpoint wire 格式；缺省保持历史 JSON。"""

    try:
        return normalize_webhook_payload_format("json" if value is None else value)
    except ValueError as exc:
        raise ApiError("webhook.format_invalid") from exc


def _normalize_security_event_rate_limit(value: object | None) -> int:
    """校验 Endpoint 每分钟标准安全事件上限，不接受 bool 或隐式数值转换。"""

    if value is None:
        return DEFAULT_SECURITY_EVENT_RATE_LIMIT
    if type(value) is not int or not (
        MIN_SECURITY_EVENT_RATE_LIMIT <= value <= MAX_SECURITY_EVENT_RATE_LIMIT
    ):
        raise ApiError("webhook.security_rate_limit_invalid")
    return value


def _normalize_optional_event_type(value: object | None) -> str | None:
    """校验可选事件类型筛选条件，空值表示不过滤事件类型。"""

    event_type = str(value or "").strip()
    if not event_type:
        return None
    # 筛选条件也要走事件白名单，避免未知事件类型污染控制台查询语义。
    if event_type not in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES:
        raise ApiError("webhook.event_type_invalid")
    return event_type


def _normalize_query_status(value: object | None) -> str:
    """归一化 Endpoint 状态筛选，默认 all，其他值必须在状态白名单内。"""

    status = str(value or "all").strip().lower()
    if status == "all":
        return "all"
    return _normalize_status(status)


def _normalize_status(value: object | None) -> str:
    """校验 Endpoint 启停状态，避免半启用状态进入投递链路。"""

    status = str(value or "").strip().lower()
    if status not in VALID_STATUSES:
        # Webhook 事件必须校验租户边界和载荷合法性，异常不能跨租户投递。
        raise ApiError("webhook.endpoint_invalid")
    return status


def _normalize_timeout_ms(value: object | None) -> int:
    """校验单次 HTTP 投递超时。

    M0 限定 1-30 秒，既避免测试请求长期占用控制面线程，也避免过短超时造成企业内网回调误失败。
    """

    if value is None:
        return DEFAULT_TIMEOUT_MS
    # bool 是 int 子类，但 Webhook 超时不能把 true/false 当作毫秒值保存。
    if isinstance(value, bool):
        raise ApiError("webhook.endpoint_invalid")
    try:
        timeout_ms = int(value)
    # 超时字段解析失败要返回业务错误码，不能让 TypeError/ValueError 穿透到 API。
    except (TypeError, ValueError) as exc:
        raise ApiError("webhook.endpoint_invalid") from exc
    # 超时范围直接影响重试耗时和控制台体验，超界值必须在写入前拒绝。
    if timeout_ms < MIN_TIMEOUT_MS or timeout_ms > MAX_TIMEOUT_MS:
        raise ApiError("webhook.endpoint_invalid")
    return timeout_ms


def _normalize_endpoint_id(value: object | None) -> str:
    """校验路径中的 Webhook Endpoint ID，非法 ID 统一按未找到处理。"""

    endpoint_id = str(value or "").strip()
    if not endpoint_id or len(endpoint_id) > 80:
        # Webhook 事件必须校验租户边界和载荷合法性，异常不能跨租户投递。
        raise ApiError("webhook.endpoint_not_found")
    return endpoint_id


def _normalize_delivery_id(value: object | None) -> str:
    """校验来源 delivery ID；非法与跨租户 ID 都统一按未找到处理。"""

    delivery_id = str(value or "").strip()
    if not delivery_id or len(delivery_id) > 96:
        # 非法格式与不存在保持相同 404，避免输入校验成为 ID 枚举侧信道。
        raise ApiError("webhook.delivery_not_found")
    return delivery_id


def _normalize_dead_letter_delivery_id(value: object | None) -> str:
    """校验顶层死信 replay 的来源 delivery ID，并保持 PRD-057 的统一 404 合同。

    旧 Endpoint replay API 已公开 ``webhook.delivery_not_found``，不能直接修改通用校验器；
    新顶层 API 则把非法、缺失、跨租户和不存在统一为 ``webhook.dead_letter_not_found``，
    避免相同资源边界因路径格式不同暴露可枚举差异。
    """

    try:
        return _normalize_delivery_id(value)
    # 顶层资源边界要映射自己的稳定错误码，同时保留旧 Endpoint replay 的兼容合同。
    except ApiError as exc:
        if exc.code == "webhook.delivery_not_found":
            # 非法路径格式也与不存在、跨租户保持同形 404，避免格式差异成为枚举侧信道。
            raise ApiError("webhook.dead_letter_not_found") from exc
        raise


def _normalize_idempotency_key(value: object | None) -> str:
    """校验 replay 幂等键，限制字符和长度以保护索引与日志边界。"""

    key = str(value or "").strip()
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        # 有界 ASCII 键保证唯一索引稳定，也避免控制字符进入诊断链路。
        raise ApiError("webhook.idempotency_key_invalid")
    return key


def _normalize_delivery_event_type(value: object | None) -> str | None:
    """校验历史 delivery 事件筛选；允许内部 test/replayed 类型用于排障。"""

    event_type = str(value or "").strip()
    if not event_type:
        return None
    if event_type not in WEBHOOK_EVENT_TYPES:
        # 历史筛选只接受稳定事件目录，未知值不能形成含糊或无界查询。
        raise ApiError("webhook.delivery_filter_invalid")
    return event_type


def _normalize_delivery_status(value: object | None) -> str:
    """校验历史 delivery 状态筛选，all 表示不限制状态。"""

    status = str(value or "all").strip().lower()
    if status != "all" and status not in VALID_DELIVERY_STATUSES:
        # delivery 状态是固定枚举，拒绝任意文本避免筛选语义漂移。
        raise ApiError("webhook.delivery_filter_invalid")
    return status


def _normalize_delivery_limit(value: object) -> int:
    """把详情查询 limit 限制在 1-100，避免无界读取 attempts。"""

    if isinstance(value, bool):
        # bool 是 int 子类但不是合法分页数量，必须显式拒绝。
        raise ApiError("webhook.delivery_filter_invalid")
    try:
        limit = int(value)
    # 类型转换失败映射为业务筛选错误，不让 Python 异常穿透 API。
    except (TypeError, ValueError) as exc:
        # 统一错误不回显原始 limit 文本，避免控制台注入无关内容。
        raise ApiError("webhook.delivery_filter_invalid") from exc
    if limit < 1 or limit > DETAIL_DELIVERY_LIMIT:
        # 100 条硬上限保护 delivery 与 attempts 的查询成本。
        raise ApiError("webhook.delivery_filter_invalid")
    return limit


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """复用 shared runtime 递归安全校验，避免控制面和数据面规则漂移。"""

    try:
        return validate_webhook_payload(payload)
    # 新事件安全校验失败属于调用方构造缺陷，不能发送或落库。
    except ValueError as exc:
        # 统一为 Endpoint 载荷错误，响应不包含命中的敏感字段路径。
        raise ApiError("webhook.endpoint_invalid") from exc


def _event_wire(
    endpoint: sqlite3.Row | dict[str, object],
    event: WebhookEvent,
    safe_envelope: dict[str, Any],
) -> tuple[str, str, bytes]:
    """按 Endpoint 格式生成并冻结实际 wire，HMAC 与 HTTP 必须使用同一字节。"""

    try:
        payload_format = normalize_webhook_payload_format(endpoint["payload_format"])
        if payload_format == "json":
            body = canonical_json_bytes(safe_envelope)
        elif event.event_type == SECURITY_EVENT_TYPE:
            body = security_event_cef_bytes(safe_envelope)
        elif event.event_type == "webhook.test":
            body = webhook_test_cef_bytes(safe_envelope)
        else:
            raise ValueError("webhook.format_invalid")
        wire_body_text = body.decode("utf-8", errors="strict")
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        # 配置/事件漂移不能退化成 JSON 或替换正文，统一在零网络前失败关闭。
        raise ApiError("webhook.delivery_unavailable") from exc
    return payload_format, wire_body_text, body


def _validated_stored_envelope(
    value: object,
    *,
    source: sqlite3.Row,
    tenant_id: str,
) -> dict[str, Any]:
    """重新校验历史信封的敏感键和来源元数据一致性。

    参数：value 是存量 payload_json，source 是当前租户和 Endpoint 三重校验后的 delivery 行。
    返回：可安全重放的完整事件信封。
    约束：tenant/event/type/timestamp 任一与数据库索引事实不一致都拒绝出网，
    避免损坏历史制造审计漂移。
    """

    try:
        parsed = json.loads(str(value))
        envelope = validate_webhook_payload(parsed)
    # 历史数据即使已经落库也必须重新解析和递归验证，损坏记录禁止出网。
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        # replay 错误不回显历史 JSON 或敏感键，只保留 request_id 供后台排查。
        raise ApiError("webhook.replay_unavailable") from exc
    expected = {
        "tenant_id": tenant_id,
        "event_id": source["event_id"],
        "event_type": source["event_type"],
        "timestamp": source["event_timestamp"],
    }
    if any(envelope.get(key) != expected_value for key, expected_value in expected.items()):
        # 存量信封和索引列冲突时数据库事实不可信，不能猜测或静默修补后发送。
        raise ApiError("webhook.replay_unavailable")
    return envelope


def _event_types_from_row(row: sqlite3.Row) -> list[str]:
    """从数据库 JSON 字段读取订阅事件类型，坏数据按空列表处理避免页面整体失败。"""

    raw = _json_loads(row["event_types_json"])
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item) in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES]


def _json_dumps(value: object) -> str:
    """生成稳定 JSON 字符串，供数据库保存和测试断言使用。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: object) -> Any:
    """解析数据库 JSON 字段，解析失败返回空对象而不是泄露底层异常。"""

    try:
        return json.loads(str(value or "{}"))
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except json.JSONDecodeError:
        return {}


def _truncate_text(value: object | None, max_length: int) -> str | None:
    """裁剪可展示错误摘要，避免接收端异常文本过长影响日志和页面。"""

    if value is None:
        return None
    text = str(value)
    return text[:max_length]


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析可空数据库时间字段，空值统一返回 None。"""

    if not value:
        return None
    return _parse_db_time(value)


def _parse_db_time(value: object) -> datetime:
    """把数据库 UTC ISO 字符串转换为 datetime，兼容旧数据的 naive 时间。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 Webhook 管理使用的时间统一写成 UTC ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，避免同一测试中毫秒差异造成断言不稳定。"""

    return datetime.now(UTC).replace(microsecond=0)


def _connect(
    config: AppConfig,
    *,
    unavailable_code: str = "webhook.delivery_unavailable",
) -> sqlite3.Connection:
    """创建 Webhook 短连接，并按调用场景映射固定的基础设施错误码。"""

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 调用场景只传入代码常量；禁止把客户端值用作错误码，且不泄露数据库路径。
        raise ApiError(unavailable_code) from exc


def _log_endpoint_created(
    context: AuthContext,
    row: sqlite3.Row,
    request_id: str,
    trace_id: str,
) -> None:
    """记录 Endpoint 创建事件，payload 不包含 URL query、secret 明文或签名头。"""

    logger.info(
        "webhook.endpoint_created",
        extra={
            "event": "webhook.endpoint_created",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": row["webhook_endpoint_id"],
            "payload": {
                "event_types": _event_types_from_row(row),
                "status": row["status"],
                "timeout_ms": row["timeout_ms"],
                "secret_fingerprint": row["secret_fingerprint"],
            },
        },
    )


def _log_delivery_result(
    endpoint: sqlite3.Row,
    delivery: WebhookDeliveryInfo,
    request_id: str,
    trace_id: str,
) -> None:
    """记录 Webhook 投递结果，失败也要可追踪，但不记录 body、签名或响应正文。"""

    event_name = (
        "webhook.delivery_succeeded" if delivery.status == "success" else "webhook.delivery_failed"
    )
    logger.info(
        event_name,
        extra={
            "event": event_name,
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": delivery.tenant_id,
            "actor_id": None,
            "object_id": delivery.webhook_delivery_id,
            "payload": {
                "webhook_endpoint_id": endpoint["webhook_endpoint_id"],
                "event_id": delivery.event_id,
                "event_type": delivery.event_type,
                "status": delivery.status,
                "attempt_count": delivery.attempt_count,
                "http_status": delivery.http_status,
                "error_code": delivery.error_code,
            },
        },
    )

    if delivery.status == "failed" and delivery.delivery_kind == "original":
        # 死信事实只进入内部结构化日志；该事件已从 Endpoint 可订阅集合排除，避免失败递归。
        _log_dead_lettered(delivery, request_id, trace_id)


def _log_dead_lettered(
    delivery: WebhookDeliveryInfo,
    request_id: str,
    trace_id: str,
) -> None:
    """记录稳定 webhook.dead_lettered 内部事实，不包含 payload、URL、Secret 或响应正文。"""

    if not re.fullmatch(r"whd_[0-9a-f]{32}", delivery.webhook_delivery_id):
        # 非法历史 ID 不应伪造可关联死信 ID；数据库 trigger 会独立拒绝这种生产写入。
        return
    dead_letter_event_id = f"whdl_{delivery.webhook_delivery_id[4:]}"
    event = build_webhook_event(
        tenant_id=delivery.tenant_id,
        event_type="webhook.dead_lettered",
        event_id=f"evt_{dead_letter_event_id}",
        object_id=dead_letter_event_id,
        trace_id=trace_id,
        payload={
            "dead_letter_event_id": dead_letter_event_id,
            "source_delivery_id": delivery.webhook_delivery_id,
            "webhook_endpoint_id": delivery.webhook_endpoint_id,
            "event_id": delivery.event_id,
            "event_type": delivery.event_type,
            "error_code": delivery.error_code,
            "http_status": delivery.http_status,
            "failed_at": _to_db_time(delivery.updated_at),
        },
    )
    logger.info(
        "webhook.dead_lettered",
        extra={
            "event": "webhook.dead_lettered",
            "event_id": event.event_id,
            "timestamp": event.timestamp,
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": delivery.tenant_id,
            "actor_id": None,
            "object_id": dead_letter_event_id,
            "payload": event.payload,
        },
    )


def _log_replay_created(
    context: AuthContext,
    source_delivery_id: str,
    delivery: WebhookDeliveryInfo,
    request_id: str,
    trace_id: str,
) -> None:
    """记录稳定 `webhook.replayed` 内部事实，不投递到客户 Endpoint。

    日志只保留租户、操作者、来源/新 delivery 和结果；payload、URL、幂等键与 Secret 均不进入日志。
    """

    replay_event = build_webhook_event(
        tenant_id=context.user.tenant_id,
        event_type="webhook.replayed",
        event_id=f"evt_{delivery.webhook_delivery_id}",
        actor_id=context.user.user_id,
        object_id=delivery.webhook_delivery_id,
        trace_id=trace_id,
        payload={
            "source_delivery_id": source_delivery_id,
            "replay_delivery_id": delivery.webhook_delivery_id,
            "webhook_endpoint_id": delivery.webhook_endpoint_id,
            "event_type": delivery.event_type,
            "status": delivery.status,
            "secret_version": delivery.secret_version,
        },
    )
    logger.info(
        "webhook.replayed",
        extra={
            "event": "webhook.replayed",
            "event_id": replay_event.event_id,
            "timestamp": replay_event.timestamp,
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": delivery.webhook_delivery_id,
            "payload": replay_event.payload,
        },
    )


def _log_secret_rotated(
    context: AuthContext,
    endpoint: sqlite3.Row,
    request_id: str,
    trace_id: str,
) -> None:
    """记录 Secret 版本轮换事实，只输出版本和对象 ID，不输出任何密钥材料或 Endpoint URL。"""

    logger.info(
        "webhook.secret_rotated",
        extra={
            "event": "webhook.secret_rotated",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": endpoint["webhook_endpoint_id"],
            "payload": {
                "secret_version": int(endpoint["secret_version"]),
                "status": endpoint["status"],
            },
        },
    )
