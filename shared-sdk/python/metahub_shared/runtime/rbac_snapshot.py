"""后台任务实时权限快照：按租户重建完整 RBAC 矩阵，拒绝静态角色回退和跨租户关系。

Database-backed RBAC snapshots for durable, sessionless actors.

Recovery workers and internal data-plane calls receive actor identities only from immutable
tenant-owned rows. This module validates the complete current catalog, tenant projection, role
ceiling, relation ownership, and matrix hash before returning any permissions. Static default
roles are validation ceilings only and are never used as fallback grants.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from metahub_shared.db.schema import table_exists
from metahub_shared.rbac import (
    ADMIN_ROLE,
    CATALOG_REVISION,
    PERMISSION_CODES,
    ROLE_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    authorization_fingerprint,
    canonical_permission_codes,
    permission_matrix_sha256,
)
from metahub_shared.runtime.errors import ApiError


@dataclass(frozen=True, slots=True)
class RuntimePermissionSnapshot:
    """封装后台操作者的实时权限、矩阵版本与授权指纹，不包含会话或令牌。"""
    permission_codes: tuple[str, ...]
    permission_revision: int
    matrix_sha256: str
    authorization_fingerprint: str


def require_actor_runtime_permissions(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    permission_codes: tuple[str, ...],
    unavailable_error_code: str,
) -> RuntimePermissionSnapshot:
    """在调用方事务内重建后台操作者权限并校验所需权限，禁止静态角色回退。

    Validate a durable actor and require permissions in the caller's DB transaction.
    """

    if not connection.in_transaction:
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError(unavailable_error_code)
    if (
        type(tenant_id) is not str
        or not tenant_id
        or type(user_id) is not str
        or not user_id
        or not permission_codes
        or len(permission_codes) != len(set(permission_codes))
        or any(type(code) is not str or code not in PERMISSION_CODES for code in permission_codes)
    ):
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")
    try:
        actor = connection.execute(
            """
            SELECT legacy_user.role
            FROM users legacy_user
            JOIN tenants tenant ON tenant.tenant_id=legacy_user.tenant_id
            WHERE legacy_user.tenant_id=? AND legacy_user.user_id=?
              AND legacy_user.status='active' AND tenant.status='active'
            """,
            (tenant_id, user_id),
        ).fetchone()
        if actor is None:
            # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
            raise ApiError("auth.permission_denied")
        snapshot = _load_permission_snapshot(
            connection,
            tenant_id=tenant_id,
            user_id=user_id,
            role_code=actor["role"],
        )
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        if any(code not in snapshot.permission_codes for code in permission_codes):
            # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
            raise ApiError("auth.permission_denied")
        return snapshot
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    except ApiError:
        raise
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    except sqlite3.Error as exc:
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError(unavailable_error_code) from exc
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    except (KeyError, TypeError, ValueError) as exc:
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed") from exc


def _load_permission_snapshot(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    role_code: object,
) -> RuntimePermissionSnapshot:
    """从租户权限目录和角色关系重建完整快照，并验证矩阵哈希与管理员闭包。"""
    required_tables = {
        "roles",
        "permissions",
        "role_permissions",
        "rbac_permission_templates",
        "rbac_role_permission_templates",
    }
    if not all(table_exists(connection, table) for table in sorted(required_tables)):
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")

    template_by_code = _validate_permission_templates(connection)
    permission_by_id = _load_tenant_permission_catalog(
        connection,
        tenant_id,
        template_by_code,
    )
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    if type(role_code) is not str or role_code not in ROLE_CODES:
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")
    role_rows = connection.execute(
        """
        SELECT role_id,tenant_id,code,status,permission_revision,matrix_sha256,
               is_permission_immutable
        FROM roles WHERE tenant_id=? AND code=?
        """,
        (tenant_id, role_code),
    ).fetchall()
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    if len(role_rows) != 1:
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
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
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")
    granted = _load_role_permission_codes(
        connection,
        tenant_id=tenant_id,
        role_id=role["role_id"],
        permission_by_id=permission_by_id,
    )
    grantable = ROLE_GRANTABLE_PERMISSIONS[role_code]
    if not set(granted) <= grantable:
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    if role_code == ADMIN_ROLE and set(granted) != set(PERMISSION_CODES):
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")
    calculated = permission_matrix_sha256(granted)
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    if role["matrix_sha256"] != calculated:
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    return RuntimePermissionSnapshot(
        permission_codes=granted,
        permission_revision=revision,
        matrix_sha256=calculated,
        authorization_fingerprint=authorization_fingerprint(
            tenant_id=tenant_id,
            user_id=user_id,
            role=role_code,
            permission_revision=revision,
            matrix_sha256=calculated,
        ),
    )


def _validate_permission_templates(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, str, str, str, int]]:
    """验证全局权限模板和角色授权上限与代码目录完全一致。"""
    rows = connection.execute(
        """
        SELECT permission_code,category,name,description,risk_level,catalog_revision
        FROM rbac_permission_templates
        """
    ).fetchall()
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    if len(rows) != len(PERMISSION_CODES):
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")
    template_by_code: dict[str, tuple[str, str, str, str, int]] = {}
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
            or code in template_by_code
            or code not in PERMISSION_CODES
            or any(type(value) is not str or not value for value in metadata[:3])
            or metadata[3] not in {"read", "write", "critical"}
            or metadata[4] != CATALOG_REVISION
        ):
            # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
            raise ApiError("rbac.matrix_integrity_failed")
        template_by_code[code] = metadata
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    if set(template_by_code) != set(PERMISSION_CODES):
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")

    role_rows = connection.execute(
        """
        SELECT role_code,permission_code,default_granted
        FROM rbac_role_permission_templates
        """
    ).fetchall()
    expected_pairs = {
        (role, code)
        for role, permission_codes in ROLE_GRANTABLE_PERMISSIONS.items()
        for code in permission_codes
    }
    actual_pairs: set[tuple[str, str]] = set()
    for row in role_rows:
        pair = (row["role_code"], row["permission_code"])
        if pair in actual_pairs or pair not in expected_pairs or row["default_granted"] != 1:
            # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
            raise ApiError("rbac.matrix_integrity_failed")
        actual_pairs.add(pair)
    if actual_pairs != expected_pairs:
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")
    return template_by_code


def _load_tenant_permission_catalog(
    connection: sqlite3.Connection,
    tenant_id: str,
    template_by_code: dict[str, tuple[str, str, str, str, int]],
) -> dict[str, str]:
    """加载指定租户的权限投影，并核对每条元数据与全局模板一致。"""
    rows = connection.execute(
        """
        SELECT permission_id,tenant_id,code,category,name,description,risk_level,
               catalog_revision,status
        FROM permissions WHERE tenant_id=?
        """,
        (tenant_id,),
    ).fetchall()
    if len(rows) != len(template_by_code):
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
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
            # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
            raise ApiError("rbac.matrix_integrity_failed")
        metadata = (
            row["category"],
            row["name"],
            row["description"],
            row["risk_level"],
            row["catalog_revision"],
        )
        if metadata != template_by_code[code] or row["status"] != "active":
            # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
            raise ApiError("rbac.matrix_integrity_failed")
        permission_by_id[permission_id] = code
        seen_codes.add(code)
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    if seen_codes != set(PERMISSION_CODES):
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed")
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    return permission_by_id


def _load_role_permission_codes(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    role_id: object,
    permission_by_id: dict[str, str],
) -> tuple[str, ...]:
    """加载指定租户角色的权限关系，拒绝孤儿关系、跨租户关系和越权授予。"""
    rows = connection.execute(
        """
        SELECT rp.role_permission_id,rp.tenant_id AS relation_tenant_id,
               rp.role_id AS relation_role_id,rp.permission_id AS relation_permission_id,
               role.tenant_id AS role_tenant_id,role.code AS relation_role_code,
               permission.tenant_id AS permission_tenant_id,
               permission.code AS permission_code,permission.status AS permission_status
        FROM role_permissions rp
        LEFT JOIN roles role ON role.role_id=rp.role_id
        LEFT JOIN permissions permission ON permission.permission_id=rp.permission_id
        WHERE rp.tenant_id=? OR role.tenant_id=? OR permission.tenant_id=?
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
            # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
            raise ApiError("rbac.matrix_integrity_failed")
        relation_ids.add(relation_id)
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        if row["relation_role_id"] == role_id:
            granted_codes.append(permission_code)
    try:
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        return canonical_permission_codes(granted_codes)
    # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
    except ValueError as exc:
        # 权限目录、租户投影或矩阵哈希不一致时关闭访问，禁止沿用漂移授权。
        raise ApiError("rbac.matrix_integrity_failed") from exc


__all__ = ["RuntimePermissionSnapshot", "require_actor_runtime_permissions"]
