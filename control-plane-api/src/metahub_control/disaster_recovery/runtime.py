"""Fenced PRD-106 handler for PRD-111 disaster-recovery runs."""

from __future__ import annotations

from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ConfigDict, Field

from metahub_control.platform_jobs.catalog import register_job_handler
from metahub_control.platform_jobs.runtime import (
    PlatformJobContext,
    PlatformJobFailure,
    PlatformJobRetry,
)

from .service import (
    complete_run_in_transaction,
    fail_run_in_transaction,
    precheck_run_in_transaction,
    quiesce_run_in_transaction,
    route_run_in_transaction,
    validate_restore_in_transaction,
    verify_run_in_transaction,
)


class DisasterRecoveryRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str = Field(pattern=r"^drr_[0-9a-f]{32}$")
    expected_epoch: int = Field(ge=0)
    plan_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _run_handler(context: PlatformJobContext, payload: DisasterRecoveryRunInput) -> str:
    try:
        _phase(
            context,
            payload,
            "precheck",
            lambda connection: precheck_run_in_transaction(
                connection,
                run_id=payload.run_id,
                expected_epoch=payload.expected_epoch,
                plan_manifest_sha256=payload.plan_manifest_sha256,
            ),
        )
        _phase(
            context,
            payload,
            "quiesce",
            lambda connection: quiesce_run_in_transaction(connection, run_id=payload.run_id),
        )
        _phase(
            context,
            payload,
            "restore",
            lambda connection: validate_restore_in_transaction(connection, run_id=payload.run_id),
        )
        _phase(
            context,
            payload,
            "route",
            lambda connection: route_run_in_transaction(
                connection, context.config, run_id=payload.run_id
            ),
        )
        _phase(
            context,
            payload,
            "verify",
            lambda connection: verify_run_in_transaction(connection, run_id=payload.run_id),
        )
        _phase(
            context,
            payload,
            "complete",
            lambda connection: complete_run_in_transaction(connection, run_id=payload.run_id),
        )
    except ApiError as exc:
        if exc.code in {"dr.unavailable", "platform_job.unavailable"}:
            raise PlatformJobRetry(exc.code) from None
        outcome_code = exc.code
        try:
            context.commit_effect(
                "dr.failure",
                {"run_id": payload.run_id, "outcome_code": outcome_code},
                lambda connection: fail_run_in_transaction(
                    connection, run_id=payload.run_id, outcome_code=outcome_code
                ),
            )
        except ApiError:
            pass
        raise PlatformJobFailure(outcome_code) from None
    return "dr.run_succeeded"


def _phase(
    context: PlatformJobContext, payload: DisasterRecoveryRunInput, phase: str, apply
) -> None:
    context.checkpoint(
        {
            "phase": phase,
            "run_id": payload.run_id,
            "expected_epoch": payload.expected_epoch,
            "plan_manifest_sha256": payload.plan_manifest_sha256,
        }
    )
    context.commit_effect(
        f"dr.{phase}",
        {
            "run_id": payload.run_id,
            "expected_epoch": payload.expected_epoch,
            "plan_manifest_sha256": payload.plan_manifest_sha256,
        },
        apply,
    )


register_job_handler(
    "dr.run",
    input_model=DisasterRecoveryRunInput,
    handler=_run_handler,
)

__all__ = ["DisasterRecoveryRunInput"]
