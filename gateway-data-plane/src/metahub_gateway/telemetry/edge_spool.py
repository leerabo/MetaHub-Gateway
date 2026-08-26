"""Crash-safe append-only local spool for zero-sensitive Edge telemetry."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from metahub_shared.private_gateway import (
    EdgeTelemetryBatch,
    EdgeTelemetryError,
    EdgeTelemetryReceiptWire,
    TelemetryLevel,
    TelemetryStream,
    canonical_telemetry_bytes,
    telemetry_batch_sha256,
    validate_telemetry_batch,
)

_STREAMS = frozenset({"health", "operational", "usage", "failure"})
_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_EPOCH_ID = re.compile(r"^ete_[0-9a-f]{32}$")
_SESSION = re.compile(r"^egs_[A-Za-z0-9_-]{43}$")
_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS edge_telemetry_spool_heads(
  stream TEXT PRIMARY KEY CHECK(stream IN('health','operational','usage','failure')),
  epoch_id TEXT NOT NULL UNIQUE CHECK(length(epoch_id)=36 AND substr(epoch_id,1,4)='ete_'),
  previous_epoch_id TEXT,
  next_sequence INTEGER NOT NULL CHECK(next_sequence>=1),
  policy_level TEXT NOT NULL CHECK(policy_level IN('minimal','operational','billing')),
  policy_revision INTEGER NOT NULL CHECK(policy_revision>=1),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edge_telemetry_spool_batches(
  batch_id TEXT PRIMARY KEY CHECK(length(batch_id)=36 AND substr(batch_id,1,4)='etb_'),
  deployment_id TEXT NOT NULL,
  stream TEXT NOT NULL CHECK(stream IN('health','operational','usage','failure')),
  epoch_id TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  policy_level TEXT NOT NULL CHECK(policy_level IN('minimal','operational','billing')),
  policy_revision INTEGER NOT NULL CHECK(policy_revision>=1),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  payload_json BLOB NOT NULL CHECK(length(payload_json) BETWEEN 64 AND 1048576),
  created_at TEXT NOT NULL,
  UNIQUE(stream,epoch_id,sequence),
  UNIQUE(batch_id,payload_sha256)
);
CREATE TABLE IF NOT EXISTS edge_telemetry_spool_receipts(
  receipt_event_id TEXT PRIMARY KEY CHECK(length(receipt_event_id)=36),
  batch_id TEXT NOT NULL UNIQUE REFERENCES edge_telemetry_spool_batches(batch_id),
  receipt_id TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
  contiguous_sequence INTEGER NOT NULL CHECK(contiguous_sequence>=0),
  received_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_edge_telemetry_spool_batch_update
BEFORE UPDATE ON edge_telemetry_spool_batches
BEGIN SELECT RAISE(ABORT,'edge_telemetry_spool_append_only'); END;
CREATE TRIGGER IF NOT EXISTS trg_edge_telemetry_spool_batch_delete
BEFORE DELETE ON edge_telemetry_spool_batches
BEGIN SELECT RAISE(ABORT,'edge_telemetry_spool_append_only'); END;
CREATE TRIGGER IF NOT EXISTS trg_edge_telemetry_spool_receipt_update
BEFORE UPDATE ON edge_telemetry_spool_receipts
BEGIN SELECT RAISE(ABORT,'edge_telemetry_spool_append_only'); END;
CREATE TRIGGER IF NOT EXISTS trg_edge_telemetry_spool_receipt_delete
BEFORE DELETE ON edge_telemetry_spool_receipts
BEGIN SELECT RAISE(ABORT,'edge_telemetry_spool_append_only'); END;
"""


