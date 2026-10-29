"""PRD-116 Workspace conditional-access policy authority and evaluator."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import consume_step_up_grant_in_transaction

from .models import (
    ConditionalAccessPolicySummary,
    ConditionalAccessSimulationSummary,
)
from .service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)


@dataclass(frozen=True, slots=True)
class ConditionalAccessState:
    policy: ConditionalAccessPolicySummary | None
    latest_simulation: ConditionalAccessSimulationSummary | None


def get_conditional_access(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str,
) -> ConditionalAccessState:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_workspace(context, workspace_id, "conditional_access.read")
        policy = _policy_row(connection, workspace_id)
        simulation = _latest_simulation(connection, workspace_id)
        connection.commit()
        return ConditionalAccessState(
            _policy_summary(policy) if policy is not None else None,
            _simulation_summary(simulation) if simulation is not None else None,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("conditional_access.unavailable") from None
    finally:
        connection.close()


def simulate_conditional_access(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    request_id: str,
    trace_id: str,
) -> tuple[ConditionalAccessPolicySummary, ConditionalAccessSimulationSummary]:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        _require_workspace(context, workspace_id, "conditional_access.manage")
        now = _utcnow()
        policy = _policy_row(connection, workspace_id)
        if policy is None:
            policy_id = _id("cap")
            stamp = _db_time(now)
            connection.execute(
                """INSERT INTO conditional_access_policies(
                     conditional_access_policy_id,workspace_id,tenant_id,mode,require_mfa,
                     require_step_up_for_high_risk,allow_trusted_devices,region_rule,
                     network_rule,allowed_cidrs_json,exception_membership_ids_json,revision,
                     simulation_watermark_at,updated_by_account_id,created_at,updated_at)
                   VALUES(?,?,?,'audit',0,1,0,'any','any','[]','[]',1,NULL,?,?,?)""",
                (
                    policy_id,
                    context.workspace_id,
                    context.tenant_id,
                    context.account_id,
                    stamp,
                    stamp,
                ),
            )
            policy = _policy_row(connection, workspace_id)
        owner_ready = _owners_have_recovery(connection, context.workspace_id)
        affected = _affected_sessions(connection, policy)
        outcome = "simulation.ready" if owner_ready else "simulation.owner_blocked"
        simulation_id = _id("cas")
        stamp = _db_time(now)
        connection.execute(
            """INSERT INTO conditional_access_simulations(
                 conditional_access_simulation_id,workspace_id,tenant_id,policy_revision,
                 outcome_code,affected_session_count,owner_ready,simulated_by_account_id,
                 simulated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                simulation_id,
                context.workspace_id,
                context.tenant_id,
                policy["revision"],
                outcome,
                affected,
                int(owner_ready),
                context.account_id,
                stamp,
            ),
        )
        connection.execute(
            """UPDATE conditional_access_policies SET simulation_watermark_at=?,updated_at=?
                 WHERE conditional_access_policy_id=? AND revision=?""",
            (stamp, stamp, policy["conditional_access_policy_id"], policy["revision"]),
        )
        _write_event(
            connection,
            context=context,
            event_type="conditional_access.denied",
            subject_type="policy",
            subject_id=str(policy["conditional_access_policy_id"]),
            reason_code=("simulation_ready" if owner_ready else "simulation_owner_blocked"),
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "outcome_code": outcome,
                "affected_session_count": affected,
            },
            now=now,
        )
        policy = _policy_row(connection, workspace_id)
        simulation = _latest_simulation(connection, workspace_id)
        connection.commit()
        return _policy_summary(policy), _simulation_summary(simulation)
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("conditional_access.unavailable") from None
    finally:
        connection.close()


