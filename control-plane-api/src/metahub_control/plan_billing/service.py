"""套餐、试用、额度调整、分类报表与到期收敛服务。

所有写操作使用 ``BEGIN IMMEDIATE``，并在同一事务内重新认证 Session 与实时权限矩阵。
客户端不能提交 tenant、actor、Plan/Bucket ID、余额或结果 revision；这些 owner 全部从
认证上下文和数据库恢复。日志只记录不透明 ID、金额状态与统一错误码。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.quota import (
    MAX_CNY_MICROS,
    QUOTA_CONSUMPTION_ORDER,
    QUOTA_TYPES,
    canonical_quota_intent_sha256,
    format_cny_micros,
    parse_cny_micros,
)
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
from metahub_control.auth import AuthContext, require_transaction_permission

from .models import (
    PlanInfo,
    PlanResponse,
    QuotaAdjustmentResponse,
    QuotaAdjustmentSummary,
    QuotaBucketInfo,
    QuotaBucketsResponse,
    QuotaOverageSummary,
)

logger = logging.getLogger("metahub.control.plan_billing")


@dataclass(frozen=True, slots=True)
class _NormalizedPlanPatch:
    """保存严格套餐版本化意图，不包含租户、操作者或任何服务端 owner。"""

    expected_revision: int
    display_name: str
    base_tier: str
    status: str
    trial_ends_at: str | None


@dataclass(frozen=True, slots=True)
class _NormalizedAdjustment:
    """严格额度调整意图；金额已转换为 CNY 微元整数。"""

    quota_type: str
    direction: str
    amount_micros: int
    reason: str
    reason_sha256: str
    expected_bucket_revision: int
    payload_sha256: str


def get_plan(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> PlanResponse:
    """读取当前认证租户的 Plan 和固定四 Bucket；无 Plan 保持向后兼容。"""

    connection = _connect(config, "plan.unavailable")
    try:
        connection.execute("BEGIN DEFERRED")
        response = _plan_response(
            connection,
            tenant_id=context.user.tenant_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.rollback()
        return response
    # 已归一化的领域错误保留稳定合同，回滚只清除当前只读事务状态。
    except ApiError:
        connection.rollback()
        raise
    # 持久事实或类型异常统一收敛为套餐不可用，禁止泄露 SQL 与数据库路径。
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        connection.rollback()
        raise ApiError("plan.unavailable") from exc
    finally:
        connection.close()


def patch_plan(
    config: AppConfig,
    context: AuthContext,
    payload: dict[str, Any],
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> PlanResponse:
    """创建或按 revision CAS 修改当前租户 Plan，并原子维护四个 Bucket。"""

    normalized = _normalize_plan_patch(payload)
    connection = _connect(config, "plan.unavailable")
    trial_cycle_changed = False
    event_created = False
    audit_record: AuditRecord | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = require_transaction_permission(
            connection,
            context,
            "billing.plan.write",
            unavailable_error_code="plan.unavailable",
        )
        tenant_id = live.user.tenant_id
        current = _plan_row(connection, tenant_id)
        now = _database_now(connection)
        if current is not None and _expire_tenant_trial_if_due(
            connection,
            tenant_id=tenant_id,
            source_request_id=request_id,
            source_trace_id=trace_id,
            actor_id="quota-expiry-guard",
            now=now,
        ):
            # 到期事实与 source/outbox 先独立提交；旧 revision 的 PATCH 不得续期或回滚到期。
            connection.commit()
            raise ApiError("plan.revision_conflict")
        before_audit = _plan_audit_summary(current) if current is not None else {}
        if normalized.expected_revision == 0:
            # revision 零仅代表创建意图，已有同租户套餐时必须返回版本冲突。
            if current is not None:
                raise ApiError("plan.revision_conflict")
            trial_active = normalized.trial_ends_at is not None
            # 首次试用结束时间必须晚于数据库时钟，客户端时间不能回填过期试用。
            if trial_active and not _is_future(connection, normalized.trial_ends_at, now):
                raise ApiError("plan.request_invalid")
            plan_id = f"pln_{uuid4().hex}"
            trial_status = "active" if trial_active else "not_started"
            trial_cycle = 1 if trial_active else 0
            connection.execute(
                """
                INSERT INTO plans(
                  plan_id,tenant_id,display_name,base_tier,status,trial_status,
                  trial_started_at,trial_ends_at,trial_cycle,state_revision,
                  quota_generation,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,1,1,?,?)
                """,
                (
                    plan_id,
                    tenant_id,
                    normalized.display_name,
                    normalized.base_tier,
                    normalized.status,
                    trial_status,
                    now if trial_active else None,
                    normalized.trial_ends_at,
                    trial_cycle,
                    now,
                    now,
                ),
            )
            _create_zero_buckets(
                connection,
                tenant_id=tenant_id,
                plan_id=plan_id,
                plan_status=normalized.status,
                trial_status=trial_status,
                trial_ends_at=normalized.trial_ends_at,
                now=now,
            )
            trial_cycle_changed = trial_active
        else:
            # 更新意图必须命中当前租户套餐，缺失与跨租户对象保持同形不可见。
            if current is None:
                raise ApiError("plan.not_found")
            # 客户端 revision 必须与事务内权威版本一致，旧页面不能覆盖新事实。
            if int(current["state_revision"]) != normalized.expected_revision:
                raise ApiError("plan.revision_conflict")
            transition = _derive_trial_transition(
                connection,
                current,
                normalized.trial_ends_at,
                now=now,
            )
            trial_cycle_changed = transition["trial_cycle"] != int(current["trial_cycle"])
            next_generation = int(current["quota_generation"]) + int(trial_cycle_changed)
            updated = connection.execute(
                """
                UPDATE plans
                SET display_name=?,base_tier=?,status=?,trial_status=?,trial_started_at=?,
                    trial_ends_at=?,trial_cycle=?,state_revision=state_revision+1,
                    quota_generation=?,updated_at=?
                WHERE tenant_id=? AND plan_id=? AND state_revision=?
                """,
                (
                    normalized.display_name,
                    normalized.base_tier,
                    normalized.status,
                    transition["trial_status"],
                    transition["trial_started_at"],
                    transition["trial_ends_at"],
                    transition["trial_cycle"],
                    next_generation,
                    now,
                    tenant_id,
                    current["plan_id"],
                    normalized.expected_revision,
                ),
            )
            # SQL CAS 未命中表示并发修改已获胜，当前请求不得继续更新额度桶。
            if updated.rowcount != 1:
                raise ApiError("plan.revision_conflict")
            _update_bucket_governance(
                connection,
                tenant_id=tenant_id,
                plan_id=str(current["plan_id"]),
                plan_status=normalized.status,
                trial_status=str(transition["trial_status"]),
                trial_ends_at=transition["trial_ends_at"],
                trial_cycle_changed=trial_cycle_changed,
                now=now,
            )
            if current["trial_status"] == "active" and transition["trial_status"] == "expired":
                event_created = _insert_trial_expired_event(
                    connection,
                    tenant_id=tenant_id,
                    plan_id=str(current["plan_id"]),
                    base_tier=normalized.base_tier,
                    source_request_id=request_id,
                    source_trace_id=trace_id,
                    actor_id="quota-expiry-guard",
                    occurred_at=now,
                )
        response = _plan_response(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            trial_cycle_changed=trial_cycle_changed,
        )
        committed_plan = _plan_row(connection, tenant_id)
        # 写后必须能从同一事务恢复套餐事实，否则不能形成审计或成功响应。
        if committed_plan is None:
            raise ApiError("plan.unavailable")
        audit_record = record_audit_log(
            connection,
            live,
            action="plan.created" if current is None else "plan.updated",
            object_type="plan",
            object_id=str(committed_plan["plan_id"]),
            object_name=str(committed_plan["display_name"]),
            before_summary=before_audit,
            after_summary=_plan_audit_summary(committed_plan),
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
            created_at=_parse_time(now),
        )
        # 审计记录是套餐写入的原子组成部分，缺失时整笔事务必须失败关闭。
        if audit_record is None:
            raise ApiError("plan.unavailable")
        commit_audited_transaction(connection)
    # 领域错误先回滚套餐、额度桶、事件与审计的整个事务，再按固定合同返回。
    except ApiError as exc:
        connection.rollback()
        # 审计子系统错误统一映射套餐不可用，避免暴露内部审计实现和错误码。
        if exc.code in {"audit_log.record_unavailable", "audit_log.summary_invalid"}:
            raise ApiError("plan.unavailable") from exc
        raise
    # 唯一约束或数据库 CAS 冲突按套餐版本冲突返回，不暴露索引名称。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ApiError("plan.revision_conflict") from exc
    # 其他持久化与转换异常统一失败关闭，禁止返回部分套餐或额度桶状态。
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        connection.rollback()
        raise ApiError("plan.unavailable") from exc
    finally:
        connection.close()
    _log_plan_change(
        response,
        request_id=request_id,
        trace_id=trace_id,
        event_created=event_created,
    )
    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )
    return response


def get_quota_buckets(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> QuotaBucketsResponse:
    """读取固定四分类余额、权威 Allocation 汇总、overage 和最近调整。"""

    connection = _connect(config, "quota.unavailable")
    try:
        connection.execute("BEGIN DEFERRED")
        response = _quota_response(
            connection,
            tenant_id=context.user.tenant_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.rollback()
        return response
    # 已知额度领域错误保持原错误码，只回滚当前一致性读取事务。
    except ApiError:
        connection.rollback()
        raise
    # 数据库或持久类型异常统一返回额度不可用，不泄露 SQL 和原始值。
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        connection.rollback()
        raise ApiError("quota.unavailable") from exc
    finally:
        connection.close()


def create_quota_adjustment(
    config: AppConfig,
    context: AuthContext,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> QuotaAdjustmentResponse:
    """按租户哈希幂等键增加或减少一个 Bucket 的可用额度。"""

    normalized = _normalize_adjustment(payload)
    key_hash = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    connection = _connect(config, "quota.unavailable")
    audit_record: AuditRecord | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = require_transaction_permission(
            connection,
            context,
            "billing.quota.write",
            unavailable_error_code="quota.unavailable",
        )
        tenant_id = live.user.tenant_id
        now = _database_now(connection)
        expired = _expire_tenant_trial_if_due(
            connection,
            tenant_id=tenant_id,
            source_request_id=request_id,
            source_trace_id=trace_id,
            actor_id="quota-expiry-guard",
            now=now,
        )
        if expired:
            # 到期是独立系统事实；后续额度冲突不得把试用终态和 outbox 一并回滚。
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            live = require_transaction_permission(
                connection,
                context,
                "billing.quota.write",
                unavailable_error_code="quota.unavailable",
            )
            tenant_id = live.user.tenant_id
            now = _database_now(connection)
        replay = connection.execute(
            """
            SELECT * FROM quota_adjustments
            WHERE tenant_id=? AND idempotency_key_sha256=?
            """,
            (tenant_id, key_hash),
        ).fetchone()
        if replay is not None:
            # 同一幂等键只允许完全相同意图，摘要漂移不能伪装成成功重放。
            if replay["payload_sha256"] != normalized.payload_sha256:
                raise ApiError("quota.adjustment_conflict")
            response = _adjustment_response(
                connection,
                tenant_id=tenant_id,
                adjustment_row=replay,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response

        plan = _plan_row(connection, tenant_id)
        # 无同租户套餐时额度对象不可见，不能由客户端 ID 创建孤立 Bucket。
        if plan is None:
            raise ApiError("quota.not_found")
        # 暂停套餐禁止任何额度增减，恢复必须先走套餐版本化更新。
        if plan["status"] != "active":
            raise ApiError("plan.suspended")
        bucket = connection.execute(
            """
            SELECT * FROM quota_buckets
            WHERE tenant_id=? AND plan_id=? AND quota_type=?
            """,
            (tenant_id, plan["plan_id"], normalized.quota_type),
        ).fetchone()
        # 固定类型 Bucket 缺失表示租户事实不完整，不能运行时懒建或跨桶写入。
        if bucket is None:
            raise ApiError("quota.not_found")
        # 调整必须绑定事务内 Bucket revision，旧页面不得覆盖并发消费或调整。
        if int(bucket["state_revision"]) != normalized.expected_bucket_revision:
            raise ApiError("quota.adjustment_conflict")
        # 试用已结束后不能继续充值试用桶，历史余额只保留为审计事实。
        if normalized.quota_type == "trial" and plan["trial_status"] != "active":
            raise ApiError("quota.adjustment_conflict")
        granted = _micros(bucket["granted_micros"])
        consumed = _micros(bucket["consumed_micros"])
        reserved = _micros(bucket["reserved_micros"])
        available = granted - consumed - reserved
        if normalized.direction == "credit":
            # 充值后金额不得超过共享微元上限，防止 SQLite 整数溢出与口径漂移。
            if normalized.amount_micros > MAX_CNY_MICROS - granted:
                raise ApiError("quota.adjustment_conflict")
            next_granted = granted + normalized.amount_micros
        else:
            # 扣减只能使用扣除消费和预留后的余额，不能侵占已承诺额度。
            if normalized.amount_micros > available:
                raise ApiError("quota.adjustment_conflict")
            next_granted = granted - normalized.amount_micros
        next_status = _bucket_status(
            plan_status="active",
            quota_type=normalized.quota_type,
            trial_status=str(plan["trial_status"]),
            granted=next_granted,
            consumed=consumed,
            reserved=reserved,
        )
        updated = connection.execute(
            """
            UPDATE quota_buckets
            SET granted_micros=?,status=?,state_revision=state_revision+1,updated_at=?
            WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
            """,
            (
                next_granted,
                next_status,
                now,
                tenant_id,
                bucket["quota_bucket_id"],
                normalized.expected_bucket_revision,
            ),
        )
        # Bucket CAS 未命中表示并发事实已变化，当前调整不能继续形成审计记录。
        if updated.rowcount != 1:
            raise ApiError("quota.adjustment_conflict")
        if normalized.direction == "credit":
            plan_updated = connection.execute(
                """
                UPDATE plans SET state_revision=state_revision+1,
                  quota_generation=quota_generation+1,updated_at=?
                WHERE tenant_id=? AND plan_id=? AND state_revision=?
                """,
                (now, tenant_id, plan["plan_id"], plan["state_revision"]),
            )
            # 充值同步推进套餐 generation，任一 CAS 失败都必须回滚 Bucket 更新。
            if plan_updated.rowcount != 1:
                raise ApiError("quota.adjustment_conflict")
        adjustment_id = f"qadj_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO quota_adjustments(
              quota_adjustment_id,tenant_id,plan_id,quota_bucket_id,quota_type,direction,
              amount_micros,reason,actor_user_id,operation_request_id,operation_trace_id,
              expected_bucket_revision,resulting_bucket_revision,idempotency_key_sha256,
              payload_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                adjustment_id,
                tenant_id,
                plan["plan_id"],
                bucket["quota_bucket_id"],
                normalized.quota_type,
                normalized.direction,
                normalized.amount_micros,
                normalized.reason,
                live.user.user_id,
                request_id,
                trace_id,
                normalized.expected_bucket_revision,
                normalized.expected_bucket_revision + 1,
                key_hash,
                normalized.payload_sha256,
                now,
            ),
        )
        adjustment = connection.execute(
            "SELECT * FROM quota_adjustments WHERE tenant_id=? AND quota_adjustment_id=?",
            (tenant_id, adjustment_id),
        ).fetchone()
        # 新增调整必须能在同一事务按租户回读，否则不能构造成功响应。
        if adjustment is None:
            raise ApiError("quota.unavailable")
        response = _adjustment_response(
            connection,
            tenant_id=tenant_id,
            adjustment_row=adjustment,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        audit_record = record_audit_log(
            connection,
            live,
            action="quota.adjustment_created",
            object_type="quota_adjustment",
            object_id=adjustment_id,
            object_name=None,
            before_summary={},
            after_summary=_quota_adjustment_audit_summary(
                adjustment,
                reason_sha256=normalized.reason_sha256,
            ),
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
            created_at=_parse_time(now),
        )
        # 调整审计与余额变化必须原子存在，审计缺失时整笔写入回滚。
        if audit_record is None:
            raise ApiError("quota.unavailable")
        commit_audited_transaction(connection)
    # 领域错误先回滚余额、调整和审计事实，再保留稳定公开错误语义。
    except ApiError as exc:
        connection.rollback()
        # 审计内部错误折叠为额度不可用，不能向调用方暴露审计表结构。
        if exc.code in {"audit_log.record_unavailable", "audit_log.summary_invalid"}:
            raise ApiError("quota.unavailable") from exc
        raise
    # 唯一约束与并发 CAS 都表示调整冲突，原始 SQLite 错误不得外泄。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 唯一键与 CAS 冲突都折叠为稳定业务冲突，不回显幂等摘要或 SQL。
        raise ApiError("quota.adjustment_conflict") from exc
    # 其他数据库与转换异常统一失败关闭，禁止留下半条额度或审计事实。
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        connection.rollback()
        raise ApiError("quota.unavailable") from exc
    finally:
        connection.close()
    _log_adjustment(response, request_id=request_id, trace_id=trace_id)
    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )
    return response


def expire_due_trials(config: AppConfig, *, limit: int = 50) -> int:
    """由 Control scheduler 收敛到期试用，并为每个周期只创建一个 source/outbox。"""

    # 单轮批量必须是有界整数，防止配置错误长期占用全局写锁。
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("plan.expiry_limit_invalid")
    connection = _connect(config, "plan.unavailable")
    expired = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        rows = connection.execute(
            """
            SELECT tenant_id FROM plans
            WHERE trial_status='active' AND unixepoch(trial_ends_at)<=unixepoch(?)
            ORDER BY trial_ends_at,tenant_id LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        for row in rows:
            event_request_id = f"req_qexpiry_{uuid4().hex}"
            event_trace_id = f"trc_qexpiry_{uuid4().hex}"
            if _expire_tenant_trial_if_due(
                connection,
                tenant_id=str(row["tenant_id"]),
                source_request_id=event_request_id,
                source_trace_id=event_trace_id,
                actor_id="quota-trial-scheduler",
                now=now,
            ):
                expired += 1
        connection.commit()
    # 已知套餐错误回滚整个批次，避免只提交部分租户到期状态。
    except ApiError:
        connection.rollback()
        raise
    # 数据库或持久事实异常统一按套餐不可用处理，不暴露具体租户与 SQL。
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        connection.rollback()
        raise ApiError("plan.unavailable") from exc
    finally:
        connection.close()
    return expired


