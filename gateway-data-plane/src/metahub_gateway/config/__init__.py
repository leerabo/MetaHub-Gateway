"""Gateway runtime configuration and signed distribution ownership."""

from .distribution import ConfigSyncResult, GatewayConfigSynchronizer
from .edge_desired_state import DesiredStateSyncResult, GatewayDesiredStateSynchronizer

__all__ = [
    "ConfigSyncResult",
    "DesiredStateSyncResult",
    "GatewayConfigSynchronizer",
    "GatewayDesiredStateSynchronizer",
]
