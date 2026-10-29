"""Fenced PRD-146 backfill and projection workers."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.workspace_access.service import (
    workspace_actor_permissions_in_transaction,
)

from .catalog import ADVANCED_TOOL_SPECS, CONTRACT_SPECS
from .service import (
    _ensure_advanced_entitlements,
    _ensure_contract,
    _ensure_projection,
    _ensure_task_home,
    _sha,
    _visible_specs,
)

_SYSTEM_REQUEST_ID = "scheduler:workspace-navigation"
_SYSTEM_TRACE_ID = "scheduler:workspace-navigation"
_PRIMARY_PLACEMENTS = frozenset({"primary", "task_home"})


@dataclass(frozen=True, slots=True)
class NavigationLeaseProof:
    queue_code: str
    lease_owner_id: str
    fence_generation: int
    lease_token_hmac: str


@dataclass(frozen=True, slots=True)
class _TaskActor:
    account_id: str
    workspace_id: str
    tenant_id: str
    membership_id: str
    membership_role: str
    permission_codes: tuple[str, ...]
    permission_revision: int
    authorization_fingerprint: str


def run_navigation_queue_once(
    config: AppConfig,
    proof: NavigationLeaseProof,
) -> bool:
    workers = {
        "workspace_navigation_projection": _refresh_projection,
        "role_task_home": _refresh_role_task_home,
        "advanced_tool_entitlement": _refresh_advanced_entitlement,
        "workspace_navigation_outbox": _drain_outbox,
        "navigation_reconciliation": _reconcile_workspace,
    }
    worker = workers.get(proof.queue_code)
    if worker is None:
        raise ApiError("platform.unavailable")
    return worker(config, proof)


def _refresh_projection(config: AppConfig, proof: NavigationLeaseProof) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_lease(connection, proof)
        row = connection.execute(
            """SELECT binding.workspace_id,binding.tenant_id
               FROM workspace_tenant_bindings binding
               WHERE binding.status='active' AND(
                 NOT EXISTS(
                   SELECT 1 FROM workspace_navigation_contract_heads head
                   WHERE head.workspace_id=binding.workspace_id
                     AND head.tenant_id=binding.tenant_id)
                 OR EXISTS(
                   SELECT 1 FROM user_preferences preference
                   JOIN account_identity_links link
                     ON link.legacy_tenant_id=preference.tenant_id
                    AND link.legacy_user_id=preference.user_id
                    AND link.status='active'
                   JOIN workspace_memberships member
                     ON member.account_id=link.account_id
                    AND member.tenant_id=preference.tenant_id
                    AND member.workspace_id=binding.workspace_id
                    AND member.status='active'
                    AND member.role_code!='service_account'
                   WHERE preference.tenant_id=binding.tenant_id
                     AND NOT EXISTS(
                       SELECT 1
                       FROM workspace_navigation_preference_source_mappings mapping
                       WHERE mapping.source_preference_id=preference.preference_id
                         AND mapping.workspace_membership_id=
                           member.workspace_membership_id)))
               ORDER BY binding.workspace_id LIMIT 1"""
        ).fetchone()
        if row is None:
            _update_projection_cursors(connection)
            _require_lease(connection, proof)
            connection.commit()
            return False
        contract = _ensure_contract(
            connection,
            str(row["workspace_id"]),
            str(row["tenant_id"]),
        )
        projection = _ensure_projection(connection, contract)
        _backfill_preference(
            connection,
            config,
            str(row["workspace_id"]),
            str(row["tenant_id"]),
            contract,
            projection,
        )
        _update_projection_cursors(connection)
        _require_lease(connection, proof)
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _backfill_preference(
    connection: sqlite3.Connection,
    config: AppConfig,
    workspace_id: str,
    tenant_id: str,
    contract: sqlite3.Row,
    projection: sqlite3.Row,
) -> bool:
    row = connection.execute(
        """SELECT preference.*,link.account_id,member.workspace_membership_id,
             member.role_code,member.revision membership_revision
           FROM user_preferences preference
           JOIN account_identity_links link
             ON link.legacy_tenant_id=preference.tenant_id
            AND link.legacy_user_id=preference.user_id AND link.status='active'
           JOIN workspace_memberships member
             ON member.account_id=link.account_id
            AND member.tenant_id=preference.tenant_id
            AND member.workspace_id=?
            AND member.status='active' AND member.role_code!='service_account'
           WHERE preference.tenant_id=?
             AND NOT EXISTS(
               SELECT 1 FROM workspace_navigation_preference_source_mappings mapping
               WHERE mapping.source_preference_id=preference.preference_id
                 AND mapping.workspace_membership_id=member.workspace_membership_id)
           ORDER BY preference.preference_id LIMIT 1""",
        (workspace_id, tenant_id),
    ).fetchone()
    if row is None:
        return False
    actor = workspace_actor_permissions_in_transaction(
        connection,
        account_id=str(row["account_id"]),
        workspace_id=workspace_id,
        tenant_id=tenant_id,
    )
    visible = _visible_specs(
        connection, projection, actor.permission_codes, actor.membership_role,
    )
    visible_primary = {item.route_code for item in visible if item.placement in _PRIMARY_PLACEMENTS}
    source_id = str(row["active_navigation_item_id"])
    source_spec = next(
        (item for item in CONTRACT_SPECS if item.source_id == source_id),
        None,
    )
    fallback = (
        "workspace.home.overview"
        if "workspace.home.overview" in visible_primary
        else next(iter(sorted(visible_primary)), None)
    )
    preference_route = fallback
    mapped_route = fallback
    mapping_code = "fallback"
    if (
        source_spec is not None
        and source_spec.placement in _PRIMARY_PLACEMENTS
        and source_spec.route_code in visible_primary
    ):
        preference_route = source_spec.route_code
        mapped_route = source_spec.route_code
        mapping_code = "preserved"
    elif source_spec is not None:
        mapped_route = source_spec.route_code
        mapping_code = {
            "detail": "detail_parent",
            "advanced": "advanced_parent",
            "account": "account_parent",
            "retired_internal": "retired",
        }.get(source_spec.placement, "fallback")
    current = connection.execute(
        """SELECT 1 FROM workspace_navigation_preferences
           WHERE workspace_id=? AND tenant_id=? AND account_id=? AND status='active'""",
        (workspace_id, tenant_id, row["account_id"]),
    ).fetchone()
    stamp = _db_time(_now())
    if current is None:
        connection.execute(
            """INSERT INTO workspace_navigation_preferences(
                 navigation_preference_id,workspace_id,tenant_id,account_id,
                 workspace_membership_id,membership_revision,last_primary_route_code,
                 expanded_group_code,sidebar_collapsed,contract_snapshot_id,status,
                 revision,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,NULL,?,?,'active',1,?,?)""",
            (
                _stable_id(
                    "nvp_",
                    f"backfill:{workspace_id}:{row['account_id']}",
                ),
                workspace_id,
                tenant_id,
                row["account_id"],
                row["workspace_membership_id"],
                int(row["membership_revision"]),
                preference_route,
                int(bool(row["sidebar_collapsed"])),
                contract["navigation_contract_snapshot_id"],
                stamp,
                stamp,
            ),
        )
    source_sha = _sha(
        {
            "preference_id": row["preference_id"],
            "tenant_id": row["tenant_id"],
            "user_id": row["user_id"],
            "active_navigation_item_id": row["active_navigation_item_id"],
            "sidebar_collapsed": int(row["sidebar_collapsed"]),
            "updated_at": row["updated_at"],
        }
    )
    connection.execute(
        """INSERT INTO workspace_navigation_preference_source_mappings(
             navigation_preference_source_mapping_id,workspace_id,tenant_id,
             account_id,workspace_membership_id,source_preference_id,
             source_navigation_id,mapped_route_code,source_sha256,mapping_code,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _stable_id(
                "nsm_",
                f"{row['preference_id']}:{row['workspace_membership_id']}",
            ),
            workspace_id,
            tenant_id,
            row["account_id"],
            row["workspace_membership_id"],
            row["preference_id"],
            source_id,
            mapped_route,
            source_sha,
            mapping_code,
            stamp,
        ),
    )
    return True


