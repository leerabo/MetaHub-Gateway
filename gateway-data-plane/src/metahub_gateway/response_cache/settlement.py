"""Atomic settlement for a verified Response Cache candidate.

The cached payload stays in memory until the database transaction is known to
have committed.  Every durable owner used by a hit is generated before the
transaction so a failed ``commit`` can be resolved with an exact fresh read.

命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
from dataclasses import dataclass, field, replace
from decimal import Decimal, DecimalException
from typing import Literal
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.response_cache import (
    CACHE_KEY_SCHEMA_VERSION,
    CacheHitWebhookPayload,
    CacheInvalidatedWebhookPayload,
    EncryptedResponseCachePayload,
    ResponseCacheAmount,
    ResponseCacheHitAmounts,
    build_route_revision_digest,
    calculate_saved_amount,
    response_cache_secret_fingerprint,
)
from metahub_shared.runtime.budget_reservations import (
    set_request_budget_reservations_outstanding,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_gateway.budgets.models import EstimatedRequestCost
from metahub_gateway.output_safety.models import (
    OutputSafetyStoreError,
    OutputSafetyTerminalState,
    PreparedOutputSafetyScan,
)
from metahub_gateway.output_safety.service import (
    finalize_output_safety_scan_in_transaction,
)
from metahub_gateway.providers.keypool import snapshot_response_cache_credential_scope
from metahub_gateway.quota import (
    release_request_quota_in_transaction,
    settle_quota_usage,
)
from metahub_gateway.routing.models import GatewayRoute
from metahub_gateway.usage.ledger import (
    CostEstimate,
    PriceSnapshotEvidence,
    RequestLogContext,
    _cost_amounts,
    _decimal_value,
    _ensure_replay_request_log,
    _insert_price_snapshot_evidence,
    _money_amount,
    _normalize_currency,
    record_request_log,
)

from .models import ResponseCacheCandidate, ResponseCacheSettlementIds
from .repository import (
    _database_now,
    _entry_owner,
    _load_enabled_policy,
    _parse_db_time,
    _route_revision_input,
)

logger = logging.getLogger("metahub.gateway.response_cache_settlement")

# 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
_SETTLEMENT_ERROR = "gateway.response_cache_settlement_unavailable"
_OUTPUT_SAFETY_ERROR = "gateway.output_safety_unavailable"
_CONTENT_POLICY_ERROR = "content_policy_violation"
_UNPRICED_SCHEMA = "unpriced-v1"
_NO_CHARGE_SCHEMA = "response-cache-no-charge-v1"

ResponseCacheSettlementStatus = Literal[
    "hit", "hit_blocked", "stale", "unavailable"
]
ResponseCacheReadbackStatus = Literal["complete", "absent", "partial", "unreadable"]
ResponseCacheTerminalOutcome = Literal["hit", "hit_blocked", "settlement_failed"]


@dataclass(frozen=True, slots=True)
class PreparedResponseCacheSettlement:
    """Pricing evidence and exact durable IDs frozen before Budget Guard.

    命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 PreparedResponseCacheSettlement 对应步骤。"""

    candidate: ResponseCacheCandidate = field(repr=False)
    ids: ResponseCacheSettlementIds
    pricing_observed_at: str
    policy_snapshot_id: str | None = field(repr=False)
    policy_snapshot_version: int | None
    policy_snapshot_status: str
    reference_estimate: CostEstimate = field(repr=False)
    trusted_estimate: EstimatedRequestCost


@dataclass(frozen=True, slots=True)
class ResponseCacheSettlementResult:
    """A payload-bearing success or an explicit terminal ownership result.

    命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 ResponseCacheSettlementResult 对应步骤。"""

    status: ResponseCacheSettlementStatus
    payload: dict[str, object] | None = field(default=None, repr=False)
    error_code: str | None = None
    settlement_owned: bool = False
    ownership_unknown: bool = False
    ids: ResponseCacheSettlementIds | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 __post_init__ 对应步骤。"""
        if self.status == "hit":
            valid = (
                self.payload is not None
                and self.error_code is None
                and self.settlement_owned
                and not self.ownership_unknown
                and self.ids is not None
            )
        elif self.status == "stale":
            valid = (
                self.payload is None
                and self.error_code is None
                and not self.settlement_owned
                and not self.ownership_unknown
            )
        else:
            valid = (
                self.payload is None
                and isinstance(self.error_code, str)
                and bool(self.error_code)
                and self.settlement_owned
                and self.ids is not None
            )
        if not valid or (self.ownership_unknown and self.status != "unavailable"):
            # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
            raise ValueError("response_cache_settlement_result_invalid")


@dataclass(frozen=True, slots=True)
class ResponseCacheSettlementReadback:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 ResponseCacheSettlementReadback 对应步骤。"""
    status: ResponseCacheReadbackStatus
    outcome: ResponseCacheTerminalOutcome | None = None

    def __post_init__(self) -> None:
        """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 __post_init__ 对应步骤。"""
        if (self.status == "complete") != (self.outcome is not None):
            # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
            raise ValueError("response_cache_settlement_readback_invalid")


@dataclass(frozen=True, slots=True)
class ResponseCacheSettlementRecovery:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 ResponseCacheSettlementRecovery 对应步骤。"""
    status: Literal["recovered", "already_complete", "partial", "unreadable"]


def freeze_response_cache_reference(
    config: AppConfig,
    *,
    context: RequestLogContext,
    route: GatewayRoute,
    candidate: ResponseCacheCandidate,
    policy_snapshot_id: str | None,
    policy_snapshot_version: int | None,
    policy_snapshot_status: str,
) -> PreparedResponseCacheSettlement | None:
    """Freeze a reference PriceSnapshot before cache-specific Budget Guard.

        ``None`` is a durable stale decision: the request fact has already moved
        out of ``hit_candidate`` so the caller can safely continue to Provider.


    命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 freeze_response_cache_reference 对应步骤。"""

    _validate_freeze_arguments(
        context,
        route,
        candidate,
        policy_snapshot_id,
        policy_snapshot_version,
        policy_snapshot_status,
    )
    ids = _new_settlement_ids(charge_cache_hits=candidate.policy.charge_cache_hits)
    routed_context = _context_for_cache_route(context, route)

    # The PriceSnapshot owner trigger requires a RequestLog.  This conservative
    # row is deliberately not a success and may later be overwritten only by a
    # terminal cache transaction or the normal Provider path.
    try:
        record_request_log(
            config,
            routed_context,
            status="failed",
            error_code=None,
            latency_ms=None,
        )
    except Exception:
        _close_freeze_failure(config, routed_context, candidate, ids)
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ApiError(_SETTLEMENT_ERROR) from None

    connection: sqlite3.Connection | None = None
    prepared: PreparedResponseCacheSettlement | None = None
    stale = False
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        if not _candidate_is_current(
            config,
            connection,
            context=routed_context,
            route=route,
            candidate=candidate,
            policy_snapshot_id=policy_snapshot_id,
            policy_snapshot_version=policy_snapshot_version,
            policy_snapshot_status=policy_snapshot_status,
            now=now,
        ):
            _transition_candidate_fact(
                connection,
                tenant_id=context.tenant_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                status="unavailable_bypass",
                failure_code=None,
                now=now,
            )
            stale = True
        else:
            estimate, reference_available = _freeze_reference_estimate(
                connection,
                routed_context,
                route,
                candidate,
                snapshot_id=ids.reference_price_snapshot_id,
                observed_at=now,
            )
            trusted = EstimatedRequestCost(
                input_tokens=candidate.owner.input_tokens,
                output_tokens=candidate.owner.output_tokens,
                cost_cny=(
                    estimate.cost_cny
                    if candidate.policy.charge_cache_hits and reference_available
                    else Decimal("0")
                ),
                price_available=(
                    reference_available if candidate.policy.charge_cache_hits else True
                ),
            )
            prepared = PreparedResponseCacheSettlement(
                candidate=candidate,
                ids=ids,
                pricing_observed_at=now,
                policy_snapshot_id=policy_snapshot_id,
                policy_snapshot_version=policy_snapshot_version,
                policy_snapshot_status=policy_snapshot_status,
                reference_estimate=estimate,
                trusted_estimate=trusted,
            )
        connection.commit()
        return None if stale else prepared
    except Exception:
        _rollback_safely(connection)
        # A commit exception can still mean the reference or stale transition
        # was published.  Resolve those two intended outcomes before failing.
        if stale and _fact_has_status(
            config, context.tenant_id, context.request_id, context.trace_id, "unavailable_bypass"
        ):
            return None
        if prepared is not None and _prepared_reference_is_committed(
            config, routed_context, prepared
        ):
            return prepared
        _close_freeze_failure(config, routed_context, candidate, ids)
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ApiError(_SETTLEMENT_ERROR) from None
    finally:
        _close_safely(connection)