def _expire_tenant_trial_if_due(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    source_request_id: str,
    source_trace_id: str,
    actor_id: str,
    now: str,
) -> bool:
    """在调用方写事务内 CAS 收敛一个租户；已有 Reservation 不被回收。"""

    plan = connection.execute(
        """
        SELECT * FROM plans WHERE tenant_id=? AND trial_status='active'
          AND unixepoch(trial_ends_at)<=unixepoch(?)
        """,
        (tenant_id, now),
    ).fetchone()
    if plan is None:
        return False
    changed = connection.execute(
        """
        UPDATE plans SET trial_status='expired',state_revision=state_revision+1,updated_at=?
        WHERE tenant_id=? AND plan_id=? AND state_revision=? AND trial_status='active'
        """,
        (now, tenant_id, plan["plan_id"], plan["state_revision"]),
    )
    if changed.rowcount != 1:
        return False
    bucket = connection.execute(
        """
        SELECT quota_bucket_id,state_revision FROM quota_buckets
        WHERE tenant_id=? AND plan_id=? AND quota_type='trial'
        """,
        (tenant_id, plan["plan_id"]),
    ).fetchone()
    # 到期套餐必须拥有同租户 trial Bucket，缺失时不能只更新一半状态。
    if bucket is None:
        raise ApiError("plan.unavailable")
    connection.execute(
        """
        UPDATE quota_buckets SET status='expired',state_revision=state_revision+1,updated_at=?
        WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
        """,
        (now, tenant_id, bucket["quota_bucket_id"], bucket["state_revision"]),
    )
    # trial Bucket CAS 必须唯一命中，否则套餐到期事务不能形成完整事实。
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise ApiError("plan.unavailable")
    _insert_trial_expired_event(
        connection,
        tenant_id=tenant_id,
        plan_id=str(plan["plan_id"]),
        base_tier=str(plan["base_tier"]),
        source_request_id=source_request_id,
        source_trace_id=source_trace_id,
        actor_id=actor_id,
        occurred_at=now,
    )
    return True


