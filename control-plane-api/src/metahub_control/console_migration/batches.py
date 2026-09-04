"""Transactional child and aggregate state for all-active migration batches."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def batch_item_status(migration_state: str) -> str:
    if migration_state in {"ready", "active", "rolled_back"}:
        return "completed"
    if migration_state == "blocked":
        return "blocked"
    if migration_state == "backfill_queued":
        return "queued"
    if migration_state in {"backfilling", "shadowing"}:
        return "running"
    return "inventoried"


def sync_migration_batches(
    connection: sqlite3.Connection,
    migration_id: str,
    migration_state: str,
    now: datetime,
) -> None:
    stamp = _db_time(now)
    item_state = batch_item_status(migration_state)
    rows = connection.execute(
        """SELECT platform_console_migration_batch_id
           FROM platform_console_migration_batch_items
           WHERE console_domain_migration_id=?""",
        (migration_id,),
    ).fetchall()
    if not rows:
        return
    connection.execute(
        """UPDATE platform_console_migration_batch_items
           SET status=?,revision=revision+1,updated_at=?
           WHERE console_domain_migration_id=? AND status!=?""",
        (item_state, stamp, migration_id, item_state),
    )
    for row in rows:
        _sync_batch(connection, str(row["platform_console_migration_batch_id"]), stamp)


def sync_console_migration_batch(
    connection: sqlite3.Connection,
    batch_id: str,
    now: datetime,
) -> None:
    _sync_batch(connection, batch_id, _db_time(now))


def _sync_batch(connection: sqlite3.Connection, batch_id: str, stamp: str) -> None:
    counts = connection.execute(
        """SELECT count(*) workspace_total,
             sum(CASE WHEN status='completed' THEN 1 ELSE 0 END) workspace_completed,
             sum(CASE WHEN status IN('blocked','cancelled') THEN 1 ELSE 0 END)
               workspace_blocked,
             sum(CASE WHEN status IN('queued','running') THEN 1 ELSE 0 END) workspace_running
           FROM platform_console_migration_batch_items
           WHERE platform_console_migration_batch_id=?""",
        (batch_id,),
    ).fetchone()
    total = int(counts["workspace_total"] or 0)
    completed = int(counts["workspace_completed"] or 0)
    blocked = int(counts["workspace_blocked"] or 0)
    running = int(counts["workspace_running"] or 0)
    if total == 0 or completed == total:
        status = "completed"
    elif completed + blocked == total and blocked:
        status = "blocked"
    elif running:
        status = "running"
    else:
        status = "prepared"
    batch = connection.execute(
        """SELECT domain_code,operation_generation,status,workspace_total,
                  workspace_completed,workspace_blocked
           FROM platform_console_migration_batches
           WHERE platform_console_migration_batch_id=?""",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise ValueError("migration batch missing")
    if (
        str(batch["status"]) != status
        or int(batch["workspace_total"]) != total
        or int(batch["workspace_completed"]) != completed
        or int(batch["workspace_blocked"]) != blocked
    ):
        connection.execute(
            """UPDATE platform_console_migration_batches SET status=?,workspace_total=?,
                 workspace_completed=?,workspace_blocked=?,revision=revision+1,updated_at=?
               WHERE platform_console_migration_batch_id=?""",
            (status, total, completed, blocked, stamp, batch_id),
        )
    if status == "completed":
        platform_state = "ready"
    elif status == "blocked":
        platform_state = "blocked"
    elif status == "running":
        platform_state = "backfilling"
    else:
        platform_state = "inventoried"
    connection.execute(
        """UPDATE platform_console_domain_migrations
           SET state=?,workspace_total=?,workspace_ready=?,workspace_blocked=?,
             revision=revision+1,updated_at=?
           WHERE domain_code=? AND operation_generation=? AND(
             state!=? OR workspace_total!=? OR workspace_ready!=? OR workspace_blocked!=?)""",
        (
            platform_state,
            total,
            completed,
            blocked,
            stamp,
            batch["domain_code"],
            batch["operation_generation"],
            platform_state,
            total,
            completed,
            blocked,
        ),
    )


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "batch_item_status",
    "sync_console_migration_batch",
    "sync_migration_batches",
]