class EdgeTelemetrySpoolError(RuntimeError):
    """Fixed-code local failure that never includes telemetry or credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TelemetryBacklog:
    pending_batches: int
    oldest_created_at: str | None


class EdgeTelemetrySpool:
    """Own immutable local batches and receipt facts with an atomic sequence head."""

    def __init__(
        self,
        path: str | Path,
        *,
        deployment_id: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if _DEPLOYMENT_ID.fullmatch(deployment_id) is None:
            raise EdgeTelemetrySpoolError("edge_telemetry.owner_invalid")
        self._path = Path(path)
        self._deployment_id = deployment_id
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.executescript(_SCHEMA)
                connection.commit()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            raise EdgeTelemetrySpoolError("edge_telemetry.storage_unavailable") from None

    def append(
        self,
        stream: TelemetryStream,
        records: Iterable[object],
        *,
        policy_level: TelemetryLevel,
        policy_revision: int,
        sent_at: datetime | None = None,
    ) -> EdgeTelemetryBatch:
        if stream not in _STREAMS or type(policy_revision) is not int or policy_revision < 1:
            raise EdgeTelemetrySpoolError("edge_telemetry.payload_invalid")
        record_values = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in records
        ]
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                head = connection.execute(
                    "SELECT * FROM edge_telemetry_spool_heads WHERE stream=?", (stream,)
                ).fetchone()
                now = _db_time(sent_at or self._now())
                if head is None:
                    epoch_id = _id("ete_")
                    previous_epoch_id = None
                    sequence = 1
                    connection.execute(
                        """
                        INSERT INTO edge_telemetry_spool_heads(
                          stream,epoch_id,previous_epoch_id,next_sequence,policy_level,
                          policy_revision,updated_at) VALUES(?,?,NULL,2,?,?,?)
                        """,
                        (stream, epoch_id, policy_level, policy_revision, now),
                    )
                else:
                    epoch_id = str(head["epoch_id"])
                    previous_epoch_id = head["previous_epoch_id"]
                    sequence = int(head["next_sequence"])
                    _validate_policy_transition(head, policy_level, policy_revision)
                    connection.execute(
                        """
                        UPDATE edge_telemetry_spool_heads SET next_sequence=?,policy_level=?,
                          policy_revision=?,updated_at=? WHERE stream=? AND next_sequence=?
                        """,
                        (sequence + 1, policy_level, policy_revision, now, stream, sequence),
                    )
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        raise EdgeTelemetrySpoolError("edge_telemetry.storage_conflict")
                batch = EdgeTelemetryBatch.model_validate(
                    {
                        "schema_version": "1",
                        "deployment_id": self._deployment_id,
                        "stream": stream,
                        "epoch_id": epoch_id,
                        "previous_epoch_id": previous_epoch_id,
                        "sequence": sequence,
                        "policy_revision": policy_revision,
                        "sent_at": now,
                        "records": record_values,
                    }
                )
                validate_telemetry_batch(batch, policy_level)
                encoded = canonical_telemetry_bytes(batch.model_dump(mode="json"))
                batch_id = _id("etb_")
                connection.execute(
                    """
                    INSERT INTO edge_telemetry_spool_batches(
                      batch_id,deployment_id,stream,epoch_id,sequence,policy_level,
                      policy_revision,payload_sha256,payload_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch_id,
                        self._deployment_id,
                        stream,
                        epoch_id,
                        sequence,
                        policy_level,
                        policy_revision,
                        telemetry_batch_sha256(batch),
                        encoded,
                        now,
                    ),
                )
                connection.commit()
                return batch
            except (EdgeTelemetryError, ValueError):
                _rollback(connection)
                raise EdgeTelemetrySpoolError("edge_telemetry.payload_invalid") from None
            except EdgeTelemetrySpoolError:
                _rollback(connection)
                raise
            except sqlite3.Error:
                _rollback(connection)
                raise EdgeTelemetrySpoolError("edge_telemetry.storage_unavailable") from None
            finally:
                connection.close()

    def rotate_epoch(
        self,
        stream: TelemetryStream,
        *,
        policy_level: TelemetryLevel,
        policy_revision: int,
        epoch_id: str | None = None,
    ) -> str:
        if stream not in _STREAMS or type(policy_revision) is not int or policy_revision < 1:
            raise EdgeTelemetrySpoolError("edge_telemetry.payload_invalid")
        next_epoch = epoch_id or _id("ete_")
        if _EPOCH_ID.fullmatch(next_epoch) is None:
            raise EdgeTelemetrySpoolError("edge_telemetry.payload_invalid")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                head = connection.execute(
                    "SELECT * FROM edge_telemetry_spool_heads WHERE stream=?", (stream,)
                ).fetchone()
                reused = connection.execute(
                    """
                    SELECT 1 FROM edge_telemetry_spool_heads WHERE epoch_id=?
                    UNION ALL
                    SELECT 1 FROM edge_telemetry_spool_batches WHERE epoch_id=? LIMIT 1
                    """,
                    (next_epoch, next_epoch),
                ).fetchone()
                if reused is not None:
                    raise EdgeTelemetrySpoolError("edge_telemetry.epoch_conflict")
                if head is None:
                    previous = None
                else:
                    _validate_policy_transition(head, policy_level, policy_revision)
                    pending = connection.execute(
                        """
                        SELECT COUNT(*) FROM edge_telemetry_spool_batches batch
                        LEFT JOIN edge_telemetry_spool_receipts receipt
                          ON receipt.batch_id=batch.batch_id
                        WHERE batch.stream=? AND batch.epoch_id=? AND receipt.batch_id IS NULL
                        """,
                        (stream, head["epoch_id"]),
                    ).fetchone()[0]
                    if int(pending) != 0:
                        raise EdgeTelemetrySpoolError("edge_telemetry.epoch_pending")
                    previous = str(head["epoch_id"])
                now = _db_time(self._now())
                connection.execute(
                    """
                    INSERT INTO edge_telemetry_spool_heads(
                      stream,epoch_id,previous_epoch_id,next_sequence,policy_level,
                      policy_revision,updated_at) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(stream) DO UPDATE SET epoch_id=excluded.epoch_id,
                      previous_epoch_id=excluded.previous_epoch_id,next_sequence=1,
                      policy_level=excluded.policy_level,policy_revision=excluded.policy_revision,
                      updated_at=excluded.updated_at
                    """,
                    (stream, next_epoch, previous, 1, policy_level, policy_revision, now),
                )
                connection.commit()
                return next_epoch
            except EdgeTelemetrySpoolError:
                _rollback(connection)
                raise
            except sqlite3.Error:
                _rollback(connection)
                raise EdgeTelemetrySpoolError("edge_telemetry.storage_unavailable") from None
            finally:
                connection.close()

    def pending(self, *, limit: int = 32) -> tuple[tuple[str, EdgeTelemetryBatch], ...]:
        if type(limit) is not int or not 1 <= limit <= 128:
            raise EdgeTelemetrySpoolError("edge_telemetry.payload_invalid")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT batch.batch_id,batch.payload_json
                FROM edge_telemetry_spool_batches batch
                LEFT JOIN edge_telemetry_spool_receipts receipt ON receipt.batch_id=batch.batch_id
                WHERE receipt.batch_id IS NULL
                ORDER BY batch.created_at,batch.stream,batch.epoch_id,batch.sequence LIMIT ?
                """,
                (limit,),
            ).fetchall()
            result: list[tuple[str, EdgeTelemetryBatch]] = []
            for row in rows:
                payload = json.loads(bytes(row["payload_json"]))
                result.append((str(row["batch_id"]), EdgeTelemetryBatch.model_validate(payload)))
            return tuple(result)
        except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
            raise EdgeTelemetrySpoolError("edge_telemetry.storage_invalid") from None
        finally:
            connection.close()

    def acknowledge(self, batch_id: str, receipt: EdgeTelemetryReceiptWire) -> None:
        encoded = canonical_telemetry_bytes(receipt.model_dump(mode="json"))
        receipt_sha256 = hashlib.sha256(encoded).hexdigest()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                batch = connection.execute(
                    "SELECT * FROM edge_telemetry_spool_batches WHERE batch_id=?", (batch_id,)
                ).fetchone()
                if batch is None or any(
                    (
                        receipt.deployment_id != batch["deployment_id"],
                        receipt.stream != batch["stream"],
                        receipt.epoch_id != batch["epoch_id"],
                        receipt.sequence != int(batch["sequence"]),
                        receipt.policy_level != batch["policy_level"],
                        receipt.policy_revision != int(batch["policy_revision"]),
                    )
                ):
                    raise EdgeTelemetrySpoolError("edge_telemetry.receipt_invalid")
                existing = connection.execute(
                    "SELECT receipt_sha256 FROM edge_telemetry_spool_receipts WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["receipt_sha256"]) != receipt_sha256:
                        raise EdgeTelemetrySpoolError("edge_telemetry.receipt_conflict")
                    connection.rollback()
                    return
                connection.execute(
                    """
                    INSERT INTO edge_telemetry_spool_receipts(
                      receipt_event_id,batch_id,receipt_id,receipt_sha256,
                      contiguous_sequence,received_at) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        _id("era_"),
                        batch_id,
                        receipt.receipt_id,
                        receipt_sha256,
                        receipt.contiguous_sequence,
                        receipt.received_at,
                    ),
                )
                connection.commit()
            except EdgeTelemetrySpoolError:
                _rollback(connection)
                raise
            except sqlite3.Error:
                _rollback(connection)
                raise EdgeTelemetrySpoolError("edge_telemetry.storage_unavailable") from None
            finally:
                connection.close()

    def backlog(self) -> TelemetryBacklog:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) pending_batches,MIN(batch.created_at) oldest_created_at
                FROM edge_telemetry_spool_batches batch
                LEFT JOIN edge_telemetry_spool_receipts receipt ON receipt.batch_id=batch.batch_id
                WHERE receipt.batch_id IS NULL
                """
            ).fetchone()
            return TelemetryBacklog(int(row["pending_batches"]), row["oldest_created_at"])
        except sqlite3.Error:
            raise EdgeTelemetrySpoolError("edge_telemetry.storage_unavailable") from None
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


class EdgeTelemetryUploader:
    """Upload gzip batches with only the Edge session credential and strict receipts."""

    def __init__(self, client: httpx.Client, control_base_url: str) -> None:
        base = control_base_url.rstrip("/")
        parsed = urlsplit(base)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and not loopback)
        ):
            raise EdgeTelemetrySpoolError("edge_telemetry.endpoint_invalid")
        self._client = client
        self._url = f"{base}/edge/telemetry/batches"

    def sync(self, spool: EdgeTelemetrySpool, *, edge_session: str, limit: int = 32) -> int:
        if _SESSION.fullmatch(edge_session) is None:
            raise EdgeTelemetrySpoolError("edge_telemetry.authentication_failed")
        completed = 0
        for batch_id, batch in spool.pending(limit=limit):
            encoded = canonical_telemetry_bytes(batch.model_dump(mode="json"))
            try:
                response = self._client.post(
                    self._url,
                    content=gzip.compress(encoded, compresslevel=6, mtime=0),
                    headers={
                        "Accept": "application/json",
                        "Cache-Control": "no-store",
                        "Content-Encoding": "gzip",
                        "Content-Type": "application/json",
                        "X-Edge-Session": edge_session,
                    },
                    follow_redirects=False,
                )
                response.raise_for_status()
                if "no-store" not in response.headers.get("cache-control", "").lower():
                    raise EdgeTelemetrySpoolError("edge_telemetry.receipt_invalid")
                if response.headers.get("content-type", "").partition(";")[0].lower() != (
                    "application/json"
                ):
                    raise EdgeTelemetrySpoolError("edge_telemetry.receipt_invalid")
                receipt = EdgeTelemetryReceiptWire.model_validate(response.json())
            except EdgeTelemetrySpoolError:
                raise
            except (httpx.HTTPError, ValueError):
                raise EdgeTelemetrySpoolError("edge_telemetry.transport_unavailable") from None
            spool.acknowledge(batch_id, receipt)
            completed += 1
        return completed


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _db_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise EdgeTelemetrySpoolError("edge_telemetry.time_invalid")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error:
        pass


def _validate_policy_transition(
    head: sqlite3.Row, policy_level: TelemetryLevel, policy_revision: int
) -> None:
    previous_revision = int(head["policy_revision"])
    if policy_revision < previous_revision or (
        policy_revision == previous_revision and policy_level != head["policy_level"]
    ):
        raise EdgeTelemetrySpoolError("edge_telemetry.policy_conflict")


__all__ = (
    "EdgeTelemetrySpool",
    "EdgeTelemetrySpoolError",
    "EdgeTelemetryUploader",
    "TelemetryBacklog",
)
