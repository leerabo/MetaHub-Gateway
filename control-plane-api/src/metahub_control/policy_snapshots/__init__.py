"""策略快照版本化与发布模块。

该包只负责控制面的候选编译、不可变快照、发布历史与 Gateway 内部拉取；
策略在 Gateway 热路径中的缓存和执行由数据面模块负责，避免控制面逻辑侵入请求链路。
"""

from .routes import router

__all__ = ["router"]
