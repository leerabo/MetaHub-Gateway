"""Frozen Platform and Workspace commercial accounting matrices for PRD-144."""

from metahub_control.workspace_access.commercial_permissions import (
    WORKSPACE_COMMERCIAL_PERMISSIONS,
    WORKSPACE_COMMERCIAL_ROLE_PERMISSIONS,
)

PLATFORM_COMMERCIAL_ROLE_PERMISSIONS = {
    "platform_owner": (
        "platform.channel.statement.read",
        "platform.commercial.governance.approve",
        "platform.commercial.invoice.read",
        "platform.commercial.plan.read",
        "platform.commercial.subscription.read",
    ),
    "platform_billing": (
        "platform.channel.statement.read",
        "platform.commercial.dispute.decide",
        "platform.commercial.invoice.read",
        "platform.commercial.plan.manage",
        "platform.commercial.plan.read",
        "platform.commercial.reconciliation.manage",
        "platform.commercial.subscription.read",
    ),
    "platform_channel": ("platform.channel.statement.read",),
}

PLATFORM_COMMERCIAL_PERMISSIONS = frozenset(
    permission
    for permissions in PLATFORM_COMMERCIAL_ROLE_PERMISSIONS.values()
    for permission in permissions
)

__all__ = (
    "PLATFORM_COMMERCIAL_PERMISSIONS",
    "PLATFORM_COMMERCIAL_ROLE_PERMISSIONS",
    "WORKSPACE_COMMERCIAL_PERMISSIONS",
    "WORKSPACE_COMMERCIAL_ROLE_PERMISSIONS",
)
