"""Root host worker: serialize owner requests for signed, locally verified releases."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import stat
from uuid import uuid4

spec = importlib.util.spec_from_file_location("github_releases", Path(__file__).with_name("github-releases.py"))
releases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(releases)
JOB = re.compile(r"[0-9]{8}T[0-9]{6}_[0-9a-f]{32}")
OPERATIONS = {"upgrade", "restart"}


def run_bounded(command, timeout=7200, grace=30):
    """Bound the whole host process group, including Compose CLI children."""
    child = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return subprocess.CompletedProcess(command, child.wait(timeout=timeout))
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The shell can exit before an ignoring grandchild; reap its entire
            # group even when wait() already reports the shell as terminated.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        raise


def read_request(path):
    """Read bounded regular-file input without following application-owned links."""
    if not JOB.fullmatch(path.stem) or path.is_symlink():
        raise ValueError("Invalid update request file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
            raise ValueError("Invalid update request file.")
        payload = source.read(4097)
    if len(payload) > 4096:
        raise ValueError("Oversized update request.")
    job = json.loads(payload)
    if not isinstance(job, dict) or set(job) not in ({"job_id", "version", "account_id", "requested_at"}, {"job_id", "operation", "version", "account_id", "requested_at"}):
        raise ValueError("Invalid update request fields.")
    # Requests created before the explicit operation field remain upgrade jobs.
    job.setdefault("operation", "upgrade")
    if (job["operation"] not in OPERATIONS
            or job["job_id"] != path.stem or not isinstance(job["version"], str)
            or len(job["version"]) > 64 or not releases.VERSION.fullmatch(job["version"])
            or not isinstance(job["account_id"], str) or not 1 <= len(job["account_id"]) <= 128
            or any(ord(character) < 32 for character in job["account_id"])
            or not isinstance(job["requested_at"], str) or len(job["requested_at"]) > 64):
        raise ValueError("Invalid update request fields.")
    requested_at = datetime.fromisoformat(job["requested_at"])
    if requested_at.tzinfo is None:
        raise ValueError("Update request timestamp must include a timezone.")
    return job


def retain_request(updates, request):
    # Preserve failed/malformed/completed inputs outside the pending queue. Move
    # links themselves, never their targets; the application cannot read this dir.
    retained = updates / "retained-requests"
    retained.mkdir(mode=0o700, exist_ok=True)
    releases.check_owned(retained)
    os.replace(request, retained / (uuid4().hex + ".json"))


def write_status(status_root, job_id, version, outcome, message, *, operation="upgrade"):
    releases.atomic_json(status_root / (job_id + ".json"), {
        "job_id": job_id, "operation": operation, "version": version, "status": outcome,
        "message": message, "updated_at": datetime.now(UTC).isoformat(),
    })


def process(config_path):
    import fcntl  # Host execution remains Linux-only; parsing tests need no flock.
    releases.check_owned(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    updates, store, state = (Path(config[name]) for name in ("updates", "release_store", "state"))
    for path in (updates, store, state):
        releases.check_owned(path)
    with (state / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if (state / "recovery-in-progress").exists():
            # Cold recovery activates fresh volumes under a new state path.
            # Do not let the old timer address that stack using its old config.
            return
        status_root = updates / "status"
        releases.check_owned(status_root)
        for status in status_root.glob("*.json"):
            releases.check_owned(status)
            if json.loads(status.read_text(encoding="utf-8")).get("status") in {"running", "failed"}:
                # An interrupted or failed migration requires explicit recovery.
                # Never start another upgrade against an uncertain database.
                return
        for request in sorted((updates / "requests").glob("*.json"))[:256]:
            previous = status_root / request.name
            if JOB.fullmatch(request.stem) and previous.exists():
                releases.check_owned(previous)
                if json.loads(previous.read_text(encoding="utf-8")).get("status") in {"completed", "rejected", "recovered"}:
                    retain_request(updates, request)
                    continue
            try:
                job = read_request(request)
            except (OSError, ValueError, TypeError):
                if JOB.fullmatch(request.stem):
                    write_status(status_root, request.stem, "", "rejected", "升级请求无效；未执行安装或修改数据库。")
                retain_request(updates, request)
                continue
            version = job["version"]
            operation = job["operation"]
            if operation == "restart":
                active = Path((state / "current-release").read_text().strip())
                releases.check_owned(active)
                controller = active / "scripts/deploy/metahubctl.sh"
                releases.check_owned(controller)
                if not controller.is_file():
                    raise releases.ReleaseError("Active release controller is missing.")
                def restart_status(outcome, message):
                    write_status(status_root, job["job_id"], version, outcome, message, operation=operation)
                restart_status("running", "正在重启生产服务。")
                try:
                    result = run_bounded(["bash", str(controller), "restart", str(state)], timeout=900)
                    if result.returncode:
                        restart_status("failed", "服务重启未通过；请检查服务状态后再重试。")
                        retain_request(updates, request)
                        return
                    restart_status("completed", "生产服务已重启并通过健康检查。")
                except (Exception, KeyboardInterrupt, SystemExit):
                    restart_status("failed", "服务重启中断；请检查服务状态后再重试。")
                    retain_request(updates, request)
                    return
                retain_request(updates, request)
                continue
            if version not in {item["version"] for item in json.loads((updates / "catalog.json").read_text(encoding="utf-8"))["releases"]}:
                write_status(status_root, job["job_id"], version, "rejected", "该版本已不在可升级目录中；未执行安装。", operation=operation)
                retain_request(updates, request)
                continue
            receipt = store / f"{version}.verified.json"
            candidate = store / version
            releases.check_owned(receipt)
            releases.check_owned(candidate)
            # Validate the entire tree's ownership before running signed code as root.
            for path in candidate.rglob("*"):
                releases.check_owned(path)
            trusted = json.loads(receipt.read_text(encoding="utf-8"))
            if trusted["version"] != version:
                raise releases.ReleaseError("Release receipt does not match request.")
            active = Path((state / "current-release").read_text().strip())
            releases.check_owned(active)
            # Use the authenticated candidate's upgrade safeguards even when the
            # installed release predates them. The controller separately reads
            # current-release to address the serving database and Compose stack.
            controller = candidate / "scripts/deploy/metahubctl.sh"
            releases.check_owned(controller)
            if not controller.is_file():
                raise releases.ReleaseError("Verified release controller is missing.")
            def status(outcome, message):
                write_status(status_root, job["job_id"], version, outcome, message, operation=operation)
            current = next(line.split("=", 1)[1] for line in (state / "compose.env").read_text().splitlines()
                           if line.startswith("METAHUB_RELEASE="))
            if releases.version_order(version) <= releases.version_order(current):
                status("rejected", "该版本不是更高版本，已拒绝重复安装或降级。")
                retain_request(updates, request)
                return
            status("running", "正在准备备份和安装。")
            # No arbitrary request field becomes a path, command, or shell expression.
            failed = True
            try:
                result = run_bounded(["bash", str(controller), "upgrade", str(state), str(candidate), version])
                if result.returncode < 0:
                    raise RuntimeError("Upgrade controller terminated by a signal.")
                if result.returncode:
                    status("failed", "升级未通过；已暂停后续升级，请由运维检查备份并恢复。")
                else:
                    failed = False
                    status("completed", "备份、迁移和服务健康检查已通过。")
            except (Exception, KeyboardInterrupt, SystemExit):
                status("failed", "升级中断；请由运维检查服务和备份后恢复。")
                # Killing docker compose does not stop containers owned by the
                # Docker daemon. Quiesce those separately before releasing lock.
                try:
                    cleanup = run_bounded(["bash", str(controller), "quiesce-upgrade", str(state)], timeout=120)
                    if cleanup.returncode:
                        status("failed", "升级中断且停止服务未通过；请立即检查迁移容器、服务和备份。")
                except (Exception, KeyboardInterrupt, SystemExit):
                    status("failed", "升级中断且停止服务未通过；请立即检查迁移容器、服务和备份。")
            retain_request(updates, request)
            if failed:
                # Re-checking at the next timer tick is too late for requests
                # already loaded by this invocation.
                return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        process(args.config)
    except Exception as error:
        print(json.dumps({"status": "failed", "type": type(error).__name__}))
        raise SystemExit(1) from None
