"""PRD-086 客户健康确定性评分、物化与纯读查询服务。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext

from .models import (
    CustomerHealthAction,
    CustomerHealthActivation,
    CustomerHealthResponse,
    CustomerHealthSignal,
    CustomerHealthWindow,
)

# 评分版本、计数上限、信号顺序、权重、风险与建议映射共同冻结公开业务公式。
SCORE_VERSION = "customer-health-v1"
SAFE_COUNT_MAX = 922337203685477
EFFECTIVE_WEIGHT_BPS = 9000
SIGNAL_CODES = (
    "usage_trend",
    "reliability",
    "cost_anomaly_health",
    "budget_readiness",
    "team_activity",
    "ticket_health",
)
SIGNAL_WEIGHTS = (2500, 2500, 1500, 1000, 1500, 1000)
RISK_TAGS = (
    "usage_declining",
    "high_error_rate",
    "cost_anomaly_open",
    "budget_missing",
    "team_inactive",
)
ACTION_BY_TAG = {
    "usage_declining": "restore_gateway_activity",
    "high_error_rate": "review_failed_requests",
    "cost_anomaly_open": "review_cost_anomalies",
    "budget_missing": "configure_tenant_budget",
    "team_inactive": "reengage_team",
}
ACTION_TARGET = {
    "restore_gateway_activity": ("growth-analytics", "product_analytics.read"),
    "review_failed_requests": ("request-logs", "request_log.read"),
    "review_cost_anomalies": ("cost-anomalies", "billing.anomaly.read"),
    "configure_tenant_budget": ("budgets", "billing.budget.read"),
    "reengage_team": ("users", "user.manage"),
    "connect_support_ticket_source": (None, None),
    "maintain_current_adoption": (None, None),
}
_SCORE_ID = re.compile(r"^chs_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHAIN_ID = re.compile(r"^(req|trc)_chs_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """只保存六源的安全整数聚合，不包含用户、请求或正文身份。"""

    tenant_id: str
    window_start: str
    window_end: str
    comparison_start: str
    comparison_end: str
    current_request_count: int
    previous_request_count: int
    successful_request_count: int
    failed_request_count: int
    unresolved_cost_anomaly_count: int
    critical_cost_anomaly_count: int
    active_budget_rule_count: int
    eligible_user_count: int
    recently_active_user_count: int
    activation_stage_count: int
    evaluated_at: str


@dataclass(frozen=True, slots=True)
class SignalProjection:
    """保存单个信号的可公开评分投影，不携带请求或用户明细。"""

    code: str
    weight_bps: int
    availability: str
    health_status: str
    score_bps: int | None
    contribution_bps: int
    current_value: int | None
    comparison_value: int | None
    rate_bps: int | None
    source_kind: str


@dataclass(frozen=True, slots=True)
class HealthProjection:
    """保存可哈希的客户健康总投影，用于 changed-only 事件判定。"""

    score_bps: int
    risk_level: str
    risk_tags: tuple[str, ...]
    action_codes: tuple[str, ...]
    signals: tuple[SignalProjection, ...]
    source_fingerprint: str
    health_projection_sha256: str


def evaluate_source_snapshot(source: SourceSnapshot) -> HealthProjection:
    """使用 v1 整数规则评分，并以最大余数保证贡献精确重归一化。"""

    _validate_source(source)
    usage_availability = (
        "no_samples"
        if source.current_request_count == 0 and source.previous_request_count == 0
        else "available"
    )
    if source.previous_request_count == 0:
        usage_rate = 10000 if source.current_request_count > 0 else None
        usage_score = 10000 if source.current_request_count > 0 else 0
    else:
        usage_rate = min(
            10000,
            source.current_request_count * 10000 // source.previous_request_count,
        )
        usage_score = usage_rate

    reliability_availability = (
        "no_samples" if source.current_request_count == 0 else "available"
    )
    success_rate = (
        None
        if source.current_request_count == 0
        else _round_ratio(source.successful_request_count, source.current_request_count)
    )
    reliability_score = 5000 if success_rate is None else success_rate
    error_rate = 0 if success_rate is None else 10000 - success_rate

    # 严重成本异常直接归零，普通未解决异常保留四成分值，确保成本风险可追踪。
    if source.critical_cost_anomaly_count:
        cost_score = 0
    elif source.unresolved_cost_anomaly_count:
        cost_score = 4000
    else:
        cost_score = 10000
    budget_score = 10000 if source.active_budget_rule_count else 3000
    team_availability = "available" if source.eligible_user_count else "no_samples"
    team_rate = (
        None
        if source.eligible_user_count == 0
        else _round_ratio(
            source.recently_active_user_count,
            source.eligible_user_count,
        )
    )
    team_score = 0 if team_rate is None else team_rate

    scores = (usage_score, reliability_score, cost_score, budget_score, team_score)
    weighted_sum = sum(
        score * weight for score, weight in zip(scores, SIGNAL_WEIGHTS[:5], strict=True)
    )
    total = (weighted_sum + EFFECTIVE_WEIGHT_BPS // 2) // EFFECTIVE_WEIGHT_BPS
    contributions = _largest_remainder(scores, total)

    tags = tuple(
        tag
        for tag, present in zip(
            RISK_TAGS,
            (
                source.current_request_count == 0
                or (usage_rate is not None and usage_rate < 5000),
                source.current_request_count >= 5 and error_rate >= 500,
                source.unresolved_cost_anomaly_count > 0,
                source.active_budget_rule_count == 0,
                source.recently_active_user_count == 0
                or (
                    source.eligible_user_count >= 2
                    and team_rate is not None
                    and team_rate < 5000
                ),
            ),
            strict=True,
        )
        if present
    )
    risk = _score_risk(total)
    if source.current_request_count == 0:
        risk = _raise_risk(risk, "at_risk")
    # 至少五个样本时失败率达到阈值必须提升风险下限，不能被其他高分抵消。
    if source.current_request_count >= 5 and error_rate >= 500:
        risk = _raise_risk(risk, "at_risk" if error_rate >= 1000 else "watch")
    # 严重成本异常强制 critical，普通未解决异常至少提升为 watch。
    if source.critical_cost_anomaly_count:
        risk = "critical"
    elif source.unresolved_cost_anomaly_count:
        risk = _raise_risk(risk, "watch")

    signals = (
        SignalProjection(
            "usage_trend", 2500, usage_availability,
            "critical" if usage_availability == "no_samples" else _score_risk(usage_score),
            usage_score, contributions[0], source.current_request_count,
            source.previous_request_count, usage_rate, "request_log_aggregate",
        ),
        SignalProjection(
            "reliability", 2500, reliability_availability,
            (
                "unknown"
                if reliability_availability == "no_samples"
                else _score_risk(reliability_score)
            ),
            reliability_score, contributions[1], source.successful_request_count,
            source.current_request_count, success_rate, "request_log_aggregate",
        ),
        SignalProjection(
            "cost_anomaly_health", 1500, "available", _score_risk(cost_score),
            cost_score, contributions[2], source.unresolved_cost_anomaly_count,
            source.critical_cost_anomaly_count, None, "cost_anomaly_aggregate",
        ),
        SignalProjection(
            "budget_readiness", 1000, "available", _score_risk(budget_score),
            budget_score, contributions[3], source.active_budget_rule_count,
            None, None, "budget_rule_aggregate",
        ),
        SignalProjection(
            "team_activity", 1500, team_availability,
            "critical" if team_availability == "no_samples" else _score_risk(team_score),
            team_score, contributions[4], source.recently_active_user_count,
            source.eligible_user_count, team_rate, "user_activity_aggregate",
        ),
        SignalProjection(
            "ticket_health", 1000, "unavailable", "unknown", None, 0,
            None, None, None, "support_ticket_unconfigured",
        ),
    )
    actions = tuple(ACTION_BY_TAG[tag] for tag in tags) + (
        "connect_support_ticket_source",
    )
    source_hash = _source_fingerprint(source)
    projection_hash = _projection_fingerprint(
        signals=signals,
        score_bps=total,
        risk_level=risk,
        tags=tags,
    )
    return HealthProjection(
        score_bps=total,
        risk_level=risk,
        risk_tags=tags,
        action_codes=actions,
        signals=signals,
        source_fingerprint=source_hash,
        health_projection_sha256=projection_hash,
    )


def collect_source_snapshot(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    evaluated_at: datetime,
) -> SourceSnapshot:
    """在调用方一致事务内聚合五类权威来源，并失败关闭坏时间或状态。"""

    # 来源聚合必须由调用方事务和明确 tenant_id 共同限定，缺一即失败关闭。
    if not connection.in_transaction or not tenant_id:
        raise ApiError("customer_health.unavailable")
    evaluated_at = _aware_utc(evaluated_at).replace(microsecond=0)
    window_end = evaluated_at.replace(minute=0, second=0)
    window_start = window_end - timedelta(days=7)
    comparison_end = window_start
    comparison_start = comparison_end - timedelta(days=7)
    evaluated_text = _db_time(evaluated_at)
    # 先识别 WHERE 会静默漏掉的坏时间和未知 request 终态。
    bad_request = connection.execute(
        """
        SELECT COUNT(*) FROM request_logs
        WHERE tenant_id=? AND (
          unixepoch(created_at) IS NULL OR unixepoch(created_at)>unixepoch(?)
          OR status NOT IN('success','failed')
        )
        """,
        (tenant_id, evaluated_text),
    ).fetchone()[0]
    bad_cost = connection.execute(
        """
        SELECT COUNT(*) FROM cost_anomalies
        WHERE tenant_id=? AND (
          unixepoch(detected_at) IS NULL OR unixepoch(detected_at)>unixepoch(?)
        )
        """,
        (tenant_id, evaluated_text),
    ).fetchone()[0]
    bad_budget = connection.execute(
        """
        SELECT COUNT(*) FROM budget_rules
        WHERE tenant_id=? AND (
          unixepoch(created_at) IS NULL OR unixepoch(created_at)>unixepoch(?)
          OR unixepoch(updated_at) IS NULL OR unixepoch(updated_at)>unixepoch(?)
        )
        """,
        (tenant_id, evaluated_text, evaluated_text),
    ).fetchone()[0]
    bad_user = connection.execute(
        """
        SELECT COUNT(*) FROM users
        WHERE tenant_id=? AND (
          unixepoch(created_at) IS NULL OR unixepoch(created_at)>unixepoch(?)
          OR unixepoch(updated_at) IS NULL OR unixepoch(updated_at)>unixepoch(?)
          OR(last_login_at IS NOT NULL AND(
            unixepoch(last_login_at) IS NULL OR unixepoch(last_login_at)>unixepoch(?)
          ))
        )
        """,
        (tenant_id, evaluated_text, evaluated_text, evaluated_text),
    ).fetchone()[0]
    bad_console = connection.execute(
        """
        SELECT COUNT(*) FROM console_page_events
        WHERE tenant_id=? AND (unixepoch(created_at) IS NULL OR unixepoch(created_at)>unixepoch(?))
        """,
        (tenant_id, evaluated_text),
    ).fetchone()[0]
    bad_product = connection.execute(
        """
        SELECT COUNT(*) FROM product_events
        WHERE tenant_id=? AND (
          unixepoch(occurred_at) IS NULL OR unixepoch(occurred_at)>unixepoch(?)
        )
        """,
        (tenant_id, evaluated_text),
    ).fetchone()[0]
    # 任何坏时间、未来事实或未知状态都会导致聚合失真，因此整次评分拒绝继续。
    if any(type(value) is not int or value != 0 for value in (
        bad_request, bad_cost, bad_budget, bad_user, bad_console, bad_product
    )):
        # 坏来源计数不为零时停止评分，不能静默跳过异常事实。
        raise ApiError("customer_health.unavailable")

    request = connection.execute(
        """
        SELECT
          SUM(CASE WHEN unixepoch(created_at)>=unixepoch(?)
                    AND unixepoch(created_at)<unixepoch(?) THEN 1 ELSE 0 END) current_count,
          SUM(CASE WHEN unixepoch(created_at)>=unixepoch(?)
                    AND unixepoch(created_at)<unixepoch(?) THEN 1 ELSE 0 END) previous_count,
          SUM(CASE WHEN unixepoch(created_at)>=unixepoch(?)
                    AND unixepoch(created_at)<unixepoch(?) AND status='success'
                   THEN 1 ELSE 0 END) success_count,
          SUM(CASE WHEN unixepoch(created_at)>=unixepoch(?)
                    AND unixepoch(created_at)<unixepoch(?) AND status='failed'
                   THEN 1 ELSE 0 END) failed_count
        FROM request_logs WHERE tenant_id=?
        """,
        (
            _db_time(window_start), _db_time(window_end),
            _db_time(comparison_start), _db_time(comparison_end),
            _db_time(window_start), _db_time(window_end),
            _db_time(window_start), _db_time(window_end), tenant_id,
        ),
    ).fetchone()
    cost = connection.execute(
        """
        SELECT COUNT(*) unresolved_count,
               SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) critical_count
        FROM cost_anomalies
        WHERE tenant_id=? AND status IN('open','acknowledged')
        """,
        (tenant_id,),
    ).fetchone()
    budget = connection.execute(
        "SELECT COUNT(*) FROM budget_rules WHERE tenant_id=? AND status='active'",
        (tenant_id,),
    ).fetchone()[0]
    activity_start = _db_time(window_end - timedelta(days=30))
    activity = connection.execute(
        """
        SELECT COUNT(*) eligible_count,
               SUM(CASE WHEN (
                 (last_login_at IS NOT NULL AND unixepoch(last_login_at)>=unixepoch(?)
                  AND unixepoch(last_login_at)<unixepoch(?))
                 OR EXISTS(
                   SELECT 1 FROM console_page_events event
                   WHERE event.tenant_id=user.tenant_id AND event.user_id=user.user_id
                     AND unixepoch(event.created_at)>=unixepoch(?)
                     AND unixepoch(event.created_at)<unixepoch(?)
                 )
               ) THEN 1 ELSE 0 END) recent_count
        FROM users user WHERE tenant_id=? AND status='active'
        """,
        (activity_start, _db_time(window_end), activity_start, _db_time(window_end), tenant_id),
    ).fetchone()
    activation = connection.execute(
        """
        SELECT COUNT(DISTINCT event_type) FROM product_events
        WHERE tenant_id=? AND event_type IN(
          'tenant.registered','provider.created','virtual_key.created',
          'gateway.first_call_succeeded','budget.configured','teammate.invited',
          'request_logs.viewed'
        ) AND unixepoch(occurred_at)<unixepoch(?)
        """,
        (tenant_id, evaluated_text),
    ).fetchone()[0]
    values = [
        *(0 if value is None else value for value in request),
        *(0 if value is None else value for value in cost),
        budget,
        *(0 if value is None else value for value in activity),
        activation,
    ]
    # 聚合结果只接受非负安全整数，异常类型和越界值不能写入评分或事件。
    if any(type(value) is not int or not 0 <= value <= SAFE_COUNT_MAX for value in values):
        raise ApiError("customer_health.unavailable")
    return SourceSnapshot(
        tenant_id=tenant_id,
        window_start=_db_time(window_start),
        window_end=_db_time(window_end),
        comparison_start=_db_time(comparison_start),
        comparison_end=_db_time(comparison_end),
        current_request_count=values[0],
        previous_request_count=values[1],
        successful_request_count=values[2],
        failed_request_count=values[3],
        unresolved_cost_anomaly_count=values[4],
        critical_cost_anomaly_count=values[5],
        active_budget_rule_count=values[6],
        eligible_user_count=values[7],
        recently_active_user_count=values[8],
        activation_stage_count=values[9],
        evaluated_at=evaluated_text,
    )


def materialize_customer_health(
    connection: sqlite3.Connection,
    source: SourceSnapshot,
) -> tuple[str, bool, bool]:
    """幂等插入 append-only score，并仅为基线或真实投影变化创建 outbox。"""

    # 物化与 source outbox 必须处于同一事务，避免评分提交后事件缺失。
    if not connection.in_transaction:
        raise ApiError("customer_health.unavailable")
    projection = evaluate_source_snapshot(source)
    score_id = _score_id(source, projection.source_fingerprint)
    request_id = _operation_id(source, "req_chs_")
    trace_id = _operation_id(source, "trc_chs_")
    existing = connection.execute(
        """
        SELECT * FROM customer_health_scores
        WHERE tenant_id=? AND window_end=? AND source_fingerprint=?
        """,
        (source.tenant_id, source.window_end, projection.source_fingerprint),
    ).fetchone()
    if existing is not None:
        _validated_row(existing)
        # 相同租户、窗口和来源哈希必须映射同一稳定 ID，否则视为持久化腐坏。
        if existing["customer_health_score_id"] != score_id:
            raise ApiError("customer_health.unavailable")
        return score_id, False, False
    previous = connection.execute(
        """
        SELECT health_projection_sha256 FROM customer_health_scores
        WHERE tenant_id=? ORDER BY customer_health_score_sequence DESC LIMIT 1
        """,
        (source.tenant_id,),
    ).fetchone()
    fields = _score_insert_fields(source, projection, score_id, request_id, trace_id)
    placeholders = ",".join("?" for _ in fields)
    connection.execute(
        f"INSERT INTO customer_health_scores({','.join(fields)}) VALUES({placeholders})",
        tuple(fields.values()),
    )
    inserted = True
    row = connection.execute(
        "SELECT * FROM customer_health_scores WHERE tenant_id=? AND customer_health_score_id=?",
        (source.tenant_id, score_id),
    ).fetchone()
    # INSERT 后必须能按 tenant_id 和稳定 ID 回读，缺失说明事务状态不可信。
    if row is None:
        raise ApiError("customer_health.unavailable")
    _validated_row(row)
    changed = (
        previous is None
        or previous["health_projection_sha256"]
        != projection.health_projection_sha256
    )
    outbox_created = False
    if inserted and changed:
        digest = hashlib.sha256(score_id.encode("ascii")).hexdigest()[:32]
        connection.execute(
            """
            INSERT INTO customer_health_score_outbox(
              customer_health_score_outbox_id,tenant_id,customer_health_score_id,
              webhook_event_id,status,attempt_count,generation,next_attempt_at,
              created_at,updated_at
            ) VALUES(?,?,?,?,'pending',0,0,?,?,?)
            """,
            (
                f"chso_{digest}", source.tenant_id, score_id, f"evt_chs_{digest}",
                source.evaluated_at, source.evaluated_at, source.evaluated_at,
            ),
        )
        outbox_created = True
    return score_id, inserted, outbox_created


def get_customer_health(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> CustomerHealthResponse:
    """只读取当前认证租户最新已提交快照，不扫描来源且不取得写锁。"""

    tenant_id = context.user.tenant_id
    try:
        connection = connect_database(config.database_url)
    # 数据库连接失败统一返回安全不可用码，不暴露数据库路径或底层异常。
    except DbError:
        raise ApiError("customer_health.unavailable") from None
    try:
        scheduler = connection.execute(
            "SELECT * FROM customer_health_scheduler_tenants WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        # 当前租户必须存在合法 Scheduler 游标，否则最新评分的所有权无法证明。
        if scheduler is None or scheduler["status"] not in {"pending", "leased"}:
            raise ApiError("customer_health.unavailable")
        row = connection.execute(
            """
            SELECT * FROM customer_health_scores WHERE tenant_id=?
            ORDER BY customer_health_score_sequence DESC LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()
        # 尚无已提交评分时返回固定 not_ready，不通过 GET 临时扫描来源并写入。
        if row is None:
            raise ApiError("customer_health.not_ready")
        source, projection = _validated_row(row)
    # 已分类业务错误保留统一错误码，不重新包装或附带持久化字段。
    except ApiError:
        raise
    # 结构或数据库异常统一收敛为不可用，避免原始异常正文进入 API。
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        raise ApiError("customer_health.unavailable") from None
    finally:
        connection.close()

    current = _aware_utc(now or datetime.now(UTC)).replace(microsecond=0)
    current_hour = current.replace(minute=0, second=0)
    end = _parse_time(source.window_end)
    # 评分窗口不得位于当前整点之后，未来快照可能造成跨租户或时钟污染。
    if end > current_hour:
        raise ApiError("customer_health.unavailable")
    freshness = "fresh" if current_hour - end <= timedelta(hours=2) else "stale"
    permissions = set(context.permission_codes)
    actions = [
        _action(code, permissions)
        for code in projection.action_codes
    ]
    return CustomerHealthResponse(
        tenant_id=source.tenant_id,
        customer_health_score_id=str(row["customer_health_score_id"]),
        score_bps=projection.score_bps,
        risk_level=projection.risk_level,
        risk_tags=list(projection.risk_tags),
        missing_signals=["ticket_health"],
        freshness_status=freshness,
        recommended_actions=actions,
        window=CustomerHealthWindow(
            start=source.window_start,
            end=source.window_end,
            comparison_start=source.comparison_start,
            comparison_end=source.comparison_end,
        ),
        signals=[
            CustomerHealthSignal(
                code=signal.code,
                weight_bps=signal.weight_bps,
                availability=signal.availability,
                health_status=signal.health_status,
                score_bps=signal.score_bps,
                contribution_bps=signal.contribution_bps,
                current_value=signal.current_value,
                comparison_value=signal.comparison_value,
                rate_bps=signal.rate_bps,
                source_kind=signal.source_kind,
            )
            for signal in projection.signals
        ],
        activation=CustomerHealthActivation(
            completed_stage_count=source.activation_stage_count,
            activated=source.activation_stage_count == 7,
        ),
        evaluated_at=source.evaluated_at,
        generated_at=_db_time(current),
        request_id=request_id,
        trace_id=trace_id,
    )


