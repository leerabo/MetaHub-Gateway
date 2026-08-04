"""PRD-063 Adapter 规范 Control API 模块入口。

这里只导出受统一认证、租户隔离和错误中间件保护的路由，不暴露内部激活服务实现。
"""

from .routes import router

__all__ = ["router"]
