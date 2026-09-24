"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，
Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/db/connection.py
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, NoReturn
from urllib.parse import urlparse

from .errors import DbError
from .security import password_hash_algorithm

# The local runtime opens one SQLite connection per request. A short default
# timeout turns the initial dashboard fan-out into avoidable 503 responses
# when two requests briefly contend for the single SQLite writer.
SQLITE_BUSY_TIMEOUT_SECONDS = 30
SQLITE_BUSY_TIMEOUT_MILLISECONDS = SQLITE_BUSY_TIMEOUT_SECONDS * 1000
# Retain a full local scheduler/request burst without reparsing the large schema.
# This bounds idle handles, not active concurrency; overflow still opens independently.
_MAX_IDLE_CONNECTIONS_PER_DATABASE = 16
_MAX_IDLE_DATABASES = 4
DatabaseBackend = Literal["sqlite", "postgresql"]
DatabaseCommitGuard = Callable[[Any], None]

_DATABASE_COMMIT_GUARD: ContextVar[DatabaseCommitGuard | None] = ContextVar(
    "metahub_database_commit_guard",
    default=None,
)
_DATABASE_OPERATION_DEADLINE: ContextVar[float | None] = ContextVar(
    "metahub_database_operation_deadline", default=None,
)


class _DatabaseOperationDeadline:
    def __init__(self, deadline: float | None) -> None:
        self.deadline = deadline
        self.token = None

    def __enter__(self) -> None:
        previous = _DATABASE_OPERATION_DEADLINE.get()
        deadline = self.deadline
        if deadline is not None:
            if (
                isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)
            ):
                raise sqlite3.OperationalError("Database deadline invalid.")
            deadline = min(previous, deadline) if previous is not None else deadline
        self.token = _DATABASE_OPERATION_DEADLINE.set(
            deadline if deadline is not None else previous,
        )

    def __exit__(self, *_args) -> None:
        _DATABASE_OPERATION_DEADLINE.reset(self.token)


def database_operation_deadline(deadline: float | None) -> _DatabaseOperationDeadline:
    """Opt in to a shared PostgreSQL budget without mutating frozen driver exceptions."""
    return _DatabaseOperationDeadline(deadline)


def _database_remaining_seconds() -> float | None:
    deadline = _DATABASE_OPERATION_DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise sqlite3.OperationalError("Database deadline exceeded.")
    return remaining


@contextmanager
def database_commit_guard(guard: DatabaseCommitGuard) -> Iterator[None]:
    """Require a caller-provided authority check before transactional commits."""

    token = _DATABASE_COMMIT_GUARD.set(guard)
    try:
        yield
    finally:
        _DATABASE_COMMIT_GUARD.reset(token)


def _require_commit_authority(connection: Any) -> None:
    guard = _DATABASE_COMMIT_GUARD.get()
    if guard is not None and connection.in_transaction:
        try:
            guard(connection)
        except BaseException:
            # Rejected authority must release the transaction even in legacy callers
            # that only catch database errors. Never retain a denied writer's locks.
            try:
                connection.rollback()
            except BaseException:
                with suppress(BaseException):
                    connection.close()
            raise


@dataclass(frozen=True, slots=True)
class DatabaseCapabilities:
    backend: DatabaseBackend
    server_side_pool: bool
    advisory_locks: bool
    transactional_ddl: bool
    returning: bool


_DATABASE_CAPABILITIES = {
    "sqlite": DatabaseCapabilities(
        backend="sqlite",
        server_side_pool=False,
        advisory_locks=False,
        transactional_ddl=True,
        returning=True,
    ),
    "postgresql": DatabaseCapabilities(
        backend="postgresql",
        server_side_pool=True,
        advisory_locks=True,
        transactional_ddl=True,
        returning=True,
    ),
}


