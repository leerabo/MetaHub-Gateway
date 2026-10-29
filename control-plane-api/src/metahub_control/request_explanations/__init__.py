"""PRD-054 请求解释中心 Control API、聚合服务与事件 outbox。"""

from .routes import router
from .scheduler import install_request_explanation_scheduler

__all__ = ["install_request_explanation_scheduler", "router"]
