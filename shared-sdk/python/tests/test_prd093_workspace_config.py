"""PRD-093 Workspace Region and residency configuration contracts."""

from __future__ import annotations

import pytest
from metahub_shared.runtime.config import ConfigError, load_app_config


def test_workspace_region_configuration_is_canonical_and_runtime_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv(
        "METAHUB_WORKSPACE_ALLOWED_REGIONS",
        "cn-east-1, cn-north-1,cn-east-1",
    )
    monkeypatch.setenv("METAHUB_WORKSPACE_RESIDENCY_POLICY_VERSION", "cn-residency-v2")

    config = load_app_config("control-plane-api")
    assert config.workspace_allowed_regions == ("cn-east-1", "cn-north-1")
    assert config.workspace_residency_policy_version == "cn-residency-v2"
    dumped = config.model_dump()
    assert dumped["workspace_allowed_regions"] == ("cn-east-1", "cn-north-1")
    assert dumped["workspace_residency_policy_version"] == "cn-residency-v2"


def test_workspace_region_configuration_has_closed_domestic_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("METAHUB_WORKSPACE_ALLOWED_REGIONS", raising=False)
    monkeypatch.delenv("METAHUB_WORKSPACE_RESIDENCY_POLICY_VERSION", raising=False)

    config = load_app_config("control-plane-api")
    assert config.workspace_allowed_regions == ("cn-north-1", "cn-east-1")
    assert config.workspace_residency_policy_version == "cn-residency-v1"


@pytest.mark.parametrize(
    "regions",
    [
        "",
        "us-east-1",
        "CN-NORTH-1",
        "cn-north-1,cn-east-1,cn-south-1,cn-west-1,cn-a-1,cn-b-1,cn-c-1,cn-d-1,cn-e-1",
        "cn-north_1",
        f"cn-{'a' * 38}",
    ],
)
def test_workspace_region_configuration_rejects_unsafe_allowlists(
    monkeypatch: pytest.MonkeyPatch,
    regions: str,
) -> None:
    monkeypatch.setenv("METAHUB_WORKSPACE_ALLOWED_REGIONS", regions)

    with pytest.raises(ConfigError) as exc:
        load_app_config("control-plane-api")

    assert exc.value.code == "infra.config_invalid"
    assert exc.value.message == "Workspace Region allowlist is invalid."


@pytest.mark.parametrize("version", ["", "cn residency v2", "cn/v2", "v" * 81])
def test_workspace_residency_policy_version_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    monkeypatch.setenv("METAHUB_WORKSPACE_RESIDENCY_POLICY_VERSION", version)

    with pytest.raises(ConfigError) as exc:
        load_app_config("control-plane-api")

    assert exc.value.code == "infra.config_invalid"
    assert exc.value.message == "Workspace residency policy version is invalid."
