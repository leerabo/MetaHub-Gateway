"""Bounded, replay-safe PRD-141 eligibility and aggregate projections."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError


def evaluate_source_eligibility(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    now: datetime,
    limit: int = 100,
) -> int:
    rows = connection.execute(
        """SELECT event.*,binding.workspace_id
            FROM product_events event
            JOIN workspace_tenant_bindings binding
              ON binding.tenant_id=event.tenant_id AND binding.status='active'
            WHERE NOT EXISTS(SELECT 1 FROM platform_growth_event_eligibility eligibility
              WHERE eligibility.product_event_id=event.product_event_id
                AND eligibility.formula_version='growth-eligibility-v2')
            ORDER BY event.occurred_at,event.product_event_id LIMIT ?""",
        (limit,),
    ).fetchall()
    count = 0
    stamp = _db_time(now)
    for row in rows:
        decision = _eligibility_decision(connection, row)
        token = None
        key_version = None
        if decision[0] == "eligible":
            token = _subject_token(
                config,
                str(decision[3]),
                str(row["workspace_id"]),
            )
            key_version = 1
        canonical = _digest(
            {
                "consent_record_id": decision[1],
                "event_id": row["product_event_id"],
                "preference_revision": decision[2],
                "reason": decision[0],
                "workspace_id": row["workspace_id"],
            }
        )
        connection.execute(
            """INSERT INTO platform_growth_event_eligibility(
              growth_event_eligibility_id,product_event_id,owner_workspace_id,tenant_id,
              formula_version,subject_token_sha256,subject_key_version,consent_record_id,
              privacy_preference_revision,purpose_code,purpose_version,status,reason_code,
              evaluated_at,canonical_sha256,created_at,updated_at)
            VALUES(?,?,?,?,'growth-eligibility-v2',?,?,?,?,
              'analytics','analytics-v2',?,?,?,?,?,?)""",
            (
                _id("gel_"),
                row["product_event_id"],
                row["workspace_id"],
                row["tenant_id"],
                token,
                key_version,
                decision[1],
                decision[2],
                "eligible" if decision[0] == "eligible" else "excluded",
                decision[0],
                stamp,
                canonical,
                stamp,
                stamp,
            ),
        )
        count += 1
    _advance_cursor(
        connection,
        "product_events",
        source_count=int(connection.execute("SELECT count(*) FROM product_events").fetchone()[0]),
        target_count=int(
            connection.execute(
                "SELECT count(*) FROM platform_growth_event_eligibility"
            ).fetchone()[0]
        ),
        watermark=connection.execute("SELECT max(occurred_at) FROM product_events").fetchone()[0],
        now=now,
    )
    return count


def materialize_daily_funnel(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    limit: int = 7,
) -> int:
    days = connection.execute(
        """SELECT DISTINCT substr(event.occurred_at,1,10) bucket_start
            FROM platform_growth_event_eligibility eligibility
            JOIN product_events event ON event.tenant_id=eligibility.tenant_id
              AND event.product_event_id=eligibility.product_event_id
            WHERE eligibility.status='eligible'
            ORDER BY bucket_start DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    inserted = 0
    for day_row in days:
        day = str(day_row["bucket_start"])
        rows = connection.execute(
            """SELECT event.event_type,eligibility.subject_token_sha256,event.occurred_at,
                    event.product_event_id
                FROM platform_growth_event_eligibility eligibility
                JOIN product_events event ON event.tenant_id=eligibility.tenant_id
                  AND event.product_event_id=eligibility.product_event_id
                WHERE eligibility.status='eligible' AND substr(event.occurred_at,1,10)=?
                ORDER BY event.product_event_id""",
            (day,),
        ).fetchall()
        subjects: dict[str, set[str]] = {}
        for row in rows:
            subjects.setdefault(str(row["event_type"]), set()).add(
                str(row["subject_token_sha256"])
            )
        registered = len(subjects.get("tenant.registered", set()))
        activated = len(subjects.get("gateway.first_call_succeeded", set()))
        distinct = len(set().union(*subjects.values())) if subjects else 0
        digest = _digest(
            [(row["product_event_id"], row["event_type"], row["occurred_at"]) for row in rows]
        )
        if _snapshot_digest_exists(connection, "platform_growth_funnel_snapshots", digest, day):
            continue
        generation = _next_generation(
            connection,
            "platform_growth_funnel_snapshots",
            "bucket_start",
            day,
        )
        suppressed = distinct < 20
        connection.execute(
            """INSERT INTO platform_growth_funnel_snapshots(
              growth_funnel_snapshot_id,bucket_start,bucket_kind,view_code,dimension_code,
              dimension_value,formula_version,generation,registered_count,activated_count,
              trial_started_count,paid_count,retained_30d_count,registered_to_activated_bps,
              activated_to_trial_bps,trial_to_paid_bps,paid_to_retained_bps,currency_code,
              revenue_minor,cost_minor,coverage_bps,distinct_subject_count,
              suppression_threshold,suppressed,suppression_code,source_watermark,
              source_sha256,supersedes_snapshot_id,generated_at)
            VALUES(?,?,'day','overview','none','all','growth-funnel-v2',?,?,?,0,0,0,
              ?,NULL,NULL,NULL,NULL,NULL,NULL,10000,?,20,?,?,?, ?,NULL,?)""",
            (
                _id("gfs_"),
                day,
                generation,
                registered,
                activated,
                _bps(activated, registered),
                distinct,
                int(suppressed),
                "k_anonymity" if suppressed else None,
                max((str(row["occurred_at"]) for row in rows), default=None),
                digest,
                _db_time(now),
            ),
        )
        inserted += 1
    inserted += _materialize_customer_success_views(connection, now)
    return inserted


