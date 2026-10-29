"""Gateway 输出安全持久化入口。

公开接口只处理已经鉴权的 tenant owner 与无正文扫描判定；本包不注册外部 HTTP 文本扫描路由。
"""

from .models import (
    DEFAULT_INTERVAL_TOKENS,
    DEFAULT_WINDOW_TOKENS,
    DETECTOR_VERSION,
    OutputSafetyEventRecord,
    OutputSafetyFinalization,
    OutputSafetyStoreError,
    OutputSafetyTerminalState,
    PreparedOutputSafetyScan,
)
from .service import (
    finalize_output_safety_scan,
    finalize_output_safety_scan_in_transaction,
    output_safety_event_to_webhook,
    prepare_output_safety_scan,
)

__all__ = [
    "DEFAULT_INTERVAL_TOKENS",
    "DEFAULT_WINDOW_TOKENS",
    "DETECTOR_VERSION",
    "OutputSafetyEventRecord",
    "OutputSafetyFinalization",
    "OutputSafetyStoreError",
    "OutputSafetyTerminalState",
    "PreparedOutputSafetyScan",
    "finalize_output_safety_scan",
    "finalize_output_safety_scan_in_transaction",
    "output_safety_event_to_webhook",
    "prepare_output_safety_scan",
]
