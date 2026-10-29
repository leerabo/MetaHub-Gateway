"""验证 PAGE-105 导航始终服从后端实时实验读取权限。"""

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    NAVIGATION_DEFINITIONS,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS


def test_experiment_navigation_requires_only_experiment_read() -> None:
    """模型评测入口位于模型评分之后，并只声明后端 experiment.read 依赖。"""

    definition = next(item for item in NAVIGATION_DEFINITIONS if item.id == "experiments")
    score = next(
        item for item in NAVIGATION_DEFINITIONS if item.id == "model-quality-scores"
    )
    assert definition.target_section == "experiments"
    assert definition.group == "配置"
    assert definition.order > score.order
    assert definition.required_permissions == ("experiment.read",)
    assert EVENT_TYPE_BY_NAVIGATION["experiments"] == "console.page_viewed"

    for role in ("admin", "developer", "ops_admin"):
        items = _visible_navigation_items(tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role])))
        item = next(candidate for candidate in items if candidate.id == "experiments")
        assert item.required_permissions == ["experiment.read"]
        assert role in item.required_roles


def test_experiment_navigation_follows_runtime_read_revocation() -> None:
    """撤销 experiment.read 后后端不再下发入口，单独授予时只暴露该页面。"""

    narrowed = tuple(
        sorted(
            code
            for code in ROLE_GRANTABLE_PERMISSIONS["developer"]
            if code != "experiment.read"
        )
    )
    assert "experiments" not in {
        item.id for item in _visible_navigation_items(narrowed)
    }

    items = _visible_navigation_items(("experiment.read",))
    assert [item.id for item in items] == ["experiments"]
