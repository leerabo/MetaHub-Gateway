"""Approved offline bundle exchange and conflict reconciliation for PRD-130."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.db.errors import DbError
from metahub_shared.private_gateway import (
    OfflineBundleContractError,
    OfflineBundleManifest,
    OfflineDomainVersion,
    SignedOfflineBundle,
    canonical_edge_json_bytes,
    detect_offline_conflicts,
    reject_forbidden_offline_fields,
    sign_offline_bundle_manifest,
    strict_json_object,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import (
    consume_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import PlatformContext

from .offline_bundle_models import (
    OfflineBundleRequestSummary,
    OfflineBundleStateResponse,
    OfflineBundleSummary,
    OfflineConflictSummary,
    PlatformOfflineBundleItem,
    PlatformOfflineBundleListResponse,
    PlatformOfflineDecisionRequest,
    PlatformOfflineDecisionResponse,
    WorkspaceOfflineBundleRequest,
    WorkspaceOfflineResolutionRequest,
)
from .service import (
    _connect,
    _datetime,
    _db_time,
    _digest,
    _id,
    _identifier,
    _owned_deployment,
    _rollback,
    _utcnow,
    _workspace_context,
)


def request_workspace_offline_bundle(
    config: AppConfig,
    operation: str,
    payload: WorkspaceOfflineBundleRequest,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> OfflineBundleStateResponse:
    if operation not in {"export", "import"}:
        raise ApiError("edge_offline_bundle.request_invalid")
    action_code = f"edge.offline.{operation}.request"
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.offline_bundle.transfer",
        )
        deployment = _owned_deployment(connection, context, payload.manifest.deployment_id)
        _validate_manifest_owner(
            payload.manifest,
            deployment,
            expected_direction=("cloud_to_edge" if operation == "export" else "edge_to_cloud"),
        )
        reject_forbidden_offline_fields(payload.manifest.model_dump(mode="json"))
        idempotency_sha256 = _workspace_idempotency(
            config, context.account_id, action_code, idempotency_key
        )
        replay = _action_replay(
            connection, "workspace", context.account_id, action_code, idempotency_sha256
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            result = _state_response(
                connection,
                payload.manifest.deployment_id,
                request_id,
                trace_id,
                replay=True,
            )
            connection.commit()
            return result
        now_value = _utcnow()
        if now_value > payload.manifest.expires_at.astimezone(UTC):
            raise ApiError("edge_offline_bundle.expired")
        request_object_id = _id("obr_")
        now = _db_time(now_value)
        proposal = canonical_edge_json_bytes(
            {"manifest": payload.manifest.model_dump(mode="json")}
        ).decode("ascii")
        connection.execute(
            """
            INSERT INTO edge_offline_bundle_requests(
              offline_bundle_request_id,workspace_id,tenant_id,edge_deployment_id,
              operation,status,requested_by_account_id,approved_by_operator_id,
              target_offline_bundle_id,target_offline_conflict_id,proposal_json,
              reason_code,request_sha256,revision,requested_at,decided_at,updated_at)
            VALUES(?,?,?,?,?,'pending_platform',?,NULL,NULL,NULL,?,?,?,1,?,NULL,?)
            """,
            (
                request_object_id,
                deployment["workspace_id"],
                deployment["tenant_id"],
                deployment["edge_deployment_id"],
                operation,
                context.account_id,
                proposal,
                payload.reason_code,
                request_sha256,
                now,
                now,
            ),
        )
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=context,
            grant=step_up_grant,
            action_code=action_code,
            resource_type="edge_deployment",
            resource_id=payload.manifest.deployment_id,
            now=now_value,
        )
        _insert_action(
            connection,
            actor_domain="workspace",
            actor_id=context.account_id,
            action_code=action_code,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=request_object_id,
            now=now,
        )
        result = _state_response(connection, payload.manifest.deployment_id, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except OfflineBundleContractError as exc:
        _rollback(connection)
        raise ApiError(exc.code) from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_offline_bundle.state_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_offline_bundle.unavailable") from None
    finally:
        connection.close()


def request_workspace_offline_resolution(
    config: AppConfig,
    conflict_id: str,
    payload: WorkspaceOfflineResolutionRequest,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> OfflineBundleStateResponse:
    action_code = "edge.offline.resolve.request"
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.offline_conflict.resolve",
        )
        conflict = _conflict_row(connection, conflict_id)
        deployment = _owned_deployment(connection, context, str(conflict["edge_deployment_id"]))
        if _resolution_row(connection, conflict_id) is not None:
            raise ApiError("edge_offline_bundle.conflict_resolved")
        _validate_manifest_owner(payload.manifest, deployment, expected_direction="cloud_to_edge")
        version = _domain_version(payload.manifest, str(conflict["domain"]))
        maximum_generation = max(
            int(conflict["cloud_generation"]), int(conflict["local_generation"])
        )
        if (
            payload.expected_current_generation != maximum_generation
            or version.generation <= maximum_generation
            or (
                payload.resolution == "keep_cloud"
                and not hmac.compare_digest(version.value_sha256, str(conflict["cloud_sha256"]))
            )
            or (
                payload.resolution == "keep_local"
                and not hmac.compare_digest(version.value_sha256, str(conflict["local_sha256"]))
            )
        ):
            raise ApiError("edge_offline_bundle.resolution_conflict")
        idempotency_sha256 = _workspace_idempotency(
            config, context.account_id, action_code, idempotency_key
        )
        replay = _action_replay(
            connection, "workspace", context.account_id, action_code, idempotency_sha256
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            result = _state_response(
                connection,
                str(deployment["edge_deployment_id"]),
                request_id,
                trace_id,
                replay=True,
            )
            connection.commit()
            return result
        now_value = _utcnow()
        if now_value > payload.manifest.expires_at.astimezone(UTC):
            raise ApiError("edge_offline_bundle.expired")
        request_object_id = _id("obr_")
        now = _db_time(now_value)
        proposal = canonical_edge_json_bytes(
            {
                "manifest": payload.manifest.model_dump(mode="json"),
                "resolution": payload.resolution,
            }
        ).decode("ascii")
        connection.execute(
            """
            INSERT INTO edge_offline_bundle_requests(
              offline_bundle_request_id,workspace_id,tenant_id,edge_deployment_id,
              operation,status,requested_by_account_id,approved_by_operator_id,
              target_offline_bundle_id,target_offline_conflict_id,proposal_json,
              reason_code,request_sha256,revision,requested_at,decided_at,updated_at)
            VALUES(?,?,?,?,'resolve','pending_platform',?,NULL,NULL,?,?,?,?,1,?,NULL,?)
            """,
            (
                request_object_id,
                deployment["workspace_id"],
                deployment["tenant_id"],
                deployment["edge_deployment_id"],
                context.account_id,
                conflict_id,
                proposal,
                payload.reason_code,
                request_sha256,
                now,
                now,
            ),
        )
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=context,
            grant=step_up_grant,
            action_code=action_code,
            resource_type="edge_offline_conflict",
            resource_id=conflict_id,
            now=now_value,
        )
        _insert_action(
            connection,
            actor_domain="workspace",
            actor_id=context.account_id,
            action_code=action_code,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=request_object_id,
            now=now,
        )
        result = _state_response(
            connection, str(deployment["edge_deployment_id"]), request_id, trace_id
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except OfflineBundleContractError as exc:
        _rollback(connection)
        raise ApiError(exc.code) from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_offline_bundle.state_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_offline_bundle.unavailable") from None
    finally:
        connection.close()


def get_workspace_offline_state(
    config: AppConfig,
    deployment_id: str,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> OfflineBundleStateResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            permission="edge.offline_bundle.read",
        )
        _owned_deployment(connection, context, deployment_id)
        result = _state_response(connection, deployment_id, request_id, trace_id)
        connection.rollback()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_offline_bundle.unavailable") from None
    finally:
        connection.close()


def decide_platform_offline_request(
    config: AppConfig,
    context: PlatformContext,
    offline_request_id: str,
    payload: PlatformOfflineDecisionRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> PlatformOfflineDecisionResponse:
    _require_platform(context, "platform.edge_offline_bundle.manage")
    action_code = "edge.offline.request.approve"
    request_sha256 = _digest(
        {"offline_request_id": offline_request_id, **payload.model_dump(mode="json")}
    )
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, action_code, idempotency_key
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _action_replay(
            connection, "platform", context.operator_id, action_code, idempotency_sha256
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            request_row = _request_row(connection, str(replay["result_object_id"]))
            signed = (
                _signed_for_request(config, request_row) if payload.action == "approve" else None
            )
            result = PlatformOfflineDecisionResponse(
                request=_request_summary(request_row),
                signed_bundle=signed,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
                idempotent_replay=True,
            )
            connection.commit()
            return result
        request_row = _request_row(connection, offline_request_id)
        if (
            str(request_row["status"]) != "pending_platform"
            or int(request_row["revision"]) != payload.expected_revision
            or hmac.compare_digest(str(request_row["requested_by_account_id"]), context.account_id)
        ):
            raise ApiError("edge_offline_bundle.approval_conflict")
        now_value = _utcnow()
        now = _db_time(now_value)
        signed: SignedOfflineBundle | None = None
        target_bundle_id: str | None = None
        if payload.action == "approve":
            proposal = _proposal(request_row)
            manifest = _proposal_manifest(proposal)
            if now_value > manifest.expires_at.astimezone(UTC):
                raise ApiError("edge_offline_bundle.expired")
            signed = _sign_manifest(config, manifest)
            target_bundle_id = manifest.bundle_id
            _insert_bundle(connection, signed)
            conflicts = _apply_version_vector(
                connection,
                request_row,
                signed,
                now=now,
            )
            if str(request_row["operation"]) == "resolve":
                _insert_resolution(
                    connection,
                    request_row,
                    proposal,
                    signed,
                    context,
                    now=now,
                )
            _event(
                connection,
                request_row,
                event_type=(
                    "edge.offline_conflict_detected"
                    if conflicts
                    else "edge.offline_bundle_exported"
                    if manifest.direction == "cloud_to_edge"
                    else "edge.offline_bundle_applied"
                ),
                aggregate_type="conflict" if conflicts else "bundle",
                aggregate_id=conflicts[0] if conflicts else manifest.bundle_id,
                payload={
                    "bundle_id": manifest.bundle_id,
                    "direction": manifest.direction,
                    "domain_count": len(manifest.version_vector),
                    "conflict_count": len(conflicts),
                },
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        changed = _changed_stamp(now_value, request_row["updated_at"])
        if (
            connection.execute(
                """
            UPDATE edge_offline_bundle_requests SET status=?,approved_by_operator_id=?,
              target_offline_bundle_id=?,revision=revision+1,decided_at=?,updated_at=?
            WHERE offline_bundle_request_id=? AND status='pending_platform' AND revision=?
            """,
                (
                    "approved" if payload.action == "approve" else "rejected",
                    context.operator_id,
                    target_bundle_id,
                    now,
                    changed,
                    offline_request_id,
                    payload.expected_revision,
                ),
            ).rowcount
            != 1
        ):
            raise ApiError("edge_offline_bundle.approval_conflict")
        _insert_action(
            connection,
            actor_domain="platform",
            actor_id=context.operator_id,
            action_code=action_code,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=offline_request_id,
            now=now,
        )
        current = _request_row(connection, offline_request_id)
        result = PlatformOfflineDecisionResponse(
            request=_request_summary(current),
            signed_bundle=signed,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except OfflineBundleContractError as exc:
        _rollback(connection)
        raise ApiError(exc.code) from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_offline_bundle.state_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_offline_bundle.unavailable") from None
    finally:
        connection.close()


def list_platform_offline_states(
    config: AppConfig,
    context: PlatformContext,
    *,
    request_id: str,
    trace_id: str,
) -> PlatformOfflineBundleListResponse:
    _require_platform(context, "platform.edge_offline_bundle.read")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        deployments = connection.execute(
            """
            SELECT deployment.workspace_id,deployment.tenant_id,
                   deployment.edge_deployment_id
            FROM edge_deployments deployment
            WHERE EXISTS(SELECT 1 FROM edge_offline_bundle_requests request
              WHERE request.edge_deployment_id=deployment.edge_deployment_id)
               OR EXISTS(SELECT 1 FROM edge_offline_bundles bundle
              WHERE bundle.edge_deployment_id=deployment.edge_deployment_id)
            ORDER BY deployment.updated_at DESC,deployment.edge_deployment_id DESC LIMIT 200
            """
        ).fetchall()
        result = PlatformOfflineBundleListResponse(
            items=tuple(
                PlatformOfflineBundleItem(
                    workspace_id=str(row["workspace_id"]),
                    tenant_id=str(row["tenant_id"]),
                    state=_state_response(
                        connection,
                        str(row["edge_deployment_id"]),
                        request_id,
                        trace_id,
                    ),
                )
                for row in deployments
            ),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.rollback()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_offline_bundle.unavailable") from None
    finally:
        connection.close()


def _insert_bundle(connection: sqlite3.Connection, signed: SignedOfflineBundle) -> None:
    manifest = signed.manifest
    connection.execute(
        """
        INSERT INTO edge_offline_bundles(
          offline_bundle_id,workspace_id,tenant_id,edge_deployment_id,direction,
          producer_generation,domain_count,domain_codes_json,entry_count,
          encrypted_size_bytes,version_vector_sha256,entry_merkle_root,
          manifest_sha256,signature_key_id,encryption_key_version,kms_key_id_sha256,
          created_at,expires_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            manifest.bundle_id,
            manifest.workspace_id,
            manifest.tenant_id,
            manifest.deployment_id,
            manifest.direction,
            manifest.producer_generation,
            len(manifest.version_vector),
            canonical_edge_json_bytes([item.domain for item in manifest.version_vector]).decode(
                "ascii"
            ),
            len(manifest.entries),
            sum(item.ciphertext_size for item in manifest.entries),
            _digest([item.model_dump(mode="json") for item in manifest.version_vector]),
            manifest.entry_merkle_root,
            signed.manifest_sha256,
            signed.signature.key_id,
            manifest.encryption_key_version,
            _proposal_kms_sha(manifest),
            _db_time(manifest.created_at.astimezone(UTC)),
            _db_time(manifest.expires_at.astimezone(UTC)),
        ),
    )


