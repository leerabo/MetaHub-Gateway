"""验证 PAGE-108/109 导航只服从后端实时 approval.read 权限。"""

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    NAVIGATION_DEFINITIONS,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS


def test_approval_navigation_requires_only_base_read_permission() -> None:
    """审批入口声明 base read；目标域权限由列表和详情接口继续裁剪。"""

    definition = next(item for item in NAVIGATION_DEFINITIONS if item.id == "approvals")
    assistant = next(item for item in NAVIGATION_DEFINITIONS if item.id == "admin-assistant")
    assert definition.target_section == "approvals"
    assert definition.group == "总览"
    assert definition.order > assistant.order
    assert definition.required_permissions == ("approval.read",)
    assert EVENT_TYPE_BY_NAVIGATION["approvals"] == "console.page_viewed"

    for role in ("admin", "finance_admin", "security_admin"):
        items = _visible_navigation_items(tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role])))
        item = next(candidate for candidate in items if candidate.id == "approvals")
        assert item.required_permissions == ["approval.read"]
        assert role in item.required_roles


def test_approval_navigation_follows_runtime_read_revocation() -> None:
    """撤销 approval.read 后后端不再下发入口，单独授予时只暴露审批页。"""

    narrowed = tuple(
        sorted(code for code in ROLE_GRANTABLE_PERMISSIONS["admin"] if code != "approval.read")
    )
    assert "approvals" not in {item.id for item in _visible_navigation_items(narrowed)}

    items = _visible_navigation_items(("approval.read",))
    assert [item.id for item in items] == ["approvals"]


def test_roles_without_default_approval_grant_have_no_entry() -> None:
    """developer、viewer 与 ops_admin 不能从角色名获得审批入口。"""

    for role in ("developer", "viewer", "ops_admin"):
        items = _visible_navigation_items(tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role])))
        assert "approvals" not in {item.id for item in items}