def _insert_trial_expired_event(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    plan_id: str,
    base_tier: str,
    source_request_id: str,
    source_trace_id: str,
    actor_id: str,
    occurred_at: str,
) -> bool:
    """写入唯一试用到期 source/outbox；payload 先通过 Shared exact 合同。"""

    payload = {
        "schema_version": 1,
        "plan_id": plan_id,
        "trial_cycle": 1,
        "expired_at": occurred_at,
        "effective_tier": base_tier,
    }
    event_id = f"qevt_{uuid4().hex}"
    build_webhook_event(
        tenant_id=tenant_id,
        event_type="trial.expired",
        payload=payload,
        actor_id=actor_id,
        object_id=plan_id,
        trace_id=source_trace_id,
        source_request_id=source_request_id,
        event_id=event_id,
        timestamp=_parse_time(occurred_at),
    )
    inserted = connection.execute(
        """
        INSERT OR IGNORE INTO quota_events(
          quota_event_id,tenant_id,plan_id,quota_bucket_id,event_type,
          quota_generation,trial_cycle,source_request_id,source_trace_id,actor_id,
          payload_json,occurred_at
        ) VALUES(?,?,?,NULL,'trial.expired',NULL,1,?,?,?,?,?)
        """,
        (
            event_id,
            tenant_id,
            plan_id,
            source_request_id,
            source_trace_id,
            actor_id,
            _json(payload),
            occurred_at,
        ),
    )
    if inserted.rowcount != 1:
        return False
    connection.execute(
        """
        INSERT INTO quota_event_outbox(
          quota_event_outbox_id,tenant_id,quota_event_id,status,attempt_count,
          lease_generation,next_attempt_at,created_at,updated_at
        ) VALUES(?,?,?,'pending',0,0,?,?,?)
        """,
        (f"qobx_{uuid4().hex}", tenant_id, event_id, occurred_at, occurred_at, occurred_at),
    )
    return True