def materialize_cohort_projection(
    connection: sqlite3.Connection,
    *,
    now: datetime,
) -> int:
    cohort = now.date() - timedelta(days=now.weekday())
    accounts = connection.execute(
        """SELECT customer_account_id,adoption_band,health_risk,source_revision_sha256
            FROM platform_customer_account_projections
            WHERE operational_status='active' ORDER BY customer_account_id"""
    ).fetchall()
    eligible = len(accounts)
    retained = sum(row["health_risk"] in {"healthy", "watch"} for row in accounts)
    digest = _digest(
        [(row["customer_account_id"], row["source_revision_sha256"]) for row in accounts]
    )
    if connection.execute(
        """SELECT 1 FROM platform_growth_cohort_snapshots
            WHERE cohort_bucket=? AND source_sha256=?""",
        (cohort.isoformat(), digest),
    ).fetchone():
        return 0
    generation = _next_generation(
        connection,
        "platform_growth_cohort_snapshots",
        "cohort_bucket",
        cohort.isoformat(),
    )
    suppressed = eligible < 20
    connection.execute(
        """INSERT INTO platform_growth_cohort_snapshots(
          growth_cohort_snapshot_id,cohort_kind,cohort_bucket,age_bucket,view_code,
          dimension_code,dimension_value,eligible_count,retained_count,churned_count,
          retention_bps,coverage_bps,distinct_subject_count,suppression_threshold,
          suppressed,suppression_code,formula_version,generation,source_watermark,
          source_sha256,supersedes_snapshot_id,generated_at)
        VALUES(?,'signup_week',?,0,'retention','none','all',?,?,?,?,10000,?,20,?,?,
          'growth-cohort-v2',?, ?,?,NULL,?)""",
        (
            _id("gco_"),
            cohort.isoformat(),
            eligible,
            retained,
            eligible - retained,
            _bps(retained, eligible),
            eligible,
            int(suppressed),
            "k_anonymity" if suppressed else None,
            generation,
            _db_time(now),
            digest,
            _db_time(now),
        ),
    )
    return 1


