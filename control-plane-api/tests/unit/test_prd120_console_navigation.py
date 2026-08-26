"""PRD-120 Console navigation projection for verified Edge permission extensions."""

import pytest
from metahub_control.console.service import (
    EVENT_TYPE_BY_NAVIGATION,
    _visible_navigation_items,
)
from metahub_shared.runtime.errors import ApiError


def test_private_gateway_navigation_requires_verified_edge_read_extension() -> None:
    items = _visible_navigation_items(
        (), extension_permission_codes=("edge.deployment.read",)
    )
    item = next(value for value in items if value.id == "private-gateways")
    assert item.target_section == "private-gateways"
    assert item.required_permissions == ["edge.deployment.read"]
    assert EVENT_TYPE_BY_NAVIGATION["private-gateways"] == "console.page_viewed"

    manage_only = _visible_navigation_items(
        (), extension_permission_codes=("edge.deployment.manage",)
    )
    assert "private-gateways" not in {value.id for value in manage_only}


def test_private_gateway_navigation_accepts_complete_workspace_extension_snapshot() -> None:
    items = _visible_navigation_items(
        (),
        extension_permission_codes=(
            "data_governance.manage",
            "data_governance.read",
            "edge.deployment.manage",
            "edge.deployment.read",
            "legal_hold.manage",
            "legal_hold.read",
            "machine_identity.manage",
            "machine_identity.read",
            "provider_catalog.read",
        ),
    )

    assert "private-gateways" in {value.id for value in items}


def test_unknown_or_noncanonical_navigation_extension_fails_closed() -> None:
    with pytest.raises(ApiError) as unknown:
        _visible_navigation_items(
            (), extension_permission_codes=("edge.deployment.read", "edge.unknown")
        )
    assert unknown.value.code == "rbac.matrix_integrity_failed"
    with pytest.raises(ApiError) as noncanonical:
        _visible_navigation_items(
            (),
            extension_permission_codes=(
                "edge.deployment.read",
                "edge.deployment.manage",
            ),
        )
    assert noncanonical.value.code == "rbac.matrix_integrity_failed"
