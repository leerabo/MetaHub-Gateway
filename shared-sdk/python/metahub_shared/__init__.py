"""共享 SDK 包入口，向控制面和数据面暴露统一配置、错误和运行时契约。"""

from .runtime.config import AppConfig, ServiceInfo, load_app_config
from .runtime.errors import ApiError, ErrorResponse

__all__ = [
    "ApiError",
    "AppConfig",
    "ErrorResponse",
    "ServiceInfo",
    "load_app_config",
]
