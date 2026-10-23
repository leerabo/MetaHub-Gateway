"""PRD-140 safe customer, health-v2, renewal, task, and QBR projections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from metahub_shared.runtime.errors import ApiError

from metahub_control.customer_health.service import (
    SIGNAL_WEIGHTS,
    collect_source_snapshot,
    evaluate_source_snapshot,
)

from .outbox import record_event

_JOBS = (
    "account_projection",
    "health_evaluation",
    "renewal_projection",
    "task_due",
    "qbr_snapshot",
    "event_outbox",
)


def reconcile_accounts(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    limit: int = 500,
) -> int:
    """Project bounded owner-qualified account/contact rows and ensure all six jobs."""

    _transaction(connection)
    stamp = _db_time(now)
    sources = connection.execute(
        """SELECT binding.workspace_id,binding.tenant_id,workspace.revision workspace_revision,
                  workspace.kind workspace_kind,
                  coalesce(organization.name,binding.workspace_id) display_name,
                  coalesce(
                    organization.slug_normalized,'ws-'||substr(binding.workspace_id,5,12)
                  ) slug,
                  coalesce(region.region_code,'unknown') region_code,
                  coalesce(operational.status,'migration_conflict') operational_status,
                  coalesce(operational.revision,0) operational_revision,
                  coalesce(onboarding.completed_steps,0) completed_steps,
                  coalesce(onboarding.revision,0) onboarding_revision,
                  subscription.subscription_id,subscription.plan_version_id,
                  coalesce(subscription.status,'not_configured') subscription_status,
                  subscription.current_period_ends_at renewal_at,
                  coalesce(subscription.state_revision,0) subscription_revision,
                  (SELECT count(*) FROM support_tickets ticket
                    WHERE ticket.workspace_id=binding.workspace_id
                      AND ticket.tenant_id=binding.tenant_id
                      AND ticket.status NOT IN('resolved','closed')) open_ticket_count,
                  (SELECT count(*) FROM support_sla_clocks clock
                    WHERE clock.workspace_id=binding.workspace_id
                      AND clock.tenant_id=binding.tenant_id AND clock.state!='stopped'
                      AND unixepoch(clock.resolution_deadline_at)<unixepoch(?)) breached_count
             FROM workspace_tenant_bindings binding
             JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
             JOIN organization_profiles organization
               ON organization.workspace_id=binding.workspace_id
             LEFT JOIN workspace_regions region ON region.workspace_id=binding.workspace_id
             LEFT JOIN workspace_operational_states operational
               ON operational.workspace_id=binding.workspace_id
             LEFT JOIN onboarding_progress onboarding
               ON onboarding.workspace_id=binding.workspace_id
             LEFT JOIN workspace_subscriptions subscription
               ON subscription.workspace_id=binding.workspace_id
              AND subscription.tenant_id=binding.tenant_id
            WHERE binding.status='active' ORDER BY binding.workspace_id LIMIT ?""",
        (stamp, limit),
    ).fetchall()
    changed = 0
    for source in sources:
        account_id = "csa_" + _sha("customer-account-v1\x1f" + source["workspace_id"])[:32]
        completed = int(source["completed_steps"])
        activation = (
            "activated" if completed == 8 else ("in_progress" if completed else "not_started")
        )
        adoption = (
            "high"
            if completed == 8
            else ("medium" if completed >= 4 else ("low" if completed else "unknown"))
        )
        source_digest = _digest(
            [
                "customer-account-v1",
                int(source["workspace_revision"]),
                int(source["operational_revision"]),
                int(source["onboarding_revision"]),
                int(source["subscription_revision"]),
                int(source["open_ticket_count"]),
                int(source["breached_count"]),
            ]
        )
        current = connection.execute(
            "SELECT * FROM platform_customer_account_projections WHERE customer_account_id=?",
            (account_id,),
        ).fetchone()
        values = (
            int(source["workspace_revision"]),
            source["display_name"],
            source["slug"],
            source["workspace_kind"],
            source["region_code"],
            source["operational_status"],
            activation,
            completed,
            adoption,
            source["subscription_id"],
            source["plan_version_id"],
            source["subscription_status"],
            source["renewal_at"],
            int(source["open_ticket_count"]),
            int(source["breached_count"]),
            source_digest,
        )
        if current is None:
            connection.execute(
                """INSERT INTO platform_customer_account_projections(
                  customer_account_id,workspace_id,tenant_id,workspace_revision,display_name,slug,
                  workspace_kind,region_code,operational_status,activation_phase,
                  activation_completed_steps,adoption_band,subscription_id,plan_version_id,
                  subscription_status,renewal_at,latest_health_snapshot_id,latest_health_sequence,
                  health_risk,open_support_ticket_count,breached_sla_count,source_revision_sha256,
                  projection_version,revision,projected_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,'unknown',?,?,?,
                  'customer-account-v1',1,?,?,?)""",
                (
                    account_id,
                    source["workspace_id"],
                    source["tenant_id"],
                    *values,
                    stamp,
                    stamp,
                    stamp,
                ),
            )
            changed += 1
        elif current["source_revision_sha256"] != source_digest:
            next_stamp = _next_stamp(current["updated_at"], now)
            connection.execute(
                """UPDATE platform_customer_account_projections SET
                  workspace_revision=?,display_name=?,slug=?,workspace_kind=?,region_code=?,
                  operational_status=?,activation_phase=?,activation_completed_steps=?,
                  adoption_band=?,subscription_id=?,plan_version_id=?,subscription_status=?,
                  renewal_at=?,open_support_ticket_count=?,breached_sla_count=?,
                  source_revision_sha256=?,revision=revision+1,projected_at=?,updated_at=?
                  WHERE customer_account_id=?""",
                (*values, next_stamp, next_stamp, account_id),
            )
            changed += 1
        _reconcile_contacts(
            connection, account_id, source["workspace_id"], source["tenant_id"], now
        )
        _ensure_jobs(connection, account_id, source["workspace_id"], source["tenant_id"], now)
    return changed


def materialize_health_v2(
    connection: sqlite3.Connection,
    account_id: str,
    *,
    now: datetime,
) -> str | None:
    """Append one six-factor health fact and monotonically advance its verified head."""

    _transaction(connection)
    account = connection.execute(
        "SELECT * FROM platform_customer_account_projections WHERE customer_account_id=?",
        (account_id,),
    ).fetchone()
    if account is None:
        raise ApiError("platform.unavailable")
    source = collect_source_snapshot(connection, account["tenant_id"], evaluated_at=now)
    base = evaluate_source_snapshot(source)
    support = _support_counts(connection, account, now)
    ticket_score, ticket_risk = _ticket_score(support)
    scores = tuple(int(signal.score_bps or 0) for signal in base.signals[:5]) + (ticket_score,)
    total, contributions = _weighted(scores)
    risk = _risk(total)
    rank = {"healthy": 0, "watch": 1, "at_risk": 2, "critical": 3}
    if rank[ticket_risk] > rank[risk]:
        risk = ticket_risk
    if base.risk_level == "critical" or risk == "critical":
        risk = "critical"
    elif base.risk_level == "at_risk" or risk == "at_risk":
        risk = "at_risk"
    elif base.risk_level == "watch" or risk == "watch":
        risk = "watch"
    source_fingerprint = _digest(
        [
            "customer-health-platform-v2",
            account["workspace_id"],
            account["tenant_id"],
            base.source_fingerprint,
            *support,
        ]
    )
    projection_hash = _digest(
        [
            "customer-health-platform-v2",
            total,
            risk,
            scores,
            contributions,
            *support,
        ]
    )
    snapshot_id = (
        "chs_"
        + _sha(
            "\x1f".join(
                (
                    "customer-health-platform-v2",
                    account_id,
                    source.window_end,
                    source_fingerprint,
                )
            )
        )[:32]
    )
    existing = connection.execute(
        """SELECT customer_health_snapshot_sequence FROM platform_customer_health_snapshots
            WHERE customer_account_id=? AND customer_health_snapshot_id=?""",
        (account_id, snapshot_id),
    ).fetchone()
    if existing is not None:
        return None
    stamp = _db_time(now.replace(microsecond=0))
    connection.execute(
        """INSERT INTO platform_customer_health_snapshots(
          customer_health_snapshot_id,customer_account_id,workspace_id,tenant_id,
          formula_version,window_start,window_end,comparison_start,comparison_end,
          source_fingerprint,health_projection_sha256,current_request_count,
          previous_request_count,successful_request_count,failed_request_count,
          unresolved_cost_anomaly_count,critical_cost_anomaly_count,active_budget_rule_count,
          eligible_user_count,recently_active_user_count,activation_stage_count,
          open_support_ticket_count,open_p1_ticket_count,open_p2_ticket_count,
          waiting_platform_ticket_count,sla_at_risk_count,sla_breached_count,ticket_source_status,
          usage_trend_score_bps,usage_trend_contribution_bps,reliability_score_bps,
          reliability_contribution_bps,cost_anomaly_health_score_bps,
          cost_anomaly_health_contribution_bps,budget_readiness_score_bps,
          budget_readiness_contribution_bps,team_activity_score_bps,
          team_activity_contribution_bps,ticket_health_score_bps,
          ticket_health_contribution_bps,effective_weight_bps,score_bps,risk_level,
          usage_declining,high_error_rate,cost_anomaly_open,budget_missing,team_inactive,
          operation_request_id,operation_trace_id,evaluated_at,created_at,updated_at)
        VALUES(
          :snapshot_id,:account_id,:workspace_id,:tenant_id,:formula_version,
          :window_start,:window_end,:comparison_start,:comparison_end,
          :source_fingerprint,:projection_hash,:current_requests,:previous_requests,
          :successful_requests,:failed_requests,:unresolved_cost,:critical_cost,
          :active_budgets,:eligible_users,:recent_users,:activation_steps,
          :open_tickets,:open_p1,:open_p2,:waiting_platform,:sla_at_risk,:sla_breached,
          :ticket_source,:usage_score,:usage_contribution,:reliability_score,
          :reliability_contribution,:cost_score,:cost_contribution,:budget_score,
          :budget_contribution,:team_score,:team_contribution,:ticket_score,
          :ticket_contribution,:effective_weight,:total_score,:risk_level,
          :usage_declining,:high_error_rate,:cost_anomaly_open,:budget_missing,
          :team_inactive,:request_id,:trace_id,:evaluated_at,:created_at,:updated_at)""",
        {
            "snapshot_id": snapshot_id,
            "account_id": account_id,
            "workspace_id": account["workspace_id"],
            "tenant_id": account["tenant_id"],
            "formula_version": "customer-health-platform-v2",
            "window_start": source.window_start,
            "window_end": source.window_end,
            "comparison_start": source.comparison_start,
            "comparison_end": source.comparison_end,
            "source_fingerprint": source_fingerprint,
            "projection_hash": projection_hash,
            "current_requests": source.current_request_count,
            "previous_requests": source.previous_request_count,
            "successful_requests": source.successful_request_count,
            "failed_requests": source.failed_request_count,
            "unresolved_cost": source.unresolved_cost_anomaly_count,
            "critical_cost": source.critical_cost_anomaly_count,
            "active_budgets": source.active_budget_rule_count,
            "eligible_users": source.eligible_user_count,
            "recent_users": source.recently_active_user_count,
            "activation_steps": source.activation_stage_count,
            "open_tickets": support[0],
            "open_p1": support[1],
            "open_p2": support[2],
            "waiting_platform": support[3],
            "sla_at_risk": support[4],
            "sla_breached": support[5],
            "ticket_source": "available",
            "usage_score": scores[0],
            "usage_contribution": contributions[0],
            "reliability_score": scores[1],
            "reliability_contribution": contributions[1],
            "cost_score": scores[2],
            "cost_contribution": contributions[2],
            "budget_score": scores[3],
            "budget_contribution": contributions[3],
            "team_score": scores[4],
            "team_contribution": contributions[4],
            "ticket_score": scores[5],
            "ticket_contribution": contributions[5],
            "effective_weight": 10_000,
            "total_score": total,
            "risk_level": risk,
            "usage_declining": int("usage_declining" in base.risk_tags),
            "high_error_rate": int("high_error_rate" in base.risk_tags),
            "cost_anomaly_open": int("cost_anomaly_open" in base.risk_tags),
            "budget_missing": int("budget_missing" in base.risk_tags),
            "team_inactive": int("team_inactive" in base.risk_tags),
            "request_id": "req_chs_" + _sha("request:" + snapshot_id)[:32],
            "trace_id": "trc_chs_" + _sha("trace:" + snapshot_id)[:32],
            "evaluated_at": stamp,
            "created_at": stamp,
            "updated_at": stamp,
        },
    )
    snapshot = connection.execute(
        "SELECT * FROM platform_customer_health_snapshots WHERE customer_health_snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    old_head = connection.execute(
        "SELECT * FROM platform_customer_health_heads WHERE customer_account_id=?", (account_id,)
    ).fetchone()
    if old_head is None:
        head_stamp = stamp
        connection.execute(
            """INSERT INTO platform_customer_health_heads(
              customer_account_id,workspace_id,tenant_id,customer_health_snapshot_id,
              source_sequence,source_fingerprint,projection_sha256,risk_level,
              freshness_watermark,projected_at,revision) VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
            (
                account_id,
                account["workspace_id"],
                account["tenant_id"],
                snapshot_id,
                snapshot["customer_health_snapshot_sequence"],
                source_fingerprint,
                projection_hash,
                risk,
                source.window_end,
                head_stamp,
            ),
        )
    else:
        head_stamp = _next_stamp(old_head["projected_at"], now)
        connection.execute(
            """UPDATE platform_customer_health_heads SET customer_health_snapshot_id=?,
                  source_sequence=?,source_fingerprint=?,projection_sha256=?,risk_level=?,
                  freshness_watermark=?,projected_at=?,revision=revision+1
                WHERE customer_account_id=?""",
            (
                snapshot_id,
                snapshot["customer_health_snapshot_sequence"],
                source_fingerprint,
                projection_hash,
                risk,
                source.window_end,
                head_stamp,
                account_id,
            ),
        )
    account_stamp = _next_stamp(account["updated_at"], now)
    connection.execute(
        """UPDATE platform_customer_account_projections SET latest_health_snapshot_id=?,
              latest_health_sequence=?,health_risk=?,revision=revision+1,
              projected_at=?,updated_at=? WHERE customer_account_id=?""",
        (
            snapshot_id,
            snapshot["customer_health_snapshot_sequence"],
            risk,
            account_stamp,
            account_stamp,
            account_id,
        ),
    )
    if old_head is None or old_head["projection_sha256"] != projection_hash:
        current_account = connection.execute(
            "SELECT * FROM platform_customer_account_projections WHERE customer_account_id=?",
            (account_id,),
        ).fetchone()
        record_event(
            connection,
            current_account,
            event_type="customer.health_changed",
            object_kind="health_snapshot",
            object_id=snapshot_id,
            revision=int(snapshot["customer_health_snapshot_sequence"]),
            reason_code="health_projection_changed",
            safe_count=int(support[0]),
            source_digest=projection_hash,
            occurred_at=stamp,
        )
    return snapshot_id


