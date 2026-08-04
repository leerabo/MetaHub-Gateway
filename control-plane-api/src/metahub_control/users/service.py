"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/users/service.py
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from metahub_control.auth import AuthContext
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .models import (
    CreateUserRequest,
    RoleInfo,
    UpdateUserRoleRequest,
    UserInfo,
    UserListResponse,
    UserResponse,
)

logger = logging.getLogger("metahub.users")

# 用户状态白名单定义账号生命周期边界，配合服务层最后管理员保护共同生效。
VALID_STATUSES = {"active", "disabled"}


def list_users(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    request_id: str,
    trace_id: str,
) -> UserListResponse:
    """列出当前租户用户。

    查询条件必须包含 AuthContext 中的 tenant_id；用户管理接口没有任何跨租户查询入口。
    """

    normalized_status = _normalize_status(status)
    connection = _connect(config)
    params: list[str] = [context.user.tenant_id]
    status_filter = ""
    if normalized_status:
        status_filter = "AND status=?"
        params.append(normalized_status)

    rows = connection.execute(
        f"""
        SELECT user_id, tenant_id, email, display_name, role, status, created_at, updated_at, last_login_at
        FROM users
        WHERE tenant_id=? {status_filter}
        ORDER BY created_at ASC, email ASC
        """,
        params,
    ).fetchall()
    return UserListResponse(
        users=[_user_info(row) for row in rows],
        roles=_list_roles(connection, context.user.tenant_id),
        request_id=request_id,
        trace_id=trace_id,
    )


