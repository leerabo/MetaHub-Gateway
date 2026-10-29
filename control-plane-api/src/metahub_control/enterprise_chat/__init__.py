"""PRD-066 企业 Chat 控制面模块。

本模块只编排短生命周期会话和 Gateway 内部调用；Prompt/Response 不持久化，租户、用户、
登录 Session 与 ``chat.use`` 权限均由每次 Control 请求重新恢复。
"""

from .routes import router

__all__ = ["router"]
