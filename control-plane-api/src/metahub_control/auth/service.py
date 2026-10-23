"""控制面认证服务：处理租户内登录、Session 生命周期和实时权限快照。

账号枚举、跨租户会话和撤权竞态均采用关闭式处理；日志只保留 request_id、主体标识与
脱敏审计事实，不记录密码、Bearer Token 或其他认证材料。
"""

from __future__ import annotations

import hashlib
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database, database_operation_deadline
from metahub_shared.db.errors import DbError
from metahub_shared.db.schema import table_exists
from metahub_shared.db.security import hash_token, verify_password
from metahub_shared.rbac import (
    ADMIN_ROLE,
    CATALOG_REVISION,
    DEVELOPER_ROLE,
    FINANCE_ADMIN_ROLE,
    OPS_ADMIN_ROLE,
    PERMISSION_CODES,
    ROLE_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    SECURITY_ADMIN_ROLE,
    VIEWER_ROLE,
    RoleCode,
    authorization_fingerprint,
    canonical_permission_codes,
    permission_matrix_sha256,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.rbac_snapshot import require_actor_runtime_permissions

from metahub_control.workspace_access.brand_permissions import (
    load_verified_workspace_brand_permissions,
)
from metahub_control.workspace_access.edge_permissions import (
    load_verified_workspace_edge_permissions,
    resolve_verified_workspace_edge_role,
)
from metahub_control.workspace_access.provider_catalog_permissions import (
    PROVIDER_CATALOG_PERMISSION_CODE,
    load_verified_provider_catalog_permissions,
)

from .models import AuthSession, AuthTenant, AuthUser, LoginResponse, MeResponse
from .tokens import TokenClaims, decode_access_token, encode_access_token

logger = logging.getLogger("metahub.auth")

# 角色类型和常量仍由 auth 包对外导出，但授权判断只允许使用数据库 permission 快照。
__all__ = (
    "ADMIN_ROLE",
    "DEVELOPER_ROLE",
    "FINANCE_ADMIN_ROLE",
    "OPS_ADMIN_ROLE",
    "ROLE_CODES",
    "SECURITY_ADMIN_ROLE",
    "VIEWER_ROLE",
    "RoleCode",
    "refresh_transaction_authentication",
    "require_transaction_permission",
)

# 固定假哈希用于不存在账号时做一次密码校验，降低账号枚举时的明显时序差异。
_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$200000$AAAAAAAAAAAAAAAAAAAAAA==$9JrZB7v3Jps9SMpi45e5yM/OXh+1xPzXD7fF22bWm6w="
)

# Read-heavy console refreshes authenticate many requests at once. Updating the
# same session row for every read turns that fan-out into a SQLite writer queue;
# identity and permission snapshots are still checked on every request, while
# this telemetry write is coalesced for a short interval per local worker.
_LAST_SEEN_MIN_INTERVAL_SECONDS = 5.0
_LAST_SEEN_GATE_LOCK = threading.Lock()
_LAST_SEEN_GATE: dict[str, float] = {}


def _claim_last_seen_update(session_id: str, now_monotonic: float) -> bool:
    """Reserve one recent ``last_seen`` write for a session in this worker."""

    with _LAST_SEEN_GATE_LOCK:
        previous = _LAST_SEEN_GATE.get(session_id)
        if previous is not None and now_monotonic - previous < _LAST_SEEN_MIN_INTERVAL_SECONDS:
            return False
        _LAST_SEEN_GATE[session_id] = now_monotonic
        # Bound memory if a long-lived worker sees many expired sessions.
        if len(_LAST_SEEN_GATE) > 4096:
            cutoff = now_monotonic - _LAST_SEEN_MIN_INTERVAL_SECONDS
            for key, value in tuple(_LAST_SEEN_GATE.items()):
                if value < cutoff:
                    _LAST_SEEN_GATE.pop(key, None)
        return True


def _release_last_seen_update(session_id: str, now_monotonic: float) -> None:
    """Allow a failed write to be retried instead of suppressing telemetry."""

    with _LAST_SEEN_GATE_LOCK:
        if _LAST_SEEN_GATE.get(session_id) == now_monotonic:
            _LAST_SEEN_GATE.pop(session_id, None)


def _prime_last_seen_gate(session_id: str) -> None:
    """Treat the login transaction as the first recent session observation.

    The session row is created with its current activity timestamp during
    login.  Priming the in-process gate keeps the first authenticated console
    fan-out read-only, while later requests still refresh the timestamp at the
    configured interval.
    """

    _claim_last_seen_update(session_id, time.monotonic())


