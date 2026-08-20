"""模块职责：实现租户级 Environment 管理、关联统计和统一环境绑定解析。

维护边界：所有查询必须同时使用 tenant_id；环境事件和日志只保存安全元数据，不得包含 Key、Token、Prompt 或响应正文。
来源文件：control-plane-api/src/metahub_control/environments/service.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from metahub_control.auth import AuthContext
from metahub_control.webhooks.service import dispatch_webhook_event
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from .models import (
    CreateEnvironmentRequest,
    EnvironmentInfo,
    EnvironmentListResponse,
    EnvironmentResponse,
    UpdateEnvironmentRequest,
)

logger = logging.getLogger("metahub.control.environments")

# 标准环境代码是 M0.5 的产品边界；地域、集群和自定义环境留到后续里程碑。
VALID_ENVIRONMENT_CODES = {"dev", "test", "prod"}
VALID_ENVIRONMENT_STATUSES = {"active", "disabled"}
MAX_ENVIRONMENT_NAME_LENGTH = 80
MAX_ENVIRONMENT_DESCRIPTION_LENGTH = 240
ENVIRONMENT_EDITABLE_FIELDS = {"name", "description", "status"}


@dataclass(frozen=True)
class EnvironmentBinding:
    """跨模块传递的可信 Environment 绑定结果。

    该对象只能由 tenant_id 约束下的数据库查询生成，调用方不能根据请求体自行构造后跳过租户校验。
    """

    environment_id: str
    tenant_id: str
    code: str
    name: str
    status: str


def list_environments(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    code: str | None,
    request_id: str,
    trace_id: str,
) -> EnvironmentListResponse:
    """列出当前租户 Environment，并返回 Key、别名和预算关联数量。"""

    normalized_status = _normalize_query_status(status)
    normalized_code = _normalize_optional_code(code)
    clauses = ["e.tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    if normalized_status != "all":
        clauses.append("e.status=?")
        params.append(normalized_status)
    if normalized_code:
        clauses.append("e.code=?")
        params.append(normalized_code)
    connection = _connect(config)
    try:
        rows = connection.execute(
        f"""
        SELECT
          e.environment_id, e.tenant_id, e.code, e.name, e.description, e.status,
          e.created_at, e.updated_at,
          COUNT(DISTINCT vk.virtual_key_id) AS virtual_key_count,
          COUNT(DISTINCT ma.model_alias_id) AS model_alias_count,
          COUNT(DISTINCT br.budget_rule_id) AS budget_rule_count
        FROM environments e
        LEFT JOIN virtual_keys vk
          ON vk.tenant_id=e.tenant_id AND vk.environment_id=e.environment_id
        LEFT JOIN model_aliases ma
          ON ma.tenant_id=e.tenant_id AND ma.environment_id=e.environment_id
        LEFT JOIN budget_rules br
          ON br.tenant_id=e.tenant_id AND br.environment_id=e.environment_id
        WHERE {' AND '.join(clauses)}
        GROUP BY e.environment_id
        ORDER BY CASE e.code WHEN 'dev' THEN 0 WHEN 'test' THEN 1 ELSE 2 END
        """,
        params,
        ).fetchall()
        return EnvironmentListResponse(
            environments=[_environment_info(row) for row in rows],
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def create_environment(
    config: AppConfig,
    context: AuthContext,
    payload: CreateEnvironmentRequest,
    *,
    request_id: str,
    trace_id: str,
) -> EnvironmentResponse:
    """创建当前租户 Environment，并记录 environment.created 事件。

    主数据和事件先在同一事务提交；Webhook 是外部副作用，失败时只记录安全错误，不回滚已经生效的环境配置。
    """

    code = _normalize_code(payload.code)
    name = _normalize_name(payload.name)
    description = _normalize_description(payload.description)
    connection = _connect(config)
    environment_id = f"env_{uuid4().hex}"
    now = _to_db_time(_utcnow())
    event_payload = {"code": code, "name": name, "status": "active"}
    try:
        connection.execute(
            """
            INSERT INTO environments(
              environment_id, tenant_id, code, name, description, status,
              created_by, updated_by, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                environment_id,
                context.user.tenant_id,
                code,
                name,
                description,
                context.user.user_id,
                context.user.user_id,
                now,
                now,
            ),
        )
        _write_environment_event(
            connection,
            context,
            environment_id=environment_id,
            event_type="environment.created",
            request_id=request_id,
            trace_id=trace_id,
            payload=event_payload,
        )
        connection.commit()
    # 数据库唯一约束异常必须转换成稳定业务错误，禁止透出底层 SQL 文本。
    except sqlite3.IntegrityError as exc:
        # 唯一约束是重复环境代码的最终防线，原始 SQLite 文本不能穿透到 API。
        connection.rollback()
        raise ApiError("environment.code_conflict") from exc

    environment = _load_environment(connection, context.user.tenant_id, environment_id)
    _log_environment_event(context, "environment.created", environment_id, request_id, trace_id, event_payload)
    _dispatch_created_webhook(config, context, environment, request_id, trace_id)
    return EnvironmentResponse(environment=environment, request_id=request_id, trace_id=trace_id)


