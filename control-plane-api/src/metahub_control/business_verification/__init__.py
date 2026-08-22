"""聚合 PRD-102 企业认证与开票资料路由，服务边界隔离 Workspace 和 Platform 会话域。"""

from .routes import router

__all__ = ["router"]
