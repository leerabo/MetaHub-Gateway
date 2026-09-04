"""Verified Workspace navigation permissions introduced by migration 0138."""

WORKSPACE_NAVIGATION_ROLE_PERMISSIONS = {
    role: (
        "workspace.navigation.read",
        "workspace.task_home.read",
    )
    for role in (
        "owner",
        "admin",
        "developer",
        "security_admin",
        "finance_admin",
        "legal_admin",
        "auditor",
        "viewer",
    )
}

for _advanced_role in ("owner", "admin", "developer", "security_admin", "auditor"):
    WORKSPACE_NAVIGATION_ROLE_PERMISSIONS[_advanced_role] += (
        "workspace.advanced_tool.open",
        "workspace.advanced_tool.read",
    )

WORKSPACE_NAVIGATION_PERMISSIONS = frozenset(
    permission
    for permissions in WORKSPACE_NAVIGATION_ROLE_PERMISSIONS.values()
    for permission in permissions
)

__all__ = (
    "WORKSPACE_NAVIGATION_PERMISSIONS",
    "WORKSPACE_NAVIGATION_ROLE_PERMISSIONS",
)
