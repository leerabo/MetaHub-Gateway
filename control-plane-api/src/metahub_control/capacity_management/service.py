"""Database-authoritative PRD-113 capacity and resilience management."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.platform_jobs.service import (
    _event as record_platform_job_event,
)
from metahub_control.platform_jobs.service import (
    enqueue_prepared_platform_job_in_transaction,
    prepare_platform_job,
)

from .models import (
    CapacityForecastInfo,
    CapacityForecastRequest,
    CapacityForecastResponse,
    CapacityScenarioInfo,
    CapacityServiceInfo,
    CapacityStatusResponse,
    ChaosApprovalInfo,
    ChaosCommandRequest,
    ChaosExperimentInfo,
    ChaosExperimentResponse,
    ChaosFaultInfo,
    LoadTestRequest,
    LoadTestResponse,
    LoadTestRunInfo,
    PerformanceBaselineInfo,
    PerformanceRolloutBlockInfo,
    UnitEconomicsInfo,
    UnitEconomicsResponse,
)

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def get_capacity_status(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> CapacityStatusResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token=session_token)
        _require(actor, "platform.capacity.read")
        response = CapacityStatusResponse(
            services=tuple(
                _service_info(row)
                for row in connection.execute(
                    "SELECT * FROM capacity_service_states ORDER BY service_code,region_code"
                ).fetchall()
            ),
            scenarios=tuple(
                _scenario_info(row)
                for row in connection.execute(
                    "SELECT * FROM capacity_scenarios ORDER BY scenario_code"
                ).fetchall()
            ),
            baselines=tuple(
                _baseline_info(row)
                for row in connection.execute(
                    "SELECT * FROM performance_baselines "
                    "ORDER BY created_at DESC,performance_baseline_id DESC LIMIT 50"
                ).fetchall()
            ),
            forecasts=tuple(
                _forecast_info(row)
                for row in connection.execute(
                    "SELECT * FROM capacity_forecasts "
                    "ORDER BY created_at DESC,capacity_forecast_id DESC LIMIT 50"
                ).fetchall()
            ),
            load_tests=tuple(
                _load_test_info(row)
                for row in connection.execute(
                    "SELECT * FROM load_test_runs "
                    "ORDER BY created_at DESC,load_test_run_id DESC LIMIT 50"
                ).fetchall()
            ),
            chaos_faults=tuple(
                _fault_info(row)
                for row in connection.execute(
                    "SELECT * FROM chaos_fault_catalog ORDER BY fault_code"
                ).fetchall()
            ),
            chaos_experiments=tuple(
                _chaos_info(connection, row)
                for row in connection.execute(
                    "SELECT * FROM chaos_experiments "
                    "ORDER BY created_at DESC,chaos_experiment_id DESC LIMIT 50"
                ).fetchall()
            ),
            rollout_blocks=tuple(
                _rollout_block_info(row)
                for row in connection.execute(
                    "SELECT * FROM performance_rollout_blocks "
                    "ORDER BY created_at DESC,performance_rollout_block_id DESC LIMIT 50"
                ).fetchall()
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("capacity.unavailable") from None
    finally:
        connection.close()


def create_capacity_forecast(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    payload: CapacityForecastRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> CapacityForecastResponse:
    _valid_idempotency(idempotency_key)
    request_value = {
        "expected_revision": expected_revision,
        **payload.model_dump(mode="json"),
    }
    request_hash = _digest(request_value)
    idempotency_hash = _secret_digest(config, "command", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        _require(actor, "platform.capacity.test")
        replay = _command_replay(
            connection,
            actor,
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            action="forecast.create",
        )
        if replay is not None:
            response = CapacityForecastResponse(
                forecast=_forecast_by_id(connection, str(replay["result_object_id"])),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        state = connection.execute(
            "SELECT * FROM capacity_service_states WHERE service_code=? AND region_code=?",
            (payload.service_code, payload.region_code),
        ).fetchone()
        if state is None:
            raise ApiError("capacity.scope_not_found")
        if int(state["revision"]) != expected_revision:
            raise ApiError("capacity.revision_conflict")
        forecast_id = _id("cfc_")
        input_hash = _digest(
            {
                "service_code": state["service_code"],
                "region_code": state["region_code"],
                "demand_rps": int(state["demand_rps"]),
                "capacity_rps": int(state["capacity_rps"]),
                "revision": int(state["revision"]),
            }
        )
        prepared = prepare_platform_job(
            config,
            type_code="capacity.run",
            payload={
                "operation": "forecast",
                "object_id": forecast_id,
                "manifest_sha256": input_hash,
            },
        )
        job_id, _ = enqueue_prepared_platform_job_in_transaction(
            connection,
            config,
            prepared=prepared,
            priority=40,
            source_code="capacity.forecast",
            idempotency_key=f"capacity-forecast:{forecast_id}",
            request_id=request_id,
            trace_id=trace_id,
        )
        now = _database_now(connection)
        stamp = _db_time(now)
        connection.execute(
            """INSERT INTO capacity_forecasts(
                 capacity_forecast_id,service_code,region_code,horizon_days,model_code,
                 input_demand_rps,input_capacity_rps,input_aggregate_sha256,
                 predicted_demand_rps,predicted_capacity_rps,confidence_bps,error_outcome_bps,
                 recommendation_code,status,platform_job_id,created_by_operator_id,revision,
                 created_at,updated_at,completed_at)
               VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,'queued',?,?,1,?,?,NULL)""",
            (
                forecast_id,
                payload.service_code,
                payload.region_code,
                payload.horizon_days,
                payload.model_code,
                int(state["demand_rps"]),
                int(state["capacity_rps"]),
                input_hash,
                job_id,
                actor.operator_id,
                stamp,
                stamp,
            ),
        )
        _record_command(
            connection,
            actor=actor,
            action="forecast.create",
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            result_type="capacity_forecast",
            result_id=forecast_id,
            now=now,
        )
        _audit(
            connection,
            actor=actor,
            action="capacity.forecast.create",
            object_type="capacity_forecast",
            object_id=forecast_id,
            reason="capacity_planning",
            request_id=request_id,
            trace_id=trace_id,
            after={"forecast_id": forecast_id, "revision": 1},
            now=now,
        )
        response = CapacityForecastResponse(
            forecast=_forecast_by_id(connection, forecast_id),
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("capacity.unavailable") from None
    finally:
        connection.close()


def create_load_test(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    payload: LoadTestRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> LoadTestResponse:
    _valid_idempotency(idempotency_key)
    request_hash = _digest(payload.model_dump(mode="json"))
    idempotency_hash = _secret_digest(config, "command", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        _require(actor, "platform.capacity.test")
        replay = _command_replay(
            connection,
            actor,
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            action="load_test.create",
        )
        if replay is not None:
            response = LoadTestResponse(
                run=_load_test_by_id(connection, str(replay["result_object_id"])),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        scenario = connection.execute(
            "SELECT * FROM capacity_scenarios WHERE scenario_code=?",
            (payload.scenario_code,),
        ).fetchone()
        if scenario is None or scenario["status"] != "active":
            raise ApiError("capacity.scenario_not_found")
        if (
            payload.synthetic_tenant_count > int(scenario["max_tenant_count"])
            or payload.target_rps > int(scenario["max_target_rps"])
            or payload.duration_seconds > int(scenario["max_duration_seconds"])
        ):
            raise ApiError("capacity.limit_exceeded")
        if connection.execute(
            "SELECT 1 FROM platform_regions WHERE region_code=?",
            (payload.region_code,),
        ).fetchone() is None:
            raise ApiError("capacity.scope_not_found")
        run_id = _id("ltr_")
        prepared = prepare_platform_job(
            config,
            type_code="capacity.run",
            payload={
                "operation": "load_test",
                "object_id": run_id,
                "manifest_sha256": str(scenario["manifest_sha256"]),
            },
        )
        job_id, _ = enqueue_prepared_platform_job_in_transaction(
            connection,
            config,
            prepared=prepared,
            priority=20,
            source_code="capacity.load_test",
            idempotency_key=f"capacity-load:{run_id}",
            request_id=request_id,
            trace_id=trace_id,
        )
        now = _database_now(connection)
        stamp = _db_time(now)
        connection.execute(
            """INSERT INTO load_test_runs(
                 load_test_run_id,scenario_code,service_code,run_kind,environment_class,
                 region_code,synthetic_tenant_count,target_rps,duration_seconds,
                 scenario_manifest_sha256,status,platform_job_id,created_by_operator_id,
                 sample_count,p50_ms,p95_ms,p99_ms,throughput_rps_milli,error_rate_bps,
                 cpu_peak_bps,memory_peak_mib,open_connection_delta,handle_delta,
                 fairness_jain_millionths,controlled_degradation,regression_status,
                 result_sha256,safe_outcome_code,revision,started_at,completed_at,
                 created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
                 NULL,NULL,NULL,NULL,NULL,NULL,NULL,1,NULL,NULL,?,?)""",
            (
                run_id,
                scenario["scenario_code"],
                scenario["service_code"],
                scenario["run_kind"],
                payload.environment_class,
                payload.region_code,
                payload.synthetic_tenant_count,
                payload.target_rps,
                payload.duration_seconds,
                scenario["manifest_sha256"],
                job_id,
                actor.operator_id,
                stamp,
                stamp,
            ),
        )
        _record_command(
            connection,
            actor=actor,
            action="load_test.create",
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            result_type="load_test_run",
            result_id=run_id,
            now=now,
        )
        _audit(
            connection,
            actor=actor,
            action="capacity.load_test.create",
            object_type="load_test_run",
            object_id=run_id,
            reason="performance_validation",
            request_id=request_id,
            trace_id=trace_id,
            after={"run_id": run_id, "revision": 1},
            now=now,
        )
        response = LoadTestResponse(
            run=_load_test_by_id(connection, run_id),
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("capacity.unavailable") from None
    finally:
        connection.close()


def command_chaos_experiment(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int | None,
    payload: ChaosCommandRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ChaosExperimentResponse:
    _valid_idempotency(idempotency_key)
    request_value = {
        "expected_revision": expected_revision,
        **payload.model_dump(mode="json"),
    }
    request_hash = _digest(request_value)
    idempotency_hash = _secret_digest(config, "command", idempotency_key)
    action = f"chaos.{payload.action}"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        _require(actor, "platform.capacity.chaos")
        replay = _command_replay(
            connection,
            actor,
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            action=action,
        )
        if replay is not None:
            response = ChaosExperimentResponse(
                experiment=_chaos_by_id(connection, str(replay["result_object_id"])),
                approval_recorded=False,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        now = _database_now(connection)
        if payload.action == "create":
            experiment_id = _create_chaos(
                connection,
                config,
                actor=actor,
                payload=payload,
                idempotency_key=idempotency_key,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
            approval_recorded = True
        else:
            if expected_revision is None or payload.chaos_experiment_id is None:
                raise ApiError("capacity.request_invalid")
            experiment_id = payload.chaos_experiment_id
            if payload.action == "approve":
                _approve_chaos(
                    connection,
                    config,
                    actor=actor,
                    experiment_id=experiment_id,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    trace_id=trace_id,
                    now=now,
                )
                approval_recorded = True
            else:
                _kill_chaos(
                    connection,
                    experiment_id=experiment_id,
                    expected_revision=expected_revision,
                    now=now,
                )
                approval_recorded = False
        _record_command(
            connection,
            actor=actor,
            action=action,
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            result_type="chaos_experiment",
            result_id=experiment_id,
            now=now,
        )
        experiment = _chaos_by_id(connection, experiment_id)
        _audit(
            connection,
            actor=actor,
            action=f"capacity.{action}",
            object_type="chaos_experiment",
            object_id=experiment_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={
                "experiment_id": experiment_id,
                "status": experiment.status,
                "revision": experiment.revision,
                "kill_generation": experiment.kill_generation,
            },
            now=now,
        )
        response = ChaosExperimentResponse(
            experiment=experiment,
            approval_recorded=approval_recorded,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "uq_chaos_active_scope" in str(exc) or "UNIQUE constraint" in str(exc):
            raise ApiError("capacity.chaos_conflict") from None
        raise ApiError("capacity.unavailable") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("capacity.unavailable") from None
    finally:
        connection.close()


def get_unit_economics(
    config: AppConfig,
    *,
    session_token: str,
    billing_period: str | None,
    currency: str | None,
    request_id: str,
    trace_id: str,
) -> UnitEconomicsResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token=session_token)
        _require(actor, "platform.unit_economics.read")
        clauses: list[str] = []
        values: list[object] = []
        if billing_period is not None:
            clauses.append("billing_period=?")
            values.append(billing_period)
        if currency is not None:
            clauses.append("currency=?")
            values.append(currency)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = connection.execute(
            "SELECT * FROM unit_economics_snapshots"
            + where
            + " ORDER BY billing_period DESC,service_code,currency,allocation_version DESC",
            tuple(values),
        ).fetchall()
        if rows:
            items = tuple(_economics_info(row) for row in rows)
        else:
            items = _live_economics(connection, billing_period=billing_period, currency=currency)
        response = UnitEconomicsResponse(
            items=items,
            total=len(items),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("capacity.unavailable") from None
    finally:
        connection.close()


def start_forecast_in_transaction(connection, *, forecast_id: str) -> None:
    row = _forecast_row(connection, forecast_id)
    if row["status"] != "queued":
        raise ApiError("capacity.runtime_conflict")
    now = _database_now(connection)
    connection.execute(
        "UPDATE capacity_forecasts SET status='running',revision=revision+1,updated_at=? "
        "WHERE capacity_forecast_id=? AND status='queued'",
        (_db_time(_changed_time(now, row["updated_at"])), forecast_id),
    )


def complete_forecast_in_transaction(connection, *, forecast_id: str) -> None:
    row = _forecast_row(connection, forecast_id)
    if row["status"] != "running":
        raise ApiError("capacity.runtime_conflict")
    horizon = int(row["horizon_days"])
    demand = int(row["input_demand_rps"])
    capacity = int(row["input_capacity_rps"])
    predicted = max(demand, (demand * (125 if horizon == 30 else 175) + 99) // 100)
    required_capacity = max(1, (predicted * 10_000 + 7499) // 7500)
    predicted_capacity = max(capacity, required_capacity)
    utilization = predicted * 10_000 // max(1, capacity)
    recommendation = (
        "scale-within-30d"
        if horizon == 30 and utilization >= 7500
        else "scale-within-90d"
        if utilization >= 6500
        else "hold"
    )
    now = _database_now(connection)
    stamp = _db_time(_changed_time(now, row["updated_at"]))
    connection.execute(
        """UPDATE capacity_forecasts SET status='succeeded',predicted_demand_rps=?,
             predicted_capacity_rps=?,confidence_bps=8500,error_outcome_bps=0,
             recommendation_code=?,revision=revision+1,updated_at=?,completed_at=?
           WHERE capacity_forecast_id=? AND status='running'""",
        (predicted, predicted_capacity, recommendation, stamp, stamp, forecast_id),
    )
    state = connection.execute(
        "SELECT threshold_bps FROM capacity_service_states WHERE service_code=? AND region_code=?",
        (row["service_code"], row["region_code"]),
    ).fetchone()
    if state is not None and utilization >= int(state["threshold_bps"]):
        _event(
            connection,
            event_type="capacity.threshold_reached",
            service_code=str(row["service_code"]),
            scenario_code=None,
            outcome_code=recommendation,
            aggregate={"forecast_id": forecast_id, "utilization_bps": utilization},
            request_id="capacity-runtime",
            trace_id="capacity-runtime",
            now=now,
        )


def fail_forecast_in_transaction(connection, *, forecast_id: str, outcome_code: str) -> None:
    row = _forecast_row(connection, forecast_id)
    if row["status"] in {"succeeded", "failed"}:
        return
    now = _database_now(connection)
    stamp = _db_time(_changed_time(now, row["updated_at"]))
    connection.execute(
        "UPDATE capacity_forecasts SET status='failed',revision=revision+1,updated_at=?,"
        "completed_at=? WHERE capacity_forecast_id=?",
        (stamp, stamp, forecast_id),
    )


def start_load_test_in_transaction(connection, *, run_id: str, manifest_sha256: str):
    row = _load_row(connection, run_id)
    if row["status"] != "queued" or not hmac.compare_digest(
        str(row["scenario_manifest_sha256"]), manifest_sha256
    ):
        raise ApiError("capacity.runtime_conflict")
    scenario = connection.execute(
        "SELECT * FROM capacity_scenarios WHERE scenario_code=?",
        (row["scenario_code"],),
    ).fetchone()
    if scenario is None or scenario["status"] != "active" or not hmac.compare_digest(
        str(scenario["manifest_sha256"]), manifest_sha256
    ):
        raise ApiError("capacity.scenario_unavailable")
    now = _database_now(connection)
    connection.execute(
        "UPDATE load_test_runs SET status='running',revision=revision+1,started_at=?,updated_at=? "
        "WHERE load_test_run_id=? AND status='queued'",
        (
            _db_time(now),
            _db_time(_changed_time(now, row["updated_at"])),
            run_id,
        ),
    )
    return row


def complete_load_test_in_transaction(
    connection, *, run_id: str, result: dict[str, object]
) -> None:
    row = _load_row(connection, run_id)
    if row["status"] != "running":
        raise ApiError("capacity.runtime_conflict")
    baseline = connection.execute(
        """SELECT * FROM performance_baselines WHERE service_code=? AND scenario_code=?
             AND environment_class=? ORDER BY version DESC LIMIT 1""",
        (row["service_code"], row["scenario_code"], row["environment_class"]),
    ).fetchone()
    sample_count = int(result["sample_count"])
    service = connection.execute(
        "SELECT slo_p95_ms FROM capacity_service_states WHERE service_code=? AND region_code=?",
        (row["service_code"], row["region_code"]),
    ).fetchone()
    healthy = (
        service is not None
        and int(result["p95_ms"]) <= int(service["slo_p95_ms"])
        and int(result["error_rate_bps"]) <= 100
        and int(result["throughput_rps_milli"]) * 100 >= int(row["target_rps"]) * 1000 * 80
        and int(result["fairness_jain_millionths"]) >= 950_000
        and result["controlled_degradation"] is True
    )
    if baseline is None:
        regression = (
            "no_baseline" if sample_count >= 100 and healthy else "insufficient_confidence"
        )
    elif sample_count < 100:
        regression = "insufficient_confidence"
    else:
        regression = (
            "regressed"
            if not healthy
            or int(result["p95_ms"]) * 100 > int(baseline["p95_ms"]) * 120
            or int(result["error_rate_bps"]) > int(baseline["error_rate_bps"]) + 100
            or int(result["throughput_rps_milli"]) * 100
            < int(baseline["throughput_rps_milli"]) * 80
            else "within_baseline"
        )
    safe_outcome = (
        "performance.regressed"
        if regression == "regressed"
        else "performance.baseline_recorded"
        if regression == "no_baseline"
        else "performance.within_baseline"
        if regression == "within_baseline"
        else "performance.insufficient_confidence"
    )
    result_hash = _digest(result)
    now = _database_now(connection)
    stamp = _db_time(_changed_time(now, row["updated_at"]))
    connection.execute(
        """UPDATE load_test_runs SET status='succeeded',sample_count=?,p50_ms=?,p95_ms=?,
             p99_ms=?,throughput_rps_milli=?,error_rate_bps=?,cpu_peak_bps=?,memory_peak_mib=?,
             open_connection_delta=?,handle_delta=?,fairness_jain_millionths=?,
             controlled_degradation=?,regression_status=?,result_sha256=?,safe_outcome_code=?,
             revision=revision+1,completed_at=?,updated_at=?
           WHERE load_test_run_id=? AND status='running'""",
        (
            sample_count,
            int(result["p50_ms"]),
            int(result["p95_ms"]),
            int(result["p99_ms"]),
            int(result["throughput_rps_milli"]),
            int(result["error_rate_bps"]),
            int(result["cpu_peak_bps"]),
            int(result["memory_peak_mib"]),
            int(result["open_connection_delta"]),
            int(result["handle_delta"]),
            int(result["fairness_jain_millionths"]),
            1 if bool(result["controlled_degradation"]) else 0,
            regression,
            result_hash,
            safe_outcome,
            stamp,
            stamp,
            run_id,
        ),
    )
    if regression == "no_baseline":
        baseline_id = _id("pbl_")
        connection.execute(
            """INSERT INTO performance_baselines(
                 performance_baseline_id,service_code,scenario_code,environment_class,version,
                 sample_count,p50_ms,p95_ms,p99_ms,throughput_rps_milli,error_rate_bps,
                 cpu_peak_bps,memory_peak_mib,sample_window_seconds,result_sha256,
                 source_load_test_run_id,created_at)
               VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                baseline_id,
                row["service_code"],
                row["scenario_code"],
                row["environment_class"],
                sample_count,
                int(result["p50_ms"]),
                int(result["p95_ms"]),
                int(result["p99_ms"]),
                int(result["throughput_rps_milli"]),
                int(result["error_rate_bps"]),
                int(result["cpu_peak_bps"]),
                int(result["memory_peak_mib"]),
                int(row["duration_seconds"]),
                result_hash,
                run_id,
                stamp,
            ),
        )
    elif regression == "regressed" and baseline is not None:
        active = connection.execute(
            """SELECT 1 FROM performance_rollout_blocks WHERE service_code=?
                 AND scenario_code=? AND environment_class=? AND status='active'""",
            (row["service_code"], row["scenario_code"], row["environment_class"]),
        ).fetchone()
        if active is None:
            connection.execute(
                """INSERT INTO performance_rollout_blocks(
                     performance_rollout_block_id,service_code,scenario_code,environment_class,
                     load_test_run_id,performance_baseline_id,status,created_at,cleared_at)
                   VALUES(?,?,?,?,?,?,'active',?,NULL)""",
                (
                    _id("prb_"),
                    row["service_code"],
                    row["scenario_code"],
                    row["environment_class"],
                    run_id,
                    baseline["performance_baseline_id"],
                    stamp,
                ),
            )
        _event(
            connection,
            event_type="performance.regressed",
            service_code=str(row["service_code"]),
            scenario_code=str(row["scenario_code"]),
            outcome_code="release_blocked",
            aggregate={"run_id": run_id, "result_sha256": result_hash},
            request_id="capacity-runtime",
            trace_id="capacity-runtime",
            now=now,
        )
    elif regression == "within_baseline":
        connection.execute(
            """UPDATE performance_rollout_blocks SET status='cleared',cleared_at=?
               WHERE service_code=? AND scenario_code=? AND environment_class=?
                 AND status='active'""",
            (stamp, row["service_code"], row["scenario_code"], row["environment_class"]),
        )


