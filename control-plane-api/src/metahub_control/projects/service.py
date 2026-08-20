"""模块职责：实现 Project/Application 管理、事件记录和租户内归属校验。

维护边界：本服务只信任认证上下文里的 tenant_id 和角色依赖，不接受客户端伪造租户；事件和日志不得包含明文 Key、Provider Key、Prompt 或响应正文。
来源文件：control-plane-api/src/metahub_control/projects/service.py
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import (
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext
from metahub_control.virtual_keys.service import load_virtual_key_audit_snapshot

from .models import (
    ApplicationInfo,
    ApplicationListResponse,
    ApplicationResponse,
    CreateApplicationRequest,
    CreateProjectRequest,
    ProjectInfo,
    ProjectListResponse,
    ProjectResponse,
    UpdateApplicationRequest,
    UpdateProjectRequest,
)

logger = logging.getLogger("metahub.control.projects")

# 项目和应用编码进入 API、控制台和对账视图，必须保持短小、稳定且可读。
CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
VALID_STATUSES = {"active", "disabled", "archived"}
MAX_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 500
MAX_SEARCH_LENGTH = 160
PROJECT_EDITABLE_FIELDS = {"name", "description", "owner_user_id", "status"}
APPLICATION_EDITABLE_FIELDS = {"project_id", "name", "description", "status"}


def list_projects(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    search: str | None,
    request_id: str,
    trace_id: str,
) -> ProjectListResponse:
    """列出当前租户内的项目，并附带应用数和 Key 绑定数。

    查询条件始终从 context.user.tenant_id 开始，避免管理员或开发者通过猜测 ID 读取其他租户项目。
    """

    normalized_status = _normalize_query_status(status)
    normalized_search = _normalize_optional_search(search)
    clauses = ["p.tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    if normalized_status != "all":
        clauses.append("p.status=?")
        params.append(normalized_status)
    if normalized_search:
        clauses.append("(p.project_code LIKE ? OR p.name LIKE ?)")
        like_value = f"%{normalized_search}%"
        params.extend([like_value, like_value])
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            SELECT
              p.project_id, p.tenant_id, p.project_code, p.name, p.description,
              p.owner_user_id, u.display_name AS owner_display_name, p.status,
              p.created_at, p.updated_at,
              COUNT(DISTINCT a.application_id) AS application_count,
              COUNT(DISTINCT vk.virtual_key_id) AS virtual_key_count
            FROM projects p
            LEFT JOIN users u ON u.tenant_id=p.tenant_id AND u.user_id=p.owner_user_id
            LEFT JOIN applications a ON a.tenant_id=p.tenant_id AND a.project_id=p.project_id
            LEFT JOIN virtual_keys vk ON vk.tenant_id=p.tenant_id AND vk.project_id=p.project_id
            WHERE {' AND '.join(clauses)}
            GROUP BY p.project_id
            ORDER BY p.updated_at DESC, p.project_code ASC
            """,
            params,
        ).fetchall()
        return ProjectListResponse(
            projects=[_project_info(row) for row in rows],
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def create_project(
    config: AppConfig,
    context: AuthContext,
    payload: CreateProjectRequest,
    *,
    request_id: str,
    trace_id: str,
) -> ProjectResponse:
    """创建租户内项目，并记录 project.created 事件。

    项目编码是租户内对账标识，重复时返回稳定冲突错误码，不透出底层唯一约束名称。
    """

    project_code = _normalize_code(payload.project_code, "project.code_invalid")
    name = _normalize_name(payload.name, "project.name_invalid")
    description = _normalize_description(payload.description)
    connection = _connect(config)
    owner_user_id = _normalize_optional_owner(connection, context.user.tenant_id, payload.owner_user_id)
    now = _to_db_time(_utcnow())
    project_id = f"prj_{uuid4().hex}"
    try:
        connection.execute(
            """
            INSERT INTO projects(
              project_id, tenant_id, project_code, name, description, owner_user_id,
              status, created_by, updated_by, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                project_id,
                context.user.tenant_id,
                project_code,
                name,
                description,
                owner_user_id,
                context.user.user_id,
                context.user.user_id,
                now,
                now,
            ),
        )
        _write_project_event(
            connection,
            context,
            project_id=project_id,
            application_id=None,
            event_type="project.created",
            request_id=request_id,
            trace_id=trace_id,
            payload={"project_code": project_code, "name": name, "status": "active"},
        )
        connection.commit()
    # SQLite 唯一约束是项目编码冲突的最终防线，异常分支必须回滚事务。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 编码唯一约束冲突需要映射成 409，避免泄露 SQLite 原始错误文本。
        raise ApiError("project.code_conflict") from exc
    project = _load_project(connection, context.user.tenant_id, project_id)
    _log_project_event(context, "project.created", project_id, request_id, trace_id, {"project_code": project_code})
    return ProjectResponse(project=project, request_id=request_id, trace_id=trace_id)


def update_project(
    config: AppConfig,
    context: AuthContext,
    project_id: str,
    payload: UpdateProjectRequest,
    *,
    request_id: str,
    trace_id: str,
) -> ProjectResponse:
    """更新项目名称、说明、负责人或状态。

    空 PATCH 没有治理含义，必须拒绝，避免控制台误判项目已经被保存。
    """

    provided = _provided_fields(payload, PROJECT_EDITABLE_FIELDS)
    if not provided:
        # 空更新会让审计和用户反馈都失去意义，因此统一拒绝。
        raise ApiError("project.update_empty")
    connection = _connect(config)
    _load_project(connection, context.user.tenant_id, project_id)
    updates: list[str] = []
    values: list[object | None] = []
    if "name" in provided:
        updates.append("name=?")
        values.append(_normalize_name(payload.name, "project.name_invalid"))
    if "description" in provided:
        updates.append("description=?")
        values.append(_normalize_description(payload.description))
    if "owner_user_id" in provided:
        updates.append("owner_user_id=?")
        values.append(_normalize_optional_owner(connection, context.user.tenant_id, payload.owner_user_id))
    if "status" in provided:
        updates.append("status=?")
        values.append(_normalize_status(payload.status, "project.status_invalid"))
    updates.extend(["updated_by=?", "updated_at=?"])
    values.extend([context.user.user_id, _to_db_time(_utcnow()), context.user.tenant_id, project_id])
    connection.execute(
        f"UPDATE projects SET {', '.join(updates)} WHERE tenant_id=? AND project_id=?",
        values,
    )
    connection.commit()
    return ProjectResponse(
        project=_load_project(connection, context.user.tenant_id, project_id),
        request_id=request_id,
        trace_id=trace_id,
    )


def list_applications(
    config: AppConfig,
    context: AuthContext,
    *,
    project_id: str | None,
    status: str | None,
    search: str | None,
    request_id: str,
    trace_id: str,
) -> ApplicationListResponse:
    """列出当前租户内应用，支持按项目和状态过滤。

    如果传入 project_id，先校验项目属于当前租户，避免应用列表成为跨租户项目 ID 探测入口。
    """

    normalized_project_id = _normalize_optional_text(project_id)
    normalized_status = _normalize_query_status(status)
    normalized_search = _normalize_optional_search(search)
    connection = _connect(config)
    try:
        clauses = ["a.tenant_id=?"]
        params: list[object] = [context.user.tenant_id]
        if normalized_project_id:
            _load_project(connection, context.user.tenant_id, normalized_project_id)
            clauses.append("a.project_id=?")
            params.append(normalized_project_id)
        if normalized_status != "all":
            clauses.append("a.status=?")
            params.append(normalized_status)
        if normalized_search:
            clauses.append("(a.application_code LIKE ? OR a.name LIKE ?)")
            like_value = f"%{normalized_search}%"
            params.extend([like_value, like_value])
        rows = connection.execute(
            f"""
            SELECT
              a.application_id, a.tenant_id, a.project_id, p.project_code,
              p.name AS project_name, a.application_code, a.name, a.description,
              a.status, a.created_at, a.updated_at,
              COUNT(DISTINCT vk.virtual_key_id) AS virtual_key_count
            FROM applications a
            JOIN projects p ON p.tenant_id=a.tenant_id AND p.project_id=a.project_id
            LEFT JOIN virtual_keys vk
              ON vk.tenant_id=a.tenant_id AND vk.application_id=a.application_id
            WHERE {' AND '.join(clauses)}
            GROUP BY a.application_id
            ORDER BY a.updated_at DESC, a.application_code ASC
            """,
            params,
        ).fetchall()
        return ApplicationListResponse(
            applications=[_application_info(row) for row in rows],
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def create_application(
    config: AppConfig,
    context: AuthContext,
    payload: CreateApplicationRequest,
    *,
    request_id: str,
    trace_id: str,
) -> ApplicationResponse:
    """在项目下创建应用，并记录 application.created 事件。

    应用编码只要求在同一项目内唯一，不会污染其他项目命名空间。
    """

    project_id = _normalize_required_text(payload.project_id, "project.not_found")
    application_code = _normalize_code(payload.application_code, "application.code_invalid")
    name = _normalize_name(payload.name, "application.name_invalid")
    description = _normalize_description(payload.description)
    connection = _connect(config)
    project = _load_project(connection, context.user.tenant_id, project_id)
    _ensure_writable_parent_status(project.status)
    now = _to_db_time(_utcnow())
    application_id = f"app_{uuid4().hex}"
    try:
        connection.execute(
            """
            INSERT INTO applications(
              application_id, tenant_id, project_id, application_code, name, description,
              status, created_by, updated_by, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                application_id,
                context.user.tenant_id,
                project_id,
                application_code,
                name,
                description,
                context.user.user_id,
                context.user.user_id,
                now,
                now,
            ),
        )
        _write_project_event(
            connection,
            context,
            project_id=project_id,
            application_id=application_id,
            event_type="application.created",
            request_id=request_id,
            trace_id=trace_id,
            payload={"application_code": application_code, "name": name, "project_id": project_id},
        )
        connection.commit()
    # 应用编码唯一约束由数据库兜底，异常分支需要回滚并转换成业务错误码。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 同一项目下应用编码重复时返回应用域冲突错误，便于控制台定位字段。
        raise ApiError("application.code_conflict") from exc
    application = _load_application(connection, context.user.tenant_id, application_id)
    _log_project_event(
        context,
        "application.created",
        application_id,
        request_id,
        trace_id,
        {"application_code": application_code, "project_id": project_id},
    )
    return ApplicationResponse(application=application, request_id=request_id, trace_id=trace_id)


def update_application(
    config: AppConfig,
    context: AuthContext,
    application_id: str,
    payload: UpdateApplicationRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
) -> ApplicationResponse:
    """更新应用元数据，必要时同步迁移已绑定 Key 的项目归属。

    应用迁移项目会影响后续请求归因，必须在同一事务中修正 Virtual Key.project_id。
    """

    provided = _provided_fields(payload, APPLICATION_EDITABLE_FIELDS)
    if not provided:
        # 空 PATCH 不会改变应用归属或治理状态，必须返回明确错误让控制台停止误报保存成功。
        raise ApiError("application.update_empty")
    connection = _connect(config)
    audit_records = []
    try:
        # SQLite 没有 SELECT FOR UPDATE；BEGIN IMMEDIATE 在读取 Application 和 Key 快照前取得写锁，
        # 防止并发改绑在“选中 Key 集合”和批量更新之间插入，保证每把受影响 Key 恰好审计一次。
        connection.execute("BEGIN IMMEDIATE")
        current = _load_application(connection, context.user.tenant_id, application_id)
        updates: list[str] = []
        values: list[object | None] = []
        next_project_id = current.project_id
        if "project_id" in provided:
            next_project_id = _normalize_required_text(payload.project_id, "project.not_found")
            project = _load_project(connection, context.user.tenant_id, next_project_id)
            _ensure_writable_parent_status(project.status)
            updates.append("project_id=?")
            values.append(next_project_id)
        if "name" in provided:
            updates.append("name=?")
            values.append(_normalize_name(payload.name, "application.name_invalid"))
        if "description" in provided:
            updates.append("description=?")
            values.append(_normalize_description(payload.description))
        if "status" in provided:
            updates.append("status=?")
            values.append(_normalize_status(payload.status, "application.status_invalid"))

        project_changed = next_project_id != current.project_id
        key_snapshots = (
            _load_application_virtual_key_audit_snapshots(
                connection,
                context.user.tenant_id,
                application_id,
            )
            if project_changed
            else []
        )
        now = _to_db_time(_utcnow())
        updates.extend(["updated_by=?", "updated_at=?"])
        values.extend([context.user.user_id, now, context.user.tenant_id, application_id])
        connection.execute(
            f"UPDATE applications SET {', '.join(updates)} WHERE tenant_id=? AND application_id=?",
            values,
        )
        if project_changed:
            # Key 归属更新必须继续携带认证租户和已锁定 Application，
            # 不能仅凭客户端提供的 ID 批量写入。
            connection.execute(
                """
                UPDATE virtual_keys
                SET project_id=?, updated_at=?
                WHERE tenant_id=? AND application_id=?
                """,
                (next_project_id, now, context.user.tenant_id, application_id),
            )
            for virtual_key_id, object_name, before_summary in key_snapshots:
                after_object_name, after_summary = load_virtual_key_audit_snapshot(
                    connection,
                    context.user.tenant_id,
                    virtual_key_id,
                )
                audit_record = record_audit_log(
                    connection,
                    context,
                    action="virtual_key.updated",
                    object_type="virtual_key",
                    object_id=virtual_key_id,
                    object_name=after_object_name or object_name,
                    before_summary=before_summary,
                    after_summary=after_summary,
                    request_meta=_audit_request_meta(audit_request_meta),
                    request_id=request_id,
                    trace_id=trace_id,
                )
                if audit_record is None:
                    # 已锁定快照中的 project_id 必然变化；缺失审计意味着合同失配，
                    # 不能提交无证据迁移。
                    raise ApiError("audit_log.record_unavailable")
                audit_records.append(audit_record)
            commit_audited_transaction(connection)
        else:
            connection.commit()
        application = _load_application(connection, context.user.tenant_id, application_id)
    except Exception:
        # Application、全部 Key 与逐 Key 审计是一个治理事实，任一审计失败都必须整体回滚。
        connection.rollback()
        raise

    # 外部 Webhook 投递只能发生在数据库提交之后；单条投递失败由审计模块隔离，不反向撤销已提交事实。
    for audit_record in audit_records:
        dispatch_audit_recorded(
            config,
            context,
            audit_record,
            request_id=request_id,
            trace_id=trace_id,
        )
    return ApplicationResponse(
        application=application,
        request_id=request_id,
        trace_id=trace_id,
    )


def _load_application_virtual_key_audit_snapshots(
    connection: sqlite3.Connection,
    tenant_id: str,
    application_id: str,
) -> list[tuple[str, str, dict[str, object]]]:
    """读取已锁定 Application 下全部 Key 的迁移前安全摘要。

    调用方必须先开启写事务；查询同时约束 tenant_id 与 application_id，返回值不含明文 Key、
    完整哈希或描述正文，并按稳定 ID 排序以便故障定位和测试复核。
    """

    rows = connection.execute(
        """
        SELECT virtual_key_id
        FROM virtual_keys
        WHERE tenant_id=? AND application_id=?
        ORDER BY virtual_key_id ASC
        """,
        (tenant_id, application_id),
    ).fetchall()
    snapshots: list[tuple[str, str, dict[str, object]]] = []
    for row in rows:
        virtual_key_id = str(row["virtual_key_id"])
        object_name, summary = load_virtual_key_audit_snapshot(
            connection,
            tenant_id,
            virtual_key_id,
        )
        snapshots.append((virtual_key_id, object_name, summary))
    return snapshots


def _audit_request_meta(value: AuditRequestMeta | None) -> AuditRequestMeta:
    """为内部直调兼容空来源元数据；HTTP 路由必须传入由真实 Request 构造的可信来源。"""

    return value or AuditRequestMeta(client_ip=None, user_agent=None)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建项目管理数据库连接，并把基础设施错误映射为统一 API 错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库基础设施错误统一翻译，避免连接串或文件路径透出给 API 调用方。
    except DbError as exc:
        # 数据库错误必须保留统一错误码和 request_id，不把路径或 SQL 细节透出给调用方。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _load_project(connection: sqlite3.Connection, tenant_id: str, project_id: str) -> ProjectInfo:
    """按租户读取项目详情，供更新、绑定和响应组装复用。"""

    row = connection.execute(
        """
        SELECT
          p.project_id, p.tenant_id, p.project_code, p.name, p.description,
          p.owner_user_id, u.display_name AS owner_display_name, p.status,
          p.created_at, p.updated_at,
          COUNT(DISTINCT a.application_id) AS application_count,
          COUNT(DISTINCT vk.virtual_key_id) AS virtual_key_count
        FROM projects p
        LEFT JOIN users u ON u.tenant_id=p.tenant_id AND u.user_id=p.owner_user_id
        LEFT JOIN applications a ON a.tenant_id=p.tenant_id AND a.project_id=p.project_id
        LEFT JOIN virtual_keys vk ON vk.tenant_id=p.tenant_id AND vk.project_id=p.project_id
        WHERE p.tenant_id=? AND p.project_id=?
        GROUP BY p.project_id
        """,
        (tenant_id, project_id),
    ).fetchone()
    if row is None:
        # 项目读取始终带 tenant_id，未命中时统一返回 not_found 以避免跨租户探测。
        raise ApiError("project.not_found")
    return _project_info(row)


def _load_application(connection: sqlite3.Connection, tenant_id: str, application_id: str) -> ApplicationInfo:
    """按租户读取应用详情，确保应用不会跨租户被探测。"""

    row = connection.execute(
        """
        SELECT
          a.application_id, a.tenant_id, a.project_id, p.project_code, p.name AS project_name,
          a.application_code, a.name, a.description, a.status, a.created_at, a.updated_at,
          COUNT(DISTINCT vk.virtual_key_id) AS virtual_key_count
        FROM applications a
        JOIN projects p ON p.tenant_id=a.tenant_id AND p.project_id=a.project_id
        LEFT JOIN virtual_keys vk ON vk.tenant_id=a.tenant_id AND vk.application_id=a.application_id
        WHERE a.tenant_id=? AND a.application_id=?
        GROUP BY a.application_id
        """,
        (tenant_id, application_id),
    ).fetchone()
    if row is None:
        # 应用读取同样依赖 tenant_id 过滤，未命中不能透露其他租户应用是否存在。
        raise ApiError("application.not_found")
    return _application_info(row)


def _project_info(row: sqlite3.Row) -> ProjectInfo:
    """把项目查询行转换为 API DTO，并统一解析时间和计数字段。"""

    return ProjectInfo(
        project_id=row["project_id"],
        tenant_id=row["tenant_id"],
        project_code=row["project_code"],
        name=row["name"],
        description=row["description"],
        owner_user_id=row["owner_user_id"],
        owner_display_name=row["owner_display_name"],
        status=row["status"],
        application_count=_int_value(row["application_count"]),
        virtual_key_count=_int_value(row["virtual_key_count"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _application_info(row: sqlite3.Row) -> ApplicationInfo:
    """把应用查询行转换为 API DTO，并保留项目展示名称。"""

    return ApplicationInfo(
        application_id=row["application_id"],
        tenant_id=row["tenant_id"],
        project_id=row["project_id"],
        project_code=row["project_code"],
        project_name=row["project_name"],
        application_code=row["application_code"],
        name=row["name"],
        description=row["description"],
        status=row["status"],
        virtual_key_count=_int_value(row["virtual_key_count"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _write_project_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    project_id: str | None,
    application_id: str | None,
    event_type: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
) -> None:
    """写入项目域事件摘要，不保存任何密钥、Prompt 或上游响应。"""

    connection.execute(
        """
        INSERT INTO project_events(
          project_event_id, tenant_id, project_id, application_id, event_type,
          actor_user_id, request_id, trace_id, payload_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"pre_{uuid4().hex}",
            context.user.tenant_id,
            project_id,
            application_id,
            event_type,
            context.user.user_id,
            request_id,
            trace_id,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            _to_db_time(_utcnow()),
        ),
    )


def _log_project_event(
    context: AuthContext,
    event_type: str,
    object_id: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
) -> None:
    """记录结构化项目事件日志，payload 只包含脱敏治理元数据。"""

    logger.info(
        event_type,
        extra={
            "event": event_type,
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": object_id,
            "payload": payload,
        },
    )


def _provided_fields(payload: object, allowed: set[str]) -> set[str]:
    """读取调用方实际提交的字段集合，区分字段缺失和显式传 null。"""

    if hasattr(payload, "model_fields_set"):
        raw_fields = payload.model_fields_set
    else:
        raw_fields = getattr(payload, "__fields_set__", set())
    return {field for field in raw_fields if field in allowed}


def _normalize_code(value: object | None, error_code: str) -> str:
    """校验项目或应用编码，保证编码适合 URL、控制台和对账展示。"""

    code = str(value or "").strip().lower()
    if not CODE_PATTERN.match(code):
        # 编码一旦进入历史账本就不应频繁修改，创建时必须严格拦截非法值。
        raise ApiError(error_code)
    return code


def _normalize_name(value: object | None, error_code: str) -> str:
    """校验项目或应用名称，避免空名称和超长名称破坏控制台表格。"""

    name = str(value or "").strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        # 名称会出现在控制台列表和审计事件里，空值或超长值统一映射到调用方错误码。
        raise ApiError(error_code)
    return name


def _normalize_description(value: object | None) -> str | None:
    """归一化说明字段，空字符串统一保存为 NULL。"""

    if value is None:
        return None
    description = str(value).strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        # 说明字段错误归入通用创建/更新非法，由调用处的对象域错误码承接。
        raise ApiError("project.name_invalid")
    return description or None


def _normalize_status(value: object | None, error_code: str) -> str:
    """校验项目或应用状态，只允许治理明确的三个生命周期值。"""

    status = str(value or "").strip().lower()
    if status not in VALID_STATUSES:
        # 状态直接影响新绑定和控制台筛选，不能接受未知半启用状态。
        raise ApiError(error_code)
    return status


def _normalize_query_status(value: object | None) -> str:
    """归一化列表状态筛选，默认只返回 active，对 all 做显式支持。"""

    status = str(value or "active").strip().lower()
    if status == "all":
        return status
    if status not in VALID_STATUSES:
        # 列表筛选状态必须受控，避免拼出未定义生命周期导致治理视图误判。
        raise ApiError("project.status_invalid")
    return status


def _normalize_optional_search(value: object | None) -> str | None:
    """校验搜索关键字长度，避免控制台输入导致大范围异常查询。"""

    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > MAX_SEARCH_LENGTH:
        # 搜索词只服务控制台筛选，超长输入按非法状态类错误收口，避免异常查询成本。
        raise ApiError("project.status_invalid")
    return text


def _normalize_optional_text(value: object | None) -> str | None:
    """把可选文本归一化为空或去空格字符串。"""

    text = str(value or "").strip()
    return text or None


def _normalize_required_text(value: object | None, error_code: str) -> str:
    """读取必填 ID 字段，空值直接映射到调用方指定错误码。"""

    text = _normalize_optional_text(value)
    if not text:
        # 必填归属 ID 缺失会造成跨模块绑定不完整，按调用方指定错误码返回。
        raise ApiError(error_code)
    return text


def _normalize_optional_owner(connection: sqlite3.Connection, tenant_id: str, owner_user_id: object | None) -> str | None:
    """校验项目负责人属于当前租户；显式空值表示清除负责人。"""

    normalized = _normalize_optional_text(owner_user_id)
    if normalized is None:
        return None
    row = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND user_id=? AND status='active'",
        (tenant_id, normalized),
    ).fetchone()
    if row is None:
        # 负责人必须是当前租户 active 用户，避免项目责任人指向外部或停用账号。
        raise ApiError("project.owner_invalid")
    return normalized


def _ensure_writable_parent_status(status: str) -> None:
    """阻止在 archived 项目下创建或迁移应用，保留 disabled 项目的治理调整空间。"""

    if status == "archived":
        # 归档项目表示归属边界已经收口，不能继续新增应用或迁入应用。
        raise ApiError("project.status_invalid")


def _int_value(value: object) -> int:
    """把数据库计数字段安全转换为非负整数。"""

    try:
        parsed = int(value or 0)
    # 历史测试数据或 SQLite 聚合异常值不能阻断列表页，无法解析时按 0 展示。
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _parse_db_time(value: object) -> datetime:
    """解析数据库 UTC ISO 时间，兼容没有时区的旧测试数据。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 UTC datetime 序列化为数据库统一保存的 ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，保证同一事务内时间格式稳定。"""

    return datetime.now(UTC).replace(microsecond=0)
