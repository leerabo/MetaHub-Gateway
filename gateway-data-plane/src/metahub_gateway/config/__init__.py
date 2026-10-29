"""Gateway runtime configuration and signed distribution ownership."""

from .distribution import ConfigSyncResult, GatewayConfigSynchronizer
from .edge_desired_state import DesiredStateSyncResult, GatewayDesiredStateSynchronizer
from .edge_licenses import (
    GatewayLicenseSynchronizer,
    LicenseSyncResult,
    LicenseUsageSnapshot,
)
from .edge_upgrades import GatewayUpgradeSynchronizer, UpgradeSyncResult

__all__ = [
    "ConfigSyncResult",
    "DesiredStateSyncResult",
    "GatewayConfigSynchronizer",
    "GatewayDesiredStateSynchronizer",
    "GatewayLicenseSynchronizer",
    "GatewayUpgradeSynchronizer",
    "LicenseSyncResult",
    "LicenseUsageSnapshot",
    "UpgradeSyncResult",
]
