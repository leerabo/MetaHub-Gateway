"""汇总 PRD-060 用户邀请领域的公开路由入口。

模块只导出 HTTP router；原始 Token、密码、租户恢复、强审计和事务边界均由 routes/service
内部实现，其他模块不得绕过后端 ``user.manage`` 或直接操作 Invitation 状态。
"""

from .routes import router

__all__ = ["router"]
