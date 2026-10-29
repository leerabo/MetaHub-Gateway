"""PRD-148 final GA promotion and rollback over the immutable RC facts."""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import consume_global_step_up_grant_in_transaction

from .ga_models import (
    GaDecisionSummary,
    GaEvidenceCommand,
    GaEvidenceResponse,
    GaEvidenceSummary,
    GaGateListResponse,
    GaGateSummary,
    GaLaunchCommand,
    GaLaunchResponse,
    GaLaunchSummary,
    GaReviewCommand,
    GaReviewResponse,
    GaRollbackCommand,
    GaRollbackResponse,
    GaRollbackSummary,
    PostGaReviewSummary,
)
from .service import (
    _begin_write,
    _candidate_row,
    _connect,
    _db_time,
    _digest,
    _id,
    _next_stamp,
    _platform_actor,
    _require_release_authority,
    _require_schema,
    _utcnow,
    _validate_current_approvals,
)

_GA_GATE_ID = re.compile(r"^gag_[0-9a-f]{32}$")
_GA_LAUNCH_ID = re.compile(r"^gal_[0-9a-f]{32}$")
_GA_ROLLBACK_ID = re.compile(r"^gar_[0-9a-f]{32}$")
_ACTORS = ("product", "sre", "security", "test", "finance", "legal")


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