def _apply_version_vector(
    connection: sqlite3.Connection,
    request_row: sqlite3.Row,
    signed: SignedOfflineBundle,
    *,
    now: str,
) -> tuple[str, ...]:
    manifest = signed.manifest
    current = _current_versions(connection, manifest.deployment_id)
    conflicts = (
        detect_offline_conflicts(current, manifest.version_vector)
        if manifest.direction == "edge_to_cloud"
        else ()
    )
    conflict_ids: list[str] = []
    for conflict in conflicts:
        conflict_id = _id("ofc_")
        conflict_ids.append(conflict_id)
        connection.execute(
            """
            INSERT INTO edge_offline_conflicts(
              offline_conflict_id,workspace_id,tenant_id,edge_deployment_id,
              offline_bundle_id,domain,conflict_type,base_generation,base_sha256,
              cloud_generation,cloud_sha256,local_generation,local_sha256,detected_at)
            VALUES(?,?,?,?,?,?,'divergent_update',?,?,?,?,?,?,?)
            """,
            (
                conflict_id,
                manifest.workspace_id,
                manifest.tenant_id,
                manifest.deployment_id,
                manifest.bundle_id,
                conflict.domain,
                conflict.base_generation,
                conflict.base_sha256,
                conflict.current_generation,
                conflict.current_sha256,
                conflict.incoming_generation,
                conflict.incoming_sha256,
                now,
            ),
        )
    chunk_count = len(manifest.entries)
    transfer_status = (
        "conflict"
        if conflict_ids
        else "prepared"
        if manifest.direction == "cloud_to_edge"
        else "applied"
    )
    completed = transfer_status in {"applied", "conflict"}
    connection.execute(
        """
        INSERT INTO edge_offline_bundle_transfers(
          offline_bundle_transfer_id,workspace_id,tenant_id,edge_deployment_id,
          offline_bundle_id,status,chunk_count,next_sequence,bytes_verified,
          media_receipt_sha256,revision,started_at,completed_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?)
        """,
        (
            _id("obt_"),
            manifest.workspace_id,
            manifest.tenant_id,
            manifest.deployment_id,
            manifest.bundle_id,
            transfer_status,
            chunk_count,
            chunk_count if completed else 0,
            sum(item.ciphertext_size for item in manifest.entries) if completed else 0,
            signed.manifest_sha256 if completed else None,
            now,
            now if completed else None,
            now,
        ),
    )
    if not conflict_ids:
        for version in manifest.version_vector:
            _upsert_cursor(connection, manifest, version, now=now)
    return tuple(conflict_ids)