def fail_load_test_in_transaction(connection, *, run_id: str, outcome_code: str) -> None:
    row = _load_row(connection, run_id)
    if row["status"] in {"succeeded", "failed", "stopped"}:
        return
    now = _database_now(connection)
    stamp = _db_time(_changed_time(now, row["updated_at"]))
    connection.execute(
        "UPDATE load_test_runs SET status='failed',safe_outcome_code=?,revision=revision+1,"
        "completed_at=?,updated_at=? WHERE load_test_run_id=?",
        (_safe_code(outcome_code), stamp, stamp, run_id),
    )


def stop_capacity_in_transaction(
    connection, *, job_id: str, operation: str, object_id: str,
    cancelled: bool, outcome_code: str,
) -> None:
    load = operation == "load_test"
    row = _load_row(connection, object_id) if load else _chaos_row(connection, object_id)
    if row["platform_job_id"] != job_id or row["status"] != "running":
        raise ApiError("capacity.runtime_conflict")
    if not cancelled:
        if load:
            fail_load_test_in_transaction(connection, run_id=object_id, outcome_code=outcome_code)
        else:
            fail_chaos_in_transaction(
                connection, experiment_id=object_id, outcome_code=outcome_code
            )
        return
    stamp = _db_time(_changed_time(_database_now(connection), row["updated_at"]))
    if load:
        connection.execute(
            "UPDATE load_test_runs SET status='stopped',safe_outcome_code=?,"
            "completed_at=?,updated_at=?,revision=revision+1 WHERE load_test_run_id=?",
            (_safe_code(outcome_code), stamp, stamp, object_id),
        )
    else:
        connection.execute(
            "UPDATE chaos_experiments SET status='killed',stop_reason_code='operator_kill',"
            "objective_met=0,completed_at=?,updated_at=?,revision=revision+1 "
            "WHERE chaos_experiment_id=?",
            (stamp, stamp, object_id),
        )