def materialize_attribution(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    now: datetime,
    limit: int = 100,
) -> int:
    source_id = _ensure_unknown_source(connection, now)
    rows = connection.execute(
        """SELECT eligibility.*,event.event_type,event.occurred_at
            FROM platform_growth_event_eligibility eligibility
            JOIN product_events event ON event.tenant_id=eligibility.tenant_id
              AND event.product_event_id=eligibility.product_event_id
            WHERE eligibility.status='eligible'
              AND event.event_type IN('tenant.registered','gateway.first_call_succeeded')
              AND NOT EXISTS(SELECT 1 FROM platform_conversion_attributions attribution
                WHERE attribution.conversion_source_kind='product_event'
                  AND attribution.conversion_source_id=event.product_event_id
                  AND attribution.formula_version='last-eligible-touch-30d-v1')
            ORDER BY event.occurred_at,event.product_event_id LIMIT ?""",
        (limit,),
    ).fetchall()
    count = 0
    from .service import _event

    for row in rows:
        attribution_id = _id("cat_")
        stage = "registered" if row["event_type"] == "tenant.registered" else "activated"
        source_hash = _digest(
            [row["product_event_id"], row["canonical_sha256"], source_id]
        )
        connection.execute(
            """INSERT INTO platform_conversion_attributions(
              conversion_attribution_id,conversion_source_kind,conversion_source_id,
              conversion_stage,owner_workspace_id,tenant_id,subject_token_sha256,
              subject_key_version,acquisition_source_id,growth_campaign_id,channel_partner_id,
              formula_version,lookback_days,value_minor,currency_code,consent_record_id,
              privacy_preference_revision,source_sha256,occurred_at,created_at)
            VALUES(?,'product_event',?,?,?,?,?,?,?,NULL,NULL,
              'last-eligible-touch-30d-v1',30,NULL,NULL,?,?,?,?,?)""",
            (
                attribution_id,
                row["product_event_id"],
                stage,
                row["owner_workspace_id"],
                row["tenant_id"],
                row["subject_token_sha256"],
                row["subject_key_version"],
                source_id,
                row["consent_record_id"],
                row["privacy_preference_revision"],
                source_hash,
                row["occurred_at"],
                _db_time(now),
            ),
        )
        _event(
            connection,
            event_type="growth.conversion_recorded",
            object_kind="conversion",
            object_id=attribution_id,
            revision=1,
            generation=1,
            payload={
                "conversion_id": attribution_id,
                "formula_version": "last-eligible-touch-30d-v1",
                "revision": 1,
                "schema_version": "growth-event-v1",
                "stage": stage,
            },
            now=now,
        )
        count += 1
    del config
    return count


