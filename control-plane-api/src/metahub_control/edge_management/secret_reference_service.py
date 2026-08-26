"""Cloud-safe PRD-123 reference projection and Agent synchronization."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import timedelta

from metahub_shared.db.errors import DbError
from metahub_shared.private_gateway import (
    EdgeSecretReferenceChangedEvent,
    EdgeSecretRotationFailedEvent,
    EdgeSecretSyncBatch,
    EdgeSecretSyncReceipt,
    SecretReference,
    canonical_edge_json_bytes,
    secret_reference_sha256,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.workspace_access.service import (
    authenticate_workspace_session_in_transaction,
)

from .desired_state_service import _authenticated_agent
from .models import EdgeSecretReferenceListResponse
from .service import _connect, _datetime, _db_time, _id, _identifier, _rollback, _utcnow

_CLOCK_SKEW = timedelta(minutes=5)
_MAX_OFFLINE_AGE = timedelta(days=31)


def list_workspace_secret_references(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeSecretReferenceListResponse:
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
        if "edge.secret_reference.read" not in context.permission_codes:
            raise ApiError("workspace_session.permission_denied")
        rows = connection.execute(
            """
            SELECT * FROM edge_secret_references
            WHERE workspace_id=? AND tenant_id=?
            ORDER BY updated_at DESC,reference_id DESC
            """,
            (context.workspace_id, context.tenant_id),
        ).fetchall()
        references = tuple(_reference_from_row(row) for row in rows)
        connection.rollback()
        return EdgeSecretReferenceListResponse(
            references=references,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def synchronize_agent_secret_references(
    config: AppConfig,
    batch: EdgeSecretSyncBatch,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeSecretSyncReceipt:
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
        _require_secret_reference_capability(agent)
        _validate_batch_times(batch)
        payload_sha256 = _digest(batch.model_dump(mode="json"))
        existing = connection.execute(
            """
            SELECT batch.payload_sha256,batch.event_count,receipt.*
            FROM edge_secret_sync_batches batch
            LEFT JOIN edge_secret_sync_receipts receipt
              ON receipt.edge_secret_sync_batch_id=batch.edge_secret_sync_batch_id
            WHERE batch.edge_deployment_id=? AND batch.edge_secret_sync_batch_id=?
            """,
            (batch.deployment_id, batch.batch_id),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["payload_sha256"]), payload_sha256):
                raise ApiError("edge.idempotency_conflict")
            if existing["edge_secret_sync_receipt_id"] is None:
                raise ApiError("edge.unavailable")
            connection.rollback()
            return _receipt_from_row(
                existing,
                batch_id=batch.batch_id,
                deployment_id=batch.deployment_id,
                disposition="duplicate",
                request_id=request_id,
                trace_id=trace_id,
            )

        now = _db_time(_utcnow())
        owner = (
            str(agent["workspace_id"]),
            str(agent["tenant_id"]),
            str(agent["edge_deployment_id"]),
        )
        connection.execute(
            """
            INSERT INTO edge_secret_sync_batches(
              edge_secret_sync_batch_id,workspace_id,tenant_id,edge_deployment_id,
              edge_device_identity_id,edge_device_certificate_id,edge_session_id,
              schema_version,event_count,payload_sha256,first_event_at,last_event_at,
              sent_at,received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                batch.batch_id,
                *owner,
                agent["edge_device_identity_id"],
                agent["edge_device_certificate_id"],
                agent["edge_session_id"],
                batch.schema_version,
                len(batch.events),
                payload_sha256,
                batch.events[0].occurred_at,
                batch.events[-1].occurred_at,
                batch.sent_at,
                now,
            ),
        )
        accepted = 0
        duplicates = 0
        for event in batch.events:
            event_sha256 = _digest(event.model_dump(mode="json"))
            prior = connection.execute(
                "SELECT event_payload_sha256 FROM edge_secret_events WHERE edge_secret_event_id=?",
                (event.event_id,),
            ).fetchone()
            if prior is not None:
                if not hmac.compare_digest(str(prior[0]), event_sha256):
                    raise ApiError("edge.idempotency_conflict")
                duplicates += 1
                continue
            reference_id = _apply_event_projection(connection, owner, event, now)
            _insert_event(
                connection,
                owner,
                batch.batch_id,
                event,
                reference_id=reference_id,
                event_sha256=event_sha256,
                received_at=now,
            )
            accepted += 1

        receipt_id = _id("lsa_")
        receipt_facts = {
            "accepted_event_count": accepted,
            "batch_id": batch.batch_id,
            "deployment_id": batch.deployment_id,
            "duplicate_event_count": duplicates,
            "received_at": now,
            "receipt_id": receipt_id,
            "schema_version": "1",
        }
        connection.execute(
            """
            INSERT INTO edge_secret_sync_receipts(
              edge_secret_sync_receipt_id,workspace_id,tenant_id,edge_deployment_id,
              edge_secret_sync_batch_id,accepted_event_count,duplicate_event_count,
              receipt_sha256,received_at) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt_id,
                *owner,
                batch.batch_id,
                accepted,
                duplicates,
                _digest(receipt_facts),
                now,
            ),
        )
        connection.commit()
        return EdgeSecretSyncReceipt(
            receipt_id=receipt_id,
            batch_id=batch.batch_id,
            deployment_id=batch.deployment_id,
            disposition="accepted",
            accepted_event_count=accepted,
            duplicate_event_count=duplicates,
            received_at=now,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def _apply_event_projection(connection, owner, event, received_at: str) -> str:
    if isinstance(event, EdgeSecretReferenceChangedEvent):
        reference = event.reference
        _require_owner(reference, owner)
        existing = connection.execute(
            "SELECT * FROM edge_secret_references WHERE reference_id=?",
            (reference.reference_id,),
        ).fetchone()
        digest = secret_reference_sha256(reference)
        if existing is None:
            _insert_reference(connection, reference, event.event_id, digest, received_at)
        else:
            _require_reference_identity(existing, reference)
            current_revision = int(existing["revision"])
            if reference.revision == current_revision:
                if not hmac.compare_digest(str(existing["projection_sha256"]), digest):
                    raise ApiError("edge.idempotency_conflict")
            elif reference.revision > current_revision:
                connection.execute(
                    """
                    UPDATE edge_secret_references SET status=?,revision=?,active_version=?,
                      last_test_outcome=?,capabilities_json=?,rotated_at=?,updated_at=?,
                      projection_sha256=?,source_event_id=?,received_at=? WHERE reference_id=?
                    """,
                    (
                        reference.status,
                        reference.revision,
                        reference.active_version,
                        reference.last_test_outcome,
                        _capabilities(reference.capabilities),
                        reference.rotated_at,
                        reference.updated_at,
                        digest,
                        event.event_id,
                        received_at,
                        reference.reference_id,
                    ),
                )
        return reference.reference_id

    reference_id = event.reference_id
    existing = connection.execute(
        "SELECT * FROM edge_secret_references WHERE reference_id=?",
        (reference_id,),
    ).fetchone()
    if existing is None or tuple(str(existing[key]) for key in (
        "workspace_id", "tenant_id", "edge_deployment_id"
    )) != owner:
        raise ApiError("edge.state_conflict")
    if isinstance(event, EdgeSecretRotationFailedEvent) and event.revision > int(
        existing["revision"]
    ):
        projected = _reference_from_row(
            existing,
            overrides={
                "status": "degraded",
                "revision": event.revision,
                "last_test_outcome": event.outcome,
                "capabilities": [],
                "updated_at": event.occurred_at,
            },
        )
        connection.execute(
            """
            UPDATE edge_secret_references SET status='degraded',revision=?,
              last_test_outcome=?,capabilities_json='[]',updated_at=?,projection_sha256=?,
              source_event_id=?,received_at=? WHERE reference_id=?
            """,
            (
                event.revision,
                event.outcome,
                event.occurred_at,
                secret_reference_sha256(projected),
                event.event_id,
                received_at,
                reference_id,
            ),
        )
    elif isinstance(event, EdgeSecretRotationFailedEvent) and event.revision == int(
        existing["revision"]
    ):
        if existing["status"] != "degraded" or existing["last_test_outcome"] != event.outcome:
            raise ApiError("edge.idempotency_conflict")
    return reference_id


def _insert_reference(connection, reference, event_id, digest, received_at) -> None:
    connection.execute(
        """
        INSERT INTO edge_secret_references(
          reference_id,schema_version,workspace_id,tenant_id,edge_deployment_id,
          provider_connection_id,label,kind,status,revision,active_version,last_test_outcome,
          capabilities_json,rotated_at,updated_at,projection_sha256,source_event_id,received_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            reference.reference_id,
            reference.schema_version,
            reference.workspace_id,
            reference.tenant_id,
            reference.deployment_id,
            reference.provider_connection_id,
            reference.label,
            reference.kind,
            reference.status,
            reference.revision,
            reference.active_version,
            reference.last_test_outcome,
            _capabilities(reference.capabilities),
            reference.rotated_at,
            reference.updated_at,
            digest,
            event_id,
            received_at,
        ),
    )