def update_conditional_access(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    mode: str,
    require_mfa: bool,
    require_step_up_for_high_risk: bool,
    allow_trusted_devices: bool,
    region_rule: str,
    network_rule: str,
    allowed_cidrs: tuple[str, ...],
    exception_membership_ids: tuple[str, ...],
    step_up_grant: str,
    request_id: str,
    trace_id: str,
) -> ConditionalAccessPolicySummary:
    normalized_cidrs = _canonical_cidrs(allowed_cidrs)
    _validate_policy_values(
        mode,
        region_rule,
        network_rule,
        normalized_cidrs,
        exception_membership_ids,
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        _require_workspace(context, workspace_id, "conditional_access.manage")
        now = _utcnow()
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=context,
            grant=step_up_grant,
            action_code="conditional_access.manage",
            resource_type="conditional_access_policy",
            resource_id=workspace_id,
            now=now,
        )
        current = _policy_row(connection, workspace_id)
        if current is None:
            if expected_revision != 0 or mode != "audit":
                raise ApiError("conditional_access.revision_conflict")
            _validate_exceptions(connection, context, exception_membership_ids)
            stamp = _db_time(now)
            connection.execute(
                """INSERT INTO conditional_access_policies(
                     conditional_access_policy_id,workspace_id,tenant_id,mode,require_mfa,
                     require_step_up_for_high_risk,allow_trusted_devices,region_rule,
                     network_rule,allowed_cidrs_json,exception_membership_ids_json,revision,
                     simulation_watermark_at,updated_by_account_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,?)""",
                (
                    _id("cap"),
                    context.workspace_id,
                    context.tenant_id,
                    mode,
                    int(require_mfa),
                    int(require_step_up_for_high_risk),
                    int(allow_trusted_devices),
                    region_rule,
                    network_rule,
                    _json_array(normalized_cidrs),
                    _json_array(exception_membership_ids),
                    context.account_id,
                    stamp,
                    stamp,
                ),
            )
        else:
            if int(current["revision"]) != expected_revision:
                raise ApiError("conditional_access.revision_conflict")
            _validate_exceptions(connection, context, exception_membership_ids)
            if mode == "enforced":
                _require_enforcement_preconditions(
                    connection,
                    current,
                    require_mfa=require_mfa,
                    require_step_up_for_high_risk=require_step_up_for_high_risk,
                    allow_trusted_devices=allow_trusted_devices,
                    region_rule=region_rule,
                    network_rule=network_rule,
                    allowed_cidrs=normalized_cidrs,
                    exception_membership_ids=exception_membership_ids,
                    now=now,
                )
            stamp = _db_time(now)
            updated = connection.execute(
                """UPDATE conditional_access_policies SET mode=?,require_mfa=?,
                     require_step_up_for_high_risk=?,allow_trusted_devices=?,region_rule=?,
                     network_rule=?,allowed_cidrs_json=?,exception_membership_ids_json=?,
                     revision=revision+1,simulation_watermark_at=NULL,
                     updated_by_account_id=?,updated_at=?
                   WHERE conditional_access_policy_id=? AND revision=?""",
                (
                    mode,
                    int(require_mfa),
                    int(require_step_up_for_high_risk),
                    int(allow_trusted_devices),
                    region_rule,
                    network_rule,
                    _json_array(normalized_cidrs),
                    _json_array(exception_membership_ids),
                    context.account_id,
                    stamp,
                    current["conditional_access_policy_id"],
                    expected_revision,
                ),
            ).rowcount
            if updated != 1:
                raise ApiError("conditional_access.revision_conflict")
        policy = _policy_row(connection, workspace_id)
        connection.execute(
            """UPDATE workspace_sessions SET revoke_generation=revoke_generation+1,
                 updated_at=? WHERE workspace_id=? AND status='active'""",
            (_db_time(now), workspace_id),
        )
        _write_event(
            connection,
            context=context,
            event_type="conditional_access.denied",
            subject_type="policy",
            subject_id=str(policy["conditional_access_policy_id"]),
            reason_code="policy_updated",
            request_id=request_id,
            trace_id=trace_id,
            payload={"schema_version": 1, "mode": mode, "revision": policy["revision"]},
            now=now,
        )
        connection.commit()
        return _policy_summary(policy)
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("conditional_access.unavailable") from None
    finally:
        connection.close()