class _DatabaseRow(Sequence[object]):
    """Expose PostgreSQL tuple rows with the sqlite3.Row access contract."""

    __slots__ = ("_folded_index", "_index", "_names", "_values")

    def __init__(self, names: tuple[str, ...], values: tuple[object, ...]) -> None:
        self._names = names
        self._values = values
        self._index: dict[str, int] = {}
        self._folded_index: dict[str, int] = {}
        for index, name in enumerate(names):
            # sqlite3.Row resolves duplicate aliases to the first column and performs
            # ASCII-insensitive named access. Preserve that compatibility contract.
            self._index.setdefault(name, index)
            self._folded_index.setdefault(name.casefold(), index)

    def __getitem__(self, key):
        if isinstance(key, str):
            index = self._index.get(key)
            if index is None:
                index = self._folded_index[key.casefold()]
            return self._values[index]
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._names)


class _PostgresCursor:
    __slots__ = ("_cursor", "_names")

    def __init__(self, cursor) -> None:
        self._cursor = cursor
        self._names = tuple(column.name for column in cursor.description or ())

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    @property
    def lastrowid(self) -> None:
        return None

    def fetchone(self):
        try:
            row = self._cursor.fetchone()
        except Exception as exc:
            _raise_postgres_error(exc)
        return self._row(row)

    def fetchall(self):
        try:
            return [self._row(row) for row in self._cursor.fetchall()]
        except Exception as exc:
            _raise_postgres_error(exc)

    def __iter__(self):
        try:
            for row in self._cursor:
                yield self._row(row)
        except Exception as exc:
            _raise_postgres_error(exc)

    def close(self) -> None:
        try:
            self._cursor.close()
        except Exception as exc:
            _raise_postgres_error(exc)

    def _row(self, row):
        if row is None:
            return None
        values = tuple(_postgres_compat_value(value) for value in row)
        return _DatabaseRow(self._names, values)


class _PostgresConnection:
    """Small DB-API compatibility facade around one psycopg pool checkout."""

    __slots__ = ("_connection", "_pool", "_closed")

    def __init__(self, connection, pool) -> None:
        self._connection = connection
        self._pool = pool
        self._closed = False

    @property
    def backend(self) -> DatabaseBackend:
        return "postgresql"

    @property
    def in_transaction(self) -> bool:
        status = getattr(self._connection.info, "transaction_status", None)
        return getattr(status, "name", str(status)) not in {"IDLE", "0"}

    def execute(self, sql: str, parameters: object | None = None) -> _PostgresCursor:
        if self._closed:
            raise RuntimeError("Cannot operate on a closed database.")
        if re.fullmatch(r"\s*(?:COMMIT|END)\s*;?\s*", sql, flags=re.I):
            _require_commit_authority(self)
        statement = _postgres_sql(sql, translate_placeholders=parameters is not None)
        cursor = None
        try:
            budgeted = _DATABASE_OPERATION_DEADLINE.get() is not None and not re.fullmatch(
                r"\s*(?:BEGIN(?: IMMEDIATE)?|ROLLBACK|SET TRANSACTION READ ONLY)\s*;?\s*",
                sql, re.I,
            )
            # Flush the setting and its statement together, retaining their server order.
            with self._connection.pipeline() if budgeted else nullcontext():
                if budgeted:
                    self._apply_statement_deadline()
                cursor = self._connection.cursor()
                if parameters is None:
                    cursor.execute(statement, prepare=False)
                else:
                    cursor.execute(statement, parameters)
        except Exception as exc:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            _raise_postgres_error(exc)
        return _PostgresCursor(cursor)

    def _apply_statement_deadline(self) -> None:
        remaining = _database_remaining_seconds()
        if remaining is None:
            return
        milliseconds = str(max(1, int(remaining * 1000)))
        # Transaction-local settings disappear on commit/rollback, never leaking
        # a short request budget into the next pool checkout.
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true), "
                "set_config('lock_timeout', %s, true)",
                (milliseconds, milliseconds),
            )

    def executescript(self, sql: str) -> None:
        """Run a reviewed bootstrap script containing explicit transaction batches."""

        if self._closed:
            raise RuntimeError("Cannot operate on a closed database.")
        if self.in_transaction:
            raise sqlite3.ProgrammingError(
                "PostgreSQL bootstrap requires an idle connection."
            )
        previous_autocommit = self._connection.autocommit
        cursor = None
        try:
            self._connection.autocommit = True
            cursor = self._connection.cursor()
            cursor.execute(sql, prepare=False)
        except Exception as exc:
            try:
                self._connection.rollback()
            except Exception:
                pass
            failure = exc
        else:
            failure = None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            self._connection.autocommit = previous_autocommit
        if failure is not None:
            _raise_postgres_error(failure)

    def executemany(self, sql: str, parameters) -> _PostgresCursor:
        if self._closed:
            raise RuntimeError("Cannot operate on a closed database.")
        cursor = None
        try:
            self._apply_statement_deadline()
            cursor = self._connection.cursor()
            cursor.executemany(_postgres_sql(sql), parameters)
        except Exception as exc:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            _raise_postgres_error(exc)
        return _PostgresCursor(cursor)

    def commit(self) -> None:
        _require_commit_authority(self)
        try:
            self._apply_statement_deadline()
            self._connection.commit()
        except Exception as exc:
            _raise_postgres_error(exc)

    def rollback(self) -> None:
        try:
            self._connection.rollback()
        except Exception as exc:
            _raise_postgres_error(exc)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.in_transaction:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
        finally:
            try:
                self._pool.putconn(self._connection)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


