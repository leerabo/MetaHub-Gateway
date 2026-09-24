"""PRD-089 PAGE-121 backend navigation authorization tests."""

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    NAVIGATION_DEFINITIONS,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_CODES


def test_tenant_data_navigation_requires_backend_read_permission() -> None:
    definition = next(item for item in NAVIGATION_DEFINITIONS if item.id == "tenant-data-lifecycle")

    assert definition.group == "租户治理"
    assert definition.target_section == "tenant-data-lifecycle"
    assert definition.required_permissions == ("tenant.settings.read",)
    assert EVENT_TYPE_BY_NAVIGATION["tenant-data-lifecycle"] == "console.page_viewed"


def test_tenant_data_navigation_disappears_after_runtime_revocation() -> None:
    role_matrix = {role: frozenset() for role in ROLE_CODES}
    role_matrix["admin"] = frozenset({"tenant.settings.read"})

    visible = _visible_navigation_items(("tenant.settings.read",), role_permissions_by_role=role_matrix)
    item = next(candidate for candidate in visible if candidate.id == "tenant-data-lifecycle")
    assert item.required_permissions == ["tenant.settings.read"]
    assert item.required_roles == ["admin"]

    revoked = _visible_navigation_items((), role_permissions_by_role=role_matrix)
    assert "tenant-data-lifecycle" not in {candidate.id for candidate in revoked}