def settle_response_cache_candidate(
    config: AppConfig,
    *,
    context: RequestLogContext,
    route: GatewayRoute,
    prepared: PreparedResponseCacheSettlement,
    output_safety_scan: PreparedOutputSafetyScan,
    output_safety_terminal: OutputSafetyTerminalState | None,
    latency_ms: int,
) -> ResponseCacheSettlementResult:
    """Atomically publish an allowed/blocked candidate or return ``stale``.

    命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 settle_response_cache_candidate 对应步骤。"""

    _validate_settlement_arguments(
        context,
        route,
        prepared,
        output_safety_scan,
        output_safety_terminal,
        latency_ms,
    )
    terminal = output_safety_terminal
    if terminal is not None and terminal.decision == "blocked":
        intended: ResponseCacheTerminalOutcome = "hit_blocked"
        intended_error = _CONTENT_POLICY_ERROR
    elif terminal is not None and terminal.scan_status == "unavailable":
        intended = "settlement_failed"
        intended_error = _OUTPUT_SAFETY_ERROR
    else:
        intended = "hit"
        intended_error = None

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        if not _candidate_is_current(
            config,
            connection,
            context=context,
            route=route,
            candidate=prepared.candidate,
            policy_snapshot_id=prepared.policy_snapshot_id,
            policy_snapshot_version=prepared.policy_snapshot_version,
            policy_snapshot_status=prepared.policy_snapshot_status,
            now=now,
        ):
            connection.rollback()
            return ResponseCacheSettlementResult(status="stale")

        _finalize_scan_if_pending(
            connection, output_safety_scan, output_safety_terminal
        )
        if intended == "hit":
            _write_hit_settlement(
                connection,
                context=context,
                route=route,
                prepared=prepared,
                latency_ms=latency_ms,
                now=now,
            )
        elif intended == "hit_blocked":
            _write_blocked_settlement(
                config,
                connection,
                context=context,
                prepared=prepared,
                latency_ms=latency_ms,
                now=now,
            )
        else:
            _write_failed_settlement(
                connection,
                context=context,
                prepared=prepared,
                latency_ms=latency_ms,
                error_code=intended_error or _SETTLEMENT_ERROR,
                now=now,
            )
        connection.commit()
        return _result_for_outcome(prepared, intended, intended_error)
    except Exception:
        _rollback_safely(connection)
        return _resolve_settlement_write_failure(
            config,
            context=context,
            prepared=prepared,
            intended=intended,
            intended_error=intended_error,
        )
    finally:
        _close_safely(connection)


def fail_response_cache_candidate(
    config: AppConfig,
    *,
    context: RequestLogContext,
    prepared: PreparedResponseCacheSettlement,
    output_safety_scan: PreparedOutputSafetyScan | None,
    output_safety_terminal: OutputSafetyTerminalState | None,
    latency_ms: int,
    error_code: str = _SETTLEMENT_ERROR,
) -> ResponseCacheSettlementResult:
    """Close a cache candidate that failed before Provider invocation.

        Both Output Safety arguments may be ``None`` when Budget Guard failed before
        scan preparation.  A pending scan must still carry its trusted terminal.


    命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 fail_response_cache_candidate 对应步骤。"""

    _validate_fail_arguments(
        context,
        prepared,
        output_safety_scan,
        output_safety_terminal,
        latency_ms,
        error_code,
    )
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        if output_safety_scan is not None:
            _finalize_scan_if_pending(
                connection, output_safety_scan, output_safety_terminal
            )
        _write_failed_settlement(
            connection,
            context=context,
            prepared=prepared,
            latency_ms=latency_ms,
            error_code=error_code,
            now=now,
        )
        connection.commit()
        return _result_for_outcome(prepared, "settlement_failed", error_code)
    except Exception:
        _rollback_safely(connection)
        return _resolve_settlement_write_failure(
            config,
            context=context,
            prepared=prepared,
            intended="settlement_failed",
            intended_error=error_code,
        )
    finally:
        _close_safely(connection)


def read_response_cache_settlement(
    config: AppConfig,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    cache_policy_id: str,
    cache_policy_version: int,
    ids: ResponseCacheSettlementIds,
) -> ResponseCacheSettlementReadback:
    """Classify an exact settlement graph using a fresh connection.

    命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 read_response_cache_settlement 对应步骤。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        return _readback_in_connection(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            cache_policy_id=cache_policy_id,
            cache_policy_version=cache_policy_version,
            ids=ids,
        )
    # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
    except (DbError, sqlite3.Error, ValueError, TypeError):
        return ResponseCacheSettlementReadback(status="unreadable")
    finally:
        _close_safely(connection)


def recover_absent_response_cache_settlement(
    config: AppConfig,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    cache_policy_id: str,
    cache_policy_version: int,
    ids: ResponseCacheSettlementIds,
    error_code: str,
) -> ResponseCacheSettlementRecovery:
    """Exactly once, turn an absent outcome-unknown record into failed state.

    命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 recover_absent_response_cache_settlement 对应步骤。"""

    # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
    if error_code != _SETTLEMENT_ERROR:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ValueError("response_cache_settlement_recovery_error_invalid")
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        state = _readback_in_connection(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            cache_policy_id=cache_policy_id,
            cache_policy_version=cache_policy_version,
            ids=ids,
        )
        if state.status == "complete":
            connection.rollback()
            return ResponseCacheSettlementRecovery(status="already_complete")
        if state.status == "partial":
            connection.rollback()
            return ResponseCacheSettlementRecovery(status="partial")
        if state.status == "unreadable":
            connection.rollback()
            return ResponseCacheSettlementRecovery(status="unreadable")

        now = _database_now(connection)
        claim = connection.execute(
            """
            SELECT trace_id,virtual_key_id,snapshot_version,policy_snapshot_status
            FROM gateway_request_claims
            WHERE tenant_id=? AND request_id=?
            """,
            (tenant_id, request_id),
        ).fetchone()
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        if claim is None or str(claim["trace_id"]) != trace_id:
            # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
            raise sqlite3.IntegrityError("response_cache_recovery_claim_missing")
        recovery_context = RequestLogContext(
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=claim["virtual_key_id"],
            snapshot_version=claim["snapshot_version"],
            policy_snapshot_status=str(claim["policy_snapshot_status"]),
        )
        _ensure_replay_request_log(connection, recovery_context, now)
        _set_request_log_terminal(
            connection,
            recovery_context,
            status="failed",
            error_code=error_code,
            latency_ms=None,
            now=now,
        )
        _transition_candidate_fact(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            status="settlement_failed",
            failure_code=error_code,
            now=now,
        )
        _settle_budget_rows(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            require_existing=False,
        )
        connection.commit()
        return ResponseCacheSettlementRecovery(status="recovered")
    # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
    except (DbError, sqlite3.Error, DecimalException, ValueError, TypeError):
        _rollback_safely(connection)
        return ResponseCacheSettlementRecovery(status="unreadable")
    finally:
        _close_safely(connection)


def _new_settlement_ids(*, charge_cache_hits: bool) -> ResponseCacheSettlementIds:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _new_settlement_ids 对应步骤。"""
    reference = f"prs_{uuid4().hex}"
    return ResponseCacheSettlementIds(
        cache_hit_id=f"rch_{uuid4().hex}",
        usage_ledger_id=f"usg_{uuid4().hex}",
        finops_ledger_entry_id=f"fle_{uuid4().hex}",
        finops_event_id=f"flevt_{uuid4().hex}",
        reference_price_snapshot_id=reference,
        billed_price_snapshot_id=(reference if charge_cache_hits else f"prs_{uuid4().hex}"),
        response_cache_event_id=f"rcev_{uuid4().hex}",
    )


