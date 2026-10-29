"""PRD-071 控制台导航专项：冻结 PAGE-099 的后端权限裁剪与安全访问事件。"""

from __future__ import annotations

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS


def test_output_safety_navigation_reuses_request_log_permission() -> None:
    """入口只跟随实时 request_log.read，不能按角色名称或前端隐藏状态推导授权。"""

    for role in ("admin", "security_admin", "developer", "ops_admin"):
        items = _visible_navigation_items(tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role])))
        ids = [item.id for item in items]
        output_safety_index = ids.index("output-safety")
        item = items[output_safety_index]
        assert item.target_section == "output-safety"
        assert item.group == "安全与治理"
        assert item.required_permissions == ["request_log.read"]
        assert role in item.required_roles

    for role in ("viewer", "finance_admin"):
        item_ids = {
            item.id
            for item in _visible_navigation_items(
                tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role]))
            )
        }
        assert "output-safety" not in item_ids
    assert EVENT_TYPE_BY_NAVIGATION["output-safety"] == "console.page_viewed"


def test_output_safety_navigation_obeys_runtime_permission_revocation() -> None:
    """租户矩阵撤销 request_log.read 后立即裁剪入口，默认角色授权不能作为回退。"""

    matrix = {
        role: frozenset(permissions)
        for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items()
    }
    matrix["security_admin"] = matrix["security_admin"] - {"request_log.read"}
    items = _visible_navigation_items(
        tuple(sorted(matrix["security_admin"])),
        role_permissions_by_role=matrix,
    )

    assert "output-safety" not in {item.id for item in items}
