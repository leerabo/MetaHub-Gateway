"""Independent process entrypoint for verified infrastructure backups."""

from __future__ import annotations

import logging
import signal
import threading
from uuid import uuid4

from metahub_shared.runtime.config import load_app_config

from .worker import run_backup_once

logger = logging.getLogger("metahub.control.backup_worker")


def main() -> None:
    config = load_app_config("backup-worker")
    if not config.backup_worker_enabled:
        raise RuntimeError("backup worker is disabled")
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    worker_id = f"backup-worker-{uuid4().hex}"
    while not stop.is_set():
        try:
            progress = run_backup_once(config, worker_id=worker_id)
        except Exception:
            progress = False
            logger.error(
                "Backup worker iteration failed",
                extra={
                    "event": "backup.worker_iteration_failed",
                    "error_code": "backup.worker_unavailable",
                },
            )
        if not progress:
            stop.wait(float(config.backup_poll_interval_seconds))


if __name__ == "__main__":
    main()


__all__ = ["main"]
