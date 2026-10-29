"""Actual dependency chaos, independently renewed leases and post-reversal probes."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime

import httpx

from .contracts import CapacityRunnerResult, execution_digest, result_digest
from .fault_client import FaultLeaseClient, fault_http_client
from .measurement import Aggregates, MeasuredExecution, _resource, probe_request


class _Stopped(Exception):
    def __init__(self, code):
        self.code = code


async def _samples(target, keys, rate, end, stopping, aggregate, *, count=None):
    tasks = set()
    sequence = 0
    due = time.monotonic()
    limits = httpx.Limits(
        max_connections=target.max_concurrency, max_keepalive_connections=target.max_concurrency
    )
    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        limits=limits,
        timeout=target.request_timeout_seconds,
    ) as client:

        async def sample(subject):
            started = time.monotonic()
            category = "unfinished"
            try:
                category = await probe_request(client, target, keys[subject])
            finally:
                aggregate.response(subject, category, time.monotonic() - started)

        try:
            while time.monotonic() < end:
                if stopping():
                    raise _Stopped("runner.cancelled")
                done = {task for task in tasks if task.done()}
                for task in done:
                    task.result()
                tasks.difference_update(done)
                if count is not None and sequence >= count:
                    if not tasks:
                        return
                elif len(tasks) < target.max_concurrency and time.monotonic() >= due:
                    aggregate.offered += 1
                    tasks.add(asyncio.create_task(sample(sequence % len(keys))))
                    sequence += 1
                    due = time.monotonic() + 1 / rate
                await asyncio.sleep(0.02)
            if count is not None:
                raise _Stopped("runner.chaos_probe_timeout")
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                if any(isinstance(item, Exception) for item in outcomes):
                    raise _Stopped("runner.chaos_probe_failed")


async def measure_chaos(claims, target, binding, effects, generation, stopping, progress):
    started = time.monotonic()
    keys, resource_key = target.credentials(claims.synthetic_tenant_count)
    target_digest, binding_digest = target.digest(), binding.digest()
    baseline, during = Aggregates(len(keys)), Aggregates(len(keys))
    recovery = Aggregates(len(keys))
    metrics = {
        "target_configuration_sha256": target_digest,
        "fault_binding_sha256": binding_digest,
        "stop_scope": "runner_requests",
    }
    prepared, failure, proof = False, None, None
    injected_ms = 0
    stop = asyncio.Event()
    watchers = []

    def elapsed():
        return max(0, int((time.monotonic() - started) * 1000))

    def healthy(aggregate):
        return (
            aggregate.completed >= len(keys)
            and aggregate.statuses["success"] == aggregate.completed
            and aggregate.percentile(95) <= claims.objective_p95_ms
        )

    async with fault_http_client() as management, fault_http_client() as resources:
        fault = FaultLeaseClient(management, binding, claims)
        try:
            during.resource(await _resource(resources, target, resource_key), elapsed())
            await _samples(
                target,
                keys,
                binding.probe_rps,
                time.monotonic() + 60,
                stopping,
                baseline,
                count=len(keys),
            )
            if not healthy(baseline):
                raise _Stopped("runner.chaos_baseline_failed")
            if stopping():
                raise _Stopped("runner.cancelled")
            effects.prepare(claims, generation, binding)
            prepared = True
            accepted = await fault.command("apply")
            if accepted is None:
                raise _Stopped("runner.fault_unavailable")
            fault_start = time.monotonic()
            fault_end = fault_start + min(
                claims.duration_seconds,
                max(0, (accepted.end_at - datetime.now(UTC)).total_seconds()),
            )
            errors = []

            async def renew():
                sequence = 1
                while not stop.is_set():
                    await asyncio.sleep(2)
                    if stop.is_set() or time.monotonic() >= fault_end - 0.1:
                        return
                    if stopping():
                        stop.set()
                        return
                    sequence += 1
                    try:
                        await fault.command("renew", sequence)
                    except ValueError:
                        errors.append("runner.fault_lease_lost")
                        stop.set()
                        return

            async def observe():
                next_resource = time.monotonic() + 1
                while not stop.is_set():
                    if stopping():
                        stop.set()
                        return
                    if during.completed and (
                        (during.completed - during.statuses["success"]) * 10000
                        > claims.stop_error_rate_bps * during.completed
                    ):
                        errors.append("runner.chaos_stop_threshold")
                        stop.set()
                        return
                    if time.monotonic() >= next_resource:
                        during.resource(await _resource(resources, target, resource_key), elapsed())
                        progress(during.completed, elapsed())
                        next_resource = time.monotonic() + 1
                    await asyncio.sleep(0.1)

            def watcher_failed(task):
                if not task.cancelled() and task.exception() is not None:
                    errors.append("runner.chaos_observer_failed")
                    stop.set()

            watchers = [asyncio.create_task(renew()), asyncio.create_task(observe())]
            for task in watchers:
                task.add_done_callback(watcher_failed)
            try:
                await _samples(
                    target,
                    keys,
                    binding.probe_rps,
                    fault_end,
                    lambda: stop.is_set() or stopping(),
                    during,
                )
            except _Stopped:
                raise _Stopped(errors[0] if errors else "runner.cancelled") from None
            finally:
                injected_ms = max(0, int((min(time.monotonic(), fault_end) - fault_start) * 1000))
                stop.set()
                for task in watchers:
                    task.cancel()
                await asyncio.gather(*watchers, return_exceptions=True)
            if errors:
                raise _Stopped(errors[0])
            if stopping():
                raise _Stopped("runner.cancelled")
            if during.completed < len(keys) or injected_ms < claims.duration_seconds * 1000 - 1000:
                raise _Stopped("runner.chaos_samples_missing")
        except _Stopped as exc:
            failure = exc.code
        except (httpx.HTTPError, TimeoutError, ValueError):
            failure = "runner.chaos_execution_failed"
        finally:
            stop.set()
            for task in watchers:
                task.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)
            if prepared:
                proof = await fault.release()
                if proof is not None:
                    effects.reversed(claims, generation, proof)
                    metrics["stop_scope"] = "runner_requests_and_fault"
                    metrics["fault_reversal"] = proof.model_dump(mode="json")
                else:
                    failure = "runner.fault_stop_unconfirmed"

        metrics.update(
            {
                "baseline_sample_count": baseline.completed,
                "fault_sample_count": during.completed,
                "fault_outcomes": dict(during.statuses),
                "fault_histogram_ms": dict(during.latencies),
                "injected_duration_ms": injected_ms,
            }
        )
        if failure is None and (
            proof is None
            or proof.state == "interrupted"
            or proof.reset_connections + proof.withheld_bytes < 1
        ):
            failure = "runner.chaos_fault_not_observed"
        recovery_start = time.monotonic()
        if failure is None:
            deadline = recovery_start + binding.recovery_timeout_seconds
            consecutive = 0
            try:
                while time.monotonic() < deadline and consecutive < 3:
                    round_samples = Aggregates(len(keys))
                    await _samples(
                        target,
                        keys,
                        binding.probe_rps,
                        deadline,
                        stopping,
                        round_samples,
                        count=len(keys),
                    )
                    recovery.completed += round_samples.completed
                    consecutive = consecutive + 1 if healthy(round_samples) else 0
                    if not consecutive:
                        await asyncio.sleep(0.2)
                if consecutive != 3:
                    raise _Stopped("runner.chaos_recovery_failed")
                during.resource(await _resource(resources, target, resource_key), elapsed())
            except _Stopped as exc:
                failure = exc.code
            except (httpx.HTTPError, TimeoutError, ValueError):
                failure = "runner.chaos_recovery_failed"
        if stopping():
            failure = "runner.cancelled"
        metrics["recovery_sample_count"] = recovery.completed
        metrics["target_recovered"] = failure is None
        metrics.update(
            {
                "resource_scope": "target_process",
                "resource_samples_sha256": during.resource_digest.hexdigest(),
                "resource_sample_count": during.resource_count,
                "resource_windows": during.resource_windows,
                "cpu_peak_bps": during.cpu_peak,
                "memory_peak_bytes": during.memory_peak,
            }
        )
        progress(during.completed, elapsed())
        if failure:
            return MeasuredExecution(
                "cancelled" if failure == "runner.cancelled" else "failed", failure, None, metrics
            )
        error_rate = math.ceil(
            (during.completed - during.statuses["success"]) * 10000 / during.completed
        )
        p95 = during.percentile(95)
        objective_met = (
            p95 <= claims.objective_p95_ms and error_rate <= claims.objective_error_rate_bps
        )
        outcome = "runner.chaos_objective_met" if objective_met else "runner.chaos_objective_missed"
        value = {
            "contract_version": 2,
            "evidence_kind": "measured",
            "execution_sha256": execution_digest(claims),
            "manifest_sha256": claims.manifest_sha256,
            "operation": "chaos",
            "object_id": claims.object_id,
            "safe_outcome_code": outcome,
            "observed_p95_ms": p95,
            "observed_error_rate_bps": error_rate,
            "objective_met": objective_met,
            "chaos_evidence": {
                "baseline_sample_count": baseline.completed,
                "fault_sample_count": during.completed,
                "recovery_sample_count": recovery.completed,
                "reset_connections": proof.reset_connections,
                "withheld_bytes": proof.withheld_bytes,
                "injected_duration_ms": injected_ms,
                "recovery_duration_ms": max(0, int((time.monotonic() - recovery_start) * 1000)),
                "fault_binding_sha256": binding_digest,
                "target_configuration_sha256": target_digest,
                "fault_state": proof.state,
                "requests_stopped": True,
                "fault_reversed": True,
                "target_recovered": True,
            },
        }
        return MeasuredExecution(
            "succeeded",
            outcome,
            CapacityRunnerResult(**value, result_sha256=result_digest(value)),
            metrics,
        )