def start_chaos_in_transaction(connection, *, experiment_id: str, manifest_sha256: str):
    row = _chaos_row(connection, experiment_id)
    if row["status"] != "queued" or not hmac.compare_digest(
        str(row["fault_manifest_sha256"]), manifest_sha256
    ):
        raise ApiError("capacity.runtime_conflict")
    fault = connection.execute(
        "SELECT * FROM chaos_fault_catalog WHERE fault_code=?", (row["fault_code"],)
    ).fetchone()
    if fault is None or fault["status"] != "active" or not hmac.compare_digest(
        str(fault["manifest_sha256"]), manifest_sha256
    ):
        raise ApiError("capacity.chaos_unavailable")
    approvals = connection.execute(
        "SELECT * FROM chaos_approvals WHERE chaos_experiment_id=?",
        (experiment_id,),
    ).fetchall()
    required = {"execution"}
    if row["environment_class"] == "production":
        required.add("security_review")
    if {str(value["approval_kind"]) for value in approvals} != required:
        raise ApiError("capacity.approval_required")
    for approval in approvals:
        if not _approval_is_current(connection, approval):
            raise ApiError("capacity.approval_required")
    now = _database_now(connection)
    connection.execute(
        "UPDATE chaos_experiments SET status='running',revision=revision+1,started_at=?,"
        "updated_at=? WHERE chaos_experiment_id=? AND status='queued'",
        (
            _db_time(now),
            _db_time(_changed_time(now, row["updated_at"])),
            experiment_id,
        ),
    )
    return row


