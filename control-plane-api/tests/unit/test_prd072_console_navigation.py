"""PRD-072 控制台导航专项：冻结文件工作台的后端权限裁剪与安全访问事件。"""

from __future__ import annotations

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS


def test_file_navigation_uses_read_permission_and_keeps_write_independent() -> None:
    """后端只以实时 file.read 展示入口，file.write 不能成为读取文件名的隐式授权。"""

    for role in ("admin", "developer", "viewer", "security_admin"):
        items = _visible_navigation_items(tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role])))
        item = next(candidate for candidate in items if candidate.id == "files")
        assert item.target_section == "files"
        assert item.group == "开发者"
        assert item.required_permissions == ["file.read"]
        assert role in item.required_roles

    for role in ("finance_admin", "ops_admin"):
        item_ids = {
            item.id
            for item in _visible_navigation_items(
                tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role]))
            )
        }
        assert "files" not in item_ids
    assert EVENT_TYPE_BY_NAVIGATION["files"] == "console.page_viewed"


def test_file_navigation_obeys_runtime_read_revocation() -> None:
    """租户矩阵撤销 file.read 后立即裁剪入口，默认角色名称不能充当权限回退。"""

    matrix = {
        role: frozenset(permissions)
        for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items()
    }
    matrix["security_admin"] = matrix["security_admin"] - {"file.read"}
    items = _visible_navigation_items(
        tuple(sorted(matrix["security_admin"])),
        role_permissions_by_role=matrix,
    )

    assert "files" not in {item.id for item in items}