def _insert_resolution(
    connection: sqlite3.Connection,
    request_row: sqlite3.Row,
    proposal: dict[str, object],
    signed: SignedOfflineBundle,
    context: PlatformContext,
    *,
    now: str,
) -> None:
    conflict_id = str(request_row["target_offline_conflict_id"])
    conflict = _conflict_row(connection, conflict_id)
    if _resolution_row(connection, conflict_id) is not None:
        raise ApiError("edge_offline_bundle.conflict_resolved")
    resolution = proposal.get("resolution")
    if resolution not in {"keep_cloud", "keep_local", "supersede_with_new"}:
        raise ApiError("edge_offline_bundle.resolution_conflict")
    version = _domain_version(signed.manifest, str(conflict["domain"]))
    maximum_generation = max(int(conflict["cloud_generation"]), int(conflict["local_generation"]))
    if version.generation <= maximum_generation:
        raise ApiError("edge_offline_bundle.resolution_conflict")
    approval_sha256 = _digest(
        {
            "conflict_id": conflict_id,
            "manifest_sha256": signed.manifest_sha256,
            "operator_id": context.operator_id,
            "resolution": resolution,
        }
    )
    connection.execute(
        """
        INSERT INTO edge_offline_conflict_resolutions(
          offline_conflict_resolution_id,workspace_id,tenant_id,offline_conflict_id,
          resolution,resolved_by_account_id,approved_by_operator_id,new_generation,
          new_value_sha256,approval_sha256,resolved_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("ofr_"),
            conflict["workspace_id"],
            conflict["tenant_id"],
            conflict_id,
            resolution,
            request_row["requested_by_account_id"],
            context.operator_id,
            version.generation,
            version.value_sha256,
            approval_sha256,
            now,
        ),
    )
    _event(
        connection,
        request_row,
        event_type="edge.offline_conflict_resolved",
        aggregate_type="conflict",
        aggregate_id=conflict_id,
        payload={
            "bundle_id": signed.manifest.bundle_id,
            "domain": version.domain,
            "new_generation": version.generation,
            "resolution": resolution,
        },
        request_id=approval_sha256[:32],
        trace_id=approval_sha256[32:],
        now=now,
    )


def _upsert_cursor(
    connection: sqlite3.Connection,
    manifest: OfflineBundleManifest,
    version: OfflineDomainVersion,
    *,
    now: str,
) -> None:
    row = connection.execute(
        """
        SELECT * FROM edge_offline_sync_cursors
        WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
          AND direction=? AND domain=?
        """,
        (
            manifest.workspace_id,
            manifest.tenant_id,
            manifest.deployment_id,
            manifest.direction,
            version.domain,
        ),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO edge_offline_sync_cursors(
              workspace_id,tenant_id,edge_deployment_id,direction,domain,generation,
              value_sha256,offline_bundle_id,revision,updated_at)
            VALUES(?,?,?,?,?,?,?,?,1,?)
            """,
            (
                manifest.workspace_id,
                manifest.tenant_id,
                manifest.deployment_id,
                manifest.direction,
                version.domain,
                version.generation,
                version.value_sha256,
                manifest.bundle_id,
                now,
            ),
        )
        return
    if version.generation < int(row["generation"]):
        raise ApiError("edge_offline_bundle.cursor_stale")
    if version.generation == int(row["generation"]):
        if not hmac.compare_digest(version.value_sha256, str(row["value_sha256"])):
            raise ApiError("edge_offline_bundle.cursor_conflict")
        return
    changed = _changed_stamp(_datetime(now), row["updated_at"])
    connection.execute(
        """
        UPDATE edge_offline_sync_cursors SET generation=?,value_sha256=?,
          offline_bundle_id=?,revision=revision+1,updated_at=?
        WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
          AND direction=? AND domain=? AND revision=?
        """,
        (
            version.generation,
            version.value_sha256,
            manifest.bundle_id,
            changed,
            manifest.workspace_id,
            manifest.tenant_id,
            manifest.deployment_id,
            manifest.direction,
            version.domain,
            row["revision"],
        ),
    )


