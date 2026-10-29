"""PRD-114 enterprise SSO crypto and configuration contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest
from metahub_shared.enterprise_identity import (
    EnterpriseSsoCryptoError,
    EnterpriseSsoSecretOwner,
    decrypt_enterprise_sso_secret,
    encrypt_enterprise_sso_secret,
    enterprise_sso_hmac,
)
from metahub_shared.runtime.config import ConfigError, load_app_config

HMAC_SECRET = "test-prd114-enterprise-hmac-secret-independent-2026"
ENCRYPTION_SECRET = "test-prd114-enterprise-encryption-secret-independent-2026"
OTHER_SECRET = "test-prd114-unrelated-file-secret-independent-2026"
WORKSPACE_ID = "wsp_" + "1" * 32
TENANT_ID = "tenant_prd114"
CONNECTION_ID = "ssc_" + "2" * 32


def _owner() -> EnterpriseSsoSecretOwner:
    return EnterpriseSsoSecretOwner(
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        resource_id=CONNECTION_ID,
        purpose="oidc_client_secret",
    )


def test_ciphertext_is_randomized_and_bound_to_exact_owner() -> None:
    owner = _owner()
    first = encrypt_enterprise_sso_secret(
        "synthetic-client-secret", secret=ENCRYPTION_SECRET, owner=owner
    )
    second = encrypt_enterprise_sso_secret(
        "synthetic-client-secret", secret=ENCRYPTION_SECRET, owner=owner
    )

    assert first != second
    assert "synthetic-client-secret" not in first
    assert (
        decrypt_enterprise_sso_secret(first, secret=ENCRYPTION_SECRET, owner=owner)
        == "synthetic-client-secret"
    )
    for changed_owner in (
        replace(owner, workspace_id="wsp_" + "3" * 32),
        replace(owner, tenant_id="tenant_prd114_other"),
        replace(owner, resource_id="ssc_" + "4" * 32),
    ):
        with pytest.raises(EnterpriseSsoCryptoError, match="sso.unavailable"):
            decrypt_enterprise_sso_secret(
                first, secret=ENCRYPTION_SECRET, owner=changed_owner
            )

    parts = first.split(":")
    parts[-1] = parts[-1][:-2] + ("AA" if parts[-1][-2:] != "AA" else "BB")
    with pytest.raises(EnterpriseSsoCryptoError, match="sso.unavailable"):
        decrypt_enterprise_sso_secret(
            ":".join(parts), secret=ENCRYPTION_SECRET, owner=owner
        )


def test_hmac_is_stable_and_purpose_separated() -> None:
    domain = enterprise_sso_hmac(HMAC_SECRET, "domain", "example.com")
    assert domain == enterprise_sso_hmac(HMAC_SECRET, "domain", "example.com")
    assert domain != enterprise_sso_hmac(HMAC_SECRET, "verified_email", "example.com")
    assert len(domain) == 64

    for secret, purpose, value in (
        ("short", "domain", "example.com"),
        (HMAC_SECRET, "Invalid Purpose", "example.com"),
        (HMAC_SECRET, "domain", ""),
    ):
        with pytest.raises(EnterpriseSsoCryptoError, match="sso.unavailable"):
            enterprise_sso_hmac(secret, purpose, value)


def test_config_masks_enterprise_secrets_and_rejects_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_ENTERPRISE_SSO_HMAC_SECRET", HMAC_SECRET)
    monkeypatch.setenv("METAHUB_ENTERPRISE_SSO_ENCRYPTION_SECRET", ENCRYPTION_SECRET)
    config = load_app_config("control-plane-api")

    rendered = repr(config) + repr(config.model_dump())
    assert HMAC_SECRET not in rendered
    assert ENCRYPTION_SECRET not in rendered
    items = {item.key: item for item in config.config_items}
    assert items["METAHUB_ENTERPRISE_SSO_HMAC_SECRET"].status == "masked"
    assert items["METAHUB_ENTERPRISE_SSO_ENCRYPTION_SECRET"].status == "masked"

    monkeypatch.setenv("METAHUB_FILE_ENCRYPTION_SECRET", HMAC_SECRET)
    with pytest.raises(ConfigError) as captured:
        load_app_config("control-plane-api")
    assert captured.value.code == "infra.config_invalid"
    assert HMAC_SECRET not in str(captured.value)


def test_production_rejects_default_enterprise_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METAHUB_ENV", "prod")
    for index, key in enumerate(
        (
            "METAHUB_AUTH_TOKEN_SECRET",
            "METAHUB_IDENTITY_SESSION_SECRET",
            "METAHUB_IDENTITY_CHALLENGE_SECRET",
            "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET",
            "METAHUB_WORKSPACE_SESSION_SECRET",
            "METAHUB_PLATFORM_SESSION_SECRET",
            "METAHUB_SUPPORT_SESSION_SECRET",
            "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET",
            "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET",
            "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
        ),
        start=1,
    ):
        monkeypatch.setenv(key, f"test-prd114-production-domain-{index:02d}-" + "x" * 32)
    with pytest.raises(ConfigError) as captured:
        load_app_config("control-plane-api")
    assert captured.value.code == "infra.config_invalid"
    assert "Enterprise SSO" in captured.value.message
