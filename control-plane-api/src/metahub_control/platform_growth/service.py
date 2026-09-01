"""Owner-qualified PRD-141 Platform Growth queries and commands."""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import sqlite3
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)

from .artifacts import load_artifact, store_artifact
from .models import (
    AttributionResponse,
    AttributionSummary,
    CampaignCommand,
    CampaignListResponse,
    CampaignResponse,
    CampaignSummary,
    CohortBucket,
    CohortResponse,
    ExperimentCommand,
    ExperimentListResponse,
    ExperimentResponse,
    ExperimentSummary,
    FunnelBucket,
    FunnelResponse,
    FunnelStage,
    GrowthExportApprovalCommand,
    GrowthExportReceiptResponse,
    GrowthExportRequest,
    GrowthExportResponse,
    GrowthExportSummary,
    SourceFreshness,
)
from .permissions import (
    CAMPAIGN_MANAGE,
    EXPERIMENT_MANAGE,
    EXPORT_APPROVE,
    EXPORT_DOWNLOAD,
    EXPORT_REQUEST,
    READ,
    read_scope,
    require,
)

_CURSOR_DOMAIN = b"prd141/platform/growth-cursor/v1"
_IDEMPOTENCY_DOMAIN = b"prd141/platform/growth-idempotency/v1"
_RECEIPT_DOMAIN = b"prd141/platform/growth-export-receipt/v1"
_CAMPAIGN_ID = re.compile(r"^gca_[0-9a-f]{32}$")
_EXPERIMENT_ID = re.compile(r"^gex_[0-9a-f]{32}$")
_EXPORT_ID = re.compile(r"^gxp_[0-9a-f]{32}$")
_SOURCE_ID = re.compile(r"^acs_[0-9a-f]{32}$")
_CAMPAIGN_TRANSITIONS = {
    ("draft", "schedule"): "scheduled",
    ("active", "pause"): "paused",
    ("paused", "resume"): "active",
    ("active", "complete"): "completed",
    ("paused", "complete"): "completed",
    ("completed", "archive"): "archived",
}
_EXPERIMENT_TRANSITIONS = {
    ("draft", "schedule"): ("scheduled", None),
    ("scheduled", "start"): ("running", None),
    ("running", "pause"): ("paused", None),
    ("paused", "resume"): ("running", None),
    ("running", "stop"): ("stopped", "stopped_guardrail"),
    ("paused", "stop"): ("stopped", "stopped_guardrail"),
    ("running", "decide"): ("decided", "inconclusive"),
    ("paused", "decide"): ("decided", "inconclusive"),
    ("decided", "archive"): ("archived", "preserve"),
    ("stopped", "archive"): ("archived", "preserve"),
    ("canceled", "archive"): ("archived", "preserve"),
}


