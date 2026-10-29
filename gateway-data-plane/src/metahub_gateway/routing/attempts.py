"""非流式路由尝试的调用前执行门和终态审计。

本模块只持久化租户、请求链路、模型、Provider、固定错误码和成本估算，禁止接收
Prompt、Response、Authorization、Provider Key 或上游原始异常正文。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_gateway.error_maps.facts import insert_provider_error_fact
from metahub_gateway.providers.base import (
    NormalizedProviderError,
    is_quality_observation_eligible_error,
)
from metahub_gateway.quality import record_model_quality_observation

from .models import GatewayRoute, GatewayRoutePlan

logger = logging.getLogger("metahub.gateway.routing_attempts")


@dataclass(frozen=True)
class RoutingAttempt:
    """一次已通过数据库执行门、允许发送到上游的调用事实。"""

    routing_attempt_id: str
    tenant_id: str
    request_id: str
    trace_id: str
    attempt_sequence: int
    attempt_role: str
    route: GatewayRoute


def create_routing_attempt(
    config: AppConfig,
    plan: GatewayRoutePlan,
    route: GatewayRoute,
    *,
    request_id: str,
    trace_id: str,
    attempt_sequence: int,
    attempt_role: str,
    trigger_error_code: str | None = None,
    estimated_cost_cny: Decimal | None = None,
) -> RoutingAttempt:
    """在上游调用前原子插入 started 行，并把唯一键作为重试风暴闸门。

    sequence 只能是 1..32；同 tenant/request/sequence 已存在时绝不再次调用上游。
    初始和备用执行门使用不同统一错误码，便于客户端判断失败发生在哪个阶段。
    """

    # 状态机硬限制最多 32 次调用及固定角色，阻止调用方错误循环产生无限上游请求。
    if not 1 <= attempt_sequence <= 32 or attempt_role not in {"primary", "fallback"}:
        # 审计闸门校验失败时必须先于网络调用终止，避免出现无 attempt 的隐形流量。
        raise ApiError("gateway.routing_attempt_unavailable")
    attempt_id = f"rta_{uuid4().hex}"
    now = _to_db_time(_utcnow())
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO routing_attempts(
              routing_attempt_id, tenant_id, request_id, trace_id, model_alias_id,
              fallback_policy_id, fallback_policy_version, attempt_sequence, attempt_role,
              trigger_type, trigger_error_code, model_id, internal_model_id, provider_id,
              provider_code, status, retryable, usage_status, estimated_cost_cny,
              provider_call_status,
              created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', 0, 'pending', ?, ?, ?, ?)
            """,
            (
                attempt_id,
                route.tenant_id,
                request_id,
                trace_id,
                route.model_alias_id,
                plan.fallback_policy_id,
                plan.fallback_policy_version,
                attempt_sequence,
                attempt_role,
                "fallback" if attempt_sequence > 1 else "initial",
                trigger_error_code,
                route.model_id,
                route.model_alias,
                route.provider_id,
                route.provider_code,
                str(estimated_cost_cny) if estimated_cost_cny is not None else None,
                "not_called" if route.provider_type == "local_endpoint" else "not_applicable",
                now,
                now,
            ),
        )
        connection.commit()
    # 审计闸门写入失败时执行 fail-closed：不允许在无法追踪的情况下调用任何 Provider。
    except (DbError, sqlite3.Error) as exc:
        if connection is not None:
            connection.rollback()
        _log_attempt_gate_failure(route, request_id, trace_id, attempt_sequence)
        code = (
            "gateway.fallback_unavailable"
            if attempt_sequence > 1
            else "gateway.routing_attempt_unavailable"
        )
        # 后续闸门失败使用独立错误码，便于区分首调用未开始和备用调用未开始。
        raise ApiError(code) from exc
    finally:
        if connection is not None:
            connection.close()
    return RoutingAttempt(
        routing_attempt_id=attempt_id,
        tenant_id=route.tenant_id,
        request_id=request_id,
        trace_id=trace_id,
        attempt_sequence=attempt_sequence,
        attempt_role=attempt_role,
        route=route,
    )


