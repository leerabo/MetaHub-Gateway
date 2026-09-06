"""Durable continuous availability observation, separate from GA approval."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from .product_qa_performance import _safe_target, run_loopback_smoke
    from .product_qa_stability_gate import _keep_system_awake
except ImportError:  # pragma: no cover - direct execution
    from product_qa_performance import _safe_target, run_loopback_smoke
    from product_qa_stability_gate import _keep_system_awake


REQUIRED_SECONDS = 72 * 60 * 60
MAXIMUM_GAP_SECONDS = 10.0
SCHEMA = "metahub.product-qa.continuous-availability.v1"


class Coverage:
    """Conservative sample boundaries include unobserved inter-window time."""

    def __init__(self, target_names: set[str]) -> None:
        self.target_names = target_names
        self.first: float | None = None
        self.last: float | None = None
        self.maximum_gap = 0.0
        self.requests = 0
        self.errors = 0
        self.windows = 0
        self.targets = {name: {"requests": 0, "errors": 0} for name in target_names}

    @property
    def observed_seconds(self) -> float:
        if self.first is None or self.last is None:
            return 0.0
        return max(0.0, self.last - self.first)

    def add(self, report: dict[str, Any], *, started: float, finished: float) -> None:
        first = report.get("first_observation_offset_seconds")
        last = report.get("last_observation_offset_seconds")
        wall = report["wall_seconds"]
        if (
            first is None or last is None
            or not all(math.isfinite(v) for v in (started, finished, first, last, wall))
            or not 0 <= first <= last <= wall + 0.001
            or wall > finished - started + 0.001
        ):
            raise ValueError("invalid_observation_boundaries")
        targets = {target["target"]: target for target in report["targets"]}
        if set(targets) != self.target_names or len(targets) != len(report["targets"]):
            raise ValueError("incomplete_target_coverage")
        if any(target["requests"] < 1 for target in targets.values()):
            raise ValueError("empty_target_coverage")
        if sum(target["requests"] for target in targets.values()) != report["requests"]:
            raise ValueError("request_count_mismatch")
        if sum(target["errors"] for target in targets.values()) != report["errors"]:
            raise ValueError("error_count_mismatch")
        # Sampler setup/summary time is not sampled. These bounds underestimate coverage.
        first_upper = finished - wall + first + 0.001
        last_lower = started + last - 0.001
        if self.first is None:
            self.first = first_upper
        if self.last is not None:
            if last_lower < self.last:
                raise ValueError("observation_clock_regressed")
            self.maximum_gap = max(self.maximum_gap, first_upper - self.last)
        self.last = last_lower
        self.maximum_gap = max(self.maximum_gap, report["max_observation_gap_seconds"])
        self.windows += 1
        self.requests += report["requests"]
        self.errors += report["errors"]
        for name, target in targets.items():
            self.targets[name]["requests"] += target["requests"]
            self.targets[name]["errors"] += target["errors"]

    def facts(self) -> dict[str, Any]:
        return {
            "observed_seconds": self.observed_seconds,
            "completed_windows": self.windows,
            "requests": self.requests,
            "errors": self.errors,
            "maximum_observation_gap_seconds": self.maximum_gap,
            "targets": dict(sorted(self.targets.items())),
        }


def _write_status(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(".pending")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def observe(
    *, raw_targets: list[str], output_dir: Path, revision: str,
    duration_seconds: float = REQUIRED_SECONDS, diagnostic: bool = False,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("invalid_source_revision")
    if not math.isfinite(duration_seconds) or duration_seconds < (
        1 if diagnostic else REQUIRED_SECONDS
    ):
        raise ValueError("continuous_observation_requires_72_hours")
    targets = [_safe_target(raw) for raw in raw_targets]
    names = {target.name for target in targets}
    if not targets or len(names) != len(targets):
        raise ValueError("targets_must_be_nonempty_and_unique")
    output_dir.mkdir(parents=True, exist_ok=False)
    coverage = Coverage(names)
    chain = "0" * 64
    report: dict[str, Any] = {
        "schema": SCHEMA, "status": "running", "diagnostic": diagnostic,
        "source_revision": revision,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "required_observed_seconds": duration_seconds,
        "ga_approval": False,
        "scope": (
            "Read-only local HTTP availability. "
            "Not business-flow, incident or independent approval evidence."
        ),
        **coverage.facts(),
    }
    _write_status(output_dir / "status.json", report)
    try:
        with _keep_system_awake() as awake, (output_dir / "windows.jsonl").open(
            "x", encoding="utf-8"
        ) as journal:
            report["sleep_prevention_active"] = awake
            while coverage.observed_seconds < duration_seconds:
                started = time.perf_counter()
                window = run_loopback_smoke(
                    targets, requests=1_000_000, concurrency=3,
                    duration_seconds=min(60, max(2, duration_seconds - coverage.observed_seconds)),
                    timeout_seconds=5, request_interval_seconds=0.5,
                    observation_bucket_seconds=10,
                )
                finished = time.perf_counter()
                coverage.add(window, started=started, finished=finished)
                record = {"sequence": coverage.windows, "previous_sha256": chain,
                          "completed_at_utc": datetime.now(UTC).isoformat(), "window": window}
                serialized = json.dumps(record, sort_keys=True, separators=(",", ":"))
                chain = hashlib.sha256(serialized.encode()).hexdigest()
                journal.write(serialized + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                report.update(coverage.facts(), journal_sha256=chain,
                              updated_at_utc=datetime.now(UTC).isoformat())
                if coverage.errors or coverage.maximum_gap > MAXIMUM_GAP_SECONDS:
                    report.update(status="failed", reason="availability_or_observation_gap_failed")
                _write_status(output_dir / "status.json", report)
                if report["status"] == "failed":
                    break
            else:
                report["status"] = "diagnostic_passed" if diagnostic else "passed"
    except BaseException as exc:
        report.update(status="failed", reason=type(exc).__name__)
        _write_status(output_dir / "status.json", report)
        raise
    report["finished_at_utc"] = datetime.now(UTC).isoformat()
    _write_status(output_dir / "status.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--duration-seconds", type=float, default=REQUIRED_SECONDS)
    parser.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args()
    report = observe(raw_targets=args.target, output_dir=args.output_dir,
                     revision=args.source_revision, duration_seconds=args.duration_seconds,
                     diagnostic=args.diagnostic)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"passed", "diagnostic_passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
