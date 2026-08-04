"""模型质量评分查询、人工评分 CAS、审计和持久事件服务。

业务边界：全部 SQL 使用认证 tenant_id；自动统计由 Gateway 的真实上游终态推进，
本模块只修改人工评分及其独立版本。事件和日志绝不保存 Prompt、Response、Endpoint、
Provider Key、Virtual Key、Token、逐请求 rating 或模型输出。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.feedback import round_ratio_half_up
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.routing_policy import (
    QUALITY_COST_FORMULA_VERSION,
    RoutingQualityFact,
    quality_component_bps,
)
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext

from .models import (
    ModelQualityScoreInfo,
    ModelQualityScoreListResponse,
    ModelQualityScoreMutationResponse,
)

logger = logging.getLogger("metahub.control.model_quality_scores")

# SQLite 有符号整数上限约束持久计数和 CAS 版本；PATCH 白名单则阻断未知字段进入写事务。
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
EXPECTED_MUTATION_FIELDS = frozenset(
    {"model_id", "manual_quality_bps", "expected_feedback_version"}
)


@dataclass(frozen=True)
class _FeedbackEventRecord:
    """从不可变评分事件行恢复提交后 Webhook 所需的安全事实。"""

    event_id: str
    tenant_id: str
    score_id: str
    model_id: str
    actor_user_id: str
    request_id: str
    trace_id: str
    request_fingerprint: str
    feedback_version: int
    manual_quality_bps: int | None
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class _RecoveredMutation:
    """一次已提交人工评分重试所需的当前响应、原事件和审计事实。"""

    score: ModelQualityScoreInfo
    event: _FeedbackEventRecord
    audit: AuditRecord


def list_model_quality_scores(
    config: AppConfig,
    context: AuthContext,
    *,
    model_id: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> ModelQualityScoreListResponse:
    """分页查询当前租户评分，并用 shared 公式派生有效质量分。"""

    normalized_model_id = _normalize_optional_model_id(model_id)
    connection = _connect(config, unavailable_code="model_quality_score.unavailable")
    try:
        # 指定 ID 必须先做 tenant-qualified 存在性检查，使跨租户与真实不存在保持相同 404。
        if normalized_model_id is not None:
            _require_score_row(connection, context.user.tenant_id, normalized_model_id)
        where = "WHERE qs.tenant_id=?"
        params: list[object] = [context.user.tenant_id]
        # 可选筛选仍同时绑定认证租户，不能仅凭全局 model_id 查询或统计其他租户记录。
        if normalized_model_id is not None:
            where += " AND qs.model_id=?"
            params.append(normalized_model_id)
        total_row = connection.execute(
            f"SELECT COUNT(*) AS total FROM model_quality_scores qs {where}",
            params,
        ).fetchone()
        rows = connection.execute(
            f"""
            SELECT
              qs.*, m.model_alias AS internal_model_id, m.display_name,
              m.status AS model_status, p.provider_code, p.name AS provider_name,
              COALESCE(fs.feedback_count, 0) AS request_feedback_count,
              COALESCE(fs.rating_sum, 0) AS request_feedback_rating_sum,
              COALESCE(fs.accepted_count, 0) AS request_feedback_accepted_count,
              COALESCE(fs.feedback_statistics_version, 0) AS request_feedback_statistics_version,
              COALESCE(fs.rating_1_count, 0) AS request_feedback_rating_1_count,
              COALESCE(fs.rating_2_count, 0) AS request_feedback_rating_2_count,
              COALESCE(fs.rating_3_count, 0) AS request_feedback_rating_3_count,
              COALESCE(fs.rating_4_count, 0) AS request_feedback_rating_4_count,
              COALESCE(fs.rating_5_count, 0) AS request_feedback_rating_5_count
            FROM model_quality_scores qs
            JOIN models m
              ON m.tenant_id=qs.tenant_id AND m.model_id=qs.model_id
            JOIN providers p
              ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
            LEFT JOIN model_feedback_statistics fs
              ON fs.tenant_id=qs.tenant_id AND fs.model_id=qs.model_id
            {where}
            ORDER BY m.model_alias ASC, qs.model_id ASC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        as_of = _utcnow()
        return ModelQualityScoreListResponse(
            model_quality_scores=[_score_info(row) for row in rows],
            total=int(total_row["total"] if total_row else 0),
            limit=limit,
            offset=offset,
            as_of=as_of,
            request_id=request_id,
            trace_id=trace_id,
        )
    # 领域异常保留既定 404 等公开语义，不能被宽泛转换成依赖不可用错误。
    except ApiError:
        raise
    # shared 公式或持久计数损坏属于只读依赖不可用，不能泄露异常正文或退回默认分。
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ApiError("model_quality_score.unavailable") from exc
    except sqlite3.Error as exc:
        # 查询失败只暴露固定领域码，不能把 SQL、数据库路径或底层异常正文回显。
        raise ApiError("model_quality_score.unavailable") from exc
    finally:
        connection.close()


