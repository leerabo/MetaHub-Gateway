"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/debug_console/service.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from metahub_gateway.providers.factory import adapter_for_route
from metahub_gateway.routing.models import GatewayRoute, resolve_model_route
from metahub_gateway.usage.ledger import (
    RequestLogContext,
    claim_gateway_request,
    safe_record_request_log,
    safe_record_usage_ledger,
)
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext

from .models import (
    DebugChatCompletionRequest,
    DebugCostInfo,
    DebugMessage,
    DebugResultResponse,
    DebugUsageInfo,
)

logger = logging.getLogger("metahub.debug_console")

# 调试台输入上限、角色白名单和事件字段白名单共同限制 Prompt 预览与审计泄露面。
MAX_MESSAGE_COUNT = 20
MAX_MESSAGE_LENGTH = 4000
MAX_MODEL_ALIAS_LENGTH = 128
MAX_VIRTUAL_KEY_ID_LENGTH = 120
MAX_RESPONSE_PREVIEW_LENGTH = 2000
VALID_ROLES = {"system", "user", "assistant"}
ALLOWED_EXTRA_KEYS = {"stream"}
FORBIDDEN_EXTRA_KEYS = {
    "authorization",
    "bearer",
    "provider_key",
    "providerkey",
    "virtual_key",
    "virtualkey",
    "tenant_id",
    "tenantid",
    "token",
    "secret",
    "password",
    "key_hash",
}
EVENT_ALLOWED_KEYS = {
    "model_alias",
    "virtual_key_prefix",
    "provider_code",
    "resolved_model",
    "latency_ms",
    "log_detail_url",
}
EVENT_SENSITIVE_KEYS = FORBIDDEN_EXTRA_KEYS | {"prompt", "response", "messages", "message", "input", "output"}


@dataclass(frozen=True)
class DebugVirtualKey:
    """调试台选中的 Virtual Key 安全元数据。

    这里只保存数据库记录 ID、脱敏前缀和环境；控制面无法也不应该恢复明文 Key，因此不会把它放进请求或日志。
    """

    virtual_key_id: str
    tenant_id: str
    key_prefix: str
    environment: str
    environment_id: str
    environment_name: str
    environment_status: str


@dataclass(frozen=True)
class NormalizedDebugRequest:
    """经过业务校验后的调试请求。

    规范化后只保留 Provider Adapter 需要的 OpenAI-compatible 字段，所有越权字段和调试台不支持的字段已被拒绝。
    """

    virtual_key_id: str
    environment_id: str | None
    model_alias: str
    messages: list[dict[str, str]]
    temperature: float | None
    max_tokens: int | None


