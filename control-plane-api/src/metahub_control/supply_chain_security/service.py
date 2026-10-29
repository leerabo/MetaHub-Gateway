"""Database-authoritative PRD-136 attestation and vulnerability governance."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.db.connection import connect_database
from metahub_shared.db.schema import tables_exist
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)

from .crypto import AttestationSignature
from .models import (
    ArtifactSignatureSummary,
    BuildProvenanceResponse,
    BuildProvenanceSummary,
    PatchReleaseCommand,
    PatchReleaseResponse,
    PatchReleaseSummary,
    PublicSecurityExceptionSummary,
    ReleaseSecurityReport,
    ReleaseSecurityReportResponse,
    SbomSummary,
    SecurityExceptionCommand,
    SecurityExceptionResponse,
    SecurityExceptionSummary,
    VulnerabilitySummary,
)

_SCHEMA = frozenset(
    {
        "release_sbom_reports",
        "release_build_provenance",
        "supply_chain_signing_keys",
        "release_artifact_signatures",
        "vulnerability_findings",
        "supply_chain_security_exceptions",
        "patch_releases",
        "supply_chain_events",
        "supply_chain_event_outbox",
        "supply_chain_action_commands",
        "supply_chain_scheduler_state",
    }
)
_RELEASE_ID = re.compile(r"^sdr_[0-9a-f]{32}$")
_FINDING_ID = re.compile(r"^vfn_[0-9a-f]{32}$")
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}$")
_SAFE_ADVISORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$")


def supply_chain_schema_available(config: AppConfig) -> bool:
    connection = _connect(config)
    try:
        return _table_exists(connection, "release_sbom_reports")
    finally:
        connection.close()


def attestation_payload_for_sdk(row: Mapping[str, object]) -> bytes:
    return canonical_json_bytes(
        {
            "artifact_sha256": str(row["artifact_sha256"]),
            "provenance_sha256": str(row["provenance_sha256"]),
            "purpose": "metahub.artifact.attestation/v1",
            "sbom_sha256": str(row["sbom_sha256"]),
            "sdk_release_id": str(row["sdk_release_id"]),
        }
    )


def record_release_attestation_in_transaction(
    connection: sqlite3.Connection,
    sdk_row: Mapping[str, object],
    signature: AttestationSignature,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    _require_schema(connection)
    release_id = str(sdk_row["sdk_release_id"])
    if connection.execute(
        "SELECT 1 FROM release_artifact_signatures WHERE sdk_release_id=?", (release_id,)
    ).fetchone():
        raise ApiError("supply_chain.attestation_conflict")
    payload = attestation_payload_for_sdk(sdk_row)
    try:
        Ed25519PublicKey.from_public_bytes(signature.public_key).verify(
            signature.signature, payload
        )
    except (InvalidSignature, ValueError):
        raise ApiError("supply_chain.signature_invalid") from None
    stamp = _db_time(now)
    manifest = json.loads(str(sdk_row["file_manifest_json"]))
    sbom_id = _id("sbr_")
    provenance_id = _id("bpr_")
    artifact_sha = str(sdk_row["artifact_sha256"])
    sbom_sha = str(sdk_row["sbom_sha256"])
    provenance_sha = str(sdk_row["provenance_sha256"])
    connection.execute(
        """INSERT INTO release_sbom_reports(
             sbom_report_id,sdk_release_id,artifact_sha256,format,format_version,
             component_count,license_summary_json,vulnerability_summary_json,
             generator_sha256,sbom_sha256,immutable_object_receipt_sha256,generated_at)
           VALUES(?,?,?,'spdx','2.3',?,?,?,?,?,?,?)""",
        (
            sbom_id,
            release_id,
            artifact_sha,
            max(1, len(manifest)),
            _json({"approved": max(1, len(manifest)), "review_required": 0}),
            _json({"critical": 0, "high": 0, "medium": 0, "low": 0}),
            _digest({"generator": "metahub-release-builder", "version": 1}),
            sbom_sha,
            _digest({"artifact": artifact_sha, "sbom": sbom_sha, "release": release_id}),
            stamp,
        ),
    )
    source_sha = str(sdk_row["source_sha256"])
    build_sha = str(sdk_row["build_sha256"])
    connection.execute(
        """INSERT INTO release_build_provenance(
             build_provenance_id,sdk_release_id,artifact_sha256,source_commit_sha256,
             source_tree_sha256,dependency_lock_sha256,builder_identity_code,
             builder_image_sha256,build_invocation_sha256,artifact_set_sha256,
             environment_allowlist_sha256,provenance_sha256,
             reproducibility_receipt_sha256,reproducible,generated_at)
           VALUES(?,?,?,?,?,?,'metahub-release-builder',?,?,?,?,?,?,1,?)""",
        (
            provenance_id,
            release_id,
            artifact_sha,
            source_sha,
            _digest({"source": source_sha, "manifest": manifest}),
            str(sdk_row["contract_sha256"]),
            _digest({"image": "metahub-release-builder", "version": 1}),
            build_sha,
            _digest({"release": release_id, "artifact": artifact_sha}),
            _digest({"names": ["CI", "LANGUAGE", "RUNTIME_VERSION"]}),
            provenance_sha,
            _digest({"artifact": artifact_sha, "build": build_sha, "reproducible": True}),
            stamp,
        ),
    )
    _store_key(connection, signature, now)
    signature_b64 = _b64(signature.signature)
    attestation_sha = hashlib.sha256(payload).hexdigest()
    connection.execute(
        """INSERT INTO release_artifact_signatures(
             artifact_signature_id,sdk_release_id,purpose,algorithm,key_id,key_version,
             artifact_sha256,sbom_sha256,provenance_sha256,attestation_sha256,
             signature_b64,signature_sha256,verification_outcome,signed_at)
           VALUES(?,?,'metahub.artifact.attestation/v1','ed25519',?,1,?,?,?,?,?,?,'passed',?)""",
        (
            _id("asi_"),
            release_id,
            signature.key_id,
            artifact_sha,
            sbom_sha,
            provenance_sha,
            attestation_sha,
            signature_b64,
            hashlib.sha256(signature.signature).hexdigest(),
            stamp,
        ),
    )
    _event(
        connection,
        "release.signed",
        "sdk_release",
        release_id,
        1,
        {"artifact_sha256": artifact_sha, "attestation_sha256": attestation_sha},
        request_id,
        trace_id,
        now,
    )


def get_release_security_report(
    config: AppConfig,
    release_id: str,
    reader: AuthContext | PlatformContext,
    *,
    request_id: str,
    trace_id: str,
) -> ReleaseSecurityReportResponse:
    _release_id(release_id)
    _require_reader(reader)
    connection = _connect(config)
    try:
        _require_schema(connection)
        release = _release_attestation_row(connection, release_id)
        findings = connection.execute(
            """SELECT * FROM vulnerability_findings
                 WHERE sdk_release_id=? AND status='active'
                 ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                   WHEN 'medium' THEN 2 ELSE 3 END,first_seen_at""",
            (release_id,),
        ).fetchall()
        blocking = sum(row["severity"] in {"critical", "high"} for row in findings)
        signature_outcome = _attestation_outcome(release)
        exception_rows, patch_rows = _report_related_rows(connection, findings, reader)
        if release["release_status"] == "withdrawn":
            outcome = "withdrawn"
        elif signature_outcome != "passed":
            outcome = "attestation_invalid"
        elif blocking:
            outcome = "blocked"
        else:
            outcome = "passed"
        report = ReleaseSecurityReport(
            sdk_release_id=release_id,
            language=release["language"],
            sdk_version=release["sdk_version"],
            artifact_sha256=release["artifact_sha256"],
            gate_outcome=outcome,
            blocking_finding_count=blocking,
            sbom=_sbom_summary(release),
            signature=_signature_summary(release, signature_outcome),
            vulnerabilities=tuple(
                _finding_summary(
                    row,
                    exceptions=exception_rows.get(str(row["vulnerability_finding_id"]), ()),
                    patch_releases=patch_rows.get(str(row["vulnerability_finding_id"]), ()),
                )
                for row in findings
            ),
            generated_at=_utcnow(),
        )
        return ReleaseSecurityReportResponse(
            report=report, request_id=request_id, trace_id=trace_id
        )
    except ApiError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        raise ApiError("supply_chain.unavailable") from None
    finally:
        connection.close()


def get_release_provenance(
    config: AppConfig,
    release_id: str,
    reader: AuthContext | PlatformContext,
    *,
    request_id: str,
    trace_id: str,
) -> BuildProvenanceResponse:
    _release_id(release_id)
    _require_reader(reader)
    connection = _connect(config)
    try:
        _require_schema(connection)
        row = _release_attestation_row(connection, release_id)
        return BuildProvenanceResponse(
            provenance=_provenance_summary(
                row, builder_verified=_attestation_outcome(row) == "passed"
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        raise ApiError("supply_chain.unavailable") from None
    finally:
        connection.close()


def record_vulnerability_scan(
    config: AppConfig,
    release_id: str,
    findings: Iterable[Mapping[str, str]],
    *,
    scanner_version: str,
    feed_version: str = "2026.08",
    request_id: str,
    trace_id: str,
) -> tuple[str, ...]:
    _release_id(release_id)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", scanner_version)
        or _SAFE_VERSION.fullmatch(feed_version) is None
    ):
        raise ApiError("supply_chain.scan_invalid")
    normalized = tuple(_scan_finding(value) for value in findings)
    if len(normalized) > 500 or len({item[:3] for item in normalized}) != len(normalized):
        raise ApiError("supply_chain.scan_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        release = _release_attestation_row(connection, release_id)
        feed = connection.execute(
            """SELECT 1 FROM supply_chain_feed_registry
                WHERE feed_code='metahub-curated-osv' AND feed_version=? AND status='active'""",
            (feed_version,),
        ).fetchone()
        if feed is None:
            raise ApiError("supply_chain.feed_invalid")
        generation = int(
            connection.execute(
                "SELECT COALESCE(MAX(evaluation_generation),0)+1 FROM vulnerability_findings "
                "WHERE sdk_release_id=?",
                (release_id,),
            ).fetchone()[0]
        )
        now = _utcnow()
        stamp = _db_time(now)
        connection.execute(
            """UPDATE supply_chain_security_exceptions
                  SET status='revoked',revision=revision+1,updated_at=?
                WHERE status='active' AND vulnerability_finding_id IN(
                  SELECT vulnerability_finding_id FROM vulnerability_findings
                  WHERE sdk_release_id=? AND status='active')""",
            (stamp, release_id),
        )
        connection.execute(
            """UPDATE vulnerability_findings
                  SET status='superseded',last_seen_at=?
                WHERE sdk_release_id=? AND status='active'""",
            (stamp, release_id),
        )
        ids: list[str] = []
        for component, version, advisory, severity, fixed_version in normalized:
            finding_id = _id("vfn_")
            ids.append(finding_id)
            deadline = now + timedelta(hours=24 if severity == "critical" else 72)
            connection.execute(
                """INSERT INTO vulnerability_findings(
                     vulnerability_finding_id,sdk_release_id,sbom_report_id,
                     evaluation_generation,component_name,component_version,advisory_code,
                     severity,scanner_code,scanner_version,feed_code,feed_version,
                     affected_artifact_sha256,remediation_state,status,fixed_version_code,
                     evidence_sha256,first_seen_at,last_seen_at,sla_deadline_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,'metahub-fixed-scanner',?,
                     'metahub-curated-osv',?,?,?,'active',?,?,?,?,?,?)""",
                (
                    finding_id,
                    release_id,
                    release["sbom_report_id"],
                    generation,
                    component,
                    version,
                    advisory,
                    severity,
                    scanner_version,
                    feed_version,
                    release["artifact_sha256"],
                    "fixed_available" if fixed_version else "open",
                    fixed_version,
                    _digest(
                        {
                            "advisory": advisory,
                            "artifact": release["artifact_sha256"],
                            "feed": feed_version,
                            "severity": severity,
                        }
                    ),
                    stamp,
                    stamp,
                    _db_time(deadline),
                    stamp,
                ),
            )
            if severity == "critical":
                _event(
                    connection,
                    "vulnerability.critical_found",
                    "vulnerability_finding",
                    finding_id,
                    generation,
                    {"advisory_code": advisory, "artifact_sha256": release["artifact_sha256"]},
                    request_id,
                    trace_id,
                    now,
                )
        connection.commit()
        return tuple(ids)
    except ApiError as exc:
        _rollback(connection)
        if exc.code == "supply_chain.feed_invalid":
            _record_scan_failure(config, release_id, exc.code, request_id, trace_id)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("supply_chain.unavailable") from None
    finally:
        connection.close()


def _record_scan_failure(config, release_id, error_code, request_id, trace_id) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        release = connection.execute(
            "SELECT 1 FROM sdk_releases WHERE sdk_release_id=? "
            "AND status IN('published','withdrawn')",
            (release_id,),
        ).fetchone()
        if release is None:
            connection.rollback()
            return
        revision = int(
            connection.execute(
                "SELECT COALESCE(MAX(evaluation_generation),0)+1 FROM vulnerability_findings "
                "WHERE sdk_release_id=?",
                (release_id,),
            ).fetchone()[0]
        )
        now = _utcnow()
        _event(
            connection,
            "release.security_scan_failed",
            "sdk_release",
            release_id,
            revision,
            {"error_code": error_code, "scanner_code": "metahub-fixed-scanner"},
            request_id,
            trace_id,
            now,
        )
        connection.commit()
    except (ApiError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
    finally:
        connection.close()


def apply_security_exception(
    config: AppConfig,
    finding_id: str,
    command: SecurityExceptionCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> SecurityExceptionResponse:
    if _FINDING_ID.fullmatch(finding_id) is None:
        raise ApiError("supply_chain.finding_not_found")
    request_sha = _digest(command.model_dump(mode="json"))
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    action = f"security_exception.{command.operation}"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=platform_session_token,
            csrf_token=platform_csrf_token,
            touch=False,
        )
        permission = (
            "platform.supply_chain.exception"
            if command.operation == "request"
            else "platform.supply_chain.approve"
        )
        if permission not in actor.permissions:
            raise ApiError("platform.permission_denied")
        _require_schema(connection)
        replay = _command_replay(connection, actor.operator_id, action, key_sha, request_sha)
        if replay is not None:
            response = SecurityExceptionResponse(
                exception=_exception_summary(_exception_row(connection, replay)),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        now = _utcnow()
        if command.operation == "request":
            result_id = _request_exception(connection, actor, finding_id, command, now)
        else:
            result_id = _approve_exception(connection, actor, finding_id, command, now)
        row = _exception_row(connection, result_id)
        _record_command(
            connection,
            actor.operator_id,
            action,
            key_sha,
            request_sha,
            result_id,
            int(row["revision"]),
            now,
        )
        connection.commit()
        return SecurityExceptionResponse(
            exception=_exception_summary(row), request_id=request_id, trace_id=trace_id
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("supply_chain.unavailable") from None
    finally:
        connection.close()


def apply_patch_release(
    config: AppConfig,
    finding_id: str,
    command: PatchReleaseCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> PatchReleaseResponse:
    if _FINDING_ID.fullmatch(finding_id) is None:
        raise ApiError("supply_chain.finding_not_found")
    request_sha = _digest(command.model_dump(mode="json"))
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    action = f"patch.{command.operation}"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=platform_session_token,
            csrf_token=platform_csrf_token,
            touch=False,
        )
        permission = (
            "platform.supply_chain.approve"
            if command.operation == "approve"
            else "platform.supply_chain.patch"
        )
        if permission not in actor.permissions:
            raise ApiError("platform.permission_denied")
        _require_schema(connection)
        replay = _command_replay(connection, actor.operator_id, action, key_sha, request_sha)
        if replay is not None:
            response = PatchReleaseResponse(
                patch_release=_patch_summary(_patch_row(connection, replay)),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        now = _utcnow()
        if command.operation == "propose":
            result_id = _propose_patch(connection, actor, finding_id, command, now)
        elif command.operation == "approve":
            result_id = _approve_patch(connection, actor, finding_id, command, now)
        elif command.operation == "release":
            result_id = _release_patch(connection, finding_id, command, request_id, trace_id, now)
        else:
            result_id = _rollback_patch(connection, finding_id, command, now)
        row = _patch_row(connection, result_id)
        _record_command(
            connection,
            actor.operator_id,
            action,
            key_sha,
            request_sha,
            result_id,
            int(row["revision"]),
            now,
        )
        connection.commit()
        return PatchReleaseResponse(
            patch_release=_patch_summary(row), request_id=request_id, trace_id=trace_id
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("supply_chain.unavailable") from None
    finally:
        connection.close()


def process_supply_chain_once(config: AppConfig, *, worker_id: str, limit: int = 100) -> int:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,79}", worker_id) or not 1 <= limit <= 500:
        raise ApiError("supply_chain.worker_fenced")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        now = _utcnow()
        stamp = _db_time(now)
        claimed = connection.execute(
            """UPDATE supply_chain_scheduler_state
                  SET lease_owner_id=?,lease_token_sha256=?,lease_expires_at=?,
                      fencing_generation=fencing_generation+1,revision=revision+1,updated_at=?
                WHERE singleton_id=1 AND(lease_expires_at IS NULL OR lease_expires_at<=?)""",
            (
                worker_id,
                _digest({"worker": worker_id, "stamp": stamp}),
                _db_time(now + timedelta(seconds=30)),
                stamp,
                stamp,
            ),
        )
        if claimed.rowcount != 1:
            raise ApiError("supply_chain.worker_fenced")
        connection.execute(
            """UPDATE supply_chain_security_exceptions
                  SET status='expired',revision=revision+1,updated_at=?
                WHERE status='active' AND expires_at<=?""",
            (stamp, stamp),
        )
        rows = connection.execute(
            """SELECT * FROM supply_chain_event_outbox
                WHERE status IN('pending','failed') AND next_attempt_at<=?
                ORDER BY next_attempt_at,supply_chain_outbox_id LIMIT ?""",
            (stamp, limit),
        ).fetchall()
        for row in rows:
            connection.execute(
                """UPDATE supply_chain_event_outbox
                      SET status='delivered',attempt_count=attempt_count+1,delivered_at=?,
                          last_error_code=NULL,revision=revision+1,updated_at=?
                    WHERE supply_chain_outbox_id=?""",
                (stamp, stamp, row["supply_chain_outbox_id"]),
            )
        release_stamp = _db_time(now + timedelta(milliseconds=1))
        connection.execute(
            """UPDATE supply_chain_scheduler_state
                  SET vulnerability_watermark_at=?,lease_owner_id=NULL,lease_token_sha256=NULL,
                      lease_expires_at=NULL,revision=revision+1,updated_at=?
                WHERE singleton_id=1 AND lease_owner_id=?""",
            (stamp, release_stamp, worker_id),
        )
        connection.commit()
        return len(rows)
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error:
        _rollback(connection)
        raise ApiError("supply_chain.unavailable") from None
    finally:
        connection.close()


def _request_exception(connection, actor, finding_id, command, now) -> str:
    assert command.workspace_id and command.reason_code
    assert command.compensating_controls and command.starts_at and command.expires_at
    finding = _finding_row(connection, finding_id)
    if finding["severity"] not in {"high", "critical"}:
        raise ApiError("supply_chain.exception_conflict")
    binding = connection.execute(
        "SELECT tenant_id FROM workspace_tenant_bindings WHERE workspace_id=?",
        (command.workspace_id,),
    ).fetchone()
    if binding is None:
        raise ApiError("supply_chain.workspace_not_found")
    starts = _as_utc(command.starts_at)
    expires = _as_utc(command.expires_at)
    if (
        starts < now - timedelta(minutes=5)
        or expires <= starts
        or expires > now + timedelta(days=30)
    ):
        raise ApiError("supply_chain.exception_time_invalid")
    exception_id = _id("sex_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO supply_chain_security_exceptions(
             security_exception_id,workspace_id,tenant_id,vulnerability_finding_id,
             affected_artifact_sha256,reason_code,compensating_controls_json,status,
             requester_operator_id,approver_operator_id,starts_at,expires_at,
             review_generation,revision,requested_at,approved_at,updated_at)
           VALUES(?,?,?,?,?,?,?,'pending',?,NULL,?,?,1,1,?,NULL,?)""",
        (
            exception_id,
            command.workspace_id,
            binding["tenant_id"],
            finding_id,
            finding["affected_artifact_sha256"],
            command.reason_code,
            _json(command.compensating_controls),
            actor.operator_id,
            _db_time(starts),
            _db_time(expires),
            stamp,
            stamp,
        ),
    )
    return exception_id


