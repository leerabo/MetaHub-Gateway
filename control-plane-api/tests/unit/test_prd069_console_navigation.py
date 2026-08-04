"""PRD-069 控制台导航专项：冻结 PAGE-096 的后端权限裁剪和安全访问事件。"""

from __future__ import annotations

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS


def test_intent_security_navigation_is_permission_driven_and_adjacent_to_dlp() -> None:
    """只有实时拥有 classify 权限的角色可见入口，且页面位于 DLP 规则之后。"""

    for role in ("admin", "security_admin"):
        items = _visible_navigation_items(
            tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role]))
        )
        ids = [item.id for item in items]
        intent_index = ids.index("intent-security")
        assert ids[intent_index - 1] == "dlp-rules"
        item = items[intent_index]
        assert item.target_section == "intent-security"
        assert item.group == "安全与治理"
        assert item.required_permissions == ["intent_risk.classify"]
        assert item.required_roles == ["admin", "security_admin"]

    for role in ("developer", "viewer", "finance_admin", "ops_admin"):
        item_ids = {
            item.id
            for item in _visible_navigation_items(
                tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role]))
            )
        }
        assert "intent-security" not in item_ids
    # 页面访问只允许通用三字段摘要；正文与分类结果由专用 API/事件各自承载。
    assert EVENT_TYPE_BY_NAVIGATION["intent-security"] == "console.page_viewed"


def test_intent_security_navigation_obeys_tenant_matrix_revocation() -> None:
    """租户撤销权限后必须立即裁剪入口，不能依据默认角色名称继续显示。"""

    matrix = {
        role: frozenset(permissions)
        for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items()
    }
    matrix["security_admin"] = matrix["security_admin"] - {
        "intent_risk.classify"
    }
    items = _visible_navigation_items(
        tuple(sorted(matrix["security_admin"])),
        role_permissions_by_role=matrix,
    )

    assert "intent-security" not in {item.id for item in items}
