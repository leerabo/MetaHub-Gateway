"""Process entrypoint for the independent PRD-074 Batch Worker."""

from __future__ import annotations

import logging
import signal
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from itertools import count
from uuid import uuid4

from metahub_shared.runtime.config import load_app_config

from .worker import run_batch_task_once

logger = logging.getLogger("metahub.control.batch_worker")


def main() -> None:
    """Run a bounded worker pool; shutdown stops claims and waits for current owners."""

    config = load_app_config("batch-worker")
    if not config.batch_worker_enabled:
        raise RuntimeError("batch worker is disabled")
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    process_id = f"batch-worker-{uuid4().hex}"
    slot_sequence = count(1)
    futures: set[Future[bool]] = set()
    with ThreadPoolExecutor(
        max_workers=config.batch_global_concurrency,
        thread_name_prefix="batch-task",
    ) as executor:
        while not stop.is_set():
            futures = {future for future in futures if not future.done()}
            while len(futures) < config.batch_global_concurrency and not stop.is_set():
                slot_id = f"{process_id}:{next(slot_sequence)}"
                futures.add(
                    executor.submit(
                        run_batch_task_once,
                        config,
                        worker_id=slot_id,
                        stop_event=stop,
                    )
                )
            if futures:
                done, _pending = wait(
                    futures,
                    timeout=float(config.batch_poll_interval_seconds),
                    return_when=FIRST_COMPLETED,
                )
                made_progress = False
                for future in done:
                    try:
                        made_progress = future.result() or made_progress
                    except Exception:
                        logger.error(
                            "Batch worker task failed",
                            extra={
                                "event": "batch.worker_task_failed",
                                "error_code": "batch.worker_unavailable",
                            },
                        )
                if done and not made_progress:
                    stop.wait(float(config.batch_poll_interval_seconds))
            else:
                stop.wait(float(config.batch_poll_interval_seconds))
        if futures:
            wait(futures, timeout=float(config.batch_lease_seconds))


if __name__ == "__main__":
    main()
