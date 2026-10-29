"""PRD-144 Workspace commercial navigation is permission-projected, not role-inferred."""

from metahub_control.console.service import EVENT_TYPE_BY_NAVIGATION, _visible_navigation_items
from metahub_control.workspace_access.commercial_permissions import (
    WORKSPACE_COMMERCIAL_ROLE_PERMISSIONS,
)


def test_commercial_navigation_requires_exact_verified_extensions() -> None:
    assert {item.id for item in _visible_navigation_items(())}.isdisjoint(
        {"workspace-subscription", "internal-finops"}
    )

    subscription_only = _visible_navigation_items(
        (), extension_permission_codes=("workspace.commercial.subscription.read",)
    )
    subscription = next(
        item for item in subscription_only if item.id == "workspace-subscription"
    )
    assert subscription.target_section == "workspace-subscription"
    assert subscription.required_permissions == [
        "workspace.commercial.subscription.read"
    ]
    assert "internal-finops" not in {item.id for item in subscription_only}

    incomplete_finops = _visible_navigation_items(
        (), extension_permission_codes=("workspace.internal_finops.read",)
    )
    assert "internal-finops" not in {item.id for item in incomplete_finops}

    complete_finops = _visible_navigation_items(
        (),
        extension_permission_codes=(
            "workspace.internal_finops.read",
            "workspace.provider_cost.read",
        ),
    )
    internal_finops = next(item for item in complete_finops if item.id == "internal-finops")
    assert internal_finops.target_section == "internal-finops"
    assert internal_finops.required_permissions == [
        "workspace.internal_finops.read",
        "workspace.provider_cost.read",
    ]


def test_only_fixed_finance_roles_receive_complete_commercial_navigation() -> None:
    assert set(WORKSPACE_COMMERCIAL_ROLE_PERMISSIONS) == {"owner", "finance_admin"}
    for role_code, permissions in WORKSPACE_COMMERCIAL_ROLE_PERMISSIONS.items():
        items = _visible_navigation_items((), extension_permission_codes=permissions)
        projected = {item.id for item in items}
        assert {"workspace-subscription", "internal-finops"} <= projected, role_code

    assert EVENT_TYPE_BY_NAVIGATION["workspace-subscription"] == "console.page_viewed"
    assert EVENT_TYPE_BY_NAVIGATION["internal-finops"] == "console.page_viewed"
