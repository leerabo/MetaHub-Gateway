"""PRD-076 Gateway Response Cache 的稳定内部导出面。

数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
"""

from .eligibility import (
    ResponseCacheStructuralBypass,
    classify_response_cache_request,
    trusted_response_cache_tags,
)
from .models import (
    PreparedResponseCache,
    ResponseCacheCandidate,
    ResponseCacheFillLease,
    ResponseCacheFillResult,
    ResponseCacheKeyState,
    ResponseCachePolicySnapshot,
    ResponseCacheRequestStatus,
    ResponseCacheSettlementIds,
    ResponseCacheTagGeneration,
)
from .repository import (
    commit_response_cache_fill,
    prepare_response_cache,
    release_response_cache_fill,
)
from .settlement import (
    PreparedResponseCacheSettlement,
    ResponseCacheSettlementReadback,
    ResponseCacheSettlementRecovery,
    ResponseCacheSettlementResult,
    ResponseCacheSettlementStatus,
    fail_response_cache_candidate,
    freeze_response_cache_reference,
    read_response_cache_settlement,
    recover_absent_response_cache_settlement,
    settle_response_cache_candidate,
)

__all__ = [
    "PreparedResponseCache",
    "PreparedResponseCacheSettlement",
    "ResponseCacheCandidate",
    "ResponseCacheFillLease",
    "ResponseCacheFillResult",
    "ResponseCacheKeyState",
    "ResponseCachePolicySnapshot",
    "ResponseCacheRequestStatus",
    "ResponseCacheSettlementIds",
    "ResponseCacheSettlementReadback",
    "ResponseCacheSettlementRecovery",
    "ResponseCacheSettlementResult",
    "ResponseCacheSettlementStatus",
    "ResponseCacheStructuralBypass",
    "ResponseCacheTagGeneration",
    "classify_response_cache_request",
    "fail_response_cache_candidate",
    "freeze_response_cache_reference",
    "read_response_cache_settlement",
    "recover_absent_response_cache_settlement",
    "settle_response_cache_candidate",
    "trusted_response_cache_tags",
    "commit_response_cache_fill",
    "prepare_response_cache",
    "release_response_cache_fill",
]