def update_manual_quality_score(
    config: AppConfig,
    context: AuthContext,
    payload: dict[str, Any],
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> ModelQualityScoreMutationResponse:
    """以 tenant/actor/request_id 幂等和独立 feedback version CAS 更新人工评分。"""

    model_id, manual_bps, expected_version = _normalize_mutation(payload)
    normalized_request_id = _normalize_request_id(request_id)
    fingerprint = _mutation_fingerprint(model_id, manual_bps, expected_version)
    connection = _connect(config, unavailable_code="model_quality_score.update_failed")
    now = _to_db_time(_utcnow())
    audit_record: AuditRecord | None = None
    event: _FeedbackEventRecord | None = None
    score: ModelQualityScoreInfo | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        recovered = _recover_mutation(
            connection,
            context,
            request_id=normalized_request_id,
            fingerprint=fingerprint,
        )
        if recovered is not None:
            connection.rollback()
            _dispatch_recovered_mutation(config, context, recovered)
            return ModelQualityScoreMutationResponse(
                model_quality_score=recovered.score,
                changed=True,
                request_id=normalized_request_id,
                trace_id=recovered.event.trace_id,
            )

        before_row = _require_score_row(connection, context.user.tenant_id, model_id)
        before = _score_info(before_row)
        # 自动统计版本可独立增长；人工 feedback_version 才是本写操作必须匹配的 CAS 边界。
        if before.feedback_version != expected_version:
            raise ApiError("model_quality_score.version_conflict")
        if before.manual_quality_bps == manual_bps:
            # 同值 no-op 必须在版本匹配后返回，不写审计/事件，也不推进人工版本。
            connection.rollback()
            return ModelQualityScoreMutationResponse(
                model_quality_score=before,
                changed=False,
                request_id=normalized_request_id,
                trace_id=trace_id,
            )

        cursor = connection.execute(
            """
            UPDATE model_quality_scores
            SET manual_quality_bps=?, feedback_version=feedback_version+1,
                feedback_updated_by=?, feedback_updated_at=?, updated_at=?
            WHERE tenant_id=? AND model_id=? AND feedback_version=?
            """,
            (
                manual_bps,
                context.user.user_id,
                now,
                now,
                context.user.tenant_id,
                model_id,
                expected_version,
            ),
        )
        # BEGIN IMMEDIATE 内仍使用 tenant+version WHERE，防止未来数据库驱动改变锁语义。
        if cursor.rowcount != 1:
            raise ApiError("model_quality_score.version_conflict")
        after_row = _require_score_row(connection, context.user.tenant_id, model_id)
        score = _score_info(after_row)
        audit_record = record_audit_log(
            connection,
            context,
            action="model_quality_score.updated",
            object_type="model_quality_score",
            object_id=score.model_quality_score_id,
            object_name=score.internal_model_id,
            before_summary=_audit_summary(before),
            after_summary=_audit_summary(score),
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=trace_id,
        )
        if audit_record is None:
            # 真实人工评分变化必须有管理审计；空结果代表事务事实不完整。
            raise ApiError("model_quality_score.update_failed")
        event = _insert_feedback_event(
            connection,
            context,
            before=before,
            after=score,
            request_id=normalized_request_id,
            trace_id=trace_id,
            fingerprint=fingerprint,
            created_at=now,
        )
        commit_audited_transaction(connection)
    # 所有领域错误先回滚；仅审计内部错误需折叠，避免暴露子系统错误码或留下半事务。
    except ApiError as exc:
        connection.rollback()
        # 评分 API 对审计失败只公开统一更新错误，调用方不应依赖内部审计实现细节。
        if exc.code in {"audit_log.record_unavailable", "audit_log.summary_invalid"}:
            raise ApiError("model_quality_score.update_failed") from exc
        raise
    # 写路径遇到损坏 Score 或公式必须整体回滚，不能留下评分、审计或事件半事务。
    except (TypeError, ValueError, ArithmeticError) as exc:
        connection.rollback()
        raise ApiError("model_quality_score.update_failed") from exc
    # SQLite 写入异常同样回滚并隐藏 SQL、数据库路径和驱动正文，只返回稳定领域错误。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("model_quality_score.update_failed") from exc
    finally:
        connection.close()

    # 以下外部副作用只能发生在主事务提交后；失败不能撤销已经提交的人工评分。
    if score is None or audit_record is None or event is None:  # pragma: no cover - 事务路径已保证。
        raise ApiError("model_quality_score.update_failed")
    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )
    _dispatch_feedback_event(config, event)
    _log_feedback_changed(context, score, normalized_request_id, trace_id)
    return ModelQualityScoreMutationResponse(
        model_quality_score=score,
        changed=True,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )


def _require_score_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_id: str,
) -> sqlite3.Row:
    """按 tenant + model 加载评分及安全模型摘要，跨租户统一 404。"""

    row = connection.execute(
        """
        SELECT
          qs.*, m.model_alias AS internal_model_id, m.display_name,
          m.status AS model_status, p.provider_code, p.name AS provider_name,
          COALESCE(fs.feedback_count, 0) AS request_feedback_count,
          COALESCE(fs.rating_sum, 0) AS request_feedback_rating_sum,
          COALESCE(fs.accepted_count, 0) AS request_feedback_accepted_count,
          COALESCE(fs.feedback_statistics_version, 0) AS request_feedback_statistics_version,
          COALESCE(fs.rating_1_count, 0) AS request_feedback_rating_1_count,
          COALESCE(fs.rating_2_count, 0) AS request_feedback_rating_2_count,
          COALESCE(fs.rating_3_count, 0) AS request_feedback_rating_3_count,
          COALESCE(fs.rating_4_count, 0) AS request_feedback_rating_4_count,
          COALESCE(fs.rating_5_count, 0) AS request_feedback_rating_5_count
        FROM model_quality_scores qs
        JOIN models m
          ON m.tenant_id=qs.tenant_id AND m.model_id=qs.model_id
        JOIN providers p
          ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        LEFT JOIN model_feedback_statistics fs
          ON fs.tenant_id=qs.tenant_id AND fs.model_id=qs.model_id
        WHERE qs.tenant_id=? AND qs.model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()
    if row is None:
        # 不区分完全不存在与属于其他租户，避免 model_id 成为资源枚举通道。
        raise ApiError("model_quality_score.not_found")
    return row


def _score_info(row: sqlite3.Row) -> ModelQualityScoreInfo:
    """把权威整数计数转换为 API DTO，并复用 shared 质量公式。"""

    sample_count = _bounded_count(row["sample_count"])
    success_count = _bounded_count(row["success_count"])
    error_count = _bounded_count(row["error_count"])
    latency_sample_count = _bounded_count(row["latency_sample_count"])
    total_latency_ms = _bounded_count(row["total_latency_ms"])
    statistics_version = _bounded_count(row["statistics_version"])
    feedback_version = int(row["feedback_version"])
    manual_bps = _optional_bps(row["manual_quality_bps"])
    request_feedback_count = _bounded_count(row["request_feedback_count"])
    request_feedback_rating_sum = _bounded_count(row["request_feedback_rating_sum"])
    request_feedback_accepted_count = _bounded_count(row["request_feedback_accepted_count"])
    request_feedback_statistics_version = _bounded_count(
        row["request_feedback_statistics_version"]
    )
    request_feedback_distribution = {
        str(rating): _bounded_count(row[f"request_feedback_rating_{rating}_count"])
        for rating in range(1, 6)
    }
    if (
        request_feedback_accepted_count > request_feedback_count
        or request_feedback_statistics_version != request_feedback_count
        or sum(request_feedback_distribution.values()) != request_feedback_count
        or sum(
            rating * request_feedback_distribution[str(rating)]
            for rating in range(1, 6)
        )
        != request_feedback_rating_sum
    ):
        # 聚合虽受数据库验真 trigger 保护，读取层仍失败关闭，避免损坏/旧库静默展示错误质量统计。
        raise ValueError("request feedback aggregate invalid")
    # 逐请求反馈使用独立派生表和任意精度整数算法，不推进或参与 PRD-048 路由公式版本。
    request_feedback_average_milli = round_ratio_half_up(
        request_feedback_rating_sum,
        request_feedback_count,
        1_000,
    )
    request_feedback_adoption_rate_bps = round_ratio_half_up(
        request_feedback_accepted_count,
        request_feedback_count,
        10_000,
    )
    fact = RoutingQualityFact(
        model_quality_score_id=str(row["model_quality_score_id"]),
        model_id=str(row["model_id"]),
        sample_count=sample_count,
        success_count=success_count,
        error_count=error_count,
        latency_sample_count=latency_sample_count,
        total_latency_ms=total_latency_ms,
        manual_quality_bps=manual_bps,
        statistics_version=statistics_version,
        feedback_version=feedback_version,
    )
    effective_bps = quality_component_bps(fact)
    if sample_count:
        success_rate_bps = int(
            (Decimal(success_count) * Decimal(10_000) / Decimal(sample_count)).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
    else:
        success_rate_bps = 0
    # 错误率使用补数，避免两次独立四舍五入后出现合计 10001。
    error_rate_bps = 10_000 - success_rate_bps if sample_count else 0
    average_latency = None
    if latency_sample_count:
        average_latency = format(
            (Decimal(total_latency_ms) / Decimal(latency_sample_count)).quantize(
                Decimal("0.001"),
                rounding=ROUND_HALF_UP,
            ),
            "f",
        )
    return ModelQualityScoreInfo(
        model_quality_score_id=str(row["model_quality_score_id"]),
        tenant_id=str(row["tenant_id"]),
        model_id=str(row["model_id"]),
        internal_model_id=str(row["internal_model_id"]),
        display_name=row["display_name"],
        provider_code=str(row["provider_code"]),
        provider_name=str(row["provider_name"]),
        model_status=str(row["model_status"]),
        status=str(row["status"]),
        sample_count=sample_count,
        success_count=success_count,
        error_count=error_count,
        latency_sample_count=latency_sample_count,
        total_latency_ms=total_latency_ms,
        success_rate_bps=success_rate_bps,
        error_rate_bps=error_rate_bps,
        average_latency_ms=average_latency,
        manual_quality_bps=manual_bps,
        effective_quality_bps=effective_bps,
        observation_status="observed" if sample_count else "unobserved",
        formula_version=QUALITY_COST_FORMULA_VERSION,
        statistics_version=statistics_version,
        feedback_version=feedback_version,
        first_observed_at=_parse_optional_db_time(row["first_observed_at"]),
        last_observed_at=_parse_optional_db_time(row["last_observed_at"]),
        feedback_updated_by=row["feedback_updated_by"],
        feedback_updated_at=_parse_optional_db_time(row["feedback_updated_at"]),
        request_feedback_count=request_feedback_count,
        request_feedback_rating_sum=request_feedback_rating_sum,
        request_feedback_average_milli=request_feedback_average_milli,
        request_feedback_accepted_count=request_feedback_accepted_count,
        request_feedback_adoption_rate_bps=request_feedback_adoption_rate_bps,
        request_feedback_statistics_version=request_feedback_statistics_version,
        request_feedback_rating_distribution=request_feedback_distribution,
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _insert_feedback_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    before: ModelQualityScoreInfo,
    after: ModelQualityScoreInfo,
    request_id: str,
    trace_id: str,
    fingerprint: str,
    created_at: str,
) -> _FeedbackEventRecord:
    """在人工评分事务内写入不可变事件，并回读权威持久行。"""

    event_id = f"mqe_{uuid4().hex}"
    payload: dict[str, object] = {
        "trigger": "manual_score_updated",
        "request_id": request_id,
        "model_quality_score_id": after.model_quality_score_id,
        "model_id": after.model_id,
        "previous_manual_quality_bps": before.manual_quality_bps,
        "manual_quality_bps": after.manual_quality_bps,
        "feedback_version": after.feedback_version,
        "statistics_version": after.statistics_version,
        "sample_count": after.sample_count,
    }
    connection.execute(
        """
        INSERT INTO model_quality_score_events(
          model_quality_score_event_id, tenant_id, model_quality_score_id, model_id,
          event_type, actor_user_id, request_id, trace_id, request_fingerprint,
          feedback_version, statistics_version, sample_count,
          previous_manual_quality_bps, manual_quality_bps, payload_json, created_at
        )
        VALUES(?, ?, ?, ?, 'model.feedback_recorded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            context.user.tenant_id,
            after.model_quality_score_id,
            after.model_id,
            context.user.user_id,
            request_id,
            trace_id,
            fingerprint,
            after.feedback_version,
            after.statistics_version,
            after.sample_count,
            before.manual_quality_bps,
            after.manual_quality_bps,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            created_at,
        ),
    )
    row = connection.execute(
        """
        SELECT * FROM model_quality_score_events
        WHERE tenant_id=? AND model_quality_score_event_id=?
        """,
        (context.user.tenant_id, event_id),
    ).fetchone()
    # 插入后必须在同一租户事务内读回事件；缺行表示事务事实不完整，必须失败关闭。
    if row is None:
        raise ApiError("model_quality_score.update_failed")
    return _event_record(row)