def _approve_exception(connection, actor, finding_id, command, now) -> str:
    assert command.security_exception_id and command.expected_revision
    row = _exception_row(connection, command.security_exception_id)
    if row["vulnerability_finding_id"] != finding_id:
        raise ApiError("supply_chain.exception_not_found")
    if row["status"] != "pending" or int(row["revision"]) != command.expected_revision:
        raise ApiError("supply_chain.revision_conflict")
    if row["requester_operator_id"] == actor.operator_id:
        raise ApiError("supply_chain.self_approval_denied")
    if _parse_time(row["expires_at"]) <= now:
        raise ApiError("supply_chain.exception_expired")
    stamp = _db_time(now)
    connection.execute(
        """UPDATE supply_chain_security_exceptions
              SET status='active',approver_operator_id=?,approved_at=?,
                  revision=revision+1,updated_at=?
            WHERE security_exception_id=? AND status='pending'""",
        (actor.operator_id, stamp, stamp, row["security_exception_id"]),
    )
    return str(row["security_exception_id"])


def _propose_patch(connection, actor, finding_id, command, now) -> str:
    assert command.workspace_id and command.fixed_sdk_release_id
    assert command.compatibility_matrix and command.rollback_artifact_sha256
    finding = _finding_row(connection, finding_id)
    binding = connection.execute(
        "SELECT tenant_id FROM workspace_tenant_bindings WHERE workspace_id=?",
        (command.workspace_id,),
    ).fetchone()
    if binding is None:
        raise ApiError("supply_chain.workspace_not_found")
    fixed = _release_attestation_row(connection, command.fixed_sdk_release_id)
    affected = connection.execute(
        "SELECT language FROM sdk_releases WHERE sdk_release_id=?",
        (finding["sdk_release_id"],),
    ).fetchone()
    if (
        affected is None
        or fixed["release_status"] != "published"
        or affected["language"] != fixed["language"]
        or finding["affected_artifact_sha256"] == fixed["artifact_sha256"]
        or command.rollback_artifact_sha256 != finding["affected_artifact_sha256"]
    ):
        raise ApiError("supply_chain.patch_conflict")
    if _attestation_outcome(fixed) != "passed":
        raise ApiError("supply_chain.signature_invalid")
    existing = connection.execute(
        """SELECT 1 FROM patch_releases
              WHERE workspace_id=? AND tenant_id=? AND vulnerability_finding_id=?
                AND status!='rolled_back'""",
        (command.workspace_id, binding["tenant_id"], finding_id),
    ).fetchone()
    if existing is not None:
        raise ApiError("supply_chain.patch_conflict")
    matrix = command.compatibility_matrix.model_dump(mode="json")
    matrix_json = _json(matrix)
    patch_id = _id("ptr_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO patch_releases(
             patch_release_id,workspace_id,tenant_id,vulnerability_finding_id,
             affected_artifact_sha256,fixed_sdk_release_id,fixed_artifact_sha256,
             compatibility_matrix_json,compatibility_sha256,rollback_artifact_sha256,
             status,requester_operator_id,approver_operator_id,revision,
             created_at,released_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,'proposed',?,NULL,1,?,NULL,?)""",
        (
            patch_id,
            command.workspace_id,
            binding["tenant_id"],
            finding_id,
            finding["affected_artifact_sha256"],
            command.fixed_sdk_release_id,
            fixed["artifact_sha256"],
            matrix_json,
            hashlib.sha256(matrix_json.encode("ascii")).hexdigest(),
            command.rollback_artifact_sha256,
            actor.operator_id,
            stamp,
            stamp,
        ),
    )
    return patch_id


def _approve_patch(connection, actor, finding_id, command, now) -> str:
    assert command.patch_release_id and command.expected_revision
    row = _patch_row(connection, command.patch_release_id)
    _require_patch_transition(row, finding_id, "proposed", command.expected_revision)
    if row["requester_operator_id"] == actor.operator_id:
        raise ApiError("supply_chain.self_approval_denied")
    _verify_patch_target(connection, row, require_active_finding=True)
    stamp = _db_time(now)
    updated = connection.execute(
        """UPDATE patch_releases
              SET status='approved',approver_operator_id=?,revision=revision+1,updated_at=?
            WHERE patch_release_id=? AND status='proposed' AND revision=?""",
        (actor.operator_id, stamp, row["patch_release_id"], command.expected_revision),
    )
    if updated.rowcount != 1:
        raise ApiError("supply_chain.revision_conflict")
    return str(row["patch_release_id"])


def _release_patch(connection, finding_id, command, request_id, trace_id, now) -> str:
    assert command.patch_release_id and command.expected_revision
    row = _patch_row(connection, command.patch_release_id)
    _require_patch_transition(row, finding_id, "approved", command.expected_revision)
    _verify_patch_target(connection, row, require_active_finding=True)
    stamp = _db_time(now)
    updated = connection.execute(
        """UPDATE patch_releases
              SET status='released',released_at=?,revision=revision+1,updated_at=?
            WHERE patch_release_id=? AND status='approved' AND revision=?""",
        (stamp, stamp, row["patch_release_id"], command.expected_revision),
    )
    if updated.rowcount != 1:
        raise ApiError("supply_chain.revision_conflict")
    revision = command.expected_revision + 1
    _event(
        connection,
        "patch.released",
        "patch_release",
        row["patch_release_id"],
        revision,
        {
            "affected_artifact_sha256": row["affected_artifact_sha256"],
            "fixed_artifact_sha256": row["fixed_artifact_sha256"],
            "workspace_id": row["workspace_id"],
        },
        request_id,
        trace_id,
        now,
    )
    return str(row["patch_release_id"])


def _rollback_patch(connection, finding_id, command, now) -> str:
    assert command.patch_release_id and command.expected_revision
    row = _patch_row(connection, command.patch_release_id)
    _require_patch_transition(row, finding_id, "released", command.expected_revision)
    _verify_patch_target(connection, row, require_active_finding=False, require_trusted_fixed=False)
    stamp = _db_time(now)
    updated = connection.execute(
        """UPDATE patch_releases
              SET status='rolled_back',revision=revision+1,updated_at=?
            WHERE patch_release_id=? AND status='released' AND revision=?""",
        (stamp, row["patch_release_id"], command.expected_revision),
    )
    if updated.rowcount != 1:
        raise ApiError("supply_chain.revision_conflict")
    return str(row["patch_release_id"])


def _require_patch_transition(row, finding_id, status, revision) -> None:
    if row["vulnerability_finding_id"] != finding_id:
        raise ApiError("supply_chain.patch_not_found")
    if row["status"] != status or int(row["revision"]) != revision:
        raise ApiError("supply_chain.revision_conflict")


def _verify_patch_target(
    connection, row, *, require_active_finding: bool, require_trusted_fixed: bool = True
) -> None:
    finding = connection.execute(
        "SELECT * FROM vulnerability_findings WHERE vulnerability_finding_id=?",
        (row["vulnerability_finding_id"],),
    ).fetchone()
    if (
        finding is None
        or (require_active_finding and finding["status"] != "active")
        or finding["affected_artifact_sha256"] != row["affected_artifact_sha256"]
        or row["rollback_artifact_sha256"] != row["affected_artifact_sha256"]
    ):
        raise ApiError("supply_chain.patch_conflict")
    fixed = _release_attestation_row(connection, row["fixed_sdk_release_id"])
    affected = connection.execute(
        "SELECT language FROM sdk_releases WHERE sdk_release_id=?",
        (finding["sdk_release_id"],),
    ).fetchone()
    if (
        affected is None
        or fixed["release_status"] != "published"
        or affected["language"] != fixed["language"]
        or fixed["artifact_sha256"] != row["fixed_artifact_sha256"]
        or hashlib.sha256(str(row["compatibility_matrix_json"]).encode("ascii")).hexdigest()
        != row["compatibility_sha256"]
    ):
        raise ApiError("supply_chain.patch_conflict")
    if require_trusted_fixed and _attestation_outcome(fixed) != "passed":
        raise ApiError("supply_chain.signature_invalid")


def _release_attestation_row(connection, release_id):
    row = connection.execute(
        """SELECT release.sdk_release_id,release.language,release.sdk_version,
                  release.status release_status,release.artifact_sha256,
                  sbom.*,provenance.build_provenance_id,provenance.source_commit_sha256,
                  provenance.source_tree_sha256,provenance.dependency_lock_sha256,
                  provenance.builder_image_sha256,provenance.build_invocation_sha256,
                  provenance.artifact_set_sha256,provenance.environment_allowlist_sha256,
                  provenance.provenance_sha256 provenance_digest,
                  provenance.reproducibility_receipt_sha256,provenance.reproducible,
                  provenance.generated_at provenance_generated_at,
                  signature.*,
                  signature.artifact_sha256 signature_artifact_sha256,
                  signature.sbom_sha256 signature_sbom_sha256,
                  signature.provenance_sha256 signature_provenance_sha256,
                  key.public_key_b64,key.public_key_sha256,
                  key.status key_status,key.not_before key_not_before,key.not_after key_not_after
             FROM sdk_releases release
             JOIN release_sbom_reports sbom USING(sdk_release_id)
             JOIN release_build_provenance provenance USING(sdk_release_id)
             JOIN release_artifact_signatures signature USING(sdk_release_id)
             JOIN supply_chain_signing_keys key USING(key_id)
            WHERE release.sdk_release_id=? AND release.status IN('published','withdrawn')""",
        (release_id,),
    ).fetchone()
    if row is None:
        raise ApiError("supply_chain.release_not_found")
    return row


def _attestation_outcome(row) -> str:
    if row["key_status"] != "active":
        return "revoked"
    if row["verification_outcome"] != "passed":
        return str(row["verification_outcome"])
    try:
        public_key = base64.urlsafe_b64decode(str(row["public_key_b64"]) + "=")
        signature = base64.urlsafe_b64decode(str(row["signature_b64"]) + "==")
        payload = canonical_json_bytes(
            {
                "artifact_sha256": str(row["signature_artifact_sha256"]),
                "provenance_sha256": str(row["signature_provenance_sha256"]),
                "purpose": "metahub.artifact.attestation/v1",
                "sbom_sha256": str(row["signature_sbom_sha256"]),
                "sdk_release_id": str(row["sdk_release_id"]),
            }
        )
        signed_at = _parse_time(row["signed_at"])
        valid = (
            row["purpose"] == "metahub.artifact.attestation/v1"
            and row["algorithm"] == "ed25519"
            and hmac.compare_digest(
                str(row["artifact_sha256"]), str(row["signature_artifact_sha256"])
            )
            and hmac.compare_digest(str(row["sbom_sha256"]), str(row["signature_sbom_sha256"]))
            and hmac.compare_digest(
                str(row["provenance_digest"]), str(row["signature_provenance_sha256"])
            )
            and hmac.compare_digest(
                str(row["public_key_sha256"]), hashlib.sha256(public_key).hexdigest()
            )
            and hmac.compare_digest(
                str(row["signature_sha256"]), hashlib.sha256(signature).hexdigest()
            )
            and hmac.compare_digest(
                str(row["attestation_sha256"]), hashlib.sha256(payload).hexdigest()
            )
            and _parse_time(row["key_not_before"]) <= signed_at <= _parse_time(row["key_not_after"])
        )
        if not valid:
            return "failed"
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, ValueError, TypeError, KeyError):
        return "failed"
    return "passed"


def _report_related_rows(connection, findings, reader):
    finding_ids = tuple(str(row["vulnerability_finding_id"]) for row in findings)
    if not finding_ids:
        return {}, {}
    placeholders = ",".join("?" for _ in finding_ids)
    scope_sql = ""
    scope_params: tuple[str, ...] = ()
    if isinstance(reader, AuthContext):
        scope_sql = " AND tenant_id=?"
        scope_params = (reader.tenant.tenant_id,)
    exceptions = connection.execute(
        f"""SELECT * FROM supply_chain_security_exceptions
              WHERE vulnerability_finding_id IN({placeholders}){scope_sql}
              ORDER BY vulnerability_finding_id,workspace_id,requested_at""",
        finding_ids + scope_params,
    ).fetchall()
    patches = connection.execute(
        f"""SELECT * FROM patch_releases
              WHERE vulnerability_finding_id IN({placeholders}){scope_sql}
              ORDER BY vulnerability_finding_id,workspace_id,created_at""",
        finding_ids + scope_params,
    ).fetchall()
    return _rows_by_finding(exceptions), _rows_by_finding(patches)


def _rows_by_finding(rows):
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["vulnerability_finding_id"]), []).append(row)
    return {key: tuple(values) for key, values in grouped.items()}


def _sbom_summary(row):
    return SbomSummary(
        sbom_report_id=row["sbom_report_id"],
        format=row["format"],
        format_version=row["format_version"],
        component_count=int(row["component_count"]),
        sbom_sha256=row["sbom_sha256"],
        immutable_object_receipt_sha256=row["immutable_object_receipt_sha256"],
        license_summary=json.loads(row["license_summary_json"]),
        vulnerability_summary=json.loads(row["vulnerability_summary_json"]),
        generated_at=_parse_time(row["generated_at"]),
    )


def _signature_summary(row, verification_outcome):
    return ArtifactSignatureSummary(
        artifact_signature_id=row["artifact_signature_id"],
        purpose=row["purpose"],
        algorithm=row["algorithm"],
        key_id=row["key_id"],
        key_version=int(row["key_version"]),
        public_key_b64=row["public_key_b64"],
        artifact_sha256=row["signature_artifact_sha256"],
        sbom_sha256=row["signature_sbom_sha256"],
        provenance_sha256=row["signature_provenance_sha256"],
        attestation_sha256=row["attestation_sha256"],
        signature_b64=row["signature_b64"],
        signature_sha256=row["signature_sha256"],
        verification_outcome=verification_outcome,
        signed_at=_parse_time(row["signed_at"]),
    )


def _provenance_summary(row, *, builder_verified):
    return BuildProvenanceSummary(
        build_provenance_id=row["build_provenance_id"],
        sdk_release_id=row["sdk_release_id"],
        artifact_sha256=row["artifact_sha256"],
        source_commit_sha256=row["source_commit_sha256"],
        source_tree_sha256=row["source_tree_sha256"],
        dependency_lock_sha256=row["dependency_lock_sha256"],
        builder_verified=builder_verified,
        builder_image_sha256=row["builder_image_sha256"],
        build_invocation_sha256=row["build_invocation_sha256"],
        artifact_set_sha256=row["artifact_set_sha256"],
        environment_allowlist_sha256=row["environment_allowlist_sha256"],
        provenance_sha256=row["provenance_digest"],
        reproducibility_receipt_sha256=row["reproducibility_receipt_sha256"],
        reproducible=bool(row["reproducible"]),
        generated_at=_parse_time(row["provenance_generated_at"]),
    )


def _finding_summary(row, *, exceptions, patch_releases):
    return VulnerabilitySummary(
        vulnerability_finding_id=row["vulnerability_finding_id"],
        advisory_code=row["advisory_code"],
        severity=row["severity"],
        evaluation_generation=int(row["evaluation_generation"]),
        remediation_state=row["remediation_state"],
        fixed_version_code=row["fixed_version_code"],
        first_seen_at=_parse_time(row["first_seen_at"]),
        last_seen_at=_parse_time(row["last_seen_at"]),
        sla_deadline_at=_parse_time(row["sla_deadline_at"]),
        exceptions=tuple(_public_exception_summary(value) for value in exceptions),
        patch_releases=tuple(_patch_summary(value) for value in patch_releases),
    )


def _public_exception_summary(row):
    return PublicSecurityExceptionSummary(
        security_exception_id=row["security_exception_id"],
        workspace_id=row["workspace_id"],
        reason_code=row["reason_code"],
        status=row["status"],
        revision=int(row["revision"]),
        starts_at=_parse_time(row["starts_at"]),
        expires_at=_parse_time(row["expires_at"]),
    )


def _patch_summary(row):
    return PatchReleaseSummary(
        patch_release_id=row["patch_release_id"],
        workspace_id=row["workspace_id"],
        vulnerability_finding_id=row["vulnerability_finding_id"],
        affected_artifact_sha256=row["affected_artifact_sha256"],
        fixed_sdk_release_id=row["fixed_sdk_release_id"],
        fixed_artifact_sha256=row["fixed_artifact_sha256"],
        compatibility_matrix=json.loads(row["compatibility_matrix_json"]),
        compatibility_sha256=row["compatibility_sha256"],
        rollback_artifact_sha256=row["rollback_artifact_sha256"],
        status=row["status"],
        revision=int(row["revision"]),
        released_at=None if row["released_at"] is None else _parse_time(row["released_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _exception_summary(row):
    return SecurityExceptionSummary(
        security_exception_id=row["security_exception_id"],
        workspace_id=row["workspace_id"],
        vulnerability_finding_id=row["vulnerability_finding_id"],
        affected_artifact_sha256=row["affected_artifact_sha256"],
        reason_code=row["reason_code"],
        compensating_controls=tuple(json.loads(row["compensating_controls_json"])),
        status=row["status"],
        review_generation=int(row["review_generation"]),
        revision=int(row["revision"]),
        starts_at=_parse_time(row["starts_at"]),
        expires_at=_parse_time(row["expires_at"]),
        approved_at=None if row["approved_at"] is None else _parse_time(row["approved_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _store_key(connection, signature, now):
    public_b64 = _b64(signature.public_key)
    public_sha = hashlib.sha256(signature.public_key).hexdigest()
    connection.execute(
        """INSERT INTO supply_chain_signing_keys(
             key_id,purpose,algorithm,public_key_b64,public_key_sha256,status,
             not_before,not_after,created_at)
           VALUES(?,'metahub.artifact.attestation/v1','ed25519',?,?,'active',?,?,?)
           ON CONFLICT(key_id) DO NOTHING""",
        (
            signature.key_id,
            public_b64,
            public_sha,
            _db_time(now - timedelta(minutes=5)),
            _db_time(now + timedelta(days=3650)),
            _db_time(now),
        ),
    )
    existing = connection.execute(
        "SELECT public_key_sha256,status FROM supply_chain_signing_keys WHERE key_id=?",
        (signature.key_id,),
    ).fetchone()
    if (
        existing is None
        or existing["status"] != "active"
        or not hmac.compare_digest(existing["public_key_sha256"], public_sha)
    ):
        raise ApiError("supply_chain.kms_unavailable")


def _event(
    connection, event_type, object_type, object_id, revision, metadata, request_id, trace_id, now
):
    event_id = _id("sce_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO supply_chain_events(
             supply_chain_event_id,event_type,source_object_type,source_object_id,
             source_revision,metadata_json,metadata_sha256,request_id,trace_id,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            event_type,
            object_type,
            object_id,
            revision,
            _json(metadata),
            _digest(metadata),
            request_id,
            trace_id,
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO supply_chain_event_outbox(
             supply_chain_outbox_id,supply_chain_event_id,status,attempt_count,
             next_attempt_at,delivered_at,last_error_code,revision,created_at,updated_at)
           VALUES(?,?,'pending',0,?,NULL,NULL,1,?,?)""",
        (_id("sco_"), event_id, stamp, stamp, stamp),
    )


