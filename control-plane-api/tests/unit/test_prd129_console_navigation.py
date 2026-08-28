from __future__ import annotations

import pytest
from metahub_control.console.service import _visible_navigation_items
from metahub_shared.runtime.errors import ApiError


def test_prd129_console_accepts_complete_verified_edge_extension_set() -> None:
    extensions = tuple(
        sorted(
            {
                "edge.backup.create",
                "edge.backup.read",
                "edge.delivery.accept",
                "edge.deployment.manage",
                "edge.deployment.read",
                "edge.diagnostic.approve",
                "edge.diagnostic.read",
                "edge.drill.manage",
                "edge.health.read",
                "edge.installation.manage",
                "edge.installer.read",
                "edge.license.import",
                "edge.license.read",
                "edge.license.refresh",
                "edge.restore.approve",
                "edge.rollback.manage",
                "edge.secret_reference.read",
                "edge.support_bundle.read",
                "edge.telemetry.manage",
                "edge.telemetry.read",
                "edge.upgrade.manage",
                "edge.upgrade.read",
                "edge.usage.read",
                "edge.validation.manage",
                "edge.validation.read",
            }
        )
    )

    items = _visible_navigation_items((), extension_permission_codes=extensions)
    assert "private-gateways" in {item.id for item in items}

    with pytest.raises(ApiError) as unknown:
        _visible_navigation_items(
            (), extension_permission_codes=(*extensions, "edge.backup.download")
        )
    assert unknown.value.code == "rbac.matrix_integrity_failed"
