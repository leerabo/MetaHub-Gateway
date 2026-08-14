"""PRD-078 approval API and recovery lifecycle entrypoint.
集中导出 PRD-078 审批控制面入口，公开符号不得绕过服务端租户与权限校验。
"""

from .routes import router
from .scheduler import install_approval_scheduler

__all__ = ["install_approval_scheduler", "router"]
