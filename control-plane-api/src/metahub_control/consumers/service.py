"""Tenant-scoped, audited directory without provisioning login identities."""

from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import (
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.auth.service import require_permission

from .models import ConsumerInfo, ConsumerListResponse, ConsumerResponse


def _ensure_employee_code_column(connection):
    """Upgrade older trial databases that already recorded migration 0154."""
    if getattr(connection, "backend", "sqlite") == "postgresql":
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=?",
                ("workspace_consumers",),
            ).fetchall()
        }
    else:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(workspace_consumers)").fetchall()
        }
    if "employee_code" not in columns:
        connection.execute(
            "ALTER TABLE workspace_consumers ADD COLUMN employee_code TEXT NOT NULL "
            "DEFAULT '' CHECK(length(employee_code)<=80)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_consumers_employee_code "
            "ON workspace_consumers(tenant_id,employee_code) WHERE employee_code!=''"
        )


def require_consumer_admin(context):
    if context.user.role != "admin":
        raise ApiError("auth.permission_denied")
    require_permission(context, "user.manage")


def require_active_consumer(connection, context, consumer_id):
    require_consumer_admin(context)
    row = connection.execute(
        "SELECT status FROM workspace_consumers WHERE tenant_id=? AND consumer_id=?",
        (context.user.tenant_id, consumer_id),
    ).fetchone()
    if row is None:
        raise ApiError("consumer.not_found")
    if row["status"] != "active":
        raise ApiError("consumer.disabled")


def list_consumers(config, context, *, limit, offset, request_id, trace_id):
    with ResourceWriteTransaction(
        connect_database(config.database_url), config, context, "virtual_key.read", read_only=True
    ) as transaction:
        context = transaction.context
        require_consumer_admin(context)
        connection = transaction.connection
        _ensure_employee_code_column(connection)
        rows = connection.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM virtual_key_consumers k
                 JOIN virtual_keys virtual_key
                   ON virtual_key.tenant_id=k.tenant_id
                  AND virtual_key.virtual_key_id=k.virtual_key_id
                 WHERE k.tenant_id=c.tenant_id AND k.consumer_id=c.consumer_id
                   AND virtual_key.status='active') AS key_count
               FROM workspace_consumers c WHERE c.tenant_id=? AND c.status='active'
               ORDER BY c.created_at,c.consumer_id LIMIT ? OFFSET ?""",
            (context.user.tenant_id, limit, offset),
        ).fetchall()
        total = connection.execute(
            "SELECT COUNT(*) AS total FROM workspace_consumers "
            "WHERE tenant_id=? AND status='active'",
            (context.user.tenant_id,),
        ).fetchone()["total"]
        require_consumer_admin(transaction.verify())
        return ConsumerListResponse(
            consumers=[ConsumerInfo(**dict(row)) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
            request_id=request_id,
            trace_id=trace_id,
        )


def save_consumer(
    config, context, payload, *, consumer_id=None, audit_request_meta, request_id, trace_id
):
    with ResourceWriteTransaction(
        connect_database(config.database_url), config, context, "virtual_key.write"
    ) as transaction:
        context = transaction.context
        require_consumer_admin(context)
        connection = transaction.connection
        _ensure_employee_code_column(connection)
        tenant_id = context.user.tenant_id
        now = datetime.now(UTC).isoformat()
        employee_code = payload.employee_code.strip()
        if employee_code:
            duplicate = connection.execute(
                "SELECT consumer_id FROM workspace_consumers "
                "WHERE tenant_id=? AND employee_code=? AND consumer_id!=?",
                (tenant_id, employee_code, consumer_id or ""),
            ).fetchone()
            if duplicate is not None:
                raise ApiError("consumer.employee_code_exists")
        before = None
        if consumer_id is None:
            consumer_id = "con_" + uuid4().hex
            connection.execute(
                """INSERT INTO workspace_consumers(consumer_id,tenant_id,display_name,
                   department,employee_code,status,revision,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,'active',1,?,?,?)""",
                (
                    consumer_id,
                    tenant_id,
                    payload.display_name,
                    payload.department,
                    employee_code,
                    context.user.user_id,
                    now,
                    now,
                ),
            )
        else:
            before = _load_consumer(connection, tenant_id, consumer_id)
            changed = connection.execute(
                """UPDATE workspace_consumers
                   SET display_name=?,department=?,employee_code=?,status=?,
                       revision=revision+1,updated_at=?
                   WHERE tenant_id=? AND consumer_id=? AND revision=?""",
                (
                    payload.display_name,
                    payload.department,
                    employee_code,
                    payload.status,
                    now,
                    tenant_id,
                    consumer_id,
                    payload.revision,
                ),
            )
            if changed.rowcount != 1:
                raise ApiError("consumer.revision_conflict")
        consumer = _load_consumer(connection, tenant_id, consumer_id)
        record = record_audit_log(
            connection,
            context,
            action="consumer.updated" if before else "consumer.created",
            object_type="consumer",
            object_id=consumer_id,
            object_name=consumer_id,
            before_summary=_audit_summary(before),
            after_summary=_audit_summary(consumer),
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        require_consumer_admin(transaction.verify())
        transaction.commit(commit_audited_transaction)
        dispatch_audit_recorded(config, context, record, request_id=request_id, trace_id=trace_id)
        return ConsumerResponse(consumer=consumer, request_id=request_id, trace_id=trace_id)


def archive_consumer(config, context, consumer_id, *, audit_request_meta, request_id, trace_id):
    """Soft-delete a consumer so historical usage and audit bindings remain intact."""
    with ResourceWriteTransaction(
        connect_database(config.database_url), config, context, "virtual_key.write"
    ) as transaction:
        context = transaction.context
        require_consumer_admin(context)
        connection = transaction.connection
        before = _load_consumer(connection, context.user.tenant_id, consumer_id)
        if before.status == "disabled":
            return ConsumerResponse(consumer=before, request_id=request_id, trace_id=trace_id)
        now = datetime.now(UTC).isoformat()
        changed = connection.execute(
            "UPDATE workspace_consumers SET status='disabled', revision=revision+1, updated_at=? "
            "WHERE tenant_id=? AND consumer_id=? AND revision=?",
            (now, context.user.tenant_id, consumer_id, before.revision),
        )
        if changed.rowcount != 1:
            raise ApiError("consumer.revision_conflict")
        consumer = _load_consumer(connection, context.user.tenant_id, consumer_id)
        record = record_audit_log(
            connection, context, action="consumer.updated", object_type="consumer",
            object_id=consumer_id, object_name=consumer_id,
            before_summary=_audit_summary(before), after_summary=_audit_summary(consumer),
            request_meta=audit_request_meta, request_id=request_id, trace_id=trace_id,
        )
        transaction.commit(commit_audited_transaction)
        dispatch_audit_recorded(config, context, record, request_id=request_id, trace_id=trace_id)
        return ConsumerResponse(consumer=consumer, request_id=request_id, trace_id=trace_id)


def _load_consumer(connection, tenant_id, consumer_id):
    row = connection.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM virtual_key_consumers k
             WHERE k.tenant_id=c.tenant_id AND k.consumer_id=c.consumer_id) AS key_count
           FROM workspace_consumers c WHERE c.tenant_id=? AND c.consumer_id=?""",
        (tenant_id, consumer_id),
    ).fetchone()
    if row is None:
        raise ApiError("consumer.not_found")
    return ConsumerInfo(**dict(row))


def _audit_summary(consumer):
    if consumer is None:
        return {}
    return {
        "consumer_id": consumer.consumer_id,
        "status": consumer.status,
        "revision": consumer.revision,
    }