def list_ga_gates(config, *, platform_session_token: str, request_id: str, trace_id: str):
    from .service import list_launch_gates

    try:
        base = list_launch_gates(
            config,
            platform_session_token=platform_session_token,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection = _connect(config)
        try:
            connection.execute("BEGIN")
            # GA facts belong to the candidate selected by the RC gate response.  A
            # global "latest launch" lookup could leak another candidate's release
            # into this page when candidates are created close together.
            candidate_id = getattr(base.candidate, "launch_candidate_id", None)
            launch = (
                connection.execute(
                    "SELECT ga_launch_id FROM ga_launches "
                    "WHERE launch_candidate_id=? ORDER BY updated_at DESC LIMIT 1",
                    (candidate_id,),
                ).fetchone()
                if candidate_id
                else None
            )
            evidence = tuple(
                _ga_evidence_summary(connection, row["ga_evidence_id"])
                for row in connection.execute(
                    "SELECT ga_evidence_id FROM ga_evidence "
                    "WHERE launch_candidate_id=? ORDER BY created_at, ga_evidence_id",
                    (candidate_id,),
                ).fetchall()
            ) if candidate_id else ()
            result = GaGateListResponse(
                gates=tuple(
                    GaGateSummary.model_validate(
                        {
                            "ga_gate_id": _ga_id(gate.launch_gate_id),
                            "gate_code": gate.gate_code,
                            "policy_version": gate.policy_version,
                            "verifier_code": gate.verifier_code,
                            "verifier_version": gate.verifier_version,
                            "freshness_seconds": gate.freshness_seconds,
                            "blocking_severity": gate.blocking_severity,
                            "policy_sha256": gate.policy_sha256,
                        }
                    )
                    for gate in base.gates
                ),
                candidate=base.candidate,
                evidence=evidence,
                launch=_ga_launch_summary(connection, launch["ga_launch_id"]) if launch else None,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        finally:
            connection.close()
    except ApiError:
        raise
    except (sqlite3.Error, ValueError, TypeError):
        raise ApiError("ga.unavailable") from None


def submit_ga_evidence(
    config,
    ga_gate_id: str,
    command: GaEvidenceCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> GaEvidenceResponse:
    if _GA_GATE_ID.fullmatch(ga_gate_id) is None:
        raise ApiError("launch.gate_not_found")
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    request_sha = _digest({"ga_gate_id": ga_gate_id, "command": command.model_dump(mode="json")})
    connection = _connect(config)
    try:
        _begin_write(connection)
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.launch.evidence.submit",
        )
        _require_schema(connection)
        replay = _ga_action_replay(
            connection, actor.operator_id, "ga.evidence", key_sha, request_sha
        )
        if replay:
            response = GaEvidenceResponse(
                evidence=_ga_evidence_summary(connection, replay),
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        gate = connection.execute(
            "SELECT * FROM ga_gate_policies WHERE ga_gate_id=?", (ga_gate_id,)
        ).fetchone()
        if gate is None:
            raise ApiError("launch.gate_not_found")
        candidate = _candidate_row(connection, command.launch_candidate_id)
        if candidate["revision"] != command.expected_revision:
            raise ApiError("launch.revision_conflict")
        if candidate["status"] != "release_candidate":
            raise ApiError("ga.candidate_not_ready")
        if command.outcome != "passed":
            raise ApiError("ga.gate_blocked")
        if command.verifier_code != gate["verifier_code"]:
            raise ApiError("launch.verifier_invalid")
        source = connection.execute(
            """SELECT evidence.* FROM launch_evidence evidence
               JOIN launch_gate_policies gate ON gate.launch_gate_id=evidence.launch_gate_id
              WHERE evidence.launch_candidate_id=? AND gate.gate_code=?
                AND evidence.workspace_id=? AND evidence.artifact_sha256=?
                AND evidence.verifier_code=? AND evidence.outcome='passed'
              ORDER BY evidence.created_at DESC LIMIT 1""",
            (
                command.launch_candidate_id,
                gate["gate_code"],
                command.workspace_id,
                command.artifact_sha256,
                command.verifier_code,
            ),
        ).fetchone()
        if source is None:
            raise ApiError("ga.gate_blocked")
        _require_matching_source(command, source)
        now = _utcnow()
        if datetime.fromisoformat(source["expires_at"].replace("Z", "+00:00")) <= now:
            raise ApiError("launch.evidence_stale")
        existing = connection.execute(
            "SELECT ga_evidence_id FROM ga_evidence WHERE launch_candidate_id=? AND ga_gate_id=?",
            (command.launch_candidate_id, ga_gate_id),
        ).fetchone()
        if existing:
            evidence_id = existing["ga_evidence_id"]
        else:
            evidence_id = _id("gae_")
            connection.execute(
                """INSERT INTO ga_evidence(
                  ga_evidence_id,launch_candidate_id,ga_gate_id,source_evidence_id,
                  outcome,finished_at,expires_at,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    evidence_id,
                    command.launch_candidate_id,
                    ga_gate_id,
                    source["launch_evidence_id"],
                    "passed",
                    source["finished_at"],
                    source["expires_at"],
                    _db_time(now),
                ),
            )
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code="platform.launch.evidence.submit",
            resource_type="ga_gate",
            resource_id=ga_gate_id,
            now=now,
        )
        _record_action(connection, actor, "ga.evidence", key_sha, request_sha, evidence_id, 1)
        response = GaEvidenceResponse(
            evidence=_ga_evidence_summary(connection, evidence_id),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        _rollback(connection)
        raise ApiError("ga.unavailable") from None
    finally:
        connection.close()


def promote_ga(
    config,
    command: GaLaunchCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> GaLaunchResponse:
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    request_sha = _digest(command.model_dump(mode="json"))
    connection = _connect(config)
    try:
        _begin_write(connection)
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.launch.review",
        )
        _require_schema(connection)
        replay = _ga_action_replay(connection, actor.operator_id, "ga.launch", key_sha, request_sha)
        if replay:
            return _ga_launch_response(connection, replay, request_id, trace_id, replay=True)
        candidate = _candidate_row(connection, command.launch_candidate_id)
        if (
            candidate["revision"] != command.expected_revision
            or candidate["status"] != "release_candidate"
        ):
            raise ApiError("ga.candidate_not_ready")
        now = _utcnow()
        _require_release_authority(connection, candidate, actor)
        _validate_current_approvals(connection, candidate, now)
        evidence = connection.execute(
            """SELECT ge.*, gp.gate_code, le.started_at
                 FROM ga_evidence ge
                 JOIN ga_gate_policies gp ON gp.ga_gate_id=ge.ga_gate_id
                 JOIN launch_evidence le ON le.launch_evidence_id=ge.source_evidence_id
                WHERE ge.launch_candidate_id=? AND ge.outcome='passed'
                  AND julianday(ge.expires_at)>julianday(?)""",
            (command.launch_candidate_id, _db_time(now)),
        ).fetchall()
        if len(evidence) != 20 or len({row["gate_code"] for row in evidence}) != 20:
            raise ApiError("ga.gate_blocked")
        stability = next((row for row in evidence if row["gate_code"] == "long_stability"), None)
        if stability is None or not _stability_passed(stability):
            raise ApiError("ga.gate_blocked")
        decisions = connection.execute(
            """SELECT * FROM launch_go_no_go_decisions
                WHERE launch_candidate_id=? AND decision='approved'""",
            (command.launch_candidate_id,),
        ).fetchall()
        if len(decisions) != 6 or {row["actor_kind"] for row in decisions} != set(_ACTORS):
            raise ApiError("ga.candidate_not_ready")
        review = connection.execute(
            "SELECT 1 FROM post_launch_reviews WHERE launch_candidate_id=? "
            "AND conclusion='release_candidate'",
            (command.launch_candidate_id,),
        ).fetchone()
        if review is None:
            raise ApiError("ga.candidate_not_ready")
        owner = connection.execute(
            "SELECT workspace_id,tenant_id FROM launch_candidate_artifacts "
            "WHERE launch_candidate_id=? LIMIT 1",
            (command.launch_candidate_id,),
        ).fetchone()
        if owner is None:
            raise ApiError("launch.artifact_not_found")
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code="platform.launch.execute",
            resource_type="ga_candidate",
            resource_id=command.launch_candidate_id,
            now=now,
        )
        launch_id = _id("gal_")
        stamp = _db_time(now)
        connection.execute(
            """INSERT INTO ga_launches(
              ga_launch_id,launch_candidate_id,workspace_id,tenant_id,status,
              candidate_generation,revision,approved_at,launched_at,created_at,updated_at)
            VALUES(?,?,?,?, 'launched',?,?,?, ?,?,?)""",
            (
                launch_id,
                command.launch_candidate_id,
                owner["workspace_id"],
                owner["tenant_id"],
                candidate["generation"],
                1,
                stamp,
                stamp,
                stamp,
                stamp,
            ),
        )
        for decision in decisions:
            connection.execute(
                """INSERT INTO ga_decisions(
                  ga_decision_id,ga_launch_id,actor_kind,operator_id,decision,source_decision_id,decided_at)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    _id("gad_"),
                    launch_id,
                    decision["actor_kind"],
                    decision["operator_id"],
                    "approved",
                    decision["go_no_go_decision_id"],
                    decision["decided_at"],
                ),
            )
        _emit_event(connection, launch_id, "ga.approved", command.launch_candidate_id, now)
        _emit_event(connection, launch_id, "ga.launched", command.launch_candidate_id, now)
        _record_action(connection, actor, "ga.launch", key_sha, request_sha, launch_id, 1)
        response = _ga_launch_response(connection, launch_id, request_id, trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        _rollback(connection)
        raise ApiError("ga.unavailable") from None
    finally:
        connection.close()


def apply_ga_rollback(
    config,
    ga_launch_id: str,
    command: GaRollbackCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> GaRollbackResponse:
    if _GA_LAUNCH_ID.fullmatch(ga_launch_id) is None:
        raise ApiError("ga.launch_not_found")
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    request_sha = _digest(
        {"ga_launch_id": ga_launch_id, "command": command.model_dump(mode="json")}
    )
    connection = _connect(config)
    try:
        _begin_write(connection)
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.launch.rollback",
        )
        _require_schema(connection)
        replay = _ga_action_replay(
            connection, actor.operator_id, f"ga.rollback.{command.operation}", key_sha, request_sha
        )
        if replay:
            return _ga_rollback_response(connection, replay, request_id, trace_id, replay=True)
        launch = _ga_launch_row(connection, ga_launch_id)
        if launch is None:
            raise ApiError("ga.launch_not_found")
        now = _utcnow()
        if command.operation == "request":
            if launch["revision"] != command.expected_revision or launch["status"] not in {
                "launched",
                "approved",
            }:
                raise ApiError("ga.rollback_conflict")
            pending = connection.execute(
                "SELECT ga_rollback_id FROM ga_rollbacks WHERE ga_launch_id=? AND status='pending'",
                (ga_launch_id,),
            ).fetchone()
            if pending:
                raise ApiError("ga.rollback_conflict")
            consume_global_step_up_grant_in_transaction(
                connection,
                config,
                account_id=actor.account_id,
                global_session_id=actor.global_session_id,
                grant=step_up_grant,
                action_code="platform.launch.rollback",
                resource_type="ga_launch",
                resource_id=ga_launch_id,
                now=now,
            )
            rollback_id = _id("gar_")
            stamp = _db_time(now)
            connection.execute(
                """INSERT INTO ga_rollbacks(
                  ga_rollback_id,ga_launch_id,from_status,trigger_code,trigger_sha256,
                  checkpoint_sha256,job_receipt_sha256,verification_sha256,status,revision,
                  requested_by_operator_id,verified_by_operator_id,requested_at,verified_at,updated_at)
                VALUES(?,?,?,?,?,?,?,NULL,'pending',1,?,?,?,NULL,?)""",
                (
                    rollback_id,
                    ga_launch_id,
                    launch["status"],
                    command.trigger_code,
                    command.trigger_sha256,
                    command.checkpoint_sha256,
                    command.job_receipt_sha256,
                    actor.operator_id,
                    None,
                    stamp,
                    stamp,
                ),
            )
            _advance_ga_launch(connection, launch, "rollback_pending", now)
        else:
            if (
                command.ga_rollback_id is None
                or command.verification_sha256 is None
                or command.outcome is None
            ):
                raise ApiError("ga.rollback_conflict")
            rollback = connection.execute(
                "SELECT * FROM ga_rollbacks WHERE ga_rollback_id=? AND ga_launch_id=?",
                (command.ga_rollback_id, ga_launch_id),
            ).fetchone()
            if rollback is None:
                raise ApiError("ga.rollback_not_found")
            if launch["revision"] != command.expected_revision or rollback["status"] != "pending":
                raise ApiError("ga.rollback_conflict")
            if rollback["requested_by_operator_id"] == actor.operator_id:
                raise ApiError("launch.independence_required")
            consume_global_step_up_grant_in_transaction(
                connection,
                config,
                account_id=actor.account_id,
                global_session_id=actor.global_session_id,
                grant=step_up_grant,
                action_code="platform.launch.rollback",
                resource_type="ga_rollback",
                resource_id=command.ga_rollback_id,
                now=now,
            )
            stamp = _db_time(now)
            connection.execute(
                """UPDATE ga_rollbacks SET verification_sha256=?,status=?,revision=revision+1,
                  verified_by_operator_id=?,verified_at=?,updated_at=? WHERE ga_rollback_id=?""",
                (
                    command.verification_sha256,
                    command.outcome,
                    actor.operator_id,
                    stamp,
                    stamp,
                    command.ga_rollback_id,
                ),
            )
            _advance_ga_launch(
                connection, launch, "rolled_back" if command.outcome == "passed" else "failed", now
            )
            rollback_id = command.ga_rollback_id
            _emit_event(
                connection,
                ga_launch_id,
                "ga.rolled_back" if command.outcome == "passed" else "ga.gate_failed",
                launch["launch_candidate_id"],
                now,
            )
        current_launch = _ga_launch_row(connection, ga_launch_id)
        _record_action(
            connection,
            actor,
            f"ga.rollback.{command.operation}",
            key_sha,
            request_sha,
            rollback_id,
            current_launch["revision"] if current_launch else 1,
        )
        response = _ga_rollback_response(connection, rollback_id, request_id, trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        _rollback(connection)
        raise ApiError("ga.unavailable") from None
    finally:
        connection.close()


def record_ga_review(
    config,
    ga_launch_id: str,
    command: GaReviewCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> GaReviewResponse:
    if _GA_LAUNCH_ID.fullmatch(ga_launch_id) is None:
        raise ApiError("ga.launch_not_found")
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    request_sha = _digest(
        {"ga_launch_id": ga_launch_id, "command": command.model_dump(mode="json")}
    )
    connection = _connect(config)
    try:
        _begin_write(connection)
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.launch.execute",
        )
        _require_schema(connection)
        replay = _ga_action_replay(connection, actor.operator_id, "ga.review", key_sha, request_sha)
        if replay:
            row = connection.execute(
                "SELECT * FROM post_ga_reviews WHERE post_ga_review_id=?", (replay,)
            ).fetchone()
            if row is None:
                raise ApiError("ga.launch_not_found")
            response = GaReviewResponse(
                review=_post_ga_review_summary(row),
                launch=_ga_launch_summary(connection, ga_launch_id),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        launch = _ga_launch_row(connection, ga_launch_id)
        if launch is None:
            raise ApiError("ga.launch_not_found")
        if launch["revision"] != command.expected_revision:
            raise ApiError("ga.rollback_conflict")
        expected_conclusion = "ga_launched" if launch["status"] == "launched" else "ga_rolled_back"
        if (
            launch["status"] not in {"launched", "rolled_back"}
            or command.conclusion != expected_conclusion
        ):
            raise ApiError("ga.candidate_not_ready")
        if connection.execute(
            "SELECT 1 FROM post_ga_reviews WHERE ga_launch_id=? AND conclusion=?",
            (ga_launch_id, command.conclusion),
        ).fetchone():
            raise ApiError("ga.rollback_conflict")
        now = _utcnow()
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code="platform.launch.review",
            resource_type="ga_launch",
            resource_id=ga_launch_id,
            now=now,
        )
        review_id = _id("pgr_")
        stamp = _db_time(now)
        connection.execute(
            """INSERT INTO post_ga_reviews(
              post_ga_review_id,ga_launch_id,summary_sha256,action_items_sha256,
              conclusion,reviewed_by_operator_id,reviewed_at)
            VALUES(?,?,?,?,?,?,?)""",
            (
                review_id,
                ga_launch_id,
                command.summary_sha256,
                command.action_items_sha256,
                command.conclusion,
                actor.operator_id,
                stamp,
            ),
        )
        _record_action(
            connection, actor, "ga.review", key_sha, request_sha, review_id, launch["revision"]
        )
        row = connection.execute(
            "SELECT * FROM post_ga_reviews WHERE post_ga_review_id=?", (review_id,)
        ).fetchone()
        response = GaReviewResponse(
            review=_post_ga_review_summary(row),
            launch=_ga_launch_summary(connection, ga_launch_id),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        _rollback(connection)
        raise ApiError("ga.unavailable") from None
    finally:
        connection.close()


def _ga_id(launch_gate_id: str) -> str:
    return "gag_" + launch_gate_id[4:]


def _stability_passed(row: sqlite3.Row) -> bool:
    # The signed RC interval owns the duration; a pass-ratio metric is not hours.
    duration = _as_datetime(row["finished_at"]) - _as_datetime(row["started_at"])
    return timedelta(hours=72) <= duration <= timedelta(hours=96)


def _require_matching_source(command: GaEvidenceCommand, source: sqlite3.Row) -> None:
    """Import only the already verified RC fact, including its signature digest."""
    for field in (
        "workspace_id",
        "artifact_sha256",
        "verifier_code",
        "verifier_version",
        "outcome",
        "metric_value",
        "metric_unit",
        "environment_sha256",
        "provenance_sha256",
    ):
        if getattr(command, field) != source[field]:
            raise ApiError("launch.verifier_invalid")
    for field in ("started_at", "finished_at"):
        if _db_time(getattr(command, field).astimezone(UTC)) != _db_time(
            _as_datetime(source[field])
        ):
            raise ApiError("launch.verifier_invalid")
    signature_sha = hashlib.sha256(command.signature.encode("ascii")).hexdigest()
    if not hmac.compare_digest(signature_sha, source["signature_sha256"]):
        raise ApiError("launch.verifier_invalid")


def _ga_launch_row(connection: sqlite3.Connection, launch_id: str):
    return connection.execute(
        "SELECT * FROM ga_launches WHERE ga_launch_id=?", (launch_id,)
    ).fetchone()


def _ga_action_replay(
    connection, operator_id: str, action_code: str, key_sha: str, request_sha: str
):
    row = connection.execute(
        "SELECT result_object_id,request_sha256 FROM ga_action_commands "
        "WHERE operator_id=? AND action_code=? AND idempotency_key_sha256=?",
        (operator_id, action_code, key_sha),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(row["request_sha256"], request_sha):
        raise ApiError("launch.idempotency_conflict")
    return row["result_object_id"]


def _record_action(
    connection,
    actor,
    action_code: str,
    key_sha: str,
    request_sha: str,
    result_id: str,
    revision: int,
):
    connection.execute(
        """INSERT INTO ga_action_commands(
          ga_action_command_id,operator_id,action_code,idempotency_key_sha256,request_sha256,
          result_object_id,result_revision,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (
            _id("gac_"),
            actor.operator_id,
            action_code,
            key_sha,
            request_sha,
            result_id,
            revision,
            _db_time(_utcnow()),
        ),
    )


def _emit_event(connection, launch_id: str, event_type: str, candidate_id: str, now: datetime):
    event_id = _id("gev_")
    stamp = _db_time(now)
    connection.execute(
        "INSERT INTO ga_events(ga_event_id,ga_launch_id,event_type,payload_sha256,occurred_at) "
        "VALUES(?,?,?,?,?)",
        (
            event_id,
            launch_id,
            event_type,
            _digest({"candidate": candidate_id, "event": event_type}),
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO ga_event_outbox(
          ga_event_outbox_id,ga_event_id,status,attempt_count,next_attempt_at,delivered_at,created_at,updated_at)
        VALUES(?,?, 'pending',0,?,NULL,?,?)""",
        (_id("geo_"), event_id, stamp, stamp, stamp),
    )


def _advance_ga_launch(connection, launch, status: str, now: datetime):
    # Multiple governance actions can intentionally share one test/production clock tick.
    # The immutable transition trigger requires a strictly increasing timestamp, so use the
    # same monotonic database stamp as the RC lifecycle instead of writing ``now`` directly.
    connection.execute(
        "UPDATE ga_launches SET status=?,revision=revision+1,updated_at=? WHERE ga_launch_id=?",
        (status, _next_stamp(launch["updated_at"], now), launch["ga_launch_id"]),
    )


def _ga_launch_response(
    connection, launch_id: str, request_id: str, trace_id: str, replay: bool = False
):
    return GaLaunchResponse(
        launch=_ga_launch_summary(connection, launch_id),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _ga_rollback_response(
    connection, rollback_id: str, request_id: str, trace_id: str, replay: bool = False
):
    row = connection.execute(
        "SELECT * FROM ga_rollbacks WHERE ga_rollback_id=?", (rollback_id,)
    ).fetchone()
    if row is None:
        raise ApiError("ga.rollback_not_found")
    return GaRollbackResponse(
        rollback=_ga_rollback_summary(row),
        launch=_ga_launch_summary(connection, row["ga_launch_id"]),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _ga_launch_summary(connection, launch_id: str) -> GaLaunchSummary:
    row = _ga_launch_row(connection, launch_id)
    if row is None:
        raise ApiError("ga.launch_not_found")
    evidence = tuple(
        _ga_evidence_summary(connection, item["ga_evidence_id"])
        for item in connection.execute(
            "SELECT ga_evidence_id FROM ga_evidence "
            "WHERE launch_candidate_id=? ORDER BY ga_gate_id",
            (row["launch_candidate_id"],),
        ).fetchall()
    )
    decisions = tuple(
        GaDecisionSummary.model_validate(
            {
                "ga_decision_id": item["ga_decision_id"],
                "ga_launch_id": item["ga_launch_id"],
                "actor_kind": item["actor_kind"],
                "operator_id": item["operator_id"],
                "source_decision_id": item["source_decision_id"],
                "decided_at": _as_datetime(item["decided_at"]),
            }
        )
        for item in connection.execute(
            "SELECT * FROM ga_decisions WHERE ga_launch_id=? ORDER BY actor_kind", (launch_id,)
        ).fetchall()
    )
    rollbacks = tuple(
        _ga_rollback_summary(item)
        for item in connection.execute(
            "SELECT * FROM ga_rollbacks WHERE ga_launch_id=? ORDER BY requested_at", (launch_id,)
        ).fetchall()
    )
    review_row = connection.execute(
        """SELECT * FROM post_ga_reviews WHERE ga_launch_id=?
           ORDER BY reviewed_at DESC,post_ga_review_id DESC LIMIT 1""",
        (launch_id,),
    ).fetchone()
    review = (
        PostGaReviewSummary.model_validate(
            {
                "post_ga_review_id": review_row["post_ga_review_id"],
                "ga_launch_id": review_row["ga_launch_id"],
                "summary_sha256": review_row["summary_sha256"],
                "action_items_sha256": review_row["action_items_sha256"],
                "conclusion": review_row["conclusion"],
                "reviewed_at": _as_datetime(review_row["reviewed_at"]),
            }
        )
        if review_row
        else None
    )
    return GaLaunchSummary(
        ga_launch_id=row["ga_launch_id"],
        launch_candidate_id=row["launch_candidate_id"],
        workspace_id=row["workspace_id"],
        status=row["status"],
        candidate_generation=row["candidate_generation"],
        revision=row["revision"],
        approved_at=_as_datetime(row["approved_at"]),
        launched_at=_as_datetime(row["launched_at"]) if row["launched_at"] else None,
        created_at=_as_datetime(row["created_at"]),
        updated_at=_as_datetime(row["updated_at"]),
        evidence=evidence,
        decisions=decisions,
        rollbacks=rollbacks,
        review=review,
    )


def _ga_evidence_summary(connection, evidence_id: str) -> GaEvidenceSummary:
    row = connection.execute(
        "SELECT * FROM ga_evidence WHERE ga_evidence_id=?", (evidence_id,)
    ).fetchone()
    if row is None:
        raise ApiError("ga.gate_blocked")
    return GaEvidenceSummary.model_validate(
        {
            "ga_evidence_id": row["ga_evidence_id"],
            "launch_candidate_id": row["launch_candidate_id"],
            "ga_gate_id": row["ga_gate_id"],
            "source_evidence_id": row["source_evidence_id"],
            "outcome": row["outcome"],
            "finished_at": _as_datetime(row["finished_at"]),
            "expires_at": _as_datetime(row["expires_at"]),
            "created_at": _as_datetime(row["created_at"]),
        }
    )


def _ga_rollback_summary(row) -> GaRollbackSummary:
    return GaRollbackSummary.model_validate(
        {
            "ga_rollback_id": row["ga_rollback_id"],
            "ga_launch_id": row["ga_launch_id"],
            "from_status": row["from_status"],
            "trigger_code": row["trigger_code"],
            "trigger_sha256": row["trigger_sha256"],
            "checkpoint_sha256": row["checkpoint_sha256"],
            "job_receipt_sha256": row["job_receipt_sha256"],
            "verification_sha256": row["verification_sha256"],
            "status": row["status"],
            "revision": row["revision"],
            "requested_at": _as_datetime(row["requested_at"]),
            "verified_at": _as_datetime(row["verified_at"]) if row["verified_at"] else None,
        }
    )


def _post_ga_review_summary(row) -> PostGaReviewSummary:
    return PostGaReviewSummary.model_validate(
        {
            "post_ga_review_id": row["post_ga_review_id"],
            "ga_launch_id": row["ga_launch_id"],
            "summary_sha256": row["summary_sha256"],
            "action_items_sha256": row["action_items_sha256"],
            "conclusion": row["conclusion"],
            "reviewed_at": _as_datetime(row["reviewed_at"]),
        }
    )


def _as_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


__all__ = [
    "apply_ga_rollback",
    "list_ga_gates",
    "promote_ga",
    "record_ga_review",
    "submit_ga_evidence",
]
