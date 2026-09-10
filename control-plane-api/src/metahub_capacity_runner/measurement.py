"""Bounded aggregate measurements of actual requests and target-process telemetry."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from .contracts import (
    CapacityPermitClaims,
    CapacityResourceSample,
    CapacityRunnerResult,
    execution_digest,
    result_digest,
)
from .settings import CapacityTarget


@dataclass(frozen=True)
class MeasuredExecution:
    state: str
    outcome: str
    result: CapacityRunnerResult | None
    metrics: dict


class Aggregates:
    def __init__(self, subject_count: int):
        self.latencies: Counter[int] = Counter()
        self.statuses: Counter[str] = Counter()
        self.subject_success = [0] * subject_count
        self.offered = 0
        self.dropped = 0
        self.completed = 0
        self.latency_max = 0
        self.resource_count = 0
        self.resource_digest = hashlib.sha256()
        self.resource_windows: dict[int, dict[str, int]] = {}
        self.first: CapacityResourceSample | None = None
        self.last: CapacityResourceSample | None = None
        self.cpu_peak = 0
        self.memory_peak = 0

    def response(self, subject: int, category: str, elapsed: float):
        latency = max(1, math.ceil(elapsed * 1000))
        self.latencies[min(60_000, latency)] += 1
        self.latency_max = max(self.latency_max, latency)
        self.completed += 1
        self.statuses[category] += 1
        if category == "success":
            self.subject_success[subject] += 1

    def percentile(self, percentile: int) -> int:
        rank = max(1, (self.completed * percentile + 99) // 100)
        total = 0
        for latency, count in sorted(self.latencies.items()):
            total += count
            if total >= rank:
                return self.latency_max if latency == 60_000 else latency
        raise ValueError("runner.samples_missing")

    def resource(self, sample: CapacityResourceSample, elapsed_ms: int):
        if (
            sample.sampled_at.tzinfo is None
            or abs((datetime.now(UTC) - sample.sampled_at).total_seconds()) > 10
        ):
            raise ValueError("runner.resource_unavailable")
        if self.last is not None:
            delta_ms = (sample.sampled_at - self.last.sampled_at).total_seconds() * 1000
            if (
                sample.process_sha256 != self.last.process_sha256
                or delta_ms <= 0
                or sample.cpu_time_ms < self.last.cpu_time_ms
                or sample.processor_count != self.last.processor_count
            ):
                raise ValueError("runner.resource_changed")
            cpu = math.ceil(
                (sample.cpu_time_ms - self.last.cpu_time_ms)
                * 10_000
                / (delta_ms * sample.processor_count)
            )
            self.cpu_peak = max(self.cpu_peak, min(10_000, cpu))
        self.first = self.first or sample
        self.last = sample
        self.resource_count += 1
        self.memory_peak = max(self.memory_peak, sample.memory_rss_bytes)
        self.resource_digest.update(sample.model_dump_json().encode())
        # A 72-hour permit has at most 4322 minute windows, including request drain.
        minute = elapsed_ms // 60_000
        if minute > 4321:
            raise ValueError("runner.duration_exceeded")
        window = self.resource_windows.setdefault(
            minute,
            {
                "count": 0,
                "memory_peak_bytes": 0,
                "connection_peak": 0,
                "handle_peak": 0,
            },
        )
        window["count"] += 1
        window["memory_peak_bytes"] = max(window["memory_peak_bytes"], sample.memory_rss_bytes)
        window["connection_peak"] = max(window["connection_peak"], sample.open_connections)
        window["handle_peak"] = max(window["handle_peak"], sample.handle_count)

    def evidence(self, elapsed_ms: int, target: CapacityTarget) -> dict:
        return {
            "observed_duration_ms": elapsed_ms,
            "offered_count": self.offered,
            "not_dispatched_count": self.dropped,
            "completed_count": self.completed,
            "outcomes": dict(self.statuses),
            "latency_histogram_ms": dict(self.latencies),
            "latency_max_ms": self.latency_max,
            "subject_success_counts": self.subject_success,
            "resource_sample_count": self.resource_count,
            "resource_samples_sha256": self.resource_digest.hexdigest(),
            "resource_windows": self.resource_windows,
            "target_configuration_sha256": target.digest(),
            "resource_scope": "target_process",
            "stop_scope": "runner_requests",
        }

    def result(self, claims: CapacityPermitClaims, elapsed_ms: int) -> CapacityRunnerResult:
        if self.completed < 1 or self.resource_count < 2 or self.first is None or self.last is None:
            raise ValueError("runner.samples_missing")
        successes = sum(self.subject_success)
        squares = sum(count * count for count in self.subject_success)
        fairness = (
            successes * successes * 1_000_000 // (len(self.subject_success) * squares)
            if squares
            else 0
        )
        result = {
            "contract_version": 2,
            "evidence_kind": "measured",
            "execution_sha256": execution_digest(claims),
            "manifest_sha256": claims.manifest_sha256,
            "operation": claims.operation,
            "object_id": claims.object_id,
            "safe_outcome_code": "runner.load_completed",
            "sample_count": self.completed,
            "p50_ms": self.percentile(50),
            "p95_ms": self.percentile(95),
            "p99_ms": self.percentile(99),
            "throughput_rps_milli": successes * 1_000_000 // max(1, elapsed_ms),
            "error_rate_bps": math.ceil((self.completed - successes) * 10_000 / self.completed),
            "cpu_peak_bps": self.cpu_peak,
            "memory_peak_mib": math.ceil(self.memory_peak / (1024 * 1024)),
            "open_connection_delta": max(
                0, self.last.open_connections - self.first.open_connections
            ),
            "handle_delta": max(0, self.last.handle_count - self.first.handle_count),
            "fairness_jain_millionths": fairness,
            "controlled_degradation": self.statuses["unexpected"] == 0 and self.dropped == 0,
        }
        return CapacityRunnerResult(**result, result_sha256=result_digest(result))


async def _resource(client: httpx.AsyncClient, target: CapacityTarget, token: str):
    async with asyncio.timeout(3):
        async with client.stream(
            "GET",
            target.resource_origin + "/internal/capacity/telemetry",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if response.status_code != 200:
                raise ValueError("runner.resource_unavailable")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunks) + len(chunk) > 8192:
                    raise ValueError("runner.resource_unavailable")
                chunks.extend(chunk)
            return CapacityResourceSample.model_validate_json(chunks)


async def measure_load(
    claims: CapacityPermitClaims,
    target: CapacityTarget,
    stopping: Callable[[], bool],
    progress: Callable[[int, int], None],
) -> MeasuredExecution:
    keys, resource_key = target.credentials(claims.synthetic_tenant_count)
    aggregates = Aggregates(claims.synthetic_tenant_count)
    stop = asyncio.Event()
    errors: list[str] = []
    queue: asyncio.Queue[tuple[int, float]] = asyncio.Queue(maxsize=target.max_concurrency)
    start = time.monotonic()

    def observed_ms():
        return max(0, int((time.monotonic() - start) * 1000))

    tasks: list[asyncio.Task] = []
    cancelled = False
    limits = httpx.Limits(
        max_connections=target.max_concurrency, max_keepalive_connections=target.max_concurrency
    )
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=3) as resources:
        try:
            if stopping():
                return MeasuredExecution("cancelled", "runner.cancelled", None, {})
            aggregates.resource(await _resource(resources, target, resource_key), 0)
            if stopping():
                return MeasuredExecution("cancelled", "runner.cancelled", None, {})
            if claims.expires_at <= datetime.now(UTC):
                return MeasuredExecution("failed", "runner.permit_expired", None, {})

            async def observe():
                last_resource = time.monotonic()
                last_progress = last_resource
                while not stop.is_set():
                    if stopping():
                        stop.set()
                        return
                    now = time.monotonic()
                    if now - last_resource >= 1:
                        try:
                            aggregates.resource(
                                await _resource(resources, target, resource_key), observed_ms()
                            )
                        except (httpx.HTTPError, TimeoutError, ValueError):
                            errors.append("runner.resource_unavailable")
                            stop.set()
                            return
                        last_resource = now
                    if now - last_progress >= 5:
                        progress(aggregates.completed, observed_ms())
                        last_progress = now
                    await asyncio.sleep(0.1)

            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                limits=limits,
                timeout=target.request_timeout_seconds,
            ) as client:

                async def worker():
                    while True:
                        subject, queued_at = await queue.get()
                        category = "unexpected"
                        try:
                            headers = {}
                            body = None
                            if target.service_code == "gateway":
                                method, path = "POST", "/v1/chat/completions"
                                headers["Authorization"] = f"Bearer {keys[subject]}"
                                body = {
                                    "model": target.model_name,
                                    "stream": False,
                                    "max_tokens": 8,
                                    "messages": [
                                        {
                                            "role": "user",
                                            "content": "MetaHub synthetic capacity probe.",
                                        }
                                    ],
                                }
                            elif target.service_code == "control":
                                method, path = "GET", "/workspace/session"
                                headers["Cookie"] = (
                                    f"__Host-metahub-workspace-session={keys[subject]}"
                                )
                            else:
                                method, path = "GET", "/public/status"
                            async with asyncio.timeout(target.request_timeout_seconds):
                                async with client.stream(
                                    method, target.origin + path, headers=headers, json=body
                                ) as response:
                                    category = (
                                        "success"
                                        if 200 <= response.status_code < 300
                                        else "limited"
                                        if response.status_code == 429
                                        else "unavailable"
                                        if response.status_code == 503
                                        else "unexpected"
                                    )
                                    size = 0
                                    async for chunk in response.aiter_raw():
                                        size += len(chunk)
                                        if size > 131_072:
                                            category = "unexpected"
                                            break
                        except (httpx.HTTPError, TimeoutError):
                            category = "unexpected"
                        except asyncio.CancelledError:
                            aggregates.statuses["cancelled"] += 1
                            raise
                        else:
                            aggregates.response(subject, category, time.monotonic() - queued_at)
                            continue
                        finally:
                            queue.task_done()
                        aggregates.response(subject, category, time.monotonic() - queued_at)

                tasks = [asyncio.create_task(worker()) for _ in range(target.max_concurrency)]
                observer = asyncio.create_task(observe())
                tasks.append(observer)

                def failed_task(task):
                    if not task.cancelled() and task.exception() is not None:
                        errors.append("runner.execution_failed")
                        stop.set()

                for task in tasks:
                    task.add_done_callback(failed_task)
                try:
                    start = time.monotonic()
                    end = start + claims.duration_seconds
                    sequence = 0
                    while time.monotonic() < end and not stop.is_set():
                        due = start + sequence / claims.target_rps
                        delay = due - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(min(0.1, delay))
                            continue
                        aggregates.offered += 1
                        if queue.full():
                            aggregates.dropped += 1
                        else:
                            queue.put_nowait((sequence % len(keys), time.monotonic()))
                        sequence += 1
                        await asyncio.sleep(0)
                    if stop.is_set():
                        cancelled = not errors
                    else:
                        drain = asyncio.create_task(queue.join())
                        cancelled_wait = asyncio.create_task(stop.wait())
                        try:
                            await asyncio.wait(
                                (drain, cancelled_wait), return_when=asyncio.FIRST_COMPLETED
                            )
                            cancelled = stop.is_set() and not errors
                        finally:
                            for task in (drain, cancelled_wait):
                                task.cancel()
                            await asyncio.gather(drain, cancelled_wait, return_exceptions=True)
                finally:
                    stop.set()
                    for task in tasks:
                        task.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    if any(isinstance(value, Exception) for value in results):
                        errors.append("runner.execution_failed")
            # Target request connections have been closed before the final resource sample.
            await asyncio.sleep(0.1)
            aggregates.resource(await _resource(resources, target, resource_key), observed_ms())
            elapsed = observed_ms()
            progress(aggregates.completed, elapsed)
            metrics = aggregates.evidence(elapsed, target)
            if errors:
                return MeasuredExecution("failed", errors[0], None, metrics)
            if cancelled:
                return MeasuredExecution("cancelled", "runner.cancelled", None, metrics)
            return MeasuredExecution(
                "succeeded", "runner.load_completed", aggregates.result(claims, elapsed), metrics
            )
        except (httpx.HTTPError, TimeoutError, ValueError):
            return MeasuredExecution(
                "failed",
                "runner.resource_unavailable",
                None,
                aggregates.evidence(observed_ms(), target),
            )