def _recover_mutation(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    request_id: str,
    fingerprint: str,
) -> _RecoveredMutation | None:
    """按 tenant/actor/request_id 恢复人工 mutation，允许统计版本独立增长。"""

    row = connection.execute(
        """
        SELECT * FROM model_quality_score_events
        WHERE tenant_id=? AND actor_user_id=? AND request_id=?
        """,
        (context.user.tenant_id, context.user.user_id, request_id),
    ).fetchone()
    if row is None:
        return None
    event = _event_record(row)
    # 同一 request_id 只能恢复完全相同的规范化请求，指纹不同表示幂等键被错误复用。
    if event.request_fingerprint != fingerprint:
        raise ApiError("model_quality_score.request_conflict")
    current_row = _require_score_row(connection, context.user.tenant_id, event.model_id)
    score = _score_info(current_row)
    # 自动统计可继续增长；只有人工版本或人工值继续变化才使旧 mutation 响应失效。
    if (
        score.model_quality_score_id != event.score_id
        or score.feedback_version != event.feedback_version
        or score.manual_quality_bps != event.manual_quality_bps
    ):
        # Score 身份、人工版本和人工值必须同时匹配旧事件，否则不能伪造成功重放。
        raise ApiError("model_quality_score.request_conflict")
    audit = _load_feedback_audit_record(connection, context, event)
    return _RecoveredMutation(score=score, event=event, audit=audit)


