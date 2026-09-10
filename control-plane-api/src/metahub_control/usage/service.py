"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/usage/service.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.usage import sanitize_provider_usage

from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.auth.service import require_permission
from metahub_control.consumers.service import require_consumer_admin

from .models import (
    UsageApplicationSummary,
    UsageConsumerSummary,
    UsageDaySummary,
    UsageEnvironmentSummary,
    UsageModelSummary,
    UsageProjectSummary,
    UsageRecordInfo,
    UsageRecordListResponse,
    UsageSummaryResponse,
    UsageTotals,
    UsageVirtualKeySummary,
)

# 用量查询分页和过滤上限集中定义，保护账本聚合接口免受大范围扫描。
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
MAX_FILTER_LENGTH = 160


def get_usage_summary(
    config: AppConfig,
    context: AuthContext,
    *,
    start_date: str | None,
    end_date: str | None,
    model_id: str | None,
    virtual_key_id: str | None,
    consumer_id: str | None = None,
    environment_id: str | None,
    provider_id: str | None,
    project_id: str | None,
    application_id: str | None,
    estimated: str | None,
    request_id_filter: str | None,
    trace_id_filter: str | None,
    request_id: str,
    trace_id: str,
) -> UsageSummaryResponse:
    """按当前租户聚合 Token 用量和成本。

    查询入口始终从认证上下文取得 tenant_id，所有筛选条件只在该租户内生效，避免管理员或开发者通过猜测 ID 跨租户读取账本。
    """

    filters = normalize_usage_filters(
        start_date=start_date,
        end_date=end_date,
        model_id=model_id,
        virtual_key_id=virtual_key_id,
        consumer_id=consumer_id,
        environment_id=environment_id,
        provider_id=provider_id,
        project_id=project_id,
        application_id=application_id,
        estimated=estimated,
        request_id_filter=request_id_filter,
        trace_id_filter=trace_id_filter,
    )
    where_sql, params = build_usage_filters(context.user.tenant_id, filters)
    connection = _connect(config)
    try:
        transaction = ResourceWriteTransaction(
            connection, config, context, "billing.usage.read", read_only=True
        )
        transaction.begin()
        response = UsageSummaryResponse(
            totals=_load_totals(connection, where_sql, params),
            by_day=_load_by_day(connection, where_sql, params),
            by_model=_load_by_model(connection, where_sql, params),
            by_virtual_key=_load_by_virtual_key(connection, where_sql, params),
            by_consumer=_load_by_consumer(connection, where_sql, params, transaction.context),
            by_environment=_load_by_environment(connection, where_sql, params),
            by_project=_load_by_project(connection, where_sql, params),
            by_application=_load_by_application(connection, where_sql, params),
            request_id=request_id,
            trace_id=trace_id,
        )
        # A new snapshot observes revocation committed while the report was read.
        connection.rollback()
        transaction.begin()
        if any(item.consumer_name is not None for item in response.by_consumer):
            require_consumer_admin(transaction.context)
            require_permission(transaction.context, "virtual_key.read")
        return response
    finally:
        connection.close()


