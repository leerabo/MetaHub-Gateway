"""PRD-074 批任务控制面模块：负责租户内调度、领取、续租、取消、计量和可恢复事件投递。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

PRD-074 Batch Control API and independent Worker exports.
"""

from .routes import router
from .worker import (
    claim_next_batch_task,
    prepare_batch_attempt,
    reap_expired_batch_tasks,
    run_batch_task_once,
)

__all__ = [
    "claim_next_batch_task",
    "prepare_batch_attempt",
    "reap_expired_batch_tasks",
    "router",
    "run_batch_task_once",
]
