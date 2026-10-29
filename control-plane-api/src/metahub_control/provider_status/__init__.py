"""PRD-061 内部 Provider 状态页领域入口，仅导出经过双权限保护的只读路由。"""

from .routes import router

__all__ = ["router"]