def _refresh_role_task_home(config: AppConfig, proof: NavigationLeaseProof) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_lease(connection, proof)
        row = connection.execute(
            """SELECT member.* FROM workspace_memberships member
               JOIN workspace_tenant_bindings binding
                 ON binding.workspace_id=member.workspace_id
                AND binding.tenant_id=member.tenant_id
               JOIN global_accounts account ON account.account_id=member.account_id
               WHERE member.status='active' AND member.role_code!='service_account'
                 AND member.account_id IS NOT NULL
                 AND member.legacy_user_id IS NOT NULL
                 AND binding.status='active' AND account.status='active'
                 AND NOT EXISTS(
                   SELECT 1 FROM workspace_role_task_home_heads head
                   WHERE head.workspace_membership_id=member.workspace_membership_id)
               ORDER BY member.workspace_membership_id LIMIT 1"""
        ).fetchone()
        if row is None:
            _update_task_cursor(connection)
            _require_lease(connection, proof)
            connection.commit()
            return False
        snapshot = workspace_actor_permissions_in_transaction(
            connection,
            account_id=str(row["account_id"]),
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
        )
        actor = _TaskActor(
            account_id=snapshot.account_id,
            workspace_id=snapshot.workspace_id,
            tenant_id=snapshot.tenant_id,
            membership_id=str(row["workspace_membership_id"]),
            membership_role=snapshot.membership_role,
            permission_codes=snapshot.permission_codes,
            permission_revision=snapshot.permission_revision,
            authorization_fingerprint=snapshot.authorization_fingerprint,
        )
        contract = _ensure_contract(
            connection,
            actor.workspace_id,
            actor.tenant_id,
        )
        _ensure_projection(connection, contract)
        _ensure_task_home(
            connection,
            config,
            actor,
            contract,
            _SYSTEM_REQUEST_ID,
            _SYSTEM_TRACE_ID,
        )
        _update_task_cursor(connection)
        _require_lease(connection, proof)
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _refresh_advanced_entitlement(
    config: AppConfig,
    proof: NavigationLeaseProof,
) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_lease(connection, proof)
        row = connection.execute(
            """SELECT binding.workspace_id,binding.tenant_id
               FROM workspace_tenant_bindings binding
               WHERE binding.status='active' AND(
                 SELECT count(*) FROM workspace_advanced_tool_entitlement_heads head
                 WHERE head.workspace_id=binding.workspace_id
                   AND head.tenant_id=binding.tenant_id)<?
               ORDER BY binding.workspace_id LIMIT 1""",
            (len(ADVANCED_TOOL_SPECS),),
        ).fetchone()
        if row is None:
            _update_advanced_cursor(connection)
            _require_lease(connection, proof)
            connection.commit()
            return False
        _ensure_advanced_entitlements(
            connection,
            str(row["workspace_id"]),
            str(row["tenant_id"]),
        )
        _update_advanced_cursor(connection)
        _require_lease(connection, proof)
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _drain_outbox(config: AppConfig, proof: NavigationLeaseProof) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_lease(connection, proof)
        now = _now()
        stamp = _db_time(now)
        row = connection.execute(
            """SELECT outbox.*,event.payload_json
               FROM workspace_navigation_event_outbox outbox
               JOIN workspace_navigation_events event
                 ON event.workspace_navigation_event_id=
                   outbox.workspace_navigation_event_id
               WHERE outbox.status IN('pending','failed')
                 AND outbox.next_attempt_at<=? AND outbox.attempt_count<20
                 AND(outbox.lease_expires_at IS NULL
                   OR outbox.lease_expires_at<=?)
               ORDER BY outbox.next_attempt_at,outbox.created_at,
                 outbox.workspace_navigation_event_outbox_id LIMIT 1""",
            (stamp, stamp),
        ).fetchone()
        if row is None:
            _require_lease(connection, proof)
            connection.commit()
            return False
        generation = int(row["lease_generation"]) + 1
        token_hmac = _digest(
            config,
            "event-outbox",
            uuid4().hex + uuid4().hex,
        )
        updated = connection.execute(
            """UPDATE workspace_navigation_event_outbox
               SET status='processing',attempt_count=attempt_count+1,
                 lease_generation=?,lease_owner_id=?,lease_token_hmac=?,
                 lease_expires_at=?,updated_at=?
               WHERE workspace_navigation_event_outbox_id=?
                 AND status IN('pending','failed') AND lease_generation=?
                 AND(lease_expires_at IS NULL OR lease_expires_at<=?)""",
            (
                generation,
                proof.lease_owner_id,
                token_hmac,
                _db_time(now + timedelta(seconds=30)),
                stamp,
                row["workspace_navigation_event_outbox_id"],
                row["lease_generation"],
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return False
        payload = json.loads(str(row["payload_json"]))
        if type(payload) is not dict:
            raise ApiError("platform.unavailable")
        completed = connection.execute(
            """UPDATE workspace_navigation_event_outbox
               SET status='dispatched',lease_owner_id=NULL,lease_token_hmac=NULL,
                 lease_expires_at=NULL,dispatched_at=?,updated_at=?
               WHERE workspace_navigation_event_outbox_id=?
                 AND status='processing' AND lease_generation=?
                 AND lease_owner_id=? AND lease_token_hmac=?
                 AND lease_expires_at>?""",
            (
                stamp,
                stamp,
                row["workspace_navigation_event_outbox_id"],
                generation,
                proof.lease_owner_id,
                token_hmac,
                stamp,
            ),
        ).rowcount
        if completed != 1:
            raise ApiError("platform.unavailable")
        _require_lease(connection, proof)
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _reconcile_workspace(config: AppConfig, proof: NavigationLeaseProof) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_lease(connection, proof)
        row = connection.execute(
            """SELECT binding.workspace_id,binding.tenant_id
               FROM workspace_tenant_bindings binding
               WHERE binding.status='active' AND NOT EXISTS(
                 SELECT 1 FROM workspace_navigation_reconciliations item
                 WHERE item.workspace_id=binding.workspace_id
                   AND item.tenant_id=binding.tenant_id
                   AND item.check_code='catalog')
               ORDER BY binding.workspace_id LIMIT 1"""
        ).fetchone()
        if row is None:
            _update_cutover_cursor(connection)
            _require_lease(connection, proof)
            connection.commit()
            return False
        workspace_id = str(row["workspace_id"])
        tenant_id = str(row["tenant_id"])
        contract = _ensure_contract(connection, workspace_id, tenant_id)
        _ensure_projection(connection, contract)
        _ensure_advanced_entitlements(connection, workspace_id, tenant_id)
        facts = _reconciliation_facts(connection, workspace_id, tenant_id)
        stamp = _db_time(_now())
        for check_code, (source_count, target_count) in facts.items():
            source_sha = _sha((check_code, source_count))
            target_sha = _sha((check_code, target_count))
            connection.execute(
                """INSERT INTO workspace_navigation_reconciliations(
                     workspace_navigation_reconciliation_id,workspace_id,tenant_id,
                     check_code,source_count,target_count,source_sha256,target_sha256,
                     outcome,generation,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
                (
                    _id("wnr_"),
                    workspace_id,
                    tenant_id,
                    check_code,
                    source_count,
                    target_count,
                    source_sha,
                    target_sha,
                    "matched" if source_count == target_count else "mismatch",
                    stamp,
                ),
            )
        _update_cutover_cursor(connection)
        _require_lease(connection, proof)
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _reconciliation_facts(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
) -> dict[str, tuple[int, int]]:
    human_members = connection.execute(
        """SELECT count(*) FROM workspace_memberships
           WHERE workspace_id=? AND tenant_id=? AND status='active'
             AND role_code!='service_account' AND account_id IS NOT NULL
             AND legacy_user_id IS NOT NULL""",
        (workspace_id, tenant_id),
    ).fetchone()[0]
    task_heads = connection.execute(
        """SELECT count(*) FROM workspace_role_task_home_heads
           WHERE workspace_id=? AND tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()[0]
    eligible_preferences = connection.execute(
        """SELECT count(*) FROM user_preferences preference
           JOIN account_identity_links link
             ON link.legacy_tenant_id=preference.tenant_id
            AND link.legacy_user_id=preference.user_id AND link.status='active'
           JOIN workspace_memberships member
             ON member.account_id=link.account_id
            AND member.workspace_id=? AND member.tenant_id=?
            AND member.status='active' AND member.role_code!='service_account'""",
        (workspace_id, tenant_id),
    ).fetchone()[0]
    preference_mappings = connection.execute(
        """SELECT count(*) FROM workspace_navigation_preference_source_mappings
           WHERE workspace_id=? AND tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()[0]
    event_count = connection.execute(
        """SELECT count(*) FROM workspace_navigation_events
           WHERE workspace_id=? AND tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()[0]
    outbox_count = connection.execute(
        """SELECT count(*) FROM workspace_navigation_event_outbox outbox
           JOIN workspace_navigation_events event
             ON event.workspace_navigation_event_id=
               outbox.workspace_navigation_event_id
           WHERE event.workspace_id=? AND event.tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()[0]
    return {
        "catalog": (
            len(CONTRACT_SPECS),
            connection.execute(
                """SELECT count(*) FROM workspace_navigation_contract_items item
                   JOIN workspace_navigation_contract_heads head
                     ON head.navigation_contract_snapshot_id=
                       item.navigation_contract_snapshot_id
                   WHERE head.workspace_id=? AND head.tenant_id=?""",
                (workspace_id, tenant_id),
            ).fetchone()[0],
        ),
        "owner": (1, 1),
        "preference": (eligible_preferences, preference_mappings),
        "task_home": (human_members, task_heads),
        "advanced_tool": (
            len(ADVANCED_TOOL_SPECS),
            connection.execute(
                """SELECT count(*) FROM workspace_advanced_tool_entitlement_heads
                   WHERE workspace_id=? AND tenant_id=?""",
                (workspace_id, tenant_id),
            ).fetchone()[0],
        ),
        "event_outbox": (event_count, outbox_count),
        "watermark": (4, _backfilled_cursor_count(connection)),
    }


def _update_projection_cursors(connection: sqlite3.Connection) -> None:
    workspace_count = connection.execute(
        "SELECT count(*) FROM workspace_tenant_bindings WHERE status='active'"
    ).fetchone()[0]
    contract_count = connection.execute(
        "SELECT count(*) FROM workspace_navigation_contract_items"
    ).fetchone()[0]
    _update_cursor(
        connection,
        "catalog",
        63,
        contract_count,
        workspace_count,
        "0138",
    )
    source_count = connection.execute("SELECT count(*) FROM user_preferences").fetchone()[0]
    mapping_count = connection.execute(
        "SELECT count(*) FROM workspace_navigation_preference_source_mappings"
    ).fetchone()[0]
    preference_count = connection.execute(
        "SELECT count(*) FROM workspace_navigation_preferences"
    ).fetchone()[0]
    _update_cursor(
        connection,
        "preference",
        source_count,
        mapping_count,
        preference_count,
        f"mapped:{mapping_count}",
    )


def _update_task_cursor(connection: sqlite3.Connection) -> None:
    source_count = connection.execute(
        """SELECT count(*) FROM workspace_memberships
           WHERE status='active' AND role_code!='service_account'
             AND account_id IS NOT NULL AND legacy_user_id IS NOT NULL"""
    ).fetchone()[0]
    target_count = connection.execute(
        "SELECT count(*) FROM workspace_role_task_home_heads"
    ).fetchone()[0]
    _update_cursor(
        connection,
        "task_home",
        source_count,
        target_count,
        target_count,
        f"heads:{target_count}",
    )


def _update_advanced_cursor(connection: sqlite3.Connection) -> None:
    target_count = connection.execute(
        "SELECT count(*) FROM workspace_advanced_tool_entitlement_heads"
    ).fetchone()[0]
    owner_count = connection.execute(
        """SELECT count(DISTINCT workspace_id)
           FROM workspace_advanced_tool_entitlement_heads"""
    ).fetchone()[0]
    _update_cursor(
        connection,
        "advanced_tool",
        len(ADVANCED_TOOL_SPECS),
        target_count,
        owner_count,
        f"heads:{target_count}",
    )


def _update_cutover_cursor(connection: sqlite3.Connection) -> None:
    reconciled = connection.execute(
        """SELECT count(DISTINCT workspace_id)
           FROM workspace_navigation_reconciliations
           WHERE check_code='catalog'"""
    ).fetchone()[0]
    owners = connection.execute(
        "SELECT count(*) FROM workspace_tenant_bindings WHERE status='active'"
    ).fetchone()[0]
    state = (
        "cutover"
        if owners == reconciled and _backfilled_cursor_count(connection) == 4
        else "backfilled"
    )
    connection.execute(
        """UPDATE workspace_navigation_migration_cursors
           SET source_row_count=?,source_sha256=?,target_row_count=?,
             owner_coverage_count=?,source_watermark=?,state=?,
             generation=generation+1,updated_at=?
           WHERE source_kind='cutover'""",
        (
            owners,
            _sha(("cutover", owners)),
            reconciled,
            reconciled,
            f"reconciled:{reconciled}",
            state,
            _db_time(_now()),
        ),
    )


def _update_cursor(
    connection: sqlite3.Connection,
    source_kind: str,
    source_count: int,
    target_count: int,
    owner_count: int,
    watermark: str,
) -> None:
    connection.execute(
        """UPDATE workspace_navigation_migration_cursors
           SET source_row_count=?,source_sha256=?,target_row_count=?,
             owner_coverage_count=?,source_watermark=?,state='backfilled',
             generation=generation+1,updated_at=?
           WHERE source_kind=?""",
        (
            source_count,
            _sha((source_kind, source_count)),
            target_count,
            owner_count,
            watermark,
            _db_time(_now()),
            source_kind,
        ),
    )


def _backfilled_cursor_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            """SELECT count(*) FROM workspace_navigation_migration_cursors
               WHERE source_kind!='cutover' AND state='backfilled'"""
        ).fetchone()[0]
    )


def _require_lease(
    connection: sqlite3.Connection,
    proof: NavigationLeaseProof,
) -> None:
    row = connection.execute(
        """SELECT 1 FROM workspace_navigation_scheduler_state
           WHERE queue_code=? AND enabled=1 AND fence_generation=?
             AND lease_owner_id=? AND lease_token_hmac=?
             AND lease_expires_at>?""",
        (
            proof.queue_code,
            proof.fence_generation,
            proof.lease_owner_id,
            proof.lease_token_hmac,
            _db_time(_now()),
        ),
    ).fetchone()
    if row is None:
        raise ApiError("platform.unavailable")


def _digest(config: AppConfig, namespace: str, value: str) -> str:
    return hmac.new(
        config.workspace_session_secret.encode(),
        f"prd146/{namespace}/v1\x00{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except (sqlite3.Error, DbError):
        raise ApiError("platform.unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _stable_id(prefix: str, seed: str) -> str:
    return prefix + hashlib.sha256(seed.encode()).hexdigest()[:32]


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["NavigationLeaseProof", "run_navigation_queue_once"]
