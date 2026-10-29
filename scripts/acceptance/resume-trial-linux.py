"""Resume the existing private acceptance disk; never provision or reset data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import psutil
from storage_guard import require_test_capacity, watch_test_vm

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".tmp/trial-ubuntu"


def available_commit():
    if os.name != "nt":
        return psutil.virtual_memory().available + psutil.swap_memory().free
    import ctypes
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in (
                "total_physical", "available_physical", "total_commit", "available_commit",
                "total_virtual", "available_virtual", "extended",
            )
        ]

    value = MemoryStatus()
    value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        raise ValueError("Cannot establish available host commit capacity")
    return value.available_commit


def main():
    require_test_capacity(ROOT)
    report_file = WORK / "vm.json"
    previous = json.loads(report_file.read_text())
    disk = WORK / "acceptance.qcow2"
    image = WORK / previous["image"]
    for path in (disk, image, WORK / "seed.iso", WORK / "acceptance-key"):
        if not path.is_file():
            raise ValueError("Existing acceptance assets are missing; refusing to initialize replacements")
    # A timed-out SSH probe is never a reason to kill a living QEMU process.
    for process in psutil.process_iter(("pid", "name", "cmdline")):
        if "qemu-system" not in (process.info["name"] or "").lower():
            continue
        command = process.info["cmdline"]
        if command is None:
            raise ValueError("Cannot establish ownership of an existing QEMU process")
        if any(str(disk).lower() in argument.lower() for argument in command):
            watch_test_vm(process.pid)
            print(json.dumps({"status": "already_running", "pid": process.pid}))
            return
    with image.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != previous["sha256"]:
            raise ValueError("Retained base image no longer matches its recorded checksum")
    physical = psutil.virtual_memory().available
    commit = available_commit()
    print(json.dumps({"available_host_physical_mib": physical // 1048576,
                      "available_host_commit_mib": commit // 1048576}), flush=True)
    # Windows may page idle working sets. Keep the existing guest size; this
    # emulated fixture is never evidence of native production performance.
    if physical < 512 * 1024 * 1024 or commit < 3 * 1024 * 1024 * 1024:
        raise ValueError("Insufficient host physical/commit capacity to resume the retained fixture")
    qemu = ROOT / ".tools/qemu"
    command = [str(qemu / "qemu-system-aarch64.exe"), "-machine", "virt", "-cpu", "cortex-a72",
               "-accel", "tcg,thread=multi", "-smp", "4", "-m", "1536",
               "-bios", str(qemu / "share/edk2-aarch64-code.fd"),
               "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
               "-drive", f"file={WORK / 'seed.iso'},if=virtio,media=cdrom,format=raw",
               "-netdev", "user,id=net0,hostfwd=tcp:127.0.0.1:2226-:22,hostfwd=tcp:127.0.0.1:8446-:443",
               "-device", "virtio-net-pci,netdev=net0", "-display", "none",
               "-chardev", f"file,id=serial0,path={WORK / 'serial.log'},append=on",
               "-serial", "chardev:serial0", "-monitor", "none",
               "-qmp", "tcp:127.0.0.1:4446,server=on,wait=off"]
    with (WORK / "qemu.log").open("ab") as log:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    previous.update(pid=child.pid, status="resumed_existing_disk", cpu_count=4, memory_mib=1536)
    watch_test_vm(child.pid)
    report_file.write_text(json.dumps(previous, indent=2))
    print(json.dumps({"status": "resumed_existing_disk", "pid": child.pid,
                      "database_initialized": False, "public_listener_enabled": False}))


if __name__ == "__main__":
    main()