def _state_response(
    connection: sqlite3.Connection,
    deployment_id: str,
    request_id: str,
    trace_id: str,
    *,
    replay: bool = False,
) -> OfflineBundleStateResponse:
    requests = connection.execute(
        """SELECT * FROM edge_offline_bundle_requests WHERE edge_deployment_id=?
             ORDER BY updated_at DESC,offline_bundle_request_id DESC LIMIT 40""",
        (deployment_id,),
    ).fetchall()
    bundles = connection.execute(
        """
        SELECT bundle.*,transfer.status AS transfer_status,transfer.next_sequence,
               transfer.chunk_count
        FROM edge_offline_bundles bundle
        JOIN edge_offline_bundle_transfers transfer
          ON transfer.offline_bundle_id=bundle.offline_bundle_id
        WHERE bundle.edge_deployment_id=?
        ORDER BY bundle.created_at DESC,bundle.offline_bundle_id DESC LIMIT 40
        """,
        (deployment_id,),
    ).fetchall()
    conflicts = connection.execute(
        """
        SELECT conflict.*,resolution.resolution,resolution.new_generation,
               resolution.resolved_at
        FROM edge_offline_conflicts conflict
        LEFT JOIN edge_offline_conflict_resolutions resolution
          ON resolution.offline_conflict_id=conflict.offline_conflict_id
        WHERE conflict.edge_deployment_id=?
        ORDER BY conflict.detected_at DESC,conflict.offline_conflict_id DESC LIMIT 80
        """,
        (deployment_id,),
    ).fetchall()
    return OfflineBundleStateResponse(
        deployment_id=deployment_id,
        asynchronous_management=True,
        requests=tuple(_request_summary(row) for row in requests),
        bundles=tuple(_bundle_summary(row) for row in bundles),
        conflicts=tuple(_conflict_summary(row) for row in conflicts),
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
        idempotent_replay=replay,
    )


