"""Low-cardinality observability facts and non-blocking service integration."""

from .runtime import (
    TelemetryBuffer,
    TelemetryFact,
    TelemetrySnapshot,
    install_telemetry_runtime,
)

__all__ = [
    "TelemetryBuffer",
    "TelemetryFact",
    "TelemetrySnapshot",
    "install_telemetry_runtime",
]
