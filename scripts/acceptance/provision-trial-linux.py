"""Provision a loopback-only Ubuntu VM for real Linux deployment acceptance.

Uses an immutable verified cloud image and an SSH key unique to this workspace.
Never connects to the user's production database or opens a public listening port.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import urllib.request

import paramiko
import pycdlib
from storage_guard import require_test_capacity, watch_test_vm

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".tmp/trial-ubuntu"
IMAGE_ROOT = "https://cloud-images.ubuntu.com/releases/noble/release-20260926/"
IMAGE = "ubuntu-24.04-server-cloudimg-arm64.img"


def main() -> None:
    require_test_capacity(ROOT)
    # Existing disks belong to the reusable fixture. Do not rewrite cloud-init
    # media or create another VM just to repeat an acceptance run.
    if (WORK / "acceptance.qcow2").exists():
        raise RuntimeError("Acceptance disk already exists; use resume-trial-linux.py to reuse it.")
    WORK.mkdir(parents=True, exist_ok=True)
    target = WORK / IMAGE
    sums = urllib.request.urlopen(IMAGE_ROOT + "SHA256SUMS", timeout=60).read().decode()
    expected = next(line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip("*") == IMAGE)
    if not target.exists():
        print("Downloading official Ubuntu 24.04 LTS cloud image", flush=True)
        urllib.request.urlretrieve(IMAGE_ROOT + IMAGE, target)
    with target.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != expected:
        raise RuntimeError("Ubuntu cloud image checksum mismatch")
    key_file = WORK / "acceptance-key"
    if not key_file.exists():
        paramiko.RSAKey.generate(3072).write_private_key_file(str(key_file))
    key = paramiko.RSAKey.from_private_key_file(str(key_file))
    user_data = f"""#cloud-config
hostname: metahub-trial-validation
users:
  - name: acceptance
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - {key.get_name()} {key.get_base64()}
ssh_pwauth: false
disable_root: true
package_update: false
"""
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident="cidata")
    for name, contents in (("user-data", user_data), ("meta-data", "instance-id: metahub-trial-20261014\nlocal-hostname: metahub-trial-validation\n")):
        data = contents.encode()
        iso.add_fp(io.BytesIO(data), len(data), iso_path="/" + name.upper().replace("-", "_") + ";1", joliet_path="/" + name)
    iso.write(str(WORK / "seed.iso"))
    iso.close()
    disk = WORK / "acceptance.qcow2"
    qemu = ROOT / ".tools/qemu"
    if not disk.exists():
        subprocess.run([str(qemu / "qemu-img.exe"), "create", "-f", "qcow2", "-F", "qcow2", "-b", str(target), str(disk), "24G"], check=True)
    if (WORK / "vm.json").exists():
        previous = json.loads((WORK / "vm.json").read_text())
        # Never restart a VM solely because an SSH probe timed out.
        import psutil
        if psutil.pid_exists(previous["pid"]) and "qemu" in psutil.Process(previous["pid"]).name():
            print("Existing VM still running", flush=True)
            return
    command = [str(qemu / "qemu-system-aarch64.exe"), "-machine", "virt", "-cpu", "cortex-a72", "-accel", "tcg,thread=multi", "-smp", "2", "-m", "1536", "-bios", str(qemu / "share/edk2-aarch64-code.fd"), "-drive", f"file={disk},if=virtio,format=qcow2", "-drive", f"file={WORK / 'seed.iso'},if=virtio,media=cdrom,format=raw", "-netdev", "user,id=net0,hostfwd=tcp:127.0.0.1:2226-:22,hostfwd=tcp:127.0.0.1:8446-:443", "-device", "virtio-net-pci,netdev=net0", "-display", "none", "-chardev", f"file,id=serial0,path={WORK / 'serial.log'},append=on", "-serial", "chardev:serial0", "-monitor", "none"]
    with (WORK / "qemu.log").open("ab") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    watch_test_vm(process.pid)
    report = {"pid": process.pid, "image": IMAGE, "sha256": actual, "ssh": "127.0.0.1:2226", "https": "127.0.0.1:8446", "status": "booting", "memory_mib": 1536}
    (WORK / "vm.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
