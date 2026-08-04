"""Gateway 输出安全持久化模型。

本模块只承载租户 owner、稳定枚举、规则版本和计数。模型正文、命中片段、DLP pattern、
Provider 凭据和底层异常文本都不属于持久化合同，也不得后来加入这些数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from metahub_shared.output_safety import OutputRuleSnapshot, OutputScanDecision

# 检测器版本进入 scan/event/Webhook，用来证明同一请求采用了哪套固定投影与匹配语义。
DETECTOR_VERSION = "output-safety-dlp-v1"
# 流式默认窗口在请求开始时冻结，运行过程中不得被动态配置或客户端字段改写。
DEFAULT_INTERVAL_TOKENS = 32
DEFAULT_WINDOW_TOKENS = 256

# 二维状态、暴露和账单意图集中为严格类型，防止跨模块各自发明别名。
OutputSafetyScanMode = Literal["non_stream_full", "stream_window"]
OutputSafetyScanStatus = Literal[
    "pending",
    "not_configured",
    "completed",
    "unavailable",
    "not_applicable",
]
OutputSafetyDecision = Literal["none", "clean", "matched", "blocked", "unknown"]
OutputSafetyExposureState = Literal["none", "partial", "complete", "unknown"]
OutputSafetyBillingDisposition = Literal["not_required", "dispute_pending"]


@dataclass(frozen=True, slots=True)
class PreparedOutputSafetyScan:
    """一次已提交 owner 且已冻结规则集的输出扫描。

    ``snapshot.rules`` 中的 pattern 由 Shared 模型从 repr 隐藏，只能在当前请求内存中使用。
    ``scan_status`` 仅可能是 pending 或 not_configured；后者表示空规则事实已在 Provider 前终结。
    """

    output_safety_scan_id: str
    tenant_id: str
    request_id: str
    trace_id: str
    stream_session_id: str | None
    scan_mode: OutputSafetyScanMode
    scan_status: Literal["pending", "not_configured"]
    interval_tokens: int | None
    window_tokens: int | None
    snapshot: OutputRuleSnapshot

    @property
    def configured(self) -> bool:
        """返回是否存在本次请求实际启用的 output/both 规则。

        返回值只取决于冻结快照；不能用历史规则总数或 scan 行是否存在来推断安全配置。
        """

        return bool(self.snapshot.rules)


@dataclass(frozen=True, slots=True)
class OutputSafetyTerminalState:
    """OutputSafetyScan 的完整终态写入合同。

    所有字段都可直接持久化且不包含正文。调用方必须显式提供 observed/scanned/emitted 三组计数，
    以免流式命中 event 被错误记作已经发送给客户端。
    """

    scan_status: Literal["not_configured", "completed", "unavailable", "not_applicable"]
    decision: OutputSafetyDecision
    action: Literal["allow", "log_only", "block"] | None = None
    dlp_rule_id: str | None = None
    rule_type: Literal["keyword", "regex"] | None = None
    match_count: int = 0
    checkpoint_count: int = 0
    observed_output_token_count: int = 0
    scanned_token_count: int = 0
    emitted_token_count: int = 0
    emitted_sse_event_count: int = 0
    emitted_bytes: int = 0
    exposure_state: OutputSafetyExposureState = "unknown"
    billing_disposition: OutputSafetyBillingDisposition = "not_required"
    failure_reason_code: str | None = None

    @classmethod
    def from_scan_decision(
        cls,
        decision: OutputScanDecision,
        *,
        observed_output_token_count: int,
        emitted_token_count: int,
        emitted_sse_event_count: int,
        emitted_bytes: int,
        exposure_state: OutputSafetyExposureState,
    ) -> "OutputSafetyTerminalState":
        """把 Shared 无正文判定转换成 completed 持久终态。

        参数：Shared 判定，以及 Gateway 分别观察和交付给下游的计数。
        返回：尚未写库的 completed 终态。
        关键约束：只有 blocked 派生 dispute_pending；本函数不会生成财务冲正分录。
        """

        return cls(
            scan_status="completed",
            decision=decision.decision,  # type: ignore[arg-type]
            action=decision.action,  # type: ignore[arg-type]
            dlp_rule_id=decision.dlp_rule_id,
            rule_type=decision.rule_type,  # type: ignore[arg-type]
            match_count=decision.match_count,
            checkpoint_count=decision.checkpoint_count,
            observed_output_token_count=observed_output_token_count,
            scanned_token_count=decision.scanned_token_count,
            emitted_token_count=emitted_token_count,
            emitted_sse_event_count=emitted_sse_event_count,
            emitted_bytes=emitted_bytes,
            exposure_state=exposure_state,
            billing_disposition=(
                "dispute_pending" if decision.decision == "blocked" else "not_required"
            ),
        )

    @classmethod
    def not_applicable(cls) -> "OutputSafetyTerminalState":
        """构造 Provider 未产生可扫描成功输出时的固定终态。"""

        return cls(scan_status="not_applicable", decision="none")

    @classmethod
    def unavailable(
        cls,
        reason_code: str,
        *,
        previous_decision: OutputScanDecision | None = None,
        observed_output_token_count: int = 0,
        emitted_token_count: int = 0,
        emitted_sse_event_count: int = 0,
        emitted_bytes: int = 0,
        exposure_state: OutputSafetyExposureState = "unknown",
    ) -> "OutputSafetyTerminalState":
        """构造检测器或流完整性不可证明时的失败关闭终态。

        ``previous_decision`` 只允许保留此前已确认的 matched 元数据；clean 不能在尾窗未完成时保留，
        blocked 应走 completed 阻断分支，避免把确定阻断降级成 unavailable。
        """

        if previous_decision is not None and previous_decision.decision == "blocked":
            # 已确认 block 必须保留 completed+blocked 及 dispute 意图，不能借不可用分支降级。
            raise ValueError("output_safety.blocked_decision_cannot_be_unavailable")
        matched = previous_decision is not None and previous_decision.decision == "matched"
        return cls(
            scan_status="unavailable",
            decision="matched" if matched else "unknown",
            action=previous_decision.action if matched else None,  # type: ignore[union-attr,arg-type]
            dlp_rule_id=previous_decision.dlp_rule_id if matched else None,  # type: ignore[union-attr]
            rule_type=previous_decision.rule_type if matched else None,  # type: ignore[union-attr,arg-type]
            match_count=previous_decision.match_count if matched else 0,  # type: ignore[union-attr]
            checkpoint_count=previous_decision.checkpoint_count if previous_decision else 0,
            observed_output_token_count=observed_output_token_count,
            scanned_token_count=previous_decision.scanned_token_count if previous_decision else 0,
            emitted_token_count=emitted_token_count,
            emitted_sse_event_count=emitted_sse_event_count,
            emitted_bytes=emitted_bytes,
            exposure_state=exposure_state,
            failure_reason_code=reason_code,
        )


@dataclass(frozen=True, slots=True)
class OutputSafetyEventRecord:
    """已持久化的 ``security.output_blocked`` source event。

    字段与冻结 Webhook payload 一一对应，但租户和 trace 仍只属于可信 envelope owner。
    该对象不含 ``reversal_eligible``，因为该值必须在后续正式 charge 事务中派生。
    """

    event_id: str
    tenant_id: str
    output_safety_scan_id: str
    request_id: str
    trace_id: str
    event_type: Literal["security.output_blocked"]
    schema_version: Literal[1]
    scan_mode: OutputSafetyScanMode
    scan_status: Literal["completed"]
    decision: Literal["blocked"]
    error_code: Literal["content_policy_violation"]
    exposure_state: Literal["none", "partial"]
    emitted_token_count: int
    checkpoint_count: int
    dlp_rule_id: str
    detector_version: str
    billing_disposition: Literal["dispute_pending"]
    occurred_at: str


@dataclass(frozen=True, slots=True)
class OutputSafetyFinalization:
    """一次终结调用的结果，供调用方执行 Usage 与 Webhook durability 门禁。"""

    scan: PreparedOutputSafetyScan
    terminal: OutputSafetyTerminalState
    source_event: OutputSafetyEventRecord | None
    idempotent_replay: bool


class OutputSafetyStoreError(RuntimeError):
    """只携带固定 reason code 的内部持久化错误。

    该异常供同事务 StreamSession 服务消费；公开 HTTP 边界必须转换为统一
    ``gateway.output_safety_unavailable``，不得拼接 SQLite 或规则异常正文。
    """

    def __init__(self, reason_code: str) -> None:
        """使用预定义 reason code 创建内部存储异常。"""

        self.reason_code = reason_code
        super().__init__(reason_code)


__all__ = [
    "DEFAULT_INTERVAL_TOKENS",
    "DEFAULT_WINDOW_TOKENS",
    "DETECTOR_VERSION",
    "OutputSafetyBillingDisposition",
    "OutputSafetyDecision",
    "OutputSafetyEventRecord",
    "OutputSafetyExposureState",
    "OutputSafetyFinalization",
    "OutputSafetyScanMode",
    "OutputSafetyScanStatus",
    "OutputSafetyStoreError",
    "OutputSafetyTerminalState",
    "PreparedOutputSafetyScan",
]
