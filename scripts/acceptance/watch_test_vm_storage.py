"""Pause only the owned acceptance VM before its disk exhausts the host."""
from pathlib import Path
import argparse
import json
import shutil
import time
import psutil
from storage_guard import MIN_FREE_BYTES

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".tmp/trial-ubuntu"
MAX_VM_DISK_BYTES = 20 * 1024 ** 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--created", type=float, required=True)
    args = parser.parse_args()
    disk = WORK / "acceptance.qcow2"
    while psutil.pid_exists(args.pid):
        try:
            process = psutil.Process(args.pid)
            if abs(process.create_time() - args.created) > 0.01:
                return
            command = [v.replace("\\", "/").lower() for v in process.cmdline()]
            if "qemu-system" not in process.name().lower() or not any(
                v.startswith("file=" + disk.as_posix().lower() + ",") for v in command
            ):
                return
            free = shutil.disk_usage(ROOT).free
            if free < MIN_FREE_BYTES or disk.stat().st_size >= MAX_VM_DISK_BYTES:
                process.suspend()
                (WORK / "storage-pause.json").write_text(json.dumps({
                    "status": "paused_before_host_capacity_exhaustion",
                    "pid": args.pid, "free_bytes": free,
                    "disk_bytes": disk.stat().st_size,
                    "minimum_free_bytes": MIN_FREE_BYTES,
                    "maximum_vm_disk_bytes": MAX_VM_DISK_BYTES,
                    "data_deleted": False,
                }, indent=2) + "\n")
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
