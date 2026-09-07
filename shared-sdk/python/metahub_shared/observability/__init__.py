"""Low-cardinality observability facts and non-blocking service integration."""

from .runtime import (
    TelemetryBuffer,
    TelemetryFact,
    TelemetrySnapshot,
    bind_authenticated_telemetry_tenant,
    install_telemetry_runtime,
)

__all__ = [
    "TelemetryBuffer",
    "TelemetryFact",
    "TelemetrySnapshot",
    "bind_authenticated_telemetry_tenant",
    "install_telemetry_runtime",
]