def _request_summary(row: sqlite3.Row) -> OfflineBundleRequestSummary:
    return OfflineBundleRequestSummary(
        request_id=str(row["offline_bundle_request_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        operation=str(row["operation"]),
        status=str(row["status"]),
        reason_code=str(row["reason_code"]),
        target_bundle_id=row["target_offline_bundle_id"],
        target_conflict_id=row["target_offline_conflict_id"],
        revision=int(row["revision"]),
        requested_at=_datetime(row["requested_at"]),
        decided_at=_datetime(row["decided_at"]) if row["decided_at"] else None,
    )


def _bundle_summary(row: sqlite3.Row) -> OfflineBundleSummary:
    domains = strict_json_object(
        canonical_edge_json_bytes({"items": json.loads(row["domain_codes_json"])}),
        canonical=True,
        maximum=4096,
    )["items"]
    if type(domains) is not list:
        raise ValueError
    return OfflineBundleSummary(
        bundle_id=str(row["offline_bundle_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        direction=str(row["direction"]),
        producer_generation=int(row["producer_generation"]),
        domains=tuple(domains),
        entry_count=int(row["entry_count"]),
        encrypted_size_bytes=int(row["encrypted_size_bytes"]),
        transfer_status=str(row["transfer_status"]),
        next_sequence=int(row["next_sequence"]),
        chunk_count=int(row["chunk_count"]),
        manifest_sha256=str(row["manifest_sha256"]),
        created_at=_datetime(row["created_at"]),
        expires_at=_datetime(row["expires_at"]),
    )


def _conflict_summary(row: sqlite3.Row) -> OfflineConflictSummary:
    return OfflineConflictSummary(
        conflict_id=str(row["offline_conflict_id"]),
        bundle_id=str(row["offline_bundle_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        domain=str(row["domain"]),
        base_generation=int(row["base_generation"]),
        base_sha256=row["base_sha256"],
        cloud_generation=int(row["cloud_generation"]),
        cloud_sha256=str(row["cloud_sha256"]),
        local_generation=int(row["local_generation"]),
        local_sha256=str(row["local_sha256"]),
        resolution=row["resolution"],
        new_generation=(int(row["new_generation"]) if row["new_generation"] else None),
        detected_at=_datetime(row["detected_at"]),
        resolved_at=_datetime(row["resolved_at"]) if row["resolved_at"] else None,
    )


def _current_versions(
    connection: sqlite3.Connection, deployment_id: str
) -> dict[str, tuple[int, str]]:
    rows = connection.execute(
        """
        SELECT domain,generation,value_sha256 FROM edge_offline_sync_cursors
        WHERE edge_deployment_id=? ORDER BY generation DESC,direction ASC
        """,
        (deployment_id,),
    ).fetchall()
    result: dict[str, tuple[int, str]] = {}
    for row in rows:
        result.setdefault(str(row["domain"]), (int(row["generation"]), str(row["value_sha256"])))
    return result


def _proposal(row: sqlite3.Row) -> dict[str, object]:
    return strict_json_object(
        str(row["proposal_json"]).encode("ascii"), canonical=True, maximum=512 * 1024
    )


def _proposal_manifest(proposal: dict[str, object]) -> OfflineBundleManifest:
    if set(proposal) not in ({"manifest"}, {"manifest", "resolution"}):
        raise ApiError("edge_offline_bundle.request_invalid")
    try:
        return OfflineBundleManifest.model_validate_json(
            canonical_edge_json_bytes(proposal["manifest"])
        )
    except ValueError:
        raise ApiError("edge_offline_bundle.request_invalid") from None


def _signed_for_request(config: AppConfig, request_row: sqlite3.Row) -> SignedOfflineBundle | None:
    if str(request_row["status"]) != "approved":
        return None
    return _sign_manifest(config, _proposal_manifest(_proposal(request_row)))


def _sign_manifest(config: AppConfig, manifest: OfflineBundleManifest) -> SignedOfflineBundle:
    seed = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        b"metahub/prd130/offline-bundle-signing-key/v1",
        hashlib.sha256,
    ).digest()
    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes_raw()
    key_id = f"edge-offline-{hashlib.sha256(public).hexdigest()[:16]}"
    return sign_offline_bundle_manifest(manifest, private_key=private, key_id=key_id)


def _proposal_kms_sha(manifest: OfflineBundleManifest) -> str:
    return manifest.kms_key_id_sha256


def _validate_manifest_owner(
    manifest: OfflineBundleManifest,
    deployment: sqlite3.Row,
    *,
    expected_direction: str,
) -> None:
    if not (
        manifest.direction == expected_direction
        and hmac.compare_digest(manifest.workspace_id, str(deployment["workspace_id"]))
        and hmac.compare_digest(manifest.tenant_id, str(deployment["tenant_id"]))
        and hmac.compare_digest(manifest.deployment_id, str(deployment["edge_deployment_id"]))
    ):
        raise ApiError("edge_offline_bundle.owner_mismatch")


def _domain_version(manifest: OfflineBundleManifest, domain: str) -> OfflineDomainVersion:
    matches = [item for item in manifest.version_vector if item.domain == domain]
    if len(matches) != 1:
        raise ApiError("edge_offline_bundle.resolution_conflict")
    return matches[0]


def _request_row(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_offline_bundle_requests WHERE offline_bundle_request_id=?",
        (request_id,),
    ).fetchone()
    if row is None:
        raise ApiError("edge_offline_bundle.not_found")
    return row


def _conflict_row(connection: sqlite3.Connection, conflict_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_offline_conflicts WHERE offline_conflict_id=?",
        (conflict_id,),
    ).fetchone()
    if row is None:
        raise ApiError("edge_offline_bundle.not_found")
    return row


def _resolution_row(connection: sqlite3.Connection, conflict_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM edge_offline_conflict_resolutions
             WHERE offline_conflict_id=?""",
        (conflict_id,),
    ).fetchone()


def _event(
    connection: sqlite3.Connection,
    owner: sqlite3.Row,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, object],
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    reject_forbidden_offline_fields(payload)
    event_id = _id("oev_")
    connection.execute(
        """
        INSERT INTO edge_offline_bundle_events(
          edge_offline_bundle_event_id,workspace_id,tenant_id,edge_deployment_id,
          event_type,aggregate_type,aggregate_id,safe_payload_json,request_id,trace_id,
          occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            owner["workspace_id"],
            owner["tenant_id"],
            owner["edge_deployment_id"],
            event_type,
            aggregate_type,
            aggregate_id,
            canonical_edge_json_bytes(payload).decode("ascii"),
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    for domain in ("audit", "fleet"):
        connection.execute(
            """
            INSERT INTO edge_offline_bundle_outbox(
              edge_offline_bundle_outbox_id,workspace_id,tenant_id,
              edge_offline_bundle_event_id,delivery_domain,status,attempt_count,
              available_at,delivered_at,created_at,updated_at)
            VALUES(?,?,?,?,?,'pending',0,?,NULL,?,?)
            """,
            (
                _id("oob_"),
                owner["workspace_id"],
                owner["tenant_id"],
                event_id,
                domain,
                now,
                now,
                now,
            ),
        )


def _action_replay(
    connection: sqlite3.Connection,
    actor_domain: str,
    actor_id: str,
    action_code: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM edge_offline_bundle_action_commands
        WHERE actor_domain=? AND actor_id=? AND action_code=? AND idempotency_sha256=?
        """,
        (actor_domain, actor_id, action_code, idempotency_sha256),
    ).fetchone()


def _insert_action(
    connection: sqlite3.Connection,
    *,
    actor_domain: str,
    actor_id: str,
    action_code: str,
    idempotency_sha256: str,
    request_sha256: str,
    result_object_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO edge_offline_bundle_action_commands(
          edge_offline_bundle_action_command_id,actor_domain,actor_id,action_code,
          idempotency_sha256,request_sha256,result_object_id,created_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            _id("oac_"),
            actor_domain,
            actor_id,
            action_code,
            idempotency_sha256,
            request_sha256,
            result_object_id,
            now,
        ),
    )


def _verify_replay(row: sqlite3.Row, request_sha256: str) -> None:
    if not hmac.compare_digest(str(row["request_sha256"]), request_sha256):
        raise ApiError("edge_offline_bundle.idempotency_conflict")


def _workspace_idempotency(config: AppConfig, account_id: str, action: str, value: str) -> str:
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        f"prd130\0{account_id}\0{action}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _platform_idempotency(config: AppConfig, operator_id: str, action: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd130\0{operator_id}\0{action}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _require_platform(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _changed_stamp(now: datetime, previous: object) -> str:
    current = _db_time(now)
    prior = str(previous)
    if current > prior:
        return current
    return _db_time(_datetime(prior) + timedelta(microseconds=1))


__all__ = (
    "decide_platform_offline_request",
    "get_workspace_offline_state",
    "list_platform_offline_states",
    "request_workspace_offline_bundle",
    "request_workspace_offline_resolution",
)
