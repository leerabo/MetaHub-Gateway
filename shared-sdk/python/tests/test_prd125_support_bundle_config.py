"""PRD-125 support-bundle encryption and ciphertext storage configuration boundary."""

from __future__ import annotations

import pytest
from metahub_shared.runtime.config import (
    DEFAULT_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET,
    ConfigError,
    load_app_config,
)

DEPLOYMENT_SECRET = "prd125-support-bundle-deployment-secret-016"


def _production_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "METAHUB_ENV": "prod",
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET": "prd125-cache-independent-secret-001",
        "METAHUB_IDENTITY_SESSION_SECRET": "prd125-identity-session-secret-002",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "prd125-identity-challenge-secret-003",
        "METAHUB_ENTERPRISE_SSO_HMAC_SECRET": (
            "prd125-enterprise-sso-hmac-independent-secret-004"
        ),
        "METAHUB_ENTERPRISE_SSO_ENCRYPTION_SECRET": (
            "prd125-enterprise-sso-encryption-independent-secret-005"
        ),
        "METAHUB_WORKSPACE_SESSION_SECRET": "prd125-workspace-session-secret-006",
        "METAHUB_PLATFORM_SESSION_SECRET": "prd125-platform-session-secret-007",
        "METAHUB_SUPPORT_SESSION_SECRET": "prd125-support-session-secret-008",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": (
            "prd125-platform-bootstrap-recovery-secret-009"
        ),
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "prd125-mfa-encryption-secret-010",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": "prd125-pii-encryption-secret-011",
        "METAHUB_TENANT_DATA_ENCRYPTION_SECRET": (
            "prd125-tenant-data-independent-secret-012"
        ),
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": (
            "prd125-provider-key-independent-secret-013"
        ),
        "METAHUB_EDGE_SNAPSHOT_SIGNING_SECRET": (
            "prd125-edge-snapshot-independent-secret-014"
        ),
        "METAHUB_INTERNAL_CONFIG_TOKEN": "prd125-internal-config-token-015",
        "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "true",
        "METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK": "false",
        "METAHUB_CONTROL_API_BASE_URL": "https://control.internal.example",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_control_never_loads_gateway_support_bundle_decryption_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "METAHUB_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET", DEPLOYMENT_SECRET
    )

    control = load_app_config("control-plane-api")
    gateway = load_app_config("gateway-data-plane")

    assert control.edge_support_bundle_encryption_secret is None
    assert gateway.edge_support_bundle_encryption_secret == DEPLOYMENT_SECRET
    assert DEPLOYMENT_SECRET not in repr(control)
    assert DEPLOYMENT_SECRET not in repr(control.model_dump())
    assert DEPLOYMENT_SECRET not in repr(gateway)
    assert DEPLOYMENT_SECRET not in repr(gateway.model_dump())
    assert DEPLOYMENT_SECRET not in repr(gateway.config_items)


def test_gateway_requires_an_independent_support_bundle_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METAHUB_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET", "")

    with pytest.raises(ConfigError) as captured:
        load_app_config("gateway-data-plane")

    assert captured.value.code == "infra.config_invalid"
    assert "support bundle" in captured.value.message.lower()


def test_production_gateway_rejects_default_support_bundle_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _production_gateway(monkeypatch)
    monkeypatch.setenv(
        "METAHUB_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET",
        DEFAULT_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET,
    )

    with pytest.raises(ConfigError) as captured:
        load_app_config("gateway-data-plane")

    assert captured.value.code == "infra.config_invalid"
    assert "support bundle" in captured.value.message.lower()
    assert DEFAULT_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET not in captured.value.message
