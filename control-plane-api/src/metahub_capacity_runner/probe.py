"""Separate opt-in resource observer for one deployment-bound target process."""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path

import psutil
from fastapi import FastAPI, Header, HTTPException

from .contracts import CapacityResourceSample
from .settings import read_secret

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/internal/capacity/telemetry", response_model=CapacityResourceSample)
def telemetry(authorization: str | None = Header(default=None)):
    try:
        token = read_secret(Path(os.environ["METAHUB_CAPACITY_PROBE_TOKEN_FILE"]))
        pid = int(os.environ["METAHUB_CAPACITY_PROBE_TARGET_PID"])
        created_at = float(os.environ["METAHUB_CAPACITY_PROBE_TARGET_CREATED_AT"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=503, detail="runner.resource_unavailable") from None
    if authorization is None or not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="runner.unauthorized")
    try:
        process = psutil.Process(pid)
        if process.create_time() != created_at:
            raise ValueError
        with process.oneshot():
            cpu = process.cpu_times()
            memory = process.memory_info().rss
            handles = process.num_handles() if os.name == "nt" else process.num_fds()
        connections = sum(
            item.status not in {"LISTEN", "NONE", "TIME_WAIT"}
            for item in process.net_connections(kind="tcp")
        )
        if not process.is_running() or process.create_time() != created_at:
            raise ValueError
        return CapacityResourceSample(
            process_sha256=hashlib.sha256(
                f"capacity-target/v1:{pid}:{created_at}".encode()
            ).hexdigest(),
            sampled_at=datetime.now(UTC),
            cpu_time_ms=int((cpu.user + cpu.system) * 1000),
            memory_rss_bytes=memory,
            open_connections=connections,
            handle_count=handles,
            processor_count=psutil.cpu_count() or 1,
        )
    except (psutil.Error, OSError, ValueError):
        raise HTTPException(status_code=503, detail="runner.resource_unavailable") from None
