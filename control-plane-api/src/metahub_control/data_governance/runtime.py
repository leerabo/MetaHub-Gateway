"""Fenced PRD-106 runtime for PRD-117 retention deletion schedules."""

from __future__ import annotations

from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ConfigDict, Field

from metahub_control.platform_jobs.catalog import register_job_handler
from metahub_control.platform_jobs.runtime import (
    PlatformJobContext,
    PlatformJobFailure,
    PlatformJobRetry,
)

from .service import deletion_schedule_outcome, execute_deletion_schedule_in_transaction


class DataRetentionDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schedule_id: str = Field(pattern=r"^dgs_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    tenant_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    policy_generation: int = Field(ge=1)
    hold_generation: int = Field(ge=0)


def _delete_handler(context: PlatformJobContext, payload: DataRetentionDeleteInput) -> str:
    context.checkpoint(
        {
            "phase": "fence",
            "schedule_id": payload.schedule_id,
            "workspace_id": payload.workspace_id,
            "tenant_id": payload.tenant_id,
            "policy_generation": payload.policy_generation,
            "hold_generation": payload.hold_generation,
        }
    )
    result: list[str] = []

    def apply(connection) -> None:
        result.append(
            execute_deletion_schedule_in_transaction(
                connection,
                schedule_id=payload.schedule_id,
                platform_job_id=context.lease.job_id,
                workspace_id=payload.workspace_id,
                tenant_id=payload.tenant_id,
                policy_generation=payload.policy_generation,
                hold_generation=payload.hold_generation,
            )
        )

    try:
        applied = context.commit_effect(
            "data-retention.execute",
            payload.model_dump(mode="json"),
            apply,
        )
    except ApiError as exc:
        if exc.code in {"data_governance.unavailable", "platform_job.unavailable"}:
            raise PlatformJobRetry(exc.code) from None
        raise PlatformJobFailure(exc.code) from None
    if applied:
        return result[0]
    return deletion_schedule_outcome(
        context.config,
        schedule_id=payload.schedule_id,
        platform_job_id=context.lease.job_id,
        workspace_id=payload.workspace_id,
        tenant_id=payload.tenant_id,
    )


register_job_handler(
    "data.retention.delete",
    input_model=DataRetentionDeleteInput,
    handler=_delete_handler,
)

__all__ = ["DataRetentionDeleteInput"]
