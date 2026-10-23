"""Small host-disk preflight shared by heavyweight local acceptance runners."""
from pathlib import Path
import shutil
import subprocess
import sys
import os

MIN_FREE_BYTES = 5 * 1024 ** 3


def require_test_capacity(workspace: Path) -> None:
    available = shutil.disk_usage(workspace).free
    if available < MIN_FREE_BYTES:
        raise RuntimeError(
            f"Acceptance stopped before allocating test data: "
            f"{available / 1024 ** 3:.2f} GiB free; at least 5 GiB required. "
            "Reuse or retire stopped test fixtures before retrying."
        )


def watch_test_vm(pid: int) -> None:
    import psutil
    process = psutil.Process(pid)
    watcher = Path(__file__).with_name("watch_test_vm_storage.py")
    for candidate in psutil.process_iter(["cmdline"]):
        try:
            args = candidate.info["cmdline"] or []
            if str(watcher) in args and "--pid" in args and args[args.index("--pid") + 1] == str(pid):
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied, IndexError):
            continue
    subprocess.Popen(
        [sys.executable, str(watcher),
         "--pid", str(pid), "--created", str(process.create_time())],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
