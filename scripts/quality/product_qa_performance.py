"""Safe product-QA performance and compatibility smoke runner.

The runner is intentionally read-only against a running service: it accepts
only loopback HTTP GET targets without query strings, never sends credentials,
and writes only aggregated latency/status facts.  It is suitable for a local
baseline, not a substitute for a dedicated production load environment.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, urlparse

import httpx

DEFAULT_TARGETS = (
    "control=http://127.0.0.1:8001/health",
    "gateway=http://127.0.0.1:8000/health",
    "console=http://127.0.0.1:5173/",
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class Target:
    """A validated read-only local HTTP target."""

    name: str
    url: str


@dataclass(frozen=True)
class Sample:
    """A response observation that deliberately excludes the response body."""

    target: str
    elapsed_ms: float
    status_code: int | None
    error_kind: str | None
    observed_offset_seconds: float = 0.0
    system_error_code: str | None = None


def _safe_target(raw: str) -> Target:
    """Parse ``name=http://127.0.0.1:port/path`` without accepting secrets."""

    if "=" not in raw:
        raise ValueError("target must use name=http://loopback/path")
    name, url = raw.split("=", 1)
    parsed: ParseResult = urlparse(url)
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("target name must be alphanumeric, hyphen, or underscore")
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("only http loopback targets are permitted")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("credentials, query strings, and fragments are not permitted")
    if not parsed.path.startswith("/"):
        raise ValueError("target path must begin with /")
    return Target(name=name, url=url)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[position], 3)


def _snake_case_type_name(exception: BaseException) -> str:
    """Return a stable exception class label without retaining its message."""

    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(exception).__name__).lower()


def _classify_http_error(exception: httpx.HTTPError) -> tuple[str, str | None]:
    """Classify an HTTPX failure using only safe type and numeric OS facts."""

    system_error_code: str | None = None
    current: BaseException | None = exception
    visited: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in visited:
            break
        visited.add(id(current))
        if isinstance(current, OSError):
            winerror = getattr(current, "winerror", None)
            errno = getattr(current, "errno", None)
            if isinstance(winerror, int):
                system_error_code = f"winerror_{winerror}"
                break
            if isinstance(errno, int):
                system_error_code = f"errno_{errno}"
                break
        current = current.__cause__ or current.__context__
    return _snake_case_type_name(exception), system_error_code


def _sample_succeeded(sample: Sample) -> bool:
    return (
        sample.error_kind is None
        and sample.status_code is not None
        and 200 <= sample.status_code < 300
    )


def _sample_error_kind(sample: Sample) -> str | None:
    if sample.error_kind:
        return sample.error_kind
    if sample.status_code is not None and not 200 <= sample.status_code < 300:
        return f"http_status_{sample.status_code}"
    return None


