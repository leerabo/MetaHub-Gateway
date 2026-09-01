from __future__ import annotations

import pytest
from metahub_control.console.service import _visible_navigation_items
from metahub_control.workspace_access.service import WORKSPACE_EXTENSION_PERMISSION_CODES
from metahub_shared.runtime.errors import ApiError


def test_console_accepts_complete_current_workspace_extension_snapshot() -> None:
    extensions = tuple(sorted(WORKSPACE_EXTENSION_PERMISSION_CODES))

    _visible_navigation_items((), extension_permission_codes=extensions)

    with pytest.raises(ApiError) as unknown:
        _visible_navigation_items(
            (), extension_permission_codes=(*extensions, "workspace.unknown")
        )
    assert unknown.value.code == "rbac.matrix_integrity_failed"
