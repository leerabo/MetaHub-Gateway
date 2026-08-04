"""数据面 DLP 模块入口，导出热路径检查和日志上下文应用函数。"""

from .service import (
    DlpBlockedError,
    apply_dlp_decision,
    enforce_dlp_guard,
    extract_session_user_texts,
    extract_user_texts,
)

__all__ = [
    "DlpBlockedError",
    "apply_dlp_decision",
    "enforce_dlp_guard",
    "extract_session_user_texts",
    "extract_user_texts",
]
