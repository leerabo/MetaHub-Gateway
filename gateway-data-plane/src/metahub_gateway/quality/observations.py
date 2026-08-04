"""真实上游终态到 ModelQualityScore 的单样本原子增量。

本模块只接收调用方已经判定为真实 Provider 结果的布尔结论和整数延迟，不读取或保存
Prompt、Response、Endpoint、Provider Key、Token、错误正文。调用方必须复用当前
attempt/session SQLite 事务；本函数不 commit，也不吞掉一致性错误。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

# SQLite 累计列使用 signed 64-bit；所有增量必须在写入前证明不会溢出或饱和。
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807


def record_model_quality_observation(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    model_id: str,
    succeeded: bool,
    latency_ms: int,
    observed_at: datetime,
) -> None:
    """在现有终态事务内为当前租户模型严格增加一个质量样本。

    参数：数据库连接由 attempt/session 调用方持有；tenant/model 来自冻结路由；
    succeeded 只表示真实上游结果；latency 为非负整数；observed_at 必须有时区。
    返回：无。约束：恰好更新一条 Score，否则抛 SQLite 一致性错误并由调用方回滚终态。
    """

    if (
        not isinstance(connection, sqlite3.Connection)
        or type(tenant_id) is not str
        or not tenant_id
        or type(model_id) is not str
        or not model_id
        or type(succeeded) is not bool
        or type(latency_ms) is not int
        or latency_ms < 0
        or latency_ms > MAX_SQLITE_INTEGER
        or not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
    ):
        # 内部调用图损坏不能通过裁剪/类型转换后继续累计，否则统计将无法从源事实复现。
        raise sqlite3.IntegrityError("model_quality_observation_invalid")
    observed_text = _to_db_time(observed_at)
    cursor = connection.execute(
        """
        UPDATE model_quality_scores
        SET sample_count=sample_count+1,
            success_count=success_count+?,
            error_count=error_count+?,
            latency_sample_count=latency_sample_count+1,
            total_latency_ms=total_latency_ms+?,
            statistics_version=statistics_version+1,
            first_observed_at=CASE
              WHEN first_observed_at IS NULL OR ?<first_observed_at THEN ?
              ELSE first_observed_at
            END,
            last_observed_at=CASE
              WHEN last_observed_at IS NULL OR ?>last_observed_at THEN ?
              ELSE last_observed_at
            END,
            updated_at=CASE WHEN ?>updated_at THEN ? ELSE updated_at END
        WHERE tenant_id=? AND model_id=? AND status='active'
          AND sample_count<? AND success_count<? AND error_count<?
          AND latency_sample_count<? AND statistics_version<?
          AND total_latency_ms<=?
        """,
        (
            1 if succeeded else 0,
            0 if succeeded else 1,
            latency_ms,
            observed_text,
            observed_text,
            observed_text,
            observed_text,
            observed_text,
            observed_text,
            tenant_id,
            model_id,
            MAX_SQLITE_INTEGER,
            MAX_SQLITE_INTEGER,
            MAX_SQLITE_INTEGER,
            MAX_SQLITE_INTEGER,
            MAX_SQLITE_INTEGER,
            MAX_SQLITE_INTEGER - latency_ms,
        ),
    )
    if cursor.rowcount != 1:
        # owner 缺失、状态异常或整数将溢出都不能静默丢样本后提交终态。
        raise sqlite3.IntegrityError("model_quality_observation_unavailable")


def _to_db_time(value: datetime) -> str:
    """把有时区时刻转换为秒级 UTC Z 文本，保持统计和源终态可比较。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["record_model_quality_observation"]
