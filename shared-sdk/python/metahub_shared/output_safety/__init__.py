"""输出侧安全扫描共享合同。

本包只处理内存中的有界文本投影、SSE 分帧和规则判定，不负责数据库、权限或网络调用。
所有公开对象都刻意避免在 ``repr`` 中包含模型正文和 DLP pattern，防止异常日志扩散敏感内容。
"""

from .models import (
    OutputFragment,
    OutputRule,
    OutputRuleSnapshot,
    OutputSafetyError,
    OutputScanDecision,
    SseFrame,
)
from .scanner import SlidingOutputScanner, extract_full_output_fragments, scan_full_output
from .sse import BoundedSseEventFramer

__all__ = [
    "BoundedSseEventFramer",
    "OutputFragment",
    "OutputRule",
    "OutputRuleSnapshot",
    "OutputSafetyError",
    "OutputScanDecision",
    "SlidingOutputScanner",
    "SseFrame",
    "extract_full_output_fragments",
    "scan_full_output",
]
