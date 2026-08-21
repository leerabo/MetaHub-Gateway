"""PRD-094 WorkspaceSession secret isolation and configuration privacy."""

from __future__ import annotations

import pytest
from metahub_shared.runtime.config import (
    DEFAULT_WORKSPACE_SESSION_SECRET,
    ConfigError,
    config_loaded_log_payload,
    load_app_config,
)

WORKSPACE_SECRET = "prd094-independent-workspace-session-secret-0123456789abcdef"


def test_workspace_session_secret_is_runtime_only_and_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_WORKSPACE_SESSION_SECRET", WORKSPACE_SECRET)

    config = load_app_config("control-plane-api")
    assert config.workspace_session_secret == WORKSPACE_SECRET
    assert config.workspace_session_ttl_seconds == 7200
    assert "workspace_session_secret" not in config.model_dump()
    assert WORKSPACE_SECRET not in repr(config)
    payload = config_loaded_log_payload(config)
    assert WORKSPACE_SECRET not in repr(payload)
    item = next(
        entry
        for entry in payload["items"]
        if entry["key"] == "METAHUB_WORKSPACE_SESSION_SECRET"
    )
    assert item["status"] == "masked"


@pytest.mark.parametrize(
    "reused_key",
    [
        "METAHUB_AUTH_TOKEN_SECRET",
        "METAHUB_IDENTITY_SESSION_SECRET",
        "METAHUB_IDENTITY_CHALLENGE_SECRET",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET",
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
    ],
)
def test_workspace_session_secret_reuse_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    reused_key: str,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_WORKSPACE_SESSION_SECRET", WORKSPACE_SECRET)
    monkeypatch.setenv(reused_key, WORKSPACE_SECRET)

    with pytest.raises(ConfigError) as exc:
        load_app_config("control-plane-api")

    assert exc.value.code == "infra.config_invalid"
    assert "independent" in exc.value.message


def test_production_control_plane_rejects_default_workspace_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_WORKSPACE_SESSION_SECRET", DEFAULT_WORKSPACE_SESSION_SECRET)
    monkeypatch.setenv(
        "METAHUB_IDENTITY_SESSION_SECRET",
        "prod-independent-identity-session-secret-0123456789abcdef",
    )
    monkeypatch.setenv(
        "METAHUB_IDENTITY_CHALLENGE_SECRET",
        "prod-independent-identity-challenge-secret-0123456789abcdef",
    )
    monkeypatch.setenv(
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET",
        "prod-independent-identity-pii-secret-0123456789abcdef",
    )
    monkeypatch.setenv(
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
        "prod-independent-cache-affinity-secret-0123456789abcdef",
    )
    monkeypatch.setenv(
        "METAHUB_AUTH_TOKEN_SECRET",
        "prod-independent-auth-token-secret-0123456789abcdef",
    )

    with pytest.raises(ConfigError) as exc:
        load_app_config("control-plane-api")

    assert exc.value.code == "infra.config_invalid"
    assert exc.value.message == (
        "METAHUB_WORKSPACE_SESSION_SECRET is not an independent production secret."
    )
