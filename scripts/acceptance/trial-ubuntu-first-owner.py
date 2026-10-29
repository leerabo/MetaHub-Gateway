"""Enroll our Ubuntu fixture's owner through its real interactive terminal.

No credentials are printed. Local acceptance credentials are sealed with the
current Windows user's DPAPI; they are never part of a deployment package.
"""
import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import struct
import time

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".tmp/trial-ubuntu"
CREDENTIALS = WORK / "first-owner-credentials.dpapi"


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise ValueError("Local acceptance credential storage requires Windows DPAPI")
    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def save_credentials(value):
    temporary = CREDENTIALS.with_suffix(".pending")
    temporary.write_bytes(protect(json.dumps(value).encode()))
    temporary.replace(CREDENTIALS)


def load_credentials():
    return json.loads(protect(CREDENTIALS.read_bytes(), decrypt=True))


def totp(secret, epoch):
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", int(epoch) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"


def main():
    global CREDENTIALS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="/opt/metahub-trial-state")
    parser.add_argument("--install-receipt")
    parser.add_argument("--credentials-name", default=CREDENTIALS.name)
    parser.add_argument("--report-name", default="first-owner-ubuntu-validation.json")
    args = parser.parse_args()
    if not re.fullmatch(r"/opt/[A-Za-z0-9_./-]+", args.state):
        raise ValueError("Use a task-owned deployment state under /opt")
    if any(part in {".", ".."} for part in args.state.split("/")):
        raise ValueError("Invalid deployment state path")
    for value, suffix in ((args.credentials_name, ".dpapi"), (args.report_name, ".json")):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", value) or not value.endswith(suffix):
            raise ValueError("Use a local acceptance output filename")
    if args.state != "/opt/metahub-trial-state" and not args.install_receipt:
        raise ValueError("Another acceptance state requires its actual install/restart receipt")
    if args.install_receipt and not re.fullmatch(r"/home/acceptance/[A-Za-z0-9_.-]+-result[.]json", args.install_receipt):
        raise ValueError("Use the task-owned terminal install receipt")
    CREDENTIALS = WORK / args.credentials_name
    if CREDENTIALS.exists():
        raise ValueError("Acceptance enrollment material already exists; refusing to overwrite")
    spec = importlib.util.spec_from_file_location("ubuntu_ssh", ROOT / "scripts/acceptance/trial-ubuntu-ssh.py")
    ssh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ssh)
    client = ssh.connect()
    value = dict(email="ubuntu-owner@example.invalid", name="Ubuntu acceptance owner",
                 password="IndependentUbuntu!" + secrets.token_urlsafe(36), status="pending")
    started = time.monotonic()
    try:
        if args.install_receipt:
            # Require a terminal success for this exact state, not the old
            # installation's marker or a partially written progress receipt.
            check = (
                "import json; from pathlib import Path; "
                f"r=json.loads(Path({args.install_receipt!r}).read_text()); "
                "assert r['status']=='formal_first_install_and_restart_passed'; "
                "assert r['first_install_verified'] and r['restart_verified']; "
                f"assert r['state']=={args.state!r}"
            )
            readiness = "sudo python3 -c " + shlex.quote(check)
        else:
            readiness = "test -f /home/acceptance/data-verification-install-passed"
        _, out, _ = client.exec_command(readiness, timeout=60)
        if out.channel.recv_exit_status() != 0:
            raise ValueError("Real Ubuntu installation has not passed")
        save_credentials(value)
        _, active, _ = client.exec_command("sudo cat " + shlex.quote(args.state + "/current-release"), timeout=60)
        release=active.read().decode().strip()
        if active.channel.recv_exit_status()!=0 or not re.fullmatch(r'/opt/[A-Za-z0-9_./-]+',release):
            raise ValueError("Invalid task-owned installed release path")
        command = "sudo bash " + shlex.quote(release + "/scripts/deploy/metahubctl.sh") + " first-owner " + shlex.quote(args.state)
        stdin, stdout, stderr = client.exec_command(command, get_pty=True, timeout=900)
        channel = stdout.channel
        buffer = ""
        stage = 0
        prompts = ["First-owner email: ", "Display name: ", "New password (at least 8 characters): ",
                   "Confirm new password: "]
        while time.monotonic() - started < 900:
            if channel.recv_ready():
                buffer += channel.recv(65536).decode(errors="replace")
                if len(buffer) > 262144:
                    raise ValueError("Unexpected enrollment output size")
            if stage < len(prompts) and prompts[stage] in buffer:
                answer = [value["email"], value["name"], value["password"], value["password"]][stage]
                channel.sendall((answer + "\n").encode())
                stage += 1
            if channel.exit_status_ready() and not channel.recv_ready():
                break
            time.sleep(0.05)
        else:
            raise TimeoutError("Interactive enrollment did not finish")
        status = channel.recv_exit_status()
        if status != 0 or stage != len(prompts) or '"status": "initialized"' not in buffer:
            raise ValueError("Interactive enrollment was refused or did not finish")
        value["status"] = "initialized"
        save_credentials(value)
        report = dict(status="passed", platform="Ubuntu Docker, real interactive first-owner command",
                      state=args.state, install_receipt=args.install_receipt,
                      actual_terminal_prompts_completed=True, generated_password=True,
                      password_only_initialization=True, fixed_otp_used=False, development_seed_applied=False,
                      local_credentials_storage="Windows user-scoped DPAPI", original_accounts_modified=False,
                      browser_login_verified=False, elapsed_seconds=round(time.monotonic() - started, 3))
        (ROOT / "output/deployment-ubuntu-20261014" / args.report_name).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    except Exception as error:
        print(json.dumps(dict(status="failed", phase="ubuntu_interactive_enrollment", exception_type=type(error).__name__)))
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
