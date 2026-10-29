"""Database-authoritative PRD-110 case, challenge, appeal, and overlay state."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.service import authenticate_global_session_in_transaction
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    IssuedRiskChallenge,
    RiskAppealInfo,
    RiskAppealRequest,
    RiskAppealResponse,
    RiskCaseDecisionRequest,
    RiskCaseDecisionResponse,
    RiskCaseInfo,
    RiskCaseListResponse,
    RiskChallengeInfo,
    RiskChallengeRequest,
    RiskChallengeResponse,
    RiskDecisionInfo,
    RiskRestrictionInfo,
)

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_CASE_ID_RE = re.compile(r"^rca_[0-9a-f]{32}$")
_SAFE_REASONS = frozenset(
    {
        "unusual_volume",
        "repeated_failures",
        "automation_detected",
        "resource_spike",
        "manual_review",
    }
)


def list_cases(
    config: AppConfig,
    *,
    session_token: str,
    status: str | None,
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> RiskCaseListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _platform_actor(connection, config, session_token)
        projection = _projection(actor)
        binding = {"status": status, "projection": projection}
        cursor_value = _decode_cursor(config, cursor, binding) if cursor else None
        clauses: list[str] = []
        values: list[object] = []
        if status is not None:
            clauses.append("status=?")
            values.append(status)
        if cursor_value is not None:
            clauses.append("(updated_at<? OR(updated_at=? AND risk_case_id<?))")
            values.extend((cursor_value[0], cursor_value[0], cursor_value[1]))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total_where = " WHERE status=?" if status is not None else ""
        total_values: tuple[object, ...] = (status,) if status is not None else ()
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM risk_cases{total_where}", total_values
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"SELECT * FROM risk_cases{where} ORDER BY updated_at DESC,risk_case_id DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit and visible:
            tail = visible[-1]
            next_cursor = _encode_cursor(
                config, str(tail["updated_at"]), str(tail["risk_case_id"]), binding
            )
        items = tuple(_case_info(connection, row, projection) for row in visible)
        connection.commit()
        return RiskCaseListResponse(
            items=items,
            total=total,
            limit=limit,
            next_cursor=next_cursor,
            projection=projection,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("risk.unavailable") from None
    finally:
        connection.close()


def decide_case(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    case_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: RiskCaseDecisionRequest,
    request_id: str,
    trace_id: str,
) -> RiskCaseDecisionResponse:
    if _CASE_ID_RE.fullmatch(case_id) is None or expected_revision < 1:
        raise ApiError("risk.request_invalid")
    _valid_idempotency(idempotency_key)
    request_hash = _digest(payload.model_dump(mode="json"))
    idem_hash = _secret_digest(config, "platform-command", idempotency_key)
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
        _require(actor, "platform.risk.decide")
        replay = connection.execute(
            "SELECT * FROM risk_commands WHERE operator_id=? AND idempotency_sha256=?",
            (actor.operator_id, idem_hash),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_hash):
                raise ApiError("risk.idempotency_conflict")
            decision = _decision_info(connection, str(replay["result_decision_id"]))
            case = _case_info_by_id(connection, str(replay["result_case_id"]), "security")
            connection.commit()
            return RiskCaseDecisionResponse(
                case=case,
                decision=decision,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
        row = connection.execute(
            "SELECT * FROM risk_cases WHERE risk_case_id=?", (case_id,)
        ).fetchone()
        if row is None:
            raise ApiError("risk.case_not_found")
        if int(row["revision"]) != expected_revision:
            raise ApiError("risk.revision_conflict")
        now = _database_now(connection)
        changed = _changed_time(now, row["updated_at"])
        next_status = {
            "required_review": "under_review",
            "restricted": "restricted",
            "cleared": "cleared",
        }[payload.decision]
        _valid_transition(str(row["status"]), next_status)
        safe_reason = {
            "manual_review": "manual_review",
            "automation_confirmed": str(row["safe_reason_family"]),
            "false_positive": "manual_review",
            "appeal_approved": "manual_review",
        }[payload.reason_code]
        decision_id = _id("rdc_")
        expires = (
            now + timedelta(minutes=int(payload.restriction_minutes or 0))
            if payload.decision == "restricted"
            else None
        )
        connection.execute(
            """
            INSERT INTO risk_decisions(
              risk_decision_id,risk_case_id,workspace_id,tenant_id,decision,
              safe_reason_family,policy_version,expires_at,source_code,actor_operator_id,
              request_id,trace_id,decided_at)
            VALUES(?,?,?,?,?,?,1,?,'platform',?,?,?,?)
            """,
            (
                decision_id,
                case_id,
                row["workspace_id"],
                row["tenant_id"],
                payload.decision,
                safe_reason,
                _db_time(expires) if expires else None,
                actor.operator_id,
                request_id,
                trace_id,
                _db_time(now),
            ),
        )
        if payload.decision == "restricted":
            _create_restriction(connection, config, row, payload, now)
        elif payload.decision == "cleared":
            _clear_restrictions_and_appeals(connection, row, now)
        next_revision = int(row["revision"]) + 1
        closed_at = _db_time(now) if next_status == "cleared" else None
        updated = connection.execute(
            """UPDATE risk_cases SET status=?,revision=revision+1,updated_at=?,closed_at=?
               WHERE risk_case_id=? AND revision=?""",
            (next_status, _db_time(changed), closed_at, case_id, expected_revision),
        ).rowcount
        if updated != 1:
            raise ApiError("risk.revision_conflict")
        _case_transition(
            connection,
            case_id=case_id,
            from_status=str(row["status"]),
            to_status=next_status,
            revision=next_revision,
            reason=safe_reason,
            actor_domain="platform",
            actor_id=actor.operator_id,
            now=now,
        )
        connection.execute(
            """INSERT INTO risk_commands(
                 risk_command_id,operator_id,action_code,idempotency_sha256,request_sha256,
                 result_case_id,result_decision_id,result_revision,created_at)
               VALUES(?,?,'risk.case.decision',?,?,?,?,?,?)""",
            (
                _id("rcm_"),
                actor.operator_id,
                idem_hash,
                request_hash,
                case_id,
                decision_id,
                next_revision,
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor=actor,
            action="risk.case.decided",
            object_id=case_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={"decision": payload.decision, "revision": next_revision},
            now=now,
        )
        decision = _decision_info(connection, decision_id)
        case = _case_info_by_id(connection, case_id, "security")
        connection.commit()
        return RiskCaseDecisionResponse(
            case=case,
            decision=decision,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("risk.unavailable") from None
    finally:
        connection.close()


def complete_challenge(
    config: AppConfig,
    *,
    global_session_token: str | None,
    workspace_session_token: str | None,
    csrf_token: str,
    idempotency_key: str,
    payload: RiskChallengeRequest,
    request_id: str,
    trace_id: str,
) -> RiskChallengeResponse:
    _valid_idempotency(idempotency_key)
    if (global_session_token is None) == (workspace_session_token is None):
        raise ApiError("risk.credential_conflict")
    request_hash = _digest(
        {
            "challenge_id": payload.challenge_id,
            "token_hmac": _secret_digest(config, "challenge-token", payload.challenge_token),
            "answer_hmac": _secret_digest(config, "challenge-answer", payload.answer),
            "revision": payload.revision,
            "confirmed": True,
        }
    )
    idem_hash = _secret_digest(config, "challenge-command", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        account_id, workspace_context = _risk_actor(
            connection,
            config,
            global_session_token=global_session_token,
            workspace_session_token=workspace_session_token,
            csrf_token=csrf_token,
        )
        replay = connection.execute(
            "SELECT * FROM risk_challenge_commands WHERE account_id=? AND idempotency_sha256=?",
            (account_id, idem_hash),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_hash):
                raise ApiError("risk.idempotency_conflict")
            challenge = _challenge_info_by_id(connection, str(replay["risk_challenge_id"]))
            connection.commit()
            return RiskChallengeResponse(
                challenge=challenge,
                completed=True,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
        row = connection.execute(
            "SELECT * FROM risk_challenges WHERE risk_challenge_id=?", (payload.challenge_id,)
        ).fetchone()
        if row is None or str(row["account_id"]) != account_id:
            raise ApiError("risk.challenge_not_found")
        if workspace_context is not None and (
            str(row["workspace_id"]) != workspace_context.workspace_id
            or str(row["tenant_id"]) != workspace_context.tenant_id
        ):
            raise ApiError("risk.challenge_not_found")
        if row["status"] != "pending":
            raise ApiError(
                "risk.challenge_replayed"
                if row["status"] == "consumed"
                else "risk.challenge_conflict"
            )
        if int(row["revision"]) != payload.revision:
            raise ApiError("risk.revision_conflict")
        now = _database_now(connection)
        token_ok = hmac.compare_digest(
            str(row["token_hmac"]),
            _secret_digest(config, "challenge-token", payload.challenge_token),
        )
        answer_ok = hmac.compare_digest(
            str(row["answer_hmac"]), _secret_digest(config, "challenge-answer", payload.answer)
        )
        attempt = int(row["attempts"]) + 1
        if _time(row["expires_at"]) <= now:
            _finish_challenge_attempt(
                connection, row, attempt, "expired", "expired", now, request_id, trace_id
            )
            connection.commit()
            raise ApiError("risk.challenge_expired")
        if not token_ok or not answer_ok:
            status = "failed" if attempt >= int(row["max_attempts"]) else "pending"
            _finish_challenge_attempt(
                connection, row, attempt, status, "rejected", now, request_id, trace_id
            )
            connection.commit()
            raise ApiError("risk.challenge_invalid")
        _finish_challenge_attempt(
            connection, row, attempt, "consumed", "accepted", now, request_id, trace_id
        )
        case = connection.execute(
            "SELECT * FROM risk_cases WHERE risk_case_id=?", (row["risk_case_id"],)
        ).fetchone()
        if case is None:
            raise ApiError("risk.unavailable")
        active_restriction = connection.execute(
            "SELECT 1 FROM risk_restrictions WHERE risk_case_id=? AND status='active'",
            (row["risk_case_id"],),
        ).fetchone()
        if case["status"] in {"open", "under_review"} and active_restriction is None:
            changed = _changed_time(now, case["updated_at"])
            next_revision = int(case["revision"]) + 1
            connection.execute(
                """UPDATE risk_cases
                   SET status='cleared',revision=revision+1,updated_at=?,closed_at=?
                   WHERE risk_case_id=? AND revision=?""",
                (_db_time(changed), _db_time(now), case["risk_case_id"], case["revision"]),
            )
            decision_id = _id("rdc_")
            connection.execute(
                """INSERT INTO risk_decisions(
                     risk_decision_id,risk_case_id,workspace_id,tenant_id,decision,
                     safe_reason_family,policy_version,expires_at,source_code,actor_operator_id,
                     request_id,trace_id,decided_at)
                   VALUES(?,?,?,?, 'allow','manual_review',1,NULL,'automatic',NULL,?,?,?)""",
                (
                    decision_id,
                    case["risk_case_id"],
                    case["workspace_id"],
                    case["tenant_id"],
                    request_id,
                    trace_id,
                    _db_time(now),
                ),
            )
            _case_transition(
                connection,
                case_id=str(case["risk_case_id"]),
                from_status=str(case["status"]),
                to_status="cleared",
                revision=next_revision,
                reason="challenge_completed",
                actor_domain="automatic",
                actor_id=account_id,
                now=now,
            )
        connection.execute(
            """INSERT INTO risk_challenge_commands(
                 risk_challenge_command_id,risk_challenge_id,account_id,idempotency_sha256,
                 request_sha256,result_status,result_revision,created_at)
               VALUES(?,?,?,?,?,'consumed',?,?)""",
            (
                _id("rcc_"),
                payload.challenge_id,
                account_id,
                idem_hash,
                request_hash,
                int(row["revision"]) + 1,
                _db_time(now),
            ),
        )
        challenge = _challenge_info_by_id(connection, payload.challenge_id)
        connection.commit()
        return RiskChallengeResponse(
            challenge=challenge,
            completed=True,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("risk.unavailable") from None
    finally:
        connection.close()


def create_appeal(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    payload: RiskAppealRequest,
    request_id: str,
    trace_id: str,
) -> RiskAppealResponse:
    _valid_idempotency(idempotency_key)
    request_hash = _digest(payload.model_dump(mode="json"))
    idem_hash = _secret_digest(config, "appeal-command", idempotency_key)
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
        replay = connection.execute(
            "SELECT * FROM risk_appeals WHERE account_id=? AND idempotency_sha256=?",
            (actor.account_id, idem_hash),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_hash):
                raise ApiError("risk.idempotency_conflict")
            result = _appeal_info(replay)
            connection.commit()
            return RiskAppealResponse(
                appeal=result,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
        restriction = connection.execute(
            "SELECT * FROM risk_restrictions WHERE risk_restriction_id=?",
            (payload.restriction_id,),
        ).fetchone()
        if (
            restriction is None
            or restriction["workspace_id"] != actor.workspace_id
            or restriction["tenant_id"] != actor.tenant_id
            or restriction["subject_kind"] != "account"
            or restriction["subject_account_id"] != actor.account_id
        ):
            raise ApiError("risk.restriction_not_found")
        if restriction["status"] != "active":
            raise ApiError("risk.appeal_conflict")
        pending = connection.execute(
            """SELECT 1 FROM risk_appeals
               WHERE risk_restriction_id=? AND account_id=? AND status='pending'""",
            (payload.restriction_id, actor.account_id),
        ).fetchone()
        if pending is not None:
            raise ApiError("risk.appeal_conflict")
        now = _database_now(connection)
        appeal_id = _id("rap_")
        connection.execute(
            """INSERT INTO risk_appeals(
                 risk_appeal_id,risk_case_id,risk_restriction_id,workspace_id,tenant_id,
                 account_id,category,detail_ciphertext,detail_sha256,status,resolution_reason,
                 idempotency_sha256,request_sha256,revision,created_at,resolved_at,updated_at)
               VALUES(?,?,?,?,?,?,?,NULL,NULL,'pending',NULL,?,?,1,?,NULL,?)""",
            (
                appeal_id,
                restriction["risk_case_id"],
                restriction["risk_restriction_id"],
                actor.workspace_id,
                actor.tenant_id,
                actor.account_id,
                payload.category,
                idem_hash,
                request_hash,
                _db_time(now),
                _db_time(now),
            ),
        )
        row = connection.execute(
            "SELECT * FROM risk_appeals WHERE risk_appeal_id=?", (appeal_id,)
        ).fetchone()
        result = _appeal_info(row)
        connection.commit()
        return RiskAppealResponse(
            appeal=result,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("risk.unavailable") from None
    finally:
        connection.close()


def create_case_challenge(
    config: AppConfig,
    *,
    workspace_id: str,
    tenant_id: str,
    account_id: str,
    case_kind: str,
    signal_type: str,
    safe_reason_family: str,
    score_band: str,
    subject_value: str,
    ip_prefix: str | None,
    device_value: str | None,
    request_id: str,
    trace_id: str,
) -> IssuedRiskChallenge:
    if safe_reason_family not in _SAFE_REASONS:
        raise ApiError("risk.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        owner = connection.execute(
            """SELECT 1 FROM workspace_memberships membership
               JOIN workspace_tenant_bindings binding
                 ON binding.workspace_id=membership.workspace_id
                 AND binding.tenant_id=membership.tenant_id AND binding.status='active'
               WHERE membership.workspace_id=? AND membership.tenant_id=?
                 AND membership.account_id=? AND membership.status='active'""",
            (workspace_id, tenant_id, account_id),
        ).fetchone()
        if owner is None:
            raise ApiError("risk.owner_not_found")
        now = _database_now(connection)
        case_id = _id("rca_")
        connection.execute(
            """INSERT INTO risk_cases(
                 risk_case_id,workspace_id,tenant_id,case_kind,safe_reason_family,
                 score_band,status,source_watermark,revision,opened_at,updated_at,closed_at)
               VALUES(?,?,?,?,?,?,'open',0,1,?,?,NULL)""",
            (
                case_id,
                workspace_id,
                tenant_id,
                case_kind,
                safe_reason_family,
                score_band,
                _db_time(now),
                _db_time(now),
            ),
        )
        _case_transition(
            connection,
            case_id=case_id,
            from_status=None,
            to_status="open",
            revision=1,
            reason=safe_reason_family,
            actor_domain="automatic",
            actor_id="risk-engine",
            now=now,
        )
        subject_hmac = _secret_digest(config, "subject", subject_value)
        evidence = {
            "case_kind": case_kind,
            "score_band": score_band,
            "signal_type": signal_type,
            "subject_hmac": subject_hmac,
        }
        connection.execute(
            """INSERT INTO risk_signals(
                 risk_signal_id,risk_case_id,workspace_id,tenant_id,signal_type,severity,
                 time_bucket,subject_hmac,ip_prefix_hmac,device_pseudonym,evidence_sha256,
                 policy_version,created_at)
               VALUES(?,?,?,?,?, ?,?,?,?,?,?,1,?)""",
            (
                _id("rsg_"),
                case_id,
                workspace_id,
                tenant_id,
                signal_type,
                "critical" if score_band == "critical" else "warning",
                _db_time(now.replace(second=0, microsecond=0)),
                subject_hmac,
                _secret_digest(config, "ip-prefix", ip_prefix) if ip_prefix else None,
                _secret_digest(config, "device", device_value) if device_value else None,
                _digest(evidence),
                _db_time(now),
            ),
        )
        decision_id = _id("rdc_")
        connection.execute(
            """INSERT INTO risk_decisions(
                 risk_decision_id,risk_case_id,workspace_id,tenant_id,decision,
                 safe_reason_family,policy_version,expires_at,source_code,actor_operator_id,
                 request_id,trace_id,decided_at)
               VALUES(?,?,?,?, 'challenge',?,1,NULL,'automatic',NULL,?,?,?)""",
            (
                decision_id,
                case_id,
                workspace_id,
                tenant_id,
                safe_reason_family,
                request_id,
                trace_id,
                _db_time(now),
            ),
        )
        token = secrets.token_urlsafe(32)
        answer = f"{secrets.randbelow(1_000_000):06d}"
        challenge_id = _id("rch_")
        expires = now + timedelta(minutes=10)
        connection.execute(
            """INSERT INTO risk_challenges(
                 risk_challenge_id,risk_case_id,workspace_id,tenant_id,account_id,
                 challenge_kind,token_hmac,answer_hmac,intent_sha256,status,attempts,
                 max_attempts,expires_at,consumed_at,revision,created_at,updated_at)
               VALUES(?,?,?,?,?,'proof_of_control',?,?,?,'pending',0,5,?,NULL,1,?,?)""",
            (
                challenge_id,
                case_id,
                workspace_id,
                tenant_id,
                account_id,
                _secret_digest(config, "challenge-token", token),
                _secret_digest(config, "challenge-answer", answer),
                _digest({"case_id": case_id, "account_id": account_id, "kind": "proof_of_control"}),
                _db_time(expires),
                _db_time(now),
                _db_time(now),
            ),
        )
        _event(
            connection,
            "risk.challenge_required",
            case_id,
            workspace_id,
            tenant_id,
            challenge_id,
            safe_reason_family,
            now,
        )
        challenge = _challenge_info_by_id(connection, challenge_id)
        connection.commit()
        return IssuedRiskChallenge(challenge=challenge, challenge_token=token, answer=answer)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("risk.unavailable") from None
    finally:
        connection.close()


def expire_restriction_in_transaction(
    connection,
    *,
    restriction_id: str,
    expected_revision: int,
) -> bool:
    row = connection.execute(
        "SELECT * FROM risk_restrictions WHERE risk_restriction_id=?", (restriction_id,)
    ).fetchone()
    if row is None:
        raise ApiError("risk.restriction_not_found")
    if row["status"] != "active":
        return False
    if int(row["revision"]) != expected_revision:
        raise ApiError("risk.revision_conflict")
    now = _database_now(connection)
    if _time(row["expires_at"]) > now:
        raise ApiError("risk.restriction_not_due")
    changed = _changed_time(now, row["updated_at"])
    connection.execute(
        """UPDATE risk_restrictions SET status='expired',cleared_at=?,revision=revision+1,
             updated_at=? WHERE risk_restriction_id=? AND status='active' AND revision=?""",
        (_db_time(now), _db_time(changed), restriction_id, expected_revision),
    )
    case = connection.execute(
        "SELECT * FROM risk_cases WHERE risk_case_id=?", (row["risk_case_id"],)
    ).fetchone()
    if case is not None and case["status"] == "restricted":
        case_changed = _changed_time(now, case["updated_at"])
        revision = int(case["revision"]) + 1
        connection.execute(
            "UPDATE risk_cases SET status='closed',revision=revision+1,updated_at=?,closed_at=? "
            "WHERE risk_case_id=? AND revision=?",
            (_db_time(case_changed), _db_time(now), case["risk_case_id"], case["revision"]),
        )
        _case_transition(
            connection,
            case_id=str(case["risk_case_id"]),
            from_status="restricted",
            to_status="closed",
            revision=revision,
            reason="restriction_expired",
            actor_domain="appeal_job",
            actor_id="risk.restriction.expire",
            now=now,
        )
        connection.execute(
            """INSERT INTO risk_decisions(
                 risk_decision_id,risk_case_id,workspace_id,tenant_id,decision,
                 safe_reason_family,policy_version,expires_at,source_code,actor_operator_id,
                 request_id,trace_id,decided_at)
               VALUES(?,?,?,?, 'cleared','restriction_expired',1,NULL,'appeal_job',NULL,
                 'risk-expiry-job','risk-expiry-job',?)""",
            (
                _id("rdc_"),
                case["risk_case_id"],
                case["workspace_id"],
                case["tenant_id"],
                _db_time(now),
            ),
        )
    appeals = connection.execute(
        "SELECT * FROM risk_appeals WHERE risk_restriction_id=? AND status='pending'",
        (restriction_id,),
    ).fetchall()
    for appeal in appeals:
        appeal_changed = _changed_time(now, appeal["updated_at"])
        connection.execute(
            """UPDATE risk_appeals
               SET status='rejected',resolution_reason='insufficient_evidence',
                   revision=revision+1,resolved_at=?,updated_at=?
               WHERE risk_appeal_id=? AND status='pending'""",
            (_db_time(now), _db_time(appeal_changed), appeal["risk_appeal_id"]),
        )
        _event(
            connection,
            "risk.appeal_resolved",
            str(row["risk_case_id"]),
            str(row["workspace_id"]),
            str(row["tenant_id"]),
            str(appeal["risk_appeal_id"]),
            "restriction_expired",
            now,
        )
    return True


def _create_restriction(connection, config, case, payload, now: datetime) -> str:
    challenge = connection.execute(
        """SELECT account_id FROM risk_challenges
           WHERE risk_case_id=? ORDER BY created_at DESC LIMIT 1""",
        (case["risk_case_id"],),
    ).fetchone()
    account_id = str(challenge["account_id"]) if challenge is not None else None
    subject_kind = "account" if account_id else "workspace"
    subject_value = account_id or str(case["workspace_id"])
    restriction_id = _id("rst_")
    expires = now + timedelta(minutes=int(payload.restriction_minutes))
    connection.execute(
        """INSERT INTO risk_restrictions(
             risk_restriction_id,risk_case_id,workspace_id,tenant_id,subject_kind,
             subject_account_id,subject_hmac,restriction_kind,status,starts_at,expires_at,
             cleared_at,revision,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,'active',?,?,NULL,1,?,?)""",
        (
            restriction_id,
            case["risk_case_id"],
            case["workspace_id"],
            case["tenant_id"],
            subject_kind,
            account_id,
            _secret_digest(config, f"restriction-{subject_kind}", subject_value),
            payload.restriction_kind,
            _db_time(now),
            _db_time(expires),
            _db_time(now),
            _db_time(now),
        ),
    )
    _event(
        connection,
        "risk.workspace_restricted",
        str(case["risk_case_id"]),
        str(case["workspace_id"]),
        str(case["tenant_id"]),
        restriction_id,
        str(case["safe_reason_family"]),
        now,
    )
    return restriction_id


def _clear_restrictions_and_appeals(connection, case, now: datetime) -> None:
    restrictions = connection.execute(
        "SELECT * FROM risk_restrictions WHERE risk_case_id=? AND status='active'",
        (case["risk_case_id"],),
    ).fetchall()
    for restriction in restrictions:
        changed = _changed_time(now, restriction["updated_at"])
        connection.execute(
            """UPDATE risk_restrictions SET status='cleared',cleared_at=?,revision=revision+1,
                 updated_at=? WHERE risk_restriction_id=? AND status='active'""",
            (_db_time(now), _db_time(changed), restriction["risk_restriction_id"]),
        )
    appeals = connection.execute(
        "SELECT * FROM risk_appeals WHERE risk_case_id=? AND status='pending'",
        (case["risk_case_id"],),
    ).fetchall()
    for appeal in appeals:
        changed = _changed_time(now, appeal["updated_at"])
        connection.execute(
            """UPDATE risk_appeals
               SET status='approved',resolution_reason='verified_false_positive',
                   revision=revision+1,resolved_at=?,updated_at=?
               WHERE risk_appeal_id=? AND status='pending'""",
            (_db_time(now), _db_time(changed), appeal["risk_appeal_id"]),
        )
        _event(
            connection,
            "risk.appeal_resolved",
            str(case["risk_case_id"]),
            str(case["workspace_id"]),
            str(case["tenant_id"]),
            str(appeal["risk_appeal_id"]),
            "verified_false_positive",
            now,
        )


def _finish_challenge_attempt(
    connection,
    row,
    attempt: int,
    status: str,
    outcome: str,
    now: datetime,
    request_id: str,
    trace_id: str,
) -> None:
    changed = _changed_time(now, row["updated_at"])
    consumed_at = _db_time(now) if status == "consumed" else None
    connection.execute(
        """UPDATE risk_challenges SET status=?,attempts=?,consumed_at=?,revision=revision+1,
             updated_at=? WHERE risk_challenge_id=? AND status='pending' AND revision=?""",
        (
            status,
            attempt,
            consumed_at,
            _db_time(changed),
            row["risk_challenge_id"],
            row["revision"],
        ),
    )
    connection.execute(
        """INSERT INTO risk_challenge_attempts(
             risk_challenge_attempt_id,risk_challenge_id,attempt_number,outcome,
             request_id,trace_id,occurred_at) VALUES(?,?,?,?,?,?,?)""",
        (
            _id("rat_"),
            row["risk_challenge_id"],
            attempt,
            outcome,
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _risk_actor(
    connection,
    config: AppConfig,
    *,
    global_session_token: str | None,
    workspace_session_token: str | None,
    csrf_token: str,
) -> tuple[str, WorkspaceSessionContext | None]:
    if workspace_session_token is not None:
        context = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=workspace_session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        return context.account_id, context
    identity, _now = authenticate_global_session_in_transaction(
        connection,
        config,
        session_token=str(global_session_token),
        csrf_token=csrf_token,
        touch=True,
    )
    return identity.account_id, None


def _case_info_by_id(connection, case_id: str, projection: str) -> RiskCaseInfo:
    row = connection.execute("SELECT * FROM risk_cases WHERE risk_case_id=?", (case_id,)).fetchone()
    if row is None:
        raise ApiError("risk.case_not_found")
    return _case_info(connection, row, projection)


def _case_info(connection, row, projection: str) -> RiskCaseInfo:
    restriction = connection.execute(
        """SELECT * FROM risk_restrictions WHERE risk_case_id=?
           ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,created_at DESC LIMIT 1""",
        (row["risk_case_id"],),
    ).fetchone()
    appeals = connection.execute(
        "SELECT * FROM risk_appeals WHERE risk_case_id=? ORDER BY created_at,risk_appeal_id",
        (row["risk_case_id"],),
    ).fetchall()
    signals = ()
    score_band = None
    if projection == "security":
        score_band = str(row["score_band"])
        signals = tuple(
            str(item[0])
            for item in connection.execute(
                """SELECT DISTINCT signal_type FROM risk_signals
                   WHERE risk_case_id=? ORDER BY signal_type""",
                (row["risk_case_id"],),
            ).fetchall()
        )
    return RiskCaseInfo(
        risk_case_id=str(row["risk_case_id"]),
        workspace_id=str(row["workspace_id"]),
        case_kind=str(row["case_kind"]),
        safe_reason_family=str(row["safe_reason_family"]),
        score_band=score_band,
        status=str(row["status"]),
        signal_families=signals,
        restriction=_restriction_info(restriction) if restriction is not None else None,
        appeals=tuple(_appeal_info(item) for item in appeals),
        revision=int(row["revision"]),
        opened_at=_time(row["opened_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _restriction_info(row) -> RiskRestrictionInfo:
    return RiskRestrictionInfo(
        risk_restriction_id=str(row["risk_restriction_id"]),
        restriction_kind=str(row["restriction_kind"]),
        status=str(row["status"]),
        starts_at=_time(row["starts_at"]),
        expires_at=_time(row["expires_at"]),
        cleared_at=_time(row["cleared_at"]) if row["cleared_at"] else None,
        revision=int(row["revision"]),
    )


def _appeal_info(row) -> RiskAppealInfo:
    return RiskAppealInfo(
        risk_appeal_id=str(row["risk_appeal_id"]),
        category=str(row["category"]),
        status=str(row["status"]),
        resolution_reason=str(row["resolution_reason"]) if row["resolution_reason"] else None,
        revision=int(row["revision"]),
        created_at=_time(row["created_at"]),
        resolved_at=_time(row["resolved_at"]) if row["resolved_at"] else None,
    )


def _challenge_info_by_id(connection, challenge_id: str) -> RiskChallengeInfo:
    row = connection.execute(
        "SELECT * FROM risk_challenges WHERE risk_challenge_id=?", (challenge_id,)
    ).fetchone()
    if row is None:
        raise ApiError("risk.challenge_not_found")
    return RiskChallengeInfo(
        risk_challenge_id=str(row["risk_challenge_id"]),
        challenge_kind=str(row["challenge_kind"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        expires_at=_time(row["expires_at"]),
        revision=int(row["revision"]),
    )


def _decision_info(connection, decision_id: str) -> RiskDecisionInfo:
    row = connection.execute(
        "SELECT * FROM risk_decisions WHERE risk_decision_id=?", (decision_id,)
    ).fetchone()
    if row is None:
        raise ApiError("risk.unavailable")
    return RiskDecisionInfo(
        risk_decision_id=str(row["risk_decision_id"]),
        risk_case_id=str(row["risk_case_id"]),
        decision=str(row["decision"]),
        safe_reason_family=str(row["safe_reason_family"]),
        policy_version=int(row["policy_version"]),
        expires_at=_time(row["expires_at"]) if row["expires_at"] else None,
        decided_at=_time(row["decided_at"]),
    )


def _case_transition(
    connection,
    *,
    case_id: str,
    from_status: str | None,
    to_status: str,
    revision: int,
    reason: str,
    actor_domain: str,
    actor_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO risk_case_transitions(
             risk_case_transition_id,risk_case_id,from_status,to_status,case_revision,
             safe_reason_family,actor_domain,actor_id,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            _id("rct_"),
            case_id,
            from_status,
            to_status,
            revision,
            reason,
            actor_domain,
            actor_id,
            _db_time(now),
        ),
    )


def _event(
    connection,
    event_type: str,
    case_id: str,
    workspace_id: str,
    tenant_id: str,
    object_id: str,
    reason: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO risk_event_outbox(
             risk_event_outbox_id,event_type,risk_case_id,workspace_id,tenant_id,
             object_id,safe_reason_family,occurred_at) VALUES(?,?,?,?,?,?,?,?)""",
        (
            _id("reo_"),
            event_type,
            case_id,
            workspace_id,
            tenant_id,
            object_id,
            reason,
            _db_time(now),
        ),
    )


