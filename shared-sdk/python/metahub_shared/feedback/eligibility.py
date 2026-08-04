"""PRD-049 RequestLog 可反馈资格与最终模型归因状态机。

详情读取和 POST 写入必须调用同一函数；任何不完整 attempt 组合都失败关闭，不能因为
``final_routing_attempt_id`` 偶然为空就把有路由尝试的请求宽松回退为 legacy 调用。
"""

from __future__ import annotations

from .contracts import FeedbackEligibilityResult


def evaluate_feedback_eligibility(
    *,
    request_status: str,
    stream_session_id: str | None,
    stream_status: str | None,
    routing_attempt_count: int,
    final_routing_attempt_id: str | None,
    request_model_id: str | None,
    request_provider_id: str | None,
    final_attempt_id: str | None,
    final_attempt_status: str | None,
    final_attempt_model_id: str | None,
    final_attempt_provider_id: str | None,
    has_feedback: bool,
) -> FeedbackEligibilityResult:
    """计算是否可反馈，并返回唯一可信的最终 model/provider 快照。

    优先级固定为已有反馈、请求失败、流未完成、归因不可用。legacy 仅允许零 attempt 且
    final ID 为空；1/2 次 attempt 必须存在完全匹配且 succeeded 的 final 行。
    """

    if type(has_feedback) is not bool:
        # 持久读取层必须提供明确布尔值，truthy 字符串不能改变状态机优先级。
        return _ineligible("attribution_unavailable")
    if has_feedback:
        return _ineligible("already_recorded")
    if request_status != "success":
        return _ineligible("request_failed")
    # 有流会话但未 completed 时仍可能继续产生输出，不能提前形成最终反馈事实。
    if stream_session_id is not None and stream_status != "completed":
        return _ineligible("stream_incomplete")
    # 无流会话却存在流状态表示父事实组合损坏，禁止降级成普通非流式成功。
    if stream_session_id is None and stream_status is not None:
        # 流状态无 session 表示 RequestLog 组合损坏，不能当成普通非流式成功。
        return _ineligible("stream_incomplete")
    if (
        type(routing_attempt_count) is int
        and routing_attempt_count == 0
        and final_routing_attempt_id is None
        and _present(request_model_id)
        and _present(request_provider_id)
    ):
        # 只有严格 legacy 组合才使用 RequestLog 最终快照，避免绕过 attempt 归因。
        return FeedbackEligibilityResult(
            eligible=True,
            reason="eligible",
            model_id=request_model_id,
            provider_id=request_provider_id,
        )
    if (
        type(routing_attempt_count) is int
        and routing_attempt_count in (1, 2)
        and _present(final_routing_attempt_id)
        and final_attempt_id == final_routing_attempt_id
        and final_attempt_status == "succeeded"
        and _present(final_attempt_model_id)
        and _present(final_attempt_provider_id)
    ):
        # Fallback 场景以 final succeeded attempt 为准，RequestLog 快照由数据库 trigger 再交叉验真。
        return FeedbackEligibilityResult(
            eligible=True,
            reason="eligible",
            model_id=final_attempt_model_id,
            provider_id=final_attempt_provider_id,
        )
    return _ineligible("attribution_unavailable")


def _ineligible(reason: str) -> FeedbackEligibilityResult:
    """构造不携带猜测模型归因的稳定不可反馈结果。"""

    return FeedbackEligibilityResult(
        eligible=False,
        reason=reason,  # type: ignore[arg-type]  # 调用点只传冻结的五个原因码。
        model_id=None,
        provider_id=None,
    )


def _present(value: object) -> bool:
    """判断数据库 ID 是否为非空文本，不对内部事实执行 strip 或自动修复。"""

    return type(value) is str and bool(value)