def _normalize_plan_patch(payload: dict[str, Any]) -> _NormalizedPlanPatch:
    """校验套餐精确字段、版本意图、显示名称、枚举与规范 UTC 时间。"""

    expected = {"expected_revision", "display_name", "base_tier", "status", "trial_ends_at"}
    # 请求必须精确命中冻结字段集合，owner、余额和未知字段一律拒绝。
    if type(payload) is not dict or set(payload) != expected:
        raise ApiError("plan.request_invalid")
    revision = payload["expected_revision"]
    name = payload["display_name"]
    # revision 必须是非负整数且名称必须为文本，禁止隐式类型转换。
    if type(revision) is not int or revision < 0 or type(name) is not str:
        raise ApiError("plan.request_invalid")
    name = unicodedata.normalize("NFKC", name).strip()
    # 规范化名称仍需有界且无控制字符，避免日志和页面结构被污染。
    if not 1 <= len(name) <= 80 or any(ord(character) < 32 for character in name):
        raise ApiError("plan.request_invalid")
    if (
        type(payload["base_tier"]) is not str
        or payload["base_tier"] not in {"free", "paid"}
        or type(payload["status"]) is not str
        or payload["status"] not in {"active", "suspended"}
    ):
        # 套餐层级和状态只接受冻结枚举，未知值不能进入持久状态机。
        raise ApiError("plan.request_invalid")
    trial_ends_at = payload["trial_ends_at"]
    if trial_ends_at is not None:
        trial_ends_at = _canonical_input_time(trial_ends_at, "plan.request_invalid")
    return _NormalizedPlanPatch(
        expected_revision=revision,
        display_name=name,
        base_tier=str(payload["base_tier"]),
        status=str(payload["status"]),
        trial_ends_at=trial_ends_at,
    )


