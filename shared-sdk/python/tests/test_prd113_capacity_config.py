"""PRD-113 isolated capacity runner configuration contracts."""

from __future__ import annotations

import pytest
from metahub_shared.runtime.config import ConfigError, load_app_config

TOKEN = "prd113-runner-token-independent-0123456789abcdef"
PERMIT = "prd113-runner-permit-independent-0123456789abcdef"


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_MODE", "isolated_http")
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_BASE_URL", "http://capacity-runner:8012")
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_PERMIT_SECRET", PERMIT)


def test_capacity_runner_config_is_excluded_and_summary_is_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)

    config = load_app_config("control-plane-api")
    serialized = repr(config) + repr(config.model_dump())
    assert TOKEN not in serialized
    assert PERMIT not in serialized
    summaries = {item.key: item for item in config.config_items}
    assert summaries["METAHUB_CAPACITY_RUNNER_MODE"].value_preview == "isolated_http"
    assert summaries["METAHUB_CAPACITY_RUNNER_BASE_URL"].status == "configured"
    assert summaries["METAHUB_CAPACITY_RUNNER_INTERNAL_TOKEN"].status == "masked"
    assert summaries["METAHUB_CAPACITY_RUNNER_PERMIT_SECRET"].status == "masked"
    assert TOKEN not in repr(summaries)
    assert PERMIT not in repr(summaries)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("METAHUB_CAPACITY_RUNNER_BASE_URL", "http://capacity-runner:8012/path"),
        ("METAHUB_CAPACITY_RUNNER_INTERNAL_TOKEN", "short"),
        ("METAHUB_CAPACITY_RUNNER_PERMIT_SECRET", "short"),
    ],
)
def test_isolated_capacity_runner_rejects_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv(key, value)

    with pytest.raises(ConfigError) as captured:
        load_app_config("control-plane-api")
    assert captured.value.code == "infra.config_invalid"
    assert value not in captured.value.message


def test_capacity_runner_credentials_must_be_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_PERMIT_SECRET", TOKEN)

    with pytest.raises(ConfigError) as captured:
        load_app_config("control-plane-api")
    assert captured.value.code == "infra.config_invalid"
    assert TOKEN not in captured.value.message

    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_PERMIT_SECRET", PERMIT)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", TOKEN)
    with pytest.raises(ConfigError) as reused_domain:
        load_app_config("control-plane-api")
    assert reused_domain.value.code == "infra.config_invalid"
    assert TOKEN not in reused_domain.value.message


def test_loopback_capacity_runner_is_test_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_CAPACITY_RUNNER_MODE", "test_loopback")

    with pytest.raises(ConfigError) as captured:
        load_app_config("control-plane-api")
    assert captured.value.code == "infra.config_invalid"