def _extension_authorization_fingerprint(
    base_fingerprint: str,
    extension_permissions: tuple[str, ...],
) -> str:
    """Bind response change detection to every verified permission extension."""

    material = "\0".join(
        ("workspace-permission-extensions-v2", base_fingerprint, *extension_permissions)
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()


def _auth_user_with_extensions(
    user: AuthUser,
    extension_permissions: tuple[str, ...],
) -> AuthUser:
    if not extension_permissions:
        return user
    permissions = tuple(sorted({*user.permission_codes, *extension_permissions}))
    return user.model_copy(
        update={
            "permission_codes": permissions,
            "authorization_fingerprint": _extension_authorization_fingerprint(
                user.authorization_fingerprint,
                extension_permissions,
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    """承载一次数据库权限矩阵复核后的不可变授权快照。

    ``permission_codes`` 已经过目录、租户关系、角色上限和哈希四层校验；调用方不得
    使用共享目录默认值补齐它，否则撤权和矩阵损坏都会被静默绕过。
    """

    permission_codes: tuple[str, ...]
    permission_revision: int
    matrix_sha256: str
    authorization_fingerprint: str


@dataclass(frozen=True)
class AuthContext:
    """聚合 控制面认证与会话 的可信上下文，调用方只能从这里读取已校验的身份和租户边界。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    user: AuthUser
    tenant: AuthTenant
    session: AuthSession
    token: str
    token_jti: str
    extension_permission_codes: tuple[str, ...] = ()
    extension_authorization_fingerprint: str | None = None

    @property
    def permission_codes(self) -> tuple[str, ...]:
        """返回本次请求从数据库复核得到的不可变 permission code 集合。"""

        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        return self.user.permission_codes

    @property
    def permission_revision(self) -> int:
        """返回当前角色矩阵 revision，供业务响应和前端降权检测使用。"""

        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        return self.user.permission_revision

    @property
    def authorization_fingerprint(self) -> str:
        """返回不含 Token 和用户邮箱的授权指纹。"""

        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        return self.user.authorization_fingerprint


def login_admin(
    config: AppConfig,
    *,
    tenant_slug: str | None,
    email: str,
    password: str,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
) -> LoginResponse:
    """执行本地管理员登录。

    账号不存在、密码错误、用户禁用、租户禁用统一返回 auth.login_invalid，避免登录页被用来枚举账号。
    """

    normalized_email = email.strip().lower()
    normalized_tenant = (
        tenant_slug or config.default_tenant_slug
    ).strip() or config.default_tenant_slug
    now = _utcnow()
    connection = _connect(config)
    tenant = _tenant_by_slug(connection, normalized_tenant)
    user = _user_by_email(connection, tenant["tenant_id"], normalized_email) if tenant else None

    # 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
    if not tenant or tenant["status"] != "active" or not user or user["status"] != "active":
        _fake_password_check(password)
        _log_login_failed(request_id, trace_id, None, None, "invalid_identity")
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        connection.close()
        raise ApiError("auth.login_invalid")

    locked_until = _parse_datetime(user["locked_until"])
    if locked_until and locked_until > now:
        _log_login_failed(request_id, trace_id, tenant["tenant_id"], user["user_id"], "locked")
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        connection.close()
        raise ApiError("auth.login_locked")

    if not _verify_password(password, user["password_hash"]):
        _record_failed_login(connection, user, config, now)
        _log_login_failed(
            request_id,
            trace_id,
            tenant["tenant_id"],
            user["user_id"],
            "bad_password",
        )
        connection.close()
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(
            "auth.login_locked"
            if int(user["failed_login_count"] or 0) + 1 >= config.login_max_failures
            else "auth.login_invalid"
        )

    user = _record_successful_login(connection, user, now)
    try:
        # 登录响应也必须来自数据库矩阵；共享默认集合绝不能作为缺表或损坏时的回退值。
        snapshot = _load_permission_snapshot(
            connection,
            tenant_id=tenant["tenant_id"],
            user_id=user["user_id"],
            role_code=user["role"],
            unavailable_error_code="rbac.unavailable",
        )
        provider_catalog_permissions = load_verified_provider_catalog_permissions(
            connection,
            role_code=user["role"],
            unavailable_error_code="rbac.unavailable",
        )
        edge_role = resolve_verified_workspace_edge_role(
            connection,
            tenant_id=tenant["tenant_id"],
            legacy_user_id=user["user_id"],
            legacy_role_code=user["role"],
            unavailable_error_code="rbac.unavailable",
        )
        edge_permissions = load_verified_workspace_edge_permissions(
            connection,
            role_code=edge_role,
            unavailable_error_code="rbac.unavailable",
        )
        brand_permissions = load_verified_workspace_brand_permissions(
            connection,
            role_code=edge_role,
            unavailable_error_code="rbac.unavailable",
        )
        extension_permissions = tuple(
            sorted({*provider_catalog_permissions, *edge_permissions, *brand_permissions})
        )
    except Exception:
        # 登录失败路径同样释放 SQLite 句柄，避免 Windows 验收后数据库无法重命名。
        connection.close()
        raise
    expires_at = now + timedelta(seconds=config.session_ttl_seconds)
    session_id = f"ses_{uuid4().hex}"
    token_jti = f"jti_{uuid4().hex}"
    claims = TokenClaims(
        user_id=user["user_id"],
        tenant_id=tenant["tenant_id"],
        session_id=session_id,
        role=user["role"],
        jti=token_jti,
        expires_at=expires_at,
    )
    token = encode_access_token(claims, config.auth_token_secret, issued_at=now)
    connection.execute(
        """
        INSERT INTO sessions(
          session_id, tenant_id, user_id, token_jti, token_hash, status, expires_at,
          user_agent, created_ip, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            tenant["tenant_id"],
            user["user_id"],
            token_jti,
            hash_token(token),
            _to_db_time(expires_at),
            _safe_user_agent(user_agent),
            _safe_client_host(client_host),
            _to_db_time(now),
            _to_db_time(now),
        ),
    )
    connection.commit()
    # Login already persisted the current activity timestamp.  Avoid making
    # the first dashboard fan-out contend for the same SQLite writer lock.
    _prime_last_seen_gate(session_id)

    auth_user = _auth_user(user, snapshot)
    response_user = _auth_user_with_extensions(auth_user, extension_permissions)
    logger.info(
        "user.logged_in",
        extra={
            "event": "user.logged_in",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": tenant["tenant_id"],
            "actor_id": user["user_id"],
            "object_id": session_id,
            "payload": {
                # actor_id 已能在租户内唯一关联用户；邮箱属于个人信息，
                # 不应为方便检索而复制到运行日志。
                "session_id": session_id,
                "expires_at": _to_db_time(expires_at),
            },
        },
    )
    response = LoginResponse(
        access_token=token,
        expires_at=expires_at,
        session_id=session_id,
        user=response_user,
        permission_codes=response_user.permission_codes,
        permission_revision=response_user.permission_revision,
        authorization_fingerprint=response_user.authorization_fingerprint,
        request_id=request_id,
        trace_id=trace_id,
    )
    connection.close()
    return response


def authenticate_bearer(
    config: AppConfig,
    authorization: str | None,
    *,
    monotonic_deadline: float | None = None,
    deadline_error_code: str = "feedback.unavailable",
    connection: sqlite3.Connection | None = None,
    update_last_seen: bool = True,
    invalidate_connection: Callable[[sqlite3.Connection], None] | None = None,
) -> AuthContext:
    """从 Authorization Header 中恢复已登录用户上下文。

    ``monotonic_deadline`` 仅供具有端点级硬预算的内部依赖传入；设置后，Session 查询和
    ``last_seen`` 写入共用剩余 SQLite 锁等待与 progress handler，耗尽时转换为指定公开错误码。
    调用方可把 ``last_seen`` 延迟到自己的最终治理事务；默认连接由本函数关闭，显式传入
    连接由调用方的 request 或 app runtime owner 关闭。
    """

    token = _extract_bearer(authorization)
    claims = decode_access_token(token, config.auth_token_secret)
    deadline = _normalize_auth_deadline(monotonic_deadline, deadline_error_code)
    owns_connection = connection is None
    if connection is None:
        try:
            with database_operation_deadline(deadline):
                connection = _connect(config)
        except (ApiError, sqlite3.Error) as exc:
            if deadline is not None:
                raise ApiError(deadline_error_code) from exc
            raise
    connection_invalid = False

    def mark_connection_invalid() -> None:
        """记录 deadline 装配/恢复失败，交由 finally 在当前 slot owner 内退休。"""

        nonlocal connection_invalid
        connection_invalid = True

    try:
        with _authentication_sqlite_deadline(
            connection,
            deadline=deadline,
            error_code=deadline_error_code,
            invalidate_connection=mark_connection_invalid,
        ):
            # 认证结果只能在同一 deadline handler 内构造，返回前还会再次核对绝对期限。
            return _authenticate_bearer_context(
                connection,
                claims=claims,
                token=token,
                deadline=deadline,
                deadline_error_code=deadline_error_code,
                update_last_seen=update_last_seen,
            )
    # Session SQL 或提交失败时先撤销事务；端点级预算路径再折叠为稳定依赖错误。
    except sqlite3.Error as exc:
        connection_invalid = connection_invalid or deadline is not None
        try:
            if connection.in_transaction:
                connection.rollback()
        # 连回滚状态都不可用时，长期连接必须被标记污染并从 runtime 退休。
        except sqlite3.Error:
            connection_invalid = True
        if deadline is not None:
            # 有端点预算时锁等待、I/O 和提交故障统一失败关闭，不泄露 SQL 或数据库路径。
            raise ApiError(deadline_error_code) from exc
        # 普通控制面认证也必须使用稳定 503，而不能让 SQL 文本或数据库路径穿透响应。
        raise ApiError("rbac.unavailable") from exc
    finally:
        if owns_connection:
            try:
                connection.close()
            # 独占认证连接关闭失败不能覆盖已经归一的认证或数据库错误。
            except sqlite3.Error:
                pass
        elif connection_invalid:
            if invalidate_connection is not None:
                try:
                    invalidate_connection(connection)
                # owner callback 失败后直接关闭污染句柄，不能把异常回传成敏感内部错误。
                except Exception:
                    # callback 失败也不能让污染句柄继续留在调用者可见路径。
                    try:
                        connection.close()
                    # close 再失败也只保留原固定错误，调用方不得继续持有该连接。
                    except sqlite3.Error:
                        pass
            else:
                try:
                    connection.close()
                # 无 owner callback 时仍尽力关闭污染句柄，并保持原认证错误优先。
                except sqlite3.Error:
                    pass


@contextmanager
def authentication_database_deadline(
    connection: sqlite3.Connection,
    *,
    monotonic_deadline: float | None,
    error_code: str,
) -> Iterator[None]:
    """Apply one endpoint budget to a caller-owned authentication connection."""
    deadline = _normalize_auth_deadline(monotonic_deadline, error_code)
    with _authentication_sqlite_deadline(connection, deadline=deadline, error_code=error_code):
        yield
        if deadline is not None:
            _remaining_auth_deadline_ms(deadline, error_code)


@contextmanager
def _authentication_sqlite_deadline(
    connection: sqlite3.Connection,
    *,
    deadline: float | None,
    error_code: str,
    invalidate_connection: Callable[[], None] | None = None,
) -> Iterator[None]:
    """让认证锁等待、SQL 执行和 handler 清理落入同一 SQLite 错误边界。"""

    if deadline is None:
        yield
        return
    if getattr(connection, "backend", "sqlite") == "postgresql":
        with database_operation_deadline(deadline):
            yield
            _remaining_auth_deadline_ms(deadline, error_code)
        return
    remaining_ms = _remaining_auth_deadline_ms(deadline, error_code)
    # For endpoint budgets below 250ms, a lock wait should fail fast.  Waiting
    # the entire remaining budget can be repeated across the read and writer
    # phases and exceed the caller's outer SLA due to scheduler granularity.
    # Longer internal budgets retain their measured remaining timeout.
    sqlite_timeout_ms = min(remaining_ms, 1) if remaining_ms <= 250 else remaining_ms
    previous_timeout_ms: int | None = None
    timeout_changed = False
    handler_install_attempted = False
    first_error: sqlite3.Error | None = None

    def remember(exc: sqlite3.Error) -> None:
        """保留最早 SQLite 状态错误，同时通知长期连接 owner 后续退休该句柄。"""

        nonlocal first_error
        # 只保留最早连接状态错误，后续 handler/PRAGMA 清理失败不能改写因果顺序。
        if first_error is None:
            first_error = exc
        if invalidate_connection is not None:
            invalidate_connection()

    try:
        previous_timeout_row = connection.execute("PRAGMA busy_timeout").fetchone()
        # 无法读取旧锁预算时不能在长期共享连接上盲目覆盖并留下跨请求状态。
        if previous_timeout_row is None:
            raise sqlite3.OperationalError("busy timeout unavailable")
        previous_timeout_ms = int(previous_timeout_row[0])
        connection.execute(f"PRAGMA busy_timeout={sqlite_timeout_ms}")
        timeout_changed = True
        handler_install_attempted = True
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() >= deadline else 0,
            # Keep the handler coarse enough for ordinary summary reads to
            # finish within the 250ms budget. Cold schema parsing is deferred
            # until after this handler is installed, so short lock waits still
            # fail fast without per-instruction callback overhead.
            1_000,
        )
        yield
    # PRAGMA、类型转换或 handler 安装失败都污染长期连接，统一记为 SQLite 状态错误。
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        # 非 SQLite 类型漂移折叠为固定本地错误，避免把驱动值或对象正文带出认证层。
        if not isinstance(exc, sqlite3.Error):
            exc = sqlite3.OperationalError("busy timeout invalid")
        remember(exc)
    finally:
        if handler_install_attempted:
            try:
                connection.set_progress_handler(None, 0)
            # handler 无法移除时连接可能继续跨请求中断 SQL，必须通知 owner 退休。
            except sqlite3.Error as exc:
                remember(exc)
        if timeout_changed and previous_timeout_ms is not None:
            try:
                connection.execute(f"PRAGMA busy_timeout={previous_timeout_ms}")
            # 原锁预算恢复失败会泄漏跨请求状态，同样标记长期连接不可复用。
            except sqlite3.Error as exc:
                remember(exc)
    # 清理阶段首错必须在 contextmanager 退出后传播，不能把污染连接当作认证成功。
    if first_error is not None:
        # 上层统一映射为固定依赖错误，同时按 callback 摘除长期连接引用。
        raise first_error


def _authenticate_bearer_context(
    connection: sqlite3.Connection,
    *,
    claims: TokenClaims,
    token: str,
    deadline: float | None,
    deadline_error_code: str,
    update_last_seen: bool,
) -> AuthContext:
    """在已装配 deadline 的连接上恢复实时 Session 与权限矩阵。"""

    row = connection.execute(
        """
        SELECT
          s.session_id, s.tenant_id, s.user_id, s.token_jti, s.token_hash, s.status,
          s.expires_at, u.email, u.display_name, u.role, u.status AS user_status,
          t.slug AS tenant_slug, t.name AS tenant_name, t.status AS tenant_status
        FROM sessions s
        JOIN users u ON u.user_id = s.user_id AND u.tenant_id = s.tenant_id
        JOIN tenants t ON t.tenant_id = s.tenant_id
        WHERE s.session_id=? AND s.token_jti=? AND s.tenant_id=? AND s.user_id=?
        """,
        (claims.session_id, claims.jti, claims.tenant_id, claims.user_id),
    ).fetchone()
    if not row:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露有效性细节。
        raise ApiError("auth.token_invalid")
    if row["token_hash"] != hash_token(token):
        raise ApiError("auth.token_invalid")
    if row["status"] != "active":
        # 撤销状态由服务端权威 Session 决定，不能只相信 Token 内 claims。
        raise ApiError("auth.session_revoked")

    now = _utcnow()
    expires_at = _parse_datetime(row["expires_at"])
    if not expires_at or expires_at <= now:
        if update_last_seen:
            # 普通认证继续收敛过期状态；PAGE-107 read-stage 把所有写入留给最终治理事务。
            connection.execute(
                "UPDATE sessions SET status='expired', updated_at=? WHERE session_id=?",
                (_to_db_time(now), row["session_id"]),
            )
            connection.commit()
        # 过期 Token 使用稳定认证错误，不披露 Session 行或数据库状态。
        raise ApiError("auth.token_expired")
    if row["user_status"] != "active" or row["tenant_status"] != "active":
        raise ApiError("auth.token_invalid")

    snapshot = _load_permission_snapshot(
        connection,
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        role_code=row["role"],
        unavailable_error_code=(
            deadline_error_code if deadline is not None else "rbac.unavailable"
        ),
    )
    provider_catalog_permissions = load_verified_provider_catalog_permissions(
        connection,
        role_code=row["role"],
        unavailable_error_code=(
            deadline_error_code if deadline is not None else "rbac.unavailable"
        ),
    )
    edge_role = resolve_verified_workspace_edge_role(
        connection,
        tenant_id=row["tenant_id"],
        legacy_user_id=row["user_id"],
        legacy_role_code=row["role"],
        unavailable_error_code=(
            deadline_error_code if deadline is not None else "rbac.unavailable"
        ),
    )
    edge_permissions = load_verified_workspace_edge_permissions(
        connection,
        role_code=edge_role,
        unavailable_error_code=(
            deadline_error_code if deadline is not None else "rbac.unavailable"
        ),
    )
    brand_permissions = load_verified_workspace_brand_permissions(
        connection,
        role_code=edge_role,
        unavailable_error_code=(
            deadline_error_code if deadline is not None else "rbac.unavailable"
        ),
    )
    extension_permissions = tuple(
        sorted({*provider_catalog_permissions, *edge_permissions, *brand_permissions})
    )

    if update_last_seen:
        # 普通 API 仍在认证成功后立即刷新；PAGE-107 会把该写入并入最终不可变审计事务。
        # Console fan-out requests still perform the full live auth/RBAC read;
        # only the low-value timestamp write is coalesced to avoid serializing
        # every read behind SQLite's single writer.
        seen_gate_time = time.monotonic()
        if _claim_last_seen_update(row["session_id"], seen_gate_time):
            try:
                connection.execute(
                    "UPDATE sessions SET last_seen_at=?, updated_at=? WHERE session_id=?",
                    (_to_db_time(now), _to_db_time(now), row["session_id"]),
                )
                connection.commit()
            except Exception:
                _release_last_seen_update(row["session_id"], seen_gate_time)
                raise

    context = AuthContext(
        user=_auth_user(row, snapshot),
        tenant=AuthTenant(
            tenant_id=row["tenant_id"],
            slug=row["tenant_slug"],
            name=row["tenant_name"],
        ),
        session=AuthSession(session_id=row["session_id"], expires_at=expires_at),
        token=token,
        token_jti=row["token_jti"],
        extension_permission_codes=extension_permissions,
        extension_authorization_fingerprint=(
            _extension_authorization_fingerprint(
                snapshot.authorization_fingerprint,
                extension_permissions,
            )
            if extension_permissions
            else None
        ),
    )
    if deadline is not None:
        # SQL 之间的 Python 校验也消耗入口预算，迟到结果不得在 handler 清除后返回成功。
        _remaining_auth_deadline_ms(deadline, deadline_error_code)
    return context


def _normalize_auth_deadline(
    value: float | None,
    error_code: str,
) -> float | None:
    """校验内部认证绝对 deadline，缺失时保留既有无预算认证行为。"""

    if value is None:
        return None
    # bool/NaN/Infinity 会破坏 timeout 计算，内部装配错误必须按公开依赖错误失败关闭。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(error_code)
    deadline = float(value)
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        # 已耗尽预算不能刷新为新的数据库等待窗口。
        raise ApiError(error_code)
    return deadline


def _remaining_auth_deadline_ms(deadline: float, error_code: str) -> int:
    """把认证剩余预算转换为严格正数的 SQLite busy_timeout 毫秒。"""

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        # 连接建立和 Token 校验也消耗同一预算，耗尽后禁止执行 Session SQL。
        raise ApiError(error_code)
    return max(1, int(remaining * 1000))


def current_user_response(
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> MeResponse:
    """执行控制面认证与会话的当前用户响应流程。

    集中处理输入校验、数据库写入和响应转换；服务层必须使用认证上下文中的租户和角色，
    不能相信前端传入的权限边界。
    """

    response_user = _auth_user_with_extensions(
        context.user,
        context.extension_permission_codes,
    )
    return MeResponse(
        user=response_user,
        tenant=context.tenant,
        session=context.session,
        permission_codes=response_user.permission_codes,
        permission_revision=context.permission_revision,
        authorization_fingerprint=response_user.authorization_fingerprint,
        request_id=request_id,
        trace_id=trace_id,
    )


def logout_current_session(config: AppConfig, context: AuthContext) -> None:
    """撤销当前 控制面认证与会话 会话，把 Session 标记为不可继续使用。

    边界：仅更新会话状态，不记录明文 Token，也不影响其他有效会话。
    """

    now = _utcnow()
    connection = _connect(config)
    connection.execute(
        """
        UPDATE sessions
        SET status='revoked', revoked_at=?, updated_at=?
        WHERE session_id=? AND status='active'
        """,
        (_to_db_time(now), _to_db_time(now), context.session.session_id),
    )
    connection.commit()


def require_admin_role(context: AuthContext) -> AuthContext:
    """要求当前用户具备租户用户管理权限。

    兼容入口只保留给用户管理等真正的管理员操作；领域 API 必须使用自己的 permission。
    """

    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    return require_permission(context, "user.manage")


def require_permission(context: AuthContext, permission_code: str) -> AuthContext:
    """要求当前数据库授权快照包含一个稳定 permission code。

    参数：``context`` 是本次 Bearer 认证得到的可信上下文；``permission_code`` 必须来自产品目录。
    返回：原上下文，便于 FastAPI dependency 继续向业务层传递可信 tenant_id。
    约束：未知服务端 code 视为发布包完整性错误；用户缺权统一返回 403。
    """

    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    extension_known = permission_code == PROVIDER_CATALOG_PERMISSION_CODE
    if type(permission_code) is not str or (
        permission_code not in PERMISSION_CODES and not extension_known
    ):
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        raise ApiError("rbac.matrix_integrity_failed")
    # 这里只消费已经过数据库哈希复核的集合，严禁改查共享默认角色集合。
    granted = (
        permission_code in context.extension_permission_codes
        if extension_known
        else permission_code in context.permission_codes
    )
    if not granted:
        raise ApiError("auth.permission_denied")
    return context


def refresh_transaction_authentication(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    unavailable_error_code: str,
) -> AuthContext:
    """在调用方现有事务内重读 Session owner 和完整权限矩阵。

    该入口只刷新可信认证上下文，不授予任何业务权限。需要执行受保护操作的调用方
    仍须使用 ``require_permission``；它主要供必须审计“已认证但缺少业务权限”的
    安全拒绝路径使用。

    参数：已开启事务的数据库连接、路由依赖恢复的可信认证上下文、稳定权限码，以及
    当前业务域使用的数据库不可用错误码。返回：使用事务内最新角色矩阵构造的新上下文。
    关键约束：该 helper 不接受 tenant/user/Session 的客户端输入；Session Token 只做哈希
    比较，不写入 SQL、日志或异常。调用方必须先取得自己的事务锁，使权限复核与后续写入
    共享同一数据库快照，关闭“路由鉴权通过后立即撤权”的竞态窗口。
    """

    # 没有外层事务就无法保证权限快照与后续业务写入原子一致，属于服务端装配故障。
    if not connection.in_transaction:
        raise ApiError(unavailable_error_code)
    tenant_id = context.user.tenant_id
    # AuthContext 的 tenant/user owner 理论上由认证层保证一致；再次失败关闭可防止领域层
    # 误拼来自不同请求的上下文，并统一按已失去当前操作授权返回 403。
    if tenant_id != context.tenant.tenant_id:
        raise ApiError("auth.permission_denied")
    now = _to_db_time(_utcnow())
    owner = connection.execute(
        """
        SELECT actor.role
        FROM sessions session
        JOIN users actor
          ON actor.tenant_id=session.tenant_id AND actor.user_id=session.user_id
        JOIN tenants tenant ON tenant.tenant_id=session.tenant_id
        WHERE session.tenant_id=? AND session.user_id=? AND session.session_id=?
          AND session.token_jti=? AND session.token_hash=?
          AND session.status='active' AND julianday(session.expires_at)>julianday(?)
          AND actor.status='active' AND tenant.status='active'
        """,
        (
            tenant_id,
            context.user.user_id,
            context.session.session_id,
            context.token_jti,
            hash_token(context.token),
            now,
        ),
    ).fetchone()
    # Session 撤销、过期、换 owner 或用户/租户禁用都意味着当前事务不能继续使用旧授权。
    # 这里统一返回 permission_denied，既不枚举具体失效事实，也保持二次授权检查的 403 合同。
    if owner is None:
        raise ApiError("auth.permission_denied")
    snapshot = _load_permission_snapshot(
        connection,
        tenant_id=tenant_id,
        user_id=context.user.user_id,
        role_code=owner["role"],
        unavailable_error_code=unavailable_error_code,
    )
    # 角色可能在路由依赖与业务事务之间合法变化；按实时角色构造不可变上下文，再复用统一
    # require_permission 校验目录码和缺权错误，不按角色名或共享默认矩阵推导权限。
    live_user = context.user.model_copy(
        update={
            "role": owner["role"],
            "permission_codes": snapshot.permission_codes,
            "permission_revision": snapshot.permission_revision,
            "authorization_fingerprint": snapshot.authorization_fingerprint,
        }
    )
    live_context = AuthContext(
        user=live_user,
        tenant=context.tenant,
        session=context.session,
        token=context.token,
        token_jti=context.token_jti,
    )
    return live_context


def refresh_transaction_authentication_fingerprint(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    unavailable_error_code: str,
) -> AuthContext:
    """在短写事务内复核 Session owner 与角色 revision/hash，不重扫完整目录。

    调用前的普通认证和来源事务已经完整校验目录、角色上限、关系与矩阵哈希。这里在
    ``BEGIN IMMEDIATE`` 保护下只重读会变化的 Session/actor/role 身份和授权指纹；调用方还
    必须在同一事务精确查询本次所需 grant，才能提交已构造的业务事实。
    """

    # 指纹复核只能发生在调用方持有的最终写事务内，避免认证与事实提交之间再次漂移。
    if not connection.in_transaction:
        raise ApiError(unavailable_error_code)
    tenant_id = context.user.tenant_id
    # AuthContext 内部 owner 不一致也按越权处理，不能使用其中任一 tenant 继续查询。
    if tenant_id != context.tenant.tenant_id:
        raise ApiError("auth.permission_denied")
    owner = connection.execute(
        """
        SELECT actor.role,role.permission_revision,role.matrix_sha256,
               role.is_permission_immutable
        FROM sessions session
        JOIN users actor
          ON actor.tenant_id=session.tenant_id AND actor.user_id=session.user_id
        JOIN tenants tenant ON tenant.tenant_id=session.tenant_id
        JOIN roles role ON role.tenant_id=actor.tenant_id AND role.code=actor.role
        WHERE session.tenant_id=? AND session.user_id=? AND session.session_id=?
          AND session.token_jti=? AND session.token_hash=?
          AND session.status='active' AND julianday(session.expires_at)>julianday(?)
          AND actor.status='active' AND tenant.status='active' AND role.status='active'
        """,
        (
            tenant_id,
            context.user.user_id,
            context.session.session_id,
            context.token_jti,
            hash_token(context.token),
            _to_db_time(_utcnow()),
        ),
    ).fetchone()
    # Session、用户、租户或角色任一失效都必须丢弃旧认证快照与已构造回答。
    if owner is None:
        raise ApiError("auth.permission_denied")
    role_code = owner["role"]
    revision = owner["permission_revision"]
    matrix_sha256 = owner["matrix_sha256"]
    expected_immutable = 1 if role_code == ADMIN_ROLE else 0
    if (
        type(role_code) is not str
        or role_code not in ROLE_CODES
        or type(revision) is not int
        or revision < 1
        or type(matrix_sha256) is not str
        or len(matrix_sha256) != 64
        or any(character not in "0123456789abcdef" for character in matrix_sha256)
        or owner["is_permission_immutable"] != expected_immutable
    ):
        # 角色 revision、矩阵摘要或不可变标记损坏时失败关闭，不能回退到 Shared 默认权限。
        raise ApiError("rbac.matrix_integrity_failed")
    fingerprint = authorization_fingerprint(
        tenant_id=tenant_id,
        user_id=context.user.user_id,
        role=role_code,
        permission_revision=revision,
        matrix_sha256=matrix_sha256,
    )
    # 只有指纹逐字未变时才保留前置完整快照中的权限集合；漂移上下文只可用于写拒绝审计。
    permission_codes = (
        context.permission_codes if fingerprint == context.authorization_fingerprint else ()
    )
    live_user = context.user.model_copy(
        update={
            "role": role_code,
            "permission_codes": permission_codes,
            "permission_revision": revision,
            "authorization_fingerprint": fingerprint,
        }
    )
    # 只返回同 tenant 的实时身份指纹；实际业务 grant 仍由调用方在本事务逐项查询。
    return AuthContext(
        user=live_user,
        tenant=context.tenant,
        session=context.session,
        token=context.token,
        token_jti=context.token_jti,
    )


def require_transaction_permission(
    connection: sqlite3.Connection,
    context: AuthContext,
    permission_code: str,
    *,
    unavailable_error_code: str,
) -> AuthContext:
    """在调用方现有事务内刷新认证上下文并要求一个实时业务权限。"""

    live_context = refresh_transaction_authentication(
        connection,
        context,
        unavailable_error_code=unavailable_error_code,
    )
    # 最终仍走全域统一权限 helper，确保未知权限码和缺权分别保持既有完整性错误与 403 口径。
    return require_permission(live_context, permission_code)


def require_actor_transaction_permissions(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    permission_codes: tuple[str, ...],
    unavailable_error_code: str,
) -> PermissionSnapshot:
    """在当前事务内重载持久操作者的权限快照，防止撤权后继续执行后台任务。

    Require permissions for a durable actor without a browser session.

    Batch and other recovery workers must resume work after the submitting login session has
    expired. They still have to re-read the active tenant, user, role, full catalog projection,
    role ceiling, matrix hash, and requested permission set in the caller's transaction. This
    helper accepts only identities already recovered from durable tenant-owned rows; HTTP input
    must never be passed directly to it.
    """

    snapshot = require_actor_runtime_permissions(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        permission_codes=permission_codes,
        unavailable_error_code=unavailable_error_code,
    )
    # 权限以后端实时矩阵为准；缺失任一所需权限时拒绝并保留脱敏审计事实。
    return PermissionSnapshot(
        permission_codes=snapshot.permission_codes,
        permission_revision=snapshot.permission_revision,
        matrix_sha256=snapshot.matrix_sha256,
        authorization_fingerprint=snapshot.authorization_fingerprint,
    )


def require_any_permission(context: AuthContext, permission_codes: tuple[str, ...]) -> AuthContext:
    """要求当前授权快照命中给定 permission code 中至少一项。

    该函数仅用于确实允许多种独立能力进入的兼容端点；空集合、重复或目录外 code
    都说明服务端装配错误，必须失败关闭而不是把错误配置解释为普通缺权。
    """

    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    if not permission_codes or len(permission_codes) != len(set(permission_codes)):
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        raise ApiError("rbac.matrix_integrity_failed")
    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    if any(type(code) is not str or code not in PERMISSION_CODES for code in permission_codes):
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        raise ApiError("rbac.matrix_integrity_failed")
    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    if not any(code in context.permission_codes for code in permission_codes):
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        raise ApiError("auth.permission_denied")
    return context


def _load_permission_snapshot(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    role_code: str,
    unavailable_error_code: str,
) -> PermissionSnapshot:
    """从当前租户数据库加载并复核一次完整权限快照。

    参数均来自已验证的用户、Session 和租户行；函数不接受 Header、query 或 Token 中的授权集合。
    返回值只有在表结构、目录投影、复合租户关系、角色上限和矩阵哈希全部一致时才会产生。
    """

    try:
        required_tables = {
            "roles",
            "permissions",
            "role_permissions",
            "rbac_permission_templates",
            "rbac_role_permission_templates",
        }
        if not all(table_exists(connection, table) for table in sorted(required_tables)):
            # 缺表属于权限目录不完整，不能被解释成旧版本并回退硬编码角色集合。
            raise ApiError("rbac.matrix_integrity_failed")

        template_by_code = _validate_permission_templates(connection)
        permission_by_id = _load_tenant_permission_catalog(connection, tenant_id, template_by_code)
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        if type(role_code) is not str or role_code not in ROLE_CODES:
            # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
            raise ApiError("rbac.matrix_integrity_failed")

        role_rows = connection.execute(
            """
            SELECT
              role_id, tenant_id, code, status, permission_revision, matrix_sha256,
              is_permission_immutable
            FROM roles
            WHERE tenant_id=? AND code=?
            """,
            (tenant_id, role_code),
        ).fetchall()
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        if len(role_rows) != 1:
            # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
            raise ApiError("rbac.matrix_integrity_failed")
        role = role_rows[0]
        revision = role["permission_revision"]
        expected_immutable = 1 if role_code == ADMIN_ROLE else 0
        if (
            role["tenant_id"] != tenant_id
            or role["status"] != "active"
            or type(revision) is not int
            or revision < 1
            or role["is_permission_immutable"] != expected_immutable
        ):
            # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
            raise ApiError("rbac.matrix_integrity_failed")

        permission_codes = _load_role_permission_codes(
            connection,
            tenant_id=tenant_id,
            role_id=role["role_id"],
            permission_by_id=permission_by_id,
        )
        grantable = ROLE_GRANTABLE_PERMISSIONS[role_code]
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        if not set(permission_codes) <= grantable:
            # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
            raise ApiError("rbac.matrix_integrity_failed")
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        if role_code == ADMIN_ROLE and set(permission_codes) != set(PERMISSION_CODES):
            # admin 是租户恢复入口，完整目录与不可变标记必须同时成立。
            raise ApiError("rbac.matrix_integrity_failed")

        calculated_sha256 = permission_matrix_sha256(permission_codes)
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        if role["matrix_sha256"] != calculated_sha256:
            # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
            raise ApiError("rbac.matrix_integrity_failed")
        fingerprint = authorization_fingerprint(
            tenant_id=tenant_id,
            user_id=user_id,
            role=role_code,
            permission_revision=revision,
            matrix_sha256=calculated_sha256,
        )
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        return PermissionSnapshot(
            permission_codes=permission_codes,
            permission_revision=revision,
            matrix_sha256=calculated_sha256,
            authorization_fingerprint=fingerprint,
        )
    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    except ApiError:
        raise
    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    except sqlite3.Error as exc:
        # 锁等待、I/O 和驱动错误统一折叠，响应不能包含 SQL 或数据库路径。
        raise ApiError(unavailable_error_code) from exc
    except (KeyError, TypeError, ValueError) as exc:
        # 非法 SQLite 动态类型和共享规范化失败都说明持久矩阵不可相信。
        raise ApiError("rbac.matrix_integrity_failed") from exc


def _validate_permission_templates(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, str, str, str, int]]:
    """校验数据库产品权限模板和角色授权上限。

    安全授权只绑定稳定 code；中文名称和描述是展示字段，但租户投影仍必须与数据库产品模板
    逐字段一致，防止 Permission API 与认证看到两套目录。
    """

    template_rows = connection.execute(
        """
        SELECT permission_code, category, name, description, risk_level, catalog_revision
        FROM rbac_permission_templates
        """
    ).fetchall()
    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    if len(template_rows) != len(PERMISSION_CODES):
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        raise ApiError("rbac.matrix_integrity_failed")

    template_by_code: dict[str, tuple[str, str, str, str, int]] = {}
    for row in template_rows:
        code = row["permission_code"]
        metadata = (
            row["category"],
            row["name"],
            row["description"],
            row["risk_level"],
            row["catalog_revision"],
        )
        if (
            type(code) is not str
            or code in template_by_code
            or code not in PERMISSION_CODES
            or any(type(value) is not str or not value for value in metadata[:3])
            or metadata[3] not in {"read", "write", "critical"}
            or metadata[4] != CATALOG_REVISION
        ):
            # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
            raise ApiError("rbac.matrix_integrity_failed")
        template_by_code[code] = metadata
    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    if set(template_by_code) != set(PERMISSION_CODES):
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        raise ApiError("rbac.matrix_integrity_failed")

    role_template_rows = connection.execute(
        """
        SELECT role_code, permission_code, default_granted
        FROM rbac_role_permission_templates
        """
    ).fetchall()
    expected_pairs = {
        (role, permission_code)
        for role, permission_codes in ROLE_GRANTABLE_PERMISSIONS.items()
        for permission_code in permission_codes
    }
    actual_pairs: set[tuple[str, str]] = set()
    for row in role_template_rows:
        pair = (row["role_code"], row["permission_code"])
        if pair in actual_pairs or pair not in expected_pairs or row["default_granted"] != 1:
            # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
            raise ApiError("rbac.matrix_integrity_failed")
        actual_pairs.add(pair)
    if actual_pairs != expected_pairs:
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        raise ApiError("rbac.matrix_integrity_failed")
    return template_by_code


def _load_tenant_permission_catalog(
    connection: sqlite3.Connection,
    tenant_id: str,
    template_by_code: dict[str, tuple[str, str, str, str, int]],
) -> dict[str, str]:
    """加载并复核当前租户的 Permission 目录投影。

    返回 ``permission_id -> code`` 映射供复合关系复核；目录缺项、重复 ID/code、动态类型、
    非 active 状态或模板字段漂移均失败关闭，不返回部分目录。
    """

    rows = connection.execute(
        """
        SELECT
          permission_id, tenant_id, code, category, name, description, risk_level,
          catalog_revision, status
        FROM permissions
        WHERE tenant_id=?
        """,
        (tenant_id,),
    ).fetchall()
    if len(rows) != len(template_by_code):
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        raise ApiError("rbac.matrix_integrity_failed")

    permission_by_id: dict[str, str] = {}
    seen_codes: set[str] = set()
    for row in rows:
        permission_id = row["permission_id"]
        code = row["code"]
        if (
            type(permission_id) is not str
            or not permission_id
            or permission_id in permission_by_id
            or row["tenant_id"] != tenant_id
            or type(code) is not str
            or code in seen_codes
            or code not in template_by_code
        ):
            # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
            raise ApiError("rbac.matrix_integrity_failed")
        metadata = (
            row["category"],
            row["name"],
            row["description"],
            row["risk_level"],
            row["catalog_revision"],
        )
        if metadata != template_by_code[code] or row["status"] != "active":
            # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
            raise ApiError("rbac.matrix_integrity_failed")
        permission_by_id[permission_id] = code
        seen_codes.add(code)

    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    if seen_codes != set(PERMISSION_CODES):
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        raise ApiError("rbac.matrix_integrity_failed")
    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    return permission_by_id


def _load_role_permission_codes(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    role_id: str,
    permission_by_id: dict[str, str],
) -> tuple[str, ...]:
    """复核涉及当前租户的 RolePermission 关系并返回目标角色权限集合。

    查询同时覆盖关系 tenant、角色 tenant 和 Permission tenant 任一命中当前租户的行，
    因而外键被关闭后伪造的跨租户同形 ID 也不能躲过认证检查。
    """

    rows = connection.execute(
        """
        SELECT
          rp.role_permission_id,
          rp.tenant_id AS relation_tenant_id,
          rp.role_id AS relation_role_id,
          rp.permission_id AS relation_permission_id,
          r.tenant_id AS role_tenant_id,
          r.code AS relation_role_code,
          p.tenant_id AS permission_tenant_id,
          p.code AS permission_code,
          p.status AS permission_status
        FROM role_permissions rp
        LEFT JOIN roles r ON r.role_id=rp.role_id
        LEFT JOIN permissions p ON p.permission_id=rp.permission_id
        WHERE rp.tenant_id=? OR r.tenant_id=? OR p.tenant_id=?
        """,
        (tenant_id, tenant_id, tenant_id),
    ).fetchall()

    relation_ids: set[str] = set()
    granted_codes: list[str] = []
    for row in rows:
        relation_id = row["role_permission_id"]
        relation_role_code = row["relation_role_code"]
        permission_id = row["relation_permission_id"]
        permission_code = row["permission_code"]
        if (
            type(relation_id) is not str
            or not relation_id
            or relation_id in relation_ids
            or row["relation_tenant_id"] != tenant_id
            or row["role_tenant_id"] != tenant_id
            or row["permission_tenant_id"] != tenant_id
            or relation_role_code not in ROLE_CODES
            or row["permission_status"] != "active"
            or permission_by_id.get(permission_id) != permission_code
            or permission_code not in ROLE_GRANTABLE_PERMISSIONS[relation_role_code]
        ):
            # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
            raise ApiError("rbac.matrix_integrity_failed")
        relation_ids.add(relation_id)
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        if row["relation_role_id"] == role_id:
            granted_codes.append(permission_code)

    try:
        # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
        return canonical_permission_codes(granted_codes)
    # 认证和矩阵事实任一异常都必须失败关闭，禁止回退 Token claim 或默认角色权限。
    except ValueError as exc:
        # 重复或目录外 code 不能在计算哈希前静默去重，否则损坏关系会伪装成合法矩阵。
        raise ApiError("rbac.matrix_integrity_failed") from exc


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


def _tenant_by_slug(connection: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    """在 控制面认证与会话 内部复用 tenant by slug 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return connection.execute(
        "SELECT tenant_id, slug, name, status FROM tenants WHERE slug=?",
        (slug,),
    ).fetchone()


def _user_by_email(
    connection: sqlite3.Connection,
    tenant_id: str,
    email: str,
) -> sqlite3.Row | None:
    """在控制面认证与会话内部按租户查询邮箱。

    该辅助逻辑集中处理边界值和错误映射，不得绕过租户隔离、统一错误码或脱敏约束。
    """

    return connection.execute(
        """
        SELECT
          user_id, tenant_id, email, display_name, password_hash, role, status,
          failed_login_count, locked_until
        FROM users
        WHERE tenant_id=? AND email=?
        """,
        (tenant_id, email),
    ).fetchone()


def _record_failed_login(
    connection: sqlite3.Connection,
    user: sqlite3.Row,
    config: AppConfig,
    now: datetime,
) -> None:
    """在 控制面认证与会话 内部复用 record failed login 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    failed_count = int(user["failed_login_count"] or 0) + 1
    locked_until = None
    # 该分支影响安全或治理链路，需要用中文说明处理边界。
    if failed_count >= config.login_max_failures:
        locked_until = _to_db_time(now + timedelta(seconds=config.login_lock_seconds))
    connection.execute(
        """
        UPDATE users
        SET failed_login_count=?, locked_until=?, updated_at=datetime('now')
        WHERE user_id=?
        """,
        (failed_count, locked_until, user["user_id"]),
    )
    connection.commit()


def _record_successful_login(
    connection: sqlite3.Connection,
    user: sqlite3.Row,
    now: datetime,
) -> sqlite3.Row:
    """在 控制面认证与会话 内部复用 record successful login 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    connection.execute(
        """
        UPDATE users
        SET failed_login_count=0, locked_until=NULL, last_login_at=?, updated_at=datetime('now')
        WHERE user_id=?
        """,
        (_to_db_time(now), user["user_id"]),
    )
    connection.commit()
    refreshed = connection.execute(
        """
        SELECT
          user_id, tenant_id, email, display_name, password_hash, role, status,
          failed_login_count, locked_until
        FROM users
        WHERE user_id=?
        """,
        (user["user_id"],),
    ).fetchone()
    if not refreshed:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("auth.login_invalid")
    return refreshed


def _auth_user(row: sqlite3.Row, snapshot: PermissionSnapshot) -> AuthUser:
    """在 控制面认证与会话 内部复用 auth user 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    # 关键控制流影响权限、审计或错误归因，必须保留统一错误语义。
    return AuthUser(
        user_id=row["user_id"],
        tenant_id=row["tenant_id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        permission_codes=snapshot.permission_codes,
        permission_revision=snapshot.permission_revision,
        authorization_fingerprint=snapshot.authorization_fingerprint,
    )


def _extract_bearer(authorization: str | None) -> str:
    """在 控制面认证与会话 内部复用 extract bearer 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    # 认证请求是访问控制边界，调用方必须透传统一错误和 request_id。
    if not authorization:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("auth.token_missing")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
        raise ApiError("auth.token_invalid")
    return token.strip()


def _verify_password(password: str, password_hash: str) -> bool:
    """在 控制面认证与会话 内部复用 verify password 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    try:
        return verify_password(password, password_hash)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except (ValueError, TypeError):
        return False


def _fake_password_check(password: str) -> None:
    """在 控制面认证与会话 内部复用 fake password check 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    _verify_password(password, _DUMMY_PASSWORD_HASH)


def _parse_datetime(value: object) -> datetime | None:
    """解析 控制面认证与会话 的 parse datetime 数据，把数据库或外部输入转换为内部稳定类型。

    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """

    if not value:
        return None
    raw = str(value)
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def _safe_user_agent(user_agent: str | None) -> str | None:
    """生成 控制面认证与会话 可安全展示或落日志的 safe user agent 内容。

    安全：输出只能包含脱敏摘要，不能泄露明文密钥、Token、Prompt 或完整凭据。
    """

    if not user_agent:
        return None
    return user_agent[:240]


def _safe_client_host(client_host: str | None) -> str | None:
    """生成 控制面认证与会话 可安全展示或落日志的 safe client host 内容。

    安全：输出只能包含脱敏摘要，不能泄露明文密钥、Token、Prompt 或完整凭据。
    """

    if not client_host:
        return None
    return client_host[:80]


def _log_login_failed(
    request_id: str,
    trace_id: str,
    tenant_id: str | None,
    actor_id: str | None,
    reason: str,
) -> None:
    """记录不含登录输入正文的失败审计。

    已知账号使用租户内 ``actor_id`` 关联，未知账号保持为空；无论哪种失败都不得复制邮箱、密码或
    其他攻击者可控登录字段，避免认证探测把个人信息写入集中日志。
    """

    logger.warning(
        "auth.login_failed",
        extra={
            "event": "auth.login_failed",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": tenant_id or "",
            "actor_id": actor_id or "",
            "payload": {"reason": reason},
        },
    )
