"""Gateway application assembly for local secret custody."""

from __future__ import annotations

from typing import Any

from metahub_shared.runtime.config import AppConfig

from .auth import (
    LocalAdminHandoffVerifier,
    LocalAdminSessionSigner,
    LocalManagementPolicy,
)
from .keystore import LocalKeyMaterialStore, build_key_protector
from .provider_testing import LocalProviderTesterRegistry
from .routes import install_local_api_error_handler
from .store import LocalSecretStore


def install_local_secret_runtime(app: Any) -> None:
    """Install local custody only when the deployment-owned feature gate is enabled."""

    config = getattr(app.state, "config", None)
    if not isinstance(config, AppConfig) or not config.local_secret_management_enabled:
        return
    deployment_id = config.local_secret_deployment_id
    handoff_secret = config.local_secret_admin_handoff_secret
    if deployment_id is None or handoff_secret is None:
        raise RuntimeError("local_secret.configuration_unavailable")
    protector = build_key_protector(
        deployment_id=deployment_id,
        environment=config.environment,
        kms_provider=config.kms_provider,
        kms_key_id=config.kms_key_id,
        kms_test_master_key=config.kms_test_master_key,
    )
    material = LocalKeyMaterialStore(
        config.local_secret_keystore_path, protector
    ).load_or_create()
    app.state.local_secret_store = LocalSecretStore(
        config.local_secret_database_path,
        deployment_id=deployment_id,
        keyring=material.keyring,
    )
    app.state.local_admin_session_signer = LocalAdminSessionSigner(
        material.session_signing_key
    )
    app.state.local_admin_handoff_verifier = LocalAdminHandoffVerifier(handoff_secret)
    app.state.local_management_policy = LocalManagementPolicy.build(
        allowed_origins=tuple(config.local_management_allowed_origins),
        allowed_client_cidrs=tuple(config.local_management_allowed_client_cidrs),
        allow_loopback=config.local_management_allow_loopback,
    )
    app.state.local_provider_tester = LocalProviderTesterRegistry.from_base_urls(
        config.provider_base_urls,
        timeout_seconds=float(config.infrastructure_probe_timeout_seconds),
    )
    install_local_api_error_handler(app)


__all__ = ("install_local_secret_runtime",)