def complete_chaos_in_transaction(
    connection, *, experiment_id: str, result: dict[str, object]
) -> None:
    row = _chaos_row(connection, experiment_id)
    if row["status"] == "killed":
        return
    if row["status"] != "running":
        raise ApiError("capacity.runtime_conflict")
    observed_p95 = int(result["observed_p95_ms"])
    observed_error = int(result["observed_error_rate_bps"])
    hard_limit = observed_error >= int(row["stop_error_rate_bps"])
    objective_met = (
        bool(result["objective_met"])
        and observed_p95 <= int(row["objective_p95_ms"])
        and observed_error <= int(row["objective_error_rate_bps"])
        and not hard_limit
    )
    status = "succeeded" if objective_met else "failed"
    stop_reason = (
        "completed"
        if objective_met
        else "hard_limit"
        if hard_limit
        else "objective_missed"
    )
    result_hash = _digest(result)
    now = _database_now(connection)
    stamp = _db_time(_changed_time(now, row["updated_at"]))
    connection.execute(
        """UPDATE chaos_experiments SET status=?,observed_p95_ms=?,
             observed_error_rate_bps=?,objective_met=?,stop_reason_code=?,result_sha256=?,
             revision=revision+1,completed_at=?,updated_at=?
           WHERE chaos_experiment_id=? AND status='running'""",
        (
            status,
            observed_p95,
            observed_error,
            1 if objective_met else 0,
            stop_reason,
            result_hash,
            stamp,
            stamp,
            experiment_id,
        ),
    )
    if not objective_met:
        _event(
            connection,
            event_type="chaos.objective_missed",
            service_code=str(row["service_code"]),
            scenario_code=str(row["fault_code"]),
            outcome_code=stop_reason,
            aggregate={"experiment_id": experiment_id, "result_sha256": result_hash},
            request_id="capacity-runtime",
            trace_id="capacity-runtime",
            now=now,
        )


