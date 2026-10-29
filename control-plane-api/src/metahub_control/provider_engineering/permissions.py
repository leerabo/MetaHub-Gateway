"""Frozen PRD-143 Platform and Workspace permission matrices."""

from metahub_control.workspace_access.provider_engineering_permissions import (
    WORKSPACE_PROVIDER_ENGINEERING_PERMISSIONS,
    WORKSPACE_PROVIDER_ENGINEERING_ROLE_PERMISSIONS,
)

PLATFORM_PROVIDER_ENGINEERING_ROLE_PERMISSIONS = {
    "platform_provider_engineer": (
        "platform.error_catalog.candidate_manage",
        "platform.error_catalog.read",
        "platform.provider_adapter.candidate_manage",
        "platform.provider_adapter.contract_run",
        "platform.provider_adapter.failure_review",
        "platform.provider_adapter.read",
    ),
    "platform_security": (
        "platform.error_catalog.read",
        "platform.error_catalog.security_approve",
        "platform.provider_adapter.failure_review",
        "platform.provider_adapter.read",
    ),
    "release_manager": (
        "platform.error_catalog.read",
        "platform.error_catalog.release",
        "platform.provider_adapter.read",
        "platform.provider_adapter.release",
    ),
    "platform_owner": (
        "platform.error_catalog.read",
        "platform.provider_adapter.failure_review",
        "platform.provider_adapter.read",
    ),
}

PLATFORM_PROVIDER_ENGINEERING_PERMISSIONS = frozenset(
    permission
    for permissions in PLATFORM_PROVIDER_ENGINEERING_ROLE_PERMISSIONS.values()
    for permission in permissions
)

__all__ = (
    "PLATFORM_PROVIDER_ENGINEERING_PERMISSIONS",
    "PLATFORM_PROVIDER_ENGINEERING_ROLE_PERMISSIONS",
    "WORKSPACE_PROVIDER_ENGINEERING_PERMISSIONS",
    "WORKSPACE_PROVIDER_ENGINEERING_ROLE_PERMISSIONS",
)
