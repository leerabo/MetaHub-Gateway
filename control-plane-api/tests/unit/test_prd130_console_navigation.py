from __future__ import annotations

import pytest
from metahub_control.console.service import _visible_navigation_items
from metahub_shared.runtime.errors import ApiError


def test_prd130_console_accepts_exact_offline_exchange_extension_set() -> None:
    extensions = tuple(
        sorted(
            {
                "edge.deployment.read",
                "edge.offline_bundle.read",
                "edge.offline_bundle.transfer",
                "edge.offline_conflict.resolve",
            }
        )
    )

    items = _visible_navigation_items((), extension_permission_codes=extensions)
    assert "private-gateways" in {item.id for item in items}

    with pytest.raises(ApiError) as unknown:
        _visible_navigation_items(
            (), extension_permission_codes=(*extensions, "edge.offline_bundle.download")
        )
    assert unknown.value.code == "rbac.matrix_integrity_failed"
