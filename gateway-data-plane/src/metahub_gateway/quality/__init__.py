"""Gateway 模型质量观测写入入口。

模块职责：只导出真实上游终态的事务内累计函数，供 attempt 与 stream session 复用。
维护边界：调用方必须先判定质量资格；本包不接收正文、凭据或错误详情，也不自行提交事务。
"""

from .observations import record_model_quality_observation

__all__ = ["record_model_quality_observation"]
