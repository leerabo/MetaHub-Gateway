"""Gateway Prompt Cache 感知粘性路由内部服务。

包入口只公开不可逆路由上下文、Selection 终态和启动恢复能力，不导出 Prompt 或凭据实现细节。
"""

from .lifecycle import (
    install_cache_affinity_recovery,
    safe_finalize_cache_affinity_selection,
)
from .models import (
    CacheAffinityRouteContext,
    CacheAffinityRouteFacts,
    CacheAffinitySelectionHandle,
)

__all__ = [
    "install_cache_affinity_recovery",
    "safe_finalize_cache_affinity_selection",
    "CacheAffinityRouteContext",
    "CacheAffinityRouteFacts",
    "CacheAffinitySelectionHandle",
]
