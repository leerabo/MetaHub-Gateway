"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/dashboard/service.py
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from metahub_control.auth import AuthContext
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ApiError

from .models import (
    DashboardEmptyState,
    DashboardMetric,
    DashboardOverviewResponse,
    DashboardRecentError,
    DashboardRecommendedAction,
    DashboardTopModel,
    DashboardTopVirtualKey,
    DashboardWindow,
)

# Dashboard 统计窗口、排行数量和状态阈值集中定义，保证控制台展示与后端聚合口径一致。
SHANGHAI = ZoneInfo("Asia/Shanghai")
TOP_LIMIT = 5
SUCCESS_WARNING_THRESHOLD = 95.0
SUCCESS_CRITICAL_THRESHOLD = 90.0
ERROR_WARNING_THRESHOLD = 5.0
ERROR_CRITICAL_THRESHOLD = 10.0
LATENCY_WARNING_MS = 1000
LATENCY_CRITICAL_MS = 3000


def get_dashboard_overview(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> DashboardOverviewResponse:
    """读取当前租户的今日 Dashboard 聚合数据。

    tenant_id 只来自 AuthContext；Dashboard 不接受任何租户查询参数，避免通过总览接口跨租户读取用量、成本或错误趋势。
    """

    generated_at = _utcnow()
    window = _business_day_window(generated_at)
    start_db = _to_db_time(window.start_utc)
    end_db = _to_db_time(window.end_utc)
    try:
        connection = _connect(config)
        totals = _load_request_totals(connection, context.user.tenant_id, start_db, end_db)
        p95_latency_ms = _load_p95_latency(connection, context.user.tenant_id, start_db, end_db)
        cost_totals = _load_usage_totals(connection, context.user.tenant_id, start_db, end_db)
        top_models = _load_top_models(connection, context.user.tenant_id, start_db, end_db)
        top_virtual_keys = _load_top_virtual_keys(connection, context.user.tenant_id, start_db, end_db)
        recent_errors = _load_recent_errors(connection, context.user.tenant_id, start_db, end_db)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except (sqlite3.Error, DbError) as exc:
        # Dashboard 是控制台高频刷新入口，数据库异常必须收敛为统一错误码和 request_id，不能透出 SQL 或路径。
        raise ApiError("dashboard.unavailable") from exc

    total_requests = _int_value(totals["total_requests"])
    success_count = _int_value(totals["success_count"])
    failed_count = _int_value(totals["failed_count"])
    success_rate = _percentage(success_count, total_requests)
    error_rate = _percentage(failed_count, total_requests)
    empty_state = _empty_state(total_requests)
    return DashboardOverviewResponse(
        window=DashboardWindow(
            timezone="Asia/Shanghai",
            start_at=window.start_local,
            end_at=window.end_local,
            generated_at=generated_at,
        ),
        metrics=_build_metrics(
            total_requests=total_requests,
            success_rate=success_rate,
            error_rate=error_rate,
            p95_latency_ms=p95_latency_ms,
            total_tokens=_int_value(cost_totals["total_tokens"]),
            today_cost_cny=_float_value(cost_totals["cost_cny"]),
        ),
        top_models=top_models,
        top_virtual_keys=top_virtual_keys,
        recent_errors=recent_errors,
        empty_state=empty_state,
        request_id=request_id,
        trace_id=trace_id,
    )


class _DashboardWindow:
    """内部统计窗口对象。

    同时保存本地业务日边界和 UTC 数据库边界，避免每个查询重复处理时区转换。
    """

    def __init__(self, start_local: datetime, end_local: datetime) -> None:
        """初始化 Dashboard 统计窗口并派生 UTC 查询边界。"""

        self.start_local = start_local
        self.end_local = end_local
        self.start_utc = start_local.astimezone(UTC)
        self.end_utc = end_local.astimezone(UTC)


def _business_day_window(now: datetime) -> _DashboardWindow:
    """按 Asia/Shanghai 计算今日窗口。

    数据库统一保存 UTC 字符串，但企业控制台的“今日”按中国境内业务日理解，因此这里固定用 Asia/Shanghai。
    """

    local_now = now.astimezone(SHANGHAI)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=SHANGHAI)
    end_local = start_local + timedelta(days=1)
    return _DashboardWindow(start_local, end_local)


