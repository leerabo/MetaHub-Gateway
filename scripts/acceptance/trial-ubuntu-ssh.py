"""Run acceptance commands in our own Ubuntu VM, pinning its console host key."""
from __future__ import annotations

import argparse
import base64
from pathlib import Path
import sys

import paramiko

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".tmp/trial-ubuntu"


def connect() -> paramiko.SSHClient:
    line = next(line for line in (WORK / "serial.log").read_text(errors="replace").splitlines() if line.startswith("ssh-ed25519 "))
    key = paramiko.Ed25519Key(data=base64.b64decode(line.split()[1]))
    client = paramiko.SSHClient()
    client.get_host_keys().add("[127.0.0.1]:2226", "ssh-ed25519", key)
    # TCG image builds can delay the guest's sshd without terminating the build.
    # Bound observation separately from the operation; a slow banner must not
    # cause callers to restart a still-running deployment.
    client.connect("127.0.0.1", port=2226, username="acceptance", key_filename=str(WORK / "acceptance-key"), timeout=180, banner_timeout=180, auth_timeout=120)
    return client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", type=Path)
    parser.add_argument("--command")
    parser.add_argument("--upload", type=Path)
    parser.add_argument("--download", type=Path)
    parser.add_argument("--remote")
    args = parser.parse_args()
    client = connect()
    try:
        if args.upload:
            with client.open_sftp() as sftp:
                sftp.put(str(args.upload), args.remote)
            return 0
        if args.download:
            with client.open_sftp() as sftp:
                sftp.get(args.remote, str(args.download))
            return 0
        stdin, stdout, stderr = client.exec_command(args.command or "bash -se", timeout=1800)
        if args.script:
            stdin.write(args.script.read_text(encoding="utf-8"))
            stdin.flush()
        stdin.channel.shutdown_write()
        channel = stdout.channel
        while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
            if channel.recv_ready():
                sys.stdout.write(channel.recv(65536).decode(errors="replace"))
                sys.stdout.flush()
            if channel.recv_stderr_ready():
                sys.stderr.write(channel.recv_stderr(65536).decode(errors="replace"))
                sys.stderr.flush()
            import select
            select.select([channel], [], [], 0.2)
        return channel.recv_exit_status()
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
