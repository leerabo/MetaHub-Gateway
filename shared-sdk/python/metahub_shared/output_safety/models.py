"""输出安全的内存数据模型。

模型只保存稳定 ID、枚举、计数和 ``repr=False`` 的短期匹配材料。Prompt、Response、命中片段和规则
pattern 不得进入异常文本、持久层 DTO 或结构化日志。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from re import Pattern


class OutputSafetyError(ValueError):
    """表示可安全公开为固定 reason code 的扫描失败。

    参数：``reason_code`` 必须是预定义、无正文的内部原因码。
    返回值：异常对象本身不返回业务数据。
    关键约束：禁止把原始 SSE、模型文本、pattern 或底层异常正文拼入 message。
    """

    def __init__(self, reason_code: str) -> None:
        """使用固定原因码初始化安全异常。"""

        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class OutputRule:
    """一次请求内冻结的输出 DLP 规则。

    ``pattern`` 与 ``compiled_regex`` 仅在当前请求内存中使用并从 repr 隐藏；调用方必须保证 tenant 已在
    SQL 层按 ``scan_scope IN ('output','both')`` 过滤。
    """

    dlp_rule_id: str
    rule_type: str
    action: str
    priority: int
    pattern: str = field(repr=False)
    compiled_regex: Pattern[str] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class OutputRuleSnapshot:
    """单请求冻结规则快照，不包含可持久化的规则正文副本。"""

    tenant_id: str
    ruleset_fingerprint: str
    detector_version: str
    rules: tuple[OutputRule, ...] = field(repr=False)


@dataclass(frozen=True)
class OutputFragment:
    """带稳定 lane 的短期模型输出片段。

    lane 用于阻止不同 choice、refusal、正文或 tool call 参数被错误拼接；``text`` 永远不应落库或进日志。
    """

    lane: tuple[str, ...]
    text: str = field(repr=False)


@dataclass(frozen=True)
class OutputScanDecision:
    """不含正文的扫描判定，可安全传给持久化层。"""

    decision: str
    action: str | None
    dlp_rule_id: str | None
    rule_type: str | None
    match_count: int
    checkpoint_count: int
    scanned_token_count: int


@dataclass(frozen=True)
class SseFrame:
    """一个完整 SSE event 的原始字节与安全解析结果。

    ``raw_bytes``、``data_text`` 和 fragments 都可能含模型输出，因此全部从 repr 隐藏；只有扫描通过后才能
    原样转发 raw_bytes。
    """

    raw_bytes: bytes = field(repr=False)
    event_name: str | None
    data_text: str | None = field(repr=False)
    is_done: bool
    fragments: tuple[OutputFragment, ...] = field(repr=False)
