"""租户额度准入、Reservation 释放与 Usage 同事务结算。"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.quota import (
    MAX_CNY_MICROS,
    QUOTA_CONSUMPTION_ORDER,
    QUOTA_ORDER_VERSION,
    classify_allocations,
    format_cny_micros,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from .models import QuotaGuardResult, QuotaSettlementSummary

logger = logging.getLogger("metahub.gateway.quota")
# 所有人民币金额统一换算为整数微元，禁止二进制浮点进入额度账本。
_MICROS = Decimal(1_000_000)


def enforce_quota_guard(
    config: AppConfig,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    route: object,
    payload: dict[str, Any],
    request_kind: str,
    fallback_route: object | None = None,
    fallback_routes: tuple[object, ...] = (),
    token_estimate: object | None = None,
    price_as_of: datetime | None = None,
    trusted_cost_cny: Decimal | None = None,
) -> QuotaGuardResult:
    """在任何 Provider attempt 前按冻结的最坏 CNY 成本预留额度。"""

    connection = _connect(config)
    pending_error: ApiError | None = None
    result = QuotaGuardResult(None, 0, None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        plan = connection.execute(
            "SELECT * FROM plans WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        if plan is None:
            connection.commit()
            return result
        _expire_trial_if_due(
            connection,
            plan,
            request_id=request_id,
            trace_id=trace_id,
            actor_id="quota-expiry-guard",
            now=now,
        )
        plan = connection.execute(
            "SELECT * FROM plans WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        # 租户套餐在事务内消失属于一致性故障，必须关闭准入并返回统一错误。
        if plan is None:
            raise ApiError("gateway.quota_unavailable")
        # 非活跃套餐由后端强制阻断，不能依赖调用方或前端状态判断。
        if plan["status"] != "active":
            connection.commit()
            raise ApiError("gateway.plan_suspended")

        estimated = trusted_cost_cny
        if estimated is None:
            estimated = _estimate_worst_cost(
                connection,
                route=route,
                payload=payload,
                request_kind=request_kind,
                fallback_route=fallback_route,
                fallback_routes=fallback_routes,
                token_estimate=token_estimate,
                price_as_of=price_as_of,
            )
        estimated_micros = _decimal_to_micros(estimated, allow_zero=True)
        result = QuotaGuardResult(None, estimated_micros, str(plan["plan_id"]))
        if estimated_micros == 0:
            connection.commit()
            return result

        existing = connection.execute(
            "SELECT * FROM quota_reservations WHERE tenant_id=? AND request_id=?",
            (tenant_id, request_id),
        ).fetchone()
        if existing is not None:
            items = _load_reservation_items(connection, existing)
            # 相同请求只能复用完全同租户、链路、金额和套餐的活跃预留。
            if (
                existing["trace_id"] != trace_id
                or int(existing["estimated_micros"]) != estimated_micros
                or existing["plan_id"] != plan["plan_id"]
                or existing["status"] != "active"
                or not _reservation_items_are_valid(existing, items)
            ):
                # 复用坐标或 item 顺序任一漂移都表示 owner 冲突，必须返回统一不可用错误。
                raise ApiError("gateway.quota_unavailable")
            connection.commit()
            return QuotaGuardResult(
                str(existing["quota_reservation_id"]),
                estimated_micros,
                str(plan["plan_id"]),
            )

        buckets = _ordered_buckets(connection, tenant_id, str(plan["plan_id"]))
        allocations: list[tuple[sqlite3.Row, int, int]] = []
        remaining = estimated_micros
        available_total = 0
        for priority, bucket in enumerate(buckets, start=1):
            available = _available_for_admission(plan, bucket)
            available_total += available
            reserved = min(remaining, available)
            if reserved:
                allocations.append((bucket, priority, reserved))
                remaining -= reserved
        if remaining:
            _insert_quota_exhausted_event(
                connection,
                plan=plan,
                required_micros=estimated_micros,
                available_micros=available_total,
                request_id=request_id,
                trace_id=trace_id,
                occurred_at=now,
            )
            connection.commit()
            pending_error = ApiError("gateway.quota_exhausted")
        else:
            reservation_id = f"qres_{uuid4().hex}"
            expires_at = _db_time(_parse_time(now) + timedelta(hours=24))
            connection.execute(
                """
                INSERT INTO quota_reservations(
                  quota_reservation_id,tenant_id,plan_id,request_id,trace_id,
                  estimated_micros,outstanding_micros,actual_micros,unallocated_micros,
                  order_version,status,expires_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,NULL,0,?,'active',?,?,?)
                """,
                (
                    reservation_id,
                    tenant_id,
                    plan["plan_id"],
                    request_id,
                    trace_id,
                    estimated_micros,
                    estimated_micros,
                    QUOTA_ORDER_VERSION,
                    expires_at,
                    now,
                    now,
                ),
            )
            for bucket, priority, amount in allocations:
                next_reserved = int(bucket["reserved_micros"]) + amount
                next_status = _bucket_status(
                    plan,
                    bucket,
                    consumed=int(bucket["consumed_micros"]),
                    reserved=next_reserved,
                )
                cursor = connection.execute(
                    """
                    UPDATE quota_buckets
                    SET reserved_micros=?,status=?,state_revision=state_revision+1,updated_at=?
                    WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
                    """,
                    (
                        next_reserved,
                        next_status,
                        now,
                        tenant_id,
                        bucket["quota_bucket_id"],
                        bucket["state_revision"],
                    ),
                )
                # Bucket revision CAS 失败表示并发写入，整笔预留必须回滚。
                if cursor.rowcount != 1:
                    raise ApiError("gateway.quota_unavailable")
                connection.execute(
                    """
                    INSERT INTO quota_reservation_items(
                      quota_reservation_item_id,tenant_id,quota_reservation_id,plan_id,
                      quota_bucket_id,quota_type,priority,reserved_micros,consumed_micros,
                      released_micros,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?, ?,0,0,'active',?,?)
                    """,
                    (
                        f"qri_{uuid4().hex}",
                        tenant_id,
                        reservation_id,
                        plan["plan_id"],
                        bucket["quota_bucket_id"],
                        bucket["quota_type"],
                        priority,
                        amount,
                        now,
                        now,
                    ),
                )
            connection.commit()
            result = QuotaGuardResult(reservation_id, estimated_micros, str(plan["plan_id"]))
    # 公开业务错误原样保留稳定错误码，同时回滚所有未提交额度变更。
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    # 数据库和金额解析异常统一折叠，日志只记录类型而不记录请求正文。
    except (DbError, sqlite3.Error, TypeError, ValueError, DecimalException) as exc:
        if connection.in_transaction:
            connection.rollback()
        _log_unavailable(tenant_id, request_id, trace_id, exc)
        # 只向调用方暴露稳定额度错误码，底层异常文本不得穿透边界。
        raise ApiError("gateway.quota_unavailable") from exc
    finally:
        connection.close()
    # 耗尽事件需先提交再抛公开错误，确保失败请求仍可追踪且不丢通知事实。
    if pending_error is not None:
        raise pending_error
    _log_reserved(tenant_id, request_id, trace_id, result)
    return result


def release_request_quota(
    config: AppConfig,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
) -> bool:
    """在已知零费用终态释放一条 active Reservation。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        released = release_request_quota_in_transaction(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return released
    # 公开业务错误不得被二次包装，事务内释放失败仍需完整回滚。
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    # SQLite 和所有者字段异常统一收敛，避免底层细节泄露到 API。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        if connection.in_transaction:
            connection.rollback()
        # 释放异常统一折叠，避免数据库路径和内部状态泄露给 API 调用方。
        raise ApiError("gateway.quota_unavailable") from exc
    finally:
        connection.close()


def finalize_request_quota_failure(
    config: AppConfig,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    outstanding_cost_cny: Decimal,
) -> bool:
    """按 Provider 失败覆盖额缩小或释放 Reservation，不把风险估算记作 charge。"""

    outstanding_micros = _decimal_to_micros(outstanding_cost_cny, allow_zero=True)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if outstanding_micros == 0:
            changed = release_request_quota_in_transaction(
                connection,
                tenant_id=tenant_id,
                request_id=request_id,
                trace_id=trace_id,
            )
        else:
            changed = _resize_request_quota_exposure_in_transaction(
                connection,
                tenant_id=tenant_id,
                request_id=request_id,
                trace_id=trace_id,
                outstanding_micros=outstanding_micros,
            )
        connection.commit()
        return changed
    # 公开业务错误保持统一 Quota 合同，已开启事务必须完整回滚。
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    # owner、金额或数据库异常统一折叠，禁止原始异常和连接信息穿透。
    except (sqlite3.Error, TypeError, ValueError, DecimalException) as exc:
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("gateway.quota_unavailable") from exc
    finally:
        connection.close()


def release_request_quota_in_transaction(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    terminal_status: str = "released",
) -> bool:
    """在调用方写事务中释放额度，并保留明确的恢复终态。"""

    # 释放必须与调用方事实处于同一事务，禁止脱离事务产生部分状态。
    if not connection.in_transaction:
        raise sqlite3.IntegrityError("quota_release_transaction_missing")
    # 终态仅允许已知失败释放或结果未知恢复，其他值会破坏状态机。
    if terminal_status not in {"released", "reconciliation_required"}:
        raise sqlite3.IntegrityError("quota_release_status_invalid")
    reservation = connection.execute(
        """
        SELECT * FROM quota_reservations
        WHERE tenant_id=? AND request_id=?
        """,
        (tenant_id, request_id),
    ).fetchone()
    if reservation is None:
        return False
    # 即使 request_id 相同也必须核验 trace 所有者，阻止跨链路释放额度。
    if reservation["trace_id"] != trace_id:
        raise sqlite3.IntegrityError("quota_release_owner_mismatch")
    if reservation["status"] not in {"active", "exposure_pending"}:
        return False
    now = _database_now(connection)
    plan = connection.execute(
        "SELECT * FROM plans WHERE tenant_id=? AND plan_id=?",
        (tenant_id, reservation["plan_id"]),
    ).fetchone()
    # Reservation 必须仍绑定同租户套餐，缺失时禁止继续修改 Bucket。
    if plan is None:
        raise sqlite3.IntegrityError("quota_release_plan_missing")
    items = _load_reservation_items(connection, reservation)
    # 明细必须完整覆盖当前 outstanding，否则释放会造成额度凭空增加或残留。
    if not _reservation_items_are_valid(reservation, items):
        # 明细 owner、状态或剩余额不一致时必须中止当前事务并保留原事实。
        raise sqlite3.IntegrityError("quota_release_items_invalid")
    for item in items:
        consumed = int(item["consumed_micros"])
        released = int(item["released_micros"])
        remaining = int(item["reserved_micros"]) - consumed - released
        if remaining == 0:
            continue
        next_reserved = int(item["bucket_reserved"]) - remaining
        # Bucket 预留不得被扣成负数，发现不一致时整笔事务失败。
        if next_reserved < 0:
            raise sqlite3.IntegrityError("quota_release_bucket_invalid")
        cursor = connection.execute(
            """
            UPDATE quota_buckets
            SET reserved_micros=?,status=?,state_revision=state_revision+1,updated_at=?
            WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
            """,
            (
                next_reserved,
                _bucket_status(
                    plan,
                    item,
                    consumed=int(item["bucket_consumed"]),
                    reserved=next_reserved,
                ),
                now,
                tenant_id,
                item["quota_bucket_id"],
                item["bucket_revision"],
            ),
        )
        # 复合 owner 与 revision CAS 必须精确命中一行，防止迟到写覆盖。
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("quota_release_bucket_conflict")
        item_cursor = connection.execute(
            """
            UPDATE quota_reservation_items
            SET released_micros=?,status=?,updated_at=?
            WHERE tenant_id=? AND quota_reservation_item_id=?
              AND status IN('active','exposure_pending')
              AND consumed_micros=? AND released_micros=?
            """,
            (
                released + remaining,
                "settled" if consumed else "released",
                now,
                tenant_id,
                item["quota_reservation_item_id"],
                consumed,
                released,
            ),
        )
        # active/exposure_pending 明细只能释放当前剩余额，迟到 owner 不得覆盖。
        if item_cursor.rowcount != 1:
            raise sqlite3.IntegrityError("quota_release_item_conflict")
    if reservation["actual_micros"] is not None and terminal_status == "released":
        # 已有 canonical charge 的剩余风险只能形成 reconciliation 终态，不能伪装零费用释放。
        raise sqlite3.IntegrityError("quota_release_terminal_status_invalid")
    cursor = connection.execute(
        """
        UPDATE quota_reservations
        SET outstanding_micros=0,actual_micros=COALESCE(actual_micros,0),
            status=?,updated_at=?
        WHERE tenant_id=? AND quota_reservation_id=?
          AND status=? AND outstanding_micros=?
        """,
        (
            terminal_status,
            now,
            tenant_id,
            reservation["quota_reservation_id"],
            reservation["status"],
            reservation["outstanding_micros"],
        ),
    )
    # Reservation 终态写入必须精确命中同租户当前行，避免迟到覆盖。
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("quota_release_reservation_conflict")
    return True


def _resize_request_quota_exposure_in_transaction(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    outstanding_micros: int,
) -> bool:
    """把当前预留缩到仍可能计费的前缀，并保留 exposure_pending 恢复 owner。"""

    if not connection.in_transaction or outstanding_micros <= 0:
        raise sqlite3.IntegrityError("quota_exposure_resize_transaction_invalid")
    reservation = connection.execute(
        "SELECT * FROM quota_reservations WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    ).fetchone()
    if reservation is None:
        return False
    if reservation["trace_id"] != trace_id:
        raise sqlite3.IntegrityError("quota_exposure_resize_owner_mismatch")
    if reservation["status"] not in {"active", "exposure_pending"}:
        return False
    current_outstanding = int(reservation["outstanding_micros"])
    if outstanding_micros > current_outstanding:
        raise sqlite3.IntegrityError("quota_exposure_resize_amount_invalid")
    if (
        reservation["status"] == "exposure_pending"
        and outstanding_micros == current_outstanding
    ):
        return False
    plan = connection.execute(
        "SELECT * FROM plans WHERE tenant_id=? AND plan_id=?",
        (tenant_id, reservation["plan_id"]),
    ).fetchone()
    if plan is None:
        raise sqlite3.IntegrityError("quota_exposure_resize_plan_missing")
    items = _load_reservation_items(connection, reservation)
    if not _reservation_items_are_valid(reservation, items):
        raise sqlite3.IntegrityError("quota_exposure_resize_items_invalid")
    now = _database_now(connection)
    pending = outstanding_micros
    for item in items:
        consumed = int(item["consumed_micros"])
        released = int(item["released_micros"])
        remaining = int(item["reserved_micros"]) - consumed - released
        keep = min(pending, remaining)
        pending -= keep
        release_delta = remaining - keep
        if release_delta:
            next_bucket_reserved = int(item["bucket_reserved"]) - release_delta
            if next_bucket_reserved < 0:
                raise sqlite3.IntegrityError("quota_exposure_resize_bucket_invalid")
            bucket_cursor = connection.execute(
                """
                UPDATE quota_buckets
                SET reserved_micros=?,status=?,state_revision=state_revision+1,updated_at=?
                WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
                """,
                (
                    next_bucket_reserved,
                    _bucket_status(
                        plan,
                        item,
                        consumed=int(item["bucket_consumed"]),
                        reserved=next_bucket_reserved,
                    ),
                    now,
                    tenant_id,
                    item["quota_bucket_id"],
                    item["bucket_revision"],
                ),
            )
            if bucket_cursor.rowcount != 1:
                raise sqlite3.IntegrityError("quota_exposure_resize_bucket_conflict")
        next_status = "exposure_pending" if keep else ("settled" if consumed else "released")
        if release_delta or item["status"] != next_status:
            item_cursor = connection.execute(
                """
                UPDATE quota_reservation_items
                SET released_micros=?,status=?,updated_at=?
                WHERE tenant_id=? AND quota_reservation_item_id=?
                  AND status IN('active','exposure_pending')
                  AND consumed_micros=? AND released_micros=?
                """,
                (
                    released + release_delta,
                    next_status,
                    now,
                    tenant_id,
                    item["quota_reservation_item_id"],
                    consumed,
                    released,
                ),
            )
            if item_cursor.rowcount != 1:
                raise sqlite3.IntegrityError("quota_exposure_resize_item_conflict")
    if pending:
        raise sqlite3.IntegrityError("quota_exposure_resize_amount_invalid")
    cursor = connection.execute(
        """
        UPDATE quota_reservations
        SET outstanding_micros=?,status='exposure_pending',updated_at=?
        WHERE tenant_id=? AND quota_reservation_id=? AND status=?
          AND outstanding_micros=?
        """,
        (
            outstanding_micros,
            now,
            tenant_id,
            reservation["quota_reservation_id"],
            reservation["status"],
            current_outstanding,
        ),
    )
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("quota_exposure_resize_reservation_conflict")
    return True


def settle_quota_usage(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    usage_ledger_id: str,
    finops_ledger_entry_id: str,
    actual_micros: int | None,
    exposure_outstanding_micros: int = 0,
) -> QuotaSettlementSummary:
    """在调用方用量与财务同一事务中原子结算一条额度预留。"""

    # 结算必须与 Usage、FinOps 事实同事务提交，禁止出现部分成功。
    if not connection.in_transaction:
        raise sqlite3.IntegrityError("quota_settlement_transaction_missing")
    reservation = connection.execute(
        "SELECT * FROM quota_reservations WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    ).fetchone()
    if reservation is None:
        return QuotaSettlementSummary("unallocated", 0)
    # request_id 复用时仍需匹配原始 trace，阻止跨请求结算额度。
    if reservation["trace_id"] != trace_id:
        raise sqlite3.IntegrityError("quota_settlement_owner_mismatch")
    exposure_outstanding = _bounded_micros(exposure_outstanding_micros)
    estimated = int(reservation["estimated_micros"])
    if exposure_outstanding > estimated:
        raise sqlite3.IntegrityError("quota_settlement_exposure_amount_invalid")
    amount_basis = "actual_cny"
    if actual_micros is None:
        # admission estimate 已同时覆盖主备最坏费用；主路由风险单独保留时必须从估算
        # charge 中扣除，否则会把同一暴露重复计入消费和 outstanding。
        actual = estimated - exposure_outstanding
        amount_basis = "admission_estimate"
    else:
        actual = _bounded_micros(actual_micros)
    existing = connection.execute(
        """
        SELECT * FROM quota_usage_settlements
        WHERE tenant_id=? AND usage_ledger_id=?
        """,
        (tenant_id, usage_ledger_id),
    ).fetchone()
    if existing is not None:
        # 重放只能读取同一请求、链路和财务分录的既有终态，不可漂移 owner。
        if (
            existing["request_id"] != request_id
            or existing["trace_id"] != trace_id
            or existing["finops_ledger_entry_id"] != finops_ledger_entry_id
            or existing["plan_id"] != reservation["plan_id"]
            or existing["quota_reservation_id"] != reservation["quota_reservation_id"]
            or int(existing["amount_micros"]) != actual
            or int(existing["exposure_outstanding_micros"]) != exposure_outstanding
            or existing["amount_basis"] != amount_basis
            or existing["order_version"] != QUOTA_ORDER_VERSION
            or reservation["status"] == "active"
        ):
            # 重放 owner、金额依据或终态不一致时禁止覆盖既有结算，整笔事务失败。
            raise sqlite3.IntegrityError("quota_settlement_replay_conflict")
        return QuotaSettlementSummary(
            str(existing["allocation_class"]),
            int(existing["allocation_count"]),
        )

    plan = connection.execute(
        "SELECT * FROM plans WHERE tenant_id=? AND plan_id=?",
        (tenant_id, reservation["plan_id"]),
    ).fetchone()
    # 预留关联套餐缺失时不能推断额度归属，必须中止整笔账务事务。
    if plan is None:
        raise sqlite3.IntegrityError("quota_settlement_plan_missing")
    now = _database_now(connection)
    allocations: dict[str, tuple[str, int]] = {}
    remaining = actual

    if reservation["status"] in {"active", "exposure_pending"}:
        items = _load_reservation_items(connection, reservation)
        # 结算明细必须完整覆盖原预留金额，任何缺失都应回滚整笔账务事实。
        if (
            not _reservation_items_are_valid(reservation, items)
            or any(int(item["consumed_micros"]) != 0 for item in items)
            or exposure_outstanding > int(reservation["outstanding_micros"])
        ):
            # 预留明细 owner、顺序或总额不一致时不能分摊实际费用，必须回滚账务事务。
            raise sqlite3.IntegrityError("quota_reservation_items_invalid")
        # 原预留必须先为未决主路由风险留下完整空间；其余部分才承接 canonical actual。
        # actual 超过该空间时继续走下方 overage，不得让真实 Usage 因估算偏差永久无法入账。
        reserved_actual_remaining = min(
            actual,
            int(reservation["outstanding_micros"]) - exposure_outstanding,
        )
        pending = exposure_outstanding
        for item in items:
            previous_consumed = int(item["consumed_micros"])
            previous_released = int(item["released_micros"])
            item_remaining = (
                int(item["reserved_micros"])
                - previous_consumed
                - previous_released
            )
            # canonical actual 始终先按冻结额度顺序消费；未决暴露只保留其后的余额，
            # 防止主路由风险长期占住 trial 而把备用真实费用错误扣到 free/paid。
            consumed = min(reserved_actual_remaining, item_remaining)
            reserved_actual_remaining -= consumed
            remaining -= consumed
            kept = min(pending, item_remaining - consumed)
            pending -= kept
            released = item_remaining - consumed - kept
            next_bucket_reserved = int(item["bucket_reserved"]) - consumed - released
            next_bucket_consumed = int(item["bucket_consumed"]) + consumed
            if consumed or released:
                cursor = connection.execute(
                    """
                    UPDATE quota_buckets
                    SET consumed_micros=?,reserved_micros=?,status=?,
                        state_revision=state_revision+1,updated_at=?
                    WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
                    """,
                    (
                        next_bucket_consumed,
                        next_bucket_reserved,
                        _bucket_status(
                            plan,
                            item,
                            consumed=next_bucket_consumed,
                            reserved=next_bucket_reserved,
                        ),
                        now,
                        tenant_id,
                        item["quota_bucket_id"],
                        item["bucket_revision"],
                    ),
                )
                # Bucket revision CAS 必须精确命中，避免并发结算发生迟到覆盖。
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("quota_bucket_settlement_conflict")
            next_status = (
                "exposure_pending"
                if kept
                else ("settled" if previous_consumed + consumed else "released")
            )
            if consumed or released or item["status"] != next_status:
                item_cursor = connection.execute(
                    """
                    UPDATE quota_reservation_items
                    SET consumed_micros=?,released_micros=?,status=?,updated_at=?
                    WHERE tenant_id=? AND quota_reservation_item_id=?
                      AND status IN('active','exposure_pending')
                      AND consumed_micros=? AND released_micros=?
                    """,
                    (
                        previous_consumed + consumed,
                        previous_released + released,
                        next_status,
                        now,
                        tenant_id,
                        item["quota_reservation_item_id"],
                        previous_consumed,
                        previous_released,
                    ),
                )
                # 明细只允许从当前 active/exposure owner 前进，终态空明细不得被再次改写。
                if item_cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("quota_reservation_item_settlement_conflict")
            if consumed:
                allocations[str(item["quota_type"])] = (
                    str(item["quota_bucket_id"]),
                    consumed,
                )

        if pending or reserved_actual_remaining:
            raise sqlite3.IntegrityError("quota_exposure_settlement_amount_invalid")

        if remaining and plan["status"] == "active":
            for bucket in _ordered_buckets(connection, tenant_id, str(plan["plan_id"])):
                available = _available_for_settlement(plan, bucket)
                consumed = min(remaining, available)
                if not consumed:
                    continue
                next_consumed = int(bucket["consumed_micros"]) + consumed
                cursor = connection.execute(
                    """
                    UPDATE quota_buckets
                    SET consumed_micros=?,status=?,state_revision=state_revision+1,updated_at=?
                    WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
                    """,
                    (
                        next_consumed,
                        _bucket_status(
                            plan,
                            bucket,
                            consumed=next_consumed,
                            reserved=int(bucket["reserved_micros"]),
                        ),
                        now,
                        tenant_id,
                        bucket["quota_bucket_id"],
                        bucket["state_revision"],
                    ),
                )
                # 超额补扣也必须通过 Bucket revision CAS，不能覆盖并发消费。
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("quota_overage_settlement_conflict")
                quota_type = str(bucket["quota_type"])
                previous = allocations.get(quota_type)
                allocations[quota_type] = (
                    str(bucket["quota_bucket_id"]),
                    consumed + (previous[1] if previous else 0),
                )
                remaining -= consumed
                if not remaining:
                    break
        reservation_status = (
            "exposure_pending"
            if exposure_outstanding
            else ("overage" if remaining else "settled")
        )
        cursor = connection.execute(
            """
            UPDATE quota_reservations
            SET outstanding_micros=?,actual_micros=?,unallocated_micros=?,
                status=?,updated_at=?
            WHERE tenant_id=? AND quota_reservation_id=? AND status=?
              AND outstanding_micros=?
            """,
            (
                exposure_outstanding,
                actual,
                remaining,
                reservation_status,
                now,
                tenant_id,
                reservation["quota_reservation_id"],
                reservation["status"],
                reservation["outstanding_micros"],
            ),
        )
        # Reservation 结算必须精确命中当前 owner；风险分项保留 pending，不伪装 charge。
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("quota_reservation_settlement_conflict")
    elif reservation["status"] in {"released", "reconciliation_required"}:
        if exposure_outstanding:
            raise sqlite3.IntegrityError("quota_settlement_exposure_terminal_conflict")
        # 已知失败或超时仍未知的请求都已释放 Bucket 预留；若 Usage 随后迟到，
        # 只能形成显式 overage，不能重新占用可能已授予其他请求的余额。
        remaining = actual
    else:
        # 未识别终态不能推断可消费余额，必须中止以保护账本一致性。
        raise sqlite3.IntegrityError("quota_reservation_terminal_without_settlement")

    allocated = actual - remaining
    ordered_types = [kind for kind in QUOTA_CONSUMPTION_ORDER if kind in allocations]
    allocation_class = classify_allocations(ordered_types, amount_micros=allocated)
    settlement_id = f"qset_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO quota_usage_settlements(
          quota_usage_settlement_id,tenant_id,plan_id,quota_reservation_id,
          usage_ledger_id,finops_ledger_entry_id,request_id,trace_id,amount_micros,
          exposure_outstanding_micros,allocated_micros,unallocated_micros,
          amount_basis,allocation_class,
          allocation_count,status,plan_revision,order_version,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            settlement_id,
            tenant_id,
            plan["plan_id"],
            reservation["quota_reservation_id"],
            usage_ledger_id,
            finops_ledger_entry_id,
            request_id,
            trace_id,
            actual,
            exposure_outstanding,
            allocated,
            remaining,
            amount_basis,
            allocation_class,
            len(ordered_types),
            "overage" if remaining else "settled",
            plan["state_revision"],
            QUOTA_ORDER_VERSION,
            now,
        ),
    )
    for sequence, quota_type in enumerate(ordered_types, start=1):
        bucket_id, amount = allocations[quota_type]
        connection.execute(
            """
            INSERT INTO quota_usage_allocations(
              quota_usage_allocation_id,tenant_id,quota_usage_settlement_id,
              plan_id,quota_bucket_id,quota_type,sequence,amount_micros,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                f"qalloc_{uuid4().hex}",
                tenant_id,
                settlement_id,
                plan["plan_id"],
                bucket_id,
                quota_type,
                sequence,
                amount,
                now,
            ),
        )
    return QuotaSettlementSummary(str(allocation_class), len(ordered_types))


def reconcile_expired_quota_reservations(
    config: AppConfig,
    *,
    limit: int = 50,
    connection: sqlite3.Connection | None = None,
) -> int:
    """补结算或释放一批已超期 Reservation，迟到 Usage 保留为 overage。"""

    # 恢复批量必须严格有界，防止错误配置形成长事务或资源耗尽。
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("quota_reconciliation_limit_invalid")
    owns_connection = connection is None
    database = connection or _connect(config)
    processed = 0
    try:
        now = _database_now(database)
        if not _has_expired_quota_reservations(database, now):
            return 0
        database.execute("BEGIN IMMEDIATE")
        now = _database_now(database)
        reservations = database.execute(
            """
            SELECT * FROM quota_reservations
            WHERE status IN('active','exposure_pending')
              AND unixepoch(expires_at)<=unixepoch(?)
            ORDER BY expires_at,tenant_id,quota_reservation_id
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        for reservation in reservations:
            settlements = database.execute(
                """
                SELECT * FROM quota_usage_settlements
                WHERE tenant_id=? AND quota_reservation_id=?
                ORDER BY created_at,quota_usage_settlement_id
                """,
                (reservation["tenant_id"], reservation["quota_reservation_id"]),
            ).fetchall()
            # 一个请求只有一条 canonical Usage；多条结算表示恢复 owner 已出现歧义。
            if len(settlements) > 1:
                raise sqlite3.IntegrityError("quota_reconciliation_settlement_ambiguous")
            charges = database.execute(
                """
                SELECT entry.finops_ledger_entry_id,entry.usage_ledger_id,
                       entry.normalized_cny_micros,entry.cny_normalization_status
                FROM finops_ledger_entries entry
                JOIN usage_ledger usage
                  ON usage.tenant_id=entry.tenant_id
                 AND usage.usage_ledger_id=entry.usage_ledger_id
                 AND usage.request_id=entry.source_request_id
                WHERE entry.tenant_id=? AND entry.source_request_id=?
                  AND entry.source_trace_id=? AND entry.entry_type='usage_charge'
                  AND entry.direction='debit'
                ORDER BY entry.ledger_sequence
                """,
                (
                    reservation["tenant_id"],
                    reservation["request_id"],
                    reservation["trace_id"],
                ),
            ).fetchall()
            # 单次请求只能存在一条 canonical charge，多条表示账本歧义需回滚。
            if len(charges) > 1:
                raise sqlite3.IntegrityError("quota_reconciliation_charge_ambiguous")
            if settlements:
                # fallback 成功可以留下 canonical charge 与仍未决的主路由暴露；到期时只释放
                # 暴露，不改写已结算费用，也不把未知上游结果伪装成已知零费用。
                if reservation["status"] != "exposure_pending" or len(charges) != 1:
                    raise sqlite3.IntegrityError("quota_reconciliation_settlement_invalid")
                settlement = settlements[0]
                charge = charges[0]
                settle_quota_usage(
                    database,
                    tenant_id=str(reservation["tenant_id"]),
                    request_id=str(reservation["request_id"]),
                    trace_id=str(reservation["trace_id"]),
                    usage_ledger_id=str(charge["usage_ledger_id"]),
                    finops_ledger_entry_id=str(charge["finops_ledger_entry_id"]),
                    actual_micros=_normalized_charge_micros(charge),
                    exposure_outstanding_micros=int(
                        settlement["exposure_outstanding_micros"]
                    ),
                )
                released = release_request_quota_in_transaction(
                    database,
                    tenant_id=str(reservation["tenant_id"]),
                    request_id=str(reservation["request_id"]),
                    trace_id=str(reservation["trace_id"]),
                    terminal_status="reconciliation_required",
                )
                if not released:
                    raise sqlite3.IntegrityError("quota_reconciliation_release_lost")
            elif charges:
                charge = charges[0]
                # 尚无 Settlement 时 canonical charge 是唯一金额事实；未决失败估算不能继续
                # 占用额度，也不能作为 charge 叠加，因此以 exposure=0 补齐原子结算。
                settle_quota_usage(
                    database,
                    tenant_id=str(reservation["tenant_id"]),
                    request_id=str(reservation["request_id"]),
                    trace_id=str(reservation["trace_id"]),
                    usage_ledger_id=str(charge["usage_ledger_id"]),
                    finops_ledger_entry_id=str(charge["finops_ledger_entry_id"]),
                    actual_micros=_normalized_charge_micros(charge),
                    exposure_outstanding_micros=0,
                )
            else:
                request_log = database.execute(
                    """
                    SELECT status,trace_id FROM request_logs
                    WHERE tenant_id=? AND request_id=?
                    """,
                    (reservation["tenant_id"], reservation["request_id"]),
                ).fetchone()
                # 恢复释放前复核请求链路所有者，避免相同 request_id 跨链路误释放。
                if request_log is not None and request_log["trace_id"] != reservation["trace_id"]:
                    raise sqlite3.IntegrityError("quota_reconciliation_request_owner_mismatch")
                routing_attempt = database.execute(
                    """
                    SELECT 1 FROM routing_attempts
                    WHERE tenant_id=? AND request_id=? AND trace_id=?
                    LIMIT 1
                    """,
                    (
                        reservation["tenant_id"],
                        reservation["request_id"],
                        reservation["trace_id"],
                    ),
                ).fetchone()
                terminal_status = (
                    "released"
                    if (
                        reservation["status"] == "active"
                        and request_log is not None
                        and request_log["status"] == "failed"
                        and routing_attempt is None
                    )
                    else "reconciliation_required"
                )
                released = release_request_quota_in_transaction(
                    database,
                    tenant_id=str(reservation["tenant_id"]),
                    request_id=str(reservation["request_id"]),
                    trace_id=str(reservation["trace_id"]),
                    terminal_status=terminal_status,
                )
                # 选中的 active 预留必须在本事务被终结，否则恢复状态已经漂移。
                if not released:
                    raise sqlite3.IntegrityError("quota_reconciliation_release_lost")
            processed += 1
        database.commit()
    # 恢复异常统一回滚并折叠为公开错误，日志不写数据库或请求原文。
    except (DbError, sqlite3.Error, TypeError, ValueError, DecimalException) as exc:
        if database.in_transaction:
            database.rollback()
        _log_reconciliation_failed(exc)
        # 恢复错误仅暴露稳定代码，具体数据库异常不得进入响应或日志正文。
        raise ApiError("gateway.quota_unavailable") from exc
    finally:
        if owns_connection:
            database.close()
    if processed:
        logger.info(
            "quota.reconciled",
            extra={"event": "quota.reconciled", "payload": {"processed_count": processed}},
        )
    return processed


def _has_expired_quota_reservations(connection: sqlite3.Connection, now: str) -> bool:
    """Avoid taking SQLite's single writer lock when reconciliation has no work."""

    return (
        connection.execute(
            """
            SELECT 1 FROM quota_reservations
            WHERE status IN('active','exposure_pending')
              AND unixepoch(expires_at)<=unixepoch(?)
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        is not None
    )


def _normalized_charge_micros(charge: sqlite3.Row) -> int | None:
    """从权威财务分录提取可结算微元，未归一金额保持结果未知。"""

    status = charge["cny_normalization_status"]
    value = charge["normalized_cny_micros"]
    if status == "unavailable" and value is None:
        return None
    # 除明确 unavailable 外只能接受 normalized，禁止使用含糊金额结算额度。
    if status != "normalized":
        raise sqlite3.IntegrityError("quota_reconciliation_charge_invalid")
    return _bounded_micros(value)


def _estimate_worst_cost(
    connection: sqlite3.Connection,
    *,
    route: object,
    payload: dict[str, Any],
    request_kind: str,
    fallback_route: object | None,
    fallback_routes: tuple[object, ...] = (),
    token_estimate: object | None,
    price_as_of: datetime | None,
) -> Decimal:
    """按主路由与可选回退路由冻结最坏人民币成本作为准入上限。"""

    from metahub_gateway.budgets.service import _estimate_request_cost

    initial = _estimate_request_cost(
        connection,
        route,
        request_kind,
        payload,
        token_estimate=token_estimate,
        price_as_of=price_as_of,
    )
    # 主路由缺少版本化价格时关闭准入，不能用零成本绕过套餐额度。
    if not initial.price_available:
        raise ApiError("gateway.quota_unavailable")
    amount = initial.cost_cny
    candidates = fallback_routes or ((fallback_route,) if fallback_route is not None else ())
    # 所有显式回退候选都可能实际被调用，必须叠加整条链的最坏成本。
    for candidate in candidates:
        fallback = _estimate_request_cost(
            connection,
            candidate,
            request_kind,
            payload,
            token_estimate=token_estimate,
            price_as_of=price_as_of,
        )
        # 回退链路同样必须具备冻结价格，否则最坏成本上限并不可信。
        if not fallback.price_available:
            raise ApiError("gateway.quota_unavailable")
        amount += fallback.cost_cny
    return amount


def _ordered_buckets(
    connection: sqlite3.Connection,
    tenant_id: str,
    plan_id: str,
) -> list[sqlite3.Row]:
    """按冻结消费顺序加载同租户同套餐的四类额度 Bucket。"""

    rows = connection.execute(
        """
        SELECT * FROM quota_buckets
        WHERE tenant_id=? AND plan_id=?
        ORDER BY CASE quota_type
          WHEN 'trial' THEN 1 WHEN 'free' THEN 2 WHEN 'gift' THEN 3 ELSE 4 END
        """,
        (tenant_id, plan_id),
    ).fetchall()
    # 四类 Bucket 缺失或顺序漂移会改变扣减语义，必须关闭额度服务。
    if len(rows) != 4 or tuple(str(row["quota_type"]) for row in rows) != tuple(
        QUOTA_CONSUMPTION_ORDER
    ):
        # Bucket 集合不完整会改变消费顺序，禁止以残缺数据继续准入。
        raise ApiError("gateway.quota_unavailable")
    return rows


def _load_reservation_items(
    connection: sqlite3.Connection,
    reservation: sqlite3.Row,
) -> list[sqlite3.Row]:
    """加载 Reservation item 及其同租户 Bucket owner，供重放和结算复核。"""

    return connection.execute(
        """
        SELECT item.*,bucket.plan_id AS bucket_plan_id,
               bucket.quota_type AS bucket_quota_type,
               bucket.state_revision AS bucket_revision,bucket.granted_micros,
               bucket.consumed_micros AS bucket_consumed,
               bucket.reserved_micros AS bucket_reserved,
               bucket.status AS bucket_status
        FROM quota_reservation_items item
        JOIN quota_buckets bucket
          ON bucket.tenant_id=item.tenant_id
         AND bucket.quota_bucket_id=item.quota_bucket_id
        WHERE item.tenant_id=? AND item.quota_reservation_id=?
        ORDER BY item.priority,item.quota_reservation_item_id
        """,
        (reservation["tenant_id"], reservation["quota_reservation_id"]),
    ).fetchall()


def _reservation_items_are_valid(
    reservation: sqlite3.Row,
    items: list[sqlite3.Row],
) -> bool:
    """验证 active/exposure item 的 owner、冻结顺序与剩余额守恒。"""

    reservation_status = str(reservation["status"])
    if (
        reservation["order_version"] != QUOTA_ORDER_VERSION
        or reservation_status not in {"active", "exposure_pending"}
        or not items
    ):
        return False
    original_total = 0
    outstanding_total = 0
    seen_types: set[str] = set()
    for item in items:
        quota_type = str(item["quota_type"])
        if quota_type not in QUOTA_CONSUMPTION_ORDER or quota_type in seen_types:
            return False
        seen_types.add(quota_type)
        reserved = int(item["reserved_micros"])
        consumed = int(item["consumed_micros"])
        released = int(item["released_micros"])
        remaining = reserved - consumed - released
        expected_priority = QUOTA_CONSUMPTION_ORDER.index(quota_type) + 1
        expected_status = (
            "active"
            if reservation_status == "active"
            else (
                "exposure_pending"
                if remaining
                else ("settled" if consumed else "released")
            )
        )
        if (
            item["plan_id"] != reservation["plan_id"]
            or item["bucket_plan_id"] != reservation["plan_id"]
            or item["bucket_quota_type"] != quota_type
            or int(item["priority"]) != expected_priority
            or item["status"] != expected_status
            or reserved <= 0
            or consumed < 0
            or released < 0
            or remaining < 0
            or (reservation_status == "active" and (consumed != 0 or released != 0))
            or int(item["bucket_reserved"]) < remaining
        ):
            return False
        original_total += reserved
        outstanding_total += remaining
    return (
        original_total == int(reservation["estimated_micros"])
        and outstanding_total == int(reservation["outstanding_micros"])
    )


def _available_for_admission(plan: sqlite3.Row, bucket: sqlite3.Row) -> int:
    """计算准入可用余额，并排除停用套餐或已到期试用额度。"""

    if plan["status"] != "active":
        return 0
    if bucket["quota_type"] == "trial" and plan["trial_status"] != "active":
        return 0
    return _available(bucket)


def _available_for_settlement(plan: sqlite3.Row, bucket: sqlite3.Row) -> int:
    """计算结算补扣余额，已到期试用额度不允许继续被消费。"""

    if bucket["quota_type"] == "trial" and plan["trial_status"] != "active":
        return 0
    return _available(bucket)


def _available(bucket: sqlite3.Row) -> int:
    """从授予额扣除已消费和已预留，返回可信的非负可用微元。"""

    value = (
        int(bucket["granted_micros"])
        - int(bucket["consumed_micros"])
        - int(bucket["reserved_micros"])
    )
    # 负可用额表示账本约束已失真，禁止将异常数值继续用于准入。
    if value < 0:
        raise ApiError("gateway.quota_unavailable")
    return value


def _bucket_status(
    plan: sqlite3.Row,
    bucket: sqlite3.Row,
    *,
    consumed: int,
    reserved: int,
) -> str:
    """依据套餐与余额事实推导 Bucket 状态，不读取客户端输入。"""

    if plan["status"] == "suspended":
        return "suspended"
    if bucket["quota_type"] == "trial" and plan["trial_status"] != "active":
        return "expired"
    return "active" if int(bucket["granted_micros"]) > consumed + reserved else "exhausted"


def _expire_trial_if_due(
    connection: sqlite3.Connection,
    plan: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    actor_id: str,
    now: str,
) -> bool:
    """在当前事务以套餐 revision CAS 到期试用并生成唯一安全事件。"""

    if (
        plan["trial_status"] != "active"
        or connection.execute(
            "SELECT unixepoch(?)>=unixepoch(?)",
            (now, plan["trial_ends_at"]),
        ).fetchone()[0]
        != 1
    ):
        return False
    cursor = connection.execute(
        """
        UPDATE plans SET trial_status='expired',state_revision=state_revision+1,updated_at=?
        WHERE tenant_id=? AND plan_id=? AND state_revision=? AND trial_status='active'
        """,
        (now, plan["tenant_id"], plan["plan_id"], plan["state_revision"]),
    )
    # 套餐 revision CAS 必须精确命中，防止并发到期流程互相覆盖。
    if cursor.rowcount != 1:
        raise ApiError("gateway.quota_unavailable")
    bucket = connection.execute(
        """
        SELECT * FROM quota_buckets
        WHERE tenant_id=? AND plan_id=? AND quota_type='trial'
        """,
        (plan["tenant_id"], plan["plan_id"]),
    ).fetchone()
    # 试用套餐必须具有同租户 trial Bucket，缺失时不能伪造到期终态。
    if bucket is None:
        raise ApiError("gateway.quota_unavailable")
    bucket_cursor = connection.execute(
        """
        UPDATE quota_buckets SET status='expired',state_revision=state_revision+1,updated_at=?
        WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
        """,
        (now, plan["tenant_id"], bucket["quota_bucket_id"], bucket["state_revision"]),
    )
    # Bucket revision CAS 必须与套餐到期同事务成功，避免状态部分更新。
    if bucket_cursor.rowcount != 1:
        raise ApiError("gateway.quota_unavailable")
    _insert_trial_expired_event(
        connection,
        plan=plan,
        request_id=request_id,
        trace_id=trace_id,
        actor_id=actor_id,
        occurred_at=now,
    )
    return True


def _insert_trial_expired_event(
    connection: sqlite3.Connection,
    *,
    plan: sqlite3.Row,
    request_id: str,
    trace_id: str,
    actor_id: str,
    occurred_at: str,
) -> None:
    """写入试用到期事件及恢复 outbox，仅包含固定安全摘要字段。"""

    payload = {
        "schema_version": 1,
        "plan_id": str(plan["plan_id"]),
        "trial_cycle": 1,
        "expired_at": occurred_at,
        "effective_tier": str(plan["base_tier"]),
    }
    _insert_event(
        connection,
        plan=plan,
        event_type="trial.expired",
        quota_generation=None,
        trial_cycle=1,
        request_id=request_id,
        trace_id=trace_id,
        actor_id=actor_id,
        payload=payload,
        occurred_at=occurred_at,
    )


def _insert_quota_exhausted_event(
    connection: sqlite3.Connection,
    *,
    plan: sqlite3.Row,
    required_micros: int,
    available_micros: int,
    request_id: str,
    trace_id: str,
    occurred_at: str,
) -> None:
    """写入额度耗尽事件及恢复 outbox，不携带请求正文或凭据。"""

    effective = "trial" if plan["trial_status"] == "active" else str(plan["base_tier"])
    payload = {
        "schema_version": 1,
        "plan_id": str(plan["plan_id"]),
        "quota_generation": int(plan["quota_generation"]),
        "required_cny": format_cny_micros(required_micros),
        "available_cny": format_cny_micros(available_micros),
        "reason_code": "quota_exhausted",
        "effective_tier": effective,
    }
    _insert_event(
        connection,
        plan=plan,
        event_type="quota.exhausted",
        quota_generation=int(plan["quota_generation"]),
        trial_cycle=None,
        request_id=request_id,
        trace_id=trace_id,
        actor_id="quota-admission",
        payload=payload,
        occurred_at=occurred_at,
    )


def _insert_event(
    connection: sqlite3.Connection,
    *,
    plan: sqlite3.Row,
    event_type: str,
    quota_generation: int | None,
    trial_cycle: int | None,
    request_id: str,
    trace_id: str,
    actor_id: str,
    payload: dict[str, object],
    occurred_at: str,
) -> None:
    """先校验安全事件合同，再原子持久化租户事件和待投递记录。"""

    event_id = f"qevt_{uuid4().hex}"
    build_webhook_event(
        tenant_id=str(plan["tenant_id"]),
        event_type=event_type,
        event_id=event_id,
        timestamp=_parse_time(occurred_at),
        actor_id=actor_id,
        object_id=str(plan["plan_id"]),
        trace_id=trace_id,
        source_request_id=request_id,
        payload=payload,
    )
    cursor = connection.execute(
        """
        INSERT INTO quota_events(
          quota_event_id,tenant_id,plan_id,quota_bucket_id,event_type,
          quota_generation,trial_cycle,source_request_id,source_trace_id,actor_id,
          payload_json,occurred_at
        ) VALUES(?,?,?,NULL,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING
        """,
        (
            event_id,
            plan["tenant_id"],
            plan["plan_id"],
            event_type,
            quota_generation,
            trial_cycle,
            request_id,
            trace_id,
            actor_id,
            _json(payload),
            occurred_at,
        ),
    )
    if cursor.rowcount != 1:
        return
    outbox_cursor = connection.execute(
        """
        INSERT INTO quota_event_outbox(
          quota_event_outbox_id,tenant_id,quota_event_id,status,attempt_count,
          lease_generation,next_attempt_at,created_at,updated_at
        ) VALUES(?,?,?,'pending',0,0,?,?,?)
        """,
        (
            f"qobx_{uuid4().hex}",
            plan["tenant_id"],
            event_id,
            occurred_at,
            occurred_at,
            occurred_at,
        ),
    )
    # 新事件必须同时生成同租户 outbox，否则回滚以避免永久丢失通知。
    if outbox_cursor.rowcount != 1:
        raise ApiError("gateway.quota_unavailable")


def _decimal_to_micros(value: Decimal, *, allow_zero: bool) -> int:
    """把有限非负人民币 Decimal 精确转换为有界整数微元。"""

    # 只接受 Decimal 本体，拒绝浮点、非有限值和负数污染额度账本。
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise ApiError("gateway.quota_unavailable")
    try:
        micros_decimal = value * _MICROS
        micros = int(micros_decimal)
    # 数值转换异常统一折叠为额度不可用，不回显原始金额或内部异常。
    except (DecimalException, InvalidOperation, OverflowError) as exc:
        raise ApiError("gateway.quota_unavailable") from exc
    # 任何精度损失、越界或非法零额都必须关闭结算，禁止近似记账。
    if (
        Decimal(micros) != micros_decimal
        or micros > MAX_CNY_MICROS
        or (not allow_zero and not micros)
    ):
        # 换算结果不精确或越界时拒绝记账，不能截断为近似微元。
        raise ApiError("gateway.quota_unavailable")
    return micros


def _bounded_micros(value: object) -> int:
    """校验数据库结算微元为非布尔的有界非负整数。"""

    # 结算金额必须是严格整数且处于合同上限内，异常值不得进入账本。
    if type(value) is not int or not 0 <= value <= MAX_CNY_MICROS:
        raise sqlite3.IntegrityError("quota_actual_amount_invalid")
    return value


def _database_now(connection: sqlite3.Connection) -> str:
    """从当前 SQLite 事务读取规范 UTC 秒，避免应用时钟漂移。"""

    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    # 数据库未返回时间属于基础设施故障，不能继续生成租约或审计时间。
    if row is None:
        raise ApiError("gateway.quota_unavailable")
    value = str(row[0])
    _parse_time(value)
    return value


def _parse_time(value: object) -> datetime:
    """严格解析无小数的规范 UTC 秒文本，并拒绝无时区时间。"""

    # 时间只接受数据库规范 UTC 秒格式，拒绝类型和精度漂移。
    if type(value) is not str or not value.endswith("Z") or "." in value:
        raise ValueError("quota_time_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 往返格式必须完全一致，避免模糊时区或非规范文本进入审计链路。
    if parsed.tzinfo is None or _db_time(parsed) != value:
        raise ValueError("quota_time_invalid")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """将已知时区时间规范化为数据库使用的 UTC 秒文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: object) -> str:
    """以稳定键序和 ASCII 转义序列化额度事件安全摘要。"""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _connect(config: AppConfig) -> sqlite3.Connection:
    """建立已启用完整约束的数据库连接，并折叠底层连接错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库连接错误不得暴露路径或驱动细节，只返回统一额度错误。
    except DbError as exc:
        raise ApiError("gateway.quota_unavailable") from exc


def _log_reserved(
    tenant_id: str,
    request_id: str,
    trace_id: str,
    result: QuotaGuardResult,
) -> None:
    """记录额度预留的安全标识与整数金额，不写入请求内容。"""

    logger.info(
        "quota.reserved",
        extra={
            "event": "quota.reserved",
            "tenant_id": tenant_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "object_id": result.reservation_id,
            "payload": {"estimated_micros": result.estimated_micros},
        },
    )


def _log_unavailable(
    tenant_id: str,
    request_id: str,
    trace_id: str,
    exc: BaseException,
) -> None:
    """记录额度不可用的稳定错误和异常类型，不写异常原文。"""

    logger.warning(
        "quota.unavailable",
        extra={
            "event": "quota.unavailable",
            "tenant_id": tenant_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "error_code": "gateway.quota_unavailable",
            "payload": {"exception_type": exc.__class__.__name__},
        },
    )


def _log_reconciliation_failed(exc: BaseException) -> None:
    """记录恢复失败的稳定事件及异常类型，不泄露数据库细节。"""

    logger.warning(
        "quota.reconciliation_failed",
        extra={
            "event": "quota.reconciliation_failed",
            "error_code": "gateway.quota_unavailable",
            "payload": {"exception_type": exc.__class__.__name__},
        },
    )


__all__ = [
    "enforce_quota_guard",
    "finalize_request_quota_failure",
    "reconcile_expired_quota_reservations",
    "release_request_quota",
    "release_request_quota_in_transaction",
    "settle_quota_usage",
]
