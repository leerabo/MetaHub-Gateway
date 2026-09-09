"""Single fenced supervisor for PRD-145 console-experience workers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database, database_request_scope
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.console_migration.execution import selected_scheduler_work
from metahub_control.workspace_navigation.scheduler import (
    NavigationLeaseProof,
    run_navigation_queue_once,
)

logger = logging.getLogger("metahub.control.console_experience.scheduler")
WORKER_ID = f"console-experience-{uuid4().hex}"
POLL_SECONDS = 2.0
RECONCILIATION_INTERVAL_SECONDS = 300
LEASE_SECONDS = 30
SHUTDOWN_TIMEOUT_SECONDS = 10.0
_WORKER_KINDS = (
    "platform_dashboard_refresh",
    "workspace_dashboard_refresh",
    "platform_approval_projection",
    "workspace_approval_projection",
    "workspace_deployment_diagnostic",
    "platform_outbox",
    "workspace_outbox",
    "account_preference_outbox",
    "reconciliation",
    "cleanup",
)
_NAVIGATION_WORKER_KINDS = (
    "workspace_navigation_projection",
    "role_task_home",
    "advanced_tool_entitlement",
    "workspace_navigation_outbox",
    "navigation_reconciliation",
)
_WORKER_QUEUES = {
    "platform_dashboard_refresh": "platform-dashboard",
    "workspace_dashboard_refresh": "workspace-dashboard",
    "platform_approval_projection": "platform-approval",
    "workspace_approval_projection": "workspace-approval",
    "workspace_deployment_diagnostic": "deployment-diagnostic",
    "platform_outbox": "main",
    "workspace_outbox": "main",
    "account_preference_outbox": "account-preference",
    "reconciliation": "main",
    "cleanup": "main",
    "workspace_navigation_projection": "workspace-navigation",
    "role_task_home": "role-task-home",
    "advanced_tool_entitlement": "advanced-tool",
    "workspace_navigation_outbox": "navigation-outbox",
    "navigation_reconciliation": "navigation-reconciliation",
}


@dataclass(frozen=True, slots=True)
class _SchedulerLease:
    worker_kind: str
    generation: int
    token: str


def install_console_experience_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = asyncio.Event()
            task = asyncio.create_task(
                _loop(current_app.state.config, stop),
                name="console-experience-scheduler",
            )
            try:
                yield
            finally:
                stop.set()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=SHUTDOWN_TIMEOUT_SECONDS)
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _loop(config: AppConfig, stop: asyncio.Event) -> None:
    while not stop.is_set():
        worked = await asyncio.to_thread(run_console_experience_once, config)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05 if worked else POLL_SECONDS)
        except TimeoutError:
            continue


def run_console_experience_once(config: AppConfig) -> bool:
    selected = selected_scheduler_work("console-experience", _WORKER_QUEUES)
    with database_request_scope(reuse_connections=True):
        for worker_kind in _WORKER_KINDS:
            if worker_kind not in selected:
                continue
            lease = _claim(config, worker_kind)
            if lease is None:
                continue
            try:
                worked = _run_worker(config, worker_kind)
                _release(config, lease, worked=worked)
            except ApiError as exc:
                _release_after_failure(config, lease, exc.code)
                _warning(worker_kind, exc.code)
                continue
            except Exception:
                _release_after_failure(config, lease, "platform.unavailable")
                _warning(worker_kind, "platform.unavailable")
                continue
            if worked:
                return True
        if config.workspace_navigation_scheduler_enabled:
            return _run_navigation_workers(config)
    return False


def _run_navigation_workers(config: AppConfig) -> bool:
    selected = selected_scheduler_work("console-experience", _WORKER_QUEUES)
    for queue_code in _NAVIGATION_WORKER_KINDS:
        if queue_code not in selected:
            continue
        lease = _claim_navigation(config, queue_code)
        if lease is None:
            continue
        proof = NavigationLeaseProof(
            queue_code=queue_code,
            lease_owner_id=WORKER_ID,
            fence_generation=lease.generation,
            lease_token_hmac=_navigation_lease_hmac(
                config,
                queue_code,
                lease.token,
            ),
        )
        try:
            worked = run_navigation_queue_once(config, proof)
            _release_navigation(config, lease, worked=worked)
        except ApiError as exc:
            _release_navigation_after_failure(config, lease, exc.code)
            _warning(queue_code, exc.code)
            continue
        except Exception:
            _release_navigation_after_failure(
                config,
                lease,
                "platform.unavailable",
            )
            _warning(queue_code, "platform.unavailable")
            continue
        if worked:
            return True
    return False


def _run_worker(config: AppConfig, worker_kind: str) -> bool:
    if worker_kind == "platform_dashboard_refresh":
        return _refresh_platform_dashboard(config)
    if worker_kind == "workspace_dashboard_refresh":
        return _refresh_workspace_dashboards(config)
    if worker_kind == "platform_approval_projection":
        return False
    if worker_kind == "workspace_approval_projection":
        return _project_workspace_approval(config)
    if worker_kind == "workspace_deployment_diagnostic":
        return _refresh_workspace_deployment_diagnostic(config)
    if worker_kind in {
        "platform_outbox",
        "workspace_outbox",
        "account_preference_outbox",
    }:
        return _drain_outbox(config, worker_kind)
    if worker_kind == "reconciliation":
        _verify_integrity(config)
        return False
    if worker_kind == "cleanup":
        return False
    raise ApiError("platform.unavailable")


def _claim(config: AppConfig, worker_kind: str) -> _SchedulerLease | None:
    connection = _connect(config)
    # A shared caller transaction is not owned by this claim or its cleanup.
    if connection.in_transaction:
        raise ApiError("platform.unavailable")
    query = "SELECT * FROM console_experience_scheduler_state WHERE worker_kind=?"
    try:
        with closing(connection.execute(query, (worker_kind,))) as cursor:
            candidate = cursor.fetchone()
        _rollback(connection)
        if not _claim_is_due(candidate, _db_time(_now())):
            return None
        # End the read snapshot before requesting write authority and rechecking.
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        stamp = _db_time(now)
        with closing(connection.execute(query, (worker_kind,))) as cursor:
            row = cursor.fetchone()
        if not _claim_is_due(row, stamp):
            connection.commit()
            return None
        generation = int(row["fence_generation"]) + 1
        token = uuid4().hex + uuid4().hex
        updated = connection.execute(
            """UPDATE console_experience_scheduler_state
               SET fence_generation=?,lease_owner_id=?,lease_token_hmac=?,
                 lease_expires_at=?,attempt_count=attempt_count+1,updated_at=?
               WHERE worker_kind=? AND enabled=1 AND fence_generation=?
                 AND next_attempt_at<=?
                 AND(lease_expires_at IS NULL OR lease_expires_at<=?)""",
            (
                generation,
                WORKER_ID,
                _lease_hmac(config, worker_kind, token),
                _db_time(now + timedelta(seconds=LEASE_SECONDS)),
                stamp,
                worker_kind,
                row["fence_generation"],
                stamp,
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return None
        connection.commit()
        return _SchedulerLease(worker_kind, generation, token)
    except sqlite3.Error:
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _release(config: AppConfig, lease: _SchedulerLease, *, worked: bool) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        stamp = _db_time(now)
        delay = (
            RECONCILIATION_INTERVAL_SECONDS
            if lease.worker_kind == "reconciliation"
            else 1 if worked else 2
        )
        updated = connection.execute(
            """UPDATE console_experience_scheduler_state
               SET cursor_value=?,lease_owner_id=NULL,lease_token_hmac=NULL,
                 lease_expires_at=NULL,last_error_code=NULL,next_attempt_at=?,updated_at=?
               WHERE worker_kind=? AND fence_generation=? AND lease_owner_id=?
                 AND lease_token_hmac=? AND lease_expires_at>?""",
            (
                f"worked:{stamp}" if worked else f"idle:{stamp}",
                _db_time(now + timedelta(seconds=delay)),
                stamp,
                lease.worker_kind,
                lease.generation,
                WORKER_ID,
                _lease_hmac(config, lease.worker_kind, lease.token),
                stamp,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("platform.unavailable")
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error:
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _release_after_failure(config: AppConfig, lease: _SchedulerLease, error_code: str) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        stamp = _db_time(now)
        connection.execute(
            """UPDATE console_experience_scheduler_state
               SET lease_owner_id=NULL,lease_token_hmac=NULL,lease_expires_at=NULL,
                 last_error_code=?,next_attempt_at=?,updated_at=?
               WHERE worker_kind=? AND fence_generation=? AND lease_owner_id=?
                 AND lease_token_hmac=?""",
            (
                error_code,
                _db_time(now + timedelta(seconds=POLL_SECONDS)),
                stamp,
                lease.worker_kind,
                lease.generation,
                WORKER_ID,
                _lease_hmac(config, lease.worker_kind, lease.token),
            ),
        )
        connection.commit()
    except sqlite3.Error:
        _rollback(connection)
    finally:
        connection.close()


def _claim_navigation(
    config: AppConfig,
    queue_code: str,
) -> _SchedulerLease | None:
    connection = _connect(config)
    if connection.in_transaction:
        raise ApiError("platform.unavailable")
    query = "SELECT * FROM workspace_navigation_scheduler_state WHERE queue_code=?"
    try:
        with closing(connection.execute(query, (queue_code,))) as cursor:
            candidate = cursor.fetchone()
        _rollback(connection)
        if not _claim_is_due(candidate, _db_time(_now())):
            return None
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        stamp = _db_time(now)
        with closing(connection.execute(query, (queue_code,))) as cursor:
            row = cursor.fetchone()
        if not _claim_is_due(row, stamp):
            connection.commit()
            return None
        generation = int(row["fence_generation"]) + 1
        token = uuid4().hex + uuid4().hex
        updated = connection.execute(
            """UPDATE workspace_navigation_scheduler_state
               SET fence_generation=?,lease_owner_id=?,lease_token_hmac=?,
                 lease_expires_at=?,attempt_count=attempt_count+1,updated_at=?
               WHERE queue_code=? AND enabled=1 AND fence_generation=?
                 AND next_attempt_at<=?
                 AND(lease_expires_at IS NULL OR lease_expires_at<=?)""",
            (
                generation,
                WORKER_ID,
                _navigation_lease_hmac(config, queue_code, token),
                _db_time(now + timedelta(seconds=LEASE_SECONDS)),
                stamp,
                queue_code,
                row["fence_generation"],
                stamp,
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return None
        connection.commit()
        return _SchedulerLease(queue_code, generation, token)
    except sqlite3.Error:
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _claim_is_due(row: sqlite3.Row | None, stamp: str) -> bool:
    return (
        row is not None
        and bool(row["enabled"])
        and row["next_attempt_at"] <= stamp
        and (row["lease_expires_at"] is None or row["lease_expires_at"] <= stamp)
    )


def _release_navigation(
    config: AppConfig,
    lease: _SchedulerLease,
    *,
    worked: bool,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        stamp = _db_time(now)
        updated = connection.execute(
            """UPDATE workspace_navigation_scheduler_state
               SET cursor_value=?,source_watermark=?,lease_owner_id=NULL,
                 lease_token_hmac=NULL,lease_expires_at=NULL,last_error_code=NULL,
                 next_attempt_at=?,updated_at=?
               WHERE queue_code=? AND fence_generation=? AND lease_owner_id=?
                 AND lease_token_hmac=? AND lease_expires_at>?""",
            (
                f"worked:{stamp}" if worked else f"idle:{stamp}",
                stamp,
                _db_time(now + timedelta(seconds=1 if worked else 2)),
                stamp,
                lease.worker_kind,
                lease.generation,
                WORKER_ID,
                _navigation_lease_hmac(
                    config,
                    lease.worker_kind,
                    lease.token,
                ),
                stamp,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("platform.unavailable")
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error:
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _release_navigation_after_failure(
    config: AppConfig,
    lease: _SchedulerLease,
    error_code: str,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        stamp = _db_time(now)
        connection.execute(
            """UPDATE workspace_navigation_scheduler_state
               SET lease_owner_id=NULL,lease_token_hmac=NULL,lease_expires_at=NULL,
                 last_error_code=?,next_attempt_at=?,updated_at=?
               WHERE queue_code=? AND fence_generation=? AND lease_owner_id=?
                 AND lease_token_hmac=?""",
            (
                error_code,
                _db_time(now + timedelta(seconds=5)),
                stamp,
                lease.worker_kind,
                lease.generation,
                WORKER_ID,
                _navigation_lease_hmac(
                    config,
                    lease.worker_kind,
                    lease.token,
                ),
            ),
        )
        connection.commit()
    except sqlite3.Error:
        _rollback(connection)
    finally:
        connection.close()


def _refresh_platform_dashboard(config: AppConfig) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        metrics = _platform_dashboard_metrics(connection)
        content_sha256 = _sha(metrics)
        current = connection.execute(
            """SELECT version.content_sha256,head.generation,head.revision
               FROM platform_dashboard_projection_heads head
               JOIN platform_dashboard_projection_versions version
                 USING(platform_dashboard_projection_id,generation)
               WHERE head.head_key='platform'"""
        ).fetchone()
        if current is not None and hmac.compare_digest(current["content_sha256"], content_sha256):
            connection.commit()
            return False
        generation = 1 if current is None else int(current["generation"]) + 1
        head_revision = 1 if current is None else int(current["revision"]) + 1
        projection_id = _id("pdp_")
        stamp = _db_time(_now())
        connection.execute(
            """INSERT INTO platform_dashboard_projection_versions(
                 platform_dashboard_projection_id,generation,schema_version,status,
                 content_sha256,observed_at,created_at)
               VALUES(?,?,'platform-dashboard-v1','ready',?,?,?)""",
            (projection_id, generation, content_sha256, stamp, stamp),
        )
        for metric in metrics:
            connection.execute(
                """INSERT INTO platform_dashboard_projection_sources(
                     platform_dashboard_projection_id,generation,source_code,metric_code,
                     metric_value,state_code,action_code,freshness,source_revision,
                     source_watermark,reason_code)
                   VALUES(?,?,?,?,?,?,?,'fresh',?,?,NULL)""",
                (
                    projection_id,
                    generation,
                    metric["source_code"],
                    metric["metric_code"],
                    metric["value"],
                    metric["state_code"],
                    metric["action_code"],
                    metric["source_revision"],
                    metric["source_watermark"],
                ),
            )
        connection.execute(
            """INSERT INTO platform_dashboard_projection_heads(
                 head_key,platform_dashboard_projection_id,generation,revision,updated_at)
               VALUES('platform',?,?,?,?)
               ON CONFLICT(head_key) DO UPDATE SET
                 platform_dashboard_projection_id=excluded.platform_dashboard_projection_id,
                 generation=excluded.generation,revision=excluded.revision,
                 updated_at=excluded.updated_at""",
            (projection_id, generation, head_revision, stamp),
        )
        _insert_console_event(
            connection,
            domain="platform",
            owner=None,
            event_type="platform_dashboard.refreshed",
            object_type="dashboard",
            object_id=projection_id,
            payload={"generation": generation, "status": "ready"},
            stamp=stamp,
        )
        connection.commit()
        return True
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _refresh_workspace_dashboards(config: AppConfig) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        owners = connection.execute(
            """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
               ORDER BY workspace_id,tenant_id"""
        ).fetchall()
        changed = False
        for owner in owners:
            changed = (
                _refresh_workspace_dashboard(connection, owner["workspace_id"], owner["tenant_id"])
                or changed
            )
        connection.commit()
        return changed
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def _refresh_workspace_dashboard(
    connection: sqlite3.Connection, workspace_id: str, tenant_id: str
) -> bool:
    metrics = _workspace_dashboard_metrics(connection, workspace_id, tenant_id)
    content_sha256 = _sha(metrics)
    current = connection.execute(
        """SELECT version.content_sha256,head.generation,head.revision
           FROM workspace_dashboard_projection_heads head
           JOIN workspace_dashboard_projection_versions version
             ON version.workspace_id=head.workspace_id
            AND version.tenant_id=head.tenant_id
            AND version.workspace_dashboard_projection_id=
                head.workspace_dashboard_projection_id
            AND version.generation=head.generation
           WHERE head.workspace_id=? AND head.tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()
    if current is not None and hmac.compare_digest(current["content_sha256"], content_sha256):
        return False
    generation = 1 if current is None else int(current["generation"]) + 1
    head_revision = 1 if current is None else int(current["revision"]) + 1
    projection_id = _id("wdp_")
    stamp = _db_time(_now())
    connection.execute(
        """INSERT INTO workspace_dashboard_projection_versions(
             workspace_dashboard_projection_id,workspace_id,tenant_id,generation,
             schema_version,status,content_sha256,observed_at,created_at)
           VALUES(?,?,?,?,'workspace-dashboard-v1','ready',?,?,?)""",
        (projection_id, workspace_id, tenant_id, generation, content_sha256, stamp, stamp),
    )
    for metric in metrics:
        connection.execute(
            """INSERT INTO workspace_dashboard_projection_sources(
                 workspace_dashboard_projection_id,workspace_id,tenant_id,generation,
                 source_code,metric_code,metric_value,state_code,action_code,freshness,
                 source_revision,source_watermark,reason_code)
               VALUES(?,?,?,?,?,?,?,?,?,'fresh',?,?,NULL)""",
            (
                projection_id,
                workspace_id,
                tenant_id,
                generation,
                metric["source_code"],
                metric["metric_code"],
                metric["value"],
                metric["state_code"],
                metric["action_code"],
                metric["source_revision"],
                metric["source_watermark"],
            ),
        )
    connection.execute(
        """INSERT INTO workspace_dashboard_projection_heads(
             workspace_id,tenant_id,workspace_dashboard_projection_id,generation,
             revision,updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(workspace_id,tenant_id) DO UPDATE SET
             workspace_dashboard_projection_id=excluded.workspace_dashboard_projection_id,
             generation=excluded.generation,revision=excluded.revision,
             updated_at=excluded.updated_at""",
        (workspace_id, tenant_id, projection_id, generation, head_revision, stamp),
    )
    _insert_console_event(
        connection,
        domain="workspace",
        owner=(workspace_id, tenant_id),
        event_type="workspace_dashboard.refreshed",
        object_type="dashboard",
        object_id=projection_id,
        payload={"generation": generation, "status": "ready"},
        stamp=stamp,
    )
    return True


