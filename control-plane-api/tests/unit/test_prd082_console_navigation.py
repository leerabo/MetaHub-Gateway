"""PRD-082 PAGE-114 backend navigation authorization tests."""

from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    NAVIGATION_DEFINITIONS,
    _visible_navigation_items,
)
from metahub_shared.rbac import ROLE_CODES


def test_mcp_navigation_requires_backend_mcp_read() -> None:
    definition = next(item for item in NAVIGATION_DEFINITIONS if item.id == "mcp-tools")

    assert definition.group == "开发者"
    assert definition.target_section == "mcp-tools"
    assert definition.required_permissions == ("mcp.read",)
    assert EVENT_TYPE_BY_NAVIGATION["mcp-tools"] == "console.page_viewed"


def test_mcp_navigation_disappears_after_runtime_read_revocation() -> None:
    role_matrix = {role: frozenset() for role in ROLE_CODES}
    role_matrix["admin"] = frozenset({"mcp.read"})

    visible = _visible_navigation_items(
        ("mcp.read",),
        role_permissions_by_role=role_matrix,
    )
    item = next(candidate for candidate in visible if candidate.id == "mcp-tools")
    assert item.required_permissions == ["mcp.read"]
    assert item.required_roles == ["admin"]

    revoked = _visible_navigation_items((), role_permissions_by_role=role_matrix)
    assert "mcp-tools" not in {candidate.id for candidate in revoked}
