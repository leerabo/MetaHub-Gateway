"""PRD-077 只读管理员助手 Control Plane 的路由、服务、outbox 与调度入口。"""

from .routes import router
from .scheduler import install_admin_assistant_scheduler

__all__ = ["install_admin_assistant_scheduler", "router"]
