"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/dlp/service.py
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from metahub_control.audit import (
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.safe_regex import UnsafeRegexError, compile_safe_dlp_regex

from .models import CreateDlpRuleRequest, DlpRuleInfo, DlpRuleListResponse, DlpRuleResponse, UpdateDlpRuleRequest

logger = logging.getLogger("metahub.control.dlp")

# DLP 规则类型、动作、状态和长度上限集中约束租户自定义规则，防止危险正则和越权写入。
VALID_RULE_TYPES = {"keyword", "regex"}
VALID_ACTIONS = {"allow", "log_only", "block"}
VALID_STATUSES = {"active", "disabled"}
# 方向白名单用于管理 API 和数据面 SQL；input 默认值保证历史规则不会被静默扩展到模型输出。
VALID_SCAN_SCOPES = {"input", "output", "both"}
MAX_NAME_LENGTH = 80
MAX_PATTERN_LENGTH = 512
MAX_DESCRIPTION_LENGTH = 500
MAX_RULES_PER_TENANT = 100
EDITABLE_FIELDS = {
    "name",
    "pattern",
    "action",
    "scan_scope",
    "status",
    "priority",
    "description",
}


def list_dlp_rules(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    rule_type: str | None,
    action: str | None,
    scan_scope: str | None,
    request_id: str,
    trace_id: str,
) -> DlpRuleListResponse:
    """读取当前租户的 DLP 规则列表。

    查询永远以登录态 tenant_id 为第一过滤条件，不允许前端通过 query 参数指定或猜测其他租户的 DLP 策略。
    """

    normalized_status = _normalize_query_status(status)
    normalized_rule_type = _normalize_optional_rule_type(rule_type)
    normalized_action = _normalize_optional_action(action)
    normalized_scan_scope = _normalize_optional_scan_scope(scan_scope)
    clauses = ["tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    if normalized_status != "all":
        clauses.append("status=?")
        params.append(normalized_status)
    if normalized_rule_type:
        clauses.append("rule_type=?")
        params.append(normalized_rule_type)
    if normalized_action:
        clauses.append("action=?")
        params.append(normalized_action)
    if normalized_scan_scope:
        # 方向筛选仍以 tenant_id 为首条件，不能把前端 query 当作租户边界。
        clauses.append("scan_scope=?")
        params.append(normalized_scan_scope)
    connection = _connect(config)
    rows = connection.execute(
        f"""
        SELECT *
        FROM dlp_rules
        WHERE {' AND '.join(clauses)}
        ORDER BY priority ASC, updated_at DESC, name ASC
        """,
        params,
    ).fetchall()
    return DlpRuleListResponse(
        dlp_rules=[_rule_info(row) for row in rows],
        request_id=request_id,
        trace_id=trace_id,
    )


def create_dlp_rule(
    config: AppConfig,
    context: AuthContext,
    payload: CreateDlpRuleRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
) -> DlpRuleResponse:
    """创建当前租户的 DLP 规则。

    服务端只写入认证上下文中的 tenant_id；请求体中即使携带 tenant_id 也会被 Pydantic 忽略，不能影响归属。
    """

    connection = _connect(config)
    _ensure_rule_quota(connection, context.user.tenant_id)
    rule_type = _normalize_required_rule_type(payload.rule_type)
    pattern = _normalize_pattern(rule_type, payload.pattern)
    pattern_hash = _pattern_hash(rule_type, pattern)
    now = _to_db_time(_utcnow())
    rule_id = f"dlp_{uuid4().hex}"
    try:
        connection.execute(
            """
            INSERT INTO dlp_rules(
              dlp_rule_id, tenant_id, name, rule_type, pattern, pattern_hash,
              action, scan_scope, status, priority, description, created_by, updated_by,
              created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                context.user.tenant_id,
                _normalize_name(payload.name),
                rule_type,
                pattern,
                pattern_hash,
                _normalize_required_action(payload.action),
                _normalize_scan_scope(
                    "input" if payload.scan_scope is None else payload.scan_scope
                ),
                _normalize_status(payload.status or "active"),
                _normalize_priority(payload.priority if payload.priority is not None else 100),
                _normalize_description(payload.description),
                context.user.user_id,
                context.user.user_id,
                now,
                now,
            ),
        )
        row = _load_rule(connection, context.user.tenant_id, rule_id)
        # 插入成功却无法回读时不能提交不完整业务和审计事实。
        if row is None:
            raise ApiError("dlp.status_unavailable")
        # DLP 审计只传递 pattern_hash 和治理元数据，原始 pattern/description 不进入审计参数。
        audit_record = record_audit_log(
            connection,
            context,
            action="dlp_rule.created",
            object_type="dlp_rule",
            object_id=row["dlp_rule_id"],
            object_name=row["name"],
            before_summary={},
            after_summary=_dlp_audit_summary(row),
            request_meta=_audit_request_meta(audit_request_meta),
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    # 唯一约束命中说明同租户已有相同类型和内容的规则，返回可解释业务错误而不是 SQLite 细节。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ApiError("dlp.rule_invalid") from exc
    except Exception:
        # 审计拒绝、审计表不可用或 commit 失败时，DLP 规则必须同步回滚。
        connection.rollback()
        raise
    dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    _log_rule_changed(context, row, request_id, trace_id, event="dlp.rule_created")
    return DlpRuleResponse(dlp_rule=_rule_info(row), request_id=request_id, trace_id=trace_id)


def update_dlp_rule(
    config: AppConfig,
    context: AuthContext,
    rule_id: str,
    payload: UpdateDlpRuleRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
) -> DlpRuleResponse:
    """更新当前租户的 DLP 规则。

    更新前先按 tenant_id + rule_id 回读，避免管理员通过猜测 ID 修改其他租户的安全策略。
    """

    normalized_rule_id = _normalize_rule_id(rule_id)
    provided = _provided_fields(payload, EDITABLE_FIELDS)
    if not provided:
        # 空 PATCH 无法表达有效治理变更，必须拒绝，避免控制台误报保存成功。
        raise ApiError("dlp.update_empty")
    connection = _connect(config)
    existing = _load_rule(connection, context.user.tenant_id, normalized_rule_id)
    if existing is None:
        # 404 不区分不存在和跨租户，避免暴露其他租户规则 ID 是否有效。
        raise ApiError("dlp.rule_not_found")
    updates, values = _update_values(payload, provided, existing)
    updates.extend(["updated_by=?", "updated_at=?"])
    values.extend([context.user.user_id, _to_db_time(_utcnow()), context.user.tenant_id, normalized_rule_id])
    try:
        connection.execute(
            f"""
            UPDATE dlp_rules
            SET {', '.join(updates)}
            WHERE tenant_id=? AND dlp_rule_id=?
            """,
            values,
        )
        row = _load_rule(connection, context.user.tenant_id, normalized_rule_id)
        # 更新后回读失败时回滚事务，避免返回或审计过期对象状态。
        if row is None:
            raise ApiError("dlp.rule_not_found")
        audit_record = record_audit_log(
            connection,
            context,
            action="dlp_rule.updated",
            object_type="dlp_rule",
            object_id=row["dlp_rule_id"],
            object_name=row["name"],
            before_summary=_dlp_audit_summary(existing),
            after_summary=_dlp_audit_summary(row),
            request_meta=_audit_request_meta(audit_request_meta),
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    # 修改 pattern 后可能触发同租户唯一约束，统一返回规则非法而不是泄露索引名称。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ApiError("dlp.rule_invalid") from exc
    # 任何审计、提交或未知异常都必须回滚 DLP 更新，保持业务与审计原子性。
    except Exception:
        connection.rollback()
        raise
    dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    _log_rule_changed(context, row, request_id, trace_id, event="dlp.rule_updated")
    return DlpRuleResponse(dlp_rule=_rule_info(row), request_id=request_id, trace_id=trace_id)


def _audit_request_meta(value: AuditRequestMeta | None) -> AuditRequestMeta:
    """为内部直调补齐空来源元数据，DLP 管理变更仍必须写审计。"""

    return value or AuditRequestMeta(client_ip=None, user_agent=None)


def _dlp_audit_summary(row: sqlite3.Row) -> dict[str, object]:
    """构造 DLP 审计白名单摘要，只保存不可逆 pattern_hash。"""

    return {
        "name": row["name"],
        "rule_type": row["rule_type"],
        "action": row["action"],
        "scan_scope": row["scan_scope"],
        "status": row["status"],
        "priority": int(row["priority"]),
        "pattern_hash": row["pattern_hash"],
        "description_hash": _audit_text_hash(row["description"]),
    }


def _audit_text_hash(value: str | None) -> str | None:
    """对 DLP 描述生成不可逆摘要，避免自由文本包含规则细节时扩散到审计库。"""

    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 DLP 管理数据库连接，并把基础设施错误映射为统一 DLP 状态错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # DLP 规则不可读会影响安全治理判断，控制面返回 503 便于调用方稍后重试。
        raise ApiError("dlp.status_unavailable") from exc


def _ensure_rule_quota(connection: sqlite3.Connection, tenant_id: str) -> None:
    """限制单租户 DLP 规则数量，避免数据面热路径被过多 active 规则拖慢。"""

    row = connection.execute(
        "SELECT COUNT(*) AS count FROM dlp_rules WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
    if int(row["count"] if row else 0) >= MAX_RULES_PER_TENANT:
        # M0 不做复杂规则分页加载和快照优化，因此先用硬上限保护热路径。
        raise ApiError("dlp.rule_invalid")


def _load_rule(connection: sqlite3.Connection, tenant_id: str, rule_id: str) -> sqlite3.Row | None:
    """按当前租户读取单条 DLP 规则，供创建回读和更新鉴权使用。"""

    return connection.execute(
        "SELECT * FROM dlp_rules WHERE tenant_id=? AND dlp_rule_id=?",
        (tenant_id, rule_id),
    ).fetchone()


def _update_values(
    payload: UpdateDlpRuleRequest,
    provided: set[str],
    existing: sqlite3.Row,
) -> tuple[list[str], list[object]]:
    """构造 DLP 规则更新 SQL 片段，并在 pattern 更新时复用原 rule_type 校验和重算哈希。"""

    updates: list[str] = []
    values: list[object] = []
    if "name" in provided:
        updates.append("name=?")
        values.append(_normalize_name(payload.name))
    if "pattern" in provided:
        pattern = _normalize_pattern(existing["rule_type"], payload.pattern)
        updates.extend(["pattern=?", "pattern_hash=?"])
        values.extend([pattern, _pattern_hash(existing["rule_type"], pattern)])
    if "action" in provided:
        updates.append("action=?")
        values.append(_normalize_required_action(payload.action))
    if "scan_scope" in provided:
        # 方向变更会改变输入/输出两条热路径，必须先严格校验再进入同一审计事务。
        updates.append("scan_scope=?")
        values.append(_normalize_scan_scope(payload.scan_scope))
    if "status" in provided:
        updates.append("status=?")
        values.append(_normalize_status(payload.status))
    if "priority" in provided:
        updates.append("priority=?")
        values.append(_normalize_priority(payload.priority))
    if "description" in provided:
        updates.append("description=?")
        values.append(_normalize_description(payload.description))
    return updates, values


def _provided_fields(payload: UpdateDlpRuleRequest, allowed: set[str]) -> set[str]:
    """读取 Pydantic 显式提交字段集合，兼容 v1/v2 字段集命名。"""

    if hasattr(payload, "model_fields_set"):
        raw_fields = payload.model_fields_set
    else:
        raw_fields = getattr(payload, "__fields_set__", set())
    return {field for field in raw_fields if field in allowed}


def _normalize_required_rule_type(value: object | None) -> str:
    """校验必填 DLP 规则类型，M0 只支持关键词和正则。"""

    rule_type = str(value or "").strip().lower()
    if rule_type not in VALID_RULE_TYPES:
        # 规则类型决定 pattern 的解释方式，非法值不能进入数据库。
        raise ApiError("dlp.rule_invalid")
    return rule_type


def _normalize_optional_rule_type(value: object | None) -> str | None:
    """校验可选 DLP 规则类型筛选条件，空值表示不过滤类型。"""

    rule_type = str(value or "").strip().lower()
    if not rule_type:
        return None
    if rule_type not in VALID_RULE_TYPES:
        # 类型筛选只能使用当前 DLP 白名单，避免控制台误以为存在其他扫描类型。
        raise ApiError("dlp.rule_invalid")
    return rule_type


def _normalize_required_action(value: object | None) -> str:
    """校验必填 DLP 动作，确保命中后的放行、记录或阻断语义明确。"""

    action = str(value or "").strip().lower()
    if action not in VALID_ACTIONS:
        # action 直接影响数据面是否调用上游，必须严格白名单。
        raise ApiError("dlp.rule_invalid")
    return action


def _normalize_optional_action(value: object | None) -> str | None:
    """校验可选 DLP 动作筛选条件，空值表示不过滤动作。"""

    action = str(value or "").strip().lower()
    if not action:
        return None
    if action not in VALID_ACTIONS:
        # 动作筛选只能使用 allow/log_only/block，避免无效动作进入管理查询。
        raise ApiError("dlp.rule_invalid")
    return action


def _normalize_optional_scan_scope(value: object | None) -> str | None:
    """校验列表的可选检测方向；空值表示当前租户全部方向。"""

    scan_scope = str(value or "").strip().lower()
    if not scan_scope:
        return None
    return _normalize_scan_scope(scan_scope)


def _normalize_scan_scope(value: object | None) -> str:
    """严格校验 DLP 检测方向，防止未知值被误解释为输入或双向放行。"""

    scan_scope = str(value or "").strip().lower()
    if scan_scope not in VALID_SCAN_SCOPES:
        # 非法方向会直接改变治理覆盖面，统一拒绝而不是回退到 input。
        raise ApiError("dlp.rule_invalid")
    return scan_scope


def _normalize_query_status(value: object | None) -> str:
    """归一化 DLP 规则列表状态筛选条件，默认返回全部规则。"""

    status = str(value or "all").strip().lower()
    if status == "all":
        return "all"
    if status not in VALID_STATUSES:
        # 状态筛选只能使用 active/disabled/all，非法状态统一归一为业务错误。
        raise ApiError("dlp.rule_invalid")
    return status


def _normalize_status(value: object | None) -> str:
    """校验 DLP 规则启停状态，确保数据面只加载明确 active 的规则。"""

    status = str(value or "").strip().lower()
    if status not in VALID_STATUSES:
        # 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
        raise ApiError("dlp.rule_invalid")
    return status


def _normalize_name(value: object | None) -> str:
    """校验 DLP 规则名称，避免空名称或超长名称撑破控制台表格。"""

    name = str(value or "").strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        # 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
        raise ApiError("dlp.rule_invalid")
    return name


def _normalize_pattern(rule_type: str, value: object | None) -> str:
    """按规则类型校验 pattern，并保证正则保存前已经通过安全编译。"""

    pattern = str(value or "")
    if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        # pattern 是热路径扫描核心输入，空值和超长值都不能保存。
        raise ApiError("dlp.rule_invalid")
    if rule_type == "regex":
        try:
            compile_safe_dlp_regex(pattern)
        # 正则安全检查失败必须映射为 DLP 业务错误，pattern 原文不能进入异常响应。
        except UnsafeRegexError as exc:
            if exc.reason_code == "regex.compile_invalid":
                # 语法错误和安全风险分开返回，便于控制台给出更具体的修复提示。
                raise ApiError("dlp.regex_invalid") from exc
            raise ApiError("dlp.regex_unsafe") from exc
    return pattern


def _normalize_priority(value: object | None) -> int:
    """校验 DLP 规则优先级，数字越小越先评估。"""

    if isinstance(value, bool):
        # bool 是 int 子类，但优先级不能把 true/false 当成 1/0 使用。
        raise ApiError("dlp.rule_invalid")
    try:
        priority = int(value if value is not None else 100)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (TypeError, ValueError) as exc:
        # 优先级影响热路径规则顺序，非数字输入不能进入数据库。
        raise ApiError("dlp.rule_invalid") from exc
    if priority < 1 or priority > 10000:
        # 过小或过大的优先级会让控制台排序和数据面执行顺序不可解释。
        raise ApiError("dlp.rule_invalid")
    return priority


def _normalize_description(value: object | None) -> str | None:
    """校验 DLP 规则描述，空字符串统一存为空值。"""

    if value is None:
        return None
    description = str(value).strip()
    if not description:
        return None
    if len(description) > MAX_DESCRIPTION_LENGTH:
        # 描述会展示在规则页，超长文本会影响控制台布局且没有治理价值。
        raise ApiError("dlp.rule_invalid")
    return description


def _normalize_rule_id(value: object | None) -> str:
    """校验 DLP 规则 ID，避免空路径参数进入数据库查询。"""

    rule_id = str(value or "").strip()
    if not rule_id or len(rule_id) > 80:
        # 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
        raise ApiError("dlp.rule_not_found")
    return rule_id


def _pattern_hash(rule_type: str, pattern: str) -> str:
    """生成 DLP pattern 排重哈希，哈希值不替代规则内容权限控制。"""

    digest = hashlib.sha256(f"{rule_type}:{pattern}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _rule_info(row: sqlite3.Row) -> DlpRuleInfo:
    """把 dlp_rules 数据库行转换成控制台 DTO，并只在 admin 管理接口中暴露 pattern。"""

    return DlpRuleInfo(
        dlp_rule_id=row["dlp_rule_id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        rule_type=row["rule_type"],
        pattern=row["pattern"],
        pattern_hash=row["pattern_hash"],
        action=row["action"],
        scan_scope=row["scan_scope"],
        status=row["status"],
        priority=int(row["priority"]),
        description=row["description"],
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _parse_db_time(value: object) -> datetime:
    """把数据库 UTC ISO 字符串转换为 datetime，供 Pydantic 响应序列化。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 DLP 规则写入时间统一保存为 UTC ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，避免测试中毫秒差异造成断言不稳定。"""

    return datetime.now(UTC).replace(microsecond=0)


def _log_rule_changed(context: AuthContext, row: sqlite3.Row, request_id: str, trace_id: str, *, event: str) -> None:
    """记录 DLP 规则变更事件，payload 不包含 pattern 或任何用户输入样例。"""

    logger.info(
        event,
        extra={
            "event": event,
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": row["dlp_rule_id"],
            "payload": {
                "rule_type": row["rule_type"],
                "action": row["action"],
                "scan_scope": row["scan_scope"],
                "status": row["status"],
                "priority": row["priority"],
                "pattern_hash": row["pattern_hash"],
            },
        },
    )
