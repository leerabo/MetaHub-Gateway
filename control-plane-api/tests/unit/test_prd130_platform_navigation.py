"""PRD-130 Platform offline-exchange navigation has a dedicated read grant."""

from metahub_control.platform_access.service import _NAVIGATION


def test_offline_exchange_navigation_requires_dedicated_read_permission() -> None:
    by_id = {
        item_id: (permission, label, href)
        for permission, item_id, label, href in _NAVIGATION
    }

    assert by_id["offline-bundles"] == (
        "platform.edge_offline_bundle.read",
        "Offline exchange",
        "/platform.html#offline-bundles",
    )
    assert by_id["edge-backups"][0] == "platform.edge_backup.read"
    assert "platform.edge_offline_bundle.manage" not in {item[0] for item in _NAVIGATION}
    assert len(by_id) == len(_NAVIGATION)
