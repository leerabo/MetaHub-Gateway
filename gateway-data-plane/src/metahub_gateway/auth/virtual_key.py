"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义。
Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/auth/virtual_key.py
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_secret
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

logger = logging.getLogger("metahub.gateway.auth")


@dataclass(frozen=True)
class GatewayAuthContext:
    """聚合 控制面认证与会话 的可信上下文，调用方只能从这里读取已校验的身份和租户边界。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    tenant_id: str
    virtual_key_id: str
    key_prefix: str = field(repr=False)
    environment: str
    environment_id: str = ""
    environment_name: str = ""
    environment_status: str = "active"
    project_id: str | None = None
    application_id: str | None = None


def authenticate_virtual_key(config: AppConfig, authorization: str | None) -> GatewayAuthContext:
    """通过 OpenAI-compatible Bearer Token 恢复租户上下文。

    数据面不信任请求体里的 tenant_id，也不接受前端传入租户参数；模型调用的租户边界
    只能来自 Virtual Key。Virtual Key 明文只在内存中参与哈希匹配，不能落日志。
    """

    token = _extract_bearer(authorization)
    connection = _connect(config)
    try:
        row = connection.execute(
            """
            SELECT
              vk.virtual_key_id, vk.tenant_id, vk.key_prefix, vk.environment,
              e.environment_id AS environment_id,
              e.name AS environment_name,
              e.status AS environment_status,
              vk.project_id, vk.application_id,
              vk.status, t.status AS tenant_status
            FROM virtual_keys vk
            JOIN tenants t ON t.tenant_id = vk.tenant_id
            JOIN environments e
              ON e.tenant_id=vk.tenant_id
             AND (e.environment_id=vk.environment_id
                  OR (vk.environment_id IS NULL AND e.code=vk.environment))
            WHERE vk.key_hash=?
            """,
            (hash_secret(token),),
        ).fetchone()
        # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
        if not row or row["status"] != "active" or row["tenant_status"] != "active":
            # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
            raise ApiError("gateway.auth_invalid")
        _mark_last_used(connection, row["tenant_id"], row["virtual_key_id"])
        return GatewayAuthContext(
            tenant_id=row["tenant_id"],
            virtual_key_id=row["virtual_key_id"],
            key_prefix=row["key_prefix"],
            environment=row["environment"],
            environment_id=row["environment_id"],
            environment_name=row["environment_name"],
            environment_status=row["environment_status"],
            project_id=row["project_id"],
            application_id=row["application_id"],
        )
    finally:
        # 每次模型调用和反馈提交都会鉴权；无论成功或拒绝都必须释放 SQLite 句柄，避免长期泄漏。
        connection.close()


def _extract_bearer(authorization: str | None) -> str:
    """在 控制面认证与会话 内部复用 extract bearer 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    # 认证请求是访问控制边界，调用方必须透传统一错误和 request_id。
    if not authorization:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("gateway.auth_missing")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
        raise ApiError("gateway.auth_invalid")
    return token.strip()


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 控制面认证与会话 所需的数据库连接，并把基础设施异常映射到统一错误模型。

    边界：调用方只看到业务错误码，不直接暴露数据库路径、SQL 或连接细节。
    """

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _mark_last_used(connection: sqlite3.Connection, tenant_id: str, virtual_key_id: str) -> None:
    """记录 Virtual Key 最近使用时间。

    该字段服务于控制台排障和安全清理，不参与本次鉴权决策；更新失败不能把已通过鉴权
    的模型调用变成 500，因此这里只写 warning 并继续。
    """

    try:
        connection.execute(
            """
            UPDATE virtual_keys
            SET last_used_at=?, updated_at=?
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (_to_db_time(_utcnow()), _to_db_time(_utcnow()), tenant_id, virtual_key_id),
        )
        connection.commit()
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.Error:
        logger.warning(
            "virtual_key.last_used_update_failed",
            extra={
                "event": "virtual_key.last_used_update_failed",
                "tenant_id": tenant_id,
                "object_id": virtual_key_id,
            },
        )


def _to_db_time(value: datetime) -> str:
    """把 控制面认证与会话 的 UTC 时间序列化为数据库 ISO 字符串。

    边界：统一时间口径可避免分页、审计和测试在时区上产生歧义。
    """

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回 控制面认证与会话 使用的当前 UTC 时间，去掉微秒以保持数据库字段稳定。

    边界：所有状态更新时间共用该口径，避免同一事务内出现混合时间格式。
    """

    return datetime.now(UTC).replace(microsecond=0)
