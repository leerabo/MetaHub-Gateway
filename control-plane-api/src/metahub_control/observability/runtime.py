"""Fenced PRD-106 handler for deterministic SLO and error-budget aggregation."""

from __future__ import annotations

from datetime import UTC, datetime

from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ConfigDict, Field

from metahub_control.platform_jobs.catalog import register_job_handler
from metahub_control.platform_jobs.runtime import (
    PlatformJobContext,
    PlatformJobFailure,
    PlatformJobRetry,
)

from .service import aggregate_slo_in_transaction


class ObservabilityAggregateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    slo_code: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,63}$")
    slo_version: int = Field(ge=1)
    source_watermark: int = Field(ge=0)
    window_ended_at: str = Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:00\.000Z$"
    )


def _aggregate_handler(
    context: PlatformJobContext, payload: ObservabilityAggregateInput
) -> str:
    context.checkpoint(
        {
            "phase": "aggregate",
            "slo_code": payload.slo_code,
            "slo_version": payload.slo_version,
            "source_watermark": payload.source_watermark,
            "window_ended_at": payload.window_ended_at,
        }
    )
    outcome: dict[str, str] = {}

    def apply(connection) -> None:
        outcome["budget_id"] = aggregate_slo_in_transaction(
            connection,
            context.config,
            slo_code=payload.slo_code,
            slo_version=payload.slo_version,
            source_watermark=payload.source_watermark,
            window_ended_at=_time(payload.window_ended_at),
        )

    try:
        context.commit_effect(
            "observability.aggregate",
            payload.model_dump(mode="json"),
            apply,
        )
    except ApiError as exc:
        if exc.code in {
            "observability.unavailable",
            "observability.watermark_unavailable",
        }:
            raise PlatformJobRetry(exc.code) from None
        raise PlatformJobFailure(exc.code) from None
    context.checkpoint(
        {
            "budget_id": outcome.get("budget_id", "already_committed"),
            "phase": "complete",
            "slo_code": payload.slo_code,
            "slo_version": payload.slo_version,
            "source_watermark": payload.source_watermark,
        }
    )
    return "observability.aggregate_completed"


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


register_job_handler(
    "observability.aggregate",
    input_model=ObservabilityAggregateInput,
    handler=_aggregate_handler,
)

__all__ = ["ObservabilityAggregateInput"]
