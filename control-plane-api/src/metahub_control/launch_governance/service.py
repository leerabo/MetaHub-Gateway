"""Fail-closed candidate evidence, independent decisions, waves, and rollback."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)

from .models import (
    CandidateSummary,
    DecisionSummary,
    EvidenceCommand,
    EvidenceResponse,
    GateListResponse,
    LaunchCommand,
    LaunchEvidenceSummary,
    LaunchGateSummary,
    LaunchResponse,
    ReviewSummary,
    RollbackCommand,
    RollbackResponse,
    RollbackSummary,
    WaveSummary,
)

_CANDIDATE_ID = re.compile(r"^lvc_[0-9a-f]{32}$")
_GATE_ID = re.compile(r"^lgt_[0-9a-f]{32}$")
_WAVE_SEQUENCE = {
    "internal": 1,
    "personal_pilot": 2,
    "enterprise_pilot": 3,
    "managed_private_pilot": 4,
}
_PREVIOUS_WAVE = {
    "internal": "pre_launch",
    "personal_pilot": "internal",
    "enterprise_pilot": "personal_pilot",
    "managed_private_pilot": "enterprise_pilot",
}
_ACTOR_PERMISSION = {
    "product": "platform.launch.sign.product",
    "sre": "platform.launch.sign.sre",
    "security": "platform.launch.sign.security",
    "test": "platform.launch.sign.test",
    "finance": "platform.launch.sign.finance",
    "legal": "platform.launch.sign.legal",
}
_ACTIVE_CANDIDATE_STATES = {
    "collecting",
    "signing",
    "ready",
    "launching",
    "paused",
    "review_pending",
    "rollback_pending",
    "release_candidate",
}


@dataclass(frozen=True, slots=True)
class EvidenceVerificationRequest:
    launch_candidate_id: str
    candidate_sha256: str
    launch_gate_id: str
    gate_code: str
    policy_sha256: str
    workspace_id: str
    tenant_id: str
    artifact_sha256: str
    verifier_code: str
    verifier_version: int
    outcome: str
    metric_value: float | None
    metric_unit: str | None
    environment_sha256: str
    provenance_sha256: str
    started_at: datetime
    finished_at: datetime
    envelope_sha256: str


EvidenceVerifier = Callable[[EvidenceVerificationRequest, str], bool]


def evidence_envelope_sha256(request: EvidenceVerificationRequest) -> str:
    payload = asdict(request)
    payload.pop("envelope_sha256")
    payload["started_at"] = _db_time(_as_utc(request.started_at))
    payload["finished_at"] = _db_time(_as_utc(request.finished_at))
    return _digest(payload)


def list_launch_gates(
    config: AppConfig,
    *,
    platform_session_token: str,
    request_id: str,
    trace_id: str,
) -> GateListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        _platform_actor(
            connection,
            config,
            platform_session_token,
            None,
            "platform.launch.read",
        )
        _require_schema(connection)
        candidate = connection.execute(
            """SELECT launch_candidate_id FROM launch_candidates
                ORDER BY generation DESC LIMIT 1"""
        ).fetchone()
        response = GateListResponse(
            gates=_gate_summaries(connection),
            candidate=(
                _candidate_summary(connection, candidate["launch_candidate_id"])
                if candidate is not None
                else None
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("launch.unavailable") from None
    finally:
        connection.close()


def apply_launch_command(
    config: AppConfig,
    command: LaunchCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> LaunchResponse:
    permission = (
        "platform.launch.create"
        if command.operation == "create"
        else _ACTOR_PERMISSION.get(command.actor_kind, "platform.launch.execute")
    )
    action_code = _launch_action_code(command.operation)
    request_sha = _digest(command.model_dump(mode="json"))
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            permission,
        )
        _require_schema(connection)
        replay = _command_replay(connection, actor.operator_id, action_code, key_sha, request_sha)
        if replay is not None:
            response = LaunchResponse(
                candidate=_candidate_summary(connection, replay),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        if command.operation == "create":
            candidate_id, owner = _create_candidate(
                connection, config, actor, command, step_up_grant
            )
        else:
            assert command.launch_candidate_id is not None
            candidate_id = command.launch_candidate_id
            candidate = _candidate_row(connection, candidate_id)
            if candidate["revision"] != command.expected_revision:
                raise ApiError("launch.revision_conflict")
            if command.operation == "decide":
                owner = _apply_decision(
                    connection, config, actor, candidate, command, step_up_grant
                )
            elif command.operation == "review":
                owner = _apply_review(connection, config, actor, candidate, command, step_up_grant)
            else:
                owner = _apply_wave_action(
                    connection, config, actor, candidate, command, step_up_grant
                )
        candidate = _candidate_row(connection, candidate_id)
        _record_action(
            connection,
            actor,
            owner,
            action_code,
            key_sha,
            request_sha,
            candidate_id,
            candidate["revision"],
        )
        response = LaunchResponse(
            candidate=_candidate_summary(connection, candidate_id),
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
        raise ApiError("launch.unavailable") from None
    finally:
        connection.close()


def submit_launch_evidence(
    config: AppConfig,
    launch_gate_id: str,
    command: EvidenceCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    verifier: EvidenceVerifier | None,
) -> EvidenceResponse:
    if _GATE_ID.fullmatch(launch_gate_id) is None:
        raise ApiError("launch.gate_not_found")
    request_sha = _digest(command.model_dump(mode="json", exclude={"signature"}))
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    preflight = _evidence_preflight(
        config,
        launch_gate_id,
        command,
        platform_session_token=platform_session_token,
        platform_csrf_token=platform_csrf_token,
        key_sha=key_sha,
        request_sha=request_sha,
        request_id=request_id,
        trace_id=trace_id,
    )
    if isinstance(preflight, EvidenceResponse):
        return preflight
    actor, verification = preflight
    if verifier is None:
        raise ApiError("launch.verifier_unavailable")
    try:
        verified = verifier(verification, command.signature)
    except Exception:
        raise ApiError("launch.verifier_invalid") from None
    if verified is not True:
        raise ApiError("launch.verifier_invalid")

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.launch.evidence.submit",
        )
        _require_schema(connection)
        if (
            current_actor.operator_id != actor.operator_id
            or current_actor.operator_revision != actor.operator_revision
        ):
            raise ApiError("launch.permission_revoked")
        replay = _command_replay(
            connection, current_actor.operator_id, "evidence.submit", key_sha, request_sha
        )
        if replay is not None:
            return _evidence_response(
                connection,
                replay,
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
        candidate, gate, artifact = _evidence_rows(connection, launch_gate_id, command)
        if candidate["revision"] != command.expected_revision:
            raise ApiError("launch.revision_conflict")
        if candidate["status"] != "collecting":
            raise ApiError("launch.evidence_conflict")
        now = _utcnow()
        _validate_evidence_time(command, gate, now)
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=current_actor.account_id,
            global_session_id=current_actor.global_session_id,
            grant=step_up_grant,
            action_code="platform.launch.evidence.submit",
            resource_type="launch_gate",
            resource_id=launch_gate_id,
            now=now,
        )
        evidence_id = _id("lve_")
        stamp = _db_time(now)
        expires_at = _db_time(
            _as_utc(command.finished_at) + timedelta(seconds=int(gate["freshness_seconds"]))
        )
        connection.execute(
            """INSERT INTO launch_evidence(
              launch_evidence_id,launch_candidate_id,launch_gate_id,workspace_id,tenant_id,
              artifact_sha256,verifier_code,verifier_version,envelope_sha256,
              signature_sha256,outcome,metric_value,metric_unit,environment_sha256,
              provenance_sha256,started_at,finished_at,expires_at,submitted_by_operator_id,
              submitted_permission_revision,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                evidence_id,
                candidate["launch_candidate_id"],
                gate["launch_gate_id"],
                artifact["workspace_id"],
                artifact["tenant_id"],
                artifact["artifact_sha256"],
                gate["verifier_code"],
                gate["verifier_version"],
                verification.envelope_sha256,
                hashlib.sha256(command.signature.encode("ascii")).hexdigest(),
                command.outcome,
                command.metric_value,
                command.metric_unit,
                command.environment_sha256,
                command.provenance_sha256,
                _db_time(_as_utc(command.started_at)),
                _db_time(_as_utc(command.finished_at)),
                expires_at,
                current_actor.operator_id,
                current_actor.operator_revision,
                stamp,
            ),
        )
        passed_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM launch_evidence
                    WHERE launch_candidate_id=? AND outcome='passed'""",
                (candidate["launch_candidate_id"],),
            ).fetchone()[0]
        )
        status = (
            "failed"
            if command.outcome == "failed"
            else ("signing" if passed_count == 20 else "collecting")
        )
        candidate_stamp = _next_stamp(candidate["updated_at"], now)
        connection.execute(
            """UPDATE launch_candidates SET status=?,revision=revision+1,updated_at=?
                WHERE launch_candidate_id=?""",
            (status, candidate_stamp, candidate["launch_candidate_id"]),
        )
        if command.outcome == "failed":
            _emit_event(
                connection,
                candidate["launch_candidate_id"],
                artifact["workspace_id"],
                artifact["tenant_id"],
                "launch.gate_failed",
                "evidence",
                evidence_id,
                _digest({"gate_code": gate["gate_code"], "outcome": command.outcome}),
                now,
            )
        current_candidate = _candidate_row(connection, candidate["launch_candidate_id"])
        _record_action(
            connection,
            current_actor,
            (artifact["workspace_id"], artifact["tenant_id"]),
            "evidence.submit",
            key_sha,
            request_sha,
            evidence_id,
            current_candidate["revision"],
        )
        response = _evidence_response(
            connection, evidence_id, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        _rollback(connection)
        raise ApiError("launch.unavailable") from None
    finally:
        connection.close()


def apply_launch_rollback(
    config: AppConfig,
    launch_candidate_id: str,
    command: RollbackCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> RollbackResponse:
    if _CANDIDATE_ID.fullmatch(launch_candidate_id) is None:
        raise ApiError("launch.candidate_not_found")
    action_code = f"rollback.{command.operation}"
    request_sha = _digest(command.model_dump(mode="json"))
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.launch.rollback",
        )
        _require_schema(connection)
        replay = _command_replay(connection, actor.operator_id, action_code, key_sha, request_sha)
        if replay is not None:
            response = _rollback_response(
                connection,
                replay,
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        candidate = _candidate_row(connection, launch_candidate_id)
        _require_release_authority(connection, candidate, actor)
        now = _utcnow()
        if command.operation == "request":
            if candidate["revision"] != command.expected_revision:
                raise ApiError("launch.revision_conflict")
            rollback_id, owner = _request_rollback(
                connection, config, actor, candidate, command, step_up_grant, now
            )
        else:
            rollback_id, owner = _verify_rollback(
                connection, config, actor, candidate, command, step_up_grant, now
            )
        rollback = connection.execute(
            "SELECT revision FROM launch_rollbacks WHERE launch_rollback_id=?",
            (rollback_id,),
        ).fetchone()
        assert rollback is not None
        _record_action(
            connection,
            actor,
            owner,
            action_code,
            key_sha,
            request_sha,
            rollback_id,
            rollback["revision"],
        )
        response = _rollback_response(
            connection, rollback_id, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        _rollback(connection)
        raise ApiError("launch.unavailable") from None
    finally:
        connection.close()


def process_launch_governance_once(config: AppConfig, *, worker_id: str) -> int:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", worker_id):
        raise ApiError("launch.worker_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        now = _utcnow()
        state = connection.execute(
            "SELECT * FROM launch_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        if state is None:
            raise ApiError("launch.unavailable")
        if (
            state["lease_owner_id"] is not None
            and _parse_time(state["lease_expires_at"]) > now
            and state["lease_owner_id"] != worker_id
        ):
            connection.commit()
            return 0
        stamp = _next_stamp(state["updated_at"], now)
        connection.execute(
            """UPDATE launch_scheduler_state SET lease_owner_id=?,lease_token_sha256=?,
              lease_expires_at=?,fencing_generation=fencing_generation+1,
              revision=revision+1,updated_at=? WHERE singleton_id=1""",
            (
                worker_id,
                hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                _db_time(now + timedelta(seconds=30)),
                stamp,
            ),
        )
        changed = 0
        rows = connection.execute(
            """SELECT candidate.launch_candidate_id,candidate.updated_at,
                     evidence.launch_evidence_id,evidence.workspace_id,evidence.tenant_id,
                     gate.gate_code
                FROM launch_candidates candidate
                JOIN launch_evidence evidence
                  ON evidence.launch_candidate_id=candidate.launch_candidate_id
                JOIN launch_gate_policies gate ON gate.launch_gate_id=evidence.launch_gate_id
               WHERE candidate.status IN(
                 'collecting','signing','ready','launching','paused','review_pending')
                 AND evidence.expires_at<=?
               ORDER BY candidate.generation,evidence.created_at""",
            (_db_time(now),),
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            if row["launch_candidate_id"] in seen:
                continue
            seen.add(row["launch_candidate_id"])
            connection.execute(
                """UPDATE launch_candidates SET status='failed',revision=revision+1,
                  updated_at=? WHERE launch_candidate_id=?""",
                (_next_stamp(row["updated_at"], now), row["launch_candidate_id"]),
            )
            _emit_event(
                connection,
                row["launch_candidate_id"],
                row["workspace_id"],
                row["tenant_id"],
                "launch.gate_failed",
                "evidence",
                row["launch_evidence_id"],
                _digest({"gate_code": row["gate_code"], "reason": "evidence_expired"}),
                now,
            )
            changed += 1
        pending = connection.execute(
            """SELECT launch_event_outbox_id,updated_at FROM launch_event_outbox
                WHERE status='pending' AND next_attempt_at<=?
                ORDER BY created_at LIMIT 100""",
            (_db_time(now),),
        ).fetchall()
        for row in pending:
            connection.execute(
                """UPDATE launch_event_outbox SET status='delivered',attempt_count=attempt_count+1,
                  delivered_at=?,updated_at=? WHERE launch_event_outbox_id=?""",
                (
                    _db_time(now),
                    _next_stamp(row["updated_at"], now),
                    row["launch_event_outbox_id"],
                ),
            )
            changed += 1
        state = connection.execute(
            "SELECT * FROM launch_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        assert state is not None
        connection.execute(
            """UPDATE launch_scheduler_state SET freshness_watermark_at=?,
              lease_owner_id=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,
              revision=revision+1,updated_at=? WHERE singleton_id=1""",
            (_db_time(now), _next_stamp(state["updated_at"], now)),
        )
        connection.commit()
        return changed
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("launch.unavailable") from None
    finally:
        connection.close()


def _create_candidate(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    command: LaunchCommand,
    step_up_grant: str,
) -> tuple[str, tuple[str, str]]:
    assert command.artifacts is not None and command.waves is not None
    gate_rows = connection.execute(
        "SELECT * FROM launch_gate_policies WHERE status='active' ORDER BY gate_code"
    ).fetchall()
    if len(gate_rows) != 20:
        raise ApiError("launch.gate_registry_invalid")
    gate_by_code = {row["gate_code"]: row for row in gate_rows}
    artifact_inputs: list[dict[str, object]] = []
    artifact_owners: dict[str, tuple[str, str]] = {}
    for item in sorted(command.artifacts, key=lambda value: value.gate_code):
        if item.gate_code not in gate_by_code:
            raise ApiError("launch.gate_registry_invalid")
        owner = _workspace_owner(connection, item.workspace_id)
        artifact_owners[item.gate_code] = owner
        artifact_inputs.append(
            {
                "gate_code": item.gate_code,
                "workspace_id": owner[0],
                "tenant_id": owner[1],
                "artifact_sha256": item.artifact_sha256,
            }
        )
    computed_artifact_set = _digest(
        [
            {
                "gate_code": item["gate_code"],
                "workspace_id": item["workspace_id"],
                "artifact_sha256": item["artifact_sha256"],
            }
            for item in artifact_inputs
        ]
    )
    if command.artifact_set_sha256 != computed_artifact_set:
        raise ApiError("launch.artifact_set_invalid")
    wave_inputs: list[dict[str, object]] = []
    wave_owners: dict[str, tuple[str, str]] = {}
    for item in sorted(command.waves, key=lambda value: _WAVE_SEQUENCE[value.wave_kind]):
        owner = _workspace_owner(connection, item.workspace_id)
        wave_owners[item.wave_kind] = owner
        wave_inputs.append(
            {
                **item.model_dump(mode="json"),
                "tenant_id": owner[1],
                "wave_sequence": _WAVE_SEQUENCE[item.wave_kind],
            }
        )
    policy_set_sha = _digest(
        [
            {
                "launch_gate_id": row["launch_gate_id"],
                "gate_code": row["gate_code"],
                "policy_sha256": row["policy_sha256"],
            }
            for row in gate_rows
        ]
    )
    candidate_sha = _digest(
        {
            "code_tree_sha256": command.code_tree_sha256,
            "migration_set_sha256": command.migration_set_sha256,
            "contract_set_sha256": command.contract_set_sha256,
            "artifact_set_sha256": computed_artifact_set,
            "test_manifest_sha256": command.test_manifest_sha256,
            "gate_policy_set_sha256": policy_set_sha,
            "artifacts": artifact_inputs,
            "waves": wave_inputs,
        }
    )
    if (
        connection.execute(
            "SELECT 1 FROM launch_candidates WHERE candidate_sha256=?", (candidate_sha,)
        ).fetchone()
        is not None
    ):
        raise ApiError("launch.candidate_conflict")
    running = connection.execute(
        """SELECT 1 FROM launch_candidates WHERE status IN(
          'launching','paused','review_pending','rollback_pending') LIMIT 1"""
    ).fetchone()
    if running is not None:
        raise ApiError("launch.candidate_conflict")
    now = _utcnow()
    first_owner = artifact_owners[sorted(artifact_owners)[0]]
    consume_global_step_up_grant_in_transaction(
        connection,
        config,
        account_id=actor.account_id,
        global_session_id=actor.global_session_id,
        grant=step_up_grant,
        action_code="platform.launch.create",
        resource_type="workspace",
        resource_id=first_owner[0],
        now=now,
    )
    stale_rows = connection.execute(
        """SELECT launch_candidate_id,updated_at FROM launch_candidates
            WHERE status IN('collecting','signing','ready','release_candidate')"""
    ).fetchall()
    for row in stale_rows:
        connection.execute(
            """UPDATE launch_candidates SET status='stale',revision=revision+1,updated_at=?
                WHERE launch_candidate_id=?""",
            (_next_stamp(row["updated_at"], now), row["launch_candidate_id"]),
        )
    generation = int(
        connection.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM launch_candidates"
        ).fetchone()[0]
    )
    candidate_id = _id("lvc_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO launch_candidates(
          launch_candidate_id,candidate_sha256,code_tree_sha256,migration_set_sha256,
          contract_set_sha256,artifact_set_sha256,test_manifest_sha256,
          gate_policy_set_sha256,generation,status,requested_by_operator_id,
          requested_permission_revision,revision,created_at,updated_at,completed_at)
        VALUES(?,?,?,?,?,?,?,?,?,'collecting',?,?,1,?,?,NULL)""",
        (
            candidate_id,
            candidate_sha,
            command.code_tree_sha256,
            command.migration_set_sha256,
            command.contract_set_sha256,
            computed_artifact_set,
            command.test_manifest_sha256,
            policy_set_sha,
            generation,
            actor.operator_id,
            actor.operator_revision,
            stamp,
            stamp,
        ),
    )
    command_artifacts = {item.gate_code: item for item in command.artifacts}
    for gate_code, gate in gate_by_code.items():
        item = command_artifacts[gate_code]
        owner = artifact_owners[gate_code]
        connection.execute(
            """INSERT INTO launch_candidate_artifacts(
              launch_candidate_artifact_id,launch_candidate_id,launch_gate_id,
              workspace_id,tenant_id,artifact_sha256,registered_at)
            VALUES(?,?,?,?,?,?,?)""",
            (
                _id("lca_"),
                candidate_id,
                gate["launch_gate_id"],
                *owner,
                item.artifact_sha256,
                stamp,
            ),
        )
    command_waves = {item.wave_kind: item for item in command.waves}
    for wave_kind, sequence in _WAVE_SEQUENCE.items():
        item = command_waves[wave_kind]
        owner = wave_owners[wave_kind]
        connection.execute(
            """INSERT INTO launch_waves(
              launch_wave_id,launch_candidate_id,wave_kind,wave_sequence,workspace_id,
              tenant_id,cohort_manifest_sha256,feature_flag_generation,
              migration_plan_sha256,rollback_plan_sha256,observation_seconds,status,
              observation_deadline_at,health_watermark_sha256,revision,started_at,
              completed_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,1,NULL,NULL,?)""",
            (
                _id("lvw_"),
                candidate_id,
                wave_kind,
                sequence,
                *owner,
                item.cohort_manifest_sha256,
                item.feature_flag_generation,
                item.migration_plan_sha256,
                item.rollback_plan_sha256,
                item.observation_seconds,
                stamp,
            ),
        )
    return candidate_id, first_owner