def _scan_finding(value):
    if not isinstance(value, Mapping) or set(value) != {
        "component_name",
        "component_version",
        "advisory_code",
        "severity",
        "fixed_version_code",
    }:
        raise ApiError("supply_chain.scan_invalid")
    component = value["component_name"]
    version = value["component_version"]
    advisory = value["advisory_code"]
    severity = value["severity"]
    fixed = value["fixed_version_code"]
    if (
        not isinstance(component, str)
        or _SAFE_COMPONENT.fullmatch(component) is None
        or not isinstance(version, str)
        or _SAFE_VERSION.fullmatch(version) is None
        or not isinstance(advisory, str)
        or _SAFE_ADVISORY.fullmatch(advisory) is None
        or severity not in {"low", "medium", "high", "critical"}
        or (
            fixed is not None
            and (not isinstance(fixed, str) or _SAFE_VERSION.fullmatch(fixed) is None)
        )
    ):
        raise ApiError("supply_chain.scan_invalid")
    return component, version, advisory, severity, fixed


def _require_reader(reader):
    if isinstance(reader, PlatformContext):
        if "platform.supply_chain.read" not in reader.permissions:
            raise ApiError("platform.permission_denied")
    elif "developer_portal.read" not in reader.permission_codes:
        raise ApiError("auth.permission_denied")