def _context_for_cache_route(
    context: RequestLogContext, route: GatewayRoute
) -> RequestLogContext:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _context_for_cache_route 对应步骤。"""
    return replace(
        context,
        provider_id=route.provider_id,
        model_id=route.model_id,
        request_model=route.requested_model or context.request_model,
        resolved_model=route.model_alias,
        provider_code=route.provider_code,
        routing_attempt_id=None,
        final_routing_attempt_id=None,
        routing_attempt_count=0,
        fallback_triggered=False,
    )


def _freeze_reference_estimate(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    route: GatewayRoute,
    candidate: ResponseCacheCandidate,
    *,
    snapshot_id: str,
    observed_at: str,
) -> tuple[CostEstimate, bool]:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _freeze_reference_estimate 对应步骤。"""
    row = connection.execute(
        """
        SELECT
          CASE WHEN plan.price_plan_id IS NOT NULL
            THEN plan.input_price_per_1k ELSE model.input_price_per_1k END AS input_price,
          CASE WHEN plan.price_plan_id IS NOT NULL
            THEN plan.output_price_per_1k ELSE model.output_price_per_1k END AS output_price,
          CASE WHEN plan.price_plan_id IS NOT NULL
            THEN plan.currency ELSE model.currency END AS currency,
          CASE WHEN plan.price_plan_id IS NOT NULL
            THEN plan.price_schema_version ELSE model.price_schema_version END AS schema_version,
          plan.price_plan_id,plan.effective_from,provider.provider_code
        FROM models model
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
        LEFT JOIN model_price_plans plan ON plan.price_plan_id=(
          SELECT selected.price_plan_id
          FROM model_price_plans selected
          WHERE selected.tenant_id=model.tenant_id
            AND selected.model_id=model.model_id
            AND selected.effective_from<=?
          ORDER BY selected.effective_from DESC,selected.created_at DESC
          LIMIT 1
        )
        WHERE model.tenant_id=? AND model.model_id=? AND model.provider_id=?
        """,
        (observed_at, context.tenant_id, route.model_id, route.provider_id),
    ).fetchone()
    available = False
    currency = "CNY"
    input_price = Decimal("0")
    output_price = Decimal("0")
    schema = _UNPRICED_SCHEMA
    plan_id = None
    effective_from = observed_at
    provider_code = route.provider_code
    if row is not None:
        parsed_currency = _normalize_currency(row["currency"])
        parsed_input = _decimal_value(row["input_price"])
        parsed_output = _decimal_value(row["output_price"])
        parsed_schema = str(row["schema_version"] or "").strip()
        if (
            parsed_currency is not None
            and parsed_input is not None
            and parsed_output is not None
            and parsed_schema
        ):
            currency = parsed_currency
            input_price = parsed_input
            output_price = parsed_output
            schema = parsed_schema
            plan_id = row["price_plan_id"]
            effective_from = row["effective_from"] or observed_at
            provider_code = str(row["provider_code"] or route.provider_code)
            available = currency == "CNY"
    try:
        amount = _money_amount(
            (
                Decimal(candidate.owner.input_tokens) * input_price
                + Decimal(candidate.owner.output_tokens) * output_price
            )
            / Decimal(1000)
        )
    # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
    except (DecimalException, OverflowError):
        amount = Decimal("0")
        currency = "CNY"
        input_price = Decimal("0")
        output_price = Decimal("0")
        schema = _UNPRICED_SCHEMA
        plan_id = None
        effective_from = observed_at
        available = False
    evidence = PriceSnapshotEvidence(
        price_snapshot_id=snapshot_id,
        model_id=route.model_id,
        provider_id=route.provider_id,
        provider_code=provider_code,
        currency=currency,
        input_price_per_1k=format(input_price, "f"),
        output_price_per_1k=format(output_price, "f"),
        price_schema_version=schema,
        source="response_cache_reference",
        price_plan_id=plan_id,
        effective_from=str(effective_from),
    )
    cost_cny, cost_usd = _cost_amounts(amount, currency)
    estimate = CostEstimate(
        amount=amount,
        currency=currency,
        price_schema_version=schema,
        cost_cny=cost_cny,
        cost_usd=cost_usd,
        price_snapshot_id=snapshot_id,
        price_snapshot_evidence=evidence,
    )
    _insert_price_snapshot_evidence(
        connection,
        context,
        evidence,
        now=observed_at,
        usage_ledger_id=None,
    )
    return estimate, available and schema != _UNPRICED_SCHEMA