def _insert_event(
    connection,
    owner,
    batch_id,
    event,
    *,
    reference_id,
    event_sha256,
    received_at,
) -> None:
    revision = version = outcome = capabilities = latency = None
    if isinstance(event, EdgeSecretReferenceChangedEvent):
        revision = event.reference.revision
        outcome = event.reference.last_test_outcome
        capabilities = _capabilities(event.reference.capabilities)
    elif isinstance(event, EdgeSecretRotationFailedEvent):
        revision = event.revision
        outcome = event.outcome
    else:
        version = event.version
        outcome = event.outcome
        capabilities = _capabilities(event.capabilities)
        latency = event.latency_bucket
    connection.execute(
        """
        INSERT INTO edge_secret_events(
          edge_secret_event_id,workspace_id,tenant_id,edge_deployment_id,
          edge_secret_sync_batch_id,reference_id,event_type,revision,version,outcome,
          capabilities_json,latency_bucket,event_payload_sha256,occurred_at,received_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event.event_id,
            *owner,
            batch_id,
            reference_id,
            event.event_type,
            revision,
            version,
            outcome,
            capabilities,
            latency,
            event_sha256,
            event.occurred_at,
            received_at,
        ),
    )
    for domain in ("workspace", "security"):
        connection.execute(
            """
            INSERT INTO edge_secret_event_outbox(
              edge_secret_outbox_id,workspace_id,tenant_id,edge_deployment_id,
              edge_secret_event_id,delivery_domain,status,attempt_count,available_at,
              delivered_at,last_error_code,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'pending',0,?,NULL,NULL,?,?)
            """,
            (_id("lso_"), *owner, event.event_id, domain, received_at, received_at, received_at),
        )


def _reference_from_row(row, *, overrides: dict[str, object] | None = None) -> SecretReference:
    values = {
        "schema_version": row["schema_version"],
        "reference_id": row["reference_id"],
        "deployment_id": row["edge_deployment_id"],
        "tenant_id": row["tenant_id"],
        "workspace_id": row["workspace_id"],
        "provider_connection_id": row["provider_connection_id"],
        "label": row["label"],
        "kind": row["kind"],
        "status": row["status"],
        "revision": int(row["revision"]),
        "active_version": row["active_version"],
        "last_test_outcome": row["last_test_outcome"],
        "capabilities": json.loads(str(row["capabilities_json"])),
        "rotated_at": row["rotated_at"],
        "updated_at": row["updated_at"],
    }
    values.update(overrides or {})
    return SecretReference.model_validate(values)


def _receipt_from_row(row, *, batch_id, deployment_id, disposition, request_id, trace_id):
    return EdgeSecretSyncReceipt(
        receipt_id=str(row["edge_secret_sync_receipt_id"]),
        batch_id=batch_id,
        deployment_id=deployment_id,
        disposition=disposition,
        accepted_event_count=int(row["accepted_event_count"]),
        duplicate_event_count=int(row["duplicate_event_count"]),
        received_at=str(row["received_at"]),
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _require_secret_reference_capability(agent) -> None:
    try:
        capabilities = json.loads(str(agent["capability_codes_json"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ApiError("edge.authentication_failed") from None
    if (
        type(capabilities) is not list
        or any(type(value) is not str for value in capabilities)
        or len(capabilities) != len(set(capabilities))
        or "secret_reference" not in capabilities
    ):
        raise ApiError("edge.authentication_failed")


def _require_owner(reference: SecretReference, owner: tuple[str, str, str]) -> None:
    if (reference.workspace_id, reference.tenant_id, reference.deployment_id) != owner:
        raise ApiError("edge.authentication_failed")


def _require_reference_identity(row, reference: SecretReference) -> None:
    current = (
        row["workspace_id"],
        row["tenant_id"],
        row["edge_deployment_id"],
        row["provider_connection_id"],
        row["label"],
        row["kind"],
        row["schema_version"],
    )
    incoming = (
        reference.workspace_id,
        reference.tenant_id,
        reference.deployment_id,
        reference.provider_connection_id,
        reference.label,
        reference.kind,
        reference.schema_version,
    )
    if current != incoming:
        raise ApiError("edge.idempotency_conflict")


def _validate_batch_times(batch: EdgeSecretSyncBatch) -> None:
    now = _utcnow()
    sent = _datetime(batch.sent_at)
    if sent > now + _CLOCK_SKEW or sent < now - _MAX_OFFLINE_AGE:
        raise ApiError("edge.request_invalid")


def _capabilities(values) -> str:
    return json.dumps(list(values), sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_edge_json_bytes(value)).hexdigest()


__all__ = ("list_workspace_secret_references", "synchronize_agent_secret_references")