def _load_request_totals(
    connection: sqlite3.Connection,
    tenant_id: str,
    start_db: str,
    end_db: str,
) -> sqlite3.Row:
    """聚合今日请求总数、成功数和失败数。

    SQL 第一条件始终是 tenant_id，并限定今日窗口，避免 Dashboard 刷新造成跨租户或全表扫描。
    """

    return connection.execute(
        """
        SELECT
          COUNT(*) AS total_requests,
          COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), 0) AS success_count,
          COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0) AS failed_count
        FROM request_logs
        WHERE tenant_id=? AND created_at >= ? AND created_at < ?
        """,
        (tenant_id, start_db, end_db),
    ).fetchone()


def _load_p95_latency(
    connection: sqlite3.Connection,
    tenant_id: str,
    start_db: str,
    end_db: str,
) -> int:
    """计算今日 P95 延迟，缺失延迟时返回 0。

    使用 COUNT + ORDER BY + OFFSET 在数据库内定位分位点，避免把所有延迟值读到 Python 内存。
    """

    count_row = connection.execute(
        """
        SELECT COUNT(*) AS latency_count
        FROM request_logs
        WHERE tenant_id=? AND created_at >= ? AND created_at < ? AND latency_ms IS NOT NULL
        """,
        (tenant_id, start_db, end_db),
    ).fetchone()
    latency_count = _int_value(count_row["latency_count"] if count_row else 0)
    if latency_count <= 0:
        return 0
    offset = max(0, math.ceil(latency_count * 0.95) - 1)
    row = connection.execute(
        """
        SELECT latency_ms
        FROM request_logs
        WHERE tenant_id=? AND created_at >= ? AND created_at < ? AND latency_ms IS NOT NULL
        ORDER BY latency_ms ASC
        LIMIT 1 OFFSET ?
        """,
        (tenant_id, start_db, end_db, offset),
    ).fetchone()
    return _int_value(row["latency_ms"] if row else 0)


def _load_usage_totals(
    connection: sqlite3.Connection,
    tenant_id: str,
    start_db: str,
    end_db: str,
) -> sqlite3.Row:
    """聚合今日 Token 和人民币成本。

    成本以 usage_ledger.cost_cny 为准；空账本返回 0，避免空租户 Dashboard 报错。
    """

    return connection.execute(
        """
        SELECT
          COALESCE(SUM(total_tokens), 0) AS total_tokens,
          COALESCE(SUM(cost_cny), 0) AS cost_cny
        FROM usage_ledger
        WHERE tenant_id=? AND created_at >= ? AND created_at < ?
        """,
        (tenant_id, start_db, end_db),
    ).fetchone()