def _candidate_is_current(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    context: RequestLogContext,
    route: GatewayRoute,
    candidate: ResponseCacheCandidate,
    policy_snapshot_id: str | None,
    policy_snapshot_version: int | None,
    policy_snapshot_status: str,
    now: str,
) -> bool:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _candidate_is_current 对应步骤。"""
    hmac_key = config.response_cache_hmac_key
    encryption_key = config.response_cache_encryption_key
    if hmac_key is None or encryption_key is None:
        return False
    owner = candidate.owner
    if (
        context.tenant_id != route.tenant_id
        or owner.tenant_id != context.tenant_id
        or route.model_alias_id != owner.model_alias_id
        or route.model_id != owner.model_id
        or route.provider_id != owner.provider_id
        or candidate.policy.tenant_id != context.tenant_id
        or candidate.policy.model_alias_id != owner.model_alias_id
        or candidate.policy.cache_policy_id != owner.cache_policy_id
        or candidate.policy.version != owner.cache_policy_version
        or candidate.policy.invalidation_generation != owner.cache_policy_generation
        or candidate.identity.cache_key_digest != owner.cache_key_digest
        or candidate.identity.cache_identity_digest != owner.cache_identity_digest
        or candidate.identity.key_generation != owner.key_generation
        or candidate.identity.hmac_key_version != owner.hmac_key_version
    ):
        return False
    fact = connection.execute(
        """
        SELECT trace_id,cache_policy_id,cache_policy_version,charge_cache_hits,status
        FROM response_cache_request_facts
        WHERE tenant_id=? AND request_id=?
        """,
        (context.tenant_id, context.request_id),
    ).fetchone()
    if fact is None or tuple(fact) != (
        context.trace_id,
        candidate.policy.cache_policy_id,
        candidate.policy.version,
        1 if candidate.policy.charge_cache_hits else 0,
        "hit_candidate",
    ):
        return False
    policy = _load_enabled_policy(connection, context.tenant_id, owner.model_alias_id)
    if policy != candidate.policy:
        return False
    alias = connection.execute(
        """
        SELECT status FROM model_aliases
        WHERE tenant_id=? AND model_alias_id=?
        """,
        (context.tenant_id, owner.model_alias_id),
    ).fetchone()
    if alias is None or str(alias["status"]) != "active":
        return False
    state = connection.execute(
        "SELECT * FROM response_cache_key_state WHERE key_state_id=1"
    ).fetchone()
    if state is None or (
        int(state["key_schema_version"]) != CACHE_KEY_SCHEMA_VERSION
        or int(state["active_hmac_key_version"]) != hmac_key.version
        or int(state["active_encryption_key_version"]) != encryption_key.version
        or not hmac.compare_digest(
            str(state["hmac_secret_fingerprint"]),
            response_cache_secret_fingerprint(hmac_key, purpose="hmac"),
        )
        or not hmac.compare_digest(
            str(state["encryption_secret_fingerprint"]),
            response_cache_secret_fingerprint(encryption_key, purpose="encryption"),
        )
    ):
        return False
    credential = snapshot_response_cache_credential_scope(
        config,
        connection,
        route,
        hmac_key=hmac_key,
        as_of=_parse_db_time(now),
    )
    if credential is None or not hmac.compare_digest(
        credential.digest, candidate.credential_scope_digest
    ):
        return False
    current_route_digest = build_route_revision_digest(
        _route_revision_input(
            route,
            credential_scope=credential,
            policy_snapshot_id=policy_snapshot_id,
            policy_snapshot_version=policy_snapshot_version,
            policy_snapshot_status=policy_snapshot_status,
        ),
        key=hmac_key,
    )
    # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
    if not hmac.compare_digest(current_route_digest, owner.route_revision_digest):
        return False
    generation = connection.execute(
        """
        SELECT generation FROM response_cache_key_generations
        WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
        """,
        (context.tenant_id, owner.hmac_key_version, owner.cache_identity_digest),
    ).fetchone()
    if generation is None or int(generation["generation"]) != owner.key_generation:
        return False
    row = connection.execute(
        """
        SELECT * FROM response_cache_entries
        WHERE tenant_id=? AND response_cache_entry_id=?
        """,
        (context.tenant_id, owner.entry_id),
    ).fetchone()
    if row is None or int(
        connection.execute(
            "SELECT unixepoch(?)>unixepoch(?)", (row["expires_at"], now)
        ).fetchone()[0]
    ) != 1:
        return False
    if _entry_owner(row) != owner:
        return False
    current_encrypted = EncryptedResponseCachePayload(
        int(row["encryption_key_version"]), bytes(row["nonce"]), bytes(row["ciphertext"])
    )
    if current_encrypted != candidate.encrypted:
        return False
    tag_rows = connection.execute(
        """
        SELECT tag.hmac_key_version,tag.tag_kind,tag.tag_digest,tag.tag_generation,
               generation.generation AS current_generation
        FROM response_cache_entry_tags tag
        LEFT JOIN response_cache_tag_generations generation
          ON generation.tenant_id=tag.tenant_id
         AND generation.hmac_key_version=tag.hmac_key_version
         AND generation.tag_kind=tag.tag_kind
         AND generation.tag_digest=tag.tag_digest
        WHERE tag.tenant_id=? AND tag.response_cache_entry_id=?
        ORDER BY tag.tag_kind,tag.tag_digest
        """,
        (context.tenant_id, owner.entry_id),
    ).fetchall()
    actual_tags = tuple(
        (
            int(row["hmac_key_version"]),
            str(row["tag_kind"]),
            str(row["tag_digest"]),
            int(row["tag_generation"]),
            int(row["current_generation"])
            if row["current_generation"] is not None
            else None,
        )
        for row in tag_rows
    )
    expected_tags = tuple(
        sorted(
            (
                item.tag.hmac_key_version,
                item.tag.kind,
                item.tag.digest,
                item.generation,
                item.generation,
            )
            for item in candidate.tags
        )
    )
    return actual_tags == expected_tags


def _write_hit_settlement(
    connection: sqlite3.Connection,
    *,
    context: RequestLogContext,
    route: GatewayRoute,
    prepared: PreparedResponseCacheSettlement,
    latency_ms: int,
    now: str,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _write_hit_settlement 对应步骤。"""
    candidate = prepared.candidate
    estimate = prepared.reference_estimate
    billed = estimate
    if not candidate.policy.charge_cache_hits:
        evidence = PriceSnapshotEvidence(
            price_snapshot_id=prepared.ids.billed_price_snapshot_id,
            model_id=route.model_id,
            provider_id=route.provider_id,
            provider_code=route.provider_code,
            currency="CNY",
            input_price_per_1k="0",
            output_price_per_1k="0",
            price_schema_version=_NO_CHARGE_SCHEMA,
            source="response_cache_no_charge",
            price_plan_id=None,
            effective_from=prepared.pricing_observed_at,
        )
        billed = CostEstimate(
            amount=Decimal("0"),
            currency="CNY",
            price_schema_version=_NO_CHARGE_SCHEMA,
            cost_cny=Decimal("0"),
            cost_usd=Decimal("0"),
            price_snapshot_id=prepared.ids.billed_price_snapshot_id,
            price_snapshot_evidence=evidence,
        )
        _insert_price_snapshot_evidence(
            connection,
            context,
            evidence,
            now=prepared.pricing_observed_at,
            usage_ledger_id=None,
        )
    _set_request_log_terminal(
        connection,
        _context_for_cache_route(context, route),
        status="success",
        error_code=None,
        latency_ms=latency_ms,
        now=now,
    )
    _insert_response_cache_usage(connection, context, route, prepared, billed)
    charge = connection.execute(
        """
        SELECT * FROM finops_ledger_entries
        WHERE finops_ledger_entry_id=? AND event_id=?
        """,
        (prepared.ids.finops_ledger_entry_id, prepared.ids.finops_event_id),
    ).fetchone()
    if charge is None or (
        str(charge["tenant_id"]) != context.tenant_id
        or str(charge["usage_ledger_id"]) != prepared.ids.usage_ledger_id
        or str(charge["source_request_id"]) != context.request_id
        or str(charge["source_trace_id"]) != context.trace_id
        or str(charge["price_snapshot_id"]) != prepared.ids.billed_price_snapshot_id
    ):
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise sqlite3.IntegrityError("response_cache_finops_projection_missing")
    settle_quota_usage(
        connection,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        usage_ledger_id=prepared.ids.usage_ledger_id,
        finops_ledger_entry_id=prepared.ids.finops_ledger_entry_id,
        actual_micros=_canonical_charge_micros(charge),
        # Cache hit 没有第二次 Provider 调用，不能继承动态 fallback 的未决暴露。
        exposure_outstanding_micros=0,
    )
    _settle_budget_rows(
        connection,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        require_existing=context.budget_reservation_outstanding_cny is not None,
    )
    amounts = _hit_amounts(candidate, prepared, billed)
    source_price = connection.execute(
        """
        SELECT price_snapshot_id FROM finops_ledger_entries
        WHERE tenant_id=? AND finops_ledger_entry_id=?
          AND usage_ledger_id=? AND source_request_id=? AND source_trace_id=?
        """,
        (
            context.tenant_id,
            candidate.owner.source_finops_ledger_entry_id,
            candidate.owner.source_usage_ledger_id,
            candidate.owner.source_request_id,
            candidate.owner.source_trace_id,
        ),
    ).fetchone()
    if source_price is None or source_price["price_snapshot_id"] is None:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise sqlite3.IntegrityError("response_cache_source_price_missing")
    connection.execute(
        """
        INSERT INTO response_cache_hits(
          response_cache_hit_id,tenant_id,cache_policy_id,cache_policy_version,
          request_id,trace_id,virtual_key_id,model_id,provider_id,
          source_response_cache_entry_id,source_request_id,source_trace_id,
          source_usage_ledger_id,source_finops_ledger_entry_id,charge_cache_hits,
          source_generation_cost_status,source_generation_cost_cny_micros,
          current_reference_cost_status,current_reference_cost_cny_micros,
          saved_cost_status,saved_cost_cny_micros,billed_cost_status,
          billed_cost_cny_micros,source_price_snapshot_id,
          current_reference_price_snapshot_id,billed_price_snapshot_id,
          usage_ledger_id,finops_ledger_entry_id,pricing_observed_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            prepared.ids.cache_hit_id,
            context.tenant_id,
            candidate.policy.cache_policy_id,
            candidate.policy.version,
            context.request_id,
            context.trace_id,
            context.virtual_key_id,
            route.model_id,
            route.provider_id,
            candidate.owner.entry_id,
            candidate.owner.source_request_id,
            candidate.owner.source_trace_id,
            candidate.owner.source_usage_ledger_id,
            candidate.owner.source_finops_ledger_entry_id,
            1 if candidate.policy.charge_cache_hits else 0,
            amounts.source_generation.status,
            amounts.source_generation.cny_micros,
            amounts.current_reference.status,
            amounts.current_reference.cny_micros,
            amounts.saved.status,
            amounts.saved.cny_micros,
            amounts.billed.status,
            amounts.billed.cny_micros,
            str(source_price["price_snapshot_id"]),
            prepared.ids.reference_price_snapshot_id,
            prepared.ids.billed_price_snapshot_id,
            prepared.ids.usage_ledger_id,
            prepared.ids.finops_ledger_entry_id,
            prepared.pricing_observed_at,
            now,
        ),
    )
    _transition_hit_fact(connection, context, prepared, amounts, now)
    payload = CacheHitWebhookPayload(
        cache_hit_id=prepared.ids.cache_hit_id,
        cache_policy_id=candidate.policy.cache_policy_id,
        cache_policy_version=candidate.policy.version,
        charge_cache_hits=candidate.policy.charge_cache_hits,
        amounts=amounts,
    ).to_payload()
    connection.execute(
        """
        INSERT INTO response_cache_events(
          response_cache_event_id,tenant_id,event_type,object_id,actor_user_id,
          trace_id,payload_json,occurred_at
        ) VALUES(?,?,'cache.hit',?,NULL,?,?,?)
        """,
        (
            prepared.ids.response_cache_event_id,
            context.tenant_id,
            prepared.ids.cache_hit_id,
            context.trace_id,
            _json(payload),
            now,
        ),
    )


def _insert_response_cache_usage(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    route: GatewayRoute,
    prepared: PreparedResponseCacheSettlement,
    estimate: CostEstimate,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _insert_response_cache_usage 对应步骤。"""
    owner = prepared.candidate.owner
    normalized = {
        "prompt_tokens": owner.input_tokens,
        "completion_tokens": owner.output_tokens,
        "total_tokens": owner.total_tokens,
        "cache_tokens": 0,
        "estimated": False,
        "usage_source": "response_cache",
        "cost_amount": format(estimate.amount, "f"),
        "cost_cny": format(estimate.cost_cny, "f") if estimate.currency == "CNY" else None,
        "cost_usd": format(estimate.cost_usd, "f") if estimate.currency == "USD" else None,
        "currency": estimate.currency,
        "price_snapshot_id": estimate.price_snapshot_id,
        "price_schema_version": estimate.price_schema_version,
        "posting_source": "direct",
        "finops_ledger_entry_id": prepared.ids.finops_ledger_entry_id,
        "finops_event_id": prepared.ids.finops_event_id,
    }
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,total_tokens,
          cache_tokens,raw_usage,normalized_usage,cost_amount,cost_cny,cost_usd,
          currency,price_schema_version,estimated,usage_source,price_snapshot_id,
          model_id,provider_id,virtual_key_id,environment_id,environment_code,
          project_id,application_id,endpoint,request_model,resolved_model,provider_code,
          routing_attempt_id,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            prepared.ids.usage_ledger_id,
            context.tenant_id,
            context.request_id,
            owner.input_tokens,
            owner.output_tokens,
            owner.total_tokens,
            0,
            "{}",
            _json(normalized),
            format(estimate.amount, "f"),
            format(estimate.cost_cny, "f"),
            format(estimate.cost_usd, "f"),
            estimate.currency,
            estimate.price_schema_version,
            0,
            "response_cache",
            prepared.ids.billed_price_snapshot_id,
            route.model_id,
            route.provider_id,
            context.virtual_key_id,
            context.environment_id,
            context.environment_code,
            context.project_id,
            context.application_id,
            context.endpoint,
            context.request_model,
            route.model_alias,
            route.provider_code,
            None,
            prepared.pricing_observed_at,
            prepared.pricing_observed_at,
        ),
    )
    cursor = connection.execute(
        """
        UPDATE request_logs
        SET input_tokens=?,output_tokens=?,total_tokens=?,estimated_cost_amount=?,
            currency=?,price_schema_version=?,updated_at=?
        WHERE tenant_id=? AND request_id=? AND trace_id=? AND status='success'
        """,
        (
            owner.input_tokens,
            owner.output_tokens,
            owner.total_tokens,
            format(estimate.amount, "f"),
            estimate.currency,
            estimate.price_schema_version,
            prepared.pricing_observed_at,
            context.tenant_id,
            context.request_id,
            context.trace_id,
        ),
    )
    if cursor.rowcount != 1:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise sqlite3.IntegrityError("response_cache_request_usage_projection_missing")


