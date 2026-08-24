"""Fenced PRD-106 handler for expiring PRD-110 restriction overlays."""

from __future__ import annotations

from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ConfigDict, Field

from metahub_control.platform_jobs.catalog import register_job_handler
from metahub_control.platform_jobs.runtime import (
    PlatformJobContext,
    PlatformJobFailure,
    PlatformJobRetry,
)

from .service import expire_restriction_in_transaction


class RiskRestrictionExpireInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    restriction_id: str = Field(pattern=r"^rst_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)


def _expire_handler(context: PlatformJobContext, payload: RiskRestrictionExpireInput) -> str:
    context.checkpoint(
        {
            "phase": "expire",
            "restriction_id": payload.restriction_id,
            "expected_revision": payload.expected_revision,
        }
    )
    changed = {"value": False}

    def apply(connection) -> None:
        changed["value"] = expire_restriction_in_transaction(
            connection,
            restriction_id=payload.restriction_id,
            expected_revision=payload.expected_revision,
        )

    try:
        context.commit_effect(
            "risk.restriction.expire",
            payload.model_dump(mode="json"),
            apply,
        )
    except ApiError as exc:
        if exc.code in {"risk.unavailable", "risk.restriction_not_due"}:
            raise PlatformJobRetry(exc.code) from None
        if exc.code == "risk.revision_conflict":
            raise PlatformJobFailure(exc.code) from None
        raise PlatformJobFailure(exc.code) from None
    context.checkpoint(
        {
            "phase": "complete",
            "restriction_id": payload.restriction_id,
            "expired": changed["value"],
        }
    )
    return "risk.restriction_expired" if changed["value"] else "risk.restriction_already_terminal"


register_job_handler(
    "risk.restriction.expire",
    input_model=RiskRestrictionExpireInput,
    handler=_expire_handler,
)

__all__ = ["RiskRestrictionExpireInput"]
