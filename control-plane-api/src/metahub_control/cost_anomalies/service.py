"""PRD-053 成本异常查询、处置、幂等与审计事务。

公开查询只返回当前认证租户的不可变检测证据和安全治理摘要。写路径使用
BEGIN IMMEDIATE，并把 action、anomaly 状态、临时覆盖和 Audit 放在同一事务；
幂等键仅保存 SHA-256，任何日志和响应都不得包含原始 Key、Prompt、Response、
Provider Credential、幂等键或底层 SQL 异常。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable
from uuid import UUID, uuid4

from metahub_control.audit import (
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .models import (
    CostAnomalyActionInfo,
    CostAnomalyActionRequest,
    CostAnomalyActionResponse,
    CostAnomalyInfo,
    CostAnomalyListResponse,
    CostAnomalySummary,
    TemporaryRateLimitOverrideInfo,
)

# 公开资源格式、枚举白名单、分页上限和签名域共同限定查询与处置的稳定安全合同。
ANOMALY_ID_PATTERN = re.compile(r"^can_[0-9a-f]{32}$")
VALID_STATUSES = frozenset({"open", "acknowledged", "dismissed", "mitigated"})
VALID_RULE_TYPES = frozenset(
    {"tenant_spend_spike", "virtual_key_spend_spike", "virtual_key_off_hours"}
)
VALID_SEVERITIES = frozenset({"warning", "critical"})
VALID_DISMISS_REASONS = frozenset(
    {"false_positive", "expected_activity", "resolved_without_action", "other"}
)
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
MAX_LIMIT_VALUE = 100_000_000
CURSOR_DOMAIN = b"metahub-cost-anomaly-cursor-v1"
CURSOR_VERSION = 1
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


@dataclass(frozen=True, slots=True)
class _ListFilters:
    """规范化后的租户内列表筛选；该对象也参与 cursor HMAC 绑定。"""

    status: str | None
    rule_type: str | None
    severity: str | None
    environment_id: str | None
    virtual_key_id: str | None
    from_time: datetime | None
    to_time: datetime | None
    limit: int


@dataclass(frozen=True, slots=True)
class _CursorState:
    """解签后的只读快照边界，sequence 均来自当前租户数据库。"""

    snapshot_sequence: int
    before_sequence: int


def list_cost_anomalies(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    rule_type: str | None,
    severity: str | None,
    environment_id: str | None,
    virtual_key_id: str | None,
    from_time: str | None,
    to_time: str | None,
    limit: str | None,
    cursor: str | None,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> CostAnomalyListResponse:
    """在单一 tenant-qualified 只读快照中返回异常、汇总和 keyset 分页信息。

    GET 不触发检测器或 Webhook。cursor 同租户、筛选和 snapshot sequence 绑定；
    API DTO 在关闭连接前完全物化，避免 Windows 上遗留 SQLite 文件句柄。
    """

    current = _service_now(now)
    filters = _normalize_filters(
        status=status,
        rule_type=rule_type,
        severity=severity,
        environment_id=environment_id,
        virtual_key_id=virtual_key_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
    )
    filter_hash = _filter_hash(filters)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        _validate_filter_owners(connection, context.user.tenant_id, filters)
        if cursor:
            cursor_state = _decode_cursor(
                config,
                cursor,
                context.user.tenant_id,
                filter_hash,
            )
        else:
            row = connection.execute(
                "SELECT COALESCE(MAX(anomaly_sequence), 0) AS value "
                "FROM cost_anomalies WHERE tenant_id=?",
                (context.user.tenant_id,),
            ).fetchone()
            snapshot = _non_negative_integer(row["value"] if row else 0)
            cursor_state = _CursorState(
                snapshot_sequence=snapshot,
                before_sequence=snapshot + 1,
            )
        clauses, params = _list_where(
            context.user.tenant_id,
            filters,
            cursor_state,
        )
        where_sql = " AND ".join(clauses)
        total_row = connection.execute(
            f"SELECT COUNT(*) AS total FROM cost_anomalies ca WHERE {where_sql}",
            params,
        ).fetchone()
        rows = connection.execute(
            f"""
            SELECT
              ca.*,
              t.name AS tenant_name,
              e.name AS environment_name,
              vk.name AS virtual_key_name,
              vk.key_prefix AS virtual_key_prefix
            FROM cost_anomalies ca
            JOIN tenants t
              ON t.tenant_id=ca.tenant_id
            JOIN environments e
              ON e.tenant_id=ca.tenant_id AND e.environment_id=ca.environment_id
            LEFT JOIN virtual_keys vk
              ON vk.tenant_id=ca.tenant_id AND vk.virtual_key_id=ca.virtual_key_id
            WHERE {where_sql}
              AND ca.anomaly_sequence<?
            ORDER BY ca.anomaly_sequence DESC
            LIMIT ?
            """,
            [*params, cursor_state.before_sequence, filters.limit + 1],
        ).fetchall()
        visible = rows[: filters.limit]
        has_more = len(rows) > filters.limit
        anomalies = [
            _anomaly_info(connection, row, current)
            for row in visible
        ]
        next_cursor = None
        if has_more and visible:
            next_cursor = _encode_cursor(
                config,
                context.user.tenant_id,
                filter_hash,
                snapshot_sequence=cursor_state.snapshot_sequence,
                before_sequence=_positive_integer(visible[-1]["anomaly_sequence"]),
            )
        summary = _cost_anomaly_summary(
            connection,
            context.user.tenant_id,
            current,
        )
        response = CostAnomalyListResponse(
            cost_anomalies=anomalies,
            summary=summary,
            total=_non_negative_integer(total_row["total"] if total_row else 0),
            limit=filters.limit,
            has_more=has_more,
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    # 已规范化的领域错误必须保留原错误码，同时回滚当前只读快照。
    except ApiError:
        _rollback_safely(connection)
        raise
    # 数据库与持久类型异常统一折叠，避免查询响应泄露 SQL 或损坏行内容。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback_safely(connection)
        raise ApiError("cost_anomaly.unavailable") from exc
    finally:
        connection.close()


def apply_cost_anomaly_action(
    config: AppConfig,
    context: AuthContext,
    anomaly_id: str,
    payload: CostAnomalyActionRequest,
    *,
    idempotency_key: str,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> CostAnomalyActionResponse:
    """原子执行 acknowledge、dismiss、apply 或 release，并支持 actor 级幂等恢复。

    同 tenant+actor+Idempotency-Key 的相同规范化意图返回首次 action；不同意图返回
    409。幂等 replay 不新增 Audit、action 或 override，响应使用本次 request/trace。
    """

    normalized_id = _normalize_anomaly_id(anomaly_id)
    canonical = _canonical_action(payload)
    normalized_key = _normalize_idempotency_key(idempotency_key)
    key_hash = hashlib.sha256(normalized_key.encode("ascii")).hexdigest()
    intent_hash = hashlib.sha256(
        json.dumps(
            {
                "version": 1,
                "path": normalized_id,
                "action": canonical,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    current = _service_now(now)
    connection = _connect(config)
    audit_record = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = connection.execute(
            """
            SELECT *
            FROM cost_anomaly_actions
            WHERE tenant_id=? AND actor_user_id=? AND idempotency_key_sha256=?
            """,
            (context.user.tenant_id, context.user.user_id, key_hash),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["intent_sha256"]), intent_hash):
                # 同一 actor 的键只能恢复完全相同意图，禁止用旧键覆盖另一处置。
                raise ApiError("cost_anomaly.idempotency_conflict")
            anomaly_row = _load_anomaly(
                connection,
                context.user.tenant_id,
                str(replay["cost_anomaly_id"]),
            )
            if anomaly_row is None:
                # 幂等 action 指向的同租户异常缺失表示持久事实损坏，不能伪装成首次请求。
                raise ApiError("cost_anomaly.unavailable")
            response = _action_response(
                connection,
                anomaly_row,
                replay,
                current,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response

        anomaly_row = _load_anomaly(
            connection,
            context.user.tenant_id,
            normalized_id,
        )
        if anomaly_row is None:
            # 合法但跨租户的 ID 与不存在 ID 同形，不能用于枚举其他租户异常。
            raise ApiError("cost_anomaly.not_found")
        action_row, audit_before, audit_after, audit_action = _execute_action(
            connection,
            context,
            anomaly_row,
            canonical,
            current,
            key_hash=key_hash,
            intent_hash=intent_hash,
            request_id=request_id,
            trace_id=trace_id,
        )
        audit_record = record_audit_log(
            connection,
            context,
            action=audit_action,
            object_type="cost_anomaly",
            object_id=normalized_id,
            object_name=f"{anomaly_row['rule_type']}:{anomaly_row['scope_type']}",
            before_summary=audit_before,
            after_summary=audit_after,
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        refreshed = _load_anomaly(
            connection,
            context.user.tenant_id,
            normalized_id,
        )
        if refreshed is None:
            # 状态与审计已在同一事务写入，回读缺失只能按数据库不可用失败关闭。
            raise ApiError("cost_anomaly.unavailable")
        response = _action_response(
            connection,
            refreshed,
            action_row,
            current,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    # 服务层产生的领域错误必须与 action、override 和 Audit 一起完整回滚。
    except ApiError:
        _rollback_safely(connection)
        raise
    # 唯一键或状态 trigger 冲突属于可刷新重试的治理冲突，不暴露数据库细节。
    except sqlite3.IntegrityError as exc:
        _rollback_safely(connection)
        # 并发唯一键和 trigger 拒绝都不泄露 SQLite 细节；调用方刷新后可安全重试。
        raise ApiError("cost_anomaly.action_conflict") from exc
    # 其他 SQLite 故障无法证明动作是否可重试，统一返回服务不可用并保留原子回滚。
    except sqlite3.Error as exc:
        _rollback_safely(connection)
        raise ApiError("cost_anomaly.unavailable") from exc
    finally:
        connection.close()

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )
    return response

def _execute_action(
    connection: sqlite3.Connection,
    context: AuthContext,
    anomaly: sqlite3.Row,
    canonical: dict[str, object],
    now: datetime,
    *,
    key_hash: str,
    intent_hash: str,
    request_id: str,
    trace_id: str,
) -> tuple[sqlite3.Row, dict[str, object], dict[str, object], str]:
    """执行首次规范化意图，并返回 action 与 Audit 前后摘要。"""

    action = str(canonical["action"])
    status_before = str(anomaly["status"])
    base = _audit_base(anomaly)
    timestamp = _to_db_time(now)
    # 检测器和事件 outbox 也会推进 anomaly.updated_at；管理动作必须基于事务内
    # 已读取的行版本单调递增，否则同秒连续处置会被 0043 状态触发器拒绝。
    anomaly_updated_at = _monotonic_row_time(anomaly["updated_at"], now)
    if action == "acknowledge":
        if status_before != "open":
            # acknowledge 只允许从 open 迁移，避免重复确认产生虚假审计动作。
            raise ApiError("cost_anomaly.action_conflict")
        action_id = _insert_action(
            connection,
            context,
            anomaly,
            action_type=action,
            status_before=status_before,
            status_after="acknowledged",
            dismiss_reason=None,
            duration_minutes=None,
            rpm_limit=None,
            tpm_limit=None,
            concurrency_limit=None,
            target_override_id=None,
            effective_before=(None, None, None),
            effective_after=(None, None, None),
            key_hash=key_hash,
            intent_hash=intent_hash,
            request_id=request_id,
            trace_id=trace_id,
            created_at=timestamp,
        )
        _update_anomaly_status(
            connection,
            context.user.tenant_id,
            str(anomaly["cost_anomaly_id"]),
            expected=status_before,
            target="acknowledged",
            updated_at=anomaly_updated_at,
        )
        return (
            _load_action(connection, context.user.tenant_id, action_id),
            {**base, "status": status_before},
            {**base, "status": "acknowledged"},
            "cost_anomaly.acknowledged",
        )

    if action == "dismiss":
        if status_before not in {"open", "acknowledged"}:
            # dismiss 不能覆盖已关闭或已缓解状态，历史状态只能通过 append-only action 追踪。
            raise ApiError("cost_anomaly.action_conflict")
        reason = str(canonical["dismiss_reason"])
        action_id = _insert_action(
            connection,
            context,
            anomaly,
            action_type=action,
            status_before=status_before,
            status_after="dismissed",
            dismiss_reason=reason,
            duration_minutes=None,
            rpm_limit=None,
            tpm_limit=None,
            concurrency_limit=None,
            target_override_id=None,
            effective_before=(None, None, None),
            effective_after=(None, None, None),
            key_hash=key_hash,
            intent_hash=intent_hash,
            request_id=request_id,
            trace_id=trace_id,
            created_at=timestamp,
        )
        _update_anomaly_status(
            connection,
            context.user.tenant_id,
            str(anomaly["cost_anomaly_id"]),
            expected=status_before,
            target="dismissed",
            updated_at=anomaly_updated_at,
        )
        return (
            _load_action(connection, context.user.tenant_id, action_id),
            {**base, "status": status_before},
            {**base, "status": "dismissed", "dismiss_reason": reason},
            "cost_anomaly.dismissed",
        )

    if action == "apply_temporary_rate_limit":
        return _apply_temporary_limit(
            connection,
            context,
            anomaly,
            canonical,
            now,
            key_hash=key_hash,
            intent_hash=intent_hash,
            request_id=request_id,
            trace_id=trace_id,
        )

    if action == "release_temporary_rate_limit":
        return _release_temporary_limit(
            connection,
            context,
            anomaly,
            now,
            key_hash=key_hash,
            intent_hash=intent_hash,
            request_id=request_id,
            trace_id=trace_id,
        )
    # DTO 枚举之外的动作表示内部调用绕过了 wire 校验，必须再次失败关闭。
    raise ApiError("cost_anomaly.action_invalid")


def _apply_temporary_limit(
    connection: sqlite3.Connection,
    context: AuthContext,
    anomaly: sqlite3.Row,
    canonical: dict[str, object],
    now: datetime,
    *,
    key_hash: str,
    intent_hash: str,
    request_id: str,
    trace_id: str,
) -> tuple[sqlite3.Row, dict[str, object], dict[str, object], str]:
    """校验当前 Key 归属和收紧效果，再原子创建 apply action 与独立 overlay。"""

    if anomaly["scope_type"] != "virtual_key" or not anomaly["virtual_key_id"]:
        # 租户级异常没有单一 Key 可限流，禁止创建归属含糊的临时覆盖。
        raise ApiError("cost_anomaly.mitigation_not_applicable")
    tenant_id = context.user.tenant_id
    anomaly_id = str(anomaly["cost_anomaly_id"])
    active = _source_override(connection, tenant_id, anomaly_id)
    if active is not None and _override_effective(active, now) == "active":
        # 同一异常只允许一个活动覆盖，防止重复 apply 形成不可解释的限额叠加。
        raise ApiError("cost_anomaly.mitigation_active")
    status_before = str(anomaly["status"])
    if status_before not in {"open", "acknowledged"}:
        # 只有待处理或已确认异常可以进入缓解，已关闭状态保持不可逆。
        raise ApiError("cost_anomaly.action_conflict")
    if now > _parse_db_time(anomaly["mitigation_eligible_until"]):
        # 缓解资格按真实服务时钟判断，不能用单调行版本时间延长有效窗口。
        raise ApiError("cost_anomaly.mitigation_expired")
    virtual_key_id = str(anomaly["virtual_key_id"])
    key = connection.execute(
        """
        SELECT virtual_key_id, environment_id, status
        FROM virtual_keys
        WHERE tenant_id=? AND virtual_key_id=?
        """,
        (tenant_id, virtual_key_id),
    ).fetchone()
    if (
        key is None
        or key["status"] != "active"
        or key["environment_id"] != anomaly["environment_id"]
    ):
        # Key 被禁用、删除或改绑都不能继续套用历史异常的临时处置。
        raise ApiError("cost_anomaly.mitigation_key_unavailable")

    proposed = (
        canonical.get("rpm_limit"),
        canonical.get("tpm_limit"),
        canonical.get("concurrency_limit"),
    )
    before = _effective_limits(connection, tenant_id, virtual_key_id, now)
    tightened = any(
        value is not None and (current is None or int(value) < current)
        for value, current in zip(proposed, before, strict=True)
    )
    if not tightened:
        # 至少一个维度必须实际收紧当前有效限额，禁止无效果覆盖污染治理历史。
        raise ApiError("cost_anomaly.mitigation_no_effect")
    after = tuple(
        current
        if value is None
        else int(value)
        if current is None
        else min(current, int(value))
        for value, current in zip(proposed, before, strict=True)
    )
    duration = int(canonical["duration_minutes"])
    starts_at = _to_db_time(now)
    expires_at = _to_db_time(now + timedelta(minutes=duration))
    # starts/expires 必须忠实表示真实缓解窗口；仅 anomaly 行版本时间需要在当前
    # updated_at 基础上递增，不能为了满足触发器而把覆盖开始时间推过有效期。
    anomaly_updated_at = _monotonic_row_time(anomaly["updated_at"], now)
    action_id = _insert_action(
        connection,
        context,
        anomaly,
        action_type="apply_temporary_rate_limit",
        status_before=status_before,
        status_after="mitigated",
        dismiss_reason=None,
        duration_minutes=duration,
        rpm_limit=_optional_integer(proposed[0]),
        tpm_limit=_optional_integer(proposed[1]),
        concurrency_limit=_optional_integer(proposed[2]),
        target_override_id=None,
        effective_before=before,
        effective_after=after,
        key_hash=key_hash,
        intent_hash=intent_hash,
        request_id=request_id,
        trace_id=trace_id,
        created_at=starts_at,
    )
    # 先冻结 anomaly 状态，再插入引用 apply action 的 overlay；两步仍在同一事务内。
    _update_anomaly_status(
        connection,
        tenant_id,
        anomaly_id,
        expected=status_before,
        target="mitigated",
        updated_at=anomaly_updated_at,
    )
    override_id = f"trl_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO temporary_rate_limit_overrides(
          temporary_rate_limit_override_id, tenant_id, virtual_key_id,
          source_type, source_id, source_action_id, release_action_id,
          rpm_limit, tpm_limit, concurrency_limit, status,
          starts_at, expires_at, created_by,
          released_by, released_at,
          operation_request_id, operation_trace_id, created_at, updated_at
        )
        VALUES(
          ?, ?, ?, 'cost_anomaly', ?, ?, NULL, ?, ?, ?, 'active',
          ?, ?, ?, NULL, NULL, ?, ?, ?, ?
        )
        """,
        (
            override_id,
            tenant_id,
            virtual_key_id,
            anomaly_id,
            action_id,
            proposed[0],
            proposed[1],
            proposed[2],
            starts_at,
            expires_at,
            context.user.user_id,
            request_id,
            trace_id,
            starts_at,
            starts_at,
        ),
    )
    base = _audit_base(anomaly)
    return (
        _load_action(connection, tenant_id, action_id),
        {**base, "status": status_before},
        {
            **base,
            "status": "mitigated",
            "temporary_rate_limit_override_id": override_id,
            "rpm_limit": proposed[0],
            "tpm_limit": proposed[1],
            "concurrency_limit": proposed[2],
            "effective_status": "active",
        },
        "cost_anomaly.temporary_rate_limit_applied",
    )


