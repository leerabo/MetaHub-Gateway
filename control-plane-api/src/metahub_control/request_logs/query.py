"""PRD-055 调用日志高级查询、稳定排序和查询事实审计。

本模块只处理列表查询，不改动详情读取。所有 SQL 都把可信认证 tenant_id 放在首个
过滤条件，筛选值只通过参数绑定进入数据库；查询事件只保存字段名和不可逆指纹，
不得持久化 Prompt、Response、Key、Token、原始 filters 或 cursor。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction

from .models import RequestLogListResponse
from .service import _connect, _request_log_info

if TYPE_CHECKING:
    from metahub_control.machine_identity.service import MachineAuthContext

# 下列限额、字段白名单和阶段表达式共同冻结公开查询协议及数据库索引语义。
MAX_FILTER_WIRE_LENGTH = 4096
MAX_FILTER_JSON_BYTES = 3072
# QueryEvent 还可能为 legacy model 追加一个审计字段名，因此应用层与 0063 统一按
# “27 个 v1 白名单键 + 1 个兼容键”冻结上限；未知键仍由 FILTER_FIELDS 单独拒绝。
MAX_FILTER_FIELD_COUNT = 28
MAX_OFFSET = 10_000
MAX_LIMIT = 100
SORT_FIELDS = frozenset({"created_at", "latency_ms", "total_tokens", "estimated_cost_amount"})
SORT_DIRECTIONS = frozenset({"asc", "desc"})
TERMINAL_STAGES = frozenset(
    {
        "completed", "authentication", "rate_limit", "budget", "dlp", "policy",
        "routing", "keypool", "provider", "output_safety", "usage_and_finops",
        "logs_and_webhooks", "unknown",
    }
)
# PAGE-099 四个筛选直接映射 OutputSafetyScan 冻结枚举；独立常量避免前端标签或
# RequestLog status 被误用为数据库扫描状态。
OUTPUT_SAFETY_FILTER_VALUES = {
    "output_safety_status": frozenset(
        {"pending", "not_configured", "completed", "unavailable", "not_applicable"}
    ),
    "output_safety_mode": frozenset({"non_stream_full", "stream_window"}),
    "output_safety_exposure": frozenset({"none", "partial", "complete", "unknown"}),
    "output_safety_billing": frozenset({"not_required", "dispute_pending"}),
}
FILTER_FIELDS = frozenset(
    {
        "v", "request_id", "trace_id", "status", "request_model", "resolved_model",
        "provider_code", "virtual_key_id", "environment_id", "project_id", "application_id",
        "snapshot_version", "error_code", "created_from", "created_to", "latency_ms_min",
        "latency_ms_max", "total_tokens_min", "total_tokens_max", "cost_amount_min",
        "cost_amount_max", "currency", "terminal_stage", "output_safety_status",
        "output_safety_mode", "output_safety_exposure", "output_safety_billing",
    }
)
LEGACY_ALIASES = {
    "request_id": "request_id",
    "trace_id": "trace_id",
    "status": "status",
    "model": "model",
    "request_model": "request_model",
    "resolved_model": "resolved_model",
    "provider_code": "provider_code",
    "virtual_key_id": "virtual_key_id",
    "environment_id": "environment_id",
    "project_id": "project_id",
    "application_id": "application_id",
    "snapshot_version": "snapshot_version",
    "error_code": "error_code",
    "created_from": "created_from",
    "created_to": "created_to",
    "latency_ms_min": "latency_ms_min",
    "latency_ms_max": "latency_ms_max",
    "total_tokens_min": "total_tokens_min",
    "total_tokens_max": "total_tokens_max",
    "cost_amount_min": "cost_amount_min",
    "cost_amount_max": "cost_amount_max",
    "currency": "currency",
    "terminal_stage": "terminal_stage",
    "output_safety_status": "output_safety_status",
    "output_safety_mode": "output_safety_mode",
    "output_safety_exposure": "output_safety_exposure",
    "output_safety_billing": "output_safety_billing",
}
CONTROL_KEYS = frozenset(
    {"filters", "saved_filter_id", "sort_by", "sort_direction", "limit", "offset"}
)
ALLOWED_QUERY_KEYS = frozenset(LEGACY_ALIASES) | CONTROL_KEYS
SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/@-]{1,160}$")
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
TERMINAL_STAGE_SQL = """CASE
  WHEN oss.decision='blocked' OR oss.scan_status='unavailable' THEN 'output_safety'
  WHEN rl.error_code IN ('content_policy_violation','gateway.output_safety_unavailable')
    THEN 'output_safety'
  WHEN rl.status='success' THEN 'completed'
  WHEN rl.error_code IN ('gateway.auth_missing','gateway.auth_invalid') THEN 'authentication'
  WHEN rl.error_code IN (
    'gateway.rate_limit_exceeded','gateway.rate_limit_unavailable'
  ) THEN 'rate_limit'
  WHEN rl.error_code IN ('gateway.budget_exhausted','gateway.budget_unavailable') THEN 'budget'
  WHEN rl.error_code IN ('gateway.dlp_blocked','gateway.dlp_unavailable') THEN 'dlp'
  WHEN rl.error_code LIKE 'gateway.policy_%' THEN 'policy'
  WHEN rl.error_code IN ('gateway.cache_affinity_unavailable') THEN 'keypool'
  WHEN rl.error_code LIKE 'gateway.upstream_%'
    OR rl.error_code IN (
      'gateway.execution_interrupted','gateway.stream_not_supported',
      'gateway.stream_session_unavailable','gateway.client_disconnected',
      'gateway.stream_processing_unavailable','gateway.experiment_execution_timeout'
    ) THEN 'provider'
  WHEN rl.error_code IN (
    'gateway.model_missing','gateway.messages_missing','gateway.input_missing',
    'gateway.model_not_found','gateway.model_alias_disabled','gateway.model_disabled',
    'gateway.provider_not_found','gateway.provider_unhealthy','gateway.no_route_available',
    'gateway.routing_policy_invalid','gateway.routing_snapshot_unavailable',
    'gateway.routing_snapshot_invalid','gateway.routing_decision_unavailable',
    'gateway.routing_attempt_unavailable','gateway.fallback_unavailable',
    'gateway.experiment_unavailable','gateway.experiment_variant_unavailable'
  ) THEN 'routing'
  WHEN rl.error_code='gateway.request_log_unavailable' THEN 'logs_and_webhooks'
  ELSE 'unknown'