def _normalize_adjustment(payload: dict[str, Any]) -> _NormalizedAdjustment:
    """校验额度 exact 字段、金额、reason 与 Bucket CAS，并冻结规范意图摘要。"""

    expected = {"quota_type", "direction", "amount", "reason", "expected_bucket_revision"}
    # 额度正文必须精确命中冻结字段集合，客户端不能提交租户或结果余额。
    if type(payload) is not dict or set(payload) != expected:
        raise ApiError("quota.request_invalid")
    quota_type = payload["quota_type"]
    direction = payload["direction"]
    revision = payload["expected_bucket_revision"]
    reason = payload["reason"]
    if (
        type(quota_type) is not str
        or quota_type not in QUOTA_TYPES
        or type(direction) is not str
        or direction not in {"credit", "debit"}
        or type(revision) is not int
        or revision < 1
        or type(reason) is not str
    ):
        # 类型、方向和版本任一非法都在摘要计算前拒绝，避免产生多种等价意图。
        raise ApiError("quota.request_invalid")
    reason = unicodedata.normalize("NFKC", reason).strip()
    # 原因正文必须有界且无控制字符，正文只进入权威事实而不进入日志。
    if not 1 <= len(reason) <= 500 or any(ord(character) < 32 for character in reason):
        raise ApiError("quota.request_invalid")
    try:
        amount_micros = parse_cny_micros(payload["amount"], allow_zero=False)
        payload_hash = canonical_quota_intent_sha256(
            {
                "quota_type": quota_type,
                "direction": direction,
                "amount_micros": amount_micros,
                "reason": reason,
                "expected_bucket_revision": revision,
            }
        )
    # 金额或规范摘要失败统一映射输入错误，不回显 reason 与原始金额文本。
    except ValueError as exc:
        raise ApiError("quota.request_invalid") from exc
    return _NormalizedAdjustment(
        quota_type=quota_type,
        direction=str(direction),
        amount_micros=amount_micros,
        reason=reason,
        reason_sha256=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
        expected_bucket_revision=revision,
        payload_sha256=payload_hash,
    )


def _derive_trial_transition(
    connection: sqlite3.Connection,
    current: sqlite3.Row,
    submitted_end: str | None,
    *,
    now: str,
) -> dict[str, object]:
    """实现 v1 单次试用状态机；expired 证据不可被 PATCH 清空或重新激活。"""

    status = str(current["trial_status"])
    if status == "not_started":
        if submitted_end is None:
            return {
                "trial_status": "not_started",
                "trial_started_at": None,
                "trial_ends_at": None,
                "trial_cycle": 0,
            }
        # 首次开启试用只能使用未来数据库时间，禁止客户端回填已过期周期。
        if not _is_future(connection, submitted_end, now):
            raise ApiError("plan.request_invalid")
        return {
            "trial_status": "active",
            "trial_started_at": now,
            "trial_ends_at": submitted_end,
            "trial_cycle": 1,
        }
    if status == "active":
        if submitted_end is None:
            return {
                "trial_status": "expired",
                "trial_started_at": current["trial_started_at"],
                "trial_ends_at": current["trial_ends_at"],
                "trial_cycle": 1,
            }
        # 活跃试用延期仍必须晚于数据库时钟，不能制造倒退的结束边界。
        if not _is_future(connection, submitted_end, now):
            raise ApiError("plan.request_invalid")
        return {
            "trial_status": "active",
            "trial_started_at": current["trial_started_at"],
            "trial_ends_at": submitted_end,
            "trial_cycle": 1,
        }
    if status == "expired":
        # expired 后传 NULL 表示“保持历史证据”；任何新 future 时间都构成二次试用。
        if submitted_end is not None and submitted_end != current["trial_ends_at"]:
            raise ApiError("plan.request_invalid")
        return {
            "trial_status": "expired",
            "trial_started_at": current["trial_started_at"],
            "trial_ends_at": current["trial_ends_at"],
            "trial_cycle": 1,
        }
    # 未知持久状态无法安全推导 effective tier，必须按套餐不可用失败关闭。
    raise ApiError("plan.unavailable")


def _create_zero_buckets(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    plan_id: str,
    plan_status: str,
    trial_status: str,
    trial_ends_at: str | None,
    now: str,
) -> None:
    """Plan 创建时原子生成四个零余额 Bucket，运行时不做缺表式懒补。"""

    for quota_type in QUOTA_TYPES:
        status = _bucket_status(
            plan_status=plan_status,
            quota_type=quota_type,
            trial_status=trial_status,
            granted=0,
            consumed=0,
            reserved=0,
        )
        connection.execute(
            """
            INSERT INTO quota_buckets(
              quota_bucket_id,tenant_id,plan_id,quota_type,granted_micros,
              consumed_micros,reserved_micros,status,expires_at,state_revision,
              created_at,updated_at
            ) VALUES(?,?,?,?,0,0,0,?,?,1,?,?)
            """,
            (
                f"qbk_{uuid4().hex}",
                tenant_id,
                plan_id,
                quota_type,
                status,
                trial_ends_at if quota_type == "trial" else None,
                now,
                now,
            ),
        )


