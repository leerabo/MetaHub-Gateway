"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/tenants/service.py
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from metahub_control.auth import AuthContext
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .models import TenantCurrentResponse, TenantInfo, TenantUpdateRequest

logger = logging.getLogger("metahub.tenants")


def get_current_tenant(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> TenantCurrentResponse:
    """读取当前登录用户所属租户。

    租户 ID 只来自认证上下文，不能由请求参数或请求体提供，这是多租户隔离的第一条边界。
    """

    tenant = _load_current_tenant(_connect(config), context.user.tenant_id)
    # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
    return TenantCurrentResponse(tenant=tenant, request_id=request_id, trace_id=trace_id)


def update_current_tenant(
    config: AppConfig,
    context: AuthContext,
    payload: TenantUpdateRequest,
    *,
    request_id: str,
    trace_id: str,
) -> TenantCurrentResponse:
    """更新当前租户的可编辑字段。

    PATCH /tenant/current 永远只更新 AuthContext.user.tenant_id 对应的行，普通接口没有跨租户目标选择能力。
    """

    updates: dict[str, str] = {}
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
            raise ApiError("tenant.update_invalid")
        updates["name"] = name
    if payload.service_mode is not None:
        updates["service_mode"] = payload.service_mode
    if not updates:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("tenant.update_empty")

    connection = _connect(config)
    before = _load_current_tenant(connection, context.user.tenant_id)
    assignments = ", ".join(f"{field}=?" for field in updates)
    values = [*updates.values(), _to_db_time(_utcnow()), context.user.tenant_id]
    try:
        connection.execute(
            f"UPDATE tenants SET {assignments}, updated_at=? WHERE tenant_id=?",
            values,
        )
        connection.commit()
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except sqlite3.Error as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(
            "tenant.update_invalid",
            message="Tenant update failed.",
            suggestion="Check tenant fields and retry.",
        ) from exc

    after = _load_current_tenant(connection, context.user.tenant_id)
    logger.info(
        "tenant.updated",
        extra={
            "event": "tenant.updated",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": context.user.tenant_id,
            "payload": {
                "changed_fields": sorted(updates),
                "before": {field: getattr(before, field) for field in updates},
                "after": {field: getattr(after, field) for field in updates},
            },
        },
    )
    # 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
    return TenantCurrentResponse(tenant=after, request_id=request_id, trace_id=trace_id)


def _load_current_tenant(connection: sqlite3.Connection, tenant_id: str) -> TenantInfo:
    """按当前租户加载 控制面租户资料 的 load current tenant 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    row = connection.execute(
        """
        SELECT tenant_id, slug, name, service_mode, status, created_at, updated_at
        FROM tenants
        WHERE tenant_id=?
        """,
        (tenant_id,),
    ).fetchone()
    if not row or row["status"] != "active":
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("tenant.current_not_found")
    return TenantInfo(
        tenant_id=row["tenant_id"],
        slug=row["slug"],
        name=row["name"],
        service_mode=row["service_mode"],
        status=row["status"],
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 控制面租户资料 所需的数据库连接，并把基础设施异常映射到统一错误模型。
    
    边界：调用方只看到业务错误码，不直接暴露数据库路径、SQL 或连接细节。
    """


    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _parse_db_time(value: object) -> datetime:
    """解析 控制面租户资料 的 parse db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    raw = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 控制面租户资料 的 UTC 时间序列化为数据库 ISO 字符串。
    
    边界：统一时间口径可避免分页、审计和测试在时区上产生歧义。
    """


    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回 控制面租户资料 使用的当前 UTC 时间，去掉微秒以保持数据库字段稳定。
    
    边界：所有状态更新时间共用该口径，避免同一事务内出现混合时间格式。
    """


    return datetime.now(UTC).replace(microsecond=0)
