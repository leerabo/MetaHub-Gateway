"""PRD-129 Platform backup navigation is isolated from tenant restore controls."""

from metahub_control.platform_access.service import _NAVIGATION


def test_gateway_backup_navigation_requires_dedicated_read_permission() -> None:
    by_id = {
        item_id: (permission, label, href)
        for permission, item_id, label, href in _NAVIGATION
    }

    assert by_id["edge-backups"] == (
        "platform.edge_backup.read",
        "Gateway backups",
        "/platform.html#edge-backups",
    )
    assert by_id["edge-fleet"][0] == "platform.edge_fleet.read"
    assert by_id["edge-licenses"][0] == "platform.edge_license.read"
    assert "platform.edge_restore.approve" not in {item[0] for item in _NAVIGATION}
    assert len(by_id) == len(_NAVIGATION)
