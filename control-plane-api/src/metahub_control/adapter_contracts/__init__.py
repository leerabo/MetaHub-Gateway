"""PRD-064 Adapter 合同控制面模块。

模块职责是公开只读路由并安装失败事件 Source Outbox 调度器；报告校验、租户隔离、
持久化和投递恢复分别保留在内部子模块，包入口不得绕过这些安全与事务边界。
"""

from .routes import internal_router, router
from .scheduler import install_adapter_contract_event_scheduler

__all__ = ["install_adapter_contract_event_scheduler", "internal_router", "router"]