def _apply_decision(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    candidate: sqlite3.Row,
    command: LaunchCommand,
    step_up_grant: str,
) -> tuple[str, str]:
    assert command.actor_kind is not None and command.approved is not None
    assert command.reason_code is not None
    if candidate["status"] != "signing":
        raise ApiError("launch.decision_conflict")
    if actor.operator_id == candidate["requested_by_operator_id"]:
        raise ApiError("launch.independence_required")
    if config.approval_hmac_secret is None:
        raise ApiError("launch.unavailable")
    owner = _candidate_owner(connection, candidate["launch_candidate_id"])
    now = _utcnow()
    permission = _ACTOR_PERMISSION[command.actor_kind]
    consume_global_step_up_grant_in_transaction(
        connection,
        config,
        account_id=actor.account_id,
        global_session_id=actor.global_session_id,
        grant=step_up_grant,
        action_code=permission,
        resource_type="launch_candidate",
        resource_id=candidate["launch_candidate_id"],
        now=now,
    )
    payload = {
        "launch_candidate_id": candidate["launch_candidate_id"],
        "candidate_sha256": candidate["candidate_sha256"],
        "generation": candidate["generation"],
        "actor_kind": command.actor_kind,
        "operator_id": actor.operator_id,
        "operator_revision": actor.operator_revision,
        "permission_code": permission,
        "decision": "approved" if command.approved else "rejected",
        "reason_code": command.reason_code,
    }
    decision_sha = _digest(payload)
    signature_sha = hmac.new(
        config.approval_hmac_secret.encode("utf-8"),
        f"metahub/launch-decision/v1/{decision_sha}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    connection.execute(
        """INSERT INTO launch_go_no_go_decisions(
          go_no_go_decision_id,launch_candidate_id,actor_kind,operator_id,
          operator_revision,permission_code,candidate_generation,decision,
          reason_code,decision_sha256,signature_sha256,decided_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("lgd_"),
            candidate["launch_candidate_id"],
            command.actor_kind,
            actor.operator_id,
            actor.operator_revision,
            permission,
            candidate["generation"],
            payload["decision"],
            command.reason_code,
            decision_sha,
            signature_sha,
            _db_time(now),
        ),
    )
    approved_count = int(
        connection.execute(
            """SELECT COUNT(*) FROM launch_go_no_go_decisions
                WHERE launch_candidate_id=? AND decision='approved'""",
            (candidate["launch_candidate_id"],),
        ).fetchone()[0]
    )
    if command.approved and approved_count == 6:
        _validate_current_approvals(connection, candidate, now)
    new_status = (
        "failed" if not command.approved else ("ready" if approved_count == 6 else "signing")
    )
    connection.execute(
        """UPDATE launch_candidates SET status=?,revision=revision+1,updated_at=?
            WHERE launch_candidate_id=?""",
        (new_status, _next_stamp(candidate["updated_at"], now), candidate["launch_candidate_id"]),
    )
    return owner


def _apply_wave_action(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    candidate: sqlite3.Row,
    command: LaunchCommand,
    step_up_grant: str,
) -> tuple[str, str]:
    assert command.wave_kind is not None
    _require_release_authority(connection, candidate, actor)
    wave = connection.execute(
        """SELECT * FROM launch_waves
            WHERE launch_candidate_id=? AND wave_kind=?""",
        (candidate["launch_candidate_id"], command.wave_kind),
    ).fetchone()
    if wave is None:
        raise ApiError("launch.wave_not_found")
    now = _utcnow()
    operation = command.operation
    if operation == "start_wave":
        if wave["status"] != "pending" or candidate["status"] not in {"ready", "launching"}:
            raise ApiError("launch.wave_conflict")
        if wave["wave_sequence"] > 1:
            previous = connection.execute(
                """SELECT status FROM launch_waves
                    WHERE launch_candidate_id=? AND wave_sequence=?""",
                (candidate["launch_candidate_id"], wave["wave_sequence"] - 1),
            ).fetchone()
            if previous is None or previous["status"] != "completed":
                raise ApiError("launch.wave_conflict")
        _validate_current_approvals(connection, candidate, now)
        wave_status = "running"
        candidate_status = "launching"
        deadline = _db_time(now + timedelta(seconds=int(wave["observation_seconds"])))
        started_at = _db_time(now)
        completed_at = wave["completed_at"]
    elif operation == "pause_wave":
        if wave["status"] != "running" or candidate["status"] != "launching":
            raise ApiError("launch.wave_conflict")
        wave_status = "paused"
        candidate_status = "paused"
        deadline = wave["observation_deadline_at"]
        started_at = wave["started_at"]
        completed_at = wave["completed_at"]
    elif operation == "resume_wave":
        if wave["status"] != "paused" or candidate["status"] != "paused":
            raise ApiError("launch.wave_conflict")
        _validate_current_approvals(connection, candidate, now)
        wave_status = "running"
        candidate_status = "launching"
        deadline = _db_time(now + timedelta(seconds=int(wave["observation_seconds"])))
        started_at = wave["started_at"]
        completed_at = wave["completed_at"]
    elif operation == "complete_wave":
        if wave["status"] != "running" or candidate["status"] != "launching":
            raise ApiError("launch.wave_conflict")
        _validate_current_approvals(connection, candidate, now)
        if _parse_time(wave["observation_deadline_at"]) > now:
            raise ApiError("launch.observation_pending")
        wave_status = "completed"
        candidate_status = "review_pending" if wave["wave_sequence"] == 4 else "launching"
        deadline = wave["observation_deadline_at"]
        started_at = wave["started_at"]
        completed_at = _db_time(now)
    else:
        raise ApiError("launch.request_invalid")
    consume_global_step_up_grant_in_transaction(
        connection,
        config,
        account_id=actor.account_id,
        global_session_id=actor.global_session_id,
        grant=step_up_grant,
        action_code="platform.launch.execute",
        resource_type="launch_wave",
        resource_id=wave["launch_wave_id"],
        now=now,
    )
    connection.execute(
        """UPDATE launch_waves SET status=?,observation_deadline_at=?,
          health_watermark_sha256=COALESCE(?,health_watermark_sha256),
          revision=revision+1,started_at=?,completed_at=?,updated_at=?
          WHERE launch_wave_id=?""",
        (
            wave_status,
            deadline,
            command.health_watermark_sha256,
            started_at,
            completed_at,
            _next_stamp(wave["updated_at"], now),
            wave["launch_wave_id"],
        ),
    )
    connection.execute(
        """UPDATE launch_candidates SET status=?,revision=revision+1,updated_at=?
            WHERE launch_candidate_id=?""",
        (
            candidate_status,
            _next_stamp(candidate["updated_at"], now),
            candidate["launch_candidate_id"],
        ),
    )
    if operation in {"start_wave", "complete_wave"}:
        _emit_event(
            connection,
            candidate["launch_candidate_id"],
            wave["workspace_id"],
            wave["tenant_id"],
            "launch.wave_started" if operation == "start_wave" else "launch.wave_completed",
            "wave",
            wave["launch_wave_id"],
            _digest(
                {
                    "wave_kind": wave["wave_kind"],
                    "health_watermark_sha256": command.health_watermark_sha256,
                }
            ),
            now,
        )
    return wave["workspace_id"], wave["tenant_id"]


def _apply_review(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    candidate: sqlite3.Row,
    command: LaunchCommand,
    step_up_grant: str,
) -> tuple[str, str]:
    assert command.summary_sha256 is not None and command.action_items_sha256 is not None
    if candidate["status"] != "review_pending":
        raise ApiError("launch.review_conflict")
    _require_release_authority(connection, candidate, actor)
    now = _utcnow()
    _validate_current_approvals(connection, candidate, now)
    waves = connection.execute(
        "SELECT * FROM launch_waves WHERE launch_candidate_id=? ORDER BY wave_sequence",
        (candidate["launch_candidate_id"],),
    ).fetchall()
    if len(waves) != 4 or any(wave["status"] != "completed" for wave in waves):
        raise ApiError("launch.review_conflict")
    final_wave = waves[-1]
    consume_global_step_up_grant_in_transaction(
        connection,
        config,
        account_id=actor.account_id,
        global_session_id=actor.global_session_id,
        grant=step_up_grant,
        action_code="platform.launch.execute",
        resource_type="launch_candidate",
        resource_id=candidate["launch_candidate_id"],
        now=now,
    )
    connection.execute(
        """INSERT INTO post_launch_reviews(
          post_launch_review_id,launch_candidate_id,wave_kind,workspace_id,tenant_id,
          summary_sha256,action_items_sha256,conclusion,reviewed_by_operator_id,
          reviewed_permission_revision,reviewed_at)
        VALUES(?,?,'managed_private_pilot',?,?,?,?, 'release_candidate',?,?,?)""",
        (
            _id("lpr_"),
            candidate["launch_candidate_id"],
            final_wave["workspace_id"],
            final_wave["tenant_id"],
            command.summary_sha256,
            command.action_items_sha256,
            actor.operator_id,
            actor.operator_revision,
            _db_time(now),
        ),
    )
    stamp = _next_stamp(candidate["updated_at"], now)
    connection.execute(
        """UPDATE launch_candidates SET status='release_candidate',revision=revision+1,
          updated_at=?,completed_at=? WHERE launch_candidate_id=?""",
        (stamp, stamp, candidate["launch_candidate_id"]),
    )
    return final_wave["workspace_id"], final_wave["tenant_id"]


def _request_rollback(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    candidate: sqlite3.Row,
    command: RollbackCommand,
    step_up_grant: str,
    now: datetime,
) -> tuple[str, tuple[str, str]]:
    assert command.from_wave_kind is not None and command.to_wave_kind is not None
    assert command.trigger_code is not None and command.trigger_sha256 is not None
    assert command.checkpoint_sha256 is not None and command.job_receipt_sha256 is not None
    if candidate["status"] not in {
        "launching",
        "paused",
        "review_pending",
        "release_candidate",
    }:
        raise ApiError("launch.rollback_conflict")
    if command.to_wave_kind != _PREVIOUS_WAVE[command.from_wave_kind]:
        raise ApiError("launch.rollback_conflict")
    wave = connection.execute(
        """SELECT * FROM launch_waves
            WHERE launch_candidate_id=? AND wave_kind=?""",
        (candidate["launch_candidate_id"], command.from_wave_kind),
    ).fetchone()
    if wave is None or wave["status"] not in {"running", "paused", "completed"}:
        raise ApiError("launch.rollback_conflict")
    if (
        connection.execute(
            """SELECT 1 FROM launch_rollbacks
            WHERE launch_candidate_id=? AND status='pending'""",
            (candidate["launch_candidate_id"],),
        ).fetchone()
        is not None
    ):
        raise ApiError("launch.rollback_conflict")
    consume_global_step_up_grant_in_transaction(
        connection,
        config,
        account_id=actor.account_id,
        global_session_id=actor.global_session_id,
        grant=step_up_grant,
        action_code="platform.launch.rollback",
        resource_type="launch_wave",
        resource_id=wave["launch_wave_id"],
        now=now,
    )
    rollback_id = _id("lrb_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO launch_rollbacks(
          launch_rollback_id,launch_candidate_id,workspace_id,tenant_id,
          from_wave_kind,to_wave_kind,trigger_code,trigger_sha256,checkpoint_sha256,
          job_receipt_sha256,verification_sha256,status,requested_by_operator_id,
          requested_permission_revision,revision,requested_at,verified_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,NULL,'pending',?,?,1,?,NULL,?)""",
        (
            rollback_id,
            candidate["launch_candidate_id"],
            wave["workspace_id"],
            wave["tenant_id"],
            command.from_wave_kind,
            command.to_wave_kind,
            command.trigger_code,
            command.trigger_sha256,
            command.checkpoint_sha256,
            command.job_receipt_sha256,
            actor.operator_id,
            actor.operator_revision,
            stamp,
            stamp,
        ),
    )
    affected = connection.execute(
        """SELECT launch_wave_id,updated_at FROM launch_waves
            WHERE launch_candidate_id=? AND wave_sequence>=?
              AND status!='rolled_back' ORDER BY wave_sequence""",
        (candidate["launch_candidate_id"], wave["wave_sequence"]),
    ).fetchall()
    for item in affected:
        connection.execute(
            """UPDATE launch_waves SET status='rolled_back',revision=revision+1,
              completed_at=COALESCE(completed_at,?),updated_at=? WHERE launch_wave_id=?""",
            (stamp, _next_stamp(item["updated_at"], now), item["launch_wave_id"]),
        )
    connection.execute(
        """UPDATE launch_candidates SET status='rollback_pending',revision=revision+1,
          updated_at=? WHERE launch_candidate_id=?""",
        (_next_stamp(candidate["updated_at"], now), candidate["launch_candidate_id"]),
    )
    return rollback_id, (wave["workspace_id"], wave["tenant_id"])


