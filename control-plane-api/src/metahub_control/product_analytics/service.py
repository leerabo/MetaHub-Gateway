"""当前租户七阶段产品激活漏斗的权威聚合。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext

from .models import (
    ProductAnalyticsFunnelResponse,
    ProductAnalyticsStage,
    ProductAnalyticsSummary,
    ProductAnalyticsWindow,
)

# 七阶段顺序是公开漏斗合同，数据库聚合结果必须逐项映射且不能动态扩展。
STAGE_TYPES = (
    "tenant.registered",
    "provider.created",
    "virtual_key.created",
    "gateway.first_call_succeeded",
    "budget.configured",
    "teammate.invited",
    "request_logs.viewed",
)


def get_product_analytics_funnel(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> ProductAnalyticsFunnelResponse:
    """只按认证 tenant 聚合全部历史事实，不接受客户端身份或窗口。"""

    tenant_id = context.user.tenant_id
    try:
        connection = connect_database(config.database_url)
        try:
            rows = connection.execute(
                """
                SELECT aggregate.event_type,aggregate.event_count,
                       (SELECT candidate.occurred_at
                        FROM product_events candidate
                        WHERE candidate.tenant_id=?
                          AND candidate.event_type=aggregate.event_type
                        ORDER BY julianday(candidate.occurred_at),candidate.occurred_at,
                                 candidate.product_event_id
                        LIMIT 1) AS first_occurred_at,
                       (SELECT candidate.occurred_at
                        FROM product_events candidate
                        WHERE candidate.tenant_id=?
                          AND candidate.event_type=aggregate.event_type
                        ORDER BY julianday(candidate.occurred_at) DESC,candidate.occurred_at DESC,
                                 candidate.product_event_id DESC
                        LIMIT 1) AS last_occurred_at
                FROM(
                    SELECT event_type,COUNT(*) AS event_count
                    FROM product_events
                    WHERE tenant_id=?
                    GROUP BY event_type
                ) aggregate
                """,
                (tenant_id, tenant_id, tenant_id),
            ).fetchall()
        finally:
            connection.close()
    # 数据库异常统一返回稳定领域错误，避免把 SQL、路径或租户数据带入响应。
    except (DbError, sqlite3.Error) as exc:
        raise ApiError("product_analytics.unavailable") from exc

    by_type: dict[str, sqlite3.Row] = {}
    for row in rows:
        event_type = row["event_type"]
        # 未登记阶段意味着权威事实已漂移，不能把未知类型宽松加入公开漏斗。
        if type(event_type) is not str or event_type not in STAGE_TYPES:
            raise ApiError("product_analytics.unavailable")
        by_type[event_type] = row

    stages: list[ProductAnalyticsStage] = []
    prefix_reached = True
    completed = 0
    cumulative = 0
    for index, event_type in enumerate(STAGE_TYPES):
        row = by_type.get(event_type)
        count = _count(row["event_count"] if row is not None else 0)
        reached = count > 0
        cumulative_reached = prefix_reached and reached
        previous_cumulative = 1 if index == 0 else stages[-1].cumulative_tenant_count
        conversion = None if previous_cumulative == 0 else (10000 if cumulative_reached else 0)
        first_value = _optional_utc(row["first_occurred_at"] if row is not None else None)
        last_value = _optional_utc(row["last_occurred_at"] if row is not None else None)
        # 计数和首末时间必须来自同一租户聚合事实，任一缺失都视为数据库不一致。
        if (count == 0) != (first_value is None and last_value is None):
            raise ApiError("product_analytics.unavailable")
        # 首次时间晚于末次时间说明排序或存储事实损坏，禁止返回自相矛盾的指标。
        if first_value is not None and last_value is not None and first_value[0] > last_value[0]:
            raise ApiError("product_analytics.unavailable")
        first = first_value[1] if first_value is not None else None
        last = last_value[1] if last_value is not None else None
        stages.append(
            ProductAnalyticsStage(
                order=index + 1,
                event_type=event_type,
                event_count=count,
                reached=reached,
                cumulative_reached=cumulative_reached,
                reached_tenant_count=1 if reached else 0,
                cumulative_tenant_count=1 if cumulative_reached else 0,
                conversion_from_previous_bps=conversion,
                first_occurred_at=first,
                last_occurred_at=last,
            )
        )
        completed += int(reached)
        if cumulative_reached:
            cumulative += 1
        prefix_reached = cumulative_reached

    return ProductAnalyticsFunnelResponse(
        tenant_id=tenant_id,
        window=ProductAnalyticsWindow(),
        stages=stages,
        summary=ProductAnalyticsSummary(
            completed_stage_count=completed,
            cumulative_stage_count=cumulative,
            completion_bps=(completed * 10000 + 3) // 7,
            activated=completed == 7,
        ),
        generated_at=_db_time(datetime.now(UTC)),
        request_id=request_id,
        trace_id=trace_id,
    )


def _count(value: object) -> int:
    """校验 SQLite 聚合计数是非负有符号六十四位整数，拒绝布尔和越界结果。"""

    # 异常计数会污染完成率和激活判断，因此不能截断、猜测或回退为零。
    if type(value) is not int or not 0 <= value <= 9_223_372_036_854_775_807:
        raise ApiError("product_analytics.unavailable")
    return value


def _optional_utc(value: object) -> tuple[datetime, str] | None:
    """解析可空数据库时间为 UTC 排序值与规范公开文本，并兼容旧无后缀秒值。"""

    if value is None:
        return None
    # 非文本时间无法证明来自迁移冻结格式，必须作为权威数据损坏失败关闭。
    if type(value) is not str:
        raise ApiError("product_analytics.unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 解析失败时只公开领域错误码，不回传包含原数据库值的异常详情。
    except ValueError:
        raise ApiError("product_analytics.unavailable") from None
    # 既有 seed 使用 SQLite ``datetime('now')`` 的无后缀 UTC 秒；公开 wire 统一转成 Z。
    if parsed.tzinfo is None:
        if len(value) != 19 or value[10] not in {" ", "T"}:
            raise ApiError("product_analytics.unavailable")
        parsed = parsed.replace(tzinfo=UTC)
    normalized = parsed.astimezone(UTC)
    return normalized, _wire_time(normalized)


def _wire_time(value: datetime) -> str:
    """把时间转换为保留必要微秒的 UTC Z 文本，保证首末事件精度可复核。"""

    current = value.astimezone(UTC)
    timespec = "microseconds" if current.microsecond else "seconds"
    return current.isoformat(timespec=timespec).replace("+00:00", "Z")


def _db_time(value: datetime) -> str:
    """把生成时间规范为 UTC 整秒文本，避免不同运行环境产生时区漂移。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["STAGE_TYPES", "get_product_analytics_funnel"]
