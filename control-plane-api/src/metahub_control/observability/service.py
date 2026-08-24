"""Transactional platform observability, SLO, budget, and alert services."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.platform_jobs.service import (
    enqueue_prepared_platform_job_in_transaction,
    prepare_platform_job,
)

from .models import (
    AlertAcknowledgementInfo,
    AlertAcknowledgementRequest,
    AlertAcknowledgementResponse,
    AlertInfo,
    ErrorBudgetInfo,
    ErrorBudgetListResponse,
    ServiceCatalogEntryInfo,
    ServiceCatalogResponse,
    ServiceLevelObjectiveInfo,
    ServiceLevelObjectiveListResponse,
    ServiceLevelObjectiveRequest,
    ServiceLevelObjectiveResponse,
)

_ALERT_ID_RE = re.compile(r"^alt_[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_DURATION_ORDER = {
    "le_10ms": 10,
    "le_50ms": 50,
    "le_100ms": 100,
    "le_250ms": 250,
    "le_500ms": 500,
    "le_1000ms": 1000,
    "le_2500ms": 2500,
    "le_5000ms": 5000,
    "gt_5000ms": 5001,
}


def list_services(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> ServiceCatalogResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _authenticate(connection, config, session_token)
        projection = _read_projection(actor)
        rows = connection.execute(
            "SELECT * FROM service_catalog_entries "
            + ("WHERE security_visible=1 " if projection == "security" else "")
            + "ORDER BY service_tier,service_code"
        ).fetchall()
        items = tuple(_service_info(connection, row) for row in rows)
        connection.commit()
        return ServiceCatalogResponse(
            items=items,
            total=len(items),
            projection=projection,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("observability.unavailable") from None
    finally:
        connection.close()


def list_slos(
    config: AppConfig,
    *,
    session_token: str,
    service_code: str | None,
    signal_class: str | None,
    current_only: bool,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> ServiceLevelObjectiveListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _authenticate(connection, config, session_token)
        projection = _read_projection(actor)
        clauses: list[str] = []
        values: list[object] = []
        if service_code is not None:
            clauses.append("catalog.service_code=?")
            values.append(service_code)
        effective_signal = "security" if projection == "security" else signal_class
        if effective_signal is not None:
            clauses.append("slo.signal_class=?")
            values.append(effective_signal)
        if current_only:
            clauses.append("head.current_slo_id=slo.service_level_objective_id")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        base = (
            " FROM service_level_objectives slo "
            "JOIN service_catalog_entries catalog "
            "ON catalog.service_catalog_entry_id=slo.service_catalog_entry_id "
            "JOIN service_level_objective_heads head ON head.slo_code=slo.slo_code"
        )
        rows = connection.execute(
            "SELECT slo.service_level_objective_id" + base + where
            + " ORDER BY slo.slo_code,slo.version_number DESC LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
        total = int(
            connection.execute("SELECT COUNT(*)" + base + where, tuple(values)).fetchone()[0]
        )
        items = tuple(_slo_info(connection, str(row[0])) for row in rows)
        connection.commit()
        return ServiceLevelObjectiveListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            projection=projection,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("observability.unavailable") from None
    finally:
        connection.close()


def create_slo(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    payload: ServiceLevelObjectiveRequest,
    request_id: str,
    trace_id: str,
) -> ServiceLevelObjectiveResponse:
    _valid_idempotency(idempotency_key)
    if expected_revision != payload.previous_version:
        raise ApiError("observability.revision_conflict")
    request_sha256 = _digest(payload.model_dump(mode="json"))
    idempotency_sha256 = _secret_digest(config, "command", idempotency_key)
    slo_id = _id("slo_")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require(actor, "platform.observability.manage")
        replay = connection.execute(
            "SELECT * FROM observability_commands WHERE operator_id=? "
            "AND idempotency_sha256=?",
            (actor.operator_id, idempotency_sha256),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("observability.idempotency_conflict")
            result = ServiceLevelObjectiveResponse(
                slo=_slo_info(connection, str(replay["result_slo_id"])),
                aggregation_job_id=str(replay["aggregation_job_id"]),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        catalog = connection.execute(
            "SELECT * FROM service_catalog_entries WHERE service_code=? AND status='active'",
            (payload.service_code,),
        ).fetchone()
        if catalog is None:
            raise ApiError("observability.service_not_found")
        head = connection.execute(
            "SELECT * FROM service_level_objective_heads WHERE slo_code=?",
            (payload.slo_code,),
        ).fetchone()
        if payload.previous_version == 0:
            if head is not None:
                raise ApiError("observability.revision_conflict")
            version = 1
        else:
            if (
                head is None
                or int(head["current_version"]) != payload.previous_version
                or int(head["revision"]) != expected_revision
            ):
                raise ApiError("observability.revision_conflict")
            previous = connection.execute(
                "SELECT catalog.service_code FROM service_level_objectives slo "
                "JOIN service_catalog_entries catalog ON "
                "catalog.service_catalog_entry_id=slo.service_catalog_entry_id "
                "WHERE slo.service_level_objective_id=?",
                (head["current_slo_id"],),
            ).fetchone()
            if previous is None or previous["service_code"] != payload.service_code:
                raise ApiError("observability.conflict")
            version = payload.previous_version + 1
        now = _database_now(connection)
        window_end = now.replace(second=0, microsecond=0)
        watermark = int(
            connection.execute("SELECT source_watermark FROM telemetry_ingest_head").fetchone()[0]
        )
        prepared = prepare_platform_job(
            config,
            type_code="observability.aggregate",
            payload={
                "slo_code": payload.slo_code,
                "slo_version": version,
                "source_watermark": watermark,
                "window_ended_at": _db_time(window_end),
            },
        )
        definition = {
            "burn_critical_bps": payload.burn_critical_bps,
            "burn_warning_bps": payload.burn_warning_bps,
            "display_name": payload.display_name.strip(),
            "latency_threshold_ms": payload.latency_threshold_ms,
            "operation_code": payload.operation_code,
            "region_code": payload.region_code,
            "service_code": payload.service_code,
            "signal_class": payload.signal_class,
            "sli_kind": payload.sli_kind,
            "slo_code": payload.slo_code,
            "target_bps": payload.target_bps,
            "version_number": version,
            "window_minutes": payload.window_minutes,
        }
        stamp = _db_time(now)
        connection.execute(
            """
            INSERT INTO service_level_objectives(
              service_level_objective_id,slo_code,version_number,service_catalog_entry_id,
              display_name,signal_class,sli_kind,operation_code,region_code,window_minutes,
              target_bps,latency_threshold_ms,burn_warning_bps,burn_critical_bps,
              definition_sha256,created_by_operator_id,reason_code,request_id,trace_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                slo_id,
                payload.slo_code,
                version,
                catalog["service_catalog_entry_id"],
                payload.display_name.strip(),
                payload.signal_class,
                payload.sli_kind,
                payload.operation_code,
                payload.region_code,
                payload.window_minutes,
                payload.target_bps,
                payload.latency_threshold_ms,
                payload.burn_warning_bps,
                payload.burn_critical_bps,
                _digest(definition),
                actor.operator_id,
                payload.reason_code,
                request_id,
                trace_id,
                stamp,
            ),
        )
        if head is None:
            connection.execute(
                "INSERT INTO service_level_objective_heads VALUES(?,?,?,?,?,?)",
                (_id("slh_"), payload.slo_code, slo_id, version, 1, stamp),
            )
        else:
            changed = _db_time(_changed_time(now, head["updated_at"]))
            connection.execute(
                "UPDATE service_level_objective_heads SET current_slo_id=?,"
                "current_version=?,revision=revision+1,updated_at=? "
                "WHERE slo_code=? AND revision=?",
                (slo_id, version, changed, payload.slo_code, expected_revision),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ApiError("observability.revision_conflict")
        job_id, _ = enqueue_prepared_platform_job_in_transaction(
            connection,
            config,
            prepared=prepared,
            priority=90,
            source_code="observability.slo",
            idempotency_key=idempotency_key + ":aggregate",
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.execute(
            """
            INSERT INTO observability_commands(
              observability_command_id,operator_id,action_code,idempotency_sha256,
              request_sha256,result_slo_id,result_version,aggregation_job_id,created_at)
            VALUES(?,?,'slo.version',?,?,?,?,?,?)
            """,
            (
                _id("ocm_"),
                actor.operator_id,
                idempotency_sha256,
                request_sha256,
                slo_id,
                version,
                job_id,
                stamp,
            ),
        )
        _audit(
            connection,
            actor=actor,
            action="observability.slo_versioned",
            object_type="service_level_objective",
            object_id=slo_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={"definition_sha256": _digest(definition), "version": version},
            now=now,
        )
        result = ServiceLevelObjectiveResponse(
            slo=_slo_info(connection, slo_id),
            aggregation_job_id=job_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("observability.unavailable") from None
    finally:
        connection.close()


def list_error_budgets(
    config: AppConfig,
    *,
    session_token: str,
    service_code: str | None,
    window_minutes: int | None,
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> ErrorBudgetListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _authenticate(connection, config, session_token)
        projection = _read_projection(actor)
        binding = {
            "projection": projection,
            "service_code": service_code,
            "window_minutes": window_minutes,
        }
        after = _decode_cursor(config, cursor, binding) if cursor else None
        clauses = ["head.current_slo_id=budget.service_level_objective_id"]
        values: list[object] = []
        if projection == "security":
            clauses.append("slo.signal_class='security'")
        if service_code is not None:
            clauses.append("catalog.service_code=?")
            values.append(service_code)
        if window_minutes is not None:
            clauses.append("slo.window_minutes=?")
            values.append(window_minutes)
        if after is not None:
            clauses.append("(budget.computed_at<? OR "
                           "(budget.computed_at=? AND budget.error_budget_id<?))")
            values.extend((after[0], after[0], after[1]))
        where = " AND ".join(clauses)
        rows = connection.execute(
            f"""
            SELECT budget.error_budget_id FROM error_budgets budget
            JOIN service_level_objectives slo ON
              slo.service_level_objective_id=budget.service_level_objective_id
              AND slo.version_number=budget.slo_version
            JOIN service_level_objective_heads head ON head.slo_code=slo.slo_code
            JOIN service_catalog_entries catalog ON
              catalog.service_catalog_entry_id=slo.service_catalog_entry_id
            WHERE {where}
              AND NOT EXISTS(
                SELECT 1 FROM error_budgets newer
                WHERE newer.service_level_objective_id=budget.service_level_objective_id
                  AND (newer.computed_at>budget.computed_at OR
                    (newer.computed_at=budget.computed_at
                      AND newer.error_budget_id>budget.error_budget_id)))
            ORDER BY budget.computed_at DESC,budget.error_budget_id DESC LIMIT ?
            """,
            (*values, limit + 1),
        ).fetchall()
        page = rows[:limit]
        items = tuple(_budget_info(connection, str(row[0])) for row in page)
        next_cursor = None
        if len(rows) > limit and page:
            tail = connection.execute(
                "SELECT computed_at,error_budget_id FROM error_budgets "
                "WHERE error_budget_id=?", (page[-1][0],)
            ).fetchone()
            next_cursor = _encode_cursor(
                config, str(tail["computed_at"]), str(tail["error_budget_id"]), binding
            )
        slo_ids = tuple(item.service_level_objective_id for item in items)
        alerts = _alerts_for_slos(connection, slo_ids, projection)
        connection.commit()
        return ErrorBudgetListResponse(
            items=items,
            alerts=alerts,
            limit=limit,
            next_cursor=next_cursor,
            projection=projection,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("observability.unavailable") from None
    finally:
        connection.close()


def acknowledge_alert(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    alert_id: str,
    idempotency_key: str,
    payload: AlertAcknowledgementRequest,
    request_id: str,
    trace_id: str,
) -> AlertAcknowledgementResponse:
    if _ALERT_ID_RE.fullmatch(alert_id) is None:
        raise ApiError("observability.alert_not_found")
    _valid_idempotency(idempotency_key)
    request_sha256 = _digest({"alert_id": alert_id, **payload.model_dump(mode="json")})
    idempotency_sha256 = _secret_digest(config, "alert-ack", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require(actor, "platform.observability.alert_ack")
        replay = connection.execute(
            "SELECT * FROM alert_acknowledgements WHERE operator_id=? "
            "AND idempotency_sha256=?",
            (actor.operator_id, idempotency_sha256),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("observability.idempotency_conflict")
            result = AlertAcknowledgementResponse(
                acknowledgement=_ack_info(replay),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        alert = connection.execute(
            "SELECT * FROM platform_alerts WHERE platform_alert_id=?", (alert_id,)
        ).fetchone()
        if alert is None:
            raise ApiError("observability.alert_not_found")
        if int(alert["revision"]) != payload.revision:
            raise ApiError("observability.revision_conflict")
        now = _database_now(connection)
        ack_id = _id("aak_")
        connection.execute(
            """
            INSERT INTO alert_acknowledgements(
              alert_acknowledgement_id,platform_alert_id,operator_id,alert_revision,
              reason_code,idempotency_sha256,request_sha256,request_id,trace_id,acknowledged_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ack_id,
                alert_id,
                actor.operator_id,
                payload.revision,
                payload.reason_code,
                idempotency_sha256,
                request_sha256,
                request_id,
                trace_id,
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor=actor,
            action="observability.alert_acknowledged",
            object_type="platform_alert",
            object_id=alert_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={"alert_revision": payload.revision},
            now=now,
        )
        row = connection.execute(
            "SELECT * FROM alert_acknowledgements WHERE alert_acknowledgement_id=?",
            (ack_id,),
        ).fetchone()
        result = AlertAcknowledgementResponse(
            acknowledgement=_ack_info(row),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("observability.unavailable") from None
    finally:
        connection.close()


def aggregate_slo_in_transaction(
    connection,
    config: AppConfig,
    *,
    slo_code: str,
    slo_version: int,
    source_watermark: int,
    window_ended_at: datetime,
) -> str:
    slo = connection.execute(
        """
        SELECT slo.*,catalog.service_code FROM service_level_objectives slo
        JOIN service_catalog_entries catalog ON
          catalog.service_catalog_entry_id=slo.service_catalog_entry_id
        WHERE slo.slo_code=? AND slo.version_number=?
        """,
        (slo_code, slo_version),
    ).fetchone()
    if slo is None:
        raise ApiError("observability.slo_not_found")
    head = connection.execute("SELECT source_watermark FROM telemetry_ingest_head").fetchone()
    if head is None or source_watermark > int(head[0]):
        raise ApiError("observability.watermark_unavailable")
    window_end = window_ended_at.astimezone(UTC).replace(second=0, microsecond=0)
    window_start = window_end - timedelta(minutes=int(slo["window_minutes"]))
    rows = connection.execute(
        """
        SELECT status_class,duration_bucket,SUM(good_count) good_count,
          SUM(total_count) total_count FROM sli_time_buckets
        WHERE service_code=? AND operation_code=? AND region_code=?
          AND source_watermark<=? AND unixepoch(bucket_started_at)>=unixepoch(?)
          AND unixepoch(bucket_started_at)<unixepoch(?)
        GROUP BY status_class,duration_bucket ORDER BY duration_bucket,status_class
        """,
        (
            slo["service_code"],
            slo["operation_code"],
            slo["region_code"],
            source_watermark,
            _db_time(window_start),
            _db_time(window_end),
        ),
    ).fetchall()
    total = sum(int(row["total_count"]) for row in rows)
    if slo["sli_kind"] == "availability":
        good = sum(int(row["good_count"]) for row in rows)
    else:
        threshold = int(slo["latency_threshold_ms"])
        good = sum(
            int(row["good_count"])
            for row in rows
            if _DURATION_ORDER[str(row["duration_bucket"])] <= threshold
        )
    allowed = (total * (10000 - int(slo["target_bps"]))) // 10000
    consumed = total - good
    remaining = max(allowed - consumed, 0)
    burn = 0 if consumed == 0 else (
        100000000 if allowed == 0 else min(100000000, consumed * 10000 // allowed)
    )
    if total == 0:
        health = "unavailable"
    elif consumed > allowed:
        health = "exhausted"
    elif burn >= int(slo["burn_critical_bps"]):
        health = "critical"
    elif burn >= int(slo["burn_warning_bps"]):
        health = "warning"
    else:
        health = "healthy"
    source = {
        "allowed_bad_count": allowed,
        "consumed_bad_count": consumed,
        "good_count": good,
        "service_level_objective_id": slo["service_level_objective_id"],
        "slo_version": slo_version,
        "source_watermark": source_watermark,
        "total_count": total,
        "window_ended_at": _db_time(window_end),
        "window_started_at": _db_time(window_start),
    }
    source_sha256 = _digest(source)
    existing = connection.execute(
        """
        SELECT * FROM error_budgets WHERE service_level_objective_id=?
          AND slo_version=? AND window_ended_at=? AND source_watermark=?
        """,
        (
            slo["service_level_objective_id"],
            slo_version,
            _db_time(window_end),
            source_watermark,
        ),
    ).fetchone()
    if existing is not None:
        if not hmac.compare_digest(str(existing["source_sha256"]), source_sha256):
            raise ApiError("observability.aggregate_conflict")
        return str(existing["error_budget_id"])
    budget_id = _id("ebg_")
    now = _database_now(connection)
    connection.execute(
        """
        INSERT INTO error_budgets(
          error_budget_id,service_level_objective_id,slo_version,window_started_at,
          window_ended_at,total_count,good_count,allowed_bad_count,consumed_bad_count,
          remaining_bad_count,burn_rate_bps,health_status,source_watermark,
          source_sha256,computed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            budget_id,
            slo["service_level_objective_id"],
            slo_version,
            _db_time(window_start),
            _db_time(window_end),
            total,
            good,
            allowed,
            consumed,
            remaining,
            burn,
            health,
            source_watermark,
            source_sha256,
            _db_time(now),
        ),
    )
    _apply_alert_state(connection, config, slo, health, source_watermark, now)
    if slo["service_code"] == "gateway" and source_watermark >= 1:
        _publish_rollout_health(
            connection,
            config,
            service_code="gateway",
            region_code=str(slo["region_code"]),
            source_watermark=source_watermark,
            window_end=window_end,
            observed_at=now,
        )
    return budget_id


def _apply_alert_state(
    connection, config: AppConfig, slo, health: str, watermark: int, now: datetime
) -> None:
    active = connection.execute(
        "SELECT * FROM platform_alerts WHERE service_level_objective_id=? "
        "AND slo_version=? AND state!='resolved'",
        (slo["service_level_objective_id"], slo["version_number"]),
    ).fetchall()
    if health == "unavailable":
        return
    if health == "healthy":
        for alert in active:
            _transition_alert(connection, alert, "resolved", watermark, now, "budget_recovered")
            _event(
                connection,
                slo,
                alert["platform_alert_id"],
                "alert.resolved",
                "info",
                "budget_recovered",
                watermark,
                now,
            )
        return
    reason = "error_budget_exhausted" if health == "exhausted" else "slo_breached"
    severity = "critical" if health in {"critical", "exhausted"} else "warning"
    dedupe = _secret_digest(
        config,
        "alert-dedupe",
        _canonical(
            {
                "region_code": slo["region_code"],
                "safe_reason_code": reason,
                "service_level_objective_id": slo["service_level_objective_id"],
                "slo_version": slo["version_number"],
            }
        ),
    )
    for alert in active:
        if alert["dedupe_sha256"] != dedupe:
            _transition_alert(connection, alert, "resolved", watermark, now, "budget_recovered")
            _event(
                connection,
                slo,
                alert["platform_alert_id"],
                "alert.resolved",
                "info",
                "budget_recovered",
                watermark,
                now,
            )
    alert = connection.execute(
        "SELECT * FROM platform_alerts WHERE dedupe_sha256=?", (dedupe,)
    ).fetchone()
    route = connection.execute(
        "SELECT status FROM on_call_routes WHERE service_code=? AND severity=?",
        (slo["service_code"], severity),
    ).fetchone()
    state = "suppressed" if route is not None and route["status"] == "disabled" else "firing"
    if alert is None:
        alert_id = _id("alt_")
        stamp = _db_time(now)
        connection.execute(
            """
            INSERT INTO platform_alerts(
              platform_alert_id,dedupe_sha256,service_level_objective_id,slo_version,
              service_code,region_code,severity,state,safe_reason_code,source_watermark,
              first_seen_at,last_seen_at,suppressed_until,resolved_at,revision,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,1,?,?)
            """,
            (
                alert_id,
                dedupe,
                slo["service_level_objective_id"],
                slo["version_number"],
                slo["service_code"],
                slo["region_code"],
                severity,
                state,
                reason,
                watermark,
                stamp,
                stamp,
                _db_time(now + timedelta(minutes=5)) if state == "suppressed" else None,
                stamp,
                stamp,
            ),
        )
        _alert_transition(connection, alert_id, None, state, 1, reason, watermark, now)
        _event(connection, slo, alert_id, "alert.triggered", severity, reason, watermark, now)
        _event(
            connection,
            slo,
            alert_id,
            "error_budget.exhausted" if reason == "error_budget_exhausted" else "slo.breached",
            severity,
            reason,
            watermark,
            now,
        )
        return
    if (
        alert["state"] == state
        and alert["severity"] == severity
        and int(alert["source_watermark"]) == watermark
    ):
        return
    prior_state = str(alert["state"])
    revision = int(alert["revision"]) + 1
    changed = _db_time(_changed_time(now, alert["updated_at"]))
    connection.execute(
        """
        UPDATE platform_alerts SET severity=?,state=?,source_watermark=?,last_seen_at=?,
          suppressed_until=?,resolved_at=NULL,revision=?,updated_at=?
        WHERE platform_alert_id=? AND revision=?
        """,
        (
            severity,
            state,
            watermark,
            changed,
            _db_time(now + timedelta(minutes=5)) if state == "suppressed" else None,
            revision,
            changed,
            alert["platform_alert_id"],
            int(alert["revision"]),
        ),
    )
    transition_reason = reason if prior_state == "resolved" else (
        "notification_suppressed" if state == "suppressed" else "notification_resumed"
    )
    _alert_transition(
        connection,
        str(alert["platform_alert_id"]),
        prior_state,
        state,
        revision,
        transition_reason,
        watermark,
        now,
    )
    if prior_state == "resolved":
        _event(
            connection,
            slo,
            alert["platform_alert_id"],
            "alert.triggered",
            severity,
            reason,
            watermark,
            now,
        )


def _transition_alert(
    connection, alert, state: str, watermark: int, now: datetime, reason: str
) -> None:
    revision = int(alert["revision"]) + 1
    changed = _db_time(_changed_time(now, alert["updated_at"]))
    connection.execute(
        "UPDATE platform_alerts SET state=?,source_watermark=?,last_seen_at=?,"
        "suppressed_until=NULL,resolved_at=?,revision=?,updated_at=? "
        "WHERE platform_alert_id=? AND revision=?",
        (
            state,
            watermark,
            changed,
            changed if state == "resolved" else None,
            revision,
            changed,
            alert["platform_alert_id"],
            int(alert["revision"]),
        ),
    )
    _alert_transition(
        connection,
        str(alert["platform_alert_id"]),
        str(alert["state"]),
        state,
        revision,
        reason,
        watermark,
        now,
    )


def _alert_transition(
    connection,
    alert_id: str,
    from_state: str | None,
    to_state: str,
    revision: int,
    reason: str,
    watermark: int,
    now: datetime,
) -> None:
    connection.execute(
        "INSERT INTO platform_alert_transitions VALUES(?,?,?,?,?,?,?,?)",
        (_id("atr_"), alert_id, from_state, to_state, revision, reason, watermark, _db_time(now)),
    )


def _event(
    connection,
    slo,
    alert_id: object,
    event_type: str,
    severity: str,
    reason: str,
    watermark: int,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO observability_event_outbox(
          observability_event_outbox_id,event_type,service_code,
          service_level_objective_id,slo_version,platform_alert_id,severity,
          safe_reason_code,source_watermark,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("oeo_"),
            event_type,
            slo["service_code"],
            slo["service_level_objective_id"],
            slo["version_number"],
            alert_id,
            severity,
            reason,
            watermark,
            _db_time(now),
        ),
    )


def _publish_rollout_health(
    connection,
    config: AppConfig,
    *,
    service_code: str,
    region_code: str,
    source_watermark: int,
    window_end: datetime,
    observed_at: datetime,
) -> None:
    existing = connection.execute(
        "SELECT 1 FROM rollout_health_summaries WHERE service_code=? "
        "AND region_code=? AND source_watermark=?",
        (service_code, region_code, source_watermark),
    ).fetchone()
    if existing is not None:
        return
    rows = connection.execute(
        """
        SELECT status_class,duration_bucket,SUM(total_count) total_count
        FROM sli_time_buckets WHERE service_code=? AND region_code=?
          AND source_watermark<=? AND unixepoch(bucket_started_at)>=unixepoch(?)
          AND unixepoch(bucket_started_at)<unixepoch(?)
        GROUP BY status_class,duration_bucket
        """,
        (
            service_code,
            region_code,
            source_watermark,
            _db_time(window_end - timedelta(minutes=5)),
            _db_time(window_end),
        ),
    ).fetchall()
    total = sum(int(row["total_count"]) for row in rows)
    good = sum(
        int(row["total_count"]) for row in rows if row["status_class"] in {"2xx", "3xx"}
    )
    error_rate = 0 if total == 0 else (total - good) * 10000 // total
    latency = _p95(rows)
    status = "unavailable" if total == 0 else (
        "unhealthy" if error_rate >= 500 else "healthy"
    )
    body = {
        "service_code": service_code,
        "region_code": region_code,
        "watermark": source_watermark,
        "health_status": status,
        "error_rate_bps": error_rate,
        "latency_p95_ms": latency,
    }
    canonical = _canonical(body)
    signature = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd107/health-summary/v1\0{canonical}".encode(),
        hashlib.sha256,
    ).hexdigest()
    connection.execute(
        "INSERT INTO rollout_health_summaries VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            _id("rhs_"),
            service_code,
            region_code,
            source_watermark,
            status,
            error_rate,
            latency,
            signature,
            _db_time(observed_at),
            _db_time(observed_at + timedelta(minutes=5)),
        ),
    )


def _service_info(connection, row) -> ServiceCatalogEntryInfo:
    watermark = int(
        connection.execute("SELECT source_watermark FROM telemetry_ingest_head").fetchone()[0]
    )
    latest = connection.execute(
        "SELECT MAX(bucket_started_at) FROM sli_time_buckets WHERE service_code=? "
        "AND source_watermark<=?",
        (row["service_code"], watermark),
    ).fetchone()[0]
    buckets = [] if latest is None else connection.execute(
        "SELECT status_class,duration_bucket,total_count FROM sli_time_buckets "
        "WHERE service_code=? AND source_watermark<=? AND bucket_started_at=?",
        (row["service_code"], watermark, latest),
    ).fetchall()
    total = sum(int(bucket["total_count"]) for bucket in buckets)
    good = sum(
        int(bucket["total_count"])
        for bucket in buckets
        if bucket["status_class"] in {"2xx", "3xx"}
    )
    error_rate = 0 if total == 0 else (total - good) * 10000 // total
    return ServiceCatalogEntryInfo(
        service_catalog_entry_id=row["service_catalog_entry_id"],
        service_code=row["service_code"],
        display_name=row["display_name"],
        owner_team_code=row["owner_team_code"],
        service_tier=int(row["service_tier"]),
        golden_signals=tuple(json.loads(row["golden_signals_json"])),
        status=row["status"],
        revision=int(row["revision"]),
        source_watermark=watermark,
        total_count=total,
        error_rate_bps=error_rate,
        latency_p95_ms=_p95(buckets),
        health_status="unavailable" if total == 0 else (
            "unhealthy" if error_rate >= 500 else "healthy"
        ),
    )


def _p95(rows) -> int:
    counts: dict[str, int] = {}
    for row in rows:
        bucket = str(row["duration_bucket"])
        counts[bucket] = counts.get(bucket, 0) + int(row["total_count"])
    total = sum(counts.values())
    if total == 0:
        return 0
    threshold = (total * 95 + 99) // 100
    cumulative = 0
    for bucket, upper in _DURATION_ORDER.items():
        cumulative += counts.get(bucket, 0)
        if cumulative >= threshold:
            return upper
    return 5001


def _slo_info(connection, slo_id: str) -> ServiceLevelObjectiveInfo:
    row = connection.execute(
        """
        SELECT slo.*,catalog.service_code,
          CASE WHEN head.current_slo_id=slo.service_level_objective_id THEN 1 ELSE 0 END is_current
        FROM service_level_objectives slo
        JOIN service_catalog_entries catalog ON
          catalog.service_catalog_entry_id=slo.service_catalog_entry_id
        JOIN service_level_objective_heads head ON head.slo_code=slo.slo_code
        WHERE slo.service_level_objective_id=?
        """,
        (slo_id,),
    ).fetchone()
    if row is None:
        raise ApiError("observability.slo_not_found")
    return ServiceLevelObjectiveInfo(
        service_level_objective_id=row["service_level_objective_id"],
        slo_code=row["slo_code"],
        version_number=int(row["version_number"]),
        service_code=row["service_code"],
        display_name=row["display_name"],
        signal_class=row["signal_class"],
        sli_kind=row["sli_kind"],
        operation_code=row["operation_code"],
        region_code=row["region_code"],
        window_minutes=int(row["window_minutes"]),
        target_bps=int(row["target_bps"]),
        latency_threshold_ms=row["latency_threshold_ms"],
        burn_warning_bps=int(row["burn_warning_bps"]),
        burn_critical_bps=int(row["burn_critical_bps"]),
        definition_sha256=row["definition_sha256"],
        is_current=bool(row["is_current"]),
        created_at=_time(row["created_at"]),
    )


def _budget_info(connection, budget_id: str) -> ErrorBudgetInfo:
    row = connection.execute(
        """
        SELECT budget.*,slo.slo_code,slo.signal_class,slo.region_code,catalog.service_code
        FROM error_budgets budget JOIN service_level_objectives slo ON
          slo.service_level_objective_id=budget.service_level_objective_id
          AND slo.version_number=budget.slo_version
        JOIN service_catalog_entries catalog ON
          catalog.service_catalog_entry_id=slo.service_catalog_entry_id
        WHERE budget.error_budget_id=?
        """,
        (budget_id,),
    ).fetchone()
    if row is None:
        raise ApiError("observability.budget_not_found")
    return ErrorBudgetInfo(
        error_budget_id=row["error_budget_id"],
        service_level_objective_id=row["service_level_objective_id"],
        slo_code=row["slo_code"],
        slo_version=int(row["slo_version"]),
        service_code=row["service_code"],
        signal_class=row["signal_class"],
        region_code=row["region_code"],
        window_started_at=_time(row["window_started_at"]),
        window_ended_at=_time(row["window_ended_at"]),
        total_count=int(row["total_count"]),
        good_count=int(row["good_count"]),
        allowed_bad_count=int(row["allowed_bad_count"]),
        consumed_bad_count=int(row["consumed_bad_count"]),
        remaining_bad_count=int(row["remaining_bad_count"]),
        burn_rate_bps=int(row["burn_rate_bps"]),
        health_status=row["health_status"],
        source_watermark=int(row["source_watermark"]),
        source_sha256=row["source_sha256"],
        computed_at=_time(row["computed_at"]),
    )


def _alerts_for_slos(
    connection, slo_ids: tuple[str, ...], projection: str
) -> tuple[AlertInfo, ...]:
    if not slo_ids:
        return ()
    placeholders = ",".join("?" for _ in slo_ids)
    rows = connection.execute(
        f"""
        SELECT alert.*,slo.slo_code,slo.signal_class,
          (SELECT MAX(ack.acknowledged_at) FROM alert_acknowledgements ack
           WHERE ack.platform_alert_id=alert.platform_alert_id) latest_ack
        FROM platform_alerts alert JOIN service_level_objectives slo ON
          slo.service_level_objective_id=alert.service_level_objective_id
          AND slo.version_number=alert.slo_version
        WHERE alert.service_level_objective_id IN({placeholders})
          {"AND slo.signal_class='security'" if projection == "security" else ""}
        ORDER BY alert.last_seen_at DESC,alert.platform_alert_id DESC
        """,
        slo_ids,
    ).fetchall()
    return tuple(
        AlertInfo(
            alert_id=row["platform_alert_id"],
            service_level_objective_id=row["service_level_objective_id"],
            slo_code=row["slo_code"],
            service_code=row["service_code"],
            signal_class=row["signal_class"],
            region_code=row["region_code"],
            severity=row["severity"],
            state=row["state"],
            safe_reason_code=row["safe_reason_code"],
            source_watermark=int(row["source_watermark"]),
            revision=int(row["revision"]),
            first_seen_at=_time(row["first_seen_at"]),
            last_seen_at=_time(row["last_seen_at"]),
            resolved_at=None if row["resolved_at"] is None else _time(row["resolved_at"]),
            latest_acknowledgement_at=(
                None if row["latest_ack"] is None else _time(row["latest_ack"])
            ),
        )
        for row in rows
    )


def _ack_info(row) -> AlertAcknowledgementInfo:
    return AlertAcknowledgementInfo(
        acknowledgement_id=row["alert_acknowledgement_id"],
        alert_id=row["platform_alert_id"],
        alert_revision=int(row["alert_revision"]),
        reason_code=row["reason_code"],
        acknowledged_at=_time(row["acknowledged_at"]),
    )


def _encode_cursor(config: AppConfig, stamp: str, budget_id: str, binding: object) -> str:
    body = _canonical({"binding": binding, "budget_id": budget_id, "computed_at": stamp})
    signature = _secret_digest(config, "budget-cursor", body)
    return (
        base64.urlsafe_b64encode(f"{body}.{signature}".encode("ascii"))
        .decode("ascii")
        .rstrip("=")
    )


def _decode_cursor(
    config: AppConfig, cursor: str, binding: object
) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")
        body, signature = decoded.rsplit(".", 1)
        if not hmac.compare_digest(signature, _secret_digest(config, "budget-cursor", body)):
            raise ValueError
        value = json.loads(body)
        if value["binding"] != binding:
            raise ValueError
        stamp = str(value["computed_at"])
        budget_id = str(value["budget_id"])
        _time(stamp)
        if re.fullmatch(r"ebg_[0-9a-f]{32}", budget_id) is None:
            raise ValueError
        return stamp, budget_id
    except (KeyError, UnicodeError, ValueError, json.JSONDecodeError):
        raise ApiError("observability.cursor_invalid") from None


def _authenticate(connection, config: AppConfig, session_token: str) -> PlatformContext:
    return authenticate_platform_session_in_transaction(
        connection, config, session_token=session_token, touch=True
    )


def _read_projection(actor: PlatformContext) -> str:
    if "platform.observability.read" in actor.permissions:
        return "operations"
    if "platform.observability.security_read" in actor.permissions:
        return "security"
    raise ApiError("platform.permission_denied")


def _require(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _audit(
    connection,
    *,
    actor: PlatformContext,
    action: str,
    object_type: str,
    object_id: str,
    reason: str,
    request_id: str,
    trace_id: str,
    after: object,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,'platform',?,?,?,?,?,?,?,NULL,?,?)
        """,
        (
            _id("pau_"), actor.operator_id, action, object_type, object_id, reason,
            request_id, trace_id, _digest(after), _db_time(now),
        ),
    )


def _valid_idempotency(value: str) -> None:
    if _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError("observability.request_invalid")


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("observability.unavailable") from None


def _rollback(connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


def _secret_digest(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd109/{purpose}/v1\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _database_now(connection) -> datetime:
    return _time(connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0])


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "acknowledge_alert",
    "aggregate_slo_in_transaction",
    "create_slo",
    "list_error_budgets",
    "list_services",
    "list_slos",
]