def _event_record(row: sqlite3.Row) -> _FeedbackEventRecord:
    """严格解析不可变事件 JSON；损坏时不回显原始载荷。"""

    try:
        payload = json.loads(str(row["payload_json"]))
    # 不可变事件 JSON 损坏时隐藏原始载荷并终止幂等恢复，不能返回不完整成功响应。
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("model_quality_score.update_failed") from exc
    # 事件载荷必须维持对象结构；其他根类型代表持久事实损坏，不能按空对象修复。
    if not isinstance(payload, dict):
        raise ApiError("model_quality_score.update_failed")
    return _FeedbackEventRecord(
        event_id=str(row["model_quality_score_event_id"]),
        tenant_id=str(row["tenant_id"]),
        score_id=str(row["model_quality_score_id"]),
        model_id=str(row["model_id"]),
        actor_user_id=str(row["actor_user_id"]),
        request_id=str(row["request_id"]),
        trace_id=str(row["trace_id"]),
        request_fingerprint=str(row["request_fingerprint"]),
        feedback_version=int(row["feedback_version"]),
        manual_quality_bps=_optional_bps(row["manual_quality_bps"]),
        payload=payload,
        created_at=_parse_db_time(row["created_at"]),
    )


def _load_feedback_audit_record(
    connection: sqlite3.Connection,
    context: AuthContext,
    event: _FeedbackEventRecord,
) -> AuditRecord:
    """恢复与评分事件同事务提交的唯一审计事实。"""

    rows = connection.execute(
        """
        SELECT audit_log_id, tenant_id, actor_user_id, action, object_type,
               object_id, changed_fields_json, trace_id, created_at
        FROM audit_logs
        WHERE tenant_id=? AND actor_user_id=? AND request_id=?
          AND action='model_quality_score.updated'
          AND object_type='model_quality_score' AND object_id=?
        ORDER BY created_at, audit_log_id
        """,
        (
            context.user.tenant_id,
            context.user.user_id,
            event.request_id,
            event.score_id,
        ),
    ).fetchall()
    # 每次已提交评分变化必须恰好对应一条同租户审计记录，缺失或重复都拒绝恢复。
    if len(rows) != 1:
        raise ApiError("model_quality_score.update_failed")
    row = rows[0]
    try:
        changed = json.loads(str(row["changed_fields_json"]))
    # 审计字段 JSON 损坏时不得回显数据库正文，也不能绕过完整性检查继续重派事件。
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("model_quality_score.update_failed") from exc
    # changed_fields 必须是字符串白名单列表，异常结构说明审计事实不可用于安全重放。
    if not isinstance(changed, list) or not all(isinstance(item, str) for item in changed):
        raise ApiError("model_quality_score.update_failed")
    return AuditRecord(
        audit_log_id=str(row["audit_log_id"]),
        tenant_id=str(row["tenant_id"]),
        actor_user_id=str(row["actor_user_id"]),
        action=str(row["action"]),
        object_type=str(row["object_type"]),
        object_id=str(row["object_id"]),
        changed_fields=tuple(changed),
        trace_id=str(row["trace_id"]),
        created_at=_parse_db_time(row["created_at"]),
    )