END"""


@dataclass(frozen=True, slots=True)
class AdvancedQuery:
    """规范化后的查询合同；canonical_query 仅用于指纹，不得写入运行日志。

    legacy_model 单独保存旧 model 的“请求模型或最终模型”语义。它不能通过两个
    新字段值相等来推断，否则 inline v1 同时精确指定相同模型时会被错误放宽成 OR。
    """

    filters: dict[str, Any]
    canonical_query: str
    fingerprint: str
    legacy_model: str | None
    saved_filter_id: str | None
    sort_by: str
    sort_direction: str
    limit: int
    offset: int


def list_request_logs_advanced(
    config: AppConfig,
    context: AuthContext,
    *,
    query_items: tuple[tuple[str, str], ...],
    request_id: str,
    trace_id: str,
) -> RequestLogListResponse:
    """在同一租户读快照中查询列表，随后以短事务写不可变查询事实。

    参数来自 FastAPI 已完成 RBAC 的路由，因此不可信 query 的解析发生在权限校验之后。
    返回值只含安全日志摘要；审计提交失败时关闭失败，禁止返回未经追踪的敏感列表。
    """

    started = perf_counter()
    connection = _connect(config)
    try:
        authentication = ResourceWriteTransaction(
            connection, config, context, "request_log.read", read_only=True,
        )
        authentication.begin()
        context = authentication.context
        query = _parse_query(connection, context, query_items, allow_saved_filters=True)
        rows, total = _read_page(connection, context.user.tenant_id, query)
        connection.rollback()
    # 已归一的业务错误保持原错误码，但仍需回滚并关闭本次数据库连接。
    except ApiError:
        _rollback(connection)
        connection.close()
        raise
    # SQLite 读取或快照失败不能泄露 SQL 文本，统一转换成可重试查询错误。
    except sqlite3.Error as exc:
        _rollback(connection)
        connection.close()
        # 底层异常正文可能包含文件路径或 SQL，只向客户端返回固定错误码。
        raise ApiError("request_log.query_unavailable") from exc

    page_rows = rows[: query.limit]
    # has_more 使用与数据库/Webhook 完全相同的冻结公式，避免 offset 超界时由探测行
    # 和 matched_count 得出互相矛盾的查询事实。
    has_more = query.offset + len(page_rows) < total
    try:
        # 先把数据库行完整映射为安全 DTO，再记录“成功查询”。若持久行损坏导致映射
        # 失败，本次请求必须关闭失败，不能留下实际未返回给调用方的成功审计事实。
        request_logs = [_request_log_info(row) for row in page_rows]
    except ApiError:
        # 输出安全 owner/状态污染使用专用 503，不能被通用查询错误覆盖。
        connection.close()
        raise
    except Exception as exc:
        connection.close()
        # DTO 映射失败表示持久事实不可安全公开，必须以查询不可用关闭失败。
        raise ApiError("request_log.query_unavailable") from exc
    duration_ms = max(0, math.ceil((perf_counter() - started) * 1000))
    try:
        query_event_id = _write_query_event(
            connection,
            context,
            query,
            config=config,
            total=total,
            returned_count=len(page_rows),
            has_more=has_more,
            duration_ms=duration_ms,
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        # 审计写失败和成功都释放数据库句柄，避免测试或长驻控制面累积文件锁。
        connection.close()
    return RequestLogListResponse(
        request_logs=request_logs,
        total=total,
        limit=query.limit,
        offset=query.offset,
        has_more=has_more,
        sort_by=query.sort_by,
        sort_direction=query.sort_direction,
        query_event_id=query_event_id,
        request_id=request_id,
        trace_id=trace_id,
    )


def list_request_logs_machine(
    config: AppConfig,
    context: MachineAuthContext,
    *,
    query_items: tuple[tuple[str, str], ...],
    request_id: str,
    trace_id: str,
) -> RequestLogListResponse:
    """Read the tenant list without personal SavedFilters or personal query-event FKs."""

    connection = _connect(config)
    try:
        query = _parse_query(
            connection,
            context,
            query_items,
            allow_saved_filters=False,
        )
        rows, total = _read_page(connection, context.tenant_id, query)
        connection.rollback()
        page_rows = rows[: query.limit]
        request_logs = [_request_log_info(row) for row in page_rows]
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("request_log.query_unavailable") from exc
    except Exception as exc:
        _rollback(connection)
        raise ApiError("request_log.query_unavailable") from exc
    finally:
        connection.close()
    return RequestLogListResponse(
        request_logs=request_logs,
        total=total,
        limit=query.limit,
        offset=query.offset,
        has_more=query.offset + len(page_rows) < total,
        sort_by=query.sort_by,
        sort_direction=query.sort_direction,
        query_event_id=None,
        request_id=request_id,
        trace_id=trace_id,
    )


def _read_page(
    connection: sqlite3.Connection,
    tenant_id: str,
    query: AdvancedQuery,
) -> tuple[list[sqlite3.Row], int]:
    where_sql, parameters = _build_where(
        tenant_id,
        query.filters,
        legacy_model=query.legacy_model,
    )
    order_sql = _build_order(query.sort_by, query.sort_direction)
    native = getattr(connection, "backend", "sqlite") == "postgresql"
    row_key = "request_id" if native else "rowid"
    index_hint = "" if native else _matched_index_hint(query.filters, query.sort_by)
    if not connection.in_transaction:
        connection.execute("BEGIN")
    rows = connection.execute(
        f"""
        WITH matched AS MATERIALIZED (
          SELECT rl.{row_key} AS matched_rowid, COUNT(*) OVER() AS _matched_total
          FROM request_logs rl {index_hint}
          {_OUTPUT_SAFETY_JOIN}
          {where_sql}
          {order_sql}
          LIMIT ? OFFSET ?
        )
        {_PAGE_SELECT_SQL.format(row_key=row_key)}
        {order_sql}
        """,
        [*parameters, query.limit + 1, query.offset],
    ).fetchall()
    if rows:
        return rows, int(rows[0]["_matched_total"])
    total_row = connection.execute(
        f"SELECT COUNT(*) AS total FROM request_logs rl {_OUTPUT_SAFETY_JOIN} {where_sql}",
        parameters,
    ).fetchone()
    return rows, int(total_row["total"] if total_row else 0)


def _parse_query(
    connection: sqlite3.Connection,
    context: AuthContext | MachineAuthContext,
    items: tuple[tuple[str, str], ...],
    *,
    allow_saved_filters: bool = True,
) -> AdvancedQuery:
    """严格解析三种互斥来源：inline filters、可信 SavedFilter 或 legacy 参数。"""

    values: dict[str, str] = {}
    for key, value in items:
        # 未知或重复 query 在代理间可能产生不同覆盖规则，因此统一拒绝整次查询。
        if key not in ALLOWED_QUERY_KEYS or key in values:
            raise ApiError("request_log.query_invalid")
        values[key] = value
    inline = "filters" in values
    saved = "saved_filter_id" in values
    if saved and not allow_saved_filters:
        raise ApiError("request_log.query_invalid")
    legacy_keys = set(values) & set(LEGACY_ALIASES)
    # 旧显式查询保留 PRD-016 的 query_invalid；版本化 filters 与可信 SavedFilter
    # 使用 PRD-055 新增的 pagination_invalid，避免破坏既有客户端错误分支。
    pagination_error = (
        "request_log.pagination_invalid"
        if inline or saved
        else "request_log.query_invalid"
    )
    limit = _page_integer(
        values.get("limit"),
        default=20,
        minimum=1,
        maximum=MAX_LIMIT,
        error_code=pagination_error,
    )
    offset = _page_integer(
        values.get("offset"),
        default=0,
        minimum=0,
        maximum=MAX_OFFSET,
        error_code=pagination_error,
    )
    # 三种筛选来源必须互斥，否则客户端可利用优先级差异改变实际查询边界。
    if sum((inline, saved, bool(legacy_keys))) > 1:
        raise ApiError("request_log.query_invalid")

    if saved:
        # SavedFilter 排序只能来自可信 owner 行，禁止客户端回传排序覆盖已保存语义。
        if "sort_by" in values or "sort_direction" in values:
            raise ApiError("request_log.query_invalid")
        saved_filter_id, filters, sort_by, direction = _load_saved_query(
            connection,
            context,
            values["saved_filter_id"],
        )
        legacy_model = None
    elif inline:
        filters = decode_filters(values["filters"])
        legacy_model = None
        saved_filter_id = None
        sort_by = _sort_field(values.get("sort_by"))
        direction = _sort_direction(values.get("sort_direction"))
    else:
        filters = {"v": 1}
        legacy_model = None
        for key in legacy_keys:
            target = LEGACY_ALIASES[key]
            if target == "model":
                # 旧 model 参数有独立 OR 语义，不能伪装成两个精确字段再由值相等推断。
                legacy_model = _safe_text(values[key])
            elif target == "status":
                status = values[key].strip().lower()
                if status not in {"success", "failed"}:
                    # 旧客户端依赖专用状态错误码；inline v1 仍统一归入 query_invalid。
                    raise ApiError("request_log.status_invalid")
                filters[target] = status
            elif target in {
                "snapshot_version", "latency_ms_min", "latency_ms_max",
                "total_tokens_min", "total_tokens_max",
            }:
                filters[target] = _legacy_filter_integer(
                    values[key],
                    minimum=1 if target == "snapshot_version" else 0,
                )
            else:
                filters[target] = values[key]
        filters = normalize_filters(filters, error_code="request_log.query_invalid")
        saved_filter_id = None
        sort_by = _sort_field(values.get("sort_by"))
        direction = _sort_direction(values.get("sort_direction"))

    # legacy model 参与指纹但不进入 v1 filters；这样相同模型值的 OR 兼容查询与
    # request_model/resolved_model 精确交集拥有不同且可复现的审计指纹。
    canonical = canonical_filters(
        filters if legacy_model is None else {**filters, "model": legacy_model}
    )
    return AdvancedQuery(
        filters=filters,
        canonical_query=canonical,
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        legacy_model=legacy_model,
        saved_filter_id=saved_filter_id,
        sort_by=sort_by,
        sort_direction=direction,
        limit=limit,
        offset=offset,
    )


def decode_filters(token: str) -> dict[str, Any]:
    """解码严格 Base64URL JSON，拒绝 padding、重复 JSON key 和非对象根节点。"""

    # wire token 必须有界且只含无填充 Base64URL 字符，避免多种等价编码绕过指纹。
    if not token or len(token) > MAX_FILTER_WIRE_LENGTH or not BASE64URL.fullmatch(token):
        raise ApiError("request_log.query_invalid")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
    # Base64 解码细节不得成为输入探针，统一折叠为高级查询非法。
    except (binascii.Error, ValueError) as exc:
        raise ApiError("request_log.query_invalid") from exc
    # 解码后再次执行字节上限，不能只依赖编码字符串长度限制内存。
    if len(raw) > MAX_FILTER_JSON_BYTES:
        raise ApiError("request_log.query_invalid")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    # UTF-8、JSON 或重复键失败都不得回显原始筛选正文和解析器位置。
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ApiError("request_log.query_invalid") from exc
    return normalize_filters(parsed, error_code="request_log.query_invalid")


def normalize_filters(value: object, *, error_code: str) -> dict[str, Any]:
    """把结构化筛选规范化为版本 1 白名单对象，供查询与 SavedFilter 共用。"""

    # 根节点、字段数量和白名单同时受限，避免嵌套正文或身份字段进入查询构造器。
    if (
        not isinstance(value, dict)
        or len(value) > MAX_FILTER_FIELD_COUNT
        or set(value) - FILTER_FIELDS
    ):
        # 根对象超出完整白名单时立即拒绝，禁止未知筛选被静默忽略后扩大查询结果。
        raise ApiError(error_code)
    # 版本必须是原生整数一，拒绝 bool 和未知版本被旧服务错误解释。
    if type(value.get("v")) is not int or value.get("v") != 1:
        raise ApiError(error_code)
    result: dict[str, Any] = {"v": 1}
    string_fields = FILTER_FIELDS - {
        "v", "snapshot_version", "latency_ms_min", "latency_ms_max",
        "total_tokens_min", "total_tokens_max", "cost_amount_min", "cost_amount_max",
    }
    for key in string_fields:
        if key not in value:
            continue
        raw = value[key]
        # 字符串字段不接受数字、数组或对象的隐式转换，避免规范化结果漂移。
        if not isinstance(raw, str):
            raise ApiError(error_code)
        # request/trace 使用更窄标识字符集，防止控制字符进入审计链路。
        if key in {"request_id", "trace_id"}:
            result[key] = _request_identifier(raw, error_code)
        elif key == "status":
            normalized = raw.strip().lower()
            # 状态只允许数据库已冻结的两个终态，运行中状态不能伪造为历史事实。
            if normalized not in {"success", "failed"}:
                raise ApiError(error_code)
            result[key] = normalized
        elif key == "terminal_stage":
            normalized = raw.strip().lower()
            # 阶段必须来自 terminal_stage_v1 白名单，未知标签不能进入表达式索引。
            if normalized not in TERMINAL_STAGES:
                raise ApiError(error_code)
            result[key] = normalized
        elif key in OUTPUT_SAFETY_FILTER_VALUES:
            normalized = raw.strip().lower()
            # 四个 PAGE-099 枚举分别绑定 0063 字段；空值和未知标签不能进入 SQL。
            if normalized not in OUTPUT_SAFETY_FILTER_VALUES[key]:
                raise ApiError(error_code)
            result[key] = normalized
        elif key in {"created_from", "created_to"}:
            result[key] = _utc_time(raw, error_code)
        # 币种影响金额比较语义，只允许系统成本账本明确支持的 CNY/USD。
        elif key == "currency":
            normalized = raw.strip().upper()
            if normalized not in {"CNY", "USD"}:
                # 未知币种会破坏成本区间比较和汇总口径，因此在进入查询表达式前立即拒绝。
                raise ApiError(error_code)
            result[key] = normalized
        else:
            result[key] = _safe_text(raw, error_code)
    integer_fields = {
        "snapshot_version",
        "latency_ms_min",
        "latency_ms_max",
        "total_tokens_min",
        "total_tokens_max",
    }
    for key in integer_fields:
        if key in value:
            result[key] = _filter_integer(
                value[key],
                error_code,
                minimum=1 if key == "snapshot_version" else 0,
            )
    for key in {"cost_amount_min", "cost_amount_max"}:
        if key in value:
            result[key] = _decimal_text(value[key], error_code)
    _validate_ranges(result, error_code)
    return result


def canonical_filters(filters: dict[str, Any]) -> str:
    """生成稳定 UTF-8 canonical JSON；该正文只允许存 SavedFilter，不写 QueryEvent 或日志。"""

    return json.dumps(filters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object_pairs_hook：重复 key 一律拒绝，避免前后端采用不同优先级。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        # 重复键会导致不同 JSON 实现选择不同值，必须在规范化前立即拒绝。
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result

def _load_saved_query(
    connection: sqlite3.Connection,
    context: AuthContext,
    raw_id: str,
) -> tuple[str, dict[str, Any], str, str]:
    """按 tenant+owner+active 加载筛选；不存在与越权始终返回同形 404。"""

    saved_filter_id = _saved_filter_identifier(raw_id)
    row = connection.execute(
        """
        SELECT filter_json, sort_by, sort_direction
        FROM saved_filters
        WHERE tenant_id=? AND owner_user_id=? AND saved_filter_id=? AND status='active'
        """,
        (context.user.tenant_id, context.user.user_id, saved_filter_id),
    ).fetchone()
    # 不存在、跨租户、非 owner 和已删除记录保持同形，阻止枚举个人筛选。
    if row is None:
        raise ApiError("saved_filter.not_found")
    try:
        parsed = json.loads(str(row["filter_json"]), object_pairs_hook=_unique_object)
        filters = normalize_filters(parsed, error_code="saved_filter.unavailable")
        sort_by = _sort_field(str(row["sort_by"]))
        direction = _sort_direction(str(row["sort_direction"]))
    # 可信 owner 行一旦损坏应返回存储不可用，不能误报成调用方输入错误。
    except (ApiError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        # 这里的数据已经来自可信 owner 行；格式错误是持久层损坏而非用户输入错误。
        raise ApiError("saved_filter.unavailable") from exc
    return (
        saved_filter_id,
        filters,
        sort_by,
        direction,
    )


def _build_where(
    tenant_id: str,
    filters: dict[str, Any],
    *,
    legacy_model: str | None = None,
) -> tuple[str, list[object]]:
    """构造 tenant-first 参数化 WHERE，并显式保留 legacy model 的 OR 语义。"""

    clauses = ["rl.tenant_id=?"]
    parameters: list[object] = [tenant_id]
    equality = {
        "request_id": "rl.request_id",
        "trace_id": "rl.trace_id",
        "status": "rl.status",
        "provider_code": "rl.provider_code",
        "virtual_key_id": "rl.virtual_key_id",
        "environment_id": "rl.environment_id",
        "project_id": "rl.project_id",
        "application_id": "rl.application_id",
        "snapshot_version": "rl.snapshot_version",
        "error_code": "rl.error_code",
        "currency": "rl.currency",
        "output_safety_status": "oss.scan_status",
        "output_safety_mode": "oss.scan_mode",
        "output_safety_exposure": "oss.exposure_state",
        "output_safety_billing": "oss.billing_disposition",
    }
    for key, column in equality.items():
        if key in filters:
            clauses.append(f"{column}=?")
            parameters.append(filters[key])
    request_model = filters.get("request_model")
    resolved_model = filters.get("resolved_model")
    if legacy_model is not None:
        # 兼容模式只由解析器的独立标记进入；inline 相等双字段仍是精确 AND。
        clauses.append("(rl.request_model=? OR rl.resolved_model=?)")
        parameters.extend([legacy_model, legacy_model])
    if request_model is not None:
        clauses.append("rl.request_model=?")
        parameters.append(request_model)
    # resolved_model 是新合同的精确字段，不能因数值与 request_model 相等而放宽。
    if resolved_model is not None:
        clauses.append("rl.resolved_model=?")
        parameters.append(resolved_model)
    comparisons = {
        "created_from": ("rl.created_at", ">="),
        "created_to": ("rl.created_at", "<"),
        "latency_ms_min": ("rl.latency_ms", ">="),
        "latency_ms_max": ("rl.latency_ms", "<="),
        "total_tokens_min": ("rl.total_tokens", ">="),
        "total_tokens_max": ("rl.total_tokens", "<="),
        "cost_amount_min": ("rl.estimated_cost_amount", ">="),
        "cost_amount_max": ("rl.estimated_cost_amount", "<="),
    }
    for key, (column, operator) in comparisons.items():
        if key in filters:
            clauses.append(f"{column}{operator}?")
            parameters.append(filters[key])
    if "terminal_stage" in filters:
        clauses.append(f"({TERMINAL_STAGE_SQL})=?")
        parameters.append(filters["terminal_stage"])
    return "WHERE " + " AND ".join(clauses), parameters


def _build_order(sort_by: str, direction: str) -> str:
    """生成四种冻结排序；可空主列始终放在非空值之后，尾键保证结果稳定。"""

    keyword = "ASC" if direction == "asc" else "DESC"
    if sort_by == "created_at":
        return f"ORDER BY rl.created_at {keyword}, rl.request_id {keyword}"
    return (
        f"ORDER BY (rl.{sort_by} IS NULL) ASC, rl.{sort_by} {keyword}, "
        f"rl.created_at {keyword}, rl.request_id {keyword}"
    )


def _matched_index_hint(filters: dict[str, Any], sort_by: str) -> str:
    """为跨列排序的 status+provider 组合选择固定 tenant-first 候选集索引。

    该提示只来自后端白名单字段，不拼接客户端文本。created_at 排序继续交给 SQLite
    选择能同时满足过滤和顺序的索引；其他排序先把候选集缩到 tenant+status，再对
    已同时匹配 provider 的较小集合排序，避免为保持排序而扫描租户全部热日志。
    """

    if (
        sort_by != "created_at"
        and "status" in filters
        and "provider_code" in filters
    ):
        # 先缩小 tenant+status 候选集再做其他列排序，避免 SQLite 为保序扫描租户全部日志。
        return "INDEXED BY idx_request_logs_status_time_prd055"
    return ""


def _write_query_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    query: AdvancedQuery,
    *,
    config: AppConfig,
    total: int,
    returned_count: int,
    has_more: bool,
    duration_ms: int,
    request_id: str,
    trace_id: str,
) -> str:
    """以短写事务保存最小查询事实；不保存筛选值或原始 wire token。"""

    query_event_id = "lqe_" + uuid4().hex
    event_id = "lqevt_" + uuid4().hex
    timestamp = _db_time(datetime.now(UTC))
    filter_keys = json.dumps(
        sorted(
            [key for key in query.filters if key != "v"]
            + (["model"] if query.legacy_model is not None else [])
        ),
        separators=(",", ":"),
    )
    try:
        transaction = ResourceWriteTransaction(connection, config, context, "request_log.read")
        transaction.begin()
        context = transaction.context
        workspace = context.session.session_id.startswith("wss_")
        connection.execute(
            """
            INSERT INTO request_log_query_events(
              query_event_id, event_id, tenant_id, actor_user_id, actor_session_id,
              filter_version, filter_fingerprint, filter_keys_json, saved_filter_id,
              sort_by, sort_direction, query_limit, query_offset, matched_count,
              returned_count, has_more, query_duration_ms, operation_request_id,
              operation_trace_id, event_dispatch_status, event_dispatch_attempts,
              event_next_attempt_at, executed_at, created_at, updated_at, workspace_session_id
            )
            VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   'pending', 0, ?, ?, ?, ?, ?)
            """,
            (
                query_event_id,
                event_id,
                context.user.tenant_id,
                context.user.user_id,
                None if workspace else context.session.session_id,
                query.fingerprint,
                filter_keys,
                query.saved_filter_id,
                query.sort_by,
                query.sort_direction,
                query.limit,
                query.offset,
                total,
                returned_count,
                int(has_more),
                duration_ms,
                _request_identifier(request_id, "request_log.query_audit_unavailable"),
                _request_identifier(trace_id, "request_log.query_audit_unavailable"),
                timestamp,
                timestamp,
                timestamp,
                timestamp,
                context.session.session_id if workspace else None,
            ),
        )
        transaction.commit()
        return query_event_id
    except ApiError:
        _rollback(connection)
        raise
    # 审计事实写入失败时回滚整个短事务，列表结果不得在无追踪状态下返回。
    except sqlite3.Error as exc:
        _rollback(connection)
        # 触发器或锁异常只转换为固定审计错误，禁止泄露数据库内部信息。
        raise ApiError("request_log.query_audit_unavailable") from exc


def _page_integer(
    raw: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    error_code: str,
) -> int:
    """严格解析分页值，并由已识别的 legacy/PRD-055 模式选择稳定错误码。"""

    if raw is None:
        return default
    # 分页只接受规范 ASCII 十进制，拒绝符号、空白和 Unicode 数字替代。
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise ApiError(error_code)
    value = int(raw)
    # 深分页和超大页面会拖垮控制面，因此在进入 SQL 前执行硬上限。
    if value < minimum or value > maximum:
        raise ApiError(error_code)
    return value


def _legacy_filter_integer(raw: str, *, minimum: int = 0) -> int:
    """把 legacy query 数字转换为 v1 原生整数，保持旧参数同时拒绝符号和 Unicode 数字。"""

    # legacy 数字同样拒绝符号和 Unicode 数字，避免兼容入口成为宽松旁路。
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise ApiError("request_log.query_invalid")
    value = int(raw)
    # 整数必须满足字段下界且可由 SQLite 精确保存，禁止溢出后比较漂移。
    if value < minimum or value > 9_223_372_036_854_775_807:
        raise ApiError("request_log.query_invalid")
    return value


def _filter_integer(raw: object, error_code: str, *, minimum: int = 0) -> int:
    """筛选数值只接受原生非负整数，避免 JSON bool 或浮点截断。"""

    # JSON 整数拒绝 bool、负值和 SQLite 上限外数值，避免隐式类型转换。
    if type(raw) is not int or raw < minimum or raw > 9_223_372_036_854_775_807:
        raise ApiError(error_code)
    return raw


def _decimal_text(raw: object, error_code: str) -> str:
    """规范化非负十进制金额字符串，最多六位小数且不经二进制浮点。"""

    # 金额只接受非负十进制字符串和六位小数，禁止二进制浮点进入成本比较。
    if not isinstance(raw, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,6})?",
        raw,
    ):
        raise ApiError(error_code)
    try:
        value = Decimal(raw)
    # Decimal 解析失败统一映射业务错误，不回显原始金额字符串。
    except InvalidOperation as exc:
        raise ApiError(error_code) from exc
    # 即使正则已限制符号，仍保留领域下界校验以防未来格式规则调整。
    if value < 0:
        raise ApiError(error_code)
    return format(value, "f")


def _validate_ranges(filters: dict[str, Any], error_code: str) -> None:
    """验证时间、整数和金额区间，时间窗口最多三十天。"""

    has_created_from = "created_from" in filters
    has_created_to = "created_to" in filters
    # 单边时间条件仍会形成无界扫描；只有两端都缺省时才允许兼容历史数据。
    if has_created_from != has_created_to:
        raise ApiError(error_code)
    # 成对时间边界必须保持正向且最多三十天，以限制热库扫描范围。
    if has_created_from and has_created_to:
        start = datetime.fromisoformat(filters["created_from"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(filters["created_to"].replace("Z", "+00:00"))
        if start >= end or end - start > timedelta(days=30):
            # 反向或过宽时间窗既无稳定业务语义，也可能放大热库扫描，必须统一关闭。
            raise ApiError(error_code)
    for lower, upper in (
        ("latency_ms_min", "latency_ms_max"),
        ("total_tokens_min", "total_tokens_max"),
    ):
        # 数值下界不得超过上界，否则查询语义为空且容易掩盖客户端字段错配。
        if lower in filters and upper in filters and filters[lower] > filters[upper]:
            raise ApiError(error_code)
    # 成本范围使用 Decimal 比较，不能转换成可能失真的二进制浮点数。
    if "cost_amount_min" in filters and "cost_amount_max" in filters:
        # 金额下界超过上界时直接拒绝，不能静默返回空结果伪装成功。
        if Decimal(filters["cost_amount_min"]) > Decimal(filters["cost_amount_max"]):
            raise ApiError(error_code)


def _utc_time(raw: str, error_code: str) -> str:
    """只接受带时区 ISO-8601，并统一为秒级 UTC Z 文本。"""

    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    # 时间解析异常不得回显原始筛选值，统一返回调用方可诊断的固定错误。
    except (ValueError, TypeError) as exc:
        raise ApiError(error_code) from exc
    # 无时区时间在多地域部署下含义不确定，必须要求调用方明确时区。
    if parsed.tzinfo is None:
        raise ApiError(error_code)
    return _db_time(parsed.astimezone(UTC))


def _safe_text(raw: str, error_code: str = "request_log.query_invalid") -> str:
    """校验业务 ID/枚举的安全字符集，避免控制字符和任意正文进入查询合同。"""

    normalized = raw.strip()
    # 业务标识只允许受限 ASCII，控制字符和自由正文不得进入 SQL 或审计指纹。
    if not SAFE_TEXT.fullmatch(normalized):
        raise ApiError(error_code)
    return normalized


def _request_identifier(raw: str, error_code: str) -> str:
    """校验 request/trace 标识，保证数据库审计约束与 API 合同一致。"""

    normalized = raw.strip()
    # 链路标识采用更窄白名单，保证响应头、数据库和事件合同使用同一字符集。
    if not SAFE_REQUEST_ID.fullmatch(normalized):
        raise ApiError(error_code)
    return normalized


def _saved_filter_identifier(raw: str) -> str:
    """校验 SavedFilter 稳定 ID；非法 ID 与不存在记录保持同形 404。"""

    normalized = raw.strip()
    # 非法 ID 与不存在记录保持同形 404，防止根据格式差异枚举个人筛选。
    if not re.fullmatch(r"sf_[0-9a-f]{32}", normalized):
        raise ApiError("saved_filter.not_found")
    return normalized


def _sort_field(raw: str | None) -> str:
    """把缺省排序冻结为 created_at，非白名单字段统一拒绝。"""

    value = "created_at" if raw is None else raw.strip()
    # 排序列只能来自冻结白名单，绝不允许客户端字段名拼接进入 SQL。
    if value not in SORT_FIELDS:
        raise ApiError("request_log.sort_invalid")
    return value


def _sort_direction(raw: str | None) -> str:
    """把缺省方向冻结为 desc，只允许 asc/desc 小写合同。"""

    value = "desc" if raw is None else raw.strip()
    # 排序方向只允许两个固定关键字，避免自由 SQL 片段进入 ORDER BY。
    if value not in SORT_DIRECTIONS:
        raise ApiError("request_log.sort_invalid")
    return value


def _db_time(value: datetime) -> str:
    """把带时区时间规范为 SQLite 可比较的 UTC 秒级文本。"""

    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚失败事务；原始 SQLite 文本不得越过统一错误边界。"""

    try:
        connection.rollback()
    # 回滚失败不能覆盖原始统一错误；连接随后关闭以释放可能残留的锁。
    except sqlite3.Error:
        pass


