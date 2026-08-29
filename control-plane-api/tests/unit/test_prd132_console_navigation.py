"""PRD-132 Workspace support navigation is projected only from its verified extension."""

from metahub_control.console.service import EVENT_TYPE_BY_NAVIGATION, _visible_navigation_items


def test_customer_support_navigation_requires_exact_read_permission() -> None:
    items = _visible_navigation_items((), extension_permission_codes=("support.ticket.read",))
    item = next(value for value in items if value.id == "support-tickets")
    assert item.target_section == "support-tickets"
    assert item.required_permissions == ["support.ticket.read"]
    assert EVENT_TYPE_BY_NAVIGATION["support-tickets"] == "console.page_viewed"

    manage_only = _visible_navigation_items(
        (), extension_permission_codes=("support.ticket.manage",)
    )
    assert "support-tickets" not in {value.id for value in manage_only}
