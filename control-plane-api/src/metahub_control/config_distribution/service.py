"""DB-authoritative config publication, internal delivery, ACKs, and rebuild orchestration."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import Response
from metahub_shared.config_distribution import (
    canonical_json_bytes,
    create_manifest_delta,
)
from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.platform_jobs.service import (
    enqueue_prepared_platform_job_in_transaction,
    prepare_platform_job,
)

from .crypto import open_config_value, seal_config_value, sign_config_manifest
from .models import (
    ConfigAckRequest,
    ConfigAckResponse,
    ConfigDistributionResponse,
    ConfigRebuildInfo,
    ConfigRebuildRequest,
    ConfigRebuildResponse,
    ConfigWatermarkSummary,
)

_WORKSPACE_ID_RE = re.compile(r"^wsp_[0-9a-f]{32}$")
_SNAPSHOT_ID_RE = re.compile(r"^csn_[0-9a-f]{32}$")
_GATEWAY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def list_config_distribution(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> ConfigDistributionResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require(actor, "platform.config_distribution.read")
        rows = connection.execute(
            """
            SELECT placement.workspace_id,cell.cell_code,
              coalesce(head.generation,0) generation,
              coalesce(head.revocation_watermark,0) revocation_watermark,
              head.manifest_sha256,
              count(DISTINCT watermark.gateway_identity_sha256) gateway_count,
              coalesce(min(watermark.applied_generation),0) minimum_applied_generation,
              coalesce(min(watermark.lkg_generation),0) minimum_lkg_generation,
              count(DISTINCT CASE WHEN watermark.stale_reason!='none'
                THEN watermark.gateway_identity_sha256 END) stale_gateway_count,
              max(ack.acked_at) latest_ack_at
            FROM workspace_gateway_placements placement
            JOIN gateway_cells cell
              ON cell.gateway_cell_id=placement.primary_gateway_cell_id
            LEFT JOIN config_snapshot_heads head
              ON head.workspace_id=placement.workspace_id
            LEFT JOIN cache_watermarks watermark
              ON watermark.workspace_id=placement.workspace_id
            LEFT JOIN config_acks ack ON ack.workspace_id=placement.workspace_id
            GROUP BY placement.workspace_id,cell.cell_code,head.generation,
              head.revocation_watermark,head.manifest_sha256
            ORDER BY placement.workspace_id
            LIMIT 500
            """
        ).fetchall()
        items = tuple(
            ConfigWatermarkSummary(
                workspace_id=str(row["workspace_id"]),
                cell_code=str(row["cell_code"]),
                generation=int(row["generation"]),
                revocation_watermark=int(row["revocation_watermark"]),
                manifest_sha256=row["manifest_sha256"],
                gateway_count=int(row["gateway_count"]),
                minimum_applied_generation=int(row["minimum_applied_generation"]),
                minimum_lkg_generation=int(row["minimum_lkg_generation"]),
                stale_gateway_count=int(row["stale_gateway_count"]),
                latest_ack_at=_optional_time(row["latest_ack_at"]),
                latest_rebuild=_latest_rebuild(connection, str(row["workspace_id"])),
            )
            for row in rows
        )
        connection.commit()
        return ConfigDistributionResponse(
            items=items, total=len(items), request_id=request_id, trace_id=trace_id
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("config_distribution.unavailable") from None
    finally:
        connection.close()


def create_config_rebuild(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    payload: ConfigRebuildRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ConfigRebuildResponse:
    if _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
        raise ApiError("config_distribution.request_invalid")
    request_value = payload.model_dump(mode="json")
    request_sha256 = _digest(request_value)
    idempotency_sha256 = _secret_digest(config, "rebuild-idempotency", idempotency_key)
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
        _require(actor, "platform.config_distribution.rebuild")
        existing = connection.execute(
            "SELECT * FROM config_rebuild_jobs WHERE requested_by_operator_id=? "
            "AND idempotency_sha256=?",
            (actor.operator_id, idempotency_sha256),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["request_sha256"]), request_sha256):
                raise ApiError("config_distribution.idempotency_conflict")
            connection.commit()
            return ConfigRebuildResponse(
                rebuild=_rebuild_info(existing),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
        owner = connection.execute(
            """
            SELECT binding.workspace_id,binding.tenant_id,placement.primary_gateway_cell_id,
              coalesce(head.generation,0)+1 target_generation
            FROM workspace_tenant_bindings binding
            JOIN workspace_gateway_placements placement
              ON placement.workspace_id=binding.workspace_id
             AND placement.tenant_id=binding.tenant_id
            LEFT JOIN config_snapshot_heads head ON head.workspace_id=binding.workspace_id
            WHERE binding.workspace_id=? AND binding.status='active'
            """,
            (payload.workspace_id,),
        ).fetchone()
        if owner is None:
            raise ApiError("config_distribution.workspace_unavailable")
        rebuild_id = _id("crj_")
        target_generation = int(owner["target_generation"])
        prepared = prepare_platform_job(
            config,
            type_code="config.snapshot.rebuild",
            payload={
                "rebuild_job_id": rebuild_id,
                "workspace_id": payload.workspace_id,
                "target_generation": target_generation,
            },
        )
        platform_job_id, _ = enqueue_prepared_platform_job_in_transaction(
            connection,
            config,
            prepared=prepared,
            priority=90,
            source_code="config_distribution",
            idempotency_key="config-rebuild:" + rebuild_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        now = _database_now(connection)
        connection.execute(
            """
            INSERT INTO config_rebuild_jobs(
              config_rebuild_job_id,workspace_id,tenant_id,target_generation,
              platform_job_id,requested_by_operator_id,status,result_manifest_sha256,
              idempotency_sha256,request_sha256,revision,request_id,trace_id,
              created_at,updated_at,completed_at)
            VALUES(?,?,?,?,?,?,'queued',NULL,?,?,1,?,?,?,?,NULL)
            """,
            (
                rebuild_id,
                owner["workspace_id"],
                owner["tenant_id"],
                target_generation,
                platform_job_id,
                actor.operator_id,
                idempotency_sha256,
                request_sha256,
                request_id,
                trace_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor=actor,
            action="config.rebuild_requested",
            object_id=rebuild_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={"workspace_id": payload.workspace_id, "target_generation": target_generation},
            now=now,
        )
        row = connection.execute(
            "SELECT * FROM config_rebuild_jobs WHERE config_rebuild_job_id=?", (rebuild_id,)
        ).fetchone()
        connection.commit()
        return ConfigRebuildResponse(
            rebuild=_rebuild_info(row), request_id=request_id, trace_id=trace_id
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("config_distribution.unavailable") from None
    finally:
        connection.close()


def build_internal_config_response(
    config: AppConfig,
    *,
    workspace_id: str,
    gateway_identity: str,
    after_generation: int | None,
    if_none_match: str | None,
    request_id: str,
    trace_id: str,
) -> Response:
    _valid_workspace(workspace_id)
    _valid_gateway_identity(gateway_identity)
    if after_generation is not None and after_generation < 0:
        raise ApiError("config_distribution.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        gateway = _gateway_owner(connection, gateway_identity)
        row = connection.execute(
            """
            SELECT head.*,snapshot.manifest_envelope,snapshot.source_sha256,
              snapshot.kms_key_version,snapshot.signature_algorithm,snapshot.signature_b64,
              snapshot.published_at snapshot_published_at,placement.primary_gateway_cell_id
            FROM config_snapshot_heads head
            JOIN config_snapshots snapshot
              ON snapshot.config_snapshot_id=head.config_snapshot_id
            JOIN workspace_gateway_placements placement
              ON placement.workspace_id=head.workspace_id
             AND placement.tenant_id=head.tenant_id
            WHERE head.workspace_id=? AND placement.primary_gateway_cell_id=?
            """,
            (workspace_id, gateway["gateway_cell_id"]),
        ).fetchone()
        if row is None:
            raise ApiError("config_distribution.snapshot_unavailable")
        _observe_watermark(connection, row, gateway, config, now=_database_now(connection))
        etag = f'"{row["manifest_sha256"]}"'
        if if_none_match == etag or after_generation == int(row["generation"]):
            connection.commit()
            return Response(status_code=304, headers={"Cache-Control": "no-store", "ETag": etag})
        wire = _snapshot_wire(
            connection,
            config,
            row=row,
            after_generation=after_generation,
            request_id=request_id,
            trace_id=trace_id,
        )
        body = canonical_json_bytes(wire)
        connection.commit()
        return Response(
            content=body,
            media_type="application/json",
            headers={"Cache-Control": "no-store", "ETag": etag},
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("config_distribution.unavailable") from None
    finally:
        connection.close()


def acknowledge_internal_config(
    config: AppConfig,
    *,
    snapshot_id: str,
    gateway_identity: str,
    payload: ConfigAckRequest,
    request_id: str,
    trace_id: str,
) -> ConfigAckResponse:
    if _SNAPSHOT_ID_RE.fullmatch(snapshot_id) is None:
        raise ApiError("config_distribution.snapshot_unavailable")
    _valid_gateway_identity(gateway_identity)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        gateway = _gateway_owner(connection, gateway_identity)
        identity_sha256 = _gateway_identity_digest(config, gateway_identity)
        snapshot = connection.execute(
            """
            SELECT snapshot.*,placement.primary_gateway_cell_id
            FROM config_snapshots snapshot
            JOIN workspace_gateway_placements placement
              ON placement.workspace_id=snapshot.workspace_id
             AND placement.tenant_id=snapshot.tenant_id
            WHERE snapshot.config_snapshot_id=?
              AND placement.primary_gateway_cell_id=?
            """,
            (snapshot_id, gateway["gateway_cell_id"]),
        ).fetchone()
        if snapshot is None:
            raise ApiError("config_distribution.snapshot_unavailable")
        if (
            payload.generation != int(snapshot["generation"])
            or not hmac.compare_digest(payload.manifest_sha256, str(snapshot["manifest_sha256"]))
        ):
            raise ApiError("config_distribution.ack_invalid")
        existing = connection.execute(
            "SELECT * FROM config_acks WHERE gateway_identity_sha256=? AND config_snapshot_id=?",
            (identity_sha256, snapshot_id),
        ).fetchone()
        if existing is not None:
            if existing["ack_status"] != payload.status:
                raise ApiError("config_distribution.idempotency_conflict")
            watermark = _watermark(connection, str(snapshot["workspace_id"]), identity_sha256)
            connection.commit()
            return _ack_response(
                existing,
                watermark,
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
        watermark = _watermark(connection, str(snapshot["workspace_id"]), identity_sha256)
        now = _database_now(connection)
        accepted = payload.status == "applied" and payload.generation >= int(
            watermark["applied_generation"]
        )
        disposition = "accepted" if accepted else "late"
        reason = (
            "applied"
            if accepted
            else "apply_failed"
            if payload.status == "failed"
            else "superseded_generation"
        )
        ack_id = _id("cak_")
        connection.execute(
            """
            INSERT INTO config_acks(
              config_ack_id,config_snapshot_id,workspace_id,tenant_id,gateway_cell_id,
              gateway_identity_sha256,generation,manifest_sha256,ack_status,disposition,
              reason_code,acked_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ack_id,
                snapshot_id,
                snapshot["workspace_id"],
                snapshot["tenant_id"],
                gateway["gateway_cell_id"],
                identity_sha256,
                payload.generation,
                payload.manifest_sha256,
                payload.status,
                disposition,
                reason,
                _db_time(now),
            ),
        )
        if accepted:
            connection.execute(
                """
                UPDATE cache_watermarks
                SET observed_generation=max(observed_generation,?),
                  applied_generation=max(applied_generation,?),
                  lkg_generation=max(lkg_generation,?),
                  observed_revocation_watermark=max(observed_revocation_watermark,?),
                  applied_revocation_watermark=max(applied_revocation_watermark,?),
                  stale_reason='none',revision=revision+1,last_observed_at=?,last_applied_at=?
                WHERE cache_watermark_id=?
                """,
                (
                    payload.generation,
                    payload.generation,
                    payload.generation,
                    int(snapshot["revocation_watermark"]),
                    int(snapshot["revocation_watermark"]),
                    _db_time(now),
                    _db_time(now),
                    watermark["cache_watermark_id"],
                ),
            )
        else:
            connection.execute(
                """
                UPDATE cache_watermarks SET stale_reason='revocation_lag',revision=revision+1,
                  last_observed_at=? WHERE cache_watermark_id=?
                """,
                (_db_time(now), watermark["cache_watermark_id"]),
            )
            _event(
                connection,
                event_type="config.ack_lagging",
                workspace_id=str(snapshot["workspace_id"]),
                tenant_id=str(snapshot["tenant_id"]),
                generation=payload.generation,
                manifest_sha256=payload.manifest_sha256,
                gateway_cell_id=str(gateway["gateway_cell_id"]),
                reason="ack_lagging",
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        ack = connection.execute(
            "SELECT * FROM config_acks WHERE config_ack_id=?", (ack_id,)
        ).fetchone()
        current = _watermark(connection, str(snapshot["workspace_id"]), identity_sha256)
        connection.commit()
        return _ack_response(
            ack, current, request_id=request_id, trace_id=trace_id, replay=False
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("config_distribution.unavailable") from None
    finally:
        connection.close()


def publish_rebuild_in_transaction(
    connection,
    config: AppConfig,
    *,
    rebuild_job_id: str,
    platform_job_id: str,
    target_generation: int,
) -> str:
    rebuild = connection.execute(
        "SELECT * FROM config_rebuild_jobs WHERE config_rebuild_job_id=?",
        (rebuild_job_id,),
    ).fetchone()
    if (
        rebuild is None
        or rebuild["platform_job_id"] != platform_job_id
        or int(rebuild["target_generation"]) != target_generation
    ):
        raise ApiError("config_distribution.rebuild_stale")
    if rebuild["status"] == "completed":
        return str(rebuild["result_manifest_sha256"])
    if rebuild["status"] not in {"queued", "running"}:
        raise ApiError("config_distribution.rebuild_stale")
    now = _database_now(connection)
    if rebuild["status"] == "queued":
        connection.execute(
            "UPDATE config_rebuild_jobs SET status='running',revision=revision+1,updated_at=? "
            "WHERE config_rebuild_job_id=? AND status='queued'",
            (_db_time(_changed_time(now, rebuild["updated_at"])), rebuild_job_id),
        )
        rebuild = connection.execute(
            "SELECT * FROM config_rebuild_jobs WHERE config_rebuild_job_id=?", (rebuild_job_id,)
        ).fetchone()
    manifest_sha256 = _publish_snapshot(
        connection,
        config,
        workspace_id=str(rebuild["workspace_id"]),
        tenant_id=str(rebuild["tenant_id"]),
        generation=target_generation,
        request_id=str(rebuild["request_id"]),
        trace_id=str(rebuild["trace_id"]),
        now=now,
    )
    completed_at = _changed_time(now, rebuild["updated_at"])
    connection.execute(
        """
        UPDATE config_rebuild_jobs SET status='completed',result_manifest_sha256=?,
          revision=revision+1,updated_at=?,completed_at=?
        WHERE config_rebuild_job_id=? AND status='running'
        """,
        (
            manifest_sha256,
            _db_time(completed_at),
            _db_time(completed_at),
            rebuild_job_id,
        ),
    )
    _event(
        connection,
        event_type="config.rebuild_completed",
        workspace_id=str(rebuild["workspace_id"]),
        tenant_id=str(rebuild["tenant_id"]),
        generation=target_generation,
        manifest_sha256=manifest_sha256,
        gateway_cell_id=None,
        reason="rebuild_completed",
        request_id=str(rebuild["request_id"]),
        trace_id=str(rebuild["trace_id"]),
        now=completed_at,
    )
    return manifest_sha256


def publish_workspace_snapshot_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    workspace_id: str,
    tenant_id: str,
    request_id: str,
    trace_id: str,
) -> str:
    """Publish the next signed workspace snapshot inside the caller transaction."""

    if not connection.in_transaction:
        raise ApiError("config_distribution.unavailable")
    head = connection.execute(
        "SELECT generation FROM config_snapshot_heads WHERE workspace_id=? AND tenant_id=?",
        (workspace_id, tenant_id),
    ).fetchone()
    generation = 1 if head is None else int(head["generation"]) + 1
    return _publish_snapshot(
        connection,
        config,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        generation=generation,
        request_id=request_id,
        trace_id=trace_id,
        now=_database_now(connection),
    )


def _publish_snapshot(
    connection,
    config: AppConfig,
    *,
    workspace_id: str,
    tenant_id: str,
    generation: int,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> str:
    head = connection.execute(
        "SELECT * FROM config_snapshot_heads WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    if head is not None and generation <= int(head["generation"]):
        existing = connection.execute(
            "SELECT manifest_sha256 FROM config_snapshots WHERE workspace_id=? AND generation=?",
            (workspace_id, generation),
        ).fetchone()
        if existing is not None and generation == int(head["generation"]):
            return str(existing["manifest_sha256"])
        raise ApiError("config_distribution.rebuild_stale")
    if head is None and generation != 1:
        raise ApiError("config_distribution.rebuild_stale")
    if head is not None and generation != int(head["generation"]) + 1:
        raise ApiError("config_distribution.rebuild_stale")
    previous_manifest = _head_manifest(connection, config, head) if head is not None else None
    current_revocation = int(head["revocation_watermark"]) if head else 0
    revocation_watermark = _next_revocation_watermark(
        connection, workspace_id, previous_manifest, current_revocation
    )
    manifest = _build_manifest(
        connection,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        generation=generation,
        revocation_watermark=revocation_watermark,
    )
    manifest_sha256 = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    signature = sign_config_manifest(config, manifest)
    snapshot_id = _id("csn_")
    sealed = seal_config_value(
        config,
        manifest,
        owner_id=snapshot_id,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        kind="manifest",
    )
    if sealed.plaintext_sha256 != manifest_sha256:
        raise ApiError("config_distribution.envelope_invalid")
    stamp = now if head is None else _changed_time(now, head["updated_at"])
    connection.execute(
        """
        INSERT INTO config_snapshots(
          config_snapshot_id,workspace_id,tenant_id,generation,manifest_envelope,
          manifest_sha256,source_sha256,kms_key_version,signature_algorithm,
          signature_b64,revocation_watermark,published_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            snapshot_id,
            workspace_id,
            tenant_id,
            generation,
            sealed.envelope,
            manifest_sha256,
            manifest["source_sha256"],
            signature.key_version,
            signature.algorithm,
            signature.value,
            revocation_watermark,
            _db_time(stamp),
        ),
    )
    if head is None:
        connection.execute(
            """
            INSERT INTO config_snapshot_heads(
              config_snapshot_head_id,workspace_id,tenant_id,generation,config_snapshot_id,
              manifest_sha256,revocation_watermark,revision,published_at,updated_at)
            VALUES(?,?,?,?,?,?,?,1,?,?)
            """,
            (
                _id("csh_"),
                workspace_id,
                tenant_id,
                generation,
                snapshot_id,
                manifest_sha256,
                revocation_watermark,
                _db_time(stamp),
                _db_time(stamp),
            ),
        )
    else:
        delta = create_manifest_delta(previous_manifest, manifest)
        delta_id = _id("cdl_")
        sealed_delta = seal_config_value(
            config,
            delta,
            owner_id=delta_id,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            kind="delta",
        )
        categories = sorted(delta["changes"])
        connection.execute(
            """
            INSERT INTO config_deltas(
              config_delta_id,workspace_id,tenant_id,from_generation,to_generation,
              from_manifest_sha256,to_manifest_sha256,categories_json,patch_envelope,
              patch_sha256,full_rebuild,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,0,?)
            """,
            (
                delta_id,
                workspace_id,
                tenant_id,
                int(head["generation"]),
                generation,
                head["manifest_sha256"],
                manifest_sha256,
                _canonical(categories),
                sealed_delta.envelope,
                sealed_delta.plaintext_sha256,
                _db_time(stamp),
            ),
        )
        connection.execute(
            """
            UPDATE config_snapshot_heads SET generation=?,config_snapshot_id=?,manifest_sha256=?,
              revocation_watermark=?,revision=revision+1,published_at=?,updated_at=?
            WHERE config_snapshot_head_id=? AND generation=?
            """,
            (
                generation,
                snapshot_id,
                manifest_sha256,
                revocation_watermark,
                _db_time(stamp),
                _db_time(stamp),
                head["config_snapshot_head_id"],
                head["generation"],
            ),
        )
    placement = connection.execute(
        "SELECT primary_gateway_cell_id FROM workspace_gateway_placements WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone()
    _event(
        connection,
        event_type="config.snapshot_published",
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        generation=generation,
        manifest_sha256=manifest_sha256,
        gateway_cell_id=str(placement["primary_gateway_cell_id"]),
        reason="snapshot_published",
        request_id=request_id,
        trace_id=trace_id,
        now=stamp,
    )
    return manifest_sha256


def _build_manifest(
    connection,
    *,
    workspace_id: str,
    tenant_id: str,
    generation: int,
    revocation_watermark: int,
) -> dict[str, object]:
    owner = connection.execute(
        """
        SELECT state.status,state.revision workspace_revision,cell.cell_code,
          cell.region_code,placement.revision placement_revision
        FROM workspace_operational_states state
        JOIN workspace_gateway_placements placement
          ON placement.workspace_id=state.workspace_id AND placement.tenant_id=state.tenant_id
        JOIN gateway_cells cell ON cell.gateway_cell_id=placement.primary_gateway_cell_id
        WHERE state.workspace_id=? AND state.tenant_id=?
        """,
        (workspace_id, tenant_id),
    ).fetchone()
    if owner is None:
        raise ApiError("config_distribution.workspace_unavailable")
    entitlement = connection.execute(
        """
        SELECT snapshot_version,entitlements_sha256 FROM entitlement_snapshots
        WHERE workspace_id=? AND tenant_id=?
          AND effective_from<=strftime('%Y-%m-%dT%H:%M:%fZ','now')
          AND (effective_until IS NULL OR effective_until>strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ORDER BY snapshot_version DESC LIMIT 1
        """,
        (workspace_id, tenant_id),
    ).fetchone()
    policy = connection.execute(
        """
        SELECT head.current_snapshot_version,snapshot.artifact_hash
        FROM policy_snapshot_heads head
        JOIN policy_snapshots snapshot ON snapshot.policy_snapshot_id=head.current_snapshot_id
        WHERE head.tenant_id=?
        """,
        (tenant_id,),
    ).fetchone()
    refs = [
        {
            "credential_id": str(row["provider_credential_id"]),
            "provider_id": str(row["provider_id"]),
            "status": "active" if row["status"] == "active" else "disabled",
        }
        for row in connection.execute(
            "SELECT provider_credential_id,provider_id,status FROM provider_credentials "
            "WHERE tenant_id=? ORDER BY provider_id,provider_credential_id",
            (tenant_id,),
        ).fetchall()
    ]
    features = [
        {
            "flag_code": str(row["flag_code"]),
            "version": int(row["version_number"]),
            "enabled": row["status"] == "active" and int(row["rollout_percentage_bps"]) > 0,
            "kill_generation": int(row["kill_generation"]),
        }
        for row in connection.execute(
            """
            SELECT flag.flag_code,flag.status,flag.rollout_percentage_bps,
              flag.kill_generation,version.version_number
            FROM feature_flags flag
            JOIN feature_flag_versions version
              ON version.feature_flag_id=flag.feature_flag_id
             AND version.feature_flag_version_id=flag.current_version_id
            ORDER BY flag.flag_code
            """
        ).fetchall()
    ]
    data_governance = _data_governance_projection(
        connection,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
    )
    safe = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "tenant_id": tenant_id,
        "generation": generation,
        "revocation_watermark": revocation_watermark,
        "workspace": {"status": str(owner["status"]), "revision": int(owner["workspace_revision"])},
        "placement": {
            "cell_code": str(owner["cell_code"]),
            "region_code": str(owner["region_code"]),
            "revision": int(owner["placement_revision"]),
        },
        "entitlements": (
            {
                "version": int(entitlement["snapshot_version"]),
                "sha256": str(entitlement["entitlements_sha256"]),
            }
            if entitlement is not None
            else None
        ),
        "policy": (
            {
                "version": int(policy["current_snapshot_version"]),
                "sha256": str(policy["artifact_hash"]),
            }
            if policy is not None
            else None
        ),
        "provider_key_refs": refs,
        "feature_capabilities": features,
    }
    if data_governance is not None:
        safe["data_governance"] = data_governance
    return {**safe, "source_sha256": _digest(safe)}


def _next_revocation_watermark(
    connection,
    workspace_id: str,
    previous: dict[str, object] | None,
    current_watermark: int,
) -> int:
    if previous is None:
        state = connection.execute(
            "SELECT status FROM workspace_operational_states WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        baseline = 0 if state is not None and state["status"] == "active" else 1
        governance_table = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='residency_policies'"
        ).fetchone()
        if governance_table is None:
            return baseline
        governance = connection.execute(
            "SELECT generation FROM residency_policies WHERE workspace_id=? AND status='active'",
            (workspace_id,),
        ).fetchone()
        return baseline + max(0, int(governance["generation"]) - 1) if governance else baseline
    current_security = {
        "workspace": previous["workspace"],
        "provider_key_refs": previous["provider_key_refs"],
        "feature_capabilities": previous["feature_capabilities"],
        "data_governance": previous.get("data_governance"),
    }
    tenant_id = str(previous["tenant_id"])
    state = connection.execute(
        "SELECT status,revision FROM workspace_operational_states WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone()
    refs = [
        {
            "credential_id": str(row["provider_credential_id"]),
            "provider_id": str(row["provider_id"]),
            "status": "active" if row["status"] == "active" else "disabled",
        }
        for row in connection.execute(
            "SELECT provider_credential_id,provider_id,status FROM provider_credentials "
            "WHERE tenant_id=? ORDER BY provider_id,provider_credential_id",
            (tenant_id,),
        ).fetchall()
    ]
    features = [
        {
            "flag_code": str(row["flag_code"]),
            "version": int(row["version_number"]),
            "enabled": row["status"] == "active" and int(row["rollout_percentage_bps"]) > 0,
            "kill_generation": int(row["kill_generation"]),
        }
        for row in connection.execute(
            """
            SELECT flag.flag_code,flag.status,flag.rollout_percentage_bps,
              flag.kill_generation,version.version_number
            FROM feature_flags flag JOIN feature_flag_versions version
              ON version.feature_flag_id=flag.feature_flag_id
             AND version.feature_flag_version_id=flag.current_version_id
            ORDER BY flag.flag_code
            """
        ).fetchall()
    ]
    updated_security = {
        "workspace": {"status": str(state["status"]), "revision": int(state["revision"])},
        "provider_key_refs": refs,
        "feature_capabilities": features,
        "data_governance": _data_governance_projection(
            connection,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
        ),
    }
    return current_watermark + (1 if _digest(current_security) != _digest(updated_security) else 0)


def _data_governance_projection(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
) -> dict[str, object] | None:
    table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='residency_policies'"
    ).fetchone()
    if table is None:
        return None
    policy = connection.execute(
        "SELECT * FROM residency_policies WHERE workspace_id=? AND tenant_id=? "
        "AND status='active'",
        (workspace_id, tenant_id),
    ).fetchone()
    if policy is None:
        raise ApiError("config_distribution.workspace_unavailable")
    retention_rows = connection.execute(
        "SELECT classification_code,retention_days,deletion_mode,backup_grace_days,revision "
        "FROM retention_policies WHERE workspace_id=? AND tenant_id=? AND status='active' "
        "AND generation=? ORDER BY classification_code",
        (workspace_id, tenant_id, policy["generation"]),
    ).fetchall()
    if len(retention_rows) != 6:
        raise ApiError("config_distribution.workspace_unavailable")
    try:
        allowed_groups = json.loads(str(policy["allowed_residency_groups_json"]))
    except json.JSONDecodeError:
        raise ApiError("config_distribution.workspace_unavailable") from None
    if (
        type(allowed_groups) is not list
        or not allowed_groups
        or any(type(group) is not str or not group for group in allowed_groups)
        or len(allowed_groups) != len(set(allowed_groups))
        or str(policy["primary_residency_group"]) not in allowed_groups
    ):
        raise ApiError("config_distribution.workspace_unavailable")
    return {
        "schema_version": 1,
        "policy_id": str(policy["residency_policy_id"]),
        "generation": int(policy["generation"]),
        "sha256": str(policy["policy_sha256"]),
        "allowed_residency_groups": sorted(allowed_groups),
        "primary_residency_group": str(policy["primary_residency_group"]),
        "cross_region_mode": str(policy["cross_region_mode"]),
        "valid_until": str(policy["valid_until"]),
        "retention": [
            {
                "classification_code": str(row["classification_code"]),
                "retention_days": int(row["retention_days"]),
                "deletion_mode": str(row["deletion_mode"]),
                "backup_grace_days": int(row["backup_grace_days"]),
                "revision": int(row["revision"]),
            }
            for row in retention_rows
        ],
    }


def _head_manifest(connection, config: AppConfig, head) -> dict[str, object]:
    row = connection.execute(
        "SELECT * FROM config_snapshots WHERE config_snapshot_id=?", (head["config_snapshot_id"],)
    ).fetchone()
    value = open_config_value(
        config,
        str(row["manifest_envelope"]),
        expected_sha256=str(row["manifest_sha256"]),
        owner_id=str(row["config_snapshot_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        kind="manifest",
    )
    if type(value) is not dict:
        raise ApiError("config_distribution.envelope_invalid")
    return value


def _snapshot_wire(
    connection,
    config: AppConfig,
    *,
    row,
    after_generation: int | None,
    request_id: str,
    trace_id: str,
) -> dict[str, object]:
    common: dict[str, object] = {
        "wire_schema_version": 1,
        "snapshot_id": str(row["config_snapshot_id"]),
        "workspace_id": str(row["workspace_id"]),
        "generation": int(row["generation"]),
        "revocation_watermark": int(row["revocation_watermark"]),
        "manifest_sha256": str(row["manifest_sha256"]),
        "kms_key_version": str(row["kms_key_version"]),
        "signature_algorithm": str(row["signature_algorithm"]),
        "signature": str(row["signature_b64"]),
        "published_at": str(row["snapshot_published_at"]),
        "stale_policy": {"lkg_max_seconds": 30, "revocation_max_seconds": 2},
        "request_id": request_id,
        "trace_id": trace_id,
    }
    delta = None
    if after_generation is not None and after_generation > 0:
        delta = connection.execute(
            "SELECT * FROM config_deltas WHERE workspace_id=? AND from_generation=? "
            "AND to_generation=? AND from_manifest_sha256 IS NOT NULL",
            (row["workspace_id"], after_generation, row["generation"]),
        ).fetchone()
    if delta is not None:
        patch = open_config_value(
            config,
            str(delta["patch_envelope"]),
            expected_sha256=str(delta["patch_sha256"]),
            owner_id=str(delta["config_delta_id"]),
            workspace_id=str(delta["workspace_id"]),
            tenant_id=str(delta["tenant_id"]),
            kind="delta",
        )
        return {
            **common,
            "kind": "delta",
            "from_generation": int(delta["from_generation"]),
            "from_manifest_sha256": str(delta["from_manifest_sha256"]),
            "delta": patch,
        }
    manifest = open_config_value(
        config,
        str(row["manifest_envelope"]),
        expected_sha256=str(row["manifest_sha256"]),
        owner_id=str(row["config_snapshot_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        kind="manifest",
    )
    return {**common, "kind": "full", "manifest": manifest}


def _observe_watermark(connection, snapshot, gateway, config: AppConfig, *, now: datetime) -> None:
    identity_sha256 = _gateway_identity_digest(config, str(gateway["worker_id"]))
    existing = _watermark_or_none(connection, str(snapshot["workspace_id"]), identity_sha256)
    if existing is None:
        connection.execute(
            """
            INSERT INTO cache_watermarks(
              cache_watermark_id,workspace_id,tenant_id,gateway_cell_id,
              gateway_identity_sha256,observed_generation,applied_generation,lkg_generation,
              observed_revocation_watermark,applied_revocation_watermark,stale_reason,
              revision,last_observed_at,last_applied_at)
            VALUES(?,?,?,?,?,?,0,0,?,0,'none',1,?,NULL)
            """,
            (
                _id("cwm_"),
                snapshot["workspace_id"],
                snapshot["tenant_id"],
                gateway["gateway_cell_id"],
                identity_sha256,
                snapshot["generation"],
                snapshot["revocation_watermark"],
                _db_time(now),
            ),
        )
        return
    connection.execute(
        """
        UPDATE cache_watermarks SET observed_generation=max(observed_generation,?),
          observed_revocation_watermark=max(observed_revocation_watermark,?),
          revision=revision+1,last_observed_at=? WHERE cache_watermark_id=?
        """,
        (
            snapshot["generation"],
            snapshot["revocation_watermark"],
            _db_time(now),
            existing["cache_watermark_id"],
        ),
    )


def _gateway_owner(connection, gateway_identity: str):
    rows = connection.execute(
        """
        SELECT lease.worker_id,lease.gateway_cell_id,cell.cell_code
        FROM gateway_config_snapshot_leases lease
        JOIN gateway_cells cell ON cell.gateway_cell_id=lease.gateway_cell_id
        WHERE lease.worker_id=? AND lease.status='active'
          AND lease.expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')
          AND cell.status='active'
        ORDER BY lease.generation DESC
        """,
        (gateway_identity,),
    ).fetchall()
    owners = {(str(row["gateway_cell_id"]), str(row["cell_code"])) for row in rows}
    if len(owners) != 1:
        raise ApiError("config_distribution.gateway_identity_invalid")
    return rows[0]


def _watermark(connection, workspace_id: str, identity_sha256: str):
    row = _watermark_or_none(connection, workspace_id, identity_sha256)
    if row is None:
        raise ApiError("config_distribution.ack_invalid")
    return row


def _watermark_or_none(connection, workspace_id: str, identity_sha256: str):
    return connection.execute(
        "SELECT * FROM cache_watermarks WHERE workspace_id=? AND gateway_identity_sha256=?",
        (workspace_id, identity_sha256),
    ).fetchone()


def _latest_rebuild(connection, workspace_id: str) -> ConfigRebuildInfo | None:
    row = connection.execute(
        "SELECT * FROM config_rebuild_jobs WHERE workspace_id=? "
        "ORDER BY created_at DESC,config_rebuild_job_id DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    return _rebuild_info(row) if row is not None else None


def _rebuild_info(row) -> ConfigRebuildInfo:
    return ConfigRebuildInfo(
        rebuild_job_id=str(row["config_rebuild_job_id"]),
        workspace_id=str(row["workspace_id"]),
        target_generation=int(row["target_generation"]),
        platform_job_id=str(row["platform_job_id"]),
        status=str(row["status"]),
        result_manifest_sha256=row["result_manifest_sha256"],
        revision=int(row["revision"]),
        created_at=_time(row["created_at"]),
        completed_at=_optional_time(row["completed_at"]),
    )


def _ack_response(
    ack,
    watermark,
    *,
    request_id: str,
    trace_id: str,
    replay: bool,
) -> ConfigAckResponse:
    return ConfigAckResponse(
        ack_id=str(ack["config_ack_id"]),
        disposition=str(ack["disposition"]),
        observed_generation=int(watermark["observed_generation"]),
        applied_generation=int(watermark["applied_generation"]),
        lkg_generation=int(watermark["lkg_generation"]),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _event(
    connection,
    *,
    event_type: str,
    workspace_id: str,
    tenant_id: str,
    generation: int,
    manifest_sha256: str,
    gateway_cell_id: str | None,
    reason: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO config_event_outbox(
          config_event_outbox_id,event_type,workspace_id,tenant_id,generation,
          manifest_sha256,gateway_cell_id,reason_code,request_id,trace_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("ceo_"),
            event_type,
            workspace_id,
            tenant_id,
            generation,
            manifest_sha256,
            gateway_cell_id,
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
        VALUES(?,'platform',?,?,'config_rebuild',?,?,?,?,NULL,?,?)
        """,
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


def _valid_workspace(value: str) -> None:
    if _WORKSPACE_ID_RE.fullmatch(value) is None:
        raise ApiError("config_distribution.snapshot_unavailable")


def _valid_gateway_identity(value: str) -> None:
    if _GATEWAY_ID_RE.fullmatch(value) is None:
        raise ApiError("config_distribution.gateway_identity_invalid")


def _require(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("config_distribution.unavailable") from None


def _rollback(connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


def _gateway_identity_digest(config: AppConfig, value: str) -> str:
    return _secret_digest(config, "gateway-identity", value)


def _secret_digest(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd108/{purpose}/v1\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _database_now(connection) -> datetime:
    value = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    return _time(value)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


__all__ = [
    "acknowledge_internal_config",
    "build_internal_config_response",
    "create_config_rebuild",
    "list_config_distribution",
    "publish_rebuild_in_transaction",
]
