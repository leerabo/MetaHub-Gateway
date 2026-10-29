"""PRD-073 控制台导航专项：冻结文件安全报告入口的后端权限边界。"""

from __future__ import annotations

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS


def test_file_security_navigation_requires_only_file_read() -> None:
    """报告入口只要求 file.read，扫描权限仅控制有成本的 POST 命令。

    参数：无。返回值：无。关键约束：SecurityAdmin 可以只读审查已有报告；后端不能把
    ``file.security.scan`` 放进导航门槛，否则会把合法只读用户误裁掉。
    """

    for role in ("admin", "developer", "viewer", "security_admin"):
        items = _visible_navigation_items(tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role])))
        item = next(candidate for candidate in items if candidate.id == "file-security")
        assert item.target_section == "file-security"
        assert item.group == "安全与治理"
        assert item.required_permissions == ["file.read"]
        assert role in item.required_roles

    for role in ("finance_admin", "ops_admin"):
        item_ids = {
            item.id
            for item in _visible_navigation_items(
                tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role]))
            )
        }
        assert "file-security" not in item_ids
    assert EVENT_TYPE_BY_NAVIGATION["file-security"] == "console.page_viewed"


def test_file_security_navigation_obeys_runtime_read_revocation() -> None:
    """实时撤销 file.read 后立即裁剪入口，默认角色名不能恢复旧权限。

    参数：无。返回值：无。关键约束：前端是否仍有旧菜单不影响后端结果；租户定制矩阵
    是唯一授权事实，撤销扫描权限本身则不得隐藏只读报告入口。
    """

    matrix = {
        role: frozenset(permissions)
        for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items()
    }
    matrix["security_admin"] = matrix["security_admin"] - {"file.read"}
    revoked_items = _visible_navigation_items(
        tuple(sorted(matrix["security_admin"])),
        role_permissions_by_role=matrix,
    )
    assert "file-security" not in {item.id for item in revoked_items}

    scan_revoked = matrix["admin"] - {"file.security.scan"}
    read_only_items = _visible_navigation_items(
        tuple(sorted(scan_revoked)),
        role_permissions_by_role={**matrix, "admin": scan_revoked},
    )
    assert "file-security" in {item.id for item in read_only_items}
