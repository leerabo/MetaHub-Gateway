"""用户偏好模块。

该模块承载 PRD-027 的语言偏好 API；控制台导航偏好仍由 console 模块负责。
"""

from .routes import router

__all__ = ["router"]