def _load_top_models(
    connection: sqlite3.Connection,
    tenant_id: str,
    start_db: str,
    end_db: str,
) -> list[DashboardTopModel]:
    """读取今日 Top 模型。

    请求量来自 request_logs，Token 和成本来自按 request_id 聚合后的 usage_ledger，避免多条账本记录把请求数放大。
    """

    rows = connection.execute(
        """
        WITH usage_by_request AS (
          SELECT
            tenant_id, request_id,
            MAX(model_id) AS model_id,
            MAX(resolved_model) AS resolved_model,
            MAX(provider_code) AS provider_code,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(cost_cny), 0) AS cost_cny
          FROM usage_ledger
          WHERE tenant_id=? AND created_at >= ? AND created_at < ?
          GROUP BY tenant_id, request_id
        )
        SELECT
          COALESCE(rl.model_id, ub.model_id) AS model_id,
          COALESCE(rl.resolved_model, ub.resolved_model, rl.request_model, 'unknown') AS resolved_model,
          COALESCE(rl.provider_code, ub.provider_code) AS provider_code,
          COUNT(DISTINCT rl.request_id) AS request_count,
          COUNT(DISTINCT CASE WHEN rl.status='success' THEN rl.request_id END) AS success_count,
          COALESCE(SUM(COALESCE(ub.total_tokens, rl.total_tokens, 0)), 0) AS total_tokens,
          COALESCE(SUM(COALESCE(ub.cost_cny, CASE WHEN rl.currency='CNY' THEN rl.estimated_cost_amount ELSE 0 END, 0)), 0) AS cost_cny
        FROM request_logs rl
        LEFT JOIN usage_by_request ub ON ub.tenant_id=rl.tenant_id AND ub.request_id=rl.request_id
        WHERE rl.tenant_id=? AND rl.created_at >= ? AND rl.created_at < ?
        GROUP BY COALESCE(rl.model_id, ub.model_id), COALESCE(rl.resolved_model, ub.resolved_model, rl.request_model, 'unknown'), COALESCE(rl.provider_code, ub.provider_code)
        ORDER BY request_count DESC, cost_cny DESC, total_tokens DESC
        LIMIT ?
        """,
        (tenant_id, start_db, end_db, tenant_id, start_db, end_db, TOP_LIMIT),
    ).fetchall()
    return [
        DashboardTopModel(
            model_id=row["model_id"],
            resolved_model=row["resolved_model"],
            provider_code=row["provider_code"],
            request_count=_int_value(row["request_count"]),
            success_count=_int_value(row["success_count"]),
            total_tokens=_int_value(row["total_tokens"]),
            cost_cny=_float_value(row["cost_cny"]),
        )
        for row in rows
    ]


def _load_top_virtual_keys(
    connection: sqlite3.Connection,
    tenant_id: str,
    start_db: str,
    end_db: str,
) -> list[DashboardTopVirtualKey]:
    """读取今日 Top Virtual Key。

    只返回 Key 名称和 key_prefix，明文 Key、哈希和密文不会进入 Dashboard DTO。
    """

    rows = connection.execute(
        """
        WITH usage_by_request AS (
          SELECT
            tenant_id, request_id,
            MAX(virtual_key_id) AS virtual_key_id,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(cost_cny), 0) AS cost_cny
          FROM usage_ledger
          WHERE tenant_id=? AND created_at >= ? AND created_at < ?
          GROUP BY tenant_id, request_id
        )
        SELECT
          COALESCE(rl.virtual_key_id, ub.virtual_key_id) AS virtual_key_id,
          vk.name AS virtual_key_name,
          vk.key_prefix AS virtual_key_prefix,
          COUNT(DISTINCT rl.request_id) AS request_count,
          COUNT(DISTINCT CASE WHEN rl.status='success' THEN rl.request_id END) AS success_count,
          COALESCE(SUM(COALESCE(ub.total_tokens, rl.total_tokens, 0)), 0) AS total_tokens,
          COALESCE(SUM(COALESCE(ub.cost_cny, CASE WHEN rl.currency='CNY' THEN rl.estimated_cost_amount ELSE 0 END, 0)), 0) AS cost_cny
        FROM request_logs rl
        LEFT JOIN usage_by_request ub ON ub.tenant_id=rl.tenant_id AND ub.request_id=rl.request_id
        LEFT JOIN virtual_keys vk ON vk.tenant_id=rl.tenant_id AND vk.virtual_key_id=COALESCE(rl.virtual_key_id, ub.virtual_key_id)
        WHERE rl.tenant_id=? AND rl.created_at >= ? AND rl.created_at < ?
        GROUP BY COALESCE(rl.virtual_key_id, ub.virtual_key_id), vk.name, vk.key_prefix
        ORDER BY request_count DESC, cost_cny DESC, total_tokens DESC
        LIMIT ?
        """,
        (tenant_id, start_db, end_db, tenant_id, start_db, end_db, TOP_LIMIT),
    ).fetchall()
    return [
        DashboardTopVirtualKey(
            virtual_key_id=row["virtual_key_id"],
            virtual_key_name=row["virtual_key_name"],
            virtual_key_prefix=row["virtual_key_prefix"],
            request_count=_int_value(row["request_count"]),
            success_count=_int_value(row["success_count"]),
            total_tokens=_int_value(row["total_tokens"]),
            cost_cny=_float_value(row["cost_cny"]),
        )
        for row in rows
    ]


