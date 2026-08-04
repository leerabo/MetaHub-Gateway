"""PRD-054 请求解释的租户限定读取仓储与访问事实写入。

所有读取都复用调用方创建的一条 SQLite 连接和显式只读事务，确保十一区块来自同一
快照。集合查询统一使用稳定排序的 ``LIMIT + 1``，多出的一行只判断截断，不执行无界
COUNT。查询列采用逐字段白名单，明确排除 Prompt、Response、raw usage、Credential、
Webhook URL/payload/secret 和原始错误摘要。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig

# 每类明细上限与设计文档一致；CacheAffinity 没有 Excel 单独上限，采用与 Usage 相同的
# 16 条防御性上限，足以覆盖 8 次 Provider attempt 的正常链路。
CANDIDATE_LIMIT = 64
ATTEMPT_LIMIT = 8
CACHE_AFFINITY_LIMIT = 16
BUDGET_LIMIT = 16
DLP_LIMIT = 100
USAGE_LIMIT = 16
FINOPS_LIMIT = 32
WEBHOOK_DELIVERY_LIMIT = 100
WEBHOOK_ATTEMPT_LIMIT = 300


@dataclass(frozen=True, slots=True)
class BoundedRows:
    """保存有界查询结果及是否发现第 N+1 行。"""

    rows: tuple[sqlite3.Row, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ExplanationSnapshot:
    """一次 tenant-qualified 只读事务聚合出的全部安全数据库事实。"""

    anchor: sqlite3.Row
    claim: sqlite3.Row | None
    routing_decision: sqlite3.Row | None
    candidates: BoundedRows
    attempts: BoundedRows
    cache_affinity: BoundedRows
    budget_evaluations: BoundedRows
    budget_events: BoundedRows
    dlp_matches: BoundedRows
    usage: BoundedRows
    finops: BoundedRows
    stream_session: sqlite3.Row | None
    webhook_deliveries: BoundedRows
    webhook_attempts: BoundedRows


def connect_explanation_database(config: AppConfig) -> sqlite3.Connection:
    """创建请求解释专用连接；调用方必须在 ``finally`` 中显式关闭。"""

    return connect_database(config.database_url)


def load_explanation_snapshot(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    source_request_id: str,
) -> ExplanationSnapshot | None:
    """在调用方只读事务中聚合一条当前租户请求的有界证据快照。

    参数：已开启 ``BEGIN`` 的连接、可信 Session tenant 和 canonical source request ID。
    返回：不存在/跨租户时为 None，否则为完整快照。关键约束：函数不创建、提交或关闭
    连接，也不读取 feedback，避免额外连接破坏单快照语义。
    """

    anchor = connection.execute(
        """
        SELECT
          rl.request_id, rl.trace_id, rl.virtual_key_id,
          vk.key_prefix AS virtual_key_prefix,
          rl.environment_id, rl.environment_code,
          environment.name AS environment_name,
          rl.project_id, project.name AS project_name,
          rl.application_id, application.name AS application_name,
          rl.provider_id, rl.model_id, rl.request_model, rl.resolved_model,
          rl.provider_code, rl.status, rl.error_code,
          rl.stream_session_id, rl.stream_status,
          rl.snapshot_version, rl.policy_snapshot_status,
          rl.policy_decision, rl.policy_reason_code, rl.policy_id,
          rl.policy_action_type, rl.policy_target_model_id,
          rl.policy_mask_applied, rl.policy_mask_policy_id,
          rl.latency_ms, rl.input_tokens, rl.output_tokens, rl.total_tokens,
          CAST(rl.estimated_cost_amount AS TEXT) AS estimated_cost_amount_text,
          rl.currency, rl.price_schema_version,
          rl.budget_status, rl.budget_rule_id, rl.budget_scope_type,
          rl.budget_scope_id, rl.budget_action, rl.budget_reason_code,
          CAST(rl.budget_current_cost_cny AS TEXT) AS budget_current_cost_cny_text,
          CAST(rl.budget_limit_cny AS TEXT) AS budget_limit_cny_text,
          rl.dlp_status, rl.dlp_rule_id, rl.dlp_action,
          rl.dlp_match_count, rl.dlp_reason_code,
          rl.fallback_triggered, rl.routing_attempt_count,
          rl.final_routing_attempt_id, rl.created_at,
          coalesce(rl.updated_at, rl.created_at) AS source_updated_at
        FROM request_logs rl
        LEFT JOIN virtual_keys vk
          ON vk.tenant_id=rl.tenant_id AND vk.virtual_key_id=rl.virtual_key_id
        LEFT JOIN environments environment
          ON environment.tenant_id=rl.tenant_id
         AND environment.environment_id=rl.environment_id
        LEFT JOIN projects project
          ON project.tenant_id=rl.tenant_id AND project.project_id=rl.project_id
        LEFT JOIN applications application
          ON application.tenant_id=rl.tenant_id
         AND application.application_id=rl.application_id
        WHERE rl.tenant_id=? AND rl.request_id=?
        """,
        (tenant_id, source_request_id),
    ).fetchone()
    if anchor is None:
        # tenant 条件与 request 条件同时出现，使不存在和跨租户 ID 得到同形结果。
        return None

    claim = connection.execute(
        """
        SELECT request_id, trace_id, virtual_key_id, snapshot_version,
               policy_snapshot_status, claimed_at
        FROM gateway_request_claims
        WHERE tenant_id=? AND request_id=?
        LIMIT 1
        """,
        (tenant_id, source_request_id),
    ).fetchone()
    routing_decision = connection.execute(
        """
        SELECT
          routing_decision_id, decision_status, decision_reason,
          requested_model, selected_model_id, selected_internal_model_id,
          provider_id, provider_code, error_code, routing_policy_id,
          routing_policy_version, routing_mode, created_at
        FROM routing_decisions
        WHERE tenant_id=? AND request_id=?
        ORDER BY
          CASE WHEN routing_policy_id IS NOT NULL THEN 0 ELSE 1 END,
          created_at DESC, routing_decision_id DESC
        LIMIT 1
        """,
        (tenant_id, source_request_id),
    ).fetchone()
    decision_id = (
        str(routing_decision["routing_decision_id"])
        if routing_decision is not None
        else None
    )
    candidates = _bounded_query(
        connection,
        """
        SELECT
          routing_candidate_id, model_id, provider_id, candidate_role,
          configured_priority, rank, status, reason_code, health_state,
          health_fresh, keypool_state, price_available, price_source,
          price_schema_version, currency,
          CAST(estimated_cost_amount AS TEXT) AS estimated_cost_amount_text,
          quality_component_bps, cost_component_bps, latency_component_bps,
          weighted_score_bps, created_at
        FROM routing_candidates
        WHERE tenant_id=? AND routing_decision_id=?
        ORDER BY rank ASC, configured_priority ASC, routing_candidate_id ASC
        LIMIT ?
        """,
        (tenant_id, decision_id or ""),
        CANDIDATE_LIMIT,
    )
    attempts = _bounded_query(
        connection,
        """
        SELECT
          routing_attempt_id, attempt_sequence, attempt_role, trigger_type,
          trigger_error_code, model_id, internal_model_id, provider_id,
          provider_code, status, error_code, error_category,
          upstream_http_status, retryable, latency_ms, usage_status,
          CAST(estimated_cost_cny AS TEXT) AS estimated_cost_cny_text,
          created_at, completed_at
        FROM routing_attempts
        WHERE tenant_id=? AND request_id=?
        ORDER BY attempt_sequence ASC, created_at ASC, routing_attempt_id ASC
        LIMIT ?
        """,
        (tenant_id, source_request_id),
        ATTEMPT_LIMIT,
    )
    cache_affinity = _bounded_query(
        connection,
        """
        SELECT
          cache_affinity_selection_id, attempt_sequence, routing_attempt_id,
          target_kind, target_generation, provider_id, provider_code_snapshot,
          endpoint_preference_applied, endpoint_preference_state,
          binding_state_before, selection_reason, estimated_hit,
          outcome_status, created_at, outcome_at
        FROM cache_affinity_selections
        WHERE tenant_id=? AND request_id=?
        ORDER BY attempt_sequence ASC, created_at ASC, cache_affinity_selection_id ASC
        LIMIT ?
        """,
        (tenant_id, source_request_id),
        CACHE_AFFINITY_LIMIT,
    )
    budget_evaluations = _bounded_query(
        connection,
        """
        SELECT
          budget_evaluation_id, budget_rule_id, scope_type, scope_id,
          budget_date,
          CAST(spent_before_cny AS TEXT) AS spent_before_cny_text,
          CAST(estimated_request_cost_cny AS TEXT) AS estimated_request_cost_cny_text,
          CAST(projected_cost_cny AS TEXT) AS projected_cost_cny_text,
          CAST(budget_cny AS TEXT) AS budget_cny_text,
          threshold_percent, budget_status, action_taken, reason_code, created_at
        FROM budget_evaluations
        WHERE tenant_id=? AND request_id=?
        ORDER BY created_at ASC, budget_evaluation_id ASC
        LIMIT ?
        """,
        (tenant_id, source_request_id),
        BUDGET_LIMIT,
    )
    budget_events = _bounded_query(
        connection,
        """
        SELECT
          budget_scope_event_id, event_id, budget_rule_id, scope_type, scope_id,
          action_taken, posted_cost_micros, reserved_cost_micros,
          estimated_request_cost_micros, projected_cost_micros,
          budget_micros, created_at
        FROM budget_scope_events
        WHERE tenant_id=? AND request_id=?
        ORDER BY created_at ASC, budget_scope_event_id ASC
        LIMIT ?
        """,
        (tenant_id, source_request_id),
        BUDGET_LIMIT,
    )
    dlp_matches = _bounded_query(
        connection,
        """
        SELECT
          dlp_match_id, dlp_rule_id, rule_name, rule_type, action,
          message_index, match_text_hash, match_length, created_at
        FROM dlp_matches
        WHERE tenant_id=? AND request_id=?
        ORDER BY created_at ASC, dlp_match_id ASC
        LIMIT ?
        """,
        (tenant_id, source_request_id),
        DLP_LIMIT,
    )
    usage = _bounded_query(
        connection,
        """
        SELECT
          usage_ledger_id, input_tokens, output_tokens, total_tokens,
          cache_tokens, CAST(cost_amount AS TEXT) AS cost_amount_text,
          CAST(cost_cny AS TEXT) AS cost_cny_text,
          CAST(cost_usd AS TEXT) AS cost_usd_text, currency,
          price_schema_version, price_snapshot_id, estimated, usage_source,
          routing_attempt_id, created_at
        FROM usage_ledger
        WHERE tenant_id=? AND request_id=?
        ORDER BY created_at ASC, usage_ledger_id ASC
        LIMIT ?
        """,
        (tenant_id, source_request_id),
        USAGE_LIMIT,
    )
    finops = _bounded_query(
        connection,
        """
        SELECT
          finops_ledger_entry_id, ledger_sequence, event_id, entry_type,
          direction, amount_micros, currency, normalized_cny_micros,
          cny_normalization_status, price_evidence_status,
          price_snapshot_source, posting_source, quota_type,
          price_snapshot_id, price_schema_version, input_tokens,
          output_tokens, total_tokens, cache_tokens, estimated, status, created_at
        FROM finops_ledger_entries
        WHERE tenant_id=? AND source_request_id=?
        ORDER BY created_at ASC, ledger_sequence ASC, finops_ledger_entry_id ASC
        LIMIT ?
        """,
        (tenant_id, source_request_id),
        FINOPS_LIMIT,
    )
    stream_session = connection.execute(
        """
        SELECT
          stream_session_id, status, inter_token_timeout_ms, chunk_count,
          bytes_sent, first_chunk_at, last_chunk_at, error_code,
          started_at, completed_at
        FROM stream_sessions
        WHERE tenant_id=? AND request_id=? AND stream_session_id=?
        LIMIT 1
        """,
        (tenant_id, source_request_id, anchor["stream_session_id"] or ""),
    ).fetchone()
    webhook_deliveries = _bounded_query(
        connection,
        """
        SELECT
          link.request_webhook_delivery_link_id,
          delivery.webhook_delivery_id, delivery.event_id, delivery.event_type,
          delivery.webhook_endpoint_id, endpoint.name AS endpoint_name,
          delivery.status, delivery.attempt_count, delivery.max_attempts,
          delivery.http_status, delivery.duration_ms, delivery.error_code,
          delivery.next_retry_at, delivery.delivery_kind,
          delivery.replayed_from_delivery_id, delivery.created_at
        FROM request_webhook_delivery_links link
        JOIN webhook_deliveries delivery
          ON delivery.tenant_id=link.tenant_id
         AND delivery.webhook_delivery_id=link.webhook_delivery_id
        JOIN webhook_endpoints endpoint
          ON endpoint.tenant_id=delivery.tenant_id
         AND endpoint.webhook_endpoint_id=delivery.webhook_endpoint_id
        WHERE link.tenant_id=? AND link.source_request_id=?
        ORDER BY link.created_at ASC,
                 link.request_webhook_delivery_link_id ASC
        LIMIT ?
        """,
        (tenant_id, source_request_id),
        WEBHOOK_DELIVERY_LIMIT,
    )
    webhook_attempts = _bounded_query(
        connection,
        """
        SELECT
          attempt.webhook_delivery_attempt_id,
          attempt.webhook_delivery_id, attempt.webhook_endpoint_id,
          attempt.attempt_no, attempt.status, attempt.http_status,
          attempt.duration_ms, attempt.error_code, attempt.created_at
        FROM request_webhook_delivery_links link
        JOIN webhook_delivery_attempts attempt
          ON attempt.tenant_id=link.tenant_id
         AND attempt.webhook_delivery_id=link.webhook_delivery_id
        WHERE link.tenant_id=? AND link.source_request_id=?
        ORDER BY attempt.created_at ASC, attempt.attempt_no ASC,
                 attempt.webhook_delivery_attempt_id ASC
        LIMIT ?
        """,
        (tenant_id, source_request_id),
        WEBHOOK_ATTEMPT_LIMIT,
    )
    return ExplanationSnapshot(
        anchor=anchor,
        claim=claim,
        routing_decision=routing_decision,
        candidates=candidates,
        attempts=attempts,
        cache_affinity=cache_affinity,
        budget_evaluations=budget_evaluations,
        budget_events=budget_events,
        dlp_matches=dlp_matches,
        usage=usage,
        finops=finops,
        stream_session=stream_session,
        webhook_deliveries=webhook_deliveries,
        webhook_attempts=webhook_attempts,
    )


def insert_request_explanation(
    connection: sqlite3.Connection,
    *,
    request_explanation_id: str,
    event_id: str,
    tenant_id: str,
    source_request_id: str,
    source_trace_id: str,
    actor_user_id: str,
    actor_session_id: str,
    source_status: str,
    status: str,
    section_status: dict[str, str],
    missing_sections: list[str],
    truncated_sections: list[str],
    source_created_at: str,
    source_updated_at: str,
    operation_request_id: str,
    operation_trace_id: str,
    viewed_at: datetime,
) -> None:
    """在调用方短写事务中插入不可变访问事实和 pending source outbox。

    tenant、actor、session、source owner 仍由 0044 触发器在数据库内二次校验。集合状态
    使用稳定 JSON 序列化；函数不提交事务，便于调用方在任何失败时原子回滚。
    """

    timestamp = _to_db_time(viewed_at)
    connection.execute(
        """
        INSERT INTO request_explanations(
          request_explanation_id, event_id, tenant_id,
          source_request_id, source_trace_id, actor_user_id, actor_session_id,
          source_status, status, evidence_version,
          section_status_json, missing_sections_json, truncated_sections_json,
          source_created_at, source_updated_at,
          operation_request_id, operation_trace_id,
          event_dispatch_status, event_dispatch_attempts, event_next_attempt_at,
          viewed_at, created_at, updated_at
        ) VALUES(
          ?, ?, ?, ?, ?, ?, ?, ?, ?, 'request_explanation_v1',
          ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?
        )
        """,
        (
            request_explanation_id,
            event_id,
            tenant_id,
            source_request_id,
            source_trace_id,
            actor_user_id,
            actor_session_id,
            source_status,
            status,
            json.dumps(section_status, ensure_ascii=True, separators=(",", ":")),
            json.dumps(missing_sections, ensure_ascii=True, separators=(",", ":")),
            json.dumps(truncated_sections, ensure_ascii=True, separators=(",", ":")),
            source_created_at,
            source_updated_at,
            operation_request_id,
            operation_trace_id,
            timestamp,
            timestamp,
            timestamp,
            timestamp,
        ),
    )


def _bounded_query(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...],
    limit: int,
) -> BoundedRows:
    """执行固定 SQL 的 LIMIT+1 查询并裁去哨兵行。

    参数：连接、代码内固定 SQL、绑定值和正整数上限。返回：最多 N 行与截断标志。
    SQL 结构不接收业务输入，最后一个 ``?`` 始终绑定 ``limit + 1``。
    """

    rows = connection.execute(sql, (*parameters, limit + 1)).fetchall()
    return BoundedRows(rows=tuple(rows[:limit]), truncated=len(rows) > limit)


def _to_db_time(value: datetime) -> str:
    """把已校验的 UTC aware 时间编码成数据库秒级 RFC3339。"""

    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "BoundedRows",
    "ExplanationSnapshot",
    "connect_explanation_database",
    "insert_request_explanation",
    "load_explanation_snapshot",
]
