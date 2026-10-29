"""PRD-141 Platform Growth boundary."""

from .routes import router
from .scheduler import install_platform_growth_scheduler

__all__ = ["install_platform_growth_scheduler", "router"]
