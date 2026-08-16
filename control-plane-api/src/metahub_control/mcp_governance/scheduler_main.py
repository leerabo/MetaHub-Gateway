"""Independent process entrypoint for the PRD-083 MCP Scheduler role."""

from __future__ import annotations

import logging
import signal
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from itertools import count
from uuid import uuid4

from metahub_shared.runtime.config import load_app_config

from .sandbox_client import McpSandboxHttpClient
from .scheduler import run_mcp_outbox_once, run_mcp_scheduler_once

logger = logging.getLogger("metahub.control.mcp.scheduler_main")


def main() -> None:
    """Run only the lease-owning Scheduler and durable source-outbox loops."""

    config = load_app_config("mcp-scheduler-worker")
    if (
        not config.mcp_enabled
        or not config.mcp_scheduler_enabled
        or config.mcp_executor_mode != "sandbox_http"
    ):
        raise RuntimeError("mcp scheduler is disabled")
    McpSandboxHttpClient(config).readiness()

    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    process_id = f"mcp-scheduler-{uuid4().hex}"
    slots = count(1)
    concurrency = max(1, min(int(config.mcp_global_concurrency), 32))
    poll = max(0.1, min(float(config.mcp_scheduler_poll_interval_seconds), 60.0))
    futures: set[Future[bool]] = set()

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="mcp-scheduler") as pool:
        while not stop.is_set():
            futures = {future for future in futures if not future.done()}
            while len(futures) < concurrency and not stop.is_set():
                worker_id = f"{process_id}:{next(slots)}"
                futures.add(
                    pool.submit(
                        run_mcp_scheduler_once,
                        config,
                        worker_id=worker_id,
                        stop_event=stop,
                    )
                )
            made_progress = run_mcp_outbox_once(
                config,
                worker_id=f"mcp-outbox-{process_id}",
                stop_event=stop,
            )
            if futures:
                done, _pending = wait(futures, timeout=poll)
                for future in done:
                    try:
                        made_progress = future.result() or made_progress
                    except Exception:
                        logger.error(
                            "MCP scheduler task failed",
                            extra={
                                "event": "mcp.scheduler_task_failed",
                                "error_code": "mcp.unavailable",
                            },
                        )
            if not made_progress:
                stop.wait(poll)
        if futures:
            wait(
                futures,
                timeout=float(
                    max(config.mcp_operation_lease_seconds, config.mcp_invocation_lease_seconds)
                ),
            )


if __name__ == "__main__":
    main()


__all__ = ["main"]
