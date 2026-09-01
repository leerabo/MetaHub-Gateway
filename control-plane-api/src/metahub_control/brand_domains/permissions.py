"""PRD-142 platform and Deployment branding permission matrices."""

from metahub_control.workspace_access.brand_permissions import (
    WORKSPACE_BRAND_PERMISSIONS,
    WORKSPACE_BRAND_ROLE_PERMISSIONS,
)

PLATFORM_BRAND_ROLE_PERMISSIONS = {
    "platform_owner": (
        "platform.brand.manage",
        "platform.brand.read",
        "platform.brand.release",
        "platform.channel_brand.manage",
        "platform.channel_brand.read",
        "platform.channel_brand.release",
    ),
    "platform_channel": (
        "platform.channel_brand.manage",
        "platform.channel_brand.read",
    ),
    "release_manager": (
        "platform.brand.read",
        "platform.brand.release",
        "platform.channel_brand.read",
        "platform.channel_brand.release",
    ),
}

PLATFORM_BRAND_PERMISSIONS = frozenset(
    permission
    for permissions in PLATFORM_BRAND_ROLE_PERMISSIONS.values()
    for permission in permissions
)
__all__ = (
    "PLATFORM_BRAND_PERMISSIONS",
    "PLATFORM_BRAND_ROLE_PERMISSIONS",
    "WORKSPACE_BRAND_PERMISSIONS",
    "WORKSPACE_BRAND_ROLE_PERMISSIONS",
)
