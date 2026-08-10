"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/request_logs/service.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.feedback import (
    FeedbackRecord,
    evaluate_feedback_eligibility,
    load_request_feedback_from_connection,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.error_diagnosis import ErrorDiagnosisInfo, diagnose_error
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext
from metahub_control.response_cache.models import ResponseCacheRequestLogInfo

from .models import (
    RequestFeedbackInfo,
    RequestLogBudgetEvaluation,
    RequestLogCacheAffinitySelection,
    RequestLogDetail,
    RequestLogDetailResponse,
    RequestLogDlpMatch,
    RequestLogInfo,
    RequestLogIntentRisk,
    RequestLogListResponse,
    RequestLogOutputSafetyDetail,
    RequestLogOutputSafetyDispute,
    RequestLogOutputSafetySummary,
    RequestLogProviderErrorFact,
    RequestLogRoutingAttempt,
    RequestLogRoutingCandidate,
    RequestLogRoutingDecision,
    RequestLogStreamSession,
    RequestLogUsageInfo,
)

# 调用日志查询状态和分页上限集中定义，避免租户日志检索出现无界扫描或状态绕过。
VALID_STATUSES = {"success", "failed"}
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
MAX_FILTER_LENGTH = 160
MAX_SNAPSHOT_VERSION = 9_223_372_036_854_775_807
# 输出安全字段使用封闭枚举和固定失败码；数据库校验被误关或历史文件损坏时，
# Control 仍必须失败关闭，不能把未知值包装成 clean/not_configured。
OUTPUT_SAFETY_SCAN_MODES = frozenset({"non_stream_full", "stream_window"})
OUTPUT_SAFETY_SCAN_STATUSES = frozenset(
    {"pending", "not_configured", "completed", "unavailable", "not_applicable"}
)
OUTPUT_SAFETY_DECISIONS = frozenset({"none", "clean", "matched", "blocked", "unknown"})
OUTPUT_SAFETY_ACTIONS = frozenset({"allow", "log_only", "block"})
OUTPUT_SAFETY_RULE_TYPES = frozenset({"keyword", "regex"})
OUTPUT_SAFETY_EXPOSURES = frozenset({"none", "partial", "complete", "unknown"})
OUTPUT_SAFETY_BILLING = frozenset({"not_required", "dispute_pending"})
OUTPUT_SAFETY_FAILURE_REASONS = frozenset(
    {
        "output_safety.fragment_limit_exceeded",
        "output_safety.response_shape_invalid",
        "output_safety.response_too_large",
        "output_safety.evidence_unavailable",
        "output_safety.rule_invalid",
        "output_safety.rule_snapshot_unavailable",
        "output_safety.sse_event_incomplete",
        "output_safety.sse_event_too_large",
        "output_safety.sse_json_duplicate_key",
        "output_safety.sse_json_invalid",
        "output_safety.sse_utf8_invalid",
        "output_safety.stream_incomplete",
        "output_safety.window_capacity_exceeded",
    }
)
OUTPUT_SAFETY_PRE_PROVIDER_STAGES = frozenset(
    {"authentication", "rate_limit", "budget", "dlp", "policy", "routing", "keypool"}
)
# ProviderErrorFact 的投影采用 exact schema；新增数据库列或上游任意 key 不能自动进入日志详情。
PROVIDER_ERROR_PROJECTION_KEYS = frozenset(
    {
        "body_length_bucket",
        "body_truncated",
        "is_json",
        "message",
        "message_present",
        "provider_error_code_class",
        "provider_error_code_present",
        "provider_error_type_class",
        "provider_error_type_present",
    }
)
ALLOWED_QUERY_KEYS = frozenset(
    {
        "status",
        "request_id",
        "model",
        "provider_code",
        "error_code",
        "environment_id",
        "project_id",
        "application_id",
        "snapshot_version",
        "limit",
        "offset",
    }
)