def _hit_amounts(
    candidate: ResponseCacheCandidate,
    prepared: PreparedResponseCacheSettlement,
    billed: CostEstimate,
) -> ResponseCacheHitAmounts:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _hit_amounts 对应步骤。"""
    source = ResponseCacheAmount(
        candidate.owner.source_generation_cost_status,
        candidate.owner.source_generation_cost_cny_micros,
    )
    reference_available = (
        prepared.reference_estimate.currency == "CNY"
        and prepared.reference_estimate.price_schema_version != _UNPRICED_SCHEMA
    )
    reference = ResponseCacheAmount(
        "known" if reference_available else "unavailable",
        _to_micros(prepared.reference_estimate.cost_cny) if reference_available else None,
    )
    billed_available = (
        not candidate.policy.charge_cache_hits
        or (
            billed.currency == "CNY"
            and billed.price_schema_version != _UNPRICED_SCHEMA
        )
    )
    billed_amount = ResponseCacheAmount(
        "known" if billed_available else "unavailable",
        _to_micros(billed.cost_cny) if billed_available else None,
    )
    return ResponseCacheHitAmounts(
        source_generation=source,
        current_reference=reference,
        saved=calculate_saved_amount(reference, billed_amount),
        billed=billed_amount,
    )


def _transition_hit_fact(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    prepared: PreparedResponseCacheSettlement,
    amounts: ResponseCacheHitAmounts,
    now: str,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _transition_hit_fact 对应步骤。"""
    cursor = connection.execute(
        """
        UPDATE response_cache_request_facts
        SET status='hit',response_cache_hit_id=?,hit=1,
            source_generation_cost_status=?,source_generation_cost_cny_micros=?,
            current_reference_cost_status=?,current_reference_cost_cny_micros=?,
            saved_cost_status=?,saved_cost_cny_micros=?,
            billed_cost_status=?,billed_cost_cny_micros=?,failure_code=NULL,updated_at=?
        WHERE tenant_id=? AND request_id=? AND trace_id=? AND status='hit_candidate'
        """,
        (
            prepared.ids.cache_hit_id,
            amounts.source_generation.status,
            amounts.source_generation.cny_micros,
            amounts.current_reference.status,
            amounts.current_reference.cny_micros,
            amounts.saved.status,
            amounts.saved.cny_micros,
            amounts.billed.status,
            amounts.billed.cny_micros,
            now,
            context.tenant_id,
            context.request_id,
            context.trace_id,
        ),
    )
    if cursor.rowcount != 1:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise sqlite3.IntegrityError("response_cache_hit_fact_missing")


