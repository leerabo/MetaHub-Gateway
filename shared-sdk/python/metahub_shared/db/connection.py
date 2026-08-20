"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，
Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/db/connection.py
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .errors import DbError

# The local runtime opens one SQLite connection per request. A short default
# timeout turns the initial dashboard fan-out into avoidable 503 responses
# when two requests briefly contend for the single SQLite writer.
SQLITE_BUSY_TIMEOUT_SECONDS = 30
SQLITE_BUSY_TIMEOUT_MILLISECONDS = SQLITE_BUSY_TIMEOUT_SECONDS * 1000


class _RequestConnection:
    """One request's connection facade; ``close`` never releases it early."""

    __slots__ = ("_connection", "_closed")

    def __init__(self, connection: sqlite3.Connection) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_closed", False)

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
        try:
            if self._connection.in_transaction:
                self._connection.rollback()
        finally:
            self._connection.close()


@dataclass
class _DatabaseRequestScope:
    connections: dict[str, _RequestConnection] = field(default_factory=dict)

    def connection(self, database: str) -> _RequestConnection:
        existing = self.connections.get(database)
        if existing is not None:
            return existing
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
def database_request_scope() -> Iterator[None]:
    """Share one SQLite handle inside one HTTP request and close it at the boundary."""

    scope = _DatabaseRequestScope()
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