async def run_debug_chat_completion(
    config: AppConfig,
    context: AuthContext,
    payload: DebugChatCompletionRequest,
    *,
    request_id: str,
    trace_id: str,
) -> DebugResultResponse:
    """编排一次内置调试台 Chat Completions 请求。

    调试台是控制面低频能力，但仍复用数据面的模型路由、Provider Adapter、request_logs 和 usage_ledger；
    成功和失败都会尽量落审计事件，且任何持久化路径都不保存 Prompt、Response 或密钥明文。
    """

    started = time.perf_counter()
    connection = _connect(config)
    debug_request_id = f"dbg_{uuid4().hex}"
    virtual_key: DebugVirtualKey | None = None
    route: GatewayRoute | None = None
    normalized: NormalizedDebugRequest | None = None
    request_log_context: RequestLogContext | None = None
    # 只有本次 HTTP 成功创建不可变 claim 后才允许写 RequestLog；重用 request_id 时若继续
    # UPSERT，会把第一次成功/失败终态篡改成第二次 409，破坏幂等与用量关联。
    request_claimed = False

    try:
        normalized = _normalize_request(payload)
        virtual_key = _load_active_virtual_key(connection, context.user.tenant_id, normalized.virtual_key_id)
        _validate_debug_environment(normalized.environment_id, virtual_key)
        _mark_virtual_key_used(connection, virtual_key)
        # Debug Console 直接调用 Provider，不消费 Gateway Policy 快照；仍必须在网络前用
        # disabled/NULL 快照语义 claim request_id，避免重复请求改写旧日志或错绑 Usage。
        request_log_context = _partial_request_log_context(
            context,
            virtual_key,
            None,
            request_id=request_id,
            trace_id=trace_id,
            request_model=normalized.model_alias,
        )
        claim_gateway_request(config, request_log_context)
        request_claimed = True
        route = resolve_model_route(
            config,
            tenant_id=context.user.tenant_id,
            model_alias=normalized.model_alias,
            environment=virtual_key.environment,
            environment_id=virtual_key.environment_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=virtual_key.virtual_key_id,
        )
        _validate_chat_capability(route)
        request_log_context = _request_log_context(
            context,
            virtual_key,
            route,
            request_id=request_id,
            trace_id=trace_id,
            request_model=normalized.model_alias,
        )
        provider_payload = _provider_payload(normalized)
        adapter = adapter_for_route(route)
        provider_request = adapter.build_request(route, "chat/completions", provider_payload, config)
        provider_request.request_id = request_id
        provider_request.trace_id = trace_id
        _log_debug_request_received(context, virtual_key, route, request_id, trace_id, normalized)

        upstream_response = await adapter.send_non_stream(provider_request)
        latency_ms = _latency_ms(started)
        safe_record_request_log(
            config,
            request_log_context,
            status="success",
            error_code=None,
            latency_ms=latency_ms,
        )
        safe_record_usage_ledger(config, request_log_context, upstream_response.usage.as_ledger_payload())
        usage, cost = _load_usage_and_cost(connection, context.user.tenant_id, request_id, upstream_response.usage)
        _record_debug_event(
            connection,
            context,
            debug_request_id=debug_request_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key=virtual_key,
            route=route,
            model_alias=normalized.model_alias,
            status="success",
            error_code=None,
            latency_ms=latency_ms,
        )
        return DebugResultResponse(
            tenant_id=context.user.tenant_id,
            debug_request_id=debug_request_id,
            request_id=request_id,
            trace_id=trace_id,
            status="success",
            latency_ms=latency_ms,
            virtual_key_id=virtual_key.virtual_key_id,
            virtual_key_prefix=virtual_key.key_prefix,
            environment_id=virtual_key.environment_id,
            environment=virtual_key.environment,
            environment_name=virtual_key.environment_name,
            model_alias=normalized.model_alias,
            resolved_model=route.model_alias,
            provider_code=route.provider_code,
            response_json=upstream_response.payload,
            response_preview=_response_preview(upstream_response.payload),
            usage=usage,
            cost=cost,
            log_detail_url=_log_detail_url(request_id),
        )
    # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    except ApiError as exc:
        latency_ms = _latency_ms(started)
        context_for_failure = request_log_context or _partial_request_log_context(
            context,
            virtual_key,
            route,
            request_id=request_id,
            trace_id=trace_id,
            request_model=normalized.model_alias if normalized else None,
        )
        if context_for_failure is not None and request_claimed:
            # claim 失败代表该 request_id 属于先前请求，异常处理不得改写其日志终态。
            safe_record_request_log(
                config,
                context_for_failure,
                status="failed",
                error_code=exc.code,
                latency_ms=latency_ms,
            )
        _record_debug_event(
            connection,
            context,
            debug_request_id=debug_request_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key=virtual_key,
            route=route,
            model_alias=normalized.model_alias if normalized else _safe_model_alias(payload),
            status="failed",
            error_code=exc.code,
            latency_ms=latency_ms,
        )
        raise


