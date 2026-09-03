"""Verified Workspace console-experience permissions for PRD-145."""

from __future__ import annotations

WORKSPACE_CONSOLE_ROLE_PERMISSIONS = {
    "owner": (
        "workspace.console.approval.read",
        "workspace.console.assistant.query",
        "workspace.console.overview.read",
        "workspace.deployment_diagnostic.read",
    ),
    "admin": (
        "workspace.console.approval.read",
        "workspace.console.assistant.query",
        "workspace.console.overview.read",
        "workspace.deployment_diagnostic.read",
    ),
    "developer": (
        "workspace.console.assistant.query",
        "workspace.console.overview.read",
        "workspace.deployment_diagnostic.read",
    ),
    "security_admin": (
        "workspace.console.approval.read",
        "workspace.console.assistant.query",
        "workspace.console.overview.read",
    ),
    "finance_admin": (
        "workspace.console.approval.read",
        "workspace.console.assistant.query",
        "workspace.console.overview.read",
    ),
    "legal_admin": (
        "workspace.console.approval.read",
        "workspace.console.assistant.query",
        "workspace.console.overview.read",
    ),
    "auditor": (
        "workspace.console.assistant.query",
        "workspace.console.overview.read",
        "workspace.deployment_diagnostic.audit_read",
    ),
    "viewer": ("workspace.console.overview.read",),
}

WORKSPACE_CONSOLE_PERMISSIONS = frozenset(
    permission
    for permissions in WORKSPACE_CONSOLE_ROLE_PERMISSIONS.values()
    for permission in permissions
)

__all__ = ("WORKSPACE_CONSOLE_PERMISSIONS", "WORKSPACE_CONSOLE_ROLE_PERMISSIONS")
