"""Gateway application assembly for local secret custody."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from metahub_shared.runtime.config import AppConfig

from .auth import (
    LocalAdminHandoffVerifier,
    LocalAdminSessionSigner,
    LocalManagementPolicy,
)
from .backups import LocalBackupSigningKeyStore, LocalBackupStore
from .keystore import LocalKeyMaterialStore, build_key_protector
from .provider_testing import LocalProviderTesterRegistry
from .routes import install_local_api_error_handler
from .store import LocalSecretStore, LocalSecretStoreError
from .support_bundles import LocalSupportBundleError, LocalSupportBundleStore


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
    material = LocalKeyMaterialStore(config.local_secret_keystore_path, protector).load_or_create()
    local_secret_store = LocalSecretStore(
        config.local_secret_database_path,
        deployment_id=deployment_id,
        keyring=material.keyring,
    )
    app.state.local_secret_store = local_secret_store
    app.state.local_admin_session_signer = LocalAdminSessionSigner(material.session_signing_key)
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
    support_bundle_secret = config.edge_support_bundle_encryption_secret
    if support_bundle_secret is None:
        raise RuntimeError("local_support_bundle.configuration_unavailable")
    configured_uploader = getattr(app.state, "local_support_bundle_uploader", None)
    uploader = configured_uploader if callable(configured_uploader) else _unavailable_uploader

    def support_bundle_inputs(
        tenant_id: str, workspace_id: str, scanner_hmac_key: bytes
    ) -> tuple[dict[str, object], tuple[str, ...]]:
        try:
            return local_secret_store.support_bundle_inputs(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                scanner_hmac_key=scanner_hmac_key,
            )
        except LocalSecretStoreError:
            raise LocalSupportBundleError("local_support_bundle.collection_failed") from None

    app.state.local_support_bundle_store = LocalSupportBundleStore(
        config.local_secret_database_path,
        output_root=config.edge_support_bundle_root,
        deployment_id=deployment_id,
        encryption_secret=support_bundle_secret.encode("utf-8"),
        input_provider=support_bundle_inputs,
        uploader=uploader,
        ttl_seconds=config.edge_support_bundle_ttl_seconds,
    )
    if config.kms_provider in {"aws", "test"}:
        backup_root = Path(config.edge_backup_root).resolve()
        backup_signing_key = LocalBackupSigningKeyStore(
            backup_root / "backup-signing-key.json", protector
        ).load_or_create()
        public_key = backup_signing_key.public_key().public_bytes_raw()
        kms_key_id = config.kms_key_id or f"test/{deployment_id}/edge-backup"
        app.state.local_backup_store = LocalBackupStore(
            backup_root / "restore-journal.sqlite3",
            output_root=backup_root / "artifacts",
            deployment_id=deployment_id,
            schema_head="0121",
            signing_key=backup_signing_key,
            signing_key_id="edge-backup-" + hashlib.sha256(public_key).hexdigest()[:16],
            kms_key_id=kms_key_id,
            kms_key_version=1,
            wrap_data_key=lambda value: protector.protect(
                value, purpose="edge-backup-data-key"
            ),
            unwrap_data_key=lambda value: protector.unprotect(
                value, purpose="edge-backup-data-key"
            ),
            domain_provider=lambda tenant_id, workspace_id: (
                local_secret_store.backup_domains(
                    tenant_id=tenant_id, workspace_id=workspace_id
                )
            ),
            restore_applier=lambda domains, **owners: (
                local_secret_store.apply_backup_domains(domains, **owners)
            ),
            customer_object_store_root=config.edge_backup_customer_object_store_root,
        )
    install_local_api_error_handler(app)


def _unavailable_uploader(*_args: object, **_kwargs: object) -> str:
    raise RuntimeError("local_support_bundle.upload_unavailable")


__all__ = ("install_local_secret_runtime",)