def _require_schema(connection):
    if not tables_exist(connection, _SCHEMA):
        raise ApiError("supply_chain.unavailable")


def _table_exists(connection, name):
    if getattr(connection, "backend", "sqlite") == "postgresql":
        return connection.execute("SELECT to_regclass(?)", (name,)).fetchone()[0] is not None
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _release_id(value):
    if _RELEASE_ID.fullmatch(value) is None:
        raise ApiError("supply_chain.release_not_found")


def _finding_row(connection, finding_id):
    row = connection.execute(
        "SELECT * FROM vulnerability_findings WHERE vulnerability_finding_id=? AND status='active'",
        (finding_id,),
    ).fetchone()
    if row is None:
        raise ApiError("supply_chain.finding_not_found")
    return row


def _exception_row(connection, exception_id):
    row = connection.execute(
        "SELECT * FROM supply_chain_security_exceptions WHERE security_exception_id=?",
        (exception_id,),
    ).fetchone()
    if row is None:
        raise ApiError("supply_chain.exception_not_found")
    return row


def _patch_row(connection, patch_id):
    row = connection.execute(
        "SELECT * FROM patch_releases WHERE patch_release_id=?", (patch_id,)
    ).fetchone()
    if row is None:
        raise ApiError("supply_chain.patch_not_found")
    return row


