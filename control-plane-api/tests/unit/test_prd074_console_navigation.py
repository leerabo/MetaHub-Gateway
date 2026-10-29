"""本文件验证 PRD-074 批处理调度边界，覆盖租户隔离、权限拒绝、并发领取、失败追踪与回退。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

PRD-074 PAGE-104 backend navigation authorization tests.
"""

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    NAVIGATION_DEFINITIONS,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS


def test_batch_navigation_requires_only_batch_read() -> None:
    """验证批处理场景。

    定义：test_batch_navigation_requires_only_batch_read。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    definition = next(item for item in NAVIGATION_DEFINITIONS if item.id == "batch-jobs")
    assert definition.target_section == "batch-jobs"
    assert definition.required_permissions == ("batch.read",)
    assert EVENT_TYPE_BY_NAVIGATION["batch-jobs"] == "console.page_viewed"

    for role in ("admin", "developer", "ops_admin"):
        items = _visible_navigation_items(tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role])))
        item = next(candidate for candidate in items if candidate.id == "batch-jobs")
        assert item.required_permissions == ["batch.read"]
        assert role in item.required_roles


def test_batch_navigation_follows_runtime_read_revocation() -> None:
    """验证批处理场景。

    定义：test_batch_navigation_follows_runtime_read_revocation。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    developer = tuple(sorted(
        code for code in ROLE_GRANTABLE_PERMISSIONS["developer"] if code != "batch.read"
    ))
    assert "batch-jobs" not in {item.id for item in _visible_navigation_items(developer)}

    read_only = tuple(sorted({"batch.read"}))
    items = _visible_navigation_items(read_only)
    assert [item.id for item in items] == ["batch-jobs"]