_POSTGRES_POOL_LOCK = threading.Lock()
_POSTGRES_POOLS: dict[str, Any] = {}


class _SqliteConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters=(), /):
        if re.fullmatch(r"\s*(?:COMMIT|END)\s*;?\s*", sql, flags=re.I):
            _require_commit_authority(self)
        return super().execute(sql, parameters)

    def commit(self) -> None:
        _require_commit_authority(self)
        super().commit()

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class _IdleConnectionPool:
    """Keep a small set of parsed SQLite schemas warm between sensitive requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: OrderedDict[str, list[sqlite3.Connection]] = OrderedDict()

    def acquire(self, database: str) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        with self._lock:
            available = self._connections.get(database)
            if available:
                connection = available.pop()
                if not available:
                    del self._connections[database]
                # A warm handle retains its previous PRAGMAs.  Refresh the
                # lock wait for this request so deadline-bound authentication
                # cannot inherit the normal 30s timeout from an idle loan.
                remaining = _database_remaining_seconds()
                busy_timeout_ms = SQLITE_BUSY_TIMEOUT_MILLISECONDS
                if remaining is not None:
                    busy_timeout_ms = max(1, min(busy_timeout_ms, int(remaining * 1000)))
                try:
                    connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                except sqlite3.Error:
                    connection.close()
                    connection = None
                if connection is not None:
                    return connection
        # Overflow requests open independently instead of blocking the event loop while
        # another request owns every warm handle.
        return _open_sqlite_connection(database, check_same_thread=False)

    def release(self, database: str, connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.rollback()
            connection.row_factory = sqlite3.Row
            # SQLite expires prepared statements when some boolean pragmas are
            # assigned, even if the value is unchanged. Check the actual state so
            # clean loans retain compiled queries, while dirty loans are restored.
            for pragma, expected in (
                ("query_only", 0), ("read_uncommitted", 0),
                ("recursive_triggers", 0), ("foreign_keys", 1),
            ):
                if connection.execute(f"PRAGMA {pragma}").fetchone()[0] != expected:
                    connection.execute(f"PRAGMA {pragma}={expected}")
            connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MILLISECONDS}")
            connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            connection.close()
            return

        discarded: list[sqlite3.Connection] = []
        with self._lock:
            available = self._connections.setdefault(database, [])
            self._connections.move_to_end(database)
            if len(available) < _MAX_IDLE_CONNECTIONS_PER_DATABASE:
                available.append(connection)
            else:
                discarded.append(connection)
            while len(self._connections) > _MAX_IDLE_DATABASES:
                _database, stale = self._connections.popitem(last=False)
                discarded.extend(stale)
        for stale_connection in discarded:
            stale_connection.close()

    def close_idle(self) -> None:
        with self._lock:
            connections = [
                connection
                for available in self._connections.values()
                for connection in available
            ]
            self._connections.clear()
        for connection in connections:
            connection.close()


_IDLE_CONNECTION_POOL = _IdleConnectionPool()
_SQLITE_WAL_LOCK = threading.Lock()
_SQLITE_WAL_DATABASES: set[str] = set()


def _close_idle_database_connections() -> None:
    """Close retained handles during tests or an orderly process shutdown."""

    _IDLE_CONNECTION_POOL.close_idle()


class _RequestConnection:
    """One request's connection facade; ``close`` never releases it early."""

    __slots__ = ("_connection", "_closed", "_release_connection")

    def __init__(
        self,
        connection: Any,
        release_connection: Callable[[Any], None] | None = None,
    ) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_closed", False)
        object.__setattr__(self, "_release_connection", release_connection)

    def __getattr__(self, name: str):
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        return getattr(self._connection, name)

    def __enter__(self) -> _RequestConnection:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        return bool(self._connection.__exit__(exc_type, exc_value, traceback))

    def close(self) -> None:
        # Service functions keep explicit logical ownership. The request scope owns
        # the physical close after authentication and response building both finish.
        return None

    def _release(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        release_connection = self._release_connection
        if release_connection is not None:
            release_connection(self._connection)
            return
        try:
            if self._connection.in_transaction:
                self._connection.rollback()
        finally:
            self._connection.close()


class _GuardedWorkerConnection(_RequestConnection):
    """An independently owned warm handle, returned immediately on close."""

    def close(self) -> None:
        self._release()


@dataclass
class _DatabaseRequestScope:
    reuse_connections: bool = False
    connections: dict[str, _RequestConnection] = field(default_factory=dict)

    def connection(
        self,
        key: str,
        opener: Callable[[], Any],
        release_connection: Callable[[Any], None] | None = None,
    ) -> _RequestConnection:
        existing = self.connections.get(key)
        if existing is not None:
            return existing
        scoped = _RequestConnection(opener(), release_connection)
        self.connections[key] = scoped
        return scoped

    def close(self) -> None:
        for connection in tuple(self.connections.values()):
            connection._release()
        self.connections.clear()


_DATABASE_REQUEST_SCOPE: ContextVar[_DatabaseRequestScope | None] = ContextVar(
    "metahub_database_request_scope",
    default=None,
)
_DATABASE_CONNECTION_POOL_SCOPE: ContextVar[bool] = ContextVar(
    "metahub_database_connection_pool_scope", default=False,
)


@contextmanager
def database_connection_pool_scope() -> Iterator[None]:
    """Reuse closed physical handles without sharing a transaction or close lifetime."""
    token = _DATABASE_CONNECTION_POOL_SCOPE.set(True)
    try:
        yield
    finally:
        _DATABASE_CONNECTION_POOL_SCOPE.reset(token)


@contextmanager
def database_request_scope(*, reuse_connections: bool = False) -> Iterator[None]:
    """Share one backend handle inside one HTTP request and release it at the boundary."""

    scope = _DatabaseRequestScope(reuse_connections=reuse_connections)
    token = _DATABASE_REQUEST_SCOPE.set(scope)
    try:
        yield
    finally:
        _DATABASE_REQUEST_SCOPE.reset(token)
        scope.close()


def release_idle_request_connections() -> None:
    """End an idle request stage before starting an independently owned worker.

    Prior idle facades become invalid. Active transactions remain request-owned;
    this is not a commit, rollback, or transfer of uncommitted business work.
    """
    scope = _DATABASE_REQUEST_SCOPE.get()
    if scope is not None:
        for key, connection in tuple(scope.connections.items()):
            if not connection.in_transaction:
                del scope.connections[key]
                connection._release()


def _metahub_sha256(value: str | bytes | None) -> str | None:
    """为迁移守卫计算稳定 SHA-256；SQLite 非文本/BLOB 类型必须失败关闭。"""

    if value is None:
        return None
    if type(value) is str:
        payload = value.encode("utf-8")
    elif type(value) is bytes:
        payload = value
    else:
        # 非文本参数会改变迁移哈希解释，必须在 SQLite UDF 边界直接拒绝。
        raise TypeError("metahub_sha256 accepts only TEXT, BLOB, or NULL")
    return hashlib.sha256(payload).hexdigest()


def _metahub_password_hash_algorithm(value: object) -> str | None:
    """Expose the bounded runtime password profile parser to migration SQL."""

    return password_hash_algorithm(value)


def connect_database(
    database_url: str | None,
    *,
    check_same_thread: bool = True,
) -> Any:
    """Connect to an explicit SQLite local/test or PostgreSQL production backend."""

    if not database_url:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.config_missing",
            "METAHUB_DATABASE_URL is required.",
            "Set METAHUB_DATABASE_URL=sqlite:///./.data/metahub.db for local development.",
        )
    parsed = urlparse(database_url)
    backend = database_backend(database_url)
    if backend is None:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.connection_failed",
            f"Unsupported database URL scheme: {parsed.scheme}",
            "Use sqlite:// for explicit local/test use or postgresql:// for production.",
        )

    if backend == "postgresql":
        return _connect_postgresql(database_url)

    path = _sqlite_path(parsed.path)
    if path != ":memory:":
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        database = str(db_path)
    else:
        database = path

    try:
        # Default callers retain sqlite3 thread ownership. Explicit pooling loans each
        # physical handle to one owner until close, without sharing live transactions.
        if _DATABASE_CONNECTION_POOL_SCOPE.get() and database != ":memory:":
            return _GuardedWorkerConnection(
                _IDLE_CONNECTION_POOL.acquire(database),
                lambda connection: _IDLE_CONNECTION_POOL.release(database, connection),
            )
        scope = _DATABASE_REQUEST_SCOPE.get()
        if scope is not None and database != ":memory:":
            if scope.reuse_connections:
                return scope.connection(
                    f"sqlite:{database}",
                    lambda: _IDLE_CONNECTION_POOL.acquire(database),
                    lambda connection: _IDLE_CONNECTION_POOL.release(database, connection),
                )
            return scope.connection(
                f"sqlite:{database}",
                lambda: _open_sqlite_connection(database, check_same_thread=False),
            )
        if _DATABASE_COMMIT_GUARD.get() is not None and database != ":memory:":
            return _GuardedWorkerConnection(
                _IDLE_CONNECTION_POOL.acquire(database),
                lambda connection: _IDLE_CONNECTION_POOL.release(database, connection),
            )
        connection = _open_sqlite_connection(
            database,
            check_same_thread=check_same_thread,
        )
        return connection
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.Error as exc:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.connection_failed",
            f"Failed to connect database: {exc}",
            "Check METAHUB_DATABASE_URL and filesystem permissions.",
        ) from exc