def fail_chaos_in_transaction(connection, *, experiment_id: str, outcome_code: str) -> None:
    row = _chaos_row(connection, experiment_id)
    if row["status"] in {"succeeded", "failed", "killed"}:
        return
    now = _database_now(connection)
    stamp = _db_time(_changed_time(now, row["updated_at"]))
    connection.execute(
        """UPDATE chaos_experiments SET status='failed',objective_met=0,
             stop_reason_code='runner_unavailable',result_sha256=?,revision=revision+1,
             completed_at=?,updated_at=? WHERE chaos_experiment_id=?""",
        (_digest({"outcome_code": _safe_code(outcome_code)}), stamp, stamp, experiment_id),
    )


def _create_chaos(
    connection,
    config: AppConfig,
    *,
    actor: PlatformContext,
    payload: ChaosCommandRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> str:
    if not ({"platform_ops", "platform_owner"} & set(actor.roles)):
        raise ApiError("platform.permission_denied")
    fault = connection.execute(
        "SELECT * FROM chaos_fault_catalog WHERE fault_code=?", (payload.fault_code,)
    ).fetchone()
    if fault is None or fault["status"] != "active":
        raise ApiError("capacity.chaos_fault_not_found")
    if fault["service_code"] != "shared" and fault["service_code"] != payload.service_code:
        raise ApiError("capacity.scope_conflict")
    if (
        int(payload.blast_tenant_count or 0) > int(fault["max_blast_tenants"])
        or int(payload.duration_seconds or 0) > int(fault["max_duration_seconds"])
    ):
        raise ApiError("capacity.limit_exceeded")
    if connection.execute(
        "SELECT 1 FROM platform_regions WHERE region_code=?", (payload.region_code,)
    ).fetchone() is None:
        raise ApiError("capacity.scope_not_found")
    experiment_id = _id("che_")
    status = "pending_approval" if payload.environment_class == "production" else "queued"
    job_id = None
    if status == "queued":
        prepared = prepare_platform_job(
            config,
            type_code="capacity.run",
            payload={
                "operation": "chaos",
                "object_id": experiment_id,
                "manifest_sha256": str(fault["manifest_sha256"]),
            },
        )
        job_id, _ = enqueue_prepared_platform_job_in_transaction(
            connection,
            config,
            prepared=prepared,
            priority=90,
            source_code="capacity.chaos",
            idempotency_key=f"capacity-chaos:{experiment_id}",
            request_id=request_id,
            trace_id=trace_id,
        )
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO chaos_experiments(
             chaos_experiment_id,fault_code,service_code,environment_class,region_code,
             blast_tenant_count,duration_seconds,objective_p95_ms,objective_error_rate_bps,
             stop_error_rate_bps,fault_manifest_sha256,status,platform_job_id,
             initiated_by_operator_id,kill_generation,observed_p95_ms,
             observed_error_rate_bps,objective_met,stop_reason_code,result_sha256,revision,
             started_at,completed_at,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,NULL,NULL,NULL,1,NULL,NULL,?,?)""",
        (
            experiment_id,
            payload.fault_code,
            payload.service_code,
            payload.environment_class,
            payload.region_code,
            payload.blast_tenant_count,
            payload.duration_seconds,
            payload.objective_p95_ms,
            payload.objective_error_rate_bps,
            payload.stop_error_rate_bps,
            fault["manifest_sha256"],
            status,
            job_id,
            actor.operator_id,
            stamp,
            stamp,
        ),
    )
    _chaos_approval(connection, experiment_id, actor, "execution", now)
    return experiment_id


def _approve_chaos(
    connection,
    config: AppConfig,
    *,
    actor: PlatformContext,
    experiment_id: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    row = _chaos_row(connection, experiment_id)
    if int(row["revision"]) != expected_revision:
        raise ApiError("capacity.revision_conflict")
    if row["status"] != "pending_approval" or row["environment_class"] != "production":
        raise ApiError("capacity.chaos_conflict")
    if row["initiated_by_operator_id"] == actor.operator_id:
        raise ApiError("capacity.approver_conflict")
    if not ({"platform_security", "platform_owner"} & set(actor.roles)):
        raise ApiError("platform.permission_denied")
    _chaos_approval(connection, experiment_id, actor, "security_review", now)
    prepared = prepare_platform_job(
        config,
        type_code="capacity.run",
        payload={
            "operation": "chaos",
            "object_id": experiment_id,
            "manifest_sha256": str(row["fault_manifest_sha256"]),
        },
    )
    job_id, _ = enqueue_prepared_platform_job_in_transaction(
        connection,
        config,
        prepared=prepared,
        priority=90,
        source_code="capacity.chaos",
        idempotency_key=f"capacity-chaos:{experiment_id}",
        request_id=request_id,
        trace_id=trace_id,
    )
    updated = connection.execute(
        "UPDATE chaos_experiments SET status='queued',platform_job_id=?,revision=revision+1,"
        "updated_at=? WHERE chaos_experiment_id=? AND status='pending_approval' AND revision=?",
        (
            job_id,
            _db_time(_changed_time(now, row["updated_at"])),
            experiment_id,
            expected_revision,
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("capacity.revision_conflict")


def _kill_chaos(
    connection,
    *,
    experiment_id: str,
    expected_revision: int,
    now: datetime,
) -> None:
    row = _chaos_row(connection, experiment_id)
    job = None
    if row["platform_job_id"] is not None:
        # Match the worker's root-job-first lock order before locking the domain record.
        job = connection.execute(
            "SELECT * FROM platform_jobs WHERE platform_job_id=?"
            + (" FOR UPDATE" if getattr(connection, "backend", "sqlite") == "postgresql" else ""),
            (row["platform_job_id"],),
        ).fetchone()
        row = _chaos_row(connection, experiment_id)
    if int(row["revision"]) != expected_revision:
        raise ApiError("capacity.revision_conflict")
    if row["status"] not in {"pending_approval", "queued", "running"}:
        raise ApiError("capacity.chaos_conflict")
    if row["stop_reason_code"] == "operator_kill":
        raise ApiError("capacity.chaos_conflict")
    stamp = _db_time(_changed_time(now, row["updated_at"]))
    pending_stop = job is not None and job["status"] in {"leased", "running", "cancel_requested"}
    updated = connection.execute(
        """UPDATE chaos_experiments SET status=?,kill_generation=kill_generation+1,
             stop_reason_code='operator_kill',revision=revision+1,completed_at=?,updated_at=?
           WHERE chaos_experiment_id=? AND revision=?""",
        (row["status"] if pending_stop else "killed", None if pending_stop else stamp,
         stamp, experiment_id, expected_revision),
    ).rowcount
    if updated != 1:
        raise ApiError("capacity.revision_conflict")
    if job is not None and job["status"] in {"queued", "leased", "running"}:
        terminal = job["status"] == "queued"
        connection.execute(
            "UPDATE platform_jobs SET status=?,safe_outcome_code=?,completed_at=?,"
            "revision=revision+1,updated_at=? WHERE platform_job_id=?",
            ("cancelled" if terminal else "cancel_requested",
             "platform_job.cancelled" if terminal else None, _db_time(now) if terminal else None,
             _db_time(_changed_time(now, job["updated_at"])), job["platform_job_id"]),
        )
        if terminal:
            record_platform_job_event(
                connection, job, "platform_job.completed", "platform_job.cancelled", now=now
            )


def _chaos_approval(
    connection,
    experiment_id: str,
    actor: PlatformContext,
    kind: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO chaos_approvals(
             chaos_approval_id,chaos_experiment_id,operator_id,approval_kind,
             authority_sha256,approved_at) VALUES(?,?,?,?,?,?)""",
        (
            _id("cap_"),
            experiment_id,
            actor.operator_id,
            kind,
            _authority_digest(actor.roles, actor.permissions),
            _db_time(now),
        ),
    )