def enforce_experiment_guardrails(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    limit: int = 100,
) -> int:
    rows = connection.execute(
        """SELECT experiment.*,
            (SELECT count(*) FROM platform_growth_experiment_assignments assignment
              WHERE assignment.growth_experiment_id=experiment.growth_experiment_id
                AND assignment.withdrawn_at IS NULL) assigned_count
          FROM platform_growth_experiments experiment
          WHERE experiment.status IN('running','paused')
            AND (experiment.ends_at<=? OR assigned_count>experiment.sample_cap)
          ORDER BY experiment.ends_at,experiment.growth_experiment_id LIMIT ?""",
        (_db_time(now), limit),
    ).fetchall()
    from .service import _event

    count = 0
    for row in rows:
        stamp = _db_time(now)
        connection.execute(
            """UPDATE platform_growth_experiments SET status='stopped',
              decision_code='stopped_guardrail',revision=revision+1,updated_at=?,decided_at=?
              WHERE growth_experiment_id=? AND revision=? AND status IN('running','paused')""",
            (stamp, stamp, row["growth_experiment_id"], row["revision"]),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            continue
        _event(
            connection,
            event_type="growth.experiment_decided",
            object_kind="experiment",
            object_id=row["growth_experiment_id"],
            revision=int(row["revision"]) + 1,
            generation=1,
            payload={
                "decision": "stopped_guardrail",
                "experiment_id": row["growth_experiment_id"],
                "revision": int(row["revision"]) + 1,
                "schema_version": "growth-event-v1",
                "source_digest": row["canonical_sha256"],
            },
            now=now,
        )
        count += 1
    return count


def apply_privacy_corrections(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    limit: int = 100,
) -> int:
    rows = connection.execute(
        """SELECT eligibility.growth_event_eligibility_id,
                   eligibility.owner_workspace_id,eligibility.subject_token_sha256,
                   membership.status membership_status,
                   preference.analytics_enabled,
                   (SELECT consent.decision FROM consent_records consent
                     WHERE consent.account_id=membership.account_id
                       AND consent.consent_type='analytics'
                     ORDER BY consent.consented_at DESC,consent.consent_record_id DESC
                     LIMIT 1) current_consent_decision
            FROM platform_growth_event_eligibility eligibility
            JOIN product_events event ON event.tenant_id=eligibility.tenant_id
              AND event.product_event_id=eligibility.product_event_id
            JOIN workspace_memberships membership
              ON membership.tenant_id=event.tenant_id
             AND membership.legacy_user_id=event.actor_user_id
            LEFT JOIN privacy_preferences preference
              ON preference.workspace_id=eligibility.owner_workspace_id
             AND preference.account_id=membership.account_id
            WHERE eligibility.status='eligible'
              AND (membership.status!='active' OR preference.analytics_enabled IS NOT 1
                OR coalesce((SELECT consent.decision FROM consent_records consent
                  WHERE consent.account_id=membership.account_id
                    AND consent.consent_type='analytics'
                  ORDER BY consent.consented_at DESC,consent.consent_record_id DESC
                  LIMIT 1),'withdrawn')!='granted')
            ORDER BY eligibility.growth_event_eligibility_id LIMIT ?""",
        (limit,),
    ).fetchall()
    stamp = _db_time(now)
    for row in rows:
        if row["membership_status"] != "active":
            reason = "membership_inactive"
        elif row["current_consent_decision"] == "denied":
            reason = "consent_denied"
        elif row["current_consent_decision"] != "granted":
            reason = "consent_withdrawn"
        else:
            reason = "preference_disabled"
        subject_token = row["subject_token_sha256"]
        connection.execute(
            """UPDATE platform_growth_event_eligibility SET subject_token_sha256=NULL,
              subject_key_version=NULL,status='withdrawn',reason_code=?,
              evaluated_at=?,updated_at=? WHERE growth_event_eligibility_id=?
              AND status='eligible'""",
            (reason, stamp, stamp, row["growth_event_eligibility_id"]),
        )
        if subject_token is not None:
            connection.execute(
                """UPDATE platform_growth_experiment_assignments
                  SET subject_token_sha256=NULL,subject_key_version=NULL,withdrawn_at=?
                  WHERE owner_workspace_id=? AND subject_token_sha256=?
                    AND withdrawn_at IS NULL""",
                (stamp, row["owner_workspace_id"], subject_token),
            )
    return len(rows)


def assign_experiment(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    experiment_id: str,
    eligibility_id: str,
    now: datetime,
) -> str:
    experiment = connection.execute(
        """SELECT * FROM platform_growth_experiments
            WHERE growth_experiment_id=? AND status='running'""",
        (experiment_id,),
    ).fetchone()
    eligibility = connection.execute(
        """SELECT * FROM platform_growth_event_eligibility
            WHERE growth_event_eligibility_id=? AND status='eligible'""",
        (eligibility_id,),
    ).fetchone()
    if experiment is None or eligibility is None:
        raise ApiError("platform.growth_state_conflict")
    if (
        experiment["eligibility_policy_version"] != eligibility["formula_version"]
        or experiment["consent_purpose"] != eligibility["purpose_code"]
        or experiment["consent_version"] != eligibility["purpose_version"]
    ):
        raise ApiError("platform.growth_state_conflict")
    existing = connection.execute(
        """SELECT growth_experiment_assignment_id
            FROM platform_growth_experiment_assignments
            WHERE growth_experiment_id=? AND owner_workspace_id=?
              AND subject_token_sha256=?""",
        (
            experiment_id,
            eligibility["owner_workspace_id"],
            eligibility["subject_token_sha256"],
        ),
    ).fetchone()
    if existing:
        return str(existing[0])
    assigned = int(
        connection.execute(
            """SELECT count(*) FROM platform_growth_experiment_assignments
                WHERE growth_experiment_id=?""",
            (experiment_id,),
        ).fetchone()[0]
    )
    if assigned >= int(experiment["sample_cap"]):
        raise ApiError("platform.growth_state_conflict")
    token = str(eligibility["subject_token_sha256"])
    bucket = int.from_bytes(
        hmac.new(
            config.tenant_data_encryption_secret.encode("utf-8"),
            f"prd141/experiment/v1\0{experiment_id}\0{token}".encode("ascii"),
            hashlib.sha256,
        ).digest()[:8],
        "big",
    ) % 10_000
    variant = (
        experiment["control_code"]
        if bucket < int(experiment["control_allocation_bps"])
        else experiment["variant_code"]
    )
    assignment_id = _id("gea_")
    digest = _digest([experiment_id, token, bucket, variant])
    connection.execute(
        """INSERT INTO platform_growth_experiment_assignments(
          growth_experiment_assignment_id,growth_experiment_id,owner_workspace_id,
          tenant_id,subject_token_sha256,subject_key_version,deterministic_bucket,
          variant_code,consent_record_id,privacy_preference_revision,assigned_at,
          withdrawn_at,assignment_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
        (
            assignment_id,
            experiment_id,
            eligibility["owner_workspace_id"],
            eligibility["tenant_id"],
            token,
            eligibility["subject_key_version"],
            bucket,
            variant,
            eligibility["consent_record_id"],
            eligibility["privacy_preference_revision"],
            _db_time(now),
            digest,
        ),
    )
    return assignment_id


def _eligibility_decision(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
) -> tuple[str, str | None, int | None, str | None]:
    if event["actor_user_id"] is None:
        return "historical_consent_unproven", None, None, None
    membership = connection.execute(
        """SELECT membership.account_id,membership.status
            FROM workspace_memberships membership
            WHERE membership.tenant_id=? AND membership.legacy_user_id=?
              AND membership.workspace_id=?""",
        (event["tenant_id"], event["actor_user_id"], event["workspace_id"]),
    ).fetchone()
    if membership is None or membership["status"] != "active":
        return "membership_inactive", None, None, None
    consent = connection.execute(
        """SELECT consent_record_id,decision FROM consent_records
            WHERE account_id=? AND consent_type='analytics' AND consented_at<=?
            ORDER BY consented_at DESC,consent_record_id DESC LIMIT 1""",
        (membership["account_id"], event["occurred_at"]),
    ).fetchone()
    if consent is None:
        return "historical_consent_unproven", None, None, None
    if consent["decision"] == "withdrawn":
        return "consent_withdrawn", consent["consent_record_id"], None, None
    if consent["decision"] != "granted":
        return "consent_denied", consent["consent_record_id"], None, None
    preference = connection.execute(
        """SELECT analytics_enabled,revision FROM privacy_preferences
            WHERE workspace_id=? AND account_id=?""",
        (event["workspace_id"], membership["account_id"]),
    ).fetchone()
    if preference is None or preference["analytics_enabled"] != 1:
        return "preference_disabled", consent["consent_record_id"], None, None
    return (
        "eligible",
        str(consent["consent_record_id"]),
        int(preference["revision"]),
        str(membership["account_id"]),
    )


def _materialize_customer_success_views(
    connection: sqlite3.Connection,
    now: datetime,
) -> int:
    rows = connection.execute(
        """SELECT customer_account_id,activation_phase,adoption_band,health_risk,
            source_revision_sha256 FROM platform_customer_account_projections
            WHERE operational_status='active' ORDER BY customer_account_id"""
    ).fetchall()
    if not rows:
        return 0
    digest = _digest(
        [(row["customer_account_id"], row["source_revision_sha256"]) for row in rows]
    )
    bucket = now.date().isoformat()
    inserted = 0
    for view in ("adoption", "retention"):
        view_digest = _digest([digest, view])
        if _snapshot_digest_exists(
            connection, "platform_growth_funnel_snapshots", view_digest, bucket
        ):
            continue
        total = len(rows)
        activated = sum(row["activation_phase"] == "activated" for row in rows)
        retained = sum(row["health_risk"] in {"healthy", "watch"} for row in rows)
        generation = int(
            connection.execute(
                """SELECT coalesce(max(generation),0)+1
                    FROM platform_growth_funnel_snapshots
                    WHERE bucket_start=? AND view_code=?""",
                (bucket, view),
            ).fetchone()[0]
        )
        suppressed = total < 20
        connection.execute(
            """INSERT INTO platform_growth_funnel_snapshots(
              growth_funnel_snapshot_id,bucket_start,bucket_kind,view_code,dimension_code,
              dimension_value,formula_version,generation,registered_count,activated_count,
              trial_started_count,paid_count,retained_30d_count,registered_to_activated_bps,
              activated_to_trial_bps,trial_to_paid_bps,paid_to_retained_bps,currency_code,
              revenue_minor,cost_minor,coverage_bps,distinct_subject_count,
              suppression_threshold,suppressed,suppression_code,source_watermark,
              source_sha256,supersedes_snapshot_id,generated_at)
            VALUES(?,?,'day',?,'none','all','growth-funnel-v2',?,?,?,0,0,?,
              ?,NULL,NULL,NULL,NULL,NULL,NULL,10000,?,20,?,?,?, ?,NULL,?)""",
            (
                _id("gfs_"),
                bucket,
                view,
                generation,
                total,
                activated,
                retained,
                _bps(activated, total),
                total,
                int(suppressed),
                "k_anonymity" if suppressed else None,
                _db_time(now),
                view_digest,
                _db_time(now),
            ),
        )
        inserted += 1
    return inserted


def _ensure_unknown_source(connection: sqlite3.Connection, now: datetime) -> str:
    source_hash = hashlib.sha256(b"prd141/acquisition/unknown/v1").hexdigest()
    row = connection.execute(
        """SELECT acquisition_source_id FROM platform_acquisition_sources
            WHERE source_code_sha256=?""",
        (source_hash,),
    ).fetchone()
    if row:
        return str(row[0])
    source_id = "acs_" + source_hash[:32]
    canonical = _digest(["unknown", "unknown", source_hash])
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO platform_acquisition_sources(
          acquisition_source_id,source_code_sha256,source_kind,channel_code,
          channel_partner_id,channel_referral_code_id,campaign_id,verification_status,
          valid_from,valid_until,revision,canonical_sha256,created_at,updated_at)
        VALUES(?,?, 'unknown','unknown',NULL,NULL,NULL,'verified',?,NULL,1,?,?,?)""",
        (source_id, source_hash, stamp, canonical, stamp, stamp),
    )
    return source_id


def _advance_cursor(
    connection: sqlite3.Connection,
    source_code: str,
    *,
    source_count: int,
    target_count: int,
    watermark: str | None,
    now: datetime,
) -> None:
    source_hash = _digest([source_code, source_count, target_count, watermark])
    connection.execute(
        """UPDATE platform_growth_migration_cursors SET source_row_count=?,target_row_count=?,
          source_watermark=?,source_sha256=?,generation=generation+1,updated_at=?
          WHERE source_code=? AND (source_row_count!=? OR target_row_count!=?
            OR source_watermark IS NOT ?)""",
        (
            source_count,
            target_count,
            watermark,
            source_hash,
            _db_time(now),
            source_code,
            source_count,
            target_count,
            watermark,
        ),
    )


def _snapshot_digest_exists(
    connection: sqlite3.Connection,
    table: str,
    digest: str,
    bucket: str,
) -> bool:
    if table != "platform_growth_funnel_snapshots":
        raise ApiError("platform.unavailable")
    return connection.execute(
        """SELECT 1 FROM platform_growth_funnel_snapshots
            WHERE bucket_start=? AND source_sha256=?""",
        (bucket, digest),
    ).fetchone() is not None


def _next_generation(
    connection: sqlite3.Connection,
    table: str,
    bucket_column: str,
    bucket: str,
) -> int:
    allowed = {
        ("platform_growth_funnel_snapshots", "bucket_start"),
        ("platform_growth_cohort_snapshots", "cohort_bucket"),
    }
    if (table, bucket_column) not in allowed:
        raise ApiError("platform.unavailable")
    return int(
        connection.execute(
            f"SELECT coalesce(max(generation),0)+1 FROM {table} WHERE {bucket_column}=?",
            (bucket,),
        ).fetchone()[0]
    )


def _subject_token(config: AppConfig, account_id: str, workspace_id: str) -> str:
    secret = config.tenant_data_encryption_secret
    if not secret or len(secret.encode("utf-8")) < 32:
        raise ApiError("platform.unavailable")
    return hmac.new(
        secret.encode("utf-8"),
        f"prd141/growth-subject/v1\0{account_id}\0{workspace_id}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _bps(numerator: int, denominator: int) -> int | None:
    if denominator == 0:
        return None
    return min(10_000, (numerator * 10_000 + denominator // 2) // denominator)


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


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "apply_privacy_corrections",
    "assign_experiment",
    "enforce_experiment_guardrails",
    "evaluate_source_eligibility",
    "materialize_attribution",
    "materialize_cohort_projection",
    "materialize_daily_funnel",
]