def _release_temporary_limit(
    connection: sqlite3.Connection,
    context: AuthContext,
    anomaly: sqlite3.Row,
    now: datetime,
    *,
    key_hash: str,
    intent_hash: str,
    request_id: str,
    trace_id: str,
) -> tuple[sqlite3.Row, dict[str, object], dict[str, object], str]:
    """只释放当前 anomaly 尚有效的 overlay，并保留 mitigated 历史状态。"""

    if anomaly["scope_type"] != "virtual_key" or not anomaly["virtual_key_id"]:
        # release 必须指向先前 Key 级缓解，租户级异常不存在可释放的覆盖。
        raise ApiError("cost_anomaly.mitigation_not_applicable")
    if anomaly["status"] != "mitigated":
        # 未进入 mitigated 的异常不能伪造释放动作或追加无效审计记录。
        raise ApiError("cost_anomaly.action_conflict")
    tenant_id = context.user.tenant_id
    anomaly_id = str(anomaly["cost_anomaly_id"])
    override = _source_override(connection, tenant_id, anomaly_id)
    if override is None or _override_effective(override, now) != "active":
        # 缺失、已释放或已到期覆盖都不能再次释放，保持单次 CAS 语义。
        raise ApiError("cost_anomaly.mitigation_not_active")
    override_id = str(override["temporary_rate_limit_override_id"])
    virtual_key_id = str(anomaly["virtual_key_id"])
    before = _effective_limits(connection, tenant_id, virtual_key_id, now)
    after = _effective_limits(
        connection,
        tenant_id,
        virtual_key_id,
        now,
        exclude_override_id=override_id,
    )
    timestamp = _to_db_time(now)
    action_id = _insert_action(
        connection,
        context,
        anomaly,
        action_type="release_temporary_rate_limit",
        status_before="mitigated",
        status_after="mitigated",
        dismiss_reason=None,
        duration_minutes=None,
        rpm_limit=None,
        tpm_limit=None,
        concurrency_limit=None,
        target_override_id=override_id,
        effective_before=before,
        effective_after=after,
        key_hash=key_hash,
        intent_hash=intent_hash,
        request_id=request_id,
        trace_id=trace_id,
        created_at=timestamp,
    )
    updated = connection.execute(
        """
        UPDATE temporary_rate_limit_overrides
        SET status='released', release_action_id=?, released_by=?,
            released_at=?, updated_at=?
        WHERE tenant_id=? AND temporary_rate_limit_override_id=?
          AND source_id=? AND status='active'
          AND unixepoch(starts_at)<=unixepoch(?)
          AND unixepoch(expires_at)>unixepoch(?)
        """,
        (
            action_id,
            context.user.user_id,
            timestamp,
            timestamp,
            tenant_id,
            override_id,
            anomaly_id,
            timestamp,
            timestamp,
        ),
    )
    if updated.rowcount != 1:
        # 覆盖在事务竞争中失效时不提交孤立 release action，整笔事务回滚。
        raise ApiError("cost_anomaly.mitigation_not_active")
    base = _audit_base(anomaly)
    limits = {
        "rpm_limit": override["rpm_limit"],
        "tpm_limit": override["tpm_limit"],
        "concurrency_limit": override["concurrency_limit"],
    }
    return (
        _load_action(connection, tenant_id, action_id),
        {
            **base,
            "status": "mitigated",
            "temporary_rate_limit_override_id": override_id,
            **limits,
            "effective_status": "active",
        },
        {
            **base,
            "status": "mitigated",
            "temporary_rate_limit_override_id": override_id,
            **limits,
            "effective_status": "released",
        },
        "cost_anomaly.temporary_rate_limit_released",
    )


