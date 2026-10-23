"""Gateway compatibility exports for the shared authoritative cost-rate resolver."""

from metahub_shared.runtime.cost_rates import ResolvedCostRate, resolve_workspace_cost_rate

__all__ = ["ResolvedCostRate", "resolve_workspace_cost_rate"]
