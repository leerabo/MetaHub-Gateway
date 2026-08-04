"""Provider 健康探测模块，对外只导出路由和可测试的调度入口。"""

from .routes import router
from .scheduler import install_provider_health_scheduler

__all__ = ["install_provider_health_scheduler", "router"]
