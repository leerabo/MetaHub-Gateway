"""Fenced PRD-106 capacity.run handler for PRD-113."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from metahub_capacity_runner.contracts import (
    CapacityPermitClaims,
    CapacityRunnerResult,
    execution_digest,
)
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ConfigDict, Field

from metahub_control.platform_jobs.catalog import register_job_handler
from metahub_control.platform_jobs.external import ExternalJobContext
from metahub_control.platform_jobs.runtime import PlatformJobContext, PlatformJobFailure

from .runner_client import capacity_execution, execute_capacity_run, permit_id
from .service import (
    complete_chaos_in_transaction,
    complete_forecast_in_transaction,
    complete_load_test_in_transaction,
    fail_chaos_in_transaction,
    fail_forecast_in_transaction,
    fail_load_test_in_transaction,
    start_chaos_in_transaction,
    start_forecast_in_transaction,
    start_load_test_in_transaction,
    stop_capacity_in_transaction,
)


class CapacityRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Literal["forecast", "load_test", "chaos"]
    object_id: str = Field(pattern=r"^(?:cfc|ltr|che)_[0-9a-f]{32}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _capacity_handler(context: PlatformJobContext, payload: CapacityRunInput) -> str:
    context.checkpoint(
        {
            "phase": "precheck",
            "operation": payload.operation,
            "object_id": payload.object_id,
            "manifest_sha256": payload.manifest_sha256,
        }
    )
    try:
        if payload.operation == "forecast":
            context.commit_effect(
                "capacity.forecast.start",
                {"object_id": payload.object_id},
                lambda connection: start_forecast_in_transaction(
                    connection, forecast_id=payload.object_id
                ),
            )
            context.commit_effect(
                "capacity.forecast.complete",
                {"object_id": payload.object_id, "model": "linear-v1"},
                lambda connection: complete_forecast_in_transaction(
                    connection, forecast_id=payload.object_id
                ),
            )
            return "capacity.forecast_succeeded"
        if context.config.capacity_runner_mode == "isolated_http":
            _defer_capacity(context, payload)
        if payload.operation == "load_test":
            context.commit_effect(
                "capacity.load.start",
                {"object_id": payload.object_id, "manifest": payload.manifest_sha256},
                lambda connection: start_load_test_in_transaction(
                    connection,
                    run_id=payload.object_id,
                    manifest_sha256=payload.manifest_sha256,
                ),
            )
            claims = _load_claims(context, payload)
            result = execute_capacity_run(context.config, claims)
            aggregate = result.model_dump(mode="json", exclude_none=True)
            context.commit_effect(
                "capacity.load.complete",
                {"object_id": payload.object_id, "result_sha256": result.result_sha256},
                lambda connection: complete_load_test_in_transaction(
                    connection, run_id=payload.object_id, result=aggregate
                ),
            )
            return "capacity.load_succeeded"
        context.commit_effect(
            "capacity.chaos.start",
            {"object_id": payload.object_id, "manifest": payload.manifest_sha256},
            lambda connection: start_chaos_in_transaction(
                connection,
                experiment_id=payload.object_id,
                manifest_sha256=payload.manifest_sha256,
            ),
        )
        claims = _chaos_claims(context, payload)
        result = execute_capacity_run(context.config, claims)
        aggregate = result.model_dump(mode="json", exclude_none=True)
        context.commit_effect(
            "capacity.chaos.complete",
            {"object_id": payload.object_id, "result_sha256": result.result_sha256},
            lambda connection: complete_chaos_in_transaction(
                connection, experiment_id=payload.object_id, result=aggregate
            ),
        )
        return "capacity.chaos_succeeded"
    except ApiError as exc:
        _fail_domain(context, payload, exc.code)
        raise PlatformJobFailure(exc.code) from None


def _load_claims(
    context: PlatformJobContext, payload: CapacityRunInput, *, status: str = "running"
) -> CapacityPermitClaims:
    return _read_claims(context, payload, status=status)


def _chaos_claims(
    context: PlatformJobContext, payload: CapacityRunInput, *, status: str = "running"
) -> CapacityPermitClaims:
    return _read_claims(context, payload, status=status)


def _read_claims(context, payload, *, status):
    connection = connect_database(context.config.database_url)
    try:
        table, column = (
            ("load_test_runs", "load_test_run_id")
            if payload.operation == "load_test"
            else ("chaos_experiments", "chaos_experiment_id")
        )
        row = connection.execute(
            f"SELECT * FROM {table} WHERE {column}=?", (payload.object_id,)
        ).fetchone()
        if row is None or row["status"] != status:
            raise ApiError("capacity.runtime_conflict")
        return _claims_from_row(context, payload, row)
    finally:
        connection.close()


def _claims_from_row(context, payload, row) -> CapacityPermitClaims:
    load = payload.operation == "load_test"
    manifest = row["scenario_manifest_sha256" if load else "fault_manifest_sha256"]
    if row["platform_job_id"] != context.lease.job_id or manifest != payload.manifest_sha256:
        raise ApiError("capacity.runtime_conflict")
    now = datetime.now(UTC)
    return CapacityPermitClaims(
        permit_id=permit_id(payload.object_id),
        operation=payload.operation,
        object_id=payload.object_id,
        service_code=row["service_code"],
        scenario_code=row["scenario_code"] if load else None,
        fault_code=None if load else row["fault_code"],
        environment_class=row["environment_class"],
        region_code=row["region_code"],
        synthetic_tenant_count=int(row["synthetic_tenant_count" if load else "blast_tenant_count"]),
        target_rps=int(row["target_rps"]) if load else 0,
        duration_seconds=int(row["duration_seconds"]),
        objective_p95_ms=0 if load else int(row["objective_p95_ms"]),
        objective_error_rate_bps=0 if load else int(row["objective_error_rate_bps"]),
        stop_error_rate_bps=0 if load else int(row["stop_error_rate_bps"]),
        manifest_sha256=manifest,
        kill_generation=0 if load else int(row["kill_generation"]),
        issued_at=now,
        expires_at=now + timedelta(seconds=min(3600, int(row["duration_seconds"]) + 60)),
    )


def _defer_capacity(context: PlatformJobContext, payload: CapacityRunInput) -> None:
    claims = _read_claims(context, payload, status="queued")

    def start(connection):
        if payload.operation == "load_test":
            row = start_load_test_in_transaction(
                connection, run_id=payload.object_id, manifest_sha256=payload.manifest_sha256
            )
        else:
            row = start_chaos_in_transaction(
                connection, experiment_id=payload.object_id, manifest_sha256=payload.manifest_sha256
            )
        if execution_digest(_claims_from_row(context, payload, row)) != execution_digest(claims):
            raise ApiError("capacity.runtime_conflict")

    start_window = int((claims.expires_at - claims.issued_at).total_seconds())
    context.defer_external(
        claims.model_dump(mode="json"),
        timeout_seconds=start_window + claims.duration_seconds + 300,
        apply=start,
    )


def _capacity_external_handler(context: ExternalJobContext, value: object) -> str:
    claims = CapacityPermitClaims.model_validate_json(json.dumps(value))
    reason = context.stop_reason()
    now = datetime.now(UTC)
    expired_submission = not context.lease.acknowledged and claims.expires_at <= now
    action = (
        "cancel"
        if reason or expired_submission
        else ("status" if context.lease.acknowledged else "submit")
    )
    # Only observations renew authorization; uncertain submissions retain the original permit.
    request_claims = (
        claims
        if action == "submit"
        else claims.model_copy(
            update={
                "issued_at": now,
                "expires_at": now + timedelta(seconds=60),
            }
        )
    )
    try:
        response = capacity_execution(context.config, request_claims, action=action)
    except ApiError:
        return context.defer(outcome="capacity.runner_unavailable")
    result = response if isinstance(response, CapacityRunnerResult) else response.result
    if result is None and response.state in {"queued", "running", "stopping"}:
        return context.defer(acknowledged=True)
    status = (
        "succeeded"
        if result is not None
        else ("cancelled" if response.state == "cancelled" and not expired_submission else "failed")
    )
    outcome = (
        f"capacity.{claims.operation}_succeeded"
        if result is not None
        else "capacity.submission_expired"
        if expired_submission
        else response.safe_outcome_code
    )

    def complete(connection, final_status, final_outcome):
        _require_domain_owner(connection, context.lease.job_id, claims.operation, claims.object_id)
        if final_status != "succeeded":
            stop_capacity_in_transaction(
                connection,
                job_id=context.lease.job_id,
                operation=claims.operation,
                object_id=claims.object_id,
                cancelled=final_status == "cancelled",
                outcome_code=final_outcome,
            )
        elif claims.operation == "load_test":
            complete_load_test_in_transaction(
                connection,
                run_id=claims.object_id,
                result=result.model_dump(mode="json", exclude_none=True),
            )
        else:
            complete_chaos_in_transaction(
                connection,
                experiment_id=claims.object_id,
                result=result.model_dump(mode="json", exclude_none=True),
            )

    return context.finish(
        status=status,
        outcome=outcome,
        value={
            "object_id": claims.object_id,
            "result_sha256": result.result_sha256 if result is not None else None,
        },
        stop_confirmed=True,
        apply=complete,
    )


def _fail_domain(context: PlatformJobContext, payload: CapacityRunInput, code: str) -> None:
    try:

        def apply(connection) -> None:
            _require_domain_owner(
                connection, context.lease.job_id, payload.operation, payload.object_id
            )
            if payload.operation == "forecast":
                fail_forecast_in_transaction(
                    connection, forecast_id=payload.object_id, outcome_code=code
                )
            elif payload.operation == "load_test":
                fail_load_test_in_transaction(
                    connection, run_id=payload.object_id, outcome_code=code
                )
            else:
                fail_chaos_in_transaction(
                    connection, experiment_id=payload.object_id, outcome_code=code
                )

        context.commit_effect(
            f"capacity.{payload.operation}.failure",
            {"object_id": payload.object_id, "outcome_code": code},
            apply,
        )
    except ApiError:
        pass


def _require_domain_owner(connection, job_id: str, operation: str, object_id: str) -> None:
    table, column = {
        "forecast": ("capacity_forecasts", "capacity_forecast_id"),
        "load_test": ("load_test_runs", "load_test_run_id"),
        "chaos": ("chaos_experiments", "chaos_experiment_id"),
    }[operation]
    row = connection.execute(
        f"SELECT platform_job_id FROM {table} WHERE {column}=?", (object_id,)
    ).fetchone()
    if row is None or row["platform_job_id"] != job_id:
        raise ApiError("capacity.runtime_conflict")


register_job_handler(
    "capacity.run",
    input_model=CapacityRunInput,
    handler=_capacity_handler,
    external_handler=_capacity_external_handler,
)

__all__ = ["CapacityRunInput"]