def _audit(
    connection,
    *,
    actor: PlatformContext,
    action: str,
    object_id: str,
    reason: str,
    request_id: str,
    trace_id: str,
    after: object,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO platform_audit_logs(
             audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
             request_id,trace_id,before_sha256,after_sha256,occurred_at)
           VALUES(?,'platform',?,?,'risk_case',?,?,?,?,NULL,?,?)""",
        (
            _id("pau_"),
            actor.operator_id,
            action,
            object_id,
            reason,
            request_id,
            trace_id,
            _digest(after),
            _db_time(now),
        ),
    )


def _platform_actor(connection, config: AppConfig, session_token: str) -> PlatformContext:
    return authenticate_platform_session_in_transaction(
        connection, config, session_token=session_token, touch=True
    )


def _projection(actor: PlatformContext) -> str:
    if "platform.risk.read" in actor.permissions:
        return "security"
    if "platform.risk.support_read" in actor.permissions:
        return "support"
    raise ApiError("platform.permission_denied")


def _require(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _valid_transition(current: str, target: str) -> None:
    allowed = {
        "open": {"under_review", "restricted", "cleared"},
        "under_review": {"restricted", "cleared"},
        "restricted": {"cleared"},
    }
    if target not in allowed.get(current, set()):
        raise ApiError("risk.case_conflict")


def _valid_idempotency(value: str) -> None:
    if _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError("risk.request_invalid")


def _encode_cursor(config: AppConfig, stamp: str, case_id: str, binding: object) -> str:
    body = _canonical({"binding": binding, "case_id": case_id, "updated_at": stamp})
    wire = f"{body}.{_secret_digest(config, 'case-cursor', body)}".encode("ascii")
    return base64.urlsafe_b64encode(wire).decode("ascii").rstrip("=")


def _decode_cursor(config: AppConfig, cursor: str, binding: object) -> tuple[str, str]:
    try:
        if not 20 <= len(cursor) <= 2048:
            raise ValueError
        decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
        if not hmac.compare_digest(canonical, cursor):
            raise ValueError
        wire = decoded.decode("ascii")
        body, signature = wire.rsplit(".", 1)
        if not hmac.compare_digest(signature, _secret_digest(config, "case-cursor", body)):
            raise ValueError
        value = json.loads(body)
        if value["binding"] != binding or _CASE_ID_RE.fullmatch(str(value["case_id"])) is None:
            raise ValueError
        stamp = str(value["updated_at"])
        _time(stamp)
        return stamp, str(value["case_id"])
    except (KeyError, UnicodeError, ValueError, json.JSONDecodeError):
        raise ApiError("risk.cursor_invalid") from None


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("risk.unavailable") from None


def _rollback(connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


def _secret_digest(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd110/{purpose}/v1\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _database_now(connection) -> datetime:
    value = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    return _time(value)


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
    "complete_challenge",
    "create_appeal",
    "create_case_challenge",
    "decide_case",
    "expire_restriction_in_transaction",
    "list_cases",
]