def update_environment(
    config: AppConfig,
    context: AuthContext,
    environment_id: str,
    payload: UpdateEnvironmentRequest,
    *,
    request_id: str,
    trace_id: str,
) -> EnvironmentResponse:
    """更新 Environment 名称、说明或状态，不允许修改稳定代码。"""

    supplied = _provided_fields(payload)
    if not supplied:
        # 空 PATCH 会制造虚假的配置变更反馈和无意义审计记录，必须拒绝。
        raise ApiError("environment.update_empty")
    connection = _connect(config)
    before = _load_environment(connection, context.user.tenant_id, environment_id)
    updates: list[str] = []
    values: list[object | None] = []
    if "name" in supplied:
        updates.append("name=?")
        values.append(_normalize_name(payload.name))
    if "description" in supplied:
        updates.append("description=?")
        values.append(_normalize_description(payload.description))
    if "status" in supplied:
        updates.append("status=?")
        values.append(_normalize_status(payload.status))
    now = _to_db_time(_utcnow())
    updates.extend(["updated_by=?", "updated_at=?"])
    values.extend([context.user.user_id, now, context.user.tenant_id, environment_id])
    connection.execute(
        f"UPDATE environments SET {', '.join(updates)} WHERE tenant_id=? AND environment_id=?",
        values,
    )
    after = _load_environment(connection, context.user.tenant_id, environment_id)
    event_payload = {
        "code": after.code,
        "changed_fields": sorted(supplied),
        "before_status": before.status,
        "after_status": after.status,
    }
    _write_environment_event(
        connection,
        context,
        environment_id=environment_id,
        event_type="environment.updated",
        request_id=request_id,
        trace_id=trace_id,
        payload=event_payload,
    )
    connection.commit()
    _log_environment_event(context, "environment.updated", environment_id, request_id, trace_id, event_payload)
    return EnvironmentResponse(environment=after, request_id=request_id, trace_id=trace_id)