def _verify_rollback(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    candidate: sqlite3.Row,
    command: RollbackCommand,
    step_up_grant: str,
    now: datetime,
) -> tuple[str, tuple[str, str]]:
    assert command.launch_rollback_id is not None and command.outcome is not None
    assert command.verification_sha256 is not None
    rollback = connection.execute(
        """SELECT * FROM launch_rollbacks
            WHERE launch_rollback_id=? AND launch_candidate_id=?""",
        (command.launch_rollback_id, candidate["launch_candidate_id"]),
    ).fetchone()
    if rollback is None:
        raise ApiError("launch.rollback_not_found")
    if (
        rollback["status"] != "pending"
        or rollback["revision"] != command.expected_revision
        or candidate["status"] != "rollback_pending"
        or rollback["requested_by_operator_id"] == actor.operator_id
    ):
        raise ApiError("launch.rollback_conflict")
    prior_execution = connection.execute(
        """SELECT 1 FROM launch_action_commands
            WHERE operator_id=? AND result_object_id=?
              AND action_code IN(
                'wave.start','wave.pause','wave.resume','wave.complete','candidate.review'
              ) LIMIT 1""",
        (actor.operator_id, candidate["launch_candidate_id"]),
    ).fetchone()
    if prior_execution is not None:
        raise ApiError("launch.independence_required")
    consume_global_step_up_grant_in_transaction(
        connection,
        config,
        account_id=actor.account_id,
        global_session_id=actor.global_session_id,
        grant=step_up_grant,
        action_code="platform.launch.rollback",
        resource_type="launch_rollback",
        resource_id=rollback["launch_rollback_id"],
        now=now,
    )
    status = command.outcome
    stamp = _next_stamp(rollback["updated_at"], now)
    connection.execute(
        """UPDATE launch_rollbacks SET verification_sha256=?,status=?,revision=revision+1,
          verified_at=?,updated_at=? WHERE launch_rollback_id=?""",
        (
            command.verification_sha256,
            status,
            _db_time(now),
            stamp,
            rollback["launch_rollback_id"],
        ),
    )
    candidate_status = "rolled_back" if status == "passed" else "failed"
    completed = _db_time(now) if status == "passed" else candidate["completed_at"]
    connection.execute(
        """UPDATE launch_candidates SET status=?,revision=revision+1,
          updated_at=?,completed_at=? WHERE launch_candidate_id=?""",
        (
            candidate_status,
            _next_stamp(candidate["updated_at"], now),
            completed,
            candidate["launch_candidate_id"],
        ),
    )
    if status == "passed":
        _emit_event(
            connection,
            candidate["launch_candidate_id"],
            rollback["workspace_id"],
            rollback["tenant_id"],
            "launch.rolled_back",
            "rollback",
            rollback["launch_rollback_id"],
            _digest(
                {
                    "from_wave_kind": rollback["from_wave_kind"],
                    "to_wave_kind": rollback["to_wave_kind"],
                    "verification_sha256": command.verification_sha256,
                }
            ),
            now,
        )
    return rollback["launch_rollback_id"], (
        rollback["workspace_id"],
        rollback["tenant_id"],
    )