def _approval_is_current(connection, approval) -> bool:
    roles = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT role_code FROM platform_operator_role_bindings "
            "WHERE operator_id=? AND status='active' ORDER BY role_code",
            (approval["operator_id"],),
        ).fetchall()
    )
    permissions = tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT permission.permission_code
               FROM platform_operator_role_bindings binding
               JOIN platform_capacity_role_permissions permission
                 ON permission.role_code=binding.role_code
               WHERE binding.operator_id=? AND binding.status='active'
               ORDER BY permission.permission_code""",
            (approval["operator_id"],),
        ).fetchall()
    )
    kind = str(approval["approval_kind"])
    required_role = {"platform_ops", "platform_owner"} if kind == "execution" else {
        "platform_security",
        "platform_owner",
    }
    return (
        "platform.capacity.chaos" in permissions
        and bool(required_role & set(roles))
        and hmac.compare_digest(
            str(approval["authority_sha256"]), _authority_digest(roles, permissions)
        )
    )


def _live_economics(
    connection, *, billing_period: str | None, currency: str | None
) -> tuple[UnitEconomicsInfo, ...]:
    clauses = []
    values: list[object] = []
    if billing_period is not None:
        clauses.append("billing_period=?")
        values.append(billing_period)
    if currency is not None:
        clauses.append("currency=?")
        values.append(currency)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    allocations = connection.execute(
        """SELECT billing_period,service_code,currency,allocation_version,
                  SUM(amount_micros) amount_micros,MAX(created_at) created_at
             FROM platform_cost_allocations"""
        + where
        + " GROUP BY billing_period,service_code,currency,allocation_version "
        "ORDER BY billing_period DESC,service_code,currency,allocation_version DESC",
        tuple(values),
    ).fetchall()
    result = []
    for row in allocations:
        if row["service_code"] == "gateway":
            usage = connection.execute(
                """SELECT COUNT(*) request_count,COALESCE(SUM(input_tokens),0) input_tokens,
                          COALESCE(SUM(output_tokens),0) output_tokens,
                          COALESCE(MAX(created_at),'none') watermark
                     FROM usage_ledger WHERE substr(created_at,1,7)=? AND status='recorded'""",
                (row["billing_period"],),
            ).fetchone()
            requests = int(usage["request_count"])
            input_tokens = int(usage["input_tokens"])
            output_tokens = int(usage["output_tokens"])
            source_watermark = str(usage["watermark"])
        else:
            requests = input_tokens = output_tokens = 0
            source_watermark = "none"
        total_tokens = input_tokens + output_tokens
        cost = int(row["amount_micros"])
        seed = (
            f"{row['billing_period']}:{row['service_code']}:{row['currency']}:"
            f"{row['allocation_version']}"
        )
        watermark = _digest(
            {"seed": seed, "usage": source_watermark, "request_count": requests}
        )
        result.append(
            UnitEconomicsInfo(
                unit_economics_snapshot_id="ues_" + hashlib.sha256(seed.encode()).hexdigest()[:32],
                billing_period=row["billing_period"],
                service_code=row["service_code"],
                request_count=requests,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                allocated_cost_micros=cost,
                cost_per_request_micros=(cost + requests // 2) // requests if requests else None,
                cost_per_million_tokens_micros=(
                    (cost * 1_000_000 + total_tokens // 2) // total_tokens
                    if total_tokens
                    else None
                ),
                currency=row["currency"],
                usage_watermark=watermark,
                allocation_version=int(row["allocation_version"]),
                created_at=_time(row["created_at"]),
            )
        )
    return tuple(result)


def _service_info(row) -> CapacityServiceInfo:
    demand = int(row["demand_rps"])
    capacity = int(row["capacity_rps"])
    utilization = demand * 10_000 // capacity
    threshold = int(row["threshold_bps"])
    return CapacityServiceInfo(
        service_code=row["service_code"],
        region_code=row["region_code"],
        demand_rps=demand,
        capacity_rps=capacity,
        utilization_bps=utilization,
        headroom_rps=capacity - demand,
        threshold_bps=threshold,
        threshold_reached=utilization >= threshold,
        slo_p95_ms=int(row["slo_p95_ms"]),
        recommendation_code=(
            "scale-within-30d"
            if utilization >= threshold
            else "scale-within-90d"
            if utilization >= max(5000, threshold - 1000)
            else "hold"
        ),
        revision=int(row["revision"]),
        updated_at=_time(row["updated_at"]),
    )


def _scenario_info(row) -> CapacityScenarioInfo:
    return CapacityScenarioInfo(
        scenario_code=row["scenario_code"],
        service_code=row["service_code"],
        run_kind=row["run_kind"],
        max_tenant_count=int(row["max_tenant_count"]),
        max_target_rps=int(row["max_target_rps"]),
        max_duration_seconds=int(row["max_duration_seconds"]),
        target_peak_multiplier_bps=int(row["target_peak_multiplier_bps"]),
        manifest_sha256=row["manifest_sha256"],
        status=row["status"],
    )


def _baseline_info(row) -> PerformanceBaselineInfo:
    return PerformanceBaselineInfo(
        performance_baseline_id=row["performance_baseline_id"],
        service_code=row["service_code"],
        scenario_code=row["scenario_code"],
        environment_class=row["environment_class"],
        version=int(row["version"]),
        sample_count=int(row["sample_count"]),
        p50_ms=int(row["p50_ms"]),
        p95_ms=int(row["p95_ms"]),
        p99_ms=int(row["p99_ms"]),
        throughput_rps_milli=int(row["throughput_rps_milli"]),
        error_rate_bps=int(row["error_rate_bps"]),
        created_at=_time(row["created_at"]),
    )


def _forecast_info(row) -> CapacityForecastInfo:
    return CapacityForecastInfo(
        capacity_forecast_id=row["capacity_forecast_id"],
        service_code=row["service_code"],
        region_code=row["region_code"],
        horizon_days=int(row["horizon_days"]),
        model_code=row["model_code"],
        predicted_demand_rps=_optional_int(row["predicted_demand_rps"]),
        predicted_capacity_rps=_optional_int(row["predicted_capacity_rps"]),
        confidence_bps=_optional_int(row["confidence_bps"]),
        error_outcome_bps=_optional_int(row["error_outcome_bps"]),
        recommendation_code=row["recommendation_code"],
        status=row["status"],
        job_id=row["platform_job_id"],
        revision=int(row["revision"]),
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _load_test_info(row) -> LoadTestRunInfo:
    return LoadTestRunInfo(
        load_test_run_id=row["load_test_run_id"],
        scenario_code=row["scenario_code"],
        service_code=row["service_code"],
        run_kind=row["run_kind"],
        environment_class=row["environment_class"],
        region_code=row["region_code"],
        synthetic_tenant_count=int(row["synthetic_tenant_count"]),
        target_rps=int(row["target_rps"]),
        duration_seconds=int(row["duration_seconds"]),
        status=row["status"],
        job_id=row["platform_job_id"],
        sample_count=_optional_int(row["sample_count"]),
        p50_ms=_optional_int(row["p50_ms"]),
        p95_ms=_optional_int(row["p95_ms"]),
        p99_ms=_optional_int(row["p99_ms"]),
        throughput_rps_milli=_optional_int(row["throughput_rps_milli"]),
        error_rate_bps=_optional_int(row["error_rate_bps"]),
        open_connection_delta=_optional_int(row["open_connection_delta"]),
        handle_delta=_optional_int(row["handle_delta"]),
        fairness_jain_millionths=_optional_int(row["fairness_jain_millionths"]),
        controlled_degradation=(
            bool(row["controlled_degradation"])
            if row["controlled_degradation"] is not None
            else None
        ),
        regression_status=row["regression_status"],
        safe_outcome_code=row["safe_outcome_code"],
        revision=int(row["revision"]),
        started_at=_optional_time(row["started_at"]),
        completed_at=_optional_time(row["completed_at"]),
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _fault_info(row) -> ChaosFaultInfo:
    return ChaosFaultInfo(
        fault_code=row["fault_code"],
        service_code=row["service_code"],
        max_blast_tenants=int(row["max_blast_tenants"]),
        max_duration_seconds=int(row["max_duration_seconds"]),
        manifest_sha256=row["manifest_sha256"],
        status=row["status"],
    )


def _chaos_info(connection, row) -> ChaosExperimentInfo:
    approvals = tuple(
        ChaosApprovalInfo(
            approval_kind=value["approval_kind"],
            operator_id=value["operator_id"],
            approved_at=_time(value["approved_at"]),
        )
        for value in connection.execute(
            "SELECT approval_kind,operator_id,approved_at FROM chaos_approvals "
            "WHERE chaos_experiment_id=? ORDER BY approved_at,approval_kind",
            (row["chaos_experiment_id"],),
        ).fetchall()
    )
    return ChaosExperimentInfo(
        chaos_experiment_id=row["chaos_experiment_id"],
        fault_code=row["fault_code"],
        service_code=row["service_code"],
        environment_class=row["environment_class"],
        region_code=row["region_code"],
        blast_tenant_count=int(row["blast_tenant_count"]),
        duration_seconds=int(row["duration_seconds"]),
        objective_p95_ms=int(row["objective_p95_ms"]),
        objective_error_rate_bps=int(row["objective_error_rate_bps"]),
        stop_error_rate_bps=int(row["stop_error_rate_bps"]),
        status=row["status"],
        job_id=row["platform_job_id"],
        approvals=approvals,
        kill_generation=int(row["kill_generation"]),
        observed_p95_ms=_optional_int(row["observed_p95_ms"]),
        observed_error_rate_bps=_optional_int(row["observed_error_rate_bps"]),
        objective_met=bool(row["objective_met"]) if row["objective_met"] is not None else None,
        stop_reason_code=row["stop_reason_code"],
        revision=int(row["revision"]),
        started_at=_optional_time(row["started_at"]),
        completed_at=_optional_time(row["completed_at"]),
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _rollout_block_info(row) -> PerformanceRolloutBlockInfo:
    return PerformanceRolloutBlockInfo(
        service_code=row["service_code"],
        scenario_code=row["scenario_code"],
        environment_class=row["environment_class"],
        load_test_run_id=row["load_test_run_id"],
        status=row["status"],
        created_at=_time(row["created_at"]),
        cleared_at=_optional_time(row["cleared_at"]),
    )


def _economics_info(row) -> UnitEconomicsInfo:
    return UnitEconomicsInfo(
        unit_economics_snapshot_id=row["unit_economics_snapshot_id"],
        billing_period=row["billing_period"],
        service_code=row["service_code"],
        request_count=int(row["request_count"]),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        total_tokens=int(row["total_tokens"]),
        allocated_cost_micros=int(row["allocated_cost_micros"]),
        cost_per_request_micros=_optional_int(row["cost_per_request_micros"]),
        cost_per_million_tokens_micros=_optional_int(
            row["cost_per_million_tokens_micros"]
        ),
        currency=row["currency"],
        usage_watermark=row["usage_watermark"],
        allocation_version=int(row["allocation_version"]),
        created_at=_time(row["created_at"]),
    )


def _forecast_by_id(connection, forecast_id: str) -> CapacityForecastInfo:
    return _forecast_info(_forecast_row(connection, forecast_id))


def _load_test_by_id(connection, run_id: str) -> LoadTestRunInfo:
    return _load_test_info(_load_row(connection, run_id))


def _chaos_by_id(connection, experiment_id: str) -> ChaosExperimentInfo:
    return _chaos_info(connection, _chaos_row(connection, experiment_id))


def _forecast_row(connection, forecast_id: str):
    row = connection.execute(
        "SELECT * FROM capacity_forecasts WHERE capacity_forecast_id=?", (forecast_id,)
    ).fetchone()
    if row is None:
        raise ApiError("capacity.forecast_not_found")
    return row


def _load_row(connection, run_id: str):
    row = connection.execute(
        "SELECT * FROM load_test_runs WHERE load_test_run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ApiError("capacity.load_test_not_found")
    return row


def _chaos_row(connection, experiment_id: str):
    row = connection.execute(
        "SELECT * FROM chaos_experiments WHERE chaos_experiment_id=?", (experiment_id,)
    ).fetchone()
    if row is None:
        raise ApiError("capacity.chaos_not_found")
    return row


def _command_replay(
    connection,
    actor: PlatformContext,
    *,
    idempotency_hash: str,
    request_hash: str,
    action: str,
):
    row = connection.execute(
        "SELECT * FROM capacity_commands WHERE operator_id=? AND idempotency_sha256=?",
        (actor.operator_id, idempotency_hash),
    ).fetchone()
    if row is not None and (
        row["action_code"] != action
        or not hmac.compare_digest(str(row["request_sha256"]), request_hash)
    ):
        raise ApiError("capacity.idempotency_conflict")
    return row


def _record_command(
    connection,
    *,
    actor: PlatformContext,
    action: str,
    idempotency_hash: str,
    request_hash: str,
    result_type: str,
    result_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO capacity_commands(
             capacity_command_id,operator_id,action_code,idempotency_sha256,request_sha256,
             result_object_type,result_object_id,created_at) VALUES(?,?,?,?,?,?,?,?)""",
        (
            _id("ccm_"),
            actor.operator_id,
            action,
            idempotency_hash,
            request_hash,
            result_type,
            result_id,
            _db_time(now),
        ),
    )