def _update_bucket_governance(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    plan_id: str,
    plan_status: str,
    trial_status: str,
    trial_ends_at: object,
    trial_cycle_changed: bool,
    now: str,
) -> None:
    """按 Plan 状态重算四桶治理状态；首次试用不会覆盖已有消费或历史分摊。"""

    rows = connection.execute(
        "SELECT * FROM quota_buckets WHERE tenant_id=? AND plan_id=? ORDER BY quota_type",
        (tenant_id, plan_id),
    ).fetchall()
    # 套餐必须完整拥有四个固定类型 Bucket，缺失或多余都不能运行时修复。
    if {row["quota_type"] for row in rows} != set(QUOTA_TYPES) or len(rows) != 4:
        raise ApiError("plan.unavailable")
    for row in rows:
        quota_type = str(row["quota_type"])
        # v1 首次开启试用不重置历史计数；trial_cycle=0 时理论上尚无 trial 调整或分摊。
        if trial_cycle_changed and quota_type == "trial" and (
            _micros(row["consumed_micros"]) != 0 or _micros(row["reserved_micros"]) != 0
        ):
            # 新试用周期不能覆盖已有消费或预留，否则历史分摊将失去归属。
            raise ApiError("plan.unavailable")
        status = _bucket_status(
            plan_status=plan_status,
            quota_type=quota_type,
            trial_status=trial_status,
            granted=_micros(row["granted_micros"]),
            consumed=_micros(row["consumed_micros"]),
            reserved=_micros(row["reserved_micros"]),
        )
        connection.execute(
            """
            UPDATE quota_buckets SET status=?,expires_at=?,state_revision=state_revision+1,
              updated_at=? WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
            """,
            (
                status,
                trial_ends_at if quota_type == "trial" and trial_status != "not_started" else None,
                now,
                tenant_id,
                row["quota_bucket_id"],
                row["state_revision"],
            ),
        )
        # 每个 Bucket 治理更新都必须唯一命中，部分更新会破坏四桶一致性。
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("plan.revision_conflict")


def _bucket_status(
    *,
    plan_status: str,
    quota_type: str,
    trial_status: str,
    granted: int,
    consumed: int,
    reserved: int,
) -> str:
    """从 Plan 与计数推导 Bucket 状态，不接受客户端提交状态。"""

    if plan_status == "suspended":
        return "suspended"
    if quota_type == "trial" and trial_status != "active":
        return "expired"
    return "active" if granted > consumed + reserved else "exhausted"


def _plan_audit_summary(row: sqlite3.Row) -> dict[str, object]:
    """构造 Plan 管理审计白名单；不复制 Bucket、请求正文或客户端时间。"""

    return {
        "display_name": _text(row["display_name"]),
        "base_tier": _text(row["base_tier"]),
        "status": _text(row["status"]),
        "trial_status": _text(row["trial_status"]),
        "trial_started_at": row["trial_started_at"],
        "trial_ends_at": row["trial_ends_at"],
        "trial_cycle": _count(row["trial_cycle"]),
        "state_revision": _positive(row["state_revision"]),
        "quota_generation": _positive(row["quota_generation"]),
    }


def _quota_adjustment_audit_summary(
    row: sqlite3.Row,
    *,
    reason_sha256: str,
) -> dict[str, object]:
    """构造只追加额度调整的审计投影；原因正文和幂等摘要永不复制。"""

    if (
        len(reason_sha256) != 64
        or any(character not in "0123456789abcdef" for character in reason_sha256)
    ):
        # 审计只接受规范原因摘要，异常摘要不能替代正文脱敏边界。
        raise ValueError("quota adjustment reason digest invalid")
    return {
        "plan_id": _text(row["plan_id"]),
        "quota_bucket_id": _text(row["quota_bucket_id"]),
        "quota_type": _text(row["quota_type"]),
        "direction": _text(row["direction"]),
        "amount": format_cny_micros(_positive(row["amount_micros"])),
        "expected_bucket_revision": _positive(row["expected_bucket_revision"]),
        "resulting_bucket_revision": _positive(row["resulting_bucket_revision"]),
        "reason_sha256": reason_sha256,
    }


def _plan_response(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    trial_cycle_changed: bool = False,
) -> PlanResponse:
    """从同一租户事务快照构造套餐页响应，缺失套餐保持兼容空态。"""

    row = _plan_row(connection, tenant_id)
    if row is None:
        return PlanResponse(
            plan=None,
            quota_buckets=[],
            trial_cycle_changed=False,
            request_id=request_id,
            trace_id=trace_id,
        )
    return PlanResponse(
        plan=_plan_info(row),
        quota_buckets=_bucket_infos(connection, tenant_id, str(row["plan_id"])),
        trial_cycle_changed=trial_cycle_changed,
        request_id=request_id,
        trace_id=trace_id,
    )


def _quota_response(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
) -> QuotaBucketsResponse:
    """从同一事务快照构造 PAGE-111 DTO，四分类与 overage 始终分离。"""

    plan_row = _plan_row(connection, tenant_id)
    if plan_row is None:
        return QuotaBucketsResponse(
            plan=None,
            quota_buckets=[],
            overage=QuotaOverageSummary(amount="0.000000", settlement_count=0),
            recent_adjustments=[],
            request_id=request_id,
            trace_id=trace_id,
        )
    overage = connection.execute(
        """
        SELECT COALESCE(SUM(unallocated_micros),0) AS amount_micros,
          COUNT(*) AS settlement_count FROM quota_usage_settlements
        WHERE tenant_id=? AND status='overage'
        """,
        (tenant_id,),
    ).fetchone()
    adjustments = connection.execute(
        """
        SELECT * FROM quota_adjustments WHERE tenant_id=?
        ORDER BY created_at DESC,quota_adjustment_id DESC LIMIT 50
        """,
        (tenant_id,),
    ).fetchall()
    return QuotaBucketsResponse(
        plan=_plan_info(plan_row),
        quota_buckets=_bucket_infos(connection, tenant_id, str(plan_row["plan_id"])),
        overage=QuotaOverageSummary(
            amount=format_cny_micros(_micros(overage["amount_micros"])),
            settlement_count=_count(overage["settlement_count"]),
        ),
        recent_adjustments=[_adjustment_summary(row) for row in adjustments],
        request_id=request_id,
        trace_id=trace_id,
    )


