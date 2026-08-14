"""验证 PAGE-110/111 导航只服从后端实时套餐与额度读取权限。"""

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    NAVIGATION_DEFINITIONS,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS


def test_plan_and_quota_navigation_use_independent_read_permissions() -> None:
    """两个入口只声明各自 read；写权限仅投影命令，不能制造页面入口。"""

    plan = next(item for item in NAVIGATION_DEFINITIONS if item.id == "plans")
    quota = next(item for item in NAVIGATION_DEFINITIONS if item.id == "quota-buckets")
    assert plan.target_section == "plans"
    assert quota.target_section == "quota-buckets"
    assert plan.group == quota.group == "调用与成本"
    assert plan.order < quota.order
    assert plan.required_permissions == ("billing.plan.read",)
    assert quota.required_permissions == ("billing.quota.read",)
    assert EVENT_TYPE_BY_NAVIGATION["plans"] == "console.page_viewed"
    assert EVENT_TYPE_BY_NAVIGATION["quota-buckets"] == "console.page_viewed"


def test_plan_and_quota_navigation_follow_runtime_revocation() -> None:
    """撤销任一 read 只裁剪对应入口，write 单独存在时也不能补足 read。"""

    only_plan = _visible_navigation_items(("billing.plan.read", "billing.quota.write"))
    assert [item.id for item in only_plan] == ["plans"]

    only_quota = _visible_navigation_items(("billing.plan.write", "billing.quota.read"))
    assert [item.id for item in only_quota] == ["quota-buckets"]


def test_default_roles_do_not_gain_navigation_from_role_names() -> None:
    """只有权限目录事实决定入口；默认未授权角色不能从角色名推导套餐或额度页面。"""

    for role in ("admin", "finance_admin"):
        items = _visible_navigation_items(tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role])))
        ids = {item.id for item in items}
        assert {"plans", "quota-buckets"} <= ids

    for role in ("developer", "viewer", "security_admin", "ops_admin"):
        items = _visible_navigation_items(tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role])))
        ids = {item.id for item in items}
        assert "plans" not in ids
        assert "quota-buckets" not in ids
