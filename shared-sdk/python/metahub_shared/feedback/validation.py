"""PRD-049 反馈意图的严格规范化、指纹和整数统计算法。

所有函数都是框架无关纯函数，Control 与 Gateway 不得各自复制评分、标签或四舍五入规则。
这里不接触数据库和日志，因此不会扩大 Prompt、Response 或 Key 的持久化范围。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from metahub_shared.runtime.errors import ApiError

from .contracts import FEEDBACK_TAGS, FeedbackIntent

# ID、字段集合、标签互斥组和指纹版本共同冻结 v1 机器合同，修改时必须显式升级 schema。
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", re.ASCII)
FEEDBACK_PAYLOAD_FIELDS = frozenset({"request_id", "rating", "accepted", "tags"})
FEEDBACK_TAG_INDEX = {tag: index for index, tag in enumerate(FEEDBACK_TAGS)}
FEEDBACK_TAG_CONFLICT_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"accurate", "inaccurate"}),
    frozenset({"relevant", "irrelevant"}),
    frozenset({"complete", "incomplete"}),
)
FEEDBACK_FINGERPRINT_SCHEMA_VERSION = 1


def normalize_feedback_payload(payload: object) -> FeedbackIntent:
    """校验四字段反馈对象并返回带稳定指纹的规范化意图。

    参数：已由严格 JSON 解码器得到的对象。
    返回：固定标签顺序、强类型评分/采纳和 v1 SHA-256 指纹。
    约束：不接受 tenant、actor、模型、Provider、未知字段或布尔冒充整数。
    """

    if type(payload) is not dict or set(payload) != FEEDBACK_PAYLOAD_FIELDS:
        # 精确字段集合阻止客户端注入租户、actor 或数据库恢复的路由归因。
        raise ApiError("feedback.input_invalid")
    source_request_id = normalize_safe_identifier(
        payload["request_id"], error_code="feedback.input_invalid"
    )
    rating = payload["rating"]
    if type(rating) is not int or not 1 <= rating <= 5:
        # bool 是 int 的子类，必须用精确类型拒绝 true/false 冒充 1/0 评分。
        raise ApiError("feedback.input_invalid")
    accepted = payload["accepted"]
    if type(accepted) is not bool:
        # 采纳率分母要求每条反馈都有明确布尔结论，null、0/1 和字符串均拒绝。
        raise ApiError("feedback.input_invalid")
    tags = _normalize_tags(payload["tags"])
    canonical = feedback_canonical_json_bytes(
        source_request_id=source_request_id,
        rating=rating,
        accepted=accepted,
        tags=tags,
    )
    return FeedbackIntent(
        source_request_id=source_request_id,
        rating=rating,
        accepted=accepted,
        tags=tags,
        request_fingerprint=hashlib.sha256(canonical).hexdigest(),
    )


def feedback_request_fingerprint(intent: FeedbackIntent) -> str:
    """按 v1 canonical JSON 重新计算反馈意图指纹。

    调用方可在跨入口测试或持久事实复核时使用；函数不会信任 DTO 中已有指纹，也不会把
    tenant、actor、操作 request_id、trace 或最终模型纳入意图。
    """

    canonical = feedback_canonical_json_bytes(
        source_request_id=intent.source_request_id,
        rating=intent.rating,
        accepted=intent.accepted,
        tags=intent.tags,
    )
    return hashlib.sha256(canonical).hexdigest()


def feedback_canonical_json_bytes(
    *,
    source_request_id: str,
    rating: int,
    accepted: bool,
    tags: tuple[str, ...],
) -> bytes:
    """生成字段顺序和编码均固定的 v1 canonical JSON bytes。"""

    payload = {
        "schema_version": FEEDBACK_FINGERPRINT_SCHEMA_VERSION,
        "source_request_id": source_request_id,
        "rating": rating,
        "accepted": accepted,
        "tags": list(tags),
    }
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=False,
            allow_nan=False,
        ).encode("utf-8")
    # DTO 被内部代码错误构造时也要失败关闭，不能形成与 HTTP 入口不同的指纹。
    except (TypeError, ValueError) as exc:
        raise ApiError("feedback.input_invalid") from exc


def normalize_safe_identifier(value: object, *, error_code: str) -> str:
    """校验链路和对象 ID 的 ASCII 安全字符集，阻止控制字符进入日志与索引。"""

    if type(value) is not str or SAFE_IDENTIFIER_RE.fullmatch(value) is None:
        # 不对非法值 strip、替换或截断，避免不同外部 ID 被折叠为同一幂等键。
        raise ApiError(error_code)
    return value


def round_ratio_half_up(numerator: int, denominator: int, scale: int) -> int:
    """使用 Python 任意精度整数计算非负比率的 half-up 结果。

    参数：非负分子、非负分母和正整数缩放；分母为零固定返回零。
    返回：``numerator / denominator * scale`` 的整数 half-up 值。
    约束：禁止 SQLite 先乘 scale 或 float 参与权威统计，以免溢出或跨语言漂移。
    """

    if (
        type(numerator) is not int
        or type(denominator) is not int
        or type(scale) is not int
        or numerator < 0
        or denominator < 0
        or scale <= 0
    ):
        # 持久计数损坏必须失败关闭，不能通过截断、abs 或默认零掩盖统计错误。
        raise ValueError("feedback ratio input invalid")
    if denominator == 0:
        return 0
    return (2 * numerator * scale + denominator) // (2 * denominator)


def rating_distribution(values: Mapping[int | str, int]) -> dict[str, int]:
    """把五档计数恢复为稳定字符串键顺序，并拒绝负数或非整数。"""

    distribution: dict[str, int] = {}
    for rating in range(1, 6):
        value = values.get(rating, values.get(str(rating), 0))
        if type(value) is not int or value < 0:
            # 评分分布是权威聚合结果，类型漂移或负数不能静默归零。
            raise ValueError("feedback rating distribution invalid")
        distribution[str(rating)] = value
    return distribution


def _normalize_tags(value: object) -> tuple[str, ...]:
    """校验标签白名单、数量、唯一性和互斥关系后按固定顺序返回。"""

    if type(value) is not list or len(value) > 5:
        # 必须显式提交数组；自由文本、tuple 或超限标签会扩大敏感数据存储面。
        raise ApiError("feedback.input_invalid")
    if any(type(tag) is not str or tag not in FEEDBACK_TAG_INDEX for tag in value):
        # 固定英文 code 阻止 Prompt、个人信息或业务正文借标签进入事件和报表。
        raise ApiError("feedback.input_invalid")
    if len(set(value)) != len(value):
        # 重复标签会扭曲 tag count，也会让同一意图产生多个非规范表示。
        raise ApiError("feedback.input_invalid")
    selected = set(value)
    if any(len(selected.intersection(group)) > 1 for group in FEEDBACK_TAG_CONFLICT_GROUPS):
        # 正反标签同时出现会污染满意度语义，因此在指纹计算前整体拒绝。
        raise ApiError("feedback.input_invalid")
    return tuple(sorted(value, key=FEEDBACK_TAG_INDEX.__getitem__))