def resolve_environment_binding(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    environment_id: object | None,
    environment_code: object | None,
    require_active: bool = True,
    allow_default: bool = True,
    invalid_code_error: str = "environment.code_invalid",
    not_found_error: str = "environment.not_found",
    mismatch_error: str = "environment.binding_mismatch",
    disabled_error: str = "environment.disabled",
) -> EnvironmentBinding:
    """在可信租户边界内解析 Environment ID 和兼容代码。

    新客户端优先传 environment_id，旧客户端可传 code；两者同时存在时必须一致。查询始终带 tenant_id，跨租户 ID 对外表现为当前租户内不存在。
    """

    normalized_id = str(environment_id or "").strip() or None
    normalized_code = str(environment_code or "").strip().lower() or None
    if normalized_code and normalized_code not in VALID_ENVIRONMENT_CODES:
        # 非标准代码会扩大到自定义环境或地域语义，M0.5 必须直接拒绝。
        raise ApiError(invalid_code_error)
    if not normalized_id and not normalized_code:
        if not allow_default:
            # 禁止默认环境的调用方必须显式给出绑定，不能静默落到开发环境。
            raise ApiError(not_found_error)
        normalized_code = "dev"
    if normalized_id:
        row = connection.execute(
            """
            SELECT environment_id, tenant_id, code, name, status
            FROM environments
            WHERE tenant_id=? AND environment_id=?
            """,
            (tenant_id, normalized_id),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT environment_id, tenant_id, code, name, status
            FROM environments
            WHERE tenant_id=? AND code=?
            """,
            (tenant_id, normalized_code),
        ).fetchone()
    if not row:
        # 不区分“其他租户存在”和“完全不存在”，避免 environment_id 成为跨租户探测入口。
        raise ApiError(not_found_error)
    if normalized_code and row["code"] != normalized_code:
        # ID 与兼容代码冲突说明客户端状态陈旧或存在环境覆盖尝试，不能静默选一边。
        raise ApiError(mismatch_error)
    if require_active and row["status"] != "active":
        # 禁用环境是后端和数据面共同执行的治理边界，不能只依赖前端隐藏选项。
        raise ApiError(disabled_error)
    return EnvironmentBinding(
        environment_id=row["environment_id"],
        tenant_id=row["tenant_id"],
        code=row["code"],
        name=row["name"],
        status=row["status"],
    )


def _load_environment(connection: sqlite3.Connection, tenant_id: str, environment_id: str) -> EnvironmentInfo:
    """按租户和环境稳定标识加载单个 Environment 及全部资源关联数量。"""

    row = connection.execute(
        """
        SELECT
          e.environment_id, e.tenant_id, e.code, e.name, e.description, e.status,
          e.created_at, e.updated_at,
          COUNT(DISTINCT vk.virtual_key_id) AS virtual_key_count,
          COUNT(DISTINCT ma.model_alias_id) AS model_alias_count,
          COUNT(DISTINCT br.budget_rule_id) AS budget_rule_count
        FROM environments e
        LEFT JOIN virtual_keys vk
          ON vk.tenant_id=e.tenant_id AND vk.environment_id=e.environment_id
        LEFT JOIN model_aliases ma
          ON ma.tenant_id=e.tenant_id AND ma.environment_id=e.environment_id
        LEFT JOIN budget_rules br
          ON br.tenant_id=e.tenant_id AND br.environment_id=e.environment_id
        WHERE e.tenant_id=? AND e.environment_id=?
        GROUP BY e.environment_id
        """,
        (tenant_id, environment_id),
    ).fetchone()
    if not row:
        # 当前租户内找不到资源时返回统一 404，不泄露其他租户 Environment。
        raise ApiError("environment.not_found")
    return _environment_info(row)


def _environment_info(row: sqlite3.Row) -> EnvironmentInfo:
    """把 Environment 查询行转换为稳定的安全 DTO。"""

    return EnvironmentInfo(
        environment_id=row["environment_id"],
        tenant_id=row["tenant_id"],
        code=row["code"],
        name=row["name"],
        description=row["description"],
        status=row["status"],
        virtual_key_count=int(row["virtual_key_count"] or 0),
        model_alias_count=int(row["model_alias_count"] or 0),
        budget_rule_count=int(row["budget_rule_count"] or 0),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _write_environment_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    environment_id: str,
    event_type: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
) -> None:
    """在当前主事务中写入 Environment 事件安全摘要。"""

    connection.execute(
        """
        INSERT INTO environment_events(
          environment_event_id, tenant_id, environment_id, event_type,
          actor_user_id, request_id, trace_id, payload_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"enve_{uuid4().hex}",
            context.user.tenant_id,
            environment_id,
            event_type,
            context.user.user_id,
            request_id,
            trace_id,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            _to_db_time(_utcnow()),
        ),
    )