def _platform_dashboard_metrics(connection: sqlite3.Connection) -> list[dict[str, object]]:
    sources = (
        ("status", "SELECT count(*) FROM status_components", "status-incidents"),
        ("observability", "SELECT count(*) FROM service_level_objectives", "observability"),
        ("jobs", "SELECT count(*) FROM platform_jobs", "job-runtime"),
        ("capacity", "SELECT count(*) FROM capacity_service_states", "capacity"),
        ("channel", "SELECT count(*) FROM channel_statements", "channels"),
        (
            "customer_success",
            "SELECT count(*) FROM platform_customer_account_projections",
            "customer-success",
        ),
        (
            "growth",
            "SELECT count(*) FROM platform_growth_funnel_snapshots",
            "growth",
        ),
        ("branding", "SELECT count(*) FROM platform_brands", "branding"),
        (
            "provider_engineering",
            "SELECT count(*) FROM provider_adapter_releases",
            "provider-engineering",
        ),
        (
            "commercial_accounting",
            "SELECT count(*) FROM platform_invoices",
            "commercial-accounting",
        ),
    )
    return [
        _metric(source_code, int(connection.execute(sql).fetchone()[0]), action_code)
        for source_code, sql, action_code in sources
    ]


def _workspace_dashboard_metrics(
    connection: sqlite3.Connection, workspace_id: str, tenant_id: str
) -> list[dict[str, object]]:
    counts = (
        (
            "role_tasks",
            connection.execute(
                """SELECT count(*) FROM workspace_memberships
                   WHERE workspace_id=? AND tenant_id=?""",
                (workspace_id, tenant_id),
            ).fetchone()[0],
            "overview",
        ),
        (
            "gateway_health",
            connection.execute(
                """SELECT count(*) FROM edge_deployments
                   WHERE workspace_id=? AND tenant_id=?""",
                (workspace_id, tenant_id),
            ).fetchone()[0],
            "private-gateways",
        ),
        (
            "provider_health",
            connection.execute(
                "SELECT count(*) FROM provider_health_probe_logs WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()[0],
            "provider-health",
        ),
        (
            "governance",
            connection.execute(
                "SELECT count(*) FROM dlp_rules WHERE tenant_id=?", (tenant_id,)
            ).fetchone()[0],
            "dlp-rules",
        ),
        (
            "approvals",
            connection.execute(
                "SELECT count(*) FROM approval_tasks WHERE tenant_id=?", (tenant_id,)
            ).fetchone()[0],
            "approvals",
        ),
        (
            "usage_cost",
            connection.execute(
                "SELECT count(*) FROM usage_ledger WHERE tenant_id=?", (tenant_id,)
            ).fetchone()[0],
            "usage",
        ),
        (
            "notifications",
            connection.execute(
                """SELECT count(*) FROM notifications
                   WHERE workspace_id=? AND tenant_id=?""",
                (workspace_id, tenant_id),
            ).fetchone()[0],
            "overview",
        ),
        (
            "deployment",
            connection.execute(
                """SELECT count(*) FROM edge_deployments
                   WHERE workspace_id=? AND tenant_id=?""",
                (workspace_id, tenant_id),
            ).fetchone()[0],
            "deployment-summary",
        ),
    )
    return [_metric(source, int(count), action) for source, count, action in counts]


def _metric(source_code: str, count: int, action_code: str) -> dict[str, object]:
    return {
        "source_code": source_code,
        "metric_code": f"{source_code}.records",
        "value": count,
        "state_code": "ready" if count else "empty",
        "action_code": action_code,
        "source_revision": count,
        "source_watermark": f"rows:{count}",
    }


def _project_workspace_approval(config: AppConfig) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT task.*,binding.workspace_id,link.account_id creator_account_id
               FROM approval_tasks task
               JOIN workspace_tenant_bindings binding ON binding.tenant_id=task.tenant_id
               LEFT JOIN account_identity_links link
                 ON link.legacy_tenant_id=task.tenant_id
                AND link.legacy_user_id=task.created_by_id AND link.status='active'
               ORDER BY task.updated_at,task.approval_task_id
               LIMIT 100"""
        ).fetchall()
        stamp = _db_time(_now())
        for row in rows:
            source_id_digest = _pseudonym(config, "workspace-approval-source", row["source_id"])
            projection_id = _stable_id("wdq_", f"workspace-approval:{row['approval_task_id']}")
            status = (
                "expired"
                if row["status"] == "pending" and row["expires_at"] <= stamp
                else row["status"]
            )
            applicant_id = row["creator_account_id"] or row["created_by_id"]
            applicant_domain = "workspace" if row["created_by_type"] == "user" else "service"
            applicant_pseudonym = _pseudonym(config, applicant_domain, applicant_id)
            projection = {
                "applicant_domain": applicant_domain,
                "applicant_pseudonym": applicant_pseudonym,
                "deadline_at": row["expires_at"],
                "decision_contract_code": (f"{row['target_type']}.{row['target_action']}"),
                "required_permission_code": row["required_permission_code"],
                "risk_band": row["risk_level"],
                "source_domain": row["source_type"],
                "source_id_digest": source_id_digest,
                "source_revision": int(row["state_revision"]),
                "source_type": row["target_type"],
                "status": status,
                "summary_code": f"approval.{row['source_type']}.{status}",
            }
            projection_sha256 = _sha(projection)
            current = connection.execute(
                """SELECT source_revision,projection_sha256
                   FROM workspace_domain_approval_queue_items
                   WHERE workspace_approval_projection_id=?""",
                (projection_id,),
            ).fetchone()
            if current is not None and int(current["source_revision"]) > int(row["state_revision"]):
                rejected = connection.execute(
                    """INSERT OR IGNORE INTO workspace_domain_approval_rejections(
                         rejection_id,workspace_id,tenant_id,source_domain,source_type,
                         source_id_digest,source_revision,actor_domain,reason_code,created_at)
                       VALUES(?,?,?,?,?,?,?,'service','source_revision_regressed',?)""",
                    (
                        _stable_id(
                            "war_",
                            "workspace-approval-rejection:"
                            f"{row['approval_task_id']}:{row['state_revision']}",
                        ),
                        row["workspace_id"],
                        row["tenant_id"],
                        projection["source_domain"],
                        projection["source_type"],
                        source_id_digest,
                        projection["source_revision"],
                        stamp,
                    ),
                ).rowcount
                if rejected:
                    _insert_console_event(
                        connection,
                        domain="workspace",
                        owner=(row["workspace_id"], row["tenant_id"]),
                        event_type="approval.domain_rejected",
                        object_type="approval_projection",
                        object_id=projection_id,
                        payload={
                            "reason_code": "source_revision_regressed",
                            "source_domain": projection["source_domain"],
                            "source_revision": projection["source_revision"],
                            "source_type": projection["source_type"],
                        },
                        stamp=stamp,
                    )
                    connection.commit()
                    return True
                continue
            if current is not None and (
                int(current["source_revision"]) == int(row["state_revision"])
                and hmac.compare_digest(current["projection_sha256"], projection_sha256)
            ):
                continue
            connection.execute(
                """INSERT INTO workspace_domain_approval_queue_items(
                     workspace_approval_projection_id,workspace_id,tenant_id,source_domain,
                     source_type,source_id_digest,source_revision,status,risk_band,
                     applicant_domain,applicant_pseudonym,required_permission_code,
                     summary_code,decision_contract_code,projection_generation,
                     projection_sha256,deadline_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(workspace_approval_projection_id) DO UPDATE SET
                     source_revision=excluded.source_revision,status=excluded.status,
                     risk_band=excluded.risk_band,applicant_domain=excluded.applicant_domain,
                     applicant_pseudonym=excluded.applicant_pseudonym,
                     required_permission_code=excluded.required_permission_code,
                     summary_code=excluded.summary_code,
                     decision_contract_code=excluded.decision_contract_code,
                     projection_generation=excluded.projection_generation,
                     projection_sha256=excluded.projection_sha256,
                     deadline_at=excluded.deadline_at,updated_at=excluded.updated_at""",
                (
                    projection_id,
                    row["workspace_id"],
                    row["tenant_id"],
                    projection["source_domain"],
                    projection["source_type"],
                    source_id_digest,
                    projection["source_revision"],
                    status,
                    projection["risk_band"],
                    applicant_domain,
                    applicant_pseudonym,
                    projection["required_permission_code"],
                    projection["summary_code"],
                    projection["decision_contract_code"],
                    projection["source_revision"],
                    projection_sha256,
                    projection["deadline_at"],
                    row["created_at"],
                    stamp,
                ),
            )
            connection.execute(
                """INSERT INTO workspace_domain_approval_source_cursors(
                     workspace_id,tenant_id,source_domain,source_type,source_revision,
                     source_watermark,generation,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(workspace_id,tenant_id,source_domain,source_type)
                   DO UPDATE SET
                     source_revision=max(source_revision,excluded.source_revision),
                     source_watermark=CASE
                       WHEN excluded.source_revision>=source_revision
                       THEN excluded.source_watermark ELSE source_watermark END,
                     generation=max(generation,excluded.generation),
                     updated_at=CASE
                       WHEN excluded.source_revision>=source_revision
                       THEN excluded.updated_at ELSE updated_at END""",
                (
                    row["workspace_id"],
                    row["tenant_id"],
                    row["source_type"],
                    row["target_type"],
                    row["state_revision"],
                    f"approval:{row['state_revision']}",
                    row["state_revision"],
                    stamp,
                ),
            )
            connection.commit()
            return True
        connection.commit()
        return False
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def _refresh_workspace_deployment_diagnostic(config: AppConfig) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT deployment.*,
                 coalesce((SELECT max(identity.generation)
                   FROM edge_device_identities identity
                   WHERE identity.workspace_id=deployment.workspace_id
                     AND identity.tenant_id=deployment.tenant_id
                     AND identity.edge_deployment_id=deployment.edge_deployment_id),0)
                   identity_generation
               FROM edge_deployments deployment
               ORDER BY deployment.updated_at,deployment.edge_deployment_id"""
        ).fetchall()
        stamp = _db_time(_now())
        for row in rows:
            revision = int(row["revision"])
            projection_id = _stable_id(
                "ddp_",
                f"deployment-diagnostic:{row['edge_deployment_id']}:{revision}",
            )
            if (
                connection.execute(
                    """SELECT 1 FROM workspace_deployment_diagnostic_versions
                   WHERE deployment_diagnostic_projection_id=?""",
                    (projection_id,),
                ).fetchone()
                is not None
            ):
                continue
            health_state = {
                "healthy": "healthy",
                "certificate_expiring": "degraded",
                "offline": "offline",
                "revoked": "revoked",
                "not_registered": "unknown",
                "unknown": "unknown",
            }[row["health_code"]]
            release_band = (
                "current"
                if row["status"] == "active" and row["agent_version"] is not None
                else "unknown"
            )
            compatibility_band = (
                "blocked"
                if row["status"] == "revoked"
                else "compatible"
                if health_state == "healthy"
                else "attention"
                if health_state in {"degraded", "offline"}
                else "unknown"
            )
            action_code = {
                "degraded": "deployment.rotate_certificate",
                "offline": "deployment.reconnect",
                "unknown": "deployment.register",
            }.get(health_state)
            source_watermarks = {
                "deployment_revision": revision,
                "identity_generation": int(row["identity_generation"]),
            }
            evidence_digest = _sha(
                {
                    "compatibility_band": compatibility_band,
                    "health_state": health_state,
                    "release_band": release_band,
                    "source_watermarks": source_watermarks,
                }
            )
            connection.execute(
                """INSERT INTO workspace_deployment_diagnostic_versions(
                     deployment_diagnostic_projection_id,workspace_id,tenant_id,
                     edge_deployment_id,projection_revision,deployment_kind,region_code,
                     release_band,compatibility_band,desired_generation,
                     observed_generation,health_state,reason_code,action_code,evidence_digest,
                     source_watermarks_json,observed_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    projection_id,
                    row["workspace_id"],
                    row["tenant_id"],
                    row["edge_deployment_id"],
                    revision,
                    row["mode"],
                    row["region_code"],
                    release_band,
                    compatibility_band,
                    row["revocation_generation"],
                    row["identity_generation"],
                    health_state,
                    f"deployment.{row['health_code']}",
                    action_code,
                    evidence_digest,
                    _json(source_watermarks),
                    row["updated_at"],
                    stamp,
                ),
            )
            head = connection.execute(
                """SELECT head_revision FROM workspace_deployment_diagnostic_heads
                   WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?""",
                (row["workspace_id"], row["tenant_id"], row["edge_deployment_id"]),
            ).fetchone()
            head_revision = 1 if head is None else int(head["head_revision"]) + 1
            connection.execute(
                """INSERT INTO workspace_deployment_diagnostic_heads(
                     workspace_id,tenant_id,edge_deployment_id,
                     deployment_diagnostic_projection_id,projection_revision,
                     head_revision,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(workspace_id,tenant_id,edge_deployment_id) DO UPDATE SET
                     deployment_diagnostic_projection_id=
                       excluded.deployment_diagnostic_projection_id,
                     projection_revision=excluded.projection_revision,
                     head_revision=excluded.head_revision,updated_at=excluded.updated_at""",
                (
                    row["workspace_id"],
                    row["tenant_id"],
                    row["edge_deployment_id"],
                    projection_id,
                    revision,
                    head_revision,
                    stamp,
                ),
            )
            connection.commit()
            return True
        connection.commit()
        return False
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def _insert_console_event(
    connection: sqlite3.Connection,
    *,
    domain: str,
    owner: tuple[str, str] | None,
    event_type: str,
    object_type: str,
    object_id: str,
    payload: dict[str, object],
    stamp: str,
) -> None:
    payload_json = _json(payload)
    event_key = _sha(
        {
            "domain": domain,
            "event_type": event_type,
            "object_id": object_id,
            "payload": payload,
        }
    )
    if domain == "platform":
        event_id = _id("pce_")
        connection.execute(
            """INSERT INTO platform_console_events(
                 platform_console_event_id,event_type,object_type,object_id,
                 event_key_sha256,payload_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (event_id, event_type, object_type, object_id, event_key, payload_json, stamp),
        )
        connection.execute(
            """INSERT INTO platform_console_event_outbox(
                 platform_console_event_outbox_id,platform_console_event_id,status,
                 attempt_count,next_attempt_at,lease_owner_id,lease_generation,
                 lease_token_hmac,lease_expires_at,dispatched_at,created_at,updated_at)
               VALUES(?,?,'pending',0,?,NULL,0,NULL,NULL,NULL,?,?)""",
            (_id("pco_"), event_id, stamp, stamp, stamp),
        )
        return
    if domain != "workspace" or owner is None:
        raise ValueError("invalid console event owner")
    event_id = _id("wce_")
    connection.execute(
        """INSERT INTO workspace_console_events(
             workspace_console_event_id,workspace_id,tenant_id,event_type,object_type,
             object_id,event_key_sha256,payload_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            owner[0],
            owner[1],
            event_type,
            object_type,
            object_id,
            event_key,
            payload_json,
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO workspace_console_event_outbox(
             workspace_console_event_outbox_id,workspace_console_event_id,status,
             attempt_count,next_attempt_at,lease_owner_id,lease_generation,
             lease_token_hmac,lease_expires_at,dispatched_at,created_at,updated_at)
           VALUES(?,?,'pending',0,?,NULL,0,NULL,NULL,NULL,?,?)""",
        (_id("wco_"), event_id, stamp, stamp, stamp),
    )


def _drain_outbox(config: AppConfig, worker_kind: str) -> bool:
    contracts = {
        "platform_outbox": (
            "platform_console_event_outbox",
            "platform_console_event_outbox_id",
            "platform_console_events",
            "platform_console_event_id",
        ),
        "workspace_outbox": (
            "workspace_console_event_outbox",
            "workspace_console_event_outbox_id",
            "workspace_console_events",
            "workspace_console_event_id",
        ),
        "account_preference_outbox": (
            "account_preference_event_outbox",
            "account_preference_event_outbox_id",
            "account_preference_events",
            "account_preference_event_id",
        ),
    }
    try:
        outbox_table, outbox_id_column, event_table, event_id_column = contracts[worker_kind]
    except KeyError:
        raise ApiError("platform.unavailable") from None
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        stamp = _db_time(now)
        payload_column = (
            "event.payload_json" if worker_kind != "account_preference_outbox" else "NULL"
        )
        row = connection.execute(
            f"""SELECT outbox.*,event.event_type,{payload_column} payload_json
                FROM {outbox_table} outbox
                JOIN {event_table} event USING({event_id_column})
                WHERE outbox.status IN('pending','failed')
                  AND outbox.next_attempt_at<=? AND outbox.attempt_count<20
                  AND(outbox.lease_expires_at IS NULL OR outbox.lease_expires_at<=?)
                ORDER BY outbox.next_attempt_at,outbox.created_at,{outbox_id_column}
                LIMIT 1""",
            (stamp, stamp),
        ).fetchone()
        if row is None:
            connection.commit()
            return False
        generation = int(row["lease_generation"]) + 1
        token = uuid4().hex + uuid4().hex
        token_hmac = _outbox_hmac(config, worker_kind, token)
        updated = connection.execute(
            f"""UPDATE {outbox_table}
                SET status='processing',attempt_count=attempt_count+1,
                  lease_generation=?,lease_owner_id=?,lease_token_hmac=?,
                  lease_expires_at=?,updated_at=?
                WHERE {outbox_id_column}=? AND status IN('pending','failed')
                  AND lease_generation=?
                  AND(lease_expires_at IS NULL OR lease_expires_at<=?)""",
            (
                generation,
                WORKER_ID,
                token_hmac,
                _db_time(now + timedelta(seconds=LEASE_SECONDS)),
                stamp,
                row[outbox_id_column],
                row["lease_generation"],
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return False
        payload_json = row["payload_json"]
        if payload_json is not None:
            try:
                payload = json.loads(payload_json)
                if type(payload) is not dict or _json(payload) != payload_json:
                    raise ValueError("invalid outbox payload")
            except (ValueError, UnicodeError, json.JSONDecodeError):
                failed = connection.execute(
                    f"""UPDATE {outbox_table}
                        SET status='failed',next_attempt_at=?,lease_owner_id=NULL,
                          lease_token_hmac=NULL,lease_expires_at=NULL,updated_at=?
                        WHERE {outbox_id_column}=? AND status='processing'
                          AND lease_generation=? AND lease_owner_id=?
                          AND lease_token_hmac=?""",
                    (
                        _db_time(now + timedelta(minutes=5)),
                        stamp,
                        row[outbox_id_column],
                        generation,
                        WORKER_ID,
                        token_hmac,
                    ),
                ).rowcount
                if failed != 1:
                    raise ApiError("platform.unavailable") from None
                connection.commit()
                raise ApiError("platform.unavailable") from None
        completed = connection.execute(
            f"""UPDATE {outbox_table}
                SET status='dispatched',lease_owner_id=NULL,lease_token_hmac=NULL,
                  lease_expires_at=NULL,dispatched_at=?,updated_at=?
                WHERE {outbox_id_column}=? AND status='processing'
                  AND lease_generation=? AND lease_owner_id=? AND lease_token_hmac=?
                  AND lease_expires_at>?""",
            (
                stamp,
                stamp,
                row[outbox_id_column],
                generation,
                WORKER_ID,
                token_hmac,
                stamp,
            ),
        ).rowcount
        if completed != 1:
            raise ApiError("platform.unavailable")
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error:
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _verify_integrity(config: AppConfig) -> None:
    connection = _connect(config)
    try:
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if foreign_keys or integrity is None or integrity[0] != "ok":
            raise ApiError("platform.unavailable")
    except sqlite3.Error:
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _lease_hmac(config: AppConfig, worker_kind: str, token: str) -> str:
    return hmac.new(
        config.workspace_session_secret.encode(),
        f"prd145/scheduler/{worker_kind}/v1\x00{token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _outbox_hmac(config: AppConfig, worker_kind: str, token: str) -> str:
    return hmac.new(
        config.workspace_session_secret.encode(),
        f"prd145/outbox/{worker_kind}/v1\x00{token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _navigation_lease_hmac(
    config: AppConfig,
    queue_code: str,
    token: str,
) -> str:
    return hmac.new(
        config.workspace_session_secret.encode(),
        f"prd146/scheduler/{queue_code}/v1\x00{token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _pseudonym(config: AppConfig, namespace: str, value: str) -> str:
    return hmac.new(
        config.workspace_session_secret.encode(),
        f"prd145/{namespace}/pseudonym/v1\x00{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform.unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _warning(worker_kind: str, error_code: str) -> None:
    logger.warning(
        "console experience iteration failed",
        extra={
            "event": "console_experience.worker_failed",
            "error_code": error_code,
            "worker_kind": worker_kind,
        },
    )


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _stable_id(prefix: str, seed: str) -> str:
    return prefix + hashlib.sha256(seed.encode()).hexdigest()[:32]


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["install_console_experience_scheduler", "run_console_experience_once"]