def _insert_action(
    connection: sqlite3.Connection,
    context: AuthContext,
    anomaly: sqlite3.Row,
    *,
    action_type: str,
    status_before: str,
    status_after: str,
    dismiss_reason: str | None,
    duration_minutes: int | None,
    rpm_limit: int | None,
    tpm_limit: int | None,
    concurrency_limit: int | None,
    target_override_id: str | None,
    effective_before: tuple[int | None, int | None, int | None],
    effective_after: tuple[int | None, int | None, int | None],
    key_hash: str,
    intent_hash: str,
    request_id: str,
    trace_id: str,
    created_at: str,
) -> str:
    """追加一条不可变 action；原始幂等键永不进入数据库。"""

    action_id = f"caa_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO cost_anomaly_actions(
          cost_anomaly_action_id, tenant_id, cost_anomaly_id, actor_user_id,
          action_type, status_before, status_after, dismiss_reason,
          duration_minutes, rpm_limit, tpm_limit, concurrency_limit,
          target_override_id,
          effective_rpm_before, effective_rpm_after,
          effective_tpm_before, effective_tpm_after,
          effective_concurrency_before, effective_concurrency_after,
          operation_request_id, operation_trace_id,
          idempotency_key_sha256, intent_sha256, created_at
        )
        VALUES(
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            action_id,
            context.user.tenant_id,
            anomaly["cost_anomaly_id"],
            context.user.user_id,
            action_type,
            status_before,
            status_after,
            dismiss_reason,
            duration_minutes,
            rpm_limit,
            tpm_limit,
            concurrency_limit,
            target_override_id,
            effective_before[0],
            effective_after[0],
            effective_before[1],
            effective_after[1],
            effective_before[2],
            effective_after[2],
            request_id,
            trace_id,
            key_hash,
            intent_hash,
            created_at,
        ),
    )
    return action_id