def enforce_conditional_access_in_transaction(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    global_session_id: str,
    workspace_id: str,
    membership_id: str,
    client_host: str | None,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> int:
    policy = _policy_row(connection, workspace_id)
    if policy is None:
        return 0
    if membership_id in set(_json_tuple(policy["exception_membership_ids_json"])):
        return int(policy["revision"])
    failures: list[str] = []
    session = connection.execute(
        "SELECT assurance_level,trusted_device_id FROM global_sessions WHERE session_id=?",
        (global_session_id,),
    ).fetchone()
    if session is None:
        raise ApiError("conditional_access.unavailable")
    if policy["require_mfa"] == 1 and session["assurance_level"] != "mfa":
        trusted = None
        if policy["allow_trusted_devices"] == 1 and session["trusted_device_id"] is not None:
            trusted = connection.execute(
                """SELECT 1 FROM trusted_devices WHERE trusted_device_id=? AND account_id=?
                     AND status='active' AND expires_at>?""",
                (session["trusted_device_id"], account_id, _db_time(now)),
            ).fetchone()
        if trusted is None:
            failures.append("mfa_required")
    if policy["region_rule"] == "domestic_only":
        failures.append("region_unclassified")
    if policy["network_rule"] == "trusted_only" and not _host_allowed(
        client_host, _json_tuple(policy["allowed_cidrs_json"])
    ):
        failures.append("network_untrusted")
    if failures:
        _write_denied(
            connection,
            policy=policy,
            account_id=account_id,
            reason=("audit_would_deny" if policy["mode"] == "audit" else "policy_denied"),
            failure_codes=tuple(failures),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        if policy["mode"] == "enforced":
            raise ApiError("conditional_access.denied")
    return int(policy["revision"])


def session_satisfies_conditional_access_in_transaction(
    connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
) -> bool:
    policy = _policy_row(connection, str(row["workspace_id"]))
    if policy is None:
        return int(row["conditional_access_revision"]) == 0
    if int(row["conditional_access_revision"]) != int(policy["revision"]):
        return False
    if policy["mode"] != "enforced":
        return True
    if row["membership_id"] in set(_json_tuple(policy["exception_membership_ids_json"])):
        return True
    global_session = connection.execute(
        "SELECT assurance_level,trusted_device_id FROM global_sessions WHERE session_id=?",
        (row["global_session_id"],),
    ).fetchone()
    if global_session is None:
        return False
    if policy["require_mfa"] == 1 and global_session["assurance_level"] != "mfa":
        if policy["allow_trusted_devices"] != 1 or global_session["trusted_device_id"] is None:
            return False
        if connection.execute(
            """SELECT 1 FROM trusted_devices WHERE trusted_device_id=? AND account_id=?
                 AND status='active' AND expires_at>?""",
            (global_session["trusted_device_id"], row["account_id"], _db_time(now)),
        ).fetchone() is None:
            return False
    return True


def _require_enforcement_preconditions(
    connection: sqlite3.Connection,
    current: sqlite3.Row,
    *,
    require_mfa: bool,
    require_step_up_for_high_risk: bool,
    allow_trusted_devices: bool,
    region_rule: str,
    network_rule: str,
    allowed_cidrs: tuple[str, ...],
    exception_membership_ids: tuple[str, ...],
    now: datetime,
) -> None:
    unchanged = (
        bool(current["require_mfa"]) == require_mfa
        and bool(current["require_step_up_for_high_risk"])
        == require_step_up_for_high_risk
        and bool(current["allow_trusted_devices"]) == allow_trusted_devices
        and current["region_rule"] == region_rule
        and current["network_rule"] == network_rule
        and _json_tuple(current["allowed_cidrs_json"]) == allowed_cidrs
        and _json_tuple(current["exception_membership_ids_json"])
        == exception_membership_ids
    )
    latest = _latest_simulation(connection, str(current["workspace_id"]))
    watermark = current["simulation_watermark_at"]
    if (
        current["mode"] != "audit"
        or not require_mfa
        or not unchanged
        or watermark is None
        or _parse_time(watermark) < now - timedelta(minutes=15)
        or latest is None
        or int(latest["policy_revision"]) != int(current["revision"])
        or latest["owner_ready"] != 1
        or latest["outcome_code"] != "simulation.ready"
        or not _owners_have_recovery(connection, str(current["workspace_id"]))
    ):
        raise ApiError("conditional_access.precondition_failed")


def _owners_have_recovery(connection: sqlite3.Connection, workspace_id: str) -> bool:
    owners = connection.execute(
        """SELECT account_id FROM workspace_memberships
             WHERE workspace_id=? AND role_code='owner' AND status='active'""",
        (workspace_id,),
    ).fetchall()
    return bool(owners) and all(
        connection.execute(
            """SELECT 1 FROM account_mfa_factors factor
                 WHERE factor.account_id=? AND factor.status='active'
                   AND EXISTS(SELECT 1 FROM recovery_code_sets code_set
                     WHERE code_set.account_id=factor.account_id AND code_set.status='active')
                 LIMIT 1""",
            (owner["account_id"],),
        ).fetchone()
        is not None
        for owner in owners
    )


def _affected_sessions(connection: sqlite3.Connection, policy: sqlite3.Row) -> int:
    row = connection.execute(
        """SELECT COUNT(*) FROM workspace_sessions session
             JOIN global_sessions global_session
               ON global_session.session_id=session.global_session_id
            WHERE session.workspace_id=? AND session.status='active'
              AND (?=1 AND global_session.assurance_level!='mfa'
                OR ?!='any' OR ?!='any')""",
        (
            policy["workspace_id"],
            policy["require_mfa"],
            policy["region_rule"],
            policy["network_rule"],
        ),
    ).fetchone()
    return int(row[0])


def _require_workspace(
    context: WorkspaceSessionContext, workspace_id: str, permission: str
) -> None:
    if context.workspace_id != workspace_id:
        raise ApiError("conditional_access.forbidden")
    if permission not in context.permission_codes:
        raise ApiError("conditional_access.forbidden")


def _validate_policy_values(
    mode: str,
    region_rule: str,
    network_rule: str,
    allowed_cidrs: tuple[str, ...],
    exceptions: tuple[str, ...],
) -> None:
    if (
        mode not in {"audit", "enforced"}
        or region_rule not in {"any", "domestic_only"}
        or network_rule not in {"any", "trusted_only"}
        or len(allowed_cidrs) > 32
        or len(exceptions) > 32
        or len(set(exceptions)) != len(exceptions)
        or any(not value.startswith("wmb_") or len(value) != 36 for value in exceptions)
        or (network_rule == "trusted_only" and not allowed_cidrs)
    ):
        raise ApiError("conditional_access.request_invalid")


def _validate_exceptions(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    values: tuple[str, ...],
) -> None:
    if not values:
        return
    placeholders = ",".join("?" for _ in values)
    count = connection.execute(
        f"""SELECT COUNT(*) FROM workspace_memberships
              WHERE workspace_id=? AND tenant_id=?
                AND workspace_membership_id IN({placeholders})""",
        (context.workspace_id, context.tenant_id, *values),
    ).fetchone()[0]
    if count != len(values):
        raise ApiError("conditional_access.request_invalid")


def _canonical_cidrs(values: tuple[str, ...]) -> tuple[str, ...]:
    try:
        return tuple(sorted({str(ipaddress.ip_network(value, strict=True)) for value in values}))
    except (TypeError, ValueError):
        raise ApiError("conditional_access.request_invalid") from None


def _host_allowed(host: str | None, cidrs: tuple[str, ...]) -> bool:
    if host is None:
        return False
    try:
        address = ipaddress.ip_address(host)
        return any(address in ipaddress.ip_network(cidr, strict=True) for cidr in cidrs)
    except ValueError:
        return False


def _policy_row(connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM conditional_access_policies WHERE workspace_id=?", (workspace_id,)
    ).fetchone()


def _latest_simulation(
    connection: sqlite3.Connection, workspace_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM conditional_access_simulations WHERE workspace_id=?
             ORDER BY simulated_at DESC,conditional_access_simulation_id DESC LIMIT 1""",
        (workspace_id,),
    ).fetchone()


def _policy_summary(row: sqlite3.Row) -> ConditionalAccessPolicySummary:
    return ConditionalAccessPolicySummary(
        policy_id=row["conditional_access_policy_id"],
        workspace_id=row["workspace_id"],
        mode=row["mode"],
        require_mfa=bool(row["require_mfa"]),
        require_step_up_for_high_risk=bool(row["require_step_up_for_high_risk"]),
        allow_trusted_devices=bool(row["allow_trusted_devices"]),
        region_rule=row["region_rule"],
        network_rule=row["network_rule"],
        allowed_cidrs=_json_tuple(row["allowed_cidrs_json"]),
        exception_membership_ids=_json_tuple(row["exception_membership_ids_json"]),
        revision=row["revision"],
        simulation_watermark_at=_optional_time(row["simulation_watermark_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _simulation_summary(row: sqlite3.Row) -> ConditionalAccessSimulationSummary:
    return ConditionalAccessSimulationSummary(
        simulation_id=row["conditional_access_simulation_id"],
        policy_revision=row["policy_revision"],
        outcome_code=row["outcome_code"],
        affected_session_count=row["affected_session_count"],
        owner_ready=bool(row["owner_ready"]),
        simulated_at=_parse_time(row["simulated_at"]),
    )


def _write_denied(
    connection: sqlite3.Connection,
    *,
    policy: sqlite3.Row,
    account_id: str,
    reason: str,
    failure_codes: tuple[str, ...],
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    context = _EventContext(
        workspace_id=str(policy["workspace_id"]),
        tenant_id=str(policy["tenant_id"]),
        account_id=account_id,
    )
    _write_event(
        connection,
        context=context,
        event_type="conditional_access.denied",
        subject_type="policy",
        subject_id=str(policy["conditional_access_policy_id"]),
        reason_code=reason,
        request_id=request_id,
        trace_id=trace_id,
        payload={"schema_version": 1, "failure_codes": list(failure_codes)},
        now=now,
    )


@dataclass(frozen=True, slots=True)
class _EventContext:
    workspace_id: str
    tenant_id: str
    account_id: str


def _write_event(
    connection: sqlite3.Connection,
    *,
    context: WorkspaceSessionContext | _EventContext,
    event_type: str,
    subject_type: str,
    subject_id: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    now: datetime,
) -> None:
    body = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    event_id = _id("ise")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO identity_security_events(
             identity_security_event_id,account_id,workspace_id,tenant_id,actor_account_id,
             event_type,subject_type,subject_id,reason_code,request_id,trace_id,payload_json,
             payload_sha256,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            context.account_id,
            context.workspace_id,
            context.tenant_id,
            context.account_id,
            event_type,
            subject_type,
            subject_id,
            reason_code,
            request_id,
            trace_id,
            body,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO identity_security_event_outbox(
             identity_security_event_outbox_id,identity_security_event_id,status,
             attempt_count,next_attempt_at,delivered_at,created_at,updated_at)
           VALUES(?,?,'pending',0,?,NULL,?,?)""",
        (_id("iso"), event_id, stamp, stamp, stamp),
    )


def _json_tuple(value: str) -> tuple[str, ...]:
    parsed = json.loads(value)
    if type(parsed) is not list or any(type(item) is not str for item in parsed):
        raise ValueError("conditional access JSON")
    return tuple(parsed)


def _json_array(values: tuple[str, ...]) -> str:
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"))


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("conditional_access.unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _optional_time(value: str | None) -> datetime | None:
    return _parse_time(value) if value is not None else None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utcnow() -> datetime:
    return datetime.now(UTC)
