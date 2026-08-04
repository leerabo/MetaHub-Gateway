"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/dlp/service.py
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.safe_regex import UnsafeRegexError, compile_safe_dlp_regex

from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.routing.models import GatewayRoute
from metahub_gateway.usage.ledger import RequestLogContext

from .models import DlpDecision, DlpMatch, DlpRule

logger = logging.getLogger("metahub.gateway.dlp")

# 数据面 DLP 扫描上限限制单次请求规则数和文本长度，避免正则治理拖垮网关主链路。
MAX_RULES_PER_REQUEST = 100
MAX_SCAN_CHARS = 20000


class DlpBlockedError(ApiError):
    """DLP 阻断专用错误，额外携带可写入 request_logs 的 DLP 决策摘要。"""

    def __init__(self, decision: DlpDecision) -> None:
        """创建 DLP 阻断错误，并保持对外错误码仍是统一的 gateway.dlp_blocked。"""

        super().__init__("gateway.dlp_blocked")
        self.dlp_decision = decision


def enforce_dlp_guard(
    config: AppConfig,
    auth_context: GatewayAuthContext,
    route: GatewayRoute,
    payload: dict[str, Any],
    *,
    endpoint: str,
    request_kind: str,
    request_id: str,
    trace_id: str,
) -> DlpDecision | None:
    """执行数据面 DLP 检查。

    M0 只扫描 Chat Completions 的 user messages 文本；命中摘要写入 dlp_matches，阻断动作会在调用上游前抛出统一错误。
    """

    if request_kind != "chat":
        return None
    try:
        connection = connect_database(config.database_url)
        rules = _load_rules(connection, auth_context.tenant_id)
        if not rules:
            return None
        user_texts, truncated = extract_user_texts(payload)
        if truncated:
            _log_scan_truncated(auth_context, route, endpoint, request_id, trace_id)
        matches = _match_rules(auth_context.tenant_id, rules, user_texts)
        if not matches:
            return DlpDecision(status="clean", rule_id=None, action=None, match_count=0, reason_code=None)
        _insert_matches(connection, auth_context.tenant_id, request_id, trace_id, matches)
        connection.commit()
    # DLP 规则读取或正则执行失败时采用失败关闭，避免安全保护静默失效后继续打上游。
    except (DbError, sqlite3.Error, UnsafeRegexError) as exc:
        _log_dlp_unavailable(auth_context, route, endpoint, request_id, trace_id, exc)
        raise ApiError("gateway.dlp_unavailable") from exc

    decision = _decision_from_matches(matches, _last_user_message_index(payload))
    _log_dlp_matched(auth_context, route, endpoint, request_id, trace_id, decision)
    if decision.action == "block":
        # 阻断决策随异常带出，外层失败日志才能写入 DLP 命中摘要。
        raise DlpBlockedError(decision)
    return decision


def apply_dlp_decision(context: RequestLogContext, decision: DlpDecision | None) -> RequestLogContext:
    """把 DLP 决策摘要写入 request log 上下文。

    该函数只处理规则 ID、动作、命中数量和原因码，不接触用户输入明文。
    """

    if decision is None:
        return context
    return replace(
        context,
        dlp_status=decision.status,
        dlp_rule_id=decision.rule_id,
        dlp_action=decision.action,
        dlp_match_count=decision.match_count,
        dlp_reason_code=decision.reason_code,
    )