def _validated_row(row: sqlite3.Row) -> tuple[SourceSnapshot, HealthProjection]:
    """逐列重算不可变 score、两类 hash、ID 与风险，损坏时失败关闭。"""

    source = SourceSnapshot(
        tenant_id=_text(row["tenant_id"]),
        window_start=_time_text(row["window_start"]),
        window_end=_time_text(row["window_end"]),
        comparison_start=_time_text(row["comparison_start"]),
        comparison_end=_time_text(row["comparison_end"]),
        current_request_count=_count(row["current_request_count"]),
        previous_request_count=_count(row["previous_request_count"]),
        successful_request_count=_count(row["successful_request_count"]),
        failed_request_count=_count(row["failed_request_count"]),
        unresolved_cost_anomaly_count=_count(row["unresolved_cost_anomaly_count"]),
        critical_cost_anomaly_count=_count(row["critical_cost_anomaly_count"]),
        active_budget_rule_count=_count(row["active_budget_rule_count"]),
        eligible_user_count=_count(row["eligible_user_count"]),
        recently_active_user_count=_count(row["recently_active_user_count"]),
        activation_stage_count=_integer(row["activation_stage_count"], 0, 7),
        evaluated_at=_time_text(row["evaluated_at"]),
    )
    projection = evaluate_source_snapshot(source)
    score_id = _text(row["customer_health_score_id"])
    expected_values: Mapping[str, object] = {
        "score_version": SCORE_VERSION,
        "source_fingerprint": projection.source_fingerprint,
        "health_projection_sha256": projection.health_projection_sha256,
        "ticket_source_status": "unavailable",
        "ticket_health_score_bps": None,
        "ticket_health_contribution_bps": 0,
        "effective_weight_bps": EFFECTIVE_WEIGHT_BPS,
        "score_bps": projection.score_bps,
        "risk_level": projection.risk_level,
        "usage_declining": int("usage_declining" in projection.risk_tags),
        "high_error_rate": int("high_error_rate" in projection.risk_tags),
        "cost_anomaly_open": int("cost_anomaly_open" in projection.risk_tags),
        "budget_missing": int("budget_missing" in projection.risk_tags),
        "team_inactive": int("team_inactive" in projection.risk_tags),
        "operation_request_id": _operation_id(source, "req_chs_"),
        "operation_trace_id": _operation_id(source, "trc_chs_"),
        "created_at": source.evaluated_at,
        "updated_at": source.evaluated_at,
    }
    for signal in projection.signals[:5]:
        expected_values[f"{signal.code}_score_bps"] = signal.score_bps
        expected_values[f"{signal.code}_contribution_bps"] = signal.contribution_bps
    # 回读行必须完整复现稳定 ID、两类哈希、评分、风险与请求链，任一漂移都视为腐坏。
    if (
        _SCORE_ID.fullmatch(score_id) is None
        or score_id != _score_id(source, projection.source_fingerprint)
        or any(row[key] != value for key, value in expected_values.items())
        or not _SHA256.fullmatch(_text(row["source_fingerprint"]))
        or not _SHA256.fullmatch(_text(row["health_projection_sha256"]))
        or _CHAIN_ID.fullmatch(_text(row["operation_request_id"])) is None
        or _CHAIN_ID.fullmatch(_text(row["operation_trace_id"])) is None
    ):
        # 任一回读字段无法复算时拒绝快照，不能返回部分可信评分。
        raise ApiError("customer_health.unavailable")
    return source, projection


