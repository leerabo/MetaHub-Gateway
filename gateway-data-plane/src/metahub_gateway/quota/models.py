"""Gateway 额度准入与结算的进程内结果合同。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QuotaGuardResult:
    """一次 Provider 调用前预留的安全内存摘要。"""

    reservation_id: str | None
    estimated_micros: int
    plan_id: str | None


@dataclass(frozen=True, slots=True)
class QuotaSettlementSummary:
    """绑定一条 canonical charge 的权威额度分摊投影。"""

    allocation_class: str
    allocation_count: int


__all__ = ["QuotaGuardResult", "QuotaSettlementSummary"]