def _normalize_request(payload: DebugChatCompletionRequest) -> NormalizedDebugRequest:
    """校验并规范化调试台请求参数。

    该函数刻意把全部参数问题归并到 debug.request_invalid，前端即可用统一错误结构展示修复建议。
    """

    _reject_extra_fields(payload)
    virtual_key_id = _required_short_text(payload.virtual_key_id, MAX_VIRTUAL_KEY_ID_LENGTH)
    environment_id = _optional_short_text(payload.environment_id, MAX_VIRTUAL_KEY_ID_LENGTH)
    model_alias = _required_short_text(payload.model_alias, MAX_MODEL_ALIAS_LENGTH)
    messages = _normalize_messages(payload.messages)
    temperature = _normalize_temperature(payload.temperature)
    max_tokens = _normalize_max_tokens(payload.max_tokens)
    return NormalizedDebugRequest(
        virtual_key_id=virtual_key_id,
        environment_id=environment_id,
        model_alias=model_alias,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _reject_extra_fields(payload: DebugChatCompletionRequest) -> None:
    """拒绝调试台不支持的附加字段，尤其是任何可能携带密钥或跨租户边界的字段。"""

    extra = dict(payload.model_extra or {})
    normalized_keys = {str(key).strip().lower() for key in extra}
    if normalized_keys & FORBIDDEN_EXTRA_KEYS:
        # 这些字段一旦进入调试台请求，就可能绕过后端租户边界或把密钥带入日志链路，必须直接拒绝。
        raise ApiError("debug.request_invalid")
    unsupported = normalized_keys - ALLOWED_EXTRA_KEYS
    if unsupported:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise ApiError("debug.request_invalid")
    if extra.get("stream") is True:
        # PRD-020 的 M0 范围只做非流式调试，流式会增加事件落库和前端增量渲染复杂度，先统一拒绝。
        raise ApiError("debug.request_invalid")


def _required_short_text(value: object, max_length: int) -> str:
    """读取必填短文本参数，避免空值或异常长字符串进入路由和审计字段。"""

    text = str(value or "").strip()
    if not text or len(text) > max_length:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise ApiError("debug.request_invalid")
    return text


def _optional_short_text(value: object | None, max_length: int) -> str | None:
    """读取可选短文本；旧调试客户端省略环境 ID 时返回 None。"""

    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > max_length:
        # 超长 Environment ID 没有业务意义，统一按调试请求参数错误处理。
        raise ApiError("debug.request_invalid")
    return text


def _normalize_messages(messages: list[DebugMessage] | None) -> list[dict[str, str]]:
    """校验 messages 数组，只允许调试台当前编辑器支持的 role/content 文本结构。"""

    if not messages or len(messages) > MAX_MESSAGE_COUNT:
        # messages 为空或过多会让调试请求不可控，统一映射为调试台参数错误。
        raise ApiError("debug.request_invalid")
    normalized: list[dict[str, str]] = []
    for message in messages:
        _reject_message_extra_fields(message)
        role = str(message.role or "").strip().lower()
        content = message.content
        # 角色判断是后端权限边界，未命中允许角色时必须统一拒绝。
        if role not in VALID_ROLES or not isinstance(content, str):
            # M0 调试台只支持三类 Chat 角色和纯文本 content，复杂多模态结构后续 PRD 再开放。
            raise ApiError("debug.request_invalid")
        content_text = content.strip()
        if not content_text or len(content_text) > MAX_MESSAGE_LENGTH:
            # 空消息无法构成有效调试请求，超长消息也不能进入低频控制面调试路径。
            raise ApiError("debug.request_invalid")
        normalized.append({"role": role, "content": content_text})
    return normalized


def _reject_message_extra_fields(message: DebugMessage) -> None:
    """拒绝 message 内的附加字段，避免把隐藏工具调用、附件或密钥片段带入 M0 调试路径。"""

    extra = dict(message.model_extra or {})
    if extra:
        # message 附加字段可能携带工具调用、附件或密钥片段，M0 调试台直接拒绝。
        raise ApiError("debug.request_invalid")


def _normalize_temperature(value: float | None) -> float | None:
    """规范化 temperature，调试台只接受 OpenAI-compatible 常见范围。"""

    if value is None:
        return None
    if value < 0 or value > 2:
        # temperature 越界通常来自表单或脚本错误，按统一参数错误提示用户修正。
        raise ApiError("debug.request_invalid")
    return value


def _normalize_max_tokens(value: int | None) -> int | None:
    """规范化 max_tokens，防止调试请求误触发异常大输出。"""

    if value is None:
        return None
    if value < 1 or value > 32768:
        # max_tokens 过小没有意义，过大可能触发不可控成本，调试台先按固定边界拒绝。
        raise ApiError("debug.request_invalid")
    return value


def _load_active_virtual_key(
    connection: sqlite3.Connection,
    tenant_id: str,
    virtual_key_id: str,
) -> DebugVirtualKey:
    """按当前登录租户加载 active Virtual Key 的脱敏元数据。

    查询必须同时带 tenant_id 和 virtual_key_id，不能让控制台用户通过猜 ID 读取其他租户 Key 元数据。
    """

    row = connection.execute(
        """
        SELECT
          vk.virtual_key_id, vk.tenant_id, vk.key_prefix, vk.environment,
          e.environment_id AS environment_id, e.name AS environment_name, e.status AS environment_status,
          vk.status
        FROM virtual_keys vk
        JOIN environments e
          ON e.tenant_id=vk.tenant_id
         AND (e.environment_id=vk.environment_id OR (vk.environment_id IS NULL AND e.code=vk.environment))
        WHERE vk.tenant_id=? AND vk.virtual_key_id=?
        """,
        (tenant_id, virtual_key_id),
    ).fetchone()
    if not row or row["status"] != "active":
        # Key 必须属于当前 tenant 且处于 active 状态，不能通过猜 ID 跨租户或使用已禁用凭据。
        raise ApiError("debug.virtual_key_invalid")
    return DebugVirtualKey(
        virtual_key_id=row["virtual_key_id"],
        tenant_id=row["tenant_id"],
        key_prefix=row["key_prefix"],
        environment=row["environment"],
        environment_id=row["environment_id"],
        environment_name=row["environment_name"],
        environment_status=row["environment_status"],
    )


def _validate_debug_environment(environment_id: str | None, virtual_key: DebugVirtualKey) -> None:
    """校验调试页面选择环境与 Virtual Key 权威环境一致。

    旧客户端未传 environment_id 时安全地从 Key 派生；新版页面显式传入后，任何不一致都在路由和上游调用前阻断。
    """

    if environment_id and environment_id != virtual_key.environment_id:
        # 前端过滤只能降低误操作，后端比较才是阻止 prod Key 进入 test 页面调用的真实边界。
        raise ApiError("debug.environment_mismatch")
    if virtual_key.environment_status != "active":
        # 禁用 Environment 不能通过调试台绕过 Gateway 的数据面状态检查。
        raise ApiError("environment.disabled")


def _mark_virtual_key_used(connection: sqlite3.Connection, virtual_key: DebugVirtualKey) -> None:
    """更新 Virtual Key 最近使用时间，帮助控制台和安全治理识别调试台触发的真实调用。"""

    now = _to_db_time(_utcnow())
    try:
        connection.execute(
            """
            UPDATE virtual_keys
            SET last_used_at=?, updated_at=?
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (now, now, virtual_key.tenant_id, virtual_key.virtual_key_id),
        )
        connection.commit()
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.Error:
        connection.rollback()
        logger.warning(
            "debug.virtual_key_last_used_update_failed",
            extra={
                "event": "debug.virtual_key_last_used_update_failed",
                "tenant_id": virtual_key.tenant_id,
                "object_id": virtual_key.virtual_key_id,
            },
        )


def _validate_chat_capability(route: GatewayRoute) -> None:
    """确认路由后的模型支持 Chat Completions 能力。"""

    # 模型路由结果决定上游调用，异常时必须用统一错误码停止。
    if route.capabilities.get("chat") is False:
        # 模型路由结果决定上游调用，异常时必须用统一错误码停止。
        raise ApiError("gateway.model_not_found")
    # 模型路由结果决定上游调用，异常时必须用统一错误码停止。
    if route.capabilities.get("stream") is False:
        # 调试台当前强制非流式；这里仅保留能力分支注释，后续若开放 stream 可复用该判断。
        return


def _request_log_context(
    context: AuthContext,
    virtual_key: DebugVirtualKey,
    route: GatewayRoute,
    *,
    request_id: str,
    trace_id: str,
    request_model: str,
) -> RequestLogContext:
    """构造完整 request_logs/usage_ledger 上下文。

    上下文只包含可审计元数据：租户、request_id、Virtual Key 记录 ID、模型和 Provider 标识；Prompt、
    Response、Authorization 与 Provider Key 都不会进入该结构。
    """

    return RequestLogContext(
        tenant_id=context.user.tenant_id,
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=virtual_key.virtual_key_id,
        environment_id=virtual_key.environment_id,
        environment_code=virtual_key.environment,
        provider_id=route.provider_id,
        model_id=route.model_id,
        endpoint="/debug/chat-completions",
        request_model=route.requested_model or request_model,
        resolved_model=route.model_alias,
        provider_code=route.provider_code,
    )


def _partial_request_log_context(
    context: AuthContext,
    virtual_key: DebugVirtualKey | None,
    route: GatewayRoute | None,
    *,
    request_id: str,
    trace_id: str,
    request_model: str | None,
) -> RequestLogContext | None:
    """在失败路径尽量补齐 request_logs 上下文。

    只有选中并校验过 Virtual Key 后才写 request_logs；纯参数错误只写 debug_console_events，避免伪造 Key ID
    被错误地当作一次真实模型调用。
    """

    if virtual_key is None:
        return None
    return RequestLogContext(
        tenant_id=context.user.tenant_id,
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=virtual_key.virtual_key_id,
        environment_id=virtual_key.environment_id,
        environment_code=virtual_key.environment,
        provider_id=route.provider_id if route else None,
        model_id=route.model_id if route else None,
        endpoint="/debug/chat-completions",
        request_model=(route.requested_model if route else request_model),
        resolved_model=route.model_alias if route else None,
        provider_code=route.provider_code if route else None,
    )


def _provider_payload(normalized: NormalizedDebugRequest) -> dict[str, Any]:
    """生成传给 Provider Adapter 的 OpenAI-compatible 请求体。

    该请求体只在内存中发送给上游 Adapter，不写入 request_logs、usage_ledger 或 debug_console_events；持久化
    路径只记录模型、Provider、usage 和成本等治理元数据。
    """

    payload: dict[str, Any] = {
        "model": normalized.model_alias,
        "messages": normalized.messages,
        "stream": False,
    }
    if normalized.temperature is not None:
        payload["temperature"] = normalized.temperature
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if normalized.max_tokens is not None:
        payload["max_tokens"] = normalized.max_tokens
    return payload


def _load_usage_and_cost(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
    upstream_usage: Any,
) -> tuple[DebugUsageInfo, DebugCostInfo]:
    """从 usage_ledger 读取本次调试请求的用量和成本摘要。"""

    row = connection.execute(
        """
        SELECT
          input_tokens, output_tokens, total_tokens, cache_tokens, cost_amount,
          cost_cny, cost_usd, currency, price_schema_version, estimated,
          usage_source, price_snapshot_id
        FROM usage_ledger
        WHERE tenant_id=? AND request_id=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (tenant_id, request_id),
    ).fetchone()
    if row:
        usage = DebugUsageInfo(
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            total_tokens=int(row["total_tokens"] or 0),
            cache_tokens=int(row["cache_tokens"] or 0),
            estimated=bool(row["estimated"]),
            usage_source=str(row["usage_source"] or "upstream"),
        )
        cost = DebugCostInfo(
            amount=str(row["cost_amount"] or "0"),
            currency=str(row["currency"] or "CNY"),
            cost_cny=str(row["cost_cny"] or "0"),
            cost_usd=str(row["cost_usd"] or "0"),
            price_schema_version=row["price_schema_version"],
            price_snapshot_id=row["price_snapshot_id"],
        )
        return usage, cost
    return (
        DebugUsageInfo(
            input_tokens=int(getattr(upstream_usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(upstream_usage, "output_tokens", 0) or 0),
            total_tokens=int(getattr(upstream_usage, "total_tokens", 0) or 0),
            cache_tokens=int(getattr(upstream_usage, "cache_tokens", 0) or 0),
            estimated=bool(getattr(upstream_usage, "estimated", False)),
            usage_source="estimated" if bool(getattr(upstream_usage, "estimated", False)) else "upstream",
        ),
        DebugCostInfo(
            amount="0",
            currency="CNY",
            cost_cny="0",
            cost_usd="0",
            price_schema_version=None,
            price_snapshot_id=None,
        ),
    )


def _record_debug_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    debug_request_id: str,
    request_id: str,
    trace_id: str,
    virtual_key: DebugVirtualKey | None,
    route: GatewayRoute | None,
    model_alias: str | None,
    status: str,
    error_code: str | None,
    latency_ms: int,
) -> None:
    """写入 debug.request_sent 审计事件，失败时只记录结构化 warning 不影响主响应。"""

    safe_payload = _safe_event_payload(
        {
            "model_alias": model_alias or "",
            "virtual_key_prefix": virtual_key.key_prefix if virtual_key else "",
            "provider_code": route.provider_code if route else "",
            "resolved_model": route.model_alias if route else "",
            "latency_ms": latency_ms,
            "log_detail_url": _log_detail_url(request_id),
        }
    )
    try:
        connection.execute(
            """
            INSERT INTO debug_console_events(
              event_id, tenant_id, user_id, session_id, event_type, debug_request_id,
              gateway_request_id, trace_id, virtual_key_id, model_alias, status,
              error_code, latency_ms, payload_json, created_at
            )
            VALUES(?, ?, ?, ?, 'debug.request_sent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"dce_{uuid4().hex}",
                context.user.tenant_id,
                context.user.user_id,
                context.session.session_id,
                debug_request_id,
                request_id,
                trace_id,
                virtual_key.virtual_key_id if virtual_key else None,
                model_alias,
                status,
                error_code,
                latency_ms,
                json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
                _to_db_time(_utcnow()),
            ),
        )
        connection.commit()
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.Error:
        connection.rollback()
        logger.warning(
            "debug.event_write_failed",
            extra={
                "event": "debug.event_write_failed",
                "tenant_id": context.user.tenant_id,
                "user_id": context.user.user_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "debug.event_write_failed",
            },
        )


def _safe_event_payload(payload: dict[str, object]) -> dict[str, object]:
    """过滤调试事件 payload，只允许短元数据进入数据库。"""

    safe: dict[str, object] = {}
    for key, value in payload.items():
        normalized_key = str(key).strip().lower()
        if normalized_key in EVENT_SENSITIVE_KEYS or normalized_key not in EVENT_ALLOWED_KEYS:
            # 调试事件采用白名单策略，任何 prompt、response、密钥或未知字段都不能进入审计表。
            raise ApiError("debug.event_invalid")
        if not isinstance(value, (str, int, float, bool)):
            # 审计 payload 只允许短标量，复杂对象容易夹带请求正文或上游响应片段。
            raise ApiError("debug.event_invalid")
        if isinstance(value, str) and len(value) > 240:
            # 过长字符串按潜在敏感内容处理，避免把 Prompt 或响应正文伪装成元数据落库。
            raise ApiError("debug.event_invalid")
        safe[normalized_key] = value
    return safe


def _response_preview(payload: dict[str, Any]) -> str:
    """生成前端展示用响应预览，不进入任何持久化路径。"""

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content[:MAX_RESPONSE_PREVIEW_LENGTH]
    preview = json.dumps(payload, ensure_ascii=False, indent=2)
    return preview[:MAX_RESPONSE_PREVIEW_LENGTH]


def _safe_model_alias(payload: DebugChatCompletionRequest) -> str | None:
    """在请求校验失败时提取可用于事件定位的短模型别名。"""

    value = str(payload.model_alias or "").strip()
    # 模型路由结果决定上游调用，异常时必须用统一错误码停止。
    if not value or len(value) > MAX_MODEL_ALIAS_LENGTH:
        return None
    return value


def _log_detail_url(request_id: str) -> str:
    """生成调用日志跳转地址，前端会据此切换到 request-logs 并按 request_id 过滤。"""

    return f"#request-logs?request_id={request_id}"


def _log_debug_request_received(
    context: AuthContext,
    virtual_key: DebugVirtualKey,
    route: GatewayRoute,
    request_id: str,
    trace_id: str,
    normalized: NormalizedDebugRequest,
) -> None:
    """记录调试请求进入后端的安全元数据，不输出 Prompt、Response 或任何密钥明文。"""

    logger.info(
        "debug.request_received",
        extra={
            "event": "debug.request_received",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": virtual_key.virtual_key_id,
            "payload": {
                "model_alias": normalized.model_alias,
                "message_count": len(normalized.messages),
                "provider_code": route.provider_code,
                "resolved_model": route.model_alias,
                "virtual_key_prefix": virtual_key.key_prefix,
            },
        },
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面数据库，并把基础设施异常映射为统一错误码。"""

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 数据库连接错误继续沿用共享错误码，不暴露路径、SQL 或底层连接细节。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _latency_ms(started: float) -> int:
    """把 perf_counter 差值转换为毫秒延迟，供响应、日志和事件保持同一口径。"""

    return int((time.perf_counter() - started) * 1000)


def _to_db_time(value: datetime) -> str:
    """把 UTC datetime 转换为数据库统一保存的 ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，保证事件和账本时间字段稳定。"""

    return datetime.now(UTC).replace(microsecond=0)
