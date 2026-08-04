"""PRD-056 Provider 错误归一化规则控制面入口。"""

from .routes import router
from .scheduler import install_provider_error_map_scheduler

__all__ = ["install_provider_error_map_scheduler", "router"]
