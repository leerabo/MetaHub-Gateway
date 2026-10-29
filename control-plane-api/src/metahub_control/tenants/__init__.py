"""租户管理模块入口，集中导出租户资料读取、服务模式更新和租户隔离边界。"""

from .routes import router

__all__ = ["router"]
