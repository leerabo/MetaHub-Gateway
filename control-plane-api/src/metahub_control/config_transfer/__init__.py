"""配置导入导出 Control API 领域包。

该包集中承载严格 JSON 解析、租户级导出快照和原子 create-only 导入；外部调用必须经过路由权限依赖，
不得绕过 Control API 直接复用内部写入函数处理不可信请求。
"""

from .routes import router

__all__ = ["router"]
