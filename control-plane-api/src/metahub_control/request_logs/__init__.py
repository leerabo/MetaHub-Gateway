"""调用日志模块入口，承载检索、详情审计及查询事件可靠投递能力。"""

from .scheduler import install_request_log_query_event_scheduler

__all__ = ["install_request_log_query_event_scheduler"]
