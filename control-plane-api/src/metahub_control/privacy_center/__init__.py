"""PRD-134 global account privacy and data subject request center."""

from .routes import router
from .scheduler import install_privacy_scheduler

__all__ = ["install_privacy_scheduler", "router"]