def list_usage_records(
    config: AppConfig,
    context: AuthContext,
    *,
    start_date: str | None,
    end_date: str | None,
    model_id: str | None,
    virtual_key_id: str | None,
    consumer_id: str | None = None,
    environment_id: str | None,
    provider_id: str | None,
    project_id: str | None,
    application_id: str | None,
    estimated: str | None,
    request_id_filter: str | None,
    trace_id_filter: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> UsageRecordListResponse:
    """分页读取当前租户的 UsageRecord。

    列表返回规范化用量、成本和双重脱敏后的 Provider raw usage，绝不返回完整响应或认证字段。
    """

    filters = normalize_usage_filters(
        start_date=start_date,
        end_date=end_date,
        model_id=model_id,
        virtual_key_id=virtual_key_id,
        consumer_id=consumer_id,
        environment_id=environment_id,
        provider_id=provider_id,
        project_id=project_id,
        application_id=application_id,
        estimated=estimated,
        request_id_filter=request_id_filter,
        trace_id_filter=trace_id_filter,
    )
    normalized_limit = _normalize_limit(limit)
    normalized_offset = _normalize_offset(offset)
    where_sql, params = build_usage_filters(context.user.tenant_id, filters)
    connection = _connect(config)
    try:
        transaction = ResourceWriteTransaction(
            connection, config, context, "billing.usage.read", read_only=True
        )
        transaction.begin()
        total_row = connection.execute(
            f"SELECT COUNT(*) AS total FROM usage_ledger ul LEFT JOIN request_logs rl ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id {where_sql}",
            params,
        ).fetchone()
        rows = connection.execute(
            f"""
        SELECT
          ul.usage_ledger_id, ul.tenant_id, ul.request_id, rl.trace_id,
          (SELECT kc.consumer_id FROM virtual_key_consumers kc
            WHERE kc.tenant_id=ul.tenant_id
              AND kc.virtual_key_id=COALESCE(ul.virtual_key_id, rl.virtual_key_id)) AS consumer_id,
          COALESCE(ul.virtual_key_id, rl.virtual_key_id) AS virtual_key_id,
          vk.key_prefix AS virtual_key_prefix,
          COALESCE(ul.environment_id, rl.environment_id) AS environment_id,
          COALESCE(ul.environment_code, rl.environment_code) AS environment,
          e.name AS environment_name,
          COALESCE(ul.project_id, rl.project_id) AS project_id,
          p.name AS project_name,
          COALESCE(ul.application_id, rl.application_id) AS application_id,
          a.name AS application_name,
          COALESCE(ul.provider_id, rl.provider_id) AS provider_id,
          COALESCE(ul.provider_code, rl.provider_code) AS provider_code,
          COALESCE(ul.model_id, rl.model_id) AS model_id,
          COALESCE(ul.request_model, rl.request_model) AS request_model,
          COALESCE(ul.resolved_model, rl.resolved_model) AS resolved_model,
          COALESCE(ul.endpoint, rl.endpoint) AS endpoint,
          ul.input_tokens, ul.output_tokens, ul.total_tokens, ul.cache_tokens,
          ul.cost_amount, ul.cost_cny, ul.cost_usd, ul.currency, ul.price_schema_version,
          ul.price_snapshot_id, ul.estimated, ul.usage_source, ul.normalized_usage, ul.raw_usage,
          ul.created_at, ul.updated_at
        FROM usage_ledger ul
        LEFT JOIN request_logs rl ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
        LEFT JOIN virtual_keys vk ON vk.tenant_id=ul.tenant_id AND vk.virtual_key_id=COALESCE(ul.virtual_key_id, rl.virtual_key_id)
        LEFT JOIN environments e ON e.tenant_id=ul.tenant_id AND e.environment_id=COALESCE(ul.environment_id, rl.environment_id)
        LEFT JOIN projects p ON p.tenant_id=ul.tenant_id AND p.project_id=COALESCE(ul.project_id, rl.project_id)
        LEFT JOIN applications a ON a.tenant_id=ul.tenant_id AND a.application_id=COALESCE(ul.application_id, rl.application_id)
        {where_sql}
        ORDER BY ul.created_at DESC, ul.usage_ledger_id DESC
        LIMIT ? OFFSET ?
        """,
            [*params, normalized_limit, normalized_offset],
        ).fetchall()
        response = UsageRecordListResponse(
            usage_records=[_usage_record_info(row) for row in rows],
            total=int(total_row["total"] if total_row else 0),
            limit=normalized_limit,
            offset=normalized_offset,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.rollback()
        transaction.begin()
        return response
    finally:
        connection.close()


def normalize_usage_filters(
    *,
    start_date: str | None,
    end_date: str | None,
    model_id: str | None,
    virtual_key_id: str | None,
    consumer_id: str | None = None,
    environment_id: str | None,
    provider_id: str | None,
    project_id: str | None,
    application_id: str | None,
    estimated: str | None,
    request_id_filter: str | None,
    trace_id_filter: str | None,
) -> dict[str, object | None]:
    """归一化用量查询筛选条件，集中处理日期、布尔值和字符串长度边界。"""

    start_bound, end_bound = _normalize_date_range(start_date, end_date)
    filters = {
        "start_bound": start_bound,
        "end_bound": end_bound,
        "model_id": _normalize_optional_filter(model_id),
        "virtual_key_id": _normalize_optional_filter(virtual_key_id),
        "environment_id": _normalize_optional_filter(environment_id),
        "provider_id": _normalize_optional_filter(provider_id),
        "project_id": _normalize_optional_filter(project_id),
        "application_id": _normalize_optional_filter(application_id),
        "estimated": _normalize_optional_bool(estimated),
        "request_id_filter": _normalize_optional_filter(request_id_filter),
        "trace_id_filter": _normalize_optional_filter(trace_id_filter),
    }
    normalized_consumer = _normalize_optional_filter(consumer_id)
    # Keep existing export fingerprints unchanged when the new filter is absent.
    if normalized_consumer is not None:
        filters["consumer_id"] = normalized_consumer
    return filters


def build_usage_filters(
    tenant_id: str,
    filters: dict[str, object | None],
    *,
    snapshot_at: str | None = None,
    snapshot_ledger_sequence: int | None = None,
) -> tuple[str, list[object]]:
    """构建 Usage 查询条件，并确保 tenant_id 与可选导出快照始终进入 SQL。

    普通概览/明细不传快照参数；CSV 导出在计数与生成时同时传 UTC cutoff 和
    只增账本序号水位。时间条件保持日期语义兼容，序号水位负责排除同秒晚到记录。
    """

    clauses = ["ul.tenant_id=?"]
    params: list[object] = [tenant_id]
    if snapshot_at:
        # 存量时间既有秒级也可能含小数秒；julianday 避免纯文本比较把同秒 `...Z` 排到 `.fffZ` 之后。
        clauses.append("julianday(ul.created_at)<=julianday(?)")
        params.append(snapshot_at)
    if snapshot_ledger_sequence is not None:
        # Gateway 当前可写秒级时间戳；AUTOINCREMENT 序号不会因删除复用，可稳定区分计数前后的同秒写入。
        clauses.append("uls.usage_export_sequence<=?")
        params.append(snapshot_ledger_sequence)
    if filters["start_bound"]:
        clauses.append("ul.created_at>=?")
        params.append(filters["start_bound"])
    if filters["end_bound"]:
        clauses.append("ul.created_at<=?")
        params.append(filters["end_bound"])
    # 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
    if filters["model_id"]:
        clauses.append("(ul.model_id=? OR ul.resolved_model=? OR rl.model_id=? OR rl.resolved_model=?)")
        params.extend([filters["model_id"], filters["model_id"], filters["model_id"], filters["model_id"]])
    # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
    if filters["virtual_key_id"]:
        clauses.append("COALESCE(ul.virtual_key_id, rl.virtual_key_id)=?")
        params.append(filters["virtual_key_id"])
    if filters.get("consumer_id"):
        clauses.append(
            "EXISTS (SELECT 1 FROM virtual_key_consumers kc "
            "WHERE kc.tenant_id=ul.tenant_id "
            "AND kc.virtual_key_id=COALESCE(ul.virtual_key_id, rl.virtual_key_id) "
            "AND kc.consumer_id=?)"
        )
        params.append(filters["consumer_id"])
    if filters["environment_id"]:
        clauses.append("COALESCE(ul.environment_id, rl.environment_id)=?")
        params.append(filters["environment_id"])
    # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
    # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
    if filters["provider_id"]:
        clauses.append("COALESCE(ul.provider_id, rl.provider_id)=?")
        params.append(filters["provider_id"])
    if filters["project_id"]:
        clauses.append("COALESCE(ul.project_id, rl.project_id)=?")
        params.append(filters["project_id"])
    if filters["application_id"]:
        clauses.append("COALESCE(ul.application_id, rl.application_id)=?")
        params.append(filters["application_id"])
    if filters["estimated"] is not None:
        clauses.append("ul.estimated=?")
        params.append(1 if filters["estimated"] is True else 0)
    # 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
    if filters["request_id_filter"]:
        clauses.append("ul.request_id=?")
        params.append(filters["request_id_filter"])
    # 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
    if filters["trace_id_filter"]:
        clauses.append("rl.trace_id=?")
        params.append(filters["trace_id_filter"])
    return f"WHERE {' AND '.join(clauses)}", params


def _load_totals(connection: sqlite3.Connection, where_sql: str, params: list[object]) -> UsageTotals:
    """读取当前筛选条件下的总用量，空结果返回全 0 统计。"""

    row = connection.execute(
        f"""
        SELECT
          COUNT(*) AS request_count,
          COALESCE(SUM(ul.input_tokens), 0) AS input_tokens,
          COALESCE(SUM(ul.output_tokens), 0) AS output_tokens,
          COALESCE(SUM(ul.total_tokens), 0) AS total_tokens,
          COALESCE(SUM(ul.cache_tokens), 0) AS cache_tokens,
          COALESCE(SUM(ul.cost_cny), 0) AS cost_cny,
          COALESCE(SUM(ul.cost_usd), 0) AS cost_usd,
          COALESCE(SUM(CASE WHEN ul.estimated=1 THEN 1 ELSE 0 END), 0) AS estimated_records
        FROM usage_ledger ul
        LEFT JOIN request_logs rl ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
        {where_sql}
        """,
        params,
    ).fetchone()
    estimated_records = _int_value(row["estimated_records"] if row else 0)
    request_count = _int_value(row["request_count"] if row else 0)
    return UsageTotals(
        request_count=request_count,
        input_tokens=_int_value(row["input_tokens"] if row else 0),
        output_tokens=_int_value(row["output_tokens"] if row else 0),
        total_tokens=_int_value(row["total_tokens"] if row else 0),
        cache_tokens=_int_value(row["cache_tokens"] if row else 0),
        cost_cny=_float_value(row["cost_cny"] if row else 0),
        cost_usd=_float_value(row["cost_usd"] if row else 0),
        estimated_records=estimated_records,
        real_usage_records=max(request_count - estimated_records, 0),
    )


def _load_by_day(connection: sqlite3.Connection, where_sql: str, params: list[object]) -> list[UsageDaySummary]:
    """按日期聚合当前筛选条件下的用量，供控制台趋势展示。"""

    rows = connection.execute(
        f"""
        SELECT
          substr(ul.created_at, 1, 10) AS day,
          COUNT(*) AS request_count,
          COALESCE(SUM(ul.input_tokens), 0) AS input_tokens,
          COALESCE(SUM(ul.output_tokens), 0) AS output_tokens,
          COALESCE(SUM(ul.total_tokens), 0) AS total_tokens,
          COALESCE(SUM(ul.cache_tokens), 0) AS cache_tokens,
          COALESCE(SUM(ul.cost_cny), 0) AS cost_cny,
          COALESCE(SUM(ul.cost_usd), 0) AS cost_usd,
          COALESCE(SUM(CASE WHEN ul.estimated=1 THEN 1 ELSE 0 END), 0) AS estimated_records
        FROM usage_ledger ul
        LEFT JOIN request_logs rl ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
        {where_sql}
        GROUP BY substr(ul.created_at, 1, 10)
        ORDER BY day ASC
        """,
        params,
    ).fetchall()
    return [
        UsageDaySummary(
            day=str(row["day"]),
            request_count=_int_value(row["request_count"]),
            input_tokens=_int_value(row["input_tokens"]),
            output_tokens=_int_value(row["output_tokens"]),
            total_tokens=_int_value(row["total_tokens"]),
            cache_tokens=_int_value(row["cache_tokens"]),
            cost_cny=_float_value(row["cost_cny"]),
            cost_usd=_float_value(row["cost_usd"]),
            estimated_records=_int_value(row["estimated_records"]),
        )
        for row in rows
    ]


def _load_by_model(connection: sqlite3.Connection, where_sql: str, params: list[object]) -> list[UsageModelSummary]:
    """按模型和 Provider 聚合用量，帮助定位主要成本来源。"""

    rows = connection.execute(
        f"""
        SELECT
          COALESCE(ul.model_id, rl.model_id) AS model_id,
          COALESCE(ul.resolved_model, rl.resolved_model) AS resolved_model,
          COALESCE(ul.provider_id, rl.provider_id) AS provider_id,
          COALESCE(ul.provider_code, rl.provider_code) AS provider_code,
          COUNT(*) AS request_count,
          COALESCE(SUM(ul.input_tokens), 0) AS input_tokens,
          COALESCE(SUM(ul.output_tokens), 0) AS output_tokens,
          COALESCE(SUM(ul.total_tokens), 0) AS total_tokens,
          COALESCE(SUM(ul.cache_tokens), 0) AS cache_tokens,
          COALESCE(SUM(ul.cost_cny), 0) AS cost_cny,
          COALESCE(SUM(ul.cost_usd), 0) AS cost_usd,
          COALESCE(SUM(CASE WHEN ul.estimated=1 THEN 1 ELSE 0 END), 0) AS estimated_records
        FROM usage_ledger ul
        LEFT JOIN request_logs rl ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
        {where_sql}
        GROUP BY
          COALESCE(ul.model_id, rl.model_id),
          COALESCE(ul.resolved_model, rl.resolved_model),
          COALESCE(ul.provider_id, rl.provider_id),
          COALESCE(ul.provider_code, rl.provider_code)
        ORDER BY total_tokens DESC, request_count DESC
        LIMIT 20
        """,
        params,
    ).fetchall()
    return [
        UsageModelSummary(
            model_id=row["model_id"],
            resolved_model=row["resolved_model"],
            provider_id=row["provider_id"],
            provider_code=row["provider_code"],
            request_count=_int_value(row["request_count"]),
            input_tokens=_int_value(row["input_tokens"]),
            output_tokens=_int_value(row["output_tokens"]),
            total_tokens=_int_value(row["total_tokens"]),
            cache_tokens=_int_value(row["cache_tokens"]),
            cost_cny=_float_value(row["cost_cny"]),
            cost_usd=_float_value(row["cost_usd"]),
            estimated_records=_int_value(row["estimated_records"]),
        )
        for row in rows
    ]


def _load_by_virtual_key(
    connection: sqlite3.Connection,
    where_sql: str,
    params: list[object],
) -> list[UsageVirtualKeySummary]:
    """按 Virtual Key 聚合用量，只展示脱敏前缀用于定位调用方。"""

    rows = connection.execute(
        f"""
        SELECT
          COALESCE(ul.virtual_key_id, rl.virtual_key_id) AS virtual_key_id,
          vk.key_prefix AS virtual_key_prefix,
          COUNT(*) AS request_count,
          COALESCE(SUM(ul.input_tokens), 0) AS input_tokens,
          COALESCE(SUM(ul.output_tokens), 0) AS output_tokens,
          COALESCE(SUM(ul.total_tokens), 0) AS total_tokens,
          COALESCE(SUM(ul.cache_tokens), 0) AS cache_tokens,
          COALESCE(SUM(ul.cost_cny), 0) AS cost_cny,
          COALESCE(SUM(ul.cost_usd), 0) AS cost_usd,
          COALESCE(SUM(CASE WHEN ul.estimated=1 THEN 1 ELSE 0 END), 0) AS estimated_records
        FROM usage_ledger ul
        LEFT JOIN request_logs rl ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
        LEFT JOIN virtual_keys vk ON vk.tenant_id=ul.tenant_id AND vk.virtual_key_id=COALESCE(ul.virtual_key_id, rl.virtual_key_id)
        {where_sql}
        GROUP BY COALESCE(ul.virtual_key_id, rl.virtual_key_id), vk.key_prefix
        ORDER BY total_tokens DESC, request_count DESC
        LIMIT 20
        """,
        params,
    ).fetchall()
    return [
        UsageVirtualKeySummary(
            virtual_key_id=row["virtual_key_id"],
            virtual_key_prefix=row["virtual_key_prefix"],
            request_count=_int_value(row["request_count"]),
            input_tokens=_int_value(row["input_tokens"]),
            output_tokens=_int_value(row["output_tokens"]),
            total_tokens=_int_value(row["total_tokens"]),
            cache_tokens=_int_value(row["cache_tokens"]),
            cost_cny=_float_value(row["cost_cny"]),
            cost_usd=_float_value(row["cost_usd"]),
            estimated_records=_int_value(row["estimated_records"]),
        )
        for row in rows
    ]


def _load_by_consumer(
    connection: sqlite3.Connection, where_sql: str, params: list[object], context: AuthContext
) -> list[UsageConsumerSummary]:
    labels = False
    try:
        require_consumer_admin(context)
        require_permission(context, "virtual_key.read")
        labels = True
    except ApiError as exc:
        if exc.code != "auth.permission_denied":
            raise
    label_sql = "c.display_name AS consumer_name, c.department AS consumer_department," if labels else ""
    label_join = (
        "LEFT JOIN workspace_consumers c ON c.tenant_id=kc.tenant_id AND c.consumer_id=kc.consumer_id"
        if labels else ""
    )
    label_group = ", c.display_name, c.department" if labels else ""
    rows = connection.execute(
        f"""
        SELECT kc.consumer_id, {label_sql} COUNT(*) AS request_count,
          COALESCE(SUM(ul.input_tokens), 0) AS input_tokens,
          COALESCE(SUM(ul.output_tokens), 0) AS output_tokens,
          COALESCE(SUM(ul.total_tokens), 0) AS total_tokens,
          COALESCE(SUM(ul.cache_tokens), 0) AS cache_tokens,
          COALESCE(SUM(ul.cost_cny), 0) AS cost_cny,
          COALESCE(SUM(ul.cost_usd), 0) AS cost_usd,
          COALESCE(SUM(CASE WHEN ul.estimated=1 THEN 1 ELSE 0 END), 0) AS estimated_records,
          COALESCE(SUM(CASE WHEN ul.estimated=0 THEN 1 ELSE 0 END), 0) AS real_usage_records
        FROM usage_ledger ul
        LEFT JOIN request_logs rl ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
        LEFT JOIN virtual_key_consumers kc ON kc.tenant_id=ul.tenant_id
          AND kc.virtual_key_id=COALESCE(ul.virtual_key_id, rl.virtual_key_id)
        {label_join}
        {where_sql}
        GROUP BY kc.consumer_id{label_group}
        ORDER BY total_tokens DESC, kc.consumer_id
        """,
        params,
    ).fetchall()
    return [UsageConsumerSummary(**dict(row)) for row in rows]


def _load_by_environment(
    connection: sqlite3.Connection,
    where_sql: str,
    params: list[object],
) -> list[UsageEnvironmentSummary]:
    """按 Environment 聚合当前筛选条件下的 Token 与成本。"""

    rows = connection.execute(
        f"""
        SELECT
          COALESCE(ul.environment_id, rl.environment_id) AS environment_id,
          COALESCE(ul.environment_code, rl.environment_code) AS environment,
          e.name AS environment_name,
          COUNT(*) AS request_count,
          COALESCE(SUM(ul.input_tokens), 0) AS input_tokens,
          COALESCE(SUM(ul.output_tokens), 0) AS output_tokens,
          COALESCE(SUM(ul.total_tokens), 0) AS total_tokens,
          COALESCE(SUM(ul.cache_tokens), 0) AS cache_tokens,
          COALESCE(SUM(ul.cost_cny), 0) AS cost_cny,
          COALESCE(SUM(ul.cost_usd), 0) AS cost_usd,
          COALESCE(SUM(CASE WHEN ul.estimated=1 THEN 1 ELSE 0 END), 0) AS estimated_records
        FROM usage_ledger ul
        LEFT JOIN request_logs rl ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
        LEFT JOIN environments e
          ON e.tenant_id=ul.tenant_id
         AND e.environment_id=COALESCE(ul.environment_id, rl.environment_id)
        {where_sql}
        GROUP BY
          COALESCE(ul.environment_id, rl.environment_id),
          COALESCE(ul.environment_code, rl.environment_code),
          e.name
        ORDER BY total_tokens DESC, request_count DESC
        """,
        params,
    ).fetchall()
    return [
        UsageEnvironmentSummary(
            environment_id=row["environment_id"],
            environment=row["environment"],
            environment_name=row["environment_name"],
            request_count=_int_value(row["request_count"]),
            input_tokens=_int_value(row["input_tokens"]),
            output_tokens=_int_value(row["output_tokens"]),
            total_tokens=_int_value(row["total_tokens"]),
            cache_tokens=_int_value(row["cache_tokens"]),
            cost_cny=_float_value(row["cost_cny"]),
            cost_usd=_float_value(row["cost_usd"]),
            estimated_records=_int_value(row["estimated_records"]),
        )
        for row in rows
    ]


def _load_by_project(
    connection: sqlite3.Connection,
    where_sql: str,
    params: list[object],
) -> list[UsageProjectSummary]:
    """按项目聚合当前筛选条件下的用量和成本，供项目负责人治理预算归属。"""

    rows = connection.execute(
        f"""
        SELECT
          COALESCE(ul.project_id, rl.project_id) AS project_id,
          p.name AS project_name,
          COUNT(*) AS request_count,
          COALESCE(SUM(ul.input_tokens), 0) AS input_tokens,
          COALESCE(SUM(ul.output_tokens), 0) AS output_tokens,
          COALESCE(SUM(ul.total_tokens), 0) AS total_tokens,
          COALESCE(SUM(ul.cache_tokens), 0) AS cache_tokens,
          COALESCE(SUM(ul.cost_cny), 0) AS cost_cny,
          COALESCE(SUM(ul.cost_usd), 0) AS cost_usd,
          COALESCE(SUM(CASE WHEN ul.estimated=1 THEN 1 ELSE 0 END), 0) AS estimated_records
        FROM usage_ledger ul
        LEFT JOIN request_logs rl ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
        LEFT JOIN projects p ON p.tenant_id=ul.tenant_id AND p.project_id=COALESCE(ul.project_id, rl.project_id)
        {where_sql}
        GROUP BY COALESCE(ul.project_id, rl.project_id), p.name
        ORDER BY total_tokens DESC, request_count DESC
        LIMIT 20
        """,
        params,
    ).fetchall()
    return [
        UsageProjectSummary(
            project_id=row["project_id"],
            project_name=row["project_name"],
            request_count=_int_value(row["request_count"]),
            input_tokens=_int_value(row["input_tokens"]),
            output_tokens=_int_value(row["output_tokens"]),
            total_tokens=_int_value(row["total_tokens"]),
            cache_tokens=_int_value(row["cache_tokens"]),
            cost_cny=_float_value(row["cost_cny"]),
            cost_usd=_float_value(row["cost_usd"]),
            estimated_records=_int_value(row["estimated_records"]),
        )
        for row in rows
    ]


def _load_by_application(
    connection: sqlite3.Connection,
    where_sql: str,
    params: list[object],
) -> list[UsageApplicationSummary]:
    """按应用聚合当前筛选条件下的用量和成本，细分同一项目内不同业务系统。"""

    rows = connection.execute(
        f"""
        SELECT
          COALESCE(ul.application_id, rl.application_id) AS application_id,
          a.name AS application_name,
          COALESCE(ul.project_id, rl.project_id) AS project_id,
          p.name AS project_name,
          COUNT(*) AS request_count,
          COALESCE(SUM(ul.input_tokens), 0) AS input_tokens,
          COALESCE(SUM(ul.output_tokens), 0) AS output_tokens,
          COALESCE(SUM(ul.total_tokens), 0) AS total_tokens,
          COALESCE(SUM(ul.cache_tokens), 0) AS cache_tokens,
          COALESCE(SUM(ul.cost_cny), 0) AS cost_cny,
          COALESCE(SUM(ul.cost_usd), 0) AS cost_usd,
          COALESCE(SUM(CASE WHEN ul.estimated=1 THEN 1 ELSE 0 END), 0) AS estimated_records
        FROM usage_ledger ul
        LEFT JOIN request_logs rl ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
        LEFT JOIN projects p ON p.tenant_id=ul.tenant_id AND p.project_id=COALESCE(ul.project_id, rl.project_id)
        LEFT JOIN applications a ON a.tenant_id=ul.tenant_id AND a.application_id=COALESCE(ul.application_id, rl.application_id)
        {where_sql}
        GROUP BY COALESCE(ul.application_id, rl.application_id), a.name, COALESCE(ul.project_id, rl.project_id), p.name
        ORDER BY total_tokens DESC, request_count DESC
        LIMIT 20
        """,
        params,
    ).fetchall()
    return [
        UsageApplicationSummary(
            application_id=row["application_id"],
            application_name=row["application_name"],
            project_id=row["project_id"],
            project_name=row["project_name"],
            request_count=_int_value(row["request_count"]),
            input_tokens=_int_value(row["input_tokens"]),
            output_tokens=_int_value(row["output_tokens"]),
            total_tokens=_int_value(row["total_tokens"]),
            cache_tokens=_int_value(row["cache_tokens"]),
            cost_cny=_float_value(row["cost_cny"]),
            cost_usd=_float_value(row["cost_usd"]),
            estimated_records=_int_value(row["estimated_records"]),
        )
        for row in rows
    ]

def _usage_record_info(row: sqlite3.Row) -> UsageRecordInfo:
    """把 usage_ledger 查询行转换成 API DTO，并保证布尔值和 JSON 字段稳定。"""

    total_tokens = _int_value(row["total_tokens"])
    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    if total_tokens <= 0:
        total_tokens = _int_value(row["input_tokens"]) + _int_value(row["output_tokens"])
    return UsageRecordInfo(
        usage_ledger_id=row["usage_ledger_id"],
        consumer_id=row["consumer_id"],
        tenant_id=row["tenant_id"],
        request_id=row["request_id"],
        trace_id=row["trace_id"],
        virtual_key_id=row["virtual_key_id"],
        virtual_key_prefix=row["virtual_key_prefix"],
        environment_id=row["environment_id"],
        environment=row["environment"],
        environment_name=row["environment_name"],
        project_id=row["project_id"],
        project_name=row["project_name"],
        application_id=row["application_id"],
        application_name=row["application_name"],
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        model_id=row["model_id"],
        request_model=row["request_model"],
        resolved_model=row["resolved_model"],
        endpoint=row["endpoint"],
        input_tokens=_int_value(row["input_tokens"]),
        output_tokens=_int_value(row["output_tokens"]),
        total_tokens=total_tokens,
        cache_tokens=_int_value(row["cache_tokens"]),
        cost_amount=_float_value(row["cost_amount"]),
        cost_cny=_float_value(row["cost_cny"]),
        cost_usd=_float_value(row["cost_usd"]),
        currency=row["currency"] or "CNY",
        price_schema_version=row["price_schema_version"],
        price_snapshot_id=row["price_snapshot_id"],
        estimated=bool(row["estimated"]),
        usage_source=row["usage_source"] or "upstream",
        normalized_usage=_json_object(row["normalized_usage"]),
        provider_raw_usage=sanitize_provider_usage(_json_object(row["raw_usage"])),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_optional_db_time(row["updated_at"]),
    )


def _normalize_date_range(start_date: str | None, end_date: str | None) -> tuple[str | None, str | None]:
    """把 YYYY-MM-DD 查询日期转换成数据库 ISO 时间边界。"""

    start_value = _parse_date(start_date) if start_date else None
    end_value = _parse_date(end_date) if end_date else None
    if start_value and end_value and start_value > end_value:
        # 日期范围必须先校验再进入 SQL，避免出现反向区间导致统计结果被误读。
        raise ApiError("usage.date_invalid")
    start_bound = f"{start_value.isoformat()}T00:00:00Z" if start_value else None
    end_bound = f"{end_value.isoformat()}T23:59:59Z" if end_value else None
    return start_bound, end_bound


def _parse_date(value: str) -> date:
    """解析 API 日期参数，任何非 YYYY-MM-DD 输入都映射为统一业务错误。"""

    try:
        return date.fromisoformat(value)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (TypeError, ValueError) as exc:
        # 日期格式错误统一映射为业务错误码，避免框架默认 422 破坏统一错误结构。
        raise ApiError("usage.date_invalid") from exc


def _normalize_optional_filter(value: str | None) -> str | None:
    """校验可选字符串筛选条件，防止超长输入进入 SQL 参数。"""

    if value is None or value == "":
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_FILTER_LENGTH:
        # 字符串筛选条件必须限制长度，避免异常输入扩大查询成本或污染审计字段。
        raise ApiError("usage.query_invalid")
    return normalized


def _normalize_optional_bool(value: str | None) -> bool | None:
    """解析 estimated 查询参数，支持 true/false/1/0 并拒绝模糊值。"""

    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    # estimated 只能是明确布尔值，避免模糊字符串造成估算/真实 usage 统计口径混乱。
    raise ApiError("usage.query_invalid")


def _normalize_limit(value: int) -> int:
    """限制分页大小，避免一次性扫描过多用量账本。"""

    try:
        limit = int(value)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (TypeError, ValueError) as exc:
        # 分页参数类型错误统一映射为 usage 查询错误，保证响应包含 request_id。
        raise ApiError("usage.query_invalid") from exc
    if limit < 1 or limit > MAX_LIMIT:
        # 分页大小必须受限，避免控制台查询一次性扫过多租户内账本记录。
        raise ApiError("usage.query_invalid")
    return limit


def _normalize_offset(value: int) -> int:
    """规范分页偏移量，负数视为非法查询。"""

    try:
        offset = int(value)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (TypeError, ValueError) as exc:
        # 分页偏移类型错误统一映射为 usage 查询错误，避免底层异常穿透到 API。
        raise ApiError("usage.query_invalid") from exc
    if offset < 0:
        # 负数偏移没有业务含义，必须在进入数据库前拒绝。
        raise ApiError("usage.query_invalid")
    return offset


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面配置指定的数据库，并把底层连接错误映射为统一业务错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 数据库连接错误保留统一错误码和建议，不把 sqlite 细节直接暴露给调用方。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _json_object(value: object) -> dict[str, Any]:
    """安全解析 JSON 文本，解析失败或非对象时返回空对象。"""

    try:
        parsed = json.loads(str(value or "{}"))
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_value(value: object) -> int:
    """把数据库数值字段安全转换成非负整数。"""

    try:
        parsed = int(value or 0)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _float_value(value: object) -> float:
    """把数据库金额字段安全转换成浮点数供 JSON 响应使用。"""

    try:
        return float(value or 0)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except (TypeError, ValueError):
        return 0.0


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析可空数据库时间字段，空值保持为 None。"""

    if not value:
        return None
    return _parse_db_time(value)


def _parse_db_time(value: object) -> datetime:
    """解析数据库 ISO 时间，并统一转换成 UTC datetime。"""

    raw = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