def _score_insert_fields(
    source: SourceSnapshot,
    projection: HealthProjection,
    score_id: str,
    request_id: str,
    trace_id: str,
) -> dict[str, object]:
    """生成 append-only 评分插入字段，并绑定 tenant_id 与安全请求链标识。"""

    fields: dict[str, object] = {
        "customer_health_score_id": score_id,
        "tenant_id": source.tenant_id,
        "score_version": SCORE_VERSION,
        "window_start": source.window_start,
        "window_end": source.window_end,
        "comparison_start": source.comparison_start,
        "comparison_end": source.comparison_end,
        "source_fingerprint": projection.source_fingerprint,
        "health_projection_sha256": projection.health_projection_sha256,
        "current_request_count": source.current_request_count,
        "previous_request_count": source.previous_request_count,
        "successful_request_count": source.successful_request_count,
        "failed_request_count": source.failed_request_count,
        "unresolved_cost_anomaly_count": source.unresolved_cost_anomaly_count,
        "critical_cost_anomaly_count": source.critical_cost_anomaly_count,
        "active_budget_rule_count": source.active_budget_rule_count,
        "eligible_user_count": source.eligible_user_count,
        "recently_active_user_count": source.recently_active_user_count,
        "ticket_source_status": "unavailable",
        "activation_stage_count": source.activation_stage_count,
    }
    for signal in projection.signals:
        fields[f"{signal.code}_score_bps"] = signal.score_bps
        fields[f"{signal.code}_contribution_bps"] = signal.contribution_bps
    fields.update(
        {
            "effective_weight_bps": EFFECTIVE_WEIGHT_BPS,
            "score_bps": projection.score_bps,
            "risk_level": projection.risk_level,
            **{tag: int(tag in projection.risk_tags) for tag in RISK_TAGS},
            "operation_request_id": request_id,
            "operation_trace_id": trace_id,
            "evaluated_at": source.evaluated_at,
            "created_at": source.evaluated_at,
            "updated_at": source.evaluated_at,
        }
    )
    return fields