def reconcile_renewals(connection: sqlite3.Connection, *, now: datetime, limit: int = 500) -> int:
    _transaction(connection)
    rows = connection.execute(
        """SELECT account.*,subscription.subscription_id source_subscription_id,
                  subscription.plan_version_id source_plan_version_id,
                  subscription.current_period_ends_at source_renewal_at,
                  subscription.status source_subscription_status,
                  subscription.state_revision
             FROM platform_customer_account_projections account
             JOIN workspace_subscriptions subscription
               ON subscription.workspace_id=account.workspace_id
              AND subscription.tenant_id=account.tenant_id
            WHERE subscription.current_period_ends_at IS NOT NULL
            ORDER BY account.customer_account_id LIMIT ?""",
        (limit,),
    ).fetchall()
    changed = 0
    for row in rows:
        opportunity_id = "rno_" + _sha("renewal-v1\x1f" + row["source_subscription_id"])[:32]
        adoption = {"high": "healthy", "medium": "watch", "low": "at_risk"}.get(
            row["adoption_band"], "unknown"
        )
        payment = (
            "past_due"
            if row["source_subscription_status"] == "past_due"
            else (
                "current"
                if row["source_subscription_status"] in {"active", "trialing"}
                else "unknown"
            )
        )
        digest = _digest(
            [
                "renewal-opportunity-v1",
                row["source_subscription_id"],
                row["source_plan_version_id"],
                row["source_renewal_at"],
                row["source_subscription_status"],
                int(row["state_revision"]),
                row["health_risk"],
                row["adoption_band"],
            ]
        )
        current = connection.execute(
            "SELECT * FROM platform_renewal_opportunities WHERE renewal_opportunity_id=?",
            (opportunity_id,),
        ).fetchone()
        stamp = _db_time(now)
        if current is None:
            connection.execute(
                """INSERT INTO platform_renewal_opportunities(
                  renewal_opportunity_id,customer_account_id,workspace_id,tenant_id,
                  subscription_id,plan_version_id,renewal_at,currency,amount_minor,amount_status,
                  health_risk_code,adoption_risk_code,payment_risk_code,invoice_status,status,
                  formula_version,source_digest,revision,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,NULL,NULL,'not_available',?,?,?,'not_available','observing',
                  'renewal-opportunity-v1',?,1,?,?)""",
                (
                    opportunity_id,
                    row["customer_account_id"],
                    row["workspace_id"],
                    row["tenant_id"],
                    row["source_subscription_id"],
                    row["source_plan_version_id"],
                    row["source_renewal_at"],
                    row["health_risk"],
                    adoption,
                    payment,
                    digest,
                    stamp,
                    stamp,
                ),
            )
            changed += 1
        elif current["source_digest"] != digest:
            next_stamp = _next_stamp(current["updated_at"], now)
            old_risk = (
                current["health_risk_code"],
                current["adoption_risk_code"],
                current["payment_risk_code"],
            )
            new_risk = (row["health_risk"], adoption, payment)
            connection.execute(
                """UPDATE platform_renewal_opportunities SET renewal_at=?,health_risk_code=?,
                      adoption_risk_code=?,payment_risk_code=?,source_digest=?,revision=revision+1,
                      updated_at=? WHERE renewal_opportunity_id=?""",
                (row["source_renewal_at"], *new_risk, digest, next_stamp, opportunity_id),
            )
            changed += 1
            if old_risk != new_risk:
                account = connection.execute(
                    """SELECT * FROM platform_customer_account_projections
                        WHERE customer_account_id=?""",
                    (row["customer_account_id"],),
                ).fetchone()
                revision = int(current["revision"]) + 1
                record_event(
                    connection,
                    account,
                    event_type="renewal.risk_changed",
                    object_kind="renewal_opportunity",
                    object_id=opportunity_id,
                    revision=revision,
                    reason_code="renewal_risk_changed",
                    safe_count=0,
                    source_digest=digest,
                    occurred_at=next_stamp,
                )
    return changed