def mark_local_provider_call_started(
    database_url: str | None,
    *,
    tenant_id: str,
    routing_attempt_id: str | None,
) -> None:
    """Atomically cross the local Provider send fence after network preflight.

    The caller must invoke this after DNS/ACL validation and before opening the connection.
    A missing or stale attempt fails closed so no untracked request reaches a local Endpoint.
    """

    if not database_url or not routing_attempt_id:
        raise ApiError("gateway.routing_attempt_unavailable")
    now = _to_db_time(_utcnow())
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(database_url)
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE routing_attempts
            SET provider_call_status='called', provider_call_started_at=?, updated_at=?
            WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
              AND provider_call_status='not_called'
            """,
            (now, now, tenant_id, routing_attempt_id),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("local_provider_call_fence_conflict")
        connection.commit()
    except (DbError, sqlite3.Error) as exc:
        if connection is not None:
            connection.rollback()
        logger.warning(
            "route.local_provider_call_fence_failed",
            extra={
                "event": "route.local_provider_call_fence_failed",
                "tenant_id": tenant_id,
                "object_id": routing_attempt_id,
            },
        )
        raise ApiError("gateway.routing_attempt_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()


def safe_complete_routing_attempt(
    config: AppConfig,
    attempt: RoutingAttempt,
    *,
    succeeded: bool,
    latency_ms: int,
    error: NormalizedProviderError | None = None,
    usage_status: str | None = None,
    usage_exposure_possible: bool = False,
    quality_observation_eligible: bool,
    record_quality_observation: bool = True,
) -> None:
    """尽力把 started 更新为唯一终态，不让日志故障覆盖已经取得的模型响应。

    失败尝试若可能已经到达 Provider 但没有可信 usage，明确写 unavailable 而不是 0；
    本地失败或围栏前阻断写 not_applicable。成功尝试先保持 pending，Usage Ledger 同事务
    写入后再改为 recorded。用量暴露可能性与模型质量资格是两个独立维度。
    """

    if (
        type(quality_observation_eligible) is not bool
        or type(record_quality_observation) is not bool
        or type(usage_exposure_possible) is not bool
    ):
        # 内部调用必须显式给出布尔资格，truthy 对象不能决定是否处罚模型质量。
        raise ValueError("quality_observation_eligibility_invalid")
    if type(succeeded) is not bool or type(latency_ms) is not int:
        # float/bool 延迟或 truthy 成功标记会让 SQLite 源事实无法按同一口径重建。
        raise ValueError("routing_attempt_quality_contract_invalid")
    status = "succeeded" if succeeded else "failed"
    final_usage_status = usage_status or ("pending" if succeeded else "unavailable")
    # 非法用量状态不能被宽松改写，否则调用方可能借 unavailable 错误纳入质量样本。
    if final_usage_status not in {"pending", "recorded", "unavailable", "not_applicable"}:
        raise ValueError("routing_attempt_quality_contract_invalid")
    expected_quality_eligible = succeeded or is_quality_observation_eligible_error(error)
    expected_failure_usage_status = (
        "unavailable"
        if expected_quality_eligible or usage_exposure_possible
        else "not_applicable"
    )
    valid_usage_status = (
        succeeded and error is None and final_usage_status in {"pending", "recorded"}
    ) or (
        not succeeded
        and error is not None
        and final_usage_status == expected_failure_usage_status
    )
    # 显式资格必须与错误白名单和持久 usage_status 完全一致，防止 caller 制造 down/up 漂移。
    if not valid_usage_status or quality_observation_eligible is not expected_quality_eligible:
        raise ValueError("routing_attempt_quality_contract_invalid")
    # attempt 终态与 Score 观察必须共享同一个时刻，否则跨秒事务无法由 completed_at 重建。
    completed_at = _utcnow()
    now = _to_db_time(completed_at)
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE routing_attempts
            SET status=?, error_code=?, error_category=?, upstream_http_status=?, retryable=?,
                latency_ms=?, usage_status=?, completed_at=?, updated_at=?
            WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
              AND (?=0 OR provider_call_status='called')
            """,
            (
                status,
                error.code if error else None,
                error.error_category if error else None,
                error.http_status if error else None,
                1 if error and error.retryable else 0,
                max(0, latency_ms),
                final_usage_status,
                now,
                now,
                attempt.tenant_id,
                attempt.routing_attempt_id,
                1 if usage_exposure_possible else 0,
            ),
        )
        if cursor.rowcount != 1:
            # 只有 started 状态允许一次性转终态，重复完成或串租户更新都视为一致性冲突。
            raise sqlite3.IntegrityError("routing_attempt_terminal_state_conflict")
        if not succeeded and error is not None and error.evidence is not None:
            # 只有 Adapter 附带的冻结 evidence 能取得写入资格；Fact 与 owner 终态在同一事务，
            # 任一外键/触发器拒绝都会回滚上面的 UPDATE 和后续质量观察。
            insert_provider_error_fact(
                connection,
                tenant_id=attempt.tenant_id,
                request_id=attempt.request_id,
                trace_id=attempt.trace_id,
                routing_attempt_id=attempt.routing_attempt_id,
                provider_id=attempt.route.provider_id,
                model_id=attempt.route.model_id,
                provider_code=attempt.route.provider_code,
                evidence=error.evidence,
                # network/other 可能使用内部 transport_failed；evidence 仍保留公开归一码。
                owner_terminal_error_code=error.code,
                created_at=now,
            )
        if quality_observation_eligible and record_quality_observation:
            # 质量增量与唯一终态共同提交；本地 Adapter/Key 等错误必须传 false，不能罚模型。
            record_model_quality_observation(
                connection,
                tenant_id=attempt.tenant_id,
                model_id=attempt.route.model_id,
                succeeded=succeeded,
                latency_ms=max(0, latency_ms),
                observed_at=completed_at,
            )
        connection.commit()
    # attempt 终态记录是辅助审计，失败不能覆盖原始上游响应，但必须输出安全告警供排查。
    except (DbError, sqlite3.Error, ValueError):
        if connection is not None:
            connection.rollback()
        logger.warning(
            "route.attempt_completion_failed",
            extra={
                "event": "route.attempt_completion_failed",
                "tenant_id": attempt.tenant_id,
                "request_id": attempt.request_id,
                "trace_id": attempt.trace_id,
                "object_id": attempt.routing_attempt_id,
                "payload": {"attempt_sequence": attempt.attempt_sequence, "target_status": status},
            },
        )
    finally:
        if connection is not None:
            connection.close()