def _time_buckets(samples: list[Sample], *, bucket_seconds: float) -> list[dict[str, Any]]:
    """Aggregate chronology without retaining per-request details."""

    buckets: dict[int, list[Sample]] = {}
    for sample in samples:
        bucket_index = max(0, int(sample.observed_offset_seconds // bucket_seconds))
        buckets.setdefault(bucket_index, []).append(sample)

    result: list[dict[str, Any]] = []
    for bucket_index in sorted(buckets):
        bucket_samples = buckets[bucket_index]
        errors = [sample for sample in bucket_samples if not _sample_succeeded(sample)]
        target_counts: dict[str, dict[str, int]] = {}
        for sample in bucket_samples:
            target = target_counts.setdefault(sample.target, {"requests": 0, "errors": 0})
            target["requests"] += 1
            if not _sample_succeeded(sample):
                target["errors"] += 1
        result.append(
            {
                "start_offset_seconds": round(bucket_index * bucket_seconds, 3),
                "requests": len(bucket_samples),
                "successes": len(bucket_samples) - len(errors),
                "errors": len(errors),
                "error_counts": dict(
                    sorted(
                        Counter(
                            error_kind
                            for sample in errors
                            if (error_kind := _sample_error_kind(sample)) is not None
                        ).items()
                    )
                ),
                "system_error_counts": dict(
                    sorted(
                        Counter(
                            sample.system_error_code
                            for sample in errors
                            if sample.system_error_code
                        ).items()
                    )
                ),
                "targets": dict(sorted(target_counts.items())),
            }
        )
    return result


def summarize(
    samples: list[Sample], *, wall_seconds: float, observation_bucket_seconds: float = 0.0
) -> dict[str, Any]:
    """Return a redacted, JSON-serializable aggregate report."""

    by_target: dict[str, list[Sample]] = {}
    for sample in samples:
        by_target.setdefault(sample.target, []).append(sample)
    targets: list[dict[str, Any]] = []
    for name in sorted(by_target):
        target_samples = by_target[name]
        latencies = [sample.elapsed_ms for sample in target_samples]
        status_counts = Counter(
            str(sample.status_code) for sample in target_samples if sample.status_code is not None
        )
        error_counts = Counter(
            error_kind
            for sample in target_samples
            if (error_kind := _sample_error_kind(sample)) is not None
        )
        system_error_counts = Counter(
            sample.system_error_code for sample in target_samples if sample.system_error_code
        )
        success_count = sum(
            1
            for sample in target_samples
            if _sample_succeeded(sample)
        )
        targets.append(
            {
                "target": name,
                "requests": len(target_samples),
                "successes": success_count,
                "errors": len(target_samples) - success_count,
                "status_counts": dict(sorted(status_counts.items())),
                "error_counts": dict(sorted(error_counts.items())),
                "system_error_counts": dict(sorted(system_error_counts.items())),
                "latency_ms": {
                    "min": round(min(latencies), 3) if latencies else None,
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                    "p99": _percentile(latencies, 0.99),
                    "max": round(max(latencies), 3) if latencies else None,
                },
            }
        )
    total = len(samples)
    successes = sum(item["successes"] for item in targets)
    report = {
        "schema": "metahub.product-qa.performance.v1",
        "mode": "loopback-read-only-http",
        "wall_seconds": round(wall_seconds, 3),
        "requests": total,
        "successes": successes,
        "errors": total - successes,
        "error_counts": dict(
            sorted(
                Counter(
                    error_kind
                    for sample in samples
                    if (error_kind := _sample_error_kind(sample)) is not None
                ).items()
            )
        ),
        "system_error_counts": dict(
            sorted(
                Counter(
                    sample.system_error_code for sample in samples if sample.system_error_code
                ).items()
            )
        ),
        "throughput_rps": round(total / wall_seconds, 3) if wall_seconds else 0.0,
        "targets": targets,
    }
    if observation_bucket_seconds > 0:
        report["observation_bucket_seconds"] = observation_bucket_seconds
        report["time_buckets"] = _time_buckets(
            samples, bucket_seconds=observation_bucket_seconds
        )
    return report


def run_loopback_smoke(
    targets: list[Target],
    *,
    requests: int,
    concurrency: int,
    duration_seconds: float,
    timeout_seconds: float,
    request_interval_seconds: float = 0.0,
    observation_bucket_seconds: float = 0.0,
) -> dict[str, Any]:
    """Cycle validated targets with bounded concurrency and no response-body reads."""

    if (
        requests < 1
        or concurrency < 1
        or duration_seconds < 0
        or timeout_seconds <= 0
        or request_interval_seconds < 0
        or observation_bucket_seconds < 0
    ):
        raise ValueError("requests/concurrency must be positive; duration must be non-negative")
    if not targets:
        raise ValueError("at least one target is required")
    samples: list[Sample] = []
    sample_lock = threading.Lock()
    sequence_lock = threading.Lock()
    next_index = 0
    deadline: float | None = None
    started = 0.0
    ready = threading.Barrier(concurrency + 1)
    start_gate = threading.Event()
    last_observation_completed: float | None = None
    max_observation_gap_seconds = 0.0

    def worker() -> None:
        nonlocal last_observation_completed, max_observation_gap_seconds, next_index
        with httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            ready.wait()
            start_gate.wait()
            while True:
                with sequence_lock:
                    if next_index >= requests or (
                        deadline is not None and time.perf_counter() >= deadline
                    ):
                        return
                    target = targets[next_index % len(targets)]
                    next_index += 1
                observation_started = time.perf_counter()
                try:
                    response = client.get(target.url, headers={"X-Request-ID": "qa_perf_health"})
                    sample = Sample(
                        target=target.name,
                        elapsed_ms=(time.perf_counter() - observation_started) * 1000,
                        status_code=response.status_code,
                        error_kind=None,
                        observed_offset_seconds=time.perf_counter() - started,
                    )
                except httpx.TimeoutException as exc:
                    error_kind, system_error_code = _classify_http_error(exc)
                    sample = Sample(
                        target.name,
                        (time.perf_counter() - observation_started) * 1000,
                        None,
                        error_kind,
                        time.perf_counter() - started,
                        system_error_code,
                    )
                except httpx.HTTPError as exc:
                    error_kind, system_error_code = _classify_http_error(exc)
                    sample = Sample(
                        target.name,
                        (time.perf_counter() - observation_started) * 1000,
                        None,
                        error_kind,
                        time.perf_counter() - started,
                        system_error_code,
                    )
                observation_completed = time.perf_counter()
                with sample_lock:
                    if last_observation_completed is not None:
                        max_observation_gap_seconds = max(
                            max_observation_gap_seconds,
                            observation_completed - last_observation_completed,
                        )
                    last_observation_completed = observation_completed
                    samples.append(sample)
                if request_interval_seconds:
                    time.sleep(request_interval_seconds)

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="metahub-qa-perf") as pool:
        futures = [pool.submit(worker) for _ in range(concurrency)]
        ready.wait()
        started = time.perf_counter()
        deadline = started + duration_seconds if duration_seconds else None
        start_gate.set()
        for future in futures:
            future.result()
    report = summarize(
        samples,
        wall_seconds=time.perf_counter() - started,
        observation_bucket_seconds=observation_bucket_seconds,
    )
    report["max_observation_gap_seconds"] = round(max_observation_gap_seconds, 3)
    offsets = [sample.observed_offset_seconds for sample in samples]
    report["first_observation_offset_seconds"] = min(offsets) if offsets else None
    report["last_observation_offset_seconds"] = max(offsets) if offsets else None
    return report


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="read-only loopback target; repeat to override the default target set",
    )
    parser.add_argument("--requests", type=int, default=30, help="maximum aggregate request count")
    parser.add_argument("--concurrency", type=int, default=3, help="bounded worker count")
    parser.add_argument(
        "--duration-seconds", type=float, default=0.0, help="optional early stop duration"
    )
    parser.add_argument("--timeout-seconds", type=float, default=3.0, help="per-request timeout")
    parser.add_argument("--output", type=Path, help="write only the redacted aggregate JSON report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv or sys.argv[1:])
    try:
        raw_targets = args.target or list(DEFAULT_TARGETS)
        report = run_loopback_smoke(
            [_safe_target(item) for item in raw_targets],
            requests=args.requests,
            concurrency=args.concurrency,
            duration_seconds=args.duration_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    except ValueError as exc:
        print(json.dumps({"schema": "metahub.product-qa.performance.v1", "error": str(exc)}))
        return 2
    serialized = json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if report["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