def emit_due_task_events(connection: sqlite3.Connection, *, now: datetime, limit: int = 100) -> int:
    _transaction(connection)
    stamp = _db_time(now)
    rows = connection.execute(
        """SELECT task.*,account.source_revision_sha256 FROM platform_customer_tasks task
             JOIN platform_customer_account_projections account
               ON account.customer_account_id=task.customer_account_id
            WHERE task.status IN('open','in_progress') AND task.due_at<=?
              AND NOT EXISTS(SELECT 1 FROM platform_customer_success_events event
                WHERE event.event_type='customer.task_due'
                  AND event.object_id=task.customer_task_id
                  AND event.object_revision=task.due_generation)
            ORDER BY task.due_at,task.customer_task_id LIMIT ?""",
        (stamp, limit),
    ).fetchall()
    for row in rows:
        account = connection.execute(
            "SELECT * FROM platform_customer_account_projections WHERE customer_account_id=?",
            (row["customer_account_id"],),
        ).fetchone()
        record_event(
            connection,
            account,
            event_type="customer.task_due",
            object_kind="customer_task",
            object_id=row["customer_task_id"],
            revision=int(row["due_generation"]),
            reason_code="task_due",
            safe_count=0,
            source_digest=row["source_revision_sha256"],
            occurred_at=stamp,
        )
    return len(rows)


