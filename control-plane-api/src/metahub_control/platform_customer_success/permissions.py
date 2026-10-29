"""PRD-140 permission capabilities and exact response scopes."""

from __future__ import annotations

from typing import Literal

from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import PlatformContext

DIRECTORY = "platform.customer_success.read"
SUPPORT_READ = "platform.customer_success.support_read"
FINANCE_READ = "platform.customer_success.finance_read"
CONTACT_READ = "platform.customer_success.contact.read"
PLAN_MANAGE = "platform.customer_success.plan.manage"
TASK_MANAGE = "platform.customer_success.task.manage"
RENEWAL_READ = "platform.customer_success.renewal.read"
BATCH_APPROVE = "platform.customer_success.batch.approve"

ReadScope = Literal["customer_success", "support", "finance", "owner"]


def account_scope(context: PlatformContext) -> ReadScope:
    permissions = set(context.permissions)
    if DIRECTORY in permissions and FINANCE_READ in permissions:
        return "owner"
    if DIRECTORY in permissions:
        return "customer_success"
    if SUPPORT_READ in permissions:
        return "support"
    if FINANCE_READ in permissions:
        return "finance"
    raise ApiError("platform.permission_denied")


def health_scope(context: PlatformContext) -> Literal["customer_success", "support"]:
    if DIRECTORY in context.permissions:
        return "customer_success"
    if SUPPORT_READ in context.permissions:
        return "support"
    raise ApiError("platform.permission_denied")


def require(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


__all__ = [
    "BATCH_APPROVE",
    "CONTACT_READ",
    "DIRECTORY",
    "FINANCE_READ",
    "PLAN_MANAGE",
    "RENEWAL_READ",
    "SUPPORT_READ",
    "TASK_MANAGE",
    "account_scope",
    "health_scope",
    "require",
]
