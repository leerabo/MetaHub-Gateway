"""实现租户级权限目录读取、角色矩阵 CAS 替换及可追踪治理事件。

运行时授权必须逐请求读取数据库；本模块中的共享目录仅用于验证租户投影和产品授权上限，
绝不能在矩阵缺失或损坏时作为回退权限。
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
from metahub_shared.rbac import (
    ADMIN_ROLE,
    CATALOG_REVISION,
    PERMISSION_CODES,
    ROLE_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    canonical_permission_codes,
    permission_matrix_sha256,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_control.audit import (
    AuditRequestMeta,
    commit_audited_transaction,
    new_audit_log_id,
    record_audit_log,
)
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.auth.service import AuthContext
from metahub_control.webhooks.service import dispatch_webhook_event

from .models import (
    PermissionInfo,
    PermissionMatrixResponse,
    ReplaceRolePermissionsRequest,
    RolePermissionInfo,
    RolePermissionUpdateResponse,
)

logger = logging.getLogger("metahub.control.permissions")

# 哈希、资源 ID 与可公开领域错误均使用冻结白名单，避免底层异常或任意文本越过 API 边界。
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_DOMAIN_ERRORS = frozenset(
    {
        "rbac.role_not_found",
        "rbac.payload_invalid",
        "rbac.permission_not_grantable",
        "rbac.role_immutable",
        "rbac.revision_conflict",
        "rbac.matrix_integrity_failed",
        "rbac.unavailable",
    }
)


def list_permission_matrix(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> PermissionMatrixResponse:
    """读取并完整验证当前租户的 48 权限、六角色及全部授权关系。

    参数：可信认证上下文和中间件链路标识。返回：稳定排序的完整租户矩阵。
    约束：任一目录、哈希、租户关系或产品上限异常都整体失败，不返回部分结果。
    """

    tenant_id = _trusted_tenant_id(context)
    connection = _connect(config)
    try:
        transaction = ResourceWriteTransaction(
            connection, config, context, "rbac.matrix.manage", read_only=True
        )
        transaction.begin()
        workspace_id = _authenticated_workspace_id(connection, transaction.context)
        permissions, roles = _load_and_validate_matrix(connection, tenant_id)
        transaction.verify()
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        return PermissionMatrixResponse(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            catalog_revision=CATALOG_REVISION,
            permissions=permissions,
            roles=roles,
            request_id=request_id,
            trace_id=trace_id,
        )
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    except ApiError:
        raise
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    except sqlite3.Error as exc:
        # 查询故障不返回 SQL、数据库路径或驱动文本，只暴露可重试领域码。
        raise ApiError("rbac.unavailable") from exc
    except (TypeError, ValueError, KeyError) as exc:
        # 存量值无法按冻结目录解码时视为完整性损坏，禁止回退默认权限。
        raise ApiError("rbac.matrix_integrity_failed") from exc
    finally:
        connection.close()


def replace_role_permissions(
    config: AppConfig,
    context: AuthContext,
    role_id: str,
    payload: ReplaceRolePermissionsRequest,
    *,
    request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> RolePermissionUpdateResponse:
    """以 ``BEGIN IMMEDIATE`` 和 revision CAS 完整替换一个非 admin 角色矩阵。

    业务矩阵、角色 revision、AuditLog 和不可变 source event 位于同一事务；网络 Webhook
    只在提交成功后执行。相同集合是无副作用 no-op，不提升 revision 或制造审计事实。
    """

    tenant_id = _trusted_tenant_id(context)
    normalized_role_id = _normalize_role_lookup_id(role_id)
    requested_codes = _normalize_requested_codes(payload.permission_codes)
    connection = _connect(config)
    webhook_event: WebhookEvent | None = None
    event_id: str | None = None
    updated_role: RolePermissionInfo | None = None
    try:
        transaction = ResourceWriteTransaction(connection, config, context, "rbac.matrix.manage")
        transaction.begin()
        context = transaction.context
        workspace_id = _authenticated_workspace_id(connection, context)
        permissions, roles = _load_and_validate_matrix(connection, tenant_id)
        role = next((item for item in roles if item.role_id == normalized_role_id), None)
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        if role is None:
            # 跨租户和真实不存在使用完全相同的 404，不能借 role_id 枚举其他租户。
            raise ApiError("rbac.role_not_found")
        if role.immutable or role.code == ADMIN_ROLE:
            # admin 是恢复入口，数据库 trigger 与服务层共同禁止任何矩阵变更。
            raise ApiError("rbac.role_immutable")
        if payload.expected_revision != role.permission_revision:
            raise ApiError("rbac.revision_conflict")
        grantable = ROLE_GRANTABLE_PERMISSIONS[role.code]
        if not set(requested_codes) <= grantable:
            # 产品职责分离是服务端上限，租户管理员也不能通过 PATCH 扩大边界。
            raise ApiError("rbac.permission_not_grantable")
        before_codes = tuple(role.permission_codes)
        if requested_codes == before_codes:
            # 读锁来自 BEGIN IMMEDIATE；no-op 显式回滚，保证零 revision、零审计和零事件。
            transaction.verify()
            connection.rollback()
            return RolePermissionUpdateResponse(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                role=role,
                changed=False,
                event_id=None,
                request_id=request_id,
                trace_id=trace_id,
            )

        permission_by_code = {item.code: item for item in permissions}
        added = tuple(sorted(set(requested_codes) - set(before_codes)))
        removed = tuple(sorted(set(before_codes) - set(requested_codes)))
        after_hash = permission_matrix_sha256(requested_codes)
        occurred_at = _utcnow()
        occurred_at_text = _to_db_time(occurred_at)

        # RolePermission 只做集合差异，保留未变化关联的身份与创建时间。
        for code in removed:
            cursor = connection.execute(
                """
                DELETE FROM role_permissions
                WHERE tenant_id=? AND role_id=? AND permission_id=?
                """,
                (tenant_id, role.role_id, permission_by_code[code].permission_id),
            )
            if cursor.rowcount != 1:
                # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
                raise ApiError("rbac.matrix_integrity_failed")
        for code in added:
            connection.execute(
                """
                INSERT INTO role_permissions(
                  role_permission_id, tenant_id, role_id, permission_id, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    f"rperm_{uuid4().hex}",
                    tenant_id,
                    role.role_id,
                    permission_by_code[code].permission_id,
                    occurred_at_text,
                    occurred_at_text,
                ),
            )

        cursor = connection.execute(
            """
            UPDATE roles
            SET permission_revision=permission_revision+1, matrix_sha256=?, updated_at=?
            WHERE tenant_id=? AND role_id=? AND permission_revision=? AND matrix_sha256=?
              AND is_permission_immutable=0
            """,
            (
                after_hash,
                occurred_at_text,
                tenant_id,
                role.role_id,
                role.permission_revision,
                role.matrix_sha256,
            ),
        )
        if cursor.rowcount != 1:
            # 锁内 CAS 未命中只表示客户端 revision 已陈旧，不能覆盖并发管理员结果。
            raise ApiError("rbac.revision_conflict")

        audit_log_id = new_audit_log_id()
        audit_record = record_audit_log(
            connection,
            context,
            action="role.permission_changed",
            object_type="role_permission",
            object_id=role.role_id,
            object_name=role.name,
            before_summary=_audit_summary(role, permission_codes=before_codes),
            after_summary={
                "role_code": role.code,
                "permission_revision": role.permission_revision + 1,
                "matrix_sha256": after_hash,
                "permission_count": len(requested_codes),
            },
            request_meta=request_meta,
            request_id=request_id,
            trace_id=trace_id,
            audit_log_id=audit_log_id,
            created_at=occurred_at,
        )
        if audit_record is None:
            # revision、hash 和计数中至少两项必然变化；无审计行说明接入合同已损坏。
            raise ApiError("rbac.matrix_integrity_failed")

        event_id = f"rpevt_{uuid4().hex}"
        event_payload = {
            "schema_version": 1,
            "role_id": role.role_id,
            "role_code": role.code,
            "before_revision": role.permission_revision,
            "after_revision": role.permission_revision + 1,
            "before_sha256": role.matrix_sha256,
            "after_sha256": after_hash,
            "added_permissions": list(added),
            "removed_permissions": list(removed),
        }
        # 在 commit 前完成安全 payload 和信封校验；这里只构造对象，不执行任何网络 I/O。
        webhook_event = build_webhook_event(
            tenant_id=tenant_id,
            event_type="role.permission_changed",
            payload=event_payload,
            actor_id=context.user.user_id,
            object_id=role.role_id,
            trace_id=trace_id,
            source_request_id=request_id,
            event_id=event_id,
            timestamp=occurred_at,
        )
        connection.execute(
            """
            INSERT INTO role_permission_events(
              event_id, tenant_id, role_id, actor_user_id, actor_role, request_id,
              before_revision, after_revision, before_sha256, after_sha256,
              added_permissions_json, removed_permissions_json, audit_log_id, trace_id,
              payload_json, delivery_status, last_error_code, created_at, updated_at, dispatched_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, NULL)
            """,
            (
                event_id,
                tenant_id,
                role.role_id,
                context.user.user_id,
                context.user.role,
                request_id,
                role.permission_revision,
                role.permission_revision + 1,
                role.matrix_sha256,
                after_hash,
                _json_text(list(added)),
                _json_text(list(removed)),
                audit_log_id,
                trace_id,
                _json_text(event_payload),
                occurred_at_text,
                occurred_at_text,
            ),
        )

        # 锁内重读并复核写后矩阵，防止 trigger 或 SQL 漂移产生无法授权的已提交状态。
        _, after_roles = _load_and_validate_matrix(connection, tenant_id)
        updated_role = next(
            (item for item in after_roles if item.role_id == role.role_id),
            None,
        )
        if (
            updated_role is None
            or tuple(updated_role.permission_codes) != requested_codes
            or updated_role.permission_revision != role.permission_revision + 1
            or updated_role.matrix_sha256 != after_hash
        ):
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed")
        transaction.commit(commit_audited_transaction)
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    except ApiError as exc:
        connection.rollback()
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        if exc.code in _DOMAIN_ERRORS or exc.code.startswith(
            ("auth.", "workspace_session.", "identity.")
        ):
            raise
        # 审计或其他内部领域故障统一折叠为矩阵服务不可用，不泄露下游表名。
        raise ApiError("rbac.unavailable") from exc
    except sqlite3.Error as exc:
        connection.rollback()
        # 约束、锁、I/O 或事件落库故障均回滚完整事务并返回稳定可重试错误。
        raise ApiError("rbac.unavailable") from exc
    except (TypeError, ValueError, KeyError) as exc:
        connection.rollback()
        # 内部目录或事件构造不一致属于完整性失败，不允许提交后再尝试修补。
        raise ApiError("rbac.matrix_integrity_failed") from exc
    finally:
        connection.close()

    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    if webhook_event is None or event_id is None or updated_role is None:
        # 该分支理论上不可达；显式失败关闭可防未来重构绕过事务初始化。
        raise ApiError("rbac.matrix_integrity_failed")
    _dispatch_role_permission_webhook(
        config,
        context,
        webhook_event,
        event_id=event_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    return RolePermissionUpdateResponse(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        role=updated_role,
        changed=True,
        event_id=event_id,
        request_id=request_id,
        trace_id=trace_id,
    )


def _authenticated_workspace_id(connection: sqlite3.Connection, context: AuthContext) -> str | None:
    if not context.session.session_id.startswith("wss_"):
        return None
    row = connection.execute(
        "SELECT workspace_id FROM workspace_sessions WHERE workspace_session_id=? AND tenant_id=?",
        (context.session.session_id, context.user.tenant_id),
    ).fetchone()
    if row is None:
        raise ApiError("auth.permission_denied")
    return str(row["workspace_id"])


def load_permission_matrix_from_connection(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> tuple[list[PermissionInfo], list[RolePermissionInfo]]:
    """在调用方既有事务或连接内加载并完整验证租户权限矩阵。

    参数：启用项目 row_factory 的 SQLite 连接及可信 tenant_id。返回：权限目录和六角色矩阵。
    该入口供 Console 反算兼容角色标签，不能绕过路由层 rbac.matrix.manage 权限。
    """

    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    return _load_and_validate_matrix(connection, tenant_id)


def _load_and_validate_matrix(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> tuple[list[PermissionInfo], list[RolePermissionInfo]]:
    """从单一连接加载并验证一个租户的完整权限目录和角色矩阵。"""

    template_by_code = _load_permission_templates(connection)
    permission_rows = connection.execute(
        """
        SELECT permission_id, tenant_id, code, category, name, description,
               risk_level, catalog_revision, status
        FROM permissions
        WHERE tenant_id=?
        ORDER BY category ASC, code ASC
        """,
        (tenant_id,),
    ).fetchall()
    permissions = _decode_permissions(permission_rows, tenant_id, template_by_code)
    role_rows = connection.execute(
        """
        SELECT
          role.role_id, role.tenant_id, role.code, role.name, role.status,
          role.permission_revision, role.matrix_sha256, role.is_permission_immutable,
          relation.role_permission_id, permission.permission_id,
          permission.tenant_id AS permission_tenant_id,
          permission.code AS permission_code, permission.status AS permission_status
        FROM roles role
        LEFT JOIN role_permissions relation
          ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        LEFT JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE role.tenant_id=?
        ORDER BY role.code ASC, permission.code ASC
        """,
        (tenant_id,),
    ).fetchall()
    roles = _decode_roles(role_rows, tenant_id)
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    return permissions, roles


def _load_permission_templates(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, str, str, str, int]]:
    """加载并严格验证数据库产品权限模板。

    稳定 permission code 和 catalog revision 由发布包约束；中文名称、描述及风险标签属于
    数据库展示合同，只要求模板完整、唯一且类型安全，不把 Python 文案误当成授权事实。
    """

    rows = connection.execute(
        """
        SELECT permission_code,category,name,description,risk_level,catalog_revision
        FROM rbac_permission_templates
        """
    ).fetchall()
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    if len(rows) != len(PERMISSION_CODES):
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ApiError("rbac.matrix_integrity_failed")
    templates: dict[str, tuple[str, str, str, str, int]] = {}
    for row in rows:
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
            or code not in PERMISSION_CODES
            or code in templates
            or any(type(value) is not str or not value for value in metadata[:3])
            or metadata[3] not in {"read", "write", "critical"}
            or type(metadata[4]) is not int
            or metadata[4] != CATALOG_REVISION
        ):
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed")
        templates[code] = metadata
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    if set(templates) != set(PERMISSION_CODES):
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ApiError("rbac.matrix_integrity_failed")
    return templates


def _decode_permissions(
    rows: list[sqlite3.Row],
    tenant_id: str,
    template_by_code: dict[str, tuple[str, str, str, str, int]],
) -> list[PermissionInfo]:
    """校验租户 Permission 是数据库 catalog v1 的完整、唯一且同租户投影。"""

    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    if len(rows) != len(PERMISSION_CODES):
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ApiError("rbac.matrix_integrity_failed")
    by_code: dict[str, PermissionInfo] = {}
    for row in rows:
        code = row["code"]
        template = template_by_code.get(code) if type(code) is str else None
        if (
            template is None
            or row["tenant_id"] != tenant_id
            or row["category"] != template[0]
            or row["name"] != template[1]
            or row["description"] != template[2]
            or row["risk_level"] != template[3]
            or type(row["catalog_revision"]) is not int
            or row["catalog_revision"] != template[4]
            or row["status"] != "active"
            or not _is_prefixed_id(row["permission_id"], "perm_")
            or code in by_code
        ):
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed")
        by_code[code] = PermissionInfo(
            permission_id=str(row["permission_id"]),
            tenant_id=tenant_id,
            code=code,
            category=template[0],
            name=template[1],
            description=template[2],
            risk_level=template[3],
            status="active",
        )
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    if set(by_code) != set(PERMISSION_CODES):
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ApiError("rbac.matrix_integrity_failed")
    return sorted(by_code.values(), key=lambda item: (item.category, item.code))

def _decode_roles(rows: list[sqlite3.Row], tenant_id: str) -> list[RolePermissionInfo]:
    """校验六角色、授权上限、revision 与矩阵哈希并返回产品顺序。"""

    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        code = str(row["code"])
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        if code not in ROLE_CODES or row["tenant_id"] != tenant_id:
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed")
        current = grouped.setdefault(
            code,
            {
                "role_id": row["role_id"],
                "name": row["name"],
                "status": row["status"],
                "revision": row["permission_revision"],
                "hash": row["matrix_sha256"],
                "immutable": row["is_permission_immutable"],
                "permission_codes": [],
                "relation_ids": set(),
            },
        )
        if any(
            current[key] != row[column]
            for key, column in (
                ("role_id", "role_id"),
                ("name", "name"),
                ("status", "status"),
                ("revision", "permission_revision"),
                ("hash", "matrix_sha256"),
                ("immutable", "is_permission_immutable"),
            )
        ):
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed")
        relation_id = row["role_permission_id"]
        if relation_id is None:
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            if row["permission_id"] is not None or row["permission_code"] is not None:
                # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
                raise ApiError("rbac.matrix_integrity_failed")
            continue
        relation_ids = current["relation_ids"]
        if not isinstance(relation_ids, set) or relation_id in relation_ids:
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed")
        relation_ids.add(relation_id)
        if (
            not _is_prefixed_id(relation_id, "rperm_")
            or not _is_prefixed_id(row["permission_id"], "perm_")
            or row["permission_tenant_id"] != tenant_id
            or row["permission_status"] != "active"
            or row["permission_code"] not in PERMISSION_CODES
        ):
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed")
        codes = current["permission_codes"]
        if not isinstance(codes, list):
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed")
        codes.append(str(row["permission_code"]))

    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    if set(grouped) != set(ROLE_CODES):
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ApiError("rbac.matrix_integrity_failed")
    decoded: list[RolePermissionInfo] = []
    for code in ROLE_CODES:
        stored = grouped[code]
        raw_codes = stored["permission_codes"]
        revision = stored["revision"]
        matrix_hash = stored["hash"]
        immutable = stored["immutable"]
        if (
            not isinstance(raw_codes, list)
            or type(revision) is not int
            or revision < 1
            or type(matrix_hash) is not str
            or _LOWER_SHA256.fullmatch(matrix_hash) is None
            or type(immutable) is not int
            or immutable not in (0, 1)
            or bool(immutable) != (code == ADMIN_ROLE)
            or stored["status"] != "active"
            or type(stored["role_id"]) is not str
            or _SAFE_ID.fullmatch(stored["role_id"]) is None
            or type(stored["name"]) is not str
            or not str(stored["name"]).strip()
        ):
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed")
        try:
            codes = canonical_permission_codes(raw_codes)
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        except ValueError:
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed") from None
        grantable = ROLE_GRANTABLE_PERMISSIONS[code]
        if (
            not set(codes) <= grantable
            or (code == ADMIN_ROLE and set(codes) != set(PERMISSION_CODES))
            or permission_matrix_sha256(codes) != matrix_hash
        ):
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise ApiError("rbac.matrix_integrity_failed")
        decoded.append(
            RolePermissionInfo(
                role_id=str(stored["role_id"]),
                tenant_id=tenant_id,
                code=code,
                name=str(stored["name"]),
                permission_revision=revision,
                matrix_sha256=matrix_hash,
                immutable=bool(immutable),
                permission_codes=list(codes),
                grantable_permission_codes=sorted(grantable),
            )
        )
    return decoded


def _normalize_requested_codes(values: list[str]) -> tuple[str, ...]:
    """把 PATCH 权限列表转换为唯一 canonical 元组并统一映射输入错误。"""

    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    if type(values) is not list or len(values) > len(PERMISSION_CODES):
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ApiError("rbac.payload_invalid")
    try:
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        return canonical_permission_codes(values)
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    except ValueError:
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ApiError("rbac.payload_invalid") from None


def _normalize_role_lookup_id(value: object) -> str:
    """校验 path role_id 的有界安全文本；非法值与不存在保持同形 404。"""

    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ApiError("rbac.role_not_found")
    return value


def _trusted_tenant_id(context: AuthContext) -> str:
    """复核认证上下文的用户与租户身份一致，禁止调用方拼接跨租户上下文。"""

    tenant_id = context.user.tenant_id
    if (
        type(tenant_id) is not str
        or not tenant_id
        or tenant_id != context.tenant.tenant_id
        or _SAFE_ID.fullmatch(tenant_id) is None
    ):
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ApiError("rbac.matrix_integrity_failed")
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    return tenant_id


def _audit_summary(
    role: RolePermissionInfo,
    *,
    permission_codes: tuple[str, ...],
) -> dict[str, object]:
    """构造 AuditLog 白名单摘要，不复制完整权限集合或前端草稿。"""

    return {
        "role_code": role.code,
        "permission_revision": role.permission_revision,
        "matrix_sha256": role.matrix_sha256,
        "permission_count": len(permission_codes),
    }


def _dispatch_role_permission_webhook(
    config: AppConfig,
    context: AuthContext,
    event: WebhookEvent,
    *,
    event_id: str,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后尽力投递矩阵事件，并把本次结果写回 source event 状态。"""

    status = "dispatched"
    error_code: str | None = None
    try:
        deliveries = dispatch_webhook_event(
            config,
            context.user.tenant_id,
            event,
            request_id=request_id,
            trace_id=trace_id,
        )
        if any(delivery.status != "success" for delivery in deliveries):
            status = "failed"
            error_code = "webhook.delivery_unavailable"
    except Exception:
        # 外部异常可能含 URL、响应或签名细节；只记录固定定位字段和错误码。
        status = "failed"
        error_code = "webhook.delivery_unavailable"
        logger.error(
            "rbac.webhook_dispatch_failed",
            extra={
                "event": "rbac.webhook_dispatch_failed",
                "tenant_id": context.user.tenant_id,
                "actor_id": context.user.user_id,
                "object_id": event.object_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": error_code,
            },
        )
    _mark_event_delivery(
        config,
        context.user.tenant_id,
        event_id,
        status=status,
        error_code=error_code,
        request_id=request_id,
        trace_id=trace_id,
    )


def _mark_event_delivery(
    config: AppConfig,
    tenant_id: str,
    event_id: str,
    *,
    status: str,
    error_code: str | None,
    request_id: str,
    trace_id: str,
) -> None:
    """在独立短事务中把 source event 从 pending 推进到 dispatched 或 failed。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(config)
        now = _to_db_time(_utcnow())
        cursor = connection.execute(
            """
            UPDATE role_permission_events
            SET delivery_status=?, last_error_code=?, updated_at=?, dispatched_at=?
            WHERE tenant_id=? AND event_id=? AND delivery_status='pending'
            """,
            (status, error_code, now, now, tenant_id, event_id),
        )
        if cursor.rowcount != 1:
            # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
            raise sqlite3.IntegrityError("role_permission_event_state")
        connection.commit()
    except Exception:
        if connection is not None:
            connection.rollback()
        # 主矩阵和审计已提交，状态回写失败只能记录固定事件，不能把成功响应改成 5xx。
        logger.error(
            "rbac.event_status_update_failed",
            extra={
                "event": "rbac.event_status_update_failed",
                "tenant_id": tenant_id,
                "object_id": event_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "rbac.unavailable",
            },
        )
    finally:
        if connection is not None:
            connection.close()


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建权限服务数据库连接，并隐藏数据库 URL 与底层连接细节。"""

    try:
        return connect_database(config.database_url)
    # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
    except (DbError, OSError) as exc:
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ApiError("rbac.unavailable") from exc


def _is_prefixed_id(value: object, prefix: str) -> bool:
    """判断数据库 ID 是否为固定前缀加 32 位小写十六进制。"""

    if type(value) is not str or not value.startswith(prefix):
        return False
    suffix = value[len(prefix) :]
    return len(suffix) == 32 and all(character in "0123456789abcdef" for character in suffix)


def _json_text(value: object) -> str:
    """生成稳定紧凑 JSON，供事件列、payload 和测试逐字复核。"""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _utcnow() -> datetime:
    """返回带 UTC 时区的当前时间，集中保持审计和事件时间一致。"""

    return datetime.now(UTC)


def _to_db_time(value: datetime) -> str:
    """把带时区时间规范化为 SQLite 使用的 UTC 秒级 ``Z`` 文本。

    权限矩阵、AuditLog 和 source event 共享同一个业务发生时间；审计服务的既有
    数据库合同精确到秒，因此这里也必须截断微秒，避免同一事务中的事件 owner
    触发器因两个等价但文本不同的时间戳而拒绝合法写入。
    """

    if value.tzinfo is None:
        # 权限目录、租户关系或矩阵哈希异常时必须回滚并失败关闭，禁止按默认角色补权。
        raise ValueError("rbac.time_invalid")
    return (
        value.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