def _adjustment_response(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    adjustment_row: sqlite3.Row,
    idempotent_replay: bool,
    request_id: str,
    trace_id: str,
) -> QuotaAdjustmentResponse:
    """按同租户权威行恢复调整、Bucket 与 Plan，重放不重新写事实。"""

    plan = _plan_row(connection, tenant_id)
    # 调整关联的 Plan 必须与当前租户权威行一致，跨 owner 事实不能回读。
    if plan is None or plan["plan_id"] != adjustment_row["plan_id"]:
        raise ApiError("quota.unavailable")
    buckets = _bucket_infos(connection, tenant_id, str(plan["plan_id"]), allocation=True)
    matching = [
        item
        for item in buckets
        if item.quota_bucket_id == adjustment_row["quota_bucket_id"]
    ]
    # 调整必须唯一关联一个 Bucket，缺失或重复都表示持久 owner 图损坏。
    if len(matching) != 1:
        raise ApiError("quota.unavailable")
    return QuotaAdjustmentResponse(
        adjustment=_adjustment_summary(adjustment_row),
        quota_bucket=matching[0],
        plan=_plan_info(plan),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _bucket_infos(
    connection: sqlite3.Connection,
    tenant_id: str,
    plan_id: str,
    *,
    allocation: bool = True,
) -> list[QuotaBucketInfo]:
    """按冻结业务顺序读取四 Bucket 和 Allocation 汇总，并验证 owner 完整性。"""

    rows = connection.execute(
        """
        SELECT bucket.*,
          COALESCE(SUM(allocation.amount_micros),0) AS allocation_consumed_micros,
          COUNT(allocation.quota_usage_allocation_id) AS allocation_count
        FROM quota_buckets bucket
        LEFT JOIN quota_usage_allocations allocation
          ON allocation.tenant_id=bucket.tenant_id
         AND allocation.quota_bucket_id=bucket.quota_bucket_id
        WHERE bucket.tenant_id=? AND bucket.plan_id=?
        GROUP BY bucket.quota_bucket_id
        """,
        (tenant_id, plan_id),
    ).fetchall()
    by_type = {str(row["quota_type"]): row for row in rows}
    # DTO 只接受完整四桶集合，不能向客户端返回部分余额造成错误决策。
    if len(rows) != 4 or set(by_type) != set(QUOTA_TYPES):
        raise ApiError("quota.unavailable")
    return [_bucket_info(by_type[quota_type]) for quota_type in QUOTA_CONSUMPTION_ORDER]


def _bucket_info(row: sqlite3.Row) -> QuotaBucketInfo:
    """把 Bucket 行转换为安全金额 DTO，并验证计数守恒。"""

    granted = _micros(row["granted_micros"])
    consumed = _micros(row["consumed_micros"])
    reserved = _micros(row["reserved_micros"])
    # 消费与预留之和不得超过授予额，损坏计数不能伪造负可用余额。
    if consumed + reserved > granted:
        raise ValueError("quota bucket counters invalid")
    quota_type = str(row["quota_type"])
    # 持久额度类型必须属于共享冻结顺序，未知分类不能进入公开报表。
    if quota_type not in QUOTA_TYPES:
        raise ValueError("quota type invalid")
    status = str(row["status"])
    # Bucket 状态必须属于固定枚举，异常状态不能由页面自行解释。
    if status not in {"active", "exhausted", "expired", "suspended"}:
        raise ValueError("quota status invalid")
    return QuotaBucketInfo(
        quota_bucket_id=_text(row["quota_bucket_id"]),
        tenant_id=_text(row["tenant_id"]),
        plan_id=_text(row["plan_id"]),
        quota_type=quota_type,  # type: ignore[arg-type]
        granted=format_cny_micros(granted),
        consumed=format_cny_micros(consumed),
        reserved=format_cny_micros(reserved),
        available=format_cny_micros(granted - consumed - reserved),
        status=status,  # type: ignore[arg-type]
        expires_at=_optional_time(row["expires_at"]),
        state_revision=_positive(row["state_revision"]),
        allocation_consumed=format_cny_micros(_micros(row["allocation_consumed_micros"])),
        allocation_count=_count(row["allocation_count"]),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _plan_info(row: sqlite3.Row) -> PlanInfo:
    """把 Plan 行转换为安全 DTO，effective tier 只从持久状态派生。"""

    base_tier = str(row["base_tier"])
    status = str(row["status"])
    trial_status = str(row["trial_status"])
    # 基础层级与套餐状态必须来自冻结枚举，未知值不能推导 effective tier。
    if base_tier not in {"free", "paid"} or status not in {"active", "suspended"}:
        raise ValueError("plan enum invalid")
    # 试用状态必须遵守单次生命周期，持久异常值不能进入公开响应。
    if trial_status not in {"not_started", "active", "expired"}:
        raise ValueError("plan trial invalid")
    effective = "trial" if trial_status == "active" else base_tier
    return PlanInfo(
        plan_id=_text(row["plan_id"]),
        tenant_id=_text(row["tenant_id"]),
        display_name=_text(row["display_name"]),
        base_tier=base_tier,  # type: ignore[arg-type]
        effective_tier=effective,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        trial_status=trial_status,  # type: ignore[arg-type]
        trial_started_at=_optional_time(row["trial_started_at"]),
        trial_ends_at=_optional_time(row["trial_ends_at"]),
        trial_cycle=_count(row["trial_cycle"]),
        state_revision=_positive(row["state_revision"]),
        quota_generation=_positive(row["quota_generation"]),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _adjustment_summary(row: sqlite3.Row) -> QuotaAdjustmentSummary:
    """构造不含 reason 和幂等摘要的最近调整 DTO。"""

    quota_type = str(row["quota_type"])
    direction = str(row["direction"])
    # 最近调整只接受固定分类与方向，损坏事实不能被页面静默展示。
    if quota_type not in QUOTA_TYPES or direction not in {"credit", "debit"}:
        raise ValueError("quota adjustment enum invalid")
    return QuotaAdjustmentSummary(
        quota_adjustment_id=_text(row["quota_adjustment_id"]),
        tenant_id=_text(row["tenant_id"]),
        plan_id=_text(row["plan_id"]),
        quota_type=quota_type,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        amount=format_cny_micros(_positive(row["amount_micros"])),
        actor_user_id=_text(row["actor_user_id"]),
        operation_request_id=_text(row["operation_request_id"]),
        resulting_bucket_revision=_positive(row["resulting_bucket_revision"]),
        created_at=_parse_time(row["created_at"]),
    )


def _plan_row(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row | None:
    """只按认证 tenant 读取唯一 Plan，不接受客户端 owner。"""

    # tenant_id 仅来自认证或事务 owner，查询始终把租户列作为首要归属条件。
    return connection.execute("SELECT * FROM plans WHERE tenant_id=?", (tenant_id,)).fetchone()


def _database_now(connection: sqlite3.Connection) -> str:
    """读取事务内 SQLite UTC 秒级时钟，避免浏览器时间决定试用边界。"""

    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    return _canonical_input_time(row[0] if row else None, "plan.unavailable")


def _is_future(connection: sqlite3.Connection, value: str | None, now: str) -> bool:
    """使用 SQLite unixepoch 比较 canonical 时间，避免 Python/数据库时区口径漂移。"""

    if value is None:
        return False
    row = connection.execute("SELECT unixepoch(?)>unixepoch(?)", (value, now)).fetchone()
    return bool(row and row[0] == 1)


def _canonical_input_time(value: object, error_code: str) -> str:
    """只接受无小数、Z 后缀、可往返的 UTC 秒级文本。"""

    # 只接受 Z 结尾秒级文本，拒绝小数秒和隐式本地时区产生多种表示。
    if type(value) is not str or not value.endswith("Z") or "." in value:
        raise ApiError(error_code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 无法解析的时间统一映射调用方领域错误，不回显客户端原始文本。
    except ValueError as exc:
        raise ApiError(error_code) from exc
    canonical = parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 往返后必须逐字一致，带偏移但等价的文本也不能进入持久 CAS。
    if canonical != value:
        raise ApiError(error_code)
    return canonical


def _parse_time(value: object) -> datetime:
    """解析权威 UTC 秒级时间，坏持久事实失败关闭。"""

    canonical = _canonical_input_time(value, "quota.unavailable")
    return datetime.fromisoformat(canonical.replace("Z", "+00:00"))


def _optional_time(value: object) -> datetime | None:
    """把可空持久时间转换为规范 UTC 时间，非空坏事实继续失败关闭。"""

    return None if value is None else _parse_time(value)


def _connect(config: AppConfig, unavailable_code: str) -> sqlite3.Connection:
    """连接失败统一折叠为领域错误，不暴露数据库 URL 或宿主路径。"""

    try:
        return connect_database(config.database_url)
    # 数据库连接和 URL 解析错误只返回领域不可用，禁止泄露宿主路径。
    except (DbError, sqlite3.Error, ValueError) as exc:
        raise ApiError(unavailable_code) from exc


def _json(value: object) -> str:
    """生成稳定紧凑 JSON 文本，用于不可变事件 payload 的持久化。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: object) -> str:
    """读取非空且无控制字符的持久文本，异常值不得进入响应或日志。"""

    # 租户、对象与显示文本都不能含空值或控制字符，坏事实必须失败关闭。
    if type(value) is not str or not value or any(ord(character) < 32 for character in value):
        raise ValueError("quota text invalid")
    return value


def _micros(value: object) -> int:
    """校验非负 CNY 微元整数及共享上限，拒绝布尔和隐式数值转换。"""

    # 金额必须是范围内原生整数，避免浮点、布尔或溢出破坏财务口径。
    if type(value) is not int or not 0 <= value <= MAX_CNY_MICROS:
        raise ValueError("quota micros invalid")
    return value


def _positive(value: object) -> int:
    """校验严格正微元金额，供调整和不可变财务事实复用。"""

    parsed = _micros(value)
    # 需要形成业务事实的金额必须大于零，零值不能伪装成有效调整。
    if parsed < 1:
        raise ValueError("quota positive invalid")
    return parsed


def _count(value: object) -> int:
    """校验非负原生整数计数，拒绝布尔和损坏的持久聚合值。"""

    # 报表计数只能是非负整数，数据库类型漂移时不能继续构造响应。
    if type(value) is not int or value < 0:
        raise ValueError("quota count invalid")
    return value


def _log_plan_change(
    response: PlanResponse,
    *,
    request_id: str,
    trace_id: str,
    event_created: bool,
) -> None:
    """只记录 owner、revision 和状态，不记录名称、试用日期或任何请求正文。"""

    plan = response.plan
    logger.info(
        "plan state committed",
        extra={
            "event": "plan.state_committed",
            "tenant_id": plan.tenant_id if plan else None,
            "request_id": request_id,
            "trace_id": trace_id,
            "object_id": plan.plan_id if plan else None,
            "state_revision": plan.state_revision if plan else None,
            "trial_status": plan.trial_status if plan else None,
            "event_created": event_created,
        },
    )


def _log_adjustment(
    response: QuotaAdjustmentResponse,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """记录安全调整摘要；reason、幂等键及其摘要均不得进入日志。"""

    item = response.adjustment
    logger.info(
        "quota adjustment committed",
        extra={
            "event": "quota.adjustment_committed",
            "tenant_id": item.tenant_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "object_id": item.quota_adjustment_id,
            "quota_type": item.quota_type,
            "direction": item.direction,
            "amount": item.amount,
            "idempotent_replay": response.idempotent_replay,
        },
    )


__all__ = [
    "create_quota_adjustment",
    "expire_due_trials",
    "get_plan",
    "get_quota_buckets",
    "patch_plan",
]
