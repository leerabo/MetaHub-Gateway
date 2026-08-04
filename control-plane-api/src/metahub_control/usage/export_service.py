"""PRD-043 用量 CSV 导出领域服务。

同步和异步导出共用同一租户筛选、固定 CSV schema、SQLite BLOB 制品与审计合同。
任何查询都先限定认证 tenant_id；任务、事件、审计和下载计数均使用明确事务边界，
后台生成在写事务外执行，避免大报表长期占用 SQLite 写锁。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext

from .models import UsageExportJobInfo, UsageExportStatusResponse
from .service import build_usage_filters, normalize_usage_filters

logger = logging.getLogger("metahub.control.usage.export")

# 文件列是对账 API 的稳定 schema，不能跟随数据库新增列自动扩张或泄露 raw usage。
CSV_COLUMNS = (
    "usage_ledger_id",
    "tenant_id",
    "request_id",
    "trace_id",
    "request_status",
    "request_error_code",
    "created_at",
    "updated_at",
    "environment_id",
    "environment",
    "environment_name",
    "project_id",
    "project_name",
    "application_id",
    "application_name",
    "virtual_key_id",
    "virtual_key_name",
    "virtual_key_prefix",
    "provider_id",
    "provider_code",
    "model_id",
    "request_model",
    "resolved_model",
    "endpoint",
    "routing_attempt_id",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_tokens",
    "cost_amount",
    "cost_cny",
    "cost_usd",
    "currency",
    "price_schema_version",
    "price_snapshot_id",
    "snapshot_input_price_per_1k",
    "snapshot_output_price_per_1k",
    "snapshot_currency",
    "price_plan_id",
    "price_effective_from",
    "estimated",
    "usage_source",
)

INTEGER_COLUMNS = frozenset({"input_tokens", "output_tokens", "total_tokens", "cache_tokens"})
DECIMAL_COLUMNS = frozenset(
    {
        "cost_amount",
        "cost_cny",
        "cost_usd",
        "snapshot_input_price_per_1k",
        "snapshot_output_price_per_1k",
    }
)
JOB_ID_PATTERN = re.compile(r"^uex_[0-9a-f]{32}$")
MAX_TEXT_CELL_BYTES = 4096


@dataclass(frozen=True)
class UsageExportRequestResult:
    """导出入口结果；content 非空表示路由应直接返回同步 CSV。"""

    response: UsageExportStatusResponse
    content: bytes | None


@dataclass(frozen=True)
class UsageExportDownloadResult:
    """已授权且完整性校验通过的下载结果。"""

    job: UsageExportJobInfo
    content: bytes
    audit_record: AuditRecord | None


@dataclass(frozen=True)
class ClaimedUsageExport:
    """后台 worker 已取得的租户任务和随机 lease；lease 丢失后结果必须丢弃。"""

    usage_export_job_id: str
    tenant_id: str
    lease_token: str


@dataclass(frozen=True)
class GeneratedCsv:
    """完整 CSV 及由逐行账本精确计算的安全汇总。"""

    content: bytes
    row_count: int
    total_tokens: int
    total_cost_cny: Decimal
    total_cost_usd: Decimal
    sha256: str


def request_usage_export(
    config: AppConfig,
    context: AuthContext,
    *,
    start_date: str | None,
    end_date: str | None,
    model_id: str | None,
    virtual_key_id: str | None,
    environment_id: str | None,
    project_id: str | None,
    application_id: str | None,
    provider_id: str | None,
    estimated: str | None,
    request_id_filter: str | None,
    trace_id_filter: str | None,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> UsageExportRequestResult:
    """创建或幂等回放当前租户的同步/异步 Usage CSV 导出。

    参数：筛选来自严格路由，租户和操作者来自认证上下文，request_id 同时是业务幂等键。
    返回：同步任务携带完整 CSV；异步任务仅返回可轮询 DTO。
    约束：同步制品生成在事务外完成，任务、事件和审计在一个短事务内原子提交。
    """

    filters = normalize_usage_filters(
        start_date=start_date,
        end_date=end_date,
        model_id=model_id,
        virtual_key_id=virtual_key_id,
        environment_id=environment_id,
        provider_id=provider_id,
        project_id=project_id,
        application_id=application_id,
        estimated=estimated,
        request_id_filter=request_id_filter,
        trace_id_filter=trace_id_filter,
    )
    filters_json = _json_text(filters)
    fingerprint = hashlib.sha256(filters_json.encode("utf-8")).hexdigest()
    connection = _connect(config)
    existing = _load_job_by_request_id(connection, context.user.tenant_id, request_id)
    if existing is not None:
        try:
            return _replay_existing_export(
                existing,
                requested_fingerprint=fingerprint,
                request_id=request_id,
                trace_id=trace_id,
            )
        finally:
            connection.close()

    snapshot_at = _to_snapshot_time(_utcnow())
    try:
        snapshot_ledger_sequence = _load_snapshot_ledger_sequence(
            connection,
            context.user.tenant_id,
        )
        row_count, total_tokens, total_cost_cny, total_cost_usd = _count_usage_rows(
            connection,
            context.user.tenant_id,
            filters,
            snapshot_at,
            snapshot_ledger_sequence,
        )
    # 已归一化的业务异常必须原样透传，关闭连接后保留 usage 专用错误码。
    except ApiError:
        connection.close()
        raise
    # 未知计数异常不得泄露 SQL、租户或账本细节，统一映射为导出不可用。
    except Exception as exc:
        connection.close()
        raise ApiError("usage.export_unavailable") from exc
    # 硬上限在创建任务前执行，避免超大租户查询占用 worker 和数据库制品空间。
    if row_count > config.usage_export_max_rows:
        connection.close()
        raise ApiError("usage.export_too_large")
    generation_mode = "sync" if row_count <= config.usage_export_sync_row_limit else "async"
    # worker 被关闭时不能创建永远停留 pending 的任务；小结果同步导出仍可用于人工排障。
    if generation_mode == "async" and not config.usage_export_enabled:
        connection.close()
        raise ApiError("usage.export_unavailable")
    job_id = f"uex_{uuid4().hex}"
    file_name = _file_name(job_id, filters)
    generated: GeneratedCsv | None = None
    if generation_mode == "sync":
        try:
            generated = _generate_csv(
                connection,
                context.user.tenant_id,
                filters,
                snapshot_at,
                snapshot_ledger_sequence,
                max_bytes=config.usage_export_max_bytes,
            )
            _ensure_generation_matches(
                generated,
                row_count=row_count,
                total_tokens=total_tokens,
                total_cost_cny=total_cost_cny,
                total_cost_usd=total_cost_usd,
            )
        # 生成器已识别的文件上限等业务错误必须保留原始统一错误码。
        except ApiError:
            connection.close()
            raise
        except Exception as exc:
            # CSV 类型转换、编码或标准库异常统一映射，不能落到 generic internal_error。
            connection.close()
            raise ApiError("usage.export_unavailable") from exc

    requested_audit: AuditRecord | None = None
    downloaded_audit: AuditRecord | None = None
    now = _utcnow()
    expires_at = now + timedelta(hours=config.usage_export_retention_hours)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 并发相同 X-Request-ID 在写锁内再次回读，只有一个调用能创建任务、事件和审计。
        existing = _load_job_by_request_id(connection, context.user.tenant_id, request_id)
        if existing is not None:
            connection.commit()
            try:
                return _replay_existing_export(
                    existing,
                    requested_fingerprint=fingerprint,
                    request_id=request_id,
                    trace_id=trace_id,
                )
            finally:
                connection.close()
        _insert_job(
            connection,
            job_id=job_id,
            tenant_id=context.user.tenant_id,
            requested_by=context.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            generation_mode=generation_mode,
            filters_json=filters_json,
            fingerprint=fingerprint,
            snapshot_at=snapshot_at,
            snapshot_ledger_sequence=snapshot_ledger_sequence,
            row_count=row_count,
            total_tokens=total_tokens,
            total_cost_cny=total_cost_cny,
            total_cost_usd=total_cost_usd,
            file_name=file_name,
            generated=generated,
            now=now,
            expires_at=expires_at,
        )
        requested_summary = _audit_summary(
            status="completed" if generated else "pending",
            generation_mode=generation_mode,
            row_count=row_count,
            total_tokens=total_tokens,
            total_cost_cny=total_cost_cny,
            total_cost_usd=total_cost_usd,
            content_size_bytes=len(generated.content) if generated else 0,
            download_count=0,
            filter_fingerprint=fingerprint,
        )
        requested_audit = record_audit_log(
            connection,
            context,
            action="usage_export.requested",
            object_type="usage_export_job",
            object_id=job_id,
            object_name="usage-csv",
            before_summary={},
            after_summary=requested_summary,
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        if generated is not None:
            _insert_completed_event(connection, job_id, context.user.tenant_id, context.user.user_id, request_id, trace_id, generated)
            connection.execute(
                "UPDATE usage_export_jobs SET download_count=1, updated_at=? WHERE tenant_id=? AND usage_export_job_id=?",
                (_to_db_time(now), context.user.tenant_id, job_id),
            )
            downloaded_summary = {**requested_summary, "download_count": 1}
            downloaded_audit = record_audit_log(
                connection,
                context,
                action="usage_export.downloaded",
                object_type="usage_export_job",
                object_id=job_id,
                object_name="usage-csv",
                before_summary=requested_summary,
                after_summary=downloaded_summary,
                request_meta=audit_request_meta,
                request_id=request_id,
                trace_id=trace_id,
            )
        commit_audited_transaction(connection)
    # 审计或事件层产生的统一业务异常需要回滚整个短事务，防止任务与审计不一致。
    except ApiError:
        connection.rollback()
        connection.close()
        raise
    # 数据库、JSON 或制品写入细节都不能进入 API；同 request_id 可安全重试。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        connection.rollback()
        connection.close()
        # 回滚后只暴露固定错误码，数据库、JSON 和租户任务细节不得进入响应。
        raise ApiError("usage.export_unavailable") from exc

    dispatch_audit_recorded(config, context, requested_audit, request_id=request_id, trace_id=trace_id)
    dispatch_audit_recorded(config, context, downloaded_audit, request_id=request_id, trace_id=trace_id)
    row = _load_job(connection, context.user.tenant_id, job_id)
    if row is None:
        connection.close()
        # 提交后任务缺失属于存储一致性故障，不能伪装成空导出或返回未验证内容。
        raise ApiError("usage.export_unavailable")
    job = _job_info(row)
    if generated is not None:
        _dispatch_usage_exported(config, row)
    result = UsageExportRequestResult(
        response=_status_response(job, request_id=request_id, trace_id=trace_id),
        content=generated.content if generated else None,
    )
    connection.close()
    return result


def get_usage_export_status(
    config: AppConfig,
    context: AuthContext,
    job_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> UsageExportStatusResponse:
    """读取当前租户导出任务状态；跨租户与不存在统一返回 404。"""

    normalized_job_id = _normalize_job_id(job_id)
    connection = _connect(config)
    try:
        # 状态查询只能推进当前租户当前 job；全局保留期清理由 scheduler 承担。
        connection.execute("BEGIN IMMEDIATE")
        row = _load_job(connection, context.user.tenant_id, normalized_job_id)
        if row is None:
            # 不存在和跨租户任务统一返回 404，避免通过状态接口枚举其他租户 ID。
            raise ApiError("usage.export_not_found")
        row = _expire_row_if_due(connection, row)
        connection.commit()
        return _status_response(_job_info(row), request_id=request_id, trace_id=trace_id)
    # 已归一化状态错误只回滚本次读取事务，保留 404/410 等对外合同。
    except ApiError:
        connection.rollback()
        raise
    # 非业务异常统一隐藏数据库和任务内部元数据，只暴露稳定错误码。
    except Exception as exc:
        connection.rollback()
        raise ApiError("usage.export_unavailable") from exc
    finally:
        connection.close()


def download_usage_export(
    config: AppConfig,
    context: AuthContext,
    job_id: str,
    *,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> UsageExportDownloadResult:
    """授权下载当前租户 completed 导出，并原子增加计数与写入审计。"""

    normalized_job_id = _normalize_job_id(job_id)
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_job(connection, context.user.tenant_id, normalized_job_id)
        if row is None:
            # 下载同样隐藏跨租户任务是否存在，避免通过制品接口形成租户侧信道。
            raise ApiError("usage.export_not_found")
        row = _expire_row_if_due(connection, row)
        _ensure_downloadable(row)
        content = bytes(row["csv_content"])
        # 长度校验先于哈希和返回，任何截断或损坏都必须拒绝下载。
        if len(content) != int(row["content_size_bytes"] or 0):
            raise ApiError("usage.export_unavailable")
        if hashlib.sha256(content).hexdigest() != row["content_sha256"]:
            # 完整性失败时绝不返回半截或被修改的财务制品。
            raise ApiError("usage.export_unavailable")
        before_count = int(row["download_count"] or 0)
        connection.execute(
            """
            UPDATE usage_export_jobs SET download_count=download_count+1, updated_at=?
            WHERE tenant_id=? AND usage_export_job_id=? AND status='completed'
            """,
            (_to_db_time(_utcnow()), context.user.tenant_id, normalized_job_id),
        )
        before = _audit_summary_from_row(row, download_count=before_count)
        after = {**before, "download_count": before_count + 1}
        audit_record = record_audit_log(
            connection,
            context,
            action="usage_export.downloaded",
            object_type="usage_export_job",
            object_id=normalized_job_id,
            object_name="usage-csv",
            before_summary=before,
            after_summary=after,
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    # 下载状态、完整性或审计业务错误必须回滚计数，不能记录一次未实际交付的下载。
    except ApiError:
        connection.rollback()
        connection.close()
        raise
    # 存储和转换异常统一隐藏底层内容，同时回滚下载计数与审计。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        connection.rollback()
        connection.close()
        # 下载事务失败后只返回固定错误码，不能泄露存储内容或审计内部状态。
        raise ApiError("usage.export_unavailable") from exc
    dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    updated = _load_job(connection, context.user.tenant_id, normalized_job_id)
    if updated is None:
        connection.close()
        # 审计提交后任务缺失说明事务外一致性损坏，禁止返回之前缓存的 CSV。
        raise ApiError("usage.export_unavailable")
    try:
        return UsageExportDownloadResult(
            job=_job_info(updated),
            content=content,
            audit_record=audit_record,
        )
    # DTO 转换中的业务错误保持统一错误结构，连接由 finally 可靠关闭。
    except ApiError:
        raise
    # 未知 DTO 异常不得泄露任务行或文件内容，统一映射为不可用。
    except Exception as exc:
        raise ApiError("usage.export_unavailable") from exc
    finally:
        connection.close()


def expire_usage_exports(config: AppConfig) -> int:
    """清除已过保留期的 BLOB 并保留安全任务元数据。"""

    connection = _connect(config)
    now = _to_db_time(_utcnow())
    try:
        cursor = connection.execute(
            """
            UPDATE usage_export_jobs
            SET status='expired', csv_content=NULL,
                object_key=NULL, lease_token=NULL, lease_until=NULL, updated_at=?
            WHERE status IN ('completed', 'failed') AND expires_at IS NOT NULL AND expires_at<=?
            """,
            (now, now),
        )
        connection.commit()
        count = int(cursor.rowcount)
        connection.close()
        return count
    # 全局清理失败不能输出数据库路径或 SQL，只返回调度器可识别的统一错误。
    # 领取事务失败时不能留下半 lease 或暴露 SQL，统一回滚并映射导出错误。
    except sqlite3.Error as exc:
        connection.rollback()
        connection.close()
        # 清理或领取失败均不能暴露数据库实现，调度器仅依赖统一错误码重试。
        raise ApiError("usage.export_unavailable") from exc


def claim_next_usage_export(config: AppConfig) -> ClaimedUsageExport | None:
    """以短写事务领取一个 pending 或 lease 过期任务，并返回随机 lease。"""

    connection = _connect(config)
    now = _utcnow()
    now_text = _to_db_time(now)
    failed_expiry = _to_db_time(now + timedelta(hours=config.usage_export_retention_hours))
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 第三次执行期间崩溃后不再产生第四次生成；将已耗尽 lease 的任务安全收敛为 failed。
        connection.execute(
            """
            UPDATE usage_export_jobs
            SET status='failed', lease_token=NULL, lease_until=NULL,
                error_code='usage.export_unavailable', completed_at=?, expires_at=?, updated_at=?
            WHERE status='processing' AND lease_until<=? AND attempt_count>=?
            """,
            (now_text, failed_expiry, now_text, now_text, config.usage_export_max_attempts),
        )
        row = connection.execute(
            """
            SELECT usage_export_job_id, tenant_id, status, attempt_count
            FROM usage_export_jobs
            WHERE generation_mode='async'
              AND attempt_count<?
              AND (status='pending' OR (status='processing' AND lease_until<=?))
            ORDER BY requested_at ASC, usage_export_job_id ASC
            LIMIT 1
            """,
            (config.usage_export_max_attempts, now_text),
        ).fetchone()
        if row is None:
            connection.commit()
            connection.close()
            return None
        lease_token = f"uel_{uuid4().hex}"
        lease_until = _to_db_time(now + timedelta(seconds=config.usage_export_lease_seconds))
        cursor = connection.execute(
            """
            UPDATE usage_export_jobs
            SET status='processing', attempt_count=attempt_count+1, lease_token=?, lease_until=?,
                started_at=COALESCE(started_at, ?), error_code=NULL, updated_at=?
            WHERE tenant_id=? AND usage_export_job_id=?
              AND (status='pending' OR (status='processing' AND lease_until<=?))
            """,
            (
                lease_token,
                lease_until,
                now_text,
                now_text,
                row["tenant_id"],
                row["usage_export_job_id"],
                now_text,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            connection.close()
            return None
        connection.commit()
        claim = ClaimedUsageExport(
            usage_export_job_id=row["usage_export_job_id"],
            tenant_id=row["tenant_id"],
            lease_token=lease_token,
        )
        connection.close()
        return claim
    # 读取已领取任务失败时不能继续生成未知租户制品，也不得记录底层数据库异常。
    except sqlite3.Error as exc:
        connection.rollback()
        connection.close()
        # lease 写事务失败必须回滚完整状态，并隐藏 SQL 与租户任务元数据。
        raise ApiError("usage.export_unavailable") from exc


def process_claimed_usage_export(config: AppConfig, claim: ClaimedUsageExport) -> bool:
    """在写事务外生成已领取任务，并用 lease 条件提交完整 BLOB 与完成事件。"""

    connection = _connect(config)
    try:
        row = connection.execute(
            """
            SELECT * FROM usage_export_jobs
            WHERE tenant_id=? AND usage_export_job_id=? AND status='processing' AND lease_token=?
            """,
            (claim.tenant_id, claim.usage_export_job_id, claim.lease_token),
        ).fetchone()
    # 已领取任务读取失败时不能继续生成未知制品，且不得输出数据库异常链。
    except sqlite3.Error as exc:
        connection.close()
        # 对外只返回 worker 可识别的固定错误码，原始 SQL 仅保留在异常链内部。
        raise ApiError("usage.export_unavailable") from exc
    if row is None:
        connection.close()
        return False
    generation_failed = False
    try:
        filters = _parse_filters(row["filters_json"])
        generated = _generate_csv(
            connection,
            claim.tenant_id,
            filters,
            row["snapshot_at"],
            int(row["snapshot_ledger_sequence"]),
            max_bytes=config.usage_export_max_bytes,
        )
        _ensure_generation_matches(
            generated,
            row_count=int(row["row_count"]),
            total_tokens=int(row["total_tokens"]),
            total_cost_cny=_decimal(row["total_cost_cny"]),
            total_cost_usd=_decimal(row["total_cost_usd"]),
        )
        now = _utcnow()
        expires_at = now + timedelta(hours=config.usage_export_retention_hours)
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE usage_export_jobs
            SET status='completed', csv_content=?, content_size_bytes=?, content_sha256=?,
                lease_token=NULL, lease_until=NULL, completed_at=?, expires_at=?, error_code=NULL, updated_at=?
            WHERE tenant_id=? AND usage_export_job_id=? AND status='processing' AND lease_token=?
            """,
            (
                generated.content,
                len(generated.content),
                generated.sha256,
                _to_db_time(now),
                _to_db_time(expires_at),
                _to_db_time(now),
                claim.tenant_id,
                claim.usage_export_job_id,
                claim.lease_token,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            connection.close()
            return False
        _insert_completed_event(
            connection,
            claim.usage_export_job_id,
            claim.tenant_id,
            row["requested_by"],
            row["request_id"],
            row["trace_id"],
            generated,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        # 先退出异常链作用域，确保生成游标及其 SQLite 读锁释放后再开启状态回退写事务。
        generation_failed = True
    if generation_failed:
        # 原始 SQL、CSV 或异常内容不能写日志；固定错误码驱动可恢复重试和最终失败。
        _retry_or_fail_claim(config, claim)
        return False
    completed = _load_job(connection, claim.tenant_id, claim.usage_export_job_id)
    if completed is not None:
        _dispatch_usage_exported(config, completed)
    connection.close()
    return True


def _retry_or_fail_claim(config: AppConfig, claim: ClaimedUsageExport) -> None:
    """按 attempt 上限把失败 lease 退回 pending 或终结为 failed。"""

    connection = _connect(config)
    now = _utcnow()
    try:
        row = connection.execute(
            """
            SELECT attempt_count FROM usage_export_jobs
            WHERE tenant_id=? AND usage_export_job_id=? AND status='processing' AND lease_token=?
            """,
            (claim.tenant_id, claim.usage_export_job_id, claim.lease_token),
        ).fetchone()
    # 重试状态读取失败时保持原 lease 供超时接管，禁止猜测 attempt 状态继续写入。
    except sqlite3.Error:
        connection.close()
        return
    if row is None:
        connection.close()
        return
    final = int(row["attempt_count"]) >= config.usage_export_max_attempts
    status = "failed" if final else "pending"
    completed_at = _to_db_time(now) if final else None
    expires_at = (
        _to_db_time(now + timedelta(hours=config.usage_export_retention_hours)) if final else None
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE usage_export_jobs
            SET status=?, lease_token=NULL, lease_until=NULL, error_code=?, completed_at=?, expires_at=?, updated_at=?
            WHERE tenant_id=? AND usage_export_job_id=? AND status='processing' AND lease_token=?
            """,
            (
                status,
                "usage.export_unavailable" if final else None,
                completed_at,
                expires_at,
                _to_db_time(now),
                claim.tenant_id,
                claim.usage_export_job_id,
                claim.lease_token,
            ),
        )
        connection.commit()
        connection.close()
    # 状态回退写入失败时由 lease 超时恢复，不能二次写入或泄露数据库细节。
    except sqlite3.Error:
        connection.rollback()
        connection.close()


def _insert_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    tenant_id: str,
    requested_by: str,
    request_id: str,
    trace_id: str,
    generation_mode: str,
    filters_json: str,
    fingerprint: str,
    snapshot_at: str,
    snapshot_ledger_sequence: int,
    row_count: int,
    total_tokens: int,
    total_cost_cny: Decimal,
    total_cost_usd: Decimal,
    file_name: str,
    generated: GeneratedCsv | None,
    now: datetime,
    expires_at: datetime,
) -> None:
    """在调用方事务内写入 pending async 或 completed sync 任务。"""

    completed = generated is not None
    connection.execute(
        """
        INSERT INTO usage_export_jobs(
          usage_export_job_id, tenant_id, requested_by, request_id, trace_id,
          status, generation_mode, format, filters_json, filter_fingerprint, snapshot_at,
          snapshot_ledger_sequence,
          row_count, total_tokens, total_cost_cny, total_cost_usd,
          file_name, content_type, csv_content, content_size_bytes, content_sha256,
          storage_backend, object_key, download_count, attempt_count,
          lease_token, lease_until, error_code, requested_at, started_at,
          completed_at, expires_at, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, 'csv', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'text/csv', ?, ?, ?,
               'sqlite_blob', NULL, 0, 0, NULL, NULL, NULL, ?, NULL, ?, ?, ?, ?)
        """,
        (
            job_id,
            tenant_id,
            requested_by,
            request_id,
            trace_id,
            "completed" if completed else "pending",
            generation_mode,
            filters_json,
            fingerprint,
            snapshot_at,
            snapshot_ledger_sequence,
            row_count,
            total_tokens,
            _decimal_text(total_cost_cny),
            _decimal_text(total_cost_usd),
            file_name,
            generated.content if generated else None,
            len(generated.content) if generated else 0,
            generated.sha256 if generated else None,
            _to_db_time(now),
            _to_db_time(now) if completed else None,
            _to_db_time(expires_at) if completed else None,
            _to_db_time(now),
            _to_db_time(now),
        ),
    )


def _insert_completed_event(
    connection: sqlite3.Connection,
    job_id: str,
    tenant_id: str,
    actor_user_id: str,
    request_id: str,
    trace_id: str,
    generated: GeneratedCsv,
) -> None:
    """在任务完成事务内追加唯一安全事件事实。"""

    payload: dict[str, object] = {
        "usage_export_job_id": job_id,
        "row_count": generated.row_count,
        "content_size_bytes": len(generated.content),
        "status": "completed",
    }
    # generation_mode 必须取任务权威值；避免调用方参数和数据库事实发生漂移。
    mode_row = connection.execute(
        "SELECT generation_mode FROM usage_export_jobs WHERE tenant_id=? AND usage_export_job_id=?",
        (tenant_id, job_id),
    ).fetchone()
    if mode_row is None:
        # 完成事件必须引用已落库任务的权威模式，缺失时终止整个完成事务。
        raise ApiError("usage.export_unavailable")
    payload["generation_mode"] = mode_row["generation_mode"]
    connection.execute(
        """
        INSERT INTO usage_export_events(
          usage_export_event_id, tenant_id, usage_export_job_id, event_type,
          actor_user_id, request_id, trace_id, payload_json, created_at
        ) VALUES(?, ?, ?, 'usage.exported', ?, ?, ?, ?, ?)
        """,
        (
            f"uee_{uuid4().hex}",
            tenant_id,
            job_id,
            actor_user_id,
            request_id,
            trace_id,
            _json_text(payload),
            _to_db_time(_utcnow()),
        ),
    )


def _load_snapshot_ledger_sequence(connection: sqlite3.Connection, tenant_id: str) -> int:
    """读取租户当前最大的只增账本序号，空租户以 0 作为稳定水位。

    参数：数据库连接与认证上下文中的 tenant_id。
    返回：只用于任务内部快照的非负只增序号，不进入 API、审计、事件或日志。
    约束：查询必须显式限定租户；该水位与任务一起持久化，避免 worker 排队期间同秒新写入造成漂移。
    """

    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(usage_export_sequence), 0) AS snapshot_ledger_sequence "
            "FROM usage_export_ledger_sequences WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
    # 水位读取失败意味着无法证明租户快照边界，必须拒绝创建任务。
    except sqlite3.Error as exc:
        # 底层 schema 或数据库异常不得把 SQL 细节暴露给调用方，统一交给导出错误码处理。
        raise ApiError("usage.export_unavailable") from exc
    return int(row["snapshot_ledger_sequence"] if row else 0)


def _count_usage_rows(
    connection: sqlite3.Connection,
    tenant_id: str,
    filters: dict[str, object | None],
    snapshot_at: str,
    snapshot_ledger_sequence: int,
) -> tuple[int, int, Decimal, Decimal]:
    """按导出共享筛选读取固定行数和精确账本汇总。"""

    where_sql, params = build_usage_filters(
        tenant_id,
        filters,
        snapshot_at=snapshot_at,
        snapshot_ledger_sequence=snapshot_ledger_sequence,
    )
    try:
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS row_count,
                   COALESCE(SUM(ul.total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(ul.cost_cny), 0) AS total_cost_cny,
                   COALESCE(SUM(ul.cost_usd), 0) AS total_cost_usd
            FROM usage_ledger ul
            JOIN usage_export_ledger_sequences uls
              ON uls.tenant_id=ul.tenant_id AND uls.usage_ledger_id=ul.usage_ledger_id
            LEFT JOIN request_logs rl
              ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
            {where_sql}
            """,
            params,
        ).fetchone()
    # 计数失败时不能创建基数未知的导出任务，底层 SQL 统一隐藏。
    except sqlite3.Error as exc:
        raise ApiError("usage.export_unavailable") from exc
    return (
        int(row["row_count"] if row else 0),
        int(row["total_tokens"] if row else 0),
        _decimal(row["total_cost_cny"] if row else 0),
        _decimal(row["total_cost_usd"] if row else 0),
    )


def _generate_csv(
    connection: sqlite3.Connection,
    tenant_id: str,
    filters: dict[str, object | None],
    snapshot_at: str,
    snapshot_ledger_sequence: int,
    *,
    max_bytes: int,
) -> GeneratedCsv:
    """读取固定账本列并生成带 BOM、CRLF 和公式防护的完整 CSV。"""

    where_sql, params = build_usage_filters(
        tenant_id,
        filters,
        snapshot_at=snapshot_at,
        snapshot_ledger_sequence=snapshot_ledger_sequence,
    )
    try:
        cursor = connection.execute(
            f"""
            SELECT
              ul.usage_ledger_id, ul.tenant_id, ul.request_id, rl.trace_id,
              rl.status AS request_status, rl.error_code AS request_error_code,
              ul.created_at, ul.updated_at,
              COALESCE(ul.environment_id, rl.environment_id) AS environment_id,
              COALESCE(ul.environment_code, rl.environment_code) AS environment,
              e.name AS environment_name,
              COALESCE(ul.project_id, rl.project_id) AS project_id, p.name AS project_name,
              COALESCE(ul.application_id, rl.application_id) AS application_id, a.name AS application_name,
              COALESCE(ul.virtual_key_id, rl.virtual_key_id) AS virtual_key_id,
              vk.name AS virtual_key_name, vk.key_prefix AS virtual_key_prefix,
              COALESCE(ul.provider_id, rl.provider_id) AS provider_id,
              COALESCE(ul.provider_code, rl.provider_code) AS provider_code,
              COALESCE(ul.model_id, rl.model_id) AS model_id,
              COALESCE(ul.request_model, rl.request_model) AS request_model,
              COALESCE(ul.resolved_model, rl.resolved_model) AS resolved_model,
              COALESCE(ul.endpoint, rl.endpoint) AS endpoint,
              ul.routing_attempt_id,
              ul.input_tokens, ul.output_tokens, ul.total_tokens, ul.cache_tokens,
              ul.cost_amount, ul.cost_cny, ul.cost_usd, ul.currency,
              ul.price_schema_version, ul.price_snapshot_id,
              ps.input_price_per_1k AS snapshot_input_price_per_1k,
              ps.output_price_per_1k AS snapshot_output_price_per_1k,
              ps.currency AS snapshot_currency, ps.price_plan_id, ps.effective_from AS price_effective_from,
              ul.estimated, ul.usage_source
            FROM usage_ledger ul
            JOIN usage_export_ledger_sequences uls
              ON uls.tenant_id=ul.tenant_id AND uls.usage_ledger_id=ul.usage_ledger_id
            LEFT JOIN request_logs rl
              ON rl.tenant_id=ul.tenant_id AND rl.request_id=ul.request_id
            LEFT JOIN virtual_keys vk
              ON vk.tenant_id=ul.tenant_id
             AND vk.virtual_key_id=COALESCE(ul.virtual_key_id, rl.virtual_key_id)
            LEFT JOIN environments e
              ON e.tenant_id=ul.tenant_id
             AND e.environment_id=COALESCE(ul.environment_id, rl.environment_id)
            LEFT JOIN projects p
              ON p.tenant_id=ul.tenant_id
             AND p.project_id=COALESCE(ul.project_id, rl.project_id)
            LEFT JOIN applications a
              ON a.tenant_id=ul.tenant_id
             AND a.application_id=COALESCE(ul.application_id, rl.application_id)
            LEFT JOIN price_snapshots ps
              ON ps.tenant_id=ul.tenant_id AND ps.price_snapshot_id=ul.price_snapshot_id
            {where_sql}
            ORDER BY ul.created_at ASC, ul.usage_ledger_id ASC
            """,
            params,
        )
    # 生成查询失败时不产生半份 CSV，数据库细节统一映射为导出不可用。
    except sqlite3.Error as exc:
        raise ApiError("usage.export_unavailable") from exc

    # TextIOWrapper 直接写入 BytesIO，tell() 为 O(1)；避免每行 getvalue().encode 导致 10 万行 O(n²)。
    binary = io.BytesIO()
    binary.write(b"\xef\xbb\xbf")
    text_stream = io.TextIOWrapper(binary, encoding="utf-8", newline="", write_through=True)
    writer = csv.writer(text_stream, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_COLUMNS)
    total_tokens = 0
    total_cost_cny = Decimal("0")
    total_cost_usd = Decimal("0")
    row_count = 0
    for row in cursor:
        writer.writerow([_csv_value(column, row[column]) for column in CSV_COLUMNS])
        row_count += 1
        total_tokens += int(row["total_tokens"] or 0)
        total_cost_cny += _decimal(row["cost_cny"])
        total_cost_usd += _decimal(row["cost_usd"])
        # 生成期间按当前编码后的实际字节提前拒绝，绝不保存或返回被截断的半份文件。
        if binary.tell() > max_bytes:
            raise ApiError("usage.export_too_large")
    text_stream.flush()
    content = binary.getvalue()
    # flush 后再次按最终字节数校验，覆盖编码缓冲区带来的尾部增长。
    if len(content) > max_bytes:
        raise ApiError("usage.export_too_large")
    return GeneratedCsv(
        content=content,
        row_count=row_count,
        total_tokens=total_tokens,
        total_cost_cny=total_cost_cny,
        total_cost_usd=total_cost_usd,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _csv_value(column: str, value: object | None) -> str:
    """按列类型生成安全单元格，文本执行公式注入防护，金额保持 Decimal 字符串。"""

    if value is None:
        return ""
    if column in INTEGER_COLUMNS:
        return str(int(value))
    if column in DECIMAL_COLUMNS:
        return _decimal_text(_decimal(value))
    if column == "estimated":
        return "true" if bool(int(value)) else "false"
    return _safe_text_cell(value)


def _safe_text_cell(value: object) -> str:
    """清理控制字符、限制长度，并阻止 Excel/LibreOffice 把文本解释为公式。"""

    raw = str(value).replace("\x00", "")
    # U+200B/U+2060/ESC 等 C 类字符可藏在公式前；必须先清理，再对真实可见首字符判定。
    cleaned = "".join(" " if unicodedata.category(char).startswith("C") else char for char in raw)
    dangerous = cleaned.lstrip().startswith(("=", "+", "-", "@"))
    encoded = cleaned.encode("utf-8")[:MAX_TEXT_CELL_BYTES]
    while encoded:
        try:
            cleaned = encoded.decode("utf-8")
            break
        # UTF-8 截断若落在多字节字符中间，只回退当前字符，绝不输出非法编码。
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    if dangerous:
        cleaned = f"'{cleaned}"
    return cleaned


def _ensure_generation_matches(
    generated: GeneratedCsv,
    *,
    row_count: int,
    total_tokens: int,
    total_cost_cny: Decimal,
    total_cost_usd: Decimal,
) -> None:
    """核对快照生成结果和任务不可变基数，任何漂移都拒绝完成。"""

    if (
        generated.row_count != row_count
        or generated.total_tokens != total_tokens
        or generated.total_cost_cny != total_cost_cny
        or generated.total_cost_usd != total_cost_usd
    ):
        # 计数与生成汇总不一致说明快照或数据被破坏，禁止把不可信制品标记完成。
        raise ApiError("usage.export_unavailable")


def _replay_existing_export(
    row: sqlite3.Row,
    *,
    requested_fingerprint: str,
    request_id: str,
    trace_id: str,
) -> UsageExportRequestResult:
    """按原任务模式幂等回放；不重复创建审计、事件或 worker。"""

    if row["filter_fingerprint"] != requested_fingerprint:
        # 同一幂等键绑定第一份规范筛选；不同筛选不能静默拿到旧租户制品。
        raise ApiError("usage.export_filter_invalid")
    job = _job_info(row)
    content: bytes | None = None
    if job.generation_mode == "sync":
        _ensure_downloadable(row)
        content = bytes(row["csv_content"])
        if len(content) != job.content_size_bytes or hashlib.sha256(content).hexdigest() != job.content_sha256:
            # 幂等回放也必须复验完整性，不能因为已有任务而绕过制品校验。
            raise ApiError("usage.export_unavailable")
    return UsageExportRequestResult(
        response=_status_response(job, request_id=request_id, trace_id=trace_id),
        content=content,
    )


def _ensure_downloadable(row: sqlite3.Row) -> None:
    """把任务状态映射为稳定下载错误，不泄露 BLOB 或内部 lease。"""

    status = str(row["status"])
    if status in {"pending", "processing"}:
        # 排队或处理中任务尚无可信完整制品，对外统一返回可重试的未就绪错误。
        raise ApiError("usage.export_not_ready")
    if status == "expired":
        # 过期任务只保留安全元数据，正文已经清除，必须返回明确的 410 合同。
        raise ApiError("usage.export_expired")
    if status != "completed" or row["csv_content"] is None:
        # failed 或状态/正文矛盾都不可下载，避免泄露内部失败和存储细节。
        raise ApiError("usage.export_unavailable")


def _expire_row_if_due(connection: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """在下载事务内原子过期到期终态，避免清理线程与下载竞态。"""

    if row["status"] in {"completed", "failed"} and row["expires_at"] and row["expires_at"] <= _to_db_time(_utcnow()):
        connection.execute(
            """
            UPDATE usage_export_jobs
            SET status='expired', csv_content=NULL,
                object_key=NULL, lease_token=NULL, lease_until=NULL, updated_at=?
            WHERE tenant_id=? AND usage_export_job_id=?
            """,
            (_to_db_time(_utcnow()), row["tenant_id"], row["usage_export_job_id"]),
        )
        refreshed = _load_job(connection, row["tenant_id"], row["usage_export_job_id"])
        if refreshed is None:
            # 原子过期后必须能回读当前租户任务，否则视为数据库一致性故障。
            raise ApiError("usage.export_unavailable")
        return refreshed
    return row


def _audit_summary(
    *,
    status: str,
    generation_mode: str,
    row_count: int,
    total_tokens: int,
    total_cost_cny: Decimal,
    total_cost_usd: Decimal,
    content_size_bytes: int,
    download_count: int,
    filter_fingerprint: str,
) -> dict[str, object]:
    """生成严格白名单的导出审计摘要。"""

    return {
        "status": status,
        "generation_mode": generation_mode,
        "format": "csv",
        "row_count": row_count,
        "total_tokens": total_tokens,
        "total_cost_cny": _decimal_text(total_cost_cny),
        "total_cost_usd": _decimal_text(total_cost_usd),
        "content_size_bytes": content_size_bytes,
        "download_count": download_count,
        "filter_fingerprint": filter_fingerprint,
    }


def _audit_summary_from_row(row: sqlite3.Row, *, download_count: int) -> dict[str, object]:
    """从任务行构造下载前后可比较的审计摘要。"""

    return _audit_summary(
        status=row["status"],
        generation_mode=row["generation_mode"],
        row_count=int(row["row_count"]),
        total_tokens=int(row["total_tokens"]),
        total_cost_cny=_decimal(row["total_cost_cny"]),
        total_cost_usd=_decimal(row["total_cost_usd"]),
        content_size_bytes=int(row["content_size_bytes"]),
        download_count=download_count,
        filter_fingerprint=row["filter_fingerprint"],
    )


def _dispatch_usage_exported(config: AppConfig, row: sqlite3.Row) -> None:
    """提交后尽力投递最小 `usage.exported`，Webhook 失败不能回滚财务制品。"""

    connection = _connect(config)
    try:
        event_row = connection.execute(
            """
            SELECT payload_json, created_at FROM usage_export_events
            WHERE tenant_id=? AND usage_export_job_id=? AND event_type='usage.exported'
            """,
            (row["tenant_id"], row["usage_export_job_id"]),
        ).fetchone()
    finally:
        connection.close()
    if event_row is None:
        return
    try:
        payload = json.loads(event_row["payload_json"])
        event = build_webhook_event(
            tenant_id=row["tenant_id"],
            event_type="usage.exported",
            event_id=f"evt_{row['usage_export_job_id']}",
            timestamp=_parse_db_time(event_row["created_at"]),
            actor_id=row["requested_by"],
            object_id=row["usage_export_job_id"],
            trace_id=row["trace_id"],
            payload=payload,
        )
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            row["tenant_id"],
            event,
            request_id=row["request_id"],
            trace_id=row["trace_id"],
        )
    except Exception:
        # 日志只记录安全标识和固定错误码，不输出筛选、CSV、URL 或异常正文。
        logger.warning(
            "usage.exported webhook dispatch failed",
            extra={
                "event": "usage.exported.dispatch_failed",
                "tenant_id": row["tenant_id"],
                "object_id": row["usage_export_job_id"],
                "request_id": row["request_id"],
                "trace_id": row["trace_id"],
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _job_info(row: sqlite3.Row) -> UsageExportJobInfo:
    """把数据库任务转换为不含 BLOB、lease 和对象路径的安全 DTO。"""

    return UsageExportJobInfo(
        usage_export_job_id=row["usage_export_job_id"],
        status=row["status"],
        generation_mode=row["generation_mode"],
        filters=_parse_filters(row["filters_json"]),
        row_count=int(row["row_count"]),
        total_tokens=int(row["total_tokens"]),
        total_cost_cny=_decimal_text(_decimal(row["total_cost_cny"])),
        total_cost_usd=_decimal_text(_decimal(row["total_cost_usd"])),
        file_name=row["file_name"],
        content_type=row["content_type"],
        content_size_bytes=int(row["content_size_bytes"]),
        content_sha256=row["content_sha256"],
        download_count=int(row["download_count"]),
        attempt_count=int(row["attempt_count"]),
        error_code=row["error_code"],
        requested_at=_parse_db_time(row["requested_at"]),
        started_at=_parse_optional_db_time(row["started_at"]),
        completed_at=_parse_optional_db_time(row["completed_at"]),
        expires_at=_parse_optional_db_time(row["expires_at"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _status_response(job: UsageExportJobInfo, *, request_id: str, trace_id: str) -> UsageExportStatusResponse:
    """为任务生成固定、相对且不携带 Token 的状态和下载 URL。"""

    base = f"/usage/exports/{job.usage_export_job_id}"
    return UsageExportStatusResponse(
        export_job=job,
        status_url=base,
        download_url=f"{base}/download",
        request_id=request_id,
        trace_id=trace_id,
    )


def _load_job(connection: sqlite3.Connection, tenant_id: str, job_id: str) -> sqlite3.Row | None:
    """按 tenant_id + job_id 读取任务，禁止仅凭全局 ID 查询。"""

    return connection.execute(
        "SELECT * FROM usage_export_jobs WHERE tenant_id=? AND usage_export_job_id=?",
        (tenant_id, job_id),
    ).fetchone()


def _load_job_by_request_id(connection: sqlite3.Connection, tenant_id: str, request_id: str) -> sqlite3.Row | None:
    """按租户与 X-Request-ID 读取导出幂等任务。

    参数：数据库连接、认证租户 ID 和请求链路 ID。
    返回：当前租户绑定该幂等键的任务，未创建时返回 None。
    约束：request_id 不能脱离 tenant_id 查询，避免不同租户复用链路 ID 时串取制品。
    """

    return connection.execute(
        "SELECT * FROM usage_export_jobs WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    ).fetchone()


def _normalize_job_id(value: object) -> str:
    """校验任务 ID；非法格式与不存在统一 404，避免形成枚举侧信道。"""

    normalized = str(value or "").strip().lower()
    if not JOB_ID_PATTERN.fullmatch(normalized):
        # 非法格式与不存在统一返回 404，避免任务 ID 校验成为枚举侧信道。
        raise ApiError("usage.export_not_found")
    return normalized


def _parse_filters(value: object) -> dict[str, object | None]:
    """解析持久化规范筛选并拒绝损坏或非对象任务。"""

    try:
        parsed = json.loads(str(value))
    # 持久化筛选损坏时不能继续构造 SQL，也不得把原始 JSON 内容暴露给 API。
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("usage.export_unavailable") from exc
    if not isinstance(parsed, dict):
        # worker 只接受设计约定的对象筛选，数组或标量任务统一判为不可用。
        raise ApiError("usage.export_unavailable")
    return parsed


def _file_name(job_id: str, filters: dict[str, object | None]) -> str:
    """生成不反射租户或业务名称的 ASCII CSV 文件名。"""

    start = str(filters.get("start_bound") or "all")[:10]
    end = str(filters.get("end_bound") or "all")[:10]
    return f"metahub-usage-{start}-{end}-{job_id[4:12]}.csv"


def _decimal(value: object | None) -> Decimal:
    """把 SQLite NUMERIC 安全转换为 Decimal，拒绝 NaN、无限和损坏金额。"""

    try:
        result = Decimal(str(value or 0))
    # 损坏金额不能降级成浮点或零值，否则审计汇总会与实际账本不一致。
    except (InvalidOperation, ValueError) as exc:
        raise ApiError("usage.export_unavailable") from exc
    if not result.is_finite() or result < 0:
        # 非有限值和负成本不属于可导出的合法账本金额，必须拒绝生成制品。
        raise ApiError("usage.export_unavailable")
    return result


def _decimal_text(value: Decimal) -> str:
    """输出不使用科学计数法的稳定金额字符串。"""

    return format(value, "f")


def _json_text(value: object) -> str:
    """生成稳定紧凑 JSON，供筛选指纹和事件事实复用。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建导出数据库连接并隐藏 URL、路径和底层驱动异常。"""

    try:
        return connect_database(config.database_url)
    # 数据库 URL、文件路径和驱动异常都属于敏感部署信息，不能穿透到调用方。
    except DbError as exc:
        raise ApiError("usage.export_unavailable") from exc


def _utcnow() -> datetime:
    """返回 UTC 当前时间，集中支持测试替换。"""

    return datetime.now(UTC)


def _to_db_time(value: datetime) -> str:
    """把时间稳定保存为 UTC 秒级字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_snapshot_time(value: datetime) -> str:
    """生成微秒级 UTC 时间边界，配合 rowid 水位形成稳定导出快照。"""

    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_db_time(value: object) -> datetime:
    """解析数据库 UTC 时间，损坏元数据统一判定导出不可用。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 损坏时间会影响过期和下载授权判断，统一拒绝且不暴露原始存储值。
    except ValueError as exc:
        raise ApiError("usage.export_unavailable") from exc
    if parsed.tzinfo is None:
        # 无时区时间会造成不同部署环境下的过期漂移，数据库合同要求显式 UTC。
        raise ApiError("usage.export_unavailable")
    return parsed.astimezone(UTC)


def _parse_optional_db_time(value: object | None) -> datetime | None:
    """解析可选任务时间并保持空值语义。

    参数：SQLite 任务行中的可选时间文本。
    返回：空值保持 None，非空值转换为 UTC datetime。
    约束：非空值复用严格解析，禁止无时区或损坏元数据进入 API DTO。
    """

    if value is None:
        return None
    return _parse_db_time(value)
