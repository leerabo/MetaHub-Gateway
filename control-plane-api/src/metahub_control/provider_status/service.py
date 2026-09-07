"""PRD-061 Provider 状态页只读聚合服务。

服务在一个显式只读事务和同一 UTC 时刻内合并 Provider 元数据、真实调用与探测事实。
所有查询都以认证上下文中的 tenant_id 为首条件；任何损坏的历史事实都会失败关闭为统一 503，
不能把不可判定的数据静默排除后继续展示看似精确的成功率。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ERROR_SPECS, ApiError
from metahub_shared.runtime.provider_adapter_spec import provider_types

from metahub_control.auth import AuthContext, require_permission
from metahub_control.auth.resource_transactions import ResourceWriteTransaction

from .models import (
    ProviderFailureItem,
    ProviderStatusItem,
    ProviderStatusListResponse,
    ProviderStatusSummary,
)

# Provider、健康、排序与安全枚举全部由后端冻结，页面不能根据自由文本自行扩展业务口径。
PROVIDER_TYPES = provider_types()
EFFECTIVE_STATUSES = {"healthy", "unhealthy", "observing", "unknown", "disabled"}
PROVIDER_STATUSES = {"active", "disabled"}
AVAILABILITY_BY_HEALTH = {
    "healthy": "available",
    "observing": "degraded",
    "unhealthy": "unavailable",
    "unknown": "unknown",
    "disabled": "disabled",
}
STATUS_ORDER = {"unhealthy": 0, "observing": 1, "unknown": 2, "healthy": 3, "disabled": 4}
SAFE_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,119}$")
LEGACY_UTC_TIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$")
# JSON 数字会由浏览器按 IEEE-754 解析；超过安全整数会让计数和 P95 在页面端静默失真。
MAX_SAFE_INTEGER = 9_007_199_254_740_991
# 调用事实只接受平台统一错误目录中的 Gateway/Provider Key 码，不能把历史自由文本伪装成 code。
CALL_ERROR_CODES = frozenset(
    code
    for code in ERROR_SPECS
    if code.startswith("gateway.") or code == "provider_key.unavailable"
)
# PRD-035 探测器持久化的固定枚举与运行时实现保持显式同步，便于审计新增分类。
PROBE_ERROR_CODES = frozenset(
    {
        "authentication_failed",
        "connect_failed",
        "credential_sample_limited",
        "credential_unavailable",
        "endpoint_blocked",
        "endpoint_invalid",
        "probe_http_error",
        "probe_unsupported",
        "rate_limited",
        "redirect_rejected",
        "timeout",
        "tls_failed",
        "transport_error",
        "upstream_5xx",
    }
)
PROBE_FAILURE_CODES = PROBE_ERROR_CODES - {
    "credential_sample_limited",
    "probe_unsupported",
    "rate_limited",
}
PROBE_INCONCLUSIVE_CODES = frozenset(
    {"credential_sample_limited", "probe_unsupported", "rate_limited"}
)


class _DataContractError(ValueError):
    """表示历史事实无法按冻结口径解释；异常正文永不进入 API 或日志。"""


@dataclass(frozen=True)
class _ProviderBase:
    """分页后 Provider 的内部脱敏元数据与派生健康状态。"""

    row: sqlite3.Row
    effective_health_status: str
    is_health_stale: bool
    safe_base_url: str


@dataclass
class _CallMetrics:
    """一个 Provider 的真实调用计数、延迟样本和最近观测。"""

    terminal: int = 0
    eligible: int = 0
    success: int = 0
    provider_failure: int = 0
    credential_failure: int = 0
    excluded: int = 0
    call_latency_sample_count: int = 0
    call_latency_p95_ms: int | None = None
    stream_first_chunk_sample_count: int = 0
    stream_first_chunk_p95_ms: int | None = None
    latest_call_at: datetime | None = None


@dataclass
class _ProbeMetrics:
    """一个 Provider 的完整探测组计数、代表延迟和最近确定性观测。"""

    total: int = 0
    success: int = 0
    failure: int = 0
    inconclusive: int = 0
    latencies: list[int] = field(default_factory=list)
    latest_probe_at: datetime | None = None


@dataclass(frozen=True)
class _FailureFact:
    """带内部稳定主键的故障事实；stable_id 只参与排序，不进入响应。"""

    provider_id: str
    source: str
    failure_kind: str
    eligible_for_success_rate: bool
    error_code: str
    request_id: str
    trace_id: str
    latency_ms: int | None
    observed_at: datetime
    stable_id: str

    def to_dto(self) -> ProviderFailureItem:
        """转换为公开安全 DTO，并故意丢弃底层事实主键。"""

        return ProviderFailureItem(
            source=self.source,
            failure_kind=self.failure_kind,
            eligible_for_success_rate=self.eligible_for_success_rate,
            error_code=self.error_code,
            request_id=self.request_id,
            trace_id=self.trace_id,
            latency_ms=self.latency_ms,
            observed_at=self.observed_at,
        )


def list_provider_statuses(
    config: AppConfig,
    context: AuthContext,
    *,
    provider_id: str | None,
    provider_type: str | None,
    health_status: str | None,
    provider_status: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> ProviderStatusListResponse:
    """返回当前租户分页 Provider 状态。

    参数只允许固定筛选和分页；返回值使用同一 `now` 计算 24 小时指标与 7 天故障历史。
    数据库、Schema、时间或资格矩阵异常统一转换为 `provider_status.query_unavailable`。
    """

    normalized_provider_id = _normalize_provider_id(provider_id)
    normalized_type = _normalize_filter(provider_type, PROVIDER_TYPES)
    normalized_health = _normalize_filter(health_status, EFFECTIVE_STATUSES)
    normalized_provider_status = _normalize_filter(provider_status, PROVIDER_STATUSES)
    # 服务层重复约束分页，防止内部调用绕过路由的严格 ASCII 十进制解析。
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or not 0 <= offset <= 10000:
        raise ApiError("provider_status.pagination_invalid")

    now = _utcnow()
    window_started_at = now - timedelta(hours=24)
    history_started_at = now - timedelta(days=7)
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(config)
        # 显式事务确保 Provider、调用和探测均来自同一 SQLite 快照；GET 不提交任何业务写入。
        connection.execute("BEGIN")
        transaction = ResourceWriteTransaction(connection, config, context, "provider.health.read")
        transaction.begin()
        context = require_permission(transaction.context, "request_log.read")
        provider_rows = _load_provider_rows(
            connection,
            context.user.tenant_id,
            provider_id=normalized_provider_id,
            provider_type=normalized_type,
            provider_status=normalized_provider_status,
        )
        provider_bases = [_provider_base(row, config, now) for row in provider_rows]
        if normalized_health is not None:
            provider_bases = [
                provider for provider in provider_bases if provider.effective_health_status == normalized_health
            ]
        provider_bases.sort(
            key=lambda provider: (STATUS_ORDER[provider.effective_health_status], provider.row["provider_id"])
        )
        summary = _status_summary(provider_bases)
        total = len(provider_bases)
        page = provider_bases[offset : offset + limit]
        provider_ids = [str(provider.row["provider_id"]) for provider in page]
        call_metrics, call_failures = _load_call_facts(
            connection,
            context.user.tenant_id,
            provider_ids,
            window_started_at,
            now,
            history_started_at,
        )
        probe_metrics, probe_failures = _load_probe_facts(
            connection,
            context.user.tenant_id,
            provider_ids,
            window_started_at,
            now,
            history_started_at,
        )
        failures = _group_failures(provider_ids, call_failures + probe_failures)
        items = [
            _status_item(
                provider,
                call_metrics.get(provider.row["provider_id"], _CallMetrics()),
                probe_metrics.get(provider.row["provider_id"], _ProbeMetrics()),
                failures.get(provider.row["provider_id"], []),
            )
            for provider in page
        ]
        def finish_read(connection):
            require_permission(transaction.context, "request_log.read")
            connection.rollback()

        transaction.commit(finish_read)
    # 已归一化的领域错误保持原码；回滚只结束可能已经打开的只读快照。
    except ApiError:
        if connection is not None:
            _safe_rollback(connection)
        raise
    # 数据合同或基础设施异常一律脱敏成 503，禁止 SQL、路径或历史坏值穿透到响应。
    except (sqlite3.Error, DbError, _DataContractError, TypeError, ValueError, OverflowError):
        if connection is not None:
            _safe_rollback(connection)
        # 底层路径、SQL 和异常文本都不得穿透；request_id 由统一异常中间件补回响应。
        raise ApiError("provider_status.query_unavailable") from None
    finally:
        if connection is not None:
            connection.close()

    return ProviderStatusListResponse(
        provider_statuses=items,
        summary=summary,
        window_started_at=window_started_at,
        window_ended_at=now,
        history_started_at=history_started_at,
        total=total,
        limit=limit,
        offset=offset,
        request_id=request_id,
        trace_id=trace_id,
    )


def _load_provider_rows(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    provider_id: str | None,
    provider_type: str | None,
    provider_status: str | None,
) -> list[sqlite3.Row]:
    """按 tenant_id 首条件读取 Provider 元数据；跨租户 ID 自然表现为空集合。"""

    clauses = ["tenant_id=?"]
    params: list[object] = [tenant_id]
    for column, value in (
        ("provider_id", provider_id),
        ("provider_type", provider_type),
        ("status", provider_status),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    return connection.execute(
        f"""
        SELECT provider_id, tenant_id, provider_code, name, provider_type, base_url, auth_type,
               status, health_status, consecutive_probe_failures, health_updated_at,
               maintenance_note, last_tested_at, last_probe_latency_ms, created_at, updated_at
        FROM providers
        WHERE {' AND '.join(clauses)}
        ORDER BY provider_id ASC
        """,
        params,
    ).fetchall()


def _provider_base(row: sqlite3.Row, config: AppConfig, now: datetime) -> _ProviderBase:
    """验证 Provider 元数据并按 PRD-035 同一规则派生 effective 健康状态。"""

    # 未知状态无法映射为稳定页面语义，不能静默降级成“未知”掩盖历史数据损坏。
    if row["status"] not in PROVIDER_STATUSES or row["health_status"] not in {"unknown", "healthy", "unhealthy"}:
        raise _DataContractError()
    failures = row["consecutive_probe_failures"]
    # 连续失败数参与 observing 判定，负数或非整数会反转健康语义，必须失败关闭。
    if type(failures) is not int or failures < 0:
        raise _DataContractError()
    health_updated_at = _parse_optional_time(row["health_updated_at"])
    stale = health_updated_at is None or health_updated_at < now - timedelta(
        seconds=config.provider_health_stale_seconds
    )
    if row["status"] != "active":
        effective = "disabled"
    elif failures > 0 and row["health_status"] != "unhealthy" and not stale:
        effective = "observing"
    elif stale:
        effective = "unknown"
    else:
        effective = str(row["health_status"])
    maintenance_note = row["maintenance_note"]
    # 旁路写入的超长或控制字符备注不得进入授权页面 DOM，即使数据库来自旧版本或手工维护。
    if maintenance_note is not None and not _valid_maintenance_note(maintenance_note):
        raise _DataContractError()
    # created/updated 会进入 DTO，因此在聚合前验证，避免局部响应含不可追踪时间。
    _parse_time(row["created_at"])
    _parse_time(row["updated_at"])
    _parse_optional_time(row["last_tested_at"])
    _safe_optional_nonnegative_integer(row["last_probe_latency_ms"])
    return _ProviderBase(
        row=row,
        effective_health_status=effective,
        is_health_stale=stale,
        safe_base_url=_safe_base_url(str(row["base_url"])),
    )


def _load_call_facts(
    connection: sqlite3.Connection,
    tenant_id: str,
    provider_ids: list[str],
    window_started_at: datetime,
    window_ended_at: datetime,
    history_started_at: datetime,
) -> tuple[dict[str, _CallMetrics], list[_FailureFact]]:
    """用 SQL 聚合真实调用，进程内只接收每 Provider 指标和最多五条故障。"""

    metrics = {provider_id: _CallMetrics() for provider_id in provider_ids}
    # 空分页无需构造 IN ()，同时保持每个输入 Provider 都已有零值指标容器。
    if not provider_ids:
        return metrics, []
    placeholders = ",".join("?" for _ in provider_ids)
    _assert_no_invalid_terminal_calls(connection, tenant_id, provider_ids, placeholders)
    start_us = _epoch_microseconds(window_started_at)
    end_us = _epoch_microseconds(window_ended_at)
    rows = connection.execute(
        f"""
        WITH non_stream_all AS (
          SELECT provider_id, metahub_epoch_us(completed_at) AS observed_us,
                 CASE WHEN status='succeeded' AND usage_status<>'not_applicable' THEN 1 ELSE 0 END AS success_count,
                 CASE WHEN status='failed' AND usage_status='unavailable' AND (
                   error_category IN ('timeout','rate_limited','upstream_5xx')
                   OR error_code='gateway.upstream_transport_failed'
                 ) THEN 1 ELSE 0 END AS provider_failure_count,
                 CASE WHEN status='failed' AND error_code IN (
                   'gateway.upstream_auth_failed','provider_key.unavailable'
                 ) THEN 1 ELSE 0 END AS credential_failure_count,
                 latency_ms AS non_stream_latency,
                 NULL AS stream_first_chunk_latency
          FROM routing_attempts
          WHERE tenant_id=? AND provider_id IN ({placeholders})
            AND status IN ('succeeded','failed')
        ),
        stream_all AS (
          SELECT provider_id, metahub_epoch_us(completed_at) AS observed_us,
                 CASE WHEN status='completed' THEN 1 ELSE 0 END AS success_count,
                 CASE WHEN status='timed_out' OR (
                   status='upstream_failed' AND error_code IN (
                     'gateway.upstream_rate_limited','gateway.upstream_timeout',
                     'gateway.upstream_stream_timeout','gateway.upstream_stream_failed'
                   )
                 ) THEN 1 ELSE 0 END AS provider_failure_count,
                 CASE WHEN status='upstream_failed' AND error_code IN (
                   'gateway.upstream_auth_failed','provider_key.unavailable'
                 ) THEN 1 ELSE 0 END AS credential_failure_count,
                 NULL AS non_stream_latency,
                 CASE WHEN first_chunk_at IS NOT NULL
                   THEN metahub_elapsed_ms(started_at,first_chunk_at) ELSE NULL END
                   AS stream_first_chunk_latency
          FROM stream_sessions
          WHERE tenant_id=? AND provider_id IN ({placeholders})
            AND status IN ('completed','timed_out','client_disconnected','upstream_failed')
        ),
        window_calls AS (
          SELECT *, success_count+provider_failure_count AS eligible_count
          FROM (
            SELECT * FROM non_stream_all
            UNION ALL
            SELECT * FROM stream_all
          )
          WHERE observed_us>=? AND observed_us<?
        ),
        counts AS (
          SELECT provider_id, COUNT(*) AS terminal_count,
                 SUM(eligible_count) AS eligible_count,
                 SUM(success_count) AS success_count,
                 SUM(provider_failure_count) AS provider_failure_count,
                 SUM(credential_failure_count) AS credential_failure_count,
                 SUM(1-eligible_count) AS excluded_count,
                 MAX(observed_us) AS latest_call_us
          FROM window_calls GROUP BY provider_id
        ),
        call_ranked AS (
          SELECT provider_id,non_stream_latency,
                 ROW_NUMBER() OVER(PARTITION BY provider_id ORDER BY non_stream_latency) AS sample_rank,
                 COUNT(*) OVER(PARTITION BY provider_id) AS sample_count
          FROM window_calls
          WHERE eligible_count=1 AND non_stream_latency IS NOT NULL
        ),
        call_p95 AS (
          SELECT provider_id,MAX(sample_count) AS sample_count,
                 MAX(CASE WHEN sample_rank=((95*sample_count+99)/100)
                          THEN non_stream_latency END) AS p95_ms
          FROM call_ranked GROUP BY provider_id
        ),
        stream_ranked AS (
          SELECT provider_id,stream_first_chunk_latency,
                 ROW_NUMBER() OVER(PARTITION BY provider_id ORDER BY stream_first_chunk_latency) AS sample_rank,
                 COUNT(*) OVER(PARTITION BY provider_id) AS sample_count
          FROM window_calls WHERE stream_first_chunk_latency IS NOT NULL
        ),
        stream_p95 AS (
          SELECT provider_id,MAX(sample_count) AS sample_count,
                 MAX(CASE WHEN sample_rank=((95*sample_count+99)/100)
                          THEN stream_first_chunk_latency END) AS p95_ms
          FROM stream_ranked GROUP BY provider_id
        )
        SELECT counts.*,COALESCE(call_p95.sample_count,0) AS call_sample_count,
               call_p95.p95_ms AS call_p95_ms,
               COALESCE(stream_p95.sample_count,0) AS stream_sample_count,
               stream_p95.p95_ms AS stream_p95_ms
        FROM counts
        LEFT JOIN call_p95 ON call_p95.provider_id=counts.provider_id
        LEFT JOIN stream_p95 ON stream_p95.provider_id=counts.provider_id
        """,
        [tenant_id, *provider_ids, tenant_id, *provider_ids, start_us, end_us],
    ).fetchall()
    for row in rows:
        value = _CallMetrics(
            terminal=int(row["terminal_count"]),
            eligible=int(row["eligible_count"]),
            success=int(row["success_count"]),
            provider_failure=int(row["provider_failure_count"]),
            credential_failure=int(row["credential_failure_count"]),
            excluded=int(row["excluded_count"]),
            call_latency_sample_count=int(row["call_sample_count"]),
            call_latency_p95_ms=row["call_p95_ms"],
            stream_first_chunk_sample_count=int(row["stream_sample_count"]),
            stream_first_chunk_p95_ms=row["stream_p95_ms"],
            latest_call_at=_time_from_epoch_microseconds(int(row["latest_call_us"])),
        )
        _validate_call_identities(value)
        metrics[str(row["provider_id"])] = value
    return metrics, _load_call_failures(
        connection,
        tenant_id,
        provider_ids,
        placeholders,
        history_started_at,
        window_ended_at,
    )


def _assert_no_invalid_terminal_calls(
    connection: sqlite3.Connection,
    tenant_id: str,
    provider_ids: list[str],
    placeholders: str,
) -> None:
    """在裁窗前由 SQLite 扫描全部终态合同，只返回一个异常计数。"""

    non_stream_invalid = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM routing_attempts
        WHERE tenant_id=? AND provider_id IN ({placeholders})
          AND status IN ('succeeded','failed')
          AND (
            typeof(latency_ms)<>'integer' OR latency_ms<0 OR latency_ms>9007199254740991
            OR completed_at IS NULL
            OR metahub_epoch_us(completed_at) IS NULL
            OR (status='succeeded' AND (error_code IS NOT NULL OR error_category IS NOT NULL))
            OR (status='failed' AND error_code IS NULL)
            OR (status='failed' AND (
              error_category IN ('timeout','rate_limited','upstream_5xx')
              OR error_code='gateway.upstream_transport_failed'
            ) AND usage_status<>'unavailable')
            OR (error_code IS NOT NULL AND metahub_safe_error_code(error_code) IS NULL)
          )
        """,
        [tenant_id, *provider_ids],
    ).fetchone()[0]
    stream_invalid = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM stream_sessions
        WHERE tenant_id=? AND provider_id IN ({placeholders})
          AND status IN ('completed','timed_out','client_disconnected','upstream_failed')
          AND (
            started_at IS NULL OR completed_at IS NULL
            OR metahub_epoch_us(started_at) IS NULL OR metahub_epoch_us(completed_at) IS NULL
            OR metahub_epoch_us(completed_at)<metahub_epoch_us(started_at)
            OR (first_chunk_at IS NOT NULL AND (
              metahub_epoch_us(first_chunk_at)<metahub_epoch_us(started_at)
              OR metahub_epoch_us(first_chunk_at)>metahub_epoch_us(completed_at)
            ))
            OR (status='completed' AND error_code IS NOT NULL)
            OR (status='timed_out' AND (
              error_code IS NULL OR error_code<>'gateway.upstream_stream_timeout'
            ))
            OR (status='client_disconnected' AND (
              error_code IS NULL OR error_code<>'gateway.client_disconnected'
            ))
            OR (status='upstream_failed' AND error_code IS NULL)
            OR (error_code IS NOT NULL AND metahub_safe_error_code(error_code) IS NULL)
          )
        """,
        [tenant_id, *provider_ids],
    ).fetchone()[0]
    # 任一终态合同异常都会让六项计数失去可解释性，整页统一 503 而不是漏掉坏行。
    if non_stream_invalid or stream_invalid:
        raise _DataContractError()


def _load_call_failures(
    connection: sqlite3.Connection,
    tenant_id: str,
    provider_ids: list[str],
    placeholders: str,
    history_started_at: datetime,
    window_ended_at: datetime,
) -> list[_FailureFact]:
    """用 UNION 与窗口排序只读取每个 Provider 最近五条调用故障。"""

    rows = connection.execute(
        f"""
        WITH failure_union AS (
          SELECT provider_id,'non_stream_call' AS source,
                 CASE
                   WHEN error_code IN ('gateway.upstream_auth_failed','provider_key.unavailable')
                     THEN 'credential'
                   WHEN usage_status='unavailable' AND (
                     error_category IN ('timeout','rate_limited','upstream_5xx')
                     OR error_code='gateway.upstream_transport_failed'
                   ) THEN 'provider'
                   ELSE 'excluded'
                 END AS failure_kind,
                 CASE WHEN usage_status='unavailable' AND (
                   error_category IN ('timeout','rate_limited','upstream_5xx')
                   OR error_code='gateway.upstream_transport_failed'
                 ) THEN 1 ELSE 0 END AS eligible,
                 metahub_safe_error_code(error_code) AS error_code,
                 request_id,trace_id,latency_ms,
                 metahub_epoch_us(completed_at) AS observed_us,
                 routing_attempt_id AS stable_id
          FROM routing_attempts
          WHERE tenant_id=? AND provider_id IN ({placeholders})
            AND status='failed' AND error_code IS NOT NULL
            AND metahub_epoch_us(completed_at)>=? AND metahub_epoch_us(completed_at)<?
          UNION ALL
          SELECT provider_id,'stream_call',
                 CASE
                   WHEN error_code IN ('gateway.upstream_auth_failed','provider_key.unavailable')
                     THEN 'credential'
                   WHEN status='timed_out' OR (
                     status='upstream_failed' AND error_code IN (
                       'gateway.upstream_rate_limited','gateway.upstream_timeout',
                       'gateway.upstream_stream_timeout','gateway.upstream_stream_failed'
                     )
                   ) THEN 'provider'
                   ELSE 'excluded'
                 END,
                 CASE WHEN status='timed_out' OR (
                   status='upstream_failed' AND error_code IN (
                     'gateway.upstream_rate_limited','gateway.upstream_timeout',
                     'gateway.upstream_stream_timeout','gateway.upstream_stream_failed'
                   )
                 ) THEN 1 ELSE 0 END,
                 metahub_safe_error_code(error_code),request_id,trace_id,
                 metahub_elapsed_ms(started_at,completed_at),
                 metahub_epoch_us(completed_at),stream_session_id
          FROM stream_sessions
          WHERE tenant_id=? AND provider_id IN ({placeholders})
            AND status IN ('timed_out','client_disconnected','upstream_failed')
            AND error_code IS NOT NULL
            AND metahub_epoch_us(completed_at)>=? AND metahub_epoch_us(completed_at)<?
        ),
        ranked AS (
          SELECT *,ROW_NUMBER() OVER(
            PARTITION BY provider_id
            ORDER BY observed_us DESC,source ASC,request_id ASC,stable_id ASC
          ) AS failure_rank
          FROM failure_union
        )
        SELECT * FROM ranked WHERE failure_rank<=5
        ORDER BY provider_id,observed_us DESC,source,request_id,stable_id
        """,
        [
            tenant_id,
            *provider_ids,
            _epoch_microseconds(history_started_at),
            _epoch_microseconds(window_ended_at),
            tenant_id,
            *provider_ids,
            _epoch_microseconds(history_started_at),
            _epoch_microseconds(window_ended_at),
        ],
    ).fetchall()
    return [
        _FailureFact(
            provider_id=_safe_identifier(row["provider_id"]),
            source=str(row["source"]),
            failure_kind=str(row["failure_kind"]),
            eligible_for_success_rate=bool(row["eligible"]),
            error_code=_safe_error_code(row["error_code"], CALL_ERROR_CODES),
            request_id=_safe_identifier(row["request_id"]),
            trace_id=_safe_identifier(row["trace_id"]),
            latency_ms=int(row["latency_ms"]),
            observed_at=_time_from_epoch_microseconds(int(row["observed_us"])),
            stable_id=_safe_identifier(row["stable_id"]),
        )
        for row in rows
    ]


def _validate_call_identities(metrics: _CallMetrics) -> None:
    """验证成功率六项恒等式及两个独立延迟样本合同。"""

    # 三个恒等式保证页面计数能由用户独立复算，任何部分聚合或重复分类都必须失败关闭。
    if (
        metrics.eligible != metrics.success + metrics.provider_failure
        or metrics.excluded != metrics.terminal - metrics.eligible
        or metrics.credential_failure > metrics.excluded
    ):
        # 恒等式不成立时任何成功率都会误导运维，必须拒绝整份状态快照。
        raise _DataContractError()
    for value in (
        metrics.terminal,
        metrics.eligible,
        metrics.success,
        metrics.provider_failure,
        metrics.credential_failure,
        metrics.excluded,
        metrics.call_latency_sample_count,
        metrics.stream_first_chunk_sample_count,
    ):
        _safe_nonnegative_integer(value)
    for sample_count, p95_ms in (
        (metrics.call_latency_sample_count, metrics.call_latency_p95_ms),
        (metrics.stream_first_chunk_sample_count, metrics.stream_first_chunk_p95_ms),
    ):
        # 空样本必须返回 null；有样本必须返回可由浏览器精确表示的非负 P95，防止伪造 0ms。
        if sample_count == 0:
            if p95_ms is not None:
                raise _DataContractError()
        else:
            _safe_nonnegative_integer(p95_ms)


def _load_probe_facts(
    connection: sqlite3.Connection,
    tenant_id: str,
    provider_ids: list[str],
    window_started_at: datetime,
    window_ended_at: datetime,
    history_started_at: datetime,
) -> tuple[dict[str, _ProbeMetrics], list[_FailureFact]]:
    """按 Provider、request 与健康代次分组探测 attempt，再生成指标与失败投影。"""

    metrics = {provider_id: _ProbeMetrics() for provider_id in provider_ids}
    # 空分页不访问探测大表，也避免生成非法的空 IN 子句。
    if not provider_ids:
        return metrics, []
    placeholders = ",".join("?" for _ in provider_ids)
    rows = connection.execute(
        f"""
        WITH candidate_probes AS (
          SELECT DISTINCT provider_id, request_id, health_generation, created_at
          FROM provider_health_probe_logs
          WHERE tenant_id=? AND provider_id IN ({placeholders})
            AND checked_at>=? AND checked_at<?
        )
        SELECT log.probe_log_id, log.provider_id, log.request_id, log.trace_id,
               log.health_generation,
               log.attempt_sequence, log.result_status, log.latency_ms,
               log.error_code, log.checked_at, log.created_at
        FROM provider_health_probe_logs log
        JOIN candidate_probes candidate
         ON candidate.provider_id=log.provider_id
         AND candidate.request_id=log.request_id
         AND candidate.health_generation=log.health_generation
         AND candidate.created_at=log.created_at
        WHERE log.tenant_id=? AND log.provider_id IN ({placeholders})
        ORDER BY log.provider_id, log.request_id, log.health_generation, log.created_at,
                 log.attempt_sequence, log.probe_log_id
        """,
        [
            tenant_id,
            *provider_ids,
            _db_time(history_started_at),
            _db_time(window_ended_at),
            tenant_id,
            *provider_ids,
        ],
    ).fetchall()
    groups: dict[tuple[str, str, int, datetime], list[sqlite3.Row]] = {}
    for row in rows:
        _validate_probe_attempt(row)
        # request_id/trace_id 都允许调用方透传，状态不变化时 health_generation 也不会递增；
        # 一次探测事务为全部 Key attempt 写入相同 created_at，因此四者共同构成持久化分组身份。
        # 这样既不会拆散一次多 Key 探测，也不会把重复链路标识的两个独立请求误判成重复序号。
        group_key = (
            str(row["provider_id"]),
            str(row["request_id"]),
            _safe_nonnegative_integer(row["health_generation"]),
            _parse_time(row["created_at"]),
        )
        groups.setdefault(group_key, []).append(row)

    failures: list[_FailureFact] = []
    for (
        provider_id,
        request_id,
        _health_generation,
        _probe_created_at,
    ), attempts in groups.items():
        trace_ids = {_safe_identifier(row["trace_id"]) for row in attempts}
        sequences = [int(row["attempt_sequence"]) for row in attempts]
        # 同一探测必须共享 trace 且 attempt_sequence 唯一，否则无法确定 PRD-035 的代表 attempt。
        if len(trace_ids) != 1 or len(sequences) != len(set(sequences)):
            raise _DataContractError()
        observed_at = max(_parse_time(row["checked_at"]) for row in attempts)
        # 完整分组后再二次判窗；含未来 attempt 的组不能被截断成历史成功/失败。
        if not history_started_at <= observed_at < window_ended_at:
            continue
        group_status = _probe_group_status(attempts)
        representative = min(
            (row for row in attempts if row["result_status"] == group_status),
            key=lambda row: (int(row["attempt_sequence"]), str(row["probe_log_id"])),
        )
        if window_started_at <= observed_at < window_ended_at:
            value = metrics[provider_id]
            value.total += 1
            if group_status == "success":
                value.success += 1
            elif group_status == "failed":
                value.failure += 1
            else:
                value.inconclusive += 1
            if group_status in {"success", "failed"}:
                value.latencies.append(int(representative["latency_ms"]))
                if value.latest_probe_at is None or observed_at > value.latest_probe_at:
                    value.latest_probe_at = observed_at
        if group_status == "failed":
            failures.append(
                _FailureFact(
                    provider_id=provider_id,
                    source="probe",
                    failure_kind="probe",
                    eligible_for_success_rate=False,
                    error_code=_safe_error_code(
                        representative["error_code"], PROBE_FAILURE_CODES
                    ),
                    request_id=_safe_identifier(request_id),
                    trace_id=next(iter(trace_ids)),
                    latency_ms=int(representative["latency_ms"]),
                    observed_at=observed_at,
                    stable_id=_safe_identifier(representative["probe_log_id"]),
                )
            )
    for value in metrics.values():
        # 探测总数必须由三个互斥终态完全组成，防止跨窗口分组产生部分计数。
        if value.total != value.success + value.failure + value.inconclusive:
            raise _DataContractError()
        for count in (
            value.total,
            value.success,
            value.failure,
            value.inconclusive,
            len(value.latencies),
        ):
            _safe_nonnegative_integer(count)
    return metrics, failures


def _validate_probe_attempt(row: sqlite3.Row) -> None:
    """验证探测 attempt 的时间、序号、延迟和固定状态，不接收猜测值。"""

    if (
        row["result_status"] not in {"success", "failed", "inconclusive"}
        or type(row["attempt_sequence"]) is not int
        or row["attempt_sequence"] <= 0
        or type(row["latency_ms"]) is not int
        or row["latency_ms"] < 0
        or row["latency_ms"] > MAX_SAFE_INTEGER
    ):
        # 序号和延迟决定代表行与 P95；宽松转换会让错误事实获得合法排序，因此直接拒绝。
        raise _DataContractError()
    _parse_time(row["checked_at"])
    _safe_identifier(row["provider_id"])
    _safe_identifier(row["request_id"])
    _safe_identifier(row["probe_log_id"])
    if row["result_status"] == "success":
        # success 与错误码互斥，保留错误码会把一次探测同时解释成成功和故障。
        if row["error_code"] is not None:
            raise _DataContractError()
    elif row["result_status"] == "failed":
        _safe_error_code(row["error_code"], PROBE_FAILURE_CODES)
    else:
        _safe_error_code(row["error_code"], PROBE_INCONCLUSIVE_CODES)


def _probe_group_status(attempts: list[sqlite3.Row]) -> str:
    """按 success 优先、failed 次之、全 inconclusive 最后的规则生成探测组终态。"""

    statuses = {str(row["result_status"]) for row in attempts}
    if "success" in statuses:
        return "success"
    if "failed" in statuses:
        return "failed"
    return "inconclusive"


def _group_failures(
    provider_ids: list[str],
    failures: list[_FailureFact],
) -> dict[str, list[_FailureFact]]:
    """按冻结四键稳定排序每个 Provider 最近 5 条故障。"""

    grouped = {provider_id: [] for provider_id in provider_ids}
    for failure in failures:
        # 只接收当前租户分页 Provider；防御性忽略不属于本页的事实，避免形成跨页数据旁路。
        if failure.provider_id in grouped:
            grouped[failure.provider_id].append(failure)
    for provider_id, values in grouped.items():
        # 两次稳定排序保留精确 datetime 比较，避免 float timestamp 在微秒相邻事实上丢失顺序。
        values.sort(key=lambda value: (value.source, value.request_id, value.stable_id))
        values.sort(key=lambda value: value.observed_at, reverse=True)
        grouped[provider_id] = values[:5]
    return grouped


def _status_item(
    provider: _ProviderBase,
    calls: _CallMetrics,
    probes: _ProbeMetrics,
    failures: list[_FailureFact],
) -> ProviderStatusItem:
    """合并一个 Provider 的元数据、调用、探测和故障投影。"""

    row = provider.row
    failure_dtos = [failure.to_dto() for failure in failures]
    return ProviderStatusItem(
        provider_id=row["provider_id"],
        tenant_id=row["tenant_id"],
        provider_code=row["provider_code"],
        name=row["name"],
        provider_type=row["provider_type"],
        base_url=provider.safe_base_url,
        auth_type=row["auth_type"],
        provider_status=row["status"],
        health_status=row["health_status"],
        effective_health_status=provider.effective_health_status,
        availability_status=AVAILABILITY_BY_HEALTH[provider.effective_health_status],
        is_health_stale=provider.is_health_stale,
        maintenance_note=row["maintenance_note"],
        terminal_call_count=calls.terminal,
        eligible_call_count=calls.eligible,
        success_count=calls.success,
        provider_failure_count=calls.provider_failure,
        credential_failure_count=calls.credential_failure,
        excluded_call_count=calls.excluded,
        success_rate_bps=_rate_bps(calls.success, calls.eligible),
        probe_total=probes.total,
        probe_success_count=probes.success,
        probe_failure_count=probes.failure,
        probe_inconclusive_count=probes.inconclusive,
        probe_success_rate_bps=_rate_bps(probes.success, probes.success + probes.failure),
        call_latency_sample_count=calls.call_latency_sample_count,
        call_latency_p95_ms=calls.call_latency_p95_ms,
        stream_first_chunk_sample_count=calls.stream_first_chunk_sample_count,
        stream_first_chunk_p95_ms=calls.stream_first_chunk_p95_ms,
        probe_latency_sample_count=len(probes.latencies),
        probe_latency_p95_ms=_nearest_rank_p95(probes.latencies),
        latest_call_at=calls.latest_call_at,
        latest_probe_at=probes.latest_probe_at,
        last_tested_at=_parse_optional_time(row["last_tested_at"]),
        last_probe_latency_ms=_safe_optional_nonnegative_integer(row["last_probe_latency_ms"]),
        last_error=failure_dtos[0] if failure_dtos else None,
        recent_failures=failure_dtos,
        provider_created_at=_parse_time(row["created_at"]),
        provider_updated_at=_parse_time(row["updated_at"]),
    )


def _status_summary(providers: list[_ProviderBase]) -> ProviderStatusSummary:
    """生成筛选后、分页前互斥状态桶；总数必须等于五个桶之和。"""

    counts = {status: 0 for status in EFFECTIVE_STATUSES}
    for provider in providers:
        counts[provider.effective_health_status] += 1
    return ProviderStatusSummary(
        total=len(providers),
        healthy=counts["healthy"],
        unhealthy=counts["unhealthy"],
        observing=counts["observing"],
        unknown=counts["unknown"],
        disabled=counts["disabled"],
    )


def _elapsed_ms(started_at: datetime, ended_at: datetime) -> int:
    """以整数微秒差向下截断毫秒，并拒绝负时间差。"""

    delta = ended_at - started_at
    microseconds = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    # 负时长代表终态时间顺序损坏，不能通过绝对值或归零伪造延迟样本。
    if microseconds < 0:
        raise _DataContractError()
    return _safe_nonnegative_integer(microseconds // 1000)


def _nearest_rank_p95(values: list[int]) -> int | None:
    """计算非负整数样本的 nearest-rank P95；空样本返回 None。"""

    if not values:
        return None
    for value in values:
        _safe_nonnegative_integer(value)
    ordered = sorted(values)
    rank = (95 * len(ordered) + 99) // 100
    return ordered[rank - 1]


def _rate_bps(success: int, denominator: int) -> int | None:
    """用有界整数 half-up 计算万分比，避免二进制浮点舍入漂移。"""

    _safe_nonnegative_integer(success)
    _safe_nonnegative_integer(denominator)
    if denominator == 0:
        return None
    # 分子大于分母说明资格计数合同已经损坏，不能返回超过 100% 的误导指标。
    if success > denominator:
        raise _DataContractError()
    return (success * 10_000 + denominator // 2) // denominator


def _normalize_provider_id(value: str | None) -> str | None:
    """校验可选 Provider ID；非法值返回统一筛选错误而非合法空结果。"""

    if value is None:
        return None
    # 内部调用也只能传字符串，拒绝依赖隐式 str() 把对象变成可枚举标识。
    if not isinstance(value, str):
        raise ApiError("provider_status.filter_invalid")
    normalized = value.strip()
    # 资源标识保留大小写，但必须是短且无控制字符的单值，避免日志与查询解释差异。
    if not 1 <= len(normalized) <= 128 or _has_control_character(normalized):
        raise ApiError("provider_status.filter_invalid")
    return normalized


def _normalize_filter(value: str | None, allowed: set[str]) -> str | None:
    """归一化固定枚举筛选，未知值不能被解释为合法空集合。"""

    if value is None:
        return None
    # 固定枚举不接受数字或对象，避免宽松字符串化制造与真实 Provider 类型相同的值。
    if not isinstance(value, str):
        raise ApiError("provider_status.filter_invalid")
    normalized = value.strip().lower()
    # 未知枚举应返回统一 422，不能伪装成合法但无数据的租户内查询。
    if normalized not in allowed:
        raise ApiError("provider_status.filter_invalid")
    return normalized


def _safe_base_url(value: str) -> str:
    """移除 Endpoint 的 userinfo、query 和 fragment，只保留可展示网络位置。"""

    parsed = urlsplit(value)
    # 非 HTTP(S) 或无主机 Endpoint 无法安全展示，且可能把本地路径误当网络地址泄露。
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _DataContractError()
    hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))


def _valid_maintenance_note(value: object) -> bool:
    """复核数据库维护备注仍满足 API 写入合同。"""

    return (
        isinstance(value, str)
        and value == value.strip()
        and 1 <= len(value) <= 240
        and not _has_control_character(value)
    )


def _has_control_character(value: str) -> bool:
    """识别 C0/C1 控制字符，换行与制表也不能进入状态页维护备注或标识。"""

    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _safe_error_code(value: object, allowed: frozenset[str]) -> str:
    """只允许指定来源的固定枚举错误码进入故障 DTO。

    参数：数据库值及该事实来源的白名单。返回：已验证错误码。
    关键约束：仅校验字符格式不足以排除历史 raw message，因此未知但形似错误码的文本也必须失败关闭。
    """

    if (
        not isinstance(value, str)
        or not SAFE_ERROR_CODE.fullmatch(value)
        or value not in allowed
    ):
        # 未知码可能实际是历史异常正文；绝不能仅因字符形状相似就把它送入响应或 DOM。
        raise _DataContractError()
    return value


def _safe_nonnegative_integer(value: object) -> int:
    """校验会进入 JSON 的非负 JavaScript 安全整数并返回原值。"""

    # bool 虽是 int 子类但不是计数；超出安全整数也会在浏览器端静默丢失精度。
    if type(value) is not int or value < 0 or value > MAX_SAFE_INTEGER:
        raise _DataContractError()
    return value


def _safe_optional_nonnegative_integer(value: object) -> int | None:
    """校验可空的非负 JavaScript 安全整数，空值保持为 ``None``。"""

    return None if value is None else _safe_nonnegative_integer(value)


def _safe_identifier(value: object) -> str:
    """验证追踪或内部排序标识为短、无控制字符文本。"""

    # request/trace/stable ID 会进入排序或排障视图，控制字符和超长文本都可能污染页面与日志。
    if not isinstance(value, str) or not 1 <= len(value) <= 240 or _has_control_character(value):
        raise _DataContractError()
    return value


def _parse_time(value: object) -> datetime:
    """解析运行时 UTC 时间，并兼容早期 SQLite ``datetime('now')`` 存量格式。

    新事实必须携带时区；历史种子和早期迁移生成的 ``YYYY-MM-DD HH:MM:SS`` 明确定义为 UTC，
    仅对这一种无歧义格式补充 UTC。其他无时区或不可解析文本仍按数据合同损坏处理。
    """

    # 时间不是字符串时拒绝隐式转换，避免本地时区或数值 epoch 被当成 UTC 历史事实。
    if not isinstance(value, str) or not value:
        raise _DataContractError()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # 只兼容早期 SQLite 固定 UTC 格式；其他无时区文本不能由服务器本地时区猜测。
        if not LEGACY_UTC_TIME.fullmatch(value):
            raise _DataContractError()
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_optional_time(value: object) -> datetime | None:
    """解析可空 UTC 时间；非空坏值仍失败关闭。"""

    return None if value is None else _parse_time(value)


def _db_time(value: datetime) -> str:
    """把窗口边界编码为可按字符串比较的 UTC 毫秒 ISO 格式。"""

    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _epoch_microseconds(value: datetime | object) -> int:
    """把 UTC 时间转换为精确整数微秒，供 SQLite 窗口边界和稳定排序使用。"""

    parsed = value if isinstance(value, datetime) else _parse_time(value)
    # 所有窗口边界必须带时区；naive datetime 会让租户指标随部署时区漂移。
    if parsed.tzinfo is None:
        raise _DataContractError()
    utc_value = parsed.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc_value - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _time_from_epoch_microseconds(value: int) -> datetime:
    """把 SQLite 返回的整数微秒恢复为带 UTC 时区时间。"""

    # SQLite 聚合必须返回精确整数微秒，浮点会破坏相邻事实的稳定排序。
    if type(value) is not int:
        raise _DataContractError()
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)


def _sqlite_elapsed_ms(started_at: object, ended_at: object) -> int:
    """供 SQLite 调用的精确毫秒差函数；坏时间会终止整条状态查询。"""

    return _elapsed_ms(_parse_time(started_at), _parse_time(ended_at))


def _sqlite_safe_error_code(value: object) -> str:
    """供 SQLite 故障 UNION 使用的错误码守卫，阻止正文伪装成错误码。"""

    return _safe_error_code(value, CALL_ERROR_CODES)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """打开状态查询连接，并把基础设施错误收敛到领域统一 503。"""

    try:
        connection = connect_database(config.database_url)
        connection.execute("PRAGMA busy_timeout=5000")
        # 自定义函数只做纯整数时间运算与固定码校验，避免 julianday 浮点误差和正文泄露。
        connection.create_function("metahub_epoch_us", 1, _epoch_microseconds, deterministic=True)
        connection.create_function("metahub_elapsed_ms", 2, _sqlite_elapsed_ms, deterministic=True)
        connection.create_function(
            "metahub_safe_error_code",
            1,
            _sqlite_safe_error_code,
            deterministic=True,
        )
        return connection
    # 连接配置或打开失败统一映射成状态领域 503，绝不透传数据库文件路径。
    except DbError:
        raise ApiError("provider_status.query_unavailable") from None


def _safe_rollback(connection: sqlite3.Connection) -> None:
    """尽力结束只读事务；回滚失败不能覆盖原始统一业务错误。"""

    try:
        connection.rollback()
    # 清理失败不能覆盖最初的数据合同或领域错误，连接仍会在 finally 中关闭。
    except sqlite3.Error:
        pass


def _utcnow() -> datetime:
    """集中生成一次带时区 UTC 当前时刻，便于专项测试替换。"""

    return datetime.now(UTC)
