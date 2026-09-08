"""PRD-054 请求解释聚合、完整性判定与访问审计服务。

服务先在一条显式只读事务中按 tenant 聚合固定上限证据，关闭连接后构造十阶段/十一区块
响应，再用独立短写事务保存不可变 RequestExplanation 与 pending Webhook source outbox。
任何读取或审计失败都不会返回已经聚合的敏感治理详情；网络投递由后台 worker 完成。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .contracts import SECTION_ORDER, STAGE_ORDER, validate_fact_values
from .models import (
    RequestExplanation,
    RequestExplanationDecisionStage,
    RequestExplanationFact,
    RequestExplanationResponse,
    RequestExplanationSection,
    RequestExplanationSummary,
)
from .projection import project_explanation
from .repository import (
    ExplanationSnapshot,
    connect_explanation_database,
    insert_request_explanation,
    load_explanation_snapshot,
)

# 公开 code/ID 必须保持受限 ASCII；显示标签允许常规 Unicode，但禁止控制字符和无界长度。
SAFE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
DECIMAL_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
HEX_PATTERN = re.compile(r"^[0-9a-f]+$")
RATE_LIMIT_ERRORS = frozenset(
    {"gateway.rate_limit_exceeded", "gateway.rate_limit_unavailable"}
)


@dataclass(frozen=True, slots=True)
class BuiltExplanation:
    """保存已验证响应和数据库访问事实需要的稳定完整性摘要。"""

    explanation: RequestExplanation
    section_status: dict[str, str]
    # 触发器按源行原始时间文本复核 TOCTOU 快照，不能用等值但不同格式的时间替换。
    source_created_at_text: str
    source_updated_at_text: str


def get_request_explanation(
    config: AppConfig,
    context: AuthContext,
    source_request_id: str,
    *,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> RequestExplanationResponse:
    """读取、审计并返回当前租户的一条请求解释。

    参数：可信配置/AuthContext、已通过 wire 校验的源 request ID、本次 operation 链路。
    返回：固定十阶段/十一区块响应。关键约束：read 和 audit 使用两条短连接；audit 提交
    失败时整体失败关闭，不返回已读详情，也不向通用 audit_logs 复制同义事实。
    """

    captured_at = _now(now)
    snapshot = _read_snapshot(
        config,
        context,
        source_request_id=source_request_id,
    )
    if snapshot is None:
        # tenant+request 锚点未命中时不做全局探测，跨租户与不存在保持同形 404。
        raise ApiError("request_explanation.not_found")
    explanation_id = f"rxe_{uuid4().hex}"
    event_id = f"rxevt_{uuid4().hex}"
    try:
        built = _build_explanation(
            snapshot,
            explanation_id=explanation_id,
            captured_at=captured_at,
        )
    # DTO 或事实映射失败说明持久证据不可信，不能把半成品解释返回给已授权用户。
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        # 持久事实损坏或 owner 图漂移不能被降级成空值，否则页面会把缺证据误当成功。
        raise ApiError("request_explanation.integrity_failed") from exc
    current_context = _persist_access_fact(
        config,
        context,
        built,
        event_id=event_id,
        operation_request_id=request_id,
        operation_trace_id=trace_id,
    )
    return RequestExplanationResponse(
        request_explanation=project_explanation(
            built.explanation, current_context.permission_codes,
        ),
        request_id=request_id,
        trace_id=trace_id,
    )


def _read_snapshot(
    config: AppConfig,
    context: AuthContext,
    *,
    source_request_id: str,
) -> ExplanationSnapshot | None:
    """在一条一致性读事务中加载快照，并在所有路径结束事务、关闭连接。"""

    try:
        connection = connect_explanation_database(config)
    except Exception as exc:
        # 数据库 URL 与驱动正文属于内部实现，统一折叠为可重试领域错误。
        raise ApiError("request_explanation.unavailable") from exc
    try:
        with ResourceWriteTransaction(
            connection, config, context, "request_explanation.read", read_only=True,
        ) as transaction:
            snapshot = load_explanation_snapshot(
                connection, tenant_id=context.user.tenant_id,
                source_request_id=source_request_id,
            )
            connection.rollback()
            transaction.begin()
            return snapshot
    # 读取锁、缺表或数据库损坏统一折叠，SQL 与文件路径不得进入 API 响应。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("request_explanation.unavailable") from exc
    finally:
        connection.close()


def _persist_access_fact(
    config: AppConfig,
    context: AuthContext,
    built: BuiltExplanation,
    *,
    event_id: str,
    operation_request_id: str,
    operation_trace_id: str,
) -> AuthContext:
    """把一次成功读取原子保存为不可变访问事实与 pending source outbox。"""

    try:
        connection = connect_explanation_database(config)
    # 访问事实连接失败时必须整体失败关闭，禁止先返回敏感解释再漏记审计。
    except Exception as exc:
        raise ApiError("request_explanation.audit_unavailable") from exc
    try:
        with ResourceWriteTransaction(
            connection, config, context, "request_explanation.read",
        ) as transaction:
            _insert_access_fact(
                connection, context, built, event_id=event_id,
                operation_request_id=operation_request_id,
                operation_trace_id=operation_trace_id,
            )
            transaction.commit()
            return transaction.context
    except sqlite3.IntegrityError as exc:
        raise ApiError("request_explanation.integrity_failed") from exc
    except sqlite3.Error as exc:
        raise ApiError("request_explanation.audit_unavailable") from exc
    finally:
        connection.close()


def _insert_access_fact(
    connection, context, built, *, event_id, operation_request_id, operation_trace_id,
):
    explanation = built.explanation
    workspace = context.session.session_id.startswith("wss_")
    insert_request_explanation(
        connection,
        request_explanation_id=explanation.request_explanation_id,
        event_id=event_id,
        tenant_id=context.user.tenant_id,
        source_request_id=explanation.source_request_id,
        source_trace_id=explanation.source_trace_id,
        actor_user_id=context.user.user_id,
        actor_session_id=None if workspace else context.session.session_id,
        workspace_session_id=context.session.session_id if workspace else None,
        source_status=explanation.summary.source_status,
        status=explanation.status,
        section_status=built.section_status,
        missing_sections=list(explanation.missing_sections),
        truncated_sections=list(explanation.truncated_sections),
        source_created_at=built.source_created_at_text,
        source_updated_at=built.source_updated_at_text,
        operation_request_id=operation_request_id,
        operation_trace_id=operation_trace_id,
        viewed_at=explanation.captured_at,
    )


def _build_explanation(
    snapshot: ExplanationSnapshot,
    *,
    explanation_id: str,
    captured_at: datetime,
) -> BuiltExplanation:
    """把安全数据库行转换成固定区块、决策链和完整性摘要。"""

    anchor = snapshot.anchor
    sections = _build_sections(snapshot)
    missing = [
        name
        for name in SECTION_ORDER
        if sections[name].evidence_state in {"not_recorded", "pending", "partial"}
        and not sections[name].truncated
    ]
    truncated = [name for name in SECTION_ORDER if sections[name].truncated]
    # missing/truncated 互斥：达到 N+1 的区块已经有事实，只用 truncated 表达部分完整。
    missing = [name for name in missing if name not in truncated]
    status = "complete" if not missing and not truncated else "partial"
    source_created_at_text = _validated_source_time_text(anchor["created_at"])
    source_updated_at_text = _validated_source_time_text(anchor["source_updated_at"])
    summary = RequestExplanationSummary(
        source_status=_required_source_status(anchor["status"]),
        error_code=_safe_code(anchor["error_code"]),
        request_model=_safe_identifier(anchor["request_model"]),
        resolved_model=_safe_identifier(anchor["resolved_model"]),
        provider_code=_safe_identifier(anchor["provider_code"]),
        environment_id=_safe_identifier(anchor["environment_id"]),
        environment_name=_safe_label(anchor["environment_name"]),
        project_id=_safe_identifier(anchor["project_id"]),
        project_name=_safe_label(anchor["project_name"]),
        application_id=_safe_identifier(anchor["application_id"]),
        application_name=_safe_label(anchor["application_name"]),
        snapshot_version=_optional_non_negative_int(anchor["snapshot_version"]),
        latency_ms=_optional_non_negative_int(anchor["latency_ms"]),
        input_tokens=_non_negative_int(anchor["input_tokens"]),
        output_tokens=_non_negative_int(anchor["output_tokens"]),
        total_tokens=_non_negative_int(anchor["total_tokens"]),
        estimated_cost_amount=_decimal(anchor["estimated_cost_amount_text"]),
        currency=_safe_code(anchor["currency"]) or "CNY",
        fallback_triggered=_bool(anchor["fallback_triggered"]),
        created_at=_parse_time(source_created_at_text),
        updated_at=_parse_time(source_updated_at_text),
    )
    explanation = RequestExplanation(
        request_explanation_id=explanation_id,
        source_request_id=_required_identifier(anchor["request_id"]),
        source_trace_id=_required_identifier(anchor["trace_id"]),
        status=status,
        evidence_version="request_explanation_v1",
        captured_at=captured_at,
        summary=summary,
        decision_chain=_build_decision_chain(snapshot, sections),
        authentication=sections["authentication"],
        rate_limit=sections["rate_limit"],
        budget=sections["budget"],
        dlp=sections["dlp"],
        policy=sections["policy"],
        routing=sections["routing"],
        keypool=sections["keypool"],
        provider_response=sections["provider_response"],
        usage_and_finops=sections["usage_and_finops"],
        logs=sections["logs"],
        webhooks=sections["webhooks"],
        missing_sections=missing,
        truncated_sections=truncated,
    )
    return BuiltExplanation(
        explanation=explanation,
        section_status={
            name: sections[name].evidence_state for name in SECTION_ORDER
        },
        source_created_at_text=source_created_at_text,
        source_updated_at_text=source_updated_at_text,
    )


def _build_sections(
    snapshot: ExplanationSnapshot,
) -> dict[str, RequestExplanationSection]:
    """按执行链构造十一个区块；禁止读取当前配置回算历史决定。"""

    anchor = snapshot.anchor
    authentication = _authentication_section(snapshot)
    rate_limit = _rate_limit_section(snapshot)
    budget = _budget_section(snapshot, rate_limit)
    dlp = _dlp_section(snapshot, rate_limit, budget)
    policy = _policy_section(snapshot, rate_limit, budget, dlp)
    routing = _routing_section(snapshot, rate_limit, budget, dlp, policy)
    keypool = _keypool_section(snapshot, routing)
    provider = _provider_section(snapshot, routing)
    usage = _usage_section(snapshot, provider)
    logs = _logs_section(snapshot)
    webhooks = _webhooks_section(snapshot)
    sections = {
        "authentication": authentication,
        "rate_limit": rate_limit,
        "budget": budget,
        "dlp": dlp,
        "policy": policy,
        "routing": routing,
        "keypool": keypool,
        "provider_response": provider,
        "usage_and_finops": usage,
        "logs": logs,
        "webhooks": webhooks,
    }
    # 固定区块顺序与源锚点是响应签名基础，任一漂移都必须在写访问事实前拒绝。
    if tuple(sections) != SECTION_ORDER or anchor["request_id"] is None:
        # 构造器顺序本身属于公开合同，漂移时在持久审计前失败关闭。
        raise ValueError("request_explanation.section_order_invalid")
    return sections


def _authentication_section(snapshot: ExplanationSnapshot) -> RequestExplanationSection:
    """构造鉴权/归属证据；claim 与 RequestLog 身份不一致时视为完整性故障。"""

    claim = snapshot.claim
    anchor = snapshot.anchor
    if claim is None:
        # legacy 请求没有鉴权 claim 时只标记缺证据，禁止用当前 Key 配置反推历史身份。
        return _section("not_recorded", "request_explanation.authentication_not_recorded")
    if (
        claim["request_id"] != anchor["request_id"]
        or claim["trace_id"] != anchor["trace_id"]
        or claim["virtual_key_id"] != anchor["virtual_key_id"]
    ):
        # claim 与日志的 request/trace/Key 任一不一致都表示 owner 图损坏，不能降级展示。
        raise ValueError("request_explanation.authentication_owner_mismatch")
    fact = _fact(
        "authentication",
        fact_id=_required_identifier(anchor["request_id"]),
        occurred_at=claim["claimed_at"],
        values={
            "virtual_key_id": _safe_identifier(anchor["virtual_key_id"]),
            "virtual_key_prefix": _safe_identifier(anchor["virtual_key_prefix"]),
            "environment_id": _safe_identifier(anchor["environment_id"]),
            "environment_name": _safe_label(anchor["environment_name"]),
            "project_id": _safe_identifier(anchor["project_id"]),
            "project_name": _safe_label(anchor["project_name"]),
            "application_id": _safe_identifier(anchor["application_id"]),
            "application_name": _safe_label(anchor["application_name"]),
            "snapshot_version": _optional_non_negative_int(claim["snapshot_version"]),
            "policy_snapshot_status": _safe_code(claim["policy_snapshot_status"]),
        },
    )
    # 只有同租户 claim 与 RequestLog 三个身份字段完全一致才确认鉴权证据。
    return _section("complete", "request_explanation.authentication_verified", [fact])


def _rate_limit_section(snapshot: ExplanationSnapshot) -> RequestExplanationSection:
    """仅从冻结错误或后续阶段存在性解释限流，不读取当前规则伪造历史快照。"""

    error_code = _safe_code(snapshot.anchor["error_code"])
    # 限流没有独立历史表，只允许使用冻结错误码或后续阶段事实，绝不读当前规则回算。
    if error_code in RATE_LIMIT_ERRORS:
        decision = "blocked" if error_code == "gateway.rate_limit_exceeded" else "failed"
        fact = _fact(
            "rate_limit",
            fact_id=None,
            occurred_at=snapshot.anchor["source_updated_at"],
            values={"decision": decision, "error_code": error_code},
        )
        return _section("complete", error_code, [fact])
    if _has_later_rate_limit_evidence(snapshot):
        fact = _fact(
            "rate_limit",
            fact_id=None,
            occurred_at=snapshot.anchor["created_at"],
            values={"decision": "passed", "error_code": None},
        )
        return _section(
            "complete",
            "request_explanation.rate_limit_passed_by_later_stage",
            [fact],
        )
    return _section("not_recorded", "request_explanation.rate_limit_not_recorded")


def _budget_section(
    snapshot: ExplanationSnapshot,
    rate_limit: RequestExplanationSection,
) -> RequestExplanationSection:
    """构造请求级预算评估与正式 scope 事件，金额保持数据库文本/整数证据。"""

    if _fact_decision(rate_limit) in {"blocked", "failed"}:
        return _section("not_applicable", "request_explanation.blocked_before_budget")
    facts: list[RequestExplanationFact] = []
    for row in snapshot.budget_evaluations.rows:
        facts.append(
            _fact(
                "budget_evaluation",
                fact_id=_required_identifier(row["budget_evaluation_id"]),
                occurred_at=row["created_at"],
                values={
                    "budget_rule_id": _safe_identifier(row["budget_rule_id"]),
                    "scope_type": _safe_code(row["scope_type"]),
                    "scope_id": _safe_identifier(row["scope_id"]),
                    "budget_date": _safe_code(row["budget_date"]),
                    "spent_before_cny": _decimal(row["spent_before_cny_text"]),
                    "estimated_request_cost_cny": _decimal(
                        row["estimated_request_cost_cny_text"]
                    ),
                    "projected_cost_cny": _decimal(row["projected_cost_cny_text"]),
                    "budget_cny": _decimal(row["budget_cny_text"]),
                    "threshold_percent": _optional_non_negative_int(
                        row["threshold_percent"]
                    ),
                    "budget_status": _safe_code(row["budget_status"]),
                    "action_taken": _safe_code(row["action_taken"]),
                    "reason_code": _safe_code(row["reason_code"]),
                },
            )
        )
    for row in snapshot.budget_events.rows:
        facts.append(
            _fact(
                "budget_scope_event",
                fact_id=_required_identifier(row["budget_scope_event_id"]),
                occurred_at=row["created_at"],
                values={
                    "event_id": _safe_identifier(row["event_id"]),
                    "budget_rule_id": _safe_identifier(row["budget_rule_id"]),
                    "scope_type": _safe_code(row["scope_type"]),
                    "scope_id": _safe_identifier(row["scope_id"]),
                    "action_taken": _safe_code(row["action_taken"]),
                    "posted_cost_micros": _integer_text(row["posted_cost_micros"]),
                    "reserved_cost_micros": _integer_text(row["reserved_cost_micros"]),
                    "estimated_request_cost_micros": _integer_text(
                        row["estimated_request_cost_micros"]
                    ),
                    "projected_cost_micros": _integer_text(row["projected_cost_micros"]),
                    "budget_micros": _integer_text(row["budget_micros"]),
                },
            )
        )
    truncated = snapshot.budget_evaluations.truncated or snapshot.budget_events.truncated
    if facts:
        return _section(
            "partial" if truncated else "complete",
            "request_explanation.budget_truncated" if truncated else "request_explanation.budget_recorded",
            facts,
            truncated=truncated,
        )
    if _has_after_budget_evidence(snapshot):
        return _section("complete", "request_explanation.budget_passed_by_later_stage")
    return _section("not_recorded", "request_explanation.budget_not_recorded")


def _dlp_section(
    snapshot: ExplanationSnapshot,
    rate_limit: RequestExplanationSection,
    budget: RequestExplanationSection,
) -> RequestExplanationSection:
    """构造 DLP 规则、动作、tenant-salted hash 与长度，不返回命中文本或 pattern。"""

    if _stopped(rate_limit) or _budget_blocked(snapshot):
        return _section("not_applicable", "request_explanation.blocked_before_dlp")
    facts = [
        _fact(
            "dlp_match",
            fact_id=_required_identifier(row["dlp_match_id"]),
            occurred_at=row["created_at"],
            values={
                "dlp_rule_id": _safe_identifier(row["dlp_rule_id"]),
                "rule_name": _safe_label(row["rule_name"]),
                "rule_type": _safe_code(row["rule_type"]),
                "action": _safe_code(row["action"]),
                "message_index": _optional_non_negative_int(row["message_index"]),
                "match_text_hash": _sha256_digest(row["match_text_hash"]),
                "match_length": _non_negative_int(row["match_length"]),
            },
        )
        for row in snapshot.dlp_matches.rows
    ]
    if facts:
        return _section(
            "partial" if snapshot.dlp_matches.truncated else "complete",
            (
                "request_explanation.dlp_truncated"
                if snapshot.dlp_matches.truncated
                else "request_explanation.dlp_recorded"
            ),
            facts,
            truncated=snapshot.dlp_matches.truncated,
        )
    dlp_status = _safe_code(snapshot.anchor["dlp_status"])
    if dlp_status in {"clean", "not_evaluated"} or _has_after_dlp_evidence(snapshot):
        return _section("complete", "request_explanation.dlp_clear_by_later_stage")
    return _section("not_recorded", "request_explanation.dlp_not_recorded")


def _policy_section(
    snapshot: ExplanationSnapshot,
    rate_limit: RequestExplanationSection,
    budget: RequestExplanationSection,
    dlp: RequestExplanationSection,
) -> RequestExplanationSection:
    """展示冻结 snapshot/Policy 决策，不回读当前 DSL、AST 或上下文值。"""

    if _stopped(rate_limit) or _budget_blocked(snapshot) or _dlp_blocked(snapshot):
        return _section("not_applicable", "request_explanation.blocked_before_policy")
    anchor = snapshot.anchor
    decision = snapshot.routing_decision
    has_fact = any(
        anchor[field] is not None
        for field in (
            "snapshot_version",
            "policy_snapshot_status",
            "policy_decision",
            "policy_id",
            "policy_mask_policy_id",
        )
    ) or decision is not None
    if not has_fact:
        return _section("not_recorded", "request_explanation.policy_not_recorded")
    fact = _fact(
        "policy_snapshot",
        # 只有 mask 命中且最终默认 allow 时没有决定性 Policy，事实身份回退到 modifier Policy。
        fact_id=_safe_identifier(anchor["policy_id"] or anchor["policy_mask_policy_id"]),
        occurred_at=anchor["created_at"],
        values={
            "snapshot_version": _optional_non_negative_int(anchor["snapshot_version"]),
            "snapshot_status": _safe_code(anchor["policy_snapshot_status"]),
            "policy_id": _safe_identifier(anchor["policy_id"]),
            "policy_action_type": _safe_code(anchor["policy_action_type"]),
            "policy_target_model_id": _safe_identifier(anchor["policy_target_model_id"]),
            "policy_decision": _safe_code(anchor["policy_decision"]),
            "policy_reason_code": _safe_code(anchor["policy_reason_code"]),
            # Explain 只披露是否完整应用及稳定 Policy ID，不返回命中位置、数量、正文或 hash。
            "policy_mask_applied": _bool(anchor["policy_mask_applied"]),
            "policy_mask_policy_id": _safe_identifier(anchor["policy_mask_policy_id"]),
            "routing_policy_id": (
                _safe_identifier(decision["routing_policy_id"])
                if decision is not None
                else None
            ),
            "routing_policy_version": (
                _optional_non_negative_int(decision["routing_policy_version"])
                if decision is not None
                else None
            ),
            "routing_mode": (
                _safe_code(decision["routing_mode"]) if decision is not None else None
            ),
        },
    )
    if anchor["policy_snapshot_status"] == "disabled":
        return _section("not_applicable", "request_explanation.policy_disabled", [fact])
    return _section("complete", "request_explanation.policy_recorded", [fact])


def _routing_section(
    snapshot: ExplanationSnapshot,
    rate_limit: RequestExplanationSection,
    budget: RequestExplanationSection,
    dlp: RequestExplanationSection,
    policy: RequestExplanationSection,
) -> RequestExplanationSection:
    """构造不可变路由决策和候选，不用当前 Provider/价格/质量状态重算。"""

    if (
        _stopped(rate_limit)
        or _budget_blocked(snapshot)
        or _dlp_blocked(snapshot)
        or _policy_blocked(snapshot)
    ):
        return _section("not_applicable", "request_explanation.blocked_before_routing")
    if snapshot.routing_decision is None:
        return _section("not_recorded", "request_explanation.routing_not_recorded")
    facts = [_routing_decision_fact(snapshot.routing_decision)]
    facts.extend(_routing_candidate_fact(row) for row in snapshot.candidates.rows)
    return _section(
        "partial" if snapshot.candidates.truncated else "complete",
        (
            "request_explanation.routing_truncated"
            if snapshot.candidates.truncated
            else "request_explanation.routing_recorded"
        ),
        facts,
        truncated=snapshot.candidates.truncated,
    )


def _keypool_section(
    snapshot: ExplanationSnapshot,
    routing: RequestExplanationSection,
) -> RequestExplanationSection:
    """展示候选 KeyPool 状态与安全 CacheAffinity/attempt 摘要，排除 Credential ID。"""

    if routing.evidence_state == "not_applicable":
        return _section("not_applicable", "request_explanation.routing_not_executed")
    facts: list[RequestExplanationFact] = [
        _routing_candidate_fact(row) for row in snapshot.candidates.rows
    ]
    for row in snapshot.cache_affinity.rows:
        facts.append(
            _fact(
                "cache_affinity",
                fact_id=_required_identifier(row["cache_affinity_selection_id"]),
                occurred_at=row["created_at"],
                values={
                    "attempt_sequence": _non_negative_int(row["attempt_sequence"]),
                    "routing_attempt_id": _safe_identifier(row["routing_attempt_id"]),
                    "target_kind": _safe_code(row["target_kind"]),
                    "target_generation": _optional_non_negative_int(row["target_generation"]),
                    "provider_id": _safe_identifier(row["provider_id"]),
                    "provider_code": _safe_code(row["provider_code_snapshot"]),
                    "endpoint_preference_applied": _bool(row["endpoint_preference_applied"]),
                    "endpoint_preference_state": _safe_code(
                        row["endpoint_preference_state"]
                    ),
                    "binding_state_before": _safe_code(row["binding_state_before"]),
                    "selection_reason": _safe_code(row["selection_reason"]),
                    "estimated_hit": _bool(row["estimated_hit"]),
                    "outcome_status": _safe_code(row["outcome_status"]),
                },
            )
        )
    facts.extend(_routing_attempt_fact(row) for row in snapshot.attempts.rows)
    truncated = (
        snapshot.candidates.truncated
        or snapshot.cache_affinity.truncated
        or snapshot.attempts.truncated
    )
    if facts:
        return _section(
            "partial" if truncated else "complete",
            "request_explanation.keypool_truncated" if truncated else "request_explanation.keypool_recorded",
            facts,
            truncated=truncated,
        )
    if _routing_failed(snapshot):
        return _section("not_applicable", "request_explanation.no_routable_candidate")
    return _section("not_recorded", "request_explanation.keypool_not_recorded")


def _provider_section(
    snapshot: ExplanationSnapshot,
    routing: RequestExplanationSection,
) -> RequestExplanationSection:
    """构造 Provider attempt 的归一错误/HTTP/延迟，不返回原始响应或错误正文。"""

    if routing.evidence_state == "not_applicable" or _routing_failed(snapshot):
        return _section("not_applicable", "request_explanation.provider_not_called")
    facts = [_routing_attempt_fact(row) for row in snapshot.attempts.rows]
    if not facts:
        return _section("not_recorded", "request_explanation.provider_not_recorded")
    last_status = str(snapshot.attempts.rows[-1]["status"])
    # started 是已记录但尚未收敛的异步终态；若同时达到 N+1，上限截断比 pending 更保守。
    evidence_state = (
        "partial"
        if snapshot.attempts.truncated
        else "pending" if last_status == "started" else "complete"
    )
    reason_code = (
        "request_explanation.provider_truncated"
        if snapshot.attempts.truncated
        else (
            "request_explanation.provider_pending"
            if last_status == "started"
            else "request_explanation.provider_recorded"
        )
    )
    return _section(
        evidence_state,
        reason_code,
        facts,
        truncated=snapshot.attempts.truncated,
    )


def _usage_section(
    snapshot: ExplanationSnapshot,
    provider: RequestExplanationSection,
) -> RequestExplanationSection:
    """构造兼容 Usage 与正式 FinOps 分录，只返回白名单金额/Token 证据。"""

    facts: list[RequestExplanationFact] = []
    for row in snapshot.usage.rows:
        facts.append(
            _fact(
                "usage",
                fact_id=_required_identifier(row["usage_ledger_id"]),
                occurred_at=row["created_at"],
                values={
                    "input_tokens": _non_negative_int(row["input_tokens"]),
                    "output_tokens": _non_negative_int(row["output_tokens"]),
                    "total_tokens": _non_negative_int(row["total_tokens"]),
                    "cache_tokens": _non_negative_int(row["cache_tokens"]),
                    "cost_amount": _decimal(row["cost_amount_text"]),
                    "cost_cny": _decimal(row["cost_cny_text"]),
                    "cost_usd": _decimal(row["cost_usd_text"]),
                    "currency": _safe_code(row["currency"]),
                    "price_schema_version": _safe_code(row["price_schema_version"]),
                    "price_snapshot_id": _safe_identifier(row["price_snapshot_id"]),
                    "estimated": _bool(row["estimated"]),
                    "usage_source": _safe_code(row["usage_source"]),
                    "routing_attempt_id": _safe_identifier(row["routing_attempt_id"]),
                },
            )
        )
    for row in snapshot.finops.rows:
        facts.append(
            _fact(
                "finops_entry",
                fact_id=_required_identifier(row["finops_ledger_entry_id"]),
                occurred_at=row["created_at"],
                values={
                    "ledger_sequence": _integer_text(row["ledger_sequence"]),
                    "event_id": _safe_identifier(row["event_id"]),
                    "entry_type": _safe_code(row["entry_type"]),
                    "direction": _safe_code(row["direction"]),
                    "amount_micros": _integer_text(row["amount_micros"]),
                    "currency": _safe_code(row["currency"]),
                    "normalized_cny_micros": _integer_text(row["normalized_cny_micros"]),
                    "cny_normalization_status": _safe_code(
                        row["cny_normalization_status"]
                    ),
                    "price_evidence_status": _safe_code(row["price_evidence_status"]),
                    "price_snapshot_source": _safe_code(row["price_snapshot_source"]),
                    "posting_source": _safe_code(row["posting_source"]),
                    "quota_type": _safe_code(row["quota_type"]),
                    "price_snapshot_id": _safe_identifier(row["price_snapshot_id"]),
                    "price_schema_version": _safe_code(row["price_schema_version"]),
                    "input_tokens": _non_negative_int(row["input_tokens"]),
                    "output_tokens": _non_negative_int(row["output_tokens"]),
                    "total_tokens": _non_negative_int(row["total_tokens"]),
                    "cache_tokens": _non_negative_int(row["cache_tokens"]),
                    "estimated": _bool(row["estimated"]),
                    "status": _safe_code(row["status"]),
                },
            )
        )
    truncated = snapshot.usage.truncated or snapshot.finops.truncated
    if facts:
        return _section(
            "partial" if truncated else "complete",
            "request_explanation.usage_truncated" if truncated else "request_explanation.usage_recorded",
            facts,
            truncated=truncated,
        )
    # 成功请求的 Usage/FinOps 可能异步到账；暂时缺失应标 pending 而非伪造零用量。
    if snapshot.anchor["status"] == "success" and provider.evidence_state != "not_applicable":
        # Usage/FinOps 是异步终态；成功响应后短暂缺失不能错误标成永久未记录。
        return _section("pending", "request_explanation.usage_pending")
    return _section("not_applicable", "request_explanation.usage_not_applicable")


def _logs_section(snapshot: ExplanationSnapshot) -> RequestExplanationSection:
    """构造 RequestLog 与可选 stream 终态，不扫描文件日志。"""

    anchor = snapshot.anchor
    facts = [
        _fact(
            "request_log",
            fact_id=_required_identifier(anchor["request_id"]),
            occurred_at=anchor["created_at"],
            values={
                "source_status": _required_source_status(anchor["status"]),
                "error_code": _safe_code(anchor["error_code"]),
                "stream_status": _safe_code(anchor["stream_status"]),
                "latency_ms": _optional_non_negative_int(anchor["latency_ms"]),
                "fallback_triggered": _bool(anchor["fallback_triggered"]),
                "routing_attempt_count": _non_negative_int(anchor["routing_attempt_count"]),
                "final_routing_attempt_id": _safe_identifier(
                    anchor["final_routing_attempt_id"]
                ),
            },
        )
    ]
    stream = snapshot.stream_session
    if stream is not None:
        facts.append(
            _fact(
                "stream_session",
                fact_id=_required_identifier(stream["stream_session_id"]),
                occurred_at=stream["started_at"],
                values={
                    "status": _safe_code(stream["status"]),
                    "inter_token_timeout_ms": _non_negative_int(
                        stream["inter_token_timeout_ms"]
                    ),
                    "chunk_count": _non_negative_int(stream["chunk_count"]),
                    "bytes_sent": _non_negative_int(stream["bytes_sent"]),
                    "first_chunk_at": _safe_time_text(stream["first_chunk_at"]),
                    "last_chunk_at": _safe_time_text(stream["last_chunk_at"]),
                    "error_code": _safe_code(stream["error_code"]),
                    "completed_at": _safe_time_text(stream["completed_at"]),
                },
            )
        )
    return _section("complete", "request_explanation.logs_recorded", facts)


def _webhooks_section(snapshot: ExplanationSnapshot) -> RequestExplanationSection:
    """构造 delivery/attempt 安全摘要，明确排除 URL、payload、签名和 error summary。"""

    facts: list[RequestExplanationFact] = []
    for row in snapshot.webhook_deliveries.rows:
        facts.append(
            _fact(
                "webhook_delivery",
                fact_id=_required_identifier(row["webhook_delivery_id"]),
                occurred_at=row["created_at"],
                values={
                    "event_id": _safe_identifier(row["event_id"]),
                    "event_type": _safe_code(row["event_type"]),
                    "webhook_endpoint_id": _safe_identifier(row["webhook_endpoint_id"]),
                    "endpoint_name": _safe_label(row["endpoint_name"]),
                    "status": _safe_code(row["status"]),
                    "attempt_count": _non_negative_int(row["attempt_count"]),
                    "max_attempts": _non_negative_int(row["max_attempts"]),
                    "http_status": _optional_non_negative_int(row["http_status"]),
                    "duration_ms": _optional_non_negative_int(row["duration_ms"]),
                    "error_code": _safe_code(row["error_code"]),
                    "next_retry_at": _safe_time_text(row["next_retry_at"]),
                    "delivery_kind": _safe_code(row["delivery_kind"]),
                    "replayed_from_delivery_id": _safe_identifier(
                        row["replayed_from_delivery_id"]
                    ),
                },
            )
        )
    for row in snapshot.webhook_attempts.rows:
        facts.append(
            _fact(
                "webhook_attempt",
                fact_id=_required_identifier(row["webhook_delivery_attempt_id"]),
                occurred_at=row["created_at"],
                values={
                    "webhook_delivery_id": _safe_identifier(row["webhook_delivery_id"]),
                    "webhook_endpoint_id": _safe_identifier(row["webhook_endpoint_id"]),
                    "attempt_no": _non_negative_int(row["attempt_no"]),
                    "status": _safe_code(row["status"]),
                    "http_status": _optional_non_negative_int(row["http_status"]),
                    "duration_ms": _optional_non_negative_int(row["duration_ms"]),
                    "error_code": _safe_code(row["error_code"]),
                },
            )
        )
    truncated = snapshot.webhook_deliveries.truncated or snapshot.webhook_attempts.truncated
    return _section(
        "partial" if truncated else "complete",
        (
            "request_explanation.webhooks_truncated"
            if truncated
            else (
                "request_explanation.webhooks_recorded"
                if facts
                else "request_explanation.webhooks_none"
            )
        ),
        facts,
        truncated=truncated,
    )


def _build_decision_chain(
    snapshot: ExplanationSnapshot,
    sections: dict[str, RequestExplanationSection],
) -> list[RequestExplanationDecisionStage]:
    """把十一区块压缩成固定顺序的十阶段业务状态与安全引用。"""

    stage_to_sections = {
        "authentication": ("authentication",),
        "rate_limit": ("rate_limit",),
        "budget": ("budget",),
        "dlp": ("dlp",),
        "policy": ("policy",),
        "routing": ("routing",),
        "keypool": ("keypool",),
        "provider": ("provider_response",),
        "usage_and_finops": ("usage_and_finops",),
        "logs_and_webhooks": ("logs", "webhooks"),
    }
    result: list[RequestExplanationDecisionStage] = []
    for stage_name in STAGE_ORDER:
        section_names = stage_to_sections[stage_name]
        selected = [sections[name] for name in section_names]
        evidence_state = _combined_evidence_state(selected)
        facts = [fact for section in selected for fact in section.facts]
        result.append(
            RequestExplanationDecisionStage(
                stage=stage_name,
                status=_stage_status(stage_name, snapshot, selected),
                evidence_state=evidence_state,
                reason_code=selected[0].reason_code,
                occurred_at=_stage_time(facts),
                evidence_refs=[fact.fact_id for fact in facts if fact.fact_id][:16],
            )
        )
    return result


def _stage_status(
    stage: str,
    snapshot: ExplanationSnapshot,
    sections: list[RequestExplanationSection],
) -> str:
    """根据冻结事实派生单阶段业务状态，不把证据缺失误报成 passed。"""

    state = _combined_evidence_state(sections)
    if state == "not_applicable":
        return "not_evaluated"
    if state == "not_recorded":
        return "not_recorded"
    if state == "pending":
        return "pending"
    if stage == "rate_limit":
        decision = _fact_decision(sections[0])
        return "blocked" if decision == "blocked" else "failed" if decision == "failed" else "passed"
    if stage == "budget" and _budget_blocked(snapshot):
        return "blocked"
    if stage == "dlp" and _dlp_blocked(snapshot):
        return "blocked"
    if stage == "policy" and _policy_blocked(snapshot):
        return "blocked"
    if stage == "routing" and _routing_failed(snapshot):
        return "failed"
    # Provider 阶段严格映射 routing_attempts 的真实冻结枚举；started 已由 pending 提前返回。
    if stage == "provider":
        attempts = snapshot.attempts.rows
        if attempts and str(attempts[-1]["status"]) == "failed":
            return "failed"
    if stage == "logs_and_webhooks" and snapshot.anchor["status"] == "failed":
        # 日志阶段本身已成功记录失败事实，业务状态仍表达源请求失败。
        return "failed"
    return "passed"


def _routing_decision_fact(row: sqlite3.Row) -> RequestExplanationFact:
    """映射一条不可变 RoutingDecision 白名单事实。"""

    return _fact(
        "routing_decision",
        fact_id=_required_identifier(row["routing_decision_id"]),
        occurred_at=row["created_at"],
        values={
            "decision_status": _safe_code(row["decision_status"]),
            "decision_reason": _safe_code(row["decision_reason"]),
            "requested_model": _safe_identifier(row["requested_model"]),
            "selected_model_id": _safe_identifier(row["selected_model_id"]),
            "selected_internal_model_id": _safe_identifier(
                row["selected_internal_model_id"]
            ),
            "provider_id": _safe_identifier(row["provider_id"]),
            "provider_code": _safe_code(row["provider_code"]),
            "error_code": _safe_code(row["error_code"]),
        },
    )


def _routing_candidate_fact(row: sqlite3.Row) -> RequestExplanationFact:
    """映射一条候选的健康、KeyPool、价格与评分分量，不返回当前 Credential。"""

    return _fact(
        "routing_candidate",
        fact_id=_required_identifier(row["routing_candidate_id"]),
        occurred_at=row["created_at"],
        values={
            "model_id": _safe_identifier(row["model_id"]),
            "provider_id": _safe_identifier(row["provider_id"]),
            "candidate_role": _safe_code(row["candidate_role"]),
            "configured_priority": _optional_non_negative_int(row["configured_priority"]),
            "rank": _optional_non_negative_int(row["rank"]),
            "status": _safe_code(row["status"]),
            "reason_code": _safe_code(row["reason_code"]),
            "health_state": _safe_code(row["health_state"]),
            "health_fresh": _bool(row["health_fresh"]),
            "keypool_state": _safe_code(row["keypool_state"]),
            "price_available": _bool(row["price_available"]),
            "price_source": _safe_code(row["price_source"]),
            "price_schema_version": _safe_code(row["price_schema_version"]),
            "currency": _safe_code(row["currency"]),
            "estimated_cost_amount": _decimal(row["estimated_cost_amount_text"]),
            "quality_component_bps": _optional_non_negative_int(row["quality_component_bps"]),
            "cost_component_bps": _optional_non_negative_int(row["cost_component_bps"]),
            "latency_component_bps": _optional_non_negative_int(row["latency_component_bps"]),
            "weighted_score_bps": _optional_non_negative_int(row["weighted_score_bps"]),
        },
    )


def _routing_attempt_fact(row: sqlite3.Row) -> RequestExplanationFact:
    """映射 Provider attempt 的归一终态，不返回原始 Provider 错误或响应。"""

    return _fact(
        "routing_attempt",
        fact_id=_required_identifier(row["routing_attempt_id"]),
        occurred_at=row["created_at"],
        values={
            "attempt_sequence": _non_negative_int(row["attempt_sequence"]),
            "attempt_role": _safe_code(row["attempt_role"]),
            "trigger_type": _safe_code(row["trigger_type"]),
            "trigger_error_code": _safe_code(row["trigger_error_code"]),
            "model_id": _safe_identifier(row["model_id"]),
            "internal_model_id": _safe_identifier(row["internal_model_id"]),
            "provider_id": _safe_identifier(row["provider_id"]),
            "provider_code": _safe_code(row["provider_code"]),
            "status": _safe_code(row["status"]),
            "error_code": _safe_code(row["error_code"]),
            "error_category": _safe_code(row["error_category"]),
            "upstream_http_status": _optional_non_negative_int(row["upstream_http_status"]),
            "retryable": _bool(row["retryable"]),
            "latency_ms": _optional_non_negative_int(row["latency_ms"]),
            "usage_status": _safe_code(row["usage_status"]),
            "estimated_cost_cny": _decimal(row["estimated_cost_cny_text"]),
        },
    )


def _fact(
    kind: str,
    *,
    fact_id: str | None,
    occurred_at: object,
    values: dict[str, Any],
) -> RequestExplanationFact:
    """用 exact-key 合同构造一条扁平事实，禁止任意数据库字典穿透。"""

    validate_fact_values(kind, values)
    return RequestExplanationFact(
        kind=kind,
        fact_id=fact_id,
        occurred_at=_parse_optional_time(occurred_at),
        values=values,
    )


def _section(
    evidence_state: str,
    reason_code: str,
    facts: list[RequestExplanationFact] | None = None,
    *,
    truncated: bool = False,
) -> RequestExplanationSection:
    """构造一个固定区块；reason 只接受内部 code，不携带异常正文。"""

    return RequestExplanationSection(
        evidence_state=evidence_state,
        reason_code=_required_code(reason_code),
        facts=facts or [],
        truncated=truncated,
    )


def _has_later_rate_limit_evidence(snapshot: ExplanationSnapshot) -> bool:
    """判断是否存在只有通过限流后才能到达的冻结后续事实。"""

    return bool(
        snapshot.budget_evaluations.rows
        or snapshot.budget_events.rows
        or snapshot.dlp_matches.rows
        or snapshot.routing_decision is not None
        or snapshot.attempts.rows
    )


def _has_after_budget_evidence(snapshot: ExplanationSnapshot) -> bool:
    """判断 DLP/路由/Provider 是否证明预算阶段已放行。"""

    return bool(
        snapshot.dlp_matches.rows
        or snapshot.anchor["dlp_status"] in {"clean", "matched", "blocked"}
        or snapshot.routing_decision is not None
        or snapshot.attempts.rows
    )


def _has_after_dlp_evidence(snapshot: ExplanationSnapshot) -> bool:
    """判断策略/路由/Provider 是否证明 DLP 阶段已完成。"""

    return bool(
        snapshot.anchor["policy_decision"] is not None
        or snapshot.routing_decision is not None
        or snapshot.attempts.rows
    )


def _budget_blocked(snapshot: ExplanationSnapshot) -> bool:
    """根据冻结日志或评估动作判断预算是否阻断。"""

    return snapshot.anchor["budget_action"] == "block" or any(
        row["action_taken"] == "block" for row in snapshot.budget_evaluations.rows
    )


def _dlp_blocked(snapshot: ExplanationSnapshot) -> bool:
    """根据冻结日志与 DLP match 动作判断是否阻断。"""

    return snapshot.anchor["dlp_status"] == "blocked" or any(
        row["action"] == "block" for row in snapshot.dlp_matches.rows
    )


def _policy_blocked(snapshot: ExplanationSnapshot) -> bool:
    """只依据 RequestLog 冻结 Policy 决策判断阻断，禁止读取当前策略回算历史。"""

    return snapshot.anchor["policy_decision"] == "deny"


def _routing_failed(snapshot: ExplanationSnapshot) -> bool:
    """只依据不可变 RoutingDecision 终态判断失败，避免当前路由配置污染历史。"""

    return (
        snapshot.routing_decision is not None
        and snapshot.routing_decision["decision_status"] == "failed"
    )


def _stopped(section: RequestExplanationSection) -> bool:
    """判断前一阶段是否以 blocked/failed 停止链路。"""

    return _fact_decision(section) in {"blocked", "failed"}


def _fact_decision(section: RequestExplanationSection) -> str | None:
    """从已验证 rate-limit 事实读取固定 decision，其他区块返回 None。"""

    for fact in section.facts:
        if fact.kind == "rate_limit":
            value = fact.values.get("decision")
            return str(value) if value is not None else None
    return None


def _combined_evidence_state(sections: list[RequestExplanationSection]) -> str:
    """按最保守顺序合并一个或两个区块的证据状态。"""

    order = ("partial", "not_recorded", "pending", "not_applicable", "complete")
    for candidate in order:
        if any(section.evidence_state == candidate for section in sections):
            return candidate
    # 固定状态集合之外的值表示服务构造器漂移，不能默认为 complete。
    raise ValueError("request_explanation.evidence_state_invalid")


def _stage_time(facts: list[RequestExplanationFact]) -> datetime | None:
    """返回阶段第一条稳定事实时间；无事实时保持 None，不用当前时刻伪造。"""

    return next((fact.occurred_at for fact in facts if fact.occurred_at is not None), None)


def _required_source_status(value: object) -> str:
    """恢复数据库冻结的 success/failed 源请求终态。"""

    if value not in {"success", "failed"}:
        # 源终态影响后续阶段适用性，未知状态不能被字符串转换后直接公开。
        raise ValueError("request_explanation.source_status_invalid")
    return str(value)


def _required_identifier(value: object) -> str:
    """恢复受限 ASCII 稳定 ID；损坏 ID 使解释失败关闭。"""

    result = _safe_identifier(value)
    if result is None:
        # 必填 ID 无效时禁止生成替代值，否则审计事实会绑定到错误对象。
        raise ValueError("request_explanation.identifier_invalid")
    return result


def _safe_identifier(value: object) -> str | None:
    """返回受限 ASCII ID 或 None，绝不回显控制字符/空白文本。"""

    if value is None:
        return None
    raw = str(value)
    return raw if SAFE_IDENTIFIER_PATTERN.fullmatch(raw) else None


def _required_code(value: object) -> str:
    """恢复固定内部 code；reason 无效时整体失败，不能显示任意数据库正文。"""

    result = _safe_code(value)
    if result is None:
        # 内部 reason code 不能携带任意数据库正文，无效时必须整体失败关闭。
        raise ValueError("request_explanation.code_invalid")
    return result


def _safe_code(value: object) -> str | None:
    """返回受限 ASCII 枚举或错误码，拒绝控制字符和任意上游错误正文。"""

    if value is None:
        return None
    raw = str(value)
    return raw if SAFE_CODE_PATTERN.fullmatch(raw) else None


def _safe_label(value: object) -> str | None:
    """返回当前租户显示标签；拒绝控制字符与超过 160 字符的任意正文。"""

    if value is None:
        return None
    raw = str(value)
    if not 1 <= len(raw) <= 160 or any(ord(character) < 32 for character in raw):
        return None
    return raw


def _decimal(value: object) -> str | None:
    """返回最长 80 位 canonical 非负十进制文本，不经 Python float 重算。"""

    if value is None:
        return None
    raw = str(value)
    if len(raw) > 80 or not DECIMAL_PATTERN.fullmatch(raw):
        return None
    return raw


def _integer_text(value: object) -> str | None:
    """把非负 SQLite 整数/文本恢复为 canonical 字符串，保护 JS 大整数精度。"""

    if value is None:
        return None
    raw = str(value)
    if not raw.isascii() or not raw.isdigit() or (len(raw) > 1 and raw.startswith("0")):
        # micros/序号必须保持 canonical 十进制文本，防止前导零或类型漂移改变签名。
        raise ValueError("request_explanation.integer_text_invalid")
    return raw


def _sha256_digest(value: object) -> str | None:
    """恢复 Gateway 持久化的 ``sha256:<64位小写十六进制>`` DLP 摘要。

    DLP 数据面从首版起就保留算法前缀，便于未来轮换摘要算法；Explain 读取端必须兼容
    这个既有格式，同时严格拒绝裸摘要、其他算法、命中文字和大小写漂移，避免把任意
    数据库文本误当作已脱敏证据返回。
    """

    if value is None:
        return None
    raw = str(value)
    algorithm, separator, digest = raw.partition(":")
    if (
        algorithm != "sha256"
        or separator != ":"
        or len(digest) != 64
        or not HEX_PATTERN.fullmatch(digest)
    ):
        # DLP 摘要格式异常可能意味着历史损坏或明文误写，必须失败关闭而非原样回显。
        raise ValueError("request_explanation.hash_invalid")
    return raw


def _non_negative_int(value: object) -> int:
    """恢复严格非负 SQLite integer，明确拒绝 bool、float 与负数。"""

    if type(value) is not int or value < 0:
        # Token/延迟等计数拒绝 bool、float 和负数，避免隐式转换掩盖持久数据损坏。
        raise ValueError("request_explanation.integer_invalid")
    return value


def _optional_non_negative_int(value: object) -> int | None:
    """恢复可空非负整数；非空时沿用严格类型检查而不执行隐式转换。"""

    return None if value is None else _non_negative_int(value)


def _bool(value: object) -> bool:
    """只接受 SQLite 0/1，拒绝其他整数被 Python bool 静默折叠。"""

    if value not in {0, 1, False, True}:
        # SQLite 布尔事实只接受 0/1，其他值不能被 Python truthiness 静默折叠。
        raise ValueError("request_explanation.boolean_invalid")
    return bool(value)


def _parse_time(value: object) -> datetime:
    """解析有时区数据库时间并统一为 UTC；naive/非法文本视为完整性错误。"""

    if value is None:
        # 必填时间缺失会破坏证据先后关系，禁止使用当前时刻填补。
        raise ValueError("request_explanation.time_invalid")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 解析失败的原始时间不进入响应或日志，只保留固定完整性错误。
    except ValueError as exc:
        raise ValueError("request_explanation.time_invalid") from exc
    if parsed.tzinfo is None:
        # naive 时间不能按服务器本地时区猜测，否则跨部署验收结果会漂移。
        raise ValueError("request_explanation.time_invalid")
    return parsed.astimezone(UTC)


def _validated_source_time_text(value: object) -> str:
    """校验源快照时间并保留原始数据库文本，供不可变审计触发器执行精确复核。"""

    if not isinstance(value, str):
        # 非文本时间不能参与数据库原值比较，禁止用隐式 str 转换掩盖持久化损坏。
        raise ValueError("request_explanation.time_invalid")
    _parse_time(value)
    # 响应仍使用解析后的 UTC datetime；这里只保留原文，避免 .000000Z 被重写成 Z。
    return value


def _parse_optional_time(value: object) -> datetime | None:
    """解析可空有时区数据库时间。"""

    return None if value is None else _parse_time(value)


def _safe_time_text(value: object) -> str | None:
    """把可空数据库时间输出为 canonical UTC 秒级文本。"""

    parsed = _parse_optional_time(value)
    return _to_db_time(parsed) if parsed is not None else None


def _now(value: datetime | None) -> datetime:
    """返回 UTC 秒级服务器时刻；测试可注入 aware datetime。"""

    current = value if value is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        # 访问事实时间必须可比较且可审计，naive 注入不能按本地时区猜测。
        raise ApiError("request_explanation.integrity_failed")
    return current.astimezone(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """编码 canonical UTC 秒级时间，保证访问事实、lease 与 Webhook 签名稳定。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚且不让第二个 SQLite 异常遮蔽稳定领域错误。"""

    try:
        connection.rollback()
    # 清理阶段回滚失败不能覆盖更早的读取、完整性或审计领域错误。
    except sqlite3.Error:
        return


__all__ = ["get_request_explanation"]
