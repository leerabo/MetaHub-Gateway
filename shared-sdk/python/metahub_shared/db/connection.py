"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/db/connection.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from .errors import DbError


def connect_database(database_url: str | None) -> sqlite3.Connection:
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
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.Error as exc:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.connection_failed",
            f"Failed to connect database: {exc}",
            "Check METAHUB_DATABASE_URL and filesystem permissions.",
        ) from exc


def _sqlite_path(raw_path: str) -> str:
    """在 数据库基础设施 内部复用 sqlite path 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    if raw_path in {"", "/", "/:memory:"}:
        return ":memory:"
    if len(raw_path) >= 4 and raw_path[0] == "/" and raw_path[2] == ":":
        return raw_path[1:]
    return raw_path.lstrip("/")