def _evidence_preflight(
    config: AppConfig,
    launch_gate_id: str,
    command: EvidenceCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    key_sha: str,
    request_sha: str,
    request_id: str,
    trace_id: str,
) -> tuple[PlatformContext, EvidenceVerificationRequest] | EvidenceResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.launch.evidence.submit",
        )
        _require_schema(connection)
        replay = _command_replay(
            connection, actor.operator_id, "evidence.submit", key_sha, request_sha
        )
        if replay is not None:
            response = _evidence_response(
                connection,
                replay,
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        candidate, gate, artifact = _evidence_rows(connection, launch_gate_id, command)
        if candidate["revision"] != command.expected_revision:
            raise ApiError("launch.revision_conflict")
        if candidate["status"] != "collecting":
            raise ApiError("launch.evidence_conflict")
        now = _utcnow()
        _validate_evidence_time(command, gate, now)
        verification = EvidenceVerificationRequest(
            launch_candidate_id=candidate["launch_candidate_id"],
            candidate_sha256=candidate["candidate_sha256"],
            launch_gate_id=gate["launch_gate_id"],
            gate_code=gate["gate_code"],
            policy_sha256=gate["policy_sha256"],
            workspace_id=artifact["workspace_id"],
            tenant_id=artifact["tenant_id"],
            artifact_sha256=artifact["artifact_sha256"],
            verifier_code=command.verifier_code,
            verifier_version=command.verifier_version,
            outcome=command.outcome,
            metric_value=command.metric_value,
            metric_unit=command.metric_unit,
            environment_sha256=command.environment_sha256,
            provenance_sha256=command.provenance_sha256,
            started_at=_as_utc(command.started_at),
            finished_at=_as_utc(command.finished_at),
            envelope_sha256="",
        )
        verification = replace(
            verification, envelope_sha256=evidence_envelope_sha256(verification)
        )
        connection.commit()
        return actor, verification
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("launch.unavailable") from None
    finally:
        connection.close()


def _evidence_rows(
    connection: sqlite3.Connection,
    launch_gate_id: str,
    command: EvidenceCommand,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    candidate = _candidate_row(connection, command.launch_candidate_id)
    gate = connection.execute(
        "SELECT * FROM launch_gate_policies WHERE launch_gate_id=? AND status='active'",
        (launch_gate_id,),
    ).fetchone()
    if gate is None:
        raise ApiError("launch.gate_not_found")
    artifact = connection.execute(
        """SELECT artifact.* FROM launch_candidate_artifacts artifact
            JOIN workspace_tenant_bindings binding
              ON binding.workspace_id=artifact.workspace_id
             AND binding.tenant_id=artifact.tenant_id
            JOIN workspaces workspace ON workspace.workspace_id=artifact.workspace_id
           WHERE artifact.launch_candidate_id=? AND artifact.launch_gate_id=?
             AND artifact.workspace_id=? AND artifact.artifact_sha256=?
             AND binding.status='active' AND workspace.status='active'""",
        (
            command.launch_candidate_id,
            launch_gate_id,
            command.workspace_id,
            command.artifact_sha256,
        ),
    ).fetchone()
    if artifact is None:
        raise ApiError("launch.artifact_not_found")
    if (
        command.verifier_code != gate["verifier_code"]
        or command.verifier_version != gate["verifier_version"]
    ):
        raise ApiError("launch.verifier_invalid")
    if (
        connection.execute(
            """SELECT 1 FROM launch_evidence
            WHERE launch_candidate_id=? AND launch_gate_id=?""",
            (command.launch_candidate_id, launch_gate_id),
        ).fetchone()
        is not None
    ):
        raise ApiError("launch.evidence_conflict")
    return candidate, gate, artifact


def _validate_evidence_time(command: EvidenceCommand, gate: sqlite3.Row, now: datetime) -> None:
    started = _as_utc(command.started_at)
    finished = _as_utc(command.finished_at)
    duration = finished - started
    if finished > now + timedelta(minutes=5) or finished < now - timedelta(days=8):
        raise ApiError("launch.evidence_time_invalid")
    if gate["gate_code"] == "long_stability":
        if duration < timedelta(hours=72) or duration > timedelta(hours=96):
            raise ApiError("launch.evidence_time_invalid")
        if command.outcome != "passed":
            raise ApiError("launch.evidence_conflict")
    elif duration > timedelta(hours=72):
        raise ApiError("launch.evidence_time_invalid")
    if command.metric_value is not None and not math.isfinite(command.metric_value):
        raise ApiError("launch.request_invalid")


def _validate_current_approvals(
    connection: sqlite3.Connection, candidate: sqlite3.Row, now: datetime
) -> None:
    policy_set_sha = _digest(
        [
            {
                "launch_gate_id": row["launch_gate_id"],
                "gate_code": row["gate_code"],
                "policy_sha256": row["policy_sha256"],
            }
            for row in connection.execute(
                "SELECT * FROM launch_gate_policies WHERE status='active' ORDER BY gate_code"
            ).fetchall()
        ]
    )
    if policy_set_sha != candidate["gate_policy_set_sha256"]:
        raise ApiError("launch.evidence_stale")
    evidence = connection.execute(
        """SELECT outcome,expires_at FROM launch_evidence
            WHERE launch_candidate_id=?""",
        (candidate["launch_candidate_id"],),
    ).fetchall()
    if len(evidence) != 20 or any(
        row["outcome"] != "passed" or _parse_time(row["expires_at"]) <= now for row in evidence
    ):
        raise ApiError("launch.evidence_stale")
    decisions = connection.execute(
        """SELECT * FROM launch_go_no_go_decisions
            WHERE launch_candidate_id=?""",
        (candidate["launch_candidate_id"],),
    ).fetchall()
    if len(decisions) != 6:
        raise ApiError("launch.decision_conflict")
    for decision in decisions:
        if decision["decision"] != "approved" or not _operator_has_launch_permission(
            connection,
            decision["operator_id"],
            decision["operator_revision"],
            decision["permission_code"],
        ):
            raise ApiError("launch.permission_revoked")


def _require_release_authority(
    connection: sqlite3.Connection, candidate: sqlite3.Row, actor: PlatformContext
) -> None:
    if actor.operator_id == candidate["requested_by_operator_id"]:
        raise ApiError("launch.independence_required")
    if (
        connection.execute(
            """SELECT 1 FROM launch_go_no_go_decisions
            WHERE launch_candidate_id=? AND operator_id=?""",
            (candidate["launch_candidate_id"], actor.operator_id),
        ).fetchone()
        is not None
    ):
        raise ApiError("launch.independence_required")


def _operator_has_launch_permission(
    connection: sqlite3.Connection,
    operator_id: str,
    revision: int,
    permission_code: str,
) -> bool:
    return (
        connection.execute(
            """SELECT 1 FROM platform_operators operator
            JOIN platform_operator_role_bindings binding
              ON binding.operator_id=operator.operator_id AND binding.status='active'
            JOIN platform_launch_role_permissions role_permission
              ON role_permission.role_code=binding.role_code
           WHERE operator.operator_id=? AND operator.status='active'
             AND operator.revision=? AND role_permission.permission_code=? LIMIT 1""",
            (operator_id, revision, permission_code),
        ).fetchone()
        is not None
    )


def _gate_summaries(connection: sqlite3.Connection) -> tuple[LaunchGateSummary, ...]:
    return tuple(
        LaunchGateSummary(
            launch_gate_id=row["launch_gate_id"],
            gate_code=row["gate_code"],
            policy_version=row["policy_version"],
            verifier_code=row["verifier_code"],
            verifier_version=row["verifier_version"],
            threshold_rule=row["threshold_rule"],
            freshness_seconds=row["freshness_seconds"],
            blocking_severity=row["blocking_severity"],
            policy_sha256=row["policy_sha256"],
        )
        for row in connection.execute(
            "SELECT * FROM launch_gate_policies WHERE status='active' ORDER BY gate_code"
        ).fetchall()
    )


def _candidate_summary(connection: sqlite3.Connection, candidate_id: str) -> CandidateSummary:
    row = _candidate_row(connection, candidate_id)
    evidence = tuple(
        _evidence_summary(item)
        for item in connection.execute(
            """SELECT evidence.*,gate.gate_code FROM launch_evidence evidence
                JOIN launch_gate_policies gate ON gate.launch_gate_id=evidence.launch_gate_id
               WHERE evidence.launch_candidate_id=? ORDER BY gate.gate_code""",
            (candidate_id,),
        ).fetchall()
    )
    decisions = tuple(
        DecisionSummary(
            go_no_go_decision_id=item["go_no_go_decision_id"],
            actor_kind=item["actor_kind"],
            operator_id=item["operator_id"],
            operator_revision=item["operator_revision"],
            decision=item["decision"],
            reason_code=item["reason_code"],
            decision_sha256=item["decision_sha256"],
            signature_sha256=item["signature_sha256"],
            decided_at=_parse_time(item["decided_at"]),
        )
        for item in connection.execute(
            """SELECT * FROM launch_go_no_go_decisions
                WHERE launch_candidate_id=? ORDER BY actor_kind""",
            (candidate_id,),
        ).fetchall()
    )
    waves = tuple(
        _wave_summary(item)
        for item in connection.execute(
            """SELECT * FROM launch_waves
                WHERE launch_candidate_id=? ORDER BY wave_sequence""",
            (candidate_id,),
        ).fetchall()
    )
    rollbacks = tuple(
        _rollback_summary(item)
        for item in connection.execute(
            """SELECT * FROM launch_rollbacks
                WHERE launch_candidate_id=? ORDER BY requested_at""",
            (candidate_id,),
        ).fetchall()
    )
    review_row = connection.execute(
        "SELECT * FROM post_launch_reviews WHERE launch_candidate_id=?", (candidate_id,)
    ).fetchone()
    review = (
        ReviewSummary(
            post_launch_review_id=review_row["post_launch_review_id"],
            workspace_id=review_row["workspace_id"],
            summary_sha256=review_row["summary_sha256"],
            action_items_sha256=review_row["action_items_sha256"],
            conclusion=review_row["conclusion"],
            reviewed_at=_parse_time(review_row["reviewed_at"]),
        )
        if review_row is not None
        else None
    )
    return CandidateSummary(
        launch_candidate_id=row["launch_candidate_id"],
        candidate_sha256=row["candidate_sha256"],
        code_tree_sha256=row["code_tree_sha256"],
        migration_set_sha256=row["migration_set_sha256"],
        contract_set_sha256=row["contract_set_sha256"],
        artifact_set_sha256=row["artifact_set_sha256"],
        test_manifest_sha256=row["test_manifest_sha256"],
        gate_policy_set_sha256=row["gate_policy_set_sha256"],
        generation=row["generation"],
        status=row["status"],
        revision=row["revision"],
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        completed_at=_optional_time(row["completed_at"]),
        evidence=evidence,
        decisions=decisions,
        waves=waves,
        rollbacks=rollbacks,
        review=review,
    )


def _evidence_summary(row: sqlite3.Row) -> LaunchEvidenceSummary:
    return LaunchEvidenceSummary(
        launch_evidence_id=row["launch_evidence_id"],
        launch_gate_id=row["launch_gate_id"],
        gate_code=row["gate_code"],
        workspace_id=row["workspace_id"],
        artifact_sha256=row["artifact_sha256"],
        verifier_code=row["verifier_code"],
        verifier_version=row["verifier_version"],
        envelope_sha256=row["envelope_sha256"],
        outcome=row["outcome"],
        metric_value=row["metric_value"],
        metric_unit=row["metric_unit"],
        environment_sha256=row["environment_sha256"],
        provenance_sha256=row["provenance_sha256"],
        started_at=_parse_time(row["started_at"]),
        finished_at=_parse_time(row["finished_at"]),
        expires_at=_parse_time(row["expires_at"]),
        created_at=_parse_time(row["created_at"]),
    )


def _wave_summary(row: sqlite3.Row) -> WaveSummary:
    return WaveSummary(
        launch_wave_id=row["launch_wave_id"],
        wave_kind=row["wave_kind"],
        wave_sequence=row["wave_sequence"],
        workspace_id=row["workspace_id"],
        cohort_manifest_sha256=row["cohort_manifest_sha256"],
        feature_flag_generation=row["feature_flag_generation"],
        migration_plan_sha256=row["migration_plan_sha256"],
        rollback_plan_sha256=row["rollback_plan_sha256"],
        observation_seconds=row["observation_seconds"],
        status=row["status"],
        observation_deadline_at=_optional_time(row["observation_deadline_at"]),
        health_watermark_sha256=row["health_watermark_sha256"],
        revision=row["revision"],
        started_at=_optional_time(row["started_at"]),
        completed_at=_optional_time(row["completed_at"]),
    )


def _rollback_summary(row: sqlite3.Row) -> RollbackSummary:
    return RollbackSummary(
        launch_rollback_id=row["launch_rollback_id"],
        workspace_id=row["workspace_id"],
        from_wave_kind=row["from_wave_kind"],
        to_wave_kind=row["to_wave_kind"],
        trigger_code=row["trigger_code"],
        trigger_sha256=row["trigger_sha256"],
        checkpoint_sha256=row["checkpoint_sha256"],
        job_receipt_sha256=row["job_receipt_sha256"],
        verification_sha256=row["verification_sha256"],
        status=row["status"],
        revision=row["revision"],
        requested_at=_parse_time(row["requested_at"]),
        verified_at=_optional_time(row["verified_at"]),
    )


def _evidence_response(
    connection: sqlite3.Connection,
    evidence_id: str,
    *,
    request_id: str,
    trace_id: str,
    replay: bool = False,
) -> EvidenceResponse:
    row = connection.execute(
        """SELECT evidence.*,gate.gate_code FROM launch_evidence evidence
            JOIN launch_gate_policies gate ON gate.launch_gate_id=evidence.launch_gate_id
           WHERE evidence.launch_evidence_id=?""",
        (evidence_id,),
    ).fetchone()
    if row is None:
        raise ApiError("launch.evidence_conflict")
    return EvidenceResponse(
        evidence=_evidence_summary(row),
        candidate=_candidate_summary(connection, row["launch_candidate_id"]),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _rollback_response(
    connection: sqlite3.Connection,
    rollback_id: str,
    *,
    request_id: str,
    trace_id: str,
    replay: bool = False,
) -> RollbackResponse:
    row = connection.execute(
        "SELECT * FROM launch_rollbacks WHERE launch_rollback_id=?", (rollback_id,)
    ).fetchone()
    if row is None:
        raise ApiError("launch.rollback_not_found")
    return RollbackResponse(
        rollback=_rollback_summary(row),
        candidate=_candidate_summary(connection, row["launch_candidate_id"]),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _candidate_row(connection: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
    if _CANDIDATE_ID.fullmatch(candidate_id) is None:
        raise ApiError("launch.candidate_not_found")
    row = connection.execute(
        "SELECT * FROM launch_candidates WHERE launch_candidate_id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise ApiError("launch.candidate_not_found")
    return row


def _candidate_owner(connection: sqlite3.Connection, candidate_id: str) -> tuple[str, str]:
    row = connection.execute(
        """SELECT workspace_id,tenant_id FROM launch_candidate_artifacts
            WHERE launch_candidate_id=? ORDER BY launch_gate_id LIMIT 1""",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise ApiError("launch.artifact_not_found")
    return row["workspace_id"], row["tenant_id"]


def _workspace_owner(connection: sqlite3.Connection, workspace_id: str) -> tuple[str, str]:
    row = connection.execute(
        """SELECT binding.workspace_id,binding.tenant_id
              FROM workspace_tenant_bindings binding
              JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
             WHERE binding.workspace_id=? AND binding.status='active'
               AND workspace.status='active'""",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise ApiError("launch.workspace_not_found")
    return row["workspace_id"], row["tenant_id"]


def _platform_actor(
    connection: sqlite3.Connection,
    config: AppConfig,
    session_token: str,
    csrf_token: str | None,
    permission: str,
) -> PlatformContext:
    actor = authenticate_platform_session_in_transaction(
        connection,
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        touch=True,
    )
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")
    return actor


def _command_replay(
    connection: sqlite3.Connection,
    operator_id: str,
    action_code: str,
    key_sha: str,
    request_sha: str,
) -> str | None:
    row = connection.execute(
        """SELECT request_sha256,result_object_id FROM launch_action_commands
            WHERE operator_id=? AND action_code=? AND idempotency_key_sha256=?""",
        (operator_id, action_code, key_sha),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(row["request_sha256"], request_sha):
        raise ApiError("launch.idempotency_conflict")
    return row["result_object_id"]


def _record_action(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    owner: tuple[str, str],
    action_code: str,
    key_sha: str,
    request_sha: str,
    result_id: str,
    result_revision: int,
) -> None:
    connection.execute(
        """INSERT INTO launch_action_commands(
          launch_action_command_id,operator_id,workspace_id,tenant_id,action_code,
          idempotency_key_sha256,request_sha256,result_object_id,result_revision,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("lac_"),
            actor.operator_id,
            owner[0],
            owner[1],
            action_code,
            key_sha,
            request_sha,
            result_id,
            result_revision,
            _db_time(_utcnow()),
        ),
    )


def _emit_event(
    connection: sqlite3.Connection,
    candidate_id: str,
    workspace_id: str,
    tenant_id: str,
    event_type: str,
    object_type: str,
    object_id: str,
    payload_sha256: str,
    now: datetime,
) -> None:
    event_id = _id("lev_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO launch_events(
          launch_event_id,launch_candidate_id,workspace_id,tenant_id,event_type,
          object_type,object_id,payload_sha256,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            candidate_id,
            workspace_id,
            tenant_id,
            event_type,
            object_type,
            object_id,
            payload_sha256,
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO launch_event_outbox(
          launch_event_outbox_id,launch_event_id,workspace_id,tenant_id,status,
          attempt_count,next_attempt_at,delivered_at,created_at,updated_at)
        VALUES(?,?,?,?,'pending',0,?,NULL,?,?)""",
        (_id("leo_"), event_id, workspace_id, tenant_id, stamp, stamp, stamp),
    )


def _launch_action_code(operation: str) -> str:
    return {
        "create": "candidate.create",
        "decide": "decision.sign",
        "start_wave": "wave.start",
        "pause_wave": "wave.pause",
        "resume_wave": "wave.resume",
        "complete_wave": "wave.complete",
        "review": "candidate.review",
    }[operation]


def _require_schema(connection: sqlite3.Connection) -> None:
    required = {
        "launch_gate_policies",
        "launch_candidates",
        "launch_candidate_artifacts",
        "launch_evidence",
        "launch_go_no_go_decisions",
        "launch_waves",
        "launch_rollbacks",
        "post_launch_reviews",
        "launch_events",
        "launch_event_outbox",
        "launch_action_commands",
        "launch_scheduler_state",
    }
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ).fetchall()
    }
    applied = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version='0130' AND status='applied'"
    ).fetchone()
    if applied is None or not required <= tables:
        raise ApiError("launch.unavailable")
    if connection.execute("SELECT COUNT(*) FROM launch_gate_policies").fetchone()[0] != 20:
        raise ApiError("launch.gate_registry_invalid")


def _connect(config: AppConfig) -> sqlite3.Connection:
    connection = connect_database(config.database_url)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ApiError("launch.evidence_time_invalid")
    return value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime:
    if value is None:
        raise ValueError("timestamp required")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(UTC)


def _optional_time(value: str | None) -> datetime | None:
    return _parse_time(value) if value is not None else None


def _next_stamp(current: str, now: datetime) -> str:
    current_time = _parse_time(current)
    return _db_time(max(now, current_time + timedelta(milliseconds=1)))


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


__all__ = [
    "EvidenceVerificationRequest",
    "EvidenceVerifier",
    "apply_launch_command",
    "apply_launch_rollback",
    "list_launch_gates",
    "process_launch_governance_once",
    "submit_launch_evidence",
]