def create_user(
    config: AppConfig,
    context: AuthContext,
    payload: CreateUserRequest,
    *,
    request_id: str,
    trace_id: str,
) -> UserResponse:
    """创建当前租户用户。

    明文初始密码只在本函数内参与哈希，不能写入日志、响应或数据库中的非哈希字段。
    """

    email = payload.email.strip().lower()
    display_name = payload.display_name.strip()
    status = _normalize_status(payload.status)
    if not email or "@" not in email or not display_name or not status:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("user.create_invalid")
    if len(payload.password) < 8:
        raise ApiError("user.password_invalid")

    connection = _connect(config)
    role = _require_active_role(connection, context.user.tenant_id, payload.role)
    # 租户边界必须来自认证上下文或已校验记录，不能相信客户端传入的 tenant_id。
    if _email_exists(connection, context.user.tenant_id, email):
        raise ApiError("user.email_exists")

    user_id = str(uuid4())
    try:
        connection.execute(
            """
            INSERT INTO users(
              user_id, tenant_id, email, display_name, password_hash, role, status,
              created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                context.user.tenant_id,
                email,
                display_name,
                hash_password(payload.password),
                role,
                status,
                _to_db_time(_utcnow()),
                _to_db_time(_utcnow()),
            ),
        )
        connection.commit()
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 租户边界必须来自认证上下文或已校验记录，不能相信客户端传入的 tenant_id。
        if "users.tenant_id, users.email" in str(exc) or "UNIQUE" in str(exc):
            raise ApiError("user.email_exists") from exc
        raise ApiError("user.create_invalid") from exc

    user = _load_user(connection, context.user.tenant_id, user_id)
    logger.info(
        "user.created",
        extra={
            "event": "user.created",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": user_id,
            "payload": {"email": email, "role": role, "status": status},
        },
    )
    return UserResponse(user=user, request_id=request_id, trace_id=trace_id)


def update_user_role(
    config: AppConfig,
    context: AuthContext,
    user_id: str,
    payload: UpdateUserRoleRequest,
    *,
    request_id: str,
    trace_id: str,
) -> UserResponse:
    """修改当前租户用户角色。

    目标用户必须属于当前 tenant_id；跨租户 user_id 统一按 not_found 处理，避免泄露
    其他租户用户是否存在。
    """

    connection = _connect(config)
    target = _load_user(connection, context.user.tenant_id, user_id)
    new_role = _require_active_role(connection, context.user.tenant_id, payload.role)
    # 权限判断必须在后端完成，避免只靠前端隐藏入口造成越权。
    if target.role == new_role:
        return UserResponse(user=target, request_id=request_id, trace_id=trace_id)
    # 该分支影响安全或治理链路，需要用中文说明处理边界。
    if (
        target.role == "admin"
        and target.status == "active"
        and new_role != "admin"
        and _active_admin_count(connection, context.user.tenant_id) <= 1
    ):
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("user.last_admin_required")
    if target.user_id == context.user.user_id and target.role == "admin" and new_role != "admin":
        raise ApiError("user.self_role_change_denied")

    connection.execute(
        """
        UPDATE users
        SET role=?, updated_at=?
        WHERE tenant_id=? AND user_id=?
        """,
        (new_role, _to_db_time(_utcnow()), context.user.tenant_id, user_id),
    )
    connection.commit()
    user = _load_user(connection, context.user.tenant_id, user_id)
    logger.info(
        "user.role_changed",
        extra={
            "event": "user.role_changed",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": user_id,
            "payload": {"before_role": target.role, "after_role": new_role},
        },
    )
    return UserResponse(user=user, request_id=request_id, trace_id=trace_id)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 控制面用户管理 所需的数据库连接，并把基础设施异常映射到统一错误模型。
    
    边界：调用方只看到业务错误码，不直接暴露数据库路径、SQL 或连接细节。
    """


    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _normalize_status(value: str | None) -> str | None:
    """规范化 控制面用户管理 的 normalize status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_STATUSES:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("user.create_invalid")
    return normalized


def _require_active_role(connection: sqlite3.Connection, tenant_id: str, role: str) -> str:
    """按当前租户加载 控制面用户管理 的 require active role 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    normalized = role.strip().lower()
    row = connection.execute(
        """
        SELECT code
        FROM roles
        WHERE tenant_id=? AND code=? AND status='active'
        """,
        (tenant_id, normalized),
    ).fetchone()
    if not row:
        # 后端权限校验是最终边界，前端隐藏按钮不能替代这里的拒绝逻辑。
        raise ApiError("user.role_invalid")
    return str(row["code"])


def _list_roles(connection: sqlite3.Connection, tenant_id: str) -> list[RoleInfo]:
    """在 控制面用户管理 内部复用 list roles 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    rows = connection.execute(
        """
        SELECT code, name, description
        FROM roles
        WHERE tenant_id=? AND status='active'
        ORDER BY CASE code
            WHEN 'admin' THEN 1
            WHEN 'developer' THEN 2
            WHEN 'viewer' THEN 3
            WHEN 'finance_admin' THEN 4
            WHEN 'security_admin' THEN 5
            WHEN 'ops_admin' THEN 6
            ELSE 7
        END, code
        """,
        (tenant_id,),
    ).fetchall()
    return [
        RoleInfo(code=row["code"], name=row["name"], description=row["description"])
        for row in rows
    ]


def _email_exists(connection: sqlite3.Connection, tenant_id: str, email: str) -> bool:
    """在 控制面用户管理 内部复用 email exists 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    row = connection.execute(
        "SELECT 1 FROM users WHERE tenant_id=? AND email=?",
        (tenant_id, email),
    ).fetchone()
    return row is not None


def _load_user(connection: sqlite3.Connection, tenant_id: str, user_id: str) -> UserInfo:
    """按当前租户加载 控制面用户管理 的 load user 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    row = connection.execute(
        """
        SELECT user_id, tenant_id, email, display_name, role, status, created_at, updated_at, last_login_at
        FROM users
        WHERE tenant_id=? AND user_id=?
        """,
        (tenant_id, user_id),
    ).fetchone()
    if not row:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("user.not_found")
    return _user_info(row)


def _active_admin_count(connection: sqlite3.Connection, tenant_id: str) -> int:
    """在 控制面用户管理 内部复用 active admin count 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    row = connection.execute(
        "SELECT COUNT(*) AS total FROM users WHERE tenant_id=? AND role='admin' AND status='active'",
        (tenant_id,),
    ).fetchone()
    return int(row["total"] if row else 0)


def _user_info(row: sqlite3.Row) -> UserInfo:
    """在 控制面用户管理 内部复用 user info 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return UserInfo(
        user_id=row["user_id"],
        tenant_id=row["tenant_id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        status=row["status"],
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        last_login_at=_parse_optional_db_time(row["last_login_at"]),
    )


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析 控制面用户管理 的 parse optional db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    if not value:
        return None
    return _parse_db_time(value)


def _parse_db_time(value: object) -> datetime:
    """解析 控制面用户管理 的 parse db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    raw = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 控制面用户管理 的 UTC 时间序列化为数据库 ISO 字符串。
    
    边界：统一时间口径可避免分页、审计和测试在时区上产生歧义。
    """


    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回 控制面用户管理 使用的当前 UTC 时间，去掉微秒以保持数据库字段稳定。
    
    边界：所有状态更新时间共用该口径，避免同一事务内出现混合时间格式。
    """


    return datetime.now(UTC).replace(microsecond=0)
