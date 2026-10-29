"""内置 API 调试台模块入口，导出路由供控制面应用注册。

该模块只承载控制台低频调试请求编排；真实模型请求仍复用 Gateway 数据面的路由、Provider Adapter、
调用日志和用量账本，避免形成第二套不可追踪的调用链路。
"""

from .routes import router

__all__ = ["router"]

