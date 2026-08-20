"""PRD-092 identity challenge, PII encryption, and delivery startup contracts."""

from __future__ import annotations

import pytest
from metahub_shared.runtime.config import ConfigError, load_app_config

SESSION_SECRET = "prd092-independent-session-secret-0123456789abcdef"
CHALLENGE_SECRET = "prd092-independent-challenge-secret-0123456789abcdef"
AUTH_SECRET = "prd092-independent-auth-secret-0123456789abcdef"
PII_SECRET = "prd092-independent-pii-encryption-secret-0123456789abcdef"


def test_identity_challenge_config_is_private_and_available_only_outside_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_IDENTITY_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("METAHUB_IDENTITY_CHALLENGE_SECRET", CHALLENGE_SECRET)
    monkeypatch.setenv("METAHUB_IDENTITY_PII_ENCRYPTION_SECRET", PII_SECRET)
    monkeypatch.setenv("METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE", "test_capture")

    config = load_app_config("control-plane-api")
    assert config.identity_challenge_delivery_mode == "test_capture"
    assert CHALLENGE_SECRET not in repr(config)
    assert CHALLENGE_SECRET not in config.model_dump_json()
    assert PII_SECRET not in repr(config)
    assert PII_SECRET not in config.model_dump_json()


def test_prod_rejects_default_identity_challenge_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_IDENTITY_SESSION_SECRET", SESSION_SECRET)

    with pytest.raises(ConfigError) as exc:
        load_app_config("control-plane-api")

    assert exc.value.code == "infra.config_invalid"
    assert "METAHUB_IDENTITY_CHALLENGE_SECRET" in exc.value.message


def test_prod_rejects_test_capture_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_IDENTITY_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("METAHUB_IDENTITY_CHALLENGE_SECRET", CHALLENGE_SECRET)
    monkeypatch.setenv("METAHUB_IDENTITY_PII_ENCRYPTION_SECRET", PII_SECRET)
    monkeypatch.setenv("METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE", "test_capture")

    with pytest.raises(ConfigError) as exc:
        load_app_config("control-plane-api")

    assert exc.value.code == "infra.config_invalid"
    assert "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE" in exc.value.message


@pytest.mark.parametrize(
    ("reused_secret", "reuse_auth"),
    [(SESSION_SECRET, False), (AUTH_SECRET, True)],
)
def test_identity_challenge_secret_must_be_independent(
    monkeypatch: pytest.MonkeyPatch,
    reused_secret: str,
    reuse_auth: bool,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_IDENTITY_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("METAHUB_IDENTITY_CHALLENGE_SECRET", reused_secret)
    monkeypatch.setenv("METAHUB_IDENTITY_PII_ENCRYPTION_SECRET", PII_SECRET)
    if reuse_auth:
        monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)

    with pytest.raises(ConfigError) as exc:
        load_app_config("control-plane-api")

    assert exc.value.code == "infra.config_invalid"
    assert "METAHUB_IDENTITY_CHALLENGE_SECRET" in exc.value.message


def test_prod_rejects_default_identity_pii_encryption_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_IDENTITY_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("METAHUB_IDENTITY_CHALLENGE_SECRET", CHALLENGE_SECRET)

    with pytest.raises(ConfigError) as exc:
        load_app_config("control-plane-api")

    assert exc.value.code == "infra.config_invalid"
    assert "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET" in exc.value.message


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "METAHUB_AUTH_TOKEN_SECRET",
        "METAHUB_IDENTITY_SESSION_SECRET",
        "METAHUB_IDENTITY_CHALLENGE_SECRET",
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
        "METAHUB_FILE_ENCRYPTION_SECRET",
    ],
)
def test_identity_pii_encryption_secret_must_be_independent(
    monkeypatch: pytest.MonkeyPatch,
    forbidden_key: str,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_IDENTITY_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("METAHUB_IDENTITY_CHALLENGE_SECRET", CHALLENGE_SECRET)
    monkeypatch.setenv("METAHUB_IDENTITY_PII_ENCRYPTION_SECRET", PII_SECRET)
    monkeypatch.setenv(forbidden_key, PII_SECRET)

    with pytest.raises(ConfigError) as exc:
        load_app_config("control-plane-api")

    assert exc.value.code == "infra.config_invalid"
    assert "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET" in exc.value.message
