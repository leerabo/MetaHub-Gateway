"""Gateway Virtual Key 反馈写入口模块。

职责：统一导出只写路由供数据面应用装配。边界：本模块不提供 summary、不缓存明文
Virtual Key，也不绕过 shared 的租户、同 Key、资格、幂等和事件事务规则。
"""

from .routes import router

__all__ = ["router"]