def _load_rules(connection: sqlite3.Connection, tenant_id: str) -> list[DlpRule]:
    """按租户加载输入侧 active DLP 规则，并按原有优先级顺序供热路径执行。

    output-only 规则由模型输出扫描器负责，不能在请求进入上游前误拦截用户输入；both
    同时参与输入和输出扫描。tenant_id 始终位于查询首条件，避免跨租户加载治理规则。
    """

    rows = connection.execute(
        """
        SELECT dlp_rule_id, tenant_id, name, rule_type, pattern, action, priority
        FROM dlp_rules
        WHERE tenant_id=? AND status='active'
          AND scan_scope IN ('input', 'both')
        ORDER BY priority ASC, updated_at DESC, name ASC
        LIMIT ?
        """,
        (tenant_id, MAX_RULES_PER_REQUEST),
    ).fetchall()
    return [
        DlpRule(
            dlp_rule_id=row["dlp_rule_id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            rule_type=row["rule_type"],
            pattern=row["pattern"],
            action=row["action"],
            priority=int(row["priority"]),
        )
        for row in rows
    ]


def extract_user_texts(payload: dict[str, Any]) -> tuple[list[tuple[int, str]], bool]:
    """从 Chat messages 中提取 user 文本，并按总字符上限截断扫描范围。

    参数：已完成 Chat 结构校验且可能经过 Policy mask 的请求对象。返回：保留消息索引的
    有界文本列表与截断标志。关键约束：DLP 与意图分类必须复用本函数，才能保证角色、
    content parts 和 20,000 字符边界完全一致；返回文本只在内存中参与匹配。
    """

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return [], False
    remaining = MAX_SCAN_CHARS
    truncated = False
    texts: list[tuple[int, str]] = []
    for index, message in enumerate(messages):
        # M0 只扫描用户消息文本，system/assistant/tool 内容不进入 DLP 命中记录或日志。
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _content_text(message.get("content"))
        if not text:
            continue
        if remaining <= 0:
            truncated = True
            break
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
        texts.append((index, text))
        remaining -= len(text)
    return texts, truncated


def extract_session_user_texts(payload: dict[str, Any]) -> tuple[str, ...]:
    """提取全部 user 文本，供 Shared 一次完成历史 strong/current light 分类。

    参数：已验证且完成 Policy mask 的 Chat payload。返回：按消息顺序排列的文本 tuple。
    关键约束：不记录、缓存或持久化正文；Shared 自行执行 20,000 code points 的有界采样，
    current-turn 完整投影另由 256 KiB HMAC 门禁约束。
    """

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ()
    texts: list[str] = []
    for message in messages:
        # 只允许 user 角色正文进入风险分类，系统与工具消息不能被客户端借机混入当前轮。
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _content_text(message.get("content"))
        if text:
            texts.append(text)
    return tuple(texts)


def _content_text(content: object) -> str:
    """提取 OpenAI-compatible 文本内容，忽略图片、文件或非文本 part。"""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _match_rules(tenant_id: str, rules: list[DlpRule], user_texts: list[tuple[int, str]]) -> list[DlpMatch]:
    """按规则顺序扫描用户文本，返回每条规则的第一处命中摘要。"""

    matches: list[DlpMatch] = []
    for rule in rules:
        for message_index, text in user_texts:
            match_text = _match_rule(rule, text)
            if match_text is None:
                continue
            # 命中文本只用于后续哈希和长度计算，不能写入日志或数据库。
            matches.append(DlpMatch(rule=rule, message_index=message_index, match_text=match_text))
            break
    return matches


def _match_rule(rule: DlpRule, text: str) -> str | None:
    """执行单条 DLP 规则匹配，关键词按普通字符串处理，正则按 shared 安全编译结果执行。"""

    if rule.rule_type == "keyword":
        return rule.pattern if rule.pattern in text else None
    compiled = compile_safe_dlp_regex(rule.pattern).compiled
    result = compiled.search(text)
    if result is None:
        return None
    return result.group(0)


def _insert_matches(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    matches: list[DlpMatch],
) -> None:
    """写入 DLP 命中摘要记录，数据库中不保存命中文本明文。"""

    now = _to_db_time(_utcnow())
    for match in matches:
        connection.execute(
            """
            INSERT INTO dlp_matches(
              dlp_match_id, tenant_id, request_id, trace_id, dlp_rule_id,
              rule_name, rule_type, action, message_index, match_text_hash,
              match_length, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"dlpm_{uuid4().hex}",
                tenant_id,
                request_id,
                trace_id,
                match.rule.dlp_rule_id,
                match.rule.name,
                match.rule.rule_type,
                match.rule.action,
                match.message_index,
                _match_hash(tenant_id, match.rule.dlp_rule_id, match.match_text),
                len(match.match_text),
                now,
            ),
        )


def _decision_from_matches(
    matches: list[DlpMatch],
    current_turn_message_index: int | None,
) -> DlpDecision:
    """生成请求 DLP 决策，并单独统计最后一条 user turn 的命中数。"""

    blocking = next((match for match in matches if match.rule.action == "block"), None)
    effective = blocking or matches[0]
    current_matches = [
        match
        for match in matches
        if current_turn_message_index is not None
        and match.message_index == current_turn_message_index
    ]
    # 历史 block 仍可阻断当前 HTTP 请求，但绝不能冒充当前 turn 的 block 风险重复累计。
    current_action = (
        "block"
        if any(match.rule.action == "block" for match in current_matches)
        else ("log_only" if current_matches else "allow")
    )
    status = "blocked" if effective.rule.action == "block" else "matched"
    return DlpDecision(
        status=status,
        rule_id=effective.rule.dlp_rule_id,
        action=effective.rule.action,
        match_count=len(matches),
        reason_code=f"dlp.{effective.rule.rule_type}_matched",
        current_turn_match_count=len(current_matches),
        current_turn_action=current_action,
    )


def _last_user_message_index(payload: dict[str, Any]) -> int | None:
    """返回最后一条 user message 索引，不受 DLP 总扫描上限截断影响。"""

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        # 最后一条可信 user 消息定义 current turn，其他角色不得影响 DLP 当前轮计数。
        if isinstance(message, dict) and message.get("role") == "user":
            return index
    return None


def _match_hash(tenant_id: str, rule_id: str, match_text: str) -> str:
    """生成命中文本稳定摘要，加入租户和规则 ID 降低跨租户字典反推风险。"""

    digest = hashlib.sha256(f"{tenant_id}:{rule_id}:{match_text}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _log_dlp_matched(
    auth_context: GatewayAuthContext,
    route: GatewayRoute,
    endpoint: str,
    request_id: str,
    trace_id: str,
    decision: DlpDecision,
) -> None:
    """记录 DLP 命中安全事件，payload 不包含 pattern 或命中文本。"""

    logger.info(
        "security.dlp_matched",
        extra={
            "event": "security.dlp_matched",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": auth_context.tenant_id,
            "actor_id": auth_context.virtual_key_id,
            "object_id": decision.rule_id,
            "payload": {
                "endpoint": endpoint,
                "model_id": route.model_id,
                "provider_id": route.provider_id,
                "rule_id": decision.rule_id,
                "action": decision.action,
                "status": decision.status,
                "match_count": decision.match_count,
                "reason_code": decision.reason_code,
            },
        },
    )


def _log_scan_truncated(
    auth_context: GatewayAuthContext,
    route: GatewayRoute,
    endpoint: str,
    request_id: str,
    trace_id: str,
) -> None:
    """记录 DLP 扫描截断事件，只说明长度上限命中，不输出被截断内容。"""

    logger.info(
        "security.dlp_scan_truncated",
        extra={
            "event": "security.dlp_scan_truncated",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": auth_context.tenant_id,
            "actor_id": auth_context.virtual_key_id,
            "object_id": request_id,
            "payload": {"endpoint": endpoint, "model_id": route.model_id, "max_scan_chars": MAX_SCAN_CHARS},
        },
    )


def _log_dlp_unavailable(
    auth_context: GatewayAuthContext,
    route: GatewayRoute,
    endpoint: str,
    request_id: str,
    trace_id: str,
    exc: Exception,
) -> None:
    """记录 DLP 不可用事件，异常摘要只包含类型，不包含规则内容或用户输入。"""

    logger.warning(
        "security.dlp_unavailable",
        extra={
            "event": "security.dlp_unavailable",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": auth_context.tenant_id,
            "actor_id": auth_context.virtual_key_id,
            "object_id": request_id,
            "error_code": "gateway.dlp_unavailable",
            "payload": {
                "endpoint": endpoint,
                "model_id": route.model_id,
                "provider_id": route.provider_id,
                "exception_type": exc.__class__.__name__,
            },
        },
    )


def _to_db_time(value: datetime) -> str:
    """把 DLP 命中时间统一写成 UTC ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，避免测试中毫秒差异造成 DLP 记录排序不稳定。"""

    return datetime.now(UTC).replace(microsecond=0)