def _dispatch_created_webhook(
    config: AppConfig,
    context: AuthContext,
    environment: EnvironmentInfo,
    request_id: str,
    trace_id: str,
) -> None:
    """在主事务提交后尽力投递 environment.created Webhook。

    外部 Endpoint 不可用不能回滚 Environment；Webhook 服务会记录 delivery/attempt，本函数只补充安全结构化错误。
    """

    event = build_webhook_event(
        tenant_id=context.user.tenant_id,
        event_type="environment.created",
        actor_id=context.user.user_id,
        object_id=environment.environment_id,
        trace_id=trace_id,
        payload={"code": environment.code, "name": environment.name, "status": environment.status},
    )
    try:
        dispatch_webhook_event(
            config,
            context.user.tenant_id,
            event,
            request_id=request_id,
            trace_id=trace_id,
        )
    except Exception:
        # Webhook 异常只记录资源 ID 和链路 ID，禁止记录签名、Endpoint 密钥或响应正文。
        logger.exception(
            "environment.webhook_dispatch_failed",
            extra={
                "event": "environment.webhook_dispatch_failed",
                "tenant_id": context.user.tenant_id,
                "object_id": environment.environment_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _normalize_code(value: object | None) -> str:
    """规范化必填标准环境代码，并限制为 MVP 支持的开发、测试和生产环境。"""

    code = str(value or "").strip().lower()
    if code not in VALID_ENVIRONMENT_CODES:
        # 非标准环境代码会突破 MVP 产品边界，必须返回统一参数错误。
        raise ApiError("environment.code_invalid")
    return code


def _normalize_optional_code(value: object | None) -> str | None:
    """规范化列表可选 code；空值表示不过滤。"""

    if value is None or not str(value).strip():
        return None
    return _normalize_code(value)


def _normalize_name(value: object | None) -> str:
    """规范化 Environment 展示名称并限制长度。"""

    name = str(value or "").strip()
    if not name or len(name) > MAX_ENVIRONMENT_NAME_LENGTH:
        # 空名称或超长名称会破坏控制台展示，必须在写库之前统一拒绝。
        raise ApiError("environment.name_invalid")
    return name


def _normalize_description(value: object | None) -> str | None:
    """规范化可选说明，空字符串转为 null。"""

    if value is None:
        return None
    description = str(value).strip()
    if len(description) > MAX_ENVIRONMENT_DESCRIPTION_LENGTH:
        # 超长说明没有治理价值且可能挤压页面布局，写库前必须限制。
        raise ApiError("environment.description_invalid")
    return description or None


def _normalize_status(value: object | None) -> str:
    """规范化 Environment 状态枚举，只允许启用或禁用两种治理状态。"""

    status = str(value or "").strip().lower()
    if status not in VALID_ENVIRONMENT_STATUSES:
        # 未知状态无法由数据面正确执行，必须返回稳定状态参数错误。
        raise ApiError("environment.status_invalid")
    return status


def _normalize_query_status(value: object | None) -> str:
    """规范化列表状态筛选，默认只返回 active。"""

    status = str(value or "active").strip().lower()
    if status == "all":
        return status
    return _normalize_status(status)


def _provided_fields(payload: UpdateEnvironmentRequest) -> set[str]:
    """提取 PATCH 实际提供的可编辑字段，区分缺失和显式 null。"""

    return set(payload.model_fields_set) & ENVIRONMENT_EDITABLE_FIELDS


def _log_environment_event(
    context: AuthContext,
    event: str,
    environment_id: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
) -> None:
    """写入不含敏感信息的 Environment 结构化审计日志。"""

    logger.info(
        event,
        extra={
            "event": event,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": environment_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "payload": payload,
        },
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建控制面数据库连接并映射基础设施错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库连接错误必须收敛成统一 API 错误，避免泄露连接地址和路径。
    except DbError as exc:
        # API 只暴露统一错误码和安全建议，不返回数据库路径或 SQL。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _parse_db_time(value: object) -> datetime:
    """解析数据库中的 UTC 时间文本，转换为 API 所需的带时区时间对象。"""

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _to_db_time(value: datetime) -> str:
    """把时间对象序列化为稳定 UTC ISO 字符串，供环境审计字段持久化。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回去除微秒的当前 UTC 时间，保持测试和数据库字段稳定。"""

    return datetime.now(UTC).replace(microsecond=0)