# 候选集、COUNT 和展示投影必须复用相同的 tenant-qualified 唯一扫描关联；否则
# FinOps 503 覆盖公开错误码时，阶段筛选会漏掉已提交的 blocked/unavailable 事实。
_OUTPUT_SAFETY_JOIN = """
LEFT JOIN output_safety_scans oss
  ON oss.tenant_id=rl.tenant_id AND oss.request_id=rl.request_id
"""

# 列表 SQL 仅投影安全摘要，并在每个展示关联上重复 tenant 条件防止横向串租户。
_SELECT_COLUMNS = f"""
  rl.request_id, rl.tenant_id, rl.trace_id, rl.virtual_key_id, vk.key_prefix AS virtual_key_prefix,
  rl.environment_id, rl.environment_code AS environment, e.name AS environment_name,
  rl.project_id, p.name AS project_name, rl.application_id, a.name AS application_name,
  rl.provider_id, rl.model_id, rl.endpoint, rl.request_model, rl.resolved_model,
  rl.provider_code, rl.status, rl.error_code, ({TERMINAL_STAGE_SQL}) AS terminal_stage,
  rl.stream_session_id, rl.stream_status,
  rl.snapshot_version, rl.policy_snapshot_status, rl.policy_decision, rl.policy_reason_code,
  rl.policy_id, rl.policy_action_type, rl.policy_target_model_id,
  rl.policy_mask_applied, rl.policy_mask_policy_id, rl.latency_ms,
  rl.input_tokens, rl.output_tokens, rl.total_tokens,
  rl.estimated_cost_amount, rl.currency, rl.price_schema_version,
  rl.budget_status, rl.budget_rule_id, rl.budget_scope_type, rl.budget_scope_id,
  rl.budget_action, rl.budget_reason_code, rl.budget_current_cost_cny, rl.budget_limit_cny,
  rl.dlp_status, rl.dlp_rule_id, rl.dlp_action, rl.dlp_match_count, rl.dlp_reason_code,
  rl.fallback_triggered, rl.routing_attempt_count, rl.final_routing_attempt_id,
  oss.output_safety_scan_id AS output_safety_scan_id,
  oss.tenant_id AS output_safety_tenant_id,
  oss.request_id AS output_safety_request_id,
  oss.trace_id AS output_safety_trace_id,
  oss.scan_mode AS output_safety_scan_mode,
  oss.scan_status AS output_safety_scan_status,
  oss.decision AS output_safety_decision,
  oss.exposure_state AS output_safety_exposure_state,
  oss.emitted_token_count AS output_safety_emitted_token_count,
  oss.emitted_sse_event_count AS output_safety_emitted_sse_event_count,
  oss.emitted_bytes AS output_safety_emitted_bytes,
  oss.billing_disposition AS output_safety_billing_disposition,
  CASE WHEN EXISTS(
    SELECT 1 FROM request_feedbacks rf
    WHERE rf.tenant_id=rl.tenant_id AND rf.source_request_id=rl.request_id
  ) THEN 1 ELSE 0 END AS has_feedback,
  rl.created_at, rl.updated_at
"""

