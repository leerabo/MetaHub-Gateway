"""审计列表与 CSV 导出共用的租户筛选规范。

本模块只处理白名单 query、UTC 时间和 tenant-first SQL 条件，不读取认证信息，也不接受客户端
传入 tenant_id。列表分页属于展示层，不能混入导出指纹或快照查询。
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from metahub_shared.runtime.errors import ApiError

# 所有精确筛选共用同一长度上限，避免列表与导出接受范围发生漂移。
MAX_FILTER_LENGTH = 160


@dataclass(frozen=True, slots=True)
class AuditFilters:
    """已经规范化、可稳定指纹化的七个审计筛选条件。"""

    actor_user_id: str | None
    action: str | None
    object_type: str | None
    object_id: str | None
    request_id: str | None
    created_from: str | None
    created_to: str | None

    def canonical_json(self) -> str:
        """返回固定键集合 canonical JSON，供 AuditExport 幂等指纹和复核使用。"""

        return json.dumps(
            {
                "action": self.action,
                "actor_user_id": self.actor_user_id,
                "created_from": self.created_from,
                "created_to": self.created_to,
                "object_id": self.object_id,
                "object_type": self.object_type,
                "request_id": self.request_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def normalize_audit_filters(
    *,
    actor_user_id: str | None,
    action: str | None,
    object_type: str | None,
    object_id: str | None,
    request_id: str | None,
    created_from: str | None,
    created_to: str | None,
    allowed_actions: Collection[str],
    allowed_object_types: Collection[str],
) -> AuditFilters:
    """规范化七个共享筛选，并拒绝未知 action/object 与反向时间范围。

    参数：客户端筛选和服务端白名单。返回：可供列表与导出共用的不可变筛选。
    约束：时间必须带时区，结束为开区间且严格晚于开始；所有 ID 均为有界精确值。
    """

    normalized_actor = _optional_text(actor_user_id)
    normalized_object_id = _optional_text(object_id)
    normalized_request_id = _optional_text(request_id)
    normalized_action = _optional_enum(action, allowed_actions)
    normalized_object_type = _optional_enum(object_type, allowed_object_types)
    start = _optional_time(created_from)
    end = _optional_time(created_to)
    if start and end and end <= start:
        # 开区间结束不晚于开始没有稳定业务含义，列表和导出必须给出同一个领域错误。
        raise ApiError("audit_log.time_range_invalid")
    return AuditFilters(
        actor_user_id=normalized_actor,
        action=normalized_action,
        object_type=normalized_object_type,
        object_id=normalized_object_id,
        request_id=normalized_request_id,
        created_from=_to_db_time(start) if start else None,
        created_to=_to_db_time(end) if end else None,
    )


def audit_where_clause(
    tenant_id: str,
    filters: AuditFilters,
    *,
    table_alias: str | None = None,
) -> tuple[str, list[object]]:
    """生成 tenant-first 参数化 WHERE 子句，不插入任何客户端 SQL 片段。"""

    prefix = f"{table_alias}." if table_alias else ""
    clauses = [f"{prefix}tenant_id=?"]
    params: list[object] = [tenant_id]
    for column, value in (
        ("actor_user_id", filters.actor_user_id),
        ("action", filters.action),
        ("object_type", filters.object_type),
        ("object_id", filters.object_id),
        ("request_id", filters.request_id),
    ):
        if value is not None:
            clauses.append(f"{prefix}{column}=?")
            params.append(value)
    if filters.created_from is not None:
        clauses.append(f"{prefix}created_at>=?")
        params.append(filters.created_from)
    if filters.created_to is not None:
        clauses.append(f"{prefix}created_at<?")
        params.append(filters.created_to)
    return " AND ".join(clauses), params


def _optional_text(value: str | None) -> str | None:
    """规范化可选精确文本；空白值视为未筛选，越界或控制字符失败关闭。"""

    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if len(normalized) > MAX_FILTER_LENGTH or any(ord(character) < 32 for character in normalized):
        # 超长或控制字符会破坏稳定指纹与查询边界，统一拒绝。
        raise ApiError("audit_log.filter_invalid")
    return normalized


def _optional_enum(value: str | None, allowed: Collection[str]) -> str | None:
    """规范化可选枚举；未知值不能退化成空结果掩盖客户端或版本错误。"""

    normalized = _optional_text(value)
    if normalized is None:
        return None
    candidate = normalized.lower()
    if candidate not in allowed:
        # 未登记枚举不能静默变成空结果，否则版本错误会被误认为没有审计事实。
        raise ApiError("audit_log.filter_invalid")
    return candidate


# 时间筛选必须在列表与导出之间保持相同的时区和开区间语义。
def _optional_time(value: str | None) -> datetime | None:
    """解析可选的带时区 ISO-8601 时间，并统一转换为可比较的 UTC。"""

    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    # 解析失败的原始输入可能包含控制内容，不得进入错误消息或日志。
    except ValueError as exc:
        # 原始值可能是用户输入，不得拼进错误或日志；统一使用稳定时间范围错误。
        raise ApiError("audit_log.time_range_invalid") from exc
    if parsed.tzinfo is None:
        # 无时区值随服务器部署位置变化，无法形成确定性审计快照。
        raise ApiError("audit_log.time_range_invalid")
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把筛选边界稳定表示为 UTC 秒级文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["AuditFilters", "audit_where_clause", "normalize_audit_filters"]
