"""调用反馈共享领域入口。

Control 与 Gateway 只能从此处复用严格 wire 解析、资格状态机、事务写入、汇总计算和持久
Webhook 转换；不得在服务包内复制标签、指纹、租户或 actor 权限规则。
"""

from .contracts import (
    FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS,
    FEEDBACK_TAGS,
    FeedbackActor,
    FeedbackCreateResult,
    FeedbackEligibilityResult,
    FeedbackEventRecord,
    FeedbackIntent,
    FeedbackModelSummary,
    FeedbackRecord,
    FeedbackSummaryResult,
    FeedbackSummaryTotals,
    FeedbackTagCount,
)
from .eligibility import evaluate_feedback_eligibility
from .storage import (
    FEEDBACK_DATABASE_DEADLINE_SECONDS,
    create_request_feedback,
    get_request_feedback,
    load_request_feedback_from_connection,
    query_feedback_summary,
)
from .validation import (
    feedback_request_fingerprint,
    normalize_feedback_payload,
    rating_distribution,
    round_ratio_half_up,
)
from .webhook import feedback_event_to_webhook
from .wire import (
    MAX_FEEDBACK_BODY_BYTES,
    parse_feedback_json_bytes,
    resolve_feedback_operation_request_id,
)

# 显式导出表冻结后端可依赖的 API，内部 SQL/解析辅助函数不得跨服务引用。
__all__ = [
    "FEEDBACK_DATABASE_DEADLINE_SECONDS",
    "FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS",
    "FEEDBACK_TAGS",
    "MAX_FEEDBACK_BODY_BYTES",
    "FeedbackActor",
    "FeedbackCreateResult",
    "FeedbackEligibilityResult",
    "FeedbackEventRecord",
    "FeedbackIntent",
    "FeedbackModelSummary",
    "FeedbackRecord",
    "FeedbackSummaryResult",
    "FeedbackSummaryTotals",
    "FeedbackTagCount",
    "create_request_feedback",
    "evaluate_feedback_eligibility",
    "feedback_event_to_webhook",
    "feedback_request_fingerprint",
    "get_request_feedback",
    "load_request_feedback_from_connection",
    "normalize_feedback_payload",
    "parse_feedback_json_bytes",
    "query_feedback_summary",
    "rating_distribution",
    "resolve_feedback_operation_request_id",
    "round_ratio_half_up",
]