def _update_anomaly_status(
    connection: sqlite3.Connection,
    tenant_id: str,
    anomaly_id: str,
    *,
    expected: str,
    target: str,
    updated_at: str,
) -> None:
    """以 tenant、ID 和 expected status 三重条件执行受控状态迁移。"""

    updated = connection.execute(
        """
        UPDATE cost_anomalies
        SET status=?, updated_at=?
        WHERE tenant_id=? AND cost_anomaly_id=? AND status=?
        """,
        (target, updated_at, tenant_id, anomaly_id, expected),
    )
    if updated.rowcount != 1:
        # tenant、资源 ID 与期望状态任一不匹配都视为并发冲突，不能越权迁移。
        raise ApiError("cost_anomaly.action_conflict")


def _effective_limits(
    connection: sqlite3.Connection,
    tenant_id: str,
    virtual_key_id: str,
    now: datetime,
    *,
    exclude_override_id: str | None = None,
) -> tuple[int | None, int | None, int | None]:
    """读取永久规则和其他有效 overlay，并对每个维度独立取最小值。"""

    values: list[sqlite3.Row] = []
    permanent = connection.execute(
        """
        SELECT rpm_limit, tpm_limit, concurrency_limit
        FROM virtual_key_rate_limit_rules
        WHERE tenant_id=? AND virtual_key_id=? AND status='active'
        """,
        (tenant_id, virtual_key_id),
    ).fetchone()
    if permanent is not None:
        values.append(permanent)
    clauses = [
        "tenant_id=?",
        "virtual_key_id=?",
        "status='active'",
        "unixepoch(starts_at)<=unixepoch(?)",
        "unixepoch(expires_at)>unixepoch(?)",
    ]
    timestamp = _to_db_time(now)
    params: list[object] = [tenant_id, virtual_key_id, timestamp, timestamp]
    if exclude_override_id is not None:
        clauses.append("temporary_rate_limit_override_id!=?")
        params.append(exclude_override_id)
    values.extend(
        connection.execute(
            f"""
            SELECT rpm_limit, tpm_limit, concurrency_limit
            FROM temporary_rate_limit_overrides
            WHERE {' AND '.join(clauses)}
            ORDER BY temporary_rate_limit_override_id ASC
            """,
            params,
        ).fetchall()
    )
    return (
        _minimum_dimension(values, "rpm_limit"),
        _minimum_dimension(values, "tpm_limit"),
        _minimum_dimension(values, "concurrency_limit"),
    )


def _minimum_dimension(rows: Iterable[sqlite3.Row], field: str) -> int | None:
    """恢复一个治理维度的最小正整数；损坏数据库值必须失败关闭。"""

    values: list[int] = []
    for row in rows:
        value = row[field]
        if value is None:
            continue
        if type(value) is not int or not 1 <= value <= MAX_LIMIT_VALUE:
            # 持久限额不是合法正整数时禁止推导有效策略，避免放宽或错误收紧。
            raise ApiError("cost_anomaly.unavailable")
        values.append(value)
    return min(values) if values else None

def _load_anomaly(
    connection: sqlite3.Connection,
    tenant_id: str,
    anomaly_id: str,
) -> sqlite3.Row | None:
    """按认证 tenant 与 anomaly ID 联合加载安全展示行。"""

    return connection.execute(
        """
        SELECT
          ca.*,
          t.name AS tenant_name,
          e.name AS environment_name,
          vk.name AS virtual_key_name,
          vk.key_prefix AS virtual_key_prefix
        FROM cost_anomalies ca
        JOIN tenants t ON t.tenant_id=ca.tenant_id
        JOIN environments e
          ON e.tenant_id=ca.tenant_id AND e.environment_id=ca.environment_id
        LEFT JOIN virtual_keys vk
          ON vk.tenant_id=ca.tenant_id AND vk.virtual_key_id=ca.virtual_key_id
        WHERE ca.tenant_id=? AND ca.cost_anomaly_id=?
        """,
        (tenant_id, anomaly_id),
    ).fetchone()


