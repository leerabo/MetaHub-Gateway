"""Real Linux command-group cleanup; this does not claim a full Docker restore."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise ValueError("Requires actual Linux process groups")
    results = []
    with tempfile.TemporaryDirectory(prefix="metahub-recovery-interrupt-") as temporary:
        folder = Path(temporary)
        workload = folder / "workload.py"
        workload.write_text(
            "import os,signal,subprocess,sys,time\n"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            "with open(sys.argv[1],'a') as output: output.write(str(os.getpid())+'\\n')\n"
            "if len(sys.argv)==2: subprocess.Popen([sys.executable,__file__,sys.argv[1],'child'])\n"
            "time.sleep(120)\n"
        )
        runner = folder / "runner.py"
        runner.write_text(
            "import importlib.util,signal,subprocess,sys\n"
            "spec=importlib.util.spec_from_file_location('recovery',sys.argv[1])\n"
            "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n"
            "signal.signal(signal.SIGTERM,module.interrupted)\n"
            "try:\n"
            " module.execute([sys.executable,sys.argv[2],sys.argv[3]],timeout=3 if sys.argv[4]=='timeout' else 120)\n"
            "except subprocess.TimeoutExpired:\n"
            " if sys.argv[4]!='timeout': raise\n"
            " sys.exit(75)\n"
        )
        for mode in ("timeout", "sigterm"):
            pidfile = folder / (mode + ".pids")
            started = time.monotonic()
            parent = subprocess.Popen(
                [sys.executable, str(runner), str(args.controller.resolve()),
                 str(workload), str(pidfile), mode],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            pids = []
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if pidfile.exists():
                        pids = [int(value) for value in pidfile.read_text().splitlines()]
                    if len(pids) == 2 and all(running(pid) for pid in pids):
                        break
                    if parent.poll() is not None:
                        raise AssertionError("Fixture runner exited before live workloads")
                    time.sleep(0.1)
                else:
                    raise AssertionError("Fixture workloads did not start")
                if mode == "sigterm":
                    os.kill(parent.pid, signal.SIGTERM)
                _, error = parent.communicate(timeout=45)
                expected = 75 if mode == "timeout" else 143
                if parent.returncode != expected:
                    raise AssertionError("Recovery command exited outside expected failure path")
                deadline = time.monotonic() + 5
                while any(running(pid) for pid in pids) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if any(running(pid) for pid in pids):
                    raise AssertionError("An ignoring descendant survived cleanup")
                results.append(dict(mode=mode, live_descendants_before_signal=len(pids),
                                    active_descendants_after_cleanup=0,
                                    elapsed_seconds=round(time.monotonic() - started, 3)))
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                for pid in pids:
                    if running(pid):
                        os.kill(pid, signal.SIGKILL)
    print(json.dumps(dict(status="passed", platform="Linux", checks=results,
                          real_process_groups=True, docker_restore_executed=False)))


if __name__ == "__main__":
    main()
