"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，
Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/db/connection.py
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .errors import DbError
from .security import password_hash_algorithm

# The local runtime opens one SQLite connection per request. A short default
# timeout turns the initial dashboard fan-out into avoidable 503 responses
# when two requests briefly contend for the single SQLite writer.
SQLITE_BUSY_TIMEOUT_SECONDS = 30
SQLITE_BUSY_TIMEOUT_MILLISECONDS = SQLITE_BUSY_TIMEOUT_SECONDS * 1000
_MAX_IDLE_CONNECTIONS_PER_DATABASE = 4
_MAX_IDLE_DATABASES = 4


class _IdleConnectionPool:
    """Keep a small set of parsed SQLite schemas warm between sensitive requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: OrderedDict[str, list[sqlite3.Connection]] = OrderedDict()

    def acquire(self, database: str) -> sqlite3.Connection:
        with self._lock:
            available = self._connections.get(database)
            if available:
                connection = available.pop()
                if not available:
                    del self._connections[database]
                return connection
        # Overflow requests open independently instead of blocking the event loop while
        # another request owns every warm handle.
        return _open_sqlite_connection(database, check_same_thread=False)

    def release(self, database: str, connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.rollback()
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=OFF")
            connection.execute("PRAGMA read_uncommitted=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
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


def _close_idle_database_connections() -> None:
    """Close retained handles during tests or an orderly process shutdown."""

    _IDLE_CONNECTION_POOL.close_idle()


class _RequestConnection:
    """One request's connection facade; ``close`` never releases it early."""

    __slots__ = ("_connection", "_closed", "_release_connection")

    def __init__(
        self,
        connection: sqlite3.Connection,
        release_connection: Callable[[sqlite3.Connection], None] | None = None,
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


@dataclass
class _DatabaseRequestScope:
    reuse_connections: bool = False
    connections: dict[str, _RequestConnection] = field(default_factory=dict)

    def connection(self, database: str) -> _RequestConnection:
        existing = self.connections.get(database)
        if existing is not None:
            return existing
        if self.reuse_connections:
            raw = _IDLE_CONNECTION_POOL.acquire(database)
            scoped = _RequestConnection(
                raw,
                lambda connection: _IDLE_CONNECTION_POOL.release(database, connection),
            )
        else:
            raw = _open_sqlite_connection(database, check_same_thread=False)
            scoped = _RequestConnection(raw)
        self.connections[database] = scoped
        return scoped

    def close(self) -> None:
        for connection in tuple(self.connections.values()):
            connection._release()
        self.connections.clear()


_DATABASE_REQUEST_SCOPE: ContextVar[_DatabaseRequestScope | None] = ContextVar(
    "metahub_database_request_scope",
    default=None,
)


@contextmanager
def database_request_scope(*, reuse_connections: bool = False) -> Iterator[None]:
    """Share one SQLite handle inside one HTTP request and close it at the boundary."""

    scope = _DatabaseRequestScope(reuse_connections=reuse_connections)
    token = _DATABASE_REQUEST_SCOPE.set(scope)
    try:
        yield
    finally:
        _DATABASE_REQUEST_SCOPE.reset(token)
        scope.close()


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
) -> sqlite3.Connection:
    """连接 PRD-003 支持的本地数据库。

    当前运行时没有 PostgreSQL 驱动，因此 M0 的可运行实现使用 sqlite:// URL 完成
    迁移框架、约束、幂等和 seed 验证。生产 PostgreSQL 接入时保留同一 CLI 入口。
    """

    if not database_url:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.config_missing",
            "METAHUB_DATABASE_URL is required.",
            "Set METAHUB_DATABASE_URL=sqlite:///./.data/metahub.db for local development.",
        )
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.connection_failed",
            f"Unsupported database URL scheme: {parsed.scheme}",
            "Current local runtime supports sqlite:// URLs; install a PostgreSQL driver later for postgresql://.",
        )

    path = _sqlite_path(parsed.path)
    if path != ":memory:":
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        database = str(db_path)
    else:
        database = path

    try:
        # 默认保持 sqlite3 的线程 owner；只有显式受 admission 串行保护的 runtime
        # 连接才允许跨 worker 复用。
        scope = _DATABASE_REQUEST_SCOPE.get()
        if scope is not None and database != ":memory:":
            return scope.connection(database)  # type: ignore[return-value]
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


def _open_sqlite_connection(
    database: str,
    *,
    check_same_thread: bool,
) -> sqlite3.Connection:
    connection = sqlite3.connect(
        database,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        check_same_thread=check_same_thread,
    )
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
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MILLISECONDS}")
    return connection


def _sqlite_path(raw_path: str) -> str:
    """在 数据库基础设施 内部复用 sqlite path 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    if raw_path in {"", "/", "/:memory:"}:
        return ":memory:"
    if len(raw_path) >= 4 and raw_path[0] == "/" and raw_path[2] == ":":
        return raw_path[1:]
    return raw_path.lstrip("/")
