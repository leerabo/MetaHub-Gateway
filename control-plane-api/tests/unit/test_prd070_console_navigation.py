"""PRD-070 控制台导航专项：冻结 PAGE-098 的后端权限裁剪和安全访问事件。"""

from __future__ import annotations

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS


def test_session_risk_navigation_is_permission_driven_and_adjacent_to_intent_page() -> None:
    """只有实时拥有 read 权限的角色可见入口，且页面紧随敏感意图分类。"""

    for role in ("admin", "security_admin"):
        items = _visible_navigation_items(tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role])))
        ids = [item.id for item in items]
        session_risk_index = ids.index("session-risks")
        assert ids[session_risk_index - 1] == "intent-security"
        item = items[session_risk_index]
        assert item.target_section == "session-risks"
        assert item.group == "安全与治理"
        assert item.required_permissions == ["session_risk.read"]
        assert item.required_roles == ["admin", "security_admin"]

    for role in ("developer", "viewer", "finance_admin", "ops_admin"):
        item_ids = {
            item.id
            for item in _visible_navigation_items(
                tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role]))
            )
        }
        assert "session-risks" not in item_ids
    # 页面访问只保留通用来源/视口摘要，风险事实必须由专用 API 和事件承载。
    assert EVENT_TYPE_BY_NAVIGATION["session-risks"] == "console.page_viewed"


def test_session_risk_navigation_obeys_tenant_matrix_revocation() -> None:
    """租户撤销 read 后立即裁剪入口，不能依据默认角色名称继续显示。"""

    matrix = {
        role: frozenset(permissions)
        for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items()
    }
    matrix["security_admin"] = matrix["security_admin"] - {"session_risk.read"}
    items = _visible_navigation_items(
        tuple(sorted(matrix["security_admin"])),
        role_permissions_by_role=matrix,
    )

    assert "session-risks" not in {item.id for item in items}
