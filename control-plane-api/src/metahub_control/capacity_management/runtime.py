"""Fenced PRD-106 capacity.run handler for PRD-113."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from metahub_capacity_runner.contracts import CapacityPermitClaims
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ConfigDict, Field

from metahub_control.platform_jobs.catalog import register_job_handler
from metahub_control.platform_jobs.runtime import PlatformJobContext, PlatformJobFailure

from .runner_client import execute_capacity_run, permit_id
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


def _load_claims(context: PlatformJobContext, payload: CapacityRunInput) -> CapacityPermitClaims:
    connection = connect_database(context.config.database_url)
    try:
        row = connection.execute(
            "SELECT * FROM load_test_runs WHERE load_test_run_id=?", (payload.object_id,)
        ).fetchone()
        if row is None or row["status"] != "running":
            raise ApiError("capacity.runtime_conflict")
        now = datetime.now(UTC)
        return CapacityPermitClaims(
            permit_id=permit_id(payload.object_id),
            operation="load_test",
            object_id=payload.object_id,
            service_code=row["service_code"],
            scenario_code=row["scenario_code"],
            fault_code=None,
            environment_class=row["environment_class"],
            region_code=row["region_code"],
            synthetic_tenant_count=int(row["synthetic_tenant_count"]),
            target_rps=int(row["target_rps"]),
            duration_seconds=int(row["duration_seconds"]),
            objective_p95_ms=0,
            objective_error_rate_bps=0,
            stop_error_rate_bps=0,
            manifest_sha256=row["scenario_manifest_sha256"],
            kill_generation=0,
            issued_at=now,
            expires_at=now + timedelta(seconds=min(3600, int(row["duration_seconds"]) + 60)),
        )
    finally:
        connection.close()


def _chaos_claims(context: PlatformJobContext, payload: CapacityRunInput) -> CapacityPermitClaims:
    connection = connect_database(context.config.database_url)
    try:
        row = connection.execute(
            "SELECT * FROM chaos_experiments WHERE chaos_experiment_id=?", (payload.object_id,)
        ).fetchone()
        if row is None or row["status"] != "running":
            raise ApiError("capacity.runtime_conflict")
        now = datetime.now(UTC)
        return CapacityPermitClaims(
            permit_id=permit_id(payload.object_id),
            operation="chaos",
            object_id=payload.object_id,
            service_code=row["service_code"],
            scenario_code=None,
            fault_code=row["fault_code"],
            environment_class=row["environment_class"],
            region_code=row["region_code"],
            synthetic_tenant_count=int(row["blast_tenant_count"]),
            target_rps=0,
            duration_seconds=int(row["duration_seconds"]),
            objective_p95_ms=int(row["objective_p95_ms"]),
            objective_error_rate_bps=int(row["objective_error_rate_bps"]),
            stop_error_rate_bps=int(row["stop_error_rate_bps"]),
            manifest_sha256=row["fault_manifest_sha256"],
            kill_generation=int(row["kill_generation"]),
            issued_at=now,
            expires_at=now + timedelta(seconds=min(3600, int(row["duration_seconds"]) + 60)),
        )
    finally:
        connection.close()


def _fail_domain(context: PlatformJobContext, payload: CapacityRunInput, code: str) -> None:
    try:
        def apply(connection) -> None:
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


register_job_handler(
    "capacity.run",
    input_model=CapacityRunInput,
    handler=_capacity_handler,
)

__all__ = ["CapacityRunInput"]
