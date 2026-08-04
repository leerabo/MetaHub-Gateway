"""PRD-049 调用反馈的跨服务数据合同。

本模块只承载经过认证和规范化后的安全事实，不接收 HTTP Request，也不保存 Prompt、
Response、Token、Provider Key 或自由文本。Control 与 Gateway 复用这些不可变结构，
从而让双入口在租户、actor、幂等和事件语义上保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# actor 与资格原因是 PRD-049 的版本化机器合同；新增值必须同步数据库和两类服务入口。
FeedbackActorType = Literal["session_user", "virtual_key"]
FeedbackEligibilityReason = Literal[
    "eligible",
    "already_recorded",
    "request_failed",
    "stream_incomplete",
    "attribution_unavailable",
]
# 固定标签顺序同时决定规范化 JSON、指纹、报表和 Webhook，不允许服务端各自排序。
FEEDBACK_TAGS: tuple[str, ...] = (
    "accurate",
    "relevant",
    "complete",
    "helpful",
    "clear",
    "inaccurate",
    "irrelevant",
    "incomplete",
    "unsafe",
    "slow",
)
# Summary 从 HTTP 进入中间件到 DTO 返回共享 250ms，认证、排队和 SQL 都不能重新计时。
FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS = 0.250


@dataclass(frozen=True, slots=True)
class FeedbackActor:
    """由认证层恢复的反馈 actor，客户端不能提交或覆盖这些字段。

    ``session_user`` 只能携带 ``actor_user_id``，``virtual_key`` 只能携带
    ``actor_virtual_key_id``。领域服务和数据库 trigger 会分别复核 exact-one 关系，
    避免伪造管理员或让一把 Key 评价另一把 Key 的调用。
    """

    tenant_id: str
    actor_type: FeedbackActorType
    actor_user_id: str | None = None
    actor_virtual_key_id: str | None = None

    @property
    def actor_id(self) -> str:
        """返回事件信封使用的可信 actor ID；结构损坏时失败关闭。"""

        # Session actor 只能采用认证 user ID；此分支不能读取备用 Key 字段进行宽松回退。
        if self.actor_type == "session_user" and self.actor_user_id is not None:
            # Session 事件必须绑定真实用户，不能退化为空 actor 或 Virtual Key。
            return self.actor_user_id
        if self.actor_type == "virtual_key" and self.actor_virtual_key_id is not None:
            # 这里只返回 Key 对象 ID，不会返回认证使用的 Virtual Key 明文或前缀。
            return self.actor_virtual_key_id
        # 可信认证上下文不完整属于内部一致性故障，不能猜测另一 actor 列。
        raise ValueError("feedback actor identity is incomplete")


@dataclass(frozen=True, slots=True)
class FeedbackIntent:
    """严格规范化后的反馈意图及其 v1 指纹。

    标签已经按固定白名单顺序排序；指纹只覆盖源调用、评分、采纳和标签，不包含租户、
    actor、操作 request_id、trace 或数据库恢复的模型归因。
    """

    source_request_id: str
    rating: int
    accepted: bool
    tags: tuple[str, ...]
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    """可安全返回给客户端的不可变 RequestFeedback 事实。

    对外结构刻意不包含 tenant_id、actor ID、Provider ID、Virtual Key ID、指纹或事件载荷，
    防止租户归属和内部路由细节越过反馈接口边界。
    """

    request_feedback_id: str
    source_request_id: str
    model_id: str
    rating: int
    accepted: bool
    tags: tuple[str, ...]
    actor_type: FeedbackActorType
    status: Literal["recorded"]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FeedbackEventRecord:
    """从持久事件行恢复的提交后投递事实。

    该内部 DTO 包含 tenant 和 actor 对象 ID，唯一用途是构造 Webhook 信封；调用方不得
    把它直接序列化为 Feedback API 响应或日志 payload。
    """

    feedback_event_id: str
    tenant_id: str
    request_feedback_id: str
    source_request_id: str
    event_type: Literal["feedback.created"]
    actor_type: FeedbackActorType
    actor_id: str
    operation_request_id: str
    trace_id: str
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FeedbackCreateResult:
    """一次创建或幂等恢复的完整结果。

    ``event`` 永远是首次提交时持久化的原事件；重放响应使用本次 HTTP trace，但投递必须
    继续使用该事件的首次 trace/time 和稳定 event ID。
    """

    feedback: FeedbackRecord
    event: FeedbackEventRecord
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class FeedbackEligibilityResult:
    """RequestLog 详情和 POST 共用的可反馈资格与最终归因。"""

    eligible: bool
    reason: FeedbackEligibilityReason
    model_id: str | None
    provider_id: str | None


@dataclass(frozen=True, slots=True)
class FeedbackTagCount:
    """窗口内一个固定标签的出现次数，顺序遵循 ``FEEDBACK_TAGS``。"""

    tag: str
    count: int


@dataclass(frozen=True, slots=True)
class FeedbackSummaryTotals:
    """完整租户/模型过滤窗口的反馈汇总，不受模型行分页影响。"""

    feedback_count: int
    rating_sum: int
    average_rating_milli: int
    accepted_count: int
    adoption_rate_bps: int
    rating_distribution: dict[str, int]
    tag_counts: tuple[FeedbackTagCount, ...]


@dataclass(frozen=True, slots=True)
class FeedbackModelSummary:
    """窗口内单个模型的满意度和采纳率行。"""

    model_id: str
    internal_model_id: str
    display_name: str
    feedback_count: int
    average_rating_milli: int
    accepted_count: int
    adoption_rate_bps: int
    rating_distribution: dict[str, int]


@dataclass(frozen=True, slots=True)
class FeedbackSummaryResult:
    """同一 SQLite 只读快照内形成的租户反馈报表。"""

    scope_type: Literal["tenant"]
    from_time: datetime
    to_time: datetime
    summary: FeedbackSummaryTotals
    models: tuple[FeedbackModelSummary, ...]
    total: int
    limit: int
    offset: int