def _source_fingerprint(source: SourceSnapshot) -> str:
    """对 tenant-qualified 来源计数和窗口生成规范 SHA-256 指纹。"""

    return _hash_array(
        (
            "customer-health-source-v1", source.tenant_id, SCORE_VERSION,
            source.window_start, source.window_end,
            source.comparison_start, source.comparison_end,
            source.current_request_count, source.previous_request_count,
            source.successful_request_count, source.failed_request_count,
            source.unresolved_cost_anomaly_count, source.critical_cost_anomaly_count,
            source.active_budget_rule_count, source.eligible_user_count,
            source.recently_active_user_count, "unavailable", source.activation_stage_count,
        )
    )


def _projection_fingerprint(
    *,
    signals: Sequence[SignalProjection],
    score_bps: int,
    risk_level: str,
    tags: Sequence[str],
) -> str:
    """对公开评分、信号贡献和风险标签生成 changed-only 投影指纹。"""

    values: list[object] = ["customer-health-projection-v1", SCORE_VERSION]
    for signal in signals[:5]:
        values.extend(
            (
                signal.score_bps,
                signal.contribution_bps,
                signal.availability,
                signal.health_status,
            )
        )
    values.extend(
        (
            None, 0, "unavailable", "unknown",
            EFFECTIVE_WEIGHT_BPS, score_bps, risk_level,
            *(int(tag in tags) for tag in RISK_TAGS), "ticket_health",
        )
    )
    return _hash_array(values)