def generate_qbr(connection: sqlite3.Connection, account_id: str, *, now: datetime) -> str | None:
    _transaction(connection)
    account = connection.execute(
        "SELECT * FROM platform_customer_account_projections WHERE customer_account_id=?",
        (account_id,),
    ).fetchone()
    if account is None:
        return None
    quarter_number = (now.month - 1) // 3 + 1
    previous = quarter_number - 1
    year = now.year
    if previous == 0:
        previous = 4
        year -= 1
    quarter = f"{year:04d}-Q{previous}"
    health_sequence = account["latest_health_sequence"]
    source_digest = _digest(
        [
            quarter,
            health_sequence,
            account["adoption_band"],
            int(account["open_support_ticket_count"]),
            account["subscription_status"],
        ]
    )
    existing = connection.execute(
        """SELECT qbr_summary_id FROM platform_qbr_summaries
            WHERE customer_account_id=? AND quarter=? AND source_digest=?""",
        (account_id, quarter, source_digest),
    ).fetchone()
    if existing:
        return None
    previous_row = connection.execute(
        """SELECT qbr_summary_id FROM platform_qbr_summaries
            WHERE customer_account_id=? AND quarter=? ORDER BY generated_at DESC LIMIT 1""",
        (account_id, quarter),
    ).fetchone()
    incomplete = health_sequence is None
    support_count = int(account["open_support_ticket_count"])
    renewal_risk = int(account["subscription_status"] == "past_due")
    outcome = (
        "incomplete"
        if incomplete
        else (
            "support_attention"
            if support_count
            else (
                "renewal_attention"
                if renewal_risk
                else (
                    "adoption_attention"
                    if account["adoption_band"] in {"unknown", "low"}
                    else "healthy_growth"
                )
            )
        )
    )
    action = {
        "incomplete": "complete_sources",
        "support_attention": "review_support",
        "renewal_attention": "prepare_renewal",
        "adoption_attention": "activate_plan",
        "healthy_growth": "maintain",
    }[outcome]
    qbr_id = "qbr_" + _sha(f"{account_id}:{quarter}:{source_digest}")[:32]
    connection.execute(
        """INSERT INTO platform_qbr_summaries(
          qbr_summary_id,customer_account_id,workspace_id,tenant_id,quarter,health_sequence,
          adoption_bps,support_ticket_count,renewal_risk_count,outcome_code,action_code,
          source_digest,supersedes_qbr_summary_id,generated_at)
        VALUES(?,?,?,?,?,?,NULL,?,?,?,?,?,?,?)""",
        (
            qbr_id,
            account_id,
            account["workspace_id"],
            account["tenant_id"],
            quarter,
            health_sequence,
            support_count,
            renewal_risk,
            outcome,
            action,
            source_digest,
            None if previous_row is None else previous_row["qbr_summary_id"],
            _db_time(now),
        ),
    )
    return qbr_id


