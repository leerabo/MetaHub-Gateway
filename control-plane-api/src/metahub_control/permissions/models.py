"""定义细粒度角色权限矩阵 API 的安全请求与响应结构。

所有租户、角色和权限身份都由认证上下文及数据库恢复；请求 DTO 不接受 ``tenant_id``、
操作者或哈希字段，防止浏览器覆盖服务端安全边界。
"""

from __future__ import annotations

from metahub_shared.rbac import PERMISSION_CODES
from pydantic import BaseModel, ConfigDict, Field


class PermissionInfo(BaseModel):
    """描述当前租户的一项权限目录投影，不包含任何用户或凭据信息。"""

    permission_id: str
    tenant_id: str
    code: str
    category: str
    name: str
    description: str
    risk_level: str
    status: str


class RolePermissionInfo(BaseModel):
    """描述一个固定角色的已授予集合和产品授权上限。"""

    role_id: str
    tenant_id: str
    code: str
    name: str
    permission_revision: int
    matrix_sha256: str
    immutable: bool
    permission_codes: list[str]
    grantable_permission_codes: list[str]


class PermissionMatrixResponse(BaseModel):
    """返回当前租户完整权限目录；任何完整性异常都不得返回部分矩阵。"""

    catalog_revision: int
    permissions: list[PermissionInfo]
    roles: list[RolePermissionInfo]
    request_id: str
    trace_id: str


class ReplaceRolePermissionsRequest(BaseModel):
    """完整替换一个可变角色权限集合的严格请求。

    ``expected_revision`` 是写入 CAS 前置条件；权限 code 必须由服务层再次执行规范化和
    产品上限校验，Pydantic 校验不能代替数据库租户约束。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=1)
    permission_codes: list[str] = Field(max_length=len(PERMISSION_CODES))


class RolePermissionUpdateResponse(BaseModel):
    """返回矩阵替换后的权威角色状态及是否产生真实变更。"""

    role: RolePermissionInfo
    changed: bool
    event_id: str | None
    request_id: str
    trace_id: str