def _load_recent_errors(
    connection: sqlite3.Connection,
    tenant_id: str,
    start_db: str,
    end_db: str,
) -> list[DashboardRecentError]:
    """读取今日最近失败请求。

    错误摘要只取安全元数据，并通过统一错误诊断规则生成中文标题，不读取原始 Prompt 或上游响应正文。
    """

    rows = connection.execute(
        """
        SELECT request_id, trace_id, error_code, request_model, resolved_model, provider_code, latency_ms, created_at
        FROM request_logs
        WHERE tenant_id=? AND created_at >= ? AND created_at < ? AND status='failed'
        ORDER BY created_at DESC, request_id DESC
        LIMIT ?
        """,
        (tenant_id, start_db, end_db, TOP_LIMIT),
    ).fetchall()
    return [_recent_error(row) for row in rows]


def _build_metrics(
    *,
    total_requests: int,
    success_rate: float,
    error_rate: float,
    p95_latency_ms: int,
    total_tokens: int,
    today_cost_cny: float,
) -> list[DashboardMetric]:
    """把聚合结果转换为固定 KPI 列表。"""

    empty = total_requests <= 0
    return [
        DashboardMetric(
            id="today_requests",
            label="今日请求",
            value=float(total_requests),
            unit="count",
            status="empty" if empty else "ok",
            description="来自当前租户 request_logs 的今日调用数。",
        ),
        DashboardMetric(
            id="success_rate",
            label="成功率",
            value=success_rate,
            unit="percent",
            status="empty" if empty else _success_status(success_rate),
            description="今日成功请求数 / 今日总请求数。",
        ),
        DashboardMetric(
            id="p95_latency_ms",
            label="P95 延迟",
            value=float(p95_latency_ms),
            unit="ms",
            status="empty" if empty else _latency_status(p95_latency_ms),
            description="今日非空 latency_ms 的 P95 分位点。",
        ),
        DashboardMetric(
            id="today_cost_cny",
            label="今日成本",
            value=round(today_cost_cny, 6),
            unit="cny",
            status="empty" if empty else "ok",
            description="来自 usage_ledger.cost_cny 的人民币成本。",
        ),
        DashboardMetric(
            id="total_tokens",
            label="总 Token",
            value=float(total_tokens),
            unit="tokens",
            status="empty" if empty else "ok",
            description="来自 usage_ledger.total_tokens 的今日 Token 总量。",
        ),
        DashboardMetric(
            id="error_rate",
            label="错误率",
            value=error_rate,
            unit="percent",
            status="empty" if empty else _error_status(error_rate),
            description="今日失败请求数 / 今日总请求数。",
        ),
    ]


def _recent_error(row: sqlite3.Row) -> DashboardRecentError:
    """把 request_logs 失败行转换为 Dashboard 安全错误摘要。"""

    error_code = row["error_code"]
    diagnosis = diagnose_error(error_code or "infra.unhandled")
    return DashboardRecentError(
        request_id=row["request_id"],
        trace_id=row["trace_id"],
        error_code=error_code,
        diagnosis_title=diagnosis.reason,
        model=row["resolved_model"] or row["request_model"],
        provider_code=row["provider_code"],
        latency_ms=_optional_int(row["latency_ms"]),
        created_at=_parse_db_time(row["created_at"]),
    )


