"""Frozen Platform and Workspace console-experience matrices for PRD-145."""

from metahub_control.workspace_access.console_permissions import (
    WORKSPACE_CONSOLE_PERMISSIONS,
    WORKSPACE_CONSOLE_ROLE_PERMISSIONS,
)

PLATFORM_CONSOLE_ROLE_PERMISSIONS = {
    "platform_owner": (
        "platform.console.approval.read",
        "platform.console.assistant.query",
        "platform.console.overview.read",
    ),
    "platform_ops": (
        "platform.console.assistant.query",
        "platform.console.overview.read",
    ),
    "platform_security": (
        "platform.console.approval.read",
        "platform.console.assistant.query",
        "platform.console.overview.read",
    ),
    "platform_billing": (
        "platform.console.approval.read",
        "platform.console.assistant.query",
        "platform.console.overview.read",
    ),
    "platform_support": ("platform.console.overview.read",),
    "platform_compliance": ("platform.console.approval.read",),
    "release_manager": ("platform.console.approval.read",),
}

PLATFORM_CONSOLE_PERMISSIONS = frozenset(
    permission
    for permissions in PLATFORM_CONSOLE_ROLE_PERMISSIONS.values()
    for permission in permissions
)

__all__ = (
    "PLATFORM_CONSOLE_PERMISSIONS",
    "PLATFORM_CONSOLE_ROLE_PERMISSIONS",
    "WORKSPACE_CONSOLE_PERMISSIONS",
    "WORKSPACE_CONSOLE_ROLE_PERMISSIONS",
)
