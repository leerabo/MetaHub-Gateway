"""PRD-141 Platform Growth permissions and response scopes."""

from __future__ import annotations

from typing import Literal

from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import PlatformContext

READ = "platform.growth.read"
ADOPTION_READ = "platform.growth.adoption_read"
CHANNEL_ATTRIBUTION_READ = "platform.growth.channel_attribution_read"
CAMPAIGN_MANAGE = "platform.growth.campaign.manage"
EXPERIMENT_MANAGE = "platform.growth.experiment.manage"
EXPORT_REQUEST = "platform.growth.export.request"
EXPORT_APPROVE = "platform.growth.export.approve"
EXPORT_DOWNLOAD = "platform.growth.export.download"

ReadScope = Literal["growth", "channel", "customer_success"]


def read_scope(context: PlatformContext) -> ReadScope:
    permissions = set(context.permissions)
    if READ in permissions:
        return "growth"
    if CHANNEL_ATTRIBUTION_READ in permissions:
        return "channel"
    if ADOPTION_READ in permissions:
        return "customer_success"
    raise ApiError("platform.permission_denied")


def require(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


__all__ = [
    "ADOPTION_READ",
    "CAMPAIGN_MANAGE",
    "CHANNEL_ATTRIBUTION_READ",
    "EXPERIMENT_MANAGE",
    "EXPORT_APPROVE",
    "EXPORT_DOWNLOAD",
    "EXPORT_REQUEST",
    "READ",
    "read_scope",
    "require",
]