def _reconcile_contacts(connection, account_id, workspace_id, tenant_id, now):
    owners = connection.execute(
        """SELECT membership.workspace_membership_id,membership.account_id,membership.revision
             FROM workspace_memberships membership
             JOIN global_accounts account ON account.account_id=membership.account_id
            WHERE membership.workspace_id=? AND membership.tenant_id=?
              AND membership.role_code='owner' AND membership.status='active'
              AND account.status='active' ORDER BY membership.workspace_membership_id""",
        (workspace_id, tenant_id),
    ).fetchall()
    owner_ids = {row["workspace_membership_id"] for row in owners}
    for owner in owners:
        contact_id = (
            "cct_" + _sha("customer-contact-v1\x1f" + owner["workspace_membership_id"])[:32]
        )
        existing = connection.execute(
            "SELECT * FROM platform_customer_contacts WHERE customer_contact_id=?", (contact_id,)
        ).fetchone()
        if existing is None:
            stamp = _db_time(now)
            connection.execute(
                """INSERT INTO platform_customer_contacts(
                  customer_contact_id,customer_account_id,workspace_id,tenant_id,account_id,
                  workspace_membership_id,contact_role,status,membership_revision,
                  notification_preference_revision,preference_summary_json,revision,
                  created_at,updated_at) VALUES(?,?,?,?,?,?,'primary','active',?,0,'[]',1,?,?)""",
                (
                    contact_id,
                    account_id,
                    workspace_id,
                    tenant_id,
                    owner["account_id"],
                    owner["workspace_membership_id"],
                    owner["revision"],
                    stamp,
                    stamp,
                ),
            )
        elif (
            int(existing["membership_revision"]) != int(owner["revision"])
            or existing["status"] != "active"
        ):
            connection.execute(
                """UPDATE platform_customer_contacts SET status='active',membership_revision=?,
                      revision=revision+1,updated_at=? WHERE customer_contact_id=?""",
                (owner["revision"], _next_stamp(existing["updated_at"], now), contact_id),
            )
    stale = connection.execute(
        "SELECT * FROM platform_customer_contacts WHERE customer_account_id=? AND status='active'",
        (account_id,),
    ).fetchall()
    for row in stale:
        if row["workspace_membership_id"] not in owner_ids:
            connection.execute(
                """UPDATE platform_customer_contacts SET status='unavailable',revision=revision+1,
                      updated_at=? WHERE customer_contact_id=?""",
                (_next_stamp(row["updated_at"], now), row["customer_contact_id"]),
            )


