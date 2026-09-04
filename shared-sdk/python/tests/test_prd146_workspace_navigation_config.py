"""PRD-146 Workspace navigation scheduler configuration contracts."""

from __future__ import annotations

import pytest
from metahub_shared.runtime.config import ConfigError, load_app_config

SETTING = "METAHUB_WORKSPACE_NAVIGATION_SCHEDULER_ENABLED"


def test_workspace_navigation_scheduler_is_disabled_by_default_and_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SETTING, raising=False)
    disabled = load_app_config("control-plane-api")
    assert disabled.workspace_navigation_scheduler_enabled is False

    monkeypatch.setenv(SETTING, "true")
    enabled = load_app_config("control-plane-api")
    assert enabled.workspace_navigation_scheduler_enabled is True
    summary = {item.key: item for item in enabled.config_items}[SETTING]
    assert summary.status == "configured"
    assert summary.value_preview == "true"


def test_workspace_navigation_scheduler_rejects_non_boolean_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SETTING, "sometimes")
    with pytest.raises(ConfigError) as captured:
        load_app_config("control-plane-api")
    assert captured.value.code == "infra.config_invalid"
    assert "sometimes" not in captured.value.message