def _empty_state(total_requests: int) -> DashboardEmptyState:
    """生成空数据引导，帮助新租户完成第一次调用。"""

    if total_requests > 0:
        return DashboardEmptyState(is_empty=False, title="", description="", recommended_actions=[])
    return DashboardEmptyState(
        is_empty=True,
        title="今天还没有调用数据",
        description="完成 Provider、模型目录和 Virtual Key 配置后，用调试台发起一次调用即可看到指标。",
        recommended_actions=[
            DashboardRecommendedAction(label="注册 Provider", target="providers", description="接入模型厂商或本地 OpenAI-compatible Endpoint。"),
            DashboardRecommendedAction(label="配置模型目录", target="models", description="创建内部模型 ID 并绑定上游模型。"),
            DashboardRecommendedAction(label="创建 Virtual Key", target="virtual-keys", description="生成开发环境调用 Key，明文只返回一次。"),
            DashboardRecommendedAction(label="进入调试台", target="debug-console", description="用受治理链路完成第一次 Chat Completions 调用。"),
        ],
    )


def _success_status(value: float) -> str:
    """按成功率返回 Dashboard 状态。

    成功率阈值集中在服务端，前端不能自行改写颜色或风险等级，否则同一租户在不同入口看到的治理口径会不一致。
    """

    if value >= SUCCESS_WARNING_THRESHOLD:
        return "ok"
    if value >= SUCCESS_CRITICAL_THRESHOLD:
        return "warning"
    return "critical"


def _error_status(value: float) -> str:
    """按错误率返回 Dashboard 状态。

    错误率直接影响总览告警感知，因此这里只使用后端统一阈值，不根据前端页面或用户角色做差异化判断。
    """

    # 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
    if value <= ERROR_WARNING_THRESHOLD:
        return "ok"
    # 错误率进入关注区间时先标记 warning，超过严重阈值才升级为 critical，避免总览页过度告警。
    if value <= ERROR_CRITICAL_THRESHOLD:
        return "warning"
    return "critical"


def _latency_status(value: int) -> str:
    """按 P95 延迟返回 Dashboard 状态。

    延迟状态用于快速发现上游模型或网络异常，阈值必须与真实 request_logs 聚合值保持一致。
    """

    if value < LATENCY_WARNING_MS:
        return "ok"
    if value < LATENCY_CRITICAL_MS:
        return "warning"
    return "critical"


def _percentage(part: int, total: int) -> float:
    """计算 Dashboard 百分比。

    空分母表示当前租户今日没有调用，返回 0 而不是抛异常，保证空状态可以正常渲染接入引导。
    """

    if total <= 0:
        return 0.0
    return round(part * 100.0 / total, 2)


def _int_value(value: object | None) -> int:
    """把 SQLite 数值安全转换为非负整数。"""

    try:
        return max(0, int(value or 0))
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (TypeError, ValueError):
        return 0


def _optional_int(value: object | None) -> int | None:
    """把可空 SQLite 数值转换为 int 或 None。

    最近错误里的 latency_ms 允许为空，必须保留 None 语义，让前端展示缺失值而不是误写成 0ms。
    """

    if value is None:
        return None
    return _int_value(value)


def _float_value(value: object | None) -> float:
    """把 SQLite 数值安全转换为非负浮点数。"""

    try:
        return max(0.0, float(value or 0))
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (TypeError, ValueError):
        return 0.0


def _parse_db_time(value: object) -> datetime:
    """解析数据库 UTC ISO 时间，并统一输出 UTC datetime。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 datetime 转成数据库使用的 UTC 秒级 ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回 UTC 当前时间，集中封装便于测试中构造同口径夹具。"""

    return datetime.now(UTC).replace(microsecond=0)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面数据库，连接失败交给调用方映射为 Dashboard 统一错误。"""

    return connect_database(config.database_url)