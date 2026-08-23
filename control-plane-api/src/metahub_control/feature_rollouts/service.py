"""Transactional feature flag management and Workspace capability evaluation."""

from __future__ import annotations

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
    PreparedPlatformJob,
    enqueue_prepared_platform_job_in_transaction,
    prepare_platform_job,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    FeatureFlagCreateRequest,
    FeatureFlagInfo,
    FeatureFlagListResponse,
    FeatureFlagResponse,
    FeatureFlagVersionInfo,
    FeatureKillRequest,
    FeatureRolloutCommandRequest,
    FeatureRolloutInfo,
    FeatureRolloutResponse,
    FeatureRuleSet,
    WorkspaceCapabilitiesResponse,
    WorkspaceCapability,
)

_FLAG_ID_RE = re.compile(r"^ffg_[0-9a-f]{32}$")
_ROLLOUT_ID_RE = re.compile(r"^fro_[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_ACTIVE_ROLLOUTS = frozenset({"pending_approval", "scheduled", "running"})


def list_feature_flags(
    config: AppConfig,
    *,
    session_token: str,
    status: str | None,
    risk_level: str | None,
    limit: int,
    request_id: str,
    trace_id: str,
) -> FeatureFlagListResponse:
    if status not in {None, "active", "killed", "disabled"} or risk_level not in {
        None,
        "standard",
        "p0",
    }:
        raise ApiError("feature_flag.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require(actor, "platform.feature.read")
        clauses: list[str] = []
        values: list[object] = []
        if status is not None:
            clauses.append("status=?")
            values.append(status)
        if risk_level is not None:
            clauses.append("risk_level=?")
            values.append(risk_level)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        ids = connection.execute(
            f"SELECT feature_flag_id FROM feature_flags{where} "
            "ORDER BY updated_at DESC,feature_flag_id LIMIT ?",
            (*values, limit),
        ).fetchall()
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM feature_flags{where}", tuple(values)
            ).fetchone()[0]
        )
        flags = tuple(_flag_info(connection, str(row[0])) for row in ids)
        connection.commit()
        return FeatureFlagListResponse(
            flags=flags,
            total=total,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("feature_flag.unavailable") from None
    finally:
        connection.close()


def create_feature_flag(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int | None,
    idempotency_key: str,
    payload: FeatureFlagCreateRequest,
    request_id: str,
    trace_id: str,
) -> FeatureFlagResponse:
    _valid_idempotency(idempotency_key)
    flag_id = payload.flag_id or _id("ffg_")
    version_id = _id("ffv_")
    canonical = payload.model_dump(mode="json")
    request_sha256 = _digest(canonical)
    idempotency_sha256 = _secret_digest(config, "command", idempotency_key)
    rules_json = _canonical(payload.rules.model_dump(mode="json"))
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
        _require(actor, "platform.feature.write")
        replay = _command_replay(
            connection,
            actor,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
        )
        if replay is not None:
            result = FeatureFlagResponse(
                flag=_flag_info(connection, str(replay["feature_flag_id"])),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        _validate_rule_references(connection, payload.flag_code, payload.rules)
        now = _utcnow()
        stamp = _db_time(now)
        if payload.action == "create":
            if expected_revision is not None or connection.execute(
                "SELECT 1 FROM feature_flags WHERE flag_code=?", (payload.flag_code,)
            ).fetchone():
                raise ApiError("feature_flag.conflict")
            version_number = 1
            result_revision = 1
            connection.execute(
                """
                INSERT INTO feature_flags(
                  feature_flag_id,flag_code,display_name,risk_level,status,current_version_id,
                  rollout_percentage_bps,revision,kill_generation,kill_reason_code,
                  created_by_operator_id,created_at,updated_at)
                VALUES(?,?,?,?,'active',?,0,1,0,NULL,?,?,?)
                """,
                (
                    flag_id,
                    payload.flag_code,
                    payload.display_name.strip(),
                    payload.risk_level,
                    version_id,
                    actor.operator_id,
                    stamp,
                    stamp,
                ),
            )
        else:
            flag = _flag_row(connection, flag_id)
            if expected_revision is None or int(flag["revision"]) != expected_revision:
                raise ApiError("feature_flag.revision_conflict")
            if (
                flag["status"] != "active"
                or flag["flag_code"] != payload.flag_code
                or flag["display_name"] != payload.display_name.strip()
                or flag["risk_level"] != payload.risk_level
            ):
                raise ApiError("feature_flag.conflict")
            version_number = int(
                connection.execute(
                    "SELECT max(version_number) FROM feature_flag_versions "
                    "WHERE feature_flag_id=?",
                    (flag_id,),
                ).fetchone()[0]
            ) + 1
            result_revision = expected_revision + 1
        connection.execute(
            """
            INSERT INTO feature_flag_versions(
              feature_flag_version_id,feature_flag_id,version_number,rules_json,rules_sha256,
              created_by_operator_id,created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                version_id,
                flag_id,
                version_number,
                rules_json,
                hashlib.sha256(rules_json.encode("ascii")).hexdigest(),
                actor.operator_id,
                stamp,
            ),
        )
        if payload.action == "version":
            connection.execute(
                "UPDATE feature_flags SET revision=?,updated_at=? "
                "WHERE feature_flag_id=? AND revision=?",
                (
                    result_revision,
                    _db_time(_changed_time(now, flag["updated_at"])),
                    flag_id,
                    expected_revision,
                ),
            )
        _insert_command(
            connection,
            actor=actor,
            action=f"flag.{payload.action}",
            flag_id=flag_id,
            rollout_id=None,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_revision=result_revision,
            now=now,
        )
        _audit(
            connection,
            actor=actor,
            action=f"feature_flag.{payload.action}",
            object_type=(
                "feature_flag" if payload.action == "create" else "feature_flag_version"
            ),
            object_id=flag_id if payload.action == "create" else version_id,
            reason="release_start",
            request_id=request_id,
            trace_id=trace_id,
            after={
                "flag_code": payload.flag_code,
                "risk_level": payload.risk_level,
                "version_number": version_number,
            },
            now=now,
        )
        result = FeatureFlagResponse(
            flag=_flag_info(connection, flag_id),
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
        raise ApiError("feature_flag.unavailable") from None
    finally:
        connection.close()


def command_feature_rollout(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    flag_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: FeatureRolloutCommandRequest,
    request_id: str,
    trace_id: str,
) -> FeatureRolloutResponse:
    _valid_flag_id(flag_id)
    _valid_idempotency(idempotency_key)
    if expected_revision < 1:
        raise ApiError("feature_flag.request_invalid")
    created_rollout_id = _id("fro_") if payload.action in {"start", "rollback"} else None
    target_rollout_id = created_rollout_id or payload.rollout_id
    if target_rollout_id is None:
        raise ApiError("feature_flag.request_invalid")
    prepared = _prepare_rollout_job(
        config,
        flag_id=flag_id,
        rollout_id=target_rollout_id,
        action=payload.action,
    ) if payload.action in {"start", "approve", "resume", "rollback"} else None
    canonical = {"flag_id": flag_id, **payload.model_dump(mode="json")}
    request_sha256 = _digest(canonical)
    idempotency_sha256 = _secret_digest(config, "command", idempotency_key)
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
        permission = (
            "platform.feature.approve"
            if payload.action == "approve"
            else "platform.feature.write"
        )
        _require(actor, permission)
        replay = _command_replay(
            connection,
            actor,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
        )
        if replay is not None:
            rollout_id = str(replay["feature_rollout_id"])
            result = FeatureRolloutResponse(
                flag=_flag_info(connection, str(replay["feature_flag_id"])),
                rollout=_rollout_info(connection, rollout_id),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        flag = _flag_row(connection, flag_id)
        if flag["status"] != "active":
            raise ApiError("feature_flag.conflict")
        now = _utcnow()
        if payload.action == "start":
            rollout_id = target_rollout_id
            rollout_revision = _start_rollout(
                connection,
                config,
                actor=actor,
                flag=flag,
                rollout_id=rollout_id,
                expected_revision=expected_revision,
                payload=payload,
                prepared=prepared,
                idempotency_key=idempotency_key,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        elif payload.action == "approve":
            rollout_id = str(payload.rollout_id)
            rollout_revision = _approve_rollout(
                connection,
                config,
                actor=actor,
                flag=flag,
                rollout_id=rollout_id,
                expected_revision=expected_revision,
                prepared=prepared,
                idempotency_key=idempotency_key,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        elif payload.action == "pause":
            rollout_id = str(payload.rollout_id)
            rollout_revision = _pause_rollout(
                connection,
                actor=actor,
                flag=flag,
                rollout_id=rollout_id,
                expected_revision=expected_revision,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        elif payload.action == "resume":
            rollout_id = str(payload.rollout_id)
            rollout_revision = _resume_rollout(
                connection,
                config,
                actor=actor,
                flag=flag,
                rollout_id=rollout_id,
                expected_revision=expected_revision,
                prepared=prepared,
                idempotency_key=idempotency_key,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        else:
            rollout_id = target_rollout_id
            rollout_revision = _rollback_rollout(
                connection,
                config,
                actor=actor,
                flag=flag,
                source_rollout_id=str(payload.rollout_id),
                rollout_id=rollout_id,
                expected_revision=expected_revision,
                prepared=prepared,
                idempotency_key=idempotency_key,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        action_code = f"rollout.{payload.action}"
        _insert_command(
            connection,
            actor=actor,
            action=action_code,
            flag_id=flag_id,
            rollout_id=rollout_id,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_revision=rollout_revision,
            now=now,
        )
        _audit(
            connection,
            actor=actor,
            action=f"feature_rollout.{payload.action}",
            object_type="feature_rollout",
            object_id=rollout_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={"flag_id": flag_id, "revision": rollout_revision},
            now=now,
        )
        result = FeatureRolloutResponse(
            flag=_flag_info(connection, flag_id),
            rollout=_rollout_info(connection, rollout_id),
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
        raise ApiError("feature_flag.unavailable") from None
    finally:
        connection.close()


def kill_feature_flag(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    flag_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: FeatureKillRequest,
    request_id: str,
    trace_id: str,
) -> FeatureFlagResponse:
    _valid_flag_id(flag_id)
    _valid_idempotency(idempotency_key)
    rollout_id = _id("fro_")
    prepared = _prepare_rollout_job(
        config, flag_id=flag_id, rollout_id=rollout_id, action="kill"
    )
    canonical = {"flag_id": flag_id, **payload.model_dump(mode="json")}
    request_sha256 = _digest(canonical)
    idempotency_sha256 = _secret_digest(config, "command", idempotency_key)
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
        _require(actor, "platform.feature.kill")
        replay = _command_replay(
            connection,
            actor,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
        )
        if replay is not None:
            result = FeatureFlagResponse(
                flag=_flag_info(connection, str(replay["feature_flag_id"])),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        flag = _flag_row(connection, flag_id)
        if int(flag["revision"]) != expected_revision:
            raise ApiError("feature_flag.revision_conflict")
        if flag["status"] == "killed":
            raise ApiError("feature_flag.conflict")
        now = _utcnow()
        stamp = _db_time(now)
        job_id = _enqueue_rollout_job(
            connection,
            config,
            prepared=prepared,
            idempotency_key=idempotency_key + ":job",
            request_id=request_id,
            trace_id=trace_id,
            priority=100,
        )
        version = connection.execute(
            "SELECT version_number FROM feature_flag_versions WHERE feature_flag_version_id=?",
            (flag["current_version_id"],),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO feature_rollouts(
              feature_rollout_id,feature_flag_id,feature_flag_version_id,action_kind,status,
              target_percentage_bps,environment_code,region_code,platform_job_id,
              requested_by_operator_id,reason_code,health_status,revision,request_id,trace_id,
              scheduled_at,started_at,completed_at,created_at,updated_at)
            VALUES(?,?,?,'kill','killed',0,'production','global',?,?,'emergency_kill',
              'healthy',1,?,?,?,NULL,?,?,?)
            """,
            (
                rollout_id,
                flag_id,
                flag["current_version_id"],
                job_id,
                actor.operator_id,
                request_id,
                trace_id,
                stamp,
                stamp,
                stamp,
                stamp,
            ),
        )
        active = connection.execute(
            "SELECT feature_rollout_id,revision,updated_at FROM feature_rollouts "
            "WHERE feature_flag_id=? AND status IN('pending_approval','scheduled','running')",
            (flag_id,),
        ).fetchall()
        for row in active:
            connection.execute(
                """UPDATE feature_rollouts
                   SET status='killed',revision=?,completed_at=?,updated_at=?
                   WHERE feature_rollout_id=? AND revision=?""",
                (
                    int(row["revision"]) + 1,
                    stamp,
                    _db_time(_changed_time(now, row["updated_at"])),
                    row["feature_rollout_id"],
                    int(row["revision"]),
                ),
            )
        changed_at = _changed_time(now, flag["updated_at"])
        connection.execute(
            """
            UPDATE feature_flags SET status='killed',rollout_percentage_bps=0,
              revision=revision+1,kill_generation=kill_generation+1,kill_reason_code=?,updated_at=?
            WHERE feature_flag_id=? AND revision=?
            """,
            (
                payload.reason_code,
                _db_time(changed_at),
                flag_id,
                expected_revision,
            ),
        )
        _event(
            connection,
            event_type="feature.killed",
            flag_id=flag_id,
            rollout_id=rollout_id,
            workspace_id=None,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _insert_command(
            connection,
            actor=actor,
            action="flag.kill",
            flag_id=flag_id,
            rollout_id=rollout_id,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_revision=expected_revision + 1,
            now=now,
        )
        _audit(
            connection,
            actor=actor,
            action="feature_flag.kill",
            object_type="feature_flag",
            object_id=flag_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={
                "kill_generation": int(flag["kill_generation"]) + 1,
                "version": int(version[0]),
            },
            now=now,
        )
        result = FeatureFlagResponse(
            flag=_flag_info(connection, flag_id),
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
        raise ApiError("feature_flag.unavailable") from None
    finally:
        connection.close()


def get_workspace_capabilities(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceCapabilitiesResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        evaluated_at = _utcnow()
        context = connection.execute(
            """
            SELECT state.status operational_status,region.region_code,
              CASE WHEN EXISTS(
                SELECT 1 FROM workspace_subscriptions subscription
                JOIN entitlement_snapshots entitlement
                  ON entitlement.subscription_id=subscription.subscription_id
                WHERE subscription.workspace_id=? AND subscription.tenant_id=?
                  AND subscription.status IN('active','trialing')
                  AND unixepoch(entitlement.effective_from)<=unixepoch(?)
                  AND (entitlement.effective_until IS NULL
                    OR unixepoch(entitlement.effective_until)>unixepoch(?))
              ) THEN 1 ELSE 0 END entitled
            FROM workspace_operational_states state
            JOIN workspace_regions region
              ON region.workspace_id=state.workspace_id AND region.tenant_id=state.tenant_id
            WHERE state.workspace_id=? AND state.tenant_id=?
            """,
            (
                actor.workspace_id,
                actor.tenant_id,
                _db_time(evaluated_at),
                _db_time(evaluated_at),
                actor.workspace_id,
                actor.tenant_id,
            ),
        ).fetchone()
        if context is None:
            raise ApiError("feature_flag.unavailable")
        rows = connection.execute(
            """
            SELECT flag.*,version.version_number,version.rules_json
            FROM feature_flags flag
            JOIN feature_flag_versions version
              ON version.feature_flag_version_id=flag.current_version_id
            ORDER BY flag.flag_code
            """
        ).fetchall()
        by_code = {str(row["flag_code"]): row for row in rows}
        resolved: dict[str, WorkspaceCapability] = {}
        resolving: set[str] = set()
        for code in by_code:
            _evaluate_capability(
                connection,
                config,
                actor=actor,
                context=context,
                by_code=by_code,
                resolved=resolved,
                resolving=resolving,
                flag_code=code,
            )
        for code, capability in resolved.items():
            _record_evaluation_snapshot(
                connection,
                config,
                actor=actor,
                flag=by_code[code],
                capability=capability,
                evaluated_at=evaluated_at,
            )
        response = WorkspaceCapabilitiesResponse(
            capabilities=tuple(resolved[code] for code in sorted(resolved)),
            evaluated_at=evaluated_at,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("feature_flag.unavailable") from None
    finally:
        connection.close()


def record_feature_exposure_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    actor: WorkspaceSessionContext,
    flag_code: str,
    request_id: str,
    trace_id: str,
) -> bool:
    """Append one pseudonymous exposure for an already-authorized feature consumer."""

    row = connection.execute(
        """
        SELECT flag.feature_flag_id,flag.current_version_id,flag.status,
          flag.rollout_percentage_bps,version.version_number
        FROM feature_flags flag JOIN feature_flag_versions version
          ON version.feature_flag_version_id=flag.current_version_id
        WHERE flag.flag_code=?
        """,
        (flag_code,),
    ).fetchone()
    if row is None or row["status"] != "active" or int(row["rollout_percentage_bps"]) <= 0:
        return False
    subject = _secret_digest(config, "subject", actor.account_id)
    request_sha256 = _secret_digest(config, "request", request_id)
    now = _utcnow()
    inserted = connection.execute(
        """
        INSERT OR IGNORE INTO feature_exposures(
          feature_exposure_id,feature_flag_id,feature_flag_version_id,workspace_id,
          tenant_id,subject_pseudonym,request_sha256,exposed_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            _id("fex_"),
            row["feature_flag_id"],
            row["current_version_id"],
            actor.workspace_id,
            actor.tenant_id,
            subject,
            request_sha256,
            _db_time(now),
        ),
    ).rowcount
    if inserted:
        _event(
            connection,
            event_type="feature.exposed",
            flag_id=str(row["feature_flag_id"]),
            rollout_id=None,
            workspace_id=actor.workspace_id,
            reason="enabled",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    return bool(inserted)


def _record_evaluation_snapshot(
    connection,
    config: AppConfig,
    *,
    actor: WorkspaceSessionContext,
    flag,
    capability: WorkspaceCapability,
    evaluated_at: datetime,
) -> None:
    subject_pseudonym = _secret_digest(config, "subject", actor.account_id)
    safe_result = {
        "flag_code": capability.flag_code,
        "public_version": capability.public_version,
        "enabled": capability.enabled,
        "reason_code": capability.reason_code,
        "workspace_pseudonym": _secret_digest(
            config, "workspace", actor.workspace_id
        ),
    }
    connection.execute(
        """
        INSERT INTO flag_evaluation_snapshots(
          flag_evaluation_snapshot_id,feature_flag_id,feature_flag_version_id,
          workspace_id,tenant_id,subject_pseudonym,enabled,reason_code,
          evaluation_sha256,evaluated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("fes_"),
            flag["feature_flag_id"],
            flag["current_version_id"],
            actor.workspace_id,
            actor.tenant_id,
            subject_pseudonym,
            1 if capability.enabled else 0,
            capability.reason_code,
            _secret_digest(config, "evaluation", _canonical(safe_result)),
            _db_time(evaluated_at),
        ),
    )


def _start_rollout(
    connection,
    config: AppConfig,
    *,
    actor: PlatformContext,
    flag,
    rollout_id: str,
    expected_revision: int,
    payload: FeatureRolloutCommandRequest,
    prepared: PreparedPlatformJob | None,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> int:
    if int(flag["revision"]) != expected_revision:
        raise ApiError("feature_flag.revision_conflict")
    if connection.execute(
        "SELECT 1 FROM feature_rollouts WHERE feature_flag_id=? "
        "AND status IN('pending_approval','scheduled','running')",
        (flag["feature_flag_id"],),
    ).fetchone():
        raise ApiError("feature_flag.conflict")
    version = connection.execute(
        "SELECT * FROM feature_flag_versions WHERE feature_flag_id=? AND version_number=?",
        (flag["feature_flag_id"], payload.version_number),
    ).fetchone()
    if version is None:
        raise ApiError("feature_flag.conflict")
    current_version = int(
        connection.execute(
            "SELECT version_number FROM feature_flag_versions "
            "WHERE feature_flag_version_id=?",
            (flag["current_version_id"],),
        ).fetchone()[0]
    )
    if int(payload.version_number or 0) < current_version or (
        int(payload.version_number or 0) == current_version
        and int(payload.percentage_bps or 0) <= int(flag["rollout_percentage_bps"])
    ):
        raise ApiError("feature_flag.conflict")
    rules = FeatureRuleSet.model_validate(json.loads(version["rules_json"]))
    if int(payload.percentage_bps or 0) > rules.percentage_bps:
        raise ApiError("feature_flag.request_invalid")
    status = "pending_approval" if flag["risk_level"] == "p0" else "scheduled"
    job_id = None
    health_watermark = None
    stamp = _db_time(now)
    if status == "scheduled":
        if prepared is None:
            raise ApiError("feature_flag.unavailable")
        health_watermark = _ensure_health_summary(
            connection, payload.region_code or "global", now, config
        )
        job_id = _enqueue_rollout_job(
            connection,
            config,
            prepared=prepared,
            idempotency_key=idempotency_key + ":job",
            request_id=request_id,
            trace_id=trace_id,
        )
    connection.execute(
        """
        INSERT INTO feature_rollouts(
          feature_rollout_id,feature_flag_id,feature_flag_version_id,action_kind,status,
          target_percentage_bps,environment_code,region_code,platform_job_id,
          requested_by_operator_id,reason_code,health_status,health_source_watermark,
          revision,request_id,trace_id,
          scheduled_at,started_at,completed_at,created_at,updated_at)
        VALUES(?,?,?,'start',?,?,?,?,?,?,'release_start','pending',?,1,?,?,?,NULL,NULL,?,?)
        """,
        (
            rollout_id,
            flag["feature_flag_id"],
            version["feature_flag_version_id"],
            status,
            payload.percentage_bps,
            payload.environment_code,
            payload.region_code,
            job_id,
            actor.operator_id,
            health_watermark,
            request_id,
            trace_id,
            stamp if status == "scheduled" else None,
            stamp,
            stamp,
        ),
    )
    if status == "scheduled":
        _event(
            connection,
            event_type="feature.rollout_started",
            flag_id=str(flag["feature_flag_id"]),
            rollout_id=rollout_id,
            workspace_id=None,
            reason="release_start",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    return 1


def _approve_rollout(
    connection,
    config: AppConfig,
    *,
    actor: PlatformContext,
    flag,
    rollout_id: str,
    expected_revision: int,
    prepared: PreparedPlatformJob | None,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> int:
    rollout = _rollout_row(connection, rollout_id, flag["feature_flag_id"])
    if rollout["status"] != "pending_approval" or int(rollout["revision"]) != expected_revision:
        raise ApiError(
            "feature_flag.revision_conflict"
            if int(rollout["revision"]) != expected_revision
            else "feature_flag.conflict"
        )
    if rollout["requested_by_operator_id"] == actor.operator_id:
        raise ApiError("feature_flag.approval_conflict")
    role = "owner" if "platform_owner" in actor.roles else "release"
    try:
        connection.execute(
            """INSERT INTO feature_rollout_approvals(
                 feature_rollout_approval_id,feature_rollout_id,operator_id,approval_role,
                 rollout_revision,approved_at) VALUES(?,?,?,?,?,?)""",
            (
                _id("fap_"),
                rollout_id,
                actor.operator_id,
                role,
                expected_revision,
                _db_time(now),
            ),
        )
    except sqlite3.IntegrityError:
        raise ApiError("feature_flag.approval_conflict") from None
    approval_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM feature_rollout_approvals WHERE feature_rollout_id=?",
            (rollout_id,),
        ).fetchone()[0]
    )
    next_revision = expected_revision + 1
    changed_at = _db_time(_changed_time(now, rollout["updated_at"]))
    if approval_count >= 2:
        if prepared is None:
            raise ApiError("feature_flag.unavailable")
        health_watermark = _ensure_health_summary(
            connection, str(rollout["region_code"]), now, config
        )
        job_id = _enqueue_rollout_job(
            connection,
            config,
            prepared=prepared,
            idempotency_key=idempotency_key + ":job",
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.execute(
            """UPDATE feature_rollouts SET status='scheduled',platform_job_id=?,
                 health_source_watermark=?,revision=?,scheduled_at=?,updated_at=?
               WHERE feature_rollout_id=? AND revision=?""",
            (
                job_id,
                health_watermark,
                next_revision,
                _db_time(now),
                changed_at,
                rollout_id,
                expected_revision,
            ),
        )
        _event(
            connection,
            event_type="feature.rollout_started",
            flag_id=str(flag["feature_flag_id"]),
            rollout_id=rollout_id,
            workspace_id=None,
            reason="release_start",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    else:
        connection.execute(
            "UPDATE feature_rollouts SET revision=?,updated_at=? "
            "WHERE feature_rollout_id=? AND revision=?",
            (next_revision, changed_at, rollout_id, expected_revision),
        )
    return next_revision


def _pause_rollout(
    connection,
    *,
    actor: PlatformContext,
    flag,
    rollout_id: str,
    expected_revision: int,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> int:
    del actor
    rollout = _rollout_row(connection, rollout_id, flag["feature_flag_id"])
    if int(rollout["revision"]) != expected_revision:
        raise ApiError("feature_flag.revision_conflict")
    if rollout["status"] not in {"scheduled", "running"}:
        raise ApiError("feature_flag.conflict")
    next_revision = expected_revision + 1
    connection.execute(
        "UPDATE feature_rollouts SET status='paused',revision=?,updated_at=? "
        "WHERE feature_rollout_id=? AND revision=?",
        (
            next_revision,
            _db_time(_changed_time(now, rollout["updated_at"])),
            rollout_id,
            expected_revision,
        ),
    )
    _event(
        connection,
        event_type="feature.rollout_paused",
        flag_id=str(flag["feature_flag_id"]),
        rollout_id=rollout_id,
        workspace_id=None,
        reason="operator_pause",
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )
    return next_revision


def _resume_rollout(
    connection,
    config: AppConfig,
    *,
    actor: PlatformContext,
    flag,
    rollout_id: str,
    expected_revision: int,
    prepared: PreparedPlatformJob | None,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> int:
    del actor
    rollout = _rollout_row(connection, rollout_id, flag["feature_flag_id"])
    if int(rollout["revision"]) != expected_revision:
        raise ApiError("feature_flag.revision_conflict")
    if rollout["status"] != "paused" or prepared is None:
        raise ApiError("feature_flag.conflict")
    health_watermark = _ensure_health_summary(
        connection, str(rollout["region_code"]), now, config
    )
    job_id = _enqueue_rollout_job(
        connection,
        config,
        prepared=prepared,
        idempotency_key=idempotency_key + ":job",
        request_id=request_id,
        trace_id=trace_id,
    )
    next_revision = expected_revision + 1
    connection.execute(
        """UPDATE feature_rollouts SET status='scheduled',
             platform_job_id=?,health_status='pending',health_source_watermark=?,
             revision=?,scheduled_at=?,updated_at=?
           WHERE feature_rollout_id=? AND revision=?""",
        (
            job_id,
            health_watermark,
            next_revision,
            _db_time(now),
            _db_time(_changed_time(now, rollout["updated_at"])),
            rollout_id,
            expected_revision,
        ),
    )
    _event(
        connection,
        event_type="feature.rollout_started",
        flag_id=str(flag["feature_flag_id"]),
        rollout_id=rollout_id,
        workspace_id=None,
        reason="health_recovery",
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )
    return next_revision


def _rollback_rollout(
    connection,
    config: AppConfig,
    *,
    actor: PlatformContext,
    flag,
    source_rollout_id: str,
    rollout_id: str,
    expected_revision: int,
    prepared: PreparedPlatformJob | None,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> int:
    source = _rollout_row(connection, source_rollout_id, flag["feature_flag_id"])
    if int(source["revision"]) != expected_revision:
        raise ApiError("feature_flag.revision_conflict")
    if source["status"] != "completed" or prepared is None:
        raise ApiError("feature_flag.conflict")
    current_version = connection.execute(
        "SELECT version_number FROM feature_flag_versions WHERE feature_flag_version_id=?",
        (flag["current_version_id"],),
    ).fetchone()
    previous = connection.execute(
        """SELECT * FROM feature_flag_versions WHERE feature_flag_id=? AND version_number<?
           ORDER BY version_number DESC LIMIT 1""",
        (flag["feature_flag_id"], int(current_version[0])),
    ).fetchone()
    if previous is None:
        raise ApiError("feature_flag.conflict")
    health_watermark = _ensure_health_summary(
        connection, str(source["region_code"]), now, config
    )
    job_id = _enqueue_rollout_job(
        connection,
        config,
        prepared=prepared,
        idempotency_key=idempotency_key + ":job",
        request_id=request_id,
        trace_id=trace_id,
    )
    stamp = _db_time(now)
    connection.execute(
        """
        INSERT INTO feature_rollouts(
          feature_rollout_id,feature_flag_id,feature_flag_version_id,action_kind,status,
          target_percentage_bps,environment_code,region_code,platform_job_id,
          requested_by_operator_id,reason_code,health_status,health_source_watermark,
          revision,request_id,trace_id,
          scheduled_at,started_at,completed_at,created_at,updated_at)
        VALUES(?,?,?,'rollback','scheduled',?,?,?,?,?,'release_rollback','pending',?,1,?,?,?,
          NULL,NULL,?,?)
        """,
        (
            rollout_id,
            flag["feature_flag_id"],
            previous["feature_flag_version_id"],
            int(source["target_percentage_bps"]),
            source["environment_code"],
            source["region_code"],
            job_id,
            actor.operator_id,
            health_watermark,
            request_id,
            trace_id,
            stamp,
            stamp,
            stamp,
        ),
    )
    _event(
        connection,
        event_type="feature.rollout_started",
        flag_id=str(flag["feature_flag_id"]),
        rollout_id=rollout_id,
        workspace_id=None,
        reason="release_rollback",
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )
    return 1


def _prepare_rollout_job(
    config: AppConfig, *, flag_id: str, rollout_id: str, action: str
) -> PreparedPlatformJob:
    return prepare_platform_job(
        config,
        type_code="feature.rollout.step",
        payload={"flag_id": flag_id, "rollout_id": rollout_id, "action": action},
    )


def _enqueue_rollout_job(
    connection,
    config: AppConfig,
    *,
    prepared: PreparedPlatformJob,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    priority: int = 90,
) -> str:
    job_id, _replay = enqueue_prepared_platform_job_in_transaction(
        connection,
        config,
        prepared=prepared,
        priority=priority,
        source_code="feature.rollout",
        idempotency_key=idempotency_key,
        request_id=request_id,
        trace_id=trace_id,
    )
    return job_id


def _ensure_health_summary(
    connection, region_code: str, now: datetime, config: AppConfig
) -> int:
    current = connection.execute(
        """SELECT source_watermark FROM rollout_health_summaries
           WHERE service_code='gateway' AND region_code=? AND unixepoch(expires_at)>unixepoch(?)
           ORDER BY source_watermark DESC LIMIT 1""",
        (region_code, _db_time(now)),
    ).fetchone()
    if current is not None:
        return int(current["source_watermark"])
    cells = connection.execute(
        """SELECT COUNT(*) total,
             SUM(CASE WHEN status='active' AND health_status='healthy' THEN 1 ELSE 0 END) healthy,
             COALESCE(MAX(revision),0) watermark
           FROM gateway_cells WHERE region_code=?""",
        (region_code,),
    ).fetchone()
    total = int(cells["total"])
    healthy = int(cells["healthy"] or 0)
    health_status = "healthy" if total > 0 and healthy == total else "unavailable"
    watermark = max(1, int(cells["watermark"]) + int(now.timestamp() * 1000))
    body = {
        "service_code": "gateway",
        "region_code": region_code,
        "watermark": watermark,
        "health_status": health_status,
        "error_rate_bps": 0 if health_status == "healthy" else 10000,
        "latency_p95_ms": 0,
    }
    connection.execute(
        """
        INSERT INTO rollout_health_summaries(
          rollout_health_summary_id,service_code,region_code,source_watermark,health_status,
          error_rate_bps,latency_p95_ms,summary_sha256,observed_at,expires_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("rhs_"),
            "gateway",
            region_code,
            watermark,
            health_status,
            body["error_rate_bps"],
            0,
            _secret_digest(config, "health-summary", _canonical(body)),
            _db_time(now),
            _db_time(now + timedelta(minutes=5)),
        ),
    )
    return watermark


def _evaluate_capability(
    connection,
    config: AppConfig,
    *,
    actor: WorkspaceSessionContext,
    context,
    by_code: dict[str, object],
    resolved: dict[str, WorkspaceCapability],
    resolving: set[str],
    flag_code: str,
) -> WorkspaceCapability:
    if flag_code in resolved:
        return resolved[flag_code]
    row = by_code[flag_code]
    if flag_code in resolving:
        capability = _capability(row, False, "prerequisite_disabled")
        resolved[flag_code] = capability
        return capability
    resolving.add(flag_code)
    rules = FeatureRuleSet.model_validate(json.loads(row["rules_json"]))
    reason = "enabled"
    enabled = True
    if context["operational_status"] != "active":
        enabled, reason = False, "workspace_disabled"
    elif not bool(context["entitled"]):
        enabled, reason = False, "entitlement_denied"
    elif row["status"] == "killed":
        enabled, reason = False, "flag_killed"
    elif row["status"] != "active":
        enabled, reason = False, "flag_disabled"
    elif int(row["rollout_percentage_bps"]) <= 0:
        enabled, reason = False, "not_released"
    elif rules.prerequisite_flag_code:
        prerequisite = by_code.get(rules.prerequisite_flag_code)
        if prerequisite is None or not _evaluate_capability(
            connection,
            config,
            actor=actor,
            context=context,
            by_code=by_code,
            resolved=resolved,
            resolving=resolving,
            flag_code=rules.prerequisite_flag_code,
        ).enabled:
            enabled, reason = False, "prerequisite_disabled"
    elif "production" not in rules.environments:
        enabled, reason = False, "environment_excluded"
    elif rules.regions and context["region_code"] not in rules.regions:
        enabled, reason = False, "region_excluded"
    elif rules.cohort_code and not _cohort_contains(
        connection, config, rules.cohort_code, actor.workspace_id
    ):
        enabled, reason = False, "cohort_excluded"
    if enabled:
        threshold = min(int(row["rollout_percentage_bps"]), rules.percentage_bps)
        bucket = _stable_bucket(
            config,
            flag_code=flag_code,
            version=int(row["version_number"]),
            workspace_id=actor.workspace_id,
            subject_id=actor.account_id,
        )
        if bucket >= threshold:
            enabled, reason = False, "percentage_excluded"
    capability = _capability(row, enabled, reason)
    resolving.discard(flag_code)
    resolved[flag_code] = capability
    return capability


def _capability(row, enabled: bool, reason: str) -> WorkspaceCapability:
    return WorkspaceCapability(
        flag_code=str(row["flag_code"]),
        enabled=enabled,
        public_version=int(row["version_number"]),
        reason_code=reason,
    )


def _cohort_contains(
    connection, config: AppConfig, cohort_code: str, workspace_id: str
) -> bool:
    pseudonym = _secret_digest(config, "workspace", workspace_id)
    return connection.execute(
        """
        SELECT 1 FROM tenant_cohorts cohort JOIN tenant_cohort_members member
          ON member.tenant_cohort_id=cohort.tenant_cohort_id
        WHERE cohort.cohort_code=? AND cohort.status='active' AND member.workspace_pseudonym=?
        """,
        (cohort_code, pseudonym),
    ).fetchone() is not None


def _stable_bucket(
    config: AppConfig,
    *,
    flag_code: str,
    version: int,
    workspace_id: str,
    subject_id: str,
) -> int:
    canonical = f"{flag_code}\0{version}\0{workspace_id}\0account\0{subject_id}"
    digest = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        b"prd107/bucket/v1\0" + canonical.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10000


def _validate_rule_references(
    connection, flag_code: str, rules: FeatureRuleSet
) -> None:
    if rules.prerequisite_flag_code == flag_code:
        raise ApiError("feature_flag.request_invalid")
    if rules.prerequisite_flag_code and connection.execute(
        "SELECT 1 FROM feature_flags WHERE flag_code=?",
        (rules.prerequisite_flag_code,),
    ).fetchone() is None:
        raise ApiError("feature_flag.request_invalid")
    if rules.cohort_code and connection.execute(
        "SELECT 1 FROM tenant_cohorts WHERE cohort_code=? AND status='active'",
        (rules.cohort_code,),
    ).fetchone() is None:
        raise ApiError("feature_flag.request_invalid")


def _flag_info(connection, flag_id: str) -> FeatureFlagInfo:
    row = connection.execute(
        """
        SELECT flag.*,version.version_number,version.rules_json,
          (SELECT COUNT(*) FROM feature_exposures exposure
           WHERE exposure.feature_flag_id=flag.feature_flag_id) exposure_count
        FROM feature_flags flag JOIN feature_flag_versions version
          ON version.feature_flag_version_id=flag.current_version_id
        WHERE flag.feature_flag_id=?
        """,
        (flag_id,),
    ).fetchone()
    if row is None:
        raise ApiError("feature_flag.not_found")
    rollout = connection.execute(
        "SELECT feature_rollout_id FROM feature_rollouts WHERE feature_flag_id=? "
        "ORDER BY created_at DESC,feature_rollout_id DESC LIMIT 1",
        (flag_id,),
    ).fetchone()
    versions = connection.execute(
        """
        SELECT version.version_number,version.rules_json,version.created_at,
          (SELECT COUNT(*) FROM feature_exposures exposure
           WHERE exposure.feature_flag_version_id=version.feature_flag_version_id)
            exposure_count
        FROM feature_flag_versions version
        WHERE version.feature_flag_id=?
        ORDER BY version.version_number DESC
        """,
        (flag_id,),
    ).fetchall()
    return FeatureFlagInfo(
        flag_id=str(row["feature_flag_id"]),
        flag_code=str(row["flag_code"]),
        display_name=str(row["display_name"]),
        risk_level=str(row["risk_level"]),
        status=str(row["status"]),
        current_version=int(row["version_number"]),
        rollout_percentage_bps=int(row["rollout_percentage_bps"]),
        revision=int(row["revision"]),
        kill_generation=int(row["kill_generation"]),
        kill_reason_code=row["kill_reason_code"],
        exposure_count=int(row["exposure_count"]),
        rules=FeatureRuleSet.model_validate(json.loads(row["rules_json"])),
        versions=tuple(
            FeatureFlagVersionInfo(
                version_number=int(version["version_number"]),
                rules=FeatureRuleSet.model_validate(json.loads(version["rules_json"])),
                exposure_count=int(version["exposure_count"]),
                created_at=_time(version["created_at"]),
            )
            for version in versions
        ),
        latest_rollout=(
            _rollout_info(connection, str(rollout[0])) if rollout is not None else None
        ),
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _rollout_info(connection, rollout_id: str) -> FeatureRolloutInfo:
    row = connection.execute(
        """
        SELECT rollout.*,version.version_number,
          (SELECT COUNT(*) FROM feature_rollout_approvals approval
           WHERE approval.feature_rollout_id=rollout.feature_rollout_id) approval_count
        FROM feature_rollouts rollout JOIN feature_flag_versions version
          ON version.feature_flag_version_id=rollout.feature_flag_version_id
        WHERE rollout.feature_rollout_id=?
        """,
        (rollout_id,),
    ).fetchone()
    if row is None:
        raise ApiError("feature_flag.not_found")
    return FeatureRolloutInfo(
        rollout_id=str(row["feature_rollout_id"]),
        action=str(row["action_kind"]),
        status=str(row["status"]),
        version_number=int(row["version_number"]),
        target_percentage_bps=int(row["target_percentage_bps"]),
        environment_code=str(row["environment_code"]),
        region_code=str(row["region_code"]),
        health_status=str(row["health_status"]),
        health_source_watermark=(
            int(row["health_source_watermark"])
            if row["health_source_watermark"] is not None
            else None
        ),
        approval_count=min(2, int(row["approval_count"])),
        revision=int(row["revision"]),
        job_id=row["platform_job_id"],
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _flag_row(connection, flag_id: str):
    row = connection.execute(
        "SELECT * FROM feature_flags WHERE feature_flag_id=?", (flag_id,)
    ).fetchone()
    if row is None:
        raise ApiError("feature_flag.not_found")
    return row


def _rollout_row(connection, rollout_id: str, flag_id: object):
    if _ROLLOUT_ID_RE.fullmatch(rollout_id) is None:
        raise ApiError("feature_flag.not_found")
    row = connection.execute(
        "SELECT * FROM feature_rollouts WHERE feature_rollout_id=? AND feature_flag_id=?",
        (rollout_id, flag_id),
    ).fetchone()
    if row is None:
        raise ApiError("feature_flag.not_found")
    return row


def _command_replay(
    connection,
    actor: PlatformContext,
    *,
    idempotency_sha256: str,
    request_sha256: str,
):
    row = connection.execute(
        "SELECT * FROM feature_flag_commands WHERE operator_id=? AND idempotency_sha256=?",
        (actor.operator_id, idempotency_sha256),
    ).fetchone()
    if row is not None and not hmac.compare_digest(
        str(row["request_sha256"]), request_sha256
    ):
        raise ApiError("feature_flag.idempotency_conflict")
    return row


def _insert_command(
    connection,
    *,
    actor: PlatformContext,
    action: str,
    flag_id: str,
    rollout_id: str | None,
    idempotency_sha256: str,
    request_sha256: str,
    result_revision: int,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO feature_flag_commands(
          feature_flag_command_id,operator_id,action_code,feature_flag_id,
          feature_rollout_id,idempotency_sha256,request_sha256,result_revision,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("ffc_"),
            actor.operator_id,
            action,
            flag_id,
            rollout_id,
            idempotency_sha256,
            request_sha256,
            result_revision,
            _db_time(now),
        ),
    )


def _event(
    connection,
    *,
    event_type: str,
    flag_id: str,
    rollout_id: str | None,
    workspace_id: str | None,
    reason: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO feature_event_outbox(
          feature_event_outbox_id,event_type,feature_flag_id,feature_rollout_id,
          workspace_id,safe_reason_code,request_id,trace_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("feo_"),
            event_type,
            flag_id,
            rollout_id,
            workspace_id,
            reason,
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


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
            _id("pau_"),
            actor.operator_id,
            action,
            object_type,
            object_id,
            reason,
            request_id,
            trace_id,
            _digest(after),
            _db_time(now),
        ),
    )


def _require(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _valid_flag_id(flag_id: str) -> None:
    if _FLAG_ID_RE.fullmatch(flag_id) is None:
        raise ApiError("feature_flag.not_found")


def _valid_idempotency(value: str) -> None:
    if _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError("feature_flag.request_invalid")


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("feature_flag.unavailable") from None


def _rollback(connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


def _secret_digest(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd107/{purpose}/v1\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


__all__ = [
    "command_feature_rollout",
    "create_feature_flag",
    "get_workspace_capabilities",
    "kill_feature_flag",
    "list_feature_flags",
    "record_feature_exposure_in_transaction",
]
