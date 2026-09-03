"""PRD-145 Platform, Workspace, and GlobalAccount console boundaries."""

from .routes import router
from .scheduler import install_console_experience_scheduler

__all__ = ["install_console_experience_scheduler", "router"]
