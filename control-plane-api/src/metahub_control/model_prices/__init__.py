"""模块职责：导出模型有效期价格管理路由，供控制面应用统一注册。

维护边界：价格写入必须经过后端 RBAC 与 tenant_id 校验，不能由前端页面权限替代。
来源文件：control-plane-api/src/metahub_control/model_prices/__init__.py
"""

from .routes import router

__all__ = ["router"]
