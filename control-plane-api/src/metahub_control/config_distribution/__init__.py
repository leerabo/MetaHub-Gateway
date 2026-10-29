"""PRD-108 tenant configuration distribution domain."""

from .routes import router
from .runtime import ConfigSnapshotRebuildInput

__all__ = ["ConfigSnapshotRebuildInput", "router"]
