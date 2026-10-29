"""Transactional PRD-122 Edge telemetry, policy, cursor, and safe projection service."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timedelta

from metahub_shared.private_gateway import (
    EdgeFailureTelemetry,
    EdgeHealthTelemetry,
    EdgeOperationalTelemetry,
    EdgeTelemetryBatch,
    EdgeTelemetryError,
    EdgeTelemetryReceiptWire,
    EdgeUsageTelemetry,
    canonical_telemetry_bytes,
    telemetry_batch_sha256,
    validate_telemetry_batch,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .desired_state_service import _authenticated_agent
from .fleet_diagnostic_service import refresh_edge_health_projection_in_transaction
from .models import (
    EdgeAgentTelemetryWatermarkResponse,
    EdgeFailureCountSummary,
    EdgeTelemetryCursorSummary,
    EdgeTelemetryPolicyPatchRequest,
    EdgeTelemetryPolicyResponse,
    EdgeTelemetryPolicySummary,
    EdgeTelemetryWatermarkResponse,
    EdgeUsageTotals,
)
from .service import (
    _connect,
    _datetime,
    _db_time,
    _id,
    _identifier,
    _owned_deployment,
    _rollback,
    _utcnow,
    _workspace_idempotency,
)

_CLOCK_SKEW = timedelta(minutes=5)
_MAX_OFFLINE_AGE = timedelta(days=8)
_MAX_BUCKET = timedelta(hours=1)
_LAG_EVENT_SECONDS = 60


def receive_agent_telemetry(
    config: AppConfig,
    batch: EdgeTelemetryBatch,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeTelemetryReceiptWire:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection,
            config,
            deployment_id=batch.deployment_id,
            verified_fingerprint=verified_fingerprint,
            edge_session_token=edge_session_token,
        )
        _require_telemetry_capability(agent)
        policy = _policy_revision(connection, batch.deployment_id, batch.policy_revision)
        try:
            validate_telemetry_batch(batch, str(policy["telemetry_level"]))
        except EdgeTelemetryError:
            raise ApiError("edge.request_invalid") from None
        payload_sha256 = telemetry_batch_sha256(batch)
        existing = connection.execute(
            """
            SELECT batch.*,receipt.edge_telemetry_receipt_id,
              receipt.contiguous_sequence receipt_contiguous_sequence,
              receipt.expected_sequence receipt_expected_sequence,
              receipt.highest_seen_sequence receipt_highest_seen_sequence
            FROM edge_telemetry_batches batch
            LEFT JOIN edge_telemetry_receipts receipt
              ON receipt.edge_telemetry_batch_id=batch.edge_telemetry_batch_id
            WHERE batch.edge_deployment_id=? AND batch.stream_code=?
              AND batch.epoch_id=? AND batch.sequence=?
            """,
            (batch.deployment_id, batch.stream, batch.epoch_id, batch.sequence),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["payload_sha256"]), payload_sha256):
                raise ApiError("edge.idempotency_conflict")
            if existing["edge_telemetry_receipt_id"] is None:
                raise ApiError("edge.unavailable")
            connection.rollback()
            return EdgeTelemetryReceiptWire(
                receipt_id=str(existing["edge_telemetry_receipt_id"]),
                batch_id=str(existing["edge_telemetry_batch_id"]),
                deployment_id=batch.deployment_id,
                stream=batch.stream,
                epoch_id=batch.epoch_id,
                sequence=batch.sequence,
                disposition="duplicate",
                contiguous_sequence=int(existing["receipt_contiguous_sequence"]),
                expected_sequence=int(existing["receipt_expected_sequence"]),
                highest_seen_sequence=int(existing["receipt_highest_seen_sequence"]),
                policy_level=str(policy["telemetry_level"]),
                policy_revision=int(policy["revision"]),
                received_at=str(existing["received_at"]),
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )

        _validate_times(batch)
        current_policy = _current_policy(connection, batch.deployment_id)
        try:
            validate_telemetry_batch(batch, str(current_policy["telemetry_level"]))
        except EdgeTelemetryError:
            raise ApiError("edge.request_invalid") from None

        now = _utcnow()
        now_db = _db_time(now)
        cursor = _cursor_for_batch(connection, batch, now_db)
        batch_id = _id("etb_")
        encoded = canonical_telemetry_bytes(batch.model_dump(mode="json"))
        starts = [record.bucket_start for record in batch.records]
        ends = [record.bucket_end for record in batch.records]
        connection.execute(
            """
            INSERT INTO edge_telemetry_batches(
              edge_telemetry_batch_id,workspace_id,tenant_id,edge_deployment_id,
              edge_device_identity_id,edge_device_certificate_id,edge_session_id,
              stream_code,epoch_id,previous_epoch_id,sequence,edge_telemetry_policy_id,
              policy_revision,schema_version,payload_rfc8785,payload_sha256,record_count,
              first_bucket_at,last_bucket_at,sent_at,received_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                batch_id,
                agent["workspace_id"],
                agent["tenant_id"],
                batch.deployment_id,
                agent["edge_device_identity_id"],
                agent["edge_device_certificate_id"],
                agent["edge_session_id"],
                batch.stream,
                batch.epoch_id,
                batch.previous_epoch_id,
                batch.sequence,
                policy["edge_telemetry_policy_id"],
                batch.policy_revision,
                batch.schema_version,
                encoded.decode("utf-8"),
                payload_sha256,
                len(batch.records),
                min(starts),
                max(ends),
                batch.sent_at,
                now_db,
            ),
        )
        usage_count = _persist_records(connection, agent, batch_id, batch, now_db)
        cursor = _advance_cursor(connection, cursor, batch, now_db)
        receipt_id = _id("etr_")
        receipt = _receipt_wire(
            receipt_id=receipt_id,
            batch_id=batch_id,
            batch=batch,
            cursor=cursor,
            policy=policy,
            disposition="accepted",
            received_at=now_db,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.execute(
            """
            INSERT INTO edge_telemetry_receipts(
              edge_telemetry_receipt_id,workspace_id,tenant_id,edge_deployment_id,
              edge_telemetry_batch_id,edge_usage_sync_cursor_id,contiguous_sequence,
              expected_sequence,highest_seen_sequence,receipt_sha256,received_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt_id,
                agent["workspace_id"],
                agent["tenant_id"],
                batch.deployment_id,
                batch_id,
                cursor["edge_usage_sync_cursor_id"],
                cursor["contiguous_sequence"],
                int(cursor["contiguous_sequence"]) + 1,
                cursor["highest_seen_sequence"],
                hashlib.sha256(
                    canonical_telemetry_bytes(receipt.model_dump(mode="json"))
                ).hexdigest(),
                now_db,
            ),
        )
        lag_seconds, backlog_seconds = _update_fleet_projection(
            connection, batch, now, now_db
        )
        if _table_exists(connection, "edge_health_snapshots"):
            refresh_edge_health_projection_in_transaction(
                connection,
                batch.deployment_id,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        _event(
            connection,
            agent,
            batch_id=batch_id,
            policy_id=str(policy["edge_telemetry_policy_id"]),
            actor_type="agent",
            actor_account_id=None,
            event_type="edge.telemetry_received",
            stream=batch.stream,
            sequence=batch.sequence,
            reason="batch_accepted",
            delivery="fleet",
            request_id=request_id,
            trace_id=trace_id,
            now=now_db,
        )
        if usage_count:
            _event(
                connection,
                agent,
                batch_id=batch_id,
                policy_id=str(policy["edge_telemetry_policy_id"]),
                actor_type="system",
                actor_account_id=None,
                event_type="edge.usage_reconciled",
                stream="usage",
                sequence=batch.sequence,
                reason="usage_committed",
                delivery="billing",
                request_id=request_id,
                trace_id=trace_id,
                now=now_db,
            )
        if lag_seconds > _LAG_EVENT_SECONDS:
            _event(
                connection,
                agent,
                batch_id=batch_id,
                policy_id=str(policy["edge_telemetry_policy_id"]),
                actor_type="system",
                actor_account_id=None,
                event_type="edge.telemetry_lagging",
                stream=batch.stream,
                sequence=batch.sequence,
                reason="lag_threshold_exceeded",
                delivery="fleet",
                request_id=request_id,
                trace_id=trace_id,
                now=now_db,
            )
        connection.commit()
        return receipt
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error:
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def get_workspace_telemetry_watermark(
    config: AppConfig,
    deployment_id: str,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeTelemetryWatermarkResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            touch=False,
        )
        can_read_telemetry = "edge.telemetry.read" in context.permission_codes
        can_read_usage = "edge.usage.read" in context.permission_codes
        if not (can_read_telemetry or can_read_usage):
            raise ApiError("workspace_session.permission_denied")
        _owned_deployment(connection, context, deployment_id)
        policy = _current_policy(connection, deployment_id)
        projection = connection.execute(
            "SELECT * FROM edge_fleet_telemetry_projections WHERE edge_deployment_id=?",
            (deployment_id,),
        ).fetchone()
        cursors = (
            tuple(
                _cursor_summary(row)
                for row in connection.execute(
                    """
                    SELECT * FROM edge_usage_sync_cursors
                    WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
                      AND status='active' ORDER BY stream_code
                    """,
                    (context.workspace_id, context.tenant_id, deployment_id),
                ).fetchall()
            )
            if can_read_telemetry
            else ()
        )
        usage = _usage_totals(connection, context, deployment_id) if can_read_usage else None
        failures = (
            _failure_totals(connection, context, deployment_id) if can_read_telemetry else ()
        )
        connection.rollback()
        return EdgeTelemetryWatermarkResponse(
            deployment_id=deployment_id,
            policy=_policy_summary(policy) if can_read_telemetry else None,
            cursors=cursors,
            usage=usage,
            failures=failures,
            last_receipt_at=(
                _datetime(projection["last_receipt_at"])
                if projection is not None and can_read_telemetry
                else None
            ),
            lag_seconds=(
                int(projection["lag_seconds"])
                if projection is not None and can_read_telemetry
                else None
            ),
            backlog_seconds=(
                int(projection["backlog_seconds"])
                if projection is not None and can_read_telemetry
                else None
            ),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except sqlite3.Error:
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def get_agent_telemetry_watermark(
    config: AppConfig,
    deployment_id: str,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeAgentTelemetryWatermarkResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        agent = _authenticated_agent(
            connection,
            config,
            deployment_id=deployment_id,
            verified_fingerprint=verified_fingerprint,
            edge_session_token=edge_session_token,
        )
        _require_telemetry_capability(agent)
        policy = _current_policy(connection, deployment_id)
        cursors = tuple(
            _cursor_summary(row)
            for row in connection.execute(
                """
                SELECT * FROM edge_usage_sync_cursors
                WHERE edge_deployment_id=? AND status='active' ORDER BY stream_code
                """,
                (deployment_id,),
            ).fetchall()
        )
        connection.rollback()
        return EdgeAgentTelemetryWatermarkResponse(
            deployment_id=deployment_id,
            policy=_policy_summary(policy),
            cursors=cursors,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except sqlite3.Error:
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def patch_workspace_telemetry_policy(
    config: AppConfig,
    deployment_id: str,
    payload: EdgeTelemetryPolicyPatchRequest,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeTelemetryPolicyResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context_for_permission(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.telemetry.manage",
        )
        _owned_deployment(connection, context, deployment_id)
        head = _current_policy(connection, deployment_id)
        digest = _workspace_idempotency(
            config, context, "telemetry_policy_patch", idempotency_key
        )
        request_sha256 = _digest(
            {"deployment_id": deployment_id, "telemetry_level": payload.telemetry_level}
        )
        replay = connection.execute(
            """
            SELECT command.request_sha256,policy.*
            FROM workspace_edge_telemetry_commands command
            JOIN edge_telemetry_policies policy
              ON policy.edge_telemetry_policy_id=command.result_policy_id
            WHERE command.workspace_id=? AND command.tenant_id=?
              AND command.actor_account_id=? AND command.action_code='telemetry_policy_patch'
              AND command.idempotency_sha256=?
            """,
            (context.workspace_id, context.tenant_id, context.account_id, digest),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("edge.idempotency_conflict")
            connection.rollback()
            return EdgeTelemetryPolicyResponse(
                policy=_policy_summary(replay),
                idempotent_replay=True,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
        if expected_revision != int(head["revision"]):
            raise ApiError("edge.revision_conflict")
        now = _utcnow()
        now_db = _changed_time(now, head["updated_at"])
        if str(head["telemetry_level"]) == payload.telemetry_level:
            policy = head
        else:
            policy_id = _id("etp_")
            next_revision = int(head["revision"]) + 1
            connection.execute(
                """
                INSERT INTO edge_telemetry_policies(
                  edge_telemetry_policy_id,workspace_id,tenant_id,edge_deployment_id,
                  revision,telemetry_level,source_code,actor_account_id,
                  idempotency_sha256,request_sha256,created_at)
                VALUES(?,?,?,?,?,?,'workspace_policy',?,?,?,?)
                """,
                (
                    policy_id,
                    context.workspace_id,
                    context.tenant_id,
                    deployment_id,
                    next_revision,
                    payload.telemetry_level,
                    context.account_id,
                    digest,
                    request_sha256,
                    now_db,
                ),
            )
            connection.execute(
                """
                UPDATE edge_telemetry_policy_heads
                SET edge_telemetry_policy_id=?,revision=?,telemetry_level=?,updated_at=?
                WHERE edge_deployment_id=? AND revision=?
                """,
                (
                    policy_id,
                    next_revision,
                    payload.telemetry_level,
                    now_db,
                    deployment_id,
                    expected_revision,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ApiError("edge.revision_conflict")
            policy = _current_policy(connection, deployment_id)
        connection.execute(
            """
            INSERT INTO workspace_edge_telemetry_commands(
              workspace_edge_telemetry_command_id,workspace_id,tenant_id,
              edge_deployment_id,actor_account_id,action_code,idempotency_sha256,
              request_sha256,result_policy_id,created_at)
            VALUES(?,?,?,?,?,'telemetry_policy_patch',?,?,?,?)
            """,
            (
                _id("etc_"),
                context.workspace_id,
                context.tenant_id,
                deployment_id,
                context.account_id,
                digest,
                request_sha256,
                policy["edge_telemetry_policy_id"],
                now_db,
            ),
        )
        connection.commit()
        return EdgeTelemetryPolicyResponse(
            policy=_policy_summary(policy),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error:
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def _policy_revision(
    connection: sqlite3.Connection, deployment_id: str, revision: int
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM edge_telemetry_policies
        WHERE edge_deployment_id=? AND revision=?
        """,
        (deployment_id, revision),
    ).fetchone()
    if row is None:
        raise ApiError("edge.state_conflict")
    return row


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _require_telemetry_capability(agent: sqlite3.Row) -> None:
    try:
        capabilities = json.loads(str(agent["capability_codes_json"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ApiError("edge.authentication_failed") from None
    if (
        type(capabilities) is not list
        or any(type(value) is not str for value in capabilities)
        or "telemetry" not in capabilities
    ):
        raise ApiError("edge.authentication_failed")


def _current_policy(connection: sqlite3.Connection, deployment_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT policy.*,head.updated_at
        FROM edge_telemetry_policy_heads head
        JOIN edge_telemetry_policies policy
          ON policy.edge_telemetry_policy_id=head.edge_telemetry_policy_id
        WHERE head.edge_deployment_id=?
        """,
        (deployment_id,),
    ).fetchone()
    if row is None:
        raise ApiError("edge.unavailable")
    return row


def _cursor_for_batch(
    connection: sqlite3.Connection, batch: EdgeTelemetryBatch, now: str
) -> sqlite3.Row:
    active = _active_cursor(connection, batch.deployment_id, batch.stream, required=False)
    if active is None:
        if batch.previous_epoch_id is not None:
            raise ApiError("edge.state_conflict")
        return _insert_cursor(connection, batch, now)
    if str(active["epoch_id"]) == batch.epoch_id:
        expected_previous = active["previous_epoch_id"]
        if batch.previous_epoch_id not in {None, expected_previous}:
            raise ApiError("edge.state_conflict")
        return active
    if (
        batch.previous_epoch_id != active["epoch_id"]
        or batch.sequence != 1
        or int(active["contiguous_sequence"]) != int(active["highest_seen_sequence"])
    ):
        raise ApiError("edge.state_conflict")
    changed = _changed_time(_datetime(now), active["updated_at"])
    connection.execute(
        """
        UPDATE edge_usage_sync_cursors
        SET status='superseded',closed_at=?,revision=revision+1,updated_at=?
        WHERE edge_usage_sync_cursor_id=? AND status='active'
        """,
        (changed, changed, active["edge_usage_sync_cursor_id"]),
    )
    return _insert_cursor(connection, batch, changed)


def _insert_cursor(
    connection: sqlite3.Connection, batch: EdgeTelemetryBatch, now: str
) -> sqlite3.Row:
    cursor_id = _id("euc_")
    owner = connection.execute(
        "SELECT workspace_id,tenant_id FROM edge_deployments WHERE edge_deployment_id=?",
        (batch.deployment_id,),
    ).fetchone()
    if owner is None:
        raise ApiError("edge.authentication_failed")
    connection.execute(
        """
        INSERT INTO edge_usage_sync_cursors(
          edge_usage_sync_cursor_id,workspace_id,tenant_id,edge_deployment_id,
          stream_code,epoch_id,previous_epoch_id,status,contiguous_sequence,
          highest_seen_sequence,revision,opened_at,closed_at,updated_at)
        VALUES(?,?,?,?,?,?,?,'active',0,0,1,?,NULL,?)
        """,
        (
            cursor_id,
            owner["workspace_id"],
            owner["tenant_id"],
            batch.deployment_id,
            batch.stream,
            batch.epoch_id,
            batch.previous_epoch_id,
            now,
            now,
        ),
    )
    return _cursor_by_id(connection, cursor_id)


def _advance_cursor(
    connection: sqlite3.Connection,
    cursor: sqlite3.Row,
    batch: EdgeTelemetryBatch,
    now: str,
) -> sqlite3.Row:
    contiguous = int(cursor["contiguous_sequence"])
    highest = max(int(cursor["highest_seen_sequence"]), batch.sequence)
    sequences = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT sequence FROM edge_telemetry_batches
            WHERE edge_deployment_id=? AND stream_code=? AND epoch_id=? AND sequence>?
            ORDER BY sequence
            """,
            (batch.deployment_id, batch.stream, batch.epoch_id, contiguous),
        ).fetchall()
    ]
    expected = contiguous + 1
    for sequence in sequences:
        if sequence == expected:
            contiguous = sequence
            expected += 1
        elif sequence > expected:
            break
    changed = _changed_time(_datetime(now), cursor["updated_at"])
    connection.execute(
        """
        UPDATE edge_usage_sync_cursors
        SET contiguous_sequence=?,highest_seen_sequence=?,revision=revision+1,updated_at=?
        WHERE edge_usage_sync_cursor_id=? AND revision=? AND status='active'
        """,
        (
            contiguous,
            highest,
            changed,
            cursor["edge_usage_sync_cursor_id"],
            cursor["revision"],
        ),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise ApiError("edge.state_conflict")
    return _cursor_by_id(connection, str(cursor["edge_usage_sync_cursor_id"]))


def _active_cursor(
    connection: sqlite3.Connection,
    deployment_id: str,
    stream: str,
    *,
    required: bool = True,
) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT * FROM edge_usage_sync_cursors
        WHERE edge_deployment_id=? AND stream_code=? AND status='active'
        """,
        (deployment_id, stream),
    ).fetchone()
    if row is None and required:
        raise ApiError("edge.unavailable")
    return row


def _cursor_by_id(connection: sqlite3.Connection, cursor_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_usage_sync_cursors WHERE edge_usage_sync_cursor_id=?",
        (cursor_id,),
    ).fetchone()
    if row is None:
        raise ApiError("edge.unavailable")
    return row


def _persist_records(
    connection: sqlite3.Connection,
    owner: sqlite3.Row,
    batch_id: str,
    batch: EdgeTelemetryBatch,
    now: str,
) -> int:
    usage_count = 0
    for index, record in enumerate(batch.records):
        if isinstance(record, EdgeUsageTelemetry):
            connection.execute(
                """
                INSERT INTO edge_usage_reconciliations(
                  edge_usage_reconciliation_id,workspace_id,tenant_id,edge_deployment_id,
                  edge_telemetry_batch_id,record_index,epoch_id,sequence,model_id,provider_id,
                  request_count,input_tokens,output_tokens,cache_tokens,cost_cny_micros,
                  cost_usd_micros,bucket_start,bucket_end,reconciled_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _id("eur_"),
                    owner["workspace_id"],
                    owner["tenant_id"],
                    batch.deployment_id,
                    batch_id,
                    index,
                    batch.epoch_id,
                    batch.sequence,
                    record.model_id,
                    record.provider_id,
                    record.request_count,
                    record.input_tokens,
                    record.output_tokens,
                    record.cache_tokens,
                    record.cost_cny_micros,
                    record.cost_usd_micros,
                    record.bucket_start,
                    record.bucket_end,
                    now,
                ),
            )
            usage_count += 1
        elif isinstance(record, EdgeFailureTelemetry):
            connection.execute(
                """
                INSERT INTO edge_failure_summaries(
                  edge_failure_summary_id,workspace_id,tenant_id,edge_deployment_id,
                  edge_telemetry_batch_id,record_index,component_code,failure_code,
                  failure_count,bucket_start,bucket_end,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _id("efs_"),
                    owner["workspace_id"],
                    owner["tenant_id"],
                    batch.deployment_id,
                    batch_id,
                    index,
                    record.component_code,
                    record.failure_code,
                    record.count,
                    record.bucket_start,
                    record.bucket_end,
                    now,
                ),
            )
    return usage_count


def _update_fleet_projection(
    connection: sqlite3.Connection,
    batch: EdgeTelemetryBatch,
    now: datetime,
    now_db: str,
) -> tuple[int, int]:
    starts = [_datetime(record.bucket_start) for record in batch.records]
    ends = [_datetime(record.bucket_end) for record in batch.records]
    incoming_last_bucket = max(ends)
    backlog = max(0, int((_datetime(batch.sent_at) - min(starts)).total_seconds()))
    current = connection.execute(
        "SELECT * FROM edge_fleet_telemetry_projections WHERE edge_deployment_id=?",
        (batch.deployment_id,),
    ).fetchone()
    cpu_bucket = str(current["cpu_bucket"]) if current else "unknown"
    memory_bucket = str(current["memory_bucket"]) if current else "unknown"
    error_rate = int(current["error_rate_basis_points"]) if current else 0
    failure_code = str(current["last_failure_code"]) if current else "none"
    health_bucket_at = _optional_datetime(current, "health_bucket_at")
    operational_bucket_at = _optional_datetime(current, "operational_bucket_at")
    failure_bucket_at = _optional_datetime(current, "failure_bucket_at")
    for record in batch.records:
        record_bucket_at = _datetime(record.bucket_end)
        if isinstance(record, EdgeHealthTelemetry) and (
            health_bucket_at is None or record_bucket_at >= health_bucket_at
        ):
            duration_ms = max(
                int((_datetime(record.bucket_end) - _datetime(record.bucket_start)).total_seconds())
                * 1000,
                1,
            )
            cpu_bucket = _ratio_bucket(record.cpu_millis * 10_000 // duration_ms)
            memory_bucket = _memory_bucket(record.memory_peak_bytes)
            health_bucket_at = record_bucket_at
        elif isinstance(record, EdgeOperationalTelemetry) and (
            operational_bucket_at is None or record_bucket_at >= operational_bucket_at
        ):
            error_rate = (
                record.failure_count * 10_000 // record.request_count
                if record.request_count
                else 0
            )
            operational_bucket_at = record_bucket_at
        elif isinstance(record, EdgeFailureTelemetry) and (
            failure_bucket_at is None or record_bucket_at >= failure_bucket_at
        ):
            failure_code = record.failure_code
            failure_bucket_at = record_bucket_at
    last_bucket_at = (
        max(incoming_last_bucket, _datetime(current["last_bucket_at"]))
        if current
        else incoming_last_bucket
    )
    lag = max(0, int((now - last_bucket_at).total_seconds()))
    if current is None:
        connection.execute(
            """
            INSERT INTO edge_fleet_telemetry_projections(
              edge_deployment_id,revision,last_receipt_at,last_bucket_at,lag_seconds,
              backlog_seconds,cpu_bucket,memory_bucket,error_rate_basis_points,
              last_failure_code,health_bucket_at,operational_bucket_at,
              failure_bucket_at,updated_at) VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                batch.deployment_id,
                now_db,
                _db_time(last_bucket_at),
                lag,
                backlog,
                cpu_bucket,
                memory_bucket,
                error_rate,
                failure_code,
                _optional_db_time(health_bucket_at),
                _optional_db_time(operational_bucket_at),
                _optional_db_time(failure_bucket_at),
                now_db,
            ),
        )
    else:
        changed = _changed_time(now, current["updated_at"])
        connection.execute(
            """
            UPDATE edge_fleet_telemetry_projections SET revision=revision+1,
              last_receipt_at=?,last_bucket_at=?,lag_seconds=?,backlog_seconds=?,
              cpu_bucket=?,memory_bucket=?,error_rate_basis_points=?,last_failure_code=?,
              health_bucket_at=?,operational_bucket_at=?,failure_bucket_at=?,updated_at=?
            WHERE edge_deployment_id=?
            """,
            (
                now_db,
                _db_time(last_bucket_at),
                lag,
                backlog,
                cpu_bucket,
                memory_bucket,
                error_rate,
                failure_code,
                _optional_db_time(health_bucket_at),
                _optional_db_time(operational_bucket_at),
                _optional_db_time(failure_bucket_at),
                changed,
                batch.deployment_id,
            ),
        )
    return lag, backlog


def _optional_datetime(row: sqlite3.Row | None, key: str) -> datetime | None:
    if row is None or row[key] is None:
        return None
    return _datetime(str(row[key]))


def _optional_db_time(value: datetime | None) -> str | None:
    return _db_time(value) if value is not None else None


def _event(
    connection: sqlite3.Connection,
    owner: sqlite3.Row,
    *,
    batch_id: str | None,
    policy_id: str | None,
    actor_type: str,
    actor_account_id: str | None,
    event_type: str,
    stream: str | None,
    sequence: int | None,
    reason: str,
    delivery: str,
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    event_id = _id("ete_")
    connection.execute(
        """
        INSERT INTO edge_telemetry_events(
          edge_telemetry_event_id,workspace_id,tenant_id,edge_deployment_id,
          edge_telemetry_batch_id,edge_telemetry_policy_id,actor_type,actor_account_id,
          event_type,stream_code,sequence,reason_code,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            owner["workspace_id"],
            owner["tenant_id"],
            owner["edge_deployment_id"],
            batch_id,
            policy_id,
            actor_type,
            actor_account_id,
            event_type,
            stream,
            sequence,
            reason,
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO edge_telemetry_event_outbox(
          edge_telemetry_outbox_id,edge_telemetry_event_id,delivery_domain,status,
          attempt_count,available_at,delivered_at,last_error_code,created_at,updated_at)
        VALUES(?,?,?,'pending',0,?,NULL,NULL,?,?)
        """,
        (_id("eto_"), event_id, delivery, now, now, now),
    )


def _validate_times(batch: EdgeTelemetryBatch) -> None:
    now = _utcnow()
    sent = _datetime(batch.sent_at)
    if sent > now + _CLOCK_SKEW or sent < now - _MAX_OFFLINE_AGE:
        raise ApiError("edge.request_invalid")
    for record in batch.records:
        start = _datetime(record.bucket_start)
        end = _datetime(record.bucket_end)
        if end - start > _MAX_BUCKET or end > now + _CLOCK_SKEW or start < now - _MAX_OFFLINE_AGE:
            raise ApiError("edge.request_invalid")


def _usage_totals(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    deployment_id: str,
) -> EdgeUsageTotals:
    row = connection.execute(
        """
        SELECT COALESCE(SUM(request_count),0),COALESCE(SUM(input_tokens),0),
          COALESCE(SUM(output_tokens),0),COALESCE(SUM(cache_tokens),0),
          COALESCE(SUM(cost_cny_micros),0),COALESCE(SUM(cost_usd_micros),0)
        FROM edge_usage_reconciliations
        WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
        """,
        (context.workspace_id, context.tenant_id, deployment_id),
    ).fetchone()
    return EdgeUsageTotals(
        request_count=int(row[0]),
        input_tokens=int(row[1]),
        output_tokens=int(row[2]),
        cache_tokens=int(row[3]),
        cost_cny_micros=int(row[4]),
        cost_usd_micros=int(row[5]),
    )


def _failure_totals(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    deployment_id: str,
) -> tuple[EdgeFailureCountSummary, ...]:
    return tuple(
        EdgeFailureCountSummary(
            component_code=str(row["component_code"]),
            failure_code=str(row["failure_code"]),
            count=int(row["failure_count"]),
        )
        for row in connection.execute(
            """
            SELECT component_code,failure_code,SUM(failure_count) failure_count
            FROM edge_failure_summaries
            WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
            GROUP BY component_code,failure_code ORDER BY failure_count DESC,failure_code
            LIMIT 32
            """,
            (context.workspace_id, context.tenant_id, deployment_id),
        ).fetchall()
    )


def _policy_summary(row: sqlite3.Row) -> EdgeTelemetryPolicySummary:
    return EdgeTelemetryPolicySummary(
        policy_id=str(row["edge_telemetry_policy_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        telemetry_level=str(row["telemetry_level"]),
        revision=int(row["revision"]),
        updated_at=_datetime(
            row["updated_at"] if "updated_at" in row.keys() else row["created_at"]
        ),
    )


def _cursor_summary(row: sqlite3.Row) -> EdgeTelemetryCursorSummary:
    return EdgeTelemetryCursorSummary(
        cursor_id=str(row["edge_usage_sync_cursor_id"]),
        stream=str(row["stream_code"]),
        epoch_id=str(row["epoch_id"]),
        contiguous_sequence=int(row["contiguous_sequence"]),
        highest_seen_sequence=int(row["highest_seen_sequence"]),
        expected_sequence=int(row["contiguous_sequence"]) + 1,
        updated_at=_datetime(row["updated_at"]),
    )


def _receipt_wire(
    *,
    receipt_id: str,
    batch_id: str,
    batch: EdgeTelemetryBatch,
    cursor: sqlite3.Row,
    policy: sqlite3.Row,
    disposition: str,
    received_at: str,
    request_id: str,
    trace_id: str,
) -> EdgeTelemetryReceiptWire:
    return EdgeTelemetryReceiptWire(
        receipt_id=receipt_id,
        batch_id=batch_id,
        deployment_id=batch.deployment_id,
        stream=batch.stream,
        epoch_id=batch.epoch_id,
        sequence=batch.sequence,
        disposition=disposition,
        contiguous_sequence=int(cursor["contiguous_sequence"]),
        expected_sequence=int(cursor["contiguous_sequence"]) + 1,
        highest_seen_sequence=int(cursor["highest_seen_sequence"]),
        policy_level=str(policy["telemetry_level"]),
        policy_revision=int(policy["revision"]),
        received_at=received_at,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _workspace_context_for_permission(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    permission: str,
) -> WorkspaceSessionContext:
    context = authenticate_workspace_session_in_transaction(
        connection,
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        touch=True,
    )
    if permission not in context.permission_codes:
        raise ApiError("workspace_session.permission_denied")
    return context


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_telemetry_bytes(value)).hexdigest()


def _changed_time(value: datetime, previous: object) -> str:
    current = _db_time(value)
    if type(previous) is str and current <= previous:
        return _db_time(_datetime(previous) + timedelta(milliseconds=1))
    return current


def _ratio_bucket(basis_points: int) -> str:
    if basis_points < 2500:
        return "low"
    if basis_points < 6000:
        return "medium"
    if basis_points < 8500:
        return "high"
    return "critical"


def _memory_bucket(value: int) -> str:
    if value < 512 * 1024 * 1024:
        return "low"
    if value < 2 * 1024 * 1024 * 1024:
        return "medium"
    if value < 8 * 1024 * 1024 * 1024:
        return "high"
    return "critical"


__all__ = (
    "get_agent_telemetry_watermark",
    "get_workspace_telemetry_watermark",
    "patch_workspace_telemetry_policy",
    "receive_agent_telemetry",
)
