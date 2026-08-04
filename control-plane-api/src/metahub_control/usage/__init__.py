"""用量治理模块入口，导出 Token 用量、成本估算和汇总查询 API 路由。"""

from .routes import router

__all__ = ["router"]
