"""Provision and fence only an execution-owned PostgreSQL restore database."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql

from .backups import BackupRuntimeError, _validate_postgres_url


class PostgresRestoreTarget:
    def __init__(self, admin_url: str, execution_id: str, request_sha256: str):
        _validate_postgres_url(admin_url)
        if (
            re.fullmatch(r"rex_[0-9a-f]{32}", execution_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", request_sha256) is None
            or not urlparse(admin_url).username
        ):
            raise BackupRuntimeError("backup.restore_target_unsafe")
        self.name = "mh_restore_" + execution_id[4:]
        self.marker = "metahub-restore-v1:" + request_sha256
        self._admin_url = admin_url
        self.database_url = urlunparse(urlparse(admin_url)._replace(path="/" + self.name))
        self.connection = None
        self.oid: int | None = None
        self.server_sha256: str | None = None

    def __enter__(self):
        self.connection = psycopg.connect(
            self._admin_url,
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=5000 -c lock_timeout=5000",
        )
        try:
            identity = self.connection.execute(
                "SELECT system_identifier::text,inet_server_addr()::text,inet_server_port() "
                "FROM pg_control_system()"
            ).fetchone()
            self.server_sha256 = hashlib.sha256(repr(tuple(identity)).encode()).hexdigest()
            lock_key = int.from_bytes(hashlib.sha256(self.name.encode()).digest()[:8], signed=True)
            if not self.connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (lock_key,)
            ).fetchone()[0]:
                raise BackupRuntimeError("backup.restore_target_busy")
            return self
        except BaseException:
            self.connection.close()
            raise

    def __exit__(self, *_):
        self.connection.close()

    def _record(self):
        row = self.connection.execute(
            """SELECT d.oid,d.datallowconn,shobj_description(d.oid,'pg_database'),
                      d.datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user)
               FROM pg_database d WHERE d.datname=%s""",
            (self.name,),
        ).fetchone()
        if row is not None and (
            row[2] != self.marker or not row[3] or (self.oid is not None and self.oid != row[0])
        ):
            raise BackupRuntimeError("backup.restore_target_unowned")
        if row is None and self.oid is not None:
            raise BackupRuntimeError("backup.restore_target_changed")
        if row is not None:
            self.oid = int(row[0])
        return row

    def create(self, record_oid: Callable[[int], None]) -> None:
        if self._record() is not None:
            raise BackupRuntimeError("backup.restore_target_exists")
        self.connection.execute(
            sql.SQL("CREATE DATABASE {} WITH TEMPLATE template0 ALLOW_CONNECTIONS false").format(
                sql.Identifier(self.name)
            )
        )
        self.connection.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(self.name), sql.Literal(self.marker)
            )
        )
        self._record()
        record_oid(self.oid)
        self.connection.execute(
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(sql.Identifier(self.name))
        )

    def fence(self) -> bool:
        """False is an observation timeout, never proof of a stopped external effect."""
        if self._record() is None:
            return True
        self.connection.execute(
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(sql.Identifier(self.name))
        )
        self.connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datid=%s",
            (self.oid,),
        ).fetchall()
        deadline = time.monotonic() + 5
        while True:
            row = self._record()
            self.connection.execute("SELECT pg_stat_clear_snapshot()")
            remaining = self.connection.execute(
                "SELECT COUNT(*) FROM pg_stat_activity WHERE datid=%s", (self.oid,)
            ).fetchone()[0]
            if row is not None and not row[1] and not remaining:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


__all__ = ["PostgresRestoreTarget"]