def _event(
    connection,
    *,
    event_type: str,
    service_code: str,
    scenario_code: str | None,
    outcome_code: str,
    aggregate: object,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO capacity_event_outbox(
             capacity_event_outbox_id,event_type,service_code,scenario_code,outcome_code,
             aggregate_sha256,status,attempt_count,request_id,trace_id,occurred_at,delivered_at)
           VALUES(?,?,?,?,?,?,'pending',0,?,?,?,NULL)""",
        (
            _id("ceo_"),
            event_type,
            service_code,
            scenario_code,
            outcome_code,
            _digest(aggregate),
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _audit(
    connection,
    *,
    actor: PlatformContext,
    action: str,
    object_type: str,
    object_id: str,
    reason: str,
    request_id: str,
    trace_id: str,
    after: object,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO platform_audit_logs(
             audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
             request_id,trace_id,before_sha256,after_sha256,occurred_at)
           VALUES(?,'platform',?,?,?,?,?,?,?,NULL,?,?)""",
        (
            _id("pau_"),
            actor.operator_id,
            action,
            object_type,
            object_id,
            reason,
            request_id,
            trace_id,
            _digest(after),
            _db_time(now),
        ),
    )


def _actor(
    connection,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str | None = None,
) -> PlatformContext:
    return authenticate_platform_session_in_transaction(
        connection,
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        touch=True,
    )