_SELECT_JOINS = """
LEFT JOIN output_safety_scans oss
  ON oss.tenant_id=rl.tenant_id AND oss.request_id=rl.request_id
LEFT JOIN virtual_keys vk ON vk.virtual_key_id=rl.virtual_key_id AND vk.tenant_id=rl.tenant_id
LEFT JOIN environments e ON e.environment_id=rl.environment_id AND e.tenant_id=rl.tenant_id
LEFT JOIN projects p ON p.project_id=rl.project_id AND p.tenant_id=rl.tenant_id
LEFT JOIN applications a ON a.application_id=rl.application_id AND a.tenant_id=rl.tenant_id
"""

_SELECT_SQL = f"""
SELECT
{_SELECT_COLUMNS}
FROM request_logs rl
{_SELECT_JOINS}
"""

# 先由 matched CTE 只保留当前页 rowid，再执行展示关联；这样精确 total 与页面结果共享
# 一次租户扫描，且不会把环境、项目、应用或反馈关联扩散到整批匹配行。
_PAGE_SELECT_SQL = f"""
SELECT
  matched._matched_total AS _matched_total,
{_SELECT_COLUMNS}
FROM matched
JOIN request_logs rl ON rl.{{row_key}}=matched.matched_rowid
{_SELECT_JOINS}
"""
