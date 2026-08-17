"""产品激活分析包统一导出只读漏斗路由与可关闭的事件恢复调度器。"""

from .routes import router
from .scheduler import install_product_event_scheduler

__all__ = ["install_product_event_scheduler", "router"]
