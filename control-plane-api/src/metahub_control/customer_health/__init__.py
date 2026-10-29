"""PRD-086 客户健康路由与可关闭后台调度入口。"""

from .routes import router
from .scheduler import install_customer_health_scheduler

__all__ = ["install_customer_health_scheduler", "router"]