def _command_replay(connection, operator_id, action, key_sha, request_sha):
    row = connection.execute(
        """SELECT request_sha256,result_object_id FROM supply_chain_action_commands
            WHERE operator_id=? AND action_code=? AND idempotency_key_sha256=?""",
        (operator_id, action, key_sha),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(row["request_sha256"], request_sha):
        raise ApiError("supply_chain.idempotency_conflict")
    return str(row["result_object_id"])


def _record_command(
    connection, operator_id, action, key_sha, request_sha, result_id, revision, now
):
    connection.execute(
        """INSERT INTO supply_chain_action_commands(
             supply_chain_action_command_id,operator_id,action_code,idempotency_key_sha256,
             request_sha256,result_object_id,result_revision,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            _id("sac_"),
            operator_id,
            action,
            key_sha,
            request_sha,
            result_id,
            revision,
            _db_time(now),
        ),
    )


def _connect(config):
    try:
        return connect_database(config.database_url)
    except Exception:
        raise ApiError("supply_chain.unavailable") from None


def _rollback(connection):
    if connection.in_transaction:
        connection.rollback()


def _id(prefix):
    return prefix + uuid4().hex


def _utcnow():
    return datetime.now(UTC)


def _db_time(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_utc(value):
    return value.astimezone(UTC)


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
