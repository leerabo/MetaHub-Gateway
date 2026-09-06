"""Run Product QA performance probes against an isolated local service stack."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
for source_root in reversed(
    (
        ROOT,
        ROOT / "shared-sdk" / "python",
        ROOT / "gateway-data-plane" / "src",
        ROOT / "control-plane-api" / "src",
    )
):
    sys.path.insert(0, str(source_root))

from metahub_shared.db import apply_migrations, apply_seed_data  # noqa: E402

from scripts.quality.product_qa_performance import (  # noqa: E402
    _safe_target,
    run_loopback_smoke,
)

PYTHON = Path(sys.executable)
NODE = Path(r"C:\Program Files\nodejs\node.exe")
TEMP_ROOT = ROOT / ".tmp" / "product-qa-performance"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_http(url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 35
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("product_qa_performance_service_exited")
        try:
            with opener.open(url, timeout=1) as response:  # fixed loopback URL
                if response.status == 200:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.15)
    raise RuntimeError("product_qa_performance_service_timeout")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Windows venv launchers can own a separate interpreter process.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--duration-seconds", type=float, default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observation-dir", type=Path)
    parser.add_argument("--source-revision")
    parser.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args()
    if args.observation_dir and not args.source_revision:
        parser.error("--observation-dir requires --source-revision")
    return args


def main() -> int:
    args = _arguments()
    run_id = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(4)}"
    run_root = TEMP_ROOT / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    database_url = f"sqlite:///{(run_root / 'runtime.db').as_posix()}"
    seed_password = secrets.token_urlsafe(32)
    previous_seed_password = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = seed_password
    try:
        migration = apply_migrations(database_url)
        apply_seed_data(database_url)
    finally:
        if previous_seed_password is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous_seed_password
    if not migration.applied or migration.applied[-1] != "0139":
        raise RuntimeError("product_qa_performance_migration_invalid")

    control_port, gateway_port, console_port = _free_port(), _free_port(), _free_port()
    source_roots = (
        ROOT / "shared-sdk" / "python",
        ROOT / "gateway-data-plane" / "src",
        ROOT / "control-plane-api" / "src",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(str(path) for path in source_roots),
            "METAHUB_ENV": "dev",
            "METAHUB_DATABASE_URL": database_url,
            "METAHUB_SEED_ADMIN_PASSWORD": seed_password,
            "METAHUB_AUTH_TOKEN_SECRET": secrets.token_urlsafe(48),
            "METAHUB_ALLOWED_ORIGINS": f"http://127.0.0.1:{console_port}",
            "METAHUB_CONTROL_API_BASE_URL": f"http://127.0.0.1:{control_port}",
            "METAHUB_GATEWAY_API_BASE_URL": f"http://127.0.0.1:{gateway_port}",
            "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": "false",
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_CONSOLE_PORT": str(console_port),
        }
    )
    logs = {
        name: (run_root / f"{name}.log").open("wb")
        for name in ("control", "gateway", "console")
    }
    processes: list[subprocess.Popen[bytes]] = []
    try:
        control = subprocess.Popen(
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "metahub_control.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(control_port),
                "--no-access-log",
            ],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=logs["control"],
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        processes.append(control)
        gateway = subprocess.Popen(
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "metahub_gateway.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(gateway_port),
                "--no-access-log",
            ],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=logs["gateway"],
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        processes.append(gateway)
        console = subprocess.Popen(
            [str(NODE), "scripts/dev-server.mjs"],
            cwd=ROOT / "console-web",
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=logs["console"],
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        processes.append(console)
        _wait_http(f"http://127.0.0.1:{control_port}/health", control)
        _wait_http(f"http://127.0.0.1:{gateway_port}/health", gateway)
        _wait_http(f"http://127.0.0.1:{console_port}/", console)
        raw_targets = [
            f"control=http://127.0.0.1:{control_port}/health",
            f"gateway=http://127.0.0.1:{gateway_port}/health",
            f"console=http://127.0.0.1:{console_port}/",
        ]
        if args.observation_dir:
            from scripts.quality.product_qa_continuous_stability import observe

            report = observe(
                raw_targets=raw_targets, output_dir=args.observation_dir,
                revision=args.source_revision, duration_seconds=args.duration_seconds,
                diagnostic=args.diagnostic,
            )
            passed = report["status"] in {"passed", "diagnostic_passed"}
        else:
            report = run_loopback_smoke(
                [_safe_target(target) for target in raw_targets],
                requests=args.requests,
                concurrency=args.concurrency,
                duration_seconds=args.duration_seconds,
                timeout_seconds=3,
            )
            passed = report["errors"] == 0
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "errors": report["errors"],
                    "passed": passed,
                    "requests": report["requests"],
                },
                sort_keys=True,
            )
        )
        return 0 if passed else 1
    finally:
        for process in reversed(processes):
            _stop(process)
        for log in logs.values():
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
