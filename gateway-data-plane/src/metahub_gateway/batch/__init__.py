"""PRD-074 网关批处理模块：校验内部调用并执行单项请求，所有结果保留租户、用量和失败追踪事实。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

PRD-074 Gateway Batch isolation and trusted invocation contracts.
"""

from .gate import (
    BatchGatewayGate,
    BatchGatewayLease,
    RealtimeGatewayLease,
    get_batch_gateway_gate,
)
from .models import (
    BatchAllocationCoverage,
    BatchRouteCandidateCoverage,
    BatchRouteCoverage,
    TrustedBatchContext,
    TrustedGatewayInvocation,
)

__all__ = [
    "BatchAllocationCoverage",
    "BatchGatewayGate",
    "BatchGatewayLease",
    "BatchRouteCoverage",
    "BatchRouteCandidateCoverage",
    "RealtimeGatewayLease",
    "TrustedBatchContext",
    "TrustedGatewayInvocation",
    "get_batch_gateway_gate",
]
