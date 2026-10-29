"""PRD-127 Platform upgrade navigation keeps release and diagnostics permissions separate."""

from metahub_control.platform_access.service import _NAVIGATION


def test_fleet_upgrade_navigation_requires_release_read_without_fleet_diagnostics() -> None:
    by_id = {
        item_id: (permission, label, href)
        for permission, item_id, label, href in _NAVIGATION
    }

    assert by_id["edge-upgrades"] == (
        "platform.edge_release.read",
        "Fleet upgrades",
        "/platform.html#edge-upgrades",
    )
    assert by_id["edge-fleet"][0] == "platform.edge_fleet.read"
    assert len(by_id) == len(_NAVIGATION)