def database_backend(database_url: str | None) -> DatabaseBackend | None:
    if not database_url:
        return None
    scheme = urlparse(database_url).scheme.lower()
    if scheme == "sqlite":
        return "sqlite"
    if scheme in {"postgres", "postgresql"}:
        return "postgresql"
    return None


def database_capabilities(database_url: str | None) -> DatabaseCapabilities:
    backend = database_backend(database_url)
    if backend is None:
        raise DbError(
            "db.connection_failed",
            "Database backend is unsupported.",
            "Use an explicit sqlite:// or postgresql:// database URL.",
        )
    return _DATABASE_CAPABILITIES[backend]


def _connect_postgresql(database_url: str) -> Any:
    """Checkout one bounded pooled connection without exposing DSN failures."""

    try:
        from psycopg_pool import ConnectionPool, PoolTimeout
    except ImportError as exc:
        raise DbError(
            "db.driver_missing",
            "PostgreSQL driver is not installed.",
            "Install the production runtime dependencies before starting the service.",
        ) from exc

    pool_key = hashlib.sha256(database_url.encode("utf-8")).hexdigest()
    try:
        with _POSTGRES_POOL_LOCK:
            pool = _POSTGRES_POOLS.get(pool_key)
            if pool is None:
                minimum = _bounded_pool_setting("METAHUB_DATABASE_POOL_MIN_SIZE", 1, 0, 32)
                maximum = _bounded_pool_setting("METAHUB_DATABASE_POOL_MAX_SIZE", 10, 1, 128)
                if minimum > maximum:
                    raise DbError(
                        "db.config_invalid",
                        "Database pool minimum exceeds its maximum.",
                        "Set a bounded pool minimum no greater than the maximum.",
                    )
                timeout = _bounded_pool_setting(
                    "METAHUB_DATABASE_POOL_TIMEOUT_SECONDS", 3, 1, 30
                )
                statement_ms = _bounded_pool_setting(
                    "METAHUB_DATABASE_STATEMENT_TIMEOUT_MS", 30_000, 100, 300_000
                )
                lock_ms = _bounded_pool_setting(
                    "METAHUB_DATABASE_LOCK_TIMEOUT_MS", 5_000, 100, 60_000
                )
                pool = ConnectionPool(
                    conninfo=database_url,
                    min_size=minimum,
                    max_size=maximum,
                    timeout=timeout,
                    kwargs={
                        "autocommit": False,
                        "options": (
                            "-c timezone=UTC "
                            f"-c statement_timeout={statement_ms} "
                            f"-c lock_timeout={lock_ms}"
                        ),
                    },
                    open=True,
                    name=f"metahub-{pool_key[:12]}",
                )
                _POSTGRES_POOLS[pool_key] = pool
    except DbError:
        raise
    except Exception:
        raise DbError(
            "db.connection_failed",
            "Failed to initialize the configured PostgreSQL connection pool.",
            "Check database availability, TLS, credentials, and pool settings.",
        ) from None
    try:
        scope = _DATABASE_REQUEST_SCOPE.get()

        def checkout():
            remaining = _database_remaining_seconds()
            return _PostgresConnection(
                pool.getconn(timeout=remaining) if remaining is not None else pool.getconn(), pool,
            )

        if scope is not None:
            return scope.connection(
                f"postgresql:{pool_key}",
                checkout,
            )
        return checkout()
    except PoolTimeout:
        raise DbError(
            "db.pool_exhausted",
            "Database connection pool is exhausted.",
            "Retry after an in-flight database operation completes.",
        ) from None
    except DbError:
        raise
    except Exception:
        raise DbError(
            "db.connection_failed",
            "Failed to connect to the configured PostgreSQL database.",
            "Check database availability, TLS, credentials, and pool settings.",
        ) from None