def _anomaly_info(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: datetime,
) -> CostAnomalyInfo:
    """把持久证据、当前 overlay 与 append-only actions 转为安全 DTO。"""

    current_micros = _canonical_integer_text(row["current_cost_micros"])
    baseline_micros = _canonical_integer_text(row["baseline_total_micros"])
    baseline_count = _positive_integer(row["baseline_bucket_count"])
    baseline_average = str(int(baseline_micros) // baseline_count)
    override = _source_override(
        connection,
        str(row["tenant_id"]),
        str(row["cost_anomaly_id"]),
    )
    actions = connection.execute(
        """
        SELECT *
        FROM cost_anomaly_actions
        WHERE tenant_id=? AND cost_anomaly_id=?
        ORDER BY created_at ASC, cost_anomaly_action_id ASC
        """,
        (row["tenant_id"], row["cost_anomaly_id"]),
    ).fetchall()
    status = str(row["status"])
    eligible_until = _parse_db_time(row["mitigation_eligible_until"])
    scope_label = (
        str(row["virtual_key_name"] or row["virtual_key_prefix"] or row["scope_id"])
        if row["scope_type"] == "virtual_key"
        else str(row["tenant_name"] or row["tenant_id"])
    )
    return CostAnomalyInfo(
        cost_anomaly_id=str(row["cost_anomaly_id"]),
        tenant_id=str(row["tenant_id"]),
        environment_id=str(row["environment_id"]),
        environment_name=str(row["environment_name"]),
        rule_type=str(row["rule_type"]),
        severity=str(row["severity"]),
        status=status,
        scope_type=str(row["scope_type"]),
        scope_id=str(row["scope_id"]),
        scope_label=scope_label,
        virtual_key_id=row["virtual_key_id"],
        virtual_key_name=row["virtual_key_name"],
        virtual_key_prefix=row["virtual_key_prefix"],
        top_virtual_key_id=row["top_virtual_key_id"],
        project_id_snapshot=row["project_id_snapshot"],
        application_id_snapshot=row["application_id_snapshot"],
        window_start=_parse_db_time(row["window_start"]),
        window_end=_parse_db_time(row["window_end"]),
        baseline_start=_parse_db_time(row["baseline_start"]),
        baseline_end=_parse_db_time(row["baseline_end"]),
        current_cost_micros=current_micros,
        current_cost_cny=_micros_to_cny(current_micros),
        baseline_total_micros=baseline_micros,
        baseline_total_cny=_micros_to_cny(baseline_micros),
        baseline_average_micros=baseline_average,
        baseline_average_cny=_micros_to_cny(baseline_average),
        baseline_bucket_count=baseline_count,
        observed_ratio_millis=(
            _canonical_integer_text(row["observed_ratio_millis"])
            if row["observed_ratio_millis"] is not None
            else None
        ),
        threshold_multiplier_millis=_positive_integer(
            row["threshold_multiplier_millis"]
        ),
        minimum_cost_micros=_canonical_integer_text(row["minimum_cost_micros"]),
        source_entry_count=_positive_integer(row["source_entry_count"]),
        estimated_entry_count=_non_negative_integer(row["estimated_entry_count"]),
        legacy_entry_count=_non_negative_integer(row["legacy_entry_count"]),
        affected_virtual_key_count=_positive_integer(
            row["affected_virtual_key_count"]
        ),
        representative_request_id=row["representative_request_id"],
        representative_trace_id=row["representative_trace_id"],
        recommended_action=str(row["recommended_action"]),
        detection_version=str(row["detection_version"]),
        event_dispatch_status=str(row["event_dispatch_status"]),
        detected_at=_parse_db_time(row["detected_at"]),
        mitigation_eligible_until=eligible_until,
        mitigation_eligible=(
            row["scope_type"] == "virtual_key"
            and status in {"open", "acknowledged"}
            and now <= eligible_until
        ),
        temporary_rate_limit_override=(
            _override_info(override, now) if override is not None else None
        ),
        actions=[_action_info(action) for action in actions],
    )


def _action_response(
    connection: sqlite3.Connection,
    anomaly_row: sqlite3.Row,
    action_row: sqlite3.Row,
    now: datetime,
    *,
    idempotent_replay: bool,
    request_id: str,
    trace_id: str,
) -> CostAnomalyActionResponse:
    """构造首次提交或 replay 的当前 tenant-qualified 动作响应。"""

    anomaly = _anomaly_info(connection, anomaly_row, now)
    return CostAnomalyActionResponse(
        cost_anomaly=anomaly,
        action=_action_info(action_row),
        temporary_rate_limit_override=anomaly.temporary_rate_limit_override,
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _action_info(row: sqlite3.Row) -> CostAnomalyActionInfo:
    """把不可变 action 行转换为不含幂等摘要的公开 DTO。"""

    return CostAnomalyActionInfo(
        cost_anomaly_action_id=str(row["cost_anomaly_action_id"]),
        action_type=str(row["action_type"]),
        status_before=str(row["status_before"]),
        status_after=str(row["status_after"]),
        dismiss_reason=row["dismiss_reason"],
        duration_minutes=row["duration_minutes"],
        rpm_limit=row["rpm_limit"],
        tpm_limit=row["tpm_limit"],
        concurrency_limit=row["concurrency_limit"],
        effective_rpm_before=row["effective_rpm_before"],
        effective_rpm_after=row["effective_rpm_after"],
        effective_tpm_before=row["effective_tpm_before"],
        effective_tpm_after=row["effective_tpm_after"],
        effective_concurrency_before=row["effective_concurrency_before"],
        effective_concurrency_after=row["effective_concurrency_after"],
        target_override_id=row["target_override_id"],
        actor_user_id=str(row["actor_user_id"]),
        operation_request_id=str(row["operation_request_id"]),
        operation_trace_id=str(row["operation_trace_id"]),
        created_at=_parse_db_time(row["created_at"]),
    )


def _override_info(
    row: sqlite3.Row,
    now: datetime,
) -> TemporaryRateLimitOverrideInfo:
    """按持久状态和服务器时刻派生 active/released/expired 展示状态。"""

    return TemporaryRateLimitOverrideInfo(
        temporary_rate_limit_override_id=str(
            row["temporary_rate_limit_override_id"]
        ),
        virtual_key_id=str(row["virtual_key_id"]),
        rpm_limit=row["rpm_limit"],
        tpm_limit=row["tpm_limit"],
        concurrency_limit=row["concurrency_limit"],
        status=str(row["status"]),
        effective_status=_override_effective(row, now),
        starts_at=_parse_db_time(row["starts_at"]),
        expires_at=_parse_db_time(row["expires_at"]),
        released_at=(
            _parse_db_time(row["released_at"])
            if row["released_at"] is not None
            else None
        ),
    )


def _source_override(
    connection: sqlite3.Connection,
    tenant_id: str,
    anomaly_id: str,
) -> sqlite3.Row | None:
    """按 tenant 与 source anomaly 联合读取唯一临时覆盖。"""

    return connection.execute(
        """
        SELECT *
        FROM temporary_rate_limit_overrides
        WHERE tenant_id=? AND source_type='cost_anomaly' AND source_id=?
        """,
        (tenant_id, anomaly_id),
    ).fetchone()


def _override_effective(row: sqlite3.Row, now: datetime) -> str:
    """用服务端 UTC 判断覆盖的当前有效状态，不信任客户端自报。"""

    if row["status"] == "released":
        return "released"
    if now >= _parse_db_time(row["expires_at"]):
        return "expired"
    return "active"


def _load_action(
    connection: sqlite3.Connection,
    tenant_id: str,
    action_id: str,
) -> sqlite3.Row:
    """按 tenant 加载刚写入或幂等恢复的 action，缺失时失败关闭。"""

    row = connection.execute(
        """
        SELECT *
        FROM cost_anomaly_actions
        WHERE tenant_id=? AND cost_anomaly_action_id=?
        """,
        (tenant_id, action_id),
    ).fetchone()
    if row is None:
        # 刚写入或幂等引用的 action 缺失表示事务内持久事实不一致，必须失败关闭。
        raise ApiError("cost_anomaly.unavailable")
    return row


def _cost_anomaly_summary(
    connection: sqlite3.Connection,
    tenant_id: str,
    now: datetime,
) -> CostAnomalySummary:
    """在同一只读快照中汇总异常状态、有效覆盖和最新 final detector run。"""

    counts = connection.execute(
        """
        SELECT
          SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_count,
          SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) AS critical_count,
          SUM(CASE WHEN status='mitigated' THEN 1 ELSE 0 END) AS mitigated_count
        FROM cost_anomalies
        WHERE tenant_id=?
        """,
        (tenant_id,),
    ).fetchone()
    timestamp = _to_db_time(now)
    overrides = connection.execute(
        """
        SELECT COUNT(*) AS value
        FROM temporary_rate_limit_overrides
        WHERE tenant_id=? AND status='active'
          AND unixepoch(starts_at)<=unixepoch(?)
          AND unixepoch(expires_at)>unixepoch(?)
        """,
        (tenant_id, timestamp, timestamp),
    ).fetchone()
    run = connection.execute(
        """
        SELECT status, error_code, window_end
        FROM cost_anomaly_detection_runs
        WHERE tenant_id=? AND status IN ('completed', 'partial', 'failed')
        ORDER BY window_end DESC, evaluation_revision DESC, detection_run_id DESC
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    if run is None:
        detector_status = "idle"
        detector_error = None
        latest_window_end = None
    else:
        detector_status = "healthy" if run["status"] == "completed" else "degraded"
        detector_error = run["error_code"]
        latest_window_end = _parse_db_time(run["window_end"])
    return CostAnomalySummary(
        open_count=_non_negative_integer(counts["open_count"] or 0),
        critical_count=_non_negative_integer(counts["critical_count"] or 0),
        mitigated_count=_non_negative_integer(counts["mitigated_count"] or 0),
        active_override_count=_non_negative_integer(overrides["value"] or 0),
        detector_status=detector_status,
        detector_error_code=detector_error,
        latest_window_end=latest_window_end,
    )


def _audit_base(row: sqlite3.Row) -> dict[str, object]:
    """返回 CostAnomaly Audit 的固定非敏感资源字段。"""

    return {
        "rule_type": str(row["rule_type"]),
        "severity": str(row["severity"]),
        "environment_id": str(row["environment_id"]),
        "scope_type": str(row["scope_type"]),
        "scope_id": str(row["scope_id"]),
    }

def _canonical_action(payload: CostAnomalyActionRequest) -> dict[str, object]:
    """校验动作精确字段集合并返回稳定幂等表示。

    Pydantic 会把省略字段和显式 ``null`` 都恢复成 None，因此必须结合
    ``model_fields_set`` 判断原始意图。acknowledge、dismiss 和 release 拒绝任何
    无关 null；apply 的三个可选限流维度允许省略或显式 null，但至少一维必须是
    真正的严格正整数。
    """

    action = str(payload.action)
    fields = set(payload.model_fields_set)
    reason = payload.dismiss_reason
    duration = payload.duration_minutes
    limits = (payload.rpm_limit, payload.tpm_limit, payload.concurrency_limit)
    if action == "acknowledge":
        if fields != {"action"}:
            # acknowledge 不接受无关字段或显式 null，保证幂等摘要只有唯一表达。
            raise ApiError("cost_anomaly.action_invalid")
        return {"action": action}
    if action == "dismiss":
        if fields != {"action", "dismiss_reason"} or reason not in VALID_DISMISS_REASONS:
            # dismiss 必须精确携带一个受控原因，禁止省略或混入限流字段。
            raise ApiError("cost_anomaly.action_invalid")
        return {"action": action, "dismiss_reason": reason}
    if action == "apply_temporary_rate_limit":
        allowed = {
            "action",
            "duration_minutes",
            "rpm_limit",
            "tpm_limit",
            "concurrency_limit",
        }
        if (
            not {"action", "duration_minutes"}.issubset(fields)
            or not fields.issubset(allowed)
            or type(duration) is not int
            or not 5 <= duration <= 1440
            or not any(value is not None for value in limits)
        ):
            # apply 必须具备合法时长且至少提交一个限流维度，未知字段一律拒绝。
            raise ApiError("cost_anomaly.action_invalid")
        for value in limits:
            if value is not None and (
                type(value) is not int or not 1 <= value <= MAX_LIMIT_VALUE
            ):
                # 限流值只允许范围内严格整数，bool、float 和数字字符串不能替代。
                raise ApiError("cost_anomaly.action_invalid")
        return {
            "action": action,
            "duration_minutes": duration,
            "rpm_limit": limits[0],
            "tpm_limit": limits[1],
            "concurrency_limit": limits[2],
        }
    if action == "release_temporary_rate_limit":
        if fields != {"action"}:
            # release 的目标只能从 anomaly 当前覆盖解析，客户端不得注入资源 ID 或空字段。
            raise ApiError("cost_anomaly.action_invalid")
        return {"action": action}
    # 未知动作表示服务被绕过类型合同调用，仍须返回统一动作错误。
    raise ApiError("cost_anomaly.action_invalid")

def _normalize_idempotency_key(value: object) -> str:
    """在服务边界再次校验原始幂等键并仅返回受限 ASCII 文本。

    路由已执行同样的 raw header 校验，但服务也可能被后台任务或专项测试直接调用；
    这里拒绝非字符串、非 ASCII、控制字符和越界长度，避免 ``encode`` 异常穿透为
    未统一的 500。原始值只在当前调用栈内参与 SHA-256，不写入数据库、日志或响应。
    """

    if type(value) is not str or not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        # 服务直调也必须满足受限 ASCII 键合同，防止编码异常或敏感原文落库。
        raise ApiError("cost_anomaly.action_invalid")
    return value


def _normalize_filters(
    *,
    status: str | None,
    rule_type: str | None,
    severity: str | None,
    environment_id: str | None,
    virtual_key_id: str | None,
    from_time: str | None,
    to_time: str | None,
    limit: str | None,
) -> _ListFilters:
    """规范化列表筛选，任何未知枚举、含糊时间或非整数字符串统一返回 422。"""

    normalized_status = _optional_enum(status, VALID_STATUSES)
    normalized_rule = _optional_enum(rule_type, VALID_RULE_TYPES)
    normalized_severity = _optional_enum(severity, VALID_SEVERITIES)
    normalized_environment = _optional_environment_id(environment_id)
    normalized_key = _optional_virtual_key_id(virtual_key_id)
    start = _parse_optional_time(from_time)
    end = _parse_optional_time(to_time)
    if start is not None and end is not None and end <= start:
        # 时间范围必须严格递增，拒绝空区间和反向区间的含糊统计语义。
        raise ApiError("cost_anomaly.query_invalid")
    if limit is None or limit == "":
        page_size = DEFAULT_PAGE_SIZE
    elif not limit.isascii() or not limit.isdigit() or limit.startswith("0"):
        # 分页大小只接受 canonical ASCII 正整数，拒绝符号、空白和前导零。
        raise ApiError("cost_anomaly.query_invalid")
    else:
        try:
            page_size = int(limit)
        # 极端输入的转换失败统一映射，不能向调用方暴露 Python 转换正文。
        except ValueError as exc:
            raise ApiError("cost_anomaly.query_invalid") from exc
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            # 页大小必须处于公开上限内，避免单请求扩大租户查询和 DTO 物化成本。
            raise ApiError("cost_anomaly.query_invalid")
    return _ListFilters(
        status=normalized_status,
        rule_type=normalized_rule,
        severity=normalized_severity,
        environment_id=normalized_environment,
        virtual_key_id=normalized_key,
        from_time=start,
        to_time=end,
        limit=page_size,
    )


def _validate_filter_owners(
    connection: sqlite3.Connection,
    tenant_id: str,
    filters: _ListFilters,
) -> None:
    """在当前 tenant 快照内验证 Environment 与 Key；跨租户和不存在保持同形 404。"""

    if filters.environment_id is not None:
        row = connection.execute(
            "SELECT 1 FROM environments WHERE tenant_id=? AND environment_id=?",
            (tenant_id, filters.environment_id),
        ).fetchone()
        if row is None:
            # Environment 必须归属认证 tenant；跨租户与不存在保持同形避免枚举。
            raise ApiError("cost_anomaly.not_found")
    if filters.virtual_key_id is not None:
        row = connection.execute(
            "SELECT 1 FROM virtual_keys WHERE tenant_id=? AND virtual_key_id=?",
            (tenant_id, filters.virtual_key_id),
        ).fetchone()
        if row is None:
            # Virtual Key 同样按 tenant 联合验证，不能只依赖 UUID 的全局唯一假设。
            raise ApiError("cost_anomaly.not_found")


def _list_where(
    tenant_id: str,
    filters: _ListFilters,
    cursor: _CursorState,
) -> tuple[list[str], list[object]]:
    """生成仅含内部白名单列名的 snapshot SQL 条件和参数。"""

    clauses = ["ca.tenant_id=?", "ca.anomaly_sequence<=?"]
    params: list[object] = [tenant_id, cursor.snapshot_sequence]
    for column, value in (
        ("status", filters.status),
        ("rule_type", filters.rule_type),
        ("severity", filters.severity),
        ("environment_id", filters.environment_id),
        ("virtual_key_id", filters.virtual_key_id),
    ):
        if value is not None:
            clauses.append(f"ca.{column}=?")
            params.append(value)
    if filters.from_time is not None:
        clauses.append("unixepoch(ca.window_start)>=unixepoch(?)")
        params.append(_to_db_time(filters.from_time))
    if filters.to_time is not None:
        clauses.append("unixepoch(ca.window_start)<unixepoch(?)")
        params.append(_to_db_time(filters.to_time))
    return clauses, params


def _filter_hash(filters: _ListFilters) -> str:
    """对规范化筛选生成稳定摘要，cursor 不保存可直接展示的筛选正文。"""

    value = {
        "status": filters.status,
        "rule_type": filters.rule_type,
        "severity": filters.severity,
        "environment_id": filters.environment_id,
        "virtual_key_id": filters.virtual_key_id,
        "from_time": _to_db_time(filters.from_time) if filters.from_time else None,
        "to_time": _to_db_time(filters.to_time) if filters.to_time else None,
        "limit": filters.limit,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _encode_cursor(
    config: AppConfig,
    tenant_id: str,
    filter_hash: str,
    *,
    snapshot_sequence: int,
    before_sequence: int,
) -> str:
    """签发绑定租户、筛选和快照的 URL-safe opaque cursor。"""

    payload = json.dumps(
        {
            "v": CURSOR_VERSION,
            "tenant": tenant_id,
            "filter": filter_hash,
            "snapshot": snapshot_sequence,
            "before": before_sequence,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(
        config.auth_token_secret.encode("utf-8"),
        CURSOR_DOMAIN + b"\x00" + payload,
        hashlib.sha256,
    ).digest()
    return f"{_base64_encode(payload)}.{_base64_encode(signature)}"


def _decode_cursor(
    config: AppConfig,
    cursor: str,
    tenant_id: str,
    filter_hash: str,
) -> _CursorState:
    """验证 cursor 的签名、规范编码、租户筛选绑定及快照序列边界，拒绝跨域复用。"""

    try:
        if not 20 <= len(cursor) <= 2048 or cursor.count(".") != 1:
            # 长度和分段数先行限界，避免无界 Base64 解码及多种结构解释。
            raise ValueError("cursor shape")
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload = _base64_decode(encoded_payload)
        signature = _base64_decode(encoded_signature)
        expected = hmac.new(
            config.auth_token_secret.encode("utf-8"),
            CURSOR_DOMAIN + b"\x00" + payload,
            hashlib.sha256,
        ).digest()
        if len(signature) != hashlib.sha256().digest_size or not hmac.compare_digest(
            signature,
            expected,
        ):
            # 签名长度或常量时间比对失败说明 cursor 被篡改或来自其他签名域。
            raise ValueError("cursor signature")
        parsed = json.loads(payload.decode("ascii"))
        if not isinstance(parsed, dict) or set(parsed) != {
            "v",
            "tenant",
            "filter",
            "snapshot",
            "before",
        }:
            # 字段集合必须精确匹配版本合同，未知或缺失字段都不能静默兼容。
            raise ValueError("cursor fields")
        if (
            parsed["v"] != CURSOR_VERSION
            or parsed["tenant"] != tenant_id
            or parsed["filter"] != filter_hash
            or type(parsed["snapshot"]) is not int
            or type(parsed["before"]) is not int
            or parsed["snapshot"] < 0
            or not 1 <= parsed["before"] <= parsed["snapshot"] + 1
        ):
            # 版本、tenant、筛选摘要和序列边界必须全部匹配当前查询。
            raise ValueError("cursor binding")
        canonical = json.dumps(
            parsed,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if canonical != payload:
            # 只接受唯一 canonical JSON 编码，防止相同语义产生多个签名表示。
            raise ValueError("cursor canonical form")
        return _CursorState(
            snapshot_sequence=parsed["snapshot"],
            before_sequence=parsed["before"],
        )
    # 解码、类型或签名异常统一作为查询错误，不返回内部签名材料与解析正文。
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError("cost_anomaly.query_invalid") from exc


def _base64_encode(value: bytes) -> str:
    """生成无填充且可逆验证的 URL-safe Base64，供不透明 cursor 规范编码使用。"""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64_decode(value: str) -> bytes:
    """严格解码 canonical URL-safe Base64，拒绝空值、padding 和替代表示。"""

    if (
        not value
        or "=" in value
        or not value.isascii()
        or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for char in value
        )
    ):
        # 空值、填充、非 ASCII 或非 URL-safe 字符会产生替代编码，全部拒绝。
        raise ValueError("invalid base64")
    decoded = base64.urlsafe_b64decode(
        (value + "=" * (-len(value) % 4)).encode("ascii")
    )
    if _base64_encode(decoded) != value:
        # 解码后必须重编码为完全相同文本，禁止接受非规范等价表示。
        raise ValueError("non-canonical base64")
    return decoded

def _normalize_anomaly_id(value: object) -> str:
    """校验公开 anomaly ID；非法形状与不存在资源同形返回 404。"""

    normalized = str(value or "").strip()
    if not ANOMALY_ID_PATTERN.fullmatch(normalized):
        # 非规范 ID 与不存在资源保持同形，避免利用格式错误探测资源命名空间。
        raise ApiError("cost_anomaly.not_found")
    return normalized


def _optional_environment_id(value: str | None) -> str | None:
    """校验可选 env_ ID 形状，不允许控制字符、空白或大小写替代。"""

    if value is None:
        return None
    normalized = value.strip()
    if not re.fullmatch(r"env_[0-9a-f]{32}", normalized):
        # Environment ID 必须是唯一 canonical 形式，非法形状同样按资源不可见处理。
        raise ApiError("cost_anomaly.not_found")
    return normalized


def _optional_virtual_key_id(value: str | None) -> str | None:
    """校验现有 UUID Virtual Key ID 形状；tenant owner 由数据库再次验证。"""

    if value is None:
        return None
    normalized = value.strip()
    try:
        parsed = UUID(normalized)
    # UUID 解析异常不应暴露输入细节，跨租户和非法资源统一返回不可见。
    except (ValueError, AttributeError) as exc:
        raise ApiError("cost_anomaly.not_found") from exc
    if str(parsed) != normalized:
        # 大小写、花括号等等价表示会破坏 cursor 筛选绑定，只接受标准小写文本。
        raise ApiError("cost_anomaly.not_found")
    return normalized


def _optional_enum(value: str | None, allowed: frozenset[str]) -> str | None:
    """校验可选小写枚举；空字符串不是省略。"""

    if value is None:
        return None
    normalized = value.strip()
    if normalized not in allowed:
        # 枚举必须精确匹配服务端白名单，空值和大小写替代均视为非法查询。
        raise ApiError("cost_anomaly.query_invalid")
    return normalized


def _parse_optional_time(value: str | None) -> datetime | None:
    """解析带显式时区的 ISO-8601 时间；naive 时间统一拒绝。"""

    if value is None:
        return None
    if not value.strip():
        # 空白时间不是省略，必须显式拒绝以免不同客户端产生不同筛选语义。
        raise ApiError("cost_anomaly.query_invalid")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    # 时间解析失败统一映射为查询错误，不把原始输入或转换正文回显。
    except ValueError as exc:
        raise ApiError("cost_anomaly.query_invalid") from exc
    if parsed.tzinfo is None:
        # 查询时间必须携带显式时区，禁止按服务器本地时区进行隐式解释。
        raise ApiError("cost_anomaly.query_invalid")
    return parsed.astimezone(UTC)


def _micros_to_cny(value: str) -> str:
    """把 canonical 非负 micros 格式化为六位 CNY，不经过 float。"""

    amount = int(_canonical_integer_text(value))
    whole, fraction = divmod(amount, 1_000_000)
    return f"{whole}.{fraction:06d}"


def _canonical_integer_text(value: object) -> str:
    """恢复无前导零的非负十进制文本，拒绝 REAL、负数和替代编码。"""

    if type(value) is int:
        if value < 0:
            # 成本账本和计数不允许负值，损坏持久事实必须停止响应物化。
            raise ApiError("cost_anomaly.unavailable")
        normalized = str(value)
    elif type(value) is str:
        normalized = value
    else:
        # REAL、bool 与其他类型可能造成金额精度漂移，统一按数据库损坏处理。
        raise ApiError("cost_anomaly.unavailable")
    if (
        not normalized
        or len(normalized) > 40
        or not normalized.isascii()
        or not normalized.isdigit()
        or (len(normalized) > 1 and normalized.startswith("0"))
    ):
        # 金额文本必须是有界 canonical 十进制，拒绝前导零和替代编码。
        raise ApiError("cost_anomaly.unavailable")
    return normalized


def _positive_integer(value: object) -> int:
    """恢复严格正整数；bool 和数据库 REAL 均视为损坏事实。"""

    if type(value) is not int or value <= 0:
        # 正计数字段若为 bool、REAL 或非正数会破坏异常统计，必须失败关闭。
        raise ApiError("cost_anomaly.unavailable")
    return value


def _non_negative_integer(value: object) -> int:
    """恢复非负整数计数；bool 和数据库 REAL 均拒绝。"""

    if type(value) is not int or value < 0:
        # 汇总计数允许为零但不接受负数、布尔值或浮点近似。
        raise ApiError("cost_anomaly.unavailable")
    return value


def _optional_integer(value: object) -> int | None:
    """把已校验可选严格整数传入 SQL，保持 None 语义。"""

    if value is None:
        return None
    if type(value) is not int:
        # 可选限流值存在时必须是严格整数，禁止 bool 和自动类型转换。
        raise ApiError("cost_anomaly.action_invalid")
    return value


def _service_now(value: datetime | None) -> datetime:
    """返回 UTC 秒级服务端时刻；仅内部测试可以显式注入。"""

    current = value if value is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        # 状态与租约比较必须基于有时区时间，naive 输入会破坏有效期合同。
        raise ApiError("cost_anomaly.unavailable")
    return current.astimezone(UTC).replace(microsecond=0)


def _monotonic_row_time(persisted: object, now: datetime) -> str:
    """生成严格递增的 anomaly 行版本时间，同时不改变动作的真实业务时刻。

    参数：persisted 是事务内读取的当前 updated_at，now 是本次请求的秒级时钟。
    返回值：canonical UTC 数据库时间，至少比持久值晚一秒且不早于 now。
    关键约束：该值只写入 anomaly.updated_at；缓解资格、覆盖 starts/expires 和释放
    截止仍使用 now，避免单调行版本时钟越过业务有效期。
    """

    previous = _parse_db_time(persisted)
    current = _service_now(now)
    return _to_db_time(max(current, previous + timedelta(seconds=1)))


def _to_db_time(value: datetime) -> str:
    """编码 UTC 秒级时间，保证 SQLite unixepoch 与词典序一致。"""

    if value.tzinfo is None:
        # 数据库时间编码禁止猜测本地时区，所有调用都必须先提供显式时区。
        raise ApiError("cost_anomaly.unavailable")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: object) -> datetime:
    """解析数据库时间；损坏持久事实统一折叠为服务不可用。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 持久时间解析失败时隐藏原始数据库值，仅暴露稳定服务不可用错误。
    except (TypeError, ValueError) as exc:
        raise ApiError("cost_anomaly.unavailable") from exc
    if parsed.tzinfo is None:
        # 数据库事实必须包含显式时区，禁止按部署主机设置补齐语义。
        raise ApiError("cost_anomaly.unavailable")
    return parsed.astimezone(UTC)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 API 独享连接，并隐藏数据库 URL 与驱动异常。"""

    try:
        return connect_database(config.database_url)
    # 连接异常统一折叠，数据库 URL、文件路径及驱动正文都不得写入响应或日志。
    except DbError as exc:
        raise ApiError("cost_anomaly.unavailable") from exc


def _rollback_safely(connection: sqlite3.Connection) -> None:
    """尽力回滚；rollback 异常不得遮蔽稳定领域错误码。"""

    try:
        connection.rollback()
    # 回滚失败不得遮蔽原始领域错误，也不能将 SQLite 异常正文继续传播。
    except sqlite3.Error:
        return