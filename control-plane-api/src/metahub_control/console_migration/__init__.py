"""PRD-147 console-domain migration governance."""

from .outbox import dispatch_console_migration_event_once
from .routes import router
from .worker import JOB_TYPE

__all__ = ["JOB_TYPE", "dispatch_console_migration_event_once", "router"]
