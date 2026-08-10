"""实验控制面包入口：只导出受治理的路由、调度与恢复能力，不建立隐式全局状态。

PRD-075 Control-plane API and recovery workers.
"""

from .routes import router
from .scheduler import install_experiment_scheduler

__all__ = ["install_experiment_scheduler", "router"]
