"""用户管理模块入口，导出基础 RBAC 用户、角色列表和管理员变更接口。"""
"""控制面用户与基础 RBAC 模块。"""

from .routes import router

__all__ = ["router"]
