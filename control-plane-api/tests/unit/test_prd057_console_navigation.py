"""PRD-057 控制台导航专项：冻结 PAGE-083 顺序、角色裁剪和安全访问事件类型。"""

from __future__ import annotations

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS


def test_prd057_webhook_dead_letter_navigation_is_manager_only_and_adjacent() -> None:
    """admin/developer 可见且紧随 Webhook 设置，viewer 在后端定义层即被裁剪。"""

    for role in ("admin", "developer", "security_admin", "ops_admin"):
        items = _visible_navigation_items(
            tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role]))
        )
        ids = [item.id for item in items]
        webhook_index = ids.index("webhooks")
        assert ids[webhook_index + 1] == "webhook-dead-letters"
        dead_letter = items[webhook_index + 1]
        assert dead_letter.target_section == "webhook-dead-letters"
        assert dead_letter.required_permissions == ["webhook.manage"]
        assert dead_letter.required_roles == [
            "admin", "developer", "security_admin", "ops_admin"
        ]
        assert dead_letter.order == 92

    for role in ("viewer", "finance_admin"):
        item_ids = {
            item.id
            for item in _visible_navigation_items(
                tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role]))
            )
        }
        assert "webhook-dead-letters" not in item_ids
    # 页面访问事件只能写通用安全摘要，不能携带 event_id 或 Endpoint 等业务字段。
    assert EVENT_TYPE_BY_NAVIGATION["webhook-dead-letters"] == "console.page_viewed"
