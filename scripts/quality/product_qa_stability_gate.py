"""Configuration-driven PERF long-stability gate with honest not_run output."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import sys
import time
from pathlib import Path
from typing import Any

try:
    from .product_qa_performance import _safe_target, run_loopback_smoke
except ImportError:  # pragma: no cover - direct script execution
    from product_qa_performance import _safe_target, run_loopback_smoke


DEFAULT_CONFIG = Path(__file__).with_name("product_qa_stability.json")
SCHEMA = "metahub.product-qa.stability-report.v1"
WINDOWS_EXECUTION_STATE_CONTINUOUS = 0x80000000
WINDOWS_EXECUTION_STATE_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def _keep_system_awake() -> Any:
    """Prevent automatic Windows sleep for an active stability observation."""

    if sys.platform != "win32":
        yield False
        return
    kernel32 = ctypes.windll.kernel32
    requested = WINDOWS_EXECUTION_STATE_CONTINUOUS | WINDOWS_EXECUTION_STATE_SYSTEM_REQUIRED
    if kernel32.SetThreadExecutionState(requested) == 0:
        raise OSError("stability sleep prevention could not be enabled")
    try:
        yield True
    finally:
        kernel32.SetThreadExecutionState(WINDOWS_EXECUTION_STATE_CONTINUOUS)


def _load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema") != "metahub.product-qa.stability-config.v1":
        raise ValueError("unsupported stability configuration schema")
    if not isinstance(config.get("targets"), list) or not config["targets"]:
        raise ValueError("stability configuration requires targets")
    if not isinstance(config.get("profiles"), dict):
        raise ValueError("stability configuration requires profiles")
    return config


def _not_run_report(profile_name: str, profile: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "profile": profile_name,
        "status": "not_run",
        "reason": reason,
        "configured_duration_seconds": profile["duration_seconds"],
        "minimum_observed_seconds": profile["minimum_observed_seconds"],
        "requests": 0,
        "errors": 0,
        "targets": [],
    }


def run_gate(
    *,
    config_path: Path = DEFAULT_CONFIG,
    profile_name: str,
    run_long: bool = False,
) -> dict[str, Any]:
    """Run a configured profile or return an explicit not_run report."""

    config = _load_config(config_path)
    profile = config["profiles"].get(profile_name)
    if not isinstance(profile, dict):
        raise ValueError(f"unknown stability profile: {profile_name}")
    if profile_name == "long_stability" and not run_long:
        return _not_run_report(
            profile_name,
            profile,
            "4-hour stability requires explicit --run-long in a dedicated QA window",
        )

    targets = [_safe_target(str(raw)) for raw in config["targets"]]
    started_at = time.time()
    with _keep_system_awake() as sleep_prevention_active:
        result = run_loopback_smoke(
            targets,
            requests=int(profile["requests"]),
            concurrency=int(profile["concurrency"]),
            duration_seconds=float(profile["duration_seconds"]),
            timeout_seconds=float(profile["timeout_seconds"]),
            request_interval_seconds=float(profile.get("request_interval_seconds", 0)),
            observation_bucket_seconds=float(profile.get("observation_bucket_seconds", 0)),
        )
    observed_seconds = float(result["wall_seconds"])
    passed = (
        result["errors"] == 0
        and observed_seconds >= float(profile["minimum_observed_seconds"])
        and result["requests"] > 0
        and float(result["max_observation_gap_seconds"])
        <= float(profile["maximum_observation_gap_seconds"])
    )
    return {
        "schema": SCHEMA,
        "profile": profile_name,
        "status": "passed" if passed else "failed",
        "started_unix_seconds": int(started_at),
        "configured_duration_seconds": profile["duration_seconds"],
        "minimum_observed_seconds": profile["minimum_observed_seconds"],
        "observed_seconds": observed_seconds,
        "requests": result["requests"],
        "successes": result["successes"],
        "errors": result["errors"],
        "error_counts": result["error_counts"],
        "system_error_counts": result["system_error_counts"],
        "throughput_rps": result["throughput_rps"],
        "max_observation_gap_seconds": result["max_observation_gap_seconds"],
        "sleep_prevention_active": sleep_prevention_active,
        "targets": result["targets"],
        **(
            {
                "observation_bucket_seconds": result["observation_bucket_seconds"],
                "time_buckets": result["time_buckets"],
            }
            if "time_buckets" in result
            else {}
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", choices=("short_stability", "long_stability"), required=True
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-long", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_gate(
            config_path=args.config,
            profile_name=args.profile,
            run_long=args.run_long,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = {
            "schema": SCHEMA,
            "profile": args.profile,
            "status": "failed",
            "reason": str(exc),
        }
    serialized = json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["status"] in {"passed", "not_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