def _ensure_jobs(connection, account_id, workspace_id, tenant_id, now):
    stamp = _db_time(now)
    for job in _JOBS:
        scheduler_id = "css_" + _sha(f"customer-success-scheduler-v1\x1f{account_id}\x1f{job}")[:32]
        connection.execute(
            """INSERT INTO platform_customer_success_scheduler_state(
              customer_success_scheduler_id,customer_account_id,workspace_id,tenant_id,job_code,
              status,next_run_at,scan_sequence_watermark,scan_time_watermark,
              consecutive_failure_count,fencing_generation,lease_owner_id,lease_token_sha256,
              lease_expires_at,last_error_code,bounded_batch_size,revision,created_at,updated_at)
            VALUES(?,?,?, ?,?,'pending',?,0,'1970-01-01T00:00:00Z',0,0,
              NULL,NULL,NULL,NULL,50,1,?,?) ON CONFLICT DO NOTHING""",
            (scheduler_id, account_id, workspace_id, tenant_id, job, stamp, stamp, stamp),
        )


def _support_counts(connection, account, now):
    stamp = _db_time(now)
    row = connection.execute(
        """SELECT
          count(*) open_count,
          sum(CASE WHEN ticket.severity='p1' THEN 1 ELSE 0 END) p1_count,
          sum(CASE WHEN ticket.severity='p2' THEN 1 ELSE 0 END) p2_count,
          sum(CASE WHEN ticket.status='waiting_platform' THEN 1 ELSE 0 END) waiting_count,
          sum(CASE WHEN clock.state!='stopped' AND clock.risk_at<=?
                    AND clock.resolution_deadline_at>? THEN 1 ELSE 0 END) at_risk_count,
          sum(CASE WHEN clock.state!='stopped' AND clock.resolution_deadline_at<=?
                   THEN 1 ELSE 0 END) breached_count
        FROM support_tickets ticket
        LEFT JOIN support_sla_clocks clock ON clock.support_ticket_id=ticket.support_ticket_id
        WHERE ticket.workspace_id=? AND ticket.tenant_id=?
          AND ticket.status NOT IN('resolved','closed')""",
        (stamp, stamp, stamp, account["workspace_id"], account["tenant_id"]),
    ).fetchone()
    values = tuple(0 if value is None else int(value) for value in row)
    if any(value < 0 for value in values):
        raise ApiError("platform.unavailable")
    return values