def list_funnels(
    config: AppConfig,
    *,
    session_token: str,
    filters: dict[str, str],
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> FunnelResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token, None)
        scope = read_scope(actor)
        _require_schema(connection)
        normalized = _funnel_filters(filters, scope)
        filter_digest = _digest(normalized)
        boundary = _decode_cursor(config, actor, cursor, filter_digest, "funnel")
        clauses = [
            "snapshot.view_code=?",
            "snapshot.bucket_kind=?",
            "snapshot.bucket_start>=?",
            "snapshot.bucket_start<=?",
            "snapshot.dimension_code=?",
            "snapshot.formula_version=?",
            "NOT EXISTS(SELECT 1 FROM platform_growth_funnel_snapshots newer "
            "WHERE newer.bucket_start=snapshot.bucket_start "
            "AND newer.bucket_kind=snapshot.bucket_kind "
            "AND newer.view_code=snapshot.view_code "
            "AND newer.dimension_code=snapshot.dimension_code "
            "AND newer.dimension_value=snapshot.dimension_value "
            "AND newer.formula_version=snapshot.formula_version "
            "AND newer.generation>snapshot.generation)",
        ]
        values: list[object] = [
            normalized["view"],
            normalized["bucket"],
            normalized["from"],
            normalized["to"],
            normalized["dimension"],
            normalized["formula_version"],
        ]
        if "dimension_value" in normalized:
            clauses.append("snapshot.dimension_value=?")
            values.append(normalized["dimension_value"])
        if boundary is not None:
            clauses.append(
                "(snapshot.bucket_start>? OR(snapshot.bucket_start=? "
                "AND snapshot.growth_funnel_snapshot_id>?))"
            )
            values.extend((boundary[0], boundary[0], boundary[1]))
        rows = connection.execute(
            f"""SELECT snapshot.* FROM platform_growth_funnel_snapshots snapshot
                WHERE {' AND '.join(clauses)}
                ORDER BY snapshot.bucket_start,snapshot.growth_funnel_snapshot_id
                LIMIT ?""",
            (*values, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                config,
                actor,
                filter_digest,
                "funnel",
                str(last["bucket_start"]),
                str(last["growth_funnel_snapshot_id"]),
            )
        items = tuple(_funnel_summary(row, scope) for row in visible)
        generated_at = _db_time(_utcnow())
        response = FunnelResponse(
            items=items,
            limit=limit,
            has_more=has_more,
            next_cursor=next_cursor,
            formula_version=normalized["formula_version"],
            source_freshness=_freshness(visible, "generated_at", "source_watermark"),
            generated_at=generated_at,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_cohorts(
    config: AppConfig,
    *,
    session_token: str,
    filters: dict[str, str],
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> CohortResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token, None)
        scope = read_scope(actor)
        _require_schema(connection)
        normalized = _cohort_filters(filters, scope)
        filter_digest = _digest(normalized)
        boundary = _decode_cursor(config, actor, cursor, filter_digest, "cohort")
        clauses = [
            "snapshot.view_code=?",
            "snapshot.cohort_kind=?",
            "snapshot.cohort_bucket>=?",
            "snapshot.cohort_bucket<=?",
            "snapshot.dimension_code=?",
            "snapshot.formula_version=?",
            "snapshot.age_bucket<=?",
            "NOT EXISTS(SELECT 1 FROM platform_growth_cohort_snapshots newer "
            "WHERE newer.cohort_kind=snapshot.cohort_kind "
            "AND newer.cohort_bucket=snapshot.cohort_bucket "
            "AND newer.age_bucket=snapshot.age_bucket "
            "AND newer.view_code=snapshot.view_code "
            "AND newer.dimension_code=snapshot.dimension_code "
            "AND newer.dimension_value=snapshot.dimension_value "
            "AND newer.formula_version=snapshot.formula_version "
            "AND newer.generation>snapshot.generation)",
        ]
        values: list[object] = [
            normalized["view"],
            normalized["cohort_kind"],
            normalized["from"],
            normalized["to"],
            normalized["dimension"],
            normalized["formula_version"],
            int(normalized["age_buckets"]) - 1,
        ]
        if "dimension_value" in normalized:
            clauses.append("snapshot.dimension_value=?")
            values.append(normalized["dimension_value"])
        if boundary is not None:
            clauses.append(
                "(snapshot.cohort_bucket>? OR(snapshot.cohort_bucket=? "
                "AND snapshot.growth_cohort_snapshot_id>?))"
            )
            values.extend((boundary[0], boundary[0], boundary[1]))
        rows = connection.execute(
            f"""SELECT snapshot.* FROM platform_growth_cohort_snapshots snapshot
                WHERE {' AND '.join(clauses)}
                ORDER BY snapshot.cohort_bucket,snapshot.growth_cohort_snapshot_id
                LIMIT ?""",
            (*values, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                config,
                actor,
                filter_digest,
                "cohort",
                str(last["cohort_bucket"]),
                str(last["growth_cohort_snapshot_id"]),
            )
        response = CohortResponse(
            items=tuple(_cohort_summary(row, scope) for row in visible),
            limit=limit,
            has_more=has_more,
            next_cursor=next_cursor,
            formula_version=normalized["formula_version"],
            source_freshness=_freshness(visible, "generated_at", "source_watermark"),
            generated_at=_db_time(_utcnow()),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_campaigns(
    config: AppConfig,
    *,
    session_token: str,
    filters: dict[str, str],
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> CampaignListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token, None)
        require(actor, READ)
        _require_schema(connection)
        normalized = _campaign_filters(filters)
        filter_digest = _digest(normalized)
        boundary = _decode_cursor(config, actor, cursor, filter_digest, "campaign")
        clauses: list[str] = []
        values: list[object] = []
        for key, column in (
            ("status", "campaign.status"),
            ("source", "campaign.acquisition_source_id"),
            ("channel", "campaign.channel_code"),
        ):
            if key in normalized:
                clauses.append(f"{column}=?")
                values.append(normalized[key])
        if "from" in normalized:
            clauses.append("campaign.starts_at>=?")
            values.append(_day_start(normalized["from"]))
        if "to" in normalized:
            clauses.append("campaign.starts_at<?")
            values.append(_day_start(_day_after(normalized["to"])))
        if boundary is not None:
            clauses.append(
                "(campaign.updated_at<? OR(campaign.updated_at=? "
                "AND campaign.growth_campaign_id>?))"
            )
            values.extend((boundary[0], boundary[0], boundary[1]))
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = connection.execute(
            f"""SELECT campaign.* FROM platform_growth_campaigns campaign
                WHERE {where}
                ORDER BY campaign.updated_at DESC,campaign.growth_campaign_id LIMIT ?""",
            (*values, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                config,
                actor,
                filter_digest,
                "campaign",
                str(last["updated_at"]),
                str(last["growth_campaign_id"]),
            )
        response = CampaignListResponse(
            items=tuple(_campaign_summary(connection, row) for row in visible),
            limit=limit,
            has_more=has_more,
            next_cursor=next_cursor,
            source_freshness=_campaign_freshness(connection, visible),
            generated_at=_db_time(_utcnow()),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def apply_campaign_command(
    config: AppConfig,
    command: CampaignCommand,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> CampaignResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(connection, config, session_token, csrf_token)
        require(actor, CAMPAIGN_MANAGE)
        _require_schema(connection)
        now = _utcnow()
        action = f"campaign.{command.operation}"
        request_hash = _digest({"action": action, "command": command.model_dump(mode="json")})
        replay = _command_replay(connection, config, actor, action, idempotency_key, request_hash)
        if replay is not None:
            row = _campaign_row(connection, replay[0])
            response = CampaignResponse(
                campaign=_campaign_summary(connection, row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code=f"platform.growth.{action}",
            resource_type="growth_campaign",
            resource_id=str(command.campaign_id or "new"),
            now=now,
        )
        if command.operation == "create":
            _require_source(connection, str(command.acquisition_source_id))
            campaign_id = _id("gca_")
            stamp = _db_time(now)
            _validate_window(str(command.starts_at), str(command.ends_at))
            source_hash = _digest(_campaign_config(command))
            connection.execute(
                """INSERT INTO platform_growth_campaigns(
                  growth_campaign_id,display_name,acquisition_source_id,channel_code,
                  target_stage,starts_at,ends_at,budget_minor,currency_code,status,revision,
                  source_sha256,created_by_operator_id,updated_by_operator_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'draft',1,?,?,?,?,?)""",
                (
                    campaign_id,
                    command.display_name,
                    command.acquisition_source_id,
                    command.channel_code,
                    command.target_stage,
                    command.starts_at,
                    command.ends_at,
                    command.budget_minor,
                    command.currency,
                    source_hash,
                    actor.operator_id,
                    actor.operator_id,
                    stamp,
                    stamp,
                ),
            )
        else:
            campaign_id = str(command.campaign_id)
            row = _campaign_row(connection, campaign_id)
            if int(row["revision"]) != command.expected_revision:
                raise ApiError("platform.growth_state_conflict")
            stamp = _next_stamp(str(row["updated_at"]), now)
            if command.operation == "update":
                if row["status"] not in {"draft", "scheduled"}:
                    raise ApiError("platform.growth_state_conflict")
                _require_source(connection, str(command.acquisition_source_id))
                _validate_window(str(command.starts_at), str(command.ends_at))
                source_hash = _digest(_campaign_config(command))
                connection.execute(
                    """UPDATE platform_growth_campaigns SET display_name=?,
                      acquisition_source_id=?,channel_code=?,target_stage=?,starts_at=?,ends_at=?,
                      budget_minor=?,currency_code=?,source_sha256=?,revision=revision+1,
                      updated_by_operator_id=?,updated_at=? WHERE growth_campaign_id=?""",
                    (
                        command.display_name,
                        command.acquisition_source_id,
                        command.channel_code,
                        command.target_stage,
                        command.starts_at,
                        command.ends_at,
                        command.budget_minor,
                        command.currency,
                        source_hash,
                        actor.operator_id,
                        stamp,
                        campaign_id,
                    ),
                )
            else:
                target = _CAMPAIGN_TRANSITIONS.get((str(row["status"]), command.operation))
                if target is None:
                    raise ApiError("platform.growth_state_conflict")
                connection.execute(
                    """UPDATE platform_growth_campaigns SET status=?,revision=revision+1,
                      updated_by_operator_id=?,updated_at=? WHERE growth_campaign_id=?""",
                    (target, actor.operator_id, stamp, campaign_id),
                )
        changed = _campaign_row(connection, campaign_id)
        _event(
            connection,
            event_type="growth.campaign_changed",
            object_kind="campaign",
            object_id=campaign_id,
            revision=int(changed["revision"]),
            generation=int(changed["revision"]),
            payload={
                "campaign_id": campaign_id,
                "revision": int(changed["revision"]),
                "schema_version": "growth-event-v1",
                "source_digest": changed["source_sha256"],
                "status": changed["status"],
            },
            now=now,
        )
        _record_command(
            connection,
            config,
            actor,
            action,
            idempotency_key,
            request_hash,
            command.expected_revision,
            "campaign",
            campaign_id,
            int(changed["revision"]),
            request_id,
            trace_id,
            now,
        )
        _audit(
            connection,
            actor,
            f"platform.growth_{action}",
            "growth_campaign",
            campaign_id,
            request_hash,
            request_id,
            trace_id,
            now,
        )
        response = CampaignResponse(
            campaign=_campaign_summary(connection, changed),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_experiments(
    config: AppConfig,
    *,
    session_token: str,
    filters: dict[str, str],
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> ExperimentListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token, None)
        require(actor, READ)
        _require_schema(connection)
        normalized = _experiment_filters(filters)
        filter_digest = _digest(normalized)
        boundary = _decode_cursor(config, actor, cursor, filter_digest, "experiment")
        clauses: list[str] = []
        values: list[object] = []
        for key, column in (
            ("status", "experiment.status"),
            ("surface", "experiment.surface_code"),
            ("metric", "experiment.primary_metric_code"),
        ):
            if key in normalized:
                clauses.append(f"{column}=?")
                values.append(normalized[key])
        if "from" in normalized:
            clauses.append("experiment.starts_at>=?")
            values.append(_day_start(normalized["from"]))
        if "to" in normalized:
            clauses.append("experiment.starts_at<?")
            values.append(_day_start(_day_after(normalized["to"])))
        if boundary is not None:
            clauses.append(
                "(experiment.updated_at<? OR(experiment.updated_at=? "
                "AND experiment.growth_experiment_id>?))"
            )
            values.extend((boundary[0], boundary[0], boundary[1]))
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = connection.execute(
            f"""SELECT experiment.* FROM platform_growth_experiments experiment
                WHERE {where}
                ORDER BY experiment.updated_at DESC,experiment.growth_experiment_id LIMIT ?""",
            (*values, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                config,
                actor,
                filter_digest,
                "experiment",
                str(last["updated_at"]),
                str(last["growth_experiment_id"]),
            )
        response = ExperimentListResponse(
            items=tuple(_experiment_summary(connection, row) for row in visible),
            limit=limit,
            has_more=has_more,
            next_cursor=next_cursor,
            source_freshness=_experiment_freshness(connection, visible),
            generated_at=_db_time(_utcnow()),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def apply_experiment_command(
    config: AppConfig,
    command: ExperimentCommand,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ExperimentResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(connection, config, session_token, csrf_token)
        require(actor, EXPERIMENT_MANAGE)
        _require_schema(connection)
        now = _utcnow()
        action = f"experiment.{command.operation}"
        request_hash = _digest({"action": action, "command": command.model_dump(mode="json")})
        replay = _command_replay(connection, config, actor, action, idempotency_key, request_hash)
        if replay is not None:
            row = _experiment_row(connection, replay[0])
            response = ExperimentResponse(
                experiment=_experiment_summary(connection, row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code=f"platform.growth.{action}",
            resource_type="growth_experiment",
            resource_id=str(command.experiment_id or "new"),
            now=now,
        )
        terminal_event = False
        if command.operation == "create":
            _validate_experiment(command)
            experiment_id = _id("gex_")
            stamp = _db_time(now)
            canonical = _digest(_experiment_config(command))
            connection.execute(
                """INSERT INTO platform_growth_experiments(
                  growth_experiment_id,hypothesis_code,surface_code,primary_metric_code,
                  control_code,variant_code,control_allocation_bps,variant_allocation_bps,
                  eligibility_policy_version,consent_purpose,consent_version,sample_cap,
                  starts_at,ends_at,guardrail_code,decision_policy_version,status,
                  decision_code,source_watermark,revision,canonical_sha256,
                  created_by_operator_id,updated_by_operator_id,created_at,updated_at,decided_at)
                VALUES(?,?,?,?,?,?,?,?,?,'analytics',?,?,?,?,?,?,'draft',NULL,NULL,1,?,?,?,?,?,NULL)""",
                (
                    experiment_id,
                    command.hypothesis_code,
                    command.surface_code,
                    command.primary_metric,
                    command.control_code,
                    command.variant_code,
                    command.control_allocation_bps,
                    command.variant_allocation_bps,
                    command.eligibility_policy_version,
                    command.consent_version,
                    command.sample_cap,
                    command.starts_at,
                    command.ends_at,
                    command.guardrail_code,
                    command.decision_policy_version,
                    canonical,
                    actor.operator_id,
                    actor.operator_id,
                    stamp,
                    stamp,
                ),
            )
        else:
            experiment_id = str(command.experiment_id)
            row = _experiment_row(connection, experiment_id)
            if int(row["revision"]) != command.expected_revision:
                raise ApiError("platform.growth_state_conflict")
            stamp = _next_stamp(str(row["updated_at"]), now)
            if command.operation == "update":
                if row["status"] not in {"draft", "scheduled"}:
                    raise ApiError("platform.growth_state_conflict")
                _validate_experiment(command)
                canonical = _digest(_experiment_config(command))
                connection.execute(
                    """UPDATE platform_growth_experiments SET hypothesis_code=?,surface_code=?,
                      primary_metric_code=?,control_code=?,variant_code=?,control_allocation_bps=?,
                      variant_allocation_bps=?,eligibility_policy_version=?,consent_version=?,
                      sample_cap=?,starts_at=?,ends_at=?,guardrail_code=?,decision_policy_version=?,
                      canonical_sha256=?,revision=revision+1,updated_by_operator_id=?,updated_at=?
                    WHERE growth_experiment_id=?""",
                    (
                        command.hypothesis_code,
                        command.surface_code,
                        command.primary_metric,
                        command.control_code,
                        command.variant_code,
                        command.control_allocation_bps,
                        command.variant_allocation_bps,
                        command.eligibility_policy_version,
                        command.consent_version,
                        command.sample_cap,
                        command.starts_at,
                        command.ends_at,
                        command.guardrail_code,
                        command.decision_policy_version,
                        canonical,
                        actor.operator_id,
                        stamp,
                        experiment_id,
                    ),
                )
            else:
                transition = _EXPERIMENT_TRANSITIONS.get((str(row["status"]), command.operation))
                if transition is None:
                    raise ApiError("platform.growth_state_conflict")
                target, decision = transition
                if command.operation in {"start", "decide"}:
                    _require_source_watermark(connection, command.source_watermark)
                if decision == "preserve":
                    decision = str(row["decision_code"])
                terminal_event = row["decision_code"] is None and decision is not None
                decided_at = stamp if terminal_event else row["decided_at"]
                connection.execute(
                    """UPDATE platform_growth_experiments SET status=?,decision_code=?,
                      source_watermark=coalesce(?,source_watermark),revision=revision+1,
                      updated_by_operator_id=?,updated_at=?,decided_at=?
                    WHERE growth_experiment_id=?""",
                    (
                        target,
                        decision,
                        command.source_watermark,
                        actor.operator_id,
                        stamp,
                        decided_at,
                        experiment_id,
                    ),
                )
        changed = _experiment_row(connection, experiment_id)
        if terminal_event:
            _event(
                connection,
                event_type="growth.experiment_decided",
                object_kind="experiment",
                object_id=experiment_id,
                revision=int(changed["revision"]),
                generation=1,
                payload={
                    "decision": changed["decision_code"],
                    "experiment_id": experiment_id,
                    "revision": int(changed["revision"]),
                    "schema_version": "growth-event-v1",
                    "source_digest": changed["canonical_sha256"],
                },
                now=now,
            )
        _record_command(
            connection,
            config,
            actor,
            action,
            idempotency_key,
            request_hash,
            command.expected_revision,
            "experiment",
            experiment_id,
            int(changed["revision"]),
            request_id,
            trace_id,
            now,
        )
        _audit(
            connection,
            actor,
            f"platform.growth_{action}",
            "growth_experiment",
            experiment_id,
            request_hash,
            request_id,
            trace_id,
            now,
        )
        response = ExperimentResponse(
            experiment=_experiment_summary(connection, changed),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_attribution(
    config: AppConfig,
    *,
    session_token: str,
    filters: dict[str, str],
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> AttributionResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token, None)
        scope = read_scope(actor)
        _require_schema(connection)
        normalized = _attribution_filters(filters, scope)
        filter_digest = _digest(normalized)
        boundary = _decode_cursor(config, actor, cursor, filter_digest, "attribution")
        view = normalized["view"]
        threshold = 50 if scope == "channel" else 20
        group_expression = {
            "summary": "attribution.acquisition_source_id",
            "campaign": "attribution.growth_campaign_id",
            "channel": "coalesce(attribution.channel_partner_id,'direct')",
            "channel_summary": "coalesce(attribution.channel_partner_id,'direct')",
        }[view]
        clauses = [
            "attribution.occurred_at>=?",
            "attribution.occurred_at<?",
            "attribution.formula_version=?",
        ]
        values: list[object] = [
            _day_start(normalized["from"]),
            _day_start(_day_after(normalized["to"])),
            normalized["formula_version"],
        ]
        for key, column in (
            ("stage", "attribution.conversion_stage"),
            ("source", "attribution.acquisition_source_id"),
            ("channel", "attribution.channel_partner_id"),
            ("campaign", "attribution.growth_campaign_id"),
        ):
            if key in normalized:
                clauses.append(f"{column}=?")
                values.append(normalized[key])
        if boundary is not None:
            clauses.append(f"{group_expression}>?")
            values.append(boundary[1])
        rows = connection.execute(
            f"""SELECT {group_expression} safe_id,
                    min(attribution.acquisition_source_id) acquisition_source_id,
                    min(attribution.growth_campaign_id) growth_campaign_id,
                    min(attribution.channel_partner_id) channel_partner_id,
                    min(attribution.conversion_stage) conversion_stage,
                    count(*) conversion_count,
                    count(DISTINCT attribution.subject_token_sha256) distinct_subject_count,
                    CASE WHEN count(DISTINCT attribution.currency_code)=1
                      THEN sum(attribution.value_minor) END value_minor,
                    CASE WHEN count(DISTINCT attribution.currency_code)=1
                      THEN min(attribution.currency_code) END currency_code,
                    max(attribution.occurred_at) source_watermark
                FROM platform_conversion_attributions attribution
                WHERE {' AND '.join(clauses)}
                GROUP BY {group_expression} ORDER BY safe_id LIMIT ?""",
            (*values, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                config,
                actor,
                filter_digest,
                "attribution",
                "",
                str(last["safe_id"]),
            )
        items = tuple(_attribution_summary(row, view, scope, threshold) for row in visible)
        response = AttributionResponse(
            view=view,
            items=items,
            limit=limit,
            has_more=has_more,
            next_cursor=next_cursor,
            source_freshness=_attribution_freshness(visible),
            generated_at=_db_time(_utcnow()),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def request_export(
    config: AppConfig,
    manifest: GrowthExportRequest,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> GrowthExportResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(connection, config, session_token, csrf_token)
        require(actor, EXPORT_REQUEST)
        _require_schema(connection)
        _validate_export_scope(actor, manifest)
        now = _utcnow()
        action = "export.request"
        request_hash = _digest(manifest.model_dump(mode="json"))
        replay = _command_replay(connection, config, actor, action, idempotency_key, request_hash)
        if replay is not None:
            row = _export_row(connection, replay[0])
            response = GrowthExportResponse(
                export=_export_summary(row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code="platform.growth.export.request",
            resource_type="growth_export",
            resource_id="new",
            now=now,
        )
        export_id = _id("gxp_")
        stamp = _db_time(now)
        query_json = json.dumps(
            manifest.model_dump(mode="json", exclude={"confirmed"}),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        status = "approved" if manifest.sensitivity == "aggregate" else "policy_review"
        permission_hash = _digest(actor.permissions)
        connection.execute(
            """INSERT INTO platform_growth_exports(
              growth_export_id,requester_operator_id,requester_revision,
              requester_permission_sha256,query_kind,query_manifest_json,query_sha256,
              schema_version,formula_version,sensitivity,status,revision,row_count,cell_count,
              artifact_sha256,artifact_key_reference,expires_at,download_count,generation,
              created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'platform-growth-export-v1',?,?,?,1,NULL,NULL,
              NULL,NULL,NULL,0,1,?,?)""",
            (
                export_id,
                actor.operator_id,
                actor.operator_revision,
                permission_hash,
                manifest.query_kind,
                query_json,
                hashlib.sha256(query_json.encode("ascii")).hexdigest(),
                manifest.formula_version,
                manifest.sensitivity,
                status,
                stamp,
                stamp,
            ),
        )
        _record_command(
            connection,
            config,
            actor,
            action,
            idempotency_key,
            request_hash,
            None,
            "export",
            export_id,
            1,
            request_id,
            trace_id,
            now,
        )
        _audit(
            connection,
            actor,
            "platform.growth_export_requested",
            "growth_export",
            export_id,
            request_hash,
            request_id,
            trace_id,
            now,
        )
        response = GrowthExportResponse(
            export=_export_summary(_export_row(connection, export_id)),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def get_export(
    config: AppConfig,
    *,
    session_token: str,
    export_id: str,
    request_id: str,
    trace_id: str,
) -> GrowthExportResponse:
    _validate_id(_EXPORT_ID, export_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token, None)
        _require_schema(connection)
        row = _export_row(connection, export_id)
        if (
            row["requester_operator_id"] != actor.operator_id
            and EXPORT_APPROVE not in actor.permissions
        ):
            raise ApiError("platform.permission_denied")
        response = GrowthExportResponse(
            export=_export_summary(row), request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def approve_export(
    config: AppConfig,
    command: GrowthExportApprovalCommand,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    export_id: str,
    request_id: str,
    trace_id: str,
) -> GrowthExportResponse:
    _validate_id(_EXPORT_ID, export_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(connection, config, session_token, csrf_token)
        require(actor, EXPORT_APPROVE)
        _require_schema(connection)
        now = _utcnow()
        action = f"export.{command.operation}"
        request_hash = _digest({"export_id": export_id, **command.model_dump(mode="json")})
        replay = _command_replay(connection, config, actor, action, idempotency_key, request_hash)
        if replay is not None:
            response = GrowthExportResponse(
                export=_export_summary(_export_row(connection, export_id)),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        row = _export_row(connection, export_id)
        if (
            int(row["revision"]) != command.expected_revision
            or row["status"] != "policy_review"
            or row["requester_operator_id"] == actor.operator_id
        ):
            raise ApiError("platform.growth_state_conflict")
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code=f"platform.growth.export.{command.operation}",
            resource_type="growth_export",
            resource_id=export_id,
            now=now,
        )
        stamp = _next_stamp(str(row["updated_at"]), now)
        decision = "approved" if command.operation == "approve" else "rejected"
        generation = int(
            connection.execute(
                """SELECT coalesce(max(generation),0)+1
                    FROM platform_growth_export_approvals WHERE growth_export_id=?""",
                (export_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO platform_growth_export_approvals(
              growth_export_approval_id,growth_export_id,generation,decision,
              approver_operator_id,approver_revision,permission_sha256,step_up_generation,
              request_sha256,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                _id("gxa_"),
                export_id,
                generation,
                decision,
                actor.operator_id,
                actor.operator_revision,
                _digest(actor.permissions),
                generation,
                request_hash,
                stamp,
            ),
        )
        connection.execute(
            """UPDATE platform_growth_exports SET status=?,revision=revision+1,
              updated_at=? WHERE growth_export_id=?""",
            (decision, stamp, export_id),
        )
        changed = _export_row(connection, export_id)
        _record_command(
            connection,
            config,
            actor,
            action,
            idempotency_key,
            request_hash,
            command.expected_revision,
            "export",
            export_id,
            int(changed["revision"]),
            request_id,
            trace_id,
            now,
        )
        _audit(
            connection,
            actor,
            f"platform.growth_export_{command.operation}d",
            "growth_export",
            export_id,
            request_hash,
            request_id,
            trace_id,
            now,
        )
        response = GrowthExportResponse(
            export=_export_summary(changed), request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def issue_export_receipt(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    export_id: str,
    request_id: str,
    trace_id: str,
) -> GrowthExportReceiptResponse:
    _validate_id(_EXPORT_ID, export_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(connection, config, session_token, csrf_token)
        require(actor, EXPORT_DOWNLOAD)
        _require_schema(connection)
        now = _utcnow()
        row = _export_row(connection, export_id)
        if row["requester_operator_id"] != actor.operator_id:
            raise ApiError("platform.permission_denied")
        if (
            row["status"] != "ready"
            or int(row["download_count"]) != 0
            or row["expires_at"] is None
            or _parse_time(str(row["expires_at"])) <= now
            or not hmac.compare_digest(
                str(row["requester_permission_sha256"]), _digest(actor.permissions)
            )
        ):
            raise ApiError("platform.growth_export_unavailable")
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code="platform.growth.export.receipt",
            resource_type="growth_export",
            resource_id=export_id,
            now=now,
        )
        connection.execute(
            """UPDATE platform_growth_export_receipts SET status='expired'
                WHERE growth_export_id=? AND status='active' AND expires_at<=?""",
            (export_id, _db_time(now)),
        )
        if connection.execute(
            """SELECT 1 FROM platform_growth_export_receipts
                WHERE growth_export_id=? AND status='active'""",
            (export_id,),
        ).fetchone():
            raise ApiError("platform.growth_state_conflict")
        raw_receipt = secrets.token_urlsafe(48)
        generation = int(
            connection.execute(
                """SELECT coalesce(max(generation),0)+1
                    FROM platform_growth_export_receipts WHERE growth_export_id=?""",
                (export_id,),
            ).fetchone()[0]
        )
        expires = now + timedelta(minutes=5)
        connection.execute(
            """INSERT INTO platform_growth_export_receipts(
              growth_export_receipt_id,growth_export_id,requester_operator_id,receipt_hmac,
              generation,status,expires_at,created_at,consumed_at)
            VALUES(?,?,?,?,?,'active',?,?,NULL)""",
            (
                _id("gxr_"),
                export_id,
                actor.operator_id,
                _receipt_hmac(config, raw_receipt),
                generation,
                _db_time(expires),
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor,
            "platform.growth_export_receipt_issued",
            "growth_export",
            export_id,
            _digest({"generation": generation}),
            request_id,
            trace_id,
            now,
        )
        response = GrowthExportReceiptResponse(
            export_id=export_id,
            receipt=raw_receipt,
            expires_at=_db_time(expires),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def download_export(
    config: AppConfig,
    *,
    session_token: str,
    export_id: str,
    receipt: str,
    request_id: str,
    trace_id: str,
) -> tuple[bytes, str]:
    _validate_id(_EXPORT_ID, export_id)
    if not 32 <= len(receipt) <= 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", receipt):
        raise ApiError("platform.growth_export_unavailable")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(connection, config, session_token, None)
        require(actor, EXPORT_DOWNLOAD)
        _require_schema(connection)
        now = _utcnow()
        row = _export_row(connection, export_id)
        receipt_row = connection.execute(
            """SELECT * FROM platform_growth_export_receipts
                WHERE growth_export_id=? AND receipt_hmac=?""",
            (export_id, _receipt_hmac(config, receipt)),
        ).fetchone()
        if (
            receipt_row is None
            or receipt_row["status"] != "active"
            or receipt_row["requester_operator_id"] != actor.operator_id
            or _parse_time(str(receipt_row["expires_at"])) <= now
            or row["requester_operator_id"] != actor.operator_id
            or row["status"] != "ready"
            or int(row["download_count"]) != 0
            or row["expires_at"] is None
            or _parse_time(str(row["expires_at"])) <= now
            or not hmac.compare_digest(
                str(row["requester_permission_sha256"]), _digest(actor.permissions)
            )
            or row["artifact_key_reference"] is None
            or row["artifact_sha256"] is None
        ):
            raise ApiError("platform.growth_export_unavailable")
        content = load_artifact(
            config,
            reference=str(row["artifact_key_reference"]),
            expected_sha256=str(row["artifact_sha256"]),
        )
        stamp = _db_time(now)
        connection.execute(
            """UPDATE platform_growth_export_receipts SET status='consumed',consumed_at=?
                WHERE growth_export_receipt_id=? AND status='active'""",
            (stamp, receipt_row["growth_export_receipt_id"]),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("platform.growth_export_unavailable")
        connection.execute(
            """UPDATE platform_growth_exports SET status='downloaded',download_count=1,
              revision=revision+1,updated_at=? WHERE growth_export_id=? AND status='ready'
              AND download_count=0""",
            (stamp, export_id),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("platform.growth_export_unavailable")
        changed = _export_row(connection, export_id)
        _event(
            connection,
            event_type="growth.exported",
            object_kind="export",
            object_id=export_id,
            revision=int(changed["revision"]),
            generation=1,
            payload={
                "export_id": export_id,
                "revision": int(changed["revision"]),
                "row_count": int(changed["row_count"]),
                "schema_version": "growth-event-v1",
                "status": "downloaded",
            },
            now=now,
        )
        _audit(
            connection,
            actor,
            "platform.growth_export_downloaded",
            "growth_export",
            export_id,
            str(row["artifact_sha256"]),
            request_id,
            trace_id,
            now,
        )
        connection.commit()
        return content, f"metahub-growth-{export_id}.csv"
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def build_next_export(
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> bool:
    current = _utc(now)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        row = connection.execute(
            """SELECT * FROM platform_growth_exports WHERE status='approved'
                ORDER BY created_at,growth_export_id LIMIT 1"""
        ).fetchone()
        if row is None:
            connection.commit()
            return False
        export_id = str(row["growth_export_id"])
        stamp = _next_stamp(str(row["updated_at"]), current)
        connection.execute(
            """UPDATE platform_growth_exports SET status='building',revision=revision+1,
              generation=generation+1,updated_at=? WHERE growth_export_id=? AND status='approved'
              AND revision=?""",
            (stamp, export_id, int(row["revision"])),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return False
        building = _export_row(connection, export_id)
        try:
            content, row_count, cell_count = _render_export(connection, building)
            _scan_artifact(content)
            reference, digest = store_artifact(
                config,
                export_id=export_id,
                generation=int(building["generation"]),
                plaintext=content,
            )
            ready_stamp = _next_stamp(stamp, current + timedelta(milliseconds=1))
            connection.execute(
                """UPDATE platform_growth_exports SET status='ready',row_count=?,cell_count=?,
                  artifact_sha256=?,artifact_key_reference=?,expires_at=?,revision=revision+1,
                  updated_at=? WHERE growth_export_id=? AND status='building' AND generation=?""",
                (
                    row_count,
                    cell_count,
                    digest,
                    reference,
                    _db_time(current + timedelta(hours=24)),
                    ready_stamp,
                    export_id,
                    int(building["generation"]),
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ApiError("platform.growth_export_unavailable")
        except ApiError:
            connection.execute(
                """UPDATE platform_growth_exports SET status='failed',revision=revision+1,
                  updated_at=? WHERE growth_export_id=? AND status='building'""",
                (_next_stamp(stamp, current + timedelta(milliseconds=1)), export_id),
            )
            connection.commit()
            return False
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.growth_export_unavailable") from None
    finally:
        connection.close()


def _funnel_filters(filters: dict[str, str], scope: str) -> dict[str, str]:
    today = _utcnow().date()
    normalized = {
        "view": filters.get("view", "overview"),
        "from": filters.get("from", (today - timedelta(days=29)).isoformat()),
        "to": filters.get("to", today.isoformat()),
        "bucket": filters.get("bucket", "day"),
        "dimension": filters.get("dimension", "none"),
        "formula_version": filters.get("formula_version", "growth-funnel-v2"),
    }
    _date_range(normalized["from"], normalized["to"], 366)
    if normalized["view"] not in {"overview", "adoption", "retention"}:
        raise ApiError("platform.request_invalid")
    if normalized["bucket"] not in {"day", "week", "month"}:
        raise ApiError("platform.request_invalid")
    if normalized["dimension"] not in {"none", "source", "channel", "region", "plan_band"}:
        raise ApiError("platform.request_invalid")
    if normalized["formula_version"] != "growth-funnel-v2":
        raise ApiError("platform.request_invalid")
    dimension_value = filters.get("dimension_value")
    if (normalized["dimension"] == "none") != (dimension_value is None):
        raise ApiError("platform.request_invalid")
    if dimension_value is not None:
        if not 1 <= len(dimension_value) <= 80 or "\x00" in dimension_value:
            raise ApiError("platform.request_invalid")
        normalized["dimension_value"] = dimension_value
    if scope == "channel":
        raise ApiError("platform.permission_denied")
    if scope == "customer_success" and (
        normalized["view"] not in {"adoption", "retention"}
        or normalized["dimension"] in {"source", "channel"}
    ):
        raise ApiError("platform.permission_denied")
    return normalized


def _cohort_filters(filters: dict[str, str], scope: str) -> dict[str, str]:
    today = _utcnow().date()
    normalized = {
        "view": filters.get("view", "retention"),
        "cohort_kind": filters.get("cohort_kind", "signup_week"),
        "from": filters.get("from", (today - timedelta(days=364)).isoformat()),
        "to": filters.get("to", today.isoformat()),
        "age_buckets": filters.get("age_buckets", "12"),
        "dimension": filters.get("dimension", "none"),
        "formula_version": filters.get("formula_version", "growth-cohort-v2"),
    }
    _date_range(normalized["from"], normalized["to"], 730)
    if normalized["view"] not in {"adoption", "retention"}:
        raise ApiError("platform.request_invalid")
    if normalized["cohort_kind"] not in {
        "signup_week",
        "activation_week",
        "first_paid_month",
        "campaign",
    }:
        raise ApiError("platform.request_invalid")
    if normalized["dimension"] not in {"none", "source", "channel", "region", "plan_band"}:
        raise ApiError("platform.request_invalid")
    if normalized["formula_version"] != "growth-cohort-v2":
        raise ApiError("platform.request_invalid")
    try:
        age_buckets = int(normalized["age_buckets"])
    except ValueError:
        raise ApiError("platform.request_invalid") from None
    if str(age_buckets) != normalized["age_buckets"] or not 1 <= age_buckets <= 24:
        raise ApiError("platform.request_invalid")
    dimension_value = filters.get("dimension_value")
    if (normalized["dimension"] == "none") != (dimension_value is None):
        raise ApiError("platform.request_invalid")
    if dimension_value is not None:
        if not 1 <= len(dimension_value) <= 80 or "\x00" in dimension_value:
            raise ApiError("platform.request_invalid")
        normalized["dimension_value"] = dimension_value
    if scope == "channel":
        raise ApiError("platform.permission_denied")
    if scope == "customer_success" and normalized["dimension"] in {"source", "channel"}:
        raise ApiError("platform.permission_denied")
    return normalized


def _campaign_filters(filters: dict[str, str]) -> dict[str, str]:
    allowed_status = {"draft", "scheduled", "active", "paused", "completed", "archived"}
    normalized: dict[str, str] = {}
    if "status" in filters:
        if filters["status"] not in allowed_status:
            raise ApiError("platform.request_invalid")
        normalized["status"] = filters["status"]
    if "source" in filters:
        _validate_id(_SOURCE_ID, filters["source"])
        normalized["source"] = filters["source"]
    if "channel" in filters:
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}", filters["channel"]) is None:
            raise ApiError("platform.request_invalid")
        normalized["channel"] = filters["channel"]
    _optional_range(filters, normalized, 730)
    return normalized


def _experiment_filters(filters: dict[str, str]) -> dict[str, str]:
    allowed_status = {
        "draft",
        "scheduled",
        "running",
        "paused",
        "decided",
        "stopped",
        "canceled",
        "archived",
    }
    normalized: dict[str, str] = {}
    if "status" in filters:
        if filters["status"] not in allowed_status:
            raise ApiError("platform.request_invalid")
        normalized["status"] = filters["status"]
    for key in ("surface", "metric"):
        if key in filters:
            if re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,79}", filters[key]) is None:
                raise ApiError("platform.request_invalid")
            normalized[key] = filters[key]
    _optional_range(filters, normalized, 730)
    return normalized


def _attribution_filters(filters: dict[str, str], scope: str) -> dict[str, str]:
    today = _utcnow().date()
    normalized = {
        "view": filters.get("view", "summary"),
        "from": filters.get("from", (today - timedelta(days=29)).isoformat()),
        "to": filters.get("to", today.isoformat()),
        "formula_version": filters.get("formula_version", "last-eligible-touch-30d-v1"),
    }
    _date_range(normalized["from"], normalized["to"], 366)
    if normalized["formula_version"] != "last-eligible-touch-30d-v1":
        raise ApiError("platform.request_invalid")
    if scope == "customer_success":
        raise ApiError("platform.permission_denied")
    if scope == "channel":
        if normalized["view"] != "channel_summary" or any(
            key in filters for key in ("campaign", "source")
        ):
            raise ApiError("platform.permission_denied")
    elif normalized["view"] not in {"summary", "campaign", "channel"}:
        raise ApiError("platform.request_invalid")
    if "stage" in filters:
        if filters["stage"] not in {
            "registered",
            "activated",
            "trial_started",
            "paid",
            "retained_30d",
        }:
            raise ApiError("platform.request_invalid")
        normalized["stage"] = filters["stage"]
    for key, pattern in (
        ("source", _SOURCE_ID),
        ("campaign", _CAMPAIGN_ID),
        ("channel", re.compile(r"^par_[0-9a-f]{32}$")),
    ):
        if key in filters:
            _validate_id(pattern, filters[key])
            normalized[key] = filters[key]
    return normalized


def _funnel_summary(row: sqlite3.Row, scope: str) -> FunnelBucket:
    suppressed = bool(row["suppressed"]) or (
        scope == "customer_success" and int(row["distinct_subject_count"]) < 50
    )
    code = str(row["suppression_code"] or "k_anonymity") if suppressed else None
    counts = (
        ("registered", "registered_count", None),
        ("activated", "activated_count", "registered_to_activated_bps"),
        ("trial_started", "trial_started_count", "activated_to_trial_bps"),
        ("paid", "paid_count", "trial_to_paid_bps"),
        ("retained_30d", "retained_30d_count", "paid_to_retained_bps"),
    )
    stages = tuple(
        FunnelStage(
            code=stage,
            subject_count=None if suppressed else int(row[count_column]),
            event_count=None if suppressed else int(row[count_column]),
            conversion_from_previous_bps=(
                None if suppressed or bps_column is None or row[bps_column] is None
                else int(row[bps_column])
            ),
        )
        for stage, count_column, bps_column in counts
    )
    return FunnelBucket(
        snapshot_id=row["growth_funnel_snapshot_id"],
        bucket_start=row["bucket_start"],
        bucket=row["bucket_kind"],
        view=row["view_code"],
        dimension=row["dimension_code"],
        dimension_value=None if row["dimension_code"] == "none" else row["dimension_value"],
        stages=stages,
        coverage_bps=int(row["coverage_bps"]),
        suppressed=suppressed,
        suppression_code=code,
        currency=None if suppressed else row["currency_code"],
        revenue_minor=None if suppressed else row["revenue_minor"],
        cost_minor=None if suppressed else row["cost_minor"],
        contribution_minor=(
            None
            if suppressed or row["revenue_minor"] is None or row["cost_minor"] is None
            else int(row["revenue_minor"]) - int(row["cost_minor"])
        ),
        generation=int(row["generation"]),
        source_watermark=row["source_watermark"],
    )


def _cohort_summary(row: sqlite3.Row, scope: str) -> CohortBucket:
    suppressed = bool(row["suppressed"]) or (
        scope == "customer_success" and int(row["distinct_subject_count"]) < 50
    )
    return CohortBucket(
        snapshot_id=row["growth_cohort_snapshot_id"],
        cohort_kind=row["cohort_kind"],
        cohort_bucket=row["cohort_bucket"],
        age_bucket=int(row["age_bucket"]),
        view=row["view_code"],
        dimension=row["dimension_code"],
        dimension_value=None if row["dimension_code"] == "none" else row["dimension_value"],
        eligible_count=None if suppressed else int(row["eligible_count"]),
        retained_count=None if suppressed else int(row["retained_count"]),
        churned_count=None if suppressed else int(row["churned_count"]),
        retention_bps=(
            None
            if suppressed or row["retention_bps"] is None
            else int(row["retention_bps"])
        ),
        coverage_bps=int(row["coverage_bps"]),
        suppressed=suppressed,
        suppression_code=str(row["suppression_code"] or "k_anonymity") if suppressed else None,
        generation=int(row["generation"]),
        supersedes_snapshot_id=row["supersedes_snapshot_id"],
        source_watermark=row["source_watermark"],
    )


def _campaign_summary(connection: sqlite3.Connection, row: sqlite3.Row) -> CampaignSummary:
    conversion_count = int(
        connection.execute(
            """SELECT count(*) FROM platform_conversion_attributions
                WHERE growth_campaign_id=?""",
            (row["growth_campaign_id"],),
        ).fetchone()[0]
    )
    return CampaignSummary(
        campaign_id=row["growth_campaign_id"],
        display_name=row["display_name"],
        acquisition_source_id=row["acquisition_source_id"],
        channel_code=row["channel_code"],
        target_stage=row["target_stage"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        budget_minor=int(row["budget_minor"]),
        currency=row["currency_code"],
        status=row["status"],
        revision=int(row["revision"]),
        conversion_count=conversion_count,
        spend_minor=0,
        outcome_bps=None,
        action_capabilities=_campaign_actions(str(row["status"])),
        updated_at=row["updated_at"],
    )


def _experiment_summary(connection: sqlite3.Connection, row: sqlite3.Row) -> ExperimentSummary:
    counts = connection.execute(
        """SELECT count(*) assigned,
            count(CASE WHEN variant_code=? THEN 1 END) control_count,
            count(CASE WHEN variant_code=? THEN 1 END) variant_count
          FROM platform_growth_experiment_assignments WHERE growth_experiment_id=?""",
        (row["control_code"], row["variant_code"], row["growth_experiment_id"]),
    ).fetchone()
    return ExperimentSummary(
        experiment_id=row["growth_experiment_id"],
        hypothesis_code=row["hypothesis_code"],
        surface_code=row["surface_code"],
        primary_metric=row["primary_metric_code"],
        control_code=row["control_code"],
        variant_code=row["variant_code"],
        control_allocation_bps=int(row["control_allocation_bps"]),
        variant_allocation_bps=int(row["variant_allocation_bps"]),
        eligibility_policy_version=row["eligibility_policy_version"],
        consent_version=row["consent_version"],
        sample_cap=int(row["sample_cap"]),
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        guardrail_code=row["guardrail_code"],
        decision_policy_version=row["decision_policy_version"],
        status=row["status"],
        decision=row["decision_code"],
        assigned_count=int(counts["assigned"]),
        control_count=int(counts["control_count"]),
        variant_count=int(counts["variant_count"]),
        revision=int(row["revision"]),
        action_capabilities=_experiment_actions(str(row["status"])),
        updated_at=row["updated_at"],
    )


def _attribution_summary(
    row: sqlite3.Row,
    view: str,
    scope: str,
    threshold: int,
) -> AttributionSummary:
    suppressed = int(row["distinct_subject_count"]) < threshold
    channel_only = scope == "channel"
    return AttributionSummary(
        safe_id=str(row["safe_id"]),
        source_id=(
            None
            if channel_only or view not in {"summary", "campaign"}
            else row["acquisition_source_id"]
        ),
        campaign_id=None if channel_only or view != "campaign" else row["growth_campaign_id"],
        channel_partner_id=(
            row["channel_partner_id"] if view in {"channel", "channel_summary"} else None
        ),
        conversion_stage=row["conversion_stage"],
        conversion_count=None if suppressed else int(row["conversion_count"]),
        value_minor=None if suppressed or channel_only else row["value_minor"],
        cost_minor=None,
        currency=None if suppressed or channel_only else row["currency_code"],
        conversion_bps=None,
        coverage_bps=10_000,
        suppressed=suppressed,
        suppression_code="k_anonymity" if suppressed else None,
        source_watermark=row["source_watermark"],
    )


def _export_summary(row: sqlite3.Row) -> GrowthExportSummary:
    return GrowthExportSummary(
        export_id=row["growth_export_id"],
        query_kind=row["query_kind"],
        sensitivity=row["sensitivity"],
        status=row["status"],
        revision=int(row["revision"]),
        row_count=None if row["row_count"] is None else int(row["row_count"]),
        cell_count=None if row["cell_count"] is None else int(row["cell_count"]),
        expires_at=row["expires_at"],
        download_count=int(row["download_count"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _render_export(connection: sqlite3.Connection, export: sqlite3.Row) -> tuple[bytes, int, int]:
    manifest = json.loads(str(export["query_manifest_json"]))
    canonical = json.dumps(
        manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if hashlib.sha256(canonical).hexdigest() != export["query_sha256"]:
        raise ApiError("platform.growth_export_unavailable")
    query_kind = str(export["query_kind"])
    start = manifest["from_date"]
    end = manifest["to_date"]
    if query_kind == "funnels":
        headers = (
            "bucket_start",
            "view",
            "dimension",
            "dimension_value",
            "registered_count",
            "activated_count",
            "trial_started_count",
            "paid_count",
            "retained_30d_count",
            "coverage_bps",
            "suppressed",
            "generation",
        )
        source_rows = connection.execute(
            """SELECT * FROM platform_growth_funnel_snapshots
                WHERE bucket_start>=? AND bucket_start<=? AND view_code=?
                  AND bucket_kind=? AND dimension_code=? AND formula_version=?
                ORDER BY bucket_start,growth_funnel_snapshot_id""",
            (
                start,
                end,
                manifest["view"],
                manifest["bucket"],
                manifest["dimension"],
                manifest["formula_version"],
            ),
        ).fetchall()
        rows = [
            (
                row["bucket_start"],
                row["view_code"],
                row["dimension_code"],
                "" if row["dimension_code"] == "none" else row["dimension_value"],
                "" if row["suppressed"] else row["registered_count"],
                "" if row["suppressed"] else row["activated_count"],
                "" if row["suppressed"] else row["trial_started_count"],
                "" if row["suppressed"] else row["paid_count"],
                "" if row["suppressed"] else row["retained_30d_count"],
                row["coverage_bps"],
                row["suppressed"],
                row["generation"],
            )
            for row in source_rows
        ]
    elif query_kind == "cohorts":
        headers = (
            "cohort_kind",
            "cohort_bucket",
            "age_bucket",
            "view",
            "dimension",
            "eligible_count",
            "retained_count",
            "churned_count",
            "retention_bps",
            "coverage_bps",
            "suppressed",
            "generation",
        )
        source_rows = connection.execute(
            """SELECT * FROM platform_growth_cohort_snapshots
                WHERE cohort_bucket>=? AND cohort_bucket<=? AND view_code=?
                  AND dimension_code=? AND formula_version=?
                ORDER BY cohort_bucket,age_bucket,growth_cohort_snapshot_id""",
            (
                start,
                end,
                manifest["view"],
                manifest["dimension"],
                manifest["formula_version"],
            ),
        ).fetchall()
        rows = [
            (
                row["cohort_kind"],
                row["cohort_bucket"],
                row["age_bucket"],
                row["view_code"],
                row["dimension_code"],
                "" if row["suppressed"] else row["eligible_count"],
                "" if row["suppressed"] else row["retained_count"],
                "" if row["suppressed"] else row["churned_count"],
                "" if row["suppressed"] else row["retention_bps"],
                row["coverage_bps"],
                row["suppressed"],
                row["generation"],
            )
            for row in source_rows
        ]
    elif query_kind == "campaigns":
        headers = (
            "campaign_id",
            "source_id",
            "channel_code",
            "target_stage",
            "status",
            "revision",
            "starts_at",
            "ends_at",
        )
        source_rows = connection.execute(
            """SELECT * FROM platform_growth_campaigns
                WHERE starts_at>=? AND starts_at<? ORDER BY growth_campaign_id""",
            (_day_start(start), _day_start(_day_after(end))),
        ).fetchall()
        rows = [
            (
                row["growth_campaign_id"],
                row["acquisition_source_id"],
                row["channel_code"],
                row["target_stage"],
                row["status"],
                row["revision"],
                row["starts_at"],
                row["ends_at"],
            )
            for row in source_rows
        ]
    elif query_kind == "experiments":
        headers = (
            "experiment_id",
            "surface_code",
            "primary_metric",
            "status",
            "decision",
            "revision",
            "starts_at",
            "ends_at",
        )
        source_rows = connection.execute(
            """SELECT * FROM platform_growth_experiments
                WHERE starts_at>=? AND starts_at<? ORDER BY growth_experiment_id""",
            (_day_start(start), _day_start(_day_after(end))),
        ).fetchall()
        rows = [
            (
                row["growth_experiment_id"],
                row["surface_code"],
                row["primary_metric_code"],
                row["status"],
                row["decision_code"] or "",
                row["revision"],
                row["starts_at"],
                row["ends_at"],
            )
            for row in source_rows
        ]
    elif query_kind == "attribution":
        headers = (
            "source_id",
            "campaign_id",
            "channel_partner_id",
            "conversion_stage",
            "conversion_count",
            "currency",
            "value_minor",
        )
        source_rows = connection.execute(
            """SELECT acquisition_source_id,growth_campaign_id,channel_partner_id,
                    conversion_stage,count(*) conversion_count,
                    CASE WHEN count(DISTINCT currency_code)=1 THEN min(currency_code) END currency,
                    CASE WHEN count(DISTINCT currency_code)=1 THEN sum(value_minor) END value_minor
                FROM platform_conversion_attributions
                WHERE occurred_at>=? AND occurred_at<? AND formula_version=?
                GROUP BY acquisition_source_id,growth_campaign_id,
                  channel_partner_id,conversion_stage
                ORDER BY acquisition_source_id,growth_campaign_id,channel_partner_id""",
            (
                _day_start(start),
                _day_start(_day_after(end)),
                manifest["formula_version"],
            ),
        ).fetchall()
        rows = [tuple(row[column] or "" for column in headers) for row in source_rows]
    else:
        raise ApiError("platform.growth_export_unavailable")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    content = output.getvalue().encode("utf-8")
    return content, len(rows), len(rows) * len(headers)


def _scan_artifact(content: bytes) -> None:
    try:
        text = content.decode("utf-8").lower()
    except UnicodeError:
        raise ApiError("platform.growth_export_unavailable") from None
    forbidden = (
        "authorization",
        "cookie",
        "receipt",
        "subject_token",
        "workspace_id",
        "account_id",
        "email",
        "phone",
        "prompt",
        "response",
        "utm_",
        "http://",
        "https://",
    )
    if any(value in text for value in forbidden) or re.search(
        r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", text
    ):
        raise ApiError("platform.growth_export_unavailable")


def _validate_export_scope(actor: PlatformContext, manifest: GrowthExportRequest) -> None:
    scope = read_scope(actor)
    if scope == "channel":
        if manifest.query_kind != "attribution" or manifest.view != "channel_summary":
            raise ApiError("platform.permission_denied")
    elif scope == "customer_success":
        if manifest.query_kind not in {"funnels", "cohorts"} or manifest.view not in {
            "adoption",
            "retention",
        }:
            raise ApiError("platform.permission_denied")
    if manifest.sensitivity == "high_cardinality" and scope != "growth":
        raise ApiError("platform.permission_denied")
    _date_range(manifest.from_date, manifest.to_date, 730)


def _campaign_config(command: CampaignCommand) -> dict[str, object]:
    return {
        "acquisition_source_id": command.acquisition_source_id,
        "budget_minor": command.budget_minor,
        "channel_code": command.channel_code,
        "currency": command.currency,
        "display_name": command.display_name,
        "ends_at": command.ends_at,
        "starts_at": command.starts_at,
        "target_stage": command.target_stage,
    }


def _experiment_config(command: ExperimentCommand) -> dict[str, object]:
    return {
        "consent_version": command.consent_version,
        "control_allocation_bps": command.control_allocation_bps,
        "control_code": command.control_code,
        "decision_policy_version": command.decision_policy_version,
        "eligibility_policy_version": command.eligibility_policy_version,
        "ends_at": command.ends_at,
        "guardrail_code": command.guardrail_code,
        "hypothesis_code": command.hypothesis_code,
        "primary_metric": command.primary_metric,
        "sample_cap": command.sample_cap,
        "starts_at": command.starts_at,
        "surface_code": command.surface_code,
        "variant_allocation_bps": command.variant_allocation_bps,
        "variant_code": command.variant_code,
    }


def _validate_experiment(command: ExperimentCommand) -> None:
    if (
        command.control_code == command.variant_code
        or command.control_allocation_bps is None
        or command.variant_allocation_bps is None
        or command.control_allocation_bps + command.variant_allocation_bps != 10_000
    ):
        raise ApiError("platform.request_invalid")
    _validate_window(str(command.starts_at), str(command.ends_at))


def _require_source(connection: sqlite3.Connection, source_id: str) -> None:
    _validate_id(_SOURCE_ID, source_id)
    row = connection.execute(
        """SELECT verification_status FROM platform_acquisition_sources
            WHERE acquisition_source_id=?""",
        (source_id,),
    ).fetchone()
    if row is None or row["verification_status"] != "verified":
        raise ApiError("platform.request_invalid")


def _require_source_watermark(connection: sqlite3.Connection, expected: str | None) -> None:
    if expected is None:
        raise ApiError("platform.request_invalid")
    row = connection.execute(
        """SELECT source_watermark FROM platform_growth_migration_cursors
            WHERE source_code='product_events'"""
    ).fetchone()
    current = None if row is None else row["source_watermark"]
    if current != expected:
        raise ApiError("platform.growth_state_conflict")


def _campaign_actions(status: str) -> tuple[str, ...]:
    return {
        "draft": ("update", "schedule"),
        "scheduled": ("update",),
        "active": ("pause", "complete"),
        "paused": ("resume", "complete"),
        "completed": ("archive",),
        "archived": (),
    }[status]


def _experiment_actions(status: str) -> tuple[str, ...]:
    return {
        "draft": ("update", "schedule"),
        "scheduled": ("update", "start"),
        "running": ("pause", "stop", "decide"),
        "paused": ("resume", "stop", "decide"),
        "decided": ("archive",),
        "stopped": ("archive",),
        "canceled": ("archive",),
        "archived": (),
    }[status]


def _campaign_row(connection: sqlite3.Connection, campaign_id: str) -> sqlite3.Row:
    _validate_id(_CAMPAIGN_ID, campaign_id)
    row = connection.execute(
        "SELECT * FROM platform_growth_campaigns WHERE growth_campaign_id=?",
        (campaign_id,),
    ).fetchone()
    if row is None:
        raise ApiError("platform.growth_not_found")
    return row


def _experiment_row(connection: sqlite3.Connection, experiment_id: str) -> sqlite3.Row:
    _validate_id(_EXPERIMENT_ID, experiment_id)
    row = connection.execute(
        "SELECT * FROM platform_growth_experiments WHERE growth_experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        raise ApiError("platform.growth_not_found")
    return row


def _export_row(connection: sqlite3.Connection, export_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM platform_growth_exports WHERE growth_export_id=?",
        (export_id,),
    ).fetchone()
    if row is None:
        raise ApiError("platform.growth_not_found")
    return row


def _event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    object_kind: str,
    object_id: str,
    revision: int,
    generation: int,
    payload: dict[str, object],
    now: datetime,
) -> str:
    allowed = {
        "growth.conversion_recorded": "conversion",
        "growth.campaign_changed": "campaign",
        "growth.experiment_decided": "experiment",
        "growth.exported": "export",
    }
    if allowed.get(event_type) != object_kind:
        raise ApiError("platform.unavailable")
    payload_json = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    if len(payload_json.encode("ascii")) > 2048:
        raise ApiError("platform.unavailable")
    event_id = _id("gev_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO platform_growth_events(
          growth_event_id,event_type,object_kind,object_id,object_revision,generation,
          payload_json,payload_sha256,occurred_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            event_type,
            object_kind,
            object_id,
            revision,
            generation,
            payload_json,
            hashlib.sha256(payload_json.encode("ascii")).hexdigest(),
            stamp,
        ),
    )
    stable_event_id = hashlib.sha256(
        f"prd141\0{event_type}\0{object_id}\0{generation}".encode("ascii")
    ).hexdigest()
    connection.execute(
        """INSERT INTO platform_growth_event_outbox(
          growth_event_outbox_id,growth_event_id,stable_event_id,status,attempt_count,
          generation,lease_owner_id,lease_token_sha256,lease_expires_at,next_attempt_at,
          subscription_snapshot_json,snapshot_frozen_at,delivered_at,last_error_code,
          created_at,updated_at)
        VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,?,NULL,NULL,NULL,NULL,?,?)""",
        (_id("gevo_"), event_id, stable_event_id, stamp, stamp, stamp),
    )
    return event_id


def _record_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    action: str,
    key: str,
    request_hash: str,
    expected_revision: int | None,
    result_kind: str,
    result_id: str,
    result_revision: int,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO platform_growth_commands(
          growth_command_id,operator_id,operator_revision,action_code,object_kind,object_id,
          idempotency_key_hmac,request_sha256,expected_revision,result_revision,status,
          result_code,request_id,trace_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,'committed','completed',?,?,?)""",
        (
            _id("gcm_"),
            actor.operator_id,
            actor.operator_revision,
            action,
            result_kind,
            result_id,
            _idempotency_hmac(config, key),
            request_hash,
            expected_revision,
            result_revision,
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _command_replay(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    action: str,
    key: str,
    request_hash: str,
) -> tuple[str, int] | None:
    row = connection.execute(
        """SELECT object_id,result_revision,request_sha256,status
            FROM platform_growth_commands
            WHERE operator_id=? AND action_code=? AND idempotency_key_hmac=?""",
        (actor.operator_id, action, _idempotency_hmac(config, key)),
    ).fetchone()
    if row is None:
        return None
    if row["status"] != "committed" or not hmac.compare_digest(
        str(row["request_sha256"]), request_hash
    ):
        raise ApiError("platform.idempotency_conflict")
    return str(row["object_id"]), int(row["result_revision"])


def _audit(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    action: str,
    object_type: str,
    object_id: str,
    after_hash: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,'platform',?,?,?,?,?,?,?,NULL,?,?)""",
        (
            _id("pau_"),
            actor.operator_id,
            action,
            object_type,
            object_id,
            "growth_command",
            request_id,
            trace_id,
            after_hash,
            _db_time(now),
        ),
    )


def _consume_step_up(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    grant: str,
    *,
    action_code: str,
    resource_type: str,
    resource_id: str,
    now: datetime,
) -> None:
    if not grant:
        raise ApiError("platform.step_up_required")
    consume_global_step_up_grant_in_transaction(
        connection,
        config,
        account_id=actor.account_id,
        global_session_id=actor.global_session_id,
        grant=grant,
        action_code=action_code,
        resource_type=resource_type,
        resource_id=resource_id,
        now=now,
    )


def _freshness(
    rows: list[sqlite3.Row],
    generated_field: str,
    watermark_field: str,
) -> SourceFreshness:
    if not rows:
        return SourceFreshness(
            status="partial", watermark=None, stale_seconds=0, coverage_bps=0
        )
    generated = max(_parse_time(str(row[generated_field])) for row in rows)
    stale_seconds = max(0, int((_utcnow() - generated).total_seconds()))
    coverage = min(int(row["coverage_bps"]) for row in rows)
    status = "fresh" if stale_seconds <= 600 and coverage == 10_000 else "partial"
    if stale_seconds > 600:
        status = "stale"
    watermarks = [str(row[watermark_field]) for row in rows if row[watermark_field] is not None]
    return SourceFreshness(
        status=status,
        watermark=max(watermarks) if watermarks else None,
        stale_seconds=stale_seconds,
        coverage_bps=coverage,
    )


def _campaign_freshness(
    connection: sqlite3.Connection, rows: list[sqlite3.Row]
) -> SourceFreshness:
    return _cursor_freshness(connection, rows, "updated_at")


def _experiment_freshness(
    connection: sqlite3.Connection, rows: list[sqlite3.Row]
) -> SourceFreshness:
    return _cursor_freshness(connection, rows, "updated_at")


def _attribution_freshness(rows: list[sqlite3.Row]) -> SourceFreshness:
    if not rows:
        return SourceFreshness(
            status="partial", watermark=None, stale_seconds=0, coverage_bps=0
        )
    watermarks = [str(row["source_watermark"]) for row in rows if row["source_watermark"]]
    watermark = max(watermarks) if watermarks else None
    stale_seconds = 0 if watermark is None else max(
        0, int((_utcnow() - _parse_time(watermark)).total_seconds())
    )
    return SourceFreshness(
        status="fresh" if watermark is not None and stale_seconds <= 600 else "stale",
        watermark=watermark,
        stale_seconds=stale_seconds,
        coverage_bps=10_000,
    )


def _cursor_freshness(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
    fallback_field: str,
) -> SourceFreshness:
    cursor = connection.execute(
        """SELECT source_watermark FROM platform_growth_migration_cursors
            WHERE source_code='product_events'"""
    ).fetchone()
    watermark = None if cursor is None else cursor["source_watermark"]
    if watermark is None and rows:
        watermark = max(str(row[fallback_field]) for row in rows)
    if watermark is None:
        return SourceFreshness(
            status="partial", watermark=None, stale_seconds=0, coverage_bps=0
        )
    stale_seconds = max(0, int((_utcnow() - _parse_time(str(watermark))).total_seconds()))
    return SourceFreshness(
        status="fresh" if stale_seconds <= 600 else "stale",
        watermark=str(watermark),
        stale_seconds=stale_seconds,
        coverage_bps=10_000,
    )


def _encode_cursor(
    config: AppConfig,
    actor: PlatformContext,
    filter_digest: str,
    kind: str,
    boundary_time: str,
    boundary_id: str,
) -> str:
    payload = json.dumps(
        {
            "boundary_id": boundary_id,
            "boundary_time": boundary_time,
            "filter_digest": filter_digest,
            "kind": kind,
            "operator_revision": actor.operator_revision,
            "permission_digest": _digest(actor.permissions),
            "platform_session_id": actor.platform_session_id,
            "roles": actor.roles,
            "v": 1,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        _CURSOR_DOMAIN + b"\0" + payload,
        hashlib.sha256,
    ).digest()
    return _b64(payload) + "." + _b64(signature)


def _decode_cursor(
    config: AppConfig,
    actor: PlatformContext,
    cursor: str | None,
    filter_digest: str,
    kind: str,
) -> tuple[str, str] | None:
    if cursor is None:
        return None
    try:
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload = _unb64(encoded_payload)
        signature = _unb64(encoded_signature)
        expected = hmac.new(
            config.platform_session_secret.encode("utf-8"),
            _CURSOR_DOMAIN + b"\0" + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        parsed = json.loads(payload.decode("ascii"))
        if set(parsed) != {
            "boundary_id",
            "boundary_time",
            "filter_digest",
            "kind",
            "operator_revision",
            "permission_digest",
            "platform_session_id",
            "roles",
            "v",
        }:
            raise ValueError
        if (
            parsed["v"] != 1
            or parsed["kind"] != kind
            or parsed["filter_digest"] != filter_digest
            or parsed["operator_revision"] != actor.operator_revision
            or parsed["permission_digest"] != _digest(actor.permissions)
            or parsed["platform_session_id"] != actor.platform_session_id
            or tuple(parsed["roles"]) != actor.roles
            or not isinstance(parsed["boundary_time"], str)
            or not isinstance(parsed["boundary_id"], str)
        ):
            raise ValueError
        return parsed["boundary_time"], parsed["boundary_id"]
    except (
        ValueError,
        TypeError,
        KeyError,
        binascii.Error,
        UnicodeError,
        json.JSONDecodeError,
    ):
        raise ApiError("platform.request_invalid") from None


def _idempotency_hmac(config: AppConfig, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        _IDEMPOTENCY_DOMAIN + b"\0" + value.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _receipt_hmac(config: AppConfig, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        _RECEIPT_DOMAIN + b"\0" + value.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _optional_range(
    filters: dict[str, str],
    normalized: dict[str, str],
    maximum_days: int,
) -> None:
    if ("from" in filters) != ("to" in filters):
        raise ApiError("platform.request_invalid")
    if "from" in filters:
        _date_range(filters["from"], filters["to"], maximum_days)
        normalized["from"] = filters["from"]
        normalized["to"] = filters["to"]


def _date_range(start: str, end: str, maximum_days: int) -> tuple[date, date]:
    try:
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
    except ValueError:
        raise ApiError("platform.request_invalid") from None
    if start_day.isoformat() != start or end_day.isoformat() != end:
        raise ApiError("platform.request_invalid")
    days = (end_day - start_day).days
    if days < 0 or days >= maximum_days:
        raise ApiError("platform.request_invalid")
    return start_day, end_day


def _day_after(value: str) -> str:
    try:
        return (date.fromisoformat(value) + timedelta(days=1)).isoformat()
    except ValueError:
        raise ApiError("platform.request_invalid") from None


def _day_start(value: str) -> str:
    return f"{value}T00:00:00.000Z"


def _validate_window(starts_at: str, ends_at: str) -> None:
    if _parse_time(ends_at) <= _parse_time(starts_at):
        raise ApiError("platform.request_invalid")


def _validate_id(pattern: re.Pattern[str], value: str) -> None:
    if pattern.fullmatch(value) is None:
        raise ApiError("platform.growth_not_found")


def _actor(
    connection: sqlite3.Connection,
    config: AppConfig,
    token: str,
    csrf: str | None,
) -> PlatformContext:
    return authenticate_platform_session_in_transaction(
        connection,
        config,
        session_token=token,
        csrf_token=csrf,
        touch=True,
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform.unavailable") from None


def _require_schema(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version='0133' AND status='applied'"
    ).fetchone() is None:
        raise ApiError("platform.unavailable")


def _next_stamp(previous: str, now: datetime) -> str:
    current = _db_time(now)
    if current > previous:
        return current
    return _db_time(_parse_time(previous) + timedelta(milliseconds=1))


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime:
    current = value or _utcnow()
    if current.tzinfo is None:
        raise ApiError("platform.unavailable")
    return current.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError
    return parsed.astimezone(UTC)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not value or "=" in value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError
    decoded = base64.b64decode(
        value + "=" * ((4 - len(value) % 4) % 4),
        altchars=b"-_",
        validate=True,
    )
    if _b64(decoded) != value:
        raise ValueError
    return decoded


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


__all__ = [
    "apply_campaign_command",
    "apply_experiment_command",
    "approve_export",
    "build_next_export",
    "download_export",
    "get_export",
    "issue_export_receipt",
    "list_attribution",
    "list_campaigns",
    "list_cohorts",
    "list_experiments",
    "list_funnels",
    "request_export",
]