def close_database_pools() -> None:
    """Close PostgreSQL pools during orderly shutdown and isolated tests."""

    with _POSTGRES_POOL_LOCK:
        pools = tuple(_POSTGRES_POOLS.values())
        _POSTGRES_POOLS.clear()
    for pool in pools:
        pool.close()


def _bounded_pool_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = minimum - 1
    if not minimum <= value <= maximum:
        raise DbError(
            "db.config_invalid",
            f"{name} is outside the supported range.",
            "Configure a bounded integer value for the database pool.",
        )
    return value


def _postgres_compat_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    return value


def _raise_postgres_error(error: Exception) -> NoReturn:
    """Map provider failures to the existing fixed, non-sensitive DB-API surface."""

    sqlstate = str(getattr(error, "sqlstate", "") or "")
    if sqlstate.startswith("23"):
        raise sqlite3.IntegrityError("Database integrity constraint failed.") from None
    if sqlstate.startswith("40") or sqlstate == "55P03":
        conflict = sqlite3.OperationalError("Database transaction conflict.")
        conflict.sqlstate = sqlstate
        raise conflict from None
    raise sqlite3.DatabaseError("Database operation failed.") from None


def _postgres_sql(sql: str, *, translate_placeholders: bool = True) -> str:
    """Translate the narrow DB-API surface without rewriting SQL structure."""

    normalized = re.sub(r"^\s*BEGIN\s+IMMEDIATE\s*;?\s*$", "BEGIN", sql, flags=re.I)
    if translate_placeholders:
        # Psycopg parses percent placeholders even inside SQL strings/comments.
        # Existing callers use qmark parameters, so every source percent is literal.
        normalized = normalized.replace("%", "%%")
    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(normalized):
        character = normalized[index]
        if quote is not None:
            output.append(character)
            if character == quote:
                if index + 1 < len(normalized) and normalized[index + 1] == quote:
                    output.append(normalized[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
            output.append(character)
        elif character == "?" and translate_placeholders:
            output.append("%s")
        else:
            output.append(character)
        index += 1
    return "".join(output)


def _open_sqlite_connection(
    database: str,
    *,
    check_same_thread: bool,
) -> sqlite3.Connection:
    # Authentication and other deadline-bound reads must not inherit the
    # process-wide 30s SQLite lock wait.  Resolve the remaining request budget
    # before opening the handle and apply it to both connect() and PRAGMA so a
    # writer holding BEGIN IMMEDIATE cannot stretch a 60ms/250ms endpoint into
    # a multi-hundred-millisecond response.
    remaining = _database_remaining_seconds()
    connect_timeout = SQLITE_BUSY_TIMEOUT_SECONDS
    if remaining is not None:
        connect_timeout = max(0.001, min(connect_timeout, remaining))
    connection = sqlite3.connect(
        database,
        timeout=connect_timeout,
        check_same_thread=check_same_thread,
        factory=_SqliteConnection,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.create_function(
            "metahub_sha256",
            1,
            _metahub_sha256,
            deterministic=True,
        )
        connection.create_function(
            "metahub_password_hash_algorithm",
            1,
            _metahub_password_hash_algorithm,
            deterministic=True,
        )
        # Install the request budget before any SQLite pragma that can need a
        # lock.  In the scheduler profile, control and gateway are separate
        # processes sharing one database; a first connection in either process
        # may still be promoting the journal to WAL.
        busy_timeout_ms = SQLITE_BUSY_TIMEOUT_MILLISECONDS
        if remaining is not None:
            busy_timeout_ms = max(1, min(busy_timeout_ms, int(remaining * 1000)))
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        # The local scheduler profile has many short-lived writers while health and
        # console reads must remain available. WAL lets readers keep their snapshot
        # while a writer commits, avoiding rollback-journal read starvation.
        if database != ":memory:":
            # Do not let a deadline-bound request wait behind another thread's
            # cross-process WAL promotion.  A later non-deadline connection can
            # finish the promotion; reads and writes remain valid in rollback
            # journal mode in the meantime.
            lock_timeout = None if remaining is None else max(0.0, remaining)
            acquired = _SQLITE_WAL_LOCK.acquire(timeout=lock_timeout) if lock_timeout is not None else _SQLITE_WAL_LOCK.acquire()
            if acquired:
                try:
                    if database not in _SQLITE_WAL_DATABASES:
                        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                        if str(journal_mode).casefold() != "wal":
                            if remaining is None:
                                connection.execute("PRAGMA journal_mode=WAL").close()
                            else:
                                journal_mode = "deadline-deferred"
                        if str(journal_mode).casefold() == "wal":
                            if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 1:
                                connection.execute("PRAGMA synchronous=NORMAL")
                            _SQLITE_WAL_DATABASES.add(database)
                finally:
                    _SQLITE_WAL_LOCK.release()
        # Multi-workspace credential projection intentionally fans out from one User trigger.
        # Recursive trigger execution would re-enter the global credential revision transition.
        connection.execute("PRAGMA recursive_triggers = OFF")
        # SQLite defers parsing the schema until the first schema-dependent statement.
        # BEGIN IMMEDIATE does not load it: a cold worker otherwise holds the sole
        # writer while parsing thousands of tables/triggers before its first read.
        # For deadline-bound authentication, defer that parse until the caller has
        # installed its progress handler; otherwise a cold schema load can consume
        # the entire short budget before authentication begins.
        if remaining is None:
            connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").close()
    except BaseException:
        connection.close()
        raise
    return connection


def _sqlite_path(raw_path: str) -> str:
    """在 数据库基础设施 内部复用 sqlite path 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    if raw_path in {"", "/", "/:memory:"}:
        return ":memory:"
    if raw_path.startswith("//"):
        return raw_path[1:]
    if len(raw_path) >= 4 and raw_path[0] == "/" and raw_path[2] == ":":
        return raw_path[1:]
    return raw_path.lstrip("/")