def log_fallback_triggered(
    plan: GatewayRoutePlan,
    attempt: RoutingAttempt,
    error: NormalizedProviderError,
) -> WebhookEvent:
    """记录 route.fallback_triggered 安全结构化事件。

    event_id 由 routing_attempt_id 稳定派生；当前函数只记录白名单元数据，异步 Webhook
    队列复用同一 payload 时也不得加入 Endpoint、请求正文或 Key 信息。
    """

    source = plan.initial_route
    target = attempt.route
    payload = {
        "request_id": attempt.request_id,
        "model_alias_id": source.model_alias_id,
        "fallback_policy_id": plan.fallback_policy_id,
        "fallback_policy_version": plan.fallback_policy_version,
        "from_model_id": source.model_id,
        "to_model_id": target.model_id,
        "from_provider_code": source.provider_code,
        "to_provider_code": target.provider_code,
        "trigger_error_code": error.code,
        "error_category": error.error_category,
        "attempt_sequence": attempt.attempt_sequence,
    }
    event = build_webhook_event(
        tenant_id=attempt.tenant_id,
        event_type="route.fallback_triggered",
        payload=payload,
        object_id=source.model_alias_id,
        trace_id=attempt.trace_id,
        event_id=f"evt_{attempt.routing_attempt_id}",
    )
    logger.info(
        "route.fallback_triggered",
        extra={
            "event": "route.fallback_triggered",
            "event_id": event.event_id,
            "tenant_id": attempt.tenant_id,
            "request_id": attempt.request_id,
            "trace_id": attempt.trace_id,
            "object_id": source.model_alias_id,
            "payload": payload,
        },
    )
    return event


def _log_attempt_gate_failure(
    route: GatewayRoute, request_id: str, trace_id: str, sequence: int
) -> None:
    """记录不含数据库路径、SQL、请求正文和凭据的执行门失败摘要。"""

    logger.error(
        "route.attempt_gate_failed",
        extra={
            "event": "route.attempt_gate_failed",
            "tenant_id": route.tenant_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "object_id": route.model_alias_id or route.model_id,
            "payload": {
                "attempt_sequence": sequence,
                "model_id": route.model_id,
                "provider_code": route.provider_code,
            },
        },
    )


def _to_db_time(value: datetime) -> str:
    """把 UTC 时间写成项目统一的秒级 ISO-8601 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回无微秒 UTC 时间，保证事务字段和测试断言稳定。"""

    return datetime.now(UTC).replace(microsecond=0)
