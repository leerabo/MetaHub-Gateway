"""Platform distributed job runtime public surface."""

from typing import Any

from .routes import router
from .runtime import run_platform_job_once
from .service import enqueue_platform_job


def __getattr__(name: str) -> Any:
    if name == "install_platform_job_supervisor":
        from .supervisor import install_platform_job_supervisor

        return install_platform_job_supervisor
    raise AttributeError(name)

__all__ = [
    "enqueue_platform_job",
    "install_platform_job_supervisor",
    "router",
    "run_platform_job_once",
]