def _ticket_score(support):
    open_count, p1, p2, waiting, at_risk, breached = support
    if p1 or breached:
        return 0, "critical"
    if at_risk or p2 >= 3:
        return 2500, "at_risk"
    if waiting and p2 or open_count >= 5:
        return 5000, "watch"
    if open_count:
        return 7500, "watch"
    return 10000, "healthy"


def _weighted(scores):
    numerators = tuple(score * weight for score, weight in zip(scores, SIGNAL_WEIGHTS, strict=True))
    total = (sum(numerators) + 5000) // 10000
    contributions = [value // 10000 for value in numerators]
    remaining = total - sum(contributions)
    order = sorted(range(6), key=lambda index: (-(numerators[index] % 10000), index))
    for index in order[:remaining]:
        contributions[index] += 1
    return total, tuple(contributions)


def _risk(score):
    if score >= 8000:
        return "healthy"
    if score >= 6000:
        return "watch"
    if score >= 3500:
        return "at_risk"
    return "critical"


def _transaction(connection):
    if not connection.in_transaction:
        raise ApiError("platform.unavailable")


def _digest(value):
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _db_time(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _next_stamp(previous, now):
    return _db_time(max(now, _parse_time(previous) + timedelta(seconds=1)))


__all__ = [
    "emit_due_task_events",
    "generate_qbr",
    "materialize_health_v2",
    "reconcile_accounts",
    "reconcile_renewals",
]