def _dispatch_recovered_mutation(
    config: AppConfig,
    context: AuthContext,
    recovered: _RecoveredMutation,
) -> None:
    """用稳定事件 ID 重派已提交人工评分的审计与反馈 Webhook。"""

    dispatch_audit_recorded(
        config,
        context,
        recovered.audit,
        request_id=recovered.event.request_id,
        trace_id=recovered.event.trace_id,
    )
    _dispatch_feedback_event(config, recovered.event)


def _dispatch_feedback_event(config: AppConfig, event: _FeedbackEventRecord) -> None:
    """从已提交事件构造稳定 Webhook；外部故障只记录安全定位摘要。"""

    try:
        webhook_event = build_webhook_event(
            tenant_id=event.tenant_id,
            event_type="model.feedback_recorded",
            event_id=event.event_id,
            timestamp=event.created_at,
            actor_id=event.actor_user_id,
            object_id=event.score_id,
            trace_id=event.trace_id,
            payload=event.payload,
        )
        # 延迟导入避免评分模块与 Webhook 服务在应用启动时形成循环依赖。
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            event.tenant_id,
            webhook_event,
            request_id=event.request_id,
            trace_id=event.trace_id,
        )
    except Exception:
        # URL、接收端正文和底层异常可能敏感，结构化日志只保留固定错误与业务 ID。
        logger.error(
            "model_quality_score.webhook_dispatch_failed",
            extra={
                "event": "model_quality_score.webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "actor_id": event.actor_user_id,
                "object_id": event.score_id,
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _audit_summary(score: ModelQualityScoreInfo) -> dict[str, object]:
    """构造人工评分审计白名单摘要，不复制高频统计或模型内容。"""

    return {
        "model_quality_score_id": score.model_quality_score_id,
        "model_id": score.model_id,
        "status": score.status,
        "manual_quality_bps": score.manual_quality_bps,
        "feedback_version": score.feedback_version,
    }


def _normalize_mutation(payload: dict[str, Any]) -> tuple[str, int | None, int]:
    """严格归一 PATCH 三字段，拒绝 bool、额外字段和 PRD-049 反馈结构。"""

    # 精确字段集合防止客户端 tenant_id、未来字段或拼写错误静默进入人工评分事务。
    if set(payload) != EXPECTED_MUTATION_FIELDS:
        raise ApiError("model_quality_score.input_invalid")
    model_id = _normalize_model_id(payload.get("model_id"))
    manual = payload.get("manual_quality_bps")
    # 人工评分只接受整数基点或显式清空，拒绝 bool 和越界值避免持久化语义漂移。
    if manual is not None and (type(manual) is not int or not 0 <= manual <= 10_000):
        raise ApiError("model_quality_score.input_invalid")
    version = payload.get("expected_feedback_version")
    # CAS 版本必须落在 SQLite 可持久范围内，非法版本与真实并发冲突使用不同错误码。
    if type(version) is not int or version < 1 or version > MAX_SQLITE_INTEGER:
        raise ApiError("model_quality_score.version_invalid")
    return model_id, manual, version


def _normalize_model_id(value: object) -> str:
    """校验原样模型 ID，禁止 trim 后与另一资源发生幂等碰撞。"""

    # 原样标识拒绝空白和控制字符，避免规范化后指向另一租户资源或污染审计日志。
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        # 非法模型标识统一拒绝，绝不裁剪后继续查询或把原始危险字符写入审计事实。
        raise ApiError("model_quality_score.input_invalid")
    return value


def _normalize_optional_model_id(value: str | None) -> str | None:
    """归一可选 GET 模型筛选，并将非法值映射为查询错误。"""

    if value is None:
        return None
    try:
        return _normalize_model_id(value)
    # GET 参数只公开查询错误，不能泄露 PATCH 校验的内部错误分类或资源存在性。
    except ApiError as exc:
        raise ApiError("model_quality_score.query_invalid") from exc


def _normalize_request_id(value: str) -> str:
    """验证 exactly-once request_id，禁止空白、控制字符、裁剪和超长值。"""

    # request_id 参与租户和操作者级幂等键，原样非法值必须在开启事务前失败关闭。
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        # 非法幂等键不能进入写事务，避免不同原始请求在规范化后共享同一提交记录。
        raise ApiError("model_quality_score.update_failed")
    return value


def _mutation_fingerprint(model_id: str, manual_bps: int | None, version: int) -> str:
    """绑定模型、规范化评分和期望版本，生成稳定 SHA-256 幂等指纹。"""

    canonical = json.dumps(
        {
            "operation": "manual_quality_score_update",
            "model_id": model_id,
            "manual_quality_bps": manual_bps,
            "expected_feedback_version": version,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _bounded_count(value: object) -> int:
    """恢复 SQLite 非负整数计数；损坏事实必须失败关闭。"""

    # 持久计数若类型或范围损坏就拒绝派生评分，不能用零值掩盖失败请求和用量事实。
    if type(value) is not int or value < 0 or value > MAX_SQLITE_INTEGER:
        # 内部数据异常由调用场景转换：GET 为 unavailable，PATCH 必须为 update_failed。
        raise ValueError("model quality score count invalid")
    return value


def _optional_bps(value: object) -> int | None:
    """恢复可空人工基点评分，未知类型或越界表示持久事实损坏。"""

    if value is None:
        return None
    # 越界人工分表示数据库事实损坏，禁止截断或采用默认值后继续影响路由决策。
    if type(value) is not int or not 0 <= value <= 10_000:
        # 使用内部异常让查询和写入各自映射稳定领域码，不能在写路径冒充只读故障。
        raise ValueError("model quality score basis points invalid")
    return value


def _connect(config: AppConfig, *, unavailable_code: str) -> sqlite3.Connection:
    """建立数据库连接，并把基础设施异常折叠为调用方领域错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库连接细节可能包含文件路径或部署信息，只向调用方暴露其场景对应的领域码。
    except DbError as exc:
        raise ApiError(unavailable_code) from exc


def _log_feedback_changed(
    context: AuthContext,
    score: ModelQualityScoreInfo,
    request_id: str,
    trace_id: str,
) -> None:
    """记录不含评分正文之外敏感信息的结构化管理事件。"""

    logger.info(
        "model.feedback_recorded",
        extra={
            "event": "model.feedback_recorded",
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": score.model_quality_score_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "payload": {
                "model_id": score.model_id,
                "feedback_version": score.feedback_version,
                "statistics_version": score.statistics_version,
            },
        },
    )


def _parse_db_time(value: object) -> datetime:
    """严格解析数据库 UTC 时间；无时区旧值按仓库既有 UTC 约定恢复。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 持久时间损坏时拒绝构造错误时间线，并隐藏原始字段及底层解析异常正文。
    except (TypeError, ValueError) as exc:
        # 上层按 GET/PATCH 场景分别转换，不把只读 unavailable 错误泄漏到写事务。
        raise ValueError("model quality score timestamp invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析可空数据库时间，保持“尚未观察/尚未人工更新”的 None 语义。"""

    return None if value is None else _parse_db_time(value)


def _to_db_time(value: datetime) -> str:
    """把输入时刻统一写成秒级 UTC ``Z`` 文本。

    参数 ``value`` 为待持久化时间；返回数据库、审计与事件共同使用的字符串。
    统一时区并去除微秒可以保证跨组件比较和幂等指纹相关时间线保持稳定。
    """

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 时刻，便于 API、事件与迁移断言稳定比较。"""

    return datetime.now(UTC).replace(microsecond=0)


__all__ = ["list_model_quality_scores", "update_manual_quality_score"]