def _write_blocked_settlement(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    context: RequestLogContext,
    prepared: PreparedResponseCacheSettlement,
    latency_ms: int,
    now: str,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _write_blocked_settlement 对应步骤。"""
    _set_request_log_terminal(
        connection,
        context,
        status="failed",
        error_code=_CONTENT_POLICY_ERROR,
        latency_ms=latency_ms,
        now=now,
    )
    _settle_budget_rows(
        connection,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        require_existing=context.budget_reservation_outstanding_cny is not None,
    )
    release_request_quota_in_transaction(
        connection,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
    )
    _fence_output_blocked_entry(config, connection, context, prepared.candidate, now)
    _transition_candidate_fact(
        connection,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        status="hit_blocked",
        failure_code=_CONTENT_POLICY_ERROR,
        now=now,
    )


def _write_failed_settlement(
    connection: sqlite3.Connection,
    *,
    context: RequestLogContext,
    prepared: PreparedResponseCacheSettlement,
    latency_ms: int | None,
    error_code: str,
    now: str,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _write_failed_settlement 对应步骤。"""
    _set_request_log_terminal(
        connection,
        context,
        status="failed",
        error_code=error_code,
        latency_ms=latency_ms,
        now=now,
    )
    _settle_budget_rows(
        connection,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        require_existing=context.budget_reservation_outstanding_cny is not None,
    )
    release_request_quota_in_transaction(
        connection,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
    )
    _transition_candidate_fact(
        connection,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        status="settlement_failed",
        failure_code=error_code,
        now=now,
    )


def _set_request_log_terminal(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    *,
    status: Literal["success", "failed"],
    error_code: str | None,
    latency_ms: int | None,
    now: str,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _set_request_log_terminal 对应步骤。"""
    cursor = connection.execute(
        """
        UPDATE request_logs
        SET provider_id=?,model_id=?,request_model=?,resolved_model=?,provider_code=?,
            status=?,error_code=?,latency_ms=?,budget_status=?,budget_rule_id=?,
            budget_scope_type=?,budget_scope_id=?,budget_action=?,budget_reason_code=?,
            budget_current_cost_cny=?,budget_limit_cny=?,updated_at=?
        WHERE tenant_id=? AND request_id=? AND trace_id=?
        """,
        (
            context.provider_id,
            context.model_id,
            context.request_model,
            context.resolved_model,
            context.provider_code,
            status,
            error_code,
            latency_ms,
            context.budget_status,
            context.budget_rule_id,
            context.budget_scope_type,
            context.budget_scope_id,
            context.budget_action,
            context.budget_reason_code,
            context.budget_current_cost_cny,
            context.budget_limit_cny,
            now,
            context.tenant_id,
            context.request_id,
            context.trace_id,
        ),
    )
    if cursor.rowcount != 1:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise sqlite3.IntegrityError("response_cache_request_log_missing")


def _settle_budget_rows(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    require_existing: bool,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _settle_budget_rows 对应步骤。"""
    rows = connection.execute(
        """
        SELECT trace_id FROM budget_reservations
        WHERE tenant_id=? AND request_id=?
        """,
        (tenant_id, request_id),
    ).fetchall()
    # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
    if any(str(row["trace_id"]) != trace_id for row in rows):
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise sqlite3.IntegrityError("response_cache_budget_owner_mismatch")
    if require_existing and not rows:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise sqlite3.IntegrityError("response_cache_budget_reservation_missing")
    if rows:
        updated = set_request_budget_reservations_outstanding(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            outstanding_cost_cny="0.000000",
        )
        if updated != len(rows):
            # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
            raise sqlite3.IntegrityError("response_cache_budget_settlement_lost")


def _canonical_charge_micros(charge: sqlite3.Row) -> int | None:
    """只从 canonical charge 的 CNY 归一化列恢复额度结算金额。"""

    status = charge["cny_normalization_status"]
    value = charge["normalized_cny_micros"]
    if status == "unavailable" and value is None:
        return None
    # 只有权威归一化的有界整数金额可进入额度结算，其他状态必须关闭处理。
    if status != "normalized" or type(value) is not int or not 0 <= value <= 10**15:
        raise sqlite3.IntegrityError("response_cache_quota_amount_invalid")
    return value


def _fence_output_blocked_entry(
    config: AppConfig,
    connection: sqlite3.Connection,
    context: RequestLogContext,
    candidate: ResponseCacheCandidate,
    now: str,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _fence_output_blocked_entry 对应步骤。"""
    key = config.response_cache_hmac_key
    if key is None:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise sqlite3.IntegrityError("response_cache_hmac_key_missing")
    owner = candidate.owner
    matched = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM response_cache_entries
            WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
            """,
            (context.tenant_id, owner.hmac_key_version, owner.cache_identity_digest),
        ).fetchone()[0]
    )
    fenced = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM response_cache_fill_leases
            WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
            """,
            (context.tenant_id, owner.hmac_key_version, owner.cache_identity_digest),
        ).fetchone()[0]
    )
    cursor = connection.execute(
        """
        UPDATE response_cache_key_generations
        SET generation=generation+1,updated_at=?
        WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
          AND generation=?
        """,
        (
            now,
            context.tenant_id,
            owner.hmac_key_version,
            owner.cache_identity_digest,
            owner.key_generation,
        ),
    )
    if cursor.rowcount != 1:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise sqlite3.IntegrityError("response_cache_output_fence_lost")
    connection.execute(
        """DELETE FROM response_cache_entries
           WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?""",
        (context.tenant_id, owner.hmac_key_version, owner.cache_identity_digest),
    )
    connection.execute(
        """DELETE FROM response_cache_fill_leases
           WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?""",
        (context.tenant_id, owner.hmac_key_version, owner.cache_identity_digest),
    )
    invalidation_id = f"rci_{uuid4().hex}"
    event_id = f"rcev_{uuid4().hex}"
    selector = hmac.new(
        key.secret.encode("utf-8"),
        b"metahub/response-cache/system-selector/v1\x00"
        + owner.cache_identity_digest.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    fingerprint = hmac.new(
        key.secret.encode("utf-8"),
        b"metahub/response-cache/system-invalidation/v1\x00"
        + invalidation_id.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    invalidation = {
        "cache_invalidation_id": invalidation_id,
        "tenant_id": context.tenant_id,
        "scope": "tags",
        "match_mode": "all",
        "tag_count": 1,
        "matched_entry_count": matched,
        "deleted_entry_count": matched,
        "fenced_fill_count": fenced,
        "reason_code": "system_output_blocked",
        "created_at": now,
    }
    response = {
        "tenant_id": context.tenant_id,
        "invalidation": invalidation,
        "idempotent_replay": False,
        "request_id": context.request_id,
        "trace_id": context.trace_id,
    }
    connection.execute(
        """
        INSERT INTO response_cache_invalidations(
          cache_invalidation_id,tenant_id,actor_type,actor_user_id,
          idempotency_hmac_version,idempotency_key_hmac,request_fingerprint,
          scope,match_mode,selector_hmac,tag_count,matched_entry_count,
          deleted_entry_count,fenced_fill_count,reason_code,response_json,
          operation_request_id,operation_trace_id,created_at
        ) VALUES(?,?,'system',NULL,NULL,NULL,?,'tags','all',?,1,?,?,?,
                 'system_output_blocked',?,?,?,?)
        """,
        (
            invalidation_id,
            context.tenant_id,
            fingerprint,
            selector,
            matched,
            matched,
            fenced,
            _json(response),
            context.request_id,
            context.trace_id,
            now,
        ),
    )
    payload = CacheInvalidatedWebhookPayload(
        cache_invalidation_id=invalidation_id,
        scope="tags",
        match_mode="all",
        tag_count=1,
        matched_entry_count=matched,
        deleted_entry_count=matched,
        fenced_fill_count=fenced,
        reason_code="system_output_blocked",
    ).to_payload()
    connection.execute(
        """
        INSERT INTO response_cache_events(
          response_cache_event_id,tenant_id,event_type,object_id,actor_user_id,
          trace_id,payload_json,occurred_at
        ) VALUES(?,?,'cache.invalidated',?,NULL,?,?,?)
        """,
        (event_id, context.tenant_id, invalidation_id, context.trace_id, _json(payload), now),
    )


def _finalize_scan_if_pending(
    connection: sqlite3.Connection,
    scan: PreparedOutputSafetyScan,
    terminal: OutputSafetyTerminalState | None,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _finalize_scan_if_pending 对应步骤。"""
    if scan.scan_status == "not_configured":
        row = connection.execute(
            """
            SELECT scan_status,tenant_id,request_id,trace_id
            FROM output_safety_scans
            WHERE output_safety_scan_id=?
            """,
            (scan.output_safety_scan_id,),
        ).fetchone()
        if row is None or tuple(row) != (
            "not_configured",
            scan.tenant_id,
            scan.request_id,
            scan.trace_id,
        ):
            # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
            raise OutputSafetyStoreError("output_safety.terminal_conflict")
        return
    if terminal is None:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ValueError("response_cache_output_terminal_missing")
    finalize_output_safety_scan_in_transaction(connection, scan, terminal)


def _transition_candidate_fact(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    status: Literal["unavailable_bypass", "hit_blocked", "settlement_failed"],
    failure_code: str | None,
    now: str,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _transition_candidate_fact 对应步骤。"""
    cursor = connection.execute(
        """
        UPDATE response_cache_request_facts
        SET status=?,failure_code=?,updated_at=?
        WHERE tenant_id=? AND request_id=? AND trace_id=? AND status='hit_candidate'
        """,
        (status, failure_code, now, tenant_id, request_id, trace_id),
    )
    if cursor.rowcount != 1:
        existing = connection.execute(
            """
            SELECT status,failure_code FROM response_cache_request_facts
            WHERE tenant_id=? AND request_id=? AND trace_id=?
            """,
            (tenant_id, request_id, trace_id),
        ).fetchone()
        if existing is None or tuple(existing) != (status, failure_code):
            # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
            raise sqlite3.IntegrityError("response_cache_candidate_fact_conflict")


def _readback_in_connection(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    cache_policy_id: str,
    cache_policy_version: int,
    ids: ResponseCacheSettlementIds,
) -> ResponseCacheSettlementReadback:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _readback_in_connection 对应步骤。"""
    hit = connection.execute(
        "SELECT * FROM response_cache_hits WHERE response_cache_hit_id=?",
        (ids.cache_hit_id,),
    ).fetchone()
    usage = connection.execute(
        "SELECT * FROM usage_ledger WHERE usage_ledger_id=?",
        (ids.usage_ledger_id,),
    ).fetchone()
    charge = connection.execute(
        """SELECT * FROM finops_ledger_entries
           WHERE finops_ledger_entry_id=? OR event_id=?""",
        (ids.finops_ledger_entry_id, ids.finops_event_id),
    ).fetchone()
    event = connection.execute(
        "SELECT * FROM response_cache_events WHERE response_cache_event_id=?",
        (ids.response_cache_event_id,),
    ).fetchone()
    outbox = connection.execute(
        """SELECT * FROM response_cache_event_outbox
           WHERE tenant_id=? AND response_cache_event_id=?""",
        (tenant_id, ids.response_cache_event_id),
    ).fetchone()
    billed = None
    if ids.billed_price_snapshot_id != ids.reference_price_snapshot_id:
        billed = connection.execute(
            "SELECT * FROM price_snapshots WHERE price_snapshot_id=?",
            (ids.billed_price_snapshot_id,),
        ).fetchone()
    fact = connection.execute(
        """SELECT * FROM response_cache_request_facts
           WHERE tenant_id=? AND request_id=?""",
        (tenant_id, request_id),
    ).fetchone()
    request = connection.execute(
        "SELECT * FROM request_logs WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    ).fetchone()
    budget_ok = _budget_is_terminal(
        connection, tenant_id=tenant_id, request_id=request_id, trace_id=trace_id
    )
    hit_complete = (
        hit is not None
        and usage is not None
        and charge is not None
        and event is not None
        and outbox is not None
        and (ids.billed_price_snapshot_id == ids.reference_price_snapshot_id or billed is not None)
        and fact is not None
        and request is not None
        and str(hit["tenant_id"]) == tenant_id
        and str(hit["request_id"]) == request_id
        and str(hit["trace_id"]) == trace_id
        and str(hit["cache_policy_id"]) == cache_policy_id
        and int(hit["cache_policy_version"]) == cache_policy_version
        and str(hit["usage_ledger_id"]) == ids.usage_ledger_id
        and str(hit["finops_ledger_entry_id"]) == ids.finops_ledger_entry_id
        and str(hit["current_reference_price_snapshot_id"])
        == ids.reference_price_snapshot_id
        and str(hit["billed_price_snapshot_id"]) == ids.billed_price_snapshot_id
        and str(usage["tenant_id"]) == tenant_id
        and str(usage["request_id"]) == request_id
        and str(usage["usage_source"]) == "response_cache"
        and int(usage["cache_tokens"]) == 0
        and str(usage["price_snapshot_id"]) == ids.billed_price_snapshot_id
        and str(charge["tenant_id"]) == tenant_id
        and str(charge["usage_ledger_id"]) == ids.usage_ledger_id
        and str(charge["finops_ledger_entry_id"]) == ids.finops_ledger_entry_id
        and str(charge["event_id"]) == ids.finops_event_id
        and str(charge["source_request_id"]) == request_id
        and str(charge["source_trace_id"]) == trace_id
        and str(event["tenant_id"]) == tenant_id
        and str(event["event_type"]) == "cache.hit"
        and str(event["object_id"]) == ids.cache_hit_id
        and str(event["trace_id"]) == trace_id
        and str(outbox["tenant_id"]) == tenant_id
        and str(fact["trace_id"]) == trace_id
        and str(fact["status"]) == "hit"
        and str(fact["response_cache_hit_id"]) == ids.cache_hit_id
        and str(request["trace_id"]) == trace_id
        and str(request["status"]) == "success"
        and budget_ok
    )
    if hit_complete:
        return ResponseCacheSettlementReadback(status="complete", outcome="hit")

    ids_present = any(item is not None for item in (hit, usage, charge, event, outbox, billed))
    if fact is not None and str(fact["status"]) in {"hit_blocked", "settlement_failed"}:
        outcome = str(fact["status"])
        request_complete = (
            request is not None
            and str(request["trace_id"]) == trace_id
            and str(request["status"]) == "failed"
            and request["error_code"] is not None
            and str(fact["trace_id"]) == trace_id
            and str(fact["cache_policy_id"]) == cache_policy_id
            and int(fact["cache_policy_version"]) == cache_policy_version
            and budget_ok
            and not ids_present
        )
        if outcome == "hit_blocked" and request_complete:
            invalidation = connection.execute(
                """
                SELECT cache_invalidation_id FROM response_cache_invalidations
                WHERE tenant_id=? AND operation_request_id=? AND operation_trace_id=?
                  AND reason_code='system_output_blocked'
                """,
                (tenant_id, request_id, trace_id),
            ).fetchone()
            request_complete = invalidation is not None
        if request_complete:
            return ResponseCacheSettlementReadback(
                status="complete", outcome=outcome  # type: ignore[arg-type]
            )
        return ResponseCacheSettlementReadback(status="partial")
    if ids_present or (fact is not None and str(fact["status"]) != "hit_candidate"):
        return ResponseCacheSettlementReadback(status="partial")
    return ResponseCacheSettlementReadback(status="absent")


def _budget_is_terminal(
    connection: sqlite3.Connection, *, tenant_id: str, request_id: str, trace_id: str
) -> bool:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _budget_is_terminal 对应步骤。"""
    rows = connection.execute(
        """SELECT trace_id,outstanding_cost_cny FROM budget_reservations
           WHERE tenant_id=? AND request_id=?""",
        (tenant_id, request_id),
    ).fetchall()
    for row in rows:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        if str(row["trace_id"]) != trace_id or row["outstanding_cost_cny"] is None:
            return False
        try:
            # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
            if Decimal(str(row["outstanding_cost_cny"])) != 0:
                return False
        except DecimalException:
            return False
    return True


def _resolve_settlement_write_failure(
    config: AppConfig,
    *,
    context: RequestLogContext,
    prepared: PreparedResponseCacheSettlement,
    intended: ResponseCacheTerminalOutcome,
    intended_error: str | None,
) -> ResponseCacheSettlementResult:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _resolve_settlement_write_failure 对应步骤。"""
    readback = read_response_cache_settlement(
        config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        cache_policy_id=prepared.candidate.policy.cache_policy_id,
        cache_policy_version=prepared.candidate.policy.version,
        ids=prepared.ids,
    )
    if readback.status == "complete":
        assert readback.outcome is not None
        return _result_for_outcome(
            prepared,
            readback.outcome,
            intended_error if readback.outcome == intended else _SETTLEMENT_ERROR,
        )
    if readback.status == "absent":
        recovery = recover_absent_response_cache_settlement(
            config,
            tenant_id=context.tenant_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            cache_policy_id=prepared.candidate.policy.cache_policy_id,
            cache_policy_version=prepared.candidate.policy.version,
            ids=prepared.ids,
            error_code=_SETTLEMENT_ERROR,
        )
        if recovery.status in {"recovered", "already_complete"}:
            if recovery.status == "already_complete":
                retry = read_response_cache_settlement(
                    config,
                    tenant_id=context.tenant_id,
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                    cache_policy_id=prepared.candidate.policy.cache_policy_id,
                    cache_policy_version=prepared.candidate.policy.version,
                    ids=prepared.ids,
                )
                if retry.status == "complete" and retry.outcome is not None:
                    return _result_for_outcome(prepared, retry.outcome, intended_error)
            return _result_for_outcome(
                prepared, "settlement_failed", _SETTLEMENT_ERROR
            )
    _store_outcome_unknown(config, context, prepared)
    return ResponseCacheSettlementResult(
        status="unavailable",
        error_code=_SETTLEMENT_ERROR,
        settlement_owned=True,
        ownership_unknown=True,
        ids=prepared.ids,
    )


def _result_for_outcome(
    prepared: PreparedResponseCacheSettlement,
    outcome: ResponseCacheTerminalOutcome,
    error_code: str | None,
) -> ResponseCacheSettlementResult:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _result_for_outcome 对应步骤。"""
    if outcome == "hit":
        return ResponseCacheSettlementResult(
            status="hit",
            payload=prepared.candidate.payload,
            settlement_owned=True,
            ids=prepared.ids,
        )
    if outcome == "hit_blocked":
        return ResponseCacheSettlementResult(
            status="hit_blocked",
            error_code=_CONTENT_POLICY_ERROR,
            settlement_owned=True,
            ids=prepared.ids,
        )
    return ResponseCacheSettlementResult(
        status="unavailable",
        error_code=error_code or _SETTLEMENT_ERROR,
        settlement_owned=True,
        ids=prepared.ids,
    )


def _store_outcome_unknown(
    config: AppConfig,
    context: RequestLogContext,
    prepared: PreparedResponseCacheSettlement,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _store_outcome_unknown 对应步骤。"""
    try:
        from .failure_spool import (
            ResponseCacheFailureRecord,
            store_response_cache_failure_record,
        )

        store_response_cache_failure_record(
            config,
            ResponseCacheFailureRecord(
                tenant_id=context.tenant_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                cache_policy_id=prepared.candidate.policy.cache_policy_id,
                cache_policy_version=prepared.candidate.policy.version,
                ids=prepared.ids,
            ),
        )
    except Exception as exc:
        logger.critical(
            "response_cache.settlement_outcome_unknown_not_spooled",
            extra={
                "event": "response_cache.settlement_outcome_unknown_not_spooled",
                "tenant_id": context.tenant_id,
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "error_code": _SETTLEMENT_ERROR,
                "payload": {"exception_type": exc.__class__.__name__},
            },
        )


def _close_freeze_failure(
    config: AppConfig,
    context: RequestLogContext,
    candidate: ResponseCacheCandidate,
    ids: ResponseCacheSettlementIds,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _close_freeze_failure 对应步骤。"""
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        existing = connection.execute(
            "SELECT 1 FROM request_logs WHERE tenant_id=? AND request_id=? AND trace_id=?",
            (context.tenant_id, context.request_id, context.trace_id),
        ).fetchone()
        if existing is not None:
            _set_request_log_terminal(
                connection,
                context,
                status="failed",
                error_code=_SETTLEMENT_ERROR,
                latency_ms=None,
                now=now,
            )
        _transition_candidate_fact(
            connection,
            tenant_id=context.tenant_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            status="settlement_failed",
            failure_code=_SETTLEMENT_ERROR,
            now=now,
        )
        connection.commit()
    except Exception:
        _rollback_safely(connection)
        try:
            from .failure_spool import (
                ResponseCacheFailureRecord,
                store_response_cache_failure_record,
            )

            store_response_cache_failure_record(
                config,
                ResponseCacheFailureRecord(
                    tenant_id=context.tenant_id,
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                    cache_policy_id=candidate.policy.cache_policy_id,
                    cache_policy_version=candidate.policy.version,
                    ids=ids,
                ),
            )
        except Exception:
            pass
    finally:
        _close_safely(connection)


def _prepared_reference_is_committed(
    config: AppConfig,
    context: RequestLogContext,
    prepared: PreparedResponseCacheSettlement,
) -> bool:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _prepared_reference_is_committed 对应步骤。"""
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        evidence = prepared.reference_estimate.price_snapshot_evidence
        if evidence is None:
            return False
        row = connection.execute(
            """
            SELECT * FROM price_snapshots
            WHERE price_snapshot_id=? AND tenant_id=? AND request_id=?
            """,
            (
                prepared.ids.reference_price_snapshot_id,
                context.tenant_id,
                context.request_id,
            ),
        ).fetchone()
        fact = connection.execute(
            """SELECT status,trace_id FROM response_cache_request_facts
               WHERE tenant_id=? AND request_id=?""",
            (context.tenant_id, context.request_id),
        ).fetchone()
        return row is not None and fact is not None and (
            str(row["source"]) == "response_cache_reference"
            and str(row["created_at"]) == prepared.pricing_observed_at
            and str(row["currency"]) == evidence.currency
            and str(row["price_schema_version"]) == evidence.price_schema_version
            and tuple(fact) == ("hit_candidate", context.trace_id)
        )
    # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
    except (DbError, sqlite3.Error):
        return False
    finally:
        _close_safely(connection)


def _fact_has_status(
    config: AppConfig, tenant_id: str, request_id: str, trace_id: str, status: str
) -> bool:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _fact_has_status 对应步骤。"""
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        row = connection.execute(
            """SELECT 1 FROM response_cache_request_facts
               WHERE tenant_id=? AND request_id=? AND trace_id=? AND status=?""",
            (tenant_id, request_id, trace_id, status),
        ).fetchone()
        return row is not None
    # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
    except (DbError, sqlite3.Error):
        return False
    finally:
        _close_safely(connection)


def _validate_freeze_arguments(
    context: RequestLogContext,
    route: GatewayRoute,
    candidate: ResponseCacheCandidate,
    policy_snapshot_id: str | None,
    policy_snapshot_version: int | None,
    policy_snapshot_status: str,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _validate_freeze_arguments 对应步骤。"""
    if (
        not isinstance(context, RequestLogContext)
        or not isinstance(route, GatewayRoute)
        or not isinstance(candidate, ResponseCacheCandidate)
        or not isinstance(policy_snapshot_status, str)
        or not policy_snapshot_status
        or not _valid_policy_snapshot_owner(
            policy_snapshot_id,
            policy_snapshot_version,
            policy_snapshot_status,
        )
        or context.tenant_id != route.tenant_id
        or context.tenant_id != candidate.policy.tenant_id
    ):
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ValueError("response_cache_freeze_contract_invalid")


def _valid_policy_snapshot_owner(
    policy_snapshot_id: str | None,
    policy_snapshot_version: int | None,
    policy_snapshot_status: str,
) -> bool:
    """验证可执行策略租约的真实 nullable owner/status 矩阵。"""

    absent_statuses = {"disabled", "not_published", "lkg", "emergency_lkg"}
    present_statuses = {"current", "lkg", "emergency_lkg"}
    if policy_snapshot_id is None or policy_snapshot_version is None:
        return (
            policy_snapshot_id is None
            and policy_snapshot_version is None
            and policy_snapshot_status in absent_statuses
        )
    return (
        isinstance(policy_snapshot_id, str)
        and bool(policy_snapshot_id)
        and type(policy_snapshot_version) is int
        and policy_snapshot_version >= 1
        and policy_snapshot_status in present_statuses
    )


def _validate_settlement_arguments(
    context: RequestLogContext,
    route: GatewayRoute,
    prepared: PreparedResponseCacheSettlement,
    scan: PreparedOutputSafetyScan,
    terminal: OutputSafetyTerminalState | None,
    latency_ms: int,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _validate_settlement_arguments 对应步骤。"""
    if (
        not isinstance(prepared, PreparedResponseCacheSettlement)
        or not isinstance(scan, PreparedOutputSafetyScan)
        or type(latency_ms) is not int
        or latency_ms < 0
        or context.tenant_id != prepared.candidate.policy.tenant_id
        or route.tenant_id != context.tenant_id
        or scan.tenant_id != context.tenant_id
        or scan.request_id != context.request_id
        or scan.trace_id != context.trace_id
        or scan.scan_mode != "non_stream_full"
        or (scan.scan_status == "not_configured") != (terminal is None)
    ):
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ValueError("response_cache_settlement_contract_invalid")
    if terminal is not None and terminal.scan_status not in {"completed", "unavailable"}:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ValueError("response_cache_settlement_terminal_invalid")


def _validate_fail_arguments(
    context: RequestLogContext,
    prepared: PreparedResponseCacheSettlement,
    scan: PreparedOutputSafetyScan | None,
    terminal: OutputSafetyTerminalState | None,
    latency_ms: int,
    error_code: str,
) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _validate_fail_arguments 对应步骤。"""
    if (
        not isinstance(prepared, PreparedResponseCacheSettlement)
        or context.tenant_id != prepared.candidate.policy.tenant_id
        or type(latency_ms) is not int
        or latency_ms < 0
        or not isinstance(error_code, str)
        or not error_code
    ):
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ValueError("response_cache_failure_contract_invalid")
    if scan is None:
        if terminal is not None:
            # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
            raise ValueError("response_cache_failure_terminal_invalid")
        return
    if (
        scan.tenant_id != context.tenant_id
        or scan.request_id != context.request_id
        or scan.trace_id != context.trace_id
        or scan.scan_mode != "non_stream_full"
    ):
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ValueError("response_cache_failure_scan_owner_invalid")
    if scan.scan_status == "not_configured":
        if terminal is not None:
            # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
            raise ValueError("response_cache_failure_terminal_invalid")
        return
    if terminal is None or terminal.decision == "blocked":
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ValueError("response_cache_failure_terminal_invalid")


def _to_micros(value: Decimal) -> int:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _to_micros 对应步骤。"""
    micros = value * Decimal(1_000_000)
    integral = micros.to_integral_value()
    if micros != integral or integral < 0 or integral > 1_000_000_000_000_000:
        # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
        raise ValueError("response_cache_amount_invalid")
    return int(integral)


def _json(value: object) -> str:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _json 对应步骤。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _rollback_safely(connection: sqlite3.Connection | None) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _rollback_safely 对应步骤。"""
    if connection is None:
        return
    try:
        connection.rollback()
    # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
    except sqlite3.Error:
        pass


def _close_safely(connection: sqlite3.Connection | None) -> None:
    """命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。 当前定义负责 _close_safely 对应步骤。"""
    if connection is None:
        return
    try:
        connection.close()
    # 命中结算必须原子写入缓存事实、用量、成本和预算状态，禁止出现部分成功。
    except sqlite3.Error:
        pass


__all__ = [
    "PreparedResponseCacheSettlement",
    "ResponseCacheSettlementReadback",
    "ResponseCacheSettlementRecovery",
    "ResponseCacheSettlementResult",
    "ResponseCacheSettlementStatus",
    "fail_response_cache_candidate",
    "freeze_response_cache_reference",
    "read_response_cache_settlement",
    "recover_absent_response_cache_settlement",
    "settle_response_cache_candidate",
]
