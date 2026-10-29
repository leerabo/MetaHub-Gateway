"""PRD-128 Platform license navigation is isolated from Fleet operations."""

from metahub_control.platform_access.service import _NAVIGATION


def test_gateway_license_navigation_requires_dedicated_read_permission() -> None:
    by_id = {
        item_id: (permission, label, href)
        for permission, item_id, label, href in _NAVIGATION
    }

    assert by_id["edge-licenses"] == (
        "platform.edge_license.read",
        "Gateway licenses",
        "/platform.html#edge-licenses",
    )
    assert by_id["edge-fleet"][0] == "platform.edge_fleet.read"
    assert by_id["edge-upgrades"][0] == "platform.edge_release.read"
    assert len(by_id) == len(_NAVIGATION)
