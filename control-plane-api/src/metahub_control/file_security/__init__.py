"""PRD-073 文件隐藏 Prompt Injection 检测控制面入口与后端模型准入边界。"""

from .routes import router
from .service import build_file_model_context

__all__ = ["build_file_model_context", "router"]