def list_request_logs(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    request_id_filter: str | None,
    model: str | None,
    provider_code: str | None,
    error_code: str | None,
    environment_id: str | None,
    project_id: str | None,
    application_id: str | None,
    snapshot_version: str | None,
    query_keys: tuple[str, ...],
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> RequestLogListResponse:
    """按当前租户分页查询调用日志。

    所有查询都以 context.user.tenant_id 为第一过滤条件，避免管理员或开发者
    通过 request_id/model/provider_code 猜测访问其他租户的调用记录。
    """

    if any(key not in ALLOWED_QUERY_KEYS for key in query_keys):
        # 未知 query 不能被静默忽略，否则调用方可能误以为已按快照或租户维度过滤。
        raise ApiError("request_log.query_invalid")
    normalized_status = _normalize_optional_status(status)
    normalized_request_id = _normalize_optional_filter(request_id_filter, "request_id")
    normalized_model = _normalize_optional_filter(model, "model")
    normalized_provider_code = _normalize_optional_filter(provider_code, "provider_code")
    normalized_error_code = _normalize_optional_filter(error_code, "error_code")
    normalized_environment_id = _normalize_optional_filter(environment_id, "environment_id")
    normalized_project_id = _normalize_optional_filter(project_id, "project_id")
    normalized_application_id = _normalize_optional_filter(application_id, "application_id")
    normalized_snapshot_version = _normalize_optional_snapshot_version(snapshot_version)
    normalized_limit = _normalize_limit(limit)
    normalized_offset = _normalize_offset(offset)

    where_sql, params = _build_filters(
        context.user.tenant_id,
        status=normalized_status,
        request_id_filter=normalized_request_id,
        model=normalized_model,
        provider_code=normalized_provider_code,
        error_code=normalized_error_code,
        environment_id=normalized_environment_id,
        project_id=normalized_project_id,
        application_id=normalized_application_id,
        snapshot_version=normalized_snapshot_version,
    )
    connection = _connect(config)
    try:
        # COUNT 与分页行必须来自同一只读快照，避免并发写入造成 total/page 漂移。
        connection.execute("BEGIN")
        total_row = connection.execute(f"SELECT COUNT(*) AS total FROM request_logs rl {where_sql}", params).fetchone()
        rows = connection.execute(
            f"""
            SELECT
              rl.request_id, rl.tenant_id, rl.trace_id, rl.virtual_key_id, vk.key_prefix AS virtual_key_prefix,
              rl.environment_id, rl.environment_code AS environment, e.name AS environment_name,
              rl.project_id, p.name AS project_name, rl.application_id, a.name AS application_name,
              rl.provider_id, rl.model_id, rl.endpoint, rl.request_model, rl.resolved_model,
              rl.provider_code, rl.status, rl.error_code, rl.stream_session_id, rl.stream_status,
              rl.snapshot_version, rl.policy_snapshot_status, rl.policy_decision, rl.policy_reason_code,
              rl.policy_id, rl.policy_action_type, rl.policy_target_model_id,
              rl.policy_mask_applied, rl.policy_mask_policy_id,
              rl.latency_ms,
              rl.input_tokens, rl.output_tokens, rl.total_tokens,
              rl.estimated_cost_amount, rl.currency, rl.price_schema_version,
              rl.budget_status, rl.budget_rule_id, rl.budget_scope_type, rl.budget_scope_id,
              rl.budget_action, rl.budget_reason_code, rl.budget_current_cost_cny, rl.budget_limit_cny,
              rl.dlp_status, rl.dlp_rule_id, rl.dlp_action, rl.dlp_match_count, rl.dlp_reason_code,
              rl.fallback_triggered, rl.routing_attempt_count, rl.final_routing_attempt_id,
              CASE WHEN EXISTS(
                SELECT 1 FROM request_feedbacks rf
                WHERE rf.tenant_id=rl.tenant_id AND rf.source_request_id=rl.request_id
              ) THEN 1 ELSE 0 END AS has_feedback,
              rl.created_at, rl.updated_at
            FROM request_logs rl
            LEFT JOIN virtual_keys vk ON vk.virtual_key_id = rl.virtual_key_id AND vk.tenant_id = rl.tenant_id
            LEFT JOIN environments e ON e.environment_id=rl.environment_id AND e.tenant_id=rl.tenant_id
            LEFT JOIN projects p ON p.project_id = rl.project_id AND p.tenant_id = rl.tenant_id
            LEFT JOIN applications a ON a.application_id = rl.application_id AND a.tenant_id = rl.tenant_id
            {where_sql}
            ORDER BY rl.created_at DESC, rl.request_id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, normalized_limit, normalized_offset],
        ).fetchall()
        response = RequestLogListResponse(
            request_logs=[_request_log_info(row) for row in rows],
            total=int(total_row["total"] if total_row else 0),
            limit=normalized_limit,
            offset=normalized_offset,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.rollback()
        return response
    finally:
        # sqlite Connection 的上下文管理器只管事务，不负责 close，必须显式释放。
        connection.close()


def get_request_log_detail(
    config: AppConfig,
    context: AuthContext,
    log_request_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> RequestLogDetailResponse:
    """读取当前租户内单条调用日志详情。

    详情会补充路由决策和用量账本，但仍不返回 raw prompt/response；Virtual Key
    只返回 key_prefix，便于排障时定位来源而不暴露密钥。
    """

    normalized_log_request_id = _normalize_required_request_id(log_request_id)
    connection = _connect(config)
    try:
        # 锚点、反馈、路由、预算、DLP、Usage 与流终态必须来自同一读快照。
        connection.execute("BEGIN")
        detail = load_request_log_detail_from_connection(
            connection,
            context.user.tenant_id,
            normalized_log_request_id,
            include_feedback=True,
        )
        connection.rollback()
    finally:
        # 404、SQLite/Pydantic 异常和成功返回都必须释放文件句柄与快照。
        connection.close()
    return RequestLogDetailResponse(
        request_log=detail, request_id=request_id, trace_id=trace_id
    )


def load_request_log_detail_from_connection(
    connection: sqlite3.Connection,
    tenant_id: str,
    log_request_id: str,
    *,
    include_feedback: bool = True,
) -> RequestLogDetail:
    """使用调用方连接加载一条 tenant-qualified RequestLog 详情。

    参数：调用方事务连接、可信 tenant、源 request ID，以及是否装载反馈正文。返回：
    旧 API 使用的详情 DTO。关键约束：本函数不创建、提交、回滚或关闭连接；Explain
    可传 ``include_feedback=False``，避免额外读取非验收所需反馈，同时保留已反馈资格状态。
    """

    normalized_log_request_id = _normalize_required_request_id(log_request_id)
    row = connection.execute(
        """
        SELECT
          rl.request_id, rl.tenant_id, rl.trace_id, rl.virtual_key_id, vk.key_prefix AS virtual_key_prefix,
          rl.environment_id, rl.environment_code AS environment, e.name AS environment_name,
          rl.project_id, p.name AS project_name, rl.application_id, a.name AS application_name,
          rl.provider_id, rl.model_id, rl.endpoint, rl.request_model, rl.resolved_model,
          rl.provider_code, rl.status, rl.error_code, rl.stream_session_id, rl.stream_status,
          rl.snapshot_version, rl.policy_snapshot_status, rl.policy_decision, rl.policy_reason_code,
          rl.policy_id, rl.policy_action_type, rl.policy_target_model_id,
          rl.policy_mask_applied, rl.policy_mask_policy_id,
          rl.latency_ms,
          rl.input_tokens, rl.output_tokens, rl.total_tokens,
          rl.estimated_cost_amount, rl.currency, rl.price_schema_version,
          rl.budget_status, rl.budget_rule_id, rl.budget_scope_type, rl.budget_scope_id,
          rl.budget_action, rl.budget_reason_code, rl.budget_current_cost_cny, rl.budget_limit_cny,
          rl.dlp_status, rl.dlp_rule_id, rl.dlp_action, rl.dlp_match_count, rl.dlp_reason_code,
          rl.intent_risk_result_id, rl.intent_risk_schedule_status, rl.intent_risk_mode,
          rl.intent_risk_type, rl.intent_risk_score, rl.intent_risk_action,
          rl.intent_risk_reason_code, rl.intent_risk_detector_version,
          rl.intent_risk_input_truncated,
          rl.fallback_triggered, rl.routing_attempt_count, rl.final_routing_attempt_id,
          CASE WHEN EXISTS(
            SELECT 1 FROM request_feedbacks rf
            WHERE rf.tenant_id=rl.tenant_id AND rf.source_request_id=rl.request_id
          ) THEN 1 ELSE 0 END AS has_feedback,
          rl.created_at, rl.updated_at
        FROM request_logs rl
        LEFT JOIN virtual_keys vk ON vk.virtual_key_id = rl.virtual_key_id AND vk.tenant_id = rl.tenant_id
        LEFT JOIN environments e ON e.environment_id=rl.environment_id AND e.tenant_id=rl.tenant_id
        LEFT JOIN projects p ON p.project_id = rl.project_id AND p.tenant_id = rl.tenant_id
        LEFT JOIN applications a ON a.application_id = rl.application_id AND a.tenant_id = rl.tenant_id
        WHERE rl.tenant_id=? AND rl.request_id=?
        """,
        (tenant_id, normalized_log_request_id),
    ).fetchone()
    if row is None:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("request_log.not_found")

    log_info = _request_log_info(row)
    output_safety = _load_output_safety(
        connection,
        tenant_id,
        normalized_log_request_id,
        row,
        log_info,
    )
    # 安全扫描事实优先于随后可能覆盖公开错误码的 FinOps 503；详情与列表必须给出
    # 相同 terminal_stage，避免安全页按阶段筛选后进入详情却变成 unknown。
    if output_safety.decision == "blocked" or output_safety.scan_status == "unavailable":
        log_info = log_info.model_copy(update={"terminal_stage": "output_safety"})
    feedback = (
        load_request_feedback_from_connection(
            connection,
            tenant_id=tenant_id,
            source_request_id=normalized_log_request_id,
        )
        if include_feedback
        else None
    )
    final_attempt = _load_feedback_final_attempt(
        connection,
        tenant_id,
        normalized_log_request_id,
        log_info.final_routing_attempt_id,
    )
    eligibility = evaluate_feedback_eligibility(
        request_status=log_info.status,
        stream_session_id=log_info.stream_session_id,
        stream_status=log_info.stream_status,
        routing_attempt_count=log_info.routing_attempt_count,
        final_routing_attempt_id=log_info.final_routing_attempt_id,
        request_model_id=row["model_id"],
        request_provider_id=row["provider_id"],
        final_attempt_id=(final_attempt["routing_attempt_id"] if final_attempt else None),
        final_attempt_status=(final_attempt["status"] if final_attempt else None),
        final_attempt_model_id=(final_attempt["model_id"] if final_attempt else None),
        final_attempt_provider_id=(final_attempt["provider_id"] if final_attempt else None),
        has_feedback=bool(row["has_feedback"]),
    )
    routing_decision = _load_routing_decision(connection, tenant_id, normalized_log_request_id)
    detail = RequestLogDetail(
        log=log_info,
        routing_decision=routing_decision,
        routing_candidates=_load_routing_candidates(
            connection,
            tenant_id,
            routing_decision.routing_decision_id if routing_decision else None,
        ),
        usage=_load_usage(connection, tenant_id, normalized_log_request_id),
        routing_attempts=_load_routing_attempts(connection, tenant_id, normalized_log_request_id),
        cache_affinity_selections=_load_cache_affinity_selections(
            connection,
            tenant_id,
            normalized_log_request_id,
        ),
        response_cache=_load_response_cache_projection(
            connection,
            tenant_id,
            normalized_log_request_id,
        ),
        budget_evaluations=_load_budget_evaluations(connection, tenant_id, normalized_log_request_id),
        dlp_matches=_load_dlp_matches(connection, tenant_id, normalized_log_request_id),
        intent_risk=_load_intent_risk(
            connection,
            tenant_id,
            normalized_log_request_id,
            row,
        ),
        output_safety=output_safety,
        stream_session=_load_stream_session(
            connection,
            tenant_id,
            normalized_log_request_id,
            log_info.stream_session_id,
        ),
        error_diagnosis=_diagnose_request_log_error(log_info, routing_decision),
        feedback=_request_feedback_info(feedback),
        feedback_eligible=eligibility.eligible,
        feedback_ineligible_reason=eligibility.reason,
    )
    return detail


def _load_response_cache_projection(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> ResponseCacheRequestLogInfo | None:
    """从 tenant-qualified request fact/hit 构造 7.5 exact 白名单，历史请求返回 null。"""

    # 少数旧单元测试使用迁移前的最小手工 schema；没有 0069 表时等价于历史不适用请求。
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='response_cache_request_facts'"
    ).fetchone() is None:
        return None
    row = connection.execute(
        """
        SELECT
          fact.tenant_id,fact.request_id,fact.trace_id,fact.cache_policy_id,
          fact.cache_policy_version,fact.status,fact.response_cache_hit_id,
          fact.hit,fact.charge_cache_hits,
          hit.tenant_id AS hit_tenant_id,hit.request_id AS hit_request_id,
          hit.trace_id AS hit_trace_id,hit.cache_policy_id AS hit_cache_policy_id,
          hit.cache_policy_version AS hit_cache_policy_version,
          hit.charge_cache_hits AS hit_charge_cache_hits,
          hit.source_generation_cost_status,hit.source_generation_cost_cny_micros,
          hit.current_reference_cost_status,hit.current_reference_cost_cny_micros,
          hit.saved_cost_status,hit.saved_cost_cny_micros,
          hit.billed_cost_status,hit.billed_cost_cny_micros
        FROM response_cache_request_facts fact
        LEFT JOIN response_cache_hits hit
          ON hit.tenant_id=fact.tenant_id
         AND hit.response_cache_hit_id=fact.response_cache_hit_id
         AND hit.request_id=fact.request_id
        WHERE fact.tenant_id=? AND fact.request_id=?
        """,
        (tenant_id, request_id),
    ).fetchone()
    if row is None:
        return None
    try:
        status = str(row["status"])
        is_hit = bool(row["hit"])
        policy_id = row["cache_policy_id"]
        policy_version = row["cache_policy_version"]
        charge = None if row["charge_cache_hits"] is None else bool(row["charge_cache_hits"])
        # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
        if str(row["tenant_id"]) != tenant_id or str(row["request_id"]) != request_id:
            # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
            raise ValueError
        if is_hit:
            # hit owner 必须与 fact 的 tenant/request/trace/policy/charge 全镜像，不能拼接半条账务事实。
            if (
                row["response_cache_hit_id"] is None
                or row["hit_tenant_id"] != tenant_id
                or row["hit_request_id"] != request_id
                or row["hit_trace_id"] != row["trace_id"]
                or row["hit_cache_policy_id"] != policy_id
                or row["hit_cache_policy_version"] != policy_version
                or bool(row["hit_charge_cache_hits"]) != charge
            ):
                # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
                raise ValueError
            source_status = str(row["source_generation_cost_status"])
            current_status = str(row["current_reference_cost_status"])
            saved_status = str(row["saved_cost_status"])
            billed_status = str(row["billed_cost_status"])
            source_amount = _response_cache_micros(row["source_generation_cost_cny_micros"])
            current_amount = _response_cache_micros(row["current_reference_cost_cny_micros"])
            saved_amount = _response_cache_micros(row["saved_cost_cny_micros"])
            billed_amount = _response_cache_micros(row["billed_cost_cny_micros"])
        else:
            source_status = current_status = saved_status = billed_status = "not_applicable"
            source_amount = current_amount = saved_amount = billed_amount = None
        return ResponseCacheRequestLogInfo(
            tenant_id=tenant_id,
            status=status,
            hit=is_hit,
            charge_cache_hits=charge,
            cache_policy_id=None if policy_id is None else str(policy_id),
            cache_policy_version=None if policy_version is None else int(policy_version),
            source_generation_cost_status=source_status,
            source_generation_cost_cny=source_amount,
            current_reference_cost_status=current_status,
            current_reference_cost_cny=current_amount,
            saved_cost_status=saved_status,
            saved_cost_cny=saved_amount,
            billed_cost_status=billed_status,
            billed_cost_cny=billed_amount,
        )
    # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
    except (TypeError, ValueError):
        # 损坏或跨 owner 的财务投影不能降级为 cache miss/null，否则会隐藏收费完整性故障。
        raise ApiError("response_cache.unavailable") from None


def _response_cache_micros(value: object) -> str | None:
    """把非负整数微元转换为 exact 六位 CNY 字符串，避免 float 精度漂移。"""

    if value is None:
        return None
    if type(value) is not int or value < 0:
        # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
        raise ValueError("invalid response cache amount")
    major, minor = divmod(value, 1_000_000)
    return f"{major}.{minor:06d}"


def _load_intent_risk(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
    request_log_row: sqlite3.Row,
) -> RequestLogIntentRisk:
    """按完整 owner 与执行元数据关联敏感意图摘要，并映射调度中间态。

    参数：当前只读快照连接、可信 tenant、顶层 RequestLog ID 和该日志行。返回：不含正文的
    安全摘要。关键约束：结果除 ``tenant_id + result_id + source_request_id`` 外，还必须逐项绑定
    trace、Virtual Key actor、分类模式、detector 版本和截断标志；任何漂移都必须失败关闭，
    不能当作 pending 掩盖完整性问题。
    """

    schedule_status = str(request_log_row["intent_risk_schedule_status"])
    result_id = request_log_row["intent_risk_result_id"]
    if schedule_status == "disabled":
        return _intent_risk_without_result(request_log_row, status="disabled")

    result = None
    if result_id is not None:
        result = connection.execute(
            """
            SELECT
              intent_risk_result_id, source_trace_id, actor_type, actor_id,
              status, classification_mode, risk_type,
              risk_score, risk_score_scale, action, reason_code, detector_version,
              ruleset_version, input_code_points, input_truncated, duration_ms, completed_at
            FROM intent_risk_results
            WHERE tenant_id=? AND intent_risk_result_id=?
              AND source_kind='gateway' AND source_request_id=?
            """,
            (tenant_id, result_id, request_id),
        ).fetchone()
        if result is None:
            # 这里只读取存在性，不投影另一 owner 的任何字段；
            # 其唯一用途是区分合法 pending 与换主污染。
            mismatched = connection.execute(
                """
                SELECT EXISTS(
                  SELECT 1 FROM intent_risk_results
                  WHERE intent_risk_result_id=?
                    AND NOT (tenant_id=? AND source_kind='gateway' AND source_request_id=?)
                ) AS found
                """,
                (result_id, tenant_id, request_id),
            ).fetchone()
            if mismatched is not None and bool(mismatched["found"]):
                # 已存在的结果 ID 证明这不是正常异步延迟，必须关闭跨 owner 拼接路径。
                raise ApiError("intent_risk.result_integrity_failed")

    if result is not None:
        # RequestLog 调度模式与不可变结果必须相容，防止合法 owner 下仍拼接错误阶段的终态。
        expected_mode = request_log_row["intent_risk_mode"]
        expected_statuses = {
            "queued": {"clean", "detected", "failed"},
            "blocked": {"blocked"},
            "failed_closed": {"failed"},
        }.get(schedule_status, set())
        if (
            result["source_trace_id"] != request_log_row["trace_id"]
            or result["actor_type"] != "virtual_key"
            or result["actor_id"] != request_log_row["virtual_key_id"]
            or result["classification_mode"] != expected_mode
            or result["detector_version"]
            != request_log_row["intent_risk_detector_version"]
            or bool(result["input_truncated"])
            != bool(request_log_row["intent_risk_input_truncated"])
            or result["status"] not in expected_statuses
            or schedule_status == "skipped"
        ):
            # 同 tenant/request 仍可能被错误链路、actor 或执行元数据污染，不能降级成普通状态展示。
            raise ApiError("intent_risk.result_integrity_failed")
        return RequestLogIntentRisk(
            intent_risk_result_id=result["intent_risk_result_id"],
            status=result["status"],
            classification_mode=result["classification_mode"],
            risk_type=result["risk_type"],
            risk_score=_optional_int(result["risk_score"]),
            risk_score_scale=_int_value(result["risk_score_scale"]),
            action=result["action"],
            reason_code=result["reason_code"],
            detector_version=result["detector_version"],
            ruleset_version=result["ruleset_version"],
            input_code_points=_optional_int(result["input_code_points"]),
            input_truncated=bool(result["input_truncated"]),
            duration_ms=_optional_int(result["duration_ms"]),
            completed_at=_parse_optional_db_time(result["completed_at"]),
        )

    if schedule_status == "blocked":
        # blocked 只有 result/event 事务提交后才成立，缺失终态即数据库证据损坏。
        raise ApiError("intent_risk.result_integrity_failed")
    if schedule_status == "queued":
        return _intent_risk_without_result(request_log_row, status="pending")
    if schedule_status == "skipped":
        return _intent_risk_without_result(request_log_row, status="skipped")
    if schedule_status == "failed_closed":
        return _intent_risk_without_result(request_log_row, status="failed")
    # 数据库 CHECK 正常情况下不会进入此分支；显式失败关闭可避免未来宽松迁移泄露未知状态。
    raise ApiError("intent_risk.result_integrity_failed")


def _intent_risk_without_result(
    request_log_row: sqlite3.Row,
    *,
    status: str,
) -> RequestLogIntentRisk:
    """把 disabled/pending/skipped/failed 调度事实投影为固定安全 DTO。

    无终态结果时不得补造 risk type、规则版本、耗时或完成时间；只能返回 RequestLog 已冻结的
    result ID、模式、固定原因、detector 版本和截断标志。
    """

    return RequestLogIntentRisk(
        intent_risk_result_id=request_log_row["intent_risk_result_id"],
        status=status,
        classification_mode=request_log_row["intent_risk_mode"],
        risk_type=None,
        risk_score=None,
        risk_score_scale=10000,
        # failed_closed 已执行失败关闭且数据库冻结显式 none；其余无终态状态仍保持 NULL。
        action=request_log_row["intent_risk_action"],
        reason_code=request_log_row["intent_risk_reason_code"],
        detector_version=request_log_row["intent_risk_detector_version"],
        ruleset_version=None,
        input_code_points=None,
        input_truncated=bool(request_log_row["intent_risk_input_truncated"]),
        duration_ms=None,
        completed_at=None,
    )


def _build_filters(
    tenant_id: str,
    *,
    status: str | None,
    request_id_filter: str | None,
    model: str | None,
    provider_code: str | None,
    error_code: str | None,
    environment_id: str | None,
    project_id: str | None,
    application_id: str | None,
    snapshot_version: int | None,
) -> tuple[str, list[object]]:
    """在 调用日志查询 内部复用 build filters 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    clauses = ["rl.tenant_id=?"]
    params: list[object] = [tenant_id]
    if status:
        clauses.append("rl.status=?")
        params.append(status)
    # 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
    if request_id_filter:
        clauses.append("rl.request_id=?")
        params.append(request_id_filter)
    if model:
        # model 过滤同时覆盖用户请求别名和实际解析出的内部模型 ID。
        clauses.append("(rl.request_model=? OR rl.resolved_model=?)")
        params.extend([model, model])
    if provider_code:
        clauses.append("rl.provider_code=?")
        params.append(provider_code)
    # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    if error_code:
        clauses.append("rl.error_code=?")
        params.append(error_code)
    if environment_id:
        clauses.append("rl.environment_id=?")
        params.append(environment_id)
    if project_id:
        clauses.append("rl.project_id=?")
        params.append(project_id)
    if application_id:
        clauses.append("rl.application_id=?")
        params.append(application_id)
    if snapshot_version is not None:
        clauses.append("rl.snapshot_version=?")
        params.append(snapshot_version)
    return f"WHERE {' AND '.join(clauses)}", params


def _load_routing_decision(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> RequestLogRoutingDecision | None:
    """按 tenant+request 加载权威路由决策及冻结 RoutingPolicy 元数据。

    0036 之前可能为同一 request 留下多条 legacy Decision；迁移必须保留历史事实，
    因此优先选择带持久 Policy 的新事实，否则按时间和稳定 ID 确定性选择最新一条。
    """

    row = connection.execute(
        """
        SELECT
          routing_decision_id, model_alias_id, decision_status, decision_reason,
          requested_model, environment, environment_id, selected_model_id,
          selected_internal_model_id, provider_id, provider_code, error_code,
          routing_policy_id, routing_policy_version, routing_mode, price_as_of,
          estimated_input_tokens, estimated_output_tokens, token_estimate_truncated,
          routing_policy_event_id,
          quality_weight_bps, cost_weight_bps, latency_weight_bps,
          quality_floor_bps, minimum_sample_count, quality_as_of,
          quality_formula_version, quality_scoring_performed,
          candidate_summary, created_at
        FROM routing_decisions
        WHERE tenant_id=? AND request_id=?
        ORDER BY
          CASE WHEN routing_policy_id IS NOT NULL THEN 0 ELSE 1 END,
          created_at DESC,
          routing_decision_id DESC
        LIMIT 1
        """,
        (tenant_id, request_id),
    ).fetchone()
    if row is None:
        return None
    return RequestLogRoutingDecision(
        routing_decision_id=str(row["routing_decision_id"]),
        model_alias_id=row["model_alias_id"],
        decision_status=row["decision_status"],
        decision_reason=row["decision_reason"],
        requested_model=row["requested_model"],
        environment=row["environment"],
        environment_id=row["environment_id"],
        selected_model_id=row["selected_model_id"],
        selected_internal_model_id=row["selected_internal_model_id"],
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        error_code=row["error_code"],
        routing_policy_id=row["routing_policy_id"],
        routing_policy_version=_optional_int(row["routing_policy_version"]),
        routing_mode=row["routing_mode"],
        price_as_of=_parse_optional_db_time(row["price_as_of"]),
        estimated_input_tokens=_optional_int(row["estimated_input_tokens"]),
        estimated_output_tokens=_optional_int(row["estimated_output_tokens"]),
        token_estimate_truncated=(
            bool(row["token_estimate_truncated"])
            if row["token_estimate_truncated"] is not None
            else None
        ),
        routing_policy_event_id=row["routing_policy_event_id"],
        quality_weight_bps=_optional_int(row["quality_weight_bps"]),
        cost_weight_bps=_optional_int(row["cost_weight_bps"]),
        latency_weight_bps=_optional_int(row["latency_weight_bps"]),
        quality_floor_bps=_optional_int(row["quality_floor_bps"]),
        minimum_sample_count=_optional_int(row["minimum_sample_count"]),
        quality_as_of=_parse_optional_db_time(row["quality_as_of"]),
        quality_formula_version=row["quality_formula_version"],
        quality_scoring_performed=bool(row["quality_scoring_performed"]),
        candidate_summary=_json_object(row["candidate_summary"]),
        created_at=_parse_db_time(row["created_at"]),
    )


def _load_feedback_final_attempt(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
    final_routing_attempt_id: str | None,
) -> sqlite3.Row | None:
    """按 tenant/request/final ID 读取反馈归因所需的最终 attempt 最小快照。

    参数来自已经租户限定的 RequestLog；返回只包含 attempt ID、状态、模型和 Provider。
    三重条件阻止猜到全局 attempt ID 后跨租户或跨调用读取。final ID 为空时不查询，
    shared 状态机会按严格 legacy 规则决定是否允许回退。
    """

    if final_routing_attempt_id is None:
        return None
    return connection.execute(
        """
        SELECT routing_attempt_id, status, model_id, provider_id
        FROM routing_attempts
        WHERE tenant_id=? AND request_id=? AND routing_attempt_id=?
        """,
        (tenant_id, request_id, final_routing_attempt_id),
    ).fetchone()


def _load_routing_candidates(
    connection: sqlite3.Connection,
    tenant_id: str,
    routing_decision_id: str | None,
) -> list[RequestLogRoutingCandidate]:
    """按 tenant+decision 加载强类型候选事实，绝不从兼容 candidate_summary 猜测字段。

    参数：认证租户和已由同租户 request 查询得到的 decision ID。返回：rank 优先、配置优先级
    次序的至多两项安全 DTO；无决策时不执行 SQL。所有展示 JOIN 继续绑定 tenant_id。
    """

    if routing_decision_id is None:
        return []
    rows = connection.execute(
        """
        SELECT
          c.routing_candidate_id, c.candidate_role, c.configured_priority,
          c.rank, c.status, c.reason_code, c.model_id,
          m.model_alias AS internal_model_id, m.display_name AS model_display_name,
          c.provider_id, p.provider_code, p.name AS provider_name,
          c.health_state, c.health_fresh, c.keypool_state,
          c.price_available, c.price_source, c.price_schema_version,
          c.currency, c.estimated_cost_amount,
          c.model_quality_score_id, c.quality_statistics_version,
          c.quality_feedback_version, c.quality_evidence_state,
          c.quality_sample_count, c.quality_success_count, c.quality_error_count,
          c.manual_quality_bps, c.average_latency_ms, c.quality_component_bps,
          c.cost_component_bps, c.latency_component_bps, c.weighted_score_bps,
          c.created_at
        FROM routing_candidates c
        LEFT JOIN models m
          ON m.tenant_id=c.tenant_id AND m.model_id=c.model_id
        LEFT JOIN providers p
          ON p.tenant_id=c.tenant_id AND p.provider_id=c.provider_id
        WHERE c.tenant_id=? AND c.routing_decision_id=?
        ORDER BY c.rank IS NULL, c.rank, c.configured_priority, c.routing_candidate_id
        """,
        (tenant_id, routing_decision_id),
    ).fetchall()
    return [
        RequestLogRoutingCandidate(
            routing_candidate_id=str(row["routing_candidate_id"]),
            candidate_role=str(row["candidate_role"]),
            configured_priority=int(row["configured_priority"]),
            rank=_optional_int(row["rank"]),
            status=str(row["status"]),
            reason_code=str(row["reason_code"]),
            model_id=str(row["model_id"]),
            internal_model_id=row["internal_model_id"],
            model_display_name=row["model_display_name"],
            provider_id=row["provider_id"],
            provider_code=row["provider_code"],
            provider_name=row["provider_name"],
            health_state=str(row["health_state"]),
            health_fresh=bool(row["health_fresh"]),
            keypool_state=str(row["keypool_state"]),
            price_available=bool(row["price_available"]),
            price_source=str(row["price_source"]),
            price_schema_version=row["price_schema_version"],
            currency=row["currency"],
            estimated_cost_amount=_optional_cost_text(row["estimated_cost_amount"]),
            model_quality_score_id=row["model_quality_score_id"],
            quality_statistics_version=_optional_int(row["quality_statistics_version"]),
            quality_feedback_version=_optional_int(row["quality_feedback_version"]),
            quality_evidence_state=str(row["quality_evidence_state"]),
            quality_sample_count=_optional_int(row["quality_sample_count"]),
            quality_success_count=_optional_int(row["quality_success_count"]),
            quality_error_count=_optional_int(row["quality_error_count"]),
            manual_quality_bps=_optional_int(row["manual_quality_bps"]),
            average_latency_ms=_optional_latency_text(row["average_latency_ms"]),
            quality_component_bps=_optional_int(row["quality_component_bps"]),
            cost_component_bps=_optional_int(row["cost_component_bps"]),
            latency_component_bps=_optional_int(row["latency_component_bps"]),
            weighted_score_bps=_optional_int(row["weighted_score_bps"]),
            created_at=_parse_db_time(row["created_at"]),
        )
        for row in rows
    ]


def _load_usage(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> list[RequestLogUsageInfo]:
    """按当前租户加载 调用日志查询 的 load usage 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """

    rows = connection.execute(
        """
        SELECT
          environment_id, environment_code, input_tokens, output_tokens, total_tokens, cache_tokens,
          normalized_usage, cost_amount, cost_cny, cost_usd, currency,
          price_schema_version, price_snapshot_id, estimated, usage_source, routing_attempt_id, created_at
        FROM usage_ledger
        WHERE tenant_id=? AND request_id=?
        ORDER BY created_at ASC
        """,
        (tenant_id, request_id),
    ).fetchall()
    return [_usage_info(row) for row in rows]


def _load_routing_attempts(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> list[RequestLogRoutingAttempt]:
    """按租户和请求 ID 读取最多两条上游尝试，禁止仅凭全局 ID 跨租户查询。"""

    rows = connection.execute(
        """
        SELECT
          routing_attempt_id, attempt_sequence, attempt_role, trigger_type, trigger_error_code,
          model_id, internal_model_id, provider_id, provider_code, status, error_code,
          error_category, upstream_http_status, retryable, latency_ms, usage_status,
          usage_ledger_id, estimated_cost_cny, created_at, completed_at
        FROM routing_attempts
        WHERE tenant_id=? AND request_id=?
        ORDER BY attempt_sequence ASC
        """,
        (tenant_id, request_id),
    ).fetchall()
    return [
        RequestLogRoutingAttempt(
            routing_attempt_id=str(row["routing_attempt_id"]),
            attempt_sequence=int(row["attempt_sequence"]),
            attempt_role=str(row["attempt_role"]),
            trigger_type=str(row["trigger_type"]),
            trigger_error_code=row["trigger_error_code"],
            model_id=str(row["model_id"]),
            resolved_model=str(row["internal_model_id"]),
            provider_id=str(row["provider_id"]),
            provider_code=str(row["provider_code"]),
            status=str(row["status"]),
            error_code=row["error_code"],
            error_category=row["error_category"],
            upstream_http_status=_optional_int(row["upstream_http_status"]),
            retryable=bool(row["retryable"]),
            latency_ms=_optional_int(row["latency_ms"]),
            usage_status=str(row["usage_status"]),
            usage_ledger_id=row["usage_ledger_id"],
            estimated_cost_cny=_optional_float(row["estimated_cost_cny"]),
            created_at=_parse_db_time(row["created_at"]),
            completed_at=_parse_optional_db_time(row["completed_at"]),
            provider_error_fact=_load_provider_error_fact(
                connection,
                tenant_id,
                routing_attempt_id=str(row["routing_attempt_id"]),
                stream_session_id=None,
                # Fact.retryable 必须取 owner 冻结值，不能按当前目录重新计算。
                owner_retryable=bool(row["retryable"]),
            ),
        )
        for row in rows
    ]


def _load_cache_affinity_selections(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> list[RequestLogCacheAffinitySelection]:
    """按 tenant+request 加载至多两条亲和选择安全快照。

    查询和返回均不包含 scope/Binding/Credential、data_class、Prompt 指纹或 endpoint
    revision。即使调用者猜到 Selection ID，也无法绕过已认证日志的租户与 request 边界。
    """

    rows = connection.execute(
        """
        SELECT
          cache_affinity_selection_id, cache_affinity_id, cache_affinity_version,
          attempt_sequence, selection_reason, estimated_hit, endpoint_preference_applied,
          target_kind, model_endpoint_id, provider_id, provider_code_snapshot, created_at
        FROM cache_affinity_selections
        WHERE tenant_id=? AND request_id=?
        ORDER BY attempt_sequence ASC
        LIMIT 2
        """,
        (tenant_id, request_id),
    ).fetchall()
    return [
        RequestLogCacheAffinitySelection(
            cache_affinity_selection_id=str(row["cache_affinity_selection_id"]),
            cache_affinity_id=str(row["cache_affinity_id"]),
            cache_affinity_version=int(row["cache_affinity_version"]),
            attempt_sequence=int(row["attempt_sequence"]),
            selection_reason=str(row["selection_reason"]),
            estimated_hit=bool(row["estimated_hit"]),
            endpoint_preference_applied=bool(row["endpoint_preference_applied"]),
            target_kind=str(row["target_kind"]),
            model_id=str(row["model_endpoint_id"]),
            provider_id=str(row["provider_id"]),
            provider_code=str(row["provider_code_snapshot"]),
            created_at=_parse_db_time(row["created_at"]),
        )
        for row in rows
    ]


def _load_budget_evaluations(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> list[RequestLogBudgetEvaluation]:
    """按租户读取单次调用触发过的预算评估明细。

    预算命中属于成本治理证据，查询必须同时限定 tenant_id 和 request_id，避免管理员在排障时
    看到其他租户的预算阈值、成本金额或策略动作。
    """

    rows = connection.execute(
        """
        SELECT
          budget_rule_id, environment_id, scope_type, scope_id, budget_date, spent_before_cny,
          estimated_request_cost_cny, projected_cost_cny, budget_cny, threshold_percent,
          budget_status, action_taken, reason_code, created_at
        FROM budget_evaluations
        WHERE tenant_id=? AND request_id=?
        ORDER BY created_at ASC, budget_evaluation_id ASC
        """,
        (tenant_id, request_id),
    ).fetchall()
    return [_budget_evaluation_info(row) for row in rows]


def _load_dlp_matches(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> list[RequestLogDlpMatch]:
    """按租户读取单次调用触发过的 DLP 命中摘要。

    DLP 命中可能涉及敏感输入，因此详情查询只返回哈希、长度和规则元数据，不返回任何命中文本明文。
    """

    rows = connection.execute(
        """
        SELECT
          dlp_match_id, dlp_rule_id, rule_name, rule_type, action,
          message_index, match_text_hash, match_length, created_at
        FROM dlp_matches
        WHERE tenant_id=? AND request_id=?
        ORDER BY created_at ASC, dlp_match_id ASC
        """,
        (tenant_id, request_id),
    ).fetchall()
    return [_dlp_match_info(row) for row in rows]


def _load_stream_session(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
    stream_session_id: str | None,
) -> RequestLogStreamSession | None:
    """按认证租户、请求 ID 和日志关联 ID 加载流会话详情。

    三个条件同时出现是防枚举边界：即使调用方猜到全局会话 ID，也不能绕过
    当前 request_logs 记录的租户和 request_id 关联。非流式日志不执行额外查询。
    """

    # 非流式日志没有会话关联，直接返回可空详情并避免执行无意义的数据库查询。
    if stream_session_id is None:
        return None
    row = connection.execute(
        """
        SELECT
          stream_session_id, status, inter_token_timeout_ms, chunk_count, bytes_sent,
          first_chunk_at, last_chunk_at, error_code, started_at, completed_at,
          upstream_http_status, retryable
        FROM stream_sessions
        WHERE tenant_id=? AND request_id=? AND stream_session_id=?
        """,
        (tenant_id, request_id, stream_session_id),
    ).fetchone()
    # 正常写入会由数据库一致性触发器保证关联存在；若历史库或人工修复造成缺口，
    # 控制面安全降级为 null，而不是放宽条件查询其他租户的会话。
    if row is None:
        return None
    return RequestLogStreamSession(
        stream_session_id=str(row["stream_session_id"]),
        status=str(row["status"]),
        inter_token_timeout_ms=int(row["inter_token_timeout_ms"]),
        chunk_count=_int_value(row["chunk_count"]),
        bytes_sent=_int_value(row["bytes_sent"]),
        first_chunk_at=_parse_optional_db_time(row["first_chunk_at"]),
        last_chunk_at=_parse_optional_db_time(row["last_chunk_at"]),
        error_code=row["error_code"],
        started_at=_parse_db_time(row["started_at"]),
        completed_at=_parse_optional_db_time(row["completed_at"]),
        upstream_http_status=_optional_int(row["upstream_http_status"]),
        retryable=bool(row["retryable"]),
        provider_error_fact=_load_provider_error_fact(
            connection,
            tenant_id,
            routing_attempt_id=None,
            stream_session_id=str(row["stream_session_id"]),
            owner_retryable=bool(row["retryable"]),
        ),
    )


def _load_provider_error_fact(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    routing_attempt_id: str | None,
    stream_session_id: str | None,
    owner_retryable: bool,
) -> RequestLogProviderErrorFact | None:
    """按 tenant 与 attempt/session 二选一 owner 读取一条冻结事实。

    参数中的 owner ID 只来自当前详情快照；返回值逐列构造公开 DTO。即使猜到其他租户的
    Fact ID，也没有按 Fact ID 单独读取的通道。``retryable`` 明确使用 owner 终态值，当前
    builtin 目录升级不能重算历史结果。
    """

    # owner 必须是 attempt/session 精确二选一，否则查询条件可能把事实关联到错误状态机。
    if (routing_attempt_id is None) == (stream_session_id is None):
        raise ApiError("request_log.query_unavailable")
    owner_column = (
        "routing_attempt_id" if routing_attempt_id is not None else "stream_session_id"
    )
    other_column = (
        "stream_session_id" if routing_attempt_id is not None else "routing_attempt_id"
    )
    owner_id = routing_attempt_id or stream_session_id
    # 列名只来自上方固定二选一，不接收 HTTP 或数据库文本；值仍全部参数化。
    row = connection.execute(
        f"""
        SELECT
          provider_error_fact_id, routing_attempt_id, stream_session_id,
          matched_error_map_id, matched_rule_source, matched_rule_version,
          matched_pattern_hash, error_map_revision, overlay_content_hash,
          builtin_catalog_version, error_map_snapshot_source,
          normalized_output_error_code, owner_terminal_error_code,
          upstream_http_status, transport_kind, match_basis,
          provider_error_code_class, provider_error_type_class,
          provider_error_code_present, provider_error_type_present, message_present,
          redacted_projection_json, projection_hash, body_length_bucket,
          body_truncated, is_json
        FROM provider_error_facts
        WHERE tenant_id=? AND {owner_column}=? AND {other_column} IS NULL
        """,
        (tenant_id, owner_id),
    ).fetchone()
    if row is None:
        return None
    return RequestLogProviderErrorFact(
        provider_error_fact_id=str(row["provider_error_fact_id"]),
        routing_attempt_id=row["routing_attempt_id"],
        stream_session_id=row["stream_session_id"],
        matched_error_map_id=str(row["matched_error_map_id"]),
        matched_rule_source=str(row["matched_rule_source"]),
        matched_rule_version=_optional_int(row["matched_rule_version"]),
        matched_pattern_hash=str(row["matched_pattern_hash"]),
        error_map_revision=int(row["error_map_revision"]),
        overlay_content_hash=str(row["overlay_content_hash"]),
        builtin_catalog_version=str(row["builtin_catalog_version"]),
        error_map_snapshot_source=str(row["error_map_snapshot_source"]),
        normalized_output_error_code=str(row["normalized_output_error_code"]),
        owner_terminal_error_code=str(row["owner_terminal_error_code"]),
        upstream_http_status=_optional_int(row["upstream_http_status"]),
        transport_kind=str(row["transport_kind"]),
        retryable=owner_retryable,
        match_basis=str(row["match_basis"]),
        provider_error_code_class=row["provider_error_code_class"],
        provider_error_type_class=row["provider_error_type_class"],
        provider_error_code_present=bool(row["provider_error_code_present"]),
        provider_error_type_present=bool(row["provider_error_type_present"]),
        message_present=bool(row["message_present"]),
        redacted_projection_json=_provider_error_projection(
            row["redacted_projection_json"]
        ),
        projection_hash=str(row["projection_hash"]),
        body_length_bucket=str(row["body_length_bucket"]),
        body_truncated=bool(row["body_truncated"]),
        is_json=bool(row["is_json"]),
    )


def _provider_error_projection(value: object) -> dict[str, Any]:
    """解析 ProviderErrorFact exact-schema 投影，拒绝新增、缺失或嵌套字段。"""

    try:
        parsed = json.loads(str(value))
    # 投影损坏时统一返回查询不可用，解析异常不得把数据库原文带入响应或日志。
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ApiError("request_log.query_unavailable") from None
    # exact schema 拒绝新增和缺失字段，防止未来敏感字段未经评审直接出现在详情 DTO。
    if type(parsed) is not dict or set(parsed) != PROVIDER_ERROR_PROJECTION_KEYS:
        raise ApiError("request_log.query_unavailable")
    # 投影只允许固定标量，嵌套容器可能携带原始 Provider 正文，必须失败关闭。
    if any(type(item) in {dict, list} for item in parsed.values()):
        raise ApiError("request_log.query_unavailable")
    return parsed


def _output_safety_summary_from_row(
    row: sqlite3.Row,
) -> RequestLogOutputSafetySummary | None:
    """从列表 JOIN 行恢复输出安全最小摘要，并复核 tenant/request/trace owner。

    参数：RequestLog 列表查询行。返回：没有 scan 时为 ``None``，否则返回固定摘要。
    关键约束：LEFT JOIN 的部分空行、跨 owner 行或未知枚举一律视为完整性故障，
    不能静默隐藏已持久化的安全事实。
    """

    keys = set(row.keys())
    if "output_safety_scan_id" not in keys:
        # 兼容尚未选择 PRD-071 列的内部旧查询；公开高级列表始终包含这些列。
        return None
    projected_keys = (
        "output_safety_scan_id",
        "output_safety_tenant_id",
        "output_safety_request_id",
        "output_safety_trace_id",
        "output_safety_scan_mode",
        "output_safety_scan_status",
        "output_safety_decision",
        "output_safety_exposure_state",
        "output_safety_emitted_token_count",
        "output_safety_emitted_sse_event_count",
        "output_safety_emitted_bytes",
        "output_safety_billing_disposition",
    )
    scan_id = row["output_safety_scan_id"]
    if scan_id is None:
        # 真正未命中的 LEFT JOIN 必须整组为空；部分为空说明 schema/查询结果被污染。
        if any(row[key] is not None for key in projected_keys[1:]):
            raise ApiError("output_safety.integrity_failed")
        return None
    if (
        row["output_safety_tenant_id"] != row["tenant_id"]
        or row["output_safety_request_id"] != row["request_id"]
        or row["output_safety_trace_id"] != row["trace_id"]
    ):
        # 列表摘要也必须逐边复核 owner，不能依赖 JOIN 条件替代完整性判断。
        raise ApiError("output_safety.integrity_failed")
    try:
        return RequestLogOutputSafetySummary(
            output_safety_scan_id=_output_safety_scan_id(scan_id),
            scan_mode=_output_safety_enum(
                row["output_safety_scan_mode"], OUTPUT_SAFETY_SCAN_MODES
            ),
            scan_status=_output_safety_enum(
                row["output_safety_scan_status"], OUTPUT_SAFETY_SCAN_STATUSES
            ),
            decision=_output_safety_enum(
                row["output_safety_decision"], OUTPUT_SAFETY_DECISIONS
            ),
            exposure_state=_output_safety_enum(
                row["output_safety_exposure_state"], OUTPUT_SAFETY_EXPOSURES
            ),
            emitted_token_count=_output_safety_count(
                row["output_safety_emitted_token_count"]
            ),
            emitted_sse_event_count=_output_safety_count(
                row["output_safety_emitted_sse_event_count"]
            ),
            emitted_bytes=_output_safety_count(row["output_safety_emitted_bytes"]),
            billing_disposition=_output_safety_enum(
                row["output_safety_billing_disposition"], OUTPUT_SAFETY_BILLING
            ),
        )
    # 专用完整性错误不能被列表的通用查询错误覆盖。
    except ApiError:
        # 错误已脱敏且使用固定目录文案，直接交给上层统一响应。
        raise
    except Exception as exc:
        # Pydantic/类型异常只作为 cause 保留，禁止底层值进入响应。
        raise ApiError("output_safety.integrity_failed") from exc


def _load_output_safety(
    connection: sqlite3.Connection,
    tenant_id: str,
    log_request_id: str,
    request_row: sqlite3.Row,
    log_info: RequestLogInfo,
) -> RequestLogOutputSafetyDetail:
    """读取并校验 RequestLog 对应的唯一 scan 与可选 dispute。

    参数：同一读事务连接、可信 tenant/request、RequestLog 行和已恢复摘要。返回：
    持久 scan 或严格派生的历史/不适用证据。关键约束：查询始终 tenant-first；
    scan、dispute、Usage 和 charge 任一 owner 或状态矛盾都返回统一 503。
    """

    rows = connection.execute(
        "SELECT * FROM output_safety_scans WHERE tenant_id=? AND request_id=?",
        (tenant_id, log_request_id),
    ).fetchall()
    if len(rows) > 1:
        # 0063 声明 tenant/request 唯一；重复 owner 代表 schema 或数据已损坏。
        raise ApiError("output_safety.integrity_failed")
    if not rows:
        return _missing_output_safety_detail(connection, request_row, log_info)
    scan = rows[0]
    if (
        scan["tenant_id"] != tenant_id
        or scan["request_id"] != log_request_id
        or scan["trace_id"] != log_info.trace_id
    ):
        # 同 tenant/request 仍必须复核 trace，防止错误挂接另一条链路证据。
        raise ApiError("output_safety.integrity_failed")
    dispute = _load_output_safety_dispute(
        connection,
        tenant_id,
        log_request_id,
        log_info,
        scan,
    )
    return _output_safety_detail_from_scan(scan, dispute)


def _missing_output_safety_detail(
    connection: sqlite3.Connection,
    request_row: sqlite3.Row,
    log_info: RequestLogInfo,
) -> RequestLogOutputSafetyDetail:
    """按 0063 时间边界为缺 scan 的历史或 Provider 前请求生成固定证据。

    Chat 请求在 0063 之后一旦可能进入 Provider 就必须已有 pending scan；此类日志缺行
    直接失败关闭。迁移前 Chat 只能标为 legacy_unavailable，非 Chat 或 Provider 前终止
    固定为 not_applicable，二者都不得被解释为 clean。
    """

    migration = connection.execute(
        """
        SELECT applied_at FROM schema_migrations
        WHERE version='0063' AND status='applied'
        """
    ).fetchone()
    if migration is None:
        # 无法证明 0063 时间边界时不能猜测历史状态。
        raise ApiError("output_safety.integrity_failed")
    applied_at = _parse_db_time(migration["applied_at"])
    created_at = _parse_db_time(request_row["created_at"])
    endpoint = str(request_row["endpoint"] or "").strip().lower().lstrip("/")
    is_chat = endpoint in {"chat/completions", "v1/chat/completions"}
    pre_provider = log_info.terminal_stage in OUTPUT_SAFETY_PRE_PROVIDER_STAGES
    # 非 Chat 没有文本扫描合同；Provider 前终止则确定没有可扫描输出。
    if not is_chat or pre_provider:
        scan_status = "not_applicable"
        decision = "none"
    elif created_at <= applied_at:
        scan_status = "legacy_unavailable"
        decision = "unknown"
    else:
        # 0063 后 Chat 已越过 Provider 前闸门却没有 owner 行，不能伪装成未配置。
        raise ApiError("output_safety.integrity_failed")
    return RequestLogOutputSafetyDetail(
        output_safety_scan_id=None,
        scan_mode=None,
        scan_status=scan_status,
        decision=decision,
        action=None,
        dlp_rule_id=None,
        rule_type=None,
        match_count=0,
        ruleset_fingerprint=None,
        detector_version=None,
        interval_tokens=None,
        window_tokens=None,
        checkpoint_count=0,
        observed_output_token_count=0,
        scanned_token_count=0,
        emitted_token_count=0,
        emitted_sse_event_count=0,
        emitted_bytes=0,
        exposure_state="unknown",
        billing_disposition="not_required",
        failure_reason_code=None,
        started_at=None,
        completed_at=None,
        dispute=None,
    )


def _load_output_safety_dispute(
    connection: sqlite3.Connection,
    tenant_id: str,
    log_request_id: str,
    log_info: RequestLogInfo,
    scan: sqlite3.Row,
) -> RequestLogOutputSafetyDispute | None:
    """读取并交叉验证 blocked scan 对应的账单复核 marker。

    dispute 不公开 Usage/charge ID，但服务端必须证明它们同 tenant/request/trace 且正式
    分录确为对应 usage_charge。FinOps durability 双失败允许 marker 缺失；该请求的
    公开错误码必须明确是 ``finops.usage_record_unavailable``，不能泛化放宽。
    """

    rows = connection.execute(
        """
        SELECT
          dispute.*,
          usage.usage_ledger_id AS owner_usage_ledger_id,
          charge.finops_ledger_entry_id AS owner_finops_ledger_entry_id,
          charge.source_request_id AS owner_source_request_id,
          charge.source_trace_id AS owner_source_trace_id,
          charge.usage_ledger_id AS owner_charge_usage_ledger_id,
          charge.entry_type AS owner_entry_type,
          charge.amount_micros AS owner_amount_micros
        FROM finops_ledger_disputes dispute
        LEFT JOIN usage_ledger usage
          ON usage.tenant_id=dispute.tenant_id
         AND usage.request_id=dispute.request_id
         AND usage.usage_ledger_id=dispute.usage_ledger_id
        LEFT JOIN finops_ledger_entries charge
          ON charge.tenant_id=dispute.tenant_id
         AND charge.source_request_id=dispute.request_id
         AND charge.finops_ledger_entry_id=dispute.finops_ledger_entry_id
        WHERE dispute.tenant_id=? AND dispute.request_id=?
        """,
        (tenant_id, log_request_id),
    ).fetchall()
    if len(rows) > 1:
        # 一个 request 只能有一个 append-only dispute，重复行不能选择其一返回。
        raise ApiError("output_safety.integrity_failed")
    blocked = scan["scan_status"] == "completed" and scan["decision"] == "blocked"
    if not rows:
        # 只有明确的 Usage 双失败允许 blocked 意图尚无可靠 marker。
        if blocked and log_info.error_code != "finops.usage_record_unavailable":
            # 普通阻断缺 marker 会伪装账单已进入复核，必须失败关闭。
            raise ApiError("output_safety.integrity_failed")
        return None
    if not blocked:
        # 非阻断 scan 出现 dispute 会误导财务执行人工冲正，必须拒绝。
        raise ApiError("output_safety.integrity_failed")
    row = rows[0]
    try:
        reversal_eligible = _output_safety_boolean(row["reversal_eligible"])
        amount_micros = _output_safety_count(row["owner_amount_micros"])
        valid = (
            row["tenant_id"] == tenant_id
            and row["request_id"] == log_request_id
            and row["trace_id"] == log_info.trace_id == scan["trace_id"]
            and row["output_safety_scan_id"] == scan["output_safety_scan_id"]
            and row["review_status"] == "dispute_pending"
            and row["reason_code"] == "output_safety.content_policy_violation"
            and row["owner_usage_ledger_id"] == row["usage_ledger_id"]
            and row["owner_finops_ledger_entry_id"] == row["finops_ledger_entry_id"]
            and row["owner_source_request_id"] == log_request_id
            and row["owner_source_trace_id"] == log_info.trace_id
            and row["owner_charge_usage_ledger_id"] == row["usage_ledger_id"]
            and row["owner_entry_type"] == "usage_charge"
            and reversal_eligible == (amount_micros > 0)
        )
        if not valid:
            # 账单争议、扫描、用量与正式分录的复合归属必须逐项相等，不能选择性信任。
            raise ApiError("output_safety.integrity_failed")
        return RequestLogOutputSafetyDispute(
            output_safety_scan_id=_output_safety_scan_id(
                row["output_safety_scan_id"]
            ),
            review_status="dispute_pending",
            reversal_eligible=reversal_eligible,
        )
    # 专用完整性错误保留统一 503 语义。
    except ApiError:
        # 固定业务错误不携带数据库正文，可直接向上抛出。
        raise
    except Exception as exc:
        # 解析异常可能含持久化值，只通过 cause 记录并返回固定错误码。
        raise ApiError("output_safety.integrity_failed") from exc


def _output_safety_detail_from_scan(
    row: sqlite3.Row,
    dispute: RequestLogOutputSafetyDispute | None,
) -> RequestLogOutputSafetyDetail:
    """验证完整扫描状态机并转换为详情 DTO。

    数据库 CHECK/trigger 是写边界，本函数是读边界的第二道防线；这里重复验证状态
    组合是为了防止损坏备份、关闭约束的运维操作或未来迁移把未知事实误报为 clean。
    """

    try:
        scan_mode = _output_safety_enum(row["scan_mode"], OUTPUT_SAFETY_SCAN_MODES)
        scan_status = _output_safety_enum(row["scan_status"], OUTPUT_SAFETY_SCAN_STATUSES)
        decision = _output_safety_enum(row["decision"], OUTPUT_SAFETY_DECISIONS)
        action = _output_safety_optional_enum(row["action"], OUTPUT_SAFETY_ACTIONS)
        rule_type = _output_safety_optional_enum(row["rule_type"], OUTPUT_SAFETY_RULE_TYPES)
        exposure = _output_safety_enum(row["exposure_state"], OUTPUT_SAFETY_EXPOSURES)
        billing = _output_safety_enum(row["billing_disposition"], OUTPUT_SAFETY_BILLING)
        failure_reason = _output_safety_optional_enum(
            row["failure_reason_code"], OUTPUT_SAFETY_FAILURE_REASONS
        )
        counts = {
            key: _output_safety_count(row[key])
            for key in (
                "match_count",
                "checkpoint_count",
                "observed_output_token_count",
                "scanned_token_count",
                "emitted_token_count",
                "emitted_sse_event_count",
                "emitted_bytes",
            )
        }
        fingerprint = _output_safety_fingerprint(row["ruleset_fingerprint"])
        dlp_rule_id = _output_safety_optional_identifier(row["dlp_rule_id"])
        started_at = _parse_db_time(row["started_at"])
        completed_at = _parse_optional_db_time(row["completed_at"])
        if completed_at is not None and completed_at < started_at:
            # 完成时间倒序无法证明扫描生命周期，不能继续投影。
            raise ValueError("output safety time invalid")
        interval = _output_safety_optional_count(row["interval_tokens"])
        window = _output_safety_optional_count(row["window_tokens"])
        if scan_mode == "non_stream_full":
            # 非流式扫描没有会话或滑窗参数，任一非空都表示模式污染。
            if row["stream_session_id"] is not None or interval is not None or window is not None:
                # 使用内部 ValueError 汇入统一完整性 503，避免泄露具体字段值。
                raise ValueError("output safety non-stream parameters invalid")
        elif (
            _output_safety_optional_identifier(row["stream_session_id"]) is None
            or interval is None
            or not 8 <= interval <= 256
            or window is None
            or not 32 <= window <= 1024
            or window < interval
        ):
            # 流式参数必须完整且满足 interval/window 冻结范围。
            raise ValueError("output safety stream parameters invalid")
        if not (
            counts["emitted_token_count"] <= counts["scanned_token_count"]
            <= counts["observed_output_token_count"]
        ):
            # 客户暴露不得超过已扫描，已扫描也不得超过 Provider 实际产出。
            raise ValueError("output safety count order invalid")
        _validate_output_safety_state(
            scan_mode=scan_mode,
            scan_status=scan_status,
            decision=decision,
            action=action,
            dlp_rule_id=dlp_rule_id,
            rule_type=rule_type,
            fingerprint=fingerprint,
            exposure=exposure,
            billing=billing,
            failure_reason=failure_reason,
            completed_at=completed_at,
            counts=counts,
            dispute=dispute,
        )
        if row["detector_version"] != "output-safety-dlp-v1":
            # 未知检测器可能使用不同状态含义，当前 DTO 不得擅自兼容。
            raise ValueError("output safety detector invalid")
        return RequestLogOutputSafetyDetail(
            output_safety_scan_id=_output_safety_scan_id(row["output_safety_scan_id"]),
            scan_mode=scan_mode,
            scan_status=scan_status,
            decision=decision,
            action=action,
            dlp_rule_id=dlp_rule_id,
            rule_type=rule_type,
            match_count=counts["match_count"],
            ruleset_fingerprint=fingerprint,
            detector_version="output-safety-dlp-v1",
            interval_tokens=interval,
            window_tokens=window,
            checkpoint_count=counts["checkpoint_count"],
            observed_output_token_count=counts["observed_output_token_count"],
            scanned_token_count=counts["scanned_token_count"],
            emitted_token_count=counts["emitted_token_count"],
            emitted_sse_event_count=counts["emitted_sse_event_count"],
            emitted_bytes=counts["emitted_bytes"],
            exposure_state=exposure,
            billing_disposition=billing,
            failure_reason_code=failure_reason,
            started_at=started_at,
            completed_at=completed_at,
            dispute=dispute,
        )
    # 下游 owner 校验生成的专用错误保持原语义。
    except ApiError:
        # 固定错误码已完成脱敏，可直接交给统一异常处理中间件。
        raise
    except Exception as exc:
        # 所有状态机/类型异常统一失败关闭，不向客户端回显内部字段。
        raise ApiError("output_safety.integrity_failed") from exc


def _validate_output_safety_state(
    *,
    scan_mode: str,
    scan_status: str,
    decision: str,
    action: str | None,
    dlp_rule_id: str | None,
    rule_type: str | None,
    fingerprint: str | None,
    exposure: str,
    billing: str,
    failure_reason: str | None,
    completed_at: datetime | None,
    counts: dict[str, int],
    dispute: RequestLogOutputSafetyDispute | None,
) -> None:
    """验证 scan 终态矩阵以及 dispute 与 blocked 的对应关系。"""

    match_count = counts["match_count"]
    zero_observation = all(
        counts[key] == 0
        for key in (
            "checkpoint_count",
            "observed_output_token_count",
            "scanned_token_count",
            "emitted_token_count",
            "emitted_sse_event_count",
            "emitted_bytes",
        )
    )
    no_rule = action is None and dlp_rule_id is None and rule_type is None and match_count == 0
    if scan_status == "pending":
        valid = (
            decision == "none" and no_rule and fingerprint is None and zero_observation
            and exposure == "unknown" and billing == "not_required"
            and failure_reason is None and completed_at is None and dispute is None
        )
    elif scan_status in {"not_configured", "not_applicable"}:
        valid = (
            decision == "none" and no_rule and fingerprint is not None and zero_observation
            and exposure == "unknown" and billing == "not_required"
            and failure_reason is None and completed_at is not None and dispute is None
        )
    elif scan_status == "completed" and decision == "clean":
        valid = (
            no_rule and fingerprint is not None and counts["checkpoint_count"] >= 1
            and exposure == "complete" and billing == "not_required"
            and failure_reason is None and completed_at is not None and dispute is None
        )
    elif scan_status == "completed" and decision == "matched":
        valid = (
            action in {"allow", "log_only"} and dlp_rule_id is not None
            and rule_type is not None and match_count >= 1 and fingerprint is not None
            and counts["checkpoint_count"] >= 1 and exposure == "complete"
            and billing == "not_required" and failure_reason is None
            and completed_at is not None and dispute is None
        )
    elif scan_status == "completed" and decision == "blocked":
        valid = (
            action == "block" and dlp_rule_id is not None and rule_type is not None
            and match_count >= 1 and fingerprint is not None
            and counts["checkpoint_count"] >= 1 and exposure in {"none", "partial"}
            and billing == "dispute_pending" and failure_reason is None
            and completed_at is not None
        )
        if scan_mode == "non_stream_full":
            valid = valid and counts["emitted_token_count"] == 0
            valid = valid and counts["emitted_sse_event_count"] == 0
            valid = valid and counts["emitted_bytes"] == 0 and exposure == "none"
    elif scan_status == "unavailable":
        matched = (
            decision == "matched" and action in {"allow", "log_only"}
            and dlp_rule_id is not None and rule_type is not None
            and match_count >= 1 and fingerprint is not None
        )
        unknown = decision == "unknown" and no_rule
        valid = (
            (matched or unknown) and exposure in {"none", "partial", "unknown"}
            and billing == "not_required" and failure_reason is not None
            and completed_at is not None and dispute is None
        )
    else:
        valid = False
    if not valid:
        # 未落入冻结矩阵的组合不能被前端解释为任何已知安全状态。
        raise ValueError("output safety state invalid")


def _output_safety_enum(value: object, allowed: frozenset[str]) -> str:
    """返回受白名单约束的数据库枚举，未知值触发读模型失败关闭。"""

    if not isinstance(value, str) or value not in allowed:
        # 未知枚举的业务含义不可推断，交由上层转换成完整性 503。
        raise ValueError("output safety enum invalid")
    return value


# 可空字段仍必须在非空时经过同一封闭白名单，不能由 Pydantic 宽松转换。
def _output_safety_optional_enum(
    value: object,
    allowed: frozenset[str],
) -> str | None:
    """校验可空输出安全枚举，非空值仍必须属于对应冻结白名单。"""

    if value is None:
        return None
    return _output_safety_enum(value, allowed)


def _output_safety_count(value: object) -> int:
    """恢复非负 SQLite INTEGER，显式拒绝 bool、NULL 和文本数字。"""

    if type(value) is not int or value < 0:
        # bool 是 int 子类，因此必须用精确类型判断阻止隐式真假值进入计数。
        raise ValueError("output safety count invalid")
    return value


# 滑窗参数允许 NULL，但非空值必须复用严格 INTEGER 校验。
def _output_safety_optional_count(value: object) -> int | None:
    """恢复可空非负整数，并拒绝布尔值或文本数字的隐式转换。"""

    if value is None:
        return None
    return _output_safety_count(value)


def _output_safety_boolean(value: object) -> bool:
    """只接受 SQLite 0/1，避免其他非零整数被 Python 宽松转换为真。"""

    if type(value) is not int or value not in {0, 1}:
        # 只接受数据库冻结的 0/1，不能把 2 或文本转换成可冲正。
        raise ValueError("output safety boolean invalid")
    return bool(value)


def _output_safety_scan_id(value: object) -> str:
    """验证公开 scan ID 的固定前缀和 32 位小写十六进制主体。"""

    if not isinstance(value, str) or not value.startswith("oss_") or len(value) != 36:
        # 格式异常的对象身份不得进入 URL、DOM 或财务投影。
        raise ValueError("output safety scan id invalid")
    if any(character not in "0123456789abcdef" for character in value[4:]):
        # 仅接受小写十六进制，保持迁移和公开合同完全一致。
        raise ValueError("output safety scan id invalid")
    return value


def _output_safety_optional_identifier(value: object) -> str | None:
    """验证内部关联 ID 是非空、无控制字符的有界文本。"""

    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 160:
        # 关联 ID 必须有界，防止损坏大字段扩散到响应或日志。
        raise ValueError("output safety identifier invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        # 控制字符可能制造日志/界面边界混淆，读模型必须拒绝。
        raise ValueError("output safety identifier invalid")
    return value


def _output_safety_fingerprint(value: object) -> str | None:
    """校验不可逆规则集指纹；详情只返回指纹，不返回规则 pattern。"""

    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64:
        # 规则集指纹只允许固定 SHA-256 形状，不接受任意文本。
        raise ValueError("output safety fingerprint invalid")
    if any(character not in "0123456789abcdef" for character in value):
        # 非小写十六进制无法证明来自冻结规则快照。
        raise ValueError("output safety fingerprint invalid")
    return value


def _request_log_info(row: sqlite3.Row) -> RequestLogInfo:
    """把数据库行转换成列表/详情共用的响应对象。"""

    output_safety = _output_safety_summary_from_row(row)
    return RequestLogInfo(
        request_id=row["request_id"],
        trace_id=row["trace_id"],
        tenant_id=row["tenant_id"],
        virtual_key_id=row["virtual_key_id"],
        virtual_key_prefix=row["virtual_key_prefix"],
        environment_id=row["environment_id"],
        environment=row["environment"],
        environment_name=row["environment_name"],
        project_id=row["project_id"],
        project_name=row["project_name"],
        application_id=row["application_id"],
        application_name=row["application_name"],
        endpoint=row["endpoint"],
        request_model=row["request_model"],
        resolved_model=row["resolved_model"],
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        snapshot_version=_optional_int(row["snapshot_version"]),
        policy_snapshot_status=row["policy_snapshot_status"] or "disabled",
        policy_decision=row["policy_decision"],
        policy_reason_code=row["policy_reason_code"],
        policy_id=row["policy_id"],
        policy_action_type=row["policy_action_type"],
        policy_target_model_id=row["policy_target_model_id"],
        # 只把冻结布尔值和稳定 Policy ID 投影给控制面，不回读模板 DSL 或请求正文。
        policy_mask_applied=bool(row["policy_mask_applied"]),
        policy_mask_policy_id=row["policy_mask_policy_id"],
        status=row["status"],
        error_code=row["error_code"],
        terminal_stage=(
            str(row["terminal_stage"])
            if "terminal_stage" in row.keys()
            else _terminal_stage(str(row["status"]), row["error_code"])
        ),
        stream_session_id=row["stream_session_id"],
        stream_status=row["stream_status"],
        latency_ms=_optional_int(row["latency_ms"]),
        input_tokens=_int_value(row["input_tokens"]),
        output_tokens=_int_value(row["output_tokens"]),
        total_tokens=_int_value(row["total_tokens"]),
        estimated_cost_amount=_float_value(row["estimated_cost_amount"]),
        currency=row["currency"] or "CNY",
        price_schema_version=row["price_schema_version"],
        budget_status=row["budget_status"],
        budget_rule_id=row["budget_rule_id"],
        budget_scope_type=row["budget_scope_type"],
        budget_scope_id=row["budget_scope_id"],
        budget_action=row["budget_action"],
        budget_reason_code=row["budget_reason_code"],
        budget_current_cost_cny=_optional_float(row["budget_current_cost_cny"]),
        budget_limit_cny=_optional_float(row["budget_limit_cny"]),
        dlp_status=row["dlp_status"],
        dlp_rule_id=row["dlp_rule_id"],
        dlp_action=row["dlp_action"],
        dlp_match_count=_int_value(row["dlp_match_count"]),
        dlp_reason_code=row["dlp_reason_code"],
        fallback_triggered=bool(row["fallback_triggered"]),
        routing_attempt_count=_int_value(row["routing_attempt_count"]),
        final_routing_attempt_id=row["final_routing_attempt_id"],
        output_safety=output_safety,
        has_feedback=bool(row["has_feedback"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_optional_db_time(row["updated_at"]),
    )


def _terminal_stage(status: str, error_code: object) -> str:
    """按 terminal_stage_v1 推导可证明的完成/终止阶段。

    该 Python 回退只供未选择 SQL 表达式的详情查询使用；高级列表直接返回与 0045
    表达式索引逐字一致的 SQL 结果。无法由成功状态或冻结错误码证明时必须是 unknown。
    """

    code = str(error_code or "")
    if code in {"content_policy_violation", "gateway.output_safety_unavailable"}:
        return "output_safety"
    if status == "success":
        return "completed"
    exact = {
        "gateway.auth_missing": "authentication", "gateway.auth_invalid": "authentication",
        "gateway.rate_limit_exceeded": "rate_limit", "gateway.rate_limit_unavailable": "rate_limit",
        "gateway.budget_exhausted": "budget", "gateway.budget_unavailable": "budget",
        "gateway.dlp_blocked": "dlp", "gateway.dlp_unavailable": "dlp",
        "gateway.cache_affinity_unavailable": "keypool",
        "gateway.request_log_unavailable": "logs_and_webhooks",
    }
    if code in exact:
        return exact[code]
    if code.startswith("gateway.policy_"):
        return "policy"
    if code.startswith("gateway.upstream_") or code in {
        "gateway.execution_interrupted", "gateway.stream_not_supported",
        "gateway.stream_session_unavailable", "gateway.client_disconnected",
        "gateway.stream_processing_unavailable", "gateway.experiment_execution_timeout",
    }:
        return "provider"
    if code in {
        "gateway.model_missing", "gateway.messages_missing", "gateway.input_missing",
        "gateway.model_not_found", "gateway.model_alias_disabled", "gateway.model_disabled",
        "gateway.provider_not_found", "gateway.provider_unhealthy", "gateway.no_route_available",
        "gateway.routing_policy_invalid", "gateway.routing_snapshot_unavailable",
        "gateway.routing_snapshot_invalid", "gateway.routing_decision_unavailable",
        "gateway.routing_attempt_unavailable", "gateway.fallback_unavailable",
        "gateway.experiment_unavailable", "gateway.experiment_variant_unavailable",
    }:
        return "routing"
    return "unknown"


def _request_feedback_info(feedback: FeedbackRecord | None) -> RequestFeedbackInfo | None:
    """把 shared 安全反馈事实映射为调用日志详情 DTO。

    参数为 tenant-qualified 查询得到的可空反馈；返回页面可读字段。shared ``FeedbackRecord``
    已排除 tenant、actor ID、Provider、Virtual Key、指纹和事件 payload，本层仍显式逐字段
    构造，防止未来内部 DTO 增字段后被框架自动序列化到控制面。
    """

    if feedback is None:
        return None
    return RequestFeedbackInfo(
        request_feedback_id=feedback.request_feedback_id,
        request_id=feedback.source_request_id,
        model_id=feedback.model_id,
        rating=feedback.rating,
        accepted=feedback.accepted,
        tags=list(feedback.tags),
        actor_type=feedback.actor_type,
        status=feedback.status,
        created_at=feedback.created_at,
    )


def _usage_info(row: sqlite3.Row) -> RequestLogUsageInfo:
    """在 调用日志查询 内部复用 usage info 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    normalized_usage = _json_object(row["normalized_usage"])
    total_tokens = _int_value(row["total_tokens"]) or _int_value(normalized_usage.get("total_tokens"))
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if total_tokens <= 0:
        total_tokens = _int_value(row["input_tokens"]) + _int_value(row["output_tokens"])
    return RequestLogUsageInfo(
        environment_id=row["environment_id"],
        environment=row["environment_code"],
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
        normalized_usage=normalized_usage,
        routing_attempt_id=row["routing_attempt_id"],
        created_at=_parse_db_time(row["created_at"]),
    )


def _budget_evaluation_info(row: sqlite3.Row) -> RequestLogBudgetEvaluation:
    """把预算评估行转换成调用日志详情可直接返回的 DTO。"""

    return RequestLogBudgetEvaluation(
        budget_rule_id=row["budget_rule_id"],
        environment_id=row["environment_id"],
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        budget_date=row["budget_date"],
        spent_before_cny=_float_value(row["spent_before_cny"]),
        estimated_request_cost_cny=_float_value(row["estimated_request_cost_cny"]),
        projected_cost_cny=_float_value(row["projected_cost_cny"]),
        budget_cny=_float_value(row["budget_cny"]),
        threshold_percent=_int_value(row["threshold_percent"]),
        budget_status=row["budget_status"],
        action_taken=row["action_taken"],
        reason_code=row["reason_code"],
        created_at=_parse_db_time(row["created_at"]),
    )


def _dlp_match_info(row: sqlite3.Row) -> RequestLogDlpMatch:
    """把 DLP 命中行转换成调用日志详情 DTO，确保不包含任何敏感明文。"""

    return RequestLogDlpMatch(
        dlp_match_id=row["dlp_match_id"],
        dlp_rule_id=row["dlp_rule_id"],
        rule_name=row["rule_name"],
        rule_type=row["rule_type"],
        action=row["action"],
        message_index=_optional_int(row["message_index"]),
        match_text_hash=row["match_text_hash"],
        match_length=_int_value(row["match_length"]),
        created_at=_parse_db_time(row["created_at"]),
    )


def _diagnose_request_log_error(
    log: RequestLogInfo,
    routing_decision: RequestLogRoutingDecision | None,
) -> ErrorDiagnosisInfo | None:
    """为调用日志详情派生错误诊断。

    成功请求不展示诊断；失败请求优先使用 request_logs.error_code，路由阶段失败再使用 routing_decisions.error_code。
    """

    error_code = log.error_code or (routing_decision.error_code if routing_decision else None)
    # 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
    if not error_code:
        return None
    return diagnose_error(error_code)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面当前配置的数据库。"""

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _normalize_optional_status(value: str | None) -> str | None:
    """规范化 调用日志查询 的 normalize optional status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """

    if value is None or value == "":
        return None
    status = value.strip().lower()
    if status not in VALID_STATUSES:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("request_log.status_invalid")
    return status


def _normalize_optional_filter(value: str | None, field_name: str) -> str | None:
    """校验可选字符串过滤条件长度。"""

    if value is None or value == "":
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_FILTER_LENGTH:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("request_log.query_invalid")
    return normalized


def _normalize_optional_snapshot_version(value: str | None) -> int | None:
    """把可选快照版本限制为 SQLite 可表示的正整数。

    参数：value 来自未信任 query。返回：空值为 None，合法十进制为 int。约束：拒绝
    正负号、空白内嵌、前端 bool 文本和溢出，避免不同解析器对同一过滤条件产生歧义。
    """

    if value is None or value == "":
        return None
    normalized = value.strip()
    # 快照版本只接受纯 ASCII 十进制，拒绝 Unicode 数字和符号造成的解析器语义分歧。
    if not normalized.isascii() or not normalized.isdecimal():
        raise ApiError("request_log.query_invalid")
    parsed = int(normalized)
    # 查询边界与数据库快照版本合同保持一致，避免越界值进入租户日志检索条件。
    if parsed < 1 or parsed > MAX_SNAPSHOT_VERSION:
        raise ApiError("request_log.query_invalid")
    return parsed


def _normalize_required_request_id(value: str) -> str:
    """规范化 调用日志查询 的 normalize required request id 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """

    normalized = (value or "").strip()
    if not normalized or len(normalized) > MAX_FILTER_LENGTH:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("request_log.query_invalid")
    return normalized


def _normalize_limit(value: int) -> int:
    """限制分页大小，避免一次性扫描过多调用日志。"""

    try:
        limit = int(value)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (TypeError, ValueError) as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("request_log.query_invalid") from exc
    if limit < 1 or limit > MAX_LIMIT:
        raise ApiError("request_log.query_invalid")
    return limit


def _normalize_offset(value: int) -> int:
    """规范化 调用日志查询 的 normalize offset 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """

    try:
        offset = int(value)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (TypeError, ValueError) as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("request_log.query_invalid") from exc
    if offset < 0:
        raise ApiError("request_log.query_invalid")
    return offset


def _json_object(value: object) -> dict[str, Any]:
    """把数据库 JSON 文本安全解析成对象。"""

    try:
        parsed = json.loads(str(value or "{}"))
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_value(value: object) -> int:
    """把数据库数值安全转换成非负整数。"""

    try:
        parsed = int(value or 0)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _optional_int(value: object) -> int | None:
    """把可空数据库数值转换成 int 或 None。"""

    if value is None:
        return None
    return _int_value(value)


def _optional_float(value: object) -> float | None:
    """把可空数据库金额转换成 float 或 None。"""

    if value is None:
        return None
    return _float_value(value)


def _optional_cost_text(value: object) -> str | None:
    """安全返回 15 位 Decimal 文本，损坏值降级为空且绝不回显任意数据库字符串。"""

    if value is None:
        return None
    raw = str(value)
    whole, separator, fraction = raw.partition(".")
    if (
        separator != "."
        or not whole.isascii()
        or not fraction.isascii()
        or not whole.isdecimal()
        or not fraction.isdecimal()
        or len(fraction) != 15
        or len(raw) > 80
    ):
        return None
    return raw


def _optional_latency_text(value: object) -> str | None:
    """安全返回三位小数延迟文本，损坏值降级为空且不回显原始数据库内容。

    参数：不可变 Candidate 的 ``average_latency_ms`` 原值。返回：规范的非负三位小数
    或 ``None``。关键约束：不使用 float，避免页面展示值与实际排序快照发生精度漂移。
    """

    if value is None:
        return None
    raw = str(value)
    whole, separator, fraction = raw.partition(".")
    if (
        separator != "."
        or not whole.isascii()
        or not fraction.isascii()
        or not whole.isdecimal()
        or not fraction.isdecimal()
        or len(fraction) != 3
        or len(raw) > 80
    ):
        return None
    return raw


def _float_value(value: object) -> float:
    """把数据库金额字段转换成浮点数用于 JSON 响应。"""

    try:
        return float(value or 0)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except (TypeError, ValueError):
        return 0.0


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析 调用日志查询 的 parse optional db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """

    if not value:
        return None
    return _parse_db_time(value)


def _parse_db_time(value: object) -> datetime:
    """解析 调用日志查询 的 parse db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """

    raw = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
