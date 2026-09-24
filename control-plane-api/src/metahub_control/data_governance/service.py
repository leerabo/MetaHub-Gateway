"""Database-authoritative PRD-117 policy, hold, evidence, and deletion fences."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.config_distribution.service import (
    publish_workspace_snapshot_in_transaction,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    begin_platform_read_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    DataClassificationSummary,
    DataGovernanceResponse,
    DataGovernanceUpdateRequest,
    DeletionScheduleSummary,
    GovernanceFenceSummary,
    GovernanceSimulationSummary,
    LegalHoldCreateRequest,
    LegalHoldReleaseRequest,
    LegalHoldResponse,
    LegalHoldSummary,
    ResidencyEvidenceListResponse,
    ResidencyEvidenceSummary,
    ResidencyPolicySummary,
    RetentionPolicySummary,
)

_WORKSPACE_RE = re.compile(r"^wsp_[0-9a-f]{32}$")
_HOLD_RE = re.compile(r"^lgh_[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_CLASSIFICATIONS = ("audit", "billing", "file", "identity", "prompt", "response")
_WORKSPACE_READ_ROLES = frozenset({"owner", "security_admin", "legal_admin", "auditor"})
_WORKSPACE_MANAGE_ROLES = frozenset({"owner", "security_admin"})
_HOLD_MANAGE_ROLES = frozenset({"owner", "legal_admin"})


@dataclass(frozen=True, slots=True)
class ActiveGovernancePolicy:
    policy_id: str
    workspace_id: str
    tenant_id: str
    generation: int
    revision: int
    sha256: str
    allowed_groups: tuple[str, ...]
    primary_group: str
    cross_region_mode: str
    valid_until: datetime


def get_data_governance(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str,
    request_id: str,
    trace_id: str,
) -> DataGovernanceResponse:
    _valid_workspace(workspace_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_workspace(actor, workspace_id, "data_governance.read", _WORKSPACE_READ_ROLES)
        result = _governance_response(
            connection,
            workspace_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("data_governance.unavailable") from None
    finally:
        connection.close()


def update_data_governance(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: DataGovernanceUpdateRequest,
    request_id: str,
    trace_id: str,
) -> DataGovernanceResponse:
    _valid_workspace(workspace_id)
    _valid_idempotency(idempotency_key)
    if expected_revision < 1:
        raise ApiError("data_governance.revision_conflict")
    canonical_request = {
        "allowed_residency_groups": payload.allowed_residency_groups,
        "primary_residency_group": payload.primary_residency_group,
        "cross_region_mode": payload.cross_region_mode,
        "retention_policies": [item.model_dump(mode="json") for item in payload.retention_policies],
        "reason_code": payload.reason_code,
    }
    request_sha256 = _digest(canonical_request)
    idempotency_sha256 = _secret_digest(config, "command", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require_workspace(actor, workspace_id, "data_governance.manage", _WORKSPACE_MANAGE_ROLES)
        current = _active_policy_row(connection, workspace_id, actor.tenant_id)
        fence = _fence_row(connection, workspace_id, actor.tenant_id)
        replay = _command_replay(
            connection,
            workspace_id=workspace_id,
            action_code="policy.apply",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
        )
        if replay is not None:
            result = _governance_response(
                connection,
                workspace_id,
                request_id=request_id,
                trace_id=trace_id,
                applied=True,
                idempotent_replay=True,
            )
            connection.commit()
            return result
        if int(current["revision"]) != expected_revision:
            raise ApiError("data_governance.revision_conflict")
        _validate_residency_groups(connection, payload.allowed_residency_groups)
        simulation = _simulate_policy(
            connection,
            workspace_id=workspace_id,
            tenant_id=actor.tenant_id,
            next_generation=int(fence["policy_generation"]) + 1,
            canonical_request=canonical_request,
        )
        if not payload.confirmed:
            result = _governance_response(
                connection,
                workspace_id,
                request_id=request_id,
                trace_id=trace_id,
                simulation=simulation,
            )
            connection.commit()
            return result

        now = _database_now(connection)
        stamp = _db_time(_changed_time(now, current["updated_at"]))
        generation = simulation.next_generation
        policy_id = _id("rgp_")
        connection.execute(
            "UPDATE residency_policies SET status='superseded',superseded_at=?,updated_at=? "
            "WHERE residency_policy_id=? AND status='active'",
            (stamp, stamp, current["residency_policy_id"]),
        )
        active_retentions = connection.execute(
            "SELECT * FROM retention_policies WHERE workspace_id=? AND tenant_id=? "
            "AND status='active' ORDER BY classification_code",
            (workspace_id, actor.tenant_id),
        ).fetchall()
        if len(active_retentions) != 6:
            raise ApiError("data_governance.policy_unavailable")
        for row in active_retentions:
            connection.execute(
                "UPDATE retention_policies SET status='superseded',superseded_at=?,updated_at=? "
                "WHERE retention_policy_id=? AND status='active'",
                (stamp, stamp, row["retention_policy_id"]),
            )
        valid_until = str(current["valid_until"])
        connection.execute(
            """INSERT INTO residency_policies(
                 residency_policy_id,workspace_id,tenant_id,allowed_residency_groups_json,
                 primary_residency_group,cross_region_mode,status,revision,generation,
                 policy_sha256,reason_code,updated_by_account_id,valid_until,superseded_at,
                 created_at,updated_at)
               VALUES(?,?,?,?,?,?,'active',?,?,?,?,?,?,NULL,?,?)""",
            (
                policy_id,
                workspace_id,
                actor.tenant_id,
                _canonical(payload.allowed_residency_groups),
                payload.primary_residency_group,
                payload.cross_region_mode,
                int(current["revision"]) + 1,
                generation,
                simulation.candidate_sha256,
                payload.reason_code,
                actor.account_id,
                valid_until,
                stamp,
                stamp,
            ),
        )
        previous_by_code = {str(row["classification_code"]): row for row in active_retentions}
        classifications = {
            str(row["code"]): row
            for row in connection.execute(
                "SELECT * FROM data_classifications WHERE workspace_id=? AND tenant_id=?",
                (workspace_id, actor.tenant_id),
            ).fetchall()
        }
        for item in payload.retention_policies:
            previous = previous_by_code[item.classification_code]
            classification = classifications[item.classification_code]
            connection.execute(
                """INSERT INTO retention_policies(
                     retention_policy_id,workspace_id,tenant_id,data_classification_id,
                     classification_code,retention_days,deletion_mode,backup_grace_days,
                     status,revision,generation,superseded_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,'active',?,?,NULL,?,?)""",
                (
                    _id("rtp_"),
                    workspace_id,
                    actor.tenant_id,
                    classification["data_classification_id"],
                    item.classification_code,
                    item.retention_days,
                    item.deletion_mode,
                    item.backup_grace_days,
                    int(previous["revision"]) + 1,
                    generation,
                    stamp,
                    stamp,
                ),
            )
        _advance_fence(connection, fence, "policy", stamp)
        if simulation.placement_compliant:
            placement = connection.execute(
                "SELECT revision,snapshot_generation,updated_at "
                "FROM workspace_gateway_placements WHERE workspace_id=? AND tenant_id=?",
                (workspace_id, actor.tenant_id),
            ).fetchone()
            if placement is not None:
                placement_stamp = _db_time(_changed_time(now, placement["updated_at"]))
                changed = connection.execute(
                    "UPDATE workspace_gateway_placements SET revision=revision+1,"
                    "snapshot_generation=snapshot_generation+1,governance_generation=?,"
                    "updated_at=? WHERE workspace_id=? AND tenant_id=? AND revision=?",
                    (
                        generation,
                        placement_stamp,
                        workspace_id,
                        actor.tenant_id,
                        int(placement["revision"]),
                    ),
                ).rowcount
                if changed != 1:
                    raise ApiError("data_governance.revision_conflict")
        publish_workspace_snapshot_in_transaction(
            connection,
            config,
            workspace_id=workspace_id,
            tenant_id=actor.tenant_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_command(
            connection,
            workspace_id=workspace_id,
            tenant_id=actor.tenant_id,
            action_code="policy.apply",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=policy_id,
            result_revision=int(current["revision"]) + 1,
            result_generation=generation,
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
        _write_event(
            connection,
            event_type="data.retention_changed",
            workspace_id=workspace_id,
            tenant_id=actor.tenant_id,
            object_type="residency_policy",
            object_id=policy_id,
            classification_code=None,
            policy_generation=generation,
            status_code="data_governance.policy_applied",
            item_count=6,
            manifest_sha256=simulation.candidate_sha256,
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
        _record_current_placement_evidence(
            connection,
            workspace_id=workspace_id,
            tenant_id=actor.tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
        result = _governance_response(
            connection,
            workspace_id,
            request_id=request_id,
            trace_id=trace_id,
            simulation=simulation,
            applied=True,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("data_governance.unavailable") from None
    finally:
        connection.close()


def create_legal_hold(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    idempotency_key: str,
    payload: LegalHoldCreateRequest,
    request_id: str,
    trace_id: str,
) -> LegalHoldResponse:
    _valid_workspace(workspace_id)
    _valid_idempotency(idempotency_key)
    request_sha256 = _digest(payload.model_dump(mode="json"))
    idempotency_sha256 = _secret_digest(config, "command", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require_workspace(actor, workspace_id, "legal_hold.manage", _HOLD_MANAGE_ROLES)
        replay = _command_replay(
            connection,
            workspace_id=workspace_id,
            action_code="legal_hold.create",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
        )
        if replay is not None:
            hold = _hold_row(connection, workspace_id, str(replay["result_object_id"]))
            result = _hold_response(
                connection, hold, request_id=request_id, trace_id=trace_id, replay=True
            )
            connection.commit()
            return result
        now = _database_now(connection)
        _expire_holds_in_transaction(connection, workspace_id, actor.tenant_id, now)
        if payload.expires_at is not None and payload.expires_at.astimezone(UTC) <= now:
            raise ApiError("data_governance.request_invalid")
        destructive = connection.execute(
            "SELECT 1 FROM deletion_requests WHERE tenant_id=? AND status='deleting' LIMIT 1",
            (actor.tenant_id,),
        ).fetchone()
        running = connection.execute(
            "SELECT 1 FROM deletion_schedules WHERE workspace_id=? AND tenant_id=? "
            "AND classification_code=? AND status='running' LIMIT 1",
            (workspace_id, actor.tenant_id, payload.classification_code),
        ).fetchone()
        if destructive is not None or running is not None:
            raise ApiError("data_governance.deletion_conflict")
        classification = connection.execute(
            "SELECT * FROM data_classifications WHERE workspace_id=? AND tenant_id=? AND code=?",
            (workspace_id, actor.tenant_id, payload.classification_code),
        ).fetchone()
        if classification is None:
            raise ApiError("data_governance.not_found")
        existing = connection.execute(
            """SELECT * FROM legal_holds WHERE workspace_id=? AND tenant_id=?
                 AND classification_code=? AND scope_kind=?
                 AND selector_sha256 IS ? AND status='active'""",
            (
                workspace_id,
                actor.tenant_id,
                payload.classification_code,
                payload.scope_kind,
                payload.selector_sha256,
            ),
        ).fetchone()
        if existing is not None:
            raise ApiError("data_governance.hold_conflict")
        fence = _fence_row(connection, workspace_id, actor.tenant_id)
        stamp = _db_time(_changed_time(now, fence["updated_at"]))
        hold_generation = int(fence["hold_generation"]) + 1
        _advance_fence(connection, fence, "hold", stamp)
        hold_id = _id("lgh_")
        connection.execute(
            """INSERT INTO legal_holds(
                 legal_hold_id,workspace_id,tenant_id,data_classification_id,
                 classification_code,scope_kind,selector_sha256,reason_code,status,revision,
                 created_hold_generation,released_hold_generation,legal_actor_account_id,
                 release_reason_code,expires_at,released_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,'active',1,?,NULL,?,NULL,?,NULL,?,?)""",
            (
                hold_id,
                workspace_id,
                actor.tenant_id,
                classification["data_classification_id"],
                payload.classification_code,
                payload.scope_kind,
                payload.selector_sha256,
                payload.reason_code,
                hold_generation,
                actor.account_id,
                _db_time(payload.expires_at) if payload.expires_at is not None else None,
                stamp,
                stamp,
            ),
        )
        active_policy = _active_policy_row(connection, workspace_id, actor.tenant_id)
        _write_command(
            connection,
            workspace_id=workspace_id,
            tenant_id=actor.tenant_id,
            action_code="legal_hold.create",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=hold_id,
            result_revision=1,
            result_generation=hold_generation,
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
        _write_event(
            connection,
            event_type="legal_hold.created",
            workspace_id=workspace_id,
            tenant_id=actor.tenant_id,
            object_type="legal_hold",
            object_id=hold_id,
            classification_code=payload.classification_code,
            policy_generation=int(active_policy["generation"]),
            status_code="legal_hold.active",
            item_count=1,
            manifest_sha256=str(active_policy["policy_sha256"]),
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
        hold = _hold_row(connection, workspace_id, hold_id)
        result = _hold_response(connection, hold, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("data_governance.unavailable") from None
    finally:
        connection.close()


def release_legal_hold(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    hold_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: LegalHoldReleaseRequest,
    request_id: str,
    trace_id: str,
) -> LegalHoldResponse:
    _valid_workspace(workspace_id)
    if _HOLD_RE.fullmatch(hold_id) is None or expected_revision < 1:
        raise ApiError("data_governance.request_invalid")
    _valid_idempotency(idempotency_key)
    request_sha256 = _digest({"hold_id": hold_id, **payload.model_dump(mode="json")})
    idempotency_sha256 = _secret_digest(config, "command", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require_workspace(actor, workspace_id, "legal_hold.manage", _HOLD_MANAGE_ROLES)
        replay = _command_replay(
            connection,
            workspace_id=workspace_id,
            action_code="legal_hold.release",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
        )
        if replay is not None:
            hold = _hold_row(connection, workspace_id, hold_id)
            result = _hold_response(
                connection, hold, request_id=request_id, trace_id=trace_id, replay=True
            )
            connection.commit()
            return result
        hold = _hold_row(connection, workspace_id, hold_id)
        if hold["tenant_id"] != actor.tenant_id:
            raise ApiError("data_governance.not_found")
        if int(hold["revision"]) != expected_revision:
            raise ApiError("data_governance.revision_conflict")
        if hold["status"] != "active":
            raise ApiError("data_governance.hold_conflict")
        fence = _fence_row(connection, workspace_id, actor.tenant_id)
        now = _database_now(connection)
        stamp = _db_time(_changed_time(now, fence["updated_at"]))
        released_generation = int(fence["hold_generation"]) + 1
        _advance_fence(connection, fence, "hold", stamp)
        updated = connection.execute(
            """UPDATE legal_holds SET status='released',revision=revision+1,
                 released_hold_generation=?,release_reason_code=?,released_at=?,updated_at=?
               WHERE legal_hold_id=? AND workspace_id=? AND tenant_id=?
                 AND status='active' AND revision=?""",
            (
                released_generation,
                payload.reason_code,
                stamp,
                stamp,
                hold_id,
                workspace_id,
                actor.tenant_id,
                expected_revision,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("data_governance.revision_conflict")
        _write_command(
            connection,
            workspace_id=workspace_id,
            tenant_id=actor.tenant_id,
            action_code="legal_hold.release",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=hold_id,
            result_revision=expected_revision + 1,
            result_generation=released_generation,
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
        current = _hold_row(connection, workspace_id, hold_id)
        result = _hold_response(connection, current, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("data_governance.unavailable") from None
    finally:
        connection.close()


def list_residency_evidence(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str | None,
    component: str | None,
    outcome: str | None,
    cursor: str | None,
    limit: int,
    request_id: str,
    trace_id: str,
) -> ResidencyEvidenceListResponse:
    if workspace_id is not None:
        _valid_workspace(workspace_id)
    if component is not None and component not in {
        "gateway",
        "database",
        "object_storage",
        "backup",
        "job_runtime",
        "disaster_recovery",
        "tenant_data",
    }:
        raise ApiError("data_governance.request_invalid")
    if outcome is not None and outcome not in {
        "compliant",
        "migration_required",
        "blocked",
        "violation",
    }:
        raise ApiError("data_governance.request_invalid")
    if not 1 <= limit <= 100:
        raise ApiError("data_governance.request_invalid")
    cursor_value = _decode_cursor(config, cursor) if cursor is not None else None
    connection = _connect(config)
    try:
        actor = begin_platform_read_transaction(
            connection, config, session_token=session_token
        )
        _require_platform(actor, "platform.data_residency.read")
        where: list[str] = []
        params: list[object] = []
        if workspace_id is not None:
            where.append("workspace_id=?")
            params.append(workspace_id)
        if component is not None:
            where.append("component_code=?")
            params.append(component)
        if outcome is not None:
            where.append("outcome=?")
            params.append(outcome)
        if cursor_value is not None:
            where.append("(observed_at<? OR(observed_at=? AND residency_evidence_id<?))")
            params.extend((cursor_value[0], cursor_value[0], cursor_value[1]))
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = connection.execute(
            "SELECT * FROM residency_evidence"
            + clause
            + " ORDER BY observed_at DESC,residency_evidence_id DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit and visible:
            next_cursor = _encode_cursor(
                config,
                str(visible[-1]["observed_at"]),
                str(visible[-1]["residency_evidence_id"]),
            )
        connection.commit()
        return ResidencyEvidenceListResponse(
            items=tuple(_evidence_summary(row) for row in visible),
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("data_governance.unavailable") from None
    finally:
        connection.close()


def active_governance_policy_in_transaction(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    now: datetime | None = None,
) -> ActiveGovernancePolicy:
    row = _active_policy_row(connection, workspace_id, tenant_id)
    valid_until = _time(row["valid_until"])
    if valid_until <= (now or _database_now(connection)):
        raise ApiError("data_governance.policy_expired")
    try:
        groups = tuple(json.loads(str(row["allowed_residency_groups_json"])))
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ApiError("data_governance.policy_unavailable") from None
    if (
        not groups
        or groups != tuple(sorted(set(groups)))
        or str(row["primary_residency_group"]) not in groups
        or any(not isinstance(value, str) for value in groups)
    ):
        raise ApiError("data_governance.policy_unavailable")
    retention_count = connection.execute(
        "SELECT COUNT(*) FROM retention_policies WHERE workspace_id=? AND tenant_id=? "
        "AND generation=? AND status='active'",
        (workspace_id, tenant_id, int(row["generation"])),
    ).fetchone()[0]
    if retention_count != 6:
        raise ApiError("data_governance.policy_unavailable")
    return ActiveGovernancePolicy(
        policy_id=str(row["residency_policy_id"]),
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        generation=int(row["generation"]),
        revision=int(row["revision"]),
        sha256=str(row["policy_sha256"]),
        allowed_groups=groups,
        primary_group=str(row["primary_residency_group"]),
        cross_region_mode=str(row["cross_region_mode"]),
        valid_until=valid_until,
    )


def residency_group_for_region_in_transaction(
    connection: sqlite3.Connection, region_code: str
) -> str:
    row = connection.execute(
        "SELECT residency_group FROM platform_regions WHERE region_code=?",
        (region_code,),
    ).fetchone()
    if row is None:
        raise ApiError("data_governance.region_not_found")
    return str(row["residency_group"])


def require_region_allowed_in_transaction(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    region_code: str,
    expected_generation: int | None = None,
) -> tuple[ActiveGovernancePolicy, str]:
    policy = active_governance_policy_in_transaction(
        connection, workspace_id=workspace_id, tenant_id=tenant_id
    )
    if expected_generation is not None and expected_generation != policy.generation:
        raise ApiError("data_governance.generation_stale")
    group = residency_group_for_region_in_transaction(connection, region_code)
    if group not in policy.allowed_groups:
        raise ApiError("data_governance.residency_conflict")
    return policy, group


def active_hold_blocks_deletion_in_transaction(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    classification_code: str | None = None,
) -> bool:
    params: list[object] = [workspace_id, tenant_id, _db_time(_database_now(connection))]
    extra = ""
    if classification_code is not None:
        extra = " AND classification_code=?"
        params.append(classification_code)
    return (
        connection.execute(
            "SELECT 1 FROM legal_holds WHERE workspace_id=? AND tenant_id=? "
            "AND status='active' AND(expires_at IS NULL OR expires_at>?)" + extra + " LIMIT 1",
            params,
        ).fetchone()
        is not None
    )


def claim_deletion_fence_in_transaction(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    classification_code: str | None = None,
) -> tuple[int, int, int]:
    fence = _fence_row(connection, workspace_id, tenant_id)
    if active_hold_blocks_deletion_in_transaction(
        connection,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        classification_code=classification_code,
    ):
        raise ApiError("data_governance.hold_active")
    stamp = _db_time(_changed_time(_database_now(connection), fence["updated_at"]))
    _advance_fence(connection, fence, "deletion", stamp)
    return (
        int(fence["policy_generation"]),
        int(fence["hold_generation"]),
        int(fence["deletion_generation"]) + 1,
    )


def execute_deletion_schedule_in_transaction(
    connection: sqlite3.Connection,
    *,
    schedule_id: str,
    platform_job_id: str,
    workspace_id: str,
    tenant_id: str,
    policy_generation: int,
    hold_generation: int,
) -> str:
    """Execute one fenced retention schedule without exposing governed records."""

    schedule = connection.execute(
        "SELECT * FROM deletion_schedules WHERE deletion_schedule_id=? "
        "AND workspace_id=? AND tenant_id=?",
        (schedule_id, workspace_id, tenant_id),
    ).fetchone()
    if schedule is None or schedule["platform_job_id"] != platform_job_id:
        raise ApiError("data_governance.generation_stale")
    if schedule["status"] == "completed":
        return "data_governance.deletion_completed"
    if schedule["status"] == "blocked":
        return str(schedule["safe_outcome_code"] or "data_governance.deletion_blocked")
    if schedule["status"] != "queued":
        raise ApiError("data_governance.generation_stale")
    policy = active_governance_policy_in_transaction(
        connection, workspace_id=workspace_id, tenant_id=tenant_id
    )
    fence = _fence_row(connection, workspace_id, tenant_id)
    if (
        policy.generation != policy_generation
        or int(schedule["policy_generation"]) != policy_generation
        or int(schedule["hold_generation"]) != hold_generation
        or int(fence["policy_generation"]) != policy_generation
        or int(fence["hold_generation"]) != hold_generation
    ):
        return _block_deletion_schedule(
            connection,
            schedule=schedule,
            policy=policy,
            outcome_code="data_governance.generation_stale",
            violation_code="generation_stale",
        )
    classification_code = str(schedule["classification_code"])
    if active_hold_blocks_deletion_in_transaction(
        connection,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        classification_code=classification_code,
    ):
        return _block_deletion_schedule(
            connection,
            schedule=schedule,
            policy=policy,
            outcome_code="data_governance.hold_active",
            violation_code="hold_active",
        )
    lifecycle = connection.execute(
        "SELECT lifecycle_status FROM tenant_data_states WHERE tenant_id=?", (tenant_id,)
    ).fetchone()
    export_active = connection.execute(
        "SELECT 1 FROM tenant_export_jobs WHERE tenant_id=? "
        "AND status IN('pending','processing') LIMIT 1",
        (tenant_id,),
    ).fetchone()
    deletion_active = connection.execute(
        "SELECT 1 FROM deletion_requests WHERE tenant_id=? "
        "AND status IN('requested','quiescing','deleting') LIMIT 1",
        (tenant_id,),
    ).fetchone()
    now = _database_now(connection)
    backup_active = connection.execute(
        "SELECT 1 FROM backup_sets WHERE workspace_id=? AND tenant_id=? "
        "AND status NOT IN('expired','failed') AND unixepoch(retention_until)>unixepoch(?) "
        "LIMIT 1",
        (workspace_id, tenant_id, _db_time(now)),
    ).fetchone()
    if (
        lifecycle is None
        or lifecycle["lifecycle_status"] != "active"
        or export_active is not None
        or deletion_active is not None
        or backup_active is not None
    ):
        return _block_deletion_schedule(
            connection,
            schedule=schedule,
            policy=policy,
            outcome_code="data_governance.dependency_active",
            violation_code=None,
        )
    claim_deletion_fence_in_transaction(
        connection,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        classification_code=classification_code,
    )
    fence_revision = int(_fence_row(connection, workspace_id, tenant_id)["revision"])
    stamp = _db_time(_changed_time(now, schedule["updated_at"]))
    changed = connection.execute(
        "UPDATE deletion_schedules SET status='completed',checkpoint_code='completed',"
        "deleted_count=0,retained_count=0,manifest_sha256=?,safe_outcome_code=?,"
        "revision=revision+1,started_at=?,completed_at=?,updated_at=? "
        "WHERE deletion_schedule_id=? AND platform_job_id=? AND status='queued' "
        "AND revision=?",
        (
            policy.sha256,
            "data_governance.deletion_completed",
            stamp,
            stamp,
            stamp,
            schedule_id,
            platform_job_id,
            int(schedule["revision"]),
        ),
    ).rowcount
    if changed != 1:
        raise ApiError("data_governance.generation_stale")
    region_code = _workspace_region_code(connection, workspace_id, tenant_id)
    write_residency_evidence_in_transaction(
        connection,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        policy=policy,
        component_code="tenant_data",
        region_code=region_code,
        storage_class="deletion",
        outcome="compliant",
        violation_code=None,
        observed_watermark=fence_revision,
        manifest_sha256=policy.sha256,
        request_id="data-retention-job",
        trace_id="data-retention-job",
        now=stamp,
    )
    _write_event(
        connection,
        event_type="deletion.executed",
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        object_type="deletion_schedule",
        object_id=schedule_id,
        classification_code=classification_code,
        policy_generation=policy.generation,
        status_code="data_governance.deletion_completed",
        item_count=0,
        manifest_sha256=policy.sha256,
        request_id="data-retention-job",
        trace_id="data-retention-job",
        now=stamp,
    )
    return "data_governance.deletion_completed"


def deletion_schedule_outcome(
    config: AppConfig,
    *,
    schedule_id: str,
    platform_job_id: str,
    workspace_id: str,
    tenant_id: str,
) -> str:
    """Recover the fixed terminal outcome after an atomic job effect replay."""

    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT status,safe_outcome_code,platform_job_id FROM deletion_schedules "
            "WHERE deletion_schedule_id=? AND workspace_id=? AND tenant_id=?",
            (schedule_id, workspace_id, tenant_id),
        ).fetchone()
        if row is None or row["platform_job_id"] != platform_job_id:
            raise ApiError("data_governance.generation_stale")
        if row["status"] not in {"blocked", "completed"} or not row["safe_outcome_code"]:
            raise ApiError("data_governance.unavailable")
        connection.commit()
        return str(row["safe_outcome_code"])
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("data_governance.unavailable") from None
    finally:
        connection.close()


def write_residency_evidence_in_transaction(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    policy: ActiveGovernancePolicy,
    component_code: str,
    region_code: str,
    storage_class: str,
    outcome: str,
    violation_code: str | None,
    observed_watermark: int,
    manifest_sha256: str,
    request_id: str,
    trace_id: str,
    now: datetime | str | None = None,
) -> str:
    stamp = now if isinstance(now, str) else _db_time(now or _database_now(connection))
    evidence_id = _id("rse_")
    inserted = connection.execute(
        """INSERT OR IGNORE INTO residency_evidence(
             residency_evidence_id,workspace_id,tenant_id,residency_policy_id,
             policy_generation,component_code,region_code,storage_class,outcome,
             violation_code,observed_watermark,manifest_sha256,observed_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            evidence_id,
            workspace_id,
            tenant_id,
            policy.policy_id,
            policy.generation,
            component_code,
            region_code,
            storage_class,
            outcome,
            violation_code,
            observed_watermark,
            manifest_sha256,
            stamp,
            stamp,
        ),
    ).rowcount
    if inserted != 1:
        existing = connection.execute(
            "SELECT residency_evidence_id FROM residency_evidence WHERE workspace_id=? "
            "AND component_code=? AND observed_watermark=? AND manifest_sha256=?",
            (workspace_id, component_code, observed_watermark, manifest_sha256),
        ).fetchone()
        return str(existing["residency_evidence_id"])
    if outcome == "violation":
        _write_event(
            connection,
            event_type="residency.violation_detected",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            object_type="residency_evidence",
            object_id=evidence_id,
            classification_code=None,
            policy_generation=policy.generation,
            status_code="residency." + str(violation_code or "policy_unavailable"),
            item_count=1,
            manifest_sha256=manifest_sha256,
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
    return evidence_id


def _block_deletion_schedule(
    connection: sqlite3.Connection,
    *,
    schedule,
    policy: ActiveGovernancePolicy,
    outcome_code: str,
    violation_code: str | None,
) -> str:
    now = _database_now(connection)
    stamp = _db_time(_changed_time(now, schedule["updated_at"]))
    changed = connection.execute(
        "UPDATE deletion_schedules SET status='blocked',checkpoint_code='blocked',"
        "safe_outcome_code=?,revision=revision+1,updated_at=? "
        "WHERE deletion_schedule_id=? AND status='queued' AND revision=?",
        (
            outcome_code,
            stamp,
            schedule["deletion_schedule_id"],
            int(schedule["revision"]),
        ),
    ).rowcount
    if changed != 1:
        raise ApiError("data_governance.generation_stale")
    write_residency_evidence_in_transaction(
        connection,
        workspace_id=str(schedule["workspace_id"]),
        tenant_id=str(schedule["tenant_id"]),
        policy=policy,
        component_code="tenant_data",
        region_code=_workspace_region_code(
            connection, str(schedule["workspace_id"]), str(schedule["tenant_id"])
        ),
        storage_class="deletion",
        outcome="blocked",
        violation_code=violation_code,
        observed_watermark=int(
            _fence_row(
                connection, str(schedule["workspace_id"]), str(schedule["tenant_id"])
            )["revision"]
        ),
        manifest_sha256=policy.sha256,
        request_id="data-retention-job",
        trace_id="data-retention-job",
        now=stamp,
    )
    return outcome_code


def _workspace_region_code(
    connection: sqlite3.Connection, workspace_id: str, tenant_id: str
) -> str:
    row = connection.execute(
        "SELECT region_code FROM workspace_regions WHERE workspace_id=? AND tenant_id=? "
        "AND status='active'",
        (workspace_id, tenant_id),
    ).fetchone()
    if row is None:
        raise ApiError("data_governance.policy_unavailable")
    return str(row["region_code"])


def _governance_response(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    request_id: str,
    trace_id: str,
    simulation: GovernanceSimulationSummary | None = None,
    applied: bool = False,
    idempotent_replay: bool = False,
) -> DataGovernanceResponse:
    policy = _active_policy_row(connection, workspace_id)
    classifications = connection.execute(
        "SELECT * FROM data_classifications WHERE workspace_id=? ORDER BY code", (workspace_id,)
    ).fetchall()
    retentions = connection.execute(
        "SELECT * FROM retention_policies WHERE workspace_id=? AND status='active' "
        "ORDER BY classification_code",
        (workspace_id,),
    ).fetchall()
    holds = connection.execute(
        "SELECT * FROM legal_holds WHERE workspace_id=? "
        "ORDER BY created_at DESC,legal_hold_id DESC LIMIT 100",
        (workspace_id,),
    ).fetchall()
    schedules = connection.execute(
        "SELECT * FROM deletion_schedules WHERE workspace_id=? "
        "ORDER BY created_at DESC,deletion_schedule_id DESC LIMIT 100",
        (workspace_id,),
    ).fetchall()
    fence = _fence_row(connection, workspace_id, str(policy["tenant_id"]))
    if len(classifications) != 6 or len(retentions) != 6:
        raise ApiError("data_governance.policy_unavailable")
    return DataGovernanceResponse(
        workspace_id=workspace_id,
        policy=_policy_summary(policy),
        classifications=tuple(_classification_summary(row) for row in classifications),
        retention_policies=tuple(_retention_summary(row) for row in retentions),
        legal_holds=tuple(_hold_summary(row) for row in holds),
        deletion_schedules=tuple(_schedule_summary(row) for row in schedules),
        fence=_fence_summary(fence),
        simulation=simulation,
        applied=applied,
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _simulate_policy(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    next_generation: int,
    canonical_request: dict[str, object],
) -> GovernanceSimulationSummary:
    canonical = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "tenant_id": tenant_id,
        "generation": next_generation,
        **canonical_request,
    }
    allowed = set(canonical_request["allowed_residency_groups"])
    migration: set[str] = set()
    placement = connection.execute(
        """SELECT primary_cell.region_code primary_region,
                  failover_cell.region_code failover_region
             FROM workspace_gateway_placements placement
             JOIN gateway_cells primary_cell
               ON primary_cell.gateway_cell_id=placement.primary_gateway_cell_id
             LEFT JOIN gateway_cells failover_cell
               ON failover_cell.gateway_cell_id=placement.failover_gateway_cell_id
             WHERE placement.workspace_id=? AND placement.tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()
    placement_compliant = placement is not None
    if placement is None:
        migration.update(("gateway", "disaster_recovery"))
    if placement is not None:
        regions = [str(placement["primary_region"])]
        if placement["failover_region"] is not None:
            regions.append(str(placement["failover_region"]))
        groups = {
            residency_group_for_region_in_transaction(connection, region) for region in regions
        }
        placement_compliant = groups <= allowed
        if not placement_compliant:
            migration.update(("gateway", "disaster_recovery"))
    backup_groups = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT residency_group FROM backup_sets WHERE workspace_id=? "
            "AND tenant_id=? AND residency_group IS NOT NULL AND status NOT IN('expired','failed')",
            (workspace_id, tenant_id),
        ).fetchall()
    }
    if not backup_groups <= allowed:
        migration.add("backup")
    return GovernanceSimulationSummary(
        candidate_sha256=_digest(canonical),
        next_generation=next_generation,
        placement_compliant=placement_compliant,
        migration_required_components=tuple(sorted(migration)),
    )


def _record_current_placement_evidence(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    placement = connection.execute(
        """SELECT placement.revision,cell.region_code
             FROM workspace_gateway_placements placement
             JOIN gateway_cells cell ON cell.gateway_cell_id=placement.primary_gateway_cell_id
             WHERE placement.workspace_id=? AND placement.tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()
    if placement is None:
        return
    group = residency_group_for_region_in_transaction(connection, str(placement["region_code"]))
    policy = active_governance_policy_in_transaction(
        connection,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
    )
    compliant = group in policy.allowed_groups
    write_residency_evidence_in_transaction(
        connection,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        policy=policy,
        component_code="gateway",
        region_code=str(placement["region_code"]),
        storage_class="routing",
        outcome="compliant" if compliant else "migration_required",
        violation_code=None if compliant else "region_not_allowed",
        observed_watermark=int(placement["revision"]),
        manifest_sha256=policy.sha256,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _expire_holds_in_transaction(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    now: datetime,
) -> None:
    rows = connection.execute(
        "SELECT * FROM legal_holds WHERE workspace_id=? AND tenant_id=? AND status='active' "
        "AND expires_at IS NOT NULL AND expires_at<=?",
        (workspace_id, tenant_id, _db_time(now)),
    ).fetchall()
    if not rows:
        return
    fence = _fence_row(connection, workspace_id, tenant_id)
    stamp = _db_time(_changed_time(now, fence["updated_at"]))
    generation = int(fence["hold_generation"]) + 1
    _advance_fence(connection, fence, "hold", stamp)
    for row in rows:
        connection.execute(
            "UPDATE legal_holds SET status='expired',revision=revision+1,"
            "released_hold_generation=?,release_reason_code='hold_expired',"
            "released_at=?,updated_at=? "
            "WHERE legal_hold_id=? AND status='active'",
            (generation, stamp, stamp, row["legal_hold_id"]),
        )


def _validate_residency_groups(connection: sqlite3.Connection, groups: list[str]) -> None:
    known = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT residency_group FROM platform_regions"
        ).fetchall()
    }
    if not set(groups) <= known:
        raise ApiError("data_governance.region_not_found")


def _advance_fence(connection, row, kind: str, stamp: str) -> None:
    column = {
        "policy": "policy_generation",
        "hold": "hold_generation",
        "deletion": "deletion_generation",
    }[kind]
    changed = connection.execute(
        f"UPDATE data_governance_fences SET {column}={column}+1,revision=revision+1,updated_at=? "
        "WHERE data_governance_fence_id=? AND revision=?",
        (stamp, row["data_governance_fence_id"], int(row["revision"])),
    ).rowcount
    if changed != 1:
        raise ApiError("data_governance.revision_conflict")


def _write_command(
    connection,
    *,
    workspace_id: str,
    tenant_id: str,
    action_code: str,
    idempotency_sha256: str,
    request_sha256: str,
    result_object_id: str,
    result_revision: int,
    result_generation: int,
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    connection.execute(
        """INSERT INTO data_governance_commands(
             data_governance_command_id,workspace_id,tenant_id,action_code,
             idempotency_sha256,request_sha256,result_object_id,result_revision,
             result_generation,request_id,trace_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("dgc_"),
            workspace_id,
            tenant_id,
            action_code,
            idempotency_sha256,
            request_sha256,
            result_object_id,
            result_revision,
            result_generation,
            request_id,
            trace_id,
            now,
        ),
    )


def _command_replay(
    connection,
    *,
    workspace_id: str,
    action_code: str,
    idempotency_sha256: str,
    request_sha256: str,
):
    row = connection.execute(
        "SELECT * FROM data_governance_commands WHERE workspace_id=? AND action_code=? "
        "AND idempotency_sha256=?",
        (workspace_id, action_code, idempotency_sha256),
    ).fetchone()
    if row is not None and not hmac.compare_digest(str(row["request_sha256"]), request_sha256):
        raise ApiError("data_governance.idempotency_conflict")
    return row


def _write_event(
    connection,
    *,
    event_type: str,
    workspace_id: str,
    tenant_id: str,
    object_type: str,
    object_id: str,
    classification_code: str | None,
    policy_generation: int,
    status_code: str,
    item_count: int,
    manifest_sha256: str,
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    event_id = _id("dge_")
    connection.execute(
        """INSERT INTO data_governance_events(
             data_governance_event_id,event_type,workspace_id,tenant_id,object_type,
             object_id,classification_code,policy_generation,status_code,item_count,
             manifest_sha256,request_id,trace_id,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            event_type,
            workspace_id,
            tenant_id,
            object_type,
            object_id,
            classification_code,
            policy_generation,
            status_code,
            item_count,
            manifest_sha256,
            request_id,
            trace_id,
            now,
        ),
    )
    connection.execute(
        """INSERT INTO data_governance_event_outbox(
             data_governance_event_outbox_id,data_governance_event_id,workspace_id,tenant_id,
             delivery_status,attempt_count,next_attempt_at,delivered_at,created_at,updated_at)
           VALUES(?,?,?,?,'pending',0,?,NULL,?,?)""",
        (_id("dgo_"), event_id, workspace_id, tenant_id, now, now, now),
    )


def _active_policy_row(connection, workspace_id: str, tenant_id: str | None = None):
    sql = "SELECT * FROM residency_policies WHERE workspace_id=? AND status='active'"
    params: tuple[object, ...] = (workspace_id,)
    if tenant_id is not None:
        sql += " AND tenant_id=?"
        params = (workspace_id, tenant_id)
    row = connection.execute(sql, params).fetchone()
    if row is None:
        raise ApiError("data_governance.not_found")
    return row


def _fence_row(connection, workspace_id: str, tenant_id: str):
    row = connection.execute(
        "SELECT * FROM data_governance_fences WHERE workspace_id=? AND tenant_id=?",
        (workspace_id, tenant_id),
    ).fetchone()
    if row is None:
        raise ApiError("data_governance.not_found")
    return row


def _hold_row(connection, workspace_id: str, hold_id: str):
    row = connection.execute(
        "SELECT * FROM legal_holds WHERE workspace_id=? AND legal_hold_id=?",
        (workspace_id, hold_id),
    ).fetchone()
    if row is None:
        raise ApiError("data_governance.not_found")
    return row


def _policy_summary(row) -> ResidencyPolicySummary:
    return ResidencyPolicySummary(
        residency_policy_id=row["residency_policy_id"],
        allowed_residency_groups=tuple(json.loads(row["allowed_residency_groups_json"])),
        primary_residency_group=row["primary_residency_group"],
        cross_region_mode=row["cross_region_mode"],
        revision=row["revision"],
        generation=row["generation"],
        policy_sha256=row["policy_sha256"],
        valid_until=_time(row["valid_until"]),
        updated_at=_time(row["updated_at"]),
    )


def _classification_summary(row) -> DataClassificationSummary:
    return DataClassificationSummary(
        data_classification_id=row["data_classification_id"],
        code=row["code"],
        sensitivity_level=row["sensitivity_level"],
        allowed_purposes=tuple(json.loads(row["allowed_purposes_json"])),
        default_retention_days=row["default_retention_days"],
    )


def _retention_summary(row) -> RetentionPolicySummary:
    return RetentionPolicySummary(
        retention_policy_id=row["retention_policy_id"],
        classification_code=row["classification_code"],
        retention_days=row["retention_days"],
        deletion_mode=row["deletion_mode"],
        backup_grace_days=row["backup_grace_days"],
        revision=row["revision"],
        generation=row["generation"],
    )


def _hold_summary(row) -> LegalHoldSummary:
    return LegalHoldSummary(
        legal_hold_id=row["legal_hold_id"],
        classification_code=row["classification_code"],
        scope_kind=row["scope_kind"],
        selector_configured=row["selector_sha256"] is not None,
        reason_code=row["reason_code"],
        status=row["status"],
        revision=row["revision"],
        created_hold_generation=row["created_hold_generation"],
        expires_at=_optional_time(row["expires_at"]),
        released_at=_optional_time(row["released_at"]),
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _schedule_summary(row) -> DeletionScheduleSummary:
    return DeletionScheduleSummary(
        deletion_schedule_id=row["deletion_schedule_id"],
        classification_code=row["classification_code"],
        due_at=_time(row["due_at"]),
        status=row["status"],
        policy_generation=row["policy_generation"],
        hold_generation=row["hold_generation"],
        checkpoint_code=row["checkpoint_code"],
        deleted_count=row["deleted_count"],
        retained_count=row["retained_count"],
        safe_outcome_code=row["safe_outcome_code"],
        revision=row["revision"],
        completed_at=_optional_time(row["completed_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _fence_summary(row) -> GovernanceFenceSummary:
    return GovernanceFenceSummary(
        policy_generation=row["policy_generation"],
        hold_generation=row["hold_generation"],
        deletion_generation=row["deletion_generation"],
        revision=row["revision"],
    )


def _hold_response(
    connection,
    row,
    *,
    request_id: str,
    trace_id: str,
    replay: bool = False,
) -> LegalHoldResponse:
    return LegalHoldResponse(
        hold=_hold_summary(row),
        fence=_fence_summary(_fence_row(connection, row["workspace_id"], row["tenant_id"])),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _evidence_summary(row) -> ResidencyEvidenceSummary:
    return ResidencyEvidenceSummary(
        residency_evidence_id=row["residency_evidence_id"],
        workspace_id=row["workspace_id"],
        policy_generation=row["policy_generation"],
        component_code=row["component_code"],
        region_code=row["region_code"],
        storage_class=row["storage_class"],
        outcome=row["outcome"],
        violation_code=row["violation_code"],
        observed_watermark=row["observed_watermark"],
        manifest_sha256=row["manifest_sha256"],
        observed_at=_time(row["observed_at"]),
    )


def _require_workspace(
    actor: WorkspaceSessionContext,
    workspace_id: str,
    permission: str,
    roles: frozenset[str],
) -> None:
    if actor.workspace_id != workspace_id:
        raise ApiError("data_governance.not_found")
    if actor.membership_role not in roles or permission not in actor.permission_codes:
        raise ApiError("workspace_session.permission_denied")


def _require_platform(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _valid_workspace(value: str) -> None:
    if _WORKSPACE_RE.fullmatch(value) is None:
        raise ApiError("data_governance.request_invalid")


def _valid_idempotency(value: str) -> None:
    if _IDEMPOTENCY_RE.fullmatch(value or "") is None:
        raise ApiError("data_governance.request_invalid")


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("data_governance.unavailable") from None


def _rollback(connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _secret_digest(config: AppConfig, domain: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        b"prd117/" + domain.encode("ascii") + b"/v1\0" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _encode_cursor(config: AppConfig, observed_at: str, evidence_id: str) -> str:
    payload = f"{observed_at}\x1f{evidence_id}"
    signature = _secret_digest(config, "evidence-cursor", payload)
    return base64.urlsafe_b64encode(f"{payload}\x1f{signature}".encode()).decode().rstrip("=")


def _decode_cursor(config: AppConfig, cursor: str) -> tuple[str, str]:
    if not isinstance(cursor, str) or not 20 <= len(cursor) <= 512:
        raise ApiError("data_governance.request_invalid")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
        observed_at, evidence_id, signature = raw.split("\x1f")
    except (ValueError, UnicodeError):
        raise ApiError("data_governance.request_invalid") from None
    if not hmac.compare_digest(
        signature, _secret_digest(config, "evidence-cursor", f"{observed_at}\x1f{evidence_id}")
    ):
        raise ApiError("data_governance.request_invalid")
    if _time(observed_at) is None or not re.fullmatch(r"rse_[0-9a-f]{32}", evidence_id):
        raise ApiError("data_governance.request_invalid")
    return observed_at, evidence_id


def _database_now(connection) -> datetime:
    return _time(connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_time(value: object | None) -> datetime | None:
    return None if value is None else _time(value)


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


__all__ = [
    "ActiveGovernancePolicy",
    "active_governance_policy_in_transaction",
    "active_hold_blocks_deletion_in_transaction",
    "claim_deletion_fence_in_transaction",
    "create_legal_hold",
    "deletion_schedule_outcome",
    "execute_deletion_schedule_in_transaction",
    "get_data_governance",
    "list_residency_evidence",
    "release_legal_hold",
    "require_region_allowed_in_transaction",
    "residency_group_for_region_in_transaction",
    "update_data_governance",
    "write_residency_evidence_in_transaction",
]