def _require(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("capacity.unavailable") from None


def _rollback(connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _valid_idempotency(value: str) -> None:
    if _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError("capacity.request_invalid")


def _database_now(connection) -> datetime:
    return _time(
        connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    )


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_time(value: object | None) -> datetime | None:
    return _time(value) if value is not None else None


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _secret_digest(config: AppConfig, domain: str, value: str) -> str:
    return hmac.new(
        str(config.platform_session_secret).encode(),
        f"prd113/{domain}/v1\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _authority_digest(roles, permissions) -> str:
    return _digest({"roles": sorted(roles), "permissions": sorted(permissions)})


def _safe_code(value: str) -> str:
    cleaned = "".join(
        character
        for character in value.lower()
        if character.isalnum() or character in "_.-"
    )
    return (cleaned or "capacity.runner_unavailable")[:96]


def _optional_int(value: object | None) -> int | None:
    return int(value) if value is not None else None


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


__all__ = [
    "command_chaos_experiment",
    "complete_chaos_in_transaction",
    "complete_forecast_in_transaction",
    "complete_load_test_in_transaction",
    "create_capacity_forecast",
    "create_load_test",
    "fail_chaos_in_transaction",
    "fail_forecast_in_transaction",
    "fail_load_test_in_transaction",
    "get_capacity_status",
    "get_unit_economics",
    "start_chaos_in_transaction",
    "start_forecast_in_transaction",
    "start_load_test_in_transaction",
]