def _score_id(source: SourceSnapshot, source_fingerprint: str) -> str:
    """从租户、窗口和来源指纹派生稳定且不可枚举的评分标识。"""

    digest = _hash_array(
        (
            "customer-health-score-v1", source.tenant_id, SCORE_VERSION,
            source.window_end, source_fingerprint,
        )
    )[:32]
    return f"chs_{digest}"


def _operation_id(source: SourceSnapshot, prefix: str) -> str:
    """为 Scheduler 物化生成稳定 request_id 或 trace_id，便于失败追踪。"""

    digest = _hash_array(
        (
            "customer-health-operation-v1",
            source.tenant_id,
            source.window_end,
            _source_fingerprint(source),
        )
    )[:32]
    return f"{prefix}{digest}"


def _largest_remainder(scores: Sequence[int], total: int) -> tuple[int, ...]:
    """按整数最大余数法分配贡献，确保每项之和精确等于总分。"""

    numerators = [score * weight for score, weight in zip(scores, SIGNAL_WEIGHTS[:5], strict=True)]
    contributions = [value // EFFECTIVE_WEIGHT_BPS for value in numerators]
    remaining = total - sum(contributions)
    order = sorted(
        range(len(numerators)),
        key=lambda index: (-(numerators[index] % EFFECTIVE_WEIGHT_BPS), index),
    )
    for index in order[:remaining]:
        contributions[index] += 1
    # 分配后贡献和若不等于总分，说明公式或输入已漂移，必须停止物化。
    if sum(contributions) != total:
        raise ApiError("customer_health.unavailable")
    return tuple(contributions)


def _validate_source(source: SourceSnapshot) -> None:
    """校验租户归属、计数守恒、时间窗口和激活范围后才允许评分。"""

    # tenant_id 不得为空，成功与失败请求之和必须精确等于当前请求数。
    if (
        not source.tenant_id
        or source.successful_request_count + source.failed_request_count
        != source.current_request_count
    ):
        # 缺失租户或请求计数不守恒时，评分和追踪链均不可建立。
        raise ApiError("customer_health.unavailable")
    counts = (
        source.current_request_count, source.previous_request_count,
        source.successful_request_count, source.failed_request_count,
        source.unresolved_cost_anomaly_count, source.critical_cost_anomaly_count,
        source.active_budget_rule_count, source.eligible_user_count,
        source.recently_active_user_count,
    )
    # 全部来源计数必须是非负安全整数，布尔值和隐式数值均不接受。
    if any(type(value) is not int or not 0 <= value <= SAFE_COUNT_MAX for value in counts):
        raise ApiError("customer_health.unavailable")
    # 严重异常和活跃用户不得超过各自总数，激活阶段限定在冻结范围。
    if (
        source.critical_cost_anomaly_count > source.unresolved_cost_anomaly_count
        or source.recently_active_user_count > source.eligible_user_count
        or type(source.activation_stage_count) is not int
        or not 0 <= source.activation_stage_count <= 7
    ):
        # 来源子计数超过总数或激活范围异常时拒绝生成健康结论。
        raise ApiError("customer_health.unavailable")
    comparison_start = _parse_time(source.comparison_start)
    comparison_end = _parse_time(source.comparison_end)
    start = _parse_time(source.window_start)
    end = _parse_time(source.window_end)
    evaluated = _parse_time(source.evaluated_at)
    # 当前与比较窗口必须相邻且各七日，评估时间只能处于窗口结束后一小时内。
    if (
        end - start != timedelta(days=7)
        or start - comparison_start != timedelta(days=7)
        or comparison_end != start
        or evaluated < end
        or evaluated >= end + timedelta(hours=1)
        or end.minute or end.second or end.microsecond
    ):
        # 时间窗口或评估小时不符合冻结规则时停止物化。
        raise ApiError("customer_health.unavailable")


def _action(code: str, permissions: set[str]) -> CustomerHealthAction:
    """按后端实时权限投影建议导航目标，无权限时保留建议但清空链接。"""

    target, permission = ACTION_TARGET[code]
    return CustomerHealthAction(
        code=code,
        target_navigation_item_id=(
            target if permission is not None and permission in permissions else None
        ),
    )


def _score_risk(score: int) -> str:
    """按冻结基点阈值把评分映射为四档基础风险等级。"""

    if score >= 8000:
        return "healthy"
    if score >= 6000:
        return "watch"
    if score >= 3000:
        return "at_risk"
    return "critical"


def _raise_risk(current: str, minimum: str) -> str:
    """把当前风险提升到指定下限，绝不因附加信号而降低等级。"""

    order = ("healthy", "watch", "at_risk", "critical")
    return order[max(order.index(current), order.index(minimum))]


def _round_ratio(numerator: int, denominator: int) -> int:
    """用整数四舍五入计算基点比率，并拒绝非正分母。"""

    # 分母非正意味着来源计数守恒失效，不能生成用量或可靠性比率。
    if denominator <= 0:
        raise ApiError("customer_health.unavailable")
    return (numerator * 10000 + denominator // 2) // denominator


def _hash_array(values: Sequence[object]) -> str:
    """以规范 ASCII JSON 编码序列并生成稳定 SHA-256 摘要。"""

    body = json.dumps(list(values), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("ascii")).hexdigest()


def _text(value: object) -> str:
    """读取数据库必填文本，类型漂移或空值统一视为不可用。"""

    # tenant、哈希和请求链等必填文本不能通过隐式转换掩盖持久化腐坏。
    if type(value) is not str or not value:
        raise ApiError("customer_health.unavailable")
    return value


def _count(value: object) -> int:
    """读取来源聚合计数并限制在数据库安全整数范围内。"""

    return _integer(value, 0, SAFE_COUNT_MAX)


def _integer(value: object, minimum: int, maximum: int) -> int:
    """读取闭区间原生整数，拒绝布尔值、浮点数与范围外数据。"""

    # 评分字段只能使用原生整数，类型或范围异常时统一失败关闭。
    if type(value) is not int or not minimum <= value <= maximum:
        raise ApiError("customer_health.unavailable")
    return value


def _time_text(value: object) -> str:
    """读取并往返校验数据库 UTC 整秒文本，拒绝非规范表示。"""

    # 数据库时间必须经解析和规范化后字节一致，避免多种表示破坏稳定哈希。
    text = _text(value)
    if _db_time(_parse_time(text)) != text:
        raise ApiError("customer_health.unavailable")
    return text


def _parse_time(value: str) -> datetime:
    """严格解析 UTC Z 整秒文本，并把全部格式异常收敛为统一错误。"""

    # 缺少 Z、含小数秒或非字符串值都不属于冻结时间合同。
    if type(value) is not str or not value.endswith("Z") or "." in value:
        raise ApiError("customer_health.unavailable")
    # 日期解析失败时不透传底层异常正文，统一映射为客户健康不可用。
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # 时间解析失败只返回固定不可用码，原始异常正文不得外泄。
        raise ApiError("customer_health.unavailable") from None
    # 可解析时间仍必须确认零偏移，非 UTC 值不得参与租约或窗口比较。
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ApiError("customer_health.unavailable")
    return parsed.astimezone(UTC)


def _aware_utc(value: datetime) -> datetime:
    """要求 datetime 携带时区并转换为 UTC，拒绝含糊本地时间。"""

    # 无时区时间会让不同节点计算不同窗口，必须在聚合前失败关闭。
    if value.tzinfo is None:
        raise ApiError("customer_health.unavailable")
    return value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """把有时区时间格式化为数据库使用的 UTC Z 整秒文本。"""

    return _aware_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "EFFECTIVE_WEIGHT_BPS",
    "HealthProjection",
    "SAFE_COUNT_MAX",
    "SCORE_VERSION",
    "SourceSnapshot",
    "collect_source_snapshot",
    "evaluate_source_snapshot",
    "get_customer_health",
    "materialize_customer_health",
]
